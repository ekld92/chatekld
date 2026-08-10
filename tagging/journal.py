"""Restore manifest + archive for the biblio Tagging write ops.

ONE per-vault manifest covers all three tagging write targets so the sub-view
has a single Restore surface: Obsidian note-frontmatter writes (``note_tag``),
PDF Finder-tag writes (``finder_tag``), and Zotero item-tag writes
(``zotero_tag``). Slice 2 implements ``note_tag``; the other two land in slices
4/5 (their prior state — a small tag list — is stored inline in the op, so they
need no file snapshot).

Modelled on ``refactor/journal.py`` (snapshot-before-write, conservative revert
that refuses to clobber later edits, atomic whole-file manifest, capped,
``log_vault_write``-audited) but self-contained under
``<tagging_archive_dir>/<vault_key>/`` — ALWAYS validated to resolve OUTSIDE the
vault (a snapshot written into the vault would be re-indexed). Callers hold the
obsidian operation lock for the duration, so there is a single writer.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from core.constants import BASE_DIR
from core.utils import log_vault_write, write_bytes_atomic, write_text_atomic

_MANIFEST_VERSION = 1
_MANIFEST_NAME = "manifest.json"

# Cap on retained ops. The manifest JSON (rewritten whole on every change) and
# the per-op note snapshots would otherwise grow unbounded over the app's life.
_MAX_OPS = 200


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
    """``<archive_dir>/manifest.json`` — the restore journal for this vault."""
    return archive_dir(vault_root, cfg) / _MANIFEST_NAME


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


def load(vault_root: Path, cfg: dict) -> dict:
    """Load the manifest (a fresh empty one only when genuinely ABSENT).

    Raises :class:`JournalUnreadableError` when the file exists but cannot be
    read, parsed, or has the wrong shape — never silently replaces it.
    """
    p = manifest_path(vault_root, cfg)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": _MANIFEST_VERSION, "vault_key": _vault_key(vault_root), "ops": []}
    except OSError as exc:
        raise JournalUnreadableError(
            f"tagging journal could not be read ({exc}); refusing to write "
            "so the existing restore history is not overwritten"
        ) from exc
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise JournalUnreadableError(
            "tagging journal is corrupt; refusing to write so the existing "
            f"restore history is not overwritten ({p})"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("ops"), list):
        raise JournalUnreadableError(
            "tagging journal has an unexpected shape; refusing to write so "
            f"the existing restore history is not overwritten ({p})"
        )
    data.setdefault("version", _MANIFEST_VERSION)
    data.setdefault("vault_key", _vault_key(vault_root))
    return data


def save(vault_root: Path, cfg: dict, manifest: dict) -> None:
    """Atomically persist the whole manifest. Caller holds the op lock."""
    write_text_atomic(
        str(manifest_path(vault_root, cfg)),
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )


def new_op_id(manifest: dict) -> str:
    """Monotonic-ish op id: ``<epoch_ms>-<seq>`` (unique within a manifest)."""
    return f"{int(time.time() * 1000)}-{len(manifest.get('ops', []))}"


def now_iso() -> str:
    """UTC ISO-8601 timestamp for the op record."""
    return datetime.now(timezone.utc).isoformat()


def find_op(manifest: dict, op_id: str) -> dict | None:
    """Return the op with id *op_id*, or ``None``."""
    for op in manifest.get("ops", []):
        if op.get("op_id") == op_id:
            return op
    return None


def prune(vault_root: Path, cfg: dict, manifest: dict, *,
          protect_batch_id: str | None = None,
          protect_op_ids: set[str] | None = None) -> int:
    """Bound the manifest and reclaim snapshot disk. Returns ops dropped.

    Phase 1 drops spent ops (``reverted``/``failed``) and reclaims their file
    snapshots. Phase 2 enforces ``_MAX_OPS`` oldest-first. Only snapshots of
    dropped ops are unlinked (no blind directory sweep), so a snapshot orphaned
    by a crash mid-write survives for manual recovery. Caller persists after.

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

    if len(kept) > _MAX_OPS:
        over = len(kept) - _MAX_OPS
        survivors: list[dict] = []
        for op in kept:
            # Cap eviction must never drop an APPLIED finder/zotero op: their
            # before-lists live INLINE in the op record — no snapshot file
            # backs them — so eviction destroys the only restore mapping.
            # The deck journals 2-3 such ops per Accept, so one bulk triage
            # session (~100 papers) blows the cap; same carve-out as
            # refactor/journal.py's applied archive_image. Note ops stay
            # evictable (their .bak snapshot is the recovery of last resort,
            # and log_vault_write keeps the human-readable trail).
            protected = (
                op.get("kind") in ("finder_tag", "zotero_tag",
                                   "finder_untag", "zotero_untag")
                and op.get("state") == "applied"
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
            if over > 0 and not protected:
                _del_snapshot(op)
                dropped += 1
                over -= 1
                continue
            survivors.append(op)
        kept = survivors

    manifest["ops"] = kept
    return dropped


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
    if kind in ("note_tag", "note_untag", "vocab_retire", "vocab_add"):
        return _revert_note_tag(vault_root, cfg, op)
    return {"ok": False, "status": "unknown", "message": f"Unknown op kind: {kind!r}"}


def _revert_note_tag(vault_root: Path, cfg: dict, op: dict) -> dict:
    """Restore a note's pre-write bytes from its snapshot, conservatively.

    Skips (does not error) when the note was deleted or edited after the tag
    write — the snapshot no longer represents a safe rollback point. On a clean
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
        op["state"] = "reverted"
        return {"ok": True, "status": "skipped", "message": "Note no longer exists."}

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
    log_vault_write("tagging_restore_note", note_rel,
                    f"{hash_after[:8]}→{_sha256_bytes(snap)[:8]}")
    op["state"] = "reverted"
    return {"ok": True, "status": "reverted", "message": "Note tags reverted."}


__all__ = [
    "ScopeError", "archive_dir", "manifest_path", "assert_under",
    "load", "save", "prune", "new_op_id", "now_iso", "find_op",
    "write_note_snapshot", "read_snapshot", "revert_op",
]
