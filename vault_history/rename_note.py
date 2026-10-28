"""Journalled rename of a vault note (a reference note whose citation key changed).

Adds the ``rename_note`` op kind to the vault history journal, so the existing
Note Refactor → Restore UI undoes it with no new UI. The caller (the
``/api/notesync/rename-note`` route) derives the new name and text, validates
both, and holds the obsidian op-lock.

The move is NO-REPLACE: the new file is published with ``os.link`` from a
temporary sibling, which fails rather than overwrite if a note appeared at
the destination in the meantime. The old file is removed only after the new
one exists, so a crash between the two leaves both copies (never neither);
the op is journalled ``applied`` before either, and its revert handles both
states.

Removing the old name is itself guarded (library-workflow plan lot 1,
defect 3, 2026-09-21). The source is verified against *before* at the start,
but the journal save and the publish come between that check and the
removal, and an external editor (Obsidian, a sync client) is not bound by any
lock of ours — so the old code's ``old_path.unlink()`` deleted, BY PATH,
whatever file held the old name at that instant, including an edit or a
replacement made in that window. Now the old file is first moved aside with
one atomic ``rename`` to a hidden sibling in the same directory; the moved
bytes are compared with *before*; and only a match is deleted. A mismatch is
an external change: the moved file is put back under the old name
(no-replace), our freshly published copy — built from the pre-edit bytes — is
withdrawn if it still holds exactly what we wrote, and the op is recorded
``failed``. The user's edit is then the only version of the note.

Residual window, stated rather than hidden: a program that keeps the old file
OPEN and writes into it after the atomic move, but before the verification
read, is caught (its bytes differ); one that writes between the verification
read and the final unlink of the hidden sibling is not. That window is two
system calls wide and needs an editor writing in place through a descriptor
opened before the move; editors that save by writing the PATH (Obsidian's
normal save) recreate the old name instead, which is detected and reported.

Crash recovery: a crash after the move aside leaves the old bytes in a hidden
``.<name>.renaming-<op id>`` file beside the note, with the op ``applied``.
Reverting the op from Note Refactor → Restore rebuilds the old name from the
journal snapshot (the same bytes, verified at the start); the hidden file is
then a duplicate and can be deleted by hand. Nothing is lost in either order.

Restore is conservative, like every history restore: it puts the original
bytes back under the original name only while the renamed note still holds
exactly the bytes this op wrote, and the original name is free. A note edited
after the rename is left alone and reported ``skipped``.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

from vault_history import journal


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _publish_no_replace(dest: Path, data: bytes) -> None:
    """Write *data* at *dest*, refusing (``FileExistsError``) if it exists."""
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, dest)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    try:
        dfd = os.open(str(dest.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def rename_note(
    vault_root: Path, cfg: dict, old_rel: str, new_rel: str,
    before: bytes, after: bytes, manifest: dict, *, detail: str = "",
) -> tuple[str, str, Optional[str]]:
    """Move the note at *old_rel* (holding *before*) to *new_rel* holding *after*.

    Returns ``(op_id, status, error)``, *status* ``"applied"`` or ``"failed"``.
    Refuses when the source no longer holds *before* (edited since the caller
    read it) or the destination exists. The op and a snapshot of *before* are
    durable before any file moves.
    """
    old_path, new_path = vault_root / old_rel, vault_root / new_rel
    try:
        journal.assert_under(old_path, vault_root)
        journal.assert_under(new_path, vault_root)
    except journal.ScopeError as exc:
        return "", "failed", str(exc)
    if new_path.exists():
        return "", "failed", "a note already exists at the new name"
    try:
        if _sha(old_path.read_bytes()) != _sha(before):
            return "", "failed", "the note changed since it was read"
    except OSError as exc:
        return "", "failed", f"read failed ({type(exc).__name__})"

    op_id = journal.new_op_id(manifest)
    snapshot_rel = journal.write_note_snapshot(vault_root, cfg, old_rel, op_id, before)
    op = {
        "id": op_id, "kind": "rename_note", "ts": journal.now_iso(),
        "note_rel": new_rel, "old_rel": old_rel,
        "hash_before": _sha(before), "hash_after": _sha(after),
        "snapshot_rel": snapshot_rel, "state": "applied", "detail": detail,
    }
    manifest["ops"].append(op)
    journal.save(vault_root, cfg, manifest)          # crash trail first
    journal.log_vault_write("rename_note", old_rel, f"→{new_rel}")
    try:
        _publish_no_replace(new_path, after)
    except (OSError, FileExistsError) as exc:
        journal.log_vault_write("rename_note_failed", old_rel, type(exc).__name__)
        op["state"] = "failed"
        journal.save(vault_root, cfg, manifest)
        return op_id, "failed", f"write failed ({type(exc).__name__})"
    outcome, detail_msg = _retire_old(old_path, new_path, before, after, op_id)
    stash = old_path.with_name(f".{old_path.name}.renaming-{op_id}")
    if stash.exists():
        # The user's bytes may now live ONLY in this hidden file (put-back
        # refused because the old name was recreated). A failed op is pruned
        # with its snapshot, so the durable pointer is the vault-write log
        # and the op record itself (2026-09-21 review).
        stash_rel = str(stash.relative_to(vault_root))
        op["stash_rel"] = stash_rel
        journal.log_vault_write("rename_note_stash_kept", stash_rel,
                                "a version of the note was kept in this hidden file")
    if outcome == "external_change":
        journal.log_vault_write("rename_note_failed", old_rel, "changed during the rename")
        op["state"] = "failed"
        journal.save(vault_root, cfg, manifest)
        return op_id, "failed", detail_msg
    if outcome == "partial":
        # The new note exists and the old one too: nothing is lost, and the
        # revert handles this state. Report it rather than pretend.
        journal.log_vault_write("rename_note_partial", old_rel, detail_msg or "")
        journal.prune(vault_root, cfg, manifest)
        journal.save(vault_root, cfg, manifest)
        return op_id, "applied", detail_msg
    journal.prune(vault_root, cfg, manifest)
    journal.save(vault_root, cfg, manifest)
    return op_id, "applied", detail_msg


def _retire_old(old_path: Path, new_path: Path, before: bytes, after: bytes,
                op_id: str) -> tuple[str, Optional[str]]:
    """Remove the old name only if it still holds *before* (see the module doc).

    Returns ``(outcome, message)``: ``"done"`` (removed; message set only when
    the old name was recreated meanwhile), ``"partial"`` (the old file could
    not be moved aside — both copies remain) or ``"external_change"`` (the old
    file changed after verification — it is restored and our copy withdrawn).
    """
    stash = old_path.with_name(f".{old_path.name}.renaming-{op_id}")
    try:
        # One atomic rename: from here on no writer of the PATH can reach the
        # bytes we are about to judge, and nothing is lost if we stop here.
        os.rename(old_path, stash)
    except FileNotFoundError:
        # Removed externally in the window: the new note exists, nothing to do.
        return "done", "the old note had already been removed"
    except OSError as exc:
        return "partial", (f"renamed, but the old note could not be removed "
                           f"({type(exc).__name__}); delete it by hand")
    moved: Optional[bytes] = None
    read_error: Optional[str] = None
    try:
        moved = stash.read_bytes()
    except OSError as exc:
        read_error = type(exc).__name__
    if moved is not None and _sha(moved) == _sha(before):
        try:
            stash.unlink()
        except OSError as exc:
            return "partial", (f"renamed; the old note was moved to {stash.name} but "
                               f"could not be deleted ({type(exc).__name__})")
        if old_path.exists():
            return "done", ("renamed, but a note was saved again under the old name "
                            "meanwhile; it was left in place")
        return "done", None
    # Changed (or unreadable) after verification: an external edit. Put the
    # moved file back under the old name without replacing anything that
    # appeared there, then withdraw our copy built from the pre-edit bytes.
    put_back = False
    try:
        os.link(stash, old_path)
        stash.unlink()
        put_back = True
    except OSError:
        pass
    withdrawn = False
    try:
        if new_path.read_bytes() == after:
            new_path.unlink()
            withdrawn = True
    except OSError:
        pass
    where = "kept under its old name" if put_back else f"kept as {stash.name}"
    ours = ("the renamed copy was withdrawn" if withdrawn
            else f"the renamed copy {new_path.name} was left in place — compare them")
    why = f" (unreadable: {read_error})" if read_error else ""
    return "external_change", (f"the note changed during the rename{why}; your version "
                               f"is {where} and {ours}")


def revert_rename_note(vault_root: Path, cfg: dict, op: dict) -> dict:
    """Undo a ``rename_note`` op. Returns ``{ok, status, message}``."""
    old_rel, new_rel = op.get("old_rel"), op.get("note_rel")
    if not old_rel or not new_rel:
        return {"ok": False, "status": "error", "message": "The op names no paths."}
    old_path, new_path = vault_root / old_rel, vault_root / new_rel
    try:
        journal.assert_under(old_path, vault_root)
        journal.assert_under(new_path, vault_root)
    except journal.ScopeError as exc:
        return {"ok": False, "status": "error", "message": str(exc)}
    new_bytes = new_path.read_bytes() if new_path.exists() else None
    if new_bytes is not None and _sha(new_bytes) != op.get("hash_after"):
        return {"ok": False, "status": "skipped",
                "message": "The renamed note changed since the rename — left in place."}
    if old_path.exists():
        if _sha(old_path.read_bytes()) != op.get("hash_before"):
            return {"ok": False, "status": "skipped",
                    "message": "A different note now holds the original name — left in place."}
    else:
        try:
            before = journal.read_snapshot(vault_root, cfg, op.get("snapshot_rel") or "")
        except (OSError, journal.ScopeError) as exc:
            return {"ok": False, "status": "error",
                    "message": f"snapshot unreadable ({type(exc).__name__})"}
        try:
            _publish_no_replace(old_path, before)
        except (OSError, FileExistsError) as exc:
            return {"ok": False, "status": "error",
                    "message": f"restore failed ({type(exc).__name__})"}
    if new_bytes is not None:
        try:
            new_path.unlink()
        except OSError as exc:
            return {"ok": False, "status": "error",
                    "message": f"the original is back, but the renamed copy could not be "
                               f"removed ({type(exc).__name__}); delete it by hand"}
    journal.log_vault_write("restore_rename_note", new_rel, f"→{old_rel}")
    op["state"] = "reverted"
    return {"ok": True, "status": "reverted",
            "message": f"Renamed back to {Path(old_rel).name}."}
