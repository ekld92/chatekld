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

    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    allowed_hosts = {"127.0.0.1", "localhost"}
    
    if origin:
        parsed_origin = urlparse(origin)
        return parsed_origin.hostname in allowed_hosts
    if referer:
        parsed_referer = urlparse(referer)
        return parsed_referer.hostname in allowed_hosts

    # Neither Origin nor Referer present (normal for PyWebView's embedded renderer).
    # Use the server-derived remote address rather than the client-supplied Host
    # header, which an attacker can set to "localhost" from a non-local connection.
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
        pass
    return msg
