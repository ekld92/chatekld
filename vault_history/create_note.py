"""Journalled creation of a NEW vault note (docgen Markdown long-form output).

Adds a third vault-write op kind to the vault history journal — ``create_note`` —
whose Restore DELETES the created file (there is no prior content to snapshot,
so the inverse of "create" is "delete", not "restore a snapshot"). Routed
through :mod:`vault_history.journal` so the existing Restore UI + manifest cover
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
from typing import Any, Optional

from core.utils import CreatePublicationUncertainError, create_text_atomic
from vault_history import journal


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def create_note(
    vault_root: Path, cfg: dict, rel: str, text: str, manifest: dict,
    *, prune: bool = True, save: bool = True,
) -> tuple[str, str, Optional[str]]:
    """Create a NEW note at ``<vault>/<rel>`` with *text*, journalled + reversible.

    Returns ``(op_id, status, error)`` where *status* is ``"applied"`` or
    ``"failed"`` or ``"uncertain"`` (published bytes with unconfirmed durability
    or recovery journal). Refuses (``failed``) if a file already exists at *rel* — the
    caller validated *rel* is under the vault; this guards the create-only
    contract. Appends the op and persists the manifest (caller holds the op lock).

    ``prune``/``save`` exist for BATCH callers (``/api/notesync/create``) and
    default to the historical single-note behaviour, so the docgen path is
    unchanged. A batch must pass ``prune=False``: pruning per note lets the
    cap evict the batch's own earliest ops — their snapshots with them — while
    each response still reports an ``op_id``. It prunes ONCE at the end with
    ``protect_op_ids`` instead. ``save=False`` likewise lets a batch checkpoint
    on its own cadence rather than rewriting the whole manifest per note; the
    FAILURE path always saves regardless, because a crash trail that records an
    attempted write is the one record that must be durable immediately.
    """
    note_path = vault_root / rel
    if note_path.exists():
        return "", "failed", "a note already exists at that path"

    op_id = journal.new_op_id(manifest)
    raw = text.encode("utf-8")
    hash_after = _sha(raw)
    op: dict[str, Any] = {
        "id": op_id, "kind": "create_note", "ts": journal.now_iso(),
        "note_rel": rel, "hash_before": "", "hash_after": hash_after,
        "snapshot_rel": "", "state": "applied",
    }
    manifest["ops"].append(op)
    journal.log_vault_write("create_note", rel, f"∅→{hash_after[:8]}")

    try:
        journal.assert_under(note_path, vault_root)  # defence-in-depth
        os.makedirs(os.path.dirname(str(note_path)) or str(vault_root), exist_ok=True)
        create_text_atomic(str(note_path), text)
    except CreatePublicationUncertainError as exc:
        # The complete file exists: retain its reversible applied record while
        # reporting the uncertain durability outcome to the caller.
        op["publication_uncertain"] = True
        journal.log_vault_write("create_note_uncertain", rel, str(exc))
        try:
            journal.save(vault_root, cfg, manifest)
        except Exception as journal_exc:  # noqa: BLE001 — preserve the published receipt
            return op_id, "uncertain", (
                f"{exc}; recovery journal could not be confirmed "
                f"({type(journal_exc).__name__}). Do not assume Restore is available."
            )
        return op_id, "uncertain", str(exc)
    except (OSError, journal.ScopeError) as exc:
        # Audit-trail completeness (audit #11): the create_note line above was
        # logged BEFORE the write (journal-before-write, matching
        # journalled_write_note's intent-first order) — emit the failure
        # counterpart so the human-readable trail never asserts a create that
        # never reached disk (the manifest op alone records `failed`, but the
        # log is the recovery record when the manifest is lost).
        journal.log_vault_write("create_note_failed", rel, type(exc).__name__)
        op["state"] = "failed"
        journal.save(vault_root, cfg, manifest)   # always: crash-trail durability
        return op_id, "failed", f"write failed ({type(exc).__name__})"

    try:
        if prune:
            journal.prune(vault_root, cfg, manifest)
        if save:
            journal.save(vault_root, cfg, manifest)
    except Exception as exc:  # noqa: BLE001 — publication already happened; retain its receipt
        op["publication_uncertain"] = True
        return op_id, "uncertain", (
            "Note published, but its recovery journal could not be confirmed "
            f"({type(exc).__name__}). Do not assume Restore is available."
        )
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
