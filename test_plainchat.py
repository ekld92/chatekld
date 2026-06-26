"""Hermetic tests for the Plain Chat panel.

Covers the unified streaming helper (``core.llm.chat.stream_chat_messages``)
with a mocked ``LLMProvider`` — full messages array reaches ``LLMRequest``,
fallback-before-first-token, no-fallback-after-first-token, no-fallback on a
terminal error — plus the ``POST /api/plainchat`` route (403 / 400 / SSE
frames / array + content + system-prompt caps).

Runs inside the hermetic suite (root conftest.py points CHATEKLD_BASE_DIR at
a temp dir before app import), so importing app / api.routes.plainchat is safe.
``stream_chat_messages`` / the LLM provider are mocked — no model, no network.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from core.llm.types import ErrorCategory, LLMError


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _provider_yielding(tokens, *, raise_at_end=None):
    """A stand-in LLMProvider whose ``stream(request)`` records the request and
    returns a ``.response_gen`` that yields *tokens* then optionally raises."""
    captured: dict = {}

    def stream(request):
        captured["request"] = request

        def gen():
            for t in tokens:
                yield t
            if raise_at_end is not None:
                raise raise_at_end

        return SimpleNamespace(response_gen=gen())

    return SimpleNamespace(stream=stream, captured=captured)


def _sse_frames(raw: bytes) -> list:
    """Parse SSE bytes into the list of decoded ``data:`` payloads.

    ``[DONE]`` is kept verbatim; JSON frames are parsed into dicts.
    """
    out = []
    for block in raw.decode().split("\n\n"):
        block = block.strip()
        if not block.startswith("data:"):
            continue
        payload = block[len("data:"):].strip()
        if not payload:
            continue
        if payload == "[DONE]":
            out.append("[DONE]")
        else:
            out.append(json.loads(payload))
    return out


# --------------------------------------------------------------------------- #
# stream_chat_messages
# --------------------------------------------------------------------------- #

def test_stream_chat_messages_passes_full_messages_to_request():
    from core.llm import chat

    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "tell me more"},
    ]
    primary = _provider_yielding(["a", "b", "c"])
    with mock.patch.object(chat, "get_llm_provider", return_value=primary):
        out = list(chat.stream_chat_messages(
            messages=msgs, system_prompt="SP", provider_name="ollama",
            model="llama3.2", temperature=0.5, cfg={},
        ))

    assert "".join(out) == "abc"
    req = primary.captured["request"]
    # The full multi-turn array (incl. the assistant turn) reaches LLMRequest.
    assert req.messages == msgs
    assert req.system_prompt == "SP"
    assert req.model == "llama3.2"
    assert req.temperature == 0.5


def test_stream_chat_messages_falls_back_before_first_token():
    from core.llm import chat

    err = LLMError(category=ErrorCategory.SERVER_ERROR, message="boom",
                   provider="openai", retryable=True)
    primary = _provider_yielding([], raise_at_end=err)
    fallback = _provider_yielding(["x", "y"])
    infos: list = []

    def fake_get(name, cfg=None):
        return primary if name == "openai" else fallback

    cfg = {"fallback_provider": "ollama", "llm": "llama3.2"}
    with mock.patch.object(chat, "get_llm_provider", side_effect=fake_get):
        out = list(chat.stream_chat_messages(
            messages=[{"role": "user", "content": "hi"}], system_prompt="",
            provider_name="openai", model="gpt-4o", temperature=0.3,
            cfg=cfg, info_cb=infos.append,
        ))

    assert "".join(out) == "xy"
    assert any("falling back" in m for m in infos)
    # Fallback request uses the resolved local model, not the OpenAI id.
    assert fallback.captured["request"].model == "llama3.2"
    # Same conversation array is forwarded to the fallback.
    assert fallback.captured["request"].messages == [{"role": "user", "content": "hi"}]


def test_stream_chat_messages_no_fallback_after_first_token():
    from core.llm import chat

    err = LLMError(category=ErrorCategory.SERVER_ERROR, message="mid-stream",
                   provider="openai", retryable=True)
    primary = _provider_yielding(["partial"], raise_at_end=err)
    fallback = _provider_yielding(["SHOULD_NOT_APPEAR"])
    names: list = []

    def fake_get(name, cfg=None):
        names.append(name)
        return primary if name == "openai" else fallback

    cfg = {"fallback_provider": "ollama", "llm": "llama3.2"}
    collected: list = []
    with mock.patch.object(chat, "get_llm_provider", side_effect=fake_get):
        with pytest.raises(LLMError):
            for tok in chat.stream_chat_messages(
                messages=[{"role": "user", "content": "hi"}], system_prompt="",
                provider_name="openai", model="gpt-4o", temperature=0.3, cfg=cfg,
            ):
                collected.append(tok)

    assert collected == ["partial"]
    # The fallback provider was never even constructed.
    assert "ollama" not in names


def test_stream_chat_messages_terminal_error_does_not_fall_back():
    from core.llm import chat

    err = LLMError(category=ErrorCategory.AUTH, message="bad key", provider="openai")
    primary = _provider_yielding([], raise_at_end=err)
    names: list = []

    def fake_get(name, cfg=None):
        names.append(name)
        return primary

    cfg = {"fallback_provider": "ollama", "llm": "llama3.2"}
    with mock.patch.object(chat, "get_llm_provider", side_effect=fake_get):
        with pytest.raises(LLMError):
            list(chat.stream_chat_messages(
                messages=[{"role": "user", "content": "hi"}], system_prompt="",
                provider_name="openai", model="gpt-4o", temperature=0.3, cfg=cfg,
            ))

    # AUTH is not in the default fallback set → fallback never attempted.
    assert names == ["openai"]


# --------------------------------------------------------------------------- #
# POST /api/plainchat
# --------------------------------------------------------------------------- #

def test_plainchat_requires_local_origin():
    from app import app

    client = app.test_client()
    # No X-Requested-With header → origin check fails → 403.
    r = client.post("/api/plainchat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403


def test_plainchat_rejects_malformed_messages():
    from app import app

    client = app.test_client()
    h = {"X-Requested-With": "ChatEKLD"}

    assert client.post("/api/plainchat", json={}, headers=h).status_code == 400
    assert client.post("/api/plainchat", json={"messages": []}, headers=h).status_code == 400
    assert client.post("/api/plainchat", json={"messages": "nope"}, headers=h).status_code == 400
    # Bad role.
    assert client.post("/api/plainchat",
                       json={"messages": [{"role": "system", "content": "x"}]},
                       headers=h).status_code == 400
    # Non-dict entry.
    assert client.post("/api/plainchat",
                       json={"messages": ["just a string"]},
                       headers=h).status_code == 400
    # Empty content.
    assert client.post("/api/plainchat",
                       json={"messages": [{"role": "user", "content": "   "}]},
                       headers=h).status_code == 400


def test_plainchat_streams_tokens_and_done():
    from app import app
    import api.routes.plainchat as pc

    captured: dict = {}

    def fake_stream(**kwargs):
        captured.update(kwargs)
        yield "Hello "
        yield "world"

    client = app.test_client()
    h = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(pc, "stream_chat_messages", new=fake_stream):
        r = client.post("/api/plainchat",
                        json={"messages": [{"role": "user", "content": "hi"}]},
                        headers=h)
        frames = _sse_frames(r.get_data())

    assert r.status_code == 200
    tokens = [f["token"] for f in frames if isinstance(f, dict) and "token" in f]
    assert "".join(tokens) == "Hello world"
    assert "[DONE]" in frames
    # The validated conversation array reached the helper.
    assert captured["messages"] == [{"role": "user", "content": "hi"}]


def test_plainchat_caps_message_array_and_content():
    from app import app
    import api.routes.plainchat as pc

    captured: dict = {}

    def fake_stream(**kwargs):
        captured.update(kwargs)
        yield "ok"

    # A realistic 50-turn alternating conversation (user/assistant), with an
    # oversized last message. The window keeps the last 20 turns; since this is
    # a clean alternating log no merge happens.
    big = "z" * (pc._MSG_CONTENT_MAX_LEN + 5000)
    msgs = []
    for i in range(50):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": big if i == 49 else f"m{i}"})

    client = app.test_client()
    h = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(pc, "stream_chat_messages", new=fake_stream):
        client.post("/api/plainchat", json={"messages": msgs}, headers=h).get_data()

    sent = captured["messages"]
    assert len(sent) <= pc._MAX_MESSAGES                      # array capped to last 20
    assert sent[0]["role"] == "user"                          # window starts on a user turn
    assert len(sent[-1]["content"]) == pc._MSG_CONTENT_MAX_LEN  # content truncated


def test_validate_messages_normalizes_for_provider_shape():
    """_validate_messages trims leading assistant turns and merges consecutive
    same-role turns so strict-alternation providers (Anthropic/Gemini) accept
    the window; both are no-ops for a clean alternating log."""
    from api.routes.plainchat import _validate_messages, _MSG_CONTENT_MAX_LEN

    # Leading assistant turn dropped → window starts on 'user'.
    out = _validate_messages([
        {"role": "assistant", "content": "stale"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert out[0]["content"] == "hi"

    # Consecutive same-role turns (e.g. two user turns after an empty model
    # reply) are merged so the array alternates.
    out2 = _validate_messages([
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": "c"},
    ])
    assert [m["role"] for m in out2] == ["user", "assistant"]
    assert out2[0]["content"] == "a\n\nb"

    # An all-assistant body normalizes to empty → rejected.
    assert _validate_messages([{"role": "assistant", "content": "x"}]) is None

    # The merge re-caps to the per-message bound.
    half = "z" * (_MSG_CONTENT_MAX_LEN - 1)
    out3 = _validate_messages([
        {"role": "user", "content": half},
        {"role": "user", "content": half},
    ])
    assert len(out3) == 1
    assert len(out3[0]["content"]) == _MSG_CONTENT_MAX_LEN


def test_plainchat_empty_response_has_no_synthetic_token():
    """An empty-but-clean stream must NOT synthesize a placeholder token — the
    client records every token into history, so a synthetic one would be re-sent
    as a fake assistant turn (the frontend renders the muted bubble instead)."""
    from app import app
    import api.routes.plainchat as pc

    def empty_stream(**kwargs):
        return
        yield  # pragma: no cover (makes this a generator)

    client = app.test_client()
    h = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(pc, "stream_chat_messages", new=empty_stream):
        r = client.post("/api/plainchat",
                        json={"messages": [{"role": "user", "content": "hi"}]},
                        headers=h)
        frames = _sse_frames(r.get_data())

    tokens = [f for f in frames if isinstance(f, dict) and "token" in f]
    assert tokens == []
    assert "[DONE]" in frames


def test_plainchat_caps_system_prompt_and_clamps_temperature():
    from app import app
    from core.constants import SYSTEM_PROMPT_LIMIT
    import api.routes.plainchat as pc

    captured: dict = {}

    def fake_stream(**kwargs):
        captured.update(kwargs)
        yield "ok"

    client = app.test_client()
    h = {"X-Requested-With": "ChatEKLD"}
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "system_prompt": "S" * (SYSTEM_PROMPT_LIMIT + 1000),
        "temperature": 9.9,  # out of [0,2] → clamped
    }
    with mock.patch.object(pc, "stream_chat_messages", new=fake_stream):
        client.post("/api/plainchat", json=body, headers=h).get_data()

    assert len(captured["system_prompt"]) == SYSTEM_PROMPT_LIMIT
    assert captured["temperature"] == 2.0


def test_config_validator_clamps_and_drops_chat_knobs():
    """POST /api/config routes chat_* through _validate_llm_config_keys: in-range
    kept, out-of-range clamped, malformed dropped (prior preserved)."""
    from api.routes.config import _validate_llm_config_keys
    from core.constants import SYSTEM_PROMPT_LIMIT

    d = {"chat_temperature": 9.0, "chat_system_prompt": "S" * (SYSTEM_PROMPT_LIMIT + 50)}
    _validate_llm_config_keys(d)
    assert d["chat_temperature"] == 2.0                       # clamped to [0, 2]
    assert len(d["chat_system_prompt"]) == SYSTEM_PROMPT_LIMIT  # capped

    # Non-string system prompt / garbage temperature are dropped entirely.
    d2 = {"chat_system_prompt": 123, "chat_temperature": "abc"}
    _validate_llm_config_keys(d2)
    assert "chat_system_prompt" not in d2
    assert "chat_temperature" not in d2

    # An (whitespace-only) empty system prompt is a valid "no system prompt".
    d3 = {"chat_system_prompt": "   "}
    _validate_llm_config_keys(d3)
    assert d3["chat_system_prompt"] == ""


def test_plainchat_emits_error_frame():
    from app import app
    import api.routes.plainchat as pc

    def boom(**kwargs):
        raise RuntimeError("kaboom")
        yield  # pragma: no cover (makes this a generator)

    client = app.test_client()
    h = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(pc, "stream_chat_messages", new=boom):
        r = client.post("/api/plainchat",
                        json={"messages": [{"role": "user", "content": "hi"}]},
                        headers=h)
        frames = _sse_frames(r.get_data())

    errors = [f["error"] for f in frames if isinstance(f, dict) and "error" in f]
    assert errors and "kaboom" in errors[0]
    assert "[DONE]" in frames
