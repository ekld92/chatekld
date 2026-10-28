"""A real SQLite WAL fixture, not a mocked read_items result."""
import sqlite3
from audit.core.zotero import _open_snapshot


def test_direct_fallback_handles_uri_characters(tmp_path, monkeypatch):
    from audit.core import zotero
    path = tmp_path / 'fixture?revision#1.sqlite'
    with sqlite3.connect(path) as writer:
        writer.execute('CREATE TABLE sample (value INTEGER)')
        writer.execute('INSERT INTO sample VALUES (1)')
    monkeypatch.setattr(zotero, '_SNAPSHOT_COPY_MAX_BYTES', 0)
    with _open_snapshot(path) as snapshot:
        assert snapshot.execute('SELECT COUNT(*) FROM sample').fetchone()[0] == 1


def test_snapshot_includes_committed_wal_without_touching_source(tmp_path):
    path = tmp_path / 'fixture.sqlite'
    writer = sqlite3.connect(path)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE sample (value TEXT)')
        writer.commit()
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        writer.execute("INSERT INTO sample VALUES ('committed-only-in-wal')")
        writer.commit()
        sources = [path, path.with_name(path.name+'-wal'), path.with_name(path.name+'-shm')]
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in sources]
        with _open_snapshot(path) as snapshot:
            assert [tuple(row) for row in snapshot.execute('SELECT * FROM sample')] == [('committed-only-in-wal',)]
        assert before == [(p.read_bytes(), p.stat().st_mtime_ns) for p in sources]
    finally:
        writer.close()


def test_changed_copy_uses_sqlite_consistent_fallback(tmp_path, monkeypatch):
    from audit.core import zotero
    path = tmp_path / 'fixture.sqlite'
    writer = sqlite3.connect(path)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE sample (value INTEGER)')
        writer.commit()
        original = zotero.shutil.copy2
        changed = False
        def copy(source, target):
            nonlocal changed
            result = original(source, target)
            if not changed:
                changed = True
                writer.execute('INSERT INTO sample VALUES (1)')
                writer.commit()
            return result
        monkeypatch.setattr(zotero.shutil, 'copy2', copy)
        with _open_snapshot(path) as snapshot:
            assert snapshot.execute('SELECT COUNT(*) FROM sample').fetchone()[0] == 1
    finally:
        writer.close()


def test_parent_database_identity_comes_from_same_snapshot(tmp_path):
    from audit.core.zotero import read_items
    path = tmp_path / 'fixture.sqlite'
    with sqlite3.connect(path) as writer:
        writer.executescript('''
            CREATE TABLE items (itemID INTEGER, key TEXT, itemTypeID INTEGER,
                dateModified TEXT, dateAdded TEXT, libraryID INTEGER, version INTEGER);
            CREATE TABLE itemTypes (itemTypeID INTEGER, typeName TEXT);
            CREATE TABLE deletedItems (itemID INTEGER);
            CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
            CREATE TABLE fields (fieldID INTEGER, fieldName TEXT);
            CREATE TABLE itemDataValues (valueID INTEGER, value TEXT);
            CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
            CREATE TABLE tags (tagID INTEGER, name TEXT);
            CREATE TABLE itemAttachments (itemID INTEGER, parentItemID INTEGER,
                path TEXT, contentType TEXT, linkMode INTEGER);
            CREATE TABLE itemNotes (itemID INTEGER, parentItemID INTEGER, note TEXT);
            INSERT INTO items VALUES (1,'PARENT23',1,'2026-09-09','2026-09-09',1,0);
            INSERT INTO itemTypes VALUES (1,'journalArticle');
        ''')
    assert read_items(path, tmp_path)[0].server_id is None
    with sqlite3.connect(path) as writer:
        writer.executescript('''
            CREATE TABLE settings (setting TEXT, key TEXT, value TEXT);
            INSERT INTO settings VALUES ('localAPI','serverID','fixture-database');
        ''')
    assert read_items(path, tmp_path)[0].server_id == 'fixture-database'
