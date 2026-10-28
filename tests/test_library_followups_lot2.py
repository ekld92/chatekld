"""Follow-ups plan lot 2 (2026-09-22): the mirror writes `pdf:` and vocab-gated
Zotero tags into the notes it creates, the run applies mirror → pdf: → tags →
renames, the `pdf:` lists of existing notes are a review group, and note tags
are on by default. Each regression test was red on 3e406a5d.
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_notesync_route as _route_tests  # noqa: E402
from test_library_workflow_lot5 import FakeT  # noqa: E402
from test_notesync_route import _HEADERS  # noqa: E402

from accession import library_run  # noqa: E402

_TAGS_MD = """| Description | Tags | Fichiers |
| --- | --- | --- |
| Dépression | #dep, #mood | [[dep]] |
| Bipolaire | #bipo | [[bipo]] |
"""


def _frames(text):
    out = []
    for line in text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            with contextlib.suppress(ValueError):
                out.append(json.loads(line[6:]))
    return out


class TestCreatedNoteFrontmatter(_route_tests.TestSyncState):
    """A note the mirror creates reads `current` for pdf-links-status and
    carries the Zotero parent's tags through the vocabulary gate."""

    def _new_record(self, key="m2020", zot_tags=("dep", "nope"), pdf="m_2020.pdf"):
        rec = self._rec_keyed(key, has_note=False)
        rec.zotero_item.tags = list(zot_tags)
        biblio = self.vault / "Z_attachments" / "biblio_articles"
        biblio.mkdir(parents=True, exist_ok=True)
        (biblio / pdf).write_bytes(b"%PDF-1.4 x")
        rec.pdf_paths = [biblio / pdf]
        (self.vault / "_tags.md").write_text(_TAGS_MD, encoding="utf-8")
        return rec

    def _create(self, recs):
        fresh = {r.zotero_item.item_key: list(r.zotero_item.child_notes) for r in recs}
        with mock.patch("api.routes.notesync._fresh_zotero_notes", return_value=fresh):
            return self._post("/api/notesync/create",
                              {"citation_keys": [r.citation_key for r in recs], "confirm": True},
                              recs).get_json()

    def test_a_created_note_carries_pdf_links_that_read_current(self):
        from api.routes._audit_tagwrite import ambiguous_pdf_basenames, read_pdf_links
        from api.routes.tagging import _pdf_link_state
        rec = self._new_record()
        d = self._create([rec])
        self.assertTrue(d["results"]["m2020"]["ok"], d)
        path = self.notes / "m2020.md"
        text = path.read_text(encoding="utf-8")
        self.assertEqual(read_pdf_links(text), ["m_2020.pdf"])
        rec.obsidian_note = SimpleNamespace(path=path, tags=["dep"])
        state, expected, listed = _pdf_link_state(rec, self.vault, ambiguous_pdf_basenames(self.vault))
        self.assertEqual((state, expected, listed), ("current", ["m_2020.pdf"], ["m_2020.pdf"]))

    def test_a_created_note_gets_the_zotero_tags_through_the_vocabulary_gate(self):
        rec = self._new_record(zot_tags=("nope", "Dep", "mood"))
        self._create([rec])
        text = (self.notes / "m2020.md").read_text(encoding="utf-8")
        self.assertIn("tags: [dep, mood]\n", text)
        self.assertNotIn("nope", text)

    def test_without_a_vocabulary_no_zotero_tag_is_copied(self):
        rec = self._new_record(zot_tags=("dep",))
        (self.vault / "_tags.md").unlink()
        self._create([rec])
        text = (self.notes / "m2020.md").read_text(encoding="utf-8")
        self.assertIn("tags: []\n", text)
        self.assertIn('- "[[m_2020.pdf]]"', text)

    def test_preview_shows_the_same_text_the_create_writes(self):
        rec = self._new_record()
        with self._scan_as_fresh([rec]):
            preview = self._post("/api/notesync/preview", {"citation_key": "m2020"}, [rec]).get_json()
        self._create([rec])
        self.assertEqual(preview["text"], (self.notes / "m2020.md").read_text(encoding="utf-8"))


class TestMirrorNoteTags(_route_tests.TestSyncState):
    def setUp(self):
        super().setUp()
        self.cfg["notesync_zotero_authoritative"] = True
        (self.vault / "_tags.md").write_text(_TAGS_MD, encoding="utf-8")

    def _drain(self):
        from api.routes import notesync
        for _ in range(400):
            if notesync._MIRROR_GATE.acquire(blocking=False):
                notesync._MIRROR_GATE.release()
                return
            threading.Event().wait(0.005)

    def _mirror(self, recs, body, extra=()):
        from rag.vault import obsidian_manager
        with contextlib.ExitStack() as st:
            for p in self._ctx(recs):
                st.enter_context(p)
            st.enter_context(self._fresh(recs))
            st.enter_context(mock.patch("api.routes.notesync._git_undo_check",
                                        return_value=(True, "", "0" * 40)))
            st.enter_context(mock.patch.object(obsidian_manager, "try_acquire_lock", return_value=1))
            st.enter_context(mock.patch.object(obsidian_manager, "heartbeat", return_value=True))
            st.enter_context(mock.patch.object(obsidian_manager, "release_lock"))
            for p in extra:
                st.enter_context(p)
            r = self.client.post("/api/notesync/mirror", json=body, headers=_HEADERS)
            frames = _frames(r.get_data(as_text=True)) if r.status_code == 200 else []
        self._drain()
        terminal = [f["mirror"] for f in frames if "mirror" in f]
        return r, (terminal[-1] if terminal else None)

    def test_note_tags_reach_only_the_created_note_and_are_vocab_gated(self):
        existing = self._rec_keyed("a2020")           # has a note on disk
        new = self._rec_keyed("m2020", has_note=False)
        before = (self.notes / "a2020.md").read_text(encoding="utf-8")
        _r, plan = self._mirror([existing, new], {"dry_run": True})
        r, done = self._mirror([existing, new], {
            "confirm": True, "plan_digest": plan["plan_digest"],
            "note_tags": {"m2020": ["Mood", "zzz-not-in-vocab"], "a2020": ["dep"]}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(done["created"], 1, done)
        text = (self.notes / "m2020.md").read_text(encoding="utf-8")
        self.assertIn("tags: [mood]\n", text)
        self.assertNotIn("zzz", text)
        self.assertEqual((self.notes / "a2020.md").read_text(encoding="utf-8"), before)

    def test_note_tags_do_not_move_the_reviewed_digest(self):
        new = self._rec_keyed("m2020", has_note=False)
        _r, plan = self._mirror([new], {"dry_run": True})
        _r, plan2 = self._mirror([new], {"dry_run": True, "note_tags": {"m2020": ["dep"]}})
        self.assertEqual(plan["plan_digest"], plan2["plan_digest"])

    def test_malformed_note_tags_are_a_400_before_anything_runs(self):
        new = self._rec_keyed("m2020", has_note=False)
        for bad in ("x", {"m2020": "dep"}, {"m2020": [""]}, {"../x": ["dep"]}, {"m2020": [1]}):
            r, _ = self._mirror([new], {"dry_run": True, "note_tags": bad})
            self.assertEqual(r.status_code, 400, bad)

    def test_the_digest_and_the_written_bytes_come_from_one_render(self):
        """Wrap the ONE render helper: the plan's digest moves with it, and the
        written note carries its output — so neither site renders on its own."""
        from api.routes import notesync
        new = self._rec_keyed("m2020", has_note=False)
        real = notesync._render_created_note

        def marked(rec, template, ctx, extra_tags=()):
            text, notes = real(rec, template, ctx, extra_tags=extra_tags)
            return text + "MARKER\n", notes
        _r, plain = self._mirror([new], {"dry_run": True})
        patch = mock.patch("api.routes.notesync._render_created_note", side_effect=marked)
        _r, wrapped = self._mirror([new], {"dry_run": True}, extra=[patch])
        self.assertNotEqual(plain["plan_digest"], wrapped["plan_digest"])
        r, done = self._mirror([new], {"confirm": True, "plan_digest": wrapped["plan_digest"]},
                               extra=[patch])
        self.assertEqual(done["created"], 1, done)
        self.assertTrue((self.notes / "m2020.md").read_text(encoding="utf-8").endswith("MARKER\n"))


# ---------------------------------------------------------------------------
# The orchestrator (fake transport)
# ---------------------------------------------------------------------------

def test_apply_order_is_mirror_pdf_links_tags_renames_and_the_mirror_gets_the_note_tags():
    f = FakeT(apply_pdf_links=lambda keys: (200, {
        "results": {k: {"ok": True, "changed": True} for k in keys}}))
    res = library_run.apply(f.transport(), "s", {
        "mirror": {"plan_digest": "d"},
        "pdf_links": ["k", "k2"],
        "tags": [{"unit": "attachment:1:A", "citation_key": "k", "tags": ["dep"]},
                 {"unit": "entry:1:F", "citation_key": "kf", "tags": ["mood"],
                  "targets": {"note": False}}],
        "renames": [{"unit": "attachment:1:A", "rel": "x.pdf", "new_name": "k_2026.pdf"}]})
    assert [c[0] for c in f.calls] == ["mirror", "apply_pdf_links", "apply_tags", "apply_tags", "rename"]
    assert f.calls[1][1] == (["k", "k2"],)
    # Only the rows whose note target is on ride the mirror.
    assert f.calls[0][1] == ("d", {"k": ["dep"]})
    assert [r["status"] for r in res["pdf_links"]] == ["done", "done"]
    assert list(res)[:5] == ["session", "mirror", "pdf_links", "tags", "renames"]


def test_pdf_links_are_sent_in_the_routes_chunks_with_one_row_per_key():
    sent = []
    f = FakeT(apply_pdf_links=lambda keys: sent.append(list(keys)) or (200, {
        "results": {k: ({"ok": False, "reason": "dirty_list"} if k == "k7"
                        else {"ok": True, "changed": True}) for k in keys}}))
    keys = [f"k{i}" for i in range(150)]
    res = library_run.apply(f.transport(), "s", {"pdf_links": keys})
    assert [len(c) for c in sent] == [100, 50]
    rows = {r["citation_key"]: r for r in res["pdf_links"]}
    assert len(rows) == 150 and rows["k7"]["status"] == "failed" and rows["k7"]["reason"] == "dirty_list"
    assert rows["k0"]["status"] == "done" and res["notes_written"] == 149


def test_analyze_lists_pdf_link_rows_and_only_absent_or_stale_are_selectable():
    f = FakeT(open_rows=[], pdf_links_status=(200, {"counts": {"absent": 1, "dirty": 1},
        "rows": [{"citation_key": "a", "state": "absent", "expected": ["a.pdf"], "listed": [], "note": "a.md"},
                 {"citation_key": "b", "state": "dirty", "expected": ["b.pdf"], "listed": ["x"], "note": "b.md"},
                 {"citation_key": "c", "state": "stale", "expected": ["c.pdf"], "listed": ["old.pdf"], "note": "c.md"},
                 {"citation_key": "d", "state": "no_note", "expected": ["d.pdf"], "listed": [], "note": None}]}))
    review = library_run.analyze(f.transport(), "s")
    rows = {r["citation_key"]: r for r in review["pdf_links"]["rows"]}
    assert set(rows) == {"a", "b", "c"}
    assert rows["a"]["selectable"] and rows["c"]["selectable"] and not rows["b"]["selectable"]
    assert "not write" in rows["b"]["reason"]


def test_a_transport_without_pdf_links_still_analyses():
    f = FakeT(open_rows=[])
    review = library_run.analyze(f.transport(), "s")
    assert review["pdf_links"]["rows"] == [] and review["pdf_links"]["error"]


def test_note_tags_are_on_by_default_and_the_cli_can_opt_out(tmp_path):
    from click.testing import CliRunner
    from cli.main import cli
    assert library_run.DEFAULT_TAG_TARGETS["note"] is True
    approvals = tmp_path / "a.json"
    approvals.write_text(json.dumps({"tags": [{"unit": "u", "citation_key": "k", "tags": ["dep"]}]}))
    with mock.patch("core.driver.library_apply", return_value={"library_apply": {"tags": [{"status": "done"}]}}) as apply:
        res = CliRunner().invoke(cli, ["library", "run", "apply", "S", "--approvals-file", str(approvals),
                                       "--no-note-tags", "--confirm"])
        assert res.exit_code == 0, res.output
        assert apply.call_args.args[1]["tags"][0]["targets"] == {"note": False}
        res = CliRunner().invoke(cli, ["library", "run", "apply", "S", "--approvals-file", str(approvals),
                                       "--confirm"])
        assert "targets" not in apply.call_args.args[1]["tags"][0]


# ---------------------------------------------------------------------------
# Review 2026-09-22 fixes. Each test was proven red on f7869935.
# ---------------------------------------------------------------------------

def _many_units(n):
    units = {}
    for i in range(n):
        units[f"entry:1:E{i}"] = {"kind": "entry", "citation_key": f"k{i}"}
    return units


def test_more_than_100_tagged_papers_do_not_blow_the_mirrors_bound():
    """Only the notes the reviewed mirror CREATES get their ticked tags."""
    f = FakeT(units=_many_units(150))
    rows = [{"unit": f"entry:1:E{i}", "citation_key": f"k{i}", "tags": ["dep"]} for i in range(150)]
    library_run.apply(f.transport(), "s", {
        "mirror": {"plan_digest": "d", "create_keys": ["k3", "k7"]}, "tags": rows})
    assert f.calls[0] == ("mirror", ("d", {"k3": ["dep"], "k7": ["dep"]}))


def test_a_row_the_tag_step_refuses_tags_no_created_note():
    """The same check runs before the mirror: a key that is not its unit's
    record (a hand-edited approvals file) contributes nothing to note_tags."""
    f = FakeT()
    res = library_run.apply(f.transport(), "s", {
        "mirror": {"plan_digest": "d"},
        "tags": [{"unit": "entry:1:F", "citation_key": "k", "tags": ["dep"]}]})
    assert f.calls[0] == ("mirror", ("d", {}))
    assert res["tags"][0]["status"] == "failed"


def test_note_tags_are_deduplicated_per_key():
    f = FakeT()
    library_run.apply(f.transport(), "s", {
        "mirror": {"plan_digest": "d"},
        "tags": [{"unit": "attachment:1:A", "citation_key": "k", "tags": ["dep", "mood"]},
                 {"unit": "attachment:1:B", "citation_key": "k", "tags": ["mood", "dep", "bipo"]}]})
    assert f.calls[0] == ("mirror", ("d", {"k": ["dep", "mood", "bipo"]}))


def test_a_lost_lock_in_the_pdf_phase_is_reported_truthfully_and_stops_the_phase():
    """The route answers 200 + aborted and leaves unreached keys out of
    results: those were NOT written, the next chunk is not sent, and a later
    cancel still lists the tag rows it never ran."""
    sent = []

    def apply_pdf_links(keys):
        sent.append(list(keys))
        return 200, {"results": {k: {"ok": True, "changed": True} for k in keys[:25]},
                     "aborted": "lock_lost"}
    f = FakeT(apply_pdf_links=apply_pdf_links)
    keys = [f"p{i}" for i in range(250)]
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 1          # cancel right after the pdf: phase
    res = library_run.apply(f.transport(), "s", {
        "pdf_links": keys,
        "tags": [{"unit": "attachment:1:A", "citation_key": "k", "tags": ["dep"]}]},
        should_cancel=cancel)
    assert [len(c) for c in sent] == [100]
    rows = res["pdf_links"]
    assert len(rows) == 250 and len({r["citation_key"] for r in rows}) == 250
    assert [r["status"] for r in rows[:25]] == ["done"] * 25
    assert {r["status"] for r in rows[25:]} == {"failed"}
    assert res["notes_written"] == 25
    assert res["cancelled"] is True and res["not_reached"] == ["tag:attachment:1:A"]


def test_a_200_without_a_row_and_without_abort_is_uncertain_not_done():
    f = FakeT(apply_pdf_links=lambda keys: (200, {"results": {}}))
    res = library_run.apply(f.transport(), "s", {"pdf_links": ["p1"]})
    assert res["pdf_links"][0]["status"] == "uncertain"


class TestMirrorNoteTagsReport(TestMirrorNoteTags):
    def test_the_route_takes_more_note_tags_keys_than_it_creates_notes(self):
        new = self._rec_keyed("m2020", has_note=False)
        tags = {f"x{i}": ["dep"] for i in range(150)}
        r, plan = self._mirror([new], {"dry_run": True, "note_tags": tags})
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(plan)

    def test_ticked_tags_lost_to_a_missing_vocabulary_are_reported(self):
        (self.vault / "_tags.md").unlink()
        new = self._rec_keyed("m2020", has_note=False)
        _r, plan = self._mirror([new], {"dry_run": True})
        _r, done = self._mirror([new], {"confirm": True, "plan_digest": plan["plan_digest"],
                                        "note_tags": {"m2020": ["dep"]}})
        self.assertEqual(done["created"], 1, done)
        self.assertIn("vocabulary", done["note_tags_unwritten"]["m2020"])

    def test_ticked_tags_for_a_note_the_mirror_did_not_create_are_reported(self):
        new = self._rec_keyed("m2020", has_note=False)
        _r, done = self._mirror([new], {"confirm": True, "plan_digest": "stale",
                                        "note_tags": {"m2020": ["dep"]}})
        self.assertEqual(done["stopped"], "plan_changed", done)
        self.assertIn("not created", done["note_tags_unwritten"]["m2020"])

    def test_tags_that_landed_are_not_reported(self):
        new = self._rec_keyed("m2020", has_note=False)
        existing = self._rec_keyed("a2020")
        _r, plan = self._mirror([new, existing], {"dry_run": True})
        _r, done = self._mirror([new, existing], {"confirm": True, "plan_digest": plan["plan_digest"],
                                                  "note_tags": {"m2020": ["dep"], "a2020": ["dep"]}})
        self.assertEqual(done["note_tags_unwritten"], {})
