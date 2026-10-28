# CHERQAiN — User Guide

The main task tabs are **Ask** (Obsidian Agent), **Read** (Single Paper), **Library** (daily search updates and optional maintenance), and **Create** (Studio), plus **Plain Chat**, **Prompt Hub**, and **Help**. Use `?` badges beside controls for contextual detail and <kbd>⌘K</kbd> for the command palette.

| Task | Section |
| --- | --- |
| Set up models and ask a first question | [Getting started](#getting-started) |
| Summarise or chat | [Single Paper](#single-paper), [Plain Chat](#plain-chat), [Obsidian Agent](#obsidian-agent-vault-chat) |
| Update the vault and search each day | [Daily procedure](#daily-vault-and-search-update) |
| Import or review papers | [Library](#library--biblio-tagging), [Which Library action?](#which-library-action-should-i-use) |
| Create or edit material | [Studio: decks and reports](#studio-decks-and-reports) |
| Understand a status or undo a change | [Glossary](#library-audit-glossary), [Recovery](#recovery-options-and-limits) |

## Daily vault and search update

Start with **Review library update** in **Library → Update my library**: check changes and associations, prepare text, review or skip names/tags/notes, apply selected changes, then confirm one final search update. Online OCR and online tag generation require separate approval. Preparation never updates the index automatically, and partial or uncertain writes pause continuation until you inspect the outcomes and start a fresh review.

- **Search-only update:** **Scan, settings and recovery → Search only: review update**.
- **Read-only status check:** **Search status → Refresh status** reads saved evidence without scanning or starting work.
- **Note-only edits:** If you only edited ordinary Obsidian notes, click **Update Index** directly in the **Ask** workspace.

### What each step updates

| Step | What changes | What it does not complete |
| --- | --- | --- |
| Refresh library scan | Saved Library reports in application data | Does not repair links, prepare text, or update search. |
| Review library update | Extraction receipts and complete reusable text caches | Does not apply names/tags/notes or update the search index. |
| Analyse changes | Tracked import review and optional tag proposals | Does not apply the proposed library changes. |
| Apply selected | Selected vault/Zotero/Finder changes (`mirror → pdf: links → tags → renames`) | Does not update search automatically. |
| Review search index update | Incremental vector + BM25 lexical search update after confirmation | Does not resolve extraction exceptions or refresh Library reports. |

### Paths and recovery boundaries

Choose the vault via **Ask → Change…** first. In **Settings → Library paths and audit settings**, pick vault-relative paths for attachments, Zotero notes, and BibTeX (save attachments before selecting its relative bibliographic PDFs subfolder), and absolute paths for `zotero.sqlite` and Zotero storage. Symlink escapes are refused.

A preview timeout starts no work; a missing start response may mean work started—inspect live status and History before retrying. `Stop` halts at safe boundaries without undoing completed cache promotions or library writes.

### Before you begin

Wait for Zotero import, Attanger filing, and Better BibTeX export to finish. Keep the recorded local embedding model available. Full rebuild and **Re-extract even when text is already cached** are advanced operations, not daily steps.

### The daily sequence

1. **Refresh the Library scan (if sources changed):** Expand **Scan, settings and recovery ? → Refresh library scan → Run Scan** and wait for completion.
2. **Choose online OCR & prepare text:** Set **Allow online OCR and figure analysis in the next preview?** (`Yes` / `No`), click **Review library update**, inspect the preview counts, and click **Authorize and prepare text** (or **Prepare available text**).
3. **Check preparation before proceeding:** Open **Extraction exceptions and scope**. If all new sources are deferred, **No new source text prepared** appears—use the troubleshooting table below rather than re-clicking Prepare.
4. **Analyse changes (optional):** Click **Analyse changes**. If requesting tag proposals, choose the session provider/model and spending choice separately. The first observation establishes a baseline without proposing historical backlog items.
5. **Review and apply selections:** Inspect **Names**, **PDF links in notes**, **Tags**, and **Notes mirror** (resolve ambiguous associations via **Review document associations**). Select desired rows and tag destinations (none are preselected; Zotero is the default tag target), then click **Apply selected** and confirm. Mirror rewrites require verified Git before-images.
6. **Read the apply result, then update search once:** Click **Review search index update** (or **Skip proposals and review search update**) and confirm **Update using available text** or **Authorize and update**.
7. **Wait for the search result and check exceptions:** In **Activity → Details**, wait for the vector update and **Lexical search ready and saved**. Use **Ask → Indexed Materials → Refresh → Show** or **Index tools and history → History** to inspect new documents.

### When to stop, inspect or retry

| What you see | Meaning and next action |
| --- | --- |
| Slow click / no immediate progress | Prepare stays disabled through observation, preview, and lexical preparation. On timeout or reload, inspect Activity and History before retrying. |
| `partial`, `not_attempted`, or deferred documents | Does not prove a bad PDF. Inspect saved exports/diagnostics under History before retrying or changing models. |
| `initial_memory_pressure` | Preparation stopped at a memory/swap limit. Click **Check memory** to probe read-only before retrying. |
| Provider auth, quota, connection or service error | Fix the named provider issue first, then review a retry. |
| Known failed/deferred source | Open **Extraction exceptions and scope → All extraction outcomes and targeted retries → Review documents → Review targeted retry** to confirm a single-source retry. |
| Incorrect output that you inspected | Click **Report bad extraction** to defer that source; run a targeted retry after addressing the cause. |
| Partial or uncertain library write | Inspect per-operation receipts and recovery guidance before retrying remaining items. |
| Interrupted cache publication or search update | Open **Index tools and history → History** and use **Finish interrupted search update** when offered with compatible saved evidence. |
| Index incompatible with current settings | Restore the compatible configuration; a full rebuild is a separate decision. |
| Vector index finished, lexical search still preparing | Wait for BM25 completion before querying; do not launch another update. |

## When preparation stops or skips an image

Open **Extraction exceptions and scope** to inspect the affected file, HTTP status, error message, and next step. Rejected or oversized images are deferred during daily updates while other documents continue; use **All extraction outcomes and targeted retries** to retry a specific file.

## Studio and vault workspace

- **Vault audit & maintenance (`Library`):** Groups Library reports and installed maintenance checks.
- **Studio (`Create`):** Offers Document (slide deck or report), Action (generate or augment), and Format (LaTeX or Markdown). Markdown augmentation previews appended content (`≤120 KB` source cap, 7-day token expiry) before a separate Apply confirmation and saves a restore snapshot under [History and Restore](#history-and-restore).
- **Activity Console:** Bottom bar showing current progress and recent events.

## Update search without changing Library decisions

Use **Update Index** in the **Ask** tab for routine note updates. Local mode checks store compatibility; online mode reuses complete caches and previews eligible external calls. Advanced single-file or subset selection, forced re-extraction, and full rebuild live under **Settings → Index tools and history**. During vault indexing, cached PDFs also receive readable Markdown display companions under `BASE_DIR/readable_pdf_exports/<vault-key>/` outside the vault.

## Getting started

1. **Install and launch** on an Apple Silicon Mac; start Ollama or LM Studio for local models.
2. **Choose models:** Pull the configured embedding model (default `nomic-embed-text`) on Ollama before indexing, then pick a chat model (or set online API keys in `.env`).
3. **Set and index your vault:** Pick the vault folder via **Ask → Change…**, configure exclusions in Settings, and click **Update Index**.
4. **Ask a question** in **Ask** and inspect the **Sources** panel (`Read` and `Plain Chat` work without an index).
5. **Confirm writes explicitly:** Every vault, Finder, or Zotero write names its destination and undo location before executing.

## Obsidian Agent (vault chat)

Queries the indexed vault using hybrid (BM25 + dense) retrieval and a cross-encoder reranker by default. All retrieval toggles apply to the next Send with no reindex:

- **Agent mode:** Multi-step ReAct loop (`vault_search`, `vault_read_note`, `vault_list_materials`), plus optional Tavily `web_search` when using an online provider with `TAVILY_API_KEY`.
- **Paper labels:** Labels chunks from Audit-matched PDFs/notes as `"Author et al., year, title"` using the last completed scan.
- **Export MD / TXT:** Saves the conversation to `~/Downloads` with model, token/cost, source scores, and tool trace.

### Upgrading the reranker

For a multilingual FR/DE/EN vault, `BAAI/bge-reranker-v2-m3` improves cross-lingual ranking over the English default (`cross-encoder/ms-marco-MiniLM-L-6-v2`) with no reindex:

1. Set **Settings → Vault Chat · fine-tuning → Reranker model** to `BAAI/bge-reranker-v2-m3`.
2. **Quit and relaunch the app** (HuggingFace offline mode is decided at startup).
3. **Ask one question** while online to download and cache the model (~2.3 GB).

## Single Paper

Upload a PDF (trailing bibliography stripped automatically), select a preset (`concise` / `detailed`), study-design prompt, audience, or focus question, and stream a summary. Long papers exceeding the context window automatically use **map-reduce** (condensing up to 64 window-sized parts before final synthesis). Empty provider or map-part responses fail visibly rather than silently omitting text.

## Plain Chat

A stateless, RAG-free multi-turn conversation with the active model (browser holds the last 20 turns).

### Attaching a file

Attach or drop `.pdf`, `.png`/`.jpg`, `.md`, or `.txt` files onto a message:
- **Budgets:** Up to **4 files per message** and **240 000 characters of extracted text across the whole conversation** (default per-file limit: `120 000` characters under **⚙ Settings → Plain Chat**).
- **Server-side extraction at attach time:** PDFs run through the bounded subprocess extractor; images are described by your **local** vision model (Ollama or LM Studio required even when chatting with an online provider); `.md`/`.txt` are read directly.
- **Retention & Safety:** Attachments stay in context for 24 hours, disclose any truncation on the chip and to the model, and are wrapped as untrusted text.

## Library & Biblio Tagging

The Library Audit reconciles your vault against Zotero, local PDFs, and Finder tags. **Run Scan** is manual only; every report views the last completed scan. Each paper spans up to five places (`_master.bib`, Zotero item + reading notes, vault reference note, PDF under `biblio_articles/`, and the joined `Record`), with **three writable tag surfaces**: the reference note, PDF Finder tags, and the Zotero item.

### Library sections

| Section | What it shows |
| --- | --- |
| **Overview** | Separate counts for references, files, attachments, joins, and tag coverage. |
| **Documents** | Reference entries and physical-file entries with population/association filters, PDF preview, and **Files & attachments** actions. |
| **Notes** | Zotero Notes Sync comparison and mirror controls. |
| **Checks & campaigns** | Storage control, dependent PDF link inspection, bibliography campaigns, and (in private builds) Maintenance checks. |
| **History & recovery** | File operations, Tagging restore, and durable campaigns. |

- **Storage control (`Checks & campaigns → Storage control`):** Lists attachments stored inside Zotero, linked files outside `biblio_articles/`, broken/relative paths, and doubly-linked files, with manual remediation steps.
- **Duplicate copies (`Files & attachments`):** Shows byte equality, native Zotero and embedded PDF annotation counts, note references, and `_1`-style filename collision clues. Copies carrying native annotations are never auto-merged or removed without explicit acknowledgement.

### Which Library action should I use?

| If you want to… | Start with… |
| --- | --- |
| See the current state of PDFs, Zotero items, notes, and tags | **Overview → Run Scan**. |
| Process normal new Zotero Connector imports | **Update my library**, after Attanger and Better BibTeX finish and a scan completes. |
| Inspect or decide one ambiguous PDF, duplicate, attachment, or missing note | **Documents** or **Checks & campaigns**. |
| Recover an earlier partial or confirmed write | **History & recovery**, then the operation-specific Restore view. |

### Update my library (one maintenance run)

Follow [the daily sequence](#the-daily-sequence) through text preparation first, then:

1. **Analyse changes:** Identifies new/changed entries, attachments, and reading notes since the baseline, proposing filenames and optional LLM tags (bounded by an explicit session spending limit or **No spending limit**).
2. **Review:** Nothing is pre-ticked. Inspect proposed names, note `pdf:` links, tags, and the mirror preview.
3. **Apply selected:** Executes in strict order: `1. Note mirror` → `2. Note pdf: links` → `3. Tags (Finder / Zotero / Note)` → `4. PDF renames` → `5. Approved leftover imported attachments (moved to Zotero Trash)`. Uncertain outcomes block replay until inspected and settled via **Mark resolved…** (`cherqain library run resolve`).

### What writes where (and how to undo it)

| The verb | Writes | Undo |
| --- | --- | --- |
| link / map / assign (accession lists & decks, key picker, review workspace) | `mapping.json` — one PDF↔key pair | ⌫ on the deck, or **[unmap]** on the Inventory pair |
| pending / not a paper (decks, PDF workspace, review-workspace curation) | `mapping.json` — the curated queue | **[clear]** on the PDF workspace tab |
| [unmap] / [never auto-map] (Inventory drawer) | `mapping.json` — match exclusions | **[clear]** on the PDF workspace tab |
| Confirm / Wrong match (PDF workspace / PDF identification review) | the local review store only | **[undo]** (or ⌫) on the same card |
| attach (Zotero Attachments, review workspace) | a Zotero linked-file attachment | Web: eligible Tagging → Restore moves it to trash; Desktop: automatic create undo is unavailable — inspect the attachment in Zotero |
| [remove from Zotero] (Zotero Attachments) | moves the attachment to Zotero's trash | Tagging → Restore |
| [take Zotero's] (Notes Sync) | the marked region of one vault note | Settings → History and Restore |
| [keep mine] (Notes Sync) | the Zotero reading note, via the web API | Tagging → Restore |
| [mirror from Zotero] / [refresh all from Zotero] (Notes Sync, Zotero-authoritative mode) | creates missing notes, rewrites differing marked regions, records sync points | creates: Settings → History and Restore; rewrites: the vault's git, from the commit the result names |
| Apply selected (Update my library) | note mirror, `pdf:` lists, tags (Finder + Zotero + note), renames, approved leftover imported attachments | mirror: as [mirror from Zotero]; `pdf:` lists, tags, and leftover attachments: Tagging → Restore; renames: Settings → History and Restore |
| create note (Notes Sync) | a new vault note / a new Zotero reading note | Settings → History and Restore / Web: eligible journal restore; Desktop create undo unavailable |
| tag verbs — apply, [→ note], [→ Finder], harmonize, retire, × | note frontmatter / Finder tags / Zotero item tags / `_tags.md` | Tagging → Restore (bulk runs also offer a one-click undo in place) |
| Apply (Zotero vs Note Tags) | one note's `tags:` frontmatter | the `.bak` written beside the note |
| [not a duplicate] / "needs no PDF" dismissals | decision sidecars in app data | the same row's undo verb |

Four overloaded words in the interface: **pending** (the `mapping.json` "add to Zotero later" queue vs an un-reviewed card), **confirm** (an inline write confirmation vs **Confirm** in PDF identification review), **map / link** (pairing a PDF in `mapping.json`), and **attach** (creating a `linked_file` attachment item inside Zotero).

### The report tabs

- **Zotero Notes Sync:** Compares each Zotero reading note with the `<!-- zotero-note:begin/end -->` marked region of the vault reference note.
  - When **Zotero is authoritative** is on, **[mirror from Zotero]** and **[refresh all from Zotero]** pull changes from Zotero after verifying that Git holds the exact current text of every replaced note (`git_restore_commit`).
  - When off, each row offers **[take Zotero's]** (pull, undo via *Settings → History and Restore*), **[keep mine]** (push via Web API, undo via *Tagging → Restore*), and **[merge…]**, plus side-by-side word diff **[preview]**, **Baseline**, and **Migrate layout**.
- **Zotero Attachments:** Lists stored and linked files in Zotero. **[remove from Zotero]** moves an attachment to Zotero Trash (undo: *Tagging → Restore*). **Title = filename** normalizes attachment titles in a previewed batch.
- **Tagging:** Propose and apply `_tags.md` tags in the keyboard-first **Triage deck** (`P` propose · `A` accept · `S` skip · `M` defer · `V` PDF · `⌫` undo) or **Table** view. Click `×` on any tag to remove it from that store.
- **Tag Spellings & Vocabulary:** Cross-store frequencies grouped by fingerprint, with per-variant **[retire]** buttons (removes the tag across notes, Finder, Zotero items, and optionally `_tags.md` as an undoable batch).
- **Inventory:** 100 rows per page with full-inventory filtering/sorting, PDF **[view]**, and a **[detail]** drawer containing **[unmap]**, **[never auto-map]**, and **[write these into the note as a `pdf:` link]**.
- **Ambiguous PDFs & PDF workspace:** Inspect and confirm file identity (`C` confirm / `W` wrong match), set curation (`[add later]`, `[exclude from registration]`, `[clear curation]`), or **Copy confirmed DOIs**.
- **Unmapped PDFs → Identify sources online:** Opt-in keyless lookup (`_master.bib` offline pre-check → Crossref / Europe PMC → arXiv / HAL / theses.fr) producing **Re-links** and **Accessions**.
- **Note vs Finder vs Zotero Tags / Zotero vs Note Tags / Finder vs Note Tags:** Fingerprint and synonym-aware tag drift views with additive **Harmonize selected**, **Apply**, **[→ note]**, **[→ Finder]**, and **[+ vocab]** fix actions.
- **Possible Duplicates:** Multi-tier near-duplicate detection (`page1_text`, `identifier`, `bib_doi`, `multi_pdf`, `record_title`, `content_similarity` at `audit_near_dup_ratio`), with undoable **[not a duplicate]** dismissals.

### Bibliography and Note campaigns & Desktop widgets

- **Bibliography campaigns (`Checks & campaigns → Bibliography campaigns`):** Prepare and review source-grounded metadata judgments (`library authenticate`, `library bibliography-plan`, `library bibliography-decide`; `bibliography-apply` returns 501 in production). Optional `--resolve-dois` queries up to 5 explicit DOIs on Crossref.
- **Note campaigns (`notesync plan / review / inspect`):** Inspect three-way merge state across destinations (`apply`, `recover`, `settle`, and `undo` return 501 in production).
- **Search across Documents & Dependent PDF links:** Search spans all cached reports and Documents; **Inspect current PDF links in known notes** (`cherqain library links`) checks note PDF pointers without walking the full vault.
- **Desktop widgets:** SwiftBar and Übersicht widgets (`packaging/widgets/`) display `widget.json` (paper of the day, tag suggestion, drift counts, notion of the day) updated after each manual scan.

## Library Audit glossary

### Core nouns & Matching rules

| Term | Meaning |
| --- | --- |
| **Citation key / Record** | The Better BibTeX key (`smithTitle2020`) and everything joined to it (`_master.bib` entry, Zotero item, vault reference note, and matched PDFs). |
| **Surface** | One tag store: **Reference note**, **Finder (PDF)**, and **Zotero item** are writable; `.bib` `keywords` and Zotero child-note tags are read-only. |
| **`mapping.json` & Curated decisions** | App-data curation file holding `manual` / `manual_hash` / `accession` links, `no_match`, `pending`, and detach exclusions (`[unmap]`, `[never auto-map]`). |
| **Match rules (priority order)** | `zotero_link` (unique Zotero `linked_file`, beats `mapping.json`) → `manual` / `manual_hash` / `accession` → `fm_pointer` → `fm_authoryear` → `wikilink` → `doi` (embedded metadata) → `authoryear` → `doi_text` (page-1 printed DOI gap-fill). When Zotero observation succeeded, an active PDF without a Zotero `linked_file` can only be claimed by manual curation or DOI (`3.5` / `4.5`). |
| **Dead curation** | `mapping.json` entries pointing at missing files/keys, overridden by `zotero_link`, or Zotero links with unexported BBT keys / multi-item claims. |
| **Triangulated** | Records holding all four: bib entry, matched PDF, reference note, and Zotero item. |
| **Sync point & Marked region** | Saved Notes Sync baseline and the `<!-- zotero-note:begin/end -->` block inside the reference note. |
| **Identity / Association / Attachment** | **Identity:** confirmed paper match in `pdf_reviews.sqlite3`; **Association:** key pairing in `mapping.json`; **Attachment:** Zotero `linked_file` item. |

## Public-build capability limits

The public edition omits private Maintenance, cleanup executors, and experimental campaign writes (`501`). Public Library observation, document review, and supported tagging/notes/Studio actions remain available with their confirmations and recovery journals.

## Studio: decks and reports

Choose **Create** to generate or augment a **Beamer slide deck**, **LaTeX article/report**, or **Vault Markdown note/deck** grounded in the vault:
- **LaTeX path (emit-only):** Reuses your template's preamble, `.sty` macros, and `.bib` keys to scaffold `<slug>/<slug>.tex` + `Makefile`. Optional **Preview PDF** runs one `latexmk -no-shell-escape` pass without editing `.tex`; **Compile & Auto-Fix** runs a bounded LLM log-repair loop (`.tex.bak` saved first).
- **Augment & Piecewise Revision:** Deepen, add a table/section, or reformat an existing document (or use **Revise section by section** / **Try section-by-section repair** on large documents; over-cap or failed sections are skipped and left unchanged).
- **Markdown path:** Creates a new vault note or appends previewed sections after confirmation, journalled under **Settings → History and Restore**.

## History and Restore

Open **Settings → History and Restore** to inspect and revert journalled vault changes (created notes, Markdown augmentations, `[take Zotero's]`, PDF renames, and retained historical note edits). Notes Sync mirror rewrites rely on their verified Git before-images (`git_restore_commit`) rather than this journal.

## Prompt Hub & provenance

The **Prompt Hub** tab displays the latest effective system/user prompt per workflow (read-only, keys redacted). Every export and generated `.tex` / `.md` file carries a provenance header or frontmatter recording the provider, model, and usage.

## Writing good prompts

- **Be specific and decompose multi-part questions:** Use single-shot *Strict* or *Balanced* mode for targeted lookups; use **Agent mode** for cross-note synthesis.
- **Include key terms in the source's language:** In a multilingual FR/DE/EN vault, adding key terms in the target language helps both dense and BM25 lexical retrieval.
- **Keep custom system prompts brief and positive:** Two to four sentences steering style or format (`Réponds sous forme de tableau…`) work best without crowding the context window.

## Tips & limits

- **Embedding model consistency:** Keep the recorded embedding model across incremental updates; changing it requires a full rebuild.
- **"Too many streaming operations are already running."** At most 12 streaming jobs run concurrently; wait for an active stream to finish or close an unused tab.
- **PDF & Image Bounds:** PDFs are capped at 20 000 pages (extracted in 1 000-page ranges with a 600 s timeout); vault images are capped at 20 MB.
- **Network Surface:** Loopback-only server; outbound connections occur only for selected LLM providers, Zotero, one-time HuggingFace/model-seed downloads, and opt-in Tavily `web_search` or keyless bibliographic lookup.

## Recovery options and limits

- Use **Settings → History and Restore**, **Tagging → Restore**, **Files & attachments → Restore**, or the verified Git commit (`git_restore_commit`) named by a mirror receipt.
- **Zotero Desktop Authorization:** Click **Authorize Zotero Desktop…** in Tagging or Pending PDF review while Zotero 10+ is open (`Allow` = one write; `Always Allow` = session). Lost responses remain in **Tagging → Restore** as `outcome unknown` and are verified before any undo.
- **Reset App Data** (sidebar Settings) clears uploads and the local index without touching your vault.

## Verification boundaries

Automated Chromium tests do not replace native macOS WKWebView, VoiceOver, or live provider validation. See `PROJECT_REFERENCE.md` and `KNOWN_ISSUES.md` in the source checkout.

## Daily extraction and Library workflow (26 October 2026)

Follow [Daily vault and search update](#daily-vault-and-search-update) at the top of this guide, and consult [When to stop, inspect or retry](#when-to-stop-inspect-or-retry) for targeted retries, memory stops, and interrupted cache promotion.
