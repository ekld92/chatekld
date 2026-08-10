"""Aim (i): tagging discrepancies between Zotero tags and Obsidian YAML tags.

Primary direction: Zotero -> Obsidian (``missing_in_obs`` is the applyable
set). The reverse direction is *reported* read-only since plan 2026-08-05/3
(``note_only`` — note tags absent from the Zotero side) but never actionable:
writing back into Zotero belongs to the tagging subsystem, not the audit.

Comparison key (plan 2026-08-05/2): tags are compared via
``normalize.tag_key`` — the OpenRefine fingerprint shared with Tag Hygiene —
NOT raw string equality. Raw equality made every case/accent/NFC/separator
difference a "missing" tag, and a case-only row was unresolvable forever: it
survived the route's ``.lower()`` intersection but no-opped in the writer's
case-insensitive dedup, so it reappeared on every scan. Now a tag present in
the note under ANY surface form is a *variant* (informational, never
applyable), and ``missing_in_obs`` means genuinely absent in every form.

Plan 2026-08-05/3 additions, all route-injected (the vendored tree resolves
nothing app-side itself):

* ``include_parent_tags`` — union the Zotero *item*'s own tags into the
  Zotero side (users commonly tag the item, not the reading note); missing
  tags found ONLY there are flagged in ``item_only_missing`` so the UI can
  badge their provenance.
* ``synonyms`` — a ``{tag_key: canonical tag_key}`` equivalence map (built
  from the vault's curated ``_tags.md``), so an FR/EN synonym pair counts as
  a *variant* (reason ``synonym``), not as missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..inventory import Inventory
from ..normalize import group_by_key


def missing_by_key(
    zotero_tags: set[str],
    obs_tags: set[str],
    *,
    synonyms: dict[str, str] | None = None,
) -> dict[str, str]:
    """``{tag_key: representative Zotero surface}`` for genuinely-missing tags.

    A key present in the note under any surface form is NOT missing. The
    representative surface (what ``apply-tag-drift`` writes) is the sorted-first
    Zotero form. Shared by ``find_drift`` and the route's server-authoritative
    intersection so report and write path can never disagree again.
    """
    z = group_by_key(zotero_tags, synonyms)
    n = group_by_key(obs_tags, synonyms)
    return {k: sorted(forms)[0] for k, forms in z.items() if k not in n}


def variant_pairs(
    zotero_tags: set[str],
    obs_tags: set[str],
    *,
    synonyms: dict[str, str] | None = None,
) -> list[tuple[str, str, str]]:
    """``(zotero surface, note surface, reason)`` pairs present on both sides
    under *different* spellings only — a shared exact spelling means the tag is
    simply present, and nothing is reported. ``reason`` is ``"spelling"`` for a
    fingerprint match and ``"synonym"`` when only the injected synonym map
    unifies the pair."""
    z = group_by_key(zotero_tags, synonyms)
    n = group_by_key(obs_tags, synonyms)
    z_plain = group_by_key(zotero_tags)
    pairs: list[tuple[str, str, str]] = []
    for k in z.keys() & n.keys():
        if z[k] & n[k]:
            continue
        z_surface = sorted(z[k])[0]
        n_surface = sorted(n[k])[0]
        # A pair the plain fingerprint alone would NOT have unified owes its
        # match to the synonym map.
        plain_keys = {pk for pk, forms in z_plain.items() if z_surface in forms}
        n_plain = group_by_key(n[k])
        reason = "spelling" if plain_keys & n_plain.keys() else "synonym"
        pairs.append((z_surface, n_surface, reason))
    pairs.sort()
    return pairs


@dataclass
class NoteTagDriftRow:
    """One citation key whose tags disagree between Zotero and the note.

    ``missing_in_obs`` is the actionable set — Zotero tags absent from the
    note in ANY spelling (one representative Zotero surface per fingerprint).
    ``variants`` is informational: tags present on both sides under different
    spellings (a hygiene signal, never applyable — applying one would no-op).
    ``note_only`` is the read-only reverse direction; ``item_only_missing``
    marks missing tags contributed solely by the Zotero *item* (only populated
    when ``include_parent_tags`` is on). The two full tag sets are carried for
    the UI to show the comparison; ``author``/``title`` are pulled from the
    bib entry purely for display.
    """

    citation_key: str
    zotero_note_tags: set[str] = field(default_factory=set)
    obs_tags: set[str] = field(default_factory=set)
    missing_in_obs: set[str] = field(default_factory=set)
    variants: list[tuple[str, str, str]] = field(default_factory=list)
    note_only: set[str] = field(default_factory=set)
    item_only_missing: set[str] = field(default_factory=set)
    author: str | None = None
    title: str | None = None


def find_drift(
    inv: Inventory,
    *,
    include_parent_tags: bool = False,
    synonyms: dict[str, str] | None = None,
) -> list[NoteTagDriftRow]:
    """Reconciliation rule for aim (i): Zotero tags absent from Obsidian.

    For each inventory record with an Obsidian note and a matched Zotero item
    that carries tags on child notes (or, with ``include_parent_tags``, on the
    item itself), a row is emitted when the note is missing a Zotero tag in
    every spelling (``missing_in_obs``, fingerprint comparison), carries one
    under a different spelling (``variants``), or carries tags the Zotero side
    lacks (``note_only``, read-only). Rows sort most-missing-first, then
    most-variants, then citation key, so actionable gaps surface above
    informational rows.
    """
    rows: list[NoteTagDriftRow] = []
    for rec in inv.records.values():
        # Require both sources to exist. Without parent tags the historical
        # gate stands (>=1 child note); with them, an item that carries its
        # own tags is enough.
        if not rec.zotero_item:
            continue
        if not rec.zotero_item.child_notes and not (
            include_parent_tags and rec.zotero_item.tags
        ):
            continue
        if not rec.obsidian_note:
            continue
        zn = set(rec.zotero_note_tags)
        if include_parent_tags:
            zn |= set(rec.zotero_item.tags)
        on = rec.obs_tags
        missing_map = missing_by_key(zn, on, synonyms=synonyms)
        missing = set(missing_map.values())
        variants = variant_pairs(zn, on, synonyms=synonyms)
        z_keys = group_by_key(zn, synonyms).keys()
        note_only = {
            sorted(forms)[0]
            for k, forms in group_by_key(on, synonyms).items()
            if k not in z_keys
        }
        item_only: set[str] = set()
        if include_parent_tags and missing_map:
            child_keys = group_by_key(rec.zotero_note_tags, synonyms).keys()
            item_only = {
                surface for k, surface in missing_map.items()
                if k not in child_keys
            }
        if not missing and not variants and not note_only:
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
                variants=variants,
                note_only=note_only,
                item_only_missing=item_only,
                author=author_str,
                title=title_str,
            )
        )
    rows.sort(
        key=lambda r: (-len(r.missing_in_obs), -len(r.variants), r.citation_key))
    return rows
