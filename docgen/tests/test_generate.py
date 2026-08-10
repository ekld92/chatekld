"""docgen orchestration: the deckgen engine bound to the article hooks.

Proves the four document-class hooks are wired — a prose section (no frame)
survives sanitize, and a total failure degrades to a PROSE placeholder, not a
Beamer frame — using a fake ChatRunner (no server, no requests).
"""
from __future__ import annotations

from deckgen.outline import Section
from deckgen.result import ChatResult
from docgen.generate import generate_section, request_outline


class _FakeClient:
    """Duck-typed ChatRunner: every .chat() returns the same canned result and
    records the system_prompt / agent flag it was called with."""
    def __init__(self, text, error=None):
        self._text = text
        self._error = error
        self.calls = []

    def chat(self, message, **kwargs):
        self.calls.append(kwargs)
        return ChatResult(text=self._text, error=self._error)


_SECTION_KW = dict(
    index=1,
    full_outline=[Section(title="Intro", points=["a"])],
    topic="T",
    instructions="",
    audience="clinicians",
    provider="p",
    model="m",
    embed="e",
    max_iters=1,
    temperature=None,
)


def test_generate_section_accepts_prose_without_a_frame():
    # The Beamer sanitize would REJECT this (no \begin{frame}) → placeholder.
    # The article hook must accept it verbatim.
    client = _FakeClient("\\section{Intro}\nConnected prose, no frames at all.")
    out = generate_section(client, section=Section("Intro", ["a"]), **_SECTION_KW)
    assert not out.placeholder
    assert "\\section{Intro}" in out.body
    assert "\\begin{frame}" not in out.body


def test_generate_section_degrades_to_prose_placeholder():
    client = _FakeClient("I could not find anything useful.")  # no \section anywhere
    out = generate_section(client, section=Section("Intro", ["a"]), **_SECTION_KW)
    assert out.placeholder
    assert "\\section{Intro}" in out.body       # prose placeholder keeps the heading
    assert "\\begin{frame}" not in out.body      # NOT a Beamer frame


def test_generate_section_uses_the_article_system_prompt():
    client = _FakeClient("\\section{Intro}\nProse.")
    generate_section(client, section=Section("Intro", ["a"]), **_SECTION_KW)
    sysp = client.calls[0]["system_prompt"]
    assert "article" in sysp.lower()
    assert "\\begin{frame}" not in sysp          # article prompt, not the deck one


def test_request_outline_binds_article_prompt_and_parses():
    client = _FakeClient('[{"title": "Introduction", "points": ["scope"]}]')
    sections, _ = request_outline(
        client, topic="T", instructions="", provider="p", model="m", embed="e",
        max_iters=1, temperature=None, max_sections=8,
    )
    assert [s.title for s in sections] == ["Introduction"]
    assert "article" in client.calls[0]["system_prompt"].lower()
