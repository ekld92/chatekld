"""AP-09 residual: the cross-encoder is shared, so its inference must be bounded.

Reranking used to run inside ``engine.query()`` under ``_index_mutation_lock``,
which serialised it as a side effect. AP-09 moved it out of the lock but hands
each request only a shallow ``copy.copy`` FACADE — the loaded model underneath
is one manager-level singleton — so two chats could call ``.predict()`` on the
same torch model at once. That is new behaviour nothing else bounds, and on
Apple Silicon it doubles peak activation memory in the pool the LLM shares.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from llama_index.core.schema import NodeWithScore, TextNode

import rag.engine as engine


class _FakeCrossEncoder:
    """Stands in for SentenceTransformerRerank, recording overlap."""

    def __init__(self) -> None:
        self.concurrent = 0
        self.max_concurrent = 0
        self._mu = threading.Lock()

    def postprocess_nodes(self, nodes, query_bundle=None):
        with self._mu:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            threading.Event().wait(0.05)
        finally:
            with self._mu:
                self.concurrent -= 1
        return nodes


def _nodes():
    return [NodeWithScore(node=TextNode(text="chunk"), score=1.0)]


def test_shared_cross_encoder_inference_is_serialised(monkeypatch):
    shared = _FakeCrossEncoder()
    # _postprocess_nodes gates on the real class, so make the fake match it for
    # the duration of the test rather than weakening the production predicate.
    monkeypatch.setattr(engine, "SentenceTransformerRerank", _FakeCrossEncoder)

    def run():
        engine.SimpleQueryEngine._postprocess_nodes("q", _nodes(), [shared])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _i: run(), range(4)))

    assert shared.max_concurrent == 1, (
        "concurrent chats must not run inference on the shared cross-encoder "
        f"simultaneously (observed {shared.max_concurrent})"
    )


def test_non_reranker_postprocessors_stay_concurrent():
    """The gate must not serialise pure score filters."""
    plain = _FakeCrossEncoder()  # not the gated type once the patch is gone

    def run():
        engine.SimpleQueryEngine._postprocess_nodes("q", _nodes(), [plain])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _i: run(), range(4)))

    assert plain.max_concurrent > 1, (
        "ungated postprocessors must keep running concurrently"
    )
