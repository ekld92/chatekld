"""Server-discovery handshake file (2026-07-22 audit C0).

The GUI binds a RANDOM free port (``launch.py::find_free_port``) while the CLI/
MCP driver probed a hardcoded ``:5000`` — so "open the app, then retry" could
never work, and running ``papermind server`` beside the GUI created two
processes with two independent in-process op-locks over one vault (quietly
voiding the driver's "one server arbitrates all writes" contract).

The running server (GUI or headless) writes ``BASE_DIR/server.json`` once it is
accepting requests; the driver resolves its base URL from it (env override →
this file → legacy :5000 probe), re-probing ``/api/health`` before trusting any
candidate. ``BASE_DIR`` honours ``CHATEKLD_BASE_DIR``, so the test sandbox is
isolated for free.

Dependency-light on purpose: stdlib + ``core.constants``/``core.utils`` only —
the CLI must be able to import this without dragging in Flask/LlamaIndex.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from core.constants import BASE_DIR
from core.utils import write_text_atomic

logger = logging.getLogger(__name__)

SERVER_INFO_FILE = os.path.join(BASE_DIR, "server.json")

_SCHEMA_VERSION = 1


def write_server_info(port: int, host: str = "127.0.0.1") -> None:
    """Record this process as the running server (atomic + fsync'd).

    Called once the HTTP socket is provably accepting (the launch probe loop /
    right before the headless ``app.run``). Best-effort: a failed write only
    costs CLI discovery, never the server itself.
    """
    try:
        write_text_atomic(SERVER_INFO_FILE, json.dumps({
            "version": _SCHEMA_VERSION,
            "host": host,
            "port": int(port),
            "pid": os.getpid(),
            "created_at": time.time(),
        }))
    except OSError:
        logger.warning("could not write %s", SERVER_INFO_FILE, exc_info=True)


def read_server_info() -> Optional[dict]:
    """Parsed + schema-checked server record, or ``None``.

    ``None`` for absent/corrupt/wrong-shape files — readers always re-probe
    health anyway, so a bad file is simply ignored (never deleted here: it may
    belong to a live server whose write we'd be racing).
    """
    try:
        data = json.loads(open(SERVER_INFO_FILE, encoding="utf-8").read())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
        return None
    if not isinstance(data.get("port"), int) or not isinstance(data.get("pid"), int):
        return None
    if not isinstance(data.get("host"), str) or not data["host"]:
        return None
    return data


def clear_server_info() -> None:
    """Delete the record — but ONLY if it names this process.

    The pid gate is what makes shutdown safe against races: a crashed old
    instance's stale file, or a second process's record, is never deleted by
    someone else's cleanup. Stale files are harmless (readers pid+health-check).
    """
    info = read_server_info()
    if info is None or info.get("pid") != os.getpid():
        return
    try:
        os.unlink(SERVER_INFO_FILE)
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    """Best-effort liveness probe (signal 0)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
