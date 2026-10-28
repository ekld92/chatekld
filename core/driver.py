"""Unified execution driver for the CHERQAiN CLI and MCP server.

Design (rewritten 2026-07-21 after the audit of the first cut, which called
functions/kwargs/config-keys that do not exist and dispatched to the server
WITHOUT the ``X-Requested-With`` header — so every request 403'd and every path
fell into a broken in-process branch):

**HTTP-first.** The running Flask app already implements every workflow with its
real validation, confirm-gating, operation-specific recovery/audit trail and
single-writer locking. The CLI
and MCP server are therefore thin clients over the app's HTTP API, dispatched
with the ``X-Requested-With: ChatEKLD`` header the origin guard requires. This
is correct *by construction* — there is no second, drift-prone reimplementation
of the domain logic — and it dissolves the cross-process lock problem: because
every mutating request goes through the one server, the server's op-lock
arbitrates all writes. A CLI/MCP process never mutates the vault in its own
address space.

**Writes require the server.** Anything that mutates state (config, index,
vault notes, tags, deck/doc generation, audit scan) returns a clear "start the
server" error when the app is not reachable, instead of silently doing something
unsafe in-process. Only genuinely read-only, cheap operations (reading config,
a status snapshot) have an in-process fallback.

**Consent model preserved over MCP.** Write tools call the same confirmed,
validated endpoints as the GUI. Recovery remains available in History and Restore.

Path inputs (vault sub-folders) are validated with ``core.paths.resolve_under_root``
before they reach any endpoint.
"""

from __future__ import annotations
from core.branding import env_value

import json
import logging
import mimetypes
import os
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Legacy fixed port — kept as the LAST resolution candidate so a
#: ``cherqain server`` on the default port still works if the server-info
#: file write failed. NOT the primary mechanism (the GUI binds a random port).
LEGACY_SERVER_URL = "http://127.0.0.1:5000"

#: The origin guard (api/security.py) 403s any /api request missing this header.
_APP_HEADERS = {"X-Requested-With": "ChatEKLD", "User-Agent": "CHERQAiN-Driver"}

_SERVER_DOWN = (
    "The CHERQAiN server is not running. Start it with `cherqain server` "
    "(or open the CHERQAiN app), then retry. Write operations require the "
    "running server so its locking, validation and operation-specific "
    "recovery/audit controls apply."
)

#: Declarative endpoint registry (2026-07-22 audit C4a). Every HTTP call the
#: driver makes goes through this table BY NAME, so the contract ratchet
#: (tests/test_driver_contracts.py) can assert path AND method against the
#: Flask url_map — including endpoints previously built with f-strings that
#: the regex scan missed (/api/upload). Add a row when adding a driver call.
ENDPOINTS: Dict[str, tuple] = {
    "online_runs": ("/api/online-ocr/runs", "GET"),
    "online_run": ("/api/online-ocr/runs/<run_id>", "GET"),
    "health":            ("/api/health", "GET"),
    "status":            ("/api/status", "GET"),
    "config_get":        ("/api/config", "GET"),
    "config_set":        ("/api/config", "POST"),
    "obsidian_status":   ("/api/obsidian/status", "GET"),
    "upload":            ("/api/upload", "POST"),
    "summarise":         ("/api/summarise", "POST"),
    "obsidian_chat":     ("/api/obsidian/chat", "POST"),
    "obsidian_index":    ("/api/obsidian/index", "POST"),
    "audit_scan":        ("/api/audit/scan", "POST"),
    "audit_summary":     ("/api/audit/summary", "GET"),
    "audit_inventory":   ("/api/audit/inventory", "GET"),
    "audit_report":      ("/api/audit/reports/<name>", "GET"),
    "audit_mapping":     ("/api/audit/mapping", "POST"),
    "audit_pdf_decision": ("/api/audit/pdf-decision", "POST"),
    "zotlink_attach_candidates": ("/api/zotlink/attach-candidates", "GET"),
    "zotlink_attach":    ("/api/zotlink/attach", "POST"),
    "zotlink_repoint_candidates": ("/api/zotlink/repoint-candidates", "GET"),
    "zotlink_repoint":   ("/api/zotlink/repoint", "POST"),
    "tagging_inventory": ("/api/tagging/inventory", "GET"),
    "library_bibliography_decisions": ("/api/audit/library/bibliography/<identifier>/decisions", "POST"),
    "library_bibliography_plan": ("/api/audit/library/bibliography/plan", "POST"),
    "library_bibliography_authenticate": ("/api/audit/library/bibliography/authenticate", "POST"),
    "library_bibliography_apply": ("/api/audit/library/bibliography/<identifier>/apply", "POST"),
    "library_checks": ("/api/audit/library/<view>", "GET"),
    "notesync_campaign_plan": ("/api/notesync/campaign/plan", "POST"),
    "notesync_campaign_review": ("/api/notesync/campaign/<identifier>", "GET"),
    "notesync_campaign_apply": ("/api/notesync/campaign/<identifier>/apply", "POST"),
    "notesync_campaign_inspect": ("/api/notesync/campaign/<identifier>/inspect", "POST"),
    "notesync_campaign_recover": ("/api/notesync/campaign/<identifier>/recover", "POST"),
    "notesync_campaign_settle": ("/api/notesync/campaign/<identifier>/settle", "POST"),
    "notesync_campaign_undo": ("/api/notesync/campaign/<identifier>/undo", "POST"),
    "notesync_sync_status": ("/api/notesync/sync-status", "GET"),
    "tagging_journal_stats": ("/api/tagging/journal-stats", "GET"),
    "tagging_propose":   ("/api/tagging/propose", "POST"),
    "tagging_apply":     ("/api/tagging/apply-all", "POST"),
    "tagging_pdf_links": ("/api/tagging/apply-pdf-links", "POST"),
    "tagging_restore":   ("/api/tagging/restore", "POST"),
    "deck_load_template":   ("/api/deck/load-template", "POST"),
    "deck_generate":        ("/api/deck/generate", "POST"),
    "docgen_load_template": ("/api/docgen/load-template", "POST"),
    "docgen_generate":      ("/api/docgen/generate", "POST"),
    "docgen_generate_md":   ("/api/docgen/generate-md", "POST"),
    "maintenance_steps":    ("/api/maintenance/steps", "GET"),
    "maintenance_run":      ("/api/maintenance/run", "POST"),
    "maintenance_cancel":   ("/api/maintenance/cancel", "POST"),
    # Annual library cleanup T1 (2026-09-17): CLI coverage for routes that
    # were GUI-only, so the cleanup runs with no GUI step (decision D4).
    "audit_status":         ("/api/audit/status", "GET"),
    "audit_dup_decision":   ("/api/audit/dup-decision", "POST"),
    # Annual cleanup session 2: read-only per-PDF evidence for Phase 1.
    "audit_pdf_facts":      ("/api/audit/pdf-facts", "POST"),
    "pdfs_entry":           ("/api/accession/pdfs/entry", "GET"),
    "pdfs_candidates":      ("/api/accession/pdfs/candidates", "GET"),
    "pdfs_preview":         ("/api/accession/pdfs/preview", "POST"),
    "pdfs_apply":           ("/api/accession/pdfs/apply", "POST"),
    "pdfs_restore":         ("/api/accession/pdfs/restore", "POST"),
    "pdfs_inspect":         ("/api/accession/pdfs/inspect", "POST"),
    "pdfs_complete":        ("/api/accession/pdfs/complete", "POST"),
    "pdfs_holding":         ("/api/accession/pdfs/holding", "GET"),
    "accession_rows":       ("/api/accession/rows", "GET"),
    "accession_identify":   ("/api/accession/identify", "POST"),
    "accession_batch":      ("/api/accession/batch", "POST"),
    "maintenance_rename":   ("/api/maintenance/rename", "POST"),
    "maintenance_rename_preview": ("/api/maintenance/rename-preview", "POST"),
    "notesync_legacy_status": ("/api/notesync/status", "GET"),
    "notesync_preview":     ("/api/notesync/preview", "POST"),
    "notesync_create":      ("/api/notesync/create", "POST"),
    "notesync_update_preview": ("/api/notesync/update-preview", "POST"),
    "notesync_update":      ("/api/notesync/update", "POST"),
    "notesync_migrate":     ("/api/notesync/migrate", "POST"),
    "notesync_unmigrate":   ("/api/notesync/unmigrate", "POST"),
    "notesync_bootstrap":   ("/api/notesync/bootstrap", "POST"),
    "notesync_pull":        ("/api/notesync/pull", "POST"),
    "notesync_refresh_all": ("/api/notesync/refresh-all", "POST"),
    "notesync_mirror":      ("/api/notesync/mirror", "POST"),
    "notesync_push":        ("/api/notesync/push", "POST"),
    "notesync_merge_preview": ("/api/notesync/merge-preview", "POST"),
    "notesync_merge":       ("/api/notesync/merge", "POST"),
    "notesync_create_attempts": ("/api/notesync/create-attempts", "GET"),
    "notesync_key_changes": ("/api/notesync/key-changes", "GET"),
    "notesync_rename_note": ("/api/notesync/rename-note", "POST"),
    "notesync_inspect_create_attempt": ("/api/notesync/inspect-create-attempt", "POST"),
    "notesync_create_zotero_note": ("/api/notesync/create-zotero-note", "POST"),
    "zotcreate_preview":    ("/api/zotcreate/preview", "POST"),
    "zotcreate_create":     ("/api/zotcreate/create", "POST"),
    "zotcreate_batch":      ("/api/zotcreate/batch", "POST"),
    # Library-workflow run (lots 3 and 5, 2026-09-21).
    "library_session_create": ("/api/library/sessions", "POST"),
    "library_session_get":    ("/api/library/sessions/<sid>", "GET"),
    "library_session_llm":    ("/api/library/sessions/<sid>/llm", "POST"),
    "library_observe":        ("/api/library/observe", "POST"),
    "library_steps":          ("/api/library/steps", "POST"),
    "library_analyze":        ("/api/library/analyze", "POST"),
    "library_apply":          ("/api/library/apply", "POST"),
    "zotcreate_attempts":   ("/api/zotcreate/attempts", "GET"),
    "zotcreate_settle_never_created": ("/api/zotcreate/settle-never-created", "POST"),
    "zotcreate_reinstate_attempt": ("/api/zotcreate/reinstate-attempt", "POST"),
    # Record merge + chapter-title correction (annual cleanup T8/T9, 2026-09-17).
    "zotrecord_retitle":    ("/api/zotrecord/retitle", "POST"),
    "zotrecord_merge_preview": ("/api/zotrecord/merge-preview", "POST"),
    "zotrecord_merge":      ("/api/zotrecord/merge", "POST"),
    "zotrecord_trash":      ("/api/zotrecord/trash", "POST"),
    "zotlink_status":       ("/api/zotlink/status", "GET"),
    "zotlink_trash":        ("/api/zotlink/trash", "POST"),
    # Attachment title := its filename (annual cleanup T4, 2026-09-18).
    "zotlink_retitle":      ("/api/zotlink/retitle", "POST"),
    "tagging_pdf_links_status": ("/api/tagging/pdf-links-status", "GET"),
}

#: Endpoint keys whose routes exist only in the PRIVATE build (the Maintenance
#: subsystem is stripped from the public mirror, STRIP STEP 4). The contract
#: ratchet (tests/test_driver_contracts.py) skips an optional row whose route
#: is unregistered instead of failing; at runtime the driver maps their HTTP
#: 404 to the "not available in this build" message below.
_OPTIONAL_ENDPOINT_KEYS = frozenset(
    {"maintenance_steps", "maintenance_run", "maintenance_cancel",
     "maintenance_rename", "maintenance_rename_preview"})

_MAINT_UNAVAILABLE = (
    "The maintenance subsystem is not available in this build "
    "(private-only feature; the public build strips it)."
)


# ---------------------------------------------------------------------------
# HTTP plumbing (always sends the app header)
# ---------------------------------------------------------------------------


#: How long discovery waits for ``/api/health``. It was 1.5 s, but a health
#: cache MISS computes the vector-store and local-backend probes, measured at
#: ~2.05 s while an audit scan runs, so `cherqain audit wait` sometimes
#: declared a live server "not running" mid-scan (CLI proof run 2026-09-23).
#: A dead port is refused at once whatever this is; only a live but busy
#: server waits longer.
DISCOVERY_PROBE_TIMEOUT_S = 10.0


def is_server_running(base_url: str = LEGACY_SERVER_URL) -> bool:
    """True iff the local app answers ``/api/health`` (WITH the app header).

    The header is mandatory: without it the origin guard 403s even the health
    probe, which is exactly why the first cut always thought the server was
    down. A 200 means reachable; any error/timeout/non-200 means not.
    """
    try:
        url = f"{base_url.rstrip('/')}/api/health"
        req = urllib.request.Request(url, headers=_APP_HEADERS)
        with urllib.request.urlopen(req, timeout=DISCOVERY_PROBE_TIMEOUT_S) as resp:
            return resp.status == 200
    except Exception as err:  # noqa: BLE001 — any failure = not reachable
        logger.debug("server health check failed: %s", err)
        return False


def resolve_base_url() -> Optional[str]:
    """Locate the running server at CALL time (2026-07-22 audit C0).

    Resolution order — every candidate must pass the ``/api/health`` probe:

    1. ``CHATEKLD_API_URL`` (env) — authoritative when set: probe it and, if
       dead, return ``None`` with NO fallback (an explicit override that
       silently fell elsewhere would defeat both user intent and the test
       sandbox's hermeticity).
    2. ``BASE_DIR/server.json`` — written by the GUI (random port) and the
       headless ``cherqain server``; trusted only when its pid is alive AND
       the probe passes. A stale file is ignored, never deleted (the writer
       owns deletion — see ``core.server_info.clear_server_info``).
    3. The legacy fixed ``:5000`` probe.

    Deliberately NOT cached in a module global: a GUI restart changes the port
    mid-session, and one bounded probe per CLI invocation is negligible
    (``DISCOVERY_PROBE_TIMEOUT_S``; instant on a dead port).
    """
    env_url = env_value("CHATEKLD_API_URL")
    if env_url:
        return env_url if is_server_running(env_url) else None
    try:
        from core.server_info import pid_alive, read_server_info
        info = read_server_info()
    except Exception:  # noqa: BLE001 — discovery must never crash the caller
        info = None
    if info is not None and pid_alive(info["pid"]):
        candidate = f"http://{info['host']}:{info['port']}"
        if is_server_running(candidate):
            return candidate
    if is_server_running(LEGACY_SERVER_URL):
        return LEGACY_SERVER_URL
    return None


#: Appended to network-failure errors on MUTATING calls: a client-side timeout
#: does not undo the server-side write (2026-07-22 audit C5 — a large batch
#: apply can outlive the HTTP timeout; the "failed" write may have landed and
#: its op-ids are in the journal).
_MUTATING_TIMEOUT_NOTE = (
    " NOTE: this was a WRITE request — the operation MAY still have completed "
    "on the server. Check `cherqain status` and the op journal (restore "
    "manifest) before retrying."
)


def _http_json(
    endpoint_key: str,
    data: Optional[Dict[str, Any]] = None,
    *,
    base_url: str,
    timeout: float = 120.0,
    mutating: bool = False,
    query: Optional[Dict[str, str]] = None,
    path_params: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Call a registered JSON endpoint and return the parsed body (or ``{"error"}``).

    *endpoint_key* indexes :data:`ENDPOINTS` (path + method), so every call
    site is ratchet-checkable. ``mutating=True`` rewrites network-failure
    errors with the may-have-landed note (see :data:`_MUTATING_TIMEOUT_NOTE`).
    *query* is url-encoded onto the path — the registry stores the bare path so
    the contract ratchet can still match it against the Flask url_map.

    *path_params* fills a Flask converter segment (``<name>``) in the
    registered path. Kept as substitution rather than letting call sites build
    their own URL, so the registry stays the single place a path is written and
    the contract ratchet keeps matching the rule verbatim. Values are
    percent-encoded with no ``/`` allowed, so a parameter can never widen the
    path it fills.
    """
    path, method = ENDPOINTS[endpoint_key]
    for key, value in (path_params or {}).items():
        path = path.replace(
            f"<{key}>", urllib.parse.quote(str(value), safe=""))
    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    headers = dict(_APP_HEADERS)
    payload = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            if not body:
                return {"error": "Empty JSON response from server."
                        + (_MUTATING_TIMEOUT_NOTE if mutating else "")}
            try:
                result = json.loads(body)
            except json.JSONDecodeError as err:
                return {"error": f"Invalid JSON response from server: {err}"
                        + (_MUTATING_TIMEOUT_NOTE if mutating else "")}
            if not isinstance(result, dict):
                return {"error": "Expected a JSON object from server."
                        + (_MUTATING_TIMEOUT_NOTE if mutating else "")}
            return result
    except urllib.error.HTTPError as err:
        try:
            err_json = json.loads(err.read().decode("utf-8"))
            if isinstance(err_json, dict) and "error" in err_json:
                return err_json
            if isinstance(err_json, dict) and err_json:
                # A refusal body without an `error` key (zotcreate's
                # {ok:false, stage, message}) used to collapse to the bare
                # status line, losing the one sentence that says why
                # (2026-09-17, annual cleanup T1). Additive: the body's own
                # fields ride along and `error` carries its message.
                message = err_json.get("message")
                return {**err_json, "http_status": err.code,
                        "error": message if isinstance(message, str) and message
                        else f"HTTP {err.code}: {err.reason}"}
        except Exception:  # noqa: BLE001 — fall through to the generic message
            pass
        return {"error": f"HTTP {err.code}: {err.reason}"}
    except urllib.error.URLError as err:
        msg = f"Network error connecting to server: {err.reason}"
        return {"error": msg + (_MUTATING_TIMEOUT_NOTE if mutating else "")}
    except Exception as err:  # noqa: BLE001
        msg = f"Request failed: {err}"
        return {"error": msg + (_MUTATING_TIMEOUT_NOTE if mutating else "")}


def _http_sse(
    endpoint_key: str,
    data: Dict[str, Any],
    *,
    base_url: str,
    timeout: float = 900.0,
    on_event: Optional[Any] = None,
) -> Dict[str, Any]:
    """POST *data* and consume the SSE stream.

    Returns ``{"tokens": <joined answer text>, "events": [...], "terminal":
    <last non-token/info frame>, "error": <first error frame if any>}``. The
    SSE contract is one JSON object per ``data:`` line, ending with
    ``data: [DONE]`` (see the root CLAUDE.md → SSE Contract).

    *on_event*, when given, is called with every parsed frame as it arrives —
    the CLI's live-progress hook (``cherqain maintain`` prints info/step
    frames as the old maintain.sh streamed its output). A callback failure is
    swallowed: printing must never abort the consuming of a stream.

    # TIMEOUT CHAIN (2026-09-02): *timeout* is now a WALL CLOCK, not just a
    # per-read socket bound.
    # Before: it was passed straight to ``urlopen``, where it bounds each
    #   individual socket read — every call site (and the parameter's own
    #   name) read it as "this call takes at most N seconds". A server that
    #   keeps emitting a frame every few seconds therefore had no bound at
    #   all: `cherqain maintain` could stream indefinitely inside a
    #   nominally 18 000 s call, and a deck generate inside a 900 s one.
    # Change: the socket timeout still bounds one read (that is what detects
    #   a dead peer), and the consume loop additionally stops once *timeout*
    #   seconds have elapsed in total, returning the frames gathered so far
    #   with an explicit error — the shape every caller already handles.
    # Safe because: a stream that finishes inside its budget is byte-identical
    #   (the deadline is only consulted between frames), and abandoning the
    #   read is exactly what a client disconnect already means to the SSE
    #   skeleton — it sets the worker's cancel event and releases the op lock.
    # Pinned by: tests/test_driver.py::TestSseWallClock.
    """
    path, _method = ENDPOINTS[endpoint_key]
    url = f"{base_url.rstrip('/')}{path}"
    headers = dict(_APP_HEADERS)
    headers["Content-Type"] = "application/json"
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    tokens: list[str] = []
    events: list[dict] = []
    terminal: Optional[dict] = None
    error: Optional[str] = None
    done = False
    deadline = time.monotonic() + float(timeout)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                if time.monotonic() >= deadline:
                    error = (f"The operation exceeded its {timeout:.0f}s budget; "
                             "the server may still be working — check its status.")
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[len("data:"):].strip()
                if body == "[DONE]":
                    done = True
                    break
                try:
                    ev = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if on_event is not None:
                    try:
                        on_event(ev)
                    except Exception:  # noqa: BLE001 — see the docstring
                        pass
                if "token" in ev:
                    tokens.append(str(ev["token"]))
                elif "error" in ev and error is None:
                    error = str(ev["error"])
                else:
                    events.append(ev)
                    # The terminal frame is the last non-token/info payload
                    # (deck/doc/compile/note summaries live here).
                    if any(k in ev for k in
                           ("deck", "doc", "doc_md", "augment", "compile",
                            "note", "retrieval", "maintenance",
                            "zotlink_attach")):
                        terminal = ev
    except urllib.error.HTTPError as err:
        try:
            j = json.loads(err.read().decode("utf-8"))
            # Keep the refusal's machine-readable fields (zotlink trash's
            # `reason`/`annotated_keys`, the rename gate's `reason`): a CLI
            # cannot offer the second confirm without them (2026-09-17).
            return {**{k: v for k, v in j.items() if k != "error"},
                    "error": j.get("error", f"HTTP {err.code}")}
        except Exception:  # noqa: BLE001
            return {"error": f"HTTP {err.code}: {err.reason}"}
    except urllib.error.URLError as err:
        error = f"Network error connecting to server: {err.reason}"
    except Exception as err:  # noqa: BLE001
        error = f"Stream failed: {err}"
    if not done and error is None:
        error = "Stream interrupted before its completion marker; partial output retained."
    out: Dict[str, Any] = {"tokens": "".join(tokens), "events": events}
    if terminal is not None:
        out["terminal"] = terminal
    if error is not None:
        out["error"] = error
        out["status"] = "interrupted"
    return out


def _upload_pdf(pdf_path: str, *, base_url: str) -> Dict[str, Any]:
    """Multipart-POST a local PDF to ``/api/upload``; returns ``{upload_id}`` or error."""
    with open(pdf_path, "rb") as fh:
        file_bytes = fh.read()
    boundary = f"----CHERQAiN{uuid.uuid4().hex}"
    # Multipart header values cannot carry quotes/CR/LF without corrupting the
    # framing (self-inflicted only, but a clean strip beats a broken request).
    filename = os.path.basename(pdf_path).replace('"', "_").replace("\r", "_").replace("\n", "_")
    ctype = mimetypes.guess_type(filename)[0] or "application/pdf"
    parts = [
        f"--{boundary}\r\n".encode(),
        (f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
         f"Content-Type: {ctype}\r\n\r\n").encode(),
        file_bytes,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    headers = dict(_APP_HEADERS)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    upload_path, _m = ENDPOINTS["upload"]
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{upload_path}", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        try:
            return json.loads(err.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"error": f"HTTP {err.code}: {err.reason}"}
    except Exception as err:  # noqa: BLE001
        return {"error": f"Upload failed: {err}"}


# ---------------------------------------------------------------------------
# Status & config
# ---------------------------------------------------------------------------


def get_status() -> Dict[str, Any]:
    """System health + active provider/model + vault-index state.

    ONE canonical shape on both paths (2026-07-22 audit C3 — the server path
    previously returned the raw ``/api/status`` body, whose keys the CLI
    printer never matched, so ``cherqain status`` printed "Status: None"
    exactly when the server WAS running):

    ``{status, server_running, server?, config{provider, model, embed_model,
    vault_path}, vault_index}`` where ``vault_index`` is the rich
    ``/api/obsidian/status`` payload shape on both paths.
    """
    from core.config import load_config, resolve_chat_model

    base = resolve_base_url()
    if base is not None:
        server = _http_json("status", base_url=base)
        cfg_body = _http_json("config_get", base_url=base)
        vault = _http_json("obsidian_status", base_url=base)
        provider = cfg_body.get("provider", "ollama") if isinstance(cfg_body, dict) else "ollama"
        return {
            "status": "ok",
            "server_running": True,
            "server": server,
            "config": {
                "provider": provider,
                # resolve_chat_model is a pure read over the cfg dict — safe
                # to run client-side on the server-returned config.
                "model": resolve_chat_model(cfg_body if isinstance(cfg_body, dict) else {}, provider),
                # The TAGGING model is resolved separately (2026-08-13):
                # `tagging_propose_model` overrides the chat model for every
                # propose call, so reporting only the chat model told a user
                # who had set that key the wrong answer — precisely when they
                # had expressed a preference. "" means "same as chat".
                "tagging_model": _tagging_model(
                    cfg_body if isinstance(cfg_body, dict) else {}, provider),
                "embed_model": (cfg_body or {}).get("embed"),
                "vault_path": (cfg_body or {}).get("obsidian_vault_path"),
            },
            "vault_index": vault,
        }
    # Read-only in-process fallback (cheap, no vault mutation).
    from rag.vault import obsidian_manager

    cfg = load_config()
    provider = cfg.get("provider", "ollama")
    return {
        "status": "ok",
        "in_process": True,
        "server_running": False,
        "config": {
            "provider": provider,
            "model": resolve_chat_model(cfg, provider),
            "tagging_model": _tagging_model(cfg, provider),
            "embed_model": cfg.get("embed"),
            "vault_path": cfg.get("obsidian_vault_path"),
        },
        # get_status_payload() is the RICH dict /api/obsidian/status serves —
        # same shape as the server path (get_status() alone is just a string).
        "vault_index": obsidian_manager.get_status_payload(),
    }


def _tagging_model(cfg: Dict[str, Any], provider: str) -> str:
    """The model a tag proposal will actually use.

    Mirrors `api/routes/tagging.py`'s resolution exactly, INCLUDING the
    route's `coerce_string_max_len(…, 120)` semantics (non-string → unset,
    over-length → truncated), so what this discloses is what the route calls
    — a >120-char override would otherwise be disclosed in full while the
    route used its truncation (2026-08-13 review).
    """
    from core.config import resolve_chat_model

    raw = (cfg or {}).get("tagging_propose_model")
    override = raw.strip()[:120] if isinstance(raw, str) else ""
    return override or resolve_chat_model(cfg or {}, provider)


def get_config() -> Dict[str, Any]:
    """Load the configuration dict (read-only; safe in-process)."""
    from core.config import load_config

    return dict(load_config())


def tagging_model_info() -> Dict[str, Any]:
    """Provider + the model a tag proposal will use — ONE ``/api/config`` read.

    The batch's pre-spend disclosure line. Deliberately NOT :func:`get_status`:
    that helper also GETs ``/api/obsidian/status``, whose payload DRAINS the
    one-shot vault status feed the GUI polls — a disclosure line must never eat
    a window of the GUI's indexing progress (the same rule that gave the audit
    manager ``get_scan_times()``), and status costs three round-trips where one
    suffices. Falls back to the local config file when no server is up
    (read-only; same file the server reads).
    """
    from core.config import load_config, resolve_chat_model

    base = resolve_base_url()
    if base is not None:
        cfg = _http_json("config_get", base_url=base)
        if isinstance(cfg, dict) and "error" in cfg:
            return cfg
    else:
        cfg = load_config()
    cfg = cfg if isinstance(cfg, dict) else {}
    provider = cfg.get("provider", "ollama")
    return {"status": "ok", "provider": provider,
            "model": resolve_chat_model(cfg, provider),
            "tagging_model": _tagging_model(cfg, provider)}


def set_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Update configuration THROUGH the server so its validators run.

    In-process ``save_config`` bypasses ``_validate_llm_config_keys``, the
    ``audit_*`` strip, vault-path safety checks and llm-key routing — the audit
    flagged that as a hole — so this requires the server.
    """
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("config_set", updates, base_url=base, mutating=True)
    if "error" in res:
        return res
    return {"status": "ok", "updated": list(updates.keys())}


# ---------------------------------------------------------------------------
# Single-paper summarization (upload + summarise SSE)
# ---------------------------------------------------------------------------


#: Friendly audience slugs → the exact TARGET_AUDIENCE_OPTIONS keys the route
#: matches (2026-07-22 audit C1: the old hardcoded audience="general" matched
#: no key, so it silently applied NO audience modifier on every call).
AUDIENCE_SLUGS = {
    "researcher": "Researcher / Clinician",
    "student": "Student / Trainee",
    "public": "General public",
}


def summarize_paper(
    pdf_path: str,
    preset: str = "concise",
    lang: str = "fr",
    document_type: str = "auto",
    audience: str = "",
    focus_question: str = "",
) -> Dict[str, Any]:
    """Summarize a local PDF via the server's upload + summarise pipeline.

    ``preset`` IS forwarded (the route reads it — the first cut accepted the
    kwarg and dropped it). ``audience`` accepts the friendly slugs in
    :data:`AUDIENCE_SLUGS` or an exact option key; empty = server default.
    """
    if not os.path.exists(pdf_path):
        return {"error": f"PDF file not found: {pdf_path}"}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}

    up = _upload_pdf(pdf_path, base_url=base)
    if "error" in up or not up.get("upload_id"):
        return {"error": up.get("error", "Upload failed")}

    from core.config import load_config, resolve_chat_model

    cfg = load_config()
    model = resolve_chat_model(cfg, cfg.get("provider", "ollama"))
    body = {
        "upload_id": up["upload_id"],
        "model": model,
        "preset": preset or "concise",
        "focus_question": focus_question or "",
        "audience": AUDIENCE_SLUGS.get((audience or "").strip().lower(),
                                       audience or ""),
        "language": lang or "",
    }
    if document_type and document_type != "auto":
        body["report_type_id"] = document_type
    res = _http_sse("summarise", body, base_url=base)
    if res.get("error"):
        return res
    return {"status": "ok", "pdf_path": pdf_path, "summary": res.get("tokens", "")}


# ---------------------------------------------------------------------------
# Obsidian vault (RAG / agent / index)
# ---------------------------------------------------------------------------


def vault_ask(question: str, agent_mode: bool = False) -> Dict[str, Any]:
    """Ask the vault (single-shot RAG or the ReAct agent) via the chat endpoint."""
    if not question or not question.strip():
        return {"error": "Question is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_sse("obsidian_chat",
                    {"message": question.strip(), "agent_enabled": bool(agent_mode)},
                    base_url=base)
    answer = res.get("tokens", "")
    sources = []
    for event in res.get("events", []):
        if isinstance(event.get("retrieval"), list):
            sources = event["retrieval"]
    if not sources:
        term = res.get("terminal") or {}
        if isinstance(term.get("retrieval"), list):
            sources = term["retrieval"]
    return {
        **({"error": res["error"]} if res.get("error") else {}),
        "status": "interrupted" if res.get("error") else "ok",
        "mode": "agent" if agent_mode else "rag",
        "answer": answer,
        "sources": sources,
    }


def vault_search(query: str, top_k: int = 5) -> Dict[str, Any]:
    """Retrieve relevant vault chunks (one full RAG chat turn).

    There is no standalone HTTP search endpoint; the vault answer path (chat)
    subsumes it and surfaces its retrieval sources. Each call therefore costs
    an LLM GENERATION, not just retrieval. (The former ``mode`` parameter was
    removed 2026-07-22 — it was accepted and silently ignored.)
    """
    res = vault_ask(query, agent_mode=False)
    if "error" in res:
        return res
    sources = res.get("sources", [])[: max(1, min(int(top_k), 50))]
    return {"status": "ok", "query": query, "count": len(sources),
            "results": sources, "answer": res.get("answer", "")}


def online_runs() -> Dict[str, Any]:
    """Read saved run summaries without resuming work or spending provider calls."""
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    return _http_json("online_runs", base_url=base, timeout=30.0)


def vault_index() -> Dict[str, Any]:
    """Build/update the vault index THROUGH the server (single-writer lock).

    Routine updates refuse incompatible identities and interrupted stores.
    A rebuild requires the GUI's separately confirmed rebuild action.
    """
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("obsidian_index", {}, base_url=base, timeout=30.0,
                     mutating=True)
    if "error" in res:
        return res
    return {"status": "ok", "index": res, "note": "Indexing runs in the "
            "background; poll status with `cherqain status`."}


# ---------------------------------------------------------------------------
# Library audit (manual scan, then poll)
# ---------------------------------------------------------------------------


def audit_scan() -> Dict[str, Any]:
    """Start a read-only audit scan (the ONLY entry point that may start one)."""
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    started = _http_json("audit_scan",
                         {"count_annotations": True, "include_duplicates": True},
                         base_url=base)
    if "error" in started:
        return started
    return {"status": "ok", "scan": started,
            "note": "Scan runs in the background; `cherqain audit wait` returns "
                    "when it has finished."}


# ---------------------------------------------------------------------------
# Biblio tagging (propose = read-only; apply = confirm-gated write)
# ---------------------------------------------------------------------------


def tagging_inventory(scope: str = "with_pdf") -> Dict[str, Any]:
    """Read the biblio tagging inventory (read-only; needs a completed scan).

    *scope* is ``with_pdf`` (the historical set) or ``all``, which also returns
    records with no local PDF — writable via note/Zotero, and the set the batch
    subcommand walks with ``--no-pdf-entries``.
    """
    if scope not in ("with_pdf", "all"):
        return {"error": "scope must be 'with_pdf' or 'all'."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("tagging_inventory", base_url=base, query={"scope": scope})
    if "error" in res:
        return res
    # Projection ratchet (2026-08-18): every field a CLI consumer reads MUST be
    # forwarded here AND pinned in tests/test_driver.py's response-projection
    # ratchet — this whitelist silently stripped `scanned_at`, so the batch's
    # snapshot-staleness disclosure was dead end-to-end while its tests passed
    # (they mocked cli.main.tagging_inventory, above this seam).
    return {"status": "ok", "rows": res.get("rows", []),
            "count": res.get("count", 0),
            "pdfless_count": res.get("pdfless_count", 0),
            "scanned_at": res.get("scanned_at", 0), "scope": scope}


#: The audit reports an agent may READ. An allowlist rather than a
#: pass-through: the report name is a URL path segment, and the route's own
#: allowlist should not be the only thing standing between a tool argument and
#: the url_map.
AUDIT_REPORT_NAMES = (
    "note_tag_drift", "finder_tag_drift", "tag_harmony", "tag_hygiene",
    "read_unzoterod", "unread_unzoterod", "zotero_unread", "zotero_no_pdf",
    "duplicates", "near_duplicates", "ambiguous_pdfs", "skipped_pdfs",
    "orphan_notes", "orphan_zotero_items", "zotero_attachments",
    "multi_pdf", "not_in_zotero", "pdf_review", "dead_curation",
)


def audit_summary() -> Dict[str, Any]:
    """The cached scan's headline counters (read-only).

    Deliberately the `summary` route, not `inventory`: the latter carries a
    thousands-row `records` array, and an agent asking "where does the library
    stand" should not move megabytes to find out.
    """
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("audit_summary", base_url=base)
    if "error" in res:
        return res
    return {"status": "ok", "summary": res.get("summary", {})}


def notesync_status() -> Dict[str, Any]:
    """The two-way Zotero ⇄ Obsidian note-sync state (read-only).

    Reads Zotero FRESH server-side (needs a completed audit scan for the
    citekey ↔ item join). Rows carry one state each — in_sync · pull · push ·
    conflict · unbaselined · missing_obsidian · missing_zotero · unparsed ·
    multiple_notes. The write verbs are CLI-only (cleanup T1, 2026-09-17, each
    behind an explicit ``--confirm``); none is exposed over MCP, where a tool
    call would satisfy the gate blind (the elicitation gap in Known Issues).
    """
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("notesync_sync_status", base_url=base)
    if "error" in res:
        return res
    # Projection ratchet: every field a consumer reads is forwarded here AND
    # pinned in tests/test_driver.py (the tagging_inventory `scanned_at` lesson).
    return {"status": "ok", "converter_id": res.get("converter_id", ""),
            "total": res.get("total", 0), "counts": res.get("counts", {}),
            "rows": res.get("rows", []),
            "authoritative": bool(res.get("authoritative"))}


def audit_inventory() -> Dict[str, Any]:
    """The cached scan's per-citation-key records (read-only, LARGE).

    Whole-library payload — ~6,300 records on the maintainer's vault. Prefer
    :func:`audit_summary` for counts and :func:`audit_report` for a question.
    """
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("audit_inventory", base_url=base, timeout=180)
    if "error" in res:
        return res
    return {"status": "ok", "summary": res.get("summary", {}),
            "records": res.get("records", [])}


def audit_report(name: str) -> Dict[str, Any]:
    """One reconciliation report from the cached scan (read-only).

    404 until a scan completes, like every report tab.
    """
    if name not in AUDIT_REPORT_NAMES:
        return {"error": "Unknown report. Known: "
                         + ", ".join(AUDIT_REPORT_NAMES)}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("audit_report", base_url=base, timeout=120,
                     path_params={"name": name})
    if "error" in res:
        return res
    # Reports differ in shape by design, so the projection is the envelope
    # plus the report's own body — but `name` is echoed so a consumer can
    # never mistake one report's rows for another's.
    out = {"status": "ok", "name": res.get("name", name)}
    out.update({k: v for k, v in res.items() if k != "name"})
    return out


#: The mapping route's verbs a headless caller may use. ``link`` is the one
#: that asserts a relationship, so it takes exactly one PDF; the three bucket
#: verbs take a list (one atomic write).
AUDIT_MAPPING_ACTIONS: tuple = ("link", "no_match", "pending", "clear",
                                "detach", "detach_all")


def audit_mapping(action: str, pdfs: List[str],
                  citation_key: Optional[str] = None, *,
                  trash_zotero_links: bool = False) -> Dict[str, Any]:
    """Curate the PDF↔bib mapping through the confirm-gated route (2026-09-06).

    ``action`` ∈ :data:`AUDIT_MAPPING_ACTIONS`; ``pdfs`` are vault-relative
    paths; ``citation_key`` is required by ``link`` and ``detach``, and
    refused by every other action — ``detach_all`` included, since it means
    "no key at all" and taking one would read as the opposite. The server
    re-validates every path (containment, regular PDF, hashable) and refuses
    the WHOLE list on any invalid entry, so a bulk re-sort is atomic. ``confirm:
    true`` is supplied because the invocation is the decision; the safety net
    is ``clear`` (every verb here is undoable by it).
    """
    if action not in AUDIT_MAPPING_ACTIONS:
        return {"error": f"Unknown action: {action}. Valid actions: "
                         + ", ".join(AUDIT_MAPPING_ACTIONS)}
    rels = [str(p).strip() for p in (pdfs or []) if str(p).strip()]
    if not rels:
        return {"error": "At least one PDF path is required."}
    key = (citation_key or "").strip()
    if action in ("link", "detach"):
        if len(rels) != 1:
            return {"error": f"{action} takes exactly one PDF."}
        if not key:
            return {"error": f"{action} needs a citation key."}
    elif action == "detach_all":
        if len(rels) != 1:
            return {"error": "detach_all takes exactly one PDF."}
        if key:
            return {"error": "detach_all does not take a citation key."}
    elif key:
        return {"error": f"{action} does not take a citation key."}
    if trash_zotero_links and action not in ("detach", "detach_all"):
        return {"error": "trash_zotero_links applies to detach and detach_all only."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    body: Dict[str, Any] = {"confirm": True}
    if action == "link":
        body.update({"pdf": rels[0], "citation_key": key})
    elif action == "detach":
        body.update({"pdf": rels[0], "detach_from": key})
    elif action == "detach_all":
        body.update({"pdf": rels[0], "detach_all": True})
    else:
        body.update({"pdfs": rels, action: True})
    if trash_zotero_links:
        body["trash_zotero_links"] = True
    res = _http_json("audit_mapping", body, base_url=base, timeout=300.0,
                     mutating=True)
    if "error" in res:
        return res
    out = {"status": "ok", "action": res.get("action", action)}
    # `zotero_error` rides a 200 when the detach landed but the Zotero trash
    # did not: dropping it would report a half-done step as done.
    for k in ("pdf", "pdfs", "citation_key", "changed", "trashed", "batch_id",
              "zotero_error", "zotero_note"):
        if k in res:
            out[k] = res[k]
    return out


def audit_pdf_decision(verb: str, citation_keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """Record / undo "this bib entry needs no local PDF" (2026-09-06 CLI row).

    ``verb`` ∈ ``dismiss`` | ``clear`` (both take keys) | ``clear_all``. Writes
    only the ``pdf_wanted.json`` decision sidecar; applies on the next report
    LOAD, not the next scan.
    """
    if verb not in ("dismiss", "clear", "clear_all"):
        return {"error": "verb must be dismiss, clear or clear_all."}
    keys = [str(k).strip() for k in (citation_keys or []) if str(k).strip()]
    if verb == "clear_all":
        if keys:
            return {"error": "clear_all takes no keys."}
        body: Dict[str, Any] = {"clear_all": True}
    else:
        if not keys:
            return {"error": "At least one citation key is required."}
        body = {"citation_keys": keys, verb: True}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("audit_pdf_decision", body, base_url=base, timeout=120.0,
                     mutating=True)
    if "error" in res:
        return res
    return {"status": "ok", "verb": verb, "changed": res.get("changed")}


def zotlink_attach_candidates() -> Dict[str, Any]:
    """Records with a Zotero item and ≥1 mapped PDF, and how many PDFs Zotero
    does not yet hold as a linked file (read-only; 404 until a scan)."""
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("zotlink_attach_candidates", base_url=base, timeout=120)
    if "error" in res:
        return res
    out = {"status": "ok"}
    for k in ("rows", "count", "to_link", "group_count", "batch_max",
              "path_form", "can_write", "transport"):
        if k in res:
            out[k] = res[k]
    return out


def zotlink_repoint_candidates(item_key: str, query: str = "") -> Dict[str, Any]:
    """Biblio PDFs that could be the new home of a broken linked attachment.

    Read-only. ``item_key`` must name an attachment the last scan saw; the
    ranking is against the filename Zotero remembers, which after a rename is
    usually the best clue to the new one.
    """
    key = (item_key or "").strip()
    if not key:
        return {"error": "item_key is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    params = {"item_key": key}
    if (query or "").strip():
        params["q"] = query.strip()
    res = _http_json("zotlink_repoint_candidates", base_url=base, timeout=120,
                     query=params)
    if "error" in res:
        return res
    out = {"status": "ok"}
    for k in ("item_key", "stored_path", "stored_exists", "total", "candidates"):
        if k in res:
            out[k] = res[k]
    return out


def zotlink_repoint(item_key: str, rel: str) -> Dict[str, Any]:
    """Point one linked attachment at *rel* (relative to the bibliography dir).

    One GET and one PATCH, journalled as ``zotero_attachment_repath`` and
    reversible from Tagging → Restore — the same op the rename path writes,
    so there is one definition of "move this pointer".
    """
    key = (item_key or "").strip()
    target = (rel or "").strip()
    if not key or not target:
        return {"error": "item_key and rel are required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("zotlink_repoint",
                     {"item_key": key, "rel": target, "confirm": True},
                     base_url=base, timeout=300.0, mutating=True)
    if "error" in res:
        return res
    out = {"status": res.get("status", "ok")}
    for k in ("ok", "item_key", "path", "op_id", "message", "note"):
        if k in res:
            out[k] = res[k]
    return out


def zotlink_attach(citation_keys: List[str], *, dry_run: bool = False,
                   acknowledge_plugins: bool = False,
                   on_event: Optional[Any] = None) -> Dict[str, Any]:
    """Create linked-file attachments in Zotero for records' mapped PDFs
    (confirm-gated on the server; SSE consumed to completion).

    ``dry_run`` reports the plan and writes nothing. The route skips group-
    library records and already-linked PDFs with a reason; the undo is the
    Zotero Attachments tab's trash verb (journalled per attachment).
    """
    keys = [str(k).strip() for k in (citation_keys or []) if str(k).strip()]
    if not keys:
        return {"error": "At least one citation key is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    body: Dict[str, Any] = {"citation_keys": keys, "confirm": True}
    if dry_run:
        body["dry_run"] = True
    if acknowledge_plugins:
        # The server refuses while a file-renaming Zotero plugin is installed
        # unless the caller says it has disabled its auto-rename (2026-09-07).
        body["acknowledge_plugins"] = True
    # ~1–2 s per PDF (POST + version read); 7 500 records need a whole day
    # at worst, so the wall clock is 24 h. A dead peer is still detected per
    # read; the total only bounds a stream that keeps emitting.
    res = _http_sse("zotlink_attach", body, base_url=base, timeout=86400.0,
                    on_event=on_event)
    if res.get("error"):
        return {"error": res["error"], "events": res.get("events", [])}
    term = res.get("terminal") or {}
    payload = term.get("zotlink_attach") if isinstance(term, dict) else None
    if not isinstance(payload, dict):
        return {"error": "The attach run ended without a terminal frame."}
    out = {"status": "ok"}
    for k in ("dry_run", "batch_id", "attached", "results", "cancelled",
              "failures", "journal_warning", "path_form", "note"):
        if k in payload:
            out[k] = payload[k]
    return out


def tagging_journal_stats() -> Dict[str, Any]:
    """Size and composition of the tagging restore journal (read-only).

    Cheap at any history length — the route aggregates in SQL and parses no
    operation payloads. ``protected`` is the never-evicted population (applied
    Finder/Zotero ops, whose prior tags live inline), i.e. the part that grows
    for the life of the library; it is what `cherqain status` reports on.
    """
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("tagging_journal_stats", base_url=base)
    if "error" in res:
        return res
    # Projection whitelist — see tagging_inventory. Anything a CLI consumer
    # starts reading must be added HERE and to the response-projection ratchet
    # in the same commit, or it is dead end-to-end with green tests.
    return {"status": "ok",
            "ops": res.get("ops", 0),
            "bytes": res.get("bytes", 0),
            "protected": res.get("protected", 0),
            "evictable": res.get("evictable", 0),
            "oldest_ts": res.get("oldest_ts"),
            "newest_ts": res.get("newest_ts"),
            "retention": res.get("retention", {})}


def tagging_propose(citation_key: str) -> Dict[str, Any]:
    """Propose vocabulary-constrained tags for a paper (writes nothing)."""
    if not citation_key or not citation_key.strip():
        return {"error": "Invalid citation key."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("tagging_propose",
                     {"citation_key": citation_key.strip()}, base_url=base)
    if "error" in res:
        return res
    return {"status": "ok", "citation_key": citation_key.strip(), "proposal": res}


def tagging_pdf_links(citation_keys: List[str]) -> Dict[str, Any]:
    """Write each record's mapped PDFs into its note as a ``pdf:`` wikilink list.

    KEYS ONLY: the filenames come from the cached scan's PDF↔bib join, so this
    cannot point a note at an arbitrary file. Journalled per note and
    reversible from Tagging → Restore; the server refuses a batch larger than
    that journal retains.
    """
    keys = [str(k).strip() for k in (citation_keys or []) if str(k).strip()]
    if not keys:
        return {"error": "At least one citation key is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("tagging_pdf_links",
                     {"citation_keys": keys, "confirm": True},
                     base_url=base, timeout=900.0, mutating=True)
    if "error" in res:
        return res
    out = {"status": "ok"}
    # `aborted` (lock_lost) says why some keys have no result; stripping it
    # made a stopped run read like keys the server silently ignored.
    for k in ("written", "results", "aborted"):
        if k in res:
            out[k] = res[k]
    return out


def tagging_apply(
    citation_key: str, tags: List[str], targets: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Apply tags to note/Finder/Zotero via the confirm-gated fan-out endpoint.

    Tags are re-validated against ``_tags.md`` server-side, every write is
    journalled and reversible, and absent/off-platform targets are skipped with
    a reason — the exact contract the GUI's Accept uses. ``confirm: true`` is
    supplied because the tool invocation IS the decision; the safety net is the
    server-side vocabulary gate + reversibility, not a second prompt.
    """
    if not citation_key or not citation_key.strip():
        return {"error": "Invalid citation key."}
    from core.constants import TAGGING_TARGETS

    if not isinstance(tags, list) or not tags:
        return {"error": "At least one tag is required."}
    if targets is not None:
        unknown = [str(t) for t in targets if str(t) not in TAGGING_TARGETS]
        if unknown:
            return {"error": f"Unknown target(s): {', '.join(unknown)}. "
                             f"Valid targets: {', '.join(TAGGING_TARGETS)}."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    body: Dict[str, Any] = {
        "citation_key": citation_key.strip(),
        "tags": [str(t) for t in tags],
        "confirm": True,
    }
    # `is not None`, NOT truthiness: the server reads an OMITTED target key as ON
    # (api/routes/tagging.py), so sending nothing for an EMPTY selection turned
    # "write only Finder" into "write all three". That is reachable — the batch
    # command drops the finder target from a PDF-less row, and `--targets finder`
    # then leaves the list empty — and it inverts the user's restriction into
    # thousands of unrequested writes. An explicit empty selection must serialise
    # as all-false (the server writes nothing) and only an UNSET `targets=None`
    # may fall through to the server's all-on default.
    if targets is not None:
        want = set(targets)
        body["targets"] = {t: (t in want) for t in TAGGING_TARGETS}
    # 300 s: the fan-out can wait on the op-lock + a slow Zotero web call;
    # mutating=True so a client-side timeout says "may have landed".
    res = _http_json("tagging_apply", body, base_url=base, timeout=300.0,
                     mutating=True)
    if "error" in res:
        return res
    return {"status": "ok", "citation_key": citation_key.strip(), "results": res.get("results", res)}


def tagging_restore(op_id: str) -> Dict[str, Any]:
    """Reverse one journalled tagging op by id (confirm-gated, via the server)."""
    if not op_id or not op_id.strip():
        return {"error": "op_id is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("tagging_restore",
                     {"op_id": op_id.strip(), "confirm": True},
                     base_url=base, timeout=300.0, mutating=True)
    return res if "error" in res else {"status": "ok", "restore": res}


# ---------------------------------------------------------------------------
# Shared generation path validation
# ---------------------------------------------------------------------------


def _safe_subfolder(subfolder: str) -> Optional[str]:
    """Validate a vault-relative sub-folder against the configured vault root.

    Returns the normalised rel, or ``None`` if it is empty/traversing/escaping.
    Read-only + defence in depth: the server re-validates too, but rejecting
    here gives a clean CLI error and never sends a traversal string on the wire.
    """
    if not subfolder:
        return ""
    from core.config import load_config
    from core.paths import resolve_under_root

    vault = load_config().get("obsidian_vault_path")
    if not vault:
        return None
    return resolve_under_root(subfolder, vault, must_be_dir=True)


# ---------------------------------------------------------------------------
# Deck & document generation (SSE)
# ---------------------------------------------------------------------------


def _load_template(kind: str, template_path: str, base: str) -> Dict[str, Any]:
    """Server-side template read + validation (mirrors what deck.js does).

    The route wants ``template_tex`` CONTENT, not a path — and reading the file
    through ``/api/*/load-template`` keeps the path validation
    (``_resolve_template_path`` deny-lists, size cap) server-side: exactly one
    implementation, no drift (2026-07-22 audit C1 — the first cut sent neither
    content nor path, so every generate call 400'd; this surface had NEVER
    produced a deck).
    """
    key = "deck_load_template" if kind == "deck" else "docgen_load_template"
    return _http_json(key, {"path": template_path}, base_url=base)


def generate_deck(
    topic: str,
    template_path: str,
    out_dir: str = "",
    instructions: str = "",
    deck_name: str = "",
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Generate a vault-grounded Beamer deck (emit-only; scaffolds a folder).

    ``template_path`` is REQUIRED — the route refuses without a template, and
    the deck inherits its preamble/macros/bib. ``out_dir`` defaults to the
    template's suite root (same fallback the route itself applies).
    """
    if not topic or not topic.strip():
        return {"error": "Topic is required."}
    if not template_path or not template_path.strip():
        return {"error": "A Beamer template path is required (--template)."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    tpl = _load_template("deck", template_path.strip(), base)
    if "error" in tpl:
        return {"error": tpl["error"]}
    body = {
        "topic": topic.strip(),
        "template_tex": tpl.get("tex", ""),
        "template_path": tpl.get("path", template_path.strip()),
        "out_dir": out_dir or tpl.get("suite_root", ""),
        "deck_name": deck_name or topic.strip(),
        "deck_title": topic.strip(),
        "instructions": instructions or "",
        "overwrite": bool(overwrite),
    }
    res = _http_sse("deck_generate", body, base_url=base)
    if res.get("error"):
        return res
    return {"status": "ok", "topic": topic.strip(),
            "deck": (res.get("terminal") or {}).get("deck")}


def generate_doc(
    topic: str,
    instructions: str = "",
    mode: str = "markdown",
    template_path: str = "",
    out_dir: str = "",
) -> Dict[str, Any]:
    """Generate a vault-grounded document.

    ``markdown`` writes a NEW note into the vault (confirm-gated, reversible via
    Settings → History and Restore); ``latex`` scaffolds an emit-only article folder
    and REQUIRES ``template_path`` (same contract as the deck).
    """
    if not topic or not topic.strip():
        return {"error": "Topic is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    if mode == "markdown":
        res = _http_sse("docgen_generate_md",
                        {"topic": topic.strip(), "instructions": instructions or "",
                         "confirm": True},
                        base_url=base)
        if res.get("error"):
            return res
        return {"status": "ok", "mode": "markdown",
                "document": (res.get("terminal") or {}).get("doc_md")}
    if not template_path or not template_path.strip():
        return {"error": "An article template path is required for latex mode "
                         "(--template)."}
    tpl = _load_template("docgen", template_path.strip(), base)
    if "error" in tpl:
        return {"error": tpl["error"]}
    body = {
        "topic": topic.strip(),
        "template_tex": tpl.get("tex", ""),
        "template_path": tpl.get("path", template_path.strip()),
        "out_dir": out_dir or tpl.get("suite_root", ""),
        "instructions": instructions or "",
    }
    res = _http_sse("docgen_generate", body, base_url=base)
    if res.get("error"):
        return res
    return {"status": "ok", "mode": "latex",
            "document": (res.get("terminal") or {}).get("doc")}


# ---------------------------------------------------------------------------
# Vault maintenance (private-only build feature — optional endpoints)
# ---------------------------------------------------------------------------


def _maint_unavailable_mapped(res: Dict[str, Any]) -> Dict[str, Any]:
    """Map a routing 404 on an optional maintenance endpoint to the friendly
    "not available in this build" error (public mirror: blueprint stripped)."""
    err = res.get("error")
    if isinstance(err, str) and err.startswith("HTTP 404"):
        return {"error": _MAINT_UNAVAILABLE}
    return res


def maintenance_steps() -> Dict[str, Any]:
    """List the maintenance step registry (read-only; the capability probe)."""
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _maint_unavailable_mapped(
        _http_json("maintenance_steps", base_url=base))
    if "error" in res:
        return res
    # Projection ratchet (see maintenance/tests/test_driver_maintenance.py):
    # every field a CLI consumer reads must be forwarded here AND pinned there
    # in the same commit — the tagging_inventory `scanned_at` lesson.
    return {"status": "ok", "steps": res.get("steps", [])}


def maintenance_run(
    steps: Optional[List[str]] = None,
    on_event: Optional[Any] = None,
    *,
    options: Optional[Dict[str, Dict[str, Any]]] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Run maintenance steps (server-side; SSE consumed to completion).

    *steps* None ⇒ the server's default-on set. Write/egress confirmation is
    never inferred from calling this function: a UI/CLI/MCP caller must pass
    ``confirm=True`` after its own explicit ceremony. *options* is forwarded
    intact for route validation against the registry schema. *on_event*
    receives each frame live (the CLI's progress printer).
    """
    if steps is not None:
        if not isinstance(steps, list) or not steps or not all(
                isinstance(s, str) and s.strip() for s in steps):
            return {"error": "steps must be a non-empty list of step ids "
                             "(or None for the default set)."}
        steps = [s.strip() for s in steps]
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    if options is not None and not isinstance(options, dict):
        return {"error": "options must be an object or None."}
    body: Dict[str, Any] = {}
    if steps is not None:
        body["steps"] = steps
    if options:
        body["options"] = options
    if confirm:
        body["confirm"] = True
    # Five hours: the server's LaTeX sweep has an independent four-hour total
    # budget, while README sync is capped at 20 minutes. The client transport
    # must outlive the authoritative server budget so it cannot manufacture a
    # disconnect that looks like completion. The request transport is
    # potentially effectful; route-side classification remains authoritative
    # even when a caller accidentally omits its own confirmation ceremony.
    res = _http_sse("maintenance_run", body, base_url=base, timeout=18000.0,
                    on_event=on_event)
    res = _maint_unavailable_mapped(res)
    if res.get("error"):
        return res
    terminal = (res.get("terminal") or {}).get("maintenance") or {}
    step_frames = [ev["step"] for ev in res.get("events", [])
                   if isinstance(ev, dict) and "step" in ev]
    # Projection ratchet — see maintenance_steps() above.
    return {"status": "ok",
            "ok": bool(terminal.get("ok")),
            "results": terminal.get("results", {}),
            "ran": terminal.get("ran", []),
            "cancelled": bool(terminal.get("cancelled")),
            "steps": step_frames}


def maintenance_cancel() -> Dict[str, Any]:
    """Request cancellation of the active maintenance run."""
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _maint_unavailable_mapped(
        _http_json("maintenance_cancel", {}, base_url=base))
    if "error" in res:
        return res
    return {"status": "ok", "cancelling": bool(res.get("cancelling"))}


def notesync_campaign(action, *, identifier=None, data=None):
    """Thin client: the server owns proposals, approval checks and journals.

    Note: ``apply``, ``recover``, ``settle``, and ``undo`` remain disabled
    (HTTP 501) in production; only ``plan``, ``review``, and ``inspect`` are
    active by default.
    """
    if action not in ('plan', 'review', 'apply', 'inspect', 'recover', 'settle', 'undo'):
        raise ValueError('Unknown campaign action.')
    base = resolve_base_url()
    if base is None:
        return {'error': _SERVER_DOWN}
    return _http_json('notesync_campaign_' + action, data, base_url=base,
                      path_params={'identifier': identifier} if identifier else None,
                      mutating=action in ('plan', 'apply', 'recover', 'settle', 'undo'))


def library_campaign(action, data=None, identifier=None):
    """Thin client for Library deterministic checks and bibliography campaigns.

    Note: ``apply`` (``bibliography-apply``) remains disabled (HTTP 501) in
    production pending validation.
    """
    base = resolve_base_url()
    if base is None:
        return {'error': _SERVER_DOWN}
    if action in ('checks', 'links', 'storage'):
        return _http_json('library_checks', base_url=base, path_params={'view': action})
    if action == 'decisions':
        return _http_json('library_bibliography_decisions', data, base_url=base,
                          path_params={'identifier': identifier}, mutating=True)
    if action == 'bibliography':
        return _http_json('library_bibliography_plan', data, base_url=base, mutating=True)
    if action == 'authenticate':
        return _http_json('library_bibliography_authenticate', data, base_url=base)
    if action == 'apply':
        return _http_json('library_bibliography_apply', data, base_url=base,
                          path_params={'identifier': identifier}, mutating=True)
    raise ValueError('Unknown Library campaign.')


# ---------------------------------------------------------------------------
# Annual library cleanup T1 (2026-09-17): CLI coverage for GUI-only routes
# ---------------------------------------------------------------------------
#
# Thin pass-throughs by design. These routes' payloads are consumed whole by
# the cleanup harness and printed as JSON by the CLI, so there is no whitelist
# to drift from its consumer (the response-projection ratchet's failure mode
# is a whitelist that STRIPS a field; a pass-through cannot). Every write goes
# to the same confirm-gated, journalled route the GUI uses; `confirm: true` is
# sent only by functions whose CLI command itself demands an explicit
# `--confirm` from the person running it.


def _ok(res: Dict[str, Any]) -> Dict[str, Any]:
    # Truthiness, not presence: /api/audit/status always carries
    # `"error": null`, and a presence test would report every poll as failed.
    if res.get("error"):
        return res
    return {"status": "ok", **res}


def _read(key: str, *, query: Optional[Dict[str, str]] = None,
          timeout: float = 120.0) -> Dict[str, Any]:
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    return _ok(_http_json(key, base_url=base, query=query, timeout=timeout))


def _write(key: str, body: Dict[str, Any], *, timeout: float = 300.0) -> Dict[str, Any]:
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    return _ok(_http_json(key, body, base_url=base, timeout=timeout, mutating=True))


def _stream(key: str, body: Dict[str, Any], terminal_key: str, *,
            timeout: float, on_event: Optional[Any] = None) -> Dict[str, Any]:
    """POST an SSE route and return its terminal ``terminal_key`` payload,
    plus every ``row`` frame (accession streams one per PDF)."""
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_sse(key, body, base_url=base, timeout=timeout, on_event=on_event)
    events = res.get("events", [])
    rows = [ev["row"] for ev in events if isinstance(ev, dict) and "row" in ev]
    if res.get("error"):
        return {**{k: v for k, v in res.items() if k not in ("tokens", "events")},
                "rows": rows}
    frames = [ev[terminal_key] for ev in events
              if isinstance(ev, dict) and terminal_key in ev]
    if not frames:
        return {"error": f"The stream ended without a terminal '{terminal_key}' frame.",
                "rows": rows}
    return {"status": "ok", terminal_key: frames[-1], "rows": rows}


def _clean_list(values: Optional[List[str]]) -> List[str]:
    return list(dict.fromkeys(str(v).strip() for v in (values or []) if str(v).strip()))


def audit_status() -> Dict[str, Any]:
    """The audit manager's lifecycle payload (poll after `audit_scan`).

    Its ``messages`` are a DRAINED one-shot buffer: a CLI poll consumes lines
    the GUI's audit panel would otherwise have shown. Harmless while the
    cleanup runs headless (decision D4); ``state`` is the field to read.
    """
    return _read("audit_status")


def audit_dup_decision(verb: str, pair_ids: List[str]) -> Dict[str, Any]:
    """Dismiss / undo "not a duplicate" for near-duplicate groups, in ONE
    atomic write (the route's list form). Writes only ``dup_decisions.json``."""
    if verb not in ("dismiss", "clear"):
        return {"error": "verb must be dismiss or clear."}
    ids = _clean_list(pair_ids)
    if not ids:
        return {"error": "At least one pair_id is required."}
    return _write("audit_dup_decision", {"pair_ids": ids, verb: True}, timeout=120.0)


def audit_pdf_facts(pdfs: List[str]) -> Dict[str, Any]:
    """Read-only evidence per biblio PDF (≤50 vault-relative paths): sha256,
    pages, provenance class, embedded/printed DOI, page-1 excerpt. Writes
    nothing; a row that cannot be read carries its own ``error``."""
    rels = _clean_list(pdfs)
    if not rels:
        return {"error": "At least one PDF path is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    return _ok(_http_json("audit_pdf_facts", {"pdfs": rels}, base_url=base, timeout=600.0))


def pdfs_entry(citation_key: str) -> Dict[str, Any]:
    """Manage PDFs: one entry's files, attachments and retained operations."""
    key = (citation_key or "").strip()
    if not key:
        return {"error": "A citation key is required."}
    return _read("pdfs_entry", query={"key": key})


def pdfs_candidates(query: str = "") -> Dict[str, Any]:
    return _read("pdfs_candidates", query={"q": (query or "").strip()})


#: Manage PDFs actions, and the body fields each takes besides ``key``.
PDFS_ACTIONS = {
    "cleanup": ("survivor", "actions", "acknowledge_annotations"),
    "link": ("pdf", "acknowledge_plugins"),
    "trash": ("attachment", "survivor", "accept_annotation_loss"),
}


def _pdfs_body(citation_key: str, action: str, fields: Dict[str, Any]) -> Any:
    if action not in PDFS_ACTIONS:
        return {"error": "action must be one of " + ", ".join(PDFS_ACTIONS)}
    key = (citation_key or "").strip()
    if not key:
        return {"error": "A citation key is required."}
    # decision_ref (T6): the approving decision-file row, recorded on a
    # cleanup operation so every held file names the decision that moved it.
    extra = {"token", "decision_ref"} if action == "cleanup" else {"token"}
    unknown = set(fields) - set(PDFS_ACTIONS[action]) - extra
    if unknown:
        return {"error": f"{action} does not take: " + ", ".join(sorted(unknown))}
    body = {"key": key, "action": action}
    body.update({k: v for k, v in fields.items() if v is not None})
    return body


def pdfs_preview(citation_key: str, action: str, **fields: Any) -> Dict[str, Any]:
    """Plan a Manage PDFs action; writes nothing. Returns ``plan``, ``token``,
    ``blockers`` and ``executable``."""
    body = _pdfs_body(citation_key, action, fields)
    if "error" in body:
        return body
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    return _ok(_http_json("pdfs_preview", body, base_url=base, timeout=300.0))


def pdfs_apply(citation_key: str, action: str, token: str, **fields: Any) -> Dict[str, Any]:
    """Apply a previewed plan. ``token`` must be the preview's; the server
    re-plans and refuses on any difference."""
    if not (token or "").strip():
        return {"error": "The preview token is required."}
    body = _pdfs_body(citation_key, action, {**fields, "token": token})
    if "error" in body:
        return body
    body["confirm"] = True
    return _write("pdfs_apply", body, timeout=900.0)


def pdfs_holding() -> Dict[str, Any]:
    """Recoverable storage: every held file with its original path, sha256
    and approving decision row, plus every unsettled operation."""
    return _read("pdfs_holding")


def pdfs_restore(operation: str) -> Dict[str, Any]:
    op = (operation or "").strip()
    if not op:
        return {"error": "An operation id is required."}
    return _write("pdfs_restore", {"operation": op, "confirm": True}, timeout=900.0)


def pdfs_inspect(operation: str) -> Dict[str, Any]:
    op = (operation or "").strip()
    if not op:
        return {"error": "An operation id is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    return _ok(_http_json("pdfs_inspect", {"operation": op}, base_url=base, timeout=300.0))


def pdfs_complete(operation: str, token: str) -> Dict[str, Any]:
    op, tok = (operation or "").strip(), (token or "").strip()
    if not op or not tok:
        return {"error": "An operation id and the inspection token are required."}
    return _write("pdfs_complete", {"operation": op, "token": tok, "confirm": True})


#: ``accession rows`` scopes, mirroring the route's ``_ROW_SCOPES``.
ACCESSION_ROW_SCOPES = ("orphans", "pending", "all")


def accession_rows(scope: str = "orphans") -> Dict[str, Any]:
    """Orphan and/or curated ``pending`` PDFs joined to cached resolutions.

    No network and no hashing. *scope* is ``orphans`` (the historical set and
    the default), ``pending`` (the "add to Zotero later" bucket — Phase 2 of
    the annual cleanup: 296 files where the library has 0 orphans) or ``all``.
    An unknown value is refused here rather than sent, so a typo cannot come
    back as an empty orphan list that reads like "nothing to do".
    """
    if scope not in ACCESSION_ROW_SCOPES:
        return {"error": f"scope must be one of {', '.join(ACCESSION_ROW_SCOPES)}."}
    return _read("accession_rows", query={"scope": scope}, timeout=300.0)


def accession_identify(pdf: str, *, force: bool = False,
                       on_event: Optional[Any] = None) -> Dict[str, Any]:
    """Run the online identification ladder for ONE orphan (egress)."""
    rel = (pdf or "").strip()
    if not rel:
        return {"error": "A vault-relative PDF path is required."}
    body: Dict[str, Any] = {"pdf": rel}
    if force:
        body["force"] = True
    return _stream("accession_identify", body, "row", timeout=300.0, on_event=on_event)


def accession_batch(pdfs: List[str], *, force: bool = False,
                    on_event: Optional[Any] = None) -> Dict[str, Any]:
    """Identify an explicit selection (egress; bounded by the server's
    ``accession_batch_max``). One ``rows`` entry per PDF."""
    rels = _clean_list(pdfs)
    if not rels:
        return {"error": "At least one PDF is required."}
    body: Dict[str, Any] = {"pdfs": rels, "confirm": True}
    if force:
        body["force"] = True
    # One PDF may take the whole 60 s ladder budget; a day covers 1 400.
    return _stream("accession_batch", body, "accession", timeout=86400.0,
                   on_event=on_event)


def maintenance_rename(rel: str, new_name: str) -> Dict[str, Any]:
    """Rename one biblio PDF to the server's canonical proposal (private
    build). Journalled; Zotero's linked-file pointer moves with it."""
    r, n = (rel or "").strip(), (new_name or "").strip()
    if not r or not n:
        return {"error": "rel and new_name are required."}
    return _maint_unavailable_mapped(
        _write("maintenance_rename", {"rel": r, "new_name": n, "confirm": True}))


def maintenance_rename_preview(rels: List[str]) -> Dict[str, Any]:
    """What `maintenance_rename` would do for each biblio PDF (read-only,
    private build; ≤200 per call, proposals assume the given order)."""
    clean = _clean_list(rels)
    if not clean:
        return {"error": "At least one PDF is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    return _maint_unavailable_mapped(_ok(_http_json(
        "maintenance_rename_preview", {"rels": clean}, base_url=base,
        timeout=600.0)))


def _one_key(citation_key: str) -> Optional[str]:
    key = (citation_key or "").strip()
    return key or None


def notesync_legacy_status() -> Dict[str, Any]:
    """The one-directional status (missing notes, region update candidates)."""
    return _read("notesync_legacy_status", timeout=180.0)


def notesync_preview(citation_key: str) -> Dict[str, Any]:
    key = _one_key(citation_key)
    if key is None:
        return {"error": "A citation key is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    return _ok(_http_json("notesync_preview", {"citation_key": key}, base_url=base))


def notesync_create(citation_keys: List[str]) -> Dict[str, Any]:
    """Create MISSING Obsidian reference notes (≤100; journalled)."""
    keys = _clean_list(citation_keys)
    if not keys:
        return {"error": "At least one citation key is required."}
    if len(keys) > 100:
        return {"error": "At most 100 notes per request."}
    return _write("notesync_create", {"citation_keys": keys, "confirm": True}, timeout=900.0)


def notesync_update_preview(citation_key: str) -> Dict[str, Any]:
    key = _one_key(citation_key)
    if key is None:
        return {"error": "A citation key is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    return _ok(_http_json("notesync_update_preview", {"citation_key": key}, base_url=base))


def notesync_update(citation_key: str, content_sha256: str, proposed_sha256: str, *,
                    accept_overwrite: bool = False) -> Dict[str, Any]:
    key = _one_key(citation_key)
    if key is None or not content_sha256 or not proposed_sha256:
        return {"error": "citation_key, content_sha256 and proposed_sha256 are required."}
    body: Dict[str, Any] = {"citation_key": key, "content_sha256": content_sha256,
                            "proposed_sha256": proposed_sha256, "confirm": True}
    if accept_overwrite:
        body["accept_overwrite"] = True
    return _write("notesync_update", body)


def notesync_layout(direction: str, *, dry_run: bool = True) -> Dict[str, Any]:
    """Marker-layout migration (``migrate``) or its inverse (``unmigrate``).
    ``dry_run`` sends no confirm, which the route treats as a dry run."""
    if direction not in ("migrate", "unmigrate"):
        return {"error": "direction must be migrate or unmigrate."}
    body: Dict[str, Any] = {"dry_run": True} if dry_run else {"confirm": True}
    return _write("notesync_" + direction, body, timeout=1800.0)


def notesync_bootstrap(citation_keys: Optional[List[str]] = None, *,
                       all_notes: bool = False) -> Dict[str, Any]:
    """Write a sync base for notes whose two sides are already equal."""
    keys = _clean_list(citation_keys)
    if bool(keys) == bool(all_notes):
        return {"error": "Give citation keys or all_notes, not both and not neither."}
    body: Dict[str, Any] = {"confirm": True}
    if all_notes:
        body["all"] = True
    else:
        body["citation_keys"] = keys
    return _write("notesync_bootstrap", body, timeout=1800.0)


def _pull_push(kind: str, citation_keys: List[str], cap: int,
               accept_conflict: bool) -> Dict[str, Any]:
    keys = _clean_list(citation_keys)
    if not keys:
        return {"error": "At least one citation key is required."}
    if len(keys) > cap:
        return {"error": f"At most {cap} notes per {kind} request."}
    body: Dict[str, Any] = {"citation_keys": keys, "confirm": True}
    if accept_conflict:
        body["accept_conflict"] = True
    return _write("notesync_" + kind, body, timeout=1800.0)


def notesync_pull(citation_keys: List[str], *, accept_conflict: bool = False) -> Dict[str, Any]:
    """Zotero → Obsidian region write (≤100)."""
    return _pull_push("pull", citation_keys, 100, accept_conflict)


def notesync_refresh_all(*, dry_run: bool = True,
                         plan_digest: Optional[str] = None) -> Dict[str, Any]:
    """Zotero → Obsidian for every note (zotero-authoritative mode). Not
    journalled; the route refuses unless git can restore every note it
    replaces. ``dry_run`` sends no confirm, which the route treats as a dry
    run; its answer carries the ``plan_digest`` a confirm must send back
    (2026-09-21: a confirm executes only the plan that was reviewed). The
    driver never fetches a digest itself — that would bind the confirm to a
    plan nobody looked at."""
    body: Dict[str, Any] = {"dry_run": True} if dry_run else {"confirm": True}
    if not dry_run and plan_digest:
        body["plan_digest"] = plan_digest
    return _write("notesync_refresh_all", body, timeout=1800.0)


def notesync_mirror(*, dry_run: bool = True, plan_digest: Optional[str] = None,
                    on_event: Optional[Any] = None) -> Dict[str, Any]:
    """Create + refresh + baseline the Obsidian mirror, in one sweep (SSE).

    Zotero-authoritative mode only. Three phases in order, each delegating to
    the writer that already owns it: create the missing notes (journalled,
    reversible from Settings → History and Restore), rewrite every region that
    differs (NOT journalled — the vault's git is its undo), then record a sync
    point for everything still without one. ``dry_run`` sends no confirm,
    which the route treats as a plan-only run that writes nothing.

    A pass-through, never a whitelist: the terminal ``mirror`` frame is
    returned whole, so a field a CLI consumer starts reading cannot be
    stripped on the way (the 2026-08-18 response-projection defect).

    A confirm must carry the dry run's ``plan_digest`` (2026-09-21); the
    driver passes the caller's and never fetches one on its own.
    """
    body: Dict[str, Any] = {"dry_run": True} if dry_run else {"confirm": True}
    if not dry_run and plan_digest:
        body["plan_digest"] = plan_digest
    return _stream("notesync_mirror", body, "mirror", timeout=3600.0,
                   on_event=on_event)


def notesync_push(citation_keys: List[str], *, accept_conflict: bool = False) -> Dict[str, Any]:
    """Obsidian → Zotero note write (≤50, version-guarded)."""
    return _pull_push("push", citation_keys, 50, accept_conflict)


def notesync_merge_preview(citation_key: str) -> Dict[str, Any]:
    key = _one_key(citation_key)
    if key is None:
        return {"error": "A citation key is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    return _ok(_http_json("notesync_merge_preview", {"citation_key": key}, base_url=base))


def notesync_merge(citation_key: str, result: str, content_sha256: str,
                   evidence: str) -> Dict[str, Any]:
    """Write a reviewed merge to BOTH sides (the route requires both)."""
    key = _one_key(citation_key)
    if key is None or not isinstance(result, str) or not content_sha256 or not evidence:
        return {"error": "citation_key, result, content_sha256 and evidence are required."}
    return _write("notesync_merge", {
        "citation_key": key, "result": result, "content_sha256": content_sha256,
        "evidence": evidence, "targets": {"obsidian": True, "zotero": True},
        "confirm": True})


def notesync_create_attempts() -> Dict[str, Any]:
    return _read("notesync_create_attempts")


def notesync_key_changes() -> Dict[str, Any]:
    """Reference notes left under a citation key Zotero no longer uses."""
    return _read("notesync_key_changes")


def notesync_rename_note(citation_key: str, *, dry_run: bool) -> Dict[str, Any]:
    """Rename one such note to its record's new key (the server derives it)."""
    key = _one_key(citation_key)
    if key is None:
        return {"error": "A citation key is required."}
    body: Dict[str, Any] = {"citation_key": key}
    body["dry_run" if dry_run else "confirm"] = True
    return _write("notesync_rename_note", body)


def notesync_inspect_create_attempt(citation_key: str) -> Dict[str, Any]:
    key = _one_key(citation_key)
    if key is None:
        return {"error": "A citation key is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    return _ok(_http_json("notesync_inspect_create_attempt", {"citation_key": key},
                          base_url=base))


def notesync_create_zotero_note(citation_key: str) -> Dict[str, Any]:
    """Create the missing Zotero reading note for one record. A 500 can still
    mean it was created — read ``status``/``note_key`` in the result."""
    key = _one_key(citation_key)
    if key is None:
        return {"error": "A citation key is required."}
    return _write("notesync_create_zotero_note", {"citation_key": key, "confirm": True})


#: How long a caller waits for a route that runs a COMPLETE Web observation.
#:
#: `web_snapshot.observe` pages through every item of the library AND its
#: trash, honouring the API's own pacing, and re-probes both at the end. On
#: the maintainer's ~14,000-item library that is tens of minutes, and the
#: server sends no bytes until it has an answer — so this bound behaves as a
#: wall clock, not a per-read one.
#:
#: Sized generously on purpose, because the two failure modes are not
#: symmetric. Waiting too long costs patience. Giving up too early on
#: `zotcreate_create` costs the PDF: the server finishes the observation and
#: POSTs while the client is gone, the response is lost, and the attempt
#: store is left holding a reservation it never saw acknowledged —
#: `STAGE_PARENT_UNKNOWN`, which has no automatic exit and blocks that file's
#: create for good.
WEB_OBSERVATION_TIMEOUT_S = 3600.0

#: A create runs the observation AGAIN as `before_dispatch`, then POSTs the
#: parent and the attachment, so it needs the observation budget plus room
#: for the writes that follow it.
WEB_CREATE_TIMEOUT_S = 5400.0


def zotcreate_preview(pdf: str) -> Dict[str, Any]:
    """The create draft for one orphan/pending PDF (writes nothing).

    On the WEB transport this is not the cheap local read its name suggests:
    since the absence proof landed, `preview` derives `can_write` from a
    complete library observation, so it costs one (2026-09-17 — the sibling
    `settle-never-created` had its timeout raised for exactly this and this
    one was missed, leaving a CLI preview that could never return)."""
    rel = (pdf or "").strip()
    if not rel:
        return {"error": "A vault-relative PDF path is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    return _ok(_http_json("zotcreate_preview", {"pdf": rel}, base_url=base,
                          timeout=WEB_OBSERVATION_TIMEOUT_S))


#: The create body fields a caller supplies (from an approved draft).
ZOTCREATE_FIELDS = ("pdf", "item_type", "fields", "creators", "physical",
                    "review_token", "target", "acknowledge_plugins")


def zotcreate_create(draft: Dict[str, Any], *, dry_run: bool = False) -> Dict[str, Any]:
    """Create a Zotero parent and link the PDF under it (one batch undo)."""
    if not isinstance(draft, dict):
        return {"error": "The draft must be an object."}
    unknown = set(draft) - set(ZOTCREATE_FIELDS)
    if unknown:
        return {"error": "Unknown draft fields: " + ", ".join(sorted(unknown))}
    body = dict(draft)
    body.update({"dry_run": True} if dry_run else {"confirm": True})
    # A dry run returns before any observation; a real create runs one.
    return _write("zotcreate_create", body,
                  timeout=300.0 if dry_run else WEB_CREATE_TIMEOUT_S)


#: The batch body fields a row may carry (an approved decision-file draft).
ZOTCREATE_BATCH_ROW_FIELDS = ("id", "pdf", "item_type", "fields", "creators",
                              "decision_ref")

#: Per-row allowance on top of the opening observation: 2 POSTs, 2 readbacks
#: and 3 delta reads, paced by the API. Generous for the same reason as
#: `WEB_CREATE_TIMEOUT_S` — the timeout is a wall clock over the whole stream,
#: and a client that gives up mid-batch stops the server between rows (never
#: mid-POST), which costs time, not a PDF.
WEB_BATCH_ROW_TIMEOUT_S = 90.0


def zotcreate_batch(rows: List[Dict[str, Any]], *, dry_run: bool,
                    acknowledge_plugins: bool = False,
                    on_event: Optional[Any] = None) -> Dict[str, Any]:
    """Create many approved drafts against ONE Web observation (T2, SSE).

    Returns ``{status, zotcreate_batch, rows}``: the terminal tally plus one
    frame per row the server reached. A dry run costs one observation."""
    if not isinstance(rows, list) or not rows:
        return {"error": "rows must be a non-empty list."}
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            return {"error": f"Row {i + 1} must be an object."}
        unknown = set(row) - set(ZOTCREATE_BATCH_ROW_FIELDS)
        if unknown:
            return {"error": f"Row {i + 1}: unknown fields " + ", ".join(sorted(unknown))}
    body: Dict[str, Any] = {"rows": rows}
    body.update({"dry_run": True} if dry_run else {"confirm": True})
    if acknowledge_plugins:
        body["acknowledge_plugins"] = True
    return _stream("zotcreate_batch", body, "zotcreate_batch",
                   timeout=WEB_OBSERVATION_TIMEOUT_S + WEB_BATCH_ROW_TIMEOUT_S * len(rows),
                   on_event=on_event)


def zotcreate_attempts() -> Dict[str, Any]:
    return _read("zotcreate_attempts")


def zotcreate_settle_never_created(pdf: str, *, dry_run: bool = True) -> Dict[str, Any]:
    """Unblock a PDF whose earlier Web create never reached Zotero, after a
    complete observation of the library (items + trash) finds no match.
    ``dry_run`` (the default) returns the evidence and writes nothing."""
    rel = (pdf or "").strip()
    if not rel:
        return {"error": "A vault-relative PDF path is required."}
    body: Dict[str, Any] = {"pdf": rel}
    body.update({"dry_run": True} if dry_run else {"confirm": True})
    # A complete Web observation pages through the whole library and its trash.
    return _write("zotcreate_settle_never_created", body,
                  timeout=WEB_OBSERVATION_TIMEOUT_S)


def zotcreate_reinstate_attempt(seq: int) -> Dict[str, Any]:
    """Undo a retirement (the PDF is blocked again)."""
    if not isinstance(seq, int) or seq < 1:
        return {"error": "seq must be a positive integer."}
    return _write("zotcreate_reinstate_attempt", {"seq": seq, "confirm": True})


def zotrecord_retitle(citation_key: str, title: str, *, expected_title: Optional[str] = None,
                      pdf: Optional[str] = None, accept_unauthenticated: bool = False,
                      dry_run: bool = False) -> Dict[str, Any]:
    """Write ONE title on a record's Zotero item, after checking the PDF prints
    it (annual cleanup T9). Journalled as ``zotero_metadata_write``."""
    key = (citation_key or "").strip()
    new_title = " ".join((title or "").split())
    if not key or not new_title:
        return {"error": "A citation key and a non-empty title are required."}
    body: Dict[str, Any] = {"citation_key": key, "title": new_title}
    if expected_title is not None:
        body["expected_title"] = expected_title
    if pdf:
        body["pdf"] = pdf
    if accept_unauthenticated:
        body["accept_unauthenticated"] = True
    body.update({"dry_run": True} if dry_run else {"confirm": True})
    return _write("zotrecord_retitle", body, timeout=600.0)


def tagging_restore_batch(batch_id: str, *, on_event: Optional[Any] = None) -> Dict[str, Any]:
    """Undo every applied op of one batch (a merge, a harmonize, a retire) —
    the SSE branch of ``POST /api/tagging/restore``; returns its terminal
    ``restore`` payload."""
    bid = (batch_id or "").strip()
    if not bid:
        return {"error": "A batch id is required."}
    return _stream("tagging_restore", {"batch_id": bid, "confirm": True}, "restore",
                   timeout=7200.0, on_event=on_event)


def zotrecord_merge_preview(keep: str, retire: str) -> Dict[str, Any]:
    """The merge plan for two records (reads only)."""
    k, r = (keep or "").strip(), (retire or "").strip()
    if not k or not r:
        return {"error": "keep and retire citation keys are required."}
    return _write("zotrecord_merge_preview", {"keep": k, "retire": r}, timeout=600.0)


def zotrecord_merge(keep: str, retire: str, *, dry_run: bool = False) -> Dict[str, Any]:
    """Merge two Zotero records: keep one, re-parent the other's children,
    union tags, alias the retired key (Extra `tex.ids` + note `aliases`),
    trash the retired item — one batch id, undone with `tagging restore`."""
    k, r = (keep or "").strip(), (retire or "").strip()
    if not k or not r:
        return {"error": "keep and retire citation keys are required."}
    body: Dict[str, Any] = {"keep": k, "retire": r}
    body.update({"dry_run": True} if dry_run else {"confirm": True})
    return _write("zotrecord_merge", body, timeout=900.0)


def zotrecord_trash(citation_key: str, *, accept_children: bool = False,
                    dry_run: bool = False) -> Dict[str, Any]:
    """Move an empty record's Zotero item (no PDF, no note) to the trash;
    journalled as ``zotero_item_trash``, undone with ``tagging restore``."""
    key = (citation_key or "").strip()
    if not key:
        return {"error": "A citation key is required."}
    body: Dict[str, Any] = {"citation_key": key}
    if accept_children:
        body["accept_children"] = True
    body.update({"dry_run": True} if dry_run else {"confirm": True})
    return _write("zotrecord_trash", body, timeout=600.0)


def zotlink_status() -> Dict[str, Any]:
    return _read("zotlink_status")


def zotlink_trash(item_keys: List[str], *, accept_annotation_loss: bool = False,
                  on_event: Optional[Any] = None) -> Dict[str, Any]:
    """Move Zotero attachments to Zotero's trash (≤200; reversible; never a
    DELETE). An annotated attachment needs ``accept_annotation_loss``."""
    keys = _clean_list(item_keys)
    if not keys:
        return {"error": "At least one attachment key is required."}
    if len(keys) > 200:
        return {"error": "At most 200 attachments per request."}
    body: Dict[str, Any] = {"item_keys": keys, "confirm": True}
    if accept_annotation_loss:
        body["accept_annotation_loss"] = True
    return _stream("zotlink_trash", body, "zotlink", timeout=7200.0, on_event=on_event)


#: Mirrors ``api/routes/zotlink.py::_RETITLE_MAX_KEYS`` (the server stays the
#: authority; refusing here only saves a round-trip).
ZOTLINK_RETITLE_MAX_KEYS = 1000


def zotlink_retitle(item_keys: List[str], *, dry_run: bool = False,
                    on_event: Optional[Any] = None) -> Dict[str, Any]:
    """Set linked attachments' Zotero titles to their filenames (≤1000;
    journalled, one ``batch_id``, undo with ``tagging restore-batch``).

    Keys only: the server derives each title from the path Zotero holds.
    """
    keys = _clean_list(item_keys)
    if not keys:
        return {"error": "At least one attachment key is required."}
    if len(keys) > ZOTLINK_RETITLE_MAX_KEYS:
        return {"error": f"At most {ZOTLINK_RETITLE_MAX_KEYS} attachments per request."}
    body: Dict[str, Any] = {"item_keys": keys}
    body.update({"dry_run": True} if dry_run else {"confirm": True})
    return _stream("zotlink_retitle", body, "zotlink_retitle", timeout=7200.0,
                   on_event=on_event)


def tagging_pdf_links_status(state: str = "") -> Dict[str, Any]:
    """Per record: is the note's ``pdf:`` list current, absent, stale or
    hand-written (read-only)."""
    query = {"state": state} if (state or "").strip() else None
    return _read("tagging_pdf_links_status", query=query, timeout=180.0)


# ---------------------------------------------------------------------------
# Library-workflow run (lots 3 and 5, 2026-09-21). Pass-throughs, never
# whitelists (the T1 rule): the CLI prints what the server answers.
# ---------------------------------------------------------------------------

#: Analyse/apply call several routes per unit, each with its own bounds; the
#: SSE wall clock is generous rather than tight so a long run is not cut short
#: client-side while the server keeps working (it would still finish and
#: record its outcomes, but the CLI would report a failure).
LIBRARY_RUN_TIMEOUT_S = 3600.0


def library_session_create(*, llm: Optional[Dict[str, Any]] = None,
                           label: str = "") -> Dict[str, Any]:
    body: Dict[str, Any] = {"label": label}
    if llm is not None:
        body["llm"] = llm
    return _write("library_session_create", body)


def library_session(sid: str) -> Dict[str, Any]:
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    return _ok(_http_json("library_session_get", base_url=base, path_params={"sid": sid}))


def library_session_llm(sid: str, llm: Dict[str, Any]) -> Dict[str, Any]:
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    return _ok(_http_json("library_session_llm", llm, base_url=base, mutating=True,
                          path_params={"sid": sid}))


def library_observe(sid: str) -> Dict[str, Any]:
    return _write("library_observe", {"session": sid})


def library_record_step(sid: str, unit: str, step: str, status: str,
                        detail: Optional[Dict[str, Any]] = None, *,
                        confirm: bool = False) -> Dict[str, Any]:
    """Resolve ONE ``uncertain`` step to ``done``/``failed`` after inspecting
    it (audit remediation 2026-09-23: the route resolves nothing else).
    Refused client-side without ``confirm`` — the driver never invents one."""
    if confirm is not True:
        return {"error": "Resolving a step needs an explicit confirm."}
    body: Dict[str, Any] = {"session": sid, "unit": unit, "step": step, "status": status,
                            "confirm": True}
    if detail is not None:
        body["detail"] = detail
    return _write("library_steps", body)


def library_analyze(sid: str, *, propose_tags: bool = False,
                    on_event: Optional[Any] = None) -> Dict[str, Any]:
    return _stream("library_analyze", {"session": sid, "propose_tags": bool(propose_tags)},
                   "library_analyze", timeout=LIBRARY_RUN_TIMEOUT_S, on_event=on_event)


def library_apply(sid: str, approvals: Dict[str, Any], *, confirm: bool,
                  on_event: Optional[Any] = None) -> Dict[str, Any]:
    """Apply reviewed rows. Refused client-side without ``confirm`` — the
    driver never invents one (the T1 rule)."""
    if confirm is not True:
        return {"error": "Applying needs an explicit confirm."}
    if not isinstance(approvals, dict):
        return {"error": "approvals must be an object."}
    return _stream("library_apply", {"session": sid, "confirm": True, "approvals": approvals},
                   "library_apply", timeout=LIBRARY_RUN_TIMEOUT_S, on_event=on_event)
