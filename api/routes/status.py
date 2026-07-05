"""Provider status / model-listing endpoints.

Read-only routes that the UI polls to populate provider/model selectors and
the connection/health badges (/api/status, /api/models, /api/vision-models,
/api/health). Each dispatches on the *active* provider and crosses
the local-vs-online boundary: local providers (``ollama`` / ``lm_studio``) go
through ``core.providers.get_provider`` (the embedding/legacy-chat adapters),
while online providers go through ``core.llm.factory.get_llm_provider`` (the
unified chat interface). For an online provider "ok" only means an API key is
present — there is no live ping. Vision/OCR model listing is provider-scoped to
local backends only (online providers expose no vision interface here).

All routes are gated by the app-level before_request origin guard
(api.security.register_origin_guard).
"""
import threading
import time

from flask import Blueprint, jsonify, request
from core.providers import get_provider
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


# Server-side result cache for /api/health. The UI polls every 15 s and each
# uncached probe does live TCP checks against Ollama AND LM Studio plus disk
# reads — without a TTL the badge poll alone hammers both backends 4x/minute
# forever (and doubles up when two windows are open). 10 s keeps the badge at
# most one poll behind reality while collapsing concurrent/burst callers onto
# one probe.
_HEALTH_TTL_S = 10.0
_health_cache: dict = {"at": 0.0, "payload": None}
_health_cache_lock = threading.Lock()


def _compute_health() -> dict:
    """One uncached health probe: vector store + local model backends."""
    import os

    from core.config import load_config
    from core.constants import OBSIDIAN_INDEX_DIR
    from rag.lancedb_store import lancedb_table_count
    from rag.vault import obsidian_manager

    cfg = load_config()
    details = {
        "vector_store": {"status": "ok", "error": None},
        "local_model": {"status": "ok", "error": None},
    }

    # 1) Vector store. Which backend an EXISTING index uses is decided by the
    #    on-disk index, not the config knob (the knob only governs fresh
    #    builds) — lancedb_table_count returns -1 when no lancedb table
    #    exists, which routes us to the simple/docstore checks.
    try:
        vault_path = obsidian_manager.get_vault_path()
        if not vault_path:
            details["vector_store"] = {
                "status": "degraded",
                "error": "No vault folder configured.",
            }
        elif not os.path.isdir(vault_path):
            details["vector_store"] = {
                "status": "error",
                "error": f"Configured vault path is not a directory: {vault_path}",
            }
        else:
            lancedb_rows = lancedb_table_count(OBSIDIAN_INDEX_DIR)
            if lancedb_rows >= 0:
                if lancedb_rows == 0:
                    details["vector_store"] = {
                        "status": "degraded",
                        "error": "LanceDB table exists but holds no vectors.",
                    }
            else:
                # Simple backend (or nothing yet). docstore_doc_count snapshots
                # the LOADED index under the manager's own mutation lock —
                # reading obsidian_manager._index.docstore directly from this
                # poll raced the streaming indexer's inserts.
                count = obsidian_manager.docstore_doc_count()
                if count == 0:
                    details["vector_store"] = {
                        "status": "degraded",
                        "error": "Loaded index docstore is empty.",
                    }
                elif count is None and not os.path.isfile(
                    os.path.join(OBSIDIAN_INDEX_DIR, "docstore.json")
                ):
                    # None = not loaded (lazy-load pending) or briefly busy —
                    # only "and nothing persisted either" is a real finding.
                    details["vector_store"] = {
                        "status": "degraded",
                        "error": "Vault index has not been built yet.",
                    }
    except Exception as exc:
        details["vector_store"] = {
            "status": "error",
            "error": f"Unexpected health check failure: {exc}",
        }

    # 2) Local model backends: every distinct LOCAL provider any role is
    #    configured to use (chat/embed/vision/OCR), deduplicated so Ollama is
    #    probed once even when it serves all four roles.
    local_errors = []
    providers_to_check = {
        cfg.get(key, "ollama")
        for key in ("provider", "embed_provider", "vision_provider", "ocr_provider")
    } & _LOCAL_PROVIDERS

    for prov_name in sorted(providers_to_check):
        try:
            prov = get_provider(prov_name)
            ok, err = prov.check_running()
            if not ok:
                local_errors.append(f"{prov_name} is not running: {err or 'connection refused'}")
                continue
            # A4 first-run UX: a runner that is UP but has ZERO models installed
            # cannot index or chat — the first index/chat would otherwise fail at
            # call time with a model-not-found error and no guidance. Surface an
            # ACTIONABLE hint here (distinct from "not running") so the first-run
            # banner tells the user to pull a model. Only a definitively-empty list
            # with NO list error counts: a transient get_models failure must not be
            # mistaken for "no models" (that would nag a healthy install on a blip).
            try:
                models, models_err = prov.get_models()
            except Exception:
                models, models_err = None, "unavailable"
            if models is not None and not models_err and len(models) == 0:
                local_errors.append(
                    f"{prov_name} is running but has no models installed — pull an "
                    "embedding model (e.g. nomic-embed-text) and a chat model to index and chat."
                )
        except Exception as exc:
            local_errors.append(f"Failed to check {prov_name}: {exc}")

    if local_errors:
        details["local_model"] = {
            "status": "degraded",
            "error": "; ".join(local_errors),
        }

    # Overall severity is the worst sub-check: "error" must not be flattened
    # into "degraded" (the pre-fix behaviour) or the UI can't distinguish a
    # broken vault path from a merely-unbuilt index.
    statuses = {details["vector_store"]["status"], details["local_model"]["status"]}
    if "error" in statuses:
        overall_status = "error"
    elif "degraded" in statuses:
        overall_status = "degraded"
    else:
        overall_status = "ok"

    return {"status": overall_status, "details": details}


@status_bp.route("/api/health")
def api_health():
    """Grounded liveness/readiness probe (vector store + local LLM backends).

    TTL-cached server-side (see ``_HEALTH_TTL_S``) so the UI's 15 s badge
    poll cannot turn into a per-request probe storm against the local
    backends.
    """

    now = time.monotonic()
    with _health_cache_lock:
        cached = _health_cache["payload"]
        if cached is not None and now - _health_cache["at"] < _HEALTH_TTL_S:
            return jsonify(cached)

    payload = _compute_health()

    with _health_cache_lock:
        _health_cache["payload"] = payload
        _health_cache["at"] = time.monotonic()

    return jsonify(payload)
