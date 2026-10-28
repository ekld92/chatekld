"""Audit remediation 2026-09-23, Batch 2 — rename outcomes.

L2: a renamer refusal answered BEFORE the file was touched (``wrote: false``)
is a plain, retryable ``failed``; a post-write refusal stays ``uncertain``
with its journal ``op_id``; an uncertain rename is settled automatically from
the refactor journal when it can tell (analyse, and before a rename in
apply); and the user can resolve one by hand (``papermind library run
resolve``). Each test was proven red on a7a171d9.

Driven over the lot-5 fake transport; the CLI is patched at
``core.driver._http_json`` — never above the driver.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_library_workflow_lot5 import FakeT  # noqa: E402

from accession import library_run  # noqa: E402

A = "attachment:1:A"
ROW = {"id": A, "steps": ["rename", "tag"], "rel": "Z/biblio/x.pdf", "citation_key": "k"}
RENAME = {"unit": A, "rel": "x.pdf", "new_name": "k_2026.pdf"}


def test_a_pre_write_409_is_failed_and_a_retry_reaches_the_renamer():
    f = FakeT(rename=(409, {"error": "Requested destination is not the current canonical "
                                     "rename proposal.", "wrote": False}))
    res = library_run.apply(f.transport(), "s", {"renames": [RENAME]})
    assert res["renames"][0]["status"] == "failed"
    assert f.steps[-1][1:3] == ("rename", "failed")
    # The recorded failure does not block the unit: the next apply calls
    # the renamer again.
    f.units[A]["steps"] = {"rename": {"status": "failed", "detail": f.steps[-1][3]}}
    library_run.apply(f.transport(), "s", {"renames": [RENAME]})
    assert [c[0] for c in f.calls].count("rename") == 2


def test_a_post_write_409_stays_uncertain_and_keeps_the_op_id():
    f = FakeT(rename=(409, {"error": "Rename failed safely: EIO", "op_id": "op1",
                            "op_state": "pending"}))
    res = library_run.apply(f.transport(), "s", {"renames": [RENAME]})
    assert res["renames"][0]["status"] == "uncertain"
    assert f.steps[-1][3]["op_id"] == "op1" and f.steps[-1][3]["op_state"] == "pending"


def _uncertain(f, op_state):
    f.units[A]["steps"] = {"rename": {"status": "uncertain", "detail": {
        "from": "x.pdf", "to": "k_2026.pdf", "op_id": "op1", "op_state": op_state}}}


def test_analyse_settles_an_uncertain_rename_the_journal_shows_applied():
    f = FakeT([ROW])
    _uncertain(f, "pending")
    t = f.transport()
    t.rename_op = lambda op_id: (200, {"state": "applied", "old_rel": "Z/biblio/x.pdf",
                                       "new_rel": "Z/biblio/k_2026.pdf",
                                       "old_exists": False, "new_exists": True})
    out = library_run.analyze(t, "s")
    assert out["settled_renames"] == [{"unit": A, "status": "done"}]
    assert f.renamed == [(A, "Z/biblio/k_2026.pdf")]
    assert f.steps[-1][1:3] == ("rename", "done")
    assert f.steps[-1][3]["settled_from_journal"] == "applied"
    assert out["renames"] == []          # never proposed again


def test_apply_settles_an_undone_rename_as_failed_and_retries_it():
    f = FakeT()
    _uncertain(f, "failed")
    t = f.transport()
    t.rename_op = lambda op_id: (200, {"state": "failed", "old_rel": "Z/biblio/x.pdf",
                                       "new_rel": "Z/biblio/k_2026.pdf",
                                       "old_exists": True, "new_exists": False})

    def unit(uid):
        u = f.units.get(uid)
        return u

    def record(sid, unit_id, step, status, detail, **kw):
        f.steps.append((unit_id, step, status, detail))
        f.units[unit_id]["steps"] = {step: {"status": status, "detail": detail}}
        return 200, {}
    t.record_step = record
    t.unit = unit
    res = library_run.apply(t, "s", {"renames": [RENAME]})
    assert [s[2] for s in f.steps] == ["failed", "done"]
    assert "rename" in [c[0] for c in f.calls]
    assert res["renames"][0]["status"] == "done"


def test_a_journal_that_cannot_tell_leaves_the_rename_uncertain():
    """A no-change safety net, not a regression test: settlement never
    GUESSES. (It passed before the fix too, when nothing was settled.)"""
    for op in ({"state": "pending", "old_exists": True, "new_exists": True},
               {"state": "applied", "old_exists": True, "new_exists": True},
               {"state": "failed", "old_exists": False, "new_exists": False}):
        f = FakeT()
        _uncertain(f, "pending")
        t = f.transport()
        t.rename_op = lambda op_id, op=op: (200, op)
        res = library_run.apply(t, "s", {"renames": [RENAME]})
        assert res["renames"][0]["status"] == "uncertain", op
        assert "rename" not in [c[0] for c in f.calls] and f.steps == []


# ---------------------------------------------------------------------------
# The manual verb: `papermind library run resolve`
# ---------------------------------------------------------------------------

def _cli(args, http):
    from click.testing import CliRunner
    from cli.main import cli
    with mock.patch("core.driver.resolve_base_url", return_value="http://127.0.0.1:1"), \
            mock.patch("core.driver._http_json", side_effect=http):
        return CliRunner().invoke(cli, args)


def test_cli_resolve_sends_one_confirmed_resolution():
    sent = []

    def http(key, body=None, **kw):
        sent.append((key, body))
        return {"unit": {"id": A}}
    res = _cli(["library", "run", "resolve", "S1", A, "rename", "--status", "failed",
                "--note", "file still at old name", "--confirm"], http)
    assert res.exit_code == 0, res.output
    assert sent == [("library_steps", {"session": "S1", "unit": A, "step": "rename",
                                       "status": "failed", "confirm": True,
                                       "detail": {"note": "file still at old name"}})]
    assert json.loads(res.output)["unit"]["id"] == A


def test_cli_resolve_needs_confirm_and_a_known_status():
    res = _cli(["library", "run", "resolve", "S1", A, "rename", "--status", "done"],
               lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")))
    assert res.exit_code == 2 and "--confirm" in res.output, res.output
    res = _cli(["library", "run", "resolve", "S1", A, "rename", "--status", "maybe", "--confirm"],
               lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")))
    assert res.exit_code == 2 and "'maybe' is not one of" in res.output, res.output
