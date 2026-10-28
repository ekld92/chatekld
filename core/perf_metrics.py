"""Opt-in, process-memory-only timings; no payloads or per-token logging.

PAPERMIND_PERF_METRICS=1 enables a 256-event ring. snapshot() returns numeric
measurements and fixed stage labels only. elapsed_s includes children;
exclusive_s subtracts instrumented children, so nested stages must not be
summed by elapsed_s. A monotonic clock also separates lock wait from hold.
"""
from core.branding import env_value
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import threading
import time
from typing import Any

_EVENTS: deque[dict[str, Any]] = deque(maxlen=256)
_MU = threading.Lock()
_STACK = ContextVar("papermind_perf_stack", default=())


def enabled():
    return env_value("PAPERMIND_PERF_METRICS") == "1"


def snapshot():
    with _MU:
        return [dict(event) for event in _EVENTS]


def clear():
    with _MU:
        _EVENTS.clear()


@contextmanager
def span(stage):
    if not enabled():
        yield
        return
    start = time.monotonic()
    frame = [0.0]
    parents = _STACK.get()
    token = _STACK.set((*parents, frame))
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        elapsed = max(0.0, time.monotonic() - start)
        _STACK.reset(token)
        if parents:
            parents[-1][0] += elapsed
        with _MU:
            _EVENTS.append({"stage": stage, "elapsed_s": elapsed,
                            "exclusive_s": max(0.0, elapsed - frame[0]),
                            "failed": failed, "thread": threading.get_ident(),
                            "started_s": start, "kind": "span"})


def measured(stage):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with span(stage):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


@contextmanager
def measured_lock(lock, stage):
    with span(stage + ".wait"):
        lock.__enter__()
    try:
        with span(stage + ".hold"):
            yield
    finally:
        lock.__exit__(None, None, None)


def record_latency(stage, start):
    """Overlapping latency marker, excluded from exclusive compute totals."""
    if start is None:
        return
    with _MU:
        _EVENTS.append({"stage": stage, "elapsed_s": max(0.0, time.monotonic() - start),
                        "kind": "latency", "thread": threading.get_ident()})
