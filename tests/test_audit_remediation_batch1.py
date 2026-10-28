"""Audit remediation 2026-09-23, Batch 1 — tracker honesty.

Each test here was proven RED against the pre-fix code (a7a171d9): the fix
was reverted locally, the test watched fail, the fix restored.

* L1/L8  a note unit closes only on the mirror's positive per-key evidence
         (``settled_keys``), recorded with the note digest the mirror used;
* L5/N4  ticked tags that did not land keep the tag step open, "no target"
         is never ``done``, and the render reports what it dropped;
* L10    apply refuses while a scan runs, and stops before the next phase
         when one starts;
* L11    CLI exit codes and silently-dropped flags.

The orchestrator is driven over the lot-5 fake transport whose mirror
terminal carries the REAL per-key fields; the mirror route itself runs for
real (only Zotero and the op-lock are faked), and the CLI is patched at
``core.driver._http_sse`` / ``_http_json`` — never above the driver.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_library_followups_lot2 as _lot2  # noqa: E402  (module: its classes are not re-collected)
from test_library_workflow_lot5 import MIRROR_OK, FakeT  # noqa: E402

from accession import library_run  # noqa: E402


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The mirror route's terminal (real route)
# ---------------------------------------------------------------------------

class TestMirrorSettledKeys(_lot2.TestMirrorNoteTags):
    def test_settled_keys_name_created_and_in_sync_notes_with_their_digest(self):
        new = self._rec_keyed("m2020", has_note=False, html="<p>new text</p>")
        existing = self._rec_keyed("a2020")          # region equals Zotero's text
        _r, plan = self._mirror([new, existing], {"dry_run": True})
        _r, done = self._mirror([new, existing], {"confirm": True,
                                                  "plan_digest": plan["plan_digest"]})
        self.assertTrue(done["ok"], done)
        self.assertEqual(done["settled_keys"], {"a2020": _sha("<p>alpha beta</p>"),
                                                "m2020": _sha("<p>new text</p>")})

    def test_a_record_with_two_reading_notes_is_not_settled(self):
        two = self._rec_keyed("t2020", has_note=False)
        from types import SimpleNamespace
        two.zotero_item.child_notes.append(
            SimpleNamespace(note_key="OTHER123", body_html="<p>two</p>", body_plain="", version=1))
        _r, plan = self._mirror([two], {"dry_run": True})
        _r, done = self._mirror([two], {"confirm": True, "plan_digest": plan["plan_digest"]})
        self.assertNotIn("t2020", done["settled_keys"])
        self.assertIn("t2020", done["listed"]["multiple_notes"])

    def test_a_ticked_tag_outside_the_vocabulary_is_reported(self):
        new = self._rec_keyed("m2020", has_note=False)
        _r, plan = self._mirror([new], {"dry_run": True})
        _r, done = self._mirror([new], {"confirm": True, "plan_digest": plan["plan_digest"],
                                        "note_tags": {"m2020": ["dep", "nope"]}})
        self.assertEqual(done["created"], 1, done)
        self.assertIn("nope", done["note_tags_unwritten"]["m2020"])

    def test_a_pdf_list_that_could_not_be_written_is_reported(self):
        new = self._rec_keyed("m2020", has_note=False)
        biblio = self.vault / "Z_attachments" / "biblio_articles"
        biblio.mkdir(parents=True, exist_ok=True)
        (biblio / "m_2020.pdf").write_bytes(b"%PDF-1.4 x")
        new.pdf_paths = [biblio / "m_2020.pdf"]
        refuse = mock.patch("api.routes._audit_tagwrite.set_pdf_links",
                            return_value=(None, "frontmatter_unparseable"))
        _r, plan = self._mirror([new], {"dry_run": True}, extra=[refuse])
        _r, done = self._mirror([new], {"confirm": True, "plan_digest": plan["plan_digest"]},
                                extra=[refuse])
        self.assertEqual(done["created"], 1, done)
        self.assertIn("frontmatter_unparseable", done["pdf_links_unwritten"]["m2020"])


# Borrow the harness, not the tests: the inherited test_* methods already run
# in their own module (the duplicate-collection Known Issue).
for _name in [n for n in dir(_lot2.TestMirrorNoteTags) if n.startswith("test_")]:
    if _name not in TestMirrorSettledKeys.__dict__:
        setattr(TestMirrorSettledKeys, _name, None)


# ---------------------------------------------------------------------------
# The orchestrator (fake transport with the real terminal shape)
# ---------------------------------------------------------------------------

def _capturing(f: FakeT):
    t = f.transport()
    shas: list = []

    def record_step(sid, unit, step, status, detail, note_sha256=None):
        f.steps.append((unit, step, status, detail))
        shas.append(note_sha256)
        return 200, {}
    t.record_step = record_step
    return t, shas


def test_a_note_the_mirror_skipped_stays_open_with_its_reason():
    body = dict(MIRROR_OK, skipped={"create": {"multiple_notes": ["k"]}})
    f = FakeT(mirror=(200, body))
    res = library_run.apply(f.transport(), "s", {"mirror": {"plan_digest": "d", "units": ["note:1:N"]}})
    assert f.steps == []
    assert res["mirror"]["open"] == [{"unit": "note:1:N", "citation_key": "k",
                                      "reason": "multiple_notes (create)"}]


def test_a_note_the_mirror_never_mentioned_needs_a_rescan():
    f = FakeT()
    res = library_run.apply(f.transport(), "s", {"mirror": {"plan_digest": "d", "units": ["note:1:N"]}})
    assert f.steps == []
    assert res["mirror"]["open"][0]["reason"] == library_run.RESCAN_NEEDED


def test_a_settled_note_is_recorded_with_the_digest_the_mirror_used():
    f = FakeT(mirror=(200, dict(MIRROR_OK, settled_keys={"k": "abc123"})))
    t, shas = _capturing(f)
    res = library_run.apply(t, "s", {"mirror": {"plan_digest": "d", "units": ["note:1:N"]}})
    assert [(s[0], s[1], s[2]) for s in f.steps] == [("note:1:N", "mirror", "done")]
    assert shas == ["abc123"] and res["mirror"]["open"] == []


def test_every_tag_target_off_is_skipped_with_a_reason_never_done():
    f = FakeT()
    res = library_run.apply(f.transport(), "s", {"tags": [
        {"unit": "attachment:1:A", "citation_key": "k", "tags": ["dep"],
         "targets": {"finder": False, "zotero": False, "note": False}}]})
    assert "apply_tags" not in [c[0] for c in f.calls]
    assert res["tags"][0]["status"] == "skipped" and res["tags"][0]["reason"] == "no target selected"
    assert f.steps[0][2] == "skipped" and f.steps[0][3]["reason"] == "no target selected"


def test_an_entry_row_whose_only_target_was_finder_is_not_done():
    f = FakeT()
    res = library_run.apply(f.transport(), "s", {"tags": [
        {"unit": "entry:1:F", "citation_key": "kf", "tags": ["dep"],
         "targets": {"finder": True, "zotero": False, "note": False}}]})
    assert "apply_tags" not in [c[0] for c in f.calls]
    assert res["tags"][0]["status"] == "skipped"


def test_note_tags_the_mirror_did_not_write_keep_the_tag_step_open():
    body = dict(MIRROR_OK, note_tags_unwritten={"k": "not in the tag vocabulary: nope"})
    f = FakeT(mirror=(200, body), apply_tags=(200, {"results": {
        "finder": {"ok": True}, "zotero": {"ok": True},
        "note": {"ok": False, "skipped": "no_note"}}}))
    res = library_run.apply(f.transport(), "s", {
        "mirror": {"plan_digest": "d"},
        "tags": [{"unit": "attachment:1:A", "citation_key": "k", "tags": ["nope"]}]})
    tag = res["tags"][0]
    assert tag["status"] == "failed" and tag["targets"]["note"] == "failed"
    assert "vocabulary" in tag["reasons"]["note"]
    assert res["note_tags_unwritten"] == {"k": "not in the tag vocabulary: nope"}


def test_a_mirror_that_did_not_answer_leaves_the_sent_note_tags_unaccounted():
    f = FakeT(mirror=(502, {"error": "bad gateway"}), apply_tags=(200, {"results": {
        "finder": {"ok": True}, "zotero": {"ok": True},
        "note": {"ok": False, "skipped": "no_note"}}}))
    res = library_run.apply(f.transport(), "s", {
        "mirror": {"plan_digest": "d"},
        "tags": [{"unit": "attachment:1:A", "citation_key": "k", "tags": ["dep"]}]})
    assert "k" in res["note_tags_unwritten"]
    assert res["tags"][0]["status"] == "failed"


def test_apply_refuses_while_a_scan_runs():
    f = FakeT(scan={"state": "scanning", "finished_at": 1.0})
    res = library_run.apply(f.transport(), "s", {
        "mirror": {"plan_digest": "d"},
        "tags": [{"unit": "attachment:1:A", "citation_key": "k", "tags": ["dep"]}]})
    assert f.calls == [] and f.steps == []
    assert res["stopped"] == "scan_running"
    assert res["not_reached"] == ["mirror", "tag:attachment:1:A"]


def test_a_scan_starting_between_phases_stops_the_later_ones():
    f = FakeT()
    t = f.transport()
    states = iter([{"state": "idle", "finished_at": 1.0}] * 2 + [{"state": "scanning"}] * 5)
    t.scan_state = lambda: next(states)
    res = library_run.apply(t, "s", {
        "mirror": {"plan_digest": "d"},
        "tags": [{"unit": "attachment:1:A", "citation_key": "k", "tags": ["dep"]}],
        "renames": [{"unit": "attachment:1:A", "rel": "x.pdf", "new_name": "k_2026.pdf"}]})
    assert [c[0] for c in f.calls] == ["mirror"]
    assert res["stopped"] == "scan_running"
    assert res["not_reached"] == ["tag:attachment:1:A", "rename:attachment:1:A"]


# ---------------------------------------------------------------------------
# The CLI (patched at the driver's transport, never above it)
# ---------------------------------------------------------------------------

def _cli(args, *, sse=None, http=None):
    from click.testing import CliRunner
    from cli.main import cli
    with mock.patch("core.driver.resolve_base_url", return_value="http://127.0.0.1:1"), \
            mock.patch("core.driver._http_sse", side_effect=sse or AssertionError("no stream expected")), \
            mock.patch("core.driver._http_json", side_effect=http or AssertionError("no request expected")):
        return CliRunner().invoke(cli, args)


def _apply_stream(result):
    def sse(key, data, **kw):
        return {"tokens": "", "events": [{"library_apply": result}], "terminal": None, "error": None}
    return sse


def test_cli_apply_exits_1_when_ticked_note_tags_did_not_land(tmp_path):
    ap = tmp_path / "ap.json"
    ap.write_text("{}")
    ok = {"mirror": {"status": "done", "open": []}, "tags": [], "renames": [], "pdf_links": []}
    res = _cli(["library", "run", "apply", "s", "--approvals-file", str(ap), "--confirm"],
               sse=_apply_stream(ok))
    assert res.exit_code == 0, res.output
    for extra in ({"note_tags_unwritten": {"k": "why"}}, {"ignored": [{"kind": "tag"}]},
                  {"pdf_links_unwritten": {"k": "why"}}, {"stopped": "scan_running"},
                  {"mirror": {"status": "done", "open": [{"unit": "note:1:N", "reason": "rescan needed"}]}}):
        res = _cli(["library", "run", "apply", "s", "--approvals-file", str(ap), "--confirm"],
                   sse=_apply_stream({**ok, **extra}))
        assert res.exit_code == 1, extra


def test_cli_new_refuses_cost_flags_without_a_model():
    for flags in (["--budget-usd", "1"], ["--no-limit"]):
        res = _cli(["library", "run", "new", *flags])
        assert res.exit_code == 2 and "--provider" in res.output, (flags, res.output)
