# Changelog

This file tracks the current refactor line. Older entries were removed because they described deleted integrations and stale endpoints.

## 2026-06-23 (Indexing: backoff in the insert-failure breaker)

- **Transient embedding-backend blips no longer abort a long indexing run.** The consecutive-insert-failure circuit breaker (`rag/vault.py::_index_documents_streaming`, abort after `_MAX_CONSECUTIVE_FAILURES`=20) had no delay between attempts, so a backend that *instant-rejects* — observed as LM Studio returning HTTP 400 "Failed to decode batch!" in ~10 ms while it JIT-reloads the embed model under memory pressure — burned all 20 "retries" in ~0.2 s and killed a multi-hour run before the model could finish loading. The breaker is now a **wall-clock window**: each consecutive failure waits `min(_FAILURE_BACKOFF_BASE_S * 2**(streak-1), _FAILURE_BACKOFF_CAP_S)` (1.0 / 5.0 s → ~87 s across the 19 pre-abort sleeps) via the interruptible `self._stop_event.wait(delay)`, so a brief hiccup gets time to recover and a streak-resetting success cancels the backoff, while a truly-down backend still aborts in under a minute. The sleep holds no lock and runs off the chat path; a Cancel/Pause mid-sleep aborts promptly. Dropped blip chunks self-heal on the next incremental run (never inserted ⇒ re-yielded). New class attrs `_FAILURE_BACKOFF_BASE_S`/`_FAILURE_BACKOFF_CAP_S` (patchable to 0 in tests, like `_PERSIST_MIN_INTERVAL_S`).
- Tests: `test_vault_regressions.py::test_index_streaming_backoff_spaces_and_caps_consecutive_failures` (exponential-then-capped delays, no real sleep) and `...backoff_is_interruptible_by_cancel` (stop event mid-sleep aborts before the threshold); the two existing failure tests patch the backoff to 0.

## 2026-06-23 (Production hardening — disk, leaks, fallback, stall bounds)

Code-review follow-ups: bound an unbounded-disk-growth path, trim avoidable per-poll/per-call allocation under the 16 GB constraint, make a local backend going offline fail over instead of erroring, and stop a guard-less SSE route hanging on a wedged model.

### Resource management
- **Unbounded `.bak` index dirs pruned.** Every `OBSIDIAN_INDEX_VERSION` bump archives the whole prior index to a timestamped `obsidian_storage.bak.*` sibling and nothing ever deleted them — tens of GB could accumulate. `rag/vault.py::_prune_old_index_backups` keeps the newest 2 and routes each removal through `log_storage_deletion` (deletion-audit invariant). Called from `_archive_old_index_dir` after the rename.
- **Status-poll lancedb count cached.** `get_status()` is polled ~1 Hz and only needs `count > 0`, but each call opened a fresh `lancedb.connect()`. `_cached_lancedb_count` caches by the lancedb dir's `(st_size, st_mtime_ns)` with a 5 s TTL backstop; exact-count callers (crash-drift recovery, checkpoint validation) keep calling `lancedb_table_count` directly.
- **tiktoken encoder memoized** (`core/llm/usage.py`, `core/providers/lms.py`) via `functools.lru_cache` — was rebuilt per call (the LM Studio `_tokenizer` property potentially per chunk).
- **Ollama client-cache keyspace bounded** — `_ollama_client` rounds a non-None timeout to whole seconds (floored at 1 s so a sub-second value can't round to 0.0 and make httpx time out immediately) before keying, so the agent loop's near-continuous remaining-budget float no longer mints a fresh never-evicted `ollama.Client` per call.

### Graceful degradation / robustness
- **Local connection/timeout errors are now fallback-eligible.** `core/llm/adapters/local.py::_classify_local_error` maps a connection-refused/timeout from a stopped Ollama/LM Studio to `NETWORK`/`TIMEOUT` (retryable) instead of the default `UNKNOWN`, so a configured online `fallback_provider` actually takes over (matching the online adapters). Applied at all four transport catch sites.
- **`/api/summarise` stall bound.** That route streams synchronously with no consumer stall guard; at the default `local_request_timeout_s=0` a connected-but-wedged local model could hang it. The summariser now passes an explicit per-read floor (`PAPER_LOCAL_STALL_TIMEOUT_S=120` when the knob is unset) threaded through `OllamaProvider`/`LMStudioProvider.stream_chat(request_timeout=...)`.
- **Guarded local stream extraction** — the summariser reads chunk fields via `getattr` and skips empty/keep-alive frames instead of raising `IndexError`/`AttributeError` mid-stream.

### Logging / hygiene
- `err.message` redacted before logging at the three fallback-warning sites (`rag/summarizer.py`, `core/llm/chat.py`, `rag/engine.py`).
- Dropped two unused imports (`refactor/apply.py`, `refactor/archive.py`); commented two best-effort `except` swallows in `pdf_extractor.py`.

### Tests
- New `test_llm.py::TestLocalErrorClassification` (network/timeout/name-based/unknown/passthrough) and `test_vault_regressions.py::TestIndexBackupPrune` (keeps newest 2, audits each removal, leaves the live dir + unrelated siblings untouched).

## 2026-06-23 (Privacy / nomenclature scrub)

Portability + privacy pass ahead of sharing the app: remove maintainer-specific identifiers and the lingering old project name from shipped defaults, code, and docs so a fresh install on another Mac carries no personal data and consistently calls itself **ChatEKLD**.

### Changed — defaults now neutral
- `refactor_scope_subdir` and `_DEFAULT_SCOPE` default to `""` (no personal default folder name — an empty scope resolves to `None` → a clean 400, so the user must pick a folder). `audit_master_bib_path` / `audit/config.py::DEFAULT_MASTER_BIB_PATH` default to a neutral `_master.bib` (was a maintainer-specific sub-folder path). UI placeholders in `templates/index.html` genericised.

### Changed — nomenclature
- Renamed the build-script env var to `CHATEKLD_VENV_DIR`, keeping the legacy `PAPERMIND_VENV_DIR` as a deprecated fallback and `~/venvs/papermind2026` as the default path (existing venvs keep working). Replaced "papermind" → "ChatEKLD" in docstrings/instructions (`audit/`, tests, `project_structure.txt`, `README.md`, the `CLAUDE.md` deep-notes). `CHANGELOG` history left intact.

### Removed — personal identifiers
- Stripped 12 `file:///Users/<user>/…` absolute links from `PROMPTS.md` (now repo-relative). Genericised the maintainer's vault path, scope-folder name, vault-key hash, and study-domain description across `docs/project_note_refactor.md` and the `test_refactor.py` fixture (placeholders `<vault>` / `<scope>`, neutral fixture name `study_notes`); removed a contributor name from a `core/llm/model_listing.py` comment.

### Security / hardening
- `/api/log` now strips CR/LF and runs the message through `core.llm.redact.redact` before logging — closes a log-injection vector (forged audit lines) and restores the uniform "redact before logging" discipline.
- Dev-only `app.py:__main__` `app.run(...)` now pins `host="127.0.0.1"` explicitly (mirrors `launch.py`), and the README documents the un-sandboxed first-launch TCC consent prompt alongside the existing Gatekeeper/quarantine note.

## 2026-06-22 (Plain Chat — RAG-free multi-turn panel)

A sixth tab: a plain, multi-turn conversation with the globally-configured chat provider/model — **no vault retrieval, no agent loop, no tools**. The server is stateless; the browser owns the conversation and re-sends the (capped) history on every turn, so a chat is ephemeral (lost on reload).

### Added — backend
- `POST /api/plainchat` (`api/routes/plainchat.py`): an SSE route that streams `{info}` / `{token}` / `{error}` frames + the `[DONE]` sentinel. The queue + daemon-worker + consumer skeleton is lifted from `api/routes/vault.py::api_obsidian_chat` with **every** agent / iteration / tool branch stripped. `_validate_messages` (a) structurally validates the body (a list of `{role∈{user,assistant}, content:str}`; a malformed entry rejects the whole request rather than silently dropping a turn), (b) caps each message to 24 000 chars and the array to the last 20 turns, then (c) **normalizes for provider shape** — drops leading assistant turns and merges consecutive same-role turns — so strict-alternation providers (Anthropic/Gemini) accept a window sliced mid-exchange. Provider/model resolve from config (`resolve_chat_model`); `temperature`/`system_prompt` resolve **body → persisted `chat_*` config → hard default**, so a Settings change applies on the next send with no reload (the panel sends neither).
- `core/llm/chat.py::stream_chat_messages`: the unified local + online RAG-free streaming helper. Modelled on `rag/summarizer.py::_stream_online` but driven by a full `messages` array (online adapters send native message arrays; the local adapter flattens to a role-tagged prompt), so multi-turn works on every provider with no model-layer change. Falls back to `fallback_provider` **only before the first streamed token**; usage/cost tracking fires automatically inside the adapter.

### Added — frontend
- `static/js/plainchat.js` (`window.chatPlain` / `window.plainchatNew`, `ui.js`+`api.js`-only): keeps the `{role, content}` history array, sends only `{messages: history.slice(-20)}`, renders answers via vendored `marked` + `sanitiseHtml` with a `textContent` fallback, disables **Send** for the in-flight turn, renders a muted **non-recorded** bubble on an empty answer, and rolls back the un-answered user turn on error so a retry re-sends it exactly once. New **Plain Chat** tab + sidebar `chat_temperature` slider / `chat_system_prompt` textarea (persisted by `settings.js`, not `plainchat.js`).

### Safety / correctness
- On an empty-but-clean stream the route emits **no** synthetic token — a placeholder would be recorded into the client's history and re-sent as a fake assistant turn. The bounded worker queue (`maxsize=512`) applies back-pressure; the `cancel` Event lets a timed-out/disconnected consumer stop the worker; the consumer's `event_q.get` stall guard is the path's only time bound (plain chat has no agent wall-clock).

### Config / constants / tests
- New persisted keys `chat_temperature` (default `0.3`) and `chat_system_prompt` (default `"You are a helpful assistant."` — the **full** system prompt, ≤`SYSTEM_PROMPT_LIMIT`, no vault-grounding preamble). Validated/clamped in `api/routes/config.py::_validate_llm_config_keys` (`chat_temperature` 0–2, `chat_system_prompt` string-capped, empty kept).
- `SSE_SINGLE_SHOT_FLOOR_S` (300) / `SSE_STALL_MARGIN_S` (30) promoted to `core/constants.py` so the vault, deck, and plain-chat SSE routes share one stall model instead of cross-importing `vault.py` privates.
- Hermetic `test_plainchat.py` covers message validation/normalization, the body→config→default resolution, the empty-stream contract, and the stall/error frames.

## 2026-06-22 (Note Refactor — Phase 2: first vault writers — apply / archive / restore)

The app's **first feature that writes to the user's Obsidian vault**, scoped to one sub-folder and entirely opt-in (off by default; every action requires an explicit `confirm`). This supersedes the README's former "ChatEKLD never writes to your vault" banner (now: "writes only via Note Refactor's Apply/Archive"). Phase 1 previewing, re-extraction, and classification remain read-only.

### Added — bulk Apply (callout-only, WYSIWYG)
- `POST /api/refactor/apply` (`refactor/apply.py`): writes the **same** callout-only proposal the user previewed — the advisory `> [!extracted]` callout inlined beneath each described embed, with original embeds **kept**. Batch of approved notes, each applied independently (one failure never aborts the rest). Two guards protect every write: a **stale-diff** guard (the note's on-disk sha256 must equal the plan's `content_sha256`) and a **WYSIWYG** guard (the server-recomputed body's `proposed_sha256` must equal what the UI previewed). A note is only written when its bytes are a clean UTF-8 round-trip. The per-note transform is now the shared public `refactor.plan.analyze_note(...)`, so preview == apply by construction.

### Added — per-image Archive (move-out + thumbnail)
- `POST /api/refactor/archive` (`refactor/archive.py`): an explicit, per-image action (never coupled to the bulk apply). A **vault-wide reference-safety check** refuses any image embedded by another note (409 `{shared:true}`). Otherwise it materialises the original (iCloud-safe), writes a ≤384px **PNG thumbnail** into the excluded `<scope>/_thumbs/` folder (auto-added to `vault_exclude_dirs`), copies the full-res original into the recoverable archive dir (copy → verify → delete), and swaps that one embed to the thumbnail.

### Added — Restore + manifest
- `POST /api/refactor/restore` (`{op_id}` or `{all:true}`) reverses any apply/archive: note body from snapshot, archived original back to its vault path, thumbnail removed. `GET /api/refactor/manifest` lists the ops for the Restore UI. `refactor/journal.py` owns the per-vault `manifest.json` (restore journal), scope-lock (`assert_under`), and archive-dir resolution (rejects a dir inside the vault).

### Safety
- Atomic writes only (`core.utils.write_text_atomic` + new `write_bytes_atomic`). Every write/move is scope-locked to `<scope>` (+ `_thumbs/`) or the archive dir (outside the vault). Each write endpoint requires `confirm: true` and holds `obsidian_manager.try_acquire_lock` for the duration (**503 if indexing is in progress**), released in `finally`. Every mutation is audit-logged via new `core.utils.log_vault_write` (see CLAUDE.md *Logging, Deletion & Vault-Write Auditing*) and journalled for rollback/resume.

### Config / UI / tests
- New keys `refactor_archive_dir` (abs path or `""` = `BASE_DIR/refactor/archive/<vault_key>/`, must resolve outside the vault) and `refactor_thumb_max_side` (96–1024, default 384), validated/clamped in `api/routes/config.py`. New constants `REFACTOR_THUMBS_DIRNAME` / `DEFAULT_REFACTOR_THUMB_MAX_SIDE`.
- `static/js/refactor.js` gains per-note approve checkboxes + an "Apply approved" confirm modal, a per-image "Archive…" inline confirm, and a "Restore…" modal reading the manifest (`ui.js`+`api.js`-only, no `innerHTML` for user strings). The tab blurb now warns that Apply/Archive write to the vault.
- `test_refactor.py`: 22 new hermetic tests (callout apply + snapshot, stale/WYSIWYG guards, shared-image refusal, archive move + thumbnail + embed swap + `_thumbs` exclusion, restore of both, op-lock 503, scope-lock validator, thumbnail PNG, config validators, plus the self-audit cases below). Full suite green.

### Fixed (Phase 2 self-audit)
- **Restore is now atomic-or-nothing.** Reverting an archive of a note that was *edited after* archiving previously half-reverted — it could delete the thumbnail while the note still embedded it (broken embed) or clobber the user's later edits. `revert_archive_image` now runs all conflict checks up front and refuses the whole op (touching nothing) unless the note is still exactly what we wrote or already back at its pre-archive form.
- **Restore won't clobber a re-created file.** If a *different* file now occupies the archived image's original path, restore refuses rather than overwriting it (digest-compared; an identical file is treated as already-restored and the revert completes idempotently).
- **The move-safety reference check is now maximally conservative.** It previously skipped notes in the user's `vault_exclude_dirs` / `.trash`, so an image still embedded by an excluded-folder note could be wrongly judged "not shared" and moved — breaking that embed. The walk now scans every `.md` except `.git`/`.obsidian` (which structurally hold no user embeds).
- **Archive re-verifies the note immediately before its destructive write** (closing the window where the note changes during the thumbnail/copy work); on mismatch it rolls back the thumbnail + archive copy + the just-appended manifest op, leaving the vault untouched. The original-file `unlink` is now `assert_under`-guarded.
- **Restore persists the manifest per-op** (not once after the loop), so a crash mid-batch never leaves on-disk reverts unrecorded — matching `apply_notes`.

## 2026-06-22 (Note Refactor — read-only analyzer + central hub)

A new **Note Refactor** tab and `refactor/` package. It is **read-only with respect to the vault** (Phase 1 + Phase 1.5): it resolves an Obsidian sub-folder's image embeds, reuses the indexer's on-disk description cache (zero vision calls by default), flags broken links / frontmatter smells, and reports advisory cross-note dose discrepancies. The only persisted artifacts are under `BASE_DIR/obsidian_cache/` — never the vault. Apply/archive (the first real vault writes) remain Phase 2.

### Added — Phase 1 (read-only analyzer)
- `refactor/` package (`resolver` / `cache` / `hints` / `extract` / `hygiene` / `discrepancy` / `plan` / `result`) and `api/routes/refactor.py`: `POST /api/refactor/plan` (SSE: `info`/`error` + a `{"note": …}` frame per analyzed note + a terminal `{"refactor": …}` summary), `POST /api/refactor/extract-image` (the **only** vision-calling path — one user-chosen image at a time; serialized via `_VISION_LOCK`; caches `obsidian_cache/<sha256>.{table,redescribe}.txt`, never the indexer's base `<sha256>.txt`), and `GET /api/refactor/image` (read-only bytes, fetched as a blob so the X-Requested-With/origin check stays intact). `static/js/refactor.js` tab + `refactor_scope_subdir` / `refactor_extract_model` / `refactor_table_double_read` config keys. Hermetic `test_refactor.py`.

### Added — Phase 1.5 (central hub)
- **Folder picker.** `POST /api/refactor/native-pick-folder` → vault-relative scope via `_abs_to_scope` (rejects the vault root + anything outside the vault, like `_resolve_scope`); a `Browse…` button beside the scope input. Manual typing still works.
- **Single-note detail pane.** A sidebar/detail master-detail UI renders ORIGINAL vs PROPOSED markdown (vendored `marked` + `sanitiseHtml`, `textContent` fallback like `vault.js::_renderAnswer`) with a Rendered/Diff toggle. The `{"note"}` frame now carries the `original`/`proposed` bodies (~2× frame size, fine for the scoped sub-folder).
- **Classify + handwritten signal.** `extract-image` gains `mode="classify"` → one cheap vision pass labelling printed-table｜figure-diagram｜handwritten｜photo｜other (cached `<sha256>.classify.txt`), surfaced as a first-class "handwritten — can't OCR" badge.
- **Sticky ignore-list.** `GET`/`POST /api/refactor/ignore` over a rel-path-keyed, per-vault JSON sidecar at `obsidian_cache/refactor/<vault_key>/ignore_list.json` (**never the vault**). Ignored images grey out, drop from the not-extracted/likely-table counts, and get no inlined callout in the plan. The read-modify-write is serialized with a module lock so concurrent toggles under waitress's worker threads can't lose updates.

### Fixed (Phase 1.5 self-audit)
- `extract.classify` no longer caches a bogus `other` label when the model returns an empty (non-exception) reply — it surfaces an error and writes nothing, so a later plan run can't show a confident-but-wrong label (mirrors `redescribe` / the `NO_TABLE` skip).
- The detail pane disconnects the thumbnail `IntersectionObserver` on re-render so the previous note's detached `<img>` nodes aren't retained until the next run (cached blob URLs survive and are reused).

## 2026-06-22 (Indexing-pipeline audit fixes: LanceDB compaction, scanned-PDF coverage, embed-at-chat, lifecycle)

Five batches from a read-only audit of the vault indexing pipeline, landed before a full `embeddinggemma:300m` reindex on the `lancedb` backend.

### Fixed — LanceDB `_versions` O(n²) bloat (reindex blocker)
- The streaming indexer inserts one chunk per transaction; on LanceDB each insert is a new single-row fragment **and** a new version manifest that re-lists every fragment, so `_versions/` grew ~O(n²) (66k inserts measured at **209 GB** over <1 GB of real vectors — a full reindex would fill the disk). New `rag/lancedb_store.py::compact_lancedb_vector_store` runs `Table.optimize(cleanup_older_than=0)` on the live table (best-effort, never raises). It is called (a) from `_persist_index_checkpoint` (lancedb branch, under the locks both callers already hold) and (b) **every `_LANCEDB_COMPACT_EVERY` (2000) inserts** in `_index_documents_streaming`, under `_index_mutation_lock` only — independent of the ≥10-min JSON-checkpoint gate, which is too coarse to bound the interim spike on a fast embedder. `scripts/compact_lancedb.py` is a one-off reclaim for an already-bloated table (app closed). (Implements an earlier internal fix plan, plus the insert-count cadence + reclaim script that plan had deferred.)

### Fixed — scanned PDFs no longer silently lose ~90% of their content
- `_perform_ocr_fallback` capped OCR at `_OCR_MAX_PAGES` (100) **per call**, but the vault loader extracts in 1000-page ranges, so a scanned ≥100-page range only ever indexed its first 100 pages — and cached that partial as complete, so it never self-healed. `extract_structured_from_pdf` now takes `ocr_max_pages` (the vault loader passes the full range size = 1000) and `page_done_cb` (per-page op-lock heartbeat so a long scan can't expire the TTL); `ArticleSections` gained a `truncated` flag. The vault loader **warns and does not cache** a truncated range so it retries next run. The interactive upload path keeps the 100-page default (bounded by its own extraction timeout). `pdf_extractor.py`, `rag/vault.py`.

### Fixed — chat no longer mixes embedding spaces on a model switch
- Retrieval embedded the query with whatever `embed` config said, even when it differed from the model the index was built with — fusing two vector spaces and silently wrecking results (only an advisory UI banner guarded it). `stream_chat`/`retrieve` now resolve the **index's own** embed model from `obsidian_meta.json` (`_effective_embed_name`), warn once, and retrieve with it. The indexer already rebuilds on an embed change; this keeps chat correct until the user reindexes. `rag/vault.py`.

### Fixed — index lifecycle / robustness
- **Cancel-then-reindex race:** `cancel_indexing` force-releases the op lock *before* its background thread finishes the final persist, so a reindex started immediately after could archive/rebuild the index dir concurrently with the cancelled run's checkpoint. `POST /api/obsidian/index` now `wait_for_indexing(timeout=30)` first (mirrors the reset path). `api/routes/vault.py`.
- **No-recorded-embed index:** a `has_vector_data` index whose meta lacks an `embed` model is now rebuilt rather than silently extended (it can't be proven compatible). `rag/vault.py`.
- **Delete-before-insert gap:** a changed chunk whose re-insert fails *after* its old copy was deleted is now tracked and reported (`reinsert_failed`) instead of being hidden in the aggregate `failed` count; it self-heals on the next run.

### Tests
- `test_lancedb_migration.py`: compaction merges per-insert fragments + preserves rows, the streaming loop compacts at the insert cadence, `compact_lancedb_vector_store` is a safe no-op off lancedb, and a no-recorded-embed index rebuilds. `test_vault_regressions.py::TestPdfRangeSplitting`: truncated range yielded-but-not-cached, full-range `ocr_max_pages` passed (mock updated to mirror `ArticleSections.truncated`). `test_vault_regressions.py::TestChatEmbedMismatchGuard`. `smoke_test.py::test_30b` (index route 503 when a prior run is unfinished).

## 2026-06-21 (MD secondary cap: stop silently truncating long markdown sections)

### Fixed — long single-heading markdown sections are no longer truncated at embed time
- **`MarkdownNodeParser` splits `.md` only at heading boundaries**, with no size ceiling, so a long single-heading section produced one oversized chunk that exceeded the embedding model's token limit (nomic-embed-text / EmbeddingGemma ~2048) and was **silently truncated** — its tail never embedded and was unretrievable. A read-only scan of the live vault found **52 oversized sections across 39 notes**, 9 already exceeding 2048 tokens today (e.g. a 4,581-token section, >50% clipped). `_chunk_raw_documents` now runs a **conditional secondary `SentenceSplitter` pass** on the MD branch that sub-splits **only** sections over `MD_MAX_CHUNK_TOKENS` (1024, `core/constants.py`). `rag/vault.py`.
- **Oversize is detected by splitter output cardinality**, and under-cap sections pass through as the **original node object** → identical `i`+text → byte-identical chunk id ⇒ **zero re-embed churn** for the ~97% of notes with no oversized section. A note *with* an oversized section re-chunks that section onward (the `i` shift); the stale-doc_id sweep self-heals it — **no `OBSIDIAN_INDEX_VERSION` bump**. A provably-safe byte-length fast path skips tokenizing tiny sections (a cl100k token is always ≥ 1 UTF-8 byte). The secondary split is `try/except`-guarded so a pathological section degrades to un-split rather than aborting the (generator-based) run.
- Attachment extraction now runs over the **final** post-split node list (each sub-chunk gets only its own links), with the `attachments` list attached **after** the split so it can't inflate the split decision (the `_tags.md` metadata-budget case). PDF chunking is untouched.

### Notes
- `MD_MAX_CHUNK_TOKENS` is a **pinned constant**, not a config knob: it changes chunk ids, so editing it is a reindex — the same contract as the 512-token PDF chunk size. 1024 leaves ~2x headroom under the 2048 hard limit for tiktoken-vs-SentencePiece divergence. Best landed alongside an embedding-model switch so the (small) reindex is paid once.

### Tests
- `test_vault_regressions.py::TestMdSecondaryCap`: under-cap byte-identity (legacy hash), oversized split ≤ cap with unique doc_ids + propagated `header_path`, tail-only attachments, multibyte-under-cap byte-identity. `test_chunker_params_pinned` extended to pin the secondary splitter line.

## 2026-06-21 (Vision/OCR call bounds: a stuck image can no longer stall indexing)

### Fixed — indexing-time vision/OCR calls are now always bounded
- **A runaway or stuck local vision model no longer freezes a multi-hour indexing run.** The image-description and scanned-PDF OCR calls (`services/vision.py`) had no timeout, no retry cap, and no token cap, so they inherited the OpenAI/LM Studio SDK default of a 600 s timeout × 2 retries = **up to 30 minutes per stuck image**, blocking the whole streaming indexer (observed against `lm_studio` + `gemma-4-e4b` generating unbounded tokens on a single image). The transports now take keyword-only `timeout`/`max_tokens`: LM Studio passes `timeout` + `max_retries=0` to `openai.OpenAI(...)` and `max_tokens` to `create(...)`; Ollama uses the timed `_ollama_client(OLLAMA_HOST, timeout)` + `options={"num_predict": max_tokens}`. `_chat_lm_studio_image` never forwards `timeout=None` (the SDK can read that as "no timeout"). `services/vision.py`, `core/providers/ollama.py`.
- **The description path pre-emptively downscales oversized images** (`_fit_base64_image_to_max_side`, longest side > `VISION_IMAGE_MAX_SIDE` = 1568 px) to kill the giant-image prefill-stall mode. Best-effort PNG re-encode that returns the original on any failure (undecodable image — HEIC without `pillow-heif`, which is not installed — or non-image input). Invisible to the description cache (keyed on original bytes). OCR is deliberately **not** pre-downscaled (legibility); its existing context-overflow downscale-retry is unchanged and still caches on the original `base64_png`.

### Added — Settings-window knobs for the vision/OCR bounds
- `vision_timeout_s` (5-600, default 120; **always on — no "0 = off"** unlike `local_request_timeout_s`), `vision_max_tokens` (64-8192, default 1536), `ocr_max_tokens` (64-8192, default 4096). Persisted defaults in `core/config.py`, validated/clamped in `api/routes/config.py::_CONFIG_VALIDATORS`, read **per call** via `_cfg_bounded_int` (lazy `load_config`) so a change applies without restart, with a hard `DEFAULT_*` fallback on missing/≤0/garbage and a clamp to the validator range for a hand-edited out-of-range value. UI controls live in the **LLM Settings → Global · OCR & Vision** block and are owned by `static/js/settings.js`. `core/constants.py` adds `DEFAULT_VISION_TIMEOUT_S`/`DEFAULT_VISION_MAX_TOKENS`/`DEFAULT_OCR_MAX_TOKENS`/`VISION_MAX_RETRIES`/`VISION_IMAGE_MAX_SIDE`.

### Tests
- `test_concurrency.py::TestVisionCallBounds`: downscale shrinks oversized / no-ops small / survives junk; `describe_image` downscales and passes the bounded `timeout`/`max_tokens`; OCR passes `ocr_max_tokens` (not `vision_max_tokens`); the LM Studio transport sets `max_retries=0`, forwards a real `timeout`, caps `max_tokens`, and omits `timeout`/`max_tokens` when unset; `_cfg_bounded_int` falls back for unset/0/garbage and clamps a positive out-of-range value. `smoke_test.py::test_03c` covers the three new keys as defaults + clamp.

## 2026-06-20 (Production-readiness pass: provider-API correctness, agent timeouts, live model listing)

A code-review + audit response. All changes verified against the live provider docs (platform.claude.com, ai.google.dev, platform.openai.com) where an HTTP contract was involved.

### Fixed — online provider API correctness
- **OpenAI o-series reasoning models now send the right parameters.** `o1` / `o1-mini` / `o1-preview` / `o3-mini` (all in the curated list) reject `temperature`/`top_p` ("Only the default (1) value is supported") and require `max_completion_tokens` instead of the deprecated `max_tokens` — so every chat call against them used to 400. `OpenAIProvider._common_params` now detects the o-series by family prefix (`_is_reasoning_model`: `o` + digit, so future `o5`/`o6` are covered while `gpt-4o` is not) and emits `max_completion_tokens` + omits the sampling params; `gpt-*` keep the legacy shape (also safer for OpenAI-compatible `base_url` endpoints). `core/llm/adapters/openai.py`.
- **Anthropic temperature is clamped to its documented 1.0 ceiling.** Anthropic caps `temperature` at 1.0 while OpenAI/Gemini allow 2.0 and the vault-chat range is 0-2; a temperature in (1.0, 2.0] would 400 against Anthropic only. `AnthropicProvider._build_payload` now clamps to `min(temp, 1.0)`. `core/llm/adapters/anthropic.py`.

### Added — live model discovery (curated fallback)
- **`/api/models` for online providers now merges `CURATED_MODELS` with a live fetch** from each provider's models endpoint (OpenAI `client.models.list()`, Anthropic `GET /v1/models`, Gemini `GET /v1beta/models` filtered to `generateContent`), so newly-released models (e.g. a just-shipped Claude tier) appear without a code edit and retired ids simply never get appended. Curated stays first and authoritative for pricing + default selection. `core/llm/model_listing.py` (`merged_models`: short-TTL-cached, key-gated, never-raises, 20 s negative-TTL on failure so a not-yet-set key recovers fast) + per-adapter `_fetch_live_models`. Degrades to curated-only without a key / offline / on error, so no-key callers and the hermetic test suite are unchanged.

### Fixed — agent loop resilience & correctness
- **Agent reasoning calls are now bounded by the turn's wall-clock deadline.** Each iteration caps the per-call timeout to the remaining `deadline_monotonic_s`; the local tool-call path honours it via `_effective_local_timeout` (the tighter of `request.timeout_s` and `local_request_timeout_s`, **ceil-quantized to whole seconds** so the `OllamaProvider._client` cache — keyed by `(host, timeout)` — stays bounded instead of leaking one httpx pool per distinct float). `OllamaProvider._client(timeout=…)` gained an explicit override. This stops a wedged local backend from keeping the SSE worker thread alive past the deadline. `core/agent/loop.py`, `core/llm/adapters/local.py`, `core/providers/ollama.py`.
- **Agent mode honours the per-request `temperature`** and no longer coerces a configured `0.0` up to `0.3` (the old `cfg.get(...) or 0.3` treated falsy 0.0 as unset). `run_agent_loop(temperature=…)` threaded from the route. `core/agent/loop.py`, `api/routes/vault.py`.

### Changed — hardening
- **Secret redaction** adds Google OAuth (`ya29.…`) and generic `Authorization:` token patterns. `core/llm/redact.py`.
- **`online_max_retries` is clamped to `[0, 10]` in the factory** (mirrors the `/api/config` validator) so a hand-edited `config.json` can't make the retry loop attempt hundreds of round-trips. `core/llm/factory.py`.

### Added — answer-quality eval is now runnable hermetically
- Extracted `run_eval.run_pairs(manager, pairs, …)` so the scoring pipeline (previously only reachable behind `RUN_LIVE_EVAL=1` and never executed) is driven in CI by `tests/eval/test_eval_pipeline.py` with a fake manager — proving a grounded answer passes and a hallucinated one fails. Added two unanswerable hallucination-tripwire pairs to `golden_qa.json`. `tests/eval/`.

### Docs
- README states the **read-only vault** guarantee prominently (the app never writes to your Obsidian vault) and adds a screenshots scaffold (`docs/screenshots/`, whitelisted in `.gitignore`). `core/llm/CLAUDE.md` documents the live-listing merge, the o-series/Anthropic parameter contracts, and the agent-tool-path timeout exception; root `CLAUDE.md` updates the `/api/models` description.

### Tests
- New hermetic tests: live-merge / curated-fallback / failure-recovery / success-empty-cached (model listing), o-series + Anthropic parameter contracts, ollama client-cache quantization bound, agent temperature/timeout, and the eval pipeline. Full `test_llm` / `test_agent` / `test_concurrency` / `test_validators` / `test_prompts` / `test_deck` / `tests/eval` suites green (the only `test_llm` failures observed are a pre-existing, intermittent numpy "cannot load module more than once" import flake, identical with and without these changes).

## 2026-06-20 (Wikilink graph expansion — query-time, no reindex)

### Added — vault retrieval
- **Wikilink graph expansion** for vault chat: an opt-in, **reindex-free / re-embed-free** retrieval layer that widens the retrieved result set with chunks from notes linked to/from your top hits (outbound ∪ backlinks) before the rerank stage. Rerank-gated, so irrelevant neighbours drop out and the final answer size is unchanged.
  - **Graph builder** (`rag/vault.py`): `_WikilinkGraph` + `_get_wikilink_index()` build a note→note adjacency lazily from the docstore (regex over `.md` node text, Obsidian shortest-path resolution against the in-memory indexed-source set — no filesystem walk), cached by docstore size and invalidated by `_invalidate_retrieval_caches` like BM25 (no on-disk sidecar). Optionally warmed silently by `prewarm`.
  - **Expansion retriever** (`rag/engine.py`): `_WikilinkExpansionRetriever` wraps the finalized retriever in `_build_retrieval_pipeline` **before** the postprocessors — one insertion point covering the local, online, and agent-search paths. Neighbour chunks are fetched from the docstore at a decayed seed score; bounded by neighbour-note and node caps.
  - **Config** (`core/config.py`, `api/routes/config.py`, `api/routes/vault.py`): `vault_wikilink_expansion` (default `False`, body-overridable via `wikilink_expansion`) plus config-only caps `vault_wikilink_neighbor_cap` (1-100/10), `vault_wikilink_node_cap` (1-200/24), `vault_wikilink_score_decay` (0.0-1.0/0.5), all in the `_validate_llm_config_keys` clamp map. Applies to single-shot RAG (and the agent's RAG fallback) only — not the agent's active `vault.search`, matching `mmr_enabled`/`query_expansion`.
  - **UI** (`templates/index.html`, `static/js/vault.js`): a "Wikilink expansion" checkbox (`#vault-wikilink-enabled`) in the Vault Chat fine-tuning controls, owned by `vault.js` like the other live retrieval toggles (live per-Send body field + debounced config persistence).
  - **Tests**: `TestWikilinkGraph` (resolution/both-directions/limitations), `TestWikilinkExpansionRetriever` (caps/decay/dedup/seed-skip/pipeline-wrap-only-when-enabled), `_resolve_chat_params` resolution, and smoke config-clamp coverage. ~30 new hermetic tests; full vault/engine/config suite green.

## 2026-06-19 (Prompt audit + vault image descriptions + LanceDB metadata tolerance)

### Changed — prompt audit (all query/generation-time; **no reindex**)
- **Single-paper system prompt.** Dropped the "Each section: 3-6 sentences" clause that contradicted the per-template length budgets (CONCISE 1-2, DETAILED 1-3); dropped the "expert research assistant" persona framing while keeping the medical/biomedical domain cue; phrased affirmatively (small local models follow positive instructions more reliably). The detailed preset gains a lead-bias mitigation line ("draw on the whole article — including Methods and Results — not only the abstract/introduction"). `core/constants.py`.
- **Focus question is now actionable.** When supplied it carries an explicit directive ("Prioritise information that addresses this question; if the document does not, state that explicitly") instead of being injected inert, and is `.strip()`-guarded so a whitespace-only value adds nothing. `core/llm/prompt.py`.
- **Report-type prompts (7 built-ins).** Replaced the "You are a researcher specializing in X" identity opener with a "When summarising X, …" scoping clause; the "pay special attention to / focus on …" focus directives (the high-value part) are preserved verbatim. Only built-in defaults change — user-saved/overridden report types in `report_types.json` are untouched. `core/constants.py`.
- **Vault RAG citation instruction is consistent across all four answer modes.** strict/balanced/exploratory/concise now all end with "cite the source filename in brackets, e.g. `[note.md]`" (previously three said "cite source filenames when available" and only concise used brackets). `rag/engine.py`.
- **Agent preamble** gains an efficiency nudge ("prefer one or two focused searches") and a one-line worked example, targeting the small-local-model failure mode (tool-call looping / malformed calls that trip the RAG fallback). The pre-existing stop condition and untrusted-tool-output guard are retained. `core/agent/loop.py`.
- **Deckgen outline prompt** gains a one-line JSON shape example; `outline.parse_outline` (balanced-bracket extraction + heading-list fallback) remains authoritative. `deckgen/prompts.py`.

### Changed — vault image indexing (`b11dc65`)
- **Vault image vision prompt** (`VisionManager.describe_image`) now asks for a short description of what the image depicts **plus** a transcription of any visible text/labels/data, instead of the pure "extract all text" OCR prompt — which returned nothing for text-light figures, so those images embedded empty and were dropped. The scanned-PDF path (`GLMOCRManager.extract_page_text`) stays pure-OCR. The description cache is keyed by image bytes, not prompt text, so clear `obsidian_cache/.../image_cache/` to regenerate already-cached descriptions.
- **Obsidian-style attachment resolution.** Bare wikilinks (`![[image.png]]`) now resolve via a vault-wide basename index built during the single `rglob` walk, so an image in a central attachments folder resolves even when the link omits its path; ambiguous basenames break by shortest-path proximity to the linking note. The `attachments` metadata stays byte-identical, so no re-embedding is forced.

### Fixed — LanceDB backend (`8af0b0b`, `c14f565`)
- **Heterogeneous node metadata no longer aborts a lancedb build.** LanceDB freezes the metadata struct schema at table creation (MD-first → `{file_path, source, extension, header_path, attachments}`); later inserts adding a field — `page_start`/`page_end` on large-PDF range chunks, `is_image` on vault images — failed with "field does not exist in table schema", silently breaking large-textbook indexing (each run hit the consecutive-failure breaker at the first such chunk). `NormalizingLanceDBVectorStore.add()` now projects each node's metadata onto the live table's columns (`_allowed_metadata_keys()` reads the struct schema; `_LANCE_FLAT_METADATA_KEYS` is the fresh-table fallback). Dropping is lossless for the read path (those keys are write-only at query time; the full node lives in the docstore + `_node_content`). Vector-row only — `doc_id`/`doc.hash`/docstore untouched, so already-indexed MD/PDF chunks skip by hash with no re-embedding; only the previously-failing large PDFs + vault images embed (first-time) on the next run. `rag/lancedb_store.py`.

### Added — prompt regression net
- `test_prompts.py` (repo root, model-free): 20 invariants pinning the grounding/citation/placeholder/length-cap contracts of the prompts above, so an accidental edit surfaces as a failing assertion.
- `tests/eval/`: a self-contained fixture vault + golden Q&A pairs + a pure scoring layer (`scoring.py`, with `must_not_contain` using word-boundary matching to avoid substring false positives) plus an opt-in live runner (`run_eval.py`, gated by `RUN_LIVE_EVAL=1`) for before/after answer-quality comparison. The data + scoring half is verified hermetically by `tests/eval/test_scoring.py`.

### Tests
- New: 20 in `test_prompts.py`, 8 in `tests/eval/test_scoring.py`; lancedb metadata-projection regression in `test_lancedb_migration.py` (`8af0b0b`). Prompt-relevant + vault suites green with no regressions (`test_llm`, `test_agent`, `test_deck`, `test_validators`, `smoke_test`, `deckgen/tests`: 290 passed/1 skipped; `test_vault_regressions`, `test_concurrency`: 190 passed).

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
- **8 new config keys.** `audit_attachments_subdir` (default `Z_attachments`), `audit_biblio_articles_subdir` (default `biblio_articles`), `audit_zotero_notes_subdir` (default `Z_Zotero_Notes`), `audit_master_bib_path` (default `_master.bib`), `audit_zotero_sqlite` (default `~/Zotero/zotero.sqlite`), `audit_zotero_storage` (default `~/Zotero/storage`), `audit_annotations_read_threshold` (default `5`), `audit_biblio_skip_prefix` (default `z_item`). Vault root is shared with the existing `obsidian_vault_path` so there is no second vault setting.
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
