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
