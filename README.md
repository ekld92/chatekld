# ChatEKLD 2026

**Welcome!** ChatEKLD is a privacy-first desktop app that turns your Mac into a research workspace: summarise PDFs, chat with your Obsidian notes, reconcile your reference library, and generate grounded slide decks — all on your own machine.

ChatEKLD is a local Flask and PyWebView application for PDF summarisation, Obsidian vault RAG, library reconciliation, and grounded LaTeX slide generation. It runs offline by default with Ollama or LM Studio, and can optionally route chat through OpenAI, Anthropic, or Google Gemini while keeping the index — and your notes — on-device.

**At a glance:**

- **Single Paper** — drop in a PDF and get a clean, tunable summary you can export.
- **Obsidian Vault** — index your notes and PDFs, then ask questions and get grounded, cited answers (with an optional agent mode that searches as it reasons).
- **Library Audit** — a read-only reconciliation of your vault against Zotero, your PDFs, and Finder tags. This is a very personal tool, tailored to my own workflow, and may well not be of use to you.
- **Deck Generator** — turn a topic into a ready-to-compile LaTeX Beamer deck in your own template's house style.

**Feedback is very welcome!** This is an actively developed, personal project, so please don't hesitate to reach out to Etienne at [boss@etienned.xyz](mailto:boss@etienned.xyz) with ideas, bug reports, or just to let me know how you're finding it — I'd genuinely love to hear from you.

## Workflows

ChatEKLD is organised as four tabs, each a self-contained workflow:

1. **Single Paper** — upload one PDF, extract its text, and stream a tunable summary with prompt presets, document-type prompts, audience, focus question, and generation controls.
2. **Obsidian Vault** — index `.md` and `.pdf` files, then query them through dense retrieval, optional BM25 lexical retrieval, RRF fusion, and an optional cross-encoder reranker — in single-shot RAG or an opt-in ReAct agent mode. Inspect the indexed-material manifest from the UI.
3. **Library Audit** — a strictly read-only, **manual-only** reconciliation of the vault against Zotero (SQLite snapshot + Better BibTeX `_master.bib`), local PDFs (annotations + duplicates), and macOS Finder tags. Six reports plus a per-citation-key inventory.
4. **Deck Generator** — turn a topic + free-form instructions into a LaTeX **Beamer** deck grounded in the indexed vault, in your own template's house style. Emit-only: it writes a compile-ready `.tex` + `Makefile`; you compile with `make`.

## Features

**Single Paper**
- Upload one PDF and generate a streamed summary; export it as `.txt` or `.md`.
- Tune with concise/detailed presets, a document-type system prompt (systematic review, RCT, observational, narrative review, opinion/letter, case report, guideline), audience, focus question, output language, and a `system_prompt` override.
- Generation knobs (temperature, context window, max tokens, top-p, repeat penalty) resolve from request body → persisted `paper_*` defaults → hard clamp.

**Obsidian Vault**
- Index an Obsidian vault from `.md` and `.pdf` files (`.docx` excluded by design); configure which image extensions are described-and-embedded.
- Incremental, resumable, checkpointed indexing; choose the `simple` (JSON) or `lancedb` (binary Apache-Arrow) vector backend.
- Hybrid (BM25 + dense, reciprocal-rank-fusion) retrieval → cross-encoder rerank → LLM generation, with optional MMR diversity and multi-query expansion. Every retrieval knob is a query-time override — no reindex.
- Opt-in **agent mode**: a ReAct loop with `vault.search`, `vault.read_note`, and `vault.list_materials` tools, with a clean fallback to single-shot RAG.
- View the notes and PDFs recorded in the current index; clear the chat window client-side (the server holds no chat state).

**Library Audit**
- Reconcile the vault against Zotero, Better BibTeX, local PDFs, and macOS Finder tags. Strictly read-only and **manual only** — never runs at startup, only when you click **Run Scan**.

**Deck Generator**
- Generate a vault-grounded Beamer deck from the in-app **Deck Generator** window or the standalone `deckgen` CLI. Reuse your own Beamer template's preamble, custom macros (`\citefoot`, `\commonlogo`) and `_master.bib`; scaffold a compile-ready `<slug>/<slug>.tex` + `Makefile`. Emit-only.

**Providers & operations**
- Select the active chat provider, chat model, and embedding model in the UI; separate selectors for the OCR and vision providers/models (scanned-PDF fallback + image/figure reading).
- Local providers: Ollama and LM Studio (run without Ollama when LM Studio is active). Online chat-only providers: OpenAI, Anthropic, Google Gemini — embeddings and retrieval stay on-device, so only the query and retrieved chunks ever reach the cloud.
- Configurable fail-fast / hybrid fallback policy (e.g. fall back from OpenAI to Ollama on rate-limit only).
- Track token usage and estimated USD cost per request via `/api/usage` and the on-disk `llm_usage.jsonl`.
- A unified **LLM Settings** window centralises the provider/model, vault-chat, OCR/vision, generation-default, and timeout/fallback knobs; all settings persist to `config.json`.

## Prerequisites

ChatEKLD runs on macOS (Apple Silicon or Intel). The installer offers to set up anything missing, but it helps to have these ready:

- **macOS** with the Xcode Command Line Tools (`xcode-select --install`). Homebrew is installed automatically by `install_and_build.sh` if it is absent.
- **Python 3.12** specifically. The installer runs `brew install python@3.12` when 3.12 is not already on the `PATH`, and recreates the venv if it finds a different interpreter version.
- **A local model provider** — at least one of:
  - **[Ollama](https://ollama.com)** (the installer can `brew install` it for you), or
  - **[LM Studio](https://lmstudio.ai)** with its local server running on `http://localhost:1234`.
- **Disk and network:** the dependency set (PyTorch, Transformers, LanceDB, LlamaIndex, pyobjc, …) is several GB and downloads on first install. The installer also pre-caches a cross-encoder rerank model (~67 MB) and the tiktoken encodings so the first vault chat does not stall.
- **Optional — online chat providers.** OpenAI / Anthropic / Google need an API key each; see [Online provider setup](#online-provider-setup). Embeddings and retrieval always stay local.

### Models to pull on first run

To keep the install fast, **no models are pulled by default.** After launching, open the UI and:

1. Pull the **default embedding model** for the vault index — `nomic-embed-text` on Ollama (it is the app's shipped `embed` default; select another under `embed` if you prefer). It must be present in Ollama before indexing, and the chosen embedding model must stay consistent across re-indexes (switching it forces a re-embed).
2. Select a **chat model** — any installed Ollama tag, any model loaded in LM Studio, or an online provider once its key is set.

With Ollama you can pull models from the UI; LM Studio models are loaded inside LM Studio and listed from its local server.

## Setup

```bash
chmod +x install_and_build.sh
./install_and_build.sh
open ChatEKLD_$(date +%Y-%m-%d).app
```

The installer does not pull models by default. Ollama installation is optional. Select models in the UI after launch. Pulling models from the UI is available for Ollama only. LM Studio models are listed from `http://localhost:1234/v1/models`.

The built `.app` is **ad-hoc signed**. On the machine that built it, it launches normally — a locally built bundle carries no `com.apple.quarantine` flag. The flag is only attached when the app is **transferred to another Mac** (downloaded, AirDropped, or unzipped from a download), and that is when Gatekeeper blocks it ("ChatEKLD can't be opened because Apple cannot check it for malicious software"). On the receiving Mac, the reliable fix is to strip the quarantine flag:

```bash
xattr -dr com.apple.quarantine ChatEKLD_$(date +%Y-%m-%d).app
```

(Right-clicking the app and choosing **Open** may also work, but recent macOS versions increasingly route unsigned-app approval through System Settings → Privacy & Security → "Open Anyway" instead.)

If you only want to run from source without building a bundle, skip the installer and follow [Development](#development) instead.

## Development

```bash
python3.12 -m venv ~/venvs/papermind2026
source ~/venvs/papermind2026/bin/activate
pip install -r requirements.txt
python launch.py
```

The venv location is yours to choose — `~/venvs/papermind2026` is only a convention. `install_and_build.sh` creates its own venv at the same default path and honours a `PAPERMIND_VENV_DIR` override if you want it elsewhere.

Run checks from the virtual environment:

```bash
python -m py_compile app.py api/routes/*.py core/*.py core/providers/*.py core/llm/*.py core/llm/adapters/*.py core/agent/*.py rag/*.py services/*.py audit/*.py audit/core/*.py audit/engine/*.py audit/engine/reports/*.py deckgen/*.py
python -m pytest smoke_test.py test_concurrency.py test_vault_regressions.py test_llm.py test_validators.py test_agent.py test_audit.py test_deck.py tests/audit/ -v
```

The app-independent deckgen core has its own (non-hermetic, `requests`-free) suite:

```bash
python -m pytest deckgen/tests/ test_deck.py -v
```

## Architecture

- `app.py` creates the Flask app and registers blueprints; `launch.py` is the PyWebView entry point (logging, `.env` loading, offline-HF setup).
- `api/routes/` owns the HTTP route handlers (`paper`, `vault`, `audit`, `deck`, `config`, `status`, `usage`, `about`). `api/security.py` owns local-origin checks and error redaction; `api/validators.py` owns the canonical request-body coercion/clamp helpers.
- `core/` owns config, constants, the SQLite database + lock, feedback, and locking/atomic-write utilities.
  - `core/providers/` — **local** Ollama and LM Studio adapters (embeddings + the legacy local-chat path).
  - `core/llm/` — the provider-agnostic chat layer (`types`, `base`, `factory`, `policy`, `usage`, `retry`, `prompt`, `redact`, `tool_schema`) plus adapters for `local`, `openai`, `anthropic`, `google`.
  - `core/agent/` — the opt-in ReAct agent layer for vault chat (`protocol`, `tools`, `vault_tools`, `budget`, `loop`).
- `services/` owns single-PDF upload processing (`pdf_service.py`) and the vision/OCR manager singletons (`vision.py`). `pdf_extractor.py` runs PDF text extraction in a subprocess.
- `rag/` owns vault indexing + chat entrypoints (`vault.py`), the LlamaIndex query engine (`engine.py`), single-paper summarisation (`summarizer.py`), and the optional LanceDB vector backend (`lancedb_store.py`).
- `audit/` is the vendored Library Audit subsystem (read-only connectors under `core/`, report builders under `engine/`, the `audit_manager` singleton, and a `python -m audit` CLI).
- `deckgen/` is the Beamer-deck orchestrator; its core is app-independent and `requests`-free. The CLI (`__main__.py`) drives the running app over HTTP; `inprocess.py` is the only app-coupled module (used by the in-app Deck Generator window).
- `static/js/` owns the browser UI modules; `templates/index.html` is the single-page shell.

Each subtree carries a `CLAUDE.md` with deep implementation notes (`rag/`, `core/llm/`, `core/agent/`, `audit/`), and `deckgen/README.md` documents the deck orchestrator. `CHANGELOG.md` holds history; `project_structure.txt` is the annotated file map.

## Providers

The `provider` config key selects the active chat provider.

Local (offline):

- `ollama` uses the Ollama API and resolves bare model names to installed tags.
- `lm_studio` uses the OpenAI-compatible LM Studio API and passes model IDs through unchanged.

Online (require API keys):

- `openai` uses the OpenAI Chat Completions API. Default model: `gpt-4o-mini`. Set `OPENAI_API_KEY` in the environment.
- `anthropic` uses the Anthropic Messages API. Default model: `claude-haiku-4-5`. Set `ANTHROPIC_API_KEY`.
- `google` uses the Gemini REST API. Default model: `gemini-2.5-flash`. Set `GOOGLE_API_KEY`.

Online providers are chat-only. Embeddings and retrieval stay local — when the active chat provider is online, the indexer and vault chat resolve the embedding provider from the `embed_provider` config key (default `ollama`). Switching the chat provider therefore never triggers a re-index, and the user's notes never leave the machine in bulk.

The UI is the source of truth for `llm` and `embed`. Per-provider chat-model selections are persisted in their own fields (`openai_model`, `anthropic_model`, `google_model`) so toggling providers preserves each choice. Indexing and chat requests send the selected provider and model values explicitly.

OCR and vision are separate from the active chat provider. Each has its own provider and model selector, so you can use Ollama for chat with LM Studio for OCR, LM Studio for chat with Ollama OCR, or any other local combination supported by the loaded models. Online providers are NOT supported for OCR or vision — they run locally only.

### Online provider setup

1. Provide the relevant API key(s). ChatEKLD loads a `.env` file at startup from, in priority order, `~/Library/Application Support/ChatEKLD/.env` and then a `.env` beside `launch.py`. A real shell variable always wins over the file (`override=False`).

   **Dev (`python launch.py` from a terminal):** export in your shell profile, or drop a `.env` in the repo root.

   ```bash
   export OPENAI_API_KEY=sk-...
   export ANTHROPIC_API_KEY=sk-ant-...
   export GOOGLE_API_KEY=AIza...
   ```

   **Packaged `.app`:** a Finder-launched app does **not** inherit your shell environment, so keys in `~/.zshrc` are invisible to it. Copy `.env.example` to `~/Library/Application Support/ChatEKLD/.env` and put the keys there:

   ```bash
   cp .env.example ~/Library/Application\ Support/ChatEKLD/.env
   # then edit that file and uncomment your key(s)
   ```

2. Launch ChatEKLD. Pick OpenAI / Anthropic / Google from the provider dropdown. The chat-model dropdown will populate with the curated list for that provider.

3. Ask a question on the Obsidian tab. Indexing keeps running on Ollama / LM Studio; only the retrieved chunks plus your question reach the online provider.

API keys are never persisted to `config.json` and never returned by `/api/config`. Errors raised by the provider SDKs are run through a redactor before reaching logs or the UI.

### Fallback policy

Two config keys control fallback behaviour:

- `fallback_provider`: the provider name to retry against on transient errors (e.g. `"ollama"`). Empty disables fallback.
- `fallback_on`: list of categories that trigger fallback. Default: `["timeout", "network", "rate_limit", "server_error"]`. Non-transient errors (`auth`, `invalid_request`, `quota`) always surface immediately so a bad API key — or an exhausted quota / unpaid balance — is not silently swallowed. Hard quota/billing errors are detected (OpenAI `insufficient_quota`, Anthropic "credit balance", billing strings) and mapped to the terminal `quota` category, distinct from a transient `rate_limit`.

Example "primary online, fall back to local on rate-limit only":

```json
{
  "provider": "openai",
  "openai_model": "gpt-4o-mini",
  "fallback_provider": "ollama",
  "fallback_on": ["rate_limit"]
}
```

### Usage and cost tracking

Every online request records token counts and an estimated USD cost computed from the published per-million-token prices in `core/llm/usage.py`. The numbers persist to `BASE_DIR/llm_usage.jsonl` (see [Data](#data) for `BASE_DIR`) and are available via:

- `GET /api/usage?window=month` — aggregate totals plus a recent activity slice. Windows: `today`, `week`, `month`, `month_to_date`, `all`.
- `GET /api/pricing` — published prices used for the cost estimate.

You can override prices for a specific model via the `llm_pricing_overrides` config key:

```json
{
  "llm_pricing_overrides": {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60}
  }
}
```

Local requests are also tracked (cost defaults to 0) so the report endpoint shows a single combined view.

## Vault Indexing

The vault index is stored in `obsidian_storage/` under the app data directory. The indexer reads `.md` and `.pdf` files. `.docx` files are excluded by default. PDFs are folder-driven: every `.pdf` in an included folder is eligible for extraction and indexing. Images are note-driven: only image files whose extension is listed in `vault_image_exts` and that are referenced from an included markdown note are described by the configured vision model and embedded into the index. Image and PDF extraction caches live under `obsidian_cache/`, so reusable extraction work survives vector-index archives and version bumps.

`obsidian_vault_path` must be an existing non-system directory. Broad roots such as `/`, `/Users`, the home directory, and system folders are rejected before indexing. `vault_exclude_dirs` is a list of vault-relative folder paths. The UI folder picker stores relative paths and only accepts exclusion folders inside the configured vault.

The operation lock uses a heartbeat while indexing. This prevents the lock TTL from expiring during long indexing runs.

Index updates are incremental. Existing chunks are compared with LlamaIndex document hashes, unchanged chunks are skipped, changed chunks are replaced, and chunks for deleted vault files are removed.

PDF extraction during vault indexing is cached under `obsidian_cache/pdf_cache/`. Cache entries are split by vault and PDF SHA-256 hash, so unchanged PDFs reuse cached extracted text without loading or rewriting one large cache file.

Vault chat treats retrieved chunks as untrusted source text. The query prompt explicitly refuses instructions embedded inside notes or PDFs, applies a similarity cutoff (only when reranking is off — see below), and uses per-query LLM/embedding instances instead of mutating LlamaIndex global settings during retrieval.

The vault chat retrieval pipeline runs in three stages by default:

1. **Hybrid retrieval.** A BM25 (lexical) retriever and a dense (cosine) retriever each return their top candidates. The two result lists are fused with reciprocal-rank fusion. BM25 catches exact-match queries (drug names, gene symbols, acronyms, file titles) that dense embeddings miss; the dense leg catches semantic matches that share no tokens.
2. **Cross-encoder rerank.** The fused candidate pool (sized `min(max(top_k * 4, 20), 50)`) is scored by a cross-encoder model. Default: `cross-encoder/ms-marco-MiniLM-L-6-v2` (~67 MB, runs on CPU in ~80–200 ms / 30 candidates on Apple Silicon). The reranker narrows the pool down to `top_k` final chunks.
3. **LLM generation.** The final chunks are formatted into the chosen prompt mode and streamed through the active provider's chat model.

Both retrieval stages are togglable from config (`vault_hybrid_enabled`, `vault_reranker_enabled`) or per request body (`hybrid_enabled`, `reranker_enabled`). Toggling either is a query-time-only operation — no reindex is required. When the reranker is off, retrieval breadth equals `top_k` exactly (the pre-2026-05-14 behaviour). When the underlying optional package or model fails to load, the manager logs a one-shot warning and silently falls back to the dense-only path.

BM25 uses `llama-index-retrievers-bm25>=0.7,<0.8`, which is compatible with the LlamaIndex 0.14.x core used by this project. The BM25 index is built lazily in memory from the existing saved docstore on the first hybrid query and cached by chunk count, so enabling or updating BM25 does not require re-indexing the vault.

The cross-encoder model name is set via `vault_reranker_model` (config-only — `/api/obsidian/chat` does not accept it as a body override). On first chat the model downloads to `~/.cache/huggingface/hub/`; subsequent chats reuse the loaded model in memory.

When scanned PDF OCR exceeds a vision model context limit, GLM-OCR retries that page with a smaller aligned image before giving up.

Indexing also writes `obsidian_storage/indexed_materials.json`. The Obsidian tab reads this manifest to show each indexed note or PDF and its chunk count.

Markdown chunks record attachment references in their metadata. The chunker scans each note for Obsidian wikilinks (`![[image.png]]`, `[[note|alias]]`) and inline links (`[label](file.pdf)`), resolves each target relative to the note's directory, drops external URLs and anchors, and stores the resulting vault-relative paths in the `attachments` metadata field. Retrieval can join these paths back to indexed attachment chunks by matching the `{rel_path}::` prefix on doc_ids.

Indexing state survives an app restart. `obsidian_meta.json` records whether the last run completed, paused during scanning, or paused after vector chunks were written. The Obsidian tab only shows Resume for a queryable partial vector index; a pause before embedding preserves extraction caches but does not claim a usable partial index. Resuming with the same embedding model continues from the last vector checkpoint without re-embedding already-indexed chunks.

## Vault Chat

`POST /api/obsidian/chat` runs in one of two modes. The mode is selected by the `vault-agent-enabled` checkbox in the UI (persisted as `vault_agent_enabled`, default `false`) or by the `agent_enabled` body field on a single request.

### Simple RAG mode (default)

One retrieval pass feeds a single prompt. The model sees only the chunks that came back from that one query — it cannot issue further searches inside the turn. The full pipeline is hybrid retrieval → cross-encoder rerank → LLM generation (see [Vault Indexing](#vault-indexing) for the stage detail).

Per-request body fields override the persisted config keys; an invalid body value falls back to config, an invalid config value falls back to the engine default:

| Body field | Config key | Range / values | Effect |
| --- | --- | --- | --- |
| `top_k` | `vault_top_k` | 1-32 | Number of chunks reaching the LLM. |
| `similarity_cutoff` | `vault_similarity_cutoff` | 0.0-1.0 | Minimum retriever score. **Only applied when the reranker is off** — a cross-encoder score is on a different scale, so the cutoff would silently drop high-rerank chunks. The "retriever score" is dense cosine when `hybrid_enabled=false` and the reciprocal-rank-fusion score when `hybrid_enabled=true`, so the same numeric cutoff has different semantics across the two modes. |
| `prompt_mode` | `vault_prompt_mode` | `strict` / `balanced` / `exploratory` / `concise` | Selects one of four answer-mode templates (defined in `rag/engine.py`). `strict` says "I don't know" when context is thin; `balanced` marks unsupported parts of an answer; `exploratory` allows hedged inference; `concise` is strict-grounded but answers in ≤3 sentences / tight bullets with bracketed citations. |
| `temperature` | `vault_chat_temperature` | 0.0-2.0 | Sampling temperature. |
| `hybrid_enabled` | `vault_hybrid_enabled` | bool | BM25 + dense fusion. Query-time toggle, no reindex. |
| `reranker_enabled` | `vault_reranker_enabled` | bool | Cross-encoder rerank stage. Query-time toggle, no reindex. |
| `mmr_enabled` | `vault_mmr_enabled` | bool | MMR diversity on the dense leg — de-duplicates near-identical chunks. Query-time, no reindex. |
| `mmr_lambda` | `vault_mmr_lambda` | 0.0-1.0 | MMR threshold (higher = relevance, lower = diversity); applied only when `mmr_enabled`. |
| `query_expansion` | `vault_query_expansion` | bool | Rewrite the query into variants and RRF-fuse the results — better recall for vague questions. Adds an LLM call per turn; effective on local providers. |
| `num_queries` | `vault_num_queries` | 1-5 | Number of query variants when `query_expansion` is on. |
| `rerank_pool_ceiling` | `vault_rerank_pool_ceiling` | 10-200 | Max candidates the cross-encoder reranks down to `top_k`. Larger = better recall, slower. |
| `system_prompt` | `vault_chat_system_prompt` | ≤4000 chars | User-supplied behavioural instructions. On the **local** provider path it is layered as a prefix on top of the selected `prompt_mode` template. On the **online** provider path it is sent through the request's native `system_prompt` field, separate from the QA template. In both cases the selected `prompt_mode` template (safety preamble, untrusted-context guard, and `{context_str}` / `{query_str}` placeholders) stays app-controlled and unmodified, so a typo in the textarea cannot disable retrieval grounding. |

`vault_reranker_model` is config-only — it is not accepted as a body override, so a malicious page cannot point retrieval at an arbitrary HuggingFace repo.

### Agent mode

A ReAct loop runs the chat model with three tools registered. The model decides what to call and when to answer:

| Tool | Args | Output cap |
| --- | --- | --- |
| `vault.search` | `query`, optional `top_k` (1-12, default 6) | 12 000 chars; each snippet trimmed to 800 chars |
| `vault.read_note` | `rel_path` (vault-relative, forward slashes) | 32 000 chars |
| `vault.list_materials` | optional `filter` (substring on path), optional `limit` (1-200, default 100) | 20 000 chars |

The loop accepts `agent_max_iterations` per request (1-12, default `vault_agent_max_iterations` = 6) and a 300 s wall-clock cap shared with the simple-RAG path (`_CHAT_TOKEN_TIMEOUT_S` in `api/routes/vault.py`). When the iteration cap is hit without a final answer, the loop emits an info event and stops.

The `system_prompt` field is reused in agent mode. A fixed agent preamble — naming the tools and reinforcing the untrusted-tool-output guard — is always prepended to the system message; the user's text is appended after it. The preamble's text is therefore guaranteed to be present (the user cannot remove or rewrite it), but a sufficiently insistent user prompt could still contradict it in instructions to the model.

`prompt_mode` does not apply in agent mode — the answer-mode templates live on the simple-RAG path only.

**Fallback to simple RAG.** Two consecutive iterations where the model produces no parseable tool call (empty `STOP`, or `TOOL_USE` with zero parsed calls) trigger a clean re-run of `stream_chat` against the **original** user message — accumulated agent trace is not mixed back into a single-shot prompt. An info event announces the fallback.

**Capability warning.** After two consecutive turns in a session each end in fallback, a one-shot info event suggests switching models: "try a tool-capable model (e.g. Qwen 2.5, Llama 3.1+, Mistral Nemo, or any online provider)". The warning never repeats within a session; a single successful agent turn resets the counter.

**Provider capability.** All four providers report `supports_tool_use() = True`. For online providers (OpenAI / Anthropic / Google) the wire-level tool schema is honoured by the API. For the local provider (Ollama / LM Studio) the endpoint accepts the `tools=` parameter but **whether the configured model honours that schema is a runtime question** — small or non-tool-tuned local models will produce plain text instead of structured calls, which then triggers the fallback path above.

### Rule of thumb

These are recommendations, not enforced behaviour:

- Single fact, one topic, fast answer wanted → simple RAG with `prompt_mode: strict`.
- Open-ended synthesis from one search → simple RAG with `prompt_mode: exploratory` and reranker on.
- "Find then read", comparisons across notes, discovery of what's there → agent, with a tool-capable model.
- Local-only on a model that hasn't been tool-tuned → stay on simple RAG; agent mode will likely fall back anyway.

## Single Paper Summaries

Single Paper uses structured prompt presets:

- `concise`: document type, main findings, and main limits.
- `detailed`: document type, objective, methods, main findings, main limits, and key evidence.

The document type selector applies a specialized system prompt for systematic reviews, RCTs, observational studies, narrative reviews, opinion/letters, case reports, and guidelines. The request can also include a system prompt override, focus question, target audience, output language, temperature, context window, max tokens, top-p, and repeat penalty.

Uploaded PDF text is wrapped as untrusted source material before it is sent to the LLM. Uploads are streamed to a temporary file with a hard byte cap, and extraction runs in a subprocess so the timeout can terminate work rather than leaving a background extraction thread running.

## Library Audit

The Library Audit tab reconciles the configured Obsidian vault with Zotero, Better BibTeX, the local PDFs under the attachments directory, and macOS Finder tags. The engine is vendored from the `kb_harmonizer` project under `audit/` and is exposed through `/api/audit/*`.

**Manual only.** The audit is never triggered at startup, at config change, or by any other side effect. The only entry point that spawns a scan is `POST /api/audit/scan` — wired to the **Run Scan** button on the Library Audit tab. Until you click it, the tab shows an empty state and the manager reports `idle`.

**Read-only.** Every external store is opened read-only: the Zotero SQLite database is opened with `mode=ro` and immediately copied into an in-memory snapshot, PDFs are opened with pikepdf for annotation counting only (no `/Annots` decoding), BibTeX is parsed in place, Finder tags are read via the macOS `getxattr` xattr. The audit writes only one file: `BASE_DIR/audit/mapping.json`, used for manual PDF↔bib overrides that you record from the UI.

The six reports surfaced by the tab:

| Report | What it shows |
| --- | --- |
| **Inventory** | One row per BibTeX citation key, joining bib entry + Zotero parent + Obsidian note + resolved PDFs + Finder tags + annotation count. |
| **Tag Drift** | Zotero child-note tags that are missing from the corresponding Obsidian YAML. One-directional (Zotero → Obsidian). |
| **Unread PDFs** | PDFs under the biblio articles folder that the bridge could not resolve to a bib entry AND that have fewer annotations than the configured threshold. |
| **Zotero Queue** | Bib entries with a matching Zotero parent but no child note. Treats "child note exists" as the signal that a paper has been engaged with. |
| **Read PDFs Missing Zotero** | Unmapped PDFs ranked by annotation count. Suggested cutoff is informational only. |
| **Bib Entries Without PDFs** | Bib entries where no local PDF was resolved. Includes a Zotero-match indicator. |
| **Duplicate PDFs** | Content-identical PDF sets under the biblio articles folder, grouped by SHA-256. |

The Bridge (PDF ↔ bib citation key) resolves matches in priority order: `mapping.json` overrides, frontmatter PDF pointer fields (`pdf`, `attachments`, `file`, …), frontmatter author + year, wikilinks inside Zotero notes, and finally a filename author-year heuristic. Ambiguous matches are reported separately from unmapped PDFs.

### Diagnostic CLI

The same engine is reachable from the command line for development checks:

```bash
python -m audit --check inventory
python -m audit --check all
```

Available checks: `obsidian`, `zotero`, `zotero-debug`, `finder`, `bib`, `duplicates`, `duplicates-biblio`, `bridge`, `inventory`, `note-tag-drift`, `unread-unzoterod`, `zotero-unread`, `read-unzoterod`, `zotero-no-pdf`, `all`. The CLI reads the same `config.json` the Flask app uses, so vault and Zotero paths come from the same source.

## Deck Generator

The Deck Generator turns a **topic + free-form instructions** into a LaTeX **Beamer** lecture deck, grounded in the indexed Obsidian vault. It is **emit-only**: it writes a compile-ready `.tex` (and, in template mode, a `Makefile`) and you compile it yourself. It works with both local (Ollama / LM Studio) and online (OpenAI / Anthropic / Google) providers.

There are two front-ends to the same orchestrator (`deckgen/`):

- **In-app (Deck Generator tab).** Pick a Beamer template, optionally edit the preamble in-place, fill in topic / instructions / metadata, and generate. The in-app path drives the agent loop in-process over the live vault — it does not loop back over HTTP.
- **Standalone CLI** (`python -m deckgen …`). Drives the **running** app over its local HTTP API. Fully documented in [`deckgen/README.md`](deckgen/README.md).

### How it works

```text
1. preflight   confirm a vault index is ready (GET /api/obsidian/status + materials)
2. outline     one agent turn -> a JSON outline -> a list of sections
3. per section one bounded agent turn per section -> Beamer frames for that section
4. assemble    sanitize each section + wrap in a known-good (or your template's) preamble
5. validate    structural + safety checks, then write <out>.tex
```

Each section is its own bounded agent turn, so the model can `search` / `read_note` across the vault without exhausting a single whole-deck turn's iteration / wall-clock budget. The whole outline is passed into each section call to reduce drift and overlap.

> **Why a sanitize/assemble step?** ChatEKLD's `system_prompt` is a *prefix* over its own grounded answer template (the grounding + safety preamble always stays), so the model's output is not guaranteed to be pure Beamer. `deckgen` steers the model *and* strips/wraps the result itself.

### Template mode (house style)

Point the generator at your own template (`--template <path.tex>`, or pick it in the in-app window) and it:

- splits the template into preamble / opening scaffold / closing tail and **reuses your preamble verbatim** — document class, theme, packages, metadata — dropping the template's example sections and injecting the generated ones in their place;
- scans the preamble **and the local `.sty` files it `\usepackage`s** for custom macros, so house macros like `\citefoot{key}` and `\commonlogo[opts]{file}` are advertised to the model;
- resolves the bibliography from `\addbibresource{…}` so the model can emit real `\citefoot{key}` cites for a relevance-bounded candidate set (plain-prose `(source: note.md)` is the fallback). `validate` flags any cite key not found in the `.bib` for you to review;
- scaffolds `<out_dir>/<slug>/<slug>.tex` + a 2-line `Makefile` that `include`s your suite's build rules, so the deck drops straight into your suite.

### Compile safety

`deckgen` emits model-generated LaTeX built from **untrusted vault content**. `validate` warns if the output contains shell-escape / file-IO macros (`\write18`, `\input`, `\include`, `\openin`, `\read`, `\immediate`). Always compile **without** shell-escape (`latexmk -pdf …`, or `pdflatex -no-shell-escape …`) and review any such warning before building.

The deck route is **emit-only** and writes only under the chosen output directory. The exit-code / warning contract (clean / partial-placeholder / all-placeholder) and the full CLI flag set are documented in [`deckgen/README.md`](deckgen/README.md).

## Settings window

A single **LLM Settings** window (gear icon) centralises the configurable knobs that previously had no UI, alongside the provider/model and vault-chat controls:

- **Online plumbing** — `online_timeout_s`, `online_max_retries`, `online_max_tokens`, `fallback_provider`, `fallback_on`.
- **Timeouts** — `agent_wall_clock_s` (the per-agent-turn / per-deck-section cap) and `local_request_timeout_s` (per local HTTP call; `0` = each path's SDK default).
- **Retrieval** — `vault_reranker_model`, `vault_reranker_device` (`auto`/`cpu`/`mps`), and the vault-chat knobs.
- **Generation defaults** — the per-function `paper_*` and `deck_*` values.

Every value the window writes is range-/enum-validated server-side by `POST /api/config`; an out-of-range value is dropped (the prior persisted value survives) rather than stored. API keys are **never** written here — they come from environment variables only (see [Online provider setup](#online-provider-setup)).

## Config Keys

- `provider`: chat provider — `ollama`, `lm_studio`, `openai`, `anthropic`, or `google`.
- `llm`: selected chat model for local providers (Ollama / LM Studio).
- `openai_model` / `anthropic_model` / `google_model`: per-provider chat-model selection so switching providers does not lose a choice.
- `embed`: selected embedding model.
- `embed_provider`: local provider used for embeddings when `provider` is online. Default `ollama`.
- `online_timeout_s`: per-request timeout for online providers (default 60).
- `online_max_retries`: retry budget for transient errors (default 3, exponential backoff with jitter).
- `online_max_tokens`: output cap applied to online requests when the route does not set one (default 4096).
- `fallback_provider`: provider name to retry against on transient errors. Empty disables fallback.
- `fallback_on`: list of error categories that trigger fallback. Default `["timeout", "network", "rate_limit", "server_error"]`.
- `llm_pricing_overrides`: per-model `{input, output}` USD-per-1M-token overrides to keep cost estimates current when a provider re-prices.
- `context_window`: LlamaIndex context window for local providers.
- `ocr_provider`: provider used for scanned PDF OCR.
- `vision_provider`: provider used for image/figure description.
- `vault_exclude_dirs`: vault-relative folders to skip.
- `vault_image_exts`: image file extensions sent to the vision model and embedded.
- `obsidian_vault_path`: selected vault path.
- `ocr_model`: OCR model for scanned PDFs.
- `vision_model`: image/figure description model.
- `vault_top_k`: number of chunks retrieved per vault chat query (1-32, default 8).
- `vault_similarity_cutoff`: minimum cosine similarity for a retrieved chunk to reach the LLM (0.0-1.0, default 0.25).
- `vault_prompt_mode`: `strict` / `balanced` / `exploratory` / `concise` — controls how readily the LLM refuses vs. synthesises when context is thin, plus `concise` for short bulleted answers (default `strict`).
- `vault_chat_temperature`: sampling temperature for vault chat generation (0.0-2.0, default 0.3).
- `vault_hybrid_enabled`: enable BM25 + dense hybrid retrieval with RRF fusion (default `true`). Set `false` to fall back to dense-only retrieval.
- `vault_reranker_enabled`: enable cross-encoder reranking of the retrieved candidate pool (default `true`). Set `false` to skip the rerank stage.
- `vault_reranker_model`: HuggingFace model id for the cross-encoder reranker (default `cross-encoder/ms-marco-MiniLM-L-6-v2`). Config-only — not accepted as a per-request override.
- `vault_mmr_enabled`: enable MMR diversity on the dense retriever to de-duplicate near-identical chunks (default `false`). Query-time, no reindex.
- `vault_mmr_lambda`: MMR threshold 0.0-1.0 — higher favours relevance, lower favours diversity (default 0.5); applied only when `vault_mmr_enabled`.
- `vault_query_expansion`: enable multi-query expansion — the fusion retriever rewrites the query into variants and RRF-fuses the results (default `false`). Adds an LLM call per turn; effective on local providers.
- `vault_num_queries`: number of query variants when expansion is on (1-5, default 3).
- `vault_rerank_pool_multiplier` / `vault_rerank_pool_floor` / `vault_rerank_pool_ceiling`: candidate-pool sizing for the rerank stage, `min(max(top_k * multiplier, floor), ceiling)` (defaults 4 / 20 / 50). The ceiling is also a per-request override (`rerank_pool_ceiling`, 10-200).
- `vault_vector_backend`: vector store for **new** index builds — `simple` (legacy JSON, default) or `lancedb` (binary Apache-Arrow). Existing indexes keep whatever backend is recorded in `obsidian_meta.json`. See "Vector store backend (LanceDB)" above to migrate an existing vault with no re-embedding. Resolves to `simple` if lancedb is not installed.
- `audit_attachments_subdir`: vault-relative folder containing all PDF attachments (default `Z_attachments`). Read only when an audit scan runs.
- `audit_biblio_articles_subdir`: subfolder under the attachments dir that holds bibliography PDFs (default `biblio_articles`).
- `audit_zotero_notes_subdir`: vault-relative folder that holds one `<bbtkey>.md` file per Zotero parent (default `Z_Zotero_Notes`).
- `audit_master_bib_path`: vault-relative path to the Better BibTeX master export (default `presentations_slides_writings_teaching/_master.bib`).
- `audit_zotero_sqlite`: absolute path to `zotero.sqlite` (default `~/Zotero/zotero.sqlite`). Opened read-only and immediately snapshotted into memory.
- `audit_zotero_storage`: absolute path to the Zotero storage directory (default `~/Zotero/storage`).
- `audit_annotations_read_threshold`: annotations count above which a PDF is considered "read" by the Unread report (default `5`).
- `audit_biblio_skip_prefix`: filename prefix for biblio PDFs that should be skipped by every report except Duplicates (default `z_item`).

Vault chat (additional):

- `vault_chat_system_prompt`: persisted vault-chat `system_prompt` prefix (≤4000 chars; the safety/grounding preamble stays app-controlled).
- `vault_reranker_device`: cross-encoder device — `auto` / `cpu` / `mps` (default `auto`). Any non-CPU failure degrades to CPU for the session.
- `vault_prewarm_enabled`: prewarm the retrieval stack at launch (default `true`). When off, the first chat lazy-loads instead.
- `vault_agent_enabled`: route `/api/obsidian/chat` through the ReAct agent loop instead of single-shot RAG (default `false`; also a per-request `agent_enabled` body field).
- `vault_agent_max_iterations`: agent loop budget (default 6, clamped 1–12; also a per-request `agent_max_iterations` body field).

Timeouts:

- `agent_wall_clock_s`: per-agent-turn and per-deck-section wall-clock cap in seconds (default 300). Outer SSE / frontend timeouts derive from this but are floored at 300 s.
- `local_request_timeout_s`: per local HTTP call timeout for chat/generation, in seconds (default `0` = leave each path's SDK default). Embeddings are intentionally excluded.

Per-function generation defaults (set in the Settings window; body values override, then these, then the hard clamp):

- `paper_temperature` / `paper_num_ctx` / `paper_max_tokens` / `paper_top_p` / `paper_repeat_penalty`: Single-Paper generation defaults (defaults `0.3` / `32768` / `4096` / `0.9` / `1.1`).
- `deck_temperature` / `deck_max_sections` / `deck_agent_max_iterations`: Deck Generator defaults (defaults `0.3` / `8` / `6`).

## API

Most routes require `X-Requested-With: ChatEKLD`.

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/status` | GET | Provider status and warnings. |
| `/api/models` | GET | Models from the active provider. |
| `/api/vision-models` | GET | Provider-scoped model list for OCR and vision settings. |
| `/api/config` | GET/POST | Read or update config. |
| `/api/report-types` | GET | List report types. |
| `/api/pull` | POST | Pull an Ollama model. Disabled for LM Studio. |
| `/api/upload` | POST | Upload and extract one PDF. |
| `/api/upload/<upload_id>` | DELETE | Delete a stored upload row. |
| `/api/summarise` | POST | Stream summary tokens as SSE. |
| `/api/export-summary` | POST | Export a summary as `.txt` or `.md`. |
| `/api/feedback` | POST | Save a feedback record. |
| `/api/feedback/history` | GET | Read recorded feedback. |
| `/api/obsidian/index` | POST | Start vault indexing. |
| `/api/obsidian/status` | GET | Poll indexing state, messages, and warnings. |
| `/api/obsidian/materials` | GET | List files recorded in the current vault index manifest. |
| `/api/obsidian/chat` | POST | Stream vault RAG tokens as SSE. |
| `/api/obsidian/pause` | POST | Request indexing pause (resumable). |
| `/api/obsidian/cancel` | POST | Request indexing cancellation and release the operation lock. |
| `/api/native-pick-folder` | POST | Open a native folder picker. |
| `/api/reset` | POST | Clear uploads and the Obsidian index. Can also remove config and feedback when requested. |
| `/api/log` | POST | Receive a frontend error log entry (capped at 500 chars). |
| `/api/about` | GET | Build metadata and version info. |
| `/api/usage` | GET | Token / cost totals + recent activity. Window: `today`, `week`, `month` (default), `month_to_date`, `all`. |
| `/api/pricing` | GET | Published USD-per-1M-token prices used for cost estimates. |
| `/api/audit/config` | GET/POST | Read or update Library Audit settings (subpaths, Zotero paths, thresholds). |
| `/api/audit/status` | GET | Audit scan state (`idle / scanning / done / error / cancelled`), progress messages, and result flags. |
| `/api/audit/scan` | POST | Start a Library Audit scan in the background. The only endpoint that triggers a scan. |
| `/api/audit/cancel` | POST | Signal the in-flight scan to abort cooperatively. |
| `/api/audit/inventory` | GET | Cached inventory rows from the last scan (404 until a scan completes). |
| `/api/audit/reports/<name>` | GET | One of `note_tag_drift`, `unread_unzoterod`, `zotero_unread`, `read_unzoterod`, `zotero_no_pdf`, `duplicates`. |
| `/api/audit/mapping` | POST | Record a manual PDF↔bib override or "confirmed no match" in `mapping.json`. |
| `/api/audit/reveal` | POST | macOS-only: reveal a file in Finder, open it, or open a Zotero item by key. Paths are bounded to the configured roots. |
| `/api/deck/load-template` | POST | Validate a Beamer template path and scan it → `{tex, macros, bib_keys_count, suite_root}`. |
| `/api/deck/generate` | POST | Generate a vault-grounded deck (SSE: reuses the `info`/`error`/agent-trace contract, then a terminal `{"deck": {tex, warnings, tex_path, make_hint, …}}` frame). Emit-only. |
| `/api/deck/native-pick-file` | POST | Native file picker (template selection). |
| `/api/deck/native-pick-folder` | POST | Native folder picker (output directory). |

SSE routes send JSON token events, stream JSON error events when generation fails, and end with `data: [DONE]`. Agent-mode `/api/obsidian/chat` and `/api/deck/generate` additionally emit `iteration`, `thought`, `tool_call`, and `tool_result` events before the answer stream.

## Limits

- PDF upload limit: 500 MB.
- PDF extraction timeout: 600 seconds.
- System prompt limit: 4000 characters.
- Feedback string-field cap: 10 000 characters per field.
- Obsidian operation-lock TTL: 3600 seconds per acquisition (refreshed by heartbeat during long runs).
- PDF extraction call cap: 1000 pages per call (`EXTRACT_MAX_PAGES_PER_CALL`). Vault indexing automatically splits larger PDFs into 1000-page extraction chunks and concatenates the result.
- Vault PDF size cap: PDFs above 5000 pages are skipped with a warning rather than risking out-of-memory during embedding.
- Vault image size cap: 20 MB per file. Larger images are skipped with a warning.
- Vault image extension list cap: 64 entries in `vault_image_exts`.
- Consecutive-failure circuit breaker: 20 consecutive insert failures abort an indexing run with the partial work preserved.
- Mid-run checkpoint: vault indexing persists every 500 inserts, bounding crash re-work to that many chunks. LlamaIndex checkpoint files are first written to a temporary directory and JSON-validated before they replace the active vector/docstore files, so an interrupted checkpoint should not truncate the last usable index.
- Agent loop budget: `vault_agent_max_iterations` (default 6, clamped 1–12). Two consecutive malformed tool-call iterations fall back to plain RAG.
- Agent tool-output caps: `vault.search` 12 000 chars (800 / snippet), `vault.read_note` 32 000 chars, `vault.list_materials` 20 000 chars.
- Wall-clock cap: `agent_wall_clock_s` (default 300 s) bounds each agent turn and each deck section. The timeout chain is nested — `local_request_timeout_s` ≤ `agent_wall_clock_s` ≤ the SSE consumer's stall guard ≤ the frontend fetch abort — so an inner cap is never defeated by an outer one.

## Data

App data is stored under the platform app data directory.

- `config.json`: user settings.
- `uploads.db`: extracted PDF text.
- `feedback.jsonl`: feedback records.
- `llm_usage.jsonl`: append-only token/cost log used by `/api/usage`.
- `obsidian_storage/`: persisted vector index plus `indexed_materials.json`, per-PDF cache files under `pdf_cache/`, and per-image description cache files under `image_cache/`.
- `scripts/repair_simple_vector_store.py` and `scripts/prune_storage_to_vector_store.py`: maintenance-only tools for recovering complete embeddings from a truncated SimpleVectorStore JSON and pruning LlamaIndex metadata to the recovered vector IDs. They are not part of normal indexing; use them only after backing up app data.

## Troubleshooting

- **"OPENAI_API_KEY is not set" (or ANTHROPIC / GOOGLE) on /api/status.** The environment variable was not exported before launching the app. macOS GUI launches do not pick up shell `~/.zshrc` exports; either add the keys to `~/Library/LaunchAgents/` via `launchctl setenv`, source them in a wrapper script, or pin them in a `.env` you `source` before `python launch.py`.
- **`rate_limit` on the online provider.** Lower the request rate, switch to a higher-tier model with a larger quota, or set `fallback_provider: "ollama"` plus `fallback_on: ["rate_limit"]` to spill over to the local provider on the rare hard limit.
- **"Embedding model mismatch" warning after switching to an online provider.** Online providers do not provide embeddings — the warning is from the indexed embedding model versus the currently selected local embed model. Re-select the matching local embedding model under `embed`.
- **No tokens appearing on the Obsidian tab when chat is set to an online provider.** Confirm `obsidian_storage/docstore.json` exists (i.e. you have indexed at least once with a local embedding model). Online chat reuses the local index; an empty index returns "No relevant content found" by design.
- **Cost numbers look wrong.** Check `core/llm/usage.py` for the model's listed prices; the published rate can change between releases. Add an override under `llm_pricing_overrides` if you need to refresh it without waiting for an app update.

## Vector store backend (LanceDB)

By default the vault index uses LlamaIndex's JSON `SimpleVectorStore`. An optional binary **LanceDB** backend stores embeddings as float32 in Apache-Arrow files, cutting resident RAM and the GIL-held cold-start parse on large vaults. It is opt-in:

- **Migrate an existing index with no re-embedding** (recommended): close the app, then `python scripts/migrate_vector_store.py`. It joins the existing vectors to their nodes and bulk-loads LanceDB, archives the old JSON to `default__vector_store.json.bak`, and records the backend in `obsidian_meta.json`. A subsequent reindex reports `added=0` (nothing re-embedded).
- **Or build fresh on LanceDB**: set `"vault_vector_backend": "lancedb"` in `config.json` and reindex.

Retrieval results match the JSON backend (embeddings are unit-normalized so LanceDB's L2 search ranks identically to cosine). Requires `lancedb` + `llama-index-vector-stores-lancedb` (in `requirements.txt`); falls back to `simple` if absent.

## License

MIT.

