"""OpenAI online adapter.

Uses the ``openai`` SDK (already pinned in requirements.txt for the LM
Studio compatibility layer) so we get robust streaming, automatic
retries, and the maintained schema for free. The API key is read from
``OPENAI_API_KEY`` (or the legacy ``OPENAI_KEY``) at call time so the
process picks up a key set after launch without a restart.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Iterator, Optional

from core.llm.base import LLMProvider, StreamingResponse, looks_like_quota
from core.llm.retry import parse_retry_after_s, retry_with_backoff
from core.llm.tool_schema import (
    build_openai_messages,
    jsonschema_to_openai_tool,
    parse_openai_tool_call,
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

# Cached ``openai.OpenAI`` clients, keyed by (base_url, timeout, max_retries,
# organization, api_key_fingerprint). A fresh client per generate()/stream()
# opened — and never closed — a new httpx connection pool + TLS session on EVERY
# online round-trip; in agent mode (N iterations), deck generation (per-section),
# and multi-turn chat that meant N TLS handshakes to the same host with each pool
# discarded immediately. The OpenAI client is thread-safe to share (mirrors
# ``core/providers/lms.py``'s cache), so one client per key preserves keep-alive
# across a turn and across requests.
#
# The key folds in a *fingerprint* of the resolved API key (a sha256 prefix,
# never the key itself) so a key rotated mid-session cannot silently reuse a
# stale-auth client — a new key → new fingerprint → new client — while the key is
# never stored in the cache. Keyspace is tiny (one host × the fixed online
# timeout/retries × one key), so entries do not accumulate.
_client_cache: dict = {}
_client_cache_lock = threading.Lock()


def _key_fingerprint(api_key: str) -> str:
    """Short, non-reversible fingerprint of an API key for cache-keying only."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


CURATED_MODELS: list[str] = [
    # Newest generation first (Track 5.5 — curated ids must have a
    # PRICING_TABLE entry; pinned by test_all_curated_models_are_priced).
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-3.5-turbo",
    "o1",
    "o1-mini",
    "o1-preview",
    "o3-mini",
]

# Substrings marking a NON-chat OpenAI model id (embeddings, audio, image,
# moderation, …) so the live merge doesn't pollute the chat picker. A denylist
# (not an allowlist) so a new chat family — gpt-6, o5, chatgpt-* — is appended
# automatically rather than silently filtered out.
#
# Deliberately NOT included: "search" and "computer-use" — both match real chat
# models (gpt-4o-search-preview, computer-use-preview) and would wrongly drop
# them. The legacy "*-search-*" embedding models are already covered by the
# "babbage"/"davinci"/"embedding" tokens, so omitting "search" loses nothing.
_NON_CHAT_OPENAI_TOKENS = (
    "embedding", "whisper", "tts", "audio", "dall-e", "image", "moderation",
    "realtime", "transcribe", "babbage", "davinci", "codex", "guard",
)

_FINISH_REASONS: dict[str, FinishReason] = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
    "tool_calls": FinishReason.TOOL_USE,
    "function_call": FinishReason.TOOL_USE,
}


class OpenAIProvider(LLMProvider):
    """Online chat adapter for the OpenAI Chat Completions API.

    Implements the provider-agnostic :class:`LLMProvider` contract over the
    official ``openai`` SDK. The wire format is the SDK's native
    ``chat.completions.create`` shape — messages built by
    :func:`build_openai_messages`, tools wrapped as
    ``{"type": "function", "function": {...}}`` by
    :func:`jsonschema_to_openai_tool`, and tool calls returned as
    ``tool_calls[i].function.arguments`` JSON *strings* (parsed by
    :func:`parse_openai_tool_call`).

    Parameter contract (re-verified vs platform.openai.com, 2026-07): the
    token cap is ``max_completion_tokens`` for every model on the official
    endpoint — ``max_tokens`` is deprecated (2024-09), auto-converted on
    older models, and HARD-REJECTED (400) by the o-series and the gpt-5.x
    family. Only a custom OpenAI-compatible ``base_url`` (which may predate
    the new param) still gets the legacy ``max_tokens`` — see
    :meth:`_uses_max_completion_tokens`. Sampling params (``temperature`` /
    ``top_p``) are stripped proactively for the o-series only
    (:meth:`_is_reasoning_model`); other rejecting models (gpt-5 reasoning
    variants can't be name-detected) are handled reactively by the
    :meth:`_heal_unsupported_param` strip-and-retry-once pass, which also
    absorbs the *next* family's parameter drift without a code change.
    Retries are handled in-process (``max_retries=0`` on the SDK client +
    :func:`retry_with_backoff`) so transient/terminal classification stays
    under the adapter's control.
    """

    name = "openai"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        organization: Optional[str] = None,
        timeout_s: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        """Stash connection settings; the SDK client is built lazily per call.

        ``api_key`` / ``base_url`` are normally left ``None`` so they resolve
        from the environment at call time (key never persisted to config); the
        explicit forms exist mostly for tests and OpenAI-compatible endpoints.
        ``timeout_s`` / ``max_retries`` come from ``online_timeout_s`` /
        ``online_max_retries`` via :func:`core.llm.factory.get_llm_provider`.
        """
        self._explicit_key = api_key
        self._explicit_base_url = base_url
        self._organization = organization
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    def _api_key(self) -> str:
        """Resolve the API key at call time, or raise an AUTH ``LLMError``.

        Read from the environment (``OPENAI_API_KEY``, then the legacy
        ``OPENAI_KEY``) on every call so a key set after launch is picked up
        without a restart. Raising AUTH here is what gates the live model
        listing: ``_fetch_live_models`` builds the client (→ this) before any
        network call, so a missing key degrades to curated-only with no I/O.
        """
        key = self._explicit_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY")
        if not key:
            raise LLMError(
                category=ErrorCategory.AUTH,
                message=(
                    "OPENAI_API_KEY is not set. Add it to the environment "
                    "before launching ChatEKLD, or set it in your shell profile."
                ),
                provider=self.name,
            )
        return key

    def _base_url(self) -> Optional[str]:
        """Optional API base override (``OPENAI_BASE_URL``) for OpenAI-compatible
        servers; ``None`` uses the SDK's default endpoint."""
        return self._explicit_base_url or os.environ.get("OPENAI_BASE_URL")

    def _client(self):
        """Return a cached ``openai.OpenAI`` client (keep-alive across calls).

        ``max_retries=0`` deliberately disables the SDK's own retry loop so the
        adapter owns transient-vs-terminal classification and backoff via
        :func:`retry_with_backoff`. Raises an INVALID_REQUEST ``LLMError`` when
        the SDK is not installed rather than a bare ImportError.

        The client is cached per (base_url, timeout, organization, key-fingerprint)
        so consecutive online calls reuse one httpx connection pool + TLS session
        instead of paying a fresh handshake each round-trip (see the module cache
        note). ``_api_key()`` is still resolved on every call FIRST, so the
        "missing key ⇒ AUTH error, no network" contract that gates live model
        listing is unchanged; the key only ever enters the cache as a fingerprint.
        """
        try:
            import openai
        except ImportError as exc:
            raise LLMError(
                category=ErrorCategory.INVALID_REQUEST,
                message="openai SDK is not installed",
                provider=self.name,
            ) from exc
        api_key = self._api_key()  # raises AUTH if unset — must run before any cache use
        base_url = self._base_url()
        key = (base_url, self.timeout_s, self._organization, _key_fingerprint(api_key))
        with _client_cache_lock:
            client = _client_cache.get(key)
            if client is None:
                client_kwargs = {
                    "api_key": api_key,
                    "timeout": self.timeout_s,
                    "max_retries": 0,  # we handle retries ourselves
                }
                if base_url:
                    client_kwargs["base_url"] = base_url
                if self._organization:
                    client_kwargs["organization"] = self._organization
                client = openai.OpenAI(**client_kwargs)
                _client_cache[key] = client
            return client

    def supports_embeddings(self) -> bool:
        """Online providers are chat-only; embeddings always resolve to a local
        provider (see the root CLAUDE.md Provider Rules)."""
        return False

    def supports_tool_use(self) -> bool:
        """OpenAI Chat Completions supports the structured ``tools=`` schema."""
        return True

    def health_check(self) -> tuple[bool, str]:
        """``ok`` iff an API key is present — no network round-trip.

        Mirrors ``/api/status`` semantics for online providers: a True here
        only asserts the key is set, not that the endpoint is reachable.
        """
        try:
            self._api_key()
            return True, ""
        except LLMError as err:
            return False, err.message

    def list_models(self) -> tuple[list[str], str]:
        """Curated models plus any chat models the live ``/v1/models`` endpoint
        reports (cached, key-gated). Falls back to curated-only without a key,
        offline, or on any error — see ``core.llm.model_listing``."""
        from core.llm.model_listing import merged_models
        models = merged_models(
            self.name, CURATED_MODELS, self._fetch_live_models,
            cache_key=f"openai:{self._base_url() or ''}",
        )
        return models, ""

    def _fetch_live_models(self) -> list[str]:
        """Chat-capable model ids from the OpenAI models endpoint.

        Constructs the client (which calls ``_api_key()`` — raising AUTH and so
        making NO network call when no key is set), lists models, and drops the
        non-chat families. Exceptions propagate to ``merged_models``, which
        treats them as "use curated only"."""
        client = self._client()
        out: list[str] = []
        for m in client.models.list():
            mid = getattr(m, "id", "") or (m.get("id") if isinstance(m, dict) else "")
            low = str(mid).lower()
            if mid and not any(tok in low for tok in _NON_CHAT_OPENAI_TOKENS):
                out.append(str(mid))
        return out

    def _build_messages(self, request: LLMRequest) -> list[dict]:
        """Translate the request (system prompt + messages + tool history) into
        OpenAI ``messages`` via the shared :func:`build_openai_messages`."""
        return build_openai_messages(request)

    @staticmethod
    def _is_reasoning_model(model: str) -> bool:
        """True for the o-series reasoning models (o1, o3, o4, o5, …).

        Governs PROACTIVE sampling-param stripping only (the token-cap name is
        decided separately by :meth:`_uses_max_completion_tokens`): o-series
        models REJECT ``temperature`` / ``top_p`` ("Only the default (1) value
        is supported" — re-verified vs platform.openai.com, 2026-07). Detect by
        family prefix — an ``o`` followed by a digit — so a future ``o6`` is
        covered without an allowlist edit, while ``gpt-4o`` (starts with ``g``)
        is correctly excluded. gpt-5 reasoning variants are intentionally NOT
        matched here: ``gpt-5-chat`` accepts the sampling params, so name-based
        detection would mis-strip them — a gpt-5 reasoning id that rejects them
        is healed reactively by :meth:`_heal_unsupported_param` instead (one
        stripped retry), which also covers whatever family drifts next.
        """
        m = (model or "").lower()
        return len(m) >= 2 and m[0] == "o" and m[1].isdigit()

    def _uses_max_completion_tokens(self, model: str) -> bool:
        """Whether the token cap must be sent as ``max_completion_tokens``.

        ``max_tokens`` has been deprecated since 2024-09; the official endpoint
        accepts ``max_completion_tokens`` on EVERY current model (older ones
        auto-convert) while the o-series and the gpt-5.x family hard-400 the
        legacy name ("Unsupported parameter: 'max_tokens' is not supported
        with this model" — field-reported on gpt-5.4, 2026-07). So the
        forward-compatible default is the new name whenever we are talking to
        api.openai.com. The ONE reason to keep ``max_tokens`` is a custom
        OpenAI-compatible ``base_url`` (LM Studio-style local servers, older
        proxies) that may predate the new param — those keep the legacy shape,
        and a compat server that *does* reject it is healed reactively by
        :meth:`_heal_unsupported_param`.
        """
        if self._base_url():
            # Custom endpoint: legacy shape unless the model family is known
            # to reject it outright (someone proxying o-series/gpt-5 traffic).
            m = (model or "").lower()
            return self._is_reasoning_model(model) or m.startswith("gpt-5")
        return True

    def _common_params(self, request: LLMRequest) -> dict:
        """Build the non-``messages`` kwargs shared by :meth:`generate` and
        :meth:`stream`, applying the reasoning-vs-chat parameter contract.

        Sampling params are dropped for o-series reasoning models (they reject
        non-default values); the token cap is named by
        :meth:`_uses_max_completion_tokens` (``max_completion_tokens`` on the
        official endpoint — the o-series and gpt-5.x hard-reject the legacy
        ``max_tokens`` — legacy name only for custom ``base_url`` compat
        servers). Tools, when present, are serialised to the OpenAI dialect
        and ``tool_choice`` defaults to ``"auto"``.
        """
        params: dict = {"model": request.model}
        if not self._is_reasoning_model(request.model):
            if request.temperature is not None:
                params["temperature"] = request.temperature
            if request.top_p is not None:
                params["top_p"] = request.top_p
        if request.max_tokens is not None:
            if self._uses_max_completion_tokens(request.model):
                params["max_completion_tokens"] = request.max_tokens
            else:
                params["max_tokens"] = request.max_tokens
        if request.stop:
            params["stop"] = request.stop
        if request.tools:
            params["tools"] = [jsonschema_to_openai_tool(t) for t in request.tools]
            params["tool_choice"] = request.tool_choice or "auto"
        return params

    # Params the heal pass may rename/strip, at most once each per call.
    # Anything else in a 400 is a genuine caller bug and must surface.
    _HEALABLE_PARAMS = ("max_tokens", "max_completion_tokens", "temperature", "top_p")

    def _heal_unsupported_param(self, params: dict, exc: BaseException, healed: set) -> str:
        """Self-healing for parameter-shape 400s: mutate *params* and retry.

        OpenAI moves models between parameter contracts faster than a
        name-based predicate can track (o-series, then gpt-5.x — each drift
        turned every call into a terminal 400 until an app update). When a 400
        names one of the four shape params, fix *params* in place — rename
        ``max_tokens`` ⇄ ``max_completion_tokens``, drop ``temperature`` /
        ``top_p`` — and return a short description so the caller retries the
        SAME request once per param (the ``healed`` set caps it; an empty
        return means "not healable, classify and raise"). Dropping a sampling
        param trades exact sampling for a successful call, which is strictly
        better than the hard failure the user otherwise sees.
        """
        body = getattr(exc, "body", None)
        named = body.get("param") if isinstance(body, dict) else None
        message = str(exc)
        offender = None
        for candidate in self._HEALABLE_PARAMS:
            if candidate in healed or candidate not in params:
                continue
            # Match the structured `param` field when present, else the quoted
            # name in the message ("Unsupported parameter: 'max_tokens' …").
            if named == candidate or f"'{candidate}'" in message:
                offender = candidate
                break
        if offender is None:
            return ""
        healed.add(offender)
        if offender == "max_tokens":
            params["max_completion_tokens"] = params.pop("max_tokens")
            return "renamed max_tokens -> max_completion_tokens"
        if offender == "max_completion_tokens":
            # An OpenAI-compatible server that predates the new name.
            params["max_tokens"] = params.pop("max_completion_tokens")
            return "renamed max_completion_tokens -> max_tokens"
        params.pop(offender, None)
        return f"dropped unsupported {offender}"

    def _create_with_heal(self, client, request: LLMRequest, **create_kwargs):
        """``chat.completions.create`` with the parameter-400 heal loop.

        Bounded: each healable param is fixed at most once, so the loop runs
        at most ``len(_HEALABLE_PARAMS)+1`` times before the original error
        classification path takes over. Heal retries are immediate (no
        backoff) — they replace a guaranteed-terminal 400, not a transient.
        """
        params = self._common_params(request)
        healed: set = set()
        while True:
            try:
                return client.chat.completions.create(
                    messages=self._build_messages(request),
                    **create_kwargs,
                    **params,
                )
            except Exception as exc:
                is_400 = getattr(exc, "status_code", None) == 400
                fix = self._heal_unsupported_param(params, exc, healed) if is_400 else ""
                if not fix:
                    raise self._classify_error(exc, request.model)
                logger.info(
                    "openai param heal for %s: %s (retrying once)",
                    request.model, fix,
                )

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Non-streaming completion: one ``chat.completions.create(stream=False)``
        call wrapped in :func:`retry_with_backoff`.

        Extracts text, normalised finish reason, token usage (including the
        cached-prompt-token detail), and any parsed tool calls into an
        :class:`LLMResponse`, and records the request with the usage tracker.
        Provider exceptions are normalised to ``LLMError`` by
        :meth:`_classify_error` so the retry/fallback layers can pattern-match.
        """
        start = time.monotonic()
        client = self._client()

        def _do_call() -> LLMResponse:
            # _create_with_heal classifies and raises LLMError itself (after
            # exhausting the parameter-400 heal pass).
            resp = self._create_with_heal(client, request, stream=False)

            choice = resp.choices[0] if resp.choices else None
            text = choice.message.content if choice and choice.message else ""
            finish = _FINISH_REASONS.get(choice.finish_reason if choice else "", FinishReason.STOP)
            usage = LLMUsage(
                input_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
                cached_input_tokens=_cached_tokens_from_usage(resp.usage),
            )
            tool_calls = _extract_openai_tool_calls(choice)
            latency_ms = int((time.monotonic() - start) * 1000)
            response = LLMResponse(
                text=text or "",
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
        """Streaming completion: yield content deltas as they arrive.

        Requests usage on the final chunk (``stream_options={"include_usage":
        True}``) and accumulates tokens into ``buf`` so the ``final``
        :class:`LLMResponse` carries the full text + usage once the generator is
        exhausted. NOTE: this adapter is NOT wrapped in
        :func:`retry_with_backoff` and the fallback layer only switches
        providers *before the first token* — a mid-stream failure here is
        captured as ``final.error`` and re-raised, surfacing to the route as a
        structured SSE error rather than a silent re-stream of the whole answer.
        Usage is recorded in the ``finally`` so a failed stream still counts.
        """
        start = time.monotonic()
        client = self._client()
        final = LLMResponse(provider=self.name, model=request.model)
        buf: list[str] = []

        def _iter() -> Iterator[str]:
            # A parameter-shape 400 surfaces at create() time (before any
            # token), so the heal pass covers streaming too; it classifies
            # and raises LLMError itself when unhealable.
            stream = self._create_with_heal(
                client, request,
                stream=True,
                stream_options={"include_usage": True},
            )
            usage_in: int = 0
            usage_out: int = 0
            cached_in: int = 0
            finish_reason: FinishReason = FinishReason.STOP
            stream_error: LLMError | None = None
            try:
                for chunk in stream:
                    if chunk.usage is not None:
                        usage_in = getattr(chunk.usage, "prompt_tokens", usage_in) or usage_in
                        usage_out = getattr(chunk.usage, "completion_tokens", usage_out) or usage_out
                        cached_in = _cached_tokens_from_usage(chunk.usage) or cached_in
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = getattr(choice, "delta", None)
                    if delta is not None:
                        content = getattr(delta, "content", None) or ""
                        if content:
                            buf.append(content)
                            yield content
                    if choice.finish_reason:
                        finish_reason = _FINISH_REASONS.get(choice.finish_reason, FinishReason.STOP)
            except Exception as exc:
                stream_error = self._classify_error(exc, request.model)
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

    def _classify_error(self, exc: BaseException, model: str) -> LLMError:
        """Map an SDK/transport exception onto a normalised :class:`LLMError`.

        Categorises first by SDK exception type, then by HTTP status as a
        fallback, and sets ``retryable`` for the transient categories
        (timeout/network/rate_limit/server_error). Critically, a RATE_LIMIT that
        :func:`looks_like_quota` recognises (``insufficient_quota`` and friends)
        is re-mapped to the terminal QUOTA category so a billing exhaustion
        surfaces immediately instead of burning retries — keeping it distinct
        from a transient per-minute 429.
        """
        if isinstance(exc, LLMError):
            return exc
        try:
            import openai
        except ImportError:
            openai = None  # type: ignore[assignment]
        category = ErrorCategory.UNKNOWN
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        message = str(exc) or exc.__class__.__name__

        if openai is not None:
            if isinstance(exc, getattr(openai, "AuthenticationError", ())):
                category = ErrorCategory.AUTH
            elif isinstance(exc, getattr(openai, "RateLimitError", ())):
                category = ErrorCategory.RATE_LIMIT
            elif isinstance(exc, getattr(openai, "APITimeoutError", ())):
                category = ErrorCategory.TIMEOUT
            elif isinstance(exc, getattr(openai, "APIConnectionError", ())):
                category = ErrorCategory.NETWORK
            elif isinstance(exc, getattr(openai, "BadRequestError", ())):
                category = ErrorCategory.INVALID_REQUEST
            elif isinstance(exc, getattr(openai, "NotFoundError", ())):
                category = ErrorCategory.NOT_FOUND
            elif isinstance(exc, getattr(openai, "InternalServerError", ())):
                category = ErrorCategory.SERVER_ERROR
        if category == ErrorCategory.UNKNOWN and isinstance(status, int):
            if status == 401 or status == 403:
                category = ErrorCategory.AUTH
            elif status == 404:
                category = ErrorCategory.NOT_FOUND
            elif status == 408:
                category = ErrorCategory.TIMEOUT
            elif status == 429:
                category = ErrorCategory.RATE_LIMIT
            elif 500 <= status < 600:
                category = ErrorCategory.SERVER_ERROR
        # Distinguish a hard quota/billing failure from a transient rate
        # limit: the former is terminal (non-retryable, never falls back)
        # so the user sees it immediately rather than after wasted retries.
        if category == ErrorCategory.RATE_LIMIT and looks_like_quota(
            message, getattr(exc, "code", None)
        ):
            category = ErrorCategory.QUOTA
        # 429s carry the provider's own wait: the Retry-After header on the
        # SDK exception's response, or the "Please try again in Xs" phrase in
        # the body. Captured here so the retry layers can floor their backoff
        # on it — a shorter sleep is a guaranteed second 429 (field-reported:
        # deck retries at 4s against a 5.764s TPM hint burned every attempt).
        retry_after = None
        if category == ErrorCategory.RATE_LIMIT:
            headers = getattr(getattr(exc, "response", None), "headers", None)
            retry_after = parse_retry_after_s(message, headers)
        return LLMError(
            category=category,
            message=message,
            provider=self.name,
            model=model,
            status_code=int(status) if isinstance(status, int) else None,
            retryable=category in {
                ErrorCategory.TIMEOUT,
                ErrorCategory.NETWORK,
                ErrorCategory.RATE_LIMIT,
                ErrorCategory.SERVER_ERROR,
            },
            retry_after_s=retry_after,
        )


def _extract_openai_tool_calls(choice) -> list:
    """Pull ``tool_calls`` off the SDK response object and parse them.

    Returns an empty list when no calls were made, when the response
    shape is unfamiliar, or when individual call JSON arguments fail to
    parse — the agent loop counts a fully-empty list as a malformed
    iteration.
    """
    if choice is None:
        return []
    message = getattr(choice, "message", None)
    if message is None:
        return []
    raw_calls = getattr(message, "tool_calls", None) or []
    parsed: list = []
    for raw in raw_calls:
        as_dict = _openai_tool_call_to_dict(raw)
        tc = parse_openai_tool_call(as_dict)
        if tc is not None:
            parsed.append(tc)
    return parsed


def _openai_tool_call_to_dict(raw) -> dict:
    """Normalise an SDK ToolCall object (or dict) to the dict shape
    :func:`parse_openai_tool_call` expects."""
    if isinstance(raw, dict):
        return raw
    function = getattr(raw, "function", None)
    if function is None:
        return {}
    return {
        "id": getattr(raw, "id", "") or "",
        "type": getattr(raw, "type", "function"),
        "function": {
            "name": getattr(function, "name", "") or "",
            "arguments": getattr(function, "arguments", "") or "",
        },
    }


def _cached_tokens_from_usage(usage) -> int:
    """Read OpenAI's prompt-cache hit count from ``usage.prompt_tokens_details``.

    Returns 0 when the field is absent (older models / no cache hit). These
    cached input tokens are billed at a discount, so the usage tracker records
    them separately for accurate cost estimation.
    """
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return 0
    cached = getattr(details, "cached_tokens", None)
    if isinstance(cached, int):
        return cached
    return 0
