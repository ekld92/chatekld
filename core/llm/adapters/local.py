"""LLM adapter that proxies to the existing local providers."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Iterator

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


class LocalLLMProvider(LLMProvider):
    """Thin adapter around the existing :class:`core.providers.base.Provider`.

    The local Provider abstraction predates :class:`LLMProvider`; this
    adapter wraps it so the same RAG / summariser code paths can flow
    through both local and online providers without branching on the
    provider name at every call site.
    """

    def __init__(self, name: str) -> None:
        if name not in ("ollama", "lm_studio"):
            raise LLMError(
                category=ErrorCategory.INVALID_REQUEST,
                message=f"local provider must be ollama or lm_studio, got {name!r}",
                provider=name,
            )
        self.name = name

    def _provider(self):
        from core.providers import get_provider
        return get_provider(self.name)

    def supports_embeddings(self) -> bool:
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
        try:
            return self._provider().get_models()
        except Exception as exc:
            return [], str(exc)

    def health_check(self) -> tuple[bool, str]:
        try:
            return self._provider().check_running()
        except Exception as exc:
            return False, str(exc)

    def generate(self, request: LLMRequest) -> LLMResponse:
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
                err = coerce_error(exc, provider=self.name, model=request.model)
                self._record_tool_failure(request, start, err)
                raise err
        else:
            try:
                response = self._lm_studio_chat_with_tools(
                    request, messages, tools_payload,
                )
            except Exception as exc:
                err = coerce_error(exc, provider=self.name, model=request.model)
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
        # Route through the provider's client so the configured
        # local_request_timeout_s bounds this (non-streaming) tool call.
        raw = provider._client().chat(**call_kwargs)

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
        # Bound this (non-streaming) tool call with the configured local timeout
        # when set (>0); 0 leaves the OpenAI SDK default. Mirrors the Ollama tool
        # branch above, which routes through OllamaProvider._client().
        from core.providers.base import local_request_timeout
        timeout = local_request_timeout()
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
            raise coerce_error(exc, provider=self.name, model=request.model)

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
                stream_error = coerce_error(exc, provider=self.name, model=request.model)
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
        return f"{system}\n\n{prompt}" if system else prompt

    @staticmethod
    def _extract_token(chunk) -> str:
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
