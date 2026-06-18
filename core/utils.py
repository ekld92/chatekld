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
    """A multiple-reader, single-writer lock."""
    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._readers: int = 0
        self._writer_thread: Optional[int] = None
        self._writer_reentry: int = 0
        self._write_requests: int = 0

    def acquire_read(self):
        with self._condition:
            if self._writer_thread == threading.get_ident():
                return
            while self._writer_thread is not None or self._write_requests > 0:
                self._condition.wait()
            self._readers += 1

    def release_read(self):
        with self._condition:
            if self._writer_thread == threading.get_ident():
                return
            self._readers -= 1
            if self._readers == 0:
                self._condition.notify_all()

    def acquire_write(self):
        me = threading.get_ident()
        with self._condition:
            if self._writer_thread == me:
                self._writer_reentry += 1
                return
            self._write_requests += 1
            while self._readers > 0 or self._writer_thread is not None:
                self._condition.wait()
            self._write_requests -= 1
            self._writer_thread = me
            self._writer_reentry = 1

    def release_write(self):
        with self._condition:
            if self._writer_thread != threading.get_ident():
                raise RuntimeError("release_write() called by non-owner")
            self._writer_reentry -= 1
            if self._writer_reentry == 0:
                self._writer_thread = None
                self._condition.notify_all()

    @contextlib.contextmanager
    def read_lock(self):
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    @contextlib.contextmanager
    def write_lock(self):
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
    """
    def __init__(self) -> None:
        self._meta_lock = threading.Lock()
        self._holder_since: Optional[float] = None
        self._ttl: float = 0.0
        self._owner_thread: Optional[int] = None
        self._epoch: int = 0

    def try_acquire(self, ttl_seconds: float) -> bool:
        with self._meta_lock:
            now = time.monotonic()
            if self._holder_since is not None:
                elapsed = now - self._holder_since
                if elapsed < self._ttl:
                    return False
            self._holder_since = now
            self._ttl = ttl_seconds
            self._owner_thread = threading.get_ident()
            self._epoch += 1
            return True

    @property
    def epoch(self) -> int:
        with self._meta_lock:
            return self._epoch

    def heartbeat(self, epoch: int) -> bool:
        """Refresh the TTL.  No-op if *epoch* does not match the current acquisition."""
        with self._meta_lock:
            if self._holder_since is None or self._epoch != epoch:
                return False
            self._holder_since = time.monotonic()
            return True

    def release(self, epoch: int = 0) -> None:
        """Release the lock.  If *epoch* is non-zero, only releases when it matches."""
        with self._meta_lock:
            if epoch and self._epoch != epoch:
                return
            self._holder_since = None
            self._ttl = 0.0
            self._owner_thread = None

    def force_release(self) -> bool:
        with self._meta_lock:
            was_held = self._holder_since is not None
            self._holder_since = None
            self._ttl = 0.0
            self._owner_thread = None
        return was_held

    @property
    def is_held(self) -> bool:
        with self._meta_lock:
            if self._holder_since is None:
                return False
            return (time.monotonic() - self._holder_since) < self._ttl
