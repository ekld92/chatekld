# Changelog

This file tracks the current refactor line. Older entries were removed because they described deleted integrations and stale endpoints.

## 2026-06-18 (Library Audit: precompute unmapped-PDF annotations during the scan)

Fixes the "Unread PDFs" / "Read PDFs Missing Zotero" tabs hanging on "Loading…": both reports iterate `inv.bridge.unmapped_pdfs` and previously read every unmapped PDF's annotations **lazily on the report GET** (thousands of synchronous `pikepdf` opens on the request thread, recomputed on every tab visit, with no progress and no cancel). The work now happens once, during the scan, in parallel.

### Changed

- **`audit/engine/inventory.py`** — `build_inventory` now precomputes annotation counts for the bridge's *unmapped* PDFs into a new `Inventory.unmapped_annotations: dict[Path, AnnotationsResult]`, gated by the existing `count_annotations` flag and skipped on cancel. Reads run through `_read_annotations_parallel` (a `ThreadPoolExecutor` capped at `_ANNOTATION_MAX_WORKERS = min(8, cpu_count)`; pikepdf/qpdf releases the GIL so threads scale across cores). A new optional `progress_fn` emits `Reading annotations for N unmapped PDFs…` plus a `…done/total` tick every 500 files.
- **`audit/manager.py`** — passes `progress_fn=self._emit` so the annotation phase is visible in the `/api/audit/status` feed (and the Cancel button now interrupts it between files).
- **`api/routes/audit.py`** — `unread_unzoterod` / `read_unzoterod` reports are served with `annotations=inv.unmapped_annotations`; the report `find()` functions already fall back to a lazy `read_annotations` for any path missing from the cache, so a `count_annotations=False` run or a cancelled scan degrades to the old per-request read rather than breaking. No report dataclass, serializer, API-payload, or JS change.

### Tests

- `test_audit.py::TestUnmappedAnnotationPrecompute` — `build_inventory` populates `unmapped_annotations` when `count_annotations=True` and leaves it empty when `False`.
- `test_audit.py::TestUnzoterodReportsUseCache` — both reports serve from the cache without touching disk (patched `read_annotations` raises if called).
- `test_audit.py::TestReadAnnotationsParallel` — the parallel reader covers full read, empty input, and immediate cancel (no hang).

## 2026-06-17 (Unified LLM Settings window + configurable timeouts)

Consolidates every LLM/RAG **parameter** (not prompts/instructions) into one **LLM Settings** modal grouped by function (Global / Vault Chat / Single Paper / Deck), surfaces a whole tier of knobs that previously had no UI (timeout/retry/token plumbing, fallback policy, reranker device, vector backend, prewarm), and makes the agent wall-clock cap and the local-provider HTTP timeout user-configurable. The inline per-tab parameter panels are removed; content/instruction inputs (system prompts, topic, audience, language, document type, template) stay on their tabs.

### Added

- **`static/js/settings.js`** — owns the `#settings-modal` (imports only `api.js`). Populates the settings-owned controls from `/api/config` on init and debounce-saves changes. Deliberately does **not** touch the vault knobs / OCR-Vision selects / provider-model selectors that also live in the modal — those are still operated **by element id** by their original owners (`vault.js`, `config.js`); the markup was relocated, not the logic.
- **New config keys** (`core/config.py`): per-function generation defaults `paper_*` (`paper_temperature` / `paper_num_ctx` / `paper_max_tokens` / `paper_top_p` / `paper_repeat_penalty`) and `deck_*` (`deck_temperature` / `deck_max_sections` / `deck_agent_max_iterations`); `agent_wall_clock_s` (default 300); `local_request_timeout_s` (default 0 = leave each path's SDK default).
- **`api/routes/config.py::_validate_llm_config_keys`** — a per-key coerce/clamp map (reusing `api/validators.py`) over every numeric/enum/bool LLM knob the Settings window writes. Out-of-range/malformed values are **dropped** (prior value survives), so a crafted body or a hand-edited `config.json` round-tripped through the UI cannot store a pathological value. This is the first validation those previously-UI-less keys ever had.
- **Configurable agent wall-clock cap** (`agent_wall_clock_s`) and **local request timeout** (`local_request_timeout_s`), both exposed in the Settings window's Global group. The local timeout threads through `core.providers.base.local_request_timeout()` into the Ollama client + `get_llm`, and the LM Studio OpenAI client + LLM (chat/generation only — embeddings are intentionally excluded; see Hardening).

### Changed

- **`templates/index.html`** — inline LLM-parameter accordions removed from the Obsidian, Single Paper and Deck tabs; the sidebar OCR/Vision section relocated into the modal. All controls keep their original ids, so `vault.js` / `summarizer.js` / `deck.js` / `config.js` operate them unchanged. Sidebar keeps the provider / model / embedding selectors and a new **⚙ LLM Settings** button.
- **`api/routes/paper.py` / `deck.py`** — generation knobs now resolve **request body → persisted `paper_*` / `deck_*` config → hard-coded default**, so the removed panels lose nothing (the Settings window writes the config the routes fall back to).
- **Timeout chain** (`api/routes/vault.py`, `deck.py`, `static/js/vault.js`) — the agent deadline, the SSE consumer stall guard, and the vault.js fetch-abort all derive from `agent_wall_clock_s` instead of three hard-coded `300`s, so raising the cap propagates outward. The consumer/abort are floored at `_SINGLE_SHOT_FLOOR_S = 300` so lowering the cap to bound agent turns can't starve a slow single-shot first token; the vault.js abort is computed **live at send time** so a Settings change applies without a reload.
- **`core/providers/ollama.py`** — generation routes through a `(host, timeout)`-cached `ollama.Client` (was the module-level singleton `ollama.chat`) so the configured timeout reaches httpx without churning a new connection pool per call.

### Hardening (post-implementation self-review)

- **Live frontend abort.** `_CHAT_TIMEOUT_MS` is no longer cached at init (which would ignore a mid-session cap change); `_chatAbortMs()` reads the live control at send time.
- **Single-shot stall floor.** The shared SSE consumer never drops below 300 s + margin even when the agent cap is lowered, protecting the non-agent RAG path.
- **Defensive cap parse.** `agent_wall_clock_s` is read with `coerce_int_in_range`, so a hand-edited non-numeric/negative value can't crash the route or yield an instant-past deadline.
- **`fallback_on` consistency.** An all-invalid non-empty list now drops (preserve prior) like every other validated key; an explicit `[]` stays a valid "disable all".
- **Embedding scope documented.** `local_request_timeout_s` covers chat/generation only — `OllamaEmbedding` has no timeout hook and bounding indexing batches would cause spurious failures rather than recovery.
- **Tests.** `smoke_test.py::test_03c` covers the new defaults + clamp/drop; `test_vault_regressions.py::test_ollama_client_threads_timeout_and_is_cached` covers the positive timeout path + the client cache.

## 2026-06-17 (Deck Generator — in-app deckgen window + custom Beamer templates)

Promotes the standalone `deckgen` CLI into a fourth app window (**Deck Generator**, beside Single Paper / Obsidian Agent / Library Audit) and teaches it to target the user's own LaTeX suite — reusing a custom Beamer template's preamble + macros (`\citefoot`, `\commonlogo`) and bibliography instead of a hard-coded generic preamble. Emit-only: it scaffolds a compile-ready `<slug>/` folder you build with `make`.

### Added

- **`deckgen/template.py`** — template awareness (all pure stdlib, unit-testable without a server): `split_template` (preamble / opening title+outline scaffold / closing tail; drops the template's example sections), `scan_macros` (follows local `\usepackage{...}` into sibling `.sty` files to surface custom macros; ignores `@`-internal and commented-out lines), `resolve_bib` (parses `\addbibresource{...}` → `key → author/year/title`), `relevant_bib_keys` / `bib_candidates_block` (bounded per-section candidate set), `find_suite_root`, `strip_comments`.
- **`deckgen/scaffold.py`** — `scaffold_deck` writes `<out_dir>/<slug>/<slug>.tex` + a 2-line `Makefile` (`include ../common/latex-build.mk`) via its own atomic writer; rejects unsafe slugs and refuses to clobber an existing deck without `overwrite`.
- **`deckgen/inprocess.py`** — `InProcessChatRunner`, a `requests`-free, `ChatEKLDClient`-compatible runner that drives `core.agent.run_agent_loop` over `rag.vault.obsidian_manager` directly (mirrors `vault.py::_run_agent`) so the in-app path never loops back over HTTP.
- **`deckgen/result.py`** — `ChatResult` extracted to a no-third-party-imports module shared by the HTTP client and the in-process runner.
- **`api/routes/deck.py`** (`deck_bp`) — `/api/deck/load-template`, `/api/deck/generate` (SSE), `/api/deck/native-pick-file`, `/api/deck/native-pick-folder`. Path validators reject traversal/system roots; generate reuses the `obsidian/chat` SSE contract plus a terminal `{"deck": {...}}` frame.
- **Deck Generator tab** — `templates/index.html` panel + `static/js/deck.js` (imports only `ui.js`+`api.js`; DOM built with `textContent`/`createElement`). Includes an **editable preamble box** (load a template, tweak it before generating — the macro/bib scan runs on the edited text) and a **dedicated instructions textarea**.
- **CLI flags** `--template` / `--out-dir` / `--citations` / `--no-citations` on `python -m deckgen` for the same house-style template mode.

### Changed

- **`deckgen/assemble.py`** — `assemble_with_template` injects sanitized sections between the template's title/outline scaffold and `\end{document}`; `validate` gains a `generated_tex` span (so the trusted preamble isn't flagged) and a `known_bib_keys` guard that warns about `\citefoot`/`\cite` keys absent from the `.bib` (an invented citation). `extract_cite_keys` added.
- **`deckgen/prompts.py` / `sections.py`** — per-section prompt now advertises the template's custom macros and a relevance-bounded `\citefoot` candidate list (`cite_mode="bib"`), staying within the 4000-char system-prompt cap; plain-prose `(source: note.md)` remains the fallback.
- **`deckgen/client.py` / `sections.py`** decoupled from `requests` at import time (the core no longer pulls `requests`), so the app can import the deckgen core without that dependency.
- **`app.py`** registers `deck_bp`.

### Hardening (post-implementation self-review)

- **Comment-aware template parsing.** `split_template` searches a length-preserving comment-masked copy (`mask_comments`) for the `\begin{document}` / `\section` / `\appendix` / references boundaries, then slices the original — so a *fully commented-out* references frame (as in the house `presentation.tex`) is no longer pulled into the closing tail. Previously that stripped the `%` off `\begin{frame}` while leaving `% \end{frame}` commented → an unclosed frame → a **non-compiling deck** for the exact template a user would pick.
- **`validate` is comment-blind no more.** It strips comments before counting frames / scanning macros, so a commented `% \end{frame}` no longer masks an unbalanced deck and a commented `\input` / `\cite` no longer raises a false warning.
- **Temperature override fixed.** `InProcessChatRunner` writes the per-turn override to `vault_chat_temperature` (the key the agent loop reads), not a no-op `temperature` key.
- **xparse arg-spec parsing.** `\NewDocumentCommand` specs are read with a brace-balanced reader (defaults like `O{red}` no longer inflate the displayed arity).
- **Path hardening.** `\usepackage`/`\addbibresource`/`\input` resolution rejects absolute paths (only the relative `../common/...` house style is followed). The SSE route sets `cancel` in a `finally` so a client disconnect can't leave the worker blocked on a full queue (thread leak); the frontend surfaces any non-200 instead of hanging on "Generating…".

### Tests

- `deckgen/tests/test_template.py` (29): split/scan/bib/scaffold/assemble + prompt wiring + the cite-key guard + the regression tests above (commented references frame, comment-aware validate, absolute-path rejection, xparse arity), all server-free. `test_deck.py` (7, hermetic, repo root): in-process runner accumulation + event passthrough + temperature-key routing, route path validators, and the 400/403 guards. Full suite green (474 passed, 1 skipped).

## 2026-06-16 (Perf/memory batch 4 — LanceDB binary vector store, opt-in)

Fourth batch of `PERF_MEMORY_PLAN.md` (Fix E). Replaces LlamaIndex's JSON `SimpleVectorStore` with an optional binary **LanceDB** backend that removes the dominant resident-RAM cost (embeddings as float32 on disk instead of Python `list[float]`) and most of the GIL-held cold-start parse. **Opt-in** — the default backend stays `simple`, so fresh builds are byte-identical until you choose otherwise; an existing vault migrates in place with **no re-embedding**.

### Added

- **`scripts/migrate_vector_store.py`** — one-time, offline migration. Streams `embedding_dict` from `default__vector_store.json` with `ijson` (~100 MB peak RSS on a multi-GB index), joins each vector to its `docstore.json` node by id, and bulk-loads LanceDB. The embedding model is never constructed → **no re-embed**. Archives the legacy JSON to `…json.bak`, records `"vector_backend": "lancedb"` in `obsidian_meta.json`, verifies row-count parity, and refuses to run while the app is open (`--yes` to skip the prompt). Idempotent.
- **`rag/lancedb_store.py`** — `NormalizingLanceDBVectorStore`, a self-contained parity layer so neither `rag/engine.py` nor the embed model need backend branches: it **unit-normalizes** embeddings on insert and the query vector on search (LanceDB defaults to L2; for unit vectors L2 ordering == cosine ordering, so ranking is bit-for-bit identical to `SimpleVectorStore`), and **JSON-stringifies** list/dict metadata (`attachments`) on a per-node copy because LanceDB — like Chroma — rejects non-scalar metadata; the docstore keeps the original list. Plus `lancedb_available`, `is_lancedb_store`, `lancedb_table_count`, `make_lancedb_vector_store`.
- **`vault_vector_backend` config knob** (`simple` | `lancedb`, default `simple`). Governs only **fresh** builds; an existing index's backend is authoritative from `obsidian_meta.json` (`vector_backend`; a missing key ⇒ legacy `simple`). Resolves back to `simple` if lancedb is not importable.

### Changed

- **`rag/vault.py` is backend-aware.** A single `_build_storage_context` / `_build_index_for_backend` helper routes all four index-build/load sites (fresh build, incremental load, lazy-load, prewarm); `store_nodes_override=True` on the lancedb path keeps the docstore populated so BM25 and the document-hash skip-check keep working. The checkpoint promotes only `docstore.json` / `index_store.json` for lancedb (the vector data is durable-on-insert in the binary `lancedb/` dir, not part of the temp-promote dance); `_validate_persisted_index_files` and `_index_dir_has_vector_data` verify the table instead of `default__vector_store.json` on that backend.
- **Crash-drift recovery.** Because the binary store is durable-on-insert while the docstore persists at Batch-1b's `max(500 chunks, 10 min)` cadence, a hard crash can leave the table *ahead* of the docstore. Two O(1) counts at incremental load detect this; only on a mismatch does that run switch to delete-before-insert (reusing the existing `delete_ref_doc` path) so resumed chunks replace orphan rows instead of duplicating. Fresh builds and the `simple` backend pay nothing.
- **MMR works on the binary backend.** External stores silently ignore `vector_store_query_mode="mmr"`, so `rag/engine.py` now applies MMR client-side (`_ClientSideMMRRetriever`) over an over-fetched candidate pool using the nodes' stored embeddings when the index is LanceDB-backed; `SimpleVectorStore` keeps LlamaIndex's native MMR path unchanged.
- **`requirements.txt`**: `lancedb>=0.21.1,<1.0`, `llama-index-vector-stores-lancedb>=0.4.1,<0.6`, `ijson>=3.3.0,<4.0` (verified: lancedb 0.33.0, integration 0.5.0, core 0.14.22 — the earlier draft pins `<0.3.0` / `lancedb<0.10` silently downgraded core to 0.11.x). `install_and_build.sh` collects the lancedb/lance/pyarrow native libraries.

### Fixed

- **Two latent test-isolation bugs** surfaced by the first tests that build a *real* `VectorStoreIndex` in-suite: `conftest.py` and `test_concurrency.py` both evicted `llama_index` from `sys.modules` between/within test files, creating a second class identity so `isinstance`/pydantic-dataclass checks (`resolve_embed_model`, `NodeWithScore`, retrieval events) failed across the boundary. Nothing stubs `llama_index`, so it is no longer evicted.

### Tests

- New `test_lancedb_migration.py` (12): store-layer normalization + metadata shim + `is_lancedb_store`, migration count-parity/`.bak`/meta/idempotency, **retrieval ranking parity** vs SimpleVectorStore, fresh lancedb build + **no-re-embed** on reindex, crash-drift delete-before-insert (and the duplicate it guards against), and client-side MMR relevance/diversity + engine routing. Full suite green.

## 2026-06-10 (Perf/memory batch 3 — reranker device knob, prewarm gate, thread pool)

Third batch of `PERF_MEMORY_PLAN.md` (fixes 1, D, G). New `vault_*` keys flow through the existing `/api/config` passthrough — no route or UI changes; both knobs take effect on the next launch. No reindex.

### Changed

- **`vault_reranker_device` config knob** (`auto` | `cpu` | `mps`, default `auto`). Corrected premise vs. the original plan: with no device argument, llama-index's `infer_torch_device()` already picks **MPS** on Apple Silicon — the reranker was never CPU-bound, so `auto` (= construct without a device argument) is byte-identical to the old behaviour and the knob's real value is the **`cpu` escape hatch** that returns the MPS allocator's ~150–400 MB to the unified pool on a pressured 16 GB machine. Robustness added alongside: the cache and sticky-failure keys fold in the device mode (changing the knob retries a failed load), non-CPU modes run a one-pair warm-up `predict` at load (Metal failures usually surface on the first forward pass, where the engine's postprocessor walk would otherwise error every chat), and any non-CPU failure retries on CPU for the session instead of disabling reranking.
- **`vault_prewarm_enabled` config knob** (default `True`). Gated inside `ObsidianVaultManager.prewarm()` (unit-testable, independent of the caller): when off, prewarm reports terminal `skipped` before any disk access; `vault.js` already treats that as banner-hidden/Send-enabled and the first chat lazy-loads along the existing path. Honest caveat: defers the multi-GB footprint, does not remove it.
- **Waitress pool `threads=16` → `32`** (`launch.py`). Slots are parked for the full duration of SSE generations (≤ 300 s) and upload extraction joins (≤ 600 s) while 1 Hz status polling continues — cheap insurance against pool exhaustion presenting as a frontend hang. The plan's earlier `channel_timeout` idea stays withdrawn: waitress' `maintenance()` only reaps channels with no in-flight request.

### Tests

- New `TestVaultRerankerDeviceKnob` (mode normalisation, auto-omits-device, cpu pass-through, MPS-failure → CPU fallback that does not stick, warm-up-failure → CPU retry, device-change resets sticky failure) and a prewarm-disabled gate test that proves the skip happens before any disk access.

## 2026-06-10 (Perf/memory batch 2 — BM25 sidecar, mmap reload)

Second batch of `PERF_MEMORY_PLAN.md` (Fix B). `rag/vault.py` + a `bm25s` pin; no reindex — the sidecar is a derived view of the docstore.

### Changed

- **The built BM25 retriever is persisted to `obsidian_storage/bm25_index/` and mmap-loaded on later launches.** Previously every process built BM25 from scratch on launch/first-chat — minutes of re-tokenising on a large vault — and the resulting retriever held a second full copy of all vault text (`BM25Retriever.__init__` copies every node into corpus dicts). Now a fresh build persists the bm25s index + corpus (`_persist_bm25_sidecar`, skipped while an indexing run is active), and the next cold start mmap-loads it (`_load_bm25_sidecar`): score arrays memory-mapped, corpus served lazily from JSONL. Steady-state RAM drops by roughly the vault's text size; prewarm's `building_bm25` stage becomes a fast open.
- **Staleness discipline:** the sidecar carries `sidecar_meta.json` (doc count + the index meta's `indexed_at`); a mismatch on either, or any load error, deletes the sidecar and rebuilds from the docstore — the count check alone could accept a same-size-different-content sidecar left by a crash between a checkpoint's persist and its cache invalidation. `_invalidate_retrieval_caches()` now also rmtrees the sidecar (mid-run checkpoints, final persist, `cleanup()`), and the meta file is written last so a torn persist self-invalidates. `/api/reset` and the index-version archive cover the sidecar automatically (it lives inside the index dir); `_persist_index_checkpoint` never touches it.
- **`bm25s` is now pinned** (`>=0.3.9,<0.4`): the retriever package leaves it unbounded, and the sidecar is a serialized format.

### Tests

- The mocked BM25 cache tests now run against a per-test `OBSIDIAN_INDEX_DIR`; new coverage: a real build → persist → mmap-load round trip asserting identical retrieval with `from_defaults` forbidden, stale-doc-count rebuild + re-persist, no-persist-while-indexing, and sidecar removal on invalidation.

## 2026-06-10 (Perf/memory batch 1 — indexing-path RAM & checkpoint cadence)

First batch of `PERF_MEMORY_PLAN.md` (16 GB Apple Silicon audit). All changes are inside `rag/vault.py`; no reindex required.

### Changed

- **Streaming indexer no longer pins every chunk for the whole run** (plan Fix A). `_index_documents_streaming` accumulates the manifest as `{raw_source: chunk_count}` instead of a `list[LlamaDocument]` — the list held a duplicate copy of all chunk text plus per-chunk pydantic overhead until the run ended (~100–400 MB peak on a textbook vault, dominant on incremental re-runs where most chunks are "skipped"). `_write_index_manifest` now takes the counts dict and resolves each unique source once; manifest JSON and `obsidian_meta.json` output are byte-identical. Sourceless documents land in a `""` bucket so `has_vector_data` / `inserted_this_run` / the empty-vault diagnostic keep their exact pre-change semantics.
- **Mid-run checkpoints are time-gated** (plan Fix F). New class attr `_PERSIST_MIN_INTERVAL_S = 600` beside `_PERSIST_EVERY = 500`: a checkpoint now requires *both* 500 pending inserts *and* 10 minutes since the previous attempt. Each checkpoint re-serialises and re-validates the entire store with the GIL held while chat is blocked on the rw write lock — on a fast embedder the old count-only cadence spent a growing share of the run inside O(N) dumps. Crash re-work bound becomes max(500 chunks, ~10 min); the don't-spin reset applies to both gates on checkpoint failure. Patch the interval to 0 in tests for count-only behaviour.
- **GC hygiene after indexing and reset** (plan Fix C, downgraded-but-free): `gc.collect()` in `index_vault`'s `finally` and at the end of `cleanup()` breaks LlamaIndex/parser reference cycles promptly. Hygiene only — CPython arena behaviour is unchanged.

### Tests

- The three streaming-indexer assertions moved from doc-id lists to `{source: count}` dict equality (strictly stronger); new `test_mid_run_checkpoint_gated_by_count_and_min_interval` pins the dual gate in both directions.

## 2026-06-10 (Big-PDF range splitting)

### Changed

- **Vault indexing: large PDFs (> 1000 pages) become one document per 1000-page range** (`rag/vault.py::_load_pdf_range_documents`) instead of a single concatenated text. Peak indexing RAM is now bounded by ~1000 pages regardless of file size; each range is cached immediately (`{sha256}-pSSSSS-EEEEE.txt`), so a cancel keeps completed ranges and a resumed run continues from the last cached one. The page cap rose from 5 000 (skip-with-warning) to the new shared `PDF_MAX_PAGES` = 20 000 — previously skipped textbooks are picked up incrementally by the next index run; nothing already indexed is touched. Range documents salt their chunk hash with `page_start` so ranges of one file can never collide on a doc_id; single-document files keep the original unsalted hash (no reindex). Legacy whole-file caches of big PDFs are grandfathered as single documents — delete the cache file to opt a PDF into per-range documents. `read_note` stitches per-range caches back together for agent reads.
- **Single Paper: uploads over 1000 pages no longer hard-fail.** The extraction worker (`services/pdf_service.py::_extract_all_pages`) mirrors the vault's range loop and concatenates, up to `PDF_MAX_PAGES`; oversized files get a clean error instead of the extractor's internal "Page range too large".

### Fixed

- Stale docstring in `pdf_extractor.py` pointed at a non-existent page-range chunker in `summarizer.py`; it now names the two real callers.

## 2026-06-10 (Atomic writes + hot-path I/O caching)

### Fixed

- **Five remaining non-atomic file writes now use temp-sibling + `os.replace`.** Most critical: `audit/engine/bridge.py::save_mapping` — `mapping.json` is the audit subsystem's only writable file and holds hand-curated PDF→citation-key matches a rescan cannot regenerate; a crash mid-write used to truncate it. Also converted: the PDF-extraction worker's result hand-off (a SIGKILL after the 600 s timeout escalation could leave the parent a torn JSON, which now also maps to a clean `RuntimeError` instead of a raw `JSONDecodeError` via the new `_read_extract_result`), `/api/export-summary`'s write to `~/Downloads`, the `.last_deletion_log` crash-forensics marker, and the Ollama PID file. New shared helper: `core/utils.py::write_text_atomic` (text twin of `rag/vault.py::_write_json_atomic`).
- **`save_feedback` no longer stalls the request thread up to 2 s.** The flock retry loop (40 × `LOCK_NB` + 50 ms sleeps, after which it appended anyway) is now a single non-blocking attempt with the same append-anyway outcome — `_feedback_lock` already serialises writers in this single-process app.

### Changed

- **`load_config()` is stat-cached.** Keyed by `(path, st_size, st_mtime_ns)` — the `UsageTracker._disk_records` pattern — so the 4-5 reads per vault-chat request (26 call sites app-wide) cost one `stat()` plus a defensive deepcopy instead of an open+parse each. `save_config()` invalidates explicitly; external rewrites are caught by the key change.
- **`/api/obsidian/status` polls no longer parse `obsidian_meta.json` three times each.** `get_status`, `is_partial_index`, and `get_index_warning` (plus the docstore-manifest fallback) share a stat-keyed `_read_index_meta()` cache on `ObsidianVaultManager`, invalidated eagerly by `_write_index_meta`.

## 2026-06-10 (Offline rendering, config-clobber fix, hermetic tests)

### Fixed

- **Chat rendering no longer depends on a CDN.** `marked.js` (v15.0.12, MIT) is vendored at `static/js/vendor/marked.min.js` and loaded locally; previously it came from jsdelivr, and any CDN/network hiccup inside the PyWebView renderer surfaced as `Error: Can't find variable: marked` — losing the answer the LLM had already produced. `vault.js` additionally falls back to plain-text rendering if `marked` is somehow still unavailable, so a renderer problem can never cost the user the answer.
- **Online model selection no longer clobbers the local one.** `POST /api/config` routes the UI's generic `llm` field into the active online provider's per-provider key (`openai_model` / `anthropic_model` / `google_model`) and now *removes* `llm` from the payload. Previously the online model name was also persisted into `llm`, silently overwriting the saved Ollama/LM Studio selection (observed in a real config as `provider=ollama` + `llm=claude-sonnet-4-6`, which Ollama 404s on). Pinned by `smoke_test.py::test_03b`.
- **Test suite is hermetic.** `core/constants.py::_get_base_dir()` honours a `CHATEKLD_BASE_DIR` env override, and the root `conftest.py` points it at a per-session temp dir before any app import. Tests no longer read or **write** the user's live config / DB / feedback files (smoke_test's config POSTs used to rewrite the real saved model; chat-route tests used to flip onto the agent path whenever the live config had `vault_agent_enabled: true`).
- **Four broken tests in `test_vault_regressions.py` repaired** (assert-on-restored-method, JSON-escaped em dash in SSE body, and two clamp assertions that contradicted the documented `coerce_int_in_range` clamp contract). The "Known pre-existing test defects" section in CLAUDE.md is gone; the full suite passes.
- **Anthropic pricing corrected.** `claude-opus-4-7` / `claude-opus-4-5` were listed at $15/$75 per MTok; the Opus tier has been $5/$25 since Opus 4.5. Added `claude-opus-4-8`, `claude-opus-4-6`, and `claude-fable-5`; the Anthropic adapter's curated model list drops retired Claude 3.x IDs (they now 404) and adds the current generation.

### Changed

- **HF offline mode at launch.** `launch.py` sets `HF_HUB_OFFLINE=1` before app imports when the configured reranker model is already in the HuggingFace cache — eliminating the ~25 HEAD/GET requests sentence-transformers makes to huggingface.co on every reranker load. First-time downloads still work (env not set when the cache is empty); changing `vault_reranker_model` to an uncached model mid-session requires an app restart to download, and the reranker-failure warning says so.
- **Generic `/api/config` strips `audit_*` keys.** Library Audit settings must flow through `POST /api/audit/config`, which validates path traversal / absolute paths; the generic endpoint previously accepted them unvalidated.
- **Audit frontmatter warnings surfaced in the UI.** YAML parse failures in vault notes now appear in the Library Audit status feed (first 5 + count) instead of only in `chatekld.log`; notes are also parse-cached by mtime so the bridge and inventory passes no longer parse every Zotero note twice per scan (warnings re-surface on cache hits so a malformed note never silently looks fixed).
- **`/api/usage` no longer re-parses the whole JSONL per poll.** `UsageTracker` caches parsed disk records keyed by the log file's `(size, mtime_ns)`.
- **Agent-route retrieval defaults aligned.** `_run_agent`'s `VaultToolContext` fallbacks for `hybrid_enabled` / `reranker_enabled` now default `True`, matching the documented engine defaults (only reachable if a config key fails coercion).
- **`renderExclusions` XSS hardening.** Vault exclusion rows are built with `createElement`/`textContent` instead of interpolating folder names into `innerHTML`.
- **CLAUDE.md restructured for context cost.** The root file had grown to ~49 KB (~12K tokens loaded into every Claude Code session). It now holds only cross-cutting rules and invariants (~20 KB); deep implementation notes moved to subtree files that load on demand when working in those directories: `rag/CLAUDE.md` (indexing pipeline, locks, prewarm, retrieval mechanics), `core/llm/CLAUDE.md` (adapters, fallback, pricing, tool wire formats), `core/agent/CLAUDE.md` (agent loop), `audit/CLAUDE.md` (Library Audit internals + endpoint payloads). No content deleted — redistributed.

## 2026-06-08 (Audit-matrix fixes + query-time RAG improvements)

### Added

- **Query-time retrieval-quality knobs (no reindex).** Vault chat gained MMR diversity on the dense leg (`vault_mmr_enabled` + `vault_mmr_lambda`), multi-query expansion (`vault_query_expansion` + `vault_num_queries`, effective on local providers), a configurable reranker candidate pool (`vault_rerank_pool_multiplier` / `_floor` / `_ceiling`, with the ceiling also a live `rerank_pool_ceiling` body override), and a `concise` answer mode. All are per-request body overrides resolved by `_resolve_chat_params`, persisted to the matching `vault_*` config keys, exposed in the vault chat "Retrieval & Generation" UI, and reindex-free. Pinned by a `TestReindexInvariant` guardrail that fails if the embedding model, chunker params, chunk-ID scheme, or index version change.

### Changed

- **`vault_top_k` default raised 6 → 8** (the `_effective_top_k` autoscaler still caps small-context models).
- **Quota errors are now terminal.** Hard quota / billing exhaustion (OpenAI `insufficient_quota`, Anthropic "credit balance", billing strings) is detected by `core.llm.base.looks_like_quota` and mapped to `ErrorCategory.QUOTA` — excluded from `fallback_on`, non-retryable — so it is no longer treated as a transient `rate_limit` (a Gemini per-minute 429 stays retryable). Wires up the previously-dead enum.
- **Mid-stream fallback no longer duplicates answers.** Both online streaming sites (`rag.engine._OnlineStreamingResponse._stream`, `rag.summarizer._stream_online`) fall back only before the first token; a failure after ≥1 token re-raises (the route emits a structured SSE error) instead of re-streaming the whole answer from the fallback provider.
- **Agent loop edge cases.** `FinishReason.LENGTH` now emits a truncation notice instead of posing as a complete answer; `CONTENT_FILTER` ends the turn cleanly instead of triggering the RAG-fallback / capability nag. `online_max_retries` is now honoured on the agent path (`cfg` threaded into `resolve_chat_provider`).
- **Accounting / robustness.** Usage records carry a per-record `uid` so the in-memory ring and on-disk JSONL never double- or under-count; the OCR cache is checked before the failure cooldown so a cached page is always served; the Google adapter records `cachedContentTokenCount`; the API-key redaction pattern matches service-account / admin keys (`sk-svcacct-`, `sk-admin-`); a corrupt non-partial index recovers as `paused_scan` (surfacing the integrity error); `/api/summarise` caps `language`.

### Removed

- Verified-dead code: `MARKITDOWN_MIN_CHARS`, `DEFAULT_TOP_K`, `VAULT_ALLOWED_EXTS`, `core/llm/prompt.py::truncate_to_char_budget` and its unused `system_prompt` param, `Record.parent_tags`, `NoteInfo.wikilinks`, two unused function params (`inventory_summary(settings)`, `_classify_blocks(page_width)`), an unused `ImageOps` import, and the unused `pydantic-settings` dependency. Stale "slice 5/7" comments and a contradictory checkpoint-retry comment fixed; the `core/providers/__init__.py` `Provider` hint moved under `TYPE_CHECKING`.

### Notes

- All of the above is **reindex-free** — no change touches the embedding model, chunker, chunk-ID scheme, or `OBSIDIAN_INDEX_VERSION`.
- Most code-review findings were applied; a few "dead code" claims were corrected because the symbols had test/real readers (`is_held`, `naming.py`, `RAG_QA_PROMPT`, `Image`) and were kept. The items intentionally deferred (e.g. the vendored Zotero attachments removal) are tracked under **Known Issues / Deferred Work** in `CLAUDE.md`.

## 2026-05-25 (Library Audit tab — kb_harmonizer integration)

### Added

- **Library Audit tab.** New third top-level tab that reconciles the configured Obsidian vault against Zotero (SQLite snapshot + Better BibTeX `_master.bib`), local PDFs under the attachments tree, and macOS Finder tags. Strictly read-only against external stores; the only writable file is `BASE_DIR/audit/mapping.json` (manual PDF↔bib overrides). Six reports surfaced: Tag Drift, Unread PDFs, Zotero Queue, Read PDFs Missing Zotero, Bib Entries Without PDFs, Duplicate PDFs — plus a per-citation-key Inventory view.
- **Manual-only scan.** The audit runs only when the user clicks Run Scan. `audit_manager` is instantiated at module import time as `idle`; no path in `app.py:create_app()` ever calls `start_scan()`. `test_audit.py::TestFlaskAppBootDoesNotScan` pins this with a `mock.patch.object` spy.
- **`audit/` package.** Vendored from kb_harmonizer (`audit/core/` connectors for bib / obsidian / zotero / pdf_annotations / finder_tags / hashing / naming, `audit/engine/` for bridge + inventory + duplicates, plus the five report builders under `audit/engine/reports/`). The PySide6 UI subpackage was dropped. `audit/config.py` adapts papermind's `config.json` keys into the kb_harmonizer-shaped `Settings` dataclass, exposing every previously-hardcoded subpath as a configurable key. `audit/manager.py` runs the single background scan thread with cooperative cancel via `_stop_event`. `audit/serialize.py` converts engine dataclasses to JSON dicts. `audit/scan.py` is the developer CLI (`python -m audit --check ...`).
- **`/api/audit/*` blueprint.** Eight endpoints (`config` GET/POST, `status` GET, `scan` POST, `cancel` POST, `inventory` GET, `reports/<name>` GET, `mapping` POST, `reveal` POST) — all behind the same `X-Requested-With: ChatEKLD` gate as the existing routes. Path-traversal and absolute-path attempts in vault-relative subdir fields are rejected at the route layer. The macOS-only `reveal` endpoint bounds `open -R` / `open` / `zotero://` invocations to paths under the configured vault root or Zotero storage tree.
- **`/api/reset` integration.** Reset now also calls `audit_manager.request_cancel()` + `clear_results()` so an in-flight audit is signalled to abort and the cached inventory is dropped, matching the post-reset "no scan yet" empty state on the Library Audit tab.
- **8 new config keys.** `audit_attachments_subdir` (default `Z_attachments`), `audit_biblio_articles_subdir` (default `biblio_articles`), `audit_zotero_notes_subdir` (default `Z_Zotero_Notes`), `audit_master_bib_path` (default `presentations_slides_writings_teaching/_master.bib`), `audit_zotero_sqlite` (default `~/Zotero/zotero.sqlite`), `audit_zotero_storage` (default `~/Zotero/storage`), `audit_annotations_read_threshold` (default `5`), `audit_biblio_skip_prefix` (default `z_item`). Vault root is shared with the existing `obsidian_vault_path` so there is no second vault setting.
- **New requirements.** `pikepdf>=8.15,<10.0` (read-only PDF annotation counting), `ruamel.yaml>=0.18,<0.19` (Obsidian frontmatter parsing that preserves quoting), `bibtexparser>=1.4,<2.0` (Better BibTeX exports), `beautifulsoup4>=4.12,<5.0` (already transitive via markitdown — pinned explicitly so an upstream change does not silently break the Zotero child-note HTML stripper).
- **PyInstaller bundling.** Added `audit/` to `--add-data` and `pikepdf` / `ruamel.yaml` / `bibtexparser` / `bs4` to `--collect-all` in `install_and_build.sh` so the frozen .app bundles the new package and its C-extension dependencies.
- **Tests.** 86 ported kb_harmonizer unit tests under `tests/audit/` (bib parser, bridge filename / mapping helpers, hashing with cancellation, naming scorer, obsidian frontmatter / link parsing, PDF annotations error contract). 17 new audit-level tests in `test_audit.py` covering the Settings adapter, manager state machine, the create_app no-auto-scan regression, the auth gate on every `/api/audit/*` route, and config-validation rejection of path traversal / absolute paths / non-finite thresholds. Total: 154 tests passing across `tests/audit/`, `test_audit.py`, `test_validators.py`, `smoke_test.py`.

### Notes

- The kb_harmonizer engine is intentionally not invoked automatically anywhere — settings can be edited, the tab can be visited, the page can be reloaded, and the audit stays `idle` until Run Scan is clicked. The scan reads only from external stores; the only writable file (`mapping.json`) is touched exclusively by `POST /api/audit/mapping`.
- An empty `audit_biblio_skip_prefix` is now treated as "skip nothing". The vendored kb_harmonizer code used `str.startswith(prefix)`, which would skip every PDF when the prefix is empty; the engine and CLI now both guard against that.
- Finder tag reads remain macOS-only (`audit/core/finder_tags.py` short-circuits to an empty list on non-Darwin platforms). The `/api/audit/reveal` endpoint also returns 501 outside macOS. The rest of the audit subsystem works on any OS.

## 2026-05-24 (ReAct agent mode for vault chat)

### Added

- **Opt-in agent mode for vault chat.** New `#vault-agent-enabled` checkbox under *Retrieval & Generation* in the Obsidian tab routes `/api/obsidian/chat` through a ReAct loop that can call `vault.search` / `vault.read_note` / `vault.list_materials` across up to 6 iterations per turn (`vault_agent_max_iterations`, body-overridable 1–12). Default off, so non-agent chats are byte-for-byte identical to today. Works against all five chat providers — Anthropic, OpenAI, Google, Ollama (0.4+), and LM Studio — via native tool-use APIs. Adapter capability advertised through `LLMProvider.supports_tool_use()`; the configured local model's actual tool-call reliability is detected at runtime through a malformed-call counter.
- **Fail-closed RAG fallback.** Two consecutive iterations where the model produced no parseable tool call trigger an info event and a clean re-run of `stream_chat` against the original user message (no agent trace mixed in). Successful tool calls reset the streak.
- **Per-session capability warning.** After two consecutive turns each end in fallback, a one-shot info event suggests a tool-capable model (Qwen 2.5, Llama 3.1+, Mistral Nemo, or any online provider). Tracked via a module-level `AgentCapabilityState` singleton in the vault route; resets on any successful agent turn.
- **Live reasoning trace UI.** A collapsed `<details class="agent-trace">` block renders above the bot bubble, with one section per iteration showing thoughts, tool calls (monospace `name(args)` snippets), and tool results (400-char `<pre>` snippet with `error`/`truncated` tags when relevant). The final answer still streams into the bot bubble as today via `{"token": "..."}`.
- **Per-turn usage footer.** After a successful agent turn the route emits one more `info` event summarising iteration count + token totals (`Agent: 3 iterations · 1247 in / 312 out tokens`). Cost suffix appears only for online providers.
- **New SSE event types.** `{"iteration": N}`, `{"thought": "..."}`, `{"tool_call": {...}}`, `{"tool_result": {...}}` join the existing `token` / `info` / `error` / `[DONE]` frames. Tool results carry `is_error` and `truncated` flags.
- **Tool layer (`core/agent/`).** New `protocol.py` (typed `AgentEvent` subclasses), `tools.py` (`ToolRegistry` + tiny JSON-Schema validator + `wrap_untrusted` guard), `vault_tools.py` (the three concrete vault tools), `budget.py` (`UsageBudget` per-turn accumulator), and `loop.py` (`run_agent_loop` driver). The agent layer never reaches into the indexer's internals; tool runners only call public `ObsidianVaultManager` methods.
- **`ObsidianVaultManager.retrieve()` + `read_note()` helpers.** Extracted from the retrieval phase of `stream_chat` and from the indexer's path-safety logic respectively. `retrieve` holds `_index_mutation_lock` for the brief retrieval phase (matching `stream_chat`); `read_note` rejects path traversal, excluded dirs, and unsupported extensions, and serves PDFs from `_pdf_cache_file` with a bounded fresh-extract fallback (`EXTRACT_MAX_PAGES_PER_CALL` cap, no OCR).
- **Tool-use wire format helpers (`core/llm/tool_schema.py`).** `build_openai_messages` / `build_anthropic_messages` / `build_gemini_contents` translate provider-agnostic `LLMRequest.tools` + `tool_history` into each provider's native dialect; `parse_openai_tool_call` / `parse_anthropic_tool_use` / `parse_gemini_function_call` go the other way, returning `None` on malformed JSON so the agent loop can count the iteration as malformed instead of crashing. Gemini-specific sanitiser strips `default` / `additionalProperties` fields its function-calling endpoint rejects.

### Changed

- **`LLMRequest` gains tool-use fields.** New `tools: list[ToolSchema]`, `tool_choice: Optional[str]`, and `tool_history: list[ToolTurn]` fields all default to empty/None — existing callers that pass `tools=[]` are byte-identical to today. `LLMResponse` gains `tool_calls: list[ToolCall]` (also defaults to empty).
- **Local adapter (Ollama / LM Studio) gains a non-flatten tool branch.** The legacy `stream_chat` path still flattens `messages` to a single user prompt; the new `_generate_with_tools` branch carries structured `messages` + `tools` directly through `ollama.chat()` / LM Studio's OpenAI client so multi-turn tool conversations (assistant `tool_call` → user `tool_result` → assistant) are preserved.
- **`SimpleQueryEngine` refactor.** Extracted `_build_retrieval_pipeline`, `_retrieve_chunks`, `_nodes_to_chunks` helpers from `query()`. New public `retrieve(message) -> list[RetrievedChunk]` runs the same hybrid + rerank pipeline as `query()` but stops before the LLM. `_query_online` now goes through `_retrieve_chunks` for de-duplication.

### Notes

- Slice-by-slice landing for safe incremental review. Each slice is independently mergeable and the existing single-shot RAG path keeps working at every step. See commits `Agent mode slice 1` through `Agent mode slice 8`.
- The decision to issue final answers as a single `TokenEvent` (no streaming on the last iteration) saves one LLM round-trip per agent turn at the cost of the typewriter effect on the final answer. Revisitable if user feedback warrants it.

## 2026-05-22 (audit M4 + M6 + RAG UX)

### Added

- **Custom vault chat system prompt.** A new "System Prompt Override" textarea under *Retrieval & Generation → Fine-tuning* in the Obsidian tab lets the user prepend behavioural instructions to the selected Answer Mode template. The mode template's safety preamble, untrusted-context guard, and `{context_str}`/`{query_str}` placeholders remain app-controlled, so a typo cannot disable retrieval grounding. Capped at `SYSTEM_PROMPT_LIMIT` = 4000 chars (shared with the single-paper override). Persisted as `vault_chat_system_prompt`; per-request body wins, then config, then default empty. On the online path the prompt is routed through `LLMRequest.system_prompt` so providers use their native system field. Pure query-time parameter — no re-index when changed.
- **Copy buttons on vault chat user prompts and answers.** Wires the existing `copyToClipboard` helper and `.copy-btn` styling (both shipped but unused) into the vault chat flow. User messages get a Copy button on render; bot messages get one after the stream completes so the per-token `innerHTML` re-render does not clobber it. Bot copies use the raw markdown rather than the rendered HTML so paste-into-notes works. Keyboard-discoverable via `:focus-visible`.
- **Shared validator helpers (audit M6).** `api/validators.py` owns the coerce/first-valid mini-framework that used to live as private helpers in `api/routes/vault.py`. Adds `coerce_enum`, `coerce_regex`, `coerce_non_empty_string`, `coerce_string_max_len` alongside the existing `coerce_int_in_range` / `coerce_float_in_range` / `coerce_bool` / `first_valid` so `paper.py`, `config.py`, `status.py`, and `usage.py` can drop their ad-hoc `if not x` / inline regex / inline enum checks. `test_validators.py` pins the contracts.

### Changed

- **Vault loader is a generator (audit M4).** `ObsidianVaultManager._load_vault_documents` now yields one `LlamaDocument` at a time (MD docs first, sorted; then sorted referenced image descriptions; then PDFs in scan order) instead of pre-materialising every extracted PDF before chunking. Peak RAM is bounded by the largest single source document plus the chunker/indexer backlog rather than `Σ extracted_text` across the vault — a meaningful win on textbook-sized vaults where each PDF can contribute tens of MB of concatenated text. Chunk IDs are unchanged (within-document enumeration order is preserved). No re-index required. The PDF signature-cache write moved into a `try/finally` so it survives early generator close.
- **Empty-vault diagnostic moved to post-streaming.** With streaming, the "no indexable files" check fires when the streamer reports `added=skipped=failed=0` and the stop event is not set, rather than from a pre-chunking `if not raw_docs:`. Same user-visible behaviour; meta is not written as "done" with zero documents.
- **`SYSTEM_PROMPT_LIMIT` centralised in `core/constants.py`.** Single-paper and vault-chat overrides now share the same 4000-char cap rather than spelling it out inline at each call site.

### Removed

- The private `_coerce_bool` / `_coerce_int_in_range` / `_coerce_float_in_range` / `_first_valid` / `_MISSING` aliases in `api/routes/vault.py`. Tests that depended on them now import the public versions from `api.validators` (the resolver `_resolve_chat_params` in the vault route is unchanged and remains the canonical body→config→default precedence implementation).

### Notes

- The audit's "hidden precondition" on M4 (chunk-ID stability requires preserved within-document enumeration order) is now pinned by `test_chunk_ids_stable_across_runs` in `test_vault_regressions.py`. A future refactor that subtly reorders nodes will flip this assertion before it can ship a corrupt index.

## 2026-05-17 (checkpoint hardening + repair tooling)

### Fixed

- Vault index checkpoints now persist LlamaIndex storage into a sibling temporary directory, validate the resulting JSON files, and only then promote them into `obsidian_storage/`. This prevents a failed write from truncating the active multi-GB vector-store JSON.
- Vault chat now preflights persisted index files before lazy-loading them and surfaces a clearer checkpoint-corruption error instead of passing through a raw `JSONDecodeError`.
- The status payload now includes `integrity_error` when metadata claims vector data but checkpoint files are missing or have an incomplete JSON tail.
- `/api/obsidian/chat` streams stage `info` events while loading the saved index, building BM25, loading the reranker, and starting retrieval/model generation.
- Repair scripts were hardened: `repair_simple_vector_store.py` accepts compact and spaced SimpleVectorStore prefixes and can promote an existing `.repaired` candidate; `prune_storage_to_vector_store.py` has a dry-run mode, preserves manifest metadata when possible, and no longer rewrites `inserted_this_run`.
- PyInstaller collection now includes the BM25 packages (`llama_index.retrievers.bm25`, `bm25s`, and `Stemmer`).

### Added

- Regression coverage for corrupt checkpoint recovery, failed temp-checkpoint promotion, and a real BM25 exact-term retrieval smoke test.

## 2026-05-17 (external venv + BM25 dependency restore)

### Fixed

- Moved the project virtual environment out of the repository to `~/venvs/papermind2026`; `install_and_build.sh` now defaults to that path through `PAPERMIND_VENV_DIR` while remaining overrideable.
- Restored the BM25 dependency with `llama-index-retrievers-bm25>=0.7,<0.8`, which is compatible with the current `llama-index-core>=0.14.18,<0.15` pin. The earlier `0.5.x` range required `llama-index-core<0.13` and made `pip install -r requirements.txt` unsatisfiable.
- Added regression coverage for BM25 package availability, manager cache/rebuild behaviour, and RRF fusion wiring in `SimpleQueryEngine`.

### Notes

- No vault re-index is required for the BM25 restore. BM25 builds lazily in memory from the existing saved docstore on the first hybrid query and is invalidated when the docstore changes.

## 2026-05-14 (hybrid retrieval + cross-encoder reranking)

### Added

- Vault chat retrieval is now hybrid by default. A BM25 (lexical) retriever and the existing dense (cosine) retriever run in parallel; their result lists are fused with reciprocal-rank fusion via `QueryFusionRetriever(mode="reciprocal_rerank", num_queries=1)`. `num_queries=1` keeps it a thin RRF wrapper — no LLM-driven query rewriting, no extra round-trip to the local provider per chat.
- Cross-encoder reranking on top of hybrid retrieval. Default model `cross-encoder/ms-marco-MiniLM-L-6-v2` (~67 MB, ~80-200 ms / 30 candidates on Apple Silicon CPU). The reranker narrows a candidate pool (sized `min(max(top_k * 4, 20), 50)`) down to `top_k` chunks before they reach the LLM.
- Three new persisted config keys: `vault_hybrid_enabled` (default `true`), `vault_reranker_enabled` (default `true`), and `vault_reranker_model` (default `cross-encoder/ms-marco-MiniLM-L-6-v2`). The two boolean knobs are also accepted as per-request body fields (`hybrid_enabled`, `reranker_enabled`) on `/api/obsidian/chat` with the same body→config→engine-default precedence as the existing live controls. The model name is config-only — `/api/obsidian/chat` does not accept it as a body override so a malicious page cannot swap in arbitrary HuggingFace repos to download.
- `ObsidianVaultManager._get_bm25_retriever(top_k)` caches the BM25Retriever by docstore size and survives across chats. Invalidated explicitly by `_invalidate_retrieval_caches()` after every successful `idx.storage_context.persist(...)` (mid-run checkpoint and final persist) and inside `cleanup()`. Size-fingerprint same-size delete+insert races are tolerated as eventually-consistent — the indexer's invalidation resyncs.
- `ObsidianVaultManager._get_reranker(model_name, top_n)` lazy-loads the cross-encoder on first chat that needs it and caches the loaded model. A sticky `_reranker_failed` flag prevents re-attempting the same model name after a failed load; the user must change `vault_reranker_model` to retry. The reranker survives `cleanup()` / `/api/reset` so vault switches do not re-download weights.
- Clear-chat button in the vault chat panel. Server holds no chat history (each `/api/obsidian/chat` call is independent), so the button is a pure DOM operation — clears the chat container and the status line. Refuses while a query is in flight so an in-progress bot message is not orphaned mid-stream.

### Changed

- `SimilarityPostprocessor(similarity_cutoff=...)` is now attached only when the reranker is absent. Cross-encoder scores are on a different scale from dense cosine, so applying a 0.25 cosine cutoff after rerank would silently drop high-rerank, low-cosine chunks. Existing dense-only behaviour is preserved exactly when `hybrid_enabled` and `reranker_enabled` are both false (covered by the `test_similarity_cutoff_reaches_postprocessor` regression test).
- `_effective_top_k` autoscaling now applies to the post-rerank final chunk count, not the pre-rerank candidate pool. A small-context model is therefore not overwhelmed by reranker breadth — only the final, autoscaled chunks reach the LLM.
- Added optional dependencies `llama-index-retrievers-bm25` and `llama-index-postprocessor-sbert-rerank` + `sentence-transformers`. Both are import-guarded in `rag/engine.py`: `BM25Retriever` / `SentenceTransformerRerank` resolve to `None` when the package is absent, the manager treats `None` as "feature unavailable", and the chat falls back to the dense-only path with a one-shot warning.
- `install_and_build.sh` pre-downloads the cross-encoder weights into `~/.cache/huggingface/hub/` after `pip install` so first-chat latency does not include a model fetch. Includes `--collect-all sentence_transformers` in the PyInstaller invocation so the bundled `.app` ships the torch / sentence-transformers extension modules.

## 2026-05-13 (live vault chat controls)

### Added

- Vault chat now exposes four live query-time knobs in the Obsidian tab, all of which take effect on the next Send without any reindex. New collapsible "Retrieval & Generation" panel surfaces **Top K** (2-12) and **Answer Mode** (`Strict` / `Balanced` / `Exploratory`) by default, with a nested **Fine-tuning** section for **Similarity Cutoff** (0.0-0.7) and **Temperature** (0.0-2.0).
- `/api/obsidian/chat` accepts new optional body fields `top_k`, `similarity_cutoff`, `prompt_mode`, and `temperature`. Values are range-clamped server-side; an invalid `prompt_mode` is dropped (falls back to the persisted config) rather than rejected.
- Persisted config keys: `vault_top_k`, `vault_similarity_cutoff`, `vault_prompt_mode`, `vault_chat_temperature`. Restored on app launch via `applyVaultChatParams`; saved debounced (400 ms) when controls change. The request body remains authoritative — a save failure never changes what the next Send actually uses.
- Three `RAG_QA_PROMPT_*` templates in `rag/engine.py`. `strict` keeps the previous behaviour (refuse when context does not support the answer). `balanced` grounds claims in context but marks unsupported parts rather than refusing the whole question. `exploratory` permits cautious cross-excerpt synthesis with explicit hedging — useful when retrieval is thin but the user wants a starting point.
- `SimpleQueryEngine.top_k_explicit` flag bypasses the `_effective_top_k` autoscaling when the caller has chosen a value (so a user picking 12 with a 8k-context model still gets 12). Implicit callers (no flag) keep the historical autoscaling as a guard rail.
- 11 new unit tests in `test_vault_regressions.py::TestVaultChatLiveControls` cover prompt-mode dispatch, top_k autoscaling vs. explicit, cutoff plumbing, temperature forwarding, and route-level clamping / fallback / body-wins precedence.

## 2026-05-13

### Fixed

- Vault chat against an LM Studio model with a non-OpenAI-style name (e.g. `google/gemma-4-e4b`) no longer returns the spurious "No relevant content found in your vault for this query." message. LlamaIndex's upstream `is_chat_model()` helper recognises only OpenAI model name patterns and was routing every other model through the legacy `/v1/completions` endpoint, which LM Studio answers with a single empty content delta. `_LMStudioOpenAI.metadata` now forces `is_chat_model=True` so every LM Studio chat model goes through `/v1/chat/completions`. Retrieval was never broken — only the LLM call.
- Paused-during-scan indexing now recovers across an app restart even when no `docstore.json` was ever persisted. `get_status()` no longer requires both `obsidian_meta.json` and `docstore.json` to exist; it reads meta and trusts the `has_vector_data` field (with a dir-scan fallback). The recovered `phase` is mirrored into `_current_phase` so the status payload stops flickering back to `"idle"` on cold boot.
- Tightened the embed-mismatch warning. It now fires only when an actual vector store exists on disk with a different embedding model. A paused-scan state with `partial=True` but no vectors silently starts fresh instead of emitting a misleading "Resuming a partial index…" warning. The text was also reworded to "Existing index was built with embedding model …" since the warning is no longer scoped to resumes.

### Changed

- Renamed the `inserted_count` field in `obsidian_meta.json` to `inserted_this_run` to make clear it is a per-run counter, not a cumulative chunk total. Old meta files written with `inserted_count` are still readable; the field is informational and not load-bearing for resume.
- Vision and OCR prompts in `services/vision.py` and `core/constants.py` were tightened for scientific-document and scanned-page text extraction. Pure prompt-string changes; no public-contract or schema impact.

## 2026-05-12 (audit fixes)

### Fixed

- Single Paper OCR now honours the user's `ocr_provider` / `ocr_model`. The spawn subprocess in `services/pdf_service._extract_worker` reloads config and re-applies the configured provider and model on the child's `glm_ocr_manager` before extraction. Previously the subprocess used `DEFAULT_OCR_MODEL` regardless of UI selection.
- `/api/obsidian/cancel` no longer leaves `partial: True` on disk. The indexer's final meta write distinguishes cancel (clean stop, `partial=False`) from pause (`partial=True`), so a cold boot after cancel shows `done`/`idle` instead of a spurious Resume button.
- `/api/reset` waits for the indexing thread to finish before `shutil.rmtree`-ing the storage directory and calls `obsidian_manager.cleanup()` to drop the in-memory index. The previous flow could let a stray `storage_context.persist()` recreate the directory after the rmtree.
- Vault chat no longer blocks for the duration of a multi-hour indexing run. `ObsidianVaultManager.index_vault` holds the rw write lock only for the setup phase, the mid-run checkpoint callback, and the final persist; the streaming insert loop runs without it, and `self._index` is published immediately after setup.
- Vault chat producer no longer silently drops tokens on a slow consumer. `token_q.put` blocks (no 2 s timeout) and the SSE generator's outer `finally` sets the cancel event so the producer breaks out on client disconnect.
- SQLite connections opened via `get_db_connection` are now closed on exit. The helper is a `@contextmanager` that commits on success, rolls back on exception, and always calls `conn.close()`. `/api/reset` no longer uses a bare `sqlite3.connect` either.
- `/api/log` is rate-limited to 100 messages per rolling 60-second window per process.

### Changed

- `VisionManager.check_availability` / `GLMOCRManager.check_availability` are informational only. They never gate a call and never rewrite `self.model`. Whatever the user configured is the model that gets called; the probe just reports a best-effort match. `VisionManager` now uses the same exact + base-name match as `GLMOCRManager`.
- `_extract_image_description` frees the raw image bytes immediately after base64 encoding to lower the peak RAM transient on 20 MB images.
- New endpoint `DELETE /api/upload/<upload_id>`. The Single Paper "New Paper" button now deletes the prior upload row instead of leaving it in `uploads.db`.

### Frontend

- Vault chat HTML is sanitised through `sanitiseHtml` before `botMsg.innerHTML = …` so a retrieved chunk that contains `<script>` cannot execute in the renderer.
- Modals (`#excl-modal`, `#image-exts-modal`, `#pull-modal`, `#howto-modal`, `#reset-modal`) carry `role="dialog"`, `aria-modal="true"`, and `aria-labelledby`. `openModal` installs an Escape-to-close handler and a Tab/Shift-Tab focus trap; `closeModal` removes the handler and restores focus to the trigger.

## 2026-05-12 (later)

### Changed

- Vault image indexing is now configurable. The `vault_image_exts` config key holds the list of image extensions sent through the vision model for description and embedding. The list is edited from a new Image Extensions modal on the Obsidian tab.
- Image descriptions are cached under `obsidian_storage/image_cache/{vault_key}/{sha256}.txt` using the same atomic-write pattern as the PDF text cache. Unchanged images reuse the cached description on subsequent runs.
- Factored the cache atomic-write into `_atomic_write_text` so PDF and image caches share one implementation.
- Markdown chunks now carry an `attachments` metadata list. The chunker scans each note for Obsidian wikilinks and inline markdown links, resolves each target relative to the note's directory, and stores the resulting vault-relative paths. External URLs and anchor-only references are dropped. Existing chunks pick up the metadata as they are next re-indexed.
- On cold boot the Obsidian tab now queries `/api/obsidian/status` during initialisation. A paused on-disk index surfaces the Resume button immediately and shows the configured vault path without requiring the user to click Index Vault first.

### Fixed

- After an app restart the Resume button no longer requires the user to click Index Vault to discover that the prior run was partial.

## 2026-05-12

### Fixed

- Per-document insert tolerance and circuit breaker. A single transient embedding failure no longer kills a multi-hour indexing run; 20 consecutive failures aborts and preserves the partial index.
- Streaming chunker. Vault chunks are produced on demand rather than accumulated in a list, capping memory peak at one source document's worth of chunks instead of the entire vault's.
- Mid-run checkpointing. Vault indexing persists every 500 inserts so a crash bounds re-work to roughly that many chunks of work.
- Index state recovery on restart. The Obsidian tab now reflects the saved `partial`/complete status of an existing on-disk index instead of showing "not indexed".
- Pause now honored during the document-loading phase. The scan loop checks the stop event each file.
- Large PDFs (>1000 pages) extracted in 1000-page chunks; PDFs over 5000 pages skipped with a clear warning.
- Heartbeat refreshed between extraction chunks for textbook PDFs so the operation-lock TTL does not expire mid-file.
- Per-PDF size+mtime fingerprint cached in `pdf_signatures.json` so subsequent runs skip re-hashing unchanged files.
- PDF cache write failures emitted to the status feed (throttled) instead of debug-only.
- On `OBSIDIAN_INDEX_VERSION` mismatch the prior index dir is renamed to `obsidian_storage.bak.{ver}.{stamp}/` instead of `shutil.rmtree`-d.
- PDF subprocess uses an explicit `spawn` context and is no longer a daemon, so internal worker pools in PyMuPDF / MarkItDown are not blocked.
- Suppressed MuPDF C-level stderr noise (`could not parse color space`) — PyMuPDF handles these gracefully; surfacing them confused users.
- `OBSIDIAN_INDEX_VERSION` bumped to `obsidian-markdown-v3` to force a one-time clean reindex picking up the widened 16-hex chunk-ID space.

### Changed

- Removed dead code: `MarkItDownVisionShim` (never imported), `_obsidian_status_updates`/`_obsidian_status_lock` aliases in `app.py`, unused `_RagOperationLock` re-export, stale references in the `pdf_extractor.py` module docstring.
- Added `EXTRACT_MAX_PAGES_PER_CALL` as a module constant so vault chunking and the extractor share one source of truth.
- Test coverage: insert-failure tolerance and circuit breaker now have direct unit tests in `test_vault_regressions.py`.
- Pre-existing GLM-OCR retry test fixed to seed the availability TTL timestamp.

## 2026-05-11

### Fixed

- Wrapped uploaded PDF text and vault RAG context as untrusted source material to reduce prompt-injection risk.
- Reworked vault chat retrieval to use per-query LLM/embedding objects, a guarded QA prompt, and a similarity cutoff instead of mutating global LlamaIndex settings during retrieval.
- Streamed PDF uploads to temporary files with a hard byte limit before extraction.
- Moved PDF extraction for uploads into a subprocess so extraction timeouts can terminate the worker.
- Returned structured SSE error events for vault chat failures that happen after streaming starts.
- Validated Obsidian vault paths before saving and rejected broad roots and system directories.
- Replaced the single vault PDF extraction cache JSON file with per-PDF cache files under `obsidian_storage/pdf_cache/`.
- Raised the PyMuPDF minimum version to `1.25.2`.
- Removed orphaned dead code at the end of `pdf_extractor.py`.

### Changed

- Added upload resource-safety and concurrent upload regression tests.
- Documented the new RAG security boundaries, cache layout, and upload timeout behavior.

## 2026-05-10

### Fixed

- Recovered the Indexed Materials panel when an existing persisted index is missing `indexed_materials.json`.
- Retried GLM-OCR with a smaller aligned page image when a scanned PDF page exceeds the vision model context window.
- Restored `/api/summarise` SSE responses.
- Fixed Obsidian chat SSE output and frontend `[DONE]` handling.
- Fixed `/api/obsidian/cancel` lock release.
- Restored `/api/pull`, `/api/reset`, `/api/report-types`, and `/api/log`.
- Routed model listing, status checks, indexing, and chat through the active provider.
- Kept LM Studio model IDs unchanged.
- Kept Ollama model resolution inside the Ollama provider.
- Saved UI-selected chat and embedding models to config.
- Applied vault exclusions as vault-relative paths.
- Indexed `.md` and `.pdf` vault files.
- Kept `.docx` excluded by default.
- Counted skipped image files during indexing and exposed the count in status.
- Surfaced provider warnings and embedding mismatch warnings in the UI.

### Changed

- Restored structured Single Paper prompt presets and built-in document-type prompts.
- Re-exposed Single Paper controls for audience, focus question, temperature, context, max tokens, top-p, repeat penalty, and system prompt override.
- Added `.md` summary export alongside `.txt` export.
- Added an Obsidian indexed-material manifest and UI panel listing indexed notes/PDFs with chunk counts.
- Added separate OCR and vision provider/model selectors, including LM Studio-compatible image calls.
- Made Obsidian vault indexing incremental by skipping unchanged chunks, replacing changed chunks, and deleting stale chunks.
- Added a vault-scoped PDF extraction cache so repeat indexing can skip extraction and OCR for unchanged PDFs.
- Updated README, project notes, and project structure documentation.
- Added a smoke test for the `/api/summarise` SSE contract.
