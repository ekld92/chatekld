# Library campaigns: advanced reference (2026-09-15)

All commands use the running server and its guards. They never start a scan.

Cross-report search includes the **Documents** population: native references without PDFs, standalone attachments, and saved scan entries whose current evidence is unavailable. Results open by stable document ID so repeated citation keys cannot select another reference. Saved-only files and orphan notes use separate filters and provenance and never authorize writes.

```bash
papermind library check
papermind notesync plan --key smith2020 --key jones2021
papermind notesync review CAMPAIGN_ID
papermind notesync inspect CAMPAIGN_ID
papermind notesync apply --plan CAMPAIGN_ID --decisions decisions.json --confirm
papermind notesync recover --plan CAMPAIGN_ID --decision recovery.json --confirm
papermind notesync settle --plan CAMPAIGN_ID --decision settlement.json --confirm
papermind notesync undo --plan CAMPAIGN_ID --decision undo.json --confirm
papermind library bibliography-plan --input corpus.json --provider ollama --model MODEL --max-tokens 2048
papermind library authenticate --input records.json
papermind library storage
papermind library bibliography-apply --plan CAMPAIGN_ID --decisions approved.json --confirm
```

- **Note campaigns (`notesync`):** Decisions are a JSON list of `{citation_key, evidence, approve: true, result}`. Preparation, review, and inspection work independently, while **`apply`, `recover`, `settle`, and `undo` return 501 in production** pending native and human acceptance (not a configuration flag).
  - **`inspect` & `settle`:** Rereads both destinations against the reserved result (`matching`, `unchanged`, `changed`, `unavailable`, `blocked`). `settle` records a completed dispatch only when both notes, the base record, identity, converter, and journalled writes match the approved result and postdate dispatch.
  - **`undo`:** Reverses a note applied on both destinations (restores the Obsidian snapshot, PATCHes the Zotero before-image under the version guard, and restores the pre-campaign base) only while both notes still carry the exact written image. Partial undos remain `undo_incomplete` and are never retried automatically.
- **Bibliography campaigns & PDF authentication (`library`):**
  - **Input & budgets:** JSON list of records (`id`, `current`, `sources` with `{id, locator, text}`, optional claims/PDF excerpts), capped at 20 records / 60,000 serialized chars per request with no provider fallback. Record decisions via `papermind library bibliography-decide --plan CAMPAIGN_ID --decisions fields.json --confirm` (requires `support_assessment: "supported"` and nonempty `reason`).
  - **PDF authentication (`papermind library authenticate`):** Deterministically scans up to 40 PDF pages / 400k chars for the record's DOI, title, and author surnames, returning `authenticated` (DOI on pages 1–2, or title with author surname within 1,500 chars), `partial`, `not_found`, `unavailable`, or `reference_list_only` (DOI past page 2 — cited paper trap).
  - **Metadata executor (`papermind library bibliography-apply`):** Writes approved fields via the version-guarded Zotero transport (`zotero_metadata_write`, reversible from Tagging → Restore). Refuses before PATCH if Zotero normalization would move an unselected field (e.g. promoting `Extra: Citation Key:` into native `citationKey`). Returns **501 in production** pending native validation.
- **Duplicates and versions:** Filename suffixes (`_1`, ` 2`, ` (1)`, `-1`, ` copy`) never prove identical bytes. **Manage PDFs (Files & attachments)** compares bytes, native Zotero annotations, embedded PDF annotations, note dependencies, and multi-key mappings. Different bytes are kept by default; recoverable removal refuses annotated copies unless explicitly acknowledged.
- **Storage control (`papermind library storage` / Library → Checks & campaigns → Storage control):** Read-only check over the saved Zotero observation listing stored attachments (`imported_*`), linked files outside the bibliography folder, broken/relative links, and duplicate linked paths with manual remediation steps for Zotero/Attanger. Writes nothing.

Current unresolved gates live in [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

### Current dependent links and optional DOI acquisition

- **`papermind library links`** (**Checks & campaigns → Inspect current PDF links in known notes**): Rereads reference notes from the saved scan (including orphan notes), resolves PDF wikilinks/Markdown links, and reports unavailable, symlinked, or replaced paths without walking the vault or writing (capped at 10,000 notes, 1 MB/note, 32 MB total).
- **Documents → Notes conflict / Notes requiring content review:** Reads the current note comparison on demand; source errors render as `unavailable`, never as zero conflicts.
- **Optional `--resolve-dois`:** Fetches exact-DOI catalogue metadata for up to 5 selected records via the fixed-host Crossref client within the 60,000-char corpus cap. Registry metadata does not verify PDF identity or citation support.
