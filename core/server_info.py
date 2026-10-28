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

#: What THIS process registered, if anything: ``(host, port)`` or ``None``.
#: Set by :func:`write_server_info`, read by :func:`ensure_server_info` so the
#: self-heal can only ever re-assert a registration this process already made —
#: never invent one. A test or a CLI import that never served leaves it None and
#: is therefore completely unaffected.
_OWN_REGISTRATION: Optional[tuple] = None


def write_server_info(port: int, host: str = "127.0.0.1") -> None:
    """Record this process as the running server (atomic + fsync'd).

    Called once the HTTP socket is provably accepting (the launch probe loop /
    right before the headless ``app.run``). Best-effort: a failed write only
    costs CLI discovery, never the server itself.
    """
    global _OWN_REGISTRATION
    _OWN_REGISTRATION = (host, int(port))
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


def ensure_server_info() -> bool:
    """Re-assert this process's registration if the record went missing/stale.

    Observed live 2026-08-12: the GUI was serving and answering ``/api/health``
    on its logged port while ``server.json`` was simply absent, so
    ``driver.resolve_base_url()`` found nothing and every CLI/MCP **write**
    refused — while the read-only in-process fallback kept answering, which made
    it read like a config problem rather than a discovery one. The cause was
    never isolated (see CLAUDE.md § Known Issues), so this does not pretend to
    fix it: it makes the failure survivable. Called from the ``/api/health``
    cache-miss path, a vanished record is restored within one UI poll instead of
    staying broken for the life of the process.

    Deliberately conservative — it re-writes ONLY when the record is missing,
    names a dead pid, names a live pid that is provably NOT serving on its
    claimed port (post-crash pid reuse — probed via ``/api/health``, the same
    check the driver applies before believing the record), or names us with a
    stale port. A record naming a live pid that ANSWERS the probe is left
    strictly alone: that is another server's claim, and the pid gate in
    :func:`clear_server_info` depends on nobody overwriting it.

    Returns True when a repair was written (for logging/tests).
    """
    if _OWN_REGISTRATION is None:
        return False  # this process never served; nothing to re-assert
    host, port = _OWN_REGISTRATION
    info = read_server_info()
    if info is not None:
        if info.get("pid") != os.getpid():
            if pid_alive(info["pid"]) and _health_ok(info["host"], info["port"]):
                return False  # someone else's live, serving claim — never steal it
        elif info.get("port") == port:
            return False  # our record, still correct
    logger.warning(
        "server discovery file %s was missing or stale; re-writing it "
        "(port %s). CLI/MCP writes were unavailable until now.",
        SERVER_INFO_FILE, port,
    )
    write_server_info(port, host)
    return True


def _health_ok(host: str, port: int) -> bool:
    """True iff ``/api/health`` answers 200 on *host*:*port* (short timeout).

    Mirrors the driver's pre-trust probe, header included (the origin guard
    403s a header-less probe). Kept stdlib-only per this module's rule.
    """
    import urllib.request

    try:
        req = urllib.request.Request(
            f"http://{host}:{int(port)}/api/health",
            headers={"X-Requested-With": "ChatEKLD"},
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 — any failure = not serving
        return False


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


class ServerAlreadyRunning(RuntimeError):
    """The application-data directory already has a server owner."""


class ServerLease:
    """Kernel-held ownership; the stable sidecar is never removed.

    The descriptor lives for the whole serving process (including GUI daemon
    shutdown). Closing or process death releases it without stale lock cleanup.
    """

    def __init__(self, fd: int):
        self._fd: int | None = fd

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def acquire_server_lease() -> ServerLease:
    """Reserve GUI/CLI startup before app imports, services or prewarming."""
    import fcntl

    path = SERVER_INFO_FILE + '.lock'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ServerAlreadyRunning(
                'A CHERQAiN server is already starting or running. Quit it before starting another.'
            ) from exc
        # Older releases do not hold the lease. Preserve their live claim;
        # an unresponsive server still must not gain a competing writer.
        info = read_server_info()
        if info and pid_alive(info['pid']):
            raise ServerAlreadyRunning(
                f"A CHERQAiN server is already running or registered (pid {info['pid']}). Quit it first."
            )
        return ServerLease(fd)
    except BaseException:
        os.close(fd)
        raise
