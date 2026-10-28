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


def _fsync_parent_dir(path) -> None:
    """Best-effort fsync of *path*'s directory entry (see the caller's note)."""
    try:
        fd = os.open(str(Path(path).parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


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
        # DURABILITY (2026-09-02, performance plan Phase 5): fsync the PARENT
        # DIRECTORY after the rename. The app-wide policy deliberately omits
        # this for regenerable files, but dup_decisions.json is one of the three
        # UNREGENERABLE decision sidecars: fsyncing the file alone makes its
        # CONTENT durable while the directory ENTRY that names it may not be,
        # so a power loss right after the rename can leave the old file — or
        # no file — despite a successful save. Best-effort: a platform or
        # filesystem that refuses to fsync a directory is not a reason to fail
        # a write that has already landed.
        _fsync_parent_dir(target)
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


def _check_room(ids: set[str]) -> None:
    # `_save` keeps only the first _MAX_IDS sorted ids. A single add never
    # reaches it in practice, but a bulk write could, and a silent truncation
    # would drop hand-made decisions — refuse instead.
    if len(ids) > _MAX_IDS:
        raise ValueError(
            f"Refusing to store more than {_MAX_IDS} dismissed duplicate groups.")


def add_dismissed_many(pair_ids) -> int:
    """Record several dismissals in ONE atomic write (2026-09-17, cleanup T1).

    Returns how many ids were new. All or nothing: the list form exists so a
    bulk decision never lands half-written, where the user could not tell
    which half. Same strict-load contract as :func:`add_dismissed`.
    """
    wanted = list(dict.fromkeys(pair_ids))
    with _RMW_LOCK:
        ids = _load_for_rmw()
        new = [p for p in wanted if p not in ids]
        if not new:
            return 0
        ids.update(new)
        _check_room(ids)
        _save(ids)
        return len(new)


def clear_dismissed_many(pair_ids) -> int:
    """The bulk undo: drop several dismissals in one write. Returns how many
    existed."""
    wanted = set(pair_ids)
    with _RMW_LOCK:
        ids = _load_for_rmw()
        gone = ids & wanted
        if not gone:
            return 0
        _save(ids - gone)
        return len(gone)
