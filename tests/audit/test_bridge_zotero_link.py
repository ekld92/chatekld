"""The Zotero-link rung (2026-09-19): the file Zotero links under an item is
the bridge's first rung, and it beats the hand curation of mapping.json.

The maintainer's three decisions, each pinned here:

* **Zotero wins, reported.** A PDF linked by exactly one Zotero item whose key
  is in the bib belongs to that item, whatever mapping.json says — another
  key, "not for Zotero", "pending", or a detach exclusion — and every such
  disagreement is recorded (``zotero_overrides``) and listed by the
  ``dead_curation`` report as ``overridden_by_zotero``.
* **The bridge never writes mapping.json.** Stale entries are proposed for
  clearing by the routine (cleanup phase 03a), never pruned by a scan.
* A PDF two items link, or whose item Better BibTeX has not exported yet, is
  NOT claimed: the rest of the ladder decides, as before.

Measured before building it: on the live library every one of 6,248 PDFs was
linked by exactly one item and none disagreed with the ladder, so the rung
changes no pairing today.
"""

from __future__ import annotations

import json
import unicodedata
from types import SimpleNamespace

import pytest

from audit import serialize
from audit.config import Settings
from audit.core.bib import BibEntry
from audit.engine import bridge
from audit.engine.bridge import BridgeResult
from audit.engine.inventory import Inventory, zotero_link_index
from audit.engine.reports import dead_curation


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "biblio").mkdir(parents=True)
    return v


def _pdf(vault, name):
    p = vault / "biblio" / name
    p.write_bytes(b"%PDF-1.4\n" + name.encode())
    return p


def _settings(vault, mapping):
    return Settings(vault_root=vault, biblio_articles_subdir="biblio",
                    mapping_file_override=mapping)


def _entry(key, author="Smith, J", year="2020"):
    return BibEntry(citation_key=key, entry_type="article", year=year,
                    authors=[author])


# --- the rung ------------------------------------------------------------------

def test_a_zotero_link_pairs_a_file_no_other_rung_could(vault, tmp_path):
    a = _pdf(vault, "unrelated_name.pdf")
    res = bridge.build_bridge(_settings(vault, tmp_path / "m.json"),
                              [_entry("smith2020")], [a],
                              zotero_links={a: ["smith2020"]})
    assert res.pdf_to_bib == {a: "smith2020"}
    assert res.source_per_pdf[a] == "zotero_link"
    assert res.zotero_linked == 1 and res.unmapped_pdfs == []


def test_without_links_the_ladder_is_unchanged(vault, tmp_path):
    a = _pdf(vault, "unrelated_name.pdf")
    res = bridge.build_bridge(_settings(vault, tmp_path / "m.json"),
                              [_entry("smith2020")], [a])
    assert res.unmapped_pdfs == [a] and res.zotero_linked == 0


def test_the_zotero_link_beats_author_and_year(vault, tmp_path):
    """smith_2020.pdf would go to smith2020 by name; Zotero says jones2020."""
    a = _pdf(vault, "smith_2020.pdf")
    res = bridge.build_bridge(_settings(vault, tmp_path / "m.json"),
                              [_entry("smith2020"), _entry("jones2020", "Jones, K")],
                              [a], zotero_links={a: ["jones2020"]})
    assert res.pdf_to_bib == {a: "jones2020"}
    assert res.zotero_overrides == {}      # no hand decision was overridden


def test_it_beats_a_hand_pairing_with_another_key_and_reports_it(vault, tmp_path):
    mapping = tmp_path / "m.json"
    a = _pdf(vault, "smith_2020.pdf")
    bridge.add_match(mapping, a, "smith2020", vault)
    before = mapping.read_bytes()
    res = bridge.build_bridge(_settings(vault, mapping),
                              [_entry("smith2020"), _entry("jones2020", "Jones, K")],
                              [a], zotero_links={a: ["jones2020"]})
    assert res.pdf_to_bib == {a: "jones2020"}
    assert res.source_per_pdf[a] == "zotero_link"
    assert res.zotero_overrides == {a: {"zotero_key": "jones2020", "curation": "match",
                                        "curated_key": "smith2020"}}
    assert mapping.read_bytes() == before  # the scan never writes the curation


def test_a_hand_pairing_that_agrees_is_not_an_override(vault, tmp_path):
    mapping = tmp_path / "m.json"
    a = _pdf(vault, "smith_2020.pdf")
    bridge.add_match(mapping, a, "smith2020", vault)
    res = bridge.build_bridge(_settings(vault, mapping), [_entry("smith2020")], [a],
                              zotero_links={a: ["smith2020"]})
    assert res.pdf_to_bib == {a: "smith2020"} and res.zotero_overrides == {}


@pytest.mark.parametrize("curation", ["no_match", "pending"])
def test_it_beats_a_curated_bucket_and_the_file_leaves_it(vault, tmp_path, curation):
    mapping = tmp_path / "m.json"
    a = _pdf(vault, "smith_2020.pdf")
    if curation == "no_match":
        bridge.add_no_match(mapping, a, vault)
    else:
        bridge.add_pending(mapping, [a], vault)
    res = bridge.build_bridge(_settings(vault, mapping), [_entry("smith2020")], [a],
                              zotero_links={a: ["smith2020"]})
    assert res.pdf_to_bib == {a: "smith2020"}
    assert res.confirmed_no_match_pdfs == [] and res.pending_pdfs == []
    assert res.zotero_overrides[a]["curation"] == curation


def test_it_beats_a_detach_exclusion(vault, tmp_path):
    mapping = tmp_path / "m.json"
    a = _pdf(vault, "smith_2020.pdf")
    bridge.detach(mapping, a, "smith2020", vault)
    res = bridge.build_bridge(_settings(vault, mapping), [_entry("smith2020")], [a],
                              zotero_links={a: ["smith2020"]})
    assert res.pdf_to_bib == {a: "smith2020"}
    assert res.zotero_overrides[a]["curation"] == "exclude"


def test_a_file_two_items_link_is_left_to_the_ladder(vault, tmp_path):
    a = _pdf(vault, "smith_2020.pdf")
    res = bridge.build_bridge(_settings(vault, tmp_path / "m.json"),
                              [_entry("smith2020"), _entry("jones2020", "Jones, K")],
                              [a], zotero_links={a: ["jones2020", "smith2020"]})
    assert res.source_per_pdf[a] == "authoryear"
    assert res.zotero_multi_links == {a: ["jones2020", "smith2020"]}


def test_an_item_better_bibtex_has_not_exported_is_left_to_the_ladder(vault, tmp_path):
    a = _pdf(vault, "smith_2020.pdf")
    res = bridge.build_bridge(_settings(vault, tmp_path / "m.json"),
                              [_entry("smith2020")], [a],
                              zotero_links={a: ["brandnew2026"]})
    assert res.source_per_pdf[a] == "authoryear"
    assert res.zotero_unexported == {a: "brandnew2026"}


def test_a_file_renamed_in_zotero_stays_paired(vault, tmp_path):
    """Zotero renamed the linked file: the hand decision's path is gone, the
    link follows the new name, and the stale entry is reported, not pruned."""
    mapping = tmp_path / "m.json"
    a = _pdf(vault, "smith_2020.pdf")
    bridge.add_match(mapping, a, "smith2020", vault)
    moved = a.with_name("smith_1_2020.pdf")
    a.rename(moved)
    res = bridge.build_bridge(_settings(vault, mapping), [_entry("smith2020")], [moved],
                              zotero_links={moved: ["smith2020"]})
    assert res.pdf_to_bib == {moved: "smith2020"}
    assert res.source_per_pdf[moved] == "zotero_link"   # not manual_hash
    assert "biblio/smith_2020.pdf" in json.loads(mapping.read_text())["matches"]["smith2020"]


# --- the index -----------------------------------------------------------------

def _item(key, *atts):
    return SimpleNamespace(citation_key=key, attachments=list(atts))


def _att(path, mode="linked_file"):
    return SimpleNamespace(raw_path=str(path), link_mode=mode)


def test_the_index_matches_a_path_spelled_differently(vault):
    a = _pdf(vault, "café_2020.pdf")                    # NFC on disk
    nfd = unicodedata.normalize("NFD", str(a)).upper().replace(".PDF", ".pdf")
    links = zotero_link_index([_item("cafe2020", _att(nfd))], [a])
    assert links == {a: ["cafe2020"]}


def test_the_index_keeps_linked_files_even_without_a_citation_key(vault):
    a, b, c, d = (_pdf(vault, n) for n in ("a.pdf", "b.pdf", "c.pdf", "d.pdf"))
    items = [
        _item("ka", _att(a)),
        _item("kb", _att(b, mode="imported_file")),            # Zotero's copy
        _item("kc", _att("attachments:biblio/c.pdf")),         # unset base dir
        _item(None, _att(d)),                                  # no citation key
        _item("kx", _att(vault / "biblio" / "absent.pdf")),    # not a vault PDF
    ]
    assert zotero_link_index(items, [a, b, c, d]) == {a: ["ka"], d: [""]}


def test_the_index_lists_every_item_linking_a_file(vault):
    a = _pdf(vault, "a.pdf")
    links = zotero_link_index([_item("k1", _att(a)), _item("k2", _att(a))], [a])
    assert links == {a: ["k1", "k2"]}


# --- the dead_curation report --------------------------------------------------

def _inv(records, br):
    return Inventory(records={r.citation_key: r for r in records}, bridge=br,
                     pdfs_skipped=[], zotero_matched_by_title=0)


def _rec(key, bib=True):
    return SimpleNamespace(citation_key=key, pdf_paths=[], obsidian_note=None,
                           bib_entry=SimpleNamespace(title="T") if bib else None,
                           zotero_item=None)


def test_an_overridden_decision_is_listed_once_with_the_zotero_key(vault, tmp_path):
    """A citation-key change: the hand pairing names the OLD key, which is now
    dead, and Zotero links the file under the NEW key. One row, the override —
    not also a match_dead_key row."""
    mapping = tmp_path / "m.json"
    a = _pdf(vault, "smith_2020.pdf")
    bridge.add_match(mapping, a, "smithOld2020", vault)
    settings = _settings(vault, mapping)
    br = bridge.build_bridge(settings, [_entry("smithNew2020")], [a],
                             zotero_links={a: ["smithNew2020"]})
    rep = dead_curation.find(_inv([_rec("smithOld2020", bib=False),
                                   _rec("smithNew2020")], br), settings)
    assert [(r.kind, r.rel, r.citation_key, r.zotero_key, r.curation) for r in rep.rows] \
        == [("overridden_by_zotero", "biblio/smith_2020.pdf", "smithOld2020",
             "smithNew2020", "match")]
    row = serialize.report_dead_curation(rep, settings)["rows"][0]
    assert row["zotero_key"] == "smithNew2020" and row["curation"] == "match"


def test_without_overrides_the_report_is_unchanged(vault, tmp_path):
    mapping = tmp_path / "m.json"
    a = _pdf(vault, "smith_2020.pdf")
    bridge.add_match(mapping, a, "smith2020", vault)
    rep = dead_curation.find(_inv([_rec("smith2020")], BridgeResult()),
                             _settings(vault, mapping))
    assert rep.rows == []


def test_build_inventory_pairs_through_the_zotero_link(tmp_path, monkeypatch):
    """The scan itself, not a pre-built index: read_items → zotero_link_index
    → build_bridge. A file whose name matches no bib entry still joins when
    Zotero links it (2026-09-19 review D)."""
    from audit.core import zotero as zot
    from audit.engine import inventory as eng_inventory

    vault = tmp_path / "vault"
    biblio = vault / "Z_attachments" / "biblio_articles"
    biblio.mkdir(parents=True)
    pdf = biblio / "unrelated_name.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    (vault / "_master.bib").write_text(
        "@article{smith2020, title={X}, year={2020}, author={Smith, J}}",
        encoding="utf-8")
    db = vault / "zotero.sqlite"
    db.write_bytes(b"")
    stored = unicodedata.normalize("NFD", str(pdf.resolve())).upper().replace(
        ".PDF", ".pdf")
    att = zot.ZoteroAttachment(
        item_key="ATT1", parent_key="ITEM1", filename="unrelated_name.pdf",
        storage_path=None, is_stored=False, content_type="application/pdf",
        link_mode="linked_file", raw_path=stored)
    item = zot.ZoteroItem(
        item_key="ITEM1", item_type="journalArticle", title="X",
        citation_key="smith2020", attachments=[att])
    monkeypatch.setattr(eng_inventory.zotero, "read_items",
                        lambda *_a, **_k: [item])
    settings = Settings(vault_root=vault.resolve(), zotero_sqlite=db)
    inv = eng_inventory.build_inventory(settings, count_annotations=False)
    paired = {p.name: k for p, k in inv.bridge.pdf_to_bib.items()}
    assert paired == {"unrelated_name.pdf": "smith2020"}
    src = {p.name: s for p, s in inv.bridge.source_per_pdf.items()}
    assert src["unrelated_name.pdf"] == "zotero_link"


# --- a Zotero-linked PDF must never read as "not in Zotero" ---------------
#
# The rung leaves an unexported key and a two-parent file on `unmapped_pdfs`,
# because the rest of the ladder still gets to pair them. Right for pairing,
# wrong for every surface whose claim is "no Zotero entry": before 2026-09-20
# both reports listed such a file, and their rows carry the [identify] and
# [create in Zotero…] verbs the routes then refused with a 404.

def _unclaimed_inv(pdf, *, unexported=None, multi=None, no_match=(), pending=()):
    br = BridgeResult(unmapped_pdfs=[pdf],
                      confirmed_no_match_pdfs=list(no_match),
                      pending_pdfs=list(pending))
    if unexported:
        br.zotero_unexported = {pdf: unexported}
    if multi:
        br.zotero_multi_links = {pdf: list(multi)}
    return Inventory(records={}, bridge=br, pdfs_skipped=[],
                     zotero_matched_by_title=0)


@pytest.mark.parametrize("kw", [{"unexported": "smith2020"},
                                {"multi": ["a2020", "b2021"]}])
def test_unmapped_report_excludes_and_discloses_a_zotero_linked_pdf(vault, tmp_path, kw):
    from audit.engine.reports import read_unzoterod

    pdf = _pdf(vault, "held_2020.pdf")
    rep = read_unzoterod.find(_unclaimed_inv(pdf, **kw), _settings(vault, tmp_path / "m.json"),
                              annotations={})
    assert rep.rows == []
    assert rep.zotero_unclaimed_count == 1


@pytest.mark.parametrize("bucket", ["no_match", "pending"])
def test_not_in_zotero_excludes_and_discloses_a_zotero_linked_pdf(vault, tmp_path, bucket):
    from audit.engine.reports import not_in_zotero

    pdf = _pdf(vault, "held_2020.pdf")
    inv = _unclaimed_inv(pdf, unexported="smith2020", **{f"{bucket}": [pdf]})
    rep = not_in_zotero.find(inv, _settings(vault, tmp_path / "m.json"))
    assert rep.rows == []
    assert rep.zotero_unclaimed_count == 1
    # Serialized too, or the meta line cannot say what it left out.
    assert serialize.report_not_in_zotero(
        rep, _settings(vault, tmp_path / "m.json"))["zotero_unclaimed_count"] == 1


def test_the_unread_subset_excludes_exactly_what_its_superset_does(vault, tmp_path):
    """`unread_unzoterod` is documented as a strict SUBSET of
    `read_unzoterod`. Filtering one and not the other makes the two tabs
    disagree about what "without Zotero" means — the mislabelling that already
    cost this pair once (2026-07-31)."""
    from audit.engine.reports import read_unzoterod, unread_unzoterod

    pdf = _pdf(vault, "held_2020.pdf")
    inv = _unclaimed_inv(pdf, unexported="smith2020")
    settings = _settings(vault, tmp_path / "m.json")
    sup = read_unzoterod.find(inv, settings, annotations={})
    sub = unread_unzoterod.find(inv, settings, annotations={})
    assert {r.pdf for r in sub.rows} <= {r.pdf for r in sup.rows}
    assert sub.rows == []
    assert sub.zotero_unclaimed_count == 1


def test_a_plain_unmapped_pdf_is_still_listed(vault, tmp_path):
    """The exclusion is narrow: only a file Zotero actually links."""
    from audit.engine.reports import read_unzoterod

    pdf = _pdf(vault, "orphan_2020.pdf")
    rep = read_unzoterod.find(_unclaimed_inv(pdf), _settings(vault, tmp_path / "m.json"),
                              annotations={})
    assert [r.pdf for r in rep.rows] == [pdf]
    assert rep.zotero_unclaimed_count == 0


def test_casefold_collision_keeps_unknown_link_and_exact_path_wins(vault, tmp_path):
    # Path objects model a case-sensitive filesystem even on a folding host.
    a, b = vault / 'biblio' / 'Smith_2020.pdf', vault / 'biblio' / 'smith_2020.pdf'
    links = zotero_link_index([_item('k1', _att(a))], [a, b])
    assert links == {a: ['k1']} and not links.ambiguous_paths
    links = zotero_link_index([_item('k1', _att(vault / 'biblio' / 'SMITH_2020.PDF'))], [a, b])
    assert links == {} and links.ambiguous_paths == {a, b}
    settings = _settings(vault, tmp_path / 'mapping.json')
    result = bridge.build_bridge(settings, [_entry('k1')], [a, b], zotero_links=links)
    assert not result.pdf_to_bib
    assert bridge.zotero_unclaimed(result, a) and bridge.zotero_unclaimed(result, b)
