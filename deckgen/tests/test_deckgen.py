"""Unit tests for deckgen's pure logic — outline parsing and .tex assembly.

These are intentionally self-contained: they import only ``deckgen.outline`` and
``deckgen.assemble`` (neither pulls in ``requests``) and never touch the app or a
running server. They are NOT part of the app's hermetic pytest suite; run them
from the repo root with:  python -m pytest deckgen/tests/ -v
"""
from __future__ import annotations

from deckgen.assemble import (
    DeckMeta,
    SectionOutput,
    assemble,
    latex_escape_metadata,
    sanitize_section,
    validate,
)
from deckgen.outline import parse_outline


# ---------------------------------------------------------------------------
# parse_outline
# ---------------------------------------------------------------------------

def test_parse_outline_plain_json():
    text = '[{"title": "Intro", "points": ["a", "b"]}, {"title": "Methods", "points": ["c"]}]'
    secs = parse_outline(text)
    assert [s.title for s in secs] == ["Intro", "Methods"]
    assert secs[0].points == ["a", "b"]
    assert secs[1].points == ["c"]


def test_parse_outline_json_wrapped_in_prose_and_fences():
    text = (
        "Sure, here is the outline:\n\n"
        "```json\n"
        '[{"title": "Definition", "points": ["x"]}, {"title": "Epidemiology", "points": []}]\n'
        "```\n"
        "Hope that helps!"
    )
    secs = parse_outline(text)
    assert [s.title for s in secs] == ["Definition", "Epidemiology"]
    assert secs[0].points == ["x"]


def test_parse_outline_json_alternate_keys():
    text = '[{"name": "Intro", "bullets": ["a"]}, {"section": "Wrap-up", "items": ["z"]}]'
    secs = parse_outline(text)
    assert [s.title for s in secs] == ["Intro", "Wrap-up"]
    assert secs[0].points == ["a"]
    assert secs[1].points == ["z"]


def test_parse_outline_atx_heading_fallback():
    text = "## Intro\n- point a\n- point b\n## Methods\n- point c"
    secs = parse_outline(text)
    assert [s.title for s in secs] == ["Intro", "Methods"]
    assert secs[0].points == ["point a", "point b"]
    assert secs[1].points == ["point c"]


def test_parse_outline_numbered_with_indented_points():
    text = "1. Intro\n   - a\n   - b\n2. Methods\n   - c"
    secs = parse_outline(text)
    assert [s.title for s in secs] == ["Intro", "Methods"]
    assert secs[0].points == ["a", "b"]
    assert secs[1].points == ["c"]


def test_parse_outline_flat_bullets_become_sections():
    text = "- Intro\n- Methods\n- Conclusion"
    secs = parse_outline(text)
    assert [s.title for s in secs] == ["Intro", "Methods", "Conclusion"]
    assert all(s.points == [] for s in secs)


def test_parse_outline_empty_or_garbage():
    assert parse_outline("") == []
    assert parse_outline("   \n  ") == []


# ---------------------------------------------------------------------------
# sanitize_section
# ---------------------------------------------------------------------------

def test_sanitize_strips_code_fences_and_keeps_body():
    raw = (
        "```latex\n"
        "\\section{Intro}\n"
        "\\begin{frame}{Intro}\n"
        "  \\begin{itemize}\\item a\\end{itemize}\n"
        "\\end{frame}\n"
        "```"
    )
    out = sanitize_section(raw)
    assert "```" not in out
    assert out.startswith("\\section{Intro}")
    assert out.rstrip().endswith("\\end{frame}")
    assert "\\item a" in out


def test_sanitize_drops_preamble_and_document_scaffold():
    raw = (
        "\\documentclass{beamer}\n"
        "\\usepackage{amsmath}\n"
        "\\title{Whole Deck}\n"
        "\\begin{document}\n"
        "\\frame{\\titlepage}\n"
        "\\section{Body}\n"
        "\\begin{frame}{Body}\n\\end{frame}\n"
        "\\end{document}\n"
    )
    out = sanitize_section(raw)
    assert "\\documentclass" not in out
    assert "\\usepackage" not in out
    assert "\\begin{document}" not in out
    assert "\\end{document}" not in out
    assert "\\titlepage" not in out
    assert "\\section{Body}" in out
    assert "\\begin{frame}{Body}" in out


def test_sanitize_trims_leading_and_trailing_prose():
    raw = (
        "Here are the frames for this section:\n"
        "\\section{S}\n\\begin{frame}{S}\n\\end{frame}\n"
        "Let me know if you want changes."
    )
    out = sanitize_section(raw)
    assert out.startswith("\\section{S}")
    assert out.endswith("\\end{frame}")
    assert "Let me know" not in out
    assert "Here are the frames" not in out


def test_sanitize_is_idempotent():
    raw = "```\n\\section{S}\n\\begin{frame}{S}\n\\end{frame}\n```\ntrailing"
    once = sanitize_section(raw)
    twice = sanitize_section(once)
    assert once == twice


def test_sanitize_empty():
    assert sanitize_section("") == ""
    assert sanitize_section("   ") == ""


def test_sanitize_returns_empty_when_no_frame():
    # Prose / server-fallback message with no Beamer frame -> treated as empty so
    # the caller inserts a placeholder rather than leaking loose text.
    assert sanitize_section("Agent did not produce a final answer.") == ""
    # A bare \section with no frame is also not usable.
    assert sanitize_section("\\section{Orphan}") == ""


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def _good_tex() -> str:
    secs = [SectionOutput(title="S", body="\\section{S}\n\\begin{frame}{S}\n\\end{frame}")]
    return assemble(secs, DeckMeta(title="T"))


def test_validate_clean_deck_has_no_warnings():
    assert validate(_good_tex()) == []


def test_validate_flags_unbalanced_frames():
    bad = _good_tex().replace("\\end{frame}", "", 1)
    warns = validate(bad)
    assert any("Unbalanced frame" in w for w in warns)


def test_validate_flags_missing_document_env():
    body = "\\section{S}\n\\begin{frame}{S}\n\\end{frame}"
    warns = validate(body)
    assert any("document environment" in w for w in warns)


def test_validate_flags_no_section():
    secs = [SectionOutput(title="S", body="\\begin{frame}{S}\n\\end{frame}")]
    tex = assemble(secs, DeckMeta(title="T"))
    warns = validate(tex)
    assert any("No \\section" in w for w in warns)


def test_validate_flags_dangerous_macros():
    body = "\\section{S}\n\\begin{frame}{S}\n\\write18{rm -rf /}\n\\end{frame}"
    tex = assemble([SectionOutput(title="S", body=body)], DeckMeta(title="T"))
    warns = validate(tex)
    assert any("unsafe LaTeX" in w for w in warns)
    assert any("\\write18" in w for w in warns)


def test_validate_clean_deck_has_no_dangerous_macro_warning():
    # The preamble template itself must not trip the scanner.
    assert not any("unsafe LaTeX" in w for w in validate(_good_tex()))


# ---------------------------------------------------------------------------
# assemble / metadata
# ---------------------------------------------------------------------------

def test_assemble_has_single_document_env_and_substitutes_meta():
    secs = [SectionOutput(title="S", body="\\section{S}\n\\begin{frame}{S}\n\\end{frame}")]
    tex = assemble(secs, DeckMeta(title="My Topic", author="Dr X", theme="metropolis"))
    assert tex.count("\\begin{document}") == 1
    assert tex.count("\\end{document}") == 1
    assert "\\title{My Topic}" in tex
    assert "\\author{Dr X}" in tex
    assert "\\usetheme{metropolis}" in tex
    assert "\\section{S}" in tex


def test_assemble_default_date_is_today():
    tex = assemble([], DeckMeta(title="T"))
    assert "\\date{\\today}" in tex


def test_latex_escape_metadata():
    assert latex_escape_metadata("A & B_C 50%") == r"A \& B\_C 50\%"
    assert latex_escape_metadata("#1 {x}") == r"\#1 \{x\}"
