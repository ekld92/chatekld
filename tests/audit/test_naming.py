"""Coverage for the filename-quality scorer (ported from kb_harmonizer)."""

from __future__ import annotations

import pytest

from audit.core import naming


def test_year_lowers_score() -> None:
    assert naming.score("smith_2020") < naming.score("smith")


def test_dup_suffix_raises_score() -> None:
    assert naming.score("smith_2020 (1)") > naming.score("smith_2020")
    assert naming.score("smith_2020_1") > naming.score("smith_2020")
    assert naming.score("smith copy") > naming.score("smith")


def test_long_with_many_dashes_penalized() -> None:
    hashlike = "a-1b-2c-3d-4e-5f-6g-7h"
    plain = "smith_jones_2020"
    assert naming.score(hashlike) > naming.score(plain)


def test_high_digit_ratio_penalized() -> None:
    assert naming.score("123456_2020") > naming.score("alpha_2020")


def test_no_vowels_penalized() -> None:
    assert naming.score("bcdfgh") > naming.score("bcdfgha")


def test_get_cleanest_name_picks_best() -> None:
    candidates = ["smith_2020", "smith_2020 (1)", "smith_2020_1"]
    assert naming.get_cleanest_name(candidates) == "smith_2020"


def test_get_cleanest_name_stable_tiebreak() -> None:
    assert naming.get_cleanest_name(["B", "a"]) == "a"


def test_get_cleanest_name_rejects_empty() -> None:
    with pytest.raises(ValueError):
        naming.get_cleanest_name([])
