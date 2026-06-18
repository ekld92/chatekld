import copy
import json
import logging
import os
import threading
import tempfile
from typing import Optional
from core.constants import (
    CONFIG_FILE,
    REPORT_TYPES_FILE,
    DEFAULT_LLM,
    DEFAULT_EMBED,
    DEFAULT_OCR_MODEL,
    DEFAULT_VISION_MODEL,
    DEFAULT_REPORT_TYPES,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_GOOGLE_MODEL,
    DEFAULT_ONLINE_TIMEOUT_S,
    DEFAULT_ONLINE_MAX_RETRIES,
    DEFAULT_ONLINE_MAX_TOKENS,
    VAULT_IMAGE_EXTS,
)

logger = logging.getLogger(__name__)

_config_write_lock = threading.Lock()

# load_config() result cache.  A single vault-chat request calls
# load_config() 4-5 times (route layer, engine, vault manager), and 26 call
# sites across the app re-read and re-parse config.json on every call.  The
# cache is keyed by (path, st_size, st_mtime_ns) — the same self-correcting
# pattern as UsageTracker._disk_records — so any rewrite of the file (our
# own atomic save_config or an external edit) changes the key and forces a
# re-read; no call site can ever observe a stale config.  The path is part
# of the key so tests that repoint CHATEKLD_BASE_DIR (and therefore
# CONFIG_FILE) never share cache entries across locations.
_config_cache_lock = threading.Lock()
_config_cache: Optional[dict] = None
_config_cache_key: Optional[tuple] = None


def _config_file_key() -> tuple:
    try:
        stat = os.stat(CONFIG_FILE)
        return (CONFIG_FILE, stat.st_size, stat.st_mtime_ns)
    except OSError:
        return (CONFIG_FILE, None, None)


def _invalidate_config_cache() -> None:
    global _config_cache, _config_cache_key
    with _config_cache_lock:
        _config_cache = None
        _config_cache_key = None


def load_config() -> dict:
    """Load user configuration from disk (stat-cached between rewrites)."""
    global _config_cache, _config_cache_key
    # Cache hit: one stat() syscall instead of open + read + json parse.
    # Returns a deep copy because callers mutate the result (save_config
    # does current.update(...), routes pop/rewrite keys) — handing out a
    # shared reference would poison the cache for every later caller.
    key = _config_file_key()
    with _config_cache_lock:
        if _config_cache is not None and _config_cache_key == key:
            return copy.deepcopy(_config_cache)
    defaults = {
        "library_root": "",
        "llm": DEFAULT_LLM,
        "embed": DEFAULT_EMBED,
        "ocr_model": DEFAULT_OCR_MODEL,
        "vision_model": DEFAULT_VISION_MODEL,
        "provider": "ollama",
        "ocr_provider": "ollama",
        "vision_provider": "ollama",
        "vault_exclude_dirs": [],
        # Sourced from VAULT_IMAGE_EXTS so the persisted default stays in
        # sync with the indexer's canonical image-extension set.  Sorted
        # for a stable order in config.json since the source is a frozenset.
        "vault_image_exts": sorted(VAULT_IMAGE_EXTS),
        "context_window": 32768,
        # Vault chat live knobs.  These flow through /api/obsidian/chat as
        # per-request overrides; the request body wins but these are the
        # persisted fallbacks that the UI restores on launch.
        # Default retrieval breadth.  8 gives the LLM a little more evidence
        # on large-context models; the _effective_top_k autoscaler still caps
        # this for small-context models so they are not overwhelmed.
        "vault_top_k": 8,
        "vault_similarity_cutoff": 0.25,
        "vault_prompt_mode": "strict",
        "vault_chat_temperature": 0.3,
        # Optional behavioural prefix the user can layer over the selected
        # Answer Mode template.  Empty string means "use the mode template
        # as-is".  Capped to SYSTEM_PROMPT_LIMIT at the route layer.
        "vault_chat_system_prompt": "",
        # Hybrid retrieval (BM25 + dense, RRF-fused) and cross-encoder
        # reranking.  Both default on; both degrade gracefully to the legacy
        # dense-only path when the required package or model is unavailable.
        # ms-marco-MiniLM-L-6-v2 is ~67 MB on first download and runs on
        # CPU at ~80-200 ms / 30 candidates on Apple Silicon.
        "vault_hybrid_enabled": True,
        "vault_reranker_enabled": True,
        "vault_reranker_model": "cross-encoder/ms-marco-MiniLM-L-6-v2",
        # Cross-encoder execution device. "auto" keeps the pre-knob
        # behaviour: the reranker is constructed without a device argument
        # and the underlying stack picks MPS on Apple Silicon when
        # available, else CPU. "cpu" forces CPU — the escape hatch that
        # keeps unified memory free for the LLM on a 16 GB machine.
        # "mps" requires Metal. Unknown values behave like "auto"; a
        # non-CPU failure (construction or warm-up inference) retries on
        # CPU for the session instead of disabling reranking.
        "vault_reranker_device": "auto",
        # Launch-time prewarm of the vault index / BM25 / reranker.
        # Disabling defers the multi-GB load to the first chat — it does
        # not remove the cost, it lets the user choose when the footprint
        # appears.
        "vault_prewarm_enabled": True,
        # Vector store backend for NEW index builds. "simple" keeps LlamaIndex's
        # legacy JSON SimpleVectorStore (the default, so fresh builds are
        # unchanged); "lancedb" writes a binary Apache-Arrow store (low resident
        # RAM, fast GIL-free cold start, transactional inserts). EXISTING indexes
        # ignore this knob — their backend is pinned in obsidian_meta.json
        # ("vector_backend"); a missing key means the legacy "simple" store.
        # Recommended path for an existing large vault: migrate it in place with
        # NO re-embedding via scripts/migrate_vector_store.py (which sets the
        # meta key). Setting this to "lancedb" makes the *next reindex* build the
        # binary store directly; it resolves back to "simple" if lancedb is not
        # importable.
        "vault_vector_backend": "simple",
        # --- Query-time retrieval-quality knobs (no reindex required) ---
        # Maximal-marginal-relevance diversity on the dense leg.  Gated by
        # vault_mmr_enabled (off by default); vault_mmr_lambda is the MMR
        # threshold in (0,1]: higher favours relevance, lower favours
        # diversity — useful when top_k is dominated by near-identical chunks
        # from a single note.
        "vault_mmr_enabled": False,
        "vault_mmr_lambda": 0.5,
        # Reranker candidate-pool sizing: the fusion/dense retriever fetches
        # min(max(top_k*multiplier, floor), ceiling) candidates for the
        # cross-encoder to narrow back down to top_k.  Config-only.
        "vault_rerank_pool_multiplier": 4,
        "vault_rerank_pool_floor": 20,
        "vault_rerank_pool_ceiling": 50,
        # Multi-query expansion: when enabled the fusion retriever rewrites
        # the query into vault_num_queries variants and RRF-fuses the
        # results.  Adds an LLM call per turn (default off).  Currently
        # effective for LOCAL providers only — the online chat path has no
        # llama-index LLM object to drive the rewrites, so it stays single.
        "vault_query_expansion": False,
        "vault_num_queries": 3,
        # Agent mode (opt-in). When True, /api/obsidian/chat routes
        # through the ReAct agent loop with vault.search / vault.read_note /
        # vault.list_materials tools instead of the single-shot RAG path.
        # Default off; the loop budget caps a single turn at 6 iterations.
        "vault_agent_enabled": False,
        "vault_agent_max_iterations": 6,
        # Online LLM provider settings. Keys are sourced from environment
        # variables (OPENAI_API_KEY / ANTHROPIC_API_KEY / GOOGLE_API_KEY)
        # and are never persisted to config.json. Per-provider model
        # selections are saved here so toggling provider preserves choice.
        "openai_model": DEFAULT_OPENAI_MODEL,
        "anthropic_model": DEFAULT_ANTHROPIC_MODEL,
        "google_model": DEFAULT_GOOGLE_MODEL,
        "online_timeout_s": DEFAULT_ONLINE_TIMEOUT_S,
        "online_max_retries": DEFAULT_ONLINE_MAX_RETRIES,
        "online_max_tokens": DEFAULT_ONLINE_MAX_TOKENS,
        # Wall-clock cap for a single agent turn (vault chat) / per-section
        # turn (deck generation). The SSE stall guard and the frontend fetch
        # abort derive from this so raising it isn't defeated by an outer
        # timeout (the nested timeout chain — see core/llm/CLAUDE.md).
        "agent_wall_clock_s": 300,
        # Per-call HTTP timeout for LOCAL providers (Ollama / LM Studio).
        # 0 means "leave each path's existing default" (raw ollama.chat stream
        # & tool calls are unbounded — bounded only by agent_wall_clock_s; the
        # LlamaIndex Ollama query path keeps its own 30 s default; LM Studio
        # keeps the OpenAI SDK default). A positive value overrides all of them
        # to that many seconds. For streaming it is httpx's per-read gap, i.e. a
        # max-time-between-tokens stall guard; for the non-streaming tool path
        # it bounds the whole call.
        # SCOPE: chat / generation only. Embedding calls are deliberately NOT
        # bounded by this — OllamaEmbedding exposes no timeout hook, and forcing
        # one on indexing (which embeds large batches and already tolerates slow
        # runs via checkpoints + consecutive-failure limits) would cause spurious
        # failures, not graceful recovery. A hung embedding surfaces through the
        # indexer's own error handling instead.
        "local_request_timeout_s": 0,
        # Embeddings are local-only. When ``provider`` is online the
        # indexer and vault chat fall back to this provider for embedding.
        "embed_provider": "ollama",
        # Fallback policy. Default is fail-fast: if the user explicitly
        # chose an online provider they should see provider failures
        # rather than silently rolling back to local at the user's cost.
        "fallback_provider": "",
        "fallback_on": ["timeout", "network", "rate_limit", "server_error"],
        # Optional per-model price overrides — useful when a provider
        # raises a price between releases of this app. Shape:
        # {"gpt-4o": {"input": 2.50, "output": 10.00}}
        "llm_pricing_overrides": {},
        # --- Single Paper generation knobs ---
        # Persisted defaults for the Single Paper summariser.  /api/summarise
        # accepts these as per-request body overrides; when the body omits a
        # field (e.g. the inline panel was removed in favour of the Settings
        # window), api/routes/paper.py falls back to these.  Ranges mirror the
        # parse_* clamps in core/utils.py.
        "paper_temperature": 0.3,
        "paper_num_ctx": 32768,
        "paper_max_tokens": 4096,
        "paper_top_p": 0.9,
        "paper_repeat_penalty": 1.1,
        # --- Deck Generator knobs ---
        # Persisted defaults for the Deck Generator.  /api/deck/generate
        # accepts these as per-request overrides; api/routes/deck.py falls
        # back to these when the body omits them.  Ranges mirror the
        # _*_MIN/_*_MAX constants in api/routes/deck.py.
        "deck_temperature": 0.3,
        "deck_max_sections": 8,
        "deck_agent_max_iterations": 6,
        # Library Audit (kb_harmonizer) settings. The audit subsystem
        # is fully manual — these keys are read only when the user
        # explicitly clicks "Run Scan" on the Library Audit tab.
        "audit_attachments_subdir": "Z_attachments",
        "audit_biblio_articles_subdir": "biblio_articles",
        "audit_zotero_notes_subdir": "Z_Zotero_Notes",
        "audit_master_bib_path": "presentations_slides_writings_teaching/_master.bib",
        "audit_zotero_sqlite": "~/Zotero/zotero.sqlite",
        "audit_zotero_storage": "~/Zotero/storage",
        "audit_annotations_read_threshold": 5,
        "audit_biblio_skip_prefix": "z_item",
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
                defaults.update(data)
        except Exception as exc:
            logger.warning("Could not parse config file, using defaults: %s", exc)
    # Cache the merged result under the key we statted BEFORE reading.  If
    # the file is rewritten between the stat and the read we may cache fresh
    # content under a stale key — harmless: the next call re-stats, sees a
    # different key, and re-reads.  The cache can lag by at most one call,
    # never serve content older than the key it was stored under.
    with _config_cache_lock:
        _config_cache = copy.deepcopy(defaults)
        _config_cache_key = key
    return defaults

def save_config(config: dict):
    """Persist updated configuration keys to CONFIG_FILE atomically."""
    with _config_write_lock:
        current = load_config()
        current.update(config)
        
        # Atomic write: mkstemp returns an open fd; close it explicitly if
        # os.fdopen fails before taking ownership of the descriptor.
        fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(CONFIG_FILE), text=True)
        try:
            try:
                f = os.fdopen(fd, 'w')
            except Exception:
                os.close(fd)
                raise
            with f:
                json.dump(current, f, indent=4)
            os.replace(temp_path, CONFIG_FILE)
            # Drop the read cache eagerly.  The (size, mtime_ns) key would
            # catch the rewrite on its own, but two saves landing within the
            # filesystem's mtime resolution with identical byte counts could
            # leave the key unchanged — explicit invalidation closes that
            # window for free.
            _invalidate_config_cache()
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

_ONLINE_PROVIDER_NAMES = frozenset({"openai", "anthropic", "google"})
_LOCAL_PROVIDER_NAMES = frozenset({"ollama", "lm_studio"})

_ONLINE_MODEL_KEYS: dict[str, str] = {
    "openai": "openai_model",
    "anthropic": "anthropic_model",
    "google": "google_model",
}


def is_online_provider(name: str) -> bool:
    return (name or "").strip().lower() in _ONLINE_PROVIDER_NAMES


def is_local_provider(name: str) -> bool:
    return (name or "").strip().lower() in _LOCAL_PROVIDER_NAMES


def resolve_chat_model(cfg: dict, provider_name: str) -> str:
    """Return the persisted chat model for the given provider.

    Local providers (Ollama, LM Studio) read from the single ``llm``
    field for backwards compatibility. Online providers each have
    their own model field so switching back and forth does not lose
    the user's selection.
    """
    key = _ONLINE_MODEL_KEYS.get((provider_name or "").strip().lower())
    if key:
        value = cfg.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if provider_name == "openai":
            return DEFAULT_OPENAI_MODEL
        if provider_name == "anthropic":
            return DEFAULT_ANTHROPIC_MODEL
        if provider_name == "google":
            return DEFAULT_GOOGLE_MODEL
    return str(cfg.get("llm", DEFAULT_LLM) or DEFAULT_LLM)


def resolve_embed_provider(cfg: dict, provider_name: str) -> str:
    """Return a *local* provider name to use for embeddings.

    When the chat ``provider_name`` is itself local, it is returned
    unchanged. When the chat provider is online we look up the
    persisted ``embed_provider`` and fall back to ``ollama`` if that
    is missing or also online (which would be a misconfiguration).
    """
    if is_local_provider(provider_name):
        return provider_name
    embed = str(cfg.get("embed_provider") or "").strip().lower()
    if embed in _LOCAL_PROVIDER_NAMES:
        return embed
    return "ollama"


def load_report_types() -> list:
    """Load built-in and saved report types, preserving user prompt overrides."""
    builtins = copy.deepcopy(DEFAULT_REPORT_TYPES)
    saved_map: dict[str, dict] = {}
    if os.path.exists(REPORT_TYPES_FILE):
        try:
            with open(REPORT_TYPES_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                saved_map = {
                    str(item.get("id")): item
                    for item in data
                    if isinstance(item, dict) and item.get("id")
                }
        except Exception as exc:
            logger.warning("Could not parse report types, using empty list: %s", exc)

    builtin_ids = {item["id"] for item in builtins}
    for item in builtins:
        saved = saved_map.get(item["id"])
        if saved and isinstance(saved.get("system_prompt"), str):
            item["system_prompt"] = saved["system_prompt"]

    custom = [
        item for key, item in saved_map.items()
        if key not in builtin_ids and isinstance(item, dict)
    ]
    return builtins + custom
