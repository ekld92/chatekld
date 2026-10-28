"""`POST /api/notesync/mirror` — one confirmed verb for the whole mirror.

Create + refresh + baseline already existed as three routes, but the tab
offered them as three buttons over two data sources and hid the bootstrap one
in exactly the mode the maintainer runs (zotero-authoritative), so a reading
note written in Zotero could only be mirrored by hand and the note that
created could not be baselined from the app at all.

What is pinned, each proven red against the rule's removal:

* the git gate is evaluated BEFORE any note is created — the refresh phase is
  not journalled, so a dirty folder must refuse the sweep while nothing has
  been written, not after the creates;
* more than `_MAX_CREATE` missing notes creates the cap and SAYS "100 of N";
  the batch is never chunked past it (a second chunk's prune re-exposes the
  first chunk's undo);
* a dry run writes no note, no region and no base;
* a create failure stops the run — it never proceeds to rewrite regions;
* a created note gets a sync point, which is the hole the verb exists to close.
"""

from __future__ import annotations

import contextlib
import json
import time
from unittest import mock

from test_notesync_route import TestSyncState, _HEADERS


def _frames(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            out.append(json.loads(line[6:]))
        except ValueError:
            pass
    return out


class TestMirror(TestSyncState):
    EDITED = ("---\ntags: [dep]\nzotero-key: ABCD1234\nzotero-note-key: WXYZ5678\n---\n\n**T**\n\n"
              "---\n<!-- zotero-note:begin -->\n\nalpha beta MY OWN WORDS\n\n<!-- zotero-note:end -->\n"
              "my tail below the marker\n")

    def setUp(self):
        super().setUp()
        self.cfg["notesync_zotero_authoritative"] = True
        self.addCleanup(self._drain_gate)

    def _drain_gate(self):
        """The SSE guard releases just after the generator ends; don't let a
        still-held gate 429 the next test."""
        from api.routes import notesync
        for _ in range(400):
            if notesync._MIRROR_GATE.acquire(blocking=False):
                notesync._MIRROR_GATE.release()
                return
            time.sleep(0.005)

    def _mirror(self, recs, body=None, *, git_clean=True, extra=(), fresh=None):
        """POST and DRAIN inside the patch scope — the worker is a daemon that
        only runs while the response is consumed, so draining outside it would
        run the real collaborators (CLAUDE.md §Tests)."""
        from rag.vault import obsidian_manager
        with contextlib.ExitStack() as st:
            for p in self._ctx(recs):
                st.enter_context(p)
            st.enter_context(mock.patch("api.routes.notesync._fresh_zotero_notes",
                                        return_value=fresh) if fresh is not None
                             else self._fresh(recs))
            st.enter_context(mock.patch(
                "api.routes.notesync._git_undo_check",
                return_value=(git_clean, "" if git_clean else "1 file(s) git could not restore",
                              "0" * 40)))

            def acquire(ttl=3600):
                return None if not getattr(self, "_lock_available", True) else 1

            st.enter_context(mock.patch.object(obsidian_manager, "try_acquire_lock",
                                               side_effect=acquire))
            st.enter_context(mock.patch.object(obsidian_manager, "heartbeat",
                                               return_value=True))
            st.enter_context(mock.patch.object(obsidian_manager, "release_lock",
                                               side_effect=lambda _e: None))
            for p in extra:
                st.enter_context(p)
            body = dict(body) if body is not None else {"dry_run": True}
            if body.get("confirm") and "plan_digest" not in body:
                # "The user reviewed the dry run": fetch its digest in the same
                # patch scope (the binding is pinned in test_library_workflow_lot1).
                dry = self.client.post("/api/notesync/mirror", json={"dry_run": True},
                                       headers=_HEADERS)
                if dry.status_code == 200:
                    plans = [f["mirror"] for f in _frames(dry.get_data(as_text=True))
                             if "mirror" in f]
                    if plans:
                        body["plan_digest"] = plans[-1]["plan_digest"]
                self._drain_gate()
            r = self.client.post("/api/notesync/mirror", json=body, headers=_HEADERS)
            if r.status_code != 200:
                return r, None
            frames = _frames(r.get_data(as_text=True))
        return r, frames

    @staticmethod
    def _terminal(frames):
        hits = [f["mirror"] for f in frames if "mirror" in f]
        assert hits, f"no terminal mirror frame: {frames}"
        return hits[-1]

    def _base(self, key):
        from notesync import base_store
        return base_store.load(self.vault, key)

    # --- gates ------------------------------------------------------------

    def test_uncertain_create_receipt_survives_terminal_mirror_frame(self):
        receipt = {"ok": False, "status": "uncertain",
                   "reason": "publication_uncertain", "op_id": "fixture-op",
                   "rel": "Z_Zotero_Notes/m2020.md"}
        payload = {"created": 0, "results": {"m2020": receipt},
                   "created_bases": [], "cancelled": False, "not_reached": []}
        _response, frames = self._mirror(
            [self._rec_keyed("m2020", has_note=False)], {"confirm": True},
            extra=[mock.patch("api.routes.notesync._create_notes", return_value=(payload, 200))])
        terminal = self._terminal(frames)
        self.assertEqual(terminal["create_results"]["m2020"], receipt)
        self.assertIn("publication_uncertain", terminal["failed"]["m2020"])
        self.assertFalse(terminal["ok"])

    def test_refused_while_the_mode_is_off(self):
        self.cfg["notesync_zotero_authoritative"] = False
        recs = [self._rec_keyed("m2020", has_note=False)]
        r, _ = self._mirror(recs, {"confirm": True})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "mode_off")
        self.assertFalse((self.notes / "m2020.md").exists())

    def test_the_git_gate_is_checked_before_any_note_is_created(self):
        """The refresh phase's undo is the vault's git, and it runs AFTER the
        creates — so a region git cannot restore has to stop the run with
        nothing written, not after the creates. Since 2026-09-21 the proof is
        per replaced file and needs the plan, so it runs first thing in the
        worker (a terminal ``stopped`` frame) rather than as a pre-stream 409 —
        still before the first create."""
        recs = [self._rec_keyed("m2020", has_note=False),
                self._rec_keyed(raw=self.EDITED)]
        r, frames = self._mirror(recs, {"confirm": True}, git_clean=False)
        d = self._terminal(frames)
        self.assertEqual(d["stopped"], "git_not_clean")
        self.assertFalse(d["ok"])
        self.assertEqual((d["created"], d["refreshed"]), (0, 0))
        self.assertFalse((self.notes / "m2020.md").exists())
        self.assertEqual((self.notes / "a2020.md").read_text(encoding="utf-8"), self.EDITED)

    def test_a_dry_run_needs_no_clean_git_and_still_plans(self):
        recs = [self._rec_keyed("m2020", has_note=False),
                self._rec_keyed(raw=self.EDITED)]
        _r, frames = self._mirror(recs, git_clean=False)
        d = self._terminal(frames)
        self.assertTrue(d["dry_run"])
        self.assertEqual((d["to_create"], d["to_refresh"]), (1, 1))
        self.assertFalse(d["git_clean"])

    # --- the cap ----------------------------------------------------------

    def test_more_than_the_cap_creates_the_cap_and_says_so(self):
        from api.routes.notesync import _MAX_CREATE
        recs = [self._rec_keyed(f"m{i:03d}", has_note=False)
                for i in range(_MAX_CREATE + 1)]
        _r, frames = self._mirror(recs, {"confirm": True})
        d = self._terminal(frames)
        self.assertEqual((d["to_create"], d["create_total"], d["create_capped_at"]),
                         (_MAX_CREATE, _MAX_CREATE + 1, _MAX_CREATE))
        self.assertEqual(d["created"], _MAX_CREATE)
        # Never chunked past it: exactly the cap exists on disk.
        self.assertEqual(len(list(self.notes.glob("m*.md"))), _MAX_CREATE)

    # --- dry run ----------------------------------------------------------

    def test_a_dry_run_writes_no_note_no_region_and_no_base(self):
        recs = [self._rec_keyed("m2020", has_note=False),
                self._rec_keyed(raw=self.EDITED)]
        _r, frames = self._mirror(recs)
        d = self._terminal(frames)
        self.assertTrue(d["dry_run"])
        self.assertEqual((d["to_create"], d["to_refresh"], d["to_baseline"]), (1, 1, 2))
        self.assertEqual((d["created"], d["refreshed"], d["bootstrapped"]), (0, 0, 0))
        self.assertFalse((self.notes / "m2020.md").exists())
        self.assertEqual((self.notes / "a2020.md").read_text(encoding="utf-8"), self.EDITED)
        self.assertIsNone(self._base("a2020"))
        self.assertIsNone(self._base("m2020"))

    # --- ordering ---------------------------------------------------------

    def test_a_create_failure_does_not_proceed_to_refresh(self):
        """An unreadable restore journal refuses the whole create batch. The
        sweep must stop there: rewriting regions afterwards would apply the
        not-journalled half of a run whose journalled half never happened."""
        from vault_history import journal
        recs = [self._rec_keyed("m2020", has_note=False),
                self._rec_keyed(raw=self.EDITED)]
        boom = mock.patch.object(
            journal, "load",
            side_effect=journal.JournalUnreadableError("manifest unreadable"))
        _r, frames = self._mirror(recs, {"confirm": True}, extra=[boom])
        d = self._terminal(frames)
        # The damage the rule prevents, asserted first so a regression names it.
        self.assertEqual((self.notes / "a2020.md").read_text(encoding="utf-8"), self.EDITED)
        self.assertEqual((d["created"], d["refreshed"]), (0, 0))
        self.assertFalse((self.notes / "m2020.md").exists())
        self.assertEqual(d["stopped"], "create_failed")
        self.assertFalse(d["ok"])

    # --- the whole sweep --------------------------------------------------

    def test_one_run_creates_refreshes_and_baselines(self):
        recs = [self._rec_keyed("m2020", has_note=False),
                self._rec_keyed(raw=self.EDITED)]
        _r, frames = self._mirror(recs, {"confirm": True})
        d = self._terminal(frames)
        self.assertTrue(d["ok"], d)
        self.assertEqual((d["created"], d["refreshed"]), (1, 1))
        self.assertEqual(d["failed"], {})
        # 1. created
        self.assertTrue((self.notes / "m2020.md").exists())
        # 2. refreshed — region only, tail and frontmatter byte-for-byte
        after = (self.notes / "a2020.md").read_text(encoding="utf-8")
        self.assertTrue(after.startswith("---\ntags: [dep]\nzotero-key: ABCD1234\n"))
        self.assertTrue(after.endswith("<!-- zotero-note:end -->\nmy tail below the marker\n"))
        self.assertNotIn("MY OWN WORDS", after)
        # 3. baselined, both of them
        self.assertIsNotNone(self._base("a2020"))
        self.assertGreaterEqual(d["bootstrapped"], 1)

    def test_a_created_note_gets_a_sync_point(self):
        """The hole this verb closes. The cached scan does not know the new
        note exists, so `bootstrap` cannot see it — only the text phase 1 just
        wrote can record its base, and that is why phase 3 runs last."""
        recs = [self._rec_keyed("m2020", has_note=False)]
        _r, frames = self._mirror(recs, {"confirm": True})
        d = self._terminal(frames)
        self.assertEqual((d["created"], d["bootstrapped"]), (1, 1))
        base = self._base("m2020")
        self.assertIsNotNone(base)
        self.assertIn("alpha beta", base.base_region)
        self.assertEqual(base.base_html, "<p>alpha beta</p>")
        self.assertEqual(base.zotero_version, 7)
        self.assertEqual(base.note_key, "WXYZ5678")

    def test_a_reading_note_written_since_the_scan_is_mirrored(self):
        """`/status`'s missing list is the cached scan's Zotero side; the mirror
        reads Zotero FRESH, so a reading note written since the scan is created
        from today's text instead of being skipped as `no_reading_note`."""
        rec = self._rec_keyed("m2020", has_note=False, html="<p>written today</p>")
        written_today = rec.zotero_item.child_notes[0]
        rec.zotero_item.child_notes = []          # the scan saw no reading note
        _r, frames = self._mirror(
            [rec], {"confirm": True},
            fresh={rec.zotero_item.item_key: [written_today]})
        d = self._terminal(frames)
        self.assertEqual(d["created"], 1)
        self.assertIn("written today",
                      (self.notes / "m2020.md").read_text(encoding="utf-8"))
        self.assertIn("written today", self._base("m2020").base_region)

    def test_two_reading_notes_under_one_item_are_never_guessed_at(self):
        rec = self._rec_keyed("m2020", has_note=False)
        second = type(rec.zotero_item.child_notes[0])(
            note_key="OTHER123", body_html="<p>other</p>", body_plain="", version=3)
        _r, frames = self._mirror(
            [rec], {"confirm": True},
            fresh={rec.zotero_item.item_key: [rec.zotero_item.child_notes[0], second]})
        d = self._terminal(frames)
        self.assertEqual(d["created"], 0)
        # Since 2026-09-21 the row is classified `multiple_notes` by the sync
        # read itself (it used to read `missing_obsidian` and be refused at
        # create time), so the plan lists it rather than offering it.
        self.assertEqual(d["listed"].get("multiple_notes"), ["m2020"])
        self.assertEqual(d["to_create"], 0)
        self.assertFalse((self.notes / "m2020.md").exists())

    # --- 2026-09-20 review of 92371234: two gaps the first set left open ---

    def test_a_cancel_before_the_first_create_creates_nothing(self):
        """The cancel gate in front of phase 1 had no test: removing it left
        the whole file green (mutation M6 of the review)."""
        import threading
        from api.routes import notesync as ns
        recs = [self._rec_keyed("m2020", has_note=False)]
        frames: list[dict] = []
        cancel = threading.Event()
        cancel.set()
        with contextlib.ExitStack() as st:
            for p in self._ctx(recs):
                st.enter_context(p)
            st.enter_context(self._fresh(recs))
            inv, settings = self._inv(recs)
            fresh = ns._fresh_zotero_notes(None)
            # The reviewed plan's digest, as a confirm carries it (lot 1, 2026-09-21).
            digest = ns._mirror_plan(inv, settings, fresh, self.cfg)[0]["digest"]
            ns._run_mirror(frames.append, cancel, inv, settings, fresh, self.cfg,
                           dry_run=False, plan_digest=digest)
        d = self._terminal(frames)
        self.assertEqual(d["stopped"], "cancelled")
        self.assertFalse((self.notes / "m2020.md").exists())
        self.assertIsNone(self._base("m2020"))

    def test_a_run_with_no_region_to_rewrite_does_not_need_the_op_lock(self):
        """`_refresh_write` is called unconditionally by the mirror. Before
        the review fix it took the obsidian op-lock even with nothing planned,
        so a sweep that only had a stale base to delete stopped at
        `indexing` while indexing ran — for a phase that writes no vault
        file at all. The base store and the stale deletions never needed the
        lock; only the note writes do."""
        from notesync import base_store
        from notesync.state import BaseRecord
        base_store.save(self.vault, BaseRecord(
            citation_key="gone2020", item_key="GONE1234", note_key="N",
            zotero_version=1, base_html="<p>x</p>", base_region="x",
            synced_at="2026-01-01T00:00:00+00:00", converter_id="whatever"))
        recs = [self._rec_keyed()]          # in step, nothing to refresh
        self._lock_available = False        # indexing holds the op-lock
        _r, frames = self._mirror(recs, {"confirm": True})
        d = self._terminal(frames)
        self.assertIsNone(d["stopped"], d)
        self.assertEqual(d["stale_bases_removed"], 1)
        self.assertIsNone(self._base("gone2020"))
