#!/usr/bin/env python3
"""Migrate a vault index from the legacy JSON SimpleVectorStore to LanceDB.

One-time, **offline**, and with **no re-embedding**: embeddings are read out of
``default__vector_store.json`` and joined to the nodes in ``docstore.json`` by
``node_id``. The embedding model is never constructed, so the migration is fast
and works with the app's API keys unset.

What it does, in order:
  1. Refuse to run unless the app is closed (interactive confirm, ``--yes`` skips).
  2. Stream ``embedding_dict`` lazily with ijson so peak RSS stays ~100 MB even
     on a multi-GB index (pattern borrowed from repair_simple_vector_store.py).
  3. Join each embedding to its docstore node and bulk-insert into LanceDB via
     ``rag.lancedb_store.NormalizingLanceDBVectorStore`` (unit-normalizes for
     cosine parity and JSON-stringifies list/dict metadata — the docstore keeps
     the original).
  4. Verify row-count parity (table rows == embeddings joined).
  5. Archive ``default__vector_store.json`` → ``….json.bak`` (kept as a rollback;
     delete it by hand once you're happy).
  6. Record ``"vector_backend": "lancedb"`` in ``obsidian_meta.json`` (atomic,
     other keys preserved). The app reads this on next launch.

``docstore.json`` and ``index_store.json`` are left untouched — they remain the
JSON stores LlamaIndex uses for nodes / index metadata, which is exactly why
``store_nodes_override=True`` keeps BM25 and the document-hash skip-check working.

Usage:
    python scripts/migrate_vector_store.py [--index-dir DIR] [--yes]
                                           [--batch 512] [--keep-json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Script lives in scripts/; put the project root on sys.path so the
# authoritative rag/core modules import (repair_simple_vector_store.py avoids
# project imports, but we deliberately reuse rag.lancedb_store here).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ijson  # noqa: E402
from llama_index.core.storage.docstore import SimpleDocumentStore  # noqa: E402

from core.constants import OBSIDIAN_INDEX_DIR  # noqa: E402
from core.utils import write_text_atomic  # noqa: E402
from rag.lancedb_store import (  # noqa: E402
    VECTOR_BACKEND_LANCEDB,
    lancedb_available,
    lancedb_dir,
    lancedb_table_count,
    make_lancedb_vector_store,
)

VECTOR_JSON = "default__vector_store.json"
META_JSON = "obsidian_meta.json"


def _confirm_app_closed(assume_yes: bool) -> bool:
    """Interactive "is the app closed?" gate; ``--yes`` (``assume_yes``) skips it.

    A running app holds the index in memory and would overwrite this migration on
    its next checkpoint, so the migration must not run against a live instance.
    Returns ``True`` only on an explicit y/yes (or ``assume_yes``); an EOF (e.g.
    piped/non-interactive) is treated as "no".
    """
    if assume_yes:
        return True
    print(
        "\nThe ChatEKLD app MUST be closed before migrating — a running app holds\n"
        "the index in memory and could overwrite the migration on its next\n"
        "checkpoint. The original JSON store is archived to .bak, so this is\n"
        "reversible."
    )
    try:
        answer = input("Is the app closed and do you want to proceed? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def migrate(index_dir: str, *, batch_size: int, keep_json: bool) -> int:
    """Move an index from the JSON SimpleVectorStore to LanceDB without re-embedding.

    Streams ``embedding_dict`` with ijson, joins each vector to its docstore node
    by id, bulk-inserts batches of *batch_size* into LanceDB, verifies row-count
    parity, archives the legacy JSON to ``.bak`` (unless *keep_json*), and records
    ``vector_backend=lancedb`` in ``obsidian_meta.json``. Idempotent: a no-op when
    the index is already on LanceDB. Returns a process exit code (0 success, 1
    parity failure, 2 missing prerequisites). See the module docstring for the
    full step list and rationale.
    """
    index_path = Path(index_dir)
    vec_json = index_path / VECTOR_JSON
    docstore_json = index_path / "docstore.json"

    if not lancedb_available():
        print(
            "ERROR: lancedb is not installed. Install it first:\n"
            "  pip install lancedb llama-index-vector-stores-lancedb ijson",
            file=sys.stderr,
        )
        return 2
    if not docstore_json.exists():
        print(f"ERROR: no docstore.json under {index_dir} — nothing to migrate.", file=sys.stderr)
        return 2
    if not vec_json.exists():
        # Already migrated, or a fresh lancedb build — idempotent no-op.
        if (index_path / "lancedb").is_dir():
            print("Already on LanceDB (no legacy JSON vector store present). Nothing to do.")
            return 0
        print(f"ERROR: no {VECTOR_JSON} under {index_dir} — nothing to migrate.", file=sys.stderr)
        return 2

    print(f"Loading docstore from {index_dir} …")
    docstore = SimpleDocumentStore.from_persist_dir(str(index_path))

    vector_store = make_lancedb_vector_store(index_dir)

    print("Streaming embeddings and bulk-loading LanceDB …")
    batch: list = []
    joined = 0
    missing = 0
    with open(vec_json, "rb") as f:
        for node_id, embedding in ijson.kvitems(f, "embedding_dict"):
            node = docstore.get_node(node_id, raise_error=False)
            if node is None:
                # A vector whose node was deleted from the docstore — skip it
                # (it could never be retrieved or rehydrated anyway).
                missing += 1
                continue
            node.embedding = [float(x) for x in embedding]
            batch.append(node)
            if len(batch) >= batch_size:
                vector_store.add(batch)
                joined += len(batch)
                batch.clear()
                print(f"  … {joined} vectors migrated", end="\r", flush=True)
        if batch:
            vector_store.add(batch)
            joined += len(batch)
    print(f"  … {joined} vectors migrated        ")

    rows = lancedb_table_count(index_dir)
    if rows != joined:
        print(
            f"ERROR: row-count parity failed — LanceDB table has {rows} rows but "
            f"{joined} vectors were joined. Leaving the JSON store in place; "
            f"the partial lancedb/ dir under {index_dir} can be deleted.",
            file=sys.stderr,
        )
        return 1
    if missing:
        print(f"Note: skipped {missing} embedding(s) with no matching docstore node.")

    if not keep_json:
        bak = vec_json.with_suffix(".json.bak")
        os.replace(vec_json, bak)
        print(f"Archived legacy vector store → {bak.name}")
    else:
        print(f"--keep-json: left {VECTOR_JSON} in place (the app ignores it once "
              f"vector_backend=lancedb).")

    meta_path = index_path / META_JSON
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    meta["vector_backend"] = VECTOR_BACKEND_LANCEDB
    write_text_atomic(str(meta_path), json.dumps(meta, indent=2))
    print(f"Recorded vector_backend=lancedb in {META_JSON}.")

    print(
        f"\nMigration complete: {joined} vectors now in {lancedb_dir(index_dir)} "
        f"(table 'vectors').\nStart the app — prewarm should be markedly faster. "
        f"A no-change reindex must report added=0 (no re-embed)."
    )
    return 0


def main(argv: list | None = None) -> int:
    """Parse args, gate on the app-closed confirmation, and run :func:`migrate`."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--index-dir", default=OBSIDIAN_INDEX_DIR,
                        help=f"Vault index directory (default: {OBSIDIAN_INDEX_DIR})")
    parser.add_argument("--yes", action="store_true", help="Skip the app-closed confirmation prompt.")
    parser.add_argument("--batch", type=int, default=512, help="Insert batch size (default: 512).")
    parser.add_argument("--keep-json", action="store_true",
                        help="Do not rename the legacy JSON vector store to .bak.")
    args = parser.parse_args(argv)

    if not _confirm_app_closed(args.yes):
        print("Aborted.")
        return 1
    return migrate(args.index_dir, batch_size=max(1, args.batch), keep_json=args.keep_json)


if __name__ == "__main__":
    raise SystemExit(main())
