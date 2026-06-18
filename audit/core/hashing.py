"""SHA-256 hashing with size-group + partial-hash pre-filter.

Pipeline: group by size -> partial hash (first 8 KB) -> full hash.
Files with a unique size or unique partial hash are never fully hashed.

`find_duplicate_sets` accepts an optional `cancel_fn` so long scans can be
aborted by the manager — it's checked between files (not mid-file).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path

PARTIAL_BYTES = 8192
CHUNK = 65536


def _hash(path: Path, partial: bool) -> str | None:
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
    return _hash(path, partial=True)


def full_hash(path: Path) -> str | None:
    return _hash(path, partial=False)


def find_duplicate_sets(
    paths: Iterable[Path],
    *,
    cancel_fn: Callable[[], bool] | None = None,
) -> dict[str, list[Path]]:
    """Return {full_sha256: [paths]} only for content-identical sets of size >= 2.

    Returns the partial result if `cancel_fn` ever returns True."""

    def _cancelled() -> bool:
        return cancel_fn is not None and cancel_fn()

    size_map: dict[int, list[Path]] = defaultdict(list)
    for p in paths:
        if _cancelled():
            return {}
        try:
            size_map[p.stat().st_size].append(p)
        except OSError:
            continue

    duplicates: dict[str, list[Path]] = {}
    for candidates in size_map.values():
        if _cancelled():
            return duplicates
        if len(candidates) < 2:
            continue
        partial_map: dict[str, list[Path]] = defaultdict(list)
        for p in candidates:
            if _cancelled():
                return duplicates
            qh = partial_hash(p)
            if qh:
                partial_map[qh].append(p)
        for qcandidates in partial_map.values():
            if _cancelled():
                return duplicates
            if len(qcandidates) < 2:
                continue
            full_map: dict[str, list[Path]] = defaultdict(list)
            for p in qcandidates:
                if _cancelled():
                    return duplicates
                fh = full_hash(p)
                if fh:
                    full_map[fh].append(p)
            for fh, group in full_map.items():
                if len(group) >= 2:
                    duplicates[fh] = group
    return duplicates
