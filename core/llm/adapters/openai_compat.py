"""OpenAI-compatible online adapters (DeepSeek, Z.AI / Zhipu GLM, OpenRouter).

These providers expose an OpenAI-compatible Chat Completions transport but
retain provider-specific request policy for sampling, reasoning and tools.

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
import threading
import time
from typing import Optional

from core.llm.adapters.openai import OpenAIProvider
from core.llm.types import ErrorCategory, LLMError

logger = logging.getLogger(__name__)


class _OpenAICompatProvider(OpenAIProvider):
    """Shared base for OpenAI-compatible online providers.

    Subclasses set their identity and may specialize request construction.
    Streaming, usage recording, and baseline error classification are shared.
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
                    f"launching CHERQAiN, or set it in your shell profile."
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
DEEPSEEK_CURATED_MODELS: list[str] = ["deepseek-flash", "deepseek-v4-pro"]


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

    def _common_params(self, request) -> dict:
        params = super()._common_params(request)
        # The V4 thinking mode is enabled by default. DeepSeek accepts but
        # ignores temperature and silently floors top_p below 0.95 there.
        if (request.model or "").lower() in self.CURATED_MODELS:
            params.pop("temperature", None)
            if params.get("top_p", 1.0) < 0.95:
                params.pop("top_p", None)
        return params


# --- Z.AI (Zhipu GLM) -------------------------------------------------------
# Verified against docs.z.ai/guides/overview/pricing (2026-07-12): current
# line is GLM-5.x / 4.7; glm-4.7-flash and glm-4.5-flash are free-tier ($0).
# OpenAI-compatible base is https://api.z.ai/api/paas/v4 (an
# /api/openai/v1 variant also exists — reachable via ZAI_BASE_URL if ever
# needed); models use the legacy max_tokens shape.
ZAI_CURATED_MODELS: list[str] = [
    "glm-5.3",
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

    def _common_params(self, request) -> dict:
        params = super()._common_params(request)
        if "temperature" in params:
            params["temperature"] = max(0.0, min(float(params["temperature"]), 1.0))
        if "top_p" in params:
            params["top_p"] = max(0.01, min(float(params["top_p"]), 1.0))
        if request.reasoning_effort and (request.model or "").lower().startswith("glm-5.3"):
            # GLM-5.3 requires thinking enabled and accepts low/high/max.
            # The shared UI currently offers only minimal and low.
            effort = ({"minimal": "low", "low": "low", "medium": "high",
                       "high": "high", "xhigh": "max", "max": "max"}
                      .get(request.reasoning_effort))
            if effort is None:
                raise LLMError(ErrorCategory.INVALID_REQUEST,
                               "Unsupported GLM-5.3 reasoning effort",
                               provider=self.name, model=request.model)
            params["extra_body"] = {
                "thinking": {"type": "enabled"}, "reasoning_effort": effort,
            }
        return params


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

# Advisory model-level union from the most recent key-gated catalog fetch.
# Endpoint routing can still change; this never proves a parameter reached the
# selected upstream.  Keep the cache no longer than model_listing's TTL.
_openrouter_capabilities: dict[
    tuple[str, str], tuple[float, frozenset[str], frozenset[str] | None]
] = {}
_openrouter_capabilities_lock = threading.Lock()
_OPENROUTER_CAPABILITY_TTL_S = 300.0


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

    def _fetch_live_models(self) -> list[str]:
        """Keep OpenRouter namespaced chat ids and advisory parameter metadata."""
        rows = self._client().models.list()  # _api_key runs before network
        now = time.monotonic()
        found: list[str] = []
        capabilities: dict[
            tuple[str, str], tuple[float, frozenset[str], frozenset[str] | None]
        ] = {}
        for row in rows:
            mid = getattr(row, "id", None)
            if mid is None and isinstance(row, dict):
                mid = row.get("id")
            if not isinstance(mid, str) or "/" not in mid:
                continue
            # The direct OpenAI denylist drops chat-capable */*-codex ids.
            # OpenRouter advertises text output in the model architecture.
            arch = getattr(row, "architecture", None)
            if arch is None and isinstance(row, dict):
                arch = row.get("architecture")
            outputs = (arch.get("output_modalities") if isinstance(arch, dict)
                       else getattr(arch, "output_modalities", None))
            if isinstance(outputs, list) and "text" not in outputs:
                continue
            found.append(mid)
            raw = getattr(row, "supported_parameters", None)
            if raw is None and isinstance(row, dict):
                raw = row.get("supported_parameters")
            if isinstance(raw, list):
                reasoning = getattr(row, "reasoning", None)
                if reasoning is None and isinstance(row, dict):
                    reasoning = row.get("reasoning")
                efforts = (reasoning.get("supported_efforts")
                           if isinstance(reasoning, dict)
                           else getattr(reasoning, "supported_efforts", None))
                capabilities[(self._base_url() or "", mid)] = (
                    now + _OPENROUTER_CAPABILITY_TTL_S,
                    frozenset(p for p in raw if isinstance(p, str)),
                    (frozenset(e for e in efforts if isinstance(e, str))
                     if isinstance(efforts, list) else None),
                )
        with _openrouter_capabilities_lock:
            for key, entry in list(_openrouter_capabilities.items()):
                if entry[0] <= now:
                    _openrouter_capabilities.pop(key, None)
            _openrouter_capabilities.update(capabilities)
        return found

    def _common_params(self, request) -> dict:
        params = super()._common_params(request)
        model = (request.model or "").lower()
        if params.get("tool_choice") == "auto":
            # OpenRouter defaults to auto when tools are present. Omitting the
            # redundant field keeps required routing focused on tool support.
            params.pop("tool_choice")
        with _openrouter_capabilities_lock:
            entry = _openrouter_capabilities.get((self._base_url() or "", request.model))
        supported = entry[1] if entry and entry[0] > time.monotonic() else None
        supported_efforts = entry[2] if supported is not None and entry else None

        # Namespaced IDs do not match the direct-provider guards.  Defaults
        # here are intentionally conservative where an upstream can ignore a
        # field even though OpenRouter accepts its generic JSON shape.
        if (model.startswith("google/gemini-3")
                or model.startswith(("openai/gpt-5", "openai/o"))
                or model.startswith(("anthropic/claude-sonnet-5",
                                   "anthropic/claude-opus-5",
                                   "anthropic/claude-fable-5"))
                or model.startswith("deepseek/deepseek-v4")):
            params.pop("temperature", None)
            params.pop("top_p", None)
        if supported is not None:
            for field in ("temperature", "top_p"):
                if field not in supported:
                    params.pop(field, None)
        elif model not in OPENROUTER_CURATED_MODELS:
            params.pop("temperature", None)
            params.pop("top_p", None)

        if request.reasoning_effort:
            if supported is not None and "reasoning" not in supported:
                raise LLMError(ErrorCategory.INVALID_REQUEST,
                               "OpenRouter catalog does not list reasoning for this model",
                               provider=self.name, model=request.model)
            effort = request.reasoning_effort
            if effort == "minimal" and model.startswith((
                "google/gemini-3.7", "google/gemini-3.8",
                "google/gemini-3.1-pro", "google/gemini-3-pro",
            )):
                effort = "low"
            if supported_efforts is not None and effort not in supported_efforts:
                raise LLMError(ErrorCategory.INVALID_REQUEST,
                               f"OpenRouter catalog does not list {effort} reasoning for this model",
                               provider=self.name, model=request.model)
            params["extra_body"] = {"reasoning": {"effort": effort}}
        if "tools" in params or request.reasoning_effort:
            # The model-level catalog is a union of endpoints. With default
            # routing, an endpoint may accept a request but ignore unsupported
            # fields. Require an upstream that handles the tool/reasoning
            # controls, including when catalog metadata is unavailable.
            params.setdefault("extra_body", {})["provider"] = {
                "require_parameters": True,
            }
        return params

    def _is_capability_routing_error(self, exc: BaseException) -> bool:
        """OpenRouter signals "no upstream endpoint supports this request
        shape" (e.g. tools sent to a model with no tool-capable endpoint) as
        a **503** "There is no available model provider that meets your
        routing requirements" or a **404** "No endpoints found…". A match
        with tools in the request becomes a visible INVALID_REQUEST; dropping
        required tools would silently change the answer's evidence boundary.
        """
        if getattr(exc, "status_code", None) not in (404, 503):
            return False
        message = str(exc).lower()
        return any(p in message for p in _OPENROUTER_ROUTING_PHRASES)
