"""LLM adapter that proxies to the existing local providers."""
# Bridges the provider-agnostic ``core.llm.base.LLMProvider`` contract onto the
# older ``core.providers.base.Provider`` abstraction (Ollama / LM Studio) so RAG /
# summariser / agent code flows through one interface for both local and online
# backends. Two distinct code paths live here:
#
#   * Plain generation (``LocalLLMProvider.stream`` / ``generate`` with no tools)
#     goes through the legacy ``Provider.stream_chat``, which flattens the
#     structured ``messages`` back into a single ``(prompt, system_prompt)`` pair
#     (see ``_build_prompt``).
#   * Agent tool use (``_generate_with_tools``) bypasses that flatten — which
#     would corrupt a multi-turn ``tool_call``/``tool_result`` conversation — and
#     calls ``ollama.chat()`` / LM Studio's OpenAI client directly with structured
#     ``messages`` + ``tools``, bounded by ``_effective_local_timeout`` so the
#     agent's per-iteration deadline can actually interrupt a wedged backend.
#
# Token usage is estimated locally (``core.llm.usage.estimate_tokens``) when the
# backend does not return counts, so cost tracking stays uniform across providers
# even though local generation is free.
from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Iterator, Optional

from core.llm.base import LLMProvider, StreamingResponse, coerce_error
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
    ToolCall,
)
from core.llm.usage import estimate_tokens, usage_tracker

logger = logging.getLogger(__name__)


def _classify_local_error(
    exc: BaseException, *, provider: str, model: str = "",
) -> LLMError:
    """Map a local-backend transport exception to the right ``ErrorCategory``.

    A bare ``coerce_error`` defaults to ``UNKNOWN`` (non-retryable, and **not**
    in the default ``fallback_on`` set), so a connection-refused from a stopped
    Ollama / LM Studio would surface as a hard error instead of failing over to
    a configured online ``fallback_provider`` — unlike the online adapters,
    which map the same class of failure to ``NETWORK``. Detect connection /
    timeout shapes here (covers ollama's ``httpx.ConnectError`` / ``ConnectTimeout``
    and LM Studio's ``openai.APIConnectionError`` / ``APITimeoutError``) so
    local-down is fallback-eligible too; anything else degrades to
    ``coerce_error``'s ``UNKNOWN``.
    """
    if isinstance(exc, LLMError):
        return exc
    blob = f"{type(exc).__name__.lower()} {str(exc).lower()}"
    if "timeout" in blob or "timed out" in blob:
        category = ErrorCategory.TIMEOUT
    elif any(sig in blob for sig in (
        "connect", "refused", "connection", "max retries",
        "unreachable", "reset by peer", "broken pipe",
        "failed to establish", "name or service not known",
    )):
        category = ErrorCategory.NETWORK
    else:
        return coerce_error(exc, provider=provider, model=model)
    return LLMError(
        category=category,
        message=str(exc) or exc.__class__.__name__,
        provider=provider,
        model=model,
        retryable=True,
    )


def _effective_local_timeout(request: LLMRequest) -> Optional[float]:
    """Tightest HTTP timeout for a local *tool* call, in seconds.

    Takes the smaller of the per-request ``timeout_s`` (the agent loop sets this
    from the remaining wall-clock deadline) and the configured
    ``local_request_timeout_s``. Returns ``None`` when neither is set, leaving
    the SDK default. Bounding the tool-call client is what lets the agent loop's
    deadline actually interrupt a wedged local backend — the loop's
    between-iteration cancel check cannot reach into a blocking HTTP read.
    """
    from core.providers.base import local_request_timeout
    candidates: list[float] = []
    if request.timeout_s and request.timeout_s > 0:
        candidates.append(float(request.timeout_s))
    base = local_request_timeout()
    if base is not None:
        candidates.append(base)
    if not candidates:
        return None
    # Quantize UP to whole seconds. The agent feeds a continuously-varying float
    # here (the shrinking remaining-deadline), and OllamaProvider._client caches
    # one ollama.Client + httpx pool per distinct (host, timeout). Without this
    # rounding, every call near the deadline — and EVERY call whenever
    # agent_wall_clock_s <= online_timeout_s (e.g. the documented wall_clock=30
    # config) — mints a new cached client that never evicts, an unbounded fd /
    # memory leak. ceil (never floor) keeps the bound no shorter than intended;
    # integer buckets cap the cache at <= max(online_timeout_s, wall_clock)
    # entries per host (in practice a handful, since a turn spans a few seconds).
    return float(math.ceil(min(candidates)))


class LocalLLMProvider(LLMProvider):
    """Thin adapter around the existing :class:`core.providers.base.Provider`.

    The local Provider abstraction predates :class:`LLMProvider`; this
    adapter wraps it so the same RAG / summariser code paths can flow
    through both local and online providers without branching on the
    provider name at every call site.
    """

    def __init__(self, name: str) -> None:
        """Pin the adapter to ``ollama`` or ``lm_studio``; reject anything else.

        Online provider names never reach here — :func:`core.llm.factory` routes
        them to their own adapters — so an unexpected name is a programmer error.
        """
        if name not in ("ollama", "lm_studio"):
            raise LLMError(
                category=ErrorCategory.INVALID_REQUEST,
                message=f"local provider must be ollama or lm_studio, got {name!r}",
                provider=name,
            )
        self.name = name

    def _provider(self):
        """Resolve the concrete local :class:`core.providers.base.Provider`.

        ``get_provider`` returns a FRESH provider instance each call; the
        underlying ollama client (and its httpx pool) is cached at module scope
        in :mod:`core.providers.ollama`, so this is cheap.
        """
        from core.providers import get_provider
        return get_provider(self.name)

    def supports_embeddings(self) -> bool:
        """Local providers DO expose embeddings (used by the indexer); this is
        the one place the embedding-capable flag is True."""
        return True

    def supports_tool_use(self) -> bool:
        """Adapter-level capability flag.

        Returns ``True`` for both Ollama and LM Studio — both speak the
        OpenAI-shape ``tools=`` parameter on their chat endpoint.
        Whether the *configured model* honours that schema is a runtime
        question (small or non-tool-tuned local models will produce
        plain text instead of structured calls). The agent loop's
        malformed-call counter handles model-level non-support
        gracefully by falling back to plain RAG.
        """
        return True

    def list_models(self) -> tuple[list[str], str]:
        """List installed local models, or ``([], error_message)`` if the
        backend is unreachable."""
        try:
            return self._provider().get_models()
        except Exception as exc:
            return [], str(exc)

    def health_check(self) -> tuple[bool, str]:
        """Probe whether the local backend is actually running (unlike the
        online adapters' key-presence-only check)."""
        try:
            return self._provider().check_running()
        except Exception as exc:
            return False, str(exc)

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Non-streaming completion.

        Routes to the structured tool-use path when ``request.tools`` is set;
        otherwise drains :meth:`stream` to completion and returns its
        accumulated ``final`` response. The empty-tools default keeps every
        existing non-agent caller on the legacy stream-chat code path.
        """
        if request.tools:
            return self._generate_with_tools(request)
        text_chunks: list[str] = []
        stream = self.stream(request)
        for token in stream.response_gen:
            text_chunks.append(token)
        stream.final.text = "".join(text_chunks)
        return stream.final

    def _generate_with_tools(self, request: LLMRequest) -> LLMResponse:
        """Non-streaming tool-use path for Ollama and LM Studio.

        The legacy :meth:`stream` path flattens ``messages`` into a
        single user prompt, which corrupts multi-turn tool conversations
        (assistant ``tool_call`` then user ``tool_result``). This method
        bypasses that and calls the underlying chat API directly with
        structured ``messages`` + ``tools``.
        """
        messages = build_openai_messages(request)
        tools_payload = [jsonschema_to_openai_tool(t) for t in request.tools]
        start = time.monotonic()
        if self.name == "ollama":
            try:
                response = self._ollama_chat_with_tools(
                    request, messages, tools_payload,
                )
            except Exception as exc:
                err = _classify_local_error(exc, provider=self.name, model=request.model)
                self._record_tool_failure(request, start, err)
                raise err
        else:
            try:
                response = self._lm_studio_chat_with_tools(
                    request, messages, tools_payload,
                )
            except Exception as exc:
                err = _classify_local_error(exc, provider=self.name, model=request.model)
                self._record_tool_failure(request, start, err)
                raise err
        latency_ms = int((time.monotonic() - start) * 1000)
        response.latency_ms = latency_ms
        usage_tracker.record(
            provider=self.name,
            model=request.model,
            usage=response.usage,
            latency_ms=latency_ms,
            stream=False,
            success=True,
        )
        return response

    def _ollama_chat_with_tools(
        self,
        request: LLMRequest,
        messages: list[dict],
        tools_payload: list[dict],
    ) -> LLMResponse:
        """One non-streaming ``ollama.chat(tools=...)`` round-trip.

        Maps the request's sampling knobs into Ollama's ``options`` dict
        (``num_predict`` for the token cap, plus ``num_ctx`` from metadata),
        routes through the provider's cached client bounded by
        :func:`_effective_local_timeout`, parses ``message.tool_calls`` (ids are
        synthesised — Ollama omits them), and falls back to
        :func:`estimate_tokens` when the backend returns no usage counts.
        """
        import ollama
        provider = self._provider()
        resolved = provider.resolve_model(request.model) if hasattr(provider, "resolve_model") else request.model
        options: dict = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.top_p is not None:
            options["top_p"] = request.top_p
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens
        ctx = request.metadata.get("num_ctx") if request.metadata else None
        if ctx is not None:
            options["num_ctx"] = int(ctx)

        call_kwargs: dict = {
            "model": resolved,
            "messages": messages,
            "tools": tools_payload,
            "stream": False,
        }
        if options:
            call_kwargs["options"] = options
        # Route through the provider's client, bounded by the tightest of
        # (per-request deadline, configured local_request_timeout_s), so a
        # wedged backend cannot outlive the agent turn's wall-clock budget.
        timeout = _effective_local_timeout(request)
        client = provider._client() if timeout is None else provider._client(timeout=timeout)
        raw = client.chat(**call_kwargs)

        msg = _ollama_attr(raw, "message") or {}
        content = _ollama_attr(msg, "content") or ""
        raw_tool_calls = _ollama_attr(msg, "tool_calls") or []
        tool_calls = _parse_ollama_tool_calls(raw_tool_calls)
        done_reason = _ollama_attr(raw, "done_reason") or ""

        if tool_calls:
            finish = FinishReason.TOOL_USE
        elif done_reason == "length":
            finish = FinishReason.LENGTH
        else:
            finish = FinishReason.STOP

        input_tokens = int(_ollama_attr(raw, "prompt_eval_count") or 0)
        output_tokens = int(_ollama_attr(raw, "eval_count") or 0)
        if input_tokens == 0:
            input_tokens = estimate_tokens(_concat_messages(messages))
        if output_tokens == 0:
            output_tokens = estimate_tokens(content)

        return LLMResponse(
            text=content,
            provider=self.name,
            model=request.model,
            finish_reason=finish,
            usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens),
            tool_calls=tool_calls,
        )

    def _lm_studio_chat_with_tools(
        self,
        request: LLMRequest,
        messages: list[dict],
        tools_payload: list[dict],
    ) -> LLMResponse:
        """One non-streaming OpenAI-shape ``chat.completions.create(tools=...)``
        call against the LM Studio server.

        Builds a one-shot ``openai.OpenAI`` client pointed at LM Studio's
        ``base_url`` (placeholder api key), bounded by
        :func:`_effective_local_timeout` like the Ollama branch, then parses the
        OpenAI-native ``tool_calls`` and estimates usage when missing.
        """
        import openai
        provider = self._provider()
        base_url = getattr(provider, "base_url", None)
        if not base_url:
            raise LLMError(
                category=ErrorCategory.INVALID_REQUEST,
                message="LM Studio base_url is not configured",
                provider=self.name,
                model=request.model,
            )
        # Bound this (non-streaming) tool call by the tightest of (per-request
        # deadline, configured local_request_timeout_s); None leaves the OpenAI
        # SDK default. Mirrors the Ollama tool branch above.
        timeout = _effective_local_timeout(request)
        client_init: dict = {"base_url": base_url, "api_key": "lm-studio"}
        if timeout is not None:
            client_init["timeout"] = timeout
        client = openai.OpenAI(**client_init)
        call_kwargs: dict = {
            "model": request.model,
            "messages": messages,
            "tools": tools_payload,
            "tool_choice": request.tool_choice or "auto",
            "stream": False,
        }
        if request.temperature is not None:
            call_kwargs["temperature"] = request.temperature
        if request.top_p is not None:
            call_kwargs["top_p"] = request.top_p
        if request.max_tokens is not None:
            call_kwargs["max_tokens"] = request.max_tokens
        resp = client.chat.completions.create(**call_kwargs)

        choice = resp.choices[0] if resp.choices else None
        text = (choice.message.content if choice and choice.message else "") or ""
        raw_calls = getattr(choice.message, "tool_calls", None) if choice and choice.message else None
        tool_calls = _parse_lm_studio_tool_calls(raw_calls or [])
        raw_finish = (choice.finish_reason if choice else "") or "stop"
        if tool_calls:
            finish = FinishReason.TOOL_USE
        elif raw_finish == "length":
            finish = FinishReason.LENGTH
        elif raw_finish == "content_filter":
            finish = FinishReason.CONTENT_FILTER
        else:
            finish = FinishReason.STOP

        input_tokens = int(getattr(resp.usage, "prompt_tokens", 0) or 0) if resp.usage else 0
        output_tokens = int(getattr(resp.usage, "completion_tokens", 0) or 0) if resp.usage else 0
        if input_tokens == 0:
            input_tokens = estimate_tokens(_concat_messages(messages))
        if output_tokens == 0:
            output_tokens = estimate_tokens(text)

        return LLMResponse(
            text=text,
            provider=self.name,
            model=request.model,
            finish_reason=finish,
            usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens),
            tool_calls=tool_calls,
        )

    def _record_tool_failure(
        self,
        request: LLMRequest,
        start: float,
        err: LLMError,
    ) -> None:
        """Record a failed tool call (zero tokens) with the usage tracker so an
        agent turn that errored is still represented in ``/api/usage``."""
        latency_ms = int((time.monotonic() - start) * 1000)
        usage_tracker.record(
            provider=self.name,
            model=request.model,
            usage=LLMUsage(),
            latency_ms=latency_ms,
            stream=False,
            success=False,
            error_category=err.category.value,
        )

    def stream(self, request: LLMRequest) -> StreamingResponse:
        """Streaming completion through the legacy ``Provider.stream_chat``.

        Flattens the structured request to ``(prompt, system_prompt)`` (see
        :meth:`_build_prompt`) and forwards the sampling knobs, translating
        names per backend (``num_predict``/``num_ctx``/``repeat_penalty`` are
        Ollama-only; ``max_tokens`` otherwise). Token usage is *estimated* —
        local backends don't return counts on the streaming path — and recorded
        in the ``finally`` so a failed stream still counts. The per-call HTTP
        bound here comes from ``local_request_timeout_s`` inside the provider's
        own client, NOT :func:`_effective_local_timeout` (that tighter,
        deadline-aware bound is exclusive to the agent tool path).
        """
        provider = self._provider()
        prompt, system_prompt = self._build_prompt(request)

        kwargs: dict = {}
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p
        if request.max_tokens is not None:
            if self.name == "ollama":
                kwargs["num_predict"] = request.max_tokens
            else:
                kwargs["max_tokens"] = request.max_tokens
        ctx = request.metadata.get("num_ctx")
        if ctx is not None and self.name == "ollama":
            kwargs["num_ctx"] = int(ctx)
        repeat_penalty = request.metadata.get("repeat_penalty")
        if repeat_penalty is not None and self.name == "ollama":
            kwargs["repeat_penalty"] = repeat_penalty

        start = time.monotonic()
        try:
            stream_iter = provider.stream_chat(
                model=request.model,
                prompt=prompt,
                system_prompt=system_prompt,
                **kwargs,
            )
        except Exception as exc:
            raise _classify_local_error(exc, provider=self.name, model=request.model)

        final = LLMResponse(provider=self.name, model=request.model)
        buf: list[str] = []

        def _iter() -> Iterator[str]:
            stream_error: LLMError | None = None
            try:
                for chunk in stream_iter:
                    token = self._extract_token(chunk)
                    if token:
                        buf.append(token)
                        yield token
            except Exception as exc:
                stream_error = _classify_local_error(exc, provider=self.name, model=request.model)
                raise stream_error
            finally:
                final.text = "".join(buf)
                final.latency_ms = int((time.monotonic() - start) * 1000)
                final.finish_reason = FinishReason.ERROR if stream_error else FinishReason.STOP
                final.usage = LLMUsage(
                    input_tokens=estimate_tokens(self._concat_prompt(prompt, system_prompt)),
                    output_tokens=estimate_tokens(final.text),
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

    @staticmethod
    def _concat_prompt(prompt: str, system: str) -> str:
        """Join system + user text for input-token estimation only (never sent
        as a single string to the backend)."""
        return f"{system}\n\n{prompt}" if system else prompt

    @staticmethod
    def _extract_token(chunk) -> str:
        """Pull the text delta out of a streaming chunk, format-agnostically.

        Handles all three shapes a local backend can yield: the Ollama SDK's
        ``chunk.message.content``, the OpenAI/LM Studio SDK's
        ``chunk.choices[0].delta.content``, and a bare ``str``. Returns ``""``
        for keep-alive / non-content chunks.
        """
        # Ollama SDK responses
        msg = getattr(chunk, "message", None)
        if msg is not None:
            content = getattr(msg, "content", None)
            if content:
                return content
        # LM Studio / OpenAI SDK responses
        choices = getattr(chunk, "choices", None)
        if choices:
            try:
                delta = choices[0].delta
                content = getattr(delta, "content", None)
                if content:
                    return content
            except (AttributeError, IndexError):
                pass
        if isinstance(chunk, str):
            return chunk
        return ""

    def _build_prompt(self, request: LLMRequest) -> tuple[str, str]:
        """Render messages + retrieved chunks back into the legacy
        ``(prompt, system_prompt)`` shape the local providers accept.

        The local Provider.stream_chat() interface predates the
        structured LLMRequest, so we collapse the messages list back
        into a single user prompt before handing off.
        """
        if not request.messages and request.retrieved_context_chunks:
            from core.llm.prompt import render_context
            context, _ = render_context(request.retrieved_context_chunks)
            prompt = f"<context>\n{context}\n</context>\n\n"
        elif request.messages:
            parts = []
            for msg in request.messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "user":
                    parts.append(content)
                else:
                    parts.append(f"[{role}] {content}")
            prompt = "\n\n".join(parts)
        else:
            prompt = ""
        return prompt, request.system_prompt or ""


def _ollama_attr(obj, key: str):
    """Read ``key`` from an Ollama response, which may be a dict or an object."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _parse_ollama_tool_calls(raw_calls) -> list[ToolCall]:
    """Parse Ollama's ``message.tool_calls`` shape.

    Ollama returns ``{"function": {"name": ..., "arguments": {...}}}``
    per call (arguments already a dict) and does not provide an ID. We
    synthesise an ID and route the rest through the shared OpenAI-style
    parser for consistent malformed-payload handling.
    """
    parsed: list[ToolCall] = []
    for raw in raw_calls:
        function = _ollama_attr(raw, "function")
        if function is None:
            continue
        name = _ollama_attr(function, "name")
        if not isinstance(name, str):
            continue
        args = _ollama_attr(function, "arguments")
        wrapper = {
            "id": _ollama_attr(raw, "id") or f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": name, "arguments": args if args is not None else {}},
        }
        tc = parse_openai_tool_call(wrapper)
        if tc is not None:
            parsed.append(tc)
    return parsed


def _parse_lm_studio_tool_calls(raw_calls) -> list[ToolCall]:
    """Parse LM Studio's OpenAI-shape ``tool_calls`` SDK objects."""
    parsed: list[ToolCall] = []
    for raw in raw_calls:
        if isinstance(raw, dict):
            wrapper = raw
        else:
            function = getattr(raw, "function", None)
            if function is None:
                continue
            wrapper = {
                "id": getattr(raw, "id", "") or "",
                "type": getattr(raw, "type", "function"),
                "function": {
                    "name": getattr(function, "name", "") or "",
                    "arguments": getattr(function, "arguments", "") or "",
                },
            }
        tc = parse_openai_tool_call(wrapper)
        if tc is not None:
            parsed.append(tc)
    return parsed


def _concat_messages(messages: list[dict]) -> str:
    """Rough text concatenation for token estimation when the local
    backend doesn't return usage counts."""
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "content" in block:
                    parts.append(str(block.get("content") or ""))
    return "\n\n".join(parts)
