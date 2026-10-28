"""Per-key sync base records, one JSON file each under the app data directory.

``BASE_DIR/notesync/base/<vault_key>/<key digest>.json``. Never inside the
vault: a base record is app state about a note, not the note.

Discipline: atomic write WITH fsync (:func:`core.utils.write_text_atomic`) —
unlike the regenerable memos (`hash_cache.json`, `doi_cache.json`), a base
record is load-bearing for the three-way diff, and losing one silently turns
the next pass's honest ``pull`` into an ``unbaselined`` that asks the user
again. A corrupt or foreign file loads as ``None`` (→ ``unbaselined``) and is
never raised into a listing.

The filename is a digest of the citation key, not the key itself: a citation
key is a filename in the vault by convention, not by contract, and this store
must not become the one place a strange key can shape a path.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

from core.constants import BASE_DIR
from core.utils import write_text_atomic
from notesync.state import BaseRecord


def _vault_key(vault_root: Path) -> str:
    return hashlib.sha256(str(Path(vault_root).resolve()).encode("utf-8")).hexdigest()[:16]


def store_dir(vault_root: Path) -> Path:
    return Path(BASE_DIR) / "notesync" / "base" / _vault_key(vault_root)


def _path(vault_root: Path, citation_key: str) -> Path:
    digest = hashlib.sha256(citation_key.encode("utf-8")).hexdigest()[:24]
    return store_dir(vault_root) / f"{digest}.json"


def load(vault_root: Path, citation_key: str) -> BaseRecord | None:
    p = _path(vault_root, citation_key)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rec = BaseRecord.from_json(data)
    # A digest collision (or a hand-edited file) must not hand back another
    # note's base as this note's.
    if rec is not None and rec.citation_key != citation_key:
        return None
    return rec


# ---------------------------------------------------------------------------
# Coordination (library-workflow plan lot 1, defect 7, 2026-09-21)
# ---------------------------------------------------------------------------
#
# Before this, ``save`` and ``delete`` shared nothing. A sweep computes its
# plan — including "this base is stale, its note is gone" — then executes it
# later, possibly seconds or minutes later behind the op-lock. In between, a
# concurrent ``/mirror`` or ``/create`` + bootstrap could recreate that note
# and record a NEW base for it; the old plan then deleted the new base, and the
# freshly created note came back ``unbaselined``.
#
# Two mechanisms close it, and both are needed:
#
# * a per-vault EXCLUSIVE lock around every save and every conditional delete,
#   so a delete's "is it still the base I planned against?" test and the
#   unlink cannot interleave with a save;
# * a revision check: the planner records the digest of the base FILE it saw
#   (:func:`file_digest`), and :func:`delete_if_unchanged` deletes only while
#   the file still has that digest AND the note it describes is still absent.
#   A base rewritten since the plan (even with identical fields — ``synced_at``
#   differs) no longer matches, so it survives.
#
# The lock is ``fcntl.flock`` on a lock file inside the store directory, taken
# on a FRESH file descriptor per acquisition. BSD ``flock`` locks belong to the
# open file description, so two threads of this process conflict exactly like
# two processes do — one mechanism covers the Waitress worker threads, an SSE
# worker and (should one ever exist) a second process. Every hold is a few
# file operations long; nothing slow ever runs under it.
#
# What it does NOT do: make a save and the NOTE write that justified it atomic.
# A note write and its base save remain two steps; the writers record the base
# after the note, so a crash between them leaves a note with no (or an older)
# base — which reads as ``unbaselined`` / a pull, never as a false ``in_sync``.

_LOCAL_LOCK = threading.Lock()   # serialises lock-file creation only
_HELD = threading.local()        # per-thread nesting guard

#: How long a writer waits for another holder of the store lock (audit
#: remediation 2026-09-23, P12). A blocking ``flock`` waited forever: a stuck
#: holder (a wedged process, a hung network volume) pinned every sync-point
#: writer — a request thread included — with no bound and no message. The
#: lock is held for single small JSON writes, so ten seconds is generous.
LOCK_WAIT_S = 10.0
_LOCK_POLL_S = 0.05


class StoreBusy(OSError):
    """The base store's lock stayed held past :data:`LOCK_WAIT_S`.

    An ``OSError`` so every caller that already treats a failed base write as
    a per-key failure (the mirror, refresh-all, bootstrap) keeps doing so;
    routes that do not catch it answer 503 (``api/routes/notesync.py``).
    """


@contextlib.contextmanager
def store_lock(vault_root: Path) -> Iterator[None]:
    """Exclusive per-vault lock over the base store (see the section above).

    NOT re-entrant: ``flock`` on a fresh descriptor would wait on the lock
    this very thread holds, forever. A nested acquisition — e.g. a
    ``still_stale`` callback that calls :func:`save` — raises instead of
    deadlocking (2026-09-21 review).
    """
    if getattr(_HELD, "depth", 0):
        raise RuntimeError("base_store.store_lock is not re-entrant")
    d = store_dir(vault_root)
    with _LOCAL_LOCK:
        d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(d / ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StoreBusy(
                        f"The note-sync base store is busy (locked for over "
                        f"{LOCK_WAIT_S:g} s); try again shortly.") from None
                time.sleep(_LOCK_POLL_S)
    except BaseException:
        os.close(fd)
        raise
    try:
        _HELD.depth = 1
        yield
    finally:
        _HELD.depth = 0
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def file_digest(vault_root: Path, citation_key: str) -> str | None:
    """sha256 of the base FILE's bytes, or ``None`` when there is none.

    The revision token a planner records beside a planned deletion. It hashes
    the bytes, not the parsed record, so a rewrite that happens to carry equal
    fields still counts as a new revision (``synced_at`` differs anyway)."""
    try:
        return hashlib.sha256(_path(vault_root, citation_key).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def save(vault_root: Path, rec: BaseRecord) -> Path:
    p = _path(vault_root, rec.citation_key)
    with store_lock(vault_root):
        write_text_atomic(str(p), json.dumps(rec.to_json(), ensure_ascii=False, indent=1))
    return p


def delete(vault_root: Path, citation_key: str) -> bool:
    """Unconditional delete, under the store lock. Prefer
    :func:`delete_if_unchanged` for anything planned earlier."""
    p = _path(vault_root, citation_key)
    with store_lock(vault_root):
        try:
            os.unlink(p)
            return True
        except FileNotFoundError:
            return False


def delete_if_unchanged(vault_root: Path, citation_key: str, expected_digest: str | None,
                        *, still_stale: Callable[[], bool] = lambda: True) -> str:
    """Delete a base only if it is still the revision a plan saw.

    Returns ``"deleted"``, ``"gone"`` (already absent — nothing to do),
    ``"changed"`` (rewritten since the plan: kept) or ``"not_stale"`` (the
    caller's *still_stale* re-check — e.g. "the note is still absent" — now
    says no: kept). Raises ``OSError`` when the unlink itself fails, so the
    caller can report that key rather than count it.

    Both tests and the unlink run under :func:`store_lock`, so no save can land
    between "this is the base I planned against" and its removal.
    """
    # An absent base needs no lock (and must not fail for want of one when
    # the store directory is unwritable — 2026-09-21 review). A save racing
    # this read is harmless: it creates a NEW revision, which the planned
    # deletion must not touch anyway.
    if file_digest(vault_root, citation_key) is None:
        return "gone"
    with store_lock(vault_root):
        current = file_digest(vault_root, citation_key)
        if current is None:
            return "gone"
        if expected_digest is None or current != expected_digest:
            return "changed"
        if not still_stale():
            return "not_stale"
        os.unlink(_path(vault_root, citation_key))
        return "deleted"


def load_all(vault_root: Path) -> dict[str, BaseRecord]:
    d = store_dir(vault_root)
    out: dict[str, BaseRecord] = {}
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            rec = BaseRecord.from_json(json.loads((d / name).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if rec is not None:
            out[rec.citation_key] = rec
    return out
