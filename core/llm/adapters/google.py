"""Google Gemini online adapter.

Uses the v1beta REST API via ``httpx``. The Gemini SDK is optional —
when present we still read the same env vars (``GOOGLE_API_KEY`` or the
legacy ``GEMINI_API_KEY``) so users can swap between SDK and REST
without rewriting their config.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from collections import OrderedDict
import time
from typing import Iterator, Optional

from core.llm.base import LLMProvider, StreamingResponse, looks_like_quota
from core.llm.retry import check_request_dispatch, parse_retry_after_s, retry_with_backoff
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
from core.llm.usage import CANCELLED_ERROR_CATEGORY, usage_tracker
from core.llm.parameter_cache import is_rejected, remember_rejected

logger = logging.getLogger(__name__)

# Cached shared ``httpx.Client`` per (base_url, timeout) — same rationale as the
# Anthropic adapter: reuse one keep-alive connection pool across generate()/stream()
# instead of a fresh pool + TLS handshake per online round-trip (N per agent turn /
# deck section). Auth is a per-request query param (``_params()``), so the client
# is key-agnostic and a rotated key applies on the next request; the key is never
# stored. The listing call keeps its own short-lived client (different timeout).
# PERF (2026-09-02): bounded LRU, mirroring core/providers/lms.py.
# Before: a plain dict with no eviction. The keyspace is small in normal use,
#   but a rotated API key mints a new entry whose predecessor is never
#   dropped, stranding that client's idle httpx/openai connection pool for
#   the process lifetime.
# Change: an OrderedDict capped at _CLIENT_CACHE_MAX (8), LRU-evicted.
# Safe because: an evicted client is not closed here — a concurrent call may
#   still be streaming on it; dropping the reference lets GC close it once the
#   in-flight call ends (the lms.py rule, quoted). A cache miss constructs
#   exactly the client the miss path constructed before.
# Preserves: key-rotation safety — the key composition is untouched, so a new
#   key still yields a new client rather than reusing stale auth.
# Lock/thread contract: unchanged; every read/write stays inside
#   _client_cache_lock.
# Pinned by: test_llm.py::TestOnlineClientCacheBound.
_CLIENT_CACHE_MAX = 8
_client_cache: OrderedDict = OrderedDict()
_client_cache_lock = threading.Lock()

CURATED_MODELS: list[str] = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

_FINISH_REASONS: dict[str, FinishReason] = {
    "STOP": FinishReason.STOP,
    "MAX_TOKENS": FinishReason.LENGTH,
    "SAFETY": FinishReason.CONTENT_FILTER,
    "RECITATION": FinishReason.CONTENT_FILTER,
    "PROHIBITED_CONTENT": FinishReason.CONTENT_FILTER,
    "BLOCKLIST": FinishReason.CONTENT_FILTER,
    "SPII": FinishReason.CONTENT_FILTER,
    "OTHER": FinishReason.OTHER,
}


class GoogleProvider(LLMProvider):
    """Online chat adapter for the Google Gemini ``v1beta`` REST API.

    Speaks raw ``httpx`` rather than the optional Gemini SDK so the adapter has
    no hard dependency on that package. Wire-format specifics it owns:

    * Auth is a ``?key=`` query parameter (not a header); the model + method are
      in the path (``/v1beta/models/<model>:generateContent`` vs
      ``:streamGenerateContent``, the latter with ``?alt=sse``).
    * Messages are ``contents`` parts and the system prompt is a top-level
      ``systemInstruction``; sampling/limit knobs live under
      ``generationConfig`` with Gemini's own names (``topP``,
      ``maxOutputTokens``, ``stopSequences``).
    * Tools are ``function_declarations`` and tool-calling mode is set via
      ``toolConfig.functionCallingConfig.mode`` (AUTO/ANY/NONE). The adapter
      echoes a provider-assigned function-call id when present; older calls
      without one retain a local synthetic id for agent bookkeeping.
    * Usage comes from ``usageMetadata`` (``promptTokenCount`` /
      ``candidatesTokenCount`` / ``cachedContentTokenCount``).
    """

    name = "google"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_s: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        """Stash connection settings; the ``httpx`` client is built per call.

        ``timeout_s`` / ``max_retries`` come from ``online_timeout_s`` /
        ``online_max_retries`` via :func:`core.llm.factory.get_llm_provider`.
        """
        self._explicit_key = api_key
        self._explicit_base_url = base_url
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    def _api_key(self) -> str:
        """Resolve the key at call time (``GOOGLE_API_KEY``, then legacy
        ``GEMINI_API_KEY``), or raise AUTH.

        Read from the environment on every call (never persisted) so a key set
        post-launch applies without restart, and so the live model listing makes
        no network call when no key is set.
        """
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
        """API base (``GOOGLE_BASE_URL`` or the public endpoint), trailing slash
        stripped."""
        return (
            self._explicit_base_url
            or os.environ.get("GOOGLE_BASE_URL")
            or _DEFAULT_BASE_URL
        ).rstrip("/")

    def _httpx(self):
        """Import ``httpx`` lazily, raising INVALID_REQUEST if it is missing."""
        try:
            import httpx
        except ImportError as exc:
            raise LLMError(
                category=ErrorCategory.INVALID_REQUEST,
                message="httpx is required for the Google adapter",
                provider=self.name,
            ) from exc
        return httpx

    def _client(self):
        """Return a cached, long-lived ``httpx.Client`` for (base_url, timeout).

        Reused across generate()/stream() for connection keep-alive (see the
        module cache note). Never closed (process-lifetime); auth is per-request
        (``_params()``), so it is key-agnostic and rotation-safe.
        """
        httpx = self._httpx()
        key = (self._base_url(), self.timeout_s)
        with _client_cache_lock:
            client = _client_cache.get(key)
            if client is None:
                client = httpx.Client(timeout=self.timeout_s)
                _client_cache[key] = client
                while len(_client_cache) > _CLIENT_CACHE_MAX:
                    _client_cache.popitem(last=False)  # evict LRU; GC closes it
            else:
                _client_cache.move_to_end(key)
            return client

    def supports_embeddings(self) -> bool:
        """Online providers are chat-only; embeddings resolve to a local
        provider."""
        return False

    def supports_tool_use(self) -> bool:
        """Gemini supports ``function_declarations`` / ``functionCall``."""
        return True

    def list_models(self) -> tuple[list[str], str]:
        """Curated models plus any the live ``/v1beta/models`` endpoint reports
        that support ``generateContent`` (cached, key-gated). Falls back to
        curated-only without a key, offline, or on any error — see
        ``core.llm.model_listing``."""
        from core.llm.model_listing import merged_models
        models = merged_models(
            self.name, CURATED_MODELS, self._fetch_live_models,
            cache_key=f"google:{self._base_url()}",
        )
        return models, ""

    def _fetch_live_models(self) -> list[str]:
        """Chat-capable Gemini model ids from ``/v1beta/models``.

        Filters to models advertising ``generateContent`` (drops embedding /
        imagen / aqa entries) and strips the ``models/`` name prefix.
        ``_api_key()`` raises AUTH (→ no network) when no key is set; errors
        propagate to ``merged_models`` (→ curated only)."""
        httpx = self._httpx()
        with httpx.Client(timeout=min(self.timeout_s, 10.0)) as client:
            # No `pageToken` pagination: pageSize=1000 (the endpoint max) dwarfs
            # the few-dozen Gemini models, so a single page is always complete.
            resp = client.get(
                f"{self._base_url()}/v1beta/models",
                params={"key": self._api_key(), "pageSize": 1000},
            )
        if resp.status_code >= 400:
            return []
        out: list[str] = []
        for item in (resp.json().get("models") or []):
            if not isinstance(item, dict):
                continue
            methods = (
                item.get("supportedGenerationMethods")
                or item.get("supported_generation_methods")
                or []
            )
            if "generateContent" not in methods:
                continue
            name = item.get("name") or ""  # "models/gemini-2.5-pro"
            mid = name.split("/", 1)[1] if name.startswith("models/") else name
            if mid:
                out.append(mid)
        return out

    def health_check(self) -> tuple[bool, str]:
        """``ok`` iff a key is present — key-presence only, no network call."""
        try:
            self._api_key()
            return True, ""
        except LLMError as err:
            return False, err.message

    def _build_payload(self, request: LLMRequest) -> dict:
        """Assemble the Gemini request body (shared by stream + non-stream).

        Maps the request onto ``contents`` + ``generationConfig`` +
        ``systemInstruction`` + ``tools``/``toolConfig``, using Gemini's field
        names (``topP``, ``maxOutputTokens``, ``stopSequences``) and translating
        ``tool_choice`` into the ``functionCallingConfig.mode`` enum
        (AUTO/ANY/NONE).
        """
        payload: dict = {"contents": build_gemini_contents(request)}

        generation_config: dict = {}
        model = request.model.lower()
        gemini3 = model.startswith("gemini-3")
        # Gemini 3.x is tuned for its default sampling values.  The 2.5
        # contract still honours the caller's sampling settings.
        if request.temperature is not None and not gemini3:
            generation_config["temperature"] = request.temperature
        if request.top_p is not None and not gemini3:
            generation_config["topP"] = request.top_p
        if request.max_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_tokens
        if request.reasoning_effort:
            if model.startswith("gemini-2.5-pro"):
                generation_config["thinkingConfig"] = {
                    "thinkingBudget": 128 if request.reasoning_effort == "minimal" else 1024,
                }
            elif model.startswith("gemini-2.5-flash"):
                generation_config["thinkingConfig"] = {
                    "thinkingBudget": 0 if request.reasoning_effort == "minimal" else 1024,
                }
            elif model.startswith("gemini-3"):
                level = request.reasoning_effort
                # These families reject `minimal`; keep the low-effort intent
                # while using a supported level. 3.6 Flash accepts minimal.
                if level == "minimal" and model.startswith((
                    "gemini-3.7", "gemini-3.8", "gemini-3.1-pro",
                    "gemini-3-pro",
                )):
                    level = "low"
                generation_config["thinkingConfig"] = {"thinkingLevel": level}
        if request.stop:
            generation_config["stopSequences"] = request.stop
        if generation_config:
            for param in ("temperature", "topP", "thinkingConfig"):
                if is_rejected(self.name, self._base_url(), request.model, param):
                    generation_config.pop(param, None)
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
        """Build the per-method endpoint URL (the model + method live in the
        path: ``:generateContent`` vs ``:streamGenerateContent``)."""
        method = "streamGenerateContent" if stream else "generateContent"
        base = self._base_url()
        return f"{base}/v1beta/models/{model}:{method}"

    def _params(self, *, stream: bool) -> dict[str, str]:
        """Query params: the ``key`` (Gemini auths via query string) plus
        ``alt=sse`` to request the SSE framing for streaming."""
        params: dict[str, str] = {"key": self._api_key()}
        if stream:
            params["alt"] = "sse"
        return params

    @staticmethod
    def _heal_generation_config(payload: dict, message: str, healed: set) -> bool:
        config = payload.get("generationConfig") or {}
        lower = (message or "").lower()
        for key in ("temperature", "topP", "thinkingConfig"):
            if key in config and key not in healed and key.lower() in lower:
                healed.add(key)
                config.pop(key)
                return True
        return False

    @contextlib.contextmanager
    def _stream_with_heal(self, client, request: LLMRequest, payload: dict, started: float | None = None):
        healed: set = set()
        started = time.monotonic() if started is None else started
        while True:
            check_request_dispatch(request, started)
            with client.stream(
                "POST", self._build_url(request.model, stream=True),
                params=self._params(stream=True), json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    try:
                        body_text = resp.read().decode("utf-8", errors="replace")
                    except Exception:
                        body_text = ""
                    err = _http_error_to_llm_error(
                        resp, self.name, request.model, body_text=body_text,
                    )
                    before = set(healed)
                    if (err.category == ErrorCategory.INVALID_REQUEST
                            and self._heal_generation_config(payload, err.message, healed)):
                        for param in healed - before:
                            remember_rejected(self.name, self._base_url(), request.model, param)
                        continue
                    raise err
                yield resp
                return

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Non-streaming completion via a single POST to ``:generateContent``.

        Extracts text + tool calls + finish reason from the ``candidates``
        (promoting STOP→TOOL_USE when calls are present but the model still
        reported STOP), reads usage from ``usageMetadata``, and wraps the whole
        call in :func:`retry_with_backoff`.
        """
        httpx = self._httpx()
        start = time.monotonic()
        payload = self._build_payload(request)
        healed: set = set()

        def _do_call() -> LLMResponse:
            check_request_dispatch(request, start)
            try:
                # Cached shared client (keep-alive) instead of a per-call pool.
                client = self._client()
                resp = client.post(
                    self._build_url(request.model, stream=False),
                    params=self._params(stream=False),
                    json=payload,
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
                err = _http_error_to_llm_error(resp, self.name, request.model)
                before = set(healed)
                if (err.category == ErrorCategory.INVALID_REQUEST
                        and self._heal_generation_config(payload, err.message, healed)):
                    for param in healed - before:
                        remember_rejected(self.name, self._base_url(), request.model, param)
                    return _do_call()
                raise err

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
                workflow=request.workflow,
                usage=usage,
                latency_ms=latency_ms,
                stream=False,
                success=True,
            )
            return response

        # Deadline-aware retry (improvement plan 1.3): the agent loop sets
        # request.timeout_s to its REMAINING wall-clock budget each iteration;
        # deriving the retry deadline from it keeps backoff sleeps from
        # blocking past the turn deadline. None (non-agent callers) preserves
        # the unbounded-by-deadline behaviour.
        deadline = (
            start + float(request.timeout_s)
            if request.timeout_s and request.timeout_s > 0 else None
        )
        return retry_with_backoff(
            _do_call, max_attempts=self.max_retries,
            deadline_monotonic_s=deadline,
            # Interruptible backoff (2026-07-22 audit B8): the agent loop puts
            # its cancel probe in request.metadata; absent for other callers.
            should_cancel=(request.metadata or {}).get("should_cancel"),
        )

    def stream(self, request: LLMRequest) -> StreamingResponse:
        """Streaming completion over Gemini's ``?alt=sse`` ``data:`` stream.

        Each ``data:`` line is a full ``GenerateContentResponse`` fragment;
        :func:`_extract_text_and_finish` pulls the incremental text and finish
        reason, and ``usageMetadata`` (present on later fragments) supplies the
        running token counts. NOTE: tool calls are NOT surfaced on the streaming
        path (tool use goes through :meth:`generate`). As with the other
        adapters, not retried and the fallback layer only switches before the
        first token; a mid-stream failure becomes ``final.error`` and is
        re-raised, with usage still recorded in the ``finally``.
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
            cancelled = False
            terminal_seen = False
            try:
                # nullcontext wraps the CACHED client so the streaming block is
                # unchanged but the shared client is NOT closed on exit (only the
                # inner client.stream(...) response is) — keep-alive survives.
                with contextlib.nullcontext(self._client()) as client:
                    with self._stream_with_heal(client, request, self._build_payload(request), start) as resp:
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
                            terminal_seen = terminal_seen or bool(_prompt_block_reason(data)) or any(
                                c.get("finishReason") for c in (data.get("candidates") or []) if isinstance(c, dict))
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
                if not terminal_seen:
                    raise LLMError(category=ErrorCategory.NETWORK,
                                   message="Provider stream interrupted before its completion marker",
                                   provider=self.name, model=request.model)
            except GeneratorExit:
                # Consumer abandoned the stream (client disconnect) — marked,
                # not failed. See CANCELLED_ERROR_CATEGORY. Gemini reports
                # usageMetadata on every chunk, so the counts gathered so far
                # are the provider's own.
                cancelled = True
                raise
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
                    workflow=request.workflow,
                    usage=final.usage,
                    latency_ms=final.latency_ms,
                    stream=True,
                    success=stream_error is None,
                    error_category=(
                        stream_error.category.value if stream_error
                        else CANCELLED_ERROR_CATEGORY if cancelled
                        else ""
                    ),
                )

        return StreamingResponse(response_gen=_iter(), final=final)


def _prompt_block_reason(body: dict) -> str:
    """The ``promptFeedback.blockReason`` of a fully-blocked prompt, else ``""``.

    Gemini expresses a PROMPT-level safety block differently from a
    RESPONSE-level one: instead of a candidate carrying
    ``finishReason: "SAFETY"``, it returns **zero candidates** plus a
    ``promptFeedback`` block. Read it so the caller can tell "the provider
    refused this input" from "the model produced nothing" — the agent loop's
    CONTENT_FILTER branch ends the turn cleanly, while an empty STOP counts
    toward the malformed-tool-call streak and drags the turn into the RAG
    fallback + capability nag (2026-07-20 audit; fixed 2026-07-30).
    """
    feedback = body.get("promptFeedback")
    if not isinstance(feedback, dict):
        return ""
    reason = feedback.get("blockReason")
    return reason if isinstance(reason, str) and reason else ""


def _extract_text_finish_and_calls(body: dict) -> tuple[str, FinishReason, list]:
    """Walk a Gemini response body's ``candidates[].content.parts`` and pull out
    the concatenated text, parsed ``functionCall`` tool calls, and the mapped
    finish reason (unknown reasons map to OTHER).

    A body with NO candidates but a ``promptFeedback.blockReason`` is a
    prompt-level refusal and yields CONTENT_FILTER (see
    :func:`_prompt_block_reason`); a candidate's own ``finishReason`` always
    wins over it, so the response-level SAFETY path is unchanged.
    """
    text_parts: list[str] = []
    tool_calls: list = []
    finish = FinishReason.STOP
    if not (body.get("candidates") or []) and _prompt_block_reason(body):
        finish = FinishReason.CONTENT_FILTER
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
                # Gemini 3.x attaches an opaque part-level thoughtSignature to
                # each functionCall and requires it echoed back verbatim in the
                # follow-up request (else 400 "missing a thought_signature").
                # Capture it here; build_gemini_contents re-emits it.
                sig = part.get("thoughtSignature")
                tc = parse_gemini_function_call(
                    part.get("functionCall") or {},
                    thought_signature=sig if isinstance(sig, str) else "",
                )
                if tc is not None:
                    tool_calls.append(tc)
        raw_finish = candidate.get("finishReason")
        if raw_finish:
            finish = _FINISH_REASONS.get(raw_finish, FinishReason.OTHER)
            if raw_finish in {"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL",
                              "TOO_MANY_TOOL_CALLS"}:
                logger.warning("Gemini returned malformed tool use: %s", raw_finish)
    return "".join(text_parts), finish, tool_calls


def _extract_text_and_finish(body: dict) -> tuple[str, FinishReason]:
    """Back-compat wrapper for the streaming path (tools are non-streaming)."""
    text, finish, _ = _extract_text_finish_and_calls(body)
    return text, finish


def _usage_from_body(body: dict) -> LLMUsage:
    """Read token usage from a response's ``usageMetadata`` block."""
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
    """Map a ≥400 Gemini HTTP response onto a normalised :class:`LLMError`.

    Prefers the provider's ``error.message``, categorises by status, and sets
    ``retryable`` for the transient categories. Only a RATE_LIMIT/INVALID_REQUEST
    whose message :func:`looks_like_quota` recognises is promoted to terminal
    QUOTA — Gemini's per-minute "Quota exceeded for quota metric …" 429 is NOT
    matched by those signals, so it correctly stays a retryable RATE_LIMIT.
    """
    status = resp.status_code
    text = body_text
    if text is None:
        try:
            text = resp.text
        except Exception:
            text = ""
    message = text or f"HTTP {status}"
    invalid_api_key = False
    try:
        body = json.loads(text)
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]
            message = error.get("message", message)
            invalid_api_key = any(
                isinstance(detail, dict) and detail.get("reason") == "API_KEY_INVALID"
                for detail in (error.get("details") or [])
            )
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
    if invalid_api_key:
        category = ErrorCategory.AUTH
    # Terminal billing/quota exhaustion surfaces immediately.  The signals
    # in ``looks_like_quota`` are specific enough that a Gemini per-minute
    # "Quota exceeded for quota metric …" 429 stays RATE_LIMIT (retryable).
    if category in (ErrorCategory.RATE_LIMIT, ErrorCategory.INVALID_REQUEST) and looks_like_quota(message):
        category = ErrorCategory.QUOTA
    # Honour the provider's own wait hint on a 429 (header first, then the
    # "try again in Xs" body phrase) — `retry_with_backoff` treats it as a
    # FLOOR over the exponential schedule, because sleeping less than asked is
    # a guaranteed second 429. Mirrors anthropic.py:760 / openai.py:667; the
    # Google adapter was the sibling that never captured it (2026-07-20 audit,
    # fixed 2026-07-30).
    retry_after = None
    if category == ErrorCategory.RATE_LIMIT:
        retry_after = parse_retry_after_s(message, getattr(resp, "headers", None))
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
        retry_after_s=retry_after,
    )
