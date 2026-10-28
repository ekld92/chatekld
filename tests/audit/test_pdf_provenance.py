"""Batch C (2026-08-26): what an orphan PDF physically IS.

The accession ladder is built for journal articles; the orphan residual mostly
is not one. Measured against a random sample of 400 mapped PDFs, orphans run
to 27% over 30 pages (against 8%) with a 90th percentile of 300 pages (against
26). Classifying them is what lets the UI stop offering a DOI search to a
printed web page.

The classifier is advisory ROUTING evidence, never a decision — so the rule
running through these tests is that an unknown input degrades to ``article``
(the ladder's home ground) rather than to a wrong class.
"""

from __future__ import annotations

import pytest

from audit.core import pdf_provenance as pp


# --- classification --------------------------------------------------------

@pytest.mark.parametrize("producer,expected", [
    ("Skia/PDF m120", "browser_print"),
    ("Chromium", "browser_print"),
    # Deliberately NOT browser_print: Quartz PDFContext is every macOS app's
    # print producer, not a browser's. See the weak-marker tests below.
    ("macOS Version 14.5 Quartz PDFContext", "article"),
    ("wkhtmltopdf", "browser_print"),
    ("Microsoft® Word for Microsoft 365", "office_export"),
    ("Acrobat Distiller 9.0.0 (Windows)", "publisher"),
    ("Canon iR-ADV C5535", "scan"),
    ("Acrobat 5.0 Paper Capture Plug-in", "scan"),
])
def test_producer_markers(producer, expected):
    assert pp.classify(producer, "", 12) == expected


def test_an_unknown_producer_degrades_to_article_not_a_guess():
    assert pp.classify("SomeTool 9", "", 12) == "article"
    assert pp.classify(None, None, 0) == "article"
    assert pp.classify("", "", 12) == "article"


def test_page_count_alone_identifies_a_book():
    """The measured no-producer orphan class averages 499 pages; an ordinary
    article's 90th percentile is 26."""
    assert pp.classify("", "", 400) == "book"
    assert pp.classify("", "", 12) == "article"


def test_a_very_long_publisher_pdf_is_a_book_not_an_article():
    """A whole proceedings volume carries a publisher toolchain fingerprint
    but wants an ISBN, not a DOI — the page count wins."""
    assert pp.classify("Acrobat Distiller 9.0.0", "", 600) == "book"
    assert pp.classify("Acrobat Distiller 9.0.0", "", 20) == "publisher"


def test_a_long_browser_print_stays_a_browser_print():
    """Measured: browser prints average 114 pages. They must NOT be reclassed
    as books — the remedy is the URL, and an ISBN search would never work.
    `Skia/PDF` is a real browser fingerprint, so the page count never wins."""
    assert pp.classify("Skia/PDF", "", 300) == "browser_print"


# ── the weak macOS marker ──────────────────────────────────────────────────
#
# `Quartz PDFContext` is the generic macOS graphics-layer producer: Preview,
# Word's Print→Save as PDF, any Cocoa app. It is NOT a browser fingerprint.
# 36 of the 37 rows in this class on the live library carried it, 13 of them
# book-length (up to 845 pages) with no recoverable URL — each badged "printed
# web page … add it by URL in Zotero", which is a wrong instruction, not just
# a wrong label. `/Creator` separates the two cases cleanly.

def test_bare_quartz_is_not_evidence_of_a_browser():
    """An 845-page macOS-printed volume is a book, not a printed web page."""
    prod = "macOS Version 26.3 (Build 25D125) Quartz PDFContext"
    assert pp.classify(prod, "", 845) == "book"
    assert pp.classify(prod, "", 6) == "article"


def test_quartz_with_a_browser_creator_is_still_a_browser_print():
    """The live Safari prints carry it in /Creator; those must not regress."""
    prod = "macOS Version 12.6.1 (Build 21G217) Quartz PDFContext"
    assert pp.classify(prod, "Safari", 16) == "browser_print"


def test_quartz_with_a_recovered_page_url_is_a_browser_print():
    """A print whose page-1 header still carries its URL is corroborated even
    with an empty /Creator — the URL is the whole remedy for the class."""
    prod = "macOS Version 13.4.1 (Build 22F82) Quartz PDFContext"
    assert pp.classify(prod, "", 122) == "book"
    assert pp.classify(prod, "", 122,
                       source_url="https://example.org/x") == "browser_print"


def test_quartz_no_longer_falls_through_to_the_publisher_marker():
    """`tex` used to match `PDFContext` as a substring; the only thing keeping
    a Quartz file out of `publisher` was browser_print being tested first."""
    assert pp.classify("Quartz PDFContext", "", 10) == "article"


def test_the_book_threshold_is_a_parameter_not_a_constant():
    assert pp.classify("", "", 40, book_min_pages=30) == "book"
    assert pp.classify("", "", 40, book_min_pages=100) == "article"


def test_the_creator_field_is_considered_too():
    """Some browsers write only /Creator."""
    assert pp.classify("", "Chrome", 5) == "browser_print"


def test_every_produced_class_is_declared():
    seen = {
        pp.classify("Skia", "", 3), pp.classify("", "", 500),
        pp.classify("Word", "", 3), pp.classify("Canon", "", 3),
        pp.classify("Distiller", "", 3), pp.classify("?", "", 3),
    }
    assert seen <= set(pp.PROVENANCE_CLASSES)


# --- source-URL recovery ---------------------------------------------------

def test_url_is_recovered_from_page_text():
    text = "Some article\nhttps://www.example.org/articles/42\nBody"
    assert pp.extract_source_url(text) == "https://www.example.org/articles/42"


def test_metadata_wins_over_page_text():
    """A browser writes the page URL into /Info; the print footer's copy can
    be truncated by layout."""
    got = pp.extract_source_url("https://wrong.example/x",
                                info_blob="https://right.example/page")
    assert got == "https://right.example/page"


@pytest.mark.parametrize("noise", [
    "https://doi.org/10.1000/xyz",
    "https://creativecommons.org/licenses/by/4.0/",
    "http://www.w3.org/1999/xlink",
    "https://orcid.org/0000-0002-1825-0097",
])
def test_boilerplate_urls_are_never_the_source(noise):
    """These appear on almost every scholarly PDF and would otherwise win by
    being first in the text."""
    assert pp.extract_source_url(f"{noise}\nhttps://real.example/p") \
        == "https://real.example/p"


def test_trailing_punctuation_is_stripped():
    assert pp.extract_source_url("see https://example.org/a/b).") \
        == "https://example.org/a/b"


def test_no_url_is_none_not_an_empty_string():
    assert pp.extract_source_url("no links here") is None
    assert pp.extract_source_url(None, None) is None


# --- serialization ---------------------------------------------------------

def test_both_orphan_reports_carry_the_class_and_neither_crashes_with_rows():
    """`unread_unzoterod` is a FILTER over the same PDFs as `read_unzoterod`,
    so it must carry the same badge — a class on one tab and not the other
    would read as a difference in the data rather than in the filter.

    This also pins the shape ruff caught while the batch was written: the
    provenance splat was pasted into BOTH serializers but only one had the
    map in scope, so the subset report raised NameError for any non-empty row
    list — invisible to a test that only ever serialised an empty report.
    """
    from pathlib import Path
    from types import SimpleNamespace

    from audit import serialize
    from audit.engine.reports import read_unzoterod as r_read
    from audit.engine.reports import unread_unzoterod as r_unread

    pdf = Path("/vault/Z_attachments/biblio_articles/x_2020.pdf")
    settings = SimpleNamespace(vault_root=Path("/vault"))
    prov = {pdf: {"klass": "browser_print", "pages": 114,
                  "producer": "Skia/PDF", "source_url": "https://example.org/a"}}

    read_rep = r_read.ReadUnzoterodReport(
        rows=[r_read.ReadUnzoterodRow(pdf=pdf, annotations=0)])
    unread_rep = r_unread.UnreadUnzoterodReport(
        rows=[r_unread.UnreadUnzoterodRow(pdf=pdf, annotations=0)])

    for payload in (serialize.report_read_unzoterod(read_rep, settings, prov),
                    serialize.report_unread_unzoterod(unread_rep, settings, prov)):
        row = payload["rows"][0]
        assert row["klass"] == "browser_print"
        assert row["pages"] == 114
        assert row["source_url"] == "https://example.org/a"
        assert payload["provenance_classes"] == list(pp.PROVENANCE_CLASSES)


def test_a_row_with_no_provenance_serialises_exactly_as_before():
    """Absent is normal — a cancelled scan or a corrupt file simply has no
    class — and such a row must not grow empty class keys that the UI would
    render as a new category."""
    from pathlib import Path
    from types import SimpleNamespace

    from audit import serialize
    from audit.engine.reports import read_unzoterod as r_read

    pdf = Path("/vault/x.pdf")
    rep = r_read.ReadUnzoterodReport(
        rows=[r_read.ReadUnzoterodRow(pdf=pdf, annotations=3)])
    row = serialize.report_read_unzoterod(
        rep, SimpleNamespace(vault_root=Path("/vault")), None)["rows"][0]
    assert set(row) == {"pdf", "annotations", "error"}


# ── the provenance memo has to actually memoise ────────────────────────────
#
# `doi_cache` stores DOI strings under the `meta`/`text` fields and a small
# DICT under `prov`. `lookup` coerced anything non-str to None, so a `prov`
# hit came back `(True, None)`, the caller's `isinstance(cached, dict)` gate
# never passed, and every scan re-opened every residual PDF with pikepdf AND
# re-ran the page-1 text provider — measured at 11.8 s per scan on 371 files
# where a working memo serves them in ~1 ms, plus a needless rewrite of a
# 1.1 MB cache file. The docstring's "a warm scan pays nothing" described
# behaviour that did not occur.

def test_the_cache_round_trips_a_dict_field(tmp_path):
    from audit.core import doi_cache

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    cache: dict = {}
    entry = {"klass": "book", "pages": 400, "producer": "x", "source_url": None}
    doi_cache.store(pdf, cache, "prov", entry)
    hit, got = doi_cache.lookup(pdf, cache, "prov")
    assert hit is True
    assert got == entry, "a dict field must survive the round trip"


def test_string_fields_are_unchanged_by_the_widened_lookup(tmp_path):
    from audit.core import doi_cache

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    cache: dict = {}
    doi_cache.store(pdf, cache, "meta", "10.1/abc")
    assert doi_cache.lookup(pdf, cache, "meta") == (True, "10.1/abc")
    doi_cache.store(pdf, cache, "text", None)      # the cached negative
    assert doi_cache.lookup(pdf, cache, "text") == (True, None)


def test_a_corrupt_doi_field_still_reads_as_no_doi(tmp_path):
    """A hand-edited cache holding a non-string under `meta` must not leak a
    dict into the DOI join — the coercion moved to the call site, not away."""
    from audit.core import doi_cache
    from audit.engine import bridge

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    cache: dict = {}
    doi_cache.store(pdf, cache, "meta", {"not": "a doi"})
    hit, got = doi_cache.lookup(pdf, cache, "meta")
    assert hit is True
    assert bridge._cached_doi(got) is None


def test_a_warm_residual_pass_opens_no_pdf(tmp_path, monkeypatch):
    """The whole point of the memo. Red before the lookup fix: the second
    pass re-opened every file."""
    from audit.core import doi_cache
    from audit.engine import bridge

    pdf = tmp_path / "orphan.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    opens = {"n": 0}

    def _counting_meta(path):
        opens["n"] += 1
        return {"producer": "Skia/PDF", "creator": "", "pages": 3,
                "info_blob": ""}

    monkeypatch.setattr(bridge.core_pdf_ids, "read_pdf_meta", _counting_meta)
    monkeypatch.setattr(doi_cache, "cache_path", lambda: tmp_path / "doi_cache.json")

    first = bridge._classify_residual([pdf], None)
    assert first[pdf]["klass"] == "browser_print"
    assert opens["n"] == 1

    second = bridge._classify_residual([pdf], None)
    assert second[pdf] == first[pdf]
    assert opens["n"] == 1, "a warm pass must serve the memo, not re-open"


def test_a_textless_run_does_not_poison_the_memo_for_a_run_with_text(tmp_path, monkeypatch):
    """The memo key is (size, mtime_ns) and cannot see whether the scan had a
    page-1 text source. The CLI path has none; the app always does. Without a
    marker, a CLI scan would durably record "no source URL" for every printed
    web page and the app would serve that answer forever."""
    from audit.core import doi_cache
    from audit.engine import bridge

    pdf = tmp_path / "printed.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(doi_cache, "cache_path", lambda: tmp_path / "doi_cache.json")
    monkeypatch.setattr(
        bridge.core_pdf_ids, "read_pdf_meta",
        lambda path: {"producer": "Skia/PDF", "creator": "", "pages": 4,
                      "info_blob": ""})

    cli = bridge._classify_residual([pdf], None)          # no text provider
    assert cli[pdf]["klass"] == "browser_print"
    assert cli[pdf]["source_url"] is None

    app = bridge._classify_residual(
        [pdf], lambda p: "printed from https://example.org/article")
    assert app[pdf]["source_url"] == "https://example.org/article", \
        "a text-bearing run must not be served the text-less answer"

    # …and once answered, the answer sticks for a later text-less run.
    again = bridge._classify_residual([pdf], None)
    assert again[pdf]["source_url"] == "https://example.org/article"


def test_a_google_docs_export_is_not_a_browser_print():
    """`Skia/PDF m144 Google Docs Renderer` — Google Docs renders through
    Chrome's graphics library, so the generic engine name matched before the
    specific product name and a 208-page report came out "printed web page"."""
    prod = "Skia/PDF m144 Google Docs Renderer"
    # office_export deliberately ignores the page count (unlike `publisher`):
    # a 200-page Word/Docs file is a guideline or report, i.e. grey literature,
    # which is exactly the routing that class exists to give.
    assert pp.classify(prod, "", 208) == "office_export"
    assert pp.classify(prod, "", 12) == "office_export"
    assert pp.may_be_browser_print(prod, "") is False
