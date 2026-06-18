"""Coverage for the hand-rolled BibTeX parser (ported from kb_harmonizer)."""

from __future__ import annotations

from pathlib import Path

import pytest

from audit.core import bib


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "x.bib"
    p.write_text(text, encoding="utf-8")
    return p


def test_empty_file(tmp_path: Path) -> None:
    assert bib.parse_bib(_write(tmp_path, "")) == []


def test_missing_file(tmp_path: Path) -> None:
    assert bib.parse_bib(tmp_path / "nope.bib") == []


def test_single_entry_minimal(tmp_path: Path) -> None:
    p = _write(tmp_path, "@article{smith2020, title = {Hello}}")
    entries = bib.parse_bib(p)
    assert len(entries) == 1
    e = entries[0]
    assert e.citation_key == "smith2020"
    assert e.entry_type == "article"
    assert e.title == "Hello"
    assert e.year is None
    assert e.authors == []
    assert e.keywords == set()


def test_all_common_fields(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "@book{doe2019, "
        "title = {A Book}, "
        "year = {2019}, "
        "author = {Doe, John and Roe, Jane}, "
        "keywords = {alpha, beta, #gamma}"
        "}",
    )
    e = bib.parse_bib(p)[0]
    assert e.title == "A Book"
    assert e.year == "2019"
    assert e.authors == ["Doe, John", "Roe, Jane"]
    assert e.keywords == {"alpha", "beta", "gamma"}


def test_nested_braces_in_title(tmp_path: Path) -> None:
    p = _write(tmp_path, "@article{k1, title = {Some {LaTeX} word}, year = {2020}}")
    e = bib.parse_bib(p)[0]
    assert e.title == "Some {LaTeX} word"
    assert e.year == "2020"


def test_quoted_string_field(tmp_path: Path) -> None:
    p = _write(tmp_path, '@misc{k2, title = "Quoted title", year = "1999"}')
    e = bib.parse_bib(p)[0]
    assert e.title == "Quoted title"
    assert e.year == "1999"


def test_bare_year(tmp_path: Path) -> None:
    p = _write(tmp_path, "@misc{k3, year = 2024}")
    e = bib.parse_bib(p)[0]
    assert e.year == "2024"


def test_multiple_entries(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "@article{a, title={A}, year={2020}}\n"
        "@book{b, title={B}, year={2021}}\n"
        "@misc{c, title={C}}\n",
    )
    entries = bib.parse_bib(p)
    assert [e.citation_key for e in entries] == ["a", "b", "c"]
    assert [e.entry_type for e in entries] == ["article", "book", "misc"]


def test_keywords_semicolon_separated(tmp_path: Path) -> None:
    p = _write(tmp_path, "@article{k4, keywords = {foo; bar; baz}}")
    e = bib.parse_bib(p)[0]
    assert e.keywords == {"foo", "bar", "baz"}


def test_authors_with_accents(tmp_path: Path) -> None:
    p = _write(tmp_path, "@article{k5, author = {Müller, Hans and García, Ana}}")
    e = bib.parse_bib(p)[0]
    assert e.authors == ["Müller, Hans", "García, Ana"]


def test_entry_type_is_lowercased(tmp_path: Path) -> None:
    p = _write(tmp_path, "@Article{k6, title = {T}}")
    assert bib.parse_bib(p)[0].entry_type == "article"


def test_index_by_key(tmp_path: Path) -> None:
    p = _write(tmp_path, "@a{one, title={1}}\n@b{two, title={2}}")
    idx = bib.index_by_key(bib.parse_bib(p))
    assert set(idx) == {"one", "two"}
    assert idx["one"].title == "1"


def test_iter_entries_streams(tmp_path: Path) -> None:
    p = _write(tmp_path, "@a{one, title={1}}\n@b{two, title={2}}")
    keys = [e.citation_key for e in bib.iter_entries(p)]
    assert keys == ["one", "two"]


def test_entry_with_trailing_comma(tmp_path: Path) -> None:
    p = _write(tmp_path, "@article{k7, title = {Trailing}, year = {2020},}")
    e = bib.parse_bib(p)[0]
    assert e.title == "Trailing"
    assert e.year == "2020"


def test_non_entry_content_is_skipped(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "% This is a bibtex comment\n"
        "Some loose text\n"
        "@article{good, title = {OK}}\n"
        "more text\n",
    )
    entries = bib.parse_bib(p)
    assert [e.citation_key for e in entries] == ["good"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("{hello}", "hello"),
        ('"hello"', "hello"),
        ("  {{nested}}  ", "nested"),
        ("plain", "plain"),
    ],
)
def test_strip_braces(raw: str, expected: str) -> None:
    assert bib._strip_braces(raw) == expected
