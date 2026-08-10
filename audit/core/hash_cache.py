"""Persistent full-hash memo for the duplicate scan (plan 2026-08-05/4).

``BASE_DIR/audit/hash_cache.json`` maps ``{abs path: {size, mtime_ns,
sha256}}``. Before it existed every scan re-hashed every candidate byte —
the RAG layer has kept exactly this shape of memo for years
(``pdf_signatures/<vault_key>.json``), but the vendored tree cannot import
it, so this is the audit's own copy of the pattern.

Entries self-invalidate on a size/mtime mismatch (checked by
``core.hashing.cached_full_hash``, the only consumer). The file is a
REGENERABLE cache: the atomic tmp-sibling + ``os.replace`` write is kept (a
torn file must never be read back) but the fsync the unregenerable
``mapping.json`` writer pays is deliberately skipped — a lost entry costs one
re-hash, not data (the same trade the per-document PDF cache documents).
Stale keys (files that no longer exist) are pruned at save time so a
reorganised attachments tree does not grow the file forever.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 100_000  # backstop far above any real library


def cache_path() -> Path:
    """``BASE_DIR/audit/hash_cache.json`` (same dir as ``mapping.json``)."""
    from ..config import _audit_dir

    return Path(_audit_dir()) / "hash_cache.json"


def load_cache() -> dict[str, Any]:
    """Load the memo; any read/parse failure degrades to an empty dict."""
    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict[str, Any]) -> None:
    """Persist the memo atomically; failures are logged, never raised.

    Prunes entries whose path no longer exists (one ``os.path.exists`` per
    key — trivial next to the hashing it saves) and applies the entry-count
    backstop before writing.
    """
    try:
        pruned = {k: v for k, v in cache.items() if os.path.exists(k)}
        if len(pruned) > _MAX_ENTRIES:
            pruned = dict(list(pruned.items())[-_MAX_ENTRIES:])
        target = cache_path()
        fd, tmp = tempfile.mkstemp(
            dir=str(target.parent), prefix=".hash_cache-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(pruned, f)
            os.replace(tmp, str(target))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning("hash_cache save failed (cache skipped): %s", exc)
