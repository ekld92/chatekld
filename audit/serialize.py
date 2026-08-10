"""Plain-dict conversions for the audit dataclasses.

The engine layer uses ``dataclass`` types with ``Path`` and ``set`` fields
which are not JSON-serialisable as-is. Each helper takes one engine type
and returns the shape the route layer ships to the browser. Paths are
emitted as both a vault-relative ``rel`` and an absolute ``abs`` so the
UI can render the short form and the "reveal in Finder" action can use
the absolute one without re-resolving.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Settings
from .engine import bridge as eng_bridge
from .engine import duplicates as eng_duplicates
from .engine import inventory as eng_inventory
from .engine import near_duplicates as eng_near_duplicates
from .engine.reports import (
    finder_tag_drift as r_finder_tag_drift,
    note_tag_drift as r_note_tag_drift,
    read_unzoterod as r_read_unzoterod,
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
        "zotero_error": inv.zotero_error,
        "bridge_sources": _bridge_sources(inv.bridge),
        "fm_pointer_fields_seen": dict(inv.bridge.fm_pointer_fields_seen),
        "notes_with_fm": inv.bridge.notes_with_fm,
        "confirmed_no_match_count": len(inv.bridge.confirmed_no_match_pdfs),
        # Curated mapping entries whose path no longer resolves (see
        # BridgeResult.mapping_orphans) — non-zero tells the user dead entries
        # linger in mapping.json and the hash-recovery pass runs every scan.
        "mapping_orphans": getattr(inv.bridge, "mapping_orphans", 0),
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
    for rec in inv.records.values():
        bib_entry = rec.bib_entry
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
            "pdf_paths": [_path_payload(p, settings.vault_root) for p in rec.pdf_paths],
            "annotations_count_max": rec.annotations_count_max,
            "finder_tags": sorted(rec.finder_tag_set),
            "match_sources": sorted(rec.match_sources),
            "obs_tags": sorted(rec.obs_tags),
            "zotero_note_tags": sorted(rec.zotero_note_tags),
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
    rep: r_unread_unzoterod.UnreadUnzoterodReport, settings: Settings
) -> dict[str, Any]:
    """Serialize aim-(ii) report: threshold, ambiguous count, per-PDF rows."""
    return {
        "threshold": rep.threshold,
        "ambiguous_count": rep.ambiguous_count,
        "rows": [
            {
                "pdf": _path_payload(r.pdf, settings.vault_root),
                "annotations": r.annotations,
                "error": r.error,
            }
            for r in rep.rows
        ],
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
    rep: r_read_unzoterod.ReadUnzoterodReport, settings: Settings
) -> dict[str, Any]:
    """Serialize aim-(iv) report: cutoff, exclusion counts, annotation-ranked rows."""
    return {
        "suggested_read_cutoff": rep.suggested_read_cutoff,
        # Exclusion disclosure — see ReadUnzoterodReport. Counts only; the UI
        # renders them in the meta line so an omission is never silent.
        "ambiguous_count": rep.ambiguous_count,
        "confirmed_no_match_count": rep.confirmed_no_match_count,
        "skipped_count": rep.skipped_count,
        "rows": [
            {
                "pdf": _path_payload(r.pdf, settings.vault_root),
                "annotations": r.annotations,
                "error": r.error,
            }
            for r in rep.rows
        ],
    }


def report_zotero_no_pdf(
    rep: r_zotero_no_pdf.ZoteroNoPdfReport,
) -> dict[str, Any]:
    """Serialize aim-(v) report: bib entries with no resolved PDF."""
    return {
        "rows": [
            {
                "citation_key": r.citation_key,
                "title": r.title,
                "year": r.year,
                "author": r.author,
                "has_zotero_match": r.has_zotero_match,
            }
            for r in rep.rows
        ],
    }


def report_tag_hygiene(rep: r_tag_hygiene.TagHygieneReport) -> dict[str, Any]:
    """Serialize the tag-hygiene report (frequencies + co-occurrence pairs)."""
    return {
        "vocab_size": rep.vocab_size,
        "sources": list(r_tag_hygiene.SOURCES),
        "rows": [
            {
                "fingerprint": r.fingerprint,
                "variants": r.variants,
                "counts": r.counts,
                "total": r.total,
                "in_vocab": r.in_vocab,
                "vocab_tag": r.vocab_tag,
                "near_vocab": r.near_vocab,
            }
            for r in rep.rows
        ],
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
    return {
        "rows": [
            {
                "kind": c.kind,
                "evidence": c.evidence,
                "pair_id": c.pair_id,
                "paths": [_path_payload(p, settings.vault_root) for p in c.paths],
                "citation_keys": list(c.citation_keys),
                "similarity": c.similarity,
            }
            for c in rep.candidates
        ],
        "no_text_count": rep.no_text_count,
        "dismissed_count": rep.dismissed_count,
        "compared_pairs": rep.compared_pairs,
        "similarity_available": rep.similarity_available,
    }
