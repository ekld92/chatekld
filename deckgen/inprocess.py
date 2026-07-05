"""In-process ChatRunner: drive the vault agent loop without HTTP.

The CLI talks to a *running* app over HTTP (:class:`~deckgen.client.ChatEKLDClient`).
When deckgen runs **inside** the app (the Deck Generator window), looping back out
over HTTP to ourselves would be wasteful and fragile, so this adapter exposes the
same ``.chat(...) -> ChatResult`` contract that ``outline.py`` / ``sections.py``
depend on, implemented by calling :func:`core.agent.loop.run_agent_loop` directly
against ``rag.vault.obsidian_manager`` — mirroring
``api/routes/vault.py::_run_agent``.

This is the one deckgen module that intentionally imports the app; the
orchestration core stays decoupled and duck-types this runner as a "client".
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from .result import ChatResult

# App imports — this adapter is app-coupled by design.
from core.config import load_config, resolve_chat_model
from core.constants import DEFAULT_EMBED
from core.llm.redact import redact
from core.agent import (
    AgentCapabilityState,
    ErrorEvent,
    InfoEvent,
    IterationEvent,
    ThoughtEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    ToolRegistry,
    VaultToolContext,
    build_vault_tools,
    run_agent_loop,
)
from rag.vault import obsidian_manager

# Per-turn wall-clock cap, matching the legacy chat path (_CHAT_TOKEN_TIMEOUT_S).
_TURN_TIMEOUT_S = 300


def _event_to_dict(event) -> Optional[dict]:
    """Convert an :class:`~core.agent.protocol.AgentEvent` to the SSE dict shape.

    Same mapping as ``api/routes/vault.py::_agent_event_to_queue_item`` so the
    deckgen ``on_event`` callback (and the deck SSE route) see one event shape.
    """
    if isinstance(event, IterationEvent):
        return {"iteration": event.index}
    if isinstance(event, ThoughtEvent):
        return {"thought": event.text}
    if isinstance(event, ToolCallEvent):
        return {"tool_call": {
            "id": event.call.id,
            "name": event.call.name,
            "arguments": event.call.arguments,
        }}
    if isinstance(event, ToolResultEvent):
        return {"tool_result": {
            "tool_call_id": event.result.tool_call_id,
            "content": event.result.content,
            "is_error": event.result.is_error,
            "truncated": event.truncated,
        }}
    if isinstance(event, TokenEvent):
        return {"token": event.text}
    if isinstance(event, InfoEvent):
        return {"info": event.text}
    if isinstance(event, ErrorEvent):
        return {"error": event.text}
    return None  # DoneEvent / unknown — nothing to surface


class InProcessChatRunner:
    """A ``ChatEKLDClient``-compatible runner that uses the in-process agent loop."""

    def __init__(
        self,
        *,
        cfg: Optional[dict] = None,
        capability_state: Optional[AgentCapabilityState] = None,
        cancel_event=None,
        turn_timeout_s: float = _TURN_TIMEOUT_S,
        max_tokens: Optional[int] = None,
        workflow: str = "vault_agent",
    ) -> None:
        self._cfg = cfg if cfg is not None else load_config()
        self._capability_state = capability_state or AgentCapabilityState()
        self._cancel_event = cancel_event
        self._turn_timeout_s = turn_timeout_s
        # Prompt Hub workflow id threaded into run_agent_loop so the deck route's
        # agent turns are attributed to "deck_generate" / "deck_augment" in the
        # panel rather than the default "vault_agent".
        self._workflow = workflow
        # Per-turn output cap. The agent loop reads its token budget from the
        # ``online_max_tokens`` cfg key (local included), so an explicit cap is
        # injected there in ``chat`` (alongside the temperature override). ``None``
        # leaves the configured ``online_max_tokens``. Used by the deck route to
        # bound section length without touching any other path's config.
        self._max_tokens = max_tokens

    # -- preflight parity with ChatEKLDClient -------------------------------

    def status(self) -> dict:
        """Vault index status, read straight off the singleton (no HTTP).

        Same shape ``ChatEKLDClient.status`` returns, so the orchestration core's
        preflight is identical whether it runs in-process or over the API.
        """
        return obsidian_manager.get_status_payload()

    def materials(self) -> dict:
        """Indexed-materials manifest, read straight off the singleton (no HTTP)."""
        return obsidian_manager.get_indexed_materials()

    # -- the one method outline.py / sections.py rely on --------------------

    def chat(
        self,
        message: str,
        *,
        system_prompt: str = "",
        provider: str = "ollama",
        model: str = "",
        embed: str = "",
        agent: bool = True,
        max_iters: int = 6,
        temperature: Optional[float] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        extra: Optional[dict] = None,
    ) -> ChatResult:
        """Run one agent turn in-process and return the accumulated result."""
        cfg = self._cfg
        provider = provider or cfg.get("provider", "ollama")
        model = model or resolve_chat_model(cfg, provider)
        embed = embed or cfg.get("embed", DEFAULT_EMBED)

        sim_cutoff = _as_float(cfg.get("vault_similarity_cutoff"), 0.25)
        hybrid = _as_bool(cfg.get("vault_hybrid_enabled"), True)
        rerank = _as_bool(cfg.get("vault_reranker_enabled"), True)
        rerank_model = cfg.get("vault_reranker_model") or ""
        # The agent loop reads its sampling temperature from the
        # ``vault_chat_temperature`` cfg key (see core/agent/loop.py), NOT a
        # generic ``temperature`` key, and its output cap from ``online_max_tokens``
        # — so per-turn overrides must land on those exact keys or they are
        # silently ignored. Copy cfg once so we don't mutate the caller's dict (the
        # runner is reused across outline + every section).
        _overrides = {}
        if temperature is not None:
            _overrides["vault_chat_temperature"] = temperature
        if self._max_tokens is not None:
            _overrides["online_max_tokens"] = self._max_tokens
        if _overrides:
            cfg = {**cfg, **_overrides}

        result = ChatResult()

        def _on_agent_event(ev) -> None:
            item = _event_to_dict(ev)
            if item is None:
                return
            if "token" in item:
                result.text += item["token"]
            elif "info" in item:
                result.infos.append(item["info"])
            elif "error" in item:
                result.error = item["error"]
            elif "iteration" in item:
                result.iterations = max(result.iterations, int(item["iteration"]))
                result.trace.append(item)
            else:
                result.trace.append(item)
            if on_event is not None:
                try:
                    on_event(item)
                except Exception:
                    pass

        def _rag_fallback():
            response = obsidian_manager.stream_chat(
                message, model, embed,
                provider_name=provider,
                similarity_cutoff=sim_cutoff,
                hybrid_enabled=hybrid,
                reranker_enabled=rerank,
                reranker_model=rerank_model,
            )
            if hasattr(response, "response_gen"):
                yield from response.response_gen
            else:
                yield str(response)

        ctx = VaultToolContext(
            llm_name=model,
            embed_name=embed,
            provider_name=provider,
            similarity_cutoff=sim_cutoff,
            hybrid_enabled=hybrid,
            reranker_enabled=rerank,
            reranker_model=rerank_model,
        )
        tools = ToolRegistry(build_vault_tools(obsidian_manager, ctx))
        deadline = time.monotonic() + self._turn_timeout_s

        try:
            run_agent_loop(
                user_message=message,
                provider_name=provider,
                model=model,
                user_system_prompt=system_prompt,
                tools=tools,
                cfg=cfg,
                on_event=_on_agent_event,
                max_iterations=max_iters,
                deadline_monotonic_s=deadline,
                rag_fallback_fn=_rag_fallback,
                capability_state=self._capability_state,
                cancel_event=self._cancel_event,
                workflow=self._workflow,
            )
        except Exception as exc:  # pragma: no cover - defensive
            if result.error is None:
                result.error = redact(str(exc)) or exc.__class__.__name__
        return result


def _as_float(value, default: float) -> float:
    """Coerce a config value to float, falling back to *default* on bad/None input.

    Config is JSON loaded from disk and may carry a stale or hand-edited value of
    the wrong type; this keeps a malformed ``vault_similarity_cutoff`` from
    crashing the in-process runner.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default: bool) -> bool:
    """Coerce a config value to bool, accepting the usual truthy string forms.

    A real ``bool`` passes through; ``None`` yields *default*; a string is true
    only for ``1``/``true``/``yes``/``on`` (case-insensitive). Mirrors the
    defensive parsing the route layer applies to the same ``vault_*`` knobs.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)
