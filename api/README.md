# HTTP API & SSE Contracts (`api/`)

CHERQAiN serves a loopback-only single-OS-user desktop HTTP API via Waitress (32-worker pool, 100-channel ceiling).

## Security, Origin & Body Bounds

- **Loopback & Origin Guard (`api/security.py`):** Validates `Host`, `Origin`/`Referer`, and peer loopback address. Mutating and sensitive routes require `X-Requested-With: ChatEKLD` as a local-origin/CSRF signal (not authentication across same-user processes).
- **Request Envelopes:** Ordinary request bodies cap at **2 MiB**. `POST /api/chat-attachment` accepts **64 MiB** multipart uploads; `POST /api/upload` accepts a **501 MiB** multipart envelope (streamed to disk with a **500 MiB** PDF cap).
- **SSE Worker Ceiling (`api/sse.py`):** At most **12** concurrent streaming operations (`_MAX_LIVE_SSE_WORKERS`). A 13th concurrent stream returns HTTP `503`.
- **SSE Frame Protocol:** Streaming endpoints emit JSON `data:` lines:
  - Token stream: `{"token": "..."}`
  - Status/progress: `{"info": "..."}`
  - Agent trace (`/api/obsidian/chat`, `/api/deck/generate`, `/api/docgen/generate`): `{"iteration": N}`, `{"thought": "..."}`, `{"tool_call": {"id","name","arguments"}}`, `{"tool_result": {"tool_call_id","content","is_error","truncated"}}`
  - Usage summary: `{"usage": {"input_tokens", "output_tokens", "cached_input_tokens", "total_tokens", "cost_usd", "iteration_count?"}}`
  - Terminal error (always precedes `[DONE]`): `{"error": "..."}`
  - Stream terminator: `data: [DONE]`

## Endpoint Inventory

### Core, Config, Providers & Usage (`api/routes/status.py`, `config.py`, `usage.py`, `about.py`, `open.py`)

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/health` | GET | Non-draining liveness/readiness probe (`_TtlSingleFlight` cached ~10 s; `{status, details}`). External widgets and `server.json` discovery **must** poll `/api/health`, never `/api/obsidian/status` (which destructively drains one-shot status messages). |
| `/api/status` | GET | Provider reachability, vector store state, and runtime warnings (`_TtlSingleFlight` cached 5 s). |
| `/api/models` | GET | Curated + live chat models for the active (or `?provider=`) provider (`configured` is `""` for a non-active local provider). |
| `/api/vision-models` | GET | Provider-scoped model list for OCR and vision selectors (`ollama` or `lm_studio`). |
| `/api/config` | GET/POST | Read or range/enum-validated update of `config.json` (never returns or writes API keys; strips `audit_*` and `config_version`). |
| `/api/pull` | POST | Pull an Ollama model via `resolve_ollama_host()` (disabled for LM Studio / online providers). |
| `/api/native-pick-folder` | POST | Open a native macOS folder/file picker (`kind`, `relative_to_vault`, `under_attachments`). |
| `/api/reset` | POST | Clear `uploads.db` and the Obsidian index (records `log_storage_deletion`; optional config/feedback wipe). |
| `/api/log` | POST | Record a frontend error log entry (≤500 chars). |
| `/api/log/tail` | GET | Read-only redacted tail of `chatekld.log` (`lines` ≤ 5000, last ~1 MB). |
| `/api/open` | POST | Reveal vault-relative `{path}` in Finder (`run_open` reap-safe helper) or open `{url}` (`http`/`https` only) in default browser. |
| `/api/prompts` | GET | Read-only Prompt Hub snapshot of the latest effective system/user prompt per workflow. |
| `/api/about` | GET | Build metadata and version info. |
| `/api/usage` | GET | Token and USD cost totals + activity slice (`window`: `today`, `week`, `month`, `month_to_date`, `all`). |
| `/api/pricing` | GET | Published USD-per-1M-token rates from `core/llm/usage.py`. |
| `/api/widget` | GET | Desktop-widget snapshot (`widget.json`; 404 until an Audit scan completes). |
| `/api/widget/refresh` | POST | Rebuild `widget.json` from the cached Audit scan (409 without one; `redraw` optional). |

### Single Paper & Plain Chat (`api/routes/paper.py`, `plainchat.py`)

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/report-types` | GET | List Single-Paper document-type presets. |
| `/api/upload` | POST | Upload and extract one PDF (≤500 MB) into `uploads.db`. |
| `/api/upload/<upload_id>` | DELETE | Delete a stored upload row. |
| `/api/summarise` | POST | Stream a single-paper summary (direct or map-reduce) as SSE. |
| `/api/export-summary` | POST | Export summary or conversation (`raw: true`) to `~/Downloads` as `.txt` or `.md`. |
| `/api/feedback` | POST | Save a user feedback record to `feedback.jsonl`. |
| `/api/feedback/history` | GET | Read recorded feedback. |
| `/api/plainchat` | POST | Stream RAG-free multi-turn chat (last 20 turns + `attachment_ids`) as SSE. |
| `/api/chat-attachment` | POST | Upload and extract one Plain Chat attachment (`.pdf`/`.png`/`.jpg`/`.md`/`.txt`, ≤64 MB). |
| `/api/chat-attachment/<id>` | DELETE | Discard an uploaded Plain Chat attachment. |

### Obsidian Vault & Online OCR (`api/routes/vault.py`, `online_ocr.py`)

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/obsidian/index` | POST | Start incremental or confirmed `force_fresh` vault indexing (publishes run state synchronously via `begin_index_run`). |
| `/api/obsidian/status` | GET | Poll indexing state, progress, and persistent `error_code`/`error_detail` (destructively drains `_status_messages`; single UI reader per tick). |
| `/api/obsidian/readiness` | GET | Bounded (`≤8 MiB`) read-only check of `obsidian_meta.json` and `indexed_materials.json` without loading models or walking the vault. |
| `/api/obsidian/materials` | GET | List indexed files and chunk counts from `indexed_materials.json`. |
| `/api/obsidian/chat` | POST | Stream Vault RAG (single-shot or ReAct agent mode) as SSE. |
| `/api/obsidian/pause` | POST | Request resumable indexing pause. |
| `/api/obsidian/cancel` | POST | Cancel indexing and release the operation lock. |
| `/api/online-ocr/session/preview` | POST | Preview source scope and mint a mode-bound `intent-v1:` token (`prepare_text` or `update_search`). |
| `/api/online-ocr/session/start` | POST | Execute confirmed text preparation or combined extraction + index update. |
| `/api/online-ocr/session/continue-index` | POST | Continue an interrupted search index update from a compatible `update_search` receipt (`prepare_text` forbidden). |
| `/api/online-ocr/recovery/import` | POST | Confirmed import of a verified offline extraction recovery package under the promotion journal. |
| `/api/online-ocr/status` | GET | Authoritative non-draining live status for extraction and standalone indexing runs. |
| `/api/online-ocr/resources` | GET | Read-only memory pressure probe (`sample_count`, first + latest 255 samples). |
| `/api/online-ocr/runs` | GET | List saved extraction/index run receipts (`/<run_id>` for detail). |

### Library Audit, Tagging, Zotero & History (`api/routes/audit.py`, `pending.py`, `tagging.py`, `notesync.py`, `zotlink.py`, `zotcreate.py`, `zotrecord.py`, `library_workflow.py`, `history.py`)

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/audit/config` | GET/POST | Read or update Library Audit subpaths, Zotero paths, and thresholds. |
| `/api/audit/status` | GET | Poll scan state (`idle`, `scanning`, `done`, `error`, `cancelled`) and messages. |
| `/api/audit/scan` | POST | Start a manual Library Audit background scan (the only scan trigger). |
| `/api/audit/cancel` | POST | Cooperatively cancel an in-flight scan. |
| `/api/audit/inventory` / `/summary` | GET | Cached per-citation-key inventory records and whole-library summary (`tag_coverage`, `paper_index_built_at`). |
| `/api/audit/reports/<name>` | GET | Fetch one of the 18 cached audit reports (see [`audit/CLAUDE.md`](../audit/CLAUDE.md)). |
| `/api/audit/library/<view>` | GET | Library workspace projections (`overview`, `documents`, `notes`, `checks`, `storage`, `links`, `history`). |
| `/api/audit/library/bibliography/*` | GET/POST | Bibliography verification campaigns (`plan`, `authenticate`, `<id>`, `<id>/decisions`; `<id>/apply` returns `501` in production). |
| `/api/audit/search` | GET | Accent-folded search across all cached reports and Documents (`?q=`, 202 while building). |
| `/api/audit/pdf` / `/pdf-facts` | GET/POST | Stream vault-contained PDF bytes for blob-URL preview (`?rel=`) or inspect page-1 provenance/DOIs (`POST /api/audit/pdf-facts`). |
| `/api/audit/mapping` | POST | Confirmed `mapping.json` write: link, `no_match`, `pending`, `detach`/`detach_all` (`trash_zotero_links`), or `clear`. |
| `/api/audit/dup-decision` | POST | Record or undo a Possible-Duplicates dismissal in `dup_decisions.json` (`pair_id` or `pair_ids` ≤ 2000). |
| `/api/audit/pdf-decision` | POST | Record or undo a Bib-Without-PDF dismissal in `pdf_wanted.json` (`citation_keys` or `clear_all`). |
| `/api/audit/apply-tag-drift` | POST | Confirmed merge of missing Zotero tags into one Obsidian note's YAML frontmatter (`.bak` backup). |
| `/api/audit/reveal` | POST | macOS Finder reveal (`path`), default-app open (`open`), or `zotero://` select (`zotero_key`). |
| `/api/accession/*` | GET/POST | Keyless bibliographic lookup (`GET rows?scope=orphans\|pending\|all`, `POST identify` SSE, `POST batch` SSE). |
| `/api/accession/pdfs/*` | GET/POST | Manage PDFs (`entry`, `candidates`, `preview`, `apply`, `restore`, `inspect`, `complete`, `holding`). |
| `/api/accession/review/*` | GET/POST | Pending-PDF review workspace (`open`, `decision`, `page`, `export`, `reconcile`, `link`, `search`, `rename-preview` (501 if private Maintenance absent), `workspace`, `refresh-zotero`, `discrepancy`, `metadata`, `repair*`). |
| `/api/tagging/*` | GET/POST | Biblio Tagging (`vocab`, `inventory?scope=with_pdf\|all`, `propose`, `snippet`, `pdf`, `open-pdf`, `zotero-available`, `apply-note-tags`, `apply-finder-tags`, `apply-zotero-tags`, `apply-all`, `remove-tags`, `harmonize` SSE, `retire-tag` SSE, `add-vocab-tag`, `pdf-links-status`, `apply-pdf-links`, `journal-stats`, `compact-journal`, `manifest`, `restore`). |
| `/api/notesync/*` | GET/POST | Reference-note sync (`status`, `sync-status`, `preview`, `create`, `update-preview`, `update`, `pull`, `push`, `merge`, `bootstrap`, `refresh-all`, `mirror` SSE, `key-changes`, `rename-note`, `create-attempts`, `inspect-create-attempt`, and `campaign/*` where `apply`/`recover`/`settle`/`undo` return `501`). |
| `/api/zotlink/*` | GET/POST | Zotero attachment management (`status`, `trash` SSE, `repoint-candidates`, `repoint`, `retitle` SSE, `attach-candidates`, `attach` SSE). |
| `/api/zotcreate/*` | GET/POST | Orphan-PDF Zotero item creation (`preview`, `create`, `batch` SSE, `attempts`). |
| `/api/zotrecord/*` | POST | Zotero parent record operations (`retitle`, `merge-preview`, `merge`, `trash`). |
| `/api/library/*` | GET/POST | Staged daily library sessions (`observe`, `analyse`, `apply`, `steps`, `resolve`). |
| `/api/history/manifest` & `/restore` | GET/POST | List or restore journalled vault operations (`vault_history/`) for **Settings → History and Restore**. |
| `/api/maintenance/*` | GET/POST | Optional private-only Vault Maintenance blueprint (`steps`, `run` SSE, `cancel`, `rename-preview`, `rename`). |

### Studio: Deck & Document Generation (`api/routes/deck.py`, `docgen.py`)

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/deck/load-template` | POST | Validate a Beamer `.tex` template and scan custom macros + `.bib` keys. |
| `/api/deck/generate` | POST | Stream grounded Beamer `.tex` generation (emit-only, optional integrity review). |
| `/api/deck/apply-repair` | POST | Write a confirmed integrity-review repair (`base_sha256` stale-diff guarded). |
| `/api/deck/deck-sections` | POST | List `\section`s of an existing `.tex` deck for scoped augmentation. |
| `/api/deck/augment` | POST | Stage a screened section/whole-deck augmentation (`deepen`, `table`, `new_section`, `reformat`). |
| `/api/deck/apply-augment` | POST | Write the staged augmentation (`base_sha256` + `proposed_sha256` guarded, `.tex.bak` saved). |
| `/api/deck/compile-available` | GET | Check if `latexmk` is available on the host. |
| `/api/deck/compile-preview` | POST | Run one plain `latexmk -no-shell-escape` pass without modifying `.tex`. |
| `/api/deck/compile-fix` | POST | Bounded `latexmk` + screened LLM log-repair loop (`.tex.bak` saved first). |
| `/api/deck/preview-pdf` | GET | Serve the compiled `<slug>.pdf` for in-app blob-URL preview. |
| `/api/deck/open-pdf` | POST | Open the compiled PDF in the macOS system viewer. |
| `/api/deck/native-pick-file` / `folder` | POST | Native pickers for template file and output directory. |
| `/api/docgen/*` | GET/POST | Article/Report equivalents of `/api/deck/*` (`load-template`, `generate`, `doc-sections`, `augment`, `apply-augment`, `compile-*`, `preview-pdf`, `open-pdf`) plus `POST /api/docgen/generate-md` (journalled new vault Markdown note). |
