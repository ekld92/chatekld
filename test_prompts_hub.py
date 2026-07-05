"""Hermetic tests for the Prompt Hub (core.prompt_capture + GET /api/prompts).

Covers the capture sink (record/snapshot, redaction, size cap, unknown-id drop,
never-raises, latest-wins, disable toggle, thread-safety) and the read-only
endpoint (local-origin 403 gate, snapshot shape). No model, no network — the
sink is a pure in-memory dict. The root conftest.py points CHATEKLD_BASE_DIR at
a temp dir before app import, so importing app / core.prompt_capture is safe.
"""
from __future__ import annotations

import threading

import pytest

from core import prompt_capture


@pytest.fixture(autouse=True)
def _clean_capture():
    """Reset the singleton before AND after each test so state never leaks."""
    prompt_capture.configure(None)
    prompt_capture.reset()
    yield
    prompt_capture.configure(None)
    prompt_capture.reset()


# --------------------------------------------------------------------------- #
# Capture sink
# --------------------------------------------------------------------------- #

def test_snapshot_lists_every_known_workflow_before_any_capture():
    snap = prompt_capture.snapshot()
    assert snap["enabled"] is True
    ids = [w["id"] for w in snap["workflows"]]
    # The full static set is always present (placeholders for un-run ones).
    assert set(ids) == {w["id"] for w in prompt_capture.WORKFLOWS}
    assert len(ids) == len(set(ids)), "workflow ids must be unique"
    assert all(w["captured"] is False for w in snap["workflows"])
    # Every descriptor carries a label/description/role for the panel.
    for w in snap["workflows"]:
        assert w["label"] and w["description"] and w["role"]


def test_record_captures_and_redacts_api_keys():
    prompt_capture.record(
        "plain_chat",
        "You are a helpful assistant. Key: sk-ant-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
        provider="anthropic",
        model="claude-opus-4-8",
        query="what is the BDI cutoff? token sk-proj-ABCDEFGHIJKLMNOPQRSTUV",
        context_chunks=7,
    )
    row = _row(prompt_capture.snapshot(), "plain_chat")
    assert row["captured"] is True
    assert row["provider"] == "anthropic"
    assert row["model"] == "claude-opus-4-8"
    assert row["context_chunks"] == 7
    # Redaction runs on BOTH the system prompt and the query.
    assert "sk-ant-" not in row["system_prompt"]
    assert "<redacted>" in row["system_prompt"]
    assert "sk-proj-" not in row["query"]
    assert isinstance(row["captured_at"], float)


def test_record_caps_oversized_prompt():
    big = "x" * (prompt_capture._SYSTEM_PROMPT_MAX_CHARS + 5000)
    prompt_capture.record("deck_review", big)
    row = _row(prompt_capture.snapshot(), "deck_review")
    # Capped near the limit + a truncation marker; never the full 45k.
    assert len(row["system_prompt"]) <= prompt_capture._SYSTEM_PROMPT_MAX_CHARS + 64
    assert row["system_prompt"].endswith(prompt_capture._TRUNCATION_MARKER)


def test_unknown_workflow_id_is_dropped_not_ghosted():
    prompt_capture.record("totally_made_up", "prompt")
    ids = [w["id"] for w in prompt_capture.snapshot()["workflows"]]
    assert "totally_made_up" not in ids  # no phantom row


def test_record_never_raises_on_bad_input():
    # A capture bug must never break the generation it only observes.
    prompt_capture.record("plain_chat", None)  # type: ignore[arg-type]
    prompt_capture.record("plain_chat", 12345)  # type: ignore[arg-type]
    prompt_capture.record(None, "x")  # type: ignore[arg-type]
    # Whatever landed, snapshot still serialises cleanly.
    assert prompt_capture.snapshot()["enabled"] is True


def test_latest_capture_wins():
    prompt_capture.record("plain_chat", "first", provider="ollama")
    prompt_capture.record("plain_chat", "second", provider="openai")
    row = _row(prompt_capture.snapshot(), "plain_chat")
    assert row["system_prompt"] == "second"
    assert row["provider"] == "openai"


def test_disabled_capture_is_a_noop():
    prompt_capture.configure(False)
    prompt_capture.record("plain_chat", "should not land")
    snap = prompt_capture.snapshot()
    assert snap["enabled"] is False
    assert _row(snap, "plain_chat")["captured"] is False


def test_concurrent_records_are_thread_safe():
    ids = [w["id"] for w in prompt_capture.WORKFLOWS]

    def worker(i):
        for _ in range(50):
            prompt_capture.record(ids[i % len(ids)], f"prompt-{i}", provider="p")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # No crash, and a snapshot is always coherent (every id present exactly once).
    snap = prompt_capture.snapshot()
    assert len(snap["workflows"]) == len(ids)


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #

def test_endpoint_requires_local_origin():
    from app import app

    client = app.test_client()
    # No X-Requested-With header → the global origin guard 403s.
    assert client.get("/api/prompts").status_code == 403


def test_endpoint_returns_snapshot():
    from app import app

    prompt_capture.record("vault_rag", "grounded prompt", provider="ollama", model="llama3.2")
    client = app.test_client()
    resp = client.get("/api/prompts", headers={"X-Requested-With": "ChatEKLD"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enabled"] is True
    row = _row(body, "vault_rag")
    assert row["captured"] is True
    assert row["system_prompt"] == "grounded prompt"


def test_capture_fires_through_the_real_plainchat_route():
    """End-to-end: POST /api/plainchat with a mocked *provider* (not a mocked
    stream_chat_messages) must record the plain-chat prompt via the real
    core.llm.factory capture seam, then appear on GET /api/prompts.

    This is the integration proof that the wiring fires on a live request path,
    not just in a direct record() unit test.
    """
    from unittest import mock

    from app import app
    import core.llm.factory as factory

    class _FakeStream:
        def __init__(self):
            # stream_with_fallback iterates .response_gen.
            self.response_gen = iter(["ok"])

    class _FakeProvider:
        def stream(self, request):  # noqa: D401 — matches LLMProvider.stream
            return _FakeStream()

    client = app.test_client()
    h = {"X-Requested-With": "ChatEKLD"}
    # Patch the provider factory so the REAL stream_chat_messages ->
    # stream_with_fallback -> _capture_request path runs with no network.
    with mock.patch.object(factory, "get_llm_provider", return_value=_FakeProvider()):
        r = client.post(
            "/api/plainchat",
            json={"messages": [{"role": "user", "content": "hello"}],
                  "system_prompt": "You are the capture probe."},
            headers=h,
        )
        # Drain the stream so the worker thread finishes and records.
        r.get_data()
    assert r.status_code == 200

    resp = client.get("/api/prompts", headers=h)
    row = _row(resp.get_json(), "plain_chat")
    assert row["captured"] is True
    assert row["system_prompt"] == "You are the capture probe."
    assert row["query"] == "hello"


def test_config_rejects_malformed_capture_toggle():
    # The /api/config validator bool-coerces the knob; a garbage value is dropped
    # (prior value survives) rather than persisted.
    from api.routes.config import _validate_llm_config_keys

    data = {"prompt_capture_enabled": "not-a-bool"}
    _validate_llm_config_keys(data)
    assert "prompt_capture_enabled" not in data


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _row(snapshot: dict, workflow_id: str) -> dict:
    for w in snapshot["workflows"]:
        if w["id"] == workflow_id:
            return w
    raise AssertionError(f"workflow {workflow_id!r} not in snapshot")
