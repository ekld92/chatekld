import functools
import json
import logging
import tiktoken
import urllib.request
from typing import Any, Optional
from core.providers.base import Provider, local_request_timeout
from core.constants import LM_STUDIO_HOST

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _cl100k_encoding():
    """Memoized ``cl100k_base`` encoder.

    ``tiktoken.get_encoding`` rebuilds the encoder (parses the BPE merge table)
    on every call; LlamaIndex touches ``_tokenizer`` per request (and possibly
    per chunk during streaming), so cache the process-global, immutable encoder
    once instead of reconstructing it on the hot LM Studio path. (Duplicated in
    ``core/llm/usage.py`` deliberately — importing across core.providers ↔
    core.llm would risk a circular import.)
    """
    return tiktoken.get_encoding("cl100k_base")

try:
    from llama_index.llms.openai import OpenAI as _LlamaOpenAI
    from llama_index.embeddings.openai import OpenAIEmbedding as _LlamaOpenAIEmbedding
    from llama_index.core.llms import LLMMetadata
    _LLAMAINDEX_OPENAI_AVAILABLE = True

    class _LMStudioOpenAI(_LlamaOpenAI):
        """Thin subclass of LlamaIndex's OpenAI LLM wrapper for LM Studio."""
        @property
        def _tokenizer(self):
            return _cl100k_encoding()

        @property
        def metadata(self) -> LLMMetadata:
            # LM Studio serves all instruct/chat models through /v1/chat/completions.
            # The upstream is_chat_model() helper only recognises known OpenAI model
            # names, so arbitrary LM Studio IDs (e.g. "google/gemma-4-e4b") get
            # misrouted to the legacy /v1/completions endpoint, which returns an
            # empty stream and surfaces as a spurious "No relevant content found".
            return LLMMetadata(
                context_window=getattr(self, "_lmstudio_context_window", 32768),
                num_output=self.max_tokens or -1,
                is_chat_model=True,
                is_function_calling_model=False,
                model_name=self.model,
            )
except ImportError:
    _LLAMAINDEX_OPENAI_AVAILABLE = False
    _LMStudioOpenAI = None
    _LlamaOpenAIEmbedding = None

class LMStudioProvider(Provider):
    """Local :class:`Provider` over LM Studio's OpenAI-compatible server.

    Talks to LM Studio's ``/v1`` endpoints (placeholder ``lm-studio`` api key).
    Model IDs are used raw — unlike Ollama there is no tag resolution — and the
    LLM wrapper is the :class:`_LMStudioOpenAI` subclass that forces
    ``is_chat_model=True`` so arbitrary (non-OpenAI-named) local models route to
    ``/v1/chat/completions`` rather than the dead legacy completions endpoint.
    ``base_url`` is read by the local LLM adapter's tool-use branch to build its
    own one-shot ``openai.OpenAI`` client.
    """

    def __init__(self, host: str = LM_STUDIO_HOST):
        self.host = host
        self.base_url = f"{host}/v1"

    def check_running(self) -> tuple[bool, str]:
        """Reachability probe: a short-timeout GET of ``/v1/models``."""
        try:
            req = urllib.request.Request(
                f"{self.base_url}/models",
                headers={"Authorization": "Bearer lm-studio"},
            )
            with urllib.request.urlopen(req, timeout=3):
                pass
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def get_models(self) -> tuple[list[str], str]:
        """List the model IDs LM Studio currently serves, or ``([], error)``."""
        try:
            req = urllib.request.Request(
                f"{self.base_url}/models",
                headers={"Authorization": "Bearer lm-studio"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            models = [m["id"] for m in data.get("data", []) if isinstance(m, dict)]
            return models, ""
        except Exception as exc:
            return [], f"Cannot reach LM Studio at {self.host}: {exc}"

    def get_llm(self, model_name: str, **kwargs) -> Any:
        """Return a LlamaIndex LLM (the :class:`_LMStudioOpenAI` subclass) for
        the model.

        Stashes the resolved ``context_window`` on the instance (consumed by the
        subclass's ``metadata``) and applies ``local_request_timeout_s`` to the
        OpenAI client when set (>0), never overriding an explicit caller kwarg.
        """
        if not _LLAMAINDEX_OPENAI_AVAILABLE:
            raise ImportError("llama-index-llms-openai is required for LM Studio support.")
        
        context_window = int(kwargs.pop("context_window", 32768) or 32768)
        # Apply the configured per-call HTTP timeout to the LlamaIndex OpenAI
        # client (LM Studio speaks the OpenAI API). Only when set (>0) so 0
        # leaves the SDK's own default; never override an explicit caller kwarg.
        timeout = local_request_timeout()
        if timeout is not None and "timeout" not in kwargs:
            kwargs["timeout"] = timeout
        # Raw model string from config is used directly
        llm = _LMStudioOpenAI(
            model=model_name,
            api_base=self.base_url,
            api_key="lm-studio",
            **kwargs
        )
        object.__setattr__(llm, "_lmstudio_context_window", context_window)
        return llm

    def get_embedding(self, model_name: str, **kwargs) -> Any:
        """Return a LlamaIndex ``OpenAIEmbedding`` pointed at LM Studio.

        Passes the LM Studio model ID via ``model_name`` (not ``model``) to
        bypass the SDK's ``OpenAIEmbeddingModelType`` enum validation, which
        would reject arbitrary local IDs.
        """
        if not _LLAMAINDEX_OPENAI_AVAILABLE:
            raise ImportError("llama-index-embeddings-openai is required for LM Studio support.")
        
        return _LlamaOpenAIEmbedding(
            # LM Studio exposes arbitrary local model IDs. Passing them via
            # ``model`` triggers OpenAIEmbeddingModelType enum validation.
            model_name=model_name,
            api_base=self.base_url,
            api_key="lm-studio",
            **kwargs
        )

    def stream_chat(self, model: str, prompt: str, system_prompt: Optional[str] = None,
                    request_timeout: Optional[float] = None, **kwargs) -> Any:
        """Stream a chat completion through a one-shot OpenAI client.

        Builds a fresh ``openai.OpenAI`` per call (LM Studio is not on the
        ollama-style shared-client cache) and applies ``local_request_timeout_s``
        as the per-read gap — i.e. a max-time-between-tokens stall guard, not a
        total-call deadline — when set. Returns the raw streaming iterator.

        ``request_timeout`` (when not None) overrides the configured value for
        this call — the single-paper route passes a non-zero floor so a wedged
        backend can't hang its guard-less synchronous SSE stream.
        """
        try:
            import openai
            # Bound this streaming call with the explicit request_timeout when
            # given, else the configured local timeout when set (>0); otherwise
            # 0 leaves the OpenAI SDK default. For a stream this is the per-read
            # gap, i.e. a max-time-between-tokens stall guard.
            timeout = request_timeout if request_timeout is not None else local_request_timeout()
            client_kwargs = {"base_url": self.base_url, "api_key": "lm-studio"}
            if timeout is not None:
                client_kwargs["timeout"] = timeout
            client = openai.OpenAI(**client_kwargs)
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            
            return client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                **kwargs
            )
        except ImportError:
            raise ImportError("openai SDK is required for LM Studio streaming.")
