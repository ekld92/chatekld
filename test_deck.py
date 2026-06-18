"""App-coupled tests for the Deck Generator: in-process runner + route validators.

Runs inside the hermetic suite (root conftest.py points CHATEKLD_BASE_DIR at a
temp dir before app import), so importing rag.vault / api.routes.deck is safe.
``run_agent_loop`` is mocked — no model, no real retrieval.
"""
from __future__ import annotations

import os
from unittest import mock

from core.agent import InfoEvent, IterationEvent, TokenEvent


def test_inprocess_runner_accumulates_and_forwards_events():
    from deckgen import inprocess
    from deckgen.result import ChatResult

    def fake_loop(*, on_event, **kwargs):
        on_event(IterationEvent(index=1))
        on_event(TokenEvent(text="Hello "))
        on_event(TokenEvent(text="world"))
        on_event(InfoEvent(text="a note"))
        return mock.Mock(iteration_count=1)

    seen = []
    with mock.patch.object(inprocess, "run_agent_loop", side_effect=fake_loop):
        runner = inprocess.InProcessChatRunner(cfg={"provider": "ollama"})
        result = runner.chat(
            "hi", system_prompt="sp", provider="ollama", model="m",
            embed="e", agent=True, max_iters=3, on_event=seen.append,
        )

    assert isinstance(result, ChatResult)
    assert result.text == "Hello world"
    assert "a note" in result.infos
    assert result.iterations == 1
    # on_event saw the SSE-dict shape, not AgentEvent objects.
    assert {"token": "Hello "} in seen
    assert {"info": "a note"} in seen
    assert {"iteration": 1} in seen


def test_inprocess_runner_surfaces_loop_exception():
    from deckgen import inprocess

    with mock.patch.object(inprocess, "run_agent_loop", side_effect=RuntimeError("boom")):
        runner = inprocess.InProcessChatRunner(cfg={"provider": "ollama"})
        result = runner.chat("hi", provider="ollama", model="m", embed="e")
    assert result.error and "boom" in result.error


def test_inprocess_runner_temperature_lands_on_vault_chat_temperature_key():
    """The agent loop reads cfg['vault_chat_temperature'] (not 'temperature'),
    so a per-turn override must be written there or it is silently ignored."""
    from deckgen import inprocess

    captured = {}

    def fake_loop(*, cfg, on_event, **kwargs):
        captured["cfg"] = cfg
        return mock.Mock(iteration_count=0)

    base_cfg = {"provider": "ollama", "vault_chat_temperature": 0.3}
    with mock.patch.object(inprocess, "run_agent_loop", side_effect=fake_loop):
        runner = inprocess.InProcessChatRunner(cfg=base_cfg)
        runner.chat("hi", provider="ollama", model="m", embed="e", temperature=0.9)

    assert captured["cfg"]["vault_chat_temperature"] == 0.9
    # Caller's dict is not mutated (runner is reused across sections).
    assert base_cfg["vault_chat_temperature"] == 0.3


def test_resolve_template_path(tmp_path):
    from api.routes.deck import _resolve_template_path

    tex = tmp_path / "deck.tex"
    tex.write_text("x", encoding="utf-8")
    assert _resolve_template_path(str(tex)) == os.path.realpath(str(tex))

    # Wrong extension / missing / non-string are rejected.
    other = tmp_path / "deck.pdf"
    other.write_text("x", encoding="utf-8")
    assert _resolve_template_path(str(other)) is None
    assert _resolve_template_path(str(tmp_path / "missing.tex")) is None
    assert _resolve_template_path(None) is None
    assert _resolve_template_path("relative/deck.tex") is None


def test_resolve_out_dir_rejects_system_and_missing(tmp_path):
    from api.routes.deck import _resolve_out_dir

    assert _resolve_out_dir(str(tmp_path)) == os.path.realpath(str(tmp_path))
    assert _resolve_out_dir("/usr") is None
    assert _resolve_out_dir("/") is None
    assert _resolve_out_dir(str(tmp_path / "nope")) is None
    assert _resolve_out_dir(str(tmp_path / "deck.tex")) is None  # a file, not a dir


def test_deck_generate_requires_topic_and_template():
    """The route rejects missing topic/template with 400 before any model call."""
    from app import app

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    r = client.post("/api/deck/generate", json={"template_tex": "x"}, headers=headers)
    assert r.status_code == 400
    r = client.post("/api/deck/generate", json={"topic": "T"}, headers=headers)
    assert r.status_code == 400


def test_deck_routes_require_local_origin():
    from app import app

    client = app.test_client()
    # No X-Requested-With header -> origin check fails -> 403.
    r = client.post("/api/deck/load-template", json={"path": "/x.tex"})
    assert r.status_code == 403
