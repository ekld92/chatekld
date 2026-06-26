"""Config / models-pull / reset / JS-log endpoints.

This blueprint owns the read/write side of ``config.json`` plus a handful of
adjacent "app-management" routes:

- ``GET/POST /api/config`` — the generic settings persistence path. The POST
  side runs several guards before ``save_config``: it validates the vault path,
  normalises ``vault_exclude_dirs`` / ``vault_image_exts``, routes the generic
  ``llm`` field into the active online provider's per-provider key (then drops
  ``llm`` so an online model name cannot clobber the local selection), strips
  ``audit_*`` keys (those have a dedicated validated endpoint), and finally
  clamps/drops every numeric/enum/bool LLM knob via ``_validate_llm_config_keys``.
  API keys are never accepted/returned here (they live in env vars).
- ``GET /api/report_types`` / ``/api/report-types`` — built-in + saved report
  types (the hyphen form wraps them in ``{"report_types": [...]}``).
- ``POST /api/pull`` — Ollama-only model pull, streamed as SSE.
- ``POST /api/reset`` — confirmation-gated teardown of indexes / DB / optional
  config + feedback files.
- ``POST /api/log`` — rate-limited sink for frontend JS errors.

Every route is gated by :func:`api.security.origin_is_local` (403 otherwise).
"""
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
    SYSTEM_PROMPT_LIMIT,
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
    coerce_string_max_len,
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


# A HuggingFace repo id is ``name`` or ``namespace/name``: each segment must
# START with an alphanumeric and otherwise contains only ``[A-Za-z0-9._-]``,
# with AT MOST ONE ``/`` and no leading/trailing slash. Enforcing that shape —
# rather than a loose character class that happens to include ``.`` and ``/`` —
# is what actually rejects path-like values: a leading ``/`` leaves the first
# segment empty (no match), ``a//b`` introduces a second slash (no match), and
# the ``..`` guard in ``_coerce_reranker_model`` blocks traversal components. So
# a malformed value can never be persisted and later handed to
# ``SentenceTransformer`` as a local filesystem path. The regex is unbounded per
# segment; the overall length is capped separately in the coercer.
_RERANKER_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?")


def _coerce_reranker_model(value):
    """Allow "" (no reranker model) or a HuggingFace repo id; else None.

    This is the one Settings-window string that triggers a side effect — the
    cross-encoder weights download to ``~/.cache/huggingface`` on the next chat
    — so it is shape-validated here even though ``/api/config`` is local-origin
    gated, as defence in depth against a malformed value being stored and later
    handed to ``SentenceTransformer`` (which would otherwise treat an *existing*
    local path as a model directory). Path-like inputs — a leading slash,
    multiple slashes, or any ``..`` traversal component — are rejected, leaving
    only ``name`` / ``namespace/name`` shapes (verified by the stress cases in
    ``_RERANKER_MODEL_RE`` above). An empty value is kept (it disables the
    reranker-model lookup); the stored value is stripped to match how
    ``_resolve_chat_params`` reads it back.
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s == "":
        return ""
    # Overall length cap (the regex bounds neither segment) plus an explicit
    # no-``..`` rule, so a traversal component can never survive even when it
    # sits *inside* a single segment (e.g. ``a..b``), which the regex alone
    # would accept as an ordinary dotted name.
    if len(s) > 128 or ".." in s:
        return None
    return s if _RERANKER_MODEL_RE.fullmatch(s) else None


def _coerce_vault_rel_subdir(value):
    """Shape-validate a vault-relative sub-folder (no traversal/abs/NUL).

    Existence + under-root checks happen at request time in
    ``api/routes/refactor.py`` (the vault may be unset when config is saved), so
    this only guards the stored *shape* — the same posture as the ``audit_*``
    subdir keys. Empty ⇒ None (drop; keep the prior default).
    """
    if not isinstance(value, str):
        return None
    s = value.strip().replace("\\", "/")
    if not s or len(s) > 1024:
        return None
    if any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    # Reject absolute / traversal BEFORE normalising away leading slashes, so a
    # leading "/" can never be silently stripped into a valid-looking relative.
    if os.path.isabs(s) or s.startswith("/"):
        return None
    parts = [p for p in s.split("/") if p]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _coerce_model_name(value):
    """Allow "" (use vision_model) or a bounded, control-char-free model id."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s == "":
        return ""
    if len(s) > 120 or any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    return s


def _coerce_abs_dir_or_empty(value):
    """Allow "" (use the default archive dir) or an ABSOLUTE, control-char-free path.

    Shape-only: a relative path is rejected here, but "is this dir inside the
    vault?" is re-checked at apply time in api/routes/refactor.py (the vault may
    be unset when config is saved, mirroring the audit_* / refactor_scope_subdir
    posture). ``~`` is allowed (expanded at use). Empty ⇒ "" (kept, not dropped)
    so a user can clear the override back to the default.
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s == "":
        return ""
    if len(s) > 4096 or any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    # Accept a leading ~ (home) as absolute-equivalent; otherwise require an
    # absolute path so a relative archive dir can never resolve under the CWD.
    if not (s.startswith("~") or os.path.isabs(s)):
        return None
    return s


# key -> callable(raw) -> clamped value or None (None ⇒ drop the key).
_CONFIG_VALIDATORS = {
    # Online / shared LLM plumbing (also reused by the agent loop for local).
    "online_timeout_s": lambda v: coerce_int_in_range(v, 5, 600),
    "online_max_retries": lambda v: coerce_int_in_range(v, 0, 10),
    "online_max_tokens": lambda v: coerce_int_in_range(v, 64, 32768),
    # Assumed chat-model context window: sizes the local LLM context and the
    # retrieval-breadth autoscaler. No Settings control (advanced/internal), but
    # validated here so a hand-edited / generic-POST garbage value can't reach
    # the engine. Range mirrors paper_num_ctx.
    "context_window": lambda v: coerce_int_in_range(v, 512, 131072),
    "agent_wall_clock_s": lambda v: coerce_int_in_range(v, 30, 1800),
    "local_request_timeout_s": lambda v: coerce_int_in_range(v, 0, 3600),
    # Vision / OCR call bounds (always on — min 5, no "0 = off").
    "vision_timeout_s": lambda v: coerce_int_in_range(v, 5, 600),
    "vision_max_tokens": lambda v: coerce_int_in_range(v, 64, 8192),
    "ocr_max_tokens": lambda v: coerce_int_in_range(v, 64, 8192),
    "fallback_provider": _coerce_fallback_provider,
    # Vault chat generation / retrieval knobs.
    "vault_top_k": lambda v: coerce_int_in_range(v, 1, 32),
    "vault_similarity_cutoff": lambda v: coerce_float_in_range(v, 0.0, 1.0),
    "vault_prompt_mode": lambda v: coerce_enum(
        v, ("strict", "balanced", "exploratory", "concise")),
    "vault_chat_temperature": lambda v: coerce_float_in_range(v, 0.0, 2.0),
    "vault_hybrid_enabled": coerce_bool,
    "vault_reranker_enabled": coerce_bool,
    "vault_reranker_model": _coerce_reranker_model,
    "vault_reranker_device": lambda v: coerce_enum(v, ("auto", "cpu", "mps")),
    "vault_mmr_enabled": coerce_bool,
    "vault_mmr_lambda": lambda v: coerce_float_in_range(v, 0.1, 0.9),
    "vault_query_expansion": coerce_bool,
    "vault_num_queries": lambda v: coerce_int_in_range(v, 1, 5),
    "vault_rerank_pool_ceiling": lambda v: coerce_int_in_range(v, 10, 200),
    "vault_wikilink_expansion": coerce_bool,
    "vault_wikilink_neighbor_cap": lambda v: coerce_int_in_range(v, 1, 100),
    "vault_wikilink_node_cap": lambda v: coerce_int_in_range(v, 1, 200),
    "vault_wikilink_score_decay": lambda v: coerce_float_in_range(v, 0.0, 1.0),
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
    # Plain Chat (RAG-free panel). chat_system_prompt is the FULL system prompt
    # (no retrieval grounding to protect), capped at the shared limit; a non-str
    # is dropped (prior kept) while an empty string is a valid "no system
    # prompt" and is persisted as-is, matching coerce_string_max_len semantics.
    "chat_temperature": lambda v: coerce_float_in_range(v, 0.0, 2.0),
    "chat_system_prompt": lambda v: coerce_string_max_len(v, SYSTEM_PROMPT_LIMIT),
    # Note Refactor. Scope shape only; existence checked at request time.
    # refactor_extract_model "" ⇒ fall back to vision_model.
    "refactor_scope_subdir": _coerce_vault_rel_subdir,
    "refactor_extract_model": _coerce_model_name,
    "refactor_table_double_read": coerce_bool,
    # Phase 2 vault-write knobs. archive_dir is shape-only (abs or ""); the
    # not-inside-vault check is re-applied at apply time. thumb_max_side bounds
    # the in-vault thumbnail's longest side.
    "refactor_archive_dir": _coerce_abs_dir_or_empty,
    "refactor_thumb_max_side": lambda v: coerce_int_in_range(v, 96, 1024),
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
    """Return the persisted config verbatim.

    ``load_config`` never includes API keys (they live only in env vars), so
    this is safe to hand to the local UI as-is. Local-origin gated.
    """
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    return jsonify(load_config())

@config_bp.route("/api/config", methods=["POST"])
def api_save_config():
    """Persist a partial config update after layered validation.

    The body is a partial ``{key: value}`` patch (merged into the existing
    config by ``save_config``, not a full replacement). Before persisting, the
    handler applies, in order:

    1. Vault-path validation (rejects broad/system roots) — a bad path 400s the
       whole request rather than dropping the key, since it is user-visible.
    2. ``vault_exclude_dirs`` / ``vault_image_exts`` normalisation.
    3. Provider-warning clearing when any provider/model field changes.
    4. The ``llm`` → per-provider-key routing for online providers (then ``llm``
       is dropped so an online model name can't overwrite the local selection).
    5. OCR/vision manager singleton updates (so a model change applies live).
    6. ``audit_*`` key stripping (those go through ``/api/audit/config``).
    7. ``_validate_llm_config_keys`` — clamp/drop the numeric/enum/bool knobs.

    Local-origin gated; 400 on non-JSON or an invalid vault path.
    """
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
    """Resolve + safety-check the Obsidian vault path before persistence.

    An empty value is allowed (clears the vault). A non-empty value must
    resolve to an existing directory that is neither a known broad root
    (``EXACT_BLOCKED`` — e.g. ``$HOME``) nor inside a system tree
    (``SYSTEM_ROOTS``); any violation raises ``ValueError`` (turned into a 400
    by the caller). Returns the absolute, symlink-resolved path string.
    """
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
    """Coerce the exclude-dirs list to deduped, vault-relative POSIX paths.

    Absolute entries are rebased under the vault root (and dropped if they fall
    outside it, or if the vault root is unknown); relative entries are kept as
    given. Empty entries, ``..`` traversal, and duplicates are filtered out.
    The stored form is always vault-relative so exclusions survive a vault move
    and are applied before any file is read by the indexer.
    """
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
    """Coerce the image-extension allow-list to deduped, dotted, lowercase exts.

    Each entry is lowercased, a leading ``.`` is tolerated, and the body must be
    1-16 alphanumerics (``_VAULT_IMAGE_EXT_BODY``). Markdown/PDF extensions
    (``_VAULT_RESERVED_EXTS``) are rejected so a user-saved list can never
    reroute core vault files into the image/vision branch. Capped at
    ``_VAULT_IMAGE_EXTS_MAX`` entries; ``[]`` disables image indexing entirely.
    """
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
    """Return the built-in + saved/overridden report types.

    Two URL shapes share one handler for backwards compatibility: the legacy
    underscore form ``/api/report_types`` returns the bare list, while the
    hyphen form ``/api/report-types`` wraps it as ``{"report_types": [...]}``.
    ``load_report_types`` merges built-ins with custom/overridden saved types.
    """
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    report_types = load_report_types()
    if request.path.endswith("report_types"):
        return jsonify(report_types)
    return jsonify({"report_types": report_types})

@config_bp.route("/api/pull", methods=["POST"])
def api_pull():
    """Pull an Ollama model, streaming progress as SSE (Ollama-only).

    The model name is shape-validated with ``coerce_regex`` (which rejects
    non-strings) BEFORE any ``str()`` so a JSON ``null`` cannot become the
    literal ``"None"`` and be sent to the Ollama API. LM Studio has no pull
    concept, so the route 400s when the active provider is ``lm_studio``.
    Each streamed chunk is a Pydantic model serialised via ``model_dump()``
    (Pydantic v2) with a ``dict()`` fallback; the stream ends with ``[DONE]``.
    """
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
    """Confirmation-gated teardown of indexes, the uploads DB, and audit state.

    Requires ``{"confirm": "reset"}`` (a literal token, not just truthiness) so
    a stray POST cannot wipe data. Sequence: stop the indexer and *wait* for its
    final persist (refuse with 503 on timeout rather than race a stray persist
    that would re-create the storage dir with partial state), release its lock,
    reset the audit subsystem to idle, clear the ``uploads`` table, and
    ``rmtree`` the Obsidian storage dir (logged via ``log_storage_deletion``).
    ``wipe_feedback`` / ``wipe_config`` optionally delete those files too — both
    resolved through the (test-patchable) ``app.FEEDBACK_FILE`` / ``CONFIG_FILE``
    attributes. Returns the list of deleted artefacts.
    """
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
    """Sink for frontend JS errors (``logError`` in ``api.js``).

    Rate-limited to ``_LOG_RATE_MAX`` messages per ``_LOG_RATE_WINDOW_S`` so a
    runaway frontend cannot saturate ``chatekld.log`` (a throttled call returns
    ``{"throttled": true}`` and is otherwise a no-op). The message is capped at
    500 chars and routed to the ``chatekld.js`` logger — ``error`` level logs at
    WARNING, everything else at DEBUG. Local-origin gated.
    """
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
    # Strip CR/LF before logging so a crafted frontend message cannot forge
    # extra chatekld.log lines (e.g. a fake "VAULT WRITE"/deletion marker),
    # and run it through redact() so the otherwise-uniform "redact before
    # logging" discipline holds for this sink too.
    from core.llm.redact import redact
    raw = str(data.get("msg", "")).replace("\r", " ").replace("\n", " ")
    msg = redact(raw).strip()[:500]
    level = str(data.get("level", "info")).lower()
    if msg:
        import logging as _logging
        _logger = _logging.getLogger("chatekld.js")
        if level == "error":
            _logger.warning("[JS] %s", msg)
        else:
            _logger.debug("[JS] %s", msg)
    return jsonify({"ok": True})
