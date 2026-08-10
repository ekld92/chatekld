import json
import queue
import threading
import time
from typing import Callable, List, Optional
from flask import Response

# sanitise_error_msg is owned by api.security (see root CLAUDE.md §Module
# Ownership); put_done_resilient by core.utils. The 2026-07-04 Batch 2 commit
# shipped importing both from core.utils, which made `import app` fail at boot.
from api.security import sanitise_error_msg
from core.utils import put_done_resilient


def online_aware_floor_s(cfg: Optional[dict]) -> int:
    """The single-shot consumer stall floor, widened by ``online_timeout_s``.

    Timeout-chain repair (2026-07-22 audit M2): ``online_timeout_s`` — the
    inner HTTP bound for every online adapter call — validates up to 600 s,
    but the consumer stall guard was fixed at ``SSE_SINGLE_SHOT_FLOOR_S``
    (300 s), so a slow-thinking online model whose first token arrives at
    t≈400 s was inside its legal provider bound yet the app killed (and, for
    online providers, still billed) the call. "Outer must exceed inner" now
    holds: the floor is max(default floor, online_timeout_s). Taking the max
    unconditionally is safe for local providers — it can only LENGTHEN the
    stall guard, and only when the user raised the knob above the floor.
    """
    from core.constants import SSE_SINGLE_SHOT_FLOOR_S
    from api.validators import coerce_int_in_range
    online_s = coerce_int_in_range((cfg or {}).get("online_timeout_s"), 5, 600) or 0
    return max(SSE_SINGLE_SHOT_FLOOR_S, online_s)

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
        # Invoked WITH self._mu held. Safe only because every release callback
        # is a bare Lock.release — a callback that re-entered this guard (or
        # called back into the guard's owner) would deadlock on _mu. Keep
        # release callbacks trivial.
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

        # Ceiling on CONTINUOUS full-queue blocking (2026-07-22 audit P6): a
        # half-open client (peer stops reading but never closes — laptop
        # sleep, network-path death) parks the consumer inside a
        # socket-blocked `yield`, so it never reaches its queue get, never
        # trips its stall timeout, and never sets `cancel` — the worker then
        # spun in `put` forever, pinning its thread, a waitress pool slot and
        # (deck/docgen) the op lock until the kernel killed the TCP
        # connection, possibly hours later. A LIVE consumer frees a slot
        # within its own stall window, so blocking longer than that window
        # plus a margin proves the consumer is wedged: the worker declares it
        # dead itself. The timer measures one frame's continuous blocking
        # (reset on every successful put), so a slow-but-draining consumer is
        # never misclassified.
        abandon_after_s = max(60.0, float(consumer_timeout_s)) + 60.0

        def put(item):
            # Block (cancel-aware) on a full queue rather than drop frames; the 1 s
            # timeout lets us notice a cancel and stop feeding a dead consumer.
            blocked_since = None
            while not cancel.is_set():
                try:
                    event_q.put(item, timeout=1)
                    return
                except queue.Full:
                    now = time.monotonic()
                    if blocked_since is None:
                        blocked_since = now
                    elif now - blocked_since >= abandon_after_s:
                        # See abandon_after_s above — consumer provably wedged.
                        cancel.set()
                        return
                    continue

        def _runner():
            try:
                worker(put, cancel)
            except Exception as exc:  # noqa: BLE001 — surface anything as an SSE error
                if not cancel.is_set():
                    put({"error": sanitise_error_msg(exc)})
            finally:
                put_done_resilient(event_q, _DONE, cancel,
                                   max_wait_s=abandon_after_s)
                guard.worker_finished()

        try:
            for msg in preflight_msgs:
                yield f"data: {json.dumps({'info': msg})}\n\n"
            guard.worker_spawned()
            try:
                threading.Thread(target=_runner, daemon=True).start()
            except RuntimeError as exc:
                # B9 (2026-07-13): under thread exhaustion Thread.start() raises.
                # worker_spawned() was already recorded, so the guard would wait
                # forever for a worker_finished() that can never come and leak the
                # op lock (every later deck/docgen op 409s until restart). Mark
                # the worker finished so the guard can release, surface the error.
                guard.worker_finished()
                yield f"data: {json.dumps({'error': sanitise_error_msg(exc)})}\n\n"
                yield "data: [DONE]\n\n"
                return
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
                    # ANY {"error"} frame TERMINATES the whole stream — the
                    # single most consequence-laden rule in the streaming
                    # stack (2026-07-22 comment repair: it previously lived
                    # only in deck.py/CLAUDE.md). It is why the deck/docgen
                    # workers RELABEL per-section provider errors to {"info"}:
                    # an un-relabelled turn-level error here discards the
                    # entire deck (the historical random-failure mode). A new
                    # SSE route's worker must reserve {"error"} for genuinely
                    # unrecoverable states.
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
