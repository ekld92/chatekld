"""Library-workflow lot 2: tracking new imports (accession/imports.py).

Every test runs against a disposable Zotero SQLite built here and a temporary
vault, read through the REAL `reconciliation.observe` (WAL-aware snapshot) —
no mocked item lists — so the identities under test are the ones production
computes.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from accession import imports
from audit.config import Settings

_SCHEMA = '''
CREATE TABLE settings (setting TEXT, key TEXT, value TEXT);
CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INTEGER,
    dateModified TEXT, dateAdded TEXT, libraryID INTEGER, version INTEGER);
CREATE TABLE itemTypes (itemTypeID INTEGER, typeName TEXT);
CREATE TABLE deletedItems (itemID INTEGER);
CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
CREATE TABLE fields (fieldID INTEGER, fieldName TEXT);
CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
CREATE TABLE creators (creatorID INTEGER, firstName TEXT, lastName TEXT, fieldMode INTEGER);
CREATE TABLE creatorTypes (creatorTypeID INTEGER, creatorType TEXT);
CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER);
CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
CREATE TABLE tags (tagID INTEGER, name TEXT);
CREATE TABLE itemAttachments (itemID INTEGER, parentItemID INTEGER,
    path TEXT, contentType TEXT, linkMode INTEGER);
CREATE TABLE itemNotes (itemID INTEGER, parentItemID INTEGER, note TEXT);
INSERT INTO settings VALUES ('localAPI','serverID','fixture-db');
INSERT INTO itemTypes VALUES (1,'journalArticle'),(2,'attachment'),(3,'note');
INSERT INTO fields VALUES (1,'title'),(2,'citationKey');
'''



requires_birthtime = pytest.mark.skipif(
    not hasattr(Path(__file__).stat(), "st_birthtime"),
    reason="Requires stable file birth time; exercised by macos-platform CI.")

class Library:
    """A tiny Zotero + vault that tests mutate like the real apps would."""

    def __init__(self, root: Path):
        self.vault = root / 'vault'
        self.biblio = self.vault / 'Z_attachments' / 'biblio_articles'
        self.biblio.mkdir(parents=True)
        self.db = root / 'zotero.sqlite'
        with sqlite3.connect(self.db) as con:
            con.executescript(_SCHEMA)
        self.settings = Settings(vault_root=self.vault, zotero_sqlite=self.db,
                                 zotero_storage=root / 'storage')
        self._next = 1

    def _id(self):
        self._next += 1
        return self._next

    def _field(self, con, item, field, value):
        vid = self._id() + 10_000
        con.execute('INSERT INTO itemDataValues VALUES (?,?)', (vid, value))
        con.execute('DELETE FROM itemData WHERE itemID=? AND fieldID=?', (item, field))
        con.execute('INSERT INTO itemData VALUES (?,?,?)', (item, field, vid))

    def entry(self, key, citation_key, title='A paper'):
        iid = self._id()
        with sqlite3.connect(self.db) as con:
            con.execute('INSERT INTO items VALUES (?,?,1,?,?,1,1)', (iid, key, 'd', 'd'))
            self._field(con, iid, 1, title)
            self._field(con, iid, 2, citation_key)
        return iid

    def pdf(self, parent_iid, key, name):
        path = self.biblio / name
        path.write_bytes(b'%PDF-1.4 ' + name.encode())
        iid = self._id()
        with sqlite3.connect(self.db) as con:
            con.execute('INSERT INTO items VALUES (?,?,2,?,?,1,1)', (iid, key, 'd', 'd'))
            con.execute('INSERT INTO itemAttachments VALUES (?,?,?,?,2)',
                        (iid, parent_iid, str(path), 'application/pdf'))
        return iid, path

    def note(self, parent_iid, key, html):
        iid = self._id()
        with sqlite3.connect(self.db) as con:
            con.execute('INSERT INTO items VALUES (?,?,3,?,?,1,1)', (iid, key, 'd', 'd'))
            con.execute('INSERT INTO itemNotes VALUES (?,?,?)', (iid, parent_iid, html))
        return iid

    def edit_note(self, iid, html):
        with sqlite3.connect(self.db) as con:
            con.execute('UPDATE itemNotes SET note=? WHERE itemID=?', (html, iid))

    def rename(self, att_iid, old: Path, new_name: str):
        """What the app's renamer does: move the file, repoint Zotero."""
        new = old.with_name(new_name)
        os.rename(old, new)
        with sqlite3.connect(self.db) as con:
            con.execute('UPDATE itemAttachments SET path=? WHERE itemID=?', (str(new), att_iid))
        return new

    def change_citation_key(self, iid, key):
        with sqlite3.connect(self.db) as con:
            self._field(con, iid, 2, key)

    def trash(self, iid):
        with sqlite3.connect(self.db) as con:
            con.execute('INSERT INTO deletedItems VALUES (?)', (iid,))

    def orphan(self, name):
        path = self.biblio / name
        path.write_bytes(b'%PDF-1.4 orphan ' + name.encode())
        return path

    def run(self):
        """One pass: new session, fresh observation, persisted."""
        sid = imports.begin_session(self.settings)
        return sid, imports.record_pass(self.settings, sid, imports.observe(self.settings))


@pytest.fixture
def lib(tmp_path):
    lib = Library(tmp_path)
    a = lib.entry('AAAA1111', 'smithOld2019')
    lib.entry_a = a
    lib.pdf(a, 'PDFA1111', 'smith_2019.pdf')
    lib.note_a = lib.note(a, 'NOTEA111', '<p>old reading</p>')
    lib.orphan('stray_2010.pdf')
    return lib


def _all_new(report):
    return sorted(u for ids in report['new'].values() for u in ids)


def test_first_activation_records_a_baseline_and_proposes_nothing(lib):
    _, report = lib.run()
    assert report['initialized'] is True
    assert _all_new(report) == []
    assert report['open'] == []
    tracked = imports.units(lib.settings)
    assert {u['kind'] for u in tracked.values()} == {'entry', 'attachment', 'note', 'document'}
    assert all(u['baseline'] for u in tracked.values())


def test_an_empty_library_at_activation_is_not_rebaselined(tmp_path):
    lib = Library(tmp_path)
    _, first = lib.run()
    assert first['initialized'] is True
    e = lib.entry('NEWW0001', 'doeNew2026')
    lib.pdf(e, 'PDFN0001', 'doe_2026.pdf')
    _, second = lib.run()
    assert second['initialized'] is False
    assert second['new']['new_entry_with_pdf'] == ['attachment:1:PDFN0001', 'entry:1:NEWW0001']


def test_acceptance_three_imports_a_rename_and_a_key_change(lib):
    """Plan §6 acceptance: no double processing, no loss of tracking, and a
    processed unit is not reclassified on the next pass."""
    lib.run()  # baseline

    # Three imports: two new entries with PDFs, one PDF added to a known entry.
    b = lib.entry('BBBB2222', 'jonesNew2026')
    b_att, b_pdf = lib.pdf(b, 'PDFB2222', 'Jones et al - 2026 - Full text.pdf')
    c = lib.entry('CCCC3333', 'leeNew2026')
    lib.pdf(c, 'PDFC3333', 'lee_2026.pdf')
    a_iid = lib.entry_a
    lib.pdf(a_iid, 'PDFA2222', 'smith_2019_supplement.pdf')

    sid1, first = lib.run()
    assert first['new']['new_entry_with_pdf'] == [
        'attachment:1:PDFB2222', 'attachment:1:PDFC3333', 'entry:1:BBBB2222', 'entry:1:CCCC3333']
    assert first['new']['pdf_added_to_known_entry'] == ['attachment:1:PDFA2222']
    open_ids = {r['id'] for r in first['open']}
    # Entries with a PDF owe nothing themselves: tagging runs once, via the PDF.
    assert open_ids == {'attachment:1:PDFB2222', 'attachment:1:PDFC3333', 'attachment:1:PDFA2222'}

    # The run renames B and tags it; C's tagging fails; A's supplement is done.
    imports.record_step(lib.settings, sid1, 'attachment:1:PDFB2222', 'rename', 'done')
    lib.rename(b_att, b_pdf, 'jones_2026.pdf')
    imports.record_step(lib.settings, sid1, 'attachment:1:PDFB2222', 'tag', 'done')
    imports.record_step(lib.settings, sid1, 'attachment:1:PDFC3333', 'rename', 'done')
    imports.record_step(lib.settings, sid1, 'attachment:1:PDFC3333', 'tag', 'failed',
                        detail={'error': 'provider unreachable'})
    for step in ('rename', 'tag'):
        imports.record_step(lib.settings, sid1, 'attachment:1:PDFA2222', step, 'skipped')

    # Better BibTeX regenerates C's citation key between runs.
    lib.change_citation_key(c, 'leeRenamed2026')

    _, second = lib.run()
    assert _all_new(second) == [], 'nothing is re-registered as new'
    assert second['renamed'] == [{'id': 'attachment:1:PDFB2222',
                                  'from': 'Z_attachments/biblio_articles/Jones et al - 2026 - Full text.pdf',
                                  'to': 'Z_attachments/biblio_articles/jones_2026.pdf'}]
    # Only C stays open, only for the step that failed, with its first category.
    assert second['open'] == [{
        'id': 'attachment:1:PDFC3333', 'category': 'new_entry_with_pdf', 'steps': ['tag'],
        'first_seen_session': sid1, 'rel': 'Z_attachments/biblio_articles/lee_2026.pdf',
        'citation_key': 'leeRenamed2026'}]
    assert second['gone'] == [] and second['adopted'] == []

    imports.record_step(lib.settings, second['session'], 'attachment:1:PDFC3333', 'tag', 'done')
    _, third = lib.run()
    assert third['open'] == [] and _all_new(third) == []
    # The history names both sessions for C, in order.
    steps = [(e['session'], e['status']) for e in imports.history(lib.settings)
             if e['unit'] == 'attachment:1:PDFC3333' and e.get('event') == 'step' and e['step'] == 'tag']
    assert [s for _, s in steps] == ['failed', 'done']


def test_a_failed_step_never_reopens_a_succeeded_one(lib):
    lib.run()
    d = lib.entry('DDDD4444', 'kimNew2026')
    lib.pdf(d, 'PDFD4444', 'kim_2026.pdf')
    sid, _ = lib.run()
    imports.record_step(lib.settings, sid, 'attachment:1:PDFD4444', 'rename', 'done')
    imports.record_step(lib.settings, sid, 'attachment:1:PDFD4444', 'tag', 'uncertain')
    _, report = lib.run()
    (row,) = report['open']
    assert row['steps'] == ['tag']  # uncertain stays open; rename stays closed


@requires_birthtime
def test_an_orphan_that_gets_a_zotero_record_is_adopted_not_doubled(lib):
    lib.run()
    stray = lib.orphan('new_orphan_2026.pdf')
    sid, report = lib.run()
    (doc_id,) = report['new']['pdf_not_in_zotero']
    imports.record_step(lib.settings, sid, doc_id, 'associate', 'failed')

    # The maintainer creates the Zotero record and links this very file.
    e = lib.entry('EEEE5555', 'parkNew2026')
    iid = lib._id()
    with sqlite3.connect(lib.db) as con:
        con.execute("INSERT INTO items VALUES (?,?,2,'d','d',1,1)", (iid, 'PDFE5555'))
        con.execute('INSERT INTO itemAttachments VALUES (?,?,?,?,2)',
                    (iid, e, str(stray), 'application/pdf'))
    _, report = lib.run()
    assert report['adopted'] == [{'id': 'attachment:1:PDFE5555', 'from': doc_id}]
    assert report['new']['pdf_not_in_zotero'] == []
    tracked = imports.units(lib.settings)
    assert tracked[doc_id]['state'] == 'absorbed'
    assert tracked[doc_id]['steps']['associate']['status'] == 'done'
    assert {r['id'] for r in report['open']} == {'attachment:1:PDFE5555'}


def test_a_baseline_orphan_that_gets_a_record_becomes_work(lib):
    """The file predates activation, the Zotero record does not."""
    lib.run()
    stray = lib.biblio / 'stray_2010.pdf'
    e = lib.entry('FFFF6666', 'oldStray2010')
    iid = lib._id()
    with sqlite3.connect(lib.db) as con:
        con.execute("INSERT INTO items VALUES (?,?,2,'d','d',1,1)", (iid, 'PDFF6666'))
        con.execute('INSERT INTO itemAttachments VALUES (?,?,?,?,2)',
                    (iid, e, str(stray), 'application/pdf'))
    _, report = lib.run()
    assert report['new']['new_entry_with_pdf'] == ['attachment:1:PDFF6666', 'entry:1:FFFF6666']
    assert {r['id'] for r in report['open']} == {'attachment:1:PDFF6666'}


@requires_birthtime
def test_an_attachment_recreated_under_a_new_key_keeps_its_history(lib):
    lib.run()
    g = lib.entry('GGGG7777', 'wuNew2026')
    att, path = lib.pdf(g, 'PDFG7777', 'wu_2026.pdf')
    sid, _ = lib.run()
    for step in ('rename', 'tag'):
        imports.record_step(lib.settings, sid, 'attachment:1:PDFG7777', step, 'done')
    # The link is removed and re-created (a re-point, a merge): new key, same file.
    with sqlite3.connect(lib.db) as con:
        con.execute('UPDATE items SET key=? WHERE itemID=?', ('PDFG8888', att))
    _, report = lib.run()
    assert report['adopted'] == [{'id': 'attachment:1:PDFG8888', 'from': 'attachment:1:PDFG7777'}]
    assert _all_new(report) == [] and report['open'] == []


def test_notes_new_and_modified(lib):
    lib.run()
    h = lib.entry('HHHH8888', 'nguyenNew2026')
    nid = lib.note(h, 'NOTEH888', '<p>first thoughts</p>')
    sid, report = lib.run()
    assert report['new']['new_note'] == ['note:1:NOTEH888']
    assert report['new']['new_entry_without_pdf'] == ['entry:1:HHHH8888']
    imports.record_step(lib.settings, sid, 'note:1:NOTEH888', 'mirror', 'done')
    _, report = lib.run()
    assert 'note:1:NOTEH888' not in {r['id'] for r in report['open']}

    # An edit in Zotero reopens exactly `mirror` — for a new note and for a
    # baseline one alike, since both changed after activation.
    lib.edit_note(nid, '<p>first thoughts, revised</p>')
    lib.edit_note(lib.note_a, '<p>old reading, revised</p>')
    _, report = lib.run()
    assert sorted(report['modified_notes']) == ['note:1:NOTEA111', 'note:1:NOTEH888']
    rows = {r['id']: r for r in report['open']}
    assert rows['note:1:NOTEH888']['steps'] == ['mirror']
    assert rows['note:1:NOTEA111']['steps'] == ['mirror']


def test_mirror_done_binds_to_the_digest_actually_mirrored(lib):
    lib.run()
    h = lib.entry('IIII9999', 'nNew2026')
    nid = lib.note(h, 'NOTEI999', '<p>v1</p>')
    sid, _ = lib.run()
    v1 = imports.units(lib.settings)['note:1:NOTEI999']['note_sha256']
    lib.edit_note(nid, '<p>v2</p>')  # edited while the run was mirroring v1
    imports.record_step(lib.settings, sid, 'note:1:NOTEI999', 'mirror', 'done', note_sha256=v1)
    _, report = lib.run()
    assert report['modified_notes'] == ['note:1:NOTEI999']


def test_trash_is_reported_once_and_a_restore_returns_the_unit(lib):
    lib.run()
    j = lib.entry('JJJJ0000', 'trashNew2026')
    lib.pdf(j, 'PDFJ0000', 'trash_2026.pdf')
    lib.run()
    lib.trash(j)
    _, report = lib.run()
    assert sorted(report['gone']) == ['attachment:1:PDFJ0000', 'entry:1:JJJJ0000']
    # The file is still in the vault with no live record: that is honestly a
    # PDF not in Zotero (trashing a link hands the file back to the ladder).
    (doc_id,) = report['new']['pdf_not_in_zotero']
    assert [r['id'] for r in report['open']] == [doc_id]
    _, again = lib.run()
    assert again['gone'] == [] and _all_new(again) == []
    with sqlite3.connect(lib.db) as con:
        con.execute('DELETE FROM deletedItems')
    _, back = lib.run()
    assert sorted(back['returned']) == ['attachment:1:PDFJ0000', 'entry:1:JJJJ0000']
    assert back['gone'] == [doc_id]
    assert _all_new(back) == []
    assert {r['id'] for r in back['open']} == {'attachment:1:PDFJ0000'}


def test_two_racing_passes_register_each_unit_once(lib):
    lib.run()
    k = lib.entry('KKKK1111', 'raceNew2026')
    lib.pdf(k, 'PDFK1111', 'race_2026.pdf')
    obs = imports.observe(lib.settings)
    s1, s2 = imports.begin_session(lib.settings), imports.begin_session(lib.settings)
    results = {}
    barrier = threading.Barrier(2)

    def go(sid):
        barrier.wait()
        results[sid] = imports.record_pass(lib.settings, sid, obs)

    threads = [threading.Thread(target=go, args=(s,)) for s in (s1, s2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    registered = [_all_new(r) for r in results.values()]
    assert sorted(len(r) for r in registered) == [0, 2]
    events = [e for e in imports.history(lib.settings) if e.get('event') == 'registered']
    assert sorted(e['unit'] for e in events) == ['attachment:1:PDFK1111', 'entry:1:KKKK1111']


def test_several_sessions_the_same_day_are_distinct(lib):
    s1, _ = lib.run()
    s2, _ = lib.run()
    assert s1 != s2


def test_refusals(lib):
    sid, _ = lib.run()
    with pytest.raises(imports.ImportTrackingError):
        imports.record_pass(lib.settings, 'nope', {})
    with pytest.raises(imports.ImportTrackingError):
        imports.record_step(lib.settings, sid, 'note:1:NOTEA111', 'tag', 'done')
    with pytest.raises(imports.ImportTrackingError):
        imports.record_step(lib.settings, sid, 'note:1:NOTEA111', 'mirror', 'maybe')
    with pytest.raises(imports.ImportTrackingError):
        imports.record_step(lib.settings, sid, 'attachment:1:NOPE', 'tag', 'done')


def test_a_failed_zotero_read_changes_nothing(lib, monkeypatch):
    lib.run()
    before = imports.units(lib.settings)
    from accession import reconciliation

    def broken(*_a, **_k):
        raise ValueError('Zotero database identity unavailable')

    monkeypatch.setattr(reconciliation, 'observe', broken)
    with pytest.raises(ValueError):
        imports.observe(lib.settings)
    assert imports.units(lib.settings) == before


def test_default_reconciliation_snapshot_shape_is_unchanged(lib):
    """`include_note_digests` is opt-in: `changes()` diffs every field."""
    from accession import reconciliation
    snap = reconciliation.observe(lib.settings)
    assert not any('note_sha256' in i for i in snap['items'].values())


def test_unfinished_operations_reports_each_store(lib):
    out = imports.unfinished_operations(lib.settings)
    assert set(out) == {'zotero_item_creates', 'pdf_operations', 'note_creates'}
    assert all(isinstance(v, (list, dict)) for v in out.values())


# ---------------------------------------------------------------------------
# Adversarial review of lot 2 (2026-09-21). Each test was red on the first cut.
# ---------------------------------------------------------------------------

def _link(lib, entry_iid, key, path, lm=2, ctype='application/pdf'):
    iid = lib._id()
    with sqlite3.connect(lib.db) as con:
        con.execute("INSERT INTO items VALUES (?,?,2,'d','d',1,1)", (iid, key))
        con.execute('INSERT INTO itemAttachments VALUES (?,?,?,?,?)',
                    (iid, entry_iid, str(path), ctype, lm))
    return iid


def _open_ids(report):
    return {r['id'] for r in report['open']}


@requires_birthtime
def test_an_absorbed_orphan_whose_record_is_trashed_is_work_again(lib):
    lib.run()
    stray = lib.orphan('new_orphan_2026.pdf')
    _, r = lib.run()
    (doc_id,) = r['new']['pdf_not_in_zotero']
    e = lib.entry('EEEE5555', 'parkNew2026')
    _link(lib, e, 'PDFE5555', stray)
    lib.run()
    lib.trash(e)
    _, r = lib.run()
    assert 'attachment:1:PDFE5555' in r['gone']
    assert doc_id in r['returned'] and doc_id in _open_ids(r)
    assert imports.units(lib.settings)[doc_id]['steps']['associate']['status'] == 'reopened'


@requires_birthtime
def test_a_new_record_on_a_gone_baseline_files_path_is_new_work(lib):
    lib.run()
    lib.trash(lib.entry_a)
    _, r = lib.run()
    (doc_id,) = r['new']['pdf_not_in_zotero']
    e = lib.entry('ZZZZ9999', 'smithFresh2019')
    _link(lib, e, 'PDFZ9999', lib.biblio / 'smith_2019.pdf')
    _, r = lib.run()
    assert 'attachment:1:PDFZ9999' in _open_ids(r)
    assert imports.units(lib.settings)[doc_id]['state'] == 'absorbed'


def test_a_different_record_never_inherits_another_records_steps(lib):
    lib.run()
    j = lib.entry('JJJJ0000', 'oldNew2026')
    lib.pdf(j, 'PDFJ0000', 'j_2026.pdf')
    sid, _ = lib.run()
    for s in ('rename', 'tag'):
        imports.record_step(lib.settings, sid, 'attachment:1:PDFJ0000', s, 'done')
    lib.trash(j)
    lib.run()
    k = lib.entry('KKKK0000', 'otherNew2026')
    _link(lib, k, 'PDFK0000', lib.biblio / 'j_2026.pdf')
    _, r = lib.run()
    assert 'attachment:1:PDFK0000' in _open_ids(r)


def test_an_entry_tagged_through_its_pdf_is_not_tagged_again_when_the_pdf_goes(lib):
    lib.run()
    b = lib.entry('BBBB2222', 'jonesNew2026')
    att, _ = lib.pdf(b, 'PDFB2222', 'jones_2026.pdf')
    sid, _ = lib.run()
    imports.record_step(lib.settings, sid, 'attachment:1:PDFB2222', 'rename', 'done')
    # The orchestrator's real receipt: the Zotero (parent) target was written.
    imports.record_step(lib.settings, sid, 'attachment:1:PDFB2222', 'tag', 'done',
                        detail={'targets': {'finder': 'done', 'zotero': 'done'}, 'tags': ['dep']})
    lib.trash(att)
    _, r = lib.run()
    assert 'entry:1:BBBB2222' not in _open_ids(r)


def test_a_finder_only_tag_does_not_credit_the_parent_entry(lib):
    """Audit remediation 2026-09-23 (maintainer decision): the parent entry
    is credited only when the ZOTERO target was written. A Finder-only tag
    tags the file, not the entry, so the entry still owes its tagging once
    the PDF goes. Red before: any terminal attachment tag credited it."""
    lib.run()
    b = lib.entry('BBBB3333', 'finderOnly2026')
    att, _ = lib.pdf(b, 'PDFB3333', 'finder_2026.pdf')
    sid, _ = lib.run()
    imports.record_step(lib.settings, sid, 'attachment:1:PDFB3333', 'rename', 'done')
    imports.record_step(lib.settings, sid, 'attachment:1:PDFB3333', 'tag', 'done',
                        detail={'targets': {'finder': 'done'}, 'tags': ['dep']})
    assert 'tag' not in (imports.unit(lib.settings, 'entry:1:BBBB3333').get('steps') or {})
    lib.trash(att)
    _, r = lib.run()
    assert 'entry:1:BBBB3333' in _open_ids(r)


@requires_birthtime
def test_rekeying_back_keeps_the_owed_step_on_the_attachment(lib):
    lib.run()
    g = lib.entry('GGGG7777', 'wuNew2026')
    att, _ = lib.pdf(g, 'PDFG7777', 'wu_2026.pdf')
    sid, _ = lib.run()
    imports.record_step(lib.settings, sid, 'attachment:1:PDFG7777', 'rename', 'done')
    imports.record_step(lib.settings, sid, 'attachment:1:PDFG7777', 'tag', 'failed')
    for key in ('PDFG8888', 'PDFG7777'):
        with sqlite3.connect(lib.db) as con:
            con.execute('UPDATE items SET key=? WHERE itemID=?', (key, att))
        _, r = lib.run()
        assert [(x['id'], x['steps']) for x in r['open']] == [(f'attachment:1:{key}', ['tag'])]


@requires_birthtime
def test_a_repointed_attachment_absorbs_the_orphan_it_now_links(lib):
    lib.run()
    b = lib.entry('BBBB2222', 'jonesNew2026')
    att, path = lib.pdf(b, 'PDFB2222', 'jones_2026.pdf')
    lib.run()
    path.unlink()
    stray = lib.orphan('jones_2026_found.pdf')
    _, r = lib.run()
    (doc_id,) = r['new']['pdf_not_in_zotero']
    with sqlite3.connect(lib.db) as con:
        con.execute('UPDATE itemAttachments SET path=? WHERE itemID=?', (str(stray), att))
    _, r = lib.run()
    assert doc_id not in r['gone']
    assert imports.units(lib.settings)[doc_id]['state'] == 'absorbed'
    assert r['adopted'] == [{'id': 'attachment:1:PDFB2222', 'from': doc_id}]


def test_a_relative_link_is_not_also_an_orphan(lib):
    lib.run()
    e = lib.entry('RRRR1111', 'relNew2026')
    (lib.biblio / 'rel_2026.pdf').write_bytes(b'%PDF rel')
    _link(lib, e, 'PDFR1111', 'attachments:Z_attachments/biblio_articles/rel_2026.pdf')
    _, r = lib.run()
    assert r['new']['pdf_not_in_zotero'] == []
    # Nothing the renamer can name, but the entry's Zotero tags are still owed.
    assert [x['steps'] for x in r['open'] if x['id'] == 'attachment:1:PDFR1111'] == [['tag']]


def test_an_older_observation_is_refused_after_a_newer_one(lib):
    lib.run()
    stale = imports.observe(lib.settings)
    k = lib.entry('KKKK1111', 'raceNew2026')
    lib.pdf(k, 'PDFK1111', 'race_2026.pdf')
    fresh = imports.observe(lib.settings)
    s1, s2 = imports.begin_session(lib.settings), imports.begin_session(lib.settings)
    imports.record_pass(lib.settings, s2, fresh)
    before = imports.units(lib.settings)
    with pytest.raises(imports.StaleObservation):
        imports.record_pass(lib.settings, s1, stale)
    assert imports.units(lib.settings) == before


def test_a_safe_saved_orphan_is_not_a_new_import(lib):
    lib.run()
    p = lib.biblio / 'stray_2010.pdf'
    tmp = p.with_name('.stray.tmp')
    tmp.write_bytes(p.read_bytes() + b' annotated')
    os.replace(tmp, p)
    _, r = lib.run()
    assert r['new']['pdf_not_in_zotero'] == [] and r['open'] == []


@requires_birthtime
def test_an_attanger_move_is_settled_into_one_unit(lib, tmp_path):
    lib.run()
    storage = tmp_path / 'storage' / 'PDFM1111'
    storage.mkdir(parents=True)
    (storage / 'moved.pdf').write_bytes(b'%PDF m')
    m = lib.entry('MMMM1111', 'moveNew2026')
    att = _link(lib, m, 'PDFM1111', 'storage:moved.pdf', lm=1)
    lib.run()
    # Attanger has moved (and renamed) the bytes; Zotero's DB still says storage:.
    moved = lib.biblio / 'moved_2026.pdf'
    os.rename(storage / 'moved.pdf', moved)
    _, r = lib.run()
    assert r['settling'] == ['attachment:1:PDFM1111']
    transient = r['new']['pdf_not_in_zotero']
    # Zotero records the move: same attachment key, linked file, new path.
    with sqlite3.connect(lib.db) as con:
        con.execute('UPDATE itemAttachments SET path=?, linkMode=2 WHERE itemID=?', (str(moved), att))
    _, r = lib.run()
    assert r['settling'] == []
    assert [a['from'] for a in r['adopted']] == transient
    assert {x['id'] for x in r['open']} == {'attachment:1:PDFM1111'}


@requires_birthtime
def test_one_prior_is_adopted_once(lib):
    lib.run()
    g = lib.entry('GGGG7777', 'wuNew2026')
    att, path = lib.pdf(g, 'PDFG7777', 'wu_2026.pdf')
    lib.run()
    lib.trash(att)
    _link(lib, g, 'PDFX0001', path)
    _link(lib, g, 'PDFX0002', path)
    _, r = lib.run()
    froms = [a['from'] for a in r['adopted']]
    assert froms == ['attachment:1:PDFG7777']


@requires_birthtime
def test_a_step_on_an_absorbed_unit_is_refused_and_names_its_successor(lib):
    lib.run()
    g = lib.entry('GGGG7777', 'wuNew2026')
    att, _ = lib.pdf(g, 'PDFG7777', 'wu_2026.pdf')
    sid, _ = lib.run()
    with sqlite3.connect(lib.db) as con:
        con.execute('UPDATE items SET key=? WHERE itemID=?', ('PDFG8888', att))
    lib.run()
    with pytest.raises(imports.ImportTrackingError, match='PDFG8888'):
        imports.record_step(lib.settings, sid, 'attachment:1:PDFG7777', 'tag', 'done')


def test_a_standalone_pdf_is_neither_work_nor_an_orphan(lib):
    lib.run()
    f = lib.biblio / 'standalone.pdf'
    f.write_bytes(b'%PDF s')
    iid = lib._id()
    with sqlite3.connect(lib.db) as con:
        con.execute("INSERT INTO items VALUES (?,?,2,'d','d',1,1)", (iid, 'STANDALN'))
        con.execute('INSERT INTO itemAttachments VALUES (?,NULL,?,?,2)',
                    (iid, str(f), 'application/pdf'))
    _, r = lib.run()
    assert _all_new(r) == []


def test_modified_note_is_reported_once_per_change(lib):
    lib.run()
    lib.edit_note(lib.note_a, '<p>revised</p>')
    _, r = lib.run()
    assert r['modified_notes'] == ['note:1:NOTEA111']
    _, r = lib.run()
    assert r['modified_notes'] == []
    assert 'note:1:NOTEA111' in _open_ids(r)  # still owed until mirrored


# ---------------------------------------------------------------------------
# Lot 5: a rename THIS app made, before Zotero's database records the repath.
# ---------------------------------------------------------------------------

@requires_birthtime
def test_a_file_renamed_before_zotero_catches_up_stays_its_attachments(lib):
    lib.run()
    n = lib.entry('NNNN0001', 'nashNew2026')
    _att, path = lib.pdf(n, 'PDFN0001', 'Nash - 2026.pdf')
    sid, _ = lib.run()
    os.rename(path, path.with_name('nash_2026.pdf'))     # Zotero NOT updated
    _, r = lib.run()
    assert _all_new(r) == []                             # not an orphan
    assert r['renamed'] == [{'id': 'attachment:1:PDFN0001',
                             'from': 'Z_attachments/biblio_articles/Nash - 2026.pdf',
                             'to': 'Z_attachments/biblio_articles/nash_2026.pdf'}]
    unit = imports.unit(lib.settings, 'attachment:1:PDFN0001')
    assert unit['doc'] and unit['rel'].endswith('nash_2026.pdf')


@requires_birthtime
def test_note_rename_accepts_only_the_units_own_file(lib):
    lib.run()
    n = lib.entry('NNNN0002', 'nashNew2027')
    _att, path = lib.pdf(n, 'PDFN0002', 'x.pdf')
    sid, _ = lib.run()
    new = path.with_name('nash_2027.pdf')
    os.rename(path, new)
    other = lib.orphan('someone_else.pdf')
    with pytest.raises(imports.ImportTrackingError):
        imports.note_rename(lib.settings, sid, 'attachment:1:PDFN0002',
                            str(other.relative_to(lib.vault)))
    unit = imports.note_rename(lib.settings, sid, 'attachment:1:PDFN0002',
                               str(new.relative_to(lib.vault)))
    assert unit['rel'].endswith('nash_2027.pdf')


# ---------------------------------------------------------------------------
# Adversarial review of lot 5's tracker changes (2026-09-21). The note_rename,
# shared-file and in_biblio tests were red on ea8fa002.
# ---------------------------------------------------------------------------

def untrash(lib, iid):
    with sqlite3.connect(lib.db) as con:
        con.execute('DELETE FROM deletedItems WHERE itemID=?', (iid,))


def repoint(lib, iid, path):
    with sqlite3.connect(lib.db) as con:
        con.execute('UPDATE itemAttachments SET path=? WHERE itemID=?', (str(path), iid))


def live(lib):
    return {k: v for k, v in imports.units(lib.settings).items()
            if v.get('state', 'present') == 'present'}


# 1. Attachment trashed (file becomes an orphan, registered), then restored
#    while the app renamed the file; Zotero later records the path. One file
#    must end as ONE live unit (the orphan absorbed).
def test_restored_attachment_with_stale_path_absorbs_orphan(lib):
    lib.run()
    n = lib.entry('NNNN0001', 'nashNew2026')
    att, path = lib.pdf(n, 'PDFN0001', 'nash.pdf')
    lib.run()
    lib.trash(att)
    _, r = lib.run()
    (orphan,) = r['new']['pdf_not_in_zotero']
    untrash(lib, att)
    new = path.with_name('nash_2026.pdf')
    os.rename(path, new)                  # Zotero still names nash.pdf
    lib.run()
    repoint(lib, att, new)                # Zotero catches up
    _, r = lib.run()
    lv = live(lib)
    assert orphan not in lv, (orphan, lv.get(orphan), r['adopted'])


# 1b. Same, without any rename (plain trash + restore).
def test_restored_attachment_absorbs_orphan_no_rename(lib):
    lib.run()
    n = lib.entry('NNNN0001', 'nashNew2026')
    att, path = lib.pdf(n, 'PDFN0001', 'nash.pdf')
    lib.run()
    lib.trash(att)
    _, r = lib.run()
    (orphan,) = r['new']['pdf_not_in_zotero']
    untrash(lib, att)
    _, r = lib.run()
    assert orphan not in live(lib)


# 2. Two attachments (same parent) linking one file; the file is renamed and
#    Zotero lags. Both units must follow the file.
@requires_birthtime
def test_two_attachments_sharing_a_doc_both_follow_rename(lib):
    lib.run()
    n = lib.entry('NNNN0001', 'nashNew2026')
    a1, path = lib.pdf(n, 'PDFN0001', 'nash.pdf')
    _link(lib, n, 'PDFN0002', path)
    lib.run()
    os.rename(path, path.with_name('nash_2026.pdf'))
    _, r = lib.run()
    u = imports.units(lib.settings)
    rels = {k: u[k]['rel'] for k in ('attachment:1:PDFN0001', 'attachment:1:PDFN0002')}
    assert all(v.endswith('nash_2026.pdf') for v in rels.values()), rels


# 3. File moved from outside biblio into biblio; Zotero lags then catches up.
#    Once the file is in biblio, the attachment should owe `rename`.
def test_in_biblio_follows_move_into_biblio(lib):
    lib.run()
    other = lib.vault / 'elsewhere'
    other.mkdir()
    f = other / 'Paper Name.pdf'
    f.write_bytes(b'%PDF elsewhere')
    n = lib.entry('NNNN0003', 'ohNew2026')
    att = _link(lib, n, 'PDFN0003', f)
    _, r = lib.run()
    dst = lib.biblio / 'Paper Name.pdf'
    os.rename(f, dst)
    lib.run()
    repoint(lib, att, dst)
    _, r = lib.run()
    row = [x for x in r['open'] if x['id'] == 'attachment:1:PDFN0003'][0]
    assert 'rename' in row['steps'], row


# 4. note_rename accepts a path OUTSIDE the vault (hard link to the same inode).
def test_note_rename_refuses_path_outside_vault(lib, tmp_path):
    lib.run()
    n = lib.entry('NNNN0004', 'x2026')
    _att, path = lib.pdf(n, 'PDFN0004', 'x.pdf')
    sid, _ = lib.run()
    outside = tmp_path / 'outside.pdf'
    os.link(path, outside)
    rel = os.path.relpath(outside, lib.vault)   # '../outside.pdf'
    with pytest.raises(imports.ImportTrackingError):
        imports.note_rename(lib.settings, sid, 'attachment:1:PDFN0004', rel)


# 4b. '..' traversal that lands back inside the vault stores a non-canonical
#     rel, and the next pass reports a spurious rename.
@requires_birthtime
def test_note_rename_canonicalises(lib):
    lib.run()
    n = lib.entry('NNNN0005', 'y2026')
    att, path = lib.pdf(n, 'PDFN0005', 'y.pdf')
    sid, _ = lib.run()
    lib.rename(att, path, 'y_2026.pdf')
    weird = 'Z_attachments/../Z_attachments/biblio_articles/y_2026.pdf'
    u = imports.note_rename(lib.settings, sid, 'attachment:1:PDFN0005', weird)
    _, r = lib.run()
    assert r['renamed'] == [], (u['rel'], r['renamed'])


# 4c. note_rename on an absorbed unit silently updates the dead identity.
@requires_birthtime
def test_note_rename_refuses_absorbed_unit(lib):
    lib.run()
    g = lib.entry('GGGG7777', 'wuNew2026')
    att, path = lib.pdf(g, 'PDFG7777', 'wu.pdf')
    sid, _ = lib.run()
    with sqlite3.connect(lib.db) as con:
        con.execute('UPDATE items SET key=? WHERE itemID=?', ('PDFG8888', att))
    lib.run()
    assert imports.unit(lib.settings, 'attachment:1:PDFG7777')['state'] == 'absorbed'
    os.rename(path, path.with_name('wu_2026.pdf'))
    with pytest.raises(imports.ImportTrackingError):
        imports.note_rename(lib.settings, sid, 'attachment:1:PDFG7777',
                            'Z_attachments/biblio_articles/wu_2026.pdf')


# 5. note_rename then Zotero stale for a pass then catches up: no churn.
@requires_birthtime
def test_note_rename_then_catch_up_is_quiet(lib):
    lib.run()
    n = lib.entry('NNNN0006', 'z2026')
    att, path = lib.pdf(n, 'PDFN0006', 'z.pdf')
    sid, _ = lib.run()
    new = path.with_name('z_2026.pdf')
    os.rename(path, new)
    imports.note_rename(lib.settings, sid, 'attachment:1:PDFN0006',
                        str(new.relative_to(lib.vault)))
    _, r1 = lib.run()
    repoint(lib, att, new)
    _, r2 = lib.run()
    for r in (r1, r2):
        assert r['renamed'] == [] and r['gone'] == [] and _all_new(r) == [] \
            and r['adopted'] == [], r


# 6. File deleted (not renamed): the attachment should not keep owing
#    `rename` on a file that does not exist.
def test_deleted_linked_file_does_not_owe_rename(lib):
    lib.run()
    n = lib.entry('NNNN0007', 'd2026')
    att, path = lib.pdf(n, 'PDFN0007', 'd.pdf')
    lib.run()
    path.unlink()
    _, r = lib.run()
    row = [x for x in r['open'] if x['id'] == 'attachment:1:PDFN0007'][0]
    assert 'rename' not in row['steps'], row


# 7. Missing-linked-file attachment: renamed file lands as an orphan while a
#    DIFFERENT new attachment (other parent) links it correctly. The old one
#    keeps doc D; the new one is new work. Fine, but check the old unit's rel
#    doesn't get stolen.
def test_rename_then_file_linked_to_new_record(lib):
    lib.run()
    n = lib.entry('NNNN0008', 'e2026')
    att, path = lib.pdf(n, 'PDFN0008', 'e.pdf')
    lib.run()
    new = path.with_name('e_2026.pdf')
    os.rename(path, new)
    m = lib.entry('MMMM0008', 'f2026')
    _link(lib, m, 'PDFM0008', new)
    _, r = lib.run()
    # observation-only sanity
    assert r['new']['pdf_not_in_zotero'] == []


def test_missing_birth_time_does_not_adopt_or_credit_prior_history(lib, monkeypatch):
    monkeypatch.setattr(imports, '_doc_id', lambda fp: f"doc:{fp['ino']}:None")
    lib.run()
    stray = lib.orphan('birthless.pdf')
    sid, report = lib.run()
    (doc_id,) = report['new']['pdf_not_in_zotero']
    imports.record_step(lib.settings, sid, doc_id, 'associate', 'failed')
    parent = lib.entry('EEEE5555', 'parkNew2026')
    iid = lib._id()
    with sqlite3.connect(lib.db) as con:
        con.execute("INSERT INTO items VALUES (?,?,2,'d','d',1,1)", (iid, 'PDFE5555'))
        con.execute('INSERT INTO itemAttachments VALUES (?,?,?,?,2)',
                    (iid, parent, str(stray), 'application/pdf'))
    _, report = lib.run()
    assert report['adopted'] == []
    tracked = imports.units(lib.settings)
    assert tracked[doc_id]['state'] != 'absorbed'
    assert tracked[doc_id]['steps']['associate']['status'] == 'failed'
    assert 'associate' not in tracked['attachment:1:PDFE5555'].get('steps', {})
