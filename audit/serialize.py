"""Plain-dict conversions for the audit dataclasses.

The engine layer uses ``dataclass`` types with ``Path`` and ``set`` fields
which are not JSON-serialisable as-is. Each helper takes one engine type
and returns the shape the route layer ships to the browser. Paths are
emitted as both a vault-relative ``rel`` and an absolute ``abs`` so the
UI can render the short form and the "reveal in Finder" action can use
the absolute one without re-resolving.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .config import Settings
from .engine import bridge as eng_bridge
from .engine.normalize import fold_tag
from .engine import duplicates as eng_duplicates
from .engine import inventory as eng_inventory
from .core import pdf_provenance as _pdf_provenance
from .engine import near_duplicates as eng_near_duplicates
from .engine.reports import (
    dead_curation as r_dead_curation,
    multi_pdf as r_multi_pdf,
    not_in_zotero as r_not_in_zotero,
    finder_tag_drift as r_finder_tag_drift,
    note_tag_drift as r_note_tag_drift,
    read_unzoterod as r_read_unzoterod,
    tag_harmony as r_tag_harmony,
    tag_hygiene as r_tag_hygiene,
    unread_unzoterod as r_unread_unzoterod,
    zotero_no_pdf as r_zotero_no_pdf,
    zotero_unread as r_zotero_unread,
)


def _rel_to_vault(p: Path, vault_root: Path) -> str:
    """Vault-relative string for ``p``, falling back to absolute if outside."""
    try:
        return str(p.relative_to(vault_root))
    except ValueError:
        return str(p)


def _path_payload(p: Path, vault_root: Path) -> dict[str, str]:
    """Render a path as ``{abs, rel, name}`` for the browser.

    The UI shows ``rel``/``name`` but the "reveal in Finder" action needs the
    ``abs`` form, so both are emitted to spare the front end any re-resolution.
    """
    return {
        "abs": str(p),
        "rel": _rel_to_vault(p, vault_root),
        "name": p.name,
    }


def report_ambiguous_pdfs(
    inv: eng_inventory.Inventory, settings: Settings
) -> dict[str, Any]:
    """List the PDFs the bridge parked as ambiguous (author+year hit >1 key).

    Each row carries the PDF path plus its candidate citation keys (with the
    bib title when available) so the UI can let the user pick one — a pick
    POSTs to ``/api/audit/mapping`` and becomes a manual override. Read-only.
    """
    vault = settings.vault_root
    bib_titles = {
        key: (rec.bib_entry.title if rec.bib_entry else "")
        for key, rec in inv.records.items()
    }
    rows = []
    for pdf, keys in sorted(inv.bridge.ambiguous_pdfs.items(), key=lambda kv: str(kv[0])):
        rows.append({
            "pdf": _path_payload(pdf, vault),
            "candidates": [
                {"citation_key": k, "title": bib_titles.get(k, "")} for k in keys
            ],
        })
    return {"rows": rows, "count": len(rows)}


def report_skipped_pdfs(
    inv: eng_inventory.Inventory, settings: Settings
) -> dict[str, Any]:
    """List the PDFs skipped by the ``biblio_skip_prefix`` (e.g. ``z_item*``).

    These are excluded from every report except duplicates; surfacing them as a
    list (not just a count) lets the user confirm nothing important is hidden by
    the skip convention. Read-only.
    """
    vault = settings.vault_root
    rows = [
        {"pdf": _path_payload(pdf, vault)}
        for pdf in sorted(inv.pdfs_skipped, key=str)
    ]
    return {"rows": rows, "count": len(rows), "skip_prefix": settings.biblio_skip_prefix}


def inventory_summary(inv: eng_inventory.Inventory) -> dict[str, Any]:
    """Serialize the headline inventory counters for the summary panel.

    Computes the triangulation tallies (bib+PDF, bib+note, bib+Zotero, fully
    triangulated, …) and unmapped/ambiguous/skipped PDF counts on the fly from
    ``inv.records``/``inv.bridge``. ``zotero_error`` is passed straight through
    so the UI can warn when the Zotero read failed.
    """
    records = inv.records
    return {
        "record_count": len(records),
        "bib_with_pdf": sum(
            1 for r in records.values() if r.bib_entry and r.pdf_paths
        ),
        "bib_with_obsidian_note": sum(
            1 for r in records.values() if r.bib_entry and r.obsidian_note
        ),
        "bib_with_zotero_parent": sum(
            1 for r in records.values() if r.bib_entry and r.zotero_item
        ),
        "fully_triangulated": sum(
            1
            for r in records.values()
            if r.bib_entry and r.pdf_paths and r.obsidian_note and r.zotero_item
        ),
        "zotero_parents_with_child_note": sum(
            1 for r in records.values() if r.zotero_item and r.zotero_item.child_notes
        ),
        "pdfs_unmapped": len(inv.bridge.unmapped_pdfs),
        "pdfs_ambiguous": len(inv.bridge.ambiguous_pdfs),
        "pdfs_skipped": len(inv.pdfs_skipped),
        "zotero_matched_by_title": inv.zotero_matched_by_title,
        # Additive (2026-08-12): the primary BBT-citation-key join's hit count.
        # `zotero_matched_by_title` is now the FALLBACK count — read together,
        # a rising title share means BBT keys are going missing. Both count
        # RECORDS (the title figure counted distinct titles until the same day,
        # so the "share" compared different denominators).
        "zotero_matched_by_key": inv.zotero_matched_by_key,
        # Records whose key missed and whose title fallback was REFUSED as
        # ambiguous or already-claimed. A non-zero value is the fallback
        # declining to guess, not an error — each one used to be an arbitrary
        # join that could put tags on the wrong paper.
        "zotero_title_refused": inv.zotero_title_refused,
        "zotero_error": inv.zotero_error,
        "bridge_sources": _bridge_sources(inv.bridge),
        "fm_pointer_fields_seen": dict(inv.bridge.fm_pointer_fields_seen),
        "notes_with_fm": inv.bridge.notes_with_fm,
        "confirmed_no_match_count": len(inv.bridge.confirmed_no_match_pdfs),
        # The second curated state (2026-09-06): "add to Zotero later".
        "pending_count": len(getattr(inv.bridge, "pending_pdfs", None) or []),
        "exclusions_applied": getattr(inv.bridge, "exclusions_applied", 0),
        # Curated mapping entries whose path no longer resolves (see
        # BridgeResult.mapping_orphans) — non-zero tells the user dead entries
        # linger in mapping.json and the hash-recovery pass runs every scan.
        "mapping_orphans": getattr(inv.bridge, "mapping_orphans", 0),
        # Orphan buckets (2026-08-13) — counts here, rows in their own reports.
        "notes_without_record": len(getattr(inv, "notes_without_record", ())),
        "zotero_items_without_record": len(
            getattr(inv, "zotero_items_without_record", ())
        ),
        "tag_coverage": tag_coverage(inv),
        # Additive (2026-09-19): files Zotero links that the pairing rung
        # could not claim (unexported key / two parents) and relative
        # ``attachments:`` links the index skipped.
        "zotero_unexported": len(
            getattr(inv.bridge, "zotero_unexported", None) or {}
        ),
        "zotero_multi_links": len(
            getattr(inv.bridge, "zotero_multi_links", None) or {}
        ),
        "zotero_relative_links": int(
            getattr(inv, "zotero_relative_links", 0) or 0
        ),
    }


#: Tag-count buckets, in display order. Strings because they are JSON keys and
#: axis labels, not arithmetic. The split at 2 is where the product question
#: sits: "0" is untagged, "1"/"2" is under-tagged (a paper the classifier saw
#: but barely described), "3-4" and "5+" are done.
TAG_DEPTH_BUCKETS = ("0", "1", "2", "3-4", "5+")


def _depth_bucket(n: int) -> str:
    """Bucket a tag count into :data:`TAG_DEPTH_BUCKETS`."""
    if n <= 2:
        return str(n)
    return "3-4" if n <= 4 else "5+"


def tag_coverage(inv: eng_inventory.Inventory) -> dict[str, Any]:
    """Per-surface tag-presence counts over the cached records (2026-08-13).

    Answers "how many papers carry a tag, and where?" — which no report, count
    or CLI check could answer before this existed. Every input is already on
    the joined ``Record``, so this is a pure pass over ``inv.records``: no disk
    reads, no scan change.

    Each of the three WRITABLE surfaces reports ``*_present`` beside
    ``*_tagged``. The denominator is the point: a record with no reference note
    cannot have note tags, so "1 200 note-tagged" means nothing until you know
    whether 1 300 or 6 000 records have a note at all. ``bib``/``zotero_note``
    are read-only surfaces, counted for completeness but never writable.

    Two honesty caveats, carried as fields so a client cannot render the
    numbers without them:

    * ``finder_is_floor`` — Finder counts are a FLOOR, not a total. A tagged
      PDF the bridge never mapped has no record at all, so it is invisible
      here (the Tag Hygiene under-reporting lesson of 2026-08-11, where a
      floor presented as a total stood for months).
    * ``finder_platform`` — Finder xattrs read empty off macOS, so on any
      other platform ``finder_tagged`` is 0 for reasons that have nothing to
      do with the library.

    ``tag_depth`` (2026-08-26) buckets the same pass by COUNT rather than by
    presence. Everything upstream of it treated tags as a boolean — a paper
    with one tag was indistinguishable from a paper with nine — so "re-propose
    the under-tagged ones" had no selector and the CLI's ``--include-tagged``
    was forced to demand an explicit key file. Free: the counts were already
    in hand on the same records.

    Deliberately ABSENT: a "needs harmonizing" count. Deciding whether two
    surfaces disagree requires the OpenRefine fingerprint plus the vault's
    synonym map; a cheap set-difference here would disagree with the
    tag_harmony report over every case/accent/separator variant and reproduce
    the exact drift class that made a case-only row unresolvable forever. That
    number belongs to the report that computes it properly.
    """
    records = inv.records.values()
    note_present = note_tagged = 0
    finder_present = finder_tagged = 0
    zotero_present = zotero_tagged = 0
    any_tagged = writable_any = 0
    bib_tagged = zotero_note_tagged = 0
    depth = {surface: dict.fromkeys(TAG_DEPTH_BUCKETS, 0)
             for surface in ("note", "finder", "zotero", "union")}
    for r in records:
        has_note = r.obsidian_note is not None
        has_pdf = bool(r.pdf_paths)
        has_item = r.zotero_item is not None
        # Folded for the DEPTH buckets (see `fold_tag`): a raw set union
        # counted `Dep` and `dep` as two tags, so the dashboard bucket could
        # disagree with the table's `#` cell and with `--max-tags N` — the one
        # invariant this histogram exists to serve. Presence is unaffected.
        note_tags = {fold_tag(t) for t in r.obs_tags}
        finder_tags = {fold_tag(t) for t in r.finder_tag_set}
        item_tags = ({fold_tag(t) for t in r.zotero_item.tags}
                     if r.zotero_item is not None else set())
        t_note = has_note and bool(note_tags)
        t_finder = has_pdf and bool(finder_tags)
        t_item = has_item and bool(item_tags)
        note_present += has_note
        note_tagged += t_note
        finder_present += has_pdf
        finder_tagged += t_finder
        zotero_present += has_item
        zotero_tagged += t_item
        bib_tagged += bool(r.bib_keywords)
        zotero_note_tagged += bool(r.zotero_note_tags)
        # Depth, not just presence (2026-08-26). Bucketed per surface over the
        # records that HAVE that surface — a record with no reference note is
        # not a zero-tag note, it has no note — and once over the union, which
        # is what a "papers with at most N tags" cohort selection acts on.
        if has_note:
            depth["note"][_depth_bucket(len(note_tags))] += 1
        if has_pdf:
            depth["finder"][_depth_bucket(len(finder_tags))] += 1
        if has_item:
            depth["zotero"][_depth_bucket(len(item_tags))] += 1
        if has_note or has_pdf or has_item:
            writable_any += 1
            depth["union"][
                _depth_bucket(len(note_tags | finder_tags | item_tags))] += 1
            if t_note or t_finder or t_item:
                any_tagged += 1
    return {
        "tag_depth": depth,
        "records_total": len(inv.records),
        # Records carrying at least one surface a tag could be written to —
        # the denominator for `any_tagged` / `none_tagged`. A record with none
        # of the three (bib row only) is not "untagged", it is unwritable.
        "writable_records": writable_any,
        "note_present": note_present,
        "note_tagged": note_tagged,
        "finder_present": finder_present,
        "finder_tagged": finder_tagged,
        "zotero_present": zotero_present,
        "zotero_tagged": zotero_tagged,
        "any_tagged": any_tagged,
        "none_tagged": writable_any - any_tagged,
        # Read-only surfaces, for completeness — never write targets.
        "bib_keywords_tagged": bib_tagged,
        "zotero_note_tagged": zotero_note_tagged,
        "finder_is_floor": True,
        "finder_platform": sys.platform,
    }


def orphan_notes(
    inv: eng_inventory.Inventory, settings: Settings
) -> dict[str, Any]:
    """Reference notes whose stem matched no record — rows for the orphan tab.

    Vault-relative where possible so the row names something the user can go
    and open; the absolute path is kept as a fallback for a note outside the
    vault root (which the walk cannot produce today, but the serializer must
    not raise if it ever does).
    """
    rows = [
        _path_payload(p, settings.vault_root)
        for p in sorted(getattr(inv, "notes_without_record", ()), key=str)
    ]
    return {"rows": rows, "count": len(rows)}


def orphan_zotero_items(inv: eng_inventory.Inventory) -> dict[str, Any]:
    """Zotero parents no record claimed — rows for the orphan tab.

    ``tagged`` counts those carrying at least one tag: an orphan item's tags
    are the ones no drift report can ever see, since every report iterates
    records.
    """
    items = sorted(
        getattr(inv, "zotero_items_without_record", ()),
        key=lambda o: ((o.title or "").lower(), o.item_key),
    )
    rows = [
        {
            "item_key": o.item_key,
            "item_type": o.item_type,
            "title": o.title,
            "citation_key": o.citation_key,
            "tags": sorted(o.tags),
            # Additive (2026-08-18): the [Zotero] deep link needs the group
            # routing — zotero://select/groups/<id>/items/<key> — or it
            # silently selects nothing for a group-library orphan.
            "library_type": getattr(o, "library_type", None),
            "group_id": getattr(o, "group_id", None),
        }
        for o in items
    ]
    return {
        "rows": rows,
        "count": len(rows),
        "tagged": sum(1 for r in rows if r["tags"]),
    }


def _bridge_sources(b: eng_bridge.BridgeResult) -> dict[str, int]:
    """Histogram of how PDFs were matched: ``{source_label: count}``."""
    counts: dict[str, int] = {}
    for src in b.source_per_pdf.values():
        counts[src] = counts.get(src, 0) + 1
    return counts


def inventory_records(
    inv: eng_inventory.Inventory, settings: Settings
) -> list[dict[str, Any]]:
    """Serialize every per-key record into the inventory-table row shape.

    Flattens each :class:`Record` (bib metadata, the four "has X" booleans,
    PDF payloads, aggregated Finder/obs/Zotero tags and bib keywords) into a
    JSON-able dict. Sets are emitted ``sorted`` for stable output. Rows are
    ordered by year then citation key to match the table's default sort.
    """
    out: list[dict[str, Any]] = []
    # Per-PDF match provenance (2026-08-19 review, pairings board): the bridge
    # has always recorded HOW each PDF was tied to its key
    # (manual / manual_hash / fm_pointer / fm_authoryear / wikilink / doi /
    # authoryear / doi_text), but only the per-record UNION was serialized —
    # so "which rung matched THIS file" was invisible per pair.
    src_map = getattr(getattr(inv, "bridge", None), "source_per_pdf", None) or {}
    for rec in inv.records.values():
        bib_entry = rec.bib_entry
        zot = rec.zotero_item
        note = rec.obsidian_note
        out.append({
            "citation_key": rec.citation_key,
            "title": bib_entry.title if bib_entry else None,
            "year": bib_entry.year if bib_entry else None,
            "first_author": (
                bib_entry.authors[0] if bib_entry and bib_entry.authors else None
            ),
            "entry_type": bib_entry.entry_type if bib_entry else None,
            "has_bib_entry": bib_entry is not None,
            "has_zotero_item": rec.zotero_item is not None,
            "has_obsidian_note": rec.obsidian_note is not None,
            "has_zotero_child_note": rec.has_zotero_child_note,
            "pdf_count": len(rec.pdf_paths),
            "pdf_paths": [
                {**_path_payload(p, settings.vault_root),
                 "source": src_map.get(p)}
                for p in rec.pdf_paths
            ],
            # The pair's other two sides, for the row's detail drawer
            # (additive 2026-08-19). The note payload reuses _path_payload so
            # [open]/[reveal] need no re-resolution; the Zotero block carries
            # what the zotero://select deep link needs (group routing incl. —
            # the orphan-items lesson) plus the reading-note count. The
            # abstract is deliberately NOT here: ~6k records × abstract is
            # real payload weight for a table that never renders it.
            "note": (_path_payload(note.path, settings.vault_root)
                     if note else None),
            "zotero": ({
                "item_key": zot.item_key,
                "item_type": zot.item_type,
                "library_type": getattr(zot, "library_type", None),
                "group_id": getattr(zot, "group_id", None),
                "child_note_count": len(zot.child_notes),
            } if zot else None),
            "annotations_count_max": rec.annotations_count_max,
            "finder_tags": sorted(rec.finder_tag_set),
            "match_sources": sorted(rec.match_sources),
            "obs_tags": sorted(rec.obs_tags),
            "zotero_note_tags": sorted(rec.zotero_note_tags),
            # The Zotero PARENT item's own tags (2026-08-13). The four sibling
            # arrays shipped from the start; this one — the surface
            # apply-zotero-tags actually writes — was the single omission, so
            # the audit's own inventory could not show what a Zotero write
            # would union into.
            "zotero_parent_tags": (
                sorted(rec.zotero_item.tags) if rec.zotero_item else []
            ),
            "bib_keywords": sorted(rec.bib_keywords),
        })
    out.sort(key=lambda r: (r["year"] or "", r["citation_key"]))
    return out


def report_note_tag_drift(
    rows: list[r_note_tag_drift.NoteTagDriftRow],
) -> list[dict[str, Any]]:
    """Serialize aim-(i) drift rows; the three tag sets emitted ``sorted``.

    Additive fields (2026-08-05): ``variants`` — present-but-differently-
    spelled pairs (reason ``spelling`` | ``synonym``), informational, the UI
    never offers Apply for them; ``note_only`` — the read-only reverse
    direction; ``item_only_missing`` — missing tags contributed solely by the
    Zotero item (parent-tags mode).
    """
    return [
        {
            "citation_key": r.citation_key,
            "author": r.author,
            "title": r.title,
            "zotero_note_tags": sorted(r.zotero_note_tags),
            "obs_tags": sorted(r.obs_tags),
            "missing_in_obs": sorted(r.missing_in_obs),
            "variants": [
                {"zotero": z, "note": n, "reason": reason}
                for z, n, reason in r.variants
            ],
            "note_only": sorted(r.note_only),
            "item_only_missing": sorted(r.item_only_missing),
        }
        for r in rows
    ]


def report_tag_harmony(
    rows: list[r_tag_harmony.TagHarmonyRow],
    settings: Settings,
    *,
    vocab_id_for: Any = None,
) -> dict[str, Any]:
    """Serialize the three-way writable-surface comparison (2026-08-13).

    ``vocab_ids`` follows the ``finder_tag_drift`` contract exactly: per union
    surface, the exact ``_tags.md`` id whose canonical key matches, or
    ``null``. It is not a convenience here but the correctness bridge — the
    report compares by OpenRefine fingerprint while the tagging writers gate
    on the LITERAL vocabulary id under ``nfc().lower()``, which folds neither
    accents nor separators. A client that POSTed the raw drift surface would
    have every accented or hyphenated tag silently rejected as out-of-vocabulary
    while the row reappeared at every scan.

    Under the decision that out-of-vocabulary tags are skipped and reported
    (never auto-added), a ``null`` here is what the UI badges as unapplyable,
    and ``applyable_count`` states up front how much of the row survives that
    filter — so "12 tags to propagate" never turns out to mean 3 at write time.

    The envelope carries counts because this report is what a bulk selection
    is built from and every one of its numbers ends up in a confirm dialog.
    """
    def _vocab_map(r: r_tag_harmony.TagHarmonyRow) -> dict[str, Any]:
        if vocab_id_for is None:
            return {}
        return {surface: vocab_id_for(surface) for surface in sorted(r.union)}

    out_rows: list[dict[str, Any]] = []
    total_writes = 0
    total_applyable = 0
    single_surface = 0
    # A write is applyable only if its surface resolves to a real vocabulary
    # id. With no resolver at all (vocab file unavailable) the report degrades
    # to read-only, so nothing is applyable.
    def _applyable(surfaces: set[str], vocab_ids: dict[str, Any]) -> int:
        if not vocab_ids:
            return 0
        return sum(1 for s in surfaces if vocab_ids.get(s))

    for r in rows:
        vocab_ids = _vocab_map(r)
        applyable = (_applyable(r.missing_in_note, vocab_ids)
                     + _applyable(r.missing_in_finder, vocab_ids)
                     + _applyable(r.missing_in_zotero, vocab_ids))
        total_writes += r.write_count
        total_applyable += applyable
        if r.tagged_surface_count == 1:
            single_surface += 1
        out_rows.append({
            "citation_key": r.citation_key,
            "author": r.author,
            "title": r.title,
            "present": sorted(r.present),
            "note_tags": sorted(r.note_tags),
            "finder_tags": sorted(r.finder_tags),
            "zotero_tags": sorted(r.zotero_tags),
            "union": sorted(r.union),
            "missing_in_note": sorted(r.missing_in_note),
            "missing_in_finder": sorted(r.missing_in_finder),
            "missing_in_zotero": sorted(r.missing_in_zotero),
            "variants": r.variants,
            "vocab_ids": vocab_ids,
            "write_count": r.write_count,
            "applyable_count": applyable,
            "tagged_surface_count": r.tagged_surface_count,
            "note_rel": (
                _rel_to_vault(r.note_path, settings.vault_root)
                if r.note_path else None
            ),
            "pdf_count": r.pdf_count,
        })
    return {
        "rows": out_rows,
        "count": len(out_rows),
        "write_count": total_writes,
        "applyable_count": total_applyable,
        "single_surface_count": single_surface,
        "vocab_available": vocab_id_for is not None,
        # Finder xattrs read empty off macOS, so on any other platform every
        # union tag looks "missing in Finder" — fabricated drift, not real.
        # Same disclosure as tag_coverage's finder_platform; the renderer
        # must caveat the → Finder column when this is not "darwin".
        "finder_platform": sys.platform,
    }


def report_finder_tag_drift(
    rows: list[r_finder_tag_drift.FinderTagDriftRow],
    settings: Settings,
    *,
    vocab_id_for: Any = None,
) -> dict[str, Any]:
    """Serialize the per-paper Finder-vs-note tag comparison (2026-08-05).

    Additive fields (drift-fix, 2026-08-06): ``variants`` — present-both-sides
    spelling near-misses (informational, never applyable); ``note_rel`` — the
    write target the UI names in its confirm copy; ``vocab_ids`` — per drift
    surface (``finder_only`` ∪ ``note_only``) the exact ``_tags.md`` id whose
    fingerprint matches, or ``null``. The tagging writers gate on the literal
    vocabulary id (their ``nfc().lower()`` key folds neither accents nor
    separators), so the UI must POST the mapped id, never the drift surface —
    that mapping is what closes an applied row at the next scan instead of
    re-offering it forever. ``vocab_id_for`` is a route-injected callable
    (vendored-tree dependency rule: no app import here); ``None`` — vocab
    unavailable — emits ``{}`` and the UI degrades to read-only rendering.
    """
    def _vocab_map(r: r_finder_tag_drift.FinderTagDriftRow) -> dict[str, Any]:
        if vocab_id_for is None:
            return {}
        return {
            surface: vocab_id_for(surface)
            for surface in sorted(r.finder_only | r.note_only)
        }

    return {
        "rows": [
            {
                "citation_key": r.citation_key,
                "author": r.author,
                "title": r.title,
                "finder_tags": sorted(r.finder_tags),
                "obs_tags": sorted(r.obs_tags),
                "finder_only": sorted(r.finder_only),
                "note_only": sorted(r.note_only),
                "variants": [
                    {"finder": f, "note": n, "reason": reason}
                    for f, n, reason in r.variants
                ],
                "note_rel": (
                    _rel_to_vault(r.note_path, settings.vault_root)
                    if r.note_path else None
                ),
                "vocab_ids": _vocab_map(r),
                "pdf_count": r.pdf_count,
            }
            for r in rows
        ],
    }


def report_unread_unzoterod(
    rep: r_unread_unzoterod.UnreadUnzoterodReport, settings: Settings,
    provenance: dict | None = None,
) -> dict[str, Any]:
    """Serialize aim-(ii) report: threshold, ambiguous count, per-PDF rows.

    Takes ``provenance`` for the same reason its superset does: this report is
    a FILTER over the same unmapped PDFs (annotations < threshold), so a class
    badge that appeared on one tab and not the other would read as a
    difference in the data rather than in the filter.
    """
    prov = provenance or {}
    return {
        **({"error": rep.error} if rep.error else {}),
        "threshold": rep.threshold,
        "ambiguous_count": rep.ambiguous_count,
        # Same exclusion as the superset, or the two tabs disagree about what
        # "without Zotero" means (2026-09-20).
        "zotero_unclaimed_count": rep.zotero_unclaimed_count,
        "rows": [
            {
                "pdf": _path_payload(r.pdf, settings.vault_root),
                "annotations": r.annotations,
                "error": r.error,
                **_provenance_payload(prov.get(r.pdf)),
            }
            for r in rep.rows
        ],
        "provenance_classes": list(_pdf_provenance.PROVENANCE_CLASSES),
    }


def _provenance_payload(entry: dict | None) -> dict[str, Any]:
    """Flatten one provenance memo onto a row, or nothing when absent.

    Absent is normal, not an error: a scan cancelled during the pass, or a
    corrupt file the classifier declined, simply has no class. The UI must
    render such a row as it always did rather than as a new empty category.
    """
    if not isinstance(entry, dict):
        return {}
    return {
        "klass": entry.get("klass"),
        "pages": entry.get("pages"),
        "producer": entry.get("producer"),
        "source_url": entry.get("source_url"),
    }


def report_zotero_unread(
    rep: r_zotero_unread.ZoteroUnreadReport,
) -> dict[str, Any]:
    """Serialize aim-(iii) report: skip count plus the no-child-note rows."""
    return {
        "skipped_no_zotero_match": rep.skipped_no_zotero_match,
        "rows": [
            {
                "citation_key": r.citation_key,
                "title": r.title,
                "year": r.year,
                "author": r.author,
            }
            for r in rep.rows
        ],
    }


def report_read_unzoterod(
    rep: r_read_unzoterod.ReadUnzoterodReport, settings: Settings,
    provenance: dict | None = None,
) -> dict[str, Any]:
    """Serialize aim-(iv) report: cutoff, exclusion counts, annotation-ranked rows.

    ``provenance`` (Batch C, 2026-08-26) is the bridge's per-residual-PDF
    physical class — see ``core/pdf_provenance.py``. Additive, and what lets
    the UI stop offering a DOI search to a printed web page: the ladder is
    built for journal articles and the residual mostly is not one.
    """
    prov = provenance or {}
    return {
        **({"error": rep.error} if rep.error else {}),
        "suggested_read_cutoff": rep.suggested_read_cutoff,
        # Exclusion disclosure — see ReadUnzoterodReport. Counts only; the UI
        # renders them in the meta line so an omission is never silent.
        "ambiguous_count": rep.ambiguous_count,
        "confirmed_no_match_count": rep.confirmed_no_match_count,
        "pending_count": rep.pending_count,
        "skipped_count": rep.skipped_count,
        # Zotero links it, the rung could not claim it (2026-09-20).
        "zotero_unclaimed_count": rep.zotero_unclaimed_count,
        "rows": [
            {
                "pdf": _path_payload(r.pdf, settings.vault_root),
                "annotations": r.annotations,
                "error": r.error,
                **_provenance_payload(prov.get(r.pdf)),
            }
            for r in rep.rows
        ],
        "provenance_classes": list(_pdf_provenance.PROVENANCE_CLASSES),
    }


def report_zotero_no_pdf(
    rep: r_zotero_no_pdf.ZoteroNoPdfReport,
    never_pdf_types: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Serialize aim-(v) report: bib entries with no resolved PDF.

    ``item_type``/``dismissed_count``/``by_item_type`` are additive
    (2026-08-26): the type is what a bulk "these never have a PDF" dismissal
    selects on, and the count is the disclosure that keeps a filtered list from
    reading as the whole truth.
    """
    return {
        "rows": [
            {
                "citation_key": r.citation_key,
                "title": r.title,
                "year": r.year,
                "author": r.author,
                "has_zotero_match": r.has_zotero_match,
                "item_type": r.item_type,
            }
            for r in rep.rows
        ],
        "dismissed_count": rep.dismissed_count,
        "by_item_type": rep.by_item_type,
        # Which item types the user considers to never need a local PDF. Route-
        # injected from the configurable `audit_no_pdf_types` key so the client
        # renders the bulk-dismiss offer from ONE source of truth rather than a
        # hard-coded list that would drift from Settings.
        "never_pdf_types": list(never_pdf_types),
    }


def _hygiene_row(r: r_tag_hygiene.TagHygieneRow) -> dict[str, Any]:
    return {
        "fingerprint": r.fingerprint,
        "variants": r.variants,
        "counts": r.counts,
        "total": r.total,
        "in_vocab": r.in_vocab,
        "vocab_tag": r.vocab_tag,
        "near_vocab": r.near_vocab,
    }


def report_tag_hygiene(rep: r_tag_hygiene.TagHygieneReport) -> dict[str, Any]:
    """Serialize the tag-hygiene report (frequencies + co-occurrence pairs).

    ``unmapped_*`` is the separate bucket for Finder tags on biblio PDFs with
    no citation key — additive, so a client that ignores it sees the historical
    payload. Kept out of ``rows`` on purpose; see ``tag_hygiene._unmapped_finder_rows``.
    """
    return {
        "vocab_size": rep.vocab_size,
        "sources": list(r_tag_hygiene.SOURCES),
        "rows": [_hygiene_row(r) for r in rep.rows],
        "unmapped_rows": [_hygiene_row(r) for r in rep.unmapped_rows],
        "unmapped_pdf_count": rep.unmapped_pdf_count,
        "unmapped_tagged_count": rep.unmapped_tagged_count,
        "cooccurrence": rep.cooccurrence,
    }


def report_duplicates(
    sets: list[eng_duplicates.DuplicateSet], settings: Settings
) -> dict[str, Any]:
    """Serialize duplicate sets, with a precomputed total reclaimable bytes.

    ``suggested_keep`` (additive, plan 2026-08-05/4) is the set member whose
    FILENAME scores cleanest per ``core.naming`` — read-only advice for "which
    copy would a user keep"; no delete action exists and none is implied.
    """
    from .core import naming as core_naming

    rows = []
    for d in sets:
        suggested = None
        try:
            best = core_naming.get_cleanest_name([p.name for p in d.paths])
            best_path = next(p for p in d.paths if p.name == best)
            suggested = _path_payload(best_path, settings.vault_root)["rel"]
        except (ValueError, StopIteration):
            pass
        rows.append({
            "content_hash": d.content_hash,
            "size_bytes": d.size_bytes,
            "wasted_bytes": d.wasted_bytes,
            "paths": [_path_payload(p, settings.vault_root) for p in d.paths],
            "suggested_keep": suggested,
        })
    return {
        "rows": rows,
        "total_wasted_bytes": sum(d.wasted_bytes for d in sets),
    }


def report_near_duplicates(
    rep: eng_near_duplicates.NearDupReport, settings: Settings
) -> dict[str, Any]:
    """Serialize the near-duplicate candidate report (plan 2026-08-05/5).

    Meta counters travel with the rows so the UI can disclose what was NOT
    covered (no-text PDFs, dismissed groups) — no silent caps, house style.
    """
    def _row(c: eng_near_duplicates.NearDupCandidate) -> dict[str, Any]:
        return {
            "kind": c.kind,
            "evidence": c.evidence,
            "pair_id": c.pair_id,
            "paths": [_path_payload(p, settings.vault_root) for p in c.paths],
            "citation_keys": list(c.citation_keys),
            "similarity": c.similarity,
        }

    return {
        "rows": [_row(c) for c in rep.candidates],
        # Additive (2026-09-17): the rows behind the two counts, so a
        # dismissal can be re-verified and a never-compared file be read.
        # The UI ignores both; the annual cleanup harness consumes them.
        "dismissed_rows": [_row(c) for c in rep.dismissed_candidates],
        "no_text_paths": [_path_payload(p, settings.vault_root) for p in rep.no_text_paths],
        "no_text_count": rep.no_text_count,
        "dismissed_count": rep.dismissed_count,
        "compared_pairs": rep.compared_pairs,
        "similarity_available": rep.similarity_available,
    }


def zotero_attachments(inv: eng_inventory.Inventory) -> dict[str, Any]:
    """Every file Zotero holds against an item — rows for the de-link tab.

    Read-only, like every serializer here: the vendored ``audit/`` tree never
    writes, and the de-link verb lives in ``api/routes/zotlink.py`` outside it.

    ``owned`` counts the attachments whose BYTES live in Zotero's own data
    directory (``imported_*``/``embedded_image``). That is the number that
    decides how a removal has to behave — a linked file is only a pointer, but
    an imported one goes to the trash with its item — so it is computed once
    here rather than by each consumer.

    ``exists`` (2026-09-07) answers whether a LINKED file's pointer still
    resolves; ``missing`` counts the ones that do not. A linked attachment is
    only a path, so it can be silently orphaned by anything that renames or
    moves the file — which is what Zotero's own rename preference and the
    Attanger plugin each did on 2026-09-06/07, nine times between them,
    before either was gated. It is ``None`` for an imported attachment, whose
    bytes Zotero owns and whose location is not the user's business. One
    ``stat`` per linked row: ~5 200 of them cost single-digit milliseconds,
    and the alternative is a report that shows a healthy library while a
    reference is dead.
    """
    def _exists(a) -> bool | None:
        if a.link_mode != "linked_file" or not a.raw_path:
            return None
        try:
            return os.path.isfile(a.raw_path)
        except OSError:
            return False

    rows = [
        {
            "item_key": a.item_key,
            "parent_key": a.parent_key,
            "parent_title": a.parent_title,
            "citation_key": a.parent_citation_key,
            "parent_item_type": a.parent_item_type,
            "link_mode": a.link_mode,
            "content_type": a.content_type,
            "filename": a.filename,
            "title": a.title,
            "path": a.raw_path,
            "annotation_count": a.annotation_count,
            "file_owned_by_zotero": a.file_owned_by_zotero,
            "library_type": a.library_type,
            "group_id": a.group_id,
            "exists": _exists(a),
        }
        for a in sorted(
            getattr(inv, "zotero_attachments", ()),
            key=lambda a: ((a.parent_citation_key or "").lower(),
                           a.filename.lower(), a.item_key),
        )
    ]
    return {
        "rows": rows,
        "count": len(rows),
        "owned": sum(1 for r in rows if r["file_owned_by_zotero"]),
        "with_annotations": sum(1 for r in rows if r["annotation_count"]),
        "pdfs": sum(1 for r in rows if (r["content_type"] or "") == "application/pdf"),
        "linked": sum(1 for r in rows if r["exists"] is not None),
        "missing": sum(1 for r in rows if r["exists"] is False),
    }


# --- 2026-09-06 harmonization reports (read-only) ------------------------


def report_multi_pdf(
    rep: r_multi_pdf.MultiPdfReport, settings: Settings
) -> dict[str, Any]:
    """Serialize *Entries With Several PDFs*: one row per record, one
    ``files`` entry per PDF with the rung that claimed it."""
    vault = settings.vault_root
    return {
        "rows": [
            {
                "citation_key": r.citation_key,
                "title": r.title,
                "year": r.year,
                "author": r.author,
                "files": [
                    {**_path_payload(f.path, vault), "source": f.source}
                    for f in r.files
                ],
                "mixed": r.mixed,
                "all_filename": r.all_filename,
                "dismissed": r.dismissed,
                "pair_id": r.pair_id,
            }
            for r in rep.rows
        ],
        "count": len(rep.rows),
        "file_count": rep.file_count,
        "mixed_count": rep.mixed_count,
        "all_filename_count": rep.all_filename_count,
        "dismissed_count": rep.dismissed_count,
    }


def report_not_in_zotero(
    rep: r_not_in_zotero.NotInZoteroReport,
    settings: Settings,
    *,
    resolution_for: Any = None,
) -> dict[str, Any]:
    """Serialize *Not in Zotero*: the curated ``no_match`` PDFs.

    ``resolution_for`` is the app-side accession join — ``callable(path) ->
    dict | None`` — injected by the route (the ``vocab_id_for`` pattern), so
    this vendored module never imports the resolver. Each row's
    ``resolution`` is that dict verbatim or ``None``; ``resolved_count`` /
    ``with_doi_count`` are derived here so the meta line and the rows can
    never disagree.
    """
    vault = settings.vault_root
    rows = []
    resolved = with_doi = 0
    for r in rep.rows:
        res = None
        if resolution_for is not None and r.exists:
            try:
                res = resolution_for(r.pdf)
            except Exception:  # noqa: BLE001 — advisory enrichment
                res = None
        if res is not None:
            resolved += 1
            if res.get("doi") or any(
                c.get("doi") for c in (res.get("candidates") or [])
                if isinstance(c, dict)
            ):
                with_doi += 1
        rows.append({
            "pdf": _path_payload(r.pdf, vault),
            "exists": r.exists,
            "state": r.state,
            "resolution": res,
        })
    return {
        "rows": rows,
        "count": len(rows),
        "missing_count": rep.missing_count,
        "no_match_count": rep.no_match_count,
        "pending_count": rep.pending_count,
        # Zotero links it, the rung could not claim it (2026-09-20).
        "zotero_unclaimed_count": rep.zotero_unclaimed_count,
        "resolved_count": resolved,
        "with_doi_count": with_doi,
    }


def report_dead_curation(
    rep: r_dead_curation.DeadCurationReport, settings: Settings
) -> dict[str, Any]:
    """Serialize *Dead Curation*: mapping.json entries pointing at nothing."""
    vault = settings.vault_root
    rows = []
    for r in rep.rows:
        p = vault / r.rel
        rows.append({
            "kind": r.kind,
            "pdf": _path_payload(p, vault),
            "citation_key": r.citation_key,
            "zotero_key": r.zotero_key,
            "curation": r.curation,
        })
    by_kind: dict[str, int] = {}
    for row in rows:
        kind = str(row["kind"])
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {
        "rows": rows,
        "count": len(rows),
        "by_kind": by_kind,
        "mapping_orphans": rep.mapping_orphans,
        "recovered_count": rep.recovered_count,
        "curated_matches": rep.curated_matches,
        "curated_no_match": rep.curated_no_match,
        "curated_pending": rep.curated_pending,
    }
