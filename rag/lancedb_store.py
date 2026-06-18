"""LanceDB binary vector store for the Obsidian vault index (Batch 4).

Replaces LlamaIndex's JSON ``SimpleVectorStore`` on disk. Self-contained parity
layer so that neither ``rag/engine.py`` nor the embed model need any
backend-specific branching:

* **Cosine parity.** LanceDB's default search metric is L2, while
  ``SimpleVectorStore`` ranks by cosine similarity. For unit-length vectors
  ``‖a-b‖² = 2(1 - cos(a,b))``, so L2 ordering is identical to cosine ordering.
  We therefore unit-normalize embeddings on insert *and* the query vector on
  search — verified bit-for-bit rank-identical to the legacy store in the
  Batch-4 spike. (Magnitude is never needed: cosine discards it.)
* **Metadata.** LanceDB rejects non-scalar metadata values (same as Chroma), so
  list/dict values (e.g. a markdown chunk's ``attachments``) are JSON-stringified
  on a per-node copy before insert. With ``store_nodes_override=True`` the
  docstore keeps the original list, which is what the app actually reads back —
  the stringified copy only lives in the vector-store row.

The module imports cleanly even when ``lancedb`` is absent (``lancedb_available()``
returns False); callers fall back to the legacy backend. This mirrors the BM25 /
reranker import-guard discipline in ``rag/engine.py``.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any

# Layout inside OBSIDIAN_INDEX_DIR. Kept in one place so the migration script,
# the indexer, the integrity checks, and /api/reset all agree.
LANCEDB_SUBDIR = "lancedb"
LANCEDB_TABLE = "vectors"
VECTOR_BACKEND_LANCEDB = "lancedb"
VECTOR_BACKEND_SIMPLE = "simple"

try:  # optional dependency — guarded exactly like BM25 / sbert-rerank
    import lancedb  # type: ignore[import-not-found]
    from llama_index.vector_stores.lancedb import (  # type: ignore[import-not-found]
        LanceDBVectorStore,
    )
except Exception:  # pragma: no cover - exercised only when the extra is absent
    lancedb = None  # type: ignore[assignment]
    LanceDBVectorStore = None  # type: ignore[assignment,misc]


def lancedb_available() -> bool:
    """True when the lancedb backend can be constructed."""
    return LanceDBVectorStore is not None


def is_lancedb_store(vector_store: Any) -> bool:
    """True if *vector_store* is LanceDB-backed (so MMR must run client-side).

    Covers both the base ``LanceDBVectorStore`` and our normalizing subclass.
    Returns False for ``SimpleVectorStore``, ``None``, and anything else — those
    keep LlamaIndex's native MMR path. Safe when lancedb is not installed.
    """
    return LanceDBVectorStore is not None and isinstance(vector_store, LanceDBVectorStore)


def lancedb_dir(index_dir: str) -> str:
    """Absolute path to the LanceDB database directory inside *index_dir*."""
    return os.path.join(index_dir, LANCEDB_SUBDIR)


def _unit(vec: Any) -> list[float]:
    """Return *vec* scaled to unit L2 norm (zero vector returned unchanged)."""
    total = 0.0
    out = []
    for x in vec:
        fx = float(x)
        out.append(fx)
        total += fx * fx
    norm = math.sqrt(total)
    if norm == 0.0:
        return out
    return [x / norm for x in out]


if LanceDBVectorStore is not None:

    class NormalizingLanceDBVectorStore(LanceDBVectorStore):  # type: ignore[misc]
        """``LanceDBVectorStore`` with cosine-parity normalization + metadata shim.

        Both transforms are applied on copies; the caller's original node objects
        (and therefore the docstore) are never mutated.
        """

        def add(self, nodes: list, **add_kwargs: Any) -> list[str]:  # type: ignore[override]
            prepared = []
            for node in nodes:
                update: dict[str, Any] = {"embedding": _unit(node.get_embedding())}
                meta = node.metadata or {}
                shimmed = None
                for key, value in meta.items():
                    if isinstance(value, (list, dict)):
                        if shimmed is None:
                            shimmed = dict(meta)
                        shimmed[key] = json.dumps(value, ensure_ascii=False)
                if shimmed is not None:
                    update["metadata"] = shimmed
                prepared.append(node.model_copy(update=update))
            return super().add(prepared, **add_kwargs)

        def query(self, query: Any, **kwargs: Any) -> Any:  # type: ignore[override]
            if getattr(query, "query_embedding", None) is not None:
                query.query_embedding = _unit(query.query_embedding)
            return super().query(query, **kwargs)

else:  # pragma: no cover - placeholder when the extra is absent
    NormalizingLanceDBVectorStore = None  # type: ignore[assignment,misc]


def make_lancedb_vector_store(index_dir: str) -> "NormalizingLanceDBVectorStore":
    """Construct the normalizing LanceDB store rooted at *index_dir*.

    Raises ``RuntimeError`` if lancedb is not importable so callers surface a
    clear message instead of an ``AttributeError`` on ``None``.
    """
    if NormalizingLanceDBVectorStore is None:
        raise RuntimeError(
            "The 'lancedb' vector backend is configured but lancedb is not "
            "installed. Install it (pip install lancedb "
            "llama-index-vector-stores-lancedb) or set vault_vector_backend to "
            "'simple'."
        )
    return NormalizingLanceDBVectorStore(
        uri=lancedb_dir(index_dir), table_name=LANCEDB_TABLE
    )


def lancedb_table_count(index_dir: str) -> int:
    """Row count of the persisted table, or -1 when it is absent/unreadable.

    Cheap-ish: opens the table and reads its manifest row count (no scan). Used
    by the integrity / has-data checks on the lancedb path, mirroring
    ``_simple_vector_store_has_any_embedding`` for the JSON store.
    """
    if lancedb is None:
        return -1
    db_dir = lancedb_dir(index_dir)
    if not os.path.isdir(db_dir):
        return -1
    try:
        # open_table raises if the table is absent → -1 (no table_names() call,
        # which is deprecated in newer lancedb).
        return int(lancedb.connect(db_dir).open_table(LANCEDB_TABLE).count_rows())
    except Exception:
        return -1
