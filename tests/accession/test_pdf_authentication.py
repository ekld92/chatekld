"""PDF ⇄ record authentication over synthetic PDFs (phase 5, 2026-09-16).

Hermetic: the PDFs are built here with PyMuPDF; nothing reaches the network
(the package conftest trips on any opener use). The three traps the module
exists to name — a DOI printed only in the reference list, a title present
without its authors, and a replaced file — each have a case.
"""
from __future__ import annotations

import fitz
import pytest

from accession import pdf_authentication as pa

DOI = "10.1000/fixture.2026.001"
CITED = "10.1000/cited.2020.777"
TITLE = "Sleep outcomes after a brief behavioural intervention"


def _pdf(path, pages):
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        y = 72
        for line in text.split("\n"):
            page.insert_text((72, y), line, fontsize=10)
            y += 14
    doc.save(str(path))
    doc.close()


@pytest.fixture
def article(tmp_path):
    path = tmp_path / "martin_2026.pdf"
    _pdf(path, [
        f"{TITLE}\nClaire Martin, Ada Test, Grace Hopper\nJournal of Fixtures 12(3) 2026\ndoi:{DOI}",
        "Methods. 12 participants were enrolled; 3 withdrew (25 %).\nResults. 27 mg twice daily, p = 0.04.",
        f"References\n1. Other A. An unrelated paper. J Elsewhere 2020. https://doi.org/{CITED}",
    ])
    return path


def test_front_matter_doi_authenticates_with_passage_fingerprint(article):
    report = pa.authenticate(article, {"DOI": DOI, "title": TITLE,
                                       "creators": [{"lastName": "Martin"}, {"lastName": "Hopper"}]})
    assert report["status"] == "authenticated"
    assert report["doi"]["front_matter"] and report["doi"]["pages"] == [1]
    assert report["title"]["found"] and report["title"]["page"] == 1
    assert report["authors"]["found"] == ["Martin", "Hopper"] and report["authors"]["missing"] == []
    loc = next(item for item in report["locations"] if item["kind"] == "doi")
    assert DOI in loc["excerpt"] and len(loc["passage_fingerprint"]) == 64
    assert loc["passage_fingerprint"] == pa.passage_fingerprint(loc["excerpt"])
    assert report["pdf_sha256"] and report["independent_of_catalogue"]
    # Located passages are offered in the campaign's source shape and their
    # excerpts verify against the page text they came from.
    assert all(s["kind"] == "pdf_passage" and s["passage_fingerprint"] for s in report["sources"])
    assert not report["applicable"]


def test_doi_only_in_the_reference_list_is_not_this_document(article):
    """The misleading cited reference: the record carries the CITED paper's DOI."""
    report = pa.authenticate(article, {"DOI": CITED, "title": "Some other title entirely"})
    assert report["status"] == "reference_list_only"
    assert report["doi"]["hits"] == 1 and report["doi"]["pages"] == [3] and not report["doi"]["front_matter"]


def test_title_without_authors_is_partial_and_accented_authors_fold(article):
    report = pa.authenticate(article, {"title": TITLE, "creators": [{"lastName": "Nobody"}]})
    assert report["status"] == "partial"
    assert report["authors"]["missing"] == ["Nobody"]
    report = pa.authenticate(article, {"title": TITLE.upper(), "creators": [{"lastName": "Martin"}]})
    assert report["status"] == "authenticated"


def test_nothing_found_and_unreadable_file(article, tmp_path):
    report = pa.authenticate(article, {"DOI": "10.9999/none", "title": "Unrelated", "creators": []})
    assert report["status"] == "not_found" and report["locations"] == []
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-not-really")
    report = pa.authenticate(broken, {"DOI": DOI})
    assert report["status"] == "unavailable" and report["pdf_sha256"]
    report = pa.authenticate(tmp_path / "missing.pdf", {"DOI": DOI})
    assert report["status"] == "unavailable"


def test_scan_is_bounded_and_reports_truncation(tmp_path):
    path = tmp_path / "long.pdf"
    _pdf(path, ["page one text"] * 5)
    scan = pa.scan_pdf(path, max_pages=2)
    assert scan.page_count == 5 and len(scan.pages) == 2 and scan.truncated
    scan = pa.scan_pdf(path, max_chars=10)
    assert scan.truncated and sum(len(t) for _, t in scan.pages) <= 10


def test_surnames_from_every_creator_shape():
    assert pa.surnames([{"lastName": "Martin", "firstName": "C."}, {"name": "WHO Consortium"}]) == ["Martin", "WHO Consortium"]
    assert pa.surnames("Martin, Claire and Hopper, Grace") == ["Martin", "Hopper"]
    assert pa.surnames(["Li"]) == []  # too short to be a meaningful needle


def test_fold_is_symmetric_for_non_ascii_letters():
    """2026-09-17 review, F7: fold() kept only [0-9a-z] while the page map kept
    every letter, so a Greek character in a title split the needle and the
    title was never found on its own page."""
    title = "Role of α-synuclein in sleep disruption"
    scan = pa.Scan(sha256="x", page_count=1, pages=[(1, "Role of α-synuclein in sleep disruption\nA. Author")])
    hit = pa.locate_text(scan, "title", title)
    assert hit is not None and hit.page == 1
    assert pa.fold("α-Synuclein") == "α synuclein"


def test_an_author_word_inside_the_title_or_far_from_it_does_not_authenticate(tmp_path):
    """2026-09-17 review, F8: title + ANY author anywhere in the front matter
    authenticated — including a surname that is a word of the title itself."""
    path = tmp_path / "park.pdf"
    filler = "\n".join("Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor." for _ in range(24))
    _pdf(path, [f"Introduction to the park\n{filler}\nYoung"])
    report = pa.authenticate(path, {"title": "Introduction to the park",
                                    "creators": [{"lastName": "Young"}, {"lastName": "Park"}]})
    assert report["status"] == "partial", report["status"]
    near = tmp_path / "near.pdf"
    _pdf(near, ["Introduction to the park\nAda Young and Bo Park\nJournal 2020"])
    assert pa.authenticate(near, {"title": "Introduction to the park",
                                  "creators": [{"lastName": "Young"}]})["status"] == "authenticated"
