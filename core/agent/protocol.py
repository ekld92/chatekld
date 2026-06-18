"""Typed events the agent loop emits to its consumer.

The vault route subscribes to these events via the ``on_event`` callback
passed to :func:`core.agent.loop.run_agent_loop`
and translates them into the SSE wire shapes the frontend consumes:

* :class:`IterationEvent`  → ``{"iteration": N}``
* :class:`ThoughtEvent`    → ``{"thought": "..."}``
* :class:`ToolCallEvent`   → ``{"tool_call": {...}}``
* :class:`ToolResultEvent` → ``{"tool_result": {...}}``
* :class:`TokenEvent`      → ``{"token": "..."}``  (final answer)
* :class:`InfoEvent`       → ``{"info": "..."}``   (fallback notices)
* :class:`ErrorEvent`      → ``{"error": "..."}``
* :class:`DoneEvent`       → terminal sentinel

Keeping these as typed dataclasses instead of plain dicts lets the route
layer pattern-match exhaustively when wiring SSE emission.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.llm.types import ToolCall, ToolResult


@dataclass(frozen=True)
class AgentEvent:
    """Common base so consumers can type-annotate ``Callable[[AgentEvent], None]``."""


@dataclass(frozen=True)
class IterationEvent(AgentEvent):
    """Emitted at the top of every agent iteration. ``index`` is 1-based."""
    index: int


@dataclass(frozen=True)
class ThoughtEvent(AgentEvent):
    """Reasoning text the model produced alongside a tool call.

    Anthropic and Gemini frequently emit a short preamble before the
    structured tool_use block; the loop surfaces it so the UI can show
    the agent's thinking inline with the tool call.
    """
    text: str


@dataclass(frozen=True)
class ToolCallEvent(AgentEvent):
    """The agent decided to call a tool."""
    call: ToolCall


@dataclass(frozen=True)
class ToolResultEvent(AgentEvent):
    """The observation produced by running the tool.

    ``truncated`` reflects whether the tool's output was capped by the
    registry's per-tool ``max_output_chars`` (NOT whether the result
    itself reports a domain-level truncation, which lives inside
    ``result.content``).
    """
    result: ToolResult
    truncated: bool = False


@dataclass(frozen=True)
class TokenEvent(AgentEvent):
    """A chunk of the final answer text.

    The agent loop emits a single TokenEvent carrying the full final
    answer (no streaming on the last iteration). A future polish step
    could split this into a token-by-token stream if user feedback
    warrants the extra LLM round-trip.
    """
    text: str


@dataclass(frozen=True)
class InfoEvent(AgentEvent):
    """Informational message — e.g. fallback to plain RAG."""
    text: str


@dataclass(frozen=True)
class ErrorEvent(AgentEvent):
    """Terminal error — the loop will not emit further events after this."""
    text: str


@dataclass(frozen=True)
class DoneEvent(AgentEvent):
    """Terminal sentinel — the loop has completed normally."""
