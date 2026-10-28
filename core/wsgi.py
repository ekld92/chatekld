"""Supported bounded WSGI policy shared by GUI and headless launchers."""

from __future__ import annotations

from typing import Any

WSGI_THREADS = 32
WSGI_CONNECTION_LIMIT = 100
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def serve_loopback(app: Any, *, host: str, port: int) -> None:
    """Serve *app* with Waitress under the application's loopback model.

    Resource/concurrency invariant: both GUI and headless entry points use the
    same fixed 32-worker pool and explicit 100-channel ceiling. Long-lived SSE
    connections occupy workers while their bounded background producers run,
    but they cannot create unbounded request threads; the remaining pool serves
    status/cancellation traffic. Waitress drains a completed or disconnected
    request normally, while endpoint-level SSE guards own producer cancellation
    and partial-failure cleanup. This changes no persisted state or migration
    format. Refusing non-loopback here is defence in depth beneath the CLI and
    origin guard, preserving the unauthenticated single-user trust boundary.
    """
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("CHERQAiN's WSGI server may bind only to loopback")
    from waitress import serve  # type: ignore[import-untyped]

    serve(
        app,
        host=host,
        port=port,
        threads=WSGI_THREADS,
        connection_limit=WSGI_CONNECTION_LIMIT,
    )
