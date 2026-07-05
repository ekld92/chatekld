"""Deterministic, idempotent Markdown formatting normalizer (Phase 2 fix).

This is the pure transform behind the second Phase 2 batch action ("Fix
formatting"). It applies only the **unambiguous, zero-false-positive** fixes that
``hygiene.structure_notes`` already flags as advisories, plus two equally-safe
whitespace fixes — and nothing opinionated:

* a blank line before a heading / list that has content directly above it;
* blank lines around a fenced code block;
* trailing whitespace stripped (outside code fences);
* runs of 3+ blank lines collapsed to one;
* exactly one trailing newline at EOF;
* CRLF (Windows) line endings normalized to LF. This falls out of splitting on
  ``\n`` and ``rstrip()``-ing body lines (the stray ``\r`` is trailing
  whitespace); it is documented here so it is explicit, not a silent side
  effect. Consequence: a CRLF note with no other issue still shows a whole-file
  diff. ``hygiene.whitespace_notes`` reports CRLF as its own advisory.

Deliberately **NOT** done (too opinionated / risks changing meaning, kept as
advisory-only via ``hygiene.whitespace_notes``): tab→space conversion and
non-breaking-space (U+00A0) conversion — the latter would silently alter French
typographic spacing.

Two invariants the writer (``refactor.format_fix``) and the tests rely on:

* **idempotent** — ``normalize_text(normalize_text(x)) == normalize_text(x)``;
* **closes the advisories** — ``hygiene.structure_notes(normalize_text(x)) == []``
  (the fix inserts exactly the blank lines the detector flags), so a re-plan after
  applying shows no remaining structure issues.

It reuses ``hygiene``'s compiled detectors (one source of truth, so detection and
fixing can never drift) and the same frontmatter / fenced-code-block handling, so
YAML frontmatter and whitespace-significant code interiors are never rewritten.
"""
from __future__ import annotations

from refactor.hygiene import (
    _FENCE_RE,
    _HEADING_LINE_RE,
    _LIST_ITEM_RE,
    _frontmatter_end_line,
    _next_list_context,
)


def normalize_text(
    text: str,
    *,
    strip_trailing: bool = True,
    blank_before_block: bool = True,
    blank_around_fence: bool = True,
    collapse_blank_runs: bool = True,
    ensure_final_newline: bool = True,
) -> str:
    """Return *text* with the conservative formatting fixes applied.

    A whitespace-only (or empty) note is returned unchanged — there is nothing
    to normalize and an empty note must not gain a spurious newline. Frontmatter
    interior and fenced-code-block interiors are emitted verbatim.
    """
    if not text or not text.strip():
        return text

    fm_end = _frontmatter_end_line(text)
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    in_list = False   # mirror structure_notes' list-context tracking (shared helper)
    pending_blank_after_fence = False

    for idx, line in enumerate(lines):
        lineno = idx + 1

        # Frontmatter block (incl. its closing ---): emit verbatim.
        if lineno <= fm_end:
            out.append(line)
            continue

        is_fence = _FENCE_RE.match(line) is not None

        # Inside a fenced code block: never touch the interior; a fence line
        # closes it.
        if in_fence:
            out.append(line)
            if is_fence:
                in_fence = False
                pending_blank_after_fence = blank_around_fence
            continue

        cleaned = line.rstrip() if strip_trailing else line

        # Blank line (or whitespace-only → normalized to empty).
        if cleaned.strip() == "":
            pending_blank_after_fence = False  # the blank already separates it
            if collapse_blank_runs and out and out[-1] == "":
                continue  # drop the extra blank in a run
            out.append("")
            continue

        # Non-blank content line outside any fence.
        first_content = lineno <= fm_end + 1
        prev_line = out[-1] if out else ""
        prev_blank = (prev_line == "") if out else True
        is_heading_line = bool(_HEADING_LINE_RE.match(cleaned))
        is_list_line = bool(_LIST_ITEM_RE.match(cleaned))

        insert_blank = False
        if pending_blank_after_fence and not prev_blank:
            # Code block must be followed by a blank line.
            insert_blank = True
        elif is_fence:  # opening fence
            if blank_around_fence and not first_content and not prev_blank:
                insert_blank = True
        elif is_heading_line:
            if (blank_before_block and not first_content and not prev_blank
                    and not _HEADING_LINE_RE.match(prev_line)):
                insert_blank = True
        elif is_list_line:
            # Insert a blank only before a list that STARTS a block. `in_list` (set
            # by a prior list item or a lazy indented continuation of one) means
            # this item continues an existing list — inserting a blank there would
            # split one tight list in two. A list directly under a heading is also
            # left as-is. Mirrors structure_notes exactly (shared `_next_list_context`).
            if (blank_before_block and not first_content and not prev_blank
                    and not in_list
                    and not _HEADING_LINE_RE.match(prev_line)):
                insert_blank = True

        pending_blank_after_fence = False
        if insert_blank:
            out.append("")
        out.append(cleaned)
        # Advance the list context for the next line (probe indentation on the raw
        # `line` — `cleaned` has had its trailing whitespace stripped but leading
        # whitespace is intact on both).
        in_list = _next_list_context(
            in_list, is_fence=is_fence, is_heading=is_heading_line,
            is_list=is_list_line, is_indented=line[:1] in (" ", "\t"))
        if is_fence:
            in_fence = True

    result = "\n".join(out)
    if ensure_final_newline:
        # Exactly one trailing newline; also trims trailing blank lines left by
        # the collapse pass. Guarded by the empty-input check at the top, so this
        # never turns "" into "\n".
        result = result.rstrip("\n") + "\n"
    return result
