"""Widget snapshot endpoints (``/api/widget*``) + the scan-completion trigger.

The snapshot (``BASE_DIR/widget.json``, built by ``widget/snapshot.py``) is
written in exactly two situations — **no scheduler, no auto-scan**, the
audit's manual-only invariant is untouched:

* on scan completion, via ``audit_manager.register_result_listener`` (the
  search-corpus invalidator precedent) — registration at import time is
  passive, and the callback no-ops when the firing was a clear/reset
  (``get_inventory() == (None, None)``);
* on the explicit ``POST /api/widget/refresh`` (the audit tab's "Refresh
  widget" button).

``GET /api/widget`` serves the payload (memory → disk) so external widgets
can prefer a fresh copy while the server runs; when it doesn't, they read the
file directly. Widgets must NEVER poll ``/api/audit/status`` or
``/api/obsidian/status`` — those drain one-shot message buffers meant for the
GUI; ``/api/health`` is the only sanctioned liveness probe.
"""

from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request

from api.security import sanitise_error_msg
from api.validators import coerce_bool
from audit.manager import audit_manager
from core.config import load_config
from widget import snapshot as w_snapshot, store as w_store

widget_bp = Blueprint("widget", __name__)
logger = logging.getLogger(__name__)

# Last successfully-built payload, served by GET without a disk read. The
# build itself is serialized: the scan listener (worker thread) and the
# refresh route can otherwise interleave build/write/history updates.
_BUILD_LOCK = threading.Lock()
_last_payload: dict | None = None

# Cap on the PDF-text excerpt read (chars) — the builder clips to ~400.
_PDF_EXCERPT_CHARS = 600


def _pdf_text_reader(vault_root: Path):
    """Cache-first, page-1-bounded PDF text callable (the audit-scan seam)."""
    def _read(pdf_path: Path) -> str | None:
        try:
            from tagging.text_source import read_pdf_text

            text, _trunc = read_pdf_text(
                Path(pdf_path), vault_root, _PDF_EXCERPT_CHARS,
                fresh_max_pages=1)
            return text or None
        except Exception:  # noqa: BLE001 — excerpt is best-effort
            return None
    return _read


def _build_and_write(*, rng_seed: str | None = None) -> dict | None:
    """Build the payload from the cached scan and persist it. ``None`` = no scan.

    Never raises (callers include the scan worker thread): a failure logs and
    returns ``None`` — the previous snapshot on disk stays authoritative.
    """
    global _last_payload
    try:
        inv, settings = audit_manager.get_inventory()
        if inv is None or settings is None:
            return None
        with _BUILD_LOCK:
            from api.routes.audit import _drift_synonyms
            from api.routes.tagging import _read_vocab

            cfg = load_config()
            try:
                vocab, _rel = _read_vocab(cfg, settings.vault_root)
            except Exception:  # noqa: BLE001 — the notion slot is optional
                vocab = None
            prev = _last_payload or w_store.read_snapshot() or {}
            now = datetime.now(timezone.utc)
            history = w_store.load_history()
            # Same-day stability: the previous pick was appended to its own
            # anti-repeat history, so a same-day rebuild with the stable date
            # seed would EXCLUDE it from the pool and draw a different paper
            # on every refresh ("paper of the click"). Filtering it back out
            # of the history restores the same pool → same seed → same pick,
            # and the append guard below then correctly no-ops. A redraw
            # (rng_seed set) keeps the full history — a new draw is the point.
            prev_key = (prev.get("paper") or {}).get("key")
            if rng_seed is None and prev_key:
                prev_date = str(prev.get("generated_at") or "")[:10]
                if prev_date == now.date().isoformat():
                    history = [k for k in history if k != prev_key]
            payload = w_snapshot.build_payload(
                inv, settings,
                synonyms=_drift_synonyms(settings),
                history=history,
                notions=vocab,
                pdf_text_reader=_pdf_text_reader(settings.vault_root),
                now=now,
                rng_seed=rng_seed,
                # Private-attr read is deliberate: get_status_payload() DRAINS
                # the GUI's one-shot message feed, which this writer must not.
                # `or None`: start_scan resets _finished_at to 0.0, so a
                # refresh during a running scan must not ship the epoch.
                scan_finished_at=getattr(audit_manager, "_finished_at", 0.0) or None,
            )
            w_store.write_snapshot(payload)
            new_key = (payload.get("paper") or {}).get("key")
            if new_key and new_key != prev_key:
                w_store.append_history(new_key)
            _last_payload = payload
        _poke_swiftbar()
        return payload
    except Exception:  # noqa: BLE001
        logger.warning("widget snapshot build failed", exc_info=True)
        return None


def _poke_swiftbar() -> None:
    """Best-effort push refresh of the SwiftBar plugin (macOS only, never fatal)."""
    if sys.platform != "darwin":
        return
    try:
        from api.routes.open import run_open
        run_open(["open", "-g", "swiftbar://refreshplugin?name=chatekld"])
    except Exception:  # noqa: BLE001
        pass


# PERF (2026-09-02): the snapshot build leaves the SCAN thread.
# Before: the result listener ran _build_and_write inline, so a scan that had
#   already reported `done` kept its worker thread busy building the widget
#   payload — which reaches a PDF text extraction for the excerpt. The audit
#   status says the scan is finished while its thread is still working, and
#   the listener runs under the manager's own result path.
# Change: the listener enqueues onto ONE coalescing daemon. A second request
#   arriving while a build runs is collapsed into a single follow-up build
#   (the payload is derived from whatever the CURRENT cached scan is, so
#   coalescing cannot skip a state — the last build always reflects the
#   latest result).
# Safe because:
#   _build_and_write already never raises, is already serialised by
#   _BUILD_LOCK, and already reads the manager's cached inventory rather than
#   any argument — so running it a moment later on another thread produces the
#   same payload. The manual POST /api/widget/refresh path is unchanged and
#   still synchronous, because its caller wants the result in its response.
# Preserves: "no scheduler, no auto-scan; the audit manual-only invariant is
#   untouched" — the daemon only ever runs the same build the listener ran,
#   and only in response to a scan result the user asked for.
# Lock/thread contract: the listener no longer holds the scan thread; the
#   daemon takes _BUILD_LOCK exactly as the inline call did.
# Pinned by: test_widget.py::TestSnapshotOffScanThread.
_REFRESH_LOCK = threading.Lock()
_refresh_pending = False
_refresh_thread: threading.Thread | None = None


def _refresh_loop() -> None:
    global _refresh_pending, _refresh_thread
    while True:
        with _REFRESH_LOCK:
            if not _refresh_pending:
                _refresh_thread = None
                return
            _refresh_pending = False
        _build_and_write()


def _request_snapshot_refresh() -> None:
    """Coalesce a snapshot rebuild onto the single refresh daemon."""
    global _refresh_pending, _refresh_thread
    with _REFRESH_LOCK:
        _refresh_pending = True
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return
        _refresh_thread = threading.Thread(
            target=_refresh_loop, name="widget-snapshot", daemon=True)
        _refresh_thread.start()


def join_snapshot_refresh(timeout: float = 15.0) -> None:
    """Wait for a queued snapshot rebuild to finish (tests / shutdown).

    Production never needs this — the daemon is fire-and-forget — but a test
    that patches the build's inputs must be able to join INSIDE its patch
    scope, or the daemon runs against the real singletons (the SSE-drain trap,
    one subsystem over).
    """
    with _REFRESH_LOCK:
        t = _refresh_thread
    if t is not None and t.is_alive():
        t.join(timeout=timeout)


def _on_scan_result() -> None:
    """Result listener: write the snapshot when a scan COMPLETED with results.

    Also fires on ``clear_results()``/``reset_to_idle()`` with an empty
    inventory — deliberately a no-op then (the stale snapshot survives a
    reset; widgets show its ``generated_at`` age rather than losing their
    card). A cancelled scan that still produced a partial inventory writes a
    partial-but-honest snapshot. NEVER starts a scan.

    The build itself runs on the coalescing daemon above, not on the caller's
    (scan) thread.
    """
    _request_snapshot_refresh()


audit_manager.register_result_listener(_on_scan_result)


@widget_bp.route("/api/widget")
def api_widget_get():
    """Serve the current widget payload (memory, then disk). 404 pre-scan."""
    payload = _last_payload or w_store.read_snapshot()
    if payload is None:
        return jsonify({"error": "No widget snapshot. Run a scan first."}), 404
    return jsonify(payload)


@widget_bp.route("/api/widget/refresh", methods=["POST"])
def api_widget_refresh():
    """Rebuild the snapshot from the cached scan. Body ``{redraw?: bool}``.

    409 when no completed scan is cached (the button's "Run a scan first"
    state). ``redraw: true`` forces a fresh random draw instead of the stable
    date-seeded one. Writes only the two regenerable BASE_DIR files — not a
    vault write, hence no ``confirm`` field.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    redraw = coerce_bool(data.get("redraw")) is True
    seed = None
    if redraw:
        import time as _time

        seed = f"redraw-{_time.time_ns()}"
    try:
        payload = _build_and_write(rng_seed=seed)
    except Exception as exc:  # noqa: BLE001 — defensive; helper already guards
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    if payload is None:
        # Distinguish "no scan" (409, run one) from "build crashed" (500):
        # the helper returns None for both, and telling a user whose disk is
        # full to re-run a multi-minute scan is actively wrong advice.
        inv, settings = audit_manager.get_inventory()
        if inv is None or settings is None:
            return jsonify({"error": "No completed scan cached. Run a scan first."}), 409
        return jsonify({"error": "Widget snapshot build failed — see chatekld.log."}), 500
    return jsonify({"ok": True, **payload})
