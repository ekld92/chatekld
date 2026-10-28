"""Content-duplicate detection scoped to biblio_articles/."""

from __future__ import annotations

from ..core.pdf_paths import pdf_files

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def candidate_pdfs(settings: Settings) -> list[Path]:
    """The duplicate scan's candidate walk: every ``.pdf`` under
    ``biblio_articles``, PRUNED by ``settings.ignored_dirs`` (plan
    2026-08-05/4 — the inventory and bridge walks always pruned
    ``.git/.obsidian/.trash``; the duplicate walk did not, so a trashed copy
    counted as a duplicate while a live copy elsewhere in ``Z_attachments``
    was out of scope). ``biblio_skip_prefix`` files remain deliberately
    INCLUDED — the documented "exempt from everything except duplicates"
    contract. Shared with the near-duplicates engine so the two reports see
    one candidate set.
    """
    root = settings.biblio_articles_dir
    if not root.exists():
        return []
    ignored = set(settings.ignored_dirs)
    out: list[Path] = []
    for p in pdf_files(root):
        if not p.is_file():
            continue
        try:
            parents = p.relative_to(root).parts[:-1]
        except ValueError:
            parents = ()
        if any(part in ignored for part in parents):
            continue
        out.append(p)
    return out


def find_biblio_duplicates(
    settings: Settings,
    *,
    cancel_fn: Callable[[], bool] | None = None,
    hash_cache: dict[str, Any] | None = None,
) -> list[DuplicateSet]:
    """Find content-identical PDF sets under ``biblio_articles``.

    Read-only: hashes file content only, never opens for write. Delegates the
    size→partial→full hash funnel to ``core.hashing.find_duplicate_sets``
    (most files never get fully hashed; with *hash_cache*, unchanged files are
    never even read). Result sets are sorted by reclaimable space descending
    so the biggest wins lead. ``cancel_fn`` is threaded into the hasher and
    checked between files, so the manager can abort a long run; the partial
    result gathered so far is returned. A per-set ``stat`` failure degrades
    that set's ``size_bytes`` to 0 rather than aborting.
    """
    pdfs = candidate_pdfs(settings)
    raw = hashing.find_duplicate_sets(
        pdfs, cancel_fn=cancel_fn, hash_cache=hash_cache)
    out: list[DuplicateSet] = []
    for h, paths in raw.items():
        try:
            size = paths[0].stat().st_size
        except OSError:
            size = 0
        out.append(DuplicateSet(content_hash=h, paths=sorted(paths), size_bytes=size))
    out.sort(key=lambda d: d.wasted_bytes, reverse=True)
    return out
