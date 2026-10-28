"""OCR cancellation coverage and non-reasoning transport contracts."""
import json
import threading
from unittest.mock import Mock

import fitz
import httpx
import openai
import pytest


def test_pause_between_pages_preserves_partial_and_skipped_pages(tmp_path):
    from pdf_extractor import extract_structured_from_pdf
    path = tmp_path / 'scan.pdf'
    with fitz.open() as doc:
        for _ in range(3):
            doc.new_page(width=30, height=30)
        doc.save(path)
    stop = threading.Event()
    calls = []
    def ocr(_image):
        calls.append(1)
        stop.set()
        return 'first page'
    result = extract_structured_from_pdf(
        str(path), ocr_cb=ocr, use_markitdown=False, cancel_cb=stop.is_set)
    assert len(calls) == 1
    assert result.full_text == 'first page'
    assert result.truncated
    assert result.extraction_status.completed_pages == (1,)
    assert result.extraction_status.skipped_pages == (2, 3)
    # Resume with a fresh signal: the same fixture can complete; no hidden latch.
    result = extract_structured_from_pdf(
        str(path), ocr_cb=lambda _: 'text', use_markitdown=False,
        cancel_cb=lambda: False)
    assert not result.truncated
    assert result.extraction_status.completed_pages == (1, 2, 3)


def test_pause_from_heartbeat_prevents_page_call(tmp_path):
    from pdf_extractor import extract_structured_from_pdf
    path = tmp_path / 'scan.pdf'
    with fitz.open() as doc:
        doc.new_page(width=30, height=30)
        doc.save(path)
    stop = threading.Event()
    ocr = Mock(side_effect=AssertionError('must not call OCR'))
    result = extract_structured_from_pdf(
        str(path), ocr_cb=ocr, use_markitdown=False,
        page_done_cb=lambda _: stop.set(), cancel_cb=stop.is_set)
    assert not ocr.called
    assert result.truncated
    assert result.extraction_status.attempted_pages == ()
    assert result.extraction_status.skipped_pages == (1,)


def native_client(monkeypatch, output, stats, status=200):
    from core.providers import lms, base
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={'output': output, 'stats': stats})
    client = openai.OpenAI(
        base_url='http://localhost:1234/v1', api_key='fixture', max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(lms, 'get_lmstudio_client', lambda *a: client)
    monkeypatch.setattr(base, 'resolve_lm_studio_host', lambda: 'http://localhost:1234')
    return client, requests


@pytest.mark.parametrize('tokens,partial', [(7, False), (8, True)])
def test_ocr_native_wire_and_bound_projection(monkeypatch, tokens, partial):
    from services.vision import _chat_lm_studio_image
    client, requests = native_client(monkeypatch,
        [{'type': 'message', 'content': '  exact text  '}],
        {'total_output_tokens': tokens, 'reasoning_output_tokens': 0})
    try:
        meta = {}
        result = _chat_lm_studio_image('fixture', 'transcribe', 'png',
            timeout=30, max_tokens=8, meta=meta, disable_reasoning=True)
        assert result == 'exact text'
        assert meta == {'truncated': partial, 'reasoning_tokens': 0}
        assert len(requests) == 1
        assert requests[0].url.path == '/api/v1/chat'
        body = json.loads(requests[0].content)
        assert body['reasoning'] == 'off'
        assert body['store'] is False
        assert body['max_output_tokens'] == 8
        assert body['input'][1]['data_url'] == 'data:image/png;base64,png'
    finally:
        client.close()


@pytest.mark.parametrize('output,stats', [
    ([{'type': 'reasoning', 'content': 'never transcription'}],
     {'total_output_tokens': 3, 'reasoning_output_tokens': 3}),
    ([{'type': 'message', 'content': 'text'}], {}),
    ([], {'total_output_tokens': 0, 'reasoning_output_tokens': 0}),
])
def test_ocr_rejects_unverified_or_reasoning_only_response(monkeypatch, output, stats):
    from services.vision import _chat_lm_studio_image
    client, requests = native_client(monkeypatch, output, stats)
    try:
        with pytest.raises(ValueError):
            _chat_lm_studio_image('fixture', 'prompt', 'png',
                                 max_tokens=8, disable_reasoning=True)
        assert len(requests) == 1  # no fallback to a thinking-enabled route
    finally:
        client.close()


def test_manager_disables_reasoning_for_ocr_only(monkeypatch):
    from services import vision
    helper = Mock(return_value='text')
    monkeypatch.setattr(vision, '_chat_lm_studio_image', helper)
    monkeypatch.setattr(vision, '_record_vision_usage', lambda *a, **k: None)
    manager = vision.GLMOCRManager(model='fixture', provider='lm_studio')
    assert manager.extract_page_text('fixture_image') == 'text'
    assert helper.call_args.kwargs['disable_reasoning'] is True


def test_token_capped_ocr_is_retryable_in_same_process(monkeypatch):
    from services import vision
    responses = iter([('partial', True), ('complete', False)])
    calls = []
    def chat(*_args, **kwargs):
        calls.append(kwargs)
        text, truncated = next(responses)
        kwargs['meta']['truncated'] = truncated
        return text
    monkeypatch.setattr(vision, '_chat_lm_studio_image', chat)
    monkeypatch.setattr(vision, '_record_vision_usage', lambda *a, **k: None)
    manager = vision.GLMOCRManager(model='fixture', provider='lm_studio')
    first = manager.extract_page_text('fixture_image')
    second = manager.extract_page_text('fixture_image')
    third = manager.extract_page_text('fixture_image')
    assert first.truncated and first == 'partial'
    assert not second.truncated and second == third == 'complete'
    assert len(calls) == 2


def test_ollama_disables_thinking_only_when_requested(monkeypatch):
    from types import SimpleNamespace
    from core.providers import ollama as provider
    from services.vision import _chat_ollama_image
    client = Mock()
    client.chat.return_value = SimpleNamespace(
        message=SimpleNamespace(content='text'), done_reason='stop')
    monkeypatch.setattr(provider, '_ollama_client', lambda *a: client)
    assert _chat_ollama_image('fixture', 'prompt', 'png',
                             disable_reasoning=True) == 'text'
    assert client.chat.call_args.kwargs['think'] is False
    _chat_ollama_image('fixture', 'prompt', 'png')
    assert 'think' not in client.chat.call_args.kwargs


def test_manager_pause_during_range_does_not_publish_partial_cache(tmp_path, monkeypatch):
    from rag import vault
    root = tmp_path / 'vault'
    root.mkdir()
    source = root / 'scan.pdf'
    with fitz.open() as doc:
        for _ in range(3):
            doc.new_page(width=30, height=30)
        doc.save(source)
    monkeypatch.setattr(vault, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'cache'))
    manager = vault.ObsidianVaultManager()
    manager._index_state = 'embedding'
    calls = []
    def ocr(_image):
        calls.append(1)
        assert manager.pause_indexing()
        return 'first page retained'
    monkeypatch.setattr(vault.glm_ocr_manager, 'extract_page_text', ocr)
    monkeypatch.setattr(manager, '_save_pdf_cache_file', Mock())
    monkeypatch.setattr(manager, '_export_readable_pdf', Mock())
    docs = list(manager._load_pdf_range_documents(
        source, 'scan.pdf', '.pdf', root, {'sha256': 'f' * 64}, 3))
    assert calls == [1]
    assert docs == []
    assert 'scan.pdf' in manager._scan_failed_sources
    manager._save_pdf_cache_file.assert_not_called()
    manager._export_readable_pdf.assert_not_called()
