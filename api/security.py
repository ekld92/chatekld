import base64
import hashlib
import html
import re
import logging
from flask import abort, jsonify, request
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def register_request_limits(app, *, default_max_bytes: int,
                            upload_max_bytes: int,
                            extra_upload_limits: dict | None = None) -> None:
    """Enforce a small default body cap and narrowly scoped upload caps.

    ``request.max_content_length`` is a per-request Werkzeug parsing limit: it
    rejects a declared oversized body before JSON/multipart parsing and also
    stops reads from a chunked or otherwise unknown-length WSGI stream. Only
    ``POST /api/upload`` receives ``upload_max_bytes``; any other route needing
    a raised envelope names itself in ``extra_upload_limits`` (path → bytes)
    and gets ITS OWN budget rather than sharing the paper flow's. That
    distinction is deliberate: the Plain Chat attachment route caps files far
    below ``PDF_LIMIT_SIZE_MB``, and letting it inherit the 500 MB envelope
    would mean accepting and buffering half a gigabyte only to refuse it one
    layer down. Each upload service retains its independent streamed file-byte
    limit, so multipart overhead cannot be traded for an oversized file.
    """
    extra = dict(extra_upload_limits or {})
    if default_max_bytes <= 0 or upload_max_bytes < default_max_bytes:
        raise ValueError("invalid request-body limits")
    if any(v < default_max_bytes for v in extra.values()):
        raise ValueError("invalid request-body limits")

    app.config["MAX_CONTENT_LENGTH"] = default_max_bytes

    @app.before_request
    def _select_request_body_limit():
        limit = default_max_bytes
        if request.method == "POST":
            if request.path == "/api/upload":
                limit = upload_max_bytes
            elif request.path in extra:
                limit = extra[request.path]
        request.max_content_length = limit
        if request.content_length is not None and request.content_length > limit:
            abort(413)

    @app.errorhandler(413)
    def _too_large(_error):
        return jsonify({"error": "Request body too large."}), 413


def _csp_hash(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def build_content_security_policy(template_source: str) -> str:
    """Build an enforced CSP covering every intentional inline handler.

    The legacy app shell still uses inline event attributes and one tiny
    pre-paint theme bootstrap. ``unsafe-inline`` would make CSP irrelevant to
    an injection, so each exact executable snippet is hash-allowlisted using
    CSP3 ``unsafe-hashes``. Adding or changing one without updating the served
    template automatically changes the computed policy at process start; an
    injected response fragment cannot add its own hash. Inline styles remain
    allowed because the established UI uses element.style and style attributes
    extensively, but remote CSS/resources remain blocked by every other
    directive and rendered Markdown drops styles entirely.

    **Two directives here exist to keep blob-URL PDF previews working, and they
    were established by measurement in BOTH engines — they disagree.** A
    document loaded from a local scheme (``blob:``) INHERITS the policy of the
    document that created it, and the app previews PDFs by handing an object URL
    to an ``<iframe>`` (an ``/api`` src is 403'd by the origin guard, so this is
    not a stylistic choice). Six surfaces depend on it: the audit
    inventory/accession/ambiguous decks, the Tagging deck's ``V`` panel, and
    Deck and Report "Preview PDF".

    * ``object-src blob:`` — **Chromium's** blocker. It renders an embedded PDF
      through a plugin/embed, so an inherited ``object-src 'none'`` refuses it,
      loudly (a real ``securitypolicyviolation`` naming ``object-src``).
    * **No ``frame-ancestors``** — **WebKit's** blocker, and the one that
      actually ships: PyWebView runs WKWebView. The inherited policy applies to
      the blob document, which is ITSELF framed, so ``frame-ancestors 'none'``
      tells it to refuse every embedder including our own page. WebKit obeys
      silently: the iframe's ``load`` event fires, no violation is reported, and
      the frame is simply blank. Measured directly (`pywebview` probe,
      2026-08-25): a policy of nothing but ``frame-ancestors 'none'`` is enough
      to blank it, and removing it restores rendering with ``object-src 'none'``
      still in place. Chromium does not reproduce this, so a Chromium-only test
      cannot catch it.

    Dropping ``frame-ancestors`` costs nothing here because
    ``X-Frame-Options: DENY`` gives the app shell the same anti-framing
    protection and is a plain response header — it is NOT part of CSP policy
    inheritance, so it never reaches the blob document. ``_NON_DOCUMENT_CSP``
    keeps ``frame-ancestors`` precisely because no blob is ever created from an
    API response's context.

    The grant is narrow either way: ``blob:`` URLs are same-origin and can only
    be minted by script, and ``script-src`` stays hash-locked with no
    ``'unsafe-inline'``, so injected markup cannot mint one. Any future
    tightening must be re-measured in WKWebView, not just in CI's Chromium;
    ``tests/test_security_hardening.py`` pins both halves with this reasoning.
    """
    scripts = re.findall(
        r"<script(?![^>]*\bsrc\s*=)[^>]*>([\s\S]*?)</script>",
        template_source,
        flags=re.IGNORECASE,
    )
    handlers = []
    for match in re.finditer(
            r"\son[a-z]+\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
            template_source, flags=re.IGNORECASE):
        handlers.append(html.unescape(match.group(1) or match.group(2) or ""))
    hashes = sorted({_csp_hash(value) for value in scripts + handlers})
    script_src = "script-src 'self' 'unsafe-hashes' " + " ".join(hashes)
    return "; ".join([
        "default-src 'none'",
        script_src,
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "frame-src blob:",
        "child-src blob:",
        "media-src 'none'",
        # See the docstring: a blob: PDF frame inherits this policy and its
        # viewer is a plugin/embed. 'none' here blanks every PDF preview.
        "object-src blob:",
        "base-uri 'none'",
        "form-action 'none'",
        # NO frame-ancestors: see the docstring. It is inherited by the blob:
        # PDF documents the previews rely on, which are themselves framed, and
        # WebKit then blanks them with no violation event. X-Frame-Options:
        # DENY (set on every response) is the non-inherited equivalent.
        "worker-src 'none'",
        "manifest-src 'self'",
    ])


#: Policy for responses that render no markup and run no script: JSON, SSE,
#: static assets, file downloads. The document policy carries one hash per
#: inline handler in the app shell — 114 of them, ~6.2 KB — and repeating that
#: on every 2-second status poll, every SSE frame response and every static
#: asset is pure overhead for directives none of them can use. The restrictive
#: fallbacks that DO matter for a directly-navigated response are kept.
#:
#: This does not weaken the PDF previews: a blob: document inherits the policy
#: of the document that CREATED it (the app shell), never the policy of the API
#: response the bytes were fetched from.
_NON_DOCUMENT_CSP = "; ".join([
    "default-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
])


def register_security_headers(app, template_source: str) -> None:
    """Apply browser defense-in-depth headers to HTML, static and API output."""
    csp = build_content_security_policy(template_source)

    @app.after_request
    def _security_headers(response):
        response.headers["Content-Security-Policy"] = (
            csp if response.mimetype == "text/html" else _NON_DOCUMENT_CSP)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
            "serial=(), bluetooth=(), browsing-topics=()"
        )
        return response


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
