"""Local Ask timing includes work before the final generator is returned."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("staged", [False, True])
def test_local_ask_times_eager_query_and_stream(monkeypatch, staged):
    from core.llm import usage
    from rag.vault import ObsidianVaultManager

    clock = [100.0]
    monkeypatch.setattr("rag.vault.time.monotonic", lambda: clock[0])
    record = Mock()
    monkeypatch.setattr(usage, "record_local_usage", record)

    class Engine:
        def query(self, message):
            # A synthesizer can finish work before returning a buffered stream.
            clock[0] += 12.0
            return SimpleNamespace(response_gen=iter(["Answer"]), source_nodes=[])

    class StagedEngine(Engine):
        def prepare(self, message):
            clock[0] += 3.0
            return message

        def read_snapshot(self, prepared):
            return []

        def finish_query(self, prepared, nodes):
            return self.query(prepared)

    manager = ObsidianVaultManager()
    engine = StagedEngine() if staged else Engine()
    monkeypatch.setattr(manager, "_build_engine", lambda **kwargs: (engine, lambda _: None))
    response = manager.stream_chat("Question", "fixture-model", "fixture-embed")
    assert list(response.response_gen) == ["Answer"]
    assert record.call_count == 1
    assert record.call_args.kwargs["latency_ms"] == (15000 if staged else 12000)
    assert record.call_args.kwargs["success"] is True
    assert record.call_args.kwargs["workflow"] == "vault_rag"


@pytest.mark.parametrize("aborted", [False, True])
def test_local_ask_timing_preserves_partial_failure(monkeypatch, aborted):
    from core.llm import usage
    from rag.vault import ObsidianVaultManager

    clock = [105.0]
    monkeypatch.setattr("rag.vault.time.monotonic", lambda: clock[0])
    record = Mock()
    monkeypatch.setattr(usage, "record_local_usage", record)

    def tokens():
        yield "partial"
        clock[0] = 107.0
        raise RuntimeError("fixture failure")

    response = SimpleNamespace(response_gen=tokens(), source_nodes=[])
    ObsidianVaultManager._wrap_local_chat_usage(
        response, "Question", "ollama", "fixture-model", started_at=100.0,
    )
    assert next(response.response_gen) == "partial"
    if aborted:
        response.response_gen.close()
    else:
        with pytest.raises(RuntimeError, match="fixture failure"):
            next(response.response_gen)
    assert record.call_count == 1
    details = record.call_args.kwargs
    assert details["latency_ms"] == (5000 if aborted else 7000)
    assert details["output_text"] == "partial"
    assert details["success"] is False
    assert details["error_category"] == ("aborted" if aborted else "stream_error")
