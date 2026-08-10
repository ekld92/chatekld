"""Pure metrics + reporting for the retrieval eval harness.

This module is the scoring half of the retrieval harness: it knows how to load
a golden retrieval set, score one query's ranked result list against it, and
render a Δ-vs-baseline table across knob "cells". Everything that actually
touches an index, a provider or the config file lives in the ``scripts/``
runners, so this half is unit-testable hermetically (see
``test_retrieval_metrics.py``) and — deliberately — has **zero project
imports**: it must be importable before ``CHATEKLD_BASE_DIR`` is fixed.

The unit of relevance is the *document*, not the chunk. Retrieval returns
chunks, and several chunks of one note routinely occupy consecutive ranks; a
chunk-level rank would let one lucky note inflate recall@k and nDCG@k. Every
ranked list is therefore collapsed doc-level (first occurrence wins, see
``dedupe_ranked_sources``) *before* any metric is computed, so "rank 1" always
means "the best-ranked chunk of the best-ranked document".

A retrieved chunk whose ``source`` is empty (the indexer's metadata fallback
chain is ``source`` → ``file_path`` → ``""``, so a partially-stamped index can
hand one back) is *unidentifiable*, not *absent*: it still consumed a rank slot
that a relevant document did not get. It is therefore kept as an opaque
placeholder (``UNIDENTIFIED_PREFIX``) that can never match a golden source —
see ``dedupe_ranked_sources`` for the rank-accounting rationale. This is a
metric-definition change (2026-07-27): a results file recorded before it, from
an index that ever returned a sourceless chunk, over-reports rr/recall/nDCG and
must not be diffed against a run recorded after it. Runs over a fully-stamped
index are unaffected — no placeholder is emitted and every number is identical.

All comparisons run through ``norm_source`` (NFC + casefold) on BOTH sides.
The target vault is bilingual FR/EN on macOS, where the filesystem hands back
NFD filenames while a hand-curated golden file is typed in NFC, and APFS is
case-insensitive — so ``Café.md`` and ``café.md`` are the same document
and a byte comparison would score a perfect hit as a miss.

Metric definitions (binary relevance, over the DEDUPED ranking):

* ``recall@k`` — relevant documents appearing in the top *k* ÷ total relevant
                 (the denominator is the DEDUPED relevant set, so a golden
                 entry that lists one document twice cannot make recall
                 unreachable).
* ``rr``       — 1 / rank of the first relevant document (0.0 when none hit).
* ``nDCG@k``   — Σ 1/log2(rank+1) over relevant hits in the top *k*, divided by
                 the ideal DCG of ``min(|relevant|, k)`` hits stacked at the top.

Ranks are positional: this module never sees retrieval *scores*, so a tie is
resolved by whatever order the retriever emitted. Two runs that break a tie
differently genuinely retrieved different top-*k* lists, and the metrics are
meant to show that rather than paper over it.

Empty inputs (no results, no relevant sources, no scores) yield 0.0. Nothing
here raises on empty data — a query that retrieved nothing is a *miss*, not an
error. Malformed *structure*, by contrast, always raises (see ``load_golden``).
"""
from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

#: Golden-set schema this module understands. ``load_golden`` refuses anything
#: else rather than silently mis-reading a future (graded-qrel) format.
SCHEMA_VERSION = 1

#: Default cutoffs. Mirrors the runner's ``--k 1,5,10``.
DEFAULT_KS: Tuple[int, ...] = (1, 5, 10)

#: Prefix of the opaque stand-in ``dedupe_ranked_sources`` substitutes for a
#: retrieved chunk with no usable ``source``. NUL cannot appear in a filesystem
#: path, so a placeholder can never collide with ``norm_source`` of a real path
#: and can therefore never be counted as a relevant hit — which is the whole
#: point: it must occupy a rank without ever earning one. (A golden file that
#: literally contained a NUL byte in a path could in principle collide; such a
#: path cannot exist on disk, so it would be an unscoreable entry either way.)
UNIDENTIFIED_PREFIX = "\x00unidentified#"


@dataclass(frozen=True)
class RetrievalQuery:
    """One golden query plus the documents a good retrieval must surface.

    ``relevant_sources`` are vault-relative paths as the indexer stamps them
    (``RetrievedChunk.source``). ``origin`` is ``inverted`` for a generated
    query and ``curated`` once a human has vetted/edited it — v1 scores both
    identically, the field exists so graded qrels can be added later without a
    schema bump.
    """

    id: str
    query: str
    lang: str
    relevant_sources: List[str]
    origin: str = "inverted"
    chunk_preview: str = ""


@dataclass(frozen=True)
class QueryScore:
    """Per-query outcome. ``hit_ranks`` are 1-based DEDUPED ranks, ascending."""

    id: str
    hit_ranks: List[int] = field(default_factory=list)
    recall_at: Dict[int, float] = field(default_factory=dict)
    rr: float = 0.0
    ndcg_at: Dict[int, float] = field(default_factory=dict)


def norm_source(source: str) -> str:
    """Normalize a source path for comparison: NFC then casefold.

    NFC folds the macOS/APFS decomposed form (``e`` + U+0301) onto the composed
    one a hand-typed golden file carries; ``casefold`` (not ``lower``) handles
    the case-insensitive filesystem and non-ASCII casing correctly. Applied to
    both sides of every comparison in this module.
    """
    return unicodedata.normalize("NFC", str(source or "")).casefold()


def dedupe_ranked_sources(sources: Iterable[str]) -> List[str]:
    """Collapse a chunk-level ranking to a document-level one.

    Each distinct (normalized) source is kept exactly once, at its FIRST
    occurrence — i.e. at its best rank — and the relative order of the survivors
    is preserved. Three chunks of one note at ranks 1-3 therefore occupy rank 1
    alone, and a different note previously at raw rank 4 moves up to rank 2.

    Normalization happens BEFORE the dedupe, so two spellings of one filename
    (NFD vs NFC, differing case) collapse together instead of counting twice.
    Returns the normalized forms — the metric functions never re-normalize.

    A chunk whose source normalizes to ``""`` (the indexer's fallback chain
    ``source`` → ``file_path`` → ``""`` bottoming out on a partially-stamped
    index) is **kept**, as a unique ``UNIDENTIFIED_PREFIX`` placeholder. It was
    silently dropped until the 2026-07-27 audit, which promoted every document
    below it by one rank: ``["", "b.md"]`` against ``relevant=["b.md"]`` scored
    ``rr = 1.0`` when the retriever had in truth spent rank 1 on something else
    and put ``b.md`` at rank 2 (``rr = 0.5``). Dropping it credited the run for
    a slot it had wasted — the same inflation the doc-level dedupe exists to
    prevent, just from the other direction.

    Each unidentified chunk gets its OWN placeholder rather than collapsing
    with the others: dedupe collapses chunks *proven* to share a document, and
    two sourceless chunks prove nothing. Collapsing them would re-introduce the
    promotion this fix removes (n sourceless chunks would free n-1 ranks for the
    documents below); keeping them separate can at worst over-count slots when
    they really did come from one note, which understates rather than overstates
    retrieval quality. On a correctly-stamped index — every index the harness is
    meant to be run against — no placeholder is ever emitted and the function is
    byte-identical to its previous behaviour.
    """
    seen: set = set()
    out: List[str] = []
    for position, raw in enumerate(sources or []):
        key = norm_source(raw)
        if not key:
            # Position-keyed, so it is unique by construction and stable under
            # re-scoring of the same list.
            out.append(f"{UNIDENTIFIED_PREFIX}{position}")
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _norm_ks(ks: Sequence[int] | None) -> Tuple[int, ...]:
    """Sorted, de-duplicated, strictly-positive cutoffs (empty ⇒ defaults)."""
    if not ks:
        return DEFAULT_KS
    cleaned = sorted({int(k) for k in ks if int(k) > 0})
    return tuple(cleaned) if cleaned else DEFAULT_KS


def _idcg(hits: int) -> float:
    """Ideal DCG for *hits* binary-relevant documents stacked at the top.

    Callers pass ``min(n_relevant, k)`` — deliberately NOT
    ``min(n_relevant, len(ranked), k)``. The ideal is measured against the full
    relevant set, not against what this particular run happened to return, so a
    cell that retrieved fewer documents is *penalised* for the relevant ones it
    never surfaced instead of being graded on a shrunken curve. That is the
    difference between recall-aware nDCG (what the knob sweep needs: "did
    turning the reranker off lose me documents?") and precision-only nDCG,
    which would score a run that returned a single perfect hit and stopped at
    1.0 — identical to a run that returned all five relevant documents. Under
    the precision-only denominator the two would be indistinguishable, which is
    exactly the regression the harness exists to catch.

    The cap at *k* is separate and non-negotiable: only *k* slots exist, so no
    ranking can hit more than *k* relevant documents and the ratio must stay
    ≤ 1.0.
    """
    return sum(1.0 / math.log2(i + 1) for i in range(1, hits + 1))


def score_query(
    retrieved_sources: Sequence[str],
    q: RetrievalQuery,
    ks: Sequence[int] = DEFAULT_KS,
) -> QueryScore:
    """Score one ranked chunk-source list against one golden query.

    *retrieved_sources* is in rank order and may repeat a source (several chunks
    of one note); it is collapsed doc-level first, with sourceless chunks kept
    as rank-occupying placeholders (see ``dedupe_ranked_sources``). A query with
    no relevant sources, or one that retrieved nothing, scores 0.0 everywhere.

    ``k`` larger than the retrieved list is fine and needs no padding: the
    cutoff only filters hit ranks, so recall@k saturates at "everything this run
    found" and nDCG@k keeps the full-relevant-set denominator (``_idcg``) — a
    short list is scored as a short list, not rescaled to look complete.
    """
    cutoffs = _norm_ks(ks)
    ranked = dedupe_ranked_sources(retrieved_sources)
    # Set-valued on purpose: it is BOTH the membership test and the recall/nDCG
    # denominator, so a hand-curated golden entry listing one document twice
    # (or in two spellings — the normalization runs first) counts once. Under a
    # list denominator that entry would cap recall at 0.5 for a run that found
    # every document it asked for, and the miss would look like a retrieval
    # regression rather than the typo it is.
    relevant = {norm_source(s) for s in (q.relevant_sources or [])}
    relevant.discard("")

    hit_ranks = [rank for rank, src in enumerate(ranked, start=1) if src in relevant]
    n_relevant = len(relevant)

    recall_at: Dict[int, float] = {}
    ndcg_at: Dict[int, float] = {}
    for k in cutoffs:
        if not n_relevant:
            recall_at[k] = 0.0
            ndcg_at[k] = 0.0
            continue
        in_k = [r for r in hit_ranks if r <= k]
        recall_at[k] = len(in_k) / n_relevant
        dcg = sum(1.0 / math.log2(r + 1) for r in in_k)
        ideal = _idcg(min(n_relevant, k))
        ndcg_at[k] = (dcg / ideal) if ideal else 0.0

    return QueryScore(
        id=q.id,
        hit_ranks=hit_ranks,
        recall_at=recall_at,
        rr=(1.0 / hit_ranks[0]) if hit_ranks else 0.0,
        ndcg_at=ndcg_at,
    )


def aggregate(
    scores: Sequence[QueryScore],
    ks: Sequence[int] = DEFAULT_KS,
) -> Dict[str, float]:
    """Mean each metric over *scores* → ``{recall@k…, mrr, ndcg@k…, n}``.

    A missing per-query cutoff counts as 0.0 (it was never hit), so mixing
    scores computed at different ``ks`` degrades rather than raises. An empty
    ``scores`` yields zeros with ``n = 0``.
    """
    cutoffs = _norm_ks(ks)
    n = len(scores)

    def _mean(values: Iterable[float]) -> float:
        total = 0.0
        count = 0
        for v in values:
            total += float(v)
            count += 1
        return (total / count) if count else 0.0

    out: Dict[str, float] = {}
    for k in cutoffs:
        out[f"recall@{k}"] = _mean(s.recall_at.get(k, 0.0) for s in scores)
    out["mrr"] = _mean(s.rr for s in scores)
    for k in cutoffs:
        out[f"ndcg@{k}"] = _mean(s.ndcg_at.get(k, 0.0) for s in scores)
    out["n"] = float(n)
    return out


def load_golden(path: str | Path) -> Tuple[Dict, List[RetrievalQuery]]:
    """Load a golden retrieval set → ``(header, queries)``.

    *header* is every top-level field except ``queries`` (``generated_at``,
    ``vault_path``, ``embed``, ``generator``, …) — the runner reports it and
    warns on a vault mismatch. Raises ``ValueError`` on anything that is not a
    ``schema_version == 1`` object, so a future graded-qrel format can never be
    silently mis-scored as binary.

    That promise now covers the *entries* too, not just the envelope. A golden
    set is hand-curated by design, so the realistic corruption is a bare string
    or a null left in ``queries`` by manual editing — and until the 2026-07-27
    audit that escaped as ``TypeError: string indices must be integers``, past
    the runner's ``except (OSError, ValueError, KeyError)`` and out as a raw
    traceback instead of the documented named exit-1. Every per-entry shape
    failure below therefore raises ``ValueError`` naming the offending index.

    Raising rather than skipping-with-a-count is the deliberate choice, for the
    same reason ``schema_version`` is strict: results files are compared across
    runs (Δ-vs-baseline), and a run that silently scored 47 of 50 queries is not
    comparable to one that scored 50 — nothing in the results file would record
    which three vanished. A hard failure costs one edit; a silent skip costs a
    misread sweep. Field *values* stay lenient (``str()``-coerced) — this gate
    is about shape.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"golden set must be a JSON object, got {type(data).__name__}")
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported golden schema_version {version!r} (expected {SCHEMA_VERSION})"
        )

    header = {k: v for k, v in data.items() if k != "queries"}
    raw_queries = data.get("queries") or []
    if not isinstance(raw_queries, list):
        raise ValueError("golden 'queries' must be a list")

    queries: List[RetrievalQuery] = []
    for index, obj in enumerate(raw_queries):
        if not isinstance(obj, dict):
            raise ValueError(
                f"golden 'queries'[{index}] must be an object, "
                f"got {type(obj).__name__}"
            )
        # Pre-checked rather than left to KeyError so the message names the
        # index — a 50-query file gives no other way to find the bad entry.
        for required in ("id", "query"):
            if required not in obj:
                raise ValueError(f"golden 'queries'[{index}] is missing {required!r}")
        sources = obj.get("relevant_sources", [])
        # A JSON string is iterable, so the comprehension below would turn
        # "notes/a.md" into eleven single-character "documents": a golden entry
        # that loads clean and then scores 0.0 forever, reading as a retrieval
        # failure. Reject the shape instead. (``tuple`` is accepted because
        # ``json`` never produces one but a programmatic caller might.)
        if isinstance(sources, (str, bytes)) or not isinstance(sources, (list, tuple)):
            raise ValueError(
                f"golden 'queries'[{index}].relevant_sources must be a list, "
                f"got {type(sources).__name__}"
            )
        queries.append(RetrievalQuery(
            id=str(obj["id"]),
            query=str(obj["query"]),
            lang=str(obj.get("lang", "")),
            relevant_sources=[str(s) for s in sources],
            origin=str(obj.get("origin", "inverted")),
            chunk_preview=str(obj.get("chunk_preview", "")),
        ))
    return header, queries


def _metric_columns(cells: Sequence[Mapping]) -> List[str]:
    """Metric keys across *cells*, in report order (``n`` last, no Δ)."""
    keys: List[str] = []
    for cell in cells:
        for key in (cell.get("metrics") or {}):
            if key not in keys:
                keys.append(key)

    def _sort_key(key: str) -> Tuple[int, float, str]:
        if key == "n":
            return (9, 0.0, key)
        family, _, cutoff = key.partition("@")
        rank = {"recall": 0, "mrr": 1, "ndcg": 2}.get(family, 3)
        try:
            return (rank, float(cutoff), key)
        except ValueError:
            return (rank, 0.0, key)

    return sorted(keys, key=_sort_key)


def format_cells_table(
    cells: Sequence[Mapping],
    baseline: str | None = None,
) -> str:
    """Render a fixed-width metric table with Δ-vs-baseline columns.

    *cells* is a list of ``{"name": str, "metrics": {metric: value}}`` mappings
    (the runner's per-knob-combination results). *baseline* names the reference
    cell; ``None`` uses the first one — the runner always emits its implicit
    ``baseline`` cell first. The baseline row itself carries no Δ, and ``n`` is
    printed as a plain count (a differing ``n`` means the cells are not
    comparable, which the Δ would hide rather than reveal).
    """
    if not cells:
        return "(no cells)"

    columns = _metric_columns(cells)
    ref = None
    if baseline is not None:
        for cell in cells:
            if str(cell.get("name", "")) == baseline:
                ref = cell
                break
    if ref is None:
        ref = cells[0]
    ref_metrics = ref.get("metrics") or {}

    def _cell_text(cell: Mapping, key: str) -> str:
        metrics = cell.get("metrics") or {}
        if key not in metrics:
            return "-"
        value = float(metrics[key])
        if key == "n":
            return f"{int(round(value))}"
        text = f"{value:.3f}"
        if cell is ref or key not in ref_metrics:
            return text
        return f"{text} ({value - float(ref_metrics[key]):+.3f})"

    header = ["cell"] + columns
    rows = [[str(cell.get("name", "")) or "(unnamed)"] + [_cell_text(cell, k) for k in columns]
            for cell in cells]

    widths = [len(h) for h in header]
    for row in rows:
        for i, text in enumerate(row):
            widths[i] = max(widths[i], len(text))

    def _line(row: Sequence[str]) -> str:
        return "  ".join(text.ljust(widths[i]) for i, text in enumerate(row)).rstrip()

    lines = [_line(header), _line(["-" * w for w in widths])]
    lines.extend(_line(row) for row in rows)
    lines.append("")
    lines.append(f"baseline: {ref.get('name', '')}")
    return "\n".join(lines)
