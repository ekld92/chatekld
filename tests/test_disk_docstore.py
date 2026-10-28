"""Real LlamaIndex contracts on disposable stores, never a live library."""
import asyncio
import gc
import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
from llama_index.core import StorageContext, VectorStoreIndex
from llama_index.core.embeddings import MockEmbedding
from llama_index.core.schema import TextNode, NodeRelationship, RelatedNodeInfo
from llama_index.core.storage.docstore import SimpleDocumentStore

from rag.disk_docstore import DiskKVStore, _JSONReader, load_disk_docstore
from rag.docstore_access import node_count


def fixture_store(path):
    store = SimpleDocumentStore()
    nodes = [TextNode(id_=f'n{i}', text=f'Texte é 😃 {i}', metadata={
        'source': f'note-{i}.md', 'page_start': i, 'tags': ['a', 'b'],
        'nested': {'score': 0.12345678901234567, 'optional': None}},
        relationships={NodeRelationship.SOURCE: RelatedNodeInfo(node_id=f'd{i}')}) for i in range(3)]
    store.add_documents(nodes)
    store.set_document_hash('d0', 'hash-0')
    store._kvstore.put('extra', {'unknown': [1, True, None]}, 'custom.collection')
    store.persist(str(path))
    return store, nodes


def test_full_docstore_parity_mutation_and_upstream_reload(tmp_path):
    path = tmp_path / 'docstore.json'
    original, nodes = fixture_store(path)
    before = path.read_bytes()
    disk = load_disk_docstore(str(path), str(tmp_path / 'scratch'))
    try:
        assert disk.to_dict() == original.to_dict()
        assert disk.get_all_ref_doc_info() == original.get_all_ref_doc_info()
        assert disk.get_document_hash('d0') == 'hash-0'
        assert [n.to_dict() for n in disk.get_nodes(['n2', 'n0'])] == [n.to_dict() for n in original.get_nodes(['n2', 'n0'])]
        for store in (disk, original):
            store.delete_ref_doc('d1')
            store.add_documents([TextNode(id_='new', text='replacement', metadata={'list': [3, 4]})])
            store.set_document_hash('new', 'updated')
        assert disk.to_dict() == original.to_dict()
        assert node_count(disk) == node_count(original) == 3
        output = tmp_path / 'published' / 'docstore.json'
        with mock.patch.object(disk._kvstore, 'to_dict', side_effect=AssertionError('bulk persist')):
            disk.persist(str(output))
        assert SimpleDocumentStore.from_persist_path(str(output)).to_dict() == original.to_dict()
        assert path.read_bytes() == before
    finally:
        disk._kvstore.close()
    assert not list((tmp_path / 'scratch').iterdir())


def test_hot_count_and_point_reads_never_bulk_decode(tmp_path):
    path = tmp_path / 'docstore.json'
    original, _ = fixture_store(path)
    disk = load_disk_docstore(str(path), str(tmp_path / 'scratch'))
    try:
        with mock.patch.object(disk._kvstore, 'get_all', side_effect=AssertionError('full mapping')):
            assert node_count(disk) == 3
            assert disk.get_node('n1').to_dict() == original.get_node('n1').to_dict()
            disk.delete_document('n1')
            assert node_count(disk) == 2
    finally:
        disk._kvstore.close()


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'utf-16', 'utf-32'])
def test_json_semantics_and_duplicates_across_encodings(tmp_path, encoding):
    source = '{"empty":{},"c":{"a":{"n":0},"a":{"n":1}},"c":{"x":{"v":[1e400,NaN,123456789012345678901234567890,0.1,"é\\ud800"]}}}'
    path = tmp_path / 'docstore.json'
    path.write_bytes(source.encode(encoding))
    store = DiskKVStore.load(str(path), str(tmp_path / 'scratch'))
    try:
        assert json.dumps(store.to_dict()) == json.dumps(json.loads(source))
        assert store.count('c') == 1
        assert store.get('a', 'c') is None
    finally:
        store.close()


@pytest.mark.parametrize('source', ['{"c":{"a":{"n":', '{"c":{},}', '{"c":{"a":{}}} trailing', '[]', '{"c":{"a":null}}'])
def test_broken_store_fails_and_removes_scratch(tmp_path, source):
    path = tmp_path / 'docstore.json'
    path.write_text(source)
    with pytest.raises(json.JSONDecodeError):
        DiskKVStore.load(str(path), str(tmp_path / 'scratch'))
    assert path.read_text() == source
    assert not list((tmp_path / 'scratch').iterdir())


def test_streaming_decoder_handles_chunk_boundaries_without_unbounded_reads():
    class TinyReader(io.StringIO):
        def read(self, size=-1):
            assert 0 < size <= 65536
            return super().read(min(size, 7))
    data = {'collection': {'escaped"key': {'text': '\\"é😃' * 1000}, 'last': {'n': 1e-30}}}
    reader = _JSONReader(TinyReader(json.dumps(data, ensure_ascii=False)))
    loaded = {}
    for collection in reader.keys():
        loaded[collection] = {key: reader.value() for key in reader.keys()}
    assert not reader.peek()
    assert loaded == data


def test_concurrent_point_access_and_async_apis(tmp_path):
    store = DiskKVStore(str(tmp_path / 'scratch'))
    def work(i):
        store.put(str(i), {'n': i}, 'nodes')
        assert store.get(str(i), 'nodes') == {'n': i}
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(work, range(100)))
        assert store.count('nodes') == 100
        async def run():
            await store.aput('async', {'x': 1}, 'nodes')
            assert await store.aget('async', 'nodes') == {'x': 1}
            assert await store.adelete('async', 'nodes')
        asyncio.run(run())
        assert store.count('nodes') == 100
        assert not store.delete('absent', 'nodes')
    finally:
        store.close()


def test_gc_disposes_retired_store_without_touching_other_copies(tmp_path):
    work = tmp_path / 'scratch'
    first = DiskKVStore(str(work))
    second = DiskKVStore(str(work))
    assert len(list(work.iterdir())) == 2
    del first
    gc.collect()
    assert len(list(work.iterdir())) == 1
    second.put('still', {'alive': True})
    second.close()
    assert not list(work.iterdir())


def test_normal_process_exit_removes_live_scratch_copy(tmp_path):
    """Interpreter finalizers must clean even a still-referenced loaded store."""
    scratch = tmp_path / 'scratch'
    env = dict(os.environ, CHATEKLD_BASE_DIR=str(tmp_path / 'base'))
    result = subprocess.run(
        [sys.executable, '-c',
         'import sys; from rag.disk_docstore import DiskKVStore; '
         'store = DiskKVStore(sys.argv[1]); '
         'store.put("fixture", {"text": "synthetic"}); '
         'assert store.get("fixture")["text"] == "synthetic"', str(scratch)],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not list(scratch.iterdir())


def test_lancedb_manager_load_retrieve_mutate_and_publish(tmp_path, monkeypatch):
    from rag import vault
    from rag.lancedb_store import make_lancedb_vector_store, lancedb_available
    if not lancedb_available():
        pytest.skip('LanceDB optional dependency absent')
    persisted = tmp_path / 'index'
    persisted.mkdir()
    scratch = tmp_path / 'scratch'
    monkeypatch.setattr(vault, 'OBSIDIAN_INDEX_DIR', str(persisted))
    monkeypatch.setattr(vault, 'OBSIDIAN_CACHE_DIR', str(scratch))
    embedding = MockEmbedding(embed_dim=4)
    context = StorageContext.from_defaults(vector_store=make_lancedb_vector_store(str(persisted)))
    nodes = [TextNode(id_=f'n{i}', text=f'Node {i}', metadata={'source':f'n{i}.md','tags':['a']}) for i in range(4)]
    index = VectorStoreIndex(nodes, storage_context=context, embed_model=embedding, store_nodes_override=True)
    context.persist(persist_dir=str(persisted))
    expected = index.as_retriever(similarity_top_k=3).retrieve('query')
    manager = vault.ObsidianVaultManager()
    with mock.patch.object(SimpleDocumentStore, 'from_persist_dir', side_effect=AssertionError('full JSON load')):
        loaded = manager._build_index_for_backend(fresh=False, backend='lancedb', embed_model=embedding)
    try:
        actual = loaded.as_retriever(similarity_top_k=3).retrieve('query')
        assert [(n.node.to_dict(), n.score) for n in actual] == [(n.node.to_dict(), n.score) for n in expected]
        loaded.insert_nodes([TextNode(id_='new', text='new document')])
        loaded.delete_nodes(['n0'], delete_from_docstore=True)
        assert node_count(loaded.docstore) == 4
        loaded.storage_context.persist(persist_dir=str(persisted))
        reloaded = SimpleDocumentStore.from_persist_dir(str(persisted))
        assert reloaded.to_dict() == loaded.docstore.to_dict()
    finally:
        loaded.docstore._kvstore.close()


def test_storage_context_failure_disposes_only_its_temporary_copy(tmp_path, monkeypatch):
    from rag import vault
    persisted = tmp_path / 'index'
    persisted.mkdir()
    fixture_store(persisted / 'docstore.json')
    scratch = tmp_path / 'scratch'
    monkeypatch.setattr(vault, 'OBSIDIAN_INDEX_DIR', str(persisted))
    monkeypatch.setattr(vault, 'OBSIDIAN_CACHE_DIR', str(scratch))
    monkeypatch.setattr(vault, 'make_lancedb_vector_store', lambda _: object())
    with mock.patch.object(StorageContext, 'from_defaults', side_effect=RuntimeError('broken index store')):
        with pytest.raises(RuntimeError, match='broken index store'):
            vault.ObsidianVaultManager()._build_storage_context(fresh=False, backend='lancedb')
    assert not list(scratch.iterdir())
    assert SimpleDocumentStore.from_persist_dir(str(persisted)).get_node('n0').node_id == 'n0'


def test_disk_full_during_import_has_no_published_store_or_leaked_copy(tmp_path):
    import sqlite3
    source = tmp_path / 'docstore.json'
    fixture_store(source)
    before = source.read_bytes()
    with mock.patch.object(DiskKVStore, 'put', side_effect=sqlite3.OperationalError('database or disk is full')):
        with pytest.raises(sqlite3.OperationalError, match='disk is full'):
            DiskKVStore.load(str(source), str(tmp_path / 'scratch'))
    assert source.read_bytes() == before
    assert not list((tmp_path / 'scratch').iterdir())


def test_failed_sqlite_write_preserves_existing_records_and_count(tmp_path):
    import sqlite3
    store = DiskKVStore(str(tmp_path / 'scratch'))
    try:
        store.put('retained', {'value': 'before'})
        before = store.to_dict()
        pages = store._db.execute('PRAGMA page_count').fetchone()[0]
        store._db.execute(f'PRAGMA max_page_count={int(pages)}')
        with pytest.raises(sqlite3.OperationalError, match='full'):
            store.put('oversized', {'text': 'x' * 1048576})
        assert store.to_dict() == before
        assert store.count('data') == 1
        assert store._db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        store.close()


def test_ownership_lock_preserves_live_store_and_sweeps_unowned(tmp_path):
    import fcntl
    from pathlib import Path
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    # Create a simulated crashed directory with an unlocked .owner.lock
    crashed_with_lock = scratch / "docstore-crashed1"
    crashed_with_lock.mkdir()
    (crashed_with_lock / "records.sqlite").write_text("dummy")
    (crashed_with_lock / ".owner.lock").touch()

    # Create a simulated legacy directory without .owner.lock
    crashed_legacy = scratch / "docstore-legacy2"
    crashed_legacy.mkdir()
    (crashed_legacy / "records.sqlite").write_text("dummy")

    # Create a symlink docstore directory (should be skipped)
    target_dir = tmp_path / "target_dir"
    target_dir.mkdir()
    symlink_dir = scratch / "docstore-symlink"
    symlink_dir.symlink_to(target_dir, target_is_directory=True)

    # Initialize a live DiskKVStore - this should sweep crashed_with_lock and crashed_legacy
    live1 = DiskKVStore(str(scratch))
    try:
        assert live1._lock_fd is not None
        # Verify flock is held on live1's .owner.lock
        live1_dir = Path(live1._tmpdir.name)
        live1_lock = live1_dir / ".owner.lock"
        assert live1_lock.exists()
        probe_fd = os.open(live1_lock, os.O_RDWR)
        try:
            with pytest.raises((BlockingIOError, OSError)):
                fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe_fd)

        # Unowned directories must have been swept
        assert not crashed_with_lock.exists()
        assert not crashed_legacy.exists()
        # Symlink must be untouched
        assert symlink_dir.is_symlink()

        # Initialize a second live store in the same scratch dir
        live2 = DiskKVStore(str(scratch))
        try:
            # live1 must NOT have been swept because its lock was held
            assert live1_dir.exists()
            assert live1._lock_fd is not None
            live2_dir = Path(live2._tmpdir.name)
            assert live2_dir.exists()
            assert live2._lock_fd is not None
        finally:
            live2.close()
            assert not live2_dir.exists()
            assert live2._lock_fd is None

        # Clean close of live1
        live1.close()
        assert not live1_dir.exists()
        assert live1._lock_fd is None
    finally:
        live1.close()
