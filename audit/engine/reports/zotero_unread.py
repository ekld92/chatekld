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
    """One bib entry whose matched Zotero parent has no child reading note."""

    citation_key: str
    title: str | None
    year: str | None
    author: str | None = None


@dataclass
class ZoteroUnreadReport:
    """Zotero parents with no child note (the "not yet read" proxy).

    ``skipped_no_zotero_match`` counts bib entries that matched no Zotero
    parent at all — they cannot be judged read/unread, so they are skipped
    (the bib is trusted) and only tallied.
    """

    rows: list[ZoteroUnreadRow] = field(default_factory=list)
    skipped_no_zotero_match: int = 0


def find(inv: Inventory) -> ZoteroUnreadReport:
    """Reconciliation rule for aim (iii): Zotero items with no child note.

    Flows from the bib side: for each record with a bib entry, the matched
    Zotero parent (joined by normalized title in the inventory) is inspected —
    a parent with *no* child note is "queued in Zotero but not actually
    engaged with", since the user's habit is to add a reading note when they
    read. Records whose bib entry matched no Zotero parent are skipped and
    counted in ``skipped_no_zotero_match``. Sorted by year then key.
    """
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
