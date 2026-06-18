"""Coverage for the bridge's filename normalization helpers
(ported from kb_harmonizer)."""

from __future__ import annotations

import pytest

from audit.engine import bridge


def test_strip_accents() -> None:
    assert bridge._strip_accents("Müller") == "Muller"
    assert bridge._strip_accents("García") == "Garcia"
    assert bridge._strip_accents("plain") == "plain"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Smith", "smith"),
        ("Das-Munshi, Jayati", "dasmunshi"),
        ("Müller", "muller"),
        ("Doe, John", "doe"),
        ("García López", "lopez"),
        ("", ""),
    ],
)
def test_normalize_author_lastname(raw: str, expected: str) -> None:
    assert bridge._normalize_author_lastname(raw) == expected


def test_filename_authoryear_simple() -> None:
    assert bridge._filename_authoryear_candidates("smith_2020") == [("smith", "2020")]


def test_filename_authoryear_strips_leading_numeric_prefix() -> None:
    assert bridge._filename_authoryear_candidates("0_smith_2020") == [("smith", "2020")]
    assert bridge._filename_authoryear_candidates("12_smith_2020") == [
        ("smith", "2020")
    ]


def test_filename_authoryear_strips_trailing_copy_index() -> None:
    assert bridge._filename_authoryear_candidates("smith_2020_1") == [("smith", "2020")]
    assert bridge._filename_authoryear_candidates("smith_2020_99") == [
        ("smith", "2020")
    ]


def test_filename_authoryear_two_pass_multi_author() -> None:
    cands = bridge._filename_authoryear_candidates("smith_jones_2020")
    assert cands == [("smithjones", "2020"), ("smith", "2020")]


def test_filename_authoryear_hyphenated_in_filename() -> None:
    cands = bridge._filename_authoryear_candidates("das_munshi_2020")
    assert ("dasmunshi", "2020") in cands
    assert ("das", "2020") in cands


def test_filename_authoryear_picks_rightmost_year() -> None:
    assert bridge._filename_authoryear_candidates("smith_1999_2020") == [
        ("smith", "2020"),
    ]


def test_filename_authoryear_no_year_yields_nothing() -> None:
    assert bridge._filename_authoryear_candidates("smith_jones") == []


def test_filename_authoryear_single_token_yields_nothing() -> None:
    assert bridge._filename_authoryear_candidates("smith") == []


def test_filename_authoryear_year_first_yields_nothing() -> None:
    assert bridge._filename_authoryear_candidates("2020_smith") == []


def test_normalize_filename_authoryear_returns_primary() -> None:
    assert bridge._normalize_filename_authoryear("0_smith_jones_2020") == (
        "smithjones",
        "2020",
    )
    assert bridge._normalize_filename_authoryear("smith") is None


def test_fm_extract_year_from_iso_date() -> None:
    assert bridge._fm_extract_year({"date": "2020-04-15"}) == "2020"


def test_fm_extract_year_from_bare_int() -> None:
    assert bridge._fm_extract_year({"year": 2019}) == "2019"


def test_fm_extract_year_none_when_absent() -> None:
    assert bridge._fm_extract_year({"title": "x"}) is None


def test_fm_extract_first_author_string() -> None:
    assert bridge._fm_extract_first_author({"author": "Smith, John"}) == "Smith, John"


def test_fm_extract_first_author_list() -> None:
    assert bridge._fm_extract_first_author({"authors": ["Smith", "Jones"]}) == "Smith"


def test_fm_extract_first_author_dict() -> None:
    assert (
        bridge._fm_extract_first_author({"creators": [{"family": "Doe", "given": "J"}]})
        == "Doe"
    )


def test_fm_extract_first_author_missing() -> None:
    assert bridge._fm_extract_first_author({"title": "x"}) is None


def test_fm_pdf_pointers_strips_wikilink_alias() -> None:
    out = bridge._fm_extract_pdf_pointers({"pdf": "[[smith_2020.pdf|Smith 2020]]"})
    assert out == ["smith_2020.pdf"]


def test_fm_pdf_pointers_strips_wikilink_section() -> None:
    out = bridge._fm_extract_pdf_pointers({"pdf": "[[smith_2020.pdf#page=5|Smith]]"})
    assert out == ["smith_2020.pdf"]


def test_fm_pdf_pointers_handles_url_encoded_space() -> None:
    out = bridge._fm_extract_pdf_pointers({"pdf": "Z_attachments/smith%202020.pdf"})
    assert out == ["Z_attachments/smith 2020.pdf"]


def test_fm_pdf_pointers_markdown_link() -> None:
    out = bridge._fm_extract_pdf_pointers(
        {"pdf": "[Smith](Z_attachments/smith_2020.pdf)"}
    )
    assert out == ["Z_attachments/smith_2020.pdf"]


def test_fm_pdf_pointers_list_value() -> None:
    out = bridge._fm_extract_pdf_pointers({"pdfs": ["a.pdf", "b.pdf"]})
    assert out == ["a.pdf", "b.pdf"]


def test_fm_pdf_pointers_case_insensitive_key() -> None:
    out = bridge._fm_extract_pdf_pointers({"PDF": "x.pdf"})
    assert out == ["x.pdf"]


def test_fm_pdf_pointers_rejects_unknown_keys() -> None:
    out = bridge._fm_extract_pdf_pointers({"unrelated": "x.pdf"})
    assert out == []


def test_fm_pointer_field_present() -> None:
    assert bridge._fm_pointer_field_present({"pdf": "x.pdf"}) == "pdf"
    assert bridge._fm_pointer_field_present({"FILES": ["a.pdf"]}) == "files"
    assert bridge._fm_pointer_field_present({}) is None
    assert bridge._fm_pointer_field_present({"title": "x"}) is None
