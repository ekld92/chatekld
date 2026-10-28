"""Explicit online image transcription transport; no automatic index writes.

The caller owns eligibility, source binding, retry and promotion. A transport
response is evidence, not a claim that every mark in the image was transcribed.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import io
import json
import os
import re
from pathlib import Path
from typing import Callable

import httpx
from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:  # Optional runtime; unsupported HEIC is a visible failure.
    pass


_KEYS = {
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
_BASES = {
    "openai": "https://api.openai.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "deepseek": "https://api.deepseek.com",
    "openrouter": "https://openrouter.ai/api/v1",
}
_SUPPORTED = {"image/png", "image/jpeg", "image/webp"}
_MODELS = {
    "deepseek": {"deepseek-flash"},
    "openai": {"gpt-5-mini"},
    "google": {"gemini-2.5-flash-lite", "gemini-3.8-flash"},
    "openrouter": {"google/gemini-3.8-flash"},
}
_PROMPT = ("Transcribe all visible text in reading order. Preserve spelling, "
           "numbers and line breaks. Do not infer missing words or explain the image. "
           "If text is unreadable, mark that span [unreadable].")
_VISION_PROMPT = (
    "Describe this image for search and retrieval. State what it depicts, then "
    "transcribe visible text, labels, axis titles, numbers, and data. Report only "
    "what is visible; mark unreadable text [unreadable] and do not speculate."
)


ERROR_FIELDS = ("stop_reason", "error_detail", "http_status", "provider_code", "provider", "model", "failure_scope", "next_step")


def _http_failure(response: httpx.Response, secret: str) -> dict:
    """Keep bounded provider diagnostics, never request headers or image payloads.

    Only explicit image rejection is source-local. An unknown 400 can also mean
    a broken model or request configuration and must not silently skip the vault.
    """
    from core.llm.redact import redact
    message, code = "", ""
    if len(response.content) <= 64 * 1024:
        try:
            body = response.json()
            error = body.get("error", body) if isinstance(body, dict) else {}
            if isinstance(error, dict):
                message = error.get("message", "")
                code = error.get("code", error.get("status", ""))
            elif isinstance(error, str):
                message = error
        except ValueError:
            # Plain-text provider errors are useful; HTML proxy pages are not.
            if response.headers.get("content-type", "").startswith("text/plain"):
                message = response.text
    def clean(value, limit):
        text = str(value) if isinstance(value, (str, int)) else ""
        if secret:
            text = text.replace(secret, "<redacted>")
        text = redact(text)
        text = re.sub(r"data:[^\s]+", "<image data omitted>", text)
        text = re.sub(r"[A-Za-z0-9+/]{128,}={0,2}", "<long payload omitted>", text)
        return " ".join(text.split())[:limit]
    message, code = clean(message, 2000), clean(code, 100)
    status = response.status_code
    evidence = (code + " " + message).lower()
    configuration = any(term in evidence for term in (
        'api key', 'api_key', 'authentication', 'unauthorized', 'billing', 'quota',
        'unknown model', 'model_not_found', 'model not found', 'model access',
        'unsupported_model', 'model_not_supported', 'model does not support',
        'unsupported image input for this model', 'model cannot accept images',
        'unsupported parameter', 'invalid parameter', 'invalid_parameter',
        'unsupported_parameter', 'permission', 'account', 'credit'))
    image_error = any(term in evidence for term in (
        'invalid_image', 'image_parse', 'image_too_large', 'invalid image',
        'image format', 'image size', 'image dimensions', 'image resolution',
        'decode image', 'decode the image', 'decoding image', 'corrupt image',
        'corrupted image', 'image data', 'image payload', 'unsupported image'))
    # HTTP 413/415 also describe the request envelope, not necessarily its image.
    # Require image evidence and let explicit account/configuration errors win.
    local = status in (400, 413, 415, 422) and image_error and not configuration
    if local:
        category = 'image_rejected'
        action = 'Review this image or replace it with a supported, smaller image, then retry only this document. Other documents continue.'
    elif status in (401, 403):
        category = 'authentication_failed'
        action = 'Check the configured provider key and image-model access, then explicitly retry the unfinished documents.'
    elif status == 402:
        category = 'billing_failed'
        action = 'Check provider credits or billing, then explicitly retry the unfinished documents.'
    elif status == 429:
        category = 'rate_limited'
        action = 'Check provider quota and rate limits; wait before explicitly retrying unfinished documents.'
    else:
        category = 'service_error' if status >= 500 else 'request_failed'
        action = 'Review the provider error and model settings, then explicitly retry the unfinished documents. Earlier completed text is retained.'
    return dict(status=category, http_status=status, provider_code=code,
                error_detail=message or f'Provider returned HTTP {status} without a usable error message.',
                failure_scope='source' if local else 'global', next_step=action)


def normalise_image(raw: bytes, *, max_pixels: int = 20_000_000,
                    max_bytes: int = 20 * 1024 * 1024) -> tuple[bytes, str]:
    """Decode locally, orient and emit bounded RGB PNG or JPEG bytes."""
    if len(raw) > max_bytes:
        raise ValueError("Image exceeds input byte limit")
    if raw.lstrip().startswith(b"<svg") or raw.lstrip().startswith(b"<?xml"):
        import re
        if re.search(rb"(?i)(?:<!doctype|<!entity|<script|foreignobject|\bhref\s*=|url\s*\()", raw):
            raise ValueError("SVG with external or active content is unsupported")
        import fitz
        with fitz.open(stream=raw, filetype="svg") as doc:
            page = doc[0]
            scale = min(3.0, (max_pixels / max(1, page.rect.width * page.rect.height)) ** 0.5)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            data = pix.tobytes("png")
        if len(data) > max_bytes:
            raise ValueError("Converted SVG exceeds byte limit")
        return data, "image/png"
    with Image.open(io.BytesIO(raw)) as opened:
        if opened.width * opened.height > max_pixels:
            raise ValueError("Image exceeds pixel limit")
        image = ImageOps.exif_transpose(opened)
        image.load()
        # Re-encode supported pixels too: source containers/metadata may be
        # rejected remotely even when Pillow can decode them. PNG stays lossless.
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        mime = "image/png" if opened.format == "PNG" or image.mode == "RGBA" else "image/jpeg"
        image.info.clear()
        output = io.BytesIO()
        image.save(output, format="PNG" if mime == "image/png" else "JPEG",
                   quality=92 if mime == "image/jpeg" else None)
        data = output.getvalue()
    if len(data) > max_bytes:
        raise ValueError("Converted image exceeds byte limit")
    return data, mime


@dataclass(frozen=True)
class OnlineOCR:
    provider: str
    model: str
    timeout_s: float = 120
    max_tokens: int = 4096
    task: str = "ocr"

    def identity(self) -> dict:
        if (self.provider not in _KEYS or not self.model or
                self.task not in ("ocr", "vision") or not 5 <= self.timeout_s <= 600):
            raise ValueError("Unsupported online OCR provider, model, or timeout")
        if not 64 <= self.max_tokens <= 16384:
            raise ValueError("Online OCR output limit is outside 64–16384")
        # Explicit allowlist: a chat model name alone is not vision evidence.
        if self.model not in _MODELS[self.provider]:
            raise ValueError("Model is not in the verified image-input allowlist")
        if not (os.environ.get(_KEYS[self.provider]) or
                (self.provider == "google" and os.environ.get("GEMINI_API_KEY"))):
            raise ValueError(f"{_KEYS[self.provider]} is not set")
        prompt = _PROMPT if self.task == "ocr" else _VISION_PROMPT
        return {"engine": "online_image_transcription" if self.task == "ocr" else "online_image_description",
                "task": self.task, "provider": self.provider,
                "model": self.model, "timeout_s": self.timeout_s,
                "max_tokens": self.max_tokens, "prompt": prompt,
                "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

    def recognize(self, png: bytes, cancelled: Callable[[], bool],
                  heartbeat: Callable[[], None]) -> dict:
        import time
        started = time.monotonic()
        if cancelled():
            return {"status": "cancelled", "text": "", "calls": 0, "call_s": 0.0}
        heartbeat()
        try:
            data, mime = normalise_image(png)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            return {"status": "conversion_failed", "text": "", "calls": 0,
                    "call_s": 0.0, "warning": type(exc).__name__,
                    "failure_scope": "source",
                    "error_detail": "Image could not be decoded within the local byte/pixel limits.",
                    "next_step": "Inspect encoding and dimensions; prepare a bounded copy and explicitly retry this source."}
        if mime not in _SUPPORTED:
            return {"status": "unsupported_format", "text": "", "calls": 0,
                    "call_s": 0.0}
        if cancelled():
            return {"status": "cancelled", "text": "", "calls": 0, "call_s": 0.0}
        key = os.environ.get(_KEYS[self.provider]) or os.environ.get("GEMINI_API_KEY")
        assert key
        prompt = _PROMPT if self.task == "ocr" else _VISION_PROMPT
        b64 = base64.b64encode(data).decode("ascii")
        base = os.environ.get(f"{self.provider.upper()}_BASE_URL", _BASES[self.provider]).rstrip("/")
        try:
            with httpx.Client(timeout=self.timeout_s, follow_redirects=False) as client:
                if self.provider == "google":
                    response = client.post(
                        f"{base}/models/{self.model}:generateContent",
                        headers={"x-goog-api-key": key},
                        json={"contents": [{"role": "user", "parts": [
                            {"text": prompt}, {"inline_data": {"mime_type": mime, "data": b64}}]}],
                              "generationConfig": {"maxOutputTokens": self.max_tokens,
                                  **({"thinkingConfig": {"thinkingLevel": "low"}}
                                     if self.model == "gemini-3.8-flash" else {})}})
                else:
                    response = client.post(
                        f"{base}/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"model": self.model,
                              ("max_completion_tokens" if self.provider == "openai"
                               else "max_tokens"): self.max_tokens,
                              **({"usage": {"include": True}}
                                 if self.provider == "openrouter" else {}),
                              **({"thinking": {"type": "disabled"}}
                                 if self.provider == "deepseek" else {}),
                              "messages": [{"role": "user", "content": [
                                  {"type": "text", "text": prompt},
                                  {"type": "image_url", "image_url": {
                                      "url": f"data:{mime};base64,{b64}"}}]}]})
            response.raise_for_status()
            if len(response.content) > 16 * 1024 * 1024:
                raise ValueError("Provider response exceeds byte limit")
            body = response.json()
            if self.provider == "google":
                candidate = body["candidates"][0]
                text = "".join(p.get("text", "") for p in candidate["content"]["parts"]
                               if not p.get("thought"))
                finish = candidate.get("finishReason", "")
                usage = body.get("usageMetadata", {})
                output_tokens = usage.get("candidatesTokenCount")
            else:
                choice = body["choices"][0]
                text = choice["message"]["content"] or ""
                finish = choice.get("finish_reason", "")
                usage = body.get("usage", {})
                output_tokens = usage.get("completion_tokens")
            if len(str(text).encode("utf-8")) > 8 * 1024 * 1024:
                status = "output_limit"
            elif finish in ("length", "MAX_TOKENS") or (
                    isinstance(output_tokens, int) and output_tokens >= self.max_tokens):
                status = "output_limit"
            elif finish not in ("stop", "STOP"):
                status = "incomplete_response"
            else:
                status = "ocr" if str(text).strip() else "empty_unverified"
            self._record_usage(usage, started, success=status == "ocr",
                               error_category="" if status == "ocr" else status)
            return {"status": status, "text": str(text), "calls": 1,
                    "call_s": time.monotonic() - started, "finish_reason": finish,
                    "usage": usage, "response_id": body.get("id", ""),
                    "provider": self.provider, "model": self.model, "task": self.task,
                    "quality_flags": ["online_transcription_completeness_unverified" if self.task == "ocr"
                                      else "online_visual_description_unverified"]}
        except httpx.TimeoutException:
            # A timeout does not establish defective content: even a read timeout
            # may be a provider outage. Never persist it as a document exclusion.
            failure = {"status": "timeout", "failure_scope": "global",
                       "error_detail": "The provider request timed out.",
                       "next_step": "Check provider availability, then explicitly retry unfinished documents; earlier completed text is retained."}
        except httpx.HTTPStatusError as exc:
            failure = _http_failure(exc.response, key)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            failure = {"status": "failed", "failure_scope": "global",
                       "error_detail": f"Provider connection or response failed ({type(exc).__name__}).",
                       "next_step": "Check the provider connection and model settings before retrying."}
        self._record_usage({}, started, success=False, error_category=failure["status"])
        return {**failure, "provider": self.provider, "model": self.model, "text": "", "calls": 1,
                "call_s": time.monotonic() - started,
                "warning": "Remote outcome may be billable; retry explicitly."}

    def _record_usage(self, usage: dict, started: float, *, success: bool,
                      error_category: str) -> None:
        """Retain an attempt even when the provider reports no usable usage."""
        import time
        try:
            from core.llm.types import LLMUsage
            from core.llm.usage import usage_tracker
            details = usage.get("prompt_tokens_details") or {}
            tokens = LLMUsage(
                input_tokens=int(usage.get("prompt_tokens", usage.get("promptTokenCount", 0)) or 0),
                output_tokens=int(usage.get("completion_tokens", usage.get("candidatesTokenCount", 0)) or 0),
                cached_input_tokens=int(details.get(
                    "cached_tokens", usage.get("cachedContentTokenCount", 0)) or 0))
            usage_tracker.record(provider=self.provider, model=self.model,
                                 usage=tokens, latency_ms=int((time.monotonic() - started) * 1000),
                                 stream=False, success=success,
                                 billable=True if tokens.input_tokens or tokens.output_tokens else None,
                                 error_category=error_category,
                                 workflow="online_ocr" if self.task == "ocr" else "online_vision")
        except Exception:
            pass  # Bad usage metadata cannot discard transcription evidence.


def extract_image(source: Path, input_root: Path, storage: Path, engine: OnlineOCR, *,
                  retry: bool = False, reextract: bool = False,
                  attempt_generation: str | None = None,
                  cancelled: Callable[[], bool] = lambda: False) -> dict:
    """Explicit one-image operation with immutable, source-bound attempts.

    The result is deliberately not published into legacy image caches. Callers
    must review status and explicitly promote before indexing it.
    """
    from core.cache_exports import _directory, _locked, _publish_new, _safe_directory
    from core.constants import (VAULT_IMAGE_EXTS, OBSIDIAN_CACHE_DIR,
                                OBSIDIAN_INDEX_DIR)
    from rag.storage_io import vault_key

    source = source.absolute()
    inputs, target = input_root.resolve(), storage.resolve()
    if (source.is_symlink() or not source.resolve().is_relative_to(inputs)
            or target.is_relative_to(inputs) or inputs.is_relative_to(target)
            or source.suffix.lower() not in VAULT_IMAGE_EXTS):
        raise ValueError("Unsafe source/storage boundary or unsupported image extension")
    identity = engine.identity()
    if attempt_generation is not None and (len(attempt_generation) != 32 or
            any(char not in '0123456789abcdef' for char in attempt_generation)):
        raise ValueError('Invalid attempt generation')
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    key = vault_key(inputs)
    existing = (Path(OBSIDIAN_CACHE_DIR) / "image_cache" / key / f"{digest}.txt",
                Path(OBSIDIAN_INDEX_DIR) / "image_cache" / key / f"{digest}.txt")
    if not reextract:
        for cache in existing:
            if cache.is_file() and not cache.is_symlink() and cache.stat().st_size:
                return {"status": "existing_text", "coverage": "existing_unverified",
                        "text": "", "calls": 0, "source_sha256": digest,
                        "warning": "Existing image text retained; use reextract explicitly."}
    recipe_identity = {**identity, **({'attempt_generation': attempt_generation}
                                      if attempt_generation else {})}
    recipe = hashlib.sha256(json.dumps(recipe_identity, sort_keys=True).encode()).hexdigest()
    root = _safe_directory(storage, ())
    with _locked(root / "worker.lock"):
        directory = _safe_directory(root, (recipe, digest))
        binding = {"source_sha256": digest, "recipe": recipe, "image": source.name}
        for attempt in (2, 1):
            result_file = directory / f"image-a{attempt}.json"
            if result_file.exists():
                wrapper = json.loads(result_file.read_text(encoding="utf-8"))
                previous = wrapper["record"]
                if wrapper.get("sha256") != hashlib.sha256(json.dumps(
                        previous, sort_keys=True, ensure_ascii=False).encode()).hexdigest():
                    raise ValueError("Image receipt changed")
                if previous.get("binding") != binding:
                    raise ValueError("Image receipt binding mismatch")
                if previous["status"] == "ocr" or not retry or attempt == 2:
                    return previous
                break
            if (directory / f"image-a{attempt}-started.json").exists():
                if not retry or attempt == 2:
                    return {"binding": binding, "attempt": attempt,
                            "status": "interrupted", "coverage": "partial", "text": ""}
                break
        else:
            attempt = 0
        next_attempt = attempt + 1
        if next_attempt > 2:
            raise ValueError("Image attempt allowance exhausted")
        with _directory(directory) as fd:
            _publish_new(fd, f"image-a{next_attempt}-started.json",
                         json.dumps({**binding, "attempt": next_attempt}, sort_keys=True).encode())
        if cancelled():
            result = {"status": "cancelled", "text": "", "calls": 0, "call_s": 0.0}
        else:
            result = engine.recognize(raw, cancelled, lambda: None)
        if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise ValueError("Image changed during extraction; attempt reservation retained")
        record = {**result, "binding": binding, "attempt": next_attempt,
                  "coverage": "processed_unverified" if result["status"] == "ocr" else "partial"}
        raw_record = json.dumps(record, sort_keys=True, ensure_ascii=False).encode()
        with _directory(directory) as fd:
            _publish_new(fd, f"image-a{next_attempt}.json",
                         json.dumps({"record": record,
                                     "sha256": hashlib.sha256(raw_record).hexdigest()},
                                    sort_keys=True, ensure_ascii=False).encode())
        return record


def export_image(record: dict, source: Path, input_root: Path, destination: Path) -> Path:
    """No-clobber readable companion and exact text bytes outside the vault."""
    from core.cache_exports import _directory, _publish_new
    source = source.absolute()
    inputs, target = input_root.resolve(), destination.resolve()
    if (not source.resolve().is_relative_to(inputs) or target.is_relative_to(inputs)
            or inputs.is_relative_to(target)
            or hashlib.sha256(source.read_bytes()).hexdigest() !=
            record["binding"]["source_sha256"]):
        raise ValueError("Image export source changed or destination is unsafe")
    identity = hashlib.sha256(json.dumps(record, sort_keys=True,
                                         ensure_ascii=False).encode()).hexdigest()
    text = record.get("text", "")
    markdown = (f"# {source.name}\n\n[Source image](<{source.as_uri()}>)\n\n"
                f"Status: {record['status']} · transcription completeness unverified.\n\n"
                f"Source SHA-256: `{record['binding']['source_sha256']}`\n\n{text}\n")
    if record.get("error_detail"):
        markdown += (f"\nProvider error (HTTP {record.get('http_status', 'unavailable')}): "
                     f"{record['error_detail']}\n\nNext: {record.get('next_step', 'Review before retrying.')}\n")
    with _directory(destination.absolute(), create=True) as fd:
        _publish_new(fd, identity + ".txt", text.encode())
        _publish_new(fd, identity + ".md", markdown.encode())
    return destination / (identity + ".md")


class NativeOnlyOCR:
    """Fail-closed engine for native-only preparation; never calls a provider."""
    provider = "native"
    model = "native-text"

    def identity(self):
        return {"engine": "native_only", "provider": self.provider, "model": self.model}

    def recognize(self, png, cancelled, heartbeat):
        return {"status": "deferred", "text": "", "calls": 0, "call_s": 0.0,
                "warning": "Online OCR was not authorized"}
