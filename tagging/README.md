# Biblio Tagging, Accession & Zotero Write Contracts (`tagging/`, `accession/`, `notesync/`, `vault_history/`)

This document covers the confirm-gated write subsystems backing the **Library** workspace (`tagging/`, `accession/`, `notesync/`, and `vault_history/`). For read-only scan internals and the 18 Audit reports, see [`audit/CLAUDE.md`](../audit/CLAUDE.md); for the step-by-step user procedure, see [`LIBRARY_WORKFLOWS.md`](../LIBRARY_WORKFLOWS.md).

## 1. Biblio Tagging (`tagging/`)

`tagging/` turns the last completed Audit scan's per-citation-key `Record` join into a controlled-vocabulary tag writer across three targets:

1. **Obsidian Zotero Note Frontmatter:** YAML `tags:` list under `audit_zotero_notes_subdir`.
2. **macOS Finder Tags:** Extended attributes (`com.apple.metadata:_kMDItemUserTags`) on the PDF under `biblio_articles/`, implemented in `api/routes/_finder_tagwrite.py` to keep `audit/` read-only.
3. **Zotero Parent Item:** Version-guarded (`If-Unmodified-Since-Version`) HTTP PATCH via `tagging/zotero_write.py` — **never** direct writes to `zotero.sqlite`.

### Submodules & Data Flow

- **`vocab.py`:** Parses `_tags.md` (`vault_thesaurus_tags_path`) into `{tag: gloss}`. Every forward tag write (`apply-note-tags`, `apply-finder-tags`, `apply-zotero-tags`, `apply-all`) validates tags against this controlled vocabulary server-side.
- **`text_source.py`:** Cache-first PDF text reader (reuses `obsidian_cache/pdf_cache/<vault>/` and writes/consults `<sha256>.notext` negative-cache markers); backs `/api/tagging/snippet` and the LLM tag proposer.
- **`propose.py` & `session_llm.py`:** Vocabulary-constrained LLM tag proposer (`tagging_propose_model`, `tagging_propose_max_tokens`, `tagging_propose_temperature`). In staged Library sessions (`library run`), proposals use a deep-copied session config with fallback disabled and an explicit USD budget (`--budget-usd` or `--no-limit`).
- **`journal.py` (`BASE_DIR/tagging/journal.sqlite3`):** Self-contained SQLite/WAL restore journal outside the vault. Records before-images for every tag addition, single-tag deletion (`×`), cross-store tag retirement (`/api/tagging/retire-tag`, batch-journalled with `batch_id`), vocabulary additions (`add-vocab-tag`), and Zotero attachment trash/link ops.
- **UI Views (`static/js/tagging.js`):**
  - **Triage Deck:** One card at a time (untagged first, PDF excerpt) with keyboard shortcuts (<kbd>P</kbd> propose, <kbd>1</kbd>–<kbd>9</kbd> toggle chips, <kbd>A</kbd> accept to enabled targets, <kbd>S</kbd> skip, <kbd>M</kbd> defer, <kbd>V</kbd> full PDF preview, <kbd>⌫</kbd> undo).
  - **Table View:** Side-by-side overview with per-target `[→note]`, `[→finder]`, and `[→zotero]` buttons.

### Zotero HTTP Transports & Safety (`tagging/zotero_write.py`)

Configured via `tagging_zotero_transport` (`auto` | `local` | `web`, default `auto`):

- **Local Desktop API (`local`):** Targets Zotero 10+ Desktop over loopback after explicit user approval via **Authorize Zotero Desktop…** (session token held in memory only). Local item keys, versions, and restore records are bound to the observed Zotero server/database ID and never transfer to the Web API. Reserved-key attachment creation (`version: 0`) is rejected by Zotero 10.0.1/10.0.2 (`primaryData not loaded`); CHERQAiN preserves the reservation in `tagging/attachment_intents.sqlite3` and does not retry unkeyed or switch transports.
- **Web API (`web`):** Uses `ZOTERO_API_KEY` from `.env` against `api.zotero.org`.
- **Restore Guardrails:** Restore verifies the complete recorded Zotero tag array (including automatic-tag types) and target version/content before reverting; hand-edited notes or modified Zotero items are skipped rather than clobbered.

## 2. Bibliographic Resolution & Pending Review (`accession/`)

- **Keyless Catalogue Lookup:** Driven manually from the **PDFs Without Zotero** and **Not in Zotero** reports (`accession_enabled`, default `off`). Queries Crossref, Europe PMC, arXiv, HAL, and theses.fr for articles, plus opt-in Open Library (`accession_open_library`) and Google Books (`accession_google_books`, disclosed separately) for ISBNs.
- **Zero-Egress Pre-Check & Privacy:** Extracted DOIs are checked against `_master.bib` locally first (~74% hit rate at zero egress). Network lookups send only a DOI or title + first-author surname — never PDF bytes or full page text. Results are cached by file SHA-256.
- **Pending PDF Review (`BASE_DIR/audit/pdf_reviews.sqlite3`):** Per-file identity confirmations live independently of the disposable identification cache. Opening a PDF records its local identity (`same_identity`, ignoring volatile `dev`); changed bytes require fresh review. **Copy confirmed DOIs** copies unique confirmed DOIs for Zotero's *Add Item by Identifier* without marking items imported.

## 3. Staged Daily Library Orchestrator (`accession/library_run.py`)

Used by both **Library → Update my library** and `cherqain library run`:

1. **Observe & Baseline:** Requires a completed manual Audit scan. First run records an import baseline without proposing historical backlog papers.
2. **Prepare Text (`/api/online-ocr/session/*`):** Mode-bound `intent-v1:` token (`prepare_text`). Populates reusable extraction caches without updating the search index.
3. **Analyse Proposals (`library run analyze`):** Gathers filename proposals (when private Maintenance renamer is present), optional LLM tag proposals, note `pdf:` link updates, and the Zotero → Obsidian Notes Sync mirror plan (`plan_digest`).
4. **Apply Selected (`library run apply`):** Executes confirmed selections in strict order:
   `1. Notes Sync mirror` → `2. Note pdf: links` → `3. Tags (Finder / Zotero / Note)` → `4. PDF renames` → `5. Approved leftover imported attachments (trashed in Zotero)`.
5. **Search Update (`update_search`):** Separate confirmed incremental index + BM25 lexical update after apply outcomes are verified clean.

## 4. Notes Sync Mirror & Vault History (`notesync/`, `vault_history/`)

- **Notes Sync Mirror (`notesync/`):** Compares Zotero child reading notes against exported Obsidian notes under `audit_zotero_notes_subdir`.
  - **Missing notes:** Created with `pdf:` links and ticked tags (reversible via **Settings → History and Restore**).
  - **Existing note mirror region rewrites:** Have **no universal Restore snapshot**; each target file must first be proven recoverable from a verified Git commit (`git_restore_commit`).
  - **Two-way manual sync (when authoritative mode is off):** `[take Zotero's]` restores from **Settings → History and Restore**; `[keep mine]` (pushes to Zotero) restores from **Tagging → Restore**.
- **Vault History (`vault_history/`):** Maintains the operation-locked, `fsync`-backed restore manifest (`/api/history/manifest`, `/api/history/restore`) for Studio Markdown note creation (`create_note`), Markdown augmentation, PDF/note renames, and retained historical note operations (`refactor_archive_dir`). Note Refactor analysis/editing routes are retired, while all historical journals remain restorable.
