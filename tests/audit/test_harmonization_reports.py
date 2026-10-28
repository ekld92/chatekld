"""The three 2026-09-06 harmonization reports (read-only).

* ``multi_pdf`` — one citation key owning ≥2 PDFs, with the rung that claimed
  each file; mixed provenance first.
* ``not_in_zotero`` — the curated ``no_match`` bucket, which no other report
  lists.
* ``dead_curation`` — mapping.json entries pointing at a missing file or a
  dead citation key.

Each is pinned engine → serializer → search corpus, hermetically.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from audit import search, serialize
from audit.config import Settings
from audit.engine.bridge import BridgeResult
from audit.engine.inventory import Inventory, Record
from audit.engine.reports import dead_curation, multi_pdf, not_in_zotero


def _rec(key, pdfs=(), bib=True, zot=False, title="T", author="Smith"):
    be = SimpleNamespace(title=title, year="2020", authors=[author]) if bib else None
    zi = SimpleNamespace(item_type="journalArticle", child_notes=[]) if zot else None
    return SimpleNamespace(citation_key=key, bib_entry=be, zotero_item=zi,
                           pdf_paths=[Path(p) for p in pdfs], obsidian_note=None)


def _inv(records, bridge=None):
    return Inventory(
        records={r.citation_key: r for r in records},
        bridge=bridge or BridgeResult(),
        pdfs_skipped=[], zotero_matched_by_title=0,
    )


# --- multi_pdf ---------------------------------------------------------------

def test_multi_pdf_lists_only_records_with_two_or_more_files():
    br = BridgeResult(source_per_pdf={
        Path("/v/a_1.pdf"): "doi", Path("/v/a_2.pdf"): "authoryear",
        Path("/v/b.pdf"): "doi",
    })
    inv = _inv([_rec("a2020", ["/v/a_1.pdf", "/v/a_2.pdf"]),
                _rec("b2020", ["/v/b.pdf"])], br)
    rep = multi_pdf.find(inv)
    assert [r.citation_key for r in rep.rows] == ["a2020"]
    assert rep.file_count == 2


def test_multi_pdf_flags_mixed_provenance_and_sorts_it_first():
    """A DOI-verified file beside a surname+year one is the absorbed-sibling
    shape — the review priority — so it must come first and be marked."""
    br = BridgeResult(source_per_pdf={
        Path("/v/x_1.pdf"): "authoryear", Path("/v/x_2.pdf"): "authoryear",
        Path("/v/y_1.pdf"): "doi", Path("/v/y_2.pdf"): "authoryear",
        Path("/v/z_1.pdf"): "manual", Path("/v/z_2.pdf"): "manual",
    })
    inv = _inv([_rec("x2020", ["/v/x_1.pdf", "/v/x_2.pdf"]),
                _rec("y2020", ["/v/y_1.pdf", "/v/y_2.pdf"]),
                _rec("z2020", ["/v/z_1.pdf", "/v/z_2.pdf"])], br)
    rep = multi_pdf.find(inv)
    assert rep.rows[0].citation_key == "y2020" and rep.rows[0].mixed
    by = {r.citation_key: r for r in rep.rows}
    assert by["x2020"].all_filename and not by["x2020"].mixed
    assert not by["z2020"].all_filename and not by["z2020"].mixed
    assert rep.mixed_count == 1 and rep.all_filename_count == 1


def test_multi_pdf_dismissal_needs_the_memo_and_is_never_guessed():
    """``dismissed`` mirrors the Possible Duplicates decision for the SAME
    group id; without a memoised sha it is None, not False."""
    br = BridgeResult(source_per_pdf={Path("/v/a_1.pdf"): "doi",
                                      Path("/v/a_2.pdf"): "authoryear"})
    inv = _inv([_rec("a2020", ["/v/a_1.pdf", "/v/a_2.pdf"])], br)
    shas = {Path("/v/a_1.pdf"): "aa", Path("/v/a_2.pdf"): "bb"}
    rep = multi_pdf.find(inv, dismissed={"multi_pdf:aa+bb"}, sha_for=shas.get)
    assert rep.rows[0].dismissed is True and rep.dismissed_count == 1
    rep = multi_pdf.find(inv, dismissed=set(), sha_for=shas.get)
    assert rep.rows[0].dismissed is False
    rep = multi_pdf.find(inv, dismissed={"multi_pdf:aa+bb"}, sha_for=lambda p: None)
    assert rep.rows[0].dismissed is None
    rep = multi_pdf.find(inv)
    assert rep.rows[0].dismissed is None


def test_multi_pdf_serializer_carries_per_file_source(tmp_path):
    br = BridgeResult(source_per_pdf={tmp_path / "a_1.pdf": "doi",
                                      tmp_path / "a_2.pdf": "authoryear"})
    inv = _inv([_rec("a2020", [tmp_path / "a_1.pdf", tmp_path / "a_2.pdf"])], br)
    out = serialize.report_multi_pdf(multi_pdf.find(inv), Settings(vault_root=tmp_path))
    assert out["count"] == 1 and out["mixed_count"] == 1
    files = {f["name"]: f["source"] for f in out["rows"][0]["files"]}
    assert files == {"a_1.pdf": "doi", "a_2.pdf": "authoryear"}
    assert out["rows"][0]["files"][0]["rel"] == "a_1.pdf"
    json.dumps(out)


# --- not_in_zotero -----------------------------------------------------------

def test_not_in_zotero_lists_the_curated_bucket_and_counts_missing_files(tmp_path):
    present = tmp_path / "keep.pdf"
    present.write_bytes(b"%PDF")
    gone = tmp_path / "gone.pdf"
    br = BridgeResult(confirmed_no_match_pdfs=[present, gone, present])
    inv = _inv([], br)
    rep = not_in_zotero.find(inv, Settings(vault_root=tmp_path))
    assert [r.pdf.name for r in rep.rows] == ["gone.pdf", "keep.pdf"]
    assert rep.missing_count == 1
    assert {r.state for r in rep.rows} == {"no_match"}


def test_not_in_zotero_serializer_joins_the_injected_resolution(tmp_path):
    """The accession join is injected (the vocab_id_for pattern) so the
    vendored tree never imports the resolver; a missing file is not looked
    up; counts derive from the rows."""
    a = tmp_path / "a.pdf"
    a.write_bytes(b"%PDF")
    b = tmp_path / "b.pdf"
    b.write_bytes(b"%PDF")
    gone = tmp_path / "gone.pdf"
    br = BridgeResult(confirmed_no_match_pdfs=[a, b, gone])
    rep = not_in_zotero.find(_inv([], br), Settings(vault_root=tmp_path))
    asked = []

    def resolution_for(p):
        asked.append(p.name)
        if p.name == "a.pdf":
            return {"outcome": "candidates", "tier": "printed_doi",
                    "doi": None, "candidates": [{"doi": "10.1/x"}]}
        return None

    out = serialize.report_not_in_zotero(
        rep, Settings(vault_root=tmp_path), resolution_for=resolution_for)
    assert sorted(asked) == ["a.pdf", "b.pdf"]  # never the missing one
    assert out["count"] == 3 and out["missing_count"] == 1
    assert out["resolved_count"] == 1 and out["with_doi_count"] == 1
    rows = {r["pdf"]["name"]: r for r in out["rows"]}
    assert rows["a.pdf"]["resolution"]["candidates"][0]["doi"] == "10.1/x"
    assert rows["b.pdf"]["resolution"] is None
    assert rows["gone.pdf"]["exists"] is False


def test_not_in_zotero_serializer_without_the_join_is_plain(tmp_path):
    a = tmp_path / "a.pdf"
    a.write_bytes(b"%PDF")
    rep = not_in_zotero.find(_inv([], BridgeResult(confirmed_no_match_pdfs=[a])),
                             Settings(vault_root=tmp_path))
    out = serialize.report_not_in_zotero(rep, Settings(vault_root=tmp_path))
    assert out["resolved_count"] == 0 and out["rows"][0]["resolution"] is None


# --- dead_curation -----------------------------------------------------------

def _write_mapping(path, matches, no_match, pending=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"matches": matches, "no_match": no_match,
                                "pending": list(pending)}), encoding="utf-8")


def test_dead_curation_finds_the_three_shapes(tmp_path):
    vault = tmp_path / "vault"
    (vault / "b").mkdir(parents=True)
    (vault / "b" / "alive.pdf").write_bytes(b"%PDF")
    (vault / "b" / "deadkey.pdf").write_bytes(b"%PDF")
    (vault / "b" / "nm_alive.pdf").write_bytes(b"%PDF")
    mapping = tmp_path / "audit" / "mapping.json"
    _write_mapping(mapping,
                   {"alive2020": ["b/alive.pdf", "b/gone.pdf"],
                    "deadkey2020": ["b/deadkey.pdf"]},
                   ["b/nm_alive.pdf", "b/nm_gone.pdf"])
    settings = Settings(vault_root=vault, mapping_file_override=mapping)
    br = BridgeResult(source_per_pdf={vault / "b" / "alive.pdf": "manual",
                                      vault / "b" / "other.pdf": "manual_hash"},
                      mapping_orphans=2)
    inv = _inv([_rec("alive2020", [vault / "b" / "alive.pdf"]),
                _rec("deadkey2020", [vault / "b" / "deadkey.pdf"], bib=False)], br)
    rep = dead_curation.find(inv, settings)
    kinds = sorted((r.kind, r.rel) for r in rep.rows)
    assert kinds == [
        ("match_dead_key", "b/deadkey.pdf"),
        ("match_missing_file", "b/gone.pdf"),
        ("no_match_missing_file", "b/nm_gone.pdf"),
    ]
    assert rep.mapping_orphans == 2 and rep.recovered_count == 1
    assert rep.curated_matches == 3 and rep.curated_no_match == 2


def test_dead_curation_covers_the_pending_bucket(tmp_path):
    """A curated "add to Zotero later" whose file is gone.

    The ``pending`` bucket arrived with the curated-state split (2026-09-06)
    and this report was written the same day without it. Measured on the live
    library 2026-09-17: ``mapping_orphans`` counted 23 stale curated entries
    while the report — the only thing that LISTS them — showed 21, and the
    two invisible rows (``she_2005.pdf``, renamed and recovered by content
    hash, and ``vinatier_2_2024.pdf``, deleted) blocked Phase 2's "pending
    ends empty" exit criterion. The Phase 2 proposer cannot see them either:
    the bridge only serves a pending file that exists.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "here.pdf").write_bytes(b"%PDF")
    mapping = tmp_path / "audit" / "mapping.json"
    _write_mapping(mapping, {}, [], pending=["here.pdf", "gone.pdf"])
    settings = Settings(vault_root=vault, mapping_file_override=mapping)
    rep = dead_curation.find(inv := _inv([]), settings)
    assert [(r.kind, r.rel) for r in rep.rows] == [("pending_missing_file", "gone.pdf")]
    assert rep.curated_pending == 2
    out = serialize.report_dead_curation(rep, settings)
    assert out["by_kind"] == {"pending_missing_file": 1}
    assert out["curated_pending"] == 2
    # And it is searchable, like every other row of every other report.
    corpus = search.build_corpus(inv, settings)
    groups = {g["report"]: g for g in search.search(corpus, "gone")["groups"]}
    assert "dead_curation" in groups


def test_dead_curation_key_alive_via_zotero_alone_is_not_dead(tmp_path):
    """A key with a Zotero item but no bib entry (stale export) is alive."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "z.pdf").write_bytes(b"%PDF")
    mapping = tmp_path / "audit" / "mapping.json"
    _write_mapping(mapping, {"zot2020": ["z.pdf"]}, [])
    inv = _inv([_rec("zot2020", [vault / "z.pdf"], bib=False, zot=True)])
    rep = dead_curation.find(inv, Settings(vault_root=vault, mapping_file_override=mapping))
    assert rep.rows == []


def test_dead_curation_serializer_and_missing_mapping_file(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = Settings(vault_root=vault, mapping_file_override=tmp_path / "audit" / "mapping.json")
    out = serialize.report_dead_curation(dead_curation.find(_inv([]), settings), settings)
    assert out == {"rows": [], "count": 0, "by_kind": {}, "mapping_orphans": 0,
                   "recovered_count": 0, "curated_matches": 0, "curated_no_match": 0,
                   "curated_pending": 0}


# --- search corpus -----------------------------------------------------------

def test_the_three_reports_are_declared_and_searchable(tmp_path):
    for name in ("multi_pdf", "not_in_zotero", "dead_curation"):
        assert name in search.REPORT_ORDER
    vault = tmp_path / "vault"
    vault.mkdir()
    a1 = vault / "quetiapine_1_2020.pdf"
    a1.write_bytes(b"%PDF")
    a2 = vault / "quetiapine_2_2020.pdf"
    a2.write_bytes(b"%PDF")
    nm = vault / "olanzapine_2019.pdf"
    nm.write_bytes(b"%PDF")
    mapping = tmp_path / "audit" / "mapping.json"
    _write_mapping(mapping, {"dead2020": ["clozapine_2018.pdf"]}, [])
    br = BridgeResult(source_per_pdf={a1: "doi", a2: "authoryear"},
                      confirmed_no_match_pdfs=[nm])
    # The corpus also serialises the inventory rows, which read the full
    # Record/BibEntry shape — so this one test uses the real dataclass.
    rec = Record(citation_key="quet2020", pdf_paths=[a1, a2])
    rec.bib_entry = SimpleNamespace(
        citation_key="quet2020", title="Quetiapine", year="2020",
        authors=["Smith, J"], entry_type="article", keywords=set(), doi=None)
    inv = _inv([rec], br)
    settings = Settings(vault_root=vault, mapping_file_override=mapping)
    corpus = search.build_corpus(inv, settings)
    reports = {e.report for e in corpus}
    assert {"multi_pdf", "not_in_zotero", "dead_curation"} <= reports
    for q, rep, key in (("quetiapine", "multi_pdf", "quet2020"),
                        ("olanzapine", "not_in_zotero", None),
                        ("clozapine", "dead_curation", "dead2020")):
        groups = {g["report"]: g for g in search.search(corpus, q)["groups"]}
        assert rep in groups, (q, groups.keys())
        assert groups[rep]["rows"][0]["citation_key"] == key
