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
import pytest

from deckgen.outline import (
    OutlineError,
    _looks_like_outline,
    parse_outline,
    request_outline,
)
from deckgen.result import ChatResult


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
# request_outline — instructions-as-outline salvage fallback
# ---------------------------------------------------------------------------

class _StubClient:
    """Minimal ChatRunner duck-type returning a canned ChatResult."""

    def __init__(self, result):
        self._result = result
        self.calls = 0

    def chat(self, message, **kwargs):
        self.calls += 1
        return self._result


def test_looks_like_outline_detects_structure():
    assert _looks_like_outline("1. Intro\n2. Methods")
    assert _looks_like_outline("## Background")
    assert _looks_like_outline("- a\n- b")
    assert _looks_like_outline("Section 1: Foo")


def test_looks_like_outline_rejects_prose():
    assert not _looks_like_outline("Please make a nice deck about schizotypy.")
    assert not _looks_like_outline("")


def test_request_outline_falls_back_to_instructions():
    # Model returned nothing usable (empty text — the iteration-cap failure mode).
    client = _StubClient(ChatResult())
    infos = []
    sections, result = request_outline(
        client,
        topic="Schizotypy",
        instructions="1. Definition\n2. Epidemiology\n3. Clinical features",
        provider="ollama", model="m", embed="e",
        max_iters=4, temperature=None, max_sections=8,
        on_event=lambda ev: infos.append(ev),
    )
    assert [s.title for s in sections] == ["Definition", "Epidemiology", "Clinical features"]
    assert any("instructions" in ev.get("info", "") for ev in infos)


def test_request_outline_fallback_respects_max_sections():
    client = _StubClient(ChatResult())
    instructions = "\n".join(f"{i}. Section {i}" for i in range(1, 9))
    sections, _ = request_outline(
        client, topic="T", instructions=instructions,
        provider="ollama", model="m", embed="e",
        max_iters=4, temperature=None, max_sections=3,
    )
    assert len(sections) == 3


def test_request_outline_caps_model_reply_to_max_sections():
    # Improvement plan 0.2: the PRIMARY path (a usable model reply) must be
    # capped too, not just the instructions fallback — a chatty model that
    # returns 15 sections must not multiply generation time/cost.
    import json as _json
    reply = _json.dumps([
        {"title": f"Section {i}", "points": ["p"]} for i in range(1, 16)
    ])
    client = _StubClient(ChatResult(text=reply))
    sections, _ = request_outline(
        client, topic="T", instructions="",
        provider="ollama", model="m", embed="e",
        max_iters=4, temperature=None, max_sections=8,
    )
    assert len(sections) == 8
    assert [s.title for s in sections] == [f"Section {i}" for i in range(1, 9)]


def test_request_outline_raises_when_no_fallback_structure():
    client = _StubClient(ChatResult())
    with pytest.raises(OutlineError):
        request_outline(
            client, topic="X",
            instructions="just some prose with no outline at all",
            provider="ollama", model="m", embed="e",
            max_iters=4, temperature=None, max_sections=8,
        )


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


# ---------------------------------------------------------------------------
# chat_with_retry — rate-limit hint honoring (field-reported 2026-07: deck
# sections retried at 3-4s against a provider "try again in 5.764s" hint and
# burned every attempt on guaranteed second 429s)


def test_retry_after_hint_parsing():
    from deckgen.retry import _retry_after_hint_s

    assert _retry_after_hint_s(
        "rate_limit openai http=429: … Please try again in 5.764s. Visit …"
    ) == 5.764
    assert _retry_after_hint_s("Try again in 42 s") == 42.0
    assert _retry_after_hint_s("no hint at all") is None
    assert _retry_after_hint_s(None) is None
    assert _retry_after_hint_s("") is None


def test_chat_with_retry_floors_wait_on_rate_limit_hint(monkeypatch):
    from deckgen import retry as retry_mod
    from deckgen.retry import chat_with_retry

    sleeps = []
    monkeypatch.setattr(
        retry_mod, "_sleep_cancellable", lambda s, cancel: sleeps.append(s)
    )

    class _Client:
        def __init__(self):
            self.calls = 0

        def chat(self, message, on_event=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ChatResult(error=(
                    "rate_limit openai http=429: Rate limit reached… "
                    "Please try again in 3.5s."
                ))
            return ChatResult(text="ok")

    client = _Client()
    result = chat_with_retry(
        client, "msg", max_attempts=2, retry_backoff_s=1.0,
    )
    assert result.ok
    assert client.calls == 2
    # Linear backoff alone would wait 1s; the 3.5s hint (+0.5 margin) floors it.
    assert sleeps == [4.0]


def test_chat_with_retry_keeps_linear_backoff_without_hint(monkeypatch):
    from deckgen import retry as retry_mod
    from deckgen.retry import chat_with_retry

    sleeps = []
    monkeypatch.setattr(
        retry_mod, "_sleep_cancellable", lambda s, cancel: sleeps.append(s)
    )

    class _Client:
        def __init__(self):
            self.calls = 0

        def chat(self, message, on_event=None, **kwargs):
            self.calls += 1
            if self.calls < 3:
                return ChatResult(error="transient backend hiccup")
            return ChatResult(text="ok")

    result = chat_with_retry(_Client(), "msg", max_attempts=3, retry_backoff_s=2.0)
    assert result.ok
    assert sleeps == [2.0, 4.0]


# ---------------------------------------------------------------------------
# comment_out_missing_graphics — line safety (the naive whole-match "% <cmd>"
# replacement swallowed everything after the command on the same line, e.g. a
# closing brace or \end{frame}, breaking decks it was meant to protect)


def test_comment_out_alone_on_line():
    from deckgen.assemble import comment_out_missing_graphics

    tex = "before\n\\includegraphics{figures/missing.png}\nafter"
    out = comment_out_missing_graphics(tex, set())
    assert out.splitlines() == [
        "before",
        "% \\includegraphics{figures/missing.png} % Figure not found: missing.png",
        "after",
    ]


def test_comment_out_preserves_content_sharing_the_line():
    from deckgen.assemble import comment_out_missing_graphics

    tex = "\\frame{\\includegraphics{missing.png}}"
    out = comment_out_missing_graphics(tex, set())
    lines = out.splitlines()
    # The closing brace must survive on its own line — a newline is just a
    # space to LaTeX here, so the frame stays balanced.
    assert lines[0] == "\\frame{"
    assert lines[1].startswith("% \\includegraphics{missing.png}")
    assert lines[2] == "}"
    # Structural invariant: same number of braces before and after.
    assert out.count("{") == tex.count("{") and out.count("}") == tex.count("}")


def test_comment_out_mixed_resolved_and_missing_on_one_line():
    from deckgen.assemble import comment_out_missing_graphics

    tex = "\\includegraphics{keep.png} \\includegraphics{drop.png}"
    out = comment_out_missing_graphics(tex, {"keep.png"})
    lines = out.splitlines()
    assert lines[0] == "\\includegraphics{keep.png} "
    assert lines[1].startswith("% \\includegraphics{drop.png}")


def test_comment_out_resolved_figures_untouched():
    from deckgen.assemble import comment_out_missing_graphics

    tex = "x\n\\includegraphics[width=0.8\\textwidth]{figures/brain.png}\ny"
    assert comment_out_missing_graphics(tex, {"brain.png"}) == tex


def test_extract_graphics_keys():
    from deckgen.assemble import extract_graphics_keys

    tex = (
        "\\includegraphics{a.png}\n"
        "\\includegraphics[width=0.5\\textwidth]{figures/b.jpeg}\n"
        "\\includegraphics*[page=2]{sub/c.pdf}"
    )
    assert extract_graphics_keys(tex) == ["a.png", "figures/b.jpeg", "sub/c.pdf"]


# ---------------------------------------------------------------------------
# prompt brace rendering — the cite/image rules are appended AFTER .format(),
# so a doubled {{key}} reached the model verbatim (teaching it broken LaTeX)


def test_prompts_render_single_braces():
    from deckgen.prompts import augment_system_prompt, section_system_prompt

    for prompt in (
        section_system_prompt("clinicians", cite_mode="bib"),
        augment_system_prompt("clinicians", cite_mode="bib"),
    ):
        assert "\\citefoot{key}" in prompt
        assert "{figures/basename}" in prompt
        assert "{{" not in prompt and "}}" not in prompt


def test_image_rule_gated_by_images_enabled():
    from deckgen.prompts import augment_system_prompt, section_system_prompt

    assert "includegraphics" in section_system_prompt("a", images_enabled=True)
    assert "includegraphics" not in section_system_prompt("a", images_enabled=False)
    assert "includegraphics" not in augment_system_prompt("a", images_enabled=False)
