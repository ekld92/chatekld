"""Follow-ups to the 2026-09-23 CLI proof-run fixes (maintainer: "handle all
issues, keep XeLaTeX for the thesis").

* Server discovery must survive a slow ``/api/health`` (a cache miss during a
  scan measured ~2.05 s against a 1.5 s probe), yet still fail fast on a dead
  port.
"""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _SlowHealth(BaseHTTPRequestHandler):
    delay = 2.2

    def do_GET(self):  # noqa: N802 - http.server API
        time.sleep(self.delay)
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


def test_discovery_survives_a_slow_health_payload():
    from core.driver import is_server_running
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHealth)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert is_server_running(f"http://127.0.0.1:{srv.server_address[1]}") is True
    finally:
        srv.shutdown()


def test_discovery_still_fails_fast_on_a_dead_port():
    import socket
    from core.driver import is_server_running
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    started = time.monotonic()
    assert is_server_running(f"http://127.0.0.1:{port}") is False
    assert time.monotonic() - started < 1.0
