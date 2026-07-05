"""Anthropic online adapter.

Uses ``httpx`` (already in requirements) for both streaming and
non-streaming requests so the adapter has no hard dependency on the
``anthropic`` SDK package. When the SDK is present the adapter still
honours the same env vars (``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL``).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
from typing import Iterator, Optional

from core.llm.base import LLMProvider, StreamingResponse, looks_like_quota
from core.llm.retry import parse_retry_after_s, retry_with_backoff
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

# Cached shared ``httpx.Client`` per (base_url, timeout). Previously every
# generate()/stream() opened a ``with httpx.Client(...)`` and closed it on exit,
# so each online round-trip built and tore down a fresh connection pool + TLS
# session — N handshakes per agent turn / per deck section to the same host. A
# long-lived shared client keeps connections alive across calls. httpx.Client is
# thread-safe for concurrent requests, so one client serves all callers.
#
# Unlike the OpenAI cache, the API key is NOT part of the key: Anthropic auth is
# a per-request header (``_headers()`` re-reads the env key every call), so a
# rotated key applies on the next request through the SAME pooled client — no
# stale-auth risk and no need to fingerprint the key here. The listing call
# (_fetch_live_models) keeps its own short-lived client (different, shorter
# timeout, rare + TTL-cached) and is intentionally left uncached.
_client_cache: dict = {}
_client_cache_lock = threading.Lock()

# Active models only (per platform.claude.com; newest generations added
# 2026-07-04, Track 5.5 — the curated list is authoritative for the default
# selection and must carry a PRICING_TABLE entry, pinned by
# test_all_curated_models_are_priced).  The Claude 3.x entries were removed
# because those models are retired and now 404: 3.5 Sonnet (2025-10),
# 3 Opus (2026-01), 3.5 Haiku (2026-02).
CURATED_MODELS: list[str] = [
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
]

_API_VERSION = "2023-06-01"
_DEFAULT_BASE_URL = "https://api.anthropic.com"

# Model families on which Anthropic REMOVED the sampling params — sending
# temperature/top_p/top_k 400s the whole call ("`temperature` is deprecated
# for this model", field-reported on claude-fable-5 2026-07; verified vs
# platform.claude.com: removed on Fable 5 / Mythos 5 / Opus 4.7+ / Sonnet 5,
# still accepted ≤1.0 on Opus 4.6 / Sonnet 4.6 and older). Prefix-matched so
# dated snapshots ("claude-opus-4-8-2026…") are covered; a *future* family
# that also drops them is healed reactively by the strip-and-retry pass in
# generate()/stream() rather than requiring a code change here.
_SAMPLING_REMOVED_MODEL_PREFIXES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-5",
)

# Params the reactive heal pass may strip from a payload, at most once each
# per call. top_k is listed for completeness (this adapter never sends it
# today) so the heal loop keeps working if it is ever added.
_STRIPPABLE_SAMPLING_PARAMS = ("temperature", "top_p", "top_k")


def _sampling_params_removed(model: str) -> bool:
    """True when *model* belongs to a family that rejects sampling params."""
    return (model or "").lower().startswith(_SAMPLING_REMOVED_MODEL_PREFIXES)


def _strip_rejected_sampling_param(payload: dict, message: str, healed: set) -> str:
    """Reactive self-heal for a sampling-param 400: mutate *payload*, retry.

    When a 400 message names a sampling param the payload actually carries
    (word-boundary match — Anthropic backtick-quotes the name), drop it and
    return a short description so the caller retries the SAME request once
    per param (bounded by the *healed* set; "" means not healable → raise).
    Dropping the param trades exact sampling for a successful call — strictly
    better than the terminal 400 the user otherwise sees, and it absorbs the
    next family's parameter drift without an adapter update.
    """
    for candidate in _STRIPPABLE_SAMPLING_PARAMS:
        if candidate in healed or candidate not in payload:
            continue
        if re.search(rf"\b{candidate}\b", message or ""):
            healed.add(candidate)
            payload.pop(candidate, None)
            return f"dropped unsupported {candidate}"
    return ""

_STOP_REASON_MAP: dict[str, FinishReason] = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "tool_use": FinishReason.TOOL_USE,
}


def _usage_from_anthropic_block(usage_block: dict) -> LLMUsage:
    """Normalise an Anthropic ``usage`` block into the app's LLMUsage shape.

    SEMANTICS GUARD (Track 5.5): Anthropic's ``input_tokens`` EXCLUDES the
    cache-read and cache-write tokens (total prompt = input + cache_read +
    cache_creation), whereas OpenAI's ``prompt_tokens`` INCLUDES its cached
    subset — and ``estimate_cost_usd`` (plus the /api/usage totals) are built
    on the inclusive shape. Recording the raw Anthropic numbers would both
    under-report prompt size and make the cost formula subtract cache reads
    from a count that never contained them. So the adapter re-adds the cache
    figures here: ``input_tokens`` becomes the TOTAL prompt size,
    ``cached_input_tokens`` the read subset, ``cache_creation_input_tokens``
    the write subset (billed at 1.25× by the pricing layer). Pinned by
    TestAnthropicPromptCaching.
    """
    api_input = int(usage_block.get("input_tokens", 0) or 0)
    cache_read = int(usage_block.get("cache_read_input_tokens", 0) or 0)
    cache_creation = int(usage_block.get("cache_creation_input_tokens", 0) or 0)
    return LLMUsage(
        input_tokens=api_input + cache_read + cache_creation,
        output_tokens=int(usage_block.get("output_tokens", 0) or 0),
        cached_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )


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

    def _client(self):
        """Return a cached, long-lived ``httpx.Client`` for (base_url, timeout).

        Reused across generate()/stream() so connections stay keep-alive instead
        of a fresh pool + TLS handshake per call (see the module cache note). The
        client is never closed (process-lifetime, like the local-provider caches);
        auth is applied per request via ``_headers()``, so the client is
        key-agnostic and a rotated key takes effect on the next request.
        """
        httpx = self._httpx()
        key = (self._base_url(), self.timeout_s)
        with _client_cache_lock:
            client = _client_cache.get(key)
            if client is None:
                client = httpx.Client(timeout=self.timeout_s)
                _client_cache[key] = client
            return client

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
            # Prompt caching (Track 5.5, 2026-07-04): the system prompt goes as
            # a block array with a cache_control breakpoint instead of a bare
            # string. Anthropic caching is a PREFIX match over tools → system →
            # messages, so this single breakpoint caches the tool definitions
            # AND the system prompt together — exactly the stable prefix that
            # repeats across agent-loop iterations, deck per-section turns, and
            # same-settings vault chats. Cache reads bill at ~0.1× the input
            # rate, writes at ~1.25× (5-min TTL); below the model's minimum
            # cacheable prefix the marker is silently ignored (no write, no
            # premium), so marking is safe for short prompts too. The volatile
            # parts (retrieved context, the user question, tool results) ride
            # in `messages`, after the breakpoint. Wire shape per
            # platform.claude.com prompt-caching docs (anthropic-version
            # 2023-06-01 accepts both the string and block-array forms).
            payload["system"] = [{
                "type": "text",
                "text": request.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }]
        # Newest families (Fable 5 / Mythos 5 / Opus 4.7+ / Sonnet 5) REMOVED
        # the sampling params — sending them 400s every call, which took down
        # deck generate/augment and vault chat against those models entirely
        # (the app's callers always set a temperature from config). Older
        # models keep them, clamped: Anthropic caps temperature at 1.0
        # (verified vs platform.claude.com, 2026-06) whereas OpenAI/Gemini
        # allow 2.0 and the app's vault-chat range is 0-2.
        if not _sampling_params_removed(request.model):
            if request.temperature is not None:
                payload["temperature"] = min(float(request.temperature), 1.0)
            if request.top_p is not None:
                payload["top_p"] = request.top_p
        if request.stop:
            payload["stop_sequences"] = request.stop
        if request.tools:
            # Tools are included even for tool_choice="none": once the
            # history contains tool_use/tool_result blocks (the agent loop's
            # forced final turn arrives exactly then), Anthropic REQUIRES the
            # tools param and 400s without it. ``{"type": "none"}`` is the
            # supported way to forbid new calls (verified vs
            # platform.claude.com 2026-07 — the old "omit tools entirely"
            # workaround predates the none variant and breaks that case).
            payload["tools"] = [jsonschema_to_anthropic_tool(t) for t in request.tools]
            choice = request.tool_choice
            if choice in (None, "auto"):
                payload["tool_choice"] = {"type": "auto"}
            elif choice in ("required", "any"):
                payload["tool_choice"] = {"type": "any"}
            elif choice == "none":
                payload["tool_choice"] = {"type": "none"}
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

        payload = self._build_payload(request, stream=False)
        healed: set = set()

        def _do_call() -> LLMResponse:
            while True:
                try:
                    # Cached shared client (keep-alive) instead of a per-call pool.
                    client = self._client()
                    resp = client.post(
                        f"{self._base_url()}/v1/messages",
                        headers=self._headers(),
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
                if resp.status_code < 400:
                    break
                err = _http_error_to_llm_error(resp, self.name, request.model)
                # Reactive param heal: a 400 naming a sampling param the
                # payload carries (a family drift _sampling_params_removed
                # doesn't know yet) gets that param stripped and ONE
                # immediate retry — not a transient, so no backoff.
                if err.category is ErrorCategory.INVALID_REQUEST:
                    fix = _strip_rejected_sampling_param(payload, err.message, healed)
                    if fix:
                        logger.info(
                            "anthropic param heal for %s: %s (retrying once)",
                            request.model, fix,
                        )
                        continue
                raise err
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
            usage = _usage_from_anthropic_block(usage_block)
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
        )

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

        payload = self._build_payload(request, stream=True)
        healed: set = set()

        def _iter() -> Iterator[str]:
            # Raw per-event counters in Anthropic's own (exclusive) semantics;
            # normalised into the inclusive LLMUsage shape in the finally via
            # _usage_from_anthropic_block.
            usage_in = 0
            usage_out = 0
            cached_in = 0
            creation_in = 0
            finish_reason = FinishReason.STOP
            stream_error: LLMError | None = None
            try:
                # nullcontext wraps the CACHED client so the streaming block's
                # structure/indentation is unchanged, but the shared client is NOT
                # closed on exit (only the inner client.stream(...) response is) —
                # keep-alive survives for the next call.
                with contextlib.nullcontext(self._client()) as client:
                    while True:  # re-entered only by the pre-token param heal below
                        with client.stream(
                            "POST",
                            f"{self._base_url()}/v1/messages",
                            headers=self._headers(),
                            json=payload,
                        ) as resp:
                            if resp.status_code >= 400:
                                try:
                                    body_text = resp.read().decode("utf-8", errors="replace")
                                except Exception:
                                    body_text = ""
                                err = _http_error_to_llm_error(
                                    resp, self.name, request.model, body_text=body_text,
                                )
                                # Reactive param heal, streaming flavour: a
                                # parameter-shape 400 always arrives BEFORE any
                                # token (it is the response status), so retrying
                                # the whole stream here can never replay
                                # already-yielded text.
                                if err.category is ErrorCategory.INVALID_REQUEST:
                                    fix = _strip_rejected_sampling_param(
                                        payload, err.message, healed,
                                    )
                                    if fix:
                                        logger.info(
                                            "anthropic param heal for %s: %s (retrying once)",
                                            request.model, fix,
                                        )
                                        continue
                                raise err
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
                                    creation_in = int(msg_usage.get("cache_creation_input_tokens", 0) or 0)
                                elif ev_type == "message_delta":
                                    delta_usage = data.get("usage") or {}
                                    if delta_usage:
                                        usage_out = int(delta_usage.get("output_tokens", 0) or usage_out)
                                    stop_reason = (data.get("delta") or {}).get("stop_reason")
                                    if stop_reason:
                                        finish_reason = _STOP_REASON_MAP.get(stop_reason, FinishReason.STOP)
                        break  # normal completion — the while exists only for the heal retry
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
                final.usage = _usage_from_anthropic_block({
                    "input_tokens": usage_in,
                    "output_tokens": usage_out,
                    "cache_read_input_tokens": cached_in,
                    "cache_creation_input_tokens": creation_in,
                })
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
    # 429s carry the provider's own wait (Retry-After header / "try again in
    # Xs" body phrase) — captured so the retry layers can floor their backoff
    # on it instead of guaranteeing a second 429 with a shorter sleep.
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
