import json
import queue
import threading
from typing import Callable, List, Optional
from flask import Response

# sanitise_error_msg is owned by api.security (see root CLAUDE.md §Module
# Ownership); put_done_resilient by core.utils. The 2026-07-04 Batch 2 commit
# shipped importing both from core.utils, which made `import app` fail at boot.
from api.security import sanitise_error_msg
from core.utils import put_done_resilient

class _SSEOpGuard:
    """Joint lock-release ownership for one SSE operation (extracted from deck).

    Defect this replaces: lock releases that live ONLY in the SSE
    generator's ``finally``, which is wrong in both directions —

    * **Zombie writer:** a consumer stall-timeout / client disconnect ended the
      stream and released the lock while the worker thread was still
      running; the worker then wrote OUTSIDE the lock, racing whatever new 
      operation had already acquired it.
    * **Permanent leak:** a generator that is closed before its first
      iteration never runs its body, so the ``finally`` never fired and every
      subsequent op 409'd for the process lifetime.

    Fix: the lock is released exactly once, when BOTH the stream is finished
    (generator exhausted/closed *or* the response's ``call_on_close`` fires —
    the latter covers the never-iterated case) AND the worker thread — if one
    was ever spawned — has exited. Invariant (pinned by ``TestDeckOpGuard``): 
    the op lock cannot be re-acquired while a worker that may still write is alive,
    and is never leaked by an unconsumed response.
    """

    def __init__(self, release: Optional[Callable[[], None]]) -> None:
        self._release = release
        self._mu = threading.Lock()
        self._worker_spawned = False
        self._worker_done = False
        self._stream_done = False
        self._released = False

    def worker_spawned(self) -> None:
        with self._mu:
            self._worker_spawned = True

    def worker_finished(self) -> None:
        with self._mu:
            self._worker_done = True
            self._maybe_release_locked()

    def stream_finished(self) -> None:
        # Idempotent: called from the generator's finally AND call_on_close.
        with self._mu:
            self._stream_done = True
            self._maybe_release_locked()

    def _maybe_release_locked(self) -> None:
        if self._released or self._release is None:
            return
        if self._stream_done and (not self._worker_spawned or self._worker_done):
            self._released = True
            self._release()


def run_sse_worker(
    worker: Callable[[Callable[[dict], None], threading.Event], None],
    *,
    consumer_timeout_s: int,
    preflight_msgs: Optional[List[str]] = None,
    release: Optional[Callable[[], None]] = None
) -> Response:
    """Shared SSE drain skeleton for all streamed backend jobs.

    A daemon thread runs *worker(put, cancel)* (the slow, blocking pipeline) and
    pushes SSE-shaped dicts through *put*; this generator drains a bounded queue to
    the client. *preflight_msgs* are info lines emitted before the worker starts.
    *release* (the op lock's ``release``) is owned by a :class:`_SSEOpGuard`:
    it fires exactly once, when the stream has ended (including the
    never-iterated-generator case, via ``call_on_close``) AND the worker thread
    has exited — see the guard's docstring for the zombie-writer/leak defects
    this ordering closes.
    """
    guard = _SSEOpGuard(release)
    if preflight_msgs is None:
        preflight_msgs = []

    def gen():
        cancel = threading.Event()
        event_q: queue.Queue = queue.Queue(maxsize=1024)
        _DONE = object()

        def put(item):
            # Block (cancel-aware) on a full queue rather than drop frames; the 1 s
            # timeout lets us notice a cancel and stop feeding a dead consumer.
            while not cancel.is_set():
                try:
                    event_q.put(item, timeout=1)
                    return
                except queue.Full:
                    continue

        def _runner():
            try:
                worker(put, cancel)
            except Exception as exc:  # noqa: BLE001 — surface anything as an SSE error
                if not cancel.is_set():
                    put({"error": sanitise_error_msg(exc)})
            finally:
                put_done_resilient(event_q, _DONE, cancel)
                guard.worker_finished()

        try:
            for msg in preflight_msgs:
                yield f"data: {json.dumps({'info': msg})}\n\n"
            guard.worker_spawned()
            threading.Thread(target=_runner, daemon=True).start()
            try:
                while True:
                    try:
                        item = event_q.get(timeout=consumer_timeout_s)
                    except queue.Empty:
                        cancel.set()
                        yield f"data: {json.dumps({'error': 'The operation timed out — the model may be overloaded. Please try again.'})}\n\n"
                        break
                    if item is _DONE:
                        break
                    yield f"data: {json.dumps(item)}\n\n"
                    if isinstance(item, dict) and item.get("error"):
                        cancel.set()
                        break
            finally:
                cancel.set()
            yield "data: [DONE]\n\n"
        finally:
            guard.stream_finished()

    resp = Response(gen(), mimetype="text/event-stream")
    # 4.3 Cache-Control: no-cache / X-Accel-Buffering: no 
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.call_on_close(guard.stream_finished)
    return resp
