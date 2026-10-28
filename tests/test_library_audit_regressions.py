"""Library audit regressions: disposable PDFs and simulated HTTP only."""
from types import SimpleNamespace
from unittest.mock import patch

import flask
import pytest

from audit.config import Settings
from audit.core.bib import BibEntry
from audit.engine.bridge import build_bridge
from audit.engine.inventory import _walk_biblio_pdfs
from audit.engine.duplicates import candidate_pdfs
from audit.engine.reports.read_unzoterod import find
from tagging import journal, writers, zotero_write
from tagging.local_api import Version


def setup_bridge(tmp_path, pointer='[[smith_2020.pdf]]', *, nested=False):
    settings = Settings(vault_root=tmp_path, mapping_file_override=tmp_path/'mapping.json')
    settings.biblio_articles_dir.mkdir(parents=True)
    settings.zotero_notes_dir.mkdir()
    names = ['a/paper.pdf', 'b/paper.pdf'] if nested else ['smith_2020.pdf']
    pdfs = []
    for name in names:
        path = settings.biblio_articles_dir / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'%PDF-1.4\n' + name.encode())
        pdfs.append(path)
    (settings.zotero_notes_dir/'smith2020.md').write_text(
        f'---\npdf: "{pointer}"\nauthor: Smith, Jane\nyear: 2020\n---\n{pointer}\n')
    entries = [BibEntry('smith2020', 'article', year='2020', authors=['Smith, Jane'])]
    return settings, entries, pdfs


def test_removed_zotero_link_cannot_be_recreated_from_old_note_or_filename(tmp_path):
    settings, entries, pdfs = setup_bridge(tmp_path)
    bridge = build_bridge(settings, entries, pdfs, zotero_links={})
    assert bridge.pdf_to_bib == {}
    assert bridge.unmapped_pdfs == pdfs
    report = find(SimpleNamespace(bridge=bridge, pdfs_skipped=[], zotero_error=None), settings)
    assert [row.pdf for row in report.rows] == pdfs


def test_absent_zotero_observation_does_not_claim_a_removal(tmp_path):
    settings, entries, pdfs = setup_bridge(tmp_path)
    assert build_bridge(settings, entries, pdfs).pdf_to_bib == {pdfs[0]: 'smith2020'}


@pytest.mark.parametrize('qualified', [True, False])
def test_pdf_pointer_never_claims_another_directory(qualified, tmp_path):
    pointer = '[[Z_attachments/biblio_articles/a/paper.pdf]]' if qualified else '[[paper.pdf]]'
    settings, entries, pdfs = setup_bridge(tmp_path, pointer, nested=True)
    bridge = build_bridge(settings, entries, pdfs)
    assert bridge.pdf_to_bib == ({pdfs[0]: 'smith2020'} if qualified else {})


def test_all_pdf_discovery_includes_uppercase_extensions(tmp_path):
    settings, _, pdfs = setup_bridge(tmp_path)
    upper = settings.biblio_articles_dir/'UPPER.PDF'
    upper.write_bytes(b'%PDF-1.4\n')
    expected = {*pdfs, upper}
    assert set(_walk_biblio_pdfs(settings.biblio_articles_dir)) == expected
    assert set(candidate_pdfs(settings)) == expected


@pytest.mark.parametrize('mode,authorized,writable', [('local', False, False), ('local', True, True), ('web', True, False)])
def test_tag_readiness_requires_compatible_selection_and_authorization(mode, authorized, writable):
    from api.routes.tagging import tagging_bp
    app = flask.Flask(__name__)
    app.register_blueprint(tagging_bp)
    with patch('api.routes.tagging.load_config', return_value={'tagging_zotero_transport': mode}), \
         patch('tagging.local_api.capabilities', return_value={'authorized': authorized, 'supports_writes': True}) as probe, \
         patch('tagging.zotero_write._urlopen') as request:
        result = app.test_client().get('/api/tagging/zotero-available').json
    assert result['writable'] is writable
    assert result['transport'] == mode
    assert result['mode'] == mode
    assert isinstance(result['note'], str)
    request.assert_not_called()
    assert probe.call_count == (0 if mode == 'web' else 1)


@pytest.mark.parametrize('mode,authorized,targets,expected', [
    ('web', False, {'zotero': ['tag']}, 409),
    ('local', False, {'zotero': ['tag']}, 409),
    ('local', True, {'zotero': ['tag']}, 200),
    ('web', False, {'note': ['tag']}, 200),
])
def test_harmonize_preflight_refuses_unwritable_zotero_before_worker(tmp_path, mode, authorized, targets, expected):
    from api.routes.tagging import tagging_bp
    settings = SimpleNamespace(vault_root=tmp_path, biblio_articles_dir=tmp_path)
    inv = SimpleNamespace(records={'smith2020': SimpleNamespace(zotero_item=SimpleNamespace(server_id='fixture-db'))})
    app = flask.Flask(__name__)
    app.register_blueprint(tagging_bp)
    with patch('api.routes.tagging.audit_manager.get_inventory', return_value=(inv, settings)), \
         patch('api.routes.tagging.load_config', return_value={'tagging_zotero_transport': mode}), \
         patch('api.routes.tagging._harmonize_plan', return_value=([('smith2020', targets)], [], [])), \
         patch('tagging.local_api.capabilities', return_value={'authorized': authorized, 'supports_writes': True}), \
         patch('api.routes.tagging.run_sse_worker', return_value='started') as worker, \
         patch('tagging.zotero_write.get_item_tags_full') as read:
        response = app.test_client().post('/api/tagging/harmonize', json={'keys': ['smith2020'], 'confirm': True})
    assert response.status_code == expected
    assert worker.call_count == (1 if expected == 200 else 0)
    read.assert_not_called()


@pytest.mark.parametrize('remove', [False, True])
@pytest.mark.parametrize('sent', [False, True])
def test_tag_uncertainty_survives_journal_pruning(tmp_path, remove, sent):
    manifest = {'ops': []}
    prepared = dict(base='http://127.0.0.1:23119', lib_path='users/0', item_key='ABCDEFGH',
                    version=Version(3, 'fixture-db'), before=['old'], before_raw=[{'tag': 'old'}],
                    added=['new'], removed=['old'], after=['new'], after_payload=['new'], transport='local')
    error = zotero_write.ZoteroUnavailable('lost reply') if sent else zotero_write.ZoteroNotSent('refused')
    fn = writers._zotero_remove_commit_locked if remove else writers._zotero_commit_locked
    kwargs = {} if remove else {'rejected': []}
    with patch.object(journal, 'save'), patch.object(zotero_write, 'set_item_tags', side_effect=error) as send:
        result, status = fn(prepared=prepared, cfg={}, vault_root=tmp_path, citation_key='K', manifest=manifest, **kwargs)
    assert status == 503
    assert send.call_count == 1
    assert bool(result.get('outcome_unknown')) is sent
    assert manifest['ops'][0]['state'] == ('outcome_unknown' if sent else 'failed')
    with patch.object(journal, 'archive_dir', return_value=tmp_path):
        journal.prune(tmp_path, {}, manifest)
    assert len(manifest['ops']) == int(sent)


@pytest.mark.parametrize('raw,can_restore', [([{'tag': 'new'}], True), ([{'tag': 'new', 'type': 1}], False), ([{'tag': 'old'}], False)])
def test_unknown_tag_write_is_verified_without_replaying_it(raw, can_restore):
    op = dict(kind='zotero_tag', state='outcome_unknown', base_url='http://127.0.0.1:23119',
              transport='local', item_key='ABCDEFGH', server_id='fixture-db', lib_path='users/0',
              before=['old'], before_raw=[{'tag': 'old'}], after=['new'], after_raw=[{'tag': 'new'}])
    with patch.object(zotero_write, 'get_item_tags_full', return_value=(Version(8, 'fixture-db'), raw)) as read, \
         patch.object(zotero_write, 'set_item_tags') as send:
        result = writers._revert_zotero_prepare({}, op)
    assert ('commit' in result) is can_restore
    assert read.call_args.kwargs['server_id'] == 'fixture-db'
    send.assert_not_called()
    assert op['state'] == 'outcome_unknown'


def test_missing_qualified_pointer_does_not_fall_back_to_basename(tmp_path):
    settings, entries, pdfs = setup_bridge(tmp_path, '[[missing/smith_2020.pdf]]')
    # Remove independent author/year evidence from this fixture.
    (settings.zotero_notes_dir/'smith2020.md').write_text('---\npdf: "[[missing/smith_2020.pdf]]"\n---\n[[missing/smith_2020.pdf]]')
    entries[0].authors = []
    result = build_bridge(settings, entries, pdfs)
    assert result.pdf_to_bib == {}


def test_current_manual_decision_is_preserved_without_a_zotero_link(tmp_path):
    import json
    settings, entries, pdfs = setup_bridge(tmp_path)
    settings.mapping_file.write_text(json.dumps({'matches': {'smith2020': ['Z_attachments/biblio_articles/smith_2020.pdf']}}))
    before = settings.mapping_file.read_bytes()
    result = build_bridge(settings, entries, pdfs, zotero_links={})
    assert result.pdf_to_bib == {pdfs[0]: 'smith2020'}
    assert settings.mapping_file.read_bytes() == before


def test_unknown_removal_of_all_tags_can_be_checked(tmp_path):
    op = dict(kind='zotero_untag', state='outcome_unknown', transport='local',
              item_key='ABCDEFGH', server_id='fixture-db', before=['old'],
              before_raw=[{'tag': 'old'}], after=[], after_raw=[])
    with patch.object(zotero_write, 'get_item_tags_full', return_value=(Version(9, 'fixture-db'), [])):
        result = writers._revert_zotero_prepare({}, op)
    assert result['commit']['restore_tags'] == [{'tag': 'old'}]


def test_linked_item_without_citation_key_is_not_offered_as_an_orphan(tmp_path):
    from audit.engine.inventory import zotero_link_index
    from audit.engine.bridge import zotero_unclaimed
    settings, entries, pdfs = setup_bridge(tmp_path)
    item = SimpleNamespace(citation_key=None, attachments=[SimpleNamespace(
        link_mode='linked_file', raw_path=str(pdfs[0]))])
    index = zotero_link_index([item], pdfs)
    assert index == {pdfs[0]: ['']}
    result = build_bridge(settings, entries, pdfs, zotero_links=index)
    assert zotero_unclaimed(result, pdfs[0])
    assert not find(SimpleNamespace(bridge=result, pdfs_skipped=[], zotero_error=None), settings).rows
