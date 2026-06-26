"""Filename quality scoring. Lower score = cleaner."""

# A heuristic used to pick the most human-readable name among a set of
# content-identical (duplicate) PDFs — the canonical one a user would keep.
# The score is a sum of penalties (a copy suffix, a long hash-like hyphenated
# stem, a digit-heavy or vowel-less stem, raw length) minus a bonus for
# carrying a plausible publication year. Tuned to taste, not derived from
# anything formal; the absolute number is meaningless, only the ranking is used.

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

_DUP_SUFFIX_RE = re.compile(r" \(\d+\)$| copy$|_\d{1,3}$", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?:^|[\s_\-])(19\d{2}|20\d{2})(?:$|[\s_\-])")
_VOWEL_RE = re.compile(r"[aeiouy]", re.IGNORECASE)


def score(name: str) -> float:
    """Penalty score for a filename — lower is cleaner / more keepable.

    Operates on the stem only (extension ignored). Penalties: a duplicate
    suffix like `` (1)``/`` copy``/``_3`` (+100); a long, heavily hyphenated
    stem that looks like a slug or hash (+200); a digit-dense stem after the
    year is removed (+150); a vowel-less stem (+100, catches encoded/garbled
    names); plus 0.1 per character to break ties toward shorter names. A bonus
    (−50) rewards a stem that contains a 19xx/20xx year. The year is excluded
    from the digit-density test so a normal ``author_2020`` name isn't punished.
    """
    base = Path(name).stem
    s = 0.0
    if _DUP_SUFFIX_RE.search(base):
        s += 100
    year_match = _YEAR_RE.search(base)
    if year_match:
        s -= 50
    if len(base) > 20 and base.count("-") >= 3:
        s += 200
    text_only = base
    if year_match:
        text_only = text_only.replace(year_match.group(1), "")
    digits = sum(c.isdigit() for c in text_only)
    letters = sum(c.isalpha() for c in text_only)
    if digits > 0 and (digits / (digits + letters + 0.1)) > 0.4:
        s += 150
    if not _VOWEL_RE.search(base) and len(base) > 4:
        s += 100
    s += len(base) * 0.1
    return s


def get_cleanest_name(filenames: Iterable[str]) -> str:
    """Pick the lowest-scoring (cleanest) name; ties broken alphabetically.

    Raises ``ValueError`` on an empty input. The secondary ``x.lower()`` sort
    key makes the choice deterministic when two names score equally.
    """
    names = list(filenames)
    if not names:
        raise ValueError("empty filenames")
    return min(names, key=lambda x: (score(x), x.lower()))
