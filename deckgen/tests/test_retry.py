"""Unit tests for the cancel-aware per-turn retry wrapper (deckgen/retry.py).

Pure: no app, no server, no third-party. A fake client returns a scripted
sequence of ChatResults so we can assert call counts, the success short-circuit,
attempt exhaustion, cancellation, and the info-event surfacing.
"""
from deckgen.result import ChatResult
from deckgen.retry import chat_with_retry


class _ScriptedClient:
    """A ChatRunner stand-in: .chat() returns the next scripted ChatResult."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0
        self.messages = []

    def chat(self, message, *, on_event=None, **kwargs):
        self.calls += 1
        self.messages.append(message)
        # Last scripted result repeats if called more than scripted.
        idx = min(self.calls - 1, len(self._results) - 1)
        return self._results[idx]


def _ok(text="frames"):
    return ChatResult(text=text)


def _fail(error="boom"):
    return ChatResult(text="", error=error)


def test_single_attempt_is_the_legacy_path():
    """max_attempts=1 calls once and never retries, even on failure."""
    client = _ScriptedClient([_fail()])
    infos = []
    result = chat_with_retry(
        client, "msg", max_attempts=1, on_event=infos.append,
    )
    assert client.calls == 1
    assert result.error == "boom"
    assert infos == []  # no retry noise on the single-shot path


def test_retries_until_success():
    """Two failures then a success: 3 calls, final result ok, 2 retry infos."""
    client = _ScriptedClient([_fail(), _fail(), _ok("good")])
    infos = []
    result = chat_with_retry(
        client, "msg", max_attempts=3, retry_backoff_s=0,
        label="section 2", on_event=infos.append,
    )
    assert client.calls == 3
    assert result.ok and result.text == "good"
    assert len(infos) == 2
    assert all("section 2" in i["info"] for i in infos)


def test_exhausts_attempts_and_returns_last_failure():
    """Always failing: called exactly max_attempts times, returns the failure."""
    client = _ScriptedClient([_fail("nope")])
    result = chat_with_retry(client, "msg", max_attempts=3, retry_backoff_s=0)
    assert client.calls == 3
    assert not result.ok and result.error == "nope"


def test_cancellation_before_first_call_makes_no_call():
    client = _ScriptedClient([_ok()])
    result = chat_with_retry(
        client, "msg", max_attempts=3, should_cancel=lambda: True,
    )
    assert client.calls == 0
    assert not result.ok  # empty ChatResult


def test_cancellation_between_attempts_stops_retrying():
    """Cancel flips True after the first failed attempt; no further calls."""
    flag = {"cancel": False}

    def should_cancel():
        return flag["cancel"]

    class _CancelOnFirstFail(_ScriptedClient):
        def chat(self, message, *, on_event=None, **kwargs):
            r = super().chat(message, on_event=on_event, **kwargs)
            flag["cancel"] = True  # cancel arrives during the first attempt
            return r

    client = _CancelOnFirstFail([_fail(), _ok()])
    result = chat_with_retry(
        client, "msg", max_attempts=3, retry_backoff_s=0, should_cancel=should_cancel,
    )
    assert client.calls == 1  # second attempt skipped by the cancel check
    assert not result.ok
