"""Real serialized stores, synthetic nodes; no manager/recovery or disk writes."""
import threading
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from llama_index.core.schema import TextNode, QueryBundle
from llama_index.core.storage.docstore import SimpleDocumentStore

from rag.docstore_access import node_count
from rag.sidecars import RetrievalCacheMixin


class Caches(RetrievalCacheMixin):
    def __init__(self, store):
        self._index = SimpleNamespace(docstore=store)
        self._index_mutation_lock = threading.Lock()
        self._bm25_build_lock = threading.Lock()
        self._wikilink_build_lock = threading.Lock()
        self._bm25_retriever = self._wikilink_index = None
        self._bm25_cached_doc_count = self._wikilink_cached_doc_count = -1

    def _load_bm25_sidecar(self, *args):
        return None

    def _load_wikilink_sidecar(self, *args):
        return None

    def _persist_bm25_sidecar(self, *args):
        pass

    def _persist_wikilink_sidecar(self, *args):
        pass


def store():
    ds = SimpleDocumentStore(namespace="fixture")
    ds.add_documents([
        TextNode(id_="a", text="alpha psychiatry [[b]]", metadata={"source": "a.md"}),
        TextNode(id_="b", text="beta pharmacology", metadata={"source": "b.md"}),
        TextNode(id_="c", text="gamma psychotherapy", metadata={"source": "c.md"}),
    ])
    return ds



@pytest.fixture(autouse=True, params=["memory", "disk"])
def docstore_backend(request, monkeypatch, tmp_path):
    """Replay cache/ranking/invalidation contracts against both real stores."""
    if request.param == "memory":
        yield
        return
    from rag.disk_docstore import DiskKVStore
    original = store
    opened = []
    def disk_store():
        source = tmp_path / f"store-{len(opened)}.json"
        original().persist(str(source))
        kv = DiskKVStore.load(str(source), str(tmp_path / "scratch"))
        opened.append(kv)
        return SimpleDocumentStore(kv, namespace="fixture")
    monkeypatch.setattr(sys.modules[__name__], "store", disk_store)
    try:
        yield
    finally:
        for kv in opened:
            kv.close()


def test_real_store_hot_cache_never_gets_all_or_deserializes():
    ds = store()
    manager = Caches(ds)
    bm = manager._get_bm25_retriever(2)
    graph = manager._get_wikilink_index()
    from rag.engine import BM25Retriever
    baseline = BM25Retriever.from_defaults(nodes=list(ds.docs.values()), similarity_top_k=2)
    expected = [(n.node.node_id, n.score) for n in baseline.retrieve(QueryBundle("pharmacology"))]
    with patch.object(ds._kvstore, "get_all", side_effect=AssertionError("full read")), patch(
        "llama_index.core.storage.docstore.keyval_docstore.json_to_doc",
        side_effect=AssertionError("deserialize"),
    ):
        assert node_count(ds) == 3
        assert manager._get_bm25_retriever(7) is bm
        assert manager._get_wikilink_index() is graph
    assert [(n.node.node_id, n.score) for n in bm.retrieve(QueryBundle("pharmacology"))] == expected
    assert graph.outbound("a.md") == ["b.md"]


@pytest.mark.parametrize("change", ["insert", "delete", "same_count", "replace", "partial"])
def test_mutations_refresh_both_caches(change):
    ds = store()
    manager = Caches(ds)
    old_bm = manager._get_bm25_retriever(2)
    old_graph = manager._get_wikilink_index()
    if change == "replace":
        manager._index = SimpleNamespace(docstore=store())
    else:
        try:
            with manager._mutate_index():
                if change == "delete":
                    ds.delete_document("c")
                else:
                    ds.add_documents([TextNode(id_="a" if change == "same_count" else "d",
                                              text="delta [[c]]", metadata={"source": "d.md"})])
                if change == "partial":
                    raise RuntimeError("after write")
        except RuntimeError:
            pass
    assert manager._get_bm25_retriever(2) is not old_bm
    assert manager._get_wikilink_index() is not old_graph
    assert manager._bm25_cached_doc_count == node_count(manager._index.docstore)


def test_empty_absent_and_unknown_store_fallback():
    manager = Caches(SimpleDocumentStore())
    assert manager._get_bm25_retriever(2) is None
    assert manager._get_wikilink_index() is None
    manager._index = None
    assert manager._get_wikilink_index() is None
    assert node_count(SimpleNamespace(docs={"legacy": object()})) == 1


def test_replacement_at_constant_count_cannot_reload_previous_sidecar():
    manager = Caches(store())
    manager._get_bm25_retriever(2)
    manager._get_wikilink_index()
    manager._index = SimpleNamespace(docstore=store())
    with patch.object(manager, "_load_bm25_sidecar", side_effect=AssertionError("old disk cache")), patch.object(
        manager, "_load_wikilink_sidecar", side_effect=AssertionError("old disk cache")
    ):
        assert manager._get_bm25_retriever(2) is not None
        assert manager._get_wikilink_index() is not None


def test_snapshot_mutation_during_build_is_not_persisted_or_stamped_current():
    from rag.engine import BM25Retriever
    manager = Caches(store())
    original = BM25Retriever.from_defaults
    def build(**kwargs):
        with manager._mutate_index():
            manager._index.docstore.add_documents([
                TextNode(id_="a", text="new text [[c]]", metadata={"source": "a.md"})])
        return original(**kwargs)
    with patch.object(BM25Retriever, "from_defaults", side_effect=build), patch.object(
        manager, "_persist_bm25_sidecar"
    ) as persist:
        previous = manager._get_bm25_retriever(2)
        persist.assert_not_called()
    assert manager._get_bm25_retriever(2) is not previous


def test_historical_simple_kv_mapping_shape_is_localized():
    ds = store()
    ds._kvstore._data = ds._kvstore._collections_mappings
    del ds._kvstore._collections_mappings
    with patch.object(ds._kvstore, "get_all", side_effect=AssertionError("deserialize")):
        assert node_count(ds) == 3


def test_publication_epoch_detects_even_reused_identity():
    manager = Caches(store())
    old_bm = manager._get_bm25_retriever(2)
    old_graph = manager._get_wikilink_index()
    # Same object emulates allocator reuse of both index and store identities.
    manager._replace_index(manager._index)
    assert manager._get_bm25_retriever(2) is not old_bm
    assert manager._get_wikilink_index() is not old_graph
