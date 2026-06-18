"""Aim (iii): Zotero entries (parents) that the user has not 'read' yet.

Proxy for 'read': the parent item has at least one child note in Zotero.
The user's workflow is: when actually reading, create a child note and
write personal observations. So 'no child note' is a reasonable signal
for 'queued in Zotero but not actually engaged with yet'.

This report flows from the bib side. We match _master.bib citation keys
to Zotero items via normalized title (same as inventory). Records with
a bib entry but no Zotero match are silently skipped (we trust the bib).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..inventory import Inventory


@dataclass
class ZoteroUnreadRow:
    citation_key: str
    title: str | None
    year: str | None
    author: str | None = None


@dataclass
class ZoteroUnreadReport:
    rows: list[ZoteroUnreadRow] = field(default_factory=list)
    skipped_no_zotero_match: int = 0


def find(inv: Inventory) -> ZoteroUnreadReport:
    rep = ZoteroUnreadReport()
    for rec in inv.records.values():
        if not rec.bib_entry:
            continue
        if rec.zotero_item is None:
            rep.skipped_no_zotero_match += 1
            continue
        if rec.zotero_item.child_notes:
            continue
        author_str = rec.bib_entry.authors[0] if rec.bib_entry.authors else None
        rep.rows.append(
            ZoteroUnreadRow(
                citation_key=rec.citation_key,
                title=rec.bib_entry.title,
                year=rec.bib_entry.year,
                author=author_str,
            )
        )
    rep.rows.sort(key=lambda r: (r.year or "", r.citation_key))
    return rep
