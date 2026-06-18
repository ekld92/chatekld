"""Provider-agnostic LLM layer for ChatEKLD.

This package exposes a small, uniform LLM interface that wraps both the
existing local providers (Ollama, LM Studio) and the new online providers
(OpenAI, Anthropic, Google Gemini). Embeddings are deliberately NOT covered
here — they stay local and continue to go through ``core.providers``.

Public surface:

* :class:`core.llm.types.LLMRequest` / :class:`core.llm.types.LLMResponse`
* :class:`core.llm.base.LLMProvider`
* :func:`core.llm.factory.get_llm_provider`
* :func:`core.llm.factory.resolve_chat_provider` (applies fallback policy)
* :data:`core.llm.usage.usage_tracker`

The rest of the modules are internal but stable for tests.
"""
from core.llm.types import (
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    RetrievedChunk,
    FinishReason,
    ErrorCategory,
    ToolSchema,
    ToolCall,
    ToolResult,
    ToolTurn,
)
from core.llm.base import LLMProvider
from core.llm.factory import get_llm_provider, resolve_chat_provider
from core.llm.usage import usage_tracker

__all__ = [
    "LLMError",
    "LLMRequest",
    "LLMResponse",
    "LLMUsage",
    "RetrievedChunk",
    "FinishReason",
    "ErrorCategory",
    "ToolSchema",
    "ToolCall",
    "ToolResult",
    "ToolTurn",
    "LLMProvider",
    "get_llm_provider",
    "resolve_chat_provider",
    "usage_tracker",
]
