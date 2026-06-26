"""Zero-vision "likely a table" hint derived from the existing cached prose.

The indexer's cached descriptions are prose, not tables, so we cannot *reuse* a
table — but we can cheaply flag which already-described images are *worth* a
user-triggered table extraction, without spending a single vision call. Pure
heuristic over the cached text; advisory only (drives a UI badge).
"""
from __future__ import annotations

import re

# A dose-ish "<number> <unit>" token (looser than the discrepancy regex on
# purpose — here we only count density, not normalize).
_DOSE_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:mg|µg|mcg|g)\b", re.I)

# Words that, in a description, strongly imply the image is a table/grid. Mixed
# FR/EN because the notes are French but the vision model replies in either.
_TABLE_WORDS = (
    "tableau", "colonne", "colonnes", "ligne du tableau", "posolog",
    "table of", "columns", "rows", "spreadsheet", "grid",
)


def likely_table(description: str) -> tuple[bool, str]:
    """Return ``(is_likely_table, human_reason)`` for a cached description."""
    if not description:
        return (False, "")
    reasons: list[str] = []

    # The vision model sometimes already renders a table-ish structure as
    # pipe-delimited rows in its prose.
    pipe_rows = sum(1 for ln in description.splitlines() if ln.count("|") >= 2)
    if pipe_rows >= 2:
        reasons.append(f"{pipe_rows} pipe-delimited rows")

    low = description.lower()
    hit_words = sorted({w for w in _TABLE_WORDS if w in low})
    if hit_words:
        reasons.append("mentions " + ", ".join(hit_words))

    dose_hits = len(_DOSE_TOKEN_RE.findall(description))
    if dose_hits >= 3:
        reasons.append(f"{dose_hits} dose values")

    return (bool(reasons), "; ".join(reasons))
