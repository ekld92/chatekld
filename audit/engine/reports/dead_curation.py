"""Dead curation — mapping.json entries that no longer point at anything.

Three shapes, all read straight from ``mapping.json`` beside the cached scan
(2026-09-06):

* a curated **match** whose vault-relative path no longer exists on disk
  (the file was renamed, moved or deleted; hash recovery may have re-linked
  its content under ``manual_hash``, or may not);
* a curated **no_match** whose path no longer exists;
* a curated **pending** ("add to Zotero later") whose path no longer exists —
  added 2026-09-17, after the annual cleanup found two such entries
  (``she_2005.pdf``, renamed to ``shea_2005.pdf`` and recovered by content
  hash, and ``vinatier_2_2024.pdf``, deleted outright). The ``pending``
  bucket arrived with the curated-state split on 2026-09-06 and this report
  was written the same day without it, so ``mapping_orphans`` (23) and the
  rows it exists to list (21) disagreed by exactly the invisible entries.
  The Phase 2 proposer cannot see them either: the bridge only serves a
  pending file that exists;
* a curated decision the **Zotero link overrides** (2026-09-19): the file
  still exists, Zotero links it under exactly one item, and mapping.json says
  something else about it — another key, "not for Zotero", "pending", or a
  detach exclusion for that item. The bridge's first rung pairs the file with
  Zotero's item regardless (the maintainer's rule: Zotero wins, reported), so
  the stale decision does nothing and is listed for clearing. This is also
  what a citation-key change leaves behind: the old key is dead, the file is
  paired with the new one, and ``zotero_key`` names it;
* a curated match whose **citation key** is in neither the bib nor Zotero —
  the key was renamed or the entry deleted after the curation, so the PDF
  now belongs to a record that has no bib entry and no Zotero item. It is
  effectively an orphan again, but never surfaces as one.

``BridgeResult.mapping_orphans`` has counted the first two since the hash
anchoring landed; nothing listed them. Read-only: the remedy is the existing
``clear`` verb (or re-curation), never an automatic prune — a stale entry is
still the only record of a hand decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...config import Settings
from ..bridge import _load_mapping_all, _rel_to_vault
from ..inventory import Inventory


@dataclass
class DeadCurationRow:
    kind: str  # match_missing_file | no_match_missing_file |
    #            pending_missing_file | match_dead_key | overridden_by_zotero |
    #            zotero_unexported | zotero_multi_links
    rel: str
    citation_key: str | None = None
    #: overridden_by_zotero only: the key Zotero's link pairs the file with,
    #: and which curation it overrides (match / no_match / pending / exclude).
    zotero_key: str | None = None
    curation: str | None = None


@dataclass
class DeadCurationReport:
    rows: list[DeadCurationRow] = field(default_factory=list)
    mapping_orphans: int = 0  # the bridge's own count, for cross-checking
    #: PDFs the bridge re-linked THIS scan by content hash (``*_hash`` labels)
    #: — the recovered half of the orphans. The bridge does not record which
    #: stale entry each recovery came from, so this is a count, not a join.
    recovered_count: int = 0
    curated_matches: int = 0
    curated_no_match: int = 0
    curated_pending: int = 0


def find(inv: Inventory, settings: Settings) -> DeadCurationReport:
    rep = DeadCurationReport()
    rep.mapping_orphans = int(getattr(inv.bridge, "mapping_orphans", 0) or 0)
    mapping_file = getattr(settings, "mapping_file", None)
    if mapping_file is None:
        # A settings object without a mapping file (test fixtures) has no
        # curation to be dead; an empty report is the honest answer.
        return rep
    mapping = _load_mapping_all(mapping_file)
    matches, no_match = mapping.matches, mapping.no_match
    vault = settings.vault_root
    rep.curated_matches = sum(len(v) for v in matches.values())
    rep.curated_no_match = len(no_match)
    rep.curated_pending = len(mapping.pending)

    src = inv.bridge.source_per_pdf if inv.bridge is not None else {}
    overrides = getattr(inv.bridge, "zotero_overrides", None) or {}
    overridden_rels: set[str] = set()
    for pdf, info in sorted(overrides.items(), key=lambda kv: str(kv[0])):
        rel = _rel_to_vault(pdf, vault)   # the form mapping.json stores
        overridden_rels.add(rel)
        rep.rows.append(DeadCurationRow(
            "overridden_by_zotero", rel,
            citation_key=info.get("curated_key"),
            zotero_key=info.get("zotero_key"),
            curation=info.get("curation")))
    rep.recovered_count = sum(1 for label in src.values() if label.endswith("_hash"))

    for key in sorted(matches):
        rec = inv.records.get(key)
        key_alive = bool(rec and (rec.bib_entry or rec.zotero_item))
        for rel in sorted(matches[key]):
            if rel in overridden_rels:
                continue
            exists = (vault / rel).is_file()
            if not exists:
                rep.rows.append(DeadCurationRow(
                    "match_missing_file", rel, citation_key=key))
            elif not key_alive:
                rep.rows.append(DeadCurationRow(
                    "match_dead_key", rel, citation_key=key))
    for rel in sorted(no_match):
        if not (vault / rel).is_file():
            rep.rows.append(DeadCurationRow("no_match_missing_file", rel))
    for rel in sorted(mapping.pending):
        if not (vault / rel).is_file():
            rep.rows.append(DeadCurationRow("pending_missing_file", rel))
    # Files Zotero links that the pairing rung could not claim (2026-09-19).
    for pdf, key in sorted(
            (getattr(inv.bridge, "zotero_unexported", None) or {}).items(),
            key=lambda kv: str(kv[0])):
        rep.rows.append(DeadCurationRow(
            "zotero_unexported", _rel_to_vault(pdf, vault), zotero_key=key))
    for pdf, keys in sorted(
            (getattr(inv.bridge, "zotero_multi_links", None) or {}).items(),
            key=lambda kv: str(kv[0])):
        joined = ",".join(keys) if isinstance(keys, (list, tuple)) else str(keys)
        rep.rows.append(DeadCurationRow(
            "zotero_multi_links", _rel_to_vault(pdf, vault), zotero_key=joined))
    return rep
