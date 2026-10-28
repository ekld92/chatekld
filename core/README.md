# Configuration, Storage & Locking Contracts (`core/`)

`core/` owns application configuration (`config.py`), constants (`constants.py`), SQLite upload persistence (`database.py`), operation locking (`op_lock.py`), atomic file writers, and vault write auditing (`vault_audit.py`).

## Startup Environment & Storage Layout (`BASE_DIR`)

By default on macOS, application data lives under `~/Library/Application Support/ChatEKLD/`. Pointing CHERQAiN at an Obsidian vault never moves indexes, databases, or logs into the vault.

| Environment Variable | Legacy Fallbacks | Default Under `BASE_DIR` | Purpose |
| --- | --- | --- | --- |
| `CHERQAIN_BASE_DIR` | `CHATEKLD_BASE_DIR`, `PAPERMIND_BASE_DIR` | `~/Library/Application Support/ChatEKLD/` | Root application data directory (selected at startup; no live migration). |
| `CHERQAIN_DB_PATH` | `CHATEKLD_DB_PATH` | `uploads.db` | SQLite database for Single-Paper and Plain Chat extracted text. |
| `CHERQAIN_INDEX_DIR` | `CHATEKLD_INDEX_DIR` | `obsidian_storage/` | Vector index (`SimpleVectorStore` or `lancedb/`), `docstore.json`, `obsidian_meta.json`, `indexed_materials.json`, and retrieval sidecars. |
| `CHERQAIN_CACHE_DIR` | `CHATEKLD_CACHE_DIR` | `obsidian_cache/` | Reusable `pdf_cache/`, `image_cache/`, and `pdf_signatures/<vault_key>.json`. |
| `CHERQAIN_API_URL` | `CHATEKLD_API_URL` | Discovered via `BASE_DIR/server.json` | Loopback URL used by `cherqain` CLI and MCP clients. |
| `CHERQAIN_VENV_DIR` | `CHATEKLD_VENV_DIR`, `PAPERMIND_VENV_DIR` | `~/venvs/chatekld2026` | Virtualenv directory used by `install_and_build.sh`. |

### Additional `BASE_DIR` Files & Subdirectories

- `.env`: Optional owner-only (`chmod 600`) API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `DEEPSEEK_API_KEY`, `ZAI_API_KEY`, `OPENROUTER_API_KEY`, `TAVILY_API_KEY`, `ZOTERO_API_KEY`). Loaded at startup (`override=False`); never written to `config.json` or logs.
- `config.json`: Validated user settings.
- `chatekld.log`: Application log with write-time secret redaction (`core/llm/redact.py`).
- `llm_usage.jsonl` (and `.1` rotation at 10 MB): Append-only token and estimated USD cost log.
- `feedback.jsonl`: User feedback records.
- `widget.json`: Snapshot for the SwiftBar and Übersicht desktop widgets.
- `audit/`: `mapping.json` (hash-anchored manual PDF↔bib curation), `dup_decisions.json`, `pdf_wanted.json`, `pdf_reviews.sqlite3`, and regenerable `hash_cache.json` / `doi_cache.json`.
- `tagging/`: `journal.sqlite3` (3-target tag & Zotero restore journal) and `attachment_intents.sqlite3`.
- `online_index_runs/`, `online_ocr_attempts/`, `online_ocr_promotions/`, `online_ocr_exports/`, `readable_pdf_exports/`, `document_recovery/`: Durable extraction and recovery receipts.

## Concurrency, Operation Locks & Atomic Writes

- **Operation Lock (`RagOperationLock`):** Prevents concurrent vault mutations and indexing conflicts. Callers acquire with explicit TTLs (3600 s for indexing / tagging / docgen; 600 s for History and Restore) and refresh via heartbeat during long runs.
- **Atomic & Durable Persistence:** Critical JSON/Markdown writers write to a temporary sibling file in the target directory, call `os.fsync`, and atomically `os.replace` the destination.
- **Vault Write & Storage Deletion Auditing:** Every confirmed vault mutation records an entry via `log_vault_write` and writes a recovery snapshot to its respective journal (`vault_history/` or `tagging/journal.sqlite3`). Every code path calling `shutil.rmtree` on `OBSIDIAN_INDEX_DIR` or its backups must first record a stack trace and `.last_deletion_log` marker via `core.utils.log_storage_deletion(reason)`.
- **Online Provider Registration Checklist:** Adding an online provider requires updating `ONLINE_PROVIDER_NAMES` (in `core/llm/factory.py` and `core/config.py`), `_ONLINE_MODEL_KEYS`/`_ONLINE_DEFAULT_MODELS`, `api/routes/config.py::_PROVIDER_NAMES` + `_CONFIG_VALIDATORS`, `DEFAULT_*_MODEL` + `_DEFAULTS`, `PRICING_TABLE` + `unpriced_curated_models`, `<option>`s in `templates/index.html`, `static/js/config.js::_ONLINE_MODEL_KEYS`, and `static/js/ui.js`'s `_ONLINE_PROVIDERS` + `_PROVIDER_LABELS` (ratcheted by `test_online_provider_registries_agree` and `tests/js/providerRegistry.test.js`).

## Config Keys (`config.json`)

All keys written via `POST /api/config` are range- and enum-validated server-side; invalid values are dropped without clobbering existing settings. `config_version` (currently `5`, server-owned and stripped on `POST /api/config`) drives pure one-time schema migrations (`_MIGRATIONS` in `core/config.py`) and prunes unknown keys on load.

### Providers, Routing & Cost

| Key | Default | Range / Values | Purpose |
| --- | --- | --- | --- |
| `provider` | `"ollama"` | `ollama`, `lm_studio`, `openai`, `anthropic`, `google`, `deepseek`, `zai`, `openrouter` | Active chat provider. |
| `llm` | `"llama3.2"` | string | Selected chat model for local providers (`ollama` / `lm_studio`). |
| `<provider>_model` | `"gpt-4o-mini"`, `"claude-haiku-4-5"`, `"gemini-2.5-flash"`, `"deepseek-flash"`, `"glm-4.7"`, `"anthropic/claude-haiku-4.5"` | string | Per-provider model keys (`openai_model`, `anthropic_model`, `google_model`, `deepseek_model`, `zai_model`, `openrouter_model`). |
| `embed` | `"nomic-embed-text"` | string | Local embedding model (must match recorded index identity). |
| `embed_provider` | `"ollama"` | `ollama`, `lm_studio` | Local backend used for embeddings when `provider` is online. |
| `online_timeout_s` | `60` | `5–600` | Per-request HTTP timeout (s) for online providers. |
| `online_max_retries` | `3` | `0–10` | Transient error retry count beyond the initial attempt. |
| `online_max_tokens` | `4096` | `64–32768` | Default output token cap for online requests. |
| `online_reasoning_effort` | `"provider_default"` | `provider_default`, `minimal`, `low` | Opt-in reasoning budget for supported online models. |
| `fallback_provider` | `""` | provider name or `""` | Pre-first-token fallback provider on transient errors. |
| `fallback_on` | `["timeout","network","rate_limit","server_error"]` | list | Error categories triggering fallback (`auth`, `invalid_request`, `quota` never fall back). |
| `llm_pricing_overrides` | `{}` | `{model: {input, output}}` | Config-only USD-per-1M-token overrides. |
| `online_doc_window_tokens` | `100000` | `4000–2000000` | Config-only token threshold above which online Single-Paper summaries switch to map-reduce. |
| `context_window` | `32768` | `512–131072` | Config-only assumed token context window for local LLMs. |
| `local_request_timeout_s` | `0` | `0–3600` | Per-call timeout (s) for local HTTP chat (`0` = SDK default). |
| `agent_wall_clock_s` | `300` | `30–1800` | Wall-clock deadline (s) per agent turn or deck/article section. |
| `prompt_capture_enabled` | `true` | bool | Capture latest system/user prompts in memory for the Prompt Hub tab. |

### OCR & Vision (`services/vision.py`)

| Key | Default | Range / Values | Purpose |
| --- | --- | --- | --- |
| `ocr_provider` / `ocr_model` | `"ollama"` / `"glm-ocr:latest"` | provider / model | Provider and model for scanned-PDF page OCR. |
| `vision_provider` / `vision_model` | `"ollama"` / `"qwen3-vl:4b"` | provider / model | Provider and model for note-referenced image description. |
| `vision_timeout_s` | `120` | `5–600` | Always-on per-call HTTP timeout (s) for OCR and vision. |
| `vision_max_tokens` | `1536` | `64–8192` | Max output tokens for image descriptions. |
| `ocr_max_tokens` | `4096` | `64–8192` | Max output tokens for scanned-page OCR. |
| `vision_failure_cooldown_s` | `30` | `0–600` | Fast-fail skip window (s) after a failed vision/OCR call during indexing. |

### Vault Indexing & Retrieval (`rag/`)

| Key | Default | Range / Values | Purpose |
| --- | --- | --- | --- |
| `obsidian_vault_path` | `""` | absolute path | Selected Obsidian vault root (broad system roots rejected). |
| `vault_exclude_dirs` | `[]` | vault-relative list | Subfolders skipped during vault scan. |
| `vault_image_exts` | `[".png",".jpg",...]` | ≤64 image suffixes | Extensions eligible for vision description when linked from `.md`. |
| `vault_vector_backend` | `"simple"` | `simple`, `lancedb` | Backend for **new** index builds (`obsidian_meta.json` governs existing indexes). |
| `vault_embed_batch_size` | `16` | `1–256` | Config-only chunks buffered per `insert_nodes` / embed batch. |
| `vault_prewarm_enabled` | `true` | bool | Prewarm vector store, BM25, wikilinks, and reranker at startup. |
| `vault_top_k` | `8` | `1–32` | Final chunks passed to the LLM. |
| `vault_similarity_cutoff` | `0.25` | `0.0–1.0` | Minimum retriever score (applied only when `vault_reranker_enabled=false`). |
| `vault_prompt_mode` | `"strict"` | `strict`, `balanced`, `exploratory`, `concise` | Grounding template for single-shot RAG. |
| `vault_chat_temperature` | `0.3` | `0.0–2.0` | Sampling temperature for vault chat. |
| `vault_chat_system_prompt` | `""` | ≤4000 chars | Behavioural instructions layered over the protected safety preamble. |
| `vault_hybrid_enabled` | `true` | bool | Fuse BM25 lexical + dense vector retrieval via RRF (query-time). |
| `vault_rrf_dense_weight` / `vault_rrf_bm25_weight` | `1.0` / `1.0` | `0.0–10.0` | Config-only RRF leg weights (`!= 1.0` switches to `relative_score`). |
| `vault_reranker_enabled` | `true` | bool | Cross-encoder rerank stage (query-time). |
| `vault_reranker_model` | `"cross-encoder/ms-marco-MiniLM-L-6-v2"` | HF model ID | Config-only cross-encoder model (`BAAI/bge-reranker-v2-m3` recommended for multilingual vaults). |
| `vault_reranker_device` | `"auto"` | `auto`, `cpu`, `mps` | Cross-encoder execution device. |
| `vault_rerank_pool_multiplier` / `floor` / `ceiling` | `4` / `20` / `50` | `10–200` (ceiling) | Candidate pool size `min(max(top_k * mult, floor), ceiling)`. |
| `vault_mmr_enabled` / `vault_mmr_lambda` | `false` / `0.5` | bool / `0.0–1.0` | Query-time MMR diversity on the dense retriever. |
| `vault_query_expansion` / `vault_num_queries` | `false` / `3` | bool / `1–5` | Multi-query LLM rewrite + RRF fusion. |
| `vault_wikilink_expansion` | `false` | bool | Rerank-gated graph expansion across outbound and backlink notes. |
| `vault_wikilink_neighbor_cap` / `node_cap` / `score_decay` | `10` / `24` / `0.5` | `1–100` / `1–200` / `0.0–1.0` | Config-only fan-out bounds for wikilink expansion. |
| `vault_thesaurus_expansion` / `vault_thesaurus_max_variants` | `false` / `3` | bool / `1–8` | Query-time synonym expansion from `_abreviations.md` and `_tags.md`. |
| `vault_primer_enabled` / `vault_primer_max_chars` | `false` / `1500` | bool / `500–8000` | Prepend query-relevant glossary definitions to the system prompt. |
| `vault_thesaurus_abbrev_path` / `tags_path` | `"_abreviations.md"` / `"_tags.md"` | vault-relative path | Config-only paths to curated glossary Markdown tables. |
| `vault_primer_header` / `core_terms` | `""` / `""` | string | Config-only custom glossary header and priority abbreviation list. |
| `vault_paper_labels` | `true` | bool | Config-only: annotate retrieved chunks with `"Author, Year, Title [key]"` from the paper index. |
| `vault_agent_enabled` / `vault_agent_max_iterations` | `false` / `6` | bool / `1–12` | Route Ask through the ReAct tool loop instead of single-shot RAG. |
| `vault_web_search_enabled` / `max_results` | `false` / `5` | bool / `1–8` | Enable Tavily `web_search` tool in agent mode (requires online provider + `TAVILY_API_KEY`). |

### Library Audit, Tagging & Per-Workflow Generation Defaults

| Key Group | Keys & Defaults | Purpose |
| --- | --- | --- |
| **Library Audit (`audit_*`)** | `audit_attachments_subdir` (`"Z_attachments"`), `audit_biblio_articles_subdir` (`"biblio_articles"`), `audit_zotero_notes_subdir` (`"Z_Zotero_Notes"`), `audit_master_bib_path` (`"_master.bib"`), `audit_zotero_sqlite` (`"~/Zotero/zotero.sqlite"`), `audit_zotero_storage` (`"~/Zotero/storage"`), `audit_annotations_read_threshold` (`5`), `audit_biblio_skip_prefix` (`"z_item"`), `audit_near_dup_ratio` (`88`), `audit_drift_include_parent_tags` (`false`), `audit_no_pdf_types` (`"webpage,presentation,blogPost"`) | Configured via **Settings → Library paths and audit settings** (`/api/audit/config`; stripped by generic `POST /api/config`). |
| **Accession (`accession_*`)** | `accession_enabled` (`false`), `accession_sources` (`["crossref","europepmc","arxiv","hal","thesesfr"]`), `accession_max_candidates` (`5`), `accession_timeout_s` (`15`), `accession_batch_max` (`50`), `accession_contact_email` (`""`), `accession_google_books` (`false`), `accession_open_library` (`false`) | Opt-in keyless bibliographic lookup settings. |
| **Tagging & Notes Sync (`tagging_*`, `notesync_*`)** | `tagging_propose_model` (`""`), `tagging_propose_max_tokens` (`1536`), `tagging_propose_temperature` (`0.2`), `tagging_max_tags` (`4`), `tagging_min_conf` (`0.35`), `tagging_text_max_chars` (`6000`), `tagging_candidate_tags` (`20`), `tagging_fewshot_k` (`3`), `tagging_archive_dir` (`""`), `tagging_zotero_transport` (`"auto"`), `tagging_zotero_api_base` (`"http://127.0.0.1:23119"`), `tagging_zotero_web_api_base` (`"https://api.zotero.org"`), `tagging_journal_retention_enabled` (`false`), `tagging_journal_retention_policy` (`"age_days"`), `tagging_journal_retention_days` (`365`), `tagging_journal_retention_keep` (`5000`), `notesync_template` (`""`), `notesync_containment` (`0.85`), `notesync_zotero_authoritative` (`false`) | Biblio Tagging LLM proposer, journal retention, Zotero HTTP transport selection (`auto`, `local`, `web`), and Zotero → Obsidian note sync settings. |
| **Single Paper (`paper_*`)** | `paper_temperature` (`0.3`), `paper_num_ctx` (`32768`), `paper_max_tokens` (`4096`), `paper_top_p` (`0.9`), `paper_repeat_penalty` (`1.1`) | Default generation parameters for the `Read` tab. |
| **Deck (`deck_*`)** | `deck_temperature` (`0.3`), `deck_max_sections` (`8`), `deck_agent_max_iterations` (`6`), `deck_section_max_attempts` (`3`), `deck_retry_backoff_s` (`3`), `deck_section_max_tokens` (`2048`), `deck_resume_enabled` (`true`), `deck_review_enabled` (`false`), `deck_review_model` (`""`), `deck_review_max_tokens` (`4096`), `deck_compile_timeout_s` (`180`), `deck_compile_max_iters` (`2`), `deck_compile_engine` (`"pdflatex"`) | Studio Beamer generation, integrity review, and `latexmk` compile-fix bounds. |
| **Report / Article (`docgen_*`)** | `docgen_temperature` (`0.3`), `docgen_max_sections` (`8`, range `1–30`), `docgen_agent_max_iterations` (`6`), `docgen_section_max_attempts` (`3`), `docgen_retry_backoff_s` (`3`), `docgen_section_max_tokens` (`4096`), `docgen_resume_enabled` (`true`) | Studio Report/Article generation defaults (shares `deck_compile_*` and `deck_review_*`). |
| **Plain Chat (`chat_*`)** | `chat_temperature` (`0.3`), `chat_system_prompt` (`"You are a helpful assistant."`), `chat_attachment_max_chars` (`120000`, range `1000–400000`) | RAG-free chat defaults and per-file attachment character cap. |
| **Maintenance (`maintenance_*`)** | `maintenance_links_index_subdir` (`""`), `maintenance_links_index_note` (`""`), `maintenance_max_drift` (`0`), `maintenance_max_no_pdf` (`2200`), `maintenance_max_dupes` (`0`), `maintenance_max_hygiene` (`0`), `maintenance_max_hygiene_unmapped` (`0`) | Private-only Vault Maintenance check thresholds and index-note subpaths (inert in public builds). |
| **History (`refactor_archive_dir`)** | `""` (uses `BASE_DIR/refactor/archive/<vault_key>`) | Preserves custom archive location for **Settings → History and Restore**. |
