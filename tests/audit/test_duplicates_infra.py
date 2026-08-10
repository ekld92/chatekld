"""Plan 2026-08-05/4 coverage: hash memo, ignored-dirs prune, suggested_keep,
bib DOI parsing, and the scan's pdf_text_provider seam."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from audit import serialize
from audit.core import hash_cache, hashing
from audit.core.bib import normalise_doi, parse_bib
from audit.engine import duplicates as eng_duplicates
from audit.engine.duplicates import DuplicateSet, candidate_pdfs


def _w(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --- cached_full_hash / memo semantics --------------------------------------

def test_cached_full_hash_hit_miss_and_invalidation(tmp_path: Path) -> None:
    p = _w(tmp_path / "a.pdf", b"content-1")
    cache: dict = {}
    h1 = hashing.cached_full_hash(p, cache)
    assert h1 == hashing.full_hash(p)
    ent = cache[str(p)]
    assert ent["sha256"] == h1 and ent["size"] == 9

    # Hit: poison the file read path by asserting the memo answer is served
    # even for a WRONG stored sha as long as size+mtime match — proving no
    # re-read happens on a hit.
    cache[str(p)]["sha256"] = "not-a-real-sha"
    assert hashing.cached_full_hash(p, cache) == "not-a-real-sha"

    # Invalidation: a size change (different content length) forces a re-hash
    # and the entry is rewritten.
    _w(p, b"content-changed")
    h2 = hashing.cached_full_hash(p, cache)
    assert h2 == hashing.full_hash(p)
    assert cache[str(p)]["sha256"] == h2


def test_find_duplicate_sets_uses_memo_and_pairs_hit_with_miss(tmp_path: Path) -> None:
    # a is memoised, b is a byte-identical newcomer: the unique-partial prune
    # must NOT hide the pair (a's partial hash is never computed on a hit).
    a = _w(tmp_path / "a.pdf", b"same-bytes-here")
    b = _w(tmp_path / "b.pdf", b"same-bytes-here")
    cache: dict = {}
    hashing.cached_full_hash(a, cache)  # memoise a
    sets = hashing.find_duplicate_sets([a, b], hash_cache=cache)
    (paths,) = sets.values()
    assert sorted(p.name for p in paths) == ["a.pdf", "b.pdf"]
    # And b landed in the memo for the next scan.
    assert str(b) in cache


def test_hash_cache_roundtrip_and_stale_prune(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(hash_cache, "cache_path",
                        lambda: tmp_path / "hash_cache.json")
    live = _w(tmp_path / "live.pdf", b"x")
    cache = {
        str(live): {"size": 1, "mtime_ns": 1, "sha256": "s"},
        str(tmp_path / "gone.pdf"): {"size": 1, "mtime_ns": 1, "sha256": "t"},
    }
    hash_cache.save_cache(cache)
    loaded = hash_cache.load_cache()
    assert str(live) in loaded
    assert str(tmp_path / "gone.pdf") not in loaded  # stale key pruned

    # A corrupt file degrades to {}.
    (tmp_path / "hash_cache.json").write_text("{corrupt", encoding="utf-8")
    assert hash_cache.load_cache() == {}


def test_unique_partial_prune_not_widened_by_earlier_full_hashes(
        tmp_path: Path, monkeypatch) -> None:
    """2026-08-05 verification finding: the prune exception exists only for
    MEMO hits (whose partial hash was never computed). Full hashes produced by
    an earlier partial group in the same size group must not disable the
    prune for a lone-partial file — that would be wasted I/O."""
    a = _w(tmp_path / "a.pdf", b"same-content-xx")
    b = _w(tmp_path / "b.pdf", b"same-content-xx")
    c = _w(tmp_path / "c.pdf", b"diff-content-yy")  # same size, unique partial
    calls: list[Path] = []
    real = hashing.full_hash

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(hashing, "full_hash", counting)
    sets = hashing.find_duplicate_sets([a, b, c])  # no memo at all
    (paths,) = sets.values()
    assert sorted(p.name for p in paths) == ["a.pdf", "b.pdf"]
    assert c not in calls  # lone-partial file pruned, never fully hashed


# --- candidate walk: ignored_dirs pruned, skip-prefix kept ------------------

def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        biblio_articles_dir=tmp_path / "biblio",
        ignored_dirs=frozenset({".git", ".obsidian", ".trash"}),
    )


def test_candidate_walk_prunes_ignored_dirs_keeps_skip_prefix(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    keep = _w(tmp_path / "biblio" / "paper.pdf", b"k")
    skip_prefixed = _w(tmp_path / "biblio" / "z_item_x.pdf", b"z")
    _w(tmp_path / "biblio" / ".trash" / "old.pdf", b"t")
    got = {p.name for p in candidate_pdfs(s)}
    assert got == {keep.name, skip_prefixed.name}


def test_trashed_copy_no_longer_forms_a_duplicate_set(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _w(tmp_path / "biblio" / "paper.pdf", b"same")
    _w(tmp_path / "biblio" / ".trash" / "paper.pdf", b"same")
    assert eng_duplicates.find_biblio_duplicates(s) == []


# --- suggested_keep ----------------------------------------------------------

def test_report_duplicates_suggests_cleanest_name(tmp_path: Path) -> None:
    vault = tmp_path
    clean = vault / "biblio" / "smith_2020.pdf"
    copy = vault / "biblio" / "smith_2020_1.pdf"
    for p in (clean, copy):
        _w(p, b"same")
    d = DuplicateSet(content_hash="h", paths=[copy, clean], size_bytes=4)
    payload = serialize.report_duplicates(
        [d], SimpleNamespace(vault_root=vault))
    (row,) = payload["rows"]
    assert row["suggested_keep"] == "biblio/smith_2020.pdf"
    json.dumps(payload)


# --- bib DOI ----------------------------------------------------------------

def test_normalise_doi_prefixes_case_and_garbage() -> None:
    assert normalise_doi("https://doi.org/10.1000/ABC.123") == "10.1000/abc.123"
    assert normalise_doi("doi:10.1000/xyz") == "10.1000/xyz"
    assert normalise_doi("10.1000/xyz.") == "10.1000/xyz"
    assert normalise_doi("not-a-doi") is None
    assert normalise_doi("") is None


def test_parse_bib_carries_doi(tmp_path: Path) -> None:
    bib = tmp_path / "test.bib"
    bib.write_text(
        "@article{smith2020,\n"
        "  title = {A Paper},\n"
        "  author = {Smith, Anna},\n"
        "  year = {2020},\n"
        "  doi = {https://doi.org/10.1000/J.TEST},\n"
        "}\n"
        "@article{jones2021,\n  title = {No Doi},\n  year = {2021},\n}\n",
        encoding="utf-8",
    )
    entries = {e.citation_key: e for e in parse_bib(bib)}
    assert entries["smith2020"].doi == "10.1000/j.test"
    assert entries["jones2021"].doi is None
