"""Bounded Read/Library overlap: synthetic provider and disposable empty library.

This exercises Python workers and cancellation, not WKWebView or model pressure.
"""
import json
import threading


def test_read_wait_does_not_block_library_status_or_scan(tmp_path, monkeypatch):
    from app import app
    from api.routes import paper, audit as routes
    from audit.manager import AuditManager
    from core.database import init_db, get_db_connection

    vault = tmp_path / 'vault'
    (vault / 'Z_attachments' / 'biblio_articles').mkdir(parents=True)
    (vault / 'Z_Zotero_Notes').mkdir()
    (vault / '_master.bib').write_text('@article{fixture,title={Fixture}}')
    cfg_path = tmp_path / 'config.json'
    cfg_path.write_text(json.dumps({'obsidian_vault_path': str(vault),
                                   'audit_zotero_sqlite': str(tmp_path / 'absent.sqlite'),
                                   'audit_zotero_storage': str(tmp_path / 'absent-storage')}))
    monkeypatch.setattr('core.config.CONFIG_FILE', str(cfg_path))
    monkeypatch.setattr('core.constants.CONFIG_FILE', str(cfg_path))
    monkeypatch.setattr('core.constants.BASE_DIR', str(tmp_path))
    manager = AuditManager()
    monkeypatch.setattr(routes, 'audit_manager', manager)
    # No reconciliation transport or provider can escape this disposable scan.
    monkeypatch.setattr('api.routes.pending.workspace', lambda *a, **kw: None)
    monkeypatch.setattr(routes, '_build_pdf_text_provider', lambda: None)
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()

    def fake_summary(*args, should_stop, **kwargs):
        entered.set()
        try:
            assert release.wait(3), 'bounded fixture deadline exceeded'
            if not should_stop():
                yield 'fixture summary'
        finally:
            ended.set()

    monkeypatch.setattr(paper, 'summarise_stream', fake_summary)
    init_db()
    with get_db_connection() as conn:
        conn.execute('INSERT INTO uploads(upload_id,filename,extracted_text) VALUES(?,?,?)',
                     ('overlap-fixture', 'fixture.pdf', 'Disposable text'))
        conn.commit()
    headers = {'X-Requested-With': 'ChatEKLD'}
    response = app.test_client().post('/api/summarise', headers=headers,
                                    json={'upload_id': 'overlap-fixture', 'model': 'fixture'}, buffered=False)
    try:
        assert entered.wait(2)
        client = app.test_client()
        assert client.post('/api/audit/scan', headers=headers, json={
            'count_annotations': False, 'include_duplicates': False,
            'include_near_duplicates': False}).status_code == 200
        assert client.get('/api/audit/status', headers=headers).status_code == 200
        assert manager.wait_for_idle(timeout=3)
        assert manager.get_status_payload()['state'] == 'done'
        # Disconnect the consumer while generation is waiting, then let it exit.
        response.close()
    finally:
        response.close()
        release.set()
        assert ended.wait(3)
        manager.request_cancel()
        assert manager.wait_for_idle(timeout=3)
