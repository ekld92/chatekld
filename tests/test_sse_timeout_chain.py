"""TIMEOUT CHAIN (2026-09-02): the online stall floor counts the RETRIES.

The chain's rule is outer-must-exceed-inner. `online_aware_floor_s` widened
the consumer stall guard to ONE `online_timeout_s`, but the online adapters
retry a transient failure `online_max_retries` times (attempts, not extra
tries), each bounded by that same timeout, with a 1 s-doubling-to-16 s backoff
between them — so at a plausible 150 s / 3 attempts the inner reaches 450+
against an outer of 330, and the app killed (and the provider still billed) a
call that was inside its configured budget.
"""
import unittest

from api.sse import online_aware_floor_s
from core.constants import SSE_SINGLE_SHOT_FLOOR_S


class TestOnlineFloorCountsRetries(unittest.TestCase):
    def test_the_floor_outlasts_the_whole_retry_ladder(self):
        cfg = {"online_timeout_s": 150, "online_max_retries": 3}
        inner_worst_case = 150 * 3          # three attempts, ignoring backoff
        self.assertGreater(online_aware_floor_s(cfg), inner_worst_case,
                           "the guard still kills a legal retried call")

    def test_the_backoff_gaps_are_budgeted_too(self):
        cfg = {"online_timeout_s": 150, "online_max_retries": 3}
        # retry.py waits 1 s, then doubles, capped at 16 s per gap; there are
        # attempts - 1 gaps.
        self.assertGreaterEqual(online_aware_floor_s(cfg), 150 * 3 + 2 * 90)

    def test_short_retry_budgets_keep_the_minimum_floor(self):
        """A config at or below the floor must not move it."""
        for cfg in ({"online_timeout_s": 30, "online_max_retries": 3},
                    {"online_timeout_s": 5}):
            self.assertEqual(online_aware_floor_s(cfg), SSE_SINGLE_SHOT_FLOOR_S,
                             cfg)

    def test_zero_retries_still_means_one_attempt(self):
        cfg = {"online_timeout_s": 400, "online_max_retries": 0}
        self.assertEqual(online_aware_floor_s(cfg), 400)

    def test_a_missing_retries_key_budgets_the_app_default(self):
        """The adapters would use the default ladder; so must the guard."""
        from core.constants import DEFAULT_ONLINE_MAX_RETRIES
        cfg = {"online_timeout_s": 150}
        self.assertEqual(online_aware_floor_s(cfg),
                         online_aware_floor_s(
                             {**cfg, "online_max_retries":
                              DEFAULT_ONLINE_MAX_RETRIES}))

    def test_missing_timeout_still_budgets_ten_default_attempts(self):
        from core.constants import DEFAULT_ONLINE_TIMEOUT_S
        self.assertEqual(online_aware_floor_s({"online_max_retries": 10}),
                         DEFAULT_ONLINE_TIMEOUT_S * 10 + 9 * 90)

    def test_a_malformed_config_never_shortens_the_floor(self):
        for cfg in ({"online_timeout_s": "abc"},
                    {"online_timeout_s": 150, "online_max_retries": "x"},
                    {"online_timeout_s": None}):
            self.assertGreaterEqual(online_aware_floor_s(cfg),
                                    SSE_SINGLE_SHOT_FLOOR_S, cfg)


if __name__ == "__main__":
    unittest.main()
