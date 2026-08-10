"""Unit tests for docgen's pure article logic — sanitize / assemble / validate.

Self-contained: imports only ``docgen.article`` / ``docgen.prompts`` (which pull
in ``deckgen.latex`` but never ``requests`` or the app). Not part of the app's
hermetic suite; run from the repo root with:  python -m pytest docgen/tests/ -v
"""
from __future__ import annotations

from deckgen.latex import SectionOutput
from docgen.article import (
    ARTICLE,
    DocMeta,
    assemble_article,
    sanitize_article_section,
    validate_article,
)
from docgen.prompts import (
    SYSTEM_PROMPT_LIMIT,
    augment_system_prompt,
    section_system_prompt,
)


def _good_article_tex() -> str:
    secs = [
        SectionOutput(title="Intro", body="\\section{Intro}\nSome grounded prose (source: a.md)."),
        SectionOutput(title="Body", body="\\section{Body}\nMore prose.\n\\begin{itemize}\n\\item x\n\\end{itemize}"),
    ]
    return assemble_article(secs, DocMeta(title="My Report", author="Dr X"))


# --------------------------------------------------------------------------- #
# sanitize_article_section
# --------------------------------------------------------------------------- #

def test_sanitize_keeps_prose_section():
    src = "\\section{Intro}\nA paragraph of prose.\n\\begin{itemize}\n\\item a\n\\end{itemize}"
    out = sanitize_article_section(src)
    assert "\\section{Intro}" in out
    assert "A paragraph of prose." in out
    assert "\\item a" in out


def test_sanitize_does_not_require_a_frame():
    # The Beamer gate (must contain \begin{frame}) must NOT apply to an article.
    src = "\\section{Discussion}\nPure prose with no frames at all."
    assert sanitize_article_section(src) != ""


def test_sanitize_strips_fences_and_preamble_lines():
    src = (
        "```latex\n"
        "\\documentclass{article}\n"
        "\\usepackage{amsmath}\n"
        "\\begin{document}\n"
        "\\maketitle\n"
        "\\tableofcontents\n"
        "\\section{Methods}\n"
        "Prose body.\n"
        "\\end{document}\n"
        "```"
    )
    out = sanitize_article_section(src)
    assert "\\documentclass" not in out
    assert "\\usepackage" not in out
    assert "\\begin{document}" not in out
    assert "\\maketitle" not in out
    assert "\\tableofcontents" not in out
    assert "```" not in out
    assert out.startswith("\\section{Methods}")


def test_sanitize_returns_empty_when_no_section():
    # A server fallback / loose prose with no \section is rejected (the article gate).
    assert sanitize_article_section("The agent did not produce a final answer.") == ""


def test_sanitize_is_idempotent():
    src = "prose before\n\\section{S}\nbody"
    once = sanitize_article_section(src)
    assert sanitize_article_section(once) == once


# --------------------------------------------------------------------------- #
# assemble_article
# --------------------------------------------------------------------------- #

def test_assemble_single_article_document_and_meta():
    tex = _good_article_tex()
    assert tex.count("\\documentclass") == 1
    assert "\\documentclass[11pt]{article}" in tex
    assert tex.count("\\begin{document}") == 1
    assert tex.count("\\end{document}") == 1
    assert "\\maketitle" in tex
    assert "\\title{My Report}" in tex
    assert "\\author{Dr X}" in tex
    assert "\\section{Intro}" in tex
    # No Beamer artifacts leaked in.
    assert "\\documentclass{beamer}" not in tex
    assert "\\begin{frame}" not in tex


def test_assemble_default_date_is_today():
    tex = assemble_article([], DocMeta(title="T"))
    assert "\\date{\\today}" in tex


# --------------------------------------------------------------------------- #
# validate_article
# --------------------------------------------------------------------------- #

def test_validate_clean_article_has_no_warnings():
    assert validate_article(_good_article_tex()) == []


def test_validate_flags_missing_document_env():
    warns = validate_article("\\section{S}\nbody")
    assert any("document environment" in w for w in warns)


def test_validate_flags_no_section():
    tex = assemble_article([], DocMeta(title="T"))
    warns = validate_article(tex)
    assert any("No \\section" in w for w in warns)


def test_validate_flags_dangerous_macros():
    body = "\\section{S}\nProse then \\write18{rm -rf /} danger."
    tex = assemble_article([SectionOutput(title="S", body=body)], DocMeta(title="T"))
    warns = validate_article(tex)
    assert any("unsafe LaTeX" in w for w in warns)
    assert any("\\write18" in w for w in warns)


def test_validate_never_warns_about_frames():
    # An article legitimately has zero frames — no "No frames" / "Unbalanced
    # frame" warning may appear (that is Beamer-only).
    warns = validate_article(_good_article_tex())
    assert not any("frame" in w.lower() for w in warns)


def test_validate_flags_invented_citation_key():
    body = "\\section{S}\nClaim \\autocite{ghost2099}."
    tex = assemble_article([SectionOutput(title="S", body=body)], DocMeta(title="T"))
    warns = validate_article(tex, generated_tex=body, known_bib_keys={"real2020"})
    assert any("ghost2099" in w for w in warns)


# --------------------------------------------------------------------------- #
# strategy + prompts
# --------------------------------------------------------------------------- #

def test_article_strategy_identity_and_checks():
    assert ARTICLE.name == "article"
    assert "\\documentclass[11pt]{article}" in ARTICLE.preamble_template
    assert ARTICLE.structural_checks("no sections here") != []
    assert ARTICLE.structural_checks("\\section{S}") == []


def test_section_prompt_is_prose_oriented_and_within_cap():
    p = section_system_prompt("clinicians", cite_mode="bib", macros_block="\\newcommand{\\x}{y}")
    assert len(p) <= SYSTEM_PROMPT_LIMIT
    assert "article" in p.lower()
    assert "\\begin{frame}" not in p           # no slides
    assert "\\autocite" in p or "\\cite" in p   # bib rule applied


def test_augment_prompt_reformat_forbids_content_change():
    p = augment_system_prompt("clinicians", operation="reformat")
    assert len(p) <= SYSTEM_PROMPT_LIMIT
    assert "REFORMAT" in p
    assert "WITHOUT changing its content" in p


# --------------------------------------------------------------------------- #
# screen_article_repair (augment safety gate)
# --------------------------------------------------------------------------- #

def test_screen_accepts_clean_article_change():
    from docgen.review import screen_article_repair
    orig = _good_article_tex()
    changed = orig.replace("More prose.", "More prose, now deepened with detail.")
    accepted, warns = screen_article_repair(orig, changed)
    assert accepted == changed
    assert not any("Discarded" in w for w in warns)


def test_screen_rejects_new_dangerous_macro():
    from docgen.review import screen_article_repair
    orig = _good_article_tex()
    bad = orig.replace("More prose.", "More prose.\\write18{rm -rf /}")
    accepted, warns = screen_article_repair(orig, bad)
    assert accepted is None
    assert any("unsafe" in w for w in warns)


def test_screen_rejects_added_occurrence_of_existing_macro():
    """2026-07-22 audit M4 (parity with deckgen): an article already using one
    \\input must not license a change that adds another occurrence."""
    from docgen.review import screen_article_repair
    orig = _good_article_tex().replace(
        "More prose.", "More prose.\n\\input{shared-preamble}")
    bad = orig.replace(
        "\\input{shared-preamble}",
        "\\input{shared-preamble}\n\\input{/etc/passwd}")
    accepted, warns = screen_article_repair(orig, bad)
    assert accepted is None
    assert any("unsafe" in w for w in warns)


def test_screen_allows_same_count_of_existing_macro():
    from docgen.review import screen_article_repair
    orig = _good_article_tex().replace(
        "More prose.", "More prose.\n\\input{shared-preamble}")
    moved = orig.replace("\\input{shared-preamble}", "\\input{shared-fixed}")
    accepted, warns = screen_article_repair(orig, moved)
    assert accepted == moved


def test_screen_rejects_change_that_drops_all_sections():
    from docgen.review import screen_article_repair
    orig = _good_article_tex()
    # Strip every \section — an article with none must be refused (the gate).
    broken = orig.replace("\\section{Intro}", "").replace("\\section{Body}", "")
    accepted, warns = screen_article_repair(orig, broken)
    assert accepted is None
    assert any("no \\section" in w for w in warns)


def test_screen_noop_when_identical_or_empty():
    from docgen.review import screen_article_repair
    orig = _good_article_tex()
    assert screen_article_repair(orig, orig)[0] is None
    assert screen_article_repair(orig, "")[0] is None
    assert screen_article_repair(orig, None)[0] is None
