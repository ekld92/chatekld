"""Content-duplicate detection scoped to biblio_articles/."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..core import hashing


@dataclass
class DuplicateSet:
    content_hash: str
    paths: list[Path]
    size_bytes: int

    @property
    def wasted_bytes(self) -> int:
        return self.size_bytes * (len(self.paths) - 1)


def find_biblio_duplicates(
    settings: Settings,
    *,
    cancel_fn: Callable[[], bool] | None = None,
) -> list[DuplicateSet]:
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
