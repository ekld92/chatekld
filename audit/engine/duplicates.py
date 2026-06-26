"""Content-duplicate detection scoped to biblio_articles/."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..core import hashing


@dataclass
class DuplicateSet:
    """A group of byte-identical PDFs sharing one full-content SHA-256.

    ``paths`` always holds ≥2 entries. ``size_bytes`` is the per-file size
    (all copies are identical, so any one applies).
    """

    content_hash: str
    paths: list[Path]
    size_bytes: int

    @property
    def wasted_bytes(self) -> int:
        """Reclaimable bytes: every copy beyond the first is redundant."""
        return self.size_bytes * (len(self.paths) - 1)


def find_biblio_duplicates(
    settings: Settings,
    *,
    cancel_fn: Callable[[], bool] | None = None,
) -> list[DuplicateSet]:
    """Find content-identical PDF sets under ``biblio_articles``.

    Read-only: hashes file content only, never opens for write. Delegates the
    size→partial→full hash funnel to ``core.hashing.find_duplicate_sets``
    (most files never get fully hashed). Result sets are sorted by reclaimable
    space descending so the biggest wins lead. ``cancel_fn`` is threaded into
    the hasher and checked between files, so the manager can abort a long run;
    the partial result gathered so far is returned. A per-set ``stat`` failure
    degrades that set's ``size_bytes`` to 0 rather than aborting.
    """
    root = settings.biblio_articles_dir
    if not root.exists():
        return []
    pdfs = [p for p in root.rglob("*.pdf") if p.is_file()]
    raw = hashing.find_duplicate_sets(pdfs, cancel_fn=cancel_fn)
    out: list[DuplicateSet] = []
    for h, paths in raw.items():
        try:
            size = paths[0].stat().st_size
        except OSError:
            size = 0
        out.append(DuplicateSet(content_hash=h, paths=sorted(paths), size_bytes=size))
    out.sort(key=lambda d: d.wasted_bytes, reverse=True)
    return out
