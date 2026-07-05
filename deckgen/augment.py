"""Augment an existing Beamer deck: split it into sections, splice a revision in.

The Deck Generator's *generate* path builds a deck from scratch (outline → per
section → assemble). This module is the **augment** path: it takes a *finished*
deck ``.tex`` (deckgen-made or hand-written) and lets a free-text instruction
deepen one section, add a table, or insert a brand-new section — splicing the
result back in **byte-for-byte outside the edited span** so a section-scoped edit
can ride the same whole-document sha256 stale-diff guard the route enforces.

Like the rest of the deckgen core (``assemble.py`` / ``scaffold.py`` /
``review.py``) this module is **app-independent and free of any LLM/transport
import**: the model call lives in ``api/routes/deck.py``. It reuses the
boundary-detection primitives from :mod:`deckgen.template` (the same ones
``split_template`` uses) so an augment splits a deck exactly where generation
would have injected its sections.

The load-bearing invariant — mirrored on :mod:`refactor.sections` — is:

    replace_section(tex, sec, sec.body) == tex   (byte-identical)

so an *identity* replace round-trips, and therefore a real replace changes only
the bytes inside the edited span. The same holds for the un-edited region around
an :func:`insert_section`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .assemble import _FRAME_BEGIN_RE, _SECTION_RE, _strip_latex_comments
from .template import (
    _CONTENT_SECTION_RE,
    _DOC_BEGIN_RE,
    _DOC_END_RE,
    _balanced_brace_group,
    _find_closing_start,
    mask_comments,
)

# Cap the existing-deck excerpt handed to the model so a huge deck cannot blow
# the context window / cost. The route clips to this before building the prompt
# AND refuses a whole-deck replace whose source exceeds it (a model that only saw
# the first cap chars must not be allowed to overwrite the whole region — that
# would silently drop every slide past the cutoff). See api/routes/deck.py.
AUGMENT_MAX_SOURCE_CHARS = 40_000

# Locate the ``{`` that opens a section's title argument, after the macro and an
# optional ``[short]`` form. We then read the title with a brace-BALANCED reader
# (``_balanced_brace_group``) rather than a ``[^}]*`` regex, so a title containing
# a nested group — ``\section{A \textbf{B}}`` — is captured whole instead of being
# truncated at the first inner ``}``.
_SECTION_TITLE_OPEN_RE = re.compile(r"\\section\b\s*(?:\[[^\]]*\])?\s*\{")


class AugmentError(ValueError):
    """Raised when a deck cannot be parsed into the expected structure."""


@dataclass
class DeckSection:
    """One ``\\section`` span of an existing deck, in original-text coordinates.

    ``body`` is the EXACT substring ``tex[start:end]`` (trailing whitespace
    included) so :func:`replace_section` can round-trip it byte-for-byte; ``title``
    is the human-readable section title for the scope selector.
    """
    title: str
    body: str
    start: int
    end: int


@dataclass
class DeckParts:
    """An existing deck decomposed for augmentation.

    ``preamble`` / ``opening`` / ``closing`` are the verbatim slices around the
    section region (for display + macro scanning); ``sections`` are the individual
    ``\\section`` spans; ``sections_start`` / ``sections_end`` bound the region
    those sections occupy (where a new section is inserted). All offsets index the
    original ``tex``.
    """
    tex: str
    preamble: str
    opening: str
    closing: str
    sections: list = field(default_factory=list)  # list[DeckSection]
    sections_start: int = 0
    sections_end: int = 0

    @property
    def sections_region(self) -> str:
        """The exact text occupied by all sections (between opening and closing)."""
        return self.tex[self.sections_start:self.sections_end]


def _section_title(span: str) -> str:
    """Best-effort section title from a ``\\section{...}`` span, or a fallback.

    Brace-balanced so a nested group inside the title (e.g. ``\\textbf{...}``) is
    not truncated at the first inner ``}``.
    """
    m = _SECTION_TITLE_OPEN_RE.search(span)
    if m:
        # _balanced_brace_group wants the index OF the opening brace; the regex
        # ends one past it, so step back one char.
        title = _balanced_brace_group(span, m.end() - 1).strip()
        if title:
            return title
    return "(untitled section)"


def deck_counts(tex: str) -> dict:
    """Count live ``\\section``s and ``\\begin{frame}``s in *tex*.

    Comments are stripped first (a commented-out ``\\begin{frame}`` is not a real
    slide) so the count matches what ``validate`` reasons about. Used by the route
    to surface a *before → after* delta in the augment preview: a drop in the frame
    or section count is the cheap, robust signal that an augmentation silently lost
    content (e.g. a model that revised only the part of the deck it was shown).
    """
    scan = _strip_latex_comments(tex or "")
    return {
        "sections": len(_SECTION_RE.findall(scan)),
        "frames": len(_FRAME_BEGIN_RE.findall(scan)),
    }


def split_deck(tex: str) -> DeckParts:
    """Decompose a full deck ``.tex`` into preamble / opening / sections / closing.

    Boundary detection mirrors :func:`deckgen.template.split_template` (run on a
    comment-masked copy so a commented-out ``\\section`` / references frame is not
    mistaken for a live one), but here the section region is *kept* and split into
    individual :class:`DeckSection` spans rather than dropped. Raises
    :class:`AugmentError` when the input is not a complete LaTeX document.
    """
    if not tex or not tex.strip():
        raise AugmentError("The deck is empty.")

    masked = mask_comments(tex)
    m_begin = _DOC_BEGIN_RE.search(masked)
    if m_begin is None:
        raise AugmentError(
            "No \\begin{document} found — this does not look like a complete "
            "Beamer deck. Point at a full .tex deck."
        )
    m_end = _DOC_END_RE.search(masked, m_begin.end())
    if m_end is None:
        raise AugmentError("No \\end{document} found in the deck.")

    doc_start, doc_end = m_begin.start(), m_end.end()
    masked_body = masked[doc_start:doc_end]

    # Closing tail (\appendix / references frame / \end{document}) and the first
    # content \section — both as offsets into masked_body, then made absolute.
    rel_closing = _find_closing_start(masked_body)
    m_sec = _CONTENT_SECTION_RE.search(masked_body)
    rel_open_end = m_sec.start() if m_sec else None
    if rel_open_end is None or rel_open_end > rel_closing:
        # No content section before the tail (degenerate deck) — the section
        # region is empty; inserts land just before the closing tail.
        rel_open_end = rel_closing

    sections_start = doc_start + rel_open_end
    sections_end = doc_start + rel_closing

    # Each content \section start within the region; a section spans up to the
    # next section start (or the region end). Spans are contiguous and gap-free,
    # so concatenating them reproduces the region exactly.
    starts = [
        doc_start + m.start()
        for m in _CONTENT_SECTION_RE.finditer(masked_body, rel_open_end, rel_closing)
    ]
    sections: list[DeckSection] = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else sections_end
        span = tex[s:e]
        sections.append(DeckSection(title=_section_title(span), body=span, start=s, end=e))

    return DeckParts(
        tex=tex,
        preamble=tex[:doc_start],
        opening=tex[doc_start:sections_start],
        closing=tex[sections_end:],
        sections=sections,
        sections_start=sections_start,
        sections_end=sections_end,
    )


def _splice(tex: str, start: int, end: int, new_body: str) -> str:
    """Replace ``tex[start:end]`` with *new_body*, preserving the span's trailing
    whitespace so the surrounding layout (blank-line separators) is untouched.

    WHY not the naive ``tex[:start] + new_body + tex[end:]``: a model's section body
    is ``.strip()``-ed, so naive slicing would drop the blank-line separator that
    sat between this span and the next ``\\section`` — mutating bytes *outside* the
    edited region. That would break the load-bearing guarantee the whole augment
    write-path leans on: that a section-scoped edit changes ONLY the edited span, so
    the proposed deck can be screened + applied under a single whole-document sha256
    stale-diff token. By capturing the span's own trailing whitespace and re-attaching
    it after ``new_body.rstrip()``, an identity edit (``new_body == span``) reproduces
    the original byte-for-byte (see :func:`replace_section`), and a real edit perturbs
    nothing but the content.
    """
    span = tex[start:end]
    stripped = span.rstrip()
    trailing = span[len(stripped):]
    return tex[:start] + new_body.rstrip() + trailing + tex[end:]


def replace_section(tex: str, section: DeckSection, new_body: str) -> str:
    """Splice *new_body* in place of *section*, byte-identical outside the span.

    ``replace_section(tex, sec, sec.body) == tex`` for any section produced by
    :func:`split_deck` (the round-trip invariant).
    """
    return _splice(tex, section.start, section.end, new_body)


def replace_region(parts: DeckParts, new_region: str) -> str:
    """Replace the whole section region (all ``\\section``s) with *new_region*.

    Used by the whole-deck augment pass: the opening (title/TOC) and closing
    (appendix/references/``\\end{document}``) tail are preserved verbatim; only
    the generated section body region is swapped.
    """
    return _splice(parts.tex, parts.sections_start, parts.sections_end, new_region)


def insert_section(parts: DeckParts, new_section_body: str, *, after_index: Optional[int] = None) -> str:
    """Insert *new_section_body* as a new ``\\section`` block into the deck.

    By default the new section is appended at the end of the section region (just
    before the closing tail). When *after_index* names an existing section, it is
    inserted immediately after that section instead. The text outside the
    insertion point is preserved byte-for-byte.
    """
    body = new_section_body.strip()
    if not body:
        return parts.tex
    if after_index is not None and 0 <= after_index < len(parts.sections):
        pos = parts.sections[after_index].end
    else:
        pos = parts.sections_end

    before = parts.tex[:pos]
    after = parts.tex[pos:]
    # Separate the inserted section from its neighbours by exactly one blank line on
    # each side, topping up only what the surrounding text doesn't already provide.
    # `lead` brings the text BEFORE the insertion up to a "\n\n" boundary; `trail`
    # always emits "\n\n" because something non-trivial always follows (at minimum
    # the closing \end{document} tail), so a blank line before it is always wanted.
    lead = "" if before.endswith("\n\n") else ("\n" if before.endswith("\n") else "\n\n")
    return before + lead + body + "\n\n" + after
