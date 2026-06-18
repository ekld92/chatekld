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
    pdf: Path
    annotations: int  # -1 if pikepdf couldn't open
    error: ErrorKind | None = None


@dataclass
class ReadUnzoterodReport:
    rows: list[ReadUnzoterodRow] = field(default_factory=list)
    suggested_read_cutoff: int = 5  # informational only


def find(
    inv: Inventory,
    settings: Settings,
    *,
    annotations: dict[Path, AnnotationsResult] | None = None,
) -> ReadUnzoterodReport:
    rep = ReadUnzoterodReport(suggested_read_cutoff=settings.annotations_read_threshold)
    for p in inv.bridge.unmapped_pdfs:
        res = annotations.get(p) if annotations else None
        if res is None:
            res = pdf_annotations.read_annotations(p)
        rep.rows.append(ReadUnzoterodRow(pdf=p, annotations=res.count, error=res.error))
    rep.rows.sort(key=lambda r: (-r.annotations, r.pdf.name))
    return rep
