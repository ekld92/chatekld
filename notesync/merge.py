"""Three-way line merge for one note region. Pure, stdlib only.

``three_way(base, ours, theirs)`` is a small diff3: each side's changes are
expressed as replacements of base line ranges (``difflib``), the two change
lists are walked in base order, a change that only one side made is applied,
an identical change made by both is applied once, and changes whose base
ranges overlap become one **conflict block** carrying both versions::

    <<<<<<< Obsidian
    …the region as it is in the vault…
    =======
    …the Zotero note, as Markdown…
    >>>>>>> Zotero

The result is what the merge editor is pre-filled with. It is never written
anywhere on its own: the user edits it, and the write is confirm-gated.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

OURS_MARK = "<<<<<<< Obsidian"
SEP_MARK = "======="
THEIRS_MARK = ">>>>>>> Zotero"


@dataclass(frozen=True)
class _Change:
    i1: int          # base range start
    i2: int          # base range end (exclusive)
    lines: tuple     # replacement lines


def _changes(base: list[str], side: list[str]) -> list[_Change]:
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, side, autojunk=False).get_opcodes():
        if tag != "equal":
            out.append(_Change(i1, i2, tuple(side[j1:j2])))
    return out


def _overlap(a: _Change, b: _Change) -> bool:
    # Two zero-length changes at the same base index (both inserted at the
    # same point) overlap too — there is no base line to order them by.
    if a.i1 == a.i2 and b.i1 == b.i2:
        return a.i1 == b.i1
    return a.i1 < b.i2 and b.i1 < a.i2


def _splitlines(text: str) -> list[str]:
    return (text or "").replace("\r\n", "\n").split("\n")


def three_way(base: str, ours: str, theirs: str) -> tuple[str, int]:
    """Return ``(merged_text, conflict_count)``."""
    b, o, t = _splitlines(base), _splitlines(ours), _splitlines(theirs)
    ca, cb = _changes(b, o), _changes(b, t)
    out: list[str] = []
    pos = 0
    ia = ib = 0
    conflicts = 0
    while ia < len(ca) or ib < len(cb):
        a = ca[ia] if ia < len(ca) else None
        c = cb[ib] if ib < len(cb) else None
        if a is not None and c is not None and _overlap(a, c):
            # Coalesce every following change on either side that keeps
            # overlapping the growing region.
            s, e = min(a.i1, c.i1), max(a.i2, c.i2)
            ja, jb = ia + 1, ib + 1
            grown = True
            while grown:
                grown = False
                if ja < len(ca) and ca[ja].i1 < e:
                    e = max(e, ca[ja].i2)
                    ja += 1
                    grown = True
                if jb < len(cb) and cb[jb].i1 < e:
                    e = max(e, cb[jb].i2)
                    jb += 1
                    grown = True
            out.extend(b[pos:s])
            ours_lines = _apply(b, ca[ia:ja], s, e)
            theirs_lines = _apply(b, cb[ib:jb], s, e)
            if ours_lines == theirs_lines:
                out.extend(ours_lines)
            else:
                conflicts += 1
                out.append(OURS_MARK)
                out.extend(ours_lines)
                out.append(SEP_MARK)
                out.extend(theirs_lines)
                out.append(THEIRS_MARK)
            pos = e
            ia, ib = ja, jb
            continue
        # Take whichever single-side change comes first in base order.
        if c is None or (a is not None and (a.i1, a.i2) <= (c.i1, c.i2)):
            assert a is not None  # the loop always has at least one change
            out.extend(b[pos:a.i1])
            out.extend(a.lines)
            pos = a.i2
            ia += 1
        else:
            out.extend(b[pos:c.i1])
            out.extend(c.lines)
            pos = c.i2
            ib += 1
    out.extend(b[pos:])
    return "\n".join(out), conflicts


def _apply(base: list[str], changes: list[_Change], s: int, e: int) -> list[str]:
    """The base slice ``[s, e)`` with *changes* (all inside it) applied."""
    out: list[str] = []
    pos = s
    for ch in changes:
        out.extend(base[pos:ch.i1])
        out.extend(ch.lines)
        pos = ch.i2
    out.extend(base[pos:e])
    return out


def has_conflict_markers(text: str) -> bool:
    return any(line.rstrip("\r") in (OURS_MARK, SEP_MARK, THEIRS_MARK)
               for line in (text or "").split("\n"))
