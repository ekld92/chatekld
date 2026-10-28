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

from .config import AuditConfigError, Settings, load_settings, settings_context
from .engine import duplicates as eng_duplicates
from .engine import dup_decisions as eng_dup_decisions
from .engine import inventory as eng_inventory
from .engine import near_duplicates as eng_near_duplicates

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
        self._near_duplicates: Optional[eng_near_duplicates.NearDupReport] = None
        self._settings: Optional[Settings] = None
        self._scan_settings: Optional[Settings] = None
        # Route-injected page-1 PDF-text provider for the near-duplicate
        # tiers (plan 2026-08-05/4); None on the CLI path.
        self._pdf_text_provider = None
        self._error: str = ""
        self._started_at: float = 0.0
        self._finished_at: float = 0.0
        # Run-id increments per scan so a stale cancel request from the
        # previous run cannot abort the current one.
        self._run_id: int = 0
        # Opaque callbacks fired whenever the cached result is REPLACED or
        # CLEARED (finding 4, 2026-07-20 audit). The route layer registers its
        # search-corpus cache invalidator here so a replaced Inventory is not
        # pinned in the corpus memo until the next search. Kept opaque so this
        # vendored module never imports the route (dependency direction holds).
        self._result_listeners: list = []

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
            has_near = self._near_duplicates is not None
        return {
            "state": state,
            "error": error,
            "started_at": started or None,
            "finished_at": finished or None,
            "has_results": has_results,
            "has_duplicates": has_duplicates,
            "has_near_duplicates": has_near,
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
        include_near_duplicates: bool = True,
        pdf_text_provider=None,
        reconciliation_refresh=None,
    ) -> tuple[bool, str]:
        """Start a scan thread. Returns ``(started, message)``.

        Returns ``(False, ...)`` when a scan is already in flight or when
        the audit config is incomplete (no vault path, etc.). Either
        condition is communicated to the caller as a string rather than
        raised, since the route layer turns it into a 4xx response.

        ``pdf_text_provider`` (plan 2026-08-05/4) is an optional
        ``Callable[[Path], str | None]`` giving cache-first, page-bounded
        page-1 text for a PDF — built by the ROUTE layer over the app's PDF
        text cache and injected here, per the vendored-tree rule (the same
        seam shape ``search.build_corpus`` uses for ``vocab``). ``None`` (the
        CLI path) degrades the text-based near-duplicate tiers to hash-only.
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
            self._scan_settings = settings
            self._pdf_text_provider = pdf_text_provider
            self._stop_event = threading.Event()
            self._error = ""
            self._started_at = time.time()
            self._finished_at = 0.0
            self._state = STATE_SCANNING
            self._run_id += 1
            run_id = self._run_id
            # B10/#38 (2026-07-13): capture THIS run's stop event and pass it to
            # the worker, which polls it by local reference. Polling
            # ``self._stop_event`` let a reset_to_idle + immediate re-scan replace
            # the attribute before the old worker observed the set event, silently
            # losing its cancellation (two scans then run concurrently). The
            # run_id still drops the stale result; this stops the wasted work.
            stop_event = self._stop_event

        self.clear_messages()
        self._emit(f"Starting audit scan against {settings.vault_root}")

        thread = threading.Thread(
            target=self._run,
            name=f"audit-scan-{run_id}",
            args=(settings, count_annotations, include_duplicates,
                  include_near_duplicates, run_id, stop_event, reconciliation_refresh, pdf_text_provider),
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
            inventory, settings = self._inventory, self._settings
        if settings is None or inventory is None:
            return None, None
        try:
            if settings_context(settings) != settings_context(load_settings()):
                return None, None
        except (AuditConfigError, OSError):
            return None, None
        with self._state_lock:
            if inventory is not self._inventory or settings is not self._settings:
                return None, None
            return inventory, settings

    def get_duplicates(self) -> tuple[Optional[list[eng_duplicates.DuplicateSet]], Optional[Settings]]:
        """Return the cached duplicate sets + their settings (None until ready)."""
        inventory, settings = self.get_inventory()
        with self._state_lock:
            if (inventory is None or inventory is not self._inventory
                    or settings is not self._settings):
                return None, None
            return self._duplicates, settings

    def get_near_duplicates(self) -> tuple[Optional["eng_near_duplicates.NearDupReport"], Optional[Settings]]:
        """Return the cached near-duplicate report + settings (None until ready
        or when the phase was skipped on the last run)."""
        inventory, settings = self.get_inventory()
        with self._state_lock:
            if (inventory is None or inventory is not self._inventory
                    or settings is not self._settings):
                return None, None
            return self._near_duplicates, settings

    def get_scan_times(self) -> tuple[float, float]:
        """Return ``(started_at, finished_at)`` for the last/current scan.

        A NON-DRAINING companion to :meth:`get_status_payload`, which calls
        ``drain_messages()`` and therefore consumes the one-shot status feed the
        UI is polling. A data endpoint that just wants to say how old its
        snapshot is must never pay that price — the same reason the widgets are
        forbidden from touching the status routes. Zeros mean "never ran".
        """
        with self._state_lock:
            return self._started_at, self._finished_at

    def register_result_listener(self, cb) -> None:
        """Register a callback fired when the cached result is replaced/cleared.

        The route layer registers its search-corpus invalidator so a superseded
        Inventory is dropped promptly (finding 4). Idempotent per callback.
        """
        if cb not in self._result_listeners:
            self._result_listeners.append(cb)

    def _fire_result_listeners(self) -> None:
        """Call every result listener outside the state lock (never raises)."""
        for cb in list(self._result_listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001 — a listener must never break a scan
                logger.exception("AuditManager result listener failed")

    def clear_results(self) -> None:
        """Drop cached results without touching the running state."""
        with self._state_lock:
            self._inventory = None
            self._settings = None
            self._duplicates = None
            self._near_duplicates = None
        self._fire_result_listeners()

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
            self._settings = None
            self._duplicates = None
            self._near_duplicates = None
            self._error = ""
            self._state = STATE_IDLE
            self._started_at = 0.0
            self._finished_at = 0.0
        self.clear_messages()
        self._fire_result_listeners()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def _run(
        self,
        settings: Settings,
        count_annotations: bool,
        include_duplicates: bool,
        include_near_duplicates: bool,
        run_id: int,
        stop_event: "threading.Event",
        reconciliation_refresh=None,
        pdf_text_provider=None,
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
            # Full curated-hash re-anchor, ONCE per scan (2026-07-20 audit
            # finding 2). This picks up a PDF annotated since it was curated so
            # a later move stays recoverable. It used to run on every /api/audit/
            # mapping POST — O(curated) full-file hashes per triage click, so a
            # deck session against a large curation set grew each Assign's
            # latency without bound. The scan is the right (infrequent) home; the
            # per-POST writers still anchor the PDF they just wrote. Best-effort.
            try:
                from .engine import bridge as _eng_bridge
                _eng_bridge.refresh_mapping_hashes(settings.mapping_file, settings.vault_root)
            except Exception:  # noqa: BLE001 — a stale anchor must not fail a scan
                logger.debug("scan-time mapping-hash re-anchor skipped", exc_info=True)

            reconciliation_error = None
            if reconciliation_refresh is not None:
                try:
                    refresh_result = reconciliation_refresh(settings)
                    if isinstance(refresh_result, dict) and not refresh_result.get('freshness', {}).get('available', True):
                        reconciliation_error = refresh_result['freshness'].get('error') or 'Evidence unavailable'
                        self._emit('PDF reconciliation unavailable: ' + reconciliation_error)
                except Exception:  # noqa: BLE001
                    reconciliation_error = 'Refresh failed; inspect the log'
                    logger.exception('PDF reconciliation refresh failed')
                    self._emit('PDF reconciliation unavailable; previous successful observations retained.')
            self._emit("Building inventory (bib + bridge + Zotero + Obsidian)...")
            inv = eng_inventory.build_inventory(
                settings,
                count_annotations=count_annotations,
                cancel_fn=stop_event.is_set,
                # Route the unmapped-PDF annotation phase's progress lines into
                # the status feed so the (potentially multi-second) parallel
                # read is visible in the UI and Cancel has something to act on.
                progress_fn=self._emit,
                # Feeds the bridge's DOI resolution rung (step 3.5). Same
                # cache-first, page-1-bounded callable the near-duplicate
                # tiers use; None on the CLI path degrades that rung to
                # embedded PDF metadata only.
                pdf_text_provider=pdf_text_provider,
            )
            if stop_event.is_set():
                # Cancelled before duplicate detection ever ran: clear the
                # duplicate cache so /api/audit/reports/duplicates does
                # not serve stale results from a prior run.
                self._finalise(
                    STATE_CANCELLED, run_id, inv=inv, dupes=None,
                    clear_dupes=True, near=None, clear_near=True,
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

            # Persistent full-hash memo (plan 2026-08-05/4): unchanged files
            # never re-read. Loaded once for both hash-driven phases,
            # saved after whichever ran last; a cancelled run still keeps
            # whatever it hashed.
            from .core import hash_cache as core_hash_cache
            _hash_memo = core_hash_cache.load_cache()

            duplicates: Optional[list[eng_duplicates.DuplicateSet]] = None
            if include_duplicates:
                self._emit(
                    f"Hashing PDFs under {settings.biblio_articles_dir} for duplicates..."
                )
                duplicates = eng_duplicates.find_biblio_duplicates(
                    settings, cancel_fn=stop_event.is_set,
                    hash_cache=_hash_memo,
                )
                if stop_event.is_set():
                    core_hash_cache.save_cache(_hash_memo)
                    self._finalise(
                        STATE_CANCELLED, run_id, inv=inv, dupes=duplicates,
                        near=None, clear_near=True,
                    )
                    self._emit("Scan cancelled during duplicate detection.")
                    return

            near: Optional[eng_near_duplicates.NearDupReport] = None
            if include_near_duplicates:
                self._emit("Scanning for near-duplicate candidates...")
                near = eng_near_duplicates.find_near_duplicates(
                    settings, inv, duplicates or [],
                    pdf_text_provider=pdf_text_provider,
                    hash_cache=_hash_memo,
                    dismissed=eng_dup_decisions.load_dismissed(),
                    ratio_cutoff=settings.near_dup_ratio,
                    cancel_fn=stop_event.is_set,
                    progress_fn=self._emit,
                )
            core_hash_cache.save_cache(_hash_memo)
            if stop_event.is_set():
                self._finalise(
                    STATE_CANCELLED, run_id, inv=inv,
                    dupes=duplicates, clear_dupes=duplicates is None,
                    near=near, clear_near=near is None,
                )
                self._emit("Scan cancelled during near-duplicate detection.")
                return

            # ``clear_*`` fires when the user opted out of that phase on this
            # run, so the UI never reports stale results against a fresh
            # inventory.
            self._finalise(
                STATE_DONE, run_id, inv=inv,
                dupes=duplicates, clear_dupes=duplicates is None,
                near=near, clear_near=near is None,
            )
            self._emit(
                f"Scan complete: {len(inv.records)} records, "
                f"{len(inv.bridge.unmapped_pdfs)} unmapped PDFs"
                + (f", {len(duplicates)} duplicate sets" if duplicates is not None else "")
                + (f", {len(near.candidates)} near-duplicate candidates" if near is not None else "")
                + ("; PDF reconciliation unavailable." if reconciliation_error else ".")
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
        near: Optional["eng_near_duplicates.NearDupReport"] = None,
        clear_near: bool = False,
    ) -> None:
        """Atomically update the result cache + state under ``_state_lock``.

        ``dupes=None`` means "leave the duplicate cache untouched" so a
        cancelled scan that completed duplicate detection still keeps the
        partial result. ``clear_dupes=True`` means "explicitly drop the
        cached duplicates because this run did not produce a fresh set";
        the worker passes it whenever ``include_duplicates`` was false or
        the scan was cancelled before duplicate detection began.
        ``near``/``clear_near`` carry the exact same semantics for the
        near-duplicate report (plan 2026-08-05/5) — note an EMPTY list/report
        is "ran, found nothing" and is stored, never confused with ``None``.
        """
        replaced_result = False
        with self._state_lock:
            # Stale run completion (a new scan started while this one was
            # still finishing): drop the result on the floor and leave
            # the live state alone.
            if run_id != self._run_id:
                return
            if inv is not None:
                self._inventory = inv
                self._settings = self._scan_settings
                replaced_result = True
            if dupes is not None:
                self._duplicates = dupes
            elif clear_dupes:
                self._duplicates = None
            if near is not None:
                self._near_duplicates = near
            elif clear_near:
                self._near_duplicates = None
            self._state = state
            if error:
                self._error = error
            self._finished_at = time.time()
        # Finding 4: a fresh Inventory replaced the old one — drop any cache
        # (the route's search corpus) that still references the superseded
        # object. Fired outside the state lock so a listener can't deadlock it.
        if replaced_result:
            self._fire_result_listeners()


# Module-level singleton imported by the route layer and by tests that
# verify create_app() does not auto-start a scan.
audit_manager = AuditManager()
