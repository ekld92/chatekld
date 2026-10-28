"""The Zotero attachment read → inventory → report → search corpus chain.

``ZoteroItem.attachments`` was populated and read by nothing for two years. Now
it backs a report and a de-link verb, so the two facts a removal decision turns
on have to survive the whole chain intact:

* ``link_mode`` — whether Zotero owns the bytes (an ``imported_*`` file lives in
  its data directory) or merely points at them.
* ``annotation_count`` — highlights hang off the attachment and are trashed with
  it, so an undisclosed count is a silent loss.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from audit import serialize
from audit.core import zotero
from audit.engine.inventory import ZoteroAttachmentRow


class TestLinkModeVocabulary:
    def test_the_integers_map_to_the_web_apis_own_names(self):
        """Verified live 2026-08-28: an attachment stored with linkMode 4 comes
        back from api.zotero.org as ``embedded_image``. Keeping the same
        vocabulary on both sides means a row read from SQLite can be compared
        with an API response without a second mapping."""
        assert zotero.LINK_MODES[4] == "embedded_image"
        assert zotero.LINK_MODES[2] == "linked_file"
        assert zotero.LINK_MODES[0] == "imported_file"

    def test_only_imported_and_embedded_modes_are_owned_by_zotero(self):
        """A linked file is a pointer Zotero never owned; an imported one lives
        under ~/Zotero/storage/<key>/ and goes with the item."""
        owned = zotero.ZOTERO_OWNED_MODES
        assert {"imported_file", "imported_url", "embedded_image"} == set(owned)
        assert "linked_file" not in owned and "linked_url" not in owned

    def test_the_owned_flag_is_derived_not_stored(self):
        att = zotero.ZoteroAttachment(
            item_key="A", parent_key="P", filename="x.pdf", storage_path=None,
            is_stored=False, content_type="application/pdf",
            link_mode="linked_file")
        assert att.file_owned_by_zotero is False
        att.link_mode = "imported_file"
        assert att.file_owned_by_zotero is True

    def test_an_unknown_link_mode_is_not_treated_as_owned(self):
        """A future Zotero link mode must default to the conservative answer for
        the FILE question — but note it stays removable, since trashing never
        deletes bytes anyway."""
        att = zotero.ZoteroAttachment(
            item_key="A", parent_key=None, filename="", storage_path=None,
            is_stored=False, content_type=None, link_mode=None)
        assert att.file_owned_by_zotero is False


class TestAnnotationCountIsGuarded:
    """``itemAnnotations`` arrived in Zotero 6. Reading an older library must
    degrade to "no annotations", never fail the whole scan.

    Driven through the real :func:`read_items` with a fake connection, so it
    exercises the actual guard rather than asserting on the source. (The
    positive path — SQL row to ``annotation_count`` — was verified against the
    live 6,399-item library, which a hermetic test cannot reproduce without
    reconstructing Zotero's whole schema.)
    """

    def test_a_database_without_itemAnnotations_still_reads(self, monkeypatch):
        from contextlib import contextmanager

        class _Cursor:
            def fetchone(self):
                return None

            def fetchall(self):
                return []

        class _FakeConn:
            def __init__(self):
                self.saw_annotations = False

            def execute(self, sql, *args):
                if "itemAnnotations" in sql:
                    self.saw_annotations = True
                    raise sqlite3.Error("no such table: itemAnnotations")
                return _Cursor()

        conn = _FakeConn()

        @contextmanager
        def _fake_snapshot(_path):
            yield conn

        monkeypatch.setattr(zotero, "_open_snapshot", _fake_snapshot)
        items = zotero.read_items(SimpleNamespace(), SimpleNamespace())
        assert items == []
        assert conn.saw_annotations, "the guarded query never ran — test is vacuous"


class TestSerializer:
    def _inv(self, rows):
        return SimpleNamespace(zotero_attachments=rows)

    def _row(self, key, **kw):
        base = dict(item_key=key, parent_key="P", parent_title="A paper",
                    parent_citation_key="smith2020",
                    parent_item_type="journalArticle", link_mode="imported_file",
                    content_type="application/pdf", filename=f"{key}.pdf",
                    raw_path=None, annotation_count=0,
                    file_owned_by_zotero=True, library_type="user",
                    group_id=None)
        base.update(kw)
        return ZoteroAttachmentRow(**base)

    def test_counts_the_two_facts_a_removal_turns_on(self):
        d = serialize.zotero_attachments(self._inv([
            self._row("A", annotation_count=3),
            self._row("B", link_mode="linked_file", file_owned_by_zotero=False),
            self._row("C", content_type="text/html"),
        ]))
        assert d["count"] == 3
        assert d["owned"] == 2
        assert d["with_annotations"] == 1
        assert d["pdfs"] == 2

    def test_an_inventory_predating_the_field_serialises_empty(self):
        assert serialize.zotero_attachments(SimpleNamespace())["count"] == 0

    def test_rows_carry_the_attachments_own_key_as_the_write_target(self):
        d = serialize.zotero_attachments(self._inv([self._row("ATT1")]))
        assert d["rows"][0]["item_key"] == "ATT1"
        assert d["rows"][0]["parent_key"] == "P"

    def test_rows_carry_the_title_beside_the_filename(self):
        """T4 (2026-09-18): ``filename`` is the PATH's basename; ``title`` is
        what Zotero displays. The retitle proposer compares the two, so they
        must never be conflated."""
        d = serialize.zotero_attachments(self._inv([
            self._row("A", title="Full Text PDF", filename="smith_2020.pdf")]))
        assert (d["rows"][0]["title"], d["rows"][0]["filename"]) == (
            "Full Text PDF", "smith_2020.pdf")

    def test_sorted_stably_by_key_then_filename(self):
        d = serialize.zotero_attachments(self._inv([
            self._row("B", parent_citation_key="zeta"),
            self._row("A", parent_citation_key="alpha"),
        ]))
        assert [r["item_key"] for r in d["rows"]] == ["A", "B"]


class TestSearchCorpus:
    """A filename absent from a search that claims to cover every report reads
    to the user as "Zotero does not have it"."""

    def _inv(self):
        return SimpleNamespace(
            records={}, bridge=SimpleNamespace(
                unmapped_pdfs=[], ambiguous_pdfs={}, confirmed_no_match_pdfs=[],
                bib_to_pdfs={}, source_per_pdf={}, mapping_orphans=0,
                doi_collisions={}, pdf_provenance={}),
            pdfs_skipped=[], notes_without_record=[],
            zotero_items_without_record=[], unmapped_annotations={},
            zotero_error=None,
            zotero_attachments=[ZoteroAttachmentRow(
                item_key="ATT1", parent_key="P1", parent_title="A paper",
                parent_citation_key="smith2020", link_mode="imported_file",
                content_type="application/pdf", filename="quetiapine.pdf",
                annotation_count=4, file_owned_by_zotero=True)])

    def test_the_report_is_declared(self):
        from audit import search
        assert "zotero_attachments" in search.REPORT_ORDER

    def test_an_attachment_filename_is_findable(self, tmp_path):
        from audit import search
        from audit.config import Settings
        corpus = search.build_corpus(self._inv(), Settings(vault_root=tmp_path))
        hits = [e for e in corpus if e.report == "zotero_attachments"]
        assert len(hits) == 1
        res = search.search(corpus, "quetiapine")
        groups = {g["report"]: g for g in res["groups"]}
        assert "zotero_attachments" in groups, res
        assert groups["zotero_attachments"]["rows"][0]["citation_key"] == "smith2020"

    def test_matching_is_accent_and_case_folded_like_every_other_row(self, tmp_path):
        """The vault is bilingual FR/EN; the corpus folds both sides."""
        from audit import search
        from audit.config import Settings
        inv = self._inv()
        inv.zotero_attachments[0].filename = "Étude_Générale.pdf"
        corpus = search.build_corpus(inv, Settings(vault_root=tmp_path))
        assert search.search(corpus, "etude generale")["total"] == 1
