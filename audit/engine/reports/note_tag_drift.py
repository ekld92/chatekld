"""Aim (i): tagging discrepancies between Zotero child-note tags and
Obsidian YAML tags, surfaced only when Zotero has tags Obsidian is missing.

Direction: Zotero -> Obsidian. The reverse direction is intentionally
not flagged (you don't care about syncing back into Zotero notes).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..inventory import Inventory


@dataclass
class NoteTagDriftRow:
    citation_key: str
    zotero_note_tags: set[str] = field(default_factory=set)
    obs_tags: set[str] = field(default_factory=set)
    missing_in_obs: set[str] = field(default_factory=set)
    author: str | None = None
    title: str | None = None


def find_drift(inv: Inventory) -> list[NoteTagDriftRow]:
    rows: list[NoteTagDriftRow] = []
    for rec in inv.records.values():
        # Require both sources to exist.
        if not rec.zotero_item or not rec.zotero_item.child_notes:
            continue
        if not rec.obsidian_note:
            continue
        zn = rec.zotero_note_tags
        on = rec.obs_tags
        missing = zn - on
        if not missing:
            continue
        author_str = (
            rec.bib_entry.authors[0]
            if rec.bib_entry and rec.bib_entry.authors
            else None
        )
        title_str = rec.bib_entry.title if rec.bib_entry else None
        rows.append(
            NoteTagDriftRow(
                citation_key=rec.citation_key,
                zotero_note_tags=zn,
                obs_tags=on,
                missing_in_obs=missing,
                author=author_str,
                title=title_str,
            )
        )
    rows.sort(key=lambda r: (-len(r.missing_in_obs), r.citation_key))
    return rows
