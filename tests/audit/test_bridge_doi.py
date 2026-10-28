"""Coverage for the bridge's exact DOI resolution rung (step 3.5, 2026-08-19).

The rung exists because a measurement of the maintainer's live library found
968 PDFs the bridge could not place, of which 390 were **already in
_master.bib** — missed by the author+year heuristic because the filename year
and the bib year disagree (online-first vs issue year) or the surname was
transliterated differently. These tests pin both halves: that the rung links
those files, and that it cannot re-own anything an earlier step claimed.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from audit.config import Settings
from audit.core.bib import BibEntry
from audit.core import doi_cache, pdf_identifiers
from audit.engine import bridge


def _settings(tmp_path: Path) -> Settings:
    vault = tmp_path / "vault"
    (vault / "Z_attachments" / "biblio_articles").mkdir(parents=True)
    (vault / "Z_Zotero_Notes").mkdir(parents=True)
    return Settings(vault_root=vault, mapping_file_override=tmp_path / "mapping.json")


def _pdf(settings: Settings, name: str, *, doi: str | None = None) -> Path:
    """A real one-page PDF, optionally carrying a DOI in its /Info dict."""
    p = settings.biblio_articles_dir / name
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page()
    if doi is not None:
        with pdf.open_metadata() as meta:
            meta["dc:identifier"] = f"https://doi.org/{doi}"
    pdf.save(p)
    pdf.close()
    return p


def _entry(key: str, author: str, year: str, doi: str | None = None) -> BibEntry:
    return BibEntry(
        citation_key=key, entry_type="article", title=key,
        year=year, authors=[author], doi=doi,
    )


# ── the case the rung was built for ───────────────────────────────────────


def test_doi_links_a_pdf_whose_year_disagrees_with_the_bib(tmp_path: Path) -> None:
    """Online-first vs issue year: filename says 2024, the bib says 2025.

    This is 68% of the real-world misses, 235 of them exactly ±1 year.
    """
    s = _settings(tmp_path)
    pdf = _pdf(s, "myran_2024.pdf", doi="10.1001/jamapsychiatry.2024.3532")
    entries = [_entry("myran2025", "Myran, Daniel", "2025",
                      "10.1001/jamapsychiatry.2024.3532")]

    r = bridge.build_bridge(s, entries, [pdf])

    assert r.pdf_to_bib == {pdf: "myran2025"}
    assert r.source_per_pdf[pdf] == "doi"
    assert r.unmapped_pdfs == []


def test_doi_links_across_a_transliterated_surname(tmp_path: Path) -> None:
    """``boege`` in the filename, ``Böge`` in the bib — 30% of real misses."""
    s = _settings(tmp_path)
    pdf = _pdf(s, "boege_2021.pdf", doi="10.1192/bjp.2020.165")
    entries = [_entry("boge2021", "Böge, Kerem", "2021", "10.1192/bjp.2020.165")]

    r = bridge.build_bridge(s, entries, [pdf])

    assert r.source_per_pdf[pdf] == "doi"


def test_doi_beats_a_disagreeing_author_year_guess(tmp_path: Path) -> None:
    """An exact identifier must win over a surname-and-year coincidence."""
    s = _settings(tmp_path)
    pdf = _pdf(s, "smith_2020.pdf", doi="10.1000/real")
    entries = [
        _entry("smithWrong2020", "Smith, Jane", "2020", "10.1000/other"),
        _entry("smithRight2019", "Smith, John", "2019", "10.1000/real"),
    ]

    r = bridge.build_bridge(s, entries, [pdf])

    assert r.pdf_to_bib[pdf] == "smithRight2019"
    assert r.source_per_pdf[pdf] == "doi"


# ── the rung must be purely additive for an already-mapped library ────────


def test_a_manual_mapping_still_wins_over_the_doi(tmp_path: Path) -> None:
    """Curation is the top of the ladder and the DOI rung sits far below it."""
    s = _settings(tmp_path)
    pdf = _pdf(s, "smith_2020.pdf", doi="10.1000/real")
    bridge.add_match(s.mapping_file, pdf, "curatedKey", s.vault_root)
    entries = [_entry("doiKey", "Smith, John", "2019", "10.1000/real")]

    r = bridge.build_bridge(s, entries, [pdf])

    assert r.pdf_to_bib[pdf] == "curatedKey"
    assert r.source_per_pdf[pdf] == "manual"


def test_a_frontmatter_pointer_still_wins_over_the_doi(tmp_path: Path) -> None:
    """Note-derived steps (2a/2b/3) are all above 3.5, so none can be re-owned."""
    s = _settings(tmp_path)
    pdf = _pdf(s, "smith_2020.pdf", doi="10.1000/real")
    (s.zotero_notes_dir / "fmKey.md").write_text(
        "---\npdf: smith_2020.pdf\n---\n\nbody\n", encoding="utf-8")
    entries = [
        _entry("fmKey", "Smith, Jane", "2020"),
        _entry("doiKey", "Other, Otto", "2019", "10.1000/real"),
    ]

    r = bridge.build_bridge(s, entries, [pdf])

    assert r.pdf_to_bib[pdf] == "fmKey"
    assert r.source_per_pdf[pdf] == "fm_pointer"


def test_no_doi_falls_through_to_author_year_unchanged(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    pdf = _pdf(s, "smith_2020.pdf")  # no embedded DOI
    entries = [_entry("smith2020", "Smith, Jane", "2020")]

    r = bridge.build_bridge(s, entries, [pdf])

    assert r.source_per_pdf[pdf] == "authoryear"


# ── refusals and diagnostics ──────────────────────────────────────────────


def test_a_doi_on_two_bib_entries_refuses_to_link_and_is_recorded(
    tmp_path: Path,
) -> None:
    """One DOI, two records: the bibliography is duplicated, not the PDF.

    The rung declines rather than picking an iteration-order winner — the
    bridge's standing posture — and records the collision, because it is a
    finding about the .bib worth surfacing on its own.
    """
    s = _settings(tmp_path)
    pdf = _pdf(s, "smith_2020.pdf", doi="10.1000/dup")
    entries = [
        _entry("dupA", "Smith, Jane", "2020", "10.1000/dup"),
        _entry("dupB", "Smith, Jane", "2021", "10.1000/dup"),
    ]

    r = bridge.build_bridge(s, entries, [pdf])

    assert r.doi_collisions == {"10.1000/dup": ["dupA", "dupB"]}
    # Step 4 still gets its turn — the DOI rung declining is not a claim.
    assert r.source_per_pdf[pdf] == "authoryear"


def test_only_unclaimed_pdfs_are_examined(tmp_path: Path) -> None:
    """The rung's cost is bounded to the residual set, not the whole corpus."""
    s = _settings(tmp_path)
    claimed = _pdf(s, "claimed_2020.pdf", doi="10.1000/a")
    residual = _pdf(s, "residual_1999.pdf", doi="10.1000/b")
    bridge.add_match(s.mapping_file, claimed, "curated", s.vault_root)
    entries = [_entry("b", "Nobody, N", "1888", "10.1000/b")]

    r = bridge.build_bridge(s, entries, [claimed, residual])

    assert r.doi_examined == 1
    assert r.pdf_to_bib[residual] == "b"


def test_printed_doi_is_used_only_when_metadata_has_none(tmp_path: Path) -> None:
    """The injected text provider is the fallback, never the primary source.

    A DOI printed in a PDF's text is often a *cited* work's; one asserted by
    the publisher in the file's own metadata is about this file. So a PDF
    carrying both must resolve to the embedded one.
    """
    s = _settings(tmp_path)
    both = _pdf(s, "both_2020.pdf", doi="10.1000/embedded")
    printed_only = _pdf(s, "printed_2020.pdf")
    entries = [
        _entry("embeddedKey", "A, A", "1900", "10.1000/embedded"),
        _entry("printedKey", "B, B", "1900", "10.1000/printed"),
        _entry("citedKey", "C, C", "1900", "10.1000/cited"),
    ]

    def provider(_p: Path) -> str:
        # The printed DOI nearest the top wins; a reference further down loses.
        return "Title\ndoi:10.1000/printed\n... references ... 10.1000/cited"

    r = bridge.build_bridge(s, entries, [both, printed_only],
                            pdf_text_provider=provider)

    assert r.pdf_to_bib[both] == "embeddedKey"
    assert r.pdf_to_bib[printed_only] == "printedKey"


def test_without_a_text_provider_the_rung_is_metadata_only(tmp_path: Path) -> None:
    """The CLI/test path degrades, it does not fail."""
    s = _settings(tmp_path)
    pdf = _pdf(s, "printed_2020.pdf")
    entries = [_entry("printedKey", "B, B", "1900", "10.1000/printed")]

    r = bridge.build_bridge(s, entries, [pdf])  # no provider

    assert r.unmapped_pdfs == [pdf]


def test_a_failing_text_provider_never_aborts_the_scan(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    pdf = _pdf(s, "smith_2020.pdf")
    entries = [_entry("smith2020", "Smith, Jane", "2020")]

    def boom(_p: Path) -> str:
        raise OSError("cache exploded")

    r = bridge.build_bridge(s, entries, [pdf], pdf_text_provider=boom)

    assert r.source_per_pdf[pdf] == "authoryear"


# ── the extraction primitive itself ───────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("10.1000/plain", "10.1000/plain"),
    ("https://doi.org/10.1000/URLform", "10.1000/urlform"),
    ("doi:10.1000/Prefixed", "10.1000/prefixed"),
])
def test_embedded_doi_is_normalised_like_bib_entry_doi(
    tmp_path: Path, raw: str, expected: str
) -> None:
    """Both sides of the join must canonicalise identically or it never fires."""
    s = _settings(tmp_path)
    pdf = _pdf(s, "x_2020.pdf", doi=raw)
    assert pdf_identifiers.extract_embedded_doi(pdf) == expected


def test_unreadable_pdf_yields_no_doi_rather_than_raising(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")
    assert pdf_identifiers.extract_embedded_doi(broken) is None
    assert pdf_identifiers.pdf_doi(broken) is None


# ── the evidence-class split (step 3.5 vs step 4.5) ───────────────────────


def test_a_printed_doi_never_overrules_an_author_year_match(tmp_path: Path) -> None:
    """The split's whole point.

    A DOI printed on page 1 can belong to a neighbouring article on the same
    journal page. Measured on the live library, letting printed DOIs override
    author+year produced 13 re-assignments, several of them to a completely
    different paper (a World Psychiatry page resolving to its neighbour's
    DOI). As a gap-filler the same evidence is fine; as an override it is not.
    """
    s = _settings(tmp_path)
    pdf = _pdf(s, "luykx_2023.pdf")  # no embedded DOI
    entries = [
        _entry("luykx2023", "Luykx, Jurjen", "2023"),
        _entry("teo2023", "Teo, Alan", "2023", "10.1000/neighbour"),
    ]

    def provider(_p: Path) -> str:
        return "World Psychiatry 22:3 ... doi:10.1000/neighbour"

    r = bridge.build_bridge(s, entries, [pdf], pdf_text_provider=provider)

    assert r.pdf_to_bib[pdf] == "luykx2023"
    assert r.source_per_pdf[pdf] == "authoryear"


def test_a_printed_doi_still_fills_a_gap(tmp_path: Path) -> None:
    """152 of the 389 real recoveries came from printed text — keep them."""
    s = _settings(tmp_path)
    pdf = _pdf(s, "nobody_1999.pdf")
    entries = [_entry("realKey", "Someone, S", "2024", "10.1000/printed")]

    def provider(_p: Path) -> str:
        return "Title\ndoi:10.1000/printed"

    r = bridge.build_bridge(s, entries, [pdf], pdf_text_provider=provider)

    assert r.pdf_to_bib[pdf] == "realKey"
    assert r.source_per_pdf[pdf] == "doi_text"


def test_embedded_doi_still_overrules_author_year(tmp_path: Path) -> None:
    """The trusted class keeps its correcting power (20 real corrections)."""
    s = _settings(tmp_path)
    pdf = _pdf(s, "smith_2020.pdf", doi="10.1000/embedded")
    entries = [
        _entry("smithGuess2020", "Smith, Jane", "2020"),
        _entry("smithTrue2021", "Smith, Jane", "2021", "10.1000/embedded"),
    ]

    r = bridge.build_bridge(s, entries, [pdf])

    assert r.pdf_to_bib[pdf] == "smithTrue2021"
    assert r.source_per_pdf[pdf] == "doi"


def test_the_printed_rung_only_examines_what_author_year_left(
    tmp_path: Path,
) -> None:
    s = _settings(tmp_path)
    claimed = _pdf(s, "smith_2020.pdf")
    residual = _pdf(s, "nobody_1999.pdf")
    entries = [_entry("smith2020", "Smith, Jane", "2020", "10.1000/x")]

    r = bridge.build_bridge(s, entries, [claimed, residual],
                            pdf_text_provider=lambda _p: "")

    assert r.doi_examined == 2      # metadata rung saw both
    assert r.doi_text_examined == 1  # printed rung saw only the residual


# ── the memo ──────────────────────────────────────────────────────────────


def test_the_doi_memo_avoids_a_second_read(tmp_path: Path, monkeypatch) -> None:
    """The memo is load-bearing: cold, the metadata rung costs ~52 s a scan."""
    s = _settings(tmp_path)
    monkeypatch.setattr(doi_cache, "cache_path", lambda: tmp_path / "doi_cache.json")
    pdf = _pdf(s, "smith_2020.pdf", doi="10.1000/real")
    entries = [_entry("k", "Smith, Jane", "2020", "10.1000/real")]

    reads = {"n": 0}
    real = pdf_identifiers.extract_embedded_doi

    def counting(p: Path) -> str | None:
        reads["n"] += 1
        return real(p)

    monkeypatch.setattr(pdf_identifiers, "extract_embedded_doi", counting)

    assert bridge.build_bridge(s, entries, [pdf]).source_per_pdf[pdf] == "doi"
    assert reads["n"] == 1
    # Second scan, same bytes: served from the memo, no PDF opened.
    assert bridge.build_bridge(s, entries, [pdf]).source_per_pdf[pdf] == "doi"
    assert reads["n"] == 1


def test_the_memo_is_invalidated_when_the_pdf_changes(
    tmp_path: Path, monkeypatch
) -> None:
    s = _settings(tmp_path)
    monkeypatch.setattr(doi_cache, "cache_path", lambda: tmp_path / "doi_cache.json")
    pdf = _pdf(s, "smith_2020.pdf", doi="10.1000/first")
    entries = [
        _entry("first", "A, A", "1900", "10.1000/first"),
        _entry("second", "B, B", "1900", "10.1000/second"),
    ]
    assert bridge.build_bridge(s, entries, [pdf]).pdf_to_bib[pdf] == "first"

    _pdf(s, "smith_2020.pdf", doi="10.1000/second")  # rewritten in place
    assert bridge.build_bridge(s, entries, [pdf]).pdf_to_bib[pdf] == "second"


# ── control characters inside extracted identifiers (2026-08-19) ──────────
#
# PDF text extraction leaves control/format characters INSIDE captured
# identifiers — measured live: BMJ page-1 DOIs came out of the text layer as
# ``10.1136/bmj.l4378\x08`` (a literal backspace; not covered by ``\s``, so
# the regex keeps it), and the poisoned join key silently missed the bib on
# every scan while printing identically in any log. Found because the
# accession Phase 2 measurement extracted the same DOIs cleanly and disagreed
# with the memo. Both halves are pinned: canonicalisation strips the
# characters, and the JOIN re-normalises so memo entries written before the
# fix heal instead of requiring a cache flush.


def test_normalise_doi_strips_control_and_format_characters() -> None:
    from audit.core.bib import normalise_doi

    assert normalise_doi("10.1136/bmj.l4378\x08") == "10.1136/bmj.l4378"
    assert normalise_doi("10.1136/bmj​.l4378") == "10.1136/bmj.l4378"
    assert normalise_doi("\x08") is None


def test_a_printed_doi_with_a_trailing_control_char_still_joins(
    tmp_path: Path, monkeypatch
) -> None:
    s = _settings(tmp_path)
    monkeypatch.setattr(doi_cache, "cache_path", lambda: tmp_path / "doi_cache.json")
    pdf = _pdf(s, "dwan_2010.pdf")   # no embedded DOI, year disagrees w/ bib
    entries = [_entry("dwan2019", "Dwan, Kerry", "2019", "10.1136/bmj.l4378")]

    r = bridge.build_bridge(
        s, entries, [pdf],
        pdf_text_provider=lambda p: "doi: 10.1136/bmj.l4378\x08 rest of page")

    assert r.pdf_to_bib.get(pdf) == "dwan2019"
    assert r.source_per_pdf[pdf] == "doi_text"


def test_a_poisoned_memo_entry_written_before_the_fix_heals_at_the_join(
    tmp_path: Path, monkeypatch
) -> None:
    """The memo hands back the stored value verbatim — the join must
    re-normalise rather than miss forever (3 live entries were doing so)."""
    s = _settings(tmp_path)
    monkeypatch.setattr(doi_cache, "cache_path", lambda: tmp_path / "doi_cache.json")
    pdf = _pdf(s, "dwan_2010.pdf")
    cache = doi_cache.load_cache()
    doi_cache.store(pdf, cache, "meta", None)
    doi_cache.store(pdf, cache, "text", "10.1136/bmj.l4378\x08")
    doi_cache.save_cache(cache)
    entries = [_entry("dwan2019", "Dwan, Kerry", "2019", "10.1136/bmj.l4378")]

    r = bridge.build_bridge(
        s, entries, [pdf],
        pdf_text_provider=lambda p: (_ for _ in ()).throw(
            AssertionError("memo hit must not re-extract")))

    assert r.pdf_to_bib.get(pdf) == "dwan2019"
    assert r.source_per_pdf[pdf] == "doi_text"
