#!/usr/bin/env python3
"""Prune LlamaIndex storage metadata to the active vector-store IDs.

Use after repairing a truncated SimpleVectorStore when docstore/index_store
still reference nodes whose embeddings were not recovered.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


READ_SIZE = 4 * 1024 * 1024
PREFIX = '{"embedding_dict":{'
PREFIX_SPACED = '{"embedding_dict": {'


def _read_more(src, buffer: str, eof: bool) -> tuple[str, bool]:
    if eof:
        return buffer, eof
    chunk = src.read(READ_SIZE)
    if chunk == "":
        return buffer, True
    return buffer + chunk, False


def _skip_ws(buffer: str, pos: int) -> int:
    while pos < len(buffer) and buffer[pos] in " \t\r\n":
        pos += 1
    return pos


def vector_store_ids(path: Path, progress_every: int) -> set[str]:
    decoder = json.JSONDecoder()
    ids: set[str] = set()
    buffer = ""
    pos = 0
    eof = False

    with path.open("r", encoding="utf-8") as src:
        buffer, eof = _read_more(src, buffer, eof)
        if buffer.startswith(PREFIX):
            pos = len(PREFIX)
        elif buffer.startswith(PREFIX_SPACED):
            pos = len(PREFIX_SPACED)
        else:
            raise RuntimeError(f"{path} does not look like a SimpleVectorStore JSON file")

        while True:
            pos = _skip_ws(buffer, pos)
            while pos >= len(buffer) and not eof:
                buffer, eof = _read_more(src, "", eof)
                pos = 0
                pos = _skip_ws(buffer, pos)
            if pos >= len(buffer):
                break
            if buffer[pos] == ",":
                pos += 1
                continue
            if buffer[pos] == "}":
                break

            while True:
                try:
                    key, pos = decoder.raw_decode(buffer, pos)
                    break
                except json.JSONDecodeError:
                    if eof:
                        raise
                    buffer, eof = _read_more(src, buffer, eof)
            if not isinstance(key, str):
                raise RuntimeError("Expected vector-store embedding key string")
            ids.add(key)

            pos = _skip_ws(buffer, pos)
            if pos >= len(buffer) or buffer[pos] != ":":
                raise RuntimeError(f"Expected ':' after vector-store key {key!r}")
            pos += 1
            pos = _skip_ws(buffer, pos)

            while True:
                try:
                    _value, pos = decoder.raw_decode(buffer, pos)
                    break
                except json.JSONDecodeError:
                    if eof:
                        raise
                    buffer, eof = _read_more(src, buffer, eof)

            if progress_every and len(ids) % progress_every == 0:
                print(f"Read {len(ids):,} vector ids...", flush=True)

            if pos > READ_SIZE:
                buffer = buffer[pos:]
                pos = 0

    return ids


def atomic_json_write(path: Path, data: dict) -> None:
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", text=True)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def backup_once(path: Path) -> Path:
    backup = path.with_name(path.name + ".pre-prune")
    if backup.exists():
        raise RuntimeError(f"Refusing to overwrite existing backup {backup}")
    shutil.copy2(path, backup)
    return backup


def prune_docstore(path: Path, ids: set[str], *, dry_run: bool = False) -> tuple[int, int]:
    print(f"Loading docstore {path}...", flush=True)
    with path.open("r", encoding="utf-8") as f:
        docstore = json.load(f)

    ref_info = docstore.get("docstore/ref_doc_info", {})
    data = docstore.get("docstore/data", {})
    metadata = docstore.get("docstore/metadata", {})

    kept_ref_info = {}
    kept_ref_ids = set()
    for ref_doc_id, info in ref_info.items():
        if not isinstance(info, dict):
            continue
        node_ids = [node_id for node_id in info.get("node_ids", []) if node_id in ids]
        if not node_ids:
            continue
        new_info = dict(info)
        new_info["node_ids"] = node_ids
        kept_ref_info[ref_doc_id] = new_info
        kept_ref_ids.add(ref_doc_id)

    kept_data = {node_id: node for node_id, node in data.items() if node_id in ids}
    kept_metadata = {
        key: value
        for key, value in metadata.items()
        if key in ids or key in kept_ref_ids
    }

    pruned = {
        "docstore/ref_doc_info": kept_ref_info,
        "docstore/metadata": kept_metadata,
        "docstore/data": kept_data,
    }
    if not dry_run:
        backup = backup_once(path)
        atomic_json_write(path, pruned)
        print(f"Pruned docstore. Backup: {backup}", flush=True)
    return len(kept_data), len(kept_ref_info)


def prune_index_store(path: Path, ids: set[str], *, dry_run: bool = False) -> int:
    with path.open("r", encoding="utf-8") as f:
        index_store = json.load(f)
    entries = index_store.get("index_store/data", {})
    kept_total = 0
    for entry in entries.values():
        if not isinstance(entry, dict) or "__data__" not in entry:
            continue
        inner = json.loads(entry["__data__"])
        nodes_dict = inner.get("nodes_dict", {})
        inner["nodes_dict"] = {
            key: value for key, value in nodes_dict.items()
            if key in ids and value in ids
        }
        kept_total += len(inner["nodes_dict"])
        entry["__data__"] = json.dumps(inner, ensure_ascii=False)

    if not dry_run:
        backup = backup_once(path)
        atomic_json_write(path, index_store)
        print(f"Pruned index store. Backup: {backup}", flush=True)
    return kept_total


def rebuild_manifest(path: Path, docstore_path: Path, meta_path: Path, *, dry_run: bool = False) -> int:
    with docstore_path.open("r", encoding="utf-8") as f:
        docstore = json.load(f)
    ref_info = docstore.get("docstore/ref_doc_info", {})
    entries: dict[str, dict] = {}
    for ref_doc_id, info in ref_info.items():
        if not isinstance(info, dict):
            continue
        meta = info.get("metadata") if isinstance(info.get("metadata"), dict) else {}
        source = meta.get("source") or meta.get("file_path") or str(ref_doc_id)
        entry = entries.setdefault(source, {
            "source": source,
            "extension": Path(str(source)).suffix.lower(),
            "chunk_count": 0,
        })
        entry["chunk_count"] += len(info.get("node_ids", []) or [])
    vault_path = ""
    indexed_at = None
    try:
        with path.open("r", encoding="utf-8") as f:
            previous = json.load(f)
        if isinstance(previous, dict):
            vault_path = previous.get("vault_path") or ""
            indexed_at = previous.get("indexed_at")
    except Exception:
        pass
    if indexed_at is None:
        try:
            with meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            if isinstance(meta, dict):
                indexed_at = meta.get("indexed_at")
        except Exception:
            pass

    payload = {
        "vault_path": vault_path,
        "indexed_at": indexed_at,
        "materials": sorted(entries.values(), key=lambda item: item["source"].lower()),
    }
    if not dry_run:
        atomic_json_write(path, payload)
    return len(payload["materials"])


def update_meta(path: Path, vector_count: int, *, dry_run: bool = False) -> None:
    with path.open("r", encoding="utf-8") as f:
        meta = json.load(f)
    meta["repaired_at"] = datetime.now(timezone.utc).isoformat()
    meta["repaired_vector_count"] = vector_count
    meta["partial"] = True
    meta["phase"] = "paused_partial"
    meta["has_vector_data"] = vector_count > 0
    if not dry_run:
        backup = backup_once(path)
        atomic_json_write(path, meta)
        print(f"Updated meta. Backup: {backup}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-dir", default=os.path.expanduser("~/Library/Application Support/ChatEKLD/obsidian_storage"))
    parser.add_argument("--progress-every", type=int, default=10_000)
    parser.add_argument("--dry-run", action="store_true", help="Report counts without modifying storage files")
    args = parser.parse_args()

    storage = Path(args.storage_dir)
    ids = vector_store_ids(storage / "default__vector_store.json", args.progress_every)
    print(f"Active vector store has {len(ids):,} embeddings.", flush=True)

    data_count, ref_count = prune_docstore(storage / "docstore.json", ids, dry_run=args.dry_run)
    index_count = prune_index_store(storage / "index_store.json", ids, dry_run=args.dry_run)
    material_count = rebuild_manifest(
        storage / "indexed_materials.json",
        storage / "docstore.json",
        storage / "obsidian_meta.json",
        dry_run=args.dry_run,
    )
    update_meta(storage / "obsidian_meta.json", len(ids), dry_run=args.dry_run)

    print(
        ("Dry-run complete: " if args.dry_run else "Prune complete: ") +
        f"docstore_data={data_count:,}, ref_docs={ref_count:,}, "
        f"index_nodes={index_count:,}, materials={material_count:,}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
