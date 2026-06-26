"""Hermetic tests for the golden-set scoring layer (no models, no index).

These exercise everything in ``scoring.py`` plus the bundled ``golden_qa.json``
so the data + scoring half of the eval is verified in CI. The live provider
half (``run_eval.py``) is intentionally not invoked here.
"""
import unittest
from pathlib import Path

from tests.eval.scoring import (
    GoldenPair,
    format_report,
    load_pairs,
    pass_rate,
    score_answer,
)

_GOLDEN = Path(__file__).parent / "golden_qa.json"


class TestLoadPairs(unittest.TestCase):
    def test_golden_file_parses_and_is_nonempty(self):
        pairs = load_pairs(_GOLDEN)
        self.assertGreaterEqual(len(pairs), 5)
        ids = [p.id for p in pairs]
        self.assertEqual(len(ids), len(set(ids)), "duplicate pair ids")

    def test_every_pair_has_a_known_prompt_mode(self):
        for p in load_pairs(_GOLDEN):
            self.assertIn(p.prompt_mode, {"strict", "balanced", "exploratory", "concise"})


class TestScoreAnswer(unittest.TestCase):
    def test_full_pass(self):
        pair = GoldenPair(
            id="x", query="q",
            must_cite=["heart_failure"], must_contain=["40"],
            must_not_contain=["50 percent or higher"],
        )
        r = score_answer("HFrEF is LVEF below 40% [heart_failure.md].", pair)
        self.assertTrue(r.passed)
        self.assertEqual(r.missing_citations, [])

    def test_missing_citation_fails(self):
        pair = GoldenPair(id="x", query="q", must_cite=["heart_failure"], must_contain=["40"])
        r = score_answer("LVEF below 40 percent.", pair)
        self.assertFalse(r.passed)
        self.assertEqual(r.missing_citations, ["heart_failure"])

    def test_forbidden_word_fails_grounding(self):
        pair = GoldenPair(id="g", query="q", must_not_contain=["paris"])
        r = score_answer("The capital of France is Paris.", pair)
        self.assertFalse(r.passed)
        self.assertEqual(r.forbidden_present, ["paris"])

    def test_word_boundary_avoids_substring_false_positive(self):
        # Regression for the 'paris' ⊂ 'comparison' collision: a grounded
        # refusal that merely uses 'comparison' must NOT be flagged as leaking
        # the forbidden token 'paris'.
        pair = GoldenPair(id="g", query="q", must_not_contain=["paris"])
        r = score_answer("In comparison, the notes do not mention France.", pair)
        self.assertTrue(r.passed)
        self.assertEqual(r.forbidden_present, [])

    def test_case_insensitive(self):
        pair = GoldenPair(id="x", query="q", must_contain=["ACE inhibitor"])
        self.assertTrue(score_answer("an ace INHIBITOR is first-line", pair).passed)


class TestReport(unittest.TestCase):
    def test_report_and_rate(self):
        results = [
            score_answer("ok [a.md] 40", GoldenPair("p1", "q", must_cite=["a.md"], must_contain=["40"])),
            score_answer("nope", GoldenPair("p2", "q", must_contain=["xyz"])),
        ]
        self.assertAlmostEqual(pass_rate(results), 0.5)
        report = format_report(results)
        self.assertIn("1/2 passed (50%)", report)
        self.assertIn("FAIL  p2", report)


if __name__ == "__main__":
    unittest.main()
