"""Note-cache callers must observe committed WAL data, not only checkpoints."""
import sqlite3
from types import SimpleNamespace

import pytest

from api.routes import notesync
from audit.core import zotero


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'fixture.sqlite'
    con = sqlite3.connect(path)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA wal_autocheckpoint=0')
    con.executescript('''
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
        INSERT INTO items VALUES (1,'PARENT23',1,'2026-09-10','2026-09-10',1,0);
        INSERT INTO itemTypes VALUES (1,'journalArticle');
        INSERT INTO itemTypes VALUES (2,'note');
    ''')
    con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    notesync._clear_states_cache()
    try:
        yield SimpleNamespace(zotero_sqlite=path, zotero_storage=tmp_path), con
    finally:
        con.close()
        notesync._clear_states_cache()


def add_note(con):
    con.execute("INSERT INTO items VALUES (2,'NOTE2345',2,'2026-09-10','2026-09-10',1,1)")
    con.execute("INSERT INTO itemNotes VALUES (2,1,'<p>new manual note</p>')")
    con.commit()


def test_note_cache_observes_wal_only_commit(database):
    settings, con = database
    assert notesync._fresh_zotero_notes(settings) == {}
    st = settings.zotero_sqlite.stat()
    add_note(con)
    after = settings.zotero_sqlite.stat()
    assert (st.st_size, st.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    assert len(notesync._fresh_zotero_notes(settings)['PARENT23']) == 1


def test_changed_snapshot_is_not_published_as_fresh(database, monkeypatch):
    settings, con = database
    original = zotero.read_items
    def changing(*args):
        items = original(*args)
        add_note(con)
        return items
    monkeypatch.setattr(zotero, 'read_items', changing)
    with pytest.raises(RuntimeError, match='changed while reading'):
        notesync._fresh_zotero_notes(settings)
    assert notesync._FRESH_NOTES is None


def test_state_invalidation_releases_note_cache(database):
    settings, _ = database
    notesync._fresh_zotero_notes(settings)
    assert notesync._FRESH_NOTES is not None
    notesync._clear_states_cache()
    assert notesync._FRESH_NOTES is None
