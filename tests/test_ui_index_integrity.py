"""Disposable end-to-end seams for index admission, scope and run evidence."""
import hashlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('change,reason', [
    ({}, None), ({'embed': 'different'}, 'embed_model_mismatch'),
    ({'embed': ''}, 'missing_embedding_identity'),
    ({'embed_provider': ''}, 'missing_embedding_identity'),
    ({'embed_provider': 'lm_studio'}, 'embed_backend_mismatch'),
    ({'version': 'old'}, 'index_version_mismatch'),
    ({'vault_path': '/other'}, 'vault_identity_mismatch'),
])
def test_admission_is_read_only(tmp_path, change, reason):
    from core.constants import OBSIDIAN_INDEX_VERSION
    from rag.index_admission import index_admission
    meta = dict(version=OBSIDIAN_INDEX_VERSION, embed='embed', embed_provider='ollama', vault_path=str(tmp_path))
    meta.update(change)
    manager = SimpleNamespace(_index_dir_has_vector_data=lambda _: True,
                              _read_index_meta=lambda: meta,
                              _prev_embed_provider=lambda m: m.get('embed_provider'),
                              _incremental_store_is_intact=lambda _: True)
    before = list(tmp_path.iterdir())
    result = index_admission(manager, tmp_path, tmp_path, 'embed', 'ollama')
    assert result['reason'] == reason
    assert result['ready'] == (reason is None)
    assert list(tmp_path.iterdir()) == before
    assert index_admission(manager, tmp_path, tmp_path, 'embed', 'ollama', force_fresh=True)['mode'] == 'rebuild'


def test_initial_and_torn_store(tmp_path):
    from core.constants import OBSIDIAN_INDEX_VERSION
    from rag.index_admission import index_admission
    manager = Mock()
    manager._index_dir_has_vector_data.return_value = False
    assert index_admission(manager, tmp_path, tmp_path, 'e', 'ollama')['mode'] == 'initial'
    manager._index_dir_has_vector_data.return_value = True
    manager._read_index_meta.return_value = dict(version=OBSIDIAN_INDEX_VERSION, embed='e', vault_path=str(tmp_path))
    manager._prev_embed_provider.return_value = 'ollama'
    manager._incremental_store_is_intact.return_value = False
    assert index_admission(manager, tmp_path, tmp_path, 'e', 'ollama')['reason'] == 'interrupted_store'
    manager._read_index_meta.return_value = None
    assert index_admission(manager, tmp_path, tmp_path, 'e', 'ollama')['reason'] == 'missing_metadata'


def test_ten_thousand_cached_sources_only_preview_two(tmp_path, monkeypatch):
    from api.routes import online_ocr as route
    from core import extraction_cache
    vault = tmp_path / 'vault'
    vault.mkdir()
    # Discovery/stat identity is faked at its seam, page preview is instrumented.
    sources = [(vault / f'{i}.pdf', f'{i}.pdf') for i in range(10002)]
    # Physical continuity now reads real stat evidence independently of the
    # cached content signature. Keep that seam real without parsing any PDF.
    for path, _ in sources:
        path.touch()
    monkeypatch.setattr(route, '_discover_sources', lambda _: sources)
    monkeypatch.setattr(route, 'load_config', lambda: {})
    monkeypatch.setattr(route.obsidian_manager, '_load_pdf_signature_cache', lambda: {})
    monkeypatch.setattr(route.obsidian_manager, '_pdf_file_signature',
                        lambda p, *args: {'sha256': p.stem, 'size': 1, 'mtime_ns': 1})
    monkeypatch.setattr(extraction_cache, 'cache_coverage',
                        lambda roots, kind, key, digest, **kw: 'whole_unverified' if int(digest) < 10000 else 'missing')
    seen = []
    def preview(vault, path, rel, **kwargs):
        seen.append(rel)
        return dict(source=rel, source_sha256=path.stem, pages=1, eligible=0, kind='pdf', has_cached_text=False)
    monkeypatch.setattr(route, '_preview', preview)
    monkeypatch.setattr(route, '_candidate_page_count', lambda _: 1)
    monkeypatch.setattr(route.obsidian_manager, '_selected_pdf_payload', lambda *args: None)
    result = route._session_inventory(vault, large_run=True, routine=True)
    assert len(seen) == result['source_count'] == 2
    assert result['reused_count'] == 10000
    assert result['eligible'] == result['maximum_attempts'] == 0
    monkeypatch.setattr(extraction_cache, 'cache_coverage', lambda *args, **kw: 'whole_unverified')
    seen.clear()
    assert route._session_inventory(vault, large_run=True, routine=True)['source_count'] == 0
    assert seen == []


def test_partial_range_not_complete_and_symlink_refused(tmp_path):
    from core.extraction_cache import cache_coverage
    directory = tmp_path / 'pdf_cache' / 'vault'
    directory.mkdir(parents=True)
    (directory / 'hash-p00000-00050.txt').write_text('first half')
    def check():
        return cache_coverage([tmp_path], 'pdf_cache', 'vault', 'hash', page_count=lambda: 100)
    assert check() == 'partial'
    (directory / 'hash-p00050-00100.txt').write_text('second half')
    assert check() == 'ranges_unverified'
    (directory / 'hash-p00050-00100.txt').unlink()
    (directory / 'hash-p00050-00100.txt').symlink_to(directory / 'hash-p00000-00050.txt')
    assert check() == 'partial'


def test_manifest_loader_retains_deferred_but_sweeps_deleted(tmp_path, monkeypatch):
    import rag.vault as module
    vault = tmp_path / 'vault'
    vault.mkdir()
    (vault / 'keep.md').write_text('# keep\nText')
    (vault / 'deferred.pdf').write_bytes(b'not parsed')
    (vault / 'arrival.pdf').write_bytes(b'not approved')
    manager = module.ObsidianVaultManager()
    manager.restore_vault_path(str(vault))
    monkeypatch.setattr(module, 'load_config', lambda: {'vault_image_exts': []})
    monkeypatch.setattr(module, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(module, 'OBSIDIAN_INDEX_DIR', str(tmp_path / 'index'))
    monkeypatch.setattr(module, 'extract_structured_from_pdf', lambda *a, **k: pytest.fail('Deferred PDF parsed'))
    docs = list(manager._load_vault_documents(str(vault), online_extraction=True, extraction_manifest={'approved': {}}))
    assert [d.metadata['source'] for d in docs] == ['keep.md']
    assert manager._scan_failed_sources == {'deferred.pdf', 'arrival.pdf'}
    deleted = []
    store = SimpleNamespace(get_document_hash=lambda _: 'same',
                            get_all_ref_doc_info=lambda: {'keep.md::a': None, 'deferred.pdf::a': None, 'gone.pdf::a': None})
    index = SimpleNamespace(docstore=store, delete_ref_doc=lambda doc, **kw: deleted.append(doc))
    manager._index_documents_streaming(index, iter([SimpleNamespace(doc_id='keep.md::a', hash='same', metadata={'source': 'keep.md'})]))
    assert deleted == ['gone.pdf::a']


def test_promotion_preserves_curation_and_external_bytes(tmp_path, monkeypatch):
    from api.routes import online_ocr as route
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'figure.png'
    source.write_bytes(b'fixture image')
    curation = tmp_path / 'base' / 'audit'
    curation.mkdir(parents=True)
    fixtures = {
        'mapping.json': {'matches': {'key': ['figure.png']}, 'hashes': {'figure.png': sha(source)},
                         'provenance': {'key': 'manual'}, 'pending': ['pending.pdf'],
                         'no_match': ['none.pdf'], 'exclusions': ['excluded.pdf']},
        'dup_decisions.json': {'keep': 'annotated.pdf', 'dependency': 'annotation'},
        'pdf_reviews.sqlite3': {'review': 'retained'}, 'reconciliation.sqlite3': {'decision': 'manual'},
        'zotero-fixture.json': {'attachment': 'ABC123', 'version': 7, 'reservation': 'uncertain'},
        'finder-tags-fixture.json': {'figure.png': ['approved']},
    }
    for name, data in fixtures.items():
        (curation / name).write_text(json.dumps(data))
    import sqlite3
    for name in ('pdf_reviews.sqlite3', 'reconciliation.sqlite3', 'zotero.sqlite'):
        path = curation / name
        path.unlink(missing_ok=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE evidence (identity TEXT, decision TEXT, version INTEGER)')
            db.execute('INSERT INTO evidence VALUES (?, ?, ?)', ('attachment:ABC123', 'annotated/manual/uncertain', 7))
    before = {p: p.read_bytes() for p in curation.iterdir()}
    before[source] = source.read_bytes()
    import subprocess
    import sys
    import plistlib
    tags = None
    if sys.platform == 'darwin':
        tags = plistlib.dumps(['fixture-blue\n4'], fmt=plistlib.FMT_BINARY)
        subprocess.run(['/usr/bin/xattr', '-wx', 'com.apple.metadata:_kMDItemUserTags', tags.hex(), str(source)], check=True)

    monkeypatch.setattr(route, 'BASE_DIR', str(tmp_path / 'base'))
    monkeypatch.setattr(route, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(route, 'load_config', lambda: {})
    monkeypatch.setattr(route, '_preflight_index_settings', lambda *a, **kw: None)
    monkeypatch.setattr(route.obsidian_manager, 'heartbeat', lambda _: True)
    monkeypatch.setattr(route, '_cancel', threading.Event())
    monkeypatch.setattr(route.obsidian_manager, 'begin_index_run', lambda _: None)
    monkeypatch.setattr(route.obsidian_manager, 'index_vault', lambda *a, **kw: None)
    monkeypatch.setattr(route.obsidian_manager, 'get_status', lambda: 'done')
    old = dict(route._job)
    try:
        route._job.clear()
        route._promote_session_selected([(vault, source, 'figure.png', {'source_sha256': sha(source), 'recipe': 'fake'}, 'new text')],
                                        1, run_index=True, settings=route._index_settings({}))
        # A second extraction can change suggestions, never saved curation.
        route._promote_session_selected([(vault, source, 'figure.png', {'source_sha256': sha(source), 'recipe': 'fake'}, 'different derived text')],
                                        1, run_index=True, settings=route._index_settings({}))
        assert all(p.read_bytes() == value for p, value in before.items())
        if tags is not None:
            stored = subprocess.check_output(['/usr/bin/xattr', '-px', 'com.apple.metadata:_kMDItemUserTags', str(source)], text=True)
            assert bytes.fromhex(stored) == tags
        assert list((tmp_path / 'base' / 'online_ocr_promotions').glob('*/landed-0000.json'))
    finally:
        route._job.clear()
        route._job.update(old)


def test_history_is_observational_and_vault_bound(tmp_path):
    from core.online_runs import inspect_run, checkpoint
    directory = tmp_path / 'online_index_runs' / ('a' * 32)
    directory.mkdir(parents=True)
    (directory / 'run.json').write_text(json.dumps({'schema': 2, 'vault_key': 'v', 'source_count': 1}))
    (directory / 'source-000000.json').write_text(json.dumps({'source': 'a.pdf', 'status': 'partial'}))
    checkpoint(directory, 'running')
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    result = inspect_run(tmp_path, 'a' * 32, 'v')
    assert result['state'] == 'interrupted'
    assert result['unfinished_count'] == 1
    assert result['retry_allowed']
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    with pytest.raises(ValueError):
        inspect_run(tmp_path, 'a' * 32, 'other')
    with pytest.raises(ValueError):
        inspect_run(tmp_path, '../escape', 'v')


def test_driver_history_replays_endpoint(monkeypatch):
    from core import driver
    from flask import Flask
    from api.routes import online_ocr as route
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    monkeypatch.setattr(route, 'load_config', lambda: {})
    monkeypatch.setattr(driver, 'resolve_base_url', lambda: 'http://fixture')
    seen = []
    def replay(name, **kwargs):
        path, method = driver.ENDPOINTS[name]
        seen.append((path, method))
        return app.test_client().open(path, method=method).json
    monkeypatch.setattr(driver, '_http_json', replay)
    assert 'error' in driver.online_runs()
    assert seen == [('/api/online-ocr/runs', 'GET')]


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("prepare_only", [False, True])
@pytest.mark.parametrize("cancel_continuation", [False, True])
def test_exact_omissions_reach_index_manifest(tmp_path, monkeypatch, prepare_only, cancel_continuation, explicit):
    from flask import Flask
    from api.routes import online_ocr as route
    monkeypatch.setattr(route, '_cancel', threading.Event())
    vault = tmp_path / 'vault'
    vault.mkdir()
    good, bad = vault / 'good.png', vault / 'bad.png'
    good.write_bytes(b'good')
    bad.write_bytes(b'bad')
    base = tmp_path / 'base'
    run_id = 'b' * 32
    directory = base / 'online_index_runs' / run_id
    directory.mkdir(parents=True)
    cfg = {'obsidian_vault_path': str(vault)}
    settings = route._index_settings(cfg)
    inventory = dict(session_token='bound', sources=[
        dict(source='good.png', source_sha256=sha(good)), dict(source='bad.png', source_sha256=sha(bad))], reused=[])
    mode = 'prepare_text' if prepare_only else 'update_search'
    token = route._intent_token('bound', mode) if explicit else 'bound'
    receipt = dict(vault_key=route.vault_key(vault), index_settings=settings,
        session_token=token, full_rebuild=False, routine=True, prepare_only=prepare_only)
    if explicit:
        receipt.update(operation_mode=mode, initiating_action='library_review' if prepare_only else 'search_update')
    (directory / 'run.json').write_text(json.dumps(receipt))
    (directory / 'source-000000.json').write_text(json.dumps(dict(source='good.png', source_sha256=sha(good), status='ocr', recipe='fake')))
    (directory / 'source-000001.json').write_text(json.dumps(dict(source='bad.png', status='partial')))
    monkeypatch.setattr(route, 'BASE_DIR', str(base))
    monkeypatch.setattr(route, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(route, 'load_config', lambda: cfg)
    monkeypatch.setattr(route, '_session_inventory', lambda *a, **kw: inventory)
    monkeypatch.setattr(route, '_preflight_index_settings', lambda *a, **kw: None)
    monkeypatch.setattr(route, '_promotable_text', lambda *a: 'complete fixture text')
    manager = route.obsidian_manager
    monkeypatch.setattr(manager, 'wait_for_indexing', lambda **kw: True)
    monkeypatch.setattr(manager, 'try_acquire_lock', lambda **kw: 1)
    monkeypatch.setattr(manager, 'heartbeat', lambda epoch: True)
    monkeypatch.setattr(manager, 'release_lock', lambda *a: None)
    monkeypatch.setattr(manager, 'begin_index_run', lambda *a: None)
    monkeypatch.setattr(manager, 'get_status', lambda: 'done')
    threads, indexed = [], []
    monkeypatch.setattr(manager, 'register_index_thread', threads.append)
    monkeypatch.setattr(manager, 'index_vault', lambda *a, **kw: indexed.append(kw))
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    client = app.test_client()
    old = dict(route._job)
    route._job.clear()
    try:
        body = dict(run_id=run_id, session_token=token, confirm_index=not (explicit and prepare_only), confirm_omissions=True, omit_sources=[])
        if explicit:
            body.update(operation_mode=mode, confirm_preparation=prepare_only)
        assert client.post('/api/online-ocr/session/continue-incomplete', json=body).status_code == 409
        assert not (directory / 'promotion-started.json').exists()
        body['omit_sources'] = ['bad.png']
        assert client.post('/api/online-ocr/session/continue-incomplete', json=body).status_code == 200
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()
        if prepare_only:
            assert indexed == []
            assert route._job['state'] == 'text_prepared'
        else:
            assert indexed[0]['extraction_manifest'] == {'approved': {'good.png': sha(good)}, 'deferred': ['bad.png']}
            assert route._job['state'] == 'completed_with_omissions'
        assert route._job['run_id'] == run_id
        # Lost process state cannot replay extraction across the promotion barrier.
        route._job.clear()
        assert client.post('/api/online-ocr/session/continue-incomplete', json=body).status_code == 409
        # Continuation checks exact landed bytes and performs no cache writes.
        before = {p: p.read_bytes() for p in (tmp_path / 'cache').rglob('*.txt')}
        if cancel_continuation:
            scope_check = route._check_continuation_scope
            calls = []
            monkeypatch.setattr(manager, 'cancel_indexing', lambda: True)
            def cancel_before_begin(*args):
                scope_check(*args)
                calls.append(1)
                if len(calls) == 2:  # Worker check, after admission and before begin_index_run.
                    client.post('/api/online-ocr/cancel')
            monkeypatch.setattr(route, '_check_continuation_scope', cancel_before_begin)
        response = client.post('/api/online-ocr/session/continue-index', json=dict(run_id=run_id, confirm_index=True))
        assert response.status_code == (409 if prepare_only else 200), response.json
        for thread in threads:
            thread.join(3)
        assert len(indexed) == (0 if prepare_only else 1 if cancel_continuation else 2)
        assert all(p.read_bytes() == value for p, value in before.items())
        next(iter(before)).write_text('changed cache')
        assert client.post('/api/online-ocr/session/continue-index', json=dict(run_id=run_id, confirm_index=True)).status_code == 409
        assert len(indexed) == (0 if prepare_only else 1 if cancel_continuation else 2)
    finally:
        for thread in threads:
            thread.join(3)
        route._job.clear()
        route._job.update(old)


def test_bounded_long_pdf_preview(tmp_path, monkeypatch):
    import fitz
    from api.routes import online_ocr as route
    source = tmp_path / 'long.pdf'
    with fitz.open() as doc:
        for _ in range(1001):
            doc.new_page()
        doc.save(source)
    monkeypatch.setattr(route, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(route, 'OBSIDIAN_INDEX_DIR', str(tmp_path / 'index'))
    result = route._preview(tmp_path, source, 'long.pdf', page_start=1000, page_end=1001)
    assert result['page_range'] == [1000, 1001]
    assert result['eligible'] == 0
    with pytest.raises(ValueError, match='1000 pages'):
        route._preview(tmp_path, source, 'long.pdf')


def test_file_run_retry_retains_generation_and_rejects_drift(tmp_path):
    from core.online_runs import create_file_run, file_outcome, save_file_outcome
    record = {'schema': 2, 'source': 'paper.pdf', 'attempt_generation': 'c' * 32,
              'source_sha256': 'd' * 64, 'page_range': [1, 2]}
    directory, generation = create_file_run(tmp_path, record)
    save_file_outcome(directory, {'source': 'paper.pdf', 'status': 'partial'})
    resumed, resumed_generation = create_file_run(tmp_path, {**record, 'attempt_generation': 'e' * 32}, directory.name)
    assert resumed == directory and generation == resumed_generation
    save_file_outcome(directory, {'source': 'paper.pdf', 'status': 'processed_unverified'})
    assert file_outcome(directory)['status'] == 'processed_unverified'
    with pytest.raises(ValueError, match='changed'):
        create_file_run(tmp_path, {**record, 'page_range': [2, 3]}, directory.name)
    (directory / 'promotion-started.json').write_text('{}')
    with pytest.raises(ValueError, match='barrier'):
        create_file_run(tmp_path, record, directory.name)


def test_missing_approved_cache_never_falls_back_to_parsing(tmp_path, monkeypatch):
    import rag.vault as module
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'approved.pdf'
    source.write_bytes(b'not a PDF; parsing must not happen')
    manager = module.ObsidianVaultManager()
    manager.restore_vault_path(str(vault))
    monkeypatch.setattr(module, 'load_config', lambda: {'vault_image_exts': []})
    monkeypatch.setattr(module, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(module, 'OBSIDIAN_INDEX_DIR', str(tmp_path / 'index'))
    monkeypatch.setattr(module, 'get_pdf_page_count', lambda *a: pytest.fail('Unapproved parsing'))
    docs = list(manager._load_vault_documents(str(vault), online_extraction=True,
                extraction_manifest={'approved': {'approved.pdf': sha(source)}}))
    assert docs == []
    assert manager._scan_failed_sources == {'approved.pdf'}


@pytest.mark.parametrize("session", [False, True])
def test_extraction_survives_process_state_loss(tmp_path, monkeypatch, session):
    from flask import Flask
    from api.routes import online_ocr as route
    from PIL import Image
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'one.png'
    Image.new('RGB', (20, 20), 'white').save(source)
    (vault / 'note.md').write_text('![[one.png]]')
    base = tmp_path / 'base'
    cfg = {'obsidian_vault_path': str(vault), 'online_ocr_provider': 'deepseek', 'online_ocr_model': 'deepseek-flash'}
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fake-only')
    monkeypatch.setattr(route, 'BASE_DIR', str(base))
    monkeypatch.setattr(route, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(route, 'OBSIDIAN_INDEX_DIR', str(tmp_path / 'index'))
    monkeypatch.setattr(route, 'load_config', lambda: cfg)
    monkeypatch.setattr(route, '_preflight_index_settings', lambda *a, **kw: None)
    manager = route.obsidian_manager
    for name in ('begin_index_run', 'release_lock'):
        monkeypatch.setattr(manager, name, lambda *a: None)
    monkeypatch.setattr(manager, 'get_status', lambda: 'done')
    monkeypatch.setattr(manager, 'wait_for_indexing', lambda **kw: True)
    monkeypatch.setattr(manager, 'try_acquire_lock', lambda **kw: 1)
    monkeypatch.setattr(manager, 'heartbeat', lambda epoch: True)
    monkeypatch.setattr(manager, '_scan_failed_sources', set())
    threads, indexed = [], []
    monkeypatch.setattr(manager, 'register_index_thread', threads.append)
    monkeypatch.setattr(manager, 'index_vault', lambda *a, **kw: indexed.append(kw))
    monkeypatch.setattr(route, 'extract_image', lambda *a, **kw: {
        'status': 'ocr', 'coverage': 'processed_unverified', 'binding': {'recipe': 'fake'}, 'attempt': 1})
    monkeypatch.setattr(route, 'export_image', lambda *a: base / 'online_ocr_exports' / 'fake.md')
    monkeypatch.setattr(route, '_promotable_text', lambda *a: 'fixture description')
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    client = app.test_client()
    previous = dict(route._job)
    route._job.clear()
    try:
        body = dict(path='one.png', provider='deepseek', model='deepseek-flash', reextract=False)
        prefix = '/api/online-ocr/session' if session else '/api/online-ocr'
        preview = client.post(prefix + '/preview', json=body).json
        response = client.post(prefix + '/start', json={**body, 'confirm_external': True,
            'session_token': preview.get('session_token'),
            'source_sha256': preview.get('source_sha256'), 'preview_token': preview.get('preview_token')})
        assert response.status_code == 200, response.json
        threads[-1].join(3)
        assert not threads[-1].is_alive()
        route._job.clear()
        history = client.get('/api/online-ocr/runs').json['runs']
        assert history[0]['kind'] == ('extraction_session' if session else 'file') and history[0]['state'] == 'done'
        response = client.post(prefix + '/promote', json=dict(path='one.png', run_id=history[0]['run_id'], confirm_index=True))
        assert response.status_code == 200, response.json
        threads[-1].join(3)
        assert not threads[-1].is_alive()
        assert len(indexed) == 1
        assert indexed[0]['extraction_manifest']['approved'] == {'one.png': sha(source)}
        assert route._job['state'] == 'indexed'
    finally:
        for thread in threads:
            thread.join(3)
        route._job.clear()
        route._job.update(previous)


def test_unavailable_local_backend_refuses_before_opening_store(tmp_path, monkeypatch):
    import rag.vault as module
    manager = module.ObsidianVaultManager()
    monkeypatch.setattr(manager, "_vault_path", str(tmp_path))
    provider = Mock()
    provider.get_models.return_value = ([], 'offline')
    monkeypatch.setattr(module, 'get_provider', lambda _: provider)
    open_store = Mock(side_effect=AssertionError('Store must not open'))
    monkeypatch.setattr(manager, '_index_open_store', open_store)
    manager.index_vault('llm', 'embed')
    assert manager.get_status() == 'error'
    assert manager.get_status_payload()['error_code'] == 'embedding_unavailable'
    provider.get_embedding.assert_not_called()
    open_store.assert_not_called()


def test_legacy_history_does_not_infer_interruption_or_success(tmp_path):
    from core.online_runs import inspect_run, checkpoint
    directory = tmp_path / 'online_index_runs' / ('a' * 32)
    directory.mkdir(parents=True)
    (directory / 'run.json').write_text(json.dumps({'schema': 1, 'vault_key': 'v', 'source_count': 2}))
    (directory / 'promotion-started.json').write_text('{}')
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    result = inspect_run(tmp_path, 'a' * 32, 'v', limit=0)
    assert result['state'] == 'status_unrecorded'
    assert 'current search index' in result['status_note']
    assert not result['retry_allowed']
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    checkpoint(directory, 'completed_with_omissions', deferred_count=1)
    result = inspect_run(tmp_path, 'a' * 32, 'v', limit=0)
    assert result['state'] == 'completed_with_omissions'
    assert result['status_note'] is None


def test_complete_legacy_cache_wins_over_partial_current_ranges(tmp_path):
    from core.extraction_cache import cache_coverage
    current, legacy = tmp_path / 'current', tmp_path / 'legacy'
    for root in (current, legacy):
        (root / 'pdf_cache' / 'vault').mkdir(parents=True)
    (current / 'pdf_cache/vault/hash-p00000-00050.txt').write_text('partial')
    (legacy / 'pdf_cache/vault/hash.txt').write_text('complete retained text')
    def forbidden():
        raise AssertionError('Whole-file reuse must not parse the PDF')
    assert cache_coverage([current, legacy], 'pdf_cache', 'vault', 'hash',
                          page_count=forbidden) == 'whole_unverified'
