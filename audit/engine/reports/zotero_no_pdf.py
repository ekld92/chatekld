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
    citation_key: str
    title: str | None
    year: str | None
    has_zotero_match: bool
    author: str | None = None


@dataclass
class ZoteroNoPdfReport:
    rows: list[ZoteroNoPdfRow] = field(default_factory=list)


def find(inv: Inventory) -> ZoteroNoPdfReport:
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
