"""F01/F02/F07: disposable contexts and real HTTP/writer seams only."""
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import flask
import pytest

from audit.config import Settings
from audit.manager import AuditManager
from tagging import writers, zotero_write as zw


def test_inventory_settings_published_together(tmp_path):
    manager = AuditManager()
    a, b = Settings(tmp_path / 'a'), Settings(tmp_path / 'b')
    inv_a, inv_b = object(), object()
    manager._inventory, manager._settings = inv_a, a
    with patch('audit.manager.load_settings', return_value=b), patch('threading.Thread.start'):
        assert manager.start_scan()[0]
    # The old pair survives a failed replacement when the original context
    # is restored. Pending Settings B must never replace published Settings A.
    with patch('audit.manager.load_settings', return_value=a):
        assert manager.get_inventory() == (inv_a, a)
        manager._finalise('error', manager._run_id, error='synthetic failure')
        assert manager.get_inventory() == (inv_a, a)
    manager._finalise('done', manager._run_id, inv=inv_b)
    with patch('audit.manager.load_settings', return_value=b):
        assert manager.get_inventory() == (inv_b, b)


def test_all_snapshot_accessors_refuse_changed_context(tmp_path):
    manager = AuditManager()
    a, b = Settings(tmp_path / 'a'), Settings(tmp_path / 'b')
    manager._inventory, manager._settings = object(), a
    manager._duplicates, manager._near_duplicates = [], object()
    with patch('audit.manager.load_settings', return_value=b), patch.object(manager, 'start_scan') as scan:
        assert manager.get_inventory() == (None, None)
        assert manager.get_duplicates() == (None, None)
        assert manager.get_near_duplicates() == (None, None)
        scan.assert_not_called()


class Response(io.BytesIO):
    def __init__(self, database, payload=None):
        super().__init__(json.dumps(payload or {'version': 7, 'data': {'tags': [{'tag': 'old'}]}}).encode())
        self.headers = {'Zotero-Server-ID': database}
        self.status = 200


@pytest.mark.parametrize('remove', [False, True])
@pytest.mark.parametrize('database', ['A', 'B'])
def test_first_tag_get_is_database_bound(remove, database):
    rec = SimpleNamespace(zotero_item=SimpleNamespace(item_key='KEY12345', server_id='A'))
    calls = []
    def opener(request, timeout):
        calls.append(request)
        return Response(database)
    with patch.object(writers, '_zotero_transport', return_value=zw.ZoteroTransport('local', 'http://localhost:23119', None)), patch.object(zw, '_urlopen', side_effect=opener):
        args = dict(rec=rec, valid=['old'] if remove else ['new'], cfg={}, citation_key='cite')
        result = writers._zotero_remove_prepare(**args) if remove else writers._zotero_prepare(**args, rejected=[])
    assert len(calls) == 1
    assert calls[0].get_header('Zotero-server-id') == 'A'
    if database == 'B':
        assert result[0] is None
        assert result[2] == 502
    else:
        assert result[0]['version'].server_id == 'A'


@pytest.mark.parametrize('transport,server_id', [('local', None), ('web', 'A')])
def test_missing_or_local_to_web_identity_never_dispatches(transport, server_id):
    rec = SimpleNamespace(zotero_item=SimpleNamespace(item_key='KEY12345', server_id=server_id))
    with patch.object(writers, '_zotero_transport', return_value=zw.ZoteroTransport(transport, 'https://api.zotero.org' if transport == 'web' else 'http://localhost:23119', 'FAKE' if transport == 'web' else None)), patch.object(zw, '_urlopen') as request:
        prepared, error, status = writers._zotero_prepare(rec=rec, valid=['new'], rejected=[], cfg={}, citation_key='cite')
    assert prepared is None and status == 502
    assert 'no request was sent' in error['error']
    request.assert_not_called()


def test_reparent_completion_ignores_only_volatile_device(tmp_path):
    from accession import reparent
    identity = {'dev': 1, 'ino': 12, 'sha256': 'abc', 'size': 3}
    before = {'key': 'ATT', 'parentItem': 'OLD'}
    parents = [{'key': 'OLD'}, {'key': 'NEW'}]
    operation = dict(id='op', base='http://localhost:23119', database='A', pdf='p.pdf',
                     attachment='ATT', old_parent='OLD', desired_parent='NEW',
                     before=before, parents=parents, physical=identity,
                     bibliography_identity=identity, mapping={}, review={}, status='acknowledged')
    row = dict(physical={**identity, 'dev': 2}, bibliography_identity={**identity, 'dev': 2},
               mapping={}, review={}, associations=[dict(deleted=False, attachment='ATT', parent='NEW')])
    with patch.object(reparent, 'operations', return_value=[operation]), patch.object(reparent.zw, 'resolve_transport', return_value=zw.ZoteroTransport('local', operation['base'], None)), patch.object(reparent.rec, 'saved'), patch.object(reparent.rec, 'observe'), patch.object(reparent.rec, 'retain_history', return_value={'database': 'A'}), patch.object(reparent.rec, 'rows', return_value=[row]), patch.object(reparent.zw, 'get_item', side_effect=[({**before, 'parentItem': 'NEW'}, 7), (parents[0], 8), (parents[1], 9)] * 2):
        assert reparent.inspect(Settings(tmp_path), {}, 'op')['eligible']
        row['physical']['sha256'] = 'changed'
        assert not reparent.inspect(Settings(tmp_path), {}, 'op')['eligible']


def test_context_lease_survives_disconnected_sse_worker():
    from api.audit_context import register_context_guard
    from api.sse import run_sse_worker
    app = flask.Flask(__name__)
    register_context_guard(app)
    entered, finish, exited = threading.Event(), threading.Event(), threading.Event()
    @app.post('/api/notesync/fixture')
    def mutation():
        def worker(put, cancel):
            entered.set()
            put({'started': True})
            finish.wait(3)
            exited.set()
        return run_sse_worker(worker, consumer_timeout_s=2)
    @app.post('/api/config')
    def config():
        return flask.jsonify(ok=True)
    client = app.test_client()
    response = client.post('/api/notesync/fixture', buffered=False)
    assert entered.wait(2)
    response.close()
    try:
        assert client.post('/api/config', json={'obsidian_vault_path': '/fixture'}).status_code == 409
        assert client.post('/api/config', json={'temperature': 0.3}).status_code == 200
    finally:
        finish.set()
        assert exited.wait(2)
    # The guard's own worker completion follows the worker's finally.
    import time
    for _ in range(100):
        result = client.post('/api/config', json={'obsidian_vault_path': '/fixture'})
        if result.status_code == 200:
            break
        time.sleep(.01)
    assert result.status_code == 200


def test_vault_cleanup_preserves_existing_index_and_sidecar_bytes(tmp_path):
    from rag.vault import ObsidianVaultManager
    manager = ObsidianVaultManager()
    sidecar = tmp_path / 'bm25'
    sidecar.mkdir()
    for path in (sidecar / 'scores.npz', tmp_path / 'wikilinks.json',
                 tmp_path / 'docstore.json', tmp_path / 'default__vector_store.json'):
        path.write_bytes(b'EXISTING FORMAT FIXTURE\x00\xff')
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with patch.object(manager, '_bm25_sidecar_dir', return_value=str(sidecar)), patch.object(manager, '_wikilink_sidecar_path', return_value=str(tmp_path / 'wikilinks.json')):
        manager.cleanup()
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before


def test_notesync_route_refuses_previous_vault_before_read_or_write(tmp_path):
    from api.routes import notesync
    manager = AuditManager()
    manager._inventory = SimpleNamespace(records={})
    manager._settings = Settings(tmp_path / 'A')
    app = flask.Flask(__name__)
    app.register_blueprint(notesync.notesync_bp)
    with patch.object(notesync, 'audit_manager', manager), patch('audit.manager.load_settings', return_value=Settings(tmp_path / 'B')), patch.object(notesync, 'load_settings', return_value=Settings(tmp_path / 'B')), patch.object(notesync, '_fresh_zotero_notes', return_value={}) as read, patch.object(notesync, '_create_notes', return_value=({'ok': True}, 200)) as write:
        response = app.test_client().post('/api/notesync/create', json={'citation_keys': ['cite'], 'confirm': True}, headers={'X-Requested-With': 'ChatEKLD'})
    assert response.status_code == 404
    read.assert_not_called()
    write.assert_not_called()


def test_invalidated_scan_cannot_publish_old_pair(tmp_path):
    manager = AuditManager()
    with patch('audit.manager.load_settings', return_value=Settings(tmp_path)), patch('threading.Thread.start'):
        manager.start_scan()
    run_id = manager._run_id
    manager.reset_to_idle()
    manager._finalise('done', run_id, inv=object())
    assert manager.get_inventory() == (None, None)
    assert not manager.is_scanning()


def test_tag_patch_uses_only_authorization_for_scanned_database():
    from tagging import local_api
    requests = []
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if request.method == 'GET':
                return Response('A')
            result = Response('A')
            result.status = 204
            result.headers['Last-Modified-Version'] = '8'
            return result
    base = 'http://localhost:23119'
    rec = SimpleNamespace(zotero_item=SimpleNamespace(item_key='KEY12345', server_id='A'))
    with patch.object(writers, '_zotero_transport', return_value=zw.ZoteroTransport('local', base, None)), patch.object(zw, '_opener', Opener()), patch.dict(local_api._grants, {(base, 'B'): ('B' * 32, True)}, clear=True):
        prepared, _, _ = writers._zotero_prepare(rec=rec, valid=['new'], rejected=[], cfg={}, citation_key='cite')
        result, status, version = writers._zotero_patch(prepared)
        assert status != 200
        assert version is None
        assert len(requests) == 1
        local_api._grants[(base, 'A')] = ('A' * 32, True)
        _, status, version = writers._zotero_patch(prepared)
        assert status == 200
        assert version == 8
        assert prepared['version'].server_id == 'A'
        assert requests[-1].get_header('Zotero-api-key') == 'A' * 32
        assert requests[-1].get_header('Zotero-server-id') == 'A'
        assert requests[-1].get_header('If-unmodified-since-version') == '7'


@pytest.mark.parametrize('mode', ['never_consumed', 'start_failure'])
def test_context_worker_lease_releases_without_a_started_worker(mode):
    from api.audit_context import register_context_guard
    from api.sse import run_sse_worker
    app = flask.Flask(__name__)
    register_context_guard(app)
    with app.test_request_context('/api/notesync/fixture', method='POST'):
        assert app.preprocess_request() is None
        response = run_sse_worker(lambda put, cancel: None, consumer_timeout_s=1)
        if mode == 'start_failure':
            with patch('threading.Thread.start', side_effect=RuntimeError('synthetic exhaustion')):
                assert 'synthetic exhaustion' in ''.join(response.response)
        response.close()
    with app.test_request_context('/api/config', method='POST', json={'obsidian_vault_path': '/fixture'}):
        assert app.preprocess_request() is None


def test_legacy_desktop_item_cannot_be_relabelled_web():
    from audit.core.zotero import ZoteroItem
    item = ZoteroItem(item_key='LEGACY12', item_type='book', title='Fixture')
    # Older snapshots have no database ID. An arbitrary marker cannot grant
    # them the independent Web proof required for a different transport.
    item.source_transport = 'web'
    with pytest.raises(zw.ZoteroError, match='Desktop'):
        writers._zotero_read_identity(zw.ZoteroTransport('web', 'https://api.zotero.org', 'FAKE'), item)


def test_even_apparent_config_noop_serializes_against_active_context():
    from api.audit_context import register_context_guard
    app = flask.Flask(__name__)
    register_context_guard(app)
    with patch('core.config.load_config', return_value={'obsidian_vault_path': '/same'}):
        with app.test_request_context('/api/notesync/fixture', method='POST'):
            assert app.preprocess_request() is None
            # Separate request context/thread: this re-save must not escape
            # admission just because it currently equals the stored value.
            result = []
            def config_request():
                with app.test_request_context('/api/config', method='POST', json={'obsidian_vault_path': '/same'}):
                    result.append(app.preprocess_request())
            thread = threading.Thread(target=config_request)
            thread.start()
            thread.join(2)
            assert not thread.is_alive()
            assert result[0][1] == 409
