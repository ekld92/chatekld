"""Lot 1 of the 2026-09-22 follow-ups: a real "no spending limit" option.

``allow_unpriced`` only lifted the price-table requirement and still enforced a
ceiling (it is retired since the 2026-09-23 audit remediation), so an online model was refused at Analyse ("needs a spending limit")
or stopped mid-run by ``check_budget`` however the box was ticked. ``no_limit``
is the explicit acceptance of the cost. Each test here was red on f04b2fce.
"""
from __future__ import annotations

from unittest import mock

import pytest

from tagging import session_llm


def _online(**extra):
    return dict({"provider": "openai", "model": "gpt-4o-mini"}, **extra)


def test_no_limit_accepts_an_online_model_without_a_budget():
    c = session_llm.validate(_online(no_limit=True), {})
    assert c.online and c.no_limit and c.budget_usd is None
    assert c.as_dict()["no_limit"] is True


def test_no_limit_accepts_an_unpriced_online_model():
    c = session_llm.validate({"provider": "openai", "model": "not-a-priced-model", "no_limit": True}, {})
    assert c.no_limit and not c.allow_unpriced


def test_no_limit_with_a_budget_is_refused():
    with pytest.raises(session_llm.SessionLLMError, match="opposite"):
        session_llm.validate(_online(no_limit=True, budget_usd=1.0), {})


def test_without_no_limit_the_budget_is_still_required():
    with pytest.raises(session_llm.SessionLLMError, match="spending limit"):
        session_llm.validate(_online(), {})
    with pytest.raises(session_llm.SessionLLMError, match="spending limit"):
        session_llm.validate(_online(no_limit=False), {})


def test_check_budget_never_raises_under_no_limit():
    c = session_llm.validate(_online(no_limit=True), {})
    session_llm.check_budget(c, 99.0, 5.0)       # would overrun any ceiling
    session_llm.check_budget(c, None, None)      # unmeasured spend, unpriced model
    priced = session_llm.validate(_online(budget_usd=0.01), {})
    with pytest.raises(session_llm.BudgetExceeded):
        session_llm.check_budget(priced, 0.0, 0.02)


def test_a_stored_choice_round_trips_through_validate():
    """The propose route rebuilds the choice from the session record's dict."""
    c = session_llm.validate(_online(no_limit=True), {})
    again = session_llm.validate(c.as_dict(), {})
    assert again.no_limit and again.budget_usd is None


def test_cli_refuses_no_limit_together_with_a_budget_before_any_request():
    from click.testing import CliRunner
    from cli.main import cli
    with mock.patch("core.driver.library_session_create", return_value={"session": {"id": "S"}}) as create:
        res = CliRunner().invoke(cli, ["library", "run", "new", "--provider", "openai", "--model", "gpt-4o-mini",
                                       "--no-limit", "--budget-usd", "1"])
        assert res.exit_code == 2 and "opposite" in res.output
        create.assert_not_called()
        res = CliRunner().invoke(cli, ["library", "run", "new", "--provider", "openai", "--model", "gpt-4o-mini",
                                       "--no-limit"])
        assert res.exit_code == 0, res.output
        assert create.call_args.kwargs["llm"] == {"provider": "openai", "model": "gpt-4o-mini",
                                                  "no_limit": True}
