import re
import logging
from flask import jsonify, request
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def register_origin_guard(app):
    """Register a ``before_request`` hook that enforces the local-origin
    check for **every** ``/api/`` route, replacing the 67 inline
    ``if not origin_is_local(): return jsonify(…), 403`` blocks that
    previously lived in each route handler.

    .. rubric:: Why a single hook

    The per-route copy-paste idiom was the *drift class* behind Track 1.1
    (a new route that forgot the check would silently be open).  A
    ``before_request`` hook cannot be omitted — new blueprints are protected
    automatically.

    .. rubric:: Exempt paths

    Only ``/api/*`` routes are gated.  The ``/`` index page and ``/static/``
    assets are served without the CSRF header check (they carry no secrets
    and must work in a bare browser).

    .. rubric:: What it restores / preserves

    **Invariant:** every request to a ``/api/*`` endpoint is rejected with
    HTTP 403 (``{\"error\": \"Forbidden\"}``) unless ``origin_is_local()``
    returns ``True``.  Pinned by ``test_all_routes_gated`` in
    ``smoke_test.py``.

    .. rubric:: Safety

    The hook runs before any handler code — a failed check never reaches the
    route body.  The response shape (``{\"error\": \"Forbidden\"}``, 403) is
    byte-identical to the old inline pattern so the client-side error path
    is unchanged.
    """

    @app.before_request
    def _enforce_local_origin():
        # Only gate /api/ routes — the index page and static assets are exempt.
        if request.path.startswith("/api/"):
            if not origin_is_local():
                return jsonify({"error": "Forbidden"}), 403
        # Returning None lets Flask continue to the route handler.
        return None


def origin_is_local() -> bool:
    """Return True only when the request originates from a local browser context."""
    if request.headers.get("X-Requested-With") != "ChatEKLD":
        return False

    allowed_hosts = {"127.0.0.1", "localhost", "::1"}

    # Host allowlist — the DNS-rebinding gate (2026-07-20 audit H1). A page at
    # evil.com whose DNS is rebound to 127.0.0.1 becomes SAME-ORIGIN with this
    # server: the custom header sails through (no CORS preflight), a
    # no-referrer GET carries neither Origin nor Referer, and remote_addr is
    # genuinely loopback — so every pre-Host check passes and /api/config,
    # /api/log/tail etc. are exfiltratable. The Host header on such a request
    # is the attacker's domain, so a fail-closed reject-if-not-local check
    # catches exactly this. (An earlier comment here dismissed Host checking
    # as "attacker-controllable" — true for FORGING localhost from afar, which
    # remote_addr already covers, but irrelevant to rejecting a wrong Host.)
    raw_host = request.host or ""
    if raw_host.startswith("["):
        # Bracketed IPv6 literal, e.g. "[::1]:5000".
        host = raw_host.split("]", 1)[0].lstrip("[")
    else:
        host = raw_host.rsplit(":", 1)[0] if ":" in raw_host else raw_host
    if host.lower() not in allowed_hosts:
        return False

    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")

    if origin:
        parsed_origin = urlparse(origin)
        return parsed_origin.hostname in allowed_hosts
    if referer:
        parsed_referer = urlparse(referer)
        return parsed_referer.hostname in allowed_hosts

    # Neither Origin nor Referer present (normal for PyWebView's embedded renderer).
    # remote_addr is server-derived (unforgeable), and the Host allowlist above
    # already rejected rebound names.
    return request.remote_addr in {"127.0.0.1", "::1"}

def sanitise_error_msg(error: Exception | str) -> str:
    """Strip sensitive topology details and API keys from errors."""
    msg = str(error)
    msg = re.sub(r'/Users/[\w.-]+/', '/<USER>/', msg)
    msg = re.sub(r'http://(127\.0\.0\.1|localhost):\d+/', 'http://localhost:<PORT>/', msg)
    try:
        from core.llm.redact import redact
        msg = redact(msg)
    except Exception:
        # Deliberate swallow (2026-07-22 comment repair): this function runs
        # on ERROR paths, some during early boot — an import cycle or a bug in
        # redact must never turn "sanitise the error" into a second exception
        # that masks the first. The path/port scrubbing above already ran; the
        # degraded case only loses the API-key redaction layer, and keys reach
        # exceptions only via provider SDK messages, which the adapters
        # classify long after boot.
        pass
    return msg
