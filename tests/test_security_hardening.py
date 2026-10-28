"""Regression tests for browser and request-parsing defense in depth."""
from __future__ import annotations

import base64
import hashlib
import html
import io
import re
from pathlib import Path

from flask import Flask, jsonify, request
from werkzeug.test import EnvironBuilder

from api.security import (
    _NON_DOCUMENT_CSP,
    build_content_security_policy,
    register_request_limits,
    register_security_headers,
)


def _hash(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def test_security_headers_enforce_exact_inline_code_hashes():
    template = (
        '<button onclick="runAction(&quot;x&quot;)">Run</button>'
        "<script>window.theme = 'dark';</script>"
    )
    app = Flask(__name__)
    register_security_headers(app, template)

    @app.get("/")
    def index():
        return template

    response = app.test_client().get("/")
    csp = response.headers["Content-Security-Policy"]
    script_src = next(
        directive for directive in csp.split("; ")
        if directive.startswith("script-src ")
    )
    assert "'unsafe-inline'" not in script_src
    assert "'unsafe-hashes'" in script_src
    assert _hash("runAction(\"x\")") in script_src
    assert _hash("window.theme = 'dark';") in script_src
    assert "default-src 'none'" in csp
    assert "img-src 'self' data: blob:" in csp
    assert "connect-src 'self'" in csp
    assert "frame-src blob:" in csp
    assert "object-src blob:" in csp
    assert "base-uri 'none'" in csp
    assert "form-action 'none'" in csp
    # frame-ancestors is deliberately ABSENT from the document policy — see
    # test_document_csp_omits_frame_ancestors_so_blob_pdfs_render.
    assert "frame-ancestors" not in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "camera=()" in response.headers["Permissions-Policy"]


def test_csp_keeps_every_blob_pdf_directive_open():
    """A blob: PDF frame inherits this policy; all three directives must allow it.

    The app previews PDFs by fetching bytes with the origin header and handing
    an object URL to an <iframe> (an /api/ src is 403'd by the origin guard).
    The blob document inherits the embedder's CSP and its viewer is a native
    plugin, so frame-src/child-src admit the frame while object-src admits the
    viewer inside it. Tightening any one of them blanks six user-facing PDF
    surfaces at once, which is what shipped once already; assert them together
    so the next tightening is a red test instead of a white page.
    """
    csp = build_content_security_policy("<html></html>")
    directives = dict(
        (part.split(" ", 1) + [""])[:2] for part in csp.split("; ")
    )
    for name in ("frame-src", "child-src", "object-src"):
        assert "blob:" in directives[name], f"{name} must grant blob: for PDF previews"


def test_document_csp_omits_frame_ancestors_so_blob_pdfs_render():
    """WebKit's blocker, which Chromium does not reproduce.

    A blob: document inherits the creating document's policy. The PDF previews
    put that blob in an <iframe>, so an inherited `frame-ancestors 'none'` tells
    the blob document to refuse every embedder — including our own page. WebKit
    obeys SILENTLY: the iframe's load event fires, no securitypolicyviolation is
    reported, and the frame is blank. Measured in WKWebView (2026-08-25): a
    policy of nothing but `frame-ancestors 'none'` blanks the preview, and
    removing it restores rendering even with `object-src 'none'` still set.

    PyWebView ships WKWebView, so this is the engine that matters — and the
    Playwright matrix runs Chromium, which renders it fine either way. That
    asymmetry is exactly why this assertion is here and not only in a browser
    test: nothing in CI can observe the failure.

    Anti-framing protection is unchanged. `X-Frame-Options: DENY` gives the app
    shell the same guarantee and is a plain response header, so it takes no part
    in CSP inheritance and never reaches the blob document.
    """
    csp = build_content_security_policy("<html></html>")
    assert "frame-ancestors" not in csp, (
        "frame-ancestors in the DOCUMENT policy is inherited by blob: PDF "
        "frames and blanks every preview in WKWebView"
    )

    app = Flask(__name__)
    register_security_headers(app, "<html></html>")

    @app.get("/")
    def index():
        return "<html></html>"

    response = app.test_client().get("/")
    assert response.headers["X-Frame-Options"] == "DENY", (
        "X-Frame-Options is the non-inherited replacement for frame-ancestors; "
        "dropping both would leave the shell frameable"
    )
    # The non-document policy keeps it: no blob is ever created from an API
    # response's context, so there is no inheritance path to break.
    assert "frame-ancestors 'none'" in _NON_DOCUMENT_CSP


def _limited_app() -> Flask:
    app = Flask(__name__)
    register_request_limits(app, default_max_bytes=64, upload_max_bytes=256)

    @app.post("/api/small")
    def small():
        return jsonify({"size": len(request.get_data())})

    @app.post("/api/upload")
    def upload():
        return jsonify({
            "size": len(request.get_data()),
            "limit": request.max_content_length,
        })

    return app


def test_declared_oversized_small_body_is_json_413_before_route_parsing():
    response = _limited_app().test_client().post(
        "/api/small", data=b"x" * 65, content_type="application/json")
    assert response.status_code == 413
    assert response.get_json() == {"error": "Request body too large."}


def test_upload_alone_receives_the_larger_request_envelope():
    client = _limited_app().test_client()
    assert client.post("/api/small", data=b"x" * 128).status_code == 413
    response = client.post("/api/upload", data=b"x" * 128)
    assert response.status_code == 200
    assert response.get_json() == {"size": 128, "limit": 256}


def test_unknown_length_stream_is_stopped_at_the_selected_limit():
    app = _limited_app()
    builder = EnvironBuilder(
        path="/api/small",
        method="POST",
        input_stream=io.BytesIO(b"x" * 65),
        content_type="application/octet-stream",
    )
    environ = builder.get_environ()
    environ.pop("CONTENT_LENGTH", None)
    # WSGI servers set this for a safely terminated chunked input stream;
    # Werkzeug can then consume only up to max_content_length and raise 413.
    environ["wsgi.input_terminated"] = True
    response = app.test_client().open(environ)
    assert response.status_code == 413
    assert response.get_json() == {"error": "Request body too large."}


def test_real_template_csp_covers_every_inline_script_and_handler():
    source = Path("templates/index.html").read_text(encoding="utf-8")
    app = Flask(__name__)
    register_security_headers(app, source)

    @app.get("/")
    def index():
        return source

    csp = app.test_client().get("/").headers["Content-Security-Policy"]
    inline_scripts = re.findall(
        r"<script(?![^>]*\bsrc\s*=)[^>]*>([\s\S]*?)</script>",
        source,
        flags=re.IGNORECASE,
    )
    handlers = [
        html.unescape(match.group(1) or match.group(2) or "")
        for match in re.finditer(
            r"\son[a-z]+\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
            source,
            flags=re.IGNORECASE,
        )
    ]
    assert inline_scripts
    assert handlers
    for executable in inline_scripts + handlers:
        assert _hash(executable) in csp


def test_non_document_responses_get_the_compact_policy():
    """The app shell's script hashes belong on the app shell, nowhere else.

    The document policy is ~6.4 KB because it carries one hash per inline
    handler. Repeating that on every 2-second status poll, every SSE frame
    response and every static asset costs bandwidth for directives a JSON body
    cannot use. The restrictive fallbacks that matter for a directly-navigated
    response are kept.

    This does not affect the PDF previews: a blob: document inherits the policy
    of the document that CREATED it, not of the API response the bytes came from
    (test_csp_keeps_every_blob_pdf_directive_open covers that side).
    """
    app = Flask(__name__)
    register_security_headers(app, '<button onclick="a()">x</button>')

    @app.get("/")
    def index():
        return '<button onclick="a()">x</button>'

    @app.get("/api/thing")
    def thing():
        return jsonify({"ok": True})

    client = app.test_client()
    document = client.get("/").headers["Content-Security-Policy"]
    api = client.get("/api/thing").headers["Content-Security-Policy"]

    assert "sha256-" in document
    assert "sha256-" not in api
    assert len(api) < len(document)
    for directive in ("default-src 'none'", "base-uri 'none'",
                      "form-action 'none'", "frame-ancestors 'none'"):
        assert directive in api
    # Defence in depth must not be lost on the compact policy.
    assert client.get("/api/thing").headers["X-Content-Type-Options"] == "nosniff"
