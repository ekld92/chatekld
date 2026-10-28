import json
import threading
from types import SimpleNamespace

from rag.build import IndexBuildMixin


def test_postindex_status_waits_for_persisted_matching_sidecar(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    fingerprint = ('index', 'store', 1, 1, 3)
    def warm(**_kwargs):
        entered.set()
        assert release.wait(5)
        (tmp_path / 'sidecar_meta.json').write_text(json.dumps({'indexed_at': 'stamp', 'doc_count': 3}))
        return object()
    manager = SimpleNamespace(_status_lock=threading.Lock(), _index_mutation_lock=threading.Lock(),
        _stop_event=threading.Event(), _bm25_postindex_generation=0, _bm25_postindex_status='unknown',
        _get_bm25_retriever=warm, _read_index_meta=lambda: {'indexed_at': 'stamp'},
        _bm25_sidecar_dir=lambda: str(tmp_path), _cache_fingerprint=lambda: fingerprint,
        _bm25_fingerprint=fingerprint)
    threads = []
    real_thread = threading.Thread
    def start_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr('rag.build.threading.Thread', start_thread)
    IndexBuildMixin._index_kick_bm25_rebuild(manager, load_config=lambda: {})
    try:
        assert entered.wait(5)
        assert manager._bm25_postindex_status == 'preparing'
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
    assert manager._bm25_postindex_status == 'ready'


def test_postindex_failed_warm_never_claims_ready(tmp_path, monkeypatch):
    class ImmediateThread:
        def __init__(self, target, **_kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr('rag.build.threading.Thread', ImmediateThread)
    manager = SimpleNamespace(_status_lock=threading.Lock(), _index_mutation_lock=threading.Lock(),
        _stop_event=threading.Event(), _bm25_postindex_generation=0, _bm25_postindex_status='unknown',
        _get_bm25_retriever=lambda **_kwargs: None, _read_index_meta=lambda: {'indexed_at': 'stamp'},
        _bm25_sidecar_dir=lambda: str(tmp_path))
    IndexBuildMixin._index_kick_bm25_rebuild(manager, load_config=lambda: {})
    assert manager._bm25_postindex_status == 'unknown'
