"""Isolated regressions for the seven UI/index review findings."""
import json

import pytest


def test_selection_keeps_cached_sources_unless_reextraction_requested(tmp_path, monkeypatch):
    from api.routes import online_ocr as r
    from PIL import Image
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'cached.png'
    Image.new('RGB', (2, 2)).save(source)
    (vault / 'note.md').write_text('![[cached.png]]')
    monkeypatch.setattr(r, 'load_config', lambda: {'obsidian_vault_path': str(vault), 'vault_image_exts': ['.png']})
    monkeypatch.setattr(r, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(r, 'OBSIDIAN_INDEX_DIR', str(tmp_path / 'index'))
    cache = r._promotion_cache(vault, source, r._digest(source))
    cache.parent.mkdir(parents=True)
    cache.write_text('approved text')
    routine = r._session_inventory(vault, large_run=True, routine=True, paths=['cached.png'])
    assert routine['source_count'] == routine['eligible'] == 0
    assert routine['reused_count'] == 1
    explicit = r._session_inventory(vault, large_run=True, routine=False, paths=['cached.png'])
    assert explicit['source_count'] == explicit['eligible'] == 1


@pytest.mark.parametrize('fragmented', [False, True])
def test_short_range_caches_never_fall_back_to_extraction(tmp_path, monkeypatch, fragmented):
    import rag.vault as m
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'one.pdf'
    source.write_bytes(b'parser must never read this')
    manager = m.ObsidianVaultManager()
    manager.restore_vault_path(str(vault))
    monkeypatch.setattr(m, 'load_config', lambda: {'vault_image_exts': []})
    monkeypatch.setattr(m, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(m, 'OBSIDIAN_INDEX_DIR', str(tmp_path / 'index'))
    signature = manager._pdf_file_signature(source)
    for start, end in ([(0, 1), (1, 2)] if fragmented else [(0, 2)]):
        cache = manager._pdf_range_cache_file(vault, signature, start, end)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text('approved range text')
    monkeypatch.setattr(m, 'get_pdf_page_count', lambda _: 2)
    monkeypatch.setattr(m, 'extract_structured_from_pdf', lambda *a, **kw: pytest.fail('Fresh parsing'))
    monkeypatch.setattr(manager, '_export_readable_pdf', lambda *a, **kw: None)
    docs = list(manager._load_vault_documents(str(vault), online_extraction=True,
                extraction_manifest={'approved': {'one.pdf': signature['sha256']}}))
    assert not manager._pdf_cache_file(vault, signature).exists()
    if fragmented:
        assert docs == [] and manager._scan_failed_sources == {'one.pdf'}
    else:
        assert [doc.text for doc in docs] == ['approved range text']


@pytest.mark.parametrize('drift', ['none', 'exclusions', 'extensions', 'arrival', 'under_lock', 'legacy'])
def test_continuation_rechecks_scope_and_records_loader_omissions(tmp_path, monkeypatch, drift):
    from flask import Flask
    from api.routes import online_ocr as r
    import rag.vault as m
    from core.online_runs import inspect_run
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'one.png'
    source.write_bytes(b'cached image disappeared')
    (vault / 'note.md').write_text('![[one.png]]')
    base = tmp_path / 'base'
    directory = base / 'online_index_runs' / ('a' * 32)
    directory.mkdir(parents=True)
    promotion = base / 'online_ocr_promotions' / ('b' * 32)
    promotion.mkdir(parents=True)
    cfg = {'obsidian_vault_path': str(vault), 'vault_image_exts': ['.png']}
    monkeypatch.setattr(r, 'BASE_DIR', str(base))
    monkeypatch.setattr(r, 'load_config', lambda: cfg)
    monkeypatch.setattr(m, 'load_config', lambda: cfg)
    monkeypatch.setattr(m, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(m, 'OBSIDIAN_INDEX_DIR', str(tmp_path / 'index'))
    settings = r._index_settings(cfg)
    run = {'vault_key': r.vault_key(vault), 'index_settings': settings, 'source_count': 1, 'full_rebuild': True}
    (directory / 'run.json').write_text(json.dumps(run))
    (directory / 'promotion-started.json').write_text('{}')
    evidence = {'promotion_id': 'b' * 32, 'manifest': {'approved': {'one.png': r._digest(source)}},
                'scope_binding': r._continuation_scope(cfg)}
    if drift == 'legacy':
        evidence.pop('scope_binding')
    (directory / 'index-input.json').write_text(json.dumps(evidence))
    # A reused-cache-only run has no promoted replacements.
    (promotion / 'receipt.json').write_text(json.dumps({'sources': []}))
    if drift == 'exclusions':
        cfg['vault_exclude_dirs'] = ['excluded']
    elif drift == 'extensions':
        cfg['vault_image_exts'] = []
    elif drift == 'arrival':
        (vault / 'arrival.pdf').write_bytes(b'unapproved arrival')
    monkeypatch.setattr(r, '_preflight_index_settings', lambda *a, **kw: None)
    manager = m.ObsidianVaultManager()
    manager.restore_vault_path(str(vault))
    monkeypatch.setattr(r, "obsidian_manager", manager)
    threads, calls = [], []
    monkeypatch.setattr(manager, 'wait_for_indexing', lambda **kw: True)
    def acquire(**kw):
        if drift == 'under_lock':
            cfg['vault_exclude_dirs'] = ['changed-after-validation']
        return 1
    monkeypatch.setattr(manager, 'try_acquire_lock', acquire)
    # This scope-binding fixture fakes acquisition; lease takeover is exercised
    # with the real lock in test_promotion_stop_boundaries_preserve_receipts_and_do_not_index.
    monkeypatch.setattr(manager, 'heartbeat', lambda *a: True)
    monkeypatch.setattr(manager, 'release_lock', lambda *a: None)
    monkeypatch.setattr(manager, 'begin_index_run', lambda *a: None)
    monkeypatch.setattr(manager, 'register_index_thread', threads.append)
    monkeypatch.setattr(manager, '_scan_failed_sources', set())
    def index(*a, **kw):
        calls.append(kw)
        list(manager._load_vault_documents(str(vault), online_extraction=True,
                                          extraction_manifest=kw['extraction_manifest']))
    monkeypatch.setattr(manager, 'index_vault', index)
    monkeypatch.setattr(manager, 'get_status', lambda: 'done')
    old = dict(r._job)
    r._job.clear()
    try:
        app = Flask(__name__)
        app.register_blueprint(r.online_ocr_bp)
        response = app.test_client().post('/api/online-ocr/session/continue-index',
            json={'run_id': 'a' * 32, 'confirm_index': True})
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()
        if drift not in ('none', 'under_lock'):
            assert response.status_code == 409 and not calls
        elif drift == 'under_lock':
            assert response.status_code == 200 and not calls
            assert r._job['state'] == 'index_incomplete'
        else:
            assert response.status_code == 200 and len(calls) == 1
            assert manager._scan_failed_sources == {'one.png'}
            summary = inspect_run(base, 'a' * 32, r.vault_key(vault))
            assert summary['state'] == 'completed_with_omissions'
            assert summary['deferred_count'] == 1
            assert summary['deferred_content'] == 'omitted_from_new_index'
    finally:
        for thread in threads:
            thread.join(3)
        r._job.clear()
        r._job.update(old)


def test_status_payload_is_bounded_and_preserves_promotion_counts(monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as r
    old = dict(r._job)
    r._job.clear()
    try:
        monkeypatch.setattr(r.obsidian_manager, 'get_status', lambda: 'idle')
        monkeypatch.setattr(r.obsidian_manager, 'get_progress_snapshot', lambda: {})
        r._job.update(state='done', scope='vault_session', source_count=10000,
            extraction_manifest={'approved': {f'{i}.pdf': 'a' * 64 for i in range(10000)}},
            promoted_sources=['one.pdf'] * 10000, expected_sources=['one.pdf'] * 10000,
            results=[{'source': 'one.pdf', 'status': 'processed_unverified', 'snapshots': ['large'] * 1000}] * 10000)
        app = Flask(__name__)
        app.register_blueprint(r.online_ocr_bp)
        response = app.test_client().get('/api/online-ocr/status')
        assert len(response.data) < 2000
        assert response.json['completed_count'] == response.json['promoted_count'] == 10000
        assert len(response.json['recent_results']) == 5
        assert 'extraction_manifest' not in response.json
    finally:
        r._job.clear()
        r._job.update(old)


def test_renamed_pdf_reuses_text_with_current_search_source(tmp_path, monkeypatch):
    import rag.vault as m
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'old.pdf'
    source.write_bytes(b'unchanged PDF bytes; extraction must not run')
    manager = m.ObsidianVaultManager()
    manager.restore_vault_path(str(vault))
    monkeypatch.setattr(m, 'load_config', lambda: {'vault_image_exts': []})
    monkeypatch.setattr(m, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(m, 'OBSIDIAN_INDEX_DIR', str(tmp_path / 'index'))
    monkeypatch.setattr(m, 'extract_structured_from_pdf', lambda *a, **kw: pytest.fail('Fresh extraction'))
    monkeypatch.setattr(manager, '_export_readable_pdf', lambda *a, **kw: None)
    signature = manager._pdf_file_signature(source)
    cache = manager._pdf_cache_file(vault, signature)
    cache.parent.mkdir(parents=True)
    cache.write_text('Previously extracted text')
    source.rename(vault / 'renamed.pdf')
    docs = list(manager._load_vault_documents(str(vault), online_extraction=True,
                extraction_manifest={'approved': {'renamed.pdf': signature['sha256']}}))
    assert [(doc.metadata['source'], doc.text) for doc in docs] == [
        ('renamed.pdf', 'Previously extracted text')]
    assert not manager._scan_failed_sources
