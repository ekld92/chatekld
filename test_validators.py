"""Unit tests for the shared request-body validators in ``api/validators.py``.

These pin the behaviour that used to live in ``api/routes/vault.py`` as
private helpers and is now reused across the route layer (M6).  The
contracts are small but several routes depend on them, so a regression
here would silently widen accepted inputs.
"""
import math
import unittest

from api.validators import (
    MISSING,
    coerce_bool,
    coerce_enum,
    coerce_float_in_range,
    coerce_int_in_range,
    coerce_non_empty_string,
    coerce_regex,
    coerce_string_max_len,
    first_valid,
)


class TestCoerceInt(unittest.TestCase):
    def test_clamps_into_range(self):
        self.assertEqual(coerce_int_in_range(100, 1, 32), 32)
        self.assertEqual(coerce_int_in_range(-5, 1, 32), 1)
        self.assertEqual(coerce_int_in_range(6, 1, 32), 6)

    def test_parses_numeric_strings(self):
        self.assertEqual(coerce_int_in_range("12", 1, 32), 12)
        self.assertEqual(coerce_int_in_range("12.7", 1, 32), 12)

    def test_rejects_non_finite(self):
        self.assertIsNone(coerce_int_in_range(float("nan"), 1, 32))
        self.assertIsNone(coerce_int_in_range(float("inf"), 1, 32))
        self.assertIsNone(coerce_int_in_range(float("-inf"), 1, 32))

    def test_rejects_garbage(self):
        self.assertIsNone(coerce_int_in_range("abc", 1, 32))
        self.assertIsNone(coerce_int_in_range(None, 1, 32))
        self.assertIsNone(coerce_int_in_range([1], 1, 32))


class TestCoerceFloat(unittest.TestCase):
    def test_clamps_into_range(self):
        self.assertEqual(coerce_float_in_range(1.5, 0.0, 1.0), 1.0)
        self.assertEqual(coerce_float_in_range(-1.0, 0.0, 1.0), 0.0)
        self.assertAlmostEqual(coerce_float_in_range(0.42, 0.0, 1.0), 0.42)

    def test_parses_numeric_strings(self):
        self.assertAlmostEqual(coerce_float_in_range("0.3", 0.0, 1.0), 0.3)

    def test_rejects_non_finite(self):
        self.assertIsNone(coerce_float_in_range(float("nan"), 0.0, 1.0))
        self.assertIsNone(coerce_float_in_range(math.inf, 0.0, 1.0))


class TestCoerceBool(unittest.TestCase):
    def test_passes_through_json_booleans(self):
        self.assertIs(coerce_bool(True), True)
        self.assertIs(coerce_bool(False), False)

    def test_accepts_truthy_string_forms(self):
        for s in ("true", "TRUE", "1", "yes", "on", "  True  "):
            self.assertIs(coerce_bool(s), True, msg=s)
        for s in ("false", "FALSE", "0", "no", "off"):
            self.assertIs(coerce_bool(s), False, msg=s)

    def test_rejects_ambiguous_strings(self):
        for s in ("maybe", "", "2", "null"):
            self.assertIsNone(coerce_bool(s), msg=s)

    def test_accepts_only_canonical_ints(self):
        self.assertIs(coerce_bool(1), True)
        self.assertIs(coerce_bool(0), False)
        self.assertIsNone(coerce_bool(2))

    def test_rejects_garbage(self):
        self.assertIsNone(coerce_bool(None))
        self.assertIsNone(coerce_bool([]))


class TestCoerceEnum(unittest.TestCase):
    def test_accepts_members(self):
        self.assertEqual(coerce_enum("strict", {"strict", "balanced"}), "strict")

    def test_rejects_non_members(self):
        self.assertIsNone(coerce_enum("none", {"strict", "balanced"}))

    def test_rejects_non_strings(self):
        self.assertIsNone(coerce_enum(1, {"strict"}))
        self.assertIsNone(coerce_enum(None, {"strict"}))


class TestCoerceRegex(unittest.TestCase):
    def test_full_match_required(self):
        self.assertEqual(coerce_regex("abc-1", r"[a-z]+-\d+"), "abc-1")
        # Partial matches are rejected (security: prevents prefix-only validation).
        self.assertIsNone(coerce_regex("abc-1!", r"[a-z]+-\d+"))

    def test_accepts_precompiled_pattern(self):
        import re
        pat = re.compile(r"[A-Za-z0-9._:/-]{1,128}")
        self.assertEqual(coerce_regex("llama3.2:latest", pat), "llama3.2:latest")

    def test_rejects_non_strings(self):
        self.assertIsNone(coerce_regex(123, r"\d+"))


class TestCoerceNonEmptyString(unittest.TestCase):
    def test_strips_and_returns(self):
        self.assertEqual(coerce_non_empty_string("  hi  "), "hi")

    def test_rejects_empty(self):
        self.assertIsNone(coerce_non_empty_string(""))
        self.assertIsNone(coerce_non_empty_string("   "))
        self.assertIsNone(coerce_non_empty_string(None))

    def test_truncates_at_max_len(self):
        self.assertEqual(coerce_non_empty_string("abcdef", max_len=3), "abc")


class TestCoerceStringMaxLen(unittest.TestCase):
    def test_allows_empty(self):
        self.assertEqual(coerce_string_max_len("", 10), "")
        self.assertEqual(coerce_string_max_len("  ", 10), "")

    def test_truncates_at_max(self):
        self.assertEqual(coerce_string_max_len("a" * 100, 5), "aaaaa")

    def test_rejects_non_strings(self):
        self.assertIsNone(coerce_string_max_len(123, 10))


class TestFirstValid(unittest.TestCase):
    def test_returns_first_coerced_value(self):
        result = first_valid([
            (None, lambda v: v),
            (MISSING, lambda v: v),
            ("3", lambda v: coerce_int_in_range(v, 1, 10)),
            ("99", lambda v: coerce_int_in_range(v, 1, 10)),
        ])
        self.assertEqual(result, 3)

    def test_skips_invalid_and_falls_through(self):
        result = first_valid([
            ("garbage", lambda v: coerce_int_in_range(v, 1, 10)),
            ("7", lambda v: coerce_int_in_range(v, 1, 10)),
        ])
        self.assertEqual(result, 7)

    def test_returns_missing_when_all_fail(self):
        result = first_valid([
            (None, lambda v: v),
            ("garbage", lambda v: coerce_int_in_range(v, 1, 10)),
        ])
        self.assertIs(result, MISSING)


if __name__ == "__main__":
    unittest.main()
