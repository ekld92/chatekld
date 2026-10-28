"""Plan 2026-08-05/5 coverage: the near-duplicate candidate engine, the
dismissal store, and the serializer. Hermetic — the "PDFs" are byte files and
the text provider is a dict lookup."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from audit import serialize
from audit.engine import dup_decisions, near_duplicates
from audit.engine.duplicates import DuplicateSet
from audit.engine.inventory import Record
from audit.engine.near_duplicates import (
    NearDupReport,
    extract_identifier,
    find_near_duplicates,
    text_key,
)


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        biblio_articles_dir=tmp_path / "biblio",
        ignored_dirs=frozenset({".trash"}),
        vault_root=tmp_path,
    )


def _pdf(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / "biblio" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


_PAGE = (
    "Effects of sleep deprivation on mood regulation in adolescents a "
    "randomized controlled trial of cognitive behavioural therapy versus "
    "waitlist control with placebo comparison and outcomes assessed at "
    "twelve months follow up"
)


# --- key helpers -------------------------------------------------------------

def test_text_key_cancels_watermark_digits_and_case():
    a = _PAGE + " Downloaded from journals.org on 12/03/2026 by 192.168.0.4"
    b = _PAGE.upper() + " downloaded from JOURNALS.ORG on 01/07/2025 by 10.0.0.9"
    assert text_key(a) == text_key(b)
    assert text_key("too short") is None


def test_extract_identifier_doi_arxiv_and_version_folding():
    assert extract_identifier("see DOI: 10.1000/j.jad.2020.01.001. More") == \
        "doi:10.1000/j.jad.2020.01.001"
    assert extract_identifier("arXiv:2107.06499v2 [cs.CL]") == "arxiv:2107.06499"
    assert extract_identifier("arXiv: 2107.06499 [cs.CL]") == "arxiv:2107.06499"
    # Nearest-the-top wins (a cited DOI later must not shadow the header id).
    both = "arXiv:2107.06499v1 ... later cites doi 10.9999/other.1"
    assert extract_identifier(both) == "arxiv:2107.06499"
    assert extract_identifier("no ids here") is None


# --- file tiers --------------------------------------------------------------

def test_page1_text_tier_groups_byte_different_rewatermarked_copies(tmp_path):
    s = _settings(tmp_path)
    a = _pdf(tmp_path, "a.pdf", b"bytes-A")
    b = _pdf(tmp_path, "b.pdf", b"bytes-B-different")
    texts = {
        a: _PAGE + " Downloaded on 12/03/2026 by 1.2.3.4",
        b: _PAGE + " Downloaded on 05/05/2025 by 5.6.7.8",
    }
    rep = find_near_duplicates(
        s, None, [], pdf_text_provider=lambda p: texts.get(p))
    (c,) = [c for c in rep.candidates if c.kind == "page1_text"]
    assert sorted(p.name for p in c.paths) == ["a.pdf", "b.pdf"]


def test_identifier_tier_pairs_preprint_and_published(tmp_path):
    s = _settings(tmp_path)
    a = _pdf(tmp_path, "preprint.pdf", b"v1-bytes")
    b = _pdf(tmp_path, "published.pdf", b"v2-bytes")
    filler = " ".join(["different"] * 25)
    texts = {
        a: f"arXiv:2107.06499v1 {filler} alpha",
        b: f"arXiv:2107.06499v2 {_PAGE}",
    }
    rep = find_near_duplicates(
        s, None, [], pdf_text_provider=lambda p: texts.get(p))
    (c,) = [c for c in rep.candidates if c.kind == "identifier"]
    assert "arxiv:2107.06499" in c.evidence


def test_byte_identical_groups_are_left_to_the_exact_report(tmp_path):
    s = _settings(tmp_path)
    a = _pdf(tmp_path, "a.pdf", b"same-bytes")
    b = _pdf(tmp_path, "b.pdf", b"same-bytes")
    texts = {a: _PAGE, b: _PAGE}
    rep = find_near_duplicates(
        s, None, [DuplicateSet(content_hash="h", paths=[a, b], size_bytes=10)],
        pdf_text_provider=lambda p: texts.get(p))
    assert rep.candidates == []


def test_no_text_pdfs_are_counted_not_silent(tmp_path):
    s = _settings(tmp_path)
    _pdf(tmp_path, "scan.pdf", b"scanned")
    _pdf(tmp_path, "thin.pdf", b"thin")
    texts = {tmp_path / "biblio" / "thin.pdf": "just a cover"}
    rep = find_near_duplicates(
        s, None, [], pdf_text_provider=lambda p: texts.get(p))
    assert rep.no_text_count == 2  # one unreadable + one too-thin
    # The count alone could not tell a reviewer WHICH files were never
    # compared (annual cleanup A6 blind spot, 2026-09-17).
    assert sorted(p.name for p in rep.no_text_paths) == ["scan.pdf", "thin.pdf"]


def test_no_provider_skips_text_tiers_entirely(tmp_path):
    s = _settings(tmp_path)
    _pdf(tmp_path, "a.pdf", b"x")
    rep = find_near_duplicates(s, None, [], pdf_text_provider=None)
    assert rep.candidates == [] and rep.no_text_count == 0


# --- record tiers ------------------------------------------------------------

def _rec(key, title=None, year=None, authors=(), doi=None, pdfs=()):
    rec = Record(citation_key=key)
    rec.bib_entry = SimpleNamespace(
        title=title, year=year, authors=list(authors), doi=doi)
    rec.pdf_paths = list(pdfs)
    return rec


def _inv(*records):
    return SimpleNamespace(records={r.citation_key: r for r in records})


def test_bib_doi_and_confirmed_title_tiers():
    inv = _inv(
        _rec("smith2020", title="A Great Paper", year="2020",
             authors=["Smith, Anna"], doi="10.1/x"),
        _rec("smith2020b", title="A Great Paper", year="2021",
             authors=["Smith, Anna"], doi="10.1/x"),
        _rec("other2019", title="Unrelated", year="2019",
             authors=["Jones, Bob"]),
    )
    rep = find_near_duplicates(
        SimpleNamespace(biblio_articles_dir=Path("/nonexistent"),
                        ignored_dirs=frozenset()),
        inv, [], pdf_text_provider=None)
    kinds = {c.kind: c for c in rep.candidates}
    assert sorted(kinds["bib_doi"].citation_keys) == ["smith2020", "smith2020b"]
    assert sorted(kinds["record_title"].citation_keys) == [
        "smith2020", "smith2020b"]  # year ±1 + same author → confirmed


def test_unconfirmed_title_collision_is_title_only():
    inv = _inv(
        _rec("a2010", title="Sleep and Mood", year="2010", authors=["Aa, X"]),
        _rec("b2020", title="Sleep and Mood", year="2020", authors=["Bb, Y"]),
    )
    rep = find_near_duplicates(
        SimpleNamespace(biblio_articles_dir=Path("/nonexistent"),
                        ignored_dirs=frozenset()),
        inv, [], pdf_text_provider=None)
    (c,) = rep.candidates
    assert c.kind == "title_only"
    assert "NOT confirmed" in c.evidence


def test_multi_pdf_tier_requires_distinct_bytes(tmp_path):
    same1 = _pdf(tmp_path, "k1.pdf", b"identical")
    same2 = _pdf(tmp_path, "k2.pdf", b"identical")
    diff1 = _pdf(tmp_path, "d1.pdf", b"one")
    diff2 = _pdf(tmp_path, "d2.pdf", b"two-different")
    inv = _inv(
        _rec("identical2020", pdfs=[same1, same2]),
        _rec("distinct2021", pdfs=[diff1, diff2]),
    )
    rep = find_near_duplicates(
        SimpleNamespace(biblio_articles_dir=Path("/nonexistent"),
                        ignored_dirs=frozenset()),
        inv, [], pdf_text_provider=None)
    multi = [c for c in rep.candidates if c.kind == "multi_pdf"]
    assert len(multi) == 1
    assert "distinct2021" in multi[0].evidence


def test_key_prefix_neutralises_cachefirst_vs_page1_asymmetry(tmp_path):
    """2026-08-05 verification finding: the provider is cache-first, so an
    indexed PDF yields whole-document text while a fresh copy yields page-1
    only — the key must reduce both to the same first-_KEY_TOKENS prefix."""
    import itertools
    words = ["".join(c) for c in itertools.product("abcdefghij", repeat=3)]
    page1 = " ".join(words[:200])                    # the fresh page-1 side
    cached = " ".join(words[:200] + words[200:500])  # cached whole-doc side
    s = _settings(tmp_path)
    a = _pdf(tmp_path, "indexed.pdf", b"cached-copy-bytes")
    b = _pdf(tmp_path, "fresh.pdf", b"fresh-copy-bytes")
    texts = {a: cached, b: page1}
    rep = find_near_duplicates(
        s, None, [], pdf_text_provider=lambda p: texts.get(p))
    kinds = [c.kind for c in rep.candidates]
    assert "page1_text" in kinds
    assert text_key(cached) == text_key(page1)


# --- dismissals --------------------------------------------------------------

def test_dismissed_group_is_filtered_and_counted(tmp_path):
    inv = _inv(
        _rec("a2020", title="Same Title", year="2020", authors=["Aa, X"]),
        _rec("b2020", title="Same Title", year="2020", authors=["Aa, X"]),
    )
    s = SimpleNamespace(biblio_articles_dir=Path("/nonexistent"),
                        ignored_dirs=frozenset())
    rep = find_near_duplicates(s, inv, [], pdf_text_provider=None)
    (c,) = rep.candidates
    rep2 = find_near_duplicates(
        s, inv, [], pdf_text_provider=None, dismissed={c.pair_id})
    assert rep2.candidates == [] and rep2.dismissed_count == 1
    # A dismissal can only be re-verified if the dismissed group is visible.
    (hidden,) = rep2.dismissed_candidates
    assert hidden.pair_id == c.pair_id and hidden.citation_keys == c.citation_keys


def test_dismissed_file_group_keeps_its_paths(tmp_path):
    s = _settings(tmp_path)
    a = _pdf(tmp_path, "a.pdf", b"A-bytes")
    b = _pdf(tmp_path, "b.pdf", b"B-bytes")
    texts = {a: _PAGE, b: _PAGE}
    rep = find_near_duplicates(s, None, [], pdf_text_provider=lambda p: texts.get(p))
    (c,) = rep.candidates
    rep2 = find_near_duplicates(s, None, [], pdf_text_provider=lambda p: texts.get(p),
                                dismissed={c.pair_id})
    (hidden,) = rep2.dismissed_candidates
    assert rep2.candidates == [] and sorted(hidden.paths) == sorted([a, b])


def test_dup_decisions_roundtrip_add_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(dup_decisions, "decisions_path",
                        lambda: tmp_path / "dup_decisions.json")
    assert dup_decisions.load_dismissed() == set()
    assert dup_decisions.add_dismissed("k:a+b") is True
    assert dup_decisions.add_dismissed("k:a+b") is False  # idempotent
    assert dup_decisions.load_dismissed() == {"k:a+b"}
    assert dup_decisions.clear_dismissed("k:a+b") is True
    assert dup_decisions.clear_dismissed("k:a+b") is False
    assert dup_decisions.load_dismissed() == set()


def test_dup_decisions_corrupt_file_refuses_rmw_never_clobbers(tmp_path, monkeypatch):
    """2026-08-05 verification finding: a corrupt (unreadable-as-JSON) file
    must make the WRITE verbs raise — starting the RMW from empty would
    clobber every prior unregenerable decision. The scan-time read still
    degrades to empty (a corrupt file must not fail a scan)."""
    import pytest
    target = tmp_path / "dup_decisions.json"
    monkeypatch.setattr(dup_decisions, "decisions_path", lambda: target)
    target.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(ValueError):
        dup_decisions.add_dismissed("k:a+b")
    with pytest.raises(ValueError):
        dup_decisions.clear_dismissed("k:a+b")
    assert target.read_text(encoding="utf-8") == "{corrupt"  # untouched
    assert dup_decisions.load_dismissed() == set()  # scan filter degrades


# --- cancel + serializer -----------------------------------------------------

# --- plan 6: content-similarity tier ----------------------------------------

_ALT_PAGE = (
    "Prevalence of insomnia among nurses working rotating shifts a cross "
    "sectional survey of hospital staff sleep quality daytime somnolence and "
    "burnout across three university hospitals in france"
)


def test_similarity_sweep_pairs_same_authoryear_block_above_cutoff(tmp_path):
    s = _settings(tmp_path)
    # Same author+year filename block, byte-different, near-identical page-1
    # text (digit noise only) → content_similarity candidate with a score.
    a = _pdf(tmp_path, "smith_2020.pdf", b"origin-A")
    b = _pdf(tmp_path, "smith_2020_1.pdf", b"origin-B")
    c = _pdf(tmp_path, "jones_2019.pdf", b"origin-C")
    texts = {
        a: _PAGE + " Journal of Affective Disorders volume 12",
        b: _PAGE + " ResearchGate preprint copy uploaded by the author",
        c: _ALT_PAGE,
    }
    rep = find_near_duplicates(
        s, None, [], pdf_text_provider=lambda p: texts.get(p))
    sims = [x for x in rep.candidates if x.kind == "content_similarity"]
    assert len(sims) == 1
    assert sorted(p.name for p in sims[0].paths) == [
        "smith_2020.pdf", "smith_2020_1.pdf"]
    assert sims[0].similarity is not None and sims[0].similarity >= 88
    assert rep.similarity_available is True
    assert rep.compared_pairs >= 1


def test_similarity_sweep_never_compares_across_blocks(tmp_path):
    s = _settings(tmp_path)
    # Identical text but DIFFERENT author-year blocks: the sweep must not
    # compare them (blocking bound) — but the page1_text tier still catches
    # the identical-text pair, proving the tiers complement each other.
    a = _pdf(tmp_path, "smith_2020.pdf", b"A")
    b = _pdf(tmp_path, "jones_2019.pdf", b"B")
    texts = {a: _PAGE, b: _PAGE}
    rep = find_near_duplicates(
        s, None, [], pdf_text_provider=lambda p: texts.get(p))
    assert [c.kind for c in rep.candidates] == ["page1_text"]
    # The identical pair got its annotation score (100), but no
    # content_similarity row was created for a cross-block pair.
    assert rep.candidates[0].similarity == 100


def test_unrelated_texts_in_one_block_stay_below_cutoff(tmp_path):
    s = _settings(tmp_path)
    a = _pdf(tmp_path, "smith_2020.pdf", b"A")
    b = _pdf(tmp_path, "smith_2020_b.pdf", b"B")
    texts = {a: _PAGE, b: _ALT_PAGE}
    rep = find_near_duplicates(
        s, None, [], pdf_text_provider=lambda p: texts.get(p))
    assert [c for c in rep.candidates if c.kind == "content_similarity"] == []


def test_missing_rapidfuzz_degrades_with_disclosure(tmp_path, monkeypatch):
    monkeypatch.setattr(near_duplicates, "_load_fuzz", lambda: None)
    s = _settings(tmp_path)
    a = _pdf(tmp_path, "a.pdf", b"bytes-A")
    b = _pdf(tmp_path, "b.pdf", b"bytes-B")
    texts = {a: _PAGE, b: _PAGE}
    rep = find_near_duplicates(
        s, None, [], pdf_text_provider=lambda p: texts.get(p))
    # Deterministic tiers still run; similarity is honestly reported absent.
    assert [c.kind for c in rep.candidates] == ["page1_text"]
    assert rep.candidates[0].similarity is None
    assert rep.similarity_available is False


def test_cancel_stops_the_text_walk(tmp_path):
    s = _settings(tmp_path)
    for i in range(5):
        _pdf(tmp_path, f"f{i}.pdf", f"bytes-{i}".encode())
    calls = []

    def provider(p):
        calls.append(p)
        return None

    rep = find_near_duplicates(
        s, None, [], pdf_text_provider=provider, cancel_fn=lambda: True)
    assert calls == [] and rep.candidates == []


def test_serializer_shape_and_json(tmp_path):
    a = _pdf(tmp_path, "a.pdf", b"A")
    c = near_duplicates.NearDupCandidate(
        kind="page1_text", evidence="why", pair_id="page1_text:x+y",
        paths=[a])
    rep = NearDupReport(candidates=[c], no_text_count=3, dismissed_count=1)
    payload = serialize.report_near_duplicates(
        rep, SimpleNamespace(vault_root=tmp_path))
    (row,) = payload["rows"]
    assert row["kind"] == "page1_text" and row["pair_id"] == "page1_text:x+y"
    assert payload["no_text_count"] == 3 and payload["dismissed_count"] == 1
    assert payload["dismissed_rows"] == [] and payload["no_text_paths"] == []
    rep.dismissed_candidates = [c]
    rep.no_text_paths = [a]
    payload = serialize.report_near_duplicates(rep, SimpleNamespace(vault_root=tmp_path))
    assert [r["pair_id"] for r in payload["dismissed_rows"]] == ["page1_text:x+y"]
    assert [p["name"] for p in payload["no_text_paths"]] == ["a.pdf"]
    json.dumps(payload)
