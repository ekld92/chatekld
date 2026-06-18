import json
import logging
import tiktoken
import urllib.request
from typing import Any, Optional
from core.providers.base import Provider, local_request_timeout
from core.constants import LM_STUDIO_HOST

logger = logging.getLogger(__name__)

try:
    from llama_index.llms.openai import OpenAI as _LlamaOpenAI
    from llama_index.embeddings.openai import OpenAIEmbedding as _LlamaOpenAIEmbedding
    from llama_index.core.llms import LLMMetadata
    _LLAMAINDEX_OPENAI_AVAILABLE = True

    class _LMStudioOpenAI(_LlamaOpenAI):
        """Thin subclass of LlamaIndex's OpenAI LLM wrapper for LM Studio."""
        @property
        def _tokenizer(self):
            return tiktoken.get_encoding("cl100k_base")

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
    def __init__(self, host: str = LM_STUDIO_HOST):
        self.host = host
        self.base_url = f"{host}/v1"

    def check_running(self) -> tuple[bool, str]:
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

    def stream_chat(self, model: str, prompt: str, system_prompt: Optional[str] = None, **kwargs) -> Any:
        try:
            import openai
            # Bound this streaming call with the configured local timeout when
            # set (>0); 0 leaves the OpenAI SDK default. For a stream this is the
            # per-read gap, i.e. a max-time-between-tokens stall guard.
            timeout = local_request_timeout()
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
