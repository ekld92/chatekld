"""Merge, score, tier and refuse — the resolution orchestrator.

``resolve_queries`` walks the ranked :class:`~accession.identify.Query`
list and produces one :class:`Resolution`. Its contract encodes the plan's
two hard rules:

* **The bib is checked before ANY external call.** 74% of the measured
  identifiable orphans were re-links, not accessions, and Phase 1 only
  caught the ones with an extractable identifier — so every DOI query is
  joined against ``_master.bib``'s DOI index first, and a hit is a **Link**
  outcome that cost zero network. A title-matched winner's DOI is checked
  again after resolution, because a title match can also turn out to be a
  re-link.
* **Refusal beats guessing.** When the top two title candidates score
  within :data:`SCORE_MARGIN` and are not the same work, the resolution is
  ``ambiguous`` — candidates listed, no default offered (the bridge's
  standing posture, and the near-duplicates idiom the tiers mirror).

Evidence tiers (the plan's §5, verbatim — the UI's bulk-select boundary):

======================  ==========================================  =========
tier                    basis                                       outcome
======================  ==========================================  =========
``bib_doi``             identifier resolves to an entry already in  Link
                        ``_master.bib``
``xmp_doi``             DOI from embedded metadata, resolved        Accession
                        externally
``printed_doi``         topmost identifier on page 1, resolved      Accession
``title_filename``      title matched AND filename surname+year     Accession
                        corroborate
``title_only``          title matched, corroboration failed         card only
``filename_only``       author+year search, no text to confirm      card only
``ambiguous``           ≥2 candidates within the score margin       none
======================  ==========================================  =========

Outcomes are ``link`` (this PDF *is* bib entry ``citation_key``),
``candidates`` (external records found, best first — an accession row or a
card), ``ambiguous`` (refused), ``none``. An arXiv id resolved externally
to a DOI that is in the bib is a ``link`` at tier ``bib_doi`` (the id→DOI
mapping is the registry's own assertion, identifier-grade); a *title*
match whose winner's DOI is in the bib is a ``link`` that keeps its title
tier, so bulk never means "a title looked right".
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from difflib import SequenceMatcher

from accession.identify import Query
from accession.sources import Candidate, Source, SourceError
from audit.engine.bridge import _normalize_author_lastname

__all__ = ["Resolution", "resolve_queries", "score_title",
           "SCORE_MARGIN", "MIN_TITLE_SCORE"]

#: Two candidates whose scores differ by less than this are "within the
#: margin" — the resolution refuses rather than picking one.
SCORE_MARGIN = 0.05

#: Floor a title candidate must reach to count as a match at all. Measured
#: against the live library (mapped PDFs' extracted titles vs their bib
#: records) before shipping; see the Phase 2 commit message.
MIN_TITLE_SCORE = 0.65

#: A source call is skipped (with a note) when less budget than this
#: remains — the ``web_tools`` refuse-under-5s precedent.
MIN_SOURCE_CALL_S = 5.0

_IDENTIFIER_TIER_BY_EVIDENCE = {"meta": "xmp_doi", "text": "printed_doi"}

#: An ISBN gets its own tier rather than borrowing the DOI ones (Batch D,
#: 2026-08-26). It IS identifier-grade — a valid check digit is publisher-
#: asserted identity, so it belongs above the bulk-selection boundary with
#: xmp_doi — but calling it `printed_doi` in the UI would be a lie about what
#: resolved the row, and the tier is what the user reads to decide.
_ISBN_TIER = "isbn"


@dataclass
class Resolution:
    """The outcome of resolving one PDF's queries. See the module docstring."""

    outcome: str                       # link | candidates | ambiguous | none
    tier: str | None = None
    citation_key: str | None = None
    doi: str | None = None
    query_kind: str | None = None
    evidence: str | None = None
    candidates: list[Candidate] = field(default_factory=list)  # best first
    scores: list[float] = field(default_factory=list)          # aligned
    notes: list[str] = field(default_factory=list)             # status-only
    #: Source names actually CALLED for this resolution, in call order —
    #: skipped/failed calls land in ``notes``, but a clean-dry source used to
    #: leave no trace at all, so "asked crossref+europepmc, rescued by hal"
    #: was unreconstructable (2026-08-19 review). Empty for the offline bib
    #: fast path, which is what makes zero-egress provable client-side.
    consulted: list[str] = field(default_factory=list)
    from_cache: bool = False

    def to_jsonable(self) -> dict:
        return {
            "outcome": self.outcome, "tier": self.tier,
            "citation_key": self.citation_key, "doi": self.doi,
            "query_kind": self.query_kind, "evidence": self.evidence,
            "candidates": [c.__dict__.copy() for c in self.candidates],
            "scores": list(self.scores), "notes": list(self.notes),
            "consulted": list(self.consulted),
        }

    @classmethod
    def from_jsonable(cls, data: object) -> "Resolution | None":
        """Rebuild from a cache entry; any shape surprise degrades to None."""
        if not isinstance(data, dict) or not isinstance(data.get("outcome"), str):
            return None
        try:
            cands = [
                Candidate(**{
                    k: (tuple(v) if k == "authors" else v)
                    for k, v in c.items()
                    if k in Candidate.__dataclass_fields__
                })
                for c in data.get("candidates", []) if isinstance(c, dict)
            ]
            return cls(
                outcome=data["outcome"], tier=data.get("tier"),
                citation_key=data.get("citation_key"), doi=data.get("doi"),
                query_kind=data.get("query_kind"),
                evidence=data.get("evidence"), candidates=cands,
                scores=[float(s) for s in data.get("scores", [])],
                notes=[str(n) for n in data.get("notes", [])],
                consulted=[str(s) for s in data.get("consulted", [])],
            )
        except (TypeError, ValueError):
            return None


# ── title scoring ──────────────────────────────────────────────────────────


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s)).strip()


def score_title(query_title: str, candidate_title: str) -> float:
    """Similarity in [0, 1] between a page-1 title candidate and a record's.

    Plain ``difflib`` ratio over accent/case/punctuation-folded strings,
    with a prefix-aware second pass: page-1 extraction often captures only
    the first line of a wrapped title, and a clean prefix must not be
    punished for the half it never saw.
    """
    a, b = _fold(query_title), _fold(candidate_title)
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    full = SequenceMatcher(None, a, b).ratio()
    prefix = SequenceMatcher(None, a, b[: len(a) + 10]).ratio()
    return max(full, prefix)


def _surname_keys(cand: Candidate) -> set[str]:
    return {k for k in (_normalize_author_lastname(a) for a in cand.authors)
            if k}


def _corroborates(cand: Candidate, query: Query) -> bool:
    """Filename (surname, year) agreement — the ``title_filename`` gate."""
    if not query.fn_authors or not query.fn_year or cand.year is None:
        return False
    if abs(cand.year - int(query.fn_year)) > 1:
        return False
    return bool(_surname_keys(cand) & set(query.fn_authors))


def _same_work(a: Candidate, b: Candidate) -> bool:
    if a.doi and b.doi:
        return a.doi == b.doi
    return (_fold(a.title or ""), a.year) == (_fold(b.title or ""), b.year)


def _merge_candidates(groups: Sequence[list[Candidate]]) -> list[Candidate]:
    """Concatenate per-source result lists, deduping same-work entries.

    First occurrence wins position (source order is the trust order);
    fields the first occurrence lacks are filled from later duplicates, so
    a Crossref record gains the PMID Europe PMC knows and vice versa.
    """
    out: list[Candidate] = []
    for cands in groups:
        for cand in cands:
            for i, kept in enumerate(out):
                if _same_work(kept, cand):
                    fills = {
                        f: getattr(cand, f)
                        for f in ("doi", "arxiv_id", "pmid", "container",
                                  "url", "year")
                        if getattr(kept, f) is None
                        and getattr(cand, f) is not None
                    }
                    if not kept.authors and cand.authors:
                        fills["authors"] = cand.authors
                    if fills:
                        out[i] = replace(kept, **fills)
                    break
            else:
                out.append(cand)
    return out


# ── the orchestrator ───────────────────────────────────────────────────────


def _bib_keys_for(doi: str | None,
                  bib_doi_index: Mapping[str, Sequence[str]],
                  ) -> list[str] | None:
    if not doi:
        return None
    keys = bib_doi_index.get(doi)
    return list(keys) if keys else None


def _apply_bib_check(res: Resolution,
                     bib_doi_index: Mapping[str, Sequence[str]]) -> Resolution:
    """Re-join a resolution's DOI against the CURRENT bib index.

    Applied to every cache hit (and to fresh title winners), so an
    accession row whose paper has since been catalogued flips to a Link,
    and a cached link whose bib entry vanished falls back to candidates.
    Identifier-grade resolutions gain/keep tier ``bib_doi``; title-grade
    ones keep their title tier (the bulk boundary — see module docstring).
    """
    keys = _bib_keys_for(res.doi, bib_doi_index)
    if keys and len(keys) == 1:
        tier = res.tier
        if res.query_kind in ("doi", "arxiv"):
            tier = "bib_doi"
        return replace(res, outcome="link", citation_key=keys[0], tier=tier)
    if res.outcome == "link":
        # The cached link no longer resolves in the current bib.
        fallback = "candidates" if res.candidates else "none"
        note = "cached link no longer in the bib; re-review"
        return replace(res, outcome=fallback, citation_key=None,
                       tier=None if fallback == "none" else res.tier,
                       notes=[*res.notes, note])
    if keys and len(keys) > 1:
        return replace(res, notes=[
            *res.notes,
            f"doi {res.doi} maps to {len(keys)} bib entries: "
            + ", ".join(sorted(keys))])
    return res


def resolve_queries(
    queries: Sequence[Query],
    *,
    bib_doi_index: Mapping[str, Sequence[str]],
    sources: Sequence[Source],
    total_budget_s: float = 45.0,
    rows: int = 5,
    per_call_timeout_s: float = 15.0,
    cache: MutableMapping | None = None,
    sha256: str | None = None,
    force_refresh: bool = False,
    progress_cb: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Resolution:
    """Resolve one PDF's ranked queries into a single :class:`Resolution`.

    ``bib_doi_index`` is ``{normalised doi: [citation_key, ...]}`` built
    from the same ``BibEntry.doi`` values the bridge joins on (both sides
    ``normalise_doi``-canonical). ``cache`` + ``sha256`` opt into the
    accession cache: a stored network answer is revalidated against the
    CURRENT bib index and returned; a fresh network answer is stored. The
    offline bib-hit fast path is never cached — it costs nothing to
    recompute and must always reflect today's bib.

    ``total_budget_s`` is the wall-clock bound across every source call for
    this PDF; a source whose turn comes with less than
    :data:`MIN_SOURCE_CALL_S` remaining is skipped with a note, never
    silently. Per-source failures are notes too — one dead endpoint
    degrades that source, not the resolution.

    ``force_refresh`` skips the cache CONSULT (never the store): a degraded
    resolution — a flaky source 400'd mid-run — is cached like any other so
    a batch keeps its resume semantics, which also means a plain re-run
    would return the cached failure forever. The explicit per-row Identify
    verb passes this so "look again" genuinely looks again. Measured need:
    theses.fr answered HTTP 400 on 45 of 576 live batch calls, then 200 for
    the same queries minutes later.
    """
    deadline = time.monotonic() + max(0.0, float(total_budget_s))

    def _remaining() -> float:
        return deadline - time.monotonic()

    # ── 0. the offline bib check, before ANY network ──
    for q in queries:
        if q.kind != "doi":
            continue
        keys = _bib_keys_for(q.value, bib_doi_index)
        if not keys:
            continue
        if len(keys) == 1:
            return Resolution(
                outcome="link", tier="bib_doi", citation_key=keys[0],
                doi=q.value, query_kind=q.kind, evidence=q.evidence)
        # One DOI, several bib entries: the bibliography is duplicated.
        # Refuse to pick a winner (the bridge's doi_collisions posture).
        return Resolution(
            outcome="ambiguous", tier="ambiguous", doi=q.value,
            query_kind=q.kind, evidence=q.evidence,
            notes=[f"doi {q.value} maps to {len(keys)} bib entries: "
                   + ", ".join(sorted(keys))])

    # ── 1. cache consult (network answers only ever land here) ──
    if cache is not None and sha256 and not force_refresh:
        from accession import cache as accession_cache

        hit = accession_cache.lookup(cache, sha256)
        if hit is not None:
            cached = Resolution.from_jsonable(hit)
            if cached is not None:
                cached.from_cache = True
                return _apply_bib_check(cached, bib_doi_index)

    notes: list[str] = []
    consulted: list[str] = []
    network_touched = False

    def _call(source: Source, q: Query) -> list[Candidate]:
        nonlocal network_touched
        if _remaining() < MIN_SOURCE_CALL_S:
            notes.append(f"budget exhausted; skipped {source.name}")
            return []
        if should_cancel is not None and should_cancel():
            # Checked at the SOURCE-CALL boundary, which is the only
            # interruptible point in the ladder: a call already in flight runs
            # to its own timeout. Without this the route's cancellation was
            # observable only before and after a ladder that can spend the full
            # ACCESSION_TOTAL_BUDGET_S, most of it in polite pacing sleeps.
            notes.append(f"cancelled; skipped {source.name}")
            return []
        network_touched = True
        if source.name not in consulted:
            consulted.append(source.name)
        if progress_cb is not None:
            # Best-effort narration ("querying crossref…") for the batch's SSE
            # info frames — a broken callback must never fail a resolution.
            try:
                progress_cb(source.name)
            except Exception:  # noqa: BLE001
                pass
        try:
            kwargs: dict[str, Any] = {
                "year": q.fn_year, "rows": rows,
                "timeout_s": min(per_call_timeout_s, max(_remaining(), 1.0)),
            }
            # Passed ONLY when there is one, so the container keyword is not a
            # signature every source implementation must grow for a constraint
            # almost no query carries.
            if q.container:
                kwargs["container"] = q.container
            return source.run(q.kind, q.value, **kwargs)
        except SourceError as exc:
            notes.append(str(exc))   # status-only by the sources' contract
            return []

    def _finish(res: Resolution) -> Resolution:
        res.notes = [*notes, *res.notes]
        res.consulted = [*consulted, *res.consulted]
        if cache is not None and sha256 and network_touched:
            from accession import cache as accession_cache

            accession_cache.store(cache, sha256, res.to_jsonable())
        return res

    for q in queries:
        supporting = [s for s in sources if q.kind in s.supports]
        if not supporting:
            continue

        if q.kind in ("doi", "arxiv", "isbn"):
            # An identifier lookup is authoritative: first source that
            # resolves it wins, no cross-source merging needed.
            for source in supporting:
                cands = _call(source, q)
                if not cands:
                    continue
                winner = cands[0]
                res = Resolution(
                    outcome="candidates",
                    tier=(_ISBN_TIER if q.kind == "isbn"
                          else _IDENTIFIER_TIER_BY_EVIDENCE.get(q.evidence)),
                    doi=winner.doi or (q.value if q.kind == "doi" else None),
                    query_kind=q.kind, evidence=q.evidence,
                    candidates=[winner], scores=[1.0])
                return _finish(_apply_bib_check(res, bib_doi_index))

        elif q.kind == "title":
            # Two stages: primaries first, rescue sources (arXiv / HAL /
            # theses.fr) only when the primaries yield nothing above the
            # floor — the plan's "against whatever earlier phases actually
            # failed to resolve", and it spares every mainstream paper
            # arXiv's 3-second courtesy interval.
            primaries = [s for s in supporting if not getattr(s, "rescue", False)]
            rescues = [s for s in supporting if getattr(s, "rescue", False)]

            def _scored(srcs: list[Source], q: Query = q):
                merged = _merge_candidates([_call(s, q) for s in srcs])
                # The masthead guard (2026-08-19 review): when the page-1
                # extractor grabs a journal masthead ("The New England
                # Journal of Medicine"), the search finds a record whose
                # title IS that boilerplate — its own container — and scores
                # 1.0. A record titled like its own venue is never the paper.
                kept = []
                dropped = 0
                for c in merged:
                    if c.container and _fold(c.title or "") == _fold(c.container):
                        dropped += 1
                    else:
                        kept.append(c)
                if dropped:
                    notes.append(
                        f"dropped {dropped} journal-titled record(s) "
                        "(title equals its own venue)")
                pairs = sorted(
                    ((c, score_title(q.value, c.title or "")) for c in kept),
                    key=lambda cs: cs[1], reverse=True)
                return [(c, s) for c, s in pairs if s >= MIN_TITLE_SCORE]

            scored = _scored(primaries)
            if not scored and rescues:
                scored = _scored(rescues)
            if not scored:
                continue
            ordered = [c for c, _s in scored]
            scores = [round(s, 4) for _c, s in scored]
            if (len(scored) >= 2
                    and scored[0][1] - scored[1][1] < SCORE_MARGIN
                    and not _same_work(scored[0][0], scored[1][0])):
                res = Resolution(
                    outcome="ambiguous", tier="ambiguous",
                    query_kind=q.kind, evidence=q.evidence,
                    candidates=ordered, scores=scores,
                    notes=["two candidates within the score margin"])
                return _finish(res)
            winner = scored[0][0]
            tier = ("title_filename" if _corroborates(winner, q)
                    else "title_only")
            res = Resolution(
                outcome="candidates", tier=tier, doi=winner.doi,
                query_kind=q.kind, evidence=q.evidence,
                candidates=ordered, scores=scores)
            return _finish(_apply_bib_check(res, bib_doi_index))

        elif q.kind == "authoryear":
            primaries = [s for s in supporting if not getattr(s, "rescue", False)]
            rescues = [s for s in supporting if getattr(s, "rescue", False)]
            merged = _merge_candidates([_call(s, q) for s in primaries])
            if not merged and rescues:
                merged = _merge_candidates([_call(s, q) for s in rescues])
            if not merged:
                continue
            # No title to score against and no winner asserted — the tier
            # says it all: candidates for the eye, never a default.
            res = Resolution(
                outcome="candidates", tier="filename_only",
                query_kind=q.kind, evidence=q.evidence,
                candidates=merged[: max(rows, 1)],
                scores=[0.0] * min(len(merged), max(rows, 1)))
            return _finish(res)

    return _finish(Resolution(outcome="none"))
