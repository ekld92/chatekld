# Changelog

## Unreleased — Ask usage attribution

- Exclude concurrent Read/Create usage from single-shot Ask export totals by
  tagging and filtering the Ask workflow. Retain enriched Prompt Hub capture.
- Document the remaining same-workflow time-window approximation and cover the
  historical contamination pattern with focused local/online route tests.

## Unreleased — Accurate startup graph progress

- Show note-link graph preparation separately from reranker loading, retaining
  Ask readiness gating and existing retrieval/cache behavior.

## Unreleased — Ask progress and Read presentation

- Clear transient Ask progress on completion, error and Stop, and ignore late
  callbacks while retaining durable notices and partial text.
- Render Read Markdown with the shared sanitizer; preserve original source for
  copy/export and cancel pending rendering on New Paper. Math stays source text.
- Label total section attempts accurately; distinguish them from provider retries.

## Unreleased — Read empty-output outcomes

- Fail empty online summaries and empty map-part notes explicitly, without
  replaying provider calls or changing their recorded token costs.
- Emit available usage before terminal summary errors so SSE consumers can
  retain accounting evidence. Add adapter replay and route regression coverage.

## Unreleased — Field-test timing and recovery guidance

- Measure local Ask duration from server workflow entry, including retrieval and
  eager synthesis, while preserving partial/failure accounting.
- Explain that corpus changes can interrupt Create and that retained checkpoints
  require compatibility review before reuse. Context guards remain unchanged.
- Add focused timing regressions; native acceptance remains a separate check.

## Unreleased — Staged Library workflow and operation monitoring

- Make Review library update the staged entry, with separate text preparation,
  selected writes and final search confirmation. Keep search-only and saved
  Search status controls secondary; clarify daily versus existing-library review.
- Bind explicit session modes and initiating actions to previews; persist receipt
  provenance and prevent preparation receipts from indexing through recovery.
- Follow server-active work independently of history, including newer standalone
  index completion, and show unknown status on failed/malformed observation.
- Keep partial/uncertain/retry continuation gates locked; ignore departed preview
  callbacks and avoid accumulating settings handlers. Check metadata-derived names
  even when existing filenames have a plausible format.
- Add a disposable preparation → approved writes → real fixture index test and
  focused frontend/recovery regressions. These are local source changes, not an
  installed-app deployment. See the review receipt (historical record in Git).

## Unreleased — Repository review and delivery integrity

- Reject empty or non-object CLI/MCP JSON acknowledgements; retain inspection
  guidance for writes whose outcome is unknown.
- Refuse recovery-source symlinks before opening PDFs. Report reset cache
  deletion failures with completed steps instead of claiming success.
- Redirect launch-test dotenv discovery to fixtures; refresh OCR, index-admission,
  Library uncertainty and Create/browser regressions to match current contracts.
- Remove obsolete typing allowances and consolidate the completed rename plan
  and validation instructions. Mark abandoned upgrade permissions as historical.
- Replace private-document links in public artifact copies with explicit omitted
  reference labels before manifest hashing; keep public link checks enforced.
  Align private regression-test ownership with the public-source boundary.

## Unreleased — Index-build typing contract

- Complete the build mixin's type-only host-member declarations, restoring the
  repository mypy gate without runtime changes. Remove its 47-error allowance;
  `rag/build.py` is now held to zero findings.

## Unreleased — Library evidence and settings

- Distinguish dated saved-manifest source counts from unknown current search
  coverage. Bound observational metadata reads and reject malformed or changing
  evidence; preview counts state their scope and preserve unknown values.
- Link index/history and Library path tools from Settings; move Reset App Data
  under Advanced data controls with its existing confirmation.
- Layer nested dialogs by invocation order, hide lower dialogs from accessibility
  navigation, and retain keyboard focus through reopening or early parent closure.

## Unreleased — resource evidence and daily navigation

- Bound shared OCR diagnostic history and freeze each PDF snapshot's memory
  evidence so later documents cannot change an earlier snapshot or export.
- Show search readiness and recovery outcomes even when all selected documents
  were deferred. Use Ask, Read, Library and Create for the main task tabs.


## Unreleased — Memory pressure and daily search updates

- Allow bounded preparation at warning-level macOS memory pressure while retaining
  critical-pressure, monitoring and swap-growth stops. Share the PDF batch baseline.
- Add direct, preview-confirmed **Update library search**, with an explicit online
  choice and optional names/tags/notes workflow. Preserve the embedding model.
- Describe resource stops separately from document failures and offer a read-only
  memory check; never restart work from that check.

## Unreleased — Full inventory rendering

- Display large inventories in pages of 100 rows while filtering and sorting the
  complete dataset. Search jumps reveal their target page. Preserve open details
  and pending confirmations on their original record across page changes.
- Load optional note-link status separately with a bounded wait.
- Validate the 7,300-record fixture with browser regressions and native WKWebView
  pagination/filter interactions; retain native controller and timing limits.

## Unreleased — Daily extraction and responsiveness fixes

- Expose memory safety stops and recovery guidance without classifying the PDF as
  defective; recover verifiable legacy diagnostics without rewriting receipts.
- Keep Prepare busy through observation, preview and lexical preparation, bound
  network waits, and reconnect after uncertain starts without replaying them.
- Require a fresh explicit online choice for a targeted retry. Show all-deferred
  preparation as no new text, with exceptions visible.
- Preserve saved index metadata and valid lexical caches when a completed scan
  proves no mutation. Group readable-copy warnings without overwriting copies.
- Clarify daily vault/search procedures and validate using disposable fixtures.

## Unreleased — Markdown Apply retryability

- Separate definite rejection from preview-token retryability. Keep Apply
  disabled after snapshot/write rejection and request a fresh preview; preserve
  the apply-start barrier. Explicit path/vault contention can retry the same
  unattempted preview. Cover rejection/replay/fresh-preview and contention retries
  with isolated route and frontend regressions.

## Unreleased — Final review corrections

- Keep uncertain Markdown Apply outcomes unknown, retain recovery guidance, and
  enable retry only after an explicit pre-write rejection.
- Stop LaTeX generation between figure copies and before scaffolding on
  cancellation or lost ownership; disclose retained files before retrying.
- Defer Pillow oversized-image rejections without stopping unrelated sources.
- Preserve author wording in Markdown slide prompts and selected-view hover
  contrast in Index tools. Clarify Markdown preview expiry and cleanup timing.
- Add isolated route, provider, frontend outcome and rendered regressions.

## Unreleased — Audit corrections and isolated recovery checks

- Stop online cache publication/index handoff on cancellation or lost operation
  ownership; preserve partial-publication recovery evidence.
- Keep provider timeouts and ambiguous HTTP errors global; require specific image
  evidence before deferring a document.
- Bound ordinary Markdown previews to seven days/newest 50 and invalidate them
  across server sessions; retain uncertain apply evidence.
- Make the activity task list keyboard-focusable and propagate authoritative
  completion, partial, failure and cancellation outcomes.
- Cache validated history completion counts in memory, keeping page reads
  observational and invalidating counts on receipt changes.
- Update Vitest to 4.1.12 and reconcile Studio, Maintenance and recovery docs.


## Unreleased — Actionable extraction failures

Preserve bounded, redacted provider HTTP errors in extraction receipts and show
the actual error with next steps in daily preparation and History. Explicitly
rejected images are deferred during daily updates while other documents continue.
Authentication, quota, service, unknown request failures and integrity failures
still stop the run. Retrying remains explicit; existing cached text is retained
where available. Rename index recovery to “Finish interrupted search update”.

## Unreleased — Unified workspaces

Merge installed maintenance tools into Vault audit & maintenance. Add Studio
selectors for decks/reports, generate/augment and Markdown/LaTeX. Add Markdown
slides and staged, journalled append-only Markdown augmentation. Split index
tools into task views with readable history cards, add a bottom activity console,
and bundle Source Sans 3 throughout the interface.

## Unreleased — Daily Library simplification

Centralize extraction and embedding controls in general settings. Add native-only
preparation, deferred unchanged failure receipts, explicit advanced retries, and
Library text-preparation/final-index previews. Remember tag destinations with
Zotero as the initial default. Preserve existing writer/recovery boundaries.


## Unreleased — Clear daily index action

Expose **Mettre à jour l’index** as the daily action, with OCR configuration,
source selection, forced re-extraction and full rebuild in **Réglages de l’index**.
The daily action ignores advanced source/rebuild/re-extraction choices and keeps
the preview/confirmation boundary. Preserve contextual help and status visibility.

## Unreleased — Daily workflow guidance

Replace the Documents loading placeholder with an actionable no-scan/error
message. Distinguish older OCR sessions with no recorded final status from
interrupted sessions without inferring success from the active index. Add six
keyboard-accessible contextual help popovers for everyday Library/index/OCR use.

## Unreleased — Daily index integration

Integrate the online extraction and index-integrity branches, preserve explicit
incomplete-source manifests during legacy rebuild completion, and clarify daily
updates versus re-extraction and full rebuilds. Cover renamed-PDF cache reuse and
online history driver replay. Update filelock to 4.2.1 and setuptools to 84.1.0
with regenerated macOS/Linux hash locks to address newly reported advisories.

## Unreleased — UI/index review corrections

Fix selected-cache reuse, cache-only short-PDF loading, recovery scope binding
and durable omission reporting. Bound polling payloads, paginate saved evidence,
and preserve usable extraction-panel height in small windows. Legacy continuation
without scope evidence now refuses safely. Native acceptance remains open.

## Unreleased — Explicit index admission and bounded extraction recovery

Routine updates refuse incompatible embedding/store identities rather than
implicitly rebuilding. Online updates reuse complete caches and preview selected
new/changed sources; deferred binaries are enforced in the loader. One-file and
batch promotion share before-images and landed markers. Saved run inspection,
explicit bounded retries, exact omissions, reconciled index continuation, source
selection and authenticated exports are available. Library freshness remains
separate; applying Library changes offers navigation to a reviewed index update.
Native and production acceptance remain open.


## Unreleased — Run-wide online OCR and vision indexing

Add a confirmed Vault index switch that stages eligible scanned-page OCR and
referenced-image descriptions through separately saved online models, including
cached sources. Immutable attempt, run and cache-promotion receipts precede an
incremental index update with local OCR/vision model calls disabled. Local
embedding identity is checked before external transmission; a model/backend
change still requires a separate full rebuild. Incomplete extraction blocks
promotion, and a new confirmed run can retry within the attempt limit. Focused
disposable checks pass; live provider, production-vault and native UI acceptance
remain open.

## Unreleased — Optional online PDF and image transcription

Add explicit one-file and previewed vault-session online text extraction for
eligible PDF pages and note-referenced images. DeepSeek Flash, OpenAI,
Gemini, and OpenRouter use allowlisted image-input models and the existing
environment keys. Attempts and readable exports stay outside the vault;
only a separate confirmed action promotes complete text to the existing cache
and incremental index. Failed or partial results remain retryable and do not
replace indexed content. Local provider and installed-app acceptance remain
unverified.

## Unreleased — Note Refactor retirement

Remove Note Refactor analysis, editing, image archiving, settings, CLI commands
and MCP tools. Move journal recovery to **Settings → History and Restore**,
with independent shared note writers, file resolution and Plain Chat model locking.
Existing journal formats, operation IDs, default/custom archive paths and historical
restoration remain supported. Git-only Notes Sync mirror recovery is unchanged.

## Unreleased — PDF extraction integrity and readable caches

Keep known partial PDF extraction out of index replacement and stale cleanup;
completed range caches remain retryable. Export successfully cached legacy text
to no-clobber Markdown outside the vault, with an explicit cache-only backfill
command for older PDFs. No model, embedding, or profile change is introduced.
Installed-app delivery has not been performed.

## Unreleased — Opt-in canonical PDF text profiles

Add source-bound cached plain-text exports, separate page/provenance sidecars and
an explicitly activated PDF cache-reader profile. Legacy routing remains the default;
failed or stale profile reads visibly fall back. Add an isolated production-chunker
retrieval comparison and disposable real-PDF recovery evidence. Production activation
and installed/native acceptance remain unverified; the app build is deferred.

## Unreleased — Certifi trust-store security update

Pin certifi 2026.10.19 and regenerate both platform hash locks to address
GHSA-6q48-6c7w-9gw2 (emSign roots). The macOS dependency audit and dependency
gate tests pass. This source update does not update the installed environment.

## Unreleased — Documentation and dependency gate

Add a user and agent CLI guide, clarify Library workflows in the README and
in-app help, and check public documentation links during CI and artifact
creation. Update vulnerable Python pins in both platform lockfiles and move
the JavaScript test harness to Vitest 4.1.11. CI runs on `codex/**` pushes so
validated commits can satisfy protected-branch checks without a pull request.
Manual CI runs scan the pending commits for secrets; normal push and pull
request scans retain their existing action. Local JavaScript tests and the
macOS dependency audit pass; installed app behavior remains unverified.

## Unreleased — Provider routing review fixes

OpenRouter now requires a capable upstream route for tool and explicit-reasoning
requests. A model without cached capability metadata can still be dispatched
with this routing requirement. The Gemini parallel-call fixture and provider
contract now reflect the signature on the first call part. These changes have
hermetic coverage; live forwarding and installed-app behavior remain unverified.

## Unreleased — Online provider request contracts

Gemini 3.x omits sampling controls and maps unsupported minimal thinking to
low on 3.8 Flash. OpenRouter applies namespaced model-family rules, retains
text-capable Codex models in discovery, treats model parameter metadata as
advisory, and surfaces tool-routing failures instead of answering without
required tools. Direct DeepSeek omits ineffective thinking-mode sampling;
GLM-5.3 maps explicit reasoning effort to its supported values. These are
checkout-level, hermetically tested changes; live generation, provider-route
forwarding, and installed-app behavior remain unverified.

## Unreleased — Adversarial recovery corrections

Bind Zotero metadata/merge/trash reads to the observed database and transport.
Retain uncertain merge writes and missing-note snapshots; include unknown
operations in batch recovery and refuse tag restores over metadata changes.
Read mapping fields from one generation, preserve tag drafts during Desktop
authorization, and include mixed-case PDFs in the repoint picker. Report missing
Zotero observations and path ambiguity without claiming absence. Reset no longer
steals active vault locks; SSE budgets include Retry-After. Separate npm notices
from audit JSON and clarify per-operation recovery limits.

## Unreleased — Library Audit associations and Zotero tags

Prevent uncorroborated note/filename associations from hiding unlinked PDFs,
resolve qualified PDF paths exactly, and discover mixed-case PDF extensions.
Tagging exposes Desktop authorization and distinguishes it from reachability;
Desktop selections remain ineligible for Web writes. Uncertain tag writes keep
their journal evidence and require verified current tags before restoration.
Validated with disposable fixtures; installed-app/native acceptance is separate.

## 2026-10-02 — Disposable disk docstore for existing LanceDB indexes

Existing JSON docstores load record by record into a private temporary SQLite
copy. Targeted reads and counts avoid full materialization; persisted formats
and embeddings remain unchanged. Cold import takes time and scratch disk space;
bulk APIs and cold retrieval caches can still materialize the corpus. This is a
memory/disk tradeoff, not a demonstrated overall speedup. The
performance review (historical record in Git) records costs and
pending user/native acceptance. No installed bundle update is established.

## 2026-10-01 — Retrieval cache counts and SSE budgets

Warm BM25/wikilink lookups and health/drift counts avoid full node
deserialization on SimpleDocumentStore. In-memory cache fingerprints include
index identity and mutation epochs, including same-count replacements and
partial failures; persisted index/sidecar formats are unchanged.
Deck/docgen silence guards include online retries, and Vault/Plain Chat consume
the server-advertised budget with a renewing browser margin. Opt-in bounded
monotonic timings separate retrieval stages and lock wait/hold without content
logging. Offline fixture evidence is recorded in `docs/performance_2026-10-01.md`.
The consolidated review (historical record in Git) closes the
experimental campaign; installed-app acceptance remains pending.

## 2026-10-01 — Project review remediation

Public source artifacts now exclude private cleanup, native
prototypes and machine configuration, and publish directories with atomic
no-replace semantics. The npm audit gate refuses operational errors and incomplete
reports. Compilation, Bandit and mypy share the full application scope; fixed SQL
and Git's explicit non-security SHA-1 remove the new warnings without expanding
the security baseline. Newly covered Notes Sync/cleanup typing errors are resolved
without increasing the 137-error legacy ceiling. Both platform locks update
anyio, urllib3, sentence-transformers and soupsieve to clear eight advisories.

CI separates static, Python, JavaScript, browser, dependency, secret and macOS
checks. Missing provider keys cannot produce a green all-skipped live smoke.
Historical campaign citation-key findings have exact fingerprint dispositions.
Private prototype/Maintenance tests explicitly skip only when those capabilities
are absent from the public artifact. Root instructions are synchronized and
condensed; detailed contracts and the dated backlog are retained in separate
references. Research evidence remains preserved.

## 2026-10-01 — Sol Responses review fixes

Reject malformed function-call batches before dispatch, classify structured
Responses errors, and record known usage for failed attempts separately from
functional success. Agent tool turns stay on a successful fallback provider
and model to preserve reasoning continuity. GPT-6.1 Sol estimates use the
published long-context rates above 272K input tokens; explicit pricing
overrides remain final rates. Targeted offline tests cover dispatcher replay,
fallback continuity, terminal failures, retries, cache accounting and pricing.


## 2026-09-23 — CLI proof-run fixes: the leftovers

The four open items from Batch D, handled. **Thesis:** the LaTeX sweep now
builds a document with XeLaTeX when its own preamble loads `fontspec` or
`unicode-math` (only the thesis does), still without reading any rc file.
**Templates:** the four Beamer templates declare `\refline` with
`\providecommand`, so they no longer clash with `cress-style.sty`. Live: all
102 documents compile and convert. **README mirrors:** the three mirrors whose
READMEs had changed on GitHub were replaced, and the step published all 12; a
second run finds them unchanged. **CLI discovery:** the server probe waits up
to 10 s (was 1.5 s), so a busy server during a scan is no longer reported as
"not running"; a stopped server is still detected at once.

## 2026-09-23 — CLI proof-run fixes, Batch D: live re-verification

Re-ran the proof run's CLI sequence against a headless server started from the
fixed code, with no API key exported by hand. `zotlink status` reports the
`web` transport (the server loaded `.env` itself). After a scan, the first
`library run analyze --propose-tags` (DeepSeek `deepseek-flash`, $2 limit;
spent $0.0022) closed 148 already-named renames and jung's tag step, listed no
leftovers and nothing blocked; the second analysis left 4 open units (was
151): `buciuc_.pdf` (no year), `heneghan_2006.pdf` (now "editors only"),
Scienceverse (no text; its exit is a hand-written tag row) and one paper
imported since the morning, with a stable mirror digest. Maintenance: `links`
74, `attachments` 5, `latex` 5 of 102 (four templates redefining `\refline`
and the XeLaTeX thesis, PPTX/DOCX all OK), `readmes` 14/14 fetched with
publication stopped on three mirror files you must decide about. Nothing was
applied or trashed.

## 2026-09-23 — CLI proof-run fixes, Batch C: small fixes

**CLI help.** `library run apply --approvals-file` names the real keys
(`mirror`, `pdf_links`, `tags`, `renames`, `leftovers`); `audit report --help`
lists every report name from the driver's list, now pinned equal to the
route's own allow-list; `audit scan` points at `papermind audit wait` (not
`papermind status`, which does not report the scan). **Report responses.**
The PDF observation block was added to every report by re-parsing and
re-serialising each response, and computed even for reports that never show
it. It is now added once, only to the twelve reports that render it (one
server-side set, pinned against the interface's). Measured in-process on the
live library: `not_in_zotero` 1,438 → 1,213 ms and 44.7 → 43.1 MB,
`tag_hygiene` 649 → 197 ms. **Headless server.** `papermind server` now loads
`.env` exactly like the app (one shared loader), so it no longer needs the
keys sourced by hand. (The deck compiler's missing `letters/common` path, C2,
shipped with Batch B.)

## 2026-09-23 — CLI proof-run fixes, Batch B: Vault Maintenance

**Links.** `links` reported 2,860 broken references and `attachments` 2,746
missing files, almost all bare markdown links such as `[x](file.pdf)` to a
file elsewhere in the vault, which Obsidian opens. A bare markdown name now
falls back to the vault-wide basename after the note's own folder, exactly
as a bare wikilink does; a link with a directory never does. Live: 74 and 5.
**README mirror.** Every README failed as "unparseable" since 2026-08-24:
GitHub wraps the base64 at 60 characters. Whitespace is now removed before
decoding. The step then stops, by design, on three hand-changed mirror files
(see Known Issues). **LaTeX sweep.** 77 of 102 documents failed because the
security change that stopped reading the suite's `.latexmkrc` also dropped the
TeX search paths it set. They are now passed as data, from one helper shared
with the deck compiler, which also gained the missing `letters/common` path.
Live: 5 failures left, 4 real template defects and the XeLaTeX thesis.
**Renamer.** An edited book (editors, no author) is refused with that reason
instead of "no mapped bib record".

## 2026-09-23 — CLI proof-run fixes, Batch A: the library run

Found by driving a library update through the CLI only. **Renames:** 147 PDFs
that already carried their derived name stayed "open rename work" in every
session forever — the renamer refused them with no machine field and the
orchestrator's `proposal == name` test could never be true. The renamer now
marks that refusal `code: "already_named"`, and the analysis records the unit's
rename step as done-by-nothing (`skipped`, `closed_renames`), keyed on the
code, never the prose. **Tags:** a paper tagged outside a run (by hand, in
Zotero) kept owing tags, and every `--propose-tags` run asked the model about
it again. The analysis now closes the tag step when every destination the unit
owes and has already carries a `_tags.md` tag (`closed_tags`), read from the
last scan (Finder from the PDF itself); a new PDF on a tagged paper still owes
its Finder tags. **Leftovers:** two imported attachments whose files were gone,
each beside a live linked copy, were reported for 16 h as "still being moved by
Attanger". Those are now "leftover Zotero attachments": listed for review,
unticked by default, trashed on approval through the journalled de-link route
(reversible from Tagging → Restore), never when annotated.

## 2026-09-23 — Online LLM API contracts

Verified the six provider contracts against official documentation and bounded
live requests; verdicts and probe limits are in
`docs/verification_2026-09-23_online_llm_api.md`. Tool loops now retain and
replay DeepSeek/OpenRouter reasoning state and Anthropic's ordered assistant
blocks only to the originating provider/model. Gemini function-call IDs are
preserved and echoed when the API supplies one.
Streaming replies now announce output truncation in the affected chat and
summary routes. An empty agent reply stopped by the token cap reports that
cap directly. The optional online reasoning control is exposed in Settings;
its default preserves provider behavior and cost.
Curated lists now remove confirmed retired OpenAI and Gemini IDs and add
priced current GPT-5.6, Claude 5.x, Gemini 3.x, DeepSeek and GLM models.
Configuration version 5 migrates saved retired selections; the DeepSeek
default uses its current Flash ID. OpenRouter's ten curated prices were
refreshed from its public model catalog.
The migration also clears retired IDs from workflow-specific model overrides.
The type ratchet now holds the agent loop and tool-wire builder to zero
findings, reducing the legacy allowance by 14.
Named parameter rejections are remembered per provider, endpoint and model,
avoiding the same sampling 400 on later requests. Opus 5.x omits unsupported
sampling, Haiku 4.5 keeps temperature when top-p conflicts, and Z.AI clamps
sampling values to its published range. Gemini now retries named parameter
400s and remembers the rejected field.
Six tiny live GPT-5.6 requests confirmed that Sol, Terra and Luna reject
nondefault temperature and top_p. Those exact ids now omit sampling before
the first request; `gpt-5-chat` remains unaffected.
Claude and Gemini stop reasons now distinguish filtering, output limits and
malformed tool calls. Gemini's invalid-key detail is classified as an auth
error even though its HTTP status is 400.

## 2026-09-23 — Audit remediation of the library-workflow commits

Remediation of the 2026-09-23 audit of `22490593..a7a171d9`, one commit per
batch. **Batch 1 — tracker honesty:** a Zotero note's `mirror` step now
closes only when the mirror names it in `settled_keys` (a record newer than
the last scan used to be closed unmirrored and never reopen; it now stays
open as "rescan needed"); ticked note tags the mirror could not write, and a
`pdf:` list it could not insert, keep the work open and are reported; a tag
row with no destination is "not needed", never "done"; `apply` refuses while
an audit scan runs; `/api/library/observe` and `/api/library/steps` take the
run gate, and `/steps` only resolves an uncertain step (confirm-gated); an
attachment's parent entry is credited only when its Zotero tags were written;
the CLI exits 1 for anything left open or ignored and refuses cost flags
without a model. **Batch 2 — rename outcomes:** a rename the renamer
refused before touching the file is a plain, retryable failure (it used to
be "uncertain" and block the paper forever); an uncertain rename is settled
from its journal by the next analysis when the journal can tell, and
otherwise by hand ("Mark resolved…", `papermind library run resolve`); a
rename chain crossing the 200-row preview boundary gets the same names as a
single preview; the live bibliography and the notes are memoised on their
files' signatures, so a rename no longer re-parses the whole `_master.bib`
under the vault lock. **Batch 3 — cost honesty:** an unpriced online model
runs only under "No spending limit" (a limit could never bound it; the budget
check used to compare nothing for such a session), and a limited session with
an unknown spend always stops; a proposal whose cost could not be recorded is
reported and makes the spend unknown; the analysis backs off on a failing
provider and stops after five failures in a row; a session and its model are
created in one transaction; the spend and model are columns and run results
are rows, migrated from the old JSON. **Batch 4 — journal and note-sync
safety:** `apply-pdf-links` keeps the undo of the notes it already wrote when a
later note fails (one failed snapshot now fails one note, not the batch) and
merges instead of pruning after a lost lock; a PDF replaced since it was
observed is a clear refusal ("analyse again"), not an unexplained 500; the
note writers refuse a note that became a symlink; an unreadable notes folder
refuses the plan; note creation beats its lock and stops when it is lost; the
vault link walk has a budget; the sync-point store's lock is a bounded wait.
**Batch 5 — concurrency and performance:** the "Update my library" run now
shows the note mirror's progress as it happens and its Stop reaches a running
mirror; an observation resolves each path once; the safe-save lookup uses a
per-pass index; the tracker's schema is set up once per store file instead of
on every write; the model list's prefill no longer offers the active local
provider's model for another local provider; the Maintenance log appends
lines instead of rebuilding the whole pane per line. **Batch 6 — interface:**
an apply the server refused before starting no longer locks the review or
warns that something may have been written; leaving and returning to "Update
my library" restores the review or result; Stop works while analysing and
applying; progress shows as a bar and is announced every 25 items; a ticked
mirror is confirmed in the danger tone; "Select all" / "Select none"; visible
field labels; local result times; coloured failed/uncertain/partial outcomes;
plainer intro text; the Maintenance panel stays quiet when hidden and asks
again before re-running a writing run that ended without a summary.
**Batch 7 — documentation:** the guide, README and routine describe the
current apply order, default note tags, `--no-limit`, the `pdf:` group, the
05m stage and where the library checks live; dangling "see Known Issues"
pointers, hardcoded counts and a misplaced cross-reference are corrected;
Known Issues is regrouped (closed entries moved to *Closed*, half-closed ones
split, orphan numbering removed, one `apply-repair` entry); the retired
annotation-transfer sections, the September 15 plan and three native
verification records are removed (Git history keeps them). Every regression
test was proven red on `a7a171d9`; an app rebuild and native WKWebView /
VoiceOver QA are still owed.

## 2026-09-22 — Library follow-ups: no spending limit, Library-hosted checks, a new apply order

A session model can run under **"No spending limit — I accept the cost"**
(`no_limit`, refused together with a budget; spend still measured). The
library checks (`pdfnames`, `library`, `tagcheck`, `zoterostorage`, and
`batchtags`) render only in **Library → Checks & campaigns**, reported to a
persistent surface that survives a section re-render; a passing check states
its summary and each result card takes focus. The note mirror writes each
created note's `pdf:` list (the exact shape `pdf-links-status` reads as
current) and the ticked, vocabulary-gated tags, through one render shared by
the preview, the plan digest and the write. An "Update my library" apply runs
**mirror → `pdf:` lists of existing notes → tags → renames**, tags go to all
three destinations by default (`--no-note-tags` opts out), and the renames run
last so a rename no longer invalidates a reviewed mirror plan.
`GET /api/models?provider=` lists another provider's models with its
configured one. The same-day review fixed the mirror's `note_tags` bound,
refused tag rows tagging created notes, silent loss of ticked note tags, the
`pdf:` lost-lock bookkeeping, overlapping Maintenance runs and result cards
stealing focus.

## 2026-09-21 — Reviewed library maintenance (lots 1–6)

Library → **Update my library** and `papermind library run` share a manual,
reviewed import → names → tags → Zotero-note mirror workflow. Imports use
stable scoped identities; the first pass establishes a baseline. Session
models leave global configuration unchanged, disclose source/destination,
cache proposals and enforce an estimated priced budget without fallback.
Default tag destinations are Finder and Zotero; Obsidian tags are explicit.
Mirrors bind confirmation to a digest and prove Git recovery for each replaced
file; creation-only runs use their own journal without an unrelated Git gate.

Adversarial review `ddc59278` corrected stale identity/history, partial results,
missing receipts, uncertain retries, cancelled base work, obsolete proposal
paths and collisions across bounded name-preview chunks. Nineteen regressions
were proven red by restoring original production code; seven disposable
production-route acceptance checks cover the full sequence and recovery.
Full Python gate: 4,791 passed, 3 skipped, 246 subtests. No real-provider/native
or process-kill acceptance is implied. Two scratch isolation incidents added
fixture state to real app stores; exact retained evidence is in the review report.

Documentation now separates Git recovery from journal Restore, identifies all
write destinations and session side effects, and describes the shared CLI.
The delivered September 21 plan and superseded September 20 proposal are
retired; open work remains in CLAUDE Known Issues. Dated implementation records
are explicitly historical. (The incomplete September 15 plan was retired on 2026-09-23; its open points are in CLAUDE Known Issues.)
Developer workflow detail moved to `docs/library_workflow_contract.md`, with
user instructions in `docs/library_workflow.md`.

Mounted Chromium review corrected partial/cancelled result messages, stale
announcements after navigation, rename destination disclosure, tag error tones
and excerpt keyboard access, HTTP Retry, Restore outcome persistence and
confirmation wording, hoverable help outside the tablist, and long filenames
that hid the proposed-name column inside the report. The last browser
session can now be recovered with its saved result date. Native WKWebView and
VoiceOver, real writes/providers and abrupt process-death recovery remain open.

## 2026-09-20 — UI consolidation: a verb grammar, one outcome line, restrained trends

Four passes over how the app *reads*, all client-side.

**Buttons say what they cost.** 54 distinct `data-action` values across ~159
inline verbs were one undifferentiated blue: `[open]`, `[unmap]` and `[retire
everywhere]` looked identical. Five roles now, mapped in one table
(`audit.js::ACTION_ROLES`) and stamped centrally by `applyActionRoles` rather
than at 159 emit sites — primary, secondary, navigation, destructive-confirmed,
inline verb (read / write ✎ / destructive ⚠). The role is decided by what the
click costs the user, never by which endpoint it calls: an online
identification is `read` (it writes only regenerable memos), and BOTH note-sync
directions are `destructive`, since each overwrites the other side's region.
The Library section strip is drawn as a tab strip; it was already `<nav>` +
aria-current, only its shape said "action". Glyphs use the empty
alternative-text form (`content: "…" / ""`), so they are a channel for the eye
without adding a word to what a screen reader reads.

**One outcome line.** Five subsystems' `_xSay(msg, isError)` had drifted into
three conventions. `ui.js::statusLine(host, {tone, text, at})` is the one shape
— glyph · text · "7 min ago" — with four tones, because collapsing a status to
"error or not" is what made a finished scan wear the warning palette.
`banner-neutral`, a modifier whose whole job was to say "ignore my colour", is
retired. Three real fixes rode with it: the stamp (a persisted Library status
line from earlier in the scan read as if it had just happened — a recorded
Known Issue); a double announcement (two hosts were minted with `role="status"`
AND announced through the static announcer, and WebKit drops a minted live
region); and `audit-status-line`, applied to three hosts and defined in no
stylesheet. The contrast lint earned its keep — `--ok-fg`, `--danger` and
`--text-tertiary` all measure 4.17–4.44:1 on `--bg-elevated` in at least one
theme, so the scan banner is drawn on the card surface instead. Maintenance
step rows get a state chip on the head line and say the Library sentence once
rather than under every step.

**Help badges.** Plain Chat's "?" led the Send row instead of wedging between
the field and Send (where it read as help for Send); two Library tabs dropped a
`title` that duplicated their badge. The ratchet requiring a `title` on every
report tab now accepts either surface, and gains its other half: no tab may
carry both.

**Restrained trends.** A library-health sparkline over the last ten scans, a
"since you last looked" delta per report tab, the tagging deck's "N today"
generalised to the ambiguous and accession decks through one shared primitive,
and one-shot milestones for zero orphans and no dead curation (plus every note
baselined, from the note-sync tab). The health number is a named list of the
five things a cleanup actually closes — "tagged nowhere" would swamp it and
"entry, no local PDF" would never move — and its composition is printed beside
it. Explicitly declined and now ratcheted absent: streaks, points, levels,
badges, and any prompt beside a write verb.

## 2026-09-20 — Mirror review: the lock covers note writes only, and a cancel is pinned

Review of `92371234` (the one-click mirror). `_refresh_write` took the obsidian
op-lock even with nothing planned, so a mirror whose only work was a stale sync
record to delete stopped at `indexing` during an index run — for a phase that
writes no vault file. The lock is now taken only when a region is to be
rewritten; the base store and the stale-base deletions never needed it.
`/refresh-all` inherits it (an empty plan during indexing succeeds instead of
503ing). The cancel gate in front of the mirror's first create had no test —
removing it left the file green — and is pinned now. Every other claim of the
commit held under mutation, and a created note reads `in_sync` after a rescan
(measured over plain, list, blockquote and rule content; wikilinks and images
stay `unparsed` by decision). The "Sync the library" button is assessed in
the now-retired September 20 feasibility proposal: not the whole thing, and the
honest subset is a private-only Maintenance step over the routine's proposers.

## 2026-09-20 — A Zotero-linked PDF never reads as "not in Zotero"

The pairing rung leaves an unexported Better BibTeX key, and a file two items
link, on `unmapped_pdfs` — right for pairing, wrong for the three reports
whose whole claim is "no Zotero entry". All three now skip such a file and
disclose the count, through ONE definition (`bridge.zotero_unclaimed`) that
the accession routes already used; the *↳ Unread Only* tab is filtered too, or
it would list a row its own documented superset does not. A Web create runs ONE complete observation, immediately
before the reservation, instead of two identical ones back to back. The
renamer's attachment lookup prefers an exact path before falling back to the
case-folded key, so two case-distinct files cannot ride one rename. Only codes
Zotero returns before writing retire a reservation — a 408 or 409 no longer
authorises a second unkeyed POST. `storage_check` excludes Connector web pages
only, not every non-PDF import. A resumed Phase 4a row is no longer counted as
freshly renamed and now names its remedy. Zotero Attachments gains the
**Title = filename** verb (the route shipped 2026-09-18 with a CLI caller
only), one status line for the whole tab, and the relative-link disclosure the
scan had been counting unrendered. The two persisted status lines are cleared
by a new scan. Two JS ratchets that went red for a change that improved what
the user sees now accept the status-line path.

## 2026-09-19 — Docs and UI follow Zotero-first pairing

The help guide, README and Audit tab now name the Zotero linked-file as the
first pairing rung: it beats `mapping.json`, Dead Curation lists the override,
and **[never auto-map]** no longer "guarantees" unmapped. Notesync outcomes stay
visible after the report re-renders; Dead Curation offers **[clear decision]**;
Inventory shows the `pdf:` list state; **Key changed in Zotero** is on the Notes
Sync tab. The tracked migration/verification JSON dumps (vault paths) moved to
`BASE_DIR`; the 2026-09-09 plan/review/continuation chain is deleted (open items
already in Known Issues).

## 2026-09-19 — Pair and undo against Zotero's real state

Shared `linked_path_key` (NFC + casefold) for attachment lookups. Create undo
tolerates Better BibTeX's `citationKey` write-back. A landed Extra PATCH that
also moved an unselected field is applied and trash still runs. A Web create
proves absence before reserving, so a timed-out second observation no longer
blocks the PDF at `parent_unknown`. A definitive 4xx parent POST retires as
`never_created`. pdf_management link/trash go through the journalled writers.
Accession rows skip a pending file Zotero has not uniquely claimed. Clearing or
detaching a mapping that Zotero still links warns, and still writes.

## 2026-09-19 — The Zotero link is the first pairing rung

`inventory.build_inventory` reads Zotero before the bridge and claims every PDF
linked by exactly one item whose key is in the bib (`source: zotero_link`). It
beats `mapping.json`; disagreements land on Dead Curation as
`overridden_by_zotero`. The scan still never writes `mapping.json`. Omitting the
index leaves the ladder byte-identical.

## 2026-09-19 — Annual library cleanup closed

Seven phases through the journalled routes; recurring `papermind cleanup
new-pdfs`. zotrecord retitle/merge/trash; zotcreate batch (one Web observation)
and `settle-never-created`; zotlink retitle; `pdf-links-status`; notesync
key-changes and rename-note; a rename carries `mapping.json` with it.

## 2026-09-19 — Zotero-authoritative note sync

Maintainer decision: the reading notes are edited only in Zotero and
`Z_Zotero_Notes/` is a read-only mirror. New `notesync_zotero_authoritative`
setting; on, the Zotero Notes Sync tab hides keep-mine / merge / push-all and the
per-row create-in-Zotero, and offers one git-gated, un-journalled
`POST /api/notesync/refresh-all` (`papermind notesync refresh-all`) that rewrites
every note's marked region that differs from Zotero AFTER the converter (a byte
plan wanted 1,222 of 1,236 notes on the first live dry run — framing and Markdown
spelling), rebaselines every note and drops stale bases. Notes gone from Zotero
are listed, never deleted. In that mode push and merge refuse; cleanup 05b
defers to refresh-all; the git gate requires tracked files; listed notes keep
their bases. new-pdfs refuses a scan that is still running or ended in error.
Data prep on the live vault: the three notes carrying
Obsidian-only text were pushed to Zotero (yatham's pasted image dropped), the
deleted `hafnerDigitalHealthInterventions2022` item re-created through the web
API with its reading note, and the plugin template moved out of the folder.

## 2026-09-17 — Review remediation G: documentation matches what shipped

README no longer says there is no metadata executor (it exists, is journalled and
answers 501). CLAUDE.md gains the `/api/accession/pdfs/*`, `/api/audit/library/<view>`,
`/api/accession/review/*` and `/api/notesync/campaign/*` routes, the corrected
note-scope and bootstrap rules, the Manage PDFs lock and link order, and a Known
Issues section for everything the remediation left open. The help guide lists the
Zotero storage step. The 2026-09-14 annotation evidence is deleted (decision D5);
the five run session prompts and this remediation's plan and prompt are deleted
(decision D6); the 2026-09-15 unification plan is kept, still incomplete.

## 2026-09-17 — Review remediation F: low-severity findings

A cleanup stuck after a failed restore can be inspected and completed; a
cross-volume recovery root is a preview blocker; trash previews digest the
survivor's stable identity; audit lines name the PDF; symlinks are refused only
below the vault root; a reviewed value must be text or keep the current shape;
PDF authentication folds Greek letters on both sides and requires an author near
the title; the executor's no-replay guard reads every earlier batch; an unreadable
native note is `native_unreadable`; the pre-write prune protects its own op; merges
work through a symlinked notes folder inside the vault; dead code removed.

## 2026-09-17 — Review remediation E: tests and ratchets

The driver replay suite enforces coverage: every `ENDPOINTS` row is claimed by a
case that is checked to actually send it (fourteen rows had none). The CLI refuses
out-of-range note campaign selections before any request. Manage PDFs gains tests
for inspect/complete, later-verb blocking, cross-volume refusal, its routes and its
Zotero-side UI flows; the metadata executor's `apply()` itself refuses while
production application is closed.

## 2026-09-17 — Review remediation D: Library UI

Opening PDF identification review or Ambiguous PDFs selects exactly one report tab.
Library facts relabel only state fields and date only date fields; structured
values render as names and facts, never JSON. Library-hosted Maintenance checks
keep their progress glyphs across re-renders. Three Library status lines reach
static live regions.

## 2026-09-17 — Review remediation C: classification and search cost

The storage control no longer counts images embedded in Zotero notes as stored PDFs
(they are disclosed as excluded). Cross-report search stops rebuilding the Library
documents view per keystroke: ~4.2 s → ~13 ms warm on copied live stores. Note sync
no longer refuses a note for text outside its region (decision D4): 1,194 of 1,237
live notes read `unparsed` before, 157 after — all for in-region content.

## 2026-09-17 — Review remediation B: executor data safety

Campaign settle/recover/undo no longer hold the vault lock across Zotero I/O. An
`outcome_unknown` note write is resolved by one live read on the next push or merge
(decision D2), a refused connection is `failed`, and Restore offers a live-checked
revert. An unknown annotation count blocks deletion. Manage PDFs reads notes once
per operation, tolerates non-UTF-8 notes and holds the vault lock around physical
moves. Sync bases from the previous converter are carried over on re-baseline when
the note is unchanged.

## 2026-09-17 — Review remediation A: two production-breaking defects

The bibliography plan, authenticate, apply and decisions routes answered 500 on
every request (`load_settings()` called with an argument); fixed, with tests that
no longer patch above the seam. Linking a PDF from Manage PDFs now creates the
Zotero attachment before writing the mapping (decision D1); a refused create no
longer blocks the entry forever and the block message names the operation.

## 2026-09-16 — Library views: no raw JSON, contractual vocabulary

Every Library view (Overview, Documents, Checks & campaigns, Storage control,
History & recovery, dependent links, bibliography review) now renders
structured facts, tables and lists instead of JSON dumps; states read as the
plan's contractual labels (Identity to review, Associated in PaperMind,
Deferred by you, Note contents match, …); counters name their population and
date. Pinned by a source ratchet, a JS walk of every view and a Chromium spec
that captures each view.

## 2026-09-16 — Phase 7: duplicate copies compared, annotated copies kept

`accession/duplicates.py` recognises `_1`-style collision suffixes as clues
and compares copies by bytes, native and embedded annotations, note
dependencies and mappings. Manage PDFs shows those facts per copy; recoverable
removal of a copy that carries annotations now requires an explicit
acknowledgement that rides the plan digest. Different bytes are a version and
stay. A copy renamed between preview and apply stops the plan with nothing
removed.

## 2026-09-16 — Phase 6 reduced: the Zotero storage control

No storage conversion is built. `accession/storage_check.py` classifies every
saved Zotero attachment (stored, outside the bibliography folder, broken,
relative, duplicate path) with manual guidance for Zotero or Attanger, read by
the new Library → Checks & campaigns → Storage control view, `papermind
library storage` and the private Maintenance step `zoterostorage`.

## 2026-09-16 — Phase 5: PDF authentication and the metadata executor

`accession/pdf_authentication.py` locates a record's DOI, title and authors in
its own PDF pages with page, offset, excerpt and passage fingerprint; a DOI
printed only in the reference list is `reference_list_only`, never identity.
`papermind library authenticate` and `POST /api/audit/library/bibliography/
authenticate` expose it; `bibliography-plan` runs it for records naming their
`pdf`. `accession/metadata_executor.py` writes only review-approved fields
through `zotero_write.set_item_fields` under the version guard, journalled as
`zotero_metadata_write` and reversible; it refuses stale fields and predicted
unselected changes (the Extra → citationKey promotion) before the PATCH and
records unpredicted ones after it. Closed in production (501).
`prototypes/bibliography_corpus/build.py` builds the disposable evaluation
corpus with planted errors.

## 2026-09-16 — Campaign undo on both destinations

`POST /api/notesync/campaign/<id>/undo` and `papermind notesync undo` reverse
one applied campaign note on Obsidian AND Zotero as a single decision, only
while both notes still carry the exact written image (journalled hash and
written HTML at the written version). The pre-campaign base is retained at
reservation time and put back; a partial undo is kept as `undo_incomplete`
and never retried. Closed in production (501) like the other executors.
The campaign runner gained `--undo`, `--concurrent`, `--native-stop` /
`--recover-native-stop` and `--human-screen` scenarios.

## 2026-09-16 — Annotation transfer abandoned and removed

**User decision.** Moving native Zotero annotations between two attachments of
the same parent was a fail-closed prototype whose production port was never
opened. It is now removed rather than kept dormant: the engine, transport and
pairing modules under `accession/`, the whole `native/` companion, the nine
pairing/transfer routes on `/api/accession/pdfs/*`, the Manage PDFs fieldset
and its polling/reversal handlers, eleven test files, eleven prototype runners
and fixtures and the 2026-09-15 acceptance folder. Git history keeps them.
*(Corrected 2026-09-17: the 2026-09-**14** evidence — `docs/annotation_acceptance_2026-09-14/`
with its XPI, `evidence.json` and 24 screenshots, `native_annotation_selection_2026-09-14.png`,
the three `native_verification_2026-09-14_annotation_*.json` and the `.gitignore` exemption —
was NOT removed by this entry; it was deleted on 2026-09-17, decision D5.)*

* Duplicates that both carry native annotations are **kept**, and the
  dependency is displayed; recoverable physical removal is offered only for
  annotation-free files or after an explicit user decision.
* `pdf_management.require_settled` blocks cleanup on any retained operation of
  the entry that is not `complete`/`restored`; the completed-transfer native
  readback it also ran is gone with the feature.
* Annotation counts on the Zotero Attachments report and in Manage PDFs, and
  the `accept_annotation_loss` gate on attachment trash, are unchanged.

## 2026-09-07 — Note sync in three buttons, unmapping, and renames Zotero follows

**Note sync is one table with three verbs.** The tab stacked six sections,
two of which drove the older one-directional `update` route: the same note
appeared as `[pull]` in the sync table and again as `[update]` or
`[overwrite region]` below it — three names for two writes, with the
destructive one reachable from a list that looked like the safe one. Those
sections and their handlers are gone; every comparable row now carries
**[take Zotero's] / [keep mine] / [merge…]**.

* `_pullable` / `_pushable` tested the row's STATE NAME, so a row summarised
  as `push` could never be pulled — not even with `accept_conflict` — and
  "throw my edit away, take Zotero's" was inexpressible on precisely the row
  a person most wants to say it about. They now test what actually changed:
  pull asks a second time when the region moved since the sync point, push
  when Zotero moved. `_COMPARABLE_STATES` bounds the widening — `missing_*`,
  `unparsed` and `multiple_notes` are refused in both directions even with
  the acknowledgement.
* Baselining is promoted to the top of the tab while any note lacks a sync
  point (3 of 1 236 have one here), since without a base the state is a
  word-count guess and the merge view has no "before" to show. It writes
  nothing to the vault or Zotero.

**Unmapping a PDF.** `detach` was fully general but `[detach]` was rendered
only on "Entries With Several PDFs", so a record with ONE wrongly-matched PDF
had no unmap affordance anywhere. The verbs now live in the Inventory pair
drawer, and `detach_all` is the blanket form: per-key exclusion is
whack-a-mole because the ladder finds the next candidate — measured live, 2
of 127 exclusions had already needed a second round. A hand link still beats
the wildcard, and `*` cannot ride in on `detach_from`.

**A rename moves Zotero's pointer with it.** The 2026-09-06 attach run put
5 242 `linked_file` attachments into Zotero, each with an absolute vault
path, so every rename broke a real reference — while the confirm still quoted
"20 files in this library are", measured on 2026-08-21. `set_attachment_path`
PATCHes one field (the display title is deliberately untouched) under
`retire-tag`'s four-phase choreography. The new path is Zotero's OWN stored
string with its basename swapped, never one re-derived from the route's
realpath, and `find_linked_attachments` returns `(refs, error)` so "Zotero
holds no link" is never confused with "Zotero could not be read".

**Broken links are visible and repairable.** `zotero_attachments` rows carry
`exists`; a broken row says so in text and offers `[re-point…]`, a picker
ranked against the filename Zotero remembers. It reads 0 of 5 242 today,
which is the right moment to build it — the nine hand repairs after the
Zotero-preference and Attanger incidents are what it automates.

**A note can link to its own PDF.** A `pdf:` frontmatter list of wikilinks,
written by `POST /api/tagging/apply-pdf-links` from the cached scan's join
(KEYS ONLY on the wire). It is one machine-written shape because of the
renamer: the rename route rewrites exactly that shape inside its own
journalled op, and any other reference still refuses.

**Fixed:** the Attanger skip-rule reader round-tripped `prefs.js` strings
through `unicode_escape`, a latin-1 codec, so a rule naming a non-ASCII
surname came back mojibake — a valid regex matching nothing, which in that
gate means every PDF is skipped as `plugin_would_rename` and the run writes
nothing while blaming the files.

## 2026-09-06 — Library harmonization, Phase 4: linked files into Zotero

The forward verb the de-link has been the undo of. `POST /api/zotlink/attach`
(SSE, `{citation_keys ≤ 200, confirm, dry_run?}`) creates one `linked_file`
attachment on the Zotero item for each of a record's mapped PDFs — a path and
no bytes, so no cloud storage is used and it syncs down within seconds (spike
S1). Absolute vault paths by maintainer decision (the Linked Attachment Base
Directory is unset). Personal library only: a group-library record is
skipped with a reason, never refused as a whole (spike S2). A PDF Zotero
already links is skipped, so a re-run after sync-down is idempotent.

* Three phases, the note-create shape: plan + parent reads lock-free, POSTs
  lock-free, ONE short hold to journal the creates as
  `zotero_attachment_create` (a new never-evicted external kind whose revert
  is a TRASH — `tagging/writers.py::_revert_zotero_attachment_create*`,
  dispatched by the single and batch restore routes). Never a DELETE; the
  source ratchet still holds.
* `GET /api/zotlink/attach-candidates` lists every record with a Zotero item
  and ≥1 mapped PDF, what Zotero already links, and the group-library rows.
* UI: a "Link vault PDFs into Zotero" section on the Zotero Attachments tab
  (lazy candidate load, selection, dry run, confirm-gated run, per-row
  outcomes; undo is the existing remove verb / Tagging → Restore).
* **Zotero's "Rename linked files" preference is a hard gate (2026-09-07).**
  The first live run created five links and Zotero immediately renamed the
  five VAULT files per its template (`_.pdf` … `_ 5.pdf`, because those
  institutional parents carry no author or year). The attach route now reads
  `extensions.zotero.autoRenameFiles.linked` from `prefs.js` and refuses —
  dry run included — while it is on. The five files were renamed back and
  their attachments re-pointed by hand (version-guarded path PATCHes), links
  kept. Note Zotero rewrites `prefs.js` on exit, so the gate lifts only after
  a Zotero restart following the preference change.
* **Attanger and the app reconcile through Attanger's own skip rule
  (2026-09-07).** Read from the plugin's code: its "skip auto move and
  rename" and "skip rename" fields are comma-separated regexes tested
  against the filename without extension; a match stops the rename (the
  second stops the move too, so the FIRST is the one to use with a connector
  import flow). The attach route reads both from `prefs.js`; with a rule present
  the run proceeds and each PDF is guarded PER FILE — a name the rule does
  not cover is skipped with `reason: plugin_would_rename`, never linked. The
  refusal names the rule to paste (`SUGGESTED_SKIP_RULE`, the vault's
  author_year convention, which every one of the 5 606 conventional PDFs
  already matches). Web imports, which arrive under browser names, are
  still renamed and filed by Attanger as before.
* **Attachment-renaming plugins are a second gate (2026-09-07).** With
  Zotero's own preference off, the Attanger plugin renamed the next four
  files on link creation (`ades_2017.pdf` → `adès_.pdf`,
  `bland_1_1998.pdf` → `bland_1998.pdf`). The route refuses while Attanger,
  ZotFile or ZotMoov appears in `prefs.js` unless the request carries
  `acknowledge_plugins: true` (UI checkbox, CLI `--acknowledge-plugins`),
  meaning the user has disabled the plugin's auto-rename/move. The four
  files were renamed back and re-pointed by hand, links kept.
* **Same-day fixes from first use.** The primary "Link selected into Zotero"
  button never fired — the report body's row-action delegate matches
  `.audit-link-btn` only and the button is a `.btn`; it is now bound
  directly (ratcheted). The attach cap is 7 500 records per run, not 200 (the whole library in one run).
  `POST /api/accession/batch` accepts `force: true` (bulk RE-identify,
  bypassing the cache consult — the per-row verb's semantics batched) and
  the Not in Zotero tab offers `[re-identify selected]`; the
  `accession_batch_max` ceiling is 7 500 (Settings → Accession; default still 50).
* CLI: `papermind zotlink candidates` and `papermind zotlink attach
  --keys/--keys-file [--dry-run]` (driver rows, replay, projection and CLI
  tests). MCP gains no write tool.

## 2026-09-06 — Library harmonization, Phase 2: the second curated state, detach, and the CLI rows

* **`pending` — "add to Zotero later".** `mapping.json` gains an additive
  `pending` list beside `no_match`; the bridge treats both as curated and
  matched (`BridgeResult.pending_pdfs`, hash-anchored recovery included), the
  *Not in Zotero* tab lists both with their state, *PDFs Without Zotero*
  discloses the count with a jump, and the dashboard gains a tile. Every
  triage surface — the accession deck (`P`), the ambiguous deck and table —
  offers it beside "not in bib", and moving between the two is one verb.
* **`detach`** (`{pdf, detach_from: key}`): the multi-PDF review's verb.
  Writes `exclude[rel] = [key…]`; `build_bridge._link` now refuses every
  automatic rung's claim of that PDF for that key, so an absorbed
  numbered-sibling file returns to the orphan queue instead of being
  re-absorbed at the next scan. A hand link to the same key lifts the
  exclusion; `clear` removes it. Per-file `[detach]` on *Entries With
  Several PDFs*.
* **List form of the mapping route** (`pdfs: [...]`, ≤ 500) for the three
  bucket verbs, validated in full before ONE atomic write — a bulk re-sort
  never half-lands. *Not in Zotero* gains row selection with
  `[selected → pending]` / `[→ not in bib]` / `[clear selected]`, skipping
  rows a table filter hides.
* **Writers load `MappingData` and save every field.** `_load_mapping_all`
  is the writers' reader; the 4-tuple `_load_mapping_full` stays for
  read-only consumers. A writer built on the old shape would silently erase
  `pending`/`exclude` — pinned.
* **Identification reaches pending PDFs.** `api/routes/accession.py::_resolve_orphan`
  accepts the curated `pending` bucket beside the orphan queue (never
  `no_match`), so the Not in Zotero tab offers per-row `[identify]` /
  `[re-identify]` and a confirm-gated `[identify selected]` batch, repainting
  the resolution cells in place; the copy-DOIs hand-off then covers what the
  resolver found. Live: 88 of the 362 pending files had never been looked at.
* **Headless rows:** `papermind audit mapping` (`--link`/`--detach`/
  `--no-match`/`--pending`/`--clear` over `--pdfs`/`--pdfs-file`) and
  `papermind audit pdf-decision` (`--dismiss`/`--clear`/`--clear-all`),
  with the driver registry rows, replay cases and projection tests. MCP
  deliberately gains no write tool (the elicitation gap).

## 2026-09-06 — Library harmonization, Phase 0 + Phase 1: the invisible buckets get tabs

Plan in `docs/plan_2026-09-06_library_harmonization.md` (deleted after Phase 4
ships). A read-only assessment measured the live library and found two facts
the app could not show: the 291 PDFs curated "not in bib" were listed by no
report (and 203 of them carried a resolvable DOI — the intent had been "add to
Zotero later"), and the 107 citation keys owning two or more PDFs had all been
dismissed in Possible Duplicates, the only surface that ever showed them.

* **Phase 0 — two live spikes against api.zotero.org.** A `linked_file`
  attachment is a plain item write: created with an absolute `path`, no upload
  step, `GET …/file` 404, synced down to the desktop within 5 s, seen by the
  app's Zotero reader, removed afterwards. A group library refused with 403
  because of its `fileEditing: none` policy, so the future writer gates on
  library type, never on that status. Outcomes recorded in the plan's §7.
* **Three read-only audit reports** (`api/routes/audit.py::_REPORT_NAMES`,
  tabs, search corpus, CLI/MCP allowlist): **Entries With Several PDFs**
  (`multi_pdf` — per-file match rung, mixed-provenance groups first, the
  Possible Duplicates decision mirrored from the hash memo), **Not in Zotero**
  (`not_in_zotero` — the curated `no_match` bucket joined by memoised sha to
  the cached accession resolution, with a `[copy N DOIs]` hand-off for
  Zotero's Add Item by Identifier; the join is route-injected because the
  vendored tree may never import `accession`), and **Dead Curation**
  (`dead_curation` — mapping.json entries pointing at a missing file or a
  citation key absent from both bib and Zotero). No write verb: those come
  with Phase 2.
* **Driver allowlist repaired.** `core/driver.py::AUDIT_REPORT_NAMES` had
  fallen one behind the route (`zotero_attachments`), so that report was
  GUI-only; it and the three new names are now reachable over `audit_report`.
* **Two documentation drifts closed.** The root CLAUDE.md still recorded
  "`add_matches` leaves the PDF under both keys" as an open bug — the sweep
  has been in `bridge.py` for some time; and `audit/CLAUDE.md` said fourteen
  reports where the route served fifteen (now eighteen).

## 2026-09-04 — Note sync, Phases 4 + 5: pull, push, merge, and the surfaces

The two-way sync plan's last two phases. Every write in either direction is
a confirm-gated verb, each reversible from the Restore panel that already
owns that side (Note Refactor for the vault, Tagging for Zotero), and a
conflict is never a one-click verb in any surface.

* **Pull** (`POST /api/notesync/pull`) writes today's Zotero note — read
  FRESH, not the scan's copy — into the marked region, prefix and suffix
  byte-identical, journalled as `notesync_update`; a `conflict` needs
  `accept_conflict`, the two-decisions rule. **Push** (`/push`) writes the
  region to the Zotero note through the web API in retire-tag's four-phase
  choreography (reads lock-free → ONE hold to journal → PATCHes lock-free →
  ONE hold to record outcomes), as a `zotero_note_write` op in the tagging
  journal with the server's own `before` HTML inline; a failed PATCH flips the
  op to `failed`, never leaves an `applied` claim. Push additionally refuses
  when the API is ahead of the local SQLite — the listing could not see that
  edit, and writing over it is exactly the conflict case. Both verbs write
  the base record after success with the text they SENT, so the next pass
  does not flag the app's own normalisation as an edit.
* **Merge** (`/merge-preview`, `/merge`): the three texts of a note and a
  pre-filled three-way merge (`notesync/merge.py`, a small line-level diff3:
  one side's change applies, an identical change applies once, overlapping
  changes become one marked block). The user edits the result and chooses
  where it lands — both sides, the note only, or Zotero only — and whatever
  is not written is accepted as it is. A result still carrying markers is
  refused; the write is stale-diff guarded on the note's hash.
* **Create in Zotero** (`/create-zotero-note`): the only verb that creates a
  Zotero item, opt-in and per row; journalled as `zotero_note_create` whose
  undo is a TRASH, never a delete. The note's frontmatter then gains the two
  keys through a journalled note write.
* **Reverts** for both op kinds live in `notesync/zotero_notes.py` with the
  same prepare/commit split as the tag reverts, and the Tagging → Restore
  route dispatches them on both its single-op and batch paths.
* **Surfaces:** the Zotero Notes Sync tab gains a two-way section — counts,
  bulk pull/push over VISIBLE rows of exactly that state (a conflict can
  never ride a bulk verb), a baseline verb, per-row pull/push/merge/create,
  and the merge view built on the shared side-by-side diff. `papermind
  notesync status` and the MCP tool `notesync_status` are read-only; no
  write tool is exposed until elicitation can put the confirm in front of a
  human. Help guide section added.
* **Fresh-vs-scan trap, closed by construction:** the update path's region
  renderer reads the record the SCAN cached, so `pull` builds a record copy
  whose child note is today's (`_fresh_record`); anything that pulls from the
  cached copy would silently write last scan's text.

## 2026-09-04 — Note sync, Phase 3: the two-way state

Third phase of the sync plan. The app can now say WHICH side of a reference
note changed, which is the question a two-way sync turns on and the one the
containment test could never answer.

* **`notesync/state.py`** (pure) is the 2×2 table — `in_sync` · `pull` ·
  `push` · `conflict` — decided by text against a per-note **base record**
  (`BaseRecord`: the Zotero HTML and the region bytes at the last sync, the
  note item's version, the converter's identity). "Zotero changed" compares
  the converter's rendering of now against its rendering of the base, so
  converter-vs-plugin formatting can never masquerade as an edit; a base
  written by another converter is treated as absent rather than compared
  across converters. Without a base the note is `unbaselined` and the
  pre-Phase-3 word/structure test (`bootstrap_state`) says what is at stake.
* **`notesync/base_store.py`** keeps one fsynced JSON per citation key under
  `BASE_DIR/notesync/base/<vault>/` — never the vault — named by a digest of
  the key so a key can never shape a path; a corrupt, tampered or foreign
  file loads as `None`.
* **`GET /api/notesync/sync-status`** reads Zotero FRESH (`read_items` over
  the current SQLite, memoised on the file's size + mtime; 0.49 s live) and
  joins it to the cached scan's key map — the scan stays the source of the
  citekey ↔ item join, the text and version come from the database as it is
  now, so a sync pass no longer waits on an audit scan. An unreadable
  database is a 503, never a stale listing. **`POST /api/notesync/bootstrap`**
  writes a base for every unbaselined note that is in step today and refuses
  the rest with `needs_decision` — it is the one bulk verb that touches
  neither the vault nor Zotero.

## 2026-09-04 — Note sync, Phase 2: the marker layout

Second phase of the two-way sync plan. Every reference note can now carry
an explicit region boundary and the Zotero keys that address it, and the
app can put them there — and take them out again — without touching a byte
of the region itself.

* **`notesync/migrate.py`** wraps the region `split_note_parts` already
  identifies in `<!-- zotero-note:begin -->` / `<!-- zotero-note:end -->`
  (HTML comments by maintainer decision: hidden in Obsidian, dropped by a
  pandoc export) and appends `zotero-key` / `zotero-note-key` to the
  frontmatter as the last lines of the block, in the note's own newline
  style, behind a YAML round-trip guard that refuses any other change to the
  mapping. `unmigrate_note` is the exact inverse; `unmigrate(migrate(x)) == x`
  is pinned for six note shapes including CRLF, an empty region and a note
  with no trailing newline. **Not journalled**: 1,235 notes cannot ride the
  refactor journal's 200-op window, so the undo is the inverse verb plus the
  vault's git — the write route refuses unless the notes folder is committed.
  Dry-run over the live vault: 1,234 of 1,234 notes plan cleanly and invert
  byte-for-byte; +100 bytes per note.
* **`notesync.compare.split_note_parts`** returns `(prefix, region, suffix)`
  with `prefix + region + suffix == raw`, preferring the marker pair and
  falling back to the single-rule shape, so migrated and unmigrated notes
  coexist. The update path writes `prefix + new_region + suffix`, which is
  what keeps anything the user writes BELOW the end marker byte-identical
  across a refresh. `split_note` keeps its two-part read-side shape.
* **`DEFAULT_TEMPLATE` is marked**, so app-created notes arrive in the new
  layout with the keys stamped, and `render_region(marked_target=…)` renders
  the legacy shape for an unmarked target — measured identical bytes for the
  same note either way, so no unmigrated note reads as changed.
* Routes `POST /api/notesync/migrate` and `/unmigrate` (dry run without
  `confirm`; op-locked, heartbeat acted on, re-read under the lock, a note
  edited between plan and write is skipped as `changed_on_disk`), a `layout`
  block on `/status`, and a layout line on the Zotero Notes Sync tab with the
  two verbs behind `confirmInline`. The Zotero Integration template lines to
  add are returned as `template_hint` and logged to the console; the app
  never edits the plugin's template file.

## 2026-09-04 — Note sync, Phase 1: the Markdown reference

First shipped phase of the two-way Zotero ⇄ Obsidian reading-note sync plan
(`docs/plan_2026-09-04_zotero_note_sync.md`, approved the same day). No layout
change, no new write path; this phase corrects what the app compares and
renders a Zotero note AGAINST.

* **`notesync/convert.py`** is the one HTML ⇄ Markdown pair the sync path
  uses: markdownify with `-` bullets, misc-escaping on and `<img>` nodes passed
  through verbatim; markdown-it (CommonMark + tables, raw HTML on) wrapped in
  Zotero's `znv1` shell for the write direction. Each setting was measured over
  all 1,236 live child notes (spike S2): the plugin emits `-` bullets, so the
  default `*` alone produced 277 false "annotated" rows; without misc-escaping
  a line beginning `>20 : severe` or `+ fqt que MDD` round-tripped as a
  BLOCKQUOTE or a nested LIST — the only failure class that changes meaning;
  and the 7 notes embedding Zotero image attachments would have lost them.
  Non-breaking spaces are emitted as `&nbsp;` because markdown-it-py strips a
  leading U+00A0 (Python's `str.strip` counts it as whitespace) and the
  de-indented text re-parsed as a list. 1,211 of 1,236 notes are exact
  round-trip fixed points; the residue is blank-line placement (14) and bold
  adjacent to punctuation that CommonMark cannot express (11, degrades to
  literal asterisks — the accepted "lossy but readable"). `CONVERTER_ID` names
  the pair so a later sync base record can invalidate itself loudly.
* **`notesync.render.zotero_note_text` returns Markdown.** It returned
  `body_plain` — the HTML stripped to text — so a note the app created was
  LOSSIER than one the plugin created, and the comparison was measured against
  a reference the vault never contained. Live matrix before → after: "annotated
  by the user" 555 → 48, "conflict" 15 → 8, and every one of the 8 is a genuine
  two-sided edit. The 0.85 containment threshold was re-measured against the
  Markdown reference and still flags exactly the same three notes, so it is
  unchanged. A note object without `body_html` falls back to `body_plain`.
* **`ZoteroChildNote` gains `body_html` and `version`** (additive; the reader
  also selects `items.version`). The Markdown rendering deliberately does not
  live in `audit/` — that tree depends on nothing but `audit/config.py`.
* **`notesync.compare` folds `&nbsp;`/U+00A0 to a space in every tokenizer and
  ignores `<img …>` tags** — left in, "nbsp" counted as a word on one side only
  (111 false "update" rows) and the image attributes as words (7 more).
* **`markdownify` and `markdown-it-py` are first-class dependencies.** They had
  reached the tree only transitively through markitdown; both lockfiles
  regenerated (the only diff is the two new `via requirements.txt` lines).
* **Spike S1, live against api.zotero.org on a throwaway item:** a child note
  PATCH with `If-Unmodified-Since-Version` answers 204, a stale one 412, the
  `<img data-attachment-key>` node survives create and PATCH byte-exact, the
  shell is stored verbatim, and a tag write on the PARENT leaves the note's own
  version untouched. The item is in Zotero's trash.

## 2026-09-04 — CI back to green, and one bulk verb

CI had been red on every push since 2026-08-28. Five commits: the blocker and
its two neighbours, one rendering defect the user reported, and the bulk action
the Zotero-attachments route had always supported without a client for it.

**CI (three commits).** The single failing step was the dependency-audit gate,
and it was failing for a reason no change to our dependencies could have fixed:
`pip-audit` ran with no `-r`, so it audited the live ENVIRONMENT. On
ubuntu-24.04 that swept the runner's own system packages — 26 of its 50
findings were about `twisted`, `pyjwt`, `pyasn1`, `pyopenssl`, `httplib2`,
`configobj` and `wheel`, none of them project dependencies, none present in
either lockfile, and its skip list named `cloud-init`, `ufw` and `walinuxagent`
outright. It was also not reproducible on a developer machine, which is why it
sat for a week.

* **The gate now audits the platform's hash-locked requirements file**, which
  is the graph CI actually installs. Measured: 50 findings across 10 packages →
  26 across 3. `--require-hashes` rather than `--no-deps`, so a lockfile
  regenerated without `--generate-hashes` fails the gate instead of being
  audited silently as a plain pin list; a missing lockfile is a failure too,
  because the gate must never report a pass it did not perform.
* **`nltk` 3.10.0 → 3.10.3, `transformers` 5.8.0 → 5.10.1, `banks` 2.4.2 →
  2.4.5**, clearing 25 advisories. transformers is 5.10.1 and NOT the 5.10.0
  pip-audit names as the fix: 5.10.0 is yanked upstream ("pushed from a week
  old main branch … missing a bunch of fixes"), which uv reports during the
  compile. Both lockfiles regenerated; the diff is exactly these three
  packages. Verified beyond the suite — the pinned chunking invariant holds, so
  no reindex is forced, and the cross-encoder was loaded and scored by hand
  because the suite mocks it.
* **The 26th finding is waived, with its reachability argued rather than
  asserted.** PYSEC-2026-3740 on nltk has NO upstream fix — the advisory says
  so itself — and is a sandbox bypass in model-artifact load/save helpers
  (`TransitionParser`, `AveragedPerceptron`, `PerceptronTagger.save_to_json`,
  `save_maxent_params`) that appear nowhere in this tree; nltk enters
  transitively for the punkt_tab/stopwords corpora, which the app seeds itself,
  and exploiting it needs a caller-controlled model path the app never passes.
  Expires 2026-12-03.
* **All four pinned actions moved off the Node 20 runtime** (checkout v7.0.1,
  setup-python v7.0.0, setup-node v7.0.0, gitleaks v3.0.0), in live-smoke.yml
  as well as ci.yml. Every run had been warning about this, and it was a
  deadline rather than a grumble: GitHub removes Node 20 from hosted runners on
  2026-09-16. Every major crossed was read — the only removals are things this
  workflow does not use. The version comments were stale in a way worth not
  repeating: three named v4.2.2 beside a SHA that is in fact v4.4.0, so the
  human-readable half of the pin had drifted from the enforced half.

**A row of commas above the "Needs review" table.** User-reported, and a real
defect rather than a cosmetic one. `_renderNoteSync`'s two update tables passed
`rows.map(...)` to `_tbodyHtml` without `.join('')`, where the four sibling
tables in the same function join correctly; the template literal coerced the
array with `Array.prototype.toString`, and because the commas land between
`<tbody>` and `<tr>` the parser foster-parents them out of the table and drops
them immediately before it — which is also where `installTableControls` mounts
its filter strip. 15 conflicts, 14 commas. Invisible at 0 or 1 row, so the fix
is not only the two call sites: `_tbodyHtml` now joins an array rather than
coercing it, covering its other ten callers, and a ratchet walks every call
site in the file.

**Bulk de-link on the Zotero Attachments report.** `POST /api/zotlink/trash`
has accepted `item_keys: [...]` up to 200 since it shipped — SSE progress, the
annotation acknowledgement, per-key results, one batch_id for the undo — but
the report only offered a per-row verb, so clearing a folder's worth of
attachments meant one confirm each. Client-side only; the server still
re-derives every attachment from the last scan's index. Selection model and
lifetime are Tag Harmony's verbatim. Four decisions that are silent if got
wrong: the confirm names the SUMMED annotation loss and the acknowledgement
rides only when the selection actually contains annotated rows (a blanket true
would disarm the server's second gate for every batch); "select all" skips a
row the table filter is HIDING, since it hides rather than removes and this is
a destructive verb; outcomes are read per key and a failure goes back into the
selection so a retry picks up the remainder; and rows are marked in place,
never removed, because the report is a view of one cached scan. Three of the
11 new ratchets were proven red by breaking the code.

## 2026-09-03 — UI / UX review

Reviewed the app as a user meets it, by booting the real shell in Chromium at
first run (throwaway `BASE_DIR`, every tab) rather than by reading markup.
Four fixes applied, three findings checked and deliberately left, one migration
scoped and costed into the registry.

**Applied.**

* **The landing tab had no empty state.** The Obsidian Agent tab — the tab the
  app opens on, and the one the Library Audit's own empty state directs a new
  user to — showed "Vault: Not configured", an empty box and a Send button,
  with nothing about needing an index. It now carries a three-register hint
  decided from what `/api/obsidian/status` already returns: pick a folder, or
  index it, or ask a question. A hint and not a message (no `role`), never
  painted over a transcript, removed when the first turn starts, silent
  mid-run, and flipped in place when an index completes.
* **The settings filter had no zero-result answer.** A query matching nothing
  hid every section and left "0 match(es)" — a dead end on the one surface a
  user searches when a setting is missing, in an app with three settings
  surfaces. It now names the other two. (The recorded "give the audit modal a
  filter too" idea was dropped on measurement: that modal has 10 controls and
  no nesting.)
* **Three CSS custom properties were used and never declared.** `--font-mono`
  at seven sites, so those seven rendered Courier while ten sibling surfaces
  declared the SF Mono stack literally; `--mono` as a second spelling; and
  `--bg` in the command palette's scrim, which therefore always painted 40%
  black instead of the theme-aware `--overlay-scrim` every modal uses. CSS
  fallbacks are why this looked fine and was wrong. `tests/js/cssTokens.test.js`
  now fails on any `var()` whose token is undeclared.
* **Two axe violations the shell-only run structurally could not see**, plus
  focus restoration after a confirmed write. Details in the a11y commit: an
  unlabelled `.sr-only` file input (CRITICAL) and an unfocusable scroll region
  (SERIOUS), found by auditing each tab panel — now a committed test — and
  `confirmInline`'s Confirm returning focus to a surviving anchor, which Cancel
  and Escape already did.

**Checked, adequate, left alone** (recorded in the registry so it is not
re-derived): the new streaming-cap 503 reaches every client as the server's own
retryable sentence, with draft preservation and Retry boundaries already in
place; the audit Settings modal needs no filter; and all three keyboard decks
are properly focusable named regions.

**Scoped, not started:** the 113 inline `on*=` handlers whose hashes force
`'unsafe-hashes'` into the CSP. The registry entry now carries the measurement
— 104 of 113 mechanically convertible, three delegates retiring 42 of them,
nine needing hand-written bodies, the order of work, and the two ratchets to
add with it.

## 2026-09-03 — Documentation: truth pass, then the registry by subsystem

No behaviour change. Three commits.

* **CLAUDE.md described mechanisms that no longer exist.** The 2026-09-02
  performance plan landed here but not in the invariant doc, so five symbols a
  reader now has to know were absent (`_MAX_LIVE_SSE_WORKERS`,
  `_budget_tool_history`, `run_open`, `join_snapshot_refresh`,
  `_INVENTORY_ROWS_CACHE`) and eight bullets described superseded behaviour.
  Each is now recorded at its owning Module Ownership bullet as an INVARIANT
  and the reason it holds — never as change prose, which is this file's job.
  The SSE call-site count was re-measured (19/9 → 20/10; `zotlink` was added
  2026-08-30 and nobody re-counted).
* **Known Issues was 397 of 705 lines across 20 date-keyed subsections.** Every
  entry was checked against the CODE rather than against its own text. Two were
  found already fixed and are struck with the mechanism (`extract-image`'s
  unbounded inline vision call; `retire-tag` saving its manifest after
  `lock_lost`). Seven findings that appeared in two or three subsections are
  merged into one live entry each. **Nothing was deleted** — a bulk check of
  every backticked symbol and path in the section found no entry describing
  code that no longer exists, and a completeness check proves all 164 parsed
  blocks survive verbatim or in a named merge. The result is 16 subsystem
  sections, each entry stamped with the review it came from, plus *Measured and
  declined* (each carrying the number that settled it) and *Closed, kept for
  the lesson*.
* **The one user-visible status code the plan introduced was undocumented.**
  README's Limits and the in-app guide now both explain the streaming cap's
  **503** as transient and retryable, and README's timeout-chain line says the
  online stall guard budgets the whole retry ladder. The two `docs/ideas_*`
  files keep their ideas and gain a dated status pass so nobody re-plans what
  has since shipped.

## 2026-09-02 — The timeout chain, made uniform

Not part of Phases 3–5, but the same plan's outer-must-exceed-inner rule. Two
legs remained after Phase 1 closed the compile-fix one.

* **The online stall floor ignored the retry count.** `online_aware_floor_s`
  widened the consumer stall guard to ONE `online_timeout_s`, but the online
  adapters retry a transient failure `online_max_retries` times (attempts, not
  extra tries), each bounded by that same timeout, with a
  1 s-doubling-to-16 s backoff between them — so at a plausible 150 s / 3
  attempts the inner reaches 450+ against an outer of 330, and the app killed,
  and the provider still billed, a call that was inside its configured budget.
  The floor now budgets `attempts × timeout` plus the bounded backoff gaps
  (150/3: **330 → 482 s**). It only ever LENGTHENS the window, and only for a
  config that asked for a long one; the defaults are unchanged. Stated cost: a
  genuinely dead online stream takes longer to be declared timed out on such a
  config — the correct direction, since the alternative is killing legal calls.
* **The driver's SSE timeout was a per-read socket bound read everywhere as a
  wall clock.** `core/driver.py::_http_sse` passed it straight to `urlopen`,
  which bounds each individual READ — so a server emitting a frame every few
  seconds had no bound at all (`papermind maintain` could stream indefinitely
  inside a nominally 18 000 s call). The socket timeout still detects a dead
  peer; the consume loop now also stops once the budget has elapsed in total,
  returning the frames gathered so far with an explicit error — the shape
  every caller already handles, and the same thing a client disconnect already
  means to the SSE skeleton.
* **The MCP write-slot wait was shorter than the writes it serialises.** 300 s
  against a driver that gives a refactor apply 900 s, so a write queued behind
  a legitimate first one was told "busy, retry shortly" at a moment when
  retrying could only fail the same way. The wait is now 960 s, and the
  pool-starvation property the 300 s bound was introduced for is preserved
  exactly by bounding the WAITERS instead (`_WRITE_WAITER_MAX` 2, so holder +
  waiters can never occupy all four workers).

  The remaining leg — the client-side fetch abort, which recomputes its own
  formula in JS and knows nothing about the online knobs — is recorded in
  CLAUDE.md rather than fixed, because closing it means shipping the resolved
  floor to the client rather than duplicating the formula again.

  Pinned by `tests/test_sse_timeout_chain.py`,
  `tests/test_driver.py::TestSseWallClock` and
  `tests/test_mcp_server.py::TestWriteSlotWaiting`, red against the pre-change
  code with `300 not greater than 450 : the guard still kills a legal retried
  call`, `'error' not found in {...}` and `300.0 not greater than 900.0 : a
  write can be refused inside a legal write`.

## 2026-09-02 — Performance plan, Phase 5: durability

* **Three write orders that a crash could catch mid-way.** The BM25 sidecar
  was `rmtree`'d and only then rebuilt IN PLACE, so from the first line onwards
  there was a window — the whole persist of a large index — in which a crash,
  or simply a concurrent cold launch, found NO sidecar and paid the full
  re-tokenise the sidecar exists to avoid; it now builds in `<sidecar>.new` and
  swaps, so a failed persist leaves the PREVIOUS sidecar standing (a stale one
  self-invalidates on load, and having one beats having none). The index meta —
  the file every loader reads first, whose `phase: done` is what makes an index
  look complete — was written BEFORE the manifest it advertises, so a crash in
  the gap left a "done" index with a missing or stale
  `indexed_materials.json`, sending the materials route back to parsing the
  whole docstore (the cost Phase 2 removed from the request thread); the two
  writes are simply swapped, both still inside the same brief write-lock hold.
  And `deckgen/checkpoint.py::prune` only ever listed `.json`, so a
  `.ckpt-*.tmp` left by a SIGKILL between `mkstemp` and `os.replace` could
  never be seen or removed and accumulated one per killed generation; prune now
  unlinks temp files older than `_TMP_ORPHAN_AGE_S` (1 h) — the age gate is
  what makes the sweep safe beside a live save. Pinned by
  `test_vault_regressions.py::TestPhase5WriteOrdering` and
  `deckgen/tests/test_deckgen.py::TestCheckpointTmpOrphans`, red against the
  pre-change code with `the sidecar was destroyed` and `Lists differ:
  ['meta', 'manifest'] != ['manifest', 'meta']`.

* **Every `open` child was a zombie, and two recorders could rotate the usage
  log over each other.** The four route sites that hand a path or URL to macOS
  `open` (`api/routes/open.py`, two in `audit.py`, one in `tagging.py`) called
  `subprocess.Popen` and never waited, so each reveal/open left a defunct
  process entry for the life of the app — one per click, all session long. They
  now go through `open.py::run_open`, which uses `subprocess.run` with a short
  timeout: it reaps the child, and kills a wedged one instead of leaking it. A
  timeout is a warning, not a failure, because `open` returns as soon as
  LaunchServices has the request; `OSError` still propagates, so the routes'
  500 path is unchanged. Separately, `_rotate_log_if_large` checked the size
  and renamed WITHOUT holding the lock, so two recorders whose appends both
  crossed the 10 MB cap each ran `os.replace(log, log.1)` — the second moving
  the new, nearly-empty live log over the generation the first had just
  preserved. The size is now re-read inside the lock and the rotation skipped
  when another recorder has already done it. Pinned by
  `test_open.py::TestOpenChildIsReaped` and
  `test_llm.py::TestUsageRotationRace`; the existing route tests were repointed
  from `subprocess.Popen` to the consumer's `run_open` name.

* **The three unregenerable decision sidecars now fsync their directory
  entry.** `mapping.json` (hand-curated PDF↔citation-key matches),
  `dup_decisions.json` and `pdf_wanted.json` each fsync the FILE before the
  rename, so the content is durable — but not the parent directory, so the
  entry naming the new file may not be, and a power loss right after the
  rename can leave the old file (or none) despite a successful save. The
  app-wide policy omits parent fsync for REGENERABLE files deliberately; these
  three are the one place it costs something real. Best-effort: a filesystem
  that refuses a directory fsync is not a reason to fail a write that has
  already landed. Pinned by `tests/audit/test_sidecar_dir_fsync.py`, red
  against the pre-change code with `assert []` (no directory fd was ever
  fsynced).

* **The scan-time curated-mapping re-anchor could overwrite a FRESHER anchor.**
  `refresh_mapping_hashes` loads `mapping.json`, hashes every curated file
  (O(curated) whole-file reads, deliberately outside the RMW lock), then
  re-loads under the lock and wrote back on PRESENCE alone — so a
  `POST /api/audit/mapping` landing in that window, which re-anchors a PDF to
  its CURRENT bytes, was silently reverted to the refresh's own older digest.
  The value seen at the first load is now carried through and an entry is
  written only when the freshly loaded value still equals it (compare-and-set).
  A concurrent writer's anchor is by definition at least as fresh as ours, so
  skipping is always right; an untouched entry refreshes exactly as before and
  an entry curated AWAY in the window is still skipped. Lock scope unchanged.
  Pinned by `tests/audit/test_bridge_reanchor.py`, red against the pre-change
  code with `AssertionError: the refresh reverted a fresher anchor to its own
  older digest`.

* **A Finder op's restore mapping lived in memory for up to 25 papers.** A note
  op's `.bak` snapshot is durable BEFORE its write; a Finder op's
  `before`/`before_raw` lists — the ONLY record of which colours to put back —
  existed only in the manifest, which a batch checkpoints every
  `JOURNAL_FLUSH_EVERY` (25) ops. A crash between an xattr write and the next
  checkpoint therefore lost the colours for up to 25 papers, with only
  `log_vault_write`'s names left. `journal.write_external_snapshot` now
  persists the same mapping as one small fsynced JSON sidecar named by op id,
  written before the first xattr change and reclaimed by `prune` through the
  same `snapshot_rel` field the note snapshots use — parity at last, and
  **without a per-op SQLite transaction**, so a batch's write cost is unchanged
  in kind. The inline lists stay the revert's first source; the sidecar is the
  recovery-of-last-resort fallback. A sidecar write failure logs and proceeds,
  which is exactly the pre-fix durability. The Known Issues entry is struck in
  the same commit. Pinned by `test_tagging.py::TestFinderSnapshotDurability`,
  red against the pre-change code with `AssertionError: 0 != 1 : sidecars on
  disk: []`.

## 2026-09-02 — Performance plan, Phase 4: bounds on growth

* **Uncapped SSE workers, an uncancellable corpus build, and a scan thread
  doing widget work.** The finding's fourth part was measured and declined.
  * **A process-wide cap on live SSE workers.** All 19 streaming call sites
    spawned a daemon worker per request with no process-wide bound, and the
    threads are daemons — invisible to the Waitress pool bound — so a wedged
    local backend accumulated one blocked thread per retry with nothing
    refusing the next. `api/sse.py` now takes one of `_MAX_LIVE_SSE_WORKERS`
    (12) slots BEFORE the Response is built, so a refusal is a real **503**
    rather than an error frame inside a 200, and releases it through the SAME
    `_SSEOpGuard` that owns the op lock (stream ended AND any worker exited).
    A refusal hands the caller's op lock back and runs its `on_stream_finished`
    observer — otherwise the cap would strand exactly the locks Phase 0
    stopped leaking.
  * **The search-corpus build can be cancelled.** A scan finishing mid-build
    left the OLD Inventory pinned by the build's own arguments for the rest of
    a run whose result the supersession check then discarded — two
    multi-hundred-MB inventories at once, for minutes on a large vault.
    `_clear_search_cache` (the manager's result listener) now sets an event
    that `build_corpus` polls BETWEEN report sections, raising
    `CorpusCancelled`; a partial corpus is never returned, and a cancelled
    build is not recorded as an error.
  * **The widget snapshot left the scan thread.** The result listener built the
    payload inline — including a PDF text extraction for the excerpt — so a
    scan that had already reported `done` kept its worker busy. It now
    enqueues onto one coalescing daemon (`widget-snapshot`, joined by
    `suite_helpers.join_post_index_daemons` and by the targeted
    `api.routes.widget.join_snapshot_refresh`); repeat requests collapse into a
    single follow-up build. The manual `POST /api/widget/refresh` stays
    synchronous, because its caller wants the result in its response.
  * Declined on measurement: lazy Zotero child-note bodies — **1 236 notes,
    1.06 MB total** on the live library (see CLAUDE.md).

  Pinned by `tests/test_sse_worker_cap.py`,
  `test_audit.py::TestSearchCorpusCancel` and
  `test_widget.py::TestSnapshotOffScanThread`, red against the pre-change code
  with `200 != 503`, `6 builds` and `unexpectedly identical: <_MainThread…> :
  the build ran on the caller's (scan) thread`; the corpus poll was
  additionally proven red against a deliberately wrong entry-only variant
  (`CorpusCancelled not raised`).

* **Five caches with no bound.** The three ONLINE adapters' client caches
  (`openai.py`, `anthropic.py`, `google.py`) were plain dicts with no eviction,
  so a rotated API key stranded the previous client's idle connection pool for
  the process lifetime; they are now `OrderedDict`s capped at
  `_CLIENT_CACHE_MAX` (8) and LRU-evicted, exactly as the local adapters
  already are — an evicted client is not closed in place (a concurrent call may
  still be streaming on it), and key composition is untouched so a new key
  still yields a new client. `refactor/resolver.py`'s index cache kept one
  full-vault name+link index pair per `(root, exclusion set)` EVER used; it is
  capped at 2, matching its sibling `archive.py::_REF_INDEX_MAX_ROOTS`.
  Browser-side, the tagging deck's snippet cache was an unbounded object that
  `taggingRenderInto` **wiped on every render** — so flipping the
  with-PDF/all scope threw away every excerpt the user had already paid a
  server-side extraction for; it is now a 200-entry LRU that SURVIVES a scope
  flip or view toggle and is dropped only when a new `scanned_at` arrives,
  beside the selection it shares that rule with. And the audit report body now
  releases a report's cached payload (`_acc.rows`, `_acc.report`, the
  ambiguous payload) when a DIFFERENT report takes the body, instead of holding
  it for the session. Declined on measurement: narrowing the archive sweep's
  canvas index to basename sets — its substring test is deliberately
  conservative on a destructive path, and the live vault holds zero `.canvas`
  files (see CLAUDE.md). Pinned by
  `test_concurrency.py::TestOnlineClientCacheBound` /
  `::TestResolverIndexCacheBound` and `tests/js/taggingSnippetCache.test.js`,
  red against the pre-change code with `39 not less than or equal to 8 : 39
  cached clients` and `expected [ 'a2020', 'a2020' ] to deeply equal
  [ 'a2020' ]`.

* **The usage tracker kept every parsed record and re-folded on every poll.**
  `_disk_records` parsed the whole JSONL — up to `_MAX_LOG_BYTES` (10 MB) live
  plus the same again rotated — into `UsageRecord` dataclasses, held them for
  the process lifetime, and `summary()` re-folded all of them on every call.
  The cache key is the log's `(size, mtime_ns)` and **every LLM call appends**,
  so any `/api/usage` poll during generation re-read and re-parsed everything.
  Measured on a synthetic 20 000-record log (5.0 MB): **301.8 ms per poll while
  appending, 9.4 MB retained**. The window is now kept as a FOLD, not a list —
  per-`since_iso` state holding the byte offset consumed of the live log, the
  rotated file's signature, the accumulated `UsageSummary` and a bounded tail
  of disk uids — so a later `summary()` parses only the appended bytes: **0.1 ms
  per poll, 0.1 MB retained**, cold cost unchanged. Anything that could
  invalidate an offset (a rotation, a shrunk or replaced live log, a changed
  rotated generation, a reconfigure) falls back to a full re-fold, so the
  incremental path is only ever taken for a pure append; the
  read-BOTH-generations rotation window (the 2026-08-09 fix) and the uid
  de-dup against the ring are preserved — the ring holds at most 500 records,
  so a uid it shares with disk is always inside the retained tail of 1 000.
  Pinned by `test_llm.py::TestUsageFold` (8 cases incl. rotation, a partial
  trailing line, per-window independence and fold immutability), red against
  the pre-change code with `AssertionError: 303 != 3 : 303 records parsed by
  the second poll`.

## 2026-09-02 — Performance plan, Phase 3: redundant I/O and compute

* **Frontend: bootstrap, listeners, the tab-switch walk and two per-token
  costs.** Six independent defects, one batch.
  * **Launch rendered the whole audit report.** `initAuditTab` ran
    `_loadReport` inside `app.js`'s init sequence, so a cached scan made the
    browser fetch and render the full inventory (**3.71 MB, ~6 000 rows**)
    before the refactor, deck, docgen and maintenance steps ran — on the vault
    tab, which is the one on screen. The load is now armed and runs on the
    first activation of the Audit tab (`ui.js::showTab` dispatches a
    feature-blind `chatekld:tabshown`; the hook is registered once per
    DOCUMENT). The dashboard tiles still refresh at launch, from
    `/api/audit/summary` — the endpoint that exists for exactly that — instead
    of as a by-product of the deferred inventory payload. The scan is neither
    started nor polled by this path.
  * **One closure per row verb.** `_wireRowActions` bound a listener to each
    `.audit-link-btn` and each `[data-inv-view]` / `[data-inv-detail]` on every
    render — **12 000 to 20 000 closures** for the 6 000-row inventory,
    re-created on every re-render. One delegated listener on
    `#audit-report-body` now serves all of them, attached once via a dataset
    marker; the deck views keep their own card wiring behind `_genericRowPass`,
    because their `[data-action="reveal"]` buttons carry `.audit-link-btn` too
    and a flag-blind delegate would fire twice.
  * **Leaving a tab walked every descendant.** `showTab` ran
    `prev.querySelectorAll('*')` and read `scrollTop` on every node to find the
    handful that had scrolled. One passive capture-phase `scroll` listener now
    records which elements actually scroll, per panel, and only those are read.
  * **The ambiguous-PDF view toggle re-fetched the report** for a view of the
    same cached scan; it now re-renders from the payload it already has (the
    accession toggle's pattern), decided/undo still keyed to the scan token.
  * **Plain chat forced a layout per streamed token** by wrapping the
    renderer's `update` to set `scrollTop = scrollHeight`. The scroll now rides
    `makeAnswerRenderer`'s own 40 ms coalesced render through a new optional
    `onRender` hook.
  * **The table filter wrote `hidden` on every row per keystroke** and ran the
    attachment sibling-walk for each; both now happen only when a row's
    visibility actually changes.

  Pinned by `tests/js/auditDeferredLoad.test.js`, `auditRowDelegate.test.js`
  and `tabScrollMemo.test.js` — red against the pre-change code with
  `expected 1 to be +0` (inventory fetched at launch), `expected 50 to be +0`
  (per-button listeners) and `expected [ '*' ] to not include '*'`.

* **A snippet or propose on an unindexed PDF hashed the whole file three
  times.** `tagging/text_source.py::_cache_probe` fingerprinted the document to
  locate its `.notext` marker, the fresh-extract path re-probed under its
  serialisation lock and fingerprinted it again, and
  `rag/vault.py::_read_pdf_text` fingerprinted it a third time — **3 whole-file
  sha256 reads for one snippet**, counted at `_sha256_file`. The persisted
  signature map spares an INDEXED PDF, so the cost landed on exactly the
  unindexed ones the tagging deck reaches. The signature now rides from the
  probe into the read: **3 → 1**, worth ~2 ms at the maintainer's median biblio
  PDF (0.6 MB of 5 943 files) and ~260 ms on the 114.5 MB outlier. A handed-in
  signature is trusted only while a FRESH stat still matches its size and
  `mtime_ns`, so a file changed mid-read falls back to a full hash rather than
  reading or writing a cache under a stale digest. Pinned by
  `test_tagging.py::TestSnippetHashesOnce`, red against the pre-change code
  with `AssertionError: 3 != 1 : 3 whole-file hashes`.
  The finding's other half — moving `.get()`-only readers to
  `load_config_readonly()` — was measured and declined: 0.044 → 0.0012 ms per
  call against a proxy that raises on any mutation (see CLAUDE.md).

* **The audit routes re-read and re-serialised per request.** Two of the
  finding's four halves were real and are fixed; the other two were measured
  and declined (recorded in CLAUDE.md's deferral subsection).
  * `GET /api/audit/inventory` re-flattened every `Record` on every request:
    **75.3 ms of serialisation plus 10.9 ms of `json.dumps` for a 3.71 MB
    payload**, measured on a synthetic 6 000-record inventory (the
    maintainer's library is ~6 150). It is now memoised on the IDENTITY of the
    `(Inventory, Settings)` objects the manager hands out — the search-corpus
    discipline, released by the **same** result listener so a superseded
    Inventory is never pinned: **75.3 → 0.0 ms** on every request after the
    first.
  * `_drift_synonyms` re-read and re-parsed `_tags.md` on every call — three
    times per drift report and once per cross-report search KEYSTROKE. On the
    maintainer's 105 620-byte file (339 synonym entries) that is **1.47 ms per
    call → 0.002 ms** through a memo keyed on the file's own
    `(path, size, mtime_ns)`. An edit still applies on the very next call with
    no rescan, because mtime is the key, and every caller gets a fresh dict so
    none can reach the cached value.
  * Declined on measurement: the accession panel's two cache-file loads
    (**6.4 ms**, not the "two 100 000-entry memos" the plan assumed — the files
    hold 5 561 and 575 entries) and the DOI cache's twice-per-scan load and
    save (**~36 ms of a multi-minute scan**).

  Pinned by `test_audit.py::TestInventoryRowsMemo` and `::TestDriftSynonymMemo`,
  red against the pre-change code with `2 serialisations` and `4 tag-file
  parses`.

* **Bib relevance re-folded 6 000 entries per deck section.** The `.bib` parse
  is cached on `(path, size, mtime_ns)`, but the accent-folded haystacks
  `relevant_bib_keys` scores against were not part of the cached value, so
  every section re-did `2 x len(bib_index)` pure-Python NFD passes. Measured on
  a synthetic 6 000-entry index (the maintainer's `_master.bib` is 6 141):
  **73.0 ms per section, 37.7 ms of it the fold pass alone**. `_fold_cached` is
  now an LRU-bounded memo (`_FOLD_CACHE_MAX` 32 768) over the same pure
  function, so every section after the first folds nothing: **73.0 → 35.3 ms
  warm**, the first section unchanged. The query text is deliberately folded
  through the uncached function — it differs on every call, so memoising it
  would only fill the LRU. Scoring, ordering and the returned key list are
  byte-identical, and the folding recipe itself is untouched (still
  byte-identical to `audit/search.py::fold`). Pinned by
  `deckgen/tests/test_template.py::TestBibFoldMemo`, red against the pre-change
  code with `AssertionError: entry folds on the second section: 400`.

* **Few-shot exemplar selection embedded every key the journal ever touched.**
  `tagging/candidates.py::_select_exemplars` mined the whole tagging journal
  and embedded every candidate title on EVERY propose, under the 30 s
  construction bound the single-flight propose gate is held across — so in the
  field it timed out and degraded to the recency fallback after spending the
  entire budget to reach the answer the fallback gives for free. Measured with
  a counting fake embedder over a journal holding 3 000 tagged keys: **3 000
  titles embedded per proposal**. The pool is now truncated to the newest
  `_EXEMPLAR_MAX_CANDIDATES` (200) by op timestamp **before anything is
  embedded**, and each title's vector is cached on disk by (embed model,
  title) exactly as gloss vectors are: **200 on the first proposal, 0 on the
  next**. The cap applies to the ranking POOL, never to the `k` exemplars the
  prompt receives, and both the similarity sort and the recency fallback run
  over the same truncated pool. Degradation stays total — every new failure
  mode still raises into `propose_context`'s single `try/except`, which
  returns `(vocab, [])`. Pinned by `test_tagging.py::TestCandidateSelection`'s
  four new cases, red against the pre-change code with `AssertionError: 3000
  != 200`.

* **The agent loop re-sent its whole tool history on every iteration.** Tool
  results are kept at their per-tool caps (`vault_search` 12 000,
  `vault_list_materials` 20 000, `vault_read_note` 32 512 characters) and every
  completed `ToolTurn` was passed verbatim to every subsequent `generate()`, so
  prompt characters grew **quadratically in iteration count** — and a deck runs
  one such turn per section. Measured on a scripted 6-iteration turn whose tool
  returns 30 000 characters, the six requests carried
  `0 + 30 246 + 60 492 + 90 738 + 120 984 + 151 230 = 453 690` history
  characters, the last request 151 230 on its own. `core/agent/loop.py`'s new
  `_budget_tool_history` now renders the history the REQUEST carries: the
  newest turns verbatim while they fit `_HISTORY_CHAR_BUDGET` (70 000, sized
  above two whole `vault_read_note` turns) and never fewer than
  `_HISTORY_KEEP_VERBATIM` (2), with every older turn's result content replaced
  by a one-line stub naming how much was elided. Same turn after:
  `0 + 30 246 + 60 492 + 60 617 + 60 742 + 60 867 = 272 964`, the last request
  **60 867 — bounded, not growing**. Under the budget the function returns the
  input object, so a short turn's request is byte-identical to before it
  existed. **Every `tool_use` keeps its paired `tool_result`** (Anthropic 400s
  otherwise) with its `tool_call_id` and `is_error`, the `ToolCall` objects —
  Gemini `thought_signature` included — are carried over by identity, and only
  WHOLE turns, oldest-first, are ever stubbed; the loop's own `tool_history`
  list is untouched, so the `on_event` trace and the SSE frames are unchanged.
  Pinned by `test_agent.py::TestToolHistoryBudget`, red against the pre-change
  code with `AssertionError: 151230 not less than or equal to 150000`.

## 2026-09-02 — Performance plan, Phase 2: lock scopes narrowed to their rationale

The 120-second retrieval acquire is argued in the docs for checkpoints; it had
come to cover archive pruning, legacy cache migration and LanceDB compaction as
well. Each item here returns one lock to what its own comment says it protects.

* **Archive pruning ran inside the rw WRITE lock.** On any fresh rebuild
  `_archive_old_index_dir` did the rename and then, still holding the write
  lock, ran a bounded 5 s size walk and an **unbounded `rmtree`** of stale
  archives — the retention windows are 2 routine + 5 protected, and the
  maintainer's index is 5.4 GB, so tens of GB of deletes is in range. Every
  chat read is a writer-preference reader on that lock and waits with **no
  timeout**, so a rebuild could stall retrieval for the whole sweep. The
  rename stays under the lock — it is the part that must be atomic with the
  decision to rebuild — and the walk plus the prune move to one short
  `index-archive-sweep` daemon holding no lock at all. Measured on a
  3000-file stand-in archive: **117 ms** of walk + `rmtree` leaves the write
  lock (10 ms walk, 107 ms delete), and that scales with archive size and
  count while the lock hold no longer does. Safe because neither step touches
  the new index dir: the walk reads the renamed-aside archive, and the prune
  removes only `<index>.bak.*` siblings outside the retention windows — the
  archive just created is the newest by mtime, so a prune racing a later
  archive can never take either one. Every removal still routes through
  `log_storage_deletion` (`_prune_old_index_backups` is called unchanged) and
  both windows stay bounded and independent. The user-visible size line still
  arrives on the same `_emit` ring, a moment later. The daemon is joined by
  `suite_helpers.join_post_index_daemons`, which now waits for it as well —
  an unjoined daemon writing into a patched temp dir is the 2026-07-31 flake
  class. Pinned by
  `test_vault_regressions.py::TestArchivePruneOffTheWriteLock`, proven red
  against the pre-change code with `AssertionError: 3.002642417000061 not less
  than 1.0 : the rw write lock was held for 3.0s of sweep`.

  **The legacy cache migration named in the same finding was measured and left
  alone** — see the deferral registry.

* **The three longest single windows were unheartbeated and silent.** LanceDB
  compaction ran under `_index_mutation_lock` with no bound, no heartbeat and
  no status line — and the 120-second retrieval-acquire rationale is argued in
  the docs for *checkpoints*, not for this — while the store load and the
  checkpoint persist were each one unheartbeated window against the op-lock's
  3600 s TTL. A stall read as a hang, and a lapsed TTL went unnoticed, so the
  run kept writing as a second concurrent writer — exactly what
  `_heartbeat_or_stop` exists to prevent. All four sites (store load, mid-run
  checkpoint, final persist, compaction) now emit a stage line before and call
  `_heartbeat_or_stop` after. Compaction stays best-effort and otherwise
  untouched; a `False` heartbeat still sets the stop event rather than being
  discarded, because `_heartbeat_or_stop` is the shared helper that does
  exactly that; and `op_epoch == 0` — every direct and test caller — leaves all
  of it inert. `_PersistTarget` gained the `op_epoch` field, which is precisely
  the shared-locals bundle it exists to be. Pinned by
  `test_vault_regressions.py::TestLongPhasesHeartbeat`, proven red on all four
  subtests (`'Loading the existing index from disk' not found in …` and the
  three siblings).

  Incidentally, `rag/build.py`'s mypy findings dropped **69 → 47**: a
  `TYPE_CHECKING` declaration of the two host-class members the mixin reaches
  through `self` removes a whole class of "IndexBuildMixin has no attribute"
  noise that would have hidden a real typo among it. The baseline is lowered in
  the same change, per the ratchet's rule; the tree now measures **152/153**.

* **The two restore peeks parsed the whole journal.** `_restore_batch`'s "does
  this batch id exist?" pre-check and the single-op restore's "what kind is
  this op?" each called `journal.load()`, parsing **every** op. Measured on a
  journal of 11 651 ops (the live count): single-op peek **63 ms → 0.38 ms**,
  batch peek **60 ms → 1.47 ms**, and ~11 651 dict allocations per peek — twice
  per restore — gone. `operations` already carried a UNIQUE index on `op_id`
  and `operations_batch_idx` on `(batch_id, seq)`, so neither new query scans.
  **The dirty-tracking contract is untouched, structurally:** `peek_op` and
  `peek_batch_ops` return PLAIN dicts, exactly as `load_page` does, never a
  `_JournalManifest` — so it is impossible to hand a partial history to
  `save()`, whose incremental-write contract depends on the baseline id set
  covering the whole history. Both call sites already re-`load()` under the
  op-lock for the actual write; the peek only decides a 404 and which Zotero
  read to do first. Like `load`, `stats` and `load_page`, these are pure
  readers with no schema creation on the path — a `CREATE TABLE` there takes a
  RESERVED lock and turns an ordinary read into `SQLITE_BUSY` against a
  concurrent `BEGIN IMMEDIATE`, defeating WAL. **The partial-load rewrite of
  `load()` itself is still NOT taken** — it is the recorded deferral, and it
  changes exactly the invariants that lost writes twice in August. Pinned by
  `test_tagging.py::TestScopedJournalPeeks`, which counts parses at
  `json.loads` (a level both versions reach), proven red with
  `AssertionError: 40 not less than or equal to 2 : the peek parsed 40 of 40 ops`.

* **A whole-bib parse and a whole-vault walk ran inside the op-lock, per
  request.** Two halves of one plan finding, both "cheap in isolation, and each
  sitting inside a lock that indexing waits on". (a) `POST /api/audit/mapping`
  re-parsed the entire bibliography on **every triage click**, only to test one
  citation key for membership: measured **3900 ms** on the maintainer's
  6141-entry / 7.2 MB `_master.bib`. `audit/core/bib.py::parse_bib_keys` now
  memoises a `frozenset` of keys on the file's `(size, mtime_ns)` —
  **3900 ms → 0.001 ms** on a hit — and returns an immutable set rather than
  the parsed entry list, so no caller can mutate shared state; `parse_bib`
  itself is untouched, so the scan (which needs the entries) is unchanged. The
  live-bib rejection guarantee stands because mtime is the key: an entry
  removed from the bib is rejected on the next click, with no rescan. (b)
  `refactor/apply.py` and `refactor/archive.py` called the *uncached*
  `build_name_index` — a fresh whole-vault `rglob` — under the obsidian
  op-lock, moments after `build_plan` had cached exactly that index. Both now
  use `get_file_index`, the cached accessor the plan and the single-note
  re-analyze route already share, dropped by `invalidate_index_cache` after any
  write that changes the file set: measured on a 9120-entry vault,
  **185 ms → 0.05 ms**. Pinned by `test_audit.py::TestBibKeyMemo` (which counts
  parses through the ROUTE, at `parse_bib` — a level both versions reach) and
  `test_refactor.py::TestWritersUseTheCachedFileIndex`, proven red with
  `AssertionError: 5 != 1 : 5 whole-bib parses` and
  `AssertionError: 'build_name_index(' unexpectedly found in ... refactor.apply
  still does an uncached whole-vault rglob inside the op-lock`.

* **The LaTeX lease had no TTL, so a wedged worker kept it until restart.**
  Release fired only when the worker exited, and with `local_request_timeout_s`
  defaulting to `0` a worker wedged in `client.chat()` never returns — one
  lease **and one of the two global slots** were consumed for the life of the
  process, every later deck/docgen op on that path answering 409.
  `RagOperationLock` exists precisely because a zombie must not keep a lock
  forever, and the lease now uses its model: an epoch bumped on every acquire,
  passive expiry at `_LATEX_LEASE_TTL_S`, and a heartbeat that pushes the
  deadline out for anyone still alive. A stolen lease's later `release` and
  `heartbeat` no-op, so a zombie can neither free nor extend a lock a newer
  holder owns. **The stealer INHERITS the victim's global slot** rather than
  taking a second one — otherwise two steals would exhaust the semaphore
  permanently, which is the same leak one level down. **1800 s is measured
  against the longest honest SILENT stage, not the longest run:** the widest
  stall window in the file is compile-fix's (2 × `deck_compile_timeout_s`,
  ≤ 1230 s), and every SSE worker emits an `{"info"}` frame per stage. The
  zombie-writer rule is preserved through the mechanism already there rather
  than by touching every write site: `run_sse_worker` gained an optional
  `heartbeat=` called on each frame, and a refused beat **sets the worker's
  `cancel` event**, so every existing `if cancel.is_set(): return` gate in
  front of a write covers the steal case — plus a visible error frame, so the
  loss is never silent. Pinned by
  `tests/test_latex_lock_registry_phase3g.py::TestLeaseTtlSteal` and
  `::TestSseHeartbeatStopsAStolenWorker`. **Honest note on the red:** the TTL
  is a new mechanism, so a red against the pre-change code is vacuous (it fails
  on an unknown keyword, not on a claim). The tests were instead proven red
  against three deliberately WRONG versions of the mechanism — the technique
  used for the bounded-runner move: dropping slot inheritance gives
  `AssertionError: the steal consumed a second global slot` and `the global
  slots leaked across steals`; dropping the epoch check in `_release` lets the
  zombie free the new holder's lock; and not cancelling on a refused beat gives
  `Lists differ: [0, 1, 2, 3, 4] != [0]` — the worker writing four more times
  after the steal.

* **The materials-manifest fallback parsed the whole docstore on a request
  thread, unserialised.** With `indexed_materials.json` missing, the materials
  route and the agent's `vault_list_materials` tool each parsed
  `docstore.json` — several hundred MB on a real vault — with nothing
  collapsing concurrent callers, and both then wrote the manifest. The
  indexer's own documentation cites exactly this cost as the reason it skips
  the parse at checkpoint time. Measured on a 20 000-document stand-in:
  **561 ms per parse, per caller → 0.013 ms** on a memo hit, and four
  concurrent callers now do **one** parse instead of four. One build lock plus
  a memo keyed on the docstore's `(size, mtime_ns)`; a reindex rewrites the
  file and moves both, so the memo cannot serve a stale answer, and it is
  dropped on a vault switch as well. Holding a lock across the parse is
  deliberate and is the point — the alternative is N concurrent
  several-hundred-MB parses — and it is a leaf, taking no other lock beneath
  it. The recovered manifest is still written atomically in the existing
  shape, by the elected caller, exactly once. Pinned by
  `test_vault_regressions.py::TestManifestFallbackSingleFlight`, which counts
  parses at `json.load` (a level both the pre- and post-change code reach, so
  the red is not merely "this helper is new"), proven red with
  `AssertionError: 5 != 1 : 5 concurrent whole-docstore parses`.

## 2026-09-02 — Performance plan, Phase 1: blocking work off the request thread

Every item in this phase parks a Waitress worker for seconds to minutes with no
deadline, no cancel and no busy signal. The fix shape is the one the Note
Refactor tab already uses: a daemon with a deadline, per-action single-flight,
504 on timeout, 429 when busy.

* **The bounded-action runner is promoted into `api/bounded.py`.** A move +
  re-export out of `api/routes/refactor.py`, so the two upload routes and
  `extract-image` can share one implementation instead of each growing a
  near-identical single-flight registry that drifts. `refactor.py` keeps
  `_llm_action_deadline_s` and a thin binding, and re-exports the registry
  names as the SAME objects — all 152 existing refactor tests pass untouched.
  Two decisions are load-bearing and are ratcheted rather than commented: the
  deadline is a *parameter* (the right bound is action-kind-specific, and
  resolving it at the call site is what keeps the route's function a live
  `mock.patch.object` target — a from-import in the helper would silently
  ignore every such patch), and the registry is shared, not copied (a copy
  would give the two modules independent single-flight state, so the bound
  that caps the daemon backlog would stop bounding). Both new ratchets were
  demonstrated failing against a deliberately wrong version of the move.

* **Both upload routes extracted inline on the Waitress request thread.** A
  chat attachment or a paper upload joined the PDF extraction subprocess for up
  to `PDF_EXTRACT_TIMEOUT_S` (600 s), and an image attachment made a vision
  call, each holding one of thirty-two pool slots with no ceiling of its own
  and no way to answer "busy". Both now run the extraction through
  `api.bounded.run_bounded_action`. The split is at the boundary between "needs
  the live request" and "does not": the streamed save stays on the request
  thread (it reads the request stream, and is already bounded by the byte cap)
  and only the extractor — which is handed a temp path and touches no Flask
  state — moves to the daemon. Deadlines sit OUTSIDE each extractor's own bound
  per the timeout chain's outer-must-exceed-inner rule (PDF: 600 s + margin;
  image: `vision_timeout_s` + margin, read per request), so this never kills a
  legal extraction and discards work the user is waiting for. Action names are
  per route AND per kind, so a slow paper upload never makes a chat attachment
  wait, and a slow PDF never blocks a text file. Honest about what it does not
  buy: neither the extraction subprocess nor a vision call is interruptible, so
  a 504 frees the worker but not the work — the same residual recorded for the
  tagging propose gate. Pinned by
  `test_chat_attachment.py::TestExtractionIsBounded` and the new
  `test_paper_upload_bounded.py` (7 of 9 cases red pre-fix).

  Both test files reach `_EXTRACTORS` through the CONSUMER's `__globals__`
  rather than through a fresh `import services.attachment_service`. The obvious
  form passed on the file alone and failed under the full suite: this repo's
  `conftest.py::IsolatedModule.collect()` evicts app-prefixed `sys.modules`
  entries per test file, so the re-imported module is not the one the route's
  `store_attachment` closed over. That is the trap `cap_of`'s docstring already
  states; it caught me anyway.

* **The chat image attachment joined the shared local-model gate.** It is a
  user-triggered LOCAL vision call, but ran outside
  `refactor/local_model.py::LOCAL_MODEL_LOCK`, whose docstring puts the worst
  case at 1 (user-triggered) + 1 (indexer) — the real worst case was 1 + 1 + 1
  against a single local backend, which is the concurrent load the deck's
  per-section retry layer exists to paper over. Recorded in Known Issues as
  accepted, with `_run_llm_action_bounded` named as the fix shape; that is the
  shape taken, and the entry is struck. It waits a bounded 15 s and then
  reports **503 "the local vision model is busy"** rather than queueing
  silently — a distinct condition from the 400 that means the file is the
  problem, and from the 429 that means another upload of the same kind is
  running. Safe to add only because extraction now runs on a daemon, so the
  wait blocks a daemon thread and never a Flask worker — the precondition the
  gate's own docstring states. Pinned by
  `test_chat_attachment.py::TestImageSharesTheLocalModelGate`.

* **Finder tags forked `/usr/bin/xattr` once per read and once per write.**
  Every tag read and every tag write spawned a child process, inside the one
  obsidian op-lock hold a harmonize or a retire keeps for its whole batch, with
  indexing frozen behind it. Both now go through libc's `getxattr`/`setxattr`
  by ctypes — the binding `audit/core/finder_tags.py` has used for the read
  side since the audit shipped, extended with the write side. Measured on
  Darwin 25.6 (arm64): read **1.8 ms → 0.017 ms**, write **1.4 ms → 0.007 ms**,
  so a 2000-key harmonize goes from **~6.4 s and 4000 process spawns to
  ~0.05 s and none**. **The plan's premise was wrong and is corrected here:**
  it costed the spawn at 60–120 ms on the assumption that `/usr/bin/xattr` is a
  Python script, and on this release it is a Mach-O binary, so the real figure
  is 130× rather than the 4–8 minutes claimed — the fork-from-a-multi-GB-RSS
  process cost that the app pays and this micro-benchmark cannot see is what
  keeps it worth doing. `os.setxattr` (which the plan named) does not exist on
  macOS at all — CPython ships the xattr family on Linux only — so ctypes is
  the only fork-free route. What is preserved is the part that makes the write
  safe: a genuinely absent attribute still reads as `[]` (ENOATTR, where the
  subprocess path matched `"No such xattr"` on stderr) and **every other
  failure still raises**, so a caller about to union tags can still tell "no
  tags" from "could not read" and refuse the PDF rather than clobber its whole
  colour set; raw colour-suffixed entries still round-trip verbatim. The
  historical subprocess path survives as a fallback for any platform where the
  binding cannot be made, and its own version of the absent-vs-error contract
  is now pinned by forcing `_LIBC` to `None`. Pinned by
  `test_tagging.py::TestFinderTagXattrNoFork`, proven red against the pre-change
  module with `AssertionError: Finder-tag I/O forked a child process`.

* **Batch restore PATCHed Zotero serially under the obsidian op-lock.** Retire
  and harmonize were refactored into four lock phases by the 2026-07-23 audit
  for exactly this reason; batch restore never was, so its single hold covered
  every revert *including* each Zotero PATCH — and, for a
  `zotero_attachment_trash` op, a GET as well. A 50-op batch against an
  unreachable Zotero froze indexing and every other vault write for
  50 × 15 s ≈ 750 s in one hold. It now runs the same four phases: reads
  lock-free (the tag reads already were; the attachment GET now is too, via a
  new `_revert_zotero_attachment_prepare`/`_commit` split mirroring the tag
  one), ONE hold for the vault-local note/Finder reverts and the save, every
  PATCH lock-free, then a short second hold that **reloads** the manifest and
  records the outcomes. Measured with 10 Zotero ops at 50 ms per round-trip:
  the op-lock is held **564 ms → 8 ms** of a 1.14 s batch, with the same 10/10
  reverted — so the 50-op unreachable-Zotero case goes from ~750 s of frozen
  indexing to ~0.04 s. Safe because the lock never protected the round-trip in
  the first place: `If-Unmodified-Since-Version` does, and it is untouched.
  Phase 4 reloads rather than saving the phase-2 object, so a writer that took
  the lock in the gap is never clobbered, and the state flips travel by op id;
  results are collected per op id and emitted in the original newest-first
  order, so the terminal frame is unchanged for any batch that succeeds. Per-op
  conservatism is preserved exactly — an unreachable Zotero is still an `error`
  that KEEPS the op `applied` and retryable, never a "reverted" claim for a
  PATCH that did not land, and failing to re-acquire the lock in phase 4 leaves
  those ops `applied`, which a re-run resolves by re-reading the store and
  answering `skipped`. Pinned by
  `test_tagging.py::TestRetireTag::test_batch_restore_never_calls_zotero_while_holding_the_op_lock`,
  proven red against the pre-change routes with
  `Lists differ: [('read', False), ('write', True)] != [('read', False), ('write', False)]`.
  (Test trap re-encountered: the first draft drained the SSE response *after*
  the patch scope closed, so phase 3 ran unpatched and hit the real network —
  drain inside the scope.)

* **`GET /api/notesync/status` re-compared every reference note per request.**
  No memo of any kind: each poll re-read and re-tokenised the whole library.
  Measured over 1235 notes (the live count) with a 120-word reading note each:
  **467 ms per request → 3 ms** on a warm memo. **The plan's attribution was
  wrong and is corrected:** it blamed "about 1 200 whole-file reads", and the
  disk is 26 ms of that 467 — the cost is the tokenising comparison. The memo
  is keyed on the cached `Inventory`'s **identity** (the Zotero half of every
  comparison comes from it, so it cannot move while the identity holds) plus,
  per note, the file's `(size, mtime_ns)`, and is dropped by the same
  `register_result_listener` hook the audit search corpus uses, so a replaced
  or cleared scan releases both the memo and its Inventory reference. The
  threshold is part of the key, so a Settings change applies on the next poll.
  Worth stating because `notesync/compare.py` says the opposite in a nearby
  context: mtime is used here purely as the app-wide "has this file changed
  since I read it?" key (`load_config`'s and the deckgen template cache's
  idiom), **never** to answer "has this note drifted?", which that module
  correctly documents as unanswerable from a timestamp in this vault. The
  module lock guards the dict only — every read and comparison runs outside
  it, so two concurrent pollers can duplicate work but never block each other
  on I/O. Pinned by `test_notesync_route.py::TestStatusMemo`, proven red
  against the pre-memo route with
  `Lists differ: ['ok2021', 'ok2022', 'ok2021', 'ok2022'] != ['ok2021', 'ok2022']`.

  **Tag Hygiene's half of the same finding was measured and deliberately not
  changed** — see the deferral registry. Its residual-PDF xattr sweep costs
  **9 ms for 1250 files**, because `audit/core/finder_tags.py` has read through
  ctypes since the audit shipped and never forked. Precomputing it at scan time
  would add an `Inventory` field, a serialisation concern and a lazy fallback
  for 9 ms.

* **The polled status routes probed the local backend uncached and unbounded.**
  `app.js` polls `/api/status` every 15 s and each request ran a live
  `check_running()` against Ollama with the client's **generation** timeout —
  `local_request_timeout_s`, which defaults to `0` = leave the SDK default =
  **unbounded** — so a wedged backend parked one of the 32 Waitress workers per
  poll, per open window, for as long as it stayed wedged. LM Studio's sibling
  probe has been 3 s bounded since it was written; Ollama's was the odd one
  out. `/api/models` had the same unbounded probe, and `/api/health` had a TTL
  but computed *outside* its lock (correctly — a lock must never span a probe),
  so N concurrent misses each probed both backends. Three changes, one shape:
  a hard `PROBE_TIMEOUT_S` (3 s) on the Ollama liveness probe, independent of
  the generation knob (`get_models` deliberately keeps the config timeout by
  default, because `resolve_model` calls it on the generation path where a 3 s
  ceiling would be a new failure mode rather than a bound — `/api/models`
  passes the 3 s explicitly); and one `_TtlSingleFlight` helper behind all
  three routes, which keeps the never-hold-the-lock-across-a-probe property and
  adds single-flight on top, so concurrent misses share one probe instead of
  each running their own. Measured against a backend answering in 250 ms:
  four sequential polls **1024 ms → 263 ms**, four concurrent polls **4 probes
  → 1**. Two deliberate exclusions: provider **warnings** stay outside the
  cache (they accumulate in-process, cost nothing to read, and a newly raised
  one must not wait out the TTL — pinned), and a probe that RAISES caches
  nothing and leaves no wedged in-flight marker. `_health_cache` was replaced
  by the helper rather than mirrored into; it had exactly one non-route reader,
  the test fixture. Pinned by `test_status_routes.py`, proven red against the
  pre-change routes with `AssertionError: the 15 s badge poll re-probed the
  backend (assert 5 == 1)` and `AssertionError: 4 concurrent probes,
  expected 1`.

* **`POST /api/pull` downloaded to a host nothing else uses.** It called
  `ollama.pull` on the module-level client, which resolves `OLLAMA_HOST`
  independently of the app's resolved host — the same split-brain the status
  probe was fixed for, where a pull lands on one server while generation and
  embedding use another, so a model appears to download and then "was not
  installed". It now goes through the app's own `resolve_ollama_host()` and the
  shared `(host, timeout)` client cache. `timeout=None` is deliberate and not
  an oversight: a pull legitimately streams for many minutes and the SDK's read
  timeout applies per chunk. Pinned by
  `test_status_routes.py::TestPullUsesTheResolvedHost`.

* **A LaTeX compile was uninterruptible for a whole pass, and its total budget
  was installed as the SSE per-frame stall window.** `compile_latex` blocked in
  `proc.communicate(timeout=…)`, so there was no path from the user's Stop to
  the process-group kill: Cancel was **invisible for up to
  `deck_compile_timeout_s` (max 600 s) per pass**, with the LaTeX lease and one
  of its two global slots held throughout, and a compile-fix run makes up to
  `2*iters+1` passes. The maintenance `latex` step has polled since it was
  written; this one did not. It now uses that step's `run_command` shape — 200 ms
  `proc.wait` slices, `should_cancel()` polled each time, the **same** TERM →
  5 s → KILL escalation on either exit, so "never kill latexmk alone" still
  holds. A caller passing no `should_cancel` is byte-identical to before. One
  non-obvious consequence: stdout/stderr became temporary **files** rather than
  pipes, because polling a live process while holding unread pipes deadlocks
  once the OS buffer fills — which a failing LaTeX run reliably manages (pinned
  with a ~2 MB-of-stdout stand-in). Separately, `consumer_timeout_s` for
  compile-fix was `(2*max_repairs+1) * timeout_s` — up to **4 200 s** at the
  knob ceilings — and that is the window the SSE consumer waits for **one**
  frame, so a worker wedged before its first frame sat silent for over an hour.
  It is now the longest honest silent stage (one compile plus one repair, since
  a repair producing nothing is followed straight by the next compile), and the
  total budget moved into the worker, where a total budget belongs; it stops
  only after a **verified** compile, never mid-write. Both changes are mirrored
  in `api/routes/docgen.py`. Pinned by `test_compile.py::TestCompileCancel` and
  `::TestCompileFixBudgets`, proven red against the pre-change code with
  `assert 'cancelled' in 'compilation timed out after 30 seconds.'` (Stop took
  the full 30 s to reach latexmk) and `AssertionError: the per-frame stall
  window is the whole run's budget (930s) — assert 930 < 900`.

## 2026-09-02 — Performance plan, Phase 0: leaks that need a restart to clear

Six subsystem sweeps against `aeca4fc` found 29 items; Phase 0 is the five that
leave the app in a state only a restart clears. Each fix here is proven RED
against the pre-fix code before it lands, carries the structured `PERF` comment
block at the change site, and strikes or adds its Known Issues entry in the
same commit.

* **The Zotero de-link gate leaked on a client disconnect.** `POST
  /api/zotlink/trash` released `_ZOTLINK_GATE` in its worker's own `finally`,
  but `api/sse.py::run_sse_worker` spawns that worker on the generator's FIRST
  iteration — so a client that received the 200 and went away without draining
  never ran it, and every later de-link answered **429 for the rest of the
  process's life with nothing running**. One abandoned request was enough.
  Release is now handed to the skeleton's `_SSEOpGuard` via `release=`, which
  covers exactly the never-iterated-generator case (`call_on_close`) and still
  guarantees release never fires while a worker that may still PATCH is alive.
  `api/routes/accession.py` had always passed `release=`; this route had not.
  Before: gate held indefinitely after one disconnect. After: free immediately.
  Pinned by `test_zotlink.py::TestTheGateIsNotLeaked::test_a_client_that_disconnects_before_draining_leaves_the_gate_free`,
  which builds the response through `full_dispatch_request` and closes it
  without iterating — the test client's `post()` drains the stream, spawning
  the worker, and so exercises a different path entirely.

* **The vault panel drained its own one-shot status feed twice.**
  `GET /api/obsidian/status` destructively drains `_status_messages`, so a
  message handed to one reader is gone for every other. `pollVaultStatus`
  (2 s) and `pollPrewarmStatus` (1.5 s) both polled it, and the prewarm loop
  kept two fields and discarded the rest — so while prewarm and indexing
  overlap (the normal cold launch, since they compete for the same index load)
  progress lines and any warning raised in that window went to the prewarm
  loop and were rendered nowhere. Measured over a six-message window against
  the pre-fix code: **9 GETs where 4 suffice, and 3 of 5 non-empty batches
  silently discarded**; after, 4 GETs and 5 of 5. While a run is in flight the
  vault poller is the single reader and paints the prewarm banner from its own
  payload — only when a prewarm loop is actually alive, so a tick can never
  re-enable Send for a panel no prewarm ever gated — and the prewarm loop
  suspends, pushing its 30-minute deadline out while suspended because prewarm
  state is still being observed through the other loop.
  `refreshIndexState`'s one-shot GET renders the latest drained line for the
  same reason. The SwiftBar and Übersicht widgets carry this as a hard
  prohibition; the app's own panel had been breaking it against itself.
  Pinned by `tests/js/vaultStatusDrain.test.js` (3 of its 4 cases red against
  the pre-fix code; the fourth is a preservation assertion).

* **The BM25 warm was kicked before the terminal state was published.** The
  post-index warm thread ends in `_persist_bm25_sidecar`, which bails unless
  `_index_state` is `idle` or `done` — correctly, since a mid-run persist
  would churn a full-corpus write at every checkpoint. The kick ran one line
  *before* `_index_report_outcome` flipped the state: measured at the kick,
  the state was `embedding`, so on a fast run the daemon rebuilt the retriever
  and wrote no sidecar at all, and the next cold launch paid the full docstore
  re-tokenise the sidecar exists to avoid. It failed silently, and
  intermittently — whichever side of the flip the daemon happened to land on.
  The two tail calls are now in the other order. The `_stop_event` gate, not
  the state, is what keeps a cancelled or paused run from churning the build,
  and it is untouched (a cancelled run lands on `idle`, which is itself inside
  the persist allowlist). Pinned by
  `test_vault_regressions.py::TestVaultChatBM25Manager::test_bm25_warm_is_kicked_after_the_terminal_state_is_published`,
  which asserts at the kick rather than through the daemon: the defect is a
  race, and a thread assertion would only reproduce it sometimes — which is
  exactly how it stayed invisible.

* **A cancel could silently revert the run's final index metadata.**
  `cancel_indexing` read, edited and rewrote `obsidian_meta.json` under
  `_status_lock`, while the indexing thread's `_write_index_meta` held only the
  rw write lock — two writers with no shared serialisation. A cancel that read
  the file before the final persist and wrote it back after reverted `phase`,
  `has_vector_data` and `embed_provider`. The last is the expensive one: the
  incremental gate REFUSES a run whose recorded local backend differs from the
  current one and adopts silently when the field is empty, so reverting it to
  `""` turns that refusal into an unnoticed adoption and lets a genuinely
  switched backend mix two vector spaces in one index. It was also file I/O
  with an fsync inside the very lock the 2026-07-22 P5 audit removed I/O from.
  Both writers now take a dedicated `_meta_write_lock`, and the cancel's edit
  runs *before* the in-memory flip rather than beside it: that ordering is what
  preserves the original rationale ("a concurrent `get_status()` must never see
  in-memory idle while on-disk meta still says partial") without holding two
  locks at once — in the new window the disk already says `partial: false`
  while the state is still non-idle, and `get_status` returns early there, so
  lazy recovery never runs. Unknown meta keys still survive a cancel. Pinned by
  `test_vault_regressions.py::TestCancelDoesNotClobberFinalMeta`, which drives
  the interleave deterministically by landing the indexer's write in the gap
  between the cancel's read and its write-back.

* **Three PDF preview blobs outlived the iframes that showed them.** `deck.js`
  and `docgen.js` revoked the previous preview's object URL only at the moment
  the *next* one landed, so every path that clears the host without producing a
  new blob — a compile that fails, a PDF fetch that errors, Cancel — removed
  the `<iframe>` referencing it and left a multi-megabyte blob alive with
  nothing pointing at it for the rest of the process; repeat a failing compile
  and you leak one PDF per attempt. `tagging.js`'s `V` panel went the same way
  on a deck→table switch: `_renderView` re-renders the whole body, destroying
  the panel, but `_deckClosePdf` (which owns the revoke) was wired only to the
  `V` toggle, the card change and `window.taggingTeardown`. Both now release at
  the point the referencing element is destroyed. The plan's fix shape also
  named a tab switch; that one is deliberately NOT taken — `showTab` only drops
  the `.active` class, so the iframe stays in the DOM and the blob backs a
  preview the user can return to. Recorded in Known Issues with the reason.
  Pinned by `tests/js/previewBlobTeardown.test.js` (six cases, all red pre-fix).

## 2026-08-30 — Audit remediation: six batches over the 2026-08-29 lot

A full audit of the previous ten commits found seventeen defects, none visible
to the gates (2,915 python / 740 JS / ruff clean / mypy ratchet holding, all
green throughout). Three were P0. Every fix here was proven RED against the
pre-fix code before it landed; the counts below are net new tests.

**Batch A — three P0s** (`e3ecfff`)

* **The Zotero-region overwrite gate was blind to digits and markup.**
  `fold_words`' `[^\W\d_]+` drops digits entirely AND strips them from inside
  a token, so `"dose 2019 p. 47 750mg"` reduced to `['dose','p','mg']`.
  `extra_words` — the count whose whole job is "is there something of yours to
  lose?" — could not see a year, a dose, a page number, a p-value or a sample
  size the user had corrected, nor bold, bullets or a `[[wikilink]]` they had
  added. Such a note classified as a SAFE update, the UI offered it as a
  one-click refresh, and the write deleted them. Fixed by measurement over the
  live library (1,234 notes, read-only): `risk_words` includes digits and
  `structure_markers` covers markup, asymmetrically — structure counts for LOSS
  and never for IMPORT, because counting it symmetrically invented five phantom
  "something to import" rows out of pure formatting. The actionable set is
  unchanged at 16 rows; 555 notes carrying markup a regeneration would flatten
  move from "in step" to "annotated", which offers no verb.
* **`split_note`'s byte-exactness invariant was false** when the `---` rule is
  the last line with no trailing newline — the prefix gained a fabricated `\n`,
  so a region write perturbed a byte outside the region while the sha256 guard
  certified it. Its test's four fixtures all ended with a newline.
  `_body_offset` also matched `\n----` and `\n---foo` and could land mid-line,
  and returned 0 for an unterminated frontmatter block — making the OPENING
  fence look like the body's rule, so an update could overwrite the tagging
  subsystem's tags. Now line-anchored, and such a note is refused.
* **Sorting an audit table stranded the open diff panel.**
  `installTableControls` snapshots its rows at install time; sort
  `appendChild`-moves every data row to the end of the tbody, leaving an
  injected panel behind to float to the TOP — detached from its own note and
  sitting under an unrelated row whose `[update]` button it appeared to
  describe. Filtering never hid it either. Rows marked `data-row-attachment`
  now travel with their parent.

**Batch B — the Zotero de-link route** (`4f73875`)

`POST /api/zotlink/trash` ran up to 200 sequential PATCHes INSIDE the obsidian
op-lock, synchronously, with no heartbeat: every vault write frozen for the
batch, a lapsed TTL still appending to the manifest, and a browser that gave up
long before the server did. That is the pattern the 2026-07-23 audit removed
from retire-tag. Now SSE with the same four phases, `_BatchJournalKeeper`
hoisted to `api/routes/_batch_journal.py` rather than copied a third time, and
`zotero_attachment_commit_locked` split into journal + patch halves like its
tag-removal twin. Also: a gate leak introduced mid-conversion and pinned;
duplicate keys inflating the annotation warning; `str.isalnum()` accepting
`'٣'`; the redundant blueprint origin guard removed; and the client — which had
NO tests and still parsed `r.json()` on an event stream — converted and
ratcheted.

**Batch C — Plain Chat attachments** (`5b23fc2`)

The Attach button was never actually held during an upload (`_setSending` read
the raw argument for it and folded `_uploading` in only for Send), so a second
click opened a concurrent extraction — which `_uploadOne`'s docstring says is
impossible. A long PDF was extracted in FULL and then truncated, discarding
~95% of the work; now page-bounded from the character cap, with the clipping
disclosed explicitly because a sparse document can fall under the cap while
still being cut short. One expired attachment re-sent across a conversation was
announced as three. `wrap_untrusted`'s new `source=` kwarg took a user-supplied
filename straight into the trusted preamble, unsanitised, three lines above the
guard that exists for exactly that. Per-message DB round-trips collapsed to one
query, plus a `created_at` index. `.jpeg` restored to the accepted-types hint.

**Batch D — the shared diff** (`9f29e16`)

The alignment budget bounded CPU and nothing else: the degraded path still
expanded into thousands of rows, so a long reworded note built ~20k elements
synchronously. Capped at 1,500 rendered blocks — AFTER folding, which the
existing "does not degrade a big note whose edits are few" test caught on the
first attempt. Word-level changes were signalled by colour ALONE (the -/+ signs
cover the line, not the words), so removed words are struck and added ones
underlined. The stacked layout kept two column headings above one interleaved
column, taking the load-bearing counts with them; a single caption replaces
them there. The fold is a toggle rather than a one-way reveal.

**Batch E — accessibility and UX** (`69cf946`)

An unhandled file drop navigates the PyWebView window to the file and replaces
the app with a PDF viewer; only the chat panel was guarded, so a drop that
missed it destroyed the session. `installGlobalDropGuard` is document-level and
last-resort. The attachment strip was a live region full of buttons
(double-announcing every attach) and its wipe-and-rebuild ate keyboard focus.
The diff and preview panels had no in-flight guard, so two clicks inserted two
un-closable panels, and neither announced anything. Two 409s instructed end
users to "re-send with `accept_overwrite: true`". The chip's × was ~8×12 px.

**Batch F — the record** (this commit)

Three comments asserted properties the code did not have. Two are now true
rather than merely corrected: `_noteSyncUpdate` fetches its preview BEFORE the
confirm (it claimed the confirm text stated the real cost while composing it
from a flag stamped at last render, so the user could approve "nothing you
wrote is at risk" and then get a 409), and `_uploadOne`'s "sequential by
caller" holds now that Batch C fixed the guard. The third quoted a CSP hash
count that had drifted 111 → 114 and now points at the measurement instead.

This file tracks the current refactor line. Older entries were removed because they described deleted integrations and stale endpoints.

## 2026-08-29 — Review of the day's two features

An adversarial review of the attachments and side-by-side-diff commits. Five
fixes; the two behavioural ones were each reachable from the shipped UI.

### Plain Chat refused nothing while a file was still being read

The Send button is disabled during an extraction, but `#plainchat-input`'s
`onkeydown` calls `chatPlain()` directly, so the button was never the guard.
Attach a large PDF, type the question while the chip still reads "reading…",
press Enter: the turn went out carrying the CLIENT-invented
`pending-<ts>-<rand>` placeholder id, and three wrong outputs followed — the
transcript claimed the file rode the turn, the server found no row and
announced "1 earlier attachment(s) are no longer available … kept for 24 hours"
(a false explanation for a file that was never uploaded, repeated on every
later turn because the bogus id was now permanently in `_history`), and when
the upload finally landed its attachment was pushed into the composer for the
NEXT turn, so an unrelated later question silently carried the PDF.
`chatPlain` now refuses while an upload is in flight and announces, mirroring
`newChat()`. The existing test asserted `sendBtn.disabled`, which was true
throughout the defect.

### The same-role merge dropped the newest attachment

`_validate_messages` merges consecutive same-role turns for strict-alternation
providers, then re-capped the merged ids with
`[:CHAT_ATTACHMENT_MAX_PER_MESSAGE]`. That slice takes from the END, so a
4-attachment turn merged with a 1-attachment one silently dropped the newest
file — the exact failure the comment above it claimed to prevent. Adjacent user
turns are a designed-for path (an empty-but-clean stream records the user turn
with no assistant turn and does not clear the composer), so the client reaches
it. The cap is no longer re-applied: **it bounds what a user may attach to ONE
submitted message and is enforced there by `_validate_attachment_ids`**;
re-applying it after a merge this function itself performed can only destroy
something the user did supply. `CHAT_ATTACHMENT_REQUEST_MAX_CHARS` is the bound
that protects the provider, and it is untouched. The existing test used 1 + 1
ids, so the slice never fired and it passed over exactly the broken case.

### Honest refusals, and the budget refused before it is paid for

* A short but perfectly readable `.md`/`.txt` was refused with "No readable text
  found in that file. A scanned PDF with no OCR layer is the usual cause" — for
  a nine-character note the app read without difficulty. The
  `_MIN_USEFUL_CHARS` floor exists because a two-word result **from an
  extractor** means the extractor failed; a text file is read directly, so a
  short one is simply short. The floor now applies to `pdf`/`image` only, and a
  genuinely empty file gets its own message.
* `CHAT_ATTACHMENT_MAX_PER_MESSAGE` (4) and `CHAT_ATTACHMENT_REQUEST_MAX_CHARS`
  (240 000) **do not compose** with the 120 000-character per-file default: two
  files at the cap already exceed the request budget, so "four files" is
  reachable only for small ones. The user paid for up to four extractions and
  learned at SEND time. The composer now refuses when the conversation is
  already at the budget (nothing extracted, nothing paid for) and — because a
  file's length is unknowable before extraction — flags the one that tips the
  balance immediately after it lands, while the chip is still in the composer
  and removing it is one click.
* The server's refusal said "Remove an attachment, or start a new chat". The
  budget sums across the whole 20-turn window, so the offending file is often on
  an earlier turn where "remove an attachment" cannot reach it. It now says so.

### The upload envelope was wired but untested

`register_request_limits(extra_upload_limits=…)` — the `api/security.py`
parameter, its below-the-default validation, and the `app.py` call site — was
exercised by no test, because every route-level upload test uses a file of a few
dozen bytes. Deleting the whole block from `create_app` left both suites green
while in production every attachment over the 2 MiB default envelope would 413:
essentially every real PDF. Four tests, all red-proven against the removed
wiring — a ~3.2 MB attachment is accepted; the raise is scoped to that path AND
that method (a sibling POST does not inherit it, the DELETE under the same
prefix stays small); an extra limit below the default is refused; the small
default is still installed as `MAX_CONTENT_LENGTH`.

### The side-by-side diff duplicated every unchanged line when stacked

Below the 900 px breakpoint the grid collapses to one column, so a row's two
cells sit one above the other. An `equal` row holds the SAME text on both
sides, so every unchanged line was rendered twice in succession and the diff
read as though the note contained duplicated lines — with the default three
lines of context around each change, roughly doubling the context noise on
exactly the surface built for "is there something of mine in here". One column
IS the unified reading, so the `b` side of an `equal` row is now hidden there; a
changed pair still shows both versions, and del/ins rows already hid their empty
side. Also: the line-number track was `3ch` and inherited the cell's
`overflow-wrap: anywhere`, so a number of 1000+ wrapped inside it and made its
row twice as tall (36 px against 18 px). Now `4ch` and `nowrap`. Both new tests
are browser-only — jsdom has no layout engine, no media queries applied to
boxes and no text wrapping — and the existing stacking test inspected only the
`is-chg` row, the one row where two stacked cells are correct and intended.

Gates: 2 915 Python (+8), 740 JS (+5), 15 browser (+2), ruff clean, mypy 174/175.

## 2026-08-29 — Plain Chat attachments

Attach (or drop) up to four `.pdf` / `.png` / `.jpg` / `.md` / `.txt` files onto
a Plain Chat message. New: `services/attachment_service.py`,
`api/routes/chat_attachment.py`, `chat_attachments` in SQLite, and the
`chat_attachment_max_chars` knob.

### Extract to text, not native multimodal

`core/llm/types.py::Message.content` is still a plain `str` and no adapter sends
an image. The alternative was weighed and declined: widening it to content
blocks means per-adapter wire formats (OpenAI `image_url`, Anthropic
`source.base64`, Gemini `inlineData`) — the same territory that produced the
2026-07-02 P0 bugs — and the mocked suite cannot see provider-side validation, so
it needs live smokes and fixtures per provider, while local providers would keep
the text path as a fallback anyway. **The cost is real and is stated in the
README, the guide and Known Issues:** images go through the LOCAL vision model,
so attaching a picture needs Ollama or LM Studio even on an online chat
provider, and the model reads a description rather than seeing the image.

### A sibling route, not a widened `/api/upload`

`/api/upload` feeds `/api/summarise`, which assumes its stored text is "one
PDF's full text". Accepting images and `.txt` there would let a vision
description or a 40-line note reach the paper summariser through a path nobody
designed, and would put Plain Chat's cleanup in the paper flow's id space. The
new route also gets its **own** request envelope (64 MB + overhead) via
`register_request_limits(extra_upload_limits=…)` rather than inheriting the
500 MB paper one — sharing it would mean buffering half a gigabyte for a route
that refuses anything over 64 MB.

### Nothing is silently missing

This is the through-line of the design, because every failure here produces a
confident answer about a document the model never received:

* **Extraction runs at ATTACH time**, not send time. It costs a visible wait and
  buys the user the answer to "did it actually read this?" before they spend a
  turn on it.
* **Refuse by name, never attach empty.** `.svg`/`.heic` have no extraction path
  and are rejected on extension. A file yielding nothing readable is a 400 —
  narrowed the same day to `pdf`/`image` only, since a `.md`/`.txt` is read
  directly and a short one is simply short (see the review entry above). An
  image whose `describe_image` returns `""` — its documented no-raise failure
  sentinel, right for the indexer and wrong here, where the empty result *is*
  the whole answer — is refused with the local-vision cause named.
* **Truncation is disclosed twice**: on the chip, and inside the wrapper the
  model reads. A model told it holds a whole report answers as though it does,
  and "the report does not mention X" is exactly the wrong answer a silent cut
  produces. Per-file cap `chat_attachment_max_chars` defaults to 120 000, sized
  from 400 cached PDF extractions of the maintainer's library (median 43 916,
  p90 108 073, max 3.6 M) — roughly nine papers in ten kept whole.
* **An expired attachment is announced** in the transcript rather than dropped.
  Refusing the whole turn would strand a long conversation the moment a
  24-hour-old file aged out; answering silently is worse.
* **Over the whole-request budget is a 400 with the figures named**, never a
  silent trim — the policy `_validate_messages` already applies to a malformed
  turn.

### Untrusted, and the ids ride the message

Each file gets its own nonce'd `wrap_untrusted` tag, exactly as vault and
web-search content does — one shared wrapper would let the first file's text
close the guard and pass the second off as trusted material. `wrap_untrusted`
grew an additive `source=` keyword for this: telling the model a chat attachment
was "retrieved from the user's vault" is false, and that sentence exists to be
believed; the default keeps every agent call site byte-identical.

`attachment_ids` ride the MESSAGE, not the request, so a document attached on
turn 1 is re-inflated on turn 4 without the client re-uploading or ever holding
extracted text — the server owns what reaches the model.

### Defects found while building, each now pinned

* The internal `attachment_ids` key leaked into `stream_chat_messages` — and so
  toward the provider adapters — on the no-attachment fast path. Caught by the
  existing plainchat contract test, which asserts the exact array the helper
  receives.
* `get_db_connection` yields **tuple** rows; `build_attachment_block` indexed
  them by name. That would have crashed every attachment send.
* The client posted multipart through `secureFetch`, which forces
  `Content-Type: application/json` and suppresses the browser's multipart
  boundary. Python cannot see this; `tests/js/chatAttachments.test.js` can.
* The chip tooltip pointed at a Settings control that did not exist. It exists
  now (**LLM Settings → Plain Chat → Attachment text limit**).
* Merging consecutive same-role turns kept only one side's ids — an invisible
  drop, since the text still merges and the answer still streams.

Every one of those is covered by a test proven to fail without the fix
(five server mutations, five client mutations). Two client failure modes exist
only in the browser and are pinned there: the multipart content-type, and
`dragover` needing `preventDefault` or PyWebView navigates the window to the
dropped file and replaces the app with a PDF viewer.

### One suite hazard recorded, not fixed

`mock.patch.object(svc, …)` lands where the request never reaches, so the test
silently asserts nothing. Test-only (production imports once at boot), but the
failure mode is a green test proving nothing, so it is in Known Issues. The
tests here drive the real config knob instead, which is immune and is the better
test anyway. **The mechanism recorded here on the day — "two distinct module
objects when `tests/test_optional_maintenance_import.py` is collected first" —
was diagnosed later the same day and is only half of it; see the Known Issues
entry, which names both causes and the measurements.**

Gates: 2 907 Python (+28), 735 JS (+22), 13 browser, ruff clean, mypy 174/175.

## 2026-08-29 — Side-by-side diff for the Zotero-note comparison

The `[preview]` on the Zotero Notes Sync report rendered the two regions as two
plain, uncoloured `<pre>` blocks stacked vertically. That is adequate for "is
this note behind?" and useless for the decision the report actually asks the
user to make, which is whether an update would delete writing of their own.
Live matrix at build time: **1 safe update, 15 conflicts** — so fifteen times
out of sixteen, "82 words of yours at risk" was a number with nothing behind it.

Now `ui.js::renderSideBySideDiff` — a shared primitive beside `confirmInline`
and `installTableControls`, used by BOTH that preview and Note Refactor, where
it is a new third view mode.

### Two decisions taken from measurement, not taste

Measured over the 16 actionable region updates in the live library (offline,
reproducing the app's own 15/1 split exactly, so the method is validated):

| opcode | blocks | lines |
|---|---|---|
| `replace` | 52 | **178** |
| `insert` | 9 | 31 |
| `delete` | 4 | 14 |
| `equal` | 53 | 105 |

`replace` dominates, and the similarity of positionally-paired replaced lines is
strongly **bimodal**: 32 pairs at ≥0.9 (the same line lightly reworded), 26 at
≤0.1 (unrelated lines merely adjacent inside one block), **4 in between**.

* **Word-level highlighting inside a changed line** therefore earns its keep:
  line-level-only would paint 178 lines as wholesale changes when half of them
  differ by a few words.
* **But it must be gated**, or the low mode becomes confetti.
  `_DIFF_WORD_MIN_RATIO` = 0.5 sits in the empty middle of that distribution.
  Similarity is weighted by the character length of non-whitespace tokens, so
  re-indenting a line is not read as a rewrite.

### Client-side, which is what made it free

It takes two **texts**, not a server-computed diff. `/api/notesync/update-preview`
already ships `current_region` + `proposed_region`, and the refactor plan frame
already ships the full `original` + `proposed` bodies — so one renderer serves
both callers with **no payload change on either route**. Server-side opcodes
would have meant growing two payloads (one of them the app's heaviest) and
maintaining a second alignment implementation.

### Four invariants, each pinned by a test proven to fail without it

* **Losslessness.** The word pass tokenises a changed line and reassembles it
  from the tokens. A dropped or duplicated character would show the user
  something other than the bytes on disk *while the downstream WYSIWYG sha256
  guard stayed green*, because that guard hashes the server's text and not the
  pixels the user approved. Every DOM test therefore re-derives both full texts
  from the rendered output; six fixtures cover prose, markdown lists, unicode,
  tabs and trailing spaces, whitespace-only edits, and an empty side.
* **Never `innerHTML`** — both sides are vault/Zotero content. The plain branch
  and the word branch are tested **separately**: the obvious single test only
  ever exercises one of them, which a mutation run demonstrated.
* **Colour is never the sole channel** (WCAG 1.4.1): every cell carries a
  visible `−`/`+`, verified in a real browser with the tints ignored. Line
  numbers are `aria-hidden`.
* **Cells wrap, they never scroll sideways.** These are prose notes, not code.
  The host `.audit-table-scroll` has `overflow-x: auto`, so an unwrapped diff
  would be absorbed by the host while `.diff-sxs` (`overflow: hidden`) silently
  **clipped** the right-hand column — which is why the browser test asserts on
  the diff box's own width and not only on the document's. Below 900px the two
  columns stack rather than scroll.

Divs, never a `<table>`: audit.js injects this INTO a report table cell, where a
nested table would be caught by `_installReportTableControls`' `table.audit-table`
sweep. Alignment is LCS with the common prefix/suffix stripped first and a 1e6
cell budget — sized from the live vault (regions run to 175 lines, whole notes to
3,399, p99 678), where the strip means the budget bounds the lines that actually
*differ*. Past it the core is reported as one wholesale replacement with a
visible note, and the word pass is skipped: the pairing inside a degraded block
is positional and therefore arbitrary, so marking "the words that changed" would
assert a relationship the aligner had just failed to establish.

### Note Refactor keeps its unified diff

Side-by-side is a **third** view mode, not a replacement. The unified view hides
unchanged regions, which is the better read for a long note with a few scattered
edits; side-by-side shows everything. Losing it would have been a regression
dressed as consolidation, so `tests/js/sideBySideDiff.test.js` pins that both
survive.

`tests/browser/sideBySideDiff.spec.js` is new and covers the half jsdom
structurally cannot: real text wrapping, the stacking breakpoint, and genuine
column adjacency. The contrast lint gained six pairs, including the **word tint
composited over the line tint over the surface** — a naive single-layer check
would have missed the tighter of the two — for which its `over()` helper now
accepts an already-composited colour. One real regression was caught by the
existing gates during the work: the column label shipped at `0.7rem`, under the
app's 12px type floor.

Gates: 2 879 Python (unchanged), 706 JS (+46), 13 browser (+6), ruff clean,
mypy 174/175.

## 2026-08-28 — Selectable text, Zotero-region note updates, Zotero attachment de-link

Three requested capabilities, plus the measurement defect the second one
uncovered. Gates: 2 834 Python, 660 JS, ruff clean, mypy 174/175.

### The HTML→text fusion bug (found while measuring, fixed first)

`audit/core/zotero.py::_strip_html` was `BeautifulSoup(...).get_text()` with no
separator. That concatenates across `<br>` and adjacent inline elements, fusing
the words on either side — `appendicitis<br>Lebrun` became `appendicitisLebrun`,
and `criteriainclusion`, `evidencecovidnma`, `dépressionutile` are all the same
artifact. It mattered twice:

* `notesync.render.render_note` writes that text into the vault, so every note
  `POST /api/notesync/create` produced carried the fused spellings.
* `notesync.compare` measures word containment against the Obsidian note, and a
  fused word is missing from BOTH vocabularies — permanent drift no edit can
  close. It reported **91** notes as "the Zotero note has words this note
  lacks" where the true figure is **16**.

Now block-aware, and deliberately not a per-inline-sibling split (that would
break `<em>anti</em>depressant` into two words). `notesync/compare.py` had
already recorded one instance of this as an unexplained "tokenisation artifact";
that comment now names the cause. Pinned by `tests/audit/test_zotero_note_text.py`.

### Text selection in the app window

`launch.py` now passes `text_select=True` to `webview.create_window`.
PyWebView defaults it to False and then injects
`body { user-select: none; cursor: default }` into the document, so **nothing in
the app was selectable with the mouse** — the Copy buttons and the MD/TXT
exports were the only ways to get text out, and the caret never appeared. None
of the project's own CSS was responsible.

No automated gate can see this: `tests/browser/` runs Chromium, which never
receives pywebview's injection. `test_launch.py` therefore ratchets the call
site, and the real check is manual in the packaged window.

`ui.js::installLinkInterceptor` gained a companion fix — it now ignores a click
that ends a non-collapsed selection, because a drag-select that begins and ends
inside one anchor fires a `click` on that anchor, which used to open Finder
instead of selecting the sentence.

### Zotero → Obsidian note sync can now update, not only create

The old rule was "create only, never rewrite a drifted note", which was right
under the only measurement then available. It is now scoped to what the user
actually wrote: `compare.py::split_note` splits a note at its single `---` rule
with `prefix + region == raw` exactly, and only the region is rewritten —
frontmatter (and therefore tags), the generated header and the rule survive
byte-for-byte. No sentinel is written or needed: all 1 235 notes have exactly
one rule in the body.

`containment` could not gate this, so `NoteState` gained `missing_words` /
`extra_words` measured over the region. The live matrix over 1 234 comparable
notes: **1 186 nothing to do · 32 annotated by the user · 1 safe update ·
15 conflict**. Hence no bulk verb, and a separate `accept_overwrite: true` on
top of `confirm` for the conflict bucket — "yes, write" and "yes, delete what I
wrote there" are different decisions.

New: `POST /api/notesync/update-preview` and `POST /api/notesync/update`
(stale-diff re-checked under the op-lock, WYSIWYG, overwrite acknowledgement),
journalled as `notesync_update` and reverted by the existing Note Refactor →
Restore. The note is read with `read_bytes()` + explicit decode, never
`read_text()`, whose newline translation would LF-convert a CRLF note.

### Removing a file from a Zotero item without deleting it

`ZoteroItem.attachments` had been populated and read by nothing since it was
written (the "dead Zotero attachments pipeline" note). It now backs a
`zotero_attachments` audit report and a de-link verb in a new
`api/routes/zotlink.py` blueprint. The read gained `linkMode` — which side owns
the bytes — and a per-attachment annotation count.

**Trash, never delete.** `PATCH {"deleted": 1}` is exactly reversible, keeps
the item key (so relations and annotations survive), and for an `imported_*`
attachment leaves the bytes on disk until the user empties Zotero's trash —
which is what makes "without deleting the PDF" true for every link mode.
`DELETE` would destroy all three and a re-create mints a new key, so it is not
an undo; no DELETE verb exists on the path and a source ratchet keeps it that
way. Annotations need their own acknowledgement, since they are trashed with
the attachment.

Journalled in `tagging/journal.py` as `zotero_attachment_trash` and reverted
from the existing Tagging → Restore. Adding the kind exposed a latent trap: the
prune carve-out duplicated `EXTERNAL_OP_KINDS` as a second literal tuple, so a
kind added to one and not the other would have been silently evictable —
destroying the only record of what to restore. The carve-out now reads the
constant.

Removing a Zotero attachment cannot damage any mapping this app keeps: the
PDF↔bib↔Zotero join runs on citation keys and never consults an attachment.
Measured state: the library currently holds **zero** PDF attachments, so the
verb is anticipatory — Zotero's "Add Item by Identifier" re-creates them.

### What the review of the above changed

An adversarial pass over the uncommitted diff, before commit. Four real defects,
two of them silent-data-loss shaped:

* **The region renderer could truncate a note.** It derived the new region by
  splitting the *finished* rendered note, and the Zotero reading note's own text
  is part of that document — so a note containing a markdown `---` rule split in
  the wrong place. Against a custom template with no rule of its own the split
  returned only the text *after* the note's internal rule, and the writer would
  have laid that over the whole region. `render_region` now renders the slot as
  an opaque sentinel, splits that, and substitutes the real text afterwards, so
  the rule is unambiguous whatever the note contains. Zero live notes trigger it
  (measured across all 1,236 reading notes), which is why it was worth fixing
  before it could happen rather than after. Pinned in `notesync/tests/test_region.py`,
  including an assertion that the naive split really would have dropped text.
* **The de-link batch deferred its journal saves to the end**, so a crash after
  N PATCHes would have left N attachments trashed in Zotero with nothing to
  restore them from — the exact hazard journal-before-write exists to close.
  Each op is now persisted before its own PATCH (cheap since the journal became
  SQLite), and `zotero_attachment_commit_locked` raises rather than silently
  skipping a save a caller asked for. Ordering pinned by a test.
* **A completed batch could still return 503** if the final prune/save failed,
  telling the caller nothing had happened after Zotero had already been written
  to. It now reports a `journal_warning` beside the per-key results.
* **Dead client-side code with a docstring that contradicted the code.**
  `_noteSyncDiff` cached hashes "so the subsequent update sends exactly the pair
  the user looked at" — but `_noteSyncUpdate` re-previews and never read them.
  Removed, and the comment now says why re-previewing is the correct behaviour.

Two consistency gaps closed in the same pass: the `zotero_attachments` report
was missing from the cross-report search corpus (a filename absent from a search
that claims to cover every report reads as "Zotero does not have it"), and the
tagging journal's prune carve-out duplicated `EXTERNAL_OP_KINDS` as a second
literal tuple. Also corrected a pre-existing README error the pass surfaced: the
audit's writable surfaces were described as "exactly three" and had been four
since `pdf_wanted.json` landed on 2026-08-26.

## 2026-08-26 (later) — Reviewing the seven batches: six defects the gates could not see

An adversarial review of `17498c7..ecdce9b`, re-deriving every load-bearing
measurement against the live 6 316-record library rather than trusting the
figures the batches were justified by. The gates were green throughout — 2 748
Python, 643 JS, ruff clean, mypy 174/175 — which is the point.

**What the re-derivation confirmed:** 371 orphans with `pdf_text_provider` and
532 without; zero out-of-vocabulary tags across all three writable surfaces
against a 172-tag vocabulary; 1 231 notes ⟺ 1 231 reading notes with 0 missing
and 3 drifted; 11 651 journal ops of which 11 451 are permanently protected,
`load()` at 88–95 ms against `load_page` at 1.4 ms. **What it corrected:**
`tag_harmony` finds 39 rows, not 45; the no-PDF queue holds 1 350 rows, not
1 502; and the provenance page table was computed on the 532-file set — the
exact mistake the batch's own correction was written to prevent, six lines
below it.

**The Restore panel reported an empty history to any upgrading install.**
`stats()` and `load_page()` were written as pure readers and skipped
`_ensure_database`, so a journal still in its pre-SQLite `manifest.json` form
read as zero ops — and because the size header renders nothing at `ops == 0`,
the Compact control vanished with it and `papermind status` dropped its Tag
Journal line. Measured against the maintainer's own 7.5 MB legacy manifest:
11 637 ops reported as 0. A shared `_open_for_read` now migrates on all three
paths while keeping the schema-creation-free fast path that made `load` immune
to SQLITE_BUSY.

**`notesync/create` silently lost the undo for every note past 200.** The
refactor journal caps evictable ops and `create_note` prunes on every call, so
a 300-note batch evicted 100 of its own records while the response returned
their `op_id` and the confirm said "Reversible from Note Refactor → Restore".
`prune` gained `protect_op_ids` (ported from the tagging journal, where the
identical defect was found first), `create_note` gained `prune`/`save` flags so
a batch prunes once and checkpoints every 25, and `_MAX_CREATE` is now
`_MAX_OPS // 2` with a refusal that says why — `_HARMONIZE_MAX_NOTE_OPS`'s
precedent. The `[create all N]` button caps to match and no longer sweeps
drifted rows into a create.

**The provenance memo never once served a hit.** `doi_cache.lookup` coerced any
non-string to `None`, so the `prov` field — a dict — came back `(True, None)`
and the caller's `isinstance(cached, dict)` gate could not pass. Every scan
re-opened all 371 residual PDFs and re-ran the text provider: **11.81 s, now
0.00 s warm**, plus a needless rewrite of a 1.1 MB cache file each time.
Making it work exposed a second problem it had been hiding — the memo key is
`(size, mtime_ns)` and cannot tell a scan that had page-1 text from one that
did not, so a CLI scan would have durably recorded "no source URL" for every
printed web page. Entries now carry `text_seen`.

**`browser_print` was 97% not browser prints.** `quartz pdfcontext` is the
generic macOS producer every Cocoa app's Print → Save as PDF writes; 36 of the
37 rows carried it, 13 of them book-length (up to 845 pages) with no
recoverable URL, each badged "printed web page … add it by URL in Zotero" — a
wrong instruction, not just a wrong label. It is now a weak marker needing
corroboration (a browser in `/Creator`, or a recovered URL). Two things had to
land with it: the bare `tex` publisher marker matched `PDFContext` as a
substring and was masked only by browser_print being tested first (it now
names `itext`/`pdftex`/`dvips` explicitly), and `Skia/PDF … Google Docs
Renderer` is an app export whose specific product name must beat the generic
engine name. **Result: 37 → 8 rows, 7 of them with a URL.**

**`extract_isbn` returned a phone number and discarded the real ISBN.** A
10-digit toll-free number passes the ISBN-10 check about one time in eleven,
and on `meyer_2019.pdf` one sat above the book's real ISBN. Selection is now
label-decisive with a group-shape gate for unlabelled runs — a printed ISBN's
last group is the single check character, which 3-3-4 cannot be. Validated over
all 371 residual PDFs: 45 ISBNs before and after, **none lost**, one repaired,
four switched to the labelled e-ISBN of the same work. The whole candidate
corpus is now a replayable fixture.

**The cross-report search kept returning dismissed rows**, contradicting the
corpus's own "a hit is a row you can go and look at" invariant. Filtering
happens at query time, not in the memoised build.

**And the depth invariant was pinned by a test that could not see it.**
`tag_coverage` computed its union from raw strings while the batch and the
table folded, so the dashboard bucket and `--max-tags N` could disagree over
any case, `#` or NFD variant — and `test_dashboard_buckets_and_batch_depth_agree`
only fed `tag_depth`'s output into `_depth_bucket`, never running the dashboard's
own pass. One `fold_tag` now serves all three; the replacement test drives
`tag_coverage` for real and is red against the old code on exactly the three
variant cases. Live numbers are unchanged (0 divergences across 6 316 records),
which is why it was safe to land.

**The EMC measurement was wrong.** The batches recorded that the app's page-1
text drops EM-Consulte's article code, deleted a built classifier on that
basis, and wrote "do not re-add the branch" into the permanent deferral
registry. Re-measured through the app's own chokepoint, cold cache and warm,
the code is at offset 1302 / 1409 / 1583 on the three files cited as proof —
inside the window `extract_printed_identifier` already scans. Only "it is not
the first line" held. The instruction is withdrawn and the class is now an open
scoping decision, with an argument on its side: it would be the first thing
able to supply the container-scoped Crossref search its missing caller.

Also corrected: `CLAUDE.md` still described the audit's write surfaces as
"exactly three" after `pdf_wanted.json` made them four, and `_restoreMore`
carried a comment promising an in-place append over code that rewrote the whole
list (destroying an open confirm strip and focus). Both now match the code.

Gates: 2 786 Python tests, 648 JS, ruff clean, mypy ratchet 174/175.

## 2026-08-26 — Seven batches on the library workflow: cohorts, queues, provenance, note sync, ISBN, journal bounds, MCP reads

A read-only survey of the live 6 311-record library first, then seven batches
built on what it measured. Three of the findings changed the plan rather than
confirming it, and two of them corrected figures this changelog had otherwise
been about to repeat.

**The survey.** Zero out-of-vocabulary tags across all three writable surfaces;
the three-way harmony report finds 45 rows in the entire library; the 1 231
Obsidian reference notes match the 1 231 Zotero items carrying a reading note
EXACTLY, with 3 content drifts. So tag harmonisation is a solved problem here
and the note-sync invariant already holds — which is why neither batch is a
repair job. What is left is coverage (1 401 records carrying two tags or fewer)
and mapping (371 orphan PDFs, 1 502 entries with no local PDF, 72 Zotero items
outside the join).

**Batch F — the restore journal is bounded, and visible.** Applied
Finder/Zotero ops are exempt from the `_MAX_OPS` cap because their prior tag
lists live INLINE with no snapshot file behind them, so eviction would destroy
the only restore mapping — measured at 11 451 of 11 651 ops, an 8 MB payload
that `load()` JSON-parses on every single tagging write (83–110 ms), behind a
Restore panel that rendered one table row per op. `journal.stats()` aggregates
in SQL and parses no payloads (30 ms); `journal.load_page()` is bounded SQL
over a `seq` cursor (1.1 ms against 110); `journal.compact()` routes through
`load → _TrackedOps.retain → save` so removal reuses the tested
tombstone-then-delete transaction rather than adding a second write path
through this module. Automatic retention exists and **ships disabled** — it is
the only setting in the app whose effect is to delete undo records. Neither
path can touch note or vocab ops, a malformed setting drops NOTHING rather
than guessing, an unparseable timestamp keeps its op, and both honour prune's
`protect_batch_id`/`protect_op_ids`.

**Batch A — tag depth.** Everything upstream treated tags as a boolean, so a
paper with one tag was indistinguishable from one with nine and
`--include-tagged` had to demand an explicit key file. `tag_coverage` gained a
bucket histogram computed in the pass that already walked every record; the
batch gained `--min-tags`/`--max-tags`; the table gained a `#` column and a
cohort filter. The invariant is that the dashboard bucket, the cell, the filter
and `--max-tags N` select the SAME papers — depth is the case-folded union over
the surfaces a record HAS — and both halves are pinned against each other.

**Batch B — the no-PDF list becomes a queue.** `mapping.json` could record
"this PDF is deliberately not in the bibliography"; nothing said it in the
other direction, so *Bib Entries Without PDFs* could only grow. A
`pdf_wanted.json` decision sidecar (dup_decisions' discipline exactly), a
`pdf-decision` route with dismiss/clear/clear_all, item types on the rows, and
bulk dismissal by type — 293 of the 1 502 are types that never have a PDF.
`book`/`bookSection` are deliberately absent from the default type list.

**Batch C — orphans routed by what they physically are.** Orphans run 27% over
30 pages against 8% of mapped PDFs, with a 90th percentile of 300 against 26.
*(Those figures were computed over the 531-file residual this same entry warns
against; over the real 371 it is 36% and p90 417 — see the entry above.)*
A pure classifier over the producer string and page count, riding the pikepdf
open `extract_embedded_doi` already performed and memoised into the same
self-invalidating cache entry. Printed web pages (37 of 371) get their source
URL recovered instead of a DOI search that cannot work.

**Batch E — Zotero → Obsidian note sync.** A closed-vocabulary template
renderer (not a template language: that would be arbitrary code execution over
vault content), content-based comparison, and a writer that CREATES ONLY. In
every measured drift case the Obsidian body is longer than the Zotero note —
the user edits after import — so regenerating would delete real work.

**Batch D — ISBN.** Books and chapters had no resolver at all. Check-digit
validated extraction, ISBN-10 → ISBN-13, its own identifier-grade tier, and
Open Library plus Google Books, both keyless and both off by default.

**Batch G — read-only audit tools over MCP.** Three driver rows and three
tools; `_is_write_call` untouched and asserted untouched.

### Three corrections

**The orphan count was 531; it is 371.** A bridge built WITHOUT
`pdf_text_provider` leaves 531 unmapped because the printed-DOI rung has no
text to read. The app always injects it. Any measurement script must too, or
it overstates the residual by 43%.

**The EMC classifier is not shippable, and the reason is recorded so it is not
rediscovered.** *(Superseded the same day — see the entry above: the
measurement behind this was re-derived and does not hold. The article code IS
present in the text the app serves. Do not act on this paragraph.)* Every EM-Consulte chapter prints its article code as line 1 of
page 1, which would classify the class exactly — but the app's page-1 text
comes from the vault indexer's cache, whose extractor drops that line (PyMuPDF
sees it), and it is absent from `/Info` and XMP too. Verified on three known
EMC files. An inert branch was designed, built, measured and removed.

**A live Crossref record whose title IS its DOI no longer erases the
identifier** — reversing HALF of a deliberate 2026-08-19 decision. Measured on
the 20 EMC entries in `_master.bib`: Crossref holds 18 of 20 by DOI, and 2 of
them answer 200 with a registry stub whose `title[0]` is the DOI string.
`_title_or_none` rightly refuses a DOI-shaped title, but dropping the whole
candidate reported "nothing found" for an identifier Crossref had just
confirmed. On a DOI lookup the identifier is itself the evidence; on a title
search an untitled candidate still cannot be scored and is still refused.

The same probe produced Batch D's container-scoped title search: unconstrained,
a Crossref title search lands on the right EMC DOI 10 times in 20 and returns a
CONFIDENT WRONG record 4 times ("Personnalité narcissique" matched a Kernberg
volume at 0.92); with `query.container-title` it is 14 right and 1 wrong.
Generic clinical nouns are the failure mode, so the constraint is a safety
improvement as much as a recall one.

### Defects the gates caught while this was written

* **ruff** — the provenance splat was pasted into BOTH orphan serializers but
  only one had the map in scope, so the subset report raised `NameError` for
  any non-empty row list; invisible to a test that only ever serialised an
  empty report.
* **The JS suite** — `[Load more]` was gated on `total > shown` rather than on
  the cursor, so a last page could offer a button whose click could not issue a
  request. Separately, Batch B's bulk and undo controls were rendered into the
  meta element, which the `.audit-link-btn` dispatcher never wires: they would
  have been inert.
* **The mypy ratchet** — a comment line beginning `# type: -1 …` parsed as a
  type comment and broke the whole file's type check; and the maintenance step
  read a `-1` sentinel through `_int_opt`, which clamps to ≥ 0, so every run
  would silently have narrowed to the UNTAGGED cohort.
* **Measurement over intuition** — a substring test for short note bodies was
  written, measured worse (a two-word note interleaved into a sentence read as
  total drift at containment 1.0) and removed for one flat threshold.

Gates: 2 748 Python tests, 643 JS, ruff clean, mypy ratchet 174/175 (one
allowance lowered in the same change that earned it).

## 2026-08-25 — Remediating the remediation: a blank PDF, a silent journal, and the gates that missed both

The 2026-08-24 audit-remediation program (`b34063a`, `8499d6b`) passed every
gate it defined — ruff clean, 2 562 Python tests, 614 JS tests, mypy ratchet at
177/177. A follow-up review found two defects those gates structurally could not
see, plus one live user-facing break, and closed the coverage holes that let
them through. Every fix below has a regression test proven red against the prior
commit.

**PDF previews rendered a white page — and the two engines disagree about why.**
A document loaded from a local scheme (`blob:`) inherits its embedder's CSP, and
the previews put that blob in an `<iframe>`, so all six surfaces (audit
inventory/accession/ambiguous decks, the Tagging deck's `V` panel, Deck and
Report Preview PDF) broke together. Chromium refuses on `object-src 'none'`,
loudly, with a reported violation. **WebKit — which is what PyWebView actually
ships — refuses on `frame-ancestors 'none'`**: the inherited policy applies to
the blob document, which is itself framed, so it is told to refuse every
embedder including our own page, and WebKit obeys SILENTLY (the iframe's load
event fires, no violation is reported, the frame is blank). Established by
bisecting the policy in a real WKWebView window: `frame-ancestors 'none'` alone
blanks it, and removing it restores rendering with `object-src 'none'` still in
place. Both are fixed. `X-Frame-Options: DENY` keeps the anti-framing guarantee
— it is a response header and takes no part in CSP inheritance, so it never
reaches the blob document. `tests/test_security_hardening.py` had *pinned*
`frame-ancestors 'none'`, encoding the regression as intent, and no
Chromium-based test could have caught it: that engine renders it either way.

**The tagging journal silently discarded failure states.** Every write path
appends an op, saves it as the crash trail, attempts the write and on failure
flips the same object to `failed` and saves again. That second save was a
no-op, so a failed Zotero PATCH or `_tags.md` edit kept claiming `applied` —
and Restore reverts an un-run *add* by removing tags nobody added. Two causes,
both in `tagging/journal.py`: `prune()` republished survivors through
`manifest["ops"] = kept`, which rewraps each as a **copy** and detaches the
object the writer still holds; and `save()` cleared its dirty set without
recording anything to compare against, so a plain-dict op could never be dirty
again. Fixed with `_TrackedOps.retain` (in-place, identity-preserving) and
value-comparison for this request's own appends. The Phase-3F suite had tested
only `load()`-derived ops — the one shape production never uses — so the new
tests drive the real writers with an injected `OSError`.

**The journal read path took write locks.** `load()` ran `CREATE TABLE IF NOT
EXISTS` + `INSERT OR IGNORE` under `synchronous=FULL`, taking a RESERVED lock,
so a concurrent `BEGIN IMMEDIATE` turned an ordinary read into SQLITE_BUSY —
defeating WAL and 503-ing the Restore manifest during any harmonize/retire
batch. Schema creation now lives only in first-open, migration and save.

**Retrieval concurrency (AP-09 residuals).** Reranking left the mutation lock
but each request gets a shallow `copy.copy` facade over one shared cross-encoder,
so two chats could run inference on the same torch model simultaneously —
previously impossible, and unbounded on Apple Silicon's shared memory. Added a
gate covering the rerank pass only. Corrected a comment in `rag/vault.py` that
claimed `_build_retrieval_pipeline` runs "inside the mutation-lock hold" and is
"the only writer of `top_n`" — both false since AP-09, in a comment whose whole
job is a concurrency invariant. Pinned the ~10 LlamaIndex private members the
prepare/read split depends on with a contract test over the real objects.
Measured `_detach_node` (1.2 ms for a 200-node pool) and deliberately left it
whole rather than micro-optimising.

**Crash-recovery gaps.** A case-only rename whose destination was taken
externally mid-staging left the PDF reachable only from a hidden dotfile with
the op stuck `pending`; recovery now restores it where the filesystem allows and
otherwise records `staged_recovery_rel` so the file is always findable. A
README-mirror transaction that failed closed (an ordinary outcome in a
synchronized vault) wedged the step forever with neither the directory nor a
remedy named; it now names both and offers a confirm-gated
`discard_pending_transaction`.

**Lifecycle and honesty.** The rename-recovery `before_app_request` hook
re-parsed the whole refactor manifest on *every* request while indexing held
the vault lock — memoised on the file signature with a back-off. Snapshot
consumption moved from a literal set in the route onto `Step.uses_snapshot`,
with a source ratchet. A step that ran and genuinely failed is no longer
relabelled "cancelled" because Stop arrived during it (cancellation is now the
step's own `StepResult.was_cancelled`). Accession cancellation reaches the
ladder at each source-call boundary instead of only bracketing a 60-second run.
`#maint-error` gained `role="alert"` — after the AU-03 fix it had no
announcement path at all.

**Security posture, made honest.** The sanitizer no longer re-parses and
re-serialises DOMPurify's output (the mXSS shape DOMPurify warns against) or
disables its URI check via `ALLOW_UNKNOWN_PROTOCOLS`; policy runs in an
`afterSanitizeAttributes` hook, obsidian:/zotero: ride a custom
`ALLOWED_URI_REGEXP`, and `class` is narrowed to `language-*` so untrusted
Markdown cannot borrow app stylesheet classes. Bandit moved from `-lll -iii`
against an **empty** baseline (a combination nothing here can trigger) to
`-ll -ii` against 9 assessed findings, each dispositioned in
`docs/bandit-baseline-rationale.md`. Dependency audits gained the waiver
mechanism the plan required — owner, rationale, path, expiry, and an expired
waiver *fails*. The 6.4 KB script-hash CSP is now sent only on HTML documents.

**Coverage.** `tests/browser/` (Playwright + Chromium) boots the real app shell
and asserts the served CSP, blob-PDF rendering with zero CSP violations,
document reflow at 960/1024/1280/1600 px and axe serious/critical. It closes the
Chromium half of the PDF class; the WebKit half is pinned as a header assertion
instead, because the only engine CI can run does not reproduce it. CI was also
red on `main` for an unrelated reason — the Linux lockfile resolves CUDA torch
(~8 GB) and exhausted the runner disk — now reclaimed before install.
A **macOS-arm64 CI job** runs the platform-sensitive
suites; the renamer's staging branch exists because APFS is case-insensitive and
was validated only on ext4. The `docs/audit_remediation_*_2026-08-24.md` plan
and tracker are deleted per the plan-docs rule, their open items transferred to
Known Issues.

Gates: ruff clean · **2 586** Python tests · **617** JS tests · **7** browser
tests · mypy ratchet 177/177 · bandit ratchet green.


## 2026-08-21 — The Vault Sync suite in-app (Maintenance subsystem, Phases 1–3 + the renamer)

The private `maintain.sh` / Tk panel / `Vault Sync.app` stack is replaced by
one in-process subsystem: a hidden top-level **Maintenance** tab behind a
capability probe + dynamic import, `POST /api/maintenance/run` (SSE, one
`{"step"}` frame per step, one op at a time), and `papermind maintain`
(preserving the non-zero exit gate). **Private-only and strippable** — four
paths (`maintenance/`, `api/routes/maintenance.py`, `static/js/maintenance.js`,
`tests/js/maintenance.test.js`) go at publish (PUBLISHING.md STRIP STEP 4;
strippability rehearsed before Phase 1 landed: the public build boots with
zero maintenance routes and both ratchets stay green). Ten steps: the read
quartet (`pdfnames`/`links`/`attachments`/`library` — the last a view of the
CACHED audit scan, snapshot age disclosed), the Phase 3 audits
(`pathdrift`/`latexrefs`/`tagcheck`, thresholds measured against the live
vault before being set) + the `latex` build sweep (temp-dir artifacts only),
and the opt-in write pair `readmes` (journal-less by decision;
`log_vault_write` per file) / `batchtags` (binds the batch loop extracted to
the public `tagging/batch.py` over in-process route transports). Workstream B:
the biblio PDF **renamer** on the pdfnames findings — strict `- → _` rule
(PATTERN tightened in the same commit), proposals from the mapped bib record
only, every proposal verified to round-trip through the bridge's author+year
parser (unmapping is the failure that matters), duplicate-shaped names refused
toward the duplicates report, journalled `rename_attachment` ops with revert.
Live parity vs the old checkers was byte-identical before decommission;
`scripts/vault_maintain/` was then `git rm`'d (recover from history).
Deferred: bridge bib-side alias keys for the ö→oe table forms; reference
rewriting on rename (v1 refuses). Details: CLAUDE.md → Module Ownership
(`maintenance/`) + Known Issues (2026-08-21).

## 2026-08-19 — `papermind tagging batch --include-tagged` (the under-tagged cohort)

`tagging_batch_cmd` filters its work list through `_untagged(row, wanted)`,
true only when a requested, PRESENT target carries **zero** tags. That is right
for a coverage campaign and wrong for a *quality* one: a paper carrying a single
placeholder — or a single real-but-coarse tag — is already "tagged", so
`--keys-file` narrowed the rows and the gate then silently dropped every one of
them ("To process: 0").

`--include-tagged` drops the gate. It **requires** an explicit
`--keys`/`--keys-file` selection and raises a `UsageError` without one, before
the inventory fetch — the house posture that an *empty or unset* selection is
never "everything" (`_parse_targets`, `_read_key_selection`); without the
refusal the flag would re-propose the whole library, ~6 000 paid calls.

Re-proposing a tagged paper is safe by construction: the propose prompt never
shows the model the paper's own app-written tags (`from_record(exclude_keywords=…)`,
2026-08-12) and `apply-all` **unions**, so the existing tag survives. Pinned
beside `TestTaggingBatchTargets` with the UsageError and an already-tagged row
reaching the work list only with the flag.

## 2026-08-19 — Manual bibliography link picker (closes the assign-to-any-key deferral)

The maintainer: "manually search the Zotero entries and link one to a PDF that
I know for a fact are mapped." The registered deferral read *"the deck has no
'assign to an arbitrary key' verb — a `none` row that IS in the bib under a
spelling no ladder rung catches can only be linked by hand outside the deck"*;
the measured example is a transliterated surname (`von_kaenel.pdf` →
`vonkanel2019`). Both registry entries are removed (single-registry rule).

`audit.js::_openKeyPicker` is a type-ahead over the cached scan's citation
keys, offered from three places: the accession deck card (`[link to…]`, key
**`L`**, added to the keys legend), every accession TABLE row the ladder did
not resolve to a bib key, and — since the component dropped in for the cost of
one `case` — the **ambiguous** deck, where the right key may be none of the
bridge's candidates and the candidate list alone cannot express that. It
searches citation key + title + first author + year, accent- and case-folded
(`foldText`, so "eluard" finds "Éluard"), ANDs its tokens, shows at most 20
with a true-count line, and is a real `role="listbox"` with
`aria-activedescendant`: arrows move, Enter picks, Escape closes and returns
focus to the control that opened it. Its keydown handler `stopPropagation`s —
without that, typing "smith" into a picker mounted inside a deck would drive
the deck's single-letter verbs (`s` = skip).

**No new write surface, and no provenance lie.** The link is the existing
`POST /api/audit/mapping {pdf, citation_key}` behind the same `confirmInline`
(naming both sides and the one file it touches), undoable through the same
`clear` verb with the same decided/undo bookkeeping. It deliberately omits
`source`, so the route stores `"manual"`: a human-picked key is hand curation,
and annotating it `accession` would make the Batch-3 pairings board credit the
resolver for a decision it never made — permanently, and invisibly, at the next
scan. `_accMappingWrite`/`_accMappingWriteFromDeck` gained a `manual` flag for
exactly that; the ambiguous deck's `_ambigWrite` already sent no `source`.

**Data source: the existing `GET /api/audit/inventory`**, fetched once per
scan into a module cache keyed on the scan identity token (two concurrent
opens share one in-flight promise). A dedicated `GET /api/audit/keys?q=` would
ship a smaller payload, but it would add a route with its own gating, its own
404-without-scan contract and its own tests — and would put a server
round-trip in the path of every keystroke, which is worse than one fetch for a
type-ahead. The inventory route already serves exactly this snapshot and the
Inventory tab already pays for it on every visit.

Pinned by nine cases in `tests/js/accessionDeck.test.js` (opens from the real
`L` keypress; accent-insensitive narrowing incl. the transliterated surname;
AND-of-tokens; Enter confirms then posts a body asserted to have NO `source`;
arrow selection changes which key is written; Escape restores focus; the
picker's keys never reach the deck; the link round-trips through `clear`; the
table verb is offered on unresolved / candidate / no-match rows and NOT on a
row already resolved to a key) plus one in `auditAmbigDeck.test.js` (assigns a
key that is not among the row's candidates). 583 JS green, ruff clean.

## 2026-08-19 — A→Z sort + per-table filter, as ONE shared primitive

The maintainer: "one would wish to be able to filter or re-order across the
different tables by A to Z." `ui.js::installTableControls` (feature-blind,
dependency-free — the `confirmInline`/`runTour` precedent) gives every report
table a clickable-header sort with `aria-sort` and one small filter box.
Covers the Inventory board, both drift reports, `tag_harmony`, `tag_hygiene`
(all three of its tables), read/unread `unzoterod` including the accession
ranked table and its two review lists, duplicates/near-duplicates, and the
tagging inventory table. Decks are out of scope.

**It moves DOM nodes; it does not re-render** — a deliberate inversion of the
obvious design. Every one of these tables is painted from a rows array by a
pure string builder, so "sort the array and re-render" looks natural; it would
also destroy per-row state that lives in the DOM and nowhere else. The tagging
table's write cell holds a text `<input>` a user may be mid-token in, and a
sort click would have discarded it silently. Reordering the existing `<tr>`
nodes (`tbody.appendChild(fragment)`) preserves bindings, checkbox state and
carets by construction — there is no rehydration step to get wrong — and
filtering hides rows rather than re-rendering, so it cannot re-enter the
owner's seeding/wiring passes (`_acc.seeded`, `_wireRowActions`, the
`_accessionAttach` handover) on every keystroke.

Details: comparison is accent- and case-folded on both sides (`foldText`
mirrors `audit/search.py::fold` — NFD, drop combining marks, casefold; the
vault is bilingual FR/EN), so "Éluard" sorts under E; columns whose values all
parse as numbers sort numerically (keys extracted ONCE per sort, not per
comparison — at 6k rows a comparator that re-read cells would do O(n log n)
DOM work for one pass of data); blanks sink in BOTH directions ("no value" is
not the smallest value); the filter ANDs its whitespace tokens, is debounced
200 ms, discloses "N of M rows" and announces through the STATIC announcer
(never a freshly-minted live region per table); a header with no visible text
of its own (the verb columns, the `sr-only` select column) is not sortable,
and `data-sort="off"` opts one out; verb-button labels are excluded from both
the sort key and the filter haystack, so "open" does not select every row;
`data-sort-value` carries the true key where the visible text would sort wrong
(formatted byte sizes, a count followed by buttons, an em-dash for "unknown");
and a `td.cell-title[title]` contributes its FULL value to the haystack, since
searching only the 90-character visible slice answers "no match" for a word
that is genuinely in the row (other `title` attributes are explanatory tooltip
prose and are deliberately excluded).
Wire-once is a DATASET MARKER on the table, never a module flag — a module
boolean stays true after the node is replaced and silently leaves the new
table unwired (the audit search-bar defect class). audit.js remembers each
table's sort/filter per report so the accession body's repaint-on-every-write
cannot silently undo a sort the user set; a cross-report search jump onto a
row a live filter is hiding clears the filters first, rather than scrolling to
a hidden row and appearing to do nothing.

New `tests/js/tableSort.test.js` (18) pins the fold, numeric ordering, blanks
sinking, aria-sort exclusivity, non-sortable headers, AND-of-tokens filtering
with its count and empty state, verb labels excluded, the wire-once marker
against a replaced node, and — the design's whole point — that a checked box
and a live listener survive a sort because the node is the SAME node. Plus
integration pins in `auditTagHarmony.test.js` (selection survives a sort and a
filter, strip mounted above the scrollport) and `accessionDeck.test.js` (an
unticked bulk row is never re-seeded, and a sort survives the write that
repaints the body). 569 JS green, ruff clean.

## 2026-08-19 — Audit UX pass: hierarchy over prose, focus that survives a write

The maintainer's verdict on the accession deep-review batches ("adequate but
the UI/UX could be improved"), taken as a focused pass rather than a
redesign. **The accession panel had no CSS at all** — `.audit-acc-panel`,
`.audit-acc-actions`, `.audit-acc-rule` and `#audit-acc-review` were class
names with no rules, so five review-driven clarifications had landed as one
undifferentiated muted run above the buttons. Now: the risk-bounding sentence
("Only a DOI, or a title and one surname, ever leaves this machine") stays on
the surface and the reference detail (which catalogs, the polite-pool email,
the bib-first rule) collapses behind a `<details>` — collapsed by default, or
the pass buys nothing. **Evidence is a chip**: `_accTierLine` splits the
GRADE from the provenance run, and the chip is *toned* by whether an
identifier resolved the row (`chip-id`) or a title did (`chip-weak`) — the
bulk-select boundary rendered per row instead of only stated once in the rule
line. **Review-list headers** split the name + count from the `[all]`/`[none]`
verbs instead of running them together inside one `<h4>`.

**The Inventory pairings board**: the `[detail]` drawer was a run of `<p>`s
with no shared left edge — now a two-column definition grid (`<dl>`), so the
four facets (PDFs / Zotero / Note / Tags) are scannable; and the `[view]`
preview mounts as the *second* child of a layout container, which the
wide-window grid (`.audit-inv-layout.has-preview`, ≥1100px) lays out as a
SIDE panel. It used to be inserted as the body's first child — a 65vh block
that pushed every row off screen, so "look at this pair" and "keep my place
in the list" were mutually exclusive. Its close bar is sticky either way.

**Two registered paper cuts, fixed together** (the registry note said "fix
both or neither"): a table-view mapping write dropped focus to `<body>`, so a
keyboard user working down the rows restarted every subsequent Tab from the
top of the page. The accession table re-renders wholesale, so its writes now
register an ORDERED selector chain consumed after the render (`_accFocusAfter`
/ `_accRowFocus` — a verb that no longer exists, `[link]` became `[undo]`,
still lands in the same row); the ambiguous table disables its own button, so
`_focusAfterRowWrite` moves to the next row's primary verb, falling back to a
still-live verb in the same row. Both writes also announce through the static
announcer, which the ambiguous table never did. **Count-aware skeleton**: the
report body's `Loading…` collapsed a 72vh pane to one line and snapped back to
a thousands-row table; `_skeletonHtml` sizes shimmer rows from the cached scan
summary (capped at 14) and carries `aria-busy`, with the bars `aria-hidden`.

Pinned by `tests/js/accessionDeck.test.js` (chip tone per tier, disclosure
collapsed by default, focus after a table undo and after the bulk link — the
last two proven red against the un-restored render),
`auditInventoryBoard.test.js` (drawer read per `<dl>` facet, preview DOM order
+ `has-preview` hook), `auditAmbigDeck.test.js` (a new table-view describe:
focus moves to the next row, and stays in-row on the last one), and four new
`contrastTokens.test.js` pairs for the chip/disclosure/`<dl>` surfaces.
544 JS green, ruff clean.

## 2026-08-19 — Accession deep-review Batch 4: batch narration, Settings section, mapping provenance

The review's optional tail. **Batch narration:** `resolve_queries` gains a
best-effort `progress_cb` seam (called per source call, a broken callback
never fails a resolution) which the batch worker maps to `{"info"}` frames
("Identifying 3/50 (name.pdf): querying crossref…") — the polite-pacing
sleeps (~3.4 s/PDF) no longer read as dead air, and each frame feeds the
SSE stall guard. **Settings "Accession · online identification" section:**
the five knobs that had `/api/config` validators but no controls
(contact email, per-source checkboxes over the registry, max candidates,
timeout, batch cap) — the enable toggle deliberately stays on the report
beside its egress disclosure; an all-unticked sources save sends nothing
(`[]` means "full registry" server-side, the opposite of what unticking
says). **Mapping provenance:** mapping.json gains an additive
`sources: {rel: label}` map — the accession UI's link writes annotate
`source: "accession"` (route-allowlisted; any other label 400s), plain
hand curation stores nothing (legacy file shape byte-compatible),
no-match/clear/hand-re-curation drop the label, and `build_bridge` stamps
`accession` / `accession_hash` into `source_per_pdf`, so the Batch-3
pairings board distinguishes accession-made links from hand ones at the
next scan. `_load_mapping_full` returns a 4-tuple; `save_mapping` prunes
labels to matched rels. Pinned by
`tests/audit/test_bridge_mapping.py` (annotation round-trip through
build_bridge, legacy shape, label lifecycle ×4),
`test_audit.py::test_mapping_source_annotation_is_allowlisted_and_persisted`,
`tests/accession/test_candidates.py::TestProgressCallback`, and the
updated exact-body assertions in `tests/js/accessionDeck.test.js`.
415 audit/accession py + 529 JS green, ruff clean.

## 2026-08-19 — Accession deep-review Batch 3: the Inventory pairings board

The maintainer's request v ("a board, and a precise overview of each paired
PDF↔Zotero, with in-app PDF preview like tagging's V"), built as shape (i)
of the review's scoping: the Inventory tab IS the board, extended rather
than a 13th report tab. `serialize.inventory_records` additively ships
per-PDF match provenance (`pdf_paths[].source` looked up from
`bridge.source_per_pdf` — recorded since the bridge existed, serialized
never; the `match_sources` union hid which rung matched which file), a
`note` path payload and a light `zotero` block (item key/type, group
routing for the deep link, reading-note count; no abstract by design).
`audit.js` adds a per-PDF `[view]` — the blob-URL preview idiom copied from
the decks, panel above the table, toggled off on re-click, torn down on
report switch (`auditInvTeardown`) — and a per-row `[detail]` drawer:
per-PDF pairing lines ("matched via doi" + view/open/reveal), the Zotero
item with `[open in Zotero]` (zotero://select, group-aware), the note, and
per-surface tags; absent surfaces read "none", never an empty cell.
Read-only throughout — every verb goes through `POST /api/audit/reveal` or
the existing `/api/audit/pdf` read. Pinned by
`tests/js/auditInventoryBoard.test.js` (blob transport + toggle/revoke,
drawer content + collapse, absent-surface wording, group deep link) and
`tests/audit/test_tag_coverage.py::TestInventoryRowPairDetail`. 529 JS +
324 audit-py green.

## 2026-08-19 — Accession deep-review Batch 2: server-side provenance + source-quality additives

The small engine/route half of the review's sources lane, all additive on
the wire. `Resolution.consulted` records every source actually CALLED
(clean-dry included — previously unreconstructable; empty ⇒ the offline bib
fast path, so zero-egress is provable client-side; pre-field cache entries
parse to `[]`). `/rows` discloses the resolved `accession_sources` subset +
`contact_email_set`, and each cached row carries its `queried_at` stamp —
the panel's egress label is now built from the server's source list (a
narrowed config no longer overstates what leaves; a configured Crossref
email is disclosed), cells render "cached · N d ago", and tier lines carry
an "asked Crossref, Europe PMC" tooltip. **Masthead guard**
(`candidates.py`): a title-search record whose title equals its own
container is dropped with a note — the page-1 extractor's
masthead-as-title failure scored such records 1.0 ("The New England
Journal of Medicine", live). **Crossref `subtitle`** joins into the title
(`_SELECT` + `_parse_work`), ending the "Bipolarity from ancient to modern
times:" stub class — future identifications only; cached rows need
[re-identify]. guide.md explains the ladder order (bib-first offline →
primaries → rescue) and the ⚠-badge → re-identify loop. Pinned in
`tests/accession/test_candidates.py` (consulted ×3, masthead ×2),
`test_sources.py` (subtitle), `test_accession_route.py` (envelope + age,
email never in the payload), `tests/js/accessionDeck.test.js`
(server-built label, cache age, consulted tooltip). 85 py accession tests
+ 525 JS green, ruff clean.

## 2026-08-19 — Accession deep-review Batch 1: the display-layer pass (client-only, reaches every cached row)

From the maintainer-requested five-lane review
(`docs/review_2026-08-19_accession_ui_tags_sources.md`). Everything here is
`audit.js`-side by design: resolutions are cached in `accession_cache.json`
and re-served, so render-time fixes reach all ~576 already-identified rows
instantly while engine fixes would not. **Title honesty:** a
`filename_only` row (48% of the live cache — surname+year search, zero
title scoring) no longer leads with an arbitrary same-surname paper's
title; it states the candidate count and defers to the deck. `title_only`
rows are prefixed "maybe:" and their tier reads "title guess — verify".
`_accCleanTitle` strips JATS markup/entities, trims subtitle-stub colons
and ellipsizes the old mid-word slices at every render site (display only,
never matching). **Provenance surfaced:** the payload's always-shipped
`Candidate.source`, `scores[0]`, `notes` and `from_cache` now render — "via
Crossref · match 0.93 · cached" on cells/lists/status lines, a "⚠ N source
note(s)" badge so a degraded lookup is distinct from a genuine no-match
(they are cached, and only [re-identify] retries them), and the offline
zero-egress bib hit says so. **Selection & effect clarity:** the bulk
lists state the default-check rule in the DOM ("DOI-backed rows are
pre-checked…"); the Re-links list gains the matched-title column the
eyeball step needed; decided table rows carry a confirm-gated `[undo]`
(posts mapping `clear`; the bulk confirm's "undoable per PDF" promise now
has an affordance where it was made) and an undo re-checks an
identifier-grade row; counters read "looked up" vs "linked / marked"; the
batch button says "Identify next N of M unresolved"; write/copy success
lines carry the snapshot caveat and the BBT-re-export loop-closer;
disabled-state table cells explain themselves; batch crash rows render
their stored `error`. **Tag-harmony relabel (the review's critique-ii
verdict — no data bug, one misleading column):** "→ Finder" read as "tags
IN Finder"; headers are now "Add to note / Add to Finder / Add to Zotero",
the meta line says the cells list what each surface is *missing*, and every
cell's tooltip discloses the surface's current tags (serialized all along,
displayed nowhere). Pinned in `tests/js/accessionDeck.test.js`
(+8: title honesty, provenance, notes badge, rule sentence, matched-title,
row undo round-trip, disabled hint) and `tests/js/auditTagHarmony.test.js`
(+2: headers, current-tag tooltips). 524 JS tests green.

## 2026-08-19 — Accession review (same day): three server fixes, four UI fixes, one doc-truth fix

The two-lens adversarial review of the accession range (`98be416..41c052b`;
`docs/accession_review_prompt_2026-08-19.md`, deleted per the plan-docs
rule). Fixed, each with a red-proven regression test: the batch's
`_ACCESSION_GATE` leak when the context build crashes between acquire and
the SSE guard; the batch continuing egress after a scan starts mid-run
(now stops between PDFs — "nothing runs during a scan" holds past t=0);
`CrossrefSource._parse_work` crashing the whole resolution on a malformed
record shape; no batch cancel affordance (now `[cancel]` → AbortController,
finished lookups stay cached); the clipboard dead-end on a refused
`navigator.clipboard` (the `_pickCopy` textarea fallback); the accession
deck's `with-pdf` grid never matching its `#audit-acc-*` children
(auto-placement instead of the named areas — selectors extended,
source-ratcheted) plus the undefined `.mono` class; identify outcomes not
reaching the static announcer; and the README's "most resolutions are
answered offline" over-claim (the live batch answered 546 of 576 over the
network). Residual notes + refutations in Known Issues → *Deferred from
the 2026-08-19 accession review*. Suites 2,073 py / 514 JS, ruff clean.

## 2026-08-19 — Accession Phase 3: the "PDFs Without Zotero" extension (first user-visible release)

The report gains the accession surfaces, no new tab: `/api/accession/rows` /
`identify` / `batch` (`api/routes/accession.py` — egress gates in order:
`accession_enabled` (default off) → never during a scan → cached scan → the
path is one of that scan's orphans; single-flight 429; linking reuses
`POST /api/audit/mapping`, no new write surface), six validated config keys,
and the `audit.js` `_acc*` UI: an Identification column on the ranked table,
the disclosure-labelled enable toggle, a confirm-gated batch, the **bulk
review lists** (Re-links → mapping writes; Accessions → copy DOIs for
Zotero's Add Item by Identifier) where only identifier tiers default checked
and `title_filename` is listed unchecked, and a keyboard **review deck**
below the bulk bar (ambig-deck idioms: confirmInline everywhere,
peek-not-pop undo via mapping `clear`, blob-URL PDF panel).
`test_accession_route.py` + `tests/js/accessionDeck.test.js`. The live
full-library batch (all 576 orphans through the real pipeline, 33 min at
polite pacing) closed the plan: **30 links / 415 candidate sets / 52
margin-refused / 79 none** — Phase 4 shipped by construction (rung 5 + the
French rescue sources are in the engine and ran), **Phase 5 measured and
declined** (the no-text class no longer exists; the residue is catalog
coverage, not extraction — 60 of the 79 `none` rows had a good title no
keyless catalog holds, only 13 were extraction-abstained), Phase 6 awaits a
user-side Zotero capture. Follow-ups from the batch: a per-row
**re-identify** verb (`force` bypasses the cache consult, never the store —
theses.fr 400'd 45 calls it answered 200 minutes later, and a cached
degraded resolution could otherwise never improve) and select-all/none on
the review lists. Registry updated; the plan doc is deleted per the
plan-docs rule.

## 2026-08-19 — Accession Phase 2: the `accession/` keyless resolver (headless)

The core package behind the coming "PDFs Without Zotero" extension: ranked
query building from what a PDF discloses about itself (`identify.py` — the
page-1 title extractor is author-anchored and was calibrated against 4,024
mapped PDFs' own bib records, 21.8% → **61.6%** of emissions ≥0.65 with
median 0.934), five keyless sources behind one protocol (`sources.py` —
Crossref / Europe PMC / arXiv / HAL / theses.fr, shapes verified live and
recorded as fixtures; hardened HTTP mirroring `tagging/zotero_write.py`),
evidence-tier scoring with margin refusal and a bib-before-network guarantee
(`candidates.py`), and a sha256-keyed resolution memo (`cache.py`). No route,
no UI, no config keys yet; README documents the egress conditions beside the
Tavily note. The measurement also found and fixed a **Phase 1 bug**: PDF text
layers leave control characters inside captured DOIs (BMJ page-1 DOIs ended
in a literal backspace), so `normalise_doi` now strips Unicode category-C
characters and the bridge's DOI join re-normalises memoised values — 3 live
orphans heal into links at the next scan. Suite: `tests/accession/`
(hermetic by construction — an autouse tripwire fails any test that reaches
the network) + control-char regressions in `tests/audit/test_bridge_doi.py`,
proven red pre-fix.

## 2026-08-18 — review fix batches (A–J)

Fixes for the 2026-08-18 adversarial review (`fdb2aa9..b46dd75` + UX/a11y
lanes), one commit per batch; each defect fix ships with a test proven red
against the pre-fix code.

**Batch A — deckgen/docgen citation pass.** The HIGH: a **no-template CLI deck
emitted an undefined `\refline`** and could not compile — only
`assemble_with_template` injects `REFLINE_PROVIDECOMMAND`, and the CLI bound
`footer_mode="refline"` unconditionally (the integration tests pinned the
broken output; they now pin the compilable `\vfill` form). Also: the citation
pass's debris cleanup is now **sentinel-scoped to the exact tag-removal
sites** — the frame-global regexes collapsed correct French « mot : » spacing,
tabular alignment runs, and punctuation-initial indentation on tag-free lines;
a body with **no complete frame keeps its `(source:)` tags** instead of having
attribution deleted with nowhere to move it; `relevant_bib_keys` **accent-folds
both sides** before tokenizing (the ASCII tokenizer split `dépression` into
`pression`, which the word-boundary anchor then rejected — French queries
matched nothing); the augment op-rules' doubled `{{…}}` escapes (appended
*after* `.format()`, so they reached the model verbatim — Known Issues item 9)
are single braces in both `deckgen/prompts.py` and `docgen/prompts.py`;
`latex_escape_metadata` escapes backslash (source names are model output);
a frame that already carries a source footer has new names **merged** into it
across augment cycles instead of stacking a second footer; and the
between-frames comment, `rewrite_source_tags` docstring, `_short_authors`
docstring and `_BIB_AND_RE` brace comment now describe what the code does.
Deliberately NOT fixed: the pre-upgrade checkpoint salt (stale checkpoints
resume with raw tags once, then self-clear).

**Batch B — driver/CLI truthfulness.** The HIGH: `core/driver.py::
tagging_inventory`'s response whitelist stripped `scanned_at`, so the CLI
batch's snapshot-staleness disclosure — the age stamp, the stale warning, the
rescan remedy — was dead end-to-end against a healthy server, always printing
the misdirecting "scan time unknown (older server?)" fallback; its tests
passed because they mocked `cli.main.tagging_inventory` above the driver. The
field is forwarded, and a **response-projection ratchet**
(`TestDriverResponseProjections`, mocked at `_http_json`) now pins every field
a CLI consumer reads. The batch's model-disclosure line reads the new
`driver.tagging_model_info()` (one `/api/config` GET) instead of
`get_status()`, whose `/api/obsidian/status` GET destructively drained the
GUI's one-shot vault status feed on every run. `--keys-file` reads
`utf-8-sig` (a BOM glued invisibly to the first key) and a non-UTF-8 file is
a `UsageError`, not a traceback; the missing-keys diagnostic blames the
client-side `has_pdf` filter when that is what removed a key, instead of
"outside scope". The key-selection tests' propose mock used the route's
unwrapped shape where the driver returns `{"proposal": {…}}`, so they only
ever exercised the skip path — fixed, with a would-apply assertion proving
the propose path runs. `ensure_server_info` now probes `/api/health` on a
foreign record's claimed port before deferring to its live pid (post-crash
pid reuse permanently disabled the self-heal); a live pid that ANSWERS the
probe is still never stolen.

**Batch C — `scanned_at` is the snapshot's own build time.** The served value
was the manager's last-finalise time, which lied in exactly the two states
where staleness matters: `start_scan` zeroes `_finished_at` while deliberately
keeping the previous inventory live (mid-scan, real data served with
`scanned_at: 0` — and `tagging.js` cleared the user's row selection twice for
one snapshot, stamp T→0→T2), and `_finalise` stamps unconditionally even on an
errored scan with `inv=None` (a stale snapshot reported freshly scanned,
silencing the CLI staleness warning in the case it was built for). The
`Inventory` now carries `built_at`, stamped at construction; both
`/api/audit/summary` and `/api/tagging/inventory` serve it — one atomic read
of one object, no torn pair of lock acquisitions. The route tests pin the
mid-scan and stale-after-error semantics against real manager time state
instead of a mocked `get_scan_times` (which had pinned plumbing while the
semantics were broken).

**Batch D — tagging journal/restore concurrency.** The batch restore
plain-saved the phase-2 manifest after a lost op-lock — the clobber class the
write path's merge-save had already fixed — silently dropping any op a new
lock owner appended during the lapse; it now goes through
`save_merged(prefer_ours=<this batch's ops>)`, which keeps the in-memory
`reverted` flips (plain disk-wins would discard them, leaving reverted stores
double-revertible) without clobbering foreign ops. `save_merged` distinguishes
corrupt content (safe to overwrite — the disk copy is already lost) from a
**transient `OSError` read** (`JournalTransientReadError`, e.g. an unmounted
archive volume — refused with a loud log; callers keep the in-memory manifest
and retry at the next checkpoint), and its docstring stops claiming
resurrection is "effectively never" (an op a new owner legitimately spent
during the lapse IS re-appendable, bounded by the note-revert's hash check).
The verbatim-duplicate `_bump_save`/`_tick` closures in retire + harmonize are
hoisted into one `_BatchJournalKeeper` (with a route-level lock-lost test for
the retire copy). `_harmonize_plan` reports a selected key whose row yields
zero writable ids in `unresolved_keys` instead of shrinking `records` without
attribution, a malformed key value is a 400 naming it (not a silent drop), and
`_HARMONIZE_MAX_KEYS` is checked post-coercion/dedup so 2001 duplicates of one
key are one write, not a refusal. The three comments (and the phase-4
user-facing failure string) claiming an un-run Zotero ADD's revert "would
remove tags it never added" are corrected — `_revert_zotero_prepare`'s
after-equality check conservatively skips that case; the real hazard is a
journal claiming a write that did not happen. The vacuous `writes = {}`
assertion in `test_propagates_a_finder_tag_into_the_note` is wired to real
patches on the disabled targets' writers.

**Batch E — audit engine/report honesty.** `tag_harmony` carries
`finder_platform` (the `tag_coverage` precedent) and the renderer caveats the
→ Finder column off macOS — Finder xattrs read empty there, so every union tag
looked "missing in Finder" with no disclosure. `OrphanZoteroItem` regains
`library_type`/`group_id` (additive) and the orphan rows forward them, so the
`[Zotero]` verb deep-links group-library items via
`zotero://select/groups/<id>/items/<key>` instead of a personal-library URL
that selects nothing; "an entire uncovered group library" joins the bucket's
documented causes. The harmony search-corpus detail no longer renders a
dangling " · " for a bib-less record, and the cancelled-scan orphan test's
docstring stops describing pre-`49afa1d` code as current.

**Batch F — audit.js state machines.** Harmonize gets a real in-flight busy
gate (`_harmonySetAllDisabled`, retire's `_retireSetAllDisabled` twin): the
apply used to disable only the button while any checkbox change re-armed it
from selection size, so a second concurrent run was two clicks away. The
harmonize/retire status hosts (and their undo buttons) are report-identity
aware — a mid-stream tab switch no longer sprays them into a foreign report.
`_loadSummary` gets a `makeLatestGate` (superseded on scan-identity reset, so
a straddling fetch cannot repaint the previous scan's tiles) and a **visible**
"summary unavailable" state on non-404 failures. The first-scan milestone
burns its one-shot `localStorage` flag only after the count actually renders.
The audit Cancel button is re-armed on every new scan (one successful cancel
used to leave it permanently dead). A per-report 404 renders an in-body empty
state that keeps the tablist + search bar when a scan exists (2026-08-12
adjudicated #4 — a skip-duplicates scan's Duplicates tab made every other
report unreachable). The search 202 "Preparing search index…" retry loop is
capped (~30 s) with an actionable escalation message.

**Batch G — state-feedback parity.** Single Paper preserves already-streamed
text on both Stop and mid-stream error (2026-08-12 adjudicated #2 — a
90%-streamed map-reduce summary was erased; the note rides the notices strip
so exports stay clean, and a kept partial is exportable). Plain chat records a
kept partial into `_history` as an `[interrupted]` assistant turn with the
user turn kept (adjudicated #5 — a visible exchange left no trace, so
follow-ups were sent contextless), and a retry after a kept partial no longer
double-renders the question (the wire copy re-asks it; history keeps one
rendition) — the same double-bubble fixed in vault chat. `deck.js` gains the
retry boundary `docgen.js` already had, on generate and augment (the
checkpoint makes retry cheap). The tagging deck messages the busy-window
no-ops for `P`/`A`/`⌫` (previously silent dead keys for up to 60 s) and its
60 s apply abort says "timed out — the write MAY have landed" (CLI-parity
honesty) instead of asserting flat network failure. The refactor plan stream
gets a Stop button (read-only server-side, always safe); Revert-all's frozen
line now states its duration honestly (per-op progress needs an SSE
conversion — recorded as a deferral). Parity set: docgen says why
Preview/Compile are absent when latexmk is missing, renders "no change
produced" as guidance not error, the tagging table shows "applying…"
in-flight, `config.js` model-selection saves surface failures, and the audit
status poll phrases transient failures instead of dumping raw exceptions. The
dead `#settings-status` region is wired: a visible "Saved ✓" tick per clean
save and a visible NOT-persisted notice on transport failure.

**Batch H — accessibility.** `#obsidian-status-msg` ships `role="status"` in
the static markup (it was minted at write time, textContent first — the
same-tick WebKit hazard `liveRegions.test.js` exists to prevent — and was
unpinned) and index start/completion announce through `announceStatus` (a
multi-hour run used to end silently; the activity bar is not, and must not
become, a live region). The six agent-trace activity logs lose `aria-live` —
a deck run's ~50–100 per-iteration lines monopolized the SR queue (the audit
search overlay's deliberate "no aria-live" precedent) — pinned NOT-live like
`#pull-status`; harmonize/retire per-op announcements throttle to every 25th
op + the final one, visible text unthrottled. The ambiguous-PDF table's
per-row `<select>` and `[assign]`/`[not in bib]`/`[reveal]` verbs get
row-context accessible names (the deck view of the same report already had
them), and the row-context sweep covers the Zotero/Finder drift verbs, the
tagging table verbs, both Restore surfaces' revert rows, the
exclusions-modal Remove buttons and the orphan `[open]`/`[reveal]`.
Ambig-deck candidates carry `aria-pressed` and `1`–`9` announces which
candidate was selected; tokenfield chip commits/removals announce. The
command palette backs its `aria-modal` claim with the real background inert
(`openModal`'s mechanism, modal-aware on close). Send buttons park focus on
the input before disabling themselves (focus no longer drops to `<body>`),
and the tour's `scrollIntoView` honors `prefers-reduced-motion`.

**Batch I — hierarchy & responsiveness.** The audit sticky headers are real:
`.audit-report-body` is a bounded vertical scrollport (72vh) so
`th { position: sticky }` engages — previously the whole pane scrolled and two
screens into a 6,000-row report the headers, tablist, search bar and Run Scan
were all gone; `.audit-table-scroll` (harmony's wrapper, now also on
`tag_hygiene` and both drift tables) is a full scrollport (66vh) so its wide
tables get sticky headers too while the toolbar/status/undo stay anchored on
both axes; the tagging sub-view keeps its overflow opt-out (type-ahead
dropdown). `.inline-confirm-strip` wraps (`white-space: normal` — the per-tag
× confirm rendered as one unwrappable line inside a table cell). One primary
per tab: deck/docgen's second and third `.btn-primary` demoted to outline, and
refactor's Rendered/Diff/Formatting-fix view toggles use the `.theme-switch`
segmented pattern (selected = `.active` + `aria-pressed`, never a filled CTA).
The two tag tabs are renamed per the both-sides naming precedent — "Tag
Harmony" → **"Note vs Finder vs Zotero Tags"**, "Tag Hygiene" → **"Tag
Spellings & Vocabulary"** — paired across `_REPORT_LABELS`, the tab captions,
the search-group headers and `guide.md`. Actionable dashboard tiles
(unmapped/ambiguous/tagged-nowhere/orphans/dead-mapping) warn-tint their value
when non-zero; the harmonize undo says "N paper(s)" instead of journal-op
counts; the three placeholder-only inputs gain visible format hints; and the
tablist group dots stop reusing the N/F/Z target-identity color tokens.

**Batch J — registry closure.** Known Issues updated: adjudicated items #2,
#4, #5 and #9 struck as FIXED (batches G/F/G/A, each with the fix and its pin
named, the #3 precedent), the Settings-tiering entry carries the review's
drop-tiering-keep-filter recommendation, and a new *Deferred from the
2026-08-18 review* section records every deliberately-unfixed residue
(checkpoint salt, revert-all → SSE, harmonize protected-op growth disclosure,
sidebar collapse, deck/docgen nested-scroll walls, skeletons, the declined
sticky first column, and the `_batchtags_facts` second-consumer hygiene note).
`docs/code_review_2026-08-18.md` and its batch prompt are deleted per the
plan-docs rule (git history keeps them; last carrying commit `6e2272a`).

## 2026-08-13 (review fixes: the journal cap was fiction, and eight smaller truths)

The adversarial review of the tag-harmony range (`fbb3b40..b2ceb5f`), every
claim re-verified with a red test before fixing. One HIGH, everything else
proportionate.

**The HIGH — `journal.prune`'s cap budget counted ops it could never evict**,
so the entry below's claim that `_HARMONIZE_MAX_NOTE_OPS` "bounds the batch by
what `journal.prune` can RETAIN" was false: with the live manifest holding
4,105 never-evictable applied finder/zotero ops against `_MAX_OPS` = 200,
`over` was permanently positive and EVERY note op — harmonize batches, deck
Accepts, apply-note-tags — was evicted, snapshot included, by the very next
tagging write's prune. Worse, `_restore_batch` counts only surviving ops, so a
gutted undo reported itself complete ("3/3 reverted" for a 10-op batch). The
budget now counts EVICTABLE ops only: note ops get a genuine 200-deep
retention window and age out oldest-first, the harmonize bound actually keeps
a batch inside half of it, and the confirm dialog's "undoable in one click" is
true within that window. (Two tests that had pinned the defective accounting
incidentally were rewritten to the new semantics; proven red against the old
code.)

**Cancelled scans stop lying about the library** (the 2026-08-12 adjudicated
finding 3, implemented as decided): the records loop no longer `break`s on
cancel — it skips only the per-record annotation reads — so a cancelled scan's
cached inventory carries every record and `tag_coverage`'s denominators, the
reports and the widget describe the whole library, not a hash-order-arbitrary
subset.

**The lock-lost checkpoint merges instead of clobbering.** Both batch routes
(retire, harmonize) saved the manifest after a lapsed op-lock TTL — necessary
(the pending op records describe writes that already LANDED; dropping them
destroys their undo) but a blind overwrite of a manifest another writer may
now own. `journal.save_merged` reloads from disk and re-appends only our
missing ops; the comments and retire's failure message, which both claimed
saves stop entirely, now describe what actually happens.

**Smaller, in one breath:** the dashboard drops its cached tiles when tab
re-entry sees a scan finished outside the GUI's poll loop (CLI/MCP);
`/api/audit/summary` carries an additive `scanned_at` so the vault-maintenance
GUI stops fetching the full tagging inventory to read one float; the tagging
picker's selection is genuinely snapshot-keyed now (its comment claimed so; a
rescan clears it, scope/view toggles keep it); a successful harmonize spends
its selection instead of re-arming a no-op three-store pass; the harmony undo
gained retire's cancel affordance and its own anchored ratchet
(`auditHarmonyUndo.test.js` — it had shipped untested); the wide harmony table
scrolls in its own container so the toolbar and undo button stay visible; the
driver's `_tagging_model` mirrors the route's 120-char truncation so its
"cannot drift" docstring is honest; and the help glossary's "four of them can
hold tags" is corrected to five (three writable). The `find_harmony`-per-
corpus-build cost is recorded in Known Issues rather than fixed.

## 2026-08-13 (tag harmony: see the surfaces, name them, merge them additively)

Five phases turning "my tags are scattered across four places and I cannot see
where" into a dashboard, a shared vocabulary, a three-way report and one
confirmed, undoable additive merge.

**Phase 1 — the surfaces are countable now.** No count of "papers carrying a
tag, and where" existed anywhere: not a report, not a summary key, not a CLI
check. `serialize.tag_coverage` adds one, as a pure pass over the already-joined
records. Every writable surface reports `*_present` beside `*_tagged`, because
the number without its denominator is the Tag Hygiene floor-as-total failure
again — "1 200 note-tagged" means nothing until you know whether 1 300 or 6 000
records have a note. `finder_is_floor` and `finder_platform` ride the payload so
no client can render the Finder figure without its two caveats. Deliberately NOT
added: a "needs harmonizing" count, which needs the fingerprint and the synonym
map — a cheap set-difference would disagree with the report over every accent.

Two orphan buckets came with it. A reference note whose stem is in neither the
bib nor the bridge, and a Zotero item no record claimed, were both discarded in
LOCAL VARIABLES during the scan: uncounted, not merely unrendered. They are
`Inventory.notes_without_record` / `.zotero_items_without_record` now, with
their own tabs and search entries.

`GET /api/audit/summary` gives the dashboard its own feed. The tiles used to be
a by-product of the Inventory TAB, so a dashboard describing the whole library
stayed blank for anyone who clicked another tab first. Two live bugs fixed on
the way: that blankness, and a first-scan milestone reading
`status.record_count || status.counts.records` — neither field has ever existed
in the status payload, so it always rendered its countless fallback and the
one-shot flag made that permanent.

**Phase 2 — "Zotero note" meant two different things.** It reads equally well as
the child note inside Zotero and the vault note named after a citation key, and
the drift reports are exactly where that costs you. The help guide gained a
glossary (Bib entry · Zotero item · Zotero child note · Reference note · Biblio
PDF · Record), and both drift tabs now NAME THEIR SIDES: "Tag Drift" →
**Zotero vs Note Tags**, "Finder Tag Drift" → **Finder vs Note Tags**.

**Phase 3a — the comparison nobody could make.** The two existing drift reports
are pairwise, and Finder vs Zotero was compared NOWHERE; Tag Hygiene reads all
five stores but aggregates globally by tag, so "paper X has this tag in Finder
but not Zotero" was underivable. `tag_harmony` answers it per record. Three
scope decisions: writable surfaces only (child-note tags and bib keywords are
read-only everywhere, so unioning them would propagate tags no writer could take
back); **no ≥1-Finder-tag gate** — `finder_tag_drift`'s asymmetric gate is right
for its own question but would have excluded the commonest harmonize case, a
tagged note and an untagged PDF; and actionable rows only, since this report is
what a bulk selection is built from.

**Phase 3b — `POST /api/tagging/harmonize`.** The client sends KEYS ONLY; the
union, its vocabulary resolution and the writes are all server-side. The
two-step resolution order is the correctness core: `_vocab_id_resolver` maps
each drift surface to the exact `_tags.md` id sharing its canonical key (the
report compares by OpenRefine fingerprint, the writers gate on the LITERAL id
under `nfc().lower()`), then `_canonicalise_tags` re-gates it. Without the first
step every accented or hyphenated tag is dropped as out-of-vocabulary while the
row reappears at every scan — the unresolvable-row loop `apply-tag-drift`
already had to be fixed for once. The test for it is verified RED with the
bridge disabled.

Lock choreography is `retire-tag`'s four phases verbatim, which meant giving the
three ADD writers the `batch_id=`/`defer_save=` plumbing their removal twins
already had and splitting `_zotero_commit_locked` into `_zotero_journal` +
`_zotero_patch`. **One asymmetry matters:** an un-run removal reverts by
re-adding tags that were never removed (idempotent), whereas an un-run ADD would
revert by removing tags nobody added — so a cancelled or failed PATCH is flipped
to `failed`, never left `applied`. Selection-driven and capped at 2 000 keys
because the journal is the binding constraint: every write rewrites a ~3 MB
manifest. Out-of-vocabulary tags are skipped and named, never auto-added.

**Phase 4 — pick the subset, and see the model.** `papermind tagging batch`
gained `--keys` / `--keys-file`; the tagging table gained a checkbox column and
a **Copy keys** export that writes exactly that file format. Before this the
only narrowing was `--limit`, a head slice by year — so repeated runs
re-proposed the same oldest papers, since no tag write refreshes the snapshot
the selection reads. An EMPTY selection is a usage error, not "the whole
library" (the empty-vs-unset distinction that already inverted `--targets`).

And the model is named where the money is spent: propose returns additive
`provider`/`model`, the batch prints them before starting, `papermind status`
prints `Tagging Model:` when it differs from the chat model — it reported only
the chat model, which is wrong precisely when `tagging_propose_model` is set —
and the Vault Sync panel shows model + snapshot age in its batchtags sub-frame.

Also fixed: `auditRetireUndo`'s `undoBlock()` searched the whole file for the
first `/api/tagging/restore`, so the moment a second batch-undo existed it
retargeted onto the new code and would have stopped guarding retire while still
passing. A ratchet that can drift onto a different function pins nothing.

**Post-review fixes.** An adversarial review of the write path found five
divergences from the retire template, all fixed: (a) the ADD writers gated
their journal saves on `defer_save` alone where the removal twins use
`shared and defer_save`, so a `manifest=None, defer_save=True` caller would
have pruned (unlinking other ops' snapshots), written the vault, and persisted
NOTHING — latent, since both current callers pass a shared manifest, but a trap
invisible at the call site; (b) `_HARMONIZE_MAX_NOTE_OPS` now bounds the batch
by what `journal.prune` can RETAIN, because `protect_batch_id` guards only the
batch's own prune and note ops are evictable — a 500-paper harmonize would have
had its undo silently gutted by the next unrelated tag write while the button
still offered it; (c) Zotero ops were ticked in both phase 2 and phase 3, so
`[done/total]` overshot; (d) `total` counted (record, target) pairs while the
message called them tag writes; (e) `zotero_items_without_record` trusted a
claim set populated inside the loop that breaks on cancel, so a cancelled scan
reported joined items as orphans — the BBT-key join is recomputed from
`all_keys` instead, which no cancel can truncate. Both behavioural fixes have
regression tests verified RED against the pre-fix code.

Gates: 1 937 pytest + 237 CI-only + 460 vitest + ruff + the compile sweep.

## 2026-08-13 (deck prompt cost, citation artifacts, presets, server-discovery self-heal)

Four fixes from one expensive deck run: a 50,421-token prompt billed at $2.51 a
request, slides littered with raw filenames, and a CLI that reported the app was
not running while it plainly was.

**The candidate-reference block was mostly surnames.** `bib_candidates_block`
rendered the BibTeX `author` field verbatim. Measured on the maintainer's
6,141-entry `_master.bib`, **30 entries (0.5%) held 29% of every author character**
— the largest 45,344 — and one of them selected into a 12-key block made that
block **42,871 chars (~10.7k tokens), 94% surnames**, which the agent loop then
re-sent on every iteration of every section. Worse, `relevant_bib_keys` scored by
bare substring over `key + author + title`, so a 40k-char surname list acted as a
*token sponge*: the Global-Burden-of-Disease paper visible in the failing run
entered a regression lecture's candidates on `aire` and `ratio` alone, both
matched **inside** surnames, tying on score with genuinely relevant sources and
winning the alphabetical tiebreak. Mega-author papers were therefore
*preferentially* selected, then dumped.

Both halves are fixed by one change: the author (3 names + "et al.") and title
(200 chars) are bounded in `_parse_bib_into`, so the shortened value feeds the
relevance haystack **and** the rendered block. Safe because those two functions
are the only consumers of the values; every other caller uses keys. Key matching
stays unanchored (a BBT key is a run-together camelCase compound — anchoring it
would lose every real hit), while author/title match at a word-boundary prefix,
keeping `regression` → `regressions` but rejecting mid-word fragments. Measured
over four realistic section seeds: **46,112 → 8,902 chars, 81% smaller**, and the
regression seed now surfaces four actual methods papers instead of an
air-pollution study. The per-`.bib` parse cache also drops 1.29 MB → 170 KB.
`docgen` inherits all of it — same two functions.

**Decks no longer carry raw filenames.** The section prompt tells the model to
attribute claims as `(source: note-name.md)`; that string is now treated as a
machine-readable **contract** with a deterministic consumer,
`deckgen/latex.py::rewrite_source_tags`. It works because a Better-BibTeX vault
names reference notes by citation key — **1,229 of 1,232 Zotero note filenames are
verbatim `_master.bib` keys** — so a hit is a dict lookup, not a guess: it becomes
`\citefoot{key}` (`\autocite` for articles), while sources with no key are lifted
out of the bullets into one grouped `\refline{Source : …}` footer per frame
(a `\footnote` for prose, which has no frames). Pure, idempotent, indentation
preserving, and a no-op returning the input object when no tag is present, so a
citations-off deck is byte-identical to before. Unticking citations passes an
empty index, so that toggle still means "no `\citefoot`" while the tidying
still happens. The generate path emits a matching `\providecommand{\refline}`
into the deck it assembles; the augment path, which overwrites a file the user
owns, falls back to the macro-free `\vfill{\tiny…}` form rather than edit that
file's preamble. Wired via two mechanisms: a `post_process` hook on `generate_section` (covering
generate + salvage for both document classes, bound by the deck route, the
docgen route **and** the standalone `python -m deckgen` CLI, so no surface is
left emitting raw filenames) and four direct calls in the augment workers. Two
`test_cli_integration.py` assertions flipped from the raw tag to the footer,
which proves the content survived *and* that the pass ran. This replaces a
hand-run cleanup prompt the maintainer had been firing at finished decks.

**Two of those hand-written prompts ship as optional chips**, distilled to one or
two sentences each — the workflow scaffolding they carried ("act as an expert",
"ask for confirmation", "run make", "report in a table") is all already provided
by the app's own preview/confirm/compile surfaces. A Beamer-audit set on the deck
augment instruction field and an OCR-cleanup chip in Note Refactor's free-prompt,
both through the existing `renderExampleChips` primitive (so a typed draft is
appended to, never clobbered).

**`server.json` now self-heals.** The 2026-08-12 lead — the GUI serving happily
while its discovery file was absent, silently disabling every CLI/MCP write — is
still **unexplained and stays open**; this makes it survivable rather than
pretending to fix it. `ensure_server_info()` re-asserts this process's
registration from `/api/health`'s cache-miss path, so a vanished record is
restored within one 15 s UI poll instead of lasting the process's lifetime. It is
deliberately narrow: a no-op unless this process actually registered, and it
never overwrites a record naming a live *other* pid, since `clear_server_info`'s
pid gate depends on nobody stealing another server's claim. `launch.py` now logs
the write site explicitly (the original investigation could only *infer* it was
reached, which is what left "never written" and "written then removed"
indistinguishable), and `papermind status` says what is actually broken plus both
workarounds instead of a bare "not running".

Gates: 1,881 pytest + 237 CI-only + 424 JS, ruff clean.

## 2026-08-12d (review remediation: `2f8ed8d..bc2e65d`)

A six-seam review of the previous 20 commits, each finding put through a
refutation pass and a concrete reproduction before it was believed. Seven
batches, most severe first; every fix landed with a test verified red against
the pre-fix code.

**Writes that did the wrong thing.** `papermind tagging batch` drops the Finder
target from a PDF-less row, so `--targets finder --no-pdf-entries` emptied the
list on every row — and `driver.tagging_apply` tested `if targets:`, omitting
the key entirely, which the server reads as "every target ON". The user's
restriction inverted into note + Zotero writes across the library (journalled
and reversible, but real). The driver now distinguishes an explicit empty
selection (all-false) from an unset one (`None`), which also closes it for MCP
clients sending `"targets": []`; the CLI narrows targets at *selection* time so
an impossible combination costs zero LLM calls; and an unknown target name is
rejected in both commands instead of silently mapping every real target to
false. `("note", "finder", "zotero")` became one constant, because that tuple
decides what a `targets` dict means on the wire.

**The Zotero join's remaining hole.** `d534384` made the identity authoritative
(BBT citation key) after the title heuristic mis-directed 10 real tag writes,
but its fallback still matched against every item including keyed ones, first
wins. With a stale bib key plus a title collision, a record joined onto a paper
that already belonged to another record — the reproduction shows `smith2020`
resolving to Owen's item. The fallback now runs as a second pass and refuses to
guess three ways (ambiguous title, already-claimed item, two records contending),
with a `zotero_title_refused` counter so refusals are legible. Excluding keyed
items outright was rejected: all 6194 items here are keyed, so that would have
made the fallback dead code.

**Honest reporting.** The batch's "resumable by construction" claim was exactly
backwards — "already tagged" comes from the audit scan snapshot, which no tag
write updates — so the inventory now carries `scanned_at` (via a new
*non-draining* `get_scan_times`, never `get_status_payload`) and the batch
prints the snapshot's age plus the rescan remedy. A wedged provider no longer
sweeps the library at ~1/s: consecutive propose failures back off and abort.

**Smaller, same shape.** Retire-tag matched candidates with `.lower()` while the
writer it feeds is NFC-insensitive, so an NFD-stored Finder tag survived a retire
that reported success. A symlinked `biblio_articles/` 400'd every path-addressed
removal (containment pre-filter compared a realpath against an unresolved root).
The deck offered a Finder toggle on cards with no PDF, promising a write the
server then skipped, and never showed that a proposal came from metadata alone —
the field the route sends for exactly that purpose. `begin_index_run` published
its epoch before clearing the stop flag, so a Cancel could release the run's
op-lock and then be erased.

**Tooling + the deferral registry.** `sync_readmes --prune` verified only
containment, not authorship, so a hand-written `readme_*.md` in the destination
was deleted; it now requires the generated frontmatter. `audit_zotero_tag_writes`
counted already-reverted ops as outstanding (exit 1 forever) and reported removal
ops with add-semantics, inverting every derived field. The trust-check parity test
exercised only one of the two deliberate copies despite its name — it now stubs
tkinter and runs both. The three vault checkers in `docs/` moved into
`scripts/vault_maintain/` with their hardcoded iCloud path scrubbed, so they carry
the publish strip *and* the scrub rather than relying on the strip alone. Finally,
the nine 2026-08-08 leads whose verifiers were cut off are now adjudicated —
**all nine confirmed**, three reframed, recorded with file:line and repro as a
ready-to-implement queue rather than re-litigable leads.

## 2026-08-12c (vault indexing: "Update index" appeared to need two clicks)

Clicking **Index** flashed "Indexing complete." and restored the idle buttons;
clicking it a second time appeared to start the run. Confirmed from
`chatekld.log`: two clicks, **one** `Starting Obsidian vault indexing…`. The
first click always started the run — the UI reported the *previous* run's
outcome, and the second click 503'd into `refreshIndexState`, which re-attached
to the run already in flight.

`POST /api/obsidian/index` returned 200 before the state transition, which lived
on the spawned daemon thread (after an `_emit` that writes to the log file).
`vault.js` polls `/api/obsidian/status` with **zero delay** on that 200 — every
later poll is on a 2 s timer, so the first is the only racy one — and it lost
reliably, because `get_status`'s lazy recovery materialises `"done"` into
`_index_state` (the prewarm poller does that at launch), so the poll took the
early-return fast path with no disk I/O at all. Beyond the false "complete", the
same window served a **stale `error_code`/`error_detail`** against the new run
and hid Pause/Cancel for the ~40 s the store takes to open.

The route now calls `obsidian_manager.begin_index_run(op_epoch)` between
`try_acquire_lock` and `t.start()`, so the status is non-terminal for every
client (GUI, CLI, MCP) before the 200 is written. `index_vault` skips its own
identical preamble for that epoch — not a tidy-up: re-running it would
`_stop_event.clear()` away a Cancel landing in the admission gap, and since the
route also publishes `_index_op_epoch`, such a cancel now releases *this*
acquisition rather than a stale one. `op_epoch=0` (direct callers) can never
latch, so their path is byte-identical. `abort_index_run` reverts the admission
if the thread never starts.

Client-side, a matching **run-started latch**: a terminal state seen before any
running state is treated as the previous run's leftover and repolled at 400 ms,
bounded at 5 attempts so a run that really did fail before the first poll still
terminates within ~2 s.

Pinned by `smoke_test.py::test_30d` (state is `scanning` at `t.start()`, and a
real status GET after the POST reports the run with the stale error record
cleared), `test_vault_regressions.py::TestIndexRunAdmission` (admission,
epoch-guarded abort, the preamble skip preserving a gap Cancel, direct-call
parity) and `tests/js/vaultFirstPoll.test.js` (5 cases). All four were verified
to fail against the pre-fix code.

## 2026-08-12b (review of the repatriation range: Zotero join + coverage)

A deep review of `6357b65..81ab37d` refuted its three flagged risks (`rec=None`
deref, `resolve_biblio_pdf_rel` traversal, the xattr sweep's iCloud cost — all
sound; the sweep is pure `getxattr(2)` at 0.21 ms/file, and the tree has zero
dataless files) and found one write-affecting bug plus nine smaller issues.

**The Zotero join was by title, and it was wrong.** `_master.bib` ↔ Zotero
matched on normalized title, first-occurrence-wins, although Better BibTeX
*derives* the bib key from the item's own `citationKey` field. Measured: 23
Zotero items claimed by 2+ bib keys, 51 records affected — four different
papers titled "Schizophrenia" (Owen 2016, Marder 2019, Leucht 2025, Jauhar
2022) all resolving to one item. `apply-zotero-tags` writes to
`rec.zotero_item.item_key`, so three of those four tagged the wrong paper.
Joining on the key instead: **all 6122 records join, none fall through to
title, 553 previously-unjoined records gain their item and 31 mis-joins are
corrected**. `scripts/vault_maintain/audit_zotero_tag_writes.py` (read-only)
re-derives the correct item for every journalled Zotero op — exact, since each
op records both the citation key and the item key it wrote to — and finds **10
mis-directed writes** across 1937 ops.

**Two features that had shipped server-side but never reached the screen.**
`unmapped_rows` (Tag Hygiene's PDFs-with-no-citation-key bucket) was computed
on every GET and discarded by the client, so the on-screen "outside the
vocabulary" figure stayed the floor-presented-as-total that work was written to
remove — while this file and CLAUDE.md recorded it CLOSED. Likewise `?scope=all`
was never requested by the UI, leaving all 2081 PDF-less records invisible
however writable they were. Both now render, with a toolbar toggle for the
latter; `retire-tag` was blind the same way on the write side and now reaches
those PDFs through the existing path-addressed writer.

**Reconciliation, measured in-process (read-only).** PDFs close exactly: 5765
total − 353 skip-prefix = 5412 active = 4159 mapped + 968 unmapped + 269
ambiguous + 16 confirmed-no-match, zero overlap, zero unaccounted; 4041 records
hold 4159 PDFs because 100 have more than one. Notes: 1447 `.md`, 1232 under
`Z_Zotero_Notes/`, 215 thematic notes outside the audit **by design**, 1229
records joined, 3 orphans (a template plus two stale BBT `…a` suffixes).
Zotero: the apparent "72-item gap" was a net of 653 unjoined items and 553
bib-only records — after the key join the residual is a clean 72 items carrying
a citationKey no bib entry uses.

Also: the propose prompt is pinned by a golden string (the old test compared
`build_messages` against itself with the exemplar knob off, so an edited
literal would have moved every call together and still passed); the classifier
no longer sees its own tags as `EXISTING KEYWORDS` (19% of classifiable
PDF-less records); the tagging manifest serializes compactly (4.76 MB/143 ms →
2.91 MB/28 ms, five-fold, inside the op-lock); `build_vault_sync_app.sh --dest`
must end in `.app` before its `rm -rf`; `sync_readmes.py` refuses to `--prune`
outside the vault; the `batchtags` CLI flag pair no longer has a silently-empty
combination and the Vault Sync panel probes `/api/health` before running it.

## 2026-08-12 (vault-maintenance repatriation + tagging reaches the whole library)

**Maintenance tooling consolidated into one home.** The vault held three of the
seven `maintain.sh` step scripts and this checkout the other four, with no
stated rule. That split cost a second resolution path plus a per-step error
message, and had produced a real bug: vault commit `84fd52e` pruned
`.website-deploy` from the attachment audit's walks but the link checker — same
folder, same walk shape — never got the fix, so deploy-repo files stayed in its
target map where they could satisfy a genuinely broken link. `sync_readmes.py`,
`audit_attachments.py` and `check_vault_links.py` moved into
`scripts/vault_maintain/`, rewired onto `vault_paths.py`; the prune set is now
one shared `vault_paths.PRUNE_DIRS` so the two walks cannot diverge again. The
vault keeps only `maintain.sh`.

- **The link checker's docstring became true.** It claimed to check "internal
  wikilinks across vault" while walking `medecine_sciences/` only — 134 of 1447
  notes, 9%. Merged with the vault-wide audit written during the 2026-08-11
  documentation pass: Obsidian-shaped resolution (full rel path, then basename,
  each ± `.md`), markdown links as well as wikilinks, and code stripped first so
  an `![](image.png)` *example* in documentation is not counted as breakage.
  703 → 4513 references checked, still 0 broken. The index-completeness check is
  unchanged.
- `sync_readmes.py` wrote its mirrors to `Path(__file__).parent` — correct while
  it lived *in* the mirror folder, silently wrong once moved. The destination is
  resolved now and must pre-exist. Its entry point was also discarding
  `main()`'s return code, so a setup error exited 0.

**Publish flow: STRIP STEP 3.** `scripts/vault_maintain/` (and the now-deleted
`scripts/batch_tagging/`) are withheld from the public mirror. Three tree
references would otherwise have dangled there, one fatally — `smoke_test.py`
imports `check_tex` and would have shipped a suite that fails at import; it now
skips when the directory is absent. Also scrubbed three personal absolute-path
literals (macOS username, full iCloud vault path, vault name) that had been
reintroduced in `orphan_tagger.py` the day after an audit certified the tree
clean of exactly those.

**Tagging now reaches the entries it never could.** Three boundaries fell, all
of them the same boundary: the subsystem addressed everything *by citation key*
via the audit's `Record` join, so anything outside that join was invisible even
where it was perfectly writable.

- **Path-addressed removal.** `remove-tags` accepts `pdf_rel` as an alternative
  to `citation_key` (never both). A biblio PDF the bridge never mapped has no
  `Record` and so could not be named at all. `resolve_biblio_pdf_rel` is the one
  place in the subsystem where a filesystem path comes from the client and
  carries `/api/tagging/pdf`'s full discipline, with containment narrowed to the
  biblio dir — a write is a stronger reason to be narrow than a read. The
  journalled op keeps its shape, so the revert needed no change.
- **Tag Hygiene stopped under-reporting.** It iterated `inv.records.values()`,
  so unmapped PDFs contributed none of their tags: a direct sweep of all 5765
  biblio PDFs found 5 distinct out-of-vocabulary Finder tags where the report
  showed 2. Now a separate `unmapped_rows` bucket with its own
  `--max-hygiene-unmapped` bar — separate, not folded in, so `--max-hygiene`
  keeps the meaning it has had since the runner shipped. The five tags were then
  stripped through the new address form (8 PDFs, journalled, reversible); both
  dimensions read 0 and `maintain.sh` passes all five default steps.
- **`ZoteroItem.abstract`** read from Zotero SQLite (`_master.bib` has none;
  BBT does not export one), and **metadata-mode propose**: no PDF, or a PDF with
  no usable text, now classifies from abstract + bibliographic fields instead of
  answering `no_text`. The prompt's source line is parametrised so the PDF path
  stays byte-identical and metadata is labelled as metadata — the system prompt
  tells the model to return `[]` when the text is "too thin to tell", so
  mislabelling would push it toward refusing the case the path exists for.
- **Inventory `?scope=all`** surfaces PDF-less records (`has_pdf` on every row).
  Opt-in, because it multiplies the row count. `tagging.js` degrades the Finder
  column, its verb, the PDF panel and the snippet; `_taggedScore` became a
  *fraction of the targets a row has*, since as a count it ranked by how many
  targets exist as much as by how tagged they are.

**`papermind tagging batch`** replaces `scripts/batch_tagging/`, deleted. The old
script wrote frontmatter and Finder xattrs directly — vocabulary-gated, but with
no journal, no `log_vault_write` trail, no atomic write, no op-lock and no
confirm beyond `--dry-run`. Every batch write now goes through the app's
confirm-gated, journalled, reversible endpoints. Resumable by construction;
`maintain.sh`'s `batchtags` step therefore requires the running app and says so
when it is absent.

**`Vault Sync.app` is reproducible.** The launcher bundle was a hand-compiled
artifact on the Desktop, in neither repo, with nothing recording its source.
`scripts/vault_maintain/build_vault_sync_app.sh` now compiles, ad-hoc signs and
verifies it into `~/Applications`; the bundle is gitignored. It builds through a
temp dir and swaps, because `osacompile` writes *into* an existing bundle rather
than replacing it — recompiling over a live app can leave stale files beside a
signature that no longer matches.

Measured on the live library, correcting an earlier estimate: **2081** records
have no local PDF (not ~4100 — that figure came from a bib-keywords proxy
measuring a different set), and **1155 of them (55%)** carry enough metadata to
classify.

## 2026-08-11 (vault-maintenance: library audit step + GUI input fix)

- **`scripts/vault_maintain/audit_library.py`** — new pass/fail runner over the
  audit engine, wired into the vault's `maintain.sh` as the `library` step
  (default on, ~8.5–9 s over 6 119 records / 5 762 PDFs). Four dimensions, three
  of them sharing a **single** `build_inventory` call: `note_tag_drift`
  (actionable = tags missing
  from the note or spelled two ways; the note-only direction is context, since
  `batchtags` produces it by design), `zotero_no_pdf` (a ceiling at 2 200 —
  ~2 080 entries have no local PDF on purpose — not a demand for zero),
  `duplicates_biblio`, and `tag_hygiene` (which had **no CLI at all**, only
  `GET /api/audit/reports/tag_hygiene`). Counts print even when passing.
  Exists because every `check_*` in `audit/scan.py` returns 0 unconditionally,
  so wiring `python -m audit --check` in would give a permanently green step;
  `scan.py` keeps its contract untouched. A failed Zotero read or an unreadable
  `_tags.md` is reported as the finding it is, never as a clean zero.
- **`maintain_gui.py`** — a non-numeric `jobs` / `md limit` / `pdf limit` was
  dropped silently (`if jobs.isdigit()`), so a typo fell back to the checker's
  default with no signal. It now logs a warning naming the field and the
  rejected value; the log is cleared *before* the command is built so the
  warning survives.

## 2026-08-10 (plan trees deleted)

- **`docs/` removed from the repo** (maintainer request): both plan trees
  (`docs/fix_plans_2026-07-30/`, `docs/feature_plans_2026-08-05/`) were
  shipped work whose every still-open item is mirrored in CLAUDE.md Known
  Issues (verified by the 2026-08-09 Phase-2 sweep). Recover from git history
  if needed (last commit carrying them: `5991666`). CLAUDE.md's "Active
  plan set" pointer replaced with the plans-deleted-after-shipping rule;
  the PUBLISHING.md strip step is moot while `docs/` stays empty (its
  re-check-before-publish rule remains).

## 2026-08-09 (Phase 2 consolidation — docs truth pass + UI/UX pass)

An 8-agent doc-against-code verification sweep (root + subtree `CLAUDE.md`s,
README/PUBLISHING/guide/help, Known Issues registry) plus a static+dynamic
UI/UX pass. No new features.

### docs (lot 1)

- **~60 doc-follows-code corrections** across `CLAUDE.md` (root),
  `rag/CLAUDE.md`, `core/llm/CLAUDE.md`, `core/agent/CLAUDE.md`,
  `audit/CLAUDE.md`, README, `PUBLISHING.md`, `static/help/guide.md`,
  `.env.example`, `deckgen/README.md`, `docgen/README.md`,
  `packaging/widgets/README.md`, `tests/eval/README.md`. Highlights: the
  audit *scan* DOES write `mapping.json`'s `hashes` re-anchor (the "scan
  writes nothing" invariant was overstated; the annotate-then-move
  limitation is correspondingly narrower), the SSE-skeleton consumer list
  ("five routes") became "every SSE route" (15 call sites / 7 modules),
  the OpenRouter sweep was finished in the 8 places it missed (README ×6,
  guide, `.env.example`), FR/EN → FR/DE/EN where the trilingual pass had
  missed, stale symbol names (`TestUnzoterodReportsUseCache`,
  `_iter_vault_scan_files`, `_cached_lancedb_count`, `get_lmstudio_client`
  in the vision bullet), and the dev `py_compile` gate now matches CI
  (`api/*.py` + the three `scripts/` entry points). `docs/` strip step in
  `PUBLISHING.md` now covers the whole directory (a second tracked plan
  tree, `docs/feature_plans_2026-08-05/`, would have leaked under the old
  per-directory command).
- **Known Issues registry consolidated**: every 2026-08-08 entry re-verified
  still open (nothing struck in error); promotions per the single-registry
  rule — Ollama think-suppression residuals (query-expansion LLM + the
  vision/OCR path) out from under a struck CLOSED heading, the Übersicht
  `.jsx` no-CI-gate note, the Anthropic 1-h cache-write pricing tier
  (inert), the agent-tool PDF-extraction interruptibility residual folded
  into the wedge entry; the deck/docgen stall-guard entry retitled
  (plain-chat's backend floor IS online-aware — the residual there is
  frontend-only); two former "leads" adjudicated (below).
- **Hardcoded audit report counts eradicated** (the banned 9-vs-11 drift
  class): `audit/search.py` docstring, `audit/__init__.py`, `audit.js`,
  two `templates/index.html` comments — all now countless phrasings.
- Stale code comments fixed where the doc was right and the comment wrong:
  `rag/build.py` (fresh-build default is `simple`), `rag/engine.py` (the
  cutoff is skipped under fusion, not rescaled), `core/agent/loop.py`
  (capability state lives in the route singleton), `core/llm/adapters/local.py`
  (LM Studio uses the shared cached client), `.github/workflows/ci.yml`
  (its own superset comment).

### fixes (lot 1, found by the doc pass)

- **`UsageTracker._disk_records` lost the whole on-disk history right after
  a rotation** (confirmed 2026-08-08 lead): the live log does not exist
  until the next append, and the stat failure returned `[]` even though
  `<log>.1` held every record. Now falls through to the rotated generation.
  Pinned by `test_llm.py::TestUsageLogRotation::test_disk_records_survive_the_rotation_window`.
- **`audit_manager.clear_results()` kept `_near_duplicates`**, so
  `has_near_duplicates` stayed true and the report could serve a stale run
  against a cleared inventory (`reset_to_idle` already cleared all three).
  Pinned by `test_audit.py::test_clear_results_drops_near_duplicates_too`.
- `api/routes/audit.py::_SEARCH_CACHE` type annotation matches the 4-tuple.

### ui (lot 2)

- **Empty states**: every audit report table now renders a visible
  "No rows in this report for the last scan." row instead of a bare header
  (`audit.js::_tbodyHtml`, tagging-table parity).
- **Ceremony symmetry**: the near-duplicate `[restore]`/`[undo]` (undo of a
  dismissal) is now `confirmInline`-gated like its `[not a duplicate]`
  sibling and every other undo in the app — it was the lone unconfirmed
  write verb in the report. Ratcheted in `confirmCeremony.test.js`.
- **Screen-reader announcements** (the minted-role-at-write-time defect
  class): plain chat's `{info}` notices now announce through the static
  region (parity with vault chat), refactor's LLM-action stage lines
  (`_pending`) announce (previously only failures were audible), and the
  Single Paper drop-zone error announces via `announceError`. `liveRegions`
  ratchets extended to plainchat + refactor, and the 11 remaining static
  `role="status"` regions in `index.html` are now pinned (removing any role
  used to pass the suite).
- **Wording**: "Summarize/Summarizing" → "Summarise/Summarising" in the
  refactor LLM-actions panel + Settings hint + README (BrE is the app's
  dominant existing spelling — `/api/summarise`, the Single Paper tab).
- Help: the six `docgen-*` generation knobs got "?" entries (clones of
  their deck twins), `vault-cutoff`'s tip notes it is inert while the
  reranker is on, the tour's closing step no longer claims badges "on
  every control", and the triage-deck key lists in README document `V`.

## 2026-08-08 (OpenRouter chat provider + Cursor MCP registration)

### OpenRouter provider

- **New online provider `openrouter`** (`core/llm/adapters/openai_compat.py::OpenRouterProvider`) —
  the third `_OpenAICompatProvider` subclass (DeepSeek/Z.AI pattern: `name`,
  `OPENROUTER_API_KEY`, base `https://openrouter.ai/api/v1` with an
  `OPENROUTER_BASE_URL` override, curated list). One key, ~400 upstream models,
  namespaced ids (`vendor/model[:variant]`). Curated set = 10 ids, each
  verified **tools-capable** against OpenRouter's public `GET /api/v1/models`
  on 2026-08-08; default `anthropic/claude-haiku-4.5` (the app's own Anthropic
  default tier, through the aggregator). Design decisions recorded in
  `core/llm/CLAUDE.md`: static curated pricing (live-priced lookup rejected —
  `estimate_cost_usd` is pure/synchronous and runs in streaming `finally`
  blocks), attribution headers (`HTTP-Referer`/`X-Title`) deliberately not
  sent, OpenRouter's internal upstream routing composes with (does not
  replace) the app-level `fallback_provider` policy, and a live-listed
  no-tools model degrades to a plain answer via the capability-routing heal
  (added by the same-day post-review fix below — the entry as first written
  claimed the reactive tools-heal covered this, which assumed a 400 OpenRouter
  never sends).
- **All registration points wired** (the C4/2026-07-13 checklist):
  `core/llm/factory.py::ONLINE_PROVIDER_NAMES` + factory branch,
  `core/config.py` (`_ONLINE_PROVIDER_NAMES`, `_ONLINE_MODEL_KEYS`,
  `_ONLINE_DEFAULT_MODELS`, `_DEFAULTS["openrouter_model"]`),
  `core/constants.py::DEFAULT_OPENROUTER_MODEL`,
  `api/routes/config.py` (`_PROVIDER_NAMES` + `openrouter_model` shape
  validator), `core/llm/usage.py` (10 `PRICING_TABLE` rows transcribed from
  the live catalog — the `anthropic/claude-sonnet-5` row is OpenRouter's own
  $2/$10 intro rate — + the `unpriced_curated_models` loop), the two
  `templates/index.html` `<option>`s, `config.js::_ONLINE_MODEL_KEYS`, and
  `ui.js::_ONLINE_PROVIDERS`/`_PROVIDER_LABELS`.
- **Namespaced-id pricing guard** (`core/llm/usage.py::estimate_cost_usd`): the
  split-on-colon base-id fallback (written for ollama `name:tag` ids) now
  applies only to un-namespaced ids. An OpenRouter `:variant` carries its own
  price, distinct from its base (typically cheaper — `:free` is $0), so the old
  fallback would have mis-reported an unpriced variant at base rate; a
  namespaced miss now costs out at the documented unknown-model $0
  (under-report, the house rule). Curated `:variant` ids get exact rows
  (`nvidia/nemotron-3-super-120b-a12b:free` at $0).
- **Registration ratchets, both sides** (the test that was missing in
  2026-07-13): `test_llm.py::test_online_provider_registries_agree` pins the
  four server-side registries against each other, and the new
  `tests/js/providerRegistry.test.js` pins the `index.html` online optgroup ↔
  `config.js::_ONLINE_MODEL_KEYS` ↔ `ui.js::_ONLINE_PROVIDERS`/`_PROVIDER_LABELS`
  parity (plus the fallback selector), so a future provider missing point 8
  fails the suite instead of shipping as a LOCAL-looking online provider.
- **Test parity in the existing classes** (`test_llm.py`):
  `test_compat_providers_registered_everywhere` +
  `test_online_model_keys_are_shape_validated` (namespaced + `:variant` ids
  round-trip the validator) extended to `openrouter`; new
  `test_openrouter_provider_contract` (env-only key naming
  `OPENROUTER_API_KEY` in the AUTH error, base-URL override, legacy
  `max_tokens` + tools kept) and
  `test_namespaced_variant_ids_never_fall_back_to_base_pricing`;
  `TestRedaction::test_redacts_openrouter_key` pins that `sk-or-v1-…` is
  covered by the generic `sk-` pattern (no new pattern needed).
- **Docs**: `.env.example` + README (provider list, setup, per-provider model
  keys), `CLAUDE.md` §Provider Rules (registration checklist now names the
  validator/default/constants points and the two ratchet tests),
  `core/llm/CLAUDE.md` (OpenRouter block), and the online-provider
  enumerations in `scripts/gen_golden_retrieval.py`'s disclosure docstrings.

### Post-review fixes (same day — adversarial review of this lot)

- **Capability-routing heal (M1, the review's one behavioural finding):**
  OpenRouter signals "no upstream endpoint supports this request shape" (e.g.
  tools sent to a model with no tool-capable endpoint) as a **503** "There is
  no available model provider that meets your routing requirements" (or a 404
  "No endpoints found…") — never the param-named 400 the inherited heal
  understands — so a live-listed no-tools model in agent/deck/docgen mode
  burned every `online_max_retries` attempt as a retryable SERVER_ERROR and
  then triggered app-level provider fallback (a *different* model silently
  answering). `OpenAIProvider._create_with_heal` now consults a subclass
  predicate (`_is_capability_routing_error`, base = False; overridden ONLY by
  `OpenRouterProvider`, 404/503 + routing phrasings) and strips
  `tools`/`tool_choice` for one immediate retry — the same plain-answer
  degradation as the tools-400 heal, bounded by the same `healed` set, and
  gated on tools being in the payload so genuine transient 5xx keep their
  retry semantics. Pinned by
  `test_openrouter_tools_routing_503_heals_to_plain_answer` +
  `test_routing_heal_is_openrouter_scoped_and_phrase_gated`.
- **Ratchet strengthening (B1/B3):** `test_online_provider_registries_agree`
  now also asserts every `ONLINE_MODEL_KEYS` value has a `_CONFIG_VALIDATORS`
  row (an unmapped `*_model` key passes through `_validate_llm_config_keys`
  unvalidated and persists garbage as-is), and
  `test_online_model_keys_are_shape_validated` derives its key list from the
  registry instead of a hardcoded tuple; `providerRegistry.test.js`'s entry
  regexes are digit-safe (`[a-z0-9_]`) and attribute-safe
  (`<option value="…"[^>]*>`) — with the old classes a provider id carrying a
  digit vanished from every extracted set consistently, passing parity while
  silently dropping out of ratchet coverage.
- **Cursor registration IO (B4):** `register_cursor` resolves the config path
  through `os.path.realpath` so a dotfiles-managed symlinked
  `~/.cursor/mcp.json` is written THROUGH (the old `os.replace` on the link
  path swapped the link itself for a regular file), and a failed write cleans
  up its `.tmp`. Pinned by `test_register_writes_through_symlink`.
- **Doc truth (B5):** the stale "both endpoints/adapters" wording in
  `core/llm/CLAUDE.md` + the `openai_compat.py` docstring (three compat
  providers now), and the ":variant always cheaper" overclaim softened to "own
  price, distinct from its base" here and in `usage.py`/the pricing test.
- **Deferred, recorded in Known Issues (B2):** `_NON_CHAT_OPENAI_TOKENS`
  substring-filters OpenRouter's ~400-id live catalog with a denylist tuned
  for api.openai.com — the `codex` token drops chat/tools-capable
  `openai/gpt-5*-codex` ids from the live merge (curated ids unaffected).

### Cursor MCP registration

- **`scripts/install_mcp.py` now registers the stdio MCP server with Cursor**
  (`register_cursor()`): Cursor has no registration CLI, so the script merges a
  `papermind` entry into `~/.cursor/mcp.json` — conservative IO: other servers
  and unrelated keys preserved, a stale entry (moved repo/venv) refreshed, and
  an unparseable existing file NEVER overwritten (the snippet is printed for a
  manual merge instead). The shared `{command, args, env}` entry was extracted
  into `server_entry()` and reused by the OpenClaw snippet; Claude Code
  registration (`claude mcp add`) unchanged. Pinned by
  `tests/test_mcp_server.py::TestInstallMcpCursorRegistration` (merge purity,
  parent-dir creation, existing-config merge, unparseable/non-object refusal).

## 2026-08-08 (consolidation: correctness fixes, dead code, UI, docs)

A five-strand consolidation pass — no new features. Volets A/B (review +
flawed-logic) ran as a 12-seam multi-agent review weighted toward the SEAMS
between subsystems, with every finding put through two independent adversarial
lenses (refutation + concrete-reproduction) before anything was touched. 22
findings survived both lenses; the six below were fixed here and the rest are
recorded in `CLAUDE.md` → *Deferred from the 2026-08-08 consolidation audit*,
along with ten leads whose verifiers were cut off by a session limit.

### Correctness

- **Batch tagging-restore crashed before reverting anything** (`api/routes/tagging.py`).
  Phase 1 called the SSE skeleton's `threading.Event` as `cancel()` — the only
  such call in the file, every other worker uses `.is_set()`. That line is
  reached only when the batch contains a Zotero op, so the note+vocab
  round-trip test never touched it; with one present it raised `TypeError`,
  `_runner`'s blanket handler turned it into an `{"error"}` frame BEFORE the
  op-lock was taken, and a retire that had already mutated notes, Finder tags
  and Zotero items reverted **zero** ops — HTTP 200 throughout, one-click undo
  silently unavailable. New regression test builds a Zotero-carrying batch.
- **`POST /api/obsidian/cancel` stole the op-lock from other vault writers**
  (`rag/vault.py`). The route is ungated (no index-state check, no holder
  check) and the Cancel button stays visible after a pause, so a Cancel pressed
  during a Note Refactor Apply / tagging write / docgen note write
  `force_release()`d that holder's lock and admitted a SECOND concurrent
  writer; each loads its own copy of the restore manifest, so the last
  `journal.save` erased the other's op records — notes already rewritten, no
  longer restorable. `cancel_indexing` now records the indexing run's epoch and
  releases only that acquisition. Nothing leaks: the indexer's own `finally`
  releases when the aborted run unwinds, and `force_release` stays the
  operator escape hatch (`/api/reset`). Two regression tests.
- **The tagging journal evicted the op the same request had just written**
  (`tagging/journal.py`). Applied finder/zotero ops are deliberately never
  evictable, and the triage deck journals two per Accept, so after ~100 accepts
  the manifest is entirely protected ops and the next write's `note_tag` op is
  the only evictable record — the cap loop dropped it and unlinked its snapshot
  *before* the note was overwritten, while the response still returned its
  `op_id`. New `protect_op_ids` exemption (the `protect_batch_id` idiom),
  passed by all six writer sites and both batch prunes.
- **`fallback_on: []` silently re-enabled provider fallback** (`core/llm/policy.py`).
  The Settings window sends `[]` when every category is unchecked and
  `_coerce_fallback_on` deliberately PERSISTS it as "fall back on nothing" —
  but the reader's truthiness test turned it back into the full default set.
  Absent-key and all-garbage-list still resolve to the defaults.
- **Four path validators CWD-anchored a relative input** (`api/routes/_latexdoc.py`,
  `api/routes/deck.py`). The P8.2 fix had reached `deck._resolve_out_dir`
  alone; its three siblings and the two docgen twins still ran `realpath()`
  on a relative path, resolving it against the SERVER's process CWD. All six
  now share the pre-`realpath` `isabs` guard. **The ratchet was the reason this
  survived**: `test_relative_input_is_rejected` passed for the
  existence-checking validators only because nothing of that name happened to
  sit in the CWD — it now chdirs into the fixture dir so the relative names
  resolve to real files, and covers the two missing `_latexdoc` twins.
- **A non-string `provider` in `POST /api/config` 500'd** (`api/routes/config.py`)
  where every other malformed value is coerced or dropped.

### Dead code

Removed after proving zero references (grep + suite): `rag/vault.py::
_legacy_pdf_signatures_path` — a back-compat shim whose docstring justified it
by "external callers/tests" that do not exist (the live merge path is the
plural `_legacy_pdf_signature_paths`) — and `refactor/sections.py::
section_sha256` + its now-unused `hashlib` import, a section-level drift
tripwire superseded by the whole-note `content_sha256` guards. Everything else
vulture flagged is a structural false positive (Flask/click decorators, mixin
methods, documented test patch targets, LlamaIndex overrides) and stays. The
config-key and `HELP_ENTRIES` sweeps came back clean: 127/127 keys have a code
reader, 40/40 help entries have a DOM element.

### UI

- **Prompt Hub clipped 16px of every row and the tail of its intro line**
  (`static/css/styles.css`). `.content-area` is `flex-direction: row`, so the
  `flex: 0 0 auto` override — added for VERTICAL growth — released the card on
  the HORIZONTAL axis instead; it then sized to content and spilled past the
  panel, where `overflow: hidden` clipped it with no scrollbar to reach it.
  `#prompts-tab` was in that selector by mistake: unlike `#audit-tab` /
  `#refactor-tab` its card has no `.pane-view` wrapper to constrain the width,
  and it already scrolls internally via `.prompts-list`.
- **`.btn-link` used the non-text-grade accent** — `--accent`, which the token
  comment reserves for fills/outlines and the contrast lint holds only to 3:1.
  Measured 3.57:1 as 12px text on the dark card (AA wants 4.5). Now
  `--accent-hover`, the text-grade token, which is also darker in light mode.
  The contrast lint's existing pair now names `.btn-link`.

A full accessible-contrast sweep of every tab plus the Settings modal, in both
themes with proper alpha compositing, found no other failure.

### Docs

`CLAUDE.md`: dropped the hardcoded "nine reports" from the `audit/search.py`
bullet (the corpus covers 12 groups, and the file's own rule forbids hardcoding
that count); `rag/vault.py` re-measured (5,131, not 5,143); the mypy note
re-baselined from "~60" to the measured 179, with the finding that 96 of them
are the Plan 9 mixin `[attr-defined]` tax rather than debt. Verified mechanically
and found accurate: every documented constant, every referenced file path, and
the quick-gate ⊆ CI-superset invariant (57 test modules, all covered).

## 2026-08-07 (prompt-writing guide + trilingual FR/DE/EN source prompts)

Two coupled changes: the Help tab gains a **"Writing good prompts"** section,
and every default prompt that describes the vault's sources now names all
THREE languages — French, **German** and English (the old defaults assumed a
bilingual FR/EN corpus, so models routinely discarded German passages).

- **Help guide (`static/help/guide.md`).** New "Writing good prompts" section:
  cross-workflow prompt-engineering rules (scope specificity + question
  decomposition, naming the expected languages of answer AND sources — with
  the cross-lingual retrieval mechanic made concrete: put the key term in the
  target source's language, since dense retrieval aligns same-language text
  more strongly and BM25 needs the exact token (multilingual-RAG literature,
  e.g. arXiv 2502.11175); answer-mode selection (strict/balanced/exploratory/
  concise) as a first-class prompting lever; show-the-format few-shot
  micro-examples; short positively-phrased system prompts under the
  4000-char prefix cap; focus question over system-prompt stuffing; honest
  "retrieval is not a full read" framing; iterate-not-mega-prompt) plus
  concrete FR examples per workflow — vault chat single-shot vs agent, the optional knobs
  (thesaurus/wikilink/web search) with the honest FR/EN-only thesaurus limit
  stated, Plain Chat, Single Paper focus questions, deck/article
  instructions, and Note Refactor free-prompt edits. Chrome stays English,
  examples are vault-domain French (2026-07-22 language policy). The
  reranker upgrade note and its `help.js` "?" entry now say FR/DE/EN.
- **Trilingual prompt defaults.** `rag/engine.py` — a single shared
  `_MULTILINGUAL_RULE` constant (one constant, four modes, so the wording
  can't drift) added to all four vault-chat answer modes: FR/DE/EN passages
  are equally valid evidence, never discard by language, answer in the
  question's language. `core/agent/loop.py::_AGENT_PREAMBLE` — same rule
  plus a cross-language retry nudge (an empty search should be retried with
  the terms in another of the three languages). `rag/thesaurus.py::
  _PRIMER_HEADER` — "bilingual French/English" → "trilingual
  French/German/English"; **no config migration needed**: the persisted
  `vault_primer_header` default is `""` (= use the built-in), so existing
  installs pick the new header up automatically; only a user-customised
  header keeps its own text, which is the intent. `deckgen/prompts.py`,
  `docgen/prompts.py`, `docgen/markdown.py` — outline/section/augment
  prompts told to use vault sources in all three languages and to write the
  output in the language of the user's topic/instructions (augment: keep
  the document's existing language). `tagging/propose.py` — the paper-text
  wrapper now says FR/DE/EN instead of "bilingual FR-EN" (byte-identity of
  the knobs-off prompt shape is preserved — the change is inside the single
  static string all variants share). `chat_system_prompt` (Plain Chat) is
  deliberately untouched: it is RAG-free, no vault sources to describe.
- **Example chips (`app.js::wireExamplePrompts`).** One new trilingual chip
  each for the vault-chat system prompt ("Sources trilingues") and the deck
  instructions ("Sources FR/DE/EN").
- **Tests.** `test_prompts.py` gains `TestMultilingualSourceRules` — the
  deliberate-update record pinning that every vault-describing default
  prompt names all three languages (RAG modes share the one constant, agent
  preamble, primer header, all eight generator prompts, tagging propose).

## 2026-08-07 (review fixes — widget same-day stability, refresh 409/500, SwiftBar/Übersicht input hardening)

Code review of the 2026-08-05/06 commits (three parallel review passes +
manual audit of the tagging write path). No HIGH findings; the tagging/vocab
bridge, near-duplicates bounds, hash-memo pairing and textclean regexes all
verified clean. Fixed:

- **BUG (widget): same-day refresh advanced the paper.** The pick was
  appended to its own anti-repeat history, so the next same-day rebuild
  *excluded* it from the pool and the stable date seed drew a different
  paper on every refresh/scan — "paper of the day" degraded to "paper of
  the click". `api/routes/widget.py::_build_and_write` now filters the
  previous same-day pick back out of the history it passes to
  `build_payload` when using the stable seed (same pool → same seed → same
  pick; the append guard then no-ops). A `redraw` keeps the full history —
  a new draw is the point. Pinned by
  `test_widget.py::test_same_day_refresh_is_stable`.
- **BUG (widget): a failed build answered "Run a scan first".**
  `_build_and_write` returns `None` both for "no cached scan" and "build
  crashed"; the refresh route mapped both to the 409. It now re-checks
  `get_inventory()` and answers **500** ("see chatekld.log") when a scan IS
  cached — telling a user with a full disk to re-run a multi-minute scan
  was actively wrong advice. Also: `scan_finished_at` ships `null` instead
  of `0.0` when a refresh lands mid-scan (`start_scan` resets
  `_finished_at` to the epoch, and widgets do age arithmetic on it), and
  `widget/store.py`'s temp-sibling cleanup catches non-`OSError` failures
  (a `TypeError` from `json.dump` no longer strands the `.widget-*` file).
- **HARDENING (SwiftBar plugin): payload/server.json values reached menu
  lines raw.** Defence in depth — `BASE_DIR` is 0700, so every writer
  already runs as the user — but two shapes converted a file write into
  code-execution-on-click and were cheap to close. `chatekld.1d.py` now:
  validates `server.json` like `core/server_info.py` does (version, `int`
  port in range, **loopback host only** — a string port injected curl
  arguments into the refresh action's `param6=`, and a foreign host both
  carried the app header off-box and let the returned payload chain into
  the `href` hole); validates every `href=` via `safe_href` (scheme
  allowlist, no whitespace/quotes/controls — a space-bearing `note_uri`
  terminated the value and everything after became SwiftBar params,
  `bash=` included); collapses ALL whitespace forms in `esc()` (`\r`/`\v`/
  `\f`/U+2028 could fabricate a new menu line); caps the live GET read at
  1 MB behind a no-redirect opener (`web_tools.py` discipline); and
  type-guards the payload slots so a wrong-typed `paper`/`suggestions`/
  `notion` degrades instead of raising a plugin-error card. New ratchet
  `test_widget.py::TestSwiftBarPluginHardening` (the plugin previously had
  zero coverage of these helpers).
- **HARDENING (Übersicht card): `openUri` opened any payload string.**
  `shq()` was verified sound (no command injection), but the scheme was
  payload data: clicks now only `open` `obsidian://` links and fall back to
  opening the app otherwise; the `notion` slot requires `.tag` (no more
  bare "# — " row on `{}`), and `paper`/`suggestions` are type-guarded like
  the plugin's.
- **HARDENING (near-duplicates): `_load_fuzz` caught only `ImportError`.**
  A broken/ABI-mismatched rapidfuzz wheel raising `OSError`/`RuntimeError`
  at import would have crashed the scan phase; it now degrades to the
  deterministic tiers with `similarity_available:false`, as designed.

Known accepted residuals from the review (all LOW, deliberate):
`looks_like_latex` can trip on a backslash-bearing PDF excerpt (display-only,
rare in this corpus); `_save` in `dup_decisions.py` truncates past 10 000
ids; the sentinel `U+E000` could collide with PUA glyphs in extracted prose.

## 2026-08-06 (widget polish — de-LaTeXed payload, redesigned card, working actions)

Post-QA pass over the two desktop widgets, from a visual review of the first
live snapshot.

- **BUG: bibliographic LaTeX reached the widgets raw.** Both surfaces printed
  `Approches computationnelles de la schizophr\'enie` — BibTeX fields are
  LaTeX *source* and neither widget has a TeX renderer. New pure, stdlib-only
  **`widget/textclean.py`** (`delatex` / `looks_like_latex` / `clean_display`
  / `clean_prose`) resolves accent commands via combining marks + NFC (so
  every base letter is covered, not a 200-row table), the special letters
  (`\o`, `\ss`, `\ae`, `\H{o}`…), escaped punctuation, inline math
  delimiters, `\emph{…}`-style wrappers, dash/quote ligatures and protective
  braces; an unknown macro loses its name and keeps its argument, the safe
  failure mode for a display string. TeX's own "whitespace after a control
  word is eaten" rule is implemented, so `\O stergaard` is "Østergaard".
  Applied **server-side, once**, to `paper.title` / `paper.authors` — and to
  the excerpt only when it actually contains a command (`clean_prose`), so a
  page-1 PDF excerpt's legitimate `$` and `~` survive byte-identically.
  Deliberately NOT applied to the audit's `Record`s: the fingerprint and
  every PDF↔bib join key on the raw parsed value.
- **BUG: "Open ChatEKLD" never worked.** Both widgets ran `open -a ChatEKLD`,
  but the app installs under a *dated* bundle name
  (`ChatEKLD_2026-08-06.app`), so the call failed with "Unable to find
  application named 'ChatEKLD'". Both now use `open -b com.chatekld.app` —
  the bundle id `install_and_build.sh` hard-codes, stable across builds.
- **Payload, additive within version 1** (an older widget ignores the new
  fields and keeps working, so no version bump): `paper.note_uri` (the
  `obsidian://open?path=` deep link, built app-side from the vault root so a
  widget no longer has to open `config.json`; verified live against
  Obsidian), and `paper.author_line` — the bibliographic short form
  ("Jardri", "Jardri & Dubé", "Jardri et al."), because joining raw
  `"Last, First"` entries produced "Jardri, Renaud, Dubé, François" where a
  reader cannot tell names from surnames. Excerpt clipping now cuts on a word
  boundary instead of mid-word.
- **Übersicht card redesigned** ("index card", chosen from three treatments
  reviewed at true size over light / dark / high-contrast wallpapers):
  opaque warm paper so legibility is unconditional, a ruled excerpt block,
  the citation key as a call number, 3-line clamps on title *and* excerpt so
  a long French title can no longer push the card off-screen, and the
  snapshot age demoted to a 9.5 px footer that only turns amber past 7 days.
  Deliberately **single-appearance**: an index card has no dark mode, and a
  dark variant would only be legible over half of all wallpapers.
- **Übersicht is now clickable** — `onClick` → `run()` opens the note
  (`obsidian://`) or the app. Übersicht only delivers clicks once the user
  sets an interaction shortcut *and* grants Accessibility access, so the card
  stays glanceable-first, says "click to open" in one micro-line, and the
  handlers are simply never called until then. SwiftBar remains the
  no-setup action surface.
- **Backlog counters no longer shout** — the batch line dropped its
  "(1903 untagged in total)" parenthetical (which undid the completable
  batch it followed); counts fall to 10.5 px grey with per-store colour dots
  and thin-space thousands (`4 411`), and a zero count drops out entirely.
- **SwiftBar menu polished** — typographic hierarchy (14 pt title / 11 pt
  labels / 10 pt footer), the excerpt moved from a `length=`-clipped
  one-liner into a **submenu** of wrapped lines, an "Open note in Obsidian"
  item, and a new **Refresh snapshot** action that POSTs
  `/api/widget/refresh` with the required header (rendered as an inert grey
  line when no live server is in `server.json` — it never starts a scan).
- Tests: `TestDelatex` (accents / escapes / math / wrappers / ligatures /
  unknown macros / prose gate), plus author-line, `note_uri` and
  word-boundary-clip cases in `test_widget.py`.

## 2026-08-06 (Lot 3 — the two desktop widgets + packaging)

- **`packaging/widgets/`** — the external consumers of Lot 2's snapshot:
  `chatekld.1d.py` (SwiftBar/xbar menu-bar plugin, stdlib-only python3:
  menu-bar count = the completable tag batch; dropdown = paper of the day
  with an `obsidian://` note link when `config.json` names the vault, the
  suggestion counts, the notion of the day, a staleness-coloured snapshot
  age; reads `widget.json` first and prefers a live `GET /api/widget` when
  `server.json` names a live pid — never the draining status endpoints,
  never a scan) and `chatekld-widget.jsx` (Übersicht desktop card, Card
  Catalog styling, display-only `cat` of the file, zero API calls), plus an
  install `README.md`. Both check `version` and render a "please update this
  widget" state on schema drift.
- **DMG staging**: `install_and_build.sh` copies `packaging/widgets/` into
  the image (the `seed_models.command` precedent) and the generated
  "READ ME FIRST.txt" gains a step 6 pointing at it. Not bundled into the
  `.app`. `packaging/widgets/*.py` added to both `py_compile` sweeps.

## 2026-08-06 (Lot 2 — widget snapshot: `widget.json` + `/api/widget*` + Refresh button)

- **New `widget/` subsystem** — the durable feed for the desktop widgets
  (Lot 3): `snapshot.py` builds a small JSON payload from the audit's cached
  (previously memory-only) `Inventory` — *paper of the day* (date-seeded
  weighted draw, anti-repeat history `widget_history.json` cap 14 with an
  all-in-history fallback; excerpt chain `> [!summary]` callout → cache-first
  page-1 PDF text → null), one primary **completable** suggestion
  (`tag_batch`: biblio-scoped untagged count, batch ≤ 10 — Readwise-style
  micro-dose, not a dashboard of backlog counts) + `finder_drift` /
  `note_drift` / `unread` counts, and a *notion of the day* from the
  `_tags.md` glosses. `store.py` persists `BASE_DIR/widget.json`
  (atomic, no-fsync — regenerable) with `server.json`-style schema-checked
  reads.
- **Write triggers: scan completion + manual button only. No scheduler, no
  auto-scan** — `api/routes/widget.py` registers an
  `audit_manager.register_result_listener` callback (the search-corpus
  invalidator precedent; passive at import, `TestFlaskAppBootDoesNotScan`
  untouched) that no-ops on clear/reset firings, plus
  `POST /api/widget/refresh` (409 without a cached scan; `redraw:true` forces
  a new draw) behind the audit tab's new **Refresh widget** button.
  `GET /api/widget` serves the payload for widgets that prefer freshness
  while the server runs; the snapshot deliberately survives `/api/reset`.
  Each write best-effort pokes SwiftBar (`swiftbar://refreshplugin`).
- Pinned by the new `test_widget.py` (builder determinism/anti-repeat/
  excerpt chain/scoping, store degrade + history cap, routes 404/409/round-
  trip, listener no-op + never-scans), added to the dev quick-gate AND
  `.github/workflows/ci.yml` (the both-lists rule), with `widget/*.py` in the
  `py_compile` sweeps.

## 2026-08-06 (Lot 1 — Finder Tag Drift correctable from the report)

- **`finder_tag_drift` gains per-row fix verbs, routed through the tagging
  writers** (the path `plan-03-tag-drift-coverage.md` pre-specified — no new
  audit write surface; the vendored `audit/` tree still writes nothing).
  `[→ note]` applies the `finder_only` gap via `POST /api/tagging/apply-note-tags`,
  `[→ Finder]` applies `note_only` via `apply-finder-tags`; both confirmInline-
  gated, journalled, with per-row `[undo]` over `/api/tagging/restore`
  (peek-not-pop) — strictly better recovery than `apply-tag-drift`'s `.bak`.
- **The two tag identities are bridged server-side.** The report/serializer now
  ships per-surface `vocab_ids` (`api/routes/audit.py::_vocab_id_resolver`:
  `_tags.md` ids indexed by `canonical_tag_key` with the SAME `_drift_synonyms`
  map as the drift comparison), so the UI POSTs the exact literal id the
  writers' `nfc().lower()` vocab gate accepts — `sleep quality` (Finder)
  resolves to vocab id `sleep-quality` and the row genuinely closes at the
  next scan instead of re-offering forever (the unresolvable-row-loop class).
- **Out-of-vocab surfaces are badged, with a case-by-case admit verb.** A
  surface with no vocab id renders "(not in vocab)" and is excluded from the
  direction buttons; its `[+ vocab]` chains the NEW journalled
  `POST /api/tagging/add-vocab-tag` (pure `tagging/vocab.py::add_tag_to_vocab`
  appends one `_tags.md` table row — column-count-preserving, header-only
  tables handled, `|`-in-gloss folded; duplicate ids answer `already:true`
  with no op; op kind `vocab_add`, reverted by the content-agnostic snapshot
  restore like `vocab_retire`) then the single-tag apply — one confirm, one
  undo covering both ops (apply reverted before the vocab entry). A
  multi-word surface (Finder tags like "sleep quality") is slugified
  client-side to a single-token id (spaces → `-`; the confirm names the exact
  id) because `_tags.md` ids must be single tokens — the fingerprint folds
  separators, so the hyphenated id still closes the row at the next scan.
- **The report also stops hiding spelling near-misses**: `variants`
  (`variant_pairs` reused from note_tag_drift — a variants-only row is now
  emitted) + `note_rel` ride the payload and the search corpus haystack.
- Pinned by `tests/audit/test_finder_tag_drift.py` (extended),
  `test_audit.py::test_finder_tag_drift_report_carries_fix_fields`,
  `test_tagging.py::TestAddVocabTag`/`TestAddTagToVocabPure`,
  `tests/js/auditFinderDriftApply.test.js` (new) and the widened
  `confirmCeremony.test.js` ratchet.

## 2026-08-05 (audit sensitivity + single help source — `docs/feature_plans_2026-08-05/`)

- **Plan 1 — single help source.** The sidebar `#howto-modal` — a second,
  independently-authored full guide that had drifted from `guide.md` (it said
  "eleven report tabs" where the guide said "nine"; the template renders 11) —
  is deleted. Its three exclusive fragments (Tips & limits, the fuller Reset
  wording, Pause/Cancel-resumable indexing) were ported into `guide.md`, which
  is now the app's only full guide; hardcoded report-tab counts were removed
  from help copy so the drift class can't recur. The sidebar "How to Use"
  button routes to the Help tab (`window.openHowTo` — `showTab('help')` +
  `loadHelpTab()`, the loader being lazy); the duplicate ⌘K "Open How to Use"
  action is gone ("Go to Help" covers it). The `semanticsLabels` gear-glyph
  ratchet became relative (wrapped == total) instead of a fixed floor that
  counted the deleted modal's glyphs. Pinned by `tests/js/helpButton.test.js`.
- **Plan 2 — tag drift compares by fingerprint, not raw strings.** New
  vendored-tree-local `audit/engine/normalize.py` (`clean_tag` /
  `fingerprint` / `tag_key` — the OpenRefine recipe extracted from Tag
  Hygiene, which now imports it; NFC+casefold fallback keeps non-Latin tags
  distinct). `note_tag_drift.find_drift` now reports two classes per row:
  `missing_in_obs` (absent from the note in EVERY spelling — the applyable
  set, one representative Zotero surface per fingerprint) and the additive
  `variants` (`[{zotero, note, reason}]` — present under another spelling; a hygiene
  signal the UI renders in its own column with no Apply). Zotero-side tags
  are now cleaned too (`#`-prefix / padding no longer permanent false drift).
  `apply-tag-drift`'s server intersection re-keyed on the same
  `missing_by_key` helper — closing a real defect: a case-only drift row
  passed the old `.lower()` check, no-opped in the writer's case-insensitive
  dedup ("Tags already present", no write), and reappeared on every scan,
  forever. Serializer/search/CLI carry the new field. Pinned by
  `tests/audit/test_note_tag_drift.py` (15 tests at plan close; later plans
  extend the file) +
  `test_audit.py::test_apply_tag_drift_refuses_variant_spellings`.
- **Plan 3 — tag drift coverage.** (a) `audit_drift_include_parent_tags`
  (validated bool, Audit Settings checkbox, default off; read fresh at
  request time so flipping it needs no rescan) unions the Zotero *item*'s own
  tags into the drift comparison — item-only missing tags ride
  `item_only_missing` and render badged "(item)". (b) The read-only reverse
  direction: per-row `note_only` (note tags absent from the Zotero side) —
  never actionable, Zotero writes stay in the tagging subsystem. (c) An
  FR/EN synonym tier: `_drift_synonyms` builds `{tag_key → canonical}` from
  the vault's `_tags.md` single-tag rows (multi-tag rows assert facets, not
  synonyms) and injects it into BOTH drift routes and the search corpus, so
  `sommeil` on a note satisfies a Zotero `sleep` as a `variants` entry with
  reason `synonym`. (d) New read-only report **`finder_tag_drift`** — the
  per-paper Finder-vs-note comparison Tag Hygiene's global counts could not
  answer; asymmetric gate (only papers with ≥1 Finder tag; non-macOS gets an
  empty report, not noise); new tab + serializer + search section. Pinned by
  `tests/audit/test_finder_tag_drift.py` + drift plan-3 tests + the config
  bool-validation test. Audit suites 209/209, JS 375/375.
- **Plan 4 — duplicate-scan infra.** (a) Persistent full-hash memo
  `BASE_DIR/audit/hash_cache.json` (`audit/core/hash_cache.py`, the RAG
  signature-cache pattern vendored-tree-local): every scan used to re-hash
  every candidate byte; now memo-valid files skip both read stages, the
  bridge's hash-recovery consults it read-only, and the unique-partial prune
  is memo-aware (a memoised file still pairs with a byte-identical newcomer).
  Unreadable files are counted + logged instead of silently vanishing from
  the search. (b) The duplicates walk finally prunes `ignored_dirs`
  (`.trash` copies no longer count as duplicates) via the shared
  `candidate_pdfs`. (c) `audit/core/naming.py` — dead since it was vendored —
  now powers `suggested_keep` (★ in the UI): the cleanest filename of each
  duplicate set, advice only. (d) `BibEntry.doi` parsed + normalised
  (`normalise_doi`). (e) `start_scan(pdf_text_provider=…)`: the route builds
  a cache-first, page-1-bounded PDF-text callable over
  `tagging/text_source.py` and injects it (search-corpus seam pattern) for
  the plan-5 near-duplicate tiers; CLI passes none. Pinned by
  `tests/audit/test_duplicates_infra.py` (8 tests at plan close, 9 after the
  verification pass). Audit suites 217/217.
- **Plan 5 — near-duplicates engine ("Possible Duplicates" report).** The
  exact scan sees only byte-identical files; every watermark/annotation/OCR
  pass hid a real duplicate. New `audit/engine/near_duplicates.py` produces
  labelled candidates from deterministic evidence tiers: `page1_text`
  (normalized first-page token key — digits dropped, so per-download
  watermark timestamps/IPs cancel out), `identifier` (first DOI/arXiv id on
  page 1, v1/v2 folded — the preprint-vs-published catcher), `bib_doi`,
  `multi_pdf` (one citation key, ≥2 byte-distinct PDFs), and
  `record_title`/`title_only` (normalized-title collisions, confirmed
  Zotero-style: year ±1 + first-author surname). File groups require ≥2
  distinct shas (byte-identical stays the exact report's business); text
  tiers run only with the route-injected page-1 provider (CLI degrades to
  hash-only); `no_text_count`/`dismissed_count` are disclosed in the meta —
  no silent coverage gaps. Third scan phase (`include_near_duplicates`,
  default on; skipped ⇒ 404 like `duplicates`), search-corpus section, new
  tab with kind badges. **Dismissals**: `BASE_DIR/audit/dup_decisions.json`
  (`dup_decisions.py`, RMW-locked atomic+fsync — the zoplicate
  "non-duplicate marklist" on the mapping.json discipline) via
  `POST /api/audit/dup-decision {pair_id, dismiss|clear}` — `clear` is the
  undo, checked first; UI rows use confirmInline + peek-not-pop undo,
  snapshot semantics. Pinned by `tests/audit/test_near_duplicates.py`
  (14 tests at plan close) + route verb/404 tests. Audit suites 233/233.
- **Plan 6 — content-similarity tier (`rapidfuzz`).** New runtime dep
  (`rapidfuzz>=3.9,<4.0`, pinned 3.14.5 in constraints), imported lazily and
  gated (`_load_fuzz`): an env without it keeps every deterministic tier and
  the report discloses `similarity_available: false` instead of crashing.
  Every file-level candidate group now carries a weakest-link pairwise
  `similarity` % (normalized page-1 token strings), and a **blocked sweep**
  adds `content_similarity` pairs for the last uncovered class —
  same-article/different-origin PDFs with no shared identifier: pairs
  compared only inside a filename author+year block, Jaccard ≥ 0.35
  pre-filtered, `fuzz.ratio(score_cutoff=…)` short-circuited — never
  full-corpus O(n²), `compared_pairs` disclosed in the meta. Cutoff knob
  `audit_near_dup_ratio` (60–100, default 88; `Settings.near_dup_ratio`,
  validated in the audit config POST, Audit Settings field) with the
  paperless-ngx 88/92/97 ladder rendered as likely / very likely /
  near-certain badges. Calibration rationale recorded in `audit/CLAUDE.md`.
  Pinned by 4 new engine tests + the knob range-validation route test.
- **Same-day verification pass** (two fresh-eyes agents over the whole diff +
  every doc claim; five code findings, all fixed with regression tests):
  (1) HIGH — the apply-tag-drift route looked the REQUESTED tag up by plain
  `tag_key` against a synonym-canonicalised drift map, making every
  synonym-remapped missing tag unapplyable (400 on the very surface the
  report offered — the unresolvable-row loop, synonym edition); fixed via the
  new shared `normalize.canonical_tag_key`, used by `group_by_key`, the
  lookup AND `still_missing`. (2) The near-dup text key now hashes only the
  first `_KEY_TOKENS = 150` tokens — the cache-first provider returns
  whole-document text for indexed PDFs but genuine page-1 text for fresh
  copies, so unbounded keys could never match across that split (the
  flagship re-downloaded-copy case was a silent false negative; sparse
  <150-token cover pages remain a disclosed residual). (3) The search-corpus
  memo now keys on the drift options too — a parent-tags toggle or
  `_tags.md` edit rebuilds it instead of serving rows the drift tabs no
  longer render. (4) The memo-aware unique-partial-prune exception is
  snapshot before the miss loop, so earlier full hashes in a size group no
  longer widen it (pure wasted I/O, never a wrong result). (5) The
  dup-decision write verbs use a strict RMW load — a corrupt
  `dup_decisions.json` now raises (route 500s) instead of being clobbered by
  a fresh-from-empty save; the scan-time filter still degrades to empty.
  Doc corrections in the same pass: the swallowed `## 2026-08-04` CHANGELOG
  header restored, the stale "fingerprint helper is local to the report
  module" sentences updated to `audit/engine/normalize.py`, the `variants`
  wire shape corrected to `[{zotero, note, reason}]`, and the guide's
  outbound-egress list completed with the HuggingFace model downloads.

## 2026-08-04 (four-pillar audit of the 2026-07-30 sprint: one blocker, then polish)

A full audit of `d906cea..e5595b3` (architecture / docs / UX / deployment)
found the sprint healthy — Plan 9 verified byte-faithful, both external
bridges intact, deployment ready — with ONE blocker and a tail of polish,
all fixed in five commits:

- **CI was red on main (the blocker).** `e5595b3`'s flake fix imported the
  daemon-join helper as `from conftest import join_post_index_daemons`; the
  repo has two `conftest.py` files (root and `tests/audit/`, neither a
  package), both imported under the bare name `conftest`, so under full-suite
  collection the `tests/audit/` one occupied `sys.modules["conftest"]` and the
  WHOLE suite aborted at collection — while the file passed alone, which is
  how it slipped through. The helper now lives in **`suite_helpers.py`**
  (re-exported by the root conftest); rule recorded in `CLAUDE.md`: test files
  never import from `conftest`. All nine `index_vault` call sites in
  `test_vault_regressions.py` now call the guard too (they carried the same
  latent teardown race).
- **A partial tagging batch-restore destroyed its own retry affordance**
  (`audit.js`): the undo button was removed on ANY terminal frame, though the
  server deliberately keeps failed/skipped ops `applied` and even streams
  "re-run to revert the rest". Kept + relabelled with the remaining count on
  `reverted < total`; removed only on a full revert. The restore stream also
  gained a `[cancel undo]` abort (honestly scoped: already-reverted ops stay
  reverted). Pinned by `tests/js/auditRetireUndo.test.js`.
- **Preview-cancel honesty reached AT users** (`deck.js`/`docgen.js`): the
  "UI-only, server compile finishes anyway" scope lived in `title` alone while
  the inherited aria-label claimed a server-side cancel; and the preview-pdf
  blob fetch ignored the abort signal, so Cancel during the download was
  inert. Both fixed in both modules, ratcheted in `deckPreview.test.js`; the
  help-badge "pair of 4s" click-steal geometry is now test-pinned too.
- **Audit tab a11y/polish**: the "↳ Unread Only" tab carries
  `aria-label="PDFs Without Zotero · Unread Only"` (its parent was inaudible
  in out-of-order SR listings), and the superset cross-link uses the same
  `[bracketed]` grammar as the sibling tab's actions.
- **Docs re-synced**: root `CLAUDE.md`'s migration ladder now records 3→4 (the
  DeepSeek alias reset — it stopped at 2→3 while `CONFIG_VERSION` is 4, the
  one drift that could cause a future migration collision);
  `docs/fix_plans_2026-07-30/OVERALL.md`'s header/progress board caught up
  with its own per-plan sections (Plan 9 steps, 7.2/7.5, 6.2, the completed
  push); README fixed its dev check commands (missing `docgen`/`tagging`/
  `cli`/`mcp_server` sweeps + `test_path_validators.py`), the stale "six
  tabs" count, the adapter/provider lists (DeepSeek/Z.AI), the Plan 9 `rag/`
  layout, and the per-acquisition lock-TTL wording.
- **Deployment cosmetics**: `TAVILY_API_KEY` documented in `.env.example` and
  the DMG's READ ME FIRST (previously a packaged-app user had no discoverable
  way to enable web search); the literal `\n` in that heredoc fixed;
  `ruff==0.12.12` pinned in `constraints.txt` (matching the pre-commit hook);
  the stale "Plan 9 step 4a" phase-header comment removed from
  `rag/vault.py`.

## 2026-07-31 (audit: the un-Zotero'd PDF tabs were named as a partition)

- **"Read PDFs Missing Zotero" has never filtered to read PDFs.**
  `read_unzoterod.find()` applies no threshold at all — it lists EVERY PDF the
  bridge could not resolve to a bib entry, read and unread alike, ranked by
  annotation count. `unread_unzoterod` walks the same `bridge.unmapped_pdfs`
  and keeps only `annotations < threshold`, i.e. it is a strict SUBSET. The two
  tabs were called "Read PDFs Missing Zotero" and "Unread PDFs", which reads as
  a partition — so the complete list was already on screen and effectively
  undiscoverable. Renamed to **"PDFs Without Zotero"** and **"↳ Unread Only"**,
  reordered adjacent with the superset first. Report ids are unchanged, so the
  `/api/audit/reports/<name>` contract, the cross-report search corpus and the
  `python -m audit --check` names are untouched.
- **Exclusion disclosure — the substantive half.** `unmapped_pdfs` is only ONE
  of the bridge's residual buckets and the `biblio_skip_prefix` files never
  reach the bridge at all, so "every unmapped PDF appears" was never the same
  claim as "every PDF missing from Zotero". `read_unzoterod` now carries
  `ambiguous_count` / `confirmed_no_match_count` / `skipped_count` — counts
  only, never rows — and the meta line names each one, with cross-links to the
  reports that own those rows. Zero counts are dropped rather than printed as
  "0 excluded". `audit/scan.py::check_read_unzoterod` prints the same three.
  Confirmed-no-match PDFs stay counted-but-unlisted by choice: the user already
  triaged them, and listing them would re-clutter the queue they just cleared.
- **Docs corrected, not just extended.** Three places (root `CLAUDE.md`,
  `audit/CLAUDE.md`, the `/api/audit/pdf` docstring) justified that route's
  vault-root containment by claiming "ambiguous PDFs come from the whole
  scanned attachments tree". They do not — `inventory._walk_biblio_pdfs` walks
  `biblio_articles_dir` and nothing else, so every ambiguous PDF is
  biblio-scoped. The containment is left as-is (wider than needed = defence in
  depth, and narrowing a security boundary is not a drive-by) but the reason is
  now accurate.
- **Fixed a pre-existing full-suite-only flake** (unrelated to the audit
  change; it just kept surfacing during this session's runs).
  `index_vault` ends by kicking a fire-and-forget `bm25-postindex-rebuild`
  daemon, which is **still alive when `index_vault` returns** — proven directly,
  not inferred. In production that is fine (the index dir is stable), but in a
  test it outlives the enclosing `patch("rag.vault.OBSIDIAN_INDEX_DIR", tmp)`
  and the `TemporaryDirectory`, so its sidecar write races
  `TemporaryDirectory.__exit__`'s `rmtree`. Load-dependent, hence a rare flake
  under the full suite that never reproduced on the file alone (~2 failures in
  20 full runs; 12 consecutive clean runs after the fix). New
  `conftest.join_post_index_daemons()` joins the daemon, and
  `test_lancedb_migration.py::_index` calls it INSIDE the patch/tempdir scope —
  an autouse teardown fixture would be too late, the directory is already gone.
  Also stops the daemon leaking a sidecar into the shared test BASE_DIR. The
  other ~9 `index_vault` call sites in `test_vault_regressions.py` have the same
  latent exposure and can adopt the same one-liner if they ever flake.
- Pinned by `tests/js/auditUnzoteroedTabs.test.js` (naming, ordering, the
  aria-hidden containment glyph, no glyph leaking into the search-panel group
  headings, zero-suppression, delegated cross-links) and by the containment
  invariant `unread.rows ⊆ read.rows` plus the three counts in
  `test_audit.py::TestUnzoterodReportsUseCache::test_reports_serve_from_cache_without_disk_reads`.

## 2026-07-30 (fix-plan Plan 9: `rag/vault.py` decomposition, 6,300 → 5,143 lines)

- **Four move + re-export extractions, zero test edits, suite green after each.**
  `rag/storage_io.py` (stdlib-only leaf: `_write_json_atomic`,
  `_json_file_has_complete_tail`, `_simple_vector_store_has_any_embedding`),
  `rag/sidecars.py` (`RetrievalCacheMixin` — the BM25 + wikilink sidecars —
  plus `_WikilinkGraph`, `_closest_note_by_dir` and the shared vault link
  regexes), and `rag/build.py` (`IndexBuildMixin` + `_PersistTarget`).
  `rag.vault` re-exports every moved name; `obsidian_manager` is unchanged.
- **`index_vault` 654 → 216 lines.** Split in place first (step 4a) into eight
  named phases, then moved (4b). The ORCHESTRATION stayed in `rag/vault.py` on
  purpose — every early return, and specifically which of them sit inside the
  outer `try` (i.e. which reach `finally: gc.collect()`), is the part that must
  be readable in one place. `_PersistTarget` bundles the seven locals the
  mid-run checkpoint and the final persist both closed over, so the two paths
  writing the same meta for the same index cannot drift apart.
- **The constraint that shaped all of it.** A method reading a module global the
  suite patches as `rag.vault.<name>` (`OBSIDIAN_INDEX_DIR`, `load_config`,
  `get_provider`, `lancedb_available`, …) cannot simply be relocated: the global
  would resolve against the new module's namespace and silently ignore the
  patch, leaving ~90 patch sites as no-ops — tests passing while asserting
  nothing. Two sanctioned workarounds, both used: leave the read behind in a
  thin accessor the mixin reaches through `self` (`_bm25_sidecar_dir`,
  `_wikilink_sidecar_path`), or make it a keyword-only parameter whose name
  **shadows the global exactly**, read at the call site (`rag/build.py`). That
  shadowing is why the moved bodies are byte-identical apart from 8 ×
  `OBSIDIAN_INDEX_DIR` → `index_dir` — verified by statement-level AST diff at
  both steps, not by eye.
- **Steps 1 and 3 dropped deliberately** (index meta/archive; PDF/image caches):
  209/326 and 324/508 of their lines read patched globals, so moving them would
  split a cohesive family across two files for ~110 usable lines each. **The
  plan's ≤2,000-line target is not reachable under its own ground rules** — the
  residual bulk (`_index_documents_streaming` 463, `_load_vault_documents` 364,
  `_chunk_raw_documents` 199) would need 5–6 threaded parameters each, at which
  point the indirection costs more than the split returns. Recorded for a
  maintainer call in `docs/fix_plans_2026-07-30/OVERALL.md` (P9.5).
- A rare flake surfaced during this work in
  `test_lancedb_migration.py::TestBackendAwareIndexVault::test_index_with_no_recorded_embed_rebuilds`.
  **Diagnosed and fixed under the 2026-07-31 entry; it was pre-existing and
  unrelated to this decomposition** (a broken `OBSIDIAN_INDEX_DIR` patch would
  have failed that test every run, not twice in twenty).

## 2026-07-30 (fix-plan Plan 7.2 + 7.5: per-vault PDF signatures, archive footprint)

- **The PDF signature map is per-vault (P7.2).** It was ONE shared
  `pdf_signatures.json` keyed by vault-RELATIVE path. Two vaults sharing a rel
  shared an entry, and — the cost that actually bit — the loader persists the
  whole map it built for the vault it just scanned, silently **evicting** the
  other vault's entries and forcing a full re-hash of every large PDF there on
  its next run (hundreds of MB per textbook; this cache exists precisely to
  avoid that). Now `obsidian_cache/pdf_signatures/<vault_key>.json`, matching
  the sibling `pdf_cache/<vault_key>/` and `image_cache/<vault_key>/` that have
  been per-vault all along. The two legacy shared files are still **read and
  merged** so the upgrade costs no re-hashing — and a *second* vault can adopt
  them too — but are **never written again**. Cross-vault adoption is safe
  because `_pdf_file_signature` re-validates size + mtime_ns before reusing a
  sha, so a foreign or stale entry degrades to a full hash rather than serving
  wrong text. The read-path stat cache now keys on the per-vault PATH as well
  as the file signatures, so a vault switch can't be served the previous
  vault's parsed map. No configured vault degrades to the old shared file.
- **Index-archive footprint is disclosed (P7.5).** Retention is count-bounded
  (2 routine / 5 protected), so a multi-GB index could sit on tens of GB of
  archives with nothing saying so. A byte cap was rejected again — it needs a
  du-walk on every archive — but the archive is now measured **once**, right
  after the rename (`_dir_size_note`, off the indexing hot path, bounded by
  `_DIR_SIZE_WALK_BUDGET_S`), and the size + the retention counts go to the log
  AND the status ring. A walk that runs out of budget reports **nothing**
  rather than an undercount presented as the archive's size.

**P7.1 and P7.3 are deliberately NOT in this commit.** P7.1 (batching the
changed-chunk lancedb deletes) is a restructure of the delete-then-reinsert
interleave in the streaming indexer — the most data-safety-critical loop in the
app, where the 2026-07-22 A6 audit already found that a swallowed delete failure
produced permanent duplicate rows. Batching changes the failure semantics (a
partial batch failure is an ambiguous subset, where today one chunk fails
alone), so it wants a session with the maintainer's attention and dedicated
fixtures, not a tail-end refactor. P7.3 (offsetting add/delete drift) is
specified as "measure `lancedb_list_doc_ids` on a large index first, then choose
the gate" — that measurement can only be taken against the real index with the
app closed, so the gate cannot be chosen here. Both stay in Known Issues.

## 2026-07-30 (fix-plan Plan 6.2: Cancel for the two longest LaTeX operations)

Generate and Augment have had a Cancel button since their SSE wiring landed;
**compile-fix and preview — the longest deck/article operations — had none**, so
a user watching a doomed repair loop could only wait or reload the app.

- **compile-fix (deck + docgen)** now POSTs with an `AbortController` signal.
  Aborting closes the SSE stream, and the server's existing consumer-disconnect
  path does the rest: the worker's cancel event is set, latexmk's process group
  is TERM→KILLed on its own timeout, and `_SSEOpGuard` releases the op lock once
  the worker exits. No server change was needed.
- **compile-preview** is a *synchronous* route, so its Cancel frees the UI and
  the fetch only — the server's latexmk pass runs out its
  `min(deck_compile_timeout_s, 300)` cap. That limit is stated in the button's
  title and the status line rather than implied away, and since preview never
  rewrites the `.tex` an abandoned pass leaves only regenerable build artifacts.
- A cancelled run now reports cancellation instead of logging a crash.

Ratcheted in `tests/js/deckCompileFix.test.js` over BOTH `deck.js` and
`docgen.js` (signal present in the same options object as the body; controller +
Cancel button per op; cancellation messaging). JS suite 351 passed.

**P6.1 (the piecewise global-lock hold) is deliberately NOT in this commit.**
It has three viable shapes — a per-path lock registry (~20 acquisition sites;
also closes the 2026-07-20 global-lock deferral), release/re-acquire between
sections (3 sites, but a failed re-acquire discards completed agent turns), or
acquiring the lock LATE (the piecewise agent turns are read-only, so the lock
protects nothing during them; changes the documented "one op at a time ⇒ 409"
contract). They change different contracts on a lock that guards user `.tex`
writes, so the choice is recorded in the tracker for the maintainer rather than
made in passing. `CLAUDE.md` already warns against restructuring
`_SSEOpGuard`'s release ownership as a drive-by. P6.3 (apply-repair
server-owned bytes) is held with it, since it introduces the same staging shape.

## 2026-07-30 (fix-plan Plan 8: long-tail hardening)

Five of the six long-tail items; two deliberate scope calls are recorded below.

- **Vault-chat export no longer duplicates the question (P8.1).** A partial
  failure records `{user, assistant+"[interrupted]"}`; the Retry then re-asked
  the SAME question and its success path pushed the user turn again, so the
  exported transcript showed the question twice. `_pushUserTurnOnce` +
  `_recordedRetryQuestion` make the user turn idempotent per question, keeping
  BOTH assistant outcomes. Covers the retry-fails-again and
  new-different-question cases too. Pinned by `tests/js/vaultExportRetry.test.js`.
- **Parity ratchet over the six hand-rolled path validators (P8.2).** `CLAUDE.md`
  warned that a hardening of `resolve_under_root` "must be mirrored into those
  hand-rolled validators" with nothing enforcing it. `test_path_validators.py`
  pins the shared contract (embedded NUL/LF/CR, empty/non-string/over-long,
  relative-vs-absolute shape) across all six at once, each group carrying a
  POSITIVE control so a rejection can't be satisfied by a validator that returns
  None for everything. Registered in BOTH the CLAUDE.md quick-gate and CI.
  **It immediately earned its keep:** `deck.py::_resolve_out_dir` was the one
  validator of six that accepted a RELATIVE path — `realpath` anchored it to the
  server's CWD, so `out_dir: ".."` scaffolded a deck somewhere the caller never
  named. A footgun over HTTP and over the CLI alike (the CLI is a client; the
  path resolves in the server's process, not the user's shell). Now rejected
  before `realpath`, `~` expansion still honoured.
- **`llm_usage.jsonl` rotates (P8.3).** Append-only and unbounded (a Tier-3 item
  deferred from the 2026-07-03 perf pass). At 10 MB it rolls over to `<log>.1`
  via an atomic `os.replace`; `_iter_disk_records` reads the rotated generation
  FIRST and then the live log, so a summary window spanning a rotation keeps its
  records. Size comes from the append's own `tell()` (no extra stat on the
  per-request path) and a failed rotation never breaks recording.
- **Truncated vision descriptions are now visible (P8.4, partial by choice).**
  Both transports gained an additive `meta` out-dict reporting the stop cause,
  and `describe_image` logs a WARNING naming the cap and the remedy when a
  description was cut off. The description is **still cached** — deliberately:
  not caching it would re-run an expensive vision call on every indexing run,
  a worse trade for a large vault than a truncated-but-searchable description.
  The self-healing variant (record the cap, re-run when it rises) stays open in
  Known Issues rather than being half-built into the indexing hot path.
- **`vision_failure_cooldown_s` has a Settings control (P8.5).** The validator
  already existed; the knob was config-only. Added to the OCR/Vision section
  with a help tip, and both docs that called it UI-less are corrected. `0` (the
  disable value) survives the client's `Number.isFinite` collection.
- **Not done, recorded not silently dropped:** P8.6 (the three uncovered
  image-link resolver shapes) needs a read-only survey of the maintainer's real
  vault first — fixing them changes WHICH files index, i.e. additive vision +
  embed churn on the next reindex, so it must be gated on evidence that each
  shape actually occurs. Stays in Known Issues.

## 2026-07-30 (fix-plan Plan 5: Zotero/tagging robustness)

Three carried deferrals — two from the 2026-07-21 audit, one from 2026-07-23.

- **The 429 itself now arms the read backoff (P5.1).** `_backoff_until` was
  armed only by a `Backoff` header on a *successful* response, so the one
  signal that actually means "you are being throttled" — the 429/503 — was
  captured onto `ZoteroRateLimited.retry_after_s` and then dropped; the next
  read re-hit the same 429 immediately. `_raise_for_http` now calls the
  extracted `_arm_backoff`. The invariant holds: only READ paths consult it
  (`_await_backoff`), so a write under the obsidian op-lock still never sleeps,
  and the wait stays bounded by `_MAX_BACKOFF_WAIT_S`.
- **The userID cache expires (P5.2).** `resolve_user_id`'s cache doubles as
  "this key authenticates" (`probe` returns True straight off a hit), so an
  unbounded entry let a key **revoked at zotero.org** keep reporting the Zotero
  target as available until the app restarted — the user found out when a write
  failed. Entries now carry a 15-minute TTL (`_USER_ID_TTL_S`); a *rotated* key
  needed no change (the sha256 fingerprint is part of the cache key).
- **Batch restore streams (P5.3).** `POST /api/tagging/restore {batch_id}` was a
  synchronous JSON request: N pre-lock Zotero GETs + N reverts on one Waitress
  thread, silent for minutes on a 150-op undo. It now runs through
  `run_sse_worker` with per-op `{"info"}` progress and a terminal `{"restore"}`
  frame carrying the old payload; `audit.js`'s `[undo retire]` consumes it into
  the existing status line. The lock choreography is unchanged (Zotero reads
  pre-lock, one heartbeat-checked hold for the reverts + save, per-op
  conservatism). **The same URL now answers SSE for a `batch_id` body and JSON
  for an `op_id` body** — deliberate and safe: only the batch is long-running,
  and the CLI/MCP driver only ever sends `op_id` (pinned by the driver contract
  + replay suites). Validation failures are decided before streaming begins, so
  they keep real status codes — pinned by two new tests so an SSE 200 can never
  start masquerading as success.

## 2026-07-30 (fix-plan Plan 4: doc hygiene — one true record of deferred work)

Docs only, zero code. The 2026-07-29/30 verification sweep found real deferrals
recorded *only* in subtree docs, the CHANGELOG or code comments — exactly the
record loss the root Known Issues section exists to prevent — plus two pointers
that claimed something lived there when it didn't.

- **Eight orphaned deferrals promoted** into root `CLAUDE.md` (*Promoted
  2026-07-30*), each re-verified against the code first: the audit's serial
  mapped-PDF annotation reads (`inventory.py:332`, `audit/CLAUDE.md`-only); the
  four CHANGELOG-only Tier-3 items (frame-slim, apply-recompute, **unbounded
  `llm_usage.jsonl`**, deck-checkpoint `raw`); the tagging-propose
  pre-first-token cancel gap; the vision truncated-description cache (keyed on
  image bytes, so raising the cap never re-runs it); the three uncovered
  image-link shapes; retire-tag's Zotero child-note/`.bib` blind spot; the eval
  graded-qrels v2 seam; deckgen batch mode.
- **Two broken pointers fixed.** The CHANGELOG and `PUBLISHING.md` both said
  provider-native web-search v2 was "recorded in CLAUDE.md Known Issues" — it
  was recorded in the *Agent Mode* section, so a Known Issues entry now makes
  both true. And the "thread-leak-guard port deferred to 4.3" note is annotated
  **closed**: item 4.3 shipped it (`api/sse.py::_SSEOpGuard` releases only once
  the stream ended AND any spawned worker exited).
- **Drifted facts refreshed.** `rag/CLAUDE.md`'s batched-embed parity caveat is
  narrowed to *unvalidated* backends (Ollama/`nomic-embed-text` was validated
  2026-07-05 — stored vectors bit-identical at batch 1 vs 16); `rag/vault.py`
  re-measured at **6,176 lines / `index_vault` ~667** (recorded as 5.4k/~480 —
  the deferral grows while deferred); the path-validator count corrected to six.
  Bonus: `rag/CLAUDE.md` #33 said "Two link shapes"/"Both" while enumerating
  three.
- **Three open product questions recorded** (*Awaiting a maintainer decision*):
  Settings tiering — which had **no trace anywhere** that it was ever deferred —
  the A5 in-app `.env` key affordance, and deckgen batch mode.
- **`PUBLISHING.md` corrected.** Its status note claimed "nothing left to
  withhold: every `docs/*` plan doc has been deleted after shipping", which this
  plan folder itself invalidated. Scanned clean (no vault paths, personal data,
  keys or excerpts) but flagged as a *live* planning artefact naming unfixed
  defects — publish/withhold is a maintainer decision, and the "no strip step"
  claim must not be trusted while `docs/` is non-empty.
- Known Issues gained a standing rule at the top: record a deferral **there**
  and cross-reference from the detail doc, never the other way round; plus a
  pointer to `docs/fix_plans_2026-07-30/OVERALL.md` as the live tracker.

## 2026-07-30 (fix-plan Plan 3: nine correctness quick wins)

Eight long-carried 2026-07-20 audit deferrals plus one trust-check asymmetry,
each small, local and independently pinned. Known Issues shrank by eight entries.

- **Google adapter: `Retry-After` honoured (P3.1).** `_http_error_to_llm_error`
  now calls `parse_retry_after_s` on a 429 (header first, then the "try again in
  Xs" body phrase) so a Gemini rate-limit gets the provider's own wait as the
  backoff FLOOR — the fix that shipped for OpenAI/Anthropic 2026-07-03 and never
  reached the sibling. Non-429s carry no hint (a schedule the provider never
  asked for), and a response without `.headers` is tolerated.
- **Gemini prompt-level block is no longer a malformed turn (P3.2).** A blocked
  PROMPT returns zero candidates + `promptFeedback.blockReason` instead of a
  candidate with `finishReason: SAFETY`; that read as an empty STOP and counted
  toward the agent loop's malformed-tool-call streak, dragging the turn into the
  RAG fallback + capability nag. `_prompt_block_reason` maps it to
  CONTENT_FILTER; a candidate's own finish reason still wins.
- **Cancelled streams are recorded as cancelled (P3.3).** A client disconnect
  raises `GeneratorExit` through `for chunk in stream`, which `except Exception`
  deliberately does not catch — so all four adapters recorded a clean
  `success=True`. Each now has an explicit branch marking
  `error_category=CANCELLED_ERROR_CATEGORY` (new shared constant carrying the
  reasoning), and OpenAI — whose usage rides the never-delivered final chunk —
  re-estimates output tokens from the text actually streamed. `success` stays
  True on purpose: `record()` forces cost 0.0 for a failure, and the provider
  bills what it generated, so flagging it as failed would hide real spend.
  Input-token under-reporting on final-chunk-usage providers is a documented
  residual (not fabricated — see Known Issues).
- **`init_db` fails loudly (P3.4).** It logged one line and returned, letting the
  app boot with no `uploads` table and surface as a distant 500 on the first
  upload; it now re-raises after logging.
- **NFC-insensitive tag comparison (P3.5).** macOS stores Finder tags (and Zotero
  can hold) accented text in NFD while `_tags.md` and the browser send NFC, so
  `.lower()`-only comparison treated « rétablissement » NFC/NFD as two tags —
  unioning a visual duplicate and missing the tag a removal targeted. All ten
  comparison sites in `tagging/writers.py` now route through one `_tag_key`
  (NFC + lower). KEY only: Finder's raw colour-suffixed entries and Zotero's
  full tag dicts still round-trip byte-identically for Restore, and `.lower()`
  (not `casefold()`) keeps case semantics unchanged.
- **Atomic writers preserve an existing file's mode (P3.6).** `mkstemp` creates
  0600 and `os.replace` carried that to the destination, so a 0644 vault note
  rewritten by refactor Apply / docgen silently became owner-only.
  `_inherit_dest_mode` copies an EXISTING destination's bits before promotion; a
  NEW file keeps 0600 (never invented as world-readable) and a chmod failure
  still writes the content.
- **`load_config_readonly` never returns an empty proxy (P3.7).** A concurrent
  `save_config` between its cold-path `load_config()` and the locked re-read
  yielded `MappingProxyType({})`; it now retries and, in the pathological case,
  returns the fully-merged dict instead of an empty mapping.
- **`ReaderWriterLock` docstring made truthful (P3.8).** The headline said
  "reentrant" unqualified while a plain reader re-acquiring `read_lock` with a
  writer pending self-deadlocks. Now states writer-only reentrancy and forbids
  nested reads, with the deadlock mechanism spelled out.
- **The pandoc Lua filter gets `maintain.sh`'s trust check (P3.9).**
  `check_tex.py` handed a suite-resident, vault-editable Lua filter to pandoc
  after only an `.exists()` check while `maintain_gui.py` ownership-checked its
  script. `_check_file_trust` mirrors that check (a deliberate copy — the GUI
  module imports tkinter and this is a headless CLI); an untrusted filter is
  skipped with a note in that document's log and the conversion runs without it.

Also fixed, found while running subsets of the suite: **`TestVisionCallBounds`
was order-dependent** — the vision transport reuses the `(base_url, timeout)`-keyed
LM Studio client cache, so a client cached by another test file meant
`openai.OpenAI` was never constructed and the retry/token assertions saw nothing.
It passed only because the documented quick-gate order runs `test_concurrency.py`
before `test_llm.py`. Its `setUp` now clears that cache, the same discipline
`test_llm.py::_fake_httpx_module` already applies to the anthropic/google caches.
Pre-existing (reproduced on `5c22005`), not a Plan 3 regression.

Tests: `TestGoogleErrorAndBlockHandling`, `TestStreamCancellationAccounting`
(test_llm.py), `Plan3CorrectnessFixesTest` (smoke_test.py),
`TestTagKeyNFCNormalisation` (test_tagging.py) — 1888 passed, 3 skipped across
the quick-gate + CI superset (and now under reversed file order too); 339 JS
tests green.

## 2026-07-30 (fix-plan Plan 2: DeepSeek alias sunset cleanup + config migration 3→4)

The 2026-07-24 alias sunset fired as designed (the date-gated curation stopped
offering `deepseek-chat`/`deepseek-reasoner`), leaving three pieces of residue:

- **Dead machinery deleted.** `_DEEPSEEK_ALIAS_SUNSET` / `_DEEPSEEK_LEGACY_ALIASES`
  / `_deepseek_curated` were permanently-dead past the date;
  `DEEPSEEK_CURATED_MODELS` is a plain list again. The pinning test became
  `test_deepseek_legacy_aliases_stay_retired` (unconditional absence).
- **Config migration 3→4** (`_migrate_3_to_4`, `CONFIG_VERSION = 4`): the
  sunset stopped OFFERING the alias ids, not USING them — a `deepseek_model`
  persisted before it kept sending an id the provider 404s on every call. The
  migration resets exactly the two retired aliases to the current default
  (read from `_DEFAULTS`, never an inline literal), preserving any other value
  as user-chosen. The 2→3 remedy pattern; pinned in
  `smoke_test.py::TestConfigVersionMigration`.
- **Pricing rows dropped** for the two aliases (historical usage records store
  cost at write time; post-migration no new call can carry them). Docs updated
  (`core/llm/CLAUDE.md`, adapter comments) — the sunset now reads as history,
  not as a pending event.

## 2026-07-30 (fix-plan Plan 1: Ollama thinking suppression + eval-generator diagnosability)

Closes all three 2026-07-27 OPEN findings (the deferred-work plan set lives in
`docs/fix_plans_2026-07-30/`; its OVERALL.md is the cross-session tracker).

- **Ollama `think=False` everywhere (the headline fix).** Ollama bills thinking
  tokens against `num_predict` and the app never reads `message.thinking`, so a
  reasoning model returned an empty reply after burning its whole cap. Every
  Ollama chat call now suppresses thinking: `OllamaProvider.stream_chat` routes
  through `stream_chat_with_think_off` (first chunk pulled inside the guard —
  pre-first-token, the house retry boundary) and the agent tool path through
  `chat_with_think_off`. A backend/SDK that rejects the parameter itself gets
  one retry without it (`_think_param_rejected` deliberately does NOT match
  genuine generation errors) and the model is memoised in
  `_THINK_UNSUPPORTED_MODELS`. Per-call-site opt-in seam for a future
  reasoning consumer: `request.metadata["ollama_think"] = True` (or an explicit
  `think=` kwarg to `stream_chat`) forwards `think=True` verbatim, no fallback.
  Out of scope, recorded: the LlamaIndex `Ollama` LLM used by query-expansion
  rewrites. Pinned by `test_llm.py::TestOllamaThinkSuppression` (9 tests).
- **`scripts/gen_golden_retrieval.py` fails fast and says why.** (a) The reply
  budget is a real knob: `--max-tokens` (default 512 via `DEFAULT_MAX_TOKENS`),
  content-determining so it joins the resume job key. (b) The 0-query failure
  line now quotes the raw reply's length + a 200-char preview (`ChunkResult`
  gained `raw_len`/`raw_preview` — the reply was previously discarded before
  the carrier, making the failure structurally undiagnosable; the
  `tagging/propose.py` remedy pattern). (c) Systematic-failure early abort: if
  the first `EARLY_ABORT_AFTER` (5) completions of a run ALL produce nothing,
  the run cancels the queued tail and exits 1 with the likely causes (thinking
  model, starved budget) instead of absorbing ~40 identical failures over ~40
  minutes; one success re-enables the per-chunk tolerance. Covered by
  `tests/eval/test_golden_gen.py::TestDiagnosticsAndEarlyAbort`.
- **Point-of-use wording.** `tests/eval/README.md` no longer calls "any local
  model" safe (privacy vs quality axes split; the thinking-model caveat is
  rewritten as fixed-with-residual for pre-`think` servers) and the script's
  docstring/`--help` carry the caveat at the point of use.

## 2026-07-29 (four-pillar audit remediation: refusal surfacing · lock-shadowed heartbeat · settings race · toolchain hardening)

A fresh four-pillar audit of `HEAD~5..HEAD` confirmed 15 findings with one
theme: backend safety mechanisms correct server-side and then discarded
downstream. Fixed in five batch commits; two items became documented deferrals
(protected-archive byte cap; vault-resident-code sandboxing) in CLAUDE.md
Known Issues.

- **The erased refusal (P0).** The embed-backend gate's refusal — whose whole
  value is its recovery instructions — travelled only on the destructively
  drained `messages` channel, and `vault.js`'s `state === 'error'` branch then
  blanked the status line and rendered a generic "Indexing failed." + a Retry
  that re-ran the refusing gate forever. Every `index_vault` error exit now
  records persistent `error_code`/`error_detail` (`_record_index_error`, on the
  status payload additively); the client renders the real reason and keys
  actions on the code: `embed_backend_mismatch` offers a **confirm-gated
  "Rebuild from scratch"** (→ the newly exposed `force_fresh` body field on
  `POST /api/obsidian/index`, strict `coerce_bool`) instead of Retry, and the
  record survives a tab reload via `refreshIndexState`. The LanceDB preflight
  gained the same code (`lancedb_unavailable`) + its missing phase stamp.
- **Reranker heartbeat out from under the lock (P1).** The 2026-07-27 ticker
  started inside `_reranker_load_lock`, covering only the loading thread —
  launch prewarm (stage_cb=None) holds that lock for an entire multi-GB
  download, so the documented upgrade flow ("relaunch, ask one question")
  walked the first chat into a silent 330 s SSE stall kill. The ticker now
  starts before the acquisition and stops in a `finally` after it, so waiters
  beat too; the sticky-failure early return also emits a per-query
  "reranking is disabled for this session" notice through `stage_cb` (never
  the status ring). Client side, repeated heartbeat frames now UPDATE one
  bubble in place (`vault.js::progressKey`, digit-blind) instead of appending
  ~40 bubbles + ~40 SR announcements over a 20-minute download.
- **Settings modal races (P1).** `refreshSettings` re-checks its
  `_saveTimer || _savesPending` guard AFTER its awaits (a keystroke during the
  GET used to be reverted by the stale response, then persisted by the pending
  debounce); `_savesPending` increments after the payload build (leak window);
  and a server-dropped key's error note is now in the control's
  `aria-describedby` (WCAG 3.3.1 — additively, control-hint ids survive).
- **Eval-harness restore integrity (P1).** `_PENDING_LOCK` is an `RLock`
  (SIGTERM re-enters on the main thread mid-`_arm_pending`; a plain Lock
  self-deadlocked and the config was never restored) and a failed unlink in
  `restore_config`'s no-config branch raises `ConfigUnsafeError` keeping the
  sidecar, instead of claiming a restore that didn't happen.
- **vault_maintain toolchain.** `maintain_gui.py`: Stop's TERM→KILL escalation
  captures its process (it could SIGKILL a subsequently started run), `_drain`
  re-arms in a `finally` (one render error used to kill the poll loop
  permanently), only exact `::step::/::start::/::end::` prefixes are protocol,
  "Stopped" is `returncode < 0` only, and `maintain.sh` is refused unless
  owned-by-user and not group/world-writable (`_check_script_trust`; residual
  iCloud-sync risk documented). `check_tex.py`: latexmk/pandoc run in their own
  process group with TERM→KILL on timeout (mirrors `deckgen/compile.py` — a
  timeout used to orphan pdflatex/biber children into the TemporaryDirectory
  cleanup), one read per `.tex` (`classify_document` replaces the
  `is_root_document` + `is_beamer` double read), dead `_PRINT_LOCK` removed.
- **Privacy/docs/CSS.** The personal vault-path literal is gone from
  `vault_paths.py` (rule 5 is now subfolder names only — the tree publishes to
  a public mirror); `~/venvs/...` paths genericized in guide.md /
  tests/eval/README.md / install_mcp.py; `.help-tip` lost its unreachable
  scrollbar (the tip-length ratchet is the real bound); `.help-badge` gained
  the `margin-left: 4px` that makes its 24px hit-area overlay provably
  non-overlapping; `.gitignore`'s `*cells*.json` is root-anchored.
- Tests: `TestEmbedProviderStamping` (+persistent payload, supersession),
  `TestRerankerLoadVisibility` (+beat-while-blocked, sticky notice),
  `test_30c` route force_fresh coercion, `TestConfigBackupRestore` (+RLock
  ratchet, unlink-failure), and three JS suites (`vaultErrorDetail`,
  `vaultInfoDedupe`, extended `settingsFeedback`).

## 2026-07-27 (retrieval eval harness · reranker upgrade path · embed-backend gate · four-pillar audit remediation)

Retrieval quality was unmeasurable: every knob (RRF leg weights, cutoff, rerank
pool sizing, reranker model) was tuned by feel, and answer-level pass rates are
far too noisy to attribute a change to the retriever — Track 5.6 hit that wall
and fell back to eyeballing raw embedding vectors. Shipped in two passes: the
harness itself, then a four-pillar audit (principal-engineer / performance /
a11y / AppSec) whose findings are folded in below rather than deferred. No
default retrieval behaviour changed.

### Retrieval eval harness (`tests/eval/` + `scripts/`)

- **`tests/eval/retrieval_metrics.py`** — pure stdlib (**zero project imports**,
  so it imports before `CHATEKLD_BASE_DIR` is fixed) computing **document-level**
  recall@k / MRR / nDCG@k: the ranked list is collapsed to one entry per
  document at its best rank, so three chunks of one note count once instead of
  inflating recall. Every path comparison runs through `norm_source` (NFC +
  casefold) on both sides — the target vault is bilingual FR/EN on an
  NFD-filename APFS volume, where a byte comparison scores a perfect hit as a
  miss. A **sourceless** retrieved chunk now consumes a rank slot instead of
  being dropped (it used to silently promote everything below it: `["", "b.md"]`
  scored `rr = 1.0` where the honest answer is `0.5`); fully-stamped indexes are
  byte-identical, older results files are not comparable. `load_golden` raises
  `ValueError` naming the offending row on any malformed entry — including a
  `relevant_sources` given as a JSON *string*, which used to be exploded into
  one-character "documents" and score 0.0 forever, reading as a retrieval
  regression.
- **`scripts/gen_golden_retrieval.py`** — builds a per-vault golden set by
  *query inversion*: sample documents, hand each one's longest chunk to the chat
  model, ask for the queries a user would type to find it; the chunk's own
  document is the ground truth. Reads `docstore.json` directly, so it is
  backend-agnostic and builds no index or embedding model. **Read-only on the
  index dir**, hence no app-closed gate.
- **`scripts/eval_retrieval.py`** — replays the golden set through the LLM-free
  `retrieve()` against the REAL index with `top_k_explicit=True`, sweeps knob
  "cells", prints a Δ-vs-baseline table and writes a results record. Config-only
  knobs (RRF weights, pool multiplier/floor, reranker device, wikilink caps,
  thesaurus variants, `context_window`) are swept by rewriting `config.json`.

### Audit remediation — data safety

- **Vault excerpts no longer leave the machine unasked.** The generator defaulted
  to the *configured* chat provider, which may be online: on the maintainer's
  setup the documented one-liner would have shipped ~72 000 characters of
  clinical notes (40 × 1800, deliberately each note's longest chunk) to a
  third-party API with no prompt, no banner and no mention in any doc. An online
  provider is now **refused by default** and needs both `--allow-online` and an
  interactive confirmation naming the destination and payload size; `.env` is
  loaded only *after* the gate, so a refused run does not even materialise
  credentials. Every printed exception goes through `core.llm.redact.redact`
  (an `LLMError` can carry a provider URL with the key inline — the documented
  Gemini/httpx leak shape).
- **Clinical excerpts can no longer reach git.** `.gitignore` covers
  `golden_retrieval*.json`, `retrieval_eval_*.json`, `/eval/`, `*cells*.json`
  and `config.json.evalbak`; the generator additionally refuses an `--out`
  resolving inside the repo. Three layers because this repo has a
  private→public mirror workflow (`PUBLISHING.md`) where one `git add -A` would
  publish patient-note excerpts. (The `/eval/` pattern is **root-anchored**: a
  bare `eval/` matches at any depth and silently excluded the harness's own
  tracked source under `tests/eval/` — caught doing exactly that.)
- **Prompt fencing hardened** from a literal delimiter to a per-call `uuid4`
  nonce (`wrap_untrusted`'s convention), since vault content includes
  downloaded-PDF text and web clippings that can contain a fixed marker.
  Output dirs are created `0o700` (files were already 0600 via `mkstemp`).

### Audit remediation — the index gate now REFUSES instead of rebuilding

`obsidian_meta.json` gained **`embed_provider`** — which LOCAL backend produced
the vectors (`ollama` vs `lm_studio`), since one model name can be a differently
quantized/pooled build across runners. The first cut *forced a full rebuild* on
a mismatch, which was the most dangerous line in the change set:
`resolve_embed_provider` returns the *chat* provider unchanged when it is local,
so `ollama ↔ lm_studio` — or `lm_studio →` any online provider, falling back to
the untouched `embed_provider` default — would have silently archived and
re-embedded a 5.4 GB index (hours) from a chat-dropdown change that looks
unrelated to embeddings, with `keep=2` pruning meaning three toggles destroyed
the original.

- The gate now **stops the run** with an actionable error naming both backends,
  the chunk count, and both exits (switch back, or move the index aside).
  Nothing is archived, deleted, re-embedded or backend-migrated.
- `index_vault(..., force_fresh=True)` is the explicit opt-in (manager-level
  hook; not yet on `POST /api/obsidian/index`).
- **Grandfathering keeps the evidence** (`_prev_embed_provider`): recorded
  `embed_provider` → else the recorded `provider` **when local** (it predates
  the new field and, when local, is by definition what `resolve_embed_provider`
  would have returned) → else adopt silently. Without the fallback the first
  post-upgrade run — the one most likely to *be* a provider switch — would mix
  vector spaces and stamp over the evidence. An online recorded `provider` is
  deliberately not a usable proxy. **No existing index is forced to reindex.**
- **Backup retention split**: identity-driven archives (vault switch,
  `force_fresh`) are marked protected with their own window
  (`_INDEX_BAK_PROTECTED_KEEP = 5`), so routine version-bump churn (`keep=2`)
  can never evict the archive a user needs.

### Audit remediation — the sweep can no longer corrupt settings

- **Durable config restore.** The backup used to live only in RAM while the
  restore used a truncating, non-fsynced `write_bytes` — the guarantees were
  inverted (the disposable write was atomic, the corrective one was not). The
  original bytes now land in `config.json.evalbak` (atomic + fsync'd) before the
  first override, restored via `write_bytes_atomic` from the per-cell `finally`,
  the outer `finally`, `atexit` **and** a `SIGTERM` handler, verified by
  read-back before the sidecar is removed. A surviving sidecar makes the next
  run refuse to start, printing both paths — deliberately not auto-restoring.
- **Cell values are validated, not just key names.** An out-of-range or
  malformed value aborts with exit 2 naming the key and its range, mirroring
  `api/routes/config.py`'s map (with a parity ratchet) and *rejecting* rather
  than clamping, since a clamped cell would report a Δ for a setting that never
  existed. `reranker_model` gets the app's HF-repo-id shape check — that value
  reaches an arbitrary model download, which the app forbids as a body override.
  An unparseable config now aborts instead of being replaced by a file
  containing only the override keys.
- **Refusals replacing silent nonsense:** a live app (detected via
  `core/server_info.py` + `/api/health`, the definition `papermind server`
  already uses — `--yes` does not bypass it); a `cfg["embed"]` that differs from
  the index's recorded model (every query would search a foreign vector space
  and the banner printed the *index's* model, concealing the cause — the README
  had promised this check for a week without it existing); a reranker preset
  with no model configured.
- **Two of five presets were dead.** `cutoff-sweep` was a 100% no-op —
  `SimilarityPostprocessor` attaches only with no reranker *and* no fusion,
  while the defaults enable both, so four cells printed Δ = 0.000 for ~20
  minutes and read as "the cutoff doesn't matter" (false). It now switches the
  blockers off and is documented as measuring the dense-only path.
  `pool-sweep`'s 100/200 ceilings both clamped to 40; it now co-varies the
  multiplier, and the 200 cell (which provably cannot bind) is gone. A general
  **inert-override detector** flags any cell whose override cannot reach an
  active code path, loudly, next to its row.
- **Reranker state is a positively-derived tri-state** (`none|loaded|failed|
  unknown`), replacing a sticky flag that lied in both directions — a
  `reranker_enabled:false` cell inherited the previous cell's failure, and a
  cell whose model resolved empty ran rerank-free while being scored *as* the
  reranked baseline, making every Δ a comparison against a mislabelled
  reference. **Results schema v2**; the old boolean is removed, not aliased.
- **Leakage guard**: a generated query sharing >5 consecutive tokens with its
  source chunk is dropped and counted. Verbatim copying scores near-perfectly on
  BM25 and not on dense retrieval, so without this the RRF sweeps rewarded
  upweighting BM25 on a generator artifact — biasing the exact comparison the
  harness exists to run. The prompt had *asked* for this; nothing verified it.
- **Per-query failures exclude that query** rather than scoring it as a miss (a
  miss is a retrieval outcome; a failure is the absence of one); five
  consecutive failures abort the cell, not the run.

### Audit remediation — performance

- **`docstore_sources()` no longer parses 1.456 GB to build a 9 664-string set**
  (~7.2 GB peak, 11–20 s). Fallback chain: `indexed_materials.json` (~1 MB) →
  ijson-streamed `ref_doc_info` → full load. Every tier *raises* rather than
  returning empty, so a truthy-but-empty read can never be mistaken for "the
  index is empty" and drop the whole golden set.
- **Results are checkpointed after every cell** (`partial: true`, cleared on the
  final write) — a 24-minute sweep killed at cell 4 used to lose everything.
- **Progress and diagnostics**: `stage_cb` threaded into `retrieve()`, a
  per-query counter with rolling ETA, `drain_status_messages()` echoed per cell
  (the manager's warnings previously went to a 200-line ring nothing reads), a
  pre-flight cost estimate with the ±1/√N noise floor, and a `--no-warmup`
  warning that cell 1's latency then includes the model load.
- **Generation parallelised** (bounded pool of 4) with ids assigned after the
  join so they stay deterministic under out-of-order completion, plus a
  per-chunk wall-clock bound and a resume sidecar.

### Audit remediation — UX / accessibility

- **The reranker hint contradicted the new docs.** `templates/index.html` said a
  change "may trigger a model download on the next chat" — impossible, since
  `launch.py` sets `HF_HUB_OFFLINE=1` from the *configured* model at launch, so
  a mid-session switch fails and silently disables reranking. It now states the
  restart requirement and the ~2.3 GB one-time download.
- **The reranker load had no heartbeat**, so a first-time 2.3 GB download
  guaranteed an SSE stall timeout at 330 s (`SSE_SINGLE_SHOT_FLOOR_S` +
  `SSE_STALL_MARGIN_S`) — the user saw "Thinking…" for 5½ minutes and then *"the
  model may be overloaded"*, blaming the wrong thing, while the download
  continued and a later retry mysteriously worked. `_get_reranker` now beats
  through `stage_cb` (the BM25 P4b pattern), and the load-failure and MPS→CPU
  warnings are routed to the chat's SSE `{info}` channel — they previously went
  to a status ring the client polls **only while indexing** and renders only
  `warnings` from, so a typo'd model disabled reranking for the whole session
  with zero user-visible signal.
- **Silently dropped config values are now named.** `POST /api/config` returns an
  additive `dropped` list and `settings.js` marks the offending control
  (`aria-invalid` + inline error) and announces it — a WCAG 3.3.1 failure where
  the error was detected and never identified. Out-of-range numerics are
  *clamped* and deliberately not reported; only genuine discards are.
- **The settings filter counted matches it never revealed** — `set-reranker-model`
  lives in a nested `<details>` the filter never opened, so searching
  "reranker" (the documented discovery path) reported N matches, announced them
  to screen readers, and showed nothing. It now opens every ancestor.
- **Dynamic `role="status"`** on vault info notices removed in favour of the
  static `#sr-status-announcer` — the exact defect class `liveRegions.test.js`
  exists to prevent, and what makes the new reranker heartbeat audible.
- The 364-char reranker tip became a **popover** (focusable, scrollable,
  selectable — it carries a model id to copy) with a new ratchet capping tips at
  220 chars; `wireControlHints()` wires `aria-describedby` for 60 of 71
  previously AT-invisible `control-hint`s; the help badge gained a 24×24 hit
  area (WCAG 2.2 SC 2.5.8) without moving the 16×16 dot; three contrast pairs
  joined the token lint; and the in-app guide stopped telling packaged-app users
  to run `scripts/` that `install_and_build.sh` does not bundle.

### CI / docs

- `scripts/gen_golden_retrieval.py` + `scripts/eval_retrieval.py` joined the
  py_compile sweep (the `scripts/` tree is not globbed — harness entry points
  are named individually, like `install_mcp.py`), and the new
  `tests/eval/test_{retrieval_metrics,golden_gen,eval_runner}.py` joined both
  the CI list and the CLAUDE.md quick-gate. Drive-by: `tests/eval/test_eval_pipeline.py`
  had been running in **neither** list since it was written, despite a CHANGELOG
  entry claiming it was CI-driven — added to both. README's copy of the
  quick-gate had also drifted eight files behind CLAUDE.md's; the two now match.
- Three documentation claims that described behaviour the code did not have were
  corrected (the embed-model check, "nothing is written to the index", and the
  gate's rebuild semantics). A doc that promises a safety property the code
  lacks is worse than silence — a reader who trusts it stops checking.
- **Tests:** 41 (`test_retrieval_metrics.py`) + 47 (`test_golden_gen.py`) + 83
  (`test_eval_runner.py`) + 9 + 5 (`TestEmbedProviderStamping`,
  `TestRerankerLoadVisibility`) + `test_03e` (`smoke_test.py`, the `dropped`
  contract) + 4 JS files. All hermetic — fakes, tmp docstores, no models, no
  network; the config backup/restore cycle is exercised against a tmp path
  including a simulated mid-cell exception.

## 2026-07-23 (four-pillar audit of batches B–H — four remediation batches)

A four-agent audit (logic / security / performance / UX-a11y) of the
2026-07-22 range surfaced ~30 findings; fixed in four batch commits, suites
green after each (1420 Python + 307 JS):

- **Batch 1 · retire-tag data safety:** `journal.prune` gained
  `protect_batch_id` so a >200-note retire can no longer evict its OWN
  batch's ops + snapshots in the same request that promised one-click undo;
  `batch_id` is stamped inside the remove writers before each per-op save
  (the after-the-fact stamp lost the last op's stamp on cancel); a failed
  `_tags.md` write flips the `vocab_retire` op to `failed` and zeroes the
  reported `vocab_rows`.
- **Batch 2 · retire-tag lock choreography:** four phases — Zotero GETs
  pre-lock, vault writes + journal appends under ONE hold (manifest
  checkpointed every 25 ops via the writers' `defer_save`), Zotero PATCHes
  **lock-free** (the `If-Unmodified-Since-Version` guard, not the op-lock,
  prevents lost updates — previously N sequential PATCHes froze every vault
  write), then a short second hold that RELOADS the manifest and records
  each PATCH outcome (a failed/cancelled PATCH never stays an "applied"
  claim). Heartbeat `False` now aborts (retire + batch restore). Deferred
  items recorded in CLAUDE.md Known Issues: the piecewise N-turn
  `LATEX_OP_LOCK` hold, the synchronous batch restore, the bounded phase-4
  re-acquire.
- **Batch 3 · a11y/UX:** guided tour (aria-modal lie removed, focus lands on
  the labelled card so SR users hear the step content, document-level
  Escape, focus restore, per-step `onEnter` hook that lazy-loads the Help
  guide); ⌘K palette (full combobox pattern with `aria-activedescendant`,
  Tab trap, Escape-after-Tab, focus restore); refactor piecewise
  announcements made dual-channel; Help tab loads on arrow-key activation;
  a second retire no longer destroys the first batch's Undo button (all
  [retire] buttons disabled during a run; confirm states the `_tags.md`
  edit + the stores retire does NOT touch); visible skip-caveats beside all
  four piecewise offer buttons. New `tourA11y.test.js` + extended palette
  tests.
- **Batch 4 · hardening:** the Tavily client refuses redirects (the default
  handler carried the Bearer key to the Location target) and caps the
  response read at 2 MB before parsing; `web_search_enabled: true` in the
  body now actually enables the tool (the gate re-read the config knob);
  the deck/docgen size-cap refusal carries `code:"size_cap"` and the
  clients key the piecewise offer on it (never on regexing the prose);
  `repair-piecewise` requires `base_sha256` and no longer drops
  issues-but-no-repair sections from the skip accounting; tagging Zotero
  503 skips carry the actionable no-key detail; the Restore projection
  shows `removed`/`batch_id`; `check_tex.py` compiles with
  `-no-shell-escape` + treats unreadable `.tex` as failures;
  `audit_pdf_names.py` is case-sensitive.

Documentation pass the same day: `docs/feature_plan_2026-07-22.md` deleted
(shipped in full; its one deferred item — provider-native web-search v2 —
is recorded in CLAUDE.md Known Issues), PROMPTS.md re-reviewed, README /
help guide / subtree CLAUDE.md files synced.

## 2026-07-22 (five-axis feature plan — eight batches, A–H)

The maintainer's five feature axes (web search, guidance, size-limit recovery,
export provenance, tag deletion), planned in a since-removed
`docs/feature_plan_2026-07-22.md` (deleted 2026-07-23 after shipping; deferred
items live in CLAUDE.md Known Issues) and shipped one commit per batch:

- **A · Export provenance:** `{"meta"}` (resolved provider/model/params,
  pre-token) + best-effort `{"usage"}` (real tokens/cost via
  `usage_tracker.records_since`) SSE frames on plainchat/summarise/vault-chat;
  clients capture them — plus the full scored retrieval objects and the agent
  tool-call trace — onto stored turns, and `buildConversationExport` renders a
  provenance header, per-turn Model/usage lines and the tool trace into every
  MD/TXT export. Deck/article `.tex` gets a provenance comment header (articles
  add a commented-out ICMJE-style disclosure); docgen vault notes get
  provenance frontmatter.
- **B · Per-paper tag deletion:** `remove_tags_from_frontmatter` (per-shape
  splices, `before − removed` round-trip guard), the three removal writers
  (Finder survivors keep colours; Zotero version-guarded full-array PATCH),
  `note_untag`/`finder_untag`/`zotero_untag` journal ops reverted by the
  existing conservative restores, `POST /api/tagging/remove-tags` (apply-all's
  twin, deliberately NOT vocabulary-gated), and removable current-tag chips
  (×) in the tagging table and deck.
- **C · Agent web search (online providers only):** `core/agent/web_tools.py`
  — a Tavily-backed client-side `web_search` ToolSpec behind a triple gate
  (`vault_web_search_enabled` off-by-default + online chat provider +
  `TAVILY_API_KEY` env), results wrapped as untrusted content, timeout clipped
  to the turn's wall clock; registered in vault chat (body override, info when
  unavailable) and deckgen's in-process runner. `tvly-` added to redaction.
- **D · Piecewise note edits (offer-on-422):** `refactor/piecewise.py` +
  `POST /api/refactor/rewrite-piecewise` (SSE) — one bounded LLM pass per
  heading section, back-to-front splice, ONE staged combined proposal through
  the unchanged apply-staged guards; the whole-note 422 now offers "Edit
  section by section".
- **E · Piecewise deck/docgen augment (offer-on-refusal):**
  `deckgen.augment.replace_sections` + per-section agent turns behind
  `piecewise:true`; the size-cap refusal offers "Revise section by section";
  combined proposal rides the unchanged screen/staging/apply-augment pipeline.
- **F · Piecewise deck auto-repair:** section-scoped review prompts/parse in
  `deckgen/review.py` (whole-document replies refused) +
  `POST /api/deck/repair-piecewise`, surfaced as an `{"augment"}` frame so the
  existing apply-augment writes it; offered when the whole-deck repair is
  withheld (`truncated`/`repair_truncated`).
- **G · Retire a tag everywhere:** Tag Hygiene rows gain `[retire]` — one
  confirm removes a tag from every note/PDF/Zotero item (Zotero reads
  pre-lock; one lock hold with heartbeats), optionally prunes the `_tags.md`
  row (`vocab_retire` snapshot op), stamps a shared `batch_id`, and
  `POST /restore {batch_id}` (one Undo button) reverts the whole batch.
- **H · Help & discoverability:** in-app Help tab rendering
  `static/help/guide.md`, "?" badges over the vault-chat/agent knobs, a
  dependency-free guided tour and ⌘K command palette (`ui.js` primitives,
  `app.js` registry), README/CLAUDE.md sync, and the How-to modal's egress
  claim updated for the opt-in web search.

## 2026-07-22 (full audit remediation — eight batches, A–H)

Fixes ~60 findings from a six-agent end-to-end audit (backend, RAG, LLM layer,
feature subsystems, CLI/MCP/build, frontend UX/a11y), one commit per batch:

- **A · Data safety:** refactor journal refuses to overwrite an unreadable
  manifest (`JournalUnreadableError` backport; the old fresh-empty fallback
  destroyed the restore history incl. archive_image mappings); deck compile-fix
  backup routes through `_backup_deck` (the inline copy destroyed hand-written
  originals on the second run); Finder-revert read failures tally `failed`
  (prune-safe); the stale sweep protects empty-vision-description images and
  images referenced by unreadable notes; a failed changed-chunk delete skips
  the re-insert (no permanent duplicate vector rows); dangerous-macro screens
  gate on occurrence COUNTS; deck/docgen figure copies moved past the cancel gate.
- **B · Config/LLM:** pricing-override crash fixed (coercion + a never-break-
  the-caller guard in `record()`); `llm_pricing_overrides` shape/size-validated;
  `online_timeout_s` joined the SSE timeout chain; `/api/config` array-body 400,
  `{}` no-op, deferred vault switch, provider-case normalization; vault-chat
  `message` validated; `/api/health` redacted; `stream_with_fallback` cfg-load +
  coerce net; cancel-aware jittered backoff; Gemini cached-input pricing.
- **C · CLI/MCP full repair:** `BASE_DIR/server.json` discovery (GUI's random
  port reachable; second server refused), generate tools actually work
  (server-side template load + `template_tex`/`out_dir`), no-op params removed
  or wired, MCP response guarantee/-32602/busy-bounded write semaphore/markdown-
  write classification, unified status shape, `--unsafe-bind` removed,
  `python -m cli` + `scripts/install_cli.py` shim, contract ratchet v2
  (path+method + a request replay harness that catches body drift).
- **D · RAG/concurrency:** broad generator guard on the primary md parse; the
  status poll's lancedb probe moved off `_status_lock`; post-index background
  BM25 rebuild + build heartbeat frames; half-open SSE consumers abandoned at a
  ceiling; budget-bounded PDF-cache reads; NFC/NFD image dedup; audit search
  corpus drops superseded builds; query-expansion-under-lock documented.
- **E · UI feedback/ceremony:** Single-Paper renders map-reduce info frames
  (persistent coverage notices); backend death is visible; tagging-deck failure
  retry; confirm ceremony unified (ambig table, Compile & Auto-Fix, refactor
  staged-apply/Archive, Cancel-first pull modal); UI chrome standardized to
  English (example chips stay French by design).
- **F · A11y:** keyboard chip removal, full ARIA combobox, roving-tabindex
  repair in the refactor sidebar, deck `role=group`/Space-safe/Escape-closes-PDF,
  live-region hygiene (single announcer channel), labels, visible-only focus
  trap, full-title aria-labels, accent-token dropzone.
- **G · Responsive/settings:** Single Paper stacks ≤900px; measured tab-strip
  overflow fade; LLM Settings live filter (~84 controls).
- **H · Docs/build:** stale comments repaired, load-bearing "why" comments
  added, CLAUDE.md/README/CI synced (CLI/MCP story, ratchet v2, honest CI
  superset note), build gaps closed (`tagging`/`docgen`/`cli`/`mcp_server` in
  `--add-data`, dead `pydantic_settings` flags dropped, non-interactive guard,
  eval-quoting normalized, DMG READ-ME lists all key env vars).

## 2026-07-21 (multi-disciplinary audit remediation — six workstreams)

Fixes the findings of an end-to-end audit (code/perf/UX-a11y/security) across the
last 10 commits, in six workstreams:

- **Data integrity (B):** `ObsidianVaultManager.heartbeat()` now RETURNS the
  lost-lock bool and the refactor batch writers (apply/normalize/restore) act on
  it — a lapsed-then-stolen op-lock aborts cleanly instead of writing as a second
  concurrent writer; docgen's `compute_job_key` gained `images_enabled` (stale
  checkpoint-resume parity with deck); an unreadable *sub*-directory is recorded
  as a failed prefix so its subtree is withheld from the stale sweep (no durable
  partial purge); corrected the Anthropic `_build_payload` docstring.
- **Triage-deck logic + a11y (C):** fixed the double-advance race (navigating
  during an in-flight write no longer skips a card), the undo stack surviving a
  view toggle (keyed on scan identity), stale PDF-panel writes; added focus
  retention + card announcements + `aria-label`/role, moved live regions off
  render-time minting, fixed dark-theme candidate-row contrast (M2, lint pairs
  added), search jump-to-row, a Close-vs-Clear affordance, and busy feedback.
- **Performance (D):** the cross-report search corpus builds OFF the request
  thread (202 + retry) so a slow build can't convoy the WSGI pool; the mapping
  POST no longer re-hashes every curated PDF (moved to scan time); a manager
  result-listener drops the search memo promptly; the Zotero restore path's read
  + backoff sleep runs BEFORE the obsidian op-lock.
- **Security (E):** Zotero requests refuse redirects (no API-key forwarding) and
  the web base is https-validated at call time; the redact patterns cover
  quoted/colon/dict-repr key shapes; the LaTeX macro denylist gained
  `\VerbatimInput`/`\import`/`\subimport`/`\includepdf`/`\inputminted`; the
  `style` `url()` strip de-escapes CSS escapes + catches `image-set`; the audit
  reveal-`open` check is a fail-closed document-type allowlist.
- **CLI/MCP (A):** `core/driver.py` rewritten HTTP-first — it dispatches to the
  running app with the `X-Requested-With` header (the first cut 403'd every
  request into a broken in-process branch that called phantom APIs), writes
  require the running server, MCP write tools use the same confirm-gated
  reversible endpoints as the GUI, and `tests/test_driver_contracts.py` is a
  ratchet asserting every targeted endpoint/symbol actually exists.
- **CI/docs (F):** the CLI/MCP tests are now in CI + the documented suite, and
  the compile sweep covers `cli/`, `mcp_server/`.

## 2026-07-20b (Zotero tag writes unblocked: web-API transport)

Resolves the standing Known Issue that the Zotero write target "cannot work
today". The Zotero 9 blocker is real and unchanged — the local API answers GET
but returns **501** on PATCH — so `tagging/zotero_write.py` gained a second
transport rather than a workaround. Verified live end-to-end against the real
library (create → PATCH 204 → `type: 1` metadata preserved → stale version 412
→ synced down to desktop Zotero 9.0.6 → deleted). `test_tagging.py` 91 → 105,
`test_llm.py` +2, `smoke_test.py` +2; full suite 1298 green, `tests/js/` 273.

- **Two transports, one wire format.** `resolve_transport(cfg)` selects the
  loopback API or `https://api.zotero.org` per the new
  `tagging_zotero_transport` (`auto`｜`local`｜`web`, default `auto` = web when
  a key is present, else local). Every existing helper — the version guard,
  the raw-dict passthrough, the `ZoteroVersionConflict` → 409 mapping — works
  unchanged on both; only the base URL, library path and auth header differ.
  Nothing regresses when Zotero 10 ships PATCH: flip to `local`.
- **Real userID resolution.** api.zotero.org rejects the local API's magic
  `users/0`, so `resolve_user_id` looks up `GET /keys/current` (cached under
  the key's sha256 fingerprint, write-permission checked). `groups/<id>` is
  identical on both transports.
- **Key hygiene.** `ZOTERO_API_KEY` is read from the environment at call time,
  never persisted to `config.json`, never returned by `/api/config`, and
  **never journalled** — the restore manifest records only the transport, and
  the revert re-reads the key (pinned by a test asserting the key is absent
  from the manifest JSON). Two contextual patterns added to
  `core/llm/redact.py` (a bare 24-char alnum key can only be matched in
  context — a naive pattern would redact item keys and sha fragments).
- **Sharper failure vocabulary.** `ZoteroWriteUnsupported` (501, with an
  actionable "configure a key" message instead of an opaque 502),
  `ZoteroAuthError` (401/403), `ZoteroRateLimited` (429/503 with
  `retry_after_s`) — all subclassing `ZoteroError`, so existing handlers are
  untouched. The web API's `Backoff` header is honoured, bounded, on **read**
  paths only: the write path holds the obsidian op-lock, where sleeping would
  stall indexing.
- **Honest availability.** `zotero-available` now probes the *resolved*
  transport and reports `{transport, writable, note}`; a reachable but
  unauthenticated web endpoint counts as unavailable rather than offering a
  button guaranteed to 503.
- **Documented gotcha.** A create/write response reports the *library*
  version, not the item's — observed live (POST returned 77060 while the item
  was 77061, so a PATCH reusing it 412'd). Always re-read before writing.

**Caveat:** a web write lands on zotero.org and reaches the desktop only on its
next sync, so "applied" is not "visible in Zotero now".

## 2026-07-19b (Audit remediation: Finder colours, keyboard confirm, apply-all truth, a11y/responsive)

Fourteen commits resolving the full 2026-07-19 audit (the `AUDIT_19_07_2026.md`
findings plus the deeper same-day review: data loss, keyboard trap, contract
breaks, concurrency, a11y/responsive, layering). `test_tagging.py` 76 → 85,
`tests/audit/` +3, `test_thesaurus.py` +1, `tests/js/` 212 → 228.

- **P0 data loss — Finder tag colours.** The union writer read NAMES and
  rewrote the whole `_kMDItemUserTags` array, flattening every colour the user
  assigned in Finder; a failed xattr read looked like "no tags" and clobbered
  the set. Now the Zotero `before_raw` pattern: raw entries round-trip
  verbatim (journal stores `before_raw`/`after_raw`; Restore returns colours;
  legacy name-only ops still revert), a failed READ raises and refuses that
  PDF, and `xattr` is invoked by absolute path.
- **P0 keyboard trap.** The deck's container-level verb handler cancelled the
  confirm strip's native button activation (Enter re-fired Accept and
  re-opened the strip forever — confirm was mouse-only). `confirmInline` now
  owns its keys (Enter/Space stop propagating; Escape dismisses + refocuses)
  for every consumer; the deck also ignores keys from inside a strip / on a
  focused button (Space on Skip no longer Defers).
- **P0 apply-all truth + undo integrity.** A late-target crash after the note
  write had committed + journalled returned HTTP 500 — the deck reported total
  failure and never learned the note op's id. Per-target exception isolation
  (HTTP 200 with per-target truth) + the client processes `results` whenever
  present, so op_ids always reach ⌫.
- **P0 security-gate gap.** `parse_tags` skipped description-less rows, so a
  gloss-less `_tags.md` tag could neither be proposed nor applied (the vocab
  IS the write gate). `parse_tags(include_empty_desc=True)` for
  `build_vocab` only (thesaurus byte-identical); space-containing tag ids are
  rejected at the gate (they tear apart in the whitespace-splitting parsers).
- **P1 concurrency.** Propose cancel reaches the embed phase (polled between
  stages); client abort timeouts on propose/apply-all fetches (a hung fetch
  pinned `_deck.busy` forever — also reset on deck re-entry); fresh PDF
  extraction single-flights on a server lock + client promise-dedup (rapid
  skipping stacked concurrent PyMuPDF scans of the same file); Zotero's GET
  runs BEFORE the op-lock (`_zotero_prepare`/`_zotero_commit_locked` — the
  version-guarded PATCH makes the early read safe; unreachable = 503 with no
  lock touch); apply-all shares one manifest load + one prune (was 3× full
  rewrite cycles per Accept); `gloss_embeddings` gets real double-checked
  locking; Finder reverts count `failed` separately (a partial revert no
  longer spends the op and its before-lists); audit.js re-checks its
  latest-wins gate after the tagging await (cross-report handler double-bind).
- **P2 a11y/responsive.** AA contrast (suggest gloss on hover, chip numbers,
  badge-muted, managed `::placeholder`, disabled-toggle colour tier — all
  pinned as contrast-test pairs); `:focus-visible` rings on chips/suggest
  items/note entries; deck PDF grid stacks <980px, `.input-bar` wraps,
  `.control-grid` collapses <700px, deck/docgen pane-views scroll instead of
  clipping stacked cards; the type-ahead dropdown un-clipped (tagging opts the
  report body out of its coerced overflow clip, own table wrappers);
  `window.taggingTeardown` revokes the PDF blob on report switch; each new
  card announced to the SR region; vocab+Zotero probes load in parallel with
  staged text; inventory failures get a Retry error boundary; disabled targets
  carry their reason as `aria-label`.
- **P3 hygiene.** Frontmatter merge: empty `---\n---` recognised (no more
  double block), singular `tag:` refused (no parallel key), round-trip guard
  extended to sibling keys; `_MIN_USEFUL_CHARS` deduped (route imports it) and
  the empirical constants (`0.85` neighbour cutoff, snippet/deadline sizes)
  documented; broad excepts narrowed/log-raised. **Extraction:** the domain
  writers moved to `tagging/writers.py` (route: ~1,360 → ~850 lines, HTTP-only;
  private names re-exported for test-patch compat).

## 2026-07-19 (Tagging: extraction fix + deck UX + Tag Hygiene + PDF panel + propose upgrades)

Five independent commits fixing the triage deck's "Loading excerpt…" stall and
landing the four planned tagging improvements. `test_tagging.py` 49 → 76,
`tests/audit/` +8, `tests/js/` 203 → 212.

- **Extraction fix (the stall).** `text_source`'s 20 s "budget" was a no-op
  (equal to `_read_pdf_text`'s start-refusal floor, which never bounds a
  started extraction): a cache-miss scanned PDF cost a synchronous ≤1000-page
  PyMuPDF sweep + a whole-file MarkItDown/pdfminer pass per card, never
  cached, re-paid by Propose (→ 504). Now: `extract_structured_from_pdf`
  gains `use_markitdown` (all three fallback tiers gated, source-ratcheted),
  `_read_pdf_text` gains keyword-only `fresh_max_pages`/`use_markitdown`
  (`truncated` derived from `page_count`), snippet extracts ≤3 pages /
  propose ≤8 with MarkItDown off, and a no-text fresh extract writes a
  `<sha256>.notext` negative-cache marker beside the PDF text cache (real
  caches — whole-file, legacy, per-range OCR — always win).
- **Deck perceived latency.** Module-scoped snippet cache (survives re-renders
  and the Deck↔Table toggle) + sequential prefetch of the next card's
  snippet; Accept advances immediately after the confirm and reconciles the
  apply-all outcome (failures included) into a deck-level status line that
  survives the card change; Undo stays busy-gated (the undo stack fills
  post-response).
- **Tag Hygiene report** (9th audit tab, read-only, on-demand at GET):
  cross-store tag frequencies (Finder / note YAML / Zotero parent / Zotero
  notes / bib keywords) grouped by an OpenRefine-style fingerprint, vocabulary
  flags via tagging's `_read_vocab` (same `_tags.md` resolution), near-miss
  neighbours (`difflib` ≥ 0.85), surface-variant clusters, top co-occurrence
  pairs. Fingerprint helper deliberately local to the vendored report module.
- **Deck PDF side panel (V).** `GET /api/tagging/pdf?citation_key=` serves the
  key's biblio PDF (inventory-derived, realpath re-checked under the resolved
  biblio dir) + `POST open-pdf` macOS fallback; client fetches with the
  header and renders via a blob: URL (never an `/api` iframe src —
  source-ratcheted like the deck preview), on demand only, closed + blob
  revoked on card change.
- **Propose upgrades** (both optional, both degrade to the historical prompt
  on any embed failure): `tagging_candidate_tags` (default 20, 0 = off) —
  label-space reduction via gloss/paper embeddings, disk-cached per
  `_tags.md` mtime + embed model; `tagging_fewshot_k` (default 3, 0 = off) —
  the k most similar already-accepted papers from the tagging journal
  injected as `title → accepted tags` few-shot lines. `tagging/candidates.py`
  is the first embedding caller outside `rag/` (query-path template: 30 s
  construction bound, `resolve_embed_provider`).

## 2026-07-18b (Propose truncation root-cause + audit-fix batch)

Root-caused the field-reported "the model returned no tags" (live evidence:
`llm_usage.jsonl` showed Gemini propose replies of 7–12 output tokens — the
whole `maxOutputTokens` budget went to *thinking*) and fixed the 18-finding
audit of the last 10 commits. `test_tagging.py` 42 → 49, `test_llm.py` +2,
`tests/js/tagging.test.js` 15 → 18.

- **Propose truncation (the actual bug).** `tagging_propose_max_tokens` default
  → **1536** with a **CONFIG_VERSION 2→3 migration** resetting a persisted 300
  (the old materialised default — a raised code default alone never reaches an
  existing install, which is why the earlier 640 bump changed nothing on the
  reporting machine); route fallbacks now read `core.config._DEFAULTS` (the
  stale inline `or 300` contradicted the config layer); Settings gained
  max-tokens/temperature fields (the knob was un-fixable from the UI). A
  `thinkingConfig` hard budget was deliberately rejected: Gemini pro 400s on
  budget-0 and 3.x uses a different knob — headroom is model-agnostic.
- **Diagnosability.** A 0-tag propose parse now logs the raw reply length +
  preview (`tagging/propose.py`); usage records persist the **`workflow`** tag
  (all rows previously read workflow-less, making per-feature attribution
  impossible); `record_local_usage` takes `workflow=` too.
- **Security.** `RedactingFilter` on every log handler: `httpx` logs Gemini
  URLs *with the API key in the query string* — the raw key sat in
  `chatekld.log` on disk (read-time redaction only covered `/api/log/tail`).
- **Zotero metadata preservation.** The tag writer now reads full tag dicts
  (`get_item_tags_full`) and PATCHes existing tags verbatim (+ journals
  `before_raw` for Restore) — the name-only round-trip was silently converting
  every *automatic* tag (`type: 1`) to manual on each write and each revert.
- **Finder writer/revert.** `after` unions `added` not `valid` (no `dep`/`Dep`
  case-duplicate xattr); an all-skipped/all-failed revert keeps the op
  `applied` (retryable, prune-safe — flipping it destroyed the only restore
  mapping without restoring); per-PDF revert failures are logged.
- **Deck (JS).** Per-card `proposed` reset (1–9 keys could inject the PREVIOUS
  paper's tags); auto-advance timer is stored/cancelled on any explicit
  navigation + current-key-guarded (no more silent double-skip); undo is
  confirm-gated (Track 6e — it rewrites vault data), peeks-not-pops (a fully
  failed undo stays retryable), un-patches the cached row per reverted target,
  and works from the done screen; snippet fetch checks `r.ok` and never caches
  errors (a transport failure no longer renders — stickily — as "no extractable
  text"); keyboard verbs survive checkbox focus; the Restore panel's outcome
  line is visible (was SR-only).
- **Misc.** Propose single-flight gate released if `Thread.start()` raises
  (was a permanent-429 leak); `apply-note-tags` single `get_json`; apply-all's
  note-409-continues-to-other-targets behavior + `/snippet`'s no-gate choice
  documented as deliberate; stale-sweep docstore-read invariant + named
  `_STALE_PROGRESS_EMIT_S` cadence in `rag/vault.py`.

## 2026-07-18 (Biblio Tagging — Triage deck + visible Propose + fan-out apply)

Reworks the Biblio Tagging UI around a fast, keyboard-first triage flow and fixes the
long-standing "Propose gives no answer" report (the result was only ever announced to a
screen-reader-only region, so a sighted user saw nothing). +7 Python tests
(`test_tagging.py` 35 → 42), +4 JS tests (`tests/js/tagging.test.js` 11 → 15; full JS
suite 191 → 195).

- **Visible Propose/Apply feedback (the "no answers" fix).** Every Propose and Apply
  outcome now writes to a **visible status line** (in both views) as well as the SR
  announcer — including `no_text` (scanned/unreadable PDF), `empty` (model returned no
  tags), `low_confidence`, and provider 502/504/429 errors. Previously all of these were
  silent to a sighted user; a working provider looked broken.
- **Triage deck** (new default view, toggle persisted in `localStorage.tagging_view`;
  the original **Table** stays as the overview). One card at a time, **untagged-first**,
  with a PDF-text excerpt (`POST /api/tagging/snippet`, cache-first). Keyboard-first:
  `P` propose · `A`/`Enter` accept · `S`/`→` skip · `M` defer (to a review queue) · `⌫`
  undo (via `/restore`) · `←` back · `1`–`9` toggle proposed chips.
- **`POST /api/tagging/apply-all`** — Accept fans one confirmed decision out to **all
  enabled+available targets at once** (note + Finder + Zotero) under a single lock hold.
  Each target still journals its **own reversible op** (Restore stays per-target
  granular) and re-validates against `_tags.md`; an absent (no note / no Zotero item /
  no biblio PDF), off-platform (Finder off macOS) or unreachable (Zotero) target is
  *skipped with a reason* rather than failing the whole write — the primary note write
  still lands. The three standalone `apply-*` handlers were thinned to wrappers over
  shared `_prepare_tag_write` + per-target `_*_write_locked` helpers that `apply-all`
  reuses (standalone behavior byte-identical, pinned by `test_tagging.py`).
- **`tagging_propose_max_tokens` default 300 → 640** so a reasoning model (e.g. a Gemini
  "thinking" model) does not exhaust its budget on thoughts and emit empty JSON.
- **`POST /api/tagging/snippet`** — a bounded, read-only, cache-first PDF-text excerpt
  for the deck card (`{citation_key}` → `{snippet, truncated, chars}`).

## 2026-07-15 (Biblio Tagging — new Library Audit sub-view)

A new **Biblio Tagging** feature on `feat/biblio-tagging`: a Library Audit sub-view
(the "Tagging" report tab) that turns the audit's per-citation-key `Record` join into
a *write* surface for tags, scoped strictly to `biblio_articles/`. Six slices, each
committed with hermetic tests (35 new Python tests in `test_tagging.py`, 11 JS in
`tests/js/tagging.test.js`; the full JS suite went 180 → 191).

- **New package `tagging/`** (core/route split like `refactor`/`deckgen`): `vocab.py`
  (pure `_tags.md` → `{tag: gloss}`, reuses `rag.thesaurus.parse_tags`), `text_source.py`
  (cache-first PDF text via the indexer's `_read_pdf_text` — OCR text for scanned PDFs),
  `propose.py` (the only chat-LLM caller — one vocabulary-constrained classification,
  paper text wrapped untrusted, reply re-constrained to real tag ids), `journal.py`
  (self-contained restore manifest outside the vault, modelled on `refactor/journal.py`),
  `zotero_write.py` (stdlib-urllib local-API client, version-guarded PATCH).
- **Three write targets, all confirm-gated / vocab-gated / op-locked / journalled /
  reversible:** the Obsidian note frontmatter (reuses `_audit_tagwrite`), the PDF's macOS
  Finder tags (`api/routes/_finder_tagwrite.py`, xattr; 501 off-macOS), and the parent
  Zotero item (via Zotero's **local API**, never SQLite — mediated + sync-safe; personal
  vs group libraries routed from a new additive read of `library_id`/`type`/`group_id`).
  The controlled-vocabulary intersection (`_canonicalise_tags`) is the security gate —
  the client, even the LLM's own proposal, can never inject an arbitrary tag.
- **`/api/tagging/*` blueprint** + `tagging_*` config keys (propose model/params,
  `tagging_archive_dir`, `tagging_zotero_api_base`). UI: per-row Propose + vocabulary
  type-ahead + `[→note]`/`[→finder]`/`[→zotero]` apply buttons (Zotero disabled when its
  local API is unreachable) + an inline Restore panel. `tagging.js` follows the
  `ui.js`+`api.js`-only hierarchy (delegated from `audit.js` via a `window` global).

## 2026-07-13 (full-project review fixes — integrity / logic / UX / layout, 5 batches)

A 54-finding 3-axis review (app integrity, code logic, UX + layout — the latter two
axes' first deep pass) implemented in five batches on `fix/full-review-2026-07-13`
(plan: `docs/fix_plan_2026-07-13_full_review.md`). Baseline before + after: ruff clean;
pytest 1259 → 1276; JS 177 → 180.

- **Integrity criticals.** (A1) A vault switch now drops the loaded index + every
  retrieval cache (`cleanup()`/`reset_prewarm` on a real `obsidian_vault_path` change),
  and `obsidian_meta.json` records `vault_path` so a reindex refuses a cross-vault
  incremental — previously chat answered from the previous vault and a reindex ran
  incrementally against it (mixed-vault retrieval). Pre-fix indexes are grandfathered.
  (A2) `revert_archive_image` can no longer delete the only copy of an image: the
  original-path preflight is flag-independent (decides from disk, not the crash-stale
  `original_deleted` flag), the archive-copy deletion is gated on a digest-verified
  restore, and the flag persists immediately after the unlink. (A3) apply-tag-drift
  splits scalar `tags:` on whitespace AND commas (matching the scan reader) — no more
  fusing `tags: anxiety depression` into one invalid tag.
- **Logic (B1–B10).** LM Studio query-path LLM forces `max_retries=0`; NFC/NFD
  normalization on image/wikilink/archive name lookups (accented-French vaults); audit
  rel-path validator is a denylist (accepts accented paths) + case-insensitive skip
  prefix; GLM-OCR overflow→empty-downscale records a failure + cooldown (was a silent
  success); `_truncate_at_references` cuts the LAST reference heading past 50% and
  reports the drop; config-migration cache re-key moved inside the write lock; the
  `/api/config` validator map now covers provider/model-id/`vault_chat_system_prompt`;
  `/api/refactor/restore` is confirm-gated + heartbeats per op; the SSE runner releases
  the op lock if `Thread.start()` raises; plus server.py TERM→KILL escalation, launch
  join matching the 30 s poll, `_write.py` write-failure logging, `generate-md` cache
  invalidation, frontmatter fence accepted at EOF (reader+writer), audit worker polls
  its own stop event, corrupt `mapping.json` preserved before overwrite, and
  case-insensitive deny-prefix matching on darwin.
- **UX (C1–C5).** Silent-failure basket surfaced (pickVaultFolder / onProviderChange /
  cancelVaultIndex / resetAppData / removeExclusion / OCR-vision onchange / cancelAuditScan);
  deck Generate/Augment get Cancel buttons + a unified busy set incl. preview/compile-fix;
  docgen confirm-strip re-checks the busy gate; `isOnlineProvider` + labels include
  deepseek/zai; Single-Paper summary gets a Stop button that keeps the upload, uploads
  are abortable, plain-chat export flashes its outcome, vault Send disables mid-stream,
  per-image archive has a reentry guard, and the Ollama pull gets an honest "Stop watching".
- **Layout (D1–D7).** `#docgen-tab .deck-body` joins the scroll-region rule and
  `.vault-info` gets `flex-shrink:0` (the two High findings — the Report/Article tab's
  clipped controls + description-overlap, both verified by headless render); chat bubbles
  break long tokens and contain wide `<pre>`/`<img>`; card headers wrap; the help popover
  is height-bounded + dismisses on scroll; long paths break in the retrieval trace / deck
  summaries / note-path headers; the nonexistent `--text-muted` token is replaced; the
  prompt badge ellipsizes; the tab strip fades its right edge as a scroll cue.

## 2026-07-13 (audit + fixes — map-reduce bounds/cancel, DeepSeek cost accounting, preview-binding, docgen UI hardening)

A 25-finding principal-engineer audit of the docgen/help-UI series and the prior
DeepSeek + context-budget work, fully resolved in seven commits (`027e972`…`fdf10ba`);
three findings were disproven and pinned as by-design comments rather than "fixed".

- **Map-reduce hardening (rag/summarizer.py).** The map phase is now cancel-aware
  (`should_stop` from the paper worker's cancel event, polled per part + per streamed
  token — it yields nothing, so the SSE consumer's per-token cancel check could never
  stop it); parts are capped at `_MAP_MAX_PARTS` (64) with an explicit coverage notice;
  the keep-alive heartbeat runs on its own daemon thread (the token-driven beat could
  never fire during the pre-first-token stall it existed to cover); part sizes are
  equalised (no stub tail paying a full LLM call); the online document bound is the new
  `online_doc_window_tokens` config key (default 100k, validated 4000–2M); the
  "Nothing is dropped" banner no longer contradicts the reduce-clip notice. Six new
  `TestProviderRegressions` pins, including the previously-untested Ollama `num_ctx` branch.
- **DeepSeek cost accounting + degradation.** `_cached_tokens_from_usage` reads
  DeepSeek's TOP-LEVEL `usage.prompt_cache_hit_tokens` (the cached_input rates were dead
  code — every cached token billed at full input rate, ~50× over at DeepSeek's ~2%
  cache-hit pricing, which was RE-verified correct against the official docs and pinned
  with a do-not-"fix" comment); `tools`/`tool_choice` joined `_HEALABLE_PARAMS` (the only
  degradation path since lot 6bis emptied `MODELS_WITHOUT_TOOLS` — a tools 400 now strips
  tools and degrades to a plain answer instead of killing every agent/deck/docgen turn);
  the `deepseek-chat`/`deepseek-reasoner` alias retirement (2026-07-24) self-enforces via
  `_deepseek_curated(today)`; the five online `*_model` config keys get shape validation.
- **Preview-binding guard (deck + docgen apply-augment).** Staging is one slot per
  document path, so a second window's preview silently replaced the first and Apply
  could write a proposal the user never reviewed (the stale-diff sha cannot see the swap).
  `apply-augment` now REQUIRES `proposed_sha256` (carried on the augment frame) to match
  the staged digest, else 409; the stage is not cleared so the newer preview's own Apply
  stays valid.
- **Docgen UI hardening.** Compile-fix/preview join a shared `_docOpBusy()` predicate
  (deck item-3.8 parity — they previously left Generate clickable into a 409); the three
  SSE ops get a Cancel button (AbortController → consumer disconnect → the worker's
  cancel gates) and generation failures render a retry-able error boundary (the Markdown
  retry re-enters the confirm ceremony); help popovers dismiss on tab-out (non-modal,
  selection-safe) and hovered tooltips dismiss on Escape (WCAG 1.4.13).
- **Docgen worker dedupe.** The LaTeX and Markdown generate workers shared ~120
  duplicated lines of knob-resolution + outline→sections checkpoint/resume loop —
  extracted to `_resolve_gen_params` / `_run_sections_with_resume` (the routes differ
  only in their engine bindings and a per-section kwargs hook). New pins for the augment
  truncation guard, section-index bounds, and the turn-level error→info relabel.
- **Dispositioned, not changed:** the "dangerous macro in a comment" rejection
  (false positive — `find_dangerous_macros` strips comments internally); the
  warn-and-write on a failed apply-time figure copy (by design: a visible compile
  failure beats a silently omitted figure, `.tex.bak` recovers — rationale comment
  restored in docgen); the synchronous compile-preview thread pinning (bounded by the
  32-thread pool + per-subsystem op locks to ≤2/32 — documented instead of converting
  the mature contract to SSE); `create_note` additionally logs a `create_note_failed`
  companion line so the intent-first audit trail never asserts a create that never
  reached disk.

## 2026-07-13 (Report / Article Generator — docgen, + contextual help)

The prose sibling of the Deck Generator, built by parametrizing the deckgen engine
(commits `516af63`…`2128cb9`):

- **Substrate extraction (step 0).** `deckgen/latex.py` — the document-class-agnostic
  LaTeX substrate (metadata/title/dangerous-macro/cite/graphics/comment/template-assembly,
  `validate_common`, the `DocClassStrategy` seam) extracted from `assemble.py`, which
  keeps the Beamer pieces and re-exports the substrate (move+re-export, zero consumer
  churn). `deckgen/sections.py`/`outline.py` gained document-class hooks
  (sanitize / prompts / placeholder / unit label) defaulting to Beamer, so the deck path
  is byte-identical.
- **Article core (`docgen/`).** `article.py` (the `ARTICLE` strategy: prose
  `sanitize_article_section` — a `\section` gate, not a frame gate — assemble/validate),
  `prompts.py`, `generate.py` (engine bindings), `review.py`
  (`screen_article_repair` — structural floor ≥1 `\section`), `markdown.py` (the
  heading-gated Markdown long-form kind), plus `docgen/tests/`.
- **Routes (`api/routes/docgen.py` + shared `api/routes/_latexdoc.py`).** `load-template`,
  `generate` (SSE, emit-only scaffold with retry/salvage/checkpoint-resume),
  `generate-md` (SSE — a Markdown note WRITTEN INTO THE VAULT via the new
  `refactor/create_note.py` journal op, confirm-gated, refuses overwrite, reversible
  from Note Refactor → Restore: revert DELETES the created note, skipped if hand-edited),
  the augment trio, the compile/preview set, and `compile-fix` — full parity with the
  deck's emit-only exceptions. One doc op at a time (`_DOCGEN_OP_LOCK` → 409).
- **UI.** The Report / Article tab (`static/js/docgen.js`, ui.js+api.js-only), the
  `docgen_*` LLM-Settings knobs, and axis ii **contextual help**: `ui.js::helpTip` /
  `helpPopover` primitives + the declarative `help.js` table ("?" badges; every
  vault-write button must carry a `writes: true` popover — ratcheted by
  `tests/js/helpTable.test.js`).

Six user-requested axes implemented, then hardened by a principal-engineer review pass
(fix lots 1–6bis):

- **DeepSeek + Z.AI providers (axis iii).** `core/llm/adapters/openai_compat.py` —
  `OpenAIProvider` subclasses (streaming/tools/retry/heal/usage inherited); registered
  across factory/config/UI; every curated id priced (`test_all_curated_models_are_priced`
  still pins the guard). Keys via `DEEPSEEK_API_KEY` / `ZAI_API_KEY` env only.
- **Usage visibility (axis vi).** The three local paths that bypassed the adapter seam —
  local vault RAG, local single-paper summarise, vision/OCR — now record into
  `/api/usage` via `usage.record_local_usage` (estimated tokens, $0 cost, client aborts
  recorded as failures, not successes).
- **Audit curation + first vault write (axis ii wave 1).** mapping.json entries are
  hash-anchored (sha256 per curated PDF; a renamed/moved file re-links at the next scan —
  size-prefiltered + capped recovery); new read-only reports `ambiguous_pdfs` (with an
  in-UI key picker) and `skipped_pdfs`; `POST /api/audit/apply-tag-drift` writes missing
  Zotero tags into a note's frontmatter — the audit's ONLY vault write: confirm-gated
  (confirmInline ceremony), drift-validated server-side, op-locked, non-clobbering
  `.bak`, `log_vault_write`, YAML-quoted merge with a post-merge round-trip guard that
  refuses rather than corrupts.
- **Input drafts persist across quits (axis v).** `ui.js::installDraftPersistence`
  (localStorage) on deck topic/title/subtitle/instructions/audience, plain-chat input,
  focus question.
- **Conversation export (axis iv).** Export MD/TXT buttons on vault chat (new
  client-side `_vaultHistory` transcript incl. retrieval sources) and plain chat, through
  `/api/export-summary` (new `raw` flag: no `Summary_` prefix / injected heading).
- **In-app deck PDF preview (axis i).** `POST /api/deck/compile-preview` (single latexmk
  pass under the deck-op lock — never rewrites the `.tex`) + `GET /api/deck/preview-pdf`
  + macOS `open-pdf` fallback. The PDF is fetched header-authenticated and rendered via a
  blob: URL — a direct `<iframe src="/api/...">` would be 403'd by the origin guard
  (pinned by `tests/js/deckPreview.test.js` incl. a source ratchet).

## 2026-07-10 (UX & context-budget lots 1-5 — compile-fix env, deck title, dead links, num_ctx, map-reduce, salvage, reformat)

Five user-approved work lots from the 2026-07-10 audit (agent wrap-up / context limits /
deck generator / dead links), all landed together:

- **Compile & Auto-Fix repaired (WP-D1).** The compile env didn't match the suite's
  `common/latex-build.mk`: `BIBINPUTS` lacked the suite ROOT (where `_master.bib`
  lives — biber failed on every run), `TEXINPUTS` lacked `common/fig` (`\commonlogo`
  figures unresolvable), and the Finder-launched app's `PATH` lacked the TeX bin dir so
  latexmk couldn't spawn `pdflatex`/`biber`. New `deckgen.compile.build_compile_env`
  mirrors the `.mk` exactly; validated by compiling the maintainer's real
  `presentation-boadilla-ai.tex` through the app code (PDF produced). Missing-suite-file
  failures now explain what to check instead of a bare "compile failed". Pinned by
  `TestBuildCompileEnv` + opt-in `RUN_LIVE_LATEX=1` live compiles (the all-mocked suite
  was exactly how this class of bug shipped).
- **Deck title substitution (WP-D2).** Templated decks kept the template's placeholder
  `\title{…}` forever (`assemble_with_template` reuses the preamble verbatim and nothing
  substituted the topic). New `deckgen.assemble.substitute_title` (balanced-brace scan,
  word-bounded so `\titlepage` never matches, LaTeX-escaped, auto short-form for
  footlines) + `deck_title`/`deck_subtitle` body fields + UI fields (title defaults to
  the topic; empty subtitle leaves the template untouched).
- **Dead links fixed app-wide (WP-L1/L2).** A markdown link in a rendered answer
  navigated the whole PyWebView window to a Flask 404 ("dead page"). New global click
  delegate `ui.js::installLinkInterceptor` preventDefaults every anchor and routes it to
  the new `POST /api/open` (vault path → Finder reveal via `resolve_under_root`;
  http/https → default browser; every other scheme swallowed — never `file:`/custom
  schemes). Retrieval Context sources are now clickable. `test_open.py` +
  `tests/js/linkInterceptor.test.js`.
- **Context budgeting (WP-C1/C2/C5).** New `core/llm/budget.py` (token estimate ×
  safety margin, doubling `NUM_CTX_LADDER`, input-fitting helpers). The local adapter
  now auto-sizes Ollama's `num_ctx` per request on BOTH paths (tools + stream) —
  Ollama silently truncates past `num_ctx` (~4k default; ollama/ollama #8099/#4967), so
  agent turns / plain chat / refactor edits / compile repairs were losing their oldest
  context invisibly. The single-paper summarizer's hardcoded, silent LM Studio
  2800-token clip is replaced by a window-derived budget with a user-visible notice;
  the deck integrity review's input clip and the online RAG context-budget drop are
  also surfaced as info frames (engine gained a `stage_cb`).
- **Map-reduce Single Paper (WP-C3).** A document over the window budget is split into
  overlapping parts, each condensed into notes (progress + heartbeat via info frames so
  the SSE stall guard stays fed), and the user's template synthesises the final summary
  from the combined notes. More calls, nothing dropped.
- **Agent wrap-up (WP-A1/A2).** The loop now warns at the PENULTIMATE iteration
  ("one iteration left, it won't allow tools — answer now or make one targeted call",
  tools still enabled) ahead of the existing forced tool-less final. Deck sections that
  still fail every agent attempt get ONE tool-free single-shot salvage pass
  (`InProcessChatRunner` now honours `agent=False`, parity with the HTTP client) before
  the placeholder frame — the gathered tokens are no longer discarded outright.
- **Augment `reformat` op (WP-D3).** Content-preserving .tex formatting/layout pass over
  a section or the whole section region; a replace-type op sharing deepen/table's
  zero-section + truncation guards (`_AUGMENT_REPLACE_OPS`), same screen/stage flow.
- Docs: root/agent/llm `CLAUDE.md` + README (API table gained the previously-missing
  deck augment/compile rows and `/api/open`). Suite: 1038 py + 150 JS + 125
  deckgen/launch green; ruff clean.

## 2026-07-05 (Track 5.6 live-eval validation + run_eval wiring fix; Phase 6 / B3)

- **Batched embedding validated numerically safe on a real backend.** Track 5.6's
  hermetic `TestEmbedBatchParity` proves the batch path stores identical vectors for a
  FAKE embedder but can't see a real provider returning different numerics batched vs
  single. Ran the live golden eval twice (Ollama `nomic-embed-text` + `llama3.2:3b`) at
  `EVAL_EMBED_BATCH=1` and `=16` and compared the STORED embedding vectors per chunk:
  **bit-identical** (worst per-element abs diff `0.000e+00`, 768-dim, all chunks). So
  `vault_embed_batch_size` changes only HTTP call count, not embeddings — retrieval is
  unaffected. (The end-to-end pass rate differed 7/8 vs 8/8 on one pair, but that is
  `llama3.2:3b` sampling variance over identical retrieved context, not an embedding
  effect — which is why the vector-level comparison, not the answer pass rate, is the
  real signal.)
- **`run_eval` live path fixed (first-ever execution).** The module docstring warned
  the live path had never run and any wiring tweak would be "local to `main()`" — it
  was: a fresh `ObsidianVaultManager()` has `_vault_path=None` and `index_vault` reads
  it directly, so indexing silently bailed with "Vault path not set" and every query
  then hit "Index not found". Fixed by `manager.restore_vault_path(str(_FIXTURES))`
  before indexing (in-memory, no config clobber — unlike `set_vault_path`).
- **New `EVAL_EMBED_BATCH` env knob** pins `vault_embed_batch_size` for the run so the
  batch-vs-single comparison above is reproducible (documented in the module docstring).
  Hermetic `tests/eval/` (test_scoring + test_eval_pipeline) still pass; ruff clean. The
  live path stays opt-in behind `RUN_LIVE_EVAL=1` (no CI/suite impact).

## 2026-07-05 (Track 7.5 — permissive mypy + pre-commit; Phase 4 / B1)

- **mypy (advisory).** New permissive `[tool.mypy]` in `pyproject.toml`
  (`ignore_missing_imports`, namespace-package resolution anchored at the repo root so
  the two `config.py` files don't collide, no untyped-def checking). Run standalone
  with `mypy`. It is deliberately NOT a suite gate and NOT a pre-commit hook: a
  permissive pass surfaces ~60 findings that are almost all false positives from
  untyped third-party libs (PIL `Image.LANCZOS`, lancedb `**kwargs`, waitress) or
  benign dynamic patterns, so gating on zero would mean a pile of noise-suppressing
  ignores, not real safety. `ruff check .` stays the enforced static gate; ratchet
  mypy toward a gate as modules gain real annotations.
- **pre-commit.** New `.pre-commit-config.yaml` with local commit-time guardrails:
  `ruff check` (self-contained `astral-sh/ruff-pre-commit` pinned to v0.12.12 — the
  same 0.12.x as `requirements-dev.txt`, so pre-commit and CI never disagree),
  `gitleaks` (blocks a committed API key/secret — the app reads keys from env /
  BASE_DIR/.env, never config, so a hard-coded key should never land in git), and
  whitespace/EOF/merge-conflict/YAML/large-file/case-conflict hygiene. Opt in with
  `pre-commit install`; runs on staged files only. It is a local convenience, not a
  second CI gate.
- `requirements-dev.txt` gains `mypy>=1.14,<1.15` and `pre-commit>=4.0,<5.0` (minor-line
  pins like the other dev tools). CI is unchanged (ruff + pytest suite).
- Verified: `mypy` runs clean-collection over 123 source files (63 advisory findings);
  `pre-commit validate-config` passes; ruff + gitleaks + hygiene hooks resolve and pass
  repo-wide (gitleaks confirms no secrets, incl. `.env.example` placeholders). No
  runtime Python/JS changed, so the suites are unaffected.

## 2026-07-05 (distribution A4 — first-run guidance for the external runner; Phase 3)

- The app shells out to a system Ollama / LM Studio and bundles no models, so a fresh
  Mac opens but every local chat/summary/index fails until a runner is installed and
  models pulled. Two gaps closed so the user gets ACTIONABLE in-app guidance instead of
  an opaque per-feature failure, both surfaced through the EXISTING `#runtime-warning`
  banner (no new UI, no frontend risk):
  - `launch.py::_start_ollama` now routes an Ollama start failure through
    `add_provider_warning` (the actionable "Ollama executable not found. Install it
    (`brew install ollama` or the Ollama.app) …" message from `start_ollama_server`)
    instead of only logging it — bringing it to parity with the LM Studio path, which
    already self-reports. It reaches the UI via `/api/status` `warnings`.
  - `/api/health::_compute_health` now flags a running local provider that has ZERO
    models installed as a `local_model` degraded hint ("… is running but has no models
    installed — pull an embedding model (e.g. nomic-embed-text) and a chat model …").
    Only a definitively-empty `get_models()` with no error counts; a transient list
    failure is NOT mistaken for no-models (so a healthy install isn't nagged on a blip).
- Kept external per the A4 decision (bundling Ollama is large + licensing-encumbered);
  online providers already work with just a key.
- Tests: 2 new `test_health.py` cases (running-but-no-models → degraded with the
  actionable message; get_models error → still ok), and `test_launch.py`'s heavy stub
  gains `add_provider_warning`. Backend suite 1149 passed / 1 skipped, ruff clean.
  Verified e2e: real in-process `/api/health` against the LIVE Ollama and LM Studio
  adapters returns `local_model: ok` (proves the new `get_models()` call unpacks
  correctly against the real adapters — the contract risk the mocks can't catch).
- **A5 deferred** (in-app "set API key" writing `BASE_DIR/.env`): the `.env.example` +
  README path is the current route; a secret-writing endpoint is left to a focused
  follow-up rather than rushed into this session.

## 2026-07-05 (distribution A3 — offline model seed; Phase 2)

- **Fresh-Mac offline-first is now real.** The tiktoken BPE, NLTK punkt/stopwords, and
  the ~67 MB HF cross-encoder reranker live OUTSIDE the .app bundle, so a copy-only app
  on a new Mac previously FAILED its first offline index (the `SentenceSplitter` needs
  tiktoken + NLTK) and downloaded the reranker on first chat. New `launch.py --seed-models`
  headless mode downloads all three into the exact locations the app reads, reusing the
  SAME bundled tiktoken/nltk/sentence-transformers so it can't drift from runtime. It runs
  at module top level BEFORE the heavy `import webview; from app import app` block (a
  `sys.argv` membership test — Finder's legacy `-psn_…` arg can't match), so seeding needs
  neither webview nor Flask, then `raise SystemExit(rc)` (rc≠0 if any download failed).
- **DMG ships the seed helper.** `packaging/seed_models.command` (double-click, or
  right-click→Open from the unsigned .dmg) locates the installed app, strips its
  quarantine flag, and runs its frozen binary `--seed-models`. `install_and_build.sh`
  already copies it into the image when present (Phase 1). Run once while online;
  afterward the app indexes and chats fully offline.
- Bundling the caches into the .app was deliberately rejected (would bloat the read-only
  bundle + need a copy-on-first-run redirect) — this is the per-machine-seed model chosen
  for A3.
- Pinned by `test_launch.py` (3 new hermetic tests: seed fetches all three caches and
  returns 0; a failed download returns non-zero; the `--seed-models` argv branch exits via
  SystemExit without reaching the window). Verified e2e: a real `--seed-models` run into
  empty scratch dirs populated tiktoken/NLTK/reranker, and a `SentenceSplitter` then
  chunked with the network forced to a dead proxy + `HF_HUB_OFFLINE=1` — proving the first
  index now works fully offline.

## 2026-07-05 (distribution A1/A2 — arm64 arch pin + shareable .dmg; Phase 1)

- **arm64-only build, made explicit and enforced.** `install_and_build.sh` now passes
  `--target-arch arm64` to PyInstaller and hard-fails on a non-arm64 build host
  (`uname -m` guard after the Darwin check). Previously the bundle silently matched the
  build host and would not launch on the wrong arch; Intel Macs are now documented as
  unsupported (Rosetta only translates Intel→Apple-Silicon, and the native wheels —
  torch/lancedb/pyarrow/tokenizers — aren't reliably universal/cross-buildable). The
  flag also asserts every collected binary has an arm64 slice, so a foreign-arch
  dependency fails the build loudly instead of shipping broken. Verified: inner binary
  + spot-checked libtorch dylib are arm64.
- **Shareable `.dmg` (fixes "runs on a fresh Mac out of the box" being false).** The build
  now also packages `ChatEKLD_<date>.dmg` via `hdiutil` — staging the `.app`, an
  `/Applications` drag-target symlink, a generated `READ ME FIRST.txt` (correct dated
  app/binary names + the un-quarantine step), and `packaging/seed_models.command` when
  present (Phase 2). Still **ad-hoc signed, not notarized** (no Apple Developer account):
  a transferred copy is Gatekeeper-blocked until `xattr -dr com.apple.quarantine <app>`,
  documented in the README and inside the image. Verified: DMG mounts with the expected
  layout; the documented un-quarantine strips the flag and the ad-hoc signature stays
  valid afterward.
- **Doc-vs-reality fix:** created `.env.example` — the README and CLAUDE.md both told
  users to `cp .env.example …` but the file did not exist.
- README (§Prerequisites arm64-only, new §"Sharing the app with another Mac"), a new
  CLAUDE.md §"Build & Distribution", and `.gitignore` (`ChatEKLD_*.dmg`) updated to match.
  Build-script / docs only — no runtime Python/JS changed, so the suites are unaffected.

## 2026-07-05 (packaging — rebuild + reinstall; refactor/ bundle-data gap)

- Rebuilt `ChatEKLD_2026-07-05.app` from main (all plan tracks 1–6 + 7.1–7.3) via
  `install_and_build.sh`; ad-hoc signature verified; plan-era files spot-checked in
  the bundle (`api/sse.py`, `core/paths.py`, `refactor/`, 6a tokens, 6c markup,
  `confirmInline`). Installed to `/Applications`, previous 2026-06-30 bundle moved
  to the Trash. User data/index untouched; no reindex required.
- Build fix: `refactor/` was missing from PyInstaller's `--add-data` set while every
  sibling package (core/rag/services/api/audit/deckgen) ships — it worked only
  because static imports pull the package into the PYZ analysis. Now bundled like
  the rest.

## 2026-07-04m (Track 5.6 — batched embedding in the streaming indexer; TRACK 5 COMPLETE)

- Chunks needing (re-)embedding now buffer up to `vault_embed_batch_size` (new
  config-only knob, 1–256, default 16; ≤1 restores the legacy per-chunk path
  byte-for-byte) and flush as ONE `insert_nodes` call — one provider embed HTTP
  round-trip and (on lancedb) one transaction per batch instead of one per chunk.
  The indexing embed model's `embed_batch_size` is aligned to the same knob so
  LlamaIndex doesn't re-split the batch at its default of 10.
- **Reindex-neutral**: the batch path replicates `BaseIndex.insert()` exactly
  (run_transformations → insert_nodes → per-doc set_document_hash, verified against
  llama-index-core 0.14.22); chunk doc_ids and hashes are untouched, so existing
  indexes skip-by-hash with zero re-embedding.
- **Failure semantics preserved**: a failed batch retries chunk-by-chunk on the
  original per-doc path (after an idempotency cleanup when the store may have been
  touched), so the consecutive-failure breaker, interruptible backoff and
  `reinsert_failed` gap-tracking still count per chunk. Batches are capped at
  `_PERSIST_EVERY` so the checkpoint cadence keeps its granularity; a cancel with a
  non-empty buffer reports buffered deleted-old chunks as gaps.
- Pinned by `TestEmbedBatchParity`: identical docstore texts/hashes/vectors vs a
  batch-size-1 run with `[3,2]` embed calls vs `[1]×5`; a re-run skips everything
  with zero embed calls; a batch-allergic backend degrades per-doc losing nothing;
  a dead backend still trips the breaker at the same bound.
- **Live-eval caveat**: provider-side batch-vs-single embedding numerics are
  invisible to hermetic parity — validate with `RUN_LIVE_EVAL=1 tests/eval/`
  against the real backend (or set `vault_embed_batch_size: 1`).

## 2026-07-04l (Track 7.2 — GitHub Actions CI)

- `.github/workflows/ci.yml`: the full hermetic gate (py_compile sweep, ruff clean
  baseline, the complete pytest union incl. launch/thesaurus/deckgen targets, and the
  Vitest suite) on every push/PR — ubuntu-latest, Python 3.12 under `constraints.txt`,
  requirements filtered of the 7 macOS-only lines with a drift-detecting count check,
  pip/npm caching, 40-min timeout, per-ref concurrency cancel. The Track 4 Batch 2
  failure class (a batch committed with `import app` broken — its suite never ran)
  is now caught on push.
- `.github/workflows/live-smoke.yml`: manual (weekly schedule ready to uncomment)
  key-gated `RUN_LIVE_PROVIDER_TESTS=1` provider round-trips — the check that would
  have caught the 2026-07-02 wire-format P0s; missing secrets degrade to skips.
- First-run duration + any Linux-only test drift to be verified on the first GH run.

## 2026-07-04k (Track 6 Batch 2 — accessibility: 6c semantics & labels, 6d perceptual floors; TRACK 6 COMPLETE)

- **6c — semantics & labels**: card titles are real `<h2>`s and sidebar/panel titles
  `<h3>`s (`<main>` had zero headings — titles were styled spans, invisible to heading
  navigation; CSS resets keep rendering byte-identical); system-prompt textareas gain
  accessible names; the fallback checkbox quartet sits under `fieldset/legend`; the
  refactor note listbox uses a roving tabindex (only the selected option is a tabstop —
  Tab used to walk every analyzed note); audit report tables get `scope="col"` on all
  32 headers and the tabpanel renames itself (`aria-labelledby`) with the active tab;
  the materials toggle declares `aria-expanded`; French fragments (`example chips via a
  new `renderExampleChips` lang option, the free-prompt field, interrupted-answer
  notes) carry `lang="fr"` inside the `lang="en"` document; decorative ⚙ glyphs are
  `aria-hidden`. Pinned by `tests/js/semanticsLabels.test.js`.
- **6d — perceptual floors**: every px `font-size` (89 rules) converted to rem —
  identical default rendering, but user font scaling now passes through the
  `height:100vh; overflow:hidden` PyWebView shell where px type could not be enlarged
  at all — with a 12px floor (was 10–11px micro-type); 24px minimum targets on the
  mini controls (copy buttons, chips, `.audit-link-btn`); the fixed 56/62px headers
  became `min-height` floors; the tab strip scrolls instead of clipping grown labels.
  Pinned by `tests/js/perceptualFloors.test.js`.

Track 6 is complete. Suite: 1125 passed + 1 skipped, ruff silent, 134 JS tests.

## 2026-07-04j (Track 6 Batch 1 — accessibility: 6a contrast, 6b live regions, 6e confirmation parity)

- **6a — color & contrast token pass**: all theme tokens now clear WCAG AA (4.5:1 text /
  3:1 non-text) on the surfaces they actually sit on, in both themes — dark `--accent`
  darkened for white button text (4.28→4.68), hover roles split into text-grade
  `--accent-hover` + bg-grade `--accent-hover-bg`, light provider tokens darkened,
  accent-as-text sites (`.audit-link-btn`, `.doc-type-badge`, badges) moved to text-grade
  tokens, copy buttons rest at full opacity with muted color instead of an opacity fade
  (~2.7:1 effective), runtime-status dots express state via data-state + distinct SHAPE
  (not color alone). Enforced go-forward by a contrast lint
  (`tests/js/contrastTokens.test.js` — parses `styles.css`, recomputes real ratios,
  alpha-composited tints included; editing a token below AA fails the suite).
- **6b — live regions**: streamed chat/summary output announces Generating…/ready via
  the global `#sr-status-announcer`; `#prewarm-banner`, `#excl-status` and
  `#refactor-restore-status` became static polite regions in markup (WebKit misses live
  regions minted the same tick they're populated); model pull announces terminal states
  only (`#pull-status` deliberately non-live — chunk progress would spam readers).
- **6e — confirmation ceremony parity**: new `ui.js::confirmInline` primitive
  (Cancel-first focus order); the deck's two file-overwriting Apply buttons and
  refactor's "Revert all" — previously single-click — now require the ceremony; the
  Reset modal focuses Cancel, not the destructive default.

Remaining Track 6: 6c (semantics & labels), 6d (perceptual floors) as their own batch.
Suite: Python unchanged-green, ruff silent, JS 115 tests (contrast lint + live-region +
ceremony pins added).

## 2026-07-04i (Track 5 Batch 2 — 5.5 prompt caching + cost accounting, 5.7 config_version)

- **5.5 — Anthropic prompt caching** (`core/llm/adapters/anthropic.py`): the system prompt
  is now sent as a block array with one `cache_control: {type: "ephemeral"}` breakpoint —
  a prefix match over tools → system → messages, so tool definitions + system prompt cache
  together across agent-loop iterations, deck per-section turns, and same-settings vault
  chats (reads ~0.1× input, writes 1.25×; below-minimum prompts are silently uncached at
  no premium). **Cache-token accounting**: the adapter normalises Anthropic's exclusive
  usage semantics (its `input_tokens` excludes cache reads/writes) into the app's inclusive
  `LLMUsage` shape — without this the existing `input − cached` cost formula would
  undercount; new `cache_creation_input_tokens` field billed @1.25× input by
  `estimate_cost_usd`; Claude pricing rows gained their 0.1× `cached_input` rates.
  Curated lists gained `claude-fable-5`/`claude-sonnet-5` (+ pricing, incl.
  `claude-mythos-5`) and the `gpt-5` trio; `unpriced_curated_models()` warns at startup
  and is pinned EMPTY by `test_all_curated_models_are_priced` so a curated id can no
  longer silently cost out at $0. OpenAI needed no request change (system-first ordering
  and `cached_tokens` capture were already in place — now documented). Pinned by
  `TestAnthropicPromptCaching`.
- **5.7 — `config_version` migrations** (`core/config.py`): config.json now carries a
  server-owned schema version (stamped by every `save_config`, stripped from
  `POST /api/config`); `load_config()` runs the pure `_MIGRATIONS` chain once on a
  cache-miss load of an older file and atomically persists the result. Migration 0→1
  prunes keys unknown to this release (`save_config` materialises every default, so
  retired keys otherwise lingered forever). Downgrade-safe: a future-versioned file is
  never migrated backwards. The config write lock became an RLock (the migration path
  re-enters it from inside `save_config` — a plain Lock would self-deadlock on the first
  save over an old config). Pinned by `TestConfigVersionMigration`.
- **Fix uncovered by 5.7: `obsidian_vault_path` was missing from `_DEFAULTS`/`KNOWN_KEYS`**
  — so item 4.10's key whitelist at `POST /api/config` silently STRIPPED the validated
  vault path before persisting (set in memory, gone on restart), and the new prune
  migration would have wiped it from existing configs. Now declared (default `""`);
  `test_every_persisted_key_is_known` pins that every persisted key is in KNOWN_KEYS.

Suite: 1125 passed + 1 skipped, ruff silent, 60 JS tests.

## 2026-07-04h (Track 5 Batch 1 — performance & I/O: 5.1–5.4)

Implements the four small Track 5 items of `docs/improvement_plan_2026-07-04.md`, each with a
pinning test asserting the cache/walk is a pure read-amplification optimization (identical
results to the uncached path, self-invalidating on file change). Suite after the batch:
1116 passed + 1 skipped (documented command + launch/thesaurus/deckgen), `ruff check .` silent,
60 JS tests.

- **5.1 — refactor image-digest memo** (`refactor/cache.py`): `(path, size, mtime_ns) → sha256`
  memo consulted after the dataless/size-cap gates, LRU-bounded, dropped by
  `resolver.invalidate_index_cache`. A per-image OCR-inclusion checkbox toggle no longer
  re-reads and re-hashes every image on the note (tens of MB per click on image-heavy notes).
- **5.2 — deck template/bib parse caches** (`deckgen/template.py`): per-`.bib` parsed key index
  and per-`.sty` capped text cached keyed on `(path, size, mtime_ns)` (thesaurus-cache pattern,
  stdlib-only, module stays app-independent). Repeat generate/augment/compile calls stop
  re-reading + re-parsing a multi-MB suite `_master.bib`.
- **5.3 — archive reference-sweep index** (`refactor/archive.py` + `resolver.link_target_basenames`):
  the move-safety gate now stat-walks the vault, re-reads only files whose `(size, mtime_ns)`
  changed, locates candidates via stored per-note link-target basenames (per-canvas lowered
  text), and runs the full resolver-accurate verify scan on candidates only. Safety bias
  strictly improves: percent-encoded references (`![](fig%20two.png)`) that the old whole-text
  substring prune missed now correctly refuse the move.
- **5.4 — pruned vault scan walk** (`rag/vault.py::_iter_vault_scan_files`): the indexer's scan
  pass prunes reserved + user-excluded dirs at descent time (never listed) and drops the
  per-file `resolve()`; symlinked files keep the resolve-gate, symlinked dirs are no longer
  descended (os.walk semantics; the old behaviour double-indexed in-vault symlink dirs).
  Per-type buckets sort post-walk, so MD/PDF/name_index order is byte-identical.

## 2026-07-04g (repair: Track 4 Batch 2 shipped broken — boot, SSE framing, vault chat, refactor routes)

The `2026-07-04f` commit (`d06cd73`) landed **unverified** — its "All tests pass" claim was
false: `import app` failed (the suite could not even collect), so none of it had run. Repaired
in place; full suite re-run green (1116 passed + 1 skipped, ruff clean, 60 JS tests).

- **Boot**: `api/sse.py` imported `sanitise_error_msg` from `core.utils` (it lives in
  `api.security`) and `api/routes/refactor.py` imported a nonexistent `core.utils.get_now_utc`
  — every route import failed, so the app could not start at all.
- **SSE framing**: every `yield` in `api/sse.py` emitted literal `\n` characters
  (`"\\n\\n"` in source) instead of newlines — the SSE protocol was broken for all five
  streaming routes.
- **Vault chat deleted**: the 4.8 "extraction" replaced `stream_chat` with a *self-recursive*
  `_build_engine` stub referencing an undefined `message` — the first chat message would
  RecursionError. Restored the pre-d06cd73 build body verbatim inside a real `_build_engine`
  and re-created the `stream_chat` wrapper (see `rag/CLAUDE.md` §Lock discipline).
- **Refactor routes dead**: `resolve_under_root` was used but never imported (F821 on six
  call sites), and `_resolve_scope_note_rel` resolved the vault-relative `rel` against the
  *scope* dir, doubling the scope segment — every scope-locked endpoint 400'd (14 test
  failures once the suite could run).
- **Ruff baseline** (documented as clean-is-the-gate): 32 findings — the 3 F821s above plus
  25 dead imports and an F811 double-import, all removed.
- **Tests**: retargeted the patches that pinned moved seams (`TestDeckOpGuard` → the promoted
  `api.sse._SSEOpGuard`/`run_sse_worker`; plainchat/llm fallback tests →
  `core.llm.factory.get_llm_provider`, the single post-4.7 lookup point). CLAUDE.md files
  updated for `api/sse.py`, `core/paths.py`, and the repaired 4.8 (Batch 2 had skipped the
  documented doc-update discipline entirely).

## 2026-07-04f (Track 4 — consolidation: shared primitives (Batch 2))

Completes Track 4 of `docs/improvement_plan_2026-07-04.md` (4.2, 4.3, 4.4, 4.7, 4.8) to unify shared backend and frontend SSE handling, path resolving, and core LLM stream streaming logic. All tests pass.

- **4.2 — shared path resolver**: Introduced `core.paths.resolve_under_root` replacing 7 near-identical path validators across endpoints.
- **4.3 — shared backend SSE skeleton**: Extracted a generic SSE stream responder with threading queue and staleness checks to `api.sse` and adopted it across `deck`, `plainchat`, `refactor`, `paper`, and `vault` routes. Added `Cache-Control: no-cache` and `X-Accel-Buffering: no` universally.
- **4.4 — shared frontend stream consumer**: Extracted `consumeSSE` in `api.js` replacing `readSSE` completely, managing `secureFetch`, bad responses, and fallback handling. All modules now consume it. Unified error boundary/renderer logic implemented in `ui.js`.
- **4.7 — one stream_with_fallback helper**: Extracted the pre-first-token fallback retry logic into a unified `stream_with_fallback` in `core.llm.factory`, now shared by `chat.py`, `engine.py`, and `summarizer.py`.
- **4.8 — unify stream_chat/retrieve**: Extracted the identical `_build_engine` logic out of `rag/vault.py::stream_chat` and `rag/vault.py::retrieve`, preventing future logic drift (like thesaurus/primer expansion mismatch).

## 2026-07-04e (Track 4 — consolidation: shared primitives (Batch 1))

Implements five items of Track 4 of `docs/improvement_plan_2026-07-04.md` (4.1, 4.5, 4.6, 4.9, 4.10) to reduce copy-paste drift and fix architectural footguns. All 1114 Python tests and 60 JS tests are passing.

- **4.1 — local-origin gate consolidation**: replaced 67 inline `origin_is_local()` route gates with a single `@app.before_request` hook in `app.py` gating all `/api/*` requests. Added `test_all_routes_gated` which dynamically checks Flask's `url_map` to guarantee no future route misses the gate.
- **4.5 — shared journalled write primitive**: extracted a unified `journalled_write_note` helper to `refactor/_write.py`. Migrated `apply.py`, `format_fix.py`, and `llm_apply.py` to use this helper, eliminating duplicate stale-hash/write/journal checks.
- **4.6 — readonly config on hot paths**: switched `_cfg_bounded_int` and `_failure_cooldown_s` (vision) and `get_index_warning` (vault) from `load_config()` to `load_config_readonly()`, eliminating O(N) deep-copies of the configuration dictionary on fast paths.
- **4.9 — theme-token CSS properties**: declared semantic CSS custom properties (`--color-provider-*`, `--color-status-*`) in `static/css/styles.css` for both light and dark themes. Removed all hardcoded inline hex colors from `ui.js` and `app.js` in favor of `var()`.
- **4.10 — hierarchy & footguns**: 
  - Resolved `set_vault_path` whole-config clobber save by merging only the vault path key.
  - Resolved `check_availability` lock discipline by performing the slow network call outside `LOCAL_MODEL_LOCK`.
  - Added a configuration key whitelist for `POST /api/config` against default known keys.
  - Documented `vault.js` and `summarizer.js` reading settings from `config.js` as permitted exceptions in `CLAUDE.md` and updated the `moduleHierarchy.test.js` lint.

## 2026-07-04d (Track 3 — frontend correctness & races)

Implements Track 3 of `docs/improvement_plan_2026-07-04.md` in full (eight items; every finding re-verified — none obsolete). The track's systemic root causes — *loads that save*, *no stale-response discipline*, *module-scope booleans guarding two state machines*, *manual post-write state sync* — are addressed at every confirmed site. Every fix is pinned by a Vitest test driving the REAL module under jsdom (61 JS tests after the track); the Python suite is untouched (1108 passed + 1 skipped, ruff silent).

- **3.1 — split in-flight flags** (`vault.js`): one `_isQuerying` served both chat and the indexing lifecycle — a multi-hour index run blocked vault chat entirely (the server supports chat during indexing) and `cancelVaultIndex` force-cleared a live chat's guard. Now `_isChatting`/`_isIndexing`, transitions unchanged within each machine.
- **3.2 — loads never save** (`config.js`): `loadModels` no longer adopts-and-persists the first listed model on a degraded boot (the boot-rewrites-your-model bug); the chat-models-as-embed fallback is killed in both populate paths (honest disabled empty state); provider switches and model loads are generation-token latest-wins; `onEmbedProviderChange` no longer blanks + auto-persists. Only explicit user change events persist.
- **3.3 — latest-wins discipline** (`ui.js::makeLatestGate` + 4 sites): health poll, audit status poll, audit report switcher (slower response no longer renders last), summarizer abort-controller identity (an old run's finally no longer clobbers the new run's controller).
- **3.4 — consume post-write echoes**: deck compile-fix reads the sha through an accessor over `deck.tex_sha256` and apply-repair adopts the response's fresh `tex_sha256` (no more 409 on a legitimate follow-up); refactor re-analyzes the selected note after Apply and lazily refreshes other applied notes on selection (stale Original/Proposed panes gone).
- **3.5 — SSE hygiene**: `readSSE` cancels the reader on every exit (early break used to leak the connection) and flushes the TextDecoder at stream end (split multi-byte tail survived); vault + plain chat keep PARTIAL answers visible on a mid-stream error (display-only; plain chat's no-fake-history rule holds); Retry re-sends the original question directly and never clobbers a fresh draft.
- **3.6 — missing body fields** (`vault.js`): `wikilink_expansion`/`thesaurus_expansion`/`primer_enabled` — documented live overrides — now actually ride the chat body instead of falling back to persisted config inside the save debounce.
- **3.7 — per-overlay modal state** (`ui.js`): closing one modal (incl. refactor's delayed timers) no longer strips the live modal's Esc/Tab handling; Esc peels only the topmost; background inert clears only with the last close; focus restore is stack-aware.
- **3.8 — ten smaller confirmed items**: chained + failure-surfacing settings saves; isolated init steps; superseded-upload DELETE; export errors no longer destroy the summary pane; audit `_esc` single-quote + escaped innerHTML interpolations; honest OCR-inclusion state on partial failure + guarded bulk toggles; retryable section loading; shared in-flight thumbnail fetches; prewarm poll deadline; symmetric deck busy flags.

## 2026-07-04c (Track 2 — backend stability: locks, cancellation, worker lifecycle)

Implements Track 2 of `docs/improvement_plan_2026-07-04.md` in full (nine items; every finding re-verified against the tree first — none obsolete). The track's two systemic root causes — *lock lifetime owned by the wrong party* and *cancellation stops loops, not calls* — are addressed at every confirmed site. **No reindex required by any item.** Suite after: 1108 passed + 1 skipped, `ruff check .` silent, 35 JS tests.

- **2.1 — query-path embed bounded; timed retrieval-lock acquire.** Retrieval embeds the query over HTTP while holding `_index_mutation_lock`; one wedged local call stranded every later chat on the lock (restart-only). `Provider.get_embedding(..., request_timeout_s=…)` bounds the QUERY-path embed only (`QUERY_EMBED_TIMEOUT_S` 30 s; Ollama `client_kwargs`, LM Studio `timeout+max_retries=0`); indexing embeds stay deliberately unbounded. `stream_chat`/`retrieve` acquire via `_acquire_retrieval_lock()` — 120 s timed acquire with an actionable error (above the longest legit holder = a checkpoint; below the SSE stall floor).
- **2.2 — deck zombie-writer + lock leak.** `_DeckOpGuard` owns `_DECK_OP_LOCK` release: exactly once, when the stream ended (incl. never-iterated generator, via `call_on_close`) AND the worker exited — a stall/disconnect can no longer release the lock under a still-writing worker, and an unconsumed Response can no longer leak it forever. Cancel gates added before generate's assemble/figure-copy/scaffold and augment's staging write.
- **2.3 — LLM daemon pile-up.** The five bounded refactor LLM actions now propagate the 504's cancel into the daemon (polled before/after `LOCAL_MODEL_LOCK` and per token — an abandoned queued worker evaporates when the lock frees) and are per-action single-flight (second request while one is wedged → 429, not another queued thread).
- **2.4 — tool-dispatch deadline.** The agent wall clock now gates each tool dispatch (the docstring had always promised it), and `read_note(..., time_budget_s=…)` refuses to START an uncached-PDF fresh extraction on a sub-floor budget (20 s); cache hits always served; non-agent callers unchanged.
- **2.5 — `_DONE` sentinel survives a full queue** (`core.utils.put_done_resilient`, adopted at all five SSE worker sites) — the one-shot 5 s put dropped the sentinel and reported "timed out" on a completed answer. The thread-leak-guard port half is deferred to 4.3 by the plan's own note. *(**Closed** by item 4.3 shipping: `api/sse.py::_SSEOpGuard` tracks `worker_spawned`/`worker_finished` and releases only once the stream has ended AND any spawned worker has exited — verified 2026-07-30, no longer an open deferral.)*
- **2.6 — client caches bounded.** Both `(host, timeout)` local-client caches are 8-entry LRUs (evicted pools GC-closed, never closed in place) and the agent path quantises its per-call timeout to a coarse ceiling ladder — the "handful of entries" comment was wrong (one key per remaining-second); worst case was hundreds of idle httpx pools.
- **2.7 — retune race.** The engine's locked pipeline build is now the ONLY writer of the shared BM25 `similarity_top_k` / reranker `top_n` (tune+use atomic within one mutation-lock hold); the manager fetch paths are read-only by contract — a deck `vault_search` (k=12) could previously retrim a concurrent chat's in-flight pass (k=8).
- **2.8 — durability gaps.** (a) checkpoint promotion bracketed by `promotion_state.json` — a torn multi-file promotion is refused at load instead of silently serving a mixed-generation store; legacy checkpoints grandfathered. (b) the stale-doc sweep spares sources whose READ failed this run (iCloud dataless miss ≠ deletion). (c) the final persist's cache invalidation moved OUTSIDE the rw write lock, matching the mid-run path (inside, a concurrent BM25 rebuild froze every chat). (d) refactor batch writers checkpoint the journal every 25 notes — a mid-batch crash left applied notes invisible to Restore.
- **2.9 — write-path parity.** apply-repair now writes `.tex.bak` before overwriting (parity with its two siblings); the augment preview is genuinely write-free (figure copies deferred to apply via `pending_figures`, containment-checked); compile-repair errors pass `sanitise_error_msg`; audit `mapping.json` RMW serialised by a module mutex; `_ensure_thumbs_excluded` saves only its one-key delta instead of the whole stale config snapshot.

## 2026-07-04b (Track 7.1 + 7.3 — Ruff gate + JS module-hierarchy lint)

Sequencing step 2 of `docs/improvement_plan_2026-07-04.md`. Suite after: 1072 passed + 1 skipped (one combined pytest run of the documented command + deckgen/tests + test_thesaurus + test_launch), `ruff check .` silent, 35 JS tests.

- **7.1 — Ruff with a clean baseline.** `pyproject.toml` ([tool.ruff] only — deliberately no [project] table; the app is not an installable package) pins the default defect-shaped rule set (E4/E7/E9 + pyflakes F) explicitly so a Ruff upgrade can't move the gate; vendored-but-tested `audit/` included; `requirements-dev.txt` pins `ruff>=0.12,<0.13`. Baseline cleaned 48 → 0 and **`ruff check .` added to the documented suite gates** (CLAUDE.md §Tests). Non-mechanical fixes: `deckgen/outline.py` F821 annotations resolved via `TYPE_CHECKING` imports (runtime core stays `requests`-free); smoke_test's `from app import FEEDBACK_FILE, CONFIG_FILE` importability canary kept under an explanatory `noqa: F401`; dead imports removed from `services/vision.py` / `core/providers/{lms,ollama}.py` / `core/llm/adapters/local.py` (verified no mock.patch or probe relies on them); 22 stale `# noqa: WPS433` wemake-origin comments stripped from `test_audit.py` (ruff warned "invalid rule code" on every run).
- **7.3 — module-hierarchy import lint** (`tests/js/moduleHierarchy.test.js`), completing the track's named deliverables. A **ratchet** over the CLAUDE.md §JS Module Hierarchy rule: any new out-of-hierarchy import fails the suite; the two known drift edges (`vault.js`/`summarizer.js` → `config.js`, plan item 4.10) are allowlisted *and asserted to still exist*, so fixing 4.10 must shrink the allowlist. Also pins the module set and the absence of dynamic `import()`/bare side-effect imports (the forms the lint's regex would miss).

## 2026-07-04 (Track 1 — the five P0s of the unified fix plan, + minimal JS test harness)

Implements Track 1 of `docs/improvement_plan_2026-07-04.md` (the single active plan) plus the minimal slice of Track 7.3 needed to regression-test items 1.1/1.2. All five findings re-verified against the tree before fixing; none was obsolete. **No reindex required by any item.** Suite green: 939 passed + 1 skipped (documented pytest command), 133 passed (deckgen/tests + test_thesaurus + test_launch), 31 passed (new JS suite).

- **7.3-min — Vitest + jsdom harness** (`package.json`, `vitest.config.js`, `tests/js/`; run with `npm run test:js`). Dev-only — runtime third-party JS stays vendored under `static/js/vendor/`. Ships `readSSE` contract tests (frame parsing, split-frame + streamed-UTF-8 buffering, `[DONE]`, malformed-frame skip, reader-lock release, and a loud-failure pin for the url-instead-of-Response misuse class).
- **1.1 — Compile & Auto-Fix was dead client-side.** `runCompileFix` called `readSSE(url, options, callback)` — but `readSSE(response)` is an async generator over an already-fetched `Response`, so **no request was ever issued**; the button logged "Starting…" and silently completed. Rewritten as `secureFetch` + `for await` (the shape every other consumer uses) with a non-OK branch surfacing the server's structured 409/400 refusal. Pinned by `tests/js/deckCompileFix.test.js` (request actually issued, body/header contract, sha-holder sync, refusal + network-error recovery). (`static/js/deck.js`.)
- **1.2 — XSS bypass in the single sanitisation gate.** The `javascript:`/`vbscript:`/`data:text/html` scheme test ran after only `trimStart()`, but the HTML URL parser strips tab/LF/CR *anywhere* in a URL (post entity-decoding) and trims leading/trailing C0 + space — so `<a href="jav&#9;ascript:…">` in a vault note or model answer executed in the app origin. The tested copy of each attribute value is now normalised exactly as the URL parser preprocesses it before the scheme regex (removals only — everything previously blocked stays blocked; surviving values untouched). Invariant: the sanitiser judges the same post-preprocessing URL the browser would resolve. Pinned by `tests/js/sanitiseHtml.test.js` (21-case smuggled/blocked/legitimate matrix). (`static/js/ui.js::sanitiseHtml`.)
- **1.3 — the documented `{"retrieval"}` SSE frame was dropped** (regression of shipped Phase 5 B4). The chat worker enqueued it but the consumer dispatch chain had no branch, so the frame fell through silently and the "Retrieval Context" panel never rendered (vault.js has handled it all along). Additive forwarding branch; new `TestSSEFrameContract` enumerates **every** documented frame type end-to-end across the single-shot, error, and agent worker paths, so a future frame type without a consumer branch fails the suite. (`api/routes/vault.py`.)
- **1.4 — section-scoped LLM edits silently truncated and certified the loss** (section twin of the fixed 07-02 item 0.1). The truncation guard returned `None` whenever a section was targeted, so a section over `REWRITE_MAX_CHARS` was clipped, the truncated head spliced over the whole section span, and the WYSIWYG sha guard certified the loss. `_llm_edit_truncation_guard` now 422s any over-cap *target* body — whole note or section — before the LLM call, mirroring deck augment. Invariant: no LLM-edit proposal is ever generated from a clipped view of the span it replaces. (`api/routes/refactor.py`.)
- **1.5 — op-lock epoch was shared state, defeating its own zombie-safety.** `try_acquire_lock` cached the epoch on the manager singleton and `release_lock`/`heartbeat` read it at call time — any new acquisition overwrote the token a still-running previous holder later released with (cancel an index run → start a refactor Apply → the indexer's `finally` released the *refactor's* lock mid-batch). Now `try_acquire_lock` **returns** the epoch (truthy int; `None` refused), `release_lock(epoch)`/`heartbeat(epoch)` require the caller's own token (falsy refused — unconditional release stays exclusive to `force_release`), and `index_vault(op_epoch=…)` threads it to all internal heartbeat sites (0 ⇒ inert). Refactor write routes bind the token into their zero-arg heartbeat closures, so `refactor/`'s callback contract is unchanged. Invariant: a holder can only release/extend the acquisition whose epoch it captured at acquire time — pinned by `TestOpLockEpochToken`. (`rag/vault.py`, `api/routes/vault.py`, `api/routes/refactor.py`.)

## 2026-07-02 (Provider wire-format P0 fixes + improvement-plan Phases 0–1)

Implements `docs/improvement_plan_2026-07-02.md` (which supersedes the 07-01 plan): a new **Phase P0** for four field-reported provider wire-format bugs the mocked test suite could not see, plus the 07-01 plan's Phase 0 (verified bug fixes) and Phase 1 (robustness hardening) in full. **No reindex required by any item.** Full suite green (1000 passed, 1 skipped) — including, for the first time, `test_audit.py`/`tests/audit/` **with Zotero running** (see 0.7).

**Phase P0 — provider wire-format fixes.** All three tool-using features (Obsidian Agent, Deck Generate, Deck Augment) were fully broken on OpenAI + Anthropic; details in `core/llm/CLAUDE.md` → *Provider wire-format contracts*.

- **P0.1 — dotted tool names 400'd OpenAI + Anthropic.** `vault.search`/`vault.read_note`/`vault.list_materials` violate both providers' tool-name regex (`^[a-zA-Z0-9_-]+$` — no dot); every tool-enabled request failed before generation (Gemini accepts dots, which is why only it "worked"). Renamed to `vault_search`/`vault_read_note`/`vault_list_materials` (a pure internal registry key + provider echo — no external dependency on the dot); `ToolRegistry` now rejects any name outside `^[A-Za-z_][A-Za-z0-9_-]{0,63}$` (the strictest provider intersection) at construction. (`core/agent/vault_tools.py`, `tools.py`, `loop.py` preamble; docs/tests renamed throughout.)
- **P0.2 — Gemini 3.x `thought_signature`.** Gemini 3 requires each `functionCall` part echoed back in history to carry its `thoughtSignature`; the adapter never captured/re-emitted it, so the second tool turn 400'd ("Function call is missing a thought_signature"). New `ToolCall.thought_signature` field, captured at the part level in `google.py`, re-emitted by `build_gemini_contents` (key absent when empty). Inert for other providers / older Geminis. (`core/llm/types.py`, `tool_schema.py`, `adapters/google.py`.)
- **P0.3 — Ollama agent multi-turn always failed at iteration 2.** Not a model-capability issue: `build_openai_messages` serialises `tool_calls[].function.arguments` as a JSON *string* (correct for OpenAI/LM Studio), but the ollama client's request-side pydantic `Message` model requires a dict — re-sending tool history failed client-side ("Input should be a valid dictionary"). New `_ollama_messages()` in the local adapter parses the strings back to dicts (ollama branch only) and populates ollama's native `tool_name` on tool results. (`core/llm/adapters/local.py`.)
- **P0.4 — provider contract tests.** `test_llm.py::TestProviderToolPayloadContract`: built-in tool names satisfy all three providers' name rules; serializers preserve names; the registry rejects illegal names; the Gemini signature round-trips; the ollama message adaptation produces dict arguments. The in-process stand-in for provider-side request validation (the blind spot that let all of the above ship).

**Phase 0 — verified bug fixes (from the 07-01 audit).**

- **0.1** Whole-note LLM rewrite/custom-edit on a note over the 16 K input cap is refused up front (**422**, points at section scope) instead of silently staging only the reformatted head as the whole-note proposal — a tail-loss the WYSIWYG guard could not catch. Section-scoped edits unaffected. (`api/routes/refactor.py::_whole_note_truncation_guard`, `refactor/llm_edit.py::REWRITE_MAX_CHARS`.)
- **0.2** Deck outlines are capped to `max_sections` on the primary (model-reply) path, matching the already-capped instructions fallback. (`deckgen/outline.py`.)
- **0.3** Agent cost is no longer always $0: `usage_tracker.record()` writes the computed cost back onto the passed `LLMUsage` (the same object on every adapter's response), so `UsageBudget` sums real dollars; `llm_pricing_overrides` is now resolved from config at record time (it previously never applied to records). (`core/llm/usage.py`.)
- **0.4** Global `MAX_CONTENT_LENGTH` (550 MB, above the 500 MB streamed-upload cap) + JSON 413 handler — request bodies were previously unbounded outside `/api/upload`. (`app.py`.)
- **0.5** SQLite: per-connection redundant `journal_mode=WAL` dropped (persistent; set at init); per-connection `busy_timeout=5000` + `synchronous=NORMAL` added. (`core/database.py`.)
- **0.6** `mapping.json` fsyncs before its atomic rename (W6 durability — hand-curated, unregenerable). (`audit/engine/bridge.py`.)
- **0.7** Zotero snapshot hardened to the proven `zotero-debug` technique: copy sqlite + `-wal`/`-shm` (2 GB ceiling), open the copy `mode=ro&immutable=1`, `backup()` to `:memory:` — no SQLite locks on the live DB at all; a torn hot-WAL copy degrades to the previous direct path. Side effect: the audit test files no longer deadlock when Zotero.app is open. (`audit/core/zotero.py`.)

**Phase 1 — robustness hardening.**

- **1.1** Streaming chat render coalesced (vault + plain chat): the whole-answer `marked.parse` + sanitise + `innerHTML` now runs at most every 40 ms instead of per token (was O(n²) over the stream), with a final synchronous parse at `[DONE]` and a cancel on error. (`static/js/vault.js`, `plainchat.js`.)
- **1.2** Archive-image TOCTOU: the original is re-hashed immediately before the destructive unlink; if it changed during the thumbnail/copy window it is left in place (the archive holds only the pre-change bytes) with a warning. (`refactor/archive.py`.)
- **1.3** `retry_with_backoff` is deadline-aware: online adapters derive a monotonic deadline from `request.timeout_s` (= the agent loop's remaining wall-clock), backoff sleeps truncate to the remaining budget, and no attempt starts with < 0.5 s left. (`core/llm/retry.py`, three online adapters.)
- **1.4** Untrusted-content wrappers carry a per-call random nonce in the tag name (`<tool_output-a1b2c3d4 …>`, `<doc-…>`, `<note-…>`, referenced consistently in the system prompt), so content containing a literal closing tag cannot escape the wrapper. (`core/agent/tools.py`, `refactor/llm_edit.py`, `refactor/review.py`.)
- **1.5** The 30 s vision/OCR failure cooldown is config-tunable: `vision_failure_cooldown_s` (0–600, default 30, **0 disables**), read per call; validated in `/api/config`. Shorter values drop fewer images after a transient mid-index failure. (`services/vision.py`, `core/constants.py`, `core/config.py`, `api/routes/config.py`.)

## 2026-07-01 (Performance / execution-flow / resource-management pass)

A staged optimization pass from a four-part audit (web/routing concurrency, RAG pipeline, core infra I/O & locks, vision/refactor/deckgen writers). Every change carries thorough inline comments explaining how it preserves existing behaviour. **No reindex required by any item — none touches chunking, chunk IDs, embeddings, or the vector backend. Existing indexes stay valid in place.** Full suite green (850 passed).

- **W1 — Note Refactor: unify + cache the whole-vault name/link indexes.** `build_name_index` (images) and `build_link_index` (all files) always ran as **two** independent whole-vault `rglob` walks per plan/`analyze_one`, and `analyze_one` fires on a *debounced per-image OCR-inclusion toggle* — so flipping a few checkboxes on one note triggered several full-vault double-walks. New `resolver.build_file_index` produces both indexes in **one** pass (output-identical to the two builders), and `resolver.get_file_index` caches the pair keyed by (resolved vault path, exclusion set): `build_plan` refreshes it (the explicit re-scan), `analyze_one` + the pdf-refs/summarize-pdf routes reuse it (a toggle can't change the file set). Invalidated on file-set-changing Phase-2 writes (archive/restore). (`refactor/resolver.py`, `refactor/plan.py`, `api/routes/refactor.py`; +3 tests.)
- **W2 — Note Refactor: bound the on-demand LLM endpoints off the request thread.** `/rewrite`, `/custom-edit`, `/summarize-pdf`, `/chart`, `/review-note` ran their fully-consumed `stream_chat_messages` loop **inline on the Waitress worker thread** with no wall-clock bound, so a wedged local model (default `local_request_timeout_s=0`) could pin a pool slot indefinitely. New `_run_llm_action_bounded` runs each on a daemon thread and joins with `_llm_action_deadline_s` (the `agent_wall_clock_s` model, floored at the shared SSE floor + margin); on timeout the request thread is freed (**504**) while the daemon finishes on its own. The LLM call + its lock acquisition now run on the daemon thread, so a wedged call never pins a request thread. (`api/routes/refactor.py`; +1 test pins timeout→504 with the request thread freed early.)
- **W3 — Indexer: drop the redundant full-JSON re-parse from the checkpoint.** `_persist_index_checkpoint` validated the just-written checkpoint with `full=True`, `json.load`-ing every persisted file — on the `simple` backend re-parsing the ~3 GB `default__vector_store.json` (and the ~620 MB `docstore.json`) into a throwaway graph on **every** checkpoint, under the GIL + rw write lock (chat stalled). Switched to `full=False` (the two load-time callers already use it). Safe: the retained tail-check catches a truncated write; the read-back is page-cache-served so the full parse never validated on-disk bytes anyway; the authoritative parse still runs at load. lancedb backend unaffected.
- **W4 — LLM: reuse HTTP clients in the online adapters + LM Studio vision.** OpenAI/Anthropic/Google built a **fresh** HTTP client on every `generate()`/`stream()` — a new pool + TLS handshake per round-trip, N per agent turn / deck section. Now cached per (base_url, timeout[, org, key-fingerprint]): OpenAI folds a sha256 key-fingerprint (rotation-safe, key never stored); Anthropic/Google share a long-lived `httpx.Client` (auth is per-request, so key-agnostic) with the streaming path wrapped in `contextlib.nullcontext` so only the response closes. `_chat_lm_studio_image` now reuses the cached `get_lmstudio_client`. (`core/llm/adapters/*`, `services/vision.py`; +2 tests.)
- **W5 — Note Refactor: one local-model concurrency gate.** Vision extraction, prose review, and applyable LLM edits each owned an independent `Lock`, so all three could fire **simultaneously** at the same local backend (up to 3 concurrent inferences + the indexer) — the concurrent load that triggers the OOM/JIT-reload hiccups the deck retry layer papers over. New `refactor/local_model.py::LOCAL_MODEL_LOCK`; the three module locks are aliased to it (at most one refactor local-model call at a time). Only tightens concurrency; scoped to the refactor hub (not the indexer). (+1 test.)
- **W6 — Durability: `fsync` the low-frequency atomic writers before rename.** `write_text_atomic` / `write_bytes_atomic` (`core/utils.py`), `save_config`, and `rag/vault._write_json_atomic` did write → `os.replace` with no `fsync` — atomic but not durable (a power loss after the rename can leave it visible with unwritten blocks). Added `flush()`+`fsync()`; `write_bytes_atomic` is the one true data-loss path (the archiver moves a user's only copy of an image out of the vault). Deliberately **not** added to the per-document regenerable description cache (`_atomic_write_text`, per-image hot path). Matches the deckgen writers.
- **W7 — Retrieval: no-deepcopy read-only config view.** New `core.config.load_config_readonly()` returns a `MappingProxyType` over the shared cached dict (no per-call deepcopy; mutation raises loudly). The two per-query scalar readers (`_resolve_reranker_device_mode`, `_get_thesaurus`) use it. *(W7a — running the cross-encoder rerank outside `_index_mutation_lock` — was **deferred**: a safe split of the retrieve→rerank→synthesize flow would touch the query engine across both vector backends + the online/local streaming paths for a small gain; see Known Issues.)* (+1 test.)
- **Tier 3 (safe cleanups).** OCR in-memory cache key hashed incrementally (no multi-MB f-string per page); LanceDB migration compacts once after the bulk load; `make_thumbnail` closes its PIL intermediates; dead `RagOperationLock._owner_thread` removed; stale `lms.py` docstring fixed. *(Frame-slim, apply-recompute, usage-log rotation, and deck-checkpoint `raw` were deferred — they change a serialized contract / touch WYSIWYG guards.)*

## 2026-06-30 (Principal-review follow-ups — resume placeholder bug + writer-lock + polish)

Fixes from a principal-engineer review of the recent deck-resilience + QoL commits. **No reindex; behavioural fix + hardening.**

- **Deck resume no longer freezes a failed section (the real bug).** The checkpoint persisted *placeholder* (every-retry-failed) sections as if completed, and resume reused them verbatim — so a transiently-failed section that became a placeholder during an interrupted run was **never retried** on resume (the only escape, `force_fresh`, discards all good sections too — defeating resume for the exact case it targets). The `generate` worker now guards both the persist (`checkpoint.set_section`) and the reuse paths on `not out.placeholder`: a placeholder is left out of the checkpoint and re-attempted next run, while still riding in the current run's deck so it assembles. `reused_sections`/`resumed` now count only genuinely-reused real sections. (`api/routes/deck.py`; `test_deck.py` +1: a seeded placeholder section is regenerated, not reused.)
- **`apply-repair` / `apply-augment` now hold `_DECK_OP_LOCK`.** The two synchronous deck writers bypassed the lock the SSE routes hold, so an apply could race a streaming generate/augment (or another apply) and clobber a deck mid-flight (TOCTOU between the stale-diff read and the overwrite). Both now take the lock non-blocking (→ **409** if busy, released in `finally`) across read→screen→backup→write, so "one deck operation at a time" covers writers too.
- **Log viewer: a single >1 MB log line no longer reports "(log is empty)".** `GET /api/log/tail` dropped the first (partial) line on a truncated read unconditionally; a lone giant line was thus reduced to nothing. Now only dropped when more than one line is present (`api/routes/config.py`).
- **Example chips append instead of clobber.** Clicking a `renderExampleChips` chip overwrote any draft already in the field. It now fills an empty field but **appends** (after a blank line) to a non-empty one (`static/js/ui.js`).
- **OCR-preamble strip: documented the accepted false-positive tradeoff** of the noun-agnostic opener (can strip a genuine "This is a …" first sentence or truncate at "Fig."); no behavioural change — it stays opt-in, never-empty, reversible (`refactor/text.py`).

## 2026-06-30 (In-app log viewer · broader OCR-preamble strip · click-to-insert example prompts)

Three small, additive quality-of-life changes from a project review. **No reindex; all read-only or additive UI.**

- **In-app log viewer.** New read-only `GET /api/log/tail?lines=N` (local-origin gated like every route) tails `chatekld.log`: it reads at most the last ~1 MB from the end of the file (cost independent of file size), clamps `lines` to ≤ 5000, and runs **every** returned line through `core.llm.redact.redact` so an accidentally-logged API key can never reach the UI. It never writes/rotates/truncates the log. Surfaced as a new "Application log" section in the LLM Settings window (`settings.js`, lazy-loads on first expand; Refresh + Copy). New single-source `core.constants.LOG_FILE` — `launch.py`, `app.py`'s dev `__main__` handler, and the viewer all reference it, fixing a dev-vs-frozen log-path mismatch (`app.py` previously wrote a relative `chatekld.log`).
- **Broader OCR-preamble strip.** `refactor/text.py::strip_ocr_preamble`'s opener fallback was whitelisted to `image|page|figure|photo`, so descriptions opening "This is a **diagram** / flowchart / screenshot / presentation …" passed through verbatim. An empirical scan of the maintainer's 1788 cached descriptions showed this missed **12.5 %**; generalising the opener to a noun-agnostic "This is a/an …" (plus "This document …" / "This appears to be …") drops the miss rate to **1.7 %** (the rest are genuine non-preambles). Still strips only the first sentence, still passes through when no sentence boundary is found, never returns empty. No re-indexing (runs live at plan/apply time).
- **Click-to-insert example prompts.** New shared `ui.js::renderExampleChips` primitive renders chips that fill a text field on click (pure-DOM via `textContent`, fires an `input` event so existing save/`oninput` listeners react, idempotent per field). French, vault-tuned example sets wired onto Deck instructions, the Vault Chat system-prompt override, Plain Chat (`app.js::wireExamplePrompts`), and the Note Refactor free-prompt textarea (`refactor.js`). Chrome labels stay English; only the example *content* is French.
- Tests: `smoke_test.py` (+1: `/api/log/tail` tails + redacts a seeded key + 403-without-header + garbage-`lines` fallback); `test_refactor.py` (+4 strip cases: the noun-agnostic openers + a no-preamble passthrough).

## 2026-06-29 (Deck Generator resilience — Phase 3: per-section checkpoint + resume)

Turns "a deck generation failed → regenerate everything" into "it resumes". **No reindex; on by default, transparent when nothing fails.**

- **`deckgen/checkpoint.py`** (pure, app-independent — `json`/`hashlib`/atomic-writer only, like `scaffold.py`): a manifest under `BASE_DIR/deckgen/checkpoints/<job_key>.json` holding the parsed outline + each completed `SectionOutput`. `compute_job_key` hashes only the *content-determining* inputs (topic, instructions, template, provider/model, max_sections, audience, citations, slug, out_dir) — **not** the sampling/retry knobs — so re-running after tweaking temperature/attempts still resumes. `load` is tolerant (corrupt / wrong-version / missing ⇒ no checkpoint, fresh run); `save`/`delete` atomic + best-effort; `prune` keeps the 10 newest.
- **Route integration** (`generate`): the worker persists the outline immediately, then each section right after it completes. A re-submitted identical request reuses the saved outline (skips the outline turn) and every already-generated section, resuming from the first missing one. A fully successful scaffold deletes the checkpoint (+ prune); a scaffold failure keeps it so the next run still resumes. New config `deck_resume_enabled` (bool, default on); body `force_fresh` discards + regenerates. The terminal deck frame carries `resumed` / `reused_sections`.
- **Front (automatic resume).** `deck.js` sends `force_fresh` from a new "Start fresh" checkbox and shows a "Resumed: N section(s) reused" banner from the deck frame. No explicit resume button — re-clicking Generate with the same inputs just resumes.
- Tests: `deckgen/tests/test_checkpoint.py` (+4: job-key stability/sensitivity, manifest roundtrip, tolerant load, delete+prune); `test_deck.py` (+3: resume reuses outline + skips done sections, success deletes the checkpoint, `force_fresh` ignores it).

## 2026-06-29 (Deck Generator resilience — Phase 2: per-section output cap)

Reduces per-section load on a local backend — the cause of the memory-pressure timeouts (KV-cache / RAM OOM, model thrash). **No reindex; opt-in via a new default.**

- **`deck_section_max_tokens`** (256–8192, default 2048). The agent loop otherwise uses `online_max_tokens` (4096) for every reasoning call, local included. The deck route resolves this knob and injects it as the generate runner's `online_max_tokens` via a new `InProcessChatRunner(max_tokens=…)` param (mirrors the existing temperature → `vault_chat_temperature` injection; consolidated into one cfg-copy in `chat`). Affects deck **generation** turns only — augment, review, vault chat are untouched. Shorter slides, less RAM pressure, faster turns.
- **Settings UI.** The three new deck knobs (`deck_section_max_tokens` + the Phase-1 `deck_section_max_attempts` / `deck_retry_backoff_s`) are now editable in the LLM Settings window (Deck Generator section), owned by `settings.js` like the other `deck_*` knobs, with hints explaining the memory/latency trade-off.
- Tests: `test_deck.py` (+2: `max_tokens` lands on `online_max_tokens` and does not mutate the caller's cfg; `None` leaves it untouched).

## 2026-06-29 (Deck Generator resilience — Phase 1: per-section retry + non-fatal section errors)

Fixes the **random whole-deck failures** on local backends (LM Studio especially): a single section's transient provider error (memory hiccup / JIT model reload / momentary timeout) used to discard the entire generation. **No reindex; defaults change behaviour only on failure.**

- **Turn-level errors are no longer fatal.** The agent loop emits a `{"error"}` event on a provider failure, and the shared `_run_deck_sse` consumer treats any `{"error"}` frame as terminal — so one weak section aborted the whole deck, defeating `generate_section`'s placeholder fallback. The `generate` worker's `_on_event` now **relabels a turn-level `{"error"}` to a non-fatal `{"info"}`** (⚠ prefix); only the worker's explicit `put({"error"})` (an unrecoverable outline failure) stays fatal.
- **Per-section retry with backoff.** New `deckgen/retry.py::chat_with_retry` (pure, cancel-aware) wraps each outline/section turn; `request_outline` / `generate_section` gained optional `max_attempts` / `retry_backoff_s` / `should_cancel` params (defaults preserve the single-shot path byte-for-byte). Config: `deck_section_max_attempts` (1–5, default 3), `deck_retry_backoff_s` (0–30, default 3), validated on `/api/config`. A section that still fails after every attempt degrades to the placeholder frame.
- **SDK retries off for LM Studio.** `core/providers/lms.get_lmstudio_client` now forces `max_retries=0`. The OpenAI SDK otherwise retried a timed-out call up to 2 more times internally (each up to the full per-call timeout), so one wedged call could block ~3×timeout and trip the SSE consumer's stall window — surfacing as a spurious whole-stream timeout. With retries off, one call is bounded by exactly one timeout and recovery is owned at the app level (the new per-section retry, the agent loop's deadline).
- Tests: `deckgen/tests/test_retry.py` (+5: single-shot default, retry-until-success, attempt exhaustion, cancellation before/between attempts); `test_deck.py` (+1: a section `{"error"}` event is relabelled to info and the deck still lands with a placeholder); `test_llm.py` updated for the `max_retries=0` client kwarg.

## 2026-06-29 (Vault thesaurus — configurable file paths + primer content overrides)

Generalises the just-shipped vault thesaurus so it is no longer hard-wired to the maintainer's vault. **No reindex, no re-embedding; defaults preserve prior behaviour byte-for-byte.**

- **Curated-file paths are now config-driven.** `vault_thesaurus_abbrev_path` (default `_abreviations.md`) and `vault_thesaurus_tags_path` (default `_tags.md`) are vault-relative and resolved under the vault root in `ObsidianVaultManager._get_thesaurus` (new `_resolve_thesaurus_rel` rejects traversal/absolute/escaping; `/api/config` shape-checks via the new `_coerce_vault_rel_file`, `""` = slot disabled). The loader's signature cache now keys on the **resolved paths** as well as `(size, mtime_ns)`, so a path change busts the cache like a file edit. The old hard-coded `_THESAURUS_FILES` tuple is gone (replaced by `_THESAURUS_DEFAULT_*` fallbacks).
- **Primer content is overridable.** `vault_primer_header` replaces the glossary intro sentence and `vault_primer_core_terms` (comma-separated) replaces the priority abbreviation list (both empty ⇒ the built-in FR/EN-tuned defaults in `rag/thesaurus.py`); threaded through `build_primer(header=, core_terms=)` and read in `rag/engine.py::_build_primer`.
- **Bilingual headers.** The table parser now accepts English column headers (`Abbreviation | Meaning`, `Description | Tag`) in addition to French.
- **Settings UI.** All six config-only knobs are now editable in the LLM Settings window (vault section) — `set-thesaurus-*` / `set-primer-*` controls owned by `settings.js` (which already owned the sibling config-only `vault_*` knobs); the live enable toggles stay owned by `vault.js`. No double-save.
- Tests: `test_thesaurus.py` +6 (configured-path loading + traversal rejection, English-header parsing, header/core-terms overrides, `_coerce_vault_rel_file` shape).

## 2026-06-29 (Agent per-call timeout decoupled from `online_timeout_s`)

- **A slow local model no longer times out mid-turn on the online `Request timeout`.** The agent loop set each reasoning call's `request.timeout_s` to `min(online_timeout_s, remaining wall-clock)`, so a local model at e.g. ~4 tok/s was capped at the 60 s "Request timeout" even though the user's `agent_wall_clock_s` was much larger — the call died ~445 tokens in. Now the per-call timeout is the **remaining wall-clock budget** (`max(1.0, deadline − now)`); `online_timeout_s` is online-only as its label promises (the online adapters ignore `request.timeout_s` and bound themselves by their own `self.timeout_s`, so they are unaffected). Only the local adapter consumes `request.timeout_s` (via `_effective_local_timeout`, still further tightened by `local_request_timeout_s` when set). With no deadline (direct/test callers) it is `None` ⇒ local falls back to `local_request_timeout_s` / the SDK default. **No reindex; no API change.**
- **LLM Settings hints corrected.** The "Request timeout" hint no longer claims local models use it ("online providers only; local agent calls are bound by the Agent wall-clock cap"); the wall-clock hint notes it is the per-call bound for local agent calls (and is for the *whole* turn, all iterations); the "Local request timeout" hint explains it is a `min` (only tightens a call, never lengthens it).
- Tests: `test_agent.py` +2 (per-call `request.timeout_s` tracks the wall-clock budget, not `online_timeout_s`; `None` without a deadline).

## 2026-06-29 (Vault thesaurus — query expansion + abbreviation primer)

New **opt-in, default-off** vault-chat retrieval aids for a vault written in heavy bilingual shorthand. Query-time only — **no reindex, no re-embedding.**

- **Thesaurus query expansion** (`vault_thesaurus_expansion`, default `false`; body field `thesaurus_expansion`). When a question contains a known concept/abbreviation, the engine issues up to `vault_thesaurus_max_variants` (1-8, default 3) extra queries — each substituting a known synonym — retrieves them separately, and RRF-fuses, so the dense leg is not diluted and BM25 can hit the synonym token. Implemented as `_ThesaurusExpansionRetriever` in `rag/engine.py`.
- **System-prompt primer** (`vault_primer_enabled`, default `false`; body field `primer_enabled`). Prepends a compact glossary block (≤ `vault_primer_max_chars`, 500-8000, default 1500; query-relevant entries first) to the answer-mode template so the model can read shorthand that survives into the retrieved chunks. On the online path it is routed through `LLMRequest.system_prompt`.
- **Source files.** Both parse two hand-curated Markdown tables at the **vault root**: `_abreviations.md` (`Abréviation | Signification`) and `_tags.md` (`Description | Tags`). Filenames are hard-coded (`ObsidianVaultManager._THESAURUS_FILES`, one-`b` French spelling); both features no-op when neither file exists. Read lazily, cached by `(size, mtime_ns)` (an edit refreshes on the next chat), cleared on vault switch / `cleanup()`. Never written.
- **App-independent core.** `rag/thesaurus.py` is stdlib-only (like `rag/lancedb_store.py`): accent/case-insensitive whole-token matching with a ≥2-char floor, `≠`-flagged collisions and morphological (`...q`) rows excluded from expansion. Both consumers are best-effort — a parse/expand/primer failure degrades to seeds-only / no-primer rather than crashing the chat.
- **Scope.** Single-shot RAG and the agent's RAG fallback only — **not** the agent's active `vault.search` (parity with `wikilink_expansion`/`mmr`/`query_expansion`). The depth knobs (`vault_thesaurus_max_variants`, `vault_primer_max_chars`) are config-only, validated/clamped on `/api/config`. The current glossary content is tuned for the maintainer's bilingual FR/EN psychiatry/clinical-research vault.
- Tests: `test_thesaurus.py` (parser, expansion, primer); `test_vault_regressions.py` integration coverage.

## 2026-06-28 (Principal-review follow-ups — correctness, retention, deck-repair hardening)

Fixes from a deep skeptical review of the last ~20 commits. **None touches the chunking / chunk-ID / embedding path — no reindex required.**

- **Archive restore can no longer orphan a thumbnail embed.** `refactor/archive.revert_archive_image` only deleted the thumbnail *after* restoring the note to re-embed the original — but a failed snapshot restore appended a warning and fell through to the deletion anyway, leaving the note pointing at a now-deleted thumbnail. The note-restore failure path now **aborts the revert** (thumbnail + archive copy left in place, op kept `applied` for retry) so a half-revert can never leave a broken embed.
- **Bounded restore history + O(N) batch save.** The restore manifest, its note snapshots (`notes/*.bak`), and the LLM staging cache grew without limit, and `apply`/`normalize` re-serialized the whole manifest *per note* (O(N²) per batch). New `journal.prune` (cap `_MAX_OPS=200`, drops reverted/failed ops + reclaims their snapshots, evicts oldest note-writes over cap but never an applied `archive_image` op) runs after each write/restore; the batch writers now persist the manifest **once** after the loop (per-note snapshots stay durable-before-write for crash recovery). `staging.stage` sweeps proposals older than 7 days.
- **List-aware formatting normalizer.** `normalize.normalize_text` no longer inserts a blank line before a list item that merely continues a list with a lazy (indented) continuation line — which used to split one tight list in two. A shared `hygiene._next_list_context` tracks list context for both the detector and the fixer, preserving the `structure_notes(normalize_text(x)) == []` + idempotency invariants.
- **Deck dangerous-macro screen hardened.** `deckgen.assemble.find_dangerous_macros` (shared by `validate` + `review.screen_repair`) now catches the `\csname write18\endcsname` / `\@@input` obfuscations the bare `\macro` regex missed, and the residual limits (catcode / char-by-char construction) are documented honestly instead of over-claimed.
- **`/api/deck/apply-repair` now genuinely mirrors Note Refactor's safety.** It requires a `base_sha256` stale-diff token (409 if the on-disk deck changed since the review), **re-screens** the submitted `.tex` against the current deck (blocks a smuggled shell-escape), and rewrites **only** `<slug>.tex` (`scaffold.write_deck_tex`) so a hand-edited `Makefile` is no longer clobbered. The generate frame carries `tex_sha256`.
- **Review parsing + truncation signal.** `parse_review`'s "no issues" test is now an **anchored whole-body** match (prose merely containing "none"/"n/a" no longer silently drops a real finding), and a repair cut off by the token cap is flagged (`repair_truncated`) so the UI says "raise `deck_review_max_tokens`" rather than implying no repair was attempted.
- **OCR-inclusion panel stale-response race.** The single-note re-analyze (`/api/refactor/note`) now cancels the prior in-flight request (AbortController) and guards responses with a monotonic token, so a slow older response can't overwrite newer preview/hash state; switching notes drops a pending re-analyze.
- **LM Studio client no longer leaks an httpx pool per call.** `core.providers.lms.get_lmstudio_client` caches the `openai.OpenAI` client per `(base_url, timeout)` (thread-safe, mirrors the Ollama cache), used by both the streaming and agent-tool paths; a non-positive timeout leaves the SDK default. The agent tool-call timeout now uses `round` (matching the client caches) instead of `ceil`, so it can't overrun the wall-clock deadline by ~1 s.
- **Misc.** The chart "Copy" button routes through the shared `copyToClipboard` (honest success/failure + a11y); the indexer no longer double-warns on the first insert failure.
- Tests: `test_refactor.py`, `test_deck.py`, `test_llm.py` extended (archive orphan-guard, prune/cap, staging TTL, list-continuation normalize, `\csname` screen, apply-repair stale/re-screen/Makefile, parse_review substring + truncation, LM Studio client caching, timeout rounding). Full suite green.

## 2026-06-28 (Note Refactor — per-image OCR-inclusion panel + UI scroll/layout fixes)

- **Per-image OCR inclusion is now a discoverable panel.** A collapsible *Images — inclure l'OCR* panel sits above the preview and lists **every** attached image of the note with an include checkbox (opt-out: checked = inlined) plus *Tout inclure* / *Tout exclure*. You can include one, several, or not all images — and handwritten scans too (the checkbox forces `keep_handwritten`). Replaces the easy-to-miss per-row checkbox. Toggling persists to the ignore-list / flag sidecar and triggers a **debounced single-note re-analyze** so the preview + Apply hashes refresh immediately.
- **New `POST /api/refactor/note`** (`plan.analyze_one`) re-analyzes **one** note read-only and returns its fresh proposal frame — the cheap counterpart of `/plan` (1 `analyze_note` vs 135) used by the inclusion panel; no vision, no vault writes. Shares the same `analyze_note` transform, so preview == apply still holds (pinned by a test).
- **UI fixes (relaunch-visible).** The Note Refactor tab reused the `.deck-body` class but the scroll region was scoped to `#deck-tab` only, so the analyzer's preview + image rows clipped off the bottom of the `overflow:hidden` card with no scrollbar — `#refactor-tab .deck-body` now gets the same scroll region. The Phase 3 action controls (formatting-fix approve, Review prose, LLM actions, the new inclusion panel) were appended **after** the tall ORIGINAL/PROPOSED preview and rendered below the fold — `_renderDetail` now appends the action controls first, then the preview, then hygiene + images.
- Tests: `test_refactor.py` +3 (`/note` re-analyze reflects an ignore toggle without touching the file, `/note` scope-lock, `analyze_one == build_plan` parity). Full suite green.

## 2026-06-28 (Note Refactor Phase 3 — per-note opt-ins, on-demand LLM actions, free-prompt edit, sub-note scope)

Six requested improvements to the Note Refactor tab. **None touches the chunking / chunk-ID / embedding path — no reindex required.** All applyable LLM output is generated and cached **server-side** (`refactor/staging.py`) and written through the same stale-diff + WYSIWYG + UTF-8 guards and journal/Restore discipline as the existing writers — the client only ever passes hashes, never note bytes.

- **(a) Per-note formatting-fix opt-in.** "Fix formatting" is no longer select-all: each note gets a detail-pane approve checkbox (default off, `_normApproved`), mirroring Apply. The `/api/refactor/normalize` route already took a `notes` list, so this was frontend-only.
- **(d) Per-image OCR-include control.** Each image row gains an *Include this image's OCR in the note* checkbox (backed by the sticky ignore-list), so inclusion is decided image-by-image rather than appearing all-or-nothing.
- **(f) Heading-section sub-note scope.** New `refactor/sections.py` (pure splitter: a section = a heading + its body up to the next same-or-shallower heading, plus a synthetic intro block; trailing blanks sit outside every span so `replace_section` splices a new body back **byte-identically outside the span** and an identity replace returns the exact original). `POST /api/refactor/sections` lists them; every LLM action accepts an optional `section_index`.
- **(b) Applyable LLM formatting rewrite.** `refactor/llm_edit.rewrite_formatting` (formatting only, content preserved) → `POST /api/refactor/rewrite` stages action `rewrite` and returns a preview diff.
- **(c) Applyable PDF summary.** `refactor/pdfref.py` resolves a note's `.pdf` embeds and reuses the indexer's cached PDF text (`ObsidianVaultManager._read_pdf_text`); `llm_edit.summarize_pdf` produces 5–10 bullets; `POST /api/refactor/pdf-refs` + `/summarize-pdf` inline a `> [!summary]` callout beneath the embed and stage action `summarize_pdf`.
- **(e) Advisory Mermaid diagram.** `llm_edit.generate_chart` → `POST /api/refactor/chart` returns a ```mermaid``` block for display/copy only — never staged or written.
- **Free-prompt single-shot edit.** `llm_edit.custom_edit` + `POST /api/refactor/custom-edit` (`{instruction}`, optional `section_index`): your own instruction is the trusted task, the note stays untrusted `<doc>` source, and content changes are allowed (preview + confirm + Restore are the safety net). Stages action `custom`.
- **New generic writer + route.** `refactor/llm_apply.apply_staged_note` writes any staged proposal (op kind `llm_note`, restore via `journal.revert_op`→`revert_apply_note`); `POST /api/refactor/apply-staged` (`confirm: true`, op-locked) reads the body from staging server-side and lays it down. `journal.revert_op` now dispatches `llm_note` alongside `apply_note`/`normalize_note`.
- **UI.** `_renderLlmActions` adds an "LLM actions" panel: section-scope selector (lazy `/sections`), *Improve formatting* / *Summarize a PDF* / free-prompt textarea → preview diff → *Approve & apply*; *Generate diagram* (advisory). Typed instruction kept per note (`_customInstruction`). CSS added.
- **Config.** New `refactor_rewrite_max_tokens` (default 4096; validated 256–16384). Summary/chart reuse `refactor_review_max_tokens`; the model for all of them is `refactor_review_model` (→ chat model when empty). No other new keys.
- **Docs.** `CLAUDE.md` (module ownership, config, API routes), `README.md` (vault-write banner + feature list + module list + route table), and `PROMPTS.md` (new §7: the review + all four LLM-edit prompts) updated.
- Tests: `test_refactor.py` +20 (sections split/replace/identity, staging roundtrip + unknown-action reject + `custom`, `apply_staged_note` full cycle/restore/stale+WYSIWYG guards, `llm_edit` rewrite/custom/chart/summarize parsing, `pdfref` resolution, and the `/sections` `/rewrite` `/custom-edit` `/chart` `/pdf-refs` `/summarize-pdf` `/apply-staged` routes + the new config validator). Full suite green.

## 2026-06-28 (Code-review follow-ups — reindex-free correctness, robustness & doc-sync)

Follow-ups from a deep review of the last ~10 commits. **None touches the chunking / chunk-ID / embedding path — no reindex required.**

- **Deck integrity review can no longer discard a completed deck.** The opt-in `.tex` review ran *before* scaffolding, so a slow/stalled review (the local path has no app-level wall-clock by default) could trip the SSE consumer stall guard and throw away a fully-generated deck. The deck is now `scaffold`-ed to disk **first**, then reviewed; and `_run_integrity_review` takes a `deadline_s` (= `agent_wall_clock_s`) that bounds the streamed pass so it can't outlive the consumer window. A timed-out/over-large review degrades to **issues-only** with an explicit "deck too large for an auto-repair" note (the repair half is bounded by `REVIEW_MAX_CHARS` / `deck_review_max_tokens`).
- **Note Refactor cross-action staleness.** After a callout **Apply**, the note's formatting-fix is retired (and vice-versa: after **Fix formatting**, the callout-apply is retired — checkbox unchecked+disabled, dropped from the approve set) until a re-plan, since writing one transform makes the other's previewed hash stale. Previously the UI still offered the sibling action, which the server then rejected as drift (safe but confusing). The scope-wide strip toggle now `await`s its plan re-run and is disabled during a run so a mid-run toggle can't be silently dropped.
- **Honest whitespace advisories + CRLF.** `hygiene.whitespace_notes` now counts trailing whitespace only over the lines `normalize` actually rewrites (skips frontmatter + fenced-code interiors), so "auto-fixable" is truthful; CRLF files get a single "will convert to LF" advisory instead of being miscounted as every-line-trailing-whitespace. `normalize.py` documents the CRLF→LF behavior.
- **Local-error classification is type-based.** `core/llm/adapters/local.py::_classify_local_error` now classifies by exception **type** (`isinstance` against captured httpx/openai/builtin transport classes + an exact class-name fallback) instead of substring-scanning `str(exc)` — so a non-transport error whose message merely contains "connection"/"timeout" no longer becomes retryable and triggers an unwanted failover to a paid online provider. Real connection/timeout failures still fail over.
- **Wider dangerous-macro denylist + correct terminator.** `deckgen`'s `_DANGEROUS_MACRO_RE` adds `openout`/`special`/`directlua`/`luaexec`/general `\write`, and switches the terminator from `\b` to `\d*(?![a-zA-Z])` so stream-numbered forms (`\write18`, `\openout15`) are caught and named in full while the safe `\includegraphics` is still excluded. Advisory + screen-only (only blocks a repair that *introduces* one).
- **Frontend/markdown hardening.** `ui.js::sanitiseHtml` now also strips `href`/`src`/`xlink:href` using `vbscript:` or `data:text/html` (not just `javascript:`). New `api.js::safeJson` makes the refactor/deck action handlers surface the real HTTP status on a non-JSON error body instead of a generic parse failure. Plain Chat resolves its transcript container before locking the UI so a missing element can't permanently wedge the panel.
- **Audit-trail honesty.** The `.bak` index-backup prune now logs the deletion **after** confirming `rmtree` actually removed the dir (it uses `ignore_errors=True`), so the audit line can't claim a removal that silently failed.
- **Docs sync.** README route tables gain `/api/deck/apply-repair`, `/api/refactor/normalize`, `/api/refactor/review-note`, `/api/refactor/flag`; the deck `generate` frame documents `review`; the vault-write exception and writers list include **Fix formatting**; `deckgen/README.md` documents that `apply-repair` re-validates but does not re-screen, and the large-deck issues-only degradation; the CLAUDE.md self-citation of the README is reconciled.
- Tests: `test_deck.py` (+4: extended-macro screen, keep-original-macro, review deadline, large-deck degrade), `test_refactor.py` (+5: fence-interior trailing-ws skip, CRLF advisory, advisory-closes-after-normalize, CRLF→LF idempotency), `test_llm.py` (+1: type-based classification vs message decoy). Full suite green.

## 2026-06-28 (Note Refactor: deterministic formatting fixer, scope-wide preamble strip, link/whitespace advisories)

Four improvements aimed at screenshot-heavy knowledge folders (auditing a 135-note French psychiatry folder surfaced 1,184 missing-blank-line issues, 1,586 OCR callouts carrying a verbose "This image is…" preamble, and 2 broken `[[wikilink]]`s the tool couldn't see).

- **#2 — Deterministic formatting fixer (second Phase 2 batch writer).** New `refactor/normalize.py` is a **pure, idempotent** Markdown normalizer that applies only the unambiguous, zero-false-positive fixes `hygiene.structure_notes` already flags (blank line before a heading/list with content above; blanks around code fences) plus trailing-whitespace strip, 3+-blank-run collapse, and a single final newline. It reuses `hygiene`'s detectors so `structure_notes(normalize_text(x)) == []` and `normalize(normalize(x)) == normalize(x)`; frontmatter and fenced-code interiors are emitted verbatim; **tab→space and NBSP→space are deliberately excluded** (too opinionated — left as advisories). New `refactor/format_fix.py::apply_normalize` writes it under the **same** stale-diff + WYSIWYG (`normalized_sha256`) + UTF-8 guards and journal-before-write discipline as `apply.py` (op kind `normalize_note`; restore reuses `revert_apply_note`). New `POST /api/refactor/normalize` mirrors `/apply`; the planner computes the `normalized`/`normalized_sha256`/`normalize_diff` per note (zero extra cost), and `PlanResult` gains `normalize_changed_count`. UI: a "Fix formatting (N)" batch button + confirm modal + a third "Formatting fix" detail-pane diff view. The callout-apply and format-fix transforms are independent (each its own reversible op), so applying one makes the other's `content_sha256` stale — re-run the plan between them.
- **#1 — Scope-wide "strip OCR preamble" default.** New config knob `refactor_strip_preamble_default` (bool, default `False`), **additive** to the per-image `strip` flag and read identically by the plan and the apply writer (config is the single source of truth, so the WYSIWYG guard holds; no body override). Threaded through `analyze_note(strip_default=…)`/`build_plan`/`apply_notes`. UI: a toolbar checkbox that persists to config and re-runs the plan.
- **#3 — Frontmatter + link hygiene.** `hygiene._INLINE_TAGS_RE` now flags only a **bracket-less** `tags: a, b, c` — a valid YAML flow list `tags: [a, b, c]` is no longer a false positive. New `hygiene.link_notes` flags broken **non-embed** `[[wikilink]]`s (resolved by basename against the new whole-vault `resolver.build_link_index`, conservative to avoid false breaks; `None`/empty index short-circuits so the apply re-analysis pays nothing).
- **#5 — Whitespace/encoding advisories.** New `hygiene.whitespace_notes` adds one summary advisory each for trailing whitespace, non-breaking spaces, tab indentation, and a missing final newline (messages mark which are auto-fixable via #2).
- Tests: `test_refactor.py` gains 13 cases — normalizer (idempotency, structure-advisory closure, frontmatter/fence preservation, blank-run collapse, empty passthrough), the `format_fix` writer (write+snapshot, stale-diff + WYSIWYG skips, restore via `revert_op`'s `normalize_note` branch), the `/api/refactor/normalize` route (confirm gate + write), the strip-preamble default (preview == apply across both defaults, config validator + default), tags flow-list vs bare, `link_notes` resolve-vs-broken, `build_link_index` coverage, and `whitespace_notes` summaries.

## 2026-06-28 (Deck Generator: opt-in LLM .tex integrity review + auto-repair)

- **The Deck Generator can now run a final-stage LLM integrity pass over the assembled deck.** Off by default; enabled per run by the new Deck-panel checkbox or persisted via `deck_review_enabled` in LLM Settings (body field `review_enabled`). When on, after `assemble` + the existing regex `validate`, the route (`api/routes/deck.py::_run_integrity_review`) makes **one RAG-free `core.llm.chat.stream_chat_messages` call** over the whole `.tex` asking the model to flag compile-blocking problems (unbalanced braces/math/environments, malformed `\begin{frame}`, stray control sequences, unescaped specials) and, when it can, return a corrected copy of the document. This stays **emit-only** — no LaTeX compiler is invoked, so the pass is a smarter heuristic, not a guarantee.
- **Auto-repair is screened and preview-only.** `deckgen/review.py::screen_repair` **refuses** any proposed repair that introduces a dangerous compile-time macro the original lacked (`\write18`/`\input`/`\immediate`/… — blocks a prompt-injected vault note from smuggling shell-escape in through the "repair") or that loses the single `document` environment / all frames. An accepted repair is only ever **offered** in the terminal `{"deck": {…, "review": {ran, issues, changed, repaired_tex, repaired_warnings, truncated, error}}}` frame; the original deck is still scaffolded to disk. The user applies a repair explicitly via the new `POST /api/deck/apply-repair` (`confirm: true`; reuses the generate path/slug validators; overwrites `<slug>.tex` in place) — mirroring Note Refactor's preview-then-apply discipline.
- **`deckgen/review.py` is pure** (no LLM/transport import, like `assemble.py`/`scaffold.py`): `build_review_messages` (wrap + truncate the deck — a deck over 60k chars is reviewed head-only and never auto-repaired), `parse_review` (issue bullets + the last whole-document fenced block; a truncated/unclosed block degrades to issues-only), `screen_repair`. The model call lives in the app route, so deckgen's "`inprocess.py` is the only app-coupled core module" invariant holds. New config knobs `deck_review_model` (`""` ⇒ chat model — point the pass at a stronger model) and `deck_review_max_tokens` (256–16384, must fit the re-emitted document), validated in `api/routes/config.py`.
- Tests: `test_deck.py` gains 12 cases — `parse_review`/`screen_repair`/`build_review_messages` units (issue+block extraction, truncated-block rejection, dangerous-macro rejection, no-frames rejection, no-op), the `apply-repair` route (confirm gate, bad-dir/origin guards, overwrite-in-place), and two full `/api/deck/generate` SSE integration tests (review attaches the screened repair frame; disabled ⇒ no review LLM call).

## 2026-06-23 (Indexing: backoff in the insert-failure breaker)

- **Transient embedding-backend blips no longer abort a long indexing run.** The consecutive-insert-failure circuit breaker (`rag/vault.py::_index_documents_streaming`, abort after `_MAX_CONSECUTIVE_FAILURES`=20) had no delay between attempts, so a backend that *instant-rejects* — observed as LM Studio returning HTTP 400 "Failed to decode batch!" in ~10 ms while it JIT-reloads the embed model under memory pressure — burned all 20 "retries" in ~0.2 s and killed a multi-hour run before the model could finish loading. The breaker is now a **wall-clock window**: each consecutive failure waits `min(_FAILURE_BACKOFF_BASE_S * 2**(streak-1), _FAILURE_BACKOFF_CAP_S)` (1.0 / 5.0 s → ~87 s across the 19 pre-abort sleeps) via the interruptible `self._stop_event.wait(delay)`, so a brief hiccup gets time to recover and a streak-resetting success cancels the backoff, while a truly-down backend still aborts in under a minute. The sleep holds no lock and runs off the chat path; a Cancel/Pause mid-sleep aborts promptly. Dropped blip chunks self-heal on the next incremental run (never inserted ⇒ re-yielded). New class attrs `_FAILURE_BACKOFF_BASE_S`/`_FAILURE_BACKOFF_CAP_S` (patchable to 0 in tests, like `_PERSIST_MIN_INTERVAL_S`).
- Tests: `test_vault_regressions.py::test_index_streaming_backoff_spaces_and_caps_consecutive_failures` (exponential-then-capped delays, no real sleep) and `...backoff_is_interruptible_by_cancel` (stop event mid-sleep aborts before the threshold); the two existing failure tests patch the backoff to 0.

## 2026-06-23 (Production hardening — disk, leaks, fallback, stall bounds)

Code-review follow-ups: bound an unbounded-disk-growth path, trim avoidable per-poll/per-call allocation under the 16 GB constraint, make a local backend going offline fail over instead of erroring, and stop a guard-less SSE route hanging on a wedged model.

### Resource management
- **Unbounded `.bak` index dirs pruned.** Every `OBSIDIAN_INDEX_VERSION` bump archives the whole prior index to a timestamped `obsidian_storage.bak.*` sibling and nothing ever deleted them — tens of GB could accumulate. `rag/vault.py::_prune_old_index_backups` keeps the newest 2 and routes each removal through `log_storage_deletion` (deletion-audit invariant). Called from `_archive_old_index_dir` after the rename.
- **Status-poll lancedb count cached.** `get_status()` is polled ~1 Hz and only needs `count > 0`, but each call opened a fresh `lancedb.connect()`. `_cached_lancedb_count` caches by the lancedb dir's `(st_size, st_mtime_ns)` with a 5 s TTL backstop; exact-count callers (crash-drift recovery, checkpoint validation) keep calling `lancedb_table_count` directly.
- **tiktoken encoder memoized** (`core/llm/usage.py`, `core/providers/lms.py`) via `functools.lru_cache` — was rebuilt per call (the LM Studio `_tokenizer` property potentially per chunk).
- **Ollama client-cache keyspace bounded** — `_ollama_client` rounds a non-None timeout to whole seconds (floored at 1 s so a sub-second value can't round to 0.0 and make httpx time out immediately) before keying, so the agent loop's near-continuous remaining-budget float no longer mints a fresh never-evicted `ollama.Client` per call.

### Graceful degradation / robustness
- **Local connection/timeout errors are now fallback-eligible.** `core/llm/adapters/local.py::_classify_local_error` maps a connection-refused/timeout from a stopped Ollama/LM Studio to `NETWORK`/`TIMEOUT` (retryable) instead of the default `UNKNOWN`, so a configured online `fallback_provider` actually takes over (matching the online adapters). Applied at all four transport catch sites.
- **`/api/summarise` stall bound.** That route streams synchronously with no consumer stall guard; at the default `local_request_timeout_s=0` a connected-but-wedged local model could hang it. The summariser now passes an explicit per-read floor (`PAPER_LOCAL_STALL_TIMEOUT_S=120` when the knob is unset) threaded through `OllamaProvider`/`LMStudioProvider.stream_chat(request_timeout=...)`.
- **Guarded local stream extraction** — the summariser reads chunk fields via `getattr` and skips empty/keep-alive frames instead of raising `IndexError`/`AttributeError` mid-stream.

### Logging / hygiene
- `err.message` redacted before logging at the three fallback-warning sites (`rag/summarizer.py`, `core/llm/chat.py`, `rag/engine.py`).
- Dropped two unused imports (`refactor/apply.py`, `refactor/archive.py`); commented two best-effort `except` swallows in `pdf_extractor.py`.

### Tests
- New `test_llm.py::TestLocalErrorClassification` (network/timeout/name-based/unknown/passthrough) and `test_vault_regressions.py::TestIndexBackupPrune` (keeps newest 2, audits each removal, leaves the live dir + unrelated siblings untouched).

## 2026-06-23 (Privacy / nomenclature scrub)

Portability + privacy pass ahead of sharing the app: remove maintainer-specific identifiers and the lingering old project name from shipped defaults, code, and docs so a fresh install on another Mac carries no personal data and consistently calls itself **ChatEKLD**.

### Changed — defaults now neutral
- `refactor_scope_subdir` and `_DEFAULT_SCOPE` default to `""` (no personal default folder name — an empty scope resolves to `None` → a clean 400, so the user must pick a folder). `audit_master_bib_path` / `audit/config.py::DEFAULT_MASTER_BIB_PATH` default to a neutral `_master.bib` (was a maintainer-specific sub-folder path). UI placeholders in `templates/index.html` genericised.

### Changed — nomenclature
- Renamed the build-script env var to `CHATEKLD_VENV_DIR`, keeping the legacy `PAPERMIND_VENV_DIR` as a deprecated fallback and `~/venvs/papermind2026` as the default path (existing venvs keep working). Replaced "papermind" → "ChatEKLD" in docstrings/instructions (`audit/`, tests, `project_structure.txt`, `README.md`, the `CLAUDE.md` deep-notes). `CHANGELOG` history left intact.

### Removed — personal identifiers
- Stripped 12 `file:///Users/<user>/…` absolute links from `PROMPTS.md` (now repo-relative). Genericised the maintainer's vault path, scope-folder name, vault-key hash, and study-domain description across `docs/project_note_refactor.md` and the `test_refactor.py` fixture (placeholders `<vault>` / `<scope>`, neutral fixture name `study_notes`); removed a contributor name from a `core/llm/model_listing.py` comment.

### Security / hardening
- `/api/log` now strips CR/LF and runs the message through `core.llm.redact.redact` before logging — closes a log-injection vector (forged audit lines) and restores the uniform "redact before logging" discipline.
- Dev-only `app.py:__main__` `app.run(...)` now pins `host="127.0.0.1"` explicitly (mirrors `launch.py`), and the README documents the un-sandboxed first-launch TCC consent prompt alongside the existing Gatekeeper/quarantine note.

## 2026-06-22 (Plain Chat — RAG-free multi-turn panel)

A sixth tab: a plain, multi-turn conversation with the globally-configured chat provider/model — **no vault retrieval, no agent loop, no tools**. The server is stateless; the browser owns the conversation and re-sends the (capped) history on every turn, so a chat is ephemeral (lost on reload).

### Added — backend
- `POST /api/plainchat` (`api/routes/plainchat.py`): an SSE route that streams `{info}` / `{token}` / `{error}` frames + the `[DONE]` sentinel. The queue + daemon-worker + consumer skeleton is lifted from `api/routes/vault.py::api_obsidian_chat` with **every** agent / iteration / tool branch stripped. `_validate_messages` (a) structurally validates the body (a list of `{role∈{user,assistant}, content:str}`; a malformed entry rejects the whole request rather than silently dropping a turn), (b) caps each message to 24 000 chars and the array to the last 20 turns, then (c) **normalizes for provider shape** — drops leading assistant turns and merges consecutive same-role turns — so strict-alternation providers (Anthropic/Gemini) accept a window sliced mid-exchange. Provider/model resolve from config (`resolve_chat_model`); `temperature`/`system_prompt` resolve **body → persisted `chat_*` config → hard default**, so a Settings change applies on the next send with no reload (the panel sends neither).
- `core/llm/chat.py::stream_chat_messages`: the unified local + online RAG-free streaming helper. Modelled on `rag/summarizer.py::_stream_online` but driven by a full `messages` array (online adapters send native message arrays; the local adapter flattens to a role-tagged prompt), so multi-turn works on every provider with no model-layer change. Falls back to `fallback_provider` **only before the first streamed token**; usage/cost tracking fires automatically inside the adapter.

### Added — frontend
- `static/js/plainchat.js` (`window.chatPlain` / `window.plainchatNew`, `ui.js`+`api.js`-only): keeps the `{role, content}` history array, sends only `{messages: history.slice(-20)}`, renders answers via vendored `marked` + `sanitiseHtml` with a `textContent` fallback, disables **Send** for the in-flight turn, renders a muted **non-recorded** bubble on an empty answer, and rolls back the un-answered user turn on error so a retry re-sends it exactly once. New **Plain Chat** tab + sidebar `chat_temperature` slider / `chat_system_prompt` textarea (persisted by `settings.js`, not `plainchat.js`).

### Safety / correctness
- On an empty-but-clean stream the route emits **no** synthetic token — a placeholder would be recorded into the client's history and re-sent as a fake assistant turn. The bounded worker queue (`maxsize=512`) applies back-pressure; the `cancel` Event lets a timed-out/disconnected consumer stop the worker; the consumer's `event_q.get` stall guard is the path's only time bound (plain chat has no agent wall-clock).

### Config / constants / tests
- New persisted keys `chat_temperature` (default `0.3`) and `chat_system_prompt` (default `"You are a helpful assistant."` — the **full** system prompt, ≤`SYSTEM_PROMPT_LIMIT`, no vault-grounding preamble). Validated/clamped in `api/routes/config.py::_validate_llm_config_keys` (`chat_temperature` 0–2, `chat_system_prompt` string-capped, empty kept).
- `SSE_SINGLE_SHOT_FLOOR_S` (300) / `SSE_STALL_MARGIN_S` (30) promoted to `core/constants.py` so the vault, deck, and plain-chat SSE routes share one stall model instead of cross-importing `vault.py` privates.
- Hermetic `test_plainchat.py` covers message validation/normalization, the body→config→default resolution, the empty-stream contract, and the stall/error frames.

## 2026-06-22 (Note Refactor — Phase 2: first vault writers — apply / archive / restore)

The app's **first feature that writes to the user's Obsidian vault**, scoped to one sub-folder and entirely opt-in (off by default; every action requires an explicit `confirm`). This supersedes the README's former "ChatEKLD never writes to your vault" banner (now: "writes only via Note Refactor's Apply/Archive"). Phase 1 previewing, re-extraction, and classification remain read-only.

### Added — bulk Apply (callout-only, WYSIWYG)
- `POST /api/refactor/apply` (`refactor/apply.py`): writes the **same** callout-only proposal the user previewed — the advisory `> [!extracted]` callout inlined beneath each described embed, with original embeds **kept**. Batch of approved notes, each applied independently (one failure never aborts the rest). Two guards protect every write: a **stale-diff** guard (the note's on-disk sha256 must equal the plan's `content_sha256`) and a **WYSIWYG** guard (the server-recomputed body's `proposed_sha256` must equal what the UI previewed). A note is only written when its bytes are a clean UTF-8 round-trip. The per-note transform is now the shared public `refactor.plan.analyze_note(...)`, so preview == apply by construction.

### Added — per-image Archive (move-out + thumbnail)
- `POST /api/refactor/archive` (`refactor/archive.py`): an explicit, per-image action (never coupled to the bulk apply). A **vault-wide reference-safety check** refuses any image embedded by another note (409 `{shared:true}`). Otherwise it materialises the original (iCloud-safe), writes a ≤384px **PNG thumbnail** into the excluded `<scope>/_thumbs/` folder (auto-added to `vault_exclude_dirs`), copies the full-res original into the recoverable archive dir (copy → verify → delete), and swaps that one embed to the thumbnail.

### Added — Restore + manifest
- `POST /api/refactor/restore` (`{op_id}` or `{all:true}`) reverses any apply/archive: note body from snapshot, archived original back to its vault path, thumbnail removed. `GET /api/refactor/manifest` lists the ops for the Restore UI. `refactor/journal.py` owns the per-vault `manifest.json` (restore journal), scope-lock (`assert_under`), and archive-dir resolution (rejects a dir inside the vault).

### Safety
- Atomic writes only (`core.utils.write_text_atomic` + new `write_bytes_atomic`). Every write/move is scope-locked to `<scope>` (+ `_thumbs/`) or the archive dir (outside the vault). Each write endpoint requires `confirm: true` and holds `obsidian_manager.try_acquire_lock` for the duration (**503 if indexing is in progress**), released in `finally`. Every mutation is audit-logged via new `core.utils.log_vault_write` (see CLAUDE.md *Logging, Deletion & Vault-Write Auditing*) and journalled for rollback/resume.

### Config / UI / tests
- New keys `refactor_archive_dir` (abs path or `""` = `BASE_DIR/refactor/archive/<vault_key>/`, must resolve outside the vault) and `refactor_thumb_max_side` (96–1024, default 384), validated/clamped in `api/routes/config.py`. New constants `REFACTOR_THUMBS_DIRNAME` / `DEFAULT_REFACTOR_THUMB_MAX_SIDE`.
- `static/js/refactor.js` gains per-note approve checkboxes + an "Apply approved" confirm modal, a per-image "Archive…" inline confirm, and a "Restore…" modal reading the manifest (`ui.js`+`api.js`-only, no `innerHTML` for user strings). The tab blurb now warns that Apply/Archive write to the vault.
- `test_refactor.py`: 22 new hermetic tests (callout apply + snapshot, stale/WYSIWYG guards, shared-image refusal, archive move + thumbnail + embed swap + `_thumbs` exclusion, restore of both, op-lock 503, scope-lock validator, thumbnail PNG, config validators, plus the self-audit cases below). Full suite green.

### Fixed (Phase 2 self-audit)
- **Restore is now atomic-or-nothing.** Reverting an archive of a note that was *edited after* archiving previously half-reverted — it could delete the thumbnail while the note still embedded it (broken embed) or clobber the user's later edits. `revert_archive_image` now runs all conflict checks up front and refuses the whole op (touching nothing) unless the note is still exactly what we wrote or already back at its pre-archive form.
- **Restore won't clobber a re-created file.** If a *different* file now occupies the archived image's original path, restore refuses rather than overwriting it (digest-compared; an identical file is treated as already-restored and the revert completes idempotently).
- **The move-safety reference check is now maximally conservative.** It previously skipped notes in the user's `vault_exclude_dirs` / `.trash`, so an image still embedded by an excluded-folder note could be wrongly judged "not shared" and moved — breaking that embed. The walk now scans every `.md` except `.git`/`.obsidian` (which structurally hold no user embeds).
- **Archive re-verifies the note immediately before its destructive write** (closing the window where the note changes during the thumbnail/copy work); on mismatch it rolls back the thumbnail + archive copy + the just-appended manifest op, leaving the vault untouched. The original-file `unlink` is now `assert_under`-guarded.
- **Restore persists the manifest per-op** (not once after the loop), so a crash mid-batch never leaves on-disk reverts unrecorded — matching `apply_notes`.

## 2026-06-22 (Note Refactor — read-only analyzer + central hub)

A new **Note Refactor** tab and `refactor/` package. It is **read-only with respect to the vault** (Phase 1 + Phase 1.5): it resolves an Obsidian sub-folder's image embeds, reuses the indexer's on-disk description cache (zero vision calls by default), flags broken links / frontmatter smells, and reports advisory cross-note dose discrepancies. The only persisted artifacts are under `BASE_DIR/obsidian_cache/` — never the vault. Apply/archive (the first real vault writes) remain Phase 2.

### Added — Phase 1 (read-only analyzer)
- `refactor/` package (`resolver` / `cache` / `hints` / `extract` / `hygiene` / `discrepancy` / `plan` / `result`) and `api/routes/refactor.py`: `POST /api/refactor/plan` (SSE: `info`/`error` + a `{"note": …}` frame per analyzed note + a terminal `{"refactor": …}` summary), `POST /api/refactor/extract-image` (the **only** vision-calling path — one user-chosen image at a time; serialized via `_VISION_LOCK`; caches `obsidian_cache/<sha256>.{table,redescribe}.txt`, never the indexer's base `<sha256>.txt`), and `GET /api/refactor/image` (read-only bytes, fetched as a blob so the X-Requested-With/origin check stays intact). `static/js/refactor.js` tab + `refactor_scope_subdir` / `refactor_extract_model` / `refactor_table_double_read` config keys. Hermetic `test_refactor.py`.

### Added — Phase 1.5 (central hub)
- **Folder picker.** `POST /api/refactor/native-pick-folder` → vault-relative scope via `_abs_to_scope` (rejects the vault root + anything outside the vault, like `_resolve_scope`); a `Browse…` button beside the scope input. Manual typing still works.
- **Single-note detail pane.** A sidebar/detail master-detail UI renders ORIGINAL vs PROPOSED markdown (vendored `marked` + `sanitiseHtml`, `textContent` fallback like `vault.js::_renderAnswer`) with a Rendered/Diff toggle. The `{"note"}` frame now carries the `original`/`proposed` bodies (~2× frame size, fine for the scoped sub-folder).
- **Classify + handwritten signal.** `extract-image` gains `mode="classify"` → one cheap vision pass labelling printed-table｜figure-diagram｜handwritten｜photo｜other (cached `<sha256>.classify.txt`), surfaced as a first-class "handwritten — can't OCR" badge.
- **Sticky ignore-list.** `GET`/`POST /api/refactor/ignore` over a rel-path-keyed, per-vault JSON sidecar at `obsidian_cache/refactor/<vault_key>/ignore_list.json` (**never the vault**). Ignored images grey out, drop from the not-extracted/likely-table counts, and get no inlined callout in the plan. The read-modify-write is serialized with a module lock so concurrent toggles under waitress's worker threads can't lose updates.

### Fixed (Phase 1.5 self-audit)
- `extract.classify` no longer caches a bogus `other` label when the model returns an empty (non-exception) reply — it surfaces an error and writes nothing, so a later plan run can't show a confident-but-wrong label (mirrors `redescribe` / the `NO_TABLE` skip).
- The detail pane disconnects the thumbnail `IntersectionObserver` on re-render so the previous note's detached `<img>` nodes aren't retained until the next run (cached blob URLs survive and are reused).

## 2026-06-22 (Indexing-pipeline audit fixes: LanceDB compaction, scanned-PDF coverage, embed-at-chat, lifecycle)

Five batches from a read-only audit of the vault indexing pipeline, landed before a full `embeddinggemma:300m` reindex on the `lancedb` backend.

### Fixed — LanceDB `_versions` O(n²) bloat (reindex blocker)
- The streaming indexer inserts one chunk per transaction; on LanceDB each insert is a new single-row fragment **and** a new version manifest that re-lists every fragment, so `_versions/` grew ~O(n²) (66k inserts measured at **209 GB** over <1 GB of real vectors — a full reindex would fill the disk). New `rag/lancedb_store.py::compact_lancedb_vector_store` runs `Table.optimize(cleanup_older_than=0)` on the live table (best-effort, never raises). It is called (a) from `_persist_index_checkpoint` (lancedb branch, under the locks both callers already hold) and (b) **every `_LANCEDB_COMPACT_EVERY` (2000) inserts** in `_index_documents_streaming`, under `_index_mutation_lock` only — independent of the ≥10-min JSON-checkpoint gate, which is too coarse to bound the interim spike on a fast embedder. `scripts/compact_lancedb.py` is a one-off reclaim for an already-bloated table (app closed). (Implements an earlier internal fix plan, plus the insert-count cadence + reclaim script that plan had deferred.)

### Fixed — scanned PDFs no longer silently lose ~90% of their content
- `_perform_ocr_fallback` capped OCR at `_OCR_MAX_PAGES` (100) **per call**, but the vault loader extracts in 1000-page ranges, so a scanned ≥100-page range only ever indexed its first 100 pages — and cached that partial as complete, so it never self-healed. `extract_structured_from_pdf` now takes `ocr_max_pages` (the vault loader passes the full range size = 1000) and `page_done_cb` (per-page op-lock heartbeat so a long scan can't expire the TTL); `ArticleSections` gained a `truncated` flag. The vault loader **warns and does not cache** a truncated range so it retries next run. The interactive upload path keeps the 100-page default (bounded by its own extraction timeout). `pdf_extractor.py`, `rag/vault.py`.

### Fixed — chat no longer mixes embedding spaces on a model switch
- Retrieval embedded the query with whatever `embed` config said, even when it differed from the model the index was built with — fusing two vector spaces and silently wrecking results (only an advisory UI banner guarded it). `stream_chat`/`retrieve` now resolve the **index's own** embed model from `obsidian_meta.json` (`_effective_embed_name`), warn once, and retrieve with it. The indexer already rebuilds on an embed change; this keeps chat correct until the user reindexes. `rag/vault.py`.

### Fixed — index lifecycle / robustness
- **Cancel-then-reindex race:** `cancel_indexing` force-releases the op lock *before* its background thread finishes the final persist, so a reindex started immediately after could archive/rebuild the index dir concurrently with the cancelled run's checkpoint. `POST /api/obsidian/index` now `wait_for_indexing(timeout=30)` first (mirrors the reset path). `api/routes/vault.py`.
- **No-recorded-embed index:** a `has_vector_data` index whose meta lacks an `embed` model is now rebuilt rather than silently extended (it can't be proven compatible). `rag/vault.py`.
- **Delete-before-insert gap:** a changed chunk whose re-insert fails *after* its old copy was deleted is now tracked and reported (`reinsert_failed`) instead of being hidden in the aggregate `failed` count; it self-heals on the next run.

### Tests
- `test_lancedb_migration.py`: compaction merges per-insert fragments + preserves rows, the streaming loop compacts at the insert cadence, `compact_lancedb_vector_store` is a safe no-op off lancedb, and a no-recorded-embed index rebuilds. `test_vault_regressions.py::TestPdfRangeSplitting`: truncated range yielded-but-not-cached, full-range `ocr_max_pages` passed (mock updated to mirror `ArticleSections.truncated`). `test_vault_regressions.py::TestChatEmbedMismatchGuard`. `smoke_test.py::test_30b` (index route 503 when a prior run is unfinished).

## 2026-06-21 (MD secondary cap: stop silently truncating long markdown sections)

### Fixed — long single-heading markdown sections are no longer truncated at embed time
- **`MarkdownNodeParser` splits `.md` only at heading boundaries**, with no size ceiling, so a long single-heading section produced one oversized chunk that exceeded the embedding model's token limit (nomic-embed-text / EmbeddingGemma ~2048) and was **silently truncated** — its tail never embedded and was unretrievable. A read-only scan of the live vault found **52 oversized sections across 39 notes**, 9 already exceeding 2048 tokens today (e.g. a 4,581-token section, >50% clipped). `_chunk_raw_documents` now runs a **conditional secondary `SentenceSplitter` pass** on the MD branch that sub-splits **only** sections over `MD_MAX_CHUNK_TOKENS` (1024, `core/constants.py`). `rag/vault.py`.
- **Oversize is detected by splitter output cardinality**, and under-cap sections pass through as the **original node object** → identical `i`+text → byte-identical chunk id ⇒ **zero re-embed churn** for the ~97% of notes with no oversized section. A note *with* an oversized section re-chunks that section onward (the `i` shift); the stale-doc_id sweep self-heals it — **no `OBSIDIAN_INDEX_VERSION` bump**. A provably-safe byte-length fast path skips tokenizing tiny sections (a cl100k token is always ≥ 1 UTF-8 byte). The secondary split is `try/except`-guarded so a pathological section degrades to un-split rather than aborting the (generator-based) run.
- Attachment extraction now runs over the **final** post-split node list (each sub-chunk gets only its own links), with the `attachments` list attached **after** the split so it can't inflate the split decision (the `_tags.md` metadata-budget case). PDF chunking is untouched.

### Notes
- `MD_MAX_CHUNK_TOKENS` is a **pinned constant**, not a config knob: it changes chunk ids, so editing it is a reindex — the same contract as the 512-token PDF chunk size. 1024 leaves ~2x headroom under the 2048 hard limit for tiktoken-vs-SentencePiece divergence. Best landed alongside an embedding-model switch so the (small) reindex is paid once.

### Tests
- `test_vault_regressions.py::TestMdSecondaryCap`: under-cap byte-identity (legacy hash), oversized split ≤ cap with unique doc_ids + propagated `header_path`, tail-only attachments, multibyte-under-cap byte-identity. `test_chunker_params_pinned` extended to pin the secondary splitter line.

## 2026-06-21 (Vision/OCR call bounds: a stuck image can no longer stall indexing)

### Fixed — indexing-time vision/OCR calls are now always bounded
- **A runaway or stuck local vision model no longer freezes a multi-hour indexing run.** The image-description and scanned-PDF OCR calls (`services/vision.py`) had no timeout, no retry cap, and no token cap, so they inherited the OpenAI/LM Studio SDK default of a 600 s timeout × 2 retries = **up to 30 minutes per stuck image**, blocking the whole streaming indexer (observed against `lm_studio` + `gemma-4-e4b` generating unbounded tokens on a single image). The transports now take keyword-only `timeout`/`max_tokens`: LM Studio passes `timeout` + `max_retries=0` to `openai.OpenAI(...)` and `max_tokens` to `create(...)`; Ollama uses the timed `_ollama_client(OLLAMA_HOST, timeout)` + `options={"num_predict": max_tokens}`. `_chat_lm_studio_image` never forwards `timeout=None` (the SDK can read that as "no timeout"). `services/vision.py`, `core/providers/ollama.py`.
- **The description path pre-emptively downscales oversized images** (`_fit_base64_image_to_max_side`, longest side > `VISION_IMAGE_MAX_SIDE` = 1568 px) to kill the giant-image prefill-stall mode. Best-effort PNG re-encode that returns the original on any failure (undecodable image — HEIC without `pillow-heif`, which is not installed — or non-image input). Invisible to the description cache (keyed on original bytes). OCR is deliberately **not** pre-downscaled (legibility); its existing context-overflow downscale-retry is unchanged and still caches on the original `base64_png`.

### Added — Settings-window knobs for the vision/OCR bounds
- `vision_timeout_s` (5-600, default 120; **always on — no "0 = off"** unlike `local_request_timeout_s`), `vision_max_tokens` (64-8192, default 1536), `ocr_max_tokens` (64-8192, default 4096). Persisted defaults in `core/config.py`, validated/clamped in `api/routes/config.py::_CONFIG_VALIDATORS`, read **per call** via `_cfg_bounded_int` (lazy `load_config`) so a change applies without restart, with a hard `DEFAULT_*` fallback on missing/≤0/garbage and a clamp to the validator range for a hand-edited out-of-range value. UI controls live in the **LLM Settings → Global · OCR & Vision** block and are owned by `static/js/settings.js`. `core/constants.py` adds `DEFAULT_VISION_TIMEOUT_S`/`DEFAULT_VISION_MAX_TOKENS`/`DEFAULT_OCR_MAX_TOKENS`/`VISION_MAX_RETRIES`/`VISION_IMAGE_MAX_SIDE`.

### Tests
- `test_concurrency.py::TestVisionCallBounds`: downscale shrinks oversized / no-ops small / survives junk; `describe_image` downscales and passes the bounded `timeout`/`max_tokens`; OCR passes `ocr_max_tokens` (not `vision_max_tokens`); the LM Studio transport sets `max_retries=0`, forwards a real `timeout`, caps `max_tokens`, and omits `timeout`/`max_tokens` when unset; `_cfg_bounded_int` falls back for unset/0/garbage and clamps a positive out-of-range value. `smoke_test.py::test_03c` covers the three new keys as defaults + clamp.

## 2026-06-20 (Production-readiness pass: provider-API correctness, agent timeouts, live model listing)

A code-review + audit response. All changes verified against the live provider docs (platform.claude.com, ai.google.dev, platform.openai.com) where an HTTP contract was involved.

### Fixed — online provider API correctness
- **OpenAI o-series reasoning models now send the right parameters.** `o1` / `o1-mini` / `o1-preview` / `o3-mini` (all in the curated list) reject `temperature`/`top_p` ("Only the default (1) value is supported") and require `max_completion_tokens` instead of the deprecated `max_tokens` — so every chat call against them used to 400. `OpenAIProvider._common_params` now detects the o-series by family prefix (`_is_reasoning_model`: `o` + digit, so future `o5`/`o6` are covered while `gpt-4o` is not) and emits `max_completion_tokens` + omits the sampling params; `gpt-*` keep the legacy shape (also safer for OpenAI-compatible `base_url` endpoints). `core/llm/adapters/openai.py`.
- **Anthropic temperature is clamped to its documented 1.0 ceiling.** Anthropic caps `temperature` at 1.0 while OpenAI/Gemini allow 2.0 and the vault-chat range is 0-2; a temperature in (1.0, 2.0] would 400 against Anthropic only. `AnthropicProvider._build_payload` now clamps to `min(temp, 1.0)`. `core/llm/adapters/anthropic.py`.

### Added — live model discovery (curated fallback)
- **`/api/models` for online providers now merges `CURATED_MODELS` with a live fetch** from each provider's models endpoint (OpenAI `client.models.list()`, Anthropic `GET /v1/models`, Gemini `GET /v1beta/models` filtered to `generateContent`), so newly-released models (e.g. a just-shipped Claude tier) appear without a code edit and retired ids simply never get appended. Curated stays first and authoritative for pricing + default selection. `core/llm/model_listing.py` (`merged_models`: short-TTL-cached, key-gated, never-raises, 20 s negative-TTL on failure so a not-yet-set key recovers fast) + per-adapter `_fetch_live_models`. Degrades to curated-only without a key / offline / on error, so no-key callers and the hermetic test suite are unchanged.

### Fixed — agent loop resilience & correctness
- **Agent reasoning calls are now bounded by the turn's wall-clock deadline.** Each iteration caps the per-call timeout to the remaining `deadline_monotonic_s`; the local tool-call path honours it via `_effective_local_timeout` (the tighter of `request.timeout_s` and `local_request_timeout_s`, **ceil-quantized to whole seconds** so the `OllamaProvider._client` cache — keyed by `(host, timeout)` — stays bounded instead of leaking one httpx pool per distinct float). `OllamaProvider._client(timeout=…)` gained an explicit override. This stops a wedged local backend from keeping the SSE worker thread alive past the deadline. `core/agent/loop.py`, `core/llm/adapters/local.py`, `core/providers/ollama.py`.
- **Agent mode honours the per-request `temperature`** and no longer coerces a configured `0.0` up to `0.3` (the old `cfg.get(...) or 0.3` treated falsy 0.0 as unset). `run_agent_loop(temperature=…)` threaded from the route. `core/agent/loop.py`, `api/routes/vault.py`.

### Changed — hardening
- **Secret redaction** adds Google OAuth (`ya29.…`) and generic `Authorization:` token patterns. `core/llm/redact.py`.
- **`online_max_retries` is clamped to `[0, 10]` in the factory** (mirrors the `/api/config` validator) so a hand-edited `config.json` can't make the retry loop attempt hundreds of round-trips. `core/llm/factory.py`.

### Added — answer-quality eval is now runnable hermetically
- Extracted `run_eval.run_pairs(manager, pairs, …)` so the scoring pipeline (previously only reachable behind `RUN_LIVE_EVAL=1` and never executed) is driven in CI by `tests/eval/test_eval_pipeline.py` with a fake manager — proving a grounded answer passes and a hallucinated one fails. Added two unanswerable hallucination-tripwire pairs to `golden_qa.json`. `tests/eval/`.

### Docs
- README states the **read-only vault** guarantee prominently (the app never writes to your Obsidian vault) and adds a screenshots scaffold (`docs/screenshots/`, whitelisted in `.gitignore`). `core/llm/CLAUDE.md` documents the live-listing merge, the o-series/Anthropic parameter contracts, and the agent-tool-path timeout exception; root `CLAUDE.md` updates the `/api/models` description.

### Tests
- New hermetic tests: live-merge / curated-fallback / failure-recovery / success-empty-cached (model listing), o-series + Anthropic parameter contracts, ollama client-cache quantization bound, agent temperature/timeout, and the eval pipeline. Full `test_llm` / `test_agent` / `test_concurrency` / `test_validators` / `test_prompts` / `test_deck` / `tests/eval` suites green (the only `test_llm` failures observed are a pre-existing, intermittent numpy "cannot load module more than once" import flake, identical with and without these changes).

## 2026-06-20 (Wikilink graph expansion — query-time, no reindex)

### Added — vault retrieval
- **Wikilink graph expansion** for vault chat: an opt-in, **reindex-free / re-embed-free** retrieval layer that widens the retrieved result set with chunks from notes linked to/from your top hits (outbound ∪ backlinks) before the rerank stage. Rerank-gated, so irrelevant neighbours drop out and the final answer size is unchanged.
  - **Graph builder** (`rag/vault.py`): `_WikilinkGraph` + `_get_wikilink_index()` build a note→note adjacency lazily from the docstore (regex over `.md` node text, Obsidian shortest-path resolution against the in-memory indexed-source set — no filesystem walk), cached by docstore size and invalidated by `_invalidate_retrieval_caches` like BM25 (no on-disk sidecar). Optionally warmed silently by `prewarm`.
  - **Expansion retriever** (`rag/engine.py`): `_WikilinkExpansionRetriever` wraps the finalized retriever in `_build_retrieval_pipeline` **before** the postprocessors — one insertion point covering the local, online, and agent-search paths. Neighbour chunks are fetched from the docstore at a decayed seed score; bounded by neighbour-note and node caps.
  - **Config** (`core/config.py`, `api/routes/config.py`, `api/routes/vault.py`): `vault_wikilink_expansion` (default `False`, body-overridable via `wikilink_expansion`) plus config-only caps `vault_wikilink_neighbor_cap` (1-100/10), `vault_wikilink_node_cap` (1-200/24), `vault_wikilink_score_decay` (0.0-1.0/0.5), all in the `_validate_llm_config_keys` clamp map. Applies to single-shot RAG (and the agent's RAG fallback) only — not the agent's active `vault.search`, matching `mmr_enabled`/`query_expansion`.
  - **UI** (`templates/index.html`, `static/js/vault.js`): a "Wikilink expansion" checkbox (`#vault-wikilink-enabled`) in the Vault Chat fine-tuning controls, owned by `vault.js` like the other live retrieval toggles (live per-Send body field + debounced config persistence).
  - **Tests**: `TestWikilinkGraph` (resolution/both-directions/limitations), `TestWikilinkExpansionRetriever` (caps/decay/dedup/seed-skip/pipeline-wrap-only-when-enabled), `_resolve_chat_params` resolution, and smoke config-clamp coverage. ~30 new hermetic tests; full vault/engine/config suite green.

## 2026-06-19 (Prompt audit + vault image descriptions + LanceDB metadata tolerance)

### Changed — prompt audit (all query/generation-time; **no reindex**)
- **Single-paper system prompt.** Dropped the "Each section: 3-6 sentences" clause that contradicted the per-template length budgets (CONCISE 1-2, DETAILED 1-3); dropped the "expert research assistant" persona framing while keeping the medical/biomedical domain cue; phrased affirmatively (small local models follow positive instructions more reliably). The detailed preset gains a lead-bias mitigation line ("draw on the whole article — including Methods and Results — not only the abstract/introduction"). `core/constants.py`.
- **Focus question is now actionable.** When supplied it carries an explicit directive ("Prioritise information that addresses this question; if the document does not, state that explicitly") instead of being injected inert, and is `.strip()`-guarded so a whitespace-only value adds nothing. `core/llm/prompt.py`.
- **Report-type prompts (7 built-ins).** Replaced the "You are a researcher specializing in X" identity opener with a "When summarising X, …" scoping clause; the "pay special attention to / focus on …" focus directives (the high-value part) are preserved verbatim. Only built-in defaults change — user-saved/overridden report types in `report_types.json` are untouched. `core/constants.py`.
- **Vault RAG citation instruction is consistent across all four answer modes.** strict/balanced/exploratory/concise now all end with "cite the source filename in brackets, e.g. `[note.md]`" (previously three said "cite source filenames when available" and only concise used brackets). `rag/engine.py`.
- **Agent preamble** gains an efficiency nudge ("prefer one or two focused searches") and a one-line worked example, targeting the small-local-model failure mode (tool-call looping / malformed calls that trip the RAG fallback). The pre-existing stop condition and untrusted-tool-output guard are retained. `core/agent/loop.py`.
- **Deckgen outline prompt** gains a one-line JSON shape example; `outline.parse_outline` (balanced-bracket extraction + heading-list fallback) remains authoritative. `deckgen/prompts.py`.

### Changed — vault image indexing (`b11dc65`)
- **Vault image vision prompt** (`VisionManager.describe_image`) now asks for a short description of what the image depicts **plus** a transcription of any visible text/labels/data, instead of the pure "extract all text" OCR prompt — which returned nothing for text-light figures, so those images embedded empty and were dropped. The scanned-PDF path (`GLMOCRManager.extract_page_text`) stays pure-OCR. The description cache is keyed by image bytes, not prompt text, so clear `obsidian_cache/.../image_cache/` to regenerate already-cached descriptions.
- **Obsidian-style attachment resolution.** Bare wikilinks (`![[image.png]]`) now resolve via a vault-wide basename index built during the single `rglob` walk, so an image in a central attachments folder resolves even when the link omits its path; ambiguous basenames break by shortest-path proximity to the linking note. The `attachments` metadata stays byte-identical, so no re-embedding is forced.

### Fixed — LanceDB backend (`8af0b0b`, `c14f565`)
- **Heterogeneous node metadata no longer aborts a lancedb build.** LanceDB freezes the metadata struct schema at table creation (MD-first → `{file_path, source, extension, header_path, attachments}`); later inserts adding a field — `page_start`/`page_end` on large-PDF range chunks, `is_image` on vault images — failed with "field does not exist in table schema", silently breaking large-textbook indexing (each run hit the consecutive-failure breaker at the first such chunk). `NormalizingLanceDBVectorStore.add()` now projects each node's metadata onto the live table's columns (`_allowed_metadata_keys()` reads the struct schema; `_LANCE_FLAT_METADATA_KEYS` is the fresh-table fallback). Dropping is lossless for the read path (those keys are write-only at query time; the full node lives in the docstore + `_node_content`). Vector-row only — `doc_id`/`doc.hash`/docstore untouched, so already-indexed MD/PDF chunks skip by hash with no re-embedding; only the previously-failing large PDFs + vault images embed (first-time) on the next run. `rag/lancedb_store.py`.

### Added — prompt regression net
- `test_prompts.py` (repo root, model-free): 20 invariants pinning the grounding/citation/placeholder/length-cap contracts of the prompts above, so an accidental edit surfaces as a failing assertion.
- `tests/eval/`: a self-contained fixture vault + golden Q&A pairs + a pure scoring layer (`scoring.py`, with `must_not_contain` using word-boundary matching to avoid substring false positives) plus an opt-in live runner (`run_eval.py`, gated by `RUN_LIVE_EVAL=1`) for before/after answer-quality comparison. The data + scoring half is verified hermetically by `tests/eval/test_scoring.py`.

### Tests
- New: 20 in `test_prompts.py`, 8 in `tests/eval/test_scoring.py`; lancedb metadata-projection regression in `test_lancedb_migration.py` (`8af0b0b`). Prompt-relevant + vault suites green with no regressions (`test_llm`, `test_agent`, `test_deck`, `test_validators`, `smoke_test`, `deckgen/tests`: 290 passed/1 skipped; `test_vault_regressions`, `test_concurrency`: 190 passed).

## 2026-06-18 (Library Audit: precompute unmapped-PDF annotations during the scan)

Fixes the "Unread PDFs" / "Read PDFs Missing Zotero" tabs hanging on "Loading…": both reports iterate `inv.bridge.unmapped_pdfs` and previously read every unmapped PDF's annotations **lazily on the report GET** (thousands of synchronous `pikepdf` opens on the request thread, recomputed on every tab visit, with no progress and no cancel). The work now happens once, during the scan, in parallel.

### Changed

- **`audit/engine/inventory.py`** — `build_inventory` now precomputes annotation counts for the bridge's *unmapped* PDFs into a new `Inventory.unmapped_annotations: dict[Path, AnnotationsResult]`, gated by the existing `count_annotations` flag and skipped on cancel. Reads run through `_read_annotations_parallel` (a `ThreadPoolExecutor` capped at `_ANNOTATION_MAX_WORKERS = min(8, cpu_count)`; pikepdf/qpdf releases the GIL so threads scale across cores). A new optional `progress_fn` emits `Reading annotations for N unmapped PDFs…` plus a `…done/total` tick every 500 files.
- **`audit/manager.py`** — passes `progress_fn=self._emit` so the annotation phase is visible in the `/api/audit/status` feed (and the Cancel button now interrupts it between files).
- **`api/routes/audit.py`** — `unread_unzoterod` / `read_unzoterod` reports are served with `annotations=inv.unmapped_annotations`; the report `find()` functions already fall back to a lazy `read_annotations` for any path missing from the cache, so a `count_annotations=False` run or a cancelled scan degrades to the old per-request read rather than breaking. No report dataclass, serializer, API-payload, or JS change.

### Tests

- `test_audit.py::TestUnmappedAnnotationPrecompute` — `build_inventory` populates `unmapped_annotations` when `count_annotations=True` and leaves it empty when `False`.
- `test_audit.py::TestUnzoterodReportsUseCache` — both reports serve from the cache without touching disk (patched `read_annotations` raises if called).
- `test_audit.py::TestReadAnnotationsParallel` — the parallel reader covers full read, empty input, and immediate cancel (no hang).

## 2026-06-17 (Unified LLM Settings window + configurable timeouts)

Consolidates every LLM/RAG **parameter** (not prompts/instructions) into one **LLM Settings** modal grouped by function (Global / Vault Chat / Single Paper / Deck), surfaces a whole tier of knobs that previously had no UI (timeout/retry/token plumbing, fallback policy, reranker device, vector backend, prewarm), and makes the agent wall-clock cap and the local-provider HTTP timeout user-configurable. The inline per-tab parameter panels are removed; content/instruction inputs (system prompts, topic, audience, language, document type, template) stay on their tabs.

### Added

- **`static/js/settings.js`** — owns the `#settings-modal` (imports only `api.js`). Populates the settings-owned controls from `/api/config` on init and debounce-saves changes. Deliberately does **not** touch the vault knobs / OCR-Vision selects / provider-model selectors that also live in the modal — those are still operated **by element id** by their original owners (`vault.js`, `config.js`); the markup was relocated, not the logic.
- **New config keys** (`core/config.py`): per-function generation defaults `paper_*` (`paper_temperature` / `paper_num_ctx` / `paper_max_tokens` / `paper_top_p` / `paper_repeat_penalty`) and `deck_*` (`deck_temperature` / `deck_max_sections` / `deck_agent_max_iterations`); `agent_wall_clock_s` (default 300); `local_request_timeout_s` (default 0 = leave each path's SDK default).
- **`api/routes/config.py::_validate_llm_config_keys`** — a per-key coerce/clamp map (reusing `api/validators.py`) over every numeric/enum/bool LLM knob the Settings window writes. Out-of-range/malformed values are **dropped** (prior value survives), so a crafted body or a hand-edited `config.json` round-tripped through the UI cannot store a pathological value. This is the first validation those previously-UI-less keys ever had.
- **Configurable agent wall-clock cap** (`agent_wall_clock_s`) and **local request timeout** (`local_request_timeout_s`), both exposed in the Settings window's Global group. The local timeout threads through `core.providers.base.local_request_timeout()` into the Ollama client + `get_llm`, and the LM Studio OpenAI client + LLM (chat/generation only — embeddings are intentionally excluded; see Hardening).

### Changed

- **`templates/index.html`** — inline LLM-parameter accordions removed from the Obsidian, Single Paper and Deck tabs; the sidebar OCR/Vision section relocated into the modal. All controls keep their original ids, so `vault.js` / `summarizer.js` / `deck.js` / `config.js` operate them unchanged. Sidebar keeps the provider / model / embedding selectors and a new **⚙ LLM Settings** button.
- **`api/routes/paper.py` / `deck.py`** — generation knobs now resolve **request body → persisted `paper_*` / `deck_*` config → hard-coded default**, so the removed panels lose nothing (the Settings window writes the config the routes fall back to).
- **Timeout chain** (`api/routes/vault.py`, `deck.py`, `static/js/vault.js`) — the agent deadline, the SSE consumer stall guard, and the vault.js fetch-abort all derive from `agent_wall_clock_s` instead of three hard-coded `300`s, so raising the cap propagates outward. The consumer/abort are floored at `_SINGLE_SHOT_FLOOR_S = 300` so lowering the cap to bound agent turns can't starve a slow single-shot first token; the vault.js abort is computed **live at send time** so a Settings change applies without a reload.
- **`core/providers/ollama.py`** — generation routes through a `(host, timeout)`-cached `ollama.Client` (was the module-level singleton `ollama.chat`) so the configured timeout reaches httpx without churning a new connection pool per call.

### Hardening (post-implementation self-review)

- **Live frontend abort.** `_CHAT_TIMEOUT_MS` is no longer cached at init (which would ignore a mid-session cap change); `_chatAbortMs()` reads the live control at send time.
- **Single-shot stall floor.** The shared SSE consumer never drops below 300 s + margin even when the agent cap is lowered, protecting the non-agent RAG path.
- **Defensive cap parse.** `agent_wall_clock_s` is read with `coerce_int_in_range`, so a hand-edited non-numeric/negative value can't crash the route or yield an instant-past deadline.
- **`fallback_on` consistency.** An all-invalid non-empty list now drops (preserve prior) like every other validated key; an explicit `[]` stays a valid "disable all".
- **Embedding scope documented.** `local_request_timeout_s` covers chat/generation only — `OllamaEmbedding` has no timeout hook and bounding indexing batches would cause spurious failures rather than recovery.
- **Tests.** `smoke_test.py::test_03c` covers the new defaults + clamp/drop; `test_vault_regressions.py::test_ollama_client_threads_timeout_and_is_cached` covers the positive timeout path + the client cache.

## 2026-06-17 (Deck Generator — in-app deckgen window + custom Beamer templates)

Promotes the standalone `deckgen` CLI into a fourth app window (**Deck Generator**, beside Single Paper / Obsidian Agent / Library Audit) and teaches it to target the user's own LaTeX suite — reusing a custom Beamer template's preamble + macros (`\citefoot`, `\commonlogo`) and bibliography instead of a hard-coded generic preamble. Emit-only: it scaffolds a compile-ready `<slug>/` folder you build with `make`.

### Added

- **`deckgen/template.py`** — template awareness (all pure stdlib, unit-testable without a server): `split_template` (preamble / opening title+outline scaffold / closing tail; drops the template's example sections), `scan_macros` (follows local `\usepackage{...}` into sibling `.sty` files to surface custom macros; ignores `@`-internal and commented-out lines), `resolve_bib` (parses `\addbibresource{...}` → `key → author/year/title`), `relevant_bib_keys` / `bib_candidates_block` (bounded per-section candidate set), `find_suite_root`, `strip_comments`.
- **`deckgen/scaffold.py`** — `scaffold_deck` writes `<out_dir>/<slug>/<slug>.tex` + a 2-line `Makefile` (`include ../common/latex-build.mk`) via its own atomic writer; rejects unsafe slugs and refuses to clobber an existing deck without `overwrite`.
- **`deckgen/inprocess.py`** — `InProcessChatRunner`, a `requests`-free, `ChatEKLDClient`-compatible runner that drives `core.agent.run_agent_loop` over `rag.vault.obsidian_manager` directly (mirrors `vault.py::_run_agent`) so the in-app path never loops back over HTTP.
- **`deckgen/result.py`** — `ChatResult` extracted to a no-third-party-imports module shared by the HTTP client and the in-process runner.
- **`api/routes/deck.py`** (`deck_bp`) — `/api/deck/load-template`, `/api/deck/generate` (SSE), `/api/deck/native-pick-file`, `/api/deck/native-pick-folder`. Path validators reject traversal/system roots; generate reuses the `obsidian/chat` SSE contract plus a terminal `{"deck": {...}}` frame.
- **Deck Generator tab** — `templates/index.html` panel + `static/js/deck.js` (imports only `ui.js`+`api.js`; DOM built with `textContent`/`createElement`). Includes an **editable preamble box** (load a template, tweak it before generating — the macro/bib scan runs on the edited text) and a **dedicated instructions textarea**.
- **CLI flags** `--template` / `--out-dir` / `--citations` / `--no-citations` on `python -m deckgen` for the same house-style template mode.

### Changed

- **`deckgen/assemble.py`** — `assemble_with_template` injects sanitized sections between the template's title/outline scaffold and `\end{document}`; `validate` gains a `generated_tex` span (so the trusted preamble isn't flagged) and a `known_bib_keys` guard that warns about `\citefoot`/`\cite` keys absent from the `.bib` (an invented citation). `extract_cite_keys` added.
- **`deckgen/prompts.py` / `sections.py`** — per-section prompt now advertises the template's custom macros and a relevance-bounded `\citefoot` candidate list (`cite_mode="bib"`), staying within the 4000-char system-prompt cap; plain-prose `(source: note.md)` remains the fallback.
- **`deckgen/client.py` / `sections.py`** decoupled from `requests` at import time (the core no longer pulls `requests`), so the app can import the deckgen core without that dependency.
- **`app.py`** registers `deck_bp`.

### Hardening (post-implementation self-review)

- **Comment-aware template parsing.** `split_template` searches a length-preserving comment-masked copy (`mask_comments`) for the `\begin{document}` / `\section` / `\appendix` / references boundaries, then slices the original — so a *fully commented-out* references frame (as in the house `presentation.tex`) is no longer pulled into the closing tail. Previously that stripped the `%` off `\begin{frame}` while leaving `% \end{frame}` commented → an unclosed frame → a **non-compiling deck** for the exact template a user would pick.
- **`validate` is comment-blind no more.** It strips comments before counting frames / scanning macros, so a commented `% \end{frame}` no longer masks an unbalanced deck and a commented `\input` / `\cite` no longer raises a false warning.
- **Temperature override fixed.** `InProcessChatRunner` writes the per-turn override to `vault_chat_temperature` (the key the agent loop reads), not a no-op `temperature` key.
- **xparse arg-spec parsing.** `\NewDocumentCommand` specs are read with a brace-balanced reader (defaults like `O{red}` no longer inflate the displayed arity).
- **Path hardening.** `\usepackage`/`\addbibresource`/`\input` resolution rejects absolute paths (only the relative `../common/...` house style is followed). The SSE route sets `cancel` in a `finally` so a client disconnect can't leave the worker blocked on a full queue (thread leak); the frontend surfaces any non-200 instead of hanging on "Generating…".

### Tests

- `deckgen/tests/test_template.py` (29): split/scan/bib/scaffold/assemble + prompt wiring + the cite-key guard + the regression tests above (commented references frame, comment-aware validate, absolute-path rejection, xparse arity), all server-free. `test_deck.py` (7, hermetic, repo root): in-process runner accumulation + event passthrough + temperature-key routing, route path validators, and the 400/403 guards. Full suite green (474 passed, 1 skipped).

## 2026-06-16 (Perf/memory batch 4 — LanceDB binary vector store, opt-in)

Fourth batch of `PERF_MEMORY_PLAN.md` (Fix E). Replaces LlamaIndex's JSON `SimpleVectorStore` with an optional binary **LanceDB** backend that removes the dominant resident-RAM cost (embeddings as float32 on disk instead of Python `list[float]`) and most of the GIL-held cold-start parse. **Opt-in** — the default backend stays `simple`, so fresh builds are byte-identical until you choose otherwise; an existing vault migrates in place with **no re-embedding**.

### Added

- **`scripts/migrate_vector_store.py`** — one-time, offline migration. Streams `embedding_dict` from `default__vector_store.json` with `ijson` (~100 MB peak RSS on a multi-GB index), joins each vector to its `docstore.json` node by id, and bulk-loads LanceDB. The embedding model is never constructed → **no re-embed**. Archives the legacy JSON to `…json.bak`, records `"vector_backend": "lancedb"` in `obsidian_meta.json`, verifies row-count parity, and refuses to run while the app is open (`--yes` to skip the prompt). Idempotent.
- **`rag/lancedb_store.py`** — `NormalizingLanceDBVectorStore`, a self-contained parity layer so neither `rag/engine.py` nor the embed model need backend branches: it **unit-normalizes** embeddings on insert and the query vector on search (LanceDB defaults to L2; for unit vectors L2 ordering == cosine ordering, so ranking is bit-for-bit identical to `SimpleVectorStore`), and **JSON-stringifies** list/dict metadata (`attachments`) on a per-node copy because LanceDB — like Chroma — rejects non-scalar metadata; the docstore keeps the original list. Plus `lancedb_available`, `is_lancedb_store`, `lancedb_table_count`, `make_lancedb_vector_store`.
- **`vault_vector_backend` config knob** (`simple` | `lancedb`, default `simple`). Governs only **fresh** builds; an existing index's backend is authoritative from `obsidian_meta.json` (`vector_backend`; a missing key ⇒ legacy `simple`). Resolves back to `simple` if lancedb is not importable.

### Changed

- **`rag/vault.py` is backend-aware.** A single `_build_storage_context` / `_build_index_for_backend` helper routes all four index-build/load sites (fresh build, incremental load, lazy-load, prewarm); `store_nodes_override=True` on the lancedb path keeps the docstore populated so BM25 and the document-hash skip-check keep working. The checkpoint promotes only `docstore.json` / `index_store.json` for lancedb (the vector data is durable-on-insert in the binary `lancedb/` dir, not part of the temp-promote dance); `_validate_persisted_index_files` and `_index_dir_has_vector_data` verify the table instead of `default__vector_store.json` on that backend.
- **Crash-drift recovery.** Because the binary store is durable-on-insert while the docstore persists at Batch-1b's `max(500 chunks, 10 min)` cadence, a hard crash can leave the table *ahead* of the docstore. Two O(1) counts at incremental load detect this; only on a mismatch does that run switch to delete-before-insert (reusing the existing `delete_ref_doc` path) so resumed chunks replace orphan rows instead of duplicating. Fresh builds and the `simple` backend pay nothing.
- **MMR works on the binary backend.** External stores silently ignore `vector_store_query_mode="mmr"`, so `rag/engine.py` now applies MMR client-side (`_ClientSideMMRRetriever`) over an over-fetched candidate pool using the nodes' stored embeddings when the index is LanceDB-backed; `SimpleVectorStore` keeps LlamaIndex's native MMR path unchanged.
- **`requirements.txt`**: `lancedb>=0.21.1,<1.0`, `llama-index-vector-stores-lancedb>=0.4.1,<0.6`, `ijson>=3.3.0,<4.0` (verified: lancedb 0.33.0, integration 0.5.0, core 0.14.22 — the earlier draft pins `<0.3.0` / `lancedb<0.10` silently downgraded core to 0.11.x). `install_and_build.sh` collects the lancedb/lance/pyarrow native libraries.

### Fixed

- **Two latent test-isolation bugs** surfaced by the first tests that build a *real* `VectorStoreIndex` in-suite: `conftest.py` and `test_concurrency.py` both evicted `llama_index` from `sys.modules` between/within test files, creating a second class identity so `isinstance`/pydantic-dataclass checks (`resolve_embed_model`, `NodeWithScore`, retrieval events) failed across the boundary. Nothing stubs `llama_index`, so it is no longer evicted.

### Tests

- New `test_lancedb_migration.py` (12): store-layer normalization + metadata shim + `is_lancedb_store`, migration count-parity/`.bak`/meta/idempotency, **retrieval ranking parity** vs SimpleVectorStore, fresh lancedb build + **no-re-embed** on reindex, crash-drift delete-before-insert (and the duplicate it guards against), and client-side MMR relevance/diversity + engine routing. Full suite green.

## 2026-06-10 (Perf/memory batch 3 — reranker device knob, prewarm gate, thread pool)

Third batch of `PERF_MEMORY_PLAN.md` (fixes 1, D, G). New `vault_*` keys flow through the existing `/api/config` passthrough — no route or UI changes; both knobs take effect on the next launch. No reindex.

### Changed

- **`vault_reranker_device` config knob** (`auto` | `cpu` | `mps`, default `auto`). Corrected premise vs. the original plan: with no device argument, llama-index's `infer_torch_device()` already picks **MPS** on Apple Silicon — the reranker was never CPU-bound, so `auto` (= construct without a device argument) is byte-identical to the old behaviour and the knob's real value is the **`cpu` escape hatch** that returns the MPS allocator's ~150–400 MB to the unified pool on a pressured 16 GB machine. Robustness added alongside: the cache and sticky-failure keys fold in the device mode (changing the knob retries a failed load), non-CPU modes run a one-pair warm-up `predict` at load (Metal failures usually surface on the first forward pass, where the engine's postprocessor walk would otherwise error every chat), and any non-CPU failure retries on CPU for the session instead of disabling reranking.
- **`vault_prewarm_enabled` config knob** (default `True`). Gated inside `ObsidianVaultManager.prewarm()` (unit-testable, independent of the caller): when off, prewarm reports terminal `skipped` before any disk access; `vault.js` already treats that as banner-hidden/Send-enabled and the first chat lazy-loads along the existing path. Honest caveat: defers the multi-GB footprint, does not remove it.
- **Waitress pool `threads=16` → `32`** (`launch.py`). Slots are parked for the full duration of SSE generations (≤ 300 s) and upload extraction joins (≤ 600 s) while 1 Hz status polling continues — cheap insurance against pool exhaustion presenting as a frontend hang. The plan's earlier `channel_timeout` idea stays withdrawn: waitress' `maintenance()` only reaps channels with no in-flight request.

### Tests

- New `TestVaultRerankerDeviceKnob` (mode normalisation, auto-omits-device, cpu pass-through, MPS-failure → CPU fallback that does not stick, warm-up-failure → CPU retry, device-change resets sticky failure) and a prewarm-disabled gate test that proves the skip happens before any disk access.

## 2026-06-10 (Perf/memory batch 2 — BM25 sidecar, mmap reload)

Second batch of `PERF_MEMORY_PLAN.md` (Fix B). `rag/vault.py` + a `bm25s` pin; no reindex — the sidecar is a derived view of the docstore.

### Changed

- **The built BM25 retriever is persisted to `obsidian_storage/bm25_index/` and mmap-loaded on later launches.** Previously every process built BM25 from scratch on launch/first-chat — minutes of re-tokenising on a large vault — and the resulting retriever held a second full copy of all vault text (`BM25Retriever.__init__` copies every node into corpus dicts). Now a fresh build persists the bm25s index + corpus (`_persist_bm25_sidecar`, skipped while an indexing run is active), and the next cold start mmap-loads it (`_load_bm25_sidecar`): score arrays memory-mapped, corpus served lazily from JSONL. Steady-state RAM drops by roughly the vault's text size; prewarm's `building_bm25` stage becomes a fast open.
- **Staleness discipline:** the sidecar carries `sidecar_meta.json` (doc count + the index meta's `indexed_at`); a mismatch on either, or any load error, deletes the sidecar and rebuilds from the docstore — the count check alone could accept a same-size-different-content sidecar left by a crash between a checkpoint's persist and its cache invalidation. `_invalidate_retrieval_caches()` now also rmtrees the sidecar (mid-run checkpoints, final persist, `cleanup()`), and the meta file is written last so a torn persist self-invalidates. `/api/reset` and the index-version archive cover the sidecar automatically (it lives inside the index dir); `_persist_index_checkpoint` never touches it.
- **`bm25s` is now pinned** (`>=0.3.9,<0.4`): the retriever package leaves it unbounded, and the sidecar is a serialized format.

### Tests

- The mocked BM25 cache tests now run against a per-test `OBSIDIAN_INDEX_DIR`; new coverage: a real build → persist → mmap-load round trip asserting identical retrieval with `from_defaults` forbidden, stale-doc-count rebuild + re-persist, no-persist-while-indexing, and sidecar removal on invalidation.

## 2026-06-10 (Perf/memory batch 1 — indexing-path RAM & checkpoint cadence)

First batch of `PERF_MEMORY_PLAN.md` (16 GB Apple Silicon audit). All changes are inside `rag/vault.py`; no reindex required.

### Changed

- **Streaming indexer no longer pins every chunk for the whole run** (plan Fix A). `_index_documents_streaming` accumulates the manifest as `{raw_source: chunk_count}` instead of a `list[LlamaDocument]` — the list held a duplicate copy of all chunk text plus per-chunk pydantic overhead until the run ended (~100–400 MB peak on a textbook vault, dominant on incremental re-runs where most chunks are "skipped"). `_write_index_manifest` now takes the counts dict and resolves each unique source once; manifest JSON and `obsidian_meta.json` output are byte-identical. Sourceless documents land in a `""` bucket so `has_vector_data` / `inserted_this_run` / the empty-vault diagnostic keep their exact pre-change semantics.
- **Mid-run checkpoints are time-gated** (plan Fix F). New class attr `_PERSIST_MIN_INTERVAL_S = 600` beside `_PERSIST_EVERY = 500`: a checkpoint now requires *both* 500 pending inserts *and* 10 minutes since the previous attempt. Each checkpoint re-serialises and re-validates the entire store with the GIL held while chat is blocked on the rw write lock — on a fast embedder the old count-only cadence spent a growing share of the run inside O(N) dumps. Crash re-work bound becomes max(500 chunks, ~10 min); the don't-spin reset applies to both gates on checkpoint failure. Patch the interval to 0 in tests for count-only behaviour.
- **GC hygiene after indexing and reset** (plan Fix C, downgraded-but-free): `gc.collect()` in `index_vault`'s `finally` and at the end of `cleanup()` breaks LlamaIndex/parser reference cycles promptly. Hygiene only — CPython arena behaviour is unchanged.

### Tests

- The three streaming-indexer assertions moved from doc-id lists to `{source: count}` dict equality (strictly stronger); new `test_mid_run_checkpoint_gated_by_count_and_min_interval` pins the dual gate in both directions.

## 2026-06-10 (Big-PDF range splitting)

### Changed

- **Vault indexing: large PDFs (> 1000 pages) become one document per 1000-page range** (`rag/vault.py::_load_pdf_range_documents`) instead of a single concatenated text. Peak indexing RAM is now bounded by ~1000 pages regardless of file size; each range is cached immediately (`{sha256}-pSSSSS-EEEEE.txt`), so a cancel keeps completed ranges and a resumed run continues from the last cached one. The page cap rose from 5 000 (skip-with-warning) to the new shared `PDF_MAX_PAGES` = 20 000 — previously skipped textbooks are picked up incrementally by the next index run; nothing already indexed is touched. Range documents salt their chunk hash with `page_start` so ranges of one file can never collide on a doc_id; single-document files keep the original unsalted hash (no reindex). Legacy whole-file caches of big PDFs are grandfathered as single documents — delete the cache file to opt a PDF into per-range documents. `read_note` stitches per-range caches back together for agent reads.
- **Single Paper: uploads over 1000 pages no longer hard-fail.** The extraction worker (`services/pdf_service.py::_extract_all_pages`) mirrors the vault's range loop and concatenates, up to `PDF_MAX_PAGES`; oversized files get a clean error instead of the extractor's internal "Page range too large".

### Fixed

- Stale docstring in `pdf_extractor.py` pointed at a non-existent page-range chunker in `summarizer.py`; it now names the two real callers.

## 2026-06-10 (Atomic writes + hot-path I/O caching)

### Fixed

- **Five remaining non-atomic file writes now use temp-sibling + `os.replace`.** Most critical: `audit/engine/bridge.py::save_mapping` — `mapping.json` is the audit subsystem's only writable file and holds hand-curated PDF→citation-key matches a rescan cannot regenerate; a crash mid-write used to truncate it. Also converted: the PDF-extraction worker's result hand-off (a SIGKILL after the 600 s timeout escalation could leave the parent a torn JSON, which now also maps to a clean `RuntimeError` instead of a raw `JSONDecodeError` via the new `_read_extract_result`), `/api/export-summary`'s write to `~/Downloads`, the `.last_deletion_log` crash-forensics marker, and the Ollama PID file. New shared helper: `core/utils.py::write_text_atomic` (text twin of `rag/vault.py::_write_json_atomic`).
- **`save_feedback` no longer stalls the request thread up to 2 s.** The flock retry loop (40 × `LOCK_NB` + 50 ms sleeps, after which it appended anyway) is now a single non-blocking attempt with the same append-anyway outcome — `_feedback_lock` already serialises writers in this single-process app.

### Changed

- **`load_config()` is stat-cached.** Keyed by `(path, st_size, st_mtime_ns)` — the `UsageTracker._disk_records` pattern — so the 4-5 reads per vault-chat request (26 call sites app-wide) cost one `stat()` plus a defensive deepcopy instead of an open+parse each. `save_config()` invalidates explicitly; external rewrites are caught by the key change.
- **`/api/obsidian/status` polls no longer parse `obsidian_meta.json` three times each.** `get_status`, `is_partial_index`, and `get_index_warning` (plus the docstore-manifest fallback) share a stat-keyed `_read_index_meta()` cache on `ObsidianVaultManager`, invalidated eagerly by `_write_index_meta`.

## 2026-06-10 (Offline rendering, config-clobber fix, hermetic tests)

### Fixed

- **Chat rendering no longer depends on a CDN.** `marked.js` (v15.0.12, MIT) is vendored at `static/js/vendor/marked.min.js` and loaded locally; previously it came from jsdelivr, and any CDN/network hiccup inside the PyWebView renderer surfaced as `Error: Can't find variable: marked` — losing the answer the LLM had already produced. `vault.js` additionally falls back to plain-text rendering if `marked` is somehow still unavailable, so a renderer problem can never cost the user the answer.
- **Online model selection no longer clobbers the local one.** `POST /api/config` routes the UI's generic `llm` field into the active online provider's per-provider key (`openai_model` / `anthropic_model` / `google_model`) and now *removes* `llm` from the payload. Previously the online model name was also persisted into `llm`, silently overwriting the saved Ollama/LM Studio selection (observed in a real config as `provider=ollama` + `llm=claude-sonnet-4-6`, which Ollama 404s on). Pinned by `smoke_test.py::test_03b`.
- **Test suite is hermetic.** `core/constants.py::_get_base_dir()` honours a `CHATEKLD_BASE_DIR` env override, and the root `conftest.py` points it at a per-session temp dir before any app import. Tests no longer read or **write** the user's live config / DB / feedback files (smoke_test's config POSTs used to rewrite the real saved model; chat-route tests used to flip onto the agent path whenever the live config had `vault_agent_enabled: true`).
- **Four broken tests in `test_vault_regressions.py` repaired** (assert-on-restored-method, JSON-escaped em dash in SSE body, and two clamp assertions that contradicted the documented `coerce_int_in_range` clamp contract). The "Known pre-existing test defects" section in CLAUDE.md is gone; the full suite passes.
- **Anthropic pricing corrected.** `claude-opus-4-7` / `claude-opus-4-5` were listed at $15/$75 per MTok; the Opus tier has been $5/$25 since Opus 4.5. Added `claude-opus-4-8`, `claude-opus-4-6`, and `claude-fable-5`; the Anthropic adapter's curated model list drops retired Claude 3.x IDs (they now 404) and adds the current generation.

### Changed

- **HF offline mode at launch.** `launch.py` sets `HF_HUB_OFFLINE=1` before app imports when the configured reranker model is already in the HuggingFace cache — eliminating the ~25 HEAD/GET requests sentence-transformers makes to huggingface.co on every reranker load. First-time downloads still work (env not set when the cache is empty); changing `vault_reranker_model` to an uncached model mid-session requires an app restart to download, and the reranker-failure warning says so.
- **Generic `/api/config` strips `audit_*` keys.** Library Audit settings must flow through `POST /api/audit/config`, which validates path traversal / absolute paths; the generic endpoint previously accepted them unvalidated.
- **Audit frontmatter warnings surfaced in the UI.** YAML parse failures in vault notes now appear in the Library Audit status feed (first 5 + count) instead of only in `chatekld.log`; notes are also parse-cached by mtime so the bridge and inventory passes no longer parse every Zotero note twice per scan (warnings re-surface on cache hits so a malformed note never silently looks fixed).
- **`/api/usage` no longer re-parses the whole JSONL per poll.** `UsageTracker` caches parsed disk records keyed by the log file's `(size, mtime_ns)`.
- **Agent-route retrieval defaults aligned.** `_run_agent`'s `VaultToolContext` fallbacks for `hybrid_enabled` / `reranker_enabled` now default `True`, matching the documented engine defaults (only reachable if a config key fails coercion).
- **`renderExclusions` XSS hardening.** Vault exclusion rows are built with `createElement`/`textContent` instead of interpolating folder names into `innerHTML`.
- **CLAUDE.md restructured for context cost.** The root file had grown to ~49 KB (~12K tokens loaded into every Claude Code session). It now holds only cross-cutting rules and invariants (~20 KB); deep implementation notes moved to subtree files that load on demand when working in those directories: `rag/CLAUDE.md` (indexing pipeline, locks, prewarm, retrieval mechanics), `core/llm/CLAUDE.md` (adapters, fallback, pricing, tool wire formats), `core/agent/CLAUDE.md` (agent loop), `audit/CLAUDE.md` (Library Audit internals + endpoint payloads). No content deleted — redistributed.

## 2026-06-08 (Audit-matrix fixes + query-time RAG improvements)

### Added

- **Query-time retrieval-quality knobs (no reindex).** Vault chat gained MMR diversity on the dense leg (`vault_mmr_enabled` + `vault_mmr_lambda`), multi-query expansion (`vault_query_expansion` + `vault_num_queries`, effective on local providers), a configurable reranker candidate pool (`vault_rerank_pool_multiplier` / `_floor` / `_ceiling`, with the ceiling also a live `rerank_pool_ceiling` body override), and a `concise` answer mode. All are per-request body overrides resolved by `_resolve_chat_params`, persisted to the matching `vault_*` config keys, exposed in the vault chat "Retrieval & Generation" UI, and reindex-free. Pinned by a `TestReindexInvariant` guardrail that fails if the embedding model, chunker params, chunk-ID scheme, or index version change.

### Changed

- **`vault_top_k` default raised 6 → 8** (the `_effective_top_k` autoscaler still caps small-context models).
- **Quota errors are now terminal.** Hard quota / billing exhaustion (OpenAI `insufficient_quota`, Anthropic "credit balance", billing strings) is detected by `core.llm.base.looks_like_quota` and mapped to `ErrorCategory.QUOTA` — excluded from `fallback_on`, non-retryable — so it is no longer treated as a transient `rate_limit` (a Gemini per-minute 429 stays retryable). Wires up the previously-dead enum.
- **Mid-stream fallback no longer duplicates answers.** Both online streaming sites (`rag.engine._OnlineStreamingResponse._stream`, `rag.summarizer._stream_online`) fall back only before the first token; a failure after ≥1 token re-raises (the route emits a structured SSE error) instead of re-streaming the whole answer from the fallback provider.
- **Agent loop edge cases.** `FinishReason.LENGTH` now emits a truncation notice instead of posing as a complete answer; `CONTENT_FILTER` ends the turn cleanly instead of triggering the RAG-fallback / capability nag. `online_max_retries` is now honoured on the agent path (`cfg` threaded into `resolve_chat_provider`).
- **Accounting / robustness.** Usage records carry a per-record `uid` so the in-memory ring and on-disk JSONL never double- or under-count; the OCR cache is checked before the failure cooldown so a cached page is always served; the Google adapter records `cachedContentTokenCount`; the API-key redaction pattern matches service-account / admin keys (`sk-svcacct-`, `sk-admin-`); a corrupt non-partial index recovers as `paused_scan` (surfacing the integrity error); `/api/summarise` caps `language`.

### Removed

- Verified-dead code: `MARKITDOWN_MIN_CHARS`, `DEFAULT_TOP_K`, `VAULT_ALLOWED_EXTS`, `core/llm/prompt.py::truncate_to_char_budget` and its unused `system_prompt` param, `Record.parent_tags`, `NoteInfo.wikilinks`, two unused function params (`inventory_summary(settings)`, `_classify_blocks(page_width)`), an unused `ImageOps` import, and the unused `pydantic-settings` dependency. Stale "slice 5/7" comments and a contradictory checkpoint-retry comment fixed; the `core/providers/__init__.py` `Provider` hint moved under `TYPE_CHECKING`.

### Notes

- All of the above is **reindex-free** — no change touches the embedding model, chunker, chunk-ID scheme, or `OBSIDIAN_INDEX_VERSION`.
- Most code-review findings were applied; a few "dead code" claims were corrected because the symbols had test/real readers (`is_held`, `naming.py`, `RAG_QA_PROMPT`, `Image`) and were kept. The items intentionally deferred (e.g. the vendored Zotero attachments removal) are tracked under **Known Issues / Deferred Work** in `CLAUDE.md`.

## 2026-05-25 (Library Audit tab — kb_harmonizer integration)

### Added

- **Library Audit tab.** New third top-level tab that reconciles the configured Obsidian vault against Zotero (SQLite snapshot + Better BibTeX `_master.bib`), local PDFs under the attachments tree, and macOS Finder tags. Strictly read-only against external stores; the only writable file is `BASE_DIR/audit/mapping.json` (manual PDF↔bib overrides). Six reports surfaced: Tag Drift, Unread PDFs, Zotero Queue, Read PDFs Missing Zotero, Bib Entries Without PDFs, Duplicate PDFs — plus a per-citation-key Inventory view.
- **Manual-only scan.** The audit runs only when the user clicks Run Scan. `audit_manager` is instantiated at module import time as `idle`; no path in `app.py:create_app()` ever calls `start_scan()`. `test_audit.py::TestFlaskAppBootDoesNotScan` pins this with a `mock.patch.object` spy.
- **`audit/` package.** Vendored from kb_harmonizer (`audit/core/` connectors for bib / obsidian / zotero / pdf_annotations / finder_tags / hashing / naming, `audit/engine/` for bridge + inventory + duplicates, plus the five report builders under `audit/engine/reports/`). The PySide6 UI subpackage was dropped. `audit/config.py` adapts papermind's `config.json` keys into the kb_harmonizer-shaped `Settings` dataclass, exposing every previously-hardcoded subpath as a configurable key. `audit/manager.py` runs the single background scan thread with cooperative cancel via `_stop_event`. `audit/serialize.py` converts engine dataclasses to JSON dicts. `audit/scan.py` is the developer CLI (`python -m audit --check ...`).
- **`/api/audit/*` blueprint.** Eight endpoints (`config` GET/POST, `status` GET, `scan` POST, `cancel` POST, `inventory` GET, `reports/<name>` GET, `mapping` POST, `reveal` POST) — all behind the same `X-Requested-With: ChatEKLD` gate as the existing routes. Path-traversal and absolute-path attempts in vault-relative subdir fields are rejected at the route layer. The macOS-only `reveal` endpoint bounds `open -R` / `open` / `zotero://` invocations to paths under the configured vault root or Zotero storage tree.
- **`/api/reset` integration.** Reset now also calls `audit_manager.request_cancel()` + `clear_results()` so an in-flight audit is signalled to abort and the cached inventory is dropped, matching the post-reset "no scan yet" empty state on the Library Audit tab.
- **8 new config keys.** `audit_attachments_subdir` (default `Z_attachments`), `audit_biblio_articles_subdir` (default `biblio_articles`), `audit_zotero_notes_subdir` (default `Z_Zotero_Notes`), `audit_master_bib_path` (default `_master.bib`), `audit_zotero_sqlite` (default `~/Zotero/zotero.sqlite`), `audit_zotero_storage` (default `~/Zotero/storage`), `audit_annotations_read_threshold` (default `5`), `audit_biblio_skip_prefix` (default `z_item`). Vault root is shared with the existing `obsidian_vault_path` so there is no second vault setting.
- **New requirements.** `pikepdf>=8.15,<10.0` (read-only PDF annotation counting), `ruamel.yaml>=0.18,<0.19` (Obsidian frontmatter parsing that preserves quoting), `bibtexparser>=1.4,<2.0` (Better BibTeX exports), `beautifulsoup4>=4.12,<5.0` (already transitive via markitdown — pinned explicitly so an upstream change does not silently break the Zotero child-note HTML stripper).
- **PyInstaller bundling.** Added `audit/` to `--add-data` and `pikepdf` / `ruamel.yaml` / `bibtexparser` / `bs4` to `--collect-all` in `install_and_build.sh` so the frozen .app bundles the new package and its C-extension dependencies.
- **Tests.** 86 ported kb_harmonizer unit tests under `tests/audit/` (bib parser, bridge filename / mapping helpers, hashing with cancellation, naming scorer, obsidian frontmatter / link parsing, PDF annotations error contract). 17 new audit-level tests in `test_audit.py` covering the Settings adapter, manager state machine, the create_app no-auto-scan regression, the auth gate on every `/api/audit/*` route, and config-validation rejection of path traversal / absolute paths / non-finite thresholds. Total: 154 tests passing across `tests/audit/`, `test_audit.py`, `test_validators.py`, `smoke_test.py`.

### Notes

- The kb_harmonizer engine is intentionally not invoked automatically anywhere — settings can be edited, the tab can be visited, the page can be reloaded, and the audit stays `idle` until Run Scan is clicked. The scan reads only from external stores; the only writable file (`mapping.json`) is touched exclusively by `POST /api/audit/mapping`.
- An empty `audit_biblio_skip_prefix` is now treated as "skip nothing". The vendored kb_harmonizer code used `str.startswith(prefix)`, which would skip every PDF when the prefix is empty; the engine and CLI now both guard against that.
- Finder tag reads remain macOS-only (`audit/core/finder_tags.py` short-circuits to an empty list on non-Darwin platforms). The `/api/audit/reveal` endpoint also returns 501 outside macOS. The rest of the audit subsystem works on any OS.

## 2026-05-24 (ReAct agent mode for vault chat)

### Added

- **Opt-in agent mode for vault chat.** New `#vault-agent-enabled` checkbox under *Retrieval & Generation* in the Obsidian tab routes `/api/obsidian/chat` through a ReAct loop that can call `vault.search` / `vault.read_note` / `vault.list_materials` across up to 6 iterations per turn (`vault_agent_max_iterations`, body-overridable 1–12). Default off, so non-agent chats are byte-for-byte identical to today. Works against all five chat providers — Anthropic, OpenAI, Google, Ollama (0.4+), and LM Studio — via native tool-use APIs. Adapter capability advertised through `LLMProvider.supports_tool_use()`; the configured local model's actual tool-call reliability is detected at runtime through a malformed-call counter.
- **Fail-closed RAG fallback.** Two consecutive iterations where the model produced no parseable tool call trigger an info event and a clean re-run of `stream_chat` against the original user message (no agent trace mixed in). Successful tool calls reset the streak.
- **Per-session capability warning.** After two consecutive turns each end in fallback, a one-shot info event suggests a tool-capable model (Qwen 2.5, Llama 3.1+, Mistral Nemo, or any online provider). Tracked via a module-level `AgentCapabilityState` singleton in the vault route; resets on any successful agent turn.
- **Live reasoning trace UI.** A collapsed `<details class="agent-trace">` block renders above the bot bubble, with one section per iteration showing thoughts, tool calls (monospace `name(args)` snippets), and tool results (400-char `<pre>` snippet with `error`/`truncated` tags when relevant). The final answer still streams into the bot bubble as today via `{"token": "..."}`.
- **Per-turn usage footer.** After a successful agent turn the route emits one more `info` event summarising iteration count + token totals (`Agent: 3 iterations · 1247 in / 312 out tokens`). Cost suffix appears only for online providers.
- **New SSE event types.** `{"iteration": N}`, `{"thought": "..."}`, `{"tool_call": {...}}`, `{"tool_result": {...}}` join the existing `token` / `info` / `error` / `[DONE]` frames. Tool results carry `is_error` and `truncated` flags.
- **Tool layer (`core/agent/`).** New `protocol.py` (typed `AgentEvent` subclasses), `tools.py` (`ToolRegistry` + tiny JSON-Schema validator + `wrap_untrusted` guard), `vault_tools.py` (the three concrete vault tools), `budget.py` (`UsageBudget` per-turn accumulator), and `loop.py` (`run_agent_loop` driver). The agent layer never reaches into the indexer's internals; tool runners only call public `ObsidianVaultManager` methods.
- **`ObsidianVaultManager.retrieve()` + `read_note()` helpers.** Extracted from the retrieval phase of `stream_chat` and from the indexer's path-safety logic respectively. `retrieve` holds `_index_mutation_lock` for the brief retrieval phase (matching `stream_chat`); `read_note` rejects path traversal, excluded dirs, and unsupported extensions, and serves PDFs from `_pdf_cache_file` with a bounded fresh-extract fallback (`EXTRACT_MAX_PAGES_PER_CALL` cap, no OCR).
- **Tool-use wire format helpers (`core/llm/tool_schema.py`).** `build_openai_messages` / `build_anthropic_messages` / `build_gemini_contents` translate provider-agnostic `LLMRequest.tools` + `tool_history` into each provider's native dialect; `parse_openai_tool_call` / `parse_anthropic_tool_use` / `parse_gemini_function_call` go the other way, returning `None` on malformed JSON so the agent loop can count the iteration as malformed instead of crashing. Gemini-specific sanitiser strips `default` / `additionalProperties` fields its function-calling endpoint rejects.

### Changed

- **`LLMRequest` gains tool-use fields.** New `tools: list[ToolSchema]`, `tool_choice: Optional[str]`, and `tool_history: list[ToolTurn]` fields all default to empty/None — existing callers that pass `tools=[]` are byte-identical to today. `LLMResponse` gains `tool_calls: list[ToolCall]` (also defaults to empty).
- **Local adapter (Ollama / LM Studio) gains a non-flatten tool branch.** The legacy `stream_chat` path still flattens `messages` to a single user prompt; the new `_generate_with_tools` branch carries structured `messages` + `tools` directly through `ollama.chat()` / LM Studio's OpenAI client so multi-turn tool conversations (assistant `tool_call` → user `tool_result` → assistant) are preserved.
- **`SimpleQueryEngine` refactor.** Extracted `_build_retrieval_pipeline`, `_retrieve_chunks`, `_nodes_to_chunks` helpers from `query()`. New public `retrieve(message) -> list[RetrievedChunk]` runs the same hybrid + rerank pipeline as `query()` but stops before the LLM. `_query_online` now goes through `_retrieve_chunks` for de-duplication.

### Notes

- Slice-by-slice landing for safe incremental review. Each slice is independently mergeable and the existing single-shot RAG path keeps working at every step. See commits `Agent mode slice 1` through `Agent mode slice 8`.
- The decision to issue final answers as a single `TokenEvent` (no streaming on the last iteration) saves one LLM round-trip per agent turn at the cost of the typewriter effect on the final answer. Revisitable if user feedback warrants it.

## 2026-05-22 (audit M4 + M6 + RAG UX)

### Added

- **Custom vault chat system prompt.** A new "System Prompt Override" textarea under *Retrieval & Generation → Fine-tuning* in the Obsidian tab lets the user prepend behavioural instructions to the selected Answer Mode template. The mode template's safety preamble, untrusted-context guard, and `{context_str}`/`{query_str}` placeholders remain app-controlled, so a typo cannot disable retrieval grounding. Capped at `SYSTEM_PROMPT_LIMIT` = 4000 chars (shared with the single-paper override). Persisted as `vault_chat_system_prompt`; per-request body wins, then config, then default empty. On the online path the prompt is routed through `LLMRequest.system_prompt` so providers use their native system field. Pure query-time parameter — no re-index when changed.
- **Copy buttons on vault chat user prompts and answers.** Wires the existing `copyToClipboard` helper and `.copy-btn` styling (both shipped but unused) into the vault chat flow. User messages get a Copy button on render; bot messages get one after the stream completes so the per-token `innerHTML` re-render does not clobber it. Bot copies use the raw markdown rather than the rendered HTML so paste-into-notes works. Keyboard-discoverable via `:focus-visible`.
- **Shared validator helpers (audit M6).** `api/validators.py` owns the coerce/first-valid mini-framework that used to live as private helpers in `api/routes/vault.py`. Adds `coerce_enum`, `coerce_regex`, `coerce_non_empty_string`, `coerce_string_max_len` alongside the existing `coerce_int_in_range` / `coerce_float_in_range` / `coerce_bool` / `first_valid` so `paper.py`, `config.py`, `status.py`, and `usage.py` can drop their ad-hoc `if not x` / inline regex / inline enum checks. `test_validators.py` pins the contracts.

### Changed

- **Vault loader is a generator (audit M4).** `ObsidianVaultManager._load_vault_documents` now yields one `LlamaDocument` at a time (MD docs first, sorted; then sorted referenced image descriptions; then PDFs in scan order) instead of pre-materialising every extracted PDF before chunking. Peak RAM is bounded by the largest single source document plus the chunker/indexer backlog rather than `Σ extracted_text` across the vault — a meaningful win on textbook-sized vaults where each PDF can contribute tens of MB of concatenated text. Chunk IDs are unchanged (within-document enumeration order is preserved). No re-index required. The PDF signature-cache write moved into a `try/finally` so it survives early generator close.
- **Empty-vault diagnostic moved to post-streaming.** With streaming, the "no indexable files" check fires when the streamer reports `added=skipped=failed=0` and the stop event is not set, rather than from a pre-chunking `if not raw_docs:`. Same user-visible behaviour; meta is not written as "done" with zero documents.
- **`SYSTEM_PROMPT_LIMIT` centralised in `core/constants.py`.** Single-paper and vault-chat overrides now share the same 4000-char cap rather than spelling it out inline at each call site.

### Removed

- The private `_coerce_bool` / `_coerce_int_in_range` / `_coerce_float_in_range` / `_first_valid` / `_MISSING` aliases in `api/routes/vault.py`. Tests that depended on them now import the public versions from `api.validators` (the resolver `_resolve_chat_params` in the vault route is unchanged and remains the canonical body→config→default precedence implementation).

### Notes

- The audit's "hidden precondition" on M4 (chunk-ID stability requires preserved within-document enumeration order) is now pinned by `test_chunk_ids_stable_across_runs` in `test_vault_regressions.py`. A future refactor that subtly reorders nodes will flip this assertion before it can ship a corrupt index.

## 2026-05-17 (checkpoint hardening + repair tooling)

### Fixed

- Vault index checkpoints now persist LlamaIndex storage into a sibling temporary directory, validate the resulting JSON files, and only then promote them into `obsidian_storage/`. This prevents a failed write from truncating the active multi-GB vector-store JSON.
- Vault chat now preflights persisted index files before lazy-loading them and surfaces a clearer checkpoint-corruption error instead of passing through a raw `JSONDecodeError`.
- The status payload now includes `integrity_error` when metadata claims vector data but checkpoint files are missing or have an incomplete JSON tail.
- `/api/obsidian/chat` streams stage `info` events while loading the saved index, building BM25, loading the reranker, and starting retrieval/model generation.
- Repair scripts were hardened: `repair_simple_vector_store.py` accepts compact and spaced SimpleVectorStore prefixes and can promote an existing `.repaired` candidate; `prune_storage_to_vector_store.py` has a dry-run mode, preserves manifest metadata when possible, and no longer rewrites `inserted_this_run`.
- PyInstaller collection now includes the BM25 packages (`llama_index.retrievers.bm25`, `bm25s`, and `Stemmer`).

### Added

- Regression coverage for corrupt checkpoint recovery, failed temp-checkpoint promotion, and a real BM25 exact-term retrieval smoke test.

## 2026-05-17 (external venv + BM25 dependency restore)

### Fixed

- Moved the project virtual environment out of the repository to `~/venvs/papermind2026`; `install_and_build.sh` now defaults to that path through `PAPERMIND_VENV_DIR` while remaining overrideable.
- Restored the BM25 dependency with `llama-index-retrievers-bm25>=0.7,<0.8`, which is compatible with the current `llama-index-core>=0.14.18,<0.15` pin. The earlier `0.5.x` range required `llama-index-core<0.13` and made `pip install -r requirements.txt` unsatisfiable.
- Added regression coverage for BM25 package availability, manager cache/rebuild behaviour, and RRF fusion wiring in `SimpleQueryEngine`.

### Notes

- No vault re-index is required for the BM25 restore. BM25 builds lazily in memory from the existing saved docstore on the first hybrid query and is invalidated when the docstore changes.

## 2026-05-14 (hybrid retrieval + cross-encoder reranking)

### Added

- Vault chat retrieval is now hybrid by default. A BM25 (lexical) retriever and the existing dense (cosine) retriever run in parallel; their result lists are fused with reciprocal-rank fusion via `QueryFusionRetriever(mode="reciprocal_rerank", num_queries=1)`. `num_queries=1` keeps it a thin RRF wrapper — no LLM-driven query rewriting, no extra round-trip to the local provider per chat.
- Cross-encoder reranking on top of hybrid retrieval. Default model `cross-encoder/ms-marco-MiniLM-L-6-v2` (~67 MB, ~80-200 ms / 30 candidates on Apple Silicon CPU). The reranker narrows a candidate pool (sized `min(max(top_k * 4, 20), 50)`) down to `top_k` chunks before they reach the LLM.
- Three new persisted config keys: `vault_hybrid_enabled` (default `true`), `vault_reranker_enabled` (default `true`), and `vault_reranker_model` (default `cross-encoder/ms-marco-MiniLM-L-6-v2`). The two boolean knobs are also accepted as per-request body fields (`hybrid_enabled`, `reranker_enabled`) on `/api/obsidian/chat` with the same body→config→engine-default precedence as the existing live controls. The model name is config-only — `/api/obsidian/chat` does not accept it as a body override so a malicious page cannot swap in arbitrary HuggingFace repos to download.
- `ObsidianVaultManager._get_bm25_retriever(top_k)` caches the BM25Retriever by docstore size and survives across chats. Invalidated explicitly by `_invalidate_retrieval_caches()` after every successful `idx.storage_context.persist(...)` (mid-run checkpoint and final persist) and inside `cleanup()`. Size-fingerprint same-size delete+insert races are tolerated as eventually-consistent — the indexer's invalidation resyncs.
- `ObsidianVaultManager._get_reranker(model_name, top_n)` lazy-loads the cross-encoder on first chat that needs it and caches the loaded model. A sticky `_reranker_failed` flag prevents re-attempting the same model name after a failed load; the user must change `vault_reranker_model` to retry. The reranker survives `cleanup()` / `/api/reset` so vault switches do not re-download weights.
- Clear-chat button in the vault chat panel. Server holds no chat history (each `/api/obsidian/chat` call is independent), so the button is a pure DOM operation — clears the chat container and the status line. Refuses while a query is in flight so an in-progress bot message is not orphaned mid-stream.

### Changed

- `SimilarityPostprocessor(similarity_cutoff=...)` is now attached only when the reranker is absent. Cross-encoder scores are on a different scale from dense cosine, so applying a 0.25 cosine cutoff after rerank would silently drop high-rerank, low-cosine chunks. Existing dense-only behaviour is preserved exactly when `hybrid_enabled` and `reranker_enabled` are both false (covered by the `test_similarity_cutoff_reaches_postprocessor` regression test).
- `_effective_top_k` autoscaling now applies to the post-rerank final chunk count, not the pre-rerank candidate pool. A small-context model is therefore not overwhelmed by reranker breadth — only the final, autoscaled chunks reach the LLM.
- Added optional dependencies `llama-index-retrievers-bm25` and `llama-index-postprocessor-sbert-rerank` + `sentence-transformers`. Both are import-guarded in `rag/engine.py`: `BM25Retriever` / `SentenceTransformerRerank` resolve to `None` when the package is absent, the manager treats `None` as "feature unavailable", and the chat falls back to the dense-only path with a one-shot warning.
- `install_and_build.sh` pre-downloads the cross-encoder weights into `~/.cache/huggingface/hub/` after `pip install` so first-chat latency does not include a model fetch. Includes `--collect-all sentence_transformers` in the PyInstaller invocation so the bundled `.app` ships the torch / sentence-transformers extension modules.

## 2026-05-13 (live vault chat controls)

### Added

- Vault chat now exposes four live query-time knobs in the Obsidian tab, all of which take effect on the next Send without any reindex. New collapsible "Retrieval & Generation" panel surfaces **Top K** (2-12) and **Answer Mode** (`Strict` / `Balanced` / `Exploratory`) by default, with a nested **Fine-tuning** section for **Similarity Cutoff** (0.0-0.7) and **Temperature** (0.0-2.0).
- `/api/obsidian/chat` accepts new optional body fields `top_k`, `similarity_cutoff`, `prompt_mode`, and `temperature`. Values are range-clamped server-side; an invalid `prompt_mode` is dropped (falls back to the persisted config) rather than rejected.
- Persisted config keys: `vault_top_k`, `vault_similarity_cutoff`, `vault_prompt_mode`, `vault_chat_temperature`. Restored on app launch via `applyVaultChatParams`; saved debounced (400 ms) when controls change. The request body remains authoritative — a save failure never changes what the next Send actually uses.
- Three `RAG_QA_PROMPT_*` templates in `rag/engine.py`. `strict` keeps the previous behaviour (refuse when context does not support the answer). `balanced` grounds claims in context but marks unsupported parts rather than refusing the whole question. `exploratory` permits cautious cross-excerpt synthesis with explicit hedging — useful when retrieval is thin but the user wants a starting point.
- `SimpleQueryEngine.top_k_explicit` flag bypasses the `_effective_top_k` autoscaling when the caller has chosen a value (so a user picking 12 with a 8k-context model still gets 12). Implicit callers (no flag) keep the historical autoscaling as a guard rail.
- 11 new unit tests in `test_vault_regressions.py::TestVaultChatLiveControls` cover prompt-mode dispatch, top_k autoscaling vs. explicit, cutoff plumbing, temperature forwarding, and route-level clamping / fallback / body-wins precedence.

## 2026-05-13

### Fixed

- Vault chat against an LM Studio model with a non-OpenAI-style name (e.g. `google/gemma-4-e4b`) no longer returns the spurious "No relevant content found in your vault for this query." message. LlamaIndex's upstream `is_chat_model()` helper recognises only OpenAI model name patterns and was routing every other model through the legacy `/v1/completions` endpoint, which LM Studio answers with a single empty content delta. `_LMStudioOpenAI.metadata` now forces `is_chat_model=True` so every LM Studio chat model goes through `/v1/chat/completions`. Retrieval was never broken — only the LLM call.
- Paused-during-scan indexing now recovers across an app restart even when no `docstore.json` was ever persisted. `get_status()` no longer requires both `obsidian_meta.json` and `docstore.json` to exist; it reads meta and trusts the `has_vector_data` field (with a dir-scan fallback). The recovered `phase` is mirrored into `_current_phase` so the status payload stops flickering back to `"idle"` on cold boot.
- Tightened the embed-mismatch warning. It now fires only when an actual vector store exists on disk with a different embedding model. A paused-scan state with `partial=True` but no vectors silently starts fresh instead of emitting a misleading "Resuming a partial index…" warning. The text was also reworded to "Existing index was built with embedding model …" since the warning is no longer scoped to resumes.

### Changed

- Renamed the `inserted_count` field in `obsidian_meta.json` to `inserted_this_run` to make clear it is a per-run counter, not a cumulative chunk total. Old meta files written with `inserted_count` are still readable; the field is informational and not load-bearing for resume.
- Vision and OCR prompts in `services/vision.py` and `core/constants.py` were tightened for scientific-document and scanned-page text extraction. Pure prompt-string changes; no public-contract or schema impact.

## 2026-05-12 (audit fixes)

### Fixed

- Single Paper OCR now honours the user's `ocr_provider` / `ocr_model`. The spawn subprocess in `services/pdf_service._extract_worker` reloads config and re-applies the configured provider and model on the child's `glm_ocr_manager` before extraction. Previously the subprocess used `DEFAULT_OCR_MODEL` regardless of UI selection.
- `/api/obsidian/cancel` no longer leaves `partial: True` on disk. The indexer's final meta write distinguishes cancel (clean stop, `partial=False`) from pause (`partial=True`), so a cold boot after cancel shows `done`/`idle` instead of a spurious Resume button.
- `/api/reset` waits for the indexing thread to finish before `shutil.rmtree`-ing the storage directory and calls `obsidian_manager.cleanup()` to drop the in-memory index. The previous flow could let a stray `storage_context.persist()` recreate the directory after the rmtree.
- Vault chat no longer blocks for the duration of a multi-hour indexing run. `ObsidianVaultManager.index_vault` holds the rw write lock only for the setup phase, the mid-run checkpoint callback, and the final persist; the streaming insert loop runs without it, and `self._index` is published immediately after setup.
- Vault chat producer no longer silently drops tokens on a slow consumer. `token_q.put` blocks (no 2 s timeout) and the SSE generator's outer `finally` sets the cancel event so the producer breaks out on client disconnect.
- SQLite connections opened via `get_db_connection` are now closed on exit. The helper is a `@contextmanager` that commits on success, rolls back on exception, and always calls `conn.close()`. `/api/reset` no longer uses a bare `sqlite3.connect` either.
- `/api/log` is rate-limited to 100 messages per rolling 60-second window per process.

### Changed

- `VisionManager.check_availability` / `GLMOCRManager.check_availability` are informational only. They never gate a call and never rewrite `self.model`. Whatever the user configured is the model that gets called; the probe just reports a best-effort match. `VisionManager` now uses the same exact + base-name match as `GLMOCRManager`.
- `_extract_image_description` frees the raw image bytes immediately after base64 encoding to lower the peak RAM transient on 20 MB images.
- New endpoint `DELETE /api/upload/<upload_id>`. The Single Paper "New Paper" button now deletes the prior upload row instead of leaving it in `uploads.db`.

### Frontend

- Vault chat HTML is sanitised through `sanitiseHtml` before `botMsg.innerHTML = …` so a retrieved chunk that contains `<script>` cannot execute in the renderer.
- Modals (`#excl-modal`, `#image-exts-modal`, `#pull-modal`, `#howto-modal`, `#reset-modal`) carry `role="dialog"`, `aria-modal="true"`, and `aria-labelledby`. `openModal` installs an Escape-to-close handler and a Tab/Shift-Tab focus trap; `closeModal` removes the handler and restores focus to the trigger.

## 2026-05-12 (later)

### Changed

- Vault image indexing is now configurable. The `vault_image_exts` config key holds the list of image extensions sent through the vision model for description and embedding. The list is edited from a new Image Extensions modal on the Obsidian tab.
- Image descriptions are cached under `obsidian_storage/image_cache/{vault_key}/{sha256}.txt` using the same atomic-write pattern as the PDF text cache. Unchanged images reuse the cached description on subsequent runs.
- Factored the cache atomic-write into `_atomic_write_text` so PDF and image caches share one implementation.
- Markdown chunks now carry an `attachments` metadata list. The chunker scans each note for Obsidian wikilinks and inline markdown links, resolves each target relative to the note's directory, and stores the resulting vault-relative paths. External URLs and anchor-only references are dropped. Existing chunks pick up the metadata as they are next re-indexed.
- On cold boot the Obsidian tab now queries `/api/obsidian/status` during initialisation. A paused on-disk index surfaces the Resume button immediately and shows the configured vault path without requiring the user to click Index Vault first.

### Fixed

- After an app restart the Resume button no longer requires the user to click Index Vault to discover that the prior run was partial.

## 2026-05-12

### Fixed

- Per-document insert tolerance and circuit breaker. A single transient embedding failure no longer kills a multi-hour indexing run; 20 consecutive failures aborts and preserves the partial index.
- Streaming chunker. Vault chunks are produced on demand rather than accumulated in a list, capping memory peak at one source document's worth of chunks instead of the entire vault's.
- Mid-run checkpointing. Vault indexing persists every 500 inserts so a crash bounds re-work to roughly that many chunks of work.
- Index state recovery on restart. The Obsidian tab now reflects the saved `partial`/complete status of an existing on-disk index instead of showing "not indexed".
- Pause now honored during the document-loading phase. The scan loop checks the stop event each file.
- Large PDFs (>1000 pages) extracted in 1000-page chunks; PDFs over 5000 pages skipped with a clear warning.
- Heartbeat refreshed between extraction chunks for textbook PDFs so the operation-lock TTL does not expire mid-file.
- Per-PDF size+mtime fingerprint cached in `pdf_signatures.json` so subsequent runs skip re-hashing unchanged files.
- PDF cache write failures emitted to the status feed (throttled) instead of debug-only.
- On `OBSIDIAN_INDEX_VERSION` mismatch the prior index dir is renamed to `obsidian_storage.bak.{ver}.{stamp}/` instead of `shutil.rmtree`-d.
- PDF subprocess uses an explicit `spawn` context and is no longer a daemon, so internal worker pools in PyMuPDF / MarkItDown are not blocked.
- Suppressed MuPDF C-level stderr noise (`could not parse color space`) — PyMuPDF handles these gracefully; surfacing them confused users.
- `OBSIDIAN_INDEX_VERSION` bumped to `obsidian-markdown-v3` to force a one-time clean reindex picking up the widened 16-hex chunk-ID space.

### Changed

- Removed dead code: `MarkItDownVisionShim` (never imported), `_obsidian_status_updates`/`_obsidian_status_lock` aliases in `app.py`, unused `_RagOperationLock` re-export, stale references in the `pdf_extractor.py` module docstring.
- Added `EXTRACT_MAX_PAGES_PER_CALL` as a module constant so vault chunking and the extractor share one source of truth.
- Test coverage: insert-failure tolerance and circuit breaker now have direct unit tests in `test_vault_regressions.py`.
- Pre-existing GLM-OCR retry test fixed to seed the availability TTL timestamp.

## 2026-05-11

### Fixed

- Wrapped uploaded PDF text and vault RAG context as untrusted source material to reduce prompt-injection risk.
- Reworked vault chat retrieval to use per-query LLM/embedding objects, a guarded QA prompt, and a similarity cutoff instead of mutating global LlamaIndex settings during retrieval.
- Streamed PDF uploads to temporary files with a hard byte limit before extraction.
- Moved PDF extraction for uploads into a subprocess so extraction timeouts can terminate the worker.
- Returned structured SSE error events for vault chat failures that happen after streaming starts.
- Validated Obsidian vault paths before saving and rejected broad roots and system directories.
- Replaced the single vault PDF extraction cache JSON file with per-PDF cache files under `obsidian_storage/pdf_cache/`.
- Raised the PyMuPDF minimum version to `1.25.2`.
- Removed orphaned dead code at the end of `pdf_extractor.py`.

### Changed

- Added upload resource-safety and concurrent upload regression tests.
- Documented the new RAG security boundaries, cache layout, and upload timeout behavior.

## 2026-05-10

### Fixed

- Recovered the Indexed Materials panel when an existing persisted index is missing `indexed_materials.json`.
- Retried GLM-OCR with a smaller aligned page image when a scanned PDF page exceeds the vision model context window.
- Restored `/api/summarise` SSE responses.
- Fixed Obsidian chat SSE output and frontend `[DONE]` handling.
- Fixed `/api/obsidian/cancel` lock release.
- Restored `/api/pull`, `/api/reset`, `/api/report-types`, and `/api/log`.
- Routed model listing, status checks, indexing, and chat through the active provider.
- Kept LM Studio model IDs unchanged.
- Kept Ollama model resolution inside the Ollama provider.
- Saved UI-selected chat and embedding models to config.
- Applied vault exclusions as vault-relative paths.
- Indexed `.md` and `.pdf` vault files.
- Kept `.docx` excluded by default.
- Counted skipped image files during indexing and exposed the count in status.
- Surfaced provider warnings and embedding mismatch warnings in the UI.

### Changed

- Restored structured Single Paper prompt presets and built-in document-type prompts.
- Re-exposed Single Paper controls for audience, focus question, temperature, context, max tokens, top-p, repeat penalty, and system prompt override.
- Added `.md` summary export alongside `.txt` export.
- Added an Obsidian indexed-material manifest and UI panel listing indexed notes/PDFs with chunk counts.
- Added separate OCR and vision provider/model selectors, including LM Studio-compatible image calls.
- Made Obsidian vault indexing incremental by skipping unchanged chunks, replacing changed chunks, and deleting stale chunks.
- Added a vault-scoped PDF extraction cache so repeat indexing can skip extraction and OCR for unchanged PDFs.
- Updated README, project notes, and project structure documentation.
- Added a smoke test for the `/api/summarise` SSE contract.
