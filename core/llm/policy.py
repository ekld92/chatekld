"""Fallback policy: when the primary online provider fails, optionally
retry against a designated fallback (usually the local provider).

The policy is intentionally minimal — one primary, one fallback. A
deeper chain would tempt users into building "I never see errors"
configs that hide cost-relevant signals like quota exhaustion. If a
user explicitly chose an online provider, the default is fail-fast
so they see provider failures rather than silently paying for the
wrong tier.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from core.llm.types import ErrorCategory, LLMError

logger = logging.getLogger(__name__)


_CATEGORY_BY_NAME: dict[str, ErrorCategory] = {
    "timeout": ErrorCategory.TIMEOUT,
    "network": ErrorCategory.NETWORK,
    "rate_limit": ErrorCategory.RATE_LIMIT,
    "ratelimit": ErrorCategory.RATE_LIMIT,
    "server_error": ErrorCategory.SERVER_ERROR,
    "5xx": ErrorCategory.SERVER_ERROR,
    "auth": ErrorCategory.AUTH,
    "quota": ErrorCategory.QUOTA,
    "invalid_request": ErrorCategory.INVALID_REQUEST,
    "not_found": ErrorCategory.NOT_FOUND,
}

_DEFAULT_FALLBACK_ON = frozenset({
    ErrorCategory.TIMEOUT,
    ErrorCategory.NETWORK,
    ErrorCategory.RATE_LIMIT,
    ErrorCategory.SERVER_ERROR,
})


@dataclass(frozen=True)
class FallbackPolicy:
    """A resolved one-primary / one-fallback retry policy.

    ``fallback_on`` is the set of error categories that trigger the switch — it
    deliberately excludes terminal categories (AUTH, INVALID_REQUEST, QUOTA) so a
    bad key or exhausted balance surfaces immediately instead of being masked by
    the fallback. The actual before-vs-after-first-token boundary is enforced by
    the streaming callers, not here: this object only answers *whether* a given
    error is fallback-eligible.
    """

    primary: str
    fallback: Optional[str] = None
    fallback_on: frozenset[ErrorCategory] = field(default_factory=lambda: _DEFAULT_FALLBACK_ON)

    def should_fall_back(self, err: LLMError) -> bool:
        """True iff *err*'s category is in ``fallback_on`` and a distinct
        fallback provider is configured (a fallback equal to the primary is a
        no-op and treated as none)."""
        if self.fallback is None or self.fallback == self.primary:
            return False
        return err.category in self.fallback_on


def parse_policy_from_config(cfg: dict, *, primary_override: Optional[str] = None) -> FallbackPolicy:
    """Build a :class:`FallbackPolicy` from the persisted config dict.

    Falls back to the recommended defaults when keys are missing. The
    ``primary_override`` is used by request handlers that allow a body
    override of the active provider.
    """
    primary = (primary_override or cfg.get("provider") or "ollama").strip().lower()
    fallback_raw = cfg.get("fallback_provider")
    fallback = str(fallback_raw).strip().lower() if isinstance(fallback_raw, str) and fallback_raw.strip() else None
    if fallback == primary:
        fallback = None

    fallback_on_raw = cfg.get("fallback_on")
    if isinstance(fallback_on_raw, list) and fallback_on_raw:
        categories = set()
        for entry in fallback_on_raw:
            cat = _CATEGORY_BY_NAME.get(str(entry).strip().lower())
            if cat is not None:
                categories.add(cat)
        fallback_on = frozenset(categories) if categories else _DEFAULT_FALLBACK_ON
    else:
        fallback_on = _DEFAULT_FALLBACK_ON

    return FallbackPolicy(primary=primary, fallback=fallback, fallback_on=fallback_on)
