"""On-disk persistence for the widget snapshot + anti-repeat history.

Two small JSON files under ``BASE_DIR`` (never the vault):

* ``widget.json`` — the snapshot external widgets read. Versioned like
  ``server.json`` (schema-checked on read, ``None`` on absent/corrupt, never
  deleted here); regenerable, so the atomic write deliberately skips fsync
  (the ``hash_cache`` trade — a lost snapshot costs one Refresh click, not
  data).
* ``widget_history.json`` — the last ``_HISTORY_CAP`` papers shown, so the
  daily draw avoids immediate repeats. RMW under a module lock: the scan
  listener (worker thread) and the refresh route (request thread) both write.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from core.constants import BASE_DIR

WIDGET_FILE = os.path.join(BASE_DIR, "widget.json")
HISTORY_FILE = os.path.join(BASE_DIR, "widget_history.json")

_HISTORY_CAP = 14
_LOCK = threading.Lock()


def _write_json_no_fsync(path: str, payload: Any) -> None:
    """Atomic temp-sibling + ``os.replace`` write WITHOUT fsync (regenerable)."""
    import tempfile

    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".widget-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        # Not just OSError: a TypeError from json.dump (an unserializable
        # value in a future payload field) must not strand the temp sibling.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_snapshot(payload: dict) -> None:
    """Persist the widget payload atomically."""
    _write_json_no_fsync(WIDGET_FILE, payload)


def read_snapshot() -> dict | None:
    """Read the snapshot back; ``None`` on absent/corrupt/wrong-version.

    The ``server.json`` discipline: schema-check, degrade to ``None``, never
    delete — a corrupt file is overwritten by the next successful write.
    """
    try:
        with open(WIDGET_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    return data


def load_history() -> list[str]:
    """Recent paper keys, oldest first; empty on absent/corrupt."""
    with _LOCK:
        return _load_history_unlocked()


def _load_history_unlocked() -> list[str]:
    try:
        with open(HISTORY_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        return []
    return [str(k) for k in data["keys"]]


def append_history(key: str, cap: int = _HISTORY_CAP) -> None:
    """Record *key* as shown (deduped to the most recent slot, capped)."""
    if not key:
        return
    with _LOCK:
        keys = [k for k in _load_history_unlocked() if k != key]
        keys.append(key)
        _write_json_no_fsync(HISTORY_FILE, {"keys": keys[-cap:]})
