"""docgen Markdown long-form core: sanitize / assemble / prompts / generate binding."""
from __future__ import annotations

from deckgen.outline import Section
from deckgen.latex import SectionOutput
from deckgen.result import ChatResult
from docgen.markdown import (
    SYSTEM_PROMPT_LIMIT,
    assemble_markdown,
    generate_section,
    request_outline,
    sanitize_markdown_section,
    section_system_prompt,
)


# --- sanitize --------------------------------------------------------------- #

def test_sanitize_keeps_heading_and_prose():
    out = sanitize_markdown_section("## Background\nSome grounded prose (source: a.md).")
    assert out.startswith("## Background")
    assert "grounded prose" in out


def test_sanitize_unwraps_a_whole_answer_fence_but_keeps_inner_code():
    src = "```markdown\n## Methods\nText.\n\n```python\nx=1\n```\n\nmore.\n```"
    out = sanitize_markdown_section(src)
    assert out.startswith("## Methods")
    assert "```python" in out          # inner code block survives
    assert not out.startswith("```markdown")


def test_sanitize_trims_leading_commentary():
    out = sanitize_markdown_section("Sure! Here is the section:\n\n## Results\nBody.")
    assert out.startswith("## Results")


def test_sanitize_requires_a_heading():
    assert sanitize_markdown_section("The agent did not produce a final answer.") == ""
    assert sanitize_markdown_section("") == ""


def test_sanitize_is_idempotent():
    once = sanitize_markdown_section("intro\n## S\nbody")
    assert sanitize_markdown_section(once) == once


# --- assemble --------------------------------------------------------------- #

def test_assemble_prepends_title_and_joins_sections():
    secs = [
        SectionOutput(title="Intro", body="## Intro\nA."),
        SectionOutput(title="Body", body="## Body\nB."),
    ]
    md = assemble_markdown("My Note", secs)
    assert md.startswith("# My Note")
    assert "## Intro" in md and "## Body" in md
    assert md.endswith("\n")
    assert "\\section" not in md and "\\documentclass" not in md   # not LaTeX


def test_assemble_empty_sections_is_safe():
    md = assemble_markdown("T", [])
    assert md.startswith("# T")
    assert "no content generated" in md


def test_assemble_frontmatter_stamps_provenance():
    """Provenance frontmatter (2026-07-22): rendered before the title, quoted,
    empty values skipped; the default (no frontmatter) stays byte-identical."""
    secs = [SectionOutput(title="Intro", body="## Intro\nA.")]
    md = assemble_markdown("T", secs, frontmatter={
        "generated_by": "CHERQAiN docgen",
        "model": 'weird"model\\name',
        "empty": "",
        "topic": "line one\nline two",
    })
    assert md.startswith("---\n")
    assert 'generated_by: "CHERQAiN docgen"' in md
    # Quotes/backslashes escaped, newlines folded to one line.
    assert 'model: "weird\\"model\\\\name"' in md
    assert 'topic: "line one line two"' in md
    assert "empty:" not in md
    assert "\n# T\n" in md
    # Default path unchanged.
    assert assemble_markdown("T", secs) == assemble_markdown("T", secs, frontmatter=None)
    assert assemble_markdown("T", secs).startswith("# T")


# --- prompts ---------------------------------------------------------------- #

def test_section_prompt_is_markdown_oriented_within_cap():
    p = section_system_prompt("clinicians", title="Methods")
    assert len(p) <= SYSTEM_PROMPT_LIMIT
    assert "Markdown" in p
    assert "## " in p
    assert "\\section" not in p and "\\begin{frame}" not in p


# --- generate binding (fake engine) ----------------------------------------- #

class _FakeClient:
    def __init__(self, text):
        self._text = text
        self.calls = []

    def chat(self, message, **kwargs):
        self.calls.append(kwargs)
        return ChatResult(text=self._text)


_KW = dict(
    index=1, full_outline=[Section(title="Intro", points=["a"])],
    topic="T", instructions="", audience="readers",
    provider="p", model="m", embed="e", max_iters=1, temperature=None,
)


def test_generate_section_accepts_markdown_and_uses_md_prompt():
    client = _FakeClient("## Intro\nProse grounded (source: a.md).")
    out = generate_section(client, section=Section("Intro", ["a"]), **_KW)
    assert not out.placeholder
    assert out.body.startswith("## Intro")
    assert "Markdown" in client.calls[0]["system_prompt"]


def test_generate_section_degrades_to_markdown_placeholder():
    client = _FakeClient("no heading here, just an apology")
    out = generate_section(client, section=Section("Intro", ["a"]), **_KW)
    assert out.placeholder
    assert out.body.startswith("## Intro")
    assert "\\begin{frame}" not in out.body


def test_request_outline_binds_markdown_prompt():
    client = _FakeClient('[{"title": "Background", "points": ["x"]}]')
    sections, _ = request_outline(
        client, topic="T", instructions="", provider="p", model="m", embed="e",
        max_iters=1, temperature=None, max_sections=6,
    )
    assert [s.title for s in sections] == ["Background"]
    assert "note" in client.calls[0]["system_prompt"].lower()


def test_slide_prompts_preserve_author_content_verbatim():
    from docgen import markdown_slides
    topic = 'Cesarean sections: the section\'s prose'
    instructions = "Compare cross-sections; retain the section's prose and note outline."
    outline = markdown_slides.build_outline_message(topic, instructions, 5)
    assert f'Topic: {topic}' in outline
    assert f'Instructions from the author:\n{instructions}' in outline
    assert 'Design a slide deck outline' in outline
    assert 'between 3 and 5 slides' in outline
    title = "Cross-sections and the section's prose"
    points = ["Explain the section's prose; keep sections intact"]
    section = markdown_slides.build_section_message(
        topic=topic, instructions=instructions, full_outline=[Section(title, points)],
        index=1, title=title, points=points)
    for original in (topic, instructions, title, points[0]):
        assert original in section
    assert 'then concise slide content.' in section
