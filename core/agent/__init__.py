"""ReAct agent layer for the vault chat.

Opt-in alternative to the single-shot RAG path in ``rag.vault.stream_chat``.
When the user toggles agent mode the route hands the request to
:func:`core.agent.loop.run_agent_loop`, which orchestrates
a multi-turn conversation: the LLM emits structured tool calls, the loop
dispatches them against the registered tools (``vault_search`` /
``vault_read_note`` / ``vault_list_materials``), and the model integrates
the observations until it produces a final answer or the loop budget runs
out.

Public surface for callers:

* :class:`core.agent.protocol.AgentEvent` and its subclasses — the typed
  event stream the loop emits.
* :class:`core.agent.tools.ToolSpec` / :class:`core.agent.tools.ToolRegistry`
  — registration + validation + invocation.
* :class:`core.agent.budget.UsageBudget` — per-turn aggregated usage.
* :func:`core.agent.vault_tools.build_vault_tools` — concrete tool list.
* :func:`core.agent.loop.run_agent_loop` — the multi-turn loop driver.
"""
from core.agent.budget import UsageBudget
from core.agent.loop import AgentCapabilityState, run_agent_loop
from core.agent.protocol import (
    AgentEvent,
    DoneEvent,
    ErrorEvent,
    InfoEvent,
    IterationEvent,
    ThoughtEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from core.agent.tools import ToolArgError, ToolRegistry, ToolSpec, wrap_untrusted
from core.agent.vault_tools import VaultToolContext, build_vault_tools

__all__ = [
    "AgentCapabilityState",
    "AgentEvent",
    "DoneEvent",
    "ErrorEvent",
    "InfoEvent",
    "IterationEvent",
    "ThoughtEvent",
    "TokenEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "ToolArgError",
    "ToolRegistry",
    "ToolSpec",
    "UsageBudget",
    "VaultToolContext",
    "build_vault_tools",
    "run_agent_loop",
    "wrap_untrusted",
]
