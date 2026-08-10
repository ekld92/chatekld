import sqlite3
import logging
import os
import threading
from contextlib import contextmanager
from core.constants import BASE_DIR, DB_PATH

logger = logging.getLogger(__name__)
DB_LOCK = threading.Lock()

def init_db():
    """Initialise the SQLite database with WAL mode for concurrency.

    Raises on failure (2026-07-20 audit; fixed 2026-07-30). This used to log
    one line and return, so a failed ``CREATE TABLE`` — disk full, a locked or
    corrupt DB, an unwritable BASE_DIR — let the app boot with no ``uploads``
    table and surfaced much later as an opaque 500 on the user's first upload.
    Failing at launch names the real cause at the real moment; the call site
    (``app.create_app``) is the right place to decide that the app cannot run.
    """
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with DB_LOCK:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS uploads (
                        upload_id TEXT PRIMARY KEY,
                        filename TEXT,
                        extracted_text TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.commit()
            finally:
                conn.close()
        logger.info("Database initialised at %s", DB_PATH)
    except Exception as e:
        logger.error("Failed to initialise database: %s", e)
        raise

@contextmanager
def get_db_connection():
    """Yield a thread-safe sqlite connection and close it on exit.

    sqlite3.Connection's native ``__exit__`` only commits or rolls back; it
    leaves the connection open.  Wrapping in this context manager ensures every
    callsite CLOSES its connection when the block exits, so long-running Flask
    processes do not accumulate open handles. (There is no connection pool —
    each call opens and closes its own connection; 2026-07-20 audit N1.)

    UNWRITTEN INVARIANT made explicit (2026-07-22): "thread-safe" holds only
    because every caller wraps its use in ``with DB_LOCK`` — the connection
    itself is single-thread. All current call sites do; a new call site
    without the lock is a silent data race, not an error.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        # WAL is a PERSISTENT database property — set once in init_db(), not
        # re-issued per connection (the old per-connection PRAGMA was a
        # redundant write-lock acquisition on every open). busy_timeout and
        # synchronous are per-connection settings, so they DO belong here:
        # busy_timeout makes a concurrent writer wait instead of failing
        # immediately with SQLITE_BUSY, and NORMAL is the recommended
        # durability level under WAL (fsync at checkpoint, not every commit).
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
