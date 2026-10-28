# Known issues and decision record

## Real field-test repair review (28 October 2026)

The native follow-up receipt (private reference omitted) records completed
Read/Ask retries and a further exit at `e556e555` during Create form entry. Both
new presentations remain blocked before generation. The launcher again returned
0, with no established exit cause. Local Ask/Read remain scientifically incomplete
or unreliable; DeepSeek completed usable exports, without scientific certification.
Native Ask progress cleanup and nonzero inclusive timing were observed.

The prior Ask export's 21,879 input / 1,720 output tokens exactly include two
concurrent Read calls, one Create call and the actual Ask record (3,693 / 264).
The route filtered only provider/model/time. Ask now filters `vault_rag`, and
online Ask carries the matching tag without replacing enriched prompt capture.
Same-workflow simultaneous requests remain an approximate time-window aggregate.

A second source-native run at `19295c1e` also exited while Settings was visible,
with process/listener disappearance and launcher exit code 0. No generation was
in flight. No new Python crash report or shutdown-cause evidence was established.
This repeats the exit observation, not a proven crash or causal reproduction.

The native follow-up at `19295c1e` reached server readiness after roughly eight
minutes of startup preparation. The label stayed at “Loading cross-encoder
reranker…” during subsequent wikilink work; a separate graph-preparation stage
now corrects that misleading state. CUA observations temporarily timed out, then
recovered. Short samples include native event waits and one interval of worker
GC/GIL contention; they do not establish the cause of CUA timeouts or the earlier
unexpected exit. No performance or stall-resolution claim follows from the label fix.

Ask's stale waiting text came from retaining transient info frames as conversation
nodes. Terminal cleanup now removes those nodes and rejects late callbacks. Read
previously used plain `textContent`; it now shares sanitized Markdown rendering
while retaining raw copy/export text. Focused frontend tests cover completion,
error, Stop, late callbacks and reset. LaTeX typesetting remains unsupported.
Native visual verification remains separate from these tests.

Read now rejects empty online responses and empty intermediate map notes, while
preserving billed usage before the terminal SSE error. A replay through the real
DeepSeek adapter with a fixture response verifies one request, a truncation warning
and retained nonzero cost. It does not establish the contents of the historical
remote response or whether reasoning exhausted its token cap.

The retained published-review extraction includes explicit limitations before the
bibliography cutoff. Current map/reduce propagates the focus question, audience,
language and generation controls. Historical intermediate outputs were not retained
in the reviewed evidence, so the cause of the incomplete E2B summary remains
unresolved. These plumbing checks do not certify scientific quality.

The real source-native field pass at revision `8fe46a34` observed a completed
local Ask with a 0 ms usage duration. The local tracker started only when draining
the final response generator, after potentially expensive retrieval and eager
synthesis. Timing now begins at manager entry; focused tests cover both query
paths, stream failure and cancellation. Native verification is pending.

Create's corpus-context interruption during overlapping indexing is an expected
safety boundary. Preflight/recovery wording now explains interruption and retained
checkpoints. The historical checkpoint and completed index metadata support a
changed corpus, but no at-failure context snapshot identifies the exact compared
field. Do not disable the guard or promise checkpoint compatibility.

The later unexpected native exit remains unresolved: process/listener absence and
launch exit code 0 establish exit, not a crash mechanism. The retained log does not
establish memory pressure, GC/GIL failure or an application-versus-external cause.
No lifecycle correction is claimed. The CUA capture timeout followed by successful
fresh capture is separate instrumentation evidence. Fifteen failure/redundant
screenshots were deliberately deleted; retained non-image evidence remains valid.

## Bounded native follow-up (28 October 2026)

An isolated source-native WKWebView trial overlapped synthetic Read generation
with a real disposable Library scan (5,000 BibTeX records, 5,000 notes and 128
byte-identical small PDFs). The scan completed; loopback observations and renderer
heartbeats continued. This did **not** reproduce the historical 12B stall, model
memory pressure, a real Zotero database or a loaded index. No GC/GIL cause or
stall resolution is established. Scientific answer quality is excluded from this
acceptance and the public-readiness decision.

New Paper during an active stream demonstrably left disabled “Generating…” and
Stop controls visible after Activity became idle: invalidation correctly prevents
the old finally handler from cleaning the pane. Reset now owns that control
cleanup. The regression failed before the fix, passed afterward, and native
WKWebView confirmed the cleared state still holds after late completion.

Actual native PDF upload, Create folder selection, Library PDF subfolder/BibTeX
selection and the Markdown glossary picker were exercised on disposable paths.
Library selections stayed unsaved and were cancelled. Ask without an index,
Create generation/export, full native timeout/focus coverage, SQLite/storage
pickers, VoiceOver and installation on another Mac remain unverified or partial.
See `docs/native_read_library_trial_2026-10-28.md` for scope, evidence and limits.

## Public stabilization follow-up (28 October 2026)

Read now registers extraction and generation in Activity, including stage frames,
completion, failure/partial output and client cancellation. Repeated generation
requests are ignored while a stream owns the pane. Reset invalidates that owner
before late callbacks/finally handlers can re-enable a cleared paper. Activity and
Read output remain browser-session evidence; no durable Read reconnect/resume or
immediate remote-provider cancellation is implied.

The three extractionRecovery browser failures were reproduced: observation/preview
labels had changed; polling erased the preview-timeout message; immediate status
reconciliation erased the uncertain-start warning. Labels now match the workflow;
the latter two warnings stay separately beside current status while the view is
mounted. No launch replay was added.

Native Browse now covers Library relative paths and Markdown glossaries as well
as absolute SQLite/storage/archive paths. Resolved containment and suffix checks
run before returning a selection, including symlink escapes. Picking changes no
library content. Save Library settings before choosing PDFs under a changed
attachments folder. Internal data overrides remain startup-only and do not move data.

The native Read + Library stall remains unresolved. Source inspection finds Read
PDF extraction already isolated in a timeout-bounded spawn child, generation in
an SSE worker and Audit in its own thread. Explicit full GC calls found in the
index lifecycle do not establish that the historical Library scan invoked them.
No causal memory/GIL diagnosis or native fix is claimed. The small disposable
worker test and Chromium tests cannot reproduce model-scale native pressure.
WKWebView, VoiceOver and installation on another Mac remain separate acceptance.
Scientific answer quality is outside this stabilization/public-readiness decision;
the historical scientific observations below are retained as evidence only.

## Release acceptance follow-up (27–28 October 2026)

The historical acceptance transcript is retained in Git history at `13b78016` (formerly `docs/real_data_acceptance_2026-10-27.md`). The open findings below remain relevant; cleaning historical files does not certify deployment.


| Issue | Observation and impact | State / required follow-up |
|---|---|---|
| Native runtime stall | Initial 12B Read plus Library scan made both native UI and later loopback status unresponsive; TERM did not stop the process. Samples showed GC holding/contending for the GIL. | Unresolved. Exact trigger unproved; memory/model size is a hypothesis. Reproduce safely, bound expensive work and verify responsiveness/cancellation on the target Mac. E2B sequential success does not establish a fix. |
| Incorrect scientific attribution in Ask | Answer called 78.1% of trials planned or completed; the reference says prospectively registered. Requested numerators, eligibility, comparators and outcomes were also inadequately answered. | Scientific acceptance failed for the tested local configuration. Resolve grounding/answer quality and rerun factual, false-premise and evidence-gap cases before relying on this workflow. No causal model-versus-retrieval diagnosis established. |
| Paper-specific question used mixed sources | Ten chunks included the intended article, a thesis and proof.pdf despite explicitly naming the paper and DOI. | Observed scope limitation; not proof of a dispatcher defect. Provide or verify source restriction and clear provenance; retest attribution. Private sources also prevent using this answer view as a public screenshot. |
| Zotero transport mismatch caused partial apply | Finder tags applied first, then Zotero refused before write because auto selected Web for Desktop-bound identities without a local grant. | Data safety guard worked; batch recovered after explicit local authorization. Improve preflight/authorization before starting multi-destination writes, retain partial outcomes, and verify one-use grants. No Web write was sent. |
| Read missing Activity tracking | During E2B Read generation, Generating/Stop and part progress were visible while global Activity said No active operations. Source summarizer has no task lifecycle registration. | Source and Chromium fixture coverage added on 28 October; native acceptance remains open. |
| Search update mislabeled as preparation | Confirmed update start line said Starting the confirmed preparation. | Fixed during the historical acceptance session with two-mode regression; not yet retested in the existing native window. |
| Restarted Activity task reused old outcome | A fresh manual scan displayed terminal detail/counts from its previous run under Running. | Fixed during the historical acceptance session with restart/in-flight regression; native reload acceptance outstanding. |
| Read answer insufficient for requested appraisal | E2B two-part summarization finished. Visible result was a short generic paragraph: design, dissemination analysis and two limitations; it did not explain Table 3, provide requested numerical distinctions or page/table citations, or explicitly answer all efficacy/harms/follow-up questions. | Completion is verified, scientific acceptance is not. Check summary controls/focus handling and assess a complete exported result; do not infer root cause or certify numerical fidelity from completion. |
| Unusable screenshot deliverable | User rejected the native captures as horrible. No public-ready screenshot set or README update was delivered. | Captures deleted as requested. Any future public assets need a new visual pass and privacy review. |



## 27 October staged Library final review

Explicit preparation intent, recovery guards, authoritative active monitoring,
metadata naming checks and continuation gates have focused source/fixture coverage.
The integrated fixture proves approved rename/tag/note changes reach a final index
while prepared text is reused.
No production or native acceptance was performed. In particular, real provider
behavior, WKWebView/VoiceOver, Finder/Zotero integration, index-scale performance
and production recovery remain open verification gates. Historical incident
records did not identify the initiating control; new provenance does not repair
that missing evidence. These local changes are not an installed-app deployment.

## 27 October whole-project source review

CLI/MCP malformed acknowledgements, recovery-source containment ordering and
reset cache-removal false success are repaired. Validation fixtures now exercise
current admission/uncertainty and Create navigation contracts. The public builder
rewrites omitted private-document links in its copy before manifest hashing.
These are source/fixture results; installed application, real-library/provider,
WKWebView and VoiceOver acceptance remain separate open gates.


## 27 October Library evidence and settings follow-up

Library now shows saved index evidence separately from unknown current corpus
coverage. Existing manifest/index files do not share a generation identifier;
their source count cannot establish how many documents are currently searchable.
The status read rejects observed concurrent publication and malformed evidence,
but stable file signatures do not establish a shared generation or freshness.
Current discovery/cache/pending/deferred totals are deliberately unknown until
evidence with the appropriate scope is available. Preview discovery counts cover
PDF/image sources, not every Obsidian note. No full coverage inventory or automatic
background scan was added.

Settings now links index tools and Library paths/audit settings; Reset App Data
moved into Advanced data controls. Keyboard/Chromium checks cover nested-dialog
layering, focus return and reset cancellation. This remains incremental work;
WKWebView/VoiceOver and production recovery/performance are unverified.

## 27 October follow-up — bounded evidence and task navigation

The shared memory guard's metric list previously grew across a batch and was
aliased into PDF results after their snapshot hashes were computed. Snapshots
now freeze their metric evidence; retention keeps the initial sample and latest
255, with the total sampled count disclosed. Stop baselines and thresholds are
unchanged. Disposable PDF and simulated-metric regressions cover immutable
exports and swap-growth refusal after diagnostic retention reaches its cap.

Library's all-deferred hint now applies only to text preparation. It no longer
hides index recovery, lexical preparation or completed-with-omissions outcomes.
Ask, Read, Library and Create label the existing task tabs; advanced controls
remain in their existing locations. This is an incremental navigation change,
not a complete settings reorganization or a new corpus-readiness inventory.
Counts in preparation describe that operation; complete caches do not certify
that every document is searchable in the current index.

Read-only process and bundle searches found no running CHERQAiN/PaperMind
process and no matching installed bundle in /Applications, ~/Applications,
the checkout or the searched home locations. Spotlight returned no matching
bundle ID. This does not establish parity for an installation elsewhere. The
fixture app under /private/tmp/cherqain-native-inventory is separate evidence.
No installed app was replaced or restarted. Native WKWebView/VoiceOver,
production performance, actual providers and real-library recovery remain open.


## 27 October memory pressure and daily search — source acceptance

Bounded page preparation now admits warning-level macOS pressure. Initial
critical pressure, unknown/unavailable monitoring, >512 MiB swap growth and
sustained critical pressure still stop work. A single guard spans PDF documents
and ranges in an online batch. System-wide swap is evidence, not attribution to
CHERQAiN. This policy is verified with simulated metrics and disposable PDFs;
it is not a measured guarantee of memory use on the production corpus.

Library has a direct incremental **Update library search** preview, independent
of Audit scan and names/tags/notes analysis. No is the initial online choice;
external calls still need the preview authorization. Resource checks are read-only
and never resume a job. Completed-cache/index admission and uncertain-outcome
recovery barriers remain in force. Global resource failures still end preparation;
this change does not publish partial documents or automatically continue indexing
after such a stop.

Focused offline regressions and Chromium workflow checks cover confirmation,
no implicit online calls, busy controls, keyboard order, reload recovery and
resource messages. Installed-build parity, native WKWebView/VoiceOver, actual
provider behavior and production-library performance remain unverified. No app
restart, real-library write, index update, model change or paid call accompanies
this source work. Broader navigation/readiness redesign remains subsequent work.

At that review, the repository mypy gate failed in unchanged `rag/build.py`
(51 findings against a budget of 47); shadow-file checks reproduced it. The
27 October typing follow-up resolved those missing host-member declarations:
`rag/build.py` now has zero findings and its 47-error allowance was removed.
The full ratchet passes (38 remediation files clean; 72/85 legacy findings).
The change is confined to `TYPE_CHECKING` declarations and leaves runtime
indexing, persistence, cancellation and BM25 warm-up behavior unchanged.
32 isolated regressions plus four subtests passed with outbound network and
external child processes denied; production data and providers were untouched.

## 26 October final review follow-up — isolated validation

The five final-review P2 findings have focused fixes: Markdown Apply keeps missing
or uncertain publication confirmation unknown; LaTeX generators check cancellation
and path/vault ownership between figure copies and before scaffolding; Pillow
oversized-image rejection stays source-local; slide prompts preserve author text;
and selected Index-tools buttons retain readable hover colors in both themes.
Markdown preview documentation now separates token expiry from creation-triggered
cleanup and retained recovery/malformed records.

Regressions exercise actual route workers with mocked generation and real temporary
scaffolding, real operation-lock takeover, source receipts with a mocked provider,
frontend confirmation/outcome handlers, and rendered Chromium light/dark controls.
Copied figures may remain after cancellation; filesystem units are not a global
transaction. Lost client confirmation requires output/recovery inspection.
WKWebView/VoiceOver, installed-runtime security, real providers, real-library
recovery and the full public-artifact build remain unverified. These fixes do not
restart the active application or authorize any library/index operation.

## 26 October audit remediation — source and isolated fixtures

The follow-up fixes cover promotion cancellation, transport/HTTP classification,
Markdown preview retention, activity keyboard access and terminal outcomes,
history receipt-read amplification, obsolete navigation/recovery guidance, and
the Vitest development dependency advisories (4.1.12).

Both suspected risks received isolated regressions. A simulated server-session
change with the same corpus counter reproduced acceptance of an old Markdown
preview; session binding now refuses it. A real `RagOperationLock` takeover
between cache replacements now stops the old worker before the next replacement
or indexing and prevents it from releasing the new owner's lease. Tests do not
establish atomicity against process suspension inside one filesystem operation;
replacement plus landed-receipt publication remains a durable recovery unit.

Promotion cancellation retains already-published caches and receipts. Ordinary
preview cleanup excludes attempted/uncertain applies, which remain preserved for
inspection rather than silently replayed. Existing historical failure decisions
are not rewritten by the corrected classifier; explicit reviewed retry remains
the route for previously deferred content.

Validation uses disposable storage, mocked providers, and denied network
connections. Python regression groups cover OCR, daily preparation, index/recovery,
history pagination, Markdown generation/restore, driver replay, security headers,
and public-artifact helpers. Frontend checks cover authoritative task outcomes,
keyboard focus/scrolling, Studio selectors, provider diagnostics and index history.
The browser failure from the audit was an inconsistent synthetic status fixture;
it now serves the authoritative failure instead of a competing idle response.

Native WKWebView/VoiceOver, installed-runtime dependencies, real providers,
real-library recovery and full public-artifact build remain acceptance gates.
No application restart, real scan, extraction or index operation accompanies
these source fixes. History cache still enumerates/stats receipt metadata and
reads all receipts on a cold count; no claim of constant-time cold history is made.

## 24 October UI/index integrity acceptance boundary

The seven review findings have targeted source fixes and isolated regressions.
This is not blanket phase 0–7 acceptance: realistic curation-writer integration,
all crash/cancellation timings, live-provider behavior and native accessibility
remain unverified. The preservation fixture stubs indexing and uses synthetic
schemas; byte preservation there is narrower than end-to-end Library acceptance.
The Python integrity runner's socket hook alone does not constrain executable
children; review checks additionally deny child execution and omit its xattr test.


The source worktree now implements explicit routine/rebuild admission, candidate
cache reuse, enforced deferred-binary manifests, durable run inspection and
explicit bounded recovery. The original implementation is commit `509b62dc`; subsequent review fixes were
uncommitted in the isolated worktree at that review. This is historical checkout
provenance, not the current Git status. Neither source state establishes the code
loaded by the concurrent production job. Historical 23 October “skipped”
receipts do not prove index exclusion; only runs using the new loader manifest
have that enforcement.

Native WKWebView/VoiceOver acceptance and real-library/provider acceptance remain
open. Chromium and disposable fakes do not verify those surfaces. No production
residual repairs are included. Legacy whole-file caches retain unknown provenance;
complete-range checks establish coverage, not transcription accuracy. Partial
one-file ranges remain export-only until complete-document evidence is available.
Legacy history is retained and readable, but changed schema/bindings can refuse
resume rather than silently starting a fresh paid generation. Index continuation
requires the new saved scope binding; older promotion records lack that evidence
and cannot be automatically continued. Partial promotion
requires manual evidence inspection, not automatic rollback.

Local previews report compatibility and lightweight candidate/cache counts;
external page estimates are specific to online previews. New extraction-only
sessions and one-file runs also persist evidence. Historical process-local jobs
without a run record cannot be reconstructed automatically. See the dated implementation
plan for exact automated evidence and remaining acceptance gaps.


**23 October online index switch:** source now stages run-wide online OCR and
vision results before promoting eligible caches and starting an incremental
index pass with local visual-model calls disabled. OCR and vision online model
choices are saved independently of the local baselines. This implementation
now has a separately confirmed full-rebuild choice that retains the online
no-local-visual-model mode through archive and re-embedding. Archive failure
preserves the existing index and aborts. The native UI control was observed in
an isolated source launch on 23 October; full native and production acceptance
remain open. This implementation
has passed disposable route/loader and focused regression checks, but no real
provider, production vault, or native WKWebView run. A source with incomplete
text blocks promotion; a new confirmed run may retry within the existing
attempt allowance. Each newly confirmed online index run starts a distinct
attempt generation, so exhausted attempts from an earlier run remain as evidence
but do not prevent a new call. Long PDFs are split into bounded extraction ranges;
native PDF pages with embedded images retain their text and a visible warning
that those images were not described. Complete transcription/visual accuracy
is not established. The existing
embedding identity must match; a different model or backend requires a
separately confirmed full rebuild.

**22 October optional online OCR implementation:** explicit one-file and
preview-bound vault-session extraction is available in source for DeepSeek,
OpenAI, Gemini and OpenRouter image-input models. Complete results can be
selected for an incremental index update, with cache before-images retained.
No live provider call, production extraction, production index update, native
WKWebView acceptance, or full-document OCR completeness assessment was run for
this implementation. Mocked driver and disposable-source checks do not establish
provider-side acceptance or real-page coverage. Failed, output-limited and
interrupted evidence remains visible; review the original source and promotion
receipt after any partial run.

**22 October scope reset, authorized by the user:** the indexing/extraction/embedding
upgrade plan is abandoned. Preserve the existing index and model; no reindexing.
The dated plans, benchmarks and native trial proposals below are historical
evidence, not a work queue or prerequisites for routine maintenance. The current
priority is checking the installed application and fixing observed failures.
Direct pushes to `main` are supported; full CI is manual and PRs are optional.

**22 October installed-app check after scope reset:** `/Applications/ChatEKLD_2026-10-07.app`
started with the existing production index (501,571 passages), reached `Vault is ready`,
and displayed healthy provider/vector-store/local-model indicators. One local RAG
request through that running app's API (agent disabled for this request only)
completed with an answer and ten retrieved passages in 144 seconds. The first
source, `corpus_2026b.pdf`, was revealed through the app's source endpoint and
opened in PDF Expert. This verifies a bounded working path, not overall answer
accuracy or acceptable latency. Query expansion, thesaurus expansion and reranking
were left as configured; the slow first response remains an observed issue.
No indexing endpoint, reindex, model change, build/install, scan or paid provider
call was performed. Configuration SHA-256 was unchanged. The 29 pre-existing
index/RAG files retained their sizes and nanosecond mtimes; startup added only
`obsidian_storage/wikilink_sidecar.json`. This is metadata comparison, not a
whole-index byte-hash proof. No broader test campaign was run.

**22 October native build follow-up:** the isolated locked build and bundled
Pillow-13 checks passed, but native smoke is incomplete. UI automation relaunched
the trial without its isolation environment, loaded production configuration and
began prewarm. The unexpected process was stopped; production effects cannot be
fully certified. Temporary launch paths are disabled. Require a fail-closed
in-bundle isolation gate before another automated native trial.

**22 October native preparation:** an opt-in launch environment flag now skips
provider startup/model auto-loading and provider shutdown for isolated native
trials. Hermetic lifecycle and existing ordinary-note graph/thesaurus checks
pass; this is not native acceptance or note-relevance qualification. Retained
evidence receipts verify unchanged. Realistic scale and ordinary-note quality
remain open; no new PDF benchmark or production profile selection was made.

**22 October completion follow-up:** canonical Markdown snapshots cover 7/7 retained indexed PDFs; production export is not authorized. The corrected application-adapter benchmark completes all three profiles, but CPU reranking misses latency targets and no-reranker quality/full-scale gates remain open. No Docling migration or production activation is recommended. The original audit had 24 unwaived Pillow findings; subsequent authorized Pillow 13 remediation clears both source lockfile audits, while the installed runtime remains unchanged.

Abandoned implementation plan (historical): practical retrieval upgrade for the 16 GB Mac (`docs/indexing_upgrade_plan_2026-10-19.md`).

**Historical 20 October scope reassessment (abandoned 22 October):** useful text/table
retrieval improvement replaces the full visual-fidelity release target. VLM model
comparison and custom graph/color reconstruction are deferred; no hybrid-architecture
approval is needed to proceed with this release. Original gold and failed reports
remain unchanged. Their exact-match, zero-withheld-region and 95% fully interpreted
page targets are historical diagnostics, not the current shipping contract.
Production remains unchanged. The next work is an isolated paired retrieval
comparison using the production chunker/current embeddings, then lightweight batch
integration, recovery and native acceptance. Missing figure semantics are disclosed;
scientific corrections, silent operational content loss and citation/index corruption
remain unacceptable. The full plan defines the revised tradeoffs and boundaries.

**Historical conditional embedding scope (withdrawn 22 October):** the earlier request permitted a new local embedding model
if a bounded comparison demonstrates useful retrieval gain on the 16 GB machine.
Current config/index metadata both name EmbeddingGemma 300M through LM Studio;
exact runtime file mapping, dimension and formatting still need verification.
Compare extraction first, then models on identical frozen chunks. No new download
or activation is authorized; a switch requires a separate full-corpus vector rebuild.

**Readable conversion/export remains required:** production PDF text/cache feeding
indexing and the separate cache-only Markdown exporter exist in source. Candidate
page-marked renditions are inactive. Improved real PDF → readable artifact → actual
chunk payload parity and installed/native workflow acceptance remain open; the
historical plan and handoff required them; that abandoned plan grants no authority to resume.

**20 October practical qualification:** the isolated automatic conversion/export and
production-chunker comparison now has real five-PDF evidence, followed by two held-out
sources. Opt-in canonical cache-reader integration preserves legacy routing without
an activation record. Dense, hybrid and actual engine/LanceDB CPU-reranker results
are reported separately; dense's initial recall-gain threshold was not met, held-out
hybrid recall was unchanged, and automatic-device reranking exceeded the swap bound.
CPU reranking was resource-bounded but slow.
Production stays unchanged. A fresh production writer boundary/backup, live generation
preparation/cutover and installed/native acceptance remain open; app build was
explicitly deferred by the user. No alternative embedding model is selected.

### Historical extraction investigations (original stricter contract)

The saved-artifact intake and checkpointed Docling runner
are implemented but inactive; they cannot approve or index OCR.
The latest word-boundary review
resolves the Roberts p.2 boundary finding with explicit source-bound replacements
and retained before-images. Development remains 160/168, zero withheld regions
and zero indexable pages. All four complete-document pages replay with zero
converter calls. The development gate passes to additional complete-document
trials. The complete AuthorA resolution
now passes its review-assisted gate: 6/6 pages, zero withheld regions and zero
indexable pages. The French AuthorB control
also passes review-assisted qualification on 12/12 pages, with zero withheld
regions. The AuthorC complete control
initially completed 9/9 pages but failed table/figure fidelity and furniture
ordering, with five withheld regions and zero indexable pages. Wider-cohort,
automatic extraction, index and migration gates remain open.

The targeted AuthorC resolution
now repairs its four named findings in source-bound reviews of pages 5 and 7,
with zero withheld raster regions and no indexing authority. A fresh unreviewed
replay remains 154/168. Vector graphs and colored matrices lack a visual-semantic
coverage contract; some extra labels originate in the nonvisible native text
layer. Manual fixes cannot qualify new documents. The user authorized bounded
local visual investigation (option 1). The existing-runtime pilot
rejects both installed Gemma 4 variants: graph relations and matrix colors remain
unreliable after runtime repair, reasoning and crop trials. Source-vector geometry
is narrow corroboration only. The approved Qwen3-VL comparator
also fails: graph 14/22, then 16/22 at doubled resolution; matrix colors 15/21,
then 3/21 with row crops. A bounded source-geometry/rendered-pixel architecture
was proposed for decision, then deferred by the subsequent practical-scope
reassessment above; no production route changed.

Historical availability score: separate figure OCR comparison scores 155/168 with unchanged Docling tables/layout. The n=72 check is recovered; conflicting OCR and eight exact-match baseline regressions were recorded. Current adjudication is summarized above.

Earlier experiment (superseded by the separate figure comparison): n=72 OCR investigation. The boxes recover at 120 dpi with upright orientation, but the repeated screen falls to 149/168 and loses chart numbers. No-go remains; production unchanged.


- **[2026-10-04] Library Audit PDF/tag remediation:** code now rejects uncorroborated
  note/filename associations after a successful Zotero observation, respects exact
  PDF paths and extension case, exposes Desktop tag authorization, and retains
  uncertain tag-write journals. Regression coverage uses disposable data. Native
  Zotero 10.0.2 and WKWebView/VoiceOver acceptance is still required; no installed
  bundle or real vault note/PDF/mapping has been repaired by this change. Explicit
  Web tag selection remains unavailable for Desktop scan identities; the UI offers
  a confirmed switch to local rather than bypassing identity checks.

Extracted on 2026-10-01. Dated evidence below is retained, not revalidated wholesale. Closed decisions remain distinguished from unresolved work. CI remediation is recorded in `docs/ci_security_dispositions.md`; the current workflow and shared scope script are authoritative.

## Known Issues / Deferred Work

> **This section is the single record of deferred work.** A deferral recorded only in a subtree `CLAUDE.md`, a README, the CHANGELOG or a code comment gets lost — the 2026-07-29/30 sweep found eight such items (they carry a **[2026-07-30]** stamp below) and two pointers that claimed something lived here when it didn't. When you defer something, add it HERE and cross-reference from the detail doc, never the other way round.
>
> **Plan docs are deleted after shipping.** The 2026-07-30 fix-plan set and the 2026-08-05 feature-plan set lived under `docs/` while active; both were deleted 2026-08-10 (maintainer request) after verifying every still-open item in their trackers was already mirrored in THIS section — recover them from git history if ever needed (last commit carrying them: `5991666`). Final tracker status at deletion: fix-plans 1–5, 6.2, 7.2, 7.5, 8 (5/6) and part of 9 complete; feature-plans all six complete. Deferred work lives here, never in a plan doc.
>
> **Organised by SUBSYSTEM since 2026-09-03, not by review date.** Twenty date-keyed subsections were a chronology, not a registry: answering "what is open in tagging?" meant reading all 397 lines of it, and the same finding appeared in three places. Every entry keeps its own **[date]** stamp — that is the review it came from, and `CHANGELOG.md` is where what those reviews FIXED lives — and duplicates have been merged into ONE live entry. Two sections at the end are deliberately not deferrals: *Measured and declined* (each carries the number that settled it — do not reopen without a fresh measurement) and *Closed, kept for the lesson*. Severity is not uniform: most of this is low priority, but a few entries are real data-safety leads and say so.

### Release verification & accessibility

- **[2026-10-20] Historical full-fidelity qualification remains unmet.**
  The later user-approved practical scope supersedes this as a release gate;
  the entire upgrade plan was subsequently abandoned on 22 October. The observations below remain historical evidence, not active release gates.
  The three structural regressions and Roberts p.2 word-boundary defects are
  resolved in explicit visual review records. Original text and prior decisions
  remain preserved; no automatic layout/word repair is qualified. Development
  passes to additional complete-document trials. Explicit complete-region
  transcription and source-span ordering resolve the AuthorA figure/boundary/
  page 6 reference interruption findings. All 6 pages replay, with no withheld
  regions and no index authority. Printed source discrepancies are preserved,
  not scientifically reconciled. French AuthorB also passes review-assisted
  qualification on all 12 pages. AuthorC initially failed its complete-document gate. Its four named findings
  are now repaired in explicit reviews, but vector-figure coverage and automatic
  interpretation remain unqualified; see the targeted resolution linked above.
  The former plan required a visual-scope decision before broader controls;
  the later, now-abandoned plan prioritized paired retrieval evaluation.

- **[2026-10-20] Bounded Docling screen fails numerical-content non-regression.**
  Isolated Docling 2.140.0 with Heron/TableFormer accurate and pinned bilingual
  Tesseract scores 154/168 selected content checks versus 132/168 for the current
  candidate, after one complete-content projection revision. Numerical table
  structure improves, but `corpus_2023.pdf` physical p. 35 loses the previously
  recovered Publications included n=72 flowchart box. Processing SUCCESS does
  not establish completeness. This failed the former full-content gate; the
  revised plan retains the numerical finding as a diagnostic and permits
  bounded retrieval evaluation with explicit visual limitations. Production
  activation is still unapproved.

- **[2026-10-19] Extraction/embedding migration writer boundary and quality gates.**
  A controlled real backup was captured after stopping 11 MCP processes, with
  the cooperating server lease held, before/copy/after hashes matched, and a
  disposable restore verified. The first attempt correctly failed because the
  verifier itself created SQLite sidecars on the live source; checks now use
  disposable DB copies and the sidecars were retained in the successful backup.
  The private old generation remains. Arbitrary direct-file writers and older
  unregistered processes are not blocked by this lease; repeat the process and
  source-state review before any future capture or cutover. The 12-PDF/36-page
  pilot is range-limited, with 11 partial outputs without OCR. Six local English
  Tesseract calls completed two small samples but do not establish French or
  numeric transcription accuracy. A representative full-document comparison,
  OCR selection, embedding evaluation, and migration specification remain open.
  A later inactive, isolated local-Vision pilot processed four complete
  development PDFs (255 physical pages) with explicit partial outcomes. The
  mixed-page route now keeps cropped image-region OCR separate from native text;
  it does not certify image content. Word-position geometry proposes the four
  visually seeded cells in `corpus_2026a.pdf` Table 1 while retaining PyMuPDF's
  flattened originals. `authorb_2012.pdf` page 2 still yields four false table
  candidates; `lines_strict` also drops the real `corpus_2026a` table, so automatic
  table acceptance remains blocked. The frozen 39-document comparison and all
  local embedding/retrieval quality evaluation remain unexecuted.
  **20 October continuation:** the inactive v4 geometry recipe expands font
  ligatures consistently with PyMuPDF table extraction, repairing three selected
  Table 3 cells without changing original rows. Rendered gold now has 100 checks
  on 17 pages of seven documents. Source-specific anchors confirm column-order
  failures on Roberts pp. 1–2, Mohr pp. 1–2 and AuthorB p. 2. WHO p. 10 OCR
  omits a sentence and a closing parenthesis despite processing completion.
  Mohr contains annotations and a preceding article fragment, so it is not a
  clean control. No OCR profile is qualified by these findings; see
  `docs/extraction_quality_evidence_2026-10-20.json` and the pilot review.
  **Later ordering continuation:** `xy-cut-blocks-v2` repairs the five selected
  column-order checks and the newly reviewed Roberts three-column reference
  sequence. It preserves block text/coordinates and reconstructible original
  order; overlapping AuthorB fragments and parser-merged columns remain unsolved.
  Gold now contains 125 checks on 23 pages of ten development documents.
  Broader review exposes an AuthorA equals-sign encoding failure, fused AuthorC
  words and AuthorD hyphen spacing. AuthorA is English; the first two pages of
  the AuthorD supplement-named PDF are the main article. No extraction profile
  is qualified. See `docs/extraction_order_evidence_2026-10-20.json`.
  **Line/span diagnostic continuation:** a pure inspection helper retains raw
  blocks/spans and proposes line order separately. On four freshly rendered
  development pages, AuthorC's fused words and AuthorD's hyphen spacing already
  occur inside individual spans. Plain line-level XY cuts repair `Celle-ci` but
  break other AuthorB word sequences, including a heading. Do not activate this
  proposal or infer spaces/symbols. Gold now has 126 checks on the same 23 pages;
  no candidate recipe changed. Character geometry, baseline grouping and broader
  development review remain open; see
  `docs/extraction_line_span_evidence_2026-10-20.json`.
  **Baseline/character diagnostic continuation:** explicit 0.01-point baseline
  and 5-point fragment-gap bounds match all six selected AuthorB checks; widening
  the gap to 10 points breaks AuthorD column order. Mixed-baseline horizontal
  lines remain indivisible singletons; rotated evidence is retained separately.
  AuthorC has positive interword gaps without space characters. AuthorD's
  hyphen-following space is parser-marked non-synthetic and overlaps the next
  character; `TEXT_INHIBIT_SPACES` does not remove it. No spacing policy or
  extraction recipe is accepted. Gold remains 126 selected checks; see
  `docs/extraction_baseline_character_evidence_2026-10-20.json`.
  **Latest layout-region continuation:** manual nonoverlapping region hypotheses
  avoid the selected AuthorD narrow-gutter regression but do not qualify
  automatic layout classification. Geometric row serialization loses all three
  tested multiline table phrases on `corpus_2026a` p. 11; cell segmentation is still
  required. Roberts p. 2 also has fused words inside a span despite its prior
  selected checks passing. Gold now has **135 checks on 25 pages of ten
  documents**, including actual AuthorD supplement p. 20 and French warning
  text on `corpus_2023` p. 2. No spacing policy, parser/OCR profile or replacement
  recipe is qualified. Prior selected counts are historical. See
  `docs/extraction_layout_region_evidence_2026-10-20.json`.
  **Later offline spacing continuation:** hash-verified saved geometry supports
  a pure character-index hypothesis diagnostic, not a production spacing repair.
  Low gap/font thresholds split correctly extracted words and letter-spaced
  headers; four new controls bring gold to **139 checks on the same 25 pages**.
  Higher tested thresholds repair two selected fused-word anchors but still
  require wider boundary review. No threshold is selected. New MCP writers were
  left running during the offline experiment; the user later authorized stopping
  the three named servers. No fresh source boundary or real-PDF operation is
  established by this offline result. See
  `docs/extraction_spacing_hypothesis_evidence_2026-10-20.json`.
  **Resumed table-grid continuation:** gold now has **163 checks / 30 pages /
  eleven development documents**. Pure parser-grid hypotheses recover selected
  `corpus_2026a` multiline cells; AuthorD retains a crossing line and ligature mismatch.
  AuthorC/Papola visible two-column tables remain undetected; text strategy
  creates broad pseudo-tables. AuthorB false detections remain unqualified.
  WHO p. 11 OCR is pending. No extraction profile or recipe changed. See
  `docs/extraction_table_grid_evidence_2026-10-20.json`.
  **Approved OCR comparison:** isolated pinned English/French data improves
  selected French/scan checks at 300dpi but does not qualify a profile. Vision
  omits WHO lines in repeat runs; sparse-text segmentation helps risk labels but
  regresses PRISMA checks. Figures remain association-unverified; gold unchanged.
  See `docs/extraction_ocr_comparison_evidence_2026-10-20.json` for raw outputs,
  pinned artifacts, resource measurements and boundary evidence.
  **Rule-strip continuation:** a separate native-line/vector diagnostic recovers
  nine selected two-column cells on AuthorC/Papola at tested gaps 2/5/10 points,
  but gap 20 merges a reviewed AuthorC label/definition. Fragmented rules and
  filled rectangle borders prevent equivalent `corpus_2026a`/AuthorD reconstruction;
  AuthorB underlines create two empty strips. No table classifier or routing
  policy is qualified. Magnified Papola pixels exposed one incorrect gold
  transcription, explicitly corrected without rewriting historical scores.
  Five new multiline controls bring gold to **168 checks / 30 pages / eleven
  documents**. See `docs/extraction_rule_strip_evidence_2026-10-20.json`.
  **Fragment/rectangle continuation:** saved-evidence unions recover AuthorD
  4/8 selected cells and, with positive gap bridges, `corpus_2026a` p. 11 3/3.
  `corpus_2026a` p. 3 remains 0/16 because strip-local whitespace cannot represent
  empty columns. AuthorD merges neighboring cells and absorbs its crossing
  phrase into a merged region; this is not a repair. Grid/strip membership
  disagreement remains explicit, with no automatic winner or selected tolerance.
  Gold and extraction recipes are unchanged. No fresh source/backup boundary
  was established; see `docs/extraction_fragment_evidence_2026-10-20.json`.



- **[2026-08-25]** **The manual Safari/WebView + VoiceOver matrix is still required before a build is distributed.** Phase 4D was recorded as "completed (automated)" but its own evidence describes a one-off manual session in a non-WKWebView browser — nothing in the repo could re-run it, and the `object-src 'none'` PDF regression sat inside its scope and was not caught. `tests/browser/` (Playwright + Chromium, in CI since 2026-08-25) now covers the enforced CSP, blob-PDF rendering, document reflow at 960/1024/1280/1600 px and axe serious/critical — automatically, on every push, and **per TAB PANEL since 2026-09-03**: the shell-only run correctly ignores a hidden panel and therefore never audited one, which hid an unlabelled file input (CRITICAL) and an unfocusable scroll region (SERIOUS) for as long as it existed. **Chromium is not WKWebView and automation cannot judge announcement quality**, so the human pass stands: Maintenance status/options/log, accession partial-error flow, the bibliography combobox (0/1/20+ results, Escape/Tab/outside-focus), audit tables and the Tagging deck, under VoiceOver and keyboard-only. **Owner:** maintainer + accessibility reviewer. **Review date:** before the next build is distributed (no later than 2026-09-07).
- **[2026-08-25]** **The CSP carries `'unsafe-hashes'` plus one hash per inline handler, because `templates/index.html` still uses inline `on*=` attributes.** `'unsafe-hashes'` is materially weaker than a nonce: it authorises those exact handler strings *as event handlers on any element*, so injected markup carrying `onclick="deckGenerate()"` would execute. The header is no longer sent on non-document responses (2026-08-25), which removes the bandwidth cost but not the weakness. **Scoped and costed 2026-09-03 (measure, do not trust these figures — re-run the greps):** 113 handlers — 102 `click`, 5 `change`, 4 `input`, 2 `keydown` — over 65 distinct callees, and `app.js` carries 65 `window.X =` exports whose ONLY purpose is to make them reachable from markup. **104 of the 113 are mechanically convertible** (a zero-argument or string-literal-argument call), and three delegates would retire 42 of those on their own: `selectAuditReport('name')` ×18 over `.audit-report-tab[data-report]` (the attribute already exists), `showTab` ×10 over `.tab[aria-controls]`, and `openModal`/`closeModal` ×14 over a `[data-modal-open]`/`[data-modal-close]` pair. **Nine need hand-written bodies:** two Enter-key guards (`vault`/`plainchat` inputs), two `this`-passing export buttons, one multi-statement focus-and-scroll, and four slider mirrors reading `this.value`. Order of work: the three delegates, then the ~62 remaining id-keyed bindings in one `app.js::wireStaticHandlers()`, then the nine, then drop `'unsafe-hashes'` from `api/security.py::build_content_security_policy` — the ONE inline `<script>` (the pre-paint theme bootstrap) keeps its own hash, which needs no such directive. Two ratchets to add with it, because the risk is silent regression: a test that `templates/index.html` contains no `on*=` attribute, and one that the served policy no longer contains `'unsafe-hashes'`. Not started: it is mechanical but touches 1,746 lines of template, and the CSP is exactly the surface where a partial migration is worse than none.
- **[2026-08-25]** **A CSP that is safe in Chromium can silently blank PDF previews in WKWebView.** The engines refuse an inherited blob-document policy for different reasons — Chromium on `object-src`, loudly; WebKit on `frame-ancestors`, with no violation event and a blank frame. PyWebView ships WKWebView; the Playwright matrix runs Chromium, so **CI structurally cannot catch the WebKit class**. `tests/test_security_hardening.py` pins the header shape with the measurement recorded beside it, and any future CSP tightening must be re-measured in a real WKWebView window. Probe pattern, learned the hard way: the probe's script must be EXTERNAL (`script-src 'self'`), because an inline one is itself blocked and the run then measures nothing while looking like a clean pass.

### UI / UX

- **[2026-09-20]** **The `attachments:`-relative link disclosure rides `_lastSummary`.** `GET /api/audit/summary` is awaited before the report renders, so it is normally there — but a failed summary fetch silently omits the sentence rather than saying the count is unknown. Every other exclusion count on that tab comes from the report payload itself; this one does not, because the scan counts relative links during the inventory flatten and the attachments report is built from a different object.

- **[2026-09-03]** **The 503 from the streaming cap is handled adequately by every client, and was left alone.** Checked because the cap is new: `api.js::consumeSSE` surfaces a non-200's `error` body through `onError(err, result)`, so all 20 call sites already show the server's own sentence ("Too many streaming operations are already running. Wait for one to finish (or cancel it) and try again."), the chat panels roll the un-answered turn back and preserve the draft, and deck/docgen additionally offer a Retry boundary. The only residual is cosmetic: the deck and refactor activity logs prefix it "ERROR:", which overstates a capacity condition. Not worth a machine-readable `code` on the frame until something needs to branch on it.
- **[2026-09-03]** **The Library Audit Settings modal will not benefit from a filter.** Re-assessed while acting on the "three settings surfaces" complaint: it holds **10** controls and no nested `<details>` — a short flat form, not a wall. The gap was discoverability, and the answer went where a user actually looks: the LLM Settings filter's zero-result state now names the other two surfaces. Do not add a second filter without re-counting.
- **[2026-09-03]** **Two mono conventions existed; one was an undefined token.** Fixed (`--font-mono` is now declared once, `--mono` retired, and the command palette's scrim repointed to `--overlay-scrim`), recorded because the *class* recurs: a `var(--x, fallback)` whose token is never declared renders plausibly and silently wrong. `tests/js/cssTokens.test.js` is now the ratchet — it fails on any such use, fallback or not.
- **[2026-08-18]** **The sidebar floors at 210px and never collapses** (no breakpoint below 820px) — at a 700px window the chrome takes ~30% of width. Cramped, not broken; a collapse control is a deliberate layout change, not a drive-by.
- **[2026-08-18]** **Deck/docgen tabs remain nested-scroll walls** (2–3 `.card`s split the pane height, each `.deck-body` scrolling internally). The review's candidates — `<details>`-collapsing secondary cards, or the audit/refactor single-scroll-container model — both change tab layout wholesale; do it deliberately with visual QA.
- **[2026-08-18]** **The tagging inventory has no skeleton state.** Its three staged fetches have their own staged loading line, and a skeleton there would need to know the row count before the inventory arrives, which it does not. (The audit report body got one on 2026-08-19 — see *Closed*.)
- **[2026-08-18]** **Sticky first column (citation key) on the wide audit tables** was declined as not-cheap: sticky-left cells need background/z-index choreography verified across 16 different table layouts. Re-assessed 2026-08-19 and still declined — the tables now carry a filter that answers most of the same need. Only with visual QA, never blind.
- **[2026-08-19]** **The sort/filter primitive covers report TABLES only.** Decks (accession, ambiguous, tagging triage) are out of scope by design — a deck is one card at a time and its ordering is the report's rank, which the filter would silently reshuffle under a keyboard-driven review. If a deck ever needs narrowing, filter the table view and toggle.
- **[2026-08-19]** **A `<td>` whose full value lives only in a tooltip is searchable ONLY when it carries the `cell-title` class.** `ui.js::installTableControls` folds `td.cell-title[title]` into the filter haystack precisely because that class means "clipped for width, whole value in `title`"; any other `title` attribute is explanatory tooltip prose and is deliberately excluded (folding it in would make whole reports match "spellings"). A future truncated cell that forgets the class is silently half-searchable — use the class, or add `data-sort-value`.
- **[2026-08-19]** **The manual link picker offers only keys the LAST SCAN knows.** It reads the cached inventory, so a bib entry added since the scan cannot be picked until a rescan — the same snapshot rule every report tab follows, but worth stating because the picker looks like a live search of the bibliography.
- **[2026-08-19]** **The picker is not offered on a row the ladder already resolved to a bib key** (that row carries `[link]`). Disagreeing with a resolved key means linking and undoing, or using the deck. Small coverage gap, deliberate: two link verbs on one row is worse than one.
- **[2026-08-19]** **The picker's data comes from the full `GET /api/audit/inventory` payload** (records with `pdf_paths`/tags, not a key list). That is the price of adding no endpoint, paid once per scan; a light `GET /api/audit/keys` is the fix shape if the fetch ever bites.
- **[2026-08-13]** **Harmony's selection resets on every report render** (more conservative than the picker's snapshot-keyed lifetime, which its rows' missing `scanned_at` stamp forces); a shared selection primitive across `audit.js`/`tagging.js`/`refactor.js` is the eventual cleanup, not a defect.
- **[2026-08-19]** **The deck index is not clamped after a batch re-render**: rows resolving into bulk tiers during a batch shrink `_accDeckRows()`, and a user parked past the new tail sees "All N reviewed" with only `[review again]` — recoverable, and clamping in `_accMount` would destroy the intentional idx==total terminal state. Repro: deck view at the last card, run a batch that promotes earlier deck rows to bulk tiers.
- **[2026-08-19]** **A degraded bulk-tier resolution has no re-identify surface**: the table's `[re-identify]` renders only on `none` rows and the deck excludes bulk-tier rows, so a `candidates`+DOI resolution carrying degraded notes (a rescue source was down) can only be improved by a rescan-and-reidentify cycle. Low impact — it already has an identifier — but the verb's coverage is narrower than "any cached resolution".
- **[2026-08-19]** **A cached link whose bib entry has since vanished** flips (per `_apply_bib_check`'s fallback) to a default-checked Accessions row still labelled "identifier · in your bib" — contradictory copy on a rare path (the note field says "re-review"; the label says in-bib; the list says not-in-bib). Repro: cache a link, delete its bib entry, rescan, open the report.
- **[2026-08-30]** **`installGlobalDropGuard` is a last-resort net, not a drop target.** It
  swallows a file drop that no zone handled, because an unhandled drop
  navigates the PyWebView window to the file and replaces the app with a PDF
  viewer. It does NOT make the rest of the window droppable, and it can only
  act on an event a real zone did not already `preventDefault` — so a NEW drop
  zone must still prevent its own `dragover`, and one that forgets will silently
  get the guard's "drop it on the chat panel" announcement instead of its own
  behaviour. **jsdom has no layout and Chromium is not WKWebView**, so the
  guard's real-window behaviour is manual-QA territory like the rest of the
  CSP/blob class.
- **[2026-08-30]** **The diff's 1,500-block cap has no "show more".** Past it the reader is told
  the count and pointed at the file. A paging control would need the fold state
  to survive, which the current one-shot render does not model.
- **[2026-08-30]** **`_noteSyncUpdate` now costs a round-trip before its confirm strip appears.**
  That is the price of the confirm text being true. If it ever reads as
  sluggish, the fix is a pending state on the button, NOT reverting to the
  rendered row's flag — that flag is what let the user approve "nothing you
  wrote is at risk" for a note that had since gained their own words.
- **[2026-08-30]** **555 notes moved from "in step" to "annotated"** with the hardened
  tokenizers (historical). The converter later made the honest count 48
  (conflicts 15 → 8). Zotero-authoritative mode no longer surfaces that bucket.
- **[2026-07-22]** **UI language policy is English for chrome/status** (2026-07-22 Batch E): the French *example chips* (`app.js::wireExamplePrompts`, refactor free-prompt examples) are deliberately French — vault-domain content, not chrome. New UI strings must be English.

### Timeouts, concurrency & streaming bounds

- **[2026-10-02, integrity remediation]** Vault and Plain Chat now expose Stop
  during a response. Chromium fixtures verify the gesture, preserved partials,
  and an explicit stop-requested outcome; this closes the missing-control gate.
  Cancellation remains cooperative: closing the browser stream does not prove
  immediate termination of a blocking provider. Native WKWebView/VoiceOver and
  the configured DeepSeek agent/hybrid/reranker path remain unverified. See
  `docs/remediation_integrite_2026-10-02_preuves/frontend.md` for bounded evidence.
  The performance campaign remains closed; this is not new performance proof.

**État consolidé :** le bilan du plan performance est conservé dans le dépôt privé (`docs/performance_bilan_2026-10-02.md`), absent de l'artefact public. Les entrées datées ci-dessous sont des étapes historiques, pas des gains cumulables ni des autorisations de relance. Le chargement disque réduit la matérialisation RAM au prix d’un import froid ; aucun gain global du parcours hybride n’est établi. Le contrôle Ruff global a été rétabli en excluant uniquement le collecteur historique empreinté `docs/performance_f_preflight_evidence_2026-10-01/inspect.py` ; son contenu reste intact.

- **[2026-10-02] Essai 512 avec raisonnement désactivé : réussite technique dense.** Override par requête `reasoning_effort: "none"`, runtime hors base : 266 caractères visibles, 3 mêmes sources, zéro raisonnement, fin `stop`, égalité SDK→sortie. Chargement 37,307 s, récupération 0,269 + 0,982 s, génération 18,147 s. Usage absent ; pas de preuve de gain causal, de qualité ni de parcours hybride. Copie libérée, contrôles index/config/bases/résidents inchangés ; aucune modification des défauts de l’application. Voir `docs/performance_rag_reasoning_2026-10-02.md`. Les échecs ci-dessous restent des résultats historiques terminaux.

- **[2026-10-02] Essai unique confirmé à 512 tokens : réponse toujours vide.** Trois mêmes sources, 2 121 caractères de raisonnement, zéro contenu, fin `length`, usage absent. Le plafond supérieur ne suffit pas dans cet essai ; pas de perte SDK→sortie observée, pas de gain causal. Seuils respectés, copie libérée, contrôles inchangés, aucun retry. Voir `docs/performance_rag_512_2026-10-02.md` et son archive.

- **[2026-10-02] Essai diagnostique confirmé, PaperMind fermé : raisonnement seul et fin `length`.** Trois sources, chargement 39,260 s, récupération 0,333 + 0,891 s ; génération 27,481 s, 534 caractères de raisonnement, zéro contenu SDK/transmis. Pas de perte sur le segment instrumenté, usage absent : tokens exacts inconnus. Échec terminal de réponse visible, aucun retry ; copie libérée, index/config/checkout/résidents inchangés, seuils non franchis. Aucun succès RAG complet ou gain causal. Voir le complément de `docs/performance_rag_protocol_2026-10-02.md` et `docs/performance_rag_confirmed_evidence_2026-10-02/manifest.json`.

- **[2026-10-02] Nouveau protocole RAG préparé, non exécuté.** Worker/superviseur isolés, comparaison du contenu SDK et transmis sans texte persisté, gates mémoire/disque/deadline et preuves sur échec. Aucun nouvel essai réel autorisé ; confirmation explicite encore requise. Voir `docs/performance_rag_protocol_2026-10-02.md`.

- **[2026-10-02] Diagnostic de la réponse RAG vide, hors ligne uniquement.** Les logs moteur confirment la fin du flux mais ne donnent ni motif de fin ni usage ; l'épuisement des 128 tokens par le raisonnement reste une hypothèse. `scripts/performance_rag_diagnostics.py` corrige la perte de preuves du collecteur : snapshots atomiques sur échec/annulation, sources avant consommation, finish/usage SDK disponibles et nettoyage ; aucun texte privé, retry ou lancement réel implicite. 14 nouveaux tests, 125 ciblés passent sous `/private/tmp` ; limite du chemin temporaire dans un ancien test F documentée sans assouplir son garde. Archives terminales intactes, aucun nouvel essai réel. Voir le complément de `docs/performance_disk_docstore_2026-10-02.md`.

- **[2026-10-02] Essai réel unique après 2e93bd8c : partiel, terminal.** Après confirmation et 60 s d’observation, chargement index 39,350 s (import SQLite 36,820 s), préparation récupération 0,621 s et lecture dense 1,187 s terminent. Génération 18,070 s mais zéro contenu visible : worker en échec, aucune répétition. Pic RSS échantillonné 622 Mo, swap système +0,925 Gio (seuil +1 Gio non franchi), copie temporaire ~2,57 Go libérée. Index/config/résidents inchangés selon contrôles documentés. Pas de succès RAG complet, de gain causal ni de validation BM25/wikilinks froids ou reranking. Cause de la réponse vide non établie ; nombre de sources et métriques worker non conservés après échec. Voir le complément de `docs/performance_disk_docstore_2026-10-02.md` et `docs/performance_disk_real_evidence_2026-10-02/manifest.json`.

- **[2026-10-02] Chargement docstore réduit en mémoire, validation hors ligne.** `rag/disk_docstore.py` importe les docstores des index LanceDB existants dans une copie SQLite temporaire ; lectures ciblées et compteurs évitent la matérialisation globale, format JSON inchangé. Sur fixture de 77 Mo : pic RSS 355 → 111 Mo, chargement 0,112 → 0,449 s, ~80 Mo de disque temporaire. 5 270 tests Python complets, 51 ciblés finaux, 1 198 JS et 64 Chromium passent. Validation réelle ultérieure partielle et terminale décrite ci-dessus ; bulk APIs/caches froids et disque temporaire restent des coûts. Voir `docs/performance_disk_docstore_2026-10-02.md`.

- **[2026-10-02] Diagnostic réel borné après 5a45c7d5.** Voir `docs/performance_latency_2026-10-02.md` : trois Plain Chat sur route Flask et moteur réels terminent en 5,372 / 2,148 / 2,274 s ; événements de raisonnement avant contenu, transport moteur→SSE inférieur à 2 ms. Pas de mesure native ni gain causal établi. RAG dense arrêté au chargement du docstore sur +1,873 Gio de swap, sans reprise ; pas de mesure de récupération/génération RAG ou reranking achevée. Matérialisation JSON complète du docstore (~1,6 Go) vérifiée dans le chemin de chargement ; attribution exclusive de la pression mémoire non établie. Priorité : étudier une lecture du docstore moins coûteuse sur fixtures, sans réindexation. Preuves empreintées dans `docs/performance_latency_evidence_2026-10-02/`.

- **[2026-10-02] Réévaluation globale après d1a06b5c.** Voir `docs/performance_reassessment_2026-10-02.md` : 165 pièces vérifiées ; priorité à la provenance A–D et à une qualification F minimale avant les matrices. Distinguer observation, inférence sur résident strict et variation avec recharge ; restauration complète nécessaire avant recharge, pas prérequis universel. Identité sans JIT, effets KV autorisés et qualification d'arrêt serveur restent ouverts ; aucun gate réel du code assoupli. Correction hors ligne : StreamDriver perdait le marqueur cancelled et pouvait compter un terminal annulé comme succès ; signal désormais conservé, sans prétendre prouver l'arrêt serveur. 116 tests Python ciblés et 4 JS passent ; compilation, Bandit, mypy et Ruff ciblé passent, cinq diagnostics Ruff globaux archivés inchangés. Complément autorisé : checkout lancé sur base réelle sans réindexation, une réponse Plain Chat ALPHA terminée en 21,859 s, modèles résidents inchangés et aucun seuil mémoire franchi ; preuves dans `docs/performance_live_evidence_2026-10-02/`. Ce n=1 ne mesure aucun gain et ne qualifie ni RAG ni annulation serveur. Aucun nouveau benchmark ni scheduler.

- **[2026-10-02] Reprise E/F/G après 800a76c7.** Voir `docs/performance_continuation_2026-10-02.md` et son archive empreintée. Observation SDK autorisée : les trois prompts ont les mêmes textes rendus et IDs de tokens que les candidats archivés (42 / 2 231 / 6 631), instance à 85 760 ; seule la métadonnée lastUsedTime change dans les listes brutes. MLX 1.11.0 est chargé dans un processus, liaison directe à la référence d'instance encore ouverte. Configuration/restauration complète, transport/annulation moteur et autorisation des effets sur le cache KV restent des gates ; aucune génération F ni reprise E. Le support MLX concurrent est documenté : ne pas reprendre les anciennes conclusions MLX limité à 1 comme état actuel. Préfixes et erreurs du harnais durcis, worker bloqué testé sur fixture jetable et récupération des journaux tronqués ; 84 tests ciblés passent. Inspection GUI autorisée : contexte 85 760, concurrence affichée 4, seed et quantification KV désactivées ; panneau initial rétabli sans modifier de réglage. Template par défaut seulement partiellement lu ; candidat de restauration non exécutable dans docs/performance_f_restoration_evidence_2026-10-02/. Ruff global échoue sur cinq diagnostics du script archivé inspect.py, conservé intact ; Ruff des fichiers modifiés, compilation, Bandit et mypy passent. Aucun scheduler. Complément statique autorisé : les signaux internes distinguent configuration chargée, overrides et session serveur, et le schéma associe instance/engineInfo/PID ; aucune valeur de ces signaux lue et aucune voie publique vérifiée. Voir docs/performance_f_next_observation_2026-10-02.md ; restauration toujours bloquée.

- **[2026-10-01] Performance E/F/G — gates réels ouverts.** Harnais et fixtures synthétiques dans `scripts/performance_suite.py`, raccordements injectés et limites dans `docs/performance_connections_2026-10-01.md`, protocole et autorisations distinctes dans `docs/performance_suite_2026-10-01.md`. Le harnais initial était simulé ; les observations réelles de la reprise sont décrites ci-dessous. Aucun scheduler. Runtime LLM actif, contexte chargé, concurrence effective et restauration restent à vérifier ; la présence des backends MLX et du réglage ne prouve pas le parallélisme. Les replays ne mesurent pas les performances de bout en bout.
- **[2026-10-01] E2 réel : CPU complet, arrêt swap MPS.** Voir `docs/performance_results_2026-10-01.md` : 200 observations CPU, MPS arrêté pendant le premier warmup après +1,24 Gio de swap ; aucune comparaison appariée CPU/MPS/auto achevée. F0 sans LLM résident, politiques JIT/éviction et restauration invérifiables : F1/F2 bloqués. Aucun scheduler G, aucun changement de résidents/configuration/index.

- **[2026-10-01, checkout-only fix]** Vault and Plain Chat now consume the server-advertised SSE silence budget, renewed per event, with a 30 s browser margin. Offline fake-clock and partial-answer tests pass; installed bundle/WKWebView acceptance remains open. The server has no browser-paint measurement. See `docs/performance_2026-10-01.md`.
- **[2026-10-01, checkout-only fix]** Deck/docgen consumers now use the online-aware retry floor through `consumer_stall_s`, including compile-fix stage silence. Agent/stage and compile total deadlines are unchanged; an already-blocked provider call may still outlive a cooperative deadline. Offline route/ownership tests cover the checkout; live providers and the installed bundle remain unverified.
- **[2026-07-30]** **Tagging propose: a pre-first-token wedge can't be cancelled.** `api/routes/tagging.py::_PROPOSE_GATE` — cancellation is cooperative (polled per streamed token / between embed stages), so a local call wedged BEFORE its first token holds the single-flight gate up to the provider's own timeout after a 504, and an immediate retry 429s. Same class as the `deckgen/inprocess.py` residual and the agent-tool PDF extraction (`vault_read_note` — an extract started above the deadline floor can still overrun it, extraction is not interruptible mid-flight; `core/agent/CLAUDE.md`, promoted here 2026-08-09); a real fix needs interruptible transports.
- **[2026-07-23]** **retire-tag phase-4 re-acquire is a bounded 20×1 s retry** — if the op-lock stays busy past that (e.g. an indexing run grabbed it between the two holds), failed Zotero PATCH outcomes are not recorded and those ops remain `applied` in the journal (revert = idempotent re-add; surfaced in the terminal `failures`). Acceptable conservatism for a single-user app; revisit only if it bites.
- **[2026-08-25]** **The piecewise LaTeX hold is only half-fixed.** AP-11's `LatexPathLockRegistry` replaced the single global lock with a canonical-path registry, but the global cap is **2**, so a 12-section piecewise augment (one full agent/review turn per `\section`, 60–300 s each = 12–60 min) still starves a third operation for its whole run — `piecewise` augment and `repair-piecewise` both. Raising the cap or releasing between sections is the remaining work; the splices are computed at the end from original coordinates and the routes already re-check the base sha before staging, so release/re-acquire is viable — but restructure `_SSEOpGuard`'s release ownership only deliberately, never as a drive-by. (The *other* half of this entry — a lease with no TTL, kept until restart by a worker wedged in an unbounded `client.chat()` — was closed 2026-09-02: the lease is now epoch + TTL with `RagOperationLock`'s steal check, the stealer inherits the victim's global slot, and `run_sse_worker`'s `heartbeat=` hook sets the worker's cancel event on a refused beat so every existing pre-write `cancel.is_set()` gate covers the steal.)
- **[2026-08-19]** **A batch abort's terminal summary frame is dropped by the SSE contract**, on accession `batch` and on zotlink `trash` alike: the abort `put({"error"})` terminates the stream (any `{"error"}` frame is terminal app-wide), so the worker's following `{"accession"}` / `{"zotlink"}` summary never reaches the client — the user sees the abort message and the per-row frames already rendered, but no outcome tally. Acceptable: the contract is app-wide and the partial rows are on screen; fold the tally into the error message if it ever matters.

### LLM providers, usage & cost

- **[2026-09-23] Online API follow-up probes and price review (historical; updated 2026-10-07).** The DeepSeek, Z.AI and OpenRouter generation keys were absent; Anthropic generation returned insufficient-credit HTTP 400. Replay failure details for DeepSeek reasoning, Claude omitted thinking, OpenRouter Gemini thought signatures and Claude routing, Z.AI `clear_thinking=false`/`tool_choice=none`, and output-cap behavior on Claude and DeepSeek remain unverified live. The 2026-10-07 checkout now omits Gemini 3.x sampling and applies model-specific OpenRouter/DeepSeek/GLM-5.3 payload rules using official documentation and hermetic tests; it has not run live generation. OpenRouter's model-level `supported_parameters` is a union, not proof that the selected endpoint forwards a field. Tool and explicit-reasoning turns now set `provider.require_parameters=true`; live endpoint forwarding remains unverified. Decide separately whether to migrate direct Gemini from supported `generateContent` to Interactions (`store=true` by default). Recheck promotional GPT-5.6 Sol pricing after 2026-11-21 and Gemini 3.6/3.8 Flash pricing after 2026-12-31. The earlier scratch probe commands and evidence are in `docs/verification_2026-09-23_online_llm_api.md`.

- **Latent online-adapter streaming paths** — `resolve_chat_provider(stream=True)` skips fallback and adapters' `stream()` don't reassemble tool-call deltas; unreachable today (the agent loop always passes `stream=False`). Streaming tool-call deltas (C3b of the 2026-07-04 plan) stays deferred: high risk (the provider wire-format territory of the 07-02 P0 bugs), only on explicit demand with contract-test fixtures + live smokes per provider.
- **Provider-native web search (v2) is not built.** The shipped `web_search` tool is Tavily-backed and client-side; the providers' own server-side search (Anthropic `web_search`, Gemini grounding, OpenAI Responses) bypasses `wrap_untrusted` and needs per-adapter wire work. Full rationale at the `web_search` bullet in *Agent Mode* — this entry exists because the CHANGELOG points at Known Issues for it (2026-07-30).
- **[2026-08-29]** **Native multimodal is the recorded v2 and is NOT built.** Plain Chat
  attachments extract to text (`services/attachment_service.py`);
  `core/llm/types.py::Message.content` is still a plain `str` and no adapter
  ever sends an image. The alternative — widening it to content blocks and
  teaching each online adapter its wire format (OpenAI `image_url`, Anthropic
  `source.base64`, Gemini `inlineData`) — was weighed and declined: it is the
  same provider-wire-format territory that produced the 2026-07-02 P0 bugs, the
  mocked suite cannot see provider-side validation (`test_llm.py::TestProviderToolPayloadContract`
  is only an in-process stand-in), so it needs live smokes and fixtures per
  provider, and local providers would keep the text path as a fallback anyway.
  **Its cost, stated plainly:** images are described by the LOCAL vision model,
  so attaching one on an online chat provider still requires Ollama/LM Studio,
  and the model reads a description rather than seeing the picture — no visual
  reasoning, no reading of a figure the describer did not transcribe. Take it
  up deliberately, with per-provider fixtures, or not at all. Sibling of the
  provider-native web-search v2 entry above.
- **Dead fields** — `LLMRequest.stop` is *read* by the three online adapters but never *set* by any caller (so it never takes effect; dropping it would require deleting the adapter reads too). (`RagOperationLock._owner_thread` — the former "written but never read" dead field — was dropped in the 2026-07-01 perf pass.)
- **[2026-07-20]** **Cancelled online streams still under-report INPUT tokens.** An abandoned stream is now recorded with `error_category="cancelled"` (and `success=True` deliberately — `record()` zeroes a failure's cost, but the provider bills what it generated), and output tokens are re-estimated from the text actually streamed. On providers whose usage rides the FINAL chunk (OpenAI + the compat adapters) a cancel means no usage block ever arrived, and the prompt's input tokens are **not** fabricated — the payload is not in the generator's scope and `core/llm/usage.py`'s rule is "better to under-report than to surface a fabricated number". Anthropic (`message_start`), Gemini (per-chunk `usageMetadata`) and the local adapter (estimate fallback) are unaffected. See `CANCELLED_ERROR_CATEGORY`.
- **[2026-08-18]** **`usage_tracker.record()` zeroes cost for any `success=False` call** (`core/llm/usage.py`), so a mid-stream provider error after N streamed tokens records 0 for tokens the provider did bill. (Distinct from the cancelled-stream *input*-token entry above, which is deliberate.)
- **[2026-07-22]** **Gemini long-context pricing tier unmodeled** — `gemini-2.5-pro`'s >200k-prompt tier bills higher $/Mtok than the single-rate `PRICING_TABLE` row; large-context calls under-report. (The cached_input column itself was filled 2026-07-22.) Same family: the Anthropic **1-hour cache-write tier** (2× the 5-min write rate) is unmodeled too — inert today because the app never requests the 1-h TTL (`core/llm/usage.py` notes it); bundle both if usage accuracy ever matters (promoted here 2026-08-09 from the since-deleted plan tracker).
- **[2026-08-08]** **`_NON_CHAT_OPENAI_TOKENS` substring-filters OpenRouter's live catalog with a denylist tuned for api.openai.com** (`core/llm/adapters/openai.py::_fetch_live_models`, inherited by the compat adapters): applied to ~400 namespaced ids, the `codex` token drops OpenRouter's chat/tools-capable `openai/gpt-5*-codex` family from the live merge, so those ids never appear in the model dropdown (curated ids are unaffected; no current vendor name matches a token — checked 2026-08-08). Fix shape: scope the token match to the segment after the `vendor/` prefix, or relax the filter per-provider. UX-only, hence deferred.
- **[2026-07-30]** **Ollama think-suppression residuals (promoted 2026-08-09 — they lived under a struck CLOSED heading / in the module doc only):** the LlamaIndex `Ollama` LLM built by `get_llm` (query-expansion rewrites, local-only default-off knob) does not pass `think=False`, and neither does the vision/OCR image path (`services/vision.py::_chat_ollama_image` — its own raw client `chat` with `num_predict` caps), so a thinking-capable vision model can still burn its whole cap invisibly. Details at `core/llm/CLAUDE.md` → Ollama thinking suppression.
- **[2026-08-18]** **`stream_with_fallback` discards `StreamingResponse.final`** (`core/llm/factory.py`), so no streaming caller can read `finish_reason` — a reply the provider truncated at `max_tokens` is indistinguishable from a complete one. Ripples into every streaming consumer's contract, which is why it was not a drive-by.

### Agent loop & tools

- **Agent tool-arg validator covers scalars only** — extend `core/agent/tools.py` before adding a tool with `array`/`object` parameters.
- **[2026-08-08]** **`core/agent/vault_tools.py` (low) — REFRAMED.** The envelope fits the +512 headroom; JSON **escaping** is what overflows. `read_note` caps raw text at 32 000 chars, then `json.dumps` expands every newline/quote, so a full-length markdown note exceeds `max_output_chars` and `ToolRegistry.truncate` clips mid-string — the model receives syntactically broken JSON on essentially every full-length read.
- **[2026-08-18]** **`deckgen/inprocess.py` never gives the vault tools the turn deadline**, so `vault_read_note` can start an unbounded PDF extraction inside a bounded turn.
- **Deferred by design (from the completed 2026-07-04 plan, doc deleted — recorded here so they aren't re-scoped from scratch):** C2 unified job history (the four job systems keep purpose-built durable state; revisit only on concrete need), C3a cosmetic word-batching of the agent's single final `TokenEvent` (`core/agent/loop.py`), and retrieval-trace per-leg provenance (dense vs BM25 — destroyed by `QueryFusionRetriever` fusion, documented in the shipped B4 v1).

- **[2026-09-27]** **DeepSeek v4-pro ignores the forced-final `tool_choice="none"` and writes its tool calls as TEXT, which the agent loop then accepts as the answer.** Observed three times on the live vault (a `/api/obsidian/chat` claim check and two `generate-md` report sections): the model spends every iteration on `vault_search`, and on the last one — where `core/agent/loop.py` sends `tool_choice="none"` plus the FINAL ITERATION suffix — it emits `<｜DSML｜tool_calls>…<｜DSML｜invoke name="vault_search">…` in `content`. `finish_reason` is `STOP` and the text is non-empty, so branch 2 treats it as a final answer. On the chat route the user receives the raw markup; on docgen the Markdown heading gate empties it and `deckgen/sections.py` runs the **salvage pass, which is a FRESH single-shot RAG turn** (one retrieval, top-8 chunks) that never sees the 20+ tool results the agent gathered — so the salvaged section stated that Liu 2026, Forbes 2023 and the Linardon papers were "absent from the vault" while the same section's trace shows the agent had retrieved and READ them (all 14 keys it listed are in `indexed_materials.json`). Two fixes, neither made: (a) in the forced-final branch, treat text that parses as tool-call markup (DSML, or any adapter's textual tool syntax) as malformed rather than as an answer — re-ask once, or fall back; also covers the plain chat path; (b) hand the salvage pass the agent's `tool_history` (budgeted like `_budget_tool_history`) instead of a fresh retrieval, so a salvage can never contradict what the turn already read. Mitigation meanwhile: instruct the model in the request to stop searching after N iterations and write. `core/llm/CLAUDE.md`'s "a backend that ignores the forcing degrades to the old iteration-limit message" is therefore not the whole story: a backend can ignore it by answering WITH the calls as prose.

### Retrieval & indexing (`rag/`)

- **[2026-10-22] Practical upgrade remains no-go.** On retained copies, hybrid without reranking achieved 39 ms median / 56 ms p95, but only 21/24 development passage hits (all three misses French). CPU pools40/20 both exceeded the 512 MiB system-swap stop before completing a query. Frozen 20-question confirmation scored baseline 16/20 versus automatic Docling 14/20; one loss is sensitive to a literal hyphen anchor. No production settings/extraction were changed. No ordinary-note/full-scale or native acceptance. Partial replacement profiles now refuse; whole-generation binding/restore is disposable-only evidence. Fresh lockfile audit reports 24 Pillow 12.3.0 advisories (13.0.0 advertised fixes); no dependency update/waiver was applied.

- **[2026-09-23]** **Paper labels reflect the LAST audit scan.** The index is rewritten only when a scan completes, so a PDF renamed, moved or imported since then is unlabelled in chat (and its `(source: …)` is not resolved in a deck) until the next scan; a stale entry for a renamed file simply never matches. Only PDFs the audit mapped to a record and reference notes are covered — an unmapped or `z_item*` PDF, a hand-written note and every image chunk stay unlabelled (measured: 93% of indexed PDF chunks resolve). The standalone `deckgen` CLI (`deckgen/__main__.py`) is deliberately NOT wired (it is app-independent and has no configured vault); it keeps the stem-only citation rule. The chat path uses exact paths only; basename resolution is for model-written deck sources, where an ambiguous basename is dropped rather than guessed. The audit tab prints the index's age beside "Refresh widget" (from `GET /api/audit/summary`'s `paper_index_built_at`), and the switch lives in the LLM Settings modal.
- **[2026-09-23]** **Two live labels carry non-Latin characters** (an emoji and a variation selector, measured over all 6,763 labels; none carries `{`, `}` or `\`). Cited with citations OFF they land in a `\refline`/`\vfill` footer as text, which `pdflatex` cannot typeset (`xelatex`/`lualatex` can). Deliberately left: two papers, and the footer text is the paper's own title. The footer entries are joined by `; ` since the 2026-09-23 review (`deckgen/latex.py::_FOOTER_SEP`; `_shown` folds a `;` inside a label to `,`); a footer an older deck wrote with `, ` is accepted on read only when every comma piece is a filename, otherwise it is one entry — one cosmetic duplicate at worst, never a lost name.
- **[2026-09-23]** **`papermind vault search` printed `None` for every row until the same review**: the printer read `file_path`/`text` off rows that are the chat `retrieval` frame's `{source, score, is_image, paper?}`. It now prints `[n] <source> — <label>` and the score (pinned at the driver seam; the additive `paper` field is ratcheted in `tests/test_driver.py::TestDriverResponseProjections`).
- **[2026-09-23]** **`write_text_atomic` gained `fsync=False`** for the paper index (regenerable); every other caller keeps the durable default. The paper-index listener runs inline on the scan thread (a 0.135 s name walk + a 0.41 s build + a 0.015 s write, 3.36 MB of JSON — measured 2026-09-23), not on a daemon like the widget build.
- **LanceDB MMR / similarity-cutoff scale** — on the lancedb backend MMR runs client-side over an over-fetched pool (approximate, not the native dense-leg semantics), and the dense *score* is `exp(-distance)`, not raw cosine — so the dense-only-no-reranker `similarity_cutoff` (0.25) has a different scale on lancedb than on `SimpleVectorStore`. Ranking parity holds; absolute cutoff semantics differ. Low priority (reranker is on by default).
- **LanceDB orphan from a deleted note in a crash window — partially fixed (2026-07-03).** The drift check now sweeps orphan rows (ids in the table but not the docstore) via `lancedb_list_doc_ids`/`lancedb_delete_ids` whenever it detects `vec_rows > node_count`, and a failed sweep is logged + surfaced instead of silent. Residual gap: offsetting add/delete drift inside one crash window balances the counts, so the branch never fires and those orphans still linger until a full reindex. Rare; self-resolves on a clean full run.
- **[2026-08-18]** **Query-time embedding is served by the CHAT provider's local backend, not the backend recorded in the index** (`rag/engine.py`) — the read-side twin of the indexer's `embed_backend_mismatch` gate. The indexer refuses to mix vector spaces; retrieval does not check.
- **[2026-08-19]** **`obsidian_meta.json` records the embed PROVIDER but (apparently) not the embed MODEL.** The `embed_backend_mismatch` gate refuses a run when the recorded local backend differs, and its stated rationale is that the same model name can be a differently quantized/pooled build — but if the model name is not recorded alongside, switching the `embed` model *within* Ollama would silently mix vector spaces, which is the exact failure the gate exists to prevent, one axis over. **Unverified** — found by reading the gate's documentation, not its code. Check before trusting either way.
- **[2026-08-18]** **The stale-sweep image protection ignores an unreadable SUBTREE** (`rag/vault.py`): `_scan_failed_sources` spares an image whose own read failed, but an image referenced only from notes under a directory that became unreadable is durably de-indexed rather than withheld for one run.
- **[2026-07-30]** **Three image-link shapes the resolver doesn't cover** — partial-path wikilinks (`![[subdir/img.png]]`), basename case-collisions on a case-sensitive FS, and unencoded-space inline links (`![](Fig 1.png)`). Details + rationale in `rag/CLAUDE.md` (#33). Fixing any of them changes which files index, i.e. additive vision/embed churn on the next reindex — gate deliberately.
- **[2026-07-22]** **Referenced-image NFC dedup is first-form-wins** — when the same image is reachable as both an NFC path-ful link and an NFD bare-name resolution, the rel recorded (and therefore the chunk ids) is whichever note streams first; adding/removing the winning note flips the form and costs a one-time re-embed of that image. Accepted (rare, self-healing).
- **[2026-07-30]** **A truncated vision description is cached as if complete** — *detection shipped 2026-07-30 (Plan 8); the cache policy is deliberately unchanged.* Both transports now report the stop cause via a `meta["truncated"]` out-dict and `describe_image` logs a WARNING naming the cap and the remedy. The text is STILL cached: NOT caching it would re-run an expensive vision call on every indexing run — a worse trade for a large vault than a truncated-but-searchable description. The cache keys on the *image bytes*, so raising `vision_max_tokens` still does not regenerate it; clear `obsidian_cache/.../image_cache/`. A self-healing variant (record the cap alongside, re-run when it rises) is the open follow-up.
- **[2026-07-29]** **Protected index-archive retention is count-bounded, not byte-bounded** — *disclosure shipped 2026-07-30 (Plan 7.5); the cap itself is still count-based.* `_prune_old_index_backups` keeps up to `_INDEX_BAK_PROTECTED_KEEP = 5` identity-rebuild archives — on a 5.4 GB index that is ~27 GB of app-data. A byte cap still needs a du-walk on every archive, so it stays deferred; instead the archive's size is measured ONCE at creation (`_dir_size_note`, bounded by `_DIR_SIZE_WALK_BUDGET_S`, off the indexing hot path) and reported in the log AND the status ring together with the retention counts, so the cost is visible where the user looks. A partial walk reports nothing rather than an undercount. The `.keep` suffix keeps the archives visible in Finder.
- **[2026-08-25]** **`rag/engine.py::_prepare_retrieval` depends on ~10 LlamaIndex PRIVATE members and reimplements `QueryFusionRetriever._retrieve`'s fusion dispatch.** That is the price of AP-09's prepare/read/finish split: the remote work had to move out of `_index_mutation_lock` while preserving the legacy call graph. `tests/test_retrieval_private_api_contract.py` constructs the real objects and fails at upgrade time (the rest of the retrieval suite mocks above that boundary and cannot). When it goes red after a dependency bump, re-read the new LlamaIndex source and update `_prepare_retrieval` — never delete the assertion. The duplicated fusion algorithm can also drift silently from the library's without any test noticing.
- **[2026-07-30]** **Eval graded qrels (v2 seam, not built)** — `origin: "curated"` in the golden set is informational; it is the seam for UMBRELA-style LLM-judged graded relevance (`tests/eval/README.md`).

### Library Audit, bridge & accession

- **[2026-09-20]** **A `zotero_unexported` / `zotero_multi_links` row has a label and no verb.** Excluding them from *PDFs Without Zotero*, *↳ Unread Only*, *Not in Zotero* and `/api/accession/rows` stopped four surfaces lying about them, and Dead Curation now names each kind — but the row offers nothing to click and does not state its remedy, which is *run a Better BibTeX export* for the first and *trash one of the two linked attachments* (Zotero Attachments) for the second. The file is also invisible to the accession ladder in the meantime, which is right (it is in Zotero) but leaves no route to pair it if the export never comes. Fix shape: a `[re-scan after export]` hint and, for the multi-link case, a jump to the two attachments.
- **[2026-09-20]** **`accession/reparent.py` still hard-codes `users/0` in three places** (`:52`, `:117`, `:121`, `:155`). `pdf_management` moved to the journalled writers' `_lib_path` on 2026-09-19 for exactly this reason: `users/0` is the LOCAL API's magic personal-library segment and 404s against api.zotero.org, so a group item or a web transport reaches the wrong URL. Untouched because that module carries its own operation-record contract and has no `cfg` in scope at those call sites — a deliberate piece of work, not a drive-by.

- **[2026-07-30]** **Audit: mapped-PDF annotation reads are serial.** `audit/engine/inventory.py:332` calls `_annotations_for(rec.pdf_paths)` inside the per-record loop; the `ThreadPoolExecutor` pool (`_read_annotations_parallel`, `_ANNOTATION_MAX_WORKERS`) covers only *unmapped* PDFs. Consolidating the mapped reads into the same pool is the deferred follow-up (was recorded only in `audit/CLAUDE.md`).
- **[2026-08-08]** **`audit/manager.py` (low).** `start_scan` swaps `_settings` immediately while deliberately keeping the previous `_inventory`, so `get_inventory()` returns old-inventory + new-settings for the whole scan — and permanently if the scan errors (`_finalise(STATE_ERROR, inv=None)` leaves the stale inventory in place). Reports then render cached records against a directory layout they were never built from.
- **[2026-08-13]** **`audit/search.py::build_corpus` runs `find_harmony` + the two orphan serializers on every corpus build** — an O(records) pass with per-record `group_by_key` fingerprinting, on top of the pre-existing report flattening. Memoised per cached scan and never on the request thread (the 202-building contract), so it is bounded and off the hot path — but each scan's first search now pays measurably more than before the three reports existed. If it ever bites, the fix shape is sharing one `find_harmony` result between the corpus and the report route's memo rather than computing each independently.
- **[2026-08-12]** **The Zotero duplicate-key winner is SQLite scan order** (lead): `items_rows` has no `ORDER BY`, so a VACUUM or re-sync could flip which of two same-BBT-key items a record joins to. Both are the same paper, so the impact is bounded to which copy receives writes; deterministic first-wins is documented policy, but "first" is not pinned by the query.
- **[2026-08-12]** **A Zotero install that doesn't expose `citationKey` in `itemData` degrades the WHOLE library to the title heuristic** (lead) — silently, apart from the `zotero_matched_by_key` / `_by_title` / `_title_refused` counters. Worth a status-line warning when the key count is 0 while items exist; unverified because the maintainer's install has 6194/6194 keyed.
- **[2026-08-18]** **`apply-tag-drift` reads the note with `Path.read_text()`** (`api/routes/audit.py`) — universal-newline translation — and rewrites the whole file, so a CRLF note is silently converted to LF by a frontmatter-only tag edit.
- **[2026-08-18]** **`audit_drift_include_parent_tags` / `audit_near_dup_ratio` are persisted by `/api/audit/config` but absent from `_DEFAULTS`** (`core/config.py`), so they are invisible to the config-version migration pruning and to any defaults-materialising pass.
- **[2026-08-19]** **Printed-DOI disagreement report (not built).** 13 PDFs carry a page-1 DOI naming a *different* paper than their current mapping. Step 4.5 deliberately declines to act on them (a DOI printed on a multi-article journal page may belong to a neighbour — `luykx_2023.pdf` → a Teo paper), but some are genuine corrections it is therefore also declining (`davis_2_2024.pdf` really is the Brexpiprazole trial its printed DOI names). Surfacing them for review is the right home. Cost is why it is not in the scan: it needs a page-1 read for **every** author+year match (~1,900 files), not just the residual.
- **[2026-08-19]** **The ±1-year near-misses are a bibliography-hygiene finding in their own right.** 235 orphans differed from their bib entry by exactly one year — online-first vs issue year. Independent of file linking, and a report the bib owner would act on.
- **[2026-08-19]** **theses.fr intermittently answers HTTP 400** for queries it answers 200 minutes later (45 of 576 live batch calls; reproduced then un-reproduced same-day). Treated as designed: status-only note, resolution degrades, and the per-row **re-identify** verb (`force` — bypasses the cache consult, never the store) is the retry path. No retry machinery inside the client, deliberately.
- **[2026-08-19]** **Phase 5 (read-the-page vision button) — MEASURED AND DECLINED, not deferred.** Its premise was the "no-text 143"; that class no longer exists — every one of the 576 orphans yields page-1 text (the vault indexer's OCR caches). Of the 79 `none` rows, 60 had a title query that matched **no keyless catalog** (French grey literature: CCNE avis, EMC encyclopedia chapters, RCPsych leaflets, SPF bulletins — a *coverage* problem no better extraction fixes) and only **13** were extraction-abstained. A vision route + UI + tests for ≤13 files against catalogs that don't index the material fails the plan's own build condition ("only if the residue matters"). Re-open only if the orphan population changes character.
- **[2026-08-19]** **Phase 6 (Zotero recognizer endpoint, Mode C) — AWAITING THE USER.** The plan requires capturing one real Zotero request first (point `recognize.url` at a stub — a change to the user's Zotero prefs no agent should make unasked); the response shape is from a 2018 AGPL snapshot. Everything else about the mode is designed in the plan artifact (<https://claude.ai/code/artifact/b0744d87-b519-46c1-9124-c5cf260b70a4> — the plan's one surviving record now that `docs/accession_next_session_prompt.md` is deleted per the plan-docs rule).
- **[2026-08-26]** **An EMC classifier IS buildable — the measurement that said otherwise was
  wrong (corrected 2026-08-26).** The original entry said the app's page-1
  text drops the EM-Consulte article code and instructed the reader not to
  re-add the branch. Re-measured through the app's own chokepoint
  (`obsidian_manager._read_pdf_text(fresh_max_pages=1, use_markitdown=False)`),
  cold cache and warm, the code is present at offset 1302 / 1409 / 1583 on the
  three files cited as verification — well inside the 4 000-char window
  `extract_printed_identifier` already scans. Only "it is not the FIRST line"
  was true; EM-Consulte's download watermark precedes it. **The instruction not
  to revisit is withdrawn.** What remains is a scoping decision, and it has an
  argument on its side: an `emc` class would be the first thing able to supply
  `Query.container` automatically, which is the automatic caller the
  container-scoped Crossref search below still lacks (14/20 right with the
  constraint against 10/20 and 4 confident-wrong without). Maintainer call, not
  a drive-by.
- **[2026-08-26]** **`Source.run` drops a container constraint a non-Crossref source cannot
  honour, and `resolve_queries` then MERGES that source's unconstrained
  answers with Crossref's narrowed ones.** Inert today (nothing constructs a
  `Query` with a container), but the measured "14 right, 1 wrong" is a
  Crossref-only figure: whoever builds the first caller has to decide whether
  an unconstrained rescue answer may compete with a constrained primary one.
- **[2026-08-26]** **The container-scoped title search has no automatic caller.** It is
  measured (10 → 14 right of 20, confident-wrong 4 → 1 on the EMC set) and
  wired end to end, but nothing in the app can infer a venue for an
  UNIDENTIFIED PDF, so the constraint is only ever user-supplied. Exposing it
  on the accession `[re-identify]` verb as an optional "restrict to
  journal/series" field is the obvious next step and is not built.
- **[2026-09-07]** **The accession ladder's live hit-rate has never been measured, and two settings make it likely to come up dry.** The "[re-identify] does nothing" report was a FEEDBACK bug (fixed — see the accession route bullet), so the question it was hiding is still open and is now answerable: with outcomes visible, one click says whether the ladder was asked and what it said. Live config at the time of the report: **`accession_timeout_s` = 5, the FLOOR of its 5–60 range**, while `accession/sources.py` clips each call to the turn's remaining wall-clock and refuses under 5 s, Crossref paces at 1 s and arXiv at its documented 3 s — so a slow source plausibly times out every time; and **`accession_contact_email` is empty**, which puts Crossref on the anonymous pool rather than the polite one it asks for. Neither was changed: they are the user's settings, and changing both at once would make the result unattributable. **Measure, don't assume** — one identify at the current settings, one at `accession_timeout_s` 20–30 with an email set, comparing `consulted` and the outcome tier; the status line now names the source notes, so a timeout is legible as a timeout rather than as "no match found". `contact_email_set` already rides the `/rows` envelope and is still not surfaced anywhere in the UI.
- **[2026-09-07]** **The repoint picker ranks over EVERY biblio PDF (5,609) on each keystroke, server-side.** One `rglob` plus a `difflib` pass per request, debounced at 220 ms. Measured nowhere — it was built against a backlog of 0 broken links. If it ever feels slow, the fix shape is the accession key-picker's: fetch once per scan and filter client-side.

### Tagging, journal & note sync

- **[2026-09-21] Adversarial library-workflow review:** regression tests cover stale mirror receipts, reparenting/adoption history, cancelled base writes, hidden partial creates, unit-bound proposal input, cached-token pricing, uncertain replay, per-target retry receipts, missing terminal frames, tracker failures, capped mirror settlement and cross-chunk rename reservations. The production-route disposable acceptance covers scan → imports → rename → tagging → mirror → unchanged second pass, plus network/base failures, external edits and SSE disconnect. **Retired 2026-09-22:** a rename no longer invalidates the mirror plan — the run mirrors first and renames last, and the rename route rewrites the created note's `pdf:` entry (`tests/test_library_acceptance.py::test_a_rename_after_the_mirror_keeps_both_the_refresh_and_the_new_name`). Tag and `pdf:` writes still leave notes uncommitted; the next mirror refuses to refresh those until the folder is committed, and no automatic commit or bypass exists. Abrupt process death between an effect and its receipt, native WKWebView/VoiceOver, real writes and real providers remain unverified. Two initial scratch tests violated app-data isolation and added temporary-fixture state to the real app stores; evidence and remaining phases are in `docs/review_2026-09-21_library_workflow.md`. No cleanup has been performed.

- **[2026-09-20]** **`find_linked_attachments` still returns a whole folded bucket when nothing matches exactly.** It now prefers refs whose stored path realpaths to the named file, which is what stops a case-sensitive volume repathing two different files in one rename. The fallback is deliberate — the fold exists so an NFD or case-variant path the scan already paired is still found — but it means a bucket that is BOTH mixed and inexact still yields every ref. `zotero_link_index` drops such a clash outright; the renamer cannot, because dropping it would report "Zotero holds no link" for a file Zotero does link. Narrow (needs a case-sensitive volume AND an NFD mismatch); fix shape is to refuse the rename and name both candidates.
- **[2026-09-20]** **The Zotero Attachments retitle verb sends at most `_ZOTLINK_MAX_RETITLE` (1000) per run.** Above that the section says so and the button names the capped figure; the report reloads after a successful batch, so the remainder appears on the next render and the user clicks again. There is no "and the rest" affordance, and no progress across batches — acceptable at the measured 653, worth revisiting if the mismatch population ever exceeds one batch.

- **[2026-07-21]** **Tagging journal growth — BOUNDED AND VISIBLE since 2026-08-26 (Batch F), not closed.** Applied Finder/Zotero ops remain permanently non-evictable by design (their prior tag lists live INLINE, so eviction destroys the only restore mapping), so the history still only grows on its own. What changed: `journal.stats()` reports size and composition in pure SQL (30 ms, no payloads parsed), `journal.load_page()` serves the Restore view from bounded SQL over a `seq` cursor (**1.1 ms against `load()`'s 110 ms** on the measured 11 651-op journal), a confirm-gated `compact-journal` action drops applied external ops per a chosen policy after showing the real count, and an automatic retention pass exists behind a setting that **ships disabled** — the only setting in the app whose effect is to delete undo records. What retention costs when enabled: undoing a tag ADD is functionally "remove those tags", which `remove-tags` does without the journal; what is uniquely lost is the RAW prior form — Finder colour suffixes and Zotero tag type/colour metadata. **The residual is `load()` itself, which still parses the whole history on every write** (merged here 2026-09-03 from the duplicate 2026-08-26 entry): the partial-load rewrite is the correct fix and is deliberately NOT taken, because it changes exactly the in-memory dirty-tracking invariants that lost writes silently twice between 2026-08-24 and 2026-08-25, and it wants its own session with red-proven tests rather than a line item inside a batch. Two READ paths escaped it on 2026-09-02 without touching that contract: `peek_op` / `peek_batch_ops` answer the restore route's two peeks with one indexed SELECT returning PLAIN dicts (`load_page`'s posture — structurally unsavable), 63 ms → 0.38 ms and 60 ms → 1.47 ms over 11 651 ops.
- **[2026-07-30]** **`retire-tag` cannot reach Zotero child-note tags or `.bib` keywords**, although the Tag Hygiene report's cross-store counts include all five stores. The confirm text already says so. Restated 2026-08-12 because the NFD fix touched the same loops: the candidate enumeration covers note frontmatter, Finder xattrs and Zotero *parent* tags only. Same root cause as the unmapped-PDF gap closed 2026-08-12 (the candidate loop iterates `inv.records`) — the two remaining stores are reachable only by enumerating outside the citation-key join.
- **[2026-08-18]** **Harmonize can add hundreds of permanently prune-protected Finder/Zotero ops in one confirmed click** and the confirm flow discloses none of that growth-side cost (`_HARMONIZE_MAX_NOTE_OPS` bounds only the note half). `_HARMONIZE_MAX_KEYS` (2000) is what bounds the request, and since AP-10 made journal writes incremental it **rests on journal GROWTH, not rewrite cost** — the original "every write rewrites a ~3 MB manifest" rationale is gone, but applied finder/zotero ops remain non-evictable, so a library-wide harmonize still adds thousands of permanent rows. Revisit the number if a retention policy for external ops ever lands; a disclosure line in the confirm is the cheap interim.
- **[2026-08-12]** **Tagging journal `prune` drops `failed` Zotero ops whose PATCH may actually have landed** (lead, no reproduction): a lost response after a server-side apply leaves the op `failed`, and the next prune reclaims its before-lists — the only restore mapping — while the removal is live in Zotero. Settling it needs a lost-204 reproduction.
- **[2026-08-18; corrected 2026-10-04] Missing-note restore evidence.** A missing note now returns `missing_target` without spending the operation. Its snapshot is protected from ordinary pruning while recovery is blocked; restore can retry after the original path returns. The isolated rename/retention/retry regression covers this. Previously deleted snapshots cannot be reconstructed by this correction.
- **[2026-08-26]** **`--min-tags`/`--max-tags` is not exposed on the Maintenance panel's
  batchtags step beyond `max_tags`.** The option schema has no nullable type,
  so `max_tags` carries a `-1` sentinel; `min_tags` was left out rather than
  adding a second sentinel for a bound nobody asked for.
- **[2026-08-26]** **The tag-depth cohort filter is client-side.** `GET /api/tagging/inventory`
  has no server-side filter and returns every row anyway (the `--keys`
  precedent), so narrowing happens in the browser. Fine at 6 300 rows; if the
  inventory ever grows an order of magnitude this is where to look.
- **[2026-08-12]** **`tests/js/taggingScope.test.js` cannot preset `localStorage`** — `tagging.js` reads `_view`/`_scope` once at module load and the file imports it statically, so every test must drive the real controls. Noted because the natural fix (a `vi.resetModules()` + dynamic import harness, as `tagging.test.js` uses) is a file-wide change, not a per-test one.
- **Zotero local writes — updated 2026-09-11.** Zotero 9.0.6 was verified read-only in the earlier investigation. Zotero 10 is installed (**10.0.2** as of 2026-09-10; the write/merge evidence in `prototypes/zotero_desktop/` was gathered on 10.0.1 and the companion hard-pins 10.0.2, so an exact-version claim in that tree is a fail-closed gate, not a description of the evidence) and its local API supports writes after explicit authorization. The shared client carries local server IDs and guards restores; Web transport remains optional. See `tagging/local_api.py`. **Live auth + writes exercised 2026-09-11** (Authorize-once → notesync push/pull/merge, rename repath, attach smoke); grants remain process-memory (Zotero quit drops `:23119` until relaunch). Desktop HTTP attachment-create undo stays refused.

- **[2026-09-21] Library workflow UI — Chromium review closed, native gates open.** Mounted browser fixtures cover long Library/Notes/Tagging/Restore views, keyboard confirmations, blocked/first-pass/error/partial/uncertain states, result recovery, supported widths and short windows, CSS 200% zoom, axe serious/critical checks and enforced CSP. Corrected stale announcements after section/main-tab changes, misleading cancelled/partial results, hidden tracking errors, incomplete rename confirmation, missing analysis-terminal feedback, inaccessible excerpts, missing HTTP Retry, Restore status loss, help hover/tablist semantics and long filenames hiding the proposed-name column inside the report scroll area. **Recover last run** remembers one session ID in browser storage and reads its saved outcome with the original UTC date; this is not a history browser or an uncertain-write resolver. Review still selects proposed names/tags rather than editing them per row. **Unverified:** WKWebView/VoiceOver reading order and announcements, native keyboard traversal and app/browser zoom, contrast in native themes/high-contrast modes, real writes/providers, process-death recovery and representative performance. Chromium/CSS zoom/axe are not substitutes for those checks.

- **[2026-09-21] Library orchestration — remaining limits.** Analysis never starts an audit scan: new imports require an explicit scan and a Better BibTeX export before rename/tag proposals can use their citation keys. Renames rederive context per file; no shared batch context exists. Cancellation reaches the outer orchestrator between calls, not inside an already running in-process route — except the mirror, whose nested stream is read frame by frame since 2026-09-23 (P2): its `{"info"}` frames reach the outer stream (feeding its stall guard) and an outer cancel closes the inner response, whose `_SSEOpGuard` cancels the inner worker at its next note; the nested call still holds a second streaming slot (of `_MAX_LIVE_SSE_WORKERS`) while it runs. Recover the saved result with `GET /api/library/sessions/<id>`; unknown effects must not be retried blindly. Session `applies` remains a read/save pair of transactions, supported under one server process. A completed rename with incomplete Zotero repointing is `partial` in the result and `done` for the rename step; repair the link from Zotero Attachments. **Closed:** mirror outcomes now settle a note unit only on the mirror's `settled_keys` (audit 2026-09-23 — a record newer than the scan stays open as "rescan needed"); `apply` refuses during a scan; current disposable acceptance uses production routes, not only stub write routes. Time to first feedback, peak memory and mapping reads per preview still lack representative measurements; abrupt process-death recovery, real writes and real providers remain unverified. **Historical read-only evidence, not rerun here:** the September 21 headless check used copied app state and reported a 37 s scan, first/second analyses of 4.2/1.7 s, and an unchanged mirror plan with zero creates/refreshes/bases (6,882 entries, 6,265 PDF attachments, 1,237 notes, 353 unlinked PDFs). It made no confirmed writes or LLM calls. The earlier synthetic tracker measurement was 12,000 units in 0.43 s plus 0.04 s; neither measurement is a performance guarantee.

- **[2026-09-22] The Maintenance `links` symptom is unconfirmed.** The maintainer reported the Maintenance buttons as "broken"; a read-only run measured `links` at 4.6 s with 133 findings and `pdfnames` at 2.8 s with none, and the confirmed causes were the Library-hosted checkboxes Run silently dropped and a passing step whose card showed nothing — both fixed (follow-ups lots 3–4). If `links` still reads as broken after that, it is something else: ask what was seen.
- **[2026-09-22] Tag and `pdf:` writes make notes git-dirty; the mirror then refuses to refresh them.** By design: the mirror's per-file git proof needs the pre-write bytes in a commit. The run's result names the count; commit the notes folder before the next mirror. No automatic commit.
- **[2026-09-22] Leftovers from the review of the follow-ups commits.** Fixed the same day: the mirror's `note_tags` bound, refused tag rows tagging created notes, silent loss of ticked note tags, the `pdf:` lost-lock bookkeeping, overlapping Maintenance runs and result cards stealing focus. Left, all low: (a) re-opening Library → Checks & campaigns after a run rewrites "Elapsed" as time since the run started (`_runStartedAt` is never reset); (b) both Maintenance surfaces share one log buffer, so the Maintenance tab's Copy/Download export the last Library run's lines; (c) `initMaintenance` resets `_lib`, which would strand a Library run if a future caller re-initialised mid-run (no caller does today); (d) the links fold triggers on any detail containing "; " and miscounts `latex`/`latexrefs` details; (e) the status text can read "first.. No findings." and appends "No findings" to a failed step; (f) a failed step registry makes the Library checks vanish with no message there; (g) `_parse_note_tags` truncates keys over 512 and tags over 128 characters instead of refusing them; (h) ticked note tags for a note deferred past the 100-create cap are reported, not carried forward to the next run; (i) test gaps — no test pins that the plan and the write share one render CONTEXT (only one render function), and the in-process `pdf-links-status`/`apply-pdf-links` transport wiring in `api/routes/library_workflow.py` is covered only through the fake transport.
- **[2026-09-22] The JS suite is load-sensitive.** Under a load average above 200 (an external process on the maintainer's machine during this work) `sideBySideDiff`, `tabScrollMemo`, `noteSyncCreate` and `accessionDeck` exceed vitest's 5 s per-test default and fail; they pass with `--testTimeout=120000`. Not a code defect; raise the timeout in `vitest.config` only if it recurs on an idle machine.
- **[2026-09-21] Session tagging — remaining limits.** Budget estimates depend on tiktoken and configured/static pricing, not the provider's tokenizer or live prices. An accepted unpriced model has unknown, unbounded cost; so does a `no_limit` session (2026-09-22), by the maintainer's explicit acceptance — its spend is measured and reported, never capped. A charge that cannot be persisted is reported (`spend_error`) and marks the session's spend unknown, which stops a budgeted session (audit 2026-09-23, L4); if even that write fails the response says `spend_unrecorded` and the analysis stops proposing. An unpriced model runs only under `no_limit` (L3); an analysis stops proposing after `tagging/batch.py::MAX_CONSECUTIVE_FAILURES` provider failures in a row, backing off on `backoff_s` (L6). Session model and spend are columns and run results are rows (`import_session_results`), migrated from the old JSON by the first writer (P5); session creation stores the model in one transaction and answers 503 on a locked store (L12). No provider switching or retry is enabled by the session config. `propose {unit}` and `apply-all {unit}` resolve the PDF by tracked identity; legacy path-based `apply-all`, `apply-finder-tags`, harmonize, retire-tag and `tagging batch` require a rescan after renames to avoid stale paths. Each PDF needs its own unit application for Finder tags. A filesystem race remains between identity recheck and xattr write. Few-shot exemplar contents and the candidate embedding model are absent from the proposal cache key; vocabulary gloss changes invalidate via the vocabulary digest. **Closed:** sessions/proposals are connected to the Library UI and CLI; they are no longer API-only.

- **[2026-09-21] Import tracking — remaining limits.** The orchestrator now calls the tracker; its first observation establishes a baseline without sending historical items to an LLM. A file moved and renamed before Zotero catches up is temporarily `settling`; do not identify it online in that pass. Two attachments linking one file remain two units. Without birth times, inode-only `doc:` identities are not used for adoption. An outside-library, unresolved relative or missing linked file owes tagging only; basename suppression for unresolved `attachments:` links can hide an unrelated orphan of the same name until the link resolves. Observation ordering uses wall-clock `observed_at`; a backwards clock change may require a fresh observation. Reparenting/adoption and stale mirror receipt fixes are covered by the current Phase 1 regression tests.

- **[2026-09-21] Note safety — remaining limits.** `/migrate` and `/unmigrate` have per-file Git gates but no reviewed `plan_digest`; their confirmations recompute the plan. Git recovery names the checked commit and assumes checkout conversion settings remain unchanged; rewriting history so that commit disappears is outside the app's protection. Note retirement retains the documented two-syscall race for an editor writing in place through an already-open descriptor. Mirror bootstrap writes application state without digest binding; the dormant campaign path compares and saves bases under separate lock acquisitions (its executors return 501). `/create` needs a coherent readable Zotero snapshot; a WAL change during the read can require retry. iCloud placeholders remain untested and may block the link walk or produce `links_unverified`. The mirror test module still imports its parent test class by name, causing duplicate collection. **Closed:** unchanged `unparsed` bases are no longer rewritten; cancellation of unengaged base work and truthful partial-create counts are covered by the current Phase 1 regressions. **Historical read-only evidence, not rerun here:** September 21 checks reported Git coverage of all 1,237 notes in 0.11 s, a link walk with no uninspectable files in 0.15 s, stable dry-run digests and write-free refusal paths. These are not live-write or native-UI acceptance.
- **[2026-09-07]** **The `pdf:` field's placement was decided by the app, not by looking at Obsidian.** Frontmatter is the app's territory (tags live there, the round-trip guard exists) but Obsidian only renders a frontmatter wikilink as a CLICKABLE link when the property is typed as a link/list property, which depends on the vault's Obsidian version and property settings. **Maintainer confirmed 2026-09-11 that Obsidian renders the written `pdf:` list correctly** (koryagin sample). If it ever renders as inert text on another vault/version, alternatives are the header above the rule (Zotero Integration template territory) or a marked block (third region to parse).
- **[2026-09-20, superseded 2026-09-21]** The earlier private Maintenance `librarysync`/`cleanup.autosync` auto-accept design was not built. The delivered session workflow requires reviewed selections. Broader mechanical cleanup automation remains a separate unimplemented proposal; no old acceptance rate authorizes unattended writes.
- **[2026-09-20]** **`/api/notesync/mirror`'s create list is bounded by the cached scan's JOIN, not by Zotero.** Its Zotero half is a FRESH read (a reading note written since the scan IS mirrored, one deleted since is not), but the citation-key ↔ record join comes from `audit_manager.get_inventory()`, so a record that did not exist at the last scan is invisible however many reading notes it has. The sweep discloses the snapshot's age and names `papermind audit scan` past 30 minutes (the `tagging batch` pattern) rather than pretending otherwise. Fixing it means a scan, not a code change.
- **[2026-09-20] Mirror live-write gate:** historical read-only observations are recorded above; the new workflow has only disposable write acceptance, not authorized real-library mirror-write validation.
- **[2026-09-20]** **The mirror's git gate has the same check-then-act window every sibling has.** It is evaluated once, before the stream, so the folder could in principle be dirtied between the check and the refresh phase. `refresh-all` and `/migrate` carry the identical window; re-checking inside the worker would narrow it but not close it, and would move a refusal from a real 409 into an error frame inside a 200.
- **[2026-09-19]** **`refresh-all` has no key filter and reads the LOCAL Zotero SQLite.** A note pushed through the web API reads as "Zotero newer" (with the OLD text) until Zotero Desktop syncs down, and the sweep would lay that stale text over the region. It is one clean sweep by decision; a `citation_keys` exclusion is the fix shape if a partial sweep is ever needed. Also by decision: notes gone from Zotero are only listed — there is no delete verb — and the 156 `lossy_markup` notes (images, unsupported markup) are written from the converter's rendering, escapes (`n\=29`, `\&`, `&nbsp;`) included, exactly as `pull` writes them.
- **[2026-09-07]** **`notesync_containment` is now vestigial in the UI.** The legacy `drifted` bucket was the only surface that used the threshold, and it is retired; the config key, its validator and `_states`' computation all remain. The maintainer's value is 1.0 (default 0.85), which no longer affects anything on screen. Retire the knob or find it a consumer.


- **[2026-09-23]** **Leftovers of the 2026-09-23 audit remediation** (plan deleted after shipping; its batches are in `CHANGELOG.md` and `docs/library_workflow_contract.md`). Each is a deliberate stop, not a forgotten item:
  (a) **P10 — `import_events` and `import_proposals` grow without bound.** No production reader walks them (`imports.history()` has no caller); add a `(scope, session)` index and a retention rule when one appears.
  (b) **P11 — the observation and the mirror dry run each read Zotero.** Sharing one read would widen the transport (the two readers return different shapes: `reconciliation.observe` vs `_fresh_zotero_notes`); an analysis therefore pays two SQLite snapshots.
  (c) **The nested mirror still holds a second streaming slot** (of `_MAX_LIVE_SSE_WORKERS`) while an "Update my library" apply runs it: the call stayed nested so the route remains the one implementation (P2's permitted alternative).
  (d) **`refactor.journal.save_merged` has a small lost-write window** (N12): the refactor manifest is one JSON file with no transaction, so a write by a new lock holder between the merge's load and its save can still be lost. The tagging journal's SQLite merge has no such window.
  (e) **`git_undo` reads each converted file with its own `cat-file --filters`** (~6 s over the vault if every note needed conversion; none does today). `cat-file --batch --filters` failed on the git shipped with macOS 27 (2.54) — re-verify on the installed git before switching.
  (f) **P8 is only half done:** the tracker's DDL now runs once per store file, but a route still opens several short connections (one per transaction) rather than one per call.
  (g) **N10 was applied to LOCAL providers only.** `/api/models?provider=` answers `configured: ""` for a non-active local provider (the shared `llm` field), while an online provider keeps its own `*_model` field. If the decision meant "every non-active provider", drop the online branch in `api/routes/status.py` and the matching assertion in `test_status_routes.py`.
  (h) **A tag retry on a note the mirror created untagged stays `failed` ("rescan needed") until a rescan**: `apply-all` cannot see the note before the scan knows it, and "no note" is not true of it.
  (i) **U19 (by design):** the help-badge row stays.
  (j) **From the retired 2026-09-15 unification plan (deleted 2026-09-23):** still to decide when the feature is built — the exact file-naming convention already configured, the evidence that qualifies two PDFs as versions of one work, the handling of images already present in Zotero notes, and the human/native validation of every experimental operation (campaign executors stay 501; see *Library, PDF management and note campaigns*).

### Note Refactor, deck & document generation

- **[2026-08-18]** **Refactor "Revert all" is still a synchronous JSON request** with a static status line for what can be minutes of per-op restores (the server heartbeats per op precisely because it is long). The fix shape is the tagging batch-restore's SSE conversion (2026-07-30 precedent) — per-op `{"info"}` progress + a terminal frame — deferred because it touches the restore route's response contract; the client line now at least states the duration honestly.
- **[2026-08-18]** **The applyable LLM rewrite / custom-edit has an input-truncation guard but no OUTPUT one** (`refactor/llm_edit.py`): a reply cut off at `max_tokens` is staged as a complete whole-note proposal, and the WYSIWYG hash certifies exactly the truncated bytes. (The input half is the 2026-07-04 item 1.4 fix; the same reasoning applies to the output.)
- **[2026-08-18]** **Pre-upgrade deck checkpoints resume with raw `(source:)` tags** — `compute_job_key` carries no citation-post-pass salt, so one resumed deck per stale checkpoint ships the pre-pass artifact, then the checkpoint clears. Transitional and self-clearing; adding a salt would discard every user's in-flight checkpoint once for a one-deck cosmetic issue.
- **[2026-08-08]** **`api/routes/docgen.py` (low) — REFRAMED as prompt-quality only.** compile-fix imports `COMPILE_REPAIR_SYSTEM_PROMPT` / `build_repair_messages` from `deckgen.compile` and uses them verbatim on an `article`, so the model is told it is repairing a "Beamer presentation" with "frames". Capped by `screen_article_repair` + the verifying next compile, so no unscreened write — `docgen/prompts.py` is where an article variant belongs (`docgen/review.py` is the precedent for the split).
- **[2026-07-30]** **deckgen batch / multi-deck generation is not built** — one deck per invocation. A 2026-06-19 design review block (`deckgen/README.md`, "Future improvements") records two candidate shapes and their prerequisites; it exists nowhere else.
- **[2026-07-30]** **Tier-3 cleanups deferred from the 2026-07-03 perf pass** (recorded only in `CHANGELOG.md`): **frame-slim**, **apply-recompute**, ~~usage-log rotation~~ (**FIXED 2026-07-30**, Plan 8: rotates at 10 MB into one `.1` generation, and the summary reads BOTH so a reporting window spanning the rotation keeps its records), and **deck-checkpoint `raw`**. Each changes a serialized contract or touches the WYSIWYG guards, which is why they were held back.
- **[2026-07-20]** **The `apply-repair` client-supplied-`tex` trust model** is the one deck writer where the server does not own the bytes (it re-screens and stale-diff-guards, but the proposal itself arrives in the request body — unlike `apply-augment`, which reads its proposal from server-side staging).

### Plain Chat attachments

- **[2026-08-30]** **The `_MIN_USEFUL_CHARS` floor and the page bound do not compose cleanly for
  a pathological PDF.** A document whose first ~151 pages are all image with no
  OCR layer now yields under 16 characters and is refused as "no readable text"
  — correct, but the message names OCR while the real cause could be that the
  text starts on page 200. Not worth a second message today (the measured
  library has no such file); if it ever bites, the fix is to say which pages
  were read in the refusal.
- **[2026-08-30]** **`fetch_attachments` is retained beside `fetch_attachments_map`** and is now
  used only by tests. It documents the caller-order property the map version
  deliberately does not have; delete it only together with those tests.
- **[2026-08-29]** **`mock.patch.object(services.attachment_service, …)` never reaches a request,
  for TWO independent reasons** (diagnosed 2026-08-29; the mechanism recorded the
  same day as unexplained "module duplication" is now measured). (a)
  **From-import shadowing, always, in any ordering.** `api/routes/chat_attachment.py`
  and `api/routes/plainchat.py` both `from services.attachment_service import …`,
  so each route module holds its OWN binding; rebinding the name on the service
  module leaves `api.routes.chat_attachment.store_attachment` pointing at the
  original function. (b) **The module really is executed twice under some
  orderings, and the cause is this repo's own `conftest.py`.**
  `IsolatedModule.collect()` deletes every app-prefixed `sys.modules` entry
  before importing each test file, so a later function-local `import
  services.attachment_service` re-executes it and yields a module whose
  `__dict__` is not the one the already-built route functions close over.
  Measured with a stack-logging probe in the module: `pytest
  test_chat_attachment.py` alone → 1 execution; preceded by
  `tests/test_optional_maintenance_import.py` → 2, the second from a test body.
  NOT pytest's import mode, NOT the `services/` namespace package, NOT the
  `builtins.__import__` patch — all three checked, none load-bearing.
  **Test-only** (production imports once at boot), but the failure mode is a
  green test proving nothing. **Rule: patch at the CONSUMER's name**
  (`mock.patch("api.routes.plainchat.stream_chat_messages", …)`), immune to
  both; `test_chat_attachment.py::cap_of` sidesteps it by driving the real
  `chat_attachment_max_chars` knob.

### CLI, MCP, build & CI

- **[2026-08-12]** **The GUI can serve without leaving a `server.json`, which silently disables every CLI/MCP write** (lead, observed live 2026-08-12 during the post-review suite rebuild; NOT introduced by that range — nothing in it touches `launch.py` or `core/server_info.py`). **Mitigated 2026-08-13, still unexplained — the lead stays OPEN.** Three changes make it survivable without pretending to know the cause: (a) `core/server_info.py::ensure_server_info()` re-asserts this process's registration, called from `/api/health`'s **cache-miss** path (`api/routes/status.py`) — the UI polls every 15 s against a ~10 s TTL, so a vanished record is restored within one poll instead of staying broken for the process's lifetime. It is deliberately narrow: a no-op unless THIS process previously called `write_server_info` (so tests and CLI imports can never conjure a record), and it never overwrites a record naming a **live** other pid, because `clear_server_info`'s pid gate depends on nobody stealing another server's claim. (b) `launch.py` logs an explicit line right after `write_server_info(port)` naming the file, the port and whether it then exists — the original investigation could only *infer* "reached the write site" from the next log line, which is exactly what left "never written" and "written then removed" indistinguishable. (c) `papermind status` now prints what is actually broken (writes) plus both workarounds instead of a bare "not running". Pinned by `tests/test_server_info.py::TestEnsureServerInfo` + `test_health.py`. Symptom, for reference: the app is up and healthy (`/api/health` → `ok` on its logged port) while `BASE_DIR/server.json` is absent, so `driver.resolve_base_url()` finds nothing, `papermind status` prints "Server: not running (in-process snapshot)", and every write tool refuses — the read-only in-process fallback makes it look like a *config* problem rather than a discovery one. Ruled out during the observation: the writer itself works (`write_server_info` called in isolation wrote and read back), no `CHATEKLD_API_URL`/`CHATEKLD_BASE_DIR` override was set, the GUI and the shell agreed on `BASE_DIR`, `launch.py`'s write site is not gated behind anything that could skip it silently (its `except Exception` logs, and the log had **zero** "could not write server discovery file" lines), and both `clear_server_info` call sites are pid-gated so no other process could have deleted it. That leaves "written then removed by its own pid" and "never written despite the site being reached" as the two candidates, neither of which the evidence separates. **What would settle it:** relaunch the app on a clean `BASE_DIR` and poll for the file at sub-second granularity from the moment `open` returns (the original observation polled every 2 s for 3 min and never saw it), and log an explicit line right after `write_server_info(port)` so "reached the site" stops being an inference from the *next* log line. Workaround meanwhile: quit and relaunch the app, or export `CHATEKLD_API_URL` at the port `chatekld.log` reports, which is authoritative.
- **[2026-08-19]** **MCP 2026-07-28 deprecations, and the elicitation gap that keeps the write tools shut.** Roots, Sampling and Logging are deprecated with an open window; `mcp_server/server.py` should be audited before it closes. The same spec adds **elicitation** (form + URL modes), which is the missing half of this app's confirm-gating: every MCP write tool sends `confirm: true` blind, so the *agent* satisfies the gate and no human ever sees it. That is why Batch G (2026-08-26) added three READ tools and no write tool — the mapping write stays unexposed until elicitation can ask the human in the client UI.
- **[2026-08-25]** **`accession/sources.py` parses arXiv's Atom with `xml.etree` (Bandit B314).** `xml.etree` is vulnerable to billion-laughs/quadratic blowup. Input is bounded — `_http_get` caps the body at 2 MB *before* parsing — but in-memory expansion is not. The fix is `defusedxml`, which needs both hash-locked platform lockfiles regenerated; deliberately not bundled into this remediation. Dispositions for every baselined Bandit finding are in `docs/bandit-baseline-rationale.md`.
- **[2026-09-19]** **A broader dependency bump pass is owed.** Five Dependabot PRs were closed unmerged on 2026-09-19: #95 striprtf 0.0.33, #97 numpy 2.5.2, #99 annotated-types 0.8.0, #100 vitest 3→4 and #102 jsdom 26→30. The 2026-10-07 advisory remediation upgraded Vitest to 4.1.11 and regenerated both hash-locked Python graphs for three vulnerable packages; it did not address the other four pending bumps. The pip PRs changed only `constraints.txt`, while CI audits the hash-locked files. For the remaining pass, update constraints and regenerate BOTH `requirements-lock-{macos,linux}.txt` with the `uv pip compile` line in each file's header, review yanked releases, then run `npm run test:js` if jsdom is upgraded. Dependabot may reopen the remaining updates.
- **[2026-08-25]** **`requirements-lock-linux.txt` pins the CUDA build of torch.** Resolved for `x86_64-manylinux`, it carries 19 `nvidia-*`/`cuda-*` wheels plus `triton` — about 8 GB a CPU-only CI runner can never use, which exhausted ubuntu-latest's ~14 GB disk (`[Errno 28] No space left on device`) and left `main` red from the moment the hash-locked files landed. CI now reclaims the unused preinstalled toolchains (~25 GB) before installing, which unblocks it. The real fix is resolving that lock against the PyTorch CPU index so the wheels are never fetched: that regenerates a hash-locked graph for a platform the maintainer does not develop on, so it wants a deliberate pass with a verified install, not a drive-by.
- **[2026-09-04]** **A dated obligation: the nltk waiver expires 2026-12-03.** `PYSEC-2026-3740` is the one advisory `nltk==3.10.3` does not clear, and it has **no upstream fix** — the advisory's own text says "Patched versions: Not yet patched". It is waived in `.security-waivers.json` with its reachability argued: a file-sandbox bypass in the model-artifact load/save helpers (`TransitionParser.train`/`.parse`, `AveragedPerceptron.save`/`.load`, `PerceptronTagger.save_to_json`, `save_maxent_params`), none of which appear anywhere in this tree — nltk enters transitively for the `punkt_tab`/`stopwords` CORPORA through llama-index's `SentenceSplitter`, and exploiting it needs a caller-controlled model path the app never passes (it seeds both corpora itself, `launch.py::_pin_nltk_data`). The gate FAILS on an expired waiver by design, so this will surface on its own; **drop the waiver rather than extending it the moment a patched nltk ships**.
- **[2026-09-04]** **Two bulk-action gaps remain, both needing server work — recorded so the survey is not redone.** The 2026-09-04 pass compared every batch-capable route against what the UI offers; `zotlink/trash` was the only one whose server half already existed unused, and it now has a client. What is left: (a) **bulk propose+apply from the tagging table.** `_picked`/`Select all` already exist there and feed only `Copy keys` → the CLI, and the loop itself already exists as `tagging/batch.py::run_batch` (the private Maintenance `batchtags` step binds it in-process over `test_client()`). Bringing it to the GUI means a new SSE route plus the journal-retention bound `_HARMONIZE_MAX_NOTE_OPS` exists for — real value, its own session, not a drive-by. (b) **bulk duplicate-dismiss**: `POST /api/audit/dup-decision` takes a single `pair_id` where `pdf-decision` takes a list, so the UI cannot offer one atomically; low value, and the fix shape is to mirror `pdf-decision`'s list form first.
- **[2026-07-30]** **The Übersicht widget `.jsx` rides no gate** — Übersicht parses it, our CI does not (`packaging/widgets/chatekld-widget.jsx`; noted in the `widget/` module bullet, recorded here 2026-08-09 per the single-registry rule).

### Vault Maintenance (private-only)

- **[2026-08-21]** **The renamer's German/Nordic table transliterations currently always lose to the bridge fallback.** `maintenance/naming.py::transliterate` implements the plan's explicit table (`ö→oe`, `ß→ss`, …), but the bridge's bib-side join key (`audit/engine/bridge.py::_normalize_author_lastname`) NFKD-strips (`Böge→boge`, ß deleted), so a table-form stem never round-trips and `maintenance/rename.py` falls back to the bridge-compatible form for every affected surname (`boge_2023.pdf`, not `boege_2023.pdf`). Making the table forms usable requires the bib side to emit ALIAS keys (both `boge` and `boege`) in the bridge join — additive recall for hand-named `boege_*` files too — but that touches the library's most load-bearing join and is a deliberate change, never a drive-by. `test_naming.py::test_diacritic_table_forms_fall_back_rather_than_unmap` pins the current behaviour and will flag when the bridge gains aliases.
- **[2026-08-21]** **The renamer refuses a PDF referenced in any form other than the app's own `pdf:` list.** The contract is "rename + rewrite every reference in one journalled op, **or refuse**", and the general rewrite branch is unbuilt: reference rewriting needs percent-encoding/partial-path-faithful note edits under the WYSIWYG discipline, and the measured backlog had ZERO such candidates. Build it only when a referenced candidate actually appears.
- **[2026-08-25]** **A case-only rename whose destination is taken by an external file mid-staging can leave the PDF under its staging name.** `reconcile_rename_op` now recovers it to the original name where the filesystem allows and otherwise records `staged_recovery_rel` (journal + `log_vault_write` + a restore message that names the path), so the file is always *findable*. On a case-insensitive volume both spellings are one directory entry, so an automatic recovery is impossible while the foreign file holds it — the user moves it back by hand. Narrow, and now disclosed rather than silent.
- **[2026-08-25]** **The Maintenance `readmes` transaction can still need a human decision.** `recover_transaction` fails closed when a mirror changed outside the app mid-publication — correct, it must never destroy a user edit — and a pending transaction blocks later runs. The failure now names the exact directory and the step offers the confirm-gated `discard_pending_transaction` option. Discarding forfeits that generation's rollback, so it stays opt-in and is never automatic.
- **[2026-07-29]** **Maintenance LaTeX executable configuration is contained (2026-08-24):** the opt-in `latex` step is classified `external` and confirm-gated. It launches `latexmk -norc -no-shell-escape` with app-owned explicit settings, never `-r <suite>/.latexmkrc`, and Pandoc is never given the suite's `common/beamer_frames.lua`. This is a structural trust boundary: ownership/write-bit checks were removed because synchronized same-user content and validation-to-launch swaps make them insufficient. The suite's `.tex` remains untrusted input to local toolchains, which is why the capability is not labelled read-only even though generated artifacts stay in a temporary directory.
- **[2026-08-12]** **Vault-maintenance deferrals promoted out of the repatriation plan doc (2026-08-12; the doc itself deleted 2026-08-26, last carried by `06f48c6`).** These lived only in that plan doc, which the "plan docs are deleted after shipping" rule would have taken with it — the exact orphan class this registry exists to prevent. (a) A vault-root `maintain.json` policy file was considered and **deliberately rejected** — revisit only if a second vault appears; **do not add it** on general principle. (b) ~~`latex_audit.py`, `tag_audit.py` and `doc_path_drift.py` are adopted but not wired as thresholded steps~~ — **CLOSED 2026-08-21**: all three are Maintenance steps (`latexrefs`/`tagcheck`/`pathdrift`) with measured thresholds. (c) A `urls` step was designed and not built: 403/429/999/418 responses make a bare-URL liveness check unverifiable, so it would need an allowlist of hosts whose refusals mean nothing.

- **[2026-09-07]** **`find_linked_attachments` memoises the WHOLE `linked_file` path index on the Zotero SQLite's `(size, mtime_ns)`.** That is ~5 200 entries built by a full `read_items` (0.5 s), and it is rebuilt whenever Zotero syncs — which, during an active sync, is often. Fine for a rename (one per session at today's backlog of 1) and for the repoint picker; if a bulk rename ever lands, measure it before assuming the memo holds.
- **[2026-09-07]** **`_classify_references` reads every referencing note twice** — once inside `referencing_notes`' sweep, once to classify. The sweep is already a whole-vault read, so this adds only the hits; but the two now share a needle rule that lives in two places (`referencing_notes` and `_mentions`). They are pinned to agree by `tests/test_pdf_links.py`, not by construction. Merge them if either grows a third caller.
- **[2026-09-07]** **A rename whose note rewrite fails reverts the rename, but the note snapshots stay in the archive.** `journal.prune` reclaims a `.bak` through `snapshot_rel`, and these live under the rename op's `note_edits` instead — a shape prune does not know. They are small and few (the live backlog is one file); reclaim them if the rewrite branch ever runs at scale.
### Library, PDF management and note campaigns (2026-09-17 review remediation)

Remediation of the review of `68d26077..b2433953` shipped in seven commits (`28e5b85b..`); these are what it deliberately left open. The first three came from `docs/implementation_2026-09-15_library_maintenance.md`, where they lived only in that status doc.

- **[2026-09-15]** **The metadata executor and the storage control are unverified natively.** Whether a title-only PATCH still promotes `Extra: Citation Key` on Zotero 10.0.2, what a keyed field PATCH reads back on the authorized Desktop transport, and the storage control over a real Zotero observation remain to be observed in a disposable lab (blocked by the lab startup failure recorded in the status doc).
- **[2026-09-16]** **Phase 7 annotation counts are verified on fixtures only** — native and embedded counts, the unknown-count blocker and the acknowledgement have never run against a real annotated library copy.
- **[2026-09-15]** **The human bibliography evaluation is prepared, not performed.**
- **[2026-09-17]** **A verified link create whose mapping write then fails has no in-app exit.** The POST now precedes the mapping (decision D1); if the evidence changes between the created attachment and the mapping write, the op stays `needs_inspection`, `inspect` is never completable (it requires the mapping) and `restore` refuses a non-`failed` link — the mapping must be added by hand. Rare (evidence must change in that window).
- **[2026-09-17]** **A `failed` link keeps refusing a repeat link of the same file,** although `attachment_intents`' reserved key would make a retry safe. Deliberately not widened: a retry verb is a new feature.
- **[2026-09-17]** **`ZoteroNotSent` is raised only by `set_item_note`.** Tag, attachment and metadata writers still read a refused connection as generic unavailability, and a connect TIMEOUT stays `outcome_unknown` everywhere (indistinguishable from a read timeout).
- **[2026-09-17]** **A campaign undo now has an honest new partial state:** the native revert runs lock-free, so a local edit landing between it and the locked local restore is refused as `undo_incomplete` (Zotero reverted, the note kept) rather than clobbered.
- **[2026-09-17]** **Manage PDFs walks the notes once per apply,** so a note reference added mid-apply is not seen by later steps; and a symlinked or unreadable `.md` anywhere in the vault now blocks every recoverable delete (reported as an unverifiable dependency).
- **[2026-09-17]** **Completing a cleanup whose original and recovery copy are both gone records the loss as `complete`** with the reason; the app cannot learn where the bytes went.
- **[2026-09-17]** **Warm cross-report search is ~13 ms against a pre-range 7 ms** (measured on copied live stores; it was ~4.2 s per keystroke before the fix). The plan's "≤ pre-range" acceptance is not met literally: the combined corpus doubled (21,258 → 40,790 entries). The Library half is memoised on decision-store stats, not on PDF files, so a PDF added or edited on disk shows in search after the next decision write or scan — the report corpus's semantics.
- **[2026-09-17]** **`projection.read` still costs ~0.44 s warm** (a stat of every path under `biblio_articles/` and Zotero storage) and `reconciliation.rows` ~11 s cold, dominated by the pyparsing-based `_master.bib` parse inside `bibliography_snapshot`. A bib parse memo keyed on the file signature is the obvious next step.
- **[2026-09-17]** **The 1,080 `unbaselined` notes after converter `/2` were rebaselined** by the 2026-09-19 refresh-all after converter `/3`. Residual: 157 `unparsed` notes left as-is (see the T5 cleanup entry below).
- **[2026-09-17]** **The Library facts renderer relabels a hand-enumerated set of state keys** (`_FACTS_STATE_KEYS` plus `*_state`/`*_status`); a future state field named otherwise prints its raw id.
- **[2026-09-17]** **The three Library status announcers mirror visible lines through a `MutationObserver`** into static `sr-only` regions; untested in WKWebView/VoiceOver (the manual matrix above).
- **[2026-09-17]** **`reviewed_value_sha256` is stored and compared by nothing yet;** the executor reads the stored reviewed value server-side. The F8 author-window title span uses the record title's length, an approximation when the printed title wraps.
- **[2026-09-17]** **An untracked, git-ignored `docs/native_annotation_reader_2026-09-14.png` remains on the maintainer's disk.** Decision D5 deleted the tracked annotation evidence; this file is not in Git history, so it was left for the maintainer to delete by hand.
- **[2026-09-19]** **A merge's tag union does not go through `_canonicalise_tags`.** That function is a vocabulary gate: out-of-vocab Zotero tags on the retired item would be dropped instead of unioned onto the kept item. The Extra write still records `readback_differs` as applied (A3). Do not route merge tags through the vocab gate.

### Zotero item create (`zotcreate/`)

- **[2026-09-11]** **PR1 is the write path only; every proposal builder beyond "read the PDF" is unbuilt.** Shipped: transport, two-stage attempt store, journal + trash-undo, the two routes and the Not-in-Zotero form. Deferred, in the maintainer's chosen order — **(a) DOI builder** (map an accession/Crossref resolution to Zotero fields; dedup against a DOI Zotero already holds and offer "attach to existing" instead; bulk create for identifier tiers, which needs SSE and the four-phase choreography this route deliberately skips). **Worth being honest about before building it:** for a DOI row, Zotero's own *Add Item by Identifier* produces better metadata than our field mapping will — what we add is the automatic linked file, the mapping annotation and the one-click undo, not metadata quality. **(b) URL builder for `browser_print`** — decided 2026-09-11: **our own no-redirect fetch + extruct (JSON-LD/OG), accession's egress posture, always editable**; the Zotero connector's real translators were weighed and declined, because adopting an item Zotero created would add a second creation provenance to journal and undo. **(c) LLM propose for books/reports** behind app-owned YAML profiles, **default OFF like `accession_enabled`** until the profiles are calibrated on the FR/EN psychiatry corpus. **(d) GROBID** as an opt-in sidecar for stubborn articles only, config URL empty = off. **Measure before (c) and (d)**: count the residual by provenance class with and without a `source_url`/DOI, so the order stays evidence-based rather than inherited from this list.
- **[2026-09-18]** **T2 (batch create) is built** — see `POST /api/zotcreate/batch`. Residual: a Web `parent_done` resume compares the parent's current data with its creation data, so a parent Better BibTeX has since written a citation key into refuses the resume — conservative, recoverable by hand. ~~The single create's absence proof running AFTER the reservation~~ **FIXED 2026-09-19 (A4):** a `STAGE_FRESH` create now runs the moment-of-dispatch gate **before** `attempts.reserve`, so a timed-out Web observation no longer leaves the PDF at `parent_unknown` with nothing POSTed. The batch still passes `gate_after_reserve=False`.
- **[2026-09-19]** **`settle-never-created` remains `parent_unknown` only.** A `parent_done` reservation whose attachment POST was never acknowledged is still blocked; extending settle would need a complete observation that the attachment does not exist (including trash), which is a different proof. Use inspect / recover by hand. Do not extend settle to `parent_done`.
- **[2026-09-11]** **A bulk create was designed and dropped on a measurement (superseded 2026-09-18: the 286 reviewed Phase 2 drafts are the population T2 serves).** Of the 298 curated `pending` PDFs, only **5** have a cached resolution good enough to fill a form unreviewed (34 are identifier-grade, but 28 of those are withdrawn Crossref records). The other 293 need a human to check a title the extractor got right about 60% of the time. An SSE bulk path with the four-phase choreography would serve five rows. Re-measure with the harness in the `zotcreate` commits (`mapping.json` `pending` → `hash_cache` sha → `accession_cache` tier) before reopening.
- **[2026-09-11]** **Four smaller things the adversarial pass found and left.** (a) A non-curated orphan has no create verb anywhere: the button lives on `pending` rows of the Not in Zotero tab, and an un-triaged PDF is not listed there — mark it `pending` first. (b) The resume path re-reads the parent and checks its `itemType`, but not its title against the one the attempt reserved, so an item re-titled between a failed attach and the retry is adopted silently (the key is still provably ours). (c) A child the user added AND then trashed still counts as foreign in the parent revert, which refuses forever. (d) `/preview` hashes the whole PDF on the request thread for the attempt identity — one file per click, unlike `/rows`, which refuses to hash at all.
- **[2026-09-18] VERIFIED LIVE — the Web create path dispatched its first real POSTs.** One wanted item was created and kept (the maintainer's chosen route: there is no disposable api.zotero.org library and `zotcreate/attempts.py` has no reset, so create-then-trash would permanently block that PDF). Observed, and it matches what the code assumes: both unkeyed POSTs returned keys (parent `IJZJQTWB`, attachment `FV4ZHIZS`), the attachment came back `attached` under that parent as `linkMode 2` (linked_file) with the absolute vault path and the filename as its title, both readbacks matched, and the attempt store moved cleanly `parent_unknown` → `complete` (8 complete / 0 stuck). **The attachment's version was LOWER than the parent's** (119946 vs 119947), which is the recorded "never reuse a write response's version for a subsequent write" rule showing up in the create path too. A corporate author is stored `fieldMode: 1`, a bare year as `2025-00-00 2025`, and Better BibTeX assigned `citationKey` natively at creation and re-exported `_master.bib` within a minute, so the new item joined the library with no manual step. **Still unverified: the trash-undo half** — the response says `undo: "manual"` and "Automatic Desktop undo is unavailable", so reverting a create has only ever run against a stub.
- **[2026-09-18]** **A `zotcreate create` response is ~42 MB.** It embeds a whole `reconciliation` payload beside the ten fields a caller needs (`item_key`, `attachment_key`, `batch_id`, `op_ids`, …), so the CLI printed 52 MB for a one-item create. Harmless but absurd; the driver half is a deliberate pass-through, so the trim belongs in the route.
- **[2026-09-11]** **Nothing else in `zotcreate/` has run against live Zotero.** Every test is hermetic. Two things only a live run settles: whether an unkeyed parent create behaves the same on the authorized Desktop transport as on the web API, and what `server_id` the readback reports there (the attach gate compares against it). `{"dry_run": true}` proves the gates without writing.
- **[2026-09-11]** **A created item has no Better BibTeX citation key until BBT runs, so nothing is written to `mapping.json`.** The route says so in its `note` field rather than only in UI copy (a CLI caller needs it too), but the loop is not closed: the user rescans and the item joins the library through the normal bib path. Annotating the mapping with `source: "zotcreate"` once a key appears is the obvious follow-up and is not built.
- **[2026-09-11]** **Desktop undo of a create is refused, not attempted.** Same hole as the attachment undo (verified 10.0.1): a child added between the pre-lock read and the PATCH does not move the parent's version, so the HTTP guard cannot see it. On the local transport the message tells the user to trash the item in Zotero by hand; the web transport undoes normally.
- **[2026-09-11]** **MCP write stays unexposed.** CLI `papermind zotcreate preview|create|batch|settle-never-created|reinstate|attempts` is shipped. An MCP *write* tool stays unexposed for the recorded reason: every MCP write sends `confirm: true` blind, so the agent satisfies the gate and no human sees it — that waits on elicitation.

### Annual library cleanup 2026 (`cleanup/`; plan retired 2026-09-19)

The cleanup ran from 2026-09-17 to 2026-09-19 and closed all seven phases; its decision files, reports and run logs are under `BASE_DIR/cleanup/2026/` (final report `07-final-baseline.md`). The plan doc (`docs/plan_2026-09-17_annual_library_cleanup.md`, last carried by `6ff28686`) is deleted per the plan-docs rule. These are the items it left open:

- **[2026-09-19]** **T2's "attach to existing" was not built.** The plan wanted a create whose DOI Zotero already holds to become an attach under that item instead. Measured: 0 of the 52 candidate rows needed it, so the batch create refuses such a row (an admission conflict) and nothing more. Build it only when a real row needs it. It is the same verb the `zotcreate` DOI-builder entry above describes.
- **[2026-09-19]** **Two T5 features were built but not used:** the LLM loss summary for a note write, and the LLM normalisation of in-region markup in unparsed notes. The deterministic `risk_words`/`structure_markers` counts decided every Phase 5 write. The maintainer left all **157** unparsed notes as they are (151 carry Zotero-only formatting with identical words; 6 carry Obsidian wikilinks or embeds). They stay `unparsed` in `notesync status` by decision, not by oversight.
- **[2026-09-19]** **The LLM advisory for duplicate groups was never used.** The computed facts (bytes, annotation counts, note dependencies, page authentication) decided every Phase 1 group. The plan's LLM row for it stays unbuilt.
- **[2026-09-19]** **The cleanup's new Zotero items are mapped as `manual`, not `source: "zotcreate"`.** Once Better BibTeX had exported each new key, the `mapping` link steps in `02-orphans.jsonl` joined the PDFs through `core/driver.py::audit_mapping`. That call sends no `source`, and the route stores `manual` when none is sent. The pairings board therefore credits the maintainer, not the create path, for those rows. See the `zotcreate` entry on BBT keys above, where the annotation is also listed as unbuilt.
- **[2026-09-19]** **T8's "move the duplicate's note to recoverable storage" was not built.** `merge` refuses a pair whose retired record owns the Obsidian note (`note_on_retired_side`), and 0 of the live pairs needed the verb once the note-bearing record was the one kept (see the `zotrecord` bullet).
- **[2026-09-19]** **The 52 PDFs "never compared" were acknowledged, not compared.** The near-duplicate report's `no_text_paths` lists PDFs that yielded no text to compare. Phase 1 recorded them as category N with `acknowledge_not_compared`, so no row is open, but none of them was ever text-compared against the library. 41 are `z_item*` files and 11 are the 1984–2003 Bland & Altman BMJ statistics notes. The final baseline still counts 52. Comparing them needs an OCR pass (or a page-1 read through the text cache) followed by a fresh near-duplicate sweep.
- **[2026-09-19]** **Four entries lost their PDF during the cleanup, each by decision** (the needs-a-PDF list, `papermind cleanup needs-pdf` → `07-final/needs-pdf.{md,json}`; it was 4 of 5,913 baseline owners on 2026-09-19). `cavanaghAssistedDyingMoral2024` owned `pradat_cavanagh_2024.pdf`, a copy of the Pradat paper. The maintainer deleted that file permanently on 2026-09-17, and the bridge's later mis-map of `pradat_2024.pdf` (a one-page JAMA Patient Page, the `L1?` rule in the `cleanup/` bullet) was detached. `chevanceContemporaryCensorsWork2026` owned `chevance_2026.pdf`, which is Astrid Chevance's HDR mémoire. Phase 3 moved the file to `chevanceMemoirePourLobtention` and trashed the article's attachment. `prasadFemiOyebodeEntering2023` (`prasad_2023.pdf`) and `valentineMetaanalysisPersuasiveDesign2025` (`valentine_2025.pdf`) were Phase 1 category D: page authentication named another entry of the same author (`prasadConcernsThatMay2024`, `valentineWaitinglistInterventionsChildren2024`), so the file was detached and held. All four need a real PDF. Three more baseline owners are no longer records (merged or trashed), so nothing is missing for them. The 5 records whose `pdf:` list was already current at the baseline are absent from its raw rows and were not checked, but each had a PDF then.
- **[2026-09-19]** **The 90-day hold, due on or after 2026-12-17.** The holding folder (`BASE_DIR/audit/pdf_recovery/`, 4 files, 1.3 MB at the freeze, listed by `GET /api/accession/pdfs/holding`) and every journal are kept for at least 90 days after the freeze. Emptying the folder and enabling `tagging_journal_retention_enabled` are the maintainer's decisions, and neither is automatic.

### Awaiting a maintainer decision (not scheduled)

These are not defects; they are open product questions that have no record anywhere else. Each is "do it or drop it", and nobody should re-derive them from scratch.

- **[2026-07-30]** **Settings tiering** — the Card Catalog redesign (2026-07-20) contemplated tiering the Settings controls (~84 then, ~108 in the LLM Settings modal as of 2026-08-29 — `grep -c 'class="control-group'` over `templates/index.html` measures the app-wide count); **no trace survives in code or docs that it was ever deferred**. What shipped instead: per-section control counts, a live filter, and nested `advanced-controls` blocks (three in the LLM Settings modal — Vault Chat · fine-tuning, Zotero → Obsidian note sync, Restore-journal retention — plus two System Prompt Override blocks on the vault/paper tabs). The 2026-08-18 UX review's recommendation: **drop tiering, keep the filter** (it genuinely defuses the wall) — the residual costs worth a look instead are the settings being split across three surfaces (LLM modal / the unfiltered Library Audit Settings modal / per-panel checkboxes, so "where does the near-dup cutoff live?" has no single answer), the triple-nested `advanced-controls` disclosures, and two sections defaulting open (~30 controls on first paint). Still a maintainer call.
- **[2026-07-30]** **A5 — in-app "set API key" affordance** writing `BASE_DIR/.env`. Deferred at the 2026-07-05 distributability pass ("a secret-writing endpoint is left to a focused follow-up"); the current route is `.env.example` + the README, and the Settings UI states keys are env-only and never stored. Still absent by choice.
- **[2026-07-30]** **deckgen batch mode** (see *Note Refactor, deck & document generation* → deckgen batch) — the design block exists, the prerequisites are named, nothing is built.
- **[2026-07-30]** **`rag/vault.py` decomposition — what is left of Plan 9, and why.** Steps 0, 2, 4a and 4b shipped 2026-07-30 (6,300 → 5,143 lines and `index_vault` 654 → 216 at the time; on 2026-09-23 the file is 5,646 lines, `index_vault` 254 and `_index_documents_streaming` 507 — re-measure, do not trust these). Steps **1** (index meta/archive) and **3** (PDF/image caches) were **deliberately dropped**, not deferred: 209 of 326 and 324 of 508 of their lines read module globals the suite patches as `rag.vault.<name>`, so relocating them would split the archive/prune/meta family and the cache-path family across two files to move ~110 usable lines each — worse cohesion than it buys size. Step 4 went through using the shadowing-parameter technique described in the `rag/vault.py` *Module Ownership* bullet. **The plan's ≤2,000-line target is not reachable under its own ground rules.** The residual bulk is `_index_documents_streaming` (463), `_load_vault_documents` (364) and `_chunk_raw_documents` (199), which between them read `load_config`, `_iter_vault_scan_files`, `extract_structured_from_pdf`, `get_pdf_page_count`, `vision_manager` and `_MarkdownNodeParser`. They could move by the same route, but at five or six threaded parameters each the indirection starts costing more readability than the split returns — so taking them on is a **maintainer call**, not a drive-by. The remaining alternative is to relax the ratchet and re-point ~90 patch targets at the new modules: cleanest end state, but it turns the review into mechanical test edits and stops the ratchet proving the shims are complete.

### Measured and declined — do not reopen without a fresh measurement

These are not deferrals. Each was proposed, measured, and dropped on the
number recorded with it; the number is the reason, so reopening one means
re-measuring first.

- **[2026-09-02]** **The `load_config()` deep-copy sweep was measured and declined.** The plan lists `rag/engine.py` (2 sites), `refactor/resolver.py` and eight audit readers as `.get()`-only callers that should move to `load_config_readonly()`. Measured 2026-09-02 against the live `config.json`: `load_config()` costs **0.044 ms** and `load_config_readonly()` **0.0012 ms**, so the whole sweep saves ~0.04 ms per call — while the proxy RAISES on any top-level mutation, so one mis-classified site turns the retrieval prepare path or a fallback stream into a 500. `rag/engine.py`'s two sites both hand `cfg` onward (into `_build_retrieval_pipeline`, `_PreparedQuery` and `stream_with_fallback`), which is exactly where a hidden mutation would hide. The DOUBLE-HASHING half of the same finding WAS fixed (3 → 1 whole-file sha256 per snippet read). Reopen only with a per-site read-only proof, and only if a profile shows config copying at all.
- **[2026-09-02]** **The archive sweep's canvas index still stores lowered TEXT, deliberately.** The plan proposed narrowing it to basename sets beside the `.md` half, with its own warning to verify first. Verified: `refactor/archive.py`'s canvas check is a SUBSTRING test over the whole lowered file, and its own comment says why — a `.canvas` is JSON, the resolver's embed scan does not apply, and a false positive only makes a destructive move MORE cautious. Narrowing to a basename set would drop that conservatism on the one path that moves a user's only copy of an image. Measured on the live vault 2026-09-02: **zero `.canvas` files**, so the retention is 0 bytes there; it is already bounded to `_REF_INDEX_MAX_ROOTS` (2) roots. Reopen only with a canvas-heavy vault to measure and a replacement test that proves the narrower key still refuses every move the substring test refuses.
- **[2026-09-02]** **Inventory pinning every Zotero child-note body was measured and declined.** The plan proposed lazy or bounded note bodies with notesync reading from the snapshot on demand. Measured on the live library 2026-09-02 (a read-only copy of the Zotero SQLite): **1 236 child notes holding 1.06 MB of HTML in total, the largest 23.8 KB** — against an `Inventory` the plan itself describes as multi-hundred-megabyte. Making them lazy would put a SQL round-trip in `notesync/compare.py`'s path, which reads EVERY note's text on every status sweep, and would add a snapshot dependency to a join that currently owns its data, to save about a megabyte. Re-measure with the same query if the reading-note count grows by an order of magnitude; the other three parts of that finding (the corpus cancel, the SSE worker cap, the widget daemon) were built.
- **[2026-09-02]** **The accession panel's two cache-file loads and the DOI cache's twice-per-scan load/save stay uncached, on a measurement.** The plan sized these as "two 100 000-entry memos per panel open" and a per-scan double load and save. Measured on the live library 2026-09-02, `hash_cache.json` holds **5 561** entries (3.6 ms to load) and `accession_cache.json` **575** (2.8 ms), so a `/api/accession/rows` GET pays **6.4 ms** of JSON, not the hundreds of milliseconds the entry count implied; `doi_cache.json` is 5 123 entries — 2.8 ms per load and 15.5 ms per save (12.6 ms of which is the `os.path.exists` prune) — against a scan measured in **minutes**, so threading one memo through would save ~36 ms of a multi-minute run while complicating the DOI rung the 2026-08-19 measurement work depends on (its own code comment argues for the separate pass). Both are stat-keyed-memo candidates the moment the entry counts approach what the plan assumed; re-measure the two files first, since the cost tracks the entry count, not the library size.
- **[2026-09-02]** **`_migrate_legacy_caches` still runs inside the rw write lock, deliberately.** The plan paired it with the archive prune (which WAS moved out) and said to hoist it above the lock "since it touches only the cache dir". Two reasons not to. (a) It must precede the archive rename — it reads `pdf_cache`/`image_cache`/`pdf_signatures.json` from *inside* `OBSIDIAN_INDEX_DIR`, which the rename moves away — and the rename is under the lock. (b) It is a no-op for every install that has already migrated: three `exists()` checks, microseconds. Hoisting it above the lock would also mean running it on INCREMENTAL runs, since the `not is_incremental` decision is made inside the lock — a semantic change, not a reordering. Reopen only for an install that still carries a pre-split legacy cache dir, and measure that install first.
- **[2026-09-02]** **Tag Hygiene's per-request residual-PDF xattr sweep stays as it is, on a measurement.** The plan listed it beside the notesync sweep as "a filesystem sweep per request" to be precomputed during the scan. It is one — but it costs **9 ms for 1250 files** (measured 2026-09-02), because `audit/core/finder_tags.py` reads through the libc ctypes binding and has never forked. Precomputing it would mean a new `Inventory` field, a serialisation concern, a parallel read phase and a lazy fallback, all to save 9 ms on a tab the user opens by hand. The notesync half of the same finding WAS fixed (467 ms → 3 ms) because it is fifty times more expensive and is polled. Re-measure before reopening: the number moves with the residual-PDF count, not with the library size.
- **[2026-09-02]** **A preview blob is retained while its iframe is still mounted, and that is not a leak.** Phase 0's teardown releases a deck/report preview's object URL wherever the host is cleared (a failed compile, a failed PDF fetch, Cancel) and releases the Tagging deck's `V` panel on a view switch — every path that destroys the element referencing the blob. A **tab switch** was in the plan's fix shape and is deliberately excluded: `ui.js::showTab` only removes the `.active` class, so the `<iframe>` stays in the DOM and the blob backs a preview the user can come straight back to. Revoking there would break the preview on return — a UX regression, not a leak fix — and destroying the frame instead would silently discard a LaTeX compile the user paid for. Bounded at one blob per document, since regenerating now revokes. Revisit only together with an explicit "close preview" affordance.
Refuted during the review (recorded so they aren't re-raised): the bib-before-network promise held under every ordering tried (force bypasses only the cache consult, never the bib check; offline hits are never stored); no path lets title-grade confidence reach a pre-checked state (`_apply_bib_check` preserves title tiers, `_accSeedRow` gates on identifier tiers, title-tier links route to the deck); the `hash_cache.json` second-writer "worst case is a re-hash" claim held in every interleaving examined (whole-file last-writer-wins over regenerable `{path→sha}` entries; `accession_cache` has one writer via the gate; the accession path never writes `doi_cache`); and `_title_or_none` refusing a hypothetical legitimate title that parses as a DOI ("10.x … /") is corpus-implausible and degrades one candidate at worst.

Answered and closed by the same work: *does the DOI join ever collide?* **No** — zero DOIs are carried by more than one `_master.bib` entry on this library (the refusal path and `BridgeResult.doi_collisions` exist anyway). *How many no-identifier orphans are also already in the bib?* The full batch found **22 title-grade re-links** (12 corroborated, 10 title-only — including the predicted transliteration class: `haggerty`→`leeshaggerty`, `von_kaenel`→`vonkanel`, `munez`→`munoz`), served in the review list at title confidence, never bulk-checked. *Do the 34 confirmed-not-in-bib PDFs deserve a re-check?* **No** — zero of them carry a now-extractable DOI that is in the bib; the manual triage was sound.

### Closed, kept for the lesson

Struck, but retained because each carries something a future reader would
otherwise re-derive — a measurement, a suite trap, or the reason a fix took the
shape it did.

- ~~**Dead Zotero attachments pipeline**~~ — **CLOSED 2026-08-28.** `ZoteroItem.attachments` was populated and read by nothing for two years; it is now the source of the `zotero_attachments` report and the de-link verb (`api/routes/zotlink.py`). The read gained `linkMode` (which side owns the bytes) and a per-attachment annotation count (grouped query over `itemAnnotations`, guarded so a pre-Zotero-6 database reports zero rather than failing). Note that attachments whose parent is a NOTE — Zotero's embedded note images — still never reach an item and so never reach the report, which is correct: removing one would break the note that embeds it.
- ~~Rerank under `_index_mutation_lock` (W7a)~~ — **RESOLVED by AP-09 (2026-08-24).** Retrieval is split into prepare → one coherent bounded read snapshot → finish. Query rewrites/embeds and cross-encoder reranking run outside the lock; both vector backends return rank/source/score-equivalent detached candidates in regression tests.
- **[2026-07-20]** ~~Changed-chunk LanceDB deletes were per-chunk~~ — **RESOLVED by AP-08 (2026-08-24).** Changed node ids are resolved first and deleted through bounded `delete_nodes` batches before reinsertion; cancellation and later insert failure retain explicit gap accounting, cleanup, and retry isolation.
- **[2026-07-20]** ~~Build: `tagging/`/`docgen/` missing from `--add-data`; dead `pydantic_settings` flags~~ — **CLOSED 2026-07-22** (Batch H): both packages (plus `cli/`+`mcp_server/`) are in `--add-data` and the dead flags are gone.
- **[2026-07-21]** ~~Tagging journal whole-file growth cost~~ — **RESOLVED by AP-10 (2026-08-24).** The active journal is now SQLite/WAL with indexed operation/state/batch lookup and one FULL-synchronous transaction per append/update. Applied Finder/Zotero before-lists remain indefinitely retained—the database may grow, but normal writes touch only changed rows instead of rewriting history. Legacy `manifest.json` migrates once with a byte-verified no-replace backup; corrupt/divergent/unmounted storage fails closed. Lost-lock merges serialize under `BEGIN IMMEDIATE`, and durable tombstones prevent a stale writer from resurrecting a restored/pruned operation. The existing `_MAX_OPS` evictable note-history policy remains unchanged.
- **[2026-07-21]** ~~Zotero web transport residuals~~ — **FIXED 2026-07-30** (Plan 5): the 429/503 path now arms `_backoff_until` from its `Retry-After` (reads honour it, writes never sleep on it — `_arm_backoff`), and `resolve_user_id`'s cache carries a 15-min TTL (`_USER_ID_TTL_S`) so a revoked-but-unrotated key stops reporting the Zotero target as available; a *rotated* key was already handled by the fingerprint cache key.
- **[2026-07-22]** ~~Query-expansion's LLM rewrite under `_index_mutation_lock`~~ — **RESOLVED by AP-09 (2026-08-24).** Rewrite generation and every variant embedding occur in the prepare stage; the lock covers only the bounded vector/BM25/docstore read graph. Request-local BM25/reranker facades eliminate the old retune-exclusivity dependency.
- **[2026-07-23]** ~~Batch restore is a synchronous JSON request~~ — **FIXED 2026-07-30** (Plan 5): `POST /api/tagging/restore {batch_id}` now streams (per-op `{"info"}` progress + a terminal `{"restore"}` frame carrying the old JSON payload), Zotero reads still pre-lock and the reverts still run under one heartbeat-checked hold. **The same URL answers SSE for a `batch_id` body and JSON for an `op_id` body** — deliberate: only the batch is long-running, and the CLI/MCP driver only ever sends `op_id`. Validation failures are decided before streaming starts, so they keep a real status code.
- **[2026-08-12]** ~~**Finder-op restore mappings are durable only at the next 25-op checkpoint**~~ — **FIXED 2026-09-02** (performance plan, Phase 5): `journal.write_external_snapshot` persists the op's `before`/`before_raw` lists as one small fsynced JSON sidecar named by op id, written BEFORE the first xattr change and reclaimed by `prune` through the same `snapshot_rel` field the note `.bak`s use — parity with the note ops at last, and without a per-op SQLite transaction. The inline lists remain the revert's first source; the sidecar is the recovery-of-last-resort fallback. A sidecar write failure logs and proceeds, which is exactly the pre-fix durability. Pinned by `test_tagging.py::TestFinderSnapshotDurability`.
- **[2026-08-29]** ~~**A chat attachment's extraction runs on the Waitress request thread**~~ — **CLOSED 2026-09-02** (performance plan, Phase 1): both upload routes run their extractor through `api/bounded.py::run_bounded_action`, and the image arm now takes `LOCAL_MODEL_LOCK` with a bounded wait, so the worst case is back to 1 user-triggered + 1 indexer. The fix shape recorded below is the one that was taken. Original entry, for the record:
  `/api/upload` before it: a PDF is bounded by the 600 s `PDF_EXTRACT_TIMEOUT_S`
  subprocess and an image by `vision_timeout_s` (120 s), and neither takes
  `refactor/local_model.py::LOCAL_MODEL_LOCK`, so a chat image attach can run
  concurrently with an indexing vision call. Bounded and single-user, so it is
  accepted rather than fixed; the 64 MB per-file cap is what keeps the PDF case
  short in practice. If it ever bites, the fix shape is `_run_llm_action_bounded`,
  the refactor tab's daemon-with-deadline + per-action single-flight.
- **[2026-08-18]** **Hygiene note:** ~~`maintain_gui.py::_batchtags_facts` is a second consumer of `/api/audit/summary` outside both driver ratchets~~ — moot since 2026-08-21: the Tk panel was decommissioned with `scripts/vault_maintain/`; the replacement (`maintenance/steps/batchtags.py`) reads through the tagging routes, not `/api/audit/summary`.
- **[2026-08-18]** ~~**`extract-image` runs its `LOCAL_MODEL_LOCK`-gated vision call inline on the Waitress request thread**~~ — **FIXED 2026-09-02** (performance plan, Phase 1). It was the one caller of that gate never converted, with a plain blocking acquire — no deadline, no cancel poll, no 429 — so a request queued behind a pre-first-token-wedged `llm_edit` holder waited indefinitely. It now runs through `_run_llm_action_bounded`, the blueprint's binding of `api/bounded.py::run_bounded_action`, alongside the five endpoints W2 converted.
- **[2026-08-18]** ~~**`retire-tag` persists its manifest after `lock_lost`**~~ — **FIXED 2026-08-13** (the lock-lost merge-save), **hoisted 2026-08-18** into `api/routes/_batch_journal.py::_BatchJournalKeeper`, which both batch flows now share: a lost lock saves through `journal.save_merged` (`prefer_ours` for exactly this batch's ops) instead of a plain `save` that could clobber a new lock owner's records.

- **[2026-07-20]** ~~`pdf_signatures.json` vault-keying, the validator parity ratchet, the export duplicate, deck/docgen compile-fix + preview abort buttons~~ — **FIXED 2026-07-30** (Plans 7.2, 8 and 6.2). The signature map is now `obsidian_cache/pdf_signatures/<vault_key>.json`; the two legacy SHARED files are still read-and-merged so the upgrade costs no re-hashing, but they are never written again, so a save can no longer evict another vault's entries. Compile-fix aborts its SSE stream so the server cancels the worker and TERM→KILLs latexmk; preview is synchronous, so its Cancel frees the UI only and says so, and it never rewrites the `.tex`. The ratchet is `test_path_validators.py`, and it immediately caught `_resolve_out_dir` accepting a relative (CWD-anchored) path where its five siblings did not. (The two still-open items this entry used to carry live in their own entries: the `rag/vault.py` decomposition under *Awaiting a maintainer decision*, and the `apply-repair` trust model under *Note Refactor, deck & document generation*.)
- **[2026-08-08]** ~~**`static/js/summarizer.js` (moderate).**~~ **FIXED 2026-08-18** (Batch G): streamed partials survive both Stop and a mid-stream error — the "⚠ … partial" note rides the persistent notices strip (pane exports stay clean) and a kept partial is exportable. Pinned by the summarizer source ratchet in `tests/js/partialAnswer.test.js`. (Original finding, for the record: Stop overwrote the pane with "Summary cancelled…" and a mid-stream error blanked it before the retry boundary, erasing a long map-reduce summary and re-spending the whole generation.)
- **[2026-08-08]** ~~**`audit/engine/inventory.py` (moderate) — REFRAMED.**~~ **FIXED 2026-08-13** (the decided fix, verbatim): the records loop no longer `break`s on cancel — it keeps building every record and skips only the expensive `_annotations_for` reads (the skipped counts stay at the documented `-1` "unknown"), so a cancelled scan's cached inventory carries the COMPLETE records map and `tag_coverage`/reports/the widget no longer serve a hash-order-arbitrary subset as the whole library. Pinned by `tests/audit/test_inventory.py::test_cancelled_scan_still_builds_every_record`. (Original finding, for the record: the `break` dropped every remaining citation key; `manager._finalise(STATE_CANCELLED, inv=inv)` stored it and every consumer gates on presence, never state.)
- **[2026-08-08]** ~~**`static/js/audit.js` (moderate UX).**~~ **FIXED 2026-08-18** (Batch F): when a scan exists (the client holds its summary), a per-report 404 renders an in-body empty state that keeps the tablist + search bar; with no scan at all the pre-scan hide still applies. Pinned in `tests/js/auditDashboard.test.js`. (Original finding, for the record: any report 404 hid the entire tablist, so a skip-duplicates scan's Duplicates tab made every other servable report unreachable until rescan/reload.)
- **[2026-08-08]** ~~**`static/js/plainchat.js` (low-moderate).**~~ **FIXED 2026-08-18** (Batch G): a kept partial is recorded (user turn kept + assistant turn with `[interrupted]`, vault.js's pattern) so follow-ups have context and exports keep the exchange; a retry after a kept partial re-asks on the WIRE only (the final user turn is appended to the sent copy, never re-rendered or re-recorded), which also fixed vault.js's identical visual double-bubble. Pinned in `tests/js/partialAnswer.test.js`.
- **[2026-08-08]** ~~**`deckgen/prompts.py` (low).**~~ **FIXED 2026-08-18** (Batch A): the `table` / `new_section` op rules use single braces in both `deckgen/prompts.py` and `docgen/prompts.py` (they are appended AFTER `.format()`), pinned by a no-doubled-braces test over every operation in both suites.
- ~~**[2026-09-20]** **The two persisted Library-Audit status lines carry no timestamp.**~~ **CLOSED 2026-09-20** by the information-grammar pass: `ui.js::statusLine(host, {tone, text, at})` is the ONE outcome shape and every `_xSay` paints through it, so the three conventions are one. The stamp is computed at PAINT time, not ticked live — repainting is what those hosts already do on every render, so the number is right whenever it is on screen and the absolute time rides the `title`. `_nizSay` deliberately takes no stamp: it does not persist across a render, so it cannot outlive the moment it describes. Two things rode with it — the `role="status"` those two minted hosts carried was an unreliable DUPLICATE of the static announcer (WebKit drops a minted live region) and is gone, and `audit-status-line` was applied to three hosts and defined in no stylesheet.
- ~~**[2026-09-10]** **A retained note-creation attempt has no terminal state and no in-app exit.**~~ **CLOSED 2026-09-10 (Phase 3 of the library-audit UX plan).** Dispositions `satisfied` / `superseded` / `uncertain` live in the existing `resolutions` table; **no `DELETE`** — a trash-gated retry supersedes by writing a new generation row and pointing the old one at it. `satisfied` keeps creation blocked because the note exists (409 copy names the key). `superseded` is reachable only from confirmed `remote: trashed` + explicit Allow one retry → exactly one `reserve` may mint a replacement; a bare 404 (`absent_unresolved`) never unlocks. `uncertain` + Adopt key… cover `unknown_outcome` (parent+HTML verified; `creation_data=None`, no automatic trash/undo). Bookkeeping remains `resolve-create-attempt` (“Mark bookkeeping done”) and still does not release retry. Residual gaps unchanged in kind: legacy hashed subjects need the citation key in the cached inventory to list; a same-target alias under another citation key blocks but is not subject-inspectable; none of this is cross-system atomic.
- ~~**[2026-09-04]** **Two-way note sync shipped (Phases 1–5) but has NOT yet run against the live vault.**~~ **CLOSED 2026-09-11 (Library Audit UX Phase 4 live QA, headless).** Layout was already marked 1236/0; bootstrap wrote 1180 bases (53 `needs_decision` left); exercised one push, one pull (`accept_conflict`), one Obsidian-wins merge; created missing note `guanAssociationLithiumUse2026` + baselined it. Residuals: `_sync_rows` still re-reads every note per request with no memo; a converter change still invalidates every base by design (no bulk re-baseline UI); no `.app` rebuild this pass (headless `papermind server`).
- **[2026-08-26]** ~~**Note sync is create-only.**~~ **CLOSED 2026-09-04** — the two-way sync (see the entry above). Original entry, for the record: A drifted note is reported and never
  rewritten. A two-way merge (append under a dated marker, or a
  header-only refresh) was considered and declined: in every measured drift
  case the Obsidian body is the longer, edited one.
- ~~**[2026-09-07]** **Nothing here has run against the live library yet.**~~ **CLOSED 2026-09-11** — headless live QA (no `.app` rebuild): notesync baseline/push/pull/merge; rename `patel_2026_1.pdf` → `patel_5_2026.pdf` with Zotero repath; `pdf:` on five notes (Obsidian OK on koryagin); orphan map `løvvik_2014.pdf`; attach smoke (`to_link: 0`); created `guanAssociationLithiumUse2026`. Product notes from that pass live in the closed notesync / Zotero-local entries above.
- ~~**[2026-09-18]** **A rename does not carry `mapping.json` with it, and its round-trip check does not prove uniqueness.**~~ **FIXED 2026-09-19** (see the renamer in the `maintenance/` bullet). Measured on the Phase 4 run (76 renames): 37 files left a manual curation under their OLD name (found again only by content hash, `manual_hash`, and reported as dead curation), and 2 files first matched by a printed DOI got a name matching TWO records by author+year, so the next scan parked them as ambiguous. The harness repaired both after the fact (`cleanup/phase04.py::propose_curation`, phase `04c`). The rename now moves the curation inside its op and refuses such a name.
- **[2026-09-20, CLOSED 2026-09-21]** Newly created note discovery now uses exact folder entry names, so an unchanged second mirror does not report a cached-scan missing-note failure. Remaining external races still refuse safely.
- ~~**[2026-09-18]** **A detach exclusion blocks `zotcreate` for a PDF that has one.**~~ **CLOSED 2026-09-18 (maintainer decision, option A).** Only `keys` (already mapped) and `no_match` (not for Zotero) refuse a create now — in `_local_curation` (Web), the Desktop preflight and the Desktop resume, and in the reconciliation workspace's `can_create`; a PDF carrying only an exclusion is no longer categorised `Excluded` (that state now means `no_match`). The exclusion stays in `mapping.json` and keeps the automatic ladder from re-linking the wrong record, which is why clearing 32 of them one by one (option B) was declined: a grey-literature file without a DOI could then be reclaimed by the wrong record through the author+year rung. Left unchanged on purpose: an exclusion still counts as a conflict when the PDF already has a Zotero association or when a moved file carried it. Pinned by `test_zotcreate.py::…exclusion_alone…` (Web and Desktop) and `test_pdf_reconciliation.py::test_an_exclusion_alone_does_not_forbid_creating_a_record`, each proven red on the previous code; `no_match` still refuses on both paths.
- **[2026-09-11]** ~~**`STAGE_PARENT_UNKNOWN` has no in-app exit.**~~ **CLOSED 2026-09-17 for Web attempts** (maintainer decision, annual cleanup): `POST /api/zotcreate/settle-never-created {pdf, dry_run|confirm}` (`papermind zotcreate settle-never-created`) observes the ORIGINAL Web library completely — live items AND trash, verified by `Total-Results` and library version (`web_snapshot.observe`) — and refuses if any non-child item folds to the reserved title or any attachment names the file. Only then does a confirmed call MOVE the unacknowledged row into `retired_attempts` with that evidence (never a DELETE); `reinstate-attempt {seq}` / `zotcreate reinstate` puts the barrier back, and `GET /api/zotcreate/attempts` lists `retired`. This is a deliberate, maintainer-authorised exception to "a missing search result does not authorise retry"; every AUTOMATIC path still never releases a barrier. Residual: an item created and then permanently emptied from the trash is invisible to any observation. Desktop-database attempts are still refused (no complete-observation equivalent wired). Pinned by `tests/test_zotcreate_settle.py`. Original entry, for the record: A reservation whose parent POST was never acknowledged blocks that PDF forever — deliberately, since an item may exist under a key we never learned. The notesync store grew dispositions (`satisfied`/`superseded`/`uncertain`) for exactly this; the thinner twin here has none yet, and the remedy is to search Zotero for the title by hand. Build the inspect/repair verb with Batch 6, not before there is evidence it is needed.
- **[2026-08-08]** ~~**`audit/engine/bridge.py::add_matches` (moderate).** Re-assigning a curated PDF to a second citation key leaves BOTH keys owning it~~ — **CLOSED, verified 2026-09-06**: `add_matches` sweeps the rel out of every other key's list (`bridge.py` ~572–584, deleting keys that empty), the same sweep `add_no_matches`/`clear_curation` always had; a curated PDF belongs to exactly one key. Left here because the entry was still read as live on 2026-09-06 and the wrong belief shaped a whole assessment.
- **[2026-08-18]** ~~No skeleton state in the audit report body~~ — **FIXED 2026-08-19**: count-aware shimmer rows sized from the cached scan summary (`audit.js::_skeletonHtml`, capped at 14, `aria-busy` on the container and the bars `aria-hidden`), so the one-paint swap to a thousands-row table no longer jumps the page.
- **[2026-08-21]** ~~The renamer refused even a PDF referenced only by the app's own `pdf:` list~~ — **FIXED 2026-09-07**: that shape is rewritten inside the rename's journalled op (see the renamer bullet), which is what makes the `pdf:` field safe to write at library scale.

**[2026-08-11] Tag Hygiene under-reported out-of-vocabulary tags** — server half closed 2026-08-11, user-visible half 2026-08-12.

`audit/engine/reports/tag_hygiene.py` built its cross-store frequencies by iterating `inv.records.values()`, the citation-key join, so a biblio PDF the bridge never mapped contributed **none** of its Finder tags — including its out-of-vocabulary ones. Measured: a direct `getxattr` sweep of all 5 765 biblio PDFs found 5 distinct out-of-vocab Finder tags where the report showed **2**, the two whose PDFs carried mapped keys. The count was a floor presented as a total, and `audit_library.py`'s `--max-hygiene` bar inherited that.

**Fixed** by `_unmapped_finder_rows`, which sweeps all three residual bridge buckets (`unmapped_pdfs`, `ambiguous_pdfs`, `confirmed_no_match_pdfs` — all equally unreachable by key) and reports them in a **separate** `unmapped_rows` bucket with its own `--max-hygiene-unmapped` bar. Separate rather than folded in, by maintainer decision: merging would move every hygiene figure in one release and silently change what `--max-hygiene` means. Additive on the wire (`unmapped_rows`/`unmapped_pdf_count`/`unmapped_tagged_count`), so a client that ignores them sees the historical payload.

The five tags themselves were **stripped** the same day (maintainer decision: strip, don't promote) through the new path-addressed `remove-tags` — journalled, op-locked, `log_vault_write`-audited and reversible, deliberately not a raw xattr batch. Both hygiene dimensions now read 0.

**But nothing rendered it until 2026-08-12** (found by the follow-up review): `static/js/audit.js`'s `tag_hygiene` branch read only `d.rows`/`d.vocab_size`, so the bucket was recomputed on every GET — a ~1250-file `getxattr` sweep — and discarded by the client, leaving the *on-screen* "outside the vocabulary" number the same floor-presented-as-total the fix was written to remove. Marking this CLOSED while the user still saw the old number is the drift class the deferral registry exists to prevent, hence the split status above. The renderer now emits a second, clearly separated table (per **PDF**, unlike the main rows' per-paper counts, which is why folding them together was rejected) with its own swept/tagged/out-of-vocab counts; pinned by `tests/js/auditHygieneUnmapped.test.js`. `retire-tag` was blind the same way on the **write** side — its candidate loop also iterates `inv.records` — so its `[retire]` verb could not remove a tag from an unmapped PDF; it now enumerates the three residual buckets and writes through the PATH-addressed form of `_finder_remove_locked`, so there is still exactly one Finder write path and those ops revert unchanged (`test_tagging.py::test_retire_reaches_finder_tags_on_unmapped_pdfs`).

**Suite trap this exposed, worth knowing before writing another SSE test:** the SSE worker runs on a daemon thread that only executes while the response is being consumed, so a test that drains with `r.get_data()` *outside* its `mock.patch` scope runs the worker unpatched. The sibling retire tests get away with it because every value they depend on (`vault_root`, `cfg`, the manifest) is captured at request time; anything the worker resolves at CALL time — `_finder_tagwrite.read_finder_tags_full` is the one that bit — silently falls through to the real implementation and the assertion fails as a no-op write, not an error. Drain inside the patch scope.

**[2026-07-27] The eval-harness findings** — all three CLOSED 2026-07-30 (fix-plan Plan 1). All three findings fixed: (1) **Ollama thinking suppression** — every Ollama chat call now sends `think=False` with a one-shot fallback + per-model memo when a backend rejects the parameter, and a `metadata["ollama_think"]` per-call-site opt-in seam (`core/providers/ollama.py::{chat,stream_chat}_with_think_off`; mechanics + residuals in `core/llm/CLAUDE.md` → Local adapters; pinned by `test_llm.py::TestOllamaThinkSuppression`). Residual out of scope: the LlamaIndex `Ollama` LLM used by query-expansion rewrites doesn't pass `think` (local-only, default-off knob). (2) **`gen_golden_retrieval.py` diagnosability** — `--max-tokens` flag (in the job key), the 0-query failure line quotes the raw reply's length + preview, and a run whose first `EARLY_ABORT_AFTER` (5) chunks all produce nothing aborts early with the likely causes instead of absorbing ~40 identical failures. (3) **Docs wording** fixed at every point of use (`tests/eval/README.md`, the script docstring/`--help`).

**[2026-08-19] The Accession Deck plan's outcome.** The Accession Deck plan is **complete through its buildable phases** (all on 2026-08-19): Phase 1 (`5a4862c`, `464cb94`, merged `98be416` — the bridge's DOI rungs, orphans **968 → 579**), Phases 2–3 (merged `80dafeb` — the `accession/` resolver, `/api/accession/*`, the "PDFs Without Zotero" extension). **Phase 4 shipped by construction inside 2–3** (the filename-search rung and the HAL/theses.fr rescue stage are in the engine and were exercised against the full library). A full live batch resolved **all 576 remaining orphans** (33 min, 3.41 s/PDF, polite pacing): **30 links / 415 candidate sets / 52 margin-refused / 79 none**; tiers 8 `bib_doi` · 20 `xmp_doi` · 27 `printed_doi` · 35 `title_filename` · 82 `title_only` · 273 `filename_only`. The warm `accession_cache.json` is what the report's review lists serve.

**Review provenance.** Each entry's **[date]** names the review it came from;
what those reviews FIXED lives in `CHANGELOG.md`. The ranges, for anyone
tracing one back: 2026-07-20 audit (fix-plan Plan 3 closed eight of its
entries), 2026-07-21, 2026-07-22, 2026-07-23, 2026-07-29, 2026-08-08
(OpenRouter lot; nine leads adjudicated 2026-08-12, all confirmed),
2026-08-12 (`2f8ed8d..bc2e65d`), 2026-08-13 (`fbb3b40..b2ceb5f`), 2026-08-18
(`fdb2aa9..b46dd75`, batches A–J), 2026-08-19 (accession work, its review
`98be416..41c052b`, and the UI/sort/picker lot), 2026-08-21 (Maintenance
integration), 2026-08-25 (remediation of the 2026-08-24 remediation,
`b34063a`/`8499d6b`), 2026-08-26 (seven library-workflow batches),
2026-08-29 (attachments), 2026-08-30 (`e3ecfff..`, six batches),
2026-09-02 (the performance plan, `aeca4fc..21acc45`), 2026-09-03 (the UI/UX
review) and 2026-09-04 (the CI repair + the bulk de-link, `3df0161..a7afc71`).


## 2026-10-04 adversarial remediation — source and fixture scope

The dated plan is `docs/plan_2026-10-04_adversarial_remediation.md`. It covers the
10 confirmed findings and four additional reproduced boundary defects, with
current test results recorded there. Corrections do not repair past wrong-target
writes or reconstruct already-pruned journals. Unknown remote outcomes still
require verification, never automatic replay. Legacy Zotero tag journals without
complete after-images remain available but automatic restore is refused.

Native WKWebView/VoiceOver, installed Zotero, real libraries/providers, signed
build and installation remain unverified by this work. Chromium, simulated
case-sensitive paths, mocked failures and dependency checks are narrower evidence.

## 2026-10-22 page OCR preparation

Follow-up: the eight real-page baseline was executed and failed fidelity on APA
form labels/signs and AuthorB column order. Local geometry/partial-status repairs
are now implemented in the repository. The attempted real-page rerun was refused at
initial memory warning (eight not attempted, zero OCR calls). Fixture success is
not a pass for repaired OCR. No 98-page run, global extraction, separate index or
production activation has followed this repair. The paragraph below records the
earlier implementation boundary, not the current validation state.

A newly authorized isolated native/Tesseract implementation retains per-page
attempts, explicit partial states and immutable readable exports. Pinned bilingual
data was downloaded and verified, but no real-library OCR or reconstruction resume
was performed. The production loader/server remain on their existing paths.
Technical fixture checks do not qualify OCR quality, source coverage or memory
comfort on real dense pages. Initial memory pressure must return to normal before
the proposed bounded validation. Existing partial-profile replacement restrictions
remain enforced.

## 2026-10-22 Pillow source remediation

The separately authorized Pillow 13 candidate passed the full Python suite and
both platform lock audits with existing waivers unchanged. Only Pillow pins/hashes
changed; see `docs/pillow13_remediation_2026-10-22.md`. The installed runtime remains
on 12.3.0; this closes the source dependency blocker, not deployment or native
acceptance. Historical failed audits remain evidence.

## Daily simplification acceptance limits (25 October 2026)

No installed application, real vault/Zotero library, provider, WKWebView or
VoiceOver acceptance is established by this work. Preparation covers included
uncached vault sources rather than a session-only Zotero population. The compact article overview reports the preparation preview rather than a
verified final extraction assessment; unobserved/truncated rows stay explicit. Browser-local
tag preferences do not synchronize across clients. Legacy failures without a
hash or matching immutable inventory are not migrated speculatively. New failures
retain the explicit incomplete-run continuation gate. The first Library baseline
and existing writer order remain unchanged.
