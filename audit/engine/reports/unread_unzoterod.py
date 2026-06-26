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
    """One PDF that is both absent from the bib and below the read threshold."""

    pdf: Path
    annotations: int
    error: ErrorKind | None = None


@dataclass
class UnreadUnzoterodReport:
    """PDFs not in Zotero that also look unread (annotations < threshold).

    ``ambiguous_count`` is a diagnostic only — PDFs the bridge matched to more
    than one candidate key are *excluded* from the rows (they may well be in
    the bib) but counted here so the user knows some were set aside.
    """

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
    """Reconciliation rule for aim (ii): un-Zotero'd AND apparently unread.

    A PDF qualifies when (a) the bridge left it unmapped — not resolvable to
    any bib entry and not on the confirmed-no-match list — and (b) its
    annotation count is below ``annotations_read_threshold``. An unreadable PDF
    (count -1) is treated as 0 annotations, i.e. unread. Ambiguous-bridge PDFs
    are excluded from rows (only counted). Sorted fewest-annotations-first so
    the most clearly-untouched files lead.

    ``annotations`` is the scan's precomputed cache; a missing path falls back
    to a lazy disk read (see :func:`read_unzoterod.find` for the same pattern).
    """
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
