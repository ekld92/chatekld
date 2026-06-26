from abc import ABC, abstractmethod
from typing import Any, Optional


def local_request_timeout() -> Optional[float]:
    """Per-call HTTP timeout for the LOCAL providers, from the
    ``local_request_timeout_s`` config key. Returns ``None`` when 0/unset so
    each call site leaves its own SDK/library default untouched.

    Read per call (not cached) so the Settings knob takes effect on the next
    request without an app restart. Lazy config import keeps this module free
    of an import cycle (``core.config`` does not import providers).
    """
    try:
        from core.config import load_config
        t = float(load_config().get("local_request_timeout_s", 0) or 0)
    except Exception:
        t = 0.0
    return t if t > 0 else None


class Provider(ABC):
    """Abstract base for the LOCAL backends (Ollama, LM Studio).

    Predates the provider-agnostic :class:`core.llm.base.LLMProvider`; this
    interface stays focused on what the embedding / indexing code and the legacy
    local-chat path need: a reachability probe, model listing, LlamaIndex LLM +
    embedding factories, and a streaming chat call. Online chat providers do NOT
    implement this (they have no embedding interface) — see
    :func:`core.providers.get_provider` for how an online name is transparently
    substituted with a local embed provider.
    """

    @abstractmethod
    def check_running(self) -> tuple[bool, str]:
        """Check if the provider service is running."""
        ...

    @abstractmethod
    def get_models(self) -> tuple[list[str], str]:
        """List available models."""
        ...

    @abstractmethod
    def get_llm(self, model_name: str, **kwargs) -> Any:
        """Return a LlamaIndex-compatible LLM object."""
        ...

    @abstractmethod
    def get_embedding(self, model_name: str, **kwargs) -> Any:
        """Return a LlamaIndex-compatible Embedding object."""
        ...

    @abstractmethod
    def stream_chat(self, model: str, prompt: str, system_prompt: Optional[str] = None, **kwargs) -> Any:
        """Stream a chat completion."""
        ...
