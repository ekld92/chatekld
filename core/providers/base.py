import os
from abc import ABC, abstractmethod
from typing import Any, Optional

from core.constants import LM_STUDIO_HOST, OLLAMA_HOST


def _resolve_local_host(config_key: str, env_var: str, default: str) -> str:
    """Resolve a local-backend base URL: config key → env var → constant.

    Read per call (not cached, lazy config import) so a Settings change applies
    on the next request without a restart — exactly like ``local_request_timeout``.
    A scheme-less value (``host:port``, the shape Ollama's own ``OLLAMA_HOST``
    accepts) is prefixed with ``http://`` so every consumer — the bare
    ``ollama.Client`` AND the ``f"{host}/v1"`` LM Studio base URL — gets a
    well-formed URL; any trailing slash is stripped for the same reason.

    This is the single source of truth that closes the split-brain where the
    health/list probe honoured ``OLLAMA_HOST`` but generation/embedding silently
    used the hardcoded constant.
    """
    val = ""
    try:
        from core.config import load_config
        val = str(load_config().get(config_key, "") or "").strip()
    except Exception:
        val = ""
    if not val:
        val = os.environ.get(env_var, "").strip()
    if not val:
        return default
    if "://" not in val:
        val = "http://" + val
    return val.rstrip("/")


def resolve_ollama_host() -> str:
    """Base URL for Ollama: ``ollama_host`` config → ``OLLAMA_HOST`` env → constant."""
    return _resolve_local_host("ollama_host", "OLLAMA_HOST", OLLAMA_HOST)


def resolve_lm_studio_host() -> str:
    """Base URL for LM Studio: ``lm_studio_host`` config → ``LM_STUDIO_HOST`` env → constant."""
    return _resolve_local_host("lm_studio_host", "LM_STUDIO_HOST", LM_STUDIO_HOST)


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
