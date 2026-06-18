"""Filename quality scoring. Lower score = cleaner."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

_DUP_SUFFIX_RE = re.compile(r" \(\d+\)$| copy$|_\d{1,3}$", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?:^|[\s_\-])(19\d{2}|20\d{2})(?:$|[\s_\-])")
_VOWEL_RE = re.compile(r"[aeiouy]", re.IGNORECASE)


def score(name: str) -> float:
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
    names = list(filenames)
    if not names:
        raise ValueError("empty filenames")
    return min(names, key=lambda x: (score(x), x.lower()))
