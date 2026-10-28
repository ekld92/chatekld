"""Existing persisted stores remain queryable without rebuilding or embedding."""
import hashlib
from unittest import mock

import pytest
from llama_index.core import StorageContext, VectorStoreIndex
from llama_index.core.embeddings import MockEmbedding
from llama_index.core.schema import QueryBundle, TextNode


def _hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('backend', ['simple', 'lancedb'])
def test_existing_index_preserves_bytes_ids_metadata_and_vectors(tmp_path, monkeypatch, backend):
    from rag import vault
    from rag.lancedb_store import lancedb_available, make_lancedb_vector_store
    if backend == 'lancedb' and not lancedb_available():
        pytest.skip('LanceDB optional dependency unavailable')
    persisted = tmp_path / 'index'
    persisted.mkdir()
    monkeypatch.setattr(vault, 'OBSIDIAN_INDEX_DIR', str(persisted))
    monkeypatch.setattr(vault, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'scratch'))
    nodes = [TextNode(id_=f'old.md::fixture{i}', text=f'Existing text {i}',
                      embedding=[1., float(i), 0., 0.],
                      metadata={'source': 'old.md', 'tags': ['fixture'], 'page_start': 1})
             for i in range(3)]
    embed = MockEmbedding(embed_dim=4)
    context = StorageContext.from_defaults(**(
        {'vector_store': make_lancedb_vector_store(str(persisted))} if backend == 'lancedb' else {}))
    index = VectorStoreIndex(nodes, storage_context=context, embed_model=embed,
                             store_nodes_override=True)
    context.persist(persist_dir=str(persisted))
    before = _hashes(persisted)
    query = QueryBundle(query_str='fixture', embedding=[1., 0., 0., 0.])
    expected = index.as_retriever(similarity_top_k=3).retrieve(query)
    manager = vault.ObsidianVaultManager()
    with mock.patch.object(MockEmbedding, 'get_text_embedding_batch', side_effect=AssertionError('embedding')), \
         mock.patch.object(MockEmbedding, 'get_query_embedding', side_effect=AssertionError('embedding')), \
         mock.patch.object(VectorStoreIndex, 'from_documents', side_effect=AssertionError('rebuild')), \
         mock.patch.object(manager, 'index_vault', side_effect=AssertionError('reindex')):
        loaded = manager._build_index_for_backend(fresh=False, backend=backend, embed_model=embed)
        try:
            actual = loaded.as_retriever(similarity_top_k=3).retrieve(query)
            assert [(r.node.node_id, r.node.text, r.node.metadata, r.score) for r in actual] == [
                (r.node.node_id, r.node.text, r.node.metadata, r.score) for r in expected]
            assert _hashes(persisted) == before
        finally:
            store = loaded.docstore._kvstore
            if hasattr(store, 'close'):
                store.close()
    assert _hashes(persisted) == before
