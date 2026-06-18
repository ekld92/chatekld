"""Pure-logic tests for the template-aware deckgen layer.

No server, no ``requests`` — covers split/scan/bib/scaffold/assemble + the
prompt wiring. Run with:  python -m pytest deckgen/tests/ -v
"""
from __future__ import annotations

import os

import pytest

from deckgen.assemble import (
    SectionOutput,
    assemble_with_template,
    extract_cite_keys,
    sanitize_section,
    validate,
)
from deckgen.prompts import (
    SYSTEM_PROMPT_LIMIT,
    build_section_message,
    section_system_prompt,
)
from deckgen.scaffold import ScaffoldError, scaffold_deck, slugify
from deckgen.template import (
    TemplateError,
    bib_candidates_block,
    find_suite_root,
    load_template_parts,
    macro_cheatsheet,
    relevant_bib_keys,
    resolve_bib,
    scan_macros,
    split_template,
    strip_comments,
)

_STY = r"""\NeedsTeXFormat{LaTeX2e}
\ProvidesPackage{mystyle}
\newcommand{\citefoot}[1]{\cite{#1}}
\newcommand{\commonlogo}[2][]{\includegraphics[#1]{../common/fig/#2}}
\newcommand{\doctoralSchool}[1]{\def\@doctoralSchool{#1}}
"""

_BIB = r"""@article{smith2020depression,
  author = {Smith, Jane and Doe, John},
  year = {2020},
  title = {Digital therapy for depression},
}
@book{jones2019methods,
  author = {Jones, Alice},
  year = {2019},
  title = {Trial methods},
}
"""

_TEMPLATE = r"""\documentclass[aspectratio=169]{beamer}
\usepackage{../common/mystyle}
\usetheme{Boadilla}
% \usepackage{commented-out-package}
\addbibresource{../refs.bib}
\title{My Title}
\author{Me}
\begin{document}
\AtBeginSection[]{\begin{frame}{Plan}\tableofcontents[currentsection]\end{frame}}
\begin{frame}\titlepage\end{frame}
\begin{frame}{Outline}\tableofcontents\end{frame}
\section{Example One}
\begin{frame}{Example}\begin{itemize}\item drop me\end{itemize}\end{frame}
\section{Example Two}
\begin{frame}{Example2}content\end{frame}
\appendix
\section*{References}
\begin{frame}[allowframebreaks]{References}\printbibliography\end{frame}
\end{document}
"""


@pytest.fixture
def suite(tmp_path):
    """A miniature LaTeX suite: <root>/common/mystyle.sty, refs.bib, deck/template.tex."""
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "mystyle.sty").write_text(_STY, encoding="utf-8")
    (tmp_path / "refs.bib").write_text(_BIB, encoding="utf-8")
    (tmp_path / "deck").mkdir()
    tpl = tmp_path / "deck" / "template.tex"
    tpl.write_text(_TEMPLATE, encoding="utf-8")
    return {"root": str(tmp_path), "template": str(tpl)}


# --- split_template --------------------------------------------------------

def test_split_template_parts_and_drops_examples():
    preamble, opening, closing = split_template(_TEMPLATE)
    assert "\\documentclass" in preamble
    assert "\\begin{document}" not in preamble
    # Opening keeps the title/outline scaffold + AtBeginSection...
    assert "\\begin{document}" in opening
    assert "\\AtBeginSection" in opening
    assert "\\titlepage" in opening
    # ...but NOT the example sections.
    assert "Example One" not in opening
    assert "drop me" not in opening
    # Closing keeps the appendix/references tail through \end{document}.
    assert closing.lstrip().startswith("\\appendix")
    assert "\\printbibliography" in closing
    assert closing.rstrip().endswith("\\end{document}")


def test_split_template_requires_document_env():
    with pytest.raises(TemplateError):
        split_template(r"\documentclass{beamer}\title{x}")  # no \begin{document}


def test_mask_comments_preserves_length_and_blanks_comments():
    from deckgen.template import mask_comments
    src = "a % b\nc\\% d % e\n"
    masked = mask_comments(src)
    assert len(masked) == len(src)          # positions must map 1:1
    assert "b" not in masked and "e" not in masked  # comment bodies blanked
    assert "c\\% d" in masked               # escaped \% kept (not a comment)


def test_split_template_drops_commented_references_frame():
    """Regression: a fully commented references frame (as in the house
    presentation.tex) must NOT be pulled into the closing — that left an
    unclosed \\begin{frame} before \\end{document} (a non-compiling deck)."""
    tex = (
        "\\documentclass{beamer}\n\\begin{document}\n"
        "\\begin{frame}\\titlepage\\end{frame}\n"
        "\\section{Example}\n\\begin{frame}{x}drop\\end{frame}\n"
        "% \\begin{frame}[allowframebreaks]{References}\n"
        "%   \\printbibliography\n"
        "% \\end{frame}\n"
        "\\end{document}\n"
    )
    _pre, _opening, closing = split_template(tex)
    assert closing.strip() == "\\end{document}"   # commented frame dropped
    assert "\\begin{frame}" not in closing


def test_split_template_ignores_commented_begin_document():
    tex = (
        "\\documentclass{beamer}\n% example: \\begin{document} ... \\end{document}\n"
        "\\begin{document}\n\\section{S}\\begin{frame}{x}y\\end{frame}\n\\end{document}\n"
    )
    pre, _opening, _closing = split_template(tex)
    # The real \begin{document} (not the commented one) bounds the preamble.
    assert "\\documentclass" in pre and "% example" in pre


# --- strip_comments --------------------------------------------------------

def test_strip_comments_removes_line_comments_but_keeps_escaped_percent():
    assert strip_comments("a % comment") == "a "
    assert strip_comments(r"50\% done % note").rstrip() == r"50\% done"


# --- macro scanning --------------------------------------------------------

def test_scan_macros_follows_usepackage_into_sibling_sty(suite):
    parts = load_template_parts(_TEMPLATE, suite["template"])
    names = {m.name for m in parts.macros}
    assert "citefoot" in names
    assert "commonlogo" in names
    # @-internal and boring metadata macros are filtered out.
    assert not any("@" in m.name for m in parts.macros)
    assert "doctoralSchool" not in names


def test_macro_cheatsheet_describes_known_macros(suite):
    parts = load_template_parts(_TEMPLATE, suite["template"])
    sheet = macro_cheatsheet(parts.macros)
    assert "\\citefoot" in sheet
    assert "\\commonlogo" in sheet


def test_scan_macros_picks_up_inline_newcommand():
    pre = r"\documentclass{beamer}" + "\n" + r"\newcommand{\foo}[2]{#1#2}"
    macros = scan_macros(pre, base_dir="")
    foo = [m for m in macros if m.name == "foo"]
    assert foo and foo[0].arity == 2


# --- bibliography ----------------------------------------------------------

def test_resolve_bib_parses_entries(suite):
    idx = resolve_bib(_TEMPLATE, os.path.dirname(suite["template"]))
    assert set(idx) == {"smith2020depression", "jones2019methods"}
    author, year, title = idx["smith2020depression"]
    assert "Smith" in author and year == "2020" and "depression" in title.lower()


def test_resolve_bib_ignores_commented_addbibresource(suite):
    tex = _TEMPLATE.replace(r"\addbibresource{../refs.bib}", r"% \addbibresource{../refs.bib}")
    idx = resolve_bib(tex, os.path.dirname(suite["template"]))
    assert idx == {}


def test_resolve_bib_rejects_absolute_path(tmp_path):
    """An absolute \\addbibresource must not be followed (it would leak an
    arbitrary file's parsed author/title into the prompt)."""
    bib = tmp_path / "abs.bib"
    bib.write_text("@article{x,\n author={A},\n year={2020},\n title={t},\n}\n", encoding="utf-8")
    pre = "\\documentclass{beamer}\n\\addbibresource{%s}\n" % str(bib)
    assert resolve_bib(pre, str(tmp_path)) == {}


def test_relevant_bib_keys_and_candidates_block(suite):
    idx = resolve_bib(_TEMPLATE, os.path.dirname(suite["template"]))
    keys = relevant_bib_keys(idx, "digital depression therapy")
    assert "smith2020depression" in keys
    block = bib_candidates_block(idx, keys)
    assert "smith2020depression" in block


# --- suite root ------------------------------------------------------------

def test_find_suite_root(suite):
    assert find_suite_root(suite["template"]) == os.path.realpath(suite["root"]) or \
        find_suite_root(suite["template"]) == suite["root"]


# --- assemble_with_template ------------------------------------------------

def test_assemble_with_template_injects_sections():
    preamble, opening, closing = split_template(_TEMPLATE)
    secs = [
        SectionOutput(title="Intro", body=sanitize_section(
            r"\section{Intro}\begin{frame}{A}\begin{itemize}\item x\end{itemize}\end{frame}")),
    ]
    tex = assemble_with_template(secs, preamble=preamble, opening=opening, closing=closing)
    assert tex.count("\\begin{document}") == 1
    assert tex.count("\\end{document}") == 1
    assert "\\section{Intro}" in tex
    assert "Example One" not in tex  # template examples were dropped
    assert tex.count("\\begin{frame}") == tex.count("\\end{frame}")


# --- validate citation guard ----------------------------------------------

def test_extract_cite_keys():
    keys = extract_cite_keys(r"\citefoot{a} \cite{b,c} \textcite[12]{d}")
    assert set(keys) == {"a", "b", "c", "d"}


def test_validate_flags_unknown_cite_key():
    generated = r"\begin{frame}{x}\citefoot{realKey} \citefoot{madeUp}\end{frame}"
    tex = "\\documentclass{beamer}\\begin{document}" + generated + "\\end{document}"
    warns = validate(tex, generated_tex=generated, known_bib_keys={"realKey"})
    assert any("madeUp" in w for w in warns)
    assert not any("realKey" in w for w in warns)


def test_validate_no_cite_guard_when_keys_none():
    tex = "\\documentclass{beamer}\\begin{document}\\begin{frame}{x}\\cite{any}\\end{frame}\\end{document}"
    warns = validate(tex)  # known_bib_keys not supplied
    assert not any("not found in the bibliography" in w for w in warns)


def test_validate_dangerous_macro_uses_generated_span_only():
    # A trusted preamble \input must NOT be flagged when a generated span is given.
    tex = r"\input{trusted}\begin{document}\begin{frame}{x}ok\end{frame}\end{document}"
    warns = validate(tex, generated_tex=r"\begin{frame}{x}ok\end{frame}")
    assert not any("unsafe LaTeX" in w for w in warns)


def test_validate_frame_balance_ignores_commented_end_frame():
    """Regression: a commented `% \\end{frame}` is not a real close — validate must
    flag the unbalanced frame rather than counting the comment as balanced."""
    tex = (
        "\\documentclass{beamer}\\begin{document}\n"
        "\\begin{frame}{x}content\n% \\end{frame}\n"
        "\\end{document}"
    )
    warns = validate(tex)
    assert any("Unbalanced frame" in w for w in warns)


def test_validate_ignores_commented_dangerous_macro_and_cite():
    tex = (
        "\\documentclass{beamer}\\begin{document}"
        "\\begin{frame}{x}% \\input{evil} and % \\cite{ghost}\nok\\end{frame}"
        "\\end{document}"
    )
    warns = validate(tex, known_bib_keys=set())
    assert not any("unsafe LaTeX" in w for w in warns)
    assert not any("not found in the bibliography" in w for w in warns)


def test_newdoccommand_arity_not_inflated_by_default_values():
    # \NewDocumentCommand{\x}{O{red} m} -> 2 args (one optional with a default
    # whose letters must not be counted).
    pre = r"\NewDocumentCommand{\hl}{O{red} m}{\textcolor{#1}{#2}}"
    macros = scan_macros(pre, base_dir="")
    hl = [m for m in macros if m.name == "hl"]
    assert hl and hl[0].arity == 2


# --- scaffold --------------------------------------------------------------

def test_scaffold_deck_writes_project(tmp_path):
    (tmp_path / "common").mkdir()
    res = scaffold_deck(str(tmp_path), "my_deck", "\\documentclass{beamer}\n")
    assert os.path.isfile(res["tex_path"])
    assert os.path.isfile(res["makefile_path"])
    assert res["sibling_common"] is True
    mk = open(res["makefile_path"], encoding="utf-8").read()
    assert "DOC = my_deck" in mk
    assert "include ../common/latex-build.mk" in mk


def test_scaffold_rejects_unsafe_slug(tmp_path):
    with pytest.raises(ScaffoldError):
        scaffold_deck(str(tmp_path), "../evil", "x")


def test_scaffold_refuses_existing_without_overwrite(tmp_path):
    scaffold_deck(str(tmp_path), "deck", "one")
    with pytest.raises(ScaffoldError):
        scaffold_deck(str(tmp_path), "deck", "two")
    # overwrite=True succeeds.
    res = scaffold_deck(str(tmp_path), "deck", "two", overwrite=True)
    assert "two" in open(res["tex_path"], encoding="utf-8").read()


def test_scaffold_sibling_common_false(tmp_path):
    res = scaffold_deck(str(tmp_path), "deck", "x")
    assert res["sibling_common"] is False


def test_slugify():
    assert slugify("My Test Deck!") == "my_test_deck"
    assert slugify("   ") == "deck"


# --- prompt wiring ---------------------------------------------------------

def test_section_system_prompt_bib_mode_adds_rule_and_stays_in_budget():
    big_macros = "\n".join(f"  \\m{i}{{arg}} — x" for i in range(500))
    p = section_system_prompt("students", macros_block=big_macros, cite_mode="bib")
    assert "\\citefoot" in p
    assert len(p) <= SYSTEM_PROMPT_LIMIT


def test_build_section_message_includes_candidates():
    from deckgen.outline import Section
    msg = build_section_message(
        topic="t", instructions="", full_outline=[Section("A", ["p"])],
        index=1, title="A", points=["p"], candidate_bib_block="  key1 — Author 2020",
    )
    assert "Candidate references" in msg
    assert "key1" in msg
