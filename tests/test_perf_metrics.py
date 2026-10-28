"""Bounded diagnostics exercised only with simulated components."""
import threading
from types import SimpleNamespace
from unittest.mock import patch

import flask
import pytest
from llama_index.core.schema import QueryBundle

from core import perf_metrics as perf
from api.sse import consumer_stall_s, run_sse_worker
from rag import engine as engine_module
from rag.engine import SimpleQueryEngine
from rag.vault import ObsidianVaultManager


@pytest.fixture(autouse=True)
def metrics(monkeypatch):
    monkeypatch.setenv("PAPERMIND_PERF_METRICS", "1")
    perf.clear()
    yield
    perf.clear()


def test_disabled_and_bounded(monkeypatch):
    monkeypatch.delenv("PAPERMIND_PERF_METRICS")
    with patch.object(perf.time, "monotonic", side_effect=AssertionError("disabled clock")):
        with perf.span("test.disabled"):
            pass
    assert perf.snapshot() == []
    monkeypatch.setenv("PAPERMIND_PERF_METRICS", "1")
    for _ in range(300):
        with perf.span("test.bounded"):
            pass
    assert len(perf.snapshot()) == 256


def test_exclusive_time_and_errors():
    with patch.object(perf.time, "monotonic", side_effect=[0., 1., 3., 5.]):
        with perf.span("parent"):
            with perf.span("child"):
                pass
    child, parent = perf.snapshot()
    assert child["elapsed_s"] == 2
    assert parent["elapsed_s"] == 5
    assert parent["exclusive_s"] == 3
    with pytest.raises(ValueError), perf.span("failure"):
        raise ValueError("secret prompt not captured")
    assert perf.snapshot()[-1]["failed"] is True
    assert "secret" not in str(perf.snapshot())


def test_embedding_search_rerank_and_lock_events(monkeypatch):
    class Vector:
        def _needs_embedding(self):
            return True
        _embed_model = SimpleNamespace(get_agg_embedding_from_queries=lambda _: [1., 0.])
        def retrieve(self, bundle):
            assert bundle.embedding == [1., 0.]
            return []
    class Rerank:
        def postprocess_nodes(self, nodes, **kwargs):
            return nodes
    monkeypatch.setattr(engine_module, "VectorIndexRetriever", Vector)
    monkeypatch.setattr(engine_module, "SentenceTransformerRerank", Rerank)
    engine = SimpleQueryEngine.__new__(SimpleQueryEngine)
    plan = engine._prepare_retrieval(Vector(), QueryBundle("PRIVATE_QUERY"))
    assert engine._read_prepared(plan) == []
    assert engine._postprocess_nodes("PRIVATE_QUERY", [], [Rerank()]) == []
    manager = ObsidianVaultManager.__new__(ObsidianVaultManager)
    manager._index_mutation_lock = threading.Lock()
    manager._acquire_retrieval_lock()
    manager._index_mutation_lock.release()
    stages = {event["stage"] for event in perf.snapshot()}
    assert {"query.embedding", "search.dense", "retrieval.rerank",
            "reranker.lock.wait", "reranker.lock.hold", "index.retrieval.wait"} <= stages
    assert "PRIVATE_QUERY" not in str(perf.snapshot())


def test_server_budget_and_first_token_error_order():
    assert consumer_stall_s({}) == 390
    assert consumer_stall_s({"online_timeout_s": 150, "online_max_retries": 3}) == 660
    assert consumer_stall_s({"online_timeout_s": 150, "online_max_retries": 3}, 900) == 930
    app = flask.Flask(__name__)
    finished = threading.Event()
    def worker(put, cancel):
        try:
            for _ in range(3):
                put({"info": "simulated attempt"})
            put({"token": "PRIVATE_PARTIAL"})
            put({"token": " more"})
            put({"error": "simulated failure"})
        finally:
            finished.set()
    with app.test_request_context("/"):
        response = run_sse_worker(worker, consumer_timeout_s=512)
    assert response.headers["X-PaperMind-SSE-Silence-S"] == "512"
    data = "".join(response.response)
    response.close()
    assert finished.wait(1)
    assert data.index("PRIVATE_PARTIAL") < data.index("simulated failure") < data.index("[DONE]")
    events = perf.snapshot()
    assert sum(e["stage"] == "sse.first_token_emit" for e in events) == 1
    assert sum(e["stage"] == "sse.stream_total" for e in events) == 1
    assert "PRIVATE_PARTIAL" not in str(events)


def test_health_probe_is_nonblocking_and_serialization_free():
    from llama_index.core.storage.docstore import SimpleDocumentStore
    manager = ObsidianVaultManager.__new__(ObsidianVaultManager)
    manager._index_mutation_lock = threading.Lock()
    ds = SimpleDocumentStore()
    manager._index = SimpleNamespace(docstore=ds)
    with patch.object(ds._kvstore, "get_all", side_effect=AssertionError("full read")):
        assert manager.docstore_doc_count() == 0
        manager._index_mutation_lock.acquire()
        try:
            assert manager.docstore_doc_count() is None
        finally:
            manager._index_mutation_lock.release()


def test_simulated_dense_bm25_fusion_events(monkeypatch):
    class Dense:
        def _needs_embedding(self):
            return False
        def retrieve(self, bundle):
            return ["dense-source"]
    class Lexical:
        def retrieve(self, bundle):
            return ["lexical-source"]
    class Fusion:
        _retrievers = [Dense(), Lexical()]
        num_queries = 1
        mode = "reciprocal_rerank"
        similarity_top_k = 2
        def _reciprocal_rerank_fusion(self, results):
            return [node for values in results.values() for node in values]
    monkeypatch.setattr(engine_module, "VectorIndexRetriever", Dense)
    monkeypatch.setattr(engine_module, "BM25Retriever", Lexical)
    monkeypatch.setattr(engine_module, "QueryFusionRetriever", Fusion)
    engine = SimpleQueryEngine.__new__(SimpleQueryEngine)
    plan = engine._prepare_retrieval(Fusion(), QueryBundle("PRIVATE_QUERY"))
    assert engine._read_prepared(plan) == ["dense-source", "lexical-source"]
    stages = {event["stage"] for event in perf.snapshot()}
    assert {"search.dense", "search.bm25", "retrieval.read_fusion"} <= stages
    assert "source" not in str(perf.snapshot())


def test_client_mmr_separates_vector_and_selection_timings():
    from llama_index.core.schema import NodeWithScore, TextNode
    from rag.engine import _ClientSideMMRRetriever
    nodes = [NodeWithScore(node=TextNode(text='a', embedding=[1., 0.]), score=.9),
             NodeWithScore(node=TextNode(text='b', embedding=[0., 1.]), score=.5)]
    inner = SimpleNamespace(retrieve=lambda bundle: nodes)
    retriever = _ClientSideMMRRetriever(inner, None, .2, 1)
    found = retriever._retrieve(QueryBundle('private', embedding=[1., 0.]))
    assert len(found) == 1 and found[0].node.text == 'a'
    events = perf.snapshot()
    assert {'search.vector', 'retrieval.mmr'} <= {event['stage'] for event in events}
    assert 'private' not in str(events)
