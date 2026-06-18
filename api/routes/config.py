import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from flask import Blueprint, jsonify, request, Response
from core.config import load_config, save_config, load_report_types
from core.constants import (
    CONFIG_FILE,
    EXACT_BLOCKED,
    FEEDBACK_FILE,
    OBSIDIAN_INDEX_DIR,
    SYSTEM_ROOTS,
    VAULT_MD_EXTS,
    VAULT_BINARY_EXTS,
)
from core.database import DB_LOCK, get_db_connection, init_db
from core.utils import log_storage_deletion
from core.providers.server import clear_provider_warnings
from api.security import origin_is_local
from api.validators import (
    coerce_bool,
    coerce_enum,
    coerce_float_in_range,
    coerce_int_in_range,
    coerce_regex,
)
from rag.vault import obsidian_manager

config_bp = Blueprint('config', __name__)

# --- LLM-parameter validation on save -------------------------------------
# POST /api/config otherwise persists arbitrary values verbatim.  The Settings
# window now writes the full set of numeric/enum LLM knobs (including the
# timeout/retry/token plumbing that previously had no UI), so guard each one so
# a crafted body — or a hand-edited config.json round-tripped through the UI —
# cannot store a pathological timeout, token budget, or out-of-range knob.  A
# value that fails coercion is DROPPED from the payload (not persisted), leaving
# the previously-saved value intact rather than overwriting it with garbage.
_PROVIDER_NAMES = ("ollama", "lm_studio", "openai", "anthropic", "google")
_FALLBACK_ON_ALLOWED = ("timeout", "network", "rate_limit", "server_error")


def _coerce_fallback_provider(value):
    """Allow "" (fallback disabled) or a known provider name; else None."""
    if isinstance(value, str):
        s = value.strip().lower()
        if s == "" or s in _PROVIDER_NAMES:
            return s
    return None


def _coerce_fallback_on(value):
    """Filter to the allowed transient-error categories.

    Returns None (⇒ drop, preserve prior) when the value is not a list, OR when
    a NON-empty list filters down to nothing (it was all garbage — treat like
    any other invalid input rather than silently disabling all fallback). An
    explicitly empty list ``[]`` is a valid "fall back on nothing" and is kept.
    """
    if not isinstance(value, list):
        return None
    filtered = [x for x in value if isinstance(x, str) and x in _FALLBACK_ON_ALLOWED]
    if value and not filtered:
        return None
    return filtered


# key -> callable(raw) -> clamped value or None (None ⇒ drop the key).
_CONFIG_VALIDATORS = {
    # Online / shared LLM plumbing (also reused by the agent loop for local).
    "online_timeout_s": lambda v: coerce_int_in_range(v, 5, 600),
    "online_max_retries": lambda v: coerce_int_in_range(v, 0, 10),
    "online_max_tokens": lambda v: coerce_int_in_range(v, 64, 32768),
    "agent_wall_clock_s": lambda v: coerce_int_in_range(v, 30, 1800),
    "local_request_timeout_s": lambda v: coerce_int_in_range(v, 0, 3600),
    "fallback_provider": _coerce_fallback_provider,
    # Vault chat generation / retrieval knobs.
    "vault_top_k": lambda v: coerce_int_in_range(v, 1, 32),
    "vault_similarity_cutoff": lambda v: coerce_float_in_range(v, 0.0, 1.0),
    "vault_prompt_mode": lambda v: coerce_enum(
        v, ("strict", "balanced", "exploratory", "concise")),
    "vault_chat_temperature": lambda v: coerce_float_in_range(v, 0.0, 2.0),
    "vault_hybrid_enabled": coerce_bool,
    "vault_reranker_enabled": coerce_bool,
    "vault_reranker_device": lambda v: coerce_enum(v, ("auto", "cpu", "mps")),
    "vault_mmr_enabled": coerce_bool,
    "vault_mmr_lambda": lambda v: coerce_float_in_range(v, 0.1, 0.9),
    "vault_query_expansion": coerce_bool,
    "vault_num_queries": lambda v: coerce_int_in_range(v, 1, 5),
    "vault_rerank_pool_ceiling": lambda v: coerce_int_in_range(v, 10, 200),
    "vault_agent_enabled": coerce_bool,
    "vault_agent_max_iterations": lambda v: coerce_int_in_range(v, 1, 12),
    "vault_vector_backend": lambda v: coerce_enum(v, ("simple", "lancedb")),
    "vault_prewarm_enabled": coerce_bool,
    # Single Paper (ranges mirror core/utils.py parse_* clamps).
    "paper_temperature": lambda v: coerce_float_in_range(v, 0.0, 2.0),
    "paper_num_ctx": lambda v: coerce_int_in_range(v, 512, 131072),
    "paper_max_tokens": lambda v: coerce_int_in_range(v, 64, 32768),
    "paper_top_p": lambda v: coerce_float_in_range(v, 0.0, 1.0),
    "paper_repeat_penalty": lambda v: coerce_float_in_range(v, 0.5, 2.0),
    # Deck Generator (ranges mirror api/routes/deck.py _*_MIN/_*_MAX).
    "deck_temperature": lambda v: coerce_float_in_range(v, 0.0, 2.0),
    "deck_max_sections": lambda v: coerce_int_in_range(v, 1, 20),
    "deck_agent_max_iterations": lambda v: coerce_int_in_range(v, 1, 12),
    # fallback_on handled separately (list-valued).
}


def _validate_llm_config_keys(data: dict) -> None:
    """Clamp/drop the LLM-parameter keys in-place before save_config."""
    for key, validator in _CONFIG_VALIDATORS.items():
        if key in data:
            coerced = validator(data[key])
            if coerced is None:
                data.pop(key)
            else:
                data[key] = coerced
    if "fallback_on" in data:
        fo = _coerce_fallback_on(data["fallback_on"])
        if fo is None:
            data.pop("fallback_on")
        else:
            data["fallback_on"] = fo

@config_bp.route("/api/config")
def api_get_config():
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    return jsonify(load_config())

@config_bp.route("/api/config", methods=["POST"])
def api_save_config():
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400
    if "obsidian_vault_path" in data:
        try:
            data["obsidian_vault_path"] = _validate_vault_path(
                str(data.get("obsidian_vault_path") or "")
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        obsidian_manager.restore_vault_path(data["obsidian_vault_path"])
    if "vault_exclude_dirs" in data:
        data["vault_exclude_dirs"] = _normalise_vault_exclude_dirs(
            data.get("vault_exclude_dirs"),
            data.get("obsidian_vault_path") or obsidian_manager.get_vault_path() or load_config().get("obsidian_vault_path", ""),
        )
    if "vault_image_exts" in data:
        data["vault_image_exts"] = _normalise_vault_image_exts(
            data.get("vault_image_exts")
        )
    if any(key in data for key in ("provider", "llm", "embed", "openai_model", "anthropic_model", "google_model")):
        clear_provider_warnings()
    # When the active provider is online, route the generic "llm" field
    # into its per-provider companion so per-provider selections survive
    # toggling. The UI continues to send "llm" so this is transparent.
    # The "llm" key itself is then dropped from the payload: persisting an
    # online model name into "llm" would clobber the LOCAL provider's saved
    # selection (the bug that left llm="claude-sonnet-4-6" with
    # provider="ollama" in a real config).
    if "llm" in data:
        from core.config import is_online_provider
        active = (data.get("provider") or load_config().get("provider") or "ollama").strip().lower()
        if is_online_provider(active):
            key = {
                "openai": "openai_model",
                "anthropic": "anthropic_model",
                "google": "google_model",
            }[active]
            data.setdefault(key, data["llm"])
            data.pop("llm")
    if any(key in data for key in ("ocr_provider", "ocr_model", "vision_provider", "vision_model")):
        from services.vision import glm_ocr_manager, vision_manager
        if data.get("ocr_provider"):
            glm_ocr_manager.set_provider(str(data["ocr_provider"]))
        if data.get("ocr_model"):
            glm_ocr_manager.set_model(str(data["ocr_model"]))
        if data.get("vision_provider"):
            vision_manager.set_provider(str(data["vision_provider"]))
        if data.get("vision_model"):
            vision_manager.set_model(str(data["vision_model"]))
    # Library Audit settings have a dedicated, validated endpoint
    # (POST /api/audit/config — path-traversal and absolute-path checks).
    # Strip them here so the generic save path cannot be used to bypass
    # those validators; the audit UI (audit.js) never posts here.
    audit_keys = [key for key in data if key.startswith("audit_")]
    for key in audit_keys:
        data.pop(key)
    # Clamp/validate the LLM-parameter keys the Settings window writes; an
    # out-of-range or malformed value is dropped so the prior value survives.
    _validate_llm_config_keys(data)
    save_config(data)
    return jsonify({"ok": True})

def _validate_vault_path(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    try:
        path = Path(raw).expanduser().resolve()
    except OSError as exc:
        raise ValueError("Vault path could not be resolved") from exc
    if not path.is_dir():
        raise ValueError("Vault path must be an existing directory")
    if path in EXACT_BLOCKED:
        raise ValueError("Refusing to index broad system or home root")
    if any(path == root or root in path.parents for root in SYSTEM_ROOTS):
        raise ValueError("Refusing to index system directory")
    return str(path)

def _normalise_vault_exclude_dirs(entries, vault_path: str) -> list[str]:
    if not isinstance(entries, list):
        return []

    vault_root = None
    if vault_path:
        try:
            vault_root = Path(vault_path).expanduser().resolve()
        except OSError:
            vault_root = None

    normalised: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        raw = str(entry).strip()
        if not raw:
            continue

        path = Path(raw).expanduser()
        try:
            if path.is_absolute():
                if vault_root is None:
                    continue
                rel = path.resolve().relative_to(vault_root)
            else:
                rel = Path(raw)
        except (OSError, ValueError):
            continue

        rel_str = rel.as_posix().strip("/")
        if not rel_str or ".." in Path(rel_str).parts:
            continue
        if rel_str not in seen:
            normalised.append(rel_str)
            seen.add(rel_str)
    return normalised

_VAULT_IMAGE_EXT_BODY = re.compile(r"[a-z0-9]{1,16}")
_VAULT_IMAGE_EXTS_MAX = 64
_VAULT_RESERVED_EXTS = VAULT_MD_EXTS | VAULT_BINARY_EXTS

def _normalise_vault_image_exts(entries) -> list[str]:
    if not isinstance(entries, list):
        return []
    normalised: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        raw = str(entry).strip().lower()
        if not raw:
            continue
        if raw.startswith("."):
            body = raw[1:]
        else:
            body = raw
        if not _VAULT_IMAGE_EXT_BODY.fullmatch(body):
            continue
        ext = "." + body
        # Reject markdown and PDF extensions so a user-saved list cannot
        # reroute core vault files into the image/vision branch.
        if ext in _VAULT_RESERVED_EXTS:
            continue
        if ext in seen:
            continue
        normalised.append(ext)
        seen.add(ext)
        if len(normalised) >= _VAULT_IMAGE_EXTS_MAX:
            break
    return normalised

@config_bp.route("/api/report_types")
@config_bp.route("/api/report-types")
def api_get_report_types():
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    report_types = load_report_types()
    if request.path.endswith("report_types"):
        return jsonify(report_types)
    return jsonify({"report_types": report_types})

@config_bp.route("/api/pull", methods=["POST"])
def api_pull():
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    # Don't wrap data.get("model") in str(...): coerce_regex rejects non-string
    # inputs, but stringifying first would turn JSON null into "None", which
    # matches the regex and would hit the Ollama API with a literal "None"
    # model name.  Strip whitespace only when the input is actually a string.
    raw_model = data.get("model")
    if isinstance(raw_model, str):
        raw_model = raw_model.strip()
    model = coerce_regex(raw_model, r"[A-Za-z0-9._:/-]{1,128}")
    if model is None:
        return jsonify({"error": "Invalid model name"}), 400
    if load_config().get("provider", "ollama") == "lm_studio":
        return jsonify({"error": "Model pulling is only supported for Ollama"}), 400

    def generate():
        try:
            import ollama
            for chunk in ollama.pull(model, stream=True):
                # ollama SDK yields Pydantic model instances; json.dumps() cannot
                # serialize them directly — use model_dump() (Pydantic v2) with a
                # fallback to dict() for older SDK versions.
                chunk_data = chunk.model_dump() if hasattr(chunk, "model_dump") else dict(chunk)
                yield f"data: {json.dumps(chunk_data)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream")

@config_bp.route("/api/reset", methods=["POST"])
def api_reset():
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != "reset":
        return jsonify({"error": "Confirmation token required"}), 400

    deleted: list[str] = []
    obsidian_manager.request_stop()
    # Wait for the indexer to finish its final persist before we touch the
    # storage directory.  Refusing the reset on timeout is safer than racing
    # the indexer: a stray persist after rmtree would recreate the directory
    # with partial state on disk.
    finished = obsidian_manager.wait_for_indexing(timeout=30.0)
    if not finished:
        return jsonify({
            "error": (
                "Indexing did not stop within 30 seconds. Try again, or "
                "press Cancel on the Obsidian tab and wait for it to settle."
            ),
        }), 503
    obsidian_manager.cleanup()
    obsidian_manager.force_release()
    obsidian_manager.clear_status_messages()

    # Audit subsystem: atomic reset — signal any in-flight scan to
    # abort, bump the run-id so a worker finishing mid-reset cannot
    # repopulate the inventory, drop cached results, return to ``idle``.
    # /api/audit/inventory then returns 404, matching the post-reset
    # "no scan yet" empty state on the Library Audit tab.  Local import
    # so a packaging error in the audit module cannot break the core
    # reset flow.
    try:
        from audit.manager import audit_manager
        audit_manager.reset_to_idle()
    except Exception as exc:  # pragma: no cover - defensive
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "audit_manager reset skipped: %s", exc
        )

    with DB_LOCK:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM uploads")
    deleted.append("uploads_db")

    if os.path.isdir(OBSIDIAN_INDEX_DIR):
        log_storage_deletion("api_reset")
        shutil.rmtree(OBSIDIAN_INDEX_DIR)
        deleted.append("obsidian_storage")

    app_module = sys.modules.get("app")
    feedback_file = getattr(app_module, "FEEDBACK_FILE", FEEDBACK_FILE)
    config_file = getattr(app_module, "CONFIG_FILE", CONFIG_FILE)

    if data.get("wipe_feedback") and os.path.exists(feedback_file):
        os.remove(feedback_file)
        deleted.append("feedback.jsonl")

    if data.get("wipe_config") and os.path.exists(config_file):
        os.remove(config_file)
        deleted.append("config.json")

    init_db()
    return jsonify({"ok": True, "deleted": deleted})

_LOG_RATE_WINDOW_S = 60.0
_LOG_RATE_MAX = 100
_log_rate_lock = threading.Lock()
_log_rate_bucket: list[float] = []


@config_bp.route("/api/log", methods=["POST"])
def api_log():
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403

    # Rolling-window cap: a runaway frontend cannot saturate the log file.
    now = time.monotonic()
    with _log_rate_lock:
        cutoff = now - _LOG_RATE_WINDOW_S
        _log_rate_bucket[:] = [t for t in _log_rate_bucket if t >= cutoff]
        if len(_log_rate_bucket) >= _LOG_RATE_MAX:
            return jsonify({"ok": True, "throttled": True})
        _log_rate_bucket.append(now)

    data = request.get_json(silent=True) or {}
    msg = str(data.get("msg", "")).strip()[:500]
    level = str(data.get("level", "info")).lower()
    if msg:
        import logging as _logging
        _logger = _logging.getLogger("chatekld.js")
        if level == "error":
            _logger.warning("[JS] %s", msg)
        else:
            _logger.debug("[JS] %s", msg)
    return jsonify({"ok": True})
