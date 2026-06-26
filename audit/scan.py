"""CLI smoke test for the Library Audit subsystem.

Reads the active ChatEKLD config to build :class:`audit.config.Settings`,
then runs one or more diagnostic checks against the configured vault and
Zotero database. No data is written back to either store.

Connectors::

    python -m audit --check obsidian
    python -m audit --check zotero
    python -m audit --check zotero-debug
    python -m audit --check finder
    python -m audit --check bib
    python -m audit --check duplicates           # whole Z_attachments
    python -m audit --check duplicates-biblio    # only biblio_articles

Bridge + reports::

    python -m audit --check bridge
    python -m audit --check inventory
    python -m audit --check note-tag-drift       # aim (i)
    python -m audit --check unread-unzoterod     # aim (ii)
    python -m audit --check zotero-unread        # aim (iii)
    python -m audit --check read-unzoterod       # aim (iv)
    python -m audit --check zotero-no-pdf        # aim (v)
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

from .config import AuditConfigError, Settings, load_settings
from .core import bib, finder_tags, hashing, obsidian, zotero
from .engine import bridge as eng_bridge
from .engine import duplicates as eng_duplicates
from .engine import inventory as eng_inventory
from .engine.reports import (
    note_tag_drift as r_note_tag_drift,
)
from .engine.reports import (
    read_unzoterod as r_read_unzoterod,
)
from .engine.reports import (
    unread_unzoterod as r_unread_unzoterod,
)
from .engine.reports import (
    zotero_no_pdf as r_zotero_no_pdf,
)
from .engine.reports import (
    zotero_unread as r_zotero_unread,
)


def _rel(p: Path, settings: Settings) -> str:
    """Vault-relative display path for CLI output; absolute if outside the vault."""
    try:
        return str(p.relative_to(settings.vault_root))
    except ValueError:
        return str(p)


def check_obsidian(settings: Settings) -> int:
    """Print Obsidian-connector stats (notes, frontmatter, tags, PDF refs)."""
    print(f"\n[obsidian] scanning {settings.vault_root}")
    notes = list(obsidian.scan_vault(settings.vault_root, settings.ignored_dirs))
    tag_counter: Counter[str] = Counter()
    pdf_refs: set[str] = set()
    for n in notes:
        tag_counter.update(n.tags)
        pdf_refs.update(n.pdf_links)
    print(f"  notes parsed:         {len(notes)}")
    print(f"  notes w/ frontmatter: {sum(1 for n in notes if n.has_frontmatter)}")
    print(f"  unique YAML tags:     {len(tag_counter)}")
    print(f"  unique PDF refs:      {len(pdf_refs)}")
    return 0


def check_zotero(settings: Settings) -> int:
    """Print Zotero-connector stats (parents, child notes, tag counts)."""
    print(f"\n[zotero] reading {settings.zotero_sqlite}")
    if not settings.zotero_sqlite.exists():
        print("  zotero.sqlite not found — skip")
        return 0
    items = zotero.read_items(settings.zotero_sqlite, settings.zotero_storage)
    parent_tag_counter: Counter[str] = Counter()
    note_tag_counter: Counter[str] = Counter()
    items_with_child_notes = 0
    total_child_notes = 0
    for it in items:
        parent_tag_counter.update(it.tags)
        if it.child_notes:
            items_with_child_notes += 1
        for cn in it.child_notes:
            total_child_notes += 1
            note_tag_counter.update(cn.tags)
    print(f"  parent items:           {len(items)}")
    print(f"  items w/ child notes:   {items_with_child_notes}")
    print(f"  total child notes:      {total_child_notes}")
    print(f"  unique parent tags:     {len(parent_tag_counter)}")
    print(f"  unique note tags:       {len(note_tag_counter)}")
    return 0


def check_zotero_debug(settings: Settings) -> int:
    """Dump raw ``itemNotes`` row counts straight from a copied DB.

    A lower-level probe than :func:`check_zotero`: it copies the live SQLite
    (plus any ``-wal``/``-shm`` siblings) to a temp dir and opens it
    ``mode=ro&immutable=1`` so the running Zotero is untouched, then prints the
    note totals — used to debug why child notes may be under/over-counted.
    """
    import shutil
    import sqlite3
    import tempfile

    sp = settings.zotero_sqlite
    if not sp.exists():
        print(f"\n[zotero-debug] {sp} not found")
        return 0
    print(f"\n[zotero-debug] dumping raw rows from {sp}")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "zotero.sqlite"
        shutil.copy2(sp, tmp)
        for suffix in ("-wal", "-shm"):
            sib = sp.with_name(sp.name + suffix)
            if sib.exists():
                shutil.copy2(sib, tmp.with_name(tmp.name + suffix))
        conn = sqlite3.connect(f"file:{tmp}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            print(
                "  itemNotes total:    ",
                conn.execute("SELECT COUNT(*) FROM itemNotes").fetchone()[0],
            )
            print(
                "  notes w/ parent:    ",
                conn.execute(
                    "SELECT COUNT(*) FROM itemNotes WHERE parentItemID IS NOT NULL"
                ).fetchone()[0],
            )
        finally:
            conn.close()
    return 0


def check_finder(settings: Settings) -> int:
    """Print Finder-tag coverage under ``biblio_articles`` (macOS-only data)."""
    target = settings.biblio_articles_dir
    print(f"\n[finder] scanning tags under {target}")
    if not target.exists():
        print("  biblio_articles dir missing — skip")
        return 0
    counter: Counter[str] = Counter()
    scanned = 0
    tagged = 0
    for p in target.rglob("*"):
        if not p.is_file():
            continue
        scanned += 1
        names = finder_tags.read_tag_names(p)
        if names:
            tagged += 1
            counter.update(names)
    print(f"  files scanned:        {scanned}")
    print(f"  files with tags:      {tagged}")
    print(f"  unique tags:          {len(counter)}")
    return 0


def check_duplicates(settings: Settings) -> int:
    """Count content-duplicate PDF sets across the whole ``Z_attachments`` tree."""
    print(f"\n[duplicates] hashing PDFs under {settings.attachments_dir}")
    if not settings.attachments_dir.exists():
        return 0
    pdfs = [p for p in settings.attachments_dir.rglob("*.pdf") if p.is_file()]
    print(f"  PDFs found:           {len(pdfs)}")
    dupes = hashing.find_duplicate_sets(pdfs)
    print(f"  duplicate sets:       {len(dupes)}")
    return 0


def check_bib(settings: Settings) -> int:
    """Print bib-parser stats (entry count, entries carrying keywords)."""
    print(f"\n[bib] parsing {settings.master_bib}")
    if not settings.master_bib.exists():
        return 0
    entries = bib.parse_bib(settings.master_bib)
    print(f"  entries parsed:       {len(entries)}")
    print(f"  entries w/ keywords:  {sum(1 for e in entries if e.keywords)}")
    return 0


def check_bridge(settings: Settings) -> int:
    """Run the PDF↔bib bridge and print a per-source match breakdown.

    Mirrors the inventory's active-PDF set (skip-prefix applied) and prints how
    many PDFs each resolution step claimed, plus sample unmapped/ambiguous
    paths — the go-to diagnostic when matching looks wrong.
    """
    print("\n[bridge] resolving biblio_articles PDFs against _master.bib")
    entries = bib.parse_bib(settings.master_bib)
    skip_prefix = settings.biblio_skip_prefix or None
    pdfs = [
        p
        for p in settings.biblio_articles_dir.rglob("*.pdf")
        if p.is_file()
        and (skip_prefix is None or not p.name.lower().startswith(skip_prefix))
    ]
    result = eng_bridge.build_bridge(settings, entries, pdfs)
    sources = Counter(result.source_per_pdf.values())
    print(f"  bib entries:               {len(entries)}")
    print(f"  active PDFs:               {len(pdfs)}")
    print(f"  Z_Zotero_Notes w/ FM:      {result.notes_with_fm}")
    print(f"  FM pointer-fields seen:    {result.fm_pointer_fields_seen}")
    print(f"  matched by manual:         {sources.get('manual', 0)}")
    print(f"  matched by FM pointer:     {sources.get('fm_pointer', 0)}")
    print(f"  matched by FM author+year: {sources.get('fm_authoryear', 0)}")
    print(f"  matched by wikilink:       {sources.get('wikilink', 0)}")
    print(f"  matched by bib author+year:{sources.get('authoryear', 0)}")
    print(f"  ambiguous (author+year):   {len(result.ambiguous_pdfs)}")
    print(f"  unmapped:                  {len(result.unmapped_pdfs)}")
    print(f"  confirmed no-match (json): {len(result.confirmed_no_match_pdfs)}")
    print(f"  bib keys with >=1 PDF:     {len(result.bib_to_pdfs)}")
    if result.unmapped_pdfs:
        print("  sample 10 unmapped:")
        for p in sorted(result.unmapped_pdfs)[:10]:
            print(f"    {_rel(p, settings)}")
    if result.ambiguous_pdfs:
        print("  sample 5 ambiguous:")
        for p, keys in list(sorted(result.ambiguous_pdfs.items()))[:5]:
            print(f"    {_rel(p, settings)}  -> {keys}")
    return 0


def check_inventory(settings: Settings) -> int:
    """Build the full inventory (annotations off) and print triangulation tallies."""
    print("\n[inventory] joining bib + bridge + Z_Zotero_Notes + Finder + Zotero")
    inv = eng_inventory.build_inventory(settings, count_annotations=False)
    records = inv.records
    n_records = len(records)
    n_bib_pdf = sum(1 for r in records.values() if r.bib_entry and r.pdf_paths)
    n_bib_note = sum(1 for r in records.values() if r.bib_entry and r.obsidian_note)
    n_bib_zot = sum(1 for r in records.values() if r.bib_entry and r.zotero_item)
    n_triangulated = sum(
        1
        for r in records.values()
        if r.bib_entry and r.pdf_paths and r.obsidian_note and r.zotero_item
    )
    n_zot_with_note = sum(
        1 for r in records.values() if r.zotero_item and r.zotero_item.child_notes
    )
    print(f"  records (unique citation keys): {n_records}")
    print(f"  bib + PDF (via bridge):         {n_bib_pdf}")
    print(f"  bib + Obsidian note:            {n_bib_note}")
    print(f"  bib + Zotero parent:            {n_bib_zot}")
    print(f"  fully triangulated:             {n_triangulated}")
    print(f"  Zotero parents w/ child note:   {n_zot_with_note}")
    print(f"  PDFs unmapped by bridge:        {len(inv.bridge.unmapped_pdfs)}")
    print(f"  PDFs ambiguous by bridge:       {len(inv.bridge.ambiguous_pdfs)}")
    print(f"  PDFs skipped (z_item*):         {len(inv.pdfs_skipped)}")
    if inv.zotero_error:
        print(f"  Zotero read warning:            {inv.zotero_error}")
    return 0


def check_note_tag_drift(settings: Settings) -> int:
    """Aim (i): print citation keys whose Obsidian YAML lacks Zotero note tags."""
    print(
        "\n[note-tag-drift] Zotero child-note tags missing from Obsidian YAML (aim i)"
    )
    inv = eng_inventory.build_inventory(settings, count_annotations=False)
    rows = r_note_tag_drift.find_drift(inv)
    print(f"  rows: {len(rows)}")
    for row in rows[:15]:
        print(f"  {row.citation_key}")
        print(f"    zotero note tags : {sorted(row.zotero_note_tags)}")
        print(f"    obs YAML tags    : {sorted(row.obs_tags)}")
        print(f"    missing in obs   : {sorted(row.missing_in_obs)}")
    return 0


def check_unread_unzoterod(settings: Settings) -> int:
    """Aim (ii): print PDFs absent from the bib that also look unread."""
    print("\n[unread-unzoterod] PDFs not in bib AND look unread (aim ii)")
    inv = eng_inventory.build_inventory(settings, count_annotations=False)
    rep = r_unread_unzoterod.find(inv, settings)
    print(f"  rows: {len(rep.rows)}  (threshold = {rep.threshold} annotations)")
    print(f"  ambiguous-bridge PDFs (excluded): {rep.ambiguous_count}")
    for row in rep.rows[:20]:
        print(f"    {row.annotations:>3} annots   {_rel(row.pdf, settings)}")
    if len(rep.rows) > 20:
        print(f"    ... {len(rep.rows) - 20} more")
    return 0


def check_zotero_unread(settings: Settings) -> int:
    """Aim (iii): print bib entries whose Zotero parent has no child note."""
    print("\n[zotero-unread] bib entries with no Zotero child note (aim iii)")
    inv = eng_inventory.build_inventory(settings, count_annotations=False)
    rep = r_zotero_unread.find(inv)
    print(f"  rows: {len(rep.rows)}")
    print(f"  skipped (no Zotero title match): {rep.skipped_no_zotero_match}")
    for row in rep.rows[:20]:
        print(f"    {row.year or '----'}  {row.citation_key}  {(row.title or '')[:80]}")
    if len(rep.rows) > 20:
        print(f"    ... {len(rep.rows) - 20} more")
    return 0


def check_read_unzoterod(settings: Settings) -> int:
    """Aim (iv): print un-Zotero'd PDFs ranked by annotation count."""
    print("\n[read-unzoterod] PDFs not in bib, ranked by annotation count (aim iv)")
    inv = eng_inventory.build_inventory(settings, count_annotations=False)
    rep = r_read_unzoterod.find(inv, settings)
    print(
        f"  rows: {len(rep.rows)}  (suggested read cutoff = {rep.suggested_read_cutoff} annotations)"
    )
    above = sum(1 for r in rep.rows if r.annotations >= rep.suggested_read_cutoff)
    print(f"  rows at/above cutoff: {above}")
    for row in rep.rows[:30]:
        print(f"    {row.annotations:>4} annots   {_rel(row.pdf, settings)}")
    if len(rep.rows) > 30:
        print(f"    ... {len(rep.rows) - 30} more")
    return 0


def check_zotero_no_pdf(settings: Settings) -> int:
    """Aim (v): print bib entries for which the bridge found no PDF."""
    print("\n[zotero-no-pdf] bib entries with no resolved PDF (aim v)")
    inv = eng_inventory.build_inventory(settings, count_annotations=False)
    rep = r_zotero_no_pdf.find(inv)
    print(f"  rows: {len(rep.rows)}")
    n_with_zot = sum(1 for r in rep.rows if r.has_zotero_match)
    print(f"  of which matched to a Zotero parent: {n_with_zot}")
    for row in rep.rows[:20]:
        zmark = "Z" if row.has_zotero_match else "-"
        print(
            f"    [{zmark}] {row.year or '----'}  {row.citation_key}  {(row.title or '')[:80]}"
        )
    if len(rep.rows) > 20:
        print(f"    ... {len(rep.rows) - 20} more")
    return 0


def check_duplicates_biblio(settings: Settings) -> int:
    """Count content-duplicate PDF sets scoped to ``biblio_articles`` only."""
    print(f"\n[duplicates-biblio] hashing PDFs under {settings.biblio_articles_dir}")
    sets = eng_duplicates.find_biblio_duplicates(settings)
    print(f"  duplicate sets: {len(sets)}")
    return 0


CHECKS = {
    "obsidian": check_obsidian,
    "zotero": check_zotero,
    "zotero-debug": check_zotero_debug,
    "finder": check_finder,
    "bib": check_bib,
    "duplicates": check_duplicates,
    "duplicates-biblio": check_duplicates_biblio,
    "bridge": check_bridge,
    "inventory": check_inventory,
    "note-tag-drift": check_note_tag_drift,
    "unread-unzoterod": check_unread_unzoterod,
    "zotero-unread": check_zotero_unread,
    "read-unzoterod": check_read_unzoterod,
    "zotero-no-pdf": check_zotero_no_pdf,
}


def main() -> int:
    """Parse ``--check <name|all>``, load settings, run the selected check(s).

    Returns a bit-OR'd exit code: ``2`` for an incomplete audit config (no
    vault), ``1`` if any individual check raised (logged, others still run),
    else the OR of each check's own return. Read-only throughout.
    """
    parser = argparse.ArgumentParser(prog="audit")
    parser.add_argument(
        "--check",
        choices=[*CHECKS.keys(), "all"],
        required=True,
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
    logger = logging.getLogger(__name__)

    try:
        settings = load_settings()
    except AuditConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    targets = list(CHECKS) if args.check == "all" else [args.check]
    rc = 0
    for name in targets:
        try:
            rc |= CHECKS[name](settings)
        except Exception:
            logger.exception(f"[{name}] check failed")
            rc |= 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
