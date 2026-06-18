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


def _ollama_client(host: str, timeout: Optional[float]) -> "ollama.Client":
    """Return a cached ``ollama.Client`` for *(host, timeout)*, creating it once.

    ``timeout is None`` means "leave the SDK default" — we omit the kwarg
    entirely rather than passing None so the library's own default applies.
    """
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
    def __init__(self, host: str = OLLAMA_HOST):
        self.host = host
        # Configure ollama client if needed, but it usually uses env OLLAMA_HOST
        # or defaults to localhost:11434

    def _client(self):
        """A cached ``ollama.Client`` for our host carrying the configured
        per-call timeout (forwarded to httpx). Reused across calls so generation
        doesn't churn httpx connection pools; health/list calls keep using the
        module-level ``ollama.*`` default client."""
        return _ollama_client(self.host, local_request_timeout())

    def check_running(self) -> tuple[bool, str]:
        try:
            ollama.list()
            return True, ""
        except Exception as e:
            return False, str(e)

    def get_models(self) -> tuple[list[str], str]:
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
        from llama_index.embeddings.ollama import OllamaEmbedding
        resolved = self.resolve_model(model_name)
        return OllamaEmbedding(model_name=resolved, base_url=self.host, **kwargs)

    def stream_chat(self, model: str, prompt: str, system_prompt: Optional[str] = None, **kwargs) -> Any:
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
        return self._client().chat(model=resolved, messages=messages, stream=True, **kwargs)
