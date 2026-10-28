"""AP-06 supported WSGI parity and bounded-concurrency regressions."""

from __future__ import annotations

import threading
import urllib.request
from unittest import mock

import pytest

from core.wsgi import WSGI_CONNECTION_LIMIT, WSGI_THREADS, serve_loopback


def test_supported_server_has_explicit_bounded_pool_and_channel_limits():
    app = object()
    with mock.patch("waitress.serve") as serve:
        serve_loopback(app, host="127.0.0.1", port=43210)

    serve.assert_called_once_with(
        app,
        host="127.0.0.1",
        port=43210,
        threads=WSGI_THREADS,
        connection_limit=WSGI_CONNECTION_LIMIT,
    )
    assert WSGI_THREADS == 32
    assert WSGI_CONNECTION_LIMIT == 100


def test_supported_server_refuses_non_loopback_even_below_entrypoint_guards():
    with pytest.raises(ValueError, match="loopback"):
        serve_loopback(object(), host="0.0.0.0", port=5000)


def test_gui_and_headless_call_the_same_supported_server_source():
    launch_source = open("launch.py", encoding="utf-8").read()
    cli_source = open("cli/main.py", encoding="utf-8").read()
    assert "serve_loopback(flask_app" in launch_source
    assert "serve_loopback(app" in cli_source
    assert ".run(host=" not in cli_source
    assert "waitress.serve" not in cli_source


def test_waitress_keeps_load_responsive_with_concurrent_sse_connections():
    """Three parked SSE requests leave the fourth bounded worker for load."""
    from waitress import create_server

    release = threading.Event()
    opened = []
    failures = []

    def app(environ, start_response):
        if environ["PATH_INFO"] == "/events":
            start_response("200 OK", [("Content-Type", "text/event-stream")])
            yield b"data: ready\n\n"
            release.wait(3)
            yield b"data: done\n\n"
            return
        start_response(
            "200 OK",
            [("Content-Type", "text/plain"), ("Content-Length", "2")],
        )
        yield b"ok"

    server = create_server(
        app, host="127.0.0.1", port=0, threads=4, connection_limit=10
    )
    runner = threading.Thread(target=server.run, daemon=True)
    runner.start()
    port = int(server.effective_port)

    def park_sse():
        try:
            response = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/events", timeout=2
            )
            assert response.readline() == b"data: ready\n"
            opened.append(response)
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    clients = [threading.Thread(target=park_sse) for _ in range(3)]
    try:
        for client in clients:
            client.start()
        for client in clients:
            client.join(2)
        assert not failures
        assert len(opened) == 3
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=1
        ) as response:
            assert response.read() == b"ok"
    finally:
        release.set()
        for response in opened:
            response.close()
        server.task_dispatcher.shutdown()
        # Waitress's asyncore select loop owns its descriptor map. Closing the
        # listening socket from this test thread races a select snapshot and
        # intermittently raises EBADF in the daemon even though assertions
        # pass. Queue closure through Waitress's wakeup pipe so it executes on
        # the loop thread after parked requests drain; joining makes terminal
        # server state part of the concurrency contract, not teardown luck.
        server.trigger.pull_trigger(server.close)
        runner.join(2)
        assert not runner.is_alive()
