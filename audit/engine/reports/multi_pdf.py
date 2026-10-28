"""Entries With Several PDFs — records owning ≥2 files, with per-PDF provenance.

Why this exists (2026-09-06): the bridge accumulates every file a rung
claims for a key (``bib_to_pdfs`` is a list), so one citation key can own
several PDFs. That is sometimes right (two parts, a translation, a
supplement) and sometimes an absorption: the ``_N_`` numbered-sibling
filename convention is stripped before the author+year rung, so a SECOND
same-surname-same-year paper with no bib entry of its own lands on its
neighbour's key. Ambiguity is only ever declared when several bib entries
compete for one PDF — never when several PDFs land on one entry — so the
absorption is silent by construction.

Until now the only surface was the Possible Duplicates ``multi_pdf`` tier,
which cannot tell "two versions of one paper" from "two papers on one key";
dismissing there was the only way to clear the queue, and on the measured
library every one of the 107 groups had been dismissed, so the tier read 0.

This report is read-only and lists every multi-PDF record with HOW each file
was matched (``BridgeResult.source_per_pdf``), mixed-provenance groups first
— a DOI-verified file beside a surname+year one is the first thing to review.
``dismissed`` mirrors the Possible Duplicates decision for the same group so
the two tabs agree; it needs the files' sha256, which this module never
computes (``sha_for`` is injected — a memo hit or ``None``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..inventory import Inventory
from ..near_duplicates import _pair_id

#: Rungs whose claim rests on a filename alone. A group mixing one of these
#: with an identifier-backed rung is the review priority.
FILENAME_SOURCES = frozenset({"authoryear", "fm_authoryear"})


@dataclass
class MultiPdfFile:
    path: Path
    source: str  # BridgeResult.source_per_pdf label, or "unknown"


@dataclass
class MultiPdfRow:
    citation_key: str
    title: str | None
    year: str | None
    author: str | None
    files: list[MultiPdfFile] = field(default_factory=list)
    #: True when the group holds BOTH a filename-only rung and any other rung
    #: (the "absorbed sibling" shape). Sorted first.
    mixed: bool = False
    #: True when EVERY file was matched by a filename-only rung — nothing in
    #: the group is identifier-verified, so the whole group is a guess.
    all_filename: bool = False
    #: The Possible Duplicates decision for this exact group: True when the
    #: user dismissed it as "not a duplicate", False when not dismissed, None
    #: when unknowable (a member's sha was not memoised).
    dismissed: bool | None = None
    pair_id: str | None = None


@dataclass
class MultiPdfReport:
    rows: list[MultiPdfRow] = field(default_factory=list)
    mixed_count: int = 0
    all_filename_count: int = 0
    dismissed_count: int = 0
    file_count: int = 0


def find(
    inv: Inventory,
    *,
    dismissed: set[str] | None = None,
    sha_for: Callable[[Path], str | None] | None = None,
) -> MultiPdfReport:
    """Every record with ≥2 PDFs, mixed-provenance groups first.

    ``dismissed`` is the Possible Duplicates pair-id set and ``sha_for`` the
    hash MEMO lookup (never a hashing pass — a report load must not hash the
    library). Without both, ``dismissed`` on every row is ``None`` rather than
    a guessed ``False``.
    """
    rep = MultiPdfReport()
    src = inv.bridge.source_per_pdf if inv.bridge is not None else {}
    skip = dismissed if dismissed is not None else None
    for key, rec in inv.records.items():
        if len(rec.pdf_paths) < 2:
            continue
        files = [MultiPdfFile(p, src.get(p, "unknown")) for p in rec.pdf_paths]
        labels = {f.source for f in files}
        fn = labels & FILENAME_SOURCES
        mixed = bool(fn) and bool(labels - FILENAME_SOURCES)
        all_fn = bool(fn) and not (labels - FILENAME_SOURCES)
        dis: bool | None = None
        pid: str | None = None
        if skip is not None and sha_for is not None:
            shas = [sha_for(p) for p in rec.pdf_paths]
            if all(shas):
                distinct = sorted(set(s for s in shas if s))
                if len(distinct) >= 2:
                    pid = _pair_id("multi_pdf", distinct)
                    dis = pid in skip
        be = rec.bib_entry
        rep.rows.append(MultiPdfRow(
            citation_key=key,
            title=be.title if be else None,
            year=be.year if be else None,
            author=(be.authors[0] if be and be.authors else None),
            files=sorted(files, key=lambda f: f.path.name),
            mixed=mixed,
            all_filename=all_fn,
            dismissed=dis,
            pair_id=pid,
        ))
    rep.rows.sort(key=lambda r: (not r.mixed, -len(r.files), r.citation_key))
    rep.mixed_count = sum(1 for r in rep.rows if r.mixed)
    rep.all_filename_count = sum(1 for r in rep.rows if r.all_filename)
    rep.dismissed_count = sum(1 for r in rep.rows if r.dismissed)
    rep.file_count = sum(len(r.files) for r in rep.rows)
    return rep
