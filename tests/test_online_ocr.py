"""Offline driver and receipt checks for explicit online image transcription."""
import io
import json
import threading
from pathlib import Path

import httpx
import pytest
from PIL import Image

from core.online_ocr import OnlineOCR, extract_image, normalise_image


def _png() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(stream, format="PNG")
    return stream.getvalue()


def _oversized_png_header():
    # Pillow rejects the dimensions at open(), before decoding/allocating pixels.
    import struct
    import zlib
    def chunk(kind, data):
        return (struct.pack('!I', len(data)) + kind + data
                + struct.pack('!I', zlib.crc32(kind + data)))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('!IIBBBBB', 15000, 15000, 8, 2, 0, 0, 0))
            + chunk(b'IEND', b''))


def test_pillow_oversized_rejection_is_source_local_without_provider_call(monkeypatch):
    from core.online_runs import source_exception
    monkeypatch.setattr(httpx, 'Client', lambda **kw: pytest.fail('Rejected image reached provider'))
    result = OnlineOCR('deepseek', 'deepseek-flash').recognize(
        _oversized_png_header(), lambda: False, lambda: None)
    assert result['status'] == 'conversion_failed'
    assert result['calls'] == 0
    assert result['warning'] == 'DecompressionBombError'
    assert source_exception(result)


def test_preview_checks_native_text_only_for_substantial_image_pages(tmp_path, monkeypatch):
    import fitz
    from api.routes import online_ocr as route
    from core.page_ocr import _page_content

    vault = tmp_path / "vault"
    vault.mkdir()
    pdf = vault / "mixed.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Native text only")
    doc.new_page().insert_image(fitz.Rect(0, 0, 400, 400), stream=_png())
    doc.save(pdf)
    doc.close()
    monkeypatch.setattr(route, "OBSIDIAN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(route, "OBSIDIAN_INDEX_DIR", str(tmp_path / "index"))
    with fitz.open(pdf) as check:
        expected = []
        for index, page in enumerate(check):
            native, image = _page_content(page)
            if image and len(native.strip()) < 40:
                expected.append(index + 1)
    preview = route._preview(vault, pdf, "mixed.pdf", reextract=True)
    assert expected == preview["eligible_pages"] == [2]


def test_image_probe_skips_vector_only_page_without_rendering():
    from unittest.mock import Mock
    from core.page_ocr import _has_substantial_image

    page = Mock()
    page.get_images.return_value = []
    page.read_contents.return_value = b"0 0 m 100 100 l S"
    assert _has_substantial_image(page) is False
    page.get_image_info.assert_not_called()


def test_deepseek_payload_and_completion(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    seen = []

    def handler(request):
        seen.append(request)
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-flash"
        assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        return httpx.Response(200, json={"id": "r1", "choices": [
            {"message": {"content": "Visible text"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 4}})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    result = OnlineOCR("deepseek", "deepseek-flash").recognize(_png(), lambda: False, lambda: None)
    assert result["status"] == "ocr"
    assert result["usage"]["prompt_tokens"] == 100
    assert len(seen) == 1


def test_online_vision_uses_description_prompt_and_distinct_identity(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")

    def handler(request):
        payload = json.loads(request.content)
        prompt = payload["messages"][0]["content"][0]["text"]
        assert "Describe this image for search" in prompt
        assert "axis titles" in prompt
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "A chart of enrolment; x axis: year"},
             "finish_reason": "stop"}]})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    engine = OnlineOCR("deepseek", "deepseek-flash", task="vision")
    assert engine.identity()["prompt"] != OnlineOCR("deepseek", "deepseek-flash").identity()["prompt"]
    result = engine.recognize(_png(), lambda: False, lambda: None)
    assert result["status"] == "ocr" and result["task"] == "vision"
    assert result["quality_flags"] == ["online_visual_description_unverified"]


def test_output_limit_is_not_complete(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={
        "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
        "usage": {"completion_tokens": 4096}}))
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    assert OnlineOCR("deepseek", "deepseek-flash").recognize(
        _png(), lambda: False, lambda: None)["status"] == "output_limit"


def test_output_limit_and_http_failure_are_recorded_as_failed_attempts(monkeypatch):
    from core.llm.usage import usage_tracker
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    recorded = []
    monkeypatch.setattr(usage_tracker, "record", lambda **kwargs: recorded.append(kwargs))
    responses = iter([httpx.Response(200, json={
        "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4096}}),
        httpx.Response(429)])
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda _: next(responses)), **kwargs))
    engine = OnlineOCR("deepseek", "deepseek-flash")
    assert engine.recognize(_png(), lambda: False, lambda: None)["status"] == "output_limit"
    assert engine.recognize(_png(), lambda: False, lambda: None)["status"] == "rate_limited"
    assert [row["success"] for row in recorded] == [False, False]
    assert recorded[0]["billable"] is True
    assert recorded[0]["usage"].output_tokens == 4096
    assert recorded[1]["error_category"] == "rate_limited"


def test_new_image_generation_can_retry_after_exhausted_old_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "image.png"
    source.write_bytes(_png())
    calls = []
    def recognize(_self, *_args):
        calls.append(1)
        return {"status": "timeout" if len(calls) < 3 else "ocr",
                "text": "" if len(calls) < 3 else "recovered",
                "calls": 1, "call_s": 0.01}
    monkeypatch.setattr(OnlineOCR, "recognize", recognize)
    engine = OnlineOCR("deepseek", "deepseek-flash")
    storage = tmp_path / "attempts"
    first = extract_image(source, vault, storage, engine, retry=True,
                          attempt_generation="a" * 32)
    second = extract_image(source, vault, storage, engine, retry=True,
                           attempt_generation="a" * 32)
    assert (first["attempt"], second["attempt"]) == (1, 2)
    recovered = extract_image(source, vault, storage, engine, retry=True,
                              attempt_generation="b" * 32)
    assert recovered["status"] == "ocr" and recovered["attempt"] == 1
    assert len(calls) == 3


def test_interrupted_session_promotion_records_exact_landed_cache(tmp_path, monkeypatch):
    from api.routes import online_ocr as route
    monkeypatch.setattr(route.obsidian_manager, "heartbeat", lambda epoch: True)
    monkeypatch.setattr(route, "_cancel", threading.Event())
    vault = tmp_path / "vault"
    vault.mkdir()
    cache_root = tmp_path / "cache"
    monkeypatch.setattr(route, "BASE_DIR", str(tmp_path / "base"))
    monkeypatch.setattr(route, "OBSIDIAN_CACHE_DIR", str(cache_root))
    monkeypatch.setattr(route, "load_config", lambda: {"provider": "ollama"})
    monkeypatch.setattr(route, "_preflight_index_settings", lambda *_, **__: None)
    writes = []
    def write(cache, text):
        writes.append(cache)
        if len(writes) == 2:
            raise OSError("second cache cannot be written")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text)
    monkeypatch.setattr(route.obsidian_manager, "_atomic_write_text", write)
    selected = []
    for name in ("one.png", "two.png"):
        source = vault / name
        source.write_bytes(_png() + name.encode())
        selected.append((vault, source, name,
                         {"source_sha256": route._digest(source), "recipe": "r"},
                         "new " + name))
    with route._lock:
        previous_job = dict(route._job)
        route._job.clear()
    try:
        with pytest.raises(OSError, match="second cache"):
            route._promote_session_selected(selected, 1, run_index=False,
                                                settings=route._index_settings(route.load_config()))
        receipt = route._job["promotion_receipt"]
        rows = json.loads(open(receipt).read())["sources"]
        assert len(rows) == 2
        assert rows[0]["before_sha256"] is None
        assert rows[1]["before_sha256"] is None
        assert (tmp_path / "base" / "online_ocr_promotions" /
                receipt.split("/")[-2] / "landed-0000.json").is_file()
        assert not (tmp_path / "base" / "online_ocr_promotions" /
                    receipt.split("/")[-2] / "landed-0001.json").exists()
        assert route._job["promoted_sources"] == ["one.png"]
    finally:
        with route._lock:
            route._job.clear()
            route._job.update(previous_job)


def test_online_promotion_full_rebuild_keeps_local_visual_models_disabled(tmp_path, monkeypatch):
    from api.routes import online_ocr as route
    monkeypatch.setattr(route.obsidian_manager, "heartbeat", lambda epoch: True)
    monkeypatch.setattr(route, "_cancel", threading.Event())

    vault = tmp_path / "vault"
    vault.mkdir()
    image = vault / "figure.png"
    image.write_bytes(_png())
    monkeypatch.setattr(route, "BASE_DIR", str(tmp_path / "base"))
    monkeypatch.setattr(route, "OBSIDIAN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(route, "load_config", lambda: {"provider": "lm_studio"})
    settings = {"provider": "lm_studio", "llm": "chat", "embed": "embeddinggemma-300m",
                "embed_provider": "lm_studio"}
    monkeypatch.setattr(route, "_index_settings", lambda *_: settings)
    monkeypatch.setattr(route, "_preflight_index_settings", lambda *_, **__: None)
    monkeypatch.setattr(route.obsidian_manager, "begin_index_run", lambda *_: None)
    monkeypatch.setattr(route.obsidian_manager, "get_status", lambda: "done")
    calls = []
    monkeypatch.setattr(route.obsidian_manager, "index_vault",
                        lambda *args, **kwargs: calls.append(kwargs))
    selected = [(vault, image, "figure.png",
                 {"source_sha256": route._digest(image), "recipe": "r"}, "description")]
    with route._lock:
        previous_job = dict(route._job)
        route._job.clear()
    try:
        route._promote_session_selected(selected, 1, run_index=True,
                                        settings=settings, full_rebuild=True)
        assert calls == [{"provider_name": "lm_studio", "op_epoch": 1,
                          "online_extraction": True, "force_fresh": True,
                          "extraction_manifest": {"approved": {"figure.png": route._digest(image)}}}]
    finally:
        with route._lock:
            route._job.clear()
            route._job.update(previous_job)


@pytest.mark.parametrize("provider,model,key", [
    ("openai", "gpt-5-mini", "OPENAI_API_KEY"),
    ("google", "gemini-2.5-flash-lite", "GOOGLE_API_KEY"),
    ("openrouter", "google/gemini-3.8-flash", "OPENROUTER_API_KEY"),
])
def test_other_provider_request_and_response_projection(monkeypatch, provider, model, key):
    monkeypatch.setenv(key, "fake-key")

    def handler(request):
        body = json.loads(request.content)
        if provider == "google":
            assert body["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "image/png"
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": "Visible text"}]},
                                "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 3}})
        assert body["model"] == model
        assert body["messages"][0]["content"][1]["image_url"]["url"].startswith(
            "data:image/png;base64,")
        assert ("max_completion_tokens" in body) == (provider == "openai")
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "Visible text"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 3}})

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    result = OnlineOCR(provider, model).recognize(_png(), lambda: False, lambda: None)
    assert result["status"] == "ocr"
    assert result["text"] == "Visible text"


def test_image_receipt_replay_and_explicit_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    image = inputs / "picture.png"
    image.write_bytes(_png())
    storage = tmp_path / "storage"
    calls = []

    def fake_recognize(self, raw, cancelled, heartbeat):
        calls.append(raw)
        return {"status": "timeout" if len(calls) == 1 else "ocr",
                "text": "" if len(calls) == 1 else "text", "calls": 1, "call_s": 0.1}

    monkeypatch.setattr(OnlineOCR, "recognize", fake_recognize)
    engine = OnlineOCR("deepseek", "deepseek-flash")
    first = extract_image(image, inputs, storage, engine)
    assert first["status"] == "timeout"
    assert extract_image(image, inputs, storage, engine) == first
    second = extract_image(image, inputs, storage, engine, retry=True)
    assert second["status"] == "ocr" and second["attempt"] == 2
    assert len(calls) == 2


def test_conversion_is_bounded():
    data, mime = normalise_image(_png())
    assert data and mime == "image/png"
    try:
        normalise_image(_png(), max_pixels=100)
    except ValueError:
        pass
    else:
        raise AssertionError("Pixel limit was ignored")


def test_existing_image_text_is_not_sent(tmp_path, monkeypatch):
    from core import constants
    from rag.storage_io import vault_key
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    image = inputs / "picture.png"
    image.write_bytes(_png())
    import hashlib
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    cache = tmp_path / "cache" / "image_cache" / vault_key(inputs)
    cache.mkdir(parents=True)
    (cache / f"{digest}.txt").write_text("already extracted")
    monkeypatch.setattr(constants, "OBSIDIAN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(OnlineOCR, "recognize", lambda *_: (_ for _ in ()).throw(
        AssertionError("image was sent")))
    result = extract_image(image, inputs, tmp_path / "storage",
                           OnlineOCR("deepseek", "deepseek-flash"))
    assert result["status"] == "existing_text"


def test_explicit_reextract_bypasses_existing_image_cache(tmp_path, monkeypatch):
    from core import constants
    from rag.storage_io import vault_key
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "figure.png"
    source.write_bytes(_png())
    import hashlib
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    cache = tmp_path / "cache" / "image_cache" / vault_key(vault)
    cache.mkdir(parents=True)
    (cache / f"{digest}.txt").write_text("old description")
    monkeypatch.setattr(constants, "OBSIDIAN_CACHE_DIR", str(tmp_path / "cache"))
    calls = []
    monkeypatch.setattr(OnlineOCR, "recognize", lambda self, *_: (
        calls.append(self.task) or {"status": "ocr", "text": "new description",
                                   "calls": 1, "call_s": .1}))
    result = extract_image(source, vault, tmp_path / "attempts",
                           OnlineOCR("deepseek", "deepseek-flash", task="vision"),
                           reextract=True)
    assert result["status"] == "ocr" and result["text"] == "new description"
    assert calls == ["vision"]
    assert (cache / f"{digest}.txt").read_text() == "old description"


def test_route_preview_rejects_vault_escape(tmp_path, monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(route, "load_config", lambda: {"obsidian_vault_path": str(vault)})
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    client = app.test_client()
    response = client.post("/api/online-ocr/preview", json={"path": "../outside.png"})
    assert response.status_code == 400
    assert "vault-relative" in response.json["error"]


def test_route_requires_explicit_external_confirmation(tmp_path, monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    response = app.test_client().post("/api/online-ocr/start", json={
        "path": "image.png", "provider": "deepseek", "model": "deepseek-flash"})
    assert response.status_code == 400
    assert "Confirm external" in response.json["error"]


def test_completed_native_pdf_is_promotable_without_provider_call(tmp_path, monkeypatch):
    import fitz
    from api.routes import online_ocr as route
    from core.page_ocr import extract_pages
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "paper.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), "This native paragraph has more than forty characters and needs no OCR.")
    doc.save(source)
    doc.close()
    monkeypatch.setattr(route, "BASE_DIR", str(tmp_path / "base"))
    monkeypatch.setattr(route.obsidian_manager, "_selected_pdf_payload", lambda *_: None)
    engine = OnlineOCR("deepseek", "deepseek-flash")
    result = extract_pages(source, vault, tmp_path / "base" / "online_ocr_attempts",
                           engine, resource_guard=lambda: "")
    assert result["coverage"] == "processed_unverified"
    job = {"recipe": result["payload"]["recipe"],
           "source_sha256": result["payload"]["source_sha256"],
           "snapshot_id": result["snapshot_id"]}
    text = route._promotable_text(source, vault, job)
    assert "native paragraph" in text


def test_complete_pdf_ranges_are_joined_only_with_contiguous_coverage(tmp_path, monkeypatch):
    import fitz
    from api.routes import online_ocr as route
    from core.page_ocr import extract_pages

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "long.pdf"
    document = fitz.open()
    for number in (1, 2):
        page = document.new_page()
        page.insert_text((50, 50), f"Native page {number} has enough text for the extraction gate.")
    document.save(source)
    document.close()
    monkeypatch.setattr(route, "BASE_DIR", str(tmp_path / "base"))
    monkeypatch.setattr(route.obsidian_manager, "_selected_pdf_payload", lambda *_: None)
    engine = OnlineOCR("deepseek", "deepseek-flash")
    parts = [extract_pages(source, vault, tmp_path / "base" / "online_ocr_attempts", engine,
                           start_page=first, end_page=first + 1,
                           resource_guard=lambda: "") for first in (0, 1)]
    assert all(part["coverage"] == "processed_unverified" for part in parts)
    job = {"recipe": parts[0]["payload"]["recipe"],
           "source_sha256": parts[0]["payload"]["source_sha256"],
           "snapshots": [part["snapshot_id"] for part in parts]}
    text = route._promotable_text(source, vault, job)
    assert text.index("Native page 1") < text.index("Native page 2")
    with pytest.raises(ValueError, match="coverage is incomplete"):
        route._promotable_text(source, vault, {**job, "snapshots": job["snapshots"][:1]})


def test_index_policy_keeps_native_text_with_undescribed_image_warning(tmp_path, monkeypatch):
    import fitz
    from core.page_ocr import extract_pages

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "mixed.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((50, 50), "Native content has more than forty characters and remains searchable.")
    page.insert_image(fitz.Rect(50, 150, 550, 750), stream=_png())
    document.save(source)
    document.close()
    monkeypatch.setattr(OnlineOCR, "recognize", lambda *_: (_ for _ in ()).throw(
        AssertionError("native page was sent for OCR")))
    engine = OnlineOCR("deepseek", "deepseek-flash")
    default = extract_pages(source, vault, tmp_path / "attempts", engine,
                            resource_guard=lambda: "")
    assert default["coverage"] == "partial"
    indexed = extract_pages(source, vault, tmp_path / "attempts", engine,
                            accept_mixed_native=True, resource_guard=lambda: "")
    assert indexed["coverage"] == "processed_unverified"
    assert indexed["pages"][0]["original_status"] == "mixed_unverified"
    assert "embedded_image_content_not_described" in indexed["pages"][0]["quality_flags"]
    assert indexed["payload"]["recipe"] != default["payload"]["recipe"]


def test_online_preview_counts_vector_page_without_native_text(tmp_path):
    import fitz
    from api.routes import online_ocr as route

    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "vector.pdf"
    with fitz.open() as document:
        page = document.new_page()
        page.draw_rect(fitz.Rect(40, 40, 300, 300), color=(0, 0, 0))
        document.new_page()
        document.save(source)
    row = route._preview(vault, source, source.name)
    assert row["eligible_pages"] == [1]


def test_session_preview_only_includes_referenced_images(tmp_path, monkeypatch):
    from api.routes import online_ocr as route
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("![[wanted.png]]")
    (vault / "wanted.png").write_bytes(_png())
    (vault / "other.png").write_bytes(_png())
    monkeypatch.setattr(route, "load_config", lambda: {
        "vault_exclude_dirs": [], "vault_image_exts": [".png"]})
    inventory = route._session_inventory(vault)
    assert [row["source"] for row in inventory["sources"]] == ["wanted.png"]
    assert inventory["eligible"] == 1


def test_session_start_refuses_stale_preview_without_provider_call(tmp_path, monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("![[wanted.png]]")
    (vault / "wanted.png").write_bytes(_png())
    monkeypatch.setattr(route, "load_config", lambda: {
        "obsidian_vault_path": str(vault), "vault_exclude_dirs": [],
        "vault_image_exts": [".png"]})
    monkeypatch.setattr(route.OnlineOCR, "recognize", lambda *_: (_ for _ in ()).throw(
        AssertionError("stale session sent an image")))
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    client = app.test_client()
    preview = client.post("/api/online-ocr/session/preview").json
    (vault / "wanted.png").write_bytes(_png() + b"changed")
    response = client.post("/api/online-ocr/session/start", json={
        "confirm_external": True, "session_token": preview["session_token"],
        "provider": "deepseek", "model": "deepseek-flash"})
    assert response.status_code == 400
    assert "changed" in response.json["error"]


def test_complete_session_promotion_refuses_partial_before_cache_or_index(monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route

    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    with route._lock:
        before = dict(route._job)
        route._job.clear()
        route._job.update({"state": "done", "scope": "vault_session",
                           "expected_sources": ["a.pdf", "b.png"],
                           "results": [{"source": "a.pdf", "status": "processed_unverified"},
                                       {"source": "b.png", "status": "timeout"}]})
    try:
        monkeypatch.setattr(route, "_source", lambda *_: (_ for _ in ()).throw(
            AssertionError("promotion reached source writes")))
        response = app.test_client().post("/api/online-ocr/session/promote", json={
            "confirm_index": True, "require_complete": True})
        assert response.status_code == 409
        assert "incomplete" in response.json["error"]
    finally:
        with route._lock:
            route._job.clear()
            route._job.update(before)


def test_finish_incomplete_requires_exact_skips_and_records_barrier(tmp_path, monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route

    vault = tmp_path / "vault"
    vault.mkdir()
    good = vault / "good.pdf"
    bad = vault / "bad.pdf"
    good.write_bytes(b"good")
    bad.write_bytes(b"bad")
    base = tmp_path / "base"
    run_id = "a" * 32
    run_dir = base / "online_index_runs" / run_id
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(route, "BASE_DIR", str(base))
    monkeypatch.setattr(route, "load_config", lambda: {"obsidian_vault_path": str(vault)})
    monkeypatch.setattr(route, "_index_settings", lambda *_: {"embed": "local"})
    monkeypatch.setattr(route, "_preflight_index_settings", lambda *_, **kwargs: None)
    inventory = {"source_count": 2, "eligible": 1, "session_token": "fresh",
                 "sources": [
                     {"source": "good.pdf", "source_sha256": route._digest(good)},
                     {"source": "bad.pdf", "source_sha256": route._digest(bad)}]}
    monkeypatch.setattr(route, "_session_inventory", lambda *_args, **_kwargs: inventory)
    run = {"full_rebuild": True, "session_token": "fresh", "source_count": 2,
           "eligible": 1, "vault_key": route.vault_key(vault),
           "index_settings": {"embed": "local"}, "attempt_generation": "b" * 32,
           "ocr": OnlineOCR("deepseek", "deepseek-flash").identity(),
           "vision": OnlineOCR("deepseek", "deepseek-flash", task="vision").identity()}
    (run_dir / "run.json").write_text(json.dumps(run))
    (run_dir / "source-000000.json").write_text(json.dumps({
        "source": "good.pdf", "source_sha256": route._digest(good),
        "status": "processed_unverified"}))
    (run_dir / "source-000001.json").write_text(json.dumps({
        "source": "bad.pdf", "status": "partial"}))
    monkeypatch.setattr(route.obsidian_manager, "wait_for_indexing", lambda **_: True)
    monkeypatch.setattr(route.obsidian_manager, "try_acquire_lock", lambda **_: 1)
    monkeypatch.setattr(route.obsidian_manager, "release_lock", lambda *_, **kwargs: None)
    threads = []
    monkeypatch.setattr(route.obsidian_manager, "register_index_thread", threads.append)
    monkeypatch.setattr(route, "_promotable_text", lambda *_: "verified text")
    promoted = []
    monkeypatch.setattr(route, "_promote_session_selected",
                        lambda selected, *_args, **kwargs: promoted.append((selected, kwargs)))
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    body = {"run_id": run_id, "session_token": "fresh", "confirm_index": True,
            "confirm_full_rebuild": True, "confirm_skip_incomplete": True,
            "skip_sources": []}
    with route._lock:
        before = dict(route._job)
        route._job.clear()
        route._job["state"] = "idle"
    try:
        client = app.test_client()
        refused = client.post("/api/online-ocr/session/finish-incomplete", json=body)
        assert refused.status_code == 409
        assert not (run_dir / "accepted-incomplete.json").exists()
        accepted = client.post("/api/online-ocr/session/finish-incomplete", json={
            **body, "skip_sources": ["bad.pdf"]})
        assert accepted.status_code == 200
        threads[-1].join(timeout=3)
        assert not threads[-1].is_alive()
        manifest = json.loads((run_dir / "accepted-incomplete.json").read_text())
        assert manifest["skipped"] == [{
            "source": "bad.pdf", "source_sha256": route._digest(bad),
            "status": "partial", "receipt": "source-000001.json"}]
        assert (run_dir / "promotion-started.json").is_file()
        assert len(promoted) == 1
        assert [row[2] for row in promoted[0][0]] == ["good.pdf"]
        assert promoted[0][1]["full_rebuild"] is True
        assert promoted[0][1]["manifest"] == {
            "approved": {"good.pdf": route._digest(good)}, "deferred": ["bad.pdf"]}
    finally:
        with route._lock:
            route._job.clear()
            route._job.update(before)


@pytest.mark.parametrize("prepare_only", [False, True])
def test_cached_only_session_reaches_promotion_without_extraction(tmp_path, monkeypatch, prepare_only):
    from flask import Flask
    from api.routes import online_ocr as route

    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = {"obsidian_vault_path": str(vault), "provider": "ollama"}
    inventory = {"sources": [], "reused": [{"source": "cached.pdf", "source_sha256": "a" * 64}],
                 "source_count": 0, "reused_count": 1, "eligible": 0, "session_token": "cached-only"}
    monkeypatch.setattr(route, "load_config", lambda: cfg)
    monkeypatch.setattr(route, "BASE_DIR", str(tmp_path / "base"))
    monkeypatch.setattr(route, "_session_inventory", lambda *a, **kw: inventory.copy())
    monkeypatch.setattr(route, "_preflight_index_settings", lambda *a, **kw: None)
    monkeypatch.setattr(route, "_job", {"state": "idle"})
    monkeypatch.setattr(route, "_cancel", threading.Event())
    monkeypatch.setattr(route.obsidian_manager, "get_status", lambda: "idle")
    monkeypatch.setattr(route.obsidian_manager, "try_acquire_lock", lambda ttl: 1)
    monkeypatch.setattr(route.obsidian_manager, "release_lock", lambda epoch: None)
    threads, promoted = [], []
    monkeypatch.setattr(route.obsidian_manager, "register_index_thread", threads.append)
    monkeypatch.setattr(route, "extract_pages", lambda *a, **kw: pytest.fail("Cached source re-extracted"))
    monkeypatch.setattr(route, "extract_image", lambda *a, **kw: pytest.fail("Cached image re-extracted"))

    def promote(selected, epoch, **kwargs):
        promoted.append((selected, kwargs, route._job["extraction_manifest"]))
        route._job["state"] = "text_prepared" if kwargs["prepare_only"] else "indexed"

    monkeypatch.setattr(route, "_promote_session_selected", promote)
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    response = app.test_client().post("/api/online-ocr/session/start", json={
        "run_index": True, "confirm_index": True, "no_external": True,
        "session_token": "cached-only", "prepare_only": prepare_only})
    try:
        assert response.status_code == 200
        threads[0].join(timeout=3)
        assert not threads[0].is_alive()
        assert route._job["state"] == ("text_prepared" if prepare_only else "indexed")
        assert len(promoted) == 1
        assert promoted[0][0] == []
        assert promoted[0][1]["prepare_only"] is prepare_only
        assert promoted[0][2] == {"approved": {"cached.pdf": "a" * 64}, "deferred": []}
        directory = tmp_path / "base" / "online_index_runs" / response.json["run_id"]
        assert json.loads((directory / "promotion-started.json").read_text())["session_token"] == "cached-only"
    finally:
        for thread in threads:
            thread.join(timeout=3)


def test_online_index_stages_vision_then_promotes_complete_session(tmp_path, monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("![[figure.png]]")
    (vault / "figure.png").write_bytes(_png())
    cfg = {"obsidian_vault_path": str(vault), "vault_exclude_dirs": [],
           "vault_image_exts": [".png"], "provider": "ollama", "embed": "embeddinggemma-300m"}
    monkeypatch.setattr(route, "load_config", lambda: cfg)
    monkeypatch.setattr(route, "BASE_DIR", str(tmp_path / "base"))
    monkeypatch.setattr(route, "_preflight_index_settings", lambda *_, **__: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(route.obsidian_manager, "get_status", lambda: "idle")
    monkeypatch.setattr(route.obsidian_manager, "try_acquire_lock", lambda ttl: 1)
    monkeypatch.setattr(route.obsidian_manager, "release_lock", lambda epoch: None)
    monkeypatch.setattr(route.obsidian_manager, "_heartbeat_or_stop", lambda epoch: None)
    threads = []
    monkeypatch.setattr(route.obsidian_manager, "register_index_thread", threads.append)
    tasks = []

    def fake_extract(source, root, storage, engine, **kwargs):
        tasks.append((engine.task, kwargs["reextract"]))
        return {"status": "ocr", "binding": {"recipe": "r"}, "attempt": 1}

    monkeypatch.setattr(route, "extract_image", fake_extract)
    monkeypatch.setattr(route, "export_image", lambda *_: tmp_path / "export.txt")
    monkeypatch.setattr(route, "_promotable_text", lambda *_: "A labelled figure")
    promoted = []
    monkeypatch.setattr(route, "_promote_session_selected",
                        lambda selected, epoch, **kwargs: promoted.append((selected, kwargs)))
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    with route._lock:
        before = dict(route._job)
        route._job.clear()
        route._job["state"] = "idle"
    try:
        client = app.test_client()
        preview = client.post("/api/online-ocr/session/preview", json={"run_index": True, "reextract": True}).json
        assert preview["eligible"] == 1
        refused = client.post("/api/online-ocr/session/start", json={
            "run_index": True, "reextract": True, "confirm_external": True,
            "confirm_index": True, "full_rebuild": True,
            "session_token": preview["session_token"],
            "provider": "deepseek", "model": "deepseek-flash"})
        assert refused.status_code == 400
        assert "Full rebuild requires" in refused.json["error"]
        response = client.post("/api/online-ocr/session/start", json={
            "run_index": True, "reextract": True, "confirm_external": True,
            "confirm_index": True, "full_rebuild": True,
            "confirm_full_rebuild": True,
            "session_token": preview["session_token"],
            "provider": "deepseek", "model": "deepseek-flash"})
        assert response.status_code == 200
        threads[0].join(timeout=3)
        assert not threads[0].is_alive()
        assert tasks == [("vision", True)]
        assert len(promoted) == 1
        assert promoted[0][0][0][2] == "figure.png"
        assert promoted[0][1]["run_index"] is True
        assert promoted[0][1]["full_rebuild"] is True
        receipt = next((tmp_path / "base" / "online_index_runs").glob("*/run.json"))
        saved = json.loads(receipt.read_text())
        assert saved["session_token"] == preview["session_token"]
        assert saved["vision"]["task"] == "vision"
        assert saved["full_rebuild"] is True
        assert (receipt.parent / "source-000000.json").is_file()
    finally:
        with route._lock:
            route._job.clear()
            route._job.update(before)


def test_online_rebuild_reextracts_native_only_pdf(tmp_path, monkeypatch):
    import fitz
    from flask import Flask
    from api.routes import online_ocr as route

    vault = tmp_path / "vault"
    vault.mkdir()
    pdf = vault / "native.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Native text that must be extracted afresh")
    doc.save(pdf)
    doc.close()
    cfg = {"obsidian_vault_path": str(vault), "vault_exclude_dirs": [],
           "vault_image_exts": [], "provider": "lm_studio"}
    monkeypatch.setattr(route, "load_config", lambda: cfg)
    monkeypatch.setattr(route, "BASE_DIR", str(tmp_path / "base"))
    monkeypatch.setattr(route, "_preflight_index_settings", lambda *_, **__: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(route.obsidian_manager, "get_status", lambda: "idle")
    monkeypatch.setattr(route.obsidian_manager, "try_acquire_lock", lambda ttl: 1)
    monkeypatch.setattr(route.obsidian_manager, "release_lock", lambda epoch: None)
    monkeypatch.setattr(route.obsidian_manager, "_heartbeat_or_stop", lambda epoch: None)
    threads = []
    monkeypatch.setattr(route.obsidian_manager, "register_index_thread", threads.append)
    extracted = []
    def fake_extract(source, root, storage, engine, **kwargs):
        extracted.append((source.name, kwargs["accept_mixed_native"]))
        return {"coverage": "processed_unverified", "range": [0, 1],
                "snapshot_id": "native", "payload": {"recipe": "r"}}
    monkeypatch.setattr(route, "extract_pages", fake_extract)
    monkeypatch.setattr(route, "export_pages", lambda *_: tmp_path / "export.md")
    monkeypatch.setattr(route, "_promotable_text", lambda *_: "fresh native text")
    promoted = []
    monkeypatch.setattr(route, "_promote_session_selected",
                        lambda selected, epoch, **kwargs: promoted.extend(selected))
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    with route._lock:
        before = dict(route._job)
        route._job.clear()
        route._job["state"] = "idle"
    try:
        client = app.test_client()
        preview = client.post("/api/online-ocr/session/preview",
                              json={"run_index": True, "reextract": True}).json
        assert preview["source_count"] == 1
        assert preview["eligible"] == 0
        response = client.post("/api/online-ocr/session/start", json={
            "run_index": True, "reextract": True, "confirm_external": True,
            "confirm_index": True, "full_rebuild": True,
            "confirm_full_rebuild": True,
            "session_token": preview["session_token"],
            "provider": "deepseek", "model": "deepseek-flash"})
        assert response.status_code == 200
        threads[0].join(timeout=3)
        assert extracted == [("native.pdf", True)]
        assert len(promoted) == 1
    finally:
        with route._lock:
            route._job.clear()
            route._job.update(before)


def test_online_run_resumes_completed_source_without_recalling_provider(tmp_path, monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("![[a.png]] ![[b.png]]")
    for name in ("a.png", "b.png"):
        (vault / name).write_bytes(_png() + name.encode())
    cfg = {"obsidian_vault_path": str(vault), "vault_exclude_dirs": [],
           "vault_image_exts": [".png"], "provider": "lm_studio"}
    monkeypatch.setattr(route, "load_config", lambda: cfg)
    monkeypatch.setattr(route, "BASE_DIR", str(tmp_path / "base"))
    monkeypatch.setattr(route, "_preflight_index_settings", lambda *_, **__: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(route.obsidian_manager, "get_status", lambda: "idle")
    monkeypatch.setattr(route.obsidian_manager, "try_acquire_lock", lambda ttl: 1)
    monkeypatch.setattr(route.obsidian_manager, "release_lock", lambda epoch: None)
    monkeypatch.setattr(route.obsidian_manager, "_heartbeat_or_stop", lambda epoch: None)
    threads = []
    monkeypatch.setattr(route.obsidian_manager, "register_index_thread", threads.append)
    calls = []
    def fake_extract(source, root, storage, engine, **kwargs):
        calls.append((source.name, kwargs["attempt_generation"]))
        if len(calls) == 1:
            route._cancel.set()
        return {"status": "ocr", "binding": {"recipe": "r"}, "attempt": 1}
    monkeypatch.setattr(route, "extract_image", fake_extract)
    monkeypatch.setattr(route, "export_image", lambda *_: tmp_path / "export.md")
    monkeypatch.setattr(route, "_promotable_text", lambda *_: "description")
    promoted = []
    monkeypatch.setattr(route, "_promote_session_selected",
                        lambda selected, epoch, **kwargs: promoted.extend(selected))
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    with route._lock:
        before = dict(route._job)
        route._job.clear()
        route._job["state"] = "idle"
    try:
        client = app.test_client()
        preview = client.post("/api/online-ocr/session/preview",
                              json={"run_index": True, "reextract": True}).json
        body = {"run_index": True, "reextract": True, "confirm_external": True,
                "confirm_index": True, "full_rebuild": True,
                "confirm_full_rebuild": True, "retry_failed": True,
                "session_token": preview["session_token"],
                "provider": "deepseek", "model": "deepseek-flash"}
        assert client.post("/api/online-ocr/session/start", json=body).status_code == 200
        threads[-1].join(timeout=3)
        assert route._job["state"] == "cancelled"
        run_id = Path(route._job["run_receipt"]).parent.name
        assert client.post("/api/online-ocr/session/start", json={
            **body, "resume_run_id": run_id}).status_code == 200
        threads[-1].join(timeout=3)
        assert [name for name, _ in calls] == ["a.png", "b.png"]
        assert calls[0][1] == calls[1][1]
        assert len(promoted) == 2
        with route._lock:
            route._job["state"] = "failed"  # Simulate a restart after promotion began.
        refused = client.post("/api/online-ocr/session/start", json={
            **body, "resume_run_id": run_id})
        assert refused.status_code == 409
        assert "promotion already started" in refused.json["error"]
    finally:
        with route._lock:
            route._job.clear()
            route._job.update(before)


def test_online_index_incomplete_source_never_promotes_or_indexes(tmp_path, monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route

    vault = tmp_path / "vault"
    vault.mkdir()
    image = vault / "figure.png"
    image.write_bytes(_png())
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(route, "BASE_DIR", str(tmp_path / "base"))
    monkeypatch.setattr(route, "load_config", lambda: {
        "obsidian_vault_path": str(vault), "provider": "ollama", "embed": "embeddinggemma-300m"})
    monkeypatch.setattr(route, "_preflight_index_settings", lambda *_, **__: None)
    monkeypatch.setattr(route, "_session_inventory", lambda *_args, **_kwargs: {
        "sources": [{"source": "figure.png", "source_sha256": route._digest(image),
                     "kind": "image", "eligible": 1, "pages": 1, "has_cached_text": False}],
        "source_count": 1, "eligible": 1, "session_token": "t"})
    monkeypatch.setattr(route.obsidian_manager, "get_status", lambda: "idle")
    monkeypatch.setattr(route.obsidian_manager, "try_acquire_lock", lambda ttl: 1)
    monkeypatch.setattr(route.obsidian_manager, "release_lock", lambda *_: None)
    monkeypatch.setattr(route.obsidian_manager, "_heartbeat_or_stop", lambda *_: None)
    threads = []
    monkeypatch.setattr(route.obsidian_manager, "register_index_thread", threads.append)
    monkeypatch.setattr(route, "extract_image", lambda *_args, **_kwargs: (
        _ for _ in ()).throw(RuntimeError("provider failed")))
    monkeypatch.setattr(route, "_promote_session_selected", lambda *_args, **_kwargs: (
        _ for _ in ()).throw(AssertionError("incomplete source was promoted")))
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    with route._lock:
        before = dict(route._job)
        route._job.clear()
        route._job["state"] = "idle"
    try:
        client = app.test_client()
        response = client.post("/api/online-ocr/session/start", json={
            "run_index": True, "reextract": True, "confirm_external": True,
            "confirm_index": True, "session_token": "t",
            "provider": "deepseek", "model": "deepseek-flash"})
        assert response.status_code == 200
        threads[0].join(timeout=3)
        assert not threads[0].is_alive()
        status = client.get("/api/online-ocr/status").json
        assert status["state"] == "extraction_incomplete"
        assert status["unfinished_count"] == 1
        assert status["result_count"] == 1
        assert not list((tmp_path / "base").glob("online_ocr_promotions/*"))
    finally:
        with route._lock:
            route._job.clear()
            route._job.update(before)


def test_online_index_refuses_changed_inventory_before_promotion(tmp_path, monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route

    vault = tmp_path / "vault"
    vault.mkdir()
    image = vault / "figure.png"
    image.write_bytes(_png())
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(route, "BASE_DIR", str(tmp_path / "base"))
    monkeypatch.setattr(route, "load_config", lambda: {
        "obsidian_vault_path": str(vault), "provider": "ollama", "embed": "embeddinggemma-300m"})
    monkeypatch.setattr(route, "_preflight_index_settings", lambda *_, **__: None)
    inventories = []

    def inventory(*_args, **_kwargs):
        inventories.append(1)
        return {"sources": [{"source": "figure.png", "source_sha256": route._digest(image),
                             "kind": "image", "eligible": 1, "pages": 1,
                             "has_cached_text": False}],
                "source_count": 1, "eligible": 1,
                "session_token": "t" if len(inventories) == 1 else "changed"}

    monkeypatch.setattr(route, "_session_inventory", inventory)
    monkeypatch.setattr(route.obsidian_manager, "get_status", lambda: "idle")
    monkeypatch.setattr(route.obsidian_manager, "try_acquire_lock", lambda ttl: 1)
    monkeypatch.setattr(route.obsidian_manager, "release_lock", lambda *_: None)
    monkeypatch.setattr(route.obsidian_manager, "_heartbeat_or_stop", lambda *_: None)
    threads = []
    monkeypatch.setattr(route.obsidian_manager, "register_index_thread", threads.append)
    monkeypatch.setattr(route, "extract_image", lambda *_args, **_kwargs: {
        "status": "ocr", "binding": {"recipe": "r"}, "attempt": 1})
    monkeypatch.setattr(route, "export_image", lambda *_: tmp_path / "export.txt")
    monkeypatch.setattr(route, "_promote_session_selected", lambda *_args, **_kwargs: (
        _ for _ in ()).throw(AssertionError("changed inventory was promoted")))
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    with route._lock:
        before = dict(route._job)
        route._job.clear()
        route._job["state"] = "idle"
    try:
        response = app.test_client().post("/api/online-ocr/session/start", json={
            "run_index": True, "reextract": True, "confirm_external": True,
            "confirm_index": True, "session_token": "t",
            "provider": "deepseek", "model": "deepseek-flash"})
        assert response.status_code == 200
        threads[0].join(timeout=3)
        assert not threads[0].is_alive()
        status = app.test_client().get("/api/online-ocr/status").json
        assert status["state"] == "failed"
        assert status["error"] == "ValueError"
        assert len(inventories) == 2
    finally:
        with route._lock:
            route._job.clear()
            route._job.update(before)


def test_online_index_loader_never_calls_local_visual_models(tmp_path, monkeypatch):
    import fitz
    from types import SimpleNamespace
    from rag import vault as vault_module

    root = tmp_path / "vault"
    root.mkdir()
    pdf = root / "page.pdf"
    document = fitz.open()
    document.new_page()
    document.save(pdf)
    document.close()
    monkeypatch.setattr(vault_module, "load_config", lambda: {
        "vault_exclude_dirs": [], "vault_image_exts": []})
    manager = vault_module.ObsidianVaultManager()
    monkeypatch.setattr(manager, "_save_pdf_signature_cache", lambda *_: None)
    monkeypatch.setattr(manager, "_heartbeat_or_stop", lambda *_: None)
    captured = []

    def fake_extract(*args, **kwargs):
        captured.append(kwargs["ocr_cb"])
        return SimpleNamespace(full_text="", extraction_status=None)

    monkeypatch.setattr(vault_module, "extract_structured_from_pdf", fake_extract)
    monkeypatch.setattr(vault_module.glm_ocr_manager, "extract_page_text",
                        lambda *_: (_ for _ in ()).throw(AssertionError("local OCR called")))
    list(manager._load_vault_documents(str(root), online_extraction=True))
    assert captured == [None]

    image = root / "figure.png"
    image.write_bytes(_png())
    monkeypatch.setattr(vault_module.vision_manager, "describe_image",
                        lambda *_: (_ for _ in ()).throw(AssertionError("local vision called")))
    assert manager._extract_image_description(image, root, "figure.png", cache_only=True) == ""


def test_online_index_embedding_preflight_refuses_mismatch_before_model_probe(monkeypatch):
    from api.routes import online_ocr as route
    from core.constants import OBSIDIAN_INDEX_VERSION

    monkeypatch.setattr(route.obsidian_manager, "_read_index_meta",
                        lambda: {"version": OBSIDIAN_INDEX_VERSION,
                                 "embed": "other-model", "embed_provider": "lm_studio"})
    monkeypatch.setattr(route.obsidian_manager, "_incremental_store_is_intact",
                        lambda *_: True)
    settings = {"embed": "embeddinggemma-300m", "embed_provider": "lm_studio"}
    monkeypatch.setattr(route.obsidian_manager, "_index_dir_has_vector_data", lambda *_: True)
    with pytest.raises(ValueError, match="embed_model_mismatch"):
        route._preflight_index_settings(settings)


def test_saved_online_mode_blocks_plain_index_without_explicit_offline_choice(monkeypatch):
    from flask import Flask
    from api.routes import vault as route

    monkeypatch.setattr(route, "load_config", lambda: {
        "vault_online_visual_enabled": True})
    app = Flask(__name__)
    app.register_blueprint(route.vault_bp)
    response = app.test_client().post("/api/obsidian/index", json={})
    assert response.status_code == 409
    assert "Online OCR/vision mode" in response.json["error"]


@pytest.mark.parametrize(('status', 'message', 'code', 'expected'), [
    (400, 'Invalid image data: cannot decode image', 'invalid_image', 'image_rejected'),
    (413, 'Request exceeds payload limit', '', 'request_failed'),
    (415, 'Unsupported content type application/json', '', 'request_failed'),
    (413, 'Account quota exceeded for image payload', '', 'request_failed'),
    (400, 'This model cannot decode the image: corrupt image data', 'invalid_image', 'image_rejected'),
    (415, 'Unsupported image format', '', 'image_rejected'),
    (400, 'This model does not support image input', 'unsupported_image', 'request_failed'),
    (400, 'Unsupported image input for this model', 'unsupported_image', 'request_failed'),
    (401, 'Invalid API key', 'invalid_api_key', 'authentication_failed'),
    (429, 'Rate limit exceeded', '', 'rate_limited'),
    (400, 'Unknown model', 'model_not_found', 'request_failed'),
    (500, 'Service temporarily unavailable', '', 'service_error'),
])
def test_provider_error_details_and_scope(monkeypatch, status, message, code, expected):
    from core.online_runs import source_exception
    secret = 'test-secret-must-never-be-saved'
    monkeypatch.setenv('DEEPSEEK_API_KEY', secret)
    real = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={
        'error': {'message': message + ' ' + secret, 'code': code}}))
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real(transport=transport, **kw))
    result = OnlineOCR('deepseek', 'deepseek-flash').recognize(_png(), lambda: False, lambda: None)
    assert result['status'] == expected
    assert result['http_status'] == status
    assert message in result['error_detail']
    assert secret not in json.dumps(result)
    assert result['provider_code'] == code
    assert result['next_step']
    assert source_exception(result) is (expected == 'image_rejected')


@pytest.mark.parametrize('http_status', [400, 401, 'oversized'])
def test_daily_update_defers_one_bad_image_but_stops_for_account_error(tmp_path, monkeypatch, http_status):
    from flask import Flask
    from api.routes import online_ocr as route
    from core.online_runs import inspect_run
    from rag.storage_io import vault_key

    vault = tmp_path / 'vault'
    vault.mkdir()
    bad_bytes = _oversized_png_header() if http_status == 'oversized' else _png()
    (vault / 'bad.png').write_bytes(bad_bytes)
    image = io.BytesIO()
    Image.new('RGB', (24, 24), 'red').save(image, format='PNG')
    (vault / 'good.png').write_bytes(image.getvalue())
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fake-key')
    monkeypatch.setattr(route, 'BASE_DIR', str(tmp_path / 'base'))
    monkeypatch.setattr(route, 'load_config', lambda: {'obsidian_vault_path': str(vault), 'provider':'ollama', 'embed':'embeddinggemma-300m', 'online_ocr_provider':'deepseek', 'online_ocr_model':'deepseek-flash'})
    monkeypatch.setattr(route, '_preflight_index_settings', lambda *a, **kw: None)
    inventory = {'sources':[{'source':name, 'source_sha256':route._digest(vault / name),
        'kind':'image', 'eligible':1, 'pages':1, 'has_cached_text':False} for name in ('bad.png','good.png')],
        'source_count':2, 'eligible':2, 'session_token':'t'}
    monkeypatch.setattr(route, '_session_inventory', lambda *a, **kw: inventory)
    monkeypatch.setattr(route.obsidian_manager, 'get_status', lambda: 'idle')
    monkeypatch.setattr(route.obsidian_manager, 'try_acquire_lock', lambda **kw: 1)
    monkeypatch.setattr(route.obsidian_manager, 'release_lock', lambda *a: None)
    monkeypatch.setattr(route.obsidian_manager, '_heartbeat_or_stop', lambda *a: None)
    threads, calls, promoted = [], [], []
    monkeypatch.setattr(route.obsidian_manager, 'register_index_thread', threads.append)
    real = httpx.Client
    def respond(request):
        calls.append(request)
        if len(calls) == 1 and http_status != 'oversized':
            return httpx.Response(http_status, json={'error': {'code':'invalid_image' if http_status == 400 else 'invalid_api_key',
                'message':'Invalid image data' if http_status == 400 else 'API key rejected'}})
        return httpx.Response(200, json={'choices':[{'message':{'content':'Visible graph'},'finish_reason':'stop'}]})
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real(transport=transport, **kw))
    monkeypatch.setattr(route, '_promotable_text', lambda *a: 'Visible graph')
    def promote(selected, epoch, **kw):
        promoted.extend(selected)
        route._job['state'] = 'text_prepared'
    monkeypatch.setattr(route, '_promote_session_selected', promote)
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    before = dict(route._job)
    route._job.clear()
    route._job['state'] = 'idle'
    try:
        client = app.test_client()
        response = client.post('/api/online-ocr/session/start', json={
            'run_index':True, 'prepare_only':True, 'confirm_external':True, 'confirm_index':True,
            'session_token':'t', 'provider':'deepseek', 'model':'deepseek-flash'})
        assert response.status_code == 200, response.json
        threads[0].join(timeout=5)
        assert not threads[0].is_alive()
        status = client.get('/api/online-ocr/status').json
        saved = inspect_run(tmp_path / 'base', response.json['run_id'], vault_key(vault))
        assert status['failed_count'] == 1
        if http_status == 'oversized':
            assert saved['sources'][0]['status'] == 'conversion_failed'
            assert len(calls) == 1  # Only the subsequent valid image reached the mocked provider.
        else:
            assert status['source_errors'][0]['http_status'] == http_status
            assert saved['sources'][0]['http_status'] == http_status
            assert saved['sources'][0]['next_step']
        if http_status != 401:
            assert len(calls) == (1 if http_status == 'oversized' else 2)
            assert status['state'] == 'text_prepared'
            assert [item[2] for item in promoted] == ['good.png']
            assert route._job['extraction_manifest']['deferred'] == ['bad.png']
            assert status['deferred_count'] == 1
        else:
            assert len(calls) == 1
            assert promoted == []
            assert status['state'] == 'extraction_incomplete'
            assert saved['failure']['http_status'] == 401
            assert status['not_attempted_count'] == 1
        assert (vault / 'bad.png').read_bytes() == bad_bytes
    finally:
        for thread in threads:
            thread.join(timeout=5)
        route._job.clear()
        route._job.update(before)


@pytest.mark.parametrize('timeout_type', [httpx.ConnectTimeout, httpx.PoolTimeout, httpx.WriteTimeout, httpx.ReadTimeout])
def test_transport_timeouts_do_not_defer_valid_documents(monkeypatch, timeout_type):
    from core.online_runs import source_exception
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fake-key')
    real = httpx.Client
    def respond(request):
        raise timeout_type('fixture outage', request=request)
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real(transport=httpx.MockTransport(respond), **kw))
    result = OnlineOCR('deepseek', 'deepseek-flash').recognize(_png(), lambda: False, lambda: None)
    assert result['failure_scope'] == 'global'
    assert not source_exception(result)


@pytest.mark.parametrize('stop_at', ['before', 'between', 'handoff', 'lost_lease'])
def test_promotion_stop_boundaries_preserve_receipts_and_do_not_index(tmp_path, monkeypatch, stop_at):
    from unittest.mock import Mock
    from flask import Flask
    from api.routes import online_ocr as route
    from core.utils import RagOperationLock

    vault = tmp_path / 'vault'
    vault.mkdir()
    monkeypatch.setattr(route, 'BASE_DIR', str(tmp_path / 'base'))
    monkeypatch.setattr(route, 'OBSIDIAN_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(route, '_cancel', threading.Event())
    monkeypatch.setattr(route, '_job', {'state': 'promoting'})
    monkeypatch.setattr(route, 'load_config', lambda: {'provider': 'ollama'})
    monkeypatch.setattr(route, '_preflight_index_settings', lambda *a, **kw: None)
    op_lock = RagOperationLock()
    monkeypatch.setattr(route.obsidian_manager, '_op_lock', op_lock)
    epoch = route.obsidian_manager.try_acquire_lock(ttl=3600)
    index = Mock()
    begin = Mock()
    monkeypatch.setattr(route.obsidian_manager, 'index_vault', index)
    monkeypatch.setattr(route.obsidian_manager, 'begin_index_run', begin)
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    client = app.test_client()
    selected = []
    for name in ('one.png',) if stop_at == 'handoff' else ('one.png', 'two.png'):
        source = vault / name
        source.write_bytes(name.encode())
        selected.append((vault, source, name, {'source_sha256': route._digest(source), 'recipe': 'fixture'}, 'new text'))
    writes = []
    replacement_epoch = None
    def write(cache, text):
        nonlocal replacement_epoch
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text)
        writes.append(cache)
        if stop_at == 'lost_lease':
            op_lock.force_release()
            replacement_epoch = op_lock.try_acquire_epoch(3600)
        else:
            assert client.post('/api/online-ocr/cancel').json['phase'] == 'promotion'
    monkeypatch.setattr(route.obsidian_manager, '_atomic_write_text', write)
    if stop_at == 'before':
        client.post('/api/online-ocr/cancel')
    route._promote_session_selected(selected, epoch, run_index=True, settings=route._index_settings(route.load_config()))
    index.assert_not_called()
    begin.assert_not_called()
    if stop_at == 'before':
        assert not writes
        assert route._job['state'] == 'cancelled'
    else:
        assert len(writes) == 1
        assert route._job['state'] == 'promotion_or_index_incomplete'
        receipt = Path(route._job['promotion_receipt'])
        assert len(json.loads(receipt.read_text())['sources']) == len(selected)
        assert (receipt.parent / 'landed-0000.json').is_file()
        assert not (receipt.parent / 'landed-0001.json').exists()
        assert route._job['promoted_sources'] == ['one.png']
    if replacement_epoch:
        route.obsidian_manager.release_lock(epoch)
        assert op_lock.heartbeat(replacement_epoch)


def test_png_normalization_reencodes_without_losing_pixels():
    import io
    from PIL import Image, PngImagePlugin
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('fixture', 'source metadata need not reach provider')
    image = Image.new('RGB', (16, 12), (10, 20, 30))
    output = io.BytesIO()
    image.save(output, format='PNG', pnginfo=metadata)
    raw = output.getvalue()
    converted, mime = normalise_image(raw)
    assert mime == 'image/png'
    assert converted != raw
    with Image.open(io.BytesIO(converted)) as decoded:
        assert decoded.tobytes() == image.tobytes()
        assert 'fixture' not in decoded.info


def test_indexing_status_projects_progress_without_draining_messages(monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    monkeypatch.setattr(route, '_job', {'state': 'indexing', 'run_index': True})
    monkeypatch.setattr(route.obsidian_manager, 'get_progress_snapshot',
                        lambda: {'state': 'embedding', 'phase': 'embedding', 'message': '537 embedded'})
    monkeypatch.setattr(route.obsidian_manager, 'drain_status_messages',
                        lambda: pytest.fail('Online status drained the single-reader feed'))
    response = app.test_client().get('/api/online-ocr/status')
    assert response.status_code == 200
    assert response.json['index_progress']['message'] == '537 embedded'
