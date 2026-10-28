"""Tiny retry/backoff helper used by online adapters."""
from __future__ import annotations

import logging
import random
import re
import time
from typing import Callable, Mapping, Optional, TypeVar

from core.llm.types import ErrorCategory, LLMError, LLMRequest

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Ceiling on how long a provider retry-after hint can stretch a single backoff
# sleep. A TPM 429 legitimately asks for tens of seconds ("try again in
# 42.05s" — field-reported), so the hint must be allowed to exceed the
# exponential schedule's max_delay_s; but an absurd/parsing-glitch hint must
# not park a worker thread for minutes.
RETRY_AFTER_CEILING_S = 90.0

# OpenAI and Anthropic both embed the wait in the 429 body message
# ("Please try again in 5.764s." / "Try again in 7 seconds"); the header is
# the primary source but streaming/wrapped errors sometimes only keep the
# message, so both adapters fall back to this.
_RETRY_AFTER_MSG_RE = re.compile(
    r"try again in\s*([0-9]+(?:\.[0-9]+)?)\s*s", re.IGNORECASE
)


def parse_retry_after_s(
    message: str = "",
    headers: Optional[Mapping[str, str]] = None,
) -> Optional[float]:
    """Extract a provider's suggested retry wait (seconds) from a 429.

    Prefers the standard ``Retry-After`` header (both OpenAI and Anthropic
    send it as integer seconds); falls back to the "try again in Xs" phrase
    both providers embed in the 429 body message. Returns ``None`` when
    neither yields a positive finite number — callers treat that as
    "no hint" and keep the plain exponential schedule.
    """
    if headers is not None:
        try:
            raw = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:
            raw = None
        if raw:
            try:
                value = float(raw)
                if 0 < value < float("inf"):
                    return value
            except (TypeError, ValueError):
                pass  # e.g. an HTTP-date Retry-After — fall through to the message
    if message:
        m = _RETRY_AFTER_MSG_RE.search(message)
        if m:
            try:
                value = float(m.group(1))
                if 0 < value < float("inf"):
                    return value
            except ValueError:
                pass
    return None

_TRANSIENT_CATEGORIES = frozenset({
    ErrorCategory.TIMEOUT,
    ErrorCategory.NETWORK,
    ErrorCategory.RATE_LIMIT,
    ErrorCategory.SERVER_ERROR,
})


def is_transient(err: LLMError) -> bool:
    """Whether *err* is worth retrying.

    True for the inherently-transient categories (timeout/network/rate_limit/
    server_error) OR when the adapter explicitly flagged ``retryable``. Terminal
    categories (auth, invalid_request, quota) are neither, so they propagate on
    the first attempt — a bad key never costs three round-trips.
    """
    return err.category in _TRANSIENT_CATEGORIES or err.retryable


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 1.0,
    max_delay_s: float = 16.0,
    on_attempt: Callable[[int, LLMError], None] | None = None,
    deadline_monotonic_s: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> T:
    """Call *fn* and retry on transient :class:`LLMError` failures.

    The first retry waits ``base_delay_s`` with up to 25% jitter; each
    subsequent retry doubles up to ``max_delay_s``. Non-transient errors
    propagate immediately so a bad API key never costs the user three
    round-trips.

    ``deadline_monotonic_s`` (improvement plan 1.3) is a :func:`time.monotonic`
    cutoff: when set, a backoff sleep is truncated to the remaining budget and
    no further attempt starts once the deadline has (or would effectively have)
    passed — the last error is re-raised instead. The agent loop sets each
    call's ``request.timeout_s`` to its remaining wall-clock budget, and the
    online adapters derive this deadline from it, so a retried transient error
    can no longer block past the turn's deadline in a blocking ``sleep``.
    """
    attempt = 0
    delay = base_delay_s
    last_error: LLMError | None = None
    while True:
        cancelled = should_cancel is not None and should_cancel()
        expired = deadline_monotonic_s is not None and time.monotonic() >= deadline_monotonic_s
        if cancelled or expired:
            if last_error is not None:
                raise last_error
            raise LLMError(category=ErrorCategory.TIMEOUT,
                           message="Request cancelled before dispatch" if cancelled else "Request deadline expired before dispatch")
        attempt += 1
        try:
            return fn()
        except LLMError as err:
            last_error = err
            if attempt >= max_attempts or not is_transient(err):
                raise
            # Jitter (±25 %) BEFORE the cap, so max_delay_s is an honest
            # ceiling — the previous clamp-then-jitter order let the "max"
            # run 25 % over its own name (2026-07-22 audit B8). The
            # retry-after hint below may still exceed the cap by design.
            sleep_for = min(delay * (0.75 + random.random() * 0.5), max_delay_s)
            # Provider retry-after hint (429s): sleeping LESS than the hint
            # guarantees the retry burns on another 429 (the TPM window has
            # not drained), so the hint is a FLOOR over the exponential
            # schedule — deliberately allowed to exceed max_delay_s, since
            # a TPM 429 can legitimately ask for ~40s+ (field-reported),
            # but capped so a bogus hint can't park the thread for minutes.
            hint = getattr(err, "retry_after_s", None)
            if hint is not None and hint > 0:
                sleep_for = max(sleep_for, min(hint + 0.5, RETRY_AFTER_CEILING_S))
            if deadline_monotonic_s is not None:
                remaining = deadline_monotonic_s - time.monotonic()
                # Not enough budget left for a meaningful retry: surface the
                # error now rather than sleep past the caller's deadline.
                if remaining <= 0.5:
                    raise
                # A hint the deadline cannot cover means every remaining
                # attempt is a guaranteed 429 — surface now instead of
                # sleeping the budget away on futile waits.
                if hint is not None and hint > remaining:
                    raise
                sleep_for = min(sleep_for, remaining)
            if on_attempt is not None:
                try:
                    on_attempt(attempt, err)
                except Exception:
                    logger.debug("retry on_attempt callback failed", exc_info=True)
            logger.info(
                "transient %s error from %s (attempt %d/%d) — retrying in %.1fs",
                err.category.value,
                err.provider or "?",
                attempt,
                max_attempts,
                sleep_for,
            )
            # Sliced, cancel-aware sleep (2026-07-22 audit B8): a rate-limit
            # hint can legitimately floor this at tens of seconds, during
            # which the caller's Cancel previously appeared inert — the
            # between-attempt cancel check could only run after the full
            # sleep. Slices keep the wait interruptible at ~1 s granularity;
            # on cancel the CURRENT error is re-raised (nothing new started).
            if should_cancel is None:
                time.sleep(sleep_for)
            else:
                end_at = time.monotonic() + sleep_for
                while True:
                    if should_cancel():
                        raise err
                    remaining_sleep = end_at - time.monotonic()
                    if remaining_sleep <= 0:
                        break
                    time.sleep(min(1.0, remaining_sleep))
            delay *= 2


def check_request_dispatch(request: LLMRequest, started: float) -> None:
    """Guard every transport attempt, including immediate parameter-heal retries."""
    should_cancel = (request.metadata or {}).get("should_cancel")
    cancelled = callable(should_cancel) and should_cancel()
    expired = (request.timeout_s is not None and request.timeout_s > 0
               and time.monotonic() >= started + request.timeout_s)
    if cancelled or expired:
        raise LLMError(
            category=ErrorCategory.TIMEOUT,
            message="Request cancelled before dispatch" if cancelled else "Request deadline expired before dispatch",
            model=request.model,
        )
