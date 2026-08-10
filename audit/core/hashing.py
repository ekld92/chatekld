"""SHA-256 hashing with size-group + partial-hash pre-filter.

Pipeline: group by size -> partial hash (first 8 KB) -> full hash.
Files with a unique size or unique partial hash are never fully hashed.
With a *hash_cache* (plan 2026-08-05/4), a candidate whose full hash is
already memoised (size+mtime valid) skips BOTH read stages.

`find_duplicate_sets` accepts an optional `cancel_fn` so long scans can be
aborted by the manager — it's checked between files (not mid-file).
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PARTIAL_BYTES = 8192
CHUNK = 65536


def _hash(path: Path, partial: bool) -> str | None:
    """SHA-256 of a file, or just its first ``PARTIAL_BYTES`` when ``partial``.

    Streams the full read in ``CHUNK``-sized blocks so a large PDF never loads
    into memory. Returns ``None`` on any ``OSError`` (file vanished, permission
    denied) so an unreadable file drops out of the duplicate search instead of
    aborting it.
    """
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            if partial:
                h.update(f.read(PARTIAL_BYTES))
            else:
                for chunk in iter(lambda: f.read(CHUNK), b""):
                    h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def partial_hash(path: Path) -> str | None:
    """Cheap pre-filter hash over only the file's first ``PARTIAL_BYTES``."""
    return _hash(path, partial=True)


def full_hash(path: Path) -> str | None:
    """Full-content SHA-256, the authoritative duplicate key."""
    return _hash(path, partial=False)


def cached_full_hash(path: Path, cache: dict[str, Any] | None) -> str | None:
    """``full_hash`` through the ``{path: {size, mtime_ns, sha256}}`` memo.

    A hit requires size AND mtime_ns to match (self-invalidating, the RAG
    signature-cache contract); a miss computes and stores into *cache* — the
    caller persists it afterwards. ``cache=None`` degrades to a plain hash.
    """
    if cache is None:
        return full_hash(path)
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    ent = cache.get(key)
    if (
        isinstance(ent, dict)
        and ent.get("size") == st.st_size
        and ent.get("mtime_ns") == st.st_mtime_ns
        and isinstance(ent.get("sha256"), str)
    ):
        return ent["sha256"]
    fh = full_hash(path)
    if fh:
        cache[key] = {
            "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": fh}
    return fh


def _memo_hit(path: Path, cache: dict[str, Any] | None) -> str | None:
    """The memoised sha for *path* iff the entry is size+mtime valid, else None."""
    if cache is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    ent = cache.get(str(path))
    if (
        isinstance(ent, dict)
        and ent.get("size") == st.st_size
        and ent.get("mtime_ns") == st.st_mtime_ns
        and isinstance(ent.get("sha256"), str)
    ):
        return ent["sha256"]
    return None


def find_duplicate_sets(
    paths: Iterable[Path],
    *,
    cancel_fn: Callable[[], bool] | None = None,
    hash_cache: dict[str, Any] | None = None,
) -> dict[str, list[Path]]:
    """Return {full_sha256: [paths]} only for content-identical sets of size >= 2.

    Returns the partial result if `cancel_fn` ever returns True. With
    *hash_cache*, memo-valid candidates contribute their known full hash
    without any read (skipping the partial stage too); only misses go through
    partial -> full. Unreadable files still drop out of the search, but are
    now counted and logged once instead of vanishing silently.
    """

    def _cancelled() -> bool:
        return cancel_fn is not None and cancel_fn()

    unreadable = 0
    size_map: dict[int, list[Path]] = defaultdict(list)
    for p in paths:
        if _cancelled():
            return {}
        try:
            size_map[p.stat().st_size].append(p)
        except OSError:
            unreadable += 1
            continue

    duplicates: dict[str, list[Path]] = {}
    for candidates in size_map.values():
        if _cancelled():
            break
        if len(candidates) < 2:
            continue
        # Memo-valid candidates already carry their authoritative full hash —
        # feed them straight into the final map, zero reads.
        full_map: dict[str, list[Path]] = defaultdict(list)
        misses: list[Path] = []
        for p in candidates:
            hit = _memo_hit(p, hash_cache)
            if hit:
                full_map[hit].append(p)
            else:
                misses.append(p)
        # Snapshot BEFORE the miss loop mutates full_map: the prune exception
        # below exists only because a memoised candidate's partial hash was
        # never computed — full hashes added by earlier partial GROUPS must
        # not widen it (they provably differ from a differently-partial-hashed
        # lone file, so hashing it would be pure wasted I/O).
        has_memo_hits = bool(full_map)
        partial_map: dict[str, list[Path]] = defaultdict(list)
        for p in misses:
            if _cancelled():
                break
            qh = partial_hash(p)
            if qh:
                partial_map[qh].append(p)
            else:
                unreadable += 1
        for qcandidates in partial_map.values():
            if _cancelled():
                break
            # A lone partial hash can still be a duplicate of a MEMOISED
            # candidate (whose partial hash was never computed), so the
            # unique-partial prune only applies when nothing came from memo.
            if len(qcandidates) < 2 and not has_memo_hits:
                continue
            for p in qcandidates:
                if _cancelled():
                    break
                fh = cached_full_hash(p, hash_cache)
                if fh:
                    full_map[fh].append(p)
                else:
                    unreadable += 1
        for fh, group in full_map.items():
            if len(group) >= 2:
                duplicates[fh] = group
    if unreadable:
        logger.warning(
            "duplicate scan: %d file(s) unreadable — excluded from the "
            "duplicate search (permission/vanished).", unreadable)
    return duplicates
