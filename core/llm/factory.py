"""Factory + thin policy-aware wrapper for LLM providers."""
from __future__ import annotations

import logging
from typing import Iterator, Optional

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
    "iter_with_fallback",
]


def is_online(name: Optional[str]) -> bool:
    """True if *name* is one of the online (chat-only) providers."""
    return (name or "").strip().lower() in ONLINE_PROVIDER_NAMES


def is_local(name: Optional[str]) -> bool:
    """True if *name* is one of the local (embedding-capable) providers."""
    return (name or "").strip().lower() in LOCAL_PROVIDER_NAMES
