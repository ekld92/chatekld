"""Hermetic unit tests for rag/thesaurus.py — the curated vault thesaurus
parser behind the opt-in query-expansion and system-prompt-primer features.

Model-free and app-independent: ``rag.thesaurus`` imports nothing from the
project, so these tests build a ``Thesaurus`` straight from inline table text.

Pinned invariants:
  * abbreviation surfaces split on backticks / ``/``; bilingual parenthetical
    glosses become synonyms;
  * ``≠``-flagged collisions and ``...`` morphological rules never seed
    expansion;
  * matching is whole-token, accent/case-insensitive, longest-phrase-first;
  * single-tag rows fold their slug into the group, multi-tag rows do NOT
    (no false ``sleep``↔``bzd`` synonymy);
  * the primer is bounded and rendered from the full meaning cell.
"""
from __future__ import annotations

from rag.thesaurus import (
    Thesaurus,
    parse_abbreviations,
    parse_tags,
    _norm,
)

# A miniature but representative slice of the real vault files.
ABBR = """\
# Abréviations

| Abréviation     | Signification                                                       | Notes Associées        |
| --------------- | ------------------------------------------------------------------- | ---------------------- |
| `dep`           | Dépression / Épisode Dépressif Caractérisé                          | [[dep]]                |
| `isrs` / `ssri` | Inhibiteur Sélectif de la Recapture de la Sérotonine                | [[antidep_isrs_ssri]]  |
| `BPD` / `border`| Borderline Personality Disorder (Trouble de la Personnalité Limite) | [[border]]             |
| `svt`           | Souvent                                                             |                        |
| `CI°`           | Contre-Indication (≠ `CI` = Intervalle de Confiance)                |                        |
| `...q`          | `-ique`                                                             | clnq (clinique)        |
| `RSPR` / `RSPRD`| Rispéridone (var. `RISPRDNE`)                                       | [[antipsychotic]]      |
| `iv`            | Intraveineux                                                       | [[iv]]                 |
"""

TAGS = """\
# Tags

| Description                            | Tags             | Fichiers Associés |
| -------------------------------------- | ---------------- | ----------------- |
| Sommeil, insomnie, troubles du sommeil | #sleep           | [[sommeil_sleep]] |
| Hypnotiques, somnifères                | #sleep, #bzd     | [[hypno]]         |
| TCC, thérapies cognitivo-comportementales | #cbt          | [[psychothp_cbt]] |
"""


def _thes() -> Thesaurus:
    return Thesaurus.from_files(ABBR, TAGS)


# --------------------------------------------------------------------------- #
# parsing


def test_abbrev_surface_split_and_meanings():
    rows = {r.surfaces[0]: r for r in parse_abbreviations(ABBR)}
    assert rows["isrs"].surfaces == ["isrs", "ssri"]
    # bilingual parenthetical gloss becomes a synonym phrase
    bpd = rows["BPD"]
    assert "Borderline Personality Disorder" in bpd.meanings
    assert "Trouble de la Personnalité Limite" in bpd.meanings
    # `(var. X)` aside is dropped from meanings but the backticked variant is a surface
    rspr = rows["RSPR"]
    assert "RISPRDNE" in rspr.surfaces
    assert all("var." not in m.lower() for m in rspr.meanings)


def test_ambiguous_and_morphological_flags():
    rows = {r.surfaces[0]: r for r in parse_abbreviations(ABBR)}
    assert rows["CI°"].ambiguous is True
    assert rows["CI°"].expansion_eligible is False
    assert rows["...q"].morphological is True
    assert rows["...q"].expansion_eligible is False
    # prose shorthand with a one-word meaning and no note: ineligible for expansion
    assert rows["svt"].expansion_eligible is False


def test_tag_rows_parsed():
    trows = parse_tags(TAGS)
    descs = {tuple(r.phrases) for r in trows}
    assert ("Sommeil", "insomnie", "troubles du sommeil") in descs


# --------------------------------------------------------------------------- #
# expansion


def test_expand_bidirectional_and_acronym():
    t = _thes()
    # FR meaning → abbreviation + cross-form
    v = t.expand_query("facteurs de risque dépression", 3)
    assert any("EDC" in x or "Épisode Dépressif Caractérisé" in x for x in v)
    # acronym query → expansion (FR full form / cross acronym)
    v2 = t.expand_query("ISRS chez l'adolescent", 3)
    assert any("ssri" in x.lower() or "sérotonine" in x.lower() for x in v2)


def test_expand_substitutes_in_place_not_appends():
    t = _thes()
    v = t.expand_query("dépression périnatale", 3)
    # the matched token is replaced, the rest of the query is preserved verbatim
    assert all("périnatale" in x for x in v)
    assert all(x != "dépression périnatale" for x in v)


def test_expand_accent_and_case_insensitive():
    t = _thes()
    assert t.expand_query("DEPRESSION", 3)  # uppercase, no accent still matches


def test_expand_respects_max_variants_and_excludes_original():
    t = _thes()
    v = t.expand_query("dépression", 1)
    assert len(v) == 1
    assert _norm(v[0]) != _norm("dépression")


def test_expand_skips_collision_rows():
    t = _thes()
    # `CI°` is ≠-flagged; querying its meaning must not resurrect it as a variant
    assert t.expand_query("contre-indication", 3) == [] or all(
        "CI°" not in x for x in t.expand_query("contre-indication", 3)
    )


def test_single_tag_slug_bridges_but_multitag_does_not():
    t = _thes()
    # single-tag #sleep row → "sleep" bridges to the FR phrases …
    v = t.expand_query("sleep disorders", 3)
    assert any("Sommeil" in x or "insomnie" in x for x in v)
    # … and never to bzd (which only co-occurs on the multi-tag hypnotiques row)
    assert all("bzd" not in x.lower() for x in v)


def test_cbt_acronym_bridges_to_french():
    t = _thes()
    v = t.expand_query("CBT for anxiety", 3)
    assert any("TCC" in x or "cognitivo" in x for x in v)


def test_expand_matches_hyphenated_multiword_phrase():
    # The tokeniser-built key makes a hyphenated phrase matchable from query
    # tokens (the old raw-_norm key kept the hyphen and never matched).
    t = _thes()
    v = t.expand_query("thérapies cognitivo-comportementales efficaces", 3)
    assert v, "hyphenated phrase should have matched"
    assert all(x.endswith("efficaces") for x in v)  # rest of query preserved
    assert any("TCC" in x or "cbt" in x for x in v)


def test_expand_no_match_returns_empty():
    t = _thes()
    assert t.expand_query("quantum chromodynamics", 3) == []
    assert t.expand_query("", 3) == []


# --------------------------------------------------------------------------- #
# primer


def test_primer_bounded_and_rendered_full_meaning():
    t = _thes()
    p = t.build_primer("", 1500)
    assert len(p) <= 1500
    assert p.startswith("This knowledge base")
    # full meaning cell is used (the `/` alternative is not truncated away)
    assert "dep = Dépression / Épisode Dépressif Caractérisé" in p


def test_primer_includes_query_matched_rows():
    t = _thes()
    p = t.build_primer("question about isrs dosing", 1500)
    assert "isrs" in p


def test_primer_empty_when_no_budget_or_no_rows():
    t = _thes()
    assert t.build_primer("", 0) == ""
    assert Thesaurus.from_files("", "").build_primer("anything", 1500) == ""


def test_primer_whole_token_no_substring_false_positive():
    # `iv` (Intraveineux) must NOT be pulled in by the substring "iv" inside
    # "survival" — the regression the old `_norm(s) in qn` test had.
    t = _thes()
    p = t.build_primer("survival of the patient", 8000)
    assert not any(line.startswith("iv = ") for line in p.splitlines())
    # but a real whole-token mention DOES match
    p2 = t.build_primer("administration iv du produit", 8000)
    assert any(line.startswith("iv = ") for line in p2.splitlines())


def test_primer_matches_meaning_whole_token():
    # typing the full word (meaning) matches the abbreviation row
    t = _thes()
    p = t.build_primer("risque de dépression", 8000)
    assert any(line.startswith("dep = ") for line in p.splitlines())


def test_primer_query_rows_prioritised_over_core():
    # Under a budget that fits only a couple of lines past the header, a
    # query-relevant row (`iv`, not in the core list) must still appear — proof
    # it is emitted BEFORE the core glossary rather than truncated after it.
    from rag.thesaurus import _PRIMER_HEADER
    t = _thes()
    tight = t.build_primer("administration iv", len(_PRIMER_HEADER) + 40).splitlines()[1:]
    assert any(line.startswith("iv = ") for line in tight), tight


def test_primer_floor_and_header_only_budget():
    # A budget below the header length yields "" (deliberate), 500 yields content.
    from rag.thesaurus import _PRIMER_HEADER
    t = _thes()
    assert t.build_primer("x", len(_PRIMER_HEADER)) == ""
    assert t.build_primer("x", 500) != ""
    assert len(t.build_primer("x", 500)) <= 500


def test_primer_strips_backticks_and_disambiguation_asides():
    # CI° renders without its `(≠ ...)` aside or backticks; the BPD bilingual
    # gloss in parentheses is KEPT.
    t = _thes()
    p = t.build_primer("contre-indication et border", 8000)
    ci = [ln for ln in p.splitlines() if ln.startswith("CI°")]
    assert ci and all("`" not in ln and "≠" not in ln for ln in ci), ci
    bpd = [ln for ln in p.splitlines() if ln.startswith("BPD")]
    assert bpd and "Trouble de la Personnalité Limite" in bpd[0]


# --------------------------------------------------------------------------- #
# Tier 2 generalisation: English headers + configurable primer header/core-terms

# English-labelled equivalents of ABBR/TAGS (column headers translated).
ABBR_EN = """\
| Abbreviation | Meaning                              | Notes |
| ------------ | ------------------------------------ | ----- |
| `cbt`        | Cognitive Behavioural Therapy        | [[cbt]] |
| `dep`        | Depression / Major Depressive Episode | [[dep]] |
"""

TAGS_EN = """\
| Description          | Tag    |
| -------------------- | ------ |
| Sleep, insomnia      | #sleep |
"""


def test_english_headers_are_skipped_not_parsed_as_data():
    rows = parse_abbreviations(ABBR_EN)
    surfaces = {s for r in rows for s in r.surfaces}
    # The header row ("Abbreviation"/"Meaning") must not become a data row.
    assert "Abbreviation" not in surfaces and "cbt" in surfaces
    trows = parse_tags(TAGS_EN)
    descs = {p for r in trows for p in r.phrases}
    assert "Description" not in descs and "Sleep" in descs


def test_primer_header_override_replaces_builtin():
    from rag.thesaurus import _PRIMER_HEADER
    t = _thes()
    custom = "Custom glossary intro:"
    p = t.build_primer("isrs", 1500, header=custom)
    assert p.startswith(custom)
    assert _PRIMER_HEADER not in p
    # Empty/whitespace header falls back to the built-in.
    assert t.build_primer("isrs", 1500, header="   ").startswith(_PRIMER_HEADER)


def test_primer_core_terms_override_changes_priority_selection():
    t = _thes()
    # With a core_terms list naming only `iv`, the no-query core pass surfaces iv
    # (a non-query, non-default-prioritised row) rather than the built-in set.
    p = t.build_primer("", 8000, core_terms=["iv"])
    lines = p.splitlines()[1:]
    assert any(ln.startswith("iv") for ln in lines), lines
    # Empty core_terms falls back to the built-in priority list.
    assert t.build_primer("", 8000, core_terms=[]) == t.build_primer("", 8000)


def test_coerce_vault_rel_file_shape():
    from api.routes.config import _coerce_vault_rel_file as c
    assert c("") == ""                       # empty kept (slot disabled)
    assert c("_abreviations.md") == "_abreviations.md"
    assert c("glossary/_tags.md") == "glossary/_tags.md"
    assert c("sub\\win.md") == "sub/win.md"  # backslashes normalised
    assert c("../escape.md") is None         # traversal rejected
    assert c("/abs/path.md") is None         # absolute rejected
    assert c("a/../../b.md") is None         # interior traversal rejected
    assert c("with\x00nul.md") is None       # NUL rejected
    assert c(123) is None                    # non-string rejected


def test_get_thesaurus_honours_configured_paths(tmp_path, monkeypatch):
    """Tier 1: the loader reads the config-driven vault-relative paths, not the
    hard-coded defaults, and a config change busts the signature cache."""
    import rag.vault as rv
    mgr = rv.obsidian_manager
    # Custom-named/located glossary file; the default `_abreviations.md` is absent.
    (tmp_path / "glossary").mkdir()
    (tmp_path / "glossary" / "abbr.md").write_text(ABBR, encoding="utf-8")
    (tmp_path / "_tags.md").write_text(TAGS, encoding="utf-8")

    cfg = {
        "vault_thesaurus_abbrev_path": "glossary/abbr.md",
        "vault_thesaurus_tags_path": "_tags.md",
    }
    monkeypatch.setattr(rv, "load_config", lambda: cfg)
    # _get_thesaurus now reads via load_config_readonly (W7b, no-deepcopy path).
    monkeypatch.setattr(rv, "load_config_readonly", lambda: cfg)
    monkeypatch.setattr(mgr, "_vault_path", str(tmp_path))
    monkeypatch.setattr(mgr, "_thesaurus", None)
    monkeypatch.setattr(mgr, "_thesaurus_cache_key", None)

    thes = mgr._get_thesaurus()
    assert thes is not None and thes.expand_query("isrs dosing"), "custom path not loaded"

    # Point the abbrev path at a non-existent file → only tags remain (still a
    # thesaurus, but the abbrev surfaces are gone) and the cache is busted.
    cfg["vault_thesaurus_abbrev_path"] = "does_not_exist.md"
    thes2 = mgr._get_thesaurus()
    assert thes2 is not None and thes2 is not thes  # cache busted by path change
    assert not any("isrs" in s for r in thes2.abbrev_rows for s in r.surfaces)

    # Disabling both slots → None (degrade to no expansion / no primer).
    cfg["vault_thesaurus_abbrev_path"] = ""
    cfg["vault_thesaurus_tags_path"] = ""
    assert mgr._get_thesaurus() is None


def test_get_thesaurus_rejects_traversal_path(tmp_path, monkeypatch):
    """A traversal/absolute configured path resolves to None (not read)."""
    import rag.vault as rv
    mgr = rv.obsidian_manager
    (tmp_path / "_tags.md").write_text(TAGS, encoding="utf-8")
    cfg = {
        "vault_thesaurus_abbrev_path": "../../etc/passwd",
        "vault_thesaurus_tags_path": "_tags.md",
    }
    monkeypatch.setattr(rv, "load_config", lambda: cfg)
    # _get_thesaurus now reads via load_config_readonly (W7b, no-deepcopy path).
    monkeypatch.setattr(rv, "load_config_readonly", lambda: cfg)
    monkeypatch.setattr(mgr, "_vault_path", str(tmp_path))
    monkeypatch.setattr(mgr, "_thesaurus", None)
    monkeypatch.setattr(mgr, "_thesaurus_cache_key", None)
    thes = mgr._get_thesaurus()
    # Tags still load; the traversal abbrev path contributed nothing.
    assert thes is not None
    assert not thes.abbrev_rows
