"""Library-workflow lot 5: one orchestrator for the maintenance run.

Two layers:

* the pure orchestrator (`accession/library_run.py`) over a fake transport —
  ordering, per-step outcomes, uncertainty, cancellation;
* the real `/api/library/*` routes over a disposable library (the lot 2
  fixture), with the write routes they call replaced by small stub routes
  that do what the real ones do to the FILES (a rename renames) — so the
  SSE plumbing, the gate, the durable session record and the tracker are
  exercised for real.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import flask
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_library_imports as _imports_tests  # noqa: E402  (module import)

from accession import imports, library_run  # noqa: E402

_H = {"X-Requested-With": "ChatEKLD"}


# ---------------------------------------------------------------------------
# Pure orchestrator
# ---------------------------------------------------------------------------

#: A confirmed mirror's terminal frame with its REAL per-key fields (audit
#: remediation 2026-09-23): the orchestrator settles note units and reads
#: ticked-tag outcomes from these, so a fake that omits them tests nothing.
MIRROR_OK = {"ok": True, "plan_digest": "d", "failed": {}, "listed": {},
             "skipped": {"create": {}}, "settled_keys": {}, "note_tags_unwritten": {},
             "pdf_links_unwritten": {}}



requires_birthtime = pytest.mark.skipif(
    not hasattr(Path(__file__).stat(), "st_birthtime"),
    reason="Requires stable file birth time; exercised by macos-platform CI.")

class FakeT:
    def __init__(self, open_rows=(), **over):
        self.calls = []
        self.steps = []
        self.renamed = []
        self.open_rows = list(open_rows)
        self.over = over
        # The tracker as the orchestrator sees it (server-owned): attachment A
        # and B under entry E (key "k"), a PDF-less entry F (key "kf"), a note N.
        self.units = over.pop("units", None) or {
            "entry:1:E": {"kind": "entry", "citation_key": "k"},
            "entry:1:F": {"kind": "entry", "citation_key": "kf"},
            "attachment:1:A": {"kind": "attachment", "parent": "entry:1:E", "rel": "Z/biblio/x.pdf",
                               "in_biblio": True},
            "attachment:1:B": {"kind": "attachment", "parent": "entry:1:E", "rel": "Z/biblio/b.pdf",
                               "in_biblio": True},
            "note:1:N": {"kind": "note", "parent": "entry:1:E"},
        }

    def transport(self):
        o = self.over

        def rec(name, result):
            def f(*a):
                self.calls.append((name, a))
                r = result(*a) if callable(result) else result
                if isinstance(r, Exception):
                    raise r
                return r
            return f
        return library_run.Transport(
            observe=rec("observe", o.get("observe", (200, {"open": self.open_rows}))),
            record_step=lambda sid, unit, step, status, detail, **kw: (
                self.steps.append((unit, step, status, detail)) or (200, {})),
            note_rename=lambda sid, unit, rel: self.renamed.append((unit, rel)) or (200, {}),
            rename_preview=rec("rename_preview", o.get("rename_preview", lambda rels: (200, {
                "rows": [{"rel": r, "name": r, "proposal": "new_" + r, "can_apply": True}
                         for r in rels]}))),
            rename=rec("rename", o.get("rename", lambda rel, name: (200, {
                "new_rel": "Z/biblio/" + name}))),
            propose=rec("propose", o.get("propose", (200, {"proposed": [{"tag": "dep"}],
                                                           "status": "ok"}))),
            apply_tags=rec("apply_tags", o.get("apply_tags", (200, {"results": {
                "finder": {"ok": True}, "zotero": {"ok": True}, "note": {"ok": True}}}))),
            mirror=rec("mirror", o.get("mirror", (200, dict(MIRROR_OK)))),
            scan_state=lambda: o.get("scan", {"state": "idle", "finished_at": 1.0}),
            biblio_prefix="Z/biblio/",
            unit=lambda uid: self.units.get(uid),
            **({"pdf_links_status": rec("pdf_links_status", o["pdf_links_status"])}
               if "pdf_links_status" in o else {}),
            **({"apply_pdf_links": rec("apply_pdf_links", o["apply_pdf_links"])}
               if "apply_pdf_links" in o else {}))


ROW = {"id": "attachment:1:A", "steps": ["rename", "tag"], "rel": "Z/biblio/x.pdf",
       "citation_key": "k2026"}


def test_analyze_refuses_without_a_completed_scan_or_during_one():
    for scan in ({"state": "scanning", "finished_at": 1.0}, {"state": "idle", "finished_at": None}):
        f = FakeT(scan=scan)
        out = library_run.analyze(f.transport(), "s")
        assert out["blocked"] and not f.calls


def test_analyze_gathers_names_tags_and_the_mirror_plan():
    f = FakeT([ROW])
    out = library_run.analyze(f.transport(), "s", propose_tags=True)
    assert out["renames"][0]["proposal"] == "new_x.pdf" and out["renames"][0]["can_apply"]
    assert out["tags"][0]["proposed"] == [{"tag": "dep"}]
    assert out["mirror"] == MIRROR_OK
    assert [c[0] for c in f.calls] == ["observe", "rename_preview", "propose", "mirror"]


def test_analyze_does_not_propose_tags_unless_asked():
    f = FakeT([ROW])
    library_run.analyze(f.transport(), "s")
    assert "propose" not in [c[0] for c in f.calls]


def test_a_build_without_the_renamer_still_analyses_the_rest():
    f = FakeT([ROW], rename_preview=(404, {"error": "not available in this build"}))
    out = library_run.analyze(f.transport(), "s", propose_tags=True)
    assert out["renames"] == [] and out["renames_unavailable"]
    assert out["tags"] and out["mirror"]


def test_the_spending_limit_stops_the_proposals():
    rows = [dict(ROW, id=f"attachment:1:{i}", citation_key=f"k{i}") for i in range(3)]
    f = FakeT(rows, propose=(409, {"reason": "budget", "error": "limit"}))
    out = library_run.analyze(f.transport(), "s", propose_tags=True)
    assert len(out["tags"]) == 1 and "limit" in out["blocked"]


def test_apply_mirrors_first_then_tags_the_unit_then_renames_it():
    # Follow-ups lot 2 (2026-09-22): mirror → pdf: → tags → renames.
    f = FakeT()
    res = library_run.apply(f.transport(), "s", {
        "tags": [{"unit": "attachment:1:A", "citation_key": "k", "tags": ["dep"]}],
        "renames": [{"unit": "attachment:1:A", "rel": "x.pdf", "new_name": "k_2026.pdf"}],
        "mirror": {"plan_digest": "d"}})
    assert [c[0] for c in f.calls] == ["mirror", "apply_tags", "rename"]
    assert f.renamed == [("attachment:1:A", "Z/biblio/k_2026.pdf")]
    assert [(s[1], s[2]) for s in f.steps] == [("tag", "done"), ("rename", "done")]
    assert res["mirror"]["status"] == "done"
    # The tag write addressed the UNIT, with all three targets on by default.
    _key, _tags, unit, targets = f.calls[1][1]
    assert unit == "attachment:1:A" and targets == {"finder": True, "zotero": True, "note": True}


@pytest.mark.parametrize("answer, expected", [
    ((400, {"error": "refused"}), "failed"),
    # The renamer can answer 409 AFTER moving the file ("Rename undone …"
    # whose restore failed; "failed safely" with a pending op): uncertain.
    ((409, {"error": "Rename undone"}), "uncertain"),
    ((503, {"error": "The vault is busy (indexing)"}), "failed"),
    ((502, {"error": "bad gateway"}), "uncertain"),
    (ConnectionError("cut"), "uncertain"),
])
def test_a_write_whose_outcome_is_unknown_is_uncertain_never_done(answer, expected):
    f = FakeT(rename=answer)
    res = library_run.apply(f.transport(), "s", {"renames": [
        {"unit": "attachment:1:A", "rel": "x.pdf", "new_name": "k_2026.pdf"}]})
    assert res["renames"][0]["status"] == expected
    assert f.steps[0][2] == expected and f.renamed == []


def test_per_target_outcomes_are_recorded_and_a_partial_write_is_failed():
    f = FakeT(apply_tags=(200, {"results": {"finder": {"ok": True}, "note": {"ok": True},
                                            "zotero": {"ok": False, "error": "refused before write", "write_not_started": True}}}))
    res = library_run.apply(f.transport(), "s", {"tags": [
        {"unit": "attachment:1:A", "citation_key": "k", "tags": ["dep"]}]})
    assert res["tags"][0]["status"] == "failed"
    assert f.steps[0][3]["targets"] == {"finder": "done", "zotero": "failed", "note": "done"}
    step = {"detail": f.steps[0][3]}
    assert library_run.unresolved_targets(step) == {"finder": False, "zotero": True, "note": False}


def test_a_missing_target_is_skipped_not_failed():
    f = FakeT(apply_tags=(200, {"results": {"finder": {"ok": False, "skipped": "no_pdf"},
                                            "zotero": {"ok": True},
                                            "note": {"ok": False, "skipped": "no_note"}}}))
    res = library_run.apply(f.transport(), "s", {"tags": [
        {"unit": "attachment:1:A", "citation_key": "k", "tags": ["dep"]}]})
    assert res["tags"][0]["status"] == "done"


def test_cancellation_lists_what_was_not_reached():
    f = FakeT()
    calls = iter([False, False, True])    # mirror, rename A, then cancel
    f.units["attachment:1:A"]["rel"] = "Z/biblio/a.pdf"
    res = library_run.apply(f.transport(), "s", {"renames": [
        {"unit": "attachment:1:A", "rel": "a.pdf", "new_name": "a_2026.pdf"},
        {"unit": "attachment:1:B", "rel": "b.pdf", "new_name": "b_2026.pdf"}],
        "mirror": {"plan_digest": "d"}}, should_cancel=lambda: next(calls, True))
    assert res["cancelled"] and res["not_reached"] == ["rename:attachment:1:B"]
    assert [c[0] for c in f.calls] == ["mirror", "rename"]


# ---------------------------------------------------------------------------
# The routes over a disposable library
# ---------------------------------------------------------------------------

def _stub_routes(lib, log, *, rename_status=200, settled=None):
    bp = flask.Blueprint("stubs", __name__)
    settled = {} if settled is None else settled

    @bp.route("/api/maintenance/rename-preview", methods=["POST"])
    def preview():
        rels = flask.request.get_json()["rels"]
        return flask.jsonify({"rows": [{"rel": r, "name": r.split("/")[-1],
                                        "proposal": "jones_2026.pdf", "can_apply": True}
                                       for r in rels]})

    @bp.route("/api/maintenance/rename", methods=["POST"])
    def rename():
        body = flask.request.get_json()
        log.append(("rename", body))
        if rename_status != 200:
            return flask.jsonify({"error": "boom"}), rename_status
        src = lib.biblio / body["rel"]
        dst = src.with_name(body["new_name"])
        os.rename(src, dst)  # what the renamer does to the file (Zotero NOT updated)
        return flask.jsonify({"ok": True, "new_rel": str(dst.relative_to(lib.vault))})

    @bp.route("/api/tagging/propose", methods=["POST"])
    def propose():
        return flask.jsonify({"proposed": [{"tag": "dep", "confidence": 0.9}], "status": "ok"})

    @bp.route("/api/tagging/apply-all", methods=["POST"])
    def apply_all():
        body = flask.request.get_json()
        unit = imports.unit(lib.settings, body["unit"])
        log.append(("apply", body["unit"], unit["rel"]))
        return flask.jsonify({"results": {"finder": {"ok": (lib.vault / unit["rel"]).exists()},
                                          "zotero": {"ok": True},
                                          "note": {"ok": False, "skipped": "no_note"}}})

    @bp.route("/api/notesync/mirror", methods=["POST"])
    def mirror():
        body = flask.request.get_json()
        log.append(("mirror", body))
        # The real confirmed terminal's per-key fields; `settled` (a test
        # knob) names the keys this stub claims to have mirrored.
        frame = {"mirror": {"ok": True, "plan_digest": "abc", "dry_run": "plan_digest" not in body,
                            "failed": {}, "listed": {}, "skipped": {"create": {}},
                            "settled_keys": dict(settled), "note_tags_unwritten": {},
                            "pdf_links_unwritten": {}}}
        return flask.Response(f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n",
                              mimetype="text/event-stream")
    return bp


class Routes:
    def __init__(self, tmp_path, **kw):  # kw → _stub_routes (rename_status, settled)
        from api.routes.library_workflow import library_workflow_bp
        self.lib = _imports_tests.Library(tmp_path)
        self.log: list = []
        app = flask.Flask(__name__)
        app.register_blueprint(library_workflow_bp)
        app.register_blueprint(_stub_routes(self.lib, self.log, **kw))
        self.client = app.test_client()
        from audit.manager import audit_manager
        inv = mock.MagicMock(built_at=123.0)
        self._p = [mock.patch.object(audit_manager, "get_inventory",
                                     return_value=(inv, self.lib.settings)),
                   mock.patch.object(audit_manager, "is_scanning", return_value=False),
                   mock.patch("api.routes.library_workflow.load_config", return_value={})]
        for p in self._p:
            p.start()

    def stop(self):
        for p in reversed(self._p):
            p.stop()

    def session(self):
        return self.client.post("/api/library/sessions", json={}, headers=_H).get_json()["session"]["id"]

    def sse(self, path, body):
        r = self.client.post(path, json=body, headers=_H)
        assert r.status_code == 200, r.get_data(as_text=True)
        frames = [json.loads(line[6:]) for line in r.get_data(as_text=True).splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        return frames


@pytest.fixture
def routes(tmp_path):
    r = Routes(tmp_path)
    yield r
    r.stop()


def _terminal(frames, key):
    hits = [f[key] for f in frames if key in f]
    assert hits, frames
    return hits[-1]


@requires_birthtime
def test_end_to_end_first_run_then_an_import_renamed_and_tagged(routes):
    lib = routes.lib
    sid = routes.session()
    first = _terminal(routes.sse("/api/library/analyze", {"session": sid}), "library_analyze")
    assert first["observation"]["initialized"] is True and first["renames"] == []

    e = lib.entry("JJJJ0001", "jones2026")
    lib.pdf(e, "PDFJ0001", "Jones - 2026 - Full text.pdf")
    sid2 = routes.session()
    review = _terminal(routes.sse("/api/library/analyze", {"session": sid2, "propose_tags": True}),
                       "library_analyze")
    (ren,) = review["renames"]
    assert ren["unit"] == "attachment:1:PDFJ0001" and ren["proposal"] == "jones_2026.pdf"
    assert review["tags"][0]["citation_key"] == "jones2026"
    digest = review["mirror"]["plan_digest"]

    result = _terminal(routes.sse("/api/library/apply", {"session": sid2, "confirm": True, "approvals": {
        "renames": [{"unit": ren["unit"], "rel": ren["rel"], "new_name": ren["proposal"]}],
        "tags": [{"unit": ren["unit"], "citation_key": "jones2026", "tags": ["dep"]}],
        "mirror": {"plan_digest": digest}}}), "library_apply")
    assert [r["status"] for r in result["renames"]] == ["done"]
    assert [t["status"] for t in result["tags"]] == ["done"]
    # Follow-ups lot 2: the mirror runs first, carrying the note tags for the
    # records it creates; the tag write addresses the unit BEFORE the rename
    # (still at the old name); the rename is last.
    assert ("apply", ren["unit"], "Z_attachments/biblio_articles/Jones - 2026 - Full text.pdf") in routes.log
    assert ("mirror", {"confirm": True, "plan_digest": digest,
                       "note_tags": {"jones2026": ["dep"]}}) in routes.log
    assert routes.log[-1][0] == "rename"

    # The next run: Zotero still has the old path, yet nothing is new, nothing
    # is open, and the renamed file is not taken for an orphan.
    sid3 = routes.session()
    again = _terminal(routes.sse("/api/library/analyze", {"session": sid3}), "library_analyze")
    obs = again["observation"]
    assert obs["open"] == [] and all(not v for v in obs["new"].values())
    # The run's result survives the window: it is on the session record.
    record = routes.client.get(f"/api/library/sessions/{sid2}", headers=_H).get_json()["session"]
    assert record["applies"][-1]["result"]["renames"][0]["status"] == "done"


def test_a_cut_during_apply_leaves_a_durable_resumable_state(tmp_path):
    r = Routes(tmp_path, rename_status=502)
    try:
        lib = r.lib
        r.sse("/api/library/analyze", {"session": r.session()})
        e = lib.entry("KKKK0001", "kim2026")
        lib.pdf(e, "PDFK0001", "kim.pdf")
        sid = r.session()
        review = _terminal(r.sse("/api/library/analyze", {"session": sid}), "library_analyze")
        (ren,) = review["renames"]
        res = _terminal(r.sse("/api/library/apply", {"session": sid, "confirm": True, "approvals": {
            "renames": [{"unit": ren["unit"], "rel": ren["rel"], "new_name": ren["proposal"]}]}}),
            "library_apply")
        assert res["renames"][0]["status"] == "uncertain"
        unit = imports.unit(lib.settings, ren["unit"])
        assert unit["steps"]["rename"]["status"] == "uncertain"
        # Still open next time, with its first category — not lost, not new.
        again = _terminal(r.sse("/api/library/analyze", {"session": r.session()}), "library_analyze")
        assert [(o["id"], o["steps"]) for o in again["observation"]["open"]] == [
            (ren["unit"], ["rename", "tag"])]
    finally:
        r.stop()


def test_apply_needs_confirm_and_one_run_at_a_time(routes):
    sid = routes.session()
    r = routes.client.post("/api/library/apply", json={"session": sid}, headers=_H)
    assert r.status_code == 400
    from api.routes import library_workflow
    assert library_workflow._RUN_GATE.acquire(blocking=False)
    try:
        r = routes.client.post("/api/library/analyze", json={"session": sid}, headers=_H)
        assert r.status_code == 429
    finally:
        library_workflow._RUN_GATE.release()


def test_unknown_session_is_404(routes):
    r = routes.client.post("/api/library/analyze", json={"session": "nope"}, headers=_H)
    assert r.status_code == 404


def test_record_step_route_only_resolves_an_uncertain_step(routes):
    """Audit remediation 2026-09-23 (L9/L2): `/api/library/steps` is the
    manual "mark resolved" verb and nothing else. Red before: it recorded ANY
    outcome for any step, unconfirmed and ungated."""
    lib = routes.lib
    e = lib.entry("LLLL0001", "lee2026")
    lib.pdf(e, "PDFL0001", "lee_2026.pdf")
    sid = routes.session()
    routes.client.post("/api/library/observe", json={"session": sid}, headers=_H)
    body = {"session": sid, "unit": "attachment:1:PDFL0001", "step": "tag", "status": "done"}
    r = routes.client.post("/api/library/steps", json=body, headers=_H)
    assert r.status_code == 400, "confirm is required"
    r = routes.client.post("/api/library/steps", json=dict(body, confirm=True), headers=_H)
    assert r.status_code == 409 and r.get_json()["reason"] == "not_uncertain"
    assert "tag" not in (imports.unit(lib.settings, "attachment:1:PDFL0001").get("steps") or {})
    imports.record_step(lib.settings, sid, "attachment:1:PDFL0001", "tag", "uncertain")
    r = routes.client.post("/api/library/steps", json=dict(body, status="maybe", confirm=True), headers=_H)
    assert r.status_code == 400
    r = routes.client.post("/api/library/steps", json=dict(body, confirm=True,
                                                             detail={"x": "y" * 5000}), headers=_H)
    assert r.status_code == 400, "detail is bounded"
    from api.routes import library_workflow
    assert library_workflow._RUN_GATE.acquire(blocking=False)
    try:
        r = routes.client.post("/api/library/steps", json=dict(body, confirm=True), headers=_H)
        assert r.status_code == 429
        r = routes.client.post("/api/library/observe", json={"session": sid}, headers=_H)
        assert r.status_code == 429
    finally:
        library_workflow._RUN_GATE.release()
    r = routes.client.post("/api/library/steps", json=dict(body, confirm=True), headers=_H)
    assert r.status_code == 200
    step = imports.unit(lib.settings, "attachment:1:PDFL0001")["steps"]["tag"]
    assert step["status"] == "done" and step["detail"]["resolved_by_hand"] is True
    # The gate was released by the observation's daemon: a run can start.
    assert library_workflow._RUN_GATE.acquire(blocking=False)
    library_workflow._RUN_GATE.release()


# ---------------------------------------------------------------------------
# The CLI, through the driver's REAL HTTP request replayed into the routes
# (never mocked above the driver — CLAUDE.md §CLI tests).
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self, *_a):
        b, self._body = self._body, b""
        return b

    def readline(self):
        if not self._body:
            return b""
        line, sep, rest = self._body.partition(b"\n")
        self._body = rest
        return line + sep

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self.status

    @property
    def headers(self):
        return {}


def _replay(client):
    import io
    import re
    import urllib.error

    def fake(req, timeout=None):
        m = re.search(r"(/api/.*)$", req.full_url)
        resp = client.open(m.group(1), method=req.get_method(), data=req.data,
                           headers=dict(req.header_items()))
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, resp.status_code, resp.status,
                                         resp.headers, io.BytesIO(resp.get_data()))
        return _Resp(resp.status_code, resp.get_data())
    return fake


def test_the_cli_runs_a_session_end_to_end(routes):
    from click.testing import CliRunner
    from cli.main import cli
    lib = routes.lib
    runner = CliRunner()
    with mock.patch("core.driver.urllib.request.urlopen", side_effect=_replay(routes.client)), \
            mock.patch("core.driver.resolve_base_url", return_value="http://127.0.0.1:1"):
        res = runner.invoke(cli, ["library", "run", "new", "--provider", "ollama", "--model", "llama3"])
        assert res.exit_code == 0, res.output
        sid = json.loads(res.output)["session"]["id"]
        res = runner.invoke(cli, ["library", "run", "analyze", sid])
        assert res.exit_code == 0, res.output
        e = lib.entry("MMMM0001", "moss2026")
        lib.pdf(e, "PDFM0001", "Moss - 2026.pdf")
        res = runner.invoke(cli, ["library", "run", "analyze", sid])
        assert res.exit_code == 0, res.output
        review = json.loads(res.stdout)["library_analyze"]
        assert review["renames"][0]["proposal"] == "jones_2026.pdf", review
        res = runner.invoke(cli, ["library", "run", "show", sid])
        assert json.loads(res.output)["session"]["llm"]["model"] == "llama3"


# ---------------------------------------------------------------------------
# Adversarial review of lot 5 (2026-09-21). Each was red on ea8fa002.
# ---------------------------------------------------------------------------

def test_a_rename_whose_zotero_pointer_did_not_move_is_partial():
    body = {"ok": True, "new_rel": "Z/biblio/k_2026.pdf",
            "zotero": {"linked_attachments": 1, "repointed": 0,
                       "results": [{"item_key": "A", "ok": False, "message": "412"}]}}
    f = FakeT(rename=(200, body))
    res = library_run.apply(f.transport(), "s", {"renames": [
        {"unit": "attachment:1:A", "rel": "x.pdf", "new_name": "k_2026.pdf"}]})
    r = res["renames"][0]
    assert r["status"] == "partial" and "0 of 1" in r["zotero"]
    assert f.steps[0][2] == "done"  # the rename itself landed and is journalled


def test_the_rename_zotero_note_no_longer_claims_every_link_moved():
    import importlib.util
    if importlib.util.find_spec("maintenance") is None:
        import pytest
        pytest.skip("Private rename formatting is absent from this artifact")
    from maintenance.rename import zotero_link_note
    assert "only 0 of 1" in zotero_link_note(1, 0)
    assert "re-pointed" in zotero_link_note(1, 1)


def test_an_approved_file_that_is_not_the_units_is_never_renamed():
    f = FakeT()
    res = library_run.apply(f.transport(), "s", {"renames": [
        {"unit": "attachment:1:A", "rel": "someone_else.pdf", "new_name": "k_2026.pdf"}]})
    assert res["renames"][0]["status"] == "failed"
    assert "rename" not in [c[0] for c in f.calls]


def test_a_tracker_refusal_after_the_rename_is_uncertain_not_done():
    f = FakeT()
    t = f.transport()
    t.note_rename = lambda sid, unit, rel: (400, {"error": "not this unit's file"})
    res = library_run.apply(t, "s", {"renames": [
        {"unit": "attachment:1:A", "rel": "x.pdf", "new_name": "k_2026.pdf"}]})
    assert res["renames"][0]["status"] == "uncertain" and f.steps[0][2] == "uncertain"


def test_a_duplicate_approval_does_not_downgrade_a_done_rename():
    f = FakeT()
    row = {"unit": "attachment:1:A", "rel": "x.pdf", "new_name": "k_2026.pdf"}
    res = library_run.apply(f.transport(), "s", {"renames": [row, dict(row)]})
    assert [c[0] for c in f.calls] == ["rename"]
    assert [s[2] for s in f.steps if s[1] == "rename"] == ["done"]
    assert res["ignored"] == [{"kind": "rename", "unit": "attachment:1:A", "reason": "duplicate or malformed"}]


def test_a_stopped_mirror_is_failed_and_a_partial_one_partial():
    f = FakeT(mirror=(200, {"ok": False, "stopped": "plan_changed"}))
    assert library_run.apply(f.transport(), "s", {"mirror": {"plan_digest": "x"}})["mirror"]["status"] == "failed"
    f = FakeT(mirror=(200, {"ok": False, "failed": {"k": "create: boom"}}))
    assert library_run.apply(f.transport(), "s", {"mirror": {"plan_digest": "x"}})["mirror"]["status"] == "partial"


def test_a_citation_key_of_another_record_is_refused():
    f = FakeT()
    res = library_run.apply(f.transport(), "s", {"tags": [
        {"unit": "attachment:1:A", "citation_key": "someoneElse2020", "tags": ["dep"],
         "targets": {"finder": False}}]})
    assert res["tags"][0]["status"] == "failed" and "apply_tags" not in [c[0] for c in f.calls]
    assert f.steps == []


def test_an_entry_without_pdf_is_tagged_in_zotero_without_a_unit():
    f = FakeT(apply_tags=(200, {"results": {"zotero": {"ok": True},
                                            "note": {"ok": False, "skipped": "no_note"}}}))
    res = library_run.apply(f.transport(), "s", {"tags": [
        {"unit": "entry:1:F", "citation_key": "kf", "tags": ["dep"]}]})
    assert res["tags"][0]["status"] == "done"
    _key, _tags, unit, targets = f.calls[0][1]
    assert unit is None and targets["finder"] is False


def test_a_failed_rename_does_not_undo_the_tag_written_before_it():
    # Tags precede renames since follow-ups lot 2: the tag lands by unit at
    # the old name, and the rename's later failure is its own outcome.
    f = FakeT(rename=(502, {"error": "bad gateway"}))
    res = library_run.apply(f.transport(), "s", {
        "renames": [{"unit": "attachment:1:A", "rel": "x.pdf", "new_name": "k_2026.pdf"}],
        "tags": [{"unit": "attachment:1:A", "citation_key": "k", "tags": ["dep"]}]})
    assert [c[0] for c in f.calls] == ["apply_tags", "rename"]
    assert res["tags"][0]["status"] == "done"
    assert res["renames"][0]["status"] == "uncertain"


def test_unit_pdf_refuses_a_file_outside_the_biblio_folder(tmp_path):
    import sqlite3
    import types
    from api.routes import tagging as tg
    lib = _imports_tests.Library(tmp_path)
    lib.run()
    e = lib.entry("OOOO0001", "out2026")
    other = lib.vault / "Other"
    other.mkdir()
    p = other / "out.pdf"
    p.write_bytes(b"%PDF-1.4 out")
    with sqlite3.connect(lib.db) as con:
        con.execute("INSERT INTO items VALUES (?,?,2,?,?,1,1)", (777, "PDFO0001", "d", "d"))
        con.execute("INSERT INTO itemAttachments VALUES (?,?,?,?,2)", (777, e, str(p), "application/pdf"))
    lib.run()
    rec = types.SimpleNamespace(zotero_item=types.SimpleNamespace(item_key="OOOO0001", library_id=1))
    path, err = tg._unit_pdf("attachment:1:PDFO0001", rec, {
        "settings": lib.settings, "vault_root": lib.vault, "biblio_dir": lib.biblio})
    assert path is None and err["skipped"] == "no_pdf"


def test_a_note_the_mirror_did_not_settle_stays_open_until_it_is_mirrored(tmp_path):
    """Audit remediation 2026-09-23 (L1). This test used to PIN the defect:
    a mirror whose terminal named no key closed every note unit `done`.
    Now a unit closes only on the terminal's `settled_keys`; otherwise it
    stays open ("rescan needed") and the next run can still mirror it."""
    settled: dict = {}
    r = Routes(tmp_path, settled=settled)
    try:
        lib = r.lib
        _terminal(r.sse("/api/library/analyze", {"session": r.session()}), "library_analyze")
        e = lib.entry("NNNN0001", "nash2026")
        lib.note(e, "NOTEN001", "<p>reading</p>")
        sid = r.session()
        rev = _terminal(r.sse("/api/library/analyze", {"session": sid}), "library_analyze")
        assert "note:1:NOTEN001" in [o["id"] for o in rev["observation"]["open"]]
        res = _terminal(r.sse("/api/library/apply", {"session": sid, "confirm": True, "approvals": {
            "mirror": {"plan_digest": rev["mirror"]["plan_digest"]}}}), "library_apply")
        assert res["mirror"]["open"] == [{"unit": "note:1:NOTEN001", "citation_key": "nash2026",
                                          "reason": library_run.RESCAN_NEEDED}]
        again = _terminal(r.sse("/api/library/analyze", {"session": r.session()}), "library_analyze")
        assert "note:1:NOTEN001" in [o["id"] for o in again["observation"]["open"]]

        # The mirror now reports it, with the digest of the note it used.
        import hashlib
        settled["nash2026"] = hashlib.sha256(b"<p>reading</p>").hexdigest()
        sid = r.session()
        rev = _terminal(r.sse("/api/library/analyze", {"session": sid}), "library_analyze")
        res = _terminal(r.sse("/api/library/apply", {"session": sid, "confirm": True, "approvals": {
            "mirror": {"plan_digest": rev["mirror"]["plan_digest"]}}}), "library_apply")
        assert res["mirror"]["status"] == "done" and res["mirror"]["open"] == []
        again = _terminal(r.sse("/api/library/analyze", {"session": r.session()}), "library_analyze")
        assert "note:1:NOTEN001" not in [o["id"] for o in again["observation"]["open"]]
    finally:
        r.stop()


def test_a_client_cannot_widen_the_mirror_units(tmp_path):
    """Audit remediation 2026-09-23 (L7): the note units a mirror settles come
    from the session's own last analysis; a client-sent `units` list can only
    narrow it. Red before: a client list replaced the server's."""
    import hashlib
    settled: dict = {}
    r = Routes(tmp_path, settled=settled)
    try:
        lib = r.lib
        _terminal(r.sse("/api/library/analyze", {"session": r.session()}), "library_analyze")
        e = lib.entry("NNNN0002", "outside2026")
        lib.note(e, "NOTEN002", "<p>outside</p>")
        sid = r.session()
        # Observe only (the note is registered) — no analysis names it.
        r.client.post("/api/library/observe", json={"session": sid}, headers=_H)
        settled["outside2026"] = hashlib.sha256(b"<p>outside</p>").hexdigest()
        res = _terminal(r.sse("/api/library/apply", {"session": sid, "confirm": True, "approvals": {
            "mirror": {"plan_digest": "abc", "units": ["note:1:NOTEN002"]}}}), "library_apply")
        assert res["mirror_units_dropped"] == ["note:1:NOTEN002"]
        assert res["mirror"]["settled"] == []
        unit = imports.unit(lib.settings, "note:1:NOTEN002")
        assert "mirror" not in (unit.get("steps") or {})
    finally:
        r.stop()


def test_the_cli_exits_non_zero_when_a_write_is_uncertain(tmp_path):
    from click.testing import CliRunner
    from cli.main import cli
    r = Routes(tmp_path, rename_status=502)
    try:
        lib = r.lib
        _terminal(r.sse("/api/library/analyze", {"session": r.session()}), "library_analyze")
        e = lib.entry("KKKK0001", "kim2026")
        lib.pdf(e, "PDFK0001", "kim.pdf")
        sid = r.session()
        rev = _terminal(r.sse("/api/library/analyze", {"session": sid}), "library_analyze")
        (ren,) = rev["renames"]
        ap = tmp_path / "ap.json"
        ap.write_text(json.dumps({"renames": [{"unit": ren["unit"], "rel": ren["rel"],
                                               "new_name": ren["proposal"]}]}))
        with mock.patch("core.driver.urllib.request.urlopen", side_effect=_replay(r.client)), \
                mock.patch("core.driver.resolve_base_url", return_value="http://127.0.0.1:1"):
            res = CliRunner().invoke(cli, ["library", "run", "apply", sid, "--approvals-file",
                                           str(ap), "--confirm"])
        assert json.loads(res.stdout)["library_apply"]["renames"][0]["status"] == "uncertain"
        assert res.exit_code == 1
    finally:
        r.stop()
