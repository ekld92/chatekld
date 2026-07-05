"""Outline phase: ask the agent for a lecture outline and parse it robustly.

The model is asked for a JSON array, but ChatEKLD's prompt template means we
cannot guarantee pure JSON output. ``parse_outline`` therefore tries JSON first
(extracting the first balanced ``[...]`` span) and falls back to parsing a
markdown/numbered heading+bullet list before giving up.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from .prompts import OUTLINE_SYSTEM_PROMPT, build_outline_message

# NOTE: ``deckgen.client`` (which imports ``requests``) is imported lazily inside
# request_outline so that parse_outline — and the test-suite — can use this module
# without the HTTP dependency installed. Type hints below are strings thanks to
# ``from __future__ import annotations``.
if TYPE_CHECKING:  # type-only — never imported at runtime, so the core stays requests-free
    from .client import ChatEKLDClient
    from .result import ChatResult


@dataclass
class Section:
    """One outline section: a slide-group title plus its bullet points.

    The unit the per-section generation phase (``sections.generate_section``)
    consumes — one ``Section`` becomes one ``\\section`` worth of frames.
    """
    title: str
    points: list = field(default_factory=list)


class OutlineError(RuntimeError):
    """Raised when no usable outline could be parsed from the model output."""


# Fallback-parser line forms (checked in priority order; see _parse_heading_list).
_ATX_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")                         # "## Title"
_SECTION_WORD_RE = re.compile(                                         # "Section 1: Title"
    r"^\s*(?:[Ss]ection|[Pp]art|[Tt]opic)\s+\d+\s*[:.\-]\s*(.+?)\s*$"
)
_NUM_RE = re.compile(r"^(\s*)\d+[.)]\s+(.+?)\s*$")                      # "1. Title" / "1) Title"
_DASH_RE = re.compile(r"^(\s*)[-*+]\s+(.+?)\s*$")                       # "- bullet"


def _looks_like_outline(text: str) -> bool:
    """True if *text* contains explicit outline structure.

    Gates the instructions-as-outline fallback in :func:`request_outline`: we
    only treat the lecturer's free-text instructions as an outline when they
    actually typed one — an ATX heading, a "Section N:" line, a top-level
    numbered item, or a ``-``/``*``/``+`` bullet. Plain prose returns ``False``
    so the flat-list branch of :func:`_parse_heading_list` cannot turn each
    sentence line into a bogus section.
    """
    for ln in text.splitlines():
        if not ln.strip():
            continue
        if _ATX_RE.match(ln) or _SECTION_WORD_RE.match(ln) or _DASH_RE.match(ln):
            return True
        m = _NUM_RE.match(ln)
        if m and len(m.group(1)) == 0:  # top-level numbered item (not an indented point)
            return True
    return False


def _emit_info(on_event, text: str) -> None:
    """Push one SSE-shaped ``{"info": …}`` event, defensively.

    The deck route forwards these dicts straight onto its SSE queue and the CLI
    prints them; a listener exception must never abort outline generation.
    """
    if on_event is None:
        return
    try:
        on_event({"info": text})
    except Exception:
        pass


def request_outline(
    client: ChatEKLDClient,
    *,
    topic: str,
    instructions: str,
    provider: str,
    model: str,
    embed: str,
    max_iters: int,
    temperature: Optional[float],
    max_sections: int = 8,
    on_event=None,
    max_attempts: int = 1,
    retry_backoff_s: float = 0.0,
    should_cancel=None,
) -> tuple[list, "ChatResult"]:
    """Ask the agent for an outline; return (sections, raw ChatResult).

    *max_attempts* / *retry_backoff_s* / *should_cancel* drive the cancel-aware
    per-turn retry (see :func:`deckgen.retry.chat_with_retry`); the defaults
    (``max_attempts=1``) keep the original single-shot behaviour. A transient
    provider failure is retried before the no-outline salvage / ``OutlineError``
    path below ever fires.
    """
    from .retry import chat_with_retry

    message = build_outline_message(topic, instructions, max_sections)
    result = chat_with_retry(
        client,
        message,
        max_attempts=max_attempts,
        retry_backoff_s=retry_backoff_s,
        should_cancel=should_cancel,
        label="outline",
        on_event=on_event,
        system_prompt=OUTLINE_SYSTEM_PROMPT,
        provider=provider,
        model=model,
        embed=embed,
        agent=True,
        max_iters=max_iters,
        temperature=temperature,
    )
    # Cap to the requested section budget: the prompt ASKS for ≤ max_sections
    # but a chatty model can return more, and an uncapped outline silently
    # multiplied generation time/cost (the instructions-fallback below was
    # already capped — this makes the primary path match it).
    sections = [] if result.error else parse_outline(result.text)[:max_sections]
    if not sections:
        # Salvage: the lecturer's free-text instructions frequently already hold a
        # numbered/heading/bulleted outline. When the model returns nothing usable
        # — a small local model looping on tool calls until the iteration cap is
        # the common case — fall back to parsing those instructions, turning a
        # discarded run into a deck built from the structure the user typed.
        # Gated on _looks_like_outline so prose instructions are not split
        # line-by-line into bogus sections.
        if _looks_like_outline(instructions):
            fallback = parse_outline(instructions)[:max_sections]
            if fallback:
                _emit_info(
                    on_event,
                    "Model returned no usable outline; built it from your "
                    f"instructions instead ({len(fallback)} section(s)).",
                )
                return fallback, result
        if result.error:
            raise OutlineError(f"Outline request failed: {result.error}")
        raise OutlineError(
            "Could not parse a lecture outline from the model output. Re-run with "
            "--verbose to inspect the raw response, or try a more capable model."
        )
    return sections, result


def _extract_json_array(text: str) -> Optional[str]:
    """Return the first balanced top-level ``[...]`` span in *text*, or None."""
    start = text.find("[")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("[", start + 1)
    return None


def _coerce_sections(data) -> list:
    """Coerce a parsed JSON array into :class:`Section` objects (tolerantly).

    Accepts the loose shapes a model emits: a dict per section with the title
    under any of ``title``/``name``/``section`` and points under any of
    ``points``/``bullets``/``items``, or a bare string (a title-only section).
    Untitled/blank entries are dropped; a non-list input yields ``[]``.
    """
    sections: list = []
    if not isinstance(data, list):
        return sections
    for item in data:
        if isinstance(item, dict):
            title = item.get("title") or item.get("name") or item.get("section")
            if not title or not str(title).strip():
                continue
            raw_points = item.get("points") or item.get("bullets") or item.get("items") or []
            points = [str(p).strip() for p in raw_points if str(p).strip()] if isinstance(raw_points, list) else []
            sections.append(Section(title=str(title).strip(), points=points))
        elif isinstance(item, str) and item.strip():
            sections.append(Section(title=item.strip(), points=[]))
    return sections


def parse_outline(text: str) -> list:
    """Parse the model output into a list of :class:`Section`.

    JSON-first, with a heading/bullet fallback. Returns ``[]`` if nothing usable.
    """
    if not text or not text.strip():
        return []

    # 1) JSON array (possibly wrapped in prose / code fences).
    span = _extract_json_array(text)
    if span is not None:
        try:
            sections = _coerce_sections(json.loads(span))
            if sections:
                return sections
        except ValueError:
            pass

    # 2) Fallback: heading + nested bullets.
    return _parse_heading_list(text)


def _parse_heading_list(text: str) -> list:
    """Best-effort parse of a markdown/numbered outline into sections.

    Handles the two shapes a model typically falls back to:

      ## Intro            |   1. Intro
      - point a           |      - point a
      - point b           |   2. Methods

    ATX headings, "Section N:" lines, and top-level (unindented) numbered items
    are section titles; ``-``/``*``/``+`` bullets and indented numbered items are
    points of the current section. If the text has no such structure at all, each
    bullet/line becomes its own (point-less) section.
    """
    lines = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("```")
    ]
    if not lines:
        return []

    def _is_section_head(line: str) -> Optional[str]:
        m = _ATX_RE.match(line)
        if m:
            return m.group(1).strip()
        m = _SECTION_WORD_RE.match(line)
        if m:
            return m.group(1).strip()
        m = _NUM_RE.match(line)
        if m and len(m.group(1)) == 0:  # top-level numbered item
            return m.group(2).strip()
        return None

    has_structure = any(_is_section_head(ln) is not None for ln in lines)

    if not has_structure:
        # Flat list: each bullet (or bare line) is a section title.
        sections: list = []
        for ln in lines:
            m = _DASH_RE.match(ln)
            sections.append(Section(title=(m.group(2).strip() if m else ln.strip()), points=[]))
        return sections

    sections = []
    current: Optional[Section] = None
    for ln in lines:
        head = _is_section_head(ln)
        if head is not None:
            current = Section(title=head, points=[])
            sections.append(current)
            continue
        dash = _DASH_RE.match(ln)
        num = _NUM_RE.match(ln)
        point_text = None
        if dash:
            point_text = dash.group(2).strip()
        elif num:  # indented numbered item (top-level handled as a head above)
            point_text = num.group(2).strip()
        if point_text is not None:
            if current is None:
                current = Section(title="Overview", points=[])
                sections.append(current)
            current.points.append(point_text)
    return sections
