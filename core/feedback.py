import json
import logging
import os
import threading
from datetime import datetime, timezone
from core.constants import FEEDBACK_FILE

logger = logging.getLogger(__name__)
_feedback_lock = threading.Lock()

def save_feedback(**kwargs):
    """Append a feedback record (as JSON) to the feedback log file with timestamp."""
    try:
        import fcntl
    except ImportError:
        fcntl = None # Windows support fallback

    kwargs["timestamp"] = datetime.now(timezone.utc).isoformat()
    with _feedback_lock:
        try:
            with open(FEEDBACK_FILE, "a") as f:
                # Single non-blocking flock attempt.  _feedback_lock already
                # serialises all writers in this single-process app; flock
                # only defends against a second app instance sharing the
                # file.  The previous 2 s retry loop (40 × LOCK_NB + 50 ms
                # sleeps) stalled the request thread and then wrote anyway,
                # so trying once and proceeding has identical crash
                # consistency — single-line O_APPEND writes do not interleave
                # on local filesystems — without the worst-case stall.
                _locked = False
                if fcntl:
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        _locked = True
                    except BlockingIOError:
                        logger.warning("save_feedback: flock held elsewhere; appending anyway")
                try:
                    f.write(json.dumps(kwargs) + "\n")
                    f.flush()
                finally:
                    if _locked and fcntl:
                        fcntl.flock(f, fcntl.LOCK_UN)
        except OSError as e:
            logger.error(f"Failed to save feedback: {e}")

def load_feedback() -> list:
    """Return all feedback records, newest first."""
    if not os.path.exists(FEEDBACK_FILE):
        return []
    try:
        with open(FEEDBACK_FILE) as f:
            records = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.warning("Skipping malformed feedback line: %s", exc)
            return records[::-1]
    except OSError as exc:
        logger.warning("Could not load feedback from %s: %s", FEEDBACK_FILE, exc)
        return []
