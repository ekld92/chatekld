"""Library-workflow plan, lot 1 (2026-09-21): the existing note operations made
safe before a global "update my library" button can drive them.

Each class reproduces one defect from the 2026-09-21 audit on an ISOLATED
fixture — a throwaway vault, a throwaway git repository, the suite's throwaway
``BASE_DIR`` for the base store and journals, and simulated Zotero reads — and
pins the corrected behaviour. None of these tests touches a real vault, a real
Zotero database or the network.

Defects covered (numbering follows the plan's §5):

1. git undo coverage — a partly git-ignored notes folder passed the folder
   gate, so a rewrite could land on a file git holds nothing of;
2. zotero-authoritative mode — ``create-zotero-note`` could still send the
   Obsidian region to Zotero;
3. note rename race — an external edit between the verification and the
   unlink of the old name was destroyed;
4. Canvas references — ``.canvas`` file nodes were not read, and an unreadable
   file was skipped as if it linked nothing;
5. creation from stale data — ``/create`` rendered the SCAN's reading note, so
   a note deleted (or duplicated) in Zotero since the scan was still created;
6. result counts — a note write followed by a failed base save was counted as
   a saved base, and a failed base deletion escaped as a 500;
7. concurrent bases — an old plan deleted a base written after it was made;
8. partial cancellation — a cancel landed only between whole phases;
9. preview ≠ execution — a confirm executed whatever the world looked like at
   confirm time, not what the reviewer approved.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Imported as a MODULE: a class bound at this module's top level is collected
# again by pytest under this file's name (it would re-run the whole parent suite).
import test_notesync_route as _route_tests

_HEADERS = _route_tests._HEADERS


def _own_tests_only(cls):
    """Run only the tests this class defines.

    The classes below subclass the existing notesync suites to reuse their
    fixture helpers (throwaway vault, simulated Zotero, patched op-lock);
    without this, every inherited ``test_*`` would be collected and run a
    second time under the new name.
    """
    for base in cls.__mro__[1:]:
        for name in vars(base):
            if name.startswith("test_") and name not in vars(cls):
                setattr(cls, name, None)
    return cls


def _frames(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            try:
                out.append(json.loads(line[6:]))
            except ValueError:
                pass
    return out


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


def _git_init(vault: Path) -> None:
    _git(vault, "init", "-q")
    _git(vault, "config", "user.email", "fixture@example.invalid")
    _git(vault, "config", "user.name", "Fixture")
    _git(vault, "config", "commit.gpgsign", "false")


EDITED = ("---\ntags: [dep]\nzotero-key: ABCD1234\nzotero-note-key: WXYZ5678\n---\n\n**T**\n\n"
          "---\n<!-- zotero-note:begin -->\n\nalpha beta MY OWN WORDS\n\n<!-- zotero-note:end -->\n"
          "my tail below the marker\n")


@_own_tests_only
class _Authoritative(_route_tests.TestSyncState):
    def setUp(self):
        super().setUp()
        self.cfg["notesync_zotero_authoritative"] = True

    def _refresh(self, recs, body, *, fresh=None):
        patch = (mock.patch("api.routes.notesync._fresh_zotero_notes", return_value=fresh)
                 if fresh is not None else self._fresh(recs))
        with patch:
            return self._post("/api/notesync/refresh-all", body, recs)

    def _reviewed_refresh(self, recs):
        """Dry run, then confirm with whatever binding the dry run offered —
        the flow a reviewing user follows."""
        plan = self._refresh(recs, {"dry_run": True}).get_json()
        body = {"confirm": True}
        if plan.get("plan_digest"):
            body["plan_digest"] = plan["plan_digest"]
        return self._refresh(recs, body)


# ---------------------------------------------------------------------------
# 1. git undo coverage — real git, no patching of the check
# ---------------------------------------------------------------------------

@_own_tests_only
class TestGitUndoCoverage(_Authoritative):
    def _repo(self, *, ignored=(), dirty_after=(), untracked_extra=False):
        recs = [self._rec_keyed("a2020", raw=EDITED), self._rec_keyed("b2021", raw=EDITED)]
        _git_init(self.vault)
        if ignored:
            (self.vault / ".gitignore").write_text(
                "".join(f"Z_Zotero_Notes/{k}.md\n" for k in ignored), encoding="utf-8")
        _git(self.vault, "add", "-A")
        _git(self.vault, "commit", "-q", "-m", "fixture")
        for k in dirty_after:
            (self.notes / f"{k}.md").write_text(EDITED + "edited after the commit\n",
                                                encoding="utf-8")
        if untracked_extra:
            (self.notes / "scratch.md").write_text("unrelated\n", encoding="utf-8")
        return recs

    def test_an_ignored_note_in_a_partly_tracked_folder_refuses_the_rewrite(self):
        recs = self._repo(ignored=("b2021",))
        r = self._reviewed_refresh(recs)
        self.assertEqual(r.status_code, 409, r.get_json())
        body = r.get_json()
        self.assertEqual(body["reason"], "git_not_clean")
        self.assertIn("b2021", body["error"])
        # Nothing written — not even the tracked note — so the sweep has one
        # undo story, not two.
        for k in ("a2020", "b2021"):
            self.assertEqual((self.notes / f"{k}.md").read_text(encoding="utf-8"), EDITED)

    def test_a_note_edited_since_the_commit_refuses(self):
        recs = self._repo(dirty_after=("a2020",))
        r = self._reviewed_refresh(recs)
        self.assertEqual(r.status_code, 409, r.get_json())
        self.assertIn("a2020", r.get_json()["error"])

    def test_unrelated_changes_in_the_folder_do_not_block_a_covered_rewrite(self):
        recs = self._repo(untracked_extra=True)
        r = self._reviewed_refresh(recs)
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["written"], 2)
        # The commit that restores every replaced file is named, so "undo with
        # git" is an exact instruction rather than a hope.
        head = _git(self.vault, "rev-parse", "HEAD").strip()
        self.assertEqual(d["git_restore_commit"], head)

    def test_the_check_names_the_commit_holding_the_exact_pre_write_bytes(self):
        from api.routes.notesync import _git_undo_check
        self._repo(ignored=("b2021",))
        ok, detail, commit = _git_undo_check(
            self.vault, {self.notes / "a2020.md": EDITED})
        self.assertTrue(ok, detail)
        self.assertEqual(commit, _git(self.vault, "rev-parse", "HEAD").strip())
        ok, detail, _ = _git_undo_check(self.vault, {self.notes / "a2020.md": EDITED + "x"})
        self.assertFalse(ok)
        ok, detail, _ = _git_undo_check(self.vault, {self.notes / "b2021.md": EDITED})
        self.assertFalse(ok)
        self.assertIn("b2021", detail)

    def test_no_repository_refuses(self):
        from api.routes.notesync import _git_undo_check
        ok, detail, commit = _git_undo_check(self.vault, {self.notes / "x.md": "x"})
        self.assertFalse(ok)
        self.assertIsNone(commit)


# ---------------------------------------------------------------------------
# 2. zotero-authoritative mode covers every outbound verb
# ---------------------------------------------------------------------------

@_own_tests_only
class TestAuthoritativeBlocksCreateInZotero(_route_tests.TestPushPullMerge):
    def test_create_zotero_note_is_refused_without_any_post(self):
        self.cfg["notesync_zotero_authoritative"] = True
        raw = "---\ntags: [dep]\n---\n\n**T**\n\n---\n\nmy reading note\n"
        recs = [self._rec_keyed("nozot", raw=raw, html=None)]
        stack, fake = self._zotero({"NOZOTAAA": {"itemType": "journalArticle",
                                                 "title": "T nozot", "version": 3}})
        with stack:
            r = self._verb("/api/notesync/create-zotero-note",
                           {"citation_key": "nozot", "confirm": True}, recs)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "authoritative")
        self.assertEqual(fake.calls, [])
        self.assertEqual((self.notes / "nozot.md").read_text(encoding="utf-8"), raw)


# ---------------------------------------------------------------------------
# 3. note rename race
# ---------------------------------------------------------------------------

@_own_tests_only
class TestRenameRace(_route_tests.TestSyncState):
    def _run(self, mutate):
        from vault_history import journal, rename_note as rn
        old = self.notes / "old2020.md"
        before = b"---\ntags: []\n---\nbody\n"
        old.write_bytes(before)
        after = b"---\ntags: []\naliases: [old2020]\n---\nbody\n"
        real = rn._publish_no_replace

        def publish_then_external_edit(dest, data):
            real(dest, data)
            if dest.name == "new2020.md":
                mutate(old)

        with mock.patch.object(rn, "_publish_no_replace", side_effect=publish_then_external_edit):
            jman = journal.load(self.vault, self.cfg)
            return rn.rename_note(self.vault, self.cfg, "Z_Zotero_Notes/old2020.md",
                                  "Z_Zotero_Notes/new2020.md", before, after, jman)

    def test_an_edit_after_verification_is_preserved(self):
        op_id, status, err = self._run(
            lambda p: p.write_bytes(b"---\ntags: []\n---\nbody EDITED ELSEWHERE\n"))
        self.assertEqual(status, "failed", err)
        self.assertEqual((self.notes / "old2020.md").read_bytes(),
                         b"---\ntags: []\n---\nbody EDITED ELSEWHERE\n")
        # Our copy, built from the pre-edit bytes, is withdrawn so the edit is
        # the only version of the note.
        self.assertFalse((self.notes / "new2020.md").exists())

    def test_a_replacement_after_verification_is_preserved(self):
        def replace(p):
            tmp = p.with_name("repl.tmp")
            tmp.write_bytes(b"a different file\n")
            os.replace(tmp, p)
        _op, status, _err = self._run(replace)
        self.assertEqual(status, "failed")
        self.assertEqual((self.notes / "old2020.md").read_bytes(), b"a different file\n")

    def test_no_stash_is_left_behind_on_success(self):
        _op, status, err = self._run(lambda p: None)
        self.assertEqual(status, "applied", err)
        self.assertFalse((self.notes / "old2020.md").exists())
        self.assertEqual([p.name for p in self.notes.iterdir()], ["new2020.md"])


# ---------------------------------------------------------------------------
# 4. Canvas references and incomplete inspection
# ---------------------------------------------------------------------------

@_own_tests_only
class TestIncomingLinksCanvas(_route_tests.TestSyncState):
    def _links(self):
        from api.routes.notesync import _incoming_links
        return _incoming_links(self.vault, {"old2020"}, set())

    def test_a_canvas_file_node_is_a_reference(self):
        (self.vault / "board.canvas").write_text(json.dumps({"nodes": [
            {"id": "1", "type": "file", "file": "Z_Zotero_Notes/old2020.md"}]}), encoding="utf-8")
        links, unverified = self._links()
        self.assertEqual(links, {"old2020": ["board.canvas"]})
        self.assertEqual(unverified, [])

    def test_a_canvas_text_node_wikilink_is_a_reference(self):
        (self.vault / "board.canvas").write_text(json.dumps({"nodes": [
            {"id": "1", "type": "text", "text": "see [[old2020|the note]]"}]}), encoding="utf-8")
        links, _ = self._links()
        self.assertEqual(links, {"old2020": ["board.canvas"]})

    def test_a_malformed_canvas_is_reported_not_skipped(self):
        (self.vault / "board.canvas").write_text("{not json", encoding="utf-8")
        _links, unverified = self._links()
        self.assertEqual(unverified, ["board.canvas"])

    def test_an_unreadable_note_is_reported_not_skipped(self):
        p = self.vault / "private.md"
        p.write_text("[[old2020]]", encoding="utf-8")
        p.chmod(0)
        self.addCleanup(p.chmod, 0o644)
        _links, unverified = self._links()
        self.assertEqual(unverified, ["private.md"])

    def test_the_rename_refuses_when_inspection_is_incomplete(self):
        from api.routes.notesync import _key_change_rows
        old = self.notes / "old2020.md"
        old.write_text("---\nzotero-key: ABCD1234\n---\nbody\n", encoding="utf-8")
        (self.vault / "board.canvas").write_text("{broken", encoding="utf-8")
        rec = SimpleNamespace(obsidian_note=None, bib_entry=SimpleNamespace(),
                              zotero_item=SimpleNamespace(item_key="ABCD1234"))
        inv = SimpleNamespace(records={"new2020": rec}, notes_without_record=[old])
        settings = SimpleNamespace(vault_root=self.vault, zotero_notes_dir=self.notes)
        rows = _key_change_rows(inv, settings)
        self.assertEqual(rows[0]["state"], "links_unverified")
        self.assertEqual(rows[0]["unverified"], ["board.canvas"])


# ---------------------------------------------------------------------------
# 5. creation reads today's Zotero, never the scan's
# ---------------------------------------------------------------------------

@_own_tests_only
class TestCreateReadsFreshZotero(_route_tests.TestSyncState):
    def _create(self, recs, fresh):
        with mock.patch("api.routes.notesync._fresh_zotero_notes", return_value=fresh):
            return self._post("/api/notesync/create",
                              {"citation_keys": [r.citation_key for r in recs],
                               "confirm": True}, recs)

    def test_a_reading_note_deleted_since_the_scan_is_not_created(self):
        recs = [self._rec_keyed("m2020", has_note=False)]
        d = self._create(recs, {}).get_json()
        self.assertEqual(d["results"]["m2020"], {"ok": False, "reason": "no_reading_note"})
        self.assertFalse((self.notes / "m2020.md").exists())

    def test_two_reading_notes_are_a_decision(self):
        recs = [self._rec_keyed("m2020", has_note=False)]
        item_key = recs[0].zotero_item.item_key
        two = [SimpleNamespace(note_key="N1", body_html="<p>one</p>", body_plain="", version=1),
               SimpleNamespace(note_key="N2", body_html="<p>two</p>", body_plain="", version=1)]
        d = self._create(recs, {item_key: two}).get_json()
        self.assertEqual(d["results"]["m2020"]["reason"], "multiple_notes")
        self.assertFalse((self.notes / "m2020.md").exists())

    def test_the_created_note_holds_todays_text(self):
        recs = [self._rec_keyed("m2020", has_note=False, html="<p>scan text</p>")]
        item_key = recs[0].zotero_item.item_key
        today = [SimpleNamespace(note_key="WXYZ5678", body_html="<p>written since the scan</p>",
                                 body_plain="", version=9)]
        d = self._create(recs, {item_key: today}).get_json()
        self.assertTrue(d["results"]["m2020"]["ok"], d)
        text = (self.notes / "m2020.md").read_text(encoding="utf-8")
        self.assertIn("written since the scan", text)
        self.assertNotIn("scan text", text)

    def test_an_unreadable_zotero_refuses_rather_than_using_the_scan(self):
        recs = [self._rec_keyed("m2020", has_note=False)]
        with mock.patch("api.routes.notesync._fresh_zotero_notes",
                        side_effect=OSError("Zotero database unreadable")):
            r = self._post("/api/notesync/create",
                           {"citation_keys": ["m2020"], "confirm": True}, recs)
        self.assertEqual(r.status_code, 503)
        self.assertFalse((self.notes / "m2020.md").exists())


# ---------------------------------------------------------------------------
# 6 + 7 + 8. counts, concurrent bases, cancellation — at the writer
# ---------------------------------------------------------------------------

@_own_tests_only
class TestRefreshWriter(_Authoritative):
    def _plan(self, recs):
        from api.routes import notesync as ns
        inv, settings = self._inv(recs)
        fresh = {r.zotero_item.item_key: list(r.zotero_item.child_notes)
                 for r in recs if r.zotero_item.child_notes}
        with mock.patch("api.routes.notesync.load_config", return_value=self.cfg):
            return ns._refresh_plan(inv, settings, fresh, self.cfg)

    @contextlib.contextmanager
    def _lock(self):
        from rag.vault import obsidian_manager
        with mock.patch.object(obsidian_manager, "try_acquire_lock", return_value=1), \
                mock.patch.object(obsidian_manager, "heartbeat", return_value=True), \
                mock.patch.object(obsidian_manager, "release_lock"):
            yield

    def test_a_failed_base_save_is_not_counted_as_a_saved_base(self):
        from api.routes import notesync as ns
        recs = [self._rec_keyed("a2020", raw=EDITED)]
        plan = self._plan(recs)
        with self._lock(), mock.patch("notesync.base_store.save",
                                      side_effect=OSError("disk full")):
            done = ns._refresh_write(self.vault, plan)
        self.assertEqual(done["written"], 1)
        self.assertEqual(done["bases_written"], 0)
        self.assertEqual(done["base_failed"], {"a2020": "OSError"})

    def test_a_failed_stale_base_deletion_is_reported_per_key(self):
        from api.routes import notesync as ns
        from notesync import base_store
        from notesync.state import BaseRecord
        base_store.save(self.vault, BaseRecord(
            citation_key="gone2020", item_key="G", note_key="N", zotero_version=1,
            base_html="<p>x</p>", base_region="x", synced_at="t", converter_id="c"))
        recs = [self._rec_keyed("a2020")]
        plan = self._plan(recs)
        self.assertEqual(plan["stale_bases"], ["gone2020"])
        with self._lock(), mock.patch("os.unlink", side_effect=PermissionError("denied")):
            done = ns._refresh_write(self.vault, plan)
        self.assertEqual(done["stale_bases_removed"], 0)
        self.assertEqual(done["stale_base_failed"], {"gone2020": "PermissionError"})

    def test_an_old_plan_never_deletes_a_base_written_after_it(self):
        from api.routes import notesync as ns
        from notesync import base_store
        from notesync.state import BaseRecord

        def base(region):
            return BaseRecord(citation_key="gone2020", item_key="G", note_key="N",
                              zotero_version=1, base_html="<p>x</p>", base_region=region,
                              synced_at="t", converter_id="c")
        base_store.save(self.vault, base("old"))
        plan = self._plan([self._rec_keyed("a2020")])
        self.assertEqual(plan["stale_bases"], ["gone2020"])
        # A concurrent mirror recreates the note and records a NEW base.
        base_store.save(self.vault, base("new"))
        with self._lock():
            done = ns._refresh_write(self.vault, plan)
        self.assertEqual(done["stale_bases_removed"], 0)
        self.assertEqual(base_store.load(self.vault, "gone2020").base_region, "new")

    def test_a_note_recreated_since_the_plan_keeps_its_base(self):
        from api.routes import notesync as ns
        from notesync import base_store
        from notesync.state import BaseRecord
        base_store.save(self.vault, BaseRecord(
            citation_key="gone2020", item_key="G", note_key="N", zotero_version=1,
            base_html="<p>x</p>", base_region="x", synced_at="t", converter_id="c"))
        plan = self._plan([self._rec_keyed("a2020")])
        (self.notes / "gone2020.md").write_text("back\n", encoding="utf-8")
        with self._lock():
            done = ns._refresh_write(self.vault, plan)
        self.assertEqual(done["stale_bases_removed"], 0)
        self.assertIsNotNone(base_store.load(self.vault, "gone2020"))

    def test_cancel_stops_before_the_next_note_and_keeps_the_boundary(self):
        from api.routes import notesync as ns
        recs = [self._rec_keyed("a2020", raw=EDITED), self._rec_keyed("b2021", raw=EDITED)]
        plan = self._plan(recs)
        calls = []

        def should_cancel():
            calls.append(1)
            return len(calls) > 1          # the first note proceeds, then cancel
        with self._lock():
            done = ns._refresh_write(self.vault, plan, should_cancel=should_cancel)
        self.assertTrue(done["cancelled"])
        self.assertEqual(done["written"], 1)
        # The note written got its base (the boundary already engaged)…
        self.assertEqual(done["bases_written"], 1)
        # …and the other is byte-for-byte untouched.
        untouched = [k for k in ("a2020", "b2021")
                     if (self.notes / f"{k}.md").read_text(encoding="utf-8") == EDITED]
        self.assertEqual(len(untouched), 1)


@_own_tests_only
class TestCreateCancel(_route_tests.TestSyncState):
    def test_cancel_between_creates_keeps_the_journal_and_reports_the_rest(self):
        from api.routes import notesync as ns
        from rag.vault import obsidian_manager
        from vault_history import journal
        recs = [self._rec_keyed(k, has_note=False) for k in ("m1", "m2", "m3")]
        inv, settings = self._inv(recs)
        fresh = {r.zotero_item.item_key: list(r.zotero_item.child_notes) for r in recs}
        cancel = threading.Event()
        import vault_history.create_note as cn
        real = cn.create_note

        def create_then_cancel(*a, **k):
            out = real(*a, **k)
            cancel.set()
            return out
        with mock.patch.object(obsidian_manager, "try_acquire_lock", return_value=1), \
                mock.patch.object(obsidian_manager, "release_lock"), \
                mock.patch.object(cn, "create_note", side_effect=create_then_cancel):
            payload, status = ns._create_notes(inv, settings, self.cfg, ["m1", "m2", "m3"],
                                               fresh=fresh, should_cancel=cancel.is_set)
        self.assertEqual(status, 200)
        self.assertEqual(payload["created"], 1)
        self.assertTrue(payload["cancelled"])
        self.assertEqual(sorted(payload["not_reached"]), ["m2", "m3"])
        # The one create that landed is undoable: the journal was saved.
        ops = [o for o in journal.load(self.vault, self.cfg)["ops"] if o.get("kind") == "create_note"]
        self.assertEqual(len(ops), 1)


# ---------------------------------------------------------------------------
# 9. approval bound to the reviewed plan
# ---------------------------------------------------------------------------

@_own_tests_only
class TestPlanBinding(_Authoritative):
    def _patch_git(self):
        return mock.patch("api.routes.notesync._git_undo_check",
                          return_value=(True, "", "0" * 40))

    def test_refresh_confirm_without_a_reviewed_plan_is_refused(self):
        recs = [self._rec_keyed("a2020", raw=EDITED)]
        with self._patch_git():
            r = self._refresh(recs, {"confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "plan_digest_required")
        self.assertEqual((self.notes / "a2020.md").read_text(encoding="utf-8"), EDITED)

    def test_refresh_refuses_when_zotero_moved_after_the_preview(self):
        recs = [self._rec_keyed("a2020", raw=EDITED)]
        with self._patch_git():
            plan = self._refresh(recs, {"dry_run": True}).get_json()
            self.assertTrue(plan["plan_digest"])
            recs[0].zotero_item.child_notes[0].body_html = "<p>alpha beta gamma</p>"
            recs[0].zotero_item.child_notes[0].version = 8
            r = self._refresh(recs, {"confirm": True, "plan_digest": plan["plan_digest"]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "plan_changed")
        self.assertEqual((self.notes / "a2020.md").read_text(encoding="utf-8"), EDITED)

    def test_refresh_proceeds_when_nothing_moved(self):
        recs = [self._rec_keyed("a2020", raw=EDITED)]
        with self._patch_git():
            plan = self._refresh(recs, {"dry_run": True}).get_json()
            r = self._refresh(recs, {"confirm": True, "plan_digest": plan["plan_digest"]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["written"], 1)

    def _mirror(self, recs, body, *, git=None):
        from rag.vault import obsidian_manager
        with contextlib.ExitStack() as st:
            for p in self._ctx(recs):
                st.enter_context(p)
            st.enter_context(self._fresh(recs))
            st.enter_context(git or self._patch_git())
            st.enter_context(mock.patch.object(obsidian_manager, "try_acquire_lock", return_value=1))
            st.enter_context(mock.patch.object(obsidian_manager, "heartbeat", return_value=True))
            st.enter_context(mock.patch.object(obsidian_manager, "release_lock"))
            r = self.client.post("/api/notesync/mirror", json=body, headers=_HEADERS)
            if r.status_code != 200:
                return r, None
            frames = _frames(r.get_data(as_text=True))
        from api.routes import notesync
        for _ in range(400):                      # the SSE guard releases late
            if notesync._MIRROR_GATE.acquire(blocking=False):
                notesync._MIRROR_GATE.release()
                break
            threading.Event().wait(0.005)
        return r, [f["mirror"] for f in frames if "mirror" in f][-1]

    def test_mirror_refuses_when_a_new_note_appeared_after_the_preview(self):
        recs = [self._rec_keyed("m2020", has_note=False)]
        _r, plan = self._mirror(recs, {"dry_run": True})
        recs.append(self._rec_keyed("n2021", has_note=False))
        r, done = self._mirror(recs, {"confirm": True, "plan_digest": plan["plan_digest"]})
        self.assertEqual(done["stopped"], "plan_changed")
        self.assertEqual(done["created"], 0)
        self.assertFalse((self.notes / "m2020.md").exists())

    def test_mirror_confirm_without_a_reviewed_plan_is_refused(self):
        recs = [self._rec_keyed("m2020", has_note=False)]
        r, _ = self._mirror(recs, {"confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "plan_digest_required")

    def test_creation_only_mirror_needs_no_git(self):
        """Lot 4 acceptance, pinned here because the gate moved: creates are
        journalled, so a run with nothing to rewrite never consults git."""
        recs = [self._rec_keyed("m2020", has_note=False)]
        _r, plan = self._mirror(recs, {"dry_run": True})
        never = mock.patch("api.routes.notesync._git_undo_check",
                           side_effect=AssertionError("git consulted"))
        r, done = self._mirror(recs, {"confirm": True, "plan_digest": plan["plan_digest"]},
                               git=never)
        self.assertTrue(done["ok"], done)
        self.assertEqual(done["created"], 1)


# ---------------------------------------------------------------------------
# 9 (driver + CLI, through the REAL route). The review found the first CLI
# tests mocked `cli_main._drv` — above the driver seam — so a renamed body
# key would have left them green. These replay the driver's actual HTTP
# request into the Flask route.
# ---------------------------------------------------------------------------

@_own_tests_only
class TestPlanBindingThroughTheDriver(_Authoritative):
    @contextlib.contextmanager
    def _wired(self, recs):
        import io
        import re
        import urllib.error

        from rag.vault import obsidian_manager
        client = self.client

        class _Resp:
            def __init__(self, status, body):
                self.status, self._b = status, io.BytesIO(body)

            def read(self, *a):
                return self._b.read(*a)

            def readline(self, *a):
                return self._b.readline(*a)

            def __iter__(self):
                return iter(self._b.readlines())

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def getcode(self):
                return self.status

        def urlopen(req, timeout=None):
            path = re.search(r"(/api/.*)$", req.full_url).group(1)
            resp = client.open(path, method=req.get_method(), data=req.data,
                               headers=dict(req.header_items()))
            body = resp.get_data()
            if resp.status_code >= 400:
                raise urllib.error.HTTPError(req.full_url, resp.status_code, resp.status,
                                             resp.headers, io.BytesIO(body))
            return _Resp(resp.status_code, body)

        with contextlib.ExitStack() as st:
            for p in self._ctx(recs):
                st.enter_context(p)
            st.enter_context(self._fresh(recs))
            st.enter_context(mock.patch("api.routes.notesync._git_undo_check",
                                        return_value=(True, "", "0" * 40)))
            st.enter_context(mock.patch.object(obsidian_manager, "try_acquire_lock",
                                               return_value=1))
            st.enter_context(mock.patch.object(obsidian_manager, "heartbeat", return_value=True))
            st.enter_context(mock.patch.object(obsidian_manager, "release_lock"))
            st.enter_context(mock.patch("core.driver.urllib.request.urlopen",
                                        side_effect=urlopen))
            st.enter_context(mock.patch("core.driver.resolve_base_url",
                                        return_value="http://127.0.0.1:1"))
            yield

    def test_the_driver_confirm_carries_the_digest_the_route_reads(self):
        from core import driver
        recs = [self._rec_keyed("a2020", raw=EDITED)]
        with self._wired(recs):
            plan = driver.notesync_refresh_all(dry_run=True)
            self.assertTrue(plan.get("plan_digest"), plan)
            bad = driver.notesync_refresh_all(dry_run=False, plan_digest="0" * 64)
            self.assertIn("error", bad)
            self.assertEqual((self.notes / "a2020.md").read_text(encoding="utf-8"), EDITED)
            done = driver.notesync_refresh_all(dry_run=False, plan_digest=plan["plan_digest"])
        self.assertEqual(done.get("written"), 1, done)
        self.assertTrue(done.get("ok"), done)

    def test_the_cli_exits_non_zero_when_the_mirror_refuses_inside_the_stream(self):
        from click.testing import CliRunner

        from cli import main as cli_main
        recs = [self._rec_keyed("m2020", has_note=False)]
        with self._wired(recs):
            res = CliRunner().invoke(cli_main.cli, ["notesync", "mirror", "--confirm",
                                                    "--plan-digest", "0" * 64])
        self.assertEqual(res.exit_code, 1, res.output)
        self.assertIn("plan_changed", res.output)
        self.assertFalse((self.notes / "m2020.md").exists())
        from api.routes import notesync
        for _ in range(400):
            if notesync._MIRROR_GATE.acquire(blocking=False):
                notesync._MIRROR_GATE.release()
                break
            threading.Event().wait(0.005)


# ---------------------------------------------------------------------------
# 9 (CLI half). The command line cannot send an unbound confirm either.
# ---------------------------------------------------------------------------

def test_cli_confirm_without_a_reviewed_digest_is_refused_before_any_request():
    from click.testing import CliRunner

    from cli import main as cli_main
    for command in ("mirror", "refresh-all"):
        with mock.patch.object(cli_main._drv, "notesync_mirror") as mirror, \
                mock.patch.object(cli_main._drv, "notesync_refresh_all") as refresh:
            res = CliRunner().invoke(cli_main.cli, ["notesync", command, "--confirm"])
        assert res.exit_code == 2, res.output
        assert "--plan-digest" in res.output
        mirror.assert_not_called()
        refresh.assert_not_called()


# ---------------------------------------------------------------------------
# 1 (review follow-up, 2026-09-21). Cases the adversarial review reproduced
# against the first cut of the git check, each on a throwaway repository.
# ---------------------------------------------------------------------------

def _repo_with(tmp_path, files: dict[str, bytes], *, config=(), attrs: str = ""):
    vault = tmp_path / "vault"
    vault.mkdir()
    _git_init(vault)
    for k, v in config:
        _git(vault, "config", k, v)
    if attrs:
        (vault / ".gitattributes").write_text(attrs, encoding="utf-8")
    for name, data in files.items():
        p = vault / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    _git(vault, "add", "-A")
    _git(vault, "commit", "-q", "-m", "fixture")
    return vault


def test_git_check_refuses_a_file_checkout_would_restore_with_other_line_endings(tmp_path):
    """autocrlf=true, committed LF, LF on disk: the raw blob matches, but
    checkout writes CRLF — git cannot hand back the pre-write bytes."""
    from notesync import git_undo
    vault = _repo_with(tmp_path, {"n/a.md": b"x\ny\n"})
    _git(vault, "config", "core.autocrlf", "true")
    ok, detail, _ = git_undo.check(vault, {vault / "n/a.md": "x\ny\n"})
    assert not ok and "a.md" in detail


def test_git_check_passes_a_file_checkout_restores_exactly_under_autocrlf(tmp_path):
    from notesync import git_undo
    vault = _repo_with(tmp_path, {"n/a.md": b"x\ny\n"})
    _git(vault, "config", "core.autocrlf", "true")
    (vault / "n/a.md").write_bytes(b"x\r\ny\r\n")     # what checkout produces
    ok, detail, _ = git_undo.check(vault, {vault / "n/a.md": "x\r\ny\r\n"})
    assert ok, detail


def test_git_check_matches_an_nfd_file_name(tmp_path):
    import unicodedata
    from notesync import git_undo
    nfd = unicodedata.normalize("NFD", "Méd.md")
    vault = _repo_with(tmp_path, {f"n/{nfd}": b"x\n"}, config=[("core.precomposeunicode", "true")])
    paths = [p for p in (vault / "n").iterdir()]
    ok, detail, _ = git_undo.check(vault, {paths[0]: "x\n"})
    assert ok, detail


def test_git_check_locates_files_through_a_differently_cased_vault_path(tmp_path):
    from notesync import git_undo
    vault = _repo_with(tmp_path, {"Notes/a.md": b"x\n"})
    other = Path(str(vault).replace("/vault", "/VAULT"))
    if not other.exists():          # case-sensitive volume: nothing to test
        return
    ok, detail, _ = git_undo.check(other, {other / "Notes" / "a.md": "x\n"})
    assert ok, detail


def test_git_check_refuses_a_symlinked_note(tmp_path):
    from notesync import git_undo
    vault = _repo_with(tmp_path, {"archive/a.md": b"x\n"})
    (vault / "n").mkdir()
    (vault / "n" / "a.md").symlink_to("../archive/a.md")
    ok, detail, _ = git_undo.check(vault, {vault / "n" / "a.md": "x\n"})
    assert not ok and "symbolic link" in detail


def test_git_check_refuses_a_filtered_file_without_running_the_filter(tmp_path):
    from notesync import git_undo
    vault = _repo_with(tmp_path, {"n/a.md": b"x\n"}, attrs="*.md filter=lfs\n")
    ok, detail, _ = git_undo.check(vault, {vault / "n/a.md": "x\n"})
    assert not ok and "filter" in detail


# ---------------------------------------------------------------------------
# Review follow-up (2026-09-21): links, walk, canvas, rename, base store,
# mirror accounting — each case the adversarial review reproduced.
# ---------------------------------------------------------------------------

def _links_in(tmp_path, files: dict[str, str]):
    from api.routes.notesync import _incoming_links
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return _incoming_links(tmp_path, {"old2020"}, set())


def test_link_shapes_the_first_cut_missed(tmp_path):
    for text in ("| a | [[old2020\\|Old]] |", '[s](old2020.md "title")', "[s](<notes/old2020.md>)",
                 "[s](<my folder/old2020.md> 'title')"):
        links, _ = _links_in(tmp_path / str(abs(hash(text))), {"x.md": text})
        assert links == {"old2020": ["x.md"]}, text


def test_an_unreadable_folder_is_reported_not_skipped(tmp_path):
    from api.routes.notesync import _incoming_links
    d = tmp_path / "locked"
    d.mkdir()
    (d / "x.md").write_text("[[old2020]]", encoding="utf-8")
    d.chmod(0)
    try:
        _links, unverified = _incoming_links(tmp_path, {"old2020"}, set())
    finally:
        d.chmod(0o755)
    assert unverified == ["locked"]


def test_a_symlinked_folder_is_followed_once(tmp_path):
    from api.routes.notesync import _incoming_links
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "x.md").write_text("[[old2020]]", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "linked").symlink_to(real)
    (vault / "loop").symlink_to(vault)            # a cycle must not hang
    links, unverified = _incoming_links(vault, {"old2020"}, set())
    assert links == {"old2020": ["linked/x.md"]} and unverified == []


def test_empty_bom_and_null_canvases_reference_nothing_and_block_nothing(tmp_path):
    for body in (b"", b"\xef\xbb\xbf" + b'{"nodes": []}', b'{"nodes": null}'):
        vault = tmp_path / str(len(body)) / "v"
        vault.mkdir(parents=True)
        (vault / "b.canvas").write_bytes(body)
        from api.routes.notesync import _incoming_links
        links, unverified = _incoming_links(vault, {"old2020"}, set())
        assert (links, unverified) == ({}, []), body


@_own_tests_only
class TestKeyChangeListingAgreesWithTheRename(_route_tests.TestSyncState):
    def test_a_link_from_another_orphan_note_is_not_hidden(self):
        from api.routes.notesync import _key_change_rows
        a = self.notes / "aOld2020.md"
        a.write_text("---\nzotero-key: GONE0000\n---\nsee [[bOld2020]]\n", encoding="utf-8")
        b = self.notes / "bOld2020.md"
        b.write_text("---\nzotero-key: BBBB0000\n---\nbody\n", encoding="utf-8")
        rec = SimpleNamespace(obsidian_note=None, bib_entry=SimpleNamespace(),
                              zotero_item=SimpleNamespace(item_key="BBBB0000"))
        inv = SimpleNamespace(records={"bNew2020": rec}, notes_without_record=[a, b])
        settings = SimpleNamespace(vault_root=self.vault, zotero_notes_dir=self.notes)
        listing = {r["old_key"]: r["state"] for r in _key_change_rows(inv, settings)}
        single = _key_change_rows(inv, settings, only="bOld2020")[0]["state"]
        assert listing["bOld2020"] == single == "linked_from"

    def test_a_note_linking_to_itself_is_still_renamable(self):
        from api.routes.notesync import _key_change_rows
        b = self.notes / "bOld2020.md"
        b.write_text("---\nzotero-key: BBBB0000\n---\nsee [[bOld2020#x]]\n", encoding="utf-8")
        rec = SimpleNamespace(obsidian_note=None, bib_entry=SimpleNamespace(),
                              zotero_item=SimpleNamespace(item_key="BBBB0000"))
        inv = SimpleNamespace(records={"bNew2020": rec}, notes_without_record=[b])
        settings = SimpleNamespace(vault_root=self.vault, zotero_notes_dir=self.notes)
        assert _key_change_rows(inv, settings)[0]["state"] == "ready"

    def test_a_kept_hidden_version_is_recorded_durably(self):
        """Put-back refused because the old name was recreated: the user's
        bytes survive only in the hidden file, so its path is on the op and in
        the vault-write log."""
        from vault_history import journal, rename_note as rn
        old = self.notes / "old2020.md"
        before = b"---\ntags: []\n---\nbody\n"
        old.write_bytes(before)
        real_rename = os.rename

        def rename_then_race(src, dst):
            real_rename(src, dst)
            Path(dst).write_bytes(b"edited in place\n")   # in-place edit after the move
            Path(src).write_bytes(b"recreated by the editor\n")
        with mock.patch.object(rn.os, "rename", side_effect=rename_then_race), \
                mock.patch.object(journal, "log_vault_write") as logged:
            jman = journal.load(self.vault, self.cfg)
            op_id, status, err = rn.rename_note(
                self.vault, self.cfg, "Z_Zotero_Notes/old2020.md", "Z_Zotero_Notes/new2020.md",
                before, b"after\n", jman)
        assert status == "failed"
        op = next(o for o in jman["ops"] if o["id"] == op_id)
        stash = self.vault / op["stash_rel"]
        assert stash.read_bytes() == b"edited in place\n"
        assert any(c.args[0] == "rename_note_stash_kept" for c in logged.call_args_list)


def test_the_store_lock_refuses_to_nest_instead_of_deadlocking(tmp_path):
    import pytest

    from notesync import base_store
    with base_store.store_lock(tmp_path):
        with pytest.raises(RuntimeError):
            with base_store.store_lock(tmp_path):
                pass


def test_deleting_an_absent_base_needs_no_writable_store(tmp_path):
    from notesync import base_store
    d = base_store.store_dir(tmp_path)
    d.mkdir(parents=True)
    d.chmod(0o500)
    try:
        assert base_store.delete_if_unchanged(tmp_path, "nobody", "x") == "gone"
    finally:
        d.chmod(0o755)


@_own_tests_only
class TestReviewMirrorAccounting(_Authoritative):
    def test_two_reading_notes_without_a_mirror_are_multiple_notes_not_missing(self):
        recs = [self._rec_keyed("m2020", has_note=False)]
        second = SimpleNamespace(note_key="N2", body_html="<p>two</p>", body_plain="", version=1)
        recs[0].zotero_item.child_notes.append(second)
        assert self._status(recs)["counts"] == {"multiple_notes": 1}

    def test_a_refusal_never_hands_out_a_digest_to_replay(self):
        recs = [self._rec_keyed("a2020", raw=EDITED)]
        with mock.patch("api.routes.notesync._git_undo_check", return_value=(True, "", "0" * 40)):
            r = self._refresh(recs, {"confirm": True, "plan_digest": "0" * 64})
        assert r.status_code == 409 and "plan_digest" not in r.get_json()

    def test_the_create_digest_binds_the_rendered_header(self):
        from api.routes import notesync as ns
        recs = [self._rec_keyed("m2020", has_note=False)]
        inv, settings = self._inv(recs)
        fresh = {r.zotero_item.item_key: list(r.zotero_item.child_notes) for r in recs}
        with mock.patch("api.routes.notesync.load_config", return_value=self.cfg):
            before = ns._mirror_plan(inv, settings, fresh, self.cfg)[0]["digest"]
            recs[0].bib_entry.title = "A DIFFERENT TITLE AFTER A RESCAN"
            recs[0].zotero_item.title = "A DIFFERENT TITLE AFTER A RESCAN"
            ns._clear_states_cache()
            after = ns._mirror_plan(inv, settings, fresh, self.cfg)[0]["digest"]
        assert before != after

    def test_a_crash_inside_the_create_loop_keeps_the_undo_of_landed_creates(self):
        from api.routes import notesync as ns
        from rag.vault import obsidian_manager
        from vault_history import journal
        import vault_history.create_note as cn
        recs = [self._rec_keyed(k, has_note=False) for k in ("m1", "m2")]
        inv, settings = self._inv(recs)
        fresh = {r.zotero_item.item_key: list(r.zotero_item.child_notes) for r in recs}
        real = cn.create_note
        calls = []

        def create_then_crash(*a, **k):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("fixture crash")
            return real(*a, **k)
        with mock.patch.object(obsidian_manager, "try_acquire_lock", return_value=1), \
                mock.patch.object(obsidian_manager, "release_lock"), \
                mock.patch.object(cn, "create_note", side_effect=create_then_crash):
            payload, status = ns._create_notes(inv, settings, self.cfg, ["m1", "m2"], fresh=fresh)
        assert status == 500 and payload["created"] == 1
        ops = [o for o in journal.load(self.vault, self.cfg)["ops"] if o.get("kind") == "create_note"]
        assert len(ops) == 1

    def test_a_cancelled_refresh_names_the_notes_it_never_reached(self):
        from api.routes import notesync as ns
        from rag.vault import obsidian_manager
        recs = [self._rec_keyed("a2020", raw=EDITED), self._rec_keyed("b2021", raw=EDITED)]
        inv, settings = self._inv(recs)
        fresh = {r.zotero_item.item_key: list(r.zotero_item.child_notes) for r in recs}
        with mock.patch("api.routes.notesync.load_config", return_value=self.cfg):
            plan = ns._refresh_plan(inv, settings, fresh, self.cfg)
        with mock.patch.object(obsidian_manager, "try_acquire_lock", return_value=1), \
                mock.patch.object(obsidian_manager, "heartbeat", return_value=True), \
                mock.patch.object(obsidian_manager, "release_lock"):
            done = ns._refresh_write(self.vault, plan, should_cancel=lambda: True)
        assert done["cancelled"] and sorted(done["not_reached"]) == ["a2020", "b2021"]
