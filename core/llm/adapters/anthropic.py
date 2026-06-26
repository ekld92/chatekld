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
    """Online chat adapter for the Anthropic Messages API.

    Deliberately speaks raw ``httpx`` rather than the ``anthropic`` SDK so the
    adapter carries no hard dependency on that package (``httpx`` is already a
    requirement). Wire format specifics it owns vs. the other adapters:

    * Auth/version via the ``x-api-key`` + ``anthropic-version: 2023-06-01``
      headers (not a Bearer token), POSTing to ``/v1/messages``.
    * ``max_tokens`` is REQUIRED by the API (defaulted to 4096 when the caller
      leaves it unset) and the system prompt is a top-level ``system`` field,
      not a ``messages`` entry.
    * **Temperature is clamped to ≤ 1.0** — Anthropic rejects the (1.0, 2.0]
      range the app's vault-chat knob permits (see :meth:`_build_payload`).
    * Tools use ``input_schema`` (not ``parameters``) and ``tool_choice`` is an
      object union with no ``"none"`` variant — "none" is honoured by omitting
      the tools payload entirely.
    * The streaming response is a typed SSE event stream (``message_start`` /
      ``content_block_delta`` / ``message_delta``), parsed line-by-line in
      :meth:`stream`, with usage split across ``message_start`` (input) and
      ``message_delta`` (output).
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_s: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        """Stash connection settings; the ``httpx`` client is built per call.

        ``api_key`` / ``base_url`` normally resolve from the environment at call
        time. ``timeout_s`` / ``max_retries`` come from ``online_timeout_s`` /
        ``online_max_retries`` via :func:`core.llm.factory.get_llm_provider`.
        """
        self._explicit_key = api_key
        self._explicit_base_url = base_url
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    def _api_key(self) -> str:
        """Resolve ``ANTHROPIC_API_KEY`` at call time, or raise AUTH.

        Read from the environment on every call (never persisted to config) so
        a key set after launch applies without a restart. Raising here before
        any network call is what makes the live model listing degrade to
        curated-only with no I/O when no key is set.
        """
        key = self._explicit_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError(
                category=ErrorCategory.AUTH,
                message="ANTHROPIC_API_KEY is not set.",
                provider=self.name,
            )
        return key

    def _base_url(self) -> str:
        """API base (``ANTHROPIC_BASE_URL`` or the public endpoint), trailing
        slash stripped so URL joins are clean."""
        return (
            self._explicit_base_url
            or os.environ.get("ANTHROPIC_BASE_URL")
            or _DEFAULT_BASE_URL
        ).rstrip("/")

    def _httpx(self):
        """Import ``httpx`` lazily, raising INVALID_REQUEST if it is missing."""
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
        """Online providers are chat-only; embeddings resolve to a local
        provider."""
        return False

    def supports_tool_use(self) -> bool:
        """Anthropic Messages supports ``tools`` / ``tool_use`` blocks."""
        return True

    def list_models(self) -> tuple[list[str], str]:
        """Curated models plus any the live ``/v1/models`` endpoint reports
        (cached, key-gated). Falls back to curated-only without a key, offline,
        or on any error — see ``core.llm.model_listing``."""
        from core.llm.model_listing import merged_models
        models = merged_models(
            self.name, CURATED_MODELS, self._fetch_live_models,
            cache_key=f"anthropic:{self._base_url()}",
        )
        return models, ""

    def _fetch_live_models(self) -> list[str]:
        """Model ids from Anthropic's ``/v1/models`` (all are chat models).

        ``_headers()`` calls ``_api_key()`` — raising AUTH and making NO network
        call when no key is set. Errors propagate to ``merged_models`` (→ curated
        only). Uses a short listing timeout independent of the generation cap."""
        httpx = self._httpx()
        with httpx.Client(timeout=min(self.timeout_s, 10.0)) as client:
            # No `after_id` pagination: limit=1000 (the endpoint max) dwarfs the
            # ~dozen live Claude models, so a single page is always complete.
            resp = client.get(
                f"{self._base_url()}/v1/models",
                headers=self._headers(),
                params={"limit": 1000},
            )
        if resp.status_code >= 400:
            return []
        out: list[str] = []
        for item in (resp.json().get("data") or []):
            mid = item.get("id") if isinstance(item, dict) else None
            if isinstance(mid, str) and mid:
                out.append(mid)
        return out

    def health_check(self) -> tuple[bool, str]:
        """``ok`` iff a key is present — key-presence only, no network call."""
        try:
            self._api_key()
            return True, ""
        except LLMError as err:
            return False, err.message

    def _headers(self) -> dict[str, str]:
        """Required Anthropic request headers (auth + pinned API version)."""
        return {
            "x-api-key": self._api_key(),
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

    def _build_payload(self, request: LLMRequest, *, stream: bool) -> dict:
        """Assemble the ``/v1/messages`` JSON body shared by both code paths.

        Encodes the Anthropic-specific contract: ``max_tokens`` is mandatory
        (defaulted), the system prompt is a top-level field, ``temperature`` is
        clamped to ≤ 1.0, and tools use ``input_schema`` with an object-shaped
        ``tool_choice`` (``"none"`` is realised by omitting the tools entirely,
        since the union has no none variant).
        """
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
            # Anthropic caps temperature at 1.0 (verified vs platform.claude.com,
            # 2026-06), whereas OpenAI/Gemini allow up to 2.0 and the app's
            # vault-chat range is 0-2. Clamp so a temperature in (1.0, 2.0] does
            # not 400 against Anthropic specifically.
            payload["temperature"] = min(float(request.temperature), 1.0)
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
        """Non-streaming completion via a single POST to ``/v1/messages``.

        Concatenates the ``text`` content blocks, parses any ``tool_use``
        blocks into :class:`ToolCall`s, maps ``stop_reason`` to the normalised
        finish reason, and records usage (including ``cache_read_input_tokens``).
        Transport/HTTP errors are normalised to :class:`LLMError` and the whole
        call is wrapped in :func:`retry_with_backoff`.
        """
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
        """Streaming completion over Anthropic's typed SSE event stream.

        Parses the ``event:``/``data:`` line protocol by hand: ``message_start``
        carries input + cache-read usage, ``content_block_delta`` of subtype
        ``text_delta`` yields each token, and ``message_delta`` carries the
        running output-token count plus the terminal ``stop_reason``. Like the
        other streaming adapters this is NOT retried and the fallback layer only
        switches providers before the first token — a mid-stream failure is
        captured as ``final.error``, re-raised for a structured SSE error, and
        usage is still recorded in the ``finally``.
        """
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
    """Map a ≥400 Anthropic HTTP response onto a normalised :class:`LLMError`.

    Prefers the provider's ``error.message`` from the JSON body, categorises by
    status code, and sets ``retryable`` for the transient categories. Anthropic
    reports billing exhaustion as a 400 ("Your credit balance is too low"), so a
    RATE_LIMIT *or* INVALID_REQUEST whose message :func:`looks_like_quota`
    recognises is re-mapped to the terminal QUOTA category. ``body_text`` lets
    the streaming caller pass the already-read body (the stream response can't be
    re-read).
    """
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
