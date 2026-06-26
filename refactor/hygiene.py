"""Conservative, advisory-only formatting & link hygiene (Phase 1).

Phase 1 is preview-only and the user's French shorthand must never be touched, so
hygiene here produces **advisory notes** — it does not rewrite wording. The only
body change the planner makes is inlining extracted-text callouts beneath images
(``plan.py``); these checks surface broken/unresolved embeds and an obvious
frontmatter-formatting smell for the user to act on later (Phase 2).
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

# `tags:` written as an inline comma string instead of a YAML list — the one
# frontmatter smell we flag conservatively (no auto-fix).
_INLINE_TAGS_RE = re.compile(r"^tags:\s*\S+.*,.*$", re.MULTILINE)


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
