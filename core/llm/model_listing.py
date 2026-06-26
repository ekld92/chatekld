"""Live model-list discovery with a curated fallback.

Each online adapter's ``list_models()`` merges its hardcoded ``CURATED_MODELS``
with a best-effort live fetch from the provider's models endpoint, so newly
released models (e.g. a just-shipped Claude tier) appear in the picker without a
code change and retired ids simply never get appended. Per design
feedback: a ``get/models`` call with a hardcoded fallback handles model churn
better than a static list alone.

Design guarantees:

* **Never raises.** Any failure in the live fetch degrades to curated-only — the
  dropdown can never end up empty.
* **No network without a key.** The adapter's ``_fetch_live_models`` calls
  ``_api_key()`` first, which raises ``AUTH`` when unset; that propagates to the
  ``except`` here and yields curated-only. So offline / no-key callers — and the
  hermetic test suite — see exactly the pre-existing behaviour.
* **Short-TTL cached.** One network round-trip per provider per ``_LIVE_TTL_S``,
  not one per ``/api/models`` poll. The cache is keyed by provider+base_url so a
  self-hosted/compat endpoint override doesn't share another's entry.
* **Curated stays authoritative for pricing + defaults.** The merge appends only
  *new* live ids after the curated list; curated order (the opinionated default
  ordering) is preserved, and ``usage.PRICING_TABLE`` / default-model selection
  keep keying off the curated set.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

# One live fetch per provider+endpoint per 5 minutes. /api/models is fetched on
# provider switch, not polled, so this is generous headroom.
_LIVE_TTL_S = 300.0
# A *failed* fetch (network blip, or — common — the first call landing before
# the user has set their key) is cached for only this long, so discovery
# recovers within seconds of the key/network coming up rather than being blocked
# for the full _LIVE_TTL_S. A successful-but-empty result is still cached for the
# full TTL (it is a real answer, not a failure).
_FAILURE_TTL_S = 20.0

# key -> (expiry_monotonic, live_models). Expiry-based (not insert-timestamp)
# so the per-result TTL — full vs failure — is encoded directly in the entry.
_cache: dict[str, tuple[float, list[str]]] = {}
_cache_lock = threading.Lock()


def merged_models(
    provider: str,
    curated: list[str],
    fetch_live: Callable[[], list[str]],
    *,
    cache_key: str | None = None,
    ttl_s: float = _LIVE_TTL_S,
    failure_ttl_s: float = _FAILURE_TTL_S,
) -> list[str]:
    """Return ``curated ∪ live`` — curated first, deduped, order-preserving.

    *fetch_live* is invoked at most once per (cache lifetime) per *cache_key* and
    may raise or return ``[]`` freely; either degrades to curated-only. It is
    expected to return ids already filtered to chat-capable models. A raised
    fetch is re-tried after *failure_ttl_s*; a successful one (even empty) holds
    for *ttl_s*.

    Concurrency note: the fetch runs with the lock released, so a burst of
    concurrent first-calls for the same key can each issue one live fetch
    (idempotent — last write wins). That is intentional: holding the lock across
    a network round-trip would serialise every caller behind it. The duplicate
    fetches are harmless and self-resolve once one populates the cache.
    """
    key = cache_key or provider
    now = time.monotonic()

    with _cache_lock:
        hit = _cache.get(key)
        fresh = hit is not None and now < hit[0]
        live = hit[1] if fresh else []

    if not fresh:
        ok = True
        try:
            fetched = fetch_live() or []
            live = [m for m in fetched if isinstance(m, str) and m]
        except Exception:
            logger.debug(
                "live model fetch for %s failed; using curated only",
                provider, exc_info=True,
            )
            live = []
            ok = False
        expiry = now + (ttl_s if ok else failure_ttl_s)
        with _cache_lock:
            _cache[key] = (expiry, live)

    merged = list(curated)
    seen = set(merged)
    for m in live:
        if m not in seen:
            merged.append(m)
            seen.add(m)
    return merged


def clear_cache() -> None:
    """Drop the live-model cache (test hook; also safe to call after a config
    change that should force a fresh discovery)."""
    with _cache_lock:
        _cache.clear()
