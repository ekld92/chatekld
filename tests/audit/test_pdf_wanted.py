"""Batch B (2026-08-26): "this entry needs no local PDF" decisions.

The MIRROR of ``mapping.json``'s ``no_match`` list. Without it the
*Bib Entries Without PDFs* report could only grow — 1,502 rows on the
maintainer's library, 293 of them item types that never have a PDF.

The discipline under test is ``dup_decisions.py``'s, and it matters for the
same reason: these are hand-made, unregenerable decisions, so a corrupt file
must make the write REFUSE rather than restart the list from empty and clobber
it on the next save.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from audit.engine import pdf_wanted
from audit.engine.reports import zotero_no_pdf


@pytest.fixture()
def store(tmp_path):
    path = tmp_path / "pdf_wanted.json"
    with mock.patch.object(pdf_wanted, "decisions_path", return_value=path):
        yield path


# --- the sidecar -----------------------------------------------------------

def test_dismiss_and_clear_round_trip(store):
    assert pdf_wanted.add_dismissed(["a2020", "b2021"]) == 2
    assert pdf_wanted.load_dismissed() == {"a2020", "b2021"}
    assert pdf_wanted.clear_dismissed(["a2020"]) == 1
    assert pdf_wanted.load_dismissed() == {"b2021"}


def test_dismiss_is_idempotent_and_reports_only_what_changed(store):
    pdf_wanted.add_dismissed(["a2020"])
    assert pdf_wanted.add_dismissed(["a2020"]) == 0
    assert pdf_wanted.add_dismissed(["a2020", "c2022"]) == 1


def test_a_bulk_dismissal_is_one_atomic_write(store):
    """A partial bulk write would leave the user unable to tell which half
    landed, which is why the API takes a list rather than being called N
    times by the client."""
    writes = []
    real_save = pdf_wanted._save

    def _counting_save(keys):
        writes.append(len(keys))
        real_save(keys)

    with mock.patch.object(pdf_wanted, "_save", _counting_save):
        assert pdf_wanted.add_dismissed([f"k{i}" for i in range(40)]) == 40
    assert writes == [40]


def test_a_missing_file_is_an_empty_set_not_an_error(store):
    assert pdf_wanted.load_dismissed() == set()
    assert pdf_wanted.add_dismissed(["a2020"]) == 1


def test_a_corrupt_file_refuses_the_write_rather_than_clobbering_it(store):
    """The dup_decisions lesson: starting an RMW from empty over a merely
    unreadable file destroys every prior hand-made decision on the next save."""
    store.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        pdf_wanted.add_dismissed(["a2020"])
    with pytest.raises(ValueError):
        pdf_wanted.clear_dismissed(["a2020"])
    # And the file is untouched.
    assert store.read_text(encoding="utf-8") == "{not json"


def test_a_corrupt_file_degrades_to_empty_for_the_REPORT_filter(store):
    """Opposite policy on the read path, deliberately: a corrupt sidecar must
    not fail the report — the dismissed rows merely reappear."""
    store.write_text("{not json", encoding="utf-8")
    assert pdf_wanted.load_dismissed() == set()


def test_blank_and_non_string_keys_are_ignored(store):
    assert pdf_wanted.add_dismissed(["", "  ", None, 7, "real2020"]) == 1
    assert pdf_wanted.load_dismissed() == {"real2020"}


# --- the report ------------------------------------------------------------

def _rec(key, *, pdfs=(), item_type=None, year="2020"):
    bib = SimpleNamespace(title=f"Title {key}", year=year, authors=["Smith, A"])
    item = SimpleNamespace(item_type=item_type) if item_type else None
    return SimpleNamespace(citation_key=key, bib_entry=bib, pdf_paths=list(pdfs),
                           zotero_item=item)


def _inv(records):
    return SimpleNamespace(records={r.citation_key: r for r in records})


def test_report_hides_dismissed_rows_but_discloses_how_many():
    """Never a silently shorter list — the Tag Hygiene under-reporting rule."""
    inv = _inv([_rec("a2020", item_type="webpage"),
                _rec("b2021", item_type="journalArticle"),
                _rec("c2022", item_type="journalArticle")])
    rep = zotero_no_pdf.find(inv, dismissed={"a2020"})
    assert [r.citation_key for r in rep.rows] == ["b2021", "c2022"]
    assert rep.dismissed_count == 1


def test_report_carries_the_item_type_and_its_histogram():
    inv = _inv([_rec("a2020", item_type="webpage"),
                _rec("b2021", item_type="webpage"),
                _rec("c2022", item_type="journalArticle")])
    rep = zotero_no_pdf.find(inv)
    assert rep.by_item_type == {"webpage": 2, "journalArticle": 1}
    assert {r.item_type for r in rep.rows} == {"webpage", "journalArticle"}


def test_a_record_with_a_pdf_is_never_a_row_dismissed_or_not():
    inv = _inv([_rec("has2020", pdfs=["/v/x.pdf"], item_type="journalArticle")])
    rep = zotero_no_pdf.find(inv, dismissed={"has2020"})
    assert rep.rows == []
    # It was never eligible, so it is not "hidden" either.
    assert rep.dismissed_count == 0


def test_histogram_counts_only_visible_rows():
    """The bulk-dismiss button offers "webpage · N" — N must be what pressing
    it would actually act on, not a figure including already-hidden rows."""
    inv = _inv([_rec("a2020", item_type="webpage"),
                _rec("b2021", item_type="webpage")])
    rep = zotero_no_pdf.find(inv, dismissed={"a2020"})
    assert rep.by_item_type == {"webpage": 1}


def test_an_entry_with_no_zotero_item_reports_no_type():
    inv = _inv([_rec("a2020")])
    rep = zotero_no_pdf.find(inv)
    assert rep.rows[0].item_type is None
    assert rep.by_item_type == {"unknown": 1}


# ── the cross-report search must agree with the report tab ─────────────────

def test_search_hides_a_dismissed_entry_like_the_report_does():
    """The corpus promises that a hit is a row the user can go and look at.
    A "needs no PDF" dismissal removes the row from the tab, so search must
    stop returning it — it is memoised per scan, hence filtered at query time.
    """
    from audit import search as audit_search

    corpus = [
        audit_search._Entry("zotero_no_pdf", audit_search.fold("beck inventory"),
                            {"label": "Beck", "citation_key": "BeckInventory"}),
        audit_search._Entry("zotero_no_pdf", audit_search.fold("beck other"),
                            {"label": "Other", "citation_key": "OtherKey"}),
    ]
    both = audit_search.search(corpus, "beck")
    assert both["total"] == 2

    one = audit_search.search(corpus, "beck", hidden_keys={"BeckInventory"})
    assert one["total"] == 1, "a dismissed entry must not be findable"
    assert one["groups"][0]["count"] == 1, "the group count must shrink too"
    assert [r["citation_key"] for r in one["groups"][0]["rows"]] == ["OtherKey"]


def test_hidden_keys_only_apply_to_the_render_filtered_report():
    """A citation key dismissed from the no-PDF queue is still a real record
    everywhere else; hiding it from every report would erase it from search."""
    from audit import search as audit_search

    corpus = [
        audit_search._Entry("note_tag_drift", audit_search.fold("beck inventory"),
                            {"label": "Beck", "citation_key": "BeckInventory"}),
    ]
    got = audit_search.search(corpus, "beck", hidden_keys={"BeckInventory"})
    assert got["total"] == 1
