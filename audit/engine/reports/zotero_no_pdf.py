"""Aim (v): Zotero/bib entries for which no PDF exists in biblio_articles.

Overview-only: the user does not necessarily want a PDF for each entry,
but wants to be able to see which ones don't have one. Resolution status
comes from engine.bridge.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..inventory import Inventory


@dataclass
class ZoteroNoPdfRow:
    """One bib entry with no resolved PDF; ``has_zotero_match`` is for context."""

    citation_key: str
    title: str | None
    year: str | None
    has_zotero_match: bool
    author: str | None = None


@dataclass
class ZoteroNoPdfReport:
    """Bib/Zotero entries that have no PDF under ``biblio_articles``."""

    rows: list[ZoteroNoPdfRow] = field(default_factory=list)


def find(inv: Inventory) -> ZoteroNoPdfReport:
    """Reconciliation rule for aim (v): bib entries the bridge found no PDF for.

    Emits a row for every record that has a bib entry but an empty
    ``pdf_paths`` (the bridge resolved no file to that citation key).
    ``has_zotero_match`` flags whether the entry at least matched a Zotero
    parent, so the user can tell "in Zotero but no local PDF" from "in the bib
    only". Overview-only — the user doesn't necessarily want a PDF for each,
    just visibility. Sorted by year then key.
    """
    rep = ZoteroNoPdfReport()
    for rec in inv.records.values():
        if not rec.bib_entry:
            continue
        if rec.pdf_paths:
            continue
        author_str = rec.bib_entry.authors[0] if rec.bib_entry.authors else None
        rep.rows.append(
            ZoteroNoPdfRow(
                citation_key=rec.citation_key,
                title=rec.bib_entry.title,
                year=rec.bib_entry.year,
                has_zotero_match=rec.zotero_item is not None,
                author=author_str,
            )
        )
    rep.rows.sort(key=lambda r: (r.year or "", r.citation_key))
    return rep
