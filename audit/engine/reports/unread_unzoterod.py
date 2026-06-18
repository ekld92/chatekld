"""Aim (ii): PDFs in biblio_articles that are NOT in Zotero AND look unread.

'Not in Zotero' = bridge could not resolve the PDF to any bib entry,
and the user has not explicitly confirmed-no-match.
'Looks unread' = annotation count < threshold (-1 / unreadable counts as 0).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ...config import Settings
from ...core import pdf_annotations
from ...core.pdf_annotations import AnnotationsResult, ErrorKind
from ..inventory import Inventory


@dataclass
class UnreadUnzoterodRow:
    pdf: Path
    annotations: int
    error: ErrorKind | None = None


@dataclass
class UnreadUnzoterodReport:
    rows: list[UnreadUnzoterodRow] = field(default_factory=list)
    threshold: int = 5
    ambiguous_count: int = (
        0  # diagnostic — pdfs the bridge couldn't decide between multiple keys
    )


def find(
    inv: Inventory,
    settings: Settings,
    *,
    annotations: dict[Path, AnnotationsResult] | None = None,
) -> UnreadUnzoterodReport:
    rep = UnreadUnzoterodReport(threshold=settings.annotations_read_threshold)
    rep.ambiguous_count = len(inv.bridge.ambiguous_pdfs)
    for p in inv.bridge.unmapped_pdfs:
        res = annotations.get(p) if annotations else None
        if res is None:
            res = pdf_annotations.read_annotations(p)
        annot = res.count if res.count >= 0 else 0
        if annot < settings.annotations_read_threshold:
            rep.rows.append(
                UnreadUnzoterodRow(pdf=p, annotations=annot, error=res.error)
            )
    rep.rows.sort(key=lambda r: (r.annotations, r.pdf.name))
    return rep
