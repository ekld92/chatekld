import ollama
import logging
import threading
from collections import OrderedDict
from typing import Any, Optional
from core.providers.base import Provider, local_request_timeout, resolve_ollama_host

logger = logging.getLogger(__name__)

# Cache ollama.Client instances by (host, timeout). Each Client owns an httpx
# connection pool; creating one per generation call (the agent loop runs several
# per turn) would churn pools that only close on GC. get_provider() returns a
# FRESH OllamaProvider each call, so the cache must live at module scope rather
# than on the instance. httpx.Client is safe for concurrent use, so sharing one
# across the SSE worker threads is fine. The key also carries the timeout, so a
# change to local_request_timeout_s yields a new client. Item 2.6
# (improvement plan 2026-07-04): the old "key space is tiny" claim was wrong —
# the agent loop keys by its per-iteration REMAINING budget (rounded to whole
# seconds), i.e. up to ~agent_wall_clock_s distinct integer timeouts per host,
# each pinning an idle httpx pool forever. The cache is therefore a small LRU
# (_CLIENT_CACHE_MAX entries): the steady-state working set is genuinely tiny
# (configured timeout + a few agent buckets — see loop.py's quantisation), so
# eviction is rare, and an evicted client is deliberately NOT closed here — a
# concurrent call may still be streaming on it; dropping the reference lets it
# close via GC once the in-flight request finishes. Invariant (pinned by
# TestClientCacheBound): the cache never exceeds _CLIENT_CACHE_MAX entries.
_CLIENT_CACHE_MAX = 8
_client_cache: "OrderedDict[tuple, ollama.Client]" = OrderedDict()
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
    call and never evict it. Rounding collapses the keyspace (and loop.py's
    bucket quantisation collapses it further); the LRU bound above is the
    backstop for whatever keys still arrive. The 1 s floor stops a
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
            while len(_client_cache) > _CLIENT_CACHE_MAX:
                _client_cache.popitem(last=False)   # evict LRU; GC closes it
        else:
            _client_cache.move_to_end(key)
        return client


class OllamaProvider(Provider):
    """Local :class:`Provider` over an Ollama server.

    Owns three responsibilities the rest of the layer relies on: resolving bare
    model names to the closest installed tag (:meth:`resolve_model` — Ollama
    requires the exact ``name:tag``), handing out LlamaIndex LLM / embedding
    objects for the indexer and engine, and the legacy ``stream_chat`` path the
    local LLM adapter flattens onto. HTTP transport is shared through the
    module-level ``(host, timeout)``-keyed client cache (see
    :func:`_ollama_client`); health/list calls go through that same host-bound
    client (``self._client()``) so the reachability probe and generation can
    never target different endpoints (the OLLAMA_HOST split-brain fix).
    """

    def __init__(self, host: Optional[str] = None):
        # Resolve at CONSTRUCTION time (not as a default-arg value, which would
        # bind once at import). get_provider() mints a fresh provider per call,
        # so the host re-resolves from config/env on each request — no restart
        # needed. host=config(ollama_host) → env(OLLAMA_HOST) → constant.
        self.host = host if host is not None else resolve_ollama_host()

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
        """Reachability probe against OUR resolved host: ``(True, "")`` on success.

        Uses ``self._client().list()`` (host-bound), NOT the module-level
        ``ollama.list()`` — the latter resolves ``OLLAMA_HOST`` independently and
        produced the split-brain where the status badge probed one endpoint while
        generation/embedding used another. Now the probe and generation share one
        host, so a green badge means generation will actually reach a server.
        """
        try:
            self._client().list()
            return True, ""
        except Exception as e:
            return False, str(e)

    def get_models(self) -> tuple[list[str], str]:
        """List installed model tags from OUR resolved host, or ``([], error)``.

        Host-bound via ``self._client()`` for the same reason as
        :meth:`check_running` — see that method's note on the split-brain fix.
        """
        try:
            resp = self._client().list()
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

    def get_embedding(self, model_name: str, *, request_timeout_s: Optional[float] = None, **kwargs) -> Any:
        """Return a LlamaIndex ``OllamaEmbedding``.

        Note no ``local_request_timeout_s`` is applied — embeddings are
        deliberately excluded from that bound, since timing out an indexing batch
        would cause spurious failures rather than recovery.

        ``request_timeout_s`` (improvement plan 2026-07-04, item 2.1) is the
        QUERY-path exception to that rule: retrieval embeds the user's query
        while holding the index mutation lock, so one wedged embed HTTP call
        used to strand every subsequent chat worker on the lock (restart-only
        recovery). Callers embedding a single query pass a bound (the engine
        passes ``QUERY_EMBED_TIMEOUT_S``); the INDEXING path omits it and stays
        unbounded — the two contracts differ on purpose, so never default this.
        """
        from llama_index.embeddings.ollama import OllamaEmbedding
        resolved = self.resolve_model(model_name)
        if request_timeout_s is not None and request_timeout_s > 0:
            # client_kwargs reaches ollama.Client(host=..., **client_kwargs) —
            # the underlying httpx timeout, bounding each embed HTTP call.
            kwargs = {**kwargs, "client_kwargs": {"timeout": float(request_timeout_s)}}
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
