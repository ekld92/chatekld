"""Unified execution driver for the ChatEKLD / PaperMind CLI and MCP server.

Design (rewritten 2026-07-21 after the audit of the first cut, which called
functions/kwargs/config-keys that do not exist and dispatched to the server
WITHOUT the ``X-Requested-With`` header — so every request 403'd and every path
fell into a broken in-process branch):

**HTTP-first.** The running Flask app already implements every workflow with its
real validation, confirm-gating, journalling and single-writer locking. The CLI
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

**Consent model preserved over MCP.** The tag/refactor write tools call the
same confirm-gated endpoints the GUI uses (``confirm: true``), so the writes are
vocabulary-validated, scope-locked, WYSIWYG-guarded, journalled and reversible —
the app's contract, not a bypass of it. Refactor apply runs a plan first and
submits only the plan's own per-note hashes, so the server's stale-diff/WYSIWYG
guards still hold.

Path inputs (vault sub-folders) are validated with ``core.paths.resolve_under_root``
before they reach any endpoint.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Legacy fixed port — kept as the LAST resolution candidate so a
#: ``papermind server`` on the default port still works if the server-info
#: file write failed. NOT the primary mechanism (the GUI binds a random port).
LEGACY_SERVER_URL = "http://127.0.0.1:5000"

#: The origin guard (api/security.py) 403s any /api request missing this header.
_APP_HEADERS = {"X-Requested-With": "ChatEKLD", "User-Agent": "PaperMind-Driver"}

_SERVER_DOWN = (
    "The ChatEKLD server is not running. Start it with `papermind server` "
    "(or open the ChatEKLD app), then retry. Write operations require the "
    "running server so its locking, validation and journalling apply."
)

#: Declarative endpoint registry (2026-07-22 audit C4a). Every HTTP call the
#: driver makes goes through this table BY NAME, so the contract ratchet
#: (tests/test_driver_contracts.py) can assert path AND method against the
#: Flask url_map — including endpoints previously built with f-strings that
#: the regex scan missed (/api/upload). Add a row when adding a driver call.
ENDPOINTS: Dict[str, tuple] = {
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
    "tagging_propose":   ("/api/tagging/propose", "POST"),
    "tagging_apply":     ("/api/tagging/apply-all", "POST"),
    "tagging_restore":   ("/api/tagging/restore", "POST"),
    "refactor_plan":     ("/api/refactor/plan", "POST"),
    "refactor_apply":    ("/api/refactor/apply", "POST"),
    "refactor_restore":  ("/api/refactor/restore", "POST"),
    "deck_load_template":   ("/api/deck/load-template", "POST"),
    "deck_generate":        ("/api/deck/generate", "POST"),
    "docgen_load_template": ("/api/docgen/load-template", "POST"),
    "docgen_generate":      ("/api/docgen/generate", "POST"),
    "docgen_generate_md":   ("/api/docgen/generate-md", "POST"),
}


# ---------------------------------------------------------------------------
# HTTP plumbing (always sends the app header)
# ---------------------------------------------------------------------------


def is_server_running(base_url: str = LEGACY_SERVER_URL) -> bool:
    """True iff the local app answers ``/api/health`` (WITH the app header).

    The header is mandatory: without it the origin guard 403s even the health
    probe, which is exactly why the first cut always thought the server was
    down. A 200 means reachable; any error/timeout/non-200 means not.
    """
    try:
        url = f"{base_url.rstrip('/')}/api/health"
        req = urllib.request.Request(url, headers=_APP_HEADERS)
        with urllib.request.urlopen(req, timeout=1.5) as resp:
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
       headless ``papermind server``; trusted only when its pid is alive AND
       the probe passes. A stale file is ignored, never deleted (the writer
       owns deletion — see ``core.server_info.clear_server_info``).
    3. The legacy fixed ``:5000`` probe.

    Deliberately NOT cached in a module global: a GUI restart changes the port
    mid-session, and one 1.5 s-bounded probe per CLI invocation is negligible.
    """
    env_url = os.environ.get("CHATEKLD_API_URL")
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
    "on the server. Check `papermind status` and the op journal (restore "
    "manifest) before retrying."
)


def _http_json(
    endpoint_key: str,
    data: Optional[Dict[str, Any]] = None,
    *,
    base_url: str,
    timeout: float = 120.0,
    mutating: bool = False,
) -> Dict[str, Any]:
    """Call a registered JSON endpoint and return the parsed body (or ``{"error"}``).

    *endpoint_key* indexes :data:`ENDPOINTS` (path + method), so every call
    site is ratchet-checkable. ``mutating=True`` rewrites network-failure
    errors with the may-have-landed note (see :data:`_MUTATING_TIMEOUT_NOTE`).
    """
    path, method = ENDPOINTS[endpoint_key]
    url = f"{base_url.rstrip('/')}{path}"
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
                return {"status": "ok"}
            try:
                return json.loads(body)
            except json.JSONDecodeError as err:
                return {"error": f"Invalid JSON response from server: {err}"}
    except urllib.error.HTTPError as err:
        try:
            err_json = json.loads(err.read().decode("utf-8"))
            if isinstance(err_json, dict) and "error" in err_json:
                return err_json
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
) -> Dict[str, Any]:
    """POST *data* and consume the SSE stream.

    Returns ``{"tokens": <joined answer text>, "events": [...], "terminal":
    <last non-token/info frame>, "error": <first error frame if any>}``. The
    SSE contract is one JSON object per ``data:`` line, ending with
    ``data: [DONE]`` (see the root CLAUDE.md → SSE Contract).
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[len("data:"):].strip()
                if body == "[DONE]":
                    break
                try:
                    ev = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
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
                            "refactor", "note", "retrieval")):
                        terminal = ev
    except urllib.error.HTTPError as err:
        try:
            j = json.loads(err.read().decode("utf-8"))
            return {"error": j.get("error", f"HTTP {err.code}")}
        except Exception:  # noqa: BLE001
            return {"error": f"HTTP {err.code}: {err.reason}"}
    except urllib.error.URLError as err:
        return {"error": f"Network error connecting to server: {err.reason}"}
    except Exception as err:  # noqa: BLE001
        return {"error": f"Stream failed: {err}"}
    out: Dict[str, Any] = {"tokens": "".join(tokens), "events": events}
    if terminal is not None:
        out["terminal"] = terminal
    if error is not None:
        out["error"] = error
    return out


def _upload_pdf(pdf_path: str, *, base_url: str) -> Dict[str, Any]:
    """Multipart-POST a local PDF to ``/api/upload``; returns ``{upload_id}`` or error."""
    with open(pdf_path, "rb") as fh:
        file_bytes = fh.read()
    boundary = f"----PaperMind{uuid.uuid4().hex}"
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
    printer never matched, so ``papermind status`` printed "Status: None"
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
            "embed_model": cfg.get("embed"),
            "vault_path": cfg.get("obsidian_vault_path"),
        },
        # get_status_payload() is the RICH dict /api/obsidian/status serves —
        # same shape as the server path (get_status() alone is just a string).
        "vault_index": obsidian_manager.get_status_payload(),
    }


def get_config() -> Dict[str, Any]:
    """Load the configuration dict (read-only; safe in-process)."""
    from core.config import load_config

    return dict(load_config())


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
        return {"error": res["error"]}
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
    if res.get("error"):
        return {"error": res["error"]}
    answer = res.get("tokens", "")
    sources = []
    term = res.get("terminal") or {}
    if isinstance(term.get("retrieval"), list):
        sources = term["retrieval"]
    return {
        "status": "ok",
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


def vault_index() -> Dict[str, Any]:
    """Build/update the vault index THROUGH the server (single-writer lock).

    Indexing is always INCREMENTAL — the route has no force semantics (the
    former ``force=`` parameter sent a ``force_reindex`` body key the server
    never read; removed 2026-07-22 rather than wired, since wiring it would
    mean a destructive reset). Use the GUI's Reset for a from-scratch rebuild.
    """
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("obsidian_index", {}, base_url=base, timeout=30.0,
                     mutating=True)
    if "error" in res:
        return res
    return {"status": "ok", "index": res, "note": "Indexing runs in the "
            "background; poll status with `papermind status`."}


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
            "note": "Scan runs in the background; poll `papermind status` or the "
                    "audit status endpoint for completion."}


# ---------------------------------------------------------------------------
# Biblio tagging (propose = read-only; apply = confirm-gated write)
# ---------------------------------------------------------------------------


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
    if not isinstance(tags, list) or not tags:
        return {"error": "At least one tag is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    body: Dict[str, Any] = {
        "citation_key": citation_key.strip(),
        "tags": [str(t) for t in tags],
        "confirm": True,
    }
    if targets:
        want = set(targets)
        body["targets"] = {t: (t in want) for t in ("note", "finder", "zotero")}
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
# Note refactor (plan = read-only; apply = confirm-gated + WYSIWYG-guarded)
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


def refactor_plan(subfolder: str = "") -> Dict[str, Any]:
    """Run the read-only Note Refactor analysis over a scoped sub-folder."""
    if subfolder:
        safe = _safe_subfolder(subfolder)
        if safe is None:
            return {"error": f"Invalid or escaping subfolder: {subfolder}"}
        subfolder = safe
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_sse("refactor_plan", {"scope_subdir": subfolder}, base_url=base)
    if res.get("error"):
        return {"error": res["error"]}
    notes = [ev["note"] for ev in res.get("events", []) if "note" in ev]
    summary = None
    for ev in res.get("events", []):
        if "refactor" in ev:
            summary = ev["refactor"]
    return {"status": "ok", "subfolder": subfolder,
            "notes": notes, "summary": summary}


def refactor_apply(subfolder: str = "", rels: Optional[List[str]] = None) -> Dict[str, Any]:
    """Apply the callout-only transform to notes, confirm-gated + WYSIWYG-guarded.

    Runs a plan first to obtain each note's ``content_sha256`` /
    ``proposed_sha256`` and submits ONLY those, so the server's stale-diff +
    WYSIWYG guards hold exactly as they do for the GUI (the app never applies a
    whole folder blind). *rels* optionally restricts the write to specific notes.
    """
    plan = refactor_plan(subfolder)
    if "error" in plan:
        return plan
    approved = []
    want = set(rels) if rels else None
    for note in plan.get("notes", []):
        rel = note.get("rel_path")
        if not note.get("changed") or not rel:
            continue
        if want is not None and rel not in want:
            continue
        approved.append({
            "rel": rel,
            "content_sha256": note.get("content_sha256", ""),
            "proposed_sha256": note.get("proposed_sha256", ""),
        })
    if not approved:
        return {"status": "ok", "applied": [], "note": "No changed notes to apply."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    body = {"scope_subdir": plan.get("subfolder", ""), "confirm": True, "notes": approved}
    # 900 s: a large batch apply (per-note re-read + journal + atomic write
    # across a whole folder) is SSE-generation-scale work; the old 120 s
    # default meant "reported failure, landed write, lost op-ids" (audit C5).
    res = _http_json("refactor_apply", body, base_url=base, timeout=900.0,
                     mutating=True)
    if "error" in res:
        return res
    return {"status": "ok", "apply_result": res}


def refactor_restore(op_id: str) -> Dict[str, Any]:
    """Reverse one journalled refactor op by id (confirm-gated, via the server)."""
    if not op_id or not op_id.strip():
        return {"error": "op_id is required."}
    base = resolve_base_url()
    if base is None:
        return {"error": _SERVER_DOWN}
    res = _http_json("refactor_restore",
                     {"op_id": op_id.strip(), "confirm": True},
                     base_url=base, timeout=300.0, mutating=True)
    return res if "error" in res else {"status": "ok", "restore": res}


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
        return {"error": res["error"]}
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
    Note Refactor → Restore); ``latex`` scaffolds an emit-only article folder
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
            return {"error": res["error"]}
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
        return {"error": res["error"]}
    return {"status": "ok", "mode": "latex",
            "document": (res.get("terminal") or {}).get("doc")}
