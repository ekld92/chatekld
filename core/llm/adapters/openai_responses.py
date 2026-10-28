"""Targeted Responses transport for GPT-6.1 Sol tool turns.

Reuse the existing request, tool-history, retry, error and usage contracts.
Stateless replay includes encrypted reasoning, without server-side storage.
"""

from __future__ import annotations
import json
import time
from core.llm.base import StreamingResponse, looks_like_quota
from core.llm.retry import parse_retry_after_s, retry_with_backoff
from core.llm.types import (
    ErrorCategory,
    FinishReason,
    LLMError,
    LLMResponse,
    LLMUsage,
    ToolCall,
)
from core.llm.usage import CANCELLED_ERROR_CATEGORY, estimate_tokens, usage_tracker


def applies(provider, request):
    return (
        provider.name == "openai"
        and (provider._base_url() or "https://api.openai.com/v1").rstrip("/")
        == "https://api.openai.com/v1"
        and request.model == "gpt-6.1-sol"
        and bool(request.tools or request.tool_history)
    )


def payload(provider, request):
    effort = request.reasoning_effort or "medium"
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise LLMError(
            ErrorCategory.INVALID_REQUEST,
            "GPT-6.1 Sol reasoning effort must be low, medium, high, xhigh or max",
            provider=provider.name,
            model=request.model,
        )
    if request.stop:
        raise LLMError(
            ErrorCategory.INVALID_REQUEST,
            "Stop sequences are unsupported on the Responses tool transport",
            provider=provider.name,
            model=request.model,
        )
    items = []
    if request.system_prompt:
        items.append({"role": "system", "content": request.system_prompt})
    items.extend(
        {"role": m.get("role", "user"), "content": m["content"]}
        for m in request.messages
        if m.get("content")
    )
    for turn in request.tool_history:
        state = turn.provider_state
        if (
            state.get("provider") == provider.name
            and state.get("model") == request.model
        ):
            items.extend(state.get("output", []))
        elif turn.calls:
            # Do not fabricate reasoning continuity on a cross-provider history.
            raise LLMError(
                ErrorCategory.INVALID_REQUEST,
                "Responses tool history requires matching OpenAI model state",
                provider=provider.name,
                model=request.model,
            )
        items.extend(
            {
                "type": "function_call_output",
                "call_id": r.tool_call_id,
                "output": r.content,
            }
            for r in turn.results
        )
    result = {
        "model": request.model,
        "input": items,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": effort},
    }
    if request.max_tokens is not None:
        result["max_output_tokens"] = request.max_tokens
    if request.tools:
        result["tools"] = [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "strict": False,
            }
            for t in request.tools
        ]
        result["tool_choice"] = request.tool_choice or "auto"
    return result


def response_error(provider, request, error):
    """Classify structured Responses errors without inventing HTTP statuses."""
    def field(name):
        return error.get(name) if isinstance(error, dict) else getattr(error, name, None)

    code = field("code") or ""
    message = field("message") or str(error)
    categories = {
        "invalid_prompt": ErrorCategory.INVALID_REQUEST,
        "invalid_request_error": ErrorCategory.INVALID_REQUEST,
        "context_length_exceeded": ErrorCategory.INVALID_REQUEST,
        "invalid_api_key": ErrorCategory.AUTH,
        "authentication_error": ErrorCategory.AUTH,
        "rate_limit_exceeded": ErrorCategory.RATE_LIMIT,
        "slow_down": ErrorCategory.RATE_LIMIT,
        "server_is_overloaded": ErrorCategory.SERVER_ERROR,
        "server_error": ErrorCategory.SERVER_ERROR,
        "credit_balance_exhausted": ErrorCategory.QUOTA,
        "vector_store_timeout": ErrorCategory.TIMEOUT,
    }
    category = (ErrorCategory.QUOTA if looks_like_quota(message, code)
                else categories.get(code, ErrorCategory.UNKNOWN))
    return LLMError(category, f"{code}: {message}" if code else message,
                    provider=provider.name, model=request.model,
                    retry_after_s=parse_retry_after_s(message)
                    if category == ErrorCategory.RATE_LIMIT else None)


def decode(provider, request, response):
    output = [
        o.model_dump(exclude_none=True) if hasattr(o, "model_dump") else o
        for o in response.output
    ]
    calls = []
    texts = []
    refusal = False
    malformed = False
    for item in output:
        if item["type"] == "function_call":
            raw = item.get("arguments", "")
            try:
                args = json.loads(raw)
            except (TypeError, ValueError):
                args = None
            if (isinstance(args, dict) and isinstance(item.get("call_id"), str)
                    and item["call_id"] and isinstance(item.get("name"), str)):
                calls.append(
                    ToolCall(
                        id=item["call_id"],
                        name=item["name"],
                        arguments=args,
                        raw_arguments=raw,
                    )
                )
            else:
                malformed = True
        if item["type"] == "message":
            for content in item.get("content", []):
                if content["type"] == "output_text":
                    texts.append(content["text"])
                elif content["type"] == "refusal":
                    refusal = True
                    texts.append(content.get("refusal", ""))
    reason = FinishReason.TOOL_USE if calls else FinishReason.STOP
    if response.status == "incomplete":
        detail = getattr(response.incomplete_details, "reason", "")
        reason = (
            FinishReason.LENGTH
            if detail == "max_output_tokens"
            else FinishReason.CONTENT_FILTER
        )
    elif response.status == "failed":
        reason = FinishReason.ERROR
    elif refusal:
        reason = FinishReason.CONTENT_FILTER
    error = None
    if response.status == "failed":
        error = response_error(provider, request, response.error)
    elif malformed:
        # Reject the entire batch before any tool runs. Replaying only its
        # parseable subset would leave unmatched calls in encrypted continuity.
        error = LLMError(ErrorCategory.INVALID_REQUEST,
                         "Responses returned a malformed function-call batch",
                         provider=provider.name, model=request.model)
        reason = FinishReason.ERROR
    u = response.usage
    usage = LLMUsage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(getattr(u, "input_tokens_details", None), "cache_write_tokens", 0) or 0,
        cached_input_tokens=getattr(
            getattr(u, "input_tokens_details", None), "cached_tokens", 0
        )
        or 0,
    )
    return LLMResponse(
        text="".join(texts),
        provider=provider.name,
        model=request.model,
        finish_reason=reason,
        usage=usage,
        tool_calls=[] if error else calls,
        error=error,
        provider_state={
            "provider": provider.name,
            "model": request.model,
            "output": output,
        },
    )


def record(provider, request, final, start, *, stream, cancelled=False):
    final.latency_ms = int((time.monotonic() - start) * 1000)
    usage_tracker.record(
        provider=provider.name,
        model=request.model,
        workflow=request.workflow,
        usage=final.usage,
        latency_ms=final.latency_ms,
        stream=stream,
        success=final.error is None,
        billable=bool(final.usage.total_tokens),
        error_category=final.error.category.value
        if final.error
        else CANCELLED_ERROR_CATEGORY
        if cancelled
        else "",
    )


def generate(provider, request):
    params = payload(provider, request)
    client = provider._client()
    start = time.monotonic()

    def call():
        attempt_start = time.monotonic()
        final = LLMResponse(provider=provider.name, model=request.model)
        try:
            final = decode(provider, request, client.responses.create(**params))
            if final.error:
                raise final.error
            return final
        except Exception as exc:
            final.error = provider._classify_error(exc, request.model)
            raise final.error from exc
        finally:
            record(provider, request, final, attempt_start, stream=False)

    return retry_with_backoff(
        call,
        max_attempts=provider.max_retries,
        deadline_monotonic_s=start + request.timeout_s if request.timeout_s else None,
        should_cancel=(request.metadata or {}).get("should_cancel"),
    )


def stream(provider, request):
    params = payload(provider, request)
    client = provider._client()
    start = time.monotonic()
    final = LLMResponse(provider=provider.name, model=request.model)

    def iterate():
        handle = None
        buf = []
        cancelled = False
        terminal = False
        try:
            handle = client.responses.create(**params, stream=True)
            for event in handle:
                if (request.metadata or {}).get("should_cancel", lambda: False)():
                    cancelled = True
                    break
                if event.type == "response.output_text.delta":
                    buf.append(event.delta)
                    yield event.delta
                elif event.type in {
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                }:
                    decoded = decode(provider, request, event.response)
                    final.__dict__.update(decoded.__dict__)
                    terminal = True
                    if final.error and not final.text:
                        final.text = "".join(buf)
                    if final.error:
                        raise final.error
                elif event.type == "error":
                    raise response_error(provider, request, event)
            if not terminal and not cancelled:
                raise LLMError(
                    ErrorCategory.NETWORK,
                    "Responses stream ended without terminal event",
                    provider=provider.name,
                    model=request.model,
                )
        except GeneratorExit:
            cancelled = True
            raise
        except Exception as exc:
            final.error = provider._classify_error(exc, request.model)
            final.finish_reason = FinishReason.ERROR
            raise final.error from exc
        finally:
            if not terminal:
                final.text = "".join(buf)
                final.usage.output_tokens = estimate_tokens(final.text)
            try:
                if handle is not None:
                    handle.close()
            finally:
                record(provider, request, final, start, stream=True, cancelled=cancelled)

    return StreamingResponse(response_gen=iterate(), final=final)
