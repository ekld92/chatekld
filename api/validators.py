"""Shared request-body validation helpers.

Promoted from the vault-route mini-framework so every route layer can
reuse the same coerce-or-fall-back pattern.  All helpers return ``None``
(or ``MISSING`` for the resolver) on invalid input so callers can chain
through body / config / engine defaults without writing nested try/except.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Iterable

MISSING: Any = object()


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* into [lo, hi].

    Assumes *value* is finite — callers must reject NaN/±Inf first, because a
    NaN compares False against both bounds and would slip through unchanged.
    """
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def coerce_int_in_range(value: Any, lo: int, hi: int) -> int | None:
    """Return int(value) clamped to [lo, hi], or None if not finite/parseable.

    NaN and ±Inf must be rejected before clamping because NaN comparisons are
    always False — ``_clamp`` would otherwise return NaN unchanged.  Python's
    json module accepts ``NaN``/``Infinity`` literals, so a hand-edited
    config.json could otherwise propagate non-finite values downstream.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    try:
        return int(_clamp(int(f), lo, hi))
    except (TypeError, ValueError, OverflowError):
        return None


def coerce_float_in_range(value: Any, lo: float, hi: float) -> float | None:
    """Return float(value) clamped to [lo, hi], or None if not finite/parseable.

    The finite-ness guard is essential (see :func:`coerce_int_in_range`): NaN
    and ±Inf — which the json module will happily parse from a hand-edited
    config — are rejected rather than clamped, since NaN defeats ``_clamp``.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return float(_clamp(f, lo, hi))


def coerce_bool(value: Any) -> bool | None:
    """Return *value* as a bool when it is unambiguous, else None.

    JSON booleans pass through unchanged.  Strings are accepted only for
    the conventional 'true'/'false'/'1'/'0' forms — anything else returns
    None so the resolver falls through to the config or engine default
    rather than silently coercing 'maybe' into False.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 0:
            return False
        if value == 1:
            return True
        return None
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
    return None


def coerce_enum(value: Any, allowed: Iterable[str]) -> str | None:
    """Return *value* iff it is a string in *allowed*, else None."""
    if not isinstance(value, str):
        return None
    allowed_set = allowed if isinstance(allowed, (set, frozenset)) else set(allowed)
    return value if value in allowed_set else None


def coerce_regex(value: Any, pattern: str | re.Pattern[str]) -> str | None:
    """Return *value* iff it is a string fully matching *pattern*, else None."""
    if not isinstance(value, str):
        return None
    compiled = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern)
    return value if compiled.fullmatch(value) else None


def coerce_non_empty_string(value: Any, max_len: int | None = None) -> str | None:
    """Return a stripped non-empty string, or None.

    When *max_len* is set, the string is truncated to that length rather
    than rejected — this matches the existing cap behaviour in
    ``api/routes/paper.py`` (which silently caps oversized fields).
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if max_len is not None and len(s) > max_len:
        return s[:max_len]
    return s


def coerce_string_max_len(value: Any, max_len: int) -> str | None:
    """Return a stripped string (possibly empty) truncated to *max_len*, else None.

    Differs from :func:`coerce_non_empty_string` in that an empty string is
    a valid result — useful when the field is optional and an empty value
    means "use the default" rather than "missing".
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if max_len >= 0 and len(s) > max_len:
        return s[:max_len]
    return s


def first_valid(
    sources: Iterable[tuple[Any, Callable[[Any], Any]]],
) -> Any:
    """Return the first coerced value that is not None / MISSING.

    Each *source* is a ``(raw_value, coerce_fn)`` tuple.  Sources with a
    ``MISSING`` or ``None`` raw value are skipped before coercion.  This
    mirrors the original ``_first_valid`` in ``api/routes/vault.py`` but
    flattens the closure-based API into something importable.
    """
    for raw, coerce in sources:
        if raw is MISSING or raw is None:
            continue
        coerced = coerce(raw)
        if coerced is not None:
            return coerced
    return MISSING
