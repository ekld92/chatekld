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


@pytest.mark.parametrize("macro", [
    "\\verbatiminput{/Users/x/.ssh/id_rsa}",
    "\\lstinputlisting{/etc/passwd}",
    "\\InputIfFileExists{/etc/passwd}{}{}",
    "\\ShellEscape{rm -rf /}",
    "\\CatchFileEdef{\\x}{/etc/passwd}{}",
    # 2026-07-20 audit finding 3: sibling file-readers the earlier list missed.
    "\\VerbatimInput{/Users/x/.ssh/id_rsa}",
    "\\import{/etc/}{passwd}",
    "\\subimport{/etc/}{passwd}",
    "\\includepdf{/etc/passwd}",
    "\\inputminted{text}{/etc/passwd}",
])
def test_screen_repair_rejects_added_file_read_macros(macro):
    # 2026-07-20 audit M2: these plain control words are the same file-read/
    # exec class as \input and must be refused when INTRODUCED by a repair —
    # a prompt-injected compile-fix could otherwise typeset a secret file.
    parts = split_deck(_DECK)
    evil = f"\\section{{Intro}}\n\\begin{{frame}}{{Intro}}\n  {macro}\n\\end{{frame}}"
    proposed = replace_section(parts.tex, parts.sections[0], evil)
    accepted, warnings = screen_repair(_DECK, proposed)
    assert accepted is None
    assert warnings and "unsafe" in warnings[0].lower()


def test_screen_repair_rejects_added_occurrence_of_existing_macro():
    """2026-07-22 audit M4: a deck legitimately containing one \\input must
    NOT license a repair that adds MORE \\input occurrences — the old
    name-set diff saw an empty 'introduced' set and let the second
    occurrence (with an attacker-chosen path) through."""
    parts = split_deck(_DECK)
    original = replace_section(
        parts.tex, parts.sections[0],
        "\\section{Intro}\n\\begin{frame}{Intro}\n  \\input{preamble-extra}\n\\end{frame}",
    )
    o_parts = split_deck(original)
    evil = ("\\section{Intro}\n\\begin{frame}{Intro}\n  \\input{preamble-extra}\n"
            "  \\input{/Users/x/.ssh/id_rsa}\n\\end{frame}")
    proposed = replace_section(o_parts.tex, o_parts.sections[0], evil)
    accepted, warnings = screen_repair(original, proposed)
    assert accepted is None
    assert warnings and "unsafe" in warnings[0].lower()


def test_screen_repair_allows_same_count_of_existing_macro():
    """Count-equal is allowed: moving/re-arguing an existing occurrence is a
    legitimate edit (e.g. a repair fixing the path of the deck's own \\input);
    only an INCREASE is refused."""
    parts = split_deck(_DECK)
    original = replace_section(
        parts.tex, parts.sections[0],
        "\\section{Intro}\n\\begin{frame}{Intro}\n  \\input{preamble-extra}\n\\end{frame}",
    )
    o_parts = split_deck(original)
    moved = ("\\section{Intro}\n\\begin{frame}{Intro}\n  \\input{preamble-fixed}\n"
             "  \\begin{itemize}\\item deepened\\end{itemize}\n\\end{frame}")
    proposed = replace_section(o_parts.tex, o_parts.sections[0], moved)
    accepted, warnings = screen_repair(original, proposed)
    assert accepted == proposed


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def test_augment_system_prompt_within_limit_and_op_specific():
    big_macros = "\n".join(f"  \\macro{i}{{arg}} — desc {i}" for i in range(400))
    for op in ("deepen", "table", "new_section", "reformat"):
        p = augment_system_prompt("residents", operation=op, macros_block=big_macros, cite_mode="bib")
        assert len(p) <= SYSTEM_PROMPT_LIMIT
        assert "residents" in p
    assert "DEEPEN" in augment_system_prompt("x", operation="deepen")
    assert "TABLE" in augment_system_prompt("x", operation="table")
    assert "NEW SECTION" in augment_system_prompt("x", operation="new_section")
    reformat = augment_system_prompt("x", operation="reformat")
    assert "REFORMAT" in reformat
    # The content-preservation constraint is the load-bearing part of the rule.
    assert "WITHOUT changing its content" in reformat


def test_augment_prompt_never_leaks_doubled_brace_escapes():
    """The op rules are appended AFTER .format(), so braces in them are literal:
    a doubled {{...}} escape reaches the model verbatim and teaches it broken
    LaTeX like \\begin{{frame}} (Known Issues item 9, fixed 2026-08-18)."""
    for op in ("deepen", "table", "new_section", "reformat"):
        p = augment_system_prompt("x", operation=op)
        assert "{{" not in p and "}}" not in p, f"doubled braces leaked in {op!r}"


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


# --- replace_sections (piecewise splice, Batch E 2026-07-22) ----------------

def test_replace_sections_back_to_front_invariants():
    from deckgen.augment import replace_sections
    parts = split_deck(_DECK)
    secs = parts.sections
    assert len(secs) >= 2
    # Identity: splicing every section's own body back returns the original.
    ident = replace_sections(_DECK, [(s, s.body) for s in secs])
    assert ident == _DECK
    # Multi-edit: replace first and last, leave the middle untouched; bytes
    # outside the edited spans are identical.
    new_first = "\\section{Intro}\n\\begin{frame}{X}\n NEW1 \\end{frame}\n\n"
    new_last = "\\section{Fin}\n\\begin{frame}{Y}\n NEW2 \\end{frame}\n\n"
    out = replace_sections(_DECK, [(secs[0], new_first), (secs[-1], new_last)])
    assert "NEW1" in out and "NEW2" in out
    # The pre-section opening and the closing survive verbatim.
    assert out.startswith(_DECK[:secs[0].start])
    assert out.endswith(_DECK[secs[-1].end:])
    # Order independence: passing the edits pre-sorted forward gives the same
    # result (the function sorts descending internally).
    out2 = replace_sections(_DECK, [(secs[-1], new_last), (secs[0], new_first)])
    assert out == out2
