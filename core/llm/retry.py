"""Tiny retry/backoff helper used by online adapters."""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

from core.llm.types import ErrorCategory, LLMError

logger = logging.getLogger(__name__)

T = TypeVar("T")

_TRANSIENT_CATEGORIES = frozenset({
    ErrorCategory.TIMEOUT,
    ErrorCategory.NETWORK,
    ErrorCategory.RATE_LIMIT,
    ErrorCategory.SERVER_ERROR,
})


def is_transient(err: LLMError) -> bool:
    return err.category in _TRANSIENT_CATEGORIES or err.retryable


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 1.0,
    max_delay_s: float = 16.0,
    on_attempt: Callable[[int, LLMError], None] | None = None,
) -> T:
    """Call *fn* and retry on transient :class:`LLMError` failures.

    The first retry waits ``base_delay_s`` with up to 25% jitter; each
    subsequent retry doubles up to ``max_delay_s``. Non-transient errors
    propagate immediately so a bad API key never costs the user three
    round-trips.
    """
    attempt = 0
    delay = base_delay_s
    while True:
        attempt += 1
        try:
            return fn()
        except LLMError as err:
            if attempt >= max_attempts or not is_transient(err):
                raise
            if on_attempt is not None:
                try:
                    on_attempt(attempt, err)
                except Exception:
                    logger.debug("retry on_attempt callback failed", exc_info=True)
            sleep_for = min(delay, max_delay_s)
            sleep_for = sleep_for * (0.75 + random.random() * 0.5)
            logger.info(
                "transient %s error from %s (attempt %d/%d) — retrying in %.1fs",
                err.category.value,
                err.provider or "?",
                attempt,
                max_attempts,
                sleep_for,
            )
            time.sleep(sleep_for)
            delay *= 2
