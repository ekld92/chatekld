"""Model Context Protocol (MCP) stdio server for CHERQAiN.

Exposes tools and resources to local AI Agents (Claude Code, OpenClaw, Cursor, Roo Code).
Runs over stdio transport using standard JSON-RPC 2.0.

Architectural Rationale & Constraints:
- CRITICAL: All logging MUST be redirected to `sys.stderr`. `sys.stdout` is strictly reserved
  for line-delimited JSON-RPC 2.0 protocol strings. Writing non-JSON data to stdout breaks
  communication with host agents (e.g. Claude Code, Cursor, OpenClaw).
- Concurrency Model: Incoming requests are dispatched using a ThreadPoolExecutor. This keeps
  the main stdio input loop responsive to incoming pings or notification events even while a
  long-running tool call (e.g. `audit_scan` or `generate_deck`) executes in a background worker.
- Error Handling: JSON-RPC 2.0 single-message semantics — `-32700` for malformed
  JSON, `-32602` for bad params/unknown tools, `-32603` for internal failures,
  `-32000` for the busy write slot; notifications are ignored without a
  response, and every request carrying an id is guaranteed an answer (see
  `process_line`). Batch arrays are NOT supported (no MCP host sends them).
"""

import json
import logging
import os
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

# Ensure repository root is on sys.path BEFORE domain imports
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.driver import (  # noqa: E402
    AUDIT_REPORT_NAMES,
    audit_inventory,
    audit_report,
    audit_scan,
    audit_summary,
    generate_deck,
    generate_doc,
    get_status,
    summarize_paper,
    tagging_apply,
    tagging_propose,
    vault_ask,
    vault_search,
    notesync_status,
)
from api.security import sanitise_error_msg  # noqa: E402
from core.constants import TAGGING_TARGETS  # noqa: E402
from core.utils import log_vault_write  # noqa: E402

# Redirect python logger strictly to stderr to protect stdout JSON-RPC stream
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] [MCP] %(levelname)s: %(message)s",
)
logger = logging.getLogger("papermind-mcp")

# Worker thread pool for offloading long-running tool executions. Write tools
# are additionally serialised on a single-slot semaphore (see WRITE_TOOLS) so at
# most one vault-mutating operation runs at a time even under a burst of calls —
# the driver dispatches those through the server's op-lock too, but this keeps
# the MCP layer from stacking concurrent writes on it.
executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="MCPWorker")
_write_semaphore = threading.Semaphore(1)

# Bound on waiting for the single write slot (2026-07-22 audit M9): an
# UNbounded acquire let one wedged write + three queued write calls consume
# all 4 pool workers, after which even ping/tools/list starved and the host
# marked the server unresponsive. A vault write queued behind a long doc
# generation should wait a while — but not forever.
#
# TIMEOUT CHAIN (2026-09-02): the wait now OUTLASTS the writes it serialises,
# and the pool is protected by bounding the WAITERS instead of the wait.
# Before: 300 s, while the driver gives a refactor apply 900 s — so a second
#   write queued behind a legitimate, still-running first one was told
#   "busy, retry shortly" after 300 s, at a moment when retrying could only
#   fail the same way. Outer-must-exceed-inner, broken at the CLI/MCP seam.
# Change: the wait is the longest write the driver allows plus a margin, and
#   at most _WRITE_WAITER_MAX callers may queue — beyond that the answer is
#   an immediate busy. The pool-starvation property the 300 s bound was
#   introduced for is therefore preserved exactly (holder 1 + waiters 2 = 3
#   of the 4 workers, leaving one for ping/tools/list), while a legitimate
#   waiter is no longer cut off inside the write it is waiting for.
# Pinned by: tests/test_mcp_server.py::TestWriteSlotWaiting.
_DRIVER_LONGEST_WRITE_S = 900.0
WRITE_LOCK_TIMEOUT_S = _DRIVER_LONGEST_WRITE_S + 60.0
#: How many callers may WAIT for the slot at once. Chosen so the holder plus
#: the waiters can never occupy every worker in the pool above.
_WRITE_WAITER_MAX = 2
_write_waiters = 0
_write_waiters_lock = threading.Lock()


class ServerBusyError(Exception):
    """The single write slot could not be acquired within the timeout."""


class UnknownToolError(Exception):
    """tools/call named a tool that is not in MCP_TOOLS (JSON-RPC -32602)."""


def _is_write_call(name: str, args: Dict[str, Any]) -> bool:
    """Whether this tool invocation MUTATES the vault (semaphore + audit line).

    Not a static set (2026-07-22 audit M3): ``generate_document`` writes a NEW
    vault note in its default markdown mode (via /api/docgen/generate-md,
    confirm-gated + journalled) but was missing from the old WRITE_TOOLS set —
    so it bypassed both the single-slot serialisation and the
    ``log_vault_write`` audit line the docs claim cover every write tool. Its
    latex mode is emit-only (scaffolds outside the vault) and stays a read.
    """
    if name == "biblio_apply_tags":
        return True
    if name == "generate_document":
        return (args or {}).get("mode", "markdown") == "markdown"
    return False

# stdout carries the line-delimited JSON-RPC stream; multiple worker threads may
# finish concurrently, so serialise writes (TextIOWrapper.write atomicity is a
# CPython implementation detail, not a guarantee).
_stdout_lock = threading.Lock()

# MCP Tool Definitions (JSON Schemas)
MCP_TOOLS = [
    {
        "name": "vault_search",
        "description": ("Search notes and documents in the Obsidian vault index. "
                        "Runs one full RAG chat turn (retrieval + an LLM answer) "
                        "and returns the retrieval sources plus the answer — "
                        "each call costs a generation, not just a lookup. Each "
                        "result is {source, score, is_image} plus an additive "
                        "paper: {key, label} when the audit's paper index knows "
                        "the chunk's paper."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query or keywords"},
                "top_k": {"type": "integer", "default": 5, "description": "Number of results to return"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "vault_ask",
        "description": "Ask a question to the Obsidian vault index using RAG or ReAct Agent mode with tool access.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Question for the vault"},
                "agent_mode": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable ReAct multi-turn tool agent mode",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "summarize_paper",
        "description": "Extract text and generate a structured summary of a local PDF paper.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pdf_path": {"type": "string", "description": "Absolute path to local PDF file"},
                "preset": {
                    "type": "string",
                    "enum": ["concise", "detailed"],
                    "default": "concise",
                    "description": "Summary preset",
                },
                "lang": {"type": "string", "default": "fr", "description": "Target summary language"},
                "document_type": {
                    "type": "string",
                    "default": "auto",
                    "description": "Document type prompt preset",
                },
                "audience": {
                    "type": "string",
                    "enum": ["researcher", "student", "public"],
                    "description": "Target audience (omit for the server default)",
                },
            },
            "required": ["pdf_path"],
        },
    },
    {
        "name": "audit_library",
        "description": "Run a read-only library audit scan reconciling Obsidian Vault notes against Zotero items and PDFs.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "audit_summary",
        "description": (
            "Headline counters from the last completed audit scan: how many "
            "records have a PDF, a reference note, a Zotero item; unmapped "
            "PDFs; tag coverage and tag depth. Read-only, and far smaller "
            "than audit_inventory — prefer this for 'where does the library "
            "stand'."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "notesync_status",
        "description": (
            "The two-way Zotero ⇄ Obsidian reading-note sync state, per note: "
            "in_sync, pull (Zotero changed), push (the note changed), conflict "
            "(both), unbaselined, missing on either side. Read-only; reads "
            "Zotero fresh. The write verbs are GUI-only and confirm-gated."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "audit_report",
        "description": (
            "One reconciliation report from the last completed audit scan "
            "(read-only). Use audit_summary first to see whether a report is "
            "worth pulling."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": list(AUDIT_REPORT_NAMES),
                    "description": "Which report to read",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "audit_inventory",
        "description": (
            "Every per-citation-key record from the last completed audit scan "
            "(read-only). LARGE — thousands of records. Prefer audit_summary "
            "for counts and audit_report for a specific question."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "biblio_propose_tags",
        "description": "Propose tags for a paper constrained to the vault's controlled vocabulary (_tags.md).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "citation_key": {"type": "string", "description": "BibTeX citation key (e.g., smith2024)"}
            },
            "required": ["citation_key"],
        },
    },
    {
        "name": "biblio_apply_tags",
        "description": "Apply tags to Obsidian note frontmatter, macOS Finder tags, or Zotero library item.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "citation_key": {"type": "string", "description": "BibTeX citation key"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of tags to apply",
                },
                "targets": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["note", "finder", "zotero"]},
                    "default": ["note", "finder", "zotero"],
                    "description": "Target stores to write tags to",
                },
            },
            "required": ["citation_key", "tags"],
        },
    },
    {
        "name": "generate_deck",
        "description": ("Generate a LaTeX Beamer slide deck grounded in vault "
                        "content (emit-only: scaffolds <out_dir>/<slug>/; the "
                        "template file is read and validated server-side)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic or title for the presentation deck"},
                "template_path": {"type": "string",
                                  "description": "Absolute path to the Beamer template .tex the deck inherits"},
                "out_dir": {"type": "string",
                            "description": "Output folder (default: the template's suite root)"},
                "deck_name": {"type": "string", "description": "Deck folder/slug name (default: topic)"},
                "instructions": {"type": "string", "description": "Free-form additional instructions"},
                "overwrite": {"type": "boolean", "default": False,
                              "description": "Overwrite an existing deck folder"},
            },
            "required": ["topic", "template_path"],
        },
    },
    {
        "name": "generate_document",
        "description": ("Generate a long-form Markdown note WRITTEN INTO the "
                        "vault (default; journalled + reversible) or an "
                        "emit-only LaTeX article grounded in vault content."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic or title for the document"},
                "instructions": {"type": "string", "description": "Free-form additional instructions"},
                "mode": {
                    "type": "string",
                    "enum": ["markdown", "latex"],
                    "default": "markdown",
                    "description": "Document format mode",
                },
                "template_path": {"type": "string",
                                  "description": "Article template .tex — REQUIRED when mode=latex"},
                "out_dir": {"type": "string",
                            "description": "Output folder for latex mode (default: template suite root)"},
            },
            "required": ["topic"],
        },
    },
]

#: Valid tool names, derived from the definitions so they can never drift.
_TOOL_NAMES = {t["name"] for t in MCP_TOOLS}


def send_response(response: Dict[str, Any]) -> None:
    """Write one JSON-RPC response line to stdout (serialised across threads)."""
    raw = json.dumps(response, ensure_ascii=False)
    with _stdout_lock:
        sys.stdout.write(raw + "\n")
        sys.stdout.flush()


def _dispatch_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Route a tool name to its driver function (no logging/serialisation)."""
    if name == "vault_search":
        res = vault_search(
            query=args.get("query", ""),
            top_k=args.get("top_k", 5),
        )
    elif name == "vault_ask":
        res = vault_ask(
            question=args.get("question", ""),
            agent_mode=args.get("agent_mode", False),
        )
    elif name == "summarize_paper":
        res = summarize_paper(
            pdf_path=args.get("pdf_path", ""),
            preset=args.get("preset", "concise"),
            lang=args.get("lang", "fr"),
            document_type=args.get("document_type", "auto"),
            audience=args.get("audience", ""),
        )
    elif name == "audit_library":
        res = audit_scan()
    elif name == "audit_summary":
        res = audit_summary()
    elif name == "notesync_status":
        res = notesync_status()
    elif name == "audit_report":
        res = audit_report(name=args.get("name", ""))
    elif name == "audit_inventory":
        res = audit_inventory()
    elif name == "biblio_propose_tags":
        res = tagging_propose(citation_key=args.get("citation_key", ""))
    elif name == "biblio_apply_tags":
        res = tagging_apply(
            citation_key=args.get("citation_key", ""),
            tags=args.get("tags", []),
            targets=args.get("targets", list(TAGGING_TARGETS)),
        )
    elif name == "generate_deck":
        res = generate_deck(
            topic=args.get("topic", ""),
            template_path=args.get("template_path", ""),
            out_dir=args.get("out_dir", ""),
            deck_name=args.get("deck_name", ""),
            instructions=args.get("instructions", ""),
            overwrite=bool(args.get("overwrite", False)),
        )
    elif name == "generate_document":
        res = generate_doc(
            topic=args.get("topic", ""),
            instructions=args.get("instructions", ""),
            mode=args.get("mode", "markdown"),
            template_path=args.get("template_path", ""),
            out_dir=args.get("out_dir", ""),
        )
    else:
        raise UnknownToolError(f"Unknown MCP tool: {name}")
    return res


def _acquire_write_slot() -> bool:
    """Take the single write slot, waiting only if the queue has room.

    Returns False for "busy" — either too many callers are already waiting
    (the pool-starvation bound) or the wait itself expired.
    """
    global _write_waiters
    with _write_waiters_lock:
        if _write_waiters >= _WRITE_WAITER_MAX:
            return False
        _write_waiters += 1
    try:
        return _write_semaphore.acquire(timeout=WRITE_LOCK_TIMEOUT_S)
    finally:
        with _write_waiters_lock:
            _write_waiters -= 1


def handle_tool_call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool and return the MCP text-content result.

    - Unknown names raise :class:`UnknownToolError` BEFORE any logging or
      semaphore work (mapped to -32602 by the caller — the generic -32603
      previously hid "you typo'd the tool" inside "the server broke").
    - WRITE calls (:func:`_is_write_call`) run under the single-slot semaphore
      with a BOUNDED acquire and are audit-logged via ``log_vault_write``;
      read tools only log an info line.
    - The driver already dispatches through the server's confirm-gated,
      journalled, reversible endpoints, so the write's safety net is intact.
    """
    if name not in _TOOL_NAMES:
        raise UnknownToolError(f"Unknown MCP tool: {name}")
    is_write = _is_write_call(name, args)
    acquired = False
    if is_write:
        logger.info("Executing MCP WRITE tool '%s'", name)
        log_vault_write("mcp_tool_call", name, json.dumps(args, ensure_ascii=False))
        acquired = _acquire_write_slot()
        if not acquired:
            raise ServerBusyError(
                "Busy: another vault write is in progress; retry shortly.")
    else:
        logger.info("Executing MCP tool '%s'", name)
    try:
        res = _dispatch_tool(name, args)
    finally:
        # Release ONLY what we acquired — a timeout path must not release
        # someone else's slot.
        if acquired:
            _write_semaphore.release()
    text_output = json.dumps(res, indent=2, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text_output}]}


def handle_request(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Process a single JSON-RPC request message complying with 2.0 protocol specs."""
    if not isinstance(req, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request: expected JSON object"},
        }

    method = req.get("method")
    req_id = req.get("id")

    # Ignore notifications (messages with method starting with 'notifications/' or without 'id')
    if method and method.startswith("notifications/"):
        return None

    # One params-shape gate for every branch below (2026-07-22 audit M2): a
    # non-object params previously AttributeError'd out of handle_request and
    # the request was silently dropped — the client hung on the id forever.
    params = req.get("params")
    if params is not None and not isinstance(params, dict):
        if req_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": "Invalid params: expected object"},
        }
    params = params or {}

    if method == "initialize":
        # Echo the client's offered protocolVersion when it names one
        # (spec-compliant version negotiation); fall back to our baseline.
        client_pv = params.get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": client_pv if isinstance(client_pv, str) and client_pv
                else "2024-11-05",
                "capabilities": {
                    "tools": {},
                    "resources": {},
                },
                "serverInfo": {
                    "name": "papermind-mcp",
                    "version": "1.0.0",
                },
            },
        }

    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": MCP_TOOLS},
        }

    elif method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})
        if not isinstance(tool_name, str) or not tool_name:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "Invalid params: tool name must be a string"},
            }
        if not isinstance(tool_args, dict):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "Invalid params: arguments must be an object"},
            }

        try:
            result = handle_tool_call(tool_name, tool_args)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except UnknownToolError as err:
            # -32602 (invalid params), not -32603: "you named a tool that
            # doesn't exist" is the caller's mistake, not a server fault.
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": str(err)},
            }
        except ServerBusyError as err:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(err)},
            }
        except Exception as err:
            logger.error(f"Error executing tool '{tool_name}': {err}\n{traceback.format_exc()}")
            # Redact before the message crosses to the MCP client + its logs —
            # the rest of the app routes errors through this same chokepoint, so
            # an accidental key-bearing exception must not leak here (audit M2).
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": (f"Internal error executing tool '{tool_name}': "
                                f"{sanitise_error_msg(str(err))}"),
                },
            }

    elif method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "resources": [
                    {
                        "uri": "papermind://status",
                        "name": "CHERQAiN System Status",
                        "mimeType": "application/json",
                        "description": "Current configuration, index status and health",
                    }
                ]
            },
        }

    elif method == "resources/read":
        uri = params.get("uri")
        if uri == "papermind://status":
            # Guarded (2026-07-22 audit M2): the in-process get_status()
            # fallback imports the heavy rag.vault chain — a raising import or
            # corrupt config previously escaped handle_request entirely and
            # the client hung with NO response for this id.
            try:
                status_data = get_status()
            except Exception as err:  # noqa: BLE001
                logger.error("resources/read status failed: %s\n%s",
                             err, traceback.format_exc())
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32603,
                        "message": f"Status unavailable: {sanitise_error_msg(str(err))}",
                    },
                }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(status_data, indent=2, ensure_ascii=False),
                        }
                    ]
                },
            }
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"Unknown resource URI: {uri}"},
            }

    else:
        # If it's a notification without id, do not return method not found error
        if req_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method '{method}' not found"},
        }


def process_line(line: str) -> None:
    """Worker task to process a single line from stdin.

    RESPONSE GUARANTEE (2026-07-22 audit M2): once the line parses to a
    request carrying an id, that id ALWAYS gets an answer — an exception
    anywhere in handle_request previously logged to stderr and sent nothing,
    so the host client (Claude Code / Cursor) waited on the call until its own
    timeout with no visible error.
    """
    req_id = None
    try:
        req = json.loads(line)
        if isinstance(req, dict):
            req_id = req.get("id")
        resp = handle_request(req)
        if resp is not None:
            send_response(resp)
    except json.JSONDecodeError:
        send_response(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error: invalid JSON string"},
            }
        )
    except Exception as e:
        logger.error(f"Error handling MCP input line: {e}\n{traceback.format_exc()}")
        if req_id is not None:
            try:
                send_response({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32603,
                        "message": f"Internal error: {sanitise_error_msg(str(e))}",
                    },
                })
            except Exception:  # noqa: BLE001 — a broken stdout has no recovery
                logger.error("could not send error response", exc_info=True)


def main():
    """Main stdio loop reading line-delimited JSON-RPC requests."""
    logger.info("CHERQAiN MCP Server starting over stdio...")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        # Dispatch to thread pool to avoid blocking stdio loop on heavy tools
        executor.submit(process_line, line)


if __name__ == "__main__":
    main()
