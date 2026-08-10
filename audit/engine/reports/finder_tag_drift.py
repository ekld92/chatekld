"""Per-paper Finder-tag vs note-tag comparison (plan 2026-08-05/3).

Before this report, "this PDF is Finder-tagged X but the note isn't" was not
derivable from ANY surface: Tag Hygiene shows only global per-source counts.
Read-only like the rest of the audit; fixing a gap goes through the tagging
subsystem's confirmed writers (``apply-finder-tags`` / ``apply-note-tags``),
never a new audit write path. Since the drift-fix feature (2026-08-06) the UI
dispatches per-row to exactly those writers; this module still writes nothing —
it additionally carries ``variants`` (present-both-sides spelling near-misses,
informational) and ``note_path`` so the serializer can name the write target.

Comparison uses the shared ``normalize.tag_key`` fingerprint (with the
optional route-injected synonym map), like Note Tag Drift.

Asymmetric emission rule: ``finder_only`` (a Finder tag the note lacks) is
always meaningful — the user deliberately tagged that PDF. ``note_only`` is
computed only for records that carry >=1 Finder tag: most PDFs simply have no
Finder tags at all (and on non-macOS every set is empty), so "every note tag
is missing from Finder" would be noise, not drift. A record with zero Finder
tags therefore never emits a row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..inventory import Inventory
from ..normalize import group_by_key
from .note_tag_drift import variant_pairs


@dataclass
class FinderTagDriftRow:
    """One citation key whose PDF Finder tags and note YAML tags disagree.

    ``finder_tags`` is the union across the record's resolved PDFs (that is
    how the inventory aggregates them). ``finder_only`` / ``note_only`` are
    one representative surface per fingerprint, per the shared comparison.
    ``variants`` are pairs present on BOTH sides under different spellings
    (``(finder surface, note surface, reason)`` — reason ``spelling`` |
    ``synonym``): informational, never applyable, exactly like Note Tag
    Drift's. ``note_path`` is the record's Obsidian note (always present —
    the emission gate requires one) so the serializer can name the write
    target without re-resolving.
    """

    citation_key: str
    finder_tags: set[str] = field(default_factory=set)
    obs_tags: set[str] = field(default_factory=set)
    finder_only: set[str] = field(default_factory=set)
    note_only: set[str] = field(default_factory=set)
    variants: list[tuple[str, str, str]] = field(default_factory=list)
    note_path: Path | None = None
    pdf_count: int = 0
    author: str | None = None
    title: str | None = None


def find_drift(
    inv: Inventory,
    *,
    synonyms: dict[str, str] | None = None,
) -> list[FinderTagDriftRow]:
    """Rows for records with an Obsidian note, >=1 PDF and >=1 Finder tag whose
    Finder/note tag sets disagree (fingerprint comparison) or overlap only
    under different spellings (``variants`` — a variants-only row is emitted
    so the near-miss is not silently invisible). Sorted by total disagreement
    desc, then variant count desc, then citation key."""
    rows: list[FinderTagDriftRow] = []
    for rec in inv.records.values():
        if not rec.obsidian_note or not rec.pdf_paths:
            continue
        finder = set(rec.finder_tag_set)
        if not finder:
            continue
        note = rec.obs_tags
        f_by = group_by_key(finder, synonyms)
        n_by = group_by_key(note, synonyms)
        finder_only = {
            sorted(forms)[0] for k, forms in f_by.items() if k not in n_by}
        note_only = {
            sorted(forms)[0] for k, forms in n_by.items() if k not in f_by}
        # variant_pairs is generic over two tag sets (its parameter names say
        # "zotero" for its original caller); here side one is Finder.
        variants = variant_pairs(finder, note, synonyms=synonyms)
        if not finder_only and not note_only and not variants:
            continue
        rows.append(FinderTagDriftRow(
            citation_key=rec.citation_key,
            finder_tags=finder,
            obs_tags=note,
            finder_only=finder_only,
            note_only=note_only,
            variants=variants,
            note_path=rec.obsidian_note.path,
            pdf_count=len(rec.pdf_paths),
            author=(rec.bib_entry.authors[0]
                    if rec.bib_entry and rec.bib_entry.authors else None),
            title=rec.bib_entry.title if rec.bib_entry else None,
        ))
    rows.sort(
        key=lambda r: (-(len(r.finder_only) + len(r.note_only)),
                       -len(r.variants), r.citation_key))
    return rows
