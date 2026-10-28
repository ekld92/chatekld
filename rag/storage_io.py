"""Low-level on-disk helpers for the vault index store.

Extracted from ``rag/vault.py`` (Plan 9, step 0) as a **leaf** module: stdlib
only, no project imports, no module-level state that any test patches. It holds
the file primitives that the index meta / manifest / signature writers and the
retrieval-cache sidecars all share, so ``rag/sidecars.py`` can be lifted out of
``rag/vault.py`` without either importing the other.

``rag/vault.py`` re-exports every name here, so ``from rag.vault import
_write_json_atomic`` keeps working unchanged.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path


def _write_json_atomic(path: str, data: dict) -> None:
    """Write *data* as JSON to *path* atomically using a sibling temp file.

    Uses the same tempfile+os.replace() pattern as _save_pdf_cache_file so
    a crash or SIGTERM mid-write can never leave the file empty or corrupt.

    Adds a flush+fsync before the rename for crash *durability* (not just
    atomicity): this writer persists the index meta / indexed-materials manifest /
    PDF-signature cache — low-frequency (checkpoint cadence + final persist), so
    the fsync cost is negligible, and a torn meta on power loss could leave the
    on-disk index state unrecoverable. The per-image/per-PDF *description* cache
    (regenerable) uses the separate _atomic_write_text writer, which deliberately
    does NOT fsync — it runs per document during a multi-hour index and its loss
    only costs a re-extraction, not correctness.
    """
    dir_ = os.path.dirname(path) or "."
    os.makedirs(dir_, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".json")
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _json_file_has_complete_tail(path: Path) -> bool:
    """Cheap EOF sanity check for large JSON files.

    This does not prove the whole file is valid JSON, but it catches the
    failure mode that prompted the repair tooling: a checkpoint truncated in
    the middle of a vector array.
    """
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return False
            f.seek(max(0, size - 4096))
            tail = f.read().rstrip()
        return tail.endswith(b"}")
    except OSError:
        return False


def _simple_vector_store_has_any_embedding(path: Path) -> bool:
    """Return True when SimpleVectorStore's embedding_dict is non-empty."""
    prefixes = (b'{"embedding_dict": {', b'{"embedding_dict":{')
    try:
        with path.open("rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    for prefix in prefixes:
        if head.startswith(prefix):
            pos = len(prefix)
            while pos < len(head) and head[pos:pos + 1] in b" \t\r\n":
                pos += 1
            return pos < len(head) and head[pos:pos + 1] != b"}"
    return False


def vault_key(vault_root) -> str:
    """Stable per-vault cache key: the first 16 hex of sha256(str(root)).

    The ONE definition behind ``ObsidianVaultManager._vault_cache_key`` (which
    delegates here) and the paper-label index file name, so both name the same
    vault the same way. Callers pass an already-resolved root.
    """
    return hashlib.sha256(str(vault_root).encode("utf-8")).hexdigest()[:16]
