"""Background audit-run manager.

Mirrors the shape of :class:`rag.vault.ObsidianVaultManager` for the
subset of features the audit needs:

- exactly one scan thread at a time
- ``idle | scanning | done | error | cancelled`` state machine
- drainable status message buffer (polled by ``/api/audit/status``)
- cooperative cancel via ``request_cancel`` checked between phases
- last computed :class:`audit.engine.inventory.Inventory` cached in
  memory so the report endpoints serve it without recomputing

Critical: the manager is instantiated at import time but **never starts
a scan on its own**. :func:`start_scan` is the only entry point that
spawns the worker thread, and it is called exclusively from the
``POST /api/audit/scan`` route handler.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .config import AuditConfigError, Settings, load_settings
from .engine import duplicates as eng_duplicates
from .engine import inventory as eng_inventory

logger = logging.getLogger(__name__)

# Status values surfaced to the UI.  Keep this list small so the JS-side
# switch stays readable.
STATE_IDLE = "idle"
STATE_SCANNING = "scanning"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_CANCELLED = "cancelled"


class AuditManager:
    """Singleton container for the audit's background state.

    Thread safety: ``_state_lock`` guards every field that the worker
    thread mutates and the request thread reads (state, settings,
    inventory cache, duplicates cache, error string, started/finished
    timestamps).  ``_messages_lock`` separately guards the rolling status
    buffer so a slow reader cannot block scan progress.
    """

    def __init__(self) -> None:
        self._state: str = STATE_IDLE
        self._state_lock: threading.Lock = threading.Lock()
        self._messages: list[str] = []
        self._messages_lock: threading.Lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()
        # Last full inventory, kept in memory only.  Reset on every scan
        # so a stale inventory cannot leak into a new run.
        self._inventory: Optional[eng_inventory.Inventory] = None
        self._duplicates: Optional[list[eng_duplicates.DuplicateSet]] = None
        self._settings: Optional[Settings] = None
        self._error: str = ""
        self._started_at: float = 0.0
        self._finished_at: float = 0.0
        # Run-id increments per scan so a stale cancel request from the
        # previous run cannot abort the current one.
        self._run_id: int = 0

    # ------------------------------------------------------------------
    # Status / messages
    # ------------------------------------------------------------------
    def _emit(self, msg: str) -> None:
        """Log a status line and append it to the rolling buffer (capped at 200).

        Called from the worker thread (and passed as ``progress_fn`` into the
        inventory build). The ``del [:-200]`` keeps only the most recent 200
        lines so a multi-hour scan can't grow the buffer without bound.
        """
        logger.info("AuditManager: %s", msg)
        with self._messages_lock:
            self._messages.append(msg)
            # Cap the buffer so a long-running scan cannot grow it unbounded.
            del self._messages[:-200]

    def drain_messages(self) -> list[str]:
        """Atomically return and clear the buffered status lines.

        ``/api/audit/status`` polls this, so each line is delivered to the UI
        exactly once. Guarded by ``_messages_lock`` independently of the state
        lock so draining never blocks scan progress.
        """
        with self._messages_lock:
            out = list(self._messages)
            self._messages.clear()
            return out

    def clear_messages(self) -> None:
        """Drop all buffered status lines (called when a fresh scan starts)."""
        with self._messages_lock:
            self._messages.clear()

    def get_status_payload(self) -> dict:
        """Snapshot the status for ``/api/audit/status``.

        Reads the live fields under ``_state_lock`` (consistent snapshot), then
        drains the message buffer outside it. ``started_at``/``finished_at``
        are coerced to ``None`` when unset so the UI gets ``null`` not ``0.0``.
        """
        with self._state_lock:
            state = self._state
            error = self._error
            started = self._started_at
            finished = self._finished_at
            has_results = self._inventory is not None
            has_duplicates = self._duplicates is not None
        return {
            "state": state,
            "error": error,
            "started_at": started or None,
            "finished_at": finished or None,
            "has_results": has_results,
            "has_duplicates": has_duplicates,
            "messages": self.drain_messages(),
        }

    # ------------------------------------------------------------------
    # Public actions
    # ------------------------------------------------------------------
    def start_scan(
        self,
        *,
        count_annotations: bool = True,
        include_duplicates: bool = True,
    ) -> tuple[bool, str]:
        """Start a scan thread. Returns ``(started, message)``.

        Returns ``(False, ...)`` when a scan is already in flight or when
        the audit config is incomplete (no vault path, etc.). Either
        condition is communicated to the caller as a string rather than
        raised, since the route layer turns it into a 4xx response.
        """
        with self._state_lock:
            if self._state == STATE_SCANNING:
                return False, "A scan is already in progress."
            try:
                settings = load_settings()
            except AuditConfigError as exc:
                self._state = STATE_ERROR
                self._error = str(exc)
                return False, str(exc)

            # Reset transient state.  We keep the previous inventory live
            # until the new run completes so the UI has data to show
            # during the brief gap; the cached result is replaced under
            # the same lock once the worker finishes.
            self._settings = settings
            self._stop_event = threading.Event()
            self._error = ""
            self._started_at = time.time()
            self._finished_at = 0.0
            self._state = STATE_SCANNING
            self._run_id += 1
            run_id = self._run_id

        self.clear_messages()
        self._emit(f"Starting audit scan against {settings.vault_root}")

        thread = threading.Thread(
            target=self._run,
            name=f"audit-scan-{run_id}",
            args=(settings, count_annotations, include_duplicates, run_id),
            daemon=True,
        )
        with self._state_lock:
            self._thread = thread
        thread.start()
        return True, "Scan started."

    def request_cancel(self) -> bool:
        """Signal the worker to abort. Returns True if a scan was in flight."""
        with self._state_lock:
            if self._state != STATE_SCANNING:
                return False
            self._stop_event.set()
        self._emit("Cancellation requested.")
        return True

    def is_scanning(self) -> bool:
        """True iff a scan is currently in flight (state-lock guarded read)."""
        with self._state_lock:
            return self._state == STATE_SCANNING

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """Wait for the worker thread to finish. Returns True if it did."""
        with self._state_lock:
            thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # ------------------------------------------------------------------
    # Result access
    # ------------------------------------------------------------------
    def get_inventory(self) -> tuple[Optional[eng_inventory.Inventory], Optional[Settings]]:
        """Return the cached inventory + the settings it was built with.

        Both are ``None`` until the first scan completes; the report endpoints
        return 404 in that case. The pair is read together under the lock so
        the inventory and the settings used to serialize its paths can't be
        torn apart by a concurrent reset/new-scan.
        """
        with self._state_lock:
            return self._inventory, self._settings

    def get_duplicates(self) -> tuple[Optional[list[eng_duplicates.DuplicateSet]], Optional[Settings]]:
        """Return the cached duplicate sets + their settings (None until ready)."""
        with self._state_lock:
            return self._duplicates, self._settings

    def clear_results(self) -> None:
        """Drop cached results without touching the running state."""
        with self._state_lock:
            self._inventory = None
            self._duplicates = None

    def reset_to_idle(self) -> None:
        """Cancel any in-flight scan, drop cached results, return to idle.

        Bumps ``_run_id`` so a worker finishing mid-reset cannot win the
        race and repopulate ``_inventory`` via ``_finalise`` — the stale
        completion's run_id will not match and its update is dropped.

        Called from ``/api/reset`` so the post-reset audit tab matches
        the "no scan yet" empty state. The worker thread (if any) is
        signalled to exit but not joined, since the reset handler is
        synchronous and should not block the user.
        """
        with self._state_lock:
            self._stop_event.set()
            self._run_id += 1
            self._inventory = None
            self._duplicates = None
            self._error = ""
            self._state = STATE_IDLE
            self._started_at = 0.0
            self._finished_at = 0.0
        self.clear_messages()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def _run(
        self,
        settings: Settings,
        count_annotations: bool,
        include_duplicates: bool,
        run_id: int,
    ) -> None:
        """The scan worker body (one per thread); never called directly.

        Two phases, cancel-checked between them: build the inventory, then —
        if opted in — hash for duplicates. ``_stop_event`` is polled after each
        phase (the per-PDF pikepdf open inside a phase is not interruptible), so
        a cancel finalises with whatever partial result exists and the right
        ``clear_dupes`` semantics. After the inventory phase it drains the
        Obsidian frontmatter-parse warnings into the feed (a skipped note's
        tags are invisible to the drift report, so the user must be told) and
        surfaces any ``zotero_error``. Every exit routes through
        :meth:`_finalise` with this thread's ``run_id`` so a stale completion
        (a newer scan already started) drops its result instead of clobbering
        the live one. The blanket ``except`` is the thread's last line of
        defence — an unhandled error becomes ``STATE_ERROR``, not a dead thread.
        """
        try:
            self._emit("Building inventory (bib + bridge + Zotero + Obsidian)...")
            inv = eng_inventory.build_inventory(
                settings,
                count_annotations=count_annotations,
                cancel_fn=self._stop_event.is_set,
                # Route the unmapped-PDF annotation phase's progress lines into
                # the status feed so the (potentially multi-second) parallel
                # read is visible in the UI and Cancel has something to act on.
                progress_fn=self._emit,
            )
            if self._stop_event.is_set():
                # Cancelled before duplicate detection ever ran: clear the
                # duplicate cache so /api/audit/reports/duplicates does
                # not serve stale results from a prior run.
                self._finalise(
                    STATE_CANCELLED, run_id, inv=inv, dupes=None, clear_dupes=True
                )
                self._emit("Scan cancelled after inventory phase.")
                return

            if inv.zotero_error:
                self._emit(f"Zotero read warning: {inv.zotero_error}")

            # Surface YAML frontmatter-parse failures in the UI feed —
            # previously they only went to the app log, so notes whose tags
            # were silently skipped (and could show false note_tag_drift)
            # were invisible to the user.
            from .core import obsidian as core_obsidian
            fm_warnings = core_obsidian.drain_parse_warnings()
            _SHOWN = 5
            for msg in fm_warnings[:_SHOWN]:
                self._emit(f"WARNING: {msg} — note skipped (tags not read).")
            if len(fm_warnings) > _SHOWN:
                self._emit(
                    f"WARNING: {len(fm_warnings) - _SHOWN} more note(s) with "
                    "malformed frontmatter — see chatekld.log for the full list."
                )

            duplicates: Optional[list[eng_duplicates.DuplicateSet]] = None
            if include_duplicates:
                self._emit(
                    f"Hashing PDFs under {settings.biblio_articles_dir} for duplicates..."
                )
                duplicates = eng_duplicates.find_biblio_duplicates(
                    settings, cancel_fn=self._stop_event.is_set
                )
                if self._stop_event.is_set():
                    self._finalise(STATE_CANCELLED, run_id, inv=inv, dupes=duplicates)
                    self._emit("Scan cancelled during duplicate detection.")
                    return

                self._finalise(STATE_DONE, run_id, inv=inv, dupes=duplicates)
            else:
                # User explicitly opted out of duplicate detection on this
                # run; clear any prior duplicates so the UI does not
                # report ``has_duplicates: true`` against a fresh inventory.
                self._finalise(
                    STATE_DONE, run_id, inv=inv, dupes=None, clear_dupes=True
                )
            self._emit(
                f"Scan complete: {len(inv.records)} records, "
                f"{len(inv.bridge.unmapped_pdfs)} unmapped PDFs"
                + (f", {len(duplicates)} duplicate sets" if duplicates is not None else "")
                + "."
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Audit scan failed")
            self._finalise(STATE_ERROR, run_id, error=f"{type(exc).__name__}: {exc}")
            self._emit(f"ERROR: {type(exc).__name__}: {exc}")

    def _finalise(
        self,
        state: str,
        run_id: int,
        *,
        inv: Optional[eng_inventory.Inventory] = None,
        dupes: Optional[list[eng_duplicates.DuplicateSet]] = None,
        error: str = "",
        clear_dupes: bool = False,
    ) -> None:
        """Atomically update the result cache + state under ``_state_lock``.

        ``dupes=None`` means "leave the duplicate cache untouched" so a
        cancelled scan that completed duplicate detection still keeps the
        partial result. ``clear_dupes=True`` means "explicitly drop the
        cached duplicates because this run did not produce a fresh set";
        the worker passes it whenever ``include_duplicates`` was false or
        the scan was cancelled before duplicate detection began.
        """
        with self._state_lock:
            # Stale run completion (a new scan started while this one was
            # still finishing): drop the result on the floor and leave
            # the live state alone.
            if run_id != self._run_id:
                return
            if inv is not None:
                self._inventory = inv
            if dupes is not None:
                self._duplicates = dupes
            elif clear_dupes:
                self._duplicates = None
            self._state = state
            if error:
                self._error = error
            self._finished_at = time.time()


# Module-level singleton imported by the route layer and by tests that
# verify create_app() does not auto-start a scan.
audit_manager = AuditManager()
