from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

from notesync import create_attempts


def test_concurrent_retry_and_changed_transport_remain_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(create_attempts, 'store_path', lambda: tmp_path / 'attempts.sqlite3')
    c = dict(parent_key='PARENT23', transport=SimpleNamespace(base_url='http://fixture'),
             lib_path='users/0', server_id='fixture', html='<p>Original</p>')
    def reserve():
        return create_attempts.reserve(tmp_path, 'paper', tmp_path / 'note.md', c)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(previous is None for _, previous in results) == 1
    identity = results[0][0]
    assert create_attempts.reserve(tmp_path / 'other-vault', 'alias', tmp_path / 'other.md', c) == (identity, {})
    c['server_id'] = 'different'
    c['html'] = '<p>Changed</p>'
    assert reserve() == (identity, {})
    create_attempts.acknowledge(identity, key='NOTE2345', version=None, creation_data=None)
    assert reserve()[1] == dict(note_key='NOTE2345', version=None, creation_data=None)


def test_lookup_is_read_only_and_does_not_expose_note_body(tmp_path, monkeypatch):
    path = tmp_path / 'attempts.sqlite3'
    monkeypatch.setattr(create_attempts, 'store_path', lambda: path)
    assert create_attempts.lookup(tmp_path, 'paper') is None
    assert not path.exists()
    c = dict(parent_key='PARENT23', transport=SimpleNamespace(base_url='http://fixture'),
             lib_path='users/0', server_id='fixture', html='<p>private text</p>')
    identity, _ = create_attempts.reserve(tmp_path, 'paper', tmp_path / 'note.md', c)
    before = path.read_bytes()
    assert create_attempts.lookup(tmp_path, 'paper')['status'] == 'uncertain'
    assert path.read_bytes() == before
    create_attempts.acknowledge(identity, key='NOTE2345', version=None, creation_data=None)
    evidence = create_attempts.lookup(tmp_path, 'paper')
    assert evidence['note_key'] == 'NOTE2345'
    assert evidence['status'] == 'acknowledged' and not evidence['automatic_retry_allowed']
    assert 'private text' not in str(evidence)
    assert create_attempts.lookup(tmp_path / 'other-vault', 'paper') is None


def test_inspection_never_releases_barrier_or_upgrades_baseline(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from tagging import zotero_write
    monkeypatch.setattr(create_attempts, 'store_path', lambda: tmp_path / 'attempts.sqlite3')
    c = dict(parent_key='PARENT23', transport=SimpleNamespace(base_url='http://fixture'),
             lib_path='users/0', server_id='fixture', html='<p>Original</p>')
    identity, _ = create_attempts.reserve(tmp_path, 'paper', tmp_path / 'missing.md', c)
    target = dict(base='http://fixture', library='users/0', server_id='fixture', parent='PARENT23')
    reader = Mock()
    assert create_attempts.inspect(tmp_path, 'paper', target=target, read_item=reader)['remote'] == 'unknown_outcome'
    reader.assert_not_called()
    data = dict(itemType='note', parentItem='PARENT23', note='<p>Original</p>')
    create_attempts.acknowledge(identity, key='NOTE2345', version=4, creation_data=data)
    reader.return_value = (data, 4)
    before = create_attempts.store_path().read_bytes()
    drifted = create_attempts.inspect(tmp_path, 'paper',
                                      target={**target, 'server_id': 'other'}, read_item=reader)
    assert drifted['remote'] == 'target_changed'
    # `target_changed` is a dead end for repair, so the reason IS the message.
    # The recorded server_id came from the live `Zotero-Server-ID` header at
    # creation; the compared one from the cached scan's SQLite snapshot -- name
    # the field, or a stale scan is indistinguishable from a real change.
    assert drifted['target_changed_fields'] == ['server_id']
    assert drifted['current_target']['server_id'] == 'other'
    assert drifted['target']['server_id'] == 'fixture'
    assert create_attempts.inspect(tmp_path, 'paper',
        target={**target, 'server_id': 'other', 'parent': 'OTHER234'},
        read_item=reader)['target_changed_fields'] == ['parent', 'server_id']
    reader.assert_not_called()
    for update, expected in [({}, 'matches_acknowledgement'), ({'deleted': True}, 'trashed'),
                             ({'parentItem': 'OTHER234'}, 'type_or_parent_changed'),
                             ({'note': 'Manual edit'}, 'changed')]:
        reader.return_value = ({**data, **update}, 4)
        result = create_attempts.inspect(tmp_path, 'paper', target=target, read_item=reader)
        assert result['remote'] == expected
        assert not result['automatic_retry_allowed'] and not result['automatic_undo_allowed']
    reader.side_effect = zotero_write.ZoteroNotFound('absent')
    assert create_attempts.inspect(tmp_path, 'paper', target=target, read_item=reader)['remote'] == 'absent_unresolved'
    assert create_attempts.store_path().read_bytes() == before


def test_attempt_listing_survives_scan_removal_without_cross_vault_disclosure(tmp_path, monkeypatch):
    import json
    import sqlite3
    monkeypatch.setattr(create_attempts, 'store_path', lambda: tmp_path / 'attempts.sqlite3')
    c = dict(parent_key='PARENT23', transport=SimpleNamespace(base_url='http://fixture'),
             lib_path='users/0', server_id='fixture', html='<p>private</p>')
    identity, _ = create_attempts.reserve(tmp_path, 'paper', tmp_path / 'note.md', c)
    rows = create_attempts.list_attempts(tmp_path)
    assert rows[0]['citation_key'] == 'paper' and not rows[0]['in_inventory']
    assert 'private' not in str(rows)
    assert create_attempts.list_attempts(tmp_path / 'other', ['paper']) == []
    # Old records have only the hashed subject. They must not be assigned to
    # a vault merely because the saved source happens to lie underneath it.
    with sqlite3.connect(create_attempts.store_path()) as db:
        payload = json.loads(db.execute('SELECT payload FROM attempts').fetchone()[0])
        del payload['vault']
        del payload['citation_key']
        db.execute('UPDATE attempts SET payload=? WHERE id=?', (json.dumps(payload), identity))
    assert create_attempts.list_attempts(tmp_path) == []
    assert create_attempts.list_attempts(tmp_path, ['paper'])[0]['in_inventory']


def test_a_store_without_a_schema_reads_as_empty_not_as_an_error(tmp_path, monkeypatch):
    """`reserve` creates the FILE on connect and the table one statement later.
    A crash or a full disk in between leaves a valid, empty database; reading it
    raised `no such table: attempts`, which `create-zotero-note` turns into a
    500 -- blocking every future creation behind an opaque error until someone
    deletes the file by hand. An empty store retains nothing, so it must answer
    like no store at all."""
    import sqlite3
    store = tmp_path / 'attempts.sqlite3'
    monkeypatch.setattr(create_attempts, 'store_path', lambda: store)
    sqlite3.connect(store).close()
    assert store.stat().st_size == 0
    assert create_attempts.lookup(tmp_path, 'paper') is None
    assert create_attempts.list_attempts(tmp_path, ['paper']) == []
    # It must still be usable, not merely readable.
    c = dict(parent_key='PARENT23', transport=SimpleNamespace(base_url='http://fixture'),
             lib_path='users/0', server_id='fixture', html='<p>Original</p>')
    identity, previous = create_attempts.reserve(tmp_path, 'paper', tmp_path / 'note.md', c)
    assert previous is None
    assert create_attempts.lookup(tmp_path, 'paper')['status'] == 'uncertain'
    assert [r['citation_key'] for r in create_attempts.list_attempts(tmp_path, ['paper'])] == ['paper']


def test_a_corrupt_store_still_fails_closed(tmp_path, monkeypatch):
    """The empty-store courtesy must not become "unreadable means nothing is
    retained": that would re-open duplicate creation on a damaged barrier."""
    import pytest
    import sqlite3
    store = tmp_path / 'attempts.sqlite3'
    monkeypatch.setattr(create_attempts, 'store_path', lambda: store)
    store.write_bytes(b'this is not a sqlite database, and never was' * 8)
    with pytest.raises(sqlite3.DatabaseError):
        create_attempts.lookup(tmp_path, 'paper')
    with pytest.raises(sqlite3.DatabaseError):
        create_attempts.list_attempts(tmp_path, ['paper'])
