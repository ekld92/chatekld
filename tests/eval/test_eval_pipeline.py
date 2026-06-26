"""Hermetic test for the eval *pipeline* wiring (no model, no index, no network).

``test_scoring.py`` covers the scoring layer in isolation; this covers the
``run_eval.run_pairs`` orchestration that the live runner depends on — the half
that was previously only reachable behind ``RUN_LIVE_EVAL=1`` and, per its own
docstring, had never been executed. A fake manager stands in for
``ObsidianVaultManager`` so the loop, the two ``stream_chat`` return shapes, and
the grounded-vs-hallucinated scoring are all exercised in CI.
"""
from __future__ import annotations

import unittest
from typing import Iterator

from tests.eval.run_eval import run_pairs
from tests.eval.scoring import GoldenPair


class _FakeStream:
    """Mimics a LlamaIndex StreamingResponse: a lazy ``response_gen`` iterator."""

    def __init__(self, text: str) -> None:
        self._text = text

    @property
    def response_gen(self) -> Iterator[str]:
        # Chunked so the join in _collect_answer is genuinely exercised.
        for i in range(0, len(self._text), 5):
            yield self._text[i : i + 5]


class _PlainResponse:
    """Mimics the degenerate stringify-only Response (LM Studio empty sentinel)."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text


class _FakeManager:
    """Stands in for ObsidianVaultManager — returns a canned answer per query.

    Honours the exact ``stream_chat`` keyword contract run_pairs calls with, so a
    drift in that call signature fails here loudly rather than only under a live
    provider.
    """

    def __init__(self, answers: dict[str, object]) -> None:
        self._answers = answers
        self.calls: list[dict] = []

    def stream_chat(self, query, *, llm_name, embed_name, provider_name, prompt_mode, top_k):
        self.calls.append({
            "query": query, "llm_name": llm_name, "embed_name": embed_name,
            "provider_name": provider_name, "prompt_mode": prompt_mode, "top_k": top_k,
        })
        return self._answers[query]


class TestRunPairs(unittest.TestCase):
    def test_scores_grounded_pass_and_hallucinated_fail(self):
        pairs = [
            GoldenPair(id="grounded", query="q-grounded",
                       must_cite=["heart_failure"], must_contain=["40"]),
            GoldenPair(id="halluc", query="q-halluc", must_not_contain=["paris"]),
        ]
        answers = {
            # grounded, cites the source filename and states the fact
            "q-grounded": _FakeStream("HFrEF is an LVEF below 40 percent [heart_failure.md]."),
            # ungrounded parametric answer — leaks the forbidden token
            "q-halluc": _FakeStream("The capital of France is Paris."),
        }
        results = run_pairs(_FakeManager(answers), pairs,
                            model="m", embed="e", provider="ollama")
        by_id = {r.id: r for r in results}
        self.assertTrue(by_id["grounded"].passed)
        self.assertFalse(by_id["halluc"].passed)
        self.assertEqual(by_id["halluc"].forbidden_present, ["paris"])

    def test_unanswerable_pair_passes_on_grounded_refusal(self):
        # The new golden hallucination tripwires: a model that correctly refuses
        # (never names the drug) passes; the forbidden token only trips on leak.
        pair = GoldenPair(id="diabetes", query="q", must_not_contain=["metformin"])
        ok = run_pairs(
            _FakeManager({"q": _FakeStream("These notes do not cover diabetes therapy.")}),
            [pair], model="m", embed="e", provider="ollama",
        )[0]
        self.assertTrue(ok.passed)

        leaked = run_pairs(
            _FakeManager({"q": _FakeStream("First-line is metformin.")}),
            [pair], model="m", embed="e", provider="ollama",
        )[0]
        self.assertFalse(leaked.passed)

    def test_handles_plain_stringifiable_response(self):
        # The empty-answer sentinel path (no .response_gen) must not AttributeError.
        pair = GoldenPair(id="empty", query="q", must_contain=["40"])
        r = run_pairs(
            _FakeManager({"q": _PlainResponse("No relevant content found.")}),
            [pair], model="m", embed="e", provider="ollama",
        )[0]
        self.assertFalse(r.passed)
        self.assertEqual(r.missing_content, ["40"])

    def test_forwards_prompt_mode_and_top_k_to_manager(self):
        pair = GoldenPair(id="x", query="q", prompt_mode="balanced")
        mgr = _FakeManager({"q": _FakeStream("answer")})
        run_pairs(mgr, [pair], model="m", embed="e", provider="ollama", top_k=7)
        self.assertEqual(mgr.calls[0]["prompt_mode"], "balanced")
        self.assertEqual(mgr.calls[0]["top_k"], 7)


if __name__ == "__main__":
    unittest.main()
