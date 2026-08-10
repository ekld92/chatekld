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

    This is the COMPLETE set of un-Zotero'd PDFs — read and unread alike.
    ``suggested_read_cutoff`` is advisory: the UI draws a divider at it but it
    does **not** filter rows (see :func:`find`). ``unread_unzoterod`` is a
    strict SUBSET of this report (same source list, filtered to
    ``annotations < threshold``), not its complement.

    The three ``*_count`` fields are **exclusion disclosure** (2026-07-31), not
    row data: they name the PDFs that are missing from ``rows`` so the tab can
    say so out loud. Before this, "PDFs Without Zotero" silently omitted the
    bridge's ambiguous and confirmed-no-match buckets and the skip-prefix files,
    and nothing on screen hinted that the list was filtered at all.
    """

    rows: list[ReadUnzoterodRow] = field(default_factory=list)
    suggested_read_cutoff: int = 5  # informational only
    # Excluded from `rows` — disclosed, never listed:
    ambiguous_count: int = 0            # bridge matched >1 bib key (triage deck)
    confirmed_no_match_count: int = 0   # user curated "not in the bib"
    skipped_count: int = 0              # biblio_skip_prefix files


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
    # Exclusion disclosure (2026-07-31): `unmapped_pdfs` is only ONE of the
    # bridge's residual buckets, and the skip-prefix files never reach the
    # bridge at all, so "every unmapped PDF appears" is not the same claim as
    # "every un-Zotero'd PDF appears". Count what is left out so the UI can say
    # so; deliberately counts only — the confirmed-no-match files are ones the
    # user already triaged and listing them would re-clutter the queue.
    rep.ambiguous_count = len(inv.bridge.ambiguous_pdfs)
    rep.confirmed_no_match_count = len(inv.bridge.confirmed_no_match_pdfs)
    rep.skipped_count = len(inv.pdfs_skipped)
    for p in inv.bridge.unmapped_pdfs:
        res = annotations.get(p) if annotations else None
        if res is None:
            res = pdf_annotations.read_annotations(p)
        rep.rows.append(ReadUnzoterodRow(pdf=p, annotations=res.count, error=res.error))
    rep.rows.sort(key=lambda r: (-r.annotations, r.pdf.name))
    return rep
