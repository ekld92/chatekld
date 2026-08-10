"""Pure coverage for the fingerprint-keyed Note Tag Drift report
(plan 2026-08-05/2).

No vault, no scan: ``find_drift(inv)`` only reads ``inv.records``, so the
inventory is a SimpleNamespace over real ``Record`` dataclasses with
lightweight source stand-ins (the ``test_tag_hygiene.py`` pattern). Pins the
core semantics change: a tag present in the note under ANY spelling is a
*variant* (informational), never *missing* (applyable) — the raw-string
comparison used to report case/accent/NFC/separator differences as drift,
and a case-only row was unresolvable forever (it no-opped in the writer's
case-insensitive dedup and reappeared every scan).
"""

from __future__ import annotations

import json
import unicodedata
from types import SimpleNamespace

from audit import serialize
from audit.engine.inventory import Record
from audit.engine.normalize import clean_tag, fingerprint, tag_key
from audit.engine.reports.note_tag_drift import (
    find_drift,
    missing_by_key,
    variant_pairs,
)


def _record(key, note_tags=None, zotero_note_tags=()):
    rec = Record(citation_key=key)
    if note_tags is not None:
        rec.obsidian_note = SimpleNamespace(tags=list(note_tags))
    rec.zotero_item = SimpleNamespace(
        tags=[],
        child_notes=[SimpleNamespace(tags=list(zotero_note_tags))],
    )
    return rec


def _inv(*records):
    return SimpleNamespace(records={r.citation_key: r for r in records})


# --- normalize helpers ------------------------------------------------------

def test_clean_tag_strips_hash_and_padding_in_both_orders():
    assert clean_tag("# tag") == "tag"      # the old reader kept " tag"
    assert clean_tag("  #x  ") == "x"
    assert clean_tag("") == ""


def test_tag_key_folds_case_accents_and_separators():
    assert tag_key("Dépression") == tag_key("depression")
    assert tag_key("machine-learning") == tag_key("machine learning")
    assert tag_key("machine_learning") == tag_key("Machine Learning")


def test_tag_key_non_latin_fallback_keeps_distinct_tags_distinct():
    # Greek fingerprints to "" — the fallback must not merge everything.
    assert fingerprint("ψυχή") == ""
    assert tag_key("ψυχή") != tag_key("λόγος")
    assert tag_key("ψυχή") == tag_key("ΨΥΧΉ")  # casefold still applies


# --- missing vs variants ----------------------------------------------------

def test_case_only_difference_is_a_variant_not_missing():
    rows = find_drift(_inv(_record(
        "a2020", note_tags=["anxiety"], zotero_note_tags=["Anxiety"])))
    (row,) = rows
    assert row.missing_in_obs == set()
    assert row.variants == [("Anxiety", "anxiety", "spelling")]


def test_accent_and_separator_differences_are_variants():
    rows = find_drift(_inv(_record(
        "a2020",
        note_tags=["depression", "machine learning"],
        zotero_note_tags=["Dépression", "machine-learning"])))
    (row,) = rows
    assert row.missing_in_obs == set()
    assert sorted(row.variants) == [
        ("Dépression", "depression", "spelling"),
        ("machine-learning", "machine learning", "spelling"),
    ]


def test_nfc_vs_nfd_is_a_variant_not_missing():
    nfc = unicodedata.normalize("NFC", "rétablissement")
    nfd = unicodedata.normalize("NFD", "rétablissement")
    assert nfc != nfd  # distinct byte strings, the observed macOS drift class
    rows = find_drift(_inv(_record(
        "a2020", note_tags=[nfd], zotero_note_tags=[nfc])))
    (row,) = rows
    assert row.missing_in_obs == set()
    assert len(row.variants) == 1


def test_hash_prefix_and_padding_on_the_zotero_side_are_not_drift():
    # Zotero tags arrive verbatim from SQLite; cleaning must apply to both
    # sides, so "#anxiety" / " sleep " match the note's plain forms exactly.
    rows = find_drift(_inv(_record(
        "a2020", note_tags=["anxiety", "sleep"],
        zotero_note_tags=["#anxiety", " sleep "])))
    assert rows == []


def test_genuinely_missing_tag_is_missing_with_zotero_surface():
    rows = find_drift(_inv(_record(
        "a2020", note_tags=["anxiety"],
        zotero_note_tags=["anxiety", "Insomnie"])))
    (row,) = rows
    assert row.missing_in_obs == {"Insomnie"}
    assert row.variants == []


def test_nested_tag_is_still_missing_by_deliberate_choice():
    # psy/depression fingerprints to "depression psy" ≠ "depression": no
    # parent/child expansion. Pinned so a future change is deliberate.
    rows = find_drift(_inv(_record(
        "a2020", note_tags=["psy/depression"],
        zotero_note_tags=["depression"])))
    (row,) = rows
    assert row.missing_in_obs == {"depression"}


def test_same_spelling_present_on_both_sides_reports_nothing():
    rows = find_drift(_inv(_record(
        "a2020", note_tags=["anxiety"], zotero_note_tags=["anxiety"])))
    assert rows == []


def test_rows_sort_most_missing_first_variant_only_rows_sink():
    rows = find_drift(_inv(
        _record("varonly", note_tags=["anxiety"], zotero_note_tags=["Anxiety"]),
        _record("gap", note_tags=[], zotero_note_tags=["a", "b"]),
    ))
    assert [r.citation_key for r in rows] == ["gap", "varonly"]


def test_gating_unchanged_no_child_notes_or_no_note_is_silent():
    no_note = Record(citation_key="nonote")
    no_note.zotero_item = SimpleNamespace(
        tags=[], child_notes=[SimpleNamespace(tags=["x"])])
    no_children = Record(citation_key="nochild")
    no_children.obsidian_note = SimpleNamespace(tags=[])
    no_children.zotero_item = SimpleNamespace(tags=["x"], child_notes=[])
    assert find_drift(_inv(no_note, no_children)) == []


# --- the shared helpers the route reuses ------------------------------------

def test_missing_by_key_representative_surface_is_stable():
    m = missing_by_key({"Sleep", "SLEEP"}, set())
    # One key, sorted-first surface — deterministic across runs.
    assert m == {tag_key("sleep"): "SLEEP"}


def test_variant_pairs_ignores_keys_with_a_shared_exact_spelling():
    assert variant_pairs({"anxiety", "Anxiety"}, {"anxiety"}) == []
    assert variant_pairs({"Anxiety"}, {"anxiety"}) == [
        ("Anxiety", "anxiety", "spelling")]


# --- serializer -------------------------------------------------------------

def test_serializer_emits_variants_and_stays_json_serialisable():
    rows = find_drift(_inv(_record(
        "a2020", note_tags=["anxiety"],
        zotero_note_tags=["Anxiety", "Insomnie"])))
    payload = serialize.report_note_tag_drift(rows)
    (r,) = payload
    assert r["missing_in_obs"] == ["Insomnie"]
    assert r["variants"] == [
        {"zotero": "Anxiety", "note": "anxiety", "reason": "spelling"}]
    assert r["note_only"] == []
    assert r["item_only_missing"] == []
    json.dumps(payload)  # must not raise


# --- plan 3: parent tags, reverse direction, synonyms ------------------------

def test_parent_item_tags_count_only_when_enabled():
    rec = _record("a2020", note_tags=["anxiety"], zotero_note_tags=["anxiety"])
    rec.zotero_item.tags = ["Insomnie"]
    # Off (default): the item tag is invisible — no drift at all.
    assert find_drift(_inv(rec)) == []
    # On: the item-only tag is missing AND badged as item-only.
    rec2 = _record("a2020", note_tags=["anxiety"], zotero_note_tags=["anxiety"])
    rec2.zotero_item.tags = ["Insomnie"]
    (row,) = find_drift(_inv(rec2), include_parent_tags=True)
    assert row.missing_in_obs == {"Insomnie"}
    assert row.item_only_missing == {"Insomnie"}


def test_parent_tags_mode_relaxes_the_child_note_gate():
    rec = Record(citation_key="nochild")
    rec.obsidian_note = SimpleNamespace(tags=[])
    rec.zotero_item = SimpleNamespace(tags=["sleep"], child_notes=[])
    assert find_drift(_inv(rec)) == []
    (row,) = find_drift(_inv(rec), include_parent_tags=True)
    assert row.missing_in_obs == {"sleep"}


def test_note_only_reverse_direction_is_reported_read_only():
    rows = find_drift(_inv(_record(
        "a2020", note_tags=["anxiety", "cognition"],
        zotero_note_tags=["anxiety"])))
    (row,) = rows
    assert row.missing_in_obs == set()
    assert row.note_only == {"cognition"}


def test_synonym_map_moves_a_pair_from_missing_to_variant():
    synonyms = {tag_key("sommeil"): tag_key("sleep")}
    # Without the map: FR/EN pair is plain drift.
    (row,) = find_drift(_inv(_record(
        "a2020", note_tags=["sommeil"], zotero_note_tags=["sleep"])))
    assert row.missing_in_obs == {"sleep"}
    # With it: a variant with reason "synonym"; an unmapped pair stays missing.
    (row2,) = find_drift(
        _inv(_record("b2021", note_tags=["sommeil"],
                     zotero_note_tags=["sleep", "Insomnie"])),
        synonyms=synonyms)
    assert row2.missing_in_obs == {"Insomnie"}
    assert row2.variants == [("sleep", "sommeil", "synonym")]
