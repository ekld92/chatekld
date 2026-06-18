"""Batch 4 — LanceDB binary vector store: migration, parity, and backend logic.

Hermetic (root conftest pins CHATEKLD_BASE_DIR to a temp dir); each test scopes
its own index dir by patching rag.vault.OBSIDIAN_INDEX_DIR. A small deterministic
embedding gives distinct, stable vectors within a process so ranking can be
compared exactly. Skips cleanly if lancedb is not installed.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from rag.lancedb_store import (
    VECTOR_BACKEND_LANCEDB,
    is_lancedb_store,
    lancedb_available,
    lancedb_dir,
    lancedb_table_count,
    make_lancedb_vector_store,
)

if not lancedb_available():  # pragma: no cover
    raise unittest.SkipTest("lancedb not installed")

from llama_index.core import (
    Document,
    StorageContext,
    VectorStoreIndex,
    load_index_from_storage,
)
from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.indices.vector_store.retrievers import VectorIndexRetriever
from llama_index.core.schema import NodeWithScore, TextNode
from llama_index.core.vector_stores.simple import SimpleVectorStore

DIM = 12


def _raw_vec(seed: str) -> list[float]:
    return list(np.random.default_rng(abs(hash(seed)) % (2**32)).standard_normal(DIM))


class _DetEmbed(BaseEmbedding):
    """Deterministic (within a process) non-normalized text->vector embedding."""

    def _get_query_embedding(self, query: str) -> list[float]:
        return _raw_vec(query)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return _raw_vec(text)


DOCS = [
    ("alpha", "dense retrieval ranks candidates by cosine similarity", None),
    ("beta", "bm25 performs lexical keyword matching over the corpus", ["paper.pdf", "fig.png"]),
    ("gamma", "a cross encoder reranker narrows the candidate pool", None),
    ("delta", "lancedb stores embeddings in apache arrow columnar files", ["arch.md"]),
    ("eps", "obsidian vault notes link ideas with wikilinks", None),
    ("zeta", "reciprocal rank fusion merges dense and lexical results", None),
]
QUERY = "how are dense vector candidates ranked"


def _build_simple_index(index_dir: str) -> VectorStoreIndex:
    nodes = []
    for nid, text, att in DOCS:
        meta = {"source": f"{nid}.md"}
        if att is not None:
            meta["attachments"] = att
        nodes.append(TextNode(id_=nid, text=text, metadata=meta))
    idx = VectorStoreIndex(nodes, embed_model=_DetEmbed())
    idx.storage_context.persist(persist_dir=index_dir)
    Path(index_dir, "obsidian_meta.json").write_text(
        json.dumps({"version": 1, "embed": "det", "vector_backend": "simple",
                    "has_vector_data": True, "indexed_at": "t0"}),
        encoding="utf-8",
    )
    return idx


def _topk_ids(index, k=5):
    r = VectorIndexRetriever(index=index, similarity_top_k=k, embed_model=_DetEmbed())
    return [n.node.node_id for n in r.retrieve(QUERY)]


class TestLanceDBStoreLayer(unittest.TestCase):
    def test_add_unit_normalizes_and_stringifies_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            vs = make_lancedb_vector_store(d)
            node = TextNode(id_="x", text="t", metadata={"source": "s.md", "attachments": ["a.pdf", "b.png"]})
            node.embedding = [3.0, 4.0] + [0.0] * (DIM - 2)  # norm 5
            vs.add([node])
            # original node untouched (docstore keeps the list)
            self.assertEqual(node.metadata["attachments"], ["a.pdf", "b.png"])
            got = vs.get_nodes(node_ids=["x"])[0]
            norm = sum(v * v for v in got.embedding) ** 0.5
            self.assertAlmostEqual(norm, 1.0, places=5)
            self.assertEqual(got.metadata["attachments"], json.dumps(["a.pdf", "b.png"]))

    def test_query_normalizes_query_vector(self):
        with tempfile.TemporaryDirectory() as d:
            vs = make_lancedb_vector_store(d)
            n = TextNode(id_="x", text="t", metadata={})
            n.embedding = [1.0] + [0.0] * (DIM - 1)
            vs.add([n])
            from llama_index.core.vector_stores.types import VectorStoreQuery
            q = VectorStoreQuery(query_embedding=[10.0] + [0.0] * (DIM - 1), similarity_top_k=1)
            res = vs.query(q)
            self.assertEqual(res.ids, ["x"])
            self.assertAlmostEqual(sum(v * v for v in q.query_embedding) ** 0.5, 1.0, places=5)

    def test_is_lancedb_store_discriminates(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(is_lancedb_store(make_lancedb_vector_store(d)))
        self.assertFalse(is_lancedb_store(SimpleVectorStore()))
        self.assertFalse(is_lancedb_store(None))
        self.assertFalse(is_lancedb_store(object()))

    def test_table_count_missing_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(lancedb_table_count(d), -1)  # no table yet


class TestMigration(unittest.TestCase):
    def test_migration_count_parity_bak_and_meta(self):
        from scripts.migrate_vector_store import migrate
        with tempfile.TemporaryDirectory() as d:
            _build_simple_index(d)
            with open(Path(d, "default__vector_store.json"), "rb") as f:
                embedding_dict = json.load(f)["embedding_dict"]
            n_vectors = len(embedding_dict)

            rc = migrate(d, batch_size=512, keep_json=False)
            self.assertEqual(rc, 0)
            self.assertEqual(lancedb_table_count(d), n_vectors)
            self.assertTrue(Path(d, "default__vector_store.json.bak").exists())
            self.assertFalse(Path(d, "default__vector_store.json").exists())
            meta = json.loads(Path(d, "obsidian_meta.json").read_text())
            self.assertEqual(meta["vector_backend"], VECTOR_BACKEND_LANCEDB)

    def test_migration_is_idempotent(self):
        from scripts.migrate_vector_store import migrate
        with tempfile.TemporaryDirectory() as d:
            _build_simple_index(d)
            self.assertEqual(migrate(d, batch_size=512, keep_json=False), 0)
            # second run: legacy JSON gone, lancedb present -> clean no-op
            self.assertEqual(migrate(d, batch_size=512, keep_json=False), 0)
            self.assertEqual(lancedb_table_count(d), len(DOCS))

    def test_retrieval_ranking_parity_after_migration(self):
        from scripts.migrate_vector_store import migrate
        with tempfile.TemporaryDirectory() as d:
            simple_idx = _build_simple_index(d)
            baseline = _topk_ids(simple_idx)

            migrate(d, batch_size=512, keep_json=False)
            vs = make_lancedb_vector_store(d)
            ctx = StorageContext.from_defaults(persist_dir=d, vector_store=vs)
            lidx = load_index_from_storage(ctx, embed_model=_DetEmbed(), store_nodes_override=True)
            self.assertEqual(_topk_ids(lidx), baseline)  # cosine == normalized-L2 ranking
            # store_nodes_override keeps the docstore populated (BM25/hash-checks)
            self.assertEqual(len(lidx.docstore.docs), len(DOCS))
            # attachments survive as a list in the docstore (not the JSON string)
            self.assertEqual(lidx.docstore.get_node("beta").metadata["attachments"],
                             ["paper.pdf", "fig.png"])


class TestBackendAwareIndexVault(unittest.TestCase):
    """Drive the real ObsidianVaultManager.index_vault on the lancedb backend."""

    def _full_config(self, backend):
        from core.config import load_config as real
        cfg = dict(real())
        cfg["vault_vector_backend"] = backend
        return cfg

    def _index(self, manager, index_dir):
        from llama_index.core.embeddings import MockEmbedding

        class FakeProvider:
            def get_embedding(self, _name):
                return MockEmbedding(embed_dim=8)

        with (
            patch("rag.vault.get_provider", return_value=FakeProvider()),
            patch("rag.vault.load_config", return_value=self._full_config("lancedb")),
            patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
        ):
            manager.index_vault("llm", "embed", provider_name="ollama")
        return manager.drain_status_messages()

    def test_fresh_lancedb_build_then_no_reembed(self):
        from rag.vault import ObsidianVaultManager
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, \
             tempfile.TemporaryDirectory() as index_dir:
            Path(vault_dir, "a.md").write_text("# A\n\nfirst note body", encoding="utf-8")
            Path(vault_dir, "b.md").write_text("# B\n\nsecond note body", encoding="utf-8")
            manager = ObsidianVaultManager()
            manager.restore_vault_path(vault_dir)

            msgs1 = self._index(manager, index_dir)
            self.assertEqual(manager.get_status(), "done", msgs1)
            meta = json.loads(Path(index_dir, "obsidian_meta.json").read_text())
            self.assertEqual(meta["vector_backend"], "lancedb")
            self.assertTrue(Path(lancedb_dir(index_dir)).is_dir())
            self.assertGreater(lancedb_table_count(index_dir), 0)
            self.assertFalse(Path(index_dir, "default__vector_store.json").exists())
            rows = lancedb_table_count(index_dir)

            # second run over the unchanged vault: every chunk skipped, no
            # re-embed, table unchanged (the no-re-embed acceptance gate).
            manager2 = ObsidianVaultManager()
            manager2.restore_vault_path(vault_dir)
            msgs2 = self._index(manager2, index_dir)
            self.assertEqual(manager2.get_status(), "done", msgs2)
            joined = " ".join(msgs2)
            self.assertIn("0 embedded", joined, joined)
            self.assertEqual(lancedb_table_count(index_dir), rows)

    def test_crash_drift_upsert_replaces_orphan_not_duplicates(self):
        """Faithful crash simulation at the indexer level: the lancedb table is
        ahead of the docstore (a chunk's row is durable but its docstore node was
        lost before the checkpoint). On resume that chunk is re-processed as
        "new"; lancedb_upsert must delete-before-insert so the row is replaced,
        not duplicated. Without the flag it would duplicate — asserted too."""
        from rag.vault import ObsidianVaultManager
        from llama_index.core import Document
        from llama_index.core.embeddings import MockEmbedding

        def seed_orphan(index_dir):
            mgr = ObsidianVaultManager()
            with patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir):
                idx = mgr._build_index_for_backend(
                    fresh=True, backend="lancedb", embed_model=MockEmbedding(embed_dim=8)
                )
                idx.insert(Document(text="body text", doc_id="note.md::deadbeef"))
                # Wipe the docstore entry only — the lancedb row stays (drift).
                idx.docstore.delete_ref_doc("note.md::deadbeef", raise_error=False)
                self.assertEqual(lancedb_table_count(index_dir), 1)
                return mgr, idx

        # With upsert: orphan replaced, exactly one row remains.
        with tempfile.TemporaryDirectory() as index_dir:
            with patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir):
                mgr, idx = seed_orphan(index_dir)
                chunk = Document(text="body text", doc_id="note.md::deadbeef")
                added, _, _, _, _ = mgr._index_documents_streaming(
                    idx, iter([chunk]), None, lancedb_upsert=True
                )
                self.assertEqual(added, 1)
                self.assertEqual(lancedb_table_count(index_dir), 1)

        # Without upsert (the bug it guards against): the orphan duplicates.
        with tempfile.TemporaryDirectory() as index_dir2:
            with patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir2):
                mgr, idx = seed_orphan(index_dir2)
                chunk = Document(text="body text", doc_id="note.md::deadbeef")
                mgr._index_documents_streaming(
                    idx, iter([chunk]), None, lancedb_upsert=False
                )
                self.assertEqual(lancedb_table_count(index_dir2), 2)


class TestClientSideMMR(unittest.TestCase):
    def _mk(self, nid, emb):
        n = TextNode(id_=nid, text=nid)
        n.embedding = emb
        return NodeWithScore(node=n, score=0.0)

    def _wrapper(self, threshold):
        from rag.engine import _ClientSideMMRRetriever
        from llama_index.core.retrievers import BaseRetriever

        nodes = [self._mk("A", [1.0, 0.0, 0.0]),
                 self._mk("B", [0.98, 0.2, 0.0]),   # near-duplicate of A
                 self._mk("C", [0.0, 1.0, 0.0])]    # diverse

        class Inner(BaseRetriever):
            def _retrieve(self, qb):
                return list(nodes)

        class Embed:
            def get_query_embedding(self, q):
                return [1.0, 0.0, 0.0]

        return _ClientSideMMRRetriever(Inner(), Embed(), threshold, top_k=2)

    def test_high_lambda_favours_relevance(self):
        ids = [n.node.node_id for n in self._wrapper(0.9).retrieve("q")]
        self.assertEqual(ids, ["A", "B"])

    def test_low_lambda_favours_diversity(self):
        ids = [n.node.node_id for n in self._wrapper(0.1).retrieve("q")]
        self.assertEqual(ids, ["A", "C"])

    def test_engine_routes_lancedb_to_client_side_mmr(self):
        """A lancedb-backed index must NOT get the native vector_store_query_mode;
        a SimpleVectorStore index must."""
        from rag.engine import SimpleQueryEngine, _ClientSideMMRRetriever

        def run_with_index(index):
            engine = SimpleQueryEngine(
                index=index, llm_name="l", embed_name="e", top_k=4,
                provider_name="ollama", mmr_enabled=True, mmr_lambda=0.5,
            )
            with patch("rag.engine.load_config", return_value={"context_window": 8192}), \
                 patch("rag.engine.VectorIndexRetriever") as dense_cls, \
                 patch("rag.engine.RetrieverQueryEngine") as qe, \
                 patch("rag.engine.get_provider"):
                dense_cls.return_value = object()
                qe.from_args.return_value.query.return_value = "ok"
                engine.query("hello")
                return dense_cls.call_args.kwargs

        with tempfile.TemporaryDirectory() as d:
            vs = make_lancedb_vector_store(d)
            n = TextNode(id_="x", text="t", metadata={})
            n.embedding = [1.0] + [0.0] * (DIM - 1)
            vs.add([n])
            lance_idx = VectorStoreIndex.from_vector_store(vs, embed_model=_DetEmbed())
            kwargs = run_with_index(lance_idx)
            self.assertIsNone(kwargs.get("vector_store_query_mode"))  # client-side

        simple_idx = _build_simple_index_in_memory()
        kwargs = run_with_index(simple_idx)
        self.assertEqual(kwargs.get("vector_store_query_mode"), "mmr")  # native


def _build_simple_index_in_memory():
    nodes = [TextNode(id_="n1", text="hello", metadata={})]
    return VectorStoreIndex(nodes, embed_model=_DetEmbed())


if __name__ == "__main__":
    unittest.main()
