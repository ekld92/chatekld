"""Preparation must not change the legacy cache/ingestion contract."""
import hashlib
from unittest.mock import patch

import pytest


@pytest.mark.parametrize('layout', ['whole', 'legacy', 'ranges'])
def test_cached_read_and_chunk_ids_are_stable(tmp_path, monkeypatch, layout):
    from rag import vault
    from llama_index.core import Document
    root = tmp_path / 'vault'
    root.mkdir()
    pdf = root / 'paper.pdf'
    pdf.write_bytes(b'synthetic source identity only')
    monkeypatch.setattr(vault, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(vault, 'OBSIDIAN_INDEX_DIR', str(tmp_path / 'index'))
    manager = vault.ObsidianVaultManager()
    sig = manager._pdf_file_signature(pdf, {}, 'paper.pdf')
    text = 'Legacy evidence: 42 participants.\nUnchanged numerical text.'
    if layout == 'legacy':
        cache = manager._legacy_pdf_cache_file(root, sig)
    elif layout == 'ranges':
        cache = manager._pdf_range_cache_file(root, sig, 0, 2)
    else:
        cache = manager._pdf_cache_file(root, sig)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(text.encode())
    before = hashlib.sha256(cache.read_bytes()).hexdigest()
    with patch.object(vault, 'extract_structured_from_pdf', side_effect=AssertionError('extraction')), \
         patch.object(vault, 'get_pdf_page_count', return_value=2):
        result = manager._read_pdf_text(pdf, root, 1000, signature=sig)
        actual, truncated = result
    assert result.extraction_status.completeness == "unknown"
    assert result.extraction_status.attempted_pages == ()
    assert result.extraction_status.source_sha256 == sig["sha256"]
    assert (actual, truncated) == (text, False)
    assert hashlib.sha256(cache.read_bytes()).hexdigest() == before
    doc = Document(text=actual, metadata={'source': 'paper.pdf', 'extension': '.pdf'})
    chunks = list(manager._chunk_raw_documents([doc], str(root)))
    assert [c.text for c in chunks] == [text]
    assert [c.doc_id for c in chunks] == [
        'paper.pdf::' + hashlib.sha1(('0\n' + text).encode()).hexdigest()[:16]]


def test_identity_resolution_does_not_mutate_legacy_metadata():
    from rag.embedding_identity import resolve_query_identity
    cfg = {'embed_provider': 'ollama'}
    for meta in ({'embed': 'old', 'embed_provider': 'lm_studio'},
                 {'embed': 'old', 'provider': 'lm_studio'}):
        before = dict(meta)
        for chat in ('ollama', 'lm_studio', 'openai'):
            identity = resolve_query_identity(meta, cfg, chat, 'new')
            assert (identity.provider, identity.model) == ('lm_studio', 'old')
            assert identity.warnings
        assert meta == before
    identity = resolve_query_identity({'provider': 'openai'}, cfg, 'lm_studio', 'old')
    assert identity.provider == 'lm_studio'  # established legacy selection, explicitly uncertain
    assert 'unknown' in identity.warnings[0]
    with pytest.raises(RuntimeError):
        resolve_query_identity({'embed_provider': 'invalid'}, cfg, 'ollama', 'old')


def test_status_preserves_legacy_tuple_and_ocr_string_contract():
    from core.extraction_status import ExtractionStatus, OCRText, TextRead, status_of
    result = TextRead('unchanged', False, ExtractionStatus(origin='legacy_cache'))
    assert result == ('unchanged', False)
    assert status_of(result).completeness == 'unknown'
    assert status_of(('old reader', False)).completeness == 'unknown'
    assert TextRead('partial', True).extraction_status.completeness == 'partial'
    assert OCRText('old text', truncated=True) == 'old text'


def test_tagging_does_not_negative_cache_known_ocr_failure(tmp_path):
    from core.extraction_status import ExtractionStatus, TextRead
    from tagging.text_source import _read_capped

    class Manager:
        def _read_pdf_text(self, *args, **kwargs):
            return TextRead('', True, ExtractionStatus(
                completeness='partial', origin='ocr', failed_pages=(1,)))

        def _atomic_write_text(self, path, text):
            path.write_text(text)

    marker = tmp_path / 'source.notext'
    result = _read_capped(Manager(), tmp_path / 'source.pdf', tmp_path,
                          1000, 3, marker, False)
    assert result == ('', True)
    assert result.extraction_status.failed_pages == (1,)
    assert not marker.exists()


def test_ocr_failure_and_token_limit_are_visible(tmp_path):
    import fitz
    import pdf_extractor
    from core.extraction_status import OCRText
    pdf = tmp_path / 'blank.pdf'
    with fitz.open() as doc:
        for _ in range(3):
            doc.new_page(width=30, height=30)
        doc.save(pdf)
    responses = iter(['first', RuntimeError('fixture failure'), OCRText('last', truncated=True)])
    def ocr(_image):
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result
    result = pdf_extractor._perform_ocr_fallback(str(pdf), ocr, 0, 3, None)
    assert result.full_text == 'first\n\nlast'
    assert result.truncated
    assert result.extraction_status.failed_pages == (2,)
    assert result.extraction_status.truncated_pages == (3,)
    assert result.extraction_status.completeness == 'partial'


def test_export_is_cache_only_deterministic_and_refuses_edits(tmp_path):
    from core.cache_exports import export_selected
    import json
    cache = tmp_path / 'cache'
    cache.mkdir()
    # Prefix collision, ambiguous key, missing cache and supplement.
    hashes = ['a'*8 + '1'*56, 'a'*8 + '2'*56, 'b'*64, 'c'*64]
    raw = b'Legacy text\r\n42 participants.\n'
    for digest in hashes[:3]:
        (cache / (digest + '.txt')).write_bytes(raw)
    entries = [{'sha256': digest, 'source_pdf': 'paper.pdf',
                'association': {'citation_keys': ['key'], 'verified_sha256': digest}}
               for digest in hashes]
    entries[2].update(role='supplement', association={'citation_keys': ['ambiguous', 'other']})
    request = {'vault_root': str(tmp_path / 'vault'), 'cache_dir': str(cache), 'entries': entries}
    before = {p.name: p.read_bytes() for p in cache.iterdir()}
    out = tmp_path / 'export'
    result = export_selected(request, out)
    assert result['missing_cache'] == 1
    manifest = (out / 'manifest.jsonl').read_bytes()
    assert (out / ('papers/key__' + hashes[0][:12] + '.md')).exists()
    rows = [json.loads(line) for line in manifest.splitlines()]
    for row in rows:
        if row['status'] == 'exported':
            assert (out / row['file']).read_bytes().endswith(raw)
            assert row['completeness'] == 'unknown'
    request['entries'].reverse()
    assert export_selected(request, out) == result
    assert (out / 'manifest.jsonl').read_bytes() == manifest
    edited = next((out / 'papers').iterdir())
    edited.write_text('User edit')
    with pytest.raises(ValueError, match='user-edited'):
        export_selected(request, out)
    assert edited.read_text() == 'User edit'
    assert {p.name: p.read_bytes() for p in cache.iterdir()} == before
    with pytest.raises(ValueError, match='outside'):
        export_selected(request, tmp_path / 'vault' / 'exports')


def test_export_sparse_ranges_and_symlinks(tmp_path):
    from core.cache_exports import export_selected
    cache = tmp_path / 'cache'
    cache.mkdir()
    digest = 'd'*64
    (cache / (digest + '-p00000-00002.txt')).write_text('first')
    (cache / (digest + '-p00003-00004.txt')).write_text('last')
    request = {'vault_root': str(tmp_path / 'vault'), 'cache_dir': str(cache),
               'entries': [{'sha256': digest, 'source_pdf': 'paper.pdf'}]}
    out = tmp_path / 'out'
    export_selected(request, out)
    manifest = (out / 'manifest.jsonl').read_text()
    assert 'gap or overlap' in manifest
    assert next((out / 'papers').iterdir()).read_bytes().endswith(b'first\n\nlast')
    (cache / (digest + '.txt')).symlink_to(out / 'manifest.jsonl')
    with pytest.raises(ValueError, match='Symlink'):
        export_selected(request, tmp_path / 'other')


def test_export_refuses_symlink_manifest(tmp_path):
    from core.cache_exports import export_selected
    cache = tmp_path / 'cache'
    cache.mkdir()
    digest = 'e' * 64
    (cache / (digest + '.txt')).write_text('cached text')
    request = {'vault_root': str(tmp_path / 'vault'), 'cache_dir': str(cache),
               'entries': [{'sha256': digest, 'source_pdf': 'paper.pdf'}]}
    out = tmp_path / 'out'
    out.mkdir()
    victim = tmp_path / 'victim'
    victim.write_text('preserve')
    (out / 'manifest.jsonl').symlink_to(victim)
    with pytest.raises(ValueError, match='Symlink'):
        export_selected(request, out)
    assert victim.read_text() == 'preserve'
    assert not list((out / 'papers').iterdir())


@pytest.mark.parametrize('backend', ['simple', 'lancedb'])
@pytest.mark.parametrize('chat', ['ollama', 'lm_studio', 'openai'])
@pytest.mark.parametrize('identity_kind', ['recorded', 'legacy_local', 'unknown'])
def test_query_pipeline_uses_saved_backend_with_mock_embeddings(tmp_path, monkeypatch, backend, chat, identity_kind):
    from llama_index.core import StorageContext, VectorStoreIndex
    from llama_index.core.embeddings import MockEmbedding
    from llama_index.core.llms import MockLLM
    from llama_index.core.schema import TextNode
    from rag import engine, vault
    from rag.lancedb_store import make_lancedb_vector_store
    embed = MockEmbedding(embed_dim=4)
    context = StorageContext.from_defaults(**(
        {'vector_store': make_lancedb_vector_store(str(tmp_path))} if backend == 'lancedb' else {}))
    index = VectorStoreIndex([TextNode(id_='legacy', text='Original text', embedding=[1., 1., 1., 1.])],
                            storage_context=context, embed_model=embed, store_nodes_override=True)
    context.persist(persist_dir=str(tmp_path))
    import json
    metadata = {'embed': 'saved', 'untouched_legacy_field': ['keep', 42]}
    if identity_kind == 'recorded':
        metadata['embed_provider'] = 'lm_studio'
    elif identity_kind == 'legacy_local':
        metadata['provider'] = 'lm_studio'
    else:
        metadata['provider'] = 'openai'  # does not identify the local embedder
    (tmp_path / 'obsidian_meta.json').write_text(json.dumps(metadata, indent=3))
    monkeypatch.setattr(vault, 'OBSIDIAN_INDEX_DIR', str(tmp_path))
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    calls = []
    class Provider:
        def __init__(self, name):
            self.name = name
        def get_embedding(self, name, **kwargs):
            calls.append((self.name, name, kwargs))
            return embed
        def get_llm(self, *args, **kwargs):
            return MockLLM()
    monkeypatch.setattr(engine, 'get_provider', Provider)
    monkeypatch.setattr(engine, 'load_config', lambda: {})
    monkeypatch.setattr(vault, 'load_config', lambda: {'embed_provider': 'ollama'})
    manager = vault.ObsidianVaultManager()
    manager._index = index
    query_engine, _ = manager._build_engine(llm_name='chat', embed_name='changed', top_k=1,
                                           provider_name=chat, similarity_cutoff=0.)
    prepared = query_engine.prepare('original question')
    nodes = query_engine.read_snapshot(prepared)
    assert [node.node.text for node in nodes] == ['Original text']
    expected_provider = ('lm_studio' if identity_kind != 'unknown' or chat == 'lm_studio' else 'ollama')
    assert calls == [(expected_provider, 'saved', {'request_timeout_s': engine.QUERY_EMBED_TIMEOUT_S})]
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before


def test_successful_empty_ocr_is_not_a_transport_failure(tmp_path):
    import fitz
    import pdf_extractor
    from core.extraction_status import OCRText
    pdf = tmp_path / 'blank.pdf'
    with fitz.open() as doc:
        doc.new_page(width=30, height=30)
        doc.save(pdf)
    result = pdf_extractor._perform_ocr_fallback(str(pdf), lambda _: OCRText(''), 0, 1, None)
    assert result.full_text == ''
    assert not result.truncated
    assert result.extraction_status.failed_pages == ()
    assert result.extraction_status.completeness == 'unknown'
