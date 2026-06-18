"""Provider-agnostic data types for the LLM layer.

These types are the contract between the route handlers / RAG layer
(which construct an :class:`LLMRequest`) and the provider adapters
(which return an :class:`LLMResponse`). Keeping them dependency-free
makes the adapters trivial to unit-test with mocked HTTP transports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class FinishReason(str, Enum):
    """Normalised completion stop reasons across providers."""

    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    TOOL_USE = "tool_use"
    ERROR = "error"
    OTHER = "other"


class ErrorCategory(str, Enum):
    """Normalised provider error categories used for fallback decisions."""

    TIMEOUT = "timeout"
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    AUTH = "auth"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    QUOTA = "quota"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RetrievedChunk:
    """A single retrieved evidence chunk passed into the LLM request.

    ``source`` is the human-visible file path used for citations; ``score``
    is the relevance score from the last retrieval/rerank stage (may be 0.0
    when unknown). ``metadata`` carries optional extra fields the prompt
    builder may render (e.g. page numbers).
    """

    text: str
    source: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSchema:
    """A tool declaration the model can call during an agent turn.

    ``parameters`` is a JSON Schema object describing the tool's
    arguments. The adapter is responsible for translating this into the
    provider-native shape (OpenAI ``{"type": "function", ...}``,
    Anthropic ``{"input_schema": ...}``, Gemini ``function_declarations``).
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation produced by the model.

    ``id`` is provider-supplied for OpenAI/Anthropic and synthesised by
    the Gemini adapter (Gemini ties responses back by name, not id).
    ``arguments`` is the parsed JSON object; ``raw_arguments`` keeps the
    original JSON string for debugging and provider round-tripping.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""


@dataclass(frozen=True)
class ToolResult:
    """The observation returned to the model after running a tool.

    Tool exceptions are caught by the agent loop and surfaced here with
    ``is_error=True`` so the model can see the failure and recover,
    rather than terminating the whole turn.
    """

    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ToolTurn:
    """One assistant-call-batch plus the matching tool results.

    The agent loop appends a ``ToolTurn`` to
    :attr:`LLMRequest.tool_history` for every completed iteration so the
    adapter can render multi-turn tool-use conversations with correct
    causal ordering (the model in turn N sees the outcomes of turn
    N-1's calls, not all calls collapsed into a single assistant
    message).
    """

    calls: list[ToolCall] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)


@dataclass
class LLMRequest:
    """A provider-agnostic chat completion request.

    The provider adapter is responsible for mapping these fields onto its
    own request shape. ``model`` is required; everything else has a sane
    fallback. ``retrieved_context_chunks`` is rendered by the prompt
    builder into the system / user message, never sent as a separate
    "documents" field.
    """

    model: str
    messages: list[dict[str, str]] = field(default_factory=list)
    system_prompt: str = ""
    retrieved_context_chunks: list[RetrievedChunk] = field(default_factory=list)

    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Optional[list[str]] = None

    timeout_s: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    tools: list[ToolSchema] = field(default_factory=list)
    tool_choice: Optional[str] = None
    tool_history: list[ToolTurn] = field(default_factory=list)


@dataclass
class LLMUsage:
    """Token counts and estimated cost for a single request."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    estimated_cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class LLMResponse:
    """A non-streaming completion result.

    For streaming, the adapter returns an iterator that yields token
    strings and accumulates the final :class:`LLMResponse` on its
    ``response`` attribute once exhausted; see
    :class:`core.llm.base.StreamingResponse`.
    """

    text: str = ""
    provider: str = ""
    model: str = ""
    finish_reason: FinishReason = FinishReason.STOP
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: int = 0
    raw: Optional[Any] = None
    error: Optional["LLMError"] = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class LLMError(Exception):
    """A normalised provider error.

    Adapters always raise (or attach via :class:`LLMResponse.error`) an
    ``LLMError`` rather than provider-specific exceptions, so the
    fallback policy and the route handlers can pattern-match on
    :attr:`category` alone.
    """

    category: ErrorCategory
    message: str
    provider: str = ""
    model: str = ""
    status_code: Optional[int] = None
    retryable: bool = False

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def __str__(self) -> str:
        bits = [self.category.value]
        if self.provider:
            bits.append(self.provider)
        if self.status_code is not None:
            bits.append(f"http={self.status_code}")
        return f"{' '.join(bits)}: {self.message}"
