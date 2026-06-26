"""Provider-agnostic multi-turn chat streaming helper (no RAG).

The Plain Chat panel talks to the configured LLM directly: a full
conversation array in, a token stream out, no vault retrieval and no
agent loop.  ``stream_chat_messages`` is the single backend entrypoint;
it is modelled on ``rag.summarizer._stream_online`` but **unified** for
local and online providers (so it is hermetically testable with a mocked
:class:`core.llm.base.LLMProvider`) — every provider already accepts a
``messages`` array on :class:`LLMRequest`, and the local adapter flattens
it into a single prompt with role tags, so multi-turn works everywhere
with no model-layer changes.

Usage / cost tracking fires automatically inside the adapter — there is
no extra wiring here.
"""
from __future__ import annotations

import logging
from typing import Generator, Optional

from core.config import load_config, resolve_chat_model
from core.llm.factory import get_llm_provider
from core.llm.policy import parse_policy_from_config
from core.llm.redact import redact
from core.llm.types import LLMError, LLMRequest

logger = logging.getLogger(__name__)


def stream_chat_messages(
    *,
    messages: list[dict],
    system_prompt: str,
    provider_name: str,
    model: str,
    temperature: float,
    max_tokens: Optional[int] = None,
    cfg: Optional[dict] = None,
    info_cb=None,
) -> Generator[str, None, None]:
    """Stream tokens for a plain (RAG-free) multi-turn chat.

    *messages* is the full conversation array (``[{"role", "content"}]``)
    the caller wants the model to see; it is passed through verbatim on
    :class:`LLMRequest` so online adapters send native message arrays and
    the local adapter flattens it.

    Falls back to the configured ``fallback_provider`` **only before the
    first token reaches the client** — once ≥1 token has streamed,
    re-streaming through the fallback would duplicate the answer, so the
    error re-raises and the route surfaces a structured SSE error after
    the partial output (identical policy to the summariser).
    """
    # ``cfg is not None`` (not ``cfg or ...``) so an explicitly-passed empty dict
    # is honoured rather than silently re-read from disk — the route always
    # supplies a populated cfg, but tests/callers may pass {} deliberately.
    cfg = cfg if cfg is not None else load_config()
    # ``... or 60`` guards a persisted 0/"" (which would mean "no timeout"); the
    # online_timeout_s knob only applies to online adapters — local adapters bound
    # themselves via local_request_timeout_s (see core/llm/CLAUDE.md).
    timeout_s = float(cfg.get("online_timeout_s", 60) or 60)
    # Plain chat sets no per-request token cap, so fall back to online_max_tokens
    # (4096). This also caps LOCAL output (mapped to num_predict by the local
    # adapter) — a deliberate, sane default for an open-ended chat turn.
    effective_max_tokens = max_tokens or int(cfg.get("online_max_tokens", 4096) or 4096)

    policy = parse_policy_from_config(cfg, primary_override=provider_name)
    request = LLMRequest(
        model=model,
        system_prompt=system_prompt,
        messages=messages,
        temperature=temperature,
        max_tokens=effective_max_tokens,
        timeout_s=timeout_s,
    )

    def _emit(msg: str) -> None:
        if info_cb is not None:
            try:
                info_cb(msg)
            except Exception:
                logger.debug("info_cb failed", exc_info=True)

    primary = get_llm_provider(provider_name, cfg=cfg)
    yielded_any = False
    try:
        for token in primary.stream(request).response_gen:
            yielded_any = True
            yield token
        return
    except LLMError as err:
        # Only fall back *before* the first token (see docstring).
        if yielded_any:
            raise
        if not policy.should_fall_back(err) or policy.fallback is None:
            raise
        _emit(
            f"primary provider {provider_name} failed ({err.category.value}); "
            f"falling back to {policy.fallback}"
        )
        logger.warning(
            "plain-chat fallback %s -> %s: %s",
            provider_name,
            policy.fallback,
            redact(err.message),
        )

    fallback = get_llm_provider(policy.fallback, cfg=cfg)
    fallback_request = LLMRequest(
        model=resolve_chat_model(cfg, policy.fallback),
        system_prompt=system_prompt,
        messages=messages,
        temperature=temperature,
        max_tokens=effective_max_tokens,
        timeout_s=timeout_s,
    )
    yield from fallback.stream(fallback_request).response_gen
