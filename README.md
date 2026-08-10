# ChatEKLD 2026

A project I vibe-coded through iterative sessions for the last couple months, on my free time.

The idea is to have local (or online) LLMs help me retrieve knowledge from Obsidian .md notes or PDFs or images in my vault, generate first draft of presentations on various topics using indexed informations.

Indexing my vault took a lot of time because I had >6000 PDFs but it was mainly done over night and worth it. Of course online LLMs appear way better than local LLMs I use on my Macbook Pro M4 with 16 GB of RAM, but using offline local LLms is still pretty nice !

Some other functions that are more for my personal use, e.g.,
- the possibility to see how many PDFs I have in my vault, how many are actually referenced in my Zotero and for how many I have written up a small note. This is nice because it allows me to learn again that for every article that I (try to) read, I download 150 that I think I will read "later".
- the possibility to augment some of my Obsidian Notes with an LLM pass or to OCR some images added to such obsidian notes and add the content in it

Don't hesitate to reach out if you have suggestions or appreciate the project. A few screenshots are below.

Below is a more detailed and technical presentation, obviously not written by me but by a friendly LLM.

---

ChatEKLD is a local Flask and PyWebView application for PDF summarisation, Obsidian vault RAG, library reconciliation, and grounded LaTeX slide generation. It runs offline by default with Ollama or LM Studio, and can optionally route chat through OpenAI, Anthropic, Google Gemini, DeepSeek, Z.AI (GLM), or OpenRouter while keeping the index — and your notes — on-device.

> **ChatEKLD writes to your Obsidian vault only when you explicitly ask it to — through Note Refactor, the Report / Article Generator's "Markdown note" action, or the Library Audit's tag writes (the per-note "apply missing Zotero tags" and the Tagging sub-view's note-frontmatter, Finder-tag and `_tags.md` vocabulary writes).** Every other workflow is read-only: it *reads* your `.md` / `.pdf` files to build an on-device index — your notes are never modified, moved, or deleted. The app's own index, config, and logs live under `~/Library/Application Support/ChatEKLD/`; the Library Audit additionally keeps its curation sidecars there (`audit/mapping.json` — your manual PDF↔citation overrides, hash-anchored so a renamed/moved PDF re-links at the next scan — and `audit/dup_decisions.json` for dismissed duplicate candidates), the optional desktop widgets read a `widget.json` snapshot written there after each scan, and the Deck Generator and the Report / Article Generator's LaTeX mode emit `.tex` files (plus, on Preview/Compile-fix, the usual LaTeX build artifacts) to an output folder you choose.
>
> **Note Refactor's Apply / Fix formatting / Archive / LLM edits, the Report / Article Generator's "Markdown note", plus the Library Audit's per-note "apply missing tags" and the Tagging sub-view's writers — are the exceptions** (opt-in, off by default, and never run without an explicit per-action confirmation). The **Markdown note** action writes a NEW long-form note (it never overwrites an existing one) into a vault sub-folder you choose, journalled and reversible from Note Refactor → Restore (which deletes the created note). **Apply** inlines the approved image-description callouts into your notes; **Fix formatting** applies a deterministic, idempotent Markdown normalization (blank lines around headings/lists/code fences, trailing-whitespace strip, blank-run collapse, single final newline, CRLF→LF); **Archive** moves a chosen full-res attachment *out* of the vault to a recoverable archive folder (default under the app data dir) and leaves a small thumbnail; **LLM edits** write an LLM-generated result you previewed and approved — an applyable formatting rewrite, a per-PDF summary callout, or a free-form instruction — over the whole note or a chosen heading section (the generated body is staged server-side, so what you approve is exactly what is written). Every change is written atomically, audit-logged, snapshotted, and reversible from the **Restore** dialog. Previewing (Run Plan), re-extraction, classification, advisory prose review, and Mermaid-diagram generation remain read-only.

## Screenshots

<!-- Vault paths and the deck author name are masked in these images. New
     screenshots go in screenshots/ (root level — NOT docs/, which is stripped
     from the public mirror) and are published permanently in git history:
     check each one against the redaction checklist in PUBLISHING.md first. -->

**Obsidian Agent** — asking the vault a question in agent mode. The model calls
`vault_search`, and every tool result is wrapped in an explicit untrusted-source
guard before it reaches the prompt.

![Obsidian Agent — agent mode with a vault_search tool call and the untrusted-source wrapper](screenshots/obsidian-agent.png)

| | |
|:--|:--|
| ![Library Audit — scan summary tiles and the report tabs](screenshots/library-audit.png)<br>**Library Audit** — read-only reconciliation of the vault against Zotero, local PDFs and Finder tags. The tiles are the honest ones: 6111 records, 967 PDFs with no Zotero entry. | ![Deck Generator — a compiled Beamer title slide previewed in-app](screenshots/deck-generator.png)<br>**Deck Generator** — a vault-grounded Beamer deck in your own template's house style, compiled and previewed in-app. Emit-only by default. |
| ![Single Paper — a streamed structured summary of one PDF](screenshots/single-paper.png)<br>**Single Paper** — one PDF in, a tunable streamed summary out; the trailing bibliography is stripped before summarising. | ![Help tab — the in-app user guide](screenshots/help-guide.png)<br>**Help** — the condensed guide rendered in-app, with "?" badges on individual controls and a ⌘K command palette. |

## Workflows

ChatEKLD is organised into self-contained workflow tabs (plus Help and the read-only Prompt Hub):

1. **Single Paper** — upload one PDF, extract its text, and stream a tunable summary with prompt presets, document-type prompts, audience, focus question, and generation controls.
2. **Obsidian Vault** — index `.md` and `.pdf` files, then query them through dense retrieval, optional BM25 lexical retrieval, RRF fusion, and an optional cross-encoder reranker — in single-shot RAG or an opt-in ReAct agent mode. Inspect the indexed-material manifest from the UI.
3. **Library Audit** — a **manual-only** reconciliation of the vault against Zotero (SQLite snapshot + Better BibTeX `_master.bib`), local PDFs (annotations + duplicates), and macOS Finder tags. A set of report tabs plus a per-citation-key inventory. Read-only except explicit, confirm-gated actions: manual PDF↔bib mapping (with an ambiguous-PDF picker and triage deck), "not a duplicate" dismissals on the Possible Duplicates report, the "apply missing Zotero tags to the Obsidian note" write (`.bak`-backed), and the Finder Tag Drift fix verbs (which dispatch to the Tagging sub-view's journalled, undoable writers). That **Tagging** sub-view (see below) turns the same per-key join into a confirm-gated, reversible tag-writer for `biblio_articles/` papers.
   - **Biblio Tagging** (a sub-view of the Library Audit tab) — for each paper under `biblio_articles/`, **Propose** tags with an LLM constrained to your `_tags.md` vocabulary (from the PDF's cached text), edit with a vocabulary type-ahead, then apply to any of three targets: the Obsidian Zotero note's frontmatter, the PDF's macOS **Finder tags**, and the parent **Zotero item** (via Zotero's HTTP API, local or zotero.org — never the SQLite file, so it is sync-safe). Every write is confirm-gated, validated against the controlled vocabulary server-side, audit-logged, and reversible from an in-panel **Restore**.
4. **Deck Generator** — turn a topic + free-form instructions into a LaTeX **Beamer** deck grounded in the indexed vault, in your own template's house style. Emit-only: it writes a compile-ready `.tex` + `Makefile`; you compile with `make` — or click **Preview PDF** (one plain latexmk pass, never touches the `.tex`) to see the compiled deck in-app, and the opt-in **Compile & Auto-Fix** for LLM-assisted log repair.
5. **Report / Article Generator** — the prose sibling of the Deck Generator: turn a topic + instructions into a LaTeX **article/report** (`\documentclass{article}`, prose sections — not slides) grounded in the vault, in your own template's house style (emit-only, scaffolds `<slug>/<slug>.tex` + `Makefile`), **or** a long-form **Markdown note written into your vault** (a new note under a sub-folder you choose — never overwriting an existing one — journalled and reversible from Note Refactor → Restore).
6. **Note Refactor** — an analyzer **plus the app's primary vault writer** (the Report / Article Generator's Markdown-note write reuses its journal/Restore), scoped to one sub-folder. *Read-only review:* reuse the indexer's cached image descriptions to preview an annotated version of each note, flag broken embeds / frontmatter smells, report advisory cross-note dose discrepancies, review ORIGINAL vs PROPOSED side-by-side, re-extract/classify a single image, and mark handwritten scans to ignore. *Opt-in writes (each explicitly confirmed):* **Apply** the approved callouts into the notes (callout-only — original embeds kept), **Fix formatting** (deterministic Markdown normalization), **LLM edits** (an applyable formatting rewrite, a per-PDF summary callout, or a free-prompt instruction — over the whole note or a chosen heading section), **Archive** a chosen attachment out of the vault (vault-wide reference check first; full-res moved to a recoverable archive folder, a thumbnail left behind in an excluded `_thumbs/` folder), and **Restore** to reverse any change. There are also two **advisory** on-demand LLM actions that write nothing: a prose review and a Mermaid-diagram generator. Every write is atomic, audit-logged to `chatekld.log`, and recorded in a restore manifest.
7. **Plain Chat** — a RAG-free, multi-turn conversation with the globally-configured provider/model: no vault retrieval, no agent loop, no tools. The server is stateless — the browser owns the `{role, content}` history and sends only the last 20 turns on each send, so a conversation is ephemeral (lost on reload) — export it to `~/Downloads` as Markdown/text anytime (the vault chat has the same Export buttons, with per-answer retrieval sources included). Streams through the same unified chat layer as the rest of the app, for both local and online providers.

## Features

**Single Paper**
- Upload one PDF and generate a streamed summary; export it as `.txt` or `.md`.
- Tune with concise/detailed presets, a document-type system prompt (systematic review, RCT, observational, narrative review, opinion/letter, case report, guideline), audience, focus question, output language, and a `system_prompt` override.
- Generation knobs (temperature, context window, max tokens, top-p, repeat penalty) resolve from request body → persisted `paper_*` defaults → hard clamp.

**Obsidian Vault** (UI tab: **Obsidian Agent**)
- Index an Obsidian vault from `.md` and `.pdf` files (`.docx` excluded by design); configure which image extensions are described-and-embedded.
- Incremental, resumable, checkpointed indexing; choose the `simple` (JSON) or `lancedb` (binary Apache-Arrow) vector backend.
- Hybrid (BM25 + dense, reciprocal-rank-fusion) retrieval → cross-encoder rerank → LLM generation, with optional MMR diversity and multi-query expansion. Every retrieval knob is a query-time override — no reindex.
- Opt-in **agent mode**: a ReAct loop with `vault_search`, `vault_read_note`, and `vault_list_materials` tools, with a clean fallback to single-shot RAG — and, opt-in on top of it, a **`web_search` tool** (online chat providers only, Tavily-backed, `TAVILY_API_KEY` env; see [Agent mode](#agent-mode)).
- Opt-in **vault thesaurus** (default off): if you keep curated `_abreviations.md` / `_tags.md` glossary tables at your vault root, enable synonym-based query expansion and/or a small glossary "primer" injected into the system prompt so the model can read your vault's shorthand. Query-time, no reindex. See [Vault thesaurus](#vault-thesaurus).
- View the notes and PDFs recorded in the current index; clear the chat window client-side (the server holds no chat state).

**Library Audit**
- Reconcile the vault against Zotero, Better BibTeX, local PDFs, and macOS Finder tags. **Manual only** — never runs at startup, only when you click **Run Scan** — and read-only apart from explicit, confirmed curation/fix actions (PDF↔bib mapping, duplicate dismissals, the two tag-drift fix surfaces).

**Biblio Tagging** (a Library Audit sub-view · write only on explicit confirm)
- Tag `biblio_articles/` papers across three targets from one place: the Obsidian Zotero note frontmatter, the PDF's macOS **Finder tags**, and the parent **Zotero item** (through Zotero's HTTP API — never SQLite). **Delete** any current tag from one store with its × (confirmed, journalled, undoable), or **retire a tag everywhere** from the Tag Hygiene report — one confirm, batch-journalled, one-click undo, optionally pruning the `_tags.md` row too. Work through them in a keyboard-first **Triage deck** (one card at a time, untagged first, with a PDF excerpt — <kbd>P</kbd> propose, <kbd>A</kbd> accept → all targets at once, <kbd>S</kbd> skip, <kbd>M</kbd> defer, <kbd>V</kbd> full-PDF panel, <kbd>⌫</kbd> undo) or the side-by-side **Table**. **Propose** tags with an LLM constrained to your `_tags.md` vocabulary (reading the PDF's already-cached text), edit with a vocabulary type-ahead, and **Restore** any write from an in-panel manifest. Every write is confirm-gated, controlled-vocabulary-validated server-side, `log_vault_write`-audited, and reversible. Needs a completed audit scan.

**Deck Generator**
- Generate a vault-grounded Beamer deck from the in-app **Deck Generator** window or the standalone `deckgen` CLI. Reuse your own Beamer template's preamble, custom macros (`\citefoot`, `\commonlogo`) and `_master.bib`; scaffold a compile-ready `<slug>/<slug>.tex` + `Makefile`. Emit-only.

**Report / Article Generator**
- Generate a vault-grounded LaTeX **article/report** (prose, not slides) in your own template's house style — same engine as the deck (outline → sections, per-section retries, salvage, checkpoint/resume), scaffolded emit-only as `<slug>/<slug>.tex` + `Makefile`. **Preview PDF** compiles it in-app (never touches the `.tex`); the opt-in **Compile & Auto-Fix** runs a bounded LLM log-repair loop (each repair verified by a recompile, `.tex.bak` first). **Augment** an existing article — deepen a section, add a table, insert a new section, or reformat — grounded in your vault, previewed and confirmed before it overwrites (byte-identical outside the edited span). A document too large to revise in one pass offers **Revise section by section** (one pass per `\section`, recombined into a single preview); the deck's integrity review likewise offers **Try section-by-section repair** when the whole-deck repair exceeds its budget. In every piecewise pass, a section that is itself over-cap or fails is skipped and left unchanged — a visible caveat beside the offer says so.
- Or generate a long-form **Markdown note written into your vault** (`# Title` + `##` sections), grounded in what your notes say — a new note under a sub-folder you choose, never overwriting an existing one, journalled and reversible from **Note Refactor → Restore**.

**Note Refactor** (analyze read-only · write only on explicit confirm)
- Analyze one vault sub-folder: resolve each note's image embeds (Obsidian shortest-path), reuse the indexer's cached descriptions to inline a preview callout, and compute a unified diff — **the plan makes zero vision calls and zero vault writes**.
- Pick the sub-folder by hand or with a native folder picker; browse notes in a sidebar and review ORIGINAL vs PROPOSED rendered markdown alongside the diff.
- On demand (one image at a time), re-extract a markdown table (no-downscale double-read for accuracy), re-describe, or classify the image (printed-table｜figure-diagram｜handwritten｜photo｜other); a sticky per-vault ignore-list lets you set aside handwritten scans the local model can't OCR.
- **Apply** (opt-in, confirmed): write the approved callout-only proposals into the selected notes — original embeds are kept. A stale-diff guard (the note must be unchanged since the plan) and a WYSIWYG guard (the recomputed body must match what you previewed) protect every atomic write. An optional scope-wide "strip OCR preamble" default (`refactor_strip_preamble_default`, off) drops the descriptive preamble from every callout (additive to the per-image `strip` flag, read identically by the plan and the writer so the WYSIWYG guard holds).
- **Fix formatting** (opt-in, confirmed, **per-note**): apply a deterministic, idempotent Markdown normalization (blank lines around headings/lists/code fences, trailing-whitespace strip outside code, blank-run collapse, single final newline, CRLF→LF). You tick each note's approve checkbox (default off — not select-all); independent of Apply, under the same stale-diff + WYSIWYG guards.
- **Per-image OCR control**: a collapsible *Images — inclure l'OCR* panel (above the preview) lists every attached image of the note with an include checkbox (plus *Tout inclure* / *Tout exclure*), so you decide image-by-image — one, several, or not all — whether its OCR callout is inlined. Handwritten scans are includable too (forces `keep_handwritten`). Toggling persists to the ignore-list/flag and re-analyzes just that note so the preview + Apply hashes refresh immediately (no full re-plan).
- **Piecewise edits (2026-07-22):** a note too large for one whole-note LLM pass offers **Edit section by section** at the moment of refusal — one bounded pass per heading section (over-cap sections skipped with a reason), recombined into a single previewed diff and applied through the same confirmed, journalled path.
- **On-demand LLM actions** (per note, optional **heading-section** scope): **Improve formatting** (an LLM rewrite — bullets, line breaks, punctuation), **Summarise a PDF** (5–10 bullets from the attached PDF's cached text, inlined as a `> [!summary]` callout), and a **free-prompt** box (your own instruction — reformulate, shorten, turn a list into a table…). Each shows a preview diff, then *Approve & apply* writes it (staged server-side → stale-diff + WYSIWYG guards, reversible). Two **advisory** actions write nothing: **Generate diagram** (a Mermaid block to copy) and **Review prose** (suggestions). Model = `refactor_review_model` → chat model; the rewrite/free-prompt token cap is `refactor_rewrite_max_tokens`.
- **Archive** (opt-in, confirmed, per image): a vault-wide reference check refuses a shared image; otherwise the full-res original moves out of the vault to a recoverable archive folder (default under the app data dir — local disk, not iCloud — overridable via `refactor_archive_dir`), a ~384px PNG thumbnail is written into an excluded `<scope>/_thumbs/` folder, and that embed is swapped to the thumbnail.
- **Restore** reverses any apply / fix-formatting / LLM edit / archive from a per-vault restore manifest; every mutation is also audit-logged to `chatekld.log`.

**Desktop widgets (2026-08-06)**
- Two optional companions surface a **paper of the day** (with a summary-callout or first-page excerpt), one completable suggestion ("Tag 10 papers"), drift/unread counts and a **notion of the day** from your `_tags.md` glosses: a **SwiftBar** menu-bar plugin and an **Übersicht** desktop card, both in [`packaging/widgets/`](packaging/widgets/) (install steps in its README; also staged into the `.dmg`). They read a small snapshot (`widget.json` in the app data dir) the app writes at the end of each **manual** audit scan and on the Audit tab's **Refresh widget** button — nothing scans in the background, the widgets work with the app closed, and each shows the snapshot's age.

**Exports & provenance (2026-07-22)**
- Every MD/TXT export — vault chat, Plain Chat, single-paper summary — is stamped with the provider/model that answered, per-answer token/cost figures (best-effort, from the usage tracker), retrieval sources with scores, and the agent's tool-call list. Generated LaTeX carries a provenance comment header (articles add an optional commented-out ICMJE-style AI-disclosure paragraph); Markdown notes written into the vault carry provenance frontmatter.

**Help & discoverability (2026-07-22)**
- An in-app **Help** tab renders the condensed user guide; "?" badges cover the vault-chat retrieval knobs, the deck generation knobs and the main provider/timeout settings; a **guided tour** ("Show me around") walks the main views; **⌘K** opens a command palette (navigation + safe actions). Both the tour and the palette are fully keyboard- and screen-reader-operable: Escape closes them from anywhere, focus returns to where you started, each tour step is announced, and the palette's arrow-key selection is a proper combobox.

**Plain Chat**
- A RAG-free, multi-turn conversation with the active chat provider/model — no vault retrieval, no agent loop, no tools, no per-panel model selector (it follows the global Provider/Model setting).
- Stateless server: the browser keeps the `{role, content}` history and sends only the last 20 turns on every send; the conversation is ephemeral and is lost on reload or with **New chat**.
- Two persisted knobs in the LLM Settings window: `chat_temperature` and `chat_system_prompt`. Unlike vault chat, the system prompt is the **full** system prompt — there is no retrieval grounding to protect, so the whole prompt is yours.
- Streams through the unified `core.llm.chat.stream_chat_messages` helper for both local and online providers, falling back only before the first streamed token.

**Providers & operations**
- Select the active chat provider, chat model, and embedding model in the UI; separate selectors for the OCR and vision providers/models (scanned-PDF fallback + image/figure reading).
- Local providers: Ollama and LM Studio (run without Ollama when LM Studio is active). Online chat-only providers: OpenAI, Anthropic, Google Gemini, DeepSeek, Z.AI (GLM), OpenRouter — embeddings and retrieval stay on-device, so only the query and retrieved chunks ever reach the cloud.
- Configurable fail-fast / hybrid fallback policy (e.g. fall back from OpenAI to Ollama on rate-limit only).
- Track token usage and estimated USD cost per request via `/api/usage` and the on-disk `llm_usage.jsonl`.
- A unified **LLM Settings** window centralises the provider/model, vault-chat, OCR/vision, generation-default, and timeout/fallback knobs; all settings persist to `config.json`.

## Prerequisites

ChatEKLD runs on **Apple Silicon (arm64) macOS**. Intel Macs are **not supported**: the build is arm64-only (the native dependency set — PyTorch, LanceDB, PyArrow, tokenizers — is not reliably cross-buildable to a universal or Intel bundle, and Rosetta only translates Intel→Apple-Silicon, never the reverse). The installer refuses to build on a non-arm64 host. The installer offers to set up anything else missing, but it helps to have these ready:

- **An Apple Silicon Mac** (M1 or later) with the Xcode Command Line Tools (`xcode-select --install`). Homebrew is installed automatically by `install_and_build.sh` if it is absent.
- **Python 3.12** specifically. The installer runs `brew install python@3.12` when 3.12 is not already on the `PATH`, and recreates the venv if it finds a different interpreter version.
- **A local model provider** — at least one of:
  - **[Ollama](https://ollama.com)** (the installer can `brew install` it for you), or
  - **[LM Studio](https://lmstudio.ai)** with its local server running on `http://localhost:1234`.
- **Disk and network:** the dependency set (PyTorch, Transformers, LanceDB, LlamaIndex, pyobjc, …) is several GB and downloads on first install. The installer also pre-caches a cross-encoder rerank model (~67 MB) and the tiktoken encodings so the first vault chat does not stall.
- **Optional — online chat providers.** OpenAI / Anthropic / Google / DeepSeek / Z.AI / OpenRouter need an API key each; see [Online provider setup](#online-provider-setup). Embeddings and retrieval always stay local.

### Models to pull on first run

To keep the install fast, **no models are pulled by default.** After launching, open the UI and:

1. Pull the **default embedding model** for the vault index — `nomic-embed-text` on Ollama (it is the app's shipped `embed` default; select another under `embed` if you prefer). It must be present in Ollama before indexing, and the chosen embedding model must stay consistent across re-indexes (switching it forces a re-embed). (Indexing now fails fast with a clear "pull this model" message if the embed model is not installed, instead of a slow generic error.)
2. Select a **chat model** — any installed Ollama tag, any model loaded in LM Studio, or an online provider once its key is set.
3. *(Optional, only if you want image descriptions / scanned-PDF OCR in the vault index)* pull the **OCR and vision models** for the active OCR/vision provider — the shipped defaults are `glm-ocr` (OCR) and `qwen3-vl:4b` (vision) on Ollama; pick others under **LLM Settings → OCR & Vision**. If neither is installed, `.md`/`.pdf` *text* still indexes fine — only image/figure and scanned-page reading is skipped (and counted in `skipped_image_count`).

With Ollama you can pull models from the UI; LM Studio models are loaded inside LM Studio and listed from its local server.

## Setup

```bash
chmod +x install_and_build.sh
./install_and_build.sh
open ChatEKLD_$(date +%Y-%m-%d).app
```

The build produces two artifacts in the repo root: `ChatEKLD_<date>.app` (run it in place on this Mac) and `ChatEKLD_<date>.dmg` (the shareable image — see [Sharing the app with another Mac](#sharing-the-app-with-another-mac)). The installer does not pull models by default. Ollama installation is optional. Select models in the UI after launch. Pulling models from the UI is available for Ollama only. LM Studio models are listed from `http://localhost:1234/v1/models`.

### Sharing the app with another Mac

The `.app` is **ad-hoc signed** (no Apple Developer ID, no notarization — ChatEKLD is a personal/local tool). On the machine that built it, it launches normally — a locally built bundle carries no `com.apple.quarantine` flag. The flag is attached only when the app is **transferred to another Mac** (downloaded, AirDropped, or unzipped), and that is when Gatekeeper blocks it ("ChatEKLD can't be opened because Apple cannot check it for malicious software").

Send the recipient the **`ChatEKLD_<date>.dmg`** — it bundles the app, an `/Applications` drag-target, a `READ ME FIRST.txt`, the offline model-seed helper (`seed_models.command`), and the optional desktop widgets (a `widgets/` folder — see below). On the receiving Mac (which must be **Apple Silicon**):

1. Open the `.dmg` and drag `ChatEKLD_<date>.app` onto **Applications**.
2. Approve it once (Gatekeeper blocks unsigned transferred apps):

   ```bash
   xattr -dr com.apple.quarantine "/Applications/ChatEKLD_<date>.app"
   ```

   (Right-clicking the app → **Open** may also work, but recent macOS versions increasingly route unsigned-app approval through System Settings → Privacy & Security → "Open Anyway" instead.)
3. *(Recommended, while online, once)* double-click **`seed_models.command`** from the `.dmg` so the first vault index and chat work fully offline afterward — it downloads the cross-encoder reranker + the tiktoken/NLTK caches into `~/Library/Application Support/ChatEKLD/`. Skipping it is fine if the recipient stays online: the app downloads those on first use instead.
4. Give the app a model provider (a local runner + models, or an online key in `.env` — see [Models to pull on first run](#models-to-pull-on-first-run) and [Online provider setup](#online-provider-setup)), then launch it.

The bundle is intentionally **not sandboxed** so it can read whatever Obsidian vault folder you point it at (Documents, an iCloud Drive path, an external volume, …). The first time it reads a protected location (Documents, Desktop, Downloads, or iCloud), macOS shows a one-time TCC consent prompt — that is expected; click **Allow**. No special entitlement or security-scoped bookmark is required. If the prompt does not appear and indexing reports no files found, grant access manually under **System Settings → Privacy & Security → Files and Folders** (or **Full Disk Access**) for ChatEKLD, or point the vault at a non-protected folder for the first run.

If you only want to run from source without building a bundle, skip the installer and follow [Development](#development) instead.

## Development

```bash
python3.12 -m venv ~/venvs/chatekld2026
source ~/venvs/chatekld2026/bin/activate
pip install -r requirements.txt -c constraints.txt
python launch.py
```

The venv location is yours to choose — `~/venvs/chatekld2026` is only a convention. `install_and_build.sh` defaults to that path (auto-detecting an existing `~/venvs/papermind2026` from before the rename so it isn't needlessly rebuilt) and honours a `CHATEKLD_VENV_DIR` override (the legacy `PAPERMIND_VENV_DIR` is still accepted). `constraints.txt` pins the exact validated dependency versions; install with `-c constraints.txt` for a reproducible environment.

Run checks from the virtual environment:

```bash
python -m py_compile app.py api/routes/*.py core/*.py core/providers/*.py core/llm/*.py core/llm/adapters/*.py core/agent/*.py rag/*.py services/*.py audit/*.py audit/core/*.py audit/engine/*.py audit/engine/reports/*.py deckgen/*.py docgen/*.py refactor/*.py tagging/*.py cli/*.py mcp_server/*.py
python -m pytest smoke_test.py test_concurrency.py test_vault_regressions.py test_llm.py test_validators.py test_agent.py test_audit.py test_deck.py test_docgen_route.py test_docgen_md.py test_refactor.py test_plainchat.py test_prompts.py test_compile.py test_health.py test_lancedb_migration.py test_open.py test_path_validators.py test_tagging.py tests/test_cli.py tests/test_driver.py tests/test_driver_contracts.py tests/test_driver_replay.py tests/test_mcp_server.py tests/test_server_info.py tests/audit/ tests/eval/test_scoring.py tests/eval/test_eval_pipeline.py tests/eval/test_retrieval_metrics.py tests/eval/test_golden_gen.py tests/eval/test_eval_runner.py -v
```

`test_prompts.py` pins the prompt grounding/citation/placeholder invariants (model-free); `tests/eval/` is the vault-chat answer-quality golden set — its scoring layer is verified by `tests/eval/test_scoring.py`, and the live runner is opt-in (`RUN_LIVE_EVAL=1 python -m tests.eval.run_eval`, needs a real provider).

`tests/eval/` also holds the **retrieval** eval harness — `retrieval_metrics.py` (pure recall@k / MRR / nDCG@k, document-level) plus two maintainer-run tools, `scripts/gen_golden_retrieval.py` (build a per-vault golden set by query inversion) and `scripts/eval_retrieval.py` (replay it against the real index, sweep knob combinations, print a Δ-vs-baseline table). Golden sets and results stay under the app data dir, never the repo. Workflow: [`tests/eval/README.md`](tests/eval/README.md).

The app-independent deckgen core has its own (non-hermetic, `requests`-free) suite:

```bash
python -m pytest deckgen/tests/ test_deck.py -v
```

## CLI & MCP server (headless)

Every workflow is also reachable without the GUI — as the `papermind` CLI and as an MCP (Model Context Protocol) stdio server for local AI agents (Claude Code, Cursor, …). Both are thin HTTP clients over the running app's own `/api/*` endpoints (validation, confirm-gating, journalling and locking all happen server-side; writes are refused when no server is running).

```bash
# From the project venv:
python -m cli --help                 # the CLI (status / paper / vault / audit / tagging / refactor / deck / docgen)
python scripts/install_cli.py        # installs a `papermind` shim into ~/.local/bin
python scripts/install_mcp.py        # registers the MCP server with Claude Code + Cursor (prints an OpenClaw snippet)

papermind server                     # headless backend (loopback-only; refuses to start beside the GUI)
papermind status                     # finds the GUI app automatically via BASE_DIR/server.json
papermind vault ask "…"              # RAG answer with sources
papermind deck generate "Topic" --template /path/to/template.tex
```

Server discovery: the GUI (random port) and `papermind server` both record themselves in `BASE_DIR/server.json`; the CLI/MCP resolve it at call time (`CHATEKLD_API_URL` overrides). The CLI/MCP surface needs the dev checkout + venv — it is not inside the built `.app`.

## Architecture

- `app.py` creates the Flask app and registers blueprints; `launch.py` is the PyWebView entry point (logging, `.env` loading, offline-HF setup).
- `api/routes/` owns the HTTP route handlers (`paper`, `vault`, `audit`, `deck`, `refactor`, `config`, `status`, `usage`, `about`). `api/security.py` owns local-origin checks and error redaction; `api/validators.py` owns the canonical request-body coercion/clamp helpers.
- `core/` owns config, constants, the SQLite database + lock, feedback, and locking/atomic-write utilities.
  - `core/providers/` — **local** Ollama and LM Studio adapters (embeddings + the legacy local-chat path).
  - `core/llm/` — the provider-agnostic chat layer (`types`, `base`, `factory`, `policy`, `usage`, `retry`, `prompt`, `redact`, `tool_schema`) plus adapters for `local`, `openai`, `anthropic`, `google`, and the OpenAI-compat subclasses in `openai_compat` (DeepSeek, Z.AI, OpenRouter).
  - `core/agent/` — the opt-in ReAct agent layer for vault chat (`protocol`, `tools`, `vault_tools`, `budget`, `loop`).
- `services/` owns single-PDF upload processing (`pdf_service.py`) and the vision/OCR manager singletons (`vision.py`). `pdf_extractor.py` runs PDF text extraction in a subprocess.
- `rag/` owns vault indexing + chat entrypoints (`vault.py`, with the index-build phases in `build.py`, the BM25/wikilink retrieval-cache sidecars in `sidecars.py`, and the stdlib-only store-file primitives in `storage_io.py` — all extracted 2026-07-30 as move + re-export, so `rag.vault` still exposes every historical name), the LlamaIndex query engine (`engine.py`), single-paper summarisation (`summarizer.py`), the thesaurus parser (`thesaurus.py`), and the optional LanceDB vector backend (`lancedb_store.py`).
- `audit/` is the vendored Library Audit subsystem (read-only connectors under `core/`, report builders under `engine/`, the `audit_manager` singleton, and a `python -m audit` CLI).
- `deckgen/` is the Beamer-deck orchestrator; its core is app-independent and `requests`-free. The CLI (`__main__.py`) drives the running app over HTTP; `inprocess.py` is the only app-coupled module (used by the in-app Deck Generator window).
- `refactor/` is the Note Refactor package: a read-only analyzer (`resolver`, `cache`, `hints`, `extract`, `hygiene`, `normalize`, `discrepancy`, `ignore`, `flags`, `review`, `sections` — heading-section splitter for sub-note scope, `pdfref` — reuses the indexer's cached PDF text, `plan`, `result`), the on-demand LLM layer (`llm_edit` — formatting rewrite / PDF summary / Mermaid / free-prompt; `staging` — server-side cache for applyable LLM proposals; `local_model` — the shared one-at-a-time local-model concurrency gate), and the app's only vault writers (`journal` — restore manifest + scope-lock + audit; `apply` — callout-only batch note writer; `format_fix` — deterministic formatting-fix batch writer; `llm_apply` — generic writer for a staged LLM proposal; `archive` — per-image move-out + thumbnail + restore). It reuses the indexer's Obsidian resolver + on-disk image/PDF caches; every vault write is atomic, scope-locked, audit-logged, and reversible. Exposed through `/api/refactor/*`.
- `tagging/` is the Biblio Tagging subsystem (a Library Audit sub-view scoped to `biblio_articles/`): `vocab` (pure `_tags.md` → `{tag: gloss}`), `text_source` (cache-first PDF text via the indexer — also backs the deck card's `/api/tagging/snippet` excerpt), `propose` (the vocabulary-constrained LLM tag proposer), `journal` (self-contained restore manifest, outside the vault), and `zotero_write` (stdlib-urllib Zotero client — two transports, the local API or zotero.org's Web API; version-guarded PATCH, never SQLite). It turns the audit's per-key `Record` join into a confirm-gated, reversible tag-writer across three targets (Obsidian note frontmatter, PDF Finder tags, Zotero item); the Finder writer lives in the route layer (`api/routes/_finder_tagwrite.py`) to keep vendored `audit/` read-only. The route exposes both per-target writers, a fan-out `apply-all` (one confirmed decision → every enabled/available target under one lock, each still a reversible op), and a journalled `add-vocab-tag` (admit a new id into `_tags.md` — the Finder Tag Drift report's `[+ vocab]` verb); the UI (`static/js/tagging.js`) offers a keyboard-first **Triage deck** and the **Table**. Exposed through `/api/tagging/*`.
- `static/js/` owns the browser UI modules; `templates/index.html` is the single-page shell.

Each subtree carries a `CLAUDE.md` with deep implementation notes (`rag/`, `core/llm/`, `core/agent/`, `audit/`), and `deckgen/README.md` documents the deck orchestrator. `CHANGELOG.md` holds history; `project_structure.txt` is the annotated file map.

### Performance & resource management

A few cross-cutting invariants keep the single-process app responsive and its writes safe (see the 2026-07-01 CHANGELOG entry for the full rationale):

- **No LLM work blocks the request thread.** Every model call — vault chat, deck generation, and the Note Refactor on-demand actions (rewrite / custom-edit / summarize-PDF / chart / review) — runs on a daemon worker with a wall-clock bound, so a slow or wedged local model degrades to a timeout instead of pinning one of the server's request slots.
- **One local model call at a time in the refactor hub.** Vision/OCR extraction, prose review, and applyable edits share a single concurrency gate, so they can't pile concurrent inferences onto a memory-constrained local backend.
- **HTTP connections are reused.** Local *and* online (OpenAI/Anthropic/Google) provider clients are cached and keep-alive, so an agent turn or a multi-section deck doesn't pay a fresh TLS handshake per round-trip. Online clients are cached without ever storing the API key (a key rotation is detected and honoured).
- **Indexing stays incremental and streaming.** Peak RAM is bounded by the largest single document; unchanged chunks are skipped by hash; mid-run checkpoints validate cheaply so vault chat isn't stalled while the index persists. Query-time retrieval knobs are all reindex-free.
- **Vault writes are atomic *and* durable.** Note Refactor's writers (and config/index metadata) write to a temp sibling, `fsync`, then rename — so a crash can never leave a torn *or* an unflushed file, and the archiver never loses a moved-out image.

## Providers

The `provider` config key selects the active chat provider.

Local (offline):

- `ollama` uses the Ollama API and resolves bare model names to installed tags.
- `lm_studio` uses the OpenAI-compatible LM Studio API and passes model IDs through unchanged.

Online (require API keys):

- `openai` uses the OpenAI Chat Completions API. Default model: `gpt-4o-mini`. Set `OPENAI_API_KEY` in the environment.
- `anthropic` uses the Anthropic Messages API. Default model: `claude-haiku-4-5`. Set `ANTHROPIC_API_KEY`.
- `google` uses the Gemini REST API. Default model: `gemini-2.5-flash`. Set `GOOGLE_API_KEY`.
- `deepseek` and `zai` (Z.AI / Zhipu GLM) are OpenAI-compatible endpoints served by thin adapter subclasses. Set `DEEPSEEK_API_KEY` / `ZAI_API_KEY` (`ZHIPU_API_KEY` also accepted).
- `openrouter` is an OpenAI-compatible **aggregator** — one key, many upstream vendors, namespaced model ids (`vendor/model`, optionally `:free`-style variant suffixes). Set `OPENROUTER_API_KEY`. Default model: `anthropic/claude-haiku-4.5`. OpenRouter routes between upstream providers internally; ChatEKLD's own `fallback_provider` policy applies unchanged on top (before the first token). The optional attribution headers (`HTTP-Referer`/`X-Title`) are deliberately not sent.

Online providers are chat-only. Embeddings and retrieval stay local — when the active chat provider is online, the indexer and vault chat resolve the embedding provider from the `embed_provider` config key (default `ollama`). Switching the chat provider therefore never triggers a re-index, and the user's notes never leave the machine in bulk.

The UI is the source of truth for `llm` and `embed`. Per-provider chat-model selections are persisted in their own fields (`openai_model`, `anthropic_model`, `google_model`, `deepseek_model`, `zai_model`, `openrouter_model`) so toggling providers preserves each choice. Indexing and chat requests send the selected provider and model values explicitly.

OCR and vision are separate from the active chat provider. Each has its own provider and model selector, so you can use Ollama for chat with LM Studio for OCR, LM Studio for chat with Ollama OCR, or any other local combination supported by the loaded models. Online providers are NOT supported for OCR or vision — they run locally only.

### Online provider setup

1. Provide the relevant API key(s). ChatEKLD loads a `.env` file at startup from, in priority order, `~/Library/Application Support/ChatEKLD/.env` and then a `.env` beside `launch.py`. A real shell variable always wins over the file (`override=False`).

   **Dev (`python launch.py` from a terminal):** export in your shell profile, or drop a `.env` in the repo root.

   ```bash
   export OPENAI_API_KEY=sk-...
   export ANTHROPIC_API_KEY=sk-ant-...
   export GOOGLE_API_KEY=AIza...
   export DEEPSEEK_API_KEY=sk-...
   export ZAI_API_KEY=...        # Z.AI / Zhipu GLM (ZHIPU_API_KEY also accepted)
   export OPENROUTER_API_KEY=sk-or-v1-...
   ```

   **Packaged `.app`:** a Finder-launched app does **not** inherit your shell environment, so keys in `~/.zshrc` are invisible to it. Copy `.env.example` to `~/Library/Application Support/ChatEKLD/.env` and put the keys there:

   ```bash
   cp .env.example ~/Library/Application\ Support/ChatEKLD/.env
   # then edit that file and uncomment your key(s)
   ```

2. Launch ChatEKLD. Pick OpenAI / Anthropic / Google / DeepSeek / Z.AI / OpenRouter from the provider dropdown. The chat-model dropdown populates with a curated list **merged with the provider's live model list** (fetched once per provider, cached), so newly-released models appear automatically; if the key is unset or the provider is unreachable it falls back to the curated list alone.

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

For a multilingual FR/DE/EN vault, `BAAI/bge-reranker-v2-m3` is the recommended upgrade over the English-trained default — no reindex, since reranking is query-time. Measure it on your own vault first with `python scripts/eval_retrieval.py --preset reranker-ab` (app closed), which also pre-warms the HuggingFace cache; then change the setting and **restart** the app, because a session launched with the old model cached runs with `HF_HUB_OFFLINE=1`. See [`tests/eval/README.md`](tests/eval/README.md).

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
| `wikilink_expansion` | `vault_wikilink_expansion` | bool | After retrieval, also pull chunks from notes your top hits link to / from (outbound ∪ backlinks) before the rerank stage decides what survives. **Rerank-gated** — it only takes effect when the reranker is on, and is a no-op otherwise (the reranker's `top_n` trim is what keeps the expansion from overflowing the context). Query-time, no reindex. The fan-out caps are config-only (see below). |
| `thesaurus_expansion` | `vault_thesaurus_expansion` | bool | Synonym query expansion from the curated `_abreviations.md` / `_tags.md` glossary at the vault root — substitutes a matched concept/abbreviation with each known synonym, retrieves each variant separately, and RRF-fuses. No-op when neither curated file exists. Query-time, no reindex. The variant cap (`vault_thesaurus_max_variants`) is config-only. See [Vault thesaurus](#vault-thesaurus). |
| `primer_enabled` | `vault_primer_enabled` | bool | Inject a compact glossary block (built from the same curated files) into the system prompt so the model can read your vault's shorthand. No-op when neither curated file exists. Query-time, no reindex. The size cap (`vault_primer_max_chars`) is config-only. See [Vault thesaurus](#vault-thesaurus). |
| `system_prompt` | `vault_chat_system_prompt` | ≤4000 chars | User-supplied behavioural instructions. On the **local** provider path it is layered as a prefix on top of the selected `prompt_mode` template. On the **online** provider path it is sent through the request's native `system_prompt` field, separate from the QA template. In both cases the selected `prompt_mode` template (safety preamble, untrusted-context guard, and `{context_str}` / `{query_str}` placeholders) stays app-controlled and unmodified, so a typo in the textarea cannot disable retrieval grounding. |

`vault_reranker_model` is config-only — it is not accepted as a body override, so a malicious page cannot point retrieval at an arbitrary HuggingFace repo.

The Vault Chat sliders in the Settings window deliberately expose a narrower, *safe* subset of these ranges (e.g. `top_k` 2-12, `similarity_cutoff` 0-0.7, `rerank_pool_ceiling` 20-100). The `/api/obsidian/chat` body and the `/api/config` validators accept the full ranges shown in the table above; the sliders just keep the everyday controls in a sensible band.

### Vault thesaurus

Two **opt-in, default-off** vault-chat features help a model deal with a vault written in dense shorthand and abbreviations. Both read two hand-curated Markdown glossary tables and are query-time only — **no reindex, no re-embedding**.

**The curated files (configurable paths, default at the vault root).** The thesaurus is parsed from two files whose **vault-relative paths you configure**:

- `vault_thesaurus_abbrev_path` — default `_abreviations.md` (note the **one-`b` French** default spelling; rename it to `_abbreviations.md`, move it into a subfolder, etc. by setting this key).
- `vault_thesaurus_tags_path` — default `_tags.md`.

Set either to an empty string to disable that slot. Paths are vault-relative and resolved under the vault root (traversal / absolute paths are rejected). If neither resolved file exists, both features silently do nothing. The files are read lazily and cached by their resolved paths + size + modification time, so editing a file **or** changing a path takes effect on your next chat — no reindex. They are never written to, and they do not need to be inside an indexed folder.

**Expected table format.** Each file is a Markdown table; **French or English column headers** are accepted:

- Abbreviations file: columns `Abréviation | Signification` (or `Abbreviation | Meaning`), + an optional notes column. Multiple surface forms can be separated by `/` or wrapped in backticks; a `≠` in the meaning/notes flags an ambiguous abbreviation that is excluded from automatic expansion.
- Tags file: columns `Description | Tags` (or `… | Tag`). The comma-separated description phrases are the bilingual synonym source.

**What you get when you enable them.**

- **Query expansion** (`vault_thesaurus_expansion` / body `thesaurus_expansion`): when your question contains a known concept or abbreviation, the engine issues up to `vault_thesaurus_max_variants` (default 3) extra queries — each substituting in a known synonym — retrieves them separately, and reciprocal-rank-fuses the results. This bridges shorthand the embedding model can't and lets the lexical BM25 leg hit the synonym token.
- **System-prompt primer** (`vault_primer_enabled` / body `primer_enabled`): prepends a compact glossary block (≤ `vault_primer_max_chars`, default 1500, query-relevant entries first) to the answer so the model can *read* shorthand that survives into the retrieved chunks.

**Adapting it to your own corpus.** The built-in defaults in `rag/thesaurus.py` are tuned for the maintainer's **bilingual French/English psychiatry / clinical-research** vault, but the corpus-specific pieces are configurable — no code edit required:

- `vault_primer_header`: replace the glossary intro sentence (empty = built-in).
- `vault_primer_core_terms`: a comma-separated priority list of abbreviations that should survive truncation when the primer budget is tight (empty = built-in list).
- English or French table headers are both accepted, so the curated files can be authored in either language.

The `≠` ambiguity marker remains a convention (put it in an abbreviation's meaning/notes to keep that abbreviation out of automatic expansion). Because both features default off and no-op without the curated files, none of this affects vaults that don't opt in.

All of these config-only knobs (file paths, max variants, primer budget, header, core terms) are editable in the **LLM Settings** window under the vault section, alongside the live *Thesaurus expansion* / *Abbreviation primer* toggles — or via `/api/config` / `config.json` directly.

### Agent mode

A ReAct loop runs the chat model with three tools registered. The model decides what to call and when to answer:

| Tool | Args | Output cap |
| --- | --- | --- |
| `vault_search` | `query`, optional `top_k` (1-12, default 6) | 12 000 chars; each snippet trimmed to 800 chars |
| `vault_read_note` | `rel_path` (vault-relative, forward slashes) | 32 000 chars |
| `vault_list_materials` | optional `filter` (substring on path), optional `limit` (1-200, default 100) | 20 000 chars |

The loop accepts `agent_max_iterations` per request (1-12, default `vault_agent_max_iterations` = 6) and a wall-clock cap shared with the simple-RAG path — the config key `agent_wall_clock_s` (default 300 s, clamped 30–1800; `_CHAT_TOKEN_TIMEOUT_S` in `api/routes/vault.py` is only the fallback default). When the iteration cap is hit without a final answer, the loop emits an info event and stops.

**Web search (optional fourth tool, 2026-07-22).** With the "Web search (agent tool)" checkbox on (`vault_web_search_enabled`, default **off**), agent turns also get a `web_search(query, max_results)` tool backed by the Tavily API. Three conditions must all hold: the knob is on, the active **chat provider is online** (local models are deliberately excluded for now), and `TAVILY_API_KEY` is set in the environment (`.env`) — the key is never persisted or returned by any API, and it is redacted from logs. Results (title, URL, snippet) flow through the same nonce'd untrusted-content wrapper as vault content; web pages are the canonical prompt-injection vector and are treated accordingly. **Egress note:** this is the app's one deliberate network surface beyond the LLM providers, Zotero, and the one-time model downloads (HuggingFace reranker + the `--seed-models` helper) — everything else stays local. `vault_web_search_max_results` (1–8, default 5) caps results per call. Deck/article generation shares the tool automatically when the gate passes.

The `system_prompt` field is reused in agent mode. A fixed agent preamble — naming the tools and reinforcing the untrusted-tool-output guard — is always prepended to the system message; the user's text is appended after it. The preamble's text is therefore guaranteed to be present (the user cannot remove or rewrite it), but a sufficiently insistent user prompt could still contradict it in instructions to the model.

`prompt_mode` does not apply in agent mode — the answer-mode templates live on the simple-RAG path only.

**Fallback to simple RAG.** Two consecutive iterations where the model produces no parseable tool call (empty `STOP`, or `TOOL_USE` with zero parsed calls) trigger a clean re-run of `stream_chat` against the **original** user message — accumulated agent trace is not mixed back into a single-shot prompt. An info event announces the fallback.

**Capability warning.** After two consecutive turns in a session each end in fallback, a one-shot info event suggests switching models: "try a tool-capable model (e.g. Qwen 2.5, Llama 3.1+, Mistral Nemo, or any online provider)". The warning never repeats within a session; a single successful agent turn resets the counter.

**Provider capability.** Every provider reports `supports_tool_use() = True`. For online providers (OpenAI / Anthropic / Google / DeepSeek / Z.AI / OpenRouter) the wire-level tool schema is honoured by the API. For the local provider (Ollama / LM Studio) the endpoint accepts the `tools=` parameter but **whether the configured model honours that schema is a runtime question** — small or non-tool-tuned local models will produce plain text instead of structured calls, which then triggers the fallback path above.

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

A document longer than the model's context window is summarised **map-reduce** style: it is split into window-sized parts, each part is condensed into notes (with progress messages and keep-alive heartbeats in the UI), and the final summary is synthesised from the combined notes. Slower, but nothing is *silently* dropped — previously the tail of a long paper was clipped without warning on local models. Two announced bounds apply to extreme inputs: a per-run cap of 64 parts (a book-length document at a small context window stops there with an explicit coverage notice — raise the context window to cover more) and a single-level reduce (combined notes that still overflow the window are clipped with a notice). Cancelling the request stops the remaining part passes immediately.

## Library Audit

The Library Audit tab reconciles the configured Obsidian vault with Zotero, Better BibTeX, the local PDFs under the attachments directory, and macOS Finder tags. The engine is vendored from the `kb_harmonizer` project under `audit/` and is exposed through `/api/audit/*`.

**Manual only.** The audit is never triggered at startup, at config change, or by any other side effect. The only entry point that spawns a scan is `POST /api/audit/scan` — wired to the **Run Scan** button on the Library Audit tab. Until you click it, the tab shows an empty state and the manager reports `idle`.

**Read-only, with explicit confirmed exceptions.** Every external store is opened read-only: the Zotero SQLite database is opened with `mode=ro` and immediately copied into an in-memory snapshot, PDFs are opened with pikepdf for annotation counting only, BibTeX is parsed in place, Finder tags are read via the macOS `getxattr` xattr — and the scan itself never writes anything user-visible (it maintains one regenerable hash memo under the app data dir). The audit's own writable surfaces are exactly three: `BASE_DIR/audit/mapping.json` (your manual PDF↔bib overrides, recorded from the UI; each entry is **hash-anchored** — a sha256 of the PDF — so a curated file you later rename or move is re-linked automatically at the next scan), `BASE_DIR/audit/dup_decisions.json` (the Possible Duplicates report's persisted "not a duplicate" dismissals, undoable), and the per-note **apply missing tags** button on the Tag Drift report, which — after an explicit confirmation — writes the missing Zotero tags into that note's YAML frontmatter (server-validated against the real drift, atomic, `.bak` backup of the original kept, audit-logged). The **Finder Tag Drift** report's fix verbs are not a fourth surface: they dispatch to the Tagging sub-view's journalled, undoable writers (note frontmatter / PDF Finder tags / the `_tags.md` vocabulary).

The reports surfaced by the tab:

| Report | What it shows |
| --- | --- |
| **Inventory** | One row per BibTeX citation key, joining bib entry + Zotero parent + Obsidian note + resolved PDFs + Finder tags + annotation count. |
| **Tagging** | Tag `biblio_articles/` papers in a keyboard-first **Triage deck** (one card at a time, untagged first, PDF excerpt; Accept writes all three targets at once) or a side-by-side **Table**; LLM **Propose**, a vocabulary type-ahead, per-target apply (note / Finder / Zotero) + inline **Restore**. See *Biblio Tagging* below. |
| **Tag Drift** | Zotero tags missing from the corresponding Obsidian YAML, compared by a spelling-insensitive fingerprint (a tag present under another spelling or an `_tags.md` synonym shows as an informational *variant*, never as missing). Each row has an **apply to Obsidian** action that writes the genuinely-missing tags into the note's frontmatter (confirm-gated, `.bak`-backed); the reverse direction (*note-only*) is reported read-only. |
| **Finder Tag Drift** | Per paper, the PDF's Finder tags vs the note's YAML tags (papers carrying ≥1 Finder tag), same fingerprint comparison. Correctable in place: **[→ note]** / **[→ Finder]** write the gap through the Tagging sub-view's journalled writers (confirmed, undoable per row), and an out-of-vocabulary tag gets a **[+ vocab]** verb that adds it to `_tags.md` first (journalled too). |
| **PDFs Without Zotero** | **The complete set**: every PDF under the biblio articles folder that the bridge could not resolve to a bib entry — read *and* unread — ranked by annotation count (the suggested cutoff is a visual divider, it filters nothing). The meta line discloses what is NOT listed: ambiguous PDFs (resolve them in *Ambiguous PDFs*), ones you confirmed are not in the bib, and `biblio_skip_prefix` files. |
| **↳ Unread Only** | A filtered **subset** of *PDFs Without Zotero* — the same list narrowed to entries with fewer annotations than the configured threshold. Not the complement of it. |
| **Zotero Queue** | Bib entries with a matching Zotero parent but no child note. Treats "child note exists" as the signal that a paper has been engaged with. |
| **Bib Entries Without PDFs** | Bib entries where no local PDF was resolved. Includes a Zotero-match indicator. |
| **Ambiguous PDFs** | PDFs the bridge could not disambiguate (author+year matched more than one bib key), with a per-row key picker that records a manual mapping. |
| **Skipped PDFs** | Files excluded by the `biblio_skip_prefix` convention (default `z_item*`), listed so nothing important hides behind the skip rule. |
| **Duplicate PDFs** | Content-identical PDF sets under the biblio articles folder, grouped by SHA-256. |
| **Possible Duplicates** | Byte-*different* likely copies of the same paper the exact scan cannot see — same normalized first-page text, same DOI/arXiv id, one citation key with several PDFs, title+year+author record pairs, plus a similarity sweep. Every group states its evidence tier; **[not a duplicate]** dismissals persist across scans and are undoable. |
| **Tag Hygiene** | Cross-store tag frequencies (Finder / note YAML / Zotero / Zotero notes / bib keywords) grouped by spelling fingerprint, with vocabulary flags, near-miss clusters and top co-occurrence pairs. Read-only, except the per-spelling `[retire]` buttons — see *Retiring a tag everywhere* below. |

The Bridge (PDF ↔ bib citation key) resolves matches in priority order: `mapping.json` overrides, frontmatter PDF pointer fields (`pdf`, `attachments`, `file`, …), frontmatter author + year, wikilinks inside Zotero notes, and finally a filename author-year heuristic. Ambiguous matches are reported separately from unmapped PDFs. Manual mappings are content-hash-anchored: if a curated PDF is renamed or moved, the next scan re-links it by sha256 (`manual_hash` source) instead of orphaning your curation.

### Biblio Tagging

The **Tagging** report tab turns the audit's per-citation-key join into a confirm-gated, reversible **tag writer** for the papers under `biblio_articles/`. It reuses the last scan's results (run **Run Scan** first), so it never scans on its own. It offers **two views**, switched by the toolbar toggle (remembered across sessions):

- **Triage deck** (default) — a fast, keyboard-first flow that shows **one paper at a time**, untagged papers first, with a PDF-text excerpt for context. Best for working through a backlog.
- **Table** — every paper side-by-side with its current tags across all three targets; best as an overview or to jump to a specific key.

**Triage deck (the fast path):**

1. Open **Library Audit → Run Scan**, then the **Tagging** sub-tab (the deck opens by default).
2. **Propose** (press <kbd>P</kbd>) — an LLM constrained to your `_tags.md` vocabulary reads the PDF's already-cached text (the same text the indexer extracted, OCR included) and suggests tags; press <kbd>1</kbd>–<kbd>9</kbd> to toggle the proposed chips, or type your own (type-ahead over the full vocabulary).
3. **Accept** (press <kbd>A</kbd> or <kbd>Enter</kbd>) — after one inline confirm, the chosen tags are written to **all enabled targets at once** (note frontmatter + PDF Finder tags + parent Zotero item); a target that is absent (no note / no Zotero item), off macOS (Finder) or unreachable (Zotero) is skipped with a note, and the card auto-advances. Toggle the three target checkboxes on the card to change what Accept writes (remembered card-to-card).
4. **Skip** <kbd>S</kbd>/<kbd>→</kbd>, **Defer** <kbd>M</kbd> (sends the paper to a review queue you sweep at the end), **PDF panel** <kbd>V</kbd> (toggles the full PDF beside the card), **Undo** <kbd>⌫</kbd> (reverses the last Accept), **Back** <kbd>←</kbd>.

Every Propose/Apply outcome is shown in a **visible status line** on the card — including "no extractable text" for scanned PDFs, "the model returned no tags", or a provider error — so you always know what happened.

**Table view:** the same actions per row — **propose**, a vocabulary type-ahead box, and per-target **[→note] / [→finder] / [→zotero]** apply buttons (each confirmed inline; [→finder] macOS-only, [→zotero] disabled when Zotero is unavailable over the configured transport) — with a per-row status line.

Use **Restore…** (either view) to reverse any write. Restores are conservative — a target that changed after the write (a note you hand-edited, a tag you removed in Zotero) is left untouched rather than clobbered.

**Deleting tags (2026-07-22).** Every *current* tag shown in the table cells or on a deck card carries a small **×** — click it to remove that tag from that ONE store (note frontmatter, Finder tags, or the Zotero item), behind an inline confirm. Removal is deliberately **not** vocabulary-gated (the tag most worth deleting is often one that already left `_tags.md`); each removal is journalled as its own reversible operation, colours and Zotero tag metadata included, and undone from **Restore…** like any write.

**Retiring a tag everywhere (2026-07-22).** The read-mostly **Tag Hygiene** report gains per-spelling `[retire]` buttons: one confirm removes that tag from **every** note, PDF and Zotero item that carried it in the last scan (live state is re-checked per record; anything already changed is skipped), optionally deleting its `#tag` token from `_tags.md` too. The confirm states exactly what will happen — the occurrence count, the `_tags.md` row edit for an in-vocabulary tag, and that Zotero **child-note** tags and `.bib` keywords are **not** touched (the report's cross-store counts include those stores, which retire cannot reach). While a run is in flight, every `[retire]` button is disabled. The whole run is journalled as a **batch**: each finished run gets its own `[undo retire]` button that re-adds everything that run removed, and starting another retire does not take an earlier batch's Undo away. Counts in the report refresh on the next scan (it stays a snapshot view).

**Safety.** Every write is confirm-gated, validated against your controlled `_tags.md` vocabulary **server-side** (so nothing — not even the LLM's own suggestion — can inject a tag outside your vocabulary), `log_vault_write`-audited, and recorded in a restore manifest under a tagging archive folder that must resolve **outside** the vault. Nothing is written during indexing (the write waits on the same lock).

**Requirements.** LLM Propose uses your configured chat model (override with `tagging_propose_model` in **LLM Settings → Biblio Tagging**, where the propose max-tokens and temperature also live — keep the token cap generous: on Gemini, "thinking" tokens count against it, and a too-small cap makes every proposal come back empty). Finder-tag writes are macOS-only. Zotero writes go through Zotero's HTTP API (never the SQLite file) over one of **two transports** (`tagging_zotero_transport`: `auto` / `local` / `web`, default `auto`). The **web** transport — zotero.org's Web API v3 — is the one that works today: put a `ZOTERO_API_KEY` with write permission in the environment (`.env`; never persisted to config, never returned by any API) and `auto` picks it. The **local** transport (*Settings → Advanced → “Allow other applications on this computer to communicate with Zotero”*, `http://127.0.0.1:23119`) answers reads but, **as of Zotero 9, rejects writes** (verified live on 9.0.6: PATCH/PUT return 501 "Method not implemented"; write support is slated for Zotero 10). Without a key, a Zotero apply therefore fails with an actionable "configure a key" message, and the deck's Accept skips the Zotero target with the same reason — the note-frontmatter and Finder-tag writes still land.

### Diagnostic CLI

The same engine is reachable from the command line for development checks:

```bash
python -m audit --check inventory
python -m audit --check all
```

Available checks: `obsidian`, `zotero`, `zotero-debug`, `finder`, `bib`, `duplicates`, `duplicates-biblio`, `bridge`, `inventory`, `note-tag-drift`, `unread-unzoterod`, `zotero-unread`, `read-unzoterod`, `zotero-no-pdf`, `all`. The CLI reads the same `config.json` the Flask app uses, so vault and Zotero paths come from the same source.

## Deck Generator

The Deck Generator turns a **topic + free-form instructions** into a LaTeX **Beamer** lecture deck, grounded in the indexed Obsidian vault. It is **emit-only**: it writes a compile-ready `.tex` (and, in template mode, a `Makefile`) and you compile it yourself — or use the in-app **Preview PDF** (write-free latexmk pass, rendered in-app) and the opt-in **Compile & Auto-Fix** loop. It works with both local (Ollama / LM Studio) and online (OpenAI / Anthropic / Google / DeepSeek / Z.AI / OpenRouter) providers.

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

- splits the template into preamble / opening scaffold / closing tail and **reuses your preamble verbatim** — document class, theme, packages, metadata — dropping the template's example sections and injecting the generated ones in their place; the one exception is `\title[short]{…}` / `\subtitle{…}`, which are substituted with the deck title you set in the window (defaults to the topic; leave the field empty to keep the template's own title);
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
- **OCR & Vision** — the OCR/Vision provider + model selects, plus the always-on call bounds `vision_timeout_s`, `vision_max_tokens`, and `ocr_max_tokens`.
- **Retrieval** — `vault_reranker_model`, `vault_reranker_device` (`auto`/`cpu`/`mps`), `vault_vector_backend`, `vault_prewarm_enabled`, and the vault-chat knobs.
- **Generation defaults** — the per-function `paper_*`, `deck_*`, `docgen_*`, and Plain Chat `chat_*` values, plus the Deck integrity-review default (`deck_review_enabled`) and the Note Refactor models/limits (`refactor_*`).
- **Transparency** — `prompt_capture_enabled` (records each workflow's latest prompt for the read-only Prompt Hub tab).
- **Application log** — a read-only viewer that tails `chatekld.log` (last 100–5000 lines, selectable) so you can inspect diagnostics without leaving the app. API keys are redacted server-side before display; the view never modifies the log.

  Config-only (edit `config.json` or `POST /api/config` directly — no Settings control by design): `llm_pricing_overrides`, `context_window`, `online_doc_window_tokens`, `vault_embed_batch_size`, `vault_rrf_dense_weight`/`vault_rrf_bm25_weight`, `vault_rerank_pool_multiplier`/`vault_rerank_pool_floor`, the `vault_wikilink_*` caps, the `vault_thesaurus_*`/`vault_primer_*` group, `deck_compile_*`, `deck_review_model`/`deck_review_max_tokens`, and `deck_resume_enabled`/`docgen_resume_enabled`.

Every value the window writes is range-/enum-validated server-side by `POST /api/config`; an out-of-range value is dropped (the prior persisted value survives) rather than stored. API keys are **never** written here — they come from environment variables only (see [Online provider setup](#online-provider-setup)).

Several free-text fields (Deck instructions, the Vault Chat system-prompt override, Plain Chat, and the Note Refactor free-prompt edit) carry **click-to-insert example prompts** — small chips that fill the field with a starter instruction you can then edit.

## Config Keys

- `provider`: chat provider — `ollama`, `lm_studio`, `openai`, `anthropic`, `google`, `deepseek`, `zai`, or `openrouter`.
- `llm`: selected chat model for local providers (Ollama / LM Studio).
- `openai_model` / `anthropic_model` / `google_model` / `deepseek_model` / `zai_model` / `openrouter_model`: per-provider chat-model selection so switching providers does not lose a choice.
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
- `vault_image_exts`: image file extensions sent to the vision model and embedded. Description is best-effort and decoder-dependent — the default set includes `.heic`, which is decoded via the bundled `pillow-heif` and re-encoded to PNG before the vision call (so macOS screenshots/photos index out of the box). Vector formats like `.svg`/`.ico` that PIL cannot rasterize are skipped with a clear "convert it to PNG/JPEG" message and counted in `skipped_image_count` rather than erroring the run. Document extensions (`.docx`, `.pptx`, …) are rejected from this list outright.
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
- `vault_web_search_enabled`: register the agent's `web_search` tool (default `false`; also a per-request `web_search_enabled` body field / vault-panel checkbox). Takes effect only with an ONLINE chat provider and `TAVILY_API_KEY` in the environment; results are wrapped as untrusted content. `vault_web_search_max_results` (1–8, default 5) caps results per call.
- `vault_wikilink_expansion`: enable wikilink graph expansion — widen the result set with chunks from notes your top hits link to / from before the rerank stage (default `false`; also a per-request `wikilink_expansion` body field). Rerank-gated (no-op when the reranker is off). Query-time, no reindex.
- `vault_wikilink_neighbor_cap` / `vault_wikilink_node_cap` / `vault_wikilink_score_decay`: fan-out caps for wikilink expansion — distinct neighbour notes per query (1-100, default 10), total chunks appended (1-200, default 24), and the decay applied to a neighbour's inherited seed score (0.0-1.0, default 0.5). Config-only (not per-request body overrides).
- `vault_thesaurus_expansion`: enable synonym query expansion from the curated `_abreviations.md` / `_tags.md` glossary at the vault root (default `false`; also a per-request `thesaurus_expansion` body field). No-op when neither curated file exists. Query-time, no reindex. See [Vault thesaurus](#vault-thesaurus).
- `vault_thesaurus_max_variants`: max synonym-substituted query variants per turn (1-8, default 3). Config-only (not a per-request body override).
- `vault_primer_enabled`: prepend a compact glossary block (built from the same curated files) to the vault-chat system prompt so the model can read your vault's shorthand (default `false`; also a per-request `primer_enabled` body field). No-op when neither curated file exists. Query-time, no reindex.
- `vault_primer_max_chars`: glossary-primer byte budget (500-8000, default 1500). Config-only (not a per-request body override).
- `vault_thesaurus_abbrev_path` / `vault_thesaurus_tags_path`: vault-relative paths to the two curated glossary files (defaults `_abreviations.md` / `_tags.md`; `""` disables that slot). Resolved under the vault root; traversal/absolute paths are rejected. Config-only.
- `vault_primer_header`: override the primer's intro sentence (default `""` = built-in FR/DE/EN text). Config-only.
- `vault_primer_core_terms`: comma-separated priority list of abbreviations that survive truncation when the primer budget is tight (default `""` = built-in list). Config-only.
- `vault_vector_backend`: vector store for **new** index builds — `simple` (legacy JSON, default) or `lancedb` (binary Apache-Arrow). Existing indexes keep whatever backend is recorded in `obsidian_meta.json`. See "Vector store backend (LanceDB)" above to migrate an existing vault with no re-embedding. Resolves to `simple` if lancedb is not installed.
- `context_window`: token context window assumed for the chat model (default `32768`). Used to size the local LLM's context and to autoscale retrieval breadth down on small-context models. Advanced/internal — there is no Settings control; it is validated/clamped (512-131072) on save like the other LLM knobs.
- `audit_attachments_subdir`: vault-relative folder containing all PDF attachments (default `Z_attachments`). Read only when an audit scan runs.
- `audit_biblio_articles_subdir`: subfolder under the attachments dir that holds bibliography PDFs (default `biblio_articles`).
- `audit_zotero_notes_subdir`: vault-relative folder that holds one `<bbtkey>.md` file per Zotero parent (default `Z_Zotero_Notes`).
- `audit_master_bib_path`: vault-relative path to the Better BibTeX master export (default `_master.bib`).
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

Vision / OCR bounds (indexing-time image-description and scanned-PDF OCR calls; **always on**, separate from `local_request_timeout_s`, so a stuck local vision model cannot stall a multi-hour indexing run):

- `vision_timeout_s`: per-call HTTP timeout for both image description and OCR, in seconds (5-600, default 120). Unlike `local_request_timeout_s` there is no `0 = off` — these calls are always bounded.
- `vision_max_tokens`: generation cap for an image/figure description (64-8192, default 1536).
- `ocr_max_tokens`: generation cap for scanned-page OCR (64-8192, default 4096; higher than image description since a dense page has more text).
- `vision_failure_cooldown_s`: fast-fail window after a failed vision/OCR call (0-600, default 30; `0` disables). During indexing, images inside the window are skipped, so a shorter value trades retry traffic against fewer dropped images after a transient failure. Editable in the **LLM Settings** window (OCR / Vision section) since 2026-07-30.

Per-function generation defaults (set in the Settings window; body values override, then these, then the hard clamp):

- `paper_temperature` / `paper_num_ctx` / `paper_max_tokens` / `paper_top_p` / `paper_repeat_penalty`: Single-Paper generation defaults (defaults `0.3` / `32768` / `4096` / `0.9` / `1.1`).
- `deck_temperature` / `deck_max_sections` / `deck_agent_max_iterations`: Deck Generator defaults (defaults `0.3` / `8` / `6`). Also the per-section resilience/checkpoint knobs `deck_section_max_attempts` (1-5, default 3) / `deck_retry_backoff_s` (0-30, default 3) / `deck_section_max_tokens` (256-8192, default 2048) / `deck_resume_enabled` (default `true`, config-only).
- `docgen_temperature` / `docgen_max_sections` (1-30) / `docgen_agent_max_iterations` / `docgen_section_max_attempts` / `docgen_retry_backoff_s` / `docgen_section_max_tokens` (256-16384, default 4096) / `docgen_resume_enabled`: Report / Article Generator defaults — the prose sibling of `deck_*`, set in the Settings window's "Report / Article · generation" section (Compile & Auto-Fix reuses the `deck_compile_*` / `deck_review_*` knobs).
- `chat_temperature` / `chat_system_prompt`: Plain Chat defaults (defaults `0.3` / `"You are a helpful assistant."`). The system prompt is the **full** prompt (≤4000 chars; no vault-grounding preamble — Plain Chat has no retrieved context to protect). Body values override these, then the hard clamp; the panel itself sends neither, so a Settings change applies on the next send with no reload.

Deck / article integrity review + Compile & Auto-Fix (shared by the deck and the Report / Article LaTeX path):

- `deck_review_enabled`: default the opt-in final-stage `.tex` integrity review + auto-repair on (default `false`; Settings-window toggle; per-run checkbox still wins).
- `deck_review_model` / `deck_review_max_tokens`: HF-repo-id-shaped stronger-model override (`""` ⇒ chat model) and its output cap (256-16384, default 4096). Config-only (`POST /api/config`) — no Settings control.
- `deck_compile_timeout_s` (30-600, default 180) / `deck_compile_max_iters` (1-3, default 2) / `deck_compile_engine` (`pdflatex`/`lualatex`/`xelatex`): the Compile & Auto-Fix bounds. Config-only — re-validated at call time since the engine reaches `latexmk`'s argv.

Note Refactor (models, LLM edits, archiving — set in the Settings window's "Note Refactor" section):

- `refactor_scope_subdir` / `refactor_extract_model` (`""` ⇒ vision model) / `refactor_table_double_read` / `refactor_strip_preamble_default`.
- `refactor_review_model` (`""` ⇒ chat model) / `refactor_review_max_tokens` (64-8192, default 1024) / `refactor_rewrite_max_tokens` (256-16384, default 4096): the opt-in LLM prose-review + applyable-edit knobs.
- `refactor_archive_dir` (absolute path or `""` = default under the app data dir; re-checked to be outside the vault at apply time) / `refactor_thumb_max_side` (96-1024, default 384).

Retrieval / indexing (config-only unless noted):

- `vault_rrf_dense_weight` / `vault_rrf_bm25_weight` (0.0-10.0, default 1.0): RRF fusion leg weights. At the 1.0 defaults fusion is byte-identical `reciprocal_rerank`; any other value switches that query to `relative_score` (the only mode that applies weights) — tune against `tests/eval/` (`python scripts/eval_retrieval.py --preset rrf-sweep`, app closed).
- `vault_embed_batch_size` (1-256, default 16; `1` = legacy per-chunk inserts): how many chunks the indexer buffers into one `insert_nodes` / provider embed round-trip.

Transparency:

- `prompt_capture_enabled`: record each workflow's latest system/user prompt for the read-only Prompt Hub tab (default `true`; Settings-window toggle). Off ⇒ the Hub shows nothing new; capture never persists to disk beyond the in-memory snapshot the Hub reads.

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
| `/api/export-summary` | POST | Export a summary as `.txt` or `.md`. Optional `raw: true` writes the content verbatim under the given name (no `Summary_` prefix / injected heading) — used by the conversation exports. |
| `/api/feedback` | POST | Save a feedback record. |
| `/api/feedback/history` | GET | Read recorded feedback. |
| `/api/obsidian/index` | POST | Start vault indexing. |
| `/api/obsidian/status` | GET | Poll indexing state, messages, and warnings. |
| `/api/obsidian/materials` | GET | List files recorded in the current vault index manifest. |
| `/api/obsidian/chat` | POST | Stream vault RAG tokens as SSE. |
| `/api/plainchat` | POST | Stream RAG-free Plain Chat tokens as SSE. Body `{messages, system_prompt?, temperature?}`; `messages` is capped to the last 20 turns. |
| `/api/obsidian/pause` | POST | Request indexing pause (resumable). |
| `/api/obsidian/cancel` | POST | Request indexing cancellation and release the operation lock. |
| `/api/native-pick-folder` | POST | Open a native folder picker. |
| `/api/reset` | POST | Clear uploads and the Obsidian index. Can also remove config and feedback when requested. |
| `/api/log` | POST | Receive a frontend error log entry (capped at 500 chars). |
| `/api/log/tail` | GET | Read-only tail of `chatekld.log` for the in-app log viewer (`lines` ≤ 5000, last ~1 MB, API keys redacted server-side). |
| `/api/open` | POST | Open a link on the app's behalf: `{path}` (vault-relative, must exist) reveals the file in Finder (macOS); `{url}` (http/https only) opens the default browser. Every rendered link click routes here — the webview itself never navigates. |
| `/api/prompts` | GET | Read-only Prompt Hub snapshot of the effective system prompt last captured per workflow (`core.prompt_capture`); no model call, no writes. |
| `/api/about` | GET | Build metadata and version info. |
| `/api/usage` | GET | Token / cost totals + recent activity. Window: `today`, `week`, `month` (default), `month_to_date`, `all`. |
| `/api/pricing` | GET | Published USD-per-1M-token prices used for cost estimates. |
| `/api/widget` | GET | The desktop-widget snapshot payload (`widget.json` — paper of the day, suggestions, notion). 404 until a scan has run. |
| `/api/widget/refresh` | POST | Rebuild the widget snapshot from the cached scan (`{redraw?}`; 409 without one). Writes only regenerable app-data files — never the vault, never a scan. |
| `/api/audit/config` | GET/POST | Read or update Library Audit settings (subpaths, Zotero paths, thresholds). |
| `/api/audit/status` | GET | Audit scan state (`idle / scanning / done / error / cancelled`), progress messages, and result flags. |
| `/api/audit/scan` | POST | Start a Library Audit scan in the background. The only endpoint that triggers a scan. |
| `/api/audit/cancel` | POST | Signal the in-flight scan to abort cooperatively. |
| `/api/audit/inventory` | GET | Cached inventory rows from the last scan (404 until a scan completes). |
| `/api/audit/reports/<name>` | GET | One of `note_tag_drift`, `finder_tag_drift`, `unread_unzoterod`, `zotero_unread`, `read_unzoterod`, `zotero_no_pdf`, `duplicates`, `near_duplicates`, `ambiguous_pdfs`, `skipped_pdfs`, `tag_hygiene`. |
| `/api/audit/dup-decision` | POST | Persist / undo a Possible-Duplicates "not a duplicate" decision (`{pair_id, dismiss:true \| clear:true}`) in `dup_decisions.json`. |
| `/api/audit/mapping` | POST | Record a manual PDF↔bib override, "confirmed no match", or `clear` (the triage deck's undo) in `mapping.json`. Hash-anchored: the written entry is anchored immediately; the full re-anchor of *other* curated entries runs at scan time, not per click (2026-07-21). |
| `/api/audit/search` | GET | Free-text search across every report of the cached scan (`?q=`, min 2 chars). Read-only snapshot view; 202 `{building:true}` while the corpus index builds in the background. |
| `/api/audit/pdf` | GET | Bytes of a vault PDF for the ambiguous-PDF triage preview (`?rel=`, vault-contained, `.pdf` only). Fetched with the app header and rendered via a blob: URL. |
| `/api/audit/apply-tag-drift` | POST | Write missing Zotero tags into one note's frontmatter (`{citation_key, tags, confirm:true}`) — the audit's only vault write: drift-validated server-side, op-locked, non-clobbering `.bak`, YAML round-trip guard. |
| `/api/audit/reveal` | POST | macOS-only: reveal a file in Finder, open it, or open a Zotero item by key. Paths are bounded to the configured roots. |
| `/api/deck/load-template` | POST | Validate a Beamer template path and scan it → `{tex, macros, bib_keys_count, suite_root}`. |
| `/api/deck/generate` | POST | Generate a vault-grounded deck (SSE: reuses the `info`/`error`/agent-trace contract, then a terminal `{"deck": {tex, warnings, review, tex_path, make_hint, …}}` frame, where `review` is the opt-in integrity-pass result or `null`). Emit-only. |
| `/api/deck/apply-repair` | POST | Write a confirmed integrity-review repair over the just-generated `<slug>.tex` (`{out_dir, deck_name, tex, base_sha256, confirm:true}`; `base_sha256` is the stale-diff guard — 409 if the on-disk deck changed since the review). Re-runs structural `validate()` for warnings (not a re-screen — see deckgen/README.md). |
| `/api/deck/deck-sections` | POST | List an existing deck's `\section`s for the augment scope picker (`{deck_path}` → `{sections, deck_sha256}`). Read-only. |
| `/api/deck/augment` | POST | Deepen / add a table / add a section / **reformat** an existing deck (SSE; `{deck_path, instruction, operation∈{deepen,table,new_section,reformat}, scope∈{whole,section}, section_index?}`). Writes nothing — the screened proposal is staged server-side; 409 if another deck op runs. |
| `/api/deck/apply-augment` | POST | Write the staged augment proposal over the deck (`{deck_path, base_sha256, proposed_sha256, confirm:true}` — no client `tex`; preview-bound (`proposed_sha256` must match the staged digest → 409 if a newer preview replaced it), stale-diff guarded, re-screened, `.tex.bak` backup first). |
| `/api/deck/compile-available` | GET | Whether latexmk is installed (PATH-independent discovery). |
| `/api/deck/compile-fix` | POST | Opt-in Compile & Auto-Fix loop (SSE; `{deck_path, base_sha256, confirm:true, engine?}`): bounded `latexmk -no-shell-escape` passes + screened LLM log-repairs, every write verified by the next compile; the environment mirrors your suite's `latex-build.mk` (`TEXINPUTS` incl. `common/fig`, `BIBINPUTS` incl. the suite root, TeX bin dir on `PATH`). |
| `/api/deck/compile-preview` | POST | Compile the deck as-is for an in-app preview (`{deck_path, engine?}`): one plain latexmk pass under the deck-op lock — **never rewrites the `.tex`**, only the usual build artifacts. |
| `/api/deck/preview-pdf` | GET | Serve the compiled sibling `<base>.pdf` (rendered in-app via a blob: URL; requires the `X-Requested-With` header like every route). |
| `/api/deck/open-pdf` | POST | macOS-only: open the same validated PDF in the system viewer (fallback when inline PDF rendering is unavailable). |
| `/api/deck/native-pick-file` | POST | Native file picker (template selection). |
| `/api/deck/native-pick-folder` | POST | Native folder picker (output directory). |
| `/api/docgen/load-template` | POST | Validate an article template path and scan it → `{tex, macros, bib_keys_count, suite_root}` (the deck's twin for `\documentclass{article}`). |
| `/api/docgen/generate` | POST | Generate a vault-grounded LaTeX **article/report** (SSE: `info`/agent-trace frames + a terminal `{"doc": {tex, warnings, tex_path, tex_sha256, …}}`). Emit-only — scaffolds `<out_dir>/<slug>/` like the deck; per-section retries, salvage, checkpoint/resume. |
| `/api/docgen/generate-md` | POST | Generate a long-form **Markdown note and write it into the vault** (SSE; `{topic, …, scope_subdir?, confirm:true}`). Confirm-gated, refuses to overwrite an existing note (409), journalled — reversible from Note Refactor → Restore. Terminal `{"doc_md": {rel, op_id, …}}` frame. |
| `/api/docgen/doc-sections` | POST | List an existing article's `\section`s for the augment scope picker (`{deck_path}` → `{sections, deck_sha256}`). Read-only. |
| `/api/docgen/augment` | POST | Deepen / add a table / add a section / reformat an existing article (SSE; same body shape as the deck augment — the `deck_path` field name is deliberately shared). Writes nothing; stages the screened proposal server-side. |
| `/api/docgen/apply-augment` | POST | Write the staged article proposal (`{deck_path, base_sha256, proposed_sha256, confirm:true}` — preview-bound + stale-diff guarded + re-screened, `.tex.bak` backup first). |
| `/api/docgen/compile-available` | GET | Whether latexmk is installed (same discovery as the deck's). |
| `/api/docgen/compile-preview` | POST | Compile the article as-is for an in-app preview (`{deck_path, engine?}`) — **never rewrites the `.tex`**, only the usual build artifacts. |
| `/api/docgen/preview-pdf` | GET | Serve the compiled sibling `<base>.pdf` (blob-URL rendering, `X-Requested-With` required). |
| `/api/docgen/open-pdf` | POST | macOS-only: open the compiled article PDF in the system viewer. |
| `/api/docgen/compile-fix` | POST | Opt-in Compile & Auto-Fix loop for articles (SSE; `{deck_path, base_sha256, confirm:true, engine?}`): bounded latexmk passes + screened LLM log-repairs, every write verified by the next compile, `.tex.bak` first. |
| `/api/docgen/native-pick-file` / `native-pick-folder` | POST | Native pickers (template / output directory). |
| `/api/refactor/plan` | POST | Analyze a vault sub-folder (SSE: a `{"note": …}` frame per note + a terminal `{"refactor": …}` summary). Read-only — zero vision calls, zero vault writes. |
| `/api/refactor/extract-image` | POST | One user-triggered vision pass for a single image (`mode: table` / `describe` / `classify`). The only vision-calling refactor path; caches under `obsidian_cache/`. |
| `/api/refactor/review-note` | POST | One opt-in, scope-locked, advisory LLM prose/formatting review of a note (`{rel, scope_subdir?}`); writes nothing (502 on LLM error). |
| `/api/refactor/note` | POST | Re-analyze ONE note and return its fresh proposal frame (`{rel, scope_subdir?}`). Cheap counterpart of `/plan` for the per-image OCR-inclusion panel — no full re-plan, no vision, no writes. |
| `/api/refactor/sections` | POST | List a note's heading sections for the sub-note scope selector (`{rel, scope_subdir?}`). Read-only. |
| `/api/refactor/rewrite` | POST | One LLM formatting rewrite of a note/section (`{rel, scope_subdir?, section_index?}`); **stages** the proposal and returns a preview (`proposed`/`diff`/`proposed_sha256`). Writes only the staging cache (502 on LLM error). |
| `/api/refactor/custom-edit` | POST | Free-prompt single-shot edit (`{rel, scope_subdir?, section_index?, instruction}`); **stages** the proposal and returns the same preview shape (400 on empty instruction, 502 on LLM error). |
| `/api/refactor/pdf-refs` | POST | List a note's resolvable `.pdf` embeds for the summary action (`{rel, scope_subdir?}`). Read-only. |
| `/api/refactor/summarize-pdf` | POST | Summarize an attached PDF into a `> [!summary]` callout inlined beneath the embed (`{rel, scope_subdir?, pdf_rel}`); **stages** the proposal (502/422 on error). |
| `/api/refactor/chart` | POST | Advisory Mermaid diagram for a note/section (`{rel, scope_subdir?, section_index?}`); display only — never staged or written (502 on LLM error). |
| `/api/refactor/image` | GET | Read-only image bytes (`?rel=`). |
| `/api/refactor/apply` | POST | Vault write (requires `confirm: true`): callout-only batch note write with stale-diff + WYSIWYG guards. 503 if indexing holds the op-lock. |
| `/api/refactor/normalize` | POST | Vault write (requires `confirm: true`): deterministic formatting-fix batch write, same stale-diff + WYSIWYG guards as `apply` with `normalized_sha256` in place of `proposed_sha256`. |
| `/api/refactor/apply-staged` | POST | Vault write (requires `confirm: true`): write a staged LLM proposal (`{rel, scope_subdir?, action∈{rewrite,summarize_pdf,custom}, content_sha256, proposed_sha256}`). The proposed body is read from the server-side staging cache, never from the client; same stale-diff + WYSIWYG guards (op kind `llm_note`). |
| `/api/refactor/archive` | POST | Vault write (requires `confirm: true`): per-image move-out + thumbnail + embed swap, after a vault-wide reference check (409 `{shared:true}` if shared). |
| `/api/refactor/restore` | POST | Reverse an apply/normalize/llm-edit/archive (`{op_id}` or `{all:true}`) from the restore manifest. |
| `/api/refactor/manifest` | GET | List restore-manifest ops for the Restore UI. |
| `/api/refactor/native-pick-folder` | POST | Native folder picker (vault-relative scope). |
| `/api/refactor/ignore` | GET/POST | Read or update the sticky per-vault image ignore-list (sidecar under `obsidian_cache/`, never the vault). |
| `/api/refactor/flag` | GET/POST | Read or toggle a sticky per-image flag (`{rel, flag∈{strip,keep_handwritten}, action}`; sidecar under `obsidian_cache/`, never the vault). |

SSE routes send JSON token events, stream JSON error events when generation fails, and end with `data: [DONE]`. Agent-mode `/api/obsidian/chat` and `/api/deck/generate` additionally emit `iteration`, `thought`, `tool_call`, and `tool_result` events before the answer stream.

## Limits

- PDF upload limit: 500 MB.
- PDF extraction timeout: 600 seconds.
- System prompt limit: 4000 characters.
- Feedback string-field cap: 10 000 characters per field.
- Obsidian operation-lock TTL: set per acquisition by the caller — indexing / tagging / docgen acquire with 3600 seconds, the Note Refactor write routes with 600 seconds; long holders refresh by heartbeat.
- PDF extraction call cap: 1000 pages per call (`EXTRACT_MAX_PAGES_PER_CALL`). Vault indexing automatically splits larger PDFs into 1000-page extraction chunks and concatenates the result.
- Vault PDF size cap: PDFs above `PDF_MAX_PAGES` = 20 000 pages are skipped with a warning rather than risking out-of-memory during embedding.
- Vault image size cap: 20 MB per file. Larger images are skipped with a warning.
- Vault image extension list cap: 64 entries in `vault_image_exts`.
- Consecutive-failure circuit breaker: 20 consecutive insert failures abort an indexing run with the partial work preserved.
- Mid-run checkpoint: vault indexing persists every 500 inserts, bounding crash re-work to that many chunks. LlamaIndex checkpoint files are first written to a temporary directory and JSON-validated before they replace the active vector/docstore files, so an interrupted checkpoint should not truncate the last usable index.
- Agent loop budget: `vault_agent_max_iterations` (default 6, clamped 1–12). Two consecutive malformed tool-call iterations fall back to plain RAG.
- Agent tool-output caps: `vault_search` 12 000 chars (800 / snippet), `vault_read_note` 32 000 chars, `vault_list_materials` 20 000 chars.
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
- **"Embedding model mismatch" warning after switching to an online provider.** Online providers do not provide embeddings — the warning is from the indexed embedding model versus the currently selected local embed model. Re-select the matching local embedding model under `embed`. (Vault chat does not break in the meantime: it retrieves with the index's *own* recorded embedding model — warning once — rather than the mismatched selection, so results stay correct until you re-index.)
- **No tokens appearing on the Obsidian tab when chat is set to an online provider.** Confirm `obsidian_storage/docstore.json` exists (i.e. you have indexed at least once with a local embedding model). Online chat reuses the local index; an empty index returns "No relevant content found" by design.
- **Cost numbers look wrong.** Check `core/llm/usage.py` for the model's listed prices; the published rate can change between releases. Add an override under `llm_pricing_overrides` if you need to refresh it without waiting for an app update.

## Vector store backend (LanceDB)

By default the vault index uses LlamaIndex's JSON `SimpleVectorStore`. An optional binary **LanceDB** backend stores embeddings as float32 in Apache-Arrow files, cutting resident RAM and the GIL-held cold-start parse on large vaults. It is opt-in:

- **Migrate an existing index with no re-embedding** (recommended): close the app, then `python scripts/migrate_vector_store.py`. It joins the existing vectors to their nodes and bulk-loads LanceDB, archives the old JSON to `default__vector_store.json.bak`, and records the backend in `obsidian_meta.json`. A subsequent reindex reports `added=0` (nothing re-embedded).
- **Or build fresh on LanceDB**: set `"vault_vector_backend": "lancedb"` in `config.json` and reindex.

Retrieval results match the JSON backend (embeddings are unit-normalized so LanceDB's L2 search ranks identically to cosine). Requires `lancedb` + `llama-index-vector-stores-lancedb` (in `requirements.txt`); falls back to `simple` if absent.

The indexer inserts one chunk per LanceDB transaction, and each transaction writes a version manifest that lists every data fragment — so without maintenance the `_versions/` directory grows roughly with the *square* of the insert count (it can reach hundreds of GB over <1 GB of real vectors). To prevent this, the streaming indexer compacts and prunes superseded versions at each checkpoint **and** every few thousand inserts, keeping the on-disk footprint proportional to the vectors. If you have an index that was built before this maintenance existed and is already bloated, close the app and run `python scripts/compact_lancedb.py` once to reclaim the space.

## License

MIT.

