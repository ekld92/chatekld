"""Unit tests for deckgen's augment core — split / replace / insert + prompts.

Pure logic only: imports ``deckgen.augment`` / ``deckgen.prompts`` /
``deckgen.review`` (none pull in ``requests``) and never touches the app or a
running server. Run from the repo root:  python -m pytest deckgen/tests/ -v
"""
from __future__ import annotations

import pytest

from deckgen.augment import (
    AUGMENT_MAX_SOURCE_CHARS,
    AugmentError,
    deck_counts,
    insert_section,
    replace_region,
    replace_section,
    split_deck,
)
from deckgen.prompts import (
    SYSTEM_PROMPT_LIMIT,
    augment_system_prompt,
    build_augment_message,
)
from deckgen.review import screen_repair


# A small but realistic deck: preamble + title/outline opening + two sections +
# a references frame closing tail.
_DECK = r"""\documentclass{beamer}
\usetheme{Madrid}
\title{Demo}
\begin{document}

\frame{\titlepage}

\begin{frame}{Outline}
  \tableofcontents
\end{frame}

\section{Intro}
\begin{frame}{Intro}
  \begin{itemize}
    \item what it is
  \end{itemize}
\end{frame}

\section{Methods}
\begin{frame}{Methods}
  \begin{itemize}
    \item how we did it
  \end{itemize}
\end{frame}

\begin{frame}{References}
  \printbibliography
\end{frame}

\end{document}
"""


# ---------------------------------------------------------------------------
# split_deck
# ---------------------------------------------------------------------------

def test_split_deck_finds_sections_and_titles():
    parts = split_deck(_DECK)
    assert [s.title for s in parts.sections] == ["Intro", "Methods"]
    # The opening keeps the title/outline frames; the closing keeps the
    # references frame + \end{document}.
    assert "\\titlepage" in parts.opening
    assert "\\tableofcontents" in parts.opening
    assert "\\printbibliography" in parts.closing
    assert "\\end{document}" in parts.closing
    # No \section leaked into opening/closing.
    assert "\\section" not in parts.opening
    assert "\\section" not in parts.closing


def test_split_deck_region_reconstructs_exactly():
    parts = split_deck(_DECK)
    # preamble + opening + sections_region + closing reproduces it byte-for-byte.
    assert parts.preamble + parts.opening + parts.sections_region + parts.closing == _DECK
    # preamble is everything before \begin{document}; opening begins the body.
    assert _DECK.startswith(parts.preamble)
    assert parts.opening.startswith("\\begin{document}")


def test_split_deck_rejects_non_documents():
    with pytest.raises(AugmentError):
        split_deck("")
    with pytest.raises(AugmentError):
        split_deck("\\section{x}\n\\begin{frame}{x}\\end{frame}")  # no \begin{document}


def test_split_deck_ignores_commented_section():
    deck = _DECK.replace("\\section{Methods}", "% \\section{Commented}\n\\section{Methods}")
    parts = split_deck(deck)
    # The commented \section must not become its own span.
    assert [s.title for s in parts.sections] == ["Intro", "Methods"]


# ---------------------------------------------------------------------------
# replace_section / replace_region — round-trip invariant
# ---------------------------------------------------------------------------

def test_replace_section_identity_roundtrips():
    parts = split_deck(_DECK)
    for sec in parts.sections:
        assert replace_section(parts.tex, sec, sec.body) == _DECK


def test_replace_section_changes_only_target_span():
    parts = split_deck(_DECK)
    intro = parts.sections[0]
    new_body = "\\section{Intro}\n\\begin{frame}{Intro}\n  deepened\n\\end{frame}"
    out = replace_section(parts.tex, intro, new_body)
    assert "deepened" in out
    # Everything after the edited section is untouched.
    assert out.endswith(parts.closing)
    assert "\\section{Methods}" in out
    # The opening (everything before the first section) is byte-identical.
    assert out[:intro.start] == _DECK[:intro.start]


def test_replace_region_swaps_only_sections():
    parts = split_deck(_DECK)
    new_region = "\\section{Replaced}\n\\begin{frame}{R}\\end{frame}"
    out = replace_region(parts, new_region)
    assert "\\section{Replaced}" in out
    assert "\\section{Intro}" not in out and "\\section{Methods}" not in out
    # Preamble/opening + closing preserved verbatim.
    assert out.startswith(parts.preamble + parts.opening)
    assert out.endswith(parts.closing)


def test_replace_region_identity_roundtrips():
    parts = split_deck(_DECK)
    assert replace_region(parts, parts.sections_region) == _DECK


# ---------------------------------------------------------------------------
# insert_section
# ---------------------------------------------------------------------------

def test_insert_section_appends_before_closing():
    parts = split_deck(_DECK)
    new = "\\section{New}\n\\begin{frame}{New}\\end{frame}"
    out = insert_section(parts, new)
    # Inserted after Methods but before the references/closing tail.
    assert out.index("\\section{New}") > out.index("\\section{Methods}")
    assert out.index("\\section{New}") < out.index("\\printbibliography")
    assert out.endswith(parts.closing)


def test_insert_section_after_index():
    parts = split_deck(_DECK)
    new = "\\section{Between}\n\\begin{frame}{B}\\end{frame}"
    out = insert_section(parts, new, after_index=0)
    assert out.index("\\section{Intro}") < out.index("\\section{Between}") < out.index("\\section{Methods}")


def test_insert_empty_is_noop():
    parts = split_deck(_DECK)
    assert insert_section(parts, "   ") == _DECK


# ---------------------------------------------------------------------------
# Splice + screen_repair compose (the route's safety gate)
# ---------------------------------------------------------------------------

def test_screen_repair_rejects_macro_introduced_by_augment():
    parts = split_deck(_DECK)
    evil = "\\section{Intro}\n\\begin{frame}{Intro}\n  \\input{/etc/passwd}\n\\end{frame}"
    proposed = replace_section(parts.tex, parts.sections[0], evil)
    accepted, warnings = screen_repair(_DECK, proposed)
    assert accepted is None
    assert warnings and "unsafe" in warnings[0].lower()


def test_screen_repair_accepts_clean_augment():
    parts = split_deck(_DECK)
    clean = "\\section{Intro}\n\\begin{frame}{Intro}\n  \\begin{itemize}\\item deepened\\end{itemize}\n\\end{frame}"
    proposed = replace_section(parts.tex, parts.sections[0], clean)
    accepted, warnings = screen_repair(_DECK, proposed)
    assert accepted == proposed


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def test_augment_system_prompt_within_limit_and_op_specific():
    big_macros = "\n".join(f"  \\macro{i}{{arg}} — desc {i}" for i in range(400))
    for op in ("deepen", "table", "new_section"):
        p = augment_system_prompt("residents", operation=op, macros_block=big_macros, cite_mode="bib")
        assert len(p) <= SYSTEM_PROMPT_LIMIT
        assert "residents" in p
    assert "DEEPEN" in augment_system_prompt("x", operation="deepen")
    assert "TABLE" in augment_system_prompt("x", operation="table")
    assert "NEW SECTION" in augment_system_prompt("x", operation="new_section")


def test_build_augment_message_wraps_existing_as_source():
    msg = build_augment_message(
        topic="Schizophrenia",
        operation="deepen",
        instruction="deepen the treatment part",
        existing_excerpt="\\section{Treatment}\\begin{frame}{T}\\end{frame}",
        outline_titles=["Intro", "Treatment"],
        candidate_bib_block="  smith2020 — Smith 2020, A study",
    )
    assert "<existing>" in msg and "</existing>" in msg
    assert "deepen the treatment part" in msg
    assert "smith2020" in msg
    assert "Intro" in msg and "Treatment" in msg


def test_augment_max_source_chars_is_sane():
    assert 1000 < AUGMENT_MAX_SOURCE_CHARS <= 200_000


# ---------------------------------------------------------------------------
# deck_counts + nested-brace titles
# ---------------------------------------------------------------------------

def test_deck_counts_ignores_comments():
    counts = deck_counts(_DECK)
    # Two \section, three \begin{frame} (Outline + Intro + Methods; the references
    # frame is the closing tail), titlepage uses \frame{} not \begin{frame}.
    assert counts["sections"] == 2
    assert counts["frames"] == 4  # Outline, Intro, Methods, References
    # A commented-out frame must not be counted.
    commented = _DECK.replace("\\section{Methods}", "% \\begin{frame}{ghost}\n\\section{Methods}")
    assert deck_counts(commented)["frames"] == 4


def test_section_title_handles_nested_braces():
    deck = _DECK.replace("\\section{Intro}", "\\section{The \\textbf{bold} intro}")
    parts = split_deck(deck)
    assert parts.sections[0].title == "The \\textbf{bold} intro"
