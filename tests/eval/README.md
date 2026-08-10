# Prompt eval net

> Commands below use plain `python`: run them with the **project virtualenv's
> interpreter** (`<your-venv>/bin/python`, or activate the venv first) — the
> system python lacks the app's dependencies. (Hardcoded personal venv paths
> were removed 2026-07-29; this repo publishes to a public mirror.)

Two layers guard the prompts touched by the 2026-06 prompt audit.

## 1. `test_prompts.py` (repo root) — model-free, runs in CI
Pins the *structural* invariants of every prompt: grounding language, the
untrusted-content guards, `{context_str}`/`{query_str}` placeholders, the
single-paper sentence-count consistency, the consistent RAG citation wording,
the agent preamble's stop condition + efficiency nudge, the deckgen LaTeX
contract, and the system-prompt length cap. When you intentionally change a
prompt, update the matching assertion in the same commit.

```bash
python -m pytest test_prompts.py -q
```

## 2. `tests/eval/` — answer-quality golden set
A self-contained fixture vault (`fixtures/`) plus golden pairs (`golden_qa.json`)
that assert what a *grounded* answer must cite/contain (and must not contain, to
catch ungrounded answers).

- `scoring.py` — pure load/score/report (no models).
- `test_scoring.py` — hermetic tests for the scoring layer (runs in CI).
- `run_eval.py` — **live** runner; opt-in via `RUN_LIVE_EVAL=1`.

### Running the live eval (needs Ollama + pulled models)
```bash
RUN_LIVE_EVAL=1 \
EVAL_PROVIDER=ollama EVAL_MODEL=llama3.2 EVAL_EMBED=nomic-embed-text \
python -m tests.eval.run_eval
```
It writes only to a throwaway `CHATEKLD_BASE_DIR`, never your real app data.

### Before/after comparison
Run it, record the pass rate, switch prompt revisions (`git stash` or check out
the pre-change commit), run again, compare. The **delta** is the signal — this
is a regression tripwire, not a semantic grader.

---

# Retrieval eval harness

Layers 1 and 2 above score *answers*. This third layer scores **retrieval
itself** — did the right documents come back, before any model wrote a word.

## Why it exists

Until 2026-07-27 retrieval quality was unmeasurable. Every knob (RRF leg
weights, similarity cutoff, rerank pool sizing, reranker model, hybrid on/off)
was tuned by feel, and the answer-level pass rate was far too noisy to attribute
a change to the retriever — Track 5.6 hit exactly this wall and fell back to
eyeballing raw embedding vectors. The harness replaces that with recall@k, MRR
and nDCG@k on a per-vault golden set, and a Δ-vs-baseline table across knob
combinations.

Three pieces:

| Piece | What it is | Hermetic? |
| --- | --- | --- |
| `tests/eval/retrieval_metrics.py` | Pure scoring: metrics, golden-set loading, the Δ table. Stdlib only, **zero project imports**. | Yes — `test_retrieval_metrics.py` runs in CI. |
| `scripts/gen_golden_retrieval.py` | Builds a golden set from your own vault by *query inversion*. | The tool is maintainer-run; `test_golden_gen.py` covers it hermetically. |
| `scripts/eval_retrieval.py` | Replays the golden set against the real index and sweeps knobs. | Same — `test_eval_runner.py` drives it with a fake manager. |

**Metrics are document-level.** A ranking is first collapsed to one entry per
document at that document's best rank, so a note that contributed three chunks
in the top 3 counts once, at rank 1 — otherwise chunk-level ties would inflate
recall for no retrieval benefit. Every path comparison goes through
`norm_source()` (NFC + casefold), because the target vault is bilingual FR/EN on
an APFS volume that hands out NFD filenames.

## Workflow

### 1. Generate a golden set

```bash
# Dry run first: samples documents and prints them, no LLM call, no write.
python scripts/gen_golden_retrieval.py --dry-run

# Real run, LOCAL model: ~40 documents × 2 queries (1 FR + 1 EN) ≈ 80 queries.
python scripts/gen_golden_retrieval.py --provider ollama
```

It reads `docstore.json` directly (via `SimpleDocumentStore.from_persist_dir`),
samples a seeded spread of documents, takes each one's longest chunk, and asks
the configured chat model for the search queries a user would type to find that
passage. The chunk's own document is the ground truth. Backend-agnostic — the
docstore is populated on lancedb too (`store_nodes_override=True`) — and it
constructs no index, no embedding model and no vector store.

> ### ⚠ Your vault text is the prompt
>
> Inversion sends **the longest chunk of every sampled note, verbatim**, to the
> chat model — ~40 × 1800 characters per run. For a clinical vault that is
> special-category personal data and the disclosure is irreversible, so an
> **online provider is refused by default**. Sending it off-machine takes both
> `--allow-online` *and* an interactive y/N confirmation naming the destination
> and the payload size. `--provider ollama` is the safe path **on the privacy
> axis** (see the thinking-model note below for the *quality* axis) and is what
> the command above uses — note that the bare command inherits whatever
> `provider` is configured, which may well be an online one.

> ### Thinking models (fixed 2026-07-30; residual caveat for old servers)
>
> Until 2026-07-30 a reasoning-capable Ollama model produced **zero queries on
> every chunk**: Ollama counts thinking tokens against `num_predict`, this
> script caps the reply (512 by default, `--max-tokens` to override), and the
> app never reads `message.thinking` — so the model spent the whole budget
> reasoning and returned an empty string (measured on `qwen3.5:9b-mlx`: 1940
> chars of thinking, `content == ""`, `eval_count` exactly 512).
>
> Every Ollama chat call now sends `think=False` (`core/providers/ollama.py`,
> one fallback retry without the parameter when a backend rejects it), so
> thinking models answer normally here. The **residual**: an Ollama server too
> old to know the `think` parameter falls back to the pre-fix behaviour — on
> such a server the old symptom returns, now diagnosable instead of silent (the
> 0-query failure line quotes the raw reply's length + preview, and a run whose
> first 5 chunks all produce nothing aborts early with the likely causes
> instead of failing ~40 times over ~40 minutes). Quick triage stays:
>
> ```bash
> ollama show <model> | sed -n '/Capabilities/,/^$/p'   # "thinking" ⇒ suppressed via think=False
> ```

**No app-closed gate** for this one: it is strictly read-only on the index
directory, and its only writes land under the output file's own directory.

Useful flags: `--num-docs 40` `--per-chunk 2` `--min-chunk-chars 200`
`--seed 0` `--provider` `--model` `--out` `--allow-online` `--no-resume`
`--force` (overwrite guard) `--dry-run`. Exit codes: `0` ok/dry-run · `1` zero
queries produced (check the provider/model) · `2` precondition failed (no
`docstore.json`; output exists without `--force`; `--out` resolving inside the
repo; online provider refused or declined).

Three things it enforces rather than merely asking for:

- **Leakage guard.** The prompt tells the model not to copy the passage; the
  script *verifies* it. Any query sharing more than 5 consecutive tokens with
  its source chunk (accent/case-folded) is dropped and counted in
  `generator.leak_dropped`. Verbatim-copied text scores near-perfectly on BM25
  and not on dense retrieval, so without this the RRF sweeps would reward
  upweighting BM25 on a generator artifact rather than on retrieval quality.
- **Nonce'd fence.** The excerpt is wrapped in a per-call `uuid4` tag
  (`wrap_untrusted`'s convention), not a literal delimiter a downloaded PDF or
  web clipping could contain and close early.
- **Resume.** Progress lands in a `<out-stem>.partial.json` sidecar after every
  completed document, so an interrupted 40-call run re-sends only what is
  missing. `--no-resume` ignores it. The sidecar is removed only when every
  document succeeded.

> **Memory:** it loads the whole `docstore.json` into RAM. On the maintainer's
> vault that file is ~1.4 GB, so expect a multi-GB resident process for a minute
> or two. Close the app first if memory is tight (not required for correctness).

Output — `BASE_DIR/eval/golden_retrieval.json`, schema v1:

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-27T10:00:00+00:00",
  "vault_path": "/Users/…/Vault",
  "embed": "nomic-embed-text",
  "generator": {"provider": "ollama", "model": "…", "seed": 0,
                "num_docs": 40, "per_chunk": 2},
  "queries": [
    {"id": "q0001", "query": "…", "lang": "fr",
     "relevant_sources": ["notes/foo.md"],
     "origin": "inverted", "chunk_preview": "…"}
  ]
}
```

### 2. Curate it (do not skip)

The generator is a first draft. Open the JSON and:

- **Delete nonsense queries** — a model sometimes emits the passage's title, a
  meta-question ("what does this document say?"), or verbatim copied text. Those
  measure string matching, not retrieval.
- **Add missing `relevant_sources`** — if two notes genuinely answer a query,
  list both. Recall counts a hit on any of them.
- **Set `origin: "curated"`** on every query you kept or edited by hand. The
  field is informational today; it is the seam for graded qrels (UMBRELA-style
  LLM judging) in a later version, and it tells future-you which rows were
  reviewed.

### 3. Run a baseline

**Quit the app first.** The manager's locks are in-process only; a live app
holds the same index, rewrites the BM25 sidecar and checkpoints the store, so
two processes would race on both — and the runner additionally rewrites
`config.json` for config cells, which a running app can overwrite from its own
debounced Settings save. This is **enforced**, not requested: a live server
(pid + `/api/health`, the same check `papermind server` uses to refuse to start
beside one) aborts with exit 2, and `--yes` does not bypass it. `--yes` only
skips the interactive confirmations.

```bash
python scripts/eval_retrieval.py
```

It builds a fresh `ObsidianVaultManager` against the **real** index and
`BASE_DIR` (unlike `run_eval.py`, which redirects to a throwaway dir — the whole
point here is to measure the index you actually use), replays every golden query
through the LLM-free `retrieve()` with `top_k_explicit=True` (bypassing the
context-window clamp, for determinism), and prints the metric table.

**Sanity threshold:** inverted queries should mostly retrieve their own
document. If **recall@5 is below ~0.6**, do not trust any sweep run on top of
it — curate harder (or regenerate with a better model) first. A weak golden set
makes every cell look identical, which reads as "no knob matters".

### 4. Sweep knobs

```bash
# Canned cell lists:
scripts/eval_retrieval.py --preset reranker-ab     # current vs off vs bge-v2-m3
scripts/eval_retrieval.py --preset rerank-onoff
scripts/eval_retrieval.py --preset rrf-sweep
scripts/eval_retrieval.py --preset cutoff-sweep
scripts/eval_retrieval.py --preset pool-sweep

# Or your own cells:
scripts/eval_retrieval.py --cells my_cells.json
```

A **cell** is one knob combination:

```json
{"cells": [
  {"name": "no-hybrid",  "retrieve": {"hybrid_enabled": false}},
  {"name": "wide-pool",  "retrieve": {"rerank_pool_ceiling": 200}},
  {"name": "bm25-heavy", "config": {"vault_rrf_dense_weight": 1.0,
                                    "vault_rrf_bm25_weight": 2.0}}
]}
```

- `retrieve` — keys passed straight into `retrieve()`, validated against an
  allowlist mirroring its signature.
- `config` — knobs the engine reads from `config.json` (there is no kwarg for
  them): the RRF leg weights, `vault_rerank_pool_multiplier` / `_floor`,
  `vault_reranker_device`, the wikilink caps, `vault_thesaurus_max_variants`,
  `context_window`. Sweeping one means rewriting **your real config file**, so
  before the first override the runner writes the original bytes to
  `config.json.evalbak` (atomic + fsync'd) and restores from it in the per-cell
  `finally`, the outer `finally`, an `atexit` hook, and a `SIGTERM` handler,
  verifying the restore by read-back before removing the sidecar. If a run is
  `SIGKILL`ed or the machine loses power, the sidecar survives and the **next
  run refuses to start**, printing both paths — deliberately not auto-restoring,
  since you may have edited settings in between.

Both maps validate **values**, not just key names: an out-of-range or malformed
value **aborts with exit 2 naming the key and its accepted range**, rather than
being clamped (a clamped cell would report a Δ for a setting that never existed)
and rather than being written to your config unchecked. `reranker_model` gets
the same HF-repo-id shape check the app's own `/api/config` applies, with a
parity test against it — that value reaches an arbitrary model download.

An unknown key in either map likewise **aborts with exit 2 naming it**, checked
before the confirmation prompt — a typo'd knob must never be silently ignored
and reported as "no difference". An implicit `baseline` cell (pure config
resolution) always runs first and is the Δ reference.

The runner also refuses (rather than producing meaningless numbers) when: the
**app is running** (detected via `core/server_info.py` + the `/api/health`
probe, the same definition `papermind server` uses — `--yes` does *not* bypass
this); `cfg["embed"]` **differs from the index's recorded embed model**, which
would search a foreign vector space and score ~0 on everything; or a preset
needing a reranker is run with `vault_reranker_model` unset.

It prints a **pre-flight** first: cells × queries, a measured ETA from a timed
warm-up, the ±1/√N noise floor for your golden-set size, each cell's effective
knobs (including the *computed* rerank pool size), and any overrides detected as
**inert** — an override that provably cannot reach an active code path is
flagged loudly next to its row instead of quietly printing Δ = 0.000.

Other flags: `--k 1,5,10` `--limit N` (seeded subsample) `--seed`
`--golden FILE` `--out FILE` `--index-dir DIR` `--no-warmup` `--yes`.

### 5. Read the results

The table goes to stdout; the full record is written to
`BASE_DIR/eval/results/retrieval_eval_<UTC-stamp>.json`:

```json
{
  "schema_version": 2, "run_at": "…", "partial": false,
  "golden_set": "…/golden_retrieval.json", "golden_sha256": "…",
  "num_queries": 78, "excluded_stale": 2, "ks": [1, 5, 10],
  "index": {"vector_backend": "lancedb", "embed": "…", "vault_path": "…"},
  "cells": [{
    "name": "baseline", "retrieve_kwargs": {}, "config_overrides": {},
    "reranker": "loaded", "inert_overrides": [],
    "scored_queries": 78, "failed_queries": 0, "aborted": false,
    "metrics": {"recall@1": 0.62, "recall@5": 0.85, "recall@10": 0.90,
                "mrr": 0.71, "ndcg@1": …, "ndcg@10": 0.78, "n": 78},
    "latency_ms": {"p50": 210, "p95": 480, "mean": 240},
    "per_query": [{"id": "q0001", "first_hit_rank": 1,
                   "first_hit_source": "notes/foo.md",
                   "retrieved": [["…", 0.81]]}]
  }]
}
```

`golden_sha256` pins which golden set produced the numbers — two runs are only
comparable when it matches. `per_query.first_hit_rank` is the fastest way to
find *which* queries a cell regressed; read the document name from
`first_hit_source`, **not** from `retrieved[rank-1]` — `first_hit_rank` is a
*deduped document* rank while `retrieved` is the raw chunk list, so indexing one
by the other names a different document whenever a note contributed two chunks.

The record is rewritten after **every cell** with `partial: true`, cleared on the
final write — a sweep killed at cell 4 keeps cells 1–3.

**Schema v2** replaced the `reranker_failed` boolean with the `reranker`
tri-state (`none` | `loaded` | `failed` | `unknown`), derived from the cell's own
resolved kwargs plus the manager's reranker cache key. The boolean was unsound
in both directions: a `reranker_enabled: false` cell inherited the previous
cell's flag, and — the dangerous one — a cell whose `reranker_model` resolved
empty ran rerank-free while being recorded as the reranked baseline, making
every Δ a comparison against a mislabelled reference. Results files from before
this change do not carry the tri-state and are not comparable.

## Reranker A/B — worked example

The one sweep worth running first:

```bash
# App closed.
python scripts/eval_retrieval.py --preset reranker-ab
```

Three cells: `baseline` (your configured reranker), `rerank-off`, and
`bge-v2-m3` (`BAAI/bge-reranker-v2-m3`, the multilingual FR/DE/EN candidate). The
first run downloads bge (~2.3 GB) into `~/.cache/huggingface` — that is a
feature: a script process never runs `launch.py`, so it downloads freely, and it
leaves the cache warm.

Read `mrr` and `ndcg@10` (rank-sensitive) rather than `recall@10` (a wide net
hides ordering changes). If bge wins by a margin you believe, set
**LLM Settings → Reranker model** to `BAAI/bge-reranker-v2-m3` and **restart the
app** — a session launched with the old model cached runs with
`HF_HUB_OFFLINE=1`, so it cannot fetch a model changed mid-session. Confirm the
new reranker loads in `chatekld.log` on the next vault chat. No reindex is
needed: reranking is query-time.

## Caveats

- **The app must be closed for the runner** — and this is now *detected*, not
  merely asked: a live server (pid + `/api/health`) refuses the run, and `--yes`
  does not bypass it. Not required for the generator (read-only).
- **The runner writes into the index directory.** It reads the vectors, but a
  cold or stale BM25 sidecar is *rebuilt and persisted* inside the first cell's
  warm-up (`<index>/bm25_index/`, a multi-minute re-tokenise and a ~1.9 GB write
  on a large vault), and wikilink expansion persists `wikilink_sidecar.json`.
  Both are regenerable caches, never vault content — but "read-only" would be a
  false description, and the runner warns up front when the sidecar looks stale.
  It also rewrites `config.json` for config cells (see above) and writes the
  results file. It does **not** touch the vault, the vectors, or the docstore.
- **RRF leg weights ≠ 1.0/1.0 flip the fusion mode** (`reciprocal_rerank` →
  `relative_score`, the only LlamaIndex mode that applies `retriever_weights`).
  The two score on different scales, so such a cell is comparable on **rank**
  metrics only, never on raw scores. The runner prints this warning per cell.
- **`cutoff-sweep` measures the dense-only path**, deliberately. The similarity
  cutoff is attached only when there is no reranker *and* no fusion, so with the
  shipping defaults (both on) every cutoff cell was byte-identical to baseline —
  ~20 minutes of compute printing four Δ = 0.000 rows that read as "the cutoff
  doesn't matter", which is false. Its cells now switch the reranker, hybrid and
  query expansion off so the knob is actually live; its numbers are therefore
  **not** comparable with the other presets' cells.
- **Reranker state is reported as a tri-state**, positively derived, not
  inferred from a sticky flag — see the schema-v2 note above for the two ways
  the old boolean lied.
- **A per-query retrieval failure excludes that query, it does not score it as a
  miss** (a miss is a retrieval outcome; a failure is the absence of one). Five
  consecutive failures abort that *cell*, not the run. Counts land in
  `failed_queries` / `scored_queries`. This matters when the embed backend isn't
  running — likely, since you just quit the app.
- **Stale qrels are excluded and counted**, never scored as misses: queries
  whose relevant documents have all left the index drop out, and the count lands
  in `excluded_stale`. Regenerate the golden set after a large vault
  reorganisation.
- **Never commit a golden set or a results file.** Both live under
  `BASE_DIR/eval/` precisely because they embed excerpts of the user's vault.
  `.gitignore` covers the filenames and `/eval/`, and the generator refuses an
  `--out` resolving inside the repo — three layers, because this repo has a
  private→public mirror workflow where one `git add -A` would publish clinical
  excerpts.
- **The golden set is tied to its embedding space.** Changing `embed` is a
  reindex, and the qrels only mean something in the vector space the index was
  built in — the runner **refuses to run** when the configured and recorded
  embed models disagree (every number would otherwise be garbage, while the
  banner printed the index's model and concealed the cause).
- **A sourceless retrieved chunk consumes a rank slot** (2026-07-27). It used to
  be dropped, which silently promoted every document below it — `["", "b.md"]`
  scored `rr = 1.0` where the honest answer is `0.5`. Results recorded before
  this, from an index that ever returned a chunk with no `source`, over-report
  and must not be diffed against a newer run. Fully-stamped indexes are
  unaffected (byte-identical numbers).
- **The generator loads the whole docstore into RAM** (see above). The runner
  does **not**: it derives the known-source set from `indexed_materials.json`
  (~1 MB), falling back to a streamed `ref_doc_info` pass and only then to a
  full load.
