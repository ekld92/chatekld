"""Legacy resource diagnostics are read-only, bound and never a source verdict."""
import hashlib
import json

import pytest

from core.online_runs import source_diagnostics, source_exception, known_failed_content, inspect_run


def evidence(tmp_path):
    run = tmp_path / 'online_index_runs' / ('a' * 32)
    run.mkdir(parents=True)
    digest = 'b' * 64
    (run / 'run.json').write_text(json.dumps({'vault_key': 'vault', 'source_count': 1}))
    bundle = {'stop_reason': 'initial_memory_pressure', 'payload': {'source_sha256': digest, 'recipe': 'recipe'}}
    identity = hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    bundle['snapshot_id'] = identity
    exports = tmp_path / 'online_ocr_exports'
    exports.mkdir()
    snapshot = exports / (identity + '.json')
    snapshot.write_text(json.dumps(bundle))
    row = {'source': 'doc.pdf', 'source_sha256': digest, 'recipe': 'recipe',
           'status': 'partial', 'snapshot_id': identity, 'page_statuses': ['not_attempted']}
    receipt = run / 'source-000000.json'
    receipt.write_text(json.dumps(row))
    return run, receipt, snapshot, row


def test_legacy_memory_stop_visible_in_history_without_rewriting_or_deferring_pdf(tmp_path):
    run, receipt, snapshot, row = evidence(tmp_path)
    before = {p: p.read_bytes() for p in [receipt, snapshot]}
    history = inspect_run(tmp_path, run.name, 'vault')
    assert history['sources'][0]['stop_reason'] == 'initial_memory_pressure'
    assert history['sources'][0]['failure_scope'] == 'global'
    assert known_failed_content(tmp_path, 'vault') == {}
    assert not source_exception(row)  # not_attempted is not a document defect
    assert all(p.read_bytes() == contents for p, contents in before.items())


@pytest.mark.parametrize('damage', ['hash', 'source', 'symlink', 'missing'])
def test_legacy_snapshot_must_match_immutable_binding(tmp_path, damage):
    _, _, snapshot, row = evidence(tmp_path)
    if damage == 'hash':
        data = json.loads(snapshot.read_text())
        data['stop_reason'] = 'critical_memory_pressure'
        snapshot.write_text(json.dumps(data))
    elif damage == 'source':
        row['source_sha256'] = 'c' * 64
    elif damage == 'symlink':
        other = tmp_path / 'outside.json'
        snapshot.rename(other)
        snapshot.symlink_to(other)
    else:
        snapshot.unlink()
    assert 'stop_reason' not in source_diagnostics(tmp_path, row)


def test_deferred_history_can_read_original_memory_reason_but_not_another_source(tmp_path):
    run, receipt, _, row = evidence(tmp_path)
    deferred = {'status': 'deferred_known_failure', 'source_sha256': row['source_sha256'],
                'evidence': {'run_id': run.name, 'receipt': receipt.name}}
    assert source_diagnostics(tmp_path, deferred)['stop_reason'] == 'initial_memory_pressure'
    deferred['source_sha256'] = 'd' * 64
    assert 'stop_reason' not in source_diagnostics(tmp_path, deferred)


@pytest.mark.parametrize('reason', ['initial_memory_pressure', 'engine_unavailable'])
def test_single_pdf_status_retains_runtime_and_provider_diagnostics(tmp_path, monkeypatch, reason):
    from flask import Flask
    from api.routes import online_ocr as route
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'fixture.pdf'
    source.write_bytes(b'fixture; parser stubbed')
    monkeypatch.setattr(route, 'BASE_DIR', str(tmp_path / 'base'))
    monkeypatch.setattr(route, '_source', lambda _: (vault, source, source.name))
    monkeypatch.setattr(route, 'load_config', lambda: {})
    monkeypatch.setattr(route, '_preflight_index_settings', lambda _: None)
    monkeypatch.setattr(route, '_preview', lambda *a, **kw: {'page_range': [1, 1], 'eligible': 1, 'has_cached_text': False})
    monkeypatch.setattr(route, '_single_preview_token', lambda _: 'bound')
    monkeypatch.setattr(route.OnlineOCR, 'identity', lambda _: {'provider': 'deepseek', 'model': 'fixture'})
    page = {'binding': {'page': 1}, 'status': 'not_attempted'}
    if reason == 'engine_unavailable':
        page.update(status='engine_error', error_detail='Fixture HTTP rejection', failure_scope='global', http_status=401)
    monkeypatch.setattr(route, 'extract_pages', lambda *a, **kw: {
        'coverage': 'partial', 'snapshot_id': 'c' * 64, 'payload': {'recipe': 'fixture'},
        'stop_reason': reason, 'pages': [page]})
    monkeypatch.setattr(route, 'export_pages', lambda *a: tmp_path / 'fixture.json')
    monkeypatch.setattr(route.obsidian_manager, 'get_status', lambda: 'idle')
    monkeypatch.setattr(route.obsidian_manager, 'try_acquire_lock', lambda **kw: 1)
    monkeypatch.setattr(route.obsidian_manager, 'release_lock', lambda _: None)
    threads = []
    monkeypatch.setattr(route.obsidian_manager, 'register_index_thread', threads.append)
    before = dict(route._job)
    route._job.clear()
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    try:
        client = app.test_client()
        response = client.post('/api/online-ocr/start', json={
            'path': source.name, 'source_sha256': route._digest(source), 'preview_token': 'bound',
            'provider': 'deepseek', 'model': 'fixture', 'confirm_external': True, 'reextract': True})
        assert response.status_code == 200, response.json
        threads[0].join(5)
        assert not threads[0].is_alive()
        result = client.get('/api/online-ocr/status').json
        assert result['run_id'] == response.json['run_id']
        assert result['state'] == 'extraction_incomplete'
        assert result['stop_reason'] == reason
        assert result['failure_scope'] == 'global'
        assert result['error_detail'] == ('Fixture HTTP rejection' if reason == 'engine_unavailable'
                                          else 'Document preparation stopped because macOS reported memory pressure before processing.')
    finally:
        for thread in threads:
            thread.join(5)
        route._job.clear()
        route._job.update(before)
