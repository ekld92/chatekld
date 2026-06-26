"""Local provider registry used by the embedding + indexing code paths.

This module is intentionally local-only. Online chat providers
(OpenAI, Anthropic, Google) flow through :mod:`core.llm` and never
expose an embedding interface — when a caller asks for a "provider"
by an online name, this factory transparently substitutes the
configured local embed provider (default: Ollama) so the indexer keeps
working with a sane embedding model.

The chat path should use :func:`core.llm.get_llm_provider` directly;
this function exists primarily for embedding / model-listing in the
local server lifecycle code.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from core.providers.ollama import OllamaProvider
from core.providers.lms import LMStudioProvider
from core.config import load_config, resolve_embed_provider

if TYPE_CHECKING:
    from core.providers.base import Provider

logger = logging.getLogger(__name__)

_LOCAL_PROVIDER_NAMES = frozenset({"ollama", "lm_studio"})


def get_provider(provider_name: Optional[str] = None) -> "Provider":
    """Return a LOCAL :class:`Provider`, always — never an online adapter.

    Resolution order: an explicit *provider_name*, else the persisted
    ``provider`` config key (default ``ollama``). When the name is an ONLINE
    provider (``openai``/``anthropic``/``google``), it is silently substituted
    with the configured local embed provider via
    :func:`core.config.resolve_embed_provider`, so the indexer / embedding paths
    keep a working local model even while chat runs online. A fresh provider
    instance is returned on each call (the ollama HTTP client is cached at module
    scope, so this stays cheap).
    """
    if provider_name is None:
        cfg = load_config()
        provider_name = cfg.get("provider", "ollama")
    name = (provider_name or "").strip().lower()
    if name not in _LOCAL_PROVIDER_NAMES:
        cfg = load_config()
        resolved = resolve_embed_provider(cfg, name)
        if name:
            logger.debug(
                "get_provider(%r) is online; substituting local provider %r",
                provider_name,
                resolved,
            )
        name = resolved
    if name == "lm_studio":
        return LMStudioProvider()
    return OllamaProvider()
