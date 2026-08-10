"""OpenAI-compatible online adapters (DeepSeek, Z.AI / Zhipu GLM, OpenRouter).

These providers expose an OpenAI-compatible Chat Completions API — same wire
format, tools dialect, and streaming shape the official OpenAI endpoint uses —
so they reuse :class:`core.llm.adapters.openai.OpenAIProvider` wholesale and
only override the pieces that differ: the provider ``name`` (used for usage
records + error labels), the API-key env var, the fixed API ``base_url``, and
the curated model list.

Key resolution mirrors the OpenAI adapter: read from the environment at call
time so a key set after launch is picked up without a restart, and never
persist it to config. ``<PROVIDER>_BASE_URL`` env overrides the built-in
endpoint for self-hosted / regional gateways.

Because these run against a custom ``base_url``, the inherited
``_uses_max_completion_tokens`` returns the legacy ``max_tokens`` shape (which
these endpoints expect) for their normal models, and the reactive
``_heal_unsupported_param`` pass absorbs any residual param-shape drift.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from core.llm.adapters.openai import OpenAIProvider
from core.llm.types import ErrorCategory, LLMError

logger = logging.getLogger(__name__)


class _OpenAICompatProvider(OpenAIProvider):
    """Shared base for OpenAI-compatible online providers.

    Subclasses set the four class attributes below. Everything else —
    streaming, tool use, retry/heal, usage recording, error classification —
    is inherited unchanged from :class:`OpenAIProvider`.
    """

    name = "openai_compat"
    #: Env var holding the API key (checked first; a legacy alias may follow).
    key_env_vars: tuple[str, ...] = ()
    #: Built-in API base (the ``openai`` SDK appends ``/chat/completions`` etc.).
    default_base_url: str = ""
    #: Env var that overrides :attr:`default_base_url` when set.
    base_url_env_var: str = ""
    #: Curated model ids (each MUST have a PRICING_TABLE row — see usage.py).
    CURATED_MODELS: list[str] = []
    #: Model ids that reject the ``tools=`` schema outright (400 on every
    #: tool-enabled request). ``_common_params`` strips tools for them so the
    #: agent loop degrades to a text answer / its RAG fallback — the same
    #: graceful path an Ollama model without tool support takes — instead of
    #: hard-failing every agent/deck turn on that model.
    MODELS_WITHOUT_TOOLS: frozenset[str] = frozenset()

    def _common_params(self, request) -> dict:
        params = super()._common_params(request)
        model = (request.model or "").lower()
        if params.get("tools") and model in self.MODELS_WITHOUT_TOOLS:
            logger.warning(
                "%s: model %r does not support tool calling; sending the "
                "request without tools (agent features degrade to plain "
                "answers on this model).", self.name, request.model,
            )
            params.pop("tools", None)
            params.pop("tool_choice", None)
        return params

    def _api_key(self) -> str:
        """Resolve the key from this provider's env var(s), else raise AUTH."""
        key = self._explicit_key
        if not key:
            for env_var in self.key_env_vars:
                key = os.environ.get(env_var)
                if key:
                    break
        if not key:
            primary = self.key_env_vars[0] if self.key_env_vars else "the API key env var"
            raise LLMError(
                category=ErrorCategory.AUTH,
                message=(
                    f"{primary} is not set. Add it to the environment before "
                    f"launching ChatEKLD, or set it in your shell profile."
                ),
                provider=self.name,
            )
        return key

    def _base_url(self) -> Optional[str]:
        """Fixed provider endpoint, overridable via the provider's env var."""
        if self._explicit_base_url:
            return self._explicit_base_url
        if self.base_url_env_var:
            override = os.environ.get(self.base_url_env_var)
            if override:
                return override
        return self.default_base_url or None

    def list_models(self) -> tuple[list[str], str]:
        """Curated models plus any live-listed chat models (cached, key-gated).

        Overrides :meth:`OpenAIProvider.list_models` so the merge uses this
        provider's own curated list and a provider-scoped cache key instead of
        the OpenAI module globals.
        """
        from core.llm.model_listing import merged_models
        models = merged_models(
            self.name, self.CURATED_MODELS, self._fetch_live_models,
            cache_key=f"{self.name}:{self._base_url() or ''}",
        )
        return models, ""


# --- DeepSeek ---------------------------------------------------------------
# Verified against api-docs.deepseek.com/quick_start/pricing (2026-07-12):
# current ids are the V4 family. OpenAI-format base URL is
# https://api.deepseek.com; models use the legacy max_tokens shape. Both V4
# models support function calling (tools) per the official features table.
# The legacy `deepseek-chat` / `deepseek-reasoner` compatibility aliases were
# RETIRED at the provider's 2026-07-24 sunset (the date-gated curation that
# enforced it was removed 2026-07-30 as permanently-dead code); the 3→4 config
# migration resets a persisted alias selection, so no install can still send
# an id the provider 404s.
DEEPSEEK_CURATED_MODELS: list[str] = ["deepseek-v4-flash", "deepseek-v4-pro"]


class DeepSeekProvider(_OpenAICompatProvider):
    name = "deepseek"
    key_env_vars = ("DEEPSEEK_API_KEY",)
    default_base_url = "https://api.deepseek.com"
    base_url_env_var = "DEEPSEEK_BASE_URL"
    CURATED_MODELS = DEEPSEEK_CURATED_MODELS
    # Empty as of 2026-07-12: the V4 features table lists function calling as
    # supported on BOTH models (the historical R1-era reasoner limitation is
    # gone). The MODELS_WITHOUT_TOOLS mechanism stays on the base class for
    # the next model that ships without tool support.
    MODELS_WITHOUT_TOOLS = frozenset()


# --- Z.AI (Zhipu GLM) -------------------------------------------------------
# Verified against docs.z.ai/guides/overview/pricing (2026-07-12): current
# line is GLM-5.x / 4.7; glm-4.7-flash and glm-4.5-flash are free-tier ($0).
# OpenAI-compatible base is https://api.z.ai/api/paas/v4 (an
# /api/openai/v1 variant also exists — reachable via ZAI_BASE_URL if ever
# needed); models use the legacy max_tokens shape.
ZAI_CURATED_MODELS: list[str] = [
    "glm-5.2",
    "glm-5",
    "glm-4.7",
    "glm-4.7-flash",
    "glm-4.5-air",
    "glm-4.5-flash",
]


class ZAIProvider(_OpenAICompatProvider):
    name = "zai"
    key_env_vars = ("ZAI_API_KEY", "ZHIPU_API_KEY")
    default_base_url = "https://api.z.ai/api/paas/v4"
    base_url_env_var = "ZAI_BASE_URL"
    CURATED_MODELS = ZAI_CURATED_MODELS


# --- OpenRouter --------------------------------------------------------------
# Aggregator over many upstream vendors, one key, one OpenAI-compatible
# endpoint (https://openrouter.ai/api/v1 — verified live 2026-08-08 against
# its public GET /api/v1/models, 400 models listed). Model ids are NAMESPACED
# (``vendor/model``) and may carry a ``:variant`` suffix (``:free``,
# ``:batch``, …) with its own price — see the OpenRouter block in
# usage.PRICING_TABLE and the namespaced-id guard in estimate_cost_usd.
#
# Design decisions (2026-08-08):
# * Curated subset only, every id verified tools-capable in the live catalog.
#   A live-listed id without tool support is rejected by OpenRouter as a
#   404/503 ROUTING error ("No endpoints found…" / "routing requirements"),
#   never a param-named 400 — ``_is_capability_routing_error`` feeds it into
#   the inherited strip-tools heal so the turn degrades to a plain answer
#   (``MODELS_WITHOUT_TOOLS`` stays available for a curated exception).
# * Attribution headers (HTTP-Referer / X-Title) are deliberately NOT sent:
#   they are optional, identify the app to a third party, and buy the user
#   nothing — the app minimises identifying egress.
# * OpenRouter does its own upstream provider routing/fallback INTERNALLY;
#   the app-level fallback_provider/fallback_on policy applies unchanged on
#   top of it (before the first token, like every online provider).
OPENROUTER_CURATED_MODELS: list[str] = [
    "anthropic/claude-haiku-4.5",
    "anthropic/claude-sonnet-5",
    "openai/gpt-5.4-mini",
    "openai/gpt-5.4",
    "google/gemini-3.6-flash",
    "google/gemini-2.5-pro",
    "deepseek/deepseek-v4-flash",
    "meta-llama/llama-4-maverick",
    "moonshotai/kimi-k2.6",
    "nvidia/nemotron-3-super-120b-a12b:free",
]


# Phrasings OpenRouter uses for "no upstream endpoint can satisfy this
# request shape" (their errors doc lists the 503 wording; the 404 "No
# endpoints found" form is the historical/alternate one). Matched
# case-insensitively as substrings of the SDK exception message.
_OPENROUTER_ROUTING_PHRASES = ("no endpoints found", "routing requirements")


class OpenRouterProvider(_OpenAICompatProvider):
    name = "openrouter"
    key_env_vars = ("OPENROUTER_API_KEY",)
    default_base_url = "https://openrouter.ai/api/v1"
    base_url_env_var = "OPENROUTER_BASE_URL"
    CURATED_MODELS = OPENROUTER_CURATED_MODELS

    def _is_capability_routing_error(self, exc: BaseException) -> bool:
        """OpenRouter signals "no upstream endpoint supports this request
        shape" (e.g. tools sent to a model with no tool-capable endpoint) as
        a **503** "There is no available model provider that meets your
        routing requirements" or a **404** "No endpoints found…" — never the
        param-named 400 the inherited heal understands. Matching it here lets
        ``_create_with_heal`` strip tools for ONE retry, degrading the turn
        to a plain answer (the same path as the tools-400 heal) instead of
        burning every retry as SERVER_ERROR and then triggering provider
        fallback. Documented trade-off: a genuinely transient 503 that
        happens to carry the routing phrase on a tools request costs one
        tool-less retry; tool-less requests and other 5xx keep their normal
        retry semantics (the caller gates on ``tools`` being in the payload).
        """
        if getattr(exc, "status_code", None) not in (404, 503):
            return False
        message = str(exc).lower()
        return any(p in message for p in _OPENROUTER_ROUTING_PHRASES)
