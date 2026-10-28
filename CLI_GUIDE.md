# CHERQAiN CLI and agent runbook

For the daily desktop procedure, start with [Daily vault and search update](LIBRARY_WORKFLOWS.md#daily-vault-and-search-update). A Library CLI run does not automatically prepare extraction caches or refresh the search index.

> [!IMPORTANT]
> **Authorization & Safety:** Read the current output of each step before choosing the next command. Never treat historical plans, cached reports, or model output as permission for a live scan, library write, application restart, or paid provider call.

## Index admission and durable online evidence (24 October 2026)

- **Index Admission:** Routine index updates refuse model/backend, version, vault-identity, and interrupted-store incompatibilities before opening or archiving the store. A full rebuild requires its own confirmed request (`force_fresh`) and preserves the previous index archive.
- **Online OCR Runs & Recovery:** Read-only `/api/online-ocr/runs` and `/api/online-ocr/runs/<run_id>` return bounded summaries for the active vault. Recovery uses `session/start` with `resume_run_id`, `session/continue-incomplete` with the exact sorted omission list, and `session/continue-index` after landed-hash reconciliation. Old receipts stay immutable and cannot be resumed under a changed binding.

## Storage selection

`CHERQAIN_BASE_DIR` selects the application data directory at startup (default `~/Library/Application Support/ChatEKLD/`, with `CHATEKLD_BASE_DIR` as legacy fallback). `CHERQAIN_DB_PATH`, `CHERQAIN_CACHE_DIR`, and `CHERQAIN_INDEX_DIR` override individual subpaths without migrating existing data. See [`core/README.md`](core/README.md) for the complete storage map.

## Prepare the command and server

Run from the checkout's Python 3.12 virtual environment:

```sh
python -m cli --help
python -m cli library --help
python -m cli library run --help
```

- **CLI & MCP Installers:**
  - `python scripts/install_cli.py`: Installs a `cherqain` shim in `~/.local/bin` (pass `--name papermind` for the legacy name).
  - `python scripts/install_mcp.py`: Registers the stdio MCP server (`cherqain mcp`) with installed clients.
- **Server Discovery:** CLI and MCP talk to the running GUI or `cherqain server` over loopback (`CHERQAIN_API_URL` → `CHATEKLD_API_URL` → `BASE_DIR/server.json`). `cherqain server` refuses to start beside a live GUI.
- **Native Trial Flag:** Set `CHATEKLD_SKIP_PROVIDER_STARTUP=1` on `launch.py` to suppress launch-time Ollama/LM Studio startup and shutdown during isolated native trials.

### MCP availability

| MCP tools | Effect |
| --- | --- |
| `audit_summary`, `audit_report`, `audit_inventory`, `notesync_status` | Read saved observations or current status. |
| `audit_library` | Starts a live Audit scan; writes application reports under `BASE_DIR/audit/`. |
| `vault_search`, `vault_ask`, `summarize_paper`, `biblio_propose_tags`, `generate_deck` | Retrieve, analyze, or generate (may call a model or write an output project/cache; `vault_search` runs a synthesis). |
| `biblio_apply_tags`, `generate_document` | Write selected library/vault content (or emit a LaTeX project when `mode=latex`). Call only with explicit user approval. |

## Effects and command families

### Explicit online PDF and image transcription

The desktop **Ask** and **Library** tabs preview eligible PDF pages or vault sessions before any external OCR/vision call. For standalone single-file execution outside the index (sends images externally and can incur charges; never indexes automatically):

```sh
python scripts/extract_pdf_pages.py VAULT/path/to/paper.pdf \
  --input-root VAULT --storage APP_DATA/online_ocr_attempts \
  --exports APP_DATA/online_ocr_exports \
  --provider deepseek --model deepseek-flash

python scripts/extract_image_online.py VAULT/path/to/figure.heic \
  --input-root VAULT --storage APP_DATA/online_ocr_attempts \
  --provider deepseek --model deepseek-flash
```

Use `--retry-page N` (PDF) or `--retry` (image) for a bounded second attempt; use `--reextract` to replace existing cached text.

| Family | Representative commands | Effect to expect |
| --- | --- | --- |
| **Inspect state** | `status`, `audit status`, `audit wait`, `audit summary`, `audit report NAME`, `library check`, `library links`, `library storage`, `library run show SESSION`, `notesync inspect ID`, `zotlink status` | Reads saved evidence. Cached Audit reports reflect the last completed scan, not live disk state. |
| **Start observation** | `audit scan`, `library authenticate --input FILE`, `accession identify --pdf REL` | `audit scan` reads the live library into memory/cache; `accession identify` queries external bibliographic catalogues when enabled. |
| **Generate / preview** | `vault ask`, `paper summarize`, `deck generate`, `docgen generate`, `tagging propose`, `library bibliography-plan`, `library run analyze`, `maintain run --preview` | Calls a model or service. Note: `docgen generate` defaults to **Markdown** (writes a new vault note); inspect `--mode`. |
| **Record curation** | `audit mapping`, `audit pdf-decision`, `audit dup-decision`, `library bibliography-decide`, `cleanup decide`, `cleanup edit` | Writes curation sidecars (`mapping.json`, `pdf_wanted.json`, `dup_decisions.json`) or campaign decisions. |
| **Apply or recover** | `library run apply`, `library run resolve`, `notesync mirror --confirm`, `pdfs apply`, `pdfs restore`, `tagging apply`, `zotlink attach`, `zotcreate create` | Mutates vault files, Finder tags, Zotero, or journals. Inspect partial/uncertain outcomes before retrying. |

- **Top-Level CLI Groups:** `paper`, `vault`, `audit`, `pdfs`, `accession`, `library`, `tagging`, `notesync`, `zotlink`, `zotrecord`, `zotcreate`, `deck`, `docgen`, `maintain`, `cleanup` (private), `config`, `server`, `mcp`.
- **Exit Codes:** Shared JSON commands exit `0` on success, `1` on refusal or `{"ok": false}`, and `2` when the server is unreachable. Note: standalone `python -m audit --check NAME` is a diagnostic printer that always exits `0` on findings; `cherqain maintain run` (private build) exits `1` on findings.

## Reviewed Library session from the CLI

Wait for Zotero, Attanger, and Better BibTeX before scanning. The first analysis records an import baseline without proposing historical backlog items:

```sh
cherqain audit scan
cherqain audit wait
cherqain library run new --label 'New imports'
# Optional tag proposals (requires explicit --budget-usd N or --no-limit for online models):
# cherqain library run new --provider PROVIDER --model MODEL --budget-usd LIMIT
cherqain library run analyze SESSION
cherqain library run show SESSION
```

Copy approved unit IDs and the reviewed mirror `plan_digest` into `reviewed.json`:

```json
{
  "mirror": {"plan_digest": "COPY_FROM_REVIEWED_ANALYSIS", "create_keys": []},
  "pdf_links": [],
  "tags": [],
  "renames": [],
  "leftovers": []
}
```

Apply and inspect results (`mirror → pdf_links → tags → renames → leftovers`):

```sh
cherqain library run apply SESSION --approvals-file reviewed.json --confirm
cherqain library run show SESSION
```

If a step is marked `uncertain`, inspect its target store and recovery journal first, then settle with `cherqain library run resolve SESSION UNIT STEP --status done|failed --confirm`. See [Library workflows](LIBRARY_WORKFLOWS.md) and [`tagging/README.md`](tagging/README.md).

For standalone Zotero → Obsidian note mirroring (requires verified Git before-images for existing-note rewrites):

```sh
cherqain notesync mirror
cherqain notesync mirror --confirm --plan-digest REVIEWED_DIGEST
```

## Other CLI entry points

- **`cherqain audit mapping --help`:** Link, detach, mark `no-match` / `pending`, or `clear` entries in `mapping.json`.
- **`cherqain tagging batch --help`:** Library-wide tag proposal pass (`--dry-run` still calls the model; rescan after an interrupted batch).
- **`cherqain pdfs preview` / `apply`:** Entry-specific PDF management with preview-token binding.
- **`cherqain zotcreate preview` / `create` / `attempts`:** Reserved-key Zotero item creation; preserve reservations after uncertain outcomes.
- **`cherqain notesync plan` / `inspect` & `cherqain library bibliography-plan` / `bibliography-decide`:** Campaign planning and decision recording (`apply` / `recover` / `settle` / `undo` / `bibliography-apply` return HTTP `501` in production; see [`LIBRARY_CAMPAIGNS.md`](LIBRARY_CAMPAIGNS.md)).
- **`python -m deckgen --help`:** Standalone Beamer generator over HTTP; see [`deckgen/README.md`](deckgen/README.md).
- **Offline Cache & Snapshot Scripts (`scripts/`):**
  - `python scripts/export_cached_text.py selection.json --destination /external/snapshot`: Exports selected existing PDF cache entries and `manifest.jsonl` outside the vault without scanning or extracting.
  - `python scripts/export_cached_pdf_markdown.py selection.json`: Backfills readable Markdown companions (`readable_pdf_exports/`) from verified existing PDF caches.
  - `python scripts/export_canonical_markdown.py --help`: Cache-only batch exporter for canonical Markdown snapshots outside ingestion roots.
  - `python scripts/check_dependency_audits.py --no-resolve`: Checks pinned lockfiles against advisory databases and waivers.

## Agent and contributor checks

Before executing commands, check [`PROJECT_REFERENCE.md`](PROJECT_REFERENCE.md), [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md), and [`AGENTS.md`](AGENTS.md). Use a disposable `CHATEKLD_BASE_DIR` in tests, add driver replay and projection tests for new CLI/MCP fields, and run `python scripts/check_docs.py` across both the source checkout and built public artifacts.

## Explicit page OCR preparation

`python scripts/extract_pdf_pages.py` can also run local Tesseract over explicit 1-based page ranges into isolated checkpoint and export directories without touching the live vault or index:

```sh
python scripts/extract_pdf_pages.py /isolated/inputs/document.pdf \
  --input-root /isolated/inputs \
  --storage /isolated/page-checkpoints \
  --exports /isolated/readable-exports \
  --tessdata /isolated/pinned-tessdata \
  --first-page 1 --last-page 3
```

- **Exit codes:** `0` = `processed_unverified`, `1` = partial coverage, `2` = refused execution.
- **Memory Guard:** Admits warning-level pressure; halts on initial critical pressure, >512 MiB swap growth, or sustained critical pressure.
- **Language Data:** `python scripts/install_ocr_languages.py --destination /isolated/pinned-tessdata` downloads and hash-verifies pinned EN/FR `tessdata` when explicitly authorized.

## Daily interface and CLI boundary (25 October 2026)

The desktop Library workflow adds in-place text preparation (`prepare_text`) and final search update (`update_search`) previews around `library run`. The CLI `library run` commands do not perform text preparation or search indexing automatically—follow [the daily procedure](LIBRARY_WORKFLOWS.md#daily-vault-and-search-update).

### Extraction cancellation and diagnostic boundaries

- Provider connection/timeouts and account errors halt preparation without marking documents defective; HTTP `400`/`413`/`415`/`422` defers a source only when image-specific evidence is present.
- Cancellation during cache promotion halts between durable units and blocks index continuation; completed cache replacements keep their before-images and landed receipts.
- UI `intent-v1:` tokens bind `prepare_text` or `update_search` and cannot downgrade into legacy boolean CLI tokens, nor can a `prepare_text` receipt continue into indexing. See the [API and recovery contract](PROJECT_REFERENCE.md#library-operation-intent-and-monitoring-contract).
