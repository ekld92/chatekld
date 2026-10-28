from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import pytest
from core.llm.adapters.openai import OpenAIProvider
from core.llm.adapters import openai_responses as wire
from core.llm.types import (
    LLMRequest,
    ToolSchema,
    ToolTurn,
    ToolResult,
    FinishReason,
    LLMError,
)


def req(**kwargs):
    return LLMRequest(
        model="gpt-6.1-sol",
        tools=[ToolSchema("read_fixture", "Read", {})],
        max_tokens=1000,
        reasoning_effort="medium",
        **kwargs,
    )


def response(output=None, status="completed"):
    return NS(
        output=output or [],
        status=status,
        incomplete_details=NS(reason="max_output_tokens"),
        usage=NS(
            input_tokens=100,
            output_tokens=70,
            input_tokens_details=NS(cached_tokens=20),
        ),
    )


def test_targeted_routing_and_official_env_override(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1/")
    assert wire.applies(OpenAIProvider(), req())
    assert not wire.applies(OpenAIProvider(base_url="https://local.invalid/v1"), req())
    p = OpenAIProvider()
    p.name = "openrouter"
    assert not wire.applies(p, req())
    assert not wire.applies(
        OpenAIProvider(), LLMRequest(model="gpt-4o", tools=req().tools)
    )
    assert not wire.applies(OpenAIProvider(), LLMRequest(model="gpt-6.1-sol"))


def test_reasoning_limits_tools_and_plain_contract(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    p = OpenAIProvider()
    r = req()
    x = wire.payload(p, r)
    assert x["reasoning"] == {"effort": "medium"}
    assert x["max_output_tokens"] == 1000 and x["store"] is False
    assert x["include"] == ["reasoning.encrypted_content"]
    assert x["tools"][0]["name"] == "read_fixture"
    assert "temperature" not in x and "max_completion_tokens" not in x
    r.reasoning_effort = "none"
    with pytest.raises(LLMError):
        wire.payload(p, r)
    with pytest.raises(LLMError):
        p._common_params(r)
    r.reasoning_effort = "medium"
    r.temperature = 0.5
    assert "temperature" not in p._common_params(r)


def test_tool_decode_and_exact_reasoning_replay():
    p = OpenAIProvider()
    r = req()
    items = [
        {"type": "reasoning", "id": "rs1", "encrypted_content": "opaque"},
        {
            "type": "function_call",
            "id": "fc1",
            "call_id": "call1",
            "name": "read_fixture",
            "arguments": "{}",
        },
    ]
    out = wire.decode(p, r, response(items))
    assert (
        out.finish_reason == FinishReason.TOOL_USE and out.tool_calls[0].id == "call1"
    )
    assert out.usage.output_tokens == 70 and out.usage.cached_input_tokens == 20
    r.tool_history = [
        ToolTurn(
            calls=out.tool_calls,
            provider_state=out.provider_state,
            results=[ToolResult("call1", "fixture")],
        )
    ]
    r.tools = []
    assert wire.applies(p, r)
    x = wire.payload(p, r)
    assert x["input"][:2] == items
    assert x["input"][2] == {
        "type": "function_call_output",
        "call_id": "call1",
        "output": "fixture",
    }
    r.tool_history[0].provider_state["model"] = "different"
    with pytest.raises(LLMError):
        wire.payload(p, r)


def test_truncation_refusal_and_failed():
    p = OpenAIProvider()
    r = req()
    assert (
        wire.decode(p, r, response(status="incomplete")).finish_reason
        == FinishReason.LENGTH
    )
    out = wire.decode(
        p,
        r,
        response(
            [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]
        ),
    )
    assert out.finish_reason == FinishReason.CONTENT_FILTER
    x = response(status="failed")
    x.error = "failed"
    assert wire.decode(p, r, x).finish_reason == FinishReason.ERROR


class Handle:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def __iter__(self):
        return iter(self.events)

    def close(self):
        self.closed = True


def test_stream_final_usage_and_close():
    p = OpenAIProvider()
    r = req()
    client = Mock()
    done = response(
        [{"type": "message", "content": [{"type": "output_text", "text": "bonjour"}]}]
    )
    h = Handle(
        [
            NS(type="response.output_text.delta", delta="bonjour"),
            NS(type="response.completed", response=done),
        ]
    )
    client.responses.create.return_value = h
    with (
        patch.object(p, "_client", return_value=client),
        patch.object(wire.usage_tracker, "record") as record,
    ):
        s = p.stream(r)
        assert "".join(s.response_gen) == "bonjour"
        assert s.final.text == "bonjour" and s.final.usage.output_tokens == 70
        assert h.closed
        record.assert_called_once()


def test_cancel_and_midstream_failure_never_restart():
    p = OpenAIProvider()
    r = req()
    client = Mock()
    h = Handle([NS(type="response.output_text.delta", delta="partial")])
    client.responses.create.return_value = h
    with (
        patch.object(p, "_client", return_value=client),
        patch.object(wire.usage_tracker, "record") as record,
    ):
        s = p.stream(r)
        assert next(s.response_gen) == "partial"
        s.cancel()
        assert h.closed and s.final.text == "partial"
        assert record.call_args.kwargs["error_category"] == "cancelled"
    h = Handle([NS(type="response.output_text.delta", delta="partial")])
    client.responses.create.return_value = h
    with (
        patch.object(p, "_client", return_value=client),
        patch.object(wire.usage_tracker, "record"),
    ):
        s = p.stream(r)
        assert next(s.response_gen) == "partial"
        with pytest.raises(LLMError):
            next(s.response_gen)
        assert h.closed and s.final.finish_reason == FinishReason.ERROR


def test_sol_never_heals_by_removing_reasoning_or_tools():
    p = OpenAIProvider()
    exc = NS(body={"param": "reasoning_effort"})
    params = {"model": "gpt-6.1-sol", "reasoning_effort": "medium", "tools": [{}]}
    assert p._heal_unsupported_param(params, exc, set()) == ""
    assert "reasoning_effort" in params and "tools" in params


def test_generate_routes_and_records_usage():
    p = OpenAIProvider(max_retries=1)
    client = Mock()
    client.responses.create.return_value = response(
        [{"type": "message", "content": [{"type": "output_text", "text": "fixture"}]}]
    )
    with (
        patch.object(p, "_client", return_value=client),
        patch.object(wire.usage_tracker, "record") as record,
    ):
        out = p.generate(req())
        assert out.text == "fixture"
        client.chat.completions.create.assert_not_called()
        assert record.call_args.kwargs["usage"].output_tokens == 70


def test_sol_official_override_uses_completion_token_cap(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    params = OpenAIProvider()._common_params(req())
    assert params["max_completion_tokens"] == 1000
    assert "max_tokens" not in params


def test_reported_sol_cache_writes_are_priced():
    from core.llm.usage import estimate_cost_usd
    raw = response()
    raw.usage.input_tokens_details.cache_write_tokens = 60
    decoded = wire.decode(OpenAIProvider(), req(), raw)
    assert decoded.usage.cache_creation_input_tokens == 60
    assert estimate_cost_usd('gpt-6.1-sol', decoded.usage) == .000892


@pytest.mark.parametrize("code,category", [
    ("invalid_prompt", "invalid_request"),
    ("insufficient_quota", "quota"),
    ("invalid_api_key", "auth"),
    ("rate_limit_exceeded", "rate_limit"),
    ("server_error", "server_error"),
    ("unrecognised", "unknown"),
    ("slow_down", "rate_limit"),
    ("server_is_overloaded", "server_error"),
    ("credit_balance_exhausted", "quota"),
])
def test_structured_error_categories(code, category):
    error = wire.response_error(OpenAIProvider(), req(), {"code": code, "message": code})
    assert error.category.value == category
    assert code in error.message


def test_failed_generation_keeps_usage_and_terminal_error_never_retries():
    from core.llm.usage import UsageTracker
    p = OpenAIProvider(max_retries=3)
    client = Mock()
    failed = response(status="failed")
    failed.error = NS(code="invalid_prompt", message="invalid prompt")
    failed.usage.input_tokens_details.cache_write_tokens = 60
    client.responses.create.return_value = failed
    tracker = UsageTracker()
    with patch.object(p, "_client", return_value=client), patch.object(wire, "usage_tracker", tracker):
        with pytest.raises(LLMError, match="invalid_request"):
            p.generate(req())
    client.responses.create.assert_called_once()
    row, = tracker.recent()
    assert not row.success and row.error_category == "invalid_request"
    assert (row.input_tokens, row.output_tokens, row.cache_creation_input_tokens) == (100, 70, 60)
    assert row.cost_usd == .000892


def test_transient_failed_attempt_is_counted_before_success():
    from core.llm.usage import UsageTracker
    p = OpenAIProvider(max_retries=2)
    client = Mock()
    failed = response(status="failed")
    failed.error = NS(code="server_error", message="retry")
    client.responses.create.side_effect = [failed, response()]
    tracker = UsageTracker()
    with patch.object(p, "_client", return_value=client), patch.object(wire, "usage_tracker", tracker), patch("core.llm.retry.time.sleep"):
        p.generate(req())
    rows = tracker.recent()
    assert len(rows) == 2 and [r.success for r in rows] == [False, True]
    assert all(r.output_tokens == 70 and r.cost_usd > 0 for r in rows)


def test_failed_stream_preserves_terminal_usage():
    from core.llm.usage import UsageTracker
    p = OpenAIProvider()
    failed = response(status="failed")
    failed.error = NS(code="invalid_prompt", message="bad")
    h = Handle([NS(type="response.output_text.delta", delta="partial"), NS(type="response.failed", response=failed)])
    client = Mock()
    client.responses.create.return_value = h
    tracker = UsageTracker()
    with patch.object(p, "_client", return_value=client), patch.object(wire, "usage_tracker", tracker):
        stream = p.stream(req())
        assert next(stream.response_gen) == "partial"
        with pytest.raises(LLMError, match="invalid_request"):
            next(stream.response_gen)
    assert h.closed and stream.final.usage.output_tokens == 70
    row, = tracker.recent()
    assert not row.success and row.input_tokens == 100 and row.cost_usd > 0


def test_stream_error_event_is_terminal_and_not_replayed():
    p = OpenAIProvider(max_retries=3)
    h = Handle([NS(type="error", code="insufficient_quota", message="quota exhausted")])
    client = Mock()
    client.responses.create.return_value = h
    with patch.object(p, "_client", return_value=client), patch.object(wire.usage_tracker, "record"):
        stream = p.stream(req())
        with pytest.raises(LLMError, match="quota"):
            list(stream.response_gen)
    assert h.closed
    client.responses.create.assert_called_once()


def test_long_context_rates_and_override_precedence():
    from core.llm.types import LLMUsage
    from core.llm.usage import estimate_cost_usd
    assert estimate_cost_usd("gpt-6.1-sol", LLMUsage(input_tokens=272000, output_tokens=1000)) == .554
    assert estimate_cost_usd("gpt-6.1-sol", LLMUsage(input_tokens=300000, output_tokens=1000)) == 1.215
    usage = LLMUsage(input_tokens=300000, output_tokens=1000, cached_input_tokens=20000, cache_creation_input_tokens=60000)
    assert estimate_cost_usd("gpt-6.1-sol", usage) == 1.199
    rates = {"gpt-6.1-sol": {"input": 2, "output": 10, "cached_input": .1}}
    assert estimate_cost_usd("gpt-6.1-sol", usage, rates) == .602


def test_agent_rejects_mixed_batch_before_dispatch():
    from core.agent.loop import run_agent_loop
    from core.agent.tools import ToolRegistry, ToolSpec
    p = OpenAIProvider(max_retries=1)
    runner = Mock(return_value="fixture")
    tools = ToolRegistry([ToolSpec(ToolSchema("read_fixture", "Read", {"type": "object", "properties": {}}), runner)])
    calls = [
        {"type": "function_call", "call_id": "good", "name": "read_fixture", "arguments": "{}"},
        {"type": "function_call", "call_id": "bad", "name": "read_fixture", "arguments": "{"},
    ]
    client = Mock()
    client.responses.create.return_value = response(calls)
    events = []
    def resolve(policy, *, request, **kwargs):
        return p.generate(request), "openai"
    with patch.object(p, "_client", return_value=client), patch.object(wire.usage_tracker, "record"), patch("core.agent.loop.resolve_chat_provider", side_effect=resolve):
        run_agent_loop(user_message="test", user_system_prompt="", provider_name="openai", model="gpt-6.1-sol", tools=tools, cfg={}, on_event=events.append)
    runner.assert_not_called()
    assert [type(e).__name__ for e in events] == ["IterationEvent", "ErrorEvent"]


def test_agent_keeps_fallback_model_for_next_tool_turn():
    from core.agent.loop import run_agent_loop
    from core.agent.tools import ToolRegistry, ToolSpec
    from core.llm.types import LLMResponse, ToolCall
    from core.llm.policy import FallbackPolicy
    primary = Mock()
    primary.generate.side_effect = LLMError(wire.ErrorCategory.NETWORK, "offline")
    fallback = Mock()
    call = ToolCall("fixture_call", "read_fixture", {})
    fallback.generate.side_effect = [
        LLMResponse(provider="ollama", model="fixture_model", finish_reason=FinishReason.TOOL_USE, tool_calls=[call]),
        LLMResponse(provider="ollama", model="fixture_model", text="done"),
    ]
    runner = Mock(return_value="fixture")
    tools = ToolRegistry([ToolSpec(ToolSchema("read_fixture", "Read", {"type": "object", "properties": {}}), runner)])
    providers = {"openai": primary, "ollama": fallback}
    events = []
    cfg = {"llm": "fixture_model", "llm_fallback_provider": "ollama"}
    with patch("core.llm.factory.get_llm_provider", side_effect=lambda name, **kw: providers[name]), patch("core.llm.factory._capture_request"), patch("core.agent.loop.parse_policy_from_config", return_value=FallbackPolicy("openai", "ollama")):
        run_agent_loop(user_message="test", user_system_prompt="", provider_name="openai", model="gpt-6.1-sol", tools=tools, cfg=cfg, on_event=events.append, max_iterations=3)
    primary.generate.assert_called_once()
    assert fallback.generate.call_count == 2
    second = fallback.generate.call_args.args[0]
    assert second.model == "fixture_model"
    assert second.tool_history[0].calls[0].id == "fixture_call"
    assert "fixture" in second.tool_history[0].results[0].content
    runner.assert_called_once()
    assert type(events[-1]).__name__ == "DoneEvent"



def test_agent_dispatches_multiple_responses_turns_and_forces_final():
    from core.agent.loop import run_agent_loop
    from core.agent.tools import ToolRegistry, ToolSpec
    p = OpenAIProvider(max_retries=1)
    runner = Mock(side_effect=["first result", "second result"])
    tools = ToolRegistry([ToolSpec(ToolSchema("read_fixture", "Read", {"type": "object", "properties": {}}), runner)])
    def items(n):
        return [
            {"type": "reasoning", "id": f"rs{n}", "encrypted_content": f"opaque{n}"},
            {"type": "function_call", "id": f"fc{n}", "call_id": f"call{n}", "name": "read_fixture", "arguments": "{}"},
        ]
    client = Mock()
    client.responses.create.side_effect = [response(items(1)), response(items(2)), response([{"type": "message", "content": [{"type": "output_text", "text": "done"}]}])]
    events = []
    with patch("core.llm.factory.get_llm_provider", return_value=p), patch("core.llm.factory._capture_request"), patch.object(p, "_client", return_value=client), patch.object(wire.usage_tracker, "record"):
        run_agent_loop(user_message="test", user_system_prompt="", provider_name="openai", model="gpt-6.1-sol", tools=tools, cfg={}, on_event=events.append, max_iterations=3)
    assert runner.call_count == 2
    sent = client.responses.create.call_args.kwargs
    assert sent["tool_choice"] == "none"
    assert [i["call_id"] for i in sent["input"] if i.get("type") == "function_call_output"] == ["call1", "call2"]
    assert [i["encrypted_content"] for i in sent["input"] if i.get("type") == "reasoning"] == ["opaque1", "opaque2"]
    assert type(events[-1]).__name__ == "DoneEvent"
