"""Persisted "not a duplicate" decisions (plan 2026-08-05/5).

``BASE_DIR/audit/dup_decisions.json`` holds the pair-ids of near-duplicate
candidate groups the user dismissed — zoplicate's "non-duplicate marklist"
idea on the ``mapping.json`` discipline: hand-made, unregenerable decisions,
so the write is RMW-locked, atomic AND fsynced (unlike the regenerable
``hash_cache.json``). ``clear`` is the undo verb, mirroring
``bridge.clear_curation``. The scan filters dismissed groups at build time;
a dismissal between scans is reflected on the next scan (snapshot semantics,
like every mapping edit).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_RMW_LOCK = threading.Lock()
_MAX_IDS = 10_000  # backstop; a pair_id is ~100 bytes


def decisions_path() -> Path:
    """``BASE_DIR/audit/dup_decisions.json`` (same dir as ``mapping.json``)."""
    from ..config import _audit_dir

    return Path(_audit_dir()) / "dup_decisions.json"


def _parse(text: str) -> set[str]:
    raw = json.loads(text)
    ids = raw.get("dismissed") if isinstance(raw, dict) else None
    return {s for s in ids if isinstance(s, str)} if isinstance(ids, list) else set()


def load_dismissed() -> set[str]:
    """The dismissed pair-id set; read failures degrade to empty.

    Degrading is right for the SCAN-time filter (a corrupt file must not fail
    a scan — the dismissed rows merely reappear). It is WRONG for the
    read-modify-write verbs, which use :func:`_load_for_rmw` instead: starting
    an RMW from an empty set over a merely-unreadable file would clobber every
    prior hand-made, unregenerable decision on the next save (2026-08-05
    verification finding).
    """
    try:
        return _parse(decisions_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()


def _load_for_rmw() -> set[str]:
    """Strict load for the write verbs: a MISSING file is an empty set (first
    ever decision), but an unreadable/corrupt file RAISES so the RMW refuses
    rather than silently restarting the decision list from empty."""
    try:
        text = decisions_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    return _parse(text)


def _save(ids: set[str]) -> None:
    target = decisions_path()
    payload = {"dismissed": sorted(ids)[:_MAX_IDS]}
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=".dup_decisions-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add_dismissed(pair_id: str) -> bool:
    """Record a dismissal. Returns True when the set changed (idempotent).

    Raises ``OSError``/``ValueError`` when the existing file is unreadable or
    corrupt (never clobbers it — the route surfaces a 500 instead).
    """
    with _RMW_LOCK:
        ids = _load_for_rmw()
        if pair_id in ids:
            return False
        ids.add(pair_id)
        _save(ids)
        return True


def clear_dismissed(pair_id: str) -> bool:
    """The undo: drop a dismissal. Returns True when it existed.

    Same strict-load contract as :func:`add_dismissed`.
    """
    with _RMW_LOCK:
        ids = _load_for_rmw()
        if pair_id not in ids:
            return False
        ids.discard(pair_id)
        _save(ids)
        return True
