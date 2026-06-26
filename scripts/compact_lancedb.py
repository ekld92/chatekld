#!/usr/bin/env python3
"""One-off LanceDB reclaim for an already-bloated vault index (app closed).

The live indexer now compacts + prunes the binary store at a bounded cadence
(see ``rag.lancedb_store.compact_lancedb_vector_store`` and
``ObsidianVaultManager._LANCEDB_COMPACT_EVERY``), so a *new* index self-maintains.
This script is insurance for an index that was already bloated before that fix —
e.g. a table left huge by a hard crash *before* the first checkpoint, where the
per-insert single-row fragments and their O(n²) version manifests were never
compacted.

What it does:
  1. Refuse to run while the app is open is the caller's responsibility — close
     ChatEKLD first (interactive confirm; ``--yes`` skips it).
  2. Open the live ``vectors`` table at ``OBSIDIAN_INDEX_DIR/lancedb``.
  3. ``optimize(cleanup_older_than=0)`` — merge data fragments and prune every
     superseded version manifest.
  4. Print the ``_versions/`` footprint before and after so the reclaim is visible.

Usage:
    python scripts/compact_lancedb.py [--index-dir DIR] [--yes]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta

# Allow ``python scripts/compact_lancedb.py`` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rag.lancedb_store import LANCEDB_TABLE, lancedb_dir  # noqa: E402


def _dir_size(path: str) -> tuple[int, int]:
    """Return (file_count, total_bytes) for *path*, (0, 0) if absent."""
    count = 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
                count += 1
            except OSError:
                pass
    return count, total


def _fmt(n: int) -> str:
    """Render a byte count as a human-readable size (B/KB/MB/GB/TB)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def main() -> int:
    """Open the vault's LanceDB table, ``optimize`` it, and report the reclaim.

    Gates on an app-closed confirmation (a running indexer holds the table),
    prints the ``_versions/`` footprint before and after the
    ``optimize(cleanup_older_than=0)`` so the reclaimed space is visible, and
    leaves the row data intact. Returns a process exit code.
    """
    parser = argparse.ArgumentParser(description="Compact + prune a bloated LanceDB vault index.")
    parser.add_argument("--index-dir", default=None, help="Index dir (default: the app's OBSIDIAN_INDEX_DIR).")
    parser.add_argument("--yes", action="store_true", help="Skip the app-is-closed confirmation.")
    args = parser.parse_args()

    if args.index_dir:
        index_dir = args.index_dir
    else:
        from core.constants import OBSIDIAN_INDEX_DIR
        index_dir = OBSIDIAN_INDEX_DIR

    db_dir = lancedb_dir(index_dir)
    if not os.path.isdir(db_dir):
        print(f"No LanceDB directory at {db_dir} — nothing to compact.")
        return 1

    try:
        import lancedb
    except Exception as exc:  # pragma: no cover - depends on optional extra
        print(f"lancedb is not importable ({exc}); install it or run with the app's venv.")
        return 1

    if not args.yes:
        reply = input(
            "Make sure ChatEKLD is fully closed (a running indexer holds the table).\n"
            "Proceed with compaction? [y/N] "
        ).strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 1

    versions_dir = os.path.join(db_dir, f"{LANCEDB_TABLE}.lance", "_versions")
    before_count, before_bytes = _dir_size(versions_dir)
    print(f"_versions/ before: {before_count} files, {_fmt(before_bytes)}")

    try:
        tbl = lancedb.connect(db_dir).open_table(LANCEDB_TABLE)
        rows = tbl.count_rows()
        tbl.optimize(cleanup_older_than=timedelta(0))
    except Exception as exc:
        print(f"Compaction failed: {exc}")
        return 1

    after_count, after_bytes = _dir_size(versions_dir)
    print(f"_versions/ after:  {after_count} files, {_fmt(after_bytes)}")
    print(f"Reclaimed ~{_fmt(max(0, before_bytes - after_bytes))} ({rows} rows intact).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
