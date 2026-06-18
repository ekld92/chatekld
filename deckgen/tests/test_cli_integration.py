"""Integration test: drive ``deckgen.__main__.main`` against a stubbed client.

This exercises the full orchestration wiring (preflight -> outline -> per-section
-> sanitize -> assemble -> validate -> write) without a running server or any
network I/O, by monkeypatching the ``ChatEKLDClient`` symbol the CLI uses with a
fake that returns canned :class:`~deckgen.client.ChatResult` objects.

Unlike ``test_deckgen.py`` this transitively imports ``deckgen.client`` (and thus
``requests``), which is the declared deckgen dependency.
"""
from __future__ import annotations

import re

from deckgen import __main__ as cli
from deckgen.client import ChatResult


class _FakeClient:
    """Stand-in for ChatEKLDClient. Records calls; canned outline + sections."""

    def __init__(self, base_url, **_kwargs):
        self.base_url = base_url
        self.calls: list[dict] = []

    def status(self):
        return {"state": "done", "vault_path": "/vault", "prewarm_status": "ready"}

    def materials(self):
        return {"materials": []}

    def chat(self, message, *, system_prompt="", on_event=None, **_kwargs):
        self.calls.append({"system_prompt": system_prompt, "message": message})
        if "architect" in system_prompt.lower():
            return ChatResult(
                text='[{"title": "Definition", "points": ["a"]}, '
                     '{"title": "Management", "points": ["b"]}]'
            )
        # Per-section call: echo the requested title, wrapped in deliberately
        # messy output (code fence + leaked preamble line + trailing prose) so
        # the test also proves sanitize_section runs inside the real flow.
        m = re.search(r'ONLY section \d+: "([^"]+)"', message)
        title = m.group(1) if m else "Untitled"
        text = (
            "Here are the frames:\n```latex\n"
            "\\documentclass{beamer}\n"
            f"\\section{{{title}}}\n"
            f"\\begin{{frame}}{{{title}}}\n"
            "  \\begin{itemize}\\item key fact (source: note.md).\\end{itemize}\n"
            "\\end{frame}\n```\nLet me know if you want changes."
        )
        return ChatResult(text=text)


def test_main_writes_valid_deck(tmp_path, monkeypatch):
    captured: dict = {}

    def _factory(base_url, **kwargs):
        client = _FakeClient(base_url, **kwargs)
        captured["client"] = client
        return client

    monkeypatch.setattr(cli, "ChatEKLDClient", _factory)

    out = tmp_path / "deck.tex"
    rc = cli.main([
        "--topic", "Schizophrenia",
        "--instructions", "Focus on the essentials.",
        "--audience", "medical students",
        "--provider", "ollama", "--model", "qwen2.5",
        "--base-url", "http://127.0.0.1:9",
        "--title", "Schizophrenia",
        "--author", "Dr X",
        "--out", str(out),
    ])

    assert rc == 0  # clean deck -> no validate warnings
    tex = out.read_text(encoding="utf-8")

    # Scaffold owned by deckgen, exactly once.
    assert tex.count("\\documentclass{beamer}") == 1
    assert tex.count("\\begin{document}") == 1
    assert tex.count("\\end{document}") == 1
    assert "\\title{Schizophrenia}" in tex
    assert "\\author{Dr X}" in tex

    # Both outline sections made it through, with their citations.
    assert "\\section{Definition}" in tex
    assert "\\section{Management}" in tex
    assert "(source: note.md)" in tex

    # Sanitize stripped the fences and the leaked preamble + prose.
    assert "```" not in tex
    assert "Let me know if you want changes." not in tex
    assert tex.count("\\documentclass{beamer}") == 1  # the model's leaked one was dropped

    # 1 outline call + 2 section calls.
    client = captured["client"]
    assert len(client.calls) == 3
    assert "architect" in client.calls[0]["system_prompt"].lower()


class _AllProseClient(_FakeClient):
    """Returns a valid outline but non-Beamer prose for every section."""

    def chat(self, message, *, system_prompt="", on_event=None, **_kwargs):
        self.calls.append({"system_prompt": system_prompt, "message": message})
        if "architect" in system_prompt.lower():
            return ChatResult(
                text='[{"title": "Definition", "points": ["a"]}, '
                     '{"title": "Management", "points": ["b"]}]'
            )
        return ChatResult(text="I could not find relevant content in the vault.")


class _PartialClient(_FakeClient):
    """Real frames for every section except 'Management' (prose -> placeholder)."""

    def chat(self, message, *, system_prompt="", on_event=None, **_kwargs):
        self.calls.append({"system_prompt": system_prompt, "message": message})
        if "architect" in system_prompt.lower():
            return ChatResult(
                text='[{"title": "Definition", "points": ["a"]}, '
                     '{"title": "Management", "points": ["b"]}]'
            )
        m = re.search(r'ONLY section \d+: "([^"]+)"', message)
        title = m.group(1) if m else "Untitled"
        if title == "Management":
            return ChatResult(text="No content available for this part.")
        return ChatResult(
            text=f"\\section{{{title}}}\n\\begin{{frame}}{{{title}}}\n"
                 f"  \\begin{{itemize}}\\item fact (source: n.md).\\end{{itemize}}\n"
                 f"\\end{{frame}}"
        )


def _run(monkeypatch, client_cls, out):
    captured: dict = {}

    def _factory(base_url, **kwargs):
        captured["client"] = client_cls(base_url, **kwargs)
        return captured["client"]

    monkeypatch.setattr(cli, "ChatEKLDClient", _factory)
    rc = cli.main([
        "--topic", "Schizophrenia",
        "--provider", "ollama", "--model", "qwen2.5",
        "--base-url", "http://127.0.0.1:9",
        "--out", str(out),
    ])
    return rc, captured["client"]


def test_main_all_placeholders_exits_2(tmp_path, monkeypatch):
    out = tmp_path / "deck.tex"
    rc, _ = _run(monkeypatch, _AllProseClient, out)
    assert rc == 2  # every section was a placeholder
    tex = out.read_text(encoding="utf-8")
    assert tex.count("(no content generated for this section)") == 2
    assert "I could not find relevant content" not in tex  # prose did not leak in


def test_main_partial_placeholders_exits_1(tmp_path, monkeypatch):
    out = tmp_path / "deck.tex"
    rc, _ = _run(monkeypatch, _PartialClient, out)
    assert rc == 1  # one of two sections was a placeholder
    tex = out.read_text(encoding="utf-8")
    assert "(source: n.md)" in tex                                  # real section survived
    assert "(no content generated for this section)" in tex        # placeholder for the other


def test_main_dry_run_stops_after_outline(tmp_path, monkeypatch):
    captured: dict = {}

    def _factory(base_url, **kwargs):
        client = _FakeClient(base_url, **kwargs)
        captured["client"] = client
        return client

    monkeypatch.setattr(cli, "ChatEKLDClient", _factory)
    out = tmp_path / "deck.tex"
    rc = cli.main([
        "--topic", "Schizophrenia",
        "--provider", "ollama", "--model", "qwen2.5",
        "--base-url", "http://127.0.0.1:9",
        "--dry-run",
        "--out", str(out),
    ])
    assert rc == 0
    assert not out.exists()                 # nothing written on --dry-run
    assert len(captured["client"].calls) == 1  # outline only
