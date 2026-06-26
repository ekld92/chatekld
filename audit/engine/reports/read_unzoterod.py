"""Aim (iv): PDFs not in bib, ranked by annotation count desc.

No hard threshold — the top of the list is the actionable subset; you
decide where to stop. A ``suggested_read_cutoff`` is exposed for the UI
to render a visual divider, but it does NOT filter rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ...config import Settings
from ...core import pdf_annotations
from ...core.pdf_annotations import AnnotationsResult, ErrorKind
from ..inventory import Inventory


@dataclass
class ReadUnzoterodRow:
    """One un-Zotero'd PDF with its annotation count (the "read" proxy)."""

    pdf: Path
    annotations: int  # -1 if pikepdf couldn't open
    error: ErrorKind | None = None


@dataclass
class ReadUnzoterodReport:
    """Annotation-ranked list of PDFs absent from the bibliography.

    ``suggested_read_cutoff`` is advisory — the UI draws a divider at it but it
    does **not** filter rows (see :func:`find`).
    """

    rows: list[ReadUnzoterodRow] = field(default_factory=list)
    suggested_read_cutoff: int = 5  # informational only


def find(
    inv: Inventory,
    settings: Settings,
    *,
    annotations: dict[Path, AnnotationsResult] | None = None,
) -> ReadUnzoterodReport:
    """Reconciliation rule for aim (iv): unmapped PDFs ranked by annotations.

    Walks ``inv.bridge.unmapped_pdfs`` (PDFs the bridge could not tie to any
    bib entry) and attaches each one's annotation count, then sorts
    most-annotated-first — the top of the list being the papers most worth
    importing into Zotero. Unlike the unread report there is **no threshold
    filter**; every unmapped PDF appears.

    ``annotations`` is the inventory's precomputed ``{path: AnnotationsResult}``
    cache (populated in parallel during the scan to avoid opening thousands of
    PDFs on the request thread). Any path *missing* from it falls back to a
    lazy ``read_annotations`` on disk — so a ``count_annotations=False`` run
    (empty cache) or a scan cancelled mid-phase (partial cache) still produces
    correct numbers, just more slowly.
    """
    rep = ReadUnzoterodReport(suggested_read_cutoff=settings.annotations_read_threshold)
    for p in inv.bridge.unmapped_pdfs:
        res = annotations.get(p) if annotations else None
        if res is None:
            res = pdf_annotations.read_annotations(p)
        rep.rows.append(ReadUnzoterodRow(pdf=p, annotations=res.count, error=res.error))
    rep.rows.sort(key=lambda r: (-r.annotations, r.pdf.name))
    return rep
