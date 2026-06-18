import sqlite3
import logging
import os
import threading
from contextlib import contextmanager
from core.constants import BASE_DIR, DB_PATH

logger = logging.getLogger(__name__)
DB_LOCK = threading.Lock()

def init_db():
    """Initialise the SQLite database with WAL mode for concurrency."""
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

@contextmanager
def get_db_connection():
    """Yield a thread-safe sqlite connection and close it on exit.

    sqlite3.Connection's native ``__exit__`` only commits or rolls back; it
    leaves the connection open.  Wrapping in this context manager ensures
    every callsite returns the OS file handle to the pool when the block
    exits, so long-running Flask processes do not accumulate connections.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
