"""Automatic export checks use disposable PDFs, text caches and stub extraction."""
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.readable_pdf_export import export_pdf_text


def signature(source):
    stat = source.stat()
    return dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                sha256=hashlib.sha256(source.read_bytes()).hexdigest())


def test_no_clobber_versioning_and_same_basename(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'Smith 2026.pdf'
    source.write_bytes(b'first PDF')
    destination = tmp_path / 'exports'
    sig = signature(source)
    first = export_pdf_text(source, vault, destination, sig, 'Exact extracted text')
    assert first.name.startswith('Smith_2026__')
    assert source.as_uri() in first.read_text()
    assert first.read_text().endswith('Exact extracted text')
    assert export_pdf_text(source, vault, destination, sig, 'Exact extracted text') == first
    first.write_text('My corrections')
    with pytest.raises(ValueError, match='user-edited'):
        export_pdf_text(source, vault, destination, sig, 'Exact extracted text')
    source.write_bytes(b'updated PDF')
    second = export_pdf_text(source, vault, destination, signature(source), 'Updated text')
    assert second != first and first.read_text() == 'My corrections'
    other = vault / 'sub' / source.name
    other.parent.mkdir()
    other.write_bytes(source.read_bytes())
    third = export_pdf_text(other, vault, destination, signature(other), 'Updated text')
    assert third not in {first, second}


def test_stale_and_unsafe_destination_refused(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'paper.pdf'
    source.write_bytes(b'pdf')
    sig = signature(source)
    with pytest.raises(ValueError, match='outside'):
        export_pdf_text(source, vault, vault / 'exports', sig, 'text')
    link = tmp_path / 'linked-exports'
    external = tmp_path / 'external'
    external.mkdir()
    link.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match='Symlink'):
        export_pdf_text(source, vault, link, sig, 'text')
    source.write_bytes(b'changed PDF')
    with pytest.raises(ValueError, match='changed'):
        export_pdf_text(source, vault, tmp_path / 'exports', sig, 'text')
    assert not (tmp_path / 'exports').exists()


def loader_fixture(tmp_path, monkeypatch):
    from rag import vault as module
    vault = tmp_path / 'vault'
    vault.mkdir()
    data = tmp_path / 'data'
    monkeypatch.setattr(module, 'OBSIDIAN_INDEX_DIR', str(data / 'obsidian_storage'))
    monkeypatch.setattr(module, 'OBSIDIAN_CACHE_DIR', str(data / 'obsidian_cache'))
    monkeypatch.setattr(module, 'load_config', lambda: {'vault_path': str(vault)})
    manager = module.ObsidianVaultManager()
    signatures = {}
    monkeypatch.setattr(manager, '_load_pdf_signature_cache', lambda: dict(signatures))
    def save(value):
        signatures.clear()
        signatures.update(value)
    monkeypatch.setattr(manager, '_save_pdf_signature_cache', save)
    monkeypatch.setattr(module, 'get_pdf_page_count', lambda _: 1)
    calls = []
    def extract(path, **kwargs):
        calls.append(path)
        return SimpleNamespace(full_text='Text for ' + Path(path).name, truncated=False)
    monkeypatch.setattr(module, 'extract_structured_from_pdf', extract)
    return module, manager, vault, data, signatures, calls


def test_cached_pdfs_export_without_changing_loader_text(tmp_path, monkeypatch):
    _, manager, vault, data, signatures, calls = loader_fixture(tmp_path, monkeypatch)
    old = vault / 'old.pdf'
    old.write_bytes(b'old')
    # Every successful cache payload gets a Markdown companion.
    list(manager._load_vault_documents(str(vault)))
    assert len(list(data.rglob('*.md'))) == 1
    assert signatures['old.pdf']['sha256'] == signature(old)['sha256']
    new = vault / 'new.pdf'
    new.write_bytes(b'new')
    docs = list(manager._load_vault_documents(str(vault)))
    exports = list(data.rglob('*.md'))
    assert len(exports) == 2
    assert len(calls) == 2  # one normal extraction per PDF, none for export
    assert len(docs) == 2 and all(d.metadata['extension'] == '.pdf' for d in docs)
    new_export = next(p for p in exports if 'new' in p.name)
    assert new_export.read_text().endswith(next(d.text for d in docs if d.metadata['source'] == 'new.pdf'))
    new_export.write_text('Personal notes')
    messages = []
    monkeypatch.setattr(manager, '_emit', messages.append)
    assert len(list(manager._load_vault_documents(str(vault)))) == 2
    assert new_export.read_text() == 'Personal notes'
    assert any('Markdown export skipped' in m for m in messages)
    new.write_bytes(b'new version')
    list(manager._load_vault_documents(str(vault)))
    assert len(list(data.rglob('*.md'))) == 3
    assert new_export.read_text() == 'Personal notes'


def test_partial_retried_from_cache_path_and_ranges(tmp_path, monkeypatch):
    module, manager, vault, data, signatures, _ = loader_fixture(tmp_path, monkeypatch)
    signatures['historical.pdf'] = {'sha256': '0' * 64}
    source = vault / 'new.pdf'
    source.write_bytes(b'PDF')
    monkeypatch.setattr(module, 'extract_structured_from_pdf', lambda *a, **k:
                        SimpleNamespace(full_text='Partial text', truncated=True))
    assert list(manager._load_vault_documents(str(vault))) == []
    assert 'new.pdf' in manager._scan_failed_sources
    assert not list(data.rglob('*.md'))
    monkeypatch.setattr(module, 'get_pdf_page_count', lambda _: module.EXTRACT_MAX_PAGES_PER_CALL + 1)
    monkeypatch.setattr(module, 'extract_structured_from_pdf', lambda *a, **k:
                        SimpleNamespace(full_text=f'Range {k["start_page"]}', truncated=False))
    docs = list(manager._load_vault_documents(str(vault)))
    assert len(docs) == len(list(data.rglob('*.md'))) == 2
    assert docs[0].metadata['page_start'] == 0
    monkeypatch.setattr(module, 'extract_structured_from_pdf', lambda *a, **k: pytest.fail('re-extraction'))
    assert [d.text for d in manager._load_vault_documents(str(vault))] == [d.text for d in docs]


def test_interrupted_scan_keeps_unvisited_pdf_for_next_run(tmp_path, monkeypatch):
    _, manager, vault, data, signatures, _ = loader_fixture(tmp_path, monkeypatch)
    for name in ('a.pdf', 'b.pdf'):
        (vault / name).write_bytes(name.encode())
    documents = manager._load_vault_documents(str(vault))
    next(documents)
    documents.close()
    assert 'b.pdf' not in signatures
    assert len(list(manager._load_vault_documents(str(vault)))) == 2
    assert len(list(data.rglob('*.md'))) == 2


def test_export_failure_never_drops_documents_and_cached_new_pdf_exports(tmp_path, monkeypatch):
    _, manager, vault, data, signatures, calls = loader_fixture(tmp_path, monkeypatch)
    source = vault / 'cached.pdf'
    source.write_bytes(b'pdf')
    sig = signature(source)
    manager._save_pdf_cache_file(manager._pdf_cache_file(vault, sig), 'Cached bytes')
    assert list(manager._load_vault_documents(str(vault)))[0].text == 'Cached bytes'
    assert len(list(data.rglob('*.md'))) == 1 and calls == []
    import core.readable_pdf_export as exporter
    def fail(*args, **kwargs):
        raise RuntimeError('export unavailable')
    monkeypatch.setattr(exporter, 'export_pdf_text', fail)
    assert list(manager._load_vault_documents(str(vault)))[0].text == 'Cached bytes'
    assert calls == []


def test_range_failure_and_cancellation_keep_source_retryable(tmp_path, monkeypatch):
    module, manager, vault, data, signatures, _ = loader_fixture(tmp_path, monkeypatch)
    source = vault / 'large.pdf'
    source.write_bytes(b'large')
    monkeypatch.setattr(module, 'get_pdf_page_count',
                        lambda _: module.EXTRACT_MAX_PAGES_PER_CALL + 1)
    calls = []
    def partial(path, **kwargs):
        calls.append(kwargs['start_page'])
        if kwargs['start_page']:
            return SimpleNamespace(full_text='partial second range', truncated=True)
        return SimpleNamespace(full_text='good first range', truncated=False)
    monkeypatch.setattr(module, 'extract_structured_from_pdf', partial)
    assert list(manager._load_vault_documents(str(vault))) == []
    assert 'large.pdf' in manager._scan_failed_sources
    assert len(list(data.rglob('*.txt'))) == 1
    assert not list(data.rglob('*.md'))
    assert calls == [0, module.EXTRACT_MAX_PAGES_PER_CALL]
    manager._stop_event.clear()
    def cancelled(path, **kwargs):
        manager._stop_event.set()
        return SimpleNamespace(full_text='interrupted', truncated=True)
    monkeypatch.setattr(module, 'extract_structured_from_pdf', cancelled)
    assert list(manager._load_vault_documents(str(vault))) == []
    manager._stop_event.clear()
    monkeypatch.setattr(module, 'extract_structured_from_pdf', lambda path, **kwargs:
                        SimpleNamespace(full_text='good second range', truncated=False))
    docs = list(manager._load_vault_documents(str(vault)))
    assert [d.text for d in docs] == ['good first range', 'good second range']
    assert len(list(data.rglob('*.md'))) == 2


def test_cache_only_backfill_verified_name_and_no_clobber(tmp_path):
    from scripts.export_cached_pdf_markdown import backfill
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'Readable title.pdf'
    source.write_bytes(b'pdf bytes')
    digest = signature(source)['sha256']
    cache = tmp_path / 'cache'
    cache.mkdir()
    (cache / (digest + '.txt')).write_bytes(b'Unchanged\r\ntext')
    request = {'vault_root': str(vault), 'cache_dir': str(cache),
               'destination': str(tmp_path / 'markdown'),
               'entries': [{'source_pdf': source.name, 'sha256': digest,
                            'verified_sha256': digest, 'citation_keys': ['Smith2026']}]}
    result = Path(backfill(request)[0])
    assert result.name.startswith('Smith2026__')
    assert result.read_bytes().endswith(b'Unchanged\r\ntext')
    assert (cache / (digest + '.txt')).read_bytes() == b'Unchanged\r\ntext'
    result.write_text('user edit')
    with pytest.raises(ValueError, match='user-edited'):
        backfill(request)
    request['entries'][0]['verified_sha256'] = '0' * 64
    fallback = Path(backfill(request)[0])
    assert fallback.name.startswith('Readable_title__')
    with pytest.raises(ValueError, match='outside'):
        request['destination'] = str(vault / 'markdown')
        backfill(request)


def test_partial_source_old_chunks_survive_stale_sweep(tmp_path, monkeypatch):
    module, manager, vault, _, _, _ = loader_fixture(tmp_path, monkeypatch)
    (vault / 'paper.pdf').write_bytes(b'pdf')
    monkeypatch.setattr(module, 'extract_structured_from_pdf', lambda *a, **k:
                        SimpleNamespace(full_text='partial', truncated=True))
    assert list(manager._load_vault_documents(str(vault))) == []
    from llama_index.core import Document
    other = Document(text='unchanged', id_='other.md::chunk',
                     metadata={'source': 'other.md'})
    old_ids = {'paper.pdf::old-1', 'paper.pdf::old-2', other.doc_id}
    docstore = SimpleNamespace(get_all_ref_doc_info=lambda: dict.fromkeys(old_ids),
                               get_document_hash=lambda _: other.hash)
    deleted = []
    monkeypatch.setattr(manager, '_sweep_stale_doc_ids',
                        lambda _idx, ids, **_kw: deleted.extend(ids) or len(ids))
    manager._index_documents_streaming(SimpleNamespace(docstore=docstore), iter([other]))
    assert deleted == []


def test_cache_write_failure_does_not_create_companion(tmp_path, monkeypatch):
    module, manager, vault, data, _, _ = loader_fixture(tmp_path, monkeypatch)
    (vault / 'paper.pdf').write_bytes(b'pdf')
    monkeypatch.setattr(manager, '_atomic_write_text', lambda *_:
                        (_ for _ in ()).throw(OSError('disposable disk failure')))
    docs = list(manager._load_vault_documents(str(vault)))
    assert [d.text for d in docs] == ['Text for paper.pdf']
    assert not list(data.rglob('*.txt'))
    assert not list(data.rglob('*.md'))


def test_status_only_partial_is_not_indexed(tmp_path, monkeypatch):
    from core.extraction_status import ExtractionStatus
    module, manager, vault, data, _, _ = loader_fixture(tmp_path, monkeypatch)
    (vault / 'paper.pdf').write_bytes(b'pdf')
    monkeypatch.setattr(module, 'extract_structured_from_pdf', lambda *_args, **_kwargs:
                        SimpleNamespace(full_text='partial', truncated=False,
                                        extraction_status=ExtractionStatus(completeness='partial')))
    assert list(manager._load_vault_documents(str(vault))) == []
    assert 'paper.pdf' in manager._scan_failed_sources
    assert not list(data.rglob('*.txt'))


def test_backfill_range_bounds_and_cache_symlink_refusal(tmp_path):
    from scripts.export_cached_pdf_markdown import backfill
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'paper.pdf'
    source.write_bytes(b'pdf')
    digest = signature(source)['sha256']
    cache = tmp_path / 'cache'
    cache.mkdir()
    name = f'{digest}-p00000-01000.txt'
    (cache / name).write_text('range text')
    selection = {'vault_root': str(vault), 'cache_dir': str(cache),
                 'destination': str(tmp_path / 'exports'),
                 'entries': [{'source_pdf': 'paper.pdf', 'sha256': digest,
                              'cache_files': [name]}]}
    result = Path(backfill(selection)[0])
    assert '__p00001-01000.md' in result.name
    assert 'pages 1–1000' in result.read_text()
    result.unlink()
    (cache / name).unlink()
    (cache / name).symlink_to(tmp_path / 'elsewhere.txt')
    with pytest.raises((ValueError, OSError)):
        backfill(selection)
