"""Three-way tag comparison across the WRITABLE surfaces (2026-08-13).

The two existing drift reports are pairwise — Zotero-child-note vs Obsidian
(``note_tag_drift``) and Finder vs Obsidian (``finder_tag_drift``) — and
Finder vs Zotero is compared nowhere at all. Tag Hygiene touches all five
stores but aggregates globally by tag, so "paper X has this tag in Finder but
not in Zotero" was not derivable from any report.

This module answers the union question the harmonize feature needs: for one
record, which of the surfaces that CAN be written is missing a tag another one
already carries? The answer is deliberately additive — nothing here ever
suggests removing a tag.

Three scope decisions worth not re-litigating:

* **Writable surfaces only** — the Obsidian reference note, the biblio PDF's
  Finder tags, and the Zotero PARENT item. Zotero child-note tags and
  ``.bib`` keywords are read-only on every path in the app, so folding them
  into the union would propagate tags that no writer could ever take back.
  (``bib`` keywords are also ≈ the parent's tags once BBT re-exports, so they
  would mostly be duplicating a surface already in the union.)

* **No ≥1-Finder-tag gate.** ``finder_tag_drift`` skips any record whose
  Finder set is empty, because for THAT report "every note tag is missing from
  Finder" was noise. Here it is the single most common thing the user wants:
  propagating a tagged note onto its untagged PDF is the point. Copying that
  gate would silently exclude the majority of the work.

* **Actionable rows only.** A row is emitted when at least one present surface
  genuinely lacks a tag another one has. Spelling near-misses are carried on
  such rows as ``variants`` (informational), but a variants-ONLY record emits
  no row: this report is what a bulk selection is built from, and offering a
  row with nothing to apply makes "harmonize these 40" a lie. The two pairwise
  drift reports already surface variants-only records.

Comparison uses the shared ``normalize`` fingerprint with the route-injected
synonym map, exactly like the other two reports, so "same tag" means the same
thing in all three.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..inventory import Inventory
from ..normalize import group_by_key

# The writable surfaces, in the order the UI presents them. Kept here as the
# report's own tuple rather than imported from the app: the vendored tree
# depends only on ``audit/config.py`` (``core.constants.TAGGING_TARGETS`` is
# the app-side twin, and the route layer is where the two must agree).
SURFACES: tuple[str, ...] = ("note", "finder", "zotero")


@dataclass
class TagHarmonyRow:
    """One record whose writable surfaces do not carry the same tag set.

    ``union`` holds ONE representative surface form per canonical key — the
    sorted-first spelling among the surfaces that actually carry it — and the
    three ``missing_in_*`` sets are drawn from that same representative, so a
    tag missing from two surfaces is named identically in both. That matters
    downstream: the route resolves each representative to an exact ``_tags.md``
    id, and two different spellings of one tag would resolve twice.

    ``present`` records which surfaces the record HAS (a record with no
    reference note cannot be missing a note tag — it is missing a note), which
    is what keeps ``missing_in_note`` from meaning two different things.

    ``variants`` is informational: canonical keys carried under more than one
    spelling across the surfaces. Nothing to apply — writing either spelling
    would no-op against the fingerprint comparison — but it is the signal that
    a tag needs a hygiene pass rather than a harmonize.
    """

    citation_key: str
    note_tags: set[str] = field(default_factory=set)
    finder_tags: set[str] = field(default_factory=set)
    zotero_tags: set[str] = field(default_factory=set)
    present: set[str] = field(default_factory=set)
    union: set[str] = field(default_factory=set)
    missing_in_note: set[str] = field(default_factory=set)
    missing_in_finder: set[str] = field(default_factory=set)
    missing_in_zotero: set[str] = field(default_factory=set)
    variants: list[list[str]] = field(default_factory=list)
    note_path: Path | None = None
    pdf_count: int = 0
    author: str | None = None
    title: str | None = None

    @property
    def write_count(self) -> int:
        """How many individual tag writes closing this row would take."""
        return (len(self.missing_in_note) + len(self.missing_in_finder)
                + len(self.missing_in_zotero))

    @property
    def tagged_surface_count(self) -> int:
        """How many present surfaces carry at least one tag.

        ``1`` is the case the user described as "only one tag in either" — one
        surface holds everything and the others hold nothing — which the UI
        offers as a filter preset over the general report.
        """
        return sum(1 for tags in (self.note_tags, self.finder_tags,
                                  self.zotero_tags) if tags)


def find_harmony(
    inv: Inventory,
    *,
    synonyms: dict[str, str] | None = None,
) -> list[TagHarmonyRow]:
    """Rows for records whose writable surfaces disagree, additively.

    A record qualifies when it has **at least two** writable surfaces (with
    fewer there is nothing to harmonize across, only somewhere to invent tags
    from nothing) and at least one of them lacks a tag another carries.
    Sorted most-writes-first, then by citation key, so a bulk selection taken
    off the top of the list closes the most gaps.
    """
    rows: list[TagHarmonyRow] = []
    for rec in inv.records.values():
        note_tags = set(rec.obs_tags)
        finder_tags = set(rec.finder_tag_set)
        zot_tags = set(rec.zotero_item.tags) if rec.zotero_item else set()

        present: set[str] = set()
        if rec.obsidian_note is not None:
            present.add("note")
        if rec.pdf_paths:
            present.add("finder")
        if rec.zotero_item is not None:
            present.add("zotero")
        if len(present) < 2:
            continue

        by_surface = {
            "note": group_by_key(note_tags, synonyms),
            "finder": group_by_key(finder_tags, synonyms),
            "zotero": group_by_key(zot_tags, synonyms),
        }
        # Only surfaces the record HAS contribute to the union: a Zotero item
        # that does not exist cannot be evidence for a tag.
        contributing = {s: by_surface[s] for s in SURFACES if s in present}

        union_keys: set[str] = set()
        for grouped in contributing.values():
            union_keys |= grouped.keys()
        if not union_keys:
            continue

        # One representative spelling per key, shared by every surface that
        # will be asked to write it.
        representative: dict[str, str] = {}
        spellings: dict[str, set[str]] = {}
        for key in union_keys:
            forms: set[str] = set()
            for grouped in contributing.values():
                forms |= grouped.get(key, set())
            spellings[key] = forms
            representative[key] = sorted(forms)[0]

        missing: dict[str, set[str]] = {}
        for surface in SURFACES:
            if surface not in present:
                missing[surface] = set()
                continue
            have = contributing[surface].keys()
            missing[surface] = {representative[k] for k in union_keys - have}

        if not any(missing.values()):
            continue

        rows.append(TagHarmonyRow(
            citation_key=rec.citation_key,
            note_tags=note_tags,
            finder_tags=finder_tags,
            zotero_tags=zot_tags,
            present=present,
            union={representative[k] for k in union_keys},
            missing_in_note=missing["note"],
            missing_in_finder=missing["finder"],
            missing_in_zotero=missing["zotero"],
            variants=sorted(
                (sorted(forms) for forms in spellings.values() if len(forms) > 1),
                key=lambda forms: forms[0],
            ),
            note_path=rec.obsidian_note.path if rec.obsidian_note else None,
            pdf_count=len(rec.pdf_paths),
            author=(rec.bib_entry.authors[0]
                    if rec.bib_entry and rec.bib_entry.authors else None),
            title=rec.bib_entry.title if rec.bib_entry else None,
        ))

    rows.sort(key=lambda r: (-r.write_count, r.citation_key))
    return rows
