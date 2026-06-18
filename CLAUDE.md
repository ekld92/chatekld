# ChatEKLD 2026 Project Notes

ChatEKLD is a local macOS app for PDF summarisation and Obsidian vault RAG. It uses Flask, PyWebView, SQLite, LlamaIndex, Ollama, and LM Studio.

> **Layered docs.** This file holds the cross-cutting rules and invariants only. Deep implementation notes live in subtree `CLAUDE.md` files that load on demand when you work in those directories: `rag/CLAUDE.md` (indexing pipeline, locks, prewarm, retrieval mechanics), `core/llm/CLAUDE.md` (adapters, fallback policy, usage/pricing, tool wire formats), `core/agent/CLAUDE.md` (agent loop), `audit/CLAUDE.md` (Library Audit internals + endpoint details). History lives in `CHANGELOG.md`. Deferred items are under **Known Issues** at the bottom.

## Current Workflows

- Single Paper. Upload one PDF. Extract text. Stream a tunable summary with prompt presets, document-type prompts, audience, focus question, and generation controls.
- Obsidian Vault. Index `.md` and `.pdf` files. Query the vector index through dense retrieval, optional BM25 lexical retrieval, RRF fusion, and optional cross-encoder reranking; inspect the indexed-material manifest.
- Library Audit. Read-only reconciliation of the Obsidian vault against Zotero (SQLite snapshot + Better BibTeX `_master.bib`), local PDFs (annotations + duplicates) and macOS Finder tags. Six reports plus a per-citation-key inventory. Engine vendored from kb_harmonizer under `audit/`. Strictly manual — the scan runs only when the user clicks Run Scan.
- Deck Generator. Turn a topic + free-form instructions into a LaTeX **Beamer** deck grounded in the indexed vault, in the user's own template's house style. Pick (and edit in-app) a Beamer template; deckgen reuses its preamble + custom macros (`\citefoot`, `\commonlogo`), maps cites to the template's `_master.bib` when possible, and scaffolds a compile-ready `<slug>/<slug>.tex` + `Makefile` into the suite. **Emit-only** — the user compiles with `make`. Details in `deckgen/README.md`.

## Module Ownership

- `app.py` creates the Flask application and registers blueprints. It also exposes `FEEDBACK_FILE` and `CONFIG_FILE` as module-level attributes so the reset test can patch them via `mock.patch('app.FEEDBACK_FILE', ...)`.
- `api/routes/` owns route handlers.
- `api/security.py` owns local-origin request checks and `sanitise_error_msg`.
- `api/validators.py` owns request-body coercion helpers (`coerce_int_in_range`, `coerce_float_in_range`, `coerce_bool`, `coerce_enum`, `coerce_regex`, `coerce_non_empty_string`, `coerce_string_max_len`, `first_valid`, `MISSING`). Every route layer imports from here rather than reimplementing NaN/Inf/range/clamp logic — `api/routes/vault.py::_resolve_chat_params` is the canonical caller.
- `core/config.py` owns config and report type persistence. Defaults include the Settings-window keys: the online plumbing (`online_timeout_s`/`online_max_retries`/`online_max_tokens`, `fallback_provider`/`fallback_on`), the vault chat knobs (`vault_*`), and the per-function generation defaults `paper_*` (`paper_temperature`/`paper_num_ctx`/`paper_max_tokens`/`paper_top_p`/`paper_repeat_penalty`) and `deck_*` (`deck_temperature`/`deck_max_sections`/`deck_agent_max_iterations`).
- `api/routes/config.py` owns `POST /api/config`, which (in addition to the `llm`-routing and `audit_*`-strip) runs `_validate_llm_config_keys`: a per-key coerce/clamp map (reusing `api/validators.py`) over every numeric/enum/bool LLM knob the Settings window writes. An out-of-range or malformed value is **dropped** (the prior persisted value survives) rather than stored. This is the only place those previously-UI-less keys (timeout/retries/tokens/fallback/reranker device & backend/prewarm, plus `agent_wall_clock_s` and `local_request_timeout_s`) get validation, so widen the map when adding a new Settings control.
- `core/constants.py` owns defaults, paths, limits, and file extension policy.
- `core/database.py` owns SQLite setup and the database lock (`DB_LOCK`).
- `core/providers/` owns the LOCAL Ollama and LM Studio adapters used for embeddings and the legacy local-chat path.
- `core/llm/` owns the provider-agnostic chat layer (`types`, `base`, `factory`, `policy`, `usage`, `retry`, `prompt`, `redact`, `tool_schema`) plus adapters for `local`, `openai`, `anthropic`, `google`.
- `core/agent/` owns the opt-in ReAct agent layer for vault chat (`protocol`, `tools`, `vault_tools`, `budget`, `loop`). Imports flow agent → llm; the agent layer never reaches into the indexer's internals.
- `core/utils.py` owns `ReaderWriterLock`, `RagOperationLock`, parameter parsers, and `write_text_atomic` (the temp-sibling + `os.replace` helper every non-append file write outside `rag/` should use; `rag/vault.py::_write_json_atomic` is its JSON twin, and `audit/engine/bridge.py` keeps a local copy so the vendored tree depends only on `audit/config.py`).
- `services/pdf_service.py` owns upload extraction and database insertion.
- `services/vision.py` owns `VisionManager` and `GLMOCRManager` singletons.
- `rag/vault.py` owns vault indexing, status messages, and chat entrypoints. `rag/engine.py` owns the LlamaIndex query engine. `rag/summarizer.py` owns `summarise_stream` and `build_prompt`. `rag/lancedb_store.py` owns the optional binary vector backend (`NormalizingLanceDBVectorStore` + helpers); both `rag/vault.py` and `scripts/migrate_vector_store.py` import from it (it imports only lancedb + llama-index, never project modules, so there is no cycle).
- `audit/` is the vendored Library Audit subsystem: `config.py` (Settings adapter), `core/` (read-only connectors), `engine/` (bridge + inventory + report builders), `manager.py` (`audit_manager` singleton, one background scan thread), `serialize.py`, `scan.py` (CLI: `python -m audit --check ...`).
- `api/routes/audit.py` owns the `/api/audit/*` blueprint, including the path-traversal-rejecting config validators and the macOS-only `reveal` endpoint.
- `deckgen/` is the standalone Beamer-deck orchestrator. Its **core is app-independent and `requests`-free**: `template.py` (split a template into preamble/opening/closing, scan custom macros by following `\usepackage` into local `.sty`, parse the `\addbibresource` bib, find the suite root), `outline.py`, `sections.py`, `prompts.py`, `assemble.py` (`assemble_with_template` + `validate`'s dangerous-macro + hallucinated-citekey guards), `scaffold.py` (`scaffold_deck` writes `<slug>/<slug>.tex` + `Makefile`; keeps its own atomic-writer), `result.py` (`ChatResult`, shared, no third-party imports). `client.py` is the HTTP driver for the **CLI** (`__main__.py`); `inprocess.py` is the **only** app-coupled module — `InProcessChatRunner` drives `core.agent.run_agent_loop` over `rag.vault.obsidian_manager` directly, exposing the same `.chat(...) -> ChatResult` contract the CLI client does. Imports flow deckgen-core ← {client, inprocess}; the core never imports the app.
- `api/routes/deck.py` owns the `/api/deck/*` blueprint: `load-template` (validate + scan), `generate` (SSE, in-process orchestration), and the native file/folder pickers. Path validators reject traversal/system roots, mirroring `audit.py`.
- `static/js/` owns UI behaviour. `updateProviderBadge` lives in `ui.js` so `config.js` and `app.js` can both import it without a circular dependency. `audit.js`, `deck.js`, and `settings.js` follow the same `ui.js`+`api.js`-only import rule. `settings.js` owns the **LLM Settings** modal (`#settings-modal`): the previously-UI-less plumbing keys plus the `paper_*`/`deck_*` persistence. The vault knobs, OCR/Vision selects, and provider/model selectors physically live inside the same modal but are still operated **by element id** by their original owners (`vault.js`, `config.js`) — the markup was relocated, not the logic — so `settings.js` deliberately does not touch them (no double-save).

## Provider Rules

- `provider` selects `ollama`, `lm_studio`, `openai`, `anthropic`, or `google`.
- Online providers (`openai`, `anthropic`, `google`) are CHAT-ONLY — they never expose an embedding interface. When the active chat provider is online, the indexer, vault chat, and prewarm resolve a local embed provider via `core.config.resolve_embed_provider(cfg, provider_name)` (falls back to `embed_provider`, default `ollama`).
- `core.providers.get_provider(name)` always returns a local `Provider`; called with an online name it silently substitutes the configured local embed provider. `core.llm.get_llm_provider(name)` returns the unified `LLMProvider` chat interface for both local and online.
- API keys come from env vars (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`) at call time — never persisted to `config.json`, never returned by `/api/config`. `api.security.sanitise_error_msg` runs through `core.llm.redact.redact` so an accidental key leak in an exception is stripped before the UI sees it.
- Per-provider model selections persist independently: `llm` for local providers, `openai_model` / `anthropic_model` / `google_model` for online; `core.config.resolve_chat_model(cfg, provider_name)` picks the right field. `/api/config` routes the UI's generic `llm` field into the active online provider's key and then **drops** `llm` from the payload — persisting an online model name into `llm` would clobber the local selection (pinned by `smoke_test.py::test_03b`).
- `/api/models` routes to the active provider (curated list for online). `/api/status` ok=True for online providers just means the API key is set. `/api/pull` is Ollama-only (chunks serialised via `.model_dump()`).
- Fallback policy: `fallback_provider` + `fallback_on` config keys; falls back only on transient errors and only **before the first streamed token**; hard quota/billing errors are terminal. Full mechanics in `core/llm/CLAUDE.md`.
- Token / USD-cost usage is recorded by `core.llm.usage.usage_tracker` for every request and surfaced on `/api/usage`; pricing in `PRICING_TABLE`, overridable via `llm_pricing_overrides`. Details in `core/llm/CLAUDE.md`.
- OCR and vision settings are separate from chat provider settings. `/api/vision-models?provider=...` lists models for Ollama or LM Studio only; `ocr_provider` / `vision_provider` decide which backend receives image calls.

## Vault Rules

- The indexer reads `.md` and `.pdf` files. `.docx` is excluded by design. PDFs are folder-driven (every `.pdf` in an included folder is eligible). Images are note-driven: only files whose extension appears in `vault_image_exts` AND that are referenced from an included markdown note are described by the vision model and embedded; `[]` disables image indexing; skipped images are counted in `skipped_image_count`.
- `obsidian_vault_path` is validated before save — broad roots and system directories are rejected. `vault_exclude_dirs` stores vault-relative paths; exclusions are applied before files are read.
- Index updates are incremental: unchanged chunks are skipped by document hash, changed chunks are replaced, stale chunks are deleted. Indexing streams end-to-end (peak RAM bounded by the largest single document); checkpoints persist after 500 inserts *and* ≥ 10 minutes since the previous one (each checkpoint rewrites the whole store); per-insert failures are tolerated up to 20 consecutive. Details in `rag/CLAUDE.md`.
- **Vector store backend** (`vault_vector_backend`, `simple` | `lancedb`, default `simple`). `simple` is the legacy JSON `SimpleVectorStore`; `lancedb` is the binary Apache-Arrow backend (low resident RAM, fast cold start, transactional inserts — no full-store JSON dump for *vectors*, though the docstore/index-store JSON still persist at the checkpoint cadence). The knob governs only **fresh** builds; an existing index's backend is authoritative from `obsidian_meta.json` (`vector_backend`; missing ⇒ `simple`). Migrate an existing vault in place with **no re-embedding** via `python scripts/migrate_vector_store.py` (app closed), or set the knob and reindex. On lancedb the embeddings are unit-normalized so default-L2 ranking equals cosine; list metadata (`attachments`) is JSON-stringified in the row while the docstore keeps the list (`store_nodes_override=True`). Mechanics in `rag/CLAUDE.md`.
- Chunking is **pinned** (changing it forces a reindex; `TestReindexInvariant` guards it): `.md` at heading boundaries (`MarkdownNodeParser`), PDF flat-text via `SentenceSplitter` (512-token chunks, 64-token overlap). Chunk IDs are `"{rel_path}::{sha1(i + text)[:16]}"`; page-range documents from large PDFs (> 1000 pages, one document per 1000-page range) salt the hash input with their `page_start` so ranges of the same file cannot collide — single-document files keep the unsalted input, so they never re-embed. Details in `rag/CLAUDE.md`.
- Vault RAG prompts treat retrieved notes/PDFs as untrusted source text, apply a similarity cutoff, and construct per-query LLM/embedding objects instead of mutating global LlamaIndex settings.
- Vault chat accepts query-time knobs as per-request overrides on `/api/obsidian/chat`, resolved by `_resolve_chat_params` (body wins, then the persisted `vault_*` config key, then the engine default) — **all reindex-free**:
  - Generation: `top_k` (1-32, default 8), `similarity_cutoff` (0.0-1.0, default 0.25), `prompt_mode` (`strict`/`balanced`/`exploratory`/`concise`), `temperature` (0.0-2.0, default 0.3), `system_prompt` (≤ `SYSTEM_PROMPT_LIMIT` = 4000 chars; a prefix over the mode template — the safety preamble stays app-controlled).
  - Retrieval: `hybrid_enabled`, `reranker_enabled` (both default `True`), `mmr_enabled` + `mmr_lambda` (default off / 0.5), `query_expansion` + `num_queries` (default off / 3, local providers only), `rerank_pool_ceiling` (10-200, default 50).
- Reranker model is set via the `vault_reranker_model` config key (editable in the **LLM Settings** window, or `POST /api/config` directly — both local-only) but is **never** a `/api/obsidian/chat` body override (prevents a malicious page from triggering arbitrary HuggingFace downloads). `launch.py` enables `HF_HUB_OFFLINE=1` at startup when the model is already cached. `vault_reranker_device` (`auto`/`cpu`/`mps`, default `auto`, also Settings-window/`/api/config`-set, not a body override) selects the cross-encoder's device: `auto` keeps the library's own inference (MPS on Apple Silicon when available), `cpu` is the escape hatch that keeps unified memory free for the LLM, and any non-CPU failure degrades to CPU for the session instead of disabling reranking.
- `vault_prewarm_enabled` (default `True`) gates the launch-time prewarm inside `ObsidianVaultManager.prewarm()`: when off, prewarm reports `skipped` (terminal for the UI — Send stays enabled) and the first chat lazy-loads instead. Defers the multi-GB load; does not remove it.
- Retrieval mechanics (RRF fusion, MMR, rerank pool sizing, cutoff semantics, BM25/reranker caches, the persisted BM25 sidecar under `obsidian_storage/bm25_index/`, prewarm, lock discipline) are documented in `rag/CLAUDE.md`.

## Agent Mode (Vault Chat)

- Opt-in alternative to single-shot RAG on `/api/obsidian/chat`: `#vault-agent-enabled` checkbox (`vault_agent_enabled`, default `False`) or body field `agent_enabled`. When off, `stream_chat` runs byte-for-byte identically — the agent layer is entirely additive.
- Three tools over `ObsidianVaultManager`: `vault.search(query, top_k≤12)`, `vault.read_note(rel_path)`, `vault.list_materials(filter?, limit≤200)`. Tool output is truncated and wrapped in the untrusted-content guard.
- Loop budget `vault_agent_max_iterations` (default 6, clamped 1–12); 300 s wall-clock cap. Two consecutive malformed tool-call iterations fall back to plain RAG against the original message; a session-level capability warning fires after two consecutive fallback turns.
- Full loop/fallback/wire-format details in `core/agent/CLAUDE.md` and `core/llm/CLAUDE.md`.

## Single Paper Rules

- Prompt presets are structured dictionaries in `core/constants.py`.
- Built-in report types are merged with saved custom/overridden report types by `core.config.load_report_types()`.
- `/api/summarise` accepts `temperature`, `num_ctx`, `max_tokens`, `top_p`, `repeat_penalty`, `focus_question`, `audience`, `language`, `report_type_id`, and optional `system_prompt`. The five generation knobs now resolve **body → persisted `paper_*` config (set in the Settings window) → hard-coded clamp default** in `paper.py::_extract_summarise_params` (the nested `parse_*` re-clamps the config value too). `deck.py` mirrors this with `deck_*` for `max_sections`/`agent_max_iterations`/`temperature`. The inline LLM-parameter panels were removed from the tabs; only content/instruction inputs (system prompt, focus question, audience, language, document type, topic, template) remain on-tab.
- When `report_type_id` is supplied, the report type's `name` fills the `{document_type_line}` slot (default "Research Paper") and its `system_prompt` override applies in the same single `load_report_types()` lookup.
- Uploaded PDF text is wrapped as untrusted source material. Uploads stream to disk with a hard byte cap; PDF extraction runs in a subprocess so timeouts terminate the worker. PDFs over 1000 pages are extracted in 1000-page ranges and concatenated (`services/pdf_service.py::_extract_all_pages`), up to the shared `PDF_MAX_PAGES` ceiling.
- `/api/export-summary` accepts `format: "txt"` or `"md"` and writes to the user's Downloads directory. Feedback string fields are capped at 10 000 characters.

## Library Audit Rules

- **Manual-only.** No code path outside `POST /api/audit/scan` may call `audit_manager.start_scan()`; `create_app()` must leave the audit `idle` (pinned by `test_audit.py::TestFlaskAppBootDoesNotScan`).
- **Read-only.** External stores (Zotero SQLite via `mode=ro` snapshot, PDFs via pikepdf, BibTeX, frontmatter, Finder xattrs) are never mutated; the only writable file is `BASE_DIR/audit/mapping.json`.
- The audit reuses `obsidian_vault_path`; all other subpaths are validated config keys, and the generic `POST /api/config` strips `audit_*` keys so the validators cannot be bypassed.
- Lifecycle, result cache, Finder-tag/`reveal` platform limits, and frontmatter-warning surfacing are documented in `audit/CLAUDE.md`.

## OCR / Vision Availability Caching

The configured OCR/Vision model is always the model that gets called. `check_availability()` is informational only (cached 60 s); it never gates a call and never rewrites `self.model`. A 30 s negative-result cooldown fast-fails calls after a failure so per-page traffic cannot hammer a missing model. `set_model()` / `set_provider()` invalidate both caches immediately.

## JS Module Hierarchy

`ui.js` is the leaf module — it imports nothing from this project. `api.js` imports nothing from this project. All other modules (`config.js`, `vault.js`, `summarizer.js`, `audit.js`, `deck.js`, `settings.js`) import only from `ui.js` and `api.js`. `app.js` is the root and imports from all others. This prevents circular dependencies; in particular `config.js` must never import from `app.js`.

Third-party JS is vendored under `static/js/vendor/` — currently `marked.min.js` (v15.0.12, MIT). Never load runtime JS from a CDN: the PyWebView renderer's network is independent of the Python process, and a failed CDN fetch used to surface as `ReferenceError: Can't find variable: marked`, destroying the chat answer at render time. `vault.js::_renderAnswer` falls back to plain text when `marked` is unavailable. User-controlled strings (folder names, paths) must never be interpolated into `innerHTML` — use `createElement`/`textContent` (see `renderExclusions`) or an escape helper (see `audit.js::_esc`).

## Frontend Error Logging

`logError()` in `api.js` sends JS errors to `POST /api/log`; the server logs them via the `chatekld.js` logger, capped at 500 characters.

## API Routes

- `GET /api/status` · `GET /api/models` · `GET /api/vision-models`
- `GET /api/config` · `POST /api/config` — strips `audit_*` keys (use `/api/audit/config`); routes `llm` per Provider Rules.
- `POST /api/pull` · `POST /api/upload` · `DELETE /api/upload/<id>` · `POST /api/summarise`
- `GET /api/report-types` (`{"report_types": [...]}`) · `GET /api/report_types` (legacy flat list)
- `POST /api/export-summary` · `POST /api/feedback` · `GET /api/feedback/history`
- `POST /api/obsidian/index` · `GET /api/obsidian/status` · `GET /api/obsidian/materials` · `POST /api/obsidian/pause` · `POST /api/obsidian/cancel`
- `POST /api/obsidian/chat` — body accepts the Vault Rules knobs plus `agent_enabled: bool` and `agent_max_iterations: int (1-12)`.
- `POST /api/native-pick-folder` · `POST /api/reset` · `POST /api/log` · `GET /api/about` · `GET /api/usage` · `GET /api/pricing`
- `/api/audit/*` — `config` GET/POST, `status`, `scan`, `cancel`, `inventory`, `reports/<name>`, `mapping`, `reveal` (macOS-only). Bodies and payload shapes in `audit/CLAUDE.md`.
- `/api/deck/*` — `load-template` (POST `{path}` → `{tex, macros, bib_keys_count, suite_root}`), `generate` (POST SSE: reuses the `info`/`error`/agent-trace contract + a terminal `{"deck": {tex, warnings, tex_path, make_hint, …}}` frame), `native-pick-file`, `native-pick-folder`. Emit-only — writes `<out_dir>/<slug>/`.

Most routes require `X-Requested-With: ChatEKLD`.

## SSE Contract

Streaming routes emit JSON events, one per `data:` frame, ending with `data: [DONE]`:

```text
data: {"token": "..."}     # answer tokens
data: {"error": "..."}     # structured error (also mid-stream, before [DONE])
data: {"info": "..."}      # stage labels, fallback notices, agent usage footer
```

Agent-mode `/api/obsidian/chat` adds four event types before the final token stream:

```text
data: {"iteration": N}
data: {"thought": "..."}
data: {"tool_call": {"id": "...", "name": "...", "arguments": {...}}}
data: {"tool_result": {"tool_call_id": "...", "content": "...", "is_error": false, "truncated": false}}
```

## Limits

- PDF upload limit: 500 MB. PDF extraction timeout: 600 seconds. PDF page ceiling: `PDF_MAX_PAGES` = 20 000 (shared by the upload worker and the vault indexer; both extract in 1000-page ranges, so the ceiling bounds time, not memory).
- System prompt limit: `SYSTEM_PROMPT_LIMIT` = 4000 characters (shared by single-paper and vault-chat `system_prompt`; defined in `core/constants.py`).
- Feedback field cap: 10 000 characters per string field.
- Obsidian operation lock TTL: 3600 seconds per acquisition.
- **Timeout chain** (nested; outer must exceed inner or the inner cap is defeated): `local_request_timeout_s` (per local HTTP call) ≤ `agent_wall_clock_s` (one agent turn / each deck section, the exact user cap) ≤ SSE consumer `event_q.get(timeout=max(cap, _SINGLE_SHOT_FLOOR_S) + _STALL_MARGIN_S)` ≤ frontend fetch abort. The cap is config-driven (`agent_wall_clock_s`, default 300, parsed defensively with `coerce_int_in_range` so a hand-edited config can't crash the route). The consumer stall guard and the vault.js abort **derive** from it, but both are **floored at `_SINGLE_SHOT_FLOOR_S = 300`** (`_STALL_MARGIN_S = 30`; vault.js abort = `(max(cap,300)+60)*1000`): the agent path is bounded by its own deadline, but the *same* consumer loop also serves the single-shot RAG path whose only time guard is the consumer — so lowering the cap to bound agent turns must not starve a slow single-shot first token. The vault.js abort is computed **live at send time** in `_chatAbortMs()` (reads the `set-agent-wall-clock` control), NOT cached at init, so a change in the Settings modal takes effect on the next Send without reload. `deck.py` passes the cap to `InProcessChatRunner(turn_timeout_s=...)`. The `_CHAT_TOKEN_TIMEOUT_S = 300` constants are now just the default fallback.
- **Local request timeout** (`local_request_timeout_s`, default 0 = leave each path's SDK default): threaded through `core.providers.base.local_request_timeout()` into `OllamaProvider._client()` (ollama streaming + the agent tool path in `local.py`; the client is cached per `(host, timeout)` in `core/providers/ollama.py` so generation reuses one httpx pool — `get_provider()` returns a fresh provider each call) and `get_llm` (LlamaIndex Ollama `request_timeout`, whose own default is 30 s), plus the LM Studio `openai.OpenAI(timeout=...)` / `_LMStudioOpenAI` paths. Read per call so the knob applies without restart. **Scope: chat/generation only** — embeddings are intentionally excluded (no `OllamaEmbedding` timeout hook, and bounding indexing batches would cause spurious failures rather than recovery).

## Logging and Deletion Auditing

- `chatekld.log` is written under `BASE_DIR` (`~/Library/Application Support/ChatEKLD/` on macOS) for both dev and frozen builds.
- Every code path that calls `shutil.rmtree(OBSIDIAN_INDEX_DIR)` must first call `log_storage_deletion(reason)` from `core/utils.py` (stack trace to the log + JSON marker at `BASE_DIR/.last_deletion_log`). Current call sites: the `/api/reset` handler and the rename-failed fallback in `_archive_old_index_dir`.

## Tests

Run from the project virtual environment.

```bash
python -m py_compile app.py api/routes/*.py core/*.py core/providers/*.py core/llm/*.py core/llm/adapters/*.py core/agent/*.py rag/*.py services/*.py audit/*.py audit/core/*.py audit/engine/*.py audit/engine/reports/*.py deckgen/*.py
python -m pytest smoke_test.py test_concurrency.py test_vault_regressions.py test_llm.py test_validators.py test_agent.py test_audit.py test_deck.py tests/audit/ -v
```

`deckgen/tests/` covers the app-independent deckgen core (no server, no `requests`); `test_deck.py` (hermetic) covers the in-process runner + deck route validators. Run them with `python -m pytest deckgen/tests/ test_deck.py -v`.

The suite is **hermetic with respect to app data**: the root `conftest.py` sets `CHATEKLD_BASE_DIR` to a per-session temp directory before any app import, and `core/constants.py::_get_base_dir()` honours that override — tests can never read or write the user's real `~/Library/Application Support/ChatEKLD/` files. Set `CHATEKLD_BASE_DIR` yourself to point the suite at a fixture directory.

`test_llm.py` mocks all HTTP transports (no live keys; live smoke tests are gated behind `RUN_LIVE_PROVIDER_TESTS=1`). `tests/audit/` holds the ported kb_harmonizer connector tests; its `conftest.py` prepends the project root to `sys.path`.

Test imports must use canonical module paths, not private aliases re-exported via `app.py`:
- Use `from core.utils import RagOperationLock`, `from core.constants import DB_PATH`, `from core.database import DB_LOCK` (not `from app import ...`).
- Vault status internals: `from rag.vault import obsidian_manager`, then access `obsidian_manager._status_messages` / `_messages_lock` directly.
- Validator helpers: `from api.validators import coerce_bool, ...` (the old `api.routes.vault._coerce_*` aliases were removed).
- `FEEDBACK_FILE` / `CONFIG_FILE` remain importable from `app` because the reset test patches them at `app.FEEDBACK_FILE` / `app.CONFIG_FILE` — intentional, documented by the `# noqa: F401`.
- The vault loader is a generator: consume it with `list(manager._load_vault_documents(vault_dir))`; calling without iterating skips the `try/finally` that persists the PDF signature cache.

## Security Notes

- Local API calls require `X-Requested-With: ChatEKLD`.
- External `Origin` / `Referer` hosts are rejected; with neither header present (normal for PyWebView), the request is accepted only from a loopback `remote_addr`.
- Stored app data lives in an owner-only app data directory.
- Error messages are sanitised (and API-key-redacted) before they reach the UI.
- Streaming routes emit structured JSON error events before `[DONE]` when generation fails mid-stream.

## Known Issues / Deferred Work

All low priority; carried from the 2026-05 code review and the 2026-06-10 audit (whose fixes shipped — see `CHANGELOG.md`).

- **Dead Zotero attachments pipeline** — `audit/core/zotero.py` populates `ZoteroItem.attachments` that nothing reads; removal ripples through the vendored SQL/storage plumbing for little gain.
- **Latent online-adapter streaming paths** — `resolve_chat_provider(stream=True)` skips fallback and adapters' `stream()` don't reassemble tool-call deltas; unreachable today (the agent loop always passes `stream=False`).
- **Agent tool-arg validator covers scalars only** — extend `core/agent/tools.py` before adding a tool with `array`/`object` parameters.
- **Dead fields** — `LLMRequest.stop` is *read* by the three online adapters but never *set* by any caller (so it never takes effect; dropping it would require deleting the adapter reads too). `ReaderWriterLock._owner_thread` is written but never read; safe to drop.
- **LanceDB MMR / similarity-cutoff scale** — on the lancedb backend MMR runs client-side over an over-fetched pool (approximate, not the native dense-leg semantics), and the dense *score* is `exp(-distance)`, not raw cosine — so the dense-only-no-reranker `similarity_cutoff` (0.25) has a different scale on lancedb than on `SimpleVectorStore`. Ranking parity holds; absolute cutoff semantics differ. Low priority (reranker is on by default).
- **LanceDB orphan from a deleted note in a crash window** — crash-drift recovery reconciles re-processed chunks (delete-before-insert); a vector row whose source note was deleted *during* the pre-checkpoint crash window is never re-yielded, so it lingers until a full reindex. Rare; self-resolves on a clean full run.
