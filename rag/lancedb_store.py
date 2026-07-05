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
from datetime import timedelta
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


# Stable projection of *user* metadata keys that the LanceDB row carries as
# flat struct columns.  LanceDB stores a node's metadata as a single struct
# column whose field set is frozen when the table is first created (from the
# MD-first loader, so these are exactly the keys an MD chunk emits).  LanceDB
# then rejects any later insert whose metadata introduces a field the struct
# lacks ("field 'X' does not exist in table schema"), which is what broke
# large-PDF range chunks (`page_start`/`page_end`) and vault-image chunks
# (`is_image`).  This constant is only the *fresh-table fallback*; for an
# existing table the live schema is authoritative (see
# ``_allowed_metadata_keys``).  Dropping a key from this flat projection loses
# nothing the read path uses: the full node — every metadata key — is still
# persisted in the docstore (``store_nodes_override=True``) and in the row's
# own ``_node_content`` blob; the flat columns exist only for filtering, which
# the vault retrieval path does not use.
_LANCE_FLAT_METADATA_KEYS = frozenset(
    {"file_path", "source", "extension", "header_path", "attachments"}
)


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

        def _allowed_metadata_keys(self) -> frozenset[str]:
            """User-metadata keys the persisted table can accept as struct columns.

            An existing table's own schema is authoritative — we never present a
            field it does not already have, so a schema-drift insert error is
            structurally impossible.  A not-yet-created table (``_table is None``)
            falls back to the fixed projection, which then *becomes* the created
            schema.  ``_table`` is populated by the integration's ``__init__``
            when it opens an existing table, so this is reliable on the very
            first ``add`` to an existing table, not only after one insert.
            Reading ``schema`` is an in-memory metadata access (no table scan);
            ``add`` is called once per chunk, so we recompute rather than cache
            (avoids a pydantic private-attr declaration for negligible cost).
            """
            tbl = getattr(self, "_table", None)
            if tbl is None:
                return _LANCE_FLAT_METADATA_KEYS
            try:
                # The struct field iterates as its child Fields; their names are
                # every metadata key the table currently stores (user keys plus
                # the integration's own _node_content/_node_type/etc., which are
                # never present in node.metadata so they are irrelevant below).
                return frozenset(f.name for f in tbl.schema.field("metadata").type)
            except Exception:
                # Schema unreadable for any reason — fall back to the fixed
                # projection rather than risk presenting an unknown field.
                return _LANCE_FLAT_METADATA_KEYS

        def add(self, nodes: list, **add_kwargs: Any) -> list[str]:  # type: ignore[override]
            """Insert nodes after unit-normalizing vectors and projecting metadata.

            For each node, on a ``model_copy`` (the caller's node — and so the
            docstore — is never mutated): unit-normalize the embedding for cosine
            parity, drop any metadata key the persisted struct schema lacks
            (``_allowed_metadata_keys``, the schema-drift guard), and
            JSON-stringify list/dict values LanceDB cannot store. The copy is
            reused verbatim when neither transform applies (the common MD/PDF
            chunk), avoiding needless allocation.
            """
            allowed = self._allowed_metadata_keys()
            prepared = []
            for node in nodes:
                update: dict[str, Any] = {"embedding": _unit(node.get_embedding())}
                meta = node.metadata or {}
                # Project metadata onto the table's stable column set: drop any
                # key the schema lacks (e.g. page_start/page_end/is_image) and
                # JSON-stringify list/dict values (LanceDB rejects non-scalars).
                # ``changed`` stays False — and the original metadata is reused
                # verbatim — when neither transform is needed, preserving the
                # pre-fix copy for the common MD/PDF chunk.  The docstore keeps
                # the untouched node, so dropped keys are not lost globally.
                projected: dict[str, Any] = {}
                changed = False
                for key, value in meta.items():
                    if key not in allowed:
                        changed = True  # drop a field absent from the schema
                        continue
                    if isinstance(value, (list, dict)):
                        projected[key] = json.dumps(value, ensure_ascii=False)
                        changed = True
                    else:
                        projected[key] = value
                if changed:
                    update["metadata"] = projected
                prepared.append(node.model_copy(update=update))
            return super().add(prepared, **add_kwargs)

        def query(self, query: Any, **kwargs: Any) -> Any:  # type: ignore[override]
            """Unit-normalize the query vector so L2 search ranks by cosine.

            Mirrors the insert-side normalization: with both the stored vectors
            and the query vector on the unit sphere, LanceDB's default-L2 nearest
            neighbours are exactly the cosine nearest neighbours, making ranking
            bit-for-bit identical to the legacy ``SimpleVectorStore``.
            """
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


def compact_lancedb_vector_store(vector_store: Any) -> bool:
    """Compact data fragments and prune superseded versions on the LIVE table.

    The streaming indexer calls ``idx.insert`` once per chunk; on LanceDB each
    insert is its own transaction → one new single-row data fragment **and** one
    new version manifest, and every manifest re-lists every fragment, so manifest
    storage in ``_versions/`` grows ~O(n²) (66k inserts measured at 209 GB over
    <1 GB of real vectors).  Periodically merging the single-row fragments
    (``optimize``) collapses them, and ``cleanup_older_than=timedelta(0)`` prunes
    every superseded version, bounding ``_versions/`` to O(n).

    Operates on the live open ``_table`` (no second connection).  Best-effort —
    returns ``False`` on any failure, on the simple backend, or when the table is
    not yet open, and never raises into the caller (the indexer must not abort a
    multi-hour run because a compaction hiccup).  The caller holds
    ``_index_mutation_lock`` (which the retrieval path also takes), so no
    concurrent reader observes the pruned versions mid-optimize.
    """
    tbl = getattr(vector_store, "_table", None)
    if tbl is None:
        return False
    try:
        tbl.optimize(cleanup_older_than=timedelta(0))
        return True
    except Exception:
        return False


def lancedb_list_doc_ids(index_dir: str) -> list[str]:
    """Return all node IDs stored in the LanceDB table ([] when absent/error).

    Projects the ``id`` column through the underlying lance dataset
    (``to_lance().to_table(columns=[...])``) — a bare ``Table.to_arrow()``
    materialises EVERY column including the embedding vectors, i.e. the whole
    multi-GB store in RAM, which is exactly what the binary backend exists to
    avoid. The id column alone is a few MB even on large vaults.
    """
    if lancedb is None:
        return []
    db_dir = lancedb_dir(index_dir)
    if not os.path.isdir(db_dir):
        return []
    try:
        tbl = lancedb.connect(db_dir).open_table(LANCEDB_TABLE)
        return tbl.to_lance().to_table(columns=["id"]).column("id").to_pylist()
    except Exception:
        return []


def _sql_string_literal(value: str) -> str:
    """Quote *value* as a DataFusion SQL string literal for a delete predicate.

    Single quotes with ``''`` doubling — the standard SQL string form. Node
    IDs embed vault-relative paths, and this vault is French: apostrophes in
    filenames (``l'étude.md::…``) are the common case, stray double quotes
    the rare one; both must survive quoting rather than truncate the
    predicate (a truncated predicate could delete the WRONG rows).
    """
    return "'" + value.replace("'", "''") + "'"


def lancedb_delete_ids(vector_store: Any, ids: list[str]) -> tuple[bool, str]:
    """Delete rows by ID from the live LanceDB table (batches of 500).

    Returns ``(ok, detail)`` — *detail* is "" on success and a short
    diagnostic otherwise, so the caller can LOG the failure instead of
    silently carrying on with orphan rows still in the table. A failed batch
    falls back to per-id deletes so one malformed id cannot block the other
    499 in its batch.
    """
    tbl = getattr(vector_store, "_table", None)
    if tbl is None:
        return False, "vector store has no live LanceDB table"
    if not ids:
        return True, ""
    failed: list[str] = []
    last_error = ""
    batch_size = 500
    for i in range(0, len(ids), batch_size):
        chunk = ids[i : i + batch_size]
        id_str = ", ".join(_sql_string_literal(val) for val in chunk)
        try:
            tbl.delete(f"id IN ({id_str})")
        except Exception as exc:
            last_error = str(exc)
            for val in chunk:
                try:
                    tbl.delete(f"id = {_sql_string_literal(val)}")
                except Exception as one_exc:
                    last_error = str(one_exc)
                    failed.append(val)
    if failed:
        return False, f"{len(failed)} id(s) failed to delete (last error: {last_error})"
    return True, ""

