"""Tests for the core.agent package — tool registry, vault tool wiring,
and the per-turn usage accumulator.

The agent loop itself lands in slice 5 with its own test class.

These tests are dependency-free: they mock the ObsidianVaultManager
duck-type with MagicMock, so they run without llama_index installed.
"""
import json
import unittest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# ToolRegistry — schema validation, invocation, truncation
# ---------------------------------------------------------------------------

class TestToolRegistry(unittest.TestCase):
    def _make_spec(self, name="x", **kwargs):
        from core.agent.tools import ToolSpec
        from core.llm.types import ToolSchema
        schema = ToolSchema(
            name=name,
            description=kwargs.get("description", "d"),
            parameters=kwargs.get(
                "parameters",
                {
                    "type": "object",
                    "properties": {
                        "q": {"type": "string"},
                        "n": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["q"],
                },
            ),
        )
        return ToolSpec(
            schema=schema,
            runner=kwargs.get("runner", lambda args: "ok"),
            max_output_chars=kwargs.get("max_output_chars", 50),
        )

    def _make_call(self, name="x", args=None, raw_args=None):
        from core.llm.types import ToolCall
        return ToolCall(
            id="call_1",
            name=name,
            arguments=args if args is not None else {"q": "hello"},
            raw_arguments=raw_args or json.dumps(args or {"q": "hello"}),
        )

    def test_registry_indexes_by_name(self):
        from core.agent.tools import ToolRegistry
        reg = ToolRegistry([self._make_spec("a"), self._make_spec("b")])
        self.assertEqual(reg.names(), ["a", "b"])
        self.assertIsNotNone(reg.get("a"))
        self.assertIsNone(reg.get("z"))

    def test_registry_rejects_duplicate_names(self):
        from core.agent.tools import ToolRegistry
        with self.assertRaisesRegex(ValueError, "duplicate"):
            ToolRegistry([self._make_spec("a"), self._make_spec("a")])

    def test_schemas_returns_in_registration_order(self):
        from core.agent.tools import ToolRegistry
        reg = ToolRegistry([self._make_spec("z"), self._make_spec("a")])
        names = [s.name for s in reg.schemas]
        self.assertEqual(names, ["z", "a"])

    # ---- validate_args -----------------------------------------------

    def test_validate_args_accepts_valid_call(self):
        from core.agent.tools import ToolRegistry
        reg = ToolRegistry([self._make_spec()])
        out = reg.validate_args(self._make_call(args={"q": "hi"}))
        self.assertEqual(out, {"q": "hi"})

    def test_validate_args_rejects_missing_required(self):
        from core.agent.tools import ToolArgError, ToolRegistry
        reg = ToolRegistry([self._make_spec()])
        with self.assertRaisesRegex(ToolArgError, "required arg"):
            reg.validate_args(self._make_call(args={"n": 3}))

    def test_validate_args_rejects_wrong_type(self):
        from core.agent.tools import ToolArgError, ToolRegistry
        reg = ToolRegistry([self._make_spec()])
        with self.assertRaisesRegex(ToolArgError, "must be a string"):
            reg.validate_args(self._make_call(args={"q": 42}))

    def test_validate_args_rejects_out_of_range_integer(self):
        from core.agent.tools import ToolArgError, ToolRegistry
        reg = ToolRegistry([self._make_spec()])
        with self.assertRaisesRegex(ToolArgError, ">= 1"):
            reg.validate_args(self._make_call(args={"q": "x", "n": 0}))
        with self.assertRaisesRegex(ToolArgError, "<= 10"):
            reg.validate_args(self._make_call(args={"q": "x", "n": 11}))

    def test_validate_args_rejects_bool_as_integer(self):
        """bool is a subclass of int in Python; the validator must
        reject it as a wrong type so True/False can't slip through
        an int-typed parameter."""
        from core.agent.tools import ToolArgError, ToolRegistry
        reg = ToolRegistry([self._make_spec()])
        with self.assertRaisesRegex(ToolArgError, "must be an integer"):
            reg.validate_args(self._make_call(args={"q": "x", "n": True}))

    def test_validate_args_rejects_enum_violation(self):
        from core.agent.tools import ToolArgError, ToolRegistry
        spec = self._make_spec(
            parameters={
                "type": "object",
                "properties": {"mode": {"type": "string", "enum": ["a", "b"]}},
                "required": ["mode"],
            },
        )
        reg = ToolRegistry([spec])
        with self.assertRaisesRegex(ToolArgError, "must be one of"):
            reg.validate_args(self._make_call(args={"mode": "c"}))

    def test_validate_args_rejects_unknown_tool(self):
        from core.agent.tools import ToolArgError, ToolRegistry
        reg = ToolRegistry([self._make_spec("known")])
        with self.assertRaisesRegex(ToolArgError, "unknown tool"):
            reg.validate_args(self._make_call("missing"))

    def test_validate_args_rejects_non_object(self):
        from core.agent.tools import ToolArgError, ToolRegistry
        from core.llm.types import ToolCall
        reg = ToolRegistry([self._make_spec()])
        call = ToolCall(id="c", name="x", arguments=[1, 2, 3], raw_arguments="[1,2,3]")
        with self.assertRaisesRegex(ToolArgError, "must be a JSON object"):
            reg.validate_args(call)

    def test_validate_args_passes_extra_keys_through(self):
        from core.agent.tools import ToolRegistry
        reg = ToolRegistry([self._make_spec()])
        out = reg.validate_args(self._make_call(args={"q": "x", "extra": "y"}))
        self.assertEqual(out["extra"], "y")

    # ---- invoke ------------------------------------------------------

    def test_invoke_validates_then_calls_runner(self):
        from core.agent.tools import ToolRegistry
        seen = {}
        def _runner(args):
            seen.update(args)
            return "result"
        reg = ToolRegistry([self._make_spec(runner=_runner)])
        out = reg.invoke(self._make_call(args={"q": "hi"}))
        self.assertEqual(out, "result")
        self.assertEqual(seen, {"q": "hi"})

    def test_invoke_propagates_runner_exceptions(self):
        from core.agent.tools import ToolRegistry

        def _runner(args):
            raise RuntimeError("boom")

        reg = ToolRegistry([self._make_spec(runner=_runner)])
        # The agent loop is responsible for catching this and converting
        # to ToolResult(is_error=True); the registry itself does NOT swallow.
        with self.assertRaisesRegex(RuntimeError, "boom"):
            reg.invoke(self._make_call())

    # ---- truncate ----------------------------------------------------

    def test_truncate_under_cap_returns_unchanged(self):
        from core.agent.tools import ToolRegistry
        reg = ToolRegistry([self._make_spec(max_output_chars=100)])
        out, truncated = reg.truncate("x", "short")
        self.assertEqual(out, "short")
        self.assertFalse(truncated)

    def test_truncate_over_cap_marks_and_appends_notice(self):
        from core.agent.tools import ToolRegistry
        reg = ToolRegistry([self._make_spec(max_output_chars=10)])
        out, truncated = reg.truncate("x", "a" * 100)
        self.assertTrue(truncated)
        self.assertEqual(out[:10], "a" * 10)
        self.assertIn("truncated", out)

    def test_truncate_unknown_tool_uses_default_cap(self):
        from core.agent.tools import ToolRegistry
        reg = ToolRegistry([self._make_spec("x")])
        # Default cap is 8000 — short string passes through.
        out, truncated = reg.truncate("missing", "small")
        self.assertEqual(out, "small")
        self.assertFalse(truncated)

    # ---- wrap_untrusted ----------------------------------------------

    def test_wrap_untrusted_contains_preamble_and_attributes(self):
        from core.agent.tools import wrap_untrusted
        wrapped = wrap_untrusted("vault.search", "stuff", truncated=True)
        self.assertIn("untrusted", wrapped.lower())
        self.assertIn('tool="vault.search"', wrapped)
        self.assertIn('truncated="true"', wrapped)
        self.assertIn("stuff", wrapped)

    def test_wrap_untrusted_marks_not_truncated_by_default(self):
        from core.agent.tools import wrap_untrusted
        wrapped = wrap_untrusted("vault.search", "stuff")
        self.assertIn('truncated="false"', wrapped)


# ---------------------------------------------------------------------------
# Vault tools — build_vault_tools + each concrete tool
# ---------------------------------------------------------------------------

def _make_ctx(**overrides):
    from core.agent.vault_tools import VaultToolContext
    defaults = dict(
        llm_name="qwen2.5",
        embed_name="nomic-embed-text",
        provider_name="ollama",
    )
    defaults.update(overrides)
    return VaultToolContext(**defaults)


class TestVaultTools(unittest.TestCase):
    def test_build_vault_tools_returns_three_specs(self):
        from core.agent.vault_tools import build_vault_tools
        specs = build_vault_tools(MagicMock(), _make_ctx())
        names = [s.schema.name for s in specs]
        self.assertEqual(names, ["vault.search", "vault.read_note", "vault.list_materials"])

    def test_search_schema_caps_top_k_at_12(self):
        from core.agent.vault_tools import build_vault_tools
        specs = build_vault_tools(MagicMock(), _make_ctx())
        search = next(s for s in specs if s.schema.name == "vault.search")
        params = search.schema.parameters
        self.assertEqual(params["properties"]["top_k"]["maximum"], 12)
        self.assertEqual(params["required"], ["query"])

    # ---- vault.search -----------------------------------------------

    def test_search_calls_manager_retrieve_with_context(self):
        from core.agent.vault_tools import build_vault_tools
        from core.llm.types import RetrievedChunk
        manager = MagicMock()
        manager.retrieve.return_value = [
            RetrievedChunk(text="alpha", source="a.md", score=0.9),
            RetrievedChunk(text="beta", source="b.md", score=0.4),
        ]
        ctx = _make_ctx(hybrid_enabled=True, reranker_enabled=True, reranker_model="ms-marco")
        specs = build_vault_tools(manager, ctx)
        search = next(s for s in specs if s.schema.name == "vault.search")

        raw = search.runner({"query": "test"})
        kwargs = manager.retrieve.call_args.kwargs
        self.assertEqual(manager.retrieve.call_args.args, ("test",))
        self.assertEqual(kwargs["provider_name"], "ollama")
        self.assertTrue(kwargs["hybrid_enabled"])
        self.assertTrue(kwargs["reranker_enabled"])
        self.assertEqual(kwargs["reranker_model"], "ms-marco")
        self.assertTrue(kwargs["top_k_explicit"])  # agent always supplies top_k

        payload = json.loads(raw)
        self.assertEqual(payload["result_count"], 2)
        self.assertEqual(payload["results"][0]["source"], "a.md")
        self.assertAlmostEqual(payload["results"][0]["score"], 0.9)
        self.assertEqual(payload["results"][0]["snippet"], "alpha")

    def test_search_uses_top_k_from_args_when_present(self):
        from core.agent.vault_tools import build_vault_tools
        manager = MagicMock()
        manager.retrieve.return_value = []
        specs = build_vault_tools(manager, _make_ctx())
        search = next(s for s in specs if s.schema.name == "vault.search")
        search.runner({"query": "x", "top_k": 3})
        self.assertEqual(manager.retrieve.call_args.kwargs["top_k"], 3)

    def test_search_defaults_top_k_when_missing(self):
        from core.agent.vault_tools import build_vault_tools
        manager = MagicMock()
        manager.retrieve.return_value = []
        specs = build_vault_tools(manager, _make_ctx())
        search = next(s for s in specs if s.schema.name == "vault.search")
        search.runner({"query": "x"})
        self.assertEqual(manager.retrieve.call_args.kwargs["top_k"], 6)

    def test_search_caps_snippet_at_800_chars(self):
        from core.agent.vault_tools import build_vault_tools
        from core.llm.types import RetrievedChunk
        manager = MagicMock()
        manager.retrieve.return_value = [
            RetrievedChunk(text="a" * 5000, source="big.md", score=0.5),
        ]
        specs = build_vault_tools(manager, _make_ctx())
        search = next(s for s in specs if s.schema.name == "vault.search")
        payload = json.loads(search.runner({"query": "x"}))
        # 800-char cap + " ..." suffix.
        self.assertLessEqual(len(payload["results"][0]["snippet"]), 805)
        self.assertTrue(payload["truncated"])

    def test_search_empty_results_payload(self):
        from core.agent.vault_tools import build_vault_tools
        manager = MagicMock()
        manager.retrieve.return_value = []
        specs = build_vault_tools(manager, _make_ctx())
        search = next(s for s in specs if s.schema.name == "vault.search")
        payload = json.loads(search.runner({"query": "x"}))
        self.assertEqual(payload["result_count"], 0)
        self.assertFalse(payload["truncated"])

    # ---- vault.read_note --------------------------------------------

    def test_read_note_calls_manager_with_max_chars(self):
        from core.agent.vault_tools import build_vault_tools
        manager = MagicMock()
        manager.read_note.return_value = ("body text", False)
        specs = build_vault_tools(manager, _make_ctx())
        read = next(s for s in specs if s.schema.name == "vault.read_note")
        raw = read.runner({"rel_path": "notes/x.md"})
        kwargs = manager.read_note.call_args.kwargs
        self.assertEqual(manager.read_note.call_args.args, ("notes/x.md",))
        self.assertEqual(kwargs["max_chars"], 32000)
        payload = json.loads(raw)
        self.assertEqual(payload["rel_path"], "notes/x.md")
        self.assertEqual(payload["text"], "body text")
        self.assertEqual(payload["char_count"], len("body text"))
        self.assertFalse(payload["truncated"])

    def test_read_note_propagates_truncated_flag(self):
        from core.agent.vault_tools import build_vault_tools
        manager = MagicMock()
        manager.read_note.return_value = ("big stuff", True)
        specs = build_vault_tools(manager, _make_ctx())
        read = next(s for s in specs if s.schema.name == "vault.read_note")
        payload = json.loads(read.runner({"rel_path": "x.md"}))
        self.assertTrue(payload["truncated"])

    def test_read_note_propagates_manager_exception(self):
        """Read errors from the manager (FileNotFoundError, ValueError,
        IOError) must propagate so the agent loop can convert them to
        ToolResult(is_error=True). The tool runner does not catch."""
        from core.agent.vault_tools import build_vault_tools
        manager = MagicMock()
        manager.read_note.side_effect = ValueError("Path is outside the vault")
        specs = build_vault_tools(manager, _make_ctx())
        read = next(s for s in specs if s.schema.name == "vault.read_note")
        with self.assertRaisesRegex(ValueError, "outside the vault"):
            read.runner({"rel_path": "../etc/passwd"})

    # ---- vault.list_materials ---------------------------------------

    def test_list_materials_filters_by_substring(self):
        from core.agent.vault_tools import build_vault_tools
        manager = MagicMock()
        manager.get_indexed_materials.return_value = {
            "materials": [
                {"source": "work/meeting.md", "extension": ".md", "chunk_count": 3},
                {"source": "personal/diary.md", "extension": ".md", "chunk_count": 5},
                {"source": "work/notes.md", "extension": ".md", "chunk_count": 2},
            ],
        }
        specs = build_vault_tools(manager, _make_ctx())
        lister = next(s for s in specs if s.schema.name == "vault.list_materials")
        payload = json.loads(lister.runner({"filter": "work"}))
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["returned"], 2)
        self.assertEqual(payload["materials"][0]["source"], "work/meeting.md")

    def test_list_materials_filter_is_case_insensitive(self):
        from core.agent.vault_tools import build_vault_tools
        manager = MagicMock()
        manager.get_indexed_materials.return_value = {
            "materials": [{"source": "Work/X.md", "extension": ".md", "chunk_count": 1}],
        }
        specs = build_vault_tools(manager, _make_ctx())
        lister = next(s for s in specs if s.schema.name == "vault.list_materials")
        payload = json.loads(lister.runner({"filter": "WORK"}))
        self.assertEqual(payload["total"], 1)

    def test_list_materials_clamps_limit(self):
        from core.agent.vault_tools import build_vault_tools
        manager = MagicMock()
        materials = [{"source": f"n{i}.md", "extension": ".md", "chunk_count": 1} for i in range(300)]
        manager.get_indexed_materials.return_value = {"materials": materials}
        specs = build_vault_tools(manager, _make_ctx())
        lister = next(s for s in specs if s.schema.name == "vault.list_materials")
        # Above-max gets clamped to 200.
        payload = json.loads(lister.runner({"limit": 9999}))
        self.assertEqual(payload["returned"], 200)
        self.assertTrue(payload["truncated"])

    def test_list_materials_default_limit(self):
        from core.agent.vault_tools import build_vault_tools
        manager = MagicMock()
        materials = [{"source": f"n{i}.md", "extension": ".md", "chunk_count": 1} for i in range(150)]
        manager.get_indexed_materials.return_value = {"materials": materials}
        specs = build_vault_tools(manager, _make_ctx())
        lister = next(s for s in specs if s.schema.name == "vault.list_materials")
        payload = json.loads(lister.runner({}))
        self.assertEqual(payload["returned"], 100)
        self.assertEqual(payload["total"], 150)
        self.assertTrue(payload["truncated"])

    def test_list_materials_handles_missing_manifest(self):
        from core.agent.vault_tools import build_vault_tools
        manager = MagicMock()
        manager.get_indexed_materials.return_value = None
        specs = build_vault_tools(manager, _make_ctx())
        lister = next(s for s in specs if s.schema.name == "vault.list_materials")
        payload = json.loads(lister.runner({}))
        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["materials"], [])


# ---------------------------------------------------------------------------
# UsageBudget — accumulates LLMUsage entries
# ---------------------------------------------------------------------------

class TestUsageBudget(unittest.TestCase):
    def test_record_accumulates_tokens_and_cost(self):
        from core.agent.budget import UsageBudget
        from core.llm.types import LLMUsage
        b = UsageBudget()
        b.record(LLMUsage(input_tokens=10, output_tokens=5, cached_input_tokens=2, estimated_cost_usd=0.001))
        b.record(LLMUsage(input_tokens=20, output_tokens=7, cached_input_tokens=0, estimated_cost_usd=0.002))
        self.assertEqual(b.input_tokens, 30)
        self.assertEqual(b.output_tokens, 12)
        self.assertEqual(b.cached_input_tokens, 2)
        self.assertEqual(b.total_tokens, 42)
        self.assertAlmostEqual(b.estimated_cost_usd, 0.003)
        self.assertEqual(b.iteration_count, 2)

    def test_record_tolerates_none_fields(self):
        from core.agent.budget import UsageBudget
        from core.llm.types import LLMUsage
        # Some adapters fill defaults; make sure 0/None plumbing doesn't crash.
        b = UsageBudget()
        b.record(LLMUsage())
        self.assertEqual(b.total_tokens, 0)
        self.assertEqual(b.iteration_count, 1)

    def test_as_dict_shape(self):
        from core.agent.budget import UsageBudget
        from core.llm.types import LLMUsage
        b = UsageBudget()
        b.record(LLMUsage(input_tokens=1, output_tokens=2))
        d = b.as_dict()
        self.assertEqual(
            set(d.keys()),
            {"input_tokens", "output_tokens", "cached_input_tokens", "total_tokens",
             "estimated_cost_usd", "iteration_count"},
        )


# ---------------------------------------------------------------------------
# Agent loop — the ReAct iteration that ties everything together
# ---------------------------------------------------------------------------

def _make_tool_registry():
    """A tiny registry with one tool the loop can dispatch in tests."""
    from core.agent.tools import ToolRegistry, ToolSpec
    from core.llm.types import ToolSchema

    def _runner(args):
        return f"hit:{args.get('q', '')}"

    spec = ToolSpec(
        schema=ToolSchema(
            name="vault.search",
            description="d",
            parameters={
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        ),
        runner=_runner,
        max_output_chars=200,
    )
    return ToolRegistry([spec])


def _llm_response(*, finish_reason=None, text="", tool_calls=None, usage=None):
    """Build an LLMResponse stub for mocking resolve_chat_provider."""
    from core.llm.types import FinishReason, LLMResponse, LLMUsage
    return LLMResponse(
        text=text,
        finish_reason=finish_reason or FinishReason.STOP,
        tool_calls=tool_calls or [],
        usage=usage or LLMUsage(input_tokens=1, output_tokens=1),
    )


def _tool_call(*, name="vault.search", call_id="call_1", args=None, raw=None):
    from core.llm.types import ToolCall
    args = args if args is not None else {"q": "x"}
    return ToolCall(
        id=call_id, name=name,
        arguments=args, raw_arguments=raw or json.dumps(args),
    )


class TestAgentLoop(unittest.TestCase):
    def _run(self, responses, **overrides):
        """Helper: run run_agent_loop against a scripted sequence of
        LLM responses, collect every emitted event, and return them."""
        from core.agent.loop import run_agent_loop
        from unittest.mock import patch

        if not isinstance(responses, list):
            responses = [responses]
        # resolve_chat_provider returns (response, used_provider)
        side_effects = [(r, "ollama") for r in responses]
        collected = []

        kwargs = dict(
            user_message="hi",
            provider_name="ollama",
            model="llama3.1",
            user_system_prompt="",
            tools=_make_tool_registry(),
            cfg={"online_timeout_s": 60, "online_max_tokens": 4096},
            on_event=collected.append,
        )
        kwargs.update(overrides)

        with patch("core.agent.loop.resolve_chat_provider", side_effect=side_effects):
            budget = run_agent_loop(**kwargs)
        return collected, budget

    def _events_of(self, events, cls):
        return [e for e in events if isinstance(e, cls)]

    # ---- happy paths ---------------------------------------------------

    def test_loop_terminates_on_final_answer_iteration_1(self):
        from core.agent.protocol import DoneEvent, IterationEvent, TokenEvent
        from core.llm.types import FinishReason

        events, _ = self._run(
            _llm_response(finish_reason=FinishReason.STOP, text="the answer"),
        )
        self.assertEqual(len(self._events_of(events, IterationEvent)), 1)
        token_events = self._events_of(events, TokenEvent)
        self.assertEqual(len(token_events), 1)
        self.assertEqual(token_events[0].text, "the answer")
        self.assertEqual(len(self._events_of(events, DoneEvent)), 1)

    def test_loop_dispatches_tool_then_returns_final_answer(self):
        from core.agent.protocol import (
            DoneEvent, ToolCallEvent, ToolResultEvent, TokenEvent,
        )
        from core.llm.types import FinishReason

        responses = [
            _llm_response(
                finish_reason=FinishReason.TOOL_USE,
                tool_calls=[_tool_call(args={"q": "alpha"})],
            ),
            _llm_response(
                finish_reason=FinishReason.STOP,
                text="based on results: ...",
            ),
        ]
        events, _ = self._run(responses)
        self.assertEqual(len(self._events_of(events, ToolCallEvent)), 1)
        results = self._events_of(events, ToolResultEvent)
        self.assertEqual(len(results), 1)
        # Tool result content is the wrapped output containing hit:alpha.
        self.assertIn("hit:alpha", results[0].result.content)
        self.assertIn("untrusted", results[0].result.content.lower())
        self.assertEqual(len(self._events_of(events, TokenEvent)), 1)
        self.assertEqual(len(self._events_of(events, DoneEvent)), 1)

    def test_loop_emits_thought_when_response_has_preamble_text(self):
        from core.agent.protocol import ThoughtEvent
        from core.llm.types import FinishReason

        responses = [
            _llm_response(
                finish_reason=FinishReason.TOOL_USE,
                text="I will search the vault.",
                tool_calls=[_tool_call()],
            ),
            _llm_response(finish_reason=FinishReason.STOP, text="done"),
        ]
        events, _ = self._run(responses)
        thoughts = self._events_of(events, ThoughtEvent)
        self.assertEqual(len(thoughts), 1)
        self.assertEqual(thoughts[0].text, "I will search the vault.")

    # ---- finish-reason edge cases -------------------------------------

    def test_length_finish_reason_flags_truncation(self):
        from core.agent.protocol import DoneEvent, InfoEvent, TokenEvent
        from core.llm.types import FinishReason

        events, _ = self._run(
            _llm_response(finish_reason=FinishReason.LENGTH, text="partial answer"),
        )
        tokens = self._events_of(events, TokenEvent)
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].text, "partial answer")
        infos = self._events_of(events, InfoEvent)
        self.assertTrue(
            any("cut off" in i.text.lower() or "max-token" in i.text.lower() for i in infos),
            "LENGTH finish should emit a truncation info event",
        )
        self.assertEqual(len(self._events_of(events, DoneEvent)), 1)

    def test_content_filter_ends_turn_without_fallback(self):
        from core.agent.protocol import DoneEvent, InfoEvent, TokenEvent
        from core.llm.types import FinishReason

        # A content-filter refusal must end the turn cleanly — it is not a
        # malformed tool call, so no RAG fallback / capability nag.
        called = {"fallback": False}

        def _fb():
            called["fallback"] = True
            return iter(["should not run"])

        events, _ = self._run(
            _llm_response(finish_reason=FinishReason.CONTENT_FILTER, text=""),
            rag_fallback_fn=_fb,
        )
        self.assertFalse(called["fallback"], "content filter must not trigger RAG fallback")
        infos = self._events_of(events, InfoEvent)
        self.assertTrue(any("content filter" in i.text.lower() for i in infos))
        self.assertEqual(len(self._events_of(events, DoneEvent)), 1)
        self.assertEqual(self._events_of(events, TokenEvent), [])

    # ---- malformed-call fallback --------------------------------------

    def test_two_consecutive_malformed_iterations_trigger_fallback(self):
        from core.agent.protocol import InfoEvent, TokenEvent
        from core.llm.types import FinishReason

        # TOOL_USE finish reason but empty tool_calls — adapter parser
        # rejected the JSON. Two in a row → fallback.
        malformed = _llm_response(finish_reason=FinishReason.TOOL_USE, tool_calls=[])
        events, _ = self._run(
            [malformed, malformed],
            rag_fallback_fn=lambda: iter(["fallback ", "answer"]),
        )
        infos = self._events_of(events, InfoEvent)
        # Expect at least the "falling back" info event.
        self.assertTrue(any("falling back" in i.text.lower() for i in infos))
        # And the fallback tokens make it through.
        tokens = self._events_of(events, TokenEvent)
        self.assertEqual("".join(t.text for t in tokens), "fallback answer")

    def test_fallback_without_callback_emits_info_then_done(self):
        from core.agent.protocol import DoneEvent, InfoEvent, TokenEvent
        from core.llm.types import FinishReason

        malformed = _llm_response(finish_reason=FinishReason.TOOL_USE, tool_calls=[])
        events, _ = self._run(
            [malformed, malformed],
            rag_fallback_fn=None,
        )
        # Info + done; no tokens.
        self.assertTrue(self._events_of(events, InfoEvent))
        self.assertEqual(len(self._events_of(events, TokenEvent)), 0)
        self.assertEqual(len(self._events_of(events, DoneEvent)), 1)

    def test_successful_call_resets_malformed_streak(self):
        """A malformed iteration followed by a good tool call followed
        by another malformed iteration must NOT trigger fallback —
        success resets the counter."""
        from core.agent.protocol import InfoEvent, TokenEvent
        from core.llm.types import FinishReason

        malformed = _llm_response(finish_reason=FinishReason.TOOL_USE, tool_calls=[])
        tool_call = _llm_response(
            finish_reason=FinishReason.TOOL_USE,
            tool_calls=[_tool_call()],
        )
        final = _llm_response(finish_reason=FinishReason.STOP, text="ok")
        events, _ = self._run(
            [malformed, tool_call, final],
            rag_fallback_fn=lambda: iter(["should not appear"]),
        )
        # No "falling back" info event.
        infos = self._events_of(events, InfoEvent)
        self.assertFalse(any("falling back" in i.text.lower() for i in infos))
        # Final answer wins.
        tokens = self._events_of(events, TokenEvent)
        self.assertEqual([t.text for t in tokens], ["ok"])

    def test_empty_stop_response_counts_as_malformed(self):
        """A STOP finish with empty text twice in a row must fall
        back — otherwise the loop would terminate without ever
        producing an answer or surfacing the issue to the user."""
        from core.agent.protocol import InfoEvent
        from core.llm.types import FinishReason

        empty_stop = _llm_response(finish_reason=FinishReason.STOP, text="")
        events, _ = self._run(
            [empty_stop, empty_stop],
            rag_fallback_fn=lambda: iter([]),
        )
        infos = self._events_of(events, InfoEvent)
        self.assertTrue(any("falling back" in i.text.lower() for i in infos))

    # ---- tool error handling ------------------------------------------

    def test_tool_exception_becomes_observation_not_loop_failure(self):
        """A tool runner that raises must be caught and surfaced as
        a ToolResult(is_error=True) — the loop continues so the model
        can react to the error."""
        from core.agent.protocol import ToolResultEvent, TokenEvent
        from core.agent.tools import ToolRegistry, ToolSpec
        from core.llm.types import FinishReason, ToolSchema

        def _broken_runner(args):
            raise IOError("disk on fire")

        broken_spec = ToolSpec(
            schema=ToolSchema(
                name="vault.search", description="d",
                parameters={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
            ),
            runner=_broken_runner,
        )
        registry = ToolRegistry([broken_spec])
        responses = [
            _llm_response(
                finish_reason=FinishReason.TOOL_USE,
                tool_calls=[_tool_call()],
            ),
            _llm_response(finish_reason=FinishReason.STOP, text="model recovered"),
        ]
        events, _ = self._run(responses, tools=registry)
        results = self._events_of(events, ToolResultEvent)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].result.is_error)
        self.assertIn("disk on fire", results[0].result.content)
        # Loop continued and produced the final answer.
        self.assertEqual(len(self._events_of(events, TokenEvent)), 1)

    def test_invalid_tool_args_become_error_observation(self):
        from core.agent.protocol import ToolResultEvent
        from core.llm.types import FinishReason

        # tool_call missing required arg `q`.
        bad_call = _tool_call(args={})
        responses = [
            _llm_response(finish_reason=FinishReason.TOOL_USE, tool_calls=[bad_call]),
            _llm_response(finish_reason=FinishReason.STOP, text="ok"),
        ]
        events, _ = self._run(responses)
        results = self._events_of(events, ToolResultEvent)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].result.is_error)
        self.assertIn("argument error", results[0].result.content.lower())

    # ---- budget and limits --------------------------------------------

    def test_loop_caps_at_max_iterations(self):
        """If the model keeps asking for tools, the loop must stop
        after max_iterations and emit a final InfoEvent + DoneEvent."""
        from core.agent.protocol import (
            DoneEvent, InfoEvent, IterationEvent, TokenEvent,
        )
        from core.llm.types import FinishReason

        tool_response = _llm_response(
            finish_reason=FinishReason.TOOL_USE,
            tool_calls=[_tool_call()],
        )
        events, _ = self._run([tool_response] * 3, max_iterations=3)
        self.assertEqual(len(self._events_of(events, IterationEvent)), 3)
        # Iteration-limit info, then DoneEvent. No TokenEvent.
        self.assertEqual(len(self._events_of(events, TokenEvent)), 0)
        infos = self._events_of(events, InfoEvent)
        self.assertTrue(any("iteration limit" in i.text.lower() for i in infos))
        self.assertEqual(len(self._events_of(events, DoneEvent)), 1)

    def test_usage_accumulates_across_iterations(self):
        from core.llm.types import FinishReason, LLMUsage

        responses = [
            _llm_response(
                finish_reason=FinishReason.TOOL_USE,
                tool_calls=[_tool_call()],
                usage=LLMUsage(input_tokens=10, output_tokens=5),
            ),
            _llm_response(
                finish_reason=FinishReason.STOP, text="done",
                usage=LLMUsage(input_tokens=20, output_tokens=7),
            ),
        ]
        _, budget = self._run(responses)
        self.assertEqual(budget.input_tokens, 30)
        self.assertEqual(budget.output_tokens, 12)
        self.assertEqual(budget.iteration_count, 2)

    def test_deadline_emits_error_and_returns(self):
        from core.agent.protocol import ErrorEvent, IterationEvent
        from core.llm.types import FinishReason

        events, _ = self._run(
            [_llm_response(finish_reason=FinishReason.STOP, text="never reached")],
            deadline_monotonic_s=0.0,  # already in the past
        )
        # Loop emitted IterationEvent then ErrorEvent, never reached the LLM.
        self.assertTrue(self._events_of(events, IterationEvent))
        errors = self._events_of(events, ErrorEvent)
        self.assertEqual(len(errors), 1)
        self.assertIn("timed out", errors[0].text.lower())

    def test_llm_error_surfaces_as_error_event(self):
        from core.agent.loop import run_agent_loop
        from core.agent.protocol import ErrorEvent
        from core.llm.types import ErrorCategory, LLMError
        from unittest.mock import patch

        err = LLMError(
            category=ErrorCategory.AUTH,
            message="API key invalid",
            provider="openai",
        )
        collected = []

        with patch("core.agent.loop.resolve_chat_provider", side_effect=err):
            run_agent_loop(
                user_message="hi",
                provider_name="openai",
                model="gpt-4o-mini",
                user_system_prompt="",
                tools=_make_tool_registry(),
                cfg={},
                on_event=collected.append,
            )
        errors = [e for e in collected if isinstance(e, ErrorEvent)]
        self.assertEqual(len(errors), 1)
        self.assertIn("API key invalid", errors[0].text)

    def test_llm_error_redacts_secrets(self):
        """Provider errors that accidentally embed an API key must be
        scrubbed before the loop emits them as an ErrorEvent."""
        from core.agent.loop import run_agent_loop
        from core.agent.protocol import ErrorEvent
        from core.llm.types import ErrorCategory, LLMError
        from unittest.mock import patch

        err = LLMError(
            category=ErrorCategory.AUTH,
            message="Bad request to sk-abcdefghij1234567890abcdefghij",
            provider="openai",
        )
        collected = []
        with patch("core.agent.loop.resolve_chat_provider", side_effect=err):
            run_agent_loop(
                user_message="hi",
                provider_name="openai",
                model="gpt-4o-mini",
                user_system_prompt="",
                tools=_make_tool_registry(),
                cfg={},
                on_event=collected.append,
            )
        errors = [e for e in collected if isinstance(e, ErrorEvent)]
        self.assertNotIn("sk-abcdef", errors[0].text)
        self.assertIn("<redacted>", errors[0].text)

    # ---- capability warning state machine -----------------------------

    def test_capability_warning_emitted_after_two_fallbacks(self):
        from core.agent.loop import AgentCapabilityState
        from core.agent.protocol import InfoEvent
        from core.llm.types import FinishReason

        state = AgentCapabilityState()
        malformed = _llm_response(finish_reason=FinishReason.TOOL_USE, tool_calls=[])
        # First turn — falls back.
        events1, _ = self._run(
            [malformed, malformed],
            rag_fallback_fn=lambda: iter([]),
            capability_state=state,
        )
        infos1 = self._events_of(events1, InfoEvent)
        self.assertEqual(state.consecutive_fallbacks, 1)
        self.assertFalse(state.warning_emitted)
        self.assertFalse(any("tool-capable model" in i.text.lower() for i in infos1))

        # Second turn — falls back again. Capability warning fires.
        events2, _ = self._run(
            [malformed, malformed],
            rag_fallback_fn=lambda: iter([]),
            capability_state=state,
        )
        infos2 = self._events_of(events2, InfoEvent)
        self.assertEqual(state.consecutive_fallbacks, 2)
        self.assertTrue(state.warning_emitted)
        self.assertTrue(any("tool-capable model" in i.text.lower() for i in infos2))

    def test_capability_warning_is_one_shot_per_session(self):
        from core.agent.loop import AgentCapabilityState
        from core.agent.protocol import InfoEvent
        from core.llm.types import FinishReason

        state = AgentCapabilityState(consecutive_fallbacks=2, warning_emitted=True)
        malformed = _llm_response(finish_reason=FinishReason.TOOL_USE, tool_calls=[])
        events, _ = self._run(
            [malformed, malformed],
            rag_fallback_fn=lambda: iter([]),
            capability_state=state,
        )
        infos = self._events_of(events, InfoEvent)
        # The fallback info still fires; the capability warning does not repeat.
        self.assertFalse(any("tool-capable model" in i.text.lower() for i in infos))

    def test_successful_final_answer_resets_consecutive_fallbacks(self):
        from core.agent.loop import AgentCapabilityState
        from core.llm.types import FinishReason

        state = AgentCapabilityState(consecutive_fallbacks=1)
        events, _ = self._run(
            _llm_response(finish_reason=FinishReason.STOP, text="ok"),
            capability_state=state,
        )
        self.assertEqual(state.consecutive_fallbacks, 0)


if __name__ == "__main__":
    unittest.main()
