"""Sources: recorded-fixture parsing + the hardened-HTTP contract.

Fixtures under ``fixtures/`` are LIVE responses recorded 2026-08-19 through
the sources' own URL construction, so a parser test failing means either the
parser drifted or the recorded shape did — never a hand-written stub lying.
The transport tests pin the ``zotero_write``-mirrored client rules: redirects
refused, body read-capped before parsing, ``Retry-After`` honoured,
status-only errors.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
from unittest import mock

import pytest

from accession import sources as S


class _FakeResp:
    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = io.BytesIO(body)
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(body: bytes, headers: dict | None = None):
    """Patch the opener to answer every request with *body*."""
    return mock.patch.object(
        S, "_urlopen", lambda req, timeout: _FakeResp(body, headers))


def _http_error(code: int, headers: dict | None = None,
                body: bytes = b"") -> urllib.error.HTTPError:
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(
        "https://example.org/x", code, "err", msg, io.BytesIO(body))


# ── recorded-fixture parsing, one per source ───────────────────────────────


class TestRecordedFixtures:
    def test_crossref_doi_lookup(self, fixtures_dir):
        body = (fixtures_dir / "crossref_doi.json").read_bytes()
        with _serve(body):
            cands = S.CrossrefSource().run(
                "doi", "10.1001/jamapsychiatry.2024.3532")
        assert len(cands) == 1
        c = cands[0]
        assert c.title.startswith("Emergency Department Visits Involving")
        assert c.authors[0] == "Myran, Daniel T."
        assert c.year == 2025
        assert c.doi == "10.1001/jamapsychiatry.2024.3532"
        assert c.container == "JAMA Psychiatry"
        assert c.url == "https://doi.org/10.1001/jamapsychiatry.2024.3532"

    def test_crossref_title_search_returns_ranked_items(self, fixtures_dir):
        body = (fixtures_dir / "crossref_title.json").read_bytes()
        with _serve(body):
            cands = S.CrossrefSource().run("title", "whatever", rows=3)
        assert len(cands) == 3
        assert all(c.title and c.source == "crossref" for c in cands)

    def test_europepmc_doi_lookup_and_author_normalisation(self, fixtures_dir):
        body = (fixtures_dir / "epmc_doi.json").read_bytes()
        with _serve(body):
            cands = S.EuropePmcSource().run(
                "doi", "10.1001/jamapsychiatry.2024.3532")
        c = cands[0]
        # "Myran DT" must become "Myran, DT" — the bridge's surname recipe
        # takes the part before the comma, so without the transform the
        # initials would be read as the surname.
        assert c.authors[0] == "Myran, DT"
        assert c.pmid == "39535804"
        assert c.doi == "10.1001/jamapsychiatry.2024.3532"

    def test_arxiv_id_lookup(self, fixtures_dir):
        body = (fixtures_dir / "arxiv_id.xml").read_bytes()
        with _serve(body):
            cands = S.ArxivSource().run("arxiv", "2103.14030")
        c = cands[0]
        assert c.title.startswith("Swin Transformer")
        assert c.arxiv_id == "2103.14030"
        assert c.year == 2021
        assert c.container == "arXiv"

    def test_hal_doi_lookup(self, fixtures_dir):
        body = (fixtures_dir / "hal_doi.json").read_bytes()
        with _serve(body):
            cands = S.HalSource().run("doi", "10.1016/j.encep.2020.04.005")
        c = cands[0]
        assert c.title.startswith("Ensuring mental health care")
        assert c.year == 2020
        assert c.doi == "10.1016/j.encep.2020.04.005"
        assert "Astrid Chevance" in c.authors

    def test_thesesfr_search_parses_and_year_filters(self, fixtures_dir):
        body = (fixtures_dir / "thesesfr_search.json").read_bytes()
        with _serve(body):
            all_rows = S.ThesesFrSource().run("title", "hallucinations",
                                             rows=2)
            filtered = S.ThesesFrSource().run(
                "authoryear", "hallucinations", year="2016", rows=2)
        assert [c.year for c in all_rows] == [2016, None]
        assert all_rows[0].authors == ("Lehaire, Célia",)
        assert all_rows[0].url == "https://theses.fr/2016AIXM3079"
        # ±1-year filter drops nothing without a year, keeps the 2016 row —
        # a row with an unknown year is kept, never silently excluded.
        assert [c.year for c in filtered] == [2016, None]


# ── the hardened-transport contract ────────────────────────────────────────


class TestTransportContract:
    def test_redirects_are_refused_not_followed(self):
        handler = S._NoRedirect()
        req = urllib.request.Request("https://api.crossref.org/works/x")
        with pytest.raises(urllib.error.HTTPError):
            handler.redirect_request(
                req, None, 302, "Found", email.message.Message(),
                "http://evil.example/steal")

    def test_a_302_from_a_source_surfaces_as_an_error_not_a_follow(self):
        with mock.patch.object(
                S, "_urlopen",
                mock.Mock(side_effect=_http_error(302))):
            with pytest.raises(S.SourceHTTPError) as exc:
                S.CrossrefSource().run("doi", "10.1/x.1")
        assert exc.value.status == 302

    def test_body_is_read_capped_before_json_parsing(self):
        huge = b"[" + b"1," * S._MAX_BODY_BYTES   # would also be invalid JSON
        with _serve(huge), mock.patch.object(
                S.json, "loads",
                side_effect=AssertionError("parsed an uncapped body")):
            with pytest.raises(S.SourceError, match="size cap"):
                S.CrossrefSource().run("doi", "10.1/x.1")

    def test_retry_after_is_honoured_and_surfaced(self):
        with mock.patch.object(
                S, "_urlopen",
                mock.Mock(side_effect=_http_error(
                    429, {"Retry-After": "7"}))):
            with pytest.raises(S.SourceRateLimited) as exc:
                S.EuropePmcSource().run("doi", "10.1/x.1")
        assert exc.value.retry_after_s == 7.0
        # The host's next-call deadline was armed from the hint, so the next
        # call would wait instead of re-hitting the same 429 immediately.
        assert S._next_call_at["www.ebi.ac.uk"] > 0

    def test_http_errors_are_status_only_never_the_body(self):
        with mock.patch.object(
                S, "_urlopen",
                mock.Mock(side_effect=_http_error(
                    500, body=b"secret internal trace"))):
            with pytest.raises(S.SourceHTTPError) as exc:
                S.HalSource().run("doi", "10.1/x.1")
        assert "500" in str(exc.value)
        assert "secret" not in str(exc.value)

    def test_transport_failure_is_source_unavailable(self):
        with mock.patch.object(
                S, "_urlopen",
                mock.Mock(side_effect=OSError("connection refused"))):
            with pytest.raises(S.SourceUnavailable):
                S.CrossrefSource().run("doi", "10.1/x.1")

    def test_crossref_unknown_doi_is_a_clean_no_not_an_error(self):
        with mock.patch.object(
                S, "_urlopen", mock.Mock(side_effect=_http_error(404))):
            assert S.CrossrefSource().run("doi", "10.1/gone.1") == []

    def test_unsupported_kind_returns_empty_without_network(self):
        # The conftest tripwire would raise if this touched the opener.
        assert S.ArxivSource().run("doi", "10.1/x.1") == []
        assert S.ThesesFrSource().run("doi", "10.1/x.1") == []
        assert S.CrossrefSource().run("arxiv", "2103.14030") == []

    def test_empty_value_returns_empty_without_network(self):
        assert S.CrossrefSource().run("doi", "") == []

    def test_pacing_advances_the_host_deadline_after_a_call(self, fixtures_dir):
        body = (fixtures_dir / "crossref_doi.json").read_bytes()
        with _serve(body):
            S.CrossrefSource().run("doi", "10.1001/jamapsychiatry.2024.3532")
        assert S._next_call_at.get("api.crossref.org", 0) > 0

    def test_contact_email_rides_the_user_agent_only_when_set(self):
        seen = {}

        def _capture(req, timeout):
            seen["ua"] = req.get_header("User-agent")
            return _FakeResp(b"{}")

        with mock.patch.object(S, "_urlopen", _capture):
            S.CrossrefSource("me@example.org").run("title", "x", rows=1)
        assert "mailto:me@example.org" in seen["ua"]
        with mock.patch.object(S, "_urlopen", _capture):
            S.CrossrefSource().run("title", "x", rows=1)
        assert "mailto" not in seen["ua"]


def test_default_sources_registry_order_and_names():
    names = [s.name for s in S.default_sources()]
    # The two ISBN sources sit LAST: they serve a kind nothing else can, so
    # their position only decides which answers an ISBN first, and Open
    # Library is preferred over the Google endpoint.
    assert names == ["crossref", "europepmc", "arxiv", "hal", "thesesfr",
                     "openlibrary", "googlebooks"]


def test_isbn_is_served_only_by_the_book_sources():
    """A book ISBN must never be sent to an article catalogue: it would burn a
    call per orphan and can only ever come back empty."""
    by_kind = {s.name: s.supports for s in S.default_sources()}
    assert by_kind["openlibrary"] == frozenset({"isbn"})
    assert by_kind["googlebooks"] == frozenset({"isbn"})
    for article_source in ("crossref", "europepmc", "arxiv", "hal", "thesesfr"):
        assert "isbn" not in by_kind[article_source], article_source


def test_open_library_parses_an_edition_record():
    body = json.dumps({
        "title": "Les Troubles du Sommeil",
        "subtitle": "2e édition",
        "by_statement": "Arnulf, I. and Dauvilliers, Y.",
        "publish_date": "2019",
        "publishers": ["Elsevier Masson"],
    }).encode("utf-8")
    with _serve(body):
        got = S.OpenLibrarySource().run("isbn", "9782294748929")
    assert len(got) == 1
    assert got[0].title.startswith("Les Troubles du Sommeil")
    assert "2e édition" in got[0].title
    assert got[0].year == 2019
    assert got[0].container == "Elsevier Masson"


def test_open_library_treats_an_unknown_isbn_as_a_clean_no():
    """404 is "no such book", not a source failure — it must not raise into
    the resolver, which degrades only SourceError."""
    err = _http_error(404)
    with mock.patch.object(S, "_urlopen",
                           side_effect=lambda req, timeout: (_ for _ in ()).throw(err)):
        assert S.OpenLibrarySource().run("isbn", "9782294748929") == []


def test_google_books_parses_a_volume():
    body = json.dumps({"items": [{"volumeInfo": {
        "title": "Handbook of Anger",
        "authors": ["Colin Martin", "Victor R. Preedy"],
        "publishedDate": "2023-05-01",
        "publisher": "Springer",
        "infoLink": "https://books.google.com/x",
    }}]}).encode("utf-8")
    with _serve(body):
        got = S.GoogleBooksSource().run("isbn", "9783030987114")
    assert got[0].title == "Handbook of Anger"
    assert got[0].year == 2023
    assert got[0].authors == ("Colin Martin", "Victor R. Preedy")


def test_google_books_with_no_items_returns_nothing():
    with _serve(json.dumps({"totalItems": 0}).encode("utf-8")):
        assert S.GoogleBooksSource().run("isbn", "9783030987114") == []


_STUB_WORK = {
    "DOI": "10.1016/s0246-1072(18)83092-9",
    "title": ["10.1016/S0246-1072(18)83092-9"],
    "issued": {"date-parts": [[2000]]},
}


def test_a_crossref_stub_is_kept_on_a_DOI_lookup_but_never_titled():
    """Reverses the 2026-08-19 blanket refusal, on measurement (2026-08-26).

    Legacy PII-style DOIs resolve to registry stubs whose ``title`` IS the DOI
    string. Refusing the candidate outright made the rung report "nothing
    found" for an identifier Crossref had just CONFIRMED exists — measured at
    2 of the maintainer's 20 EMC entries (10%).

    On a DOI lookup the identifier is itself the evidence, and both downstream
    outcomes (link when the DOI is in the bib, or copy-for-Zotero when it is
    not) use the DOI and never the title — so dropping the row lost a real
    re-link. The refusal of a DOI-SHAPED TITLE stands: the candidate comes back
    with ``title=None`` rather than a title that is a DOI.
    """
    body = json.dumps({"message": _STUB_WORK}).encode("utf-8")
    with _serve(body):
        got = S.CrossrefSource().run("doi", "10.1016/s0246-1072(18)83092-9")
    assert len(got) == 1
    assert got[0].doi == "10.1016/s0246-1072(18)83092-9"
    assert got[0].title is None


def test_a_stub_is_still_refused_on_a_TITLE_search():
    """The half of the 2026-08-19 decision that stands. A candidate with no
    title cannot be scored against the query, so on a title search it is
    genuinely useless — and it would occupy a slot a real match could take."""
    body = json.dumps({"message": {"items": [_STUB_WORK]}}).encode("utf-8")
    with _serve(body):
        assert S.CrossrefSource().run("title", "Sémiologie en psychiatrie") == []


def test_a_doi_lookup_without_the_identifier_is_still_refused():
    """A shapeless payload must not become a confident answer just because
    the untitled path exists."""
    body = json.dumps({"message": {"title": ["10.1/x"]}}).encode("utf-8")
    with _serve(body):
        assert S.CrossrefSource().run("doi", "10.1016/x") == []


def test_a_malformed_crossref_work_skips_instead_of_crashing_the_resolution():
    """resolve_queries._call degrades only SourceError — an escaping
    TypeError/KeyError from a hostile/broken record shape would fail the
    whole PDF (and, repeated, abort a batch). One bad record must skip."""
    src = S.CrossrefSource()
    # "title" as a dict: subscripting raised KeyError pre-fix.
    assert src._parse_work({"title": {"bogus": 1}}) is None
    # "issued" as a list: .get raised AttributeError pre-fix.
    assert src._parse_work({"title": ["Ok title of a paper"],
                            "issued": ["bogus"]}) is None
    # A well-formed sibling in the same response still parses.
    cand = src._parse_work({"title": ["Ok title of a paper"],
                            "issued": {"date-parts": [[2021]]},
                            "DOI": "10.1/x.1"})
    assert cand is not None and cand.year == 2021


def test_crossref_subtitle_is_joined_into_the_title():
    """Crossref splits many titles at the colon; without `subtitle` a
    candidate rendered as a stub ("Bipolarity from ancient to modern
    times:"). The select list requests it and _parse_work joins it."""
    src = S.CrossrefSource()
    assert "subtitle" in src._SELECT.split(",")
    cand = src._parse_work({
        "title": ["Bipolarity from ancient to modern times:"],
        "subtitle": ["conception, birth and rebirth"],
        "issued": {"date-parts": [[2000]]},
        "DOI": "10.1/sub.1",
    })
    assert cand.title == ("Bipolarity from ancient to modern times: "
                          "conception, birth and rebirth")
    # A subtitle already contained in the title is not doubled.
    cand2 = src._parse_work({
        "title": ["Full title: the subtitle"],
        "subtitle": ["The subtitle"],
        "issued": {"date-parts": [[2000]]},
        "DOI": "10.1/sub.2",
    })
    assert cand2.title == "Full title: the subtitle"


def test_the_container_constraint_reaches_the_crossref_url():
    """Measured on the maintainer's 20 EMC entries: unconstrained, a title
    search lands on the right DOI 10 times and returns a CONFIDENT WRONG
    record 4 times ("Personnalité narcissique" → a Kernberg volume at 0.92).
    Adding the venue takes it to 14 right and 1 wrong — a safety improvement
    as much as a recall one, and it only works if the parameter survives."""
    seen = {}

    def _capture(req, timeout):
        seen["url"] = req.full_url
        return _FakeResp(json.dumps({"message": {"items": []}}).encode("utf-8"))

    with mock.patch.object(S, "_urlopen", _capture):
        S.CrossrefSource().run("title", "Paranoïa", container="EMC")
    assert "query.container-title=EMC" in seen["url"]

    with mock.patch.object(S, "_urlopen", _capture):
        S.CrossrefSource().run("title", "Paranoïa")
    # NOT a bare "container-title" check: `select=` always names that field,
    # so the query PARAMETER is what must be absent.
    assert "query.container-title" not in seen["url"]


def test_a_source_that_cannot_narrow_never_receives_the_container():
    """Europe PMC has no venue filter. It must still answer — returning
    nothing because it cannot honour a constraint would be worse than
    answering unconstrained — so `run` drops the keyword for it."""
    src = S.EuropePmcSource()
    assert src.supports_container is False
    with _serve(json.dumps({"resultList": {"result": []}}).encode("utf-8")):
        # No TypeError: the keyword is filtered out by Source.run.
        assert src.run("title", "x", container="EMC") == []
