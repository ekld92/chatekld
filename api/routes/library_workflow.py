"""``/api/library/*`` — the "update my library" maintenance run.

Library-workflow plan (2026-09-21). Lot 3 adds the SESSION: one run's id, the
model it proposes tags with, and that model's spending limit. Session-management endpoints never write library content or global configuration;
analyze/apply delegate to the reviewed workflow. The session record lives
in the reconciliation store beside the import tracker (``accession/imports``).
The orchestrator routes of lot 5 build on it.
"""
from __future__ import annotations

import threading
import time

from flask import Blueprint, jsonify, request

from accession import imports as imports_tracker
from accession import library_run
from api.security import sanitise_error_msg
from api.sse import run_sse_worker
from api.validators import coerce_bool, coerce_string_max_len
from core.constants import SSE_SINGLE_SHOT_FLOOR_S, SSE_STALL_MARGIN_S
from audit.config import AuditConfigError, load_settings
from core.config import load_config
from tagging import session_llm

library_workflow_bp = Blueprint("library_workflow", __name__)


def _settings():
    """The audit settings that scope a session.

    The CACHED SCAN's settings when a scan exists — the same object the
    propose and apply routes read — so a session is always found by the
    routes that use it; the saved configuration only when no scan ran yet.
    Reading the config while a scan's settings existed made a session
    created after an audit-config change invisible to propose (404), its
    spend and cache under another scope (review 2026-09-21).
    """
    from audit.manager import audit_manager
    _inv, scanned = audit_manager.get_inventory()
    if scanned is not None:
        return scanned, None
    try:
        return load_settings(), None
    except AuditConfigError as exc:
        return None, (jsonify({"error": str(exc)}), 400)


def _public(record: dict) -> dict:
    out = dict(record)
    llm = out.get("llm")
    if llm:
        out["llm"] = dict(llm, online=session_llm.is_online(llm.get("provider") or "",
                                                          llm.get("model") or ""))
    return out


#: A store another writer holds past sqlite's timeout answers this, never an
#: uncaught 500: nothing was written, so it is a busy refusal (L12).
_STORE_BUSY = "The library store is busy; try again in a moment."


def _set_llm(settings, sid, raw):
    import sqlite3
    try:
        choice = session_llm.validate(raw, load_config())
    except session_llm.SessionLLMError as exc:
        return None, (jsonify({"error": str(exc)}), 400)
    try:
        imports_tracker.set_session_llm(settings, sid, choice.as_dict())
    except sqlite3.OperationalError:
        return None, (jsonify({"error": _STORE_BUSY}), 503)
    return choice, None


@library_workflow_bp.route("/api/library/sessions", methods=["POST"])
def api_library_session_create():
    """Begin a session. Body ``{label?, llm?: {provider, model, budget_usd?, no_limit?}}``.

    ``no_limit`` (2026-09-22) accepts an online model with no ceiling at all
    (spend still measured); it is refused together with ``budget_usd``.

    The model choice is validated ONCE, BEFORE the session is created, and
    stored in the same transaction as the session itself (audit remediation
    2026-09-23, L12): a refused choice leaves no half-made session, and a
    store locked past sqlite's timeout answers 503 with nothing created —
    it used to create the session, then fail setting its model with an
    uncaught 500. Several sessions a day are fine; each id is new.
    """
    import sqlite3
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    settings, err = _settings()
    if err:
        return err
    llm = data.get("llm")
    choice = None
    if llm is not None:
        try:
            choice = session_llm.validate(llm, load_config())
        except session_llm.SessionLLMError as exc:
            return jsonify({"error": str(exc)}), 400
    try:
        sid = imports_tracker.begin_session(
            settings, label=coerce_string_max_len(data.get("label"), 120) or "",
            llm=choice.as_dict() if choice is not None else None)
    except sqlite3.OperationalError:
        return jsonify({"error": _STORE_BUSY}), 503
    return jsonify({"session": _public(imports_tracker.session(settings, sid) or {})})


@library_workflow_bp.route("/api/library/sessions/<sid>", methods=["GET"])
def api_library_session_get(sid):
    settings, err = _settings()
    if err:
        return err
    record = imports_tracker.session(settings, sid)
    if record is None:
        return jsonify({"error": "Unknown session for this library."}), 404
    return jsonify({"session": _public(record)})


@library_workflow_bp.route("/api/library/sessions/<sid>/llm", methods=["POST"])
def api_library_session_llm(sid):
    """Change the session's model. The spend so far stays with the session."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    settings, err = _settings()
    if err:
        return err
    if imports_tracker.session(settings, sid) is None:
        return jsonify({"error": "Unknown session for this library."}), 404
    _, err = _set_llm(settings, sid, data)
    if err:
        return err
    return jsonify({"session": _public(imports_tracker.session(settings, sid) or {})})


# ---------------------------------------------------------------------------
# Lot 5 — observe, record, analyse, apply (accession/library_run.py)
# ---------------------------------------------------------------------------

_HEADERS = {"X-Requested-With": "ChatEKLD"}

#: One analyse/apply at a time: two runs would race the same tracker rows and
#: interleave renames. Released by the SSE skeleton's guard (`release=`), never
#: by the worker's own `finally` — a client that takes the 200 and disconnects
#: without draining never starts the worker (the 2026-09-02 zotlink leak).
_RUN_GATE = threading.Lock()


def _session_or_404(settings, sid):
    sid = coerce_string_max_len(sid, 64)
    if not sid or imports_tracker.session(settings, sid) is None:
        return None, (jsonify({"error": "Unknown session for this library."}), 404)
    return sid, None


def _observe_report(settings, sid):
    report = imports_tracker.record_pass(settings, sid, imports_tracker.observe(settings))
    report["unfinished_operations"] = imports_tracker.unfinished_operations(settings)
    return report


#: The standalone observation's wall-clock bound. Measured ~2-4 s on the
#: live library (audit 2026-09-23, P3); the bound exists so a wedged Zotero
#: read cannot pin a Waitress worker, not to shape the normal case.
_OBSERVE_DEADLINE_S = 120.0

_BUSY = "A library run is already in progress."


@library_workflow_bp.route("/api/library/observe", methods=["POST"])
def api_library_observe():
    """One tracker pass for a session: ``{session}`` → the lot 2 report.

    Writes only the tracker's own rows (registration, gone/returned). The first
    call for a library records the baseline and proposes nothing.

    Gated and bounded (audit remediation 2026-09-23, L9/P3): it writes the
    same tracker rows an analyse/apply is writing, so it takes ``_RUN_GATE``
    non-blocking (429 while a run is active), and it runs on the bounded
    daemon runner (``api/bounded.py``) so a slow Zotero read never pins the
    request thread. The DAEMON releases the gate when the observation really
    ends — a 504 frees the client, never the gate, or a run could start
    beside a still-writing observation.
    """
    data = request.get_json(silent=True) or {}
    settings, err = _settings()
    if err:
        return err
    sid, err = _session_or_404(settings, data.get("session"))
    if err:
        return err
    if not _RUN_GATE.acquire(blocking=False):
        return jsonify({"error": _BUSY}), 429
    from api.bounded import run_bounded_action

    def fn(_should_cancel):
        try:
            try:
                return 200, _observe_report(settings, sid)
            except imports_tracker.StaleObservation as exc:
                return 409, {"error": str(exc), "reason": "stale"}
            except (OSError, ValueError) as exc:
                # An incomplete Zotero read raises: nothing was recorded.
                return 503, {"error": sanitise_error_msg(exc)}
        finally:
            _RUN_GATE.release()

    res, timed_out, busy = run_bounded_action("library_observe", fn,
                                              deadline_s=_OBSERVE_DEADLINE_S)
    if busy:
        # The daemon never started, so it never released the gate.
        _RUN_GATE.release()
        return jsonify({"error": _BUSY}), 429
    if timed_out:
        return jsonify({"error": "The observation is taking too long; it continues "
                                 "in the background. Try again shortly."}), 504
    status, body = res  # type: ignore[misc]
    return jsonify(body), status


#: ``/api/library/steps`` stores the caller's ``detail`` verbatim on the unit;
#: bounded so a client cannot grow the tracker row without limit.
_STEP_DETAIL_MAX_BYTES = 4096


@library_workflow_bp.route("/api/library/steps", methods=["POST"])
def api_library_record_step():
    """``{session, unit, step, status: done|failed, confirm: true, detail?}``
    — resolve ONE step currently ``uncertain``.

    Audit remediation 2026-09-23 (L9, L2): this used to record ANY outcome
    for any step, ungated, so a client could close work that never happened.
    It is now the manual "mark resolved" verb and nothing else: after the
    user has inspected a write whose outcome was unknown, they say whether it
    landed (``done``) or not (``failed`` — retryable). Confirm-gated, refused
    while a run is active (the run is writing the same rows), and the
    ``detail`` is bounded.
    """
    import json as _json
    data = request.get_json(silent=True) or {}
    settings, err = _settings()
    if err:
        return err
    sid, err = _session_or_404(settings, data.get("session"))
    if err:
        return err
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Resolving a step must be explicitly confirmed (confirm: true)."}), 400
    status = coerce_string_max_len(data.get("status"), 32) or ""
    if status not in ("done", "failed"):
        return jsonify({"error": "status must be 'done' (it landed) or 'failed' (it did not)."}), 400
    detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
    if len(_json.dumps(detail, default=str).encode("utf-8")) > _STEP_DETAIL_MAX_BYTES:
        return jsonify({"error": f"detail is limited to {_STEP_DETAIL_MAX_BYTES} bytes."}), 400
    uid = coerce_string_max_len(data.get("unit"), 200) or ""
    step = coerce_string_max_len(data.get("step"), 32) or ""
    if not _RUN_GATE.acquire(blocking=False):
        return jsonify({"error": _BUSY}), 429
    try:
        current = imports_tracker.unit(settings, uid) or {}
        prior = ((current.get("steps") or {}).get(step) or {})
        if prior.get("status") != "uncertain":
            return jsonify({"error": "Only a step whose outcome is uncertain can be resolved "
                                     "by hand.", "reason": "not_uncertain"}), 409
        try:
            unit = imports_tracker.record_step(
                settings, sid, uid, step, status,
                detail={**detail, "resolved_by_hand": True, "was": prior.get("detail")})
        except imports_tracker.ImportTrackingError as exc:
            return jsonify({"error": str(exc)}), 400
    finally:
        _RUN_GATE.release()
    return jsonify({"unit": unit})


def tag_state_for(vault_root, cfg, rec, tracked) -> tuple[int, dict]:
    """What tags a unit's targets already carry, narrowed to the vocabulary.

    CLI proof run 2026-09-23, finding 2 (decision D-b): the library run closes
    a unit tagged outside any run. Zotero and note tags come from the CACHED
    scan's ``Record``; Finder tags are read from the unit's OWN PDF, because
    the record only holds the union over all its PDFs, and a new PDF added to
    an already-tagged entry must still owe its Finder tags. A tag outside
    ``_tags.md`` does not count — the same gate as ``apply-all``
    (``_canonicalise_tags``) — and an unreadable vocabulary answers 503, so
    nothing is closed.
    """
    from pathlib import Path

    from audit.core import finder_tags
    from tagging.writers import _canonicalise_tags, _read_vocab

    vocab, _rel = _read_vocab(cfg, Path(vault_root))
    if not vocab:
        return 503, {"error": "The tag vocabulary (_tags.md) cannot be read."}

    def vocab_only(tags):
        return _canonicalise_tags(sorted(tags or ()), vocab)[0]

    zot = getattr(rec, "zotero_item", None)
    note = getattr(rec, "obsidian_note", None)
    targets = {
        "zotero": {"present": zot is not None,
                   "tags": vocab_only(getattr(zot, "tags", ()) if zot is not None else ())},
        "note": {"present": note is not None,
                 "tags": vocab_only(getattr(rec, "obs_tags", ()) if note is not None else ())},
        "finder": {"present": False, "tags": []},
    }
    rel = str((tracked or {}).get("rel") or "")
    if (tracked or {}).get("kind") == "attachment" and rel and tracked.get("in_biblio") is not False:
        path = Path(vault_root) / rel
        if path.is_file():
            try:
                names = finder_tags.read_tag_names(path)
            except OSError:
                return 503, {"error": "The PDF's Finder tags cannot be read."}
            targets["finder"] = {"present": True, "tags": vocab_only(names)}
    return 200, {"targets": targets}


def _transport(settings, flask_app, *, echo=None, cancel=None):
    """In-process transports: the app's own routes through the test client.

    *echo* and *cancel* (the outer run's progress line and cancel test) reach
    into the nested mirror stream (P2)."""
    client = flask_app.test_client()
    vault = settings.vault_root.resolve()
    biblio = settings.biblio_articles_dir.resolve()
    try:
        prefix = biblio.relative_to(vault).as_posix().rstrip("/") + "/"
    except ValueError:
        prefix = "\0"  # a biblio folder outside the vault: nothing is renamed

    def js(r):
        d = r.get_json(silent=True)
        return d if isinstance(d, dict) else {}

    def post(path, body):
        r = client.post(path, json=body, headers=_HEADERS)
        return r.status_code, js(r)

    def observe(sid):
        try:
            return 200, _observe_report(settings, sid)
        except imports_tracker.StaleObservation as exc:
            return 409, {"error": str(exc)}
        except (OSError, ValueError) as exc:
            return 503, {"error": sanitise_error_msg(exc)}

    def record_step(sid, unit, step, status, detail, note_sha256=None):
        try:
            imports_tracker.record_step(settings, sid, unit, step, status, detail=detail,
                                        note_sha256=note_sha256)
            return 200, {}
        except imports_tracker.ImportTrackingError as exc:
            return 400, {"error": str(exc)}

    def note_rename(sid, unit, new_rel):
        try:
            imports_tracker.note_rename(settings, sid, unit, new_rel)
            return 200, {}
        except imports_tracker.ImportTrackingError as exc:
            return 400, {"error": str(exc)}

    def get(path):
        r = client.get(path, headers=_HEADERS)
        return r.status_code, js(r)

    def mirror(plan_digest, note_tags=None):
        """The mirror route, STREAMED (audit remediation 2026-09-23, P2).

        It used to be read whole (`get_data()`): the outer run went silent for
        the entire sweep — the outer stall window was its only bound — and a
        Stop on the outer run could not reach it, because the nested stream
        was only closed after it had finished. Now each inner ``{"info"}``
        frame is forwarded to the outer stream as it arrives (so the outer
        stall guard sees progress), and the outer cancel is checked between
        frames: on cancel the inner response is CLOSED, which fires its
        `_SSEOpGuard` and cancels the inner worker at its next note. The
        route stays the one implementation (its gate, its checks); what is
        left is that it holds a second streaming slot while it runs.
        """
        body = {"dry_run": True} if plan_digest is None else {"confirm": True,
                                                               "plan_digest": plan_digest}
        if plan_digest is not None and note_tags:
            body["note_tags"] = {k: list(v) for k, v in note_tags.items()}
        r = client.post("/api/notesync/mirror", json=body, headers=_HEADERS, buffered=False)
        if r.status_code != 200:
            try:
                return r.status_code, js(r)
            finally:
                r.close()
        import json as _json
        terminal: dict = {}
        error = None
        pending = b""
        try:
            for chunk in r.response:
                pending += chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    line = raw.decode("utf-8", errors="replace")
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    try:
                        ev = _json.loads(line[6:])
                    except ValueError:
                        continue
                    if "mirror" in ev:
                        terminal = ev["mirror"]
                    elif "error" in ev and error is None:
                        error = ev["error"]
                    elif "info" in ev and echo is not None:
                        echo(str(ev["info"]))
                if cancel is not None and cancel() and not terminal:
                    # Nothing more is read: whatever the inner worker still
                    # did is unknown here, which the caller records as such.
                    return 0, {"error": "Cancelled while the mirror was running; "
                                        "inspect before retrying."}
        finally:
            r.close()
        if error and not terminal:
            return 500, {"error": error}
        if not terminal:
            return 502, {"error": "The mirror stream ended without a terminal result; inspect before retrying."}
        return 200, terminal

    def scan_state():
        from audit.manager import audit_manager
        inv, _s = audit_manager.get_inventory()
        return {"state": "scanning" if audit_manager.is_scanning() else "idle",
                "finished_at": getattr(inv, "built_at", None) if inv is not None else None}

    # The simulated name state of THIS analysis, carried across the
    # renamer's bounded preview chunks (never across independent runs).
    # `reserved`: names an earlier accepted row takes; `released`: names an
    # earlier accepted row frees. Kept DISJOINT in row order (a name freed
    # and later taken again is reserved; one taken and later freed is
    # released) so the server's `update(reserved)` then
    # `difference_update(released)` is order-independent. Before (audit
    # remediation 2026-09-23, N3) only reservations crossed a chunk, so a
    # rename chain whose freed name was needed in the NEXT chunk (row 5 frees
    # smith_2020.pdf, row 205 wants it) was proposed as smith_2_2020.pdf —
    # a name the single-request preview would never have produced. A
    # case-only rename frees nothing: the server keeps that name taken.
    reserved: dict[str, str] = {}
    released: dict[str, str] = {}

    def rename_preview(rels):
        status, body = post('/api/maintenance/rename-preview', {
            'rels': rels, 'reserved_names': sorted(reserved.values()),
            'released_names': sorted(released.values())})
        if status == 200:
            for r in body.get('rows') or []:
                if not (r.get('can_apply') and r.get('proposal')):
                    continue
                old, new = str(r.get('name') or ''), str(r['proposal'])
                if old and old.lower() != new.lower():
                    released[old.lower()] = old
                    reserved.pop(old.lower(), None)
                reserved[new.lower()] = new
                released.pop(new.lower(), None)
        return status, body

    def rename_op(op_id):
        """The renamer's journal op, and where the file is now (read-only)."""
        from vault_history import journal
        try:
            manifest = journal.load(vault, load_config())
        except journal.JournalUnreadableError as exc:
            return 503, {"error": str(exc)}
        op = journal.find_op(manifest, op_id)
        if op is None or op.get("kind") != "rename_attachment":
            return 404, {"error": "No rename op with that id in the journal."}
        old_rel, new_rel = str(op.get("old_rel") or ""), str(op.get("new_rel") or "")
        return 200, {"state": op.get("state"), "old_rel": old_rel, "new_rel": new_rel,
                     "old_exists": bool(old_rel) and (vault / old_rel).is_file(),
                     "new_exists": bool(new_rel) and (vault / new_rel).is_file()}

    def tag_state(citation_key, unit_id):
        from audit.manager import audit_manager
        inv, _s = audit_manager.get_inventory()
        rec = (inv.records.get(citation_key) if inv is not None else None)
        if rec is None:
            return 404, {"error": "Not in the last scan."}
        return tag_state_for(settings.vault_root, load_config(), rec,
                             imports_tracker.unit(settings, unit_id) or {})

    def leftovers():
        """A FRESH observation, classified, recording nothing (finding 4)."""
        try:
            found, _settling = imports_tracker.classify_leftovers(imports_tracker.observe(settings))
        except (OSError, ValueError) as exc:
            return 503, {"error": sanitise_error_msg(exc)}
        return 200, {"rows": found}

    def trash_attachments(item_keys):
        """The journalled ``/api/zotlink/trash`` route, read to its terminal.

        Never sends ``accept_annotation_loss``: an annotated attachment is
        refused by the route (409) rather than trashed."""
        import json as _json
        r = client.post("/api/zotlink/trash", json={"item_keys": list(item_keys), "confirm": True},
                        headers=_HEADERS, buffered=True)
        if r.status_code != 200:
            return r.status_code, js(r)
        terminal, error = None, None
        for line in r.get_data(as_text=True).splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                ev = _json.loads(line[6:])
            except ValueError:
                continue
            if "zotlink" in ev:
                terminal = ev["zotlink"]
            elif "error" in ev and error is None:
                error = ev["error"]
        if terminal is None:
            return 500 if error else 502, {"error": error or "The trash stream ended without a result."}
        return 200, terminal

    return library_run.Transport(
        leftovers=leftovers, trash_attachments=trash_attachments,
        tag_state=tag_state,
        observe=observe, record_step=record_step, note_rename=note_rename,
        rename_preview=rename_preview,
        rename=lambda rel, name: post("/api/maintenance/rename",
                                      {"rel": rel, "new_name": name, "confirm": True}),
        propose=lambda key, sid, unit: post("/api/tagging/propose", {
            "citation_key": key, "session": sid, "unit": unit}),
        apply_tags=lambda key, tags, unit, targets: post("/api/tagging/apply-all", {
            "citation_key": key, "tags": list(tags), "unit": unit, "confirm": True,
            "targets": {k: bool(v) for k, v in targets.items()}}),
        mirror=mirror, scan_state=scan_state, biblio_prefix=prefix, rename_op=rename_op,
        unit=lambda uid: imports_tracker.unit(settings, uid) if uid else None,
        pdf_links_status=lambda: get("/api/tagging/pdf-links-status"),
        apply_pdf_links=lambda keys: post("/api/tagging/apply-pdf-links", {
            "citation_keys": list(keys), "confirm": True}))


def _run(kind, fn):
    """Shared SSE plumbing for analyse and apply."""
    from flask import current_app
    data = request.get_json(silent=True) or {}
    settings, err = _settings()
    if err:
        return err
    sid, err = _session_or_404(settings, data.get("session"))
    if err:
        return err
    if kind == "apply" and coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Applying must be explicitly confirmed (confirm: true)."}), 400
    if not _RUN_GATE.acquire(blocking=False):
        return jsonify({"error": "A library run is already in progress."}), 429
    flask_app = current_app._get_current_object()

    def worker(put, cancel):
        try:
            echo = lambda m: put({"info": m})  # noqa: E731
            t = _transport(settings, flask_app, echo=echo, cancel=cancel.is_set)
            result = fn(t, sid, data, settings=settings, echo=echo,
                        should_cancel=cancel.is_set)
            # The result is kept on the session: closing the window loses
            # nothing, and the next analyse/apply can read what the last one
            # did (plan §9: a durable, understandable, resumable state). One
            # INSERT per result (P5): the old read-the-list-then-save was two
            # transactions and could lose an entry to another writer.
            imports_tracker.record_session_result(settings, sid, kind, result,
                                                  at=time.time())
            put({f"library_{kind}": result})
        except Exception as exc:  # noqa: BLE001
            put({"error": sanitise_error_msg(exc)})

    return run_sse_worker(worker, consumer_timeout_s=SSE_SINGLE_SHOT_FLOOR_S * 4 + SSE_STALL_MARGIN_S,
                          release=_RUN_GATE.release)


@library_workflow_bp.route("/api/library/analyze", methods=["POST"])
def api_library_analyze():
    """SSE ``{session, propose_tags?}`` → terminal ``{"library_analyze": review}``.

    Reads and proposes; it never writes library content. What it DOES write
    is application state: the import tracker's registration (and any rename
    it settles from the journal), the session's saved analysis, and — only
    when ``propose_tags`` is set — the session's LLM spend and the proposal
    cache (through propose), plus the regenerable PDF-text caches the
    proposal's text source fills (extracted text, ``.notext`` markers).
    """
    return _run("analyze", lambda t, sid, data, settings, **kw: library_run.analyze(
        t, sid, propose_tags=coerce_bool(data.get("propose_tags")) is True, **kw))


@library_workflow_bp.route("/api/library/apply", methods=["POST"])
def api_library_apply():
    """SSE ``{session, confirm, approvals}`` → terminal ``{"library_apply": result}``.

    ``approvals``: ``{mirror: {plan_digest} | null, pdf_links: [citation_key],
    tags: [{unit, citation_key, tags, targets?}], renames: [{unit, rel,
    new_name}], leftovers: [unit]}`` — rows the user reviewed in the analysis, applied in that
    order (``accession.library_run``). Every row goes through its own route,
    which re-validates it; nothing here widens what a route accepts.
    """
    def fn(t, sid, data, settings, **kw):
        approvals = dict(data.get("approvals")) if isinstance(data.get("approvals"), dict) else {}
        mirror = approvals.get("mirror")
        dropped: list = []
        if isinstance(mirror, dict) and mirror.get("plan_digest"):
            # The note units a mirror may settle come from the session's OWN
            # last analysis — server-owned — ALWAYS (audit remediation
            # 2026-09-23, L7). A client-sent `units` list used to replace it,
            # so a client could close notes it was never shown; now it can
            # only NARROW the server's list, and whatever it named outside
            # that list is reported, never settled. Read with the run's own
            # `settings` (the scope the session lives in), never a fresh
            # `_settings()` on the worker thread.
            last = ((imports_tracker.session(settings, sid) or {}).get("last_analysis") or {})
            server_units = list((last.get("result") or {}).get("mirror_units") or [])
            units = server_units
            if isinstance(mirror.get("units"), list):
                asked = [str(u) for u in mirror["units"]]
                allowed = set(server_units)
                units = [u for u in server_units if u in set(asked)]
                dropped = [u for u in asked if u not in allowed]
            approvals["mirror"] = dict(mirror, units=units)
        result = library_run.apply(t, sid, approvals, **kw)
        if dropped:
            result["mirror_units_dropped"] = dropped
        return result
    return _run("apply", fn)
