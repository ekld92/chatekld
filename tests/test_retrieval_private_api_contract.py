"""Pin the LlamaIndex private surface `rag/engine.py::_prepare_retrieval` needs.

AP-09 split retrieval into prepare → locked read → finish. To keep the legacy
call graph while moving the remote work out of the lock, `_prepare_retrieval`
and `_read_prepared` reach into LlamaIndex internals and reimplement
`QueryFusionRetriever._retrieve`'s fusion dispatch. That is a deliberate
coupling to UNDERSCORED members of a third-party package, so a point release
that renames one breaks vault chat at runtime.

The rest of the retrieval suite mocks at or above the retriever boundary and
therefore cannot see such a rename. This file constructs the REAL objects and
fails loudly at upgrade time instead. If it goes red after a dependency bump,
the fix is to re-read the new LlamaIndex source and update `_prepare_retrieval`
— never to delete the assertion.
"""

from __future__ import annotations

import inspect

import pytest
from llama_index.core import VectorStoreIndex
from llama_index.core.embeddings import MockEmbedding
from llama_index.core.indices.vector_store.retrievers import VectorIndexRetriever
from llama_index.core.retrievers import BaseRetriever, QueryFusionRetriever
from llama_index.core.schema import TextNode

from rag.engine import _mock_llm


class _StubRetriever(BaseRetriever):
    def _retrieve(self, query_bundle):  # pragma: no cover - never invoked
        return []


@pytest.fixture(scope="module")
def fusion() -> QueryFusionRetriever:
    return QueryFusionRetriever(
        retrievers=[_StubRetriever(), _StubRetriever()],
        llm=_mock_llm(),
        num_queries=1,
    )


@pytest.mark.parametrize("name", [
    "_get_queries",              # prepare(): the query-rewrite LLM call
    "_retrievers",               # prepare(): one child plan per leg
    "_reciprocal_rerank_fusion",  # read: fusion dispatch, mode-by-mode
    "_relative_score_fusion",
    "_simple_fusion",
    "num_queries",               # prepare(): whether to expand at all
    "similarity_top_k",          # read: the post-fusion trim
    "mode",                      # read: which fusion function to call
])
def test_query_fusion_retriever_exposes(fusion, name):
    assert hasattr(fusion, name), (
        f"QueryFusionRetriever.{name} disappeared; rag/engine.py::"
        "_prepare_retrieval/_read_prepared depend on it"
    )


def test_fusion_functions_take_the_results_mapping(fusion):
    """The reimplemented dispatch passes a {(query, leg_index): nodes} mapping."""
    for name in ("_reciprocal_rerank_fusion", "_relative_score_fusion",
                 "_simple_fusion"):
        params = list(inspect.signature(getattr(fusion, name)).parameters)
        assert params[:1] == ["results"], (
            f"{name}{tuple(params)} no longer starts with `results`; the "
            "fusion dispatch in _read_prepared must be re-derived"
        )


def test_vector_index_retriever_exposes_the_embedding_seam():
    """prepare() precomputes the query embedding through these two members."""
    index = VectorStoreIndex(nodes=[TextNode(text="a")],
                             embed_model=MockEmbedding(embed_dim=4))
    retriever = index.as_retriever()
    assert isinstance(retriever, VectorIndexRetriever)
    for name in ("_needs_embedding", "_embed_model"):
        assert hasattr(retriever, name), (
            f"VectorIndexRetriever.{name} disappeared; the query embedding "
            "would fall back inside the index mutation lock"
        )
    assert hasattr(retriever._embed_model, "get_agg_embedding_from_queries")
