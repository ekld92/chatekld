"""Disposable, disk-backed SimpleDocumentStore; persisted JSON stays authoritative.

The SQLite copy belongs to one loaded index, never survives as a reusable cache,
and is removed on close/GC. Loading parses one record at a time. Explicit bulk
APIs (docs/get_all/to_dict) still materialise their results, as upstream requires.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import weakref
from typing import Iterator, TextIO

from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.core.storage.kvstore import SimpleKVStore


class _JSONReader:
    """Incremental stdlib decoder: preserve floats, big integers and NaN semantics."""

    def __init__(self, stream: TextIO):
        self.stream = stream
        self.buffer = ""
        self.eof = False
        self.decoder = json.JSONDecoder()

    def _more(self) -> None:
        chunk = self.stream.read(65536)
        self.buffer += chunk
        self.eof = not chunk

    def peek(self) -> str:
        while True:
            self.buffer = self.buffer.lstrip(" \t\r\n")
            if self.buffer or self.eof:
                return self.buffer[:1]
            self._more()

    def require(self, char: str) -> None:
        if self.peek() != char:
            raise json.JSONDecodeError(f"Expected {char!r}", self.buffer, 0)
        self.buffer = self.buffer[1:]

    def value(self):
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer)
            except json.JSONDecodeError:
                if self.eof:
                    raise
                self._more()
            else:
                self.buffer = self.buffer[end:]
                return value

    def keys(self) -> Iterator[str]:
        self.require("{")
        if self.peek() == "}":
            self.require("}")
            return
        while True:
            key = self.value()
            if not isinstance(key, str):
                raise json.JSONDecodeError("Expected string key", self.buffer, 0)
            self.require(":")
            yield key
            if self.peek() == "}":
                self.require("}")
                return
            self.require(",")


@contextlib.contextmanager
def _init_lock(work_dir: Path):
    """Serialize startup sweep against tempdir + .owner.lock creation on work_dir."""
    fd = os.open(work_dir, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _sweep_orphaned_docstores(work_dir: Path) -> int:
    """Remove abandoned docstore-* scratch directories unowned by any live process."""
    swept = 0
    try:
        entries = list(work_dir.glob("docstore-*"))
    except OSError:
        return 0
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            lock_path = entry / ".owner.lock"
            try:
                fd = os.open(lock_path, flags, 0o600)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                os.close(fd)
                continue
            os.close(fd)
            shutil.rmtree(entry, ignore_errors=True)
            swept += 1
        except OSError:
            continue
    return swept


def _dispose(connection, directory, lock_fd_box: list[int | None]) -> None:
    try:
        if connection is not None:
            connection.close()
    finally:
        fd = lock_fd_box[0]
        if fd is not None:
            lock_fd_box[0] = None
            try:
                os.close(fd)
            except OSError:
                pass
        if directory is not None:
            directory.cleanup()


class DiskKVStore(SimpleKVStore):
    """SimpleKVStore API with bounded SQLite page cache and record-wise decoding.

    One connection is protected by an RLock (SSE workers cross thread boundaries).
    Higher-level read/modify/write operations retain the manager's existing locks.
    SQLite is scratch, not a recovery journal; only persist() writes authoritative
    JSON through the existing index publication path.
    """

    def __init__(self, work_dir: str):
        super().__init__()
        os.makedirs(work_dir, exist_ok=True)
        work_path = Path(work_dir)
        self._lock_fd_box: list[int | None] = [None]
        with _init_lock(work_path):
            _sweep_orphaned_docstores(work_path)
            self._tmpdir = tempfile.TemporaryDirectory(prefix="docstore-", dir=str(work_dir))
            try:
                lock_path = Path(self._tmpdir.name) / ".owner.lock"
                flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(lock_path, flags, 0o600)
                self._lock_fd_box[0] = fd
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                if self._lock_fd_box[0] is not None:
                    try:
                        os.close(self._lock_fd_box[0])
                    except OSError:
                        pass
                    self._lock_fd_box[0] = None
                self._tmpdir.cleanup()
                raise
        try:
            self._db = sqlite3.connect(
                os.path.join(self._tmpdir.name, "records.sqlite"),
                check_same_thread=False, isolation_level=None,
            )
        except BaseException:
            _dispose(None, self._tmpdir, self._lock_fd_box)
            raise
        try:
            # Memory rollback journal keeps failed scratch mutations atomic without
            # fsync overhead. The import transaction starts from an empty database;
            # subsequent writes use per-record transactions, not a corpus-sized undo.
            self._db.executescript("""
                PRAGMA journal_mode=MEMORY;
                PRAGMA synchronous=OFF;
                PRAGMA cache_size=-4096;
                PRAGMA temp_store=FILE;
                CREATE TABLE collections (name TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE records (collection TEXT, key TEXT, value TEXT,
                                      UNIQUE(collection, key));
                CREATE INDEX records_collection ON records(collection);
                CREATE TRIGGER inserted AFTER INSERT ON records BEGIN
                    UPDATE collections SET n=n+1 WHERE name=new.collection;
                END;
                CREATE TRIGGER deleted AFTER DELETE ON records BEGIN
                    UPDATE collections SET n=n-1 WHERE name=old.collection;
                END;
            """)
        except BaseException:
            _dispose(self._db, self._tmpdir, self._lock_fd_box)
            raise
        self._dispose = weakref.finalize(self, _dispose, self._db, self._tmpdir, self._lock_fd_box)
        self._lock = threading.RLock()

    @property
    def _lock_fd(self) -> int | None:
        return self._lock_fd_box[0]

    @_lock_fd.setter
    def _lock_fd(self, value: int | None) -> None:
        self._lock_fd_box[0] = value

    def _cleanup_tmpdir(self) -> None:
        with self._lock:
            self._dispose()
            self._lock_fd = None

    def close(self) -> None:
        with self._lock:
            self._dispose()
            self._lock_fd = None

    @classmethod
    def load(cls, path: str, work_dir: str) -> DiskKVStore:
        store = cls(work_dir)
        try:
            # Match the stdlib binary json.load encoding detection, including BOM.
            with open(path, "rb") as raw:
                encoding = json.detect_encoding(raw.read(4))
                raw.seek(0)
                import io
                with io.TextIOWrapper(raw, encoding=encoding) as text:
                    reader = _JSONReader(text)
                    store._db.execute("BEGIN")
                    for collection in reader.keys():
                        encoded = json.dumps(collection)
                        store._db.execute("DELETE FROM records WHERE collection=?", (encoded,))
                        store._db.execute("INSERT OR IGNORE INTO collections(name) VALUES (?)", (encoded,))
                        for key in reader.keys():
                            value = reader.value()
                            if not isinstance(value, dict):
                                raise json.JSONDecodeError("Expected record object", reader.buffer, 0)
                            store.put(key, value, collection)
                    if reader.peek():
                        raise json.JSONDecodeError("Extra data", reader.buffer, 0)
                    store._db.execute("COMMIT")
            return store
        except BaseException:
            store.close()
            raise

    def put(self, key: str, val: dict, collection: str = "data") -> None:
        # Encoding keys also preserves lone escaped surrogates accepted by json.
        collection, key = json.dumps(collection), json.dumps(key)
        value = json.dumps(val)
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO collections(name) VALUES (?)", (collection,))
            self._db.execute(
                "INSERT INTO records(collection,key,value) VALUES (?,?,?) "
                "ON CONFLICT(collection,key) DO UPDATE SET value=excluded.value",
                (collection, key, value),
            )

    def get(self, key: str, collection: str = "data") -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM records WHERE collection=? AND key=?",
                                   (json.dumps(collection), json.dumps(key))).fetchone()
            return json.loads(row[0]) if row else None

    def get_all(self, collection: str = "data") -> dict:
        with self._lock:
            return {json.loads(key): json.loads(value) for key, value in self._db.execute(
                "SELECT key,value FROM records WHERE collection=? ORDER BY rowid", (json.dumps(collection),))}

    def delete(self, key: str, collection: str = "data") -> bool:
        with self._lock:
            return bool(self._db.execute("DELETE FROM records WHERE collection=? AND key=?",
                                        (json.dumps(collection), json.dumps(key))).rowcount)

    def count(self, collection: str) -> int:
        with self._lock:
            row = self._db.execute("SELECT n FROM collections WHERE name=?",
                                   (json.dumps(collection),)).fetchone()
            return row[0] if row else 0

    def to_dict(self) -> dict:
        with self._lock:
            return {json.loads(row[0]): self.get_all(json.loads(row[0]))
                    for row in self._db.execute("SELECT name FROM collections ORDER BY rowid")}

    def persist(self, persist_path: str, fs=None) -> None:
        import fsspec
        fs = fs or fsspec.filesystem("file")
        parent = os.path.dirname(persist_path)
        if parent:
            fs.makedirs(parent, exist_ok=True)
        # Stream the same public JSON shape, including empty/custom collections.
        with self._lock, fs.open(persist_path, "w", encoding="utf-8") as target:
            target.write("{")
            for i, (collection,) in enumerate(self._db.execute("SELECT name FROM collections ORDER BY rowid")):
                target.write((", " if i else "") + collection + ": {")
                for j, (key, value) in enumerate(self._db.execute(
                        "SELECT key,value FROM records WHERE collection=? ORDER BY rowid", (collection,))):
                    target.write((", " if j else "") + key + ": " + value)
                target.write("}")
            target.write("}")


def load_disk_docstore(path: str, work_dir: str) -> SimpleDocumentStore:
    return SimpleDocumentStore(DiskKVStore.load(path, work_dir))
