import copy
import json
import logging
import os
import threading
import tempfile
from types import MappingProxyType
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
    DEFAULT_VISION_TIMEOUT_S,
    DEFAULT_VISION_MAX_TOKENS,
    DEFAULT_OCR_MAX_TOKENS,
    DEFAULT_VISION_FAILURE_COOLDOWN_S,
    DEFAULT_REFACTOR_THUMB_MAX_SIDE,
    VAULT_IMAGE_EXTS,
)

logger = logging.getLogger(__name__)

# RLock, not Lock (Track 5.7): save_config() calls load_config() while holding
# this, and load_config's migration path re-acquires it to persist the migrated
# file — with a plain Lock the first save over an old-version config would
# self-deadlock.
_config_write_lock = threading.RLock()

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


# Schema version of config.json (Track 5.7, 2026-07-04). Bump this AND add a
# `_MIGRATIONS[old_version]` step whenever a persisted key is renamed, retyped,
# or retired — load_config() runs the pending steps once and rewrites the file.
# Version 0 (implicit) = any config written before the key existed.
CONFIG_VERSION = 1

_DEFAULTS = {
    # Stamped into every saved config; server-owned (POST /api/config strips
    # it) and self-healing (load_config migrates + re-stamps an older file).
    "config_version": CONFIG_VERSION,
    "library_root": "",
    "llm": DEFAULT_LLM,
    "embed": DEFAULT_EMBED,
    "ocr_model": DEFAULT_OCR_MODEL,
    "vision_model": DEFAULT_VISION_MODEL,
    "provider": "ollama",
    "ocr_provider": "ollama",
    "vision_provider": "ollama",
    # Local-backend base URLs. Empty ⇒ resolve env var (OLLAMA_HOST) then the
    # hardcoded localhost default. Set to e.g. "http://192.168.1.50:11434" to
    # point at a remote/custom-port backend. Resolved by
    # core.providers.base.resolve_ollama_host / resolve_lm_studio_host so the
    # health probe AND generation/embedding share one host (no split-brain).
    "ollama_host": "",
    "lm_studio_host": "",
    # The Obsidian vault root ("" = not configured). Historically persisted by
    # rag/vault.py + the config route but NEVER declared here — which made
    # KNOWN_KEYS incomplete and bit twice (Track 5.7): the 4.10 whitelist at
    # POST /api/config silently STRIPPED the validated vault path before
    # save_config (set-in-memory-but-gone-on-restart), and the 0→1
    # prune-unknown-keys migration would have wiped it from existing configs.
    # KNOWN_KEYS must list every key any code path persists — pinned by
    # test_audit.py (vault-path-dependent fixtures) + TestConfigVersionMigration.
    "obsidian_vault_path": "",
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
    "vault_rrf_dense_weight": 1.0,
    "vault_rrf_bm25_weight": 1.0,
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
    # Streaming-indexer embed batch (Track 5.6): chunks buffered per batched
    # insert_nodes call — K texts per provider embed round-trip (and one
    # lancedb transaction per batch) instead of one per chunk. Reindex-free
    # and reindex-NEUTRAL (chunk ids/hashes untouched; parity pinned by
    # TestEmbedBatchParity). <=1 restores the legacy per-chunk insert path
    # byte-for-byte. Config-only — no UI control.
    # TRADE-OFF: the whole-batch embed round-trip runs while _index_mutation_lock
    # is held, so a larger batch lengthens the worst-case stall a *concurrent*
    # vault chat's retrieval sees on that lock (bounded by the 120 s retrieval-
    # lock acquire, so a clean error, never a hang). Bigger = fewer round-trips
    # but longer single lock holds; the default 16 balances the two.
    "vault_embed_batch_size": 16,
    # Wikilink graph expansion (query-time, no reindex): after retrieval,
    # widen the result set with chunks from notes the seeds link to AND
    # notes that link into them, before the rerank stage decides what
    # survives. The toggle is body-overridable (live per Send); the caps
    # are config-only. neighbor_cap bounds distinct neighbour notes per
    # query, node_cap the total chunks appended, score_decay scales a
    # neighbour's inherited seed score (matters on the no-reranker path).
    "vault_wikilink_expansion": False,
    "vault_wikilink_neighbor_cap": 10,
    "vault_wikilink_node_cap": 24,
    "vault_wikilink_score_decay": 0.5,
    # Thesaurus query expansion (query-time, no reindex): retrieve a few
    # synonym-substituted variants of the query — driven by the curated
    # `_abreviations.md` / `_tags.md` files at the vault root — and union
    # their hits into the candidate pool, bridging the vault's bilingual
    # FR/EN shorthand. The toggle is body-overridable (live per Send);
    # max_variants is config-only. The primer (separate toggle) injects a
    # compact abbreviation glossary into the system prompt so the LLM can
    # read the shorthand that survives into the retrieved context.
    "vault_thesaurus_expansion": False,
    "vault_thesaurus_max_variants": 3,
    "vault_primer_enabled": False,
    "vault_primer_max_chars": 1500,
    # The two curated glossary files are configurable (vault-relative paths,
    # resolved under the vault root in ObsidianVaultManager._get_thesaurus).
    # Defaults are the historical hard-coded root filenames; an empty string
    # disables that slot. Renaming/relocating either file is reindex-free —
    # the loader is signature-cached on the resolved paths.
    "vault_thesaurus_abbrev_path": "_abreviations.md",
    "vault_thesaurus_tags_path": "_tags.md",
    # Primer content overrides (empty ⇒ use the built-in defaults in
    # rag/thesaurus.py). vault_primer_header replaces the glossary intro
    # sentence; vault_primer_core_terms is a comma-separated priority list of
    # abbreviations that survive truncation when the budget is tight. These
    # generalise the (FR/EN psychiatry-tuned) built-ins to any corpus without
    # editing code.
    "vault_primer_header": "",
    "vault_primer_core_terms": "",
    # Agent mode (opt-in). When True, /api/obsidian/chat routes
    # through the ReAct agent loop with vault_search / vault_read_note /
    # vault_list_materials tools instead of the single-shot RAG path.
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
    # Vision / OCR call bounds (indexing-time image description + scanned
    # PDF OCR). Separate from local_request_timeout_s (chat-only): these
    # are always on so a runaway / stuck local vision model cannot stall a
    # long indexing run. vision_timeout_s is the per-call HTTP timeout;
    # the *_max_tokens cap the generation length.
    "vision_timeout_s": DEFAULT_VISION_TIMEOUT_S,
    "vision_max_tokens": DEFAULT_VISION_MAX_TOKENS,
    "ocr_max_tokens": DEFAULT_OCR_MAX_TOKENS,
    # Negative-result cooldown after a failed vision/OCR call (fast-fail
    # window; 0 disables). During indexing every image inside the window
    # is skipped, so shorter = fewer dropped images after a blip.
    "vision_failure_cooldown_s": DEFAULT_VISION_FAILURE_COOLDOWN_S,
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
    # --- Plain Chat knobs ---
    # Persisted defaults for the RAG-free Plain Chat panel.  /api/plainchat
    # accepts temperature/system_prompt as per-request overrides; the route
    # falls back to these when the body omits them.  chat_system_prompt is
    # used as the FULL system prompt (no retrieval grounding to protect).
    "chat_temperature": 0.3,
    "chat_system_prompt": "You are a helpful assistant.",
    # --- Prompt Hub (transparency panel) ---
    # Gates the in-memory capture of the effective system prompt last sent per
    # workflow (see core/prompt_capture.py + GET /api/prompts). Default on: it is
    # a read-only, local-only transparency aid and the capture is a cheap
    # per-request dict write. Off makes every record() call a no-op, so the Hub
    # shows only "not captured yet" placeholders.
    "prompt_capture_enabled": True,
    # --- Deck Generator knobs ---
    # Persisted defaults for the Deck Generator.  /api/deck/generate
    # accepts these as per-request overrides; api/routes/deck.py falls
    # back to these when the body omits them.  Ranges mirror the
    # _*_MIN/_*_MAX constants in api/routes/deck.py.
    "deck_temperature": 0.3,
    "deck_max_sections": 8,
    "deck_agent_max_iterations": 6,
    # Per-section resilience (deckgen retry). A local backend (LM Studio /
    # Ollama) fails a generation transiently — a memory hiccup, a JIT model
    # reload, a momentary timeout. The SDK's own retries are off (see
    # core/providers/lms.py) so each call stays bounded by one timeout;
    # recovery is owned here, where it is cancel-aware and surfaced as info.
    # deck_section_max_attempts: total tries per outline/section turn before
    # the section degrades to a placeholder frame. deck_retry_backoff_s:
    # linear backoff base (waited × attempt) between tries.
    "deck_section_max_attempts": 3,
    "deck_retry_backoff_s": 3,
    # Per-section output cap (tokens). The agent loop otherwise uses
    # online_max_tokens (4096) for every reasoning call, local included; a
    # smaller per-section budget means shorter slides, less KV-cache / RAM
    # pressure on a local backend (fewer OOMs), and faster turns. Injected
    # into the deck runner's online_max_tokens; does not touch other paths.
    "deck_section_max_tokens": 2048,
    # Per-section checkpoint + resume. When on (default), a deck generation
    # persists its outline + each completed section under
    # BASE_DIR/deckgen/checkpoints/, keyed by a hash of the request, and a
    # re-submitted identical request resumes from the first not-yet-generated
    # section. The checkpoint is deleted on a fully successful generation; the
    # body flag `force_fresh` discards it and starts over.
    "deck_resume_enabled": True,
    # Opt-in final-stage LLM .tex integrity review + auto-repair
    # (deckgen/review.py, fired by /api/deck/generate when enabled).
    # deck_review_enabled is the persisted default for the off-by-default
    # panel checkbox (the body field `review_enabled` overrides per run).
    # deck_review_model "" ⇒ the configured chat model for the active
    # provider; set a name to point the integrity pass at a stronger model
    # (mirrors refactor_review_model). deck_review_max_tokens must be large
    # enough to re-emit the whole repaired document.
    "deck_review_enabled": False,
    "deck_review_model": "",
    "deck_review_max_tokens": 4096,
    "deck_compile_timeout_s": 180,
    "deck_compile_max_iters": 2,
    "deck_compile_engine": "pdflatex",
    # --- Note Refactor knobs ---
    # Scope sub-folder analyzed by /api/refactor/plan; validated as a
    # vault-relative no-traversal path in api/routes/config.py.
    # "" ⇒ no default scope: the user must pick a folder before a plan
    # runs (an empty scope resolves to None → a clean 400 in the route).
    # refactor_extract_model "" ⇒ fall back to vision_model.
    "refactor_scope_subdir": "",
    "refactor_extract_model": "",
    "refactor_table_double_read": True,
    # When True the extracted-text callout drops the descriptive
    # "This image is…/Transcribed Text:" preamble for EVERY image (the
    # per-image `strip` flag stays additive). Read identically by the plan
    # and the apply writer so the WYSIWYG guard holds; a body override is
    # deliberately NOT accepted (config is the single source of truth).
    "refactor_strip_preamble_default": False,
    # Opt-in per-note LLM prose review (refactor/review.py). review_model ""
    # ⇒ fall back to the configured chat model for the active provider; set a
    # model name to point the quality-sensitive prose pass at a stronger
    # model than everyday chat (mirrors refactor_extract_model for vision).
    "refactor_review_model": "",
    "refactor_review_max_tokens": 1024,
    # Max tokens for the applyable LLM formatting rewrite (refactor/llm_edit.py
    # rewrite_formatting). Larger than the review/summary/chart cap because it
    # re-emits the whole note/section body. The summary + chart actions reuse
    # refactor_review_max_tokens (bounded outputs); the model for all three is
    # refactor_review_model (→ chat model when empty).
    "refactor_rewrite_max_tokens": 4096,
    # Phase 2 vault-write knobs.  refactor_archive_dir "" ⇒ default
    # BASE_DIR/refactor/archive/<vault_key>/ (local disk only — NOT iCloud);
    # set an absolute path to point at a backed-up folder.  Must resolve
    # OUTSIDE the vault (re-checked at apply time).  refactor_thumb_max_side
    # bounds the longest side of the in-vault PNG thumbnail the archiver
    # writes when it moves a full-res original out.
    "refactor_archive_dir": "",
    "refactor_thumb_max_side": DEFAULT_REFACTOR_THUMB_MAX_SIDE,
    # Library Audit (kb_harmonizer) settings. The audit subsystem
    # is fully manual — these keys are read only when the user
    # explicitly clicks "Run Scan" on the Library Audit tab.
    "audit_attachments_subdir": "Z_attachments",
    "audit_biblio_articles_subdir": "biblio_articles",
    "audit_zotero_notes_subdir": "Z_Zotero_Notes",
    "audit_master_bib_path": "_master.bib",
    "audit_zotero_sqlite": "~/Zotero/zotero.sqlite",
    "audit_zotero_storage": "~/Zotero/storage",
    "audit_annotations_read_threshold": 5,
    "audit_biblio_skip_prefix": "z_item",
}

KNOWN_KEYS = frozenset(_DEFAULTS.keys())


# --- config_version migrations (Track 5.7) -----------------------------------
# DEFECT this closes: config.json had no schema version, so a key renamed or
# retired in a newer release could never be migrated — and because save_config
# materialises EVERY default into the file, keys dropped from _DEFAULTS linger
# in every existing config forever (no code path ever removed them). Each
# release that changes the persisted schema now bumps CONFIG_VERSION and adds
# one pure step below; load_config() applies the pending chain exactly once
# and rewrites the file (atomic, under the write lock).
# SAFE W.R.T. STATE: steps are pure dict→dict functions applied to the RAW
# persisted mapping before the defaults merge; a config already at
# CONFIG_VERSION is untouched (single int comparison per load, and only on a
# cache-miss load at that); a config from a NEWER app version (downgrade)
# has version > CONFIG_VERSION and is left alone — never "migrated backwards".
# Failed writes degrade to in-memory migration (retried next load).
# INVARIANT (pinned by smoke_test.py::TestConfigVersionMigration): after any
# load, the persisted file is at CONFIG_VERSION and contains only KNOWN_KEYS.

def _migrate_0_to_1(data: dict) -> dict:
    """0→1: prune keys unknown to this release (see the defect note above).

    Safe because every config read goes through ``.get()`` on KNOWN_KEYS —
    an unknown key is dead weight by construction, never load-bearing.
    ``config_version`` itself is in KNOWN_KEYS (it lives in _DEFAULTS).
    """
    return {k: v for k, v in data.items() if k in KNOWN_KEYS}


_MIGRATIONS: dict = {
    0: _migrate_0_to_1,
}


def _apply_migrations(data: dict) -> tuple[dict, bool]:
    """Run pending migration steps on the raw persisted mapping.

    Returns ``(migrated_data, changed)``. A missing/garbage version is treated
    as 0; a version from the future (downgraded app) is returned unchanged.
    """
    raw_version = data.get("config_version")
    version = raw_version if isinstance(raw_version, int) and raw_version >= 0 else 0
    if version >= CONFIG_VERSION:
        return data, False
    while version < CONFIG_VERSION:
        step = _MIGRATIONS.get(version)
        if step is None:  # gap in the chain — a bug; stop rather than skip
            logger.warning("No config migration registered for version %d", version)
            break
        data = step(dict(data))
        version += 1
    data["config_version"] = version
    return data, True


def _write_config_file(data: dict) -> None:
    """Atomically write *data* to CONFIG_FILE (temp + fsync + replace).

    Shared by save_config and the migration path so the durability contract
    (fsync before rename — see save_config's note) exists exactly once.
    Caller holds ``_config_write_lock``.
    """
    fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(CONFIG_FILE), text=True)
    try:
        try:
            f = os.fdopen(fd, 'w')
        except Exception:
            os.close(fd)
            raise
        with f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, CONFIG_FILE)
        _invalidate_config_cache()
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def load_config() -> dict:
    """Load user configuration from disk (stat-cached between rewrites).

    On a cache-miss read of an older-versioned file, pending ``_MIGRATIONS``
    are applied and the migrated file is persisted back (best-effort — a
    failed write still serves the migrated view in memory and retries on the
    next cold load).
    """
    global _config_cache, _config_cache_key
    # Cache hit: one stat() syscall instead of open + read + json parse.
    # Returns a deep copy because callers mutate the result (save_config
    # does current.update(...), routes pop/rewrite keys) — handing out a
    # shared reference would poison the cache for every later caller.
    key = _config_file_key()
    with _config_cache_lock:
        if _config_cache is not None and _config_cache_key == key:
            return copy.deepcopy(_config_cache)
    defaults = copy.deepcopy(_DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            data, migrated = _apply_migrations(data)
            if migrated:
                # Persist the migrated shape once so the chain never re-runs.
                # Concurrent cold loads may both migrate (idempotent, atomic
                # writes) — harmless; the write lock serialises the replaces.
                try:
                    with _config_write_lock:
                        _write_config_file(data)
                    key = _config_file_key()  # re-key: we just rewrote the file
                except OSError:
                    logger.warning("Could not persist migrated config; using in-memory result", exc_info=True)
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

def load_config_readonly() -> "MappingProxyType":
    """Return a READ-ONLY view of the cached config WITHOUT the per-call deepcopy.

    ``load_config()`` hands out a fresh ``copy.deepcopy`` on every call because
    callers may mutate the result. But the retrieval hot path has several helpers
    (e.g. ``_resolve_reranker_device_mode``, ``_get_thesaurus``) that only
    ``.get()`` scalars and are each invoked per query — for them the deepcopy is
    pure waste. This returns a ``MappingProxyType`` over the *shared* cached dict:
    no copy, and any accidental top-level mutation raises ``TypeError`` loudly
    instead of silently poisoning the cache for every other caller.

    Safe to share the underlying dict because the cache is only ever *replaced*
    wholesale (``load_config`` rebinds ``_config_cache`` on a miss), never mutated
    in place — so the proxy stays consistent with what it wrapped. Callers that
    mutate, or that ``.append`` into a nested list/dict, MUST use ``load_config()``
    instead (MappingProxyType guards only the top level). On a cold/invalidated
    cache this falls back to ``load_config()`` (which repopulates the cache, paying
    one deepcopy) and then wraps the freshly cached dict.
    """
    key = _config_file_key()
    with _config_cache_lock:
        if _config_cache is not None and _config_cache_key == key:
            return MappingProxyType(_config_cache)
    load_config()  # populate/refresh the cache (one deepcopy on this cold path)
    with _config_cache_lock:
        if _config_cache is not None:
            return MappingProxyType(_config_cache)
    return MappingProxyType({})


def save_config(config: dict):
    """Persist updated configuration keys to CONFIG_FILE atomically.

    The write machinery (temp + fsync-before-rename durability + eager cache
    invalidation) lives in :func:`_write_config_file`, shared with the
    migration path. ``config_version`` is server-owned: whatever the caller
    passes, the stamped version is always this release's CONFIG_VERSION (the
    route additionally strips the key defensively).
    """
    with _config_write_lock:
        current = load_config()
        current.update(config)
        current["config_version"] = CONFIG_VERSION
        _write_config_file(current)

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
