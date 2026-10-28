"""No-change indexing retains durable search only with mutation-bound evidence."""
import json
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('change', ['none', 'same_count_mutation', 'added', 'deleted', 'failed',
                                    'partial', 'drift', 'stopped', 'fresh', 'missing_stamp'])
def test_no_change_index_preserves_disk_only_for_proven_unchanged_corpus(tmp_path, monkeypatch, change):
    from rag.vault import ObsidianVaultManager
    from rag import vault as module
    vault = tmp_path / 'vault'
    vault.mkdir()
    store = tmp_path / 'store'
    store.mkdir()
    meta_path = store / 'obsidian_meta.json'
    meta = {'phase': 'done', 'indexed_at': 'original', 'partial': change == 'partial'}
    if change == 'missing_stamp':
        meta.pop('indexed_at')
    meta_path.write_text(json.dumps(meta))
    sidecar = store / 'sidecar-fixture'
    sidecar.write_bytes(b'validated lexical search')
    before = meta_path.read_bytes()
    monkeypatch.setattr(module, 'OBSIDIAN_INDEX_DIR', str(store))
    manager = ObsidianVaultManager()
    manager._vault_path = str(vault)
    idx = SimpleNamespace(docstore=SimpleNamespace(docs={'one': object()}))
    manager._index = idx
    monkeypatch.setattr(manager, '_index_prepare_embedding', lambda *a, **kw: (object(), 'ollama'))
    monkeypatch.setattr(manager, '_index_lancedb_preflight', lambda **kw: True)
    monkeypatch.setattr(manager, '_index_open_store', lambda *a, **kw: (idx, 'simple', change != 'fresh', str(meta_path)))
    monkeypatch.setattr(manager, '_read_index_meta', lambda: json.loads(meta_path.read_text()))
    monkeypatch.setattr(manager, '_load_vault_documents', lambda *a, **kw: iter(()))
    monkeypatch.setattr(manager, '_chunk_raw_documents', lambda *a: iter(()))
    monkeypatch.setattr(manager, '_index_reconcile_lancedb_drift', lambda *a, **kw: change == 'drift')
    def stream(*a, **kw):
        if change == 'same_count_mutation':
            with manager._mutate_index():
                idx.docstore.docs['one'] = object()
        if change == 'stopped':
            manager._stop_event.set()
        return int(change == 'added'), 1, int(change == 'deleted'), int(change == 'failed'), {'note.md': 1}
    monkeypatch.setattr(manager, '_index_documents_streaming', stream)
    persisted = []
    monkeypatch.setattr(manager, '_index_finalise_persist', lambda *a, **kw: persisted.append(True))
    invalidated = []
    def invalidate():
        invalidated.append(True)
        sidecar.unlink()
    monkeypatch.setattr(manager, '_invalidate_retrieval_caches', invalidate)
    warmed = []
    monkeypatch.setattr(manager, '_index_kick_bm25_rebuild', lambda **kw: warmed.append(manager.get_status()))
    manager.index_vault('fixture-llm', 'fixture-embed', provider_name='ollama')
    assert manager.get_status() == ('idle' if change == 'stopped' else 'done'), manager.drain_status_messages()
    assert bool(persisted) == (change != 'none')
    assert bool(invalidated) == (change != 'none')
    assert warmed == [manager.get_status()]
    if change == 'none':
        assert meta_path.read_bytes() == before
        assert sidecar.read_bytes() == b'validated lexical search'


def test_readable_copy_failures_are_grouped_without_losing_log_details(tmp_path, monkeypatch, caplog):
    import logging
    from rag.vault import ObsidianVaultManager
    from core import readable_pdf_export
    manager = ObsidianVaultManager()
    manager._index_state = 'running'
    def conflict(*a, **kw):
        raise ValueError('Existing edited copy retained')
    monkeypatch.setattr(readable_pdf_export, 'export_pdf_text', conflict)
    with caplog.at_level(logging.INFO):
        for number in range(10):
            manager._export_readable_pdf(tmp_path / f'{number}.pdf', tmp_path, {}, 'fixture')
    assert manager._readable_export_warning_count == 10
    messages = manager.drain_status_messages()
    assert len(messages) == 3
    for number in range(10):
        assert f'for {number}.pdf:' in caplog.text
    assert not list(tmp_path.iterdir())
