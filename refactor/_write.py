"""Shared journal-before-write primitive for refactor note updates.

Provides a unified stale-diff-guarded and crash-resilient write sequence.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from core.utils import write_text_atomic
from refactor import journal


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
) -> tuple[str, str, Optional[str]]:
    """Perform the journal-before-write sequence for a note.

    Returns a tuple of:
      - op_id (str)
      - status (str): "applied" or "failed"
      - error_message (Optional[str]): the failure reason if status is "failed"
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
        journal.prune(vault_root, cfg, manifest)
        journal.save(vault_root, cfg, manifest)

    category = log_category or op_kind
    journal.log_vault_write(category, rel, f"{hash_before[:8]}→{hash_after[:8]}")

    try:
        write_text_atomic(str(note_path), proposed_text)
    except OSError as exc:
        op["state"] = "failed"
        if post_write_persist_on_failure or pre_write_persist:
            journal.save(vault_root, cfg, manifest)
        return op_id, "failed", f"write failed ({type(exc).__name__})"

    return op_id, "applied", None
