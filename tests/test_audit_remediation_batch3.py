"""Audit remediation 2026-09-23, Batch 3 — cost honesty.

* L3   an unpriced online model runs only under "No spending limit"; a
       budget with an unknown spend always refuses;
* L4   a charge that could not be recorded is reported and makes the spend
       unknown (and a budgeted session then stops);
* L6   the proposal loop backs off and stops after consecutive failures;
* L12  a session and its model are created in ONE transaction; a locked
       store is a 503, not an uncaught 500;
* P5   the spend and the model are columns, results are rows, and an
       old-shape session row migrates.

Each test was proven red on a7a171d9. Routes run for real over the lot-3
harness (fake providers below `stream_chat_messages`); the orchestrator over
the lot-5 fake transport; the CLI at `core.driver._http_json`.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_library_imports as _imports_tests  # noqa: E402  (module import: no re-collection)
import test_library_workflow_lot3 as _lot3  # noqa: E402
from test_library_workflow_lot5 import FakeT  # noqa: E402

from accession import imports, library_run  # noqa: E402
from tagging import session_llm  # noqa: E402

_H = {"X-Requested-With": "ChatEKLD"}


# ---------------------------------------------------------------------------
# L3 — unpriced models only under no_limit
# ---------------------------------------------------------------------------

def test_an_unpriced_model_under_a_budget_is_refused_naming_no_limit():
    with pytest.raises(session_llm.SessionLLMError, match="No spending limit"):
        session_llm.validate({"provider": "openai", "model": "not-priced", "budget_usd": 5}, {})
    with pytest.raises(session_llm.SessionLLMError, match="retired"):
        session_llm.validate({"provider": "openai", "model": "not-priced", "budget_usd": 5,
                              "allow_unpriced": True}, {})


def test_a_budget_after_an_unknown_spend_always_refuses():
    priced = session_llm.validate({"provider": "openai", "model": "gpt-4o-mini", "budget_usd": 5}, {})
    with pytest.raises(session_llm.BudgetExceeded, match="unmeasured"):
        session_llm.check_budget(priced, None, 0.01)
    # A choice built with the retired flag (a stored record) bounds nothing
    # it cannot measure either.
    legacy = session_llm.SessionLLM(provider="openai", model="not-priced", budget_usd=5,
                                    allow_unpriced=True)
    with pytest.raises(session_llm.BudgetExceeded):
        session_llm.check_budget(legacy, 0.0, None)


@pytest.mark.parametrize("model", ["gpt-4o-mini", "not-a-priced-model"])
def test_no_limit_proposes_with_a_priced_or_unpriced_model(tmp_path, model):
    """Coverage the plan asked for, not a regression test: `no_limit` already
    ran both before the fix; this pins that it still does now that it is the
    ONLY way to run the unpriced one."""
    with _lot3.Harness(tmp_path) as h:
        h.providers["openai"] = _lot3.RecordingProvider("openai")
        sid = h.session(provider="openai", model=model, no_limit=True)
        r = h.propose(session=sid)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["spend"]["unknown"] is (model == "not-a-priced-model")


def test_session_create_refuses_an_unpriced_model_with_a_budget(tmp_path):
    with _lot3.Harness(tmp_path) as h:
        r = h.client.post("/api/library/sessions", json={"llm": {
            "provider": "openai", "model": "not-a-priced-model", "budget_usd": 1}}, headers=_H)
        assert r.status_code == 400 and "No spending limit" in r.get_json()["error"]


def test_cli_allow_unpriced_is_a_usage_error_pointing_to_no_limit():
    from click.testing import CliRunner
    from cli.main import cli
    with mock.patch("core.driver._http_json", side_effect=AssertionError("no request")):
        res = CliRunner().invoke(cli, ["library", "run", "new", "--provider", "openai",
                                       "--model", "x", "--budget-usd", "1", "--allow-unpriced"])
    assert res.exit_code == 2 and "--no-limit" in res.output, res.output


# ---------------------------------------------------------------------------
# L4 — a charge that could not be recorded
# ---------------------------------------------------------------------------

def test_a_lost_charge_is_reported_and_stops_a_budgeted_session(tmp_path):
    with _lot3.Harness(tmp_path) as h:
        h.providers["openai"] = _lot3.RecordingProvider("openai")
        sid = h.session(provider="openai", model="gpt-4o-mini", budget_usd=1.0)
        with mock.patch.object(imports, "charge_session",
                               side_effect=sqlite3.OperationalError("database is locked")):
            r = h.propose(session=sid)
        body = r.get_json()
        assert r.status_code == 200 and body["spend_error"] is True, body
        assert body["spend"]["unknown"] is True
        assert imports.session(h.lib.settings, sid)["spend"]["unknown"] is True
        again = h.propose(session=sid, force=True)
        assert again.status_code == 409 and again.get_json()["reason"] == "budget"


def test_a_charge_that_cannot_even_be_marked_unknown_says_unrecorded(tmp_path):
    with _lot3.Harness(tmp_path) as h:
        h.providers["openai"] = _lot3.RecordingProvider("openai")
        sid = h.session(provider="openai", model="gpt-4o-mini", no_limit=True)
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(imports, "charge_session", side_effect=locked), \
                mock.patch.object(imports, "mark_spend_unknown", side_effect=locked):
            body = h.propose(session=sid).get_json()
        assert body["spend_error"] is True and body["spend_unrecorded"] is True


def test_analyse_stops_proposing_when_a_spend_went_unrecorded():
    rows = [{"id": f"attachment:1:{i}", "steps": ["tag"], "citation_key": f"k{i}"} for i in range(3)]
    f = FakeT(rows, propose=(200, {"proposed": [], "spend_unrecorded": True, "spend_error": True}))
    out = library_run.analyze(f.transport(), "s", propose_tags=True)
    assert [c[0] for c in f.calls].count("propose") == 1
    assert out["tags_not_proposed"] == ["attachment:1:1", "attachment:1:2"]
    assert out["tags"][0]["spend_unrecorded"] is True and out["blocked"]


# ---------------------------------------------------------------------------
# L6 — backoff and consecutive-failure abort
# ---------------------------------------------------------------------------

def _rows(n):
    return [{"id": f"attachment:1:{i}", "steps": ["tag"], "citation_key": f"k{i}"} for i in range(n)]


def test_a_failing_provider_is_abandoned_after_the_shared_threshold():
    from tagging.batch import MAX_CONSECUTIVE_FAILURES
    slept: list = []
    f = FakeT(_rows(10), propose=(502, {"error": "bad gateway"}))
    with mock.patch.object(library_run, "_sleep", side_effect=slept.append):
        out = library_run.analyze(f.transport(), "s", propose_tags=True)
    assert [c[0] for c in f.calls].count("propose") == MAX_CONSECUTIVE_FAILURES
    assert any("consecutive errors" in b for b in out["blocked"])
    assert len(out["tags_not_proposed"]) == 10 - MAX_CONSECUTIVE_FAILURES
    assert slept, "backed off between failures"


def test_a_busy_gate_streak_backs_off_on_the_shared_schedule_then_stops():
    from tagging.batch import MAX_CONSECUTIVE_FAILURES, backoff_s
    slept: list = []
    f = FakeT(_rows(8), propose=(429, {"error": "busy"}))
    with mock.patch.object(library_run, "_sleep", side_effect=slept.append):
        library_run.analyze(f.transport(), "s", propose_tags=True)
    expected = sum(backoff_s(n) for n in range(1, MAX_CONSECUTIVE_FAILURES))
    assert sum(slept) == pytest.approx(expected)


def test_a_success_resets_the_failure_count():
    """A guard on the new policy (nothing was ever abandoned before it)."""
    replies = iter([(502, {})] * 4 + [(200, {"proposed": []})] + [(502, {})] * 4 + [(200, {})])
    f = FakeT(_rows(10), propose=lambda *a: next(replies))
    with mock.patch.object(library_run, "_sleep"):
        out = library_run.analyze(f.transport(), "s", propose_tags=True)
    assert [c[0] for c in f.calls].count("propose") == 10
    assert "tags_not_proposed" not in out


def test_the_backoff_polls_the_cancel():
    f = FakeT(_rows(3), propose=(502, {}))
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 3            # cancelled during the first wait
    with mock.patch.object(library_run, "_sleep"):
        out = library_run.analyze(f.transport(), "s", propose_tags=True, should_cancel=cancel)
    assert out["cancelled"] is True
    assert [c[0] for c in f.calls].count("propose") == 1


# ---------------------------------------------------------------------------
# L12 — atomic session create
# ---------------------------------------------------------------------------

def test_a_session_and_its_model_are_created_in_one_transaction(tmp_path):
    with _lot3.Harness(tmp_path) as h:
        with mock.patch.object(imports, "set_session_llm",
                               side_effect=AssertionError("a second transaction")):
            sid = h.session(provider="ollama", model="llama3")
        assert imports.session(h.lib.settings, sid)["llm"]["model"] == "llama3"


def test_a_locked_store_answers_503_and_creates_nothing(tmp_path):
    with _lot3.Harness(tmp_path) as h:
        with mock.patch.object(imports, "begin_session",
                               side_effect=sqlite3.OperationalError("database is locked")):
            r = h.client.post("/api/library/sessions", json={"llm": {
                "provider": "ollama", "model": "llama3"}}, headers=_H)
        assert r.status_code == 503 and "busy" in r.get_json()["error"]


# ---------------------------------------------------------------------------
# P5 — session storage
# ---------------------------------------------------------------------------

def _old_shape_store(lib):
    """A store written by the previous release: everything in the JSON."""
    from accession import reconciliation as rec
    scope = rec.scope(lib.settings)
    with rec.connection(True) as con:
        con.execute('CREATE TABLE import_sessions(id TEXT PRIMARY KEY, scope TEXT NOT NULL, '
                    'started_at REAL NOT NULL, data TEXT NOT NULL)')
        con.execute('CREATE TABLE import_units(scope TEXT NOT NULL, id TEXT NOT NULL, '
                    'data TEXT NOT NULL, PRIMARY KEY(scope, id))')
        data = {"label": "old", "llm": {"provider": "openai", "model": "gpt-4o-mini",
                                        "budget_usd": 2.0},
                "spend": {"usd": 0.25, "unknown": False, "calls": 3},
                "last_analysis": {"at": 10.0, "result": {"renames": ["a"]}},
                "applies": [{"at": 11.0, "result": {"n": 1}}, {"at": 12.0, "result": {"n": 2}}]}
        con.execute("INSERT INTO import_sessions VALUES ('S', ?, 1.0, ?)", (scope, json.dumps(data)))
    return scope


def test_an_old_shape_session_reads_the_same_before_and_after_migration(tmp_path):
    from accession import reconciliation as rec
    lib = _imports_tests.Library(tmp_path)
    # Its own store file: the shared test store already has the new shape.
    with mock.patch.object(rec, "store_path", return_value=tmp_path / "old-store.sqlite3"):
        _check_old_shape_migration(lib)


def _check_old_shape_migration(lib):
    _old_shape_store(lib)
    before = imports.session(lib.settings, "S")          # read-only: not migrated
    assert before["llm"]["model"] == "gpt-4o-mini" and before["spend"]["calls"] == 3
    assert before["applies"] == [{"at": 12.0, "result": {"n": 2}}]
    after = imports.charge_session(lib.settings, "S", 0.5)   # a writer migrates
    assert after == {"usd": 0.75, "unknown": False, "calls": 4}
    got = imports.session(lib.settings, "S")
    assert got["llm"]["model"] == "gpt-4o-mini" and got["label"] == "old"
    assert got["last_analysis"] == {"at": 10.0, "result": {"renames": ["a"]}}
    assert got["applies"] == [{"at": 12.0, "result": {"n": 2}}] and got["apply_count"] == 2
    from accession import reconciliation as rec
    with rec.connection() as con:
        raw = json.loads(con.execute("SELECT data FROM import_sessions WHERE id='S'").fetchone()[0])
    assert set(raw) == {"label"}, raw


def test_a_charge_touches_only_the_spend_columns(tmp_path):
    lib = _imports_tests.Library(tmp_path)
    sid = imports.begin_session(lib.settings, label="x", llm={"provider": "ollama", "model": "m"})
    imports.record_session_result(lib.settings, sid, "apply", {"big": "x" * 1000})
    from accession import reconciliation as rec
    with rec.connection() as con:
        before = con.execute("SELECT data, llm FROM import_sessions WHERE id=?", (sid,)).fetchone()
    imports.charge_session(lib.settings, sid, 0.1)
    with rec.connection() as con:
        after = con.execute("SELECT data, llm FROM import_sessions WHERE id=?", (sid,)).fetchone()
    assert before == after
    assert "big" not in before[0]


def test_results_are_rows_bounded_per_session(tmp_path):
    lib = _imports_tests.Library(tmp_path)
    sid = imports.begin_session(lib.settings)
    for n in range(imports._KEEP_APPLIES + 3):
        imports.record_session_result(lib.settings, sid, "apply", {"n": n}, at=float(n))
    got = imports.session(lib.settings, sid)
    assert got["apply_count"] == imports._KEEP_APPLIES
    assert got["applies"] == [{"at": float(imports._KEEP_APPLIES + 2),
                               "result": {"n": imports._KEEP_APPLIES + 2}}]
