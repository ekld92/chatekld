import ollama
import logging
import threading
from typing import Any, Optional
from core.providers.base import Provider, local_request_timeout
from core.constants import OLLAMA_HOST

logger = logging.getLogger(__name__)

# Cache ollama.Client instances by (host, timeout). Each Client owns an httpx
# connection pool; creating one per generation call (the agent loop runs several
# per turn) would churn pools that only close on GC. get_provider() returns a
# FRESH OllamaProvider each call, so the cache must live at module scope rather
# than on the instance. httpx.Client is safe for concurrent use, so sharing one
# across the SSE worker threads is fine. The key also carries the timeout, so a
# change to local_request_timeout_s yields a new client (the old one lingers
# idle, but the key space is tiny — one host, a handful of timeout values).
_client_cache: "dict[tuple, ollama.Client]" = {}
_client_cache_lock = threading.Lock()

# Sentinel for ``OllamaProvider._client(timeout=...)``: distinguishes "caller
# passed no timeout, use the configured local_request_timeout_s" from an
# explicit ``None`` ("leave the SDK default / unbounded").
_USE_CONFIG_TIMEOUT = object()


def _ollama_client(host: str, timeout: Optional[float]) -> "ollama.Client":
    """Return a cached ``ollama.Client`` for *(host, timeout)*, creating it once.

    ``timeout is None`` means "leave the SDK default" — we omit the kwarg
    entirely rather than passing None so the library's own default applies.

    A non-None timeout is rounded to whole seconds (and floored at 1 s) before
    keying/constructing: the agent loop passes the turn's remaining wall-clock
    budget — a near-continuous float (297.4, 296.1, …) — so an unrounded key
    would mint a fresh ``ollama.Client`` (each owning an httpx pool) on every
    call and never evict it. Rounding collapses the keyspace back to the
    "handful of values" the cache is designed for. The 1 s floor stops a
    sub-second value — a near-exhausted agent budget, or a hand-set sub-second
    ``local_request_timeout_s`` — from rounding to 0.0, which httpx reads as
    "time out immediately" and would fail the call outright; sub-second
    precision is irrelevant to a wedged-backend bound anyway.
    """
    if timeout is not None:
        timeout = float(max(1, round(timeout)))
    key = (host, timeout)
    with _client_cache_lock:
        client = _client_cache.get(key)
        if client is None:
            client = (
                ollama.Client(host=host)
                if timeout is None
                else ollama.Client(host=host, timeout=timeout)
            )
            _client_cache[key] = client
        return client


class OllamaProvider(Provider):
    """Local :class:`Provider` over an Ollama server.

    Owns three responsibilities the rest of the layer relies on: resolving bare
    model names to the closest installed tag (:meth:`resolve_model` — Ollama
    requires the exact ``name:tag``), handing out LlamaIndex LLM / embedding
    objects for the indexer and engine, and the legacy ``stream_chat`` path the
    local LLM adapter flattens onto. HTTP transport is shared through the
    module-level ``(host, timeout)``-keyed client cache (see
    :func:`_ollama_client`); health/list calls intentionally use the module-level
    ``ollama.*`` default client instead.
    """

    def __init__(self, host: str = OLLAMA_HOST):
        self.host = host
        # Configure ollama client if needed, but it usually uses env OLLAMA_HOST
        # or defaults to localhost:11434

    def _client(self, timeout: Any = _USE_CONFIG_TIMEOUT):
        """A cached ``ollama.Client`` for our host carrying a per-call timeout
        (forwarded to httpx). Reused across calls so generation doesn't churn
        httpx connection pools; health/list calls keep using the module-level
        ``ollama.*`` default client.

        ``timeout`` defaults to the configured ``local_request_timeout_s``. The
        agent tool-call path passes an explicit value (the turn's remaining
        wall-clock budget) so a wedged backend cannot outlive the deadline;
        passing ``None`` explicitly leaves the SDK default (unbounded)."""
        if timeout is _USE_CONFIG_TIMEOUT:
            timeout = local_request_timeout()
        return _ollama_client(self.host, timeout)

    def check_running(self) -> tuple[bool, str]:
        """Reachability probe: ``(True, "")`` if ``ollama.list()`` succeeds,
        else ``(False, error)``."""
        try:
            ollama.list()
            return True, ""
        except Exception as e:
            return False, str(e)

    def get_models(self) -> tuple[list[str], str]:
        """List installed model tags, or ``([], error)`` if unreachable."""
        try:
            resp = ollama.list()
            return [m.model for m in resp.models], ""
        except Exception as e:
            return [], str(e)

    def resolve_model(self, model: str) -> str:
        """Resolve a model name to the closest installed tag for Ollama."""
        try:
            models, _err = self.get_models()
            if not models or model in models:
                return model
            
            # Base-name match
            base_model = model.split(":")[0]
            for m in models:
                if m.split(":")[0] == base_model:
                    return m
        except Exception:
            pass
        return model

    def get_llm(self, model_name: str, **kwargs) -> Any:
        from llama_index.llms.ollama import Ollama
        resolved = self.resolve_model(model_name)
        # Override LlamaIndex's 30 s default only when the user set a positive
        # local_request_timeout_s; 0 leaves the library default untouched.
        timeout = local_request_timeout()
        if timeout is not None and "request_timeout" not in kwargs:
            kwargs["request_timeout"] = timeout
        return Ollama(model=resolved, base_url=self.host, **kwargs)

    def get_embedding(self, model_name: str, **kwargs) -> Any:
        """Return a LlamaIndex ``OllamaEmbedding`` for the indexer.

        Note no ``local_request_timeout_s`` is applied — embeddings are
        deliberately excluded from that bound, since timing out an indexing batch
        would cause spurious failures rather than recovery.
        """
        from llama_index.embeddings.ollama import OllamaEmbedding
        resolved = self.resolve_model(model_name)
        return OllamaEmbedding(model_name=resolved, base_url=self.host, **kwargs)

    def stream_chat(self, model: str, prompt: str, system_prompt: Optional[str] = None,
                    request_timeout: Optional[float] = None, **kwargs) -> Any:
        """Stream a chat completion via the cached client (legacy local path).

        Collapses the (optional) system prompt + user prompt into a two-message
        conversation and folds the recognised sampling kwargs
        (``temperature``/``top_p``/``num_ctx``/``num_predict``/``repeat_penalty``,
        plus ``max_tokens`` → ``num_predict``) into Ollama's ``options`` dict.
        Returns the raw streaming iterator for the caller to drain.

        ``request_timeout`` (when not None) overrides the configured
        ``local_request_timeout_s`` for this call only — the single-paper route
        passes a non-zero floor so a wedged backend can't hang its guard-less
        synchronous SSE stream; callers that omit it keep the configured bound.
        """
        resolved = self.resolve_model(model)
        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': prompt})

        option_keys = {
            "temperature",
            "top_p",
            "num_ctx",
            "num_predict",
            "repeat_penalty",
        }
        options = kwargs.pop("options", {}) or {}
        for key in list(kwargs):
            if key in option_keys:
                options[key] = kwargs.pop(key)
        if "max_tokens" in kwargs and "num_predict" not in options:
            options["num_predict"] = kwargs.pop("max_tokens")
        
        if options:
            kwargs["options"] = options
        client = (
            self._client(timeout=request_timeout)
            if request_timeout is not None
            else self._client()
        )
        return client.chat(model=resolved, messages=messages, stream=True, **kwargs)
