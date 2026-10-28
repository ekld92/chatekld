"""mapping.json's second curated state, the detach exclusion, and the rule
that no writer may drop keys it did not carry (2026-09-06, Phase 2 of the
library-harmonization plan).

* ``pending`` = "add to Zotero later" — matched by the ladder like
  ``no_match``, listed as its own state, hash-anchored, mutually exclusive
  with ``no_match``.
* ``exclude[rel] = [key, …]`` — the ``detach`` verb: an automatic rung may
  never claim that PDF for that key again; a hand link to the same key lifts
  it; ``clear`` removes it.
* Every writer loads :func:`_load_mapping_all` and saves every field, so a
  save by one verb cannot erase another verb's decisions.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit.config import Settings
from audit.core.bib import BibEntry
from audit.engine import bridge


def _read(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "biblio").mkdir(parents=True)
    return v


def _pdf(vault, name):
    p = vault / "biblio" / name
    p.write_bytes(b"%PDF-1.4\n" + name.encode())
    return p


# --- the pending state --------------------------------------------------------

def test_pending_is_its_own_bucket_and_excludes_no_match(vault, tmp_path):
    mapping = tmp_path / "mapping.json"
    a = _pdf(vault, "a_2020.pdf")
    bridge.add_no_match(mapping, a, vault)
    bridge.add_pending(mapping, [a], vault)
    d = _read(mapping)
    assert d["pending"] == ["biblio/a_2020.pdf"]
    assert d["no_match"] == []
    assert "biblio/a_2020.pdf" in d["hashes"]           # anchored like no_match
    bridge.add_no_match(mapping, a, vault)
    d = _read(mapping)
    assert d["no_match"] == ["biblio/a_2020.pdf"]
    assert "pending" not in d                           # omitted when empty


def test_pending_drops_a_prior_match_and_a_match_drops_pending(vault, tmp_path):
    mapping = tmp_path / "mapping.json"
    a = _pdf(vault, "a_2020.pdf")
    bridge.add_match(mapping, a, "a2020", vault)
    bridge.add_pending(mapping, [a], vault)
    d = _read(mapping)
    assert d["matches"] == {} and d["pending"] == ["biblio/a_2020.pdf"]
    bridge.add_match(mapping, a, "a2020", vault)
    d = _read(mapping)
    assert d["matches"] == {"a2020": ["biblio/a_2020.pdf"]}
    assert "pending" not in d


def test_set_curated_state_rejects_an_unknown_state(vault, tmp_path):
    with pytest.raises(ValueError):
        bridge.set_curated_state(tmp_path / "m.json", [], vault, "maybe")


def test_the_ladder_treats_pending_as_matched_and_lists_it(vault, tmp_path):
    mapping = tmp_path / "mapping.json"
    a = _pdf(vault, "smith_2020.pdf")
    bridge.add_pending(mapping, [a], vault)
    settings = Settings(vault_root=vault, biblio_articles_subdir="biblio",
                        mapping_file_override=mapping)
    entries = [BibEntry(citation_key="smith2020", entry_type="article",
                        year="2020", authors=["Smith, J"])]
    res = bridge.build_bridge(settings, entries, [a])
    assert res.pending_pdfs == [a]
    assert res.unmapped_pdfs == [] and res.pdf_to_bib == {}


def test_a_renamed_pending_pdf_is_recovered_by_hash(vault, tmp_path):
    mapping = tmp_path / "mapping.json"
    a = _pdf(vault, "smith_2020.pdf")
    bridge.add_pending(mapping, [a], vault)
    moved = vault / "biblio" / "renamed_2020.pdf"
    a.rename(moved)
    settings = Settings(vault_root=vault, biblio_articles_subdir="biblio",
                        mapping_file_override=mapping)
    res = bridge.build_bridge(settings, [], [moved])
    assert res.pending_pdfs == [moved]
    assert res.mapping_orphans == 1


# --- detach -----------------------------------------------------------------

def _settings(vault, mapping):
    return Settings(vault_root=vault, biblio_articles_subdir="biblio",
                    mapping_file_override=mapping)


def test_detach_stops_the_authoryear_rung_reclaiming_the_file(vault, tmp_path):
    """The absorbed-sibling case: li_2_2024.pdf lands on the one Li 2024 entry
    by surname+year. Detaching sends it to the orphan queue and keeps the
    genuine file on the key."""
    mapping = tmp_path / "mapping.json"
    a1 = _pdf(vault, "li_1_2024.pdf")
    a2 = _pdf(vault, "li_2_2024.pdf")
    entries = [BibEntry(citation_key="li2024", entry_type="article",
                        year="2024", authors=["Li, X"])]
    res = bridge.build_bridge(_settings(vault, mapping), entries, [a1, a2])
    assert sorted(res.bib_to_pdfs["li2024"]) == [a1, a2]

    assert bridge.detach(mapping, a2, "li2024", vault) is True
    assert _read(mapping)["exclude"] == {"biblio/li_2_2024.pdf": ["li2024"]}
    assert bridge.detach(mapping, a2, "li2024", vault) is False   # idempotent

    res = bridge.build_bridge(_settings(vault, mapping), entries, [a1, a2])
    assert res.bib_to_pdfs["li2024"] == [a1]
    assert res.unmapped_pdfs == [a2]
    assert res.exclusions_applied == 1


def test_detach_removes_a_curated_match_to_that_key_only(vault, tmp_path):
    mapping = tmp_path / "mapping.json"
    a = _pdf(vault, "a_2020.pdf")
    b = _pdf(vault, "b_2020.pdf")
    bridge.add_match(mapping, a, "k2020", vault)
    bridge.add_match(mapping, b, "k2020", vault)
    bridge.detach(mapping, a, "k2020", vault)
    d = _read(mapping)
    assert d["matches"] == {"k2020": ["biblio/b_2020.pdf"]}
    assert d["exclude"] == {"biblio/a_2020.pdf": ["k2020"]}


def test_a_hand_link_to_the_detached_key_lifts_only_that_exclusion(vault, tmp_path):
    mapping = tmp_path / "mapping.json"
    a = _pdf(vault, "a_2020.pdf")
    bridge.detach(mapping, a, "k2020", vault)
    bridge.detach(mapping, a, "other2020", vault)
    bridge.add_match(mapping, a, "k2020", vault)
    d = _read(mapping)
    assert d["matches"] == {"k2020": ["biblio/a_2020.pdf"]}
    assert d["exclude"] == {"biblio/a_2020.pdf": ["other2020"]}


# --- detach_all (the wildcard, 2026-09-07) ----------------------------------


def test_detach_all_stops_every_rung_not_just_the_one_that_matched(
        vault, tmp_path, monkeypatch):
    """Per-key exclusion is whack-a-mole: the ladder finds the NEXT candidate,
    so a file belonging to no entry had to be detached once per reachable key.
    Measured live, 2 of 127 exclusions had already needed a second round
    (`gomez_penedo_2026.pdf`, excluded from two unrelated keys).

    The shape, faithful to the real one: a DOI in the PDF claims one entry,
    and the filename's surname+year reaches a different entry one rung lower.
    Detaching the winner just hands the file to the runner-up."""
    mapping = tmp_path / "mapping.json"
    a = _pdf(vault, "zed_2021.pdf")
    entries = [BibEntry(citation_key="x2021", entry_type="article", year="2021",
                        authors=["Other, P"], doi="10.1/x"),
               BibEntry(citation_key="zed2021", entry_type="article", year="2021",
                        authors=["Zed, Q"])]
    monkeypatch.setattr(bridge.core_pdf_ids, "pdf_doi", lambda *a, **k: "10.1/x")
    monkeypatch.setattr(bridge.core_doi_cache, "load_cache", lambda: {})
    monkeypatch.setattr(bridge.core_doi_cache, "save_cache", lambda m: None)

    res = bridge.build_bridge(_settings(vault, mapping), entries, [a])
    assert res.pdf_to_bib == {a: "x2021"}

    bridge.detach(mapping, a, "x2021", vault)
    res = bridge.build_bridge(_settings(vault, mapping), entries, [a])
    assert res.pdf_to_bib == {a: "zed2021"}, "the runner-up rung took it"

    assert bridge.detach(mapping, a, bridge.DETACH_ALL, vault) is True
    res = bridge.build_bridge(_settings(vault, mapping), entries, [a])
    assert res.pdf_to_bib == {}
    assert res.unmapped_pdfs == [a]


def test_detach_all_sweeps_curated_matches_too(vault, tmp_path):
    """"Maps to nothing" cannot coexist with a stored match: leaving one would
    keep the PDF mapped by the manual rung while claiming nothing may map it."""
    mapping = tmp_path / "mapping.json"
    a = _pdf(vault, "a_2020.pdf")
    b = _pdf(vault, "b_2020.pdf")
    bridge.add_match(mapping, a, "k2020", vault)
    bridge.add_match(mapping, b, "k2020", vault)
    bridge.detach(mapping, a, bridge.DETACH_ALL, vault)
    d = _read(mapping)
    assert d["matches"] == {"k2020": ["biblio/b_2020.pdf"]}          # b untouched
    assert d["exclude"] == {"biblio/a_2020.pdf": [bridge.DETACH_ALL]}


def test_a_hand_link_beats_the_wildcard(vault, tmp_path):
    """An explicit link is a newer decision than a blanket refusal. Without
    this, add_matches would write a match that _link then refuses, so the link
    would silently not take at the next scan — a button that does nothing."""
    mapping = tmp_path / "mapping.json"
    a = _pdf(vault, "a_2020.pdf")
    bridge.detach(mapping, a, bridge.DETACH_ALL, vault)
    bridge.add_match(mapping, a, "k2020", vault)
    assert _read(mapping).get("exclude", {}) == {}
    entries = [BibEntry(citation_key="k2020", entry_type="article",
                        year="2020", authors=["Aa, B"])]
    res = bridge.build_bridge(_settings(vault, mapping), entries, [a])
    assert res.pdf_to_bib == {a: "k2020"}


def test_link_would_lift_exclude_predicate():
    """Shared gate for the mapping route's accept_excluded refusal."""
    assert bridge.link_would_lift_exclude(None, "k") is False
    assert bridge.link_would_lift_exclude([], "k") is False
    assert bridge.link_would_lift_exclude(["other"], "k") is False
    assert bridge.link_would_lift_exclude(["k"], "k") is True
    assert bridge.link_would_lift_exclude([bridge.DETACH_ALL], "k") is True
    assert bridge.link_would_lift_exclude(
        ["other", bridge.DETACH_ALL], "k") is True


def test_clear_undoes_the_wildcard(vault, tmp_path):
    mapping = tmp_path / "mapping.json"
    a = _pdf(vault, "li_2026.pdf")
    bridge.detach(mapping, a, bridge.DETACH_ALL, vault)
    assert bridge.clear_curation(mapping, a, vault) is True
    assert _read(mapping).get("exclude", {}) == {}
    entries = [BibEntry(citation_key="li2026", entry_type="article",
                        year="2026", authors=["Li, X"])]
    res = bridge.build_bridge(_settings(vault, mapping), entries, [a])
    assert res.pdf_to_bib == {a: "li2026"}


def test_the_doi_rung_honours_an_exclusion_and_falls_through(vault, tmp_path, monkeypatch):
    """_claim_by_doi returns _link's verdict, so an excluded DOI claim does
    not count as linked and the file reaches the orphan queue."""
    mapping = tmp_path / "mapping.json"
    a = _pdf(vault, "x_2021.pdf")
    bridge.detach(mapping, a, "x2021", vault)
    entries = [BibEntry(citation_key="x2021", entry_type="article", year="2021",
                        authors=["Zed, Q"], doi="10.1/x")]
    monkeypatch.setattr(bridge.core_pdf_ids, "pdf_doi",
                        lambda *a, **k: "10.1/x")
    monkeypatch.setattr(bridge.core_doi_cache, "load_cache", lambda: {})
    monkeypatch.setattr(bridge.core_doi_cache, "save_cache", lambda m: None)
    res = bridge.build_bridge(_settings(vault, mapping), entries, [a])
    assert res.unmapped_pdfs == [a]
    assert res.pdf_to_bib == {}


# --- clear + preservation ---------------------------------------------------

def test_clear_curations_lifts_pending_and_exclusions_in_one_save(vault, tmp_path):
    mapping = tmp_path / "mapping.json"
    a = _pdf(vault, "a_2020.pdf")
    b = _pdf(vault, "b_2020.pdf")
    c = _pdf(vault, "c_2020.pdf")
    bridge.add_pending(mapping, [a], vault)
    bridge.detach(mapping, b, "k2020", vault)
    assert bridge.clear_curations(mapping, [a, b, c], vault) == 2
    d = _read(mapping)
    assert "pending" not in d and "exclude" not in d
    assert d["no_match"] == [] and d["matches"] == {}
    assert bridge.clear_curation(mapping, a, vault) is False


def test_every_writer_preserves_the_keys_it_did_not_touch(vault, tmp_path):
    """The reason writers load _load_mapping_all: a save that carried only
    matches/no_match would erase pending + exclude on the way through."""
    mapping = tmp_path / "mapping.json"
    a, b, c, d_ = (_pdf(vault, f"{n}_2020.pdf") for n in "abcd")
    bridge.add_pending(mapping, [a], vault)
    bridge.detach(mapping, b, "k2020", vault)

    def snapshot():
        d = _read(mapping)
        return d.get("pending"), d.get("exclude")

    base = snapshot()
    bridge.add_match(mapping, c, "c2020", vault)
    assert snapshot() == base
    bridge.add_no_match(mapping, d_, vault)
    assert snapshot() == base
    bridge.refresh_mapping_hashes(mapping, vault)
    assert snapshot() == base
    bridge.clear_curation(mapping, c, vault)
    assert snapshot() == base


def test_loader_reads_no_match_over_a_conflicting_hand_edit(tmp_path):
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps({
        "matches": {}, "no_match": ["x.pdf"], "pending": ["x.pdf", "y.pdf"],
        "exclude": {"z.pdf": ["k", "k", 3], "w.pdf": []},
    }), encoding="utf-8")
    m = bridge._load_mapping_all(mapping)
    assert m.pending == {"y.pdf"}
    assert m.exclude == {"z.pdf": ["k"]}
    # The 4-tuple reader is unchanged for read-only consumers.
    assert bridge._load_mapping_full(mapping)[1] == {"x.pdf"}


def test_legacy_file_without_the_new_keys_stays_byte_identical(vault, tmp_path):
    mapping = tmp_path / "mapping.json"
    a = _pdf(vault, "a_2020.pdf")
    bridge.add_match(mapping, a, "a2020", vault)
    before = mapping.read_bytes()
    bridge.refresh_mapping_hashes(mapping, vault)
    assert mapping.read_bytes() == before
    assert "pending" not in _read(mapping) and "exclude" not in _read(mapping)


def test_not_in_zotero_report_lists_pending_with_its_state(vault, tmp_path):
    from audit.engine.bridge import BridgeResult
    from audit.engine.inventory import Inventory
    from audit.engine.reports import not_in_zotero
    from audit import serialize
    a = _pdf(vault, "a_2020.pdf")
    b = _pdf(vault, "b_2020.pdf")
    inv = Inventory(records={}, bridge=BridgeResult(
        confirmed_no_match_pdfs=[a], pending_pdfs=[b]),
        pdfs_skipped=[], zotero_matched_by_title=0)
    rep = not_in_zotero.find(inv, Settings(vault_root=vault))
    assert {(r.pdf.name, r.state) for r in rep.rows} == {
        ("a_2020.pdf", "no_match"), ("b_2020.pdf", "pending")}
    out = serialize.report_not_in_zotero(rep, Settings(vault_root=vault))
    assert out["pending_count"] == 1 and out["no_match_count"] == 1
    assert SimpleNamespace(**out).count == 2
