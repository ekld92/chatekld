"""Concurrent observations and identity reversals on disposable libraries."""
from pathlib import Path
import sqlite3
import pytest
from accession import imports, reconciliation
import test_library_imports as base


requires_birthtime = pytest.mark.skipif(
    not hasattr(Path(__file__).stat(), "st_birthtime"),
    reason="Requires stable file birth time; exercised by macos-platform CI.")

@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(reconciliation, 'store_path', lambda: tmp_path / 'state.sqlite3')
    return base.Library(tmp_path)

def test_old_mirror_completion_after_new_observation_keeps_note_open(lib):
    lib.run()
    parent = lib.entry('ENTRY001', 'paper')
    nid = lib.note(parent, 'NOTE0001', '<p>v1</p>')
    sid, _ = lib.run()
    uid = 'note:1:NOTE0001'
    v1 = imports.unit(lib.settings, uid)['note_sha256']
    lib.edit_note(nid, '<p>v2</p>')
    lib.run() # a concurrent observation sees v2 before v1 completes
    imports.record_step(lib.settings, sid, uid, 'mirror', 'done', note_sha256=v1)
    _, report = lib.run()
    assert uid in {r['id'] for r in report['open']}, imports.unit(lib.settings, uid)

def test_reparented_attachment_does_not_credit_new_parent_with_old_tags(lib):
    lib.run()
    p1 = lib.entry('ENTRY001', 'first')
    att, _ = lib.pdf(p1, 'ATT00001', 'first.pdf')
    sid, _ = lib.run()
    uid = 'attachment:1:ATT00001'
    imports.record_step(lib.settings, sid, uid, 'rename', 'done')
    imports.record_step(lib.settings, sid, uid, 'tag', 'done')
    p2 = lib.entry('ENTRY002', 'second')
    with sqlite3.connect(lib.db) as con:
        con.execute('UPDATE itemAttachments SET parentItemID=? WHERE itemID=?', (p2, att))
    _, report = lib.run()
    assert any('tag' in r['steps'] and r['id'] in {uid, 'entry:1:ENTRY002'} for r in report['open']), report

@requires_birthtime
def test_rekey_chain_back_to_original_keeps_latest_history(lib):
    lib.run()
    p = lib.entry('ENTRY001', 'first')
    att, _ = lib.pdf(p, 'ATT00001', 'first.pdf')
    sid, _ = lib.run()
    imports.record_step(lib.settings, sid, 'attachment:1:ATT00001', 'rename', 'done')
    for key in ('ATT00002', 'ATT00003'):
        with sqlite3.connect(lib.db) as con:
            con.execute('UPDATE items SET key=? WHERE itemID=?', (key, att))
        sid, _ = lib.run()
    imports.record_step(lib.settings, sid, 'attachment:1:ATT00003', 'tag', 'done')
    with sqlite3.connect(lib.db) as con:
        con.execute('UPDATE items SET key=? WHERE itemID=?', ('ATT00001', att))
    _, report = lib.run()
    assert report['open'] == [], report
