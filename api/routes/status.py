"""Provider status / model-listing endpoints.

Three read-only routes that the UI polls to populate provider/model selectors
and the connection badge. Each dispatches on the *active* provider and crosses
the local-vs-online boundary: local providers (``ollama`` / ``lm_studio``) go
through ``core.providers.get_provider`` (the embedding/legacy-chat adapters),
while online providers go through ``core.llm.factory.get_llm_provider`` (the
unified chat interface). For an online provider "ok" only means an API key is
present — there is no live ping. Vision/OCR model listing is provider-scoped to
local backends only (online providers expose no vision interface here).

All three are gated by :func:`api.security.origin_is_local`.
"""
from flask import Blueprint, jsonify, request
from core.providers import get_provider
from api.security import origin_is_local
from api.validators import coerce_enum
from core.providers.server import get_provider_warnings
from core.llm.factory import get_llm_provider, is_online

_LOCAL_PROVIDERS = frozenset({"ollama", "lm_studio"})
_VISION_KINDS = frozenset({"ocr", "vision"})

status_bp = Blueprint('status', __name__)

@status_bp.route("/api/status")
def api_status():
    """Report the active provider's reachability for the connection badge.

    For an online provider, ``ok`` is the result of ``health_check()`` — which
    only confirms an API key is configured (no network round-trip). For a local
    provider it is ``check_running()`` (a real liveness probe). ``ollama_ok`` is
    kept as a duplicate of ``ok`` for backward compatibility with older UI code.
    ``warnings`` surfaces any accumulated provider-config warnings.
    """
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403

    from core.config import load_config
    cfg = load_config()
    provider_name = cfg.get("provider", "ollama")

    if is_online(provider_name):
        provider = get_llm_provider(provider_name)
        ok, err = provider.health_check()
    else:
        provider = get_provider(provider_name)
        ok, err = provider.check_running()

    return jsonify({
        "provider": provider_name,
        "ollama_ok": ok,  # Keeping key for backward compat
        "ok": ok,
        "online": is_online(provider_name),
        "error": err,
        "warnings": get_provider_warnings(),
    })

@status_bp.route("/api/models")
def api_models():
    """List chat models for the active provider.

    Online providers return ``CURATED_MODELS`` merged with a best-effort,
    key-gated, short-TTL-cached live fetch (degrading to curated-only without a
    key / offline); local providers return the installed model list. The active
    provider name is echoed back so the UI can confirm what it queried.
    """
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    from core.config import load_config
    provider_name = load_config().get("provider", "ollama")
    if is_online(provider_name):
        provider = get_llm_provider(provider_name)
        models, err = provider.list_models()
    else:
        provider = get_provider(provider_name)
        models, err = provider.get_models()
    return jsonify({"models": models, "error": err, "provider": provider_name})

@status_bp.route("/api/vision-models")
def api_vision_models():
    """List vision/OCR models for a LOCAL backend, plus the current selections.

    Vision/OCR is local-only (Ollama or LM Studio): the ``?provider=`` query arg
    is enum-clamped to ``_LOCAL_PROVIDERS`` and falls back to the configured
    ``vision_provider`` / ``ocr_provider`` for the requested ``?kind=`` (``vision``
    vs ``ocr``, default ``ocr``). The response also echoes the persisted
    provider/model selections (and the live manager singletons' current values)
    so the Settings UI can show what is actually wired up for each kind.
    """
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    from core.config import load_config
    from services.vision import glm_ocr_manager, vision_manager

    cfg = load_config()
    requested_provider = coerce_enum(
        request.args.get("provider", "").strip().lower(), _LOCAL_PROVIDERS,
    )
    kind = coerce_enum(
        request.args.get("kind", "ocr").strip().lower(), _VISION_KINDS,
    ) or "ocr"
    default_provider = cfg.get("vision_provider", "ollama") if kind == "vision" else cfg.get("ocr_provider", "ollama")
    model_provider = requested_provider or default_provider
    provider = get_provider(model_provider)
    models, err = provider.get_models()
    selected_provider = cfg.get("vision_provider") if kind == "vision" else cfg.get("ocr_provider")
    selected_model = cfg.get("vision_model") if kind == "vision" else cfg.get("ocr_model")
    return jsonify({
        "models": models,
        "error": err,
        "provider": model_provider or cfg.get("provider", "ollama"),
        "kind": kind,
        "selected_provider": selected_provider or model_provider,
        "selected_model": selected_model or "",
        "ocr_provider": cfg.get("ocr_provider") or glm_ocr_manager.provider,
        "ocr_model": cfg.get("ocr_model") or glm_ocr_manager.model,
        "vision_provider": cfg.get("vision_provider") or vision_manager.provider,
        "vision_model": cfg.get("vision_model") or vision_manager.model,
    })
