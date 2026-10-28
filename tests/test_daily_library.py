"""Daily extraction policy using disposable sources and denied provider seams."""
import hashlib
import json

import pytest


@pytest.mark.parametrize('level,expected', [(1, 'ready'), (2, 'warning'), (4, 'blocked'), (7, 'blocked')])
def test_memory_check_is_observational(tmp_path, monkeypatch, level, expected):
    from flask import Flask
    from types import SimpleNamespace
    from api.routes import online_ocr as route
    from core import ocr_resources
    def forbidden(*args, **kwargs):
        pytest.fail('Memory check must not load library settings or start work')
    monkeypatch.setattr(route, 'load_config', forbidden)
    monkeypatch.setattr(route, '_session_inventory', forbidden)
    monkeypatch.setattr(route.obsidian_manager, 'index_vault', forbidden)
    monkeypatch.setattr(ocr_resources.sys, 'platform', 'darwin')
    monkeypatch.setattr(ocr_resources.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        stdout=f'vm.swapusage: used = 6400.00M\nkern.memorystatus_vm_pressure_level: {level}'))
    before = dict(route._job)
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    response = app.test_client().get('/api/online-ocr/resources')
    assert response.status_code == 200
    assert response.json['state'] == expected
    assert response.json['stop_reason'] == ('memory_monitor_unavailable' if level == 7
                                           else 'initial_memory_pressure' if level == 4 else '')
    assert route._job == before
    assert list(tmp_path.iterdir()) == []


def test_failure_evidence_survives_rename_and_model_change(tmp_path):
    from core.online_runs import known_failed_content
    directory = tmp_path / 'online_index_runs' / ('a' * 32)
    directory.mkdir(parents=True)
    digest = hashlib.sha256(b'unchanged').hexdigest()
    (directory / 'run.json').write_text(json.dumps({'vault_key': 'vault', 'ocr': {'model': 'old'}}))
    (directory / 'inventory.json').write_text(json.dumps({'sources': [
        {'source': 'old.pdf', 'source_sha256': digest}]}))
    # Legacy failure with identity supplied by the immutable inventory.
    (directory / 'source-000000.json').write_text(json.dumps({'source': 'old.pdf', 'status': 'failed'}))
    found = known_failed_content(tmp_path, 'vault')
    assert found[digest]['reason'] == 'failed'
    assert hashlib.sha256(b'changed').hexdigest() not in found
    assert known_failed_content(tmp_path, 'other-vault') == {}
    (directory / 'source-000001.json').write_text(json.dumps({'source': 'unknown.pdf', 'status': 'failed'}))
    assert len(known_failed_content(tmp_path, 'vault')) == 1


@pytest.mark.parametrize('policy', ['known', 'offline', 'retry', 'native', 'prepare', 'explicit_prepare', 'explicit_update', 'source_failure', 'memory', 'batch_memory', 'provider_stop'])
def test_daily_worker_policy(tmp_path, monkeypatch, policy):
    import fitz
    from flask import Flask
    from api.routes import online_ocr as route
    from core.online_ocr import OnlineOCR

    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'article.pdf'
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), 'Native scientific text. ' * 8)
    doc.save(source)
    doc.close()
    base = tmp_path / 'base'
    monkeypatch.setattr(route, 'BASE_DIR', str(base))
    monkeypatch.setattr(route, 'load_config', lambda: {
        'obsidian_vault_path': str(vault), 'provider': 'ollama', 'embed': 'unchanged-model',
        'online_ocr_provider': 'deepseek', 'online_ocr_model': 'deepseek-flash'})
    monkeypatch.setattr(route, '_preflight_index_settings', lambda *a, **kw: None)
    row = {'source': source.name, 'source_sha256': route._digest(source), 'kind': 'pdf',
           'pages': 1, 'eligible': int(policy in ('known', 'offline', 'retry')), 'has_cached_text': False}
    if policy in ('known', 'retry'):
        row['known_failure'] = {'reason': 'failed', 'run_id': 'old'}
    inventory = {'sources': [row], 'source_count': 1, 'eligible': row['eligible'], 'session_token': 'bound'}
    monkeypatch.setattr(route, '_session_inventory', lambda *a, **kw: inventory)
    if policy == 'source_failure':
        bad = vault / 'bad.pdf'
        bad.write_bytes(b'not a PDF')
        inventory['sources'].append({'source': 'bad.pdf', 'source_sha256': route._digest(bad),
            'kind': 'pdf', 'pages': 0, 'eligible': 0, 'has_cached_text': False,
            'preview_error': 'Unreadable PDF'})
        inventory['source_count'] = 2
    if policy == 'provider_stop':
        monkeypatch.setattr(route, 'extract_pages', lambda *a, **kw: {
            'coverage': 'partial', 'range': [1, 1], 'snapshot_id': 'c' * 64,
            'payload': {'recipe': 'fixture'}, 'stop_reason': 'engine_unavailable',
            'pages': [{'status': 'engine_error', 'failure_scope': 'global',
                       'http_status': 401, 'error_detail': 'Fixture authentication rejected',
                       'next_step': 'Check provider credentials'}]})
        monkeypatch.setattr(route, 'export_pages', lambda *a: base / 'fixture.json')
    from core import ocr_resources
    guards = []
    def guard_factory(**kw):
        guards.append(object())
        return lambda: 'initial_memory_pressure' if policy == 'memory' else ''
    monkeypatch.setattr(ocr_resources, 'MemoryGuard', guard_factory)
    if policy == 'batch_memory':
        second = vault / 'second.pdf'
        second.write_bytes(source.read_bytes())
        inventory['sources'].append({**row, 'source': second.name})
        inventory['source_count'] = 2
    manager = route.obsidian_manager
    monkeypatch.setattr(manager, 'get_status', lambda: 'idle')
    monkeypatch.setattr(manager, 'try_acquire_lock', lambda **kw: 1)
    monkeypatch.setattr(manager, 'heartbeat', lambda epoch: True)
    monkeypatch.setattr(manager, 'release_lock', lambda *a: None)
    monkeypatch.setattr(manager, '_heartbeat_or_stop', lambda *a: None)
    monkeypatch.setattr(manager, '_selected_pdf_payload', lambda *a: None)
    threads = []
    monkeypatch.setattr(manager, 'register_index_thread', threads.append)
    def forbidden(*a, **kw):
        raise AssertionError('Provider access forbidden')
    monkeypatch.setattr(OnlineOCR, 'identity', forbidden)
    promoted = []
    original_promote = route._promote_session_selected
    def promote(selected, *a, **kw):
        promoted.append((selected, kw, dict(route._job['extraction_manifest'])))
        if policy in ('prepare', 'explicit_prepare', 'explicit_update'):
            original_promote(selected, *a, **kw)
    monkeypatch.setattr(route, '_promote_session_selected', promote)
    monkeypatch.setattr(route, '_continuation_scope', lambda cfg: 'isolated-scope')
    monkeypatch.setattr(route, '_promotion_cache', lambda *a: base / 'prepared.txt')
    index_calls = []
    if policy == 'explicit_update':
        monkeypatch.setattr(manager, 'begin_index_run', lambda epoch: None)
        monkeypatch.setattr(manager, 'index_vault', lambda *a, **kw: index_calls.append((a, kw)))
        monkeypatch.setattr(route, '_index_outcome', lambda *a, **kw: {'state': 'indexed'})
    else:
        monkeypatch.setattr(manager, 'index_vault', forbidden)
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    before = dict(route._job)
    route._job.clear()
    route._job['state'] = 'idle'
    try:
        body = {'run_index': True, 'confirm_index': True, 'session_token': 'bound',
                'no_external': policy == 'offline', 'confirm_external': policy != 'offline',
                'retry_failed': policy in ('retry', 'prepare'), 'prepare_only': policy in ('prepare', 'explicit_prepare')}
        if policy == 'explicit_prepare':
            body.update(operation_mode='prepare_text', confirm_index=False,
                        session_token=route._intent_token('bound', 'prepare_text'))
        if policy == 'explicit_update':
            body.update(operation_mode='update_search', confirm_index=True,
                        session_token=route._intent_token('bound', 'update_search'))
        if policy == 'retry':
            # Explicit retry reaches the provider seam; never a real provider.
            with pytest.raises(AssertionError, match='Provider access'):
                with app.test_request_context(json=body):
                    route.session_start()
            return
        response = app.test_client().post('/api/online-ocr/session/start', json=body)
        assert response.status_code == 200, response.json
        threads[0].join(5)
        assert not threads[0].is_alive()
        if policy == 'provider_stop':
            assert not promoted
            status = app.test_client().get('/api/online-ocr/status').json
            assert status['state'] == 'extraction_incomplete'
            assert status['failure']['http_status'] == 401
            assert status['failure']['error_detail'] == 'Fixture authentication rejected'
            assert status['failure']['stop_reason'] == 'engine_unavailable'
            return
        if policy == 'memory':
            assert not promoted
            assert route._job['state'] == 'extraction_incomplete'
            status = app.test_client().get('/api/online-ocr/status').json
            assert status['failure']['stop_reason'] == 'initial_memory_pressure'
            assert status['failure']['failure_scope'] == 'global'
            assert 'memory pressure' in status['failure']['error_detail']
            receipt = base / 'online_index_runs' / response.json['run_id'] / 'source-000000.json'
            assert json.loads(receipt.read_text())['stop_reason'] == 'initial_memory_pressure'
            assert not (receipt.parent / 'promotion-started.json').exists()
            return
        assert len(promoted) == 1, json.dumps(route._job, default=str)
        selected, options, manifest = promoted[0]
        if policy in ('known', 'offline'):
            assert selected == []
            assert manifest == {'approved': {}, 'deferred': ['article.pdf']}
        else:
            assert len(selected) == (2 if policy == 'batch_memory' else 1)
            assert 'Native scientific text' in selected[0][-1]
            assert manifest['deferred'] == (['bad.pdf'] if policy == 'source_failure' else [])
        if policy == 'explicit_update':
            assert route._job['state'] == 'indexed'
            assert len(index_calls) == 1
            assert index_calls[0][0][1] == 'unchanged-model'
            assert index_calls[0][1]['force_fresh'] is False
            assert index_calls[0][1]['extraction_manifest'] == manifest
            receipt = json.loads((base / 'online_index_runs' / response.json['run_id'] / 'run.json').read_text())
            assert receipt['operation_mode'] == 'update_search'
            assert receipt['initiating_action'] == 'search_update'
            assert receipt['application_version']
        assert len(guards) == 1
        assert options['prepare_only'] == (policy in ('prepare', 'explicit_prepare'))
        if policy in ('prepare', 'explicit_prepare'):
            assert route._job['state'] == 'text_prepared'
            assert 'Native scientific text' in (base / 'prepared.txt').read_text()
            receipt = base / 'online_index_runs' / response.json['run_id'] / 'source-000000.json'
            assert json.loads(receipt.read_text())['retry_authorized'] is (policy == 'prepare')
    finally:
        for thread in threads:
            thread.join(5)
        route._job.clear()
        route._job.update(before)


def test_inventory_hash_decision_cache_precedence_and_changed_bytes(tmp_path, monkeypatch):
    import fitz
    from api.routes import online_ocr as route
    from rag.storage_io import vault_key
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'renamed.pdf'
    document = fitz.open()
    document.new_page().insert_text((72, 72), 'Text on the renamed document')
    document.save(source)
    document.close()
    digest = route._digest(source)
    base = tmp_path / 'base'
    run = base / 'online_index_runs' / ('c' * 32)
    run.mkdir(parents=True)
    (run / 'run.json').write_text(json.dumps({'vault_key': vault_key(vault)}))
    (run / 'source-000000.json').write_text(json.dumps({
        'source': 'original.pdf', 'source_sha256': digest, 'status': 'failed'}))
    monkeypatch.setattr(route, 'BASE_DIR', str(base))
    monkeypatch.setattr(route, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(route, 'OBSIDIAN_INDEX_DIR', str(tmp_path / 'index'))
    monkeypatch.setattr(route, 'load_config', lambda: {})
    monkeypatch.setattr(route, '_discover_sources', lambda _: [(source, source.name)])
    monkeypatch.setattr(route.obsidian_manager, '_load_pdf_signature_cache', lambda: {})
    monkeypatch.setattr(route.obsidian_manager, '_pdf_file_signature',
                        lambda *a: {'sha256': route._digest(source), 'size': source.stat().st_size, 'mtime_ns': 1})
    monkeypatch.setattr(route.obsidian_manager, '_selected_pdf_payload', lambda *a: None)
    def inventory():
        return route._session_inventory(vault, routine=True, large_run=True)
    assert inventory()['pending_count'] == 1
    cache = tmp_path / 'cache' / 'pdf_cache' / vault_key(vault) / (digest + '.txt')
    cache.parent.mkdir(parents=True)
    cache.write_text('Successful complete cached text')
    assert inventory()['reused_count'] == 1
    assert inventory()['pending_count'] == 0
    # Different bytes, same path: the old decision cannot authorize suppression.
    source.write_bytes(source.read_bytes() + b'\n% changed\n')
    assert inventory()['pending_count'] == 0
    assert inventory()['source_count'] == 1


def test_document_identity_and_content_continuity_are_distinct():
    from core.online_runs import failure_for
    evidence = [{'source': 'before.pdf', 'source_sha256': 'old', 'reason': 'reported_bad',
                 'document_identity': {'ino': 12, 'birth': 4}}]
    assert failure_for(evidence, 'renamed.pdf', 'old', {'ino': 12, 'birth': 4})['reason'] == 'reported_bad'
    assert failure_for(evidence, 'renamed.pdf', 'new', {'ino': 12, 'birth': 4})['reason'] == 'content_continuity_uncertain'
    assert failure_for(evidence, 'before.pdf', 'new', {'ino': 13, 'birth': 5})['reason'] == 'content_continuity_uncertain'
    assert failure_for(evidence, 'unrelated.pdf', 'new', {'ino': 13, 'birth': 5}) is None


def test_only_source_local_failures_may_be_deferred():
    from core.online_runs import source_exception
    assert source_exception({'status': 'partial', 'page_statuses': ['native', 'output_limit']})
    for states in [['native', 'runtime_changed'], ['memory_limit'], ['cancelled'], []]:
        assert not source_exception({'status': 'partial', 'page_statuses': states})
    assert not source_exception({'status': 'failed', 'error': 'PermissionError'})


def test_manual_report_is_hash_bound_and_preserves_data(tmp_path, monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route
    from core.online_runs import known_failed_content
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'paper.pdf'
    source.write_bytes(b'unchanged source')
    monkeypatch.setattr(route, 'BASE_DIR', str(tmp_path / 'base'))
    monkeypatch.setattr(route, 'load_config', lambda: {'obsidian_vault_path': str(vault)})
    monkeypatch.setattr(route.obsidian_manager, 'try_acquire_lock', lambda **kw: 1)
    monkeypatch.setattr(route.obsidian_manager, 'release_lock', lambda *a: None)
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    client = app.test_client()
    body = {'path': 'paper.pdf', 'source_sha256': route._digest(source), 'confirm': True}
    assert client.post('/api/online-ocr/report-bad', json={**body, 'confirm': False}).status_code == 400
    assert client.post('/api/online-ocr/report-bad', json={**body, 'source_sha256': 'stale'}).status_code == 409
    assert client.post('/api/online-ocr/report-bad', json=body).status_code == 200
    source.rename(vault / 'renamed.pdf')
    found = known_failed_content(tmp_path / 'base', route.vault_key(vault))
    assert found[body['source_sha256']]['reason'] == 'reported_bad'
    assert (vault / 'renamed.pdf').read_bytes() == b'unchanged source'


def test_visual_enrichment_preserves_native_and_reuses_receipts(tmp_path):
    import io
    import fitz
    from PIL import Image
    from core.page_ocr import extract_pages
    from core.online_ocr import NativeOnlyOCR
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'figure.pdf'
    png = io.BytesIO()
    Image.new('RGB', (200, 200), 'white').save(png, format='PNG')
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((30, 30), 'Native text remains available and must never be silently discarded.')
    page.insert_image(fitz.Rect(30, 100, 450, 600), stream=png.getvalue())
    doc.save(source)
    doc.close()
    class Visual:
        calls = 0
        def identity(self):
            return {'engine': 'fixture-vision'}
        def recognize(self, *args):
            self.calls += 1
            return {'status': 'ocr', 'text': 'Figure: two measured groups.', 'calls': 1, 'call_s': 0}
    visual = Visual()
    kwargs = dict(accept_mixed_native=True, visual_engine=visual, resource_guard=lambda: '')
    result = extract_pages(source, vault, tmp_path / 'receipts', NativeOnlyOCR(), **kwargs)
    assert result['coverage'] == 'processed_unverified'
    assert 'Native text remains' in result['pages'][0]['text']
    assert 'Figure: two measured groups.' in result['pages'][0]['text']
    extract_pages(source, vault, tmp_path / 'receipts', NativeOnlyOCR(), **kwargs)
    assert visual.calls == 1
    offline = extract_pages(source, vault, tmp_path / 'offline', NativeOnlyOCR(),
                            accept_mixed_native=True, resource_guard=lambda: '')
    assert 'Native text remains' in offline['pages'][0]['text']
    assert visual.calls == 1


def test_failed_promotion_does_not_clear_user_report(tmp_path):
    from core.online_runs import known_failed_content, checkpoint
    root = tmp_path / 'online_index_runs'
    bad = root / ('a' * 32)
    retry = root / ('b' * 32)
    for path in (bad, retry):
        path.mkdir(parents=True)
        (path / 'run.json').write_text(json.dumps({'vault_key': 'v', 'retry_failed': path == retry}))
    digest = 'c' * 64
    (bad / 'source-000000.json').write_text(json.dumps({'source': 'old.pdf',
        'source_sha256': digest, 'status': 'reported_bad', 'recorded_at_ns': 1}))
    (retry / 'source-000000.json').write_text(json.dumps({'source': 'renamed.pdf',
        'source_sha256': digest, 'status': 'processed_unverified', 'recorded_at_ns': 2}))
    checkpoint(retry, 'failed')
    assert digest in known_failed_content(tmp_path, 'v')
    checkpoint(retry, 'text_prepared')
    assert digest not in known_failed_content(tmp_path, 'v')


def test_global_failure_never_becomes_automatic_deferral(tmp_path):
    from core.online_runs import known_failed_content
    directory = tmp_path / 'online_index_runs' / ('a' * 32)
    directory.mkdir(parents=True)
    (directory / 'run.json').write_text(json.dumps({'vault_key': 'v'}))
    (directory / 'source-000000.json').write_text(json.dumps({'source': 'paper.pdf',
        'source_sha256': 'b' * 64, 'status': 'partial', 'failure_scope': 'global'}))
    assert known_failed_content(tmp_path, 'v') == {}


def test_resumed_daily_retry_clears_report_only_after_successful_promotion(tmp_path):
    from core.online_runs import known_failed_content, checkpoint
    directory = tmp_path / 'online_index_runs' / ('a' * 32)
    directory.mkdir(parents=True)
    # The immutable original run did not authorize retries. Its explicit resume did.
    (directory / 'run.json').write_text(json.dumps({'vault_key': 'v', 'retry_failed': False}))
    digest = 'b' * 64
    (directory / 'source-000000.json').write_text(json.dumps({
        'source': 'paper.pdf', 'source_sha256': digest, 'status': 'reported_bad',
        'recorded_at_ns': 1}))
    (directory / 'source-000000-r001.json').write_text(json.dumps({
        'source': 'paper.pdf', 'source_sha256': digest, 'status': 'processed_unverified',
        'retry_authorized': True, 'recorded_at_ns': 2}))
    checkpoint(directory, 'failed')
    assert digest in known_failed_content(tmp_path, 'v')
    checkpoint(directory, 'text_prepared')
    assert digest not in known_failed_content(tmp_path, 'v')


def test_rejected_pdf_image_is_local_but_explicit_global_scope_wins():
    from core.online_runs import source_exception
    assert source_exception({'status':'partial', 'page_statuses':['native','image_rejected']})
    assert not source_exception({'status':'image_rejected','failure_scope':'global'})
    assert not source_exception({'status':'partial','failure_scope':'global','page_statuses':['native','image_rejected']})


@pytest.mark.parametrize('mode', ['prepare_text', 'update_search'])
def test_daily_intent_preview_binding(tmp_path, monkeypatch, mode):
    from flask import Flask
    from api.routes import online_ocr as route
    monkeypatch.setattr(route, 'load_config', lambda: {'obsidian_vault_path': str(tmp_path)})
    monkeypatch.setattr(route, '_session_inventory', lambda *a, **k: {
        'session_token': 'sources-and-settings', 'sources': [], 'source_count': 0})
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    client = app.test_client()
    preview = client.post('/api/online-ocr/session/preview', json={
        'run_index': True, 'operation_mode': mode}).json
    assert preview['session_token'].startswith('intent-v1:')
    assert preview['operation_mode'] == mode
    for replacement in [None, 'invalid', 'update_search' if mode == 'prepare_text' else 'prepare_text']:
        body = dict(run_index=True, no_external=True, session_token=preview['session_token'],
                    confirm_index=replacement == 'update_search')
        if replacement is not None:
            body['operation_mode'] = replacement
        response = client.post('/api/online-ocr/session/start', json=body)
        assert response.status_code == 400
    stale = client.post('/api/online-ocr/session/start', json={
        'run_index': True, 'operation_mode': mode, 'no_external': True,
        'confirm_index': mode == 'update_search', 'session_token': 'intent-v1:stale'})
    assert stale.status_code == 400


def test_preparation_receipt_cannot_continue_index(tmp_path, monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route
    run_id = 'd' * 32
    directory = tmp_path / 'online_index_runs' / run_id
    directory.mkdir(parents=True)
    (directory / 'run.json').write_text(json.dumps({'operation_mode': 'prepare_text'}))
    monkeypatch.setattr(route, 'BASE_DIR', str(tmp_path))
    def forbidden(*a, **k):
        pytest.fail('Preparation must be rejected before accessing configuration or indexing')
    monkeypatch.setattr(route, 'load_config', forbidden)
    monkeypatch.setattr(route.obsidian_manager, 'index_vault', forbidden)
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    response = app.test_client().post('/api/online-ocr/session/continue-index', json={
        'confirm_index': True, 'run_id': run_id})
    assert response.status_code == 409
    assert 'Preparation cannot' in response.json['error']


def test_active_local_index_overrides_old_online_receipt(monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route
    monkeypatch.setattr(route, '_job', {'state': 'text_prepared', 'run_id': 'old'})
    monkeypatch.setattr(route.obsidian_manager, 'get_status', lambda: 'embedding')
    monkeypatch.setattr(route.obsidian_manager, 'get_progress_snapshot', lambda: {
        'phase': 'embedding', 'message': 'Embedding fixture chunks'})
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    status = app.test_client().get('/api/online-ocr/status').json
    assert status['run_id'] == 'local-index'
    assert status['state'] == 'indexing'
    assert status['index_progress']['message'] == 'Embedding fixture chunks'


@pytest.mark.parametrize('state,expected', [('embedding', 'indexing'), ('done', 'indexed'),
    ('error', 'failed'), ('paused_partial', 'index_incomplete'), ('idle', 'idle')])
def test_newer_local_run_never_reverts_to_old_preparation(monkeypatch, state, expected):
    from flask import Flask
    from api.routes import online_ocr as route
    monkeypatch.setattr(route, '_job', {'state': 'text_prepared', 'run_id': 'old',
                                      'index_run_id': 'earlier-index'})
    monkeypatch.setattr(route.obsidian_manager, 'get_status', lambda: state)
    monkeypatch.setattr(route.obsidian_manager, 'get_progress_snapshot', lambda: {
        'state': state, 'run_id': 'new-index', 'message': 'Fixture index', 'error_detail': ''})
    monkeypatch.setattr(route.obsidian_manager, 'get_postindex_bm25_status', lambda: 'preparing')
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    result = app.test_client().get('/api/online-ocr/status').json
    assert result['state'] == expected
    assert result['run_id'] == 'local-new-index'
    assert result['bm25_status'] == 'preparing'


@pytest.mark.parametrize('body', [
    {'operation_mode': 'prepare_text', 'run_index': False},
    {'operation_mode': 'prepare_text', 'run_index': True, 'prepare_only': False},
    {'operation_mode': 'update_search', 'run_index': True, 'prepare_only': True},
    {'operation_mode': 'prepare_text', 'run_index': True, 'full_rebuild': True},
    {'operation_mode': [], 'run_index': True},
    {'run_index': True, 'session_token': 'intent-v1:bound'},
])
def test_conflicting_intent_is_refused(body):
    from api.routes.online_ocr import _session_intent
    with pytest.raises(ValueError):
        _session_intent(body)


@pytest.mark.parametrize('intent', [None, {'operation_mode': 'invalid'},
    {'operation_mode': 'prepare_text', 'prepare_only': False},
    {'operation_mode': 'update_search', 'prepare_only': True}])
def test_promotion_refuses_missing_or_conflicting_receipts(tmp_path, monkeypatch, intent):
    from api.routes import online_ocr as route
    directory = tmp_path / 'online_index_runs' / ('a' * 32)
    directory.mkdir(parents=True)
    if intent is not None:
        (directory / 'run.json').write_text(json.dumps(intent))
    monkeypatch.setattr(route, '_job', {'run_id': directory.name, 'run_receipt': str(directory / 'run.json')})
    monkeypatch.setattr(route, 'BASE_DIR', str(tmp_path))
    monkeypatch.setattr(route.obsidian_manager, 'index_vault', lambda *a, **kw: pytest.fail('Index forbidden'))
    monkeypatch.setattr(route, 'load_config', lambda: pytest.fail('Refuse before loading configuration'))
    with pytest.raises(ValueError):
        route._promote_session_selected([], 1, run_index=True, settings={})


def test_targeted_retry_provenance_is_bound_to_preview():
    from api.routes.online_ocr import _session_intent, _intent_token
    assert _session_intent({'run_index': True, 'operation_mode': 'prepare_text',
                            'initiating_action': 'targeted_retry'}) == 'prepare_text'
    assert _intent_token('scope', 'prepare_text', 'targeted_retry') != _intent_token('scope', 'prepare_text')
    with pytest.raises(ValueError):
        _session_intent({'run_index': True, 'operation_mode': 'update_search',
                         'initiating_action': 'targeted_retry'})
