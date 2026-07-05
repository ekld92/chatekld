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

# Words that, in a description, strongly imply the image is handwritten — the
# indexer's descriptions self-identify ("This image is a handwritten page …"),
# so a cheap substring scan flags them with zero vision. Mixed FR/EN like the
# table words. Advisory: drives the auto-hide of an OCR callout the local model
# could not reliably read, which the user can override per-image ("Keep anyway").
_HANDWRITTEN_WORDS = (
    "handwritten", "hand-written", "hand written", "handwriting",
    "hand-drawn", "hand drawn", "handdrawn",
    "manuscrit", "manuscrite", "écriture manuscrite", "ecriture manuscrite",
    "écrit à la main", "ecrit a la main", "écrits à la main",
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


def likely_handwritten(description: str) -> tuple[bool, str]:
    """Return ``(is_likely_handwritten, human_reason)`` for a cached description.

    Pure substring heuristic over the existing prose — no vision call. False
    positives are possible (a printed page whose description merely *mentions*
    handwriting), which is exactly why the auto-hide it drives is reversible via
    the per-image "Keep anyway" override (``flags.keep_handwritten``).
    """
    if not description:
        return (False, "")
    low = description.lower()
    hits = sorted({w for w in _HANDWRITTEN_WORDS if w in low})
    if hits:
        return (True, "mentions " + ", ".join(hits))
    return (False, "")
