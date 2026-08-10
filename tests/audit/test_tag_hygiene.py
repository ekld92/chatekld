"""Pure coverage for the tag-hygiene report builder + serializer.

No vault, no scan: ``find(inv, vocab)`` only reads ``inv.records``, so the
inventory is a SimpleNamespace over real ``Record`` dataclasses whose source
slots are lightweight stand-ins (the same duck-typing the audit's own
serializer tests use). Pins: fingerprint merging across case/accents/
separators, per-source set-dedup (Zotero tags arrive as a list), vocabulary
flags + near-miss neighbours, co-occurrence, and JSON-serialisability.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from audit import serialize
from audit.engine.inventory import Record
from audit.engine.reports import tag_hygiene


def _record(key, finder=(), note=None, zotero=None, zotero_note_tags=(), bib=()):
    rec = Record(citation_key=key)
    rec.finder_tag_set = set(finder)
    if note is not None:
        rec.obsidian_note = SimpleNamespace(tags=list(note))
    child_notes = ([SimpleNamespace(tags=list(zotero_note_tags))]
                   if zotero_note_tags else [])
    if zotero is not None or child_notes:
        rec.zotero_item = SimpleNamespace(tags=list(zotero or []),
                                          child_notes=child_notes)
    if bib:
        rec.bib_entry = SimpleNamespace(keywords=set(bib))
    return rec


def _inv(*records):
    return SimpleNamespace(records={r.citation_key: r for r in records})


VOCAB = {"dep": "Dépression", "self_harm": "Auto-agressivité", "mood": "Humeur"}


def test_variants_merge_across_case_accents_and_separators():
    inv = _inv(
        _record("a2020", finder=["Self-Harm"], note=["self_harm"]),
        _record("b2021", note=["self harm"], zotero=["SELF HARM"]),
    )
    rep = tag_hygiene.find(inv, VOCAB)
    row = next(r for r in rep.rows if r.fingerprint == "harm self")
    assert set(row.variants) == {"Self-Harm", "self_harm", "self harm", "SELF HARM"}
    assert row.counts == {"finder": 1, "note": 2, "zotero": 1}
    assert row.total == 4
    assert row.in_vocab
    assert row.vocab_tag == "self_harm"


def test_accented_variant_matches_unaccented_vocab_tag():
    inv = _inv(_record("a2020", note=["Dépression"]))
    rep = tag_hygiene.find(inv, {"depression": "gloss"})
    (row,) = rep.rows
    assert row.in_vocab
    assert row.vocab_tag == "depression"


def test_zotero_list_tags_are_set_deduped_per_source():
    # zotero_item.tags is a LIST on the wire; a duplicate entry must not
    # double-count the paper for that source.
    inv = _inv(_record("a2020", zotero=["dep", "dep"]))
    rep = tag_hygiene.find(inv, VOCAB)
    (row,) = rep.rows
    assert row.counts == {"zotero": 1}


def test_out_of_vocab_near_miss_and_orphan():
    inv = _inv(_record("a2020", note=["anxietty"], finder=["cardiology"]))
    rep = tag_hygiene.find(inv, {"anxiety": "gloss", "dep": "gloss"})
    near = next(r for r in rep.rows if r.fingerprint == "anxietty")
    orphan = next(r for r in rep.rows if r.fingerprint == "cardiology")
    assert not near.in_vocab
    assert near.near_vocab == "anxiety"
    assert not orphan.in_vocab
    assert orphan.near_vocab is None


def test_cooccurrence_requires_min_two_papers():
    inv = _inv(
        _record("a2020", note=["dep", "mood"]),
        _record("b2021", note=["dep", "mood", "scz"]),
    )
    rep = tag_hygiene.find(inv, VOCAB)
    pairs = {(p["a"], p["b"]): p["count"] for p in rep.cooccurrence}
    assert pairs == {("dep", "mood"): 2}  # (dep, scz)/(mood, scz) seen once → dropped


def test_no_vocab_still_reports_frequencies():
    inv = _inv(_record("a2020", note=["dep"]))
    rep = tag_hygiene.find(inv, None)
    (row,) = rep.rows
    assert not row.in_vocab
    assert row.near_vocab is None
    assert rep.vocab_size == 0


def test_hash_prefix_stripped_and_empty_dropped():
    inv = _inv(_record("a2020", note=["#dep", "  ", "!!"]))
    rep = tag_hygiene.find(inv, VOCAB)
    (row,) = rep.rows  # "  " and "!!" fingerprint to "" and are dropped
    assert row.fingerprint == "dep"
    assert row.variants == ["dep"]


def test_serializer_is_json_safe_and_sorted_by_total():
    inv = _inv(
        _record("a2020", note=["dep"], finder=["dep"], zotero=["dep"]),
        _record("b2021", note=["mood"]),
    )
    payload = serialize.report_tag_hygiene(tag_hygiene.find(inv, VOCAB))
    json.dumps(payload)  # must not raise
    assert [r["fingerprint"] for r in payload["rows"]] == ["dep", "mood"]
    assert payload["sources"] == list(tag_hygiene.SOURCES)
