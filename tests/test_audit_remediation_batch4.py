"""Audit remediation 2026-09-23, Batch 4 — journal & note-sync safety.

* N1/N2  `apply-pdf-links` keeps the undo of notes already written when a
         later note fails, and merges (never prunes) after a lost lock;
* N5     a PDF replaced since it was observed is a 409, not a 500;
* N8     a note that became a symlink is refused by the refresh, pull and
         migrate writers;
* N11    an unreadable notes folder refuses the plan;
* N12    note creation beats its lock and stops when it is lost;
* N9     the link walk stops at its budget and says so;
* P12    the base-store lock is a bounded wait.

Each test was proven red on a7a171d9.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_HEADERS = {"X-Requested-With": "ChatEKLD"}


def _app():
    import flask
    from api.routes.notesync import notesync_bp
    from api.routes.tagging import tagging_bp
    app = flask.Flask(__name__)
    app.register_blueprint(tagging_bp)
    app.register_blueprint(notesync_bp)
    return app


class _Vault(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="remediation-b4-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.notes = self.vault / "Z_Zotero_Notes"
        self.notes.mkdir(parents=True)
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True)
        self.cfg = {"tagging_archive_dir": str(self.tmp / "tag-archive"),
                    "refactor_archive_dir": str(self.tmp / "refactor-archive")}
        self.client = _app().test_client()


# ---------------------------------------------------------------------------
# N1/N2 — apply-pdf-links
# ---------------------------------------------------------------------------

class TestApplyPdfLinksJournal(_Vault):
    KEYS = [f"k{i}2020" for i in range(5)]

    def _inv(self):
        from audit.core.obsidian import NoteInfo
        from audit.engine.inventory import Record
        recs = {}
        for key in self.KEYS:
            note = self.notes / f"{key}.md"
            note.write_text("---\ntags: []\n---\nbody\n", encoding="utf-8")
            pdf = self.biblio / f"{key}.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            rec = Record(citation_key=key)
            rec.obsidian_note = NoteInfo(path=note, tags=[], has_frontmatter=True)
            rec.pdf_paths = [pdf]
            recs[key] = rec
        inv = mock.MagicMock()
        inv.records = recs
        return inv, SimpleNamespace(vault_root=self.vault, biblio_articles_dir=self.biblio)

    def _post(self, *extra):
        from audit.manager import audit_manager
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch("api.routes.tagging.load_config", return_value=self.cfg))
            st.enter_context(mock.patch("tagging.writers.load_config", return_value=self.cfg))
            st.enter_context(mock.patch.object(audit_manager, "get_inventory",
                                               return_value=self._inv()))
            for p in extra:
                st.enter_context(p)
            return self.client.post("/api/tagging/apply-pdf-links", headers=_HEADERS,
                                    json={"citation_keys": self.KEYS, "confirm": True})

    def _ops(self):
        from tagging import journal
        return {op["citation_key"]: op for op in journal.load(self.vault, self.cfg)["ops"]}

    def test_a_snapshot_failure_fails_one_note_and_the_rest_keep_their_undo(self):
        from tagging import journal
        real = journal.write_note_snapshot
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError(28, "No space left on device")
            return real(*a, **k)
        r = self._post(mock.patch.object(journal, "write_note_snapshot", side_effect=flaky))
        body = r.get_json()
        self.assertEqual(r.status_code, 200, body)
        self.assertEqual(body["results"]["k22020"]["reason"], "snapshot_failed")
        self.assertEqual(body["written"], 4)
        ops = self._ops()
        self.assertEqual(sorted(ops), ["k02020", "k12020", "k32020", "k42020"])
        self.assertTrue(journal.revert_op(self.vault, self.cfg, ops["k02020"])["ok"])

    def test_an_error_after_some_writes_still_saves_their_undo(self):
        from api.routes import _audit_tagwrite
        real = _audit_tagwrite.set_pdf_links
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("unexpected")
            return real(*a, **k)
        r = self._post(mock.patch.object(_audit_tagwrite, "set_pdf_links", side_effect=boom))
        body = r.get_json()
        self.assertEqual(r.status_code, 500, body)
        self.assertEqual(body["written"], 2)
        self.assertEqual(sorted(self._ops()), ["k02020", "k12020"])

    def test_a_refused_heartbeat_merges_without_pruning(self):
        from rag.vault import obsidian_manager
        from tagging import journal
        with mock.patch.object(journal, "prune") as prune, \
                mock.patch.object(journal, "save_merged", wraps=journal.save_merged) as merged:
            r = self._post(mock.patch("api.routes.tagging._PDF_LINK_FLUSH_EVERY", 2),
                           mock.patch.object(obsidian_manager, "heartbeat", return_value=False))
        body = r.get_json()
        self.assertEqual(body["aborted"], "lock_lost")
        prune.assert_not_called()
        merged.assert_called_once()
        self.assertEqual(sorted(self._ops()), ["k02020", "k12020"])


# ---------------------------------------------------------------------------
# N5 — a replaced PDF
# ---------------------------------------------------------------------------

class TestReplacedPdf(_Vault):
    def test_only_replaced_pdfs_is_a_409_not_a_500(self):
        from tagging.writers import _finder_write_locked
        pdf = self.biblio / "a.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        rec = SimpleNamespace(pdf_paths=[pdf])
        res, status = _finder_write_locked(
            rec=rec, valid=["dep"], rejected=[], cfg=self.cfg, vault_root=self.vault,
            biblio_dir=self.biblio, citation_key="a", expect_doc="doc:1:1")
        self.assertEqual(status, 409, res)
        self.assertEqual(res["reason"], "replaced")


# ---------------------------------------------------------------------------
# N8 — a note that became a symlink
# ---------------------------------------------------------------------------

class TestSymlinkedNote(_Vault):
    def _links(self):
        target = self.tmp / "elsewhere.md"
        target.write_text("same bytes\n", encoding="utf-8")
        path = self.notes / "k2020.md"
        path.symlink_to(target)
        return path, target

    def test_the_refresh_refuses_a_symlinked_note(self):
        from api.routes import notesync
        from rag.vault import obsidian_manager
        path, target = self._links()
        plan = {"planned": [{"key": "k2020", "path": path, "raw": "same bytes\n",
                             "proposed": "NEW\n", "region": "", "row": {}}],
                "keep_base": [], "stale_bases": [], "notes_dir": self.notes}
        with mock.patch.object(obsidian_manager, "try_acquire_lock", return_value=1), \
                mock.patch.object(obsidian_manager, "release_lock"):
            done = notesync._refresh_write(self.vault, plan)
        self.assertEqual(done["failed"], {"k2020": "became_symlink"})
        self.assertTrue(path.is_symlink() and target.read_text() == "same bytes\n")

    def test_the_pull_writer_refuses_a_symlinked_note(self):
        from api.routes import notesync
        from rag.vault import obsidian_manager
        path, _target = self._links()
        with mock.patch.object(obsidian_manager, "try_acquire_lock", return_value=1), \
                mock.patch.object(obsidian_manager, "release_lock"):
            res = notesync._region_writes(self.vault, self.cfg, [
                {"key": "k2020", "path": path, "raw": "same bytes\n", "proposed": "NEW\n"}],
                "notesync_pull")
        self.assertEqual(res["k2020"]["reason"], "became_symlink")
        self.assertTrue(path.is_symlink())


class TestSymlinkedNoteMigrate(_Vault):
    def test_the_layout_migration_refuses_a_symlinked_note(self):
        from audit.manager import audit_manager
        from rag.vault import obsidian_manager
        raw = "---\ntags: [dep]\n---\n\n**T**\n\n>k\n\n---\n\nreading note text\n"
        # Inside the notes folder: a target outside it is already refused by
        # the PLAN (`outside_notes_dir`); this reaches the writer.
        target = self.notes / "real_target.md"
        target.write_text(raw, encoding="utf-8")
        path = self.notes / "k2020.md"
        path.symlink_to(target)
        item = SimpleNamespace(item_key="ABCD1234",
                               child_notes=[SimpleNamespace(note_key="WXYZ5678")])
        rec = SimpleNamespace(citation_key="k2020", obsidian_note=SimpleNamespace(path=path),
                              zotero_item=item)
        inv = mock.MagicMock()
        inv.records = {"k2020": rec}
        settings = SimpleNamespace(vault_root=self.vault, zotero_notes_dir=self.notes)
        with mock.patch.object(audit_manager, "get_inventory", return_value=(inv, settings)), \
                mock.patch("api.routes.notesync.load_settings"), \
                mock.patch("api.routes.notesync._git_undo_check", return_value=(True, "", "0" * 40)), \
                mock.patch.object(obsidian_manager, "try_acquire_lock", return_value=1), \
                mock.patch.object(obsidian_manager, "heartbeat", return_value=True), \
                mock.patch.object(obsidian_manager, "release_lock"):
            r = self.client.post("/api/notesync/migrate", json={"confirm": True}, headers=_HEADERS)
        body = r.get_json()
        self.assertEqual(body["failed"], {"k2020": "became_symlink"}, body)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), raw)


# ---------------------------------------------------------------------------
# N11 — an unreadable notes folder
# ---------------------------------------------------------------------------

class TestUnreadableNotesFolder(_Vault):
    def test_it_raises_instead_of_answering_empty(self):
        from api.routes import notesync
        with mock.patch("api.routes.notesync.os.listdir", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(notesync.NotesFolderUnreadable):
                notesync._note_dir_names(self.notes)
        self.assertEqual(notesync._note_dir_names(self.tmp / "absent"), frozenset())

    def test_a_route_that_plans_over_it_answers_503(self):
        from audit.manager import audit_manager
        inv = mock.MagicMock()
        inv.records = {}
        settings = SimpleNamespace(vault_root=self.vault, zotero_notes_dir=self.notes)
        with mock.patch.object(audit_manager, "get_inventory", return_value=(inv, settings)), \
                mock.patch("api.routes.notesync.load_config", return_value=self.cfg), \
                mock.patch("api.routes.notesync.os.listdir", side_effect=PermissionError(13, "denied")):
            r = self.client.get("/api/notesync/status", headers=_HEADERS)
        self.assertEqual(r.status_code, 503, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["reason"], "notes_folder_unreadable")


# ---------------------------------------------------------------------------
# N12 — note creation and its lock
# ---------------------------------------------------------------------------

class TestCreateLosesItsLock(_Vault):
    def test_a_refused_heartbeat_stops_the_creates_and_merges(self):
        from api.routes import notesync
        from rag.vault import obsidian_manager
        from vault_history import journal
        recs = {}
        fresh = {}
        for i in range(4):
            key = f"c{i}2020"
            item = SimpleNamespace(item_key=f"ITEM{i:04d}", title="T", container="J",
                                   doi="", date="2020", tags=[], child_notes=[])
            note = SimpleNamespace(note_key=f"N{i:07d}", body_html=f"<p>n{i}</p>",
                                   body_plain=f"n{i}", version=1)
            fresh[item.item_key] = [note]
            recs[key] = SimpleNamespace(citation_key=key, zotero_item=item, obsidian_note=None,
                                        obs_tags=set(), pdf_paths=[],
                                        bib_entry=SimpleNamespace(title="T", year="2020",
                                                                  authors=["A, B"], doi=""))
        inv = mock.MagicMock()
        inv.records = recs
        settings = SimpleNamespace(vault_root=self.vault, zotero_notes_dir=self.notes)
        with mock.patch("api.routes.notesync._JOURNAL_FLUSH_EVERY", 2), \
                mock.patch.object(obsidian_manager, "try_acquire_lock", return_value=1), \
                mock.patch.object(obsidian_manager, "heartbeat", return_value=False), \
                mock.patch.object(obsidian_manager, "release_lock"), \
                mock.patch.object(journal, "prune") as prune:
            res, status = notesync._create_notes(inv, settings, self.cfg, sorted(recs),
                                                 fresh=fresh)
        self.assertEqual(status, 200, res)
        self.assertEqual(res["aborted"], "lock_lost")
        self.assertEqual(res["created"], 2)
        self.assertEqual(res["not_reached"], ["c22020", "c32020"])
        prune.assert_not_called()
        kinds = [op.get("kind") for op in journal.load(self.vault, self.cfg)["ops"]]
        self.assertEqual(kinds.count("create_note"), 2)


# ---------------------------------------------------------------------------
# N9 — the link walk's budget
# ---------------------------------------------------------------------------

class TestLinkWalkBudget(_Vault):
    def test_the_walk_stops_at_its_budget_and_says_so(self):
        from api.routes import notesync
        for i in range(5):
            (self.notes / f"n{i}.md").write_text("no links\n", encoding="utf-8")
        with mock.patch("api.routes.notesync._LINK_WALK_MAX_FILES", 2):
            links, unverified = notesync._incoming_links(self.vault, {"old2020"}, set())
        self.assertEqual(links, {})
        self.assertTrue(any("budget" in u for u in unverified), unverified)


# ---------------------------------------------------------------------------
# P12 — the base-store lock is bounded
# ---------------------------------------------------------------------------

class TestBaseStoreLockIsBounded(_Vault):
    def test_a_held_lock_raises_busy_after_the_bound(self):
        from notesync import base_store
        d = base_store.store_dir(self.vault)
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(d / ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        # Another process holding it: an flock on a separate descriptor.
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with mock.patch.object(base_store, "LOCK_WAIT_S", 0.2):
                with self.assertRaises(base_store.StoreBusy):
                    with base_store.store_lock(self.vault):
                        pass
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        with base_store.store_lock(self.vault):
            pass                                   # free again: acquired at once


if __name__ == "__main__":
    unittest.main()
