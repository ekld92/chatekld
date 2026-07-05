"""Advisory cross-note dose discrepancy check (heuristic, deterministic).

Phase 1 ships the deterministic foundation the design (§9) calls for: extract
``(subject, dose, line)`` per note, group by normalized subject, and flag a
subject mentioned in ≥2 notes whose dose values span a wide ratio — a likely
transcription slip or genuine inconsistency. **Advisory only, never auto-edits**,
and high-false-positive by nature (subject detection is heuristic). A richer
LLM atomic-claim extractor was considered in the design but not built; this
deterministic check is the shipped implementation.
"""
from __future__ import annotations

import re
from collections import defaultdict

from refactor.result import Discrepancy, DoseOccurrence

# A dose mention: number + unit, optionally with a per-something suffix.
_DOSE_RE = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*(mg|µg|mcg|g)\b(?:\s*/\s*(?:j|jour|kg|m2|m²|h|24\s*h))?",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

_UNIT_TO_MG = {"g": 1000.0, "mg": 1.0, "µg": 0.001, "mcg": 0.001}

# Flag when the max/min normalized dose for a subject reaches this ratio.
DISCREPANCY_RATIO = 3.0


def _to_mg(value_str: str, unit: str):
    """Convert *value_str* in *unit* to milligrams; ``None`` on an unknown unit/value.

    Accepts a comma decimal separator (French notes write ``2,5 mg``). Returning
    ``None`` rather than raising lets ``extract_doses`` keep an un-normalizable
    dose as a textual occurrence the report can still display.
    """
    factor = _UNIT_TO_MG.get(unit.lower())
    if factor is None:
        return None
    try:
        return float(value_str.replace(",", ".")) * factor
    except ValueError:
        return None


def normalize_subject(subject: str) -> str:
    """Lowercase, strip markdown/punctuation, collapse whitespace."""
    s = subject.lower().strip()
    s = re.sub(r"[*_`#:>\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_doses(note_rel: str, text: str) -> list[tuple[str, DoseOccurrence]]:
    """Return ``(subject_key, DoseOccurrence)`` for every dose in *text*.

    Subject = a bold term on the dose's own line if present, else the nearest
    preceding heading. Empty subjects are returned with key "" and dropped by
    ``cross_check`` (they cannot be grouped meaningfully).
    """
    out: list[tuple[str, DoseOccurrence]] = []
    current_heading = ""
    for i, line in enumerate(text.splitlines(), start=1):
        h = _HEADING_RE.match(line)
        if h:
            current_heading = h.group(1).strip()
            continue
        for m in _DOSE_RE.finditer(line):
            # Subject = the FIRST bold term on the line (even if it sits after
            # the dose) else the nearest preceding heading. This is intentionally
            # crude: it can mis-attribute a dose when a line has an unrelated bold
            # term, and ``mg/j`` (per-day) normalizes to the same mg value as an
            # absolute dose. That is why the whole report is advisory/never-edits
            # — it surfaces candidates for a human to check, not facts.
            bold = _BOLD_RE.search(line)
            subject = bold.group(1).strip() if bold else current_heading
            out.append((
                normalize_subject(subject),
                DoseOccurrence(
                    note=note_rel,
                    line=i,
                    dose=m.group(0).strip(),
                    value_mg=_to_mg(m.group(1), m.group(2)),
                ),
            ))
    return out


def cross_check(all_doses: list[tuple[str, DoseOccurrence]]) -> list[Discrepancy]:
    """Flag subjects appearing in ≥2 notes whose dose spread ratio ≥ threshold."""
    by_subject: dict[str, list[DoseOccurrence]] = defaultdict(list)
    for key, occ in all_doses:
        if key:
            by_subject[key].append(occ)

    discrepancies: list[Discrepancy] = []
    for subject, occs in sorted(by_subject.items()):
        notes = {o.note for o in occs}
        if len(notes) < 2:
            continue
        vals = [o.value_mg for o in occs if o.value_mg and o.value_mg > 0]
        if len(vals) < 2:
            continue
        lo, hi = min(vals), max(vals)
        if hi / lo >= DISCREPANCY_RATIO:
            discrepancies.append(Discrepancy(
                subject=subject,
                reason=f"dose spread {lo:g}–{hi:g} mg across {len(notes)} notes",
                occurrences=sorted(occs, key=lambda o: (o.note, o.line)),
            ))
    return discrepancies
