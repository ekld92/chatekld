"""Shared, dependency-light concurrency + I/O primitives used across the app.

Two locks anchor the app's thread-safety story and both live here:

* :class:`ReaderWriterLock` — a writer-preferring, reentrant multi-reader /
  single-writer lock. It lets many vault-chat reads run concurrently while an
  index rebuild (the writer) still gets exclusive, starvation-free access.
* :class:`RagOperationLock` — a binary admission lock with a per-acquisition TTL
  and an epoch token, so only one long LlamaIndex operation runs at a time and a
  crashed or zombie worker cannot keep the lock forever.

Plus the crash-safe file writers (:func:`write_text_atomic` /
:func:`write_bytes_atomic`), the deletion/vault-mutation audit helpers
(:func:`log_storage_deletion` / :func:`log_vault_write`), and the defensive
numeric parsers the route layer uses to clamp request params. Everything here is
import-cheap and has no project dependency beyond ``core.constants`` (imported
lazily inside the function that needs it, to avoid an import cycle), so any layer
may depend on this module.
"""
import contextlib
import json
import logging
import os
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def write_text_atomic(path: str, text: str, encoding: str = "utf-8") -> None:
    """Write *text* to *path* atomically via a sibling temp file + rename.

    A direct ``open(path, "w")`` truncates the destination first, so a crash
    or SIGKILL mid-write leaves a partial (or empty) file behind.  Writing to
    a temp file and promoting it with ``os.replace()`` guarantees readers see
    either the old complete content or the new complete content — never a
    torn write.  The temp file MUST be a sibling of the destination (same
    directory, therefore same filesystem): ``os.replace()`` is only atomic
    within one filesystem, and a cross-device rename would raise ``EXDEV``.

    Text twin of ``rag/vault.py::_write_json_atomic``; same fd-ownership
    pattern as ``core/config.py::save_config``.
    """
    dir_ = os.path.dirname(path) or "."
    os.makedirs(dir_, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_, text=True)
    try:
        try:
            f = os.fdopen(fd, "w", encoding=encoding)
        except Exception:
            # os.fdopen failed before taking ownership of the descriptor —
            # close it ourselves or it leaks.
            os.close(fd)
            raise
        with f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_bytes_atomic(path: str, data: bytes) -> None:
    """Binary twin of :func:`write_text_atomic`: sibling temp file + ``os.replace``.

    Used by the Note Refactor archiver (Phase 2) to lay down PNG thumbnails and
    archived-original copies without ever leaving a half-written image behind.
    Same fd-ownership + same-directory (same-filesystem) discipline as the text
    writer — the temp file MUST be a sibling so ``os.replace`` stays atomic.
    """
    dir_ = os.path.dirname(path) or "."
    os.makedirs(dir_, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_)
    try:
        try:
            f = os.fdopen(fd, "wb")
        except Exception:
            os.close(fd)
            raise
        with f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def log_storage_deletion(reason: str) -> None:
    """Record an imminent deletion of the persistent obsidian index dir.

    Writes a warning to the app log AND a marker JSON file under BASE_DIR so
    the trail survives a log rotation. Used to track down rogue deletions of
    OBSIDIAN_INDEX_DIR: every legitimate call site (currently /api/reset and
    the _archive_old_index_dir rmtree fallback) must invoke this immediately
    before its rmtree.
    """
    from core.constants import BASE_DIR  # local import to avoid cycle at import time
    stack = "".join(traceback.format_stack())
    logger.warning("OBSIDIAN_INDEX_DIR deletion (%s)\n%s", reason, stack)
    marker_path = os.path.join(BASE_DIR, ".last_deletion_log")
    try:
        # Atomic write: this marker exists precisely to survive crashes and
        # abrupt shutdowns, so it must not itself be corruptible by one — a
        # torn write here would destroy the previous (still useful) trail.
        write_text_atomic(marker_path, json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "stack": stack,
        }, indent=2))
    except OSError:
        logger.exception("Could not write deletion marker file %s", marker_path)


def log_vault_write(action: str, path: str, detail: str = "") -> None:
    """Record one mutation of the user's Obsidian vault to the app log.

    The Note Refactor "apply"/"archive"/"restore" paths (Phase 2) are the app's
    first writers of the vault itself. Mirrors the ``log_storage_deletion``
    discipline: every note overwrite, attachment move, thumbnail write, and
    restore emits a single traceable WARNING line to ``chatekld.log`` so a vault
    change can always be traced back to the app even if the structured restore
    manifest is lost. The manifest (``refactor/journal.py``) is the *structured*
    record used for rollback; this is the human-readable trail.

    *action* is a short verb ("write_note", "move_out", "write_thumb",
    "restore_note", "restore_image"); *path* is the vault-relative (or archive)
    target; *detail* is optional extra context (e.g. a before/after hash).
    Never raises — logging must not break a write path.
    """
    try:
        logger.warning("VAULT WRITE [%s] %s%s", action, path,
                       (" — " + detail) if detail else "")
    except Exception:  # pragma: no cover — logging must never break a mutation
        pass


def cap(value: str, limit: int = 8_000) -> str:
    """Truncate a string at the given limit."""
    if not isinstance(value, str):
        return ""
    return value[:limit]

def parse_temperature(raw, default: float = 0.3) -> float:
    """Safely parse and clamp temperature to [0.0, 2.0]."""
    try:
        return max(0.0, min(2.0, float(raw)))
    except (TypeError, ValueError):
        return default

def parse_num_ctx(raw, default: int = 32768) -> int:
    """Safely parse and clamp num_ctx to [512, 131072]."""
    try:
        return max(512, min(131072, int(raw)))
    except (TypeError, ValueError):
        return default

def parse_num_predict(raw, default: int = 4096) -> int:
    """Safely parse and clamp num_predict (max tokens) to [64, 32768]."""
    try:
        mt = int(raw) if raw is not None else None
        if mt is not None:
            return max(64, min(32768, mt))
    except (TypeError, ValueError):
        pass
    return default

def parse_top_p(raw, default: float = 0.9) -> float:
    """Safely parse and clamp top_p to [0.0, 1.0]."""
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return default

def parse_repeat_penalty(raw, default: float = 1.1) -> float:
    """Safely parse and clamp repeat_penalty to [0.5, 2.0]."""
    try:
        return max(0.5, min(2.0, float(raw)))
    except (TypeError, ValueError):
        return default

class ReaderWriterLock:
    """A writer-preferring, reentrant multiple-reader / single-writer lock.

    Semantics:

    * **Many readers, one writer.** Any number of threads may hold the read lock
      simultaneously; a write lock is exclusive against both readers and other
      writers.
    * **Writer preference (no writer starvation).** A reader that arrives while a
      write is *pending* (``_write_requests > 0``) blocks instead of barging in.
      Without this, a steady stream of readers could keep ``_readers > 0`` forever
      and a writer would never acquire — fatal for an index rebuild that must run.
    * **Reentrancy.** The thread currently holding the write lock may re-acquire it
      (counted by ``_writer_reentry``) and may also take the read lock as a no-op —
      it already has exclusive access, so nesting a read inside a write must not
      deadlock against itself.

    All state is guarded by a single :class:`threading.Condition` built over an
    ``RLock`` (re-entrant so the condition's own lock can be re-taken on the
    writer's nested calls). Every state mutation happens while holding that
    condition, and every blocking wait is a ``condition.wait()`` paired with a
    ``notify_all()`` on release, so there is no busy-spin and no lost wakeup.
    """
    def __init__(self):
        # One Condition over a re-entrant lock guards ALL fields below. RLock (not
        # a plain Lock) so a writer re-entering via acquire_write/acquire_read does
        # not deadlock on the condition's own mutex.
        self._condition = threading.Condition(threading.RLock())
        self._readers: int = 0                       # count of active read holders
        self._writer_thread: Optional[int] = None    # ident of the writing thread, or None
        self._writer_reentry: int = 0                # nesting depth of the active writer
        self._write_requests: int = 0                # writers waiting — drives reader-yielding

    def acquire_read(self):
        with self._condition:
            # The writer thread already has exclusive access; a nested read is a
            # no-op (and must NOT be counted, or release_read would underflow).
            if self._writer_thread == threading.get_ident():
                return
            # Yield to an active OR pending writer (writer preference): block while
            # a writer holds the lock or any writer is queued ahead of us.
            while self._writer_thread is not None or self._write_requests > 0:
                self._condition.wait()
            self._readers += 1

    def release_read(self):
        with self._condition:
            # Mirror of the nested-read short-circuit in acquire_read: the writer's
            # nested read never incremented _readers, so it must not decrement it.
            if self._writer_thread == threading.get_ident():
                return
            self._readers -= 1
            # Last reader out wakes any waiting writer (or writers).
            if self._readers == 0:
                self._condition.notify_all()

    def acquire_write(self):
        me = threading.get_ident()
        with self._condition:
            # Re-entrant write acquire: just deepen the nesting count.
            if self._writer_thread == me:
                self._writer_reentry += 1
                return
            # Announce intent BEFORE waiting so newly-arriving readers yield to us
            # (see acquire_read's loop). Balanced by the decrement below once we win.
            self._write_requests += 1
            while self._readers > 0 or self._writer_thread is not None:
                self._condition.wait()
            self._write_requests -= 1
            self._writer_thread = me
            self._writer_reentry = 1

    def release_write(self):
        with self._condition:
            # Only the owning thread may release — a mismatch is a real bug, so
            # raise rather than silently corrupt the writer state.
            if self._writer_thread != threading.get_ident():
                raise RuntimeError("release_write() called by non-owner")
            self._writer_reentry -= 1
            # Only the outermost release actually frees the lock and wakes waiters.
            if self._writer_reentry == 0:
                self._writer_thread = None
                self._condition.notify_all()

    @contextlib.contextmanager
    def read_lock(self):
        """Context-manager form of acquire/release_read (released even on exception)."""
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    @contextlib.contextmanager
    def write_lock(self):
        """Context-manager form of acquire/release_write (released even on exception)."""
        self.acquire_write()
        try:
            yield
        finally:
            self.release_write()

class RagOperationLock:
    """Binary admission lock for long-running LlamaIndex operations with per-acquisition TTL expiry.

    Each successful try_acquire() increments an internal epoch counter.  Callers
    must pass the epoch they received back to heartbeat() and release(), so that a
    zombie worker from a previous (expired) acquisition cannot prolong or cancel the
    lock held by a newer acquisition.

    The lock also expires *passively*: it is held only for ``ttl_seconds`` from the
    last acquire/heartbeat. A worker that crashes without releasing therefore does
    not wedge the subsystem forever — the next ``try_acquire`` after the TTL lapses
    simply takes over (and bumps the epoch, invalidating the dead worker's token).
    Long operations must call ``heartbeat(epoch)`` periodically to push the deadline
    out; ``is_held`` reflects this TTL, not merely "acquired".

    A single ``threading.Lock`` (``_meta_lock``) serialises every state read/write,
    so all the methods are short critical sections — this lock guards the *metadata*,
    it is not itself held for the duration of the long operation.
    """
    def __init__(self) -> None:
        self._meta_lock = threading.Lock()           # guards all fields below
        self._holder_since: Optional[float] = None   # monotonic time of last acquire/heartbeat; None = free
        self._ttl: float = 0.0                       # seconds the current holder is granted
        self._owner_thread: Optional[int] = None     # ident of acquirer (diagnostic only; see note)
        self._epoch: int = 0                         # bumped each acquire; the anti-zombie token

    def try_acquire(self, ttl_seconds: float) -> bool:
        """Acquire iff free or the prior holder's TTL has lapsed; bump the epoch.

        Returns ``True`` and grants the lock for *ttl_seconds* when it is free or
        the previous acquisition has expired; ``False`` when a live holder still
        owns it. The returned acquisition's token is :attr:`epoch` — capture it and
        pass it to ``heartbeat``/``release``.
        """
        with self._meta_lock:
            now = time.monotonic()
            # A still-live holder (acquired/heartbeated within its TTL) blocks us.
            # Once elapsed >= ttl the holder is considered dead and we steal it.
            if self._holder_since is not None:
                elapsed = now - self._holder_since
                if elapsed < self._ttl:
                    return False
            self._holder_since = now
            self._ttl = ttl_seconds
            self._owner_thread = threading.get_ident()
            # New epoch: any heartbeat/release carrying the OLD epoch now no-ops,
            # so a zombie worker from the stolen acquisition can't touch this one.
            self._epoch += 1
            return True

    @property
    def epoch(self) -> int:
        """The current acquisition's token (monotonic, bumped by each acquire)."""
        with self._meta_lock:
            return self._epoch

    def heartbeat(self, epoch: int) -> bool:
        """Refresh the TTL.  No-op if *epoch* does not match the current acquisition."""
        with self._meta_lock:
            # Reject a heartbeat from an expired/stolen acquisition — it must not
            # extend the deadline of whoever holds the lock now.
            if self._holder_since is None or self._epoch != epoch:
                return False
            self._holder_since = time.monotonic()
            return True

    def release(self, epoch: int = 0) -> None:
        """Release the lock.  If *epoch* is non-zero, only releases when it matches.

        Callers should pass their acquisition's epoch so a late-finishing zombie
        worker cannot release the lock out from under a newer holder. ``epoch=0``
        is an unconditional release (used only where no newer holder is possible).
        """
        with self._meta_lock:
            if epoch and self._epoch != epoch:
                return
            self._holder_since = None
            self._ttl = 0.0
            self._owner_thread = None

    def force_release(self) -> bool:
        """Unconditionally free the lock (admin/recovery escape hatch).

        Returns whether the lock was held. Ignores the epoch entirely, so use only
        for operator-driven recovery — normal callers use the epoch-checked
        :meth:`release`.
        """
        with self._meta_lock:
            was_held = self._holder_since is not None
            self._holder_since = None
            self._ttl = 0.0
            self._owner_thread = None
        return was_held

    @property
    def is_held(self) -> bool:
        """True only while a holder is within its TTL (passive expiry, not just acquired)."""
        with self._meta_lock:
            if self._holder_since is None:
                return False
            return (time.monotonic() - self._holder_since) < self._ttl
