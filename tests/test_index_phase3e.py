"""Phase 3E changed-chunk deletion batching and retrieval lock regressions."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

import pytest
from llama_index.core import Document as LlamaDocument, StorageContext, VectorStoreIndex
from llama_index.core.embeddings import MockEmbedding
from llama_index.core.indices.vector_store.retrievers import VectorIndexRetriever
from llama_index.core.schema import QueryBundle

from rag.engine import SimpleQueryEngine
from rag.lancedb_store import lancedb_available, make_lancedb_vector_store
from rag.vault import ObsidianVaultManager, VECTOR_BACKEND_LANCEDB


def _docs(count: int) -> list[LlamaDocument]:
    return [LlamaDocument(text=f"new-{i}", doc_id=f"d{i}.md::0",
                          metadata={"source": f"d{i}.md"})
            for i in range(count)]


class _Docstore:
    def __init__(self, docs):
        self.ids = [doc.doc_id for doc in docs]
        self.deleted_refs = []

    def get_document_hash(self, _doc_id):
        return "old-hash"

    def get_all_ref_doc_info(self):
        return {doc_id: SimpleNamespace(node_ids=[f"node-{doc_id}"])
                for doc_id in self.ids}

    def get_ref_doc_info(self, doc_id):
        return SimpleNamespace(node_ids=[f"node-{doc_id}"])

    def delete_ref_doc(self, doc_id, raise_error=False):
        self.deleted_refs.append(doc_id)


class _Index:
    def __init__(self, docs, *, fail_batch=False, fail_single=None,
                 stop_after_delete=None, fail_insert=False):
        self.docstore = _Docstore(docs)
        self.delete_nodes_calls = []
        self.delete_ref_calls = []
        self.inserted = []
        self.fail_batch = fail_batch
        self.fail_single = fail_single
        self.stop_after_delete = stop_after_delete
        self.fail_insert = fail_insert

    def delete_nodes(self, node_ids, delete_from_docstore=False):
        self.delete_nodes_calls.append((list(node_ids), delete_from_docstore))
        if self.fail_batch:
            raise RuntimeError("batch delete failed")
        if self.stop_after_delete is not None:
            self.stop_after_delete.set()

    def delete_ref_doc(self, doc_id, delete_from_docstore=False):
        self.delete_ref_calls.append((doc_id, delete_from_docstore))
        if doc_id == self.fail_single:
            raise RuntimeError("single delete failed")

    def insert(self, doc):
        if self.fail_insert:
            raise RuntimeError("insert failed")
        self.inserted.append(doc.doc_id)


def _run(manager, index, docs, batch_size=64):
    with mock.patch.object(ObsidianVaultManager, "_embed_batch_size",
                           return_value=batch_size), \
            mock.patch.object(ObsidianVaultManager,
                              "_FAILURE_BACKOFF_BASE_S", 0), \
            mock.patch.object(ObsidianVaultManager,
                              "_FAILURE_BACKOFF_CAP_S", 0):
        return manager._index_documents_streaming(
            index, iter(docs), vector_backend=VECTOR_BACKEND_LANCEDB)


def test_changed_lancedb_chunks_use_one_bounded_delete_nodes_round_trip():
    docs = _docs(40)
    manager = ObsidianVaultManager()
    index = _Index(docs)
    added, skipped, _deleted, failed, _manifest = _run(
        manager, index, docs, batch_size=40)

    assert (added, skipped, failed) == (40, 0, 0)
    assert len(index.delete_nodes_calls) == 1
    node_ids, delete_from_docstore = index.delete_nodes_calls[0]
    assert delete_from_docstore is True
    assert node_ids == [f"node-{doc.doc_id}" for doc in docs]
    assert index.delete_ref_calls == []
    assert index.inserted == [doc.doc_id for doc in docs]


def test_batch_delete_failure_retries_per_chunk_and_isolates_bad_row():
    docs = _docs(6)
    manager = ObsidianVaultManager()
    index = _Index(docs, fail_batch=True, fail_single=docs[2].doc_id)
    added, _skipped, _deleted, failed, manifest = _run(
        manager, index, docs, batch_size=6)

    assert len(index.delete_nodes_calls) == 1
    assert len(index.delete_ref_calls) == 6
    assert (added, failed) == (5, 1)
    assert docs[2].doc_id not in index.inserted
    assert docs[2].metadata["source"] not in manifest


def test_cancel_after_batch_delete_reports_every_removed_chunk_as_gap():
    docs = _docs(8)
    manager = ObsidianVaultManager()
    index = _Index(docs, stop_after_delete=manager._stop_event)
    added, _skipped, _deleted, _failed, _manifest = _run(
        manager, index, docs, batch_size=8)

    assert added == 0
    assert index.inserted == []
    warnings = [message for message in manager._status_messages
                if "could not be re-embedded" in message]
    assert warnings and "8 changed chunk(s)" in warnings[-1]
    manager._stop_event.clear()


def test_failed_insert_after_batch_delete_reports_every_removed_chunk_as_gap():
    docs = _docs(7)
    manager = ObsidianVaultManager()
    index = _Index(docs, fail_insert=True)
    with mock.patch.object(ObsidianVaultManager,
                           "_MAX_CONSECUTIVE_FAILURES", 3):
        added, _skipped, _deleted, failed, _manifest = _run(
            manager, index, docs, batch_size=7)

    assert (added, failed) == (0, 3)
    warnings = [message for message in manager._status_messages
                if "could not be re-embedded" in message]
    assert warnings and "7 changed chunk(s)" in warnings[-1]
    manager._stop_event.clear()


class _HashEmbedding(MockEmbedding):
    """Small deterministic embedder for backend parity/concurrency tests."""

    def _vec(self, text):
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [byte / 255.0 for byte in digest[:8]]

    def _get_text_embedding(self, text):
        return self._vec(text)

    def _get_query_embedding(self, query):
        return self._vec(query)


def _real_index(backend, tmp_path):
    embed = _HashEmbedding(embed_dim=8)
    kwargs = {"embed_model": embed}
    if backend == "lancedb":
        if not lancedb_available():
            pytest.skip("lancedb not installed")
        vector_store = make_lancedb_vector_store(str(tmp_path / "lance"))
        kwargs.update(
            storage_context=StorageContext.from_defaults(vector_store=vector_store),
            store_nodes_override=True,
        )
    docs = [
        LlamaDocument(
            text=f"alpha evidence section {i}",
            doc_id=f"seed-{i}.md",
            metadata={"source": f"seed-{i}.md"},
        )
        for i in range(6)
    ]
    return VectorStoreIndex.from_documents(docs, **kwargs), embed


@pytest.mark.parametrize("backend", ["simple", "lancedb"])
def test_prepared_snapshot_preserves_backend_rank_source_and_score(backend, tmp_path):
    """AP-09 staged reads remain retrieval-eval identical on both stores."""
    index, embed = _real_index(backend, tmp_path)
    engine = SimpleQueryEngine.__new__(SimpleQueryEngine)

    legacy_retriever = VectorIndexRetriever(
        index=index, similarity_top_k=4, embed_model=embed
    )
    legacy = legacy_retriever.retrieve(QueryBundle("alpha evidence"))

    staged_retriever = VectorIndexRetriever(
        index=index, similarity_top_k=4, embed_model=embed
    )
    prepared = engine._prepare_retrieval(
        staged_retriever, QueryBundle("alpha evidence")
    )
    staged = [engine._detach_node(node) for node in engine._read_prepared(prepared)]

    def _projection(nodes):
        return [
            (
                node.node.metadata.get("source"),
                node.node.get_content(),
                pytest.approx(float(node.score or 0.0), abs=1e-9),
            )
            for node in nodes
        ]

    assert _projection(staged) == _projection(legacy)
    assert all(a.node is not b.node for a, b in zip(staged, legacy))


@pytest.mark.parametrize("backend", ["simple", "lancedb"])
def test_insert_delete_and_prepared_retrieve_interleave_safely(backend, tmp_path):
    """Readers see complete lock epochs while bounded writers churn both stores."""
    index, embed = _real_index(backend, tmp_path)
    manager = ObsidianVaultManager()
    engine = SimpleQueryEngine.__new__(SimpleQueryEngine)

    def _read_once(_iteration):
        retriever = VectorIndexRetriever(
            index=index, similarity_top_k=3, embed_model=embed
        )
        prepared = engine._prepare_retrieval(retriever, QueryBundle("alpha"))
        with manager._index_mutation_lock:
            nodes = engine._read_prepared(prepared)
            detached = [engine._detach_node(node) for node in nodes]
        return [node.node.node_id for node in detached]

    def _write_once(iteration):
        doc_id = f"transient-{iteration}.md"
        with manager._index_mutation_lock:
            index.insert(
                LlamaDocument(
                    text=f"alpha transient {iteration}",
                    doc_id=doc_id,
                    metadata={"source": doc_id},
                )
            )
            index.delete_ref_doc(doc_id, delete_from_docstore=True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = []
        for iteration in range(12):
            futures.append(pool.submit(_read_once, iteration))
            futures.append(pool.submit(_write_once, iteration))
        results = [future.result(timeout=20) for future in futures]

    read_results = results[::2]
    assert len(read_results) == 12
    assert all(result for result in read_results)
    assert all(len(result) == len(set(result)) for result in read_results)


def test_manager_holds_mutation_lock_only_for_snapshot_read():
    """Rewrite/embed setup and rerank-like finish work stay outside the lock."""
    manager = ObsidianVaultManager()
    lock_state = {"held": False}
    order = []

    class _TracingLock:
        def acquire(self, **_kwargs):
            assert not lock_state["held"]
            lock_state["held"] = True
            order.append("lock")
            return True

        def release(self):
            assert lock_state["held"]
            lock_state["held"] = False
            order.append("unlock")

    class _Engine:
        def prepare(self, message):
            assert message == "q" and not lock_state["held"]
            order.append("prepare")
            return "prepared"

        def read_snapshot(self, prepared):
            assert prepared == "prepared" and lock_state["held"]
            order.append("read")
            return ["detached"]

        def finish_retrieve(self, prepared, nodes):
            assert prepared == "prepared" and nodes == ["detached"]
            assert not lock_state["held"]
            order.append("finish")
            return []

    manager._index_mutation_lock = _TracingLock()
    with mock.patch.object(
        manager, "_build_engine", return_value=(_Engine(), lambda _message: None)
    ):
        assert manager.retrieve("q", llm_name="m", embed_name="e") == []

    assert order == ["prepare", "lock", "read", "unlock", "finish"]
