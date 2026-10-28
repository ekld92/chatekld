"""`POST /api/notesync/refresh-all` — the zotero-authoritative sweep
(maintainer decision 2026-09-19: reading notes are edited only in Zotero and
`Z_Zotero_Notes/` is a read-only mirror).

What is pinned, each proven red against the rule's removal:

* the verb is refused while the mode is off (a two-way-sync user must never
  find a one-click "delete every edit in the folder" on the tab);
* a dry run plans and writes nothing; the write refuses on a dirty git tree;
* the write rewrites ONLY the marked region — frontmatter, header and the
  user's tail below the end marker are byte-for-byte — and does so even when
  the region holds words Zotero lacks (there is no acknowledgement step:
  by decision none of the words are the user's);
* a note whose reading note or item is gone is LISTED, never written or
  deleted, and a base whose note is gone is removed.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from test_notesync_route import TestSyncState


class TestRefreshAll(TestSyncState):
    EDITED = ("---\ntags: [dep]\nzotero-key: ABCD1234\nzotero-note-key: WXYZ5678\n---\n\n**T**\n\n"
              "---\n<!-- zotero-note:begin -->\n\nalpha beta MY OWN WORDS\n\n<!-- zotero-note:end -->\n"
              "my tail below the marker\n")

    def setUp(self):
        super().setUp()
        self.cfg["notesync_zotero_authoritative"] = True

    def _refresh(self, recs, body=None, *, git_clean=True):
        """A confirm without a digest stands for "the user reviewed the dry
        run": it runs the dry run first, in the same patch scope, and sends
        its ``plan_digest`` (2026-09-21 lot 1: a confirm must be bound to a
        reviewed plan — the binding itself is pinned in
        test_library_workflow_lot1.py::TestPlanBinding)."""
        body = dict(body or {"dry_run": True})
        with self._fresh(recs), mock.patch(
                "api.routes.notesync._git_undo_check",
                return_value=(git_clean, "" if git_clean else "dirty", "0" * 40)):
            if body.get("confirm") and "plan_digest" not in body:
                dry = self._post("/api/notesync/refresh-all", {"dry_run": True}, recs)
                if dry.status_code == 200:
                    body["plan_digest"] = dry.get_json()["plan_digest"]
            return self._post("/api/notesync/refresh-all", body, recs)

    def test_refused_while_the_mode_is_off(self):
        self.cfg["notesync_zotero_authoritative"] = False
        recs = [self._rec_keyed(raw=self.EDITED)]
        r = self._refresh(recs, {"confirm": True})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "mode_off")
        self.assertEqual((self.notes / "a2020.md").read_text(encoding="utf-8"), self.EDITED)

    def test_dry_run_plans_and_writes_nothing(self):
        recs = [self._rec_keyed(raw=self.EDITED), self._rec_keyed("b2021")]
        d = self._refresh(recs).get_json()
        self.assertTrue(d["dry_run"])
        self.assertEqual((d["planned"], d["planned_keys"], d["unchanged"]), (1, ["a2020"], 1))
        self.assertEqual((self.notes / "a2020.md").read_text(encoding="utf-8"), self.EDITED)

    def test_write_refuses_on_a_dirty_git_tree(self):
        recs = [self._rec_keyed(raw=self.EDITED)]
        r = self._refresh(recs, {"confirm": True}, git_clean=False)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "git_not_clean")
        self.assertEqual((self.notes / "a2020.md").read_text(encoding="utf-8"), self.EDITED)

    def test_write_replaces_only_the_region_even_over_the_users_words(self):
        recs = [self._rec_keyed(raw=self.EDITED)]
        d = self._refresh(recs, {"confirm": True}).get_json()
        self.assertEqual((d["written"], d["failed"], d["aborted"]), (1, {}, False))
        after = (self.notes / "a2020.md").read_text(encoding="utf-8")
        self.assertTrue(after.startswith("---\ntags: [dep]\nzotero-key: ABCD1234\n"))
        self.assertTrue(after.endswith("<!-- zotero-note:end -->\nmy tail below the marker\n"))
        self.assertNotIn("MY OWN WORDS", after)
        self.assertIn("alpha beta", after)
        # And the base records the text just written, so the next status
        # reads in_sync rather than asking again.
        self.assertEqual(self._status(recs)["counts"], {"in_sync": 1})

    def test_a_region_equal_after_the_converter_is_not_rewritten(self):
        # `**_x_**` and `***x***` are the same text to the converter; a byte
        # plan would rewrite every such note into the escaped spelling.
        spelled = self.RAW.replace("alpha beta", "**_alpha_** beta")
        recs = [self._rec_keyed(raw=spelled, html="<p><strong><em>alpha</em></strong> beta</p>")]
        d = self._refresh(recs).get_json()
        self.assertEqual((d["planned"], d["unchanged"]), (0, 1))
        d = self._refresh(recs, {"confirm": True}).get_json()
        self.assertEqual((d["written"], d["bases_written"]), (0, 1))
        self.assertEqual((self.notes / "a2020.md").read_text(encoding="utf-8"), spelled)
        self.assertEqual(self._status(recs)["counts"], {"in_sync": 1})

    def test_a_note_gone_from_zotero_is_listed_never_written_or_deleted(self):
        gone = self._rec_keyed("gone2019", html=None)
        (self.notes / "gone2019.md").write_text(self.EDITED, encoding="utf-8")
        (self.notes / "orphan2018.md").write_text(self.EDITED, encoding="utf-8")
        recs = [self._rec_keyed(raw=self.EDITED), gone]
        d = self._refresh(recs, {"confirm": True}).get_json()
        self.assertEqual(d["listed"], {"deleted_in_zotero": ["gone2019"], "no_record": ["orphan2018"]})
        self.assertEqual(d["written"], 1)
        for stem in ("gone2019", "orphan2018"):
            self.assertEqual((self.notes / f"{stem}.md").read_text(encoding="utf-8"), self.EDITED)

    def test_a_stale_base_is_removed_and_a_missing_one_written(self):
        from notesync import base_store
        from notesync.state import BaseRecord
        recs = [self._rec_keyed()]
        base_store.save(self.vault, BaseRecord(
            citation_key="renamed2000", item_key="X", note_key="Y", zotero_version=1,
            base_html="<p>x</p>", base_region="\nx\n", synced_at="2026-01-01T00:00:00+00:00",
            converter_id="whatever"))
        d = self._refresh(recs).get_json()
        self.assertEqual(d["stale_bases"], 1)
        d = self._refresh(recs, {"confirm": True}).get_json()
        self.assertEqual((d["written"], d["bases_written"], d["stale_bases_removed"]), (0, 1, 1))
        self.assertIsNone(base_store.load(self.vault, "renamed2000"))
        self.assertIsNotNone(base_store.load(self.vault, "a2020"))
        self.assertEqual(self._status(recs)["counts"], {"in_sync": 1})

    def test_a_listed_note_keeps_its_base(self):
        # Review 2026-09-19: the sweep deleted the base of every note it merely
        # LISTED (e.g. a reading note not synced down yet), so the note came
        # back unbaselined once Zotero caught up. Only a base whose note is
        # gone from disk is stale.
        from notesync import base_store
        from notesync.state import BaseRecord
        gone = self._rec_keyed("gone2019", html=None)
        (self.notes / "gone2019.md").write_text(self.EDITED, encoding="utf-8")
        base_store.save(self.vault, BaseRecord(
            citation_key="gone2019", item_key="X", note_key="Y", zotero_version=1,
            base_html="<p>x</p>", base_region="\nx\n", synced_at="2026-01-01T00:00:00+00:00",
            converter_id="whatever"))
        d = self._refresh([self._rec_keyed(), gone], {"confirm": True}).get_json()
        self.assertEqual(d["stale_bases_removed"], 0)
        self.assertIsNotNone(base_store.load(self.vault, "gone2019"))

    def test_push_and_merge_are_refused_in_the_mode(self):
        # The mirror is never written to Zotero: every caller (tab, CLI,
        # cleanup stage 05b) goes through these routes.
        recs = [self._rec_keyed(raw=self.EDITED)]
        with self._fresh(recs):
            r = self._post("/api/notesync/push", {"citation_keys": ["a2020"], "confirm": True}, recs)
            self.assertEqual((r.status_code, r.get_json()["reason"]), (409, "authoritative"))
            r = self._post("/api/notesync/merge", {"citation_key": "a2020", "confirm": True,
                                                   "result": "x"}, recs)
            self.assertEqual((r.status_code, r.get_json()["reason"]), (409, "authoritative"))

    def test_sync_status_reports_the_mode(self):
        recs = [self._rec_keyed()]
        self.assertTrue(self._status(recs)["authoritative"])
        self.cfg["notesync_zotero_authoritative"] = False
        self.assertFalse(self._status(recs)["authoritative"])


class TestConfigKey(TestSyncState):
    def test_the_key_is_a_validated_bool_with_a_false_default(self):
        from api.routes.config import _CONFIG_VALIDATORS
        from core.config import _DEFAULTS
        self.assertIs(_DEFAULTS["notesync_zotero_authoritative"], False)
        self.assertIs(_CONFIG_VALIDATORS["notesync_zotero_authoritative"]("true"), True)
        self.assertIs(_CONFIG_VALIDATORS["notesync_zotero_authoritative"]("no"), False)


_ = Path  # keep the import honest for readers grepping fixture paths


class TestGitGate:
    """`_git_undo_check` against a real repository: a git-ignored notes
    folder reads clean to `git status`, yet git holds nothing to undo with."""

    def _repo(self, tmp_path):
        import subprocess
        vault = tmp_path / "v"
        notes = vault / "Z_Zotero_Notes"
        notes.mkdir(parents=True)
        (notes / "a.md").write_text("x\n", encoding="utf-8")
        run = lambda *a: subprocess.run(["git", "-C", str(vault), *a], check=True,  # noqa: E731
                                        capture_output=True)
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        return vault, notes, run

    def test_an_ignored_folder_is_refused(self, tmp_path):
        from api.routes.notesync import _git_undo_check
        vault, notes, run = self._repo(tmp_path)
        (vault / ".gitignore").write_text("Z_Zotero_Notes/\n", encoding="utf-8")
        run("add", ".gitignore")
        run("commit", "-qm", "i")
        ok, detail, _commit = _git_undo_check(vault, {notes / "a.md": "x\n"})
        assert not ok and "not committed" in detail

    def test_a_committed_folder_passes(self, tmp_path):
        from api.routes.notesync import _git_undo_check
        vault, notes, run = self._repo(tmp_path)
        run("add", ".")
        run("commit", "-qm", "c")
        ok, detail, commit = _git_undo_check(vault, {notes / "a.md": "x\n"})
        assert (ok, detail) == (True, "") and commit
