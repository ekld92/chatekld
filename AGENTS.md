# CHERQAiN project instructions

This application uses Flask, PyWebView, SQLite, LlamaIndex, Ollama and LM Studio.
Read this file first. `AGENTS.md` and `CLAUDE.md` must remain identical.

## Current scope and delivery policy (22 October 2026)

- The indexing/extraction/embedding upgrade plan is abandoned. Dated upgrade
  plans and execution prompts are historical evidence, not active instructions.
  Do not resume benchmarks, migration, profile activation or model comparisons
  without a new explicit request.
- Keep the existing index and embedding model. No reindexing is authorized by
  stabilization or routine maintenance work.
- Prefer small fixes for observed user-facing problems. Validate the changed
  behavior with focused checks; do not run the full suite for every edit.
- Direct ordinary pushes to `main` are supported; PRs are optional. Commit/push
  still require user authorization. Never force-push or delete `main`.
- Private-repository GitHub CI is manual (`workflow_dispatch`), with no required
  status checks on `main`. Public releases omit GitHub workflows and Dependabot
  configuration; run verification locally before publication. Keep data integrity
  and runtime security protections intact.

## Documentation ownership

- [PROJECT_REFERENCE.md](PROJECT_REFERENCE.md) retains detailed workflow, module,
  API, SSE, test and build contracts. Consult the relevant section before edits.
- [KNOWN_ISSUES.md](KNOWN_ISSUES.md) retains open gates, dated measurements,
  declined changes and closed decisions. Historical verification is version-bound.
- Deep implementation contracts: `rag/CLAUDE.md`, `core/llm/CLAUDE.md`,
  `core/agent/CLAUDE.md`, `audit/CLAUDE.md`. Their `AGENTS.md` entry points direct
  agents to those same files without duplicating the contracts.
- README and `static/help/guide.md` describe current user workflows.
- `LIBRARY_WORKFLOWS.md` is the public task chooser for Library users;
  `CLI_GUIDE.md` is the public command/effects runbook for people and agents.
  Inspect current `--help` and server evidence before using an example command.
- CHANGELOG records shipped changes; dated `docs/` records are evidence, not
  authority to run an operation.

## Scope and preservation

Honor read-only instructions. Inspect status and diffs before editing or committing;
retain concurrent work and stage only exact reviewed paths. Never infer authority
for real-library writes, scans, migrations, application restarts or provider spend
from historical plans or verification records. Report verified defects separately
from hypotheses and unmet verification gates.

## Data and write boundaries

- User data belongs under `BASE_DIR`, normally Application Support/ChatEKLD.
  Credentials are environment-only; never persist them in config or logs.
- Vault writes require explicit action-specific confirmation, path containment,
  operation locking, stale-evidence checks and `log_vault_write` auditing.
  Reuse existing writers and journals; never add an unjournalled shortcut.
- Library Audit scans are manual. Cached reports represent one completed scan;
  report GETs are observational and do not automatically map or mutate data.
- The vendored `audit/` engine depends only on `audit/config.py`, never app-side
  modules. Inject app capabilities through its established seams.
- `mapping.json` writers hold the shared process mutex and stable sidecar flock,
  retain all fields, and publish atomically with durability checks.
- Compare stored file identities through `same_identity`, ignoring volatile `dev`.
  Current device numbers may establish current same-volume conditions only.
- No direct Zotero-storage deletion or permanent purge. Annotation transfer was
  abandoned on 2026-09-16; do not resurrect it. Annotated duplicate dependencies
  remain visible and require an explicit decision before physical removal.
- Retain reservations after uncertain remote outcomes. Never silently re-mint
  keys, retry an unkeyed create, change transport, or advertise automatic undo.
- Zotero local reads/writes remain bound to the observed server/database ID.
  Local keys and versions never transfer to the Web transport.
- Historical native results on Zotero 10.0.1 do not verify installed 10.0.2.
  Reserved-key/version-zero attachment POST was unsupported (`primaryData not
  loaded`); preserve the reservation. Native merge/undo was fixture-only without
  attachments/Better BibTeX. Title-only PATCH may promote Extra's Citation Key;
  omission of fields does not prove unchanged metadata. See the private prototype.
- Notes Sync mirror rewrites require verified Git before-images for every target;
  they have no universal Restore snapshot. Experimental campaign executors remain
  unavailable in production. Preserve recovery evidence and partial states.

## Provider and agent contracts

- Online providers serve chat and explicitly confirmed OCR/vision index runs;
  embeddings and retrieval remain local.
- Resolve models with the per-provider configuration keys and shared fallback
  policy. Failures must remain visible; fallback occurs only at the established
  pre-first-token boundary. Never replay a streamed partial answer automatically.
- Preserve opaque reasoning/provider state only for its matching provider/model.
  Validate tool arguments and names; wrap each external result as untrusted text
  with its own nonce. Respect cancellation, iteration and deadline gates.
- GPT-6.1 Sol tools use the targeted Responses adapter on the official endpoint.
  Campaign generation is not proof of the application tool dispatcher.
- Use existing usage contracts; disclose estimates and unknown pricing rather than
  inventing costs or accounting failures as successful calls.

## Security, UI and module boundaries

- Loopback-only service, one trusted OS user. `X-Requested-With` is an origin/CSRF
  signal, not authentication. Keep centralized Host/Origin guards and body limits.
- Preserve enforced CSP, response hardening and `ui.js::sanitiseHtml`.
  Runtime third-party JavaScript remains vendored, never a CDN dependency.
- API PDFs use authenticated fetch plus managed blob URLs, cancellation and
  revocation; never direct `/api` iframe navigation.
- Follow PROJECT_REFERENCE's JS import hierarchy and its executable test.
  Keep pendingReview/pdfManagement/libraryRun edges one-way.
- Structured SSE failures precede `[DONE]`; preserve partial text and expose errors.
- Chromium does not verify WKWebView or VoiceOver. Visual and native acceptance
  require their actual surfaces; do not equate compilation with visual QA.

## Tests and delivery

Use the project Python 3.12 environment. Choose checks relevant to the change;
for documentation/workflow edits, check documentation, YAML and the diff. Run
broader suites for cross-cutting changes or when explicitly requested. The full
validation menu below remains available locally and through private-repository
manual CI; it is not a mandatory sequence for each commit or push:

```sh
python scripts/check_static_scope.py compile
ruff check .
python scripts/check_static_scope.py bandit
python scripts/check_mypy_ratchet.py
python -m pytest -q
npm ci
npm run test:js
PAPERMIND_PYTHON=python npm run test:browser
python scripts/check_dependency_audits.py
python scripts/check_docs.py
```

- Shared production scope is `scripts/check_static_scope.py`; private roots are
  omitted only when absent.
- The mypy ratchet is enforced; never inflate existing per-file budgets to pass.
  Bandit baseline dispositions remain explicit; never regenerate the baseline.
- Tests use a disposable `CHATEKLD_BASE_DIR`; no real vault, Zotero or paid calls.
  Do not load live credentials into tests. Join post-index daemons inside each
  temporary patch scope; helpers belong in `suite_helpers`, not `conftest` imports.
- Driver additions need endpoint replay coverage; consumed response fields need
  projection coverage. Test through the driver seam, not above it.
- The standalone `python -m audit --check` prints findings but is not a
  threshold gate. `audit scan` reads the live library and saves reports; dry runs
  may still call models or write application decision files. Consult
  `CLI_GUIDE.md` for command effects and reviewed apply/recovery sequences.
- App build is macOS arm64, ad-hoc signed, with external local model runners and
  per-machine model caches. Preserve the single build entry point.
- Public source delivery uses `scripts/build_public_artifact.py` from a clean
  checkout, with verification enabled. It excludes private docs, Maintenance,
  cleanup, campaigns, prototypes and machine settings. Do not use old destructive
  branch-switching publishing recipes. Public publication is a separate action.
