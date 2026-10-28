# Detailed project contracts

Start with [AGENTS.md](AGENTS.md) for operating boundaries, [CLI_GUIDE.md](CLI_GUIDE.md) for command effects, [LIBRARY_WORKFLOWS.md](LIBRARY_WORKFLOWS.md) for the public Library task flow, and [KNOWN_ISSUES.md](KNOWN_ISSUES.md) for current gates. Deep subsystem mechanics reside in package technical specifications:
- [api/README.md](api/README.md) — HTTP endpoints, SSE contracts, origin guards, request envelopes, and worker ceilings.
- [core/README.md](core/README.md) — `config.json` keys, `CHERQAIN_BASE_DIR` storage layout, lock TTLs, and atomic write primitives.
- [services/README.md](services/README.md) — PDF subprocess extraction, map-reduce summarisation, Plain Chat attachments, and OCR/vision bounds.
- [tagging/README.md](tagging/README.md) — Biblio Tagging, Zotero transports, `accession/` keyless lookup, `notesync/`, and `vault_history/` journals.
- [rag/CLAUDE.md](rag/CLAUDE.md) · [audit/CLAUDE.md](audit/CLAUDE.md) · [core/llm/CLAUDE.md](core/llm/CLAUDE.md) · [core/agent/CLAUDE.md](core/agent/CLAUDE.md) · [deckgen/README.md](deckgen/README.md) · [docgen/README.md](docgen/README.md).

| Need | Section |
| --- | --- |
| Current workflows and module ownership | [Workflows](#current-workflows), [modules](#module-ownership) |
| Providers, vault, and agent behavior | [Provider rules](#provider-rules), [vault rules](#vault-rules), [agent mode](#agent-mode-vault-chat) |
| HTTP, JavaScript, and streaming | [JS hierarchy](#js-module-hierarchy), [API routes](#api-routes), [SSE](#sse-contract) |
| Tests, security, build | [Tests](#tests), [security](#security-notes), [build](#build--distribution) |

Dated sections below retain their revision scope. Verify runtime behavior and installed versions before treating them as current.

## Ask timing and Create context guidance (28 October 2026)

Single-shot Ask usage frames filter by `vault_rag`, provider, model, and start time (`prompt_capture_recorded` prevents overwriting the engine-side Prompt Hub capture). Concurrent Read/Create calls do not enter Ask export totals. Prewarm publishes `building_wikilinks` separately from `loading_reranker` and blocks Ask until completion. Local `vault_rag` usage latency measures `stream_chat` entry through stream termination; online adapters retain provider-call timing.

Create checks its captured vault, embedding, and index-generation/file identity context throughout generation. An indexing preflight warns that corpus changes can interrupt the run; saved sections remain reusable under a compatible context.

## Read outcome and accounting boundary (28 October 2026)

An online summary with no non-whitespace text fails at the workflow boundary after the stream finishes without reclassifying billed records or triggering fallback/replay. Empty map-part notes abort before synthesis. Available usage is emitted before the terminal error frame.

## Read lifecycle and native path selection (28 October 2026)

Read uses `ui.js::makeAnswerRenderer` (vendored Markdown parser + shared HTML sanitizer) with raw Markdown stored separately for copy/export. `summarizer.js` uses `taskBegin`/`taskUpdate`/`taskEnd` under single-controller ownership; Reset invalidates ownership and preserves partial text for export. Config native pickers accept explicit `kind` (`sqlite`, `bib`, `markdown`) and `relative_to_vault` / `under_attachments` with realpath containment.

## Daily workflow documentation

The primary **Review library update** action observes tracked changes and opens preparation confirmation (`operation_mode: prepare_text | update_search`). Review names/tags/notes or skip, apply selected changes, then obtain a fresh search preview and explicitly confirm indexing. Partial or uncertain apply results block continuation until inspected. The maintained user procedure is [Daily vault and search update](LIBRARY_WORKFLOWS.md#daily-vault-and-search-update), mirrored in the [User Guide](static/help/guide.md#daily-vault-and-search-update).

### Library operation intent and monitoring contract

`api/routes/online_ocr.py` owns admission and recovery (`run_index: true` with `operation_mode: prepare_text | update_search` on `/api/online-ocr/session/preview` and `/session/start`). `full_rebuild: true` is refused for daily modes. `prepare_text` forbids `confirm_index: true`; `update_search` requires it. `no_external: true` selects `NativeOnlyOCR`; otherwise external consent and configured OCR/vision identities are checked. Tag-generation consent belongs to the Library session/proposal routes.

The `intent-v1:` token binds mode, `initiating_action` (`library_review` / `targeted_retry` for preparation; `search_only` / `final_search_update` / `search_update` for indexing), source identities, selection paths, embedding identity, and extraction settings. Schema-2 session receipts record `operation_mode`, `initiating_action`, and `application_version`. Promotion re-reads durable intent before publishing caches or calling the indexer. `prepare_text` receipts cannot use `/session/continue-index`.

`GET /api/online-ocr/status` is authoritative for current extraction and standalone index operations (`rag/vault.py::get_progress_snapshot` observes without draining messages). The client allows one in-flight status request, polls every 1.5 s while busy (5 s idle), and reconciles on entry, focus, visibility, and `online` events. Vector completion is distinct from lexical (`preparing` / `ready` / `disabled`) readiness.

### Recovery import (27 October 2026)

`POST /api/online-ocr/recovery/import` requires `confirm_import`, `confirm_index`, a bounded recovery ID, and the reviewed catalog hash. `core.recovery_import` validates source containment/hashes, immutable snapshots, canonical payloads, complete physical page coverage, and review-copy hashes under the server operation lock and promotion journal. No OCR is performed by import.

### Progress and bounded extraction (27 October 2026)

Online indexing status includes a non-draining `index_progress` snapshot. PDF page rendering selects a scale within the 20-million-pixel budget before raster allocation; online request rasters whose PNG exceeds 20 MiB use JPEG95 transport encoding recorded in the page receipt. Images are re-encoded locally without source metadata.

### Online extraction failures (26 October 2026)

`core.online_ocr` records bounded, secret-redacted `error_detail`, `http_status`, `provider_code`, provider/model, `failure_scope`, and `next_step`. Explicit image rejections (400/413/415/422 with image-specific evidence) are source-local and deferred during daily preparation; account, configuration, rate-limit, and transport failures remain global and fail-closed.

### Workspace composition (26 October 2026)

`static/js/studio.js` composes deck/docgen nodes under Studio and imports only `api.js`/`ui.js`. Maintenance stays optional and dynamically loaded inside Library.

`/api/docgen/generate-md` accepts `output_kind: report|deck`. `/api/docgen/markdown-preview` stages append-only proposals for vault-relative Markdown files; `/api/docgen/markdown-apply` requires explicit confirmation, matching source hash, vault/corpus identity, unexpired staging, path/vault locks, and the shared `studio_markdown` journal snapshot before writing. Ordinary preview tokens expire after 7 days (capped at 50 newest). LaTeX generation rechecks cancellation and path/vault ownership between figure copies and before scaffolding. The activity console retains 150 session events and 20 ended jobs.

## Index admission, extraction manifests and run inspection (24 October 2026)

`rag/index_admission.py` owns shared read-only admission checked under the write lock before archive or writable index construction. `core/extraction_cache.py` distinguishes whole-file legacy caches, contiguous complete ranges, partial ranges, and missing caches. `extraction_manifest` is validated before index-store admission; approved binary sources are hash-bound and deferred sources populate stale-sweep protections. `core/online_runs.py` reads immutable versioned records and checkpoints with a 16-entry receipt-signature summary cache.

## Confirmed online OCR and vision index runs (23 October 2026)

`api/routes/online_ocr.py` binds previewed PDFs and note-referenced images to source hashes (up to 500 sources / 1,000 pages or images). Completed source-bound results stage before cache promotion with immutable `landed-*.json` markers; an incomplete source blocks index update. Explicit full rebuilds require `full_rebuild` + `confirm_full_rebuild` and archive the existing index first. Interrupted runs resume via `resume_run_id` + `retry_failed=true` until `promotion-started.json` is written.

## Online provider request construction (2026-10-07 checkout)

`GET /api/models` (`core/llm/model_listing.py`) keeps curated IDs first and caches live IDs for 5 minutes. OpenRouter requests with tools or explicit reasoning set `provider.require_parameters=true`. Direct Gemini uses `generateContent`/`streamGenerateContent`; thought signatures and tool response IDs travel through `core/llm/tool_schema.py`. See [core/llm/CLAUDE.md](core/llm/CLAUDE.md).

## PDF management, reconciliation and pending review (September 2026)

- **PDF management (`accession/pdf_management.py`):** Owns reviewed entry cleanup, recoverable physical removal/restore (`BASE_DIR/audit/pdf_recovery/<op_id>/`), and retained link/trash outcomes. No direct Zotero-storage deletion or permanent purge exists. Annotation transfer is abandoned; duplicates with native annotations are retained unless explicitly acknowledged.
- **Reconciliation (`accession/reconciliation.py`):** Tracks database/library/item identities independent of citation keys, snapshot availability, and unkeyed attachment dispatch barriers. Web-library item creation uses `accession/web_snapshot.py::observe` (Total-Results + stable `Last-Modified-Version` across `items` and `items/trash`).
- **Pending review (`accession/review.py`, `api/routes/pending.py`):** Per-file decisions in `BASE_DIR/audit/pdf_reviews.sqlite3` (FULL synchronous writes, revision checks). Stored file identities compare via `accession/review.py::same_identity` (ignoring volatile macOS `dev` across reboots). `tagging/attachment_intents.py` reserves Zotero item keys under `scheme: 2` (`(ino, birth, sha256)`). `tagging/local_api.py` owns Zotero 10 local authorization (credentials in memory only, bound to the discovered server/database ID).

## Current Workflows

- **Single Paper (`Read`):** Upload one PDF, extract text in an isolated subprocess, and stream a tunable summary (single-pass or map-reduce). Details in [services/README.md](services/README.md).
- **Obsidian Vault (`Ask`):** Index `.md`, `.pdf`, and note-referenced images; query via hybrid dense + BM25 retrieval, RRF fusion, cross-encoder reranking, and optional ReAct agent mode. Details in [rag/CLAUDE.md](rag/CLAUDE.md) and [core/agent/CLAUDE.md](core/agent/CLAUDE.md).
- **Library Audit & Daily Workflow (`Library`):** Manual reconciliation of the Obsidian vault against Zotero, local PDFs, and Finder tags, plus the staged daily update workflow. Details in [audit/CLAUDE.md](audit/CLAUDE.md), [tagging/README.md](tagging/README.md), and [LIBRARY_WORKFLOWS.md](LIBRARY_WORKFLOWS.md).
- **Biblio Tagging (`Library → Tagging`):** Propose and apply `_tags.md`-validated tags across Obsidian note frontmatter, macOS Finder xattrs, and Zotero items with full SQLite-journalled Restore. Details in [tagging/README.md](tagging/README.md).
- **Deck & Article Generators (`Create`):** Generate and augment vault-grounded Beamer decks, LaTeX articles, and vault Markdown notes. Details in [deckgen/README.md](deckgen/README.md) and [docgen/README.md](docgen/README.md).
- **Plain Chat:** RAG-free multi-turn chat with server-side local text/vision attachment extraction. Details in [services/README.md](services/README.md).

## Module Ownership

- **`app.py` / `launch.py`:** Flask application factory, blueprint registration, `FEEDBACK_FILE`/`CONFIG_FILE` patch attributes, and PyWebView native window lifecycle (`text_select=True`, `--seed-models`).
- **`core/driver.py` + `cli/main.py` + `mcp_server/server.py`:** HTTP-first headless CLI (`cherqain` / `papermind`) and stdio JSON-RPC MCP server. Resolves the live server via `CHATEKLD_API_URL` → `BASE_DIR/server.json` (`core/server_info.py`) → `:5000`. All write commands require the running server and explicit `--confirm`. Endpoint and response projection coverage are ratcheted by `tests/test_driver_contracts.py`, `tests/test_driver_replay.py`, and `tests/test_driver.py`. See [CLI_GUIDE.md](CLI_GUIDE.md).
- **`api/` (`routes/`, `security.py`, `validators.py`, `bounded.py`, `sse.py`, `audit_context.py`):** Loopback/Origin guards, request coercion helpers, bounded daemon single-flight runner, shared 12-worker SSE queue skeleton (`run_sse_worker`, `_SSEOpGuard`), and shared mutation context lease. Full contracts in [api/README.md](api/README.md).
- **`core/` (`config.py`, `constants.py`, `database.py`, `paths.py`, `utils.py`, `providers/`, `llm/`, `agent/`):** Stat-cached `config.json` with versioned migrations, `resolve_under_root` realpath containment, `ReaderWriterLock`, `RagOperationLock`, atomic writers (`write_text_atomic`, `write_bytes_atomic`), `log_vault_write`, local embedding providers, unified online/local chat adapters, and the ReAct agent loop. Full contracts in [core/README.md](core/README.md), [core/llm/CLAUDE.md](core/llm/CLAUDE.md), and [core/agent/CLAUDE.md](core/agent/CLAUDE.md).
- **`services/` (`pdf_service.py`, `vision.py`, `attachment_service.py`, `local_model.py`):** Subprocess PDF extraction, `VisionManager`/`GLMOCRManager` singletons, and Plain Chat attachment extraction. Full contracts in [services/README.md](services/README.md).
- **`rag/` (`vault.py`, `build.py`, `sidecars.py`, `storage_io.py`, `engine.py`, `summarizer.py`, `lancedb_store.py`, `disk_docstore.py`, `paper_index.py`, `thesaurus.py`):** Incremental vault indexing, LanceDB/SimpleVectorStore persistence, SQLite disk docstore, BM25/wikilink sidecars, paper-label join (`BASE_DIR/rag/paper_index/<vault_key>.json`), and hybrid retrieval pipeline. Full contracts in [rag/CLAUDE.md](rag/CLAUDE.md).
- **`audit/` (`config.py`, `core/`, `engine/`, `manager.py`, `serialize.py`, `search.py`, `scan.py`):** Vendored read-only Library Audit engine (depends only on `audit/config.py`), 8-rung PDF↔bib bridge ladder (Zotero `linked_file` is rung 0), cross-report accent-folded search corpus, and decision sidecars (`mapping.json`, `dup_decisions.json`, `pdf_wanted.json`). Full contracts in [audit/CLAUDE.md](audit/CLAUDE.md).
- **`tagging/`, `accession/`, `notesync/`, `zotcreate/`, `vault_history/`:** Controlled-vocabulary tag writers and SQLite restore journal, keyless bibliographic lookup (`Crossref`, `Europe PMC`, `arXiv`, `HAL`, `theses.fr`), PDF authentication and duplicate comparison, two-way and Zotero-authoritative note sync (`POST /api/notesync/mirror`), two-stage Zotero item creation, and whole-note/rename vault history journals. Full contracts in [tagging/README.md](tagging/README.md).
- **`deckgen/` & `docgen/`:** App-independent LaTeX Beamer and article/Markdown generation substrates (`deckgen/latex.py`), citation post-pass (`rewrite_source_tags`), per-section checkpoint/resume, integrity review (`screen_repair` / `screen_article_repair`), and bounded `latexmk -norc -no-shell-escape` compile-fix. Full contracts in [deckgen/README.md](deckgen/README.md) and [docgen/README.md](docgen/README.md).
- **`widget/` (`snapshot.py`, `textclean.py`):** Pure date-seeded desktop-widget snapshot builder (`BASE_DIR/widget.json`) and LaTeX-to-display text cleaner for SwiftBar/Übersicht companions.
- **`maintenance/` & `cleanup/` (private-only, stripped from public artifacts):** In-process vault maintenance steps (`pdfnames`, `links`, `attachments`, `library`, `readmes`, `batchtags`, `latex`, `pathdrift`, `latexrefs`, `tagcheck`), strict biblio PDF renamer (moves Zotero `linked_file` pointers, `pdf:` frontmatter links, and `mapping.json` curations in one journalled op), and the phase-driven JSONL annual library cleanup harness (`00`–`06`, `new-pdfs`).

## Provider Rules

- `provider` selects `ollama`, `lm_studio`, `openai`, `anthropic`, `google`, `deepseek`, `zai`, or `openrouter`. Online providers are chat-only (plus explicitly confirmed online OCR/vision index runs); embeddings always resolve locally via `core.config.resolve_embed_provider`.
- API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `DEEPSEEK_API_KEY`, `ZAI_API_KEY`/`ZHIPU_API_KEY`, `OPENROUTER_API_KEY`, `ZOTERO_API_KEY`, `TAVILY_API_KEY`) are read from the environment at call time, never persisted to `config.json` or logs, and redacted by `core.llm.redact.redact`.
- Per-provider model keys (`llm` for local, `<provider>_model` for online) persist independently. Fallback (`fallback_provider` + `fallback_on`) occurs only on transient errors before the first streamed token. Full adapter contracts live in [core/llm/CLAUDE.md](core/llm/CLAUDE.md) and [core/README.md](core/README.md).

## Vault Rules

- Indexes `.md`, `.pdf` (folder-driven), and note-referenced images (`vault_image_exts`). Routine updates verify vault identity, embedding identity, and store integrity before mutating; incompatible stores require an explicit `force_fresh` rebuild (which archives the prior index).
- Updates stream incrementally with checkpoints every 500 inserts and ≥10 min, followed by a batched cancel-aware stale sweep (`_STALE_DELETE_BATCH` = 500 on LanceDB) and atomic BM25/wikilink sidecar swaps. `indexed_materials.json` is written before `obsidian_meta.json`.
- Query-time overrides on `/api/obsidian/chat` (`top_k`, `similarity_cutoff`, `prompt_mode`, `temperature`, `system_prompt`, `hybrid_enabled`, `reranker_enabled`, `mmr_enabled`, `query_expansion`, `wikilink_expansion`, `thesaurus_expansion`, `primer_enabled`) are reindex-free. Full retrieval mechanics and override tables live in [rag/CLAUDE.md](rag/CLAUDE.md).

## Agent Mode (Vault Chat)

- Opt-in ReAct loop (`vault_agent_enabled` / `agent_enabled`, `vault_agent_max_iterations` 1–12, default 6; `agent_wall_clock_s` default 300 s) over `vault_search`, `vault_read_note`, `vault_list_materials`, and triple-gated `web_search` (`vault_web_search_enabled` + online provider + `TAVILY_API_KEY`).
- Tool names must match `^[A-Za-z_][A-Za-z0-9_-]{0,63}$`. Every tool output is truncated and wrapped in a per-call nonced `<untrusted_content_...>` tag. Older turns in `tool_history` are stubbed past 70,000 chars while keeping ≥2 verbatim turns and preserving paired `tool_use`/`tool_result` IDs and thought signatures. Full details in [core/agent/CLAUDE.md](core/agent/CLAUDE.md).

## Single Paper & Library Audit Rules

- **Single Paper:** Resolves generation knobs (`body → paper_* config → clamp default`), extracts PDFs in a subprocess (1,000-page ranges up to `PDF_MAX_PAGES` = 20,000), and switches from single-pass to map-reduce (up to 64 parts with daemon heartbeat and cancel polling) when text exceeds the provider's usable window. Details in [services/README.md](services/README.md).
- **Library Audit:** Strictly manual (`POST /api/audit/scan`). External stores are read-only during scans (`mode=ro` WAL-aware Zotero SQLite copy). Writable surfaces are limited to `mapping.json`, `dup_decisions.json`, `pdf_wanted.json`, and confirm-gated `POST /api/audit/apply-tag-drift` (plus the journalled `tagging/`, `notesync/`, `zotlink/`, `zotrecord/`, and `zotcreate/` sub-views). Details in [audit/CLAUDE.md](audit/CLAUDE.md) and [tagging/README.md](tagging/README.md).
- **OCR & Vision Bounds:** Local `VisionManager` and `GLMOCRManager` calls are always bounded by `vision_timeout_s` (default 120 s, `max_retries=0`), `vision_max_tokens` (1536), `ocr_max_tokens` (4096), and `vision_failure_cooldown_s` (30 s). Details in [services/README.md](services/README.md).

## JS Module Hierarchy

- **Leaf & base:** `api.js` imports nothing. `ui.js` imports only `api.js`.
- **Feature modules:** `config.js`, `vault.js`, `summarizer.js`, `audit.js`, `deck.js`, `docgen.js`, `studio.js`, `help.js`, `settings.js`, `plainchat.js`, `prompts.js`, `history.js`, `tagging.js`, and `libraryRun.js` import only from `ui.js` and `api.js` — with the ratcheted exceptions that `vault.js` and `summarizer.js` also import `config.js`, and `audit.js` imports `pendingReview.js`, `pdfManagement.js`, `libraryRun.js`, and `pdfPreview.js` (one-way edges: `pendingReview.js` and `pdfManagement.js` → `pdfPreview.js`, `api.js`, `ui.js`; `pdfPreview.js` → `api.js`).
- **Root:** `app.js` imports from feature modules and probes `GET /api/maintenance/steps` before dynamically importing optional `maintenance.js`. Enforced by `tests/js/moduleHierarchy.test.js`.
- **Shared UI primitives (`ui.js`):** `sanitiseHtml` (DOMParser + control-char-stripped scheme blocklist), `confirmInline` (Cancel-first inline strip for destructive actions), `installTableControls` (node-preserving A→Z sort + accent-folded filter that reorders existing `<tr>` DOM nodes in place and hides non-matching ones without re-rendering, moves live `nextElementSibling` `data-row-attachment` panels with their parent row, and skips filter-hidden rows on `[select all]`; `audit.js::_mountPagedInventory` pages inventories >100 rows), `renderSideBySideDiff` (lossless word/line LCS diff with `−`/`+` glyphs and strike/underline), `statusLine` (unified tone/glyph/timestamp outcome bar), `installLinkInterceptor` (routes vault links to `POST /api/open`), and restrained gamification helpers (`dailyCounter`, `trendSeries`, `sparkline`, `milestoneOnce` — never nudging writes). `audit.js::ACTION_ROLES` centrally assigns `data-role` and glyphs across all inline verbs.

## API Routes

Full endpoint tables, request/response schemas, and status codes are documented in [api/README.md](api/README.md), [audit/CLAUDE.md](audit/CLAUDE.md), and [tagging/README.md](tagging/README.md). Route families:
- **System & Config:** `/api/status`, `/api/models`, `/api/vision-models`, `/api/health` (`_TtlSingleFlight` cached), `/api/config`, `/api/usage`, `/api/pricing`, `/api/prompts`, `/api/log`, `/api/log/tail`, `/api/open`, `/api/reset`.
- **Read & Plain Chat:** `/api/upload`, `/api/summarise`, `/api/report-types`, `/api/export-summary`, `/api/feedback`, `/api/chat-attachment`, `/api/plainchat`.
- **Ask & Online OCR:** `/api/obsidian/index` (synchronous `begin_index_run` admission; single-reader status drain), `/api/obsidian/status`, `/api/obsidian/readiness`, `/api/obsidian/materials`, `/api/obsidian/chat`, `/api/online-ocr/*`.
- **Create (Studio):** `/api/deck/*` (`load-template`, `generate`, `apply-repair`, `deck-sections`, `augment`, `apply-augment`, `repair-piecewise`, `compile-available`, `compile-preview`, `preview-pdf`, `compile-fix`) and `/api/docgen/*` (`generate`, `generate-md`, `markdown-preview`, `markdown-apply`, plus article augment/compile twins).
- **Library, Tagging & Zotero:** `/api/audit/*`, `/api/audit/library/*`, `/api/accession/*`, `/api/accession/pdfs/*`, `/api/accession/review/*`, `/api/tagging/*` (`vocab`, `inventory`, `propose`, `apply-*`, `remove-tags`, `harmonize`, `retire-tag`, `apply-pdf-links`, `pdf-links-status`, `manifest`, `restore`), `/api/notesync/*` (`status`, `sync-status`, `create`, `update`, `pull`, `push`, `merge`, `bootstrap`, `refresh-all`, `mirror`, `key-changes`, `rename-note`, `campaign/*`), `/api/zotlink/*` (`status`, `trash`, `repoint`, `retitle`, `attach`), `/api/zotrecord/*` (`retitle`, `merge`, `trash`), `/api/library/*` (`observe`, `analyse`, `apply`, `steps`), `/api/history/*` (`manifest`, `restore`), `/api/widget/*`, and optional `/api/maintenance/*`.

## Existing LanceDB index reads

Existing LanceDB indexes load their JSON docstore through `rag/disk_docstore.py` into a private temporary SQLite copy under `OBSIDIAN_CACHE_DIR`, parsed one record at a time to avoid full RAM materialization. See [rag/CLAUDE.md](rag/CLAUDE.md).

## SSE Contract

Streaming routes run through `api/sse.py::run_sse_worker` (capped at `_MAX_LIVE_SSE_WORKERS = 12`, returning HTTP `503` when full) with lock/gate release owned by `_SSEOpGuard` and terminal delivery guaranteed by `put_done_resilient`. The server advertises its silence budget (including the provider retry ladder and 90 s `Retry-After` ceiling) via `X-PaperMind-SSE-Silence-S`, and emitting an `{"error": "..."}` frame always precedes `data: [DONE]`. Full frame schemas and timeout chains live in [api/README.md](api/README.md#security-origin--body-bounds) and [core/README.md](core/README.md#concurrency-operation-locks--atomic-writes).

## Logging, Deletion & Vault-Write Auditing

- `chatekld.log` is written under `BASE_DIR` (`core.constants.LOG_FILE`).
- Every `shutil.rmtree(OBSIDIAN_INDEX_DIR)` call must first record `log_storage_deletion(reason)`.
- Every vault mutation or external Zotero/Finder write must call `log_vault_write(action, path, detail)` and journal its reversible operation in `vault_history/journal.py` or `tagging/journal.py` (except explicitly Git-backed `notesync` mirror/migrate sweeps and regenerable private `readmes` mirrors). Details in [core/README.md](core/README.md) and [tagging/README.md](tagging/README.md).

## Tests

Run focused checks from the project Python 3.12 virtual environment according to the changed behavior:

```bash
python scripts/check_static_scope.py compile
ruff check .
python scripts/check_static_scope.py bandit
python scripts/check_mypy_ratchet.py
python -m pytest -q
npm run test:js
PAPERMIND_PYTHON=python npm run test:browser
python scripts/check_dependency_audits.py
python scripts/check_docs.py
```

- **Static & Security Gates:** `scripts/check_static_scope.py::PRODUCTION_SCOPE` defines the authoritative compile/Bandit/mypy scope. `check_mypy_ratchet.py` enforces zero errors on remediated files and caps legacy findings in `.mypy-legacy-baseline.json` (never inflate budgets). Bandit uses `.bandit-baseline.json` (`docs/bandit-baseline-rationale.md`). `check_dependency_audits.py` runs `pip-audit --require-hashes` against `requirements-lock-{macos,linux}.txt` and `npm audit` against `.security-waivers.json`.
- **Hermeticity:** `conftest.py` sets a disposable `CHATEKLD_BASE_DIR` and strips live `ZOTERO_API_KEY`/`TAVILY_API_KEY` env vars; `test_launch.py` redirects `.env` discovery to a temp dir. Any test calling `index_vault` inside a temporary patch scope must call `from suite_helpers import join_post_index_daemons` before exiting the scope. Use canonical module imports, never private `app.py` aliases.
- **JS & Browser Suites:** `npm run test:js` runs Vitest + jsdom (`tests/js/*.test.js`). `npm run test:browser` boots the real loopback app in Playwright Chromium (`tests/browser/*.spec.js`) with enforced CSP (`bypassCSP` forbidden) and axe accessibility checks.

## Security Notes

- **Loopback & Origin:** Binds `127.0.0.1` for a single trusted OS user. Enforces `Host` allowlisting (`127.0.0.1` / `localhost` / `::1`), `Origin`/`Referer` loopback validation, and `X-Requested-With: ChatEKLD` on mutating/sensitive routes.
- **Body Caps & Containment:** Default `2 MiB` request limit (`501 MiB` on `POST /api/upload`, `65 MiB` on `POST /api/chat-attachment`). All filesystem paths validate through `core/paths.py::resolve_under_root` with realpath containment.
- **Renderer Hardening:** Enforced Content-Security-Policy, vendored runtime JS (`static/js/vendor/marked.min.js`), HTML sanitisation via `ui.js::sanitiseHtml`, authenticated `fetch` + managed `blob:` URLs for PDFs, and API-key redaction on all error/log surfaces. Full details in [api/README.md](api/README.md#security-origin--body-bounds).

## Build & Distribution

- **Single Entry Point (`install_and_build.sh`):** macOS `arm64`-only (`--target-arch arm64`), ad-hoc signed (`codesign --force --sign -`), producing `CHERQAiN_<date>.app` and `CHERQAiN_<date>.dmg` (staging the `.app`, `/Applications` symlink, `READ ME FIRST.txt`, `packaging/seed_models.command`, and `packaging/widgets/`).
- **External Runners & Per-Machine Caches:** Ollama / LM Studio remain external system services (`CHATEKLD_SKIP_PROVIDER_STARTUP=1` skips auto-start for isolated trials). Tiktoken, NLTK, and HuggingFace reranker caches live outside the bundle and are seeded offline-ready via `--seed-models` (`CHATEKLD_SKIP_MODEL_CACHE=1` skips cache prep during maintenance builds).
- **Public Source Artifact (`scripts/build_public_artifact.py`):** Builds a verified public source tree from a clean checkout, stripping private directories (`docs/`, `maintenance/`, `cleanup/`, `campaigns/`, `prototypes/`), omitting GitHub workflows, Dependabot configuration and retired ChatEKLD screenshots, replacing private Markdown links with explicit omission labels, and running full local verification including `scripts/check_docs.py`.

## Practical canonical PDF text profiles & extraction preparation (October 2026)

- **`core/practical_payload.py` & `core/pdf_text_profile.py`:** Define immutable, qualification-receipt-bound plain-text payloads and staged profile generations under `BASE_DIR/pdf_text_profiles/`. Offline `activate_profile` requires explicit confirmation and changes text selection only (never triggering re-indexing). `core.practical_payload.export_markdown_batch` (`scripts/export_canonical_markdown.py`) exports immutable Markdown snapshots outside indexed inputs.
- **`core/extraction_status.py` & `rag/embedding_identity.py`:** Propagate request-local `extraction_status` fields without altering cache/vector metadata; known partial PDF extractions are withheld from vault insertion and protected from stale sweeps. `embedding_identity.py` resolves query/load/prewarm backend and model from saved index metadata.
- **`core/upgrade_snapshot.py`:** Inactive offline recovery primitive that copies and SHA-256/SQLite-verifies a quiesced `BASE_DIR` under `server.json.lock` and restores only to a new empty directory.
- **Vault history & naming compatibility (`core/branding.py`):** Note Refactor is retired; historical `vault_history/` journals (`refactor_archive_dir`) remain restorable via `GET /api/history/manifest` and `POST /api/history/restore` (or **Settings → History and Restore**). Display branding is **CHERQAiN** (`CHERQAIN_*` env vars take precedence over legacy `CHATEKLD_*`/`PAPERMIND_*`), while `BASE_DIR`, `chatekld.log`, headers, and Zotero Desktop `appName` (`PaperMind`) remain stable.

## Bounded resource evidence and task labels (27 October 2026)

The shared OCR memory guard retains the first metric sample and the latest 255 (`sample_count` tracks total samples). Main task tabs use **Ask**, **Read**, **Library**, and **Create** while preserving existing tab IDs and routes.

### Observational Library evidence and settings navigation

`GET /api/obsidian/readiness` (`core.library_readiness`) reads only bounded (`≤8 MiB`) `obsidian_meta.json` and `indexed_materials.json` files once per mount or explicit refresh, checking pre/post file signatures without loading models or walking the corpus. Sidebar Settings links the index-tools and audit-settings dialogs, with `ui.js` managing stacked modal focus and inert background layers.
