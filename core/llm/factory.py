"""Factory + thin policy-aware wrapper for LLM providers."""
from __future__ import annotations

import dataclasses
import logging
from typing import Callable, Iterator, Optional

from core.llm.base import LLMProvider, StreamingResponse, coerce_error
from core.llm.policy import FallbackPolicy
from core.llm.types import ErrorCategory, LLMError, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

ONLINE_PROVIDER_NAMES = frozenset({"openai", "anthropic", "google"})
LOCAL_PROVIDER_NAMES = frozenset({"ollama", "lm_studio"})
ALL_PROVIDER_NAMES = ONLINE_PROVIDER_NAMES | LOCAL_PROVIDER_NAMES


def get_llm_provider(name: str, cfg: Optional[dict] = None) -> LLMProvider:
    """Return a fresh :class:`LLMProvider` for *name*.

    Adapters are constructed lazily to keep optional dependencies (the
    real ``openai`` SDK, ``httpx``) out of the import graph until a user
    actually selects an online provider.

    When *cfg* is supplied, online adapters honour ``online_timeout_s``
    and ``online_max_retries`` from it; otherwise the adapter defaults
    (60 s, 3 retries) apply. Local adapters ignore *cfg* because their
    HTTP transport is owned by the local SDK.
    """
    key = (name or "").strip().lower()
    online_kwargs: dict = {}
    if cfg:
        if "online_timeout_s" in cfg:
            try:
                online_kwargs["timeout_s"] = float(cfg["online_timeout_s"])
            except (TypeError, ValueError):
                pass
        if "online_max_retries" in cfg:
            try:
                # Clamp to the same [0, 10] range the POST /api/config validator
                # enforces, so a hand-edited config.json (which bypasses that
                # validator) cannot make the retry loop attempt hundreds of
                # round-trips and outlast the SSE consumer's stall guard.
                online_kwargs["max_retries"] = max(0, min(int(cfg["online_max_retries"]), 10))
            except (TypeError, ValueError):
                pass
    if key == "openai":
        from core.llm.adapters.openai import OpenAIProvider
        return OpenAIProvider(**online_kwargs)
    if key == "anthropic":
        from core.llm.adapters.anthropic import AnthropicProvider
        return AnthropicProvider(**online_kwargs)
    if key == "google":
        from core.llm.adapters.google import GoogleProvider
        return GoogleProvider(**online_kwargs)
    if key in LOCAL_PROVIDER_NAMES:
        from core.llm.adapters.local import LocalLLMProvider
        return LocalLLMProvider(key)
    raise LLMError(
        category=ErrorCategory.INVALID_REQUEST,
        message=f"unknown provider: {name!r}",
        provider=key or "?",
        retryable=False,
    )


def _capture_request(request: LLMRequest, provider_name: str) -> None:
    """Record a request's effective system prompt for the Prompt Hub.

    Reads the layered view straight off the request — ``system_prompt`` is the
    exact system field, ``retrieved_context_chunks`` gives the grounding size,
    and the last user turn is the query — so no per-caller enrichment is needed.
    Only tagged requests (``request.workflow`` set) are recorded; the capture
    module itself never raises, so this is a cheap, safe passthrough.
    """
    if not getattr(request, "workflow", ""):
        return
    from core import prompt_capture

    # The last user-role message is the "query" for RAG/plain-chat requests; an
    # empty messages list (some prompt-only requests) just yields "".
    last_user = ""
    for msg in reversed(request.messages or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = str(msg.get("content", ""))
            break
    prompt_capture.record(
        request.workflow,
        request.system_prompt,
        provider=provider_name,
        model=request.model,
        context_chunks=len(request.retrieved_context_chunks or []),
        query=last_user,
    )


def resolve_chat_provider(
    policy: FallbackPolicy,
    *,
    request: LLMRequest,
    stream: bool,
    on_fallback=None,
    cfg: Optional[dict] = None,
) -> tuple[LLMResponse | StreamingResponse, str]:
    """Execute *request* on the policy's primary provider with fallback.

    Returns ``(response, provider_name)`` so the caller knows which
    provider was actually used (useful for usage tracking and SSE
    "info" events). ``on_fallback(err, fallback_name)`` is called once
    before the retry on the fallback provider so the route handler can
    surface a one-line "info" message to the user.

    The function never re-raises a transient error from the primary
    when a fallback is configured — that's the whole point of the
    fallback. Non-transient errors (auth, invalid_request) always
    surface to the caller regardless of policy.
    """
    primary = get_llm_provider(policy.primary, cfg=cfg)
    # Prompt Hub capture: record the effective system prompt BEFORE dispatch so
    # the panel reflects what we sent even if the call then errors. Tagged by
    # request.workflow (the agent loop sets it); no-op for untagged requests.
    _capture_request(request, policy.primary)
    try:
        response = primary.stream(request) if stream else primary.generate(request)
        return response, policy.primary
    except LLMError as err:
        if not policy.should_fall_back(err):
            raise
        if on_fallback is not None:
            try:
                on_fallback(err, policy.fallback)
            except Exception:
                logger.debug("on_fallback callback failed", exc_info=True)
        logger.warning(
            "primary provider %s failed (%s); falling back to %s",
            policy.primary,
            err.category.value,
            policy.fallback,
        )
        assert policy.fallback is not None
        fallback = get_llm_provider(policy.fallback, cfg=cfg)
        response = fallback.stream(request) if stream else fallback.generate(request)
        return response, policy.fallback
    except Exception as exc:
        raise coerce_error(exc, provider=policy.primary, model=request.model)


def stream_with_fallback(
    provider_name: str,
    request: LLMRequest,
    policy: FallbackPolicy,
    *,
    cfg: Optional[dict] = None,
    on_fallback: Optional[Callable[[str, str], None]] = None,
    log_context: str = "fallback",
) -> Iterator[str]:
    """Stream tokens through the primary provider, falling back before token 1.

    Yields tokens from the primary provider; on an ``LLMError`` it consults
    the fallback ``policy`` and retries on the fallback provider **only if no
    token has yet streamed** (``yielded_any``). Once >=1 token has streamed,
    re-streaming the whole answer would duplicate the output, so the error
    is re-raised. The fallback request mirrors the primary one but re-resolves
    the model name for the fallback provider.
    """
    from core.config import resolve_chat_model
    from core.llm.redact import redact

    primary = get_llm_provider(provider_name, cfg=cfg)
    # Prompt Hub capture (see resolve_chat_provider): record what the primary
    # provider is about to receive. This one seam covers plain chat, online
    # single-paper summarise, online vault RAG, the deck review/compile-fix
    # prompts, and the refactor review/edit prompts — every stream_chat_messages
    # and _stream_online caller — via their request.workflow tag.
    _capture_request(request, provider_name)
    yielded_any = False
    try:
        for token in primary.stream(request).response_gen:
            yielded_any = True
            yield token
        return
    except LLMError as err:
        if yielded_any:
            raise
        if not policy.should_fall_back(err) or policy.fallback is None:
            raise
            
        if on_fallback is not None:
            try:
                on_fallback(err.category.value, policy.fallback)
            except Exception:
                logger.debug("on_fallback callback failed", exc_info=True)
                
        logger.warning(
            "%s %s -> %s: %s",
            log_context,
            provider_name,
            policy.fallback,
            redact(err.message),
        )

    fallback = get_llm_provider(policy.fallback, cfg=cfg)
    fallback_model = resolve_chat_model(cfg or {}, policy.fallback)
    fallback_request = dataclasses.replace(request, model=fallback_model)
    
    yield from fallback.stream(fallback_request).response_gen



def iter_with_fallback(
    response_or_stream,
) -> Iterator[str]:
    """Helper for SSE handlers: iterate tokens from either response type."""
    if isinstance(response_or_stream, StreamingResponse):
        yield from response_or_stream.response_gen
        return
    if isinstance(response_or_stream, LLMResponse):
        if response_or_stream.text:
            yield response_or_stream.text
        return
    raise TypeError(f"unexpected response type: {type(response_or_stream)!r}")


__all__ = [
    "ONLINE_PROVIDER_NAMES",
    "LOCAL_PROVIDER_NAMES",
    "ALL_PROVIDER_NAMES",
    "get_llm_provider",
    "resolve_chat_provider",
    "stream_with_fallback",
    "iter_with_fallback",
]


def is_online(name: Optional[str]) -> bool:
    """True if *name* is one of the online (chat-only) providers."""
    return (name or "").strip().lower() in ONLINE_PROVIDER_NAMES


def is_local(name: Optional[str]) -> bool:
    """True if *name* is one of the local (embedding-capable) providers."""
    return (name or "").strip().lower() in LOCAL_PROVIDER_NAMES
