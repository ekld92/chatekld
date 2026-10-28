"""Per-surface tag-coverage counts and the two orphan buckets (2026-08-13).

Both additions answer questions a completed scan could not answer before:
"how many papers carry a tag, and on which surface?" and "what does the vault
hold that no record can see?".

The coverage counts are pinned against a hand-built inventory rather than a
round number, because the defect they replace was not a wrong count — it was a
count with no denominator. Every ``*_tagged`` figure is asserted together with
its ``*_present`` denominator, so a future change that starts counting records
lacking the surface entirely fails here.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from audit import serialize
from audit.engine.inventory import Inventory, OrphanZoteroItem, Record


def _rec(key, *, note_tags=None, finder=(), item_tags=None, pdfs=0,
         bib_keywords=None, child_note_tags=None):
    """One record with an explicit presence/tags combination per surface.

    ``note_tags=None`` means NO reference note (the surface is absent);
    ``note_tags=[]`` means a note that carries no tags. That distinction is the
    whole point of the coverage block, so the helper keeps it addressable.
    """
    rec = Record(citation_key=key)
    if note_tags is not None:
        rec.obsidian_note = SimpleNamespace(
            tags=list(note_tags), path=Path(f"/vault/Z_Zotero_Notes/{key}.md"))
    if item_tags is not None:
        rec.zotero_item = SimpleNamespace(
            item_key=f"IT{key}", item_type="journalArticle", title=key,
            citation_key=key, tags=list(item_tags),
            child_notes=([SimpleNamespace(tags=list(child_note_tags))]
                         if child_note_tags else []))
    rec.pdf_paths = [Path(f"/vault/biblio_articles/{key}-{i}.pdf")
                     for i in range(pdfs)]
    rec.finder_tag_set = set(finder)
    if bib_keywords is not None:
        rec.bib_entry = SimpleNamespace(
            citation_key=key, title=key, year="2020", authors=["A, B"],
            entry_type="article", keywords=set(bib_keywords), doi=None)
    return rec


def _inv(records, **kw):
    bridge = SimpleNamespace(
        unmapped_pdfs=[], ambiguous_pdfs={}, confirmed_no_match_pdfs=[],
        source_per_pdf={}, fm_pointer_fields_seen={}, notes_with_fm=0,
        mapping_orphans=0)
    return Inventory(
        records={r.citation_key: r for r in records},
        bridge=bridge, pdfs_skipped=[], zotero_matched_by_title=0, **kw)


class TestTagCoverage:
    def test_each_surface_counts_present_and_tagged_separately(self):
        inv = _inv([
            # note present + tagged, no PDF, no item
            _rec("a", note_tags=["x"]),
            # note present but EMPTY — present must count, tagged must not
            _rec("b", note_tags=[]),
            # PDF present + Finder-tagged
            _rec("c", pdfs=1, finder=["y"]),
            # PDF present, no Finder tags
            _rec("d", pdfs=2),
            # Zotero item present + tagged
            _rec("e", item_tags=["z"]),
            # Zotero item present, untagged
            _rec("f", item_tags=[]),
        ])
        c = serialize.tag_coverage(inv)
        assert (c["note_present"], c["note_tagged"]) == (2, 1)
        assert (c["finder_present"], c["finder_tagged"]) == (2, 1)
        assert (c["zotero_present"], c["zotero_tagged"]) == (2, 1)
        assert c["records_total"] == 6

    def test_any_and_none_are_over_the_writable_denominator(self):
        inv = _inv([
            _rec("tagged", note_tags=["x"], pdfs=1, item_tags=[]),
            _rec("bare", note_tags=[], pdfs=1),
            # A bib-only record has NO writable surface: it is not "untagged",
            # it is unwritable, and counting it as untagged would invent work
            # the harmonizer can never do.
            _rec("bibonly", bib_keywords=["kw"]),
        ])
        c = serialize.tag_coverage(inv)
        assert c["writable_records"] == 2
        assert c["any_tagged"] == 1
        assert c["none_tagged"] == 1
        assert c["records_total"] == 3

    def test_a_tag_on_any_one_surface_makes_the_record_tagged(self):
        for kw in ({"note_tags": ["x"]}, {"pdfs": 1, "finder": ["x"]},
                   {"item_tags": ["x"]}):
            c = serialize.tag_coverage(_inv([_rec("k", **kw)]))
            assert c["any_tagged"] == 1, kw
            assert c["none_tagged"] == 0, kw

    def test_read_only_surfaces_are_counted_but_kept_separate(self):
        # bib keywords and Zotero CHILD-note tags are readable and never
        # writable; they must not leak into any_tagged, or the dashboard would
        # report work as done that no writer can do.
        inv = _inv([_rec("a", note_tags=[], bib_keywords=["kw"],
                         item_tags=[], child_note_tags=["cn"])])
        c = serialize.tag_coverage(inv)
        assert c["bib_keywords_tagged"] == 1
        assert c["zotero_note_tagged"] == 1
        assert c["any_tagged"] == 0

    def test_floor_and_platform_caveats_ride_the_payload(self):
        # A client must not be able to render the Finder number without the
        # disclosure that it is a floor — the Tag Hygiene lesson.
        c = serialize.tag_coverage(_inv([_rec("a", pdfs=1, finder=["x"])]))
        assert c["finder_is_floor"] is True
        assert isinstance(c["finder_platform"], str) and c["finder_platform"]

    def test_summary_embeds_coverage_and_orphan_counts(self):
        inv = _inv(
            [_rec("a", note_tags=["x"])],
            notes_without_record=[Path("/vault/Z_Zotero_Notes/stray.md")],
            zotero_items_without_record=[OrphanZoteroItem(item_key="Q1")],
        )
        s = serialize.inventory_summary(inv)
        assert s["tag_coverage"]["note_tagged"] == 1
        assert s["notes_without_record"] == 1
        assert s["zotero_items_without_record"] == 1


class TestInventoryRowParentTags:
    def test_zotero_parent_tags_ship_on_the_row(self):
        # The one surface apply-zotero-tags writes was the one array the
        # inventory row never carried.
        inv = _inv([_rec("a", item_tags=["b", "a"])])
        settings = SimpleNamespace(vault_root=Path("/vault"))
        row = serialize.inventory_records(inv, settings)[0]
        assert row["zotero_parent_tags"] == ["a", "b"]

    def test_absent_item_yields_an_empty_list_not_a_missing_key(self):
        inv = _inv([_rec("a", note_tags=[])])
        settings = SimpleNamespace(vault_root=Path("/vault"))
        row = serialize.inventory_records(inv, settings)[0]
        assert row["zotero_parent_tags"] == []


class TestOrphanBuckets:
    def test_orphan_notes_serialize_vault_relative(self):
        inv = _inv([], notes_without_record=[
            Path("/vault/Z_Zotero_Notes/stray.md")])
        settings = SimpleNamespace(vault_root=Path("/vault"))
        out = serialize.orphan_notes(inv, settings)
        assert out["count"] == 1
        assert out["rows"][0]["rel"] == "Z_Zotero_Notes/stray.md"
        assert out["rows"][0]["name"] == "stray.md"

    def test_orphan_items_report_how_many_carry_tags(self):
        inv = _inv([], zotero_items_without_record=[
            OrphanZoteroItem(item_key="A", title="Zed", tags=["t"]),
            OrphanZoteroItem(item_key="B", title="Alpha"),
        ])
        out = serialize.orphan_zotero_items(inv)
        assert out["count"] == 2
        # The tagged count is the point of the bucket: those tags reach no
        # report, because every report iterates records.
        assert out["tagged"] == 1
        assert [r["title"] for r in out["rows"]] == ["Alpha", "Zed"]

    def test_orphan_items_carry_their_library_routing(self):
        """2026-08-18: a group-library orphan needs
        zotero://select/groups/<id>/items/<key> — the personal-library deep
        link selects NOTHING for it — but OrphanZoteroItem dropped the routing
        fields, so the UI's [Zotero] verb was silently dead for every group
        item. Additive fields; a user-library item keeps nulls."""
        inv = _inv([], zotero_items_without_record=[
            OrphanZoteroItem(item_key="G1", title="Group paper",
                             library_type="group", group_id=484650),
            OrphanZoteroItem(item_key="U1", title="User paper",
                             library_type="user"),
        ])
        rows = {r["item_key"]: r for r in serialize.orphan_zotero_items(inv)["rows"]}
        assert rows["G1"]["library_type"] == "group"
        assert rows["G1"]["group_id"] == 484650
        assert rows["U1"]["library_type"] == "user"
        assert rows["U1"]["group_id"] is None

    def test_empty_buckets_serialize_as_zero_not_as_absent(self):
        inv = _inv([])
        settings = SimpleNamespace(vault_root=Path("/vault"))
        assert serialize.orphan_notes(inv, settings) == {"rows": [], "count": 0}
        assert serialize.orphan_zotero_items(inv)["count"] == 0


class TestOrphanNotesFromRealBuild:
    """The bucket must be filled by ``build_inventory`` itself, not only by a
    hand-constructed Inventory — the defect was that the builder dropped these
    notes in a local variable, so a serializer test alone would have passed
    against the broken code."""

    def test_a_note_matching_no_key_lands_in_the_bucket(self, tmp_path: Path):
        from audit.config import Settings
        from audit.engine import inventory as eng

        (tmp_path / "Z_attachments" / "biblio_articles").mkdir(parents=True)
        (tmp_path / "_master.bib").write_text(
            "@article{smith2020, title={X}, year={2020}, author={Smith, J}}",
            encoding="utf-8")
        notes = tmp_path / "Z_Zotero_Notes"
        notes.mkdir()
        # One note whose stem IS a bib key, one whose stem is nothing at all.
        (notes / "smith2020.md").write_text("---\ntags: [a]\n---\n", encoding="utf-8")
        (notes / "orphan_note.md").write_text("---\ntags: [b]\n---\n", encoding="utf-8")

        inv = eng.build_inventory(
            Settings(vault_root=tmp_path.resolve(), zotero_sqlite=tmp_path / "absent.sqlite"),
            count_annotations=False)

        assert [p.name for p in inv.notes_without_record] == ["orphan_note.md"]
        # And the joined one is genuinely joined, so the bucket is a residue
        # rather than "every note".
        assert inv.records["smith2020"].obsidian_note is not None

    def test_a_cancelled_scan_does_not_invent_zotero_orphans(self, tmp_path: Path):
        """Since 49afa1d the records loop no longer breaks on cancel (it only
        skips the annotation reads), but `claimed_item_keys` still fills inside
        that loop — so the orphan computation recomputes the authoritative
        BBT-key join instead of trusting it. This test guards against a
        reintroduced early-exit ever again turning every item of an unvisited
        record into a reported orphan (the pre-49afa1d failure)."""
        from audit.config import Settings
        from audit.engine import inventory as eng

        biblio = tmp_path / "Z_attachments" / "biblio_articles"
        biblio.mkdir(parents=True)
        (tmp_path / "_master.bib").write_text(
            "@article{a2020, title={A}, year={2020}, author={Smith, J}}\n"
            "@article{b2021, title={B}, year={2021}, author={Taylor, K}}\n",
            encoding="utf-8")
        # Each record needs a resolved PDF: the records loop's cancel check
        # (now an annotation-skip, not a break) is guarded by
        # `count_annotations and rec.pdf_paths`, so a PDF-less inventory
        # never exercises the cancel path this test is about.
        (biblio / "smith_2020.pdf").write_bytes(b"%PDF-1.4\n")
        (biblio / "taylor_2021.pdf").write_bytes(b"%PDF-1.4\n")

        items = [
            SimpleNamespace(item_key="IT-A", item_type="journalArticle",
                            title="A", abstract=None, citation_key="a2020",
                            tags=["x"], attachments=[], child_notes=[],
                            library_id=1, library_type="user", group_id=None),
            SimpleNamespace(item_key="IT-B", item_type="journalArticle",
                            title="B", abstract=None, citation_key="b2021",
                            tags=[], attachments=[], child_notes=[],
                            library_id=1, library_type="user", group_id=None),
        ]

        # A real (empty) file so the zotero branch is taken without patching
        # Path.exists, then `read_items` is stubbed to return our two items.
        sqlite = tmp_path / "zotero.sqlite"
        sqlite.write_bytes(b"")
        settings = Settings(vault_root=tmp_path.resolve(), zotero_sqlite=sqlite)

        # cancel_fn True from the start: pre-49afa1d the records loop broke
        # before populating `claimed_item_keys` — exactly the state that used
        # to turn every joined item into a reported orphan. Today the loop
        # completes (skipping only annotations), and the recomputed key join
        # keeps the orphan set correct even if an early-exit reappears.
        with mock.patch.object(eng.zotero, "read_items", return_value=items):
            inv = eng.build_inventory(settings, count_annotations=True,
                                      cancel_fn=lambda: True)

        # Both items are claimed by the KEY join, which no cancel can affect.
        assert inv.zotero_items_without_record == []


class TestInventoryRowPairDetail:
    """The pairings-board additions (2026-08-19 review, request v)."""

    def test_per_pdf_match_source_rides_each_path_payload(self):
        # The bridge has always known HOW each PDF matched; only the
        # per-record union was serialized, so per-pair provenance was
        # invisible. Absent entries stay None (never a fabricated label).
        rec = _rec("a", pdfs=2)
        inv = _inv([rec])
        inv.bridge.source_per_pdf = {rec.pdf_paths[0]: "doi"}
        settings = SimpleNamespace(vault_root=Path("/vault"))
        row = serialize.inventory_records(inv, settings)[0]
        assert row["pdf_paths"][0]["source"] == "doi"
        assert row["pdf_paths"][1]["source"] is None
        # The pre-existing payload keys are untouched (additive change).
        assert row["pdf_paths"][0]["rel"] == "biblio_articles/a-0.pdf"

    def test_note_and_zotero_detail_blocks_are_additive_and_absent_safe(self):
        rec = _rec("a", note_tags=["t"], item_tags=["z"],
                   child_note_tags=["c"])
        settings = SimpleNamespace(vault_root=Path("/vault"))
        row = serialize.inventory_records(_inv([rec]), settings)[0]
        assert row["note"]["rel"] == "Z_Zotero_Notes/a.md"
        assert row["zotero"]["item_key"] == "ITa"
        assert row["zotero"]["item_type"] == "journalArticle"
        assert row["zotero"]["child_note_count"] == 1
        bare = serialize.inventory_records(_inv([_rec("b")]), settings)[0]
        assert bare["note"] is None
        assert bare["zotero"] is None
