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

All routes are gated by the app-level before_request origin guard
(api.security.register_origin_guard; 403 otherwise).
"""
import json
import math
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from flask import Blueprint, jsonify, request, Response
from core.config import load_config, save_config, load_report_types
from core.constants import (
    ACCESSION_BATCH_MAX_CEILING,
    CONFIG_FILE,
    EXACT_BLOCKED,
    FEEDBACK_FILE,
    LOG_FILE,
    OBSIDIAN_CACHE_DIR,
    OBSIDIAN_INDEX_DIR,
    SYSTEM_PROMPT_LIMIT,
    SYSTEM_ROOTS,
    VAULT_MD_EXTS,
    VAULT_BINARY_EXTS,
)
from core.database import DB_LOCK, get_db_connection, init_db
from core.utils import log_storage_deletion
from core.providers.server import clear_provider_warnings
from api.security import sanitise_error_msg
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
_PROVIDER_NAMES = (
    "ollama", "lm_studio", "openai", "anthropic", "google", "deepseek", "zai",
    "openrouter",
)
_FALLBACK_ON_ALLOWED = ("timeout", "network", "rate_limit", "server_error")


def _coerce_fallback_provider(value):
    """Allow "" (fallback disabled) or a known provider name; else None."""
    if isinstance(value, str):
        s = value.strip().lower()
        if s == "" or s in _PROVIDER_NAMES:
            return s
    return None


#: The accession source registry names (`accession/sources.py::default_sources`
#: order). Kept as a literal here rather than imported so loading the config
#: route never imports the network module; `test_accession_route.py` pins the
#: two lists against each other.
ACCESSION_SOURCE_NAMES = ("crossref", "europepmc", "arxiv", "hal", "thesesfr",
                          "openlibrary", "googlebooks")


def _coerce_accession_sources(value):
    """Filter to known source names, preserving order; empty/invalid ⇒ drop.

    Unlike ``fallback_on``, an empty list is NOT meaningful here — resolving
    against no sources is never a choice, so it drops like any garbage value.
    """
    if not isinstance(value, list):
        return None
    filtered = [x for x in value if isinstance(x, str) and x in ACCESSION_SOURCE_NAMES]
    # de-dup, order-preserving
    filtered = list(dict.fromkeys(filtered))
    return filtered or None


def _coerce_contact_email(value):
    """Loose e-mail shape (it only rides a User-Agent header); "" kept."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s == "":
        return ""
    if len(s) > 200 or any(c in s for c in "\x00\n\r\t "):
        return None
    return s if re.fullmatch(r"[^@]+@[^@]+\.[^@]+", s) else None


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


# An optional http(s):// scheme, then a host (alnum/dot/hyphen, or a bracketed
# IPv6), an optional :port, and an optional single trailing slash. The resolver
# (``core.providers.base._resolve_local_host``) prepends ``http://`` to a
# scheme-less value, so a bare ``host:port`` is accepted here too.
_LOCAL_HOST_RE = re.compile(r"(?:https?://)?[A-Za-z0-9._\-\[\]]+(?::\d{1,5})?/?")


def _coerce_local_host(value):
    """Allow "" (fall back to env/constant) or an http(s) ``host[:port]``; else None.

    Used for ``ollama_host`` / ``lm_studio_host``. Shape-validated as defence in
    depth (these become the base URL handed to httpx / the OpenAI SDK), even
    though ``/api/config`` is local-origin gated, so a hand-edited or
    generic-POST garbage value can never be persisted and dialed. Empty is kept
    (disables the override → the resolver uses the env var then the constant).
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s == "":
        return ""
    if len(s) > 200:
        return None
    return s if _LOCAL_HOST_RE.fullmatch(s) else None


def _coerce_http_url(value):
    """Allow "" (fall back to the built-in default) or an http(s) URL; else None.

    For a stored dial target that legitimately carries a PATH (the Zotero
    local-API base, ``http://127.0.0.1:23119/api``) — unlike ``ollama_host``,
    which is host[:port] only. Shape gate (2026-07-20 audit M3): scheme must be
    http/https, no whitespace/control chars, bounded length. Defence in depth
    over the local-origin gate on ``/api/config``, since this becomes the base
    URL a urllib client dials (an SSRF surface).
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s == "":
        return ""
    if len(s) > 200 or any(ch in s for ch in ("\x00", "\n", "\r", " ", "\t")):
        return None
    parsed = urlparse(s)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return s


def _coerce_https_url(value):
    """Like ``_coerce_http_url`` but **https only** (E2, 2026-07-20 audit).

    Used for ``tagging_zotero_web_api_base``, which carries the ZOTERO_API_KEY in
    a request header. Allowing plain ``http`` there is a cleartext-credential
    dial target; the loopback local base keeps ``http`` (127.0.0.1) via
    ``_coerce_http_url``. ``resolve_transport`` re-validates at call time too, so
    a hand-edited config can't bypass this.
    """
    out = _coerce_http_url(value)
    if out in (None, ""):
        return out
    if urlparse(out).scheme != "https":
        return None
    return out


def _coerce_vault_rel_subdir(value):
    """Shape-validate a vault-relative sub-folder (no traversal/abs/NUL).

    Existence + under-root checks happen at request time;
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


def _coerce_vault_rel_file(value):
    """Allow "" (slot disabled) or a vault-relative file path (no traversal/abs/NUL).

    Twin of :func:`_coerce_vault_rel_subdir` but keeps an empty string (the
    thesaurus loader reads "" as "skip this file") instead of dropping it, so a
    user can clear ``vault_thesaurus_abbrev_path`` / ``vault_thesaurus_tags_path``
    back to off. Shape-only — the under-root realpath check happens at read time
    in ``ObsidianVaultManager._get_thesaurus`` (the vault may be unset on save).
    """
    if not isinstance(value, str):
        return None
    s = value.strip().replace("\\", "/")
    if s == "":
        return ""
    if len(s) > 1024 or any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
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


def _coerce_online_model(value):
    """A bounded, control-char-free online model id; empty is DROPPED.

    The per-provider ``*_model`` keys (the routing target of the UI's generic
    ``llm`` field) previously persisted any string and only failed at call time
    (audit finding #25). Unlike ``_coerce_model_name`` (where "" means "use the
    chat model"), an empty online selection is meaningless — drop it so the
    prior persisted value survives.
    """
    coerced = _coerce_model_name(value)
    return coerced or None


def _coerce_abs_dir_or_empty(value):
    """Allow "" (use the default archive dir) or an ABSOLUTE, control-char-free path.

    Shape-only: relative paths are rejected here; containment is checked
    by the history writer at request time. ``~`` is allowed (expanded at use). Empty ⇒ "" (kept, not dropped)
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



# Bounds for llm_pricing_overrides: enough for every provider's curated list
# plus custom models, far below anything that could bloat config.json.
_PRICING_OVERRIDES_MAX_MODELS = 64
_PRICING_OVERRIDES_MAX_KEY_LEN = 200
_PRICING_OVERRIDES_FIELDS = ("input", "output", "cached_input")


def _coerce_pricing_overrides(value):
    """Validated copy of an ``llm_pricing_overrides`` dict, or None to drop.

    Numbers are float()ed at WRITE time (quoted numbers from hand-edited JSON
    are the common mistake), matching what ``estimate_cost_usd`` expects, so a
    malformed entry can never reach the costing hot path.
    """
    if not isinstance(value, dict) or len(value) > _PRICING_OVERRIDES_MAX_MODELS:
        return None
    out: dict = {}
    for model, row in value.items():
        if not isinstance(model, str) or not model.strip() \
                or len(model) > _PRICING_OVERRIDES_MAX_KEY_LEN:
            return None
        if not isinstance(row, dict):
            return None
        entry: dict = {}
        for field in _PRICING_OVERRIDES_FIELDS:
            if field not in row:
                continue
            raw = row[field]
            if field == "cached_input" and raw is None:
                entry[field] = None
                continue
            try:
                num = float(raw)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(num) or num < 0 or num > 1_000_000:
                return None
            entry[field] = num
        # input+output are what estimate_cost_usd requires to apply a row.
        if "input" not in entry or "output" not in entry:
            return None
        out[model.strip()] = entry
    return out


# key -> callable(raw) -> clamped value or None (None ⇒ drop the key).
_CONFIG_VALIDATORS = {
    # Online / shared LLM plumbing (also reused by the agent loop for local).
    "online_timeout_s": lambda v: coerce_int_in_range(v, 5, 600),
    "online_max_retries": lambda v: coerce_int_in_range(v, 0, 10),
    "online_max_tokens": lambda v: coerce_int_in_range(v, 64, 32768),
    "online_reasoning_effort": lambda v: v if v in ("provider_default", "low", "minimal") else None,
    # Online single-paper document bound (map-reduce trigger). Config-only, no
    # Settings control; floor 4000 keeps a garbage value from forcing every
    # paper through map-reduce.
    "online_doc_window_tokens": lambda v: coerce_int_in_range(v, 4000, 2_000_000),
    # Per-provider online model selections (the routing target of the UI's
    # generic `llm` field). Shape-only; a garbage id previously persisted and
    # failed only at call time.
    "openai_model": _coerce_online_model,
    "anthropic_model": _coerce_online_model,
    "google_model": _coerce_online_model,
    "deepseek_model": _coerce_online_model,
    "zai_model": _coerce_online_model,
    "openrouter_model": _coerce_online_model,
    # Assumed chat-model context window: sizes the local LLM context and the
    # retrieval-breadth autoscaler. No Settings control (advanced/internal), but
    # validated here so a hand-edited / generic-POST garbage value can't reach
    # the engine. Range mirrors paper_num_ctx.
    "context_window": lambda v: coerce_int_in_range(v, 512, 131072),
    "agent_wall_clock_s": lambda v: coerce_int_in_range(v, 30, 1800),
    "local_request_timeout_s": lambda v: coerce_int_in_range(v, 0, 3600),
    # Local-backend base URLs (config → env → constant resolution in
    # core.providers.base). Empty keeps the default; URL-shape validated.
    "ollama_host": _coerce_local_host,
    "lm_studio_host": _coerce_local_host,
    # Vision / OCR call bounds (always on — min 5, no "0 = off").
    "vision_timeout_s": lambda v: coerce_int_in_range(v, 5, 600),
    "vision_max_tokens": lambda v: coerce_int_in_range(v, 64, 8192),
    "ocr_max_tokens": lambda v: coerce_int_in_range(v, 64, 8192),
    # Failed-call fast-fail window; 0 legitimately disables (retry every image).
    "vision_failure_cooldown_s": lambda v: coerce_int_in_range(v, 0, 600),
    "fallback_provider": _coerce_fallback_provider,
    # B7 (2026-07-13): the active provider selectors + free-string model ids were
    # previously UNVALIDATED — a garbage `provider` persisted and surfaced only as
    # a call-time error, and `llm`/`embed`/`ocr_model`/`vision_model` accepted any
    # string. Enum-check the providers (invalid ⇒ dropped, prior survives) and
    # bound the model ids (empty kept, control-char-free, ≤120 chars).
    "provider": lambda v: coerce_enum(v, _PROVIDER_NAMES),
    "ocr_provider": lambda v: coerce_enum(v, ("ollama", "lm_studio")),
    "vision_provider": lambda v: coerce_enum(v, ("ollama", "lm_studio")),
    "online_ocr_provider": lambda v: coerce_enum(v, ("deepseek", "openai", "google", "openrouter")),
    "online_vision_provider": lambda v: coerce_enum(v, ("deepseek", "openai", "google", "openrouter")),
    "llm": _coerce_model_name,
    "embed": _coerce_model_name,
    "embed_provider": lambda v: coerce_enum(v, ("ollama", "lm_studio")),
    "ocr_model": _coerce_model_name,
    "vision_model": _coerce_model_name,
    "online_ocr_model": _coerce_model_name,
    "online_vision_model": _coerce_model_name,
    "vault_online_visual_enabled": coerce_bool,
    # Vault chat generation / retrieval knobs.
    "vault_top_k": lambda v: coerce_int_in_range(v, 1, 32),
    "vault_similarity_cutoff": lambda v: coerce_float_in_range(v, 0.0, 1.0),
    "vault_prompt_mode": lambda v: coerce_enum(
        v, ("strict", "balanced", "exploratory", "concise")),
    "vault_chat_temperature": lambda v: coerce_float_in_range(v, 0.0, 2.0),
    "vault_hybrid_enabled": coerce_bool,
    "vault_rrf_dense_weight": lambda v: coerce_float_in_range(v, 0.0, 10.0),
    "vault_rrf_bm25_weight": lambda v: coerce_float_in_range(v, 0.0, 10.0),
    "vault_reranker_enabled": coerce_bool,
    "vault_reranker_model": _coerce_reranker_model,
    "vault_reranker_device": lambda v: coerce_enum(v, ("auto", "cpu", "mps")),
    "vault_mmr_enabled": coerce_bool,
    "vault_mmr_lambda": lambda v: coerce_float_in_range(v, 0.1, 0.9),
    "vault_query_expansion": coerce_bool,
    "vault_num_queries": lambda v: coerce_int_in_range(v, 1, 5),
    "vault_rerank_pool_ceiling": lambda v: coerce_int_in_range(v, 10, 200),
    # Config-only siblings of the ceiling (2026-07-22 audit: they were the
    # only rerank-pool knobs without validators, contradicting the "every
    # numeric knob is validated" contract; the engine reads them defensively
    # but garbage should still never persist).
    "vault_rerank_pool_multiplier": lambda v: coerce_int_in_range(v, 1, 16),
    "vault_rerank_pool_floor": lambda v: coerce_int_in_range(v, 1, 200),
    # llm_pricing_overrides: shape + size gate (2026-07-22 audit M2 — the
    # exact config-bloat class fixed as B7 for vault_chat_system_prompt,
    # reopened here: the key passed the KNOWN_KEYS whitelist un-checked, so
    # one crafted POST could persist a ~100 MB blob that every load_config()
    # then deep-copies ~75 call sites deep). Entries must be
    # {model_id: {input/output/cached_input: number}}; anything else drops
    # the whole key (the previously persisted value survives).
    "llm_pricing_overrides": lambda v: _coerce_pricing_overrides(v),
    # Track 5.6: indexer embed-batch size; 1 = legacy per-chunk inserts.
    "vault_embed_batch_size": lambda v: coerce_int_in_range(v, 1, 256),
    "vault_wikilink_expansion": coerce_bool,
    "vault_paper_labels": coerce_bool,
    # Agent web_search tool (2026-07-22): the knob is a bool; the key stays
    # env-only (TAVILY_API_KEY) and is never accepted or returned here.
    "vault_web_search_enabled": coerce_bool,
    "vault_web_search_max_results": lambda v: coerce_int_in_range(v, 1, 8),
    "vault_wikilink_neighbor_cap": lambda v: coerce_int_in_range(v, 1, 100),
    "vault_wikilink_node_cap": lambda v: coerce_int_in_range(v, 1, 200),
    "vault_wikilink_score_decay": lambda v: coerce_float_in_range(v, 0.0, 1.0),
    "vault_thesaurus_expansion": coerce_bool,
    "vault_thesaurus_max_variants": lambda v: coerce_int_in_range(v, 1, 8),
    "vault_primer_enabled": coerce_bool,
    # Floored at 500 (not 200): the primer header alone is ~231 chars, so a
    # smaller budget would leave no room for glossary lines and silently yield an
    # empty primer. 500 guarantees the header + several entries.
    "vault_primer_max_chars": lambda v: coerce_int_in_range(v, 500, 8000),
    # Curated-glossary file paths (vault-relative, "" disables the slot) and the
    # primer content overrides ("" ⇒ built-in defaults). See _get_thesaurus.
    "vault_thesaurus_abbrev_path": _coerce_vault_rel_file,
    "vault_thesaurus_tags_path": _coerce_vault_rel_file,
    "vault_primer_header": lambda v: coerce_string_max_len(v, 2000),
    "vault_primer_core_terms": lambda v: coerce_string_max_len(v, 4000),
    # B7 (2026-07-13): the vault-chat system-prompt PREFIX (persisted by
    # vault.js) had no write-time cap — only a read-time cap bounded what reached
    # the LLM, so a crafted POST could bloat config.json (deep-copied ~75×).
    "vault_chat_system_prompt": lambda v: coerce_string_max_len(v, SYSTEM_PROMPT_LIMIT),
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
    "deck_section_max_attempts": lambda v: coerce_int_in_range(v, 1, 5),
    "deck_retry_backoff_s": lambda v: coerce_int_in_range(v, 0, 30),
    "deck_section_max_tokens": lambda v: coerce_int_in_range(v, 256, 8192),
    "deck_resume_enabled": coerce_bool,
    # Doc Generator (article/report) generation knobs — prose sibling of deck_*
    # (ranges mirror the _*_MIN/_*_MAX constants in api/routes/docgen.py).
    "docgen_temperature": lambda v: coerce_float_in_range(v, 0.0, 2.0),
    "docgen_max_sections": lambda v: coerce_int_in_range(v, 1, 30),
    "docgen_agent_max_iterations": lambda v: coerce_int_in_range(v, 1, 12),
    "docgen_section_max_attempts": lambda v: coerce_int_in_range(v, 1, 5),
    "docgen_retry_backoff_s": lambda v: coerce_int_in_range(v, 0, 30),
    "docgen_section_max_tokens": lambda v: coerce_int_in_range(v, 256, 16384),
    "docgen_resume_enabled": coerce_bool,
    # Opt-in final-stage .tex integrity review + auto-repair. review_enabled is
    # the persisted default for the off-by-default panel toggle; review_model is
    # the HF/provider-shaped stronger-model override (""⇒chat model); max_tokens
    # must fit the whole re-emitted document.
    "deck_review_enabled": coerce_bool,
    "deck_review_model": _coerce_model_name,
    "deck_review_max_tokens": lambda v: coerce_int_in_range(v, 256, 16384),
    # Compile-fix bounds per the Phase 5 scoping plan (30-600s / 1-3 repairs):
    # the SSE consumer budget is (2*iters+1)*timeout, so the pre-fix 3600x10
    # ceiling would have permitted a ~10-hour worker. The route re-clamps both
    # (and re-validates the engine enum) at call time for hand-edited configs.
    "deck_compile_timeout_s": lambda v: coerce_int_in_range(v, 30, 600),
    "deck_compile_max_iters": lambda v: coerce_int_in_range(v, 1, 3),
    "deck_compile_engine": lambda v: coerce_enum(v, ("pdflatex", "lualatex", "xelatex")),
    # Plain Chat (RAG-free panel). chat_system_prompt is the FULL system prompt
    # (no retrieval grounding to protect), capped at the shared limit; a non-str
    # is dropped (prior kept) while an empty string is a valid "no system
    # prompt" and is persisted as-is, matching coerce_string_max_len semantics.
    "chat_temperature": lambda v: coerce_float_in_range(v, 0.0, 2.0),
    "chat_system_prompt": lambda v: coerce_string_max_len(v, SYSTEM_PROMPT_LIMIT),
    # Per-attachment extracted-text cap. The FLOOR is not cosmetic: the value
    # decides how much of a document the model is shown, and a hand-edited 0
    # would attach an empty file while the UI still said a file was attached.
    "chat_attachment_max_chars": lambda v: coerce_int_in_range(v, 1_000, 400_000),
    # Prompt Hub capture toggle (read-only transparency panel). Bool-coerced;
    # a malformed value is dropped so the prior persisted value survives.
    "prompt_capture_enabled": coerce_bool,
    # Legacy recovery location; containment is checked by the journal on use.
    "refactor_archive_dir": _coerce_abs_dir_or_empty,
    # Biblio Tagging archive dir — shape-only (abs or ""); the not-inside-vault
    # check is re-applied at apply time by tagging/journal.py::archive_dir.
    "tagging_archive_dir": _coerce_abs_dir_or_empty,
    # Biblio Tagging LLM tag-proposal knobs (empty model kept ⇒ chat model).
    # Model id through _coerce_model_name (rejects control chars/newlines), not
    # a bare length cap (2026-07-20 audit M3) — it becomes a provider model id.
    "tagging_propose_model": _coerce_model_name,
    "tagging_propose_max_tokens": lambda v: coerce_int_in_range(v, 64, 2048),
    "tagging_propose_temperature": lambda v: coerce_float_in_range(v, 0.0, 2.0),
    "tagging_max_tags": lambda v: coerce_int_in_range(v, 1, 12),
    "tagging_min_conf": lambda v: coerce_float_in_range(v, 0.0, 1.0),
    "tagging_text_max_chars": lambda v: coerce_int_in_range(v, 500, 40000),
    "tagging_candidate_tags": lambda v: coerce_int_in_range(v, 0, 200),
    "tagging_fewshot_k": lambda v: coerce_int_in_range(v, 0, 10),
    # Restore-journal retention. `policy` selects which records get DELETED, so
    # an unknown value must be dropped rather than stored — the same reasoning
    # as tagging_zotero_transport, where a bad enum would pick a network
    # destination; here it would pick what to destroy.
    # The note template is vault CONTENT the user authors; cap its length and
    # otherwise keep it verbatim (empty = the built-in default).
    "notesync_template": lambda v: coerce_string_max_len(v, 8000),
    "notesync_containment": lambda v: coerce_float_in_range(v, 0.1, 1.0),
    "notesync_zotero_authoritative": coerce_bool,
    "tagging_journal_retention_enabled": coerce_bool,
    "tagging_journal_retention_policy":
        lambda v: coerce_enum(v, ("age_days", "keep_newest")),
    "tagging_journal_retention_days": lambda v: coerce_int_in_range(v, 1, 3650),
    "tagging_journal_retention_keep":
        lambda v: coerce_int_in_range(v, 100, 100_000),
    # Zotero local-API base — validated as an http(s) URL (it carries a path,
    # ``/api``), not a bare length cap (2026-07-20 audit M3): this is a stored
    # dial target (SSRF surface). Empty is kept (built-in local-API default).
    "tagging_zotero_api_base": _coerce_http_url,
    "tagging_zotero_web_api_base": _coerce_https_url,
    "tagging_zotero_transport": lambda v: coerce_enum(v, ("auto", "local", "web")),
    # Accession (keyless bibliographic resolution). Plain keys, deliberately
    # NOT audit_* — POST /api/config strips those. The sources list filters
    # to the known registry names and an empty result is a DROP (unlike
    # fallback_on, "resolve against no sources" is never a meaningful
    # choice). The contact e-mail is shape-checked loosely (it only ever
    # rides a User-Agent header, never an auth flow) and empty is kept.
    "accession_enabled": coerce_bool,
    "accession_contact_email": _coerce_contact_email,
    "accession_sources": _coerce_accession_sources,
    "accession_max_candidates": lambda v: coerce_int_in_range(v, 1, 10),
    "accession_timeout_s": lambda v: coerce_int_in_range(v, 5, 60),
    "accession_batch_max":
        lambda v: coerce_int_in_range(v, 1, ACCESSION_BATCH_MAX_CEILING),
    # Vault Maintenance thresholds + the links-step index folder/note. Plain
    # keys, deliberately NOT audit_* (POST /api/config strips those). The
    # subsystem itself is private-only (STRIP STEP 4) — publicly these keys
    # validate and persist but nothing reads them. The two path keys reuse the
    # thesaurus shape check ("" ⇒ the private built-in default; no
    # traversal/abs/NUL); thresholds are ceilings, 0 legitimate.
    "maintenance_links_index_subdir": _coerce_vault_rel_file,
    "maintenance_links_index_note": _coerce_vault_rel_file,
    "maintenance_max_drift": lambda v: coerce_int_in_range(v, 0, 1_000_000),
    "maintenance_max_no_pdf": lambda v: coerce_int_in_range(v, 0, 1_000_000),
    "maintenance_max_dupes": lambda v: coerce_int_in_range(v, 0, 1_000_000),
    "maintenance_max_hygiene": lambda v: coerce_int_in_range(v, 0, 1_000_000),
    "maintenance_max_hygiene_unmapped": lambda v: coerce_int_in_range(v, 0, 1_000_000),
    # fallback_on handled separately (list-valued).
}


def _validate_llm_config_keys(data: dict) -> list[str]:
    """Clamp/drop the LLM-parameter keys in-place before save_config.

    Returns the list of keys that were DROPPED (malformed / out of range, so the
    prior persisted value survives). 2026-07-26 UX audit, WCAG 3.3.1 Error
    Identification: a drop used to be completely silent — 200 OK, no field in
    the body — while the Settings modal is populated once at boot and never
    reloaded, so the rejected value sat in the input for the whole session while
    the server held the old one. The caller surfaces this ADDITIVELY (the
    response keeps ``ok`` and its 200 status; smoke_test.py asserts on those).
    """
    dropped: list[str] = []
    for key, validator in _CONFIG_VALIDATORS.items():
        if key in data:
            coerced = validator(data[key])
            if coerced is None:
                data.pop(key)
                dropped.append(key)
            else:
                data[key] = coerced
    if "fallback_on" in data:
        fo = _coerce_fallback_on(data["fallback_on"])
        if fo is None:
            data.pop("fallback_on")
            dropped.append("fallback_on")
        else:
            data["fallback_on"] = fo
    return dropped

@config_bp.route("/api/config")
def api_get_config():
    """Return the persisted config verbatim.

    ``load_config`` never includes API keys (they live only in env vars), so
    this is safe to hand to the local UI as-is. Local-origin gated.
    """
    return jsonify(load_config())

_CONFIG_APPLY_LOCK = threading.Lock()

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
    5. ``audit_*`` key stripping (those go through ``/api/audit/config``).
    6. ``_validate_llm_config_keys`` — clamp/drop the numeric/enum/bool knobs.
    7. Persist, then apply accepted OCR/vision settings to the singletons.

    Responds ``{"ok": true, "dropped": [key, …]}``. ``dropped`` names the keys
    step 6 refused (prior value kept) so the Settings window can flag the exact
    field instead of failing silently; it is additive and always present.

    Local-origin gated; 400 on non-JSON or an invalid vault path.
    """
    data = request.get_json(silent=True)
    # isinstance, not truthiness (2026-07-22 audit M7): a JSON *array* body
    # passed `if not data` and crashed on `.startswith`/`.pop` deeper in (500
    # instead of a clean 400) — the class paper.py fixed with the same check.
    # And `{}` is a legitimate no-op patch, not "Invalid JSON".
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON: expected an object"}), 400
    # Normalise `provider` ONCE, up front: the llm-routing below used to
    # .strip().lower() its own copy while the enum validator later required
    # an exact match — so {"provider": "OPENAI", "llm": "x"} routed `llm`
    # into openai_model (consuming the local selection) and then DROPPED the
    # provider change. One normalisation ⇒ routing and validation agree.
    if isinstance(data.get("provider"), str):
        data["provider"] = data["provider"].strip().lower()
    _vault_switch_to = None
    if "obsidian_vault_path" in data:
        try:
            data["obsidian_vault_path"] = _validate_vault_path(
                str(data.get("obsidian_vault_path") or "")
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        # A1 (2026-07-13): a vault SWITCH must drop the loaded index and every
        # in-memory retrieval cache (BM25/wikilink/thesaurus, prewarm state) —
        # otherwise the next chat answers from the previous vault's in-memory
        # index and a reindex runs incrementally against it (cross-vault mix).
        # The switch itself is DEFERRED until after save_config succeeds
        # (2026-07-22 audit M7b): mutating the live manager first meant a
        # failed save (disk full) left memory on the new vault while
        # config.json kept the old one — divergent until restart.
        _vault_switch_to = data["obsidian_vault_path"]
    if "vault_exclude_dirs" in data:
        data["vault_exclude_dirs"] = _normalise_vault_exclude_dirs(
            data.get("vault_exclude_dirs"),
            data.get("obsidian_vault_path") or obsidian_manager.get_vault_path() or load_config().get("obsidian_vault_path", ""),
        )
    if "vault_image_exts" in data:
        data["vault_image_exts"] = _normalise_vault_image_exts(
            data.get("vault_image_exts")
        )
    from core.config import ONLINE_MODEL_KEYS
    if any(key in data for key in ("provider", "llm", "embed", *ONLINE_MODEL_KEYS.values())):
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
        # str() first: a non-string truthy `provider` (a number, a dict — this
        # is a JSON body) made `.strip()` raise AttributeError, i.e. a 500 on a
        # malformed request where every other bad value in this handler is
        # coerced or dropped.
        active = str(data.get("provider") or load_config().get("provider")
                     or "ollama").strip().lower()
        if is_online_provider(active):
            key = ONLINE_MODEL_KEYS[active]
            data.setdefault(key, data["llm"])
            data.pop("llm")
    # Library Audit settings have a dedicated, validated endpoint
    # (POST /api/audit/config — path-traversal and absolute-path checks).
    # Strip them here so the generic save path cannot be used to bypass
    # those validators; the audit UI (audit.js) never posts here.
    audit_keys = [key for key in data if key.startswith("audit_")]
    for key in audit_keys:
        data.pop(key)

    # Item 4.10: Whitelist keys against KNOWN_KEYS to prevent clients from persisting
    # arbitrary unvalidated keys into config.json.
    # Defect: Bypassing key validation allows malicious/junk inputs to pollute config.json.
    # Safety: Completely backward-compatible. Only filters out non-default/unsupported keys.
    # Invariant: Only keys defined in core.config.KNOWN_KEYS are persisted.
    from core.config import KNOWN_KEYS
    extra_keys = [k for k in data if k not in KNOWN_KEYS]
    for k in extra_keys:
        data.pop(k)
    # Track 5.7: config_version is server-owned schema state (in KNOWN_KEYS,
    # so the whitelist above keeps it) — a client must never set it, or a
    # forged lower value would re-run migrations / a higher one would block
    # them. save_config re-stamps it regardless; this strip is defence in depth.
    data.pop("config_version", None)

    # Clamp/validate the LLM-parameter keys the Settings window writes; an
    # out-of-range or malformed value is dropped so the prior value survives.
    # The dropped list rides the response (see below) so the client can tell the
    # user WHICH field did not persist instead of leaving a rejected value
    # sitting in the input all session.
    dropped = _validate_llm_config_keys(data)
    with _CONFIG_APPLY_LOCK:
        save_config(data)
        # Runtime changes follow successful validation and durable persistence.
        if any(key in data for key in ("ocr_provider", "ocr_model", "vision_provider", "vision_model")):
            from services.vision import glm_ocr_manager, vision_manager
            if data.get("ocr_provider"):
                glm_ocr_manager.set_provider(str(data["ocr_provider"]))
            if "ocr_model" in data:
                glm_ocr_manager.set_model(str(data["ocr_model"]))
            if data.get("vision_provider"):
                vision_manager.set_provider(str(data["vision_provider"]))
            if "vision_model" in data:
                vision_manager.set_model(str(data["vision_model"]))
    # Vault switch AFTER the successful persist (see the deferral comment
    # above): compare normalised forms and only clear on a real change so a
    # no-op re-save stays cheap. cleanup() drops the in-memory index + caches
    # and calls reset_prewarm(); it does NOT delete anything on disk (only
    # /api/reset does), so a switch BACK to the prior vault still loads it.
    if _vault_switch_to is not None:
        _prev_vault = obsidian_manager.get_vault_path()
        obsidian_manager.restore_vault_path(_vault_switch_to)
        if obsidian_manager.get_vault_path() != _prev_vault:
            obsidian_manager.cleanup()
            from audit.manager import audit_manager
            audit_manager.reset_to_idle()
    # ADDITIVE field (2026-07-26 UX audit): status stays 200 and ``ok`` stays
    # True — a drop is a partial success, not a request failure, and existing
    # callers/tests that only read ``ok`` are unaffected. ``dropped`` is always
    # present (empty on the happy path) so a client can branch on length rather
    # than on presence.
    return jsonify({"ok": True, "dropped": dropped})

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
# Known office/document binary extensions never belong in the image/vision
# branch. They are added to the reserve so a hand-edited (or malicious-page)
# config can never enumerate a document into `vault_image_exts` and route its
# bytes to the vision model — defence-in-depth over the existing md/pdf reserve,
# which stopped short of other document types.
_VAULT_DOC_EXTS = frozenset({
    ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".rtf", ".odt",
})
_VAULT_RESERVED_EXTS = VAULT_MD_EXTS | VAULT_BINARY_EXTS | _VAULT_DOC_EXTS

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


_SQLITE_FILE_EXTS = (".sqlite", ".sqlite3", ".db")


def _picker_selection(chosen, data, *, is_file=False):
    """Project native selections into the existing config path conventions."""
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object"}), 400
    kind = data.get("kind", "sqlite" if is_file else "absolute")
    extensions = {"sqlite": _SQLITE_FILE_EXTS, "bib": (".bib",), "markdown": (".md",)}
    if is_file and (not isinstance(kind, str) or kind not in extensions):
        return jsonify({"error": "Unsupported file selection kind"}), 400
    if is_file and not str(chosen).lower().endswith(extensions[kind]):
        return jsonify({"error": "Selected file must use " + ", ".join(extensions[kind])}), 400
    relative = None
    if data.get("relative_to_vault"):
        cfg = load_config()
        vault = cfg.get("obsidian_vault_path")
        if not vault or not Path(vault).is_dir():
            return jsonify({"error": "Choose an existing Obsidian vault on Ask before selecting this path."}), 400
        base = Path(vault).resolve()
        if data.get("under_attachments"):
            subdir = cfg.get("audit_attachments_subdir", "Z_attachments")
            base = (base / subdir).resolve()
            if not base.is_relative_to(Path(vault).resolve()):
                return jsonify({"error": "Configured attachments folder is outside the vault."}), 400
        try:
            relative = Path(chosen).resolve().relative_to(base).as_posix()
        except ValueError:
            return jsonify({"error": "Select a path inside " + str(base)}), 400
        if relative == ".":
            return jsonify({"error": "Select a path below " + str(base)}), 400
    return jsonify({"ok": True, "path": str(Path(chosen).resolve()), "relative_path": relative})


@config_bp.route("/api/config/native-pick-folder", methods=["POST"])
def api_config_native_pick_folder():
    """Open a native folder picker for archive or Zotero storage directories."""
    try:
        import webview
    except Exception:  # noqa: BLE001
        return jsonify({"error": "Native picker requires the CHERQAiN desktop window. Enter the path manually when using a browser."}), 503
    if not getattr(webview, "windows", None):
        return jsonify({"error": "Native picker requires the CHERQAiN desktop window. Enter the path manually when using a browser."}), 503
    try:
        result = webview.windows[0].create_file_dialog(
            webview.FOLDER_DIALOG,
            allow_multiple=False,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    if not result:
        return jsonify({"cancelled": True, "path": None})
    chosen = result[0] if isinstance(result, (list, tuple)) else result
    if not chosen or not os.path.isdir(chosen):
        return jsonify({"error": "Selected path is not a directory"}), 400
    return _picker_selection(chosen, request.get_json(silent=True) or {})


@config_bp.route("/api/config/native-pick-file", methods=["POST"])
def api_config_native_pick_file():
    """Open a native file picker for SQLite database files."""
    try:
        import webview
    except Exception:  # noqa: BLE001
        return jsonify({"error": "Native picker requires the CHERQAiN desktop window. Enter the path manually when using a browser."}), 503
    if not getattr(webview, "windows", None):
        return jsonify({"error": "Native picker requires the CHERQAiN desktop window. Enter the path manually when using a browser."}), 503
    try:
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Supported files (*.sqlite;*.sqlite3;*.db;*.bib;*.md)", "All files (*.*)"),
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    if not result:
        return jsonify({"cancelled": True, "path": None})
    chosen = result[0] if isinstance(result, (list, tuple)) else result
    if not chosen or not os.path.isfile(chosen):
        return jsonify({"error": "Selected path is not a file"}), 400
    return _picker_selection(chosen, request.get_json(silent=True) or {}, is_file=True)


@config_bp.route("/api/report_types")
@config_bp.route("/api/report-types")
def api_get_report_types():
    """Return the built-in + saved/overridden report types.

    Two URL shapes share one handler for backwards compatibility: the legacy
    underscore form ``/api/report_types`` returns the bare list, while the
    hyphen form ``/api/report-types`` wraps it as ``{"report_types": [...]}``.
    ``load_report_types`` merges built-ins with custom/overridden saved types.
    """
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
            # PERF/correctness (2026-09-02): host-bind the pull.
            #   Before:    `ollama.pull` on the MODULE-level client, which
            #              resolves OLLAMA_HOST independently of the app's
            #              resolved host — the same split-brain the status probe
            #              was fixed for (core/providers/ollama.py::check_running):
            #              a pull could land on one server while generation and
            #              embedding used another, so the model appeared to
            #              download and then "was not installed".
            #   Change:    the app's own resolved host, through the shared
            #              (host, timeout) client cache. `timeout=None` is
            #              deliberate and NOT a bound worth adding: a pull
            #              legitimately streams for many minutes, and the
            #              SDK's own read timeout applies per chunk.
            #   Safe because:
            #              the streamed chunk shape is the SDK's, unchanged;
            #              only which server is asked changes, to the one every
            #              other call in the app already uses.
            #   Preserves: the "probe and generation share one host" rule the
            #              status adapters state.
            #   Lock/thread contract: unchanged; no lock.
            #   Pinned by: test_status_routes.py::TestPullUsesTheResolvedHost
            from core.providers.base import resolve_ollama_host
            from core.providers.ollama import _ollama_client
            client = _ollama_client(resolve_ollama_host(), None)
            for chunk in client.pull(model, stream=True):
                # ollama SDK yields Pydantic model instances; json.dumps() cannot
                # serialize them directly — use model_dump() (Pydantic v2) with a
                # fallback to dict() for older SDK versions.
                chunk_data = chunk.model_dump() if hasattr(chunk, "model_dump") else dict(chunk)
                yield f"data: {json.dumps(chunk_data)}\n\n"
        except Exception as exc:
            # Through sanitise_error_msg like every other SSE/error path
            # (2026-07-20 audit M4): the raw str(exc) leaked filesystem paths /
            # topology into the UI, breaking the "redact before it reaches the
            # UI" discipline the module otherwise follows.
            yield f"data: {json.dumps({'error': sanitise_error_msg(exc)})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream")

@config_bp.route("/api/reset", methods=["POST"])
def api_reset():
    """Confirmation-gated teardown of indexes, the uploads DB, and audit state.

    Requires ``{"confirm": "reset"}`` (a literal token, not just truthiness) so
    a stray POST cannot wipe data. Sequence: stop the indexer and *wait* for its
    final persist (refuse with 503 on timeout rather than race a stray persist
    that would re-create the storage dir with partial state), acquire a teardown lock,
    reset the audit subsystem to idle, clear the ``uploads`` table,
    ``rmtree`` the Obsidian storage dir (logged via ``log_storage_deletion``),
    and ``rmtree`` the Obsidian cache dir (descriptions + OCR text — the bulk of
    the on-disk footprint; content-hash keyed, so it self-heals on reindex).
    ``wipe_feedback`` / ``wipe_config`` optionally delete those files too — both
    resolved through the (test-patchable) ``app.FEEDBACK_FILE`` / ``CONFIG_FILE``
    attributes. Returns the list of deleted artefacts.
    """
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
    epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not epoch:
        return jsonify(error="A vault operation is active. Retry after it finishes.",
                       reason="vault_context_busy"), 409
    try:
        obsidian_manager.cleanup()
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

        # Also reclaim the description / OCR-text cache — the LARGEST on-disk
        # footprint (cached vision descriptions + scanned-PDF OCR text, potentially
        # many GB). It used to survive "reset", so a teardown freed little disk and
        # left stale cached descriptions from the previous vault. Safe to delete: the
        # cache is content-hash keyed, so it self-heals on the next index.
        if os.path.isdir(OBSIDIAN_CACHE_DIR):
            try:
                shutil.rmtree(OBSIDIAN_CACHE_DIR)
            except OSError as exc:
                return jsonify(ok=False, deleted=deleted,
                               error="Reset stopped while removing the extraction cache: "
                               + sanitise_error_msg(exc)), 500
            deleted.append("obsidian_cache")

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
    finally:
        obsidian_manager.release_lock(epoch)


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


# Tail bounds: a generous line cap for the viewer, plus a hard byte ceiling so a
# single pathological log line (or a huge file) can never blow up the response or
# the renderer. The reader seeks to the end and only ever pulls the last
# ``_LOG_TAIL_MAX_BYTES`` off disk, so cost is independent of total file size.
_LOG_TAIL_DEFAULT_LINES = 500
_LOG_TAIL_MAX_LINES = 5000
_LOG_TAIL_MAX_BYTES = 1_000_000  # ~1 MB window read from the end of the file


@config_bp.route("/api/log/tail", methods=["GET"])
def api_log_tail():
    """Return the tail of ``chatekld.log`` for the in-app log viewer.

    Local-origin gated like every other route. Read-only: it never writes,
    rotates, or truncates the log. Reads at most ``_LOG_TAIL_MAX_BYTES`` from the
    end of the file (cost independent of file size), keeps the last ``lines``
    (query param, clamped to ``_LOG_TAIL_MAX_LINES``), and runs every line
    through ``redact()`` so an accidentally-logged API key can never surface in
    the UI — the same "redact before it reaches the UI" discipline the JS-error
    sink and ``sanitise_error_msg`` already follow.

    Response: ``{"text", "lines", "path", "size", "truncated", "exists"}``.
    """

    from core.llm.redact import redact

    lines = coerce_int_in_range(request.args.get("lines"), 1, _LOG_TAIL_MAX_LINES)
    if lines is None:  # missing/garbage ``lines`` param → fall back to the default
        lines = _LOG_TAIL_DEFAULT_LINES

    try:
        size = os.path.getsize(LOG_FILE)
    except OSError:
        # No log yet (fresh install / first run before the first write).
        return jsonify({
            "text": "", "lines": 0, "path": LOG_FILE,
            "size": 0, "truncated": False, "exists": False,
        })

    truncated = size > _LOG_TAIL_MAX_BYTES
    try:
        with open(LOG_FILE, "rb") as fh:
            if truncated:
                fh.seek(size - _LOG_TAIL_MAX_BYTES)
            raw = fh.read()
    except OSError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 500

    # Decode leniently — a partial multibyte char at the seek boundary must not
    # raise. Drop the first (likely partial) line when we seeked into the middle —
    # but only when there's more than one line. A single log line longer than the
    # byte window would otherwise be dropped to nothing, making the viewer
    # misreport a non-empty file as "(log is empty)"; keep the partial line instead.
    text = raw.decode("utf-8", errors="replace")
    all_lines = text.splitlines()
    if truncated and len(all_lines) > 1:
        all_lines = all_lines[1:]
    tail = all_lines[-lines:]
    redacted = [redact(ln) for ln in tail]
    return jsonify({
        "text": "\n".join(redacted),
        "lines": len(redacted),
        "path": LOG_FILE,
        "size": size,
        "truncated": truncated,
        "exists": True,
    })
