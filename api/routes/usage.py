"""Read-only usage / cost reporting endpoint."""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from api.security import origin_is_local
from api.validators import coerce_enum, coerce_int_in_range
from core.llm.usage import PRICING_TABLE, usage_tracker

usage_bp = Blueprint("usage", __name__)


_WINDOW_TO_DAYS = {
    "today": 1,
    "day": 1,
    "week": 7,
    "month": 30,
    "month_to_date": 0,  # treated as "since start of current month UTC"
    "all": None,
}


def _since_iso_for_window(window: str) -> str | None:
    """Return an ISO cutoff for a window keyword, or None for "all time"."""
    key = coerce_enum(
        (window or "month").strip().lower(),
        _WINDOW_TO_DAYS.keys(),
    ) or "month"
    days = _WINDOW_TO_DAYS[key]
    now = datetime.now(timezone.utc)
    if days is None:
        return None
    if key == "month_to_date":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start.isoformat()
    if days == 1:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.isoformat()
    return (now - timedelta(days=days)).isoformat()


@usage_bp.route("/api/usage", methods=["GET"])
def api_usage():
    """Return token/USD usage for a time window plus the recent request log.

    ``?window=`` is enum-clamped against ``_WINDOW_TO_DAYS`` (default ``month``)
    and turned into an ISO cutoff by :func:`_since_iso_for_window` (``all`` ⇒
    no cutoff). ``?recent=`` (clamped 0-200, default 25) bounds how many recent
    per-request records are returned alongside the aggregated summary.
    Local-origin gated.
    """
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    window = request.args.get("window", "month")
    since = _since_iso_for_window(window)
    summary = usage_tracker.summary(since_iso=since)

    recent_limit = coerce_int_in_range(request.args.get("recent", 25), 0, 200)
    if recent_limit is None:
        recent_limit = 25
    recent = [r.as_dict() for r in usage_tracker.recent(recent_limit)]

    return jsonify({
        "window": window,
        "since": since,
        "summary": summary.as_dict(),
        "recent": recent,
    })


@usage_bp.route("/api/pricing", methods=["GET"])
def api_pricing():
    """Return the per-model input/output/cached-input USD rates.

    Flattens ``PRICING_TABLE`` (already merged with any
    ``llm_pricing_overrides``) into a ``{model: {input, output, cached_input}}``
    map so the UI can display/estimate costs. Local-origin gated.
    """
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    return jsonify({
        "models": {
            name: {
                "input": pricing.input,
                "output": pricing.output,
                "cached_input": pricing.cached_input,
            }
            for name, pricing in PRICING_TABLE.items()
        },
    })
