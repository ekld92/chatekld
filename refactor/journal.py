"""Restore manifest + scope-lock + audit core for the Phase 2 vault writers.

This is the single place that knows where archived material lives, what a vault
write is allowed to touch, and how to undo an apply/archive. ``apply.py`` and
``archive.py`` both go through it.

Layout (per vault, keyed by the same ``_vault_cache_key`` as the image cache):

    <archive_dir>/
      manifest.json          # the journal: {version, vault_key, ops:[...]}
      attachments/<image_rel> # full-res originals moved OUT of the vault
      notes/<note_rel>.<id>.bak # pre-write snapshots of mutated notes

``<archive_dir>`` defaults to ``BASE_DIR/refactor/archive/<vault_key>/`` (local
disk only — NOT iCloud; Time Machine covers it) and is overridable via the
``refactor_archive_dir`` config key. It is ALWAYS validated to resolve **outside**
the vault: archiving back into the vault would re-index the "removed" file (or
recurse), defeating the whole point.

Every mutation is atomic (``core.utils.write_text_atomic`` / ``write_bytes_atomic``)
and traced via ``core.utils.log_vault_write``. The manifest is rewritten whole on
each change; callers hold the obsidian operation lock for the duration, so there
is a single writer and no lost-update race.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from core.constants import BASE_DIR, REFACTOR_THUMBS_DIRNAME
from core.utils import log_vault_write, write_bytes_atomic, write_text_atomic
from rag.vault import obsidian_manager

_MANIFEST_VERSION = 1
_MANIFEST_NAME = "manifest.json"

# Cap on the number of ops the manifest retains. Without a bound the manifest JSON
# (rewritten whole on every change) and the per-op note snapshots under
# ``notes/*.bak`` grow without limit over the app's lifetime. 200 keeps a deep undo
# history (the restore UI lists the most recent ops) while bounding both the JSON
# size and the snapshot disk footprint. See :func:`prune`.
_MAX_OPS = 200


class ScopeError(Exception):
    """A write/move target escaped its allowed root (scope / vault / archive)."""


# --- path roots ------------------------------------------------------------

def _vault_key(vault_root: Path) -> str:
    """The per-vault key (reusing the indexer's cache-key helper).

    Same key the image-description cache and ignore-list use, so a vault's
    archive, cache, and ignore sidecar all live under one stable per-vault name.
    """
    return obsidian_manager._vault_cache_key(vault_root)


def archive_dir(vault_root: Path, cfg: dict) -> Path:
    """Resolve the per-vault archive directory; raise ``ScopeError`` if it would
    sit inside the vault.

    ``refactor_archive_dir`` (absolute, ``~`` allowed) overrides the default
    ``BASE_DIR/refactor/archive``. Either way the real archive root is suffixed
    with the vault key so two vaults (or a shared custom dir) never collide.
    """
    base = (cfg.get("refactor_archive_dir") or "").strip()
    root = Path(base).expanduser() if base else Path(BASE_DIR) / "refactor" / "archive"
    out = root / _vault_key(vault_root)
    real_out = os.path.realpath(out)
    real_vault = os.path.realpath(vault_root)
    # The archive must not live inside the vault, and the vault must not live
    # inside the archive — either direction risks re-indexing moved-out files or
    # an archive/restore recursion.
    if real_out == real_vault \
            or real_out.startswith(real_vault + os.sep) \
            or real_vault.startswith(real_out + os.sep):
        raise ScopeError("archive directory must resolve outside the vault")
    return out


def manifest_path(vault_root: Path, cfg: dict) -> Path:
    """``<archive_dir>/manifest.json`` — the restore journal for this vault."""
    return archive_dir(vault_root, cfg) / _MANIFEST_NAME


# --- scope lock ------------------------------------------------------------

def assert_under(abs_path: str | Path, root: str | Path) -> str:
    """Return ``realpath(abs_path)`` after confirming it is *root* or under it.

    The single chokepoint for "this write/move target is allowed". Used to pin
    note + thumbnail writes under ``<vault>/<scope>`` and archive writes under
    ``<archive_dir>``. Raises :class:`ScopeError` on any escape (traversal,
    symlink-out, absolute mismatch).
    """
    real = os.path.realpath(abs_path)
    real_root = os.path.realpath(root)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise ScopeError(f"path escapes its allowed root: {abs_path}")
    return real


def thumb_dir(vault_root: Path, scope: str) -> Path:
    """``<vault>/<scope>/_thumbs`` — where in-vault thumbnails live."""
    return vault_root / scope / REFACTOR_THUMBS_DIRNAME


# --- manifest io -----------------------------------------------------------

def load(vault_root: Path, cfg: dict) -> dict:
    """Load the manifest (a fresh empty one if absent / unreadable / corrupt)."""
    p = manifest_path(vault_root, cfg)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("ops"), list):
        return {"version": _MANIFEST_VERSION, "vault_key": _vault_key(vault_root), "ops": []}
    data.setdefault("version", _MANIFEST_VERSION)
    data.setdefault("vault_key", _vault_key(vault_root))
    return data


def save(vault_root: Path, cfg: dict, manifest: dict) -> None:
    """Atomically persist the whole manifest. Caller holds the op lock."""
    write_text_atomic(
        str(manifest_path(vault_root, cfg)),
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )


def prune(vault_root: Path, cfg: dict, manifest: dict) -> int:
    """Bound the manifest and reclaim snapshot disk. Returns the op count dropped.

    Caller holds the op lock (single writer) and persists the manifest afterwards.
    Two phases:

    1. **Spent ops** — an op whose ``state`` is ``reverted`` or ``failed`` no longer
       needs its note snapshot (the undo is done, or the write never happened), so
       its ``.bak`` is deleted and the op dropped. A reverted ``archive_image`` op
       already had its archive copy + thumbnail reclaimed by the reverter, so it is
       safe to drop too.
    2. **Cap** — if still over ``_MAX_OPS``, drop the OLDEST note-write ops
       (``apply_note`` / ``normalize_note`` / ``llm_note``) and delete their
       snapshots, accepting the loss of *their* undo (the vault note itself is
       intact and was audit-logged at write time). An **applied** ``archive_image``
       op is never evicted here: it holds the only restore mapping for a file
       physically moved out of the vault, so dropping it would strand the archived
       original. The cap is therefore best-effort if many applied archive ops
       accumulate (rare — archiving is one image at a time).

    Only snapshots belonging to ops this function drops are deleted — there is no
    blind directory sweep, so a ``.bak`` orphaned by a crash mid-batch (its op never
    committed to the manifest) survives for manual recovery.
    """
    arch = archive_dir(vault_root, cfg)

    def _del_snapshot(op: dict) -> None:
        for key in ("snapshot_rel", "note_snapshot_rel"):
            rel = op.get(key)
            if not rel:
                continue
            try:
                p = arch / rel
                assert_under(p, arch)   # defence-in-depth: stay under the archive dir
                Path(p).unlink(missing_ok=True)
            except (OSError, ScopeError):
                pass

    ops = manifest.get("ops", [])
    dropped = 0

    # Phase 1 — drop spent ops (reverted / failed) and reclaim their snapshots.
    kept: list[dict] = []
    for op in ops:
        if op.get("state") in ("reverted", "failed"):
            _del_snapshot(op)
            dropped += 1
        else:
            kept.append(op)

    # Phase 2 — enforce the cap, oldest-first (``kept`` is in append/chronological
    # order), but never evict an applied archive_image op (restore-critical).
    if len(kept) > _MAX_OPS:
        over = len(kept) - _MAX_OPS
        survivors: list[dict] = []
        for op in kept:
            if over > 0 and op.get("kind") in ("apply_note", "normalize_note", "llm_note"):
                _del_snapshot(op)
                dropped += 1
                over -= 1
                continue
            survivors.append(op)
        kept = survivors

    manifest["ops"] = kept
    return dropped


def new_op_id(manifest: dict) -> str:
    """A unique, ordered op id (ms timestamp + position in the ops list)."""
    return f"{int(time.time() * 1000)}-{len(manifest.get('ops', []))}"


def now_iso() -> str:
    """UTC timestamp (ISO-8601) stamped onto each op for the restore UI."""
    return datetime.now(timezone.utc).isoformat()


def find_op(manifest: dict, op_id: str) -> dict | None:
    """Return the op with id *op_id* from the manifest, or ``None`` if absent."""
    for op in manifest.get("ops", []):
        if op.get("id") == op_id:
            return op
    return None


# --- snapshots / archived copies ------------------------------------------

def write_note_snapshot(vault_root: Path, cfg: dict, note_rel: str, op_id: str,
                        raw: bytes) -> str:
    """Persist a pre-write copy of a note under ``<archive_dir>/notes/``.

    Returns the archive-relative snapshot path (stored in the op for restore).
    """
    rel = f"notes/{note_rel}.{op_id}.bak"
    dest = archive_dir(vault_root, cfg) / rel
    assert_under(dest, archive_dir(vault_root, cfg))
    write_bytes_atomic(str(dest), raw)
    return rel


def read_snapshot(vault_root: Path, cfg: dict, snapshot_rel: str) -> bytes:
    """Read back a pre-write note snapshot for restore.

    The snapshot path comes from the manifest (untrusted-ish), so it is
    re-pinned under the archive dir via ``assert_under`` before the read — a
    traversal-shaped ``snapshot_rel`` cannot reach outside the archive.
    """
    src = archive_dir(vault_root, cfg) / snapshot_rel
    assert_under(src, archive_dir(vault_root, cfg))
    return Path(src).read_bytes()


# --- restore ---------------------------------------------------------------

def revert_op(vault_root: Path, cfg: dict, op: dict) -> dict:
    """Undo one applied op; return ``{ok, status, message}``.

    Dispatches by ``kind``. Conservative: if the on-disk state no longer matches
    what the op produced (the note was edited again after apply), it refuses
    rather than clobber later work, and reports ``status="skipped"``. Marks the
    op ``state="reverted"`` only on success. Imports the per-kind reverters
    lazily to avoid an import cycle (apply/archive import this module).
    """
    if op.get("state") == "reverted":
        return {"ok": True, "status": "already_reverted", "message": "Already reverted."}
    kind = op.get("kind")
    if kind in ("apply_note", "normalize_note", "llm_note"):
        # All three write a whole note and snapshot the prior bytes; restore is
        # the same snapshot-based revert (the op carries identical note_rel /
        # hash_before / hash_after / snapshot_rel fields). ``llm_note`` covers the
        # applyable LLM rewrite + PDF-summary writers (refactor.llm_apply).
        from refactor.apply import revert_apply_note
        return revert_apply_note(vault_root, cfg, op)
    if kind == "archive_image":
        from refactor.archive import revert_archive_image
        return revert_archive_image(vault_root, cfg, op)
    return {"ok": False, "status": "unknown", "message": f"Unknown op kind: {kind!r}"}


__all__ = [
    "ScopeError", "archive_dir", "manifest_path", "assert_under", "thumb_dir",
    "load", "save", "prune", "new_op_id", "now_iso", "find_op",
    "write_note_snapshot", "read_snapshot", "revert_op",
    "log_vault_write",
]
