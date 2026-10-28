"""Run a blocking action off the Waitress request thread, with a bound.

Promoted out of ``api/routes/refactor.py`` on 2026-09-02 (performance plan,
Phase 1) so the routes that were still doing the same thing inline — the two
upload paths, ``extract-image`` — can share ONE implementation of the shape
rather than each growing a near-identical single-flight registry that drifts.

The shape, unchanged from the 2026-07-04 item 2.3 original:

* ``fn(should_cancel)`` runs on a daemon thread; the request thread joins with
  a wall-clock deadline and is freed on expiry (the caller answers **504**).
  The daemon keeps running until its underlying call returns on its own — the
  accepted trade-off the SSE routes document — so an action that runs past the
  deadline must have no side effect the caller has already denied.
* ``should_cancel`` is a zero-arg callable the action polls to abort early. It
  is set BEFORE the timeout returns, so a daemon that polls stops instead of
  running a dead generation to completion. It cannot stop a transport wedged
  before its first byte; that residual is recorded in CLAUDE.md.
* **Per-action single-flight.** While a previous daemon for the same *action*
  name is alive, a new request is REFUSED (``busy=True`` → the caller answers
  **429**) instead of stacking another thread. The bound is therefore one live
  daemon per action name, not one per client retry.
* Exceptions raised by ``fn`` are re-raised on the request thread, so a
  caller's existing ``try/except`` behaves identically.

The **deadline is a parameter, not derived here**, because the right bound
differs per action: an LLM turn uses ``agent_wall_clock_s`` floored at the SSE
single-shot floor, while a PDF extraction must sit just outside
``PDF_EXTRACT_TIMEOUT_S`` or the helper would 504 a legal extraction and throw
away work the user is waiting for. Each caller owns its own — and keeping the
resolution at the call site is also what keeps
``api.routes.refactor._llm_action_deadline_s`` a live patch target.

**Action names are global.** The registry is one process-wide dict, so two
callers sharing a name serialise against each other. Give each route+kind its
own name unless serialising them together is the intent.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional, Tuple

# One live daemon per action name. Guarded by its own mutex, which is NEVER
# held across a join or across the action itself.
_ACTION_MU = threading.Lock()
_ACTION_INFLIGHT: dict[str, threading.Thread] = {}


def run_bounded_action(
    action: str,
    fn: Callable[[Callable[[], bool]], object],
    *,
    deadline_s: float,
) -> Tuple[Optional[object], bool, bool]:
    """Run *fn* on a daemon thread, bounded by *deadline_s*.

    Returns ``(result, timed_out, busy)``. Exactly one of the three outcomes
    holds: a result, a timeout (the daemon was told to cancel), or a refusal
    because another daemon holds this action name.
    """
    box: dict = {}
    cancel = threading.Event()

    def _worker() -> None:
        try:
            box["result"] = fn(cancel.is_set)
        except BaseException as exc:  # noqa: BLE001 — ferried to the request thread
            box["exc"] = exc
        finally:
            with _ACTION_MU:
                if _ACTION_INFLIGHT.get(action) is threading.current_thread():
                    del _ACTION_INFLIGHT[action]

    t = threading.Thread(target=_worker, daemon=True,
                         name=f"bounded-action-{action}")
    with _ACTION_MU:
        prev = _ACTION_INFLIGHT.get(action)
        if prev is not None:
            return None, False, True
        _ACTION_INFLIGHT[action] = t
    try:
        t.start()
    except BaseException:
        with _ACTION_MU:
            if _ACTION_INFLIGHT.get(action) is t:
                del _ACTION_INFLIGHT[action]
        raise
    t.join(timeout=deadline_s)
    if t.is_alive():
        # Free the client AND tell the daemon to stop at its next poll point.
        cancel.set()
        return None, True, False
    if "exc" in box:
        raise box["exc"]
    return box.get("result"), False, False
