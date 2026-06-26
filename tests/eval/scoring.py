"""Pure scoring + reporting for the vault-chat golden-set eval.

This module is deliberately model-free and import-light: it knows how to load
golden pairs, score a produced answer against one, and format a report. The
live wiring that actually calls a provider lives in ``run_eval.py`` so this
half can be unit-tested hermetically (see ``test_scoring.py``).

A "golden pair" is a fixed question over the bundled fixture vault plus the
properties a *grounded* answer must have:

* ``must_cite``      — substrings that should appear in the answer (the source
                       filenames the answer is expected to cite).
* ``must_contain``   — substrings the answer must include (the actual fact).
* ``must_not_contain`` — substrings that must be ABSENT — used to catch
                       ungrounded/parametric answers (e.g. answering a question
                       the fixtures do not support).

All matching is case-insensitive and substring-based on purpose: it is a
regression *tripwire* for prompt changes, not a semantic grader. The signal is
the pass-rate *delta* between two prompt revisions, not the absolute score.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass(frozen=True)
class GoldenPair:
    id: str
    query: str
    prompt_mode: str = "strict"
    must_cite: List[str] = field(default_factory=list)
    must_contain: List[str] = field(default_factory=list)
    must_not_contain: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class PairResult:
    id: str
    passed: bool
    missing_citations: List[str]
    missing_content: List[str]
    forbidden_present: List[str]
    answer: str = ""


def load_pairs(path: str | Path) -> List[GoldenPair]:
    """Load golden pairs from a JSON file (list of objects)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    pairs: List[GoldenPair] = []
    for obj in data:
        pairs.append(GoldenPair(
            id=str(obj["id"]),
            query=str(obj["query"]),
            prompt_mode=str(obj.get("prompt_mode", "strict")),
            must_cite=list(obj.get("must_cite", [])),
            must_contain=list(obj.get("must_contain", [])),
            must_not_contain=list(obj.get("must_not_contain", [])),
        ))
    return pairs


def score_answer(answer: str, pair: GoldenPair) -> PairResult:
    """Score *answer* against *pair*. A pair passes only when every required
    citation and content term is present and no forbidden term is.

    Matching is asymmetric on purpose:

    * ``must_cite`` / ``must_contain`` use plain case-insensitive *substring*
      matching. A citation token like ``heart_failure`` is expected to appear
      verbatim (underscore and all) inside whatever bracketed filename the model
      emits, e.g. ``[heart_failure.md]``; a substring test is both sufficient
      and robust to the surrounding ``[ ]`` / ``.md`` punctuation. A spurious
      *match* here is harmless (it only makes a pass easier to earn).
    * ``must_not_contain`` uses *word-boundary* matching. A spurious match here
      is NOT harmless — it would flip a correctly-grounded answer to FAIL — and
      a bare substring test mis-fires on incidental infixes (the forbidden token
      ``paris`` is literally a substring of the innocent word ``comparison``).
      ``\\b`` anchors require the forbidden term to occur as a whole word.
    """
    low = (answer or "").lower()
    missing_citations = [c for c in pair.must_cite if c.lower() not in low]
    missing_content = [c for c in pair.must_contain if c.lower() not in low]
    # Word-boundary match: a forbidden token only trips when it appears as a
    # standalone word, never as an accidental infix of an unrelated word
    # (paris ⊂ comparison). re.escape keeps multi-word/punctuated tokens literal.
    forbidden_present = [
        c for c in pair.must_not_contain
        if re.search(rf"\b{re.escape(c)}\b", answer or "", flags=re.IGNORECASE)
    ]
    passed = not (missing_citations or missing_content or forbidden_present)
    return PairResult(
        id=pair.id,
        passed=passed,
        missing_citations=missing_citations,
        missing_content=missing_content,
        forbidden_present=forbidden_present,
        answer=answer or "",
    )


def format_report(results: List[PairResult]) -> str:
    """Render a compact human-readable report ending with the pass rate."""
    lines: List[str] = []
    passed = 0
    for r in results:
        if r.passed:
            passed += 1
            lines.append(f"PASS  {r.id}")
            continue
        reasons = []
        if r.missing_citations:
            reasons.append(f"missing citation(s): {r.missing_citations}")
        if r.missing_content:
            reasons.append(f"missing content: {r.missing_content}")
        if r.forbidden_present:
            reasons.append(f"forbidden present: {r.forbidden_present}")
        lines.append(f"FAIL  {r.id} — " + "; ".join(reasons))
    total = len(results)
    rate = (passed / total * 100.0) if total else 0.0
    lines.append("")
    lines.append(f"{passed}/{total} passed ({rate:.0f}%)")
    return "\n".join(lines)


def pass_rate(results: List[PairResult]) -> float:
    total = len(results)
    if not total:
        return 0.0
    return sum(1 for r in results if r.passed) / total
