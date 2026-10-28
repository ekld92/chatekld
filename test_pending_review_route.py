from types import SimpleNamespace

import fitz
import flask
import pytest

from accession import review, cache, reconciliation
from api.routes import pending
from api.security import register_origin_guard
from audit.engine import bridge

HEADERS = {'X-Requested-With': 'ChatEKLD'}


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / 'vault'
    folder = vault / 'pdfs'
    folder.mkdir(parents=True)
    pdf = fitz.open()
    for _ in range(2):
        pdf.new_page().insert_text((30, 30), 'A test PDF title')
    path = folder / 'one.pdf'
    pdf.save(path)
    pdf.close()
    bib = vault / 'master.bib'
    bib.write_text('@article{key1, title={A test PDF title}, doi={10.1234/one}, year={2025}}')
    settings = SimpleNamespace(vault_root=vault, biblio_articles_dir=folder,
                               master_bib=bib, mapping_file=tmp_path / 'mapping.json')
    inv = SimpleNamespace(records={})
    monkeypatch.setattr(pending.audit_manager, 'get_inventory', lambda: (inv, settings))
    monkeypatch.setattr(review, 'store_path', lambda: tmp_path / 'reviews.sqlite3')
    monkeypatch.setattr(reconciliation, 'store_path', lambda: tmp_path / 'reconciliation.sqlite3')
    monkeypatch.setattr(cache, 'cache_path', lambda: tmp_path / 'cache.json')
    memo = {}
    cache.store(memo, review.identity(path)['sha256'], {'candidates':[{'title':'A test PDF title','doi':'10.1234/one','source':'fixture'}]})
    cache.save_cache(memo)
    bridge.set_curated_state(settings.mapping_file, [path], vault, 'pending', require_hash=True)
    app = flask.Flask(__name__)
    register_origin_guard(app)
    app.register_blueprint(pending.pending_bp)
    return app.test_client(), settings, path


def post(env, name, data):
    return env[0].post('/api/accession/review/' + name, json=data, headers=HEADERS)


def confirmed(env):
    record = post(env, 'open', {'pdf':'pdfs/one.pdf'}).get_json()
    response = post(env, 'decision', {'pdf':'pdfs/one.pdf','token':record['token'], 'candidate_id':record['candidate_ids'][0],'action':'confirm','confirm':True})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_auth_page_bounds_and_changed_pdf(env):
    assert env[0].post('/api/accession/review/open', json={'pdf':'pdfs/one.pdf'}).status_code == 403
    record = confirmed(env)
    query = dict(pdf='pdfs/one.pdf', sha256=record['sha256'], page=2, scale=2)
    response = env[0].get('/api/accession/review/page', query_string=query, headers=HEADERS)
    assert response.status_code == 200
    assert response.data.startswith(b'\x89PNG')
    query['page'] = 3
    assert env[0].get('/api/accession/review/page', query_string=query, headers=HEADERS).status_code == 400
    env[2].write_bytes(b'changed')
    assert env[0].get('/api/accession/review/page', query_string=query, headers=HEADERS).status_code == 409


def test_copy_selection_dedup_and_stale_decision(env):
    record = confirmed(env)
    item = {'pdf':'pdfs/one.pdf','token':record['token']}
    response = post(env,'export',{'items':[item,item,{'pdf':'pdfs/one.pdf','token':None}]}).get_json()
    assert response['dois'] == ['10.1234/one']
    assert len(response['excluded']) == 1
    assert bridge._load_mapping_all(env[1].mapping_file).pending == {'pdfs/one.pdf'}


def test_fresh_bibliography_and_superseding_mapping(env):
    record = confirmed(env)
    item = {'pdf':'pdfs/one.pdf','token':record['token']}
    recon = post(env,'reconcile',{'items':[item]}).get_json()
    assert recon['rows'][0]['matches'][0]['citation_key'] == 'key1'
    data = {**recon['rows'][0], 'citation_key':'key1','bibliography_revision':recon['bibliography_revision'],'confirm':True}
    bridge.set_curated_state(env[1].mapping_file, [env[2]], env[1].vault_root, 'no_match', require_hash=True)
    assert post(env, 'link', data).status_code == 409
    assert 'pdfs/one.pdf' in bridge._load_mapping_all(env[1].mapping_file).no_match


def test_link_and_repeat_reconciliation(env):
    record = confirmed(env)
    item = {'pdf':'pdfs/one.pdf','token':record['token']}
    def link_once():
        recon = post(env,'reconcile',{'items':[item]}).get_json()
        return post(env,'link',{**recon['rows'][0], 'citation_key':'key1','bibliography_revision':recon['bibliography_revision'],'confirm':True})
    assert link_once().status_code == 200
    assert link_once().status_code == 200
    mapping = bridge._load_mapping_all(env[1].mapping_file)
    assert mapping.matches['key1'] == ['pdfs/one.pdf']
    assert not mapping.pending


def test_traversal_and_new_bibliography_version(env):
    assert post(env,'open',{'pdf':'../outside.pdf'}).status_code in (400,503)
    record = confirmed(env)
    recon = post(env,'reconcile',{'items':[{'pdf':'pdfs/one.pdf','token':record['token']}]}).get_json()
    env[1].master_bib.write_text('@article{key2, title={New}, doi={10.1234/one}}')
    data = {**recon['rows'][0], 'citation_key':'key1','bibliography_revision':recon['bibliography_revision'],'confirm':True}
    assert post(env,'link',data).status_code == 409
    fresh = post(env,'reconcile',{'items':[{'pdf':'pdfs/one.pdf','token':record['token']}]}).get_json()
    assert fresh['rows'][0]['matches'][0]['citation_key'] == 'key2'


def test_replacement_during_mapping_hash_is_rejected(env, monkeypatch):
    record = confirmed(env)
    recon = post(env, 'reconcile', {'items':[{'pdf':'pdfs/one.pdf', 'token':record['token']}]}).get_json()
    original = bridge._hash_entry
    def replace_after_hash(path):
        result = original(path)
        replacement = path.with_name('replacement.pdf')
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        return result
    monkeypatch.setattr(bridge, '_hash_entry', replace_after_hash)
    data = {**recon['rows'][0], 'citation_key':'key1', 'bibliography_revision':recon['bibliography_revision'], 'confirm':True}
    assert post(env, 'link', data).status_code == 409
    assert 'pdfs/one.pdf' in bridge._load_mapping_all(env[1].mapping_file).pending


def test_rename_preview_reuses_recipe_without_changing_files(env, monkeypatch):
    from tagging import writers
    monkeypatch.setattr(writers, 'find_linked_attachments', lambda *a: ([], None))
    client, settings, path = env
    settings.master_bib.write_text('@article{key1, title={Paper}, author={Smith, Jane}, year={2025}}')
    bridge.add_match(settings.mapping_file, path, 'key1', settings.vault_root, require_hash=True)
    before = (path.read_bytes(), settings.mapping_file.read_bytes())
    response = post(env, 'rename-preview', {'pdf':'pdfs/one.pdf'})
    import importlib.util
    if importlib.util.find_spec('maintenance') is None:
        assert response.status_code == 501
        assert 'unavailable' in response.get_json()['error']
        return
    assert response.status_code == 200, response.get_json()
    data = response.get_json()
    assert data['physical_filename']['proposed'] == 'smith_2025.pdf'
    assert data['preview_only'] and data['attachment_titles']['action'] == 'preserve'
    assert before == (path.read_bytes(), settings.mapping_file.read_bytes())
    assert not path.with_name('smith_2025.pdf').exists()


@pytest.mark.parametrize('boundary', ['review_lock', 'mapping_hash'])
def test_bibliography_change_while_waiting_to_map_is_refused(env, monkeypatch, boundary):
    from contextlib import contextmanager
    record = confirmed(env)
    recon = post(env, 'reconcile', {'items': [{'pdf': 'pdfs/one.pdf', 'token': record['token']}]}).get_json()
    before = env[1].mapping_file.read_bytes()
    def change():
        env[1].master_bib.write_text('@article{key1,title={Different},doi={10.1234/other}}')
    if boundary == 'review_lock':
        original = review.decision_guard
        @contextmanager
        def guard(*args):
            change()
            with original(*args) as value:
                yield value
        monkeypatch.setattr(review, 'decision_guard', guard)
    else:
        original = bridge._hash_entry
        def hashed(*args):
            change()
            return original(*args)
        monkeypatch.setattr(bridge, '_hash_entry', hashed)
    result = post(env, 'link', {**recon['rows'][0], 'citation_key': 'key1',
                               'bibliography_revision': recon['bibliography_revision'], 'confirm': True})
    assert result.status_code == 409
    assert env[1].mapping_file.read_bytes() == before
