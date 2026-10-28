"""Not in Zotero — the PDFs the user curated as "not in the bib".

Why this exists (2026-09-06): the ``[not in bib]`` verb writes a PDF into
``mapping.json``'s ``no_match`` list, and from the next scan on the bridge
treats it as matched: it leaves *PDFs Without Zotero*, the accession deck
and ``/api/accession/rows``, and survives only as a COUNT on the dashboard
and in that report's meta line. The decision is therefore terminal in
practice — the per-PDF ``clear`` undo lives in session memory and cannot
find the row after a reload. Measured on the live library the bucket held
291 files, and for most of them the intent had been "add to Zotero later",
not "never a paper": 203 carried a resolvable DOI in the accession cache.

This report lists the bucket. It is read-only and, like every module under
``audit/``, imports nothing outside the vendored tree: the accession join
(outcome, tier, best DOI) is an app-side enrichment the route injects into
the serializer, exactly as ``vocab_id_for`` is for the drift reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ...config import Settings
from ..bridge import zotero_unclaimed
from ..inventory import Inventory


@dataclass
class NotInZoteroRow:
    pdf: Path
    exists: bool
    #: Curated decision bucket: ``no_match`` ("not a paper for Zotero") or
    #: ``pending`` ("add to Zotero later") — the two-state split, Phase 2.
    state: str = "no_match"


@dataclass
class NotInZoteroReport:
    rows: list[NotInZoteroRow] = field(default_factory=list)
    missing_count: int = 0  # curated paths that no longer exist on disk
    no_match_count: int = 0
    pending_count: int = 0
    #: Curated files Zotero links but the rung could not claim (unexported
    #: key, or two items link it). Disclosed, never listed (2026-09-20): a
    #: hand-linked PDF must not read as "not in Zotero", and Phase 2 of the
    #: cleanup reads this bucket to propose a NEW record for each row.
    zotero_unclaimed_count: int = 0


def find(inv: Inventory, settings: Settings) -> NotInZoteroReport:
    """Every PDF in the bridge's two curated buckets, by name, pending first."""
    rep = NotInZoteroReport()
    seen: set[Path] = set()
    buckets = (
        ("pending", getattr(inv.bridge, "pending_pdfs", None) or []),
        ("no_match", inv.bridge.confirmed_no_match_pdfs),
    )
    for state, paths in buckets:
        for p in paths:
            if p in seen:
                continue
            seen.add(p)
            if zotero_unclaimed(inv.bridge, p):
                rep.zotero_unclaimed_count += 1
                continue
            exists = p.is_file()
            if not exists:
                rep.missing_count += 1
            if state == "pending":
                rep.pending_count += 1
            else:
                rep.no_match_count += 1
            rep.rows.append(NotInZoteroRow(pdf=p, exists=exists, state=state))
    rep.rows.sort(key=lambda r: (r.state != "pending", r.pdf.name.lower()))
    return rep
