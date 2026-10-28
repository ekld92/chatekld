"""Route-level contract for /api/notesync/* (Batch E, 2026-08-26).

The central rule under test: this endpoint CREATES and never rewrites. A
drifted note is the user's edited copy — in every case measured on the live
library it is the longer of the two — so a writer that regenerated it would
destroy real work. That is enforced twice (here, and again by
``vault_history.create_note`` refusing an existing path), and both are asserted.
"""

from __future__ import annotations

import contextlib

import dataclasses
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flask

from api.routes.notesync import notesync_bp

_HEADERS = {"X-Requested-With": "ChatEKLD"}


def _app():
    app = flask.Flask(__name__)
    app.register_blueprint(notesync_bp)
    return app


class TestNoteSyncRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="notesync-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.notes = self.vault / "Z_Zotero_Notes"
        self.notes.mkdir(parents=True)
        self.archive = self.tmp / "archive"
        self.client = _app().test_client()
        # Two DIFFERENT dirs, as in production: both journals name their
        # per-vault folder the same way, and the tagging journal's legacy
        # migration refuses a `manifest.json` that is the refactor journal's.
        self.cfg = {"tagging_archive_dir": str(self.tmp / "tagging_archive"),
                    "refactor_archive_dir": str(self.archive)}

    def _rec(self, key, *, notes=("reading note text",), has_note=False):
        child = [SimpleNamespace(body_plain=n) for n in notes]
        # An item key per record: `/create` reads the reading note from TODAY's
        # Zotero (keyed by item), not from the scan (2026-09-21, lot 1).
        item = SimpleNamespace(title=f"T {key}", container="J", doi="10.1/x",
                               date="2020", child_notes=child, tags=[],
                               item_key=f"I{key}")
        bib = SimpleNamespace(title=f"T {key}", year="2020",
                              authors=["Smith, A"], doi="10.1/x")
        note_obj = None
        if has_note:
            path = self.notes / f"{key}.md"
            if not path.exists():
                path.write_text("---\ntags: []\n---\nreading note text\n",
                                encoding="utf-8")
            note_obj = SimpleNamespace(path=path, tags=[])
        return SimpleNamespace(citation_key=key, bib_entry=bib,
                               zotero_item=item, obsidian_note=note_obj,
                               obs_tags=set())

    def _inv(self, records):
        inv = mock.MagicMock()
        inv.records = {r.citation_key: r for r in records}
        settings = SimpleNamespace(vault_root=self.vault,
                                   zotero_notes_dir=self.notes)
        return inv, settings

    def _ctx(self, records):
        from audit.manager import audit_manager
        return (
            mock.patch("api.routes.notesync.load_config", return_value=self.cfg),
            mock.patch("api.routes.notesync.load_settings",
                       return_value=SimpleNamespace(vault_root=self.vault)),
            mock.patch.object(audit_manager, "get_inventory",
                              return_value=self._inv(records)),
        )

    @staticmethod
    def _scan_as_fresh(records):
        """A fresh Zotero read that agrees with the scan's records — the
        default for a test that does not simulate Zotero moving on."""
        fresh = {r.zotero_item.item_key: list(r.zotero_item.child_notes)
                 for r in records
                 if getattr(r.zotero_item, "item_key", "") and r.zotero_item.child_notes}
        return mock.patch("api.routes.notesync._fresh_zotero_notes", return_value=fresh)

    def _post(self, path, body, records):
        import contextlib
        from api.routes import notesync as ns
        from rag.vault import obsidian_manager
        with contextlib.ExitStack() as st:
            for p in self._ctx(records):
                st.enter_context(p)
            if not isinstance(ns._fresh_zotero_notes, mock.Mock):
                st.enter_context(self._scan_as_fresh(records))
            # A simulated lease whose held state the lock-free probes read
            # (``_zotero_io_must_be_lock_free``); ``_lock_available = False``
            # makes every acquisition fail.
            def acquire(ttl=3600):
                if not getattr(self, '_lock_available', True):
                    return None
                self._lock_held = True
                return 1

            def release(epoch):
                self._lock_held = False
            st.enter_context(mock.patch.object(obsidian_manager, "try_acquire_lock", side_effect=acquire))
            # This fixture owns a simulated lease; renewals must match it.
            st.enter_context(mock.patch.object(obsidian_manager, "heartbeat", return_value=True))
            st.enter_context(mock.patch.object(obsidian_manager, "release_lock", side_effect=release))
            return self.client.post(path, json=body, headers=_HEADERS)

    def _get(self, path, records):
        import contextlib
        with contextlib.ExitStack() as st:
            for p in self._ctx(records):
                st.enter_context(p)
            return self.client.get(path, headers=_HEADERS)

    # --- status ---------------------------------------------------------

    def test_status_classifies_missing_ok_and_drifted(self):
        recs = [
            self._rec("missing2020", has_note=False),
            self._rec("ok2021", has_note=True),
            self._rec("nonote2022", notes=(), has_note=False),
        ]
        d = self._get("/api/notesync/status", recs).get_json()
        self.assertEqual([r["citation_key"] for r in d["missing"]],
                         ["missing2020"])
        self.assertEqual(d["drifted"], [])
        # A record with no READING note is in step by definition and must not
        # appear anywhere — otherwise 5,078 records become invented work.
        self.assertEqual(d["with_reading_note"], 2)

    def test_status_404s_without_a_scan(self):
        from audit.manager import audit_manager
        with mock.patch.object(audit_manager, "get_inventory",
                               return_value=(None, None)):
            r = self.client.get("/api/notesync/status", headers=_HEADERS)
        self.assertEqual(r.status_code, 404)

    # --- preview --------------------------------------------------------

    def test_preview_returns_the_exact_bytes_and_writes_nothing(self):
        recs = [self._rec("a2020", has_note=False)]
        before = sorted(p.name for p in self.notes.iterdir())
        d = self._post("/api/notesync/preview",
                       {"citation_key": "a2020"}, recs).get_json()
        self.assertIn("**T a2020**", d["text"])
        self.assertEqual(d["rel"], "Z_Zotero_Notes/a2020.md")
        self.assertFalse(d["exists"])
        self.assertEqual(sorted(p.name for p in self.notes.iterdir()), before)

    # --- create ---------------------------------------------------------

    def test_create_requires_confirm(self):
        recs = [self._rec("a2020", has_note=False)]
        r = self._post("/api/notesync/create",
                       {"citation_keys": ["a2020"]}, recs)
        self.assertEqual(r.status_code, 400)
        self.assertFalse((self.notes / "a2020.md").exists())

    def test_create_writes_the_missing_note(self):
        recs = [self._rec("a2020", has_note=False)]
        r = self._post("/api/notesync/create",
                       {"citation_keys": ["a2020"], "confirm": True}, recs)
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["created"], 1)
        written = (self.notes / "a2020.md").read_text(encoding="utf-8")
        self.assertIn("reading note text", written)
        self.assertTrue(written.startswith("---\ntags: ["))

    def test_create_NEVER_overwrites_an_existing_note(self):
        """The batch's central rule. A drifted note is the user's edited copy;
        regenerating it would delete their writing."""
        recs = [self._rec("a2020", has_note=True)]
        path = self.notes / "a2020.md"
        path.write_text("MY OWN EDITED CONTENT\n", encoding="utf-8")
        r = self._post("/api/notesync/create",
                       {"citation_keys": ["a2020"], "confirm": True}, recs)
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["created"], 0)
        self.assertEqual(d["results"]["a2020"]["reason"], "already_exists")
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "MY OWN EDITED CONTENT\n")

    def test_create_skips_a_record_with_no_reading_note_with_a_reason(self):
        recs = [self._rec("a2020", notes=(), has_note=False)]
        d = self._post("/api/notesync/create",
                       {"citation_keys": ["a2020"], "confirm": True},
                       recs).get_json()
        self.assertEqual(d["created"], 0)
        self.assertEqual(d["results"]["a2020"]["reason"], "no_reading_note")

    def test_create_rejects_a_key_that_could_shape_a_path(self):
        recs = [self._rec("a2020", has_note=False)]
        for bad in ("../evil", "a/b", "a\\b", "a\x00b"):
            r = self._post("/api/notesync/create",
                           {"citation_keys": [bad], "confirm": True}, recs)
            self.assertEqual(r.status_code, 400, bad)

    def test_create_503s_while_indexing_and_writes_nothing(self):
        import contextlib
        from rag.vault import obsidian_manager
        recs = [self._rec("a2020", has_note=False)]
        with contextlib.ExitStack() as st:
            for p in self._ctx(recs):
                st.enter_context(p)
            st.enter_context(self._scan_as_fresh(recs))
            st.enter_context(mock.patch.object(
                obsidian_manager, "try_acquire_lock", return_value=None))
            r = self.client.post("/api/notesync/create",
                                 json={"citation_keys": ["a2020"],
                                       "confirm": True}, headers=_HEADERS)
        self.assertEqual(r.status_code, 503)
        self.assertFalse((self.notes / "a2020.md").exists())

    def test_a_created_note_is_journalled_and_therefore_revertible(self):
        """It goes through vault_history.create_note, so Note Refactor → Restore
        covers it — this is NOT a new vault-write path."""
        from vault_history import journal
        recs = [self._rec("a2020", has_note=False)]
        self._post("/api/notesync/create",
                   {"citation_keys": ["a2020"], "confirm": True}, recs)
        man = journal.load(self.vault, self.cfg)
        kinds = [op.get("kind") for op in man.get("ops", [])]
        self.assertIn("create_note", kinds)


class TestNoteSyncBatchUndoSurvives(TestNoteSyncRoutes):
    """A batch must not evict its own undo records.

    ``refactor/journal.prune`` caps retained ops at ``_MAX_OPS`` and treats
    ``create_note`` as evictable, and ``create_note`` prunes on every call. A
    batch larger than the cap therefore deleted the undo for its own earliest
    notes while the response still returned their ``op_id`` and the confirm
    dialog promised "Reversible from Note Refactor → Restore". The tagging
    journal grew ``protect_op_ids`` for exactly this; the refactor journal had
    no equivalent.
    """

    def test_a_batch_keeps_the_undo_for_every_note_it_created(self):
        from vault_history import journal as rjournal

        n = rjournal._MAX_OPS + 40          # comfortably over the cap
        recs = [self._rec(f"k{i:04d}") for i in range(n)]
        with mock.patch("api.routes.notesync._MAX_CREATE", n + 1):
            r = self._post("/api/notesync/create",
                           {"citation_keys": [x.citation_key for x in recs],
                            "confirm": True}, recs)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["created"], n)

        promised = {v["op_id"] for v in body["results"].values() if v.get("ok")}
        self.assertEqual(len(promised), n)
        manifest = rjournal.load(self.vault, self.cfg)
        retained = {op.get("id") for op in manifest.get("ops", [])}
        missing = promised - retained
        self.assertEqual(
            missing, set(),
            f"{len(missing)} of {n} notes were reported reversible but their "
            f"undo record was evicted by the batch's own prune")

    def test_a_batch_bigger_than_the_journal_window_is_refused(self):
        """The confirm promises one-click reversibility. A batch that cannot
        survive the NEXT write cannot keep that promise, so it is refused
        rather than half-kept — `_HARMONIZE_MAX_NOTE_OPS`'s precedent."""
        from api.routes import notesync as ns
        from vault_history import journal as rjournal

        self.assertLessEqual(ns._MAX_CREATE, rjournal._MAX_OPS // 2)
        keys = [f"k{i:04d}" for i in range(ns._MAX_CREATE + 1)]
        r = self._post("/api/notesync/create",
                       {"citation_keys": keys, "confirm": True}, [])
        self.assertEqual(r.status_code, 400)
        self.assertIn("undo", r.get_json()["error"].lower())


class TestNoteSyncUpdate(TestNoteSyncRoutes):
    """The region-scoped update (2026-08-28).

    The rule the whole endpoint exists to keep: everything OUTSIDE the note's
    Zotero region — frontmatter (and therefore the tagging subsystem's tags),
    the generated header, the ``---`` rule itself — survives byte-for-byte, and
    a region carrying the user's own writing is never rewritten on one confirm.
    """

    #: A template-shaped note: frontmatter, header block, the single rule, then
    #: the Zotero region. `_rec(has_note=True)`'s fixture deliberately has no
    #: rule (it exercises the unparsed path), so the update tests build their own.
    def _write_note(self, key, region, *, tags="[keep, me]", header="**T %s**"):
        path = self.notes / f"{key}.md"
        path.write_text(
            f"---\ntags: {tags}\n---\n\n{header % key}\n\n>{key}\n\n---\n{region}",
            encoding="utf-8")
        return path

    def _rec_with(self, key, *, zotero="reading note text", region=None):
        rec = self._rec(key, notes=(zotero,), has_note=False)
        path = self._write_note(key, "\n" + (region if region is not None
                                             else zotero) + "\n")
        rec.obsidian_note = SimpleNamespace(path=path, tags=[])
        return rec

    def _preview(self, key, recs):
        return self._post("/api/notesync/update-preview",
                          {"citation_key": key}, recs)

    def _update(self, key, recs, **extra):
        d = self._preview(key, recs).get_json()
        body = {"citation_key": key, "confirm": True,
                "content_sha256": d.get("content_sha256"),
                "proposed_sha256": d.get("proposed_sha256"), **extra}
        return self._post("/api/notesync/update", body, recs), d

    # --- status buckets -------------------------------------------------

    def test_status_separates_updatable_from_conflict_from_annotated(self):
        recs = [
            # Zotero gained a sentence; the region holds nothing of the user's.
            self._rec_with("upd2020", zotero="alpha beta gamma", region="alpha beta"),
            # Zotero gained a word AND the user wrote in the region.
            self._rec_with("conf2021", zotero="alpha beta", region="alpha mycomment"),
            # The user annotated; Zotero has nothing new.
            self._rec_with("ann2022", zotero="alpha", region="alpha mycomment"),
            # In step.
            self._rec_with("ok2023", zotero="alpha", region="alpha"),
        ]
        d = self._get("/api/notesync/status", recs).get_json()
        self.assertEqual([r["citation_key"] for r in d["updatable"]], ["upd2020"])
        self.assertEqual([r["citation_key"] for r in d["conflicts"]], ["conf2021"])
        self.assertEqual(d["annotated_count"], 1)
        self.assertEqual(d["unparsed"], [])

    def test_status_lists_a_note_whose_shape_is_not_recognised(self):
        """Silence would read as 'in step'. A note with no rule (or two) can
        have no region identified, so it is named instead of assumed fine."""
        rec = self._rec_with("weird2020")
        Path(rec.obsidian_note.path).write_text(
            "---\ntags: []\n---\n\nno rule at all here\n", encoding="utf-8")
        d = self._get("/api/notesync/status", [rec]).get_json()
        self.assertEqual([r["citation_key"] for r in d["unparsed"]], ["weird2020"])
        self.assertEqual(d["updatable"], [])

    # --- preview --------------------------------------------------------

    def test_preview_writes_nothing_and_flags_the_overwrite_case(self):
        rec = self._rec_with("conf2021", zotero="alpha beta", region="alpha mine")
        before = Path(rec.obsidian_note.path).read_bytes()
        d = self._preview("conf2021", [rec]).get_json()
        self.assertTrue(d["can_update"])
        self.assertTrue(d["needs_overwrite_ack"])
        self.assertEqual(d["extra_words"], 1)
        self.assertEqual(d["missing_words"], 1)
        self.assertEqual(Path(rec.obsidian_note.path).read_bytes(), before)

    def test_preview_refuses_an_unrecognised_shape_without_erroring(self):
        rec = self._rec_with("weird2020")
        Path(rec.obsidian_note.path).write_text("no rule\n", encoding="utf-8")
        d = self._preview("weird2020", [rec]).get_json()
        self.assertFalse(d["can_update"])
        self.assertEqual(d["reason"], "unparsed")

    # --- write ----------------------------------------------------------

    def test_update_replaces_only_the_region(self):
        rec = self._rec_with("upd2020", zotero="alpha beta gamma", region="alpha beta")
        path = Path(rec.obsidian_note.path)
        before = path.read_text(encoding="utf-8")
        r, _ = self._update("upd2020", [rec])
        self.assertEqual(r.status_code, 200, r.get_json())
        after = path.read_text(encoding="utf-8")
        # Everything above and including the rule is byte-identical.
        cut = before.index("\n---\n", before.index("---\n", 3))
        self.assertEqual(after[:cut], before[:cut])
        self.assertIn("tags: [keep, me]", after)
        self.assertIn("alpha beta gamma", after)

    def test_update_requires_confirm(self):
        rec = self._rec_with("upd2020", zotero="alpha beta", region="alpha")
        path = Path(rec.obsidian_note.path)
        before = path.read_bytes()
        d = self._preview("upd2020", [rec]).get_json()
        r = self._post("/api/notesync/update",
                       {"citation_key": "upd2020",
                        "content_sha256": d["content_sha256"],
                        "proposed_sha256": d["proposed_sha256"]}, [rec])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(path.read_bytes(), before)

    def test_update_requires_both_hashes(self):
        rec = self._rec_with("upd2020", zotero="alpha beta", region="alpha")
        r = self._post("/api/notesync/update",
                       {"citation_key": "upd2020", "confirm": True}, [rec])
        self.assertEqual(r.status_code, 400)

    def test_update_409s_when_the_note_changed_since_the_preview(self):
        rec = self._rec_with("upd2020", zotero="alpha beta", region="alpha")
        path = Path(rec.obsidian_note.path)
        d = self._preview("upd2020", [rec]).get_json()
        self._write_note("upd2020", "\nsomething the user just typed\n")
        after_edit = path.read_bytes()
        r = self._post("/api/notesync/update",
                       {"citation_key": "upd2020", "confirm": True,
                        "content_sha256": d["content_sha256"],
                        "proposed_sha256": d["proposed_sha256"]}, [rec])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "stale")
        self.assertEqual(path.read_bytes(), after_edit)

    def test_update_refuses_to_delete_the_users_writing_on_one_confirm(self):
        """The central rule. `confirm` means "write"; deleting text the user
        wrote is a second decision and needs its own acknowledgement."""
        rec = self._rec_with("conf2021", zotero="alpha beta",
                             region="alpha my own paragraph")
        path = Path(rec.obsidian_note.path)
        before = path.read_bytes()
        r, _ = self._update("conf2021", [rec])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "would_overwrite_user_text")
        self.assertEqual(path.read_bytes(), before)

    def test_update_proceeds_on_an_explicit_overwrite_acknowledgement(self):
        rec = self._rec_with("conf2021", zotero="alpha beta",
                             region="alpha my own paragraph")
        path = Path(rec.obsidian_note.path)
        r, _ = self._update("conf2021", [rec], accept_overwrite=True)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertNotIn("my own paragraph", path.read_text(encoding="utf-8"))
        self.assertIn("alpha beta", path.read_text(encoding="utf-8"))

    def test_update_is_a_noop_when_already_in_step(self):
        rec = self._rec_with("ok2023", zotero="alpha", region="alpha")
        path = Path(rec.obsidian_note.path)
        before = path.read_bytes()
        r, _ = self._update("ok2023", [rec])
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["changed"])
        self.assertEqual(path.read_bytes(), before)

    def test_update_503s_while_indexing_and_writes_nothing(self):
        import contextlib
        from rag.vault import obsidian_manager
        rec = self._rec_with("upd2020", zotero="alpha beta", region="alpha")
        path = Path(rec.obsidian_note.path)
        before = path.read_bytes()
        d = self._preview("upd2020", [rec]).get_json()
        with contextlib.ExitStack() as st:
            for p in self._ctx([rec]):
                st.enter_context(p)
            st.enter_context(mock.patch.object(
                obsidian_manager, "try_acquire_lock", return_value=None))
            r = self.client.post("/api/notesync/update", json={
                "citation_key": "upd2020", "confirm": True,
                "content_sha256": d["content_sha256"],
                "proposed_sha256": d["proposed_sha256"]}, headers=_HEADERS)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(path.read_bytes(), before)

    def test_an_updated_note_is_journalled_and_revertible(self):
        """Not a new write path: it goes through the refactor journal, so the
        existing Note Refactor → Restore reverts it with no new UI."""
        from vault_history import journal
        rec = self._rec_with("upd2020", zotero="alpha beta gamma", region="alpha")
        path = Path(rec.obsidian_note.path)
        before = path.read_text(encoding="utf-8")
        r, _ = self._update("upd2020", [rec])
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertNotEqual(path.read_text(encoding="utf-8"), before)

        man = journal.load(self.vault, self.cfg)
        ops = [o for o in man["ops"] if o["kind"] == "notesync_update"]
        self.assertEqual(len(ops), 1)
        res = journal.revert_op(self.vault, self.cfg, ops[0])
        self.assertTrue(res["ok"], res)
        self.assertEqual(path.read_text(encoding="utf-8"), before)


class TestStatusMemo(TestNoteSyncRoutes):
    """PERF (2026-09-02): /api/notesync/status must not re-compare every note.

    The sweep re-read and re-compared the whole library on every request —
    measured 467 ms over 1235 notes, on a Waitress worker, per poll. The memo
    is keyed on the cached Inventory's identity plus each note's
    ``(size, mtime_ns)``, so these assert both halves: a repeat call with
    nothing edited recomputes nothing, an edited note IS recomputed, and a new
    scan (a new Inventory object) invalidates everything.
    """

    def _stable_get(self, inv_pair, n=1):
        import contextlib
        from audit.manager import audit_manager
        out = []
        for _ in range(n):
            with contextlib.ExitStack() as st:
                st.enter_context(mock.patch("api.routes.notesync.load_config",
                                            return_value=self.cfg))
                st.enter_context(mock.patch.object(
                    audit_manager, "get_inventory", return_value=inv_pair))
                out.append(self.client.get("/api/notesync/status",
                                           headers=_HEADERS).get_json())
        return out

    def setUp(self):
        super().setUp()
        from api.routes import notesync as ns
        # getattr, not a direct call: this class must fail on its ASSERTIONS
        # when run against a pre-memo notesync.py, not on a missing helper —
        # a red that only proves the helper is new pins nothing.
        clear = getattr(ns, "_clear_states_cache", lambda: None)
        clear()
        self.addCleanup(clear)

    def test_second_call_on_the_same_scan_recompares_nothing(self):
        from notesync import compare as ns_compare
        recs = [self._rec("ok2021", has_note=True),
                self._rec("ok2022", has_note=True)]
        inv_pair = self._inv(recs)
        calls = []
        real = ns_compare.compare_note

        def _counted(*a, **k):
            calls.append(a[0])
            return real(*a, **k)

        with mock.patch("api.routes.notesync.compare_note", _counted):
            first, second = self._stable_get(inv_pair, n=2)
        self.assertEqual(first, second, "the memo changed the answer")
        # Two notes compared on the first call, ZERO on the second.
        self.assertEqual(calls, ["ok2021", "ok2022"])

    def test_an_edited_note_is_recompared(self):
        from notesync import compare as ns_compare
        recs = [self._rec("ok2021", has_note=True),
                self._rec("ok2022", has_note=True)]
        inv_pair = self._inv(recs)
        self._stable_get(inv_pair)
        path = self.notes / "ok2021.md"
        path.write_text("---\ntags: []\n---\ntotally different prose\n",
                        encoding="utf-8")
        calls = []
        real = ns_compare.compare_note

        def _counted(*a, **k):
            calls.append(a[0])
            return real(*a, **k)

        with mock.patch("api.routes.notesync.compare_note", _counted):
            d = self._stable_get(inv_pair)[0]
        self.assertEqual(calls, ["ok2021"], "only the edited note recomputes")
        self.assertEqual([r["citation_key"] for r in d["drifted"]], ["ok2021"])

    def test_a_new_scan_invalidates_the_memo(self):
        from notesync import compare as ns_compare
        recs = [self._rec("ok2021", has_note=True)]
        self._stable_get(self._inv(recs))
        calls = []
        real = ns_compare.compare_note

        def _counted(*a, **k):
            calls.append(a[0])
            return real(*a, **k)

        with mock.patch("api.routes.notesync.compare_note", _counted):
            self._stable_get(self._inv(recs))   # a DIFFERENT Inventory object
        self.assertEqual(calls, ["ok2021"])


class TestLayoutMigration(TestNoteSyncRoutes):
    """Phase 2 (2026-09-04): the marker-pair migration and its exact inverse.

    Not journalled — the git-clean gate and ``/unmigrate`` are the undo — so
    the tests pin that a dirty tree refuses, that a dry run writes nothing,
    and that migrate → unmigrate returns the original bytes.
    """

    RAW = "---\ntags: [dep]\n---\n\n**T**\n\n>k\n\n---\n\nreading note text\n"

    def _keyed(self, key, raw=None, *, item_key="ABCD1234", note_key="WXYZ5678"):
        rec = self._rec(key, has_note=True)
        (self.notes / f"{key}.md").write_text(raw or self.RAW, encoding="utf-8")
        rec.zotero_item.item_key = item_key
        rec.zotero_item.child_notes[0].note_key = note_key
        return rec

    def _clean_git(self):
        return mock.patch("api.routes.notesync._git_undo_check",
                          return_value=(True, "", "0" * 40))

    def test_dry_run_plans_and_writes_nothing(self):
        recs = [self._keyed("a2020"),
                self._keyed("bad2021", raw="---\ntags: []\n---\n\nno rule here\n")]
        before = (self.notes / "a2020.md").read_bytes()
        d = self._post("/api/notesync/migrate", {"dry_run": True}, recs).get_json()
        self.assertTrue(d["dry_run"])
        self.assertEqual(d["planned"], 1)
        self.assertEqual(d["skipped"], {"unparsed": ["bad2021"]})
        self.assertIn("<!-- zotero-note:begin -->", d["template_hint"])
        self.assertEqual((self.notes / "a2020.md").read_bytes(), before)

    def test_write_refuses_on_a_dirty_git_tree(self):
        recs = [self._keyed("a2020")]
        before = (self.notes / "a2020.md").read_bytes()
        with mock.patch("api.routes.notesync._git_undo_check",
                        return_value=(False, "1 file(s) git could not restore", "0" * 40)):
            r = self._post("/api/notesync/migrate", {"confirm": True}, recs)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "git_not_clean")
        self.assertEqual((self.notes / "a2020.md").read_bytes(), before)

    def test_migrate_then_unmigrate_is_byte_identical(self):
        recs = [self._keyed("a2020")]
        with self._clean_git():
            d = self._post("/api/notesync/migrate", {"confirm": True}, recs).get_json()
        self.assertEqual(d["written"], 1)
        migrated = (self.notes / "a2020.md").read_text(encoding="utf-8")
        self.assertIn("zotero-key: ABCD1234\nzotero-note-key: WXYZ5678\n---\n", migrated)
        self.assertIn("<!-- zotero-note:begin -->\n\nreading note text\n\n<!-- zotero-note:end -->\n", migrated)
        # Status now reports the layout, and migrate is idempotent.
        st = self._get("/api/notesync/status", recs).get_json()
        self.assertEqual(st["layout"], {"marked": 1, "unmarked": 0})
        with self._clean_git():
            again = self._post("/api/notesync/migrate", {"dry_run": True}, recs).get_json()
        self.assertEqual(again["planned"], 0)
        self.assertEqual(again["skipped"], {"already_migrated": ["a2020"]})
        with self._clean_git():
            u = self._post("/api/notesync/unmigrate", {"confirm": True}, recs).get_json()
        self.assertEqual(u["written"], 1)
        self.assertEqual((self.notes / "a2020.md").read_text(encoding="utf-8"), self.RAW)

    def test_write_skips_a_note_edited_between_plan_and_lock(self):
        recs = [self._keyed("a2020")]
        real_read = Path.read_bytes
        calls = {"n": 0}

        def flaky(self_path):
            calls["n"] += 1
            data = real_read(self_path)
            # Second read (under the lock) sees a different note.
            return data + b"edited\n" if calls["n"] == 2 else data
        with self._clean_git(), mock.patch.object(Path, "read_bytes", flaky):
            d = self._post("/api/notesync/migrate", {"confirm": True}, recs).get_json()
        self.assertEqual(d["written"], 0)
        self.assertEqual(d["failed"], {"a2020": "changed_on_disk"})

    def test_update_on_a_marked_note_keeps_the_users_tail(self):
        rec = self._keyed("a2020")
        with self._clean_git():
            self._post("/api/notesync/migrate", {"confirm": True}, [rec])
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nmy own words\n", encoding="utf-8")
        # Zotero side gained text → an update is available.
        rec.zotero_item.child_notes[0].body_plain = "reading note text plus more"
        d = self._post("/api/notesync/update-preview", {"citation_key": "a2020"}, [rec]).get_json()
        self.assertTrue(d["can_update"])
        self.assertTrue(d["proposed"].endswith("<!-- zotero-note:end -->\n\nmy own words\n"))
        self.assertEqual(d["extra_words"], 0)


class TestSyncState(TestNoteSyncRoutes):
    """Phase 3 (2026-09-04): the two-way state against a FRESH Zotero read.

    The fresh reader is patched at the route's own name, so no SQLite is
    touched; the base store writes under the suite's throwaway BASE_DIR.
    """

    RAW = ("---\ntags: [dep]\nzotero-key: ABCD1234\nzotero-note-key: WXYZ5678\n---\n\n**T**\n\n"
           "---\n<!-- zotero-note:begin -->\n\nalpha beta\n\n<!-- zotero-note:end -->\n")

    def _rec_keyed(self, key="a2020", raw=None, *, html="<p>alpha beta</p>", version=7,
                   has_note=True):
        rec = self._rec(key, has_note=has_note)
        rec.bib_entry.title = "T"
        if has_note:
            (self.notes / f"{key}.md").write_text(raw or self.RAW, encoding="utf-8")
        # One item key per record: the fresh map is keyed on it, and three
        # records sharing one would read each other's notes.
        rec.zotero_item.item_key = (key.upper().replace("-", "") + "AAAAAAAA")[:8]
        rec.zotero_item.item_type = 'journalArticle'
        child = SimpleNamespace(note_key="WXYZ5678", body_html=html, body_plain="", version=version)
        rec.zotero_item.child_notes = [child] if html is not None else []
        return rec

    def _fresh(self, recs):
        fresh = {r.zotero_item.item_key: list(r.zotero_item.child_notes)
                 for r in recs if r.zotero_item.child_notes}
        return mock.patch("api.routes.notesync._fresh_zotero_notes", return_value=fresh)

    def _status(self, recs):
        with self._fresh(recs):
            return self._get("/api/notesync/sync-status", recs).get_json()

    def _bootstrap(self, recs, body=None):
        with self._fresh(recs):
            return self._post("/api/notesync/bootstrap", body or {"all": True, "confirm": True}, recs).get_json()

    def test_unbaselined_then_bootstrap_then_in_sync(self):
        recs = [self._rec_keyed()]
        d = self._status(recs)
        self.assertEqual(d["counts"], {"unbaselined": 1})
        row = d["rows"][0]
        self.assertEqual((row["missing_words"], row["extra_words"], row["marked"]), (0, 0, True))
        b = self._bootstrap(recs)
        self.assertEqual((b["written"], b["keys"]), (1, ["a2020"]))
        d = self._status(recs)
        self.assertEqual(d["counts"], {"in_sync": 1})
        self.assertTrue(d["rows"][0]["baselined"])
        self.assertEqual(d["rows"][0]["zotero_version"], 7)

    def test_markdown_spelling_differences_bootstrap_and_read_in_sync(self):
        """Region ``n=29`` against a Zotero note the converter renders ``n\\=29``:
        equal after the current converter, so baselined without a write."""
        raw = self.RAW.replace("alpha beta", "alpha **_beta_** n=29")
        recs = [self._rec_keyed(raw=raw, html="<p>alpha <strong><em>beta</em></strong> n=29</p>")]
        row = self._status(recs)["rows"][0]
        self.assertEqual(row["state"], "unbaselined")
        self.assertTrue(row["bootstrap_ready"])
        self.assertEqual(self._bootstrap(recs)["written"], 1)
        self.assertEqual(self._status(recs)["counts"], {"in_sync": 1})
        self.assertEqual((self.notes / "a2020.md").read_text(encoding="utf-8"), raw)

    def test_bootstrap_refuses_a_note_that_needs_a_decision(self):
        recs = [self._rec_keyed(html="<p>alpha beta gamma</p>")]
        b = self._bootstrap(recs)
        self.assertEqual(b["written"], 0)
        self.assertEqual(b["skipped"], {"needs_decision": ["a2020"]})
        self.assertEqual(self._status(recs)["counts"], {"unbaselined": 1})

    # --- 2026-09-17 review, C3 (decision D4 (a)): outside the region is yours --

    def test_text_outside_the_region_is_preserved_not_refused(self):
        """A plugin-created header that differs from our template rendering (the
        live library: title case, `>Journal` spacing) and personal text below the
        end marker made 1,194 of 1,237 live notes `unparsed`. Writers keep both
        byte-for-byte, so neither may refuse the note."""
        raw = ("---\ntags: [dep]\nzotero-key: ABCD1234\nzotero-note-key: WXYZ5678\n---\n\n"
               "**t — Differently Cased Title**\n\n>Some Journal\n\n>10.1000/x\n\n"
               "---\n<!-- zotero-note:begin -->\n\nalpha beta\n\n<!-- zotero-note:end -->\n\nMy own remark below.\n")
        recs = [self._rec_keyed(raw=raw)]
        self.assertEqual(self._status(recs)["counts"], {"unbaselined": 1})
        self._bootstrap(recs)
        recs[0].zotero_item.child_notes[0].body_html = "<p>alpha beta gamma</p>"
        recs[0].zotero_item.child_notes[0].version = 8
        with self._fresh(recs):
            d = self._post("/api/notesync/pull", {"citation_keys": ["a2020"], "confirm": True}, recs).get_json()
        self.assertTrue(d["results"]["a2020"]["ok"], d)
        text = (self.notes / "a2020.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(raw.split("<!-- zotero-note:begin -->")[0]))
        self.assertTrue(text.endswith("<!-- zotero-note:end -->\n\nMy own remark below.\n"))
        self.assertIn("alpha beta gamma", text)

    # --- 2026-09-17 review, B6: bases from the previous converter ------------

    def _legacy_base(self, recs):
        from notesync import base_store
        self._bootstrap(recs)
        base = base_store.load(self.vault, "a2020")
        legacy = dataclasses.replace(base, converter_id="notesync-convert/1 markdownify=x markdown-it-py=y")
        base_store.save(self.vault, legacy)
        return legacy

    def test_legacy_converter_base_is_rebaselined_when_the_note_is_unchanged(self):
        from notesync import base_store
        from notesync.convert import CONVERTER_ID
        recs = [self._rec_keyed()]
        legacy = self._legacy_base(recs)
        # Zotero has moved on since the legacy sync point; that fact must survive.
        recs[0].zotero_item.child_notes[0].body_html = "<p>alpha beta</p>"
        recs[0].zotero_item.child_notes[0].version = 9
        status = self._status(recs)
        self.assertEqual(status["legacy_bases"], 1)
        b = self._bootstrap(recs)
        self.assertEqual(b["rebaselined"], ["a2020"])
        rebased = base_store.load(self.vault, "a2020")
        self.assertEqual(rebased.converter_id, CONVERTER_ID)
        self.assertEqual((rebased.base_html, rebased.zotero_version, rebased.base_region),
                         (legacy.base_html, legacy.zotero_version, legacy.base_region))
        self.assertEqual(self._status(recs)["counts"], {"in_sync": 1})

    def test_legacy_converter_base_with_an_edited_note_is_left_for_a_decision(self):
        from notesync import base_store
        recs = [self._rec_keyed()]
        legacy = self._legacy_base(recs)
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8").replace("alpha beta", "alpha beta mine"), encoding="utf-8")
        b = self._bootstrap(recs)
        self.assertEqual(b["written"], 0)
        self.assertEqual(b["skipped"], {"needs_decision": ["a2020"]})
        self.assertEqual(base_store.load(self.vault, "a2020"), legacy)

    def test_bootstrap_requires_confirm_and_a_selection(self):
        recs = [self._rec_keyed()]
        with self._fresh(recs):
            self.assertEqual(self._post("/api/notesync/bootstrap", {"all": True}, recs).status_code, 400)
            self.assertEqual(self._post("/api/notesync/bootstrap", {"confirm": True}, recs).status_code, 400)

    def test_edits_on_each_side_land_in_the_right_cell(self):
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        # Obsidian edit only → push
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8").replace("alpha beta", "alpha beta mine"),
                        encoding="utf-8")
        self.assertEqual(self._status(recs)["counts"], {"push": 1})
        # Zotero edit as well → conflict
        recs[0].zotero_item.child_notes[0].body_html = "<p>alpha beta theirs</p>"
        recs[0].zotero_item.child_notes[0].version = 8
        d = self._status(recs)
        self.assertEqual(d["counts"], {"conflict": 1})
        self.assertTrue(d["rows"][0]["zotero_changed"] and d["rows"][0]["region_changed"])
        # Revert the Obsidian edit → pull
        path.write_text(self.RAW, encoding="utf-8")
        self.assertEqual(self._status(recs)["counts"], {"pull": 1})

    def test_missing_sides_and_shapes_are_named(self):
        recs = [self._rec_keyed("noobs", has_note=False),
                self._rec_keyed("nozot", html=None),
                self._rec_keyed("shape", raw="---\ntags: []\n---\n\nno rule\n")]
        d = self._status(recs)
        by = {r["citation_key"]: r["state"] for r in d["rows"]}
        self.assertEqual(by, {"noobs": "missing_obsidian", "nozot": "missing_zotero",
                              "shape": "unparsed"})
        self.assertEqual(self._bootstrap(recs)["written"], 0)

    def test_unreadable_zotero_is_503_not_a_stale_listing(self):
        recs = [self._rec_keyed()]
        with mock.patch("api.routes.notesync._fresh_zotero_notes", side_effect=OSError("locked")):
            r = self._get("/api/notesync/sync-status", recs)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["reason"], "zotero_unreadable")

    def test_a_base_from_another_converter_is_unbaselined(self):
        from notesync import base_store
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        rec = base_store.load(self.vault, "a2020")
        base_store.save(self.vault, BaseRecordWith(rec, converter_id="older/0"))
        self.assertEqual(self._status(recs)["counts"], {"unbaselined": 1})


class TestSyncRowsMemo(TestSyncState):
    """PERF (2026-09-11): /api/notesync/sync-status must not re-classify every note.

    Sibling of ``TestStatusMemo`` for the two-way path: classify + region split
    are memoised per note sig / base sig / fresh-note fingerprint. Uses a
    stable Inventory identity (``_stable_status``) — the ordinary ``_status``
    helper rebuilds a new MagicMock Inventory per call, which would always miss.
    """

    def setUp(self):
        super().setUp()
        from api.routes import notesync as ns
        clear = getattr(ns, "_clear_states_cache", lambda: None)
        clear()
        self.addCleanup(clear)

    def _stable_status(self, recs, inv_pair=None, n=1):
        import contextlib
        from audit.manager import audit_manager
        inv_pair = inv_pair or self._inv(recs)
        fresh = {r.zotero_item.item_key: list(r.zotero_item.child_notes)
                 for r in recs if r.zotero_item.child_notes}
        out = []
        for _ in range(n):
            with contextlib.ExitStack() as st:
                st.enter_context(mock.patch("api.routes.notesync.load_config",
                                            return_value=self.cfg))
                st.enter_context(mock.patch.object(
                    audit_manager, "get_inventory", return_value=inv_pair))
                st.enter_context(mock.patch(
                    "api.routes.notesync._fresh_zotero_notes", return_value=fresh))
                out.append(self.client.get("/api/notesync/sync-status",
                                           headers=_HEADERS).get_json())
        return out, inv_pair

    def test_second_call_on_the_same_inputs_reclassifies_nothing(self):
        from notesync import state as ns_state
        recs = [self._rec_keyed("a2020"), self._rec_keyed("b2021", html="<p>x y</p>")]
        calls = []
        real = ns_state.classify

        def _counted(*a, **k):
            calls.append(True)
            return real(*a, **k)

        with mock.patch("api.routes.notesync.classify", _counted):
            first, second = self._stable_status(recs, n=2)[0]
        self.assertEqual(first["counts"], second["counts"])
        self.assertEqual(len(calls), 2, "only the first sweep should classify")

    def test_an_edited_note_is_reclassified(self):
        from notesync import state as ns_state
        recs = [self._rec_keyed("a2020"), self._rec_keyed("b2021", html="<p>x y</p>")]
        payloads, inv_pair = self._stable_status(recs)
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8").replace("alpha beta", "alpha beta edited"),
                        encoding="utf-8")
        calls = []
        real = ns_state.classify

        def _counted(*a, **k):
            calls.append(True)
            return real(*a, **k)

        with mock.patch("api.routes.notesync.classify", _counted):
            d = self._stable_status(recs, inv_pair=inv_pair)[0][0]
        self.assertEqual(len(calls), 1)
        # Unbaselined + region edit → still unbaselined, or push once baselined.
        self.assertTrue(d["counts"].get("unbaselined") or d["counts"].get("push"))

    def test_a_new_scan_invalidates_the_memo(self):
        from notesync import state as ns_state
        recs = [self._rec_keyed()]
        _, inv_pair = self._stable_status(recs)
        calls = []
        real = ns_state.classify

        def _counted(*a, **k):
            calls.append(True)
            return real(*a, **k)

        with mock.patch("api.routes.notesync.classify", _counted):
            self._stable_status(recs, inv_pair=inv_pair)
        self.assertEqual(len(calls), 0)
        calls.clear()
        with mock.patch("api.routes.notesync.classify", _counted):
            d = self._stable_status(recs, inv_pair=self._inv(recs))[0][0]
        self.assertEqual(len(calls), 1)
        self.assertEqual(d["counts"], {"unbaselined": 1})


def BaseRecordWith(rec, **changes):
    from notesync.state import BaseRecord
    return BaseRecord(**{**rec.__dict__, **changes})


class _FakeZotero:
    """An in-memory stand-in for the web API: items keyed by key, versioned."""

    def __init__(self, items):
        self.items = {k: {"data": dict(v), "version": v.get("version", 1)} for k, v in items.items()}
        self.calls = []

    def get_item(self, base, lib, key, timeout=None, api_key=None):
        from tagging.zotero_write import ZoteroError
        it = self.items.get(key)
        if it is None:
            raise ZoteroError("HTTP 404")
        return dict(it["data"]), it["version"]

    def set_item_note(self, base, lib, key, html, version, timeout=None, api_key=None):
        from tagging.zotero_write import ZoteroVersionConflict
        it = self.items[key]
        if it["version"] != version:
            raise ZoteroVersionConflict("412")
        it["data"]["note"] = html
        it["version"] += 1
        self.calls.append(("patch", key, html))
        return it["version"]

    def create_child_note(self, base, lib, parent, html, timeout=None, api_key=None):
        from tagging.zotero_write import CreatedItemKey
        key = "NEWNOTE1"
        self.items[key] = {"data": {"key": key, "itemType": "note", "parentItem": parent, "note": html}, "version": 50}
        self.calls.append(("post", parent, html))
        return CreatedItemKey(key, dict(self.items[key]['data']), 50)

    def get_item_children(self, base, lib, parent, **kwargs):
        return [dict(i['data']) for i in self.items.values() if i['data'].get('parentItem') == parent]

    def set_item_deleted(self, base, lib, key, deleted, version, timeout=None, api_key=None):
        it = self.items[key]
        it["data"]["deleted"] = bool(deleted)
        it["version"] += 1
        self.calls.append(("trash", key, deleted))
        return it["version"]

    def api_key_from_env(self):
        return "k"


class TestPushPullMerge(TestSyncState):
    """Phase 4 (2026-09-04): the four write verbs against a fake Zotero transport."""

    def _zotero(self, items):
        from tagging import zotero_write as real
        # This fixture's default reviewed record is a2020; supply realistic
        # parentage rather than treating an orphan note as a child.
        items = {k: ({'parentItem': 'A2020AAA', **v} if v.get('itemType') == 'note' else v)
                 for k, v in items.items()}
        fake = _FakeZotero(items)
        shim = SimpleNamespace(
            get_item=fake.get_item, set_item_note=fake.set_item_note,
            create_child_note=fake.create_child_note, set_item_deleted=fake.set_item_deleted,
            get_item_children=fake.get_item_children,
            api_key_from_env=fake.api_key_from_env, API_KEY_ENV=real.API_KEY_ENV,
            ZoteroUnavailable=real.ZoteroUnavailable, ZoteroError=real.ZoteroError,
            ZoteroVersionConflict=real.ZoteroVersionConflict,
            ZoteroWriteUnsupported=real.ZoteroWriteUnsupported)
        transport = SimpleNamespace(is_web=True, api_key="k", base_url="https://api.example", kind="web")
        import contextlib
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch('notesync.create_attempts.store_path',
                                       return_value=self.vault / 'fixture-attempts.sqlite3'))
        stack.enter_context(mock.patch("notesync.zotero_notes.zotero_write", shim))
        stack.enter_context(mock.patch("notesync.zotero_notes._zotero_transport", return_value=transport))
        stack.enter_context(mock.patch("notesync.zotero_notes._zotero_lib_path", return_value="users/1"))
        return stack, fake

    def _verb(self, path, body, recs):
        with self._fresh(recs):
            return self._post(path, body, recs)

    # --- pull ---------------------------------------------------------------

    def test_pull_writes_the_region_and_the_base(self):
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        recs[0].zotero_item.child_notes[0].body_html = "<p>alpha beta gamma</p>"
        recs[0].zotero_item.child_notes[0].version = 8
        self.assertEqual(self._status(recs)["counts"], {"pull": 1})
        d = self._verb("/api/notesync/pull", {"citation_keys": ["a2020"], "confirm": True}, recs).get_json()
        self.assertEqual(d["pulled"], 1)
        self.assertTrue(d["results"]["a2020"]["ok"])
        text = (self.notes / "a2020.md").read_text(encoding="utf-8")
        self.assertIn("<!-- zotero-note:begin -->\n\nalpha beta gamma\n\n<!-- zotero-note:end -->", text)
        self.assertTrue(text.startswith("---\ntags: [dep]\nzotero-key:"))   # frontmatter untouched
        st = self._status(recs)
        self.assertEqual(st["counts"], {"in_sync": 1})
        self.assertEqual(st["rows"][0]["zotero_version"], 8)

    def test_pull_refuses_a_conflict_without_the_acknowledgement(self):
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8").replace("alpha beta", "alpha beta mine"), encoding="utf-8")
        recs[0].zotero_item.child_notes[0].body_html = "<p>alpha beta theirs</p>"
        d = self._verb("/api/notesync/pull", {"citation_keys": ["a2020"], "confirm": True}, recs).get_json()
        self.assertEqual(d["pulled"], 0)
        self.assertEqual(d["results"]["a2020"], {"ok": False, "reason": "conflict", "needs_accept": True})
        self.assertIn("mine", path.read_text(encoding="utf-8"))
        d = self._verb("/api/notesync/pull", {"citation_keys": ["a2020"], "confirm": True,
                                              "accept_conflict": True}, recs).get_json()
        self.assertEqual(d["pulled"], 1)
        self.assertIn("theirs", path.read_text(encoding="utf-8"))
        self.assertNotIn("mine", path.read_text(encoding="utf-8"))

    def test_pull_is_journalled_and_revertible(self):
        from vault_history import journal
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        recs[0].zotero_item.child_notes[0].body_html = "<p>alpha beta gamma</p>"
        before = (self.notes / "a2020.md").read_bytes()
        d = self._verb("/api/notesync/pull", {"citation_keys": ["a2020"], "confirm": True}, recs).get_json()
        op_id = d["results"]["a2020"]["op_id"]
        man = journal.load(self.vault, self.cfg)
        op = journal.find_op(man, op_id)
        self.assertEqual(op["kind"], "notesync_update")
        journal.revert_op(self.vault, self.cfg, op)
        self.assertEqual((self.notes / "a2020.md").read_bytes(), before)

    # --- push ---------------------------------------------------------------

    def test_push_patches_zotero_journals_and_rebases(self):
        from tagging import journal as tjournal
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8").replace("alpha beta", "alpha beta **mine**"), encoding="utf-8")
        self.assertEqual(self._status(recs)["counts"], {"push": 1})
        stack, fake = self._zotero({"WXYZ5678": {"itemType": "note", "note": "<p>alpha beta</p>",
                                                 "parentItem": "A2020AAA", "version": 7}})
        with stack:
            d = self._verb("/api/notesync/push", {"citation_keys": ["a2020"], "confirm": True}, recs).get_json()
        self.assertEqual(d["pushed"], 1, d)
        res = d["results"]["a2020"]
        self.assertEqual(res["status"], "applied")
        self.assertEqual(res["version"], 8)
        self.assertEqual(fake.calls[0][0], "patch")
        self.assertIn("<strong>mine</strong>", fake.items["WXYZ5678"]["data"]["note"])
        self.assertTrue(fake.items["WXYZ5678"]["data"]["note"].startswith('<div data-schema-version="9">'))
        man = tjournal.load(self.vault, self.cfg)
        op = tjournal.find_op(man, res["op_id"])
        self.assertEqual((op["kind"], op["state"], op["version_after"]), ("zotero_note_write", "applied", 8))
        self.assertEqual(op["before_html"], "<p>alpha beta</p>")
        # The base now holds what we SENT, so our own normalisation is not an edit.
        recs[0].zotero_item.child_notes[0].body_html = fake.items["WXYZ5678"]["data"]["note"]
        recs[0].zotero_item.child_notes[0].version = 8
        self.assertEqual(self._status(recs)["counts"], {"in_sync": 1})

    def test_push_of_a_region_in_another_markdown_spelling_reads_in_sync(self):
        """After a push the base holds the sent HTML and the region; the note
        must then read in_sync even though the converter spells the region
        differently (``n=2`` → ``n\\=2``). It read ``conflict`` before."""
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8").replace("alpha beta", "alpha beta n=2"), encoding="utf-8")
        stack, fake = self._zotero({"WXYZ5678": {"itemType": "note", "note": "<p>alpha beta</p>",
                                                 "parentItem": "A2020AAA", "version": 7}})
        with stack:
            d = self._verb("/api/notesync/push", {"citation_keys": ["a2020"], "confirm": True}, recs).get_json()
        self.assertEqual(d["pushed"], 1, d)
        recs[0].zotero_item.child_notes[0].body_html = fake.items["WXYZ5678"]["data"]["note"]
        recs[0].zotero_item.child_notes[0].version = 8
        self.assertEqual(self._status(recs)["counts"], {"in_sync": 1})

    def test_push_refuses_when_the_api_is_ahead_of_the_local_database(self):
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8").replace("alpha beta", "alpha beta mine"), encoding="utf-8")
        # The local SQLite still says "alpha beta"; the API already moved on.
        stack, fake = self._zotero({"WXYZ5678": {"itemType": "note", "note": "<p>alpha beta THEIRS</p>", "version": 9}})
        with stack:
            d = self._verb("/api/notesync/push", {"citation_keys": ["a2020"], "confirm": True}, recs).get_json()
        self.assertEqual(d["pushed"], 0)
        self.assertEqual(d["results"]["a2020"]["status"], "conflict")
        self.assertTrue(d["results"]["a2020"]["needs_accept"])
        self.assertEqual(fake.calls, [])
        # An ExitStack cannot be re-entered; build the transport again.
        stack, fake = self._zotero({"WXYZ5678": {"itemType": "note", "note": "<p>alpha beta THEIRS</p>", "version": 9}})
        with stack:
            d = self._verb("/api/notesync/push", {"citation_keys": ["a2020"], "confirm": True,
                                                  "accept_conflict": True}, recs).get_json()
        self.assertEqual(d["pushed"], 1, d)
        self.assertEqual(fake.calls[0][0], "patch")

    def test_push_marks_a_failed_patch_failed_never_applied(self):
        from tagging import journal as tjournal
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8").replace("alpha beta", "alpha beta mine"), encoding="utf-8")
        stack, fake = self._zotero({"WXYZ5678": {"itemType": "note", "note": "<p>alpha beta</p>", "version": 7}})
        real_patch = fake.set_item_note

        def racing(base, lib, key, html, version, **kw):
            fake.items[key]["version"] += 1          # someone edited between GET and PATCH
            return real_patch(base, lib, key, html, version, **kw)
        with stack, mock.patch("notesync.zotero_notes.zotero_write.set_item_note", racing):
            d = self._verb("/api/notesync/push", {"citation_keys": ["a2020"], "confirm": True}, recs).get_json()
        res = d["results"]["a2020"]
        self.assertEqual(res["status"], "conflict")
        man = tjournal.load(self.vault, self.cfg)
        self.assertEqual(tjournal.find_op(man, res["op_id"])["state"], "failed")
        self.assertEqual(self._status(recs)["counts"], {"push": 1})   # nothing rebased

    # --- 2026-09-17 review, B2: outcome_unknown must have an exit ------------

    def _push_once(self, recs, set_item_note):
        stack, fake = self._zotero({"WXYZ5678": {"itemType": "note", "note": "<p>alpha beta</p>", "version": 7}})
        with stack, mock.patch("notesync.zotero_notes.zotero_write.set_item_note", set_item_note(fake)):
            d = self._verb("/api/notesync/push", {"citation_keys": ["a2020"], "confirm": True}, recs).get_json()
        return d["results"]["a2020"], fake

    def _mine(self, recs):
        self._bootstrap(recs)
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8").replace("alpha beta", "alpha beta mine"), encoding="utf-8")

    def test_refused_connection_is_failed_not_unknown(self):
        import urllib.error
        from tagging import journal as tjournal
        from tagging import zotero_write as real
        recs = [self._rec_keyed()]
        self._mine(recs)

        def refused(fake):
            def write(base, lib, key, html, version, **kw):
                with mock.patch.object(real, "_urlopen", side_effect=urllib.error.URLError(ConnectionRefusedError(61, "refused"))):
                    return real.set_item_note("http://127.0.0.1:1", lib, key, html, version)
            return write
        res, fake = self._push_once(recs, refused)
        op = tjournal.find_op(tjournal.load(self.vault, self.cfg), res["op_id"])
        self.assertEqual(op["state"], "failed")
        self.assertEqual(fake.items["WXYZ5678"]["data"]["note"], "<p>alpha beta</p>")

    def _lost_response(self, fake):
        real_patch = fake.set_item_note

        def write(base, lib, key, html, version, **kw):
            from tagging.zotero_write import ZoteroUnavailable
            real_patch(base, lib, key, html, version, **kw)
            raise ZoteroUnavailable("timed out")
        return write

    def test_lost_response_self_heals_when_zotero_holds_the_written_note(self):
        from tagging import journal as tjournal
        recs = [self._rec_keyed()]
        self._mine(recs)
        res, fake = self._push_once(recs, self._lost_response)
        first = res["op_id"]
        self.assertEqual(tjournal.find_op(tjournal.load(self.vault, self.cfg), first)["state"], "outcome_unknown")
        written = fake.items["WXYZ5678"]
        stack, fake2 = self._zotero({"WXYZ5678": {**written["data"], "version": written["version"]}})
        with stack:
            again = self._verb("/api/notesync/push", {"citation_keys": ["a2020"], "confirm": True}, recs).get_json()
        self.assertNotEqual(again["results"]["a2020"]["status"], "outcome_unknown", again)
        healed = tjournal.find_op(tjournal.load(self.vault, self.cfg), first)
        self.assertEqual((healed["state"], healed["version_after"]), ("applied", written["version"]))
        self.assertTrue(healed["observed_after_unknown"])

    def test_lost_response_with_different_zotero_text_is_refused_and_names_the_version(self):
        from tagging import journal as tjournal
        recs = [self._rec_keyed()]
        self._mine(recs)
        res, _fake = self._push_once(recs, self._lost_response)
        stack, fake2 = self._zotero({"WXYZ5678": {"itemType": "note", "note": "<p>someone else</p>", "version": 12}})
        with stack:
            again = self._verb("/api/notesync/push", {"citation_keys": ["a2020"], "confirm": True}, recs).get_json()
        refused = again["results"]["a2020"]
        self.assertEqual((refused["status"], refused["found_version"]), ("outcome_unknown", 12))
        self.assertIn("version 12", refused["message"])
        self.assertEqual(fake2.calls, [])
        self.assertEqual(tjournal.find_op(tjournal.load(self.vault, self.cfg), res["op_id"])["state"], "outcome_unknown")

    def test_restore_of_an_unknown_note_write_checks_zotero_live_first(self):
        from notesync import zotero_notes
        from tagging import journal as tjournal
        recs = [self._rec_keyed()]
        self._mine(recs)
        res, _fake = self._push_once(recs, self._lost_response)
        op = tjournal.find_op(tjournal.load(self.vault, self.cfg), res["op_id"])
        stack, fake2 = self._zotero({"WXYZ5678": {"itemType": "note", "note": "<p>someone else</p>", "version": 12}})
        with stack:
            prepared = zotero_notes.revert_prepare(self.cfg, op)
        self.assertEqual(prepared["early"]["status"], "skipped")
        self.assertIn("may never have landed", prepared["early"]["message"])
        self.assertEqual(fake2.calls, [])

    def test_push_requires_confirm_and_a_bounded_list(self):
        recs = [self._rec_keyed()]
        self.assertEqual(self._verb("/api/notesync/push", {"citation_keys": ["a2020"]}, recs).status_code, 400)
        self.assertEqual(self._verb("/api/notesync/push", {"citation_keys": ["k"] * 51, "confirm": True},
                                    recs).status_code, 400)

    # --- merge --------------------------------------------------------------

    # --- either direction on any comparable row (2026-09-07) ---------------

    def test_a_row_the_app_calls_push_can_still_be_pulled_with_the_acknowledgement(self):
        """The gap the three-button model closes: before this, `_pullable`
        tested the state NAME, so a row summarised as `push` could never be
        pulled — not even with `accept_conflict`. "Throw my edit away, take
        Zotero's" was inexpressible on exactly the rows it is asked for."""
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8").replace("alpha beta", "alpha beta mine"),
                        encoding="utf-8")
        self.assertEqual(self._status(recs)["counts"], {"push": 1})
        # Refused bare — pulling here deletes the user's words.
        d = self._verb("/api/notesync/pull", {"citation_keys": ["a2020"], "confirm": True}, recs).get_json()
        self.assertEqual(d["pulled"], 0)
        self.assertEqual(d["results"]["a2020"], {"ok": False, "reason": "push", "needs_accept": True})
        self.assertIn("mine", path.read_text(encoding="utf-8"))
        # Accepted — and it lands.
        d = self._verb("/api/notesync/pull", {"citation_keys": ["a2020"], "confirm": True,
                                              "accept_conflict": True}, recs).get_json()
        self.assertEqual(d["pulled"], 1)
        self.assertNotIn("mine", path.read_text(encoding="utf-8"))

    def test_a_row_the_app_calls_pull_can_still_be_pushed_with_the_acknowledgement(self):
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        recs[0].zotero_item.child_notes[0].body_html = "<p>alpha beta theirs</p>"
        self.assertEqual(self._status(recs)["counts"], {"pull": 1})
        stack, fake = self._zotero({"WXYZ5678": {"itemType": "note", "note": "<p>alpha beta</p>",
                                                 "parentItem": "A2020AAA", "version": 7}})
        with stack:
            d = self._verb("/api/notesync/push", {"citation_keys": ["a2020"], "confirm": True},
                           recs).get_json()
            self.assertEqual(d["pushed"], 0)
            self.assertEqual(d["results"]["a2020"]["status"], "pull")
            self.assertTrue(d["results"]["a2020"]["needs_accept"])
            d = self._verb("/api/notesync/push", {"citation_keys": ["a2020"], "confirm": True,
                                                  "accept_conflict": True}, recs).get_json()
            self.assertEqual(d["pushed"], 1)

    def test_an_in_step_row_takes_either_direction_with_no_acknowledgement(self):
        """Nothing has moved on either side, so neither direction can delete
        anything: both are offered bare. (The write is a no-op for pull, which
        still rebases.)"""
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        self.assertEqual(self._status(recs)["counts"], {"in_sync": 1})
        d = self._verb("/api/notesync/pull", {"citation_keys": ["a2020"], "confirm": True}, recs).get_json()
        self.assertTrue(d["results"]["a2020"]["ok"])
        stack, fake = self._zotero({"WXYZ5678": {"itemType": "note", "note": "<p>alpha beta</p>",
                                                 "parentItem": "A2020AAA", "version": 7}})
        with stack:
            d = self._verb("/api/notesync/push", {"citation_keys": ["a2020"], "confirm": True},
                           recs).get_json()
        self.assertTrue(d["results"]["a2020"]["ok"], d)

    def test_a_row_with_no_comparable_region_is_refused_in_both_directions(self):
        """The widening is bounded by `_COMPARABLE_STATES`: `missing_zotero`,
        `unparsed` and friends have no region to write or nothing to write
        from, and `accept_conflict` must not open them."""
        from api.routes import notesync as ns
        for state in ("missing_obsidian", "missing_zotero", "unparsed", "multiple_notes"):
            row = {"state": state, "baselined": False, "region_changed": True,
                   "zotero_changed": True, "missing_words": 5, "extra_words": 5}
            with self.subTest(state=state):
                self.assertFalse(ns._pullable(row, True))
                self.assertFalse(ns._pushable(row, True))

    def test_merge_preview_and_write_both_sides(self):
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8").replace("alpha beta", "alpha beta\n\nmine"), encoding="utf-8")
        recs[0].zotero_item.child_notes[0].body_html = "<p>theirs</p><p>alpha beta</p>"
        pv = self._verb("/api/notesync/merge-preview", {"citation_key": "a2020"}, recs).get_json()
        self.assertEqual((pv["state"], pv["base"], pv["ours"], pv["theirs"]),
                         ("conflict", "alpha beta", "alpha beta\n\nmine", "theirs\n\nalpha beta"))
        self.assertEqual(pv["conflicts"], 0)                     # disjoint edits auto-merge
        self.assertEqual(pv["merged"], "theirs\n\nalpha beta\n\nmine")
        # Markers are refused.
        r = self._verb("/api/notesync/merge", {"citation_key": "a2020", "result": "<<<<<<< Obsidian\nx",
                                               "targets": {"obsidian": True}, "confirm": True,
                                               "content_sha256": pv["content_sha256"], "evidence": pv["evidence"]}, recs)
        self.assertEqual(r.status_code, 400)
        stack, fake = self._zotero({"WXYZ5678": {"itemType": "note", "note": "<p>theirs</p><p>alpha beta</p>", "version": 7}})
        with stack:
            d = self._verb("/api/notesync/merge", {
                "citation_key": "a2020", "result": pv["merged"] + " edited",
                "targets": {"obsidian": True, "zotero": True}, "confirm": True,
                "content_sha256": pv["content_sha256"], "evidence": pv["evidence"]}, recs).get_json()
        self.assertTrue(d["ok"], d)
        self.assertTrue(d["obsidian"]["ok"] and d["zotero"]["status"] == "applied")
        text = path.read_text(encoding="utf-8")
        self.assertIn("<!-- zotero-note:begin -->\n\ntheirs\n\nalpha beta\n\nmine edited\n\n<!-- zotero-note:end -->", text)
        self.assertIn("<p>mine edited</p>", fake.items["WXYZ5678"]["data"]["note"])
        recs[0].zotero_item.child_notes[0].body_html = fake.items["WXYZ5678"]["data"]["note"]
        recs[0].zotero_item.child_notes[0].version = 8
        self.assertEqual(self._status(recs)["counts"], {"in_sync": 1})

    WRAPPED = '<div class="zotero-note znv1"><div data-schema-version="9"><p>alpha beta</p></div></div>'
    NATIVE = '<div data-schema-version="9"><p>alpha beta</p></div>'

    def test_merge_accepts_the_web_api_note_without_the_desktop_wrapper(self):
        """Live 2026-09-18: the local database holds the note wrapped in
        ``zotero-note znv1``; the web API returns it, same version, unwrapped.
        Every merge refused "Zotero changed since the preview"."""
        recs = [self._rec_keyed(html=self.WRAPPED)]
        self._bootstrap(recs)
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8").replace("alpha beta", "alpha beta\n\nmine"), encoding="utf-8")
        pv = self._verb("/api/notesync/merge-preview", {"citation_key": "a2020"}, recs).get_json()
        stack, fake = self._zotero({"WXYZ5678": {"itemType": "note", "note": self.NATIVE, "version": 7}})
        with stack:
            d = self._verb("/api/notesync/merge", {
                "citation_key": "a2020", "result": "alpha beta\n\nmine",
                "targets": {"obsidian": True, "zotero": True}, "confirm": True,
                "content_sha256": pv["content_sha256"], "evidence": pv["evidence"]}, recs).get_json()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["zotero"]["status"], "applied")

    def test_merge_still_refuses_a_real_zotero_change(self):
        recs = [self._rec_keyed(html=self.WRAPPED)]
        self._bootstrap(recs)
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8").replace("alpha beta", "alpha beta\n\nmine"), encoding="utf-8")
        pv = self._verb("/api/notesync/merge-preview", {"citation_key": "a2020"}, recs).get_json()
        stack, fake = self._zotero({"WXYZ5678": {"itemType": "note", "version": 7,
                                                 "note": self.NATIVE.replace("beta", "beta THEIRS")}})
        with stack:
            r = self._verb("/api/notesync/merge", {
                "citation_key": "a2020", "result": "alpha beta\n\nmine",
                "targets": {"obsidian": True, "zotero": True}, "confirm": True,
                "content_sha256": pv["content_sha256"], "evidence": pv["evidence"]}, recs)
        self.assertEqual(r.status_code, 409)
        self.assertIn("mine", path.read_text(encoding="utf-8"))
        self.assertEqual(fake.calls, [])

    def test_push_accepts_a_base_holding_the_desktop_wrapper(self):
        """The base keeps the local database's wrapped HTML; the web API's
        unwrapped copy of the same note is not a Zotero change."""
        recs = [self._rec_keyed(html=self.WRAPPED)]
        self._bootstrap(recs)
        path = self.notes / "a2020.md"
        path.write_text(path.read_text(encoding="utf-8").replace("alpha beta", "alpha beta mine"), encoding="utf-8")
        stack, fake = self._zotero({"WXYZ5678": {"itemType": "note", "note": self.NATIVE, "version": 7}})
        with stack:
            d = self._verb("/api/notesync/push", {"citation_keys": ["a2020"], "confirm": True}, recs).get_json()
        self.assertEqual(d["pushed"], 1, d)

    def test_merge_through_a_symlinked_notes_folder_inside_the_vault(self):
        """2026-09-17 review, F12: local_guard read the note by its unresolved
        vault-relative path with O_NOFOLLOW per component, so a notes folder
        that is a symlink inside the vault made every merge 409. (Measured
        2026-09-17: the live vault's notes folder is not a symlink.)"""
        import os
        real = self.vault / "notes_real"
        os.rename(self.notes, real)
        os.symlink("notes_real", self.notes)
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        pv = self._verb("/api/notesync/merge-preview", {"citation_key": "a2020"}, recs).get_json()
        stack, fake = self._zotero({"WXYZ5678": {"itemType": "note", "note": "<p>alpha beta</p>", "version": 7}})
        with stack:
            d = self._verb("/api/notesync/merge", {
                "citation_key": "a2020", "result": "alpha beta merged",
                "targets": {"obsidian": True, "zotero": True}, "confirm": True,
                "content_sha256": pv["content_sha256"], "evidence": pv["evidence"]}, recs)
        self.assertEqual(d.status_code, 200, d.get_json())
        self.assertIn("alpha beta merged", (real / "a2020.md").read_text(encoding="utf-8"))

    def test_merge_is_stale_diff_guarded(self):
        recs = [self._rec_keyed()]
        r = self._verb("/api/notesync/merge", {"citation_key": "a2020", "result": "x",
                                               "targets": {"obsidian": True}, "confirm": True,
                                               "content_sha256": "0" * 64}, recs)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "stale")

    def test_merge_rejects_zotero_edit_after_preview_before_local_write(self):
        recs = [self._rec_keyed()]
        p = self._verb('/api/notesync/merge-preview', {'citation_key': 'a2020'}, recs).get_json()
        path = self.notes / 'a2020.md'
        before = path.read_bytes()
        stack, fake = self._zotero({'WXYZ5678': {'itemType': 'note', 'note': '<p>New external edit</p>', 'version': 8}})
        with stack:
            response = self._verb('/api/notesync/merge', dict(citation_key='a2020', result='merged',
                targets={'obsidian': True, 'zotero': True}, confirm=True,
                content_sha256=p['content_sha256'], evidence=p['evidence']), recs)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(fake.items['WXYZ5678']['data']['note'], '<p>New external edit</p>')

    def test_merge_partial_does_not_advance_base(self):
        from notesync import base_store
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        base = base_store.load(self.vault, 'a2020')
        p = self._verb('/api/notesync/merge-preview', {'citation_key': 'a2020'}, recs).get_json()
        stack, _ = self._zotero({'WXYZ5678': {'itemType': 'note', 'note': '<p>alpha beta</p>', 'version': 7}})
        with stack, mock.patch('api.routes.notesync._push_many', return_value={'a2020': {'ok': False, 'status': 'outcome_unknown'}}):
            response = self._verb('/api/notesync/merge', dict(citation_key='a2020', result='merged',
                targets={'obsidian': True, 'zotero': True}, confirm=True,
                content_sha256=p['content_sha256'], evidence=p['evidence']), recs)
        self.assertFalse(response.get_json()['ok'])
        self.assertTrue(response.get_json()['obsidian']['ok'])
        self.assertEqual(base_store.load(self.vault, 'a2020'), base)
        self.assertIn('merged', (self.notes / 'a2020.md').read_text())

    def test_campaign_preparation_selection_and_production_gate(self):
        recs = [self._rec_keyed()]
        plan = self._verb('/api/notesync/campaign/plan', {'citation_keys': ['a2020']}, recs).get_json()
        self.assertEqual(set(plan['proposals']), {'a2020'})
        p = plan['proposals']['a2020']
        decision = dict(citation_key='a2020', evidence=p['evidence'], result='alpha beta', approve=True)
        route = '/api/notesync/campaign/' + plan['id'] + '/apply'
        response = self._verb(route, {'confirm': True, 'decisions': [decision]}, recs)
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.get_json()['reason'], 'native_human_acceptance_required')
        with mock.patch('notesync.campaigns.application_available', return_value=True):
            # Only fixture injection opens orchestration, never a production flag.
            bad = self._verb(route, {'confirm': True, 'decisions': [{**decision, 'evidence': 'stale'}]}, recs)
            self.assertEqual(bad.status_code, 400)
            stack, _ = self._zotero({'WXYZ5678': {'itemType': 'note', 'note': '<p>alpha beta</p>', 'version': 7}})
            with stack:
                done = self._verb(route, {'confirm': True, 'decisions': [decision]}, recs)
            self.assertTrue(done.get_json()['ok'], done.get_json())
            repeated = self._verb(route, {'confirm': True, 'decisions': [decision]}, recs)
            self.assertEqual(repeated.status_code, 409)

    def test_campaign_inspection_is_fresh_and_does_not_change_journals_or_base(self):
        from notesync import campaigns, base_store
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        plan = self._verb('/api/notesync/campaign/plan', {'citation_keys': ['a2020']}, recs).get_json()
        campaigns.record(plan['id'], 'a2020', {'decision': {'result': 'merged'}}, reserve=True)
        saved = campaigns.read(plan['id'])
        before = base_store.load(self.vault, 'a2020')
        path = self.notes / 'a2020.md'
        raw = path.read_text()
        path.write_text(raw.replace('alpha beta', 'merged'))
        response = self._verb('/api/notesync/campaign/' + plan['id'] + '/inspect', {}, recs)
        self.assertEqual(response.status_code, 200, response.get_json())
        result = response.get_json()['inspections']['a2020']
        self.assertEqual(result['status'], 'incomplete', result)
        self.assertFalse(result['replay_allowed'])
        self.assertEqual(campaigns.read(plan['id']), saved)
        self.assertEqual(base_store.load(self.vault, 'a2020'), before)

    def test_campaign_recovery_completes_only_inspected_partial_result(self):
        from notesync import campaigns, base_store
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        before = base_store.load(self.vault, 'a2020')
        plan = self._verb('/api/notesync/campaign/plan', {'citation_keys': ['a2020']}, recs).get_json()
        campaigns.record(plan['id'], 'a2020', {'decision': {'result': 'merged'}}, reserve=True)
        path = self.notes / 'a2020.md'
        path.write_text(path.read_text().replace('alpha beta', 'merged'))
        route = '/api/notesync/campaign/' + plan['id']
        inspected = self._verb(route + '/inspect', {}, recs).get_json()['inspections']['a2020']
        decision = dict(citation_key='a2020', result='merged', confirm=True,
                        evidence=inspected['current']['evidence'], outcome_evidence=inspected['outcome_evidence'])
        self.assertEqual(self._verb(route + '/recover', decision, recs).status_code, 501)
        self.assertEqual(base_store.load(self.vault, 'a2020'), before)
        stack, fake = self._zotero({'WXYZ5678': {'itemType': 'note', 'note': '<p>alpha beta</p>', 'version': 7}})
        with stack, mock.patch('notesync.campaigns.application_available', return_value=True):
            bad = self._verb(route + '/recover', dict(decision, result='different'), recs)
            self.assertEqual(bad.status_code, 409)
            done = self._verb(route + '/recover', decision, recs)
            self.assertTrue(done.get_json()['ok'], done.get_json())
            self.assertEqual(self._verb(route + '/recover', decision, recs).status_code, 409)
        from notesync.convert import md_to_html
        self.assertEqual(fake.items['WXYZ5678']['data']['note'], md_to_html('merged'))
        outcome = campaigns.read(plan['id'])['outcomes']['a2020']
        self.assertTrue(outcome['recovery'])
        self.assertEqual(outcome['dispatch']['dispatch']['decision']['result'], 'merged')

    def test_merge_preserves_edit_between_local_snapshot_and_write(self):
        from vault_history import journal
        from notesync import base_store
        recs = [self._rec_keyed()]
        self._bootstrap(recs)
        before = base_store.load(self.vault, 'a2020')
        path = self.notes / 'a2020.md'
        p = self._verb('/api/notesync/merge-preview', {'citation_key': 'a2020'}, recs).get_json()
        original = journal.write_note_snapshot
        def edited(*args, **kwargs):
            saved = original(*args, **kwargs)
            path.write_text(path.read_text().replace('alpha beta', 'New personal edit'))
            return saved
        stack, fake = self._zotero({'WXYZ5678': {'itemType': 'note', 'note': '<p>alpha beta</p>', 'version': 7}})
        with stack, mock.patch.object(journal, 'write_note_snapshot', side_effect=edited):
            response = self._verb('/api/notesync/merge', dict(citation_key='a2020', result='merged',
                targets={'obsidian': True, 'zotero': True}, confirm=True,
                content_sha256=p['content_sha256'], evidence=p['evidence']), recs)
        self.assertFalse(response.get_json()['ok'])
        self.assertIn('New personal edit', path.read_text())
        self.assertEqual(fake.items['WXYZ5678']['data']['note'], '<p>alpha beta</p>')
        self.assertEqual(base_store.load(self.vault, 'a2020'), before)

    def test_campaign_interruptions_retain_barrier_and_do_not_advance_base(self):
        from api.routes import notesync as ns
        from notesync import campaigns, base_store
        # Each phase uses a separate key/path and a fresh disposable transport.
        for index, boundary in enumerate(('local', 'remote', 'base')):
            with self.subTest(boundary=boundary):
                key = f'interrupt{index}'
                recs = [self._rec_keyed(key)]
                self._bootstrap(recs)
                before = base_store.load(self.vault, key)
                plan = self._verb('/api/notesync/campaign/plan', {'citation_keys': [key]}, recs).get_json()
                p = plan['proposals'][key]
                route = '/api/notesync/campaign/' + plan['id'] + '/apply'
                decision = dict(citation_key=key, result='merged', evidence=p['evidence'], approve=True)
                obj, name = ((ns, '_region_writes') if boundary == 'local' else
                             (ns.zotero_notes, 'patch_write') if boundary == 'remote' else (ns, '_save_base'))
                original = getattr(obj, name)
                def cut(*args, **kwargs):
                    if boundary != 'base':
                        original(*args, **kwargs)
                    raise RuntimeError('fixture interruption')
                stack, _ = self._zotero({'WXYZ5678': {'itemType': 'note', 'note': '<p>alpha beta</p>',
                    'parentItem': recs[0].zotero_item.item_key, 'version': 7}})
                with stack, mock.patch('notesync.campaigns.application_available', return_value=True), mock.patch.object(obj, name, side_effect=cut) as interrupted:
                    failed = self._verb(route, {'confirm': True, 'decisions': [decision]}, recs)
                    self.assertEqual(failed.status_code, 500)
                    interrupted.assert_called_once()
                    again = self._verb(route, {'confirm': True, 'decisions': [decision]}, recs)
                    self.assertEqual(again.status_code, 409)
                self.assertEqual(base_store.load(self.vault, key), before)
                self.assertEqual(campaigns.read(plan['id'])['outcomes'][key]['status'], 'dispatched_or_unknown')

    def test_recovery_settles_exact_native_journal_without_repeating_patch(self):
        self._recovery_settlement_case()

    def test_recovery_preserves_native_edit_during_reservation(self):
        self._recovery_settlement_case('native')

    def test_recovery_preserves_local_edit_during_reservation(self):
        self._recovery_settlement_case('local')

    def test_recovery_preserves_base_change_during_reservation(self):
        self._recovery_settlement_case('base')

    def test_recovery_issues_no_zotero_io_under_the_op_lock(self):
        self._recovery_settlement_case(probe=True)

    @contextlib.contextmanager
    def _zotero_io_must_be_lock_free(self):
        """Wrap the transport the note writers call (the consumer's binding,
        ``notesync.zotero_notes.zotero_write``) and record every Zotero call
        made while the obsidian op-lock is held (2026-09-17 review, B1)."""
        from notesync import zotero_notes
        zw = zotero_notes.zotero_write
        violations, saved = [], {}
        for name in ('get_item', 'set_item_note', 'set_item_deleted'):
            original = getattr(zw, name)
            saved[name] = original

            def wrap(*args, _original=original, _name=name, **kwargs):
                if getattr(self, '_lock_held', False):
                    violations.append(_name)
                return _original(*args, **kwargs)
            setattr(zw, name, wrap)
        try:
            yield violations
        finally:
            for name, original in saved.items():
                setattr(zw, name, original)

    def _recovery_settlement_case(self, change=None, probe=False):
        from notesync import campaigns, zotero_notes
        from notesync.convert import md_to_html
        from tagging import journal
        recs = [self._rec_keyed()]
        recs[0].zotero_item.server_id = 'fixture-database'
        recs[0].zotero_item.library_id = 1
        plan = self._verb('/api/notesync/campaign/plan', {'citation_keys': ['a2020']}, recs).get_json()
        campaigns.record(plan['id'], 'a2020', {'decision': {'result': 'merged'}}, reserve=True)
        html = md_to_html('merged')
        recs[0].zotero_item.child_notes[0].body_html = html
        recs[0].zotero_item.child_notes[0].version = 8
        manifest = journal.load(self.vault, self.cfg)
        op = dict(op_id=journal.new_op_id(manifest), kind=zotero_notes.OP_WRITE, citation_key='a2020',
            item_key='WXYZ5678', parent_item='A2020AAA', before_html='<p>alpha beta</p>', after_html=html,
            version_before=7, server_id='fixture-database', lib_path='users/1', base_url='http://fixture',
            transport='local', state='outcome_unknown', ts=journal.now_iso())
        manifest['ops'].append(op)
        journal.save(self.vault, self.cfg, manifest)
        route = '/api/notesync/campaign/' + plan['id']
        inspected = self._verb(route + '/inspect', {}, recs).get_json()['inspections']['a2020']
        decision = dict(confirm=True, citation_key='a2020', result='merged',
            evidence=inspected['current']['evidence'], outcome_evidence=inspected['outcome_evidence'])
        stack, fake = self._zotero({'WXYZ5678': {'itemType': 'note', 'note': html, 'version': 8}})
        def read(base, lib, key, **kwargs):
            self.assertEqual(kwargs.pop('server_id'), 'fixture-database')
            return fake.get_item(base, lib, key, **kwargs)
        transport = SimpleNamespace(is_web=False, base_url='http://fixture', kind='local')
        reserve = campaigns.reserve_recovery
        path = self.notes / 'a2020.md'
        def reserve_then_edit(*args, **kwargs):
            reserve(*args, **kwargs)
            if change == 'native':
                fake.items['WXYZ5678']['data']['note'] = md_to_html('New personal edit: 41 mg')
                fake.items['WXYZ5678']['version'] = 9
            elif change == 'local':
                path.write_text(path.read_text().replace('alpha beta', 'New personal edit: 41 mg'))
            elif change == 'base':
                from notesync import base_store
                base_path = base_store._path(self.vault, 'a2020')
                base_path.parent.mkdir(parents=True, exist_ok=True)
                base_path.write_text('{"new": "unreadable foreign base"}')
        with stack, mock.patch.object(campaigns, 'reserve_recovery', side_effect=reserve_then_edit), mock.patch('notesync.campaigns.application_available', return_value=True), \
                mock.patch.object(zotero_notes, '_zotero_transport', return_value=transport), \
                mock.patch.object(zotero_notes.zotero_write, 'get_item', side_effect=read), \
                mock.patch.object(zotero_notes.zotero_write, 'set_item_note') as write:
            if probe:
                with self._zotero_io_must_be_lock_free() as violations:
                    done = self._verb(route + '/recover', decision, recs)
                self.assertTrue(done.get_json()['ok'], done.get_json())
                self.assertEqual(violations, [])
                return
            done = self._verb(route + '/recover', decision, recs)
        if change:
            self.assertEqual(done.status_code, 409, done.get_json())
            self.assertEqual(journal.find_op(journal.load(self.vault, self.cfg), op['op_id'])['state'], 'outcome_unknown')
            self.assertEqual(campaigns.read(plan['id'])['outcomes']['a2020']['status'], 'recovery_dispatched_or_unknown')
            write.assert_not_called()
            if change == 'native':
                self.assertIn('41 mg', fake.items['WXYZ5678']['data']['note'])
            elif change == 'local':
                self.assertIn('41 mg', path.read_text())
            return
        self.assertTrue(done.get_json()['ok'], done.get_json())
        write.assert_not_called()
        retained = journal.find_op(journal.load(self.vault, self.cfg), op['op_id'])
        self.assertEqual(retained['state'], 'observed_after_recovery')
        self.assertEqual(retained['before_html'], '<p>alpha beta</p>')
        self.assertIn('merged', (self.notes / 'a2020.md').read_text())

    def test_campaign_apply_and_recover_share_process_hold(self):
        from notesync import campaigns
        recs = [self._rec_keyed()]
        plan = self._verb('/api/notesync/campaign/plan', {'citation_keys': ['a2020']}, recs).get_json()
        route = '/api/notesync/campaign/' + plan['id']
        decision = dict(citation_key='a2020', evidence=plan['proposals']['a2020']['evidence'],
                        result='merged', approve=True)
        with mock.patch.object(campaigns, 'application_available', return_value=True):
            with campaigns.recovery_hold(plan['id']) as held:
                self.assertTrue(held)
                for endpoint, body in (('/apply', {'confirm': True, 'decisions': [decision]}),
                                       ('/recover', {'confirm': True})):
                    response = self._verb(route + endpoint, body, recs)
                    self.assertEqual(response.status_code, 409, response.get_json())
            self.assertEqual(campaigns.read(plan['id'])['outcomes'], {})

    def test_campaign_real_writer_crash_boundaries_preserve_durable_evidence(self):
        import subprocess
        import sys
        from api.routes import notesync as ns
        from notesync import campaigns, base_store
        from notesync.convert import md_to_html
        from vault_history import _write
        from tagging import journal
        # Inject failures around actual filesystem/writer/journal calls. Only
        # the external transport and inventory are synthetic; no writer stub.
        boundaries = ('before_local', 'after_local', 'before_patch', 'after_patch',
                      'after_readback', 'before_base', 'after_base', 'before_outcome')
        result = 'Personal comment: 12 participants; 3 withdrew.\n\n[Reference](https://example.invalid/fixture)\n\nSecond observation: 27 mg.'
        for index, boundary in enumerate(boundaries):
            with self.subTest(boundary=boundary):
                key = f'crash{index}'
                recs = [self._rec_keyed(key)]
                self._bootstrap(recs)
                before_base = base_store.load(self.vault, key)
                path = self.notes / f'{key}.md'
                original_text = path.read_text()
                plan = self._verb('/api/notesync/campaign/plan', {'citation_keys': [key]}, recs).get_json()
                route = '/api/notesync/campaign/' + plan['id']
                decision = dict(citation_key=key, evidence=plan['proposals'][key]['evidence'], result=result, approve=True)
                stack, fake = self._zotero({'WXYZ5678': {'itemType': 'note', 'note': '<p>alpha beta</p>',
                    'parentItem': recs[0].zotero_item.item_key, 'version': 7}})
                if boundary.endswith('local'):
                    obj, name = _write, 'write_text_atomic'
                elif boundary.endswith('patch'):
                    obj, name = ns.zotero_notes.zotero_write, 'set_item_note'
                elif boundary == 'after_readback':
                    obj, name = ns.zotero_notes, 'patch_write'
                elif boundary.endswith('base'):
                    obj, name = ns, '_save_base'
                else:
                    obj, name = campaigns, 'record'
                original = getattr(obj, name)
                def cut(*args, **kwargs):
                    if boundary == 'before_outcome' and kwargs.get('reserve'):
                        return original(*args, **kwargs)
                    if boundary.startswith('after_'):
                        original(*args, **kwargs)
                    raise RuntimeError('fixture crash: ' + boundary)
                with stack, mock.patch.object(campaigns, 'application_available', return_value=True):
                    with mock.patch.object(obj, name, side_effect=cut):
                        failed = self._verb(route + '/apply', {'confirm': True, 'decisions': [decision]}, recs)
                    self.assertEqual(failed.status_code, 500)
                    saved = campaigns.read(plan['id'])
                    self.assertEqual(saved['outcomes'][key]['status'], 'dispatched_or_unknown')
                    # Reopen the persistent store in an independent interpreter.
                    code = ('from notesync import campaigns; '
                            'assert campaigns.read(' + repr(plan['id']) + ')["outcomes"][' + repr(key) +
                            ']["status"] == "dispatched_or_unknown"')
                    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True)
                    self.client = _app().test_client()
                    self.assertEqual(self._verb(route + '/apply', {'confirm': True, 'decisions': [decision]}, recs).status_code, 409)
                    self.assertEqual(campaigns.read(plan['id']), saved)
                local_written = boundary != 'before_local'
                remote_written = boundary not in ('before_local', 'after_local', 'before_patch')
                self.assertEqual(path.read_text(), original_text.replace('alpha beta', result) if local_written else original_text)
                self.assertEqual(fake.items['WXYZ5678']['data']['note'], md_to_html(result) if remote_written else '<p>alpha beta</p>')
                self.assertEqual(len(fake.calls), int(remote_written))
                base = base_store.load(self.vault, key)
                if boundary in ('after_base', 'before_outcome'):
                    self.assertEqual(base.base_html, md_to_html(result))
                    self.assertEqual(base.base_region.strip(), result)
                else:
                    self.assertEqual(base, before_base)
                ops = [op for op in journal.load(self.vault, self.cfg)['ops'] if op.get('citation_key') == key]
                if boundary in ('before_patch', 'after_patch', 'after_readback'):
                    self.assertEqual(len(ops), 1)
                    self.assertEqual(ops[0]['state'], 'outcome_unknown')
                    self.assertEqual(ops[0]['after_html'], md_to_html(result))

    # --- settlement after a post-base / pre-outcome crash ---------------------

    _SETTLE_RESULT = 'Personal comment: 12 participants; 3 withdrew.\n\n[Reference](https://example.invalid/fixture)\n\nSecond observation: 27 mg.'

    def _settlement_fixture(self, key):
        """Dispatch through the real writers and crash before the campaign
        outcome is recorded; mirror Zotero's sync-down into the scan record."""
        from notesync import campaigns
        recs = [self._rec_keyed(key)]
        self._bootstrap(recs)
        plan = self._verb('/api/notesync/campaign/plan', {'citation_keys': [key]}, recs).get_json()
        route = '/api/notesync/campaign/' + plan['id']
        decision = dict(citation_key=key, evidence=plan['proposals'][key]['evidence'],
                        result=self._SETTLE_RESULT, approve=True)
        stack, fake = self._zotero({'WXYZ5678': {'itemType': 'note', 'note': '<p>alpha beta</p>',
            'parentItem': recs[0].zotero_item.item_key, 'version': 7}})
        stack.enter_context(mock.patch.object(campaigns, 'application_available', return_value=True))
        stack.__enter__()
        original = campaigns.record
        def cut(*args, **kwargs):
            if kwargs.get('reserve'):
                return original(*args, **kwargs)
            raise RuntimeError('fixture crash: before_outcome')
        with mock.patch.object(campaigns, 'record', side_effect=cut):
            self.assertEqual(self._verb(route + '/apply', {'confirm': True, 'decisions': [decision]}, recs).status_code, 500)
        recs[0].zotero_item.child_notes[0].body_html = fake.items['WXYZ5678']['data']['note']
        recs[0].zotero_item.child_notes[0].version = fake.items['WXYZ5678']['version']
        return recs, plan, route, stack, fake

    def _settle_body(self, inspection):
        return dict(confirm=True, citation_key=inspection['current']['citation_key'], result=self._SETTLE_RESULT,
                    evidence=inspection['current']['evidence'], outcome_evidence=inspection['outcome_evidence'])

    def test_settlement_records_post_base_crash_without_writing(self):
        import subprocess
        import sys
        from notesync import campaigns, base_store
        from vault_history import journal as rjournal
        from tagging import journal
        key = 'settle0'
        recs, plan, route, stack, fake = self._settlement_fixture(key)
        with stack:
            saved = campaigns.read(plan['id'])
            self.assertEqual(saved['outcomes'][key]['status'], 'dispatched_or_unknown')
            inspection = self._verb(route + '/inspect', {}, recs).get_json()['inspections'][key]
            self.assertEqual(inspection['status'], 'base_changed')
            self.assertTrue(inspection['settlement']['applicable'], inspection['settlement'])
            self.assertEqual(campaigns.read(plan['id']), saved)  # inspection records nothing
            before = (base_store.load(self.vault, key), (self.notes / f'{key}.md').read_bytes(),
                      journal.load(self.vault, self.cfg)['ops'], rjournal.load(self.vault, self.cfg)['ops'],
                      list(fake.calls))
            self.assertEqual(self._verb(route + '/settle', {'citation_key': key}, recs).status_code, 400)
            done = self._verb(route + '/settle', self._settle_body(inspection), recs)
            self.assertEqual(done.status_code, 200, done.get_json())
            self.assertEqual(done.get_json()['status'], 'settled_after_interruption')
            self.assertEqual(len(done.get_json()['settlement']['local_op_ids']), 1)
            self.assertEqual(len(done.get_json()['settlement']['native_op_ids']), 1)
            after = (base_store.load(self.vault, key), (self.notes / f'{key}.md').read_bytes(),
                     journal.load(self.vault, self.cfg)['ops'], rjournal.load(self.vault, self.cfg)['ops'],
                     list(fake.calls))
            self.assertEqual(after, before)  # nothing rewritten, no PATCH, no journal entry
            outcome = campaigns.read(plan['id'])['outcomes'][key]
            self.assertTrue(outcome['ok'])
            self.assertEqual(outcome['dispatch'], saved['outcomes'][key])  # history retained beneath
            self.assertNotIn('version_after', outcome)  # no invented undo baseline
            code = ('from notesync import campaigns; o = campaigns.read(' + repr(plan['id']) + ')["outcomes"][' +
                    repr(key) + ']; assert o["status"] == "settled_after_interruption" and o["dispatch"]["status"] == "dispatched_or_unknown"')
            subprocess.run([sys.executable, '-c', code], check=True, capture_output=True)
            self.assertEqual(self._verb(route + '/settle', self._settle_body(inspection), recs).status_code, 409)
            self.assertEqual(self._verb(route + '/apply', {'confirm': True, 'decisions': [
                dict(citation_key=key, evidence=plan['proposals'][key]['evidence'], result=self._SETTLE_RESULT, approve=True)]}, recs).status_code, 409)
            again = self._verb(route + '/inspect', {}, recs).get_json()['inspections'][key]
            self.assertEqual(again['status'], 'completed')
            self.assertFalse(again['applicable'])
            self.assertEqual(self._verb(route + '/recover', self._settle_body(inspection), recs).status_code, 409)

    def test_settlement_issues_no_zotero_io_under_the_op_lock(self):
        key = 'settleprobe'
        recs, plan, route, stack, fake = self._settlement_fixture(key)
        with stack:
            inspection = self._verb(route + '/inspect', {}, recs).get_json()['inspections'][key]
            with self._zotero_io_must_be_lock_free() as violations:
                done = self._verb(route + '/settle', self._settle_body(inspection), recs)
        self.assertEqual(done.status_code, 200, done.get_json())
        self.assertEqual(violations, [])

    # --- full campaign undo (2026-09-16, chantier 1) ------------------------
    # Test trap: a `_zotero()` ExitStack cannot be re-entered — a second `with
    # stack` applies NO patches and the verb silently hits the real transport.
    # Every call below builds its own fake from the same item state.

    def _applied_campaign(self, key='a2020', *, bootstrap=True, result='merged result: 27 mg'):
        """Plan and apply one campaign note on both destinations through the
        fixture admission. Returns (recs, plan, written_html, route, original
        bytes, pre-campaign base)."""
        from notesync import base_store, campaigns
        recs = [self._rec_keyed(key)]
        if bootstrap:
            self._bootstrap(recs)
        before_base = base_store.load(self.vault, key)
        path = self.notes / f'{key}.md'
        original = path.read_bytes()
        plan = self._verb('/api/notesync/campaign/plan', {'citation_keys': [key]}, recs).get_json()
        decision = dict(citation_key=key, evidence=plan['proposals'][key]['evidence'], result=result, approve=True)
        route = '/api/notesync/campaign/' + plan['id']
        stack, fake = self._zotero({'WXYZ5678': {'itemType': 'note', 'note': '<p>alpha beta</p>',
                                                 'parentItem': recs[0].zotero_item.item_key, 'version': 7}})
        with stack, mock.patch.object(campaigns, 'application_available', return_value=True):
            done = self._verb(route + '/apply', {'confirm': True, 'decisions': [decision]}, recs)
        self.assertTrue(done.get_json()['ok'], done.get_json())
        self.assertEqual(len(fake.calls), 1)
        written = fake.items['WXYZ5678']['data']['note']
        recs[0].zotero_item.child_notes[0].body_html = written
        recs[0].zotero_item.child_notes[0].version = 8
        return recs, plan, written, route, original, before_base

    def _native(self, recs, html, version):
        return self._zotero({'WXYZ5678': {'itemType': 'note', 'note': html,
                                          'parentItem': recs[0].zotero_item.item_key, 'version': version}})

    def _undo_decision(self, route, key, recs, html, version):
        stack, _ = self._native(recs, html, version)
        with stack:
            inspected = self._verb(route + '/inspect', {}, recs).get_json()['inspections'][key]
        return inspected, dict(citation_key=key, outcome_evidence=inspected['outcome_evidence'], confirm=True)

    def test_campaign_undo_restores_both_before_images_and_previous_base(self):
        from notesync import base_store, campaigns
        from vault_history import journal as rjournal
        from tagging import journal as tjournal
        recs, plan, written, route, original, before_base = self._applied_campaign()
        applied = campaigns.read(plan['id'])['outcomes']['a2020']
        self.assertEqual(applied['dispatch']['previous_base'], before_base.to_json())
        self.assertNotEqual(base_store.load(self.vault, 'a2020'), before_base)
        inspected, decision = self._undo_decision(route, 'a2020', recs, written, 8)
        self.assertEqual(inspected['status'], 'completed')
        self.assertTrue(inspected['undo']['applicable'], inspected['undo'])
        # Production gate first, then the fixture admission.
        self.assertEqual(self._verb(route + '/undo', decision, recs).status_code, 501)
        stack, fake = self._native(recs, written, 8)
        with stack, mock.patch.object(campaigns, 'application_available', return_value=True):
            done = self._verb(route + '/undo', decision, recs)
            self.assertEqual(done.status_code, 200, done.get_json())
            body = done.get_json()
            self.assertEqual((body['status'], body['undo']['base']['status']), ('undone', 'restored'))
            self.assertEqual(self._verb(route + '/undo', decision, recs).status_code, 409)
            apply_again = self._verb(route + '/apply', {'confirm': True, 'decisions': [
                dict(citation_key='a2020', evidence=plan['proposals']['a2020']['evidence'], result='x', approve=True)]}, recs)
            self.assertEqual(apply_again.status_code, 409)
        self.assertEqual((self.notes / 'a2020.md').read_bytes(), original)
        self.assertEqual(fake.items['WXYZ5678']['data']['note'], '<p>alpha beta</p>')
        self.assertEqual([c[0] for c in fake.calls], ['patch'])
        self.assertEqual(base_store.load(self.vault, 'a2020'), before_base)
        outcome = campaigns.read(plan['id'])['outcomes']['a2020']
        self.assertEqual(outcome['status'], 'undone')
        self.assertEqual(outcome['dispatch']['obsidian']['op_id'], applied['obsidian']['op_id'])
        self.assertEqual(rjournal.find_op(rjournal.load(self.vault, self.cfg), applied['obsidian']['op_id'])['state'], 'reverted')
        self.assertEqual(tjournal.find_op(tjournal.load(self.vault, self.cfg), applied['zotero']['op_id'])['state'], 'reverted')
        recs[0].zotero_item.child_notes[0].body_html = '<p>alpha beta</p>'
        recs[0].zotero_item.child_notes[0].version = 9
        with self._native(recs, '<p>alpha beta</p>', 9)[0]:
            self.assertEqual(self._verb(route + '/inspect', {}, recs).get_json()['inspections']['a2020']['status'], 'undone')

    def test_campaign_undo_issues_no_zotero_io_under_the_op_lock(self):
        from notesync import campaigns
        recs, plan, written, route, original, _ = self._applied_campaign()
        _inspected, decision = self._undo_decision(route, 'a2020', recs, written, 8)
        stack, fake = self._native(recs, written, 8)
        with stack, mock.patch.object(campaigns, 'application_available', return_value=True):
            with self._zotero_io_must_be_lock_free() as violations:
                done = self._verb(route + '/undo', decision, recs)
        self.assertEqual(done.status_code, 200, done.get_json())
        self.assertEqual([c[0] for c in fake.calls], ['patch'])
        self.assertEqual(violations, [])
        self.assertEqual((self.notes / 'a2020.md').read_bytes(), original)

    def test_campaign_undo_records_incomplete_when_the_lock_is_lost_after_the_native_revert(self):
        from notesync import campaigns
        recs, plan, written, route, original, _ = self._applied_campaign()
        local_written = (self.notes / 'a2020.md').read_bytes()
        _inspected, decision = self._undo_decision(route, 'a2020', recs, written, 8)
        stack, fake = self._native(recs, written, 8)
        self._lock_available = False
        try:
            with stack, mock.patch.object(campaigns, 'application_available', return_value=True), \
                    mock.patch('time.sleep'):
                partial = self._verb(route + '/undo', decision, recs)
        finally:
            self._lock_available = True
        self.assertEqual(partial.status_code, 502, partial.get_json())
        self.assertEqual(fake.items['WXYZ5678']['data']['note'], '<p>alpha beta</p>')
        self.assertEqual((self.notes / 'a2020.md').read_bytes(), local_written)
        outcome = campaigns.read(plan['id'])['outcomes']['a2020']
        self.assertEqual(outcome['status'], 'undo_incomplete')
        self.assertEqual(outcome['undo']['zotero']['status'], 'reverted')

    def test_campaign_undo_names_an_unreadable_native_note_as_such(self):
        """2026-09-17 review, F10: a read failure was reported as
        zotero_changed_after_write."""
        from notesync import zotero_notes
        recs, plan, written, route, original, _ = self._applied_campaign()
        early = {"early": {"ok": False, "status": "error", "message": "Zotero is not reachable."}}
        with mock.patch.object(zotero_notes, "revert_prepare", return_value=early):
            inspected, _decision = self._undo_decision(route, 'a2020', recs, written, 8)
        self.assertIn('native_unreadable', inspected['undo']['reasons'])
        self.assertNotIn('zotero_changed_after_write', inspected['undo']['reasons'])

    def test_campaign_undo_refuses_after_a_local_edit(self):
        from notesync import campaigns
        recs, plan, written, route, original, _ = self._applied_campaign()
        path = self.notes / 'a2020.md'
        edited = path.read_text().replace('27 mg', '27 mg (rechecked)')
        path.write_text(edited)
        inspected, decision = self._undo_decision(route, 'a2020', recs, written, 8)
        self.assertIn('obsidian_changed_after_write', inspected['undo']['reasons'])
        stack, fake = self._native(recs, written, 8)
        with stack, mock.patch.object(campaigns, 'application_available', return_value=True):
            refused = self._verb(route + '/undo', decision, recs)
        self.assertEqual(refused.status_code, 409)
        self.assertIn('obsidian_changed_after_write', refused.get_json()['undo']['reasons'])
        self.assertEqual(path.read_text(), edited)
        self.assertEqual(fake.calls, [])
        self.assertTrue(campaigns.read(plan['id'])['outcomes']['a2020']['ok'])

    def test_campaign_undo_refuses_after_a_native_edit(self):
        from notesync import campaigns
        recs, plan, written, route, original, _ = self._applied_campaign()
        local_written = (self.notes / 'a2020.md').read_bytes()
        edited_html = written.replace('27 mg', '27 mg (native)')
        recs[0].zotero_item.child_notes[0].body_html = edited_html
        recs[0].zotero_item.child_notes[0].version = 9
        inspected, decision = self._undo_decision(route, 'a2020', recs, edited_html, 9)
        self.assertIn('zotero_changed_after_write', inspected['undo']['reasons'])
        stack, fake = self._native(recs, edited_html, 9)
        with stack, mock.patch.object(campaigns, 'application_available', return_value=True):
            refused = self._verb(route + '/undo', decision, recs)
        self.assertEqual(refused.status_code, 409)
        self.assertEqual((self.notes / 'a2020.md').read_bytes(), local_written)
        self.assertEqual(fake.items['WXYZ5678']['data']['note'], edited_html)
        self.assertEqual(fake.calls, [])

    def test_campaign_undo_partial_is_retained_as_undo_incomplete(self):
        from notesync import campaigns
        from vault_history import journal as rjournal
        recs, plan, written, route, original, _ = self._applied_campaign()
        local_written = (self.notes / 'a2020.md').read_bytes()
        _inspected, decision = self._undo_decision(route, 'a2020', recs, written, 8)
        stack, fake = self._native(recs, written, 8)
        with stack, mock.patch.object(campaigns, 'application_available', return_value=True), \
                mock.patch.object(rjournal, 'revert_op', return_value=dict(ok=False, status='skipped', message='fixture')):
            partial = self._verb(route + '/undo', decision, recs)
            self.assertEqual(partial.status_code, 502, partial.get_json())
            self.assertEqual(partial.get_json()['status'], 'undo_incomplete')
            self.assertEqual(self._verb(route + '/undo', decision, recs).status_code, 409)
        self.assertEqual(fake.items['WXYZ5678']['data']['note'], '<p>alpha beta</p>')
        self.assertEqual((self.notes / 'a2020.md').read_bytes(), local_written)
        outcome = campaigns.read(plan['id'])['outcomes']['a2020']
        self.assertEqual((outcome['status'], outcome['ok']), ('undo_incomplete', False))
        self.assertEqual(outcome['undo']['zotero']['status'], 'reverted')
        self.assertFalse(outcome['undo']['obsidian']['ok'])
        recs[0].zotero_item.child_notes[0].body_html = '<p>alpha beta</p>'
        recs[0].zotero_item.child_notes[0].version = 9
        with self._native(recs, '<p>alpha beta</p>', 9)[0]:
            self.assertEqual(self._verb(route + '/inspect', {}, recs).get_json()['inspections']['a2020']['status'], 'undo_incomplete')

    def test_campaign_undo_removes_the_base_when_none_existed_before(self):
        from notesync import base_store, campaigns
        recs, plan, written, route, original, before_base = self._applied_campaign(bootstrap=False)
        self.assertIsNone(before_base)
        self.assertIsNone(campaigns.read(plan['id'])['outcomes']['a2020']['dispatch']['previous_base'])
        self.assertIsNotNone(base_store.load(self.vault, 'a2020'))
        _inspected, decision = self._undo_decision(route, 'a2020', recs, written, 8)
        stack, _fake = self._native(recs, written, 8)
        with stack, mock.patch.object(campaigns, 'application_available', return_value=True):
            done = self._verb(route + '/undo', decision, recs)
        self.assertEqual(done.status_code, 200, done.get_json())
        self.assertEqual(done.get_json()['undo']['base']['status'], 'removed')
        self.assertIsNone(base_store.load(self.vault, 'a2020'))
        self.assertEqual((self.notes / 'a2020.md').read_bytes(), original)

    def test_settlement_is_closed_in_production(self):
        from notesync import campaigns
        recs = [self._rec_keyed()]
        plan = self._verb('/api/notesync/campaign/plan', {'citation_keys': ['a2020']}, recs).get_json()
        self.assertFalse(campaigns.application_available())
        response = self._verb('/api/notesync/campaign/' + plan['id'] + '/settle', {'confirm': True, 'citation_key': 'a2020'}, recs)
        self.assertEqual(response.status_code, 501)
        self.assertFalse(response.get_json()['applicable'])

    def test_settlement_blockers_retain_the_outcome(self):
        from notesync import campaigns, base_store, zotero_notes
        from tagging import journal
        cases = ('local_edit', 'native_edit', 'unrelated_base', 'native_journal_failed',
                 'native_journal_unknown', 'local_journal_missing', 'base_unreadable')
        expected = dict(local_edit='obsidian_differs_from_approved', native_edit='native_version_moved_since_base',
                        unrelated_base='base_content_differs_from_approved', native_journal_failed='native_journal_evidence_missing',
                        native_journal_unknown='native_write_outcome_unknown', local_journal_missing='local_journal_evidence_missing',
                        base_unreadable='base_missing_or_unreadable')
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                key = f'settleblock{index}'
                recs, plan, route, stack, fake = self._settlement_fixture(key)
                path = self.notes / f'{key}.md'
                with stack:
                    saved = campaigns.read(plan['id'])
                    if case == 'local_edit':
                        path.write_text(path.read_text().replace('27 mg', '41 mg'))
                    elif case == 'native_edit':
                        recs[0].zotero_item.child_notes[0].body_html = '<p>New personal edit: 41 mg</p>'
                        recs[0].zotero_item.child_notes[0].version += 1
                    elif case == 'unrelated_base':
                        rec = base_store.load(self.vault, key)
                        base_store.save(self.vault, dataclasses.replace(rec, base_region='Someone else region.'))
                    elif case in ('native_journal_failed', 'native_journal_unknown'):
                        manifest = journal.load(self.vault, self.cfg)
                        op = next(op for op in manifest['ops'] if op.get('kind') == zotero_notes.OP_WRITE and op.get('citation_key') == key)
                        op['state'] = 'failed' if case == 'native_journal_failed' else 'outcome_unknown'
                        journal.save(self.vault, self.cfg, manifest)
                    elif case == 'local_journal_missing':
                        from vault_history import journal as rjournal
                        manifest = rjournal.load(self.vault, self.cfg)
                        manifest['ops'] = [op for op in manifest['ops'] if op.get('note_rel') != f'Z_Zotero_Notes/{key}.md']
                        rjournal.save(self.vault, self.cfg, manifest)
                    elif case == 'base_unreadable':
                        base_store._path(self.vault, key).write_text('{"new": "unreadable foreign base"}')
                    inspection = self._verb(route + '/inspect', {}, recs).get_json()['inspections'][key]
                    self.assertIn(inspection['status'], ('base_changed', 'concurrent_change', 'incomplete'), inspection)
                    if inspection['status'] == 'base_changed':
                        self.assertFalse(inspection['settlement']['applicable'])
                        self.assertIn(expected[case], inspection['settlement']['reasons'])
                    done = self._verb(route + '/settle', self._settle_body(inspection), recs)
                    self.assertEqual(done.status_code, 409, done.get_json())
                    self.assertEqual(campaigns.read(plan['id']), saved)
                    self.assertEqual(len(fake.calls), 1)
                    if case == 'local_edit':
                        self.assertIn('41 mg', path.read_text())

    def test_settlement_refuses_stale_inspection_competing_client_and_late_write(self):
        from notesync import campaigns
        for index, case in enumerate(('stale', 'competing', 'late_write')):
            with self.subTest(case=case):
                key = f'settlerace{index}'
                recs, plan, route, stack, fake = self._settlement_fixture(key)
                path = self.notes / f'{key}.md'
                with stack:
                    saved = campaigns.read(plan['id'])
                    inspection = self._verb(route + '/inspect', {}, recs).get_json()['inspections'][key]
                    self.assertTrue(inspection['settlement']['applicable'])
                    body = self._settle_body(inspection)
                    if case == 'stale':
                        path.write_text(path.read_text().replace('27 mg', '41 mg'))
                        done = self._verb(route + '/settle', body, recs)
                    elif case == 'competing':
                        with campaigns.recovery_hold(plan['id']) as held:
                            self.assertTrue(held)
                            done = self._verb(route + '/settle', body, recs)
                    else:
                        # The fixture's `_post` owns the simulated lease, so the
                        # write is injected at the cache clear the settle path
                        # performs right after taking the op-lock (its second
                        # clear: the inspection it runs first is the other).
                        from api.routes import notesync as ns
                        clear, calls = ns._clear_states_cache, []
                        def clear_after_late_write():
                            calls.append(1)
                            if len(calls) == 2:
                                path.write_text(path.read_text().replace('27 mg', '41 mg'))
                            clear()
                        with mock.patch.object(ns, '_clear_states_cache', side_effect=clear_after_late_write):
                            done = self._verb(route + '/settle', body, recs)
                        self.assertGreaterEqual(len(calls), 2)
                    self.assertEqual(done.status_code, 409, done.get_json())
                    self.assertEqual(campaigns.read(plan['id']), saved)
                    self.assertEqual(len(fake.calls), 1)
                    if case != 'competing':
                        self.assertIn('41 mg', path.read_text())

    def test_settlement_interruption_keeps_or_completes_exactly_once(self):
        from notesync import campaigns
        for index, when in enumerate(('before_record', 'after_record')):
            with self.subTest(when=when):
                key = f'settlecut{index}'
                recs, plan, route, stack, fake = self._settlement_fixture(key)
                with stack:
                    saved = campaigns.read(plan['id'])
                    inspection = self._verb(route + '/inspect', {}, recs).get_json()['inspections'][key]
                    body = self._settle_body(inspection)
                    original = campaigns.settle
                    def cut(*args, **kwargs):
                        if when == 'after_record':
                            original(*args, **kwargs)
                        raise RuntimeError('fixture crash: settlement ' + when)
                    with mock.patch.object(campaigns, 'settle', side_effect=cut):
                        self.assertEqual(self._verb(route + '/settle', body, recs).status_code, 500)
                    outcome = campaigns.read(plan['id'])['outcomes'][key]
                    if when == 'before_record':
                        self.assertEqual(campaigns.read(plan['id']), saved)
                        self.assertEqual(self._verb(route + '/settle', body, recs).status_code, 200)
                    else:
                        self.assertEqual(outcome['status'], 'settled_after_interruption')
                        self.assertEqual(self._verb(route + '/settle', body, recs).status_code, 409)
                    self.assertEqual(campaigns.read(plan['id'])['outcomes'][key]['status'], 'settled_after_interruption')
                    self.assertEqual(len(fake.calls), 1)

    # --- create in Zotero -----------------------------------------------------

    def test_create_zotero_note_posts_keys_the_frontmatter_and_rebases(self):
        from tagging import journal as tjournal
        raw = "---\ntags: [dep]\n---\n\n**T**\n\n---\n\nmy reading note\n"
        recs = [self._rec_keyed("nozot", raw=raw, html=None)]
        self.assertEqual(self._status(recs)["counts"], {"missing_zotero": 1})
        stack, fake = self._zotero({"NOZOTAAA": {"itemType": "journalArticle", "title": "T nozot", "DOI": "10.1/x", "version": 3}})
        with stack:
            d = self._verb("/api/notesync/create-zotero-note", {"citation_key": "nozot", "confirm": True}, recs).get_json()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["note_key"], "NEWNOTE1")
        self.assertEqual(fake.calls[0][:2], ("post", "NOZOTAAA"))
        self.assertIn("<p>my reading note</p>", fake.items["NEWNOTE1"]["data"]["note"])
        text = (self.notes / "nozot.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\ntags: [dep]\nzotero-key: NOZOTAAA\nzotero-note-key: NEWNOTE1\n---\n"))
        self.assertIn("my reading note", text)
        man = tjournal.load(self.vault, self.cfg)
        op = tjournal.find_op(man, d["op_id"])
        self.assertEqual((op["kind"], op["item_key"], op["parent_item"]), ("zotero_note_create", "NEWNOTE1", "NOZOTAAA"))
        # Now the fresh read would carry the note: in sync.
        recs[0].zotero_item.child_notes = [SimpleNamespace(
            note_key="NEWNOTE1", body_html=fake.items["NEWNOTE1"]["data"]["note"], body_plain="", version=50)]
        self.assertEqual(self._status(recs)["counts"], {"in_sync": 1})

    def test_create_refuses_when_a_note_already_exists(self):
        recs = [self._rec_keyed()]
        r = self._verb("/api/notesync/create-zotero-note", {"citation_key": "a2020", "confirm": True}, recs)
        self.assertEqual(r.status_code, 409)

    def test_lost_create_response_blocks_a_second_post(self):
        raw = "---\ntags: [dep]\n---\n\n**T**\n\n---\n\nmy reading note\n"
        recs = [self._rec_keyed("nozot", raw=raw, html=None)]
        stack, _ = self._zotero({"NOZOTAAA": {"itemType": "journalArticle", "title": "T nozot", "DOI": "10.1/x", "version": 3}})
        with stack, mock.patch('notesync.zotero_notes.post_create', return_value=(
                {'ok': False, 'status': 'unreachable'}, None, None)) as post:
            first = self._verb('/api/notesync/create-zotero-note', {'citation_key': 'nozot', 'confirm': True}, recs)
            second = self._verb('/api/notesync/create-zotero-note', {'citation_key': 'nozot', 'confirm': True}, recs)
        self.assertEqual(first.status_code, 502)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()['status'], 'reconciliation_required')
        self.assertEqual(post.call_count, 1)

    def test_retained_key_is_available_without_fresh_zotero_read(self):
        raw = "---\ntags: [dep]\n---\n\n**T**\n\n---\n\nmy reading note\n"
        recs = [self._rec_keyed("nozot", raw=raw, html=None)]
        stack, _ = self._zotero({"NOZOTAAA": {"itemType": "journalArticle", "title": "T nozot", "DOI": "10.1/x", "version": 3}})
        with stack:
            first = self._verb('/api/notesync/create-zotero-note', {'citation_key': 'nozot', 'confirm': True}, recs)
            self.assertEqual(first.status_code, 200)
            with mock.patch('api.routes.notesync._load_sync_context', side_effect=AssertionError('Fresh read must not run')):
                second = self._verb('/api/notesync/create-zotero-note', {'citation_key': 'nozot', 'confirm': True}, recs)
        self.assertEqual(second.status_code, 409)
        evidence = second.get_json()
        self.assertEqual(evidence['note_key'], 'NEWNOTE1')
        self.assertEqual(evidence['attempt']['target']['parent'], 'NOZOTAAA')

    def test_inspection_reads_original_acknowledged_item_without_writes(self):
        raw = "---\ntags: [dep]\n---\n\n**T**\n\n---\n\nmy reading note\n"
        recs = [self._rec_keyed("nozot", raw=raw, html=None)]
        stack, fake = self._zotero({"NOZOTAAA": {"itemType": "journalArticle", "title": "T nozot", "DOI": "10.1/x", "version": 3}})
        with stack:
            first = self._verb('/api/notesync/create-zotero-note', {'citation_key': 'nozot', 'confirm': True}, recs)
            self.assertEqual(first.status_code, 200)
            before = list(fake.calls)
            with mock.patch('tagging.zotero_write.get_item', side_effect=fake.get_item) as get:
                result = self._verb('/api/notesync/inspect-create-attempt', {'citation_key': 'nozot'}, recs)
            self.assertEqual(result.status_code, 200, result.get_json())
            self.assertEqual(result.get_json()['remote'], 'matches_acknowledgement')
            self.assertEqual(result.get_json()['source'], 'unchanged')
            self.assertEqual(get.call_args.args[:3], ('https://api.example', 'users/1', 'NEWNOTE1'))
            self.assertEqual(fake.calls, before)
            with mock.patch('tagging.zotero_write.get_item', side_effect=fake.get_item):
                refused = self._verb('/api/notesync/repair-create-journal', {'citation_key': 'nozot'}, recs)
                self.assertEqual(refused.status_code, 400)
                repaired = self._verb('/api/notesync/repair-create-journal', {
                    'citation_key': 'nozot', 'confirm': True,
                    'evidence_token': result.get_json()['evidence_token']}, recs)
                self.assertEqual(repaired.status_code, 200, repaired.get_json())
                self.assertEqual(repaired.get_json()['status'], 'existing')
                unresolved = self._verb('/api/notesync/resolve-create-attempt', {'citation_key': 'nozot'}, recs)
                self.assertEqual(unresolved.status_code, 400)
                resolution = self._verb('/api/notesync/resolve-create-attempt', {
                    'citation_key': 'nozot', 'confirm': True,
                    'evidence_token': result.get_json()['evidence_token'],
                    'source_sha256': result.get_json()['source_sha256']}, recs)
                self.assertEqual(resolution.status_code, 200, resolution.get_json())
                self.assertFalse(resolution.get_json()['automatic_retry_allowed'])
            self.assertEqual(fake.calls, before)

    def test_a_creation_that_could_not_take_the_lease_says_it_is_unjournalled(self):
        """Indexing holds the lease -> the note is created in Zotero and never
        journalled. `op_id: null` alone is not a sentence a user reads."""
        from rag.vault import obsidian_manager
        import contextlib
        raw = "---\ntags: [dep]\n---\n\n**T**\n\n---\n\nmy reading note\n"
        recs = [self._rec_keyed("nozot", raw=raw, html=None)]
        stack, _ = self._zotero({"NOZOTAAA": {"itemType": "journalArticle", "title": "T nozot", "DOI": "10.1/x", "version": 3}})
        with stack, contextlib.ExitStack() as st:
            st.enter_context(self._fresh(recs))
            for patcher in self._ctx(recs):
                st.enter_context(patcher)
            st.enter_context(mock.patch.object(obsidian_manager, 'try_acquire_lock', return_value=None))
            response = self.client.post('/api/notesync/create-zotero-note',
                                        json={'citation_key': 'nozot', 'confirm': True}, headers=_HEADERS)
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(body['note_key'], 'NEWNOTE1')
        self.assertIsNone(body['op_id'])
        self.assertIn('undo record was not saved', body['journal_warning'])

    def test_a_creation_whose_journal_record_is_tombstoned_says_so(self):
        """The op_id is now derived from (vault, citation_key) alone, so unlike a
        minted one it CAN collide with a durable tombstone left by an earlier
        reverted-and-pruned creation -- and `save_merged` skips a tombstoned id
        with a bare `continue` where `save` RAISED. The note exists in Zotero
        either way, so its missing undo record must not be silent."""
        import hashlib
        import json as _json
        from tagging import journal as tjournal
        identity = hashlib.sha256(
            _json.dumps([str(self.vault.resolve()), 'nozot']).encode()).hexdigest()
        op_id = 'note-create-' + identity
        # Plant the tombstone the way the journal itself writes one: a saved op
        # that a later prune removes.
        manifest = tjournal.load(self.vault, self.cfg)
        manifest['ops'].append({'op_id': op_id, 'kind': 'zotero_note_create',
                                'ts': tjournal.now_iso(), 'state': 'reverted',
                                'item_key': 'GONE1234'})
        tjournal.save(self.vault, self.cfg, manifest)
        manifest = tjournal.load(self.vault, self.cfg)
        tjournal.prune(self.vault, self.cfg, manifest)
        tjournal.save(self.vault, self.cfg, manifest)
        self.assertIsNone(tjournal.find_op(tjournal.load(self.vault, self.cfg), op_id))

        raw = "---\ntags: [dep]\n---\n\n**T**\n\n---\n\nmy reading note\n"
        recs = [self._rec_keyed("nozot", raw=raw, html=None)]
        stack, _ = self._zotero({"NOZOTAAA": {"itemType": "journalArticle", "title": "T nozot", "DOI": "10.1/x", "version": 3}})
        with stack:
            response = self._verb('/api/notesync/create-zotero-note',
                                  {'citation_key': 'nozot', 'confirm': True}, recs)
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(body['note_key'], 'NEWNOTE1')
        # Pre-fix this returned op_id='note-create-<identity>' -- a value naming
        # a row that does not exist -- with no warning and no log line.
        self.assertIsNone(body['op_id'], body)
        self.assertIn('undo record was not saved', body['journal_warning'])
        self.assertIsNone(tjournal.find_op(tjournal.load(self.vault, self.cfg), op_id))

    def test_repair_verbs_never_read_zotero_while_holding_the_vault_lease(self):
        """`get_item` sleeps on a `Backoff` hint; `_await_backoff` documents that
        sleeping under the op-lock stalls indexing and every other vault write.
        Every other lease in this module reads Zotero first; these must too."""
        from rag.vault import obsidian_manager
        raw = "---\ntags: [dep]\n---\n\n**T**\n\n---\n\nmy reading note\n"
        recs = [self._rec_keyed("nozot", raw=raw, html=None)]
        stack, fake = self._zotero({"NOZOTAAA": {"itemType": "journalArticle", "title": "T nozot", "DOI": "10.1/x", "version": 3}})
        with stack:
            self.assertEqual(self._verb('/api/notesync/create-zotero-note',
                                        {'citation_key': 'nozot', 'confirm': True}, recs).status_code, 200)
            with mock.patch('tagging.zotero_write.get_item', side_effect=fake.get_item):
                token = self._verb('/api/notesync/inspect-create-attempt',
                                   {'citation_key': 'nozot'}, recs).get_json()

            real_acquire, real_release = obsidian_manager.try_acquire_lock, obsidian_manager.release_lock
            for verb, body in (
                ('/api/notesync/repair-create-journal', {'evidence_token': token['evidence_token']}),
                ('/api/notesync/repair-create-keys', {'evidence_token': token['evidence_token'],
                                                      'source_sha256': token['source_sha256']}),
                ('/api/notesync/resolve-create-attempt', {'evidence_token': token['evidence_token'],
                                                          'source_sha256': token['source_sha256']}),
            ):
                order = []

                def acquire(*a, _r=real_acquire, _o=order, **k):
                    epoch = _r(*a, **k)
                    _o.append('lease' if epoch else 'lease-refused')
                    return epoch

                def release(*a, _r=real_release, _o=order, **k):
                    _o.append('release')
                    return _r(*a, **k)

                def read(*a, _o=order, **k):
                    _o.append('zotero-read')
                    return fake.get_item(*a, **k)

                # NOT `_verb`: `_post` installs its own lock stubs, which would
                # replace these recorders. Rebuild the request context instead.
                import contextlib
                with contextlib.ExitStack() as st:
                    st.enter_context(self._fresh(recs))
                    for patcher in self._ctx(recs):
                        st.enter_context(patcher)
                    st.enter_context(mock.patch.object(obsidian_manager, 'try_acquire_lock', acquire))
                    st.enter_context(mock.patch.object(obsidian_manager, 'release_lock', release))
                    st.enter_context(mock.patch('tagging.zotero_write.get_item', side_effect=read))
                    response = self.client.post(
                        verb, json={'citation_key': 'nozot', 'confirm': True, **body}, headers=_HEADERS)
                self.assertEqual(response.status_code, 200, (verb, response.get_json()))
                self.assertIn('zotero-read', order, verb)
                held = order[order.index('lease') + 1:order.index('release')]
                self.assertNotIn('zotero-read', held,
                                 f"{verb} read Zotero while holding the vault lease: {order}")

    def test_created_but_unverified_note_is_journalled_without_rebasing(self):
        from tagging import journal as tjournal
        raw = "---\ntags: [dep]\n---\n\n**T**\n\n---\n\nmy reading note\n"
        recs = [self._rec_keyed("nozot", raw=raw, html=None)]
        stack, _ = self._zotero({"NOZOTAAA": {"itemType": "journalArticle", "title": "T nozot", "DOI": "10.1/x", "version": 3}})
        with stack, mock.patch("notesync.zotero_notes.post_create", return_value=(
                {"ok": True, "status": "created", "verification_pending": True,
                 "message": "Created; verification pending."}, "NEWNOTE1", None)), \
                mock.patch("api.routes.notesync._save_base") as save_base:
            response = self._verb("/api/notesync/create-zotero-note",
                                  {"citation_key": "nozot", "confirm": True}, recs)
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertTrue(result["verification_pending"])
        self.assertEqual(result["note_key"], "NEWNOTE1")
        self.assertIn("zotero-note-key: NEWNOTE1", (self.notes / "nozot.md").read_text())
        op = tjournal.find_op(tjournal.load(self.vault, self.cfg), result["op_id"])
        self.assertEqual(op["version_after"], 0)
        save_base.assert_not_called()

    def test_recover_note_keys_preserves_content_and_refuses_edit_during_snapshot(self):
        from vault_history import journal as rjournal
        raw = "---\ntags: [dep]\nmanual: keep\n---\n\n**T**\n\n---\n\nmy reading note\n"
        recs = [self._rec_keyed('nozot', raw=raw, html=None)]
        stack, fake = self._zotero({'NOZOTAAA': {'itemType': 'journalArticle', 'title': 'T nozot', 'DOI': '10.1/x', 'version': 3}})
        with stack:
            self.assertEqual(self._verb('/api/notesync/create-zotero-note', {'citation_key': 'nozot', 'confirm': True}, recs).status_code, 200)
            path = self.notes / 'nozot.md'
            path.write_text(raw)
            with mock.patch('tagging.zotero_write.get_item', side_effect=fake.get_item), mock.patch('api.routes.notesync._save_base') as base, mock.patch('rag.vault.obsidian_manager.heartbeat', return_value=True):
                inspected = self._verb('/api/notesync/inspect-create-attempt', {'citation_key': 'nozot'}, recs).get_json()
                payload = dict(citation_key='nozot', confirm=True, evidence_token=inspected['evidence_token'], source_sha256=inspected['source_sha256'])
                edited = raw.replace('manual: keep', 'manual: later')
                path.write_text(edited)
                stale = self._verb('/api/notesync/repair-create-keys', payload, recs)
                self.assertEqual(stale.status_code, 409)
                self.assertEqual(path.read_text(), edited)
                path.write_text(raw)
                result = self._verb('/api/notesync/repair-create-keys', payload, recs)
                self.assertEqual(result.status_code, 200, result.get_json())
                text = path.read_text()
                self.assertIn('manual: keep', text)
                self.assertIn('zotero-note-key: NEWNOTE1', text)
                self.assertTrue(text.endswith('my reading note\n'))
                base.assert_not_called()
                path.write_text(raw)
                snapshot = rjournal.write_note_snapshot
                def edit_during_snapshot(*args, **kwargs):
                    result = snapshot(*args, **kwargs)
                    path.write_text(raw + 'later manual edit\n')
                    return result
                with mock.patch('vault_history.journal.write_note_snapshot', side_effect=edit_during_snapshot):
                    refused = self._verb('/api/notesync/repair-create-keys', payload, recs)
                self.assertEqual(refused.status_code, 409, refused.get_json())
                self.assertEqual(path.read_text(), raw + 'later manual edit\n')
                self.assertEqual(refused.get_json()['error'], 'source_changed_before_write')


class TestZoteroNoteReverts(unittest.TestCase):
    """The two reverts, through the same fake transport."""

    def _shim(self, items):
        from tagging import zotero_write as real
        fake = _FakeZotero(items)
        shim = SimpleNamespace(
            get_item=fake.get_item, set_item_note=fake.set_item_note,
            set_item_deleted=fake.set_item_deleted, api_key_from_env=fake.api_key_from_env,
            API_KEY_ENV=real.API_KEY_ENV, ZoteroUnavailable=real.ZoteroUnavailable,
            ZoteroError=real.ZoteroError, ZoteroVersionConflict=real.ZoteroVersionConflict)
        return mock.patch("notesync.zotero_notes.zotero_write", shim), fake

    def test_write_revert_restores_before_html_only_if_unchanged(self):
        from notesync import zotero_notes
        op = {"kind": "zotero_note_write", "item_key": "N1", "lib_path": "users/1",
              "base_url": "https://api.example", "transport": "web",
              "before_html": "<p>old</p>", "after_html": "<p>new</p>", "state": "applied"}
        p, fake = self._shim({"N1": {"itemType": "note", "note": "<p>new</p>", "version": 4}})
        with p:
            res = zotero_notes.revert({}, op)
        self.assertEqual((res["status"], op["state"]), ("reverted", "reverted"))
        self.assertEqual(fake.items["N1"]["data"]["note"], "<p>old</p>")
        op2 = {**op, "state": "applied"}
        p, fake = self._shim({"N1": {"itemType": "note", "note": "<p>edited since</p>", "version": 4}})
        with p:
            res = zotero_notes.revert({}, op2)
        self.assertEqual((res["status"], op2["state"]), ("skipped", "applied"))
        self.assertEqual(fake.calls, [])

    def test_create_revert_trashes_the_note(self):
        from notesync import zotero_notes
        op = {"kind": "zotero_note_create", "item_key": "N9", "lib_path": "users/1",
              "base_url": "https://api.example", "transport": "web",
              "html": "<p>x</p>", "state": "applied", "parent_item": "P1", "version_after": 2}
        p, fake = self._shim({"N9": {"itemType": "note", "note": "<p>x</p>", "parentItem": "P1", "version": 2}})
        op['creation_data'] = dict(fake.items['N9']['data'])
        with p:
            res = zotero_notes.revert({}, op)
        self.assertEqual(res["status"], "reverted")
        self.assertEqual(fake.calls, [("trash", "N9", True)])

    def test_web_op_without_a_key_is_an_error_not_a_revert(self):
        from notesync import zotero_notes
        op = {"kind": "zotero_note_write", "item_key": "N1", "lib_path": "users/1",
              "base_url": "https://api.example", "transport": "web",
              "before_html": "a", "after_html": "b", "state": "applied"}
        p, fake = self._shim({})
        with p, mock.patch("notesync.zotero_notes.zotero_write.api_key_from_env", return_value=None):
            res = zotero_notes.revert({}, op)
        self.assertEqual((res["ok"], res["status"]), (False, "error"))
