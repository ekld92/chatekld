"""Pure heading-section splitter for sub-note targeting (Phase 3 / request f).

The LLM actions (formatting rewrite, PDF summary, Mermaid chart) can target either
the whole note or a single **heading section** instead of the entire ``.md``. A
section is a heading line plus everything beneath it up to (but excluding) the next
heading of the **same or shallower** level — so an ``##`` section naturally carries
its ``###`` subsections.

This module is third-party-free and import-cheap (no project imports beyond
``refactor.hygiene``'s compiled detectors, reused so fence / frontmatter handling
matches the rest of the package exactly). It computes line spans and splices a
replacement back into the whole note **byte-for-byte outside the replaced span**,
which is what lets a section-scoped edit ride the same whole-note ``content_sha256``
/ ``proposed_sha256`` guards as a whole-note edit.

Definitions (1-based line numbers, matching the rest of the package):

* ``split_sections`` returns the targetable units in document order. A leading
  block of content *before the first heading* (after any YAML frontmatter) is
  surfaced as a synthetic ``is_intro`` section (level 0) so it is targetable too;
  a note with **no** headings is one intro section spanning the whole body.
* ``slice_section`` returns the section's exact text (heading line included).
* ``replace_section`` returns the whole note with one section's lines replaced and
  every other byte preserved (the splice is done on the ``split("\\n")`` line list,
  whose ``"\\n".join`` round-trips the original — including the trailing newline —
  exactly).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from refactor.hygiene import _FENCE_RE, _HEADING_LINE_RE, _frontmatter_end_line

# ATX heading with capture of the hashes (for the level) and the title text.
import re

_ATX_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass
class Section:
    """One targetable slice of a note (a heading + its body, or the intro)."""

    index: int          # position in the document-order section list
    level: int          # 1-6 for a heading; 0 for the synthetic intro block
    title: str          # heading text (no #'s); "(content before first heading)" for intro
    start_line: int     # 1-based first line of the section (the heading line)
    end_line: int       # 1-based last line of the section (inclusive)
    is_intro: bool = False

    def to_jsonable(self) -> dict:
        return {
            "index": self.index,
            "level": self.level,
            "title": self.title,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "is_intro": self.is_intro,
        }


def _heading_positions(text: str) -> list[tuple[int, int, str]]:
    """Return ``(lineno, level, title)`` for every ATX heading outside fences.

    Frontmatter and fenced-code-block interiors are skipped so a ``#`` inside a
    code block (or a ``---`` rule that is really frontmatter) is never mistaken
    for a heading — same discipline as ``hygiene.structure_notes``.
    """
    fm_end = _frontmatter_end_line(text)
    out: list[tuple[int, int, str]] = []
    in_fence = False
    for idx, line in enumerate(text.split("\n")):
        lineno = idx + 1
        if lineno <= fm_end:
            continue
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _HEADING_LINE_RE.match(line):
            m = _ATX_RE.match(line)
            if m:
                out.append((lineno, len(m.group(1)), m.group(2).strip()))
    return out


def split_sections(text: str) -> list[Section]:
    """Split *text* into document-order targetable :class:`Section` units.

    A heading's section ends at the line before the next heading of the **same or
    shallower** level (so subsections are included), or at EOF. Content before the
    first heading (after frontmatter), if any non-blank, is a synthetic intro
    section; a heading-less note is a single intro section over the whole body.
    """
    lines = text.split("\n")
    total = len(lines)
    if total == 0:
        return []
    fm_end = _frontmatter_end_line(text)
    headings = _heading_positions(text)

    def _trim_trailing_blanks(start: int, end: int) -> int:
        """Pull *end* back over trailing blank lines (never below *start*).

        Trailing blank lines belong to the *gap* between sections, not the
        section — so they sit outside every section's span and survive a
        ``replace_section`` splice (otherwise a section-scoped rewrite would eat
        the blank line that separates it from the next heading)."""
        while end > start and lines[end - 1].strip() == "":
            end -= 1
        return end

    sections: list[Section] = []
    idx = 0

    # Leading / intro block (after frontmatter, before the first heading).
    first_heading_line = headings[0][0] if headings else total + 1
    intro_start = fm_end + 1
    intro_end = first_heading_line - 1
    if intro_end >= intro_start and "\n".join(lines[intro_start - 1:intro_end]).strip():
        sections.append(Section(
            index=idx, level=0,
            title="(content before first heading)" if headings else "(whole note — no headings)",
            start_line=intro_start,
            end_line=_trim_trailing_blanks(intro_start, intro_end), is_intro=True,
        ))
        idx += 1

    for i, (lineno, level, title) in enumerate(headings):
        end = total
        for j in range(i + 1, len(headings)):
            if headings[j][1] <= level:
                end = headings[j][0] - 1
                break
        sections.append(Section(
            index=idx, level=level, title=title,
            start_line=lineno, end_line=_trim_trailing_blanks(lineno, end),
        ))
        idx += 1
    return sections


def find_section(sections: list[Section], index: int) -> Section | None:
    """Return the section at *index* (its position field), or ``None``."""
    for s in sections:
        if s.index == index:
            return s
    return None


def slice_section(text: str, section: Section) -> str:
    """Return the exact text of *section* (heading line included, no trailing \\n)."""
    lines = text.split("\n")
    return "\n".join(lines[section.start_line - 1:section.end_line])


def section_sha256(text: str, section: Section) -> str:
    """sha256 of the section's current slice — a drift tripwire for the UI."""
    return hashlib.sha256(slice_section(text, section).encode("utf-8")).hexdigest()


def replace_section(text: str, section: Section, new_section_text: str) -> str:
    """Return *text* with *section*'s lines replaced by *new_section_text*.

    Every line outside ``[start_line, end_line]`` is preserved byte-for-byte
    (the splice operates on the ``split("\\n")`` line list, so the document's
    trailing-newline shape is unchanged). A trailing newline on
    *new_section_text* is dropped before splicing so the replacement cannot
    introduce a spurious blank line at the seam.
    """
    lines = text.split("\n")
    new_lines = new_section_text.rstrip("\n").split("\n")
    spliced = lines[:section.start_line - 1] + new_lines + lines[section.end_line:]
    return "\n".join(spliced)
