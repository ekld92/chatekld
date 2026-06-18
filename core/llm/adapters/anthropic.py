"""Anthropic online adapter.

Uses ``httpx`` (already in requirements) for both streaming and
non-streaming requests so the adapter has no hard dependency on the
``anthropic`` SDK package. When the SDK is present the adapter still
honours the same env vars (``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL``).
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
    build_anthropic_messages,
    jsonschema_to_anthropic_tool,
    parse_anthropic_tool_use,
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

# Active models only (per platform.claude.com, 2026-05).  The Claude 3.x
# entries were removed because those models are retired and now 404:
# 3.5 Sonnet (2025-10), 3 Opus (2026-01), 3.5 Haiku (2026-02).
CURATED_MODELS: list[str] = [
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
]

_API_VERSION = "2023-06-01"
_DEFAULT_BASE_URL = "https://api.anthropic.com"

_STOP_REASON_MAP: dict[str, FinishReason] = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "tool_use": FinishReason.TOOL_USE,
}


class AnthropicProvider(LLMProvider):
    name = "anthropic"

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
        key = self._explicit_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError(
                category=ErrorCategory.AUTH,
                message="ANTHROPIC_API_KEY is not set.",
                provider=self.name,
            )
        return key

    def _base_url(self) -> str:
        return (
            self._explicit_base_url
            or os.environ.get("ANTHROPIC_BASE_URL")
            or _DEFAULT_BASE_URL
        ).rstrip("/")

    def _httpx(self):
        try:
            import httpx
        except ImportError as exc:
            raise LLMError(
                category=ErrorCategory.INVALID_REQUEST,
                message="httpx is required for the Anthropic adapter",
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

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key(),
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

    def _build_payload(self, request: LLMRequest, *, stream: bool) -> dict:
        max_tokens = request.max_tokens if request.max_tokens is not None else 4096
        payload: dict = {
            "model": request.model,
            "max_tokens": max_tokens,
            "messages": build_anthropic_messages(request),
            "stream": stream,
        }
        if request.system_prompt:
            payload["system"] = request.system_prompt
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop:
            payload["stop_sequences"] = request.stop
        if request.tools and request.tool_choice != "none":
            # Anthropic's tool_choice union has no ``none`` variant — to
            # honour the cross-provider semantic that ``"none"`` disables
            # tool use, omit the tools payload entirely so the model
            # cannot emit tool_use blocks even by default.
            payload["tools"] = [jsonschema_to_anthropic_tool(t) for t in request.tools]
            choice = request.tool_choice
            if choice in (None, "auto"):
                payload["tool_choice"] = {"type": "auto"}
            elif choice in ("required", "any"):
                payload["tool_choice"] = {"type": "any"}
            else:
                payload["tool_choice"] = {"type": "tool", "name": choice}
        return payload

    def generate(self, request: LLMRequest) -> LLMResponse:
        httpx = self._httpx()
        start = time.monotonic()

        def _do_call() -> LLMResponse:
            try:
                with httpx.Client(timeout=self.timeout_s) as client:
                    resp = client.post(
                        f"{self._base_url()}/v1/messages",
                        headers=self._headers(),
                        json=self._build_payload(request, stream=False),
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
            blocks = body.get("content", []) or []
            text = "".join(
                part.get("text", "")
                for part in blocks
                if isinstance(part, dict) and part.get("type") == "text"
            )
            tool_calls = []
            for part in blocks:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    tc = parse_anthropic_tool_use(part)
                    if tc is not None:
                        tool_calls.append(tc)
            usage_block = body.get("usage", {}) or {}
            usage = LLMUsage(
                input_tokens=int(usage_block.get("input_tokens", 0) or 0),
                output_tokens=int(usage_block.get("output_tokens", 0) or 0),
                cached_input_tokens=int(usage_block.get("cache_read_input_tokens", 0) or 0),
            )
            finish = _STOP_REASON_MAP.get(body.get("stop_reason") or "", FinishReason.STOP)
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
                        f"{self._base_url()}/v1/messages",
                        headers=self._headers(),
                        json=self._build_payload(request, stream=True),
                    ) as resp:
                        if resp.status_code >= 400:
                            try:
                                body_text = resp.read().decode("utf-8", errors="replace")
                            except Exception:
                                body_text = ""
                            raise _http_error_to_llm_error(
                                resp, self.name, request.model, body_text=body_text,
                            )
                        event_name = ""
                        for line in resp.iter_lines():
                            if not line:
                                continue
                            if line.startswith("event:"):
                                event_name = line.split(":", 1)[1].strip()
                                continue
                            if not line.startswith("data:"):
                                continue
                            raw = line.split(":", 1)[1].strip()
                            if not raw or raw == "[DONE]":
                                continue
                            try:
                                data = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            ev_type = data.get("type") or event_name
                            if ev_type == "content_block_delta":
                                delta = data.get("delta") or {}
                                if delta.get("type") == "text_delta":
                                    text = delta.get("text") or ""
                                    if text:
                                        buf.append(text)
                                        yield text
                            elif ev_type == "message_start":
                                msg_usage = (data.get("message") or {}).get("usage") or {}
                                usage_in = int(msg_usage.get("input_tokens", 0) or 0)
                                cached_in = int(msg_usage.get("cache_read_input_tokens", 0) or 0)
                            elif ev_type == "message_delta":
                                delta_usage = data.get("usage") or {}
                                if delta_usage:
                                    usage_out = int(delta_usage.get("output_tokens", 0) or usage_out)
                                stop_reason = (data.get("delta") or {}).get("stop_reason")
                                if stop_reason:
                                    finish_reason = _STOP_REASON_MAP.get(stop_reason, FinishReason.STOP)
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
    # A credit-balance/quota exhaustion (Anthropic returns this as a 400
    # "Your credit balance is too low") is terminal — map it to QUOTA so
    # it surfaces immediately instead of being retried as a bad request.
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
