"""Journalled creation of a NEW vault note (docgen Markdown long-form output).

Adds a third vault-write op kind to the refactor journal — ``create_note`` —
whose Restore DELETES the created file (there is no prior content to snapshot,
so the inverse of "create" is "delete", not "restore a snapshot"). Routed
through :mod:`refactor.journal` so the existing Restore UI + manifest cover
docgen-written notes uniformly, exactly like the apply / normalize / archive
writers. The caller (the docgen route) holds the obsidian op-lock for the write.

The write REFUSES to touch an existing file — this path only ever *creates* a new
note (overwriting a user's note is the apply/augment writers' job, with their
stale-diff + WYSIWYG guards). Restore is conservative: it deletes the note only
if its bytes still match what was created (a note the user edited afterwards is
left alone and reported ``skipped``).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

from core.utils import write_text_atomic
from refactor import journal


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def create_note(
    vault_root: Path, cfg: dict, rel: str, text: str, manifest: dict,
) -> tuple[str, str, Optional[str]]:
    """Create a NEW note at ``<vault>/<rel>`` with *text*, journalled + reversible.

    Returns ``(op_id, status, error)`` where *status* is ``"applied"`` or
    ``"failed"``. Refuses (``failed``) if a file already exists at *rel* — the
    caller validated *rel* is under the vault; this guards the create-only
    contract. Appends the op and persists the manifest (caller holds the op lock).
    """
    note_path = vault_root / rel
    if note_path.exists():
        return "", "failed", "a note already exists at that path"

    op_id = journal.new_op_id(manifest)
    raw = text.encode("utf-8")
    hash_after = _sha(raw)
    op = {
        "id": op_id, "kind": "create_note", "ts": journal.now_iso(),
        "note_rel": rel, "hash_before": "", "hash_after": hash_after,
        "snapshot_rel": "", "state": "applied",
    }
    manifest["ops"].append(op)
    journal.log_vault_write("create_note", rel, f"∅→{hash_after[:8]}")

    try:
        journal.assert_under(note_path, vault_root)  # defence-in-depth
        os.makedirs(os.path.dirname(str(note_path)) or str(vault_root), exist_ok=True)
        write_text_atomic(str(note_path), text)
    except (OSError, journal.ScopeError) as exc:
        # Audit-trail completeness (audit #11): the create_note line above was
        # logged BEFORE the write (journal-before-write, matching
        # journalled_write_note's intent-first order) — emit the failure
        # counterpart so the human-readable trail never asserts a create that
        # never reached disk (the manifest op alone records `failed`, but the
        # log is the recovery record when the manifest is lost).
        journal.log_vault_write("create_note_failed", rel, type(exc).__name__)
        op["state"] = "failed"
        journal.save(vault_root, cfg, manifest)
        return op_id, "failed", f"write failed ({type(exc).__name__})"

    journal.prune(vault_root, cfg, manifest)
    journal.save(vault_root, cfg, manifest)
    return op_id, "applied", None


def revert_create_note(vault_root: Path, cfg: dict, op: dict) -> dict:
    """Undo a ``create_note`` op by DELETING the created note. Conservative.

    Returns ``{ok, status, message}``. If the note is already gone the op is
    marked reverted (idempotent). If its current bytes no longer match what was
    created (the user edited it), the delete is refused (``skipped``) so later
    work is never clobbered.
    """
    rel = op.get("note_rel")
    note_path = vault_root / rel
    if not note_path.exists():
        op["state"] = "reverted"
        return {"ok": True, "status": "reverted", "message": "The created note is already gone."}
    try:
        cur = note_path.read_bytes()
    except OSError as exc:
        return {"ok": False, "status": "error", "message": f"read failed ({type(exc).__name__})"}
    if _sha(cur) != op.get("hash_after"):
        return {"ok": False, "status": "skipped",
                "message": "The note changed since it was created — left in place, not deleted."}
    try:
        journal.assert_under(note_path, vault_root)
        note_path.unlink()
    except (OSError, journal.ScopeError) as exc:
        return {"ok": False, "status": "error", "message": f"delete failed ({type(exc).__name__})"}
    journal.log_vault_write("restore_delete_note", rel, f"{(op.get('hash_after') or '')[:8]}→∅")
    op["state"] = "reverted"
    return {"ok": True, "status": "reverted", "message": "Deleted the created note."}
