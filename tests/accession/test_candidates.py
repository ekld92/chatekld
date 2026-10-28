"""The orchestrator's contract: bib-first, tiering, refusal, cache, budget.

The conftest tripwire replaces the sources' opener with an AssertionError,
so every test here proves its path hermetic as a side effect — most visibly
``test_bib_hit_resolves_offline_with_real_sources``, which drives the REAL
five-source registry and still must reach no network (the plan's hard rule:
the bib is checked before any external call).
"""

from __future__ import annotations

from accession import cache as accession_cache
from accession.candidates import (
    MIN_TITLE_SCORE,
    SCORE_MARGIN,
    Resolution,
    resolve_queries,
    score_title,
)
from accession.identify import Query
from accession.sources import Candidate, SourceHTTPError, default_sources


def _q(kind: str, value: str, evidence: str = "text",
       fn: tuple[str, ...] = ("myran",), year: str | None = "2024") -> Query:
    return Query(kind=kind, value=value, evidence=evidence,
                 fn_authors=fn, fn_year=year)


def _cand(title: str, *, source: str = "stub", doi: str | None = None,
          authors: tuple[str, ...] = ("Myran, Daniel",),
          year: int | None = 2024, **kw) -> Candidate:
    return Candidate(source=source, title=title, authors=authors,
                     year=year, doi=doi, **kw)


class StubSource:
    """Duck-typed source: canned results per kind, records every call."""

    def __init__(self, name: str, supports: set[str],
                 results: dict[str, list[Candidate]] | None = None,
                 error: Exception | None = None):
        self.name = name
        self.pace_s = 0.0
        self.supports = frozenset(supports)
        self._results = results or {}
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def run(self, kind, value, *, year=None, rows=5, timeout_s=15.0,
            container=None):
        self.last_container = container
        self.calls.append((kind, value))
        if self._error is not None:
            raise self._error
        return list(self._results.get(kind, []))


# ── the hard rule: bib before network ──────────────────────────────────────


class TestBibFirst:
    def test_bib_hit_resolves_offline_with_real_sources(self):
        """A DOI already in _master.bib is a Link that costs ZERO network.

        Uses the real five-source registry under the conftest tripwire —
        any HTTP attempt raises, so passing proves no call was made.
        """
        res = resolve_queries(
            [_q("doi", "10.1/known.1", "meta")],
            bib_doi_index={"10.1/known.1": ["myranEmergency2025"]},
            sources=default_sources())
        assert res.outcome == "link"
        assert res.tier == "bib_doi"
        assert res.citation_key == "myranEmergency2025"
        assert res.doi == "10.1/known.1"

    def test_bib_hit_wins_before_any_stub_is_consulted(self):
        stub = StubSource("stub", {"doi"}, {"doi": [_cand("X")]})
        resolve_queries(
            [_q("doi", "10.1/known.1", "meta")],
            bib_doi_index={"10.1/known.1": ["key"]}, sources=[stub])
        assert stub.calls == []

    def test_bib_doi_collision_is_refused_not_guessed(self):
        res = resolve_queries(
            [_q("doi", "10.1/dup.1", "meta")],
            bib_doi_index={"10.1/dup.1": ["keyA", "keyB"]},
            sources=default_sources())
        assert res.outcome == "ambiguous"
        assert res.tier == "ambiguous"
        assert res.citation_key is None
        assert any("2 bib entries" in n and "keyA" in n for n in res.notes)


# ── identifier resolution and evidence tiers ───────────────────────────────


class TestIdentifierTiers:
    def test_embedded_doi_resolves_to_xmp_doi_tier(self):
        stub = StubSource("stub", {"doi"},
                          {"doi": [_cand("Paper", doi="10.1/new.1")]})
        res = resolve_queries([_q("doi", "10.1/new.1", "meta")],
                              bib_doi_index={}, sources=[stub])
        assert (res.outcome, res.tier) == ("candidates", "xmp_doi")
        assert res.doi == "10.1/new.1"
        assert res.scores == [1.0]

    def test_printed_doi_resolves_to_printed_doi_tier(self):
        stub = StubSource("stub", {"doi"},
                          {"doi": [_cand("Paper", doi="10.1/new.1")]})
        res = resolve_queries([_q("doi", "10.1/new.1", "text")],
                              bib_doi_index={}, sources=[stub])
        assert res.tier == "printed_doi"

    def test_first_resolving_source_wins_after_a_failing_one(self):
        dead = StubSource("dead", {"doi"}, error=SourceHTTPError("dead", 500))
        live = StubSource("live", {"doi"},
                          {"doi": [_cand("Paper", doi="10.1/new.1")]})
        res = resolve_queries([_q("doi", "10.1/new.1", "meta")],
                              bib_doi_index={}, sources=[dead, live])
        assert res.outcome == "candidates"
        assert res.candidates[0].source == "stub" or res.candidates[0].title
        assert any("500" in n for n in res.notes)   # the failure is disclosed

    def test_arxiv_resolving_to_a_bib_doi_is_a_link_at_identifier_grade(self):
        # The registry's own id→DOI mapping is identifier evidence, so the
        # re-link discovered through it gets tier bib_doi, bulk-selectable.
        stub = StubSource("stub", {"arxiv"},
                          {"arxiv": [_cand("Paper", doi="10.1/known.1")]})
        res = resolve_queries([_q("arxiv", "2103.14030", "text")],
                              bib_doi_index={"10.1/known.1": ["key"]},
                              sources=[stub])
        assert (res.outcome, res.tier) == ("link", "bib_doi")
        assert res.citation_key == "key"


# ── title matching: corroboration, refusal, re-link ────────────────────────


TITLE = "Cannabis use and transition to psychosis in young adults"


class TestTitleMatching:
    def test_corroborated_winner_is_title_filename(self):
        stub = StubSource("stub", {"title"}, {"title": [
            _cand(TITLE, doi="10.1/new.1", authors=("Myran, Daniel",))]})
        res = resolve_queries([_q("title", TITLE)],
                              bib_doi_index={}, sources=[stub])
        assert (res.outcome, res.tier) == ("candidates", "title_filename")

    def test_uncorroborated_winner_is_title_only(self):
        stub = StubSource("stub", {"title"}, {"title": [
            _cand(TITLE, doi="10.1/new.1", authors=("Someone, Else",))]})
        res = resolve_queries([_q("title", TITLE)],
                              bib_doi_index={}, sources=[stub])
        assert res.tier == "title_only"

    def test_year_off_by_two_breaks_corroboration(self):
        stub = StubSource("stub", {"title"}, {"title": [
            _cand(TITLE, doi="10.1/new.1", year=2022)]})
        res = resolve_queries([_q("title", TITLE, year="2024")],
                              bib_doi_index={}, sources=[stub])
        assert res.tier == "title_only"

    def test_two_close_candidates_are_refused(self):
        near_a = _cand(TITLE + " a cohort study", doi="10.1/a.1")
        near_b = _cand(TITLE + " a cohort survey", doi="10.1/b.1")
        stub = StubSource("stub", {"title"}, {"title": [near_a, near_b]})
        res = resolve_queries([_q("title", TITLE)],
                              bib_doi_index={}, sources=[stub])
        assert (res.outcome, res.tier) == ("ambiguous", "ambiguous")
        assert res.citation_key is None
        assert len(res.candidates) == 2
        assert any("margin" in n for n in res.notes)

    def test_same_work_from_two_sources_is_not_ambiguous(self):
        a = _cand(TITLE, source="crossref", doi="10.1/same.1")
        b = _cand(TITLE, source="europepmc", doi="10.1/same.1", pmid="123")
        s1 = StubSource("s1", {"title"}, {"title": [a]})
        s2 = StubSource("s2", {"title"}, {"title": [b]})
        res = resolve_queries([_q("title", TITLE)],
                              bib_doi_index={}, sources=[s1, s2])
        assert res.outcome == "candidates"
        assert len(res.candidates) == 1
        assert res.candidates[0].pmid == "123"   # merged from the duplicate

    def test_title_winner_already_in_bib_is_a_link_keeping_its_tier(self):
        stub = StubSource("stub", {"title"}, {"title": [
            _cand(TITLE, doi="10.1/known.1")]})
        res = resolve_queries([_q("title", TITLE)],
                              bib_doi_index={"10.1/known.1": ["key"]},
                              sources=[stub])
        # Link outcome, but the CONFIDENCE stayed title-grade: never bib_doi,
        # so the bulk boundary still means "an identifier resolved".
        assert (res.outcome, res.tier) == ("link", "title_filename")
        assert res.citation_key == "key"

    def test_below_floor_candidates_fall_through_to_authoryear(self):
        stub = StubSource("stub", {"title", "authoryear"}, {
            "title": [_cand("Entirely unrelated paper about oncology")],
            "authoryear": [_cand("Another paper")]})
        res = resolve_queries(
            [_q("title", TITLE), _q("authoryear", "myran", "filename")],
            bib_doi_index={}, sources=[stub])
        assert (res.outcome, res.tier) == ("candidates", "filename_only")

    def test_score_title_is_prefix_tolerant(self):
        full = TITLE + ": a longitudinal cohort study in three countries"
        assert score_title(TITLE, full) >= MIN_TITLE_SCORE
        assert score_title("Unrelated words entirely", full) < MIN_TITLE_SCORE
        assert SCORE_MARGIN < MIN_TITLE_SCORE


# ── budget and degradation ─────────────────────────────────────────────────


class TestBudget:
    def test_zero_budget_skips_every_source_with_a_note(self):
        stub = StubSource("stub", {"doi"}, {"doi": [_cand("Paper")]})
        res = resolve_queries([_q("doi", "10.1/new.1", "meta")],
                              bib_doi_index={}, sources=[stub],
                              total_budget_s=0.0)
        assert res.outcome == "none"
        assert stub.calls == []
        assert any("budget exhausted" in n for n in res.notes)

    def test_all_sources_failing_degrades_to_none_with_notes(self):
        dead = StubSource("dead", {"doi", "title"},
                          error=SourceHTTPError("dead", 503))
        res = resolve_queries(
            [_q("doi", "10.1/new.1", "meta"), _q("title", TITLE)],
            bib_doi_index={}, sources=[dead])
        assert res.outcome == "none"
        assert any("503" in n for n in res.notes)

    def test_no_queries_resolves_to_none(self):
        res = resolve_queries([], bib_doi_index={},
                              sources=default_sources())
        assert (res.outcome, res.tier) == ("none", None)


# ── the sha-keyed cache ────────────────────────────────────────────────────


SHA = "ab" * 32


class TestCache:
    def test_network_answer_is_stored_and_served_from_cache(self):
        cache: dict = {}
        stub = StubSource("stub", {"doi"},
                          {"doi": [_cand("Paper", doi="10.1/new.1")]})
        first = resolve_queries([_q("doi", "10.1/new.1", "meta")],
                                bib_doi_index={}, sources=[stub],
                                cache=cache, sha256=SHA)
        assert not first.from_cache
        assert SHA in cache

        # Second pass: the stub would now fail — the cache must answer.
        broken = StubSource("stub", {"doi"},
                            error=AssertionError("network on a cache hit"))
        second = resolve_queries([_q("doi", "10.1/new.1", "meta")],
                                 bib_doi_index={}, sources=[broken],
                                 cache=cache, sha256=SHA)
        assert second.from_cache
        assert second.outcome == "candidates"
        assert second.candidates[0].title == "Paper"

    def test_cache_hit_is_revalidated_against_todays_bib(self):
        cache: dict = {}
        stub = StubSource("stub", {"doi"},
                          {"doi": [_cand("Paper", doi="10.1/new.1")]})
        resolve_queries([_q("doi", "10.1/new.1", "meta")],
                        bib_doi_index={}, sources=[stub],
                        cache=cache, sha256=SHA)
        # The paper has since been catalogued: the cached accession row must
        # come back as a Link, not stale advice to re-catalogue it.
        res = resolve_queries([_q("doi", "10.1/other.1", "text")],
                              bib_doi_index={"10.1/new.1": ["key"]},
                              sources=[StubSource("stub", {"doi"})],
                              cache=cache, sha256=SHA)
        assert (res.outcome, res.tier) == ("link", "bib_doi")
        assert res.citation_key == "key"
        assert res.from_cache

    def test_offline_bib_hit_is_never_cached(self):
        cache: dict = {}
        res = resolve_queries([_q("doi", "10.1/known.1", "meta")],
                              bib_doi_index={"10.1/known.1": ["key"]},
                              sources=default_sources(),
                              cache=cache, sha256=SHA)
        assert res.outcome == "link"
        assert cache == {}

    def test_cache_file_round_trip_and_version_gate(self):
        cache: dict = {}
        accession_cache.store(cache, SHA, {"outcome": "none"})
        accession_cache.save_cache(cache)
        loaded = accession_cache.load_cache()
        assert accession_cache.lookup(loaded, SHA) == {"outcome": "none"}
        # A different schema generation is treated as absent, never parsed.
        loaded[SHA]["v"] = accession_cache.CACHE_VERSION + 1
        assert accession_cache.lookup(loaded, SHA) is None
        assert accession_cache.lookup(loaded, "cd" * 32) is None

    def test_resolution_jsonable_round_trip(self):
        res = Resolution(
            outcome="candidates", tier="xmp_doi", doi="10.1/new.1",
            query_kind="doi", evidence="meta",
            candidates=[_cand("Paper", doi="10.1/new.1", pmid="9")],
            scores=[1.0], notes=["note"])
        back = Resolution.from_jsonable(res.to_jsonable())
        assert back == res
        assert Resolution.from_jsonable({"garbage": True}) is None
        assert Resolution.from_jsonable(None) is None


class TestRescueStage:
    def test_rescue_sources_are_spared_when_a_primary_resolves(self):
        primary = StubSource("primary", {"title"}, {"title": [
            _cand(TITLE, doi="10.1/new.1")]})
        rescue = StubSource("rescue", {"title"},
                            error=AssertionError("rescue consulted needlessly"))
        rescue.rescue = True
        res = resolve_queries([_q("title", TITLE)],
                              bib_doi_index={}, sources=[primary, rescue])
        assert res.outcome == "candidates"
        assert rescue.calls == []

    def test_rescue_sources_are_consulted_when_primaries_come_up_dry(self):
        primary = StubSource("primary", {"title"}, {"title": []})
        rescue = StubSource("rescue", {"title"}, {"title": [
            _cand(TITLE, doi="10.1/hal.1", source="hal")]})
        rescue.rescue = True
        res = resolve_queries([_q("title", TITLE)],
                              bib_doi_index={}, sources=[primary, rescue])
        assert res.outcome == "candidates"
        assert res.candidates[0].source == "hal"
        assert primary.calls == [("title", TITLE)]

    def test_identifier_lookups_ignore_the_rescue_flag(self):
        # An arXiv id has exactly one home; the flag must not hide it.
        arx = StubSource("arxiv", {"arxiv"}, {"arxiv": [
            _cand("Paper", doi="10.1/new.1")]})
        arx.rescue = True
        res = resolve_queries([_q("arxiv", "2103.14030", "text")],
                              bib_doi_index={}, sources=[arx])
        assert res.outcome == "candidates"

    def test_force_refresh_bypasses_the_consult_but_still_stores(self):
        cache: dict = {}
        stub1 = StubSource("stub", {"doi"},
                           {"doi": [_cand("Old", doi="10.1/new.1")]})
        resolve_queries([_q("doi", "10.1/new.1", "meta")],
                        bib_doi_index={}, sources=[stub1],
                        cache=cache, sha256=SHA)
        # A degraded/stale entry sits in the cache. Force must re-run the
        # sources AND overwrite the stored answer with the fresh one.
        stub2 = StubSource("stub", {"doi"},
                           {"doi": [_cand("Fresh", doi="10.1/new.1")]})
        res = resolve_queries([_q("doi", "10.1/new.1", "meta")],
                              bib_doi_index={}, sources=[stub2],
                              cache=cache, sha256=SHA, force_refresh=True)
        assert not res.from_cache
        assert res.candidates[0].title == "Fresh"
        assert stub2.calls == [("doi", "10.1/new.1")]
        stored = accession_cache.lookup(cache, SHA)
        assert stored["candidates"][0]["title"] == "Fresh"


# ── provenance (2026-08-19 review: sources lane) ────────────────────────────


class TestProvenance:
    def test_consulted_records_every_called_source_including_clean_dry(self):
        # A clean-dry source used to leave no trace at all, so "asked
        # crossref+europepmc" was unreconstructable from the payload.
        dry = StubSource("alpha", {"title"}, {"title": []})
        hot = StubSource("beta", {"title"},
                         {"title": [_cand("Exact title match here")]})
        res = resolve_queries([_q("title", "Exact title match here")],
                              bib_doi_index={}, sources=[dry, hot])
        assert res.consulted == ["alpha", "beta"]

    def test_offline_bib_hit_consults_nothing(self):
        # consulted == [] is what makes zero-egress provable client-side.
        stub = StubSource("stub", {"doi"},
                          {"doi": [_cand("Never reached", doi="10.1/in.1")]})
        res = resolve_queries([_q("doi", "10.1/in.1", "meta")],
                              bib_doi_index={"10.1/in.1": ["key2020"]},
                              sources=[stub])
        assert res.outcome == "link"
        assert res.consulted == []
        assert stub.calls == []

    def test_consulted_round_trips_and_tolerates_pre_field_cache_entries(self):
        res = Resolution(outcome="none", consulted=["crossref", "hal"])
        again = Resolution.from_jsonable(res.to_jsonable())
        assert again.consulted == ["crossref", "hal"]
        # An entry cached before the field existed must still parse.
        old = res.to_jsonable()
        old.pop("consulted")
        parsed = Resolution.from_jsonable(old)
        assert parsed is not None and parsed.consulted == []


class TestMastheadGuard:
    def test_a_record_titled_like_its_own_venue_is_dropped_with_a_note(self):
        # The page-1 extractor sometimes grabs a journal masthead as the
        # title; the catalogue then finds a record whose title IS that
        # boilerplate and it scores 1.0 (measured live: "The New England
        # Journal of Medicine" at score 1.0). A record titled like its own
        # container is never the paper.
        masthead = _cand("The New England Journal of Medicine",
                         container="The New England Journal of Medicine",
                         doi="10.1/masthead")
        src = StubSource("stub", {"title"}, {"title": [masthead]})
        res = resolve_queries(
            [_q("title", "The New England Journal of Medicine")],
            bib_doi_index={}, sources=[src])
        assert res.outcome == "none"
        assert any("journal-titled" in n for n in res.notes)

    def test_the_guard_spares_a_record_with_a_distinct_container(self):
        normal = _cand("A real paper about outcomes",
                       container="The New England Journal of Medicine",
                       doi="10.1/real")
        src = StubSource("stub", {"title"}, {"title": [normal]})
        res = resolve_queries([_q("title", "A real paper about outcomes")],
                              bib_doi_index={}, sources=[src])
        assert res.outcome == "candidates"
        assert res.candidates[0].doi == "10.1/real"


class TestProgressCallback:
    def test_progress_cb_narrates_each_source_call_and_never_fails_a_run(self):
        seen: list[str] = []
        hot = StubSource("beta", {"title"},
                         {"title": [_cand("Exact title match here")]})
        res = resolve_queries([_q("title", "Exact title match here")],
                              bib_doi_index={}, sources=[hot],
                              progress_cb=seen.append)
        assert res.outcome == "candidates"
        assert seen == ["beta"]

        # A broken callback degrades to silence, never a failed resolution.
        def _boom(_name: str) -> None:
            raise RuntimeError("narrator crashed")
        res2 = resolve_queries([_q("title", "Exact title match here")],
                               bib_doi_index={}, sources=[hot],
                               progress_cb=_boom)
        assert res2.outcome == "candidates"


def test_cancellation_stops_the_ladder_at_a_source_boundary():
    """An abandoned stream must stop the ladder, not merely be noticed after it.

    `should_cancel` is polled where the ladder is actually interruptible — the
    boundary before each source call. Without it the route's cancellation was
    observable only before and after a run that can spend the whole
    ACCESSION_TOTAL_BUDGET_S, most of it in the sources' polite pacing sleeps.
    """
    source = StubSource("counting", {"title"})
    resolution = resolve_queries(
        [_q("title", "a title")],
        bib_doi_index={},
        sources=[source],
        should_cancel=lambda: True,
    )
    calls = source.calls

    assert calls == [], "no source may be called once cancellation is requested"
    assert any("cancelled" in note for note in resolution.notes), resolution.notes
