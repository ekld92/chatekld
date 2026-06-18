"""Abstract LLMProvider interface plus the streaming wrapper."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from core.llm.types import (
    ErrorCategory,
    LLMError,
    LLMRequest,
    LLMResponse,
)


@dataclass
class StreamingResponse:
    """Iterator-like wrapper returned by :meth:`LLMProvider.stream`.

    The consumer iterates over :attr:`response_gen` to receive token
    strings; once exhausted the adapter populates :attr:`final` with the
    completed :class:`LLMResponse` (including usage). ``finish_reason``
    and ``usage`` are kept synchronised between the underlying generator
    and ``final`` so consumers can inspect them after iteration.

    Compatible with the existing vault chat consumer in
    ``api/routes/vault.py`` which checks ``hasattr(response, "response_gen")``.
    """

    response_gen: Iterator[str]
    final: LLMResponse = field(default_factory=LLMResponse)
    cancel_cb: Optional[Callable[[], None]] = None

    def cancel(self) -> None:
        """Best-effort cancel signal — closes the underlying generator."""
        if self.cancel_cb is not None:
            try:
                self.cancel_cb()
            except Exception:
                pass
        gen = self.response_gen
        close = getattr(gen, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


class LLMProvider(ABC):
    """Minimal interface every chat provider implements."""

    name: str = ""

    @abstractmethod
    def list_models(self) -> tuple[list[str], str]:
        """Return ``(model_ids, error_msg)``.

        Online adapters typically return a curated list of well-known
        model IDs because most provider model-listing endpoints either
        require auth or return hundreds of internal aliases. Local
        adapters proxy through to the existing Provider.get_models()
        path.
        """

    @abstractmethod
    def health_check(self) -> tuple[bool, str]:
        """Return ``(ok, error)``.

        For local providers this probes the local server. For online
        providers it confirms an API key is configured and (optionally)
        performs a cheap reachability check.
        """

    @abstractmethod
    def generate(self, request: LLMRequest) -> LLMResponse:
        """One-shot completion."""

    @abstractmethod
    def stream(self, request: LLMRequest) -> StreamingResponse:
        """Stream tokens. The returned object exposes a string iterator
        on ``response_gen`` and (once iteration completes) a populated
        :class:`LLMResponse` on ``final``.
        """

    def supports_embeddings(self) -> bool:
        """Online adapters override this to ``False``.

        The RAG indexer consults this when deciding whether to fall back
        to a local embedding provider.
        """
        return False

    def supports_tool_use(self) -> bool:
        """Adapter declares whether it can route structured tool-use.

        Defaults to ``False`` so a new adapter cannot accidentally claim
        support. The agent loop consults this before entering agent
        mode; for local providers the adapter typically returns
        ``True`` and the model-level capability is detected at runtime
        through the malformed-call counter.
        """
        return False


def coerce_error(
    exc: BaseException,
    *,
    provider: str,
    model: str = "",
    default_category: ErrorCategory = ErrorCategory.UNKNOWN,
) -> LLMError:
    """Wrap an arbitrary exception in an :class:`LLMError`.

    Adapters that catch unexpected non-LLMError exceptions should pass
    them through this helper so the fallback policy sees a consistent
    category.
    """
    if isinstance(exc, LLMError):
        return exc
    return LLMError(
        category=default_category,
        message=str(exc) or exc.__class__.__name__,
        provider=provider,
        model=model,
        retryable=False,
    )


_QUOTA_SIGNALS = (
    "insufficient_quota",
    "exceeded your current quota",
    "credit balance",
    "billing account",
    "billing is not enabled",
)


def looks_like_quota(message: str, code: Optional[str] = None) -> bool:
    """Heuristic: does *message*/*code* indicate hard quota or billing
    exhaustion rather than a transient rate-limit?

    Quota/billing errors must surface immediately — they are non-retryable
    and excluded from the default fallback set — whereas a per-minute
    rate-limit should keep retrying.  The signals are kept deliberately
    specific (OpenAI's ``insufficient_quota``, Anthropic's "credit balance
    is too low") so a Google per-minute "Quota exceeded for quota metric …"
    rate-limit is NOT misclassified as terminal.
    """
    if code and "insufficient_quota" in str(code).lower():
        return True
    low = (message or "").lower()
    return any(sig in low for sig in _QUOTA_SIGNALS)
