"""Pins for the pure query builder: ladder ordering, dedupe, title heuristic.

``identify`` is the ladder's front half — everything here is offline by
construction (the conftest tripwire would catch any regression on that).
"""

from __future__ import annotations

from accession.identify import Query, extract_isbn, extract_title, identify

# A realistic page-1 shape: journal header, DOI line, article-type label,
# a two-line title, an author line with initials, affiliations, abstract.
PAGE1 = """\
Journal of Psychiatric Research 178 (2026) 112-119

https://doi.org/10.1016/j.jpsychires.2026.01.001

Original article

Cannabis use and transition to psychosis in young adults:
a longitudinal cohort study

D. T. Myran, M. Pugliese, J. Smith

Department of Psychiatry, University of Ottawa, Ottawa, Canada
Corresponding author: myran@uottawa.ca

Abstract
Background: cannabis use has been associated with psychosis transition in
several cohorts, but the dose-response relationship remains unclear.
"""


class TestIdentifyRanking:
    def test_full_evidence_yields_the_ladder_in_order(self):
        qs = identify("10.1234/embedded.1", PAGE1, "myran_2024")
        kinds = [(q.kind, q.evidence) for q in qs]
        assert kinds == [
            ("doi", "meta"),        # publisher-asserted, always first
            ("doi", "text"),        # printed on page 1 — weaker class
            ("title", "text"),
            ("authoryear", "filename"),
        ]
        assert qs[0].value == "10.1234/embedded.1"
        assert qs[1].value == "10.1016/j.jpsychires.2026.01.001"

    def test_printed_doi_identical_to_embedded_is_deduped(self):
        qs = identify("10.1016/j.jpsychires.2026.01.001", PAGE1, "myran_2024")
        assert [q.kind for q in qs].count("doi") == 1
        assert qs[0].evidence == "meta"

    def test_printed_arxiv_id_becomes_an_arxiv_query(self):
        text = "arXiv:2103.14030v2 [cs.CV] 25 Mar 2021\n\nSwin Transformer"
        qs = identify(None, text, "liu_2021")
        assert qs[0] == Query(kind="arxiv", value="2103.14030",
                              evidence="text", fn_authors=("liu",),
                              fn_year="2021")

    def test_filename_corroboration_rides_every_query(self):
        qs = identify("10.1/x.1", PAGE1, "0_das_munshi_2020")
        for q in qs:
            assert q.fn_authors == ("dasmunshi", "das")   # bridge's two forms
            assert q.fn_year == "2020"

    def test_trailing_copy_index_is_stripped_like_the_bridge(self):
        qs = identify(None, None, "pellat_2022_1")
        assert qs == [Query(kind="authoryear", value="pellat",
                            evidence="filename", fn_authors=("pellat",),
                            fn_year="2022")]

    def test_nothing_disclosed_yields_no_queries(self):
        # No meta, no text, and a stem with no parseable year.
        assert identify(None, None, "notes-scan") == []

    def test_malformed_embedded_doi_is_dropped_not_queried(self):
        qs = identify("not-a-doi", None, "smith_2020")
        assert all(q.kind != "doi" for q in qs)


class TestExtractTitle:
    def test_realistic_page_yields_the_merged_two_line_title(self):
        assert extract_title(PAGE1) == (
            "Cannabis use and transition to psychosis in young adults: "
            "a longitudinal cohort study")

    def test_scan_stops_at_the_abstract_label(self):
        text = (
            "Short header\n"
            "Abstract\n"
            "Background: this long abstract sentence would otherwise look "
            "exactly like a title and win on length alone.\n"
        )
        assert extract_title(text) is None

    def test_french_boilerplate_and_resume_label_are_handled(self):
        text = (
            "L'Encéphale 46 (2020) 193-201\n"
            "Reçu le 3 avril 2020 ; accepté le 8 avril 2020\n"
            "Disponible en ligne sur ScienceDirect\n"
            "Assurer les soins aux patients souffrant de troubles psychiques "
            "en France pendant l'épidémie\n"
            "Résumé\n"
            "La pandémie a désorganisé l'offre de soins psychiatriques.\n"
        )
        assert extract_title(text) == (
            "Assurer les soins aux patients souffrant de troubles psychiques "
            "en France pendant l'épidémie")

    def test_author_line_with_initials_never_wins(self):
        text = (
            "A perfectly reasonable single line title about dopamine\n"
            "J. Smith, A. B. Jones, C. Miller\n"
        )
        assert extract_title(text) == (
            "A perfectly reasonable single line title about dopamine")

    def test_no_text_degrades_to_none(self):
        assert extract_title(None) is None
        assert extract_title("") is None
        assert extract_title("12 34 56\n978-0-12-345678-9\n") is None


# --- ISBN (Batch D, 2026-08-26) --------------------------------------------

def test_isbn13_round_trips_and_a_bad_check_digit_is_refused():
    """The check digit is the whole point: page-1 text is full of 10- and
    13-digit runs (phone numbers, accession numbers, grant ids) and without
    validation every one of them would burn a network call."""
    from accession.identify import normalise_isbn
    assert normalise_isbn("978-2-294-74892-9") == "9782294748929"
    assert normalise_isbn("9782294748929") == "9782294748929"
    # Same digits, last one changed.
    assert normalise_isbn("9782294748928") is None


def test_isbn10_is_converted_to_isbn13():
    """Open Library and Google Books key on ISBN-13."""
    from accession.identify import normalise_isbn
    assert normalise_isbn("0-306-40615-2") == "9780306406157"
    assert normalise_isbn("030640615X") is None      # wrong check digit


def test_an_isbn10_ending_in_x_is_accepted():
    from accession.identify import normalise_isbn
    # 0-8044-2957-X is a textbook example of a valid X-check ISBN-10.
    assert normalise_isbn("0-8044-2957-X") is not None


def test_extract_isbn_finds_a_labelled_and_a_bare_one():
    from accession.identify import extract_isbn
    assert extract_isbn("ISBN: 978-2-294-74892-9\nrest") == "9782294748929"
    assert extract_isbn("catalogue 9782294748929 ok") == "9782294748929"


def test_extract_isbn_ignores_digit_runs_that_are_not_isbns():
    from accession.identify import extract_isbn
    assert extract_isbn("tel 0142161234567") is None
    assert extract_isbn("grant 1234567890123") is None
    assert extract_isbn("") is None
    assert extract_isbn(None) is None


def test_an_isbn_query_is_emitted_before_the_title_one():
    """A valid check digit is publisher-asserted identity — the same class of
    evidence as an embedded DOI — so it must outrank a title guess."""
    from accession.identify import identify
    qs = identify(embedded_doi=None,
                  page1_text="Les Troubles du Sommeil\nI. Arnulf\n"
                        "ISBN 978-2-294-74892-9",
                  filename_stem="arnulf_2019")
    kinds = [q.kind for q in qs]
    assert "isbn" in kinds
    if "title" in kinds:
        assert kinds.index("isbn") < kinds.index("title")


def test_a_query_carries_no_container_unless_one_is_given():
    """Nothing in the app can infer a venue for an unidentified PDF; the
    constraint is only ever user-supplied."""
    from accession.identify import identify
    qs = identify(embedded_doi=None, page1_text="A Title\nA Author",
                  filename_stem="author_2020")
    assert all(q.container is None for q in qs)


# ── which candidate wins ───────────────────────────────────────────────────
#
# `extract_isbn` returned the FIRST check-digit-valid run. A 10-digit US
# toll-free number passes the ISBN-10 check about one time in eleven, and on
# the live library one did: `meyer_2019.pdf` carries `800-445-9714` above its
# real ISBN, so the extractor emitted a bogus ISBN-13 and DISCARDED the real
# one — a book losing the only resolver it has.

def test_a_phone_number_never_beats_the_labelled_isbn():
    text = "Orders: 800-445-9714\nISBN 978-1-60535-555-9\n"
    assert extract_isbn(text) == "9781605355559"


def test_a_phone_number_is_not_an_isbn_even_when_it_is_alone():
    """Group shape is the discriminator: a printed ISBN's LAST group is the
    single check character, so 3-3-4 cannot be one."""
    assert extract_isbn("Call 800-445-9714 today") is None
    assert extract_isbn("Tel 01 44 07 47 70") is None
    # A printing-impression line (2-2-2-2-2) is the same shape of noise.
    assert extract_isbn("28 27 26 25 24  10 9 8 7 6") is None


def test_an_explicit_label_wins_even_when_the_grouping_is_malformed():
    """`ISBN 9782340-061033` is a real live case: one stray hyphen, so the
    last group is six characters. The label is what makes it trustworthy, so
    the shape gate must not apply to a labelled run."""
    assert extract_isbn("ISBN 9782340-061033") == "9782340061033"


def test_an_unlabelled_but_well_formed_isbn_is_still_accepted():
    """Most printed ISBNs on this library carry no `ISBN` prefix at all."""
    assert extract_isbn("978-1-58562-345-7") == "9781585623457"
    assert extract_isbn("9781337559577") == "9781337559577"
    assert extract_isbn("0-8044-2957-X") == "9780804429573"


def test_the_live_corpus_of_isbn_candidates_still_resolves_the_same_way():
    """Replay every ISBN-shaped run the 371 residual PDFs actually printed.

    Hermetic by construction: the fixture stores the regex's own matches (the
    label included where the page carried one), not the PDFs, and replaying
    them through `extract_isbn` was verified to reproduce the full-text answer
    for all 56 files. It exists so a future change to the regex or to the
    selection rule shows its whole blast radius — 45 files where an ISBN is
    found and 11 where the candidates are all noise — instead of one hand-
    picked example.
    """
    import json
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "isbn_live_corpus.json"
    corpus = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(corpus) == 56

    actual = {name: extract_isbn("\n".join(row["runs"]))
              for name, row in corpus.items()}
    expected = {name: row["isbn"] for name, row in corpus.items()}
    assert actual == expected

    # The two rows the selection rule exists for, named so a regression is
    # legible rather than a diff of 56 entries.
    assert expected["meyer_2019.pdf"] == "9781605355559"      # not the phone
    assert expected["micoulaud_franchi_quiles_2021.pdf"] == "9782340061033"
