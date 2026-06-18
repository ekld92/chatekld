"""OpenAI online adapter.

Uses the ``openai`` SDK (already pinned in requirements.txt for the LM
Studio compatibility layer) so we get robust streaming, automatic
retries, and the maintained schema for free. The API key is read from
``OPENAI_API_KEY`` (or the legacy ``OPENAI_KEY``) at call time so the
process picks up a key set after launch without a restart.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Iterator, Optional

from core.llm.base import LLMProvider, StreamingResponse, looks_like_quota
from core.llm.retry import retry_with_backoff
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

CURATED_MODELS: list[str] = [
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

_FINISH_REASONS: dict[str, FinishReason] = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
    "tool_calls": FinishReason.TOOL_USE,
    "function_call": FinishReason.TOOL_USE,
}


class OpenAIProvider(LLMProvider):
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
        self._explicit_key = api_key
        self._explicit_base_url = base_url
        self._organization = organization
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    def _api_key(self) -> str:
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
        return self._explicit_base_url or os.environ.get("OPENAI_BASE_URL")

    def _client(self):
        try:
            import openai
        except ImportError as exc:
            raise LLMError(
                category=ErrorCategory.INVALID_REQUEST,
                message="openai SDK is not installed",
                provider=self.name,
            ) from exc
        client_kwargs = {
            "api_key": self._api_key(),
            "timeout": self.timeout_s,
            "max_retries": 0,  # we handle retries ourselves
        }
        if self._base_url():
            client_kwargs["base_url"] = self._base_url()
        if self._organization:
            client_kwargs["organization"] = self._organization
        return openai.OpenAI(**client_kwargs)

    def supports_embeddings(self) -> bool:
        return False

    def supports_tool_use(self) -> bool:
        return True

    def health_check(self) -> tuple[bool, str]:
        try:
            self._api_key()
            return True, ""
        except LLMError as err:
            return False, err.message

    def list_models(self) -> tuple[list[str], str]:
        return list(CURATED_MODELS), ""

    def _build_messages(self, request: LLMRequest) -> list[dict]:
        return build_openai_messages(request)

    def _common_params(self, request: LLMRequest) -> dict:
        params: dict = {"model": request.model}
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.top_p is not None:
            params["top_p"] = request.top_p
        if request.max_tokens is not None:
            params["max_tokens"] = request.max_tokens
        if request.stop:
            params["stop"] = request.stop
        if request.tools:
            params["tools"] = [jsonschema_to_openai_tool(t) for t in request.tools]
            params["tool_choice"] = request.tool_choice or "auto"
        return params

    def generate(self, request: LLMRequest) -> LLMResponse:
        start = time.monotonic()
        client = self._client()

        def _do_call() -> LLMResponse:
            try:
                resp = client.chat.completions.create(
                    messages=self._build_messages(request),
                    stream=False,
                    **self._common_params(request),
                )
            except Exception as exc:
                raise self._classify_error(exc, request.model)

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

        return retry_with_backoff(_do_call, max_attempts=self.max_retries)

    def stream(self, request: LLMRequest) -> StreamingResponse:
        start = time.monotonic()
        client = self._client()
        final = LLMResponse(provider=self.name, model=request.model)
        buf: list[str] = []

        def _iter() -> Iterator[str]:
            try:
                stream = client.chat.completions.create(
                    messages=self._build_messages(request),
                    stream=True,
                    stream_options={"include_usage": True},
                    **self._common_params(request),
                )
            except Exception as exc:
                raise self._classify_error(exc, request.model)
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
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return 0
    cached = getattr(details, "cached_tokens", None)
    if isinstance(cached, int):
        return cached
    return 0
