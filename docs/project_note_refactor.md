# Note Refactor Tool — Design Notes

> Status: **Phase 1 + 1.5 + 2 shipped** (design dated 2026-06-22). Phases 1/1.5 are the
> read-only analyzer + hub; **Phase 2** added the app's first vault writers (callout-only
> Apply, per-image Archive, Restore). Phase 2 reconciliation (decided 2026-06-22): the bulk
> Apply is **callout-only** (insert the callout, keep the original embed) and **Archive is a
> separate, explicit, per-image action** — not the coupled "thumbnail + callout + marker,
> embed removed" shape sketched in §7. See §12 and the CHANGELOG.

A new ChatEKLD workflow that refactors a dedicated sub-folder of Obsidian notes: improves
formatting, inlines already-extracted OCR/vision text (reusing `obsidian_cache/`), extracts
tables from images, archives the original attachments out of the vault, and produces an
advisory cross-note discrepancy report. Mutates `.md` files **only after preview/approve**.

---

## 1. Audit findings that shaped the design

**Target folder** — `<vault>/<scope>/` (the scoped sub-folder)
- 135 markdown notes, ~0.7 MB of text, **no attachments stored inside the folder**.
- It is a sub-folder of the configured vault root (`<vault>`), so it is already covered
  by the index, and the OCR cache is keyed on that root (a stable per-vault key derived
  from the vault path).
- Content: dense, domain-specific study notes with idiosyncratic
  shorthand and Unicode (`EI`, `rsq`, `àà`, `°`, `∑`, `–››`). The shorthand must be
  **preserved**, never "normalized."
- Attachments are referenced, not stored locally: **1,793 markdown image embeds**
  `![](UUID.png)` (1,704 png + 89 jpeg), plus 23 wikilink embeds (19 png, **1 webp**,
  **3 mp3**). All links are local. The image files live in a shared attachments folder
  elsewhere in the vault.

**Coverage gaps**
- `.webp` is **not** in `vault_image_exts` (heic/jpeg/jpg/png) → that image was never described.
- `.mp3` has **no transcription pipeline** → audio cannot be inlined.
- **No PDFs are embedded in this folder** → the "PDF OCR reuse" idea does not apply here
  (it would elsewhere in the vault).

**OCR/vision reuse is durable but partial**
- Image descriptions persist at
  `obsidian_cache/image_cache/<vault_key>/<sha256-of-image-bytes>.txt` (plain text); ~1,765
  cached. Same text also lives in the LlamaIndex docstore.
- The current index is **partial/paused** (`obsidian_meta.json` → `phase: paused_partial`),
  so coverage of the 1,793 images is incomplete — the tool hits cache where present and runs
  vision live on misses.
- Cached descriptions were made with the generic *describe* prompt → **prose, not tables and
  not classified**. Table extraction and image classification require **fresh** vision passes.

**Model reality** — vision is **local-only** in this app (Ollama/LM Studio; online providers
are chat-only). Configured model: `gemma-4-e2b` (~4B) on LM Studio. Light for transcribing
drug doses / dense receptor diagrams; sets the error rate (see §6).

**Disk footprint (context, deferred)** — the app's own index store is **183 GB**, almost
entirely `obsidian_storage/lancedb/vectors.lance/_versions/` (61,836 manifest snapshots over
~0.7 GB of real vectors; ~O(n²) growth from one dataset version per insert with no
compaction/cleanup). This dwarfs the whole vault, so the original "save disk by deleting
attachments" motivation is misaimed by ~100×. **User deferred this** ("leave it for now").

**Architecture fit** — clean. Follows the established Audit/Deck workflow pattern. **Caveat:**
this is the **first** feature to write to the user's vault (indexer + audit are read-only), so
write safety is first-of-its-kind work.

---

## 2. Locked decisions (user, 2026-06-22)

| Decision | Choice |
|---|---|
| Scope | `<scope>/` only; vault root stays `<vault>` so cache keys match |
| Operations | inline OCR/descriptions · image→markdown tables · conservative formatting · frontmatter/link hygiene · advisory discrepancy report · handwritten-image skip + sticky ignore-list |
| Archive | move full-res original **out of the vault** (recoverable) + keep a small **in-vault thumbnail**; inline extracted text beneath |
| Safety | **preview/diff → approve → atomic write** |
| Vision model | **keep `gemma-4-e2b`** (accuracy risk mitigated in design) |
| 183 GB index | leave for now |

---

## 3. Module layout (follows Audit/Deck conventions)

- `api/routes/refactor.py` — `refactor_bp` blueprint, registered in `app.py`.
- `refactor/` package (app-coupled orchestrator):
  - `resolver.py` — embed → real file via the existing Obsidian shortest-path resolver;
    materializes iCloud placeholders before reading bytes.
  - `classify.py` — one cheap vision pass: `printed-table` / `figure-diagram` /
    `handwritten` / `photo` / `other`.
  - `extract.py` — description mode (cache-backed) + table mode (markdown table).
  - `hygiene.py` — conservative formatting, frontmatter normalization, broken-embed and
    orphan-attachment detection.
  - `discrepancy.py` — cross-note contradiction report (advisory).
  - `archive.py` — vault-wide reference check, move-out, thumbnail, restore manifest.
  - `plan.py` — assemble per-file proposed diffs.
  - `result.py` — shared result dataclass.
- `static/js/refactor.js` — `ui.js` + `api.js` imports only; wired into `app.js`.
- Config keys `refactor_*` in `core/config.py` with validators in `api/routes/config.py`.

---

## 4. Two-phase flow (matches "preview then approve")

1. **`POST /api/refactor/plan`** (SSE) — resolve → classify → extract (cache-backed) →
   build per-note proposed diffs + discrepancy report. **Writes nothing.**
2. **`POST /api/refactor/apply`** — writes only the diffs the user approved: atomic
   temp-sibling + `os.replace`, archive moves, thumbnails, and a `refactor_manifest.json`
   recording every move (original path → archive path → thumbnail → source note → content
   hash) so **un-archive/restore** is possible.

SSE reuses the existing `info` / `error` / `token` frames; adds per-image progress and
per-file diff frames, terminating with a structured `{"refactor": {…}}` summary and `[DONE]`.

---

## 5. Per-image pipeline

```
embed ─▶ resolve (materialize iCloud placeholder)
      ─▶ classify
          ├─ printed-table        ─▶ table-extraction prompt ─▶ markdown table
          ├─ figure/diagram/photo ─▶ description (reuse cache; live on miss)
          └─ handwritten          ─▶ "(handwritten — not transcribed)" placeholder
                                      + add to sticky ignore-list (also manually flaggable
                                        from the preview)
      ─▶ inline result beneath the (thumbnail) embed
```

**Cache reuse** — describe-mode reuses
`obsidian_cache/image_cache/<vault_key>/<sha256>.txt`. Classification and table modes need
fresh passes. Extend the cache key by **mode** (`<sha256>.<mode>.txt`) so each mode caches
independently and re-runs stay cheap.

---

## 6. Keeping `gemma-4-e2b` — accuracy guardrails baked in

Dose/table transcription from a 4B model carries real risk, so the design compensates:

- **Table pass skips the description-path downscale** (routes through a no-downscale,
  OCR-style call) to keep dense tables legible.
- **Double-read self-consistency:** extract each table twice; cells that disagree are flagged
  "suspect" in the diff.
- **Preview shows the full-res source image beside the extracted table**, numeric cells
  highlighted — nothing auto-approves.
- **Discrepancy pass cross-checks inlined doses against the same drug in sibling notes** — a
  transcription slip tends to surface as a flagged contradiction.

---

## 7. Archive mechanism

- **Reference-safety:** before moving any image, scan the **whole** vault (the vault root,
  not just the sub-folder) for other notes embedding that filename. A shared image is **not**
  moved — it is flagged instead.
- **Thumbnail:** reuse the PIL path already in `services/vision.py` to write a ~256–384 px
  thumbnail into an excluded in-vault `_thumbs/` folder so the note still shows a figure.
- **Recovery:** `refactor_manifest.json` (the one writable sidecar, like audit's
  `mapping.json`) enables restore.
- **Caveat to surface in UI:** archived originals leave iCloud. A default archive under
  `BASE_DIR` is local-disk only (Time Machine covers it; iCloud will not). Consider letting
  the user point the archive at a backed-up location.

Resulting note shape (chosen "external archive + small thumbnail"):

```md
![](_thumbs/7A27.png)   <!-- ~48 KB -->

> [!table] Extracted
> | Drug | Dose |
> |------|------|
> | mirtazapine | 15-45 mg |

<!-- full-res archived: 7A27.png -> <archive-dir> -->
```

---

## 8. Formatting / frontmatter / link hygiene (conservative)

- Heading levels, spacing around stacked images, real captions instead of UUID alt-text.
- Standardize `tags` / `related_notes` frontmatter.
- Flag broken embeds (filenames that resolve nowhere in the vault) and orphan attachments
  (images not referenced by any note — also archive candidates).
- **Never** touch the user's shorthand or wording.

---

## 9. Discrepancy report (advisory only)

- Group notes by tag/topic; extract atomic claims (drug → property → dose/mechanism); flag
  conflicts with note + line citations.
- High false-positive risk → **report only, never auto-edits**.
- Instruct the model to interpret the user's French shorthand; a small glossary asset may help.
- Operates file-first; the vector index is optional (and currently partial).

---

## 10. Write safety (first vault writer in the app)

- Atomic writes (`core/utils.write_text_atomic` pattern).
- Scope-lock every write/move to `<scope>/` (+ the `_thumbs/` and archive dirs);
  reject traversal/absolute escapes (mirror `api/routes/audit.py` validators).
- Vault-wide reference check before any move (§7).
- iCloud placeholder materialization before reading bytes.
- Nothing written until the user approves the diff; restore manifest for rollback.

---

## 11. Config keys (proposed)

`refactor_archive_dir`, `refactor_thumb_max_side`, `refactor_classify_model`,
`refactor_extract_model` (default = `vision_model`), `refactor_ignore_list` (path),
`refactor_discrepancy_max_iterations`. Add defaults to `core/config.py`; validate/clamp in
`api/routes/config.py` (drop-on-invalid pattern).

---

## 12. Suggested phasing

1. **Phase 1 — `plan` endpoint only (read-only).** ✅ *Shipped (commit 471d7dc).* Produces the
   full diff + discrepancy report, writes nothing. Lets the user judge `gemma-4-e2b`'s
   table/dose quality on real figures **before** any write capability exists.
1.5. **Phase 1.5 — central hub (still read-only).** ✅ *Shipped.* Additive review affordances on
   top of the analyzer, **zero vault writes**: (a) native folder picker beside the scope input
   (`POST /api/refactor/native-pick-folder` → vault-relative scope via `_abs_to_scope`, root +
   outside-vault rejected); (b) sidebar/detail master-detail UI rendering ORIGINAL vs PROPOSED
   markdown (vendored `marked` + `sanitiseHtml`, `textContent` fallback) plus the unified-diff
   view — the `{"note"}` frame now carries the `original`/`proposed` bodies; (c) `extract-image`
   `mode="classify"` (printed-table｜figure-diagram｜handwritten｜photo｜other, cached
   `<sha256>.classify.txt`) → a first-class "handwritten — can't OCR" badge, and a **sticky
   ignore-list** (`GET`/`POST /api/refactor/ignore`, rel-path-keyed JSON sidecar at
   `obsidian_cache/refactor/<vault_key>/ignore_list.json` — never the vault) that greys ignored
   images, drops them from the candidate counts, and suppresses their inlined callout in `plan`.
2. **Phase 2 — `apply` + archive/thumbnail/restore.** ✅ *Shipped.* The app's first vault
   writers, all opt-in and per-action-confirmed. Reconciliation vs the §7 sketch: the bulk
   **Apply is callout-only** (`refactor/apply.py` — insert the callout, keep the original
   embed; preview == apply via the shared `plan.analyze_note`), and **Archive is a separate
   per-image action** (`refactor/archive.py` — vault-wide reference check → move full-res out
   to `refactor_archive_dir` → ≤`refactor_thumb_max_side` PNG thumbnail in the excluded
   `<scope>/_thumbs/` → swap that one embed). `refactor/journal.py` owns the restore
   `manifest.json` + scope-lock + the `log_vault_write` audit trail; `restore` reverses any op.
   Stale-diff + WYSIWYG guards, atomic writes, the obsidian op-lock (503 while indexing), and
   resumable journalling are all in place. The §7 "thumbnail + callout + archived-marker, embed
   removed" single-shape transform was **declined** in favour of this two-operation split.
3. **Phase 3 — discrepancy-report polish** (glossary, grouping, dose cross-check tuning).

---

## 13. To verify in-repo before coding

- Exact LanceDB/docstore node shape for clean cache + text reuse
  (`rag/lancedb_store.py`, `rag/vault.py`).
- That the Obsidian shortest-path resolver is cleanly importable for the embed→file step
  outside the chat path.
