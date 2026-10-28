"""Phase 7 (2026-09-16): suffixes are clues, the comparison names what blocks a
removal, and the removal itself needs an explicit acknowledgement."""
import json
import os
from unittest.mock import patch

import pytest

from accession import duplicates, pdf_management as pm, review
from test_pdf_management import _readable_annotation_counts, apply, copies as _copies, proposal  # noqa: F401 — autouse
from test_pdf_reconciliation import world as _world

world = _world

copies = _copies


@pytest.mark.parametrize("name,expected", [
    ("smith_2020.pdf", ("smith_2020", None, None)),
    ("smith_2020_1.pdf", ("smith_2020", "underscore", 1)),
    ("smith_2020 2.pdf", ("smith_2020", "space", 2)),
    ("smith_2020 (1).pdf", ("smith_2020", "parenthesised", 1)),
    ("smith_2020-3.pdf", ("smith_2020", "hyphen", 3)),
    ("smith_2020 copy.pdf", ("smith_2020", "copy", None)),
    ("smith_2020 copy 2.pdf", ("smith_2020", "copy", 2)),
    ("Smith_2020_Copy.pdf", ("smith_2020", "copy", None)),
])
def test_variant_stem_recognises_collision_suffixes_but_not_years(name, expected):
    assert duplicates.variant_stem(name) == expected


def test_group_candidates_needs_two_members():
    groups = duplicates.group_candidates(["a_2020.pdf", "a_2020_1.pdf", "b_2019.pdf", "A_2020 copy.pdf"])
    assert groups == {"a_2020": ["A_2020 copy.pdf", "a_2020.pdf", "a_2020_1.pdf"]}


def test_compare_names_every_blocking_fact_and_never_decides():
    rows = duplicates.compare([
        dict(rel="x.pdf", sha256="s", native_annotations=2, embedded_annotations=0, note_dependencies=[], mapping_keys=["k"]),
        dict(rel="x_1.pdf", sha256="s", native_annotations=0, embedded_annotations=3, note_dependencies=["n.md"], mapping_keys=["k", "j"]),
        dict(rel="x_2.pdf", sha256=None, native_annotations=0, embedded_annotations=0, note_dependencies=[], mapping_keys=["k"]),
    ])
    by = {r["rel"]: r for r in rows}
    assert by["x.pdf"]["identical_to"] == ["x_1.pdf"] and by["x.pdf"]["unknown_to"] == ["x_2.pdf"]
    assert by["x.pdf"]["blocking"] == ["2 native Zotero annotation(s) on its attachment(s)"]
    assert by["x_1.pdf"]["variant"] == "underscore" and len(by["x_1.pdf"]["blocking"]) == 3
    assert by["x_2.pdf"]["removable_without_acknowledgement"] and by["x_2.pdf"]["variant"] == "underscore"
    # The comparison informs and never decides: no row carries an action or an
    # applicability verdict (the old `not r.get("applicable")` was vacuous —
    # the key never exists; 2026-09-17 review, E2).
    assert all(isinstance(r["keep_hint"], str) and not ({"applicable", "action", "executable"} & set(r)) for r in rows)


def test_entry_reports_comparison_facts(copies):
    s, keep, remove = copies
    data, _ = pm.entry(s, "KeyOne")
    rows = {r["rel"]: r for r in data["rows"]}
    assert rows["biblio_articles/redundant.pdf"]["comparison"]["identical_to"] == ["biblio_articles/one.pdf"]
    assert rows["biblio_articles/one.pdf"]["native_annotations"] == 0
    assert "annotations" in data["removal_rule"]


def test_annotated_copy_is_kept_unless_acknowledged(copies):
    s, keep, remove = copies
    with patch("audit.core.pdf_annotations.count_annotations", return_value=4):
        p = proposal(copies)
        assert not p["executable"]
        assert any("carries 4 annotation(s)" in b for b in p["blockers"])
        with pytest.raises(ValueError, match="annotation"):
            apply(copies, p)
        assert remove.exists()
        acknowledged = pm.preview(s, "KeyOne", "biblio_articles/one.pdf", {"biblio_articles/redundant.pdf": "delete"},
                                  acknowledge_annotations=True)
        assert acknowledged["executable"] and acknowledged["plan"]["acknowledge_annotations"]
        assert acknowledged["token"] != p["token"]
        # The acknowledgement rides the plan digest: an apply without it cannot reuse the token.
        with pytest.raises(review.ReviewConflict):
            pm.apply(s, "KeyOne", "biblio_articles/one.pdf", {"biblio_articles/redundant.pdf": "delete"},
                     acknowledged["token"], confirm=True)
        op = pm.apply(s, "KeyOne", "biblio_articles/one.pdf", {"biblio_articles/redundant.pdf": "delete"},
                      acknowledged["token"], confirm=True, acknowledge_annotations=True)
    assert op["status"] == "complete" and not remove.exists()
    assert pm.restore(s, op["id"], confirm=True)["status"] == "restored" and remove.exists()


def test_concurrent_rename_between_preview_and_apply_is_refused(copies):
    """Attanger (or Zotero's rename preference) renames the copy after the
    preview: the plan's identity no longer resolves, nothing is removed and
    the renamed file stays where the plugin put it."""
    s, keep, remove = copies
    p = proposal(copies)
    renamed = remove.with_name("redundant_renamed_by_attanger.pdf")
    os.rename(remove, renamed)
    with pytest.raises((review.ReviewConflict, ValueError)):
        apply(copies, p)
    assert renamed.exists() and not remove.exists()
    assert json.loads(s.mapping_file.read_text())["matches"]["KeyOne"] == ["biblio_articles/one.pdf", "biblio_articles/redundant.pdf"]


def test_unknown_annotation_count_blocks_removal_and_cannot_be_acknowledged(copies):
    """2026-09-17 review, B3: count_annotations' −1 sentinel was coerced to 0,
    so an unreadable copy looked annotation-free and deletable."""
    s, keep, remove = copies
    rel = remove.relative_to(s.vault_root).as_posix()
    with patch("audit.core.pdf_annotations.count_annotations", return_value=-1):
        for acknowledge in (False, True):
            proposed = pm.preview(s, "KeyOne", keep.relative_to(s.vault_root).as_posix(), {rel: "delete"},
                                  acknowledge_annotations=acknowledge)
            assert not proposed["executable"]
            assert any("annotation count unknown" in b for b in proposed["blockers"])
        data, _ = pm.entry(s, "KeyOne")
    row = {r["rel"]: r for r in data["rows"]}[rel]
    assert row["embedded_annotations"] is None
    assert row["comparison"]["embedded_annotations"] is None
    assert not row["comparison"]["removable_without_acknowledgement"]
