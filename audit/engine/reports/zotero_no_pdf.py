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
    # The Zotero item type (2026-08-26). Additive, and load-bearing for the
    # queue: a `videoRecording` or a `webpage` has no PDF by nature, so this is
    # what lets the user dismiss a whole class in one action instead of
    # scrolling past it forever.
    item_type: str | None = None


@dataclass
class ZoteroNoPdfReport:
    """Bib/Zotero entries that have no PDF under ``biblio_articles``."""

    rows: list[ZoteroNoPdfRow] = field(default_factory=list)
    #: Rows withheld because the user marked them as needing no PDF. A COUNT,
    #: never a silently shorter list — the Tag Hygiene under-reporting lesson.
    dismissed_count: int = 0
    #: Item-type histogram over the VISIBLE rows, for the bulk-dismiss control.
    by_item_type: dict = field(default_factory=dict)


def find(inv: Inventory, *, dismissed: set[str] | None = None) -> ZoteroNoPdfReport:
    """Reconciliation rule for aim (v): bib entries the bridge found no PDF for.

    Emits a row for every record that has a bib entry but an empty
    ``pdf_paths`` (the bridge resolved no file to that citation key).
    ``has_zotero_match`` flags whether the entry at least matched a Zotero
    parent, so the user can tell "in Zotero but no local PDF" from "in the bib
    only". Sorted by year then key.

    ``dismissed`` (2026-08-26) is the set of citation keys the user marked as
    needing no PDF — see ``engine/pdf_wanted.py``. Filtering happens HERE, at
    render time, not in the scan: a decision then takes effect immediately
    instead of at the next scan, and the number withheld is reported so the
    list is never silently shorter than the truth. Until this existed the
    report could only grow, which is what made it an overview rather than a
    queue.
    """
    rep = ZoteroNoPdfReport()
    skip = dismissed or set()
    for rec in inv.records.values():
        if not rec.bib_entry:
            continue
        if rec.pdf_paths:
            continue
        if rec.citation_key in skip:
            rep.dismissed_count += 1
            continue
        author_str = rec.bib_entry.authors[0] if rec.bib_entry.authors else None
        item_type = rec.zotero_item.item_type if rec.zotero_item else None
        rep.rows.append(
            ZoteroNoPdfRow(
                citation_key=rec.citation_key,
                title=rec.bib_entry.title,
                year=rec.bib_entry.year,
                has_zotero_match=rec.zotero_item is not None,
                author=author_str,
                item_type=item_type,
            )
        )
    rep.rows.sort(key=lambda r: (r.year or "", r.citation_key))
    counts: dict[str, int] = {}
    for row in rep.rows:
        key = row.item_type or "unknown"
        counts[key] = counts.get(key, 0) + 1
    rep.by_item_type = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
    return rep
