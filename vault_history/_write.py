"""Shared journal-before-write primitive for journalled note updates.

Provides a unified stale-diff-guarded and crash-resilient write sequence.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union
from collections.abc import Callable

from core.utils import write_text_atomic
from vault_history import journal

# before_write may return True (proceed), False (legacy: source changed),
# or a non-empty str reason code surfaced to the caller.
_BeforeWriteResult = Union[bool, str]
_BeforeWrite = Callable[[], _BeforeWriteResult]


def journalled_write_note(
    vault_root: Path,
    cfg: dict,
    rel: str,
    raw_bytes: bytes,
    hash_before: str,
    hash_after: str,
    proposed_text: str,
    op_kind: str,
    manifest: dict,
    *,
    action: Optional[str] = None,
    log_category: Optional[str] = None,
    pre_write_persist: bool = False,
    post_write_persist_on_failure: bool = False,
    before_write: _BeforeWrite | None = None,
) -> tuple[str, str, Optional[str]]:
    """Perform the journal-before-write sequence for a note.

    Returns a tuple of:
      - op_id (str)
      - status (str): "applied" or "failed"
      - error_message (Optional[str]): the failure reason if status is "failed"

    ``before_write`` runs AFTER the snapshot and (when ``pre_write_persist``)
    AFTER the crash-trail save, but BEFORE the note write. That ordering is
    load-bearing: a False/str return must flip the already-persisted op to
    ``failed`` so Restore never sees an ``applied`` claim for bytes that never
    landed. Exceptions from the predicate are treated the same way — they must
    not escape after the crash-trail save, or the op would stay ``applied``
    with no disk write (the create-attempt key-recovery failure mode).
    """
    note_path = vault_root / rel
    op_id = journal.new_op_id(manifest)

    try:
        snapshot_rel = journal.write_note_snapshot(vault_root, cfg, rel, op_id, raw_bytes)
    except OSError as exc:
        return op_id, "failed", f"snapshot write failed ({type(exc).__name__})"

    op = {
        "id": op_id,
        "kind": op_kind,
        "ts": journal.now_iso(),
        "note_rel": rel,
        "hash_before": hash_before,
        "hash_after": hash_after,
        "snapshot_rel": snapshot_rel,
        "state": "applied",
    }
    if action is not None:
        op["action"] = action

    manifest["ops"].append(op)

    if pre_write_persist:
        # Never let this call's own prune evict the op it just appended
        # (2026-09-17 review, F11; the notesync batch creator's rule).
        journal.prune(vault_root, cfg, manifest, protect_op_ids={op_id})
        journal.save(vault_root, cfg, manifest)

    if before_write is not None:
        try:
            gate = before_write()
        except Exception as exc:  # noqa: BLE001 — must not leave applied-without-write
            op["state"] = "failed"
            journal.save(vault_root, cfg, manifest)
            return op_id, "failed", f"before_write_error:{type(exc).__name__}"
        if gate is True:
            pass
        else:
            # False → legacy reason; non-empty str → caller-named reason.
            reason = (
                gate if isinstance(gate, str) and gate
                else "source_changed_before_write"
            )
            op["state"] = "failed"
            journal.save(vault_root, cfg, manifest)
            return op_id, "failed", reason

    category = log_category or op_kind
    journal.log_vault_write(category, rel, f"{hash_before[:8]}→{hash_after[:8]}")

    try:
        write_text_atomic(str(note_path), proposed_text)
    except OSError as exc:
        op["state"] = "failed"
        # B10/#35 (2026-07-13): log the failure counterpart, mirroring
        # create_note's ``create_note_failed`` — the intent-first log line above
        # asserts a write that never reached disk, so record that it failed. The
        # human-readable trail is the recovery record when the manifest is lost.
        journal.log_vault_write(f"{category}_failed", rel, type(exc).__name__)
        if post_write_persist_on_failure or pre_write_persist:
            journal.save(vault_root, cfg, manifest)
        return op_id, "failed", f"write failed ({type(exc).__name__})"

    return op_id, "applied", None
