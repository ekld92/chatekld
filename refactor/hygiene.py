"""Conservative, advisory-only formatting & link hygiene (Phase 1).

Phase 1 is preview-only and the user's French shorthand must never be touched, so
hygiene here produces **advisory notes** — it does not rewrite wording. The only
body change the planner makes is inlining extracted-text callouts beneath images
(``plan.py``); these checks surface broken/unresolved embeds, an obvious
frontmatter-formatting smell, and deterministic "you didn't skip a line"
structure issues (``structure_notes``) for the user to act on later (Phase 2).

The structure checks are intentionally deterministic (no LLM): missing blank
lines before headings / lists / code fences are unambiguous rendering issues a
regex catches with zero false-positive risk on the user's shorthand. The fuzzy
"does this sentence make sense" pass is a separate, opt-in LLM call
(``refactor.review``) — kept out of this zero-cost, always-on layer.
"""
from __future__ import annotations

import re

from refactor.result import (
    HygieneNote,
    ImageRef,
    STATUS_MISSING,
    STATUS_UNRESOLVED,
)

# A leading YAML frontmatter block, tolerant of both LF and CRLF line endings
# (``re.DOTALL`` lets ``.*?`` span newlines; ``\r?`` accepts CRLF). Anchored at
# string start so a mid-note ``---`` horizontal rule is never mistaken for it.
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)

# `tags:` written as a bare inline comma string (`tags: a, b, c`) instead of a
# YAML list — the one frontmatter smell we flag conservatively (no auto-fix). The
# `(?!\[)` negative lookahead means a valid YAML **flow sequence**
# (`tags: [a, b, c]`, which Obsidian renders fine) is NOT flagged — only the
# genuinely-bracketless comma form is.
_INLINE_TAGS_RE = re.compile(r"^tags:\s*(?!\[)\S.*,.*$", re.MULTILINE)

# A non-embed Obsidian wikilink `[[target]]` (the `(?<!!)` excludes the `![[…]]`
# image/attachment embeds, which the OCR pipeline already resolves). Captures the
# inside; the file part is isolated by stripping any `|alias`, `#heading`,
# `^block` suffix in ``link_notes``.
_WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\[\]]+?)\]\]")

# A non-breaking space (U+00A0) — surfaced as an advisory only (NOT auto-fixed:
# converting it would alter French typographic spacing).
_NBSP = " "
# A line whose leading indentation contains a tab (tab-vs-space inconsistency —
# advisory only; tab→space conversion is deliberately not auto-applied).
_TAB_INDENT_RE = re.compile(r"^[ \t]*\t")

# --- Deterministic structure checks ----------------------------------------
# ATX heading (`# ` … `###### `) — requires the space, so an Obsidian `#tag`
# never matches. List item — bullet or ordered, ≤3 leading spaces (CommonMark's
# limit before it becomes a code block); a blockquote `> - x` doesn't match (the
# `>` prefix), so quoted lists are intentionally not flagged. Fence — ``` or ~~~.
_HEADING_LINE_RE = re.compile(r"^#{1,6}\s+\S")
_LIST_ITEM_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,9}[.)])\s+\S")
_FENCE_RE = re.compile(r"^\s{0,3}(?:`{3,}|~{3,})")


def _next_list_context(in_list: bool, *, is_fence: bool, is_heading: bool,
                       is_list: bool, is_indented: bool) -> bool:
    """Track whether we are inside a list block, line by line.

    THE single source of truth shared by ``structure_notes`` (which only flags a
    "list with no blank line before it") and ``normalize.normalize_text`` (which
    inserts that blank). Both must agree or the invariant
    ``structure_notes(normalize_text(x)) == []`` breaks.

    The point of the context is to tell a list that *starts* a block (needs a blank
    line above it) apart from a list item that *continues* one (must NOT gain a
    blank — inserting one would split a single tight list in two). A list item
    opens/sustains the context; a **lazy indented continuation line** of the current
    item sustains it (this is the case the old prev-line-only check got wrong); a
    heading or code fence ends it; a non-indented paragraph line ends it. Call this
    only for non-blank content lines — a blank line is tolerated inside a (loose)
    list and leaves the context unchanged, so callers skip the update on blanks.
    """
    if is_list:
        return True
    if is_fence or is_heading:
        return False
    if in_list and is_indented:
        return True   # indented continuation of the current list item
    return False      # a non-indented paragraph (or other) line ends the list

# Cap per-note structure advisories so a single very irregular note cannot flood
# the detail pane; a trailing summary note records the overflow.
_MAX_STRUCTURE_NOTES = 25


def embed_notes(images: list[ImageRef]) -> list[HygieneNote]:
    """Advisory notes for image embeds that resolve nowhere / to a missing file."""
    notes: list[HygieneNote] = []
    for im in images:
        if im.status == STATUS_UNRESOLVED:
            notes.append(HygieneNote(
                kind="unresolved_embed",
                message=f"Embed “{im.target}” does not resolve to any vault file.",
                line=im.line,
            ))
        elif im.status == STATUS_MISSING:
            notes.append(HygieneNote(
                kind="broken_embed",
                message=f"Embed “{im.target}” → {im.rel_path} (file not found).",
                line=im.line,
            ))
    return notes


def frontmatter_notes(text: str) -> list[HygieneNote]:
    """Light, conservative frontmatter advisories. Never rewrites the note."""
    notes: list[HygieneNote] = []
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return notes
    block = m.group(1)  # the YAML body between the two --- fences
    if _INLINE_TAGS_RE.search(block):
        notes.append(HygieneNote(
            kind="frontmatter",
            message="`tags` looks like an inline comma string; consider a YAML list.",
            line=1,
        ))
    return notes


def link_notes(text: str, link_index: dict[str, list[str]] | None) -> list[HygieneNote]:
    """Advisory notes for non-embed ``[[wikilink]]``s that resolve to no file.

    *link_index* is a ``basename.lower() -> [vault-relative paths]`` map over the
    **whole** vault (built by ``resolver.build_link_index``); ``None`` (or empty)
    short-circuits to no notes so a caller that did not build it — e.g. the apply
    re-analysis, which never uses hygiene — pays nothing and never mis-flags.

    A link resolves when its basename (or basename + ``.md`` for a note link
    written without an extension) is present in the index. Resolution is by
    basename only — deliberately conservative, so a link that Obsidian would
    resolve by a fuller path is never falsely flagged as broken. Same-note anchor
    links (``[[#heading]]`` / ``[[^block]]``) are skipped (no file part).
    """
    notes: list[HygieneNote] = []
    if not link_index:
        return notes
    for m in _WIKILINK_RE.finditer(text):
        inner = m.group(1)
        # Isolate the file part: drop a |display alias, then a #heading / ^block.
        target = inner.split("|", 1)[0]
        target = target.split("#", 1)[0].split("^", 1)[0].strip()
        if not target:
            continue  # pure same-note anchor link, nothing to resolve
        base = target.rsplit("/", 1)[-1].lower()
        if not base:
            continue
        if base in link_index or f"{base}.md" in link_index:
            continue
        notes.append(HygieneNote(
            kind="broken_link",
            message=f"Wikilink “{target}” resolves to no file in the vault.",
            line=text.count("\n", 0, m.start()) + 1,
        ))
    return notes


def whitespace_notes(text: str) -> list[HygieneNote]:
    """Summary advisories for whitespace/encoding smells (one note each, capped).

    Surfaces, as a single counted note each: trailing whitespace, non-breaking
    spaces (U+00A0), tab-indented lines, a missing final newline, and CRLF line
    endings. The "auto-fixable" label must stay **honest** — it is only applied
    to what ``refactor.normalize`` actually rewrites:

    * **Trailing whitespace** is counted ONLY over the lines normalize touches:
      it strips trailing whitespace on body lines but emits YAML frontmatter and
      fenced-code-block interiors verbatim. Counting every line (the old
      behaviour) over-claimed, e.g. for intentional trailing spaces inside a
      code block that Fix formatting deliberately preserves.
    * **Missing final newline** is auto-fixable only for a non-whitespace-only
      file (normalize returns a blank/whitespace-only note unchanged).
    * **CRLF** endings ARE normalized to LF (so they are reported as auto-fixable
      and counted as a single advisory, not — as before — as every line carrying
      "trailing whitespace" because ``split('\\n')`` leaves a stray ``\\r``).
    * **NBSP and tab indentation** stay advisory-only (their messages say so),
      since converting them could change French typographic spacing / intended
      indentation.
    """
    notes: list[HygieneNote] = []
    if not text:
        return notes
    lines = text.split("\n")
    fm_end = _frontmatter_end_line(text)

    # Count trailing whitespace exactly where normalize would strip it: lines
    # after the frontmatter and outside a fenced-code interior. We mirror
    # normalize's fence bookkeeping — the OPENING delimiter is fixable (it gets
    # rstripped) and toggles us *into* the fence; everything until and including
    # the CLOSING delimiter is emitted verbatim, so it is NOT counted.
    trailing = 0
    in_fence = False
    for idx, raw_line in enumerate(lines):
        if idx + 1 <= fm_end:
            continue  # frontmatter interior — normalize leaves it untouched
        is_fence = bool(_FENCE_RE.match(raw_line))
        if not in_fence:
            # Drop one trailing CR first so a CRLF file is not mistaken for
            # all-lines-trailing-whitespace; real spaces before the CR still count.
            ln = raw_line[:-1] if raw_line.endswith("\r") else raw_line
            if ln != ln.rstrip():
                trailing += 1
            if is_fence:
                in_fence = True
        elif is_fence:
            in_fence = False  # closing delimiter (verbatim) → not counted
    if trailing:
        notes.append(HygieneNote(
            kind="whitespace",
            message=f"{trailing} line(s) have trailing whitespace (auto-fixable: Fix formatting).",
            line=0,
        ))
    nbsp = sum(1 for ln in lines if _NBSP in ln)
    if nbsp:
        notes.append(HygieneNote(
            kind="whitespace",
            message=f"{nbsp} line(s) contain non-breaking spaces (U+00A0) — advisory only.",
            line=0,
        ))
    tabs = sum(1 for ln in lines if _TAB_INDENT_RE.match(ln))
    if tabs:
        notes.append(HygieneNote(
            kind="whitespace",
            message=f"{tabs} line(s) use tab indentation — advisory only.",
            line=0,
        ))
    # CRLF: a single advisory (not a per-line trailing-whitespace miscount).
    # Fix formatting normalizes the whole file to LF.
    if "\r" in text:
        notes.append(HygieneNote(
            kind="whitespace",
            message="File uses CRLF (Windows) line endings — Fix formatting will convert it to LF.",
            line=0,
        ))
    # Missing final newline: only auto-fixable for a non-empty (non-whitespace-
    # only) file — normalize returns a blank note unchanged.
    if text.strip() and not text.endswith("\n"):
        notes.append(HygieneNote(
            kind="whitespace",
            message="File has no trailing newline (auto-fixable: Fix formatting).",
            line=0,
        ))
    return notes


def _frontmatter_end_line(text: str) -> int:
    """1-based line number of the closing ``---`` of a leading frontmatter block.

    0 when there is no frontmatter. Lines ≤ this number are skipped by the
    structure scan so YAML keys are never mistaken for headings/lists.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return 0
    return text.count("\n", 0, m.end())


def structure_notes(text: str) -> list[HygieneNote]:
    """Advisory "you didn't skip a line" structure checks. Never rewrites.

    Flags the unambiguous missing-blank-line cases that change how Obsidian
    renders a note:

    * a **heading** with content on the line directly above it (no blank line);
    * a **list** that starts directly under a paragraph line (no blank line),
      which often won't render as a list;
    * a fenced **code block** not separated from surrounding text by blank lines.

    Lines inside frontmatter and fenced code blocks are skipped. Capped at
    ``_MAX_STRUCTURE_NOTES`` per note. Deterministic — zero LLM, zero false
    positives on French shorthand (terseness is never flagged).
    """
    notes: list[HygieneNote] = []
    if not text:
        return notes
    lines = text.split("\n")
    fm_end = _frontmatter_end_line(text)

    def is_blank(i: int) -> bool:
        return i < 0 or i >= len(lines) or lines[i].strip() == ""

    overflow = 0
    in_fence = False
    in_list = False
    for idx, line in enumerate(lines):
        lineno = idx + 1
        if lineno <= fm_end:
            continue

        if _FENCE_RE.match(line):
            in_list = False   # a code fence ends any list block (mirror normalize)
            if not in_fence:
                in_fence = True
                # Opening fence: want a blank line (or the note/section start)
                # before it. lineno > fm_end + 1 skips a fence on the very first
                # content line (right after frontmatter / at note start).
                if lineno > fm_end + 1 and not is_blank(idx - 1):
                    overflow += _maybe_add(
                        notes, "Code block has no blank line before it.", lineno)
            else:
                in_fence = False
                if not is_blank(idx + 1):
                    overflow += _maybe_add(
                        notes, "Code block has no blank line after it.", lineno)
            continue
        if in_fence:
            continue

        prev = lines[idx - 1] if idx > 0 else ""
        prev_blank = is_blank(idx - 1)
        first_content = lineno <= fm_end + 1
        is_heading_line = bool(_HEADING_LINE_RE.match(line))
        is_list_line = bool(_LIST_ITEM_RE.match(line))

        if is_heading_line:
            if (not first_content and not prev_blank
                    and not _HEADING_LINE_RE.match(prev)):
                overflow += _maybe_add(
                    notes, "Heading has no blank line before it (skip a line above it).",
                    lineno)
        elif is_list_line:
            # Flag only a list that STARTS a block. `in_list` (set by a prior list
            # item or a lazy indented continuation of one) means this item merely
            # continues an existing list, which needs no blank above it — so we
            # don't flag it and normalize won't insert one (invariant preserved).
            if (not first_content and not prev_blank and not in_list
                    and not _HEADING_LINE_RE.match(prev)):
                overflow += _maybe_add(
                    notes, "List has no blank line before it (skip a line above it).",
                    lineno)

        # Update the list context for the next line (blank lines are tolerated
        # inside a loose list, so they leave it unchanged → skip the update).
        if line.strip():
            in_list = _next_list_context(
                in_list, is_fence=False, is_heading=is_heading_line,
                is_list=is_list_line, is_indented=line[:1] in (" ", "\t"))

    if overflow > 0:
        notes.append(HygieneNote(
            kind="formatting",
            message=f"…and {overflow} more formatting issue(s) not shown.",
            line=0,
        ))
    return notes


def _maybe_add(notes: list[HygieneNote], message: str, line: int) -> int:
    """Append a formatting note unless the cap is reached; return overflow count.

    Returns 1 when the note was dropped due to the cap (so the caller can total
    the overflow for a single summary note), else 0.
    """
    if len(notes) >= _MAX_STRUCTURE_NOTES:
        return 1
    notes.append(HygieneNote(kind="formatting", message=message, line=line))
    return 0
