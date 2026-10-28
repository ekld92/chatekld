"""The three-way writable-surface tag comparison (2026-08-13).

This report is what the bulk harmonize selection is built from, so its
semantics are load-bearing rather than cosmetic: every row it emits becomes a
confirm dialog promising N writes.

The four properties pinned hardest, each of which was a deliberate decision
rather than a fallout of the implementation:

  1. NO >=1-FINDER-TAG GATE. ``finder_tag_drift`` skips records whose Finder
     set is empty; copying that here would exclude the single most common
     case the feature exists for (a tagged note, an untagged PDF).
  2. WRITABLE SURFACES ONLY. Zotero child-note tags and bib keywords are
     read-only everywhere in the app; folding them into the union would
     propagate tags no writer could ever take back.
  3. ABSENT != UNTAGGED. A record with no reference note is missing a NOTE,
     not a note tag — it must never appear in ``missing_in_note``.
  4. ONE REPRESENTATIVE SPELLING per canonical key, shared across surfaces,
     so a tag missing from two surfaces resolves to one vocabulary id.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from audit import serialize
from audit.engine.inventory import Inventory, Record
from audit.engine.reports.tag_harmony import find_harmony


def _rec(key, *, note=None, finder=None, zotero=None, child_notes=(),
         bib_keywords=None, pdfs=1):
    """A record with explicit per-surface presence.

    ``None`` means the surface is ABSENT; a list (even empty) means present.
    """
    rec = Record(citation_key=key)
    if note is not None:
        rec.obsidian_note = SimpleNamespace(
            tags=list(note), path=Path(f"/vault/Z_Zotero_Notes/{key}.md"))
    if zotero is not None:
        rec.zotero_item = SimpleNamespace(
            item_key=f"IT{key}", item_type="journalArticle", title=key,
            citation_key=key, tags=list(zotero),
            child_notes=[SimpleNamespace(tags=list(child_notes))]
            if child_notes else [])
    if finder is not None:
        rec.pdf_paths = [Path(f"/vault/biblio_articles/{key}-{i}.pdf")
                         for i in range(pdfs)]
        rec.finder_tag_set = set(finder)
    rec.bib_entry = SimpleNamespace(
        citation_key=key, title=f"Title {key}", year="2020",
        authors=["Smith, J"], entry_type="article",
        keywords=set(bib_keywords or ()), doi=None)
    return rec


def _inv(records):
    bridge = SimpleNamespace(
        unmapped_pdfs=[], ambiguous_pdfs={}, confirmed_no_match_pdfs=[],
        source_per_pdf={}, fm_pointer_fields_seen={}, notes_with_fm=0,
        mapping_orphans=0)
    return Inventory(records={r.citation_key: r for r in records},
                     bridge=bridge, pdfs_skipped=[], zotero_matched_by_title=0)


def _one(records, **kw):
    rows = find_harmony(_inv(records), **kw)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    return rows[0]


class TestUnionDirection:
    def test_propagates_from_each_surface_to_the_others(self):
        r = _one([_rec("a", note=["alpha"], finder=[], zotero=[])])
        assert r.union == {"alpha"}
        assert r.missing_in_note == set()
        assert r.missing_in_finder == {"alpha"}
        assert r.missing_in_zotero == {"alpha"}

    def test_an_untagged_pdf_is_the_common_case_not_an_excluded_one(self):
        # Property 1. finder_tag_drift would emit NOTHING here (zero Finder
        # tags); this report must emit the row, because propagating a tagged
        # note onto its untagged PDF is the point of the feature.
        rows = find_harmony(_inv([_rec("a", note=["alpha"], finder=[])]))
        assert len(rows) == 1
        assert rows[0].missing_in_finder == {"alpha"}

    def test_three_surfaces_each_contribute_to_one_union(self):
        r = _one([_rec("a", note=["n"], finder=["f"], zotero=["z"])])
        assert r.union == {"n", "f", "z"}
        assert r.missing_in_note == {"f", "z"}
        assert r.missing_in_finder == {"n", "z"}
        assert r.missing_in_zotero == {"n", "f"}
        assert r.write_count == 6

    def test_agreeing_surfaces_emit_no_row(self):
        assert find_harmony(_inv([
            _rec("a", note=["x"], finder=["x"], zotero=["x"])])) == []

    def test_records_with_no_tags_anywhere_emit_no_row(self):
        assert find_harmony(_inv([_rec("a", note=[], finder=[], zotero=[])])) == []


class TestSurfaceScope:
    def test_read_only_surfaces_never_enter_the_union(self):
        # Property 2. A child-note tag and a bib keyword are both invisible to
        # every writer, so proposing them would create an un-undoable write.
        rows = find_harmony(_inv([
            _rec("a", note=["x"], finder=["x"], zotero=["x"],
                 child_notes=["childonly"], bib_keywords=["bibonly"])]))
        assert rows == []

    def test_absent_surface_is_not_reported_as_missing(self):
        # Property 3: no reference note at all.
        r = _one([_rec("a", note=None, finder=["f"], zotero=[])])
        assert "note" not in r.present
        assert r.missing_in_note == set()
        assert r.missing_in_zotero == {"f"}

    def test_a_single_writable_surface_cannot_be_harmonized(self):
        # Only a note exists — there is nowhere to propagate to, and nothing
        # to propagate from. Emitting a row would invent work.
        assert find_harmony(_inv([_rec("a", note=["x"], finder=None)])) == []

    def test_a_pdfless_record_still_harmonizes_note_and_zotero(self):
        # 2 081 records in the maintainer's library have no local PDF; they
        # are perfectly writable on the other two surfaces.
        r = _one([_rec("a", note=["x"], finder=None, zotero=[])])
        assert r.present == {"note", "zotero"}
        assert r.missing_in_zotero == {"x"}
        assert r.missing_in_finder == set()


class TestSpellingAndSynonyms:
    def test_a_case_or_accent_variant_is_not_missing(self):
        # The fingerprint comparison: the whole reason a case-only "drift" row
        # was once unresolvable forever.
        rows = find_harmony(_inv([
            _rec("a", note=["Dépression"], finder=["depression"], zotero=[])]))
        assert len(rows) == 1
        # Nothing missing between note and Finder; only Zotero lacks it.
        assert rows[0].missing_in_note == set()
        assert rows[0].missing_in_finder == set()
        assert rows[0].missing_in_zotero

    def test_variant_spellings_are_reported_but_not_applyable(self):
        r = _one([_rec("a", note=["Dépression"], finder=["depression"],
                       zotero=[])])
        assert r.variants == [["Dépression", "depression"]]

    def test_a_variants_only_record_emits_no_row(self):
        # Nothing to apply — writing either spelling would no-op. The two
        # pairwise drift reports already surface this case; a selection list
        # must not contain rows a bulk apply cannot act on.
        assert find_harmony(_inv([
            _rec("a", note=["Dépression"], finder=["depression"],
                 zotero=["DEPRESSION"])])) == []

    def test_one_representative_spelling_is_shared_across_surfaces(self):
        # Property 4. Two surfaces lack the same tag; both must name it with
        # the SAME string, or the route resolves two vocabulary ids for one
        # tag and the confirm dialog double-counts the write.
        r = _one([_rec("a", note=["Dépression"], finder=[], zotero=[])])
        assert r.missing_in_finder == r.missing_in_zotero == {"Dépression"}
        assert r.union == {"Dépression"}

    def test_the_representative_is_deterministic_across_surface_spellings(self):
        # Both surfaces that HAVE the tag spell it differently, and a third
        # lacks it. Whichever spelling is chosen, it must be stable — an
        # unstable pick would make the row's vocabulary resolution flap
        # between scans of an unchanged library.
        rows = [_one([_rec("a", note=["Dépression"], finder=["depression"],
                           zotero=[])]) for _ in range(3)]
        picks = {tuple(sorted(r.missing_in_zotero)) for r in rows}
        assert len(picks) == 1
        # And it is one of the two real spellings, never a synthesised form.
        assert picks.pop()[0] in {"Dépression", "depression"}

    def test_synonyms_unify_surfaces(self):
        # An FR/EN synonym pair from _tags.md must count as the same tag, not
        # as drift in both directions.
        syn = {"depistage": "screening"}
        rows = find_harmony(
            _inv([_rec("a", note=["depistage"], finder=["screening"],
                       zotero=["screening"])]),
            synonyms=syn)
        assert rows == []


class TestOrdering:
    def test_most_writes_first(self):
        rows = find_harmony(_inv([
            _rec("small", note=["x"], finder=[], zotero=["x"]),
            _rec("big", note=["a", "b", "c"], finder=[], zotero=[]),
        ]))
        assert [r.citation_key for r in rows] == ["big", "small"]

    def test_tagged_surface_count_identifies_the_single_source_case(self):
        r = _one([_rec("a", note=["x", "y"], finder=[], zotero=[])])
        assert r.tagged_surface_count == 1
        r2 = _one([_rec("b", note=["x"], finder=["y"], zotero=[])])
        assert r2.tagged_surface_count == 2


class TestSerializer:
    def _settings(self):
        return SimpleNamespace(vault_root=Path("/vault"))

    def test_vocab_ids_bridge_the_two_identity_rules(self):
        # The report compares by fingerprint; the writers gate on the literal
        # _tags.md id. Without this map an accented surface would be rejected
        # as out-of-vocabulary while the row reappeared at every scan.
        rows = find_harmony(_inv([
            _rec("a", note=["Dépression"], finder=[], zotero=[])]))
        out = serialize.report_tag_harmony(
            rows, self._settings(),
            vocab_id_for=lambda s: "depression" if s == "Dépression" else None)
        assert out["rows"][0]["vocab_ids"] == {"Dépression": "depression"}

    def test_applyable_count_excludes_out_of_vocab_surfaces(self):
        # "12 tags to propagate" must not turn out to mean 3 at write time.
        rows = find_harmony(_inv([
            _rec("a", note=["known", "unknown"], finder=[], zotero=[])]))
        out = serialize.report_tag_harmony(
            rows, self._settings(),
            vocab_id_for=lambda s: "known" if s == "known" else None)
        row = out["rows"][0]
        assert row["write_count"] == 4       # 2 tags x 2 missing surfaces
        assert row["applyable_count"] == 2   # only the in-vocab one
        assert out["write_count"] == 4
        assert out["applyable_count"] == 2

    def test_no_resolver_degrades_to_read_only(self):
        rows = find_harmony(_inv([_rec("a", note=["x"], finder=[], zotero=[])]))
        out = serialize.report_tag_harmony(rows, self._settings())
        assert out["vocab_available"] is False
        assert out["rows"][0]["vocab_ids"] == {}
        assert out["applyable_count"] == 0

    def test_envelope_counts_the_single_surface_rows(self):
        rows = find_harmony(_inv([
            _rec("a", note=["x"], finder=[], zotero=[]),
            _rec("b", note=["x"], finder=["y"], zotero=[]),
        ]))
        out = serialize.report_tag_harmony(rows, self._settings())
        assert out["count"] == 2
        assert out["single_surface_count"] == 1

    def test_note_rel_is_vault_relative(self):
        rows = find_harmony(_inv([_rec("a", note=["x"], finder=[], zotero=[])]))
        out = serialize.report_tag_harmony(rows, self._settings())
        assert out["rows"][0]["note_rel"] == "Z_Zotero_Notes/a.md"

    def test_envelope_discloses_the_finder_platform(self):
        """2026-08-18: Finder xattrs read empty off macOS, so on any other
        platform every union tag looks 'missing in Finder' — fabricated drift.
        The envelope must carry the platform (tag_coverage's finder_platform
        precedent) so the renderer can caveat the → Finder column."""
        import sys as _sys
        rows = find_harmony(_inv([_rec("a", note=["x"], finder=[], zotero=[])]))
        out = serialize.report_tag_harmony(rows, self._settings())
        assert out["finder_platform"] == _sys.platform
