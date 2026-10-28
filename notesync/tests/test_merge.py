"""``notesync.merge`` — the three-way line merge behind the conflict editor."""

from __future__ import annotations

from notesync.merge import OURS_MARK, SEP_MARK, THEIRS_MARK, has_conflict_markers, three_way

BASE = "a\nb\nc\nd\ne"


def test_no_change_is_identity():
    assert three_way(BASE, BASE, BASE) == (BASE, 0)


def test_one_side_edit_applies_cleanly():
    assert three_way(BASE, "a\nB\nc\nd\ne", BASE) == ("a\nB\nc\nd\ne", 0)
    assert three_way(BASE, BASE, "a\nb\nc\nD\ne") == ("a\nb\nc\nD\ne", 0)


def test_disjoint_edits_from_both_sides_both_apply():
    merged, n = three_way(BASE, "a\nB\nc\nd\ne", "a\nb\nc\nD\ne")
    assert (merged, n) == ("a\nB\nc\nD\ne", 0)


def test_identical_edits_apply_once():
    merged, n = three_way(BASE, "a\nX\nc\nd\ne", "a\nX\nc\nd\ne")
    assert (merged, n) == ("a\nX\nc\nd\ne", 0)


def test_overlapping_edits_become_one_conflict_block():
    merged, n = three_way(BASE, "a\nMINE\nc\nd\ne", "a\nTHEIRS\nc\nd\ne")
    assert n == 1
    assert merged.split("\n") == ["a", OURS_MARK, "MINE", SEP_MARK, "THEIRS", THEIRS_MARK, "c", "d", "e"]
    assert has_conflict_markers(merged)


def test_insertions_at_the_same_point_conflict_unless_identical():
    merged, n = three_way("a\nb", "a\nx\nb", "a\ny\nb")
    assert n == 1 and OURS_MARK in merged
    merged, n = three_way("a\nb", "a\nx\nb", "a\nx\nb")
    assert (merged, n) == ("a\nx\nb", 0)


def test_insertions_at_different_points_both_apply():
    assert three_way("a\nb\nc", "x\na\nb\nc", "a\nb\nc\ny") == ("x\na\nb\nc\ny", 0)


def test_deletion_versus_edit_conflicts():
    merged, n = three_way(BASE, "a\nc\nd\ne", "a\nB\nc\nd\ne")
    assert n == 1
    lines = merged.split("\n")
    assert lines[1] == OURS_MARK and "B" in lines and THEIRS_MARK in lines


def test_crlf_is_normalised_and_empty_sides_are_fine():
    assert three_way("a\r\nb", "a\r\nb", "a\r\nb") == ("a\nb", 0)
    assert three_way("", "mine", "") == ("mine", 0)
    assert three_way("", "", "theirs") == ("theirs", 0)
    merged, n = three_way("", "mine", "theirs")
    assert n == 1


def test_adjacent_but_non_overlapping_changes_do_not_conflict():
    # ours changes line 2, theirs changes line 3: touching, not overlapping.
    merged, n = three_way("a\nb\nc\nd", "a\nB\nc\nd", "a\nb\nC\nd")
    assert (merged, n) == ("a\nB\nC\nd", 0)
