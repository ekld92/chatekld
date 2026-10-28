# Library workflows

## Daily vault and search update

Start with **Review library update** in **Library → Update my library**: check changes and associations, prepare text, review or skip names/tags/notes, apply selected changes, then confirm one final search update. Online OCR and online tag generation require separate approval. Preparation never updates the index automatically, and partial or uncertain writes pause continuation until you inspect the outcomes and start a fresh review.

- **Search-only update:** Use **Scan, settings and recovery → Search only: review update**.
- **Read-only status check:** **Search status → Refresh status** reads saved evidence without scanning or launching work.
- **Note-only edits:** If you only edited ordinary Obsidian notes, click **Update Index** directly in the **Ask** workspace (no Audit scan needed).
- **Settings & Reset:** Sidebar **Settings** houses model/extraction controls, **Library paths and audit settings**, **Index tools and history**, and **Advanced data controls → Reset App Data**.

### What each step updates

| Step | What changes | What it does not complete |
| --- | --- | --- |
| **Refresh library scan** | Saved Library reports under `BASE_DIR/audit/` | Does not repair links, prepare text, or update search. |
| **Review library update** | Extraction receipts and complete reusable text caches | Does not apply names/tags/notes or update the search index. |
| **Analyse changes** | Tracked import review and optional LLM tag proposals | Does not apply the proposed library changes. |
| **Apply selected** | Selected vault, Zotero, and Finder changes (`mirror → pdf: links → tags → renames → leftovers`) | Does not update search automatically. |
| **Review search index update** | Confirmed incremental vector + BM25 lexical search update | Does not resolve extraction exceptions or refresh Library reports. |

### Paths and recovery boundaries

1. Select the vault via **Ask → Change…** first.
2. In **Settings → Library paths and audit settings**, configure vault-relative paths for attachments, Zotero notes, and BibTeX (save attachments before picking its relative bibliographic PDFs subfolder), and absolute paths for `zotero.sqlite` and Zotero storage. Symlink escapes are refused.
3. **Timeouts & Cancellation:** A preview timeout starts no work; a missing start response may mean work started—check live status and History before retrying. `Stop` halts at safe boundaries without undoing completed cache promotions or library writes.

### The daily sequence

1. **Refresh the Library scan (if sources changed):** Expand **Scan, settings and recovery ? → Refresh library scan → Run Scan** and wait for completion.
2. **Choose online OCR & prepare text:** Set **Allow online OCR and figure analysis in the next preview?** (`Yes` / `No`), click **Review library update**, inspect the preview counts, and click **Authorize and prepare text** (or **Prepare available text**).
3. **Check preparation outcomes:** Open **Extraction exceptions and scope**. If all new sources are deferred, **No new source text prepared** appears—use the troubleshooting table below rather than re-clicking Prepare.
4. **Analyse changes (optional):** Click **Analyse changes**. If requesting tag proposals, select the session provider/model and spending limit separately. The first observation establishes an import baseline without proposing historical backlog papers.
5. **Review and apply selections:** Inspect **Names**, **PDF links in notes**, **Tags**, and **Notes mirror** (resolve ambiguous files via **Review document associations**). Tick desired rows and destinations (none are preselected; Zotero is the default tag target), then click **Apply selected** and confirm. Mirror rewrites require verified Git before-images.
6. **Update search index:** After inspecting apply results (or clicking **Skip proposals and review search update**), click **Review search index update** and confirm **Update using available text** or **Authorize and update**.
7. **Verify completion:** In **Activity → Details**, wait for both vector indexing and **Lexical search ready and saved**. Use **Ask → Indexed Materials → Refresh → Show** or **Index tools and history → History** to verify new documents.

### When to stop, inspect or retry

| What you see | Meaning and next action |
| --- | --- |
| **Slow click / no immediate progress** | Prepare stays disabled through observation, preview, and lexical preparation. On timeout or reload, inspect Activity and History—do not resubmit blindly. |
| **`partial`, `not_attempted`, or deferred docs** | Does not prove a corrupt PDF. Inspect saved diagnostics/exports under History before retrying or changing models. |
| **`initial_memory_pressure`** | Stopped at a system memory/swap limit (>512 MiB swap growth or critical pressure). Click **Check memory** to probe read-only before retrying. |
| **Provider auth, quota, or network error** | Fix the API key, quota, or runner connection first, then review a retry. |
| **Known failed/deferred source** | Routine runs skip unchanged known failures. Open **Extraction exceptions and scope → All extraction outcomes and targeted retries → Review documents → Review targeted retry** to confirm a single-source retry. |
| **Incorrect extracted text** | Click **Report bad extraction** to defer the source, fix the underlying issue, and run a targeted retry. |
| **Partial or uncertain library write** | Inspect per-destination receipts and recovery views before retrying remaining items; or mark resolved after manual inspection. |
| **Interrupted cache publication / index run** | Open **Index tools and history → History** and use **Finish interrupted search update** when offered with compatible evidence. |
| **Index incompatible with current settings** | Restore the recorded embedding model/provider in Settings; a full rebuild is a separate explicit decision. |
| **Vector finished, lexical still preparing** | Wait for BM25 sidecar completion before querying; do not launch a second update. |

## When a PDF needs a decision

An Audit mapping (`mapping.json`), a Zotero `linked_file` attachment, and a note's `pdf:` pointer are distinct records. Review candidate metadata and file identity before confirming a match or attachment. CHERQAiN never permanently purges Zotero items or deletes Zotero storage directly; duplicate PDFs carrying native annotations remain visible until you explicitly resolve the dependency.

## Where changes go and how to recover

| Change | Destination | Recovery starting point |
| --- | --- | --- |
| **Audit mapping or duplicate dismissal** | `BASE_DIR/audit/` (`mapping.json`, `dup_decisions.json`, `pdf_wanted.json`) | Clear or undo the decision from the Inventory, Not in Zotero, or Possible Duplicates view. |
| **Finder, Zotero, or note tags; note `pdf:` links** | Selected target store(s) | **Library → History & recovery → Tagging Restore** (`tagging/journal.sqlite3`). |
| **New mirror note or reviewed PDF/note rename** | Vault and `vault_history/` journal | **Settings → History and Restore** while the before-image remains unchanged. |
| **Mirror region rewrite** | Existing vault note | Restore from the exact verified Git commit (`git_restore_commit`) named in the receipt. |
| **Recoverable PDF removal** | Holding area and attachment journal | **Files & attachments → Restore** (inspect partial outcomes first). |
| **Zotero item or attachment moved to trash** | Zotero trash and `tagging/journal.sqlite3` | Restore from **Tagging → Restore** or Zotero Trash (never empty Zotero Trash during recovery). |

For control-by-control details see the [User Guide](static/help/guide.md), for headless commands see the [CLI guide](CLI_GUIDE.md), and for subsystem internals see [`tagging/README.md`](tagging/README.md).

## Inspect the full inventory

Open **Documents → Advanced decisions and historical reports → Inventory (advanced)**. Results render 100 records per page with full-corpus filtering and sorting; browsing pages never triggers a scan or modifies files.
