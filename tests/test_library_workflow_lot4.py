"""Library-workflow lot 4: the Zotero → Obsidian mirror writes nothing it does
not have to.

Plan §8 acceptance: "a second run without change rewrites nothing". Found live
on 2026-09-21 (lot 1 Known Issues, item h): the notes classified `unparsed`
(images, unsupported markup — 156 on the maintainer's library) were planned
as `keep_base` with `_needs_base` true on EVERY run, so each refresh / mirror
rewrote their base records although nothing had changed. Hermetic, on the
notesync route fixtures (throwaway vault, simulated Zotero).
"""
from __future__ import annotations

from unittest import mock

import test_notesync_route as _route_tests  # module import: no re-collection

from notesync.convert import html_to_md


def _own_tests_only(cls):
    for base in cls.__mro__[1:]:
        for name in vars(base):
            if name.startswith("test_") and name not in vars(cls):
                setattr(cls, name, None)
    return cls


IMG_HTML = '<p>alpha beta</p><p><img data-attachment-key="IMG12345"></p>'


@_own_tests_only
class TestSecondRunWritesNoBase(_route_tests.TestSyncState):
    def setUp(self):
        super().setUp()
        self.cfg["notesync_zotero_authoritative"] = True

    def _unparsed_rec(self):
        region = html_to_md(IMG_HTML)
        raw = ("---\ntags: [dep]\nzotero-key: ABCD1234\nzotero-note-key: WXYZ5678\n---\n\n**T**\n\n"
               f"---\n<!-- zotero-note:begin -->\n\n{region}\n\n<!-- zotero-note:end -->\n")
        return self._rec_keyed(raw=raw, html=IMG_HTML)

    def _run(self, recs, path="/api/notesync/refresh-all"):
        with self._fresh(recs), mock.patch(
                "api.routes.notesync._git_undo_check", return_value=(True, "", "0" * 40)):
            dry = self._post(path, {"dry_run": True}, recs)
            self.assertEqual(dry.status_code, 200, dry.get_json())
            return dry.get_json(), self._post(
                path, {"confirm": True, "plan_digest": dry.get_json()["plan_digest"]}, recs).get_json()

    def test_an_unparsed_note_gets_its_base_once_then_never_again(self):
        recs = [self._unparsed_rec()]
        self.assertEqual(self._status(recs)["rows"][0]["state"], "unparsed")
        _, first = self._run(recs)
        self.assertEqual((first["written"], first["bases_written"]), (0, 1))
        dry, second = self._run(recs)
        self.assertEqual(second["bases_written"], 0, second)
        self.assertEqual(second["written"], 0)

    def test_an_in_sync_note_is_left_alone_on_the_second_run(self):
        recs = [self._rec_keyed()]
        _, first = self._run(recs)
        self.assertEqual(first["bases_written"], 1)
        _, second = self._run(recs)
        self.assertEqual((second["written"], second["bases_written"]), (0, 0))

    def test_a_zotero_edit_still_reaches_the_base(self):
        """Control: skipping identical bases must not skip a CHANGED one."""
        recs = [self._unparsed_rec()]
        self._run(recs)
        rec = recs[0]
        rec.zotero_item.child_notes[0].version = 9  # re-saved in Zotero, same text
        _, again = self._run(recs)
        self.assertEqual(again["bases_written"], 1)
        from notesync import base_store
        self.assertEqual(base_store.load(self.vault, "a2020").zotero_version, 9)


import test_notesync_mirror as _mirror_tests  # noqa: E402


@_own_tests_only
class TestMirrorAcceptance(_mirror_tests.TestMirror):
    """Plan §8: three or four new notes are copied; a second run with no
    change (and no rescan in between) writes nothing and reports no failure."""

    def test_new_notes_are_copied_once_and_a_second_run_writes_nothing(self):
        recs = [self._rec_keyed(k, has_note=False) for k in ("n2026a", "n2026b", "n2026c")]
        recs.append(self._rec_keyed())  # an existing note already in step
        _r, frames = self._mirror(recs, {"confirm": True})
        first = self._terminal(frames)
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["created"], 3)
        outside = {k: (self.notes / f"{k}.md").read_bytes() for k in ("n2026a", "n2026b", "n2026c")}
        self._drain_gate()
        _r, frames = self._mirror(recs, {"confirm": True})
        second = self._terminal(frames)
        self.assertTrue(second["ok"], second)
        self.assertEqual((second["to_create"], second["created"], second["refreshed"],
                          second["bases_written"], second["bootstrapped"]), (0, 0, 0, 0, 0), second)
        for k, before in outside.items():
            self.assertEqual((self.notes / f"{k}.md").read_bytes(), before)


# ---------------------------------------------------------------------------
# Adversarial review of lot 4 (2026-09-21). The case-variant, Missing-list
# and note-key tests were red on 198324a8.
# ---------------------------------------------------------------------------

import os  # noqa: E402

@_own_tests_only
class TestCreatedSinceScanFallback(_route_tests.TestSyncState):
    def setUp(self):
        super().setUp()
        self.cfg["notesync_zotero_authoritative"] = True

    def _run(self, recs, path="/api/notesync/refresh-all", git=(True, "", "0" * 40)):
        with self._fresh(recs), mock.patch(
                "api.routes.notesync._git_undo_check", return_value=git):
            dry = self._post(path, {"dry_run": True}, recs).get_json()
            conf = self._post(path, {"confirm": True, "plan_digest": dry["plan_digest"]}, recs).get_json()
            return dry, conf

    def test_case_variant_orphan_is_not_adopted_by_another_key(self):
        """An orphan note `VanDerX2020.md` (old casing of a key, listed as
        `no_record`) must not become record `vanderx2020`'s mirror on a
        case-insensitive volume."""
        rec = self._rec_keyed("vanderx2020", has_note=False, html="<p>new text</p>")
        (self.notes / "VanDerX2020.md").write_text(self.RAW, encoding="utf-8")
        recs = [rec]
        row = self._status(recs)["rows"][0]
        dry, _ = (None, None)
        with self._fresh(recs):
            dry = self._post("/api/notesync/refresh-all", {"dry_run": True}, recs).get_json()
        both = ("VanDerX2020" in dry["listed"].get("no_record", [])
                and "vanderx2020" in dry["planned_keys"])
        self.assertFalse(both, "same file is both an orphan (no_record) and the record's planned rewrite")
        self.assertEqual(row["state"], "missing_obsidian")

    def test_case_variant_rewrite_renames_file(self):
        rec = self._rec_keyed("vanderx2020", has_note=False, html="<p>new text</p>")
        (self.notes / "VanDerX2020.md").write_text(self.RAW, encoding="utf-8")
        recs = [rec]
        dry, conf = self._run(recs)
        self.assertIn("VanDerX2020.md", os.listdir(self.notes),
                      "the orphan note was silently renamed / rewritten under another key")

    def test_status_missing_list_vs_sync_rows_after_create(self):
        """After /create (no rescan), /status still lists the key as missing
        while sync-status no longer does; the tab's narrowed Missing list
        (missing minus fresh missing_obsidian) then offers [create] again."""
        rec = self._rec_keyed("n2026a", has_note=False)
        recs = [rec]
        with self._fresh(recs):
            self._post("/api/notesync/create", {"citation_keys": ["n2026a"], "confirm": True}, recs).get_json()
        st = self._get("/api/notesync/status", recs).get_json()
        rows = self._status(recs)["rows"]
        fresh_missing = {r["citation_key"] for r in rows if r["state"] == "missing_obsidian"}
        shown = [m["citation_key"] for m in st["missing"] if m["citation_key"] not in fresh_missing]
        self.assertEqual(shown, [], "narrowed Missing list re-offers [create] for a note that exists")

    def test_symlink_at_candidate_stays_missing(self):
        rec = self._rec_keyed("s2020", has_note=False)
        target = self.tmp / "elsewhere.md"
        target.write_text(self.RAW, encoding="utf-8")
        os.symlink(target, self.notes / "s2020.md")
        row = self._status([rec])["rows"][0]
        self.assertEqual(row["state"], "missing_obsidian")

    def test_needs_base_skips_after_note_key_change_same_text(self):
        """Zotero note re-created: new note key, same text, same version."""
        from notesync import base_store
        recs = [self._rec_keyed()]
        self._run(recs)
        recs[0].zotero_item.child_notes[0].note_key = "NEWKEY99"
        _, again = self._run(recs)
        self.assertEqual(base_store.load(self.vault, "a2020").note_key, "NEWKEY99")


@_own_tests_only
class TestCreatedSinceScanUnderGit(TestCreatedSinceScanFallback):
    def test_case_variant_blocks_whole_sweep_under_real_git(self):
        import subprocess
        rec = self._rec_keyed("vanderx2020", has_note=False, html="<p>new text</p>")
        (self.notes / "VanDerX2020.md").write_text(self.RAW, encoding="utf-8")
        other = self._rec_keyed("b2021", html="<p>changed</p>")  # a normal note needing a refresh
        def g(*a):
            return subprocess.run(["git", "-C", str(self.vault), *a], check=True, capture_output=True)
        g("init", "-q")
        g("config", "user.email", "x@y")
        g("config", "user.name", "x")
        g("config", "commit.gpgsign", "false")
        g("add", "-A")
        g("commit", "-qm", "init")
        recs = [rec, other]
        with self._fresh(recs):
            dry = self._post("/api/notesync/refresh-all", {"dry_run": True}, recs).get_json()
        self.assertTrue(dry.get("git_restorable"), dry.get("git_detail"))
