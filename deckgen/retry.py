"""Cancel-aware retry wrapper for a single chat turn.

A local backend (LM Studio / Ollama) fails a generation transiently — a memory
hiccup, a JIT model reload, a momentary timeout. The OpenAI SDK's own retries
are disabled on the LM Studio path (see ``core/providers/lms.py``) so a single
call stays bounded by exactly one timeout and cannot blow past the SSE
consumer's stall window; recovery is owned **here** instead, where it is
cancel-aware and surfaced to the user as ``info`` events.

Pure module: no third-party imports, no app imports — the orchestration core
stays decoupled. *client* is duck-typed (anything exposing
``.chat(...) -> ChatResult``); ``time.sleep`` is the only side effect.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

from .result import ChatResult

# Providers embed their required wait in the 429 error text ("Please try
# again in 5.764s" — OpenAI TPM limits; Anthropic phrases similarly). A
# retry that sleeps LESS than that is a guaranteed second 429, so when the
# hint is parseable it floors the linear backoff (field-reported failure
# mode: deck sections retrying at 3-4s against a 5.8s hint burned every
# attempt). Parsed from the error STRING because this module is pure — the
# structured LLMError never crosses the ChatResult boundary by design.
_RETRY_HINT_RE = re.compile(r"try again in\s*([0-9]+(?:\.[0-9]+)?)\s*s", re.IGNORECASE)

# Ceiling so a garbled hint can't park the deck worker for minutes; 90s
# comfortably covers the ~40-60s TPM-window waits seen in the field.
_RETRY_HINT_CEILING_S = 90.0


def _retry_after_hint_s(error: Optional[str]) -> Optional[float]:
    """Parse the provider's "try again in Xs" wait out of an error string."""
    if not error:
        return None
    m = _RETRY_HINT_RE.search(error)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


def chat_with_retry(
    client,
    message: str,
    *,
    max_attempts: int = 1,
    retry_backoff_s: float = 0.0,
    should_cancel: Optional[Callable[[], bool]] = None,
    label: str = "",
    on_event=None,
    **chat_kwargs,
) -> ChatResult:
    """Call ``client.chat`` up to *max_attempts* times until it succeeds.

    Success is :attr:`ChatResult.ok` (non-empty answer text, no recorded error).
    On a failed attempt with retries remaining, emit a one-line ``info`` event
    and sleep ``retry_backoff_s × attempt`` (linear backoff) before retrying,
    unless *should_cancel* reports cancellation. Returns the last
    :class:`ChatResult` — successful, or the final failure so the caller can run
    its degraded path (placeholder frame for a section, ``OutlineError`` for the
    outline).

    ``max_attempts <= 1`` reproduces the original single-shot behaviour exactly
    (no info noise, no sleep), so existing callers are unaffected by default.
    """
    attempts = max(1, int(max_attempts))
    result = ChatResult()
    for attempt in range(1, attempts + 1):
        if should_cancel is not None and should_cancel():
            return result
        result = client.chat(message, on_event=on_event, **chat_kwargs)
        if result.ok:
            return result
        if attempt < attempts:
            if should_cancel is not None and should_cancel():
                return result
            reason = result.error or "no usable content returned"
            wait_s = retry_backoff_s * attempt
            # Rate-limit hint floors the linear backoff (see _RETRY_HINT_RE):
            # +0.5s margin because the provider's figure is when the window
            # *starts* accepting again, not a guarantee.
            hint_s = _retry_after_hint_s(result.error)
            if hint_s is not None:
                wait_s = max(wait_s, min(hint_s + 0.5, _RETRY_HINT_CEILING_S))
            prefix = f"{label}: " if label else ""
            tail = f" in {wait_s:g}s" if wait_s > 0 else ""
            _emit_info(
                on_event,
                f"{prefix}attempt {attempt}/{attempts} failed ({reason}); retrying{tail}…",
            )
            if wait_s > 0:
                _sleep_cancellable(wait_s, should_cancel)
    return result


def _sleep_cancellable(seconds: float, should_cancel: Optional[Callable[[], bool]]) -> None:
    """Sleep up to *seconds*, waking early (~0.2 s slices) if cancelled."""
    if should_cancel is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or should_cancel():
            return
        time.sleep(min(0.2, remaining))


def _emit_info(on_event, text: str) -> None:
    """Push one SSE-shaped ``{"info": …}`` event, defensively."""
    if on_event is None:
        return
    try:
        on_event({"info": text})
    except Exception:
        pass
