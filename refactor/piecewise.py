"""Piecewise (section-by-section) LLM edits for over-cap notes (Batch D).

The 422 truncation guard in ``api/routes/refactor.py`` refuses any LLM edit
whose target span exceeds ``llm_edit.REWRITE_MAX_CHARS`` — the model would see
(and rewrite) only the head, and the sha guards would certify the loss. Its
error text tells the user to "edit one section at a time"; this module is the
app doing exactly that on their behalf, offered at the moment of refusal
(maintainer decision 2026-07-22: proposed, never automatic).

Pure driver — no HTTP, no LLM import, no filesystem: the route supplies
``run_edit`` (one bounded LLM call over one section's text) and receives a
combined whole-note proposal built by splicing each edited section back
**back-to-front** over the original coordinates (``sections.replace_section``
is line-anchored, so replacing a LATER section never invalidates an EARLIER
section's span — the reverse order is what keeps every splice byte-identical
outside its own span). The combined proposal then rides the exact same
staging + stale-diff + WYSIWYG + journal path as a single-call edit.

Sections that are individually over the cap are skipped WITH a reason (never
silently clipped — the same invariant the 422 guard protects); a section whose
edit fails is skipped-with-reason too, so one provider blip degrades one
section instead of discarding the run (the deck generator's per-section
resilience philosophy).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from refactor import sections as sections_mod


@dataclass
class SectionOutcome:
    """What happened to one section during a piecewise run."""
    index: int
    title: str
    status: str          # edited | unchanged | skipped_too_large | skipped_empty | failed | cancelled
    detail: str = ""

    def to_jsonable(self) -> dict:
        return {"index": self.index, "title": self.title,
                "status": self.status, "detail": self.detail}


@dataclass
class PiecewiseResult:
    proposed: str                      # the combined whole-note proposal
    outcomes: list[SectionOutcome]
    edited_count: int
    error: Optional[str] = None        # set when NOTHING could be edited
    model: str = ""                    # first successful edit's model/provider
    provider: str = ""                 # (display only — sections share cfg)


def piecewise_edit(
    text: str,
    *,
    run_edit: Callable[[str], dict],
    max_chars: int,
    should_cancel: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> PiecewiseResult:
    """Edit every eligible section of *text* independently and recombine.

    *run_edit(section_text)* must return the ``llm_edit``-shaped dict
    ``{"text": str, "error": str|None, "truncated": bool}``. *max_chars* is
    the per-section input cap (the route passes ``REWRITE_MAX_CHARS``).
    Cancellation is checked between sections; a cancelled run returns the
    sections edited so far marked plus ``cancelled`` outcomes for the rest
    and ``error`` set (the caller should NOT stage a half-run).
    """
    secs = sections_mod.split_sections(text)
    outcomes: list[SectionOutcome] = []
    edits: list[tuple[sections_mod.Section, str]] = []
    cancelled = False
    model = ""
    provider = ""

    for i, sec in enumerate(secs):
        if cancelled or (should_cancel is not None and should_cancel()):
            cancelled = True
            outcomes.append(SectionOutcome(sec.index, sec.title, "cancelled"))
            continue
        body = sections_mod.slice_section(text, sec)
        if not body.strip():
            outcomes.append(SectionOutcome(sec.index, sec.title, "skipped_empty"))
            continue
        if len(body) > max_chars:
            outcomes.append(SectionOutcome(
                sec.index, sec.title, "skipped_too_large",
                f"{len(body):,} chars > {max_chars:,} — split it under smaller "
                "headings to include it."))
            continue
        if progress_cb is not None:
            try:
                progress_cb(i + 1, len(secs), sec.title)
            except Exception:  # noqa: BLE001 — progress must never kill the run
                pass
        try:
            res = run_edit(body)
        except Exception as exc:  # noqa: BLE001 — degrade to a per-section skip
            outcomes.append(SectionOutcome(
                sec.index, sec.title, "failed", exc.__class__.__name__))
            continue
        if res.get("error"):
            outcomes.append(SectionOutcome(
                sec.index, sec.title, "failed", str(res["error"])[:200]))
            continue
        new_body = res.get("text") or ""
        if not new_body.strip():
            outcomes.append(SectionOutcome(
                sec.index, sec.title, "failed", "empty model reply"))
            continue
        if new_body == body:
            outcomes.append(SectionOutcome(sec.index, sec.title, "unchanged"))
            continue
        edits.append((sec, new_body))
        outcomes.append(SectionOutcome(sec.index, sec.title, "edited"))
        if len(edits) == 1:  # first success wins (every section shares cfg)
            model = str(res.get("model") or "")
            provider = str(res.get("provider") or "")

    if cancelled:
        return PiecewiseResult(text, outcomes, 0, error="cancelled")
    if not edits:
        statuses = {o.status for o in outcomes}
        if statuses <= {"skipped_too_large", "skipped_empty"}:
            return PiecewiseResult(text, outcomes, 0, error=(
                "Every section is individually over the per-section cap — "
                "split them under smaller headings first."))
        if "failed" in statuses and "edited" not in statuses:
            return PiecewiseResult(text, outcomes, 0,
                                   error="Every section edit failed.")
        return PiecewiseResult(text, outcomes, 0, error=None)  # all unchanged

    # Back-to-front splice over ORIGINAL coordinates (see module docstring).
    combined = text
    for sec, new_body in sorted(edits, key=lambda e: e[0].index, reverse=True):
        combined = sections_mod.replace_section(combined, sec, new_body)
    return PiecewiseResult(combined, outcomes, len(edits),
                           model=model, provider=provider)
