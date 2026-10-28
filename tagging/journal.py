"""Restore manifest + archive for the biblio Tagging write ops.

ONE per-vault manifest covers all three tagging write targets so the sub-view
has a single Restore surface: Obsidian note-frontmatter writes (``note_tag``),
PDF Finder-tag writes (``finder_tag``), and Zotero item-tag writes
(``zotero_tag``). Slice 2 implements ``note_tag``; the other two land in slices
4/5 (their prior state — a small tag list — is stored inline in the op, so they
need no file snapshot).

Modelled on ``refactor/journal.py`` (snapshot-before-write, conservative revert
that refuses to clobber later edits, capped note-history policy,
``log_vault_write``-audited) but self-contained under
``<tagging_archive_dir>/<vault_key>/`` — ALWAYS validated to resolve OUTSIDE the
vault (a snapshot written into the vault would be re-indexed). The active
journal is SQLite/WAL; legacy ``manifest.json`` is migrated transactionally with
a verified retained backup. Callers hold the obsidian operation lock for normal
writes and SQLite additionally serializes lost-lock/cross-process merges.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import fcntl

from core.constants import BASE_DIR
from core.utils import log_vault_write, write_bytes_atomic

logger = logging.getLogger(__name__)

_MANIFEST_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_DATABASE_NAME = "journal.sqlite3"
_BACKUP_NAME = "manifest.pre-sqlite-v1.json"
_SQLITE_SCHEMA_VERSION = 1
_SQLITE_BUSY_TIMEOUT_MS = 5_000
_MIGRATION_LOCK_TIMEOUT_S = 5.0

# Cap on evictable note ops. SQLite removes the former whole-history rewrite
# cost, but note snapshots would otherwise grow without a product retention
# policy. Applied Finder/Zotero rows remain exempt because their inline
# before-lists are the only restore mapping.
_MAX_OPS = 200

#: Op kinds whose prior state lives INLINE in the record with no snapshot file
#: behind it. These are the never-evicted population (see ``prune._protected``)
#: and therefore the only ones that grow without bound — which is exactly why
#: they are also the only ones :func:`compact` and the optional retention
#: policy below are allowed to drop. Note/vocab ops keep their file snapshots
#: and their existing ``_MAX_OPS`` window; nothing here touches them.
EXTERNAL_OP_KINDS = ("finder_tag", "zotero_tag", "finder_untag", "zotero_untag",
                     "zotero_attachment_trash",
                     # Linked-file attachment CREATED in Zotero (2026-09-06,
                     # harmonization Phase 4). Restore mapping = its key; the
                     # revert is a trash.
                     "zotero_attachment_create",
                     # Linked-file attachment RE-POINTED after a vault rename
                     # (2026-09-07). Restore mapping = `before_path`, inline.
                     "zotero_attachment_repath",
                     # Linked-file attachment RETITLED to its filename (annual
                     # cleanup T4, 2026-09-18). Restore mapping = `before_title`,
                     # inline.
                     "zotero_attachment_retitle",
                     # Note sync (2026-09-04): a note BODY written to Zotero, and a
                     # child note CREATED there. Restore mapping lives inline.
                     "zotero_note_write", "zotero_note_create",
                     # Record merge (annual cleanup T8, 2026-09-17): a child
                     # RE-PARENTED onto the kept item (restore mapping =
                     # `before_parent`, inline) and the retired PARENT item
                     # TRASHED (restore = un-trash; the key is the mapping).
                     "zotero_child_reparent", "zotero_item_trash",
                     # A TOP-LEVEL item CREATED from an orphan PDF (2026-09-11,
                     # zotcreate). Restore mapping = its key plus the key of the
                     # attachment created with it; the revert is a trash.
                     "zotero_item_create",
                     # Metadata FIELDS written by an approved bibliography campaign
                     # (phase 5, 2026-09-16). Restore mapping = `before_fields`
                     # (and any `unexpected_changes` Zotero normalised), inline.
                     "zotero_metadata_write")

#: Retention policies for :func:`compact` and the optional automatic pass.
#: ``age_days`` drops applied external ops older than N days; ``keep_newest``
#: keeps the N most recent and drops the rest.
RETENTION_POLICIES = ("age_days", "keep_newest")

#: Default page size for :func:`load_page`. The Restore UI renders one row per
#: op, and a library-scale journal holds five figures of them.
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000


class ScopeError(Exception):
    """A write/restore target escaped its allowed root (vault / archive)."""


# --- path roots ------------------------------------------------------------

def _vault_key(vault_root: Path) -> str:
    """Stable per-vault key (realpath-hashed) for the archive subdir."""
    real = os.path.realpath(str(vault_root))
    return hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]


def archive_dir(vault_root: Path, cfg: dict) -> Path:
    """Resolve the per-vault archive dir; raise ``ScopeError`` if inside the vault.

    ``tagging_archive_dir`` (absolute, ``~`` allowed) overrides the default
    ``BASE_DIR/tagging/archive``. Either way the real root is suffixed with the
    vault key so two vaults never collide. The archive must not live inside the
    vault and the vault must not live inside the archive (either direction risks
    re-indexing a snapshot or a restore recursion).
    """
    base = (cfg.get("tagging_archive_dir") or "").strip()
    root = Path(base).expanduser() if base else Path(BASE_DIR) / "tagging" / "archive"
    out = root / _vault_key(vault_root)
    real_out = os.path.realpath(out)
    real_vault = os.path.realpath(vault_root)
    if real_out == real_vault \
            or real_out.startswith(real_vault + os.sep) \
            or real_vault.startswith(real_out + os.sep):
        raise ScopeError("tagging archive directory must resolve outside the vault")
    return out


def manifest_path(vault_root: Path, cfg: dict) -> Path:
    """``<archive_dir>/journal.sqlite3`` — the active restore journal."""
    return archive_dir(vault_root, cfg) / _DATABASE_NAME


def _legacy_manifest_path(vault_root: Path, cfg: dict) -> Path:
    return archive_dir(vault_root, cfg) / _MANIFEST_NAME


def migration_backup_path(vault_root: Path, cfg: dict) -> Path:
    """Verified immutable JSON backup retained after a successful migration."""
    return archive_dir(vault_root, cfg) / _BACKUP_NAME


def assert_under(abs_path: str | Path, root: str | Path) -> str:
    """Return ``realpath(abs_path)`` after confirming it is *root* or under it."""
    real = os.path.realpath(abs_path)
    real_root = os.path.realpath(root)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise ScopeError(f"path escapes its allowed root: {abs_path}")
    return real


# --- manifest io -----------------------------------------------------------

class JournalUnreadableError(RuntimeError):
    """The manifest EXISTS but could not be read/parsed — refuse to write.

    Absent-vs-unreadable matters (2026-07-20 audit): treating a transient
    ``OSError`` (archive dir on an unmounted external/iCloud volume — the
    user-configurable ``tagging_archive_dir`` points at exactly such places)
    or corrupt JSON as "fresh empty manifest" meant the very next tag write
    appended one op and atomically OVERWROTE the real file — destroying every
    prior op, including the Finder/Zotero before-lists that are the only
    restore mapping. Callers surface this as 503 and perform no write.
    """


class JournalTransientReadError(JournalUnreadableError):
    """The manifest could not be READ at all (``OSError``) — content unknown.

    Distinct from corrupt content (2026-08-18): on an unmounted volume the
    disk copy may be perfectly intact, so a fallback that overwrites on this
    error would be the exact clobber the refusal exists to prevent.
    :func:`save_merged` refuses on this and overwrites only on genuinely
    corrupt content. Subclasses :class:`JournalUnreadableError`, so every
    existing 503 handler catches it unchanged.
    """


class _TrackedOp(dict):
    """Dictionary that records whether an operation changed since ``load``."""

    def __init__(self, value: dict, *, dirty: bool, on_dirty=None) -> None:
        super().__init__(value)
        self.dirty = dirty
        self._on_dirty = on_dirty

    def _mark_dirty(self) -> None:
        self.dirty = True
        if self._on_dirty is not None:
            self._on_dirty(self)

    def __setitem__(self, key, value) -> None:
        if self.get(key, object()) != value:
            self._mark_dirty()
        super().__setitem__(key, value)

    def __delitem__(self, key) -> None:
        self._mark_dirty()
        super().__delitem__(key)

    def update(self, *args, **kwargs) -> None:
        incoming = dict(*args, **kwargs)
        if any(self.get(key, object()) != value for key, value in incoming.items()):
            self._mark_dirty()
        super().update(incoming)


class _TrackedOps(list):
    def __init__(self, values=(), *, dirty: bool, baseline_ids=(), scoped: bool = False) -> None:
        super().__init__()
        self.scoped = bool(scoped)
        self.baseline_ids = baseline_ids if isinstance(baseline_ids, set) else set(baseline_ids)
        self.current_ids: set[str] = set()
        self.by_id: dict[str, dict] = {}
        self.dirty_items: dict[int, dict] = {}
        # Value snapshots, keyed by op_id, of members that are NOT _TrackedOp.
        # See `pending_dirty`: a plain dict has no __setitem__ hook, so its
        # dirtiness is decided by comparison instead of by event. Keyed by
        # op_id rather than id() because a freed dict's id() can be reused by a
        # later op, which would make a genuinely new row compare "clean".
        self.plain_baseline: dict[str, dict] = {}
        for value in values:
            op_id = value.get("op_id")
            is_dirty = (
                dirty
                or (isinstance(value, _TrackedOp) and value.dirty)
                or (isinstance(op_id, str) and op_id not in self.baseline_ids)
            )
            tracked = _TrackedOp(
                dict(value), dirty=is_dirty, on_dirty=self._track
            )
            super().append(tracked)
            op_id = tracked.get("op_id")
            if isinstance(op_id, str):
                self.current_ids.add(op_id)
                self.by_id[op_id] = tracked
            if is_dirty:
                self._track(tracked)
        self.removed_ids = self.baseline_ids - self.current_ids
        if self.scoped and self.removed_ids:
            raise ValueError("scoped manifest cannot remove operations")

    def _track(self, value: dict) -> None:
        self.dirty_items[id(value)] = value

    def append(self, value) -> None:
        # Preserve identity for newly-created operation dicts: several write
        # paths append the record before the filesystem mutation, then flip the
        # same local object to ``failed`` if that mutation raises. Copying here
        # would strand an incorrect applied record.
        #
        # Identity alone is not enough, and assuming it was is what made those
        # failure flips vanish: the object must ALSO stay dirty-trackable across
        # the intervening save, and it must survive `prune` without being
        # rewrapped into a copy. `pending_dirty` and `retain` are the two halves
        # that make this comment true; change none of the three in isolation.
        op_id = value.get("op_id") if isinstance(value, dict) else None
        if isinstance(op_id, str) and op_id in self.current_ids:
            raise JournalUnreadableError(f"duplicate tagging operation id: {op_id}")
        super().append(value)
        if isinstance(op_id, str):
            self.current_ids.add(op_id)
            self.by_id[op_id] = value
        if isinstance(value, dict):
            self._track(value)

    def extend(self, values) -> None:
        for value in values:
            self.append(value)

    def _reindex(self) -> None:
        """Rebuild the id/dirty indexes from the current membership.

        Dropped members must leave ``dirty_items`` as well: ``save`` writes
        removals as tombstones first and then REFUSES any dirty id that was
        just tombstoned, so a stale dirty entry for an evicted op would turn an
        ordinary prune-then-save into a hard "resurrect removed operation"
        error.
        """
        self.current_ids = {
            item.get("op_id") for item in self
            if isinstance(item.get("op_id"), str)
        }
        self.by_id = {
            item["op_id"]: item for item in self
            if isinstance(item.get("op_id"), str)
        }
        self.removed_ids = self.baseline_ids - self.current_ids
        if self.scoped and self.removed_ids:
            raise ValueError("scoped manifest cannot remove operations")
        live = {id(item) for item in self}
        self.dirty_items = {
            key: value for key, value in self.dirty_items.items() if key in live
        }
        self.plain_baseline = {
            key: value for key, value in self.plain_baseline.items()
            if key in self.current_ids
        }

    def retain(self, kept: list) -> None:
        """Filter in place, PRESERVING element identity (used by ``prune``).

        ``prune`` used to publish its survivors with ``manifest["ops"] = kept``,
        which routes through ``_JournalManifest.__setitem__`` and rewraps every
        element as a fresh ``_TrackedOp(dict(value))`` — a copy. The writer that
        appended the op a few lines earlier still held the original, so its
        later ``op["state"] = "failed"`` mutated an object no longer in the
        manifest: the flip was lost in memory and on disk, and ``find_op`` began
        returning a different object than the caller was mutating. Splicing the
        very same objects back keeps every outstanding reference live.
        """
        if self.scoped:
            raise ValueError("scoped manifest cannot remove operations")
        list.clear(self)
        list.extend(self, kept)
        self._reindex()

    def __delitem__(self, key) -> None:
        if self.scoped:
            raise ValueError("scoped manifest cannot remove operations")
        super().__delitem__(key)
        self._reindex()

    def remove(self, value) -> None:
        if self.scoped:
            raise ValueError("scoped manifest cannot remove operations")
        super().remove(value)
        self._reindex()

    def pop(self, *args):
        if self.scoped:
            raise ValueError("scoped manifest cannot remove operations")
        val = super().pop(*args)
        self._reindex()
        return val

    def clear(self) -> None:
        if self.scoped:
            raise ValueError("scoped manifest cannot remove operations")
        super().clear()
        self._reindex()

    def pending_dirty(self) -> list[dict]:
        """Ops whose current value differs from the row last written.

        ``_TrackedOp`` members (everything ``load`` returned) report through
        ``__setitem__`` — event tracking, which is what keeps a save O(changed)
        over a long history. Members appended as plain dicts by a writer have no
        such hook, so they are compared by value against the snapshot taken at
        the previous save. That population is exactly the ops THIS request
        appended — rows the request is writing anyway — so the incremental-write
        property is preserved, and comparison additionally catches nested
        mutations that an attribute hook cannot see.
        """
        out: dict[int, dict] = dict(self.dirty_items)
        for item in self:
            if isinstance(item, _TrackedOp) or id(item) in out:
                continue
            op_id = item.get("op_id") if isinstance(item, dict) else None
            snapshot = (self.plain_baseline.get(op_id)
                        if isinstance(op_id, str) else None)
            if snapshot is None or snapshot != item:
                out[id(item)] = item
        return list(out.values())

    def mark_saved(self, baseline_ids: set[str]) -> None:
        """Adopt the just-committed generation as the new clean baseline."""
        self.baseline_ids = baseline_ids
        self.removed_ids.clear()
        self.dirty_items.clear()
        plain: dict[str, dict] = {}
        for item in self:
            if isinstance(item, _TrackedOp):
                item.dirty = False
                continue
            op_id = item.get("op_id") if isinstance(item, dict) else None
            if isinstance(op_id, str):
                plain[op_id] = dict(item)
        self.plain_baseline = plain

    def __setitem__(self, key, value) -> None:
        if isinstance(key, slice):
            value = list(value)
        super().__setitem__(key, value)
        if isinstance(key, slice):
            for item in value:
                if isinstance(item, dict):
                    self._track(item)
        elif isinstance(value, dict):
            self._track(value)
        self._reindex()


class _JournalManifest(dict):
    """Compatibility mapping with dirty tracking for incremental SQL writes."""

    def __init__(self, *, vault_key: str, ops, baseline_ids=(), scoped: bool = False) -> None:
        super().__init__(version=_MANIFEST_VERSION, vault_key=vault_key)
        self.baseline_ids = set(baseline_ids)
        self.scoped = bool(scoped)
        dict.__setitem__(
            self,
            "ops",
            _TrackedOps(ops, dirty=False, baseline_ids=self.baseline_ids, scoped=self.scoped),
        )

    def __setitem__(self, key, value) -> None:
        if key == "ops" and not isinstance(value, _TrackedOps):
            value = _TrackedOps(
                value, dirty=False, baseline_ids=self.baseline_ids, scoped=getattr(self, "scoped", False)
            )
        super().__setitem__(key, value)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_backup_no_replace(path: Path, raw: bytes) -> None:
    """Create and durably verify the migration backup without replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            if path.read_bytes() != raw:
                raise JournalUnreadableError(
                    "tagging migration backup exists with different content; "
                    "refusing to replace it"
                )
        except OSError as exc:
            raise JournalTransientReadError(
                f"tagging migration backup could not be verified ({exc})"
            ) from exc
        return
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_dir(path.parent)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise


def _connect(path: Path, *, create: bool) -> sqlite3.Connection:
    if not create and not path.exists():
        raise FileNotFoundError(path)
    conn = None
    try:
        conn = sqlite3.connect(
            path, timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000, isolation_level=None
        )
        conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except sqlite3.OperationalError as exc:
        if conn is not None:
            conn.close()
        raise JournalTransientReadError(
            f"tagging journal storage is unavailable ({exc}); refusing to write"
        ) from exc
    except sqlite3.DatabaseError as exc:
        if conn is not None:
            conn.close()
        raise JournalUnreadableError(
            f"tagging journal database is corrupt ({path}); refusing to write"
        ) from exc


def _init_schema(conn: sqlite3.Connection, vault_key: str) -> None:
    # Individual statements deliberately avoid sqlite3.executescript(), which
    # issues an implicit COMMIT and would tear the caller's migration/save
    # transaction into partially durable rows.
    for statement in (
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS operations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            op_id TEXT NOT NULL UNIQUE,
            kind TEXT,
            state TEXT,
            batch_id TEXT,
            snapshot_rel TEXT,
            ts TEXT,
            payload TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS operations_state_idx ON operations(state)",
        "CREATE INDEX IF NOT EXISTS operations_batch_idx ON operations(batch_id, seq)",
        "CREATE INDEX IF NOT EXISTS operations_kind_state_idx ON operations(kind, state)",
        """
        CREATE TABLE IF NOT EXISTS operation_tombstones (
            op_id TEXT PRIMARY KEY,
            removed_at TEXT NOT NULL
        )
        """,
    ):
        conn.execute(statement)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(_SQLITE_SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('vault_key', ?)",
        (vault_key,),
    )


def _validate_legacy(raw: bytes, path: Path, vault_root: Path) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise JournalUnreadableError(
            "tagging journal is corrupt; refusing migration so the existing "
            f"restore history is not overwritten ({path})"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("ops"), list):
        raise JournalUnreadableError(
            "tagging journal has an unexpected shape; refusing migration so "
            f"the existing restore history is not overwritten ({path})"
        )
    seen: set[str] = set()
    for op in data["ops"]:
        op_id = op.get("op_id") if isinstance(op, dict) else None
        if not isinstance(op_id, str) or not op_id or op_id in seen:
            raise JournalUnreadableError(
                "tagging journal contains a missing or duplicate operation id; "
                "refusing migration"
            )
        seen.add(op_id)
    data.setdefault("version", _MANIFEST_VERSION)
    data.setdefault("vault_key", _vault_key(vault_root))
    return data


def _insert_op(conn: sqlite3.Connection, op: dict) -> None:
    payload = json.dumps(dict(op), ensure_ascii=False, separators=(",", ":"))
    conn.execute(
        """INSERT INTO operations(
               op_id, kind, state, batch_id, snapshot_rel, ts, payload
           ) VALUES(?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(op_id) DO UPDATE SET
               kind=excluded.kind, state=excluded.state,
               batch_id=excluded.batch_id, snapshot_rel=excluded.snapshot_rel,
               ts=excluded.ts, payload=excluded.payload""",
        (
            op.get("op_id"), op.get("kind"), op.get("state"),
            op.get("batch_id"), op.get("snapshot_rel"), op.get("ts"), payload,
        ),
    )


def _migrate_legacy(
    vault_root: Path, cfg: dict, raw: bytes, data: dict, legacy_stat: os.stat_result
) -> None:
    """Publish a verified SQLite generation while leaving JSON untouched.

    Safety/concurrency proof: callers normally hold the cross-feature vault
    lease, while O_EXCL backup creation and hard-link publication also make a
    concurrent first opener no-replace safe. The complete legacy payload is
    inserted in one FULL-synchronous transaction and checked before publication;
    any cancellation/process failure leaves either no database or a complete
    one, with the original JSON plus its verified backup intact. WAL begins only
    after publication. The schema stores the whole operation JSON, preserving
    every old/unknown field and all restore mappings without a format rewrite.
    """
    arch = archive_dir(vault_root, cfg)
    arch.mkdir(parents=True, exist_ok=True)
    _write_backup_no_replace(migration_backup_path(vault_root, cfg), raw)
    target = manifest_path(vault_root, cfg)
    temp = arch / f".{_DATABASE_NAME}.migrate-{os.getpid()}-{time.time_ns()}"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        conn = sqlite3.connect(temp, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            _init_schema(conn, data["vault_key"])
            for op in data["ops"]:
                _insert_op(conn, op)
            meta = {
                "legacy_sha256": hashlib.sha256(raw).hexdigest(),
                "legacy_size": str(legacy_stat.st_size),
                "legacy_mtime_ns": str(legacy_stat.st_mtime_ns),
            }
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", meta.items()
            )
            conn.execute("COMMIT")
            if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise JournalUnreadableError("tagging journal migration integrity check failed")
            count = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
            if count != len(data["ops"]):
                raise JournalUnreadableError("tagging journal migration row count mismatch")
        finally:
            conn.close()
        # No fsync here: the connection above committed under
        # `synchronous=FULL` and was closed, so the database bytes are already
        # durable. The previous fsync ran on a READ-ONLY descriptor, which POSIX
        # permits an implementation to reject with EBADF.
        current_stat = _legacy_manifest_path(vault_root, cfg).stat()
        if (
            current_stat.st_size != legacy_stat.st_size
            or current_stat.st_mtime_ns != legacy_stat.st_mtime_ns
        ):
            raise JournalTransientReadError(
                "legacy tagging journal changed during migration; retrying is safe"
            )
        try:
            os.link(temp, target)
        except FileExistsError:
            pass
        _fsync_dir(arch)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def _migration_lock(arch: Path):
    """Cross-process first-open serialization with a bounded wait."""
    arch.mkdir(parents=True, exist_ok=True)
    lock_path = arch / ".journal-migration.lock"
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise JournalTransientReadError(
            f"tagging migration lock is unavailable ({exc})"
        ) from exc
    deadline = time.monotonic() + _MIGRATION_LOCK_TIMEOUT_S
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise JournalTransientReadError(
                        "timed out waiting for tagging journal migration"
                    )
                time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _ensure_database_unlocked(vault_root: Path, cfg: dict) -> Path:
    db = manifest_path(vault_root, cfg)
    if db.exists():
        return db
    legacy = _legacy_manifest_path(vault_root, cfg)
    try:
        before = legacy.stat()
        raw = legacy.read_bytes()
        after = legacy.stat()
    except FileNotFoundError:
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(db, create=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _init_schema(conn, _vault_key(vault_root))
            conn.execute("COMMIT")
        finally:
            conn.close()
        _fsync_dir(db.parent)
        return db
    except OSError as exc:
        raise JournalTransientReadError(
            f"tagging journal could not be read ({exc}); refusing to migrate"
        ) from exc
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise JournalTransientReadError(
            "legacy tagging journal changed while being read; retrying is safe"
        )
    data = _validate_legacy(raw, legacy, vault_root)
    _migrate_legacy(vault_root, cfg, raw, data, after)
    return db


def _ensure_database(vault_root: Path, cfg: dict) -> Path:
    db = manifest_path(vault_root, cfg)
    if db.exists():
        return db
    with _migration_lock(archive_dir(vault_root, cfg)):
        # Another process may have completed publication while we waited.
        return _ensure_database_unlocked(vault_root, cfg)


def _verify_legacy_not_diverged(
    conn: sqlite3.Connection, vault_root: Path, cfg: dict
) -> None:
    meta = dict(conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'legacy_%'"
    ))
    if not meta:
        return
    legacy = _legacy_manifest_path(vault_root, cfg)
    try:
        stat = legacy.stat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise JournalTransientReadError(
            f"legacy tagging journal could not be checked ({exc})"
        ) from exc
    if (
        str(stat.st_size) == meta.get("legacy_size")
        and str(stat.st_mtime_ns) == meta.get("legacy_mtime_ns")
    ):
        return
    try:
        digest = hashlib.sha256(legacy.read_bytes()).hexdigest()
    except OSError as exc:
        raise JournalTransientReadError(
            f"legacy tagging journal could not be checked ({exc})"
        ) from exc
    if digest != meta.get("legacy_sha256"):
        raise JournalUnreadableError(
            "the legacy JSON tagging journal changed after SQLite migration; "
            "refusing to choose between divergent restore histories"
        )


class _SchemaMissing(Exception):
    """Internal: the database file exists but carries no journal schema."""


def _repair_schema(db: Path, vault_key: str) -> None:
    """Create the schema for a database file that exists but has no tables.

    Only reachable when a previous process died between creating the file and
    committing its schema. Kept OFF the read path (see :func:`load`) so the
    normal case never opens a write transaction.
    """
    conn = _connect(db, create=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _init_schema(conn, vault_key)
        conn.execute("COMMIT")
    finally:
        conn.close()


def _open_for_read(vault_root: Path, cfg: dict) -> Path | None:
    """Resolve the database to read, migrating a legacy generation if needed.

    Returns ``None`` when there is genuinely nothing yet — neither a database
    nor a legacy ``manifest.json``. Shared by :func:`load`, :func:`stats` and
    :func:`load_page` so every read surface agrees about what "empty" means.

    The migration is a WRITE, and doing it from a GET is deliberate: it is what
    ``load`` has always done, and ``GET /api/tagging/manifest`` went through
    ``load`` until the paging rewrite. Skipping it made the Restore panel, the
    journal-size header and ``papermind status`` all report an empty history to
    any install that had not yet performed a tagging write since the SQLite
    migration landed — a full 11,637-op history rendered as "nothing to
    restore". Publication is O_EXCL/hard-link no-replace, so a concurrent first
    opener is safe without the operation lock.
    """
    db = manifest_path(vault_root, cfg)
    if db.exists():
        return db
    if not _legacy_manifest_path(vault_root, cfg).exists():
        return None
    return _ensure_database(vault_root, cfg)


def load(vault_root: Path, cfg: dict) -> dict:
    """Load ordered operations, migrating a legacy JSON generation once.

    Read-only by construction. This used to call :func:`_init_schema` on every
    load — five ``CREATE TABLE IF NOT EXISTS`` plus two ``INSERT OR IGNORE``
    under ``synchronous=FULL`` — which takes a RESERVED lock and therefore made
    a plain read fail with SQLITE_BUSY whenever any writer held
    ``BEGIN IMMEDIATE``. That defeats the entire point of WAL, where readers
    never block on a writer, and surfaced to the user as a 503 on the Restore
    manifest during any harmonize/retire batch. Schema creation belongs to the
    three paths that already own a write transaction: first-open, migration,
    and save; a database file with no tables at all is repaired once, lazily.
    """
    db = _open_for_read(vault_root, cfg)
    if db is None:
        return _JournalManifest(vault_key=_vault_key(vault_root), ops=[])
    try:
        return _load_from(db, vault_root, cfg)
    except _SchemaMissing:
        _repair_schema(db, _vault_key(vault_root))
    try:
        return _load_from(db, vault_root, cfg)
    except _SchemaMissing as exc:
        # Repair ran and the tables are still absent: the file is not a journal
        # we can use. Fail closed in the vocabulary callers already handle
        # rather than letting an internal marker escape as a 500.
        raise JournalUnreadableError(
            f"tagging journal schema could not be created ({db})") from exc


def _load_from(db: Path, vault_root: Path, cfg: dict) -> dict:
    conn = _connect(db, create=False)
    try:
        _verify_legacy_not_diverged(conn, vault_root, cfg)
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        if meta.get("schema_version") != str(_SQLITE_SCHEMA_VERSION):
            raise JournalUnreadableError("unsupported tagging journal schema version")
        expected_key = _vault_key(vault_root)
        if meta.get("vault_key") != expected_key:
            raise JournalUnreadableError("tagging journal belongs to a different vault")
        rows = conn.execute("SELECT op_id, payload FROM operations ORDER BY seq").fetchall()
        try:
            ops = [json.loads(payload) for _op_id, payload in rows]
        except (TypeError, ValueError) as exc:
            raise JournalUnreadableError("tagging journal contains corrupt operation JSON") from exc
        if any(op.get("op_id") != op_id for (op_id, _), op in zip(rows, ops)):
            raise JournalUnreadableError("tagging journal operation identity mismatch")
        return _JournalManifest(
            vault_key=expected_key, ops=ops, baseline_ids=(row[0] for row in rows)
        )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            raise _SchemaMissing(str(exc)) from exc
        raise JournalTransientReadError(
            f"tagging journal could not be read ({exc}); no write was attempted"
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise JournalUnreadableError(f"tagging journal database is corrupt ({db})") from exc
    finally:
        conn.close()


def load_for_ops(
    vault_root: Path, cfg: dict, op_ids: Iterable[str] = ()
) -> _JournalManifest:
    """Load only specified operations into a scoped manifest for targeted writes.

    Refuses prune/compact and row removals. Preserves legacy migration, divergence
    checks, and vault-key validation.
    """
    db = _open_for_read(vault_root, cfg)
    if db is None:
        return _JournalManifest(
            vault_key=_vault_key(vault_root), ops=[], scoped=True
        )
    try:
        return _load_for_ops_from(db, vault_root, cfg, op_ids)
    except _SchemaMissing:
        _repair_schema(db, _vault_key(vault_root))
    try:
        return _load_for_ops_from(db, vault_root, cfg, op_ids)
    except _SchemaMissing as exc:
        raise JournalUnreadableError(
            f"tagging journal schema could not be created ({db})"
        ) from exc


def _load_for_ops_from(
    db: Path, vault_root: Path, cfg: dict, op_ids: Iterable[str]
) -> _JournalManifest:
    conn = _connect(db, create=False)
    try:
        _verify_legacy_not_diverged(conn, vault_root, cfg)
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        if meta.get("schema_version") != str(_SQLITE_SCHEMA_VERSION):
            raise JournalUnreadableError("unsupported tagging journal schema version")
        expected_key = _vault_key(vault_root)
        if meta.get("vault_key") != expected_key:
            raise JournalUnreadableError("tagging journal belongs to a different vault")
        target_ids = list(dict.fromkeys(
            op_id for op_id in op_ids if isinstance(op_id, str) and op_id
        ))
        rows: list[tuple[str, str]] = []
        if target_ids:
            for offset in range(0, len(target_ids), 500):
                chunk = target_ids[offset:offset + 500]
                if not chunk:
                    continue
                chunk_rows = conn.execute(
                    "SELECT op_id, payload FROM operations WHERE op_id IN "
                    f"({','.join('?' for _ in chunk)}) ORDER BY seq",
                    chunk,
                ).fetchall()
                rows.extend(chunk_rows)
        try:
            ops = [json.loads(payload) for _op_id, payload in rows]
        except (TypeError, ValueError) as exc:
            raise JournalUnreadableError(
                "tagging journal contains corrupt operation JSON"
            ) from exc
        if any(op.get("op_id") != op_id for (op_id, _), op in zip(rows, ops)):
            raise JournalUnreadableError(
                "tagging journal operation identity mismatch"
            )
        return _JournalManifest(
            vault_key=expected_key,
            ops=ops,
            baseline_ids=(row[0] for row in rows),
            scoped=True,
        )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            raise _SchemaMissing(str(exc)) from exc
        raise JournalTransientReadError(
            f"tagging journal could not be read ({exc}); no write was attempted"
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise JournalUnreadableError(
            f"tagging journal database is corrupt ({db})"
        ) from exc
    finally:
        conn.close()


def save(vault_root: Path, cfg: dict, manifest: dict) -> None:
    """Atomically apply only changed rows in one bounded SQLite transaction.

    State/lock invariant: the caller's vault operation lease remains the sole
    logical writer; ``BEGIN IMMEDIATE`` additionally serializes a lost-lease or
    cross-process writer. Dirty request rows and explicit removals commit as one
    FULL-synchronous WAL transaction, so a crash exposes the complete old or
    new generation, never a partial restore map. Cancellation before commit
    rolls back; retrying is idempotent through the unique ``op_id`` UPSERT.
    Whole legacy payloads remain JSON in each row, preserving unknown fields and
    migration compatibility. Work and WAL growth are bounded by changed rows,
    making normal append/state-update latency independent of retained history.
    """
    db = _ensure_database(vault_root, cfg)
    ops = manifest.get("ops", [])
    if isinstance(manifest, _JournalManifest) and isinstance(ops, _TrackedOps):
        tracked = True
        incoming_ids = ops.current_ids
        baseline_ids = manifest.baseline_ids
        dirty_ops = ops.pending_dirty()
    else:
        tracked = False
        ids = [op.get("op_id") for op in ops]
        if any(not isinstance(op_id, str) or not op_id for op_id in ids):
            raise JournalUnreadableError("tagging journal operation is missing op_id")
        if len(ids) != len(set(ids)):
            raise JournalUnreadableError("tagging journal contains duplicate op_id")
        incoming_ids = set(ids)
        baseline_ids = set()
        dirty_ops = list(ops)
    conn = _connect(db, create=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _init_schema(conn, _vault_key(vault_root))
        _verify_legacy_not_diverged(conn, vault_root, cfg)
        # A manifest returned by load() carries the exact baseline id set, so a
        # normal append/update need not scan history in SQL. A plain caller-made
        # dict retains legacy whole-generation semantics and pays the full scan.
        # Unknown rows committed by a new lease owner are deliberately not in
        # our baseline and therefore cannot be deleted by a stale writer.
        if not tracked:
            baseline_ids = {
                row[0] for row in conn.execute("SELECT op_id FROM operations")
            }
        removed = ops.removed_ids if tracked else baseline_ids - incoming_ids
        if getattr(manifest, "scoped", False) and removed:
            raise ValueError("scoped manifest cannot remove operations")
        if removed:
            conn.executemany(
                "INSERT OR IGNORE INTO operation_tombstones(op_id, removed_at) VALUES(?, ?)",
                ((op_id, now_iso()) for op_id in removed),
            )
            conn.executemany(
                "DELETE FROM operations WHERE op_id=?",
                ((op_id,) for op_id in removed),
            )
        dirty_ids = [op.get("op_id") for op in dirty_ops]
        if any(not isinstance(op_id, str) or not op_id for op_id in dirty_ids):
            raise JournalUnreadableError("tagging journal operation is missing op_id")
        # Query only changed ids in bounded chunks. SQLite builds have
        # differing variable limits, while a batch can legitimately contain
        # thousands of new operations.
        tombstoned: set[str] = set()
        for offset in range(0, len(dirty_ids), 500):
            chunk = dirty_ids[offset:offset + 500]
            if not chunk:
                continue
            tombstoned.update(
                row[0] for row in conn.execute(
                    "SELECT op_id FROM operation_tombstones WHERE op_id IN "
                    f"({','.join('?' for _ in chunk)})",
                    chunk,
                )
            )
        if tombstoned:
            raise JournalUnreadableError(
                "tagging journal save attempted to resurrect removed operation(s): "
                + ", ".join(sorted(tombstoned)[:5])
            )
        for op in dirty_ops:
            _insert_op(conn, op)
        conn.execute("COMMIT")
    except (JournalUnreadableError, JournalTransientReadError):
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    except sqlite3.OperationalError as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise JournalTransientReadError(
            f"tagging journal could not be written ({exc})"
        ) from exc
    except sqlite3.DatabaseError as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise JournalUnreadableError("tagging journal database write failed") from exc
    finally:
        conn.close()
    if isinstance(manifest, _JournalManifest):
        manifest.baseline_ids.difference_update(removed)
        manifest.baseline_ids.update(
            op_id for op_id in dirty_ids if isinstance(op_id, str)
        )
        if isinstance(ops, _TrackedOps):
            # Adopting the committed rows as the clean baseline is what makes a
            # LATER mutation of an op appended in this request detectable; the
            # previous code cleared the dirty set without recording any value to
            # compare against, so every post-save change was silently dropped.
            ops.mark_saved(manifest.baseline_ids)


def save_merged(vault_root: Path, cfg: dict, manifest: dict,
                prefer_ours: set | frozenset | tuple = ()) -> None:
    """Persist *manifest*'s ops after a LOST op-lock, without clobbering.

    A batch route (retire/harmonize) that sees ``heartbeat() == False`` may no
    longer own the manifest: another tagging writer could have acquired the
    lapsed lock and saved ops of its own, and a plain :func:`save` of our
    in-memory object would silently drop theirs. But NOT saving is worse in
    the other direction — under ``defer_save`` our pending op records describe
    vault/xattr writes that already LANDED, and losing the records destroys
    their undo (Finder before-lists have no snapshot file behind them).

    So: reload the manifest from disk, append (in order) our ops that are
    missing by ``op_id``, and save the merged copy. Disk wins for ops present
    in both — EXCEPT ids listed in *prefer_ours*, where our in-memory copy
    replaces the disk one in place: the batch-restore path flips ops to
    ``reverted`` in memory, and disk-wins would silently discard those flips
    (leaving reverted stores marked ``applied``, i.e. double-revertible).

    AP-10 performs this merge inside one ``BEGIN IMMEDIATE`` transaction rather
    than load→whole-save. Concurrent appends therefore serialize without a
    clobber window. Durable tombstones suppress an in-memory stale op that a new
    lease owner already restored/pruned, closing the former resurrection path.
    Every unreadable database remains fail-closed; neither corruption nor an
    unmounted volume authorizes replacement.
    """
    db = _ensure_database(vault_root, cfg)
    prefer = set(prefer_ours or ())
    conn = _connect(db, create=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _init_schema(conn, _vault_key(vault_root))
        _verify_legacy_not_diverged(conn, vault_root, cfg)
        tombstones = {
            row[0] for row in conn.execute("SELECT op_id FROM operation_tombstones")
        }
        present = {
            row[0] for row in conn.execute("SELECT op_id FROM operations")
        }
        for op in manifest.get("ops", []):
            op_id = op.get("op_id")
            if not isinstance(op_id, str) or not op_id:
                raise JournalUnreadableError("tagging merge operation is missing op_id")
            if op_id in tombstones:
                continue
            if op_id not in present or op_id in prefer:
                _insert_op(conn, op)
                present.add(op_id)
        conn.execute("COMMIT")
    except (JournalUnreadableError, JournalTransientReadError):
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    except sqlite3.OperationalError as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise JournalTransientReadError(
            f"tagging journal merge could not be written ({exc})"
        ) from exc
    except sqlite3.DatabaseError as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise JournalUnreadableError("tagging journal merge failed") from exc
    finally:
        conn.close()


def new_op_id(manifest: dict) -> str:
    """Time-ordered id with random entropy, independent of retained row count."""
    del manifest
    return f"{time.time_ns()}-{secrets.token_hex(4)}"


def now_iso() -> str:
    """UTC ISO-8601 timestamp for the op record."""
    return datetime.now(timezone.utc).isoformat()


def find_op(manifest: dict, op_id: str) -> dict | None:
    """Return the op with id *op_id*, or ``None``."""
    ops = manifest.get("ops", [])
    if isinstance(ops, _TrackedOps):
        return ops.by_id.get(op_id)
    for op in ops:
        if op.get("op_id") == op_id:
            return op
    return None


def prune(vault_root: Path, cfg: dict, manifest: dict, *,
          protect_batch_id: str | None = None,
          protect_op_ids: set[str] | None = None) -> int:
    """Bound the manifest and reclaim snapshot disk. Returns ops dropped.

    Phase 1 drops spent ops (``reverted``/``failed``) and reclaims their file
    snapshots. Phase 2 enforces ``_MAX_OPS`` oldest-first **over the EVICTABLE
    population only** (2026-08-13): protected ops — the permanent applied
    finder/zotero carve-out below plus the per-call ``protect_*`` exemptions —
    do not consume the budget. Counting them did (the original shape), and once
    the never-evictable ops alone exceeded the cap, ``over`` was permanently
    positive and EVERY note op was evicted on EVERY prune — measured live: a
    4,105-op manifest of applied finder/zotero ops meant each note write's undo
    (snapshot included) survived only until the next tagging write of any kind,
    while responses and the harmonize confirm still promised it. Now note ops
    get a genuine ``_MAX_OPS``-deep retention window and age out gradually.
    Only snapshots of dropped ops are unlinked (no blind directory sweep), so a
    snapshot orphaned by a crash mid-write survives for manual recovery. Caller
    persists after.

    ``protect_batch_id``: applied ops stamped with this batch id are exempt
    from the phase-2 cap. A batch caller (retire-tag) prunes right after
    appending its own ops — without the exemption, a batch larger than
    ``_MAX_OPS`` would evict its OWN earliest ops and delete their snapshots
    in the same request that promised "one-click undo", making the batch
    restore silently partial with no recovery. The exemption is per-call, so
    the batch becomes normally evictable on the NEXT prune (any later write):
    bounded growth, same trade as the inline finder/zotero carve-out below.

    ``protect_op_ids``: the same exemption for a SINGLE-op write, which had the
    identical defect one op at a time. Applied finder/zotero ops are never
    evicted and the deck's Accept journals two of them per paper, so after
    ~100 accepts the manifest is almost entirely protected ops; the next
    write's own ``note_tag`` op is then the only evictable record in ``kept``
    and the cap loop drops it — deleting the pre-write snapshot BEFORE the note
    is overwritten, while the response still hands back its ``op_id``. The undo
    the caller was just promised 404s and the snapshot backing it is gone.
    Callers pass the ids they appended in this request.
    """
    if getattr(manifest, "scoped", False):
        raise ValueError("prune/compact requires a full manifest from journal.load()")
    arch = archive_dir(vault_root, cfg)

    def _del_snapshot(op: dict) -> None:
        rel = op.get("snapshot_rel")
        if not rel:
            return
        try:
            p = arch / rel
            assert_under(p, arch)
            Path(p).unlink(missing_ok=True)
        except (OSError, ScopeError):
            pass

    ops = manifest.get("ops", [])
    dropped = 0
    kept: list[dict] = []
    for op in ops:
        if op.get("state") in ("reverted", "failed"):
            _del_snapshot(op)
            dropped += 1
        else:
            kept.append(op)

    def _protected(op: dict) -> bool:
        # Cap eviction must never drop an APPLIED finder/zotero op: their
        # before-lists live INLINE in the op record — no snapshot file
        # backs them — so eviction destroys the only restore mapping.
        # The deck journals 2-3 such ops per Accept, so one bulk triage
        # session (~100 papers) blows the cap; same carve-out as
        # refactor/journal.py's applied archive_image. Note ops stay
        # evictable (their .bak snapshot is the recovery of last resort,
        # and log_vault_write keeps the human-readable trail).
        # Read from EXTERNAL_OP_KINDS rather than a second literal tuple: the
        # two lists had to agree and nothing enforced it, so adding a kind
        # (``zotero_attachment_trash``, 2026-08-28) to one and not the other
        # would have made its op silently evictable — destroying the only
        # record of which attachment to restore.
        return bool(op.get("recovery_blocked")) or (
            op.get("kind") in EXTERNAL_OP_KINDS
            and op.get("state") in ("applied", "outcome_unknown", "observed_after_recovery")
        ) or (
            # Current-batch exemption (see docstring): only APPLIED ops —
            # a failed batch op has nothing to restore and stays evictable.
            protect_batch_id is not None
            and op.get("batch_id") == protect_batch_id
            and op.get("state") == "applied"
        ) or (
            # This request's own freshly-appended ops (see docstring).
            protect_op_ids is not None
            and op.get("op_id") in protect_op_ids
            and op.get("state") == "applied"
        )

    # The cap budget counts EVICTABLE ops only (see docstring): protected ops
    # are exempt from eviction, so letting them consume the budget starves the
    # evictable population down to zero retention instead of bounding it.
    evictable_count = sum(1 for op in kept if not _protected(op))
    if evictable_count > _MAX_OPS:
        over = evictable_count - _MAX_OPS
        survivors: list[dict] = []
        for op in kept:
            if over > 0 and not _protected(op):
                _del_snapshot(op)
                dropped += 1
                over -= 1
                continue
            survivors.append(op)
        kept = survivors

    # Identity-preserving in place (see _TrackedOps.retain): assigning through
    # manifest["ops"] rewraps every survivor as a copy and detaches the op the
    # calling writer is still holding, which is how failure flips went missing.
    # Phase 3: the OPTIONAL retention pass (2026-08-26). Off unless the user
    # turned it on in Settings, because it is the one place in this module that
    # deliberately destroys a restore mapping — see `_retention_victims`.
    if cfg.get("tagging_journal_retention_enabled"):
        victims = _retention_victims(kept, cfg, protected=_protected)
        if victims:
            doomed = {id(op) for op in victims}
            survivors = []
            for op in kept:
                if id(op) in doomed:
                    _del_snapshot(op)
                    dropped += 1
                    continue
                survivors.append(op)
            kept = survivors
            logger.info(
                "tagging journal retention dropped %d applied external op(s)",
                len(victims))

    # Identity-preserving in place (see _TrackedOps.retain): assigning through
    # manifest["ops"] rewraps every survivor as a copy and detaches the op the
    # calling writer is still holding, which is how failure flips went missing.
    if isinstance(ops, _TrackedOps):
        ops.retain(kept)
    else:
        manifest["ops"] = kept
    return dropped


# --- growth: stats, paging, retention (2026-08-26) -------------------------

def _op_age_days(op: dict, now: float) -> float | None:
    """Whole days since *op* was stamped, or None when its ts is unusable.

    An unparseable timestamp yields ``None`` and the caller keeps the op: a
    retention pass must never destroy a restore mapping because it could not
    read a date. ``ts`` is written by :func:`now_iso` (UTC, ``Z``-suffixed);
    older rows migrated from JSON carry the same shape.
    """
    ts = op.get("ts")
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parsed.timestamp()) / 86400.0)


def _retention_candidates(ops: list, *, protected=None) -> list[dict]:
    """Applied external ops, oldest first — the only droppable population.

    ``protected`` (prune's per-call exemption predicate) additionally spares
    the current batch and this request's own freshly-appended ops, so a
    retention pass can never evict the very ops whose undo was just promised.
    """
    out = []
    for op in ops:
        if op.get("kind") not in EXTERNAL_OP_KINDS:
            continue
        if op.get("state") != "applied":
            continue
        if protected is not None and _is_request_protected(op, protected):
            continue
        out.append(op)
    return out


def _is_request_protected(op: dict, protected) -> bool:
    """True when *protected* spares *op* for a reason OTHER than its kind.

    ``prune._protected`` returns True for every applied external op — that is
    the permanent carve-out retention exists to override. What must still be
    honoured is the per-request half (current batch / freshly appended), so we
    re-ask the predicate with the kind neutralised.
    """
    probe = dict(op)
    probe["kind"] = "note_tag"
    return bool(protected(probe))


def _retention_victims(ops: list, cfg: dict, *, protected=None) -> list[dict]:
    """Ops the configured retention policy would drop. Never raises.

    A malformed or out-of-range setting yields an EMPTY list rather than a
    guess: the failure mode of guessing here is deleting the only record of how
    to undo a tag write.
    """
    policy = cfg.get("tagging_journal_retention_policy")
    if policy not in RETENTION_POLICIES:
        return []
    candidates = _retention_candidates(ops, protected=protected)
    if not candidates:
        return []
    if policy == "age_days":
        raw_days = cfg.get("tagging_journal_retention_days")
        try:
            days = int(raw_days)  # type: ignore[arg-type]  # guarded below
        except (TypeError, ValueError):
            return []
        if days <= 0:
            return []
        now = time.time()
        victims = []
        for op in candidates:
            age = _op_age_days(op, now)
            if age is not None and age > days:
                victims.append(op)
        return victims
    # keep_newest: candidates are in manifest order, which is append order.
    raw_keep = cfg.get("tagging_journal_retention_keep")
    try:
        keep = int(raw_keep)  # type: ignore[arg-type]  # guarded below
    except (TypeError, ValueError):
        return []
    if keep <= 0 or len(candidates) <= keep:
        return []
    return candidates[:len(candidates) - keep]


def stats(vault_root: Path, cfg: dict) -> dict:
    """Journal size and composition WITHOUT parsing a single payload.

    Pure SQL aggregates plus a stat() of the database files, so this stays
    cheap at any history length — the whole point, given that ``load`` parses
    every payload and is what this exists to let the UI avoid. Like
    :func:`load` it is a PURE READER of an EXISTING generation: no schema
    creation, so it cannot turn into SQLITE_BUSY against a concurrent
    ``BEGIN IMMEDIATE``. A legacy ``manifest.json`` is migrated first (see
    :func:`_open_for_read`) — reporting an unmigrated history as an empty one
    is what suppressed the whole Restore panel on an upgrading install.

    A journal that does not exist yet reports zeroes rather than raising —
    the Restore panel asks for this before the first write.
    """
    empty = {
        "ops": 0, "bytes": 0, "by_kind_state": {}, "oldest_ts": None,
        "newest_ts": None, "protected": 0, "evictable": 0, "tombstones": 0,
    }
    db = _open_for_read(vault_root, cfg)
    if db is None:
        return empty
    size = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            size += (db.parent / (db.name + suffix)).stat().st_size
        except OSError:
            pass
    conn = _connect(db, create=False)
    try:
        rows = conn.execute(
            "SELECT kind, state, COUNT(*) FROM operations GROUP BY kind, state"
        ).fetchall()
        span = conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM operations WHERE ts IS NOT NULL"
        ).fetchone()
        tombs = conn.execute(
            "SELECT COUNT(*) FROM operation_tombstones").fetchone()[0]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return empty
        raise JournalTransientReadError(
            f"tagging journal could not be read ({exc}); no write was attempted"
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise JournalUnreadableError(
            f"tagging journal database is corrupt ({db})") from exc
    finally:
        conn.close()

    by_kind_state: dict[str, int] = {}
    total = protected = 0
    for kind, state, count in rows:
        by_kind_state[f"{kind or '?'}/{state or '?'}"] = count
        total += count
        if kind in EXTERNAL_OP_KINDS and state in ("applied", "outcome_unknown", "observed_after_recovery"):
            protected += count
    return {
        "ops": total,
        "bytes": size,
        "by_kind_state": by_kind_state,
        "oldest_ts": span[0] if span else None,
        "newest_ts": span[1] if span else None,
        # `protected` is the never-evicted population — the growth, entirely.
        "protected": protected,
        "evictable": total - protected,
        "tombstones": tombs,
    }


def load_page(vault_root: Path, cfg: dict, *, limit: int = DEFAULT_PAGE_SIZE,
              before_seq: int | None = None) -> dict:
    """One newest-first page of operations, as PLAIN dicts.

    Deliberately not a ``_JournalManifest``: this is a read-only projection for
    the Restore view and must never be handed to :func:`save`, whose
    incremental-write contract depends on the baseline id set covering the
    whole history. Returning plain dicts makes that misuse structurally
    obvious. Pure reader of an existing generation, like :func:`load` and
    :func:`stats`; a legacy ``manifest.json`` is migrated first, or the Restore
    view would render an upgrading install's full history as "nothing to
    restore" (see :func:`_open_for_read`).

    ``before_seq`` is the ``next_before`` cursor from a previous page.
    """
    limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    db = _open_for_read(vault_root, cfg)
    if db is None:
        return {"ops": [], "total": 0, "next_before": None}
    conn = _connect(db, create=False)
    try:
        if before_seq is None:
            rows = conn.execute(
                "SELECT seq, op_id, payload FROM operations "
                "ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT seq, op_id, payload FROM operations WHERE seq < ? "
                "ORDER BY seq DESC LIMIT ?", (int(before_seq), limit)).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {"ops": [], "total": 0, "next_before": None}
        raise JournalTransientReadError(
            f"tagging journal could not be read ({exc}); no write was attempted"
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise JournalUnreadableError(
            f"tagging journal database is corrupt ({db})") from exc
    finally:
        conn.close()

    ops = []
    for _seq, op_id, payload in rows:
        try:
            op = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise JournalUnreadableError(
                "tagging journal contains corrupt operation JSON") from exc
        if op.get("op_id") != op_id:
            raise JournalUnreadableError(
                "tagging journal operation identity mismatch")
        ops.append(op)
    # A full page means there may be more; a short one is the end. `seq` is the
    # AUTOINCREMENT rowid, monotonic and never reused, so it is a stable cursor
    # even while new ops are appended at the other end of the ordering.
    next_before = rows[-1][0] if len(rows) == limit else None
    return {"ops": ops, "total": total, "next_before": next_before}


def _scoped_ops(vault_root: Path, cfg: dict, sql: str, params: tuple) -> list[dict]:
    """Run one indexed SELECT and return PLAIN op dicts (never a manifest).

    # PERF (2026-09-02): a whole-history parse for a peek.
    #   Before:    the two restore PEEKS — `_restore_batch`'s "does this batch
    #              id exist?" pre-check and the single-op restore's "what kind
    #              is this op?" — each called `load()`, parsing EVERY op in the
    #              journal. Measured on a journal of 11 651 ops (the live
    #              count): 40 ms and ~11 651 dict allocations per peek, twice
    #              per restore, on a Waitress worker.
    #   Change:    one indexed SELECT. `operations` already carries a UNIQUE
    #              index on `op_id` and `operations_batch_idx` on
    #              `(batch_id, seq)`, so neither query scans.
    #   Safe because:
    #              this returns PLAIN dicts, exactly as `load_page` does, and
    #              never a `_JournalManifest` — so it is structurally impossible
    #              to hand a partial history to `save()`, whose incremental
    #              write contract depends on the baseline id set covering the
    #              WHOLE history. Both call sites already re-`load()` under the
    #              op-lock for the actual write; the peek only decides a 404 and
    #              which Zotero read to do first.
    #   Preserves: "the journal's dirty-tracking contract that broke twice in
    #              August. A partial manifest must never let `save()` compute
    #              removals over a truncated list." Nothing here constructs a
    #              manifest, tracks dirtiness, or writes.
    #   Lock/thread contract:
    #              a pure reader of an existing generation, like `load`,
    #              `stats` and `load_page` — no schema creation on this path, or
    #              a concurrent writer's BEGIN IMMEDIATE would turn an ordinary
    #              read into SQLITE_BUSY and defeat WAL.
    #   Pinned by: test_tagging.py::TestScopedJournalPeeks
    """
    db = _open_for_read(vault_root, cfg)
    if db is None:
        return []
    conn = _connect(db, create=False)
    try:
        _verify_legacy_not_diverged(conn, vault_root, cfg)
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        if meta.get("schema_version") != str(_SQLITE_SCHEMA_VERSION):
            raise JournalUnreadableError("unsupported tagging journal schema version")
        if meta.get("vault_key") != _vault_key(vault_root):
            raise JournalUnreadableError("tagging journal belongs to a different vault")
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise JournalTransientReadError(
            f"tagging journal could not be read ({exc}); no write was attempted"
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise JournalUnreadableError(
            f"tagging journal database is corrupt ({db})") from exc
    finally:
        conn.close()
    ops: list[dict] = []
    for op_id, payload in rows:
        try:
            op = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise JournalUnreadableError(
                "tagging journal contains corrupt operation JSON") from exc
        if op.get("op_id") != op_id:
            raise JournalUnreadableError(
                "tagging journal operation identity mismatch")
        ops.append(op)
    return ops


def peek_op(vault_root: Path, cfg: dict, op_id: str) -> dict | None:
    """One op by id, as a PLAIN dict — or ``None``. See :func:`_scoped_ops`."""
    ops = _scoped_ops(
        vault_root, cfg,
        "SELECT op_id, payload FROM operations WHERE op_id = ? LIMIT 1",
        (str(op_id),))
    return ops[0] if ops else None


def peek_batch_ops(vault_root: Path, cfg: dict, batch_id: str, *,
                   state: str | None = None) -> list[dict]:
    """A batch's ops (oldest-first), as PLAIN dicts. See :func:`_scoped_ops`."""
    if state is None:
        return _scoped_ops(
            vault_root, cfg,
            "SELECT op_id, payload FROM operations WHERE batch_id = ? "
            "ORDER BY seq", (str(batch_id),))
    return _scoped_ops(
        vault_root, cfg,
        "SELECT op_id, payload FROM operations WHERE batch_id = ? AND state = ? "
        "ORDER BY seq", (str(batch_id), str(state)))


def compact(vault_root: Path, cfg: dict, *, policy: str, value: int,
            dry_run: bool = True, manifest: dict | None = None) -> dict:
    """Apply a retention policy ONCE, on demand. Returns what it did/would do.

    Routed deliberately through ``load`` → ``_TrackedOps.retain`` → ``save``
    rather than raw SQL DELETEs, so the removal reuses the same tombstone-then-
    delete transaction every other eviction already uses. That costs one full
    load — irrelevant for a manual, occasional action, and worth far more than
    a second write path through the most safety-critical module in the app.

    ``dry_run`` reports the count and touches nothing. The caller holds the
    obsidian operation lock for a real run, as for any journal write.
    """
    if policy not in RETENTION_POLICIES:
        raise ValueError(f"unknown retention policy: {policy!r}")
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("retention value must be an integer") from exc
    if value <= 0:
        raise ValueError("retention value must be positive")

    if manifest is None:
        manifest = load(vault_root, cfg)
    if getattr(manifest, "scoped", False):
        raise ValueError("prune/compact requires a full manifest from journal.load()")
    ops = manifest.get("ops", [])
    probe_cfg = {
        "tagging_journal_retention_policy": policy,
        "tagging_journal_retention_days": value,
        "tagging_journal_retention_keep": value,
    }
    victims = _retention_victims(ops, probe_cfg)
    by_kind: dict[str, int] = {}
    for op in victims:
        by_kind[op.get("kind") or "?"] = by_kind.get(op.get("kind") or "?", 0) + 1
    result = {
        "policy": policy, "value": value, "dry_run": bool(dry_run),
        "candidates": len(_retention_candidates(ops)),
        "dropped": 0, "would_drop": len(victims),
        "by_kind": by_kind, "before": len(ops), "after": len(ops),
    }
    if dry_run or not victims:
        return result

    doomed = {id(op) for op in victims}
    kept = [op for op in ops if id(op) not in doomed]
    if isinstance(ops, _TrackedOps):
        ops.retain(kept)
    else:
        manifest["ops"] = kept
    save(vault_root, cfg, manifest)
    log_vault_write(
        "tagging_journal_compact", str(manifest_path(vault_root, cfg)),
        f"{policy}={value} dropped {len(victims)} of {result['before']}")
    result["dropped"] = len(victims)
    result["after"] = len(kept)
    return result


# --- note snapshots --------------------------------------------------------

def write_note_snapshot(vault_root: Path, cfg: dict, note_rel: str, op_id: str,
                        raw: bytes) -> str:
    """Persist a pre-write copy of a note; return its archive-relative path.

    Stored flat by op id (``notes/<op_id>.bak``) so a note rel with slashes
    cannot shape the on-disk path; the op record keeps ``note_rel`` for display.
    """
    rel = f"notes/{op_id}.bak"
    dest = archive_dir(vault_root, cfg) / rel
    assert_under(dest, archive_dir(vault_root, cfg))
    write_bytes_atomic(str(dest), raw)
    return rel


def write_external_snapshot(vault_root: Path, cfg: dict, op_id: str,
                            payload: dict) -> str:
    """Persist an EXTERNAL op's restore mapping as a sidecar; return its rel.

    A note op's ``.bak`` is durable before its write, so a crash between the
    write and the next manifest checkpoint still leaves the bytes to restore
    from. A Finder op's mapping is its inline ``before``/``before_raw`` lists,
    which until 2026-09-02 existed only in the manifest — so the same crash,
    mid-batch, lost the colours for up to ``JOURNAL_FLUSH_EVERY`` papers
    (recorded in CLAUDE.md's Known Issues, now struck). This gives that
    mapping the same durability without a per-op SQLite transaction: one
    small fsynced JSON file, named by op id like the note snapshots, and
    reclaimed by ``prune`` through the same ``snapshot_rel`` field.
    """
    rel = f"external/{op_id}.json"
    dest = archive_dir(vault_root, cfg) / rel
    assert_under(dest, archive_dir(vault_root, cfg))
    write_bytes_atomic(str(dest), json.dumps(payload).encode("utf-8"))
    return rel


def read_snapshot(vault_root: Path, cfg: dict, snapshot_rel: str) -> bytes:
    """Read back a snapshot for restore, re-pinned under the archive dir."""
    src = archive_dir(vault_root, cfg) / snapshot_rel
    assert_under(src, archive_dir(vault_root, cfg))
    return Path(src).read_bytes()


# --- restore ---------------------------------------------------------------

def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def revert_op(vault_root: Path, cfg: dict, op: dict) -> dict:
    """Undo one applied tagging op; return ``{ok, status, message}``.

    Conservative: if the on-disk state no longer matches what the op produced,
    it refuses (``status="skipped"``) rather than clobber later edits. Marks the
    op ``state="reverted"`` only on success (the caller persists the manifest).
    """
    if op.get("state") == "reverted":
        return {"ok": True, "status": "already_reverted", "message": "Already reverted."}
    kind = op.get("kind")
    # note_untag (tag removal), vocab_retire (a _tags.md row edit, Batch G)
    # and vocab_add (its drift-fix mirror, 2026-08-06) share the note-snapshot
    # revert: the snapshot restore is content-agnostic — it re-instates the
    # pre-write bytes whenever the file still matches hash_after, whichever
    # direction (or file) the write went.
    if kind in ("note_tag", "note_untag", "vocab_retire", "vocab_add",
                # The `pdf:` wikilink list (2026-09-07): a different key, the
                # same whole-note snapshot, so the same content-agnostic
                # revert restores it.
                "note_pdf_links",
                # The record merge's `aliases:` addition (2026-09-17): same
                # whole-note snapshot, same revert.
                "note_aliases"):
        return _revert_note_tag(vault_root, cfg, op)
    return {"ok": False, "status": "unknown", "message": f"Unknown op kind: {kind!r}"}


def _revert_note_tag(vault_root: Path, cfg: dict, op: dict) -> dict:
    """Restore a note's pre-write bytes from its snapshot, conservatively.

    A missing target blocks recovery and protects the snapshot from pruning.
    Later edits skip without overwriting them. On a clean
    match it restores the exact prior bytes and audit-logs the restore.
    """
    note_rel = op.get("note_rel") or ""
    snapshot_rel = op.get("snapshot_rel") or ""
    hash_after = op.get("hash_after") or ""
    if not note_rel or not snapshot_rel:
        return {"ok": False, "status": "invalid", "message": "Op missing note/snapshot."}

    note_path = Path(vault_root) / note_rel
    try:
        assert_under(note_path, vault_root)
    except ScopeError:
        return {"ok": False, "status": "invalid", "message": "Note escapes the vault."}

    if not note_path.exists():
        op["recovery_blocked"] = "missing_target"
        return {"ok": False, "status": "missing_target",
                "message": "Note is missing. Operation and snapshot retained; restore its original path before retrying."}

    current = note_path.read_bytes()
    if _sha256_bytes(current) != hash_after:
        return {
            "ok": True,
            "status": "skipped",
            "message": "Note changed after the tag write — not reverting.",
        }

    try:
        snap = read_snapshot(vault_root, cfg, snapshot_rel)
    except (OSError, ScopeError):
        return {"ok": False, "status": "error", "message": "Snapshot missing/unreadable."}

    write_bytes_atomic(str(note_path), snap)
    op.pop("recovery_blocked", None)
    log_vault_write("tagging_restore_note", note_rel,
                    f"{hash_after[:8]}→{_sha256_bytes(snap)[:8]}")
    op["state"] = "reverted"
    return {"ok": True, "status": "reverted", "message": "Note tags reverted."}


__all__ = [
    "ScopeError", "JournalUnreadableError", "JournalTransientReadError",
    "archive_dir", "manifest_path", "migration_backup_path", "assert_under",
    "load", "save", "save_merged", "prune", "new_op_id", "now_iso", "find_op",
    "write_note_snapshot", "read_snapshot", "revert_op",
]
