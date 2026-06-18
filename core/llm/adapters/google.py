"""Google Gemini online adapter.

Uses the v1beta REST API via ``httpx``. The Gemini SDK is optional —
when present we still read the same env vars (``GOOGLE_API_KEY`` or the
legacy ``GEMINI_API_KEY``) so users can swap between SDK and REST
without rewriting their config.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterator, Optional

from core.llm.base import LLMProvider, StreamingResponse, looks_like_quota
from core.llm.retry import retry_with_backoff
from core.llm.tool_schema import (
    build_gemini_contents,
    jsonschema_to_gemini_tool,
    parse_gemini_function_call,
)
from core.llm.types import (
    ErrorCategory,
    FinishReason,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)
from core.llm.usage import usage_tracker

logger = logging.getLogger(__name__)

CURATED_MODELS: list[str] = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-exp",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]

_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

_FINISH_REASONS: dict[str, FinishReason] = {
    "STOP": FinishReason.STOP,
    "MAX_TOKENS": FinishReason.LENGTH,
    "SAFETY": FinishReason.CONTENT_FILTER,
    "RECITATION": FinishReason.CONTENT_FILTER,
    "OTHER": FinishReason.OTHER,
}


class GoogleProvider(LLMProvider):
    name = "google"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_s: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        self._explicit_key = api_key
        self._explicit_base_url = base_url
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    def _api_key(self) -> str:
        key = (
            self._explicit_key
            or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
        )
        if not key:
            raise LLMError(
                category=ErrorCategory.AUTH,
                message="GOOGLE_API_KEY is not set.",
                provider=self.name,
            )
        return key

    def _base_url(self) -> str:
        return (
            self._explicit_base_url
            or os.environ.get("GOOGLE_BASE_URL")
            or _DEFAULT_BASE_URL
        ).rstrip("/")

    def _httpx(self):
        try:
            import httpx
        except ImportError as exc:
            raise LLMError(
                category=ErrorCategory.INVALID_REQUEST,
                message="httpx is required for the Google adapter",
                provider=self.name,
            ) from exc
        return httpx

    def supports_embeddings(self) -> bool:
        return False

    def supports_tool_use(self) -> bool:
        return True

    def list_models(self) -> tuple[list[str], str]:
        return list(CURATED_MODELS), ""

    def health_check(self) -> tuple[bool, str]:
        try:
            self._api_key()
            return True, ""
        except LLMError as err:
            return False, err.message

    def _build_payload(self, request: LLMRequest) -> dict:
        payload: dict = {"contents": build_gemini_contents(request)}

        generation_config: dict = {}
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.top_p is not None:
            generation_config["topP"] = request.top_p
        if request.max_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_tokens
        if request.stop:
            generation_config["stopSequences"] = request.stop
        if generation_config:
            payload["generationConfig"] = generation_config

        if request.system_prompt:
            payload["systemInstruction"] = {
                "role": "system",
                "parts": [{"text": request.system_prompt}],
            }

        if request.tools:
            payload["tools"] = [{
                "function_declarations": [
                    jsonschema_to_gemini_tool(t) for t in request.tools
                ]
            }]
            choice = request.tool_choice
            mode = "AUTO"
            if choice in ("required", "any"):
                mode = "ANY"
            elif choice == "none":
                mode = "NONE"
            payload["toolConfig"] = {"functionCallingConfig": {"mode": mode}}
        return payload

    def _build_url(self, model: str, *, stream: bool) -> str:
        method = "streamGenerateContent" if stream else "generateContent"
        base = self._base_url()
        return f"{base}/v1beta/models/{model}:{method}"

    def _params(self, *, stream: bool) -> dict[str, str]:
        params: dict[str, str] = {"key": self._api_key()}
        if stream:
            params["alt"] = "sse"
        return params

    def generate(self, request: LLMRequest) -> LLMResponse:
        httpx = self._httpx()
        start = time.monotonic()

        def _do_call() -> LLMResponse:
            try:
                with httpx.Client(timeout=self.timeout_s) as client:
                    resp = client.post(
                        self._build_url(request.model, stream=False),
                        params=self._params(stream=False),
                        json=self._build_payload(request),
                    )
            except httpx.TimeoutException as exc:
                raise LLMError(
                    category=ErrorCategory.TIMEOUT, message=str(exc),
                    provider=self.name, model=request.model, retryable=True,
                )
            except httpx.HTTPError as exc:
                raise LLMError(
                    category=ErrorCategory.NETWORK, message=str(exc),
                    provider=self.name, model=request.model, retryable=True,
                )
            if resp.status_code >= 400:
                raise _http_error_to_llm_error(resp, self.name, request.model)

            try:
                body = resp.json()
            except ValueError as exc:
                raise LLMError(
                    category=ErrorCategory.SERVER_ERROR,
                    message=f"malformed response body from {self.name}: {exc}",
                    provider=self.name, model=request.model, retryable=True,
                )
            text, finish, tool_calls = _extract_text_finish_and_calls(body)
            if tool_calls and finish == FinishReason.STOP:
                finish = FinishReason.TOOL_USE
            usage = _usage_from_body(body)
            latency_ms = int((time.monotonic() - start) * 1000)
            response = LLMResponse(
                text=text,
                provider=self.name,
                model=request.model,
                finish_reason=finish,
                usage=usage,
                latency_ms=latency_ms,
                tool_calls=tool_calls,
            )
            usage_tracker.record(
                provider=self.name,
                model=request.model,
                usage=usage,
                latency_ms=latency_ms,
                stream=False,
                success=True,
            )
            return response

        return retry_with_backoff(_do_call, max_attempts=self.max_retries)

    def stream(self, request: LLMRequest) -> StreamingResponse:
        httpx = self._httpx()
        start = time.monotonic()
        final = LLMResponse(provider=self.name, model=request.model)
        buf: list[str] = []

        def _iter() -> Iterator[str]:
            usage_in = 0
            usage_out = 0
            cached_in = 0
            finish_reason = FinishReason.STOP
            stream_error: LLMError | None = None
            try:
                with httpx.Client(timeout=self.timeout_s) as client:
                    with client.stream(
                        "POST",
                        self._build_url(request.model, stream=True),
                        params=self._params(stream=True),
                        json=self._build_payload(request),
                    ) as resp:
                        if resp.status_code >= 400:
                            try:
                                body_text = resp.read().decode("utf-8", errors="replace")
                            except Exception:
                                body_text = ""
                            raise _http_error_to_llm_error(
                                resp, self.name, request.model, body_text=body_text,
                            )
                        for line in resp.iter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            raw = line.split(":", 1)[1].strip()
                            if not raw or raw == "[DONE]":
                                continue
                            try:
                                data = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            text, partial_finish = _extract_text_and_finish(data)
                            if text:
                                buf.append(text)
                                yield text
                            if partial_finish != FinishReason.STOP:
                                finish_reason = partial_finish
                            usage_meta = data.get("usageMetadata") or {}
                            if usage_meta:
                                usage_in = int(usage_meta.get("promptTokenCount", usage_in) or usage_in)
                                usage_out = int(usage_meta.get("candidatesTokenCount", usage_out) or usage_out)
                                cached_in = int(usage_meta.get("cachedContentTokenCount", cached_in) or cached_in)
            except LLMError as exc:
                stream_error = exc
                raise
            except httpx.TimeoutException as exc:
                stream_error = LLMError(
                    category=ErrorCategory.TIMEOUT, message=str(exc),
                    provider=self.name, model=request.model, retryable=True,
                )
                raise stream_error
            except httpx.HTTPError as exc:
                stream_error = LLMError(
                    category=ErrorCategory.NETWORK, message=str(exc),
                    provider=self.name, model=request.model, retryable=True,
                )
                raise stream_error
            finally:
                final.text = "".join(buf)
                final.finish_reason = FinishReason.ERROR if stream_error else finish_reason
                final.latency_ms = int((time.monotonic() - start) * 1000)
                final.usage = LLMUsage(
                    input_tokens=usage_in,
                    output_tokens=usage_out,
                    cached_input_tokens=cached_in,
                )
                final.error = stream_error
                usage_tracker.record(
                    provider=self.name,
                    model=request.model,
                    usage=final.usage,
                    latency_ms=final.latency_ms,
                    stream=True,
                    success=stream_error is None,
                    error_category=stream_error.category.value if stream_error else "",
                )

        return StreamingResponse(response_gen=_iter(), final=final)


def _extract_text_finish_and_calls(body: dict) -> tuple[str, FinishReason, list]:
    text_parts: list[str] = []
    tool_calls: list = []
    finish = FinishReason.STOP
    for candidate in body.get("candidates", []) or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content") or {}
        for part in content.get("parts", []) or []:
            if not isinstance(part, dict):
                continue
            if "text" in part:
                text_parts.append(part.get("text") or "")
            if "functionCall" in part:
                tc = parse_gemini_function_call(part.get("functionCall") or {})
                if tc is not None:
                    tool_calls.append(tc)
        raw_finish = candidate.get("finishReason")
        if raw_finish:
            finish = _FINISH_REASONS.get(raw_finish, FinishReason.OTHER)
    return "".join(text_parts), finish, tool_calls


def _extract_text_and_finish(body: dict) -> tuple[str, FinishReason]:
    """Back-compat wrapper for the streaming path (tools are non-streaming)."""
    text, finish, _ = _extract_text_finish_and_calls(body)
    return text, finish


def _usage_from_body(body: dict) -> LLMUsage:
    meta = body.get("usageMetadata") or {}
    return LLMUsage(
        input_tokens=int(meta.get("promptTokenCount", 0) or 0),
        output_tokens=int(meta.get("candidatesTokenCount", 0) or 0),
        cached_input_tokens=int(meta.get("cachedContentTokenCount", 0) or 0),
    )


def _http_error_to_llm_error(
    resp,
    provider: str,
    model: str,
    *,
    body_text: Optional[str] = None,
) -> LLMError:
    status = resp.status_code
    text = body_text
    if text is None:
        try:
            text = resp.text
        except Exception:
            text = ""
    message = text or f"HTTP {status}"
    try:
        body = json.loads(text)
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            message = body["error"].get("message", message)
    except Exception:
        pass
    category = ErrorCategory.UNKNOWN
    if status in (401, 403):
        category = ErrorCategory.AUTH
    elif status == 404:
        category = ErrorCategory.NOT_FOUND
    elif status == 408:
        category = ErrorCategory.TIMEOUT
    elif status == 429:
        category = ErrorCategory.RATE_LIMIT
    elif status == 400:
        category = ErrorCategory.INVALID_REQUEST
    elif 500 <= status < 600:
        category = ErrorCategory.SERVER_ERROR
    # Terminal billing/quota exhaustion surfaces immediately.  The signals
    # in ``looks_like_quota`` are specific enough that a Gemini per-minute
    # "Quota exceeded for quota metric …" 429 stays RATE_LIMIT (retryable).
    if category in (ErrorCategory.RATE_LIMIT, ErrorCategory.INVALID_REQUEST) and looks_like_quota(message):
        category = ErrorCategory.QUOTA
    return LLMError(
        category=category,
        message=message,
        provider=provider,
        model=model,
        status_code=status,
        retryable=category in {
            ErrorCategory.TIMEOUT,
            ErrorCategory.RATE_LIMIT,
            ErrorCategory.SERVER_ERROR,
        },
    )
