import json
import logging
import queue
import threading
import time
from typing import Callable, List, Optional
from flask import Response, jsonify

# sanitise_error_msg is owned by api.security (see root CLAUDE.md §Module
# Ownership); put_done_resilient by core.utils. The 2026-07-04 Batch 2 commit
# shipped importing both from core.utils, which made `import app` fail at boot.
from api.security import sanitise_error_msg
from core.utils import put_done_resilient
from core.perf_metrics import enabled, record_latency

logger = logging.getLogger(__name__)


#: retry.py's largest per-gap ceiling (including Retry-After). Mirrored, not imported,
#: to keep this module's import graph flat; a change there must be reflected
#: here (pinned by test_sse_timeout_chain.py).
_RETRY_BACKOFF_MAX_S = 90


def online_aware_floor_s(cfg: Optional[dict]) -> int:
    """The single-shot consumer stall floor, widened by ``online_timeout_s``.

    Timeout-chain repair (2026-07-22 audit M2): ``online_timeout_s`` — the
    inner HTTP bound for every online adapter call — validates up to 600 s,
    but the consumer stall guard was fixed at ``SSE_SINGLE_SHOT_FLOOR_S``
    (300 s), so a slow-thinking online model whose first token arrives at
    t≈400 s was inside its legal provider bound yet the app killed (and, for
    online providers, still billed) the call. "Outer must exceed inner" now
    holds: the floor is max(default floor, the online budget). Taking the max
    unconditionally is safe for local providers — it can only LENGTHEN the
    stall guard, and only when the user raised the knob above the floor.

    # TIMEOUT CHAIN (2026-09-02): the online budget counts the RETRIES.
    # Before: the floor widened to one online_timeout_s, but the adapters
    #   retry a transient failure ``online_max_retries`` times (attempts, not
    #   extra tries — retry.py's max_attempts), each bounded by that same
    #   timeout, with exponential backoff or a Retry-After wait between them. At the
    #   validator's own maxima the inner reaches 600 x 10 while the outer sat
    #   at 600, and at a plausible 150 s / 3 attempts the inner reaches 450+
    #   against an outer of 330 — so the app killed, and the provider still
    #   billed, a call that was inside its configured budget.
    # Change: the widened floor is attempts x timeout plus a bounded backoff
    #   allowance, so the guard genuinely outlasts the retry ladder it is
    #   supposed to contain.
    # Safe because: this only ever LENGTHENS the stall window, and only for a
    #   config that asked for a long one. The floor is unchanged for the
    #   short retry budgets that fit below it. Retry-After is budgeted at
    #   its 90-second ceiling, including for non-agent calls without a deadline. Every other bound
    #   in the chain (the agent wall clock, the per-call timeout, the
    #   worker's own abandon-after guard) is untouched.
    # Cost, stated: a genuinely dead online stream now takes longer to be
    #   declared timed out on such a config. That is the correct direction —
    #   the alternative is killing legal calls — and the client-side fetch
    #   abort is the residual leg, recorded in CLAUDE.md.
    # Pinned by: test_sse_timeout_chain.py::TestOnlineFloorCountsRetries.
    """
    from core.constants import (
        SSE_SINGLE_SHOT_FLOOR_S, DEFAULT_ONLINE_TIMEOUT_S, DEFAULT_ONLINE_MAX_RETRIES,
    )
    from api.validators import coerce_int_in_range
    # Missing/malformed timeout still uses the adapter default, including
    # when a partial config specifies a longer retry ladder.
    online_s = (coerce_int_in_range((cfg or {}).get("online_timeout_s"), 5, 600)
                or DEFAULT_ONLINE_TIMEOUT_S)
    attempts = coerce_int_in_range((cfg or {}).get("online_max_retries"), 0, 10)
    if attempts is None:
        # Absent or malformed: the adapters would use the app default, so the
        # guard must budget for the same ladder.
        attempts = DEFAULT_ONLINE_MAX_RETRIES
    attempts = max(1, attempts)   # 0 retries still means one attempt
    # retry.py: 1 s, then doubling, capped at 16 s per wait — bounded above by
    # Retry-After can override that to 90 s; there are attempts-1 gaps.
    backoff_s = _RETRY_BACKOFF_MAX_S * max(0, attempts - 1)
    return max(SSE_SINGLE_SHOT_FLOOR_S, online_s * attempts + backoff_s)

def consumer_stall_s(cfg: Optional[dict], stage_budget_s: int = 0) -> int:
    """Silence backstop only; never extends the agent/stage deadline."""
    from core.constants import SSE_STALL_MARGIN_S
    return max(stage_budget_s, online_aware_floor_s(cfg)) + SSE_STALL_MARGIN_S


# PERF (2026-09-02): a process-wide cap on LIVE SSE workers.
# Before: every streaming route spawned a daemon worker per request with no
#   process-wide bound — 19 call sites across 9 route modules. A wedged local
#   backend accumulates one blocked thread per retry: the user re-sends, each
#   attempt parks a worker inside a provider read that has no deadline of its
#   own, and nothing anywhere refuses the next one. The threads are daemons,
#   so they are invisible to the Waitress pool bound.
# Change: a BoundedSemaphore of _MAX_LIVE_SSE_WORKERS slots, taken BEFORE the
#   Response is built (so a refusal is a real 503, not an error frame inside a
#   200) and released by the SAME _SSEOpGuard that owns the op lock — i.e.
#   when the stream has ended AND any spawned worker has exited.
# Safe because:
#   The slot's release is chained onto the caller's own `release` inside one
#   callback, so it inherits the guard's exactly-once, never-while-a-worker-
#   may-still-write ordering rather than adding a second lifetime. A route
#   that passes no release still gets the chained callback, which is the
#   never-iterated-generator case the guard already covers via
#   call_on_close. BoundedSemaphore, not Semaphore, so a double release
#   raises here instead of silently inflating the cap.
# Preserves: "release fires exactly once, when the stream has ended AND any
#   spawned worker exited" — the guard is unchanged; only what its callback
#   does is.
# Lock/thread contract: the slot is acquired on the REQUEST thread before the
#   response exists and released from whichever thread completes the pair
#   (guard-owned). The callback stays trivial, as _maybe_release_locked
#   requires.
# Pinned by: test_sse_worker_cap.py (red before the change: a 21st concurrent
#   stream still returned 200).
_MAX_LIVE_SSE_WORKERS = 12
_LIVE_SSE_SLOTS = threading.BoundedSemaphore(_MAX_LIVE_SSE_WORKERS)


def live_sse_workers() -> int:
    """How many SSE slots are currently taken (diagnostics + tests)."""
    return _MAX_LIVE_SSE_WORKERS - _LIVE_SSE_SLOTS._value


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
    release: Optional[Callable[[], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    on_worker_finished: Optional[Callable[[], None]] = None,
    on_stream_finished: Optional[Callable[[], None]] = None,
    heartbeat: Optional[Callable[[], bool]] = None,
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

    *heartbeat* (2026-09-02) is called on every frame the worker emits, and a
    ``False`` answer means this operation's lease has been STOLEN (its TTL
    lapsed while it was silent). The skeleton then sets the worker's ``cancel``
    event, so every existing ``if cancel.is_set(): return`` gate in front of a
    write already covers the steal case — the zombie-writer rule is enforced
    through the mechanism the workers already have, with no per-site changes.
    A frame is emitted so the loss is visible rather than silent.
    """
    # A response owns a slot in this exact pool, even if the module-level
    # pool is replaced while its daemon is still finishing (e.g. test teardown).
    slots = _LIVE_SSE_SLOTS
    if not slots.acquire(blocking=False):
        # Refused BEFORE the Response exists, so this is a real status code a
        # client can act on rather than an error frame inside a 200. The
        # caller acquired its op lock expecting THIS function to own the
        # release, so a refusal must hand it back — otherwise the cap would
        # strand exactly the locks Phase 0 stopped leaking. `on_stream_finished`
        # runs too: it is the never-started-worker path its own state machine
        # already models (no worker was spawned, so `on_worker_finished` is
        # deliberately NOT called). A Response, not a (body, status) tuple,
        # because a caller may set headers on what it gets back.
        if release is not None:
            try:
                release()
            except Exception:  # noqa: BLE001 — never mask the refusal
                logger.exception("SSE cap refusal could not release the op lock")
        if on_stream_finished is not None:
            try:
                on_stream_finished()
            except Exception:  # noqa: BLE001
                logger.exception("SSE cap refusal: stream observer failed")
        refused = jsonify({
            "error": "Too many streaming operations are already running. "
                     "Wait for one to finish (or cancel it) and try again.",
        })
        refused.status_code = 503
        return refused

    from api.audit_context import retain_for_worker
    release_context = retain_for_worker()

    _slot_released = threading.Event()

    def _release_slot_and_lock() -> None:
        # Trivial by contract: _SSEOpGuard calls this holding its own mutex.
        try:
            if release is not None:
                release()
        finally:
            if not _slot_released.is_set():
                _slot_released.set()
                slots.release()
                release_context()

    guard = _SSEOpGuard(_release_slot_and_lock)
    # A caller-supplied event lets a run be cancelled by an authoritative
    # run-id endpoint before or after the streaming transport exists. Keeping
    # one event preserves the state invariant: transport loss, explicit Stop,
    # and worker polling all observe the same monotonic cancellation request.
    # Existing callers retain the old per-response event and wire format.
    cancel = cancel_event if cancel_event is not None else threading.Event()
    if preflight_msgs is None:
        preflight_msgs = []

    def gen():
        metric_start = time.monotonic() if enabled() else None
        first_token_emitted = False
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
            # A frame proves the worker is alive: beat the caller's lease, and
            # if the beat is REFUSED the lease was stolen — stop this worker
            # before it can write over a newer holder's file.
            if heartbeat is not None and not cancel.is_set():
                try:
                    alive = heartbeat()
                except Exception:  # noqa: BLE001 — liveness must never be fatal
                    alive = True
                if not alive:
                    cancel.set()
                    try:
                        event_q.put_nowait(
                            {"error": "This operation's lock was reclaimed after "
                                      "it stopped reporting progress; stopping "
                                      "without writing."})
                    except queue.Full:
                        pass
                    return
            # Block (cancel-aware) on a full queue rather than drop frames; the 1 s
            # timeout lets us notice a cancel and stop feeding a dead consumer.
            blocked_since = None
            while True:
                if cancel.is_set():
                    # Cooperative workers may still have one terminal truth
                    # frame after observing cancellation. Preserve it for a
                    # live, draining stream without ever blocking a dead one;
                    # a full bounded queue proves there is no safe delivery
                    # slot and /status remains the recovery authority.
                    try:
                        event_q.put_nowait(item)
                    except queue.Full:
                        pass
                    return
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
                if on_worker_finished is not None:
                    try:
                        on_worker_finished()
                    except Exception:
                        # Completion bookkeeping must never strand the stream
                        # or resurrect a lock after the worker has stopped.
                        pass

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
                    if not first_token_emitted and isinstance(item, dict) and item.get("token"):
                        first_token_emitted = True
                        record_latency("sse.first_token_emit", metric_start)
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
            record_latency("sse.stream_total", metric_start)
            _finish_stream()

    resp = Response(gen(), mimetype="text/event-stream")
    # 4.3 Cache-Control: no-cache / X-Accel-Buffering: no 
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["X-PaperMind-SSE-Silence-S"] = str(consumer_timeout_s)
    def _finish_stream() -> None:
        guard.stream_finished()
        if on_stream_finished is not None:
            try:
                on_stream_finished()
            except Exception:
                # Lifecycle observers are advisory bookkeeping; the guard is
                # authoritative for lock release and has already run.
                pass

    def _close_response() -> None:
        # A disconnect is a cooperative cancellation request. Setting the
        # shared event before marking the stream done ensures the op lock is
        # still retained until a spawned worker has observed cancellation and
        # exited; an uniterated response has no worker and releases promptly.
        cancel.set()
        _finish_stream()

    resp.call_on_close(_close_response)
    return resp
