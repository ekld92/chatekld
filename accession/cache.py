"""Persistent memo for network resolutions — ``accession_cache.json``.

``BASE_DIR/audit/accession_cache.json`` maps ``{pdf sha256: {"v", "queried_at",
"resolution"}}``. Keyed by **content hash** (the ``hash_cache.py`` memo makes
one cheap to obtain), so a cached identification survives a rename or move —
the same property that motivated ``mapping.json``'s hash anchoring.

Discipline follows ``hash_cache.json`` / ``doi_cache.json`` exactly: a
REGENERABLE cache (a lost entry costs one re-query, never data), atomic
tmp-sibling + ``os.replace`` write **without** fsync (the regenerable-cache
trade those two document), load degrades to empty on any read/parse failure,
save is logged-never-raised, and an entry-count backstop bounds the file.
Being sha-keyed there is no exists()-prune; the backstop evicts the oldest
entries by ``queried_at`` instead.

Only resolutions that actually touched the network are stored (the caller's
rule — ``candidates.resolve_queries``): the offline bib-hit fast path costs
nothing to recompute and must always reflect today's bib. Cached entries are
revalidated against the current bib index at read time for the same reason.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Entry-shape version. A bump invalidates every stored entry at lookup.
CACHE_VERSION = 1

_MAX_ENTRIES = 100_000  # backstop far above any real library


def cache_path() -> Path:
    """``BASE_DIR/audit/accession_cache.json`` (beside the audit's memos).

    The deferred import mirrors ``audit/config.py::_audit_dir`` so importing
    this module never drags in app constants (test harnesses stub them).
    """
    from core.constants import BASE_DIR

    audit_dir = os.path.join(BASE_DIR, "audit")
    os.makedirs(audit_dir, mode=0o700, exist_ok=True)
    return Path(audit_dir) / "accession_cache.json"


def load_cache() -> dict[str, Any]:
    """Load the memo; any read/parse failure degrades to an empty dict."""
    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def lookup(cache: dict[str, Any], sha256: str) -> dict | None:
    """The stored resolution jsonable for *sha256*, or ``None``.

    Version-checked: an entry written by a different schema generation is
    treated as absent (and re-resolved), never half-parsed.
    """
    entry = cache.get(sha256)
    if not isinstance(entry, dict) or entry.get("v") != CACHE_VERSION:
        return None
    resolution = entry.get("resolution")
    return resolution if isinstance(resolution, dict) else None


def lookup_queried_at(cache: dict[str, Any], sha256: str) -> float | None:
    """The stored entry's ``queried_at`` timestamp, or ``None``.

    Serves the UI's "cached · N d ago" disclosure (2026-08-19 review) — the
    stamp was written all along for age-based eviction and never surfaced.
    """
    entry = cache.get(sha256)
    if not isinstance(entry, dict) or entry.get("v") != CACHE_VERSION:
        return None
    ts = entry.get("queried_at")
    return float(ts) if isinstance(ts, (int, float)) else None


def store(cache: dict[str, Any], sha256: str, resolution: dict) -> None:
    """Record *resolution* for *sha256*, stamped for age-based eviction."""
    if not sha256:
        return
    cache[sha256] = {
        "v": CACHE_VERSION,
        "queried_at": time.time(),
        "resolution": resolution,
    }


def save_cache(cache: dict[str, Any]) -> None:
    """Persist the memo atomically; failures are logged, never raised."""
    try:
        pruned = cache
        if len(pruned) > _MAX_ENTRIES:
            def _age(item: tuple[str, Any]) -> float:
                entry = item[1]
                ts = entry.get("queried_at") if isinstance(entry, dict) else 0
                return float(ts) if isinstance(ts, (int, float)) else 0.0

            pruned = dict(sorted(pruned.items(), key=_age)[-_MAX_ENTRIES:])
        target = cache_path()
        fd, tmp = tempfile.mkstemp(
            dir=str(target.parent), prefix=".accession_cache-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(pruned, f, separators=(",", ":"))
            os.replace(tmp, str(target))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning("accession_cache save failed (cache skipped): %s", exc)
