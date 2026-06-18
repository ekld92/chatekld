#!/usr/bin/env python3
"""Repair a truncated LlamaIndex SimpleVectorStore JSON file.

The repair is conservative: it preserves only complete entries from
``embedding_dict`` and rebuilds ``text_id_to_ref_doc_id`` plus
``metadata_dict`` from a valid LlamaIndex docstore.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from llama_index.core.schema import TextNode
from llama_index.core.vector_stores.simple import SimpleVectorStore
from llama_index.core.vector_stores.utils import node_to_metadata_dict


PREFIXES = ('{"embedding_dict": {', '{"embedding_dict":{')
READ_SIZE = 4 * 1024 * 1024


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


def _ensure(src, buffer: str, pos: int, eof: bool, need: int = 1) -> tuple[str, bool]:
    while len(buffer) - pos < need and not eof:
        buffer, eof = _read_more(src, buffer, eof)
    return buffer, eof


def _decode_next(decoder: json.JSONDecoder, src, buffer: str, pos: int, eof: bool):
    """Decode one JSON value at *pos*, reading more until complete or EOF."""
    while True:
        try:
            return (*decoder.raw_decode(buffer, pos), buffer, eof)
        except json.JSONDecodeError:
            if eof:
                return None, pos, buffer, eof
            buffer, eof = _read_more(src, buffer, eof)


def _write_json_member(out, first: bool, key: str, value: Any) -> bool:
    if not first:
        out.write(",")
    json.dump(key, out, ensure_ascii=False, separators=(",", ":"))
    out.write(":")
    json.dump(value, out, ensure_ascii=False, separators=(",", ":"))
    return False


def recover_embedding_dict(vector_store_path: Path, out, progress_every: int) -> list[str]:
    decoder = json.JSONDecoder()
    recovered_ids: list[str] = []
    buffer = ""
    pos = 0
    eof = False

    with vector_store_path.open("r", encoding="utf-8", errors="strict") as src:
        buffer, eof = _ensure(src, buffer, pos, eof, max(len(p) for p in PREFIXES))
        prefix = next((p for p in PREFIXES if buffer.startswith(p)), "")
        if not prefix:
            raise RuntimeError(f"{vector_store_path} does not start like a SimpleVectorStore JSON file")
        pos = len(prefix)

        out.write('{"embedding_dict":{')
        first = True

        while True:
            buffer, eof = _ensure(src, buffer, pos, eof)
            pos = _skip_ws(buffer, pos)
            buffer, eof = _ensure(src, buffer, pos, eof)
            if pos >= len(buffer):
                break
            if buffer[pos] == ",":
                pos += 1
                continue
            if buffer[pos] == "}":
                pos += 1
                break

            key, next_pos, buffer, eof = _decode_next(decoder, src, buffer, pos, eof)
            if key is None:
                break
            if not isinstance(key, str):
                raise RuntimeError(f"Expected embedding id string at byte offset near {pos}")
            pos = _skip_ws(buffer, next_pos)
            buffer, eof = _ensure(src, buffer, pos, eof)
            if pos >= len(buffer) or buffer[pos] != ":":
                if eof:
                    break
                raise RuntimeError(f"Expected ':' after embedding id {key!r}")
            pos += 1
            pos = _skip_ws(buffer, pos)

            value, next_pos, buffer, eof = _decode_next(decoder, src, buffer, pos, eof)
            if value is None:
                print(f"Stopped at incomplete embedding for id {key!r}; dropping it.", file=sys.stderr)
                break
            if not isinstance(value, list):
                raise RuntimeError(f"Expected embedding list for id {key!r}")

            first = _write_json_member(out, first, key, value)
            recovered_ids.append(key)
            pos = next_pos

            if progress_every and len(recovered_ids) % progress_every == 0:
                print(f"Recovered {len(recovered_ids):,} complete embeddings...", file=sys.stderr, flush=True)

            # Drop consumed text so the buffer never grows with the whole file.
            if pos > READ_SIZE:
                buffer = buffer[pos:]
                pos = 0

        out.write("}")

    return recovered_ids


def _node_metadata_and_ref_doc(raw_node: dict) -> tuple[str, dict]:
    node = TextNode.from_dict(raw_node["__data__"])
    metadata = node_to_metadata_dict(node, remove_text=True, flat_metadata=False)
    metadata.pop("_node_content", None)
    return node.ref_doc_id or "None", metadata


def write_rebuilt_maps(out, docstore_path: Path, recovered_ids: list[str], progress_every: int) -> tuple[int, int]:
    print(f"Loading docstore from {docstore_path}...", file=sys.stderr, flush=True)
    with docstore_path.open("r", encoding="utf-8") as f:
        docstore = json.load(f)
    docstore_data = docstore.get("docstore/data", {})
    if not isinstance(docstore_data, dict):
        raise RuntimeError("docstore/data missing or invalid in docstore.json")

    out.write(',"text_id_to_ref_doc_id":{')
    first = True
    kept_ids: list[str] = []
    missing = 0

    for i, node_id in enumerate(recovered_ids, 1):
        raw_node = docstore_data.get(node_id)
        if raw_node is None:
            missing += 1
            continue
        ref_doc_id, _metadata = _node_metadata_and_ref_doc(raw_node)
        first = _write_json_member(out, first, node_id, ref_doc_id)
        kept_ids.append(node_id)
        if progress_every and i % progress_every == 0:
            print(f"Rebuilt ref-doc map for {i:,} recovered ids...", file=sys.stderr, flush=True)

    out.write('},"metadata_dict":{')
    first = True
    for i, node_id in enumerate(kept_ids, 1):
        _ref_doc_id, metadata = _node_metadata_and_ref_doc(docstore_data[node_id])
        first = _write_json_member(out, first, node_id, metadata)
        if progress_every and i % progress_every == 0:
            print(f"Rebuilt metadata map for {i:,} valid ids...", file=sys.stderr, flush=True)
    out.write("}}")
    return len(kept_ids), missing


def validate_vector_store(path: Path) -> None:
    print(f"Validating repaired vector store JSON and schema: {path}", file=sys.stderr, flush=True)
    store = SimpleVectorStore.from_persist_path(str(path))
    data = store.data
    counts = (
        len(data.embedding_dict),
        len(data.text_id_to_ref_doc_id),
        len(data.metadata_dict),
    )
    print(f"Validated counts: embeddings={counts[0]:,}, ref_docs={counts[1]:,}, metadata={counts[2]:,}", file=sys.stderr)
    if len(set(counts)) != 1:
        raise RuntimeError(f"Repaired vector store has inconsistent counts: {counts}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-dir", default=os.path.expanduser("~/Library/Application Support/ChatEKLD/obsidian_storage"))
    parser.add_argument("--progress-every", type=int, default=10_000)
    parser.add_argument("--replace", action="store_true", help="Replace default__vector_store.json after validation")
    parser.add_argument(
        "--promote-existing",
        action="store_true",
        help="Validate and promote an existing default__vector_store.json.repaired without re-running recovery",
    )
    args = parser.parse_args()

    storage_dir = Path(args.storage_dir)
    vector_store_path = storage_dir / "default__vector_store.json"
    docstore_path = storage_dir / "docstore.json"
    repaired_path = storage_dir / "default__vector_store.json.repaired"
    pre_repair_copy = storage_dir / "default__vector_store.json.pre-repair"

    if not vector_store_path.exists():
        raise FileNotFoundError(vector_store_path)
    if not docstore_path.exists():
        raise FileNotFoundError(docstore_path)
    if repaired_path.exists() and not args.promote_existing:
        repaired_path.unlink()

    if args.promote_existing:
        if not repaired_path.exists():
            raise FileNotFoundError(repaired_path)
        print(f"Promoting existing repaired candidate {repaired_path}...", file=sys.stderr, flush=True)
    else:
        print(f"Recovering complete embeddings from {vector_store_path}...", file=sys.stderr, flush=True)
        with repaired_path.open("w", encoding="utf-8") as out:
            recovered_ids = recover_embedding_dict(vector_store_path, out, args.progress_every)
            kept, missing = write_rebuilt_maps(out, docstore_path, recovered_ids, args.progress_every)

        print(
            f"Wrote repaired candidate with {kept:,} valid embeddings "
            f"({missing:,} recovered ids missing from docstore) to {repaired_path}",
            file=sys.stderr,
            flush=True,
        )
    validate_vector_store(repaired_path)

    if args.replace or args.promote_existing:
        if pre_repair_copy.exists():
            raise RuntimeError(f"Refusing to overwrite existing {pre_repair_copy}")
        shutil.move(str(vector_store_path), str(pre_repair_copy))
        shutil.move(str(repaired_path), str(vector_store_path))
        print(f"Replaced vector store. Original moved to {pre_repair_copy}", file=sys.stderr)
    else:
        print("Dry run complete. Re-run with --replace to swap the repaired file into place.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
