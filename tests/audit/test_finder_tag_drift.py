"""Pure coverage for the per-paper Finder-vs-note tag report
(plan 2026-08-05/3 + the drift-fix additions 2026-08-06). Same fake-inventory
pattern as ``test_note_tag_drift.py``.

Pins the asymmetric emission rule: ``finder_only`` always counts (the user
deliberately Finder-tagged that PDF), while ``note_only`` is computed only for
records carrying >=1 Finder tag — a record with zero Finder tags never emits a
row, so a non-macOS run (every Finder set empty) produces an empty report
instead of "every note tag is missing from Finder" noise.

Drift-fix additions pinned here: ``variants`` (present-both-sides spelling
near-misses — previously silently invisible; a variants-only row IS emitted),
``note_path``/``note_rel`` (the write target), and the serializer's
``vocab_ids`` mapping (drift surface → exact ``_tags.md`` id via the injected
resolver; ``None`` resolver ⇒ ``{}`` and the UI degrades to read-only).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from audit import serialize
from audit.engine.inventory import Record
from audit.engine.normalize import canonical_tag_key, tag_key
from audit.engine.reports.finder_tag_drift import find_drift


def _record(key, note_tags=None, finder=(), pdfs=1):
    rec = Record(citation_key=key)
    if note_tags is not None:
        rec.obsidian_note = SimpleNamespace(
            tags=list(note_tags),
            path=Path(f"/vault/Z_Zotero_Notes/{key}.md"),
        )
    rec.finder_tag_set = set(finder)
    rec.pdf_paths = [Path(f"/tmp/{key}-{i}.pdf") for i in range(pdfs)]
    return rec


def _inv(*records):
    return SimpleNamespace(records={r.citation_key: r for r in records})


_SETTINGS = SimpleNamespace(vault_root=Path("/vault"))


def test_finder_only_and_note_only_are_reported_fingerprint_keyed():
    (row,) = find_drift(_inv(_record(
        "a2020", note_tags=["anxiety", "cognition"],
        finder=["Anxiety", "Sommeil"], pdfs=2)))
    # "Anxiety" matches "anxiety" by fingerprint — not finder-only.
    assert row.finder_only == {"Sommeil"}
    assert row.note_only == {"cognition"}
    assert row.pdf_count == 2
    # ...but the case-different pair IS surfaced as a variant now.
    assert row.variants == [("Anxiety", "anxiety", "spelling")]
    assert row.note_path == Path("/vault/Z_Zotero_Notes/a2020.md")


def test_zero_finder_tags_emits_no_row():
    assert find_drift(_inv(_record(
        "a2020", note_tags=["anxiety", "cognition"], finder=()))) == []


def test_identical_sets_emit_no_row():
    assert find_drift(_inv(_record(
        "a2020", note_tags=["anxiety"], finder=["anxiety"]))) == []


def test_variants_only_row_is_emitted():
    """A row whose only disagreement is spelling used to be silently dropped —
    the near-miss was invisible from every surface (drift-fix, 2026-08-06)."""
    (row,) = find_drift(_inv(_record(
        "a2020", note_tags=["sleep quality"], finder=["sleep-quality"])))
    assert row.finder_only == set() and row.note_only == set()
    assert row.variants == [("sleep-quality", "sleep quality", "spelling")]


def test_requires_note_and_pdfs():
    no_note = _record("nonote", note_tags=None, finder=["x"])
    no_pdf = _record("nopdf", note_tags=["a"], finder=["x"], pdfs=0)
    assert find_drift(_inv(no_note, no_pdf)) == []


def test_synonym_map_yields_variant_not_drift():
    synonyms = {tag_key("sommeil"): tag_key("sleep")}
    (row,) = find_drift(
        _inv(_record("a2020", note_tags=["sommeil"], finder=["sleep"])),
        synonyms=synonyms)
    assert row.finder_only == set() and row.note_only == set()
    assert row.variants == [("sleep", "sommeil", "synonym")]


def test_sorts_by_total_disagreement_and_serialises():
    rows = find_drift(_inv(
        _record("small", note_tags=["a"], finder=["a", "b"]),
        _record("big", note_tags=["x"], finder=["c", "d", "e"]),
    ))
    assert [r.citation_key for r in rows] == ["big", "small"]
    payload = serialize.report_finder_tag_drift(rows, _SETTINGS)
    assert payload["rows"][0]["finder_only"] == ["c", "d", "e"]
    assert payload["rows"][0]["note_only"] == ["x"]
    assert payload["rows"][0]["note_rel"] == "Z_Zotero_Notes/big.md"
    # No resolver injected ⇒ empty mapping, UI degrades to read-only.
    assert payload["rows"][0]["vocab_ids"] == {}
    json.dumps(payload)  # must not raise


def test_serializer_vocab_ids_resolver_and_separator_fold():
    """The vocab_ids mapping is what lets the UI POST ids the tagging writers'
    literal-id gate accepts: 'sleep quality' (drift surface) must resolve to
    the vocab id 'sleep-quality' (fingerprint folds separators), and an
    out-of-vocab surface maps to None (badged, not applyable)."""
    vocab = {"sleep-quality": "gloss", "anxiety": ""}
    by_key = {canonical_tag_key(v, None): v for v in vocab}

    def resolver(surface):
        return by_key.get(canonical_tag_key(surface, None))

    rows = find_drift(_inv(_record(
        "a2020", note_tags=["cognition"], finder=["sleep quality"])))
    payload = serialize.report_finder_tag_drift(
        rows, _SETTINGS, vocab_id_for=resolver)
    ids = payload["rows"][0]["vocab_ids"]
    assert ids["sleep quality"] == "sleep-quality"
    assert ids["cognition"] is None
    json.dumps(payload)  # must not raise
