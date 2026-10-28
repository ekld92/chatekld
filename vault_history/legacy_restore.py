"""Restore historical note edits and archived images; no new editing actions."""
import hashlib
import os
from pathlib import Path
from core.utils import write_bytes_atomic
from vault_history import journal

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()

def revert_apply_note(vault_root: Path, cfg: dict, op: dict) -> dict:
    """Restore a note from its pre-apply snapshot. Mutates ``op['state']``.

    Conservative: only reverts when the note still matches the bytes this op
    wrote (``hash_after``). If it was edited again afterwards, it skips rather
    than clobber the newer content. Caller persists the manifest.
    """
    rel = op.get("note_rel", "")
    note_path = vault_root / rel
    try:
        cur = note_path.read_bytes()
    except OSError as exc:
        return {"ok": False, "status": "failed", "message": f"unreadable note ({type(exc).__name__})"}

    cur_hash = sha256_bytes(cur)
    if cur_hash == op.get("hash_before"):
        op["state"] = "reverted"
        op["reverted_ts"] = journal.now_iso()
        return {"ok": True, "status": "already_original", "message": "Note already at its pre-apply content."}
    if cur_hash != op.get("hash_after"):
        return {"ok": False, "status": "skipped",
                "message": "Note changed since apply; not reverting to avoid clobbering newer edits."}

    try:
        snap = journal.read_snapshot(vault_root, cfg, op.get("snapshot_rel", ""))
    except (OSError, journal.ScopeError) as exc:
        return {"ok": False, "status": "failed", "message": f"snapshot unavailable ({type(exc).__name__})"}

    journal.assert_under(note_path, vault_root)
    write_bytes_atomic(str(note_path), snap)
    journal.log_vault_write("restore_note", rel, f"→{op.get('hash_before','')[:8]}")
    op["state"] = "reverted"
    op["reverted_ts"] = journal.now_iso()
    return {"ok": True, "status": "reverted", "message": "Note restored from snapshot."}


def revert_archive_image(vault_root: Path, cfg: dict, op: dict) -> dict:
    """Reverse one archive op — **atomic-or-nothing**.

    A *partial* revert is dangerous: e.g. moving the original back and deleting the
    thumbnail while the note still embeds that thumbnail would leave a BROKEN EMBED;
    and blindly writing the original back could clobber a *different* file the user
    re-created at that path. So this runs every conflict check FIRST and, if any
    would force an inconsistent or destructive outcome, refuses the whole op WITHOUT
    touching anything (mirroring ``revert_apply_note``). Only when all checks pass
    does it mutate, in the safe order: restore the original file → restore the note
    body → drop the thumbnail → remove the archive copy. By the time the thumbnail
    is deleted the note is guaranteed to embed the *original* again (restored,
    already-original, or gone), so the deletion can never orphan a live embed.
    Mutates ``op['state']``; caller persists the manifest.
    """
    vault_root = Path(vault_root)
    arch_root = journal.archive_dir(vault_root, cfg)
    note_rel = op.get("note_rel", "")
    note_path = vault_root / note_rel
    image_rel = op.get("image_rel", "")
    arch_src = arch_root / op.get("archive_rel", "")
    thumb_rel = op.get("thumb_rel", "")
    snap_rel = op.get("note_snapshot_rel", "")

    # --- PRE-FLIGHT (no writes): decide what we WILL do, or refuse outright. ---

    # (a) Note conflict. Decide whether to restore the note body. If the note was
    #     edited after archiving and is neither the body we wrote nor already back
    #     at its pre-archive form, restoring would clobber those edits — and the
    #     note still embeds the thumbnail we are about to delete, so half-reverting
    #     would break that embed. Refuse the whole op. (A deleted note ⇒ skip the
    #     body restore but still reclaim the original/thumbnail.)
    restore_note = False
    if snap_rel and note_path.exists():
        try:
            cur_hash = sha256_bytes(note_path.read_bytes())
        except OSError as exc:
            return {"ok": False, "status": "failed",
                    "message": f"unreadable note ({type(exc).__name__})"}
        if cur_hash == op.get("note_hash_after"):
            restore_note = True            # still the body we wrote → safe to revert
        elif cur_hash == op.get("note_hash_before"):
            restore_note = False           # already pre-archive (embeds original) → nothing to do
        else:
            return {"ok": False, "status": "skipped",
                    "message": "Note changed since archiving; not reverting (would break the "
                               "thumbnail embed or clobber later edits). Resolve it by hand first."}

    # (b) Original-path check — FLAG-INDEPENDENT (A2, 2026-07-13). The
    #     `original_deleted` flag is persisted after the unlink, so a crash in
    #     that window can leave it stale-False while the original is already
    #     gone; keying this block on the flag (the old code) then skipped the
    #     move-back and let step 4 delete the only surviving copy. Decide from
    #     what is actually on disk at image_rel instead.
    move_original = False
    dest = vault_root / image_rel
    if not dest.exists():
        # The original is absent (deleted at archive time, or a crash-stale
        # flag). Move the archived copy back if we still hold it.
        if Path(arch_src).exists():
            move_original = True
        elif op.get("original_deleted"):
            # We recorded the delete AND the archive copy is gone → nothing to
            # restore from. (Flag False + no copy + no original means we never
            # deleted it; the note-body restore below is still valid work.)
            return {"ok": False, "status": "failed",
                    "message": "Archived original is missing; cannot restore."}
    else:
        try:
            already = sha256_bytes(dest.read_bytes()) == op.get("digest")
        except OSError as exc:
            return {"ok": False, "status": "failed",
                    "message": f"cannot inspect the original path ({type(exc).__name__})"}
        if already:
            move_original = False                 # identical bytes already there → idempotent skip
        elif op.get("original_deleted"):
            # We deleted the original, yet a DIFFERENT file now occupies the
            # path (user created one meanwhile) — refuse rather than clobber it.
            return {"ok": False, "status": "skipped",
                    "message": "A different file now occupies the original path; not "
                               "overwriting it. Move it aside, then restore."}
        else:
            # `original_deleted` False and the on-disk bytes differ from the
            # archived copy: the original was left in place but changed during
            # archiving (scenario B). Leave the user's bytes; the note-body
            # restore re-embeds them and step 4 KEEPS the pre-change archive
            # copy (its digest guard sees the mismatch).
            move_original = False

    # --- MUTATE (all checks passed; safe order) -----------------------------
    warnings: list[str] = []

    # 1) original back to its vault path.
    if move_original:
        try:
            journal.assert_under(arch_src, arch_root)
            data = Path(arch_src).read_bytes()
            dest = vault_root / image_rel
            journal.assert_under(dest, vault_root)
            write_bytes_atomic(str(dest), data)
            journal.log_vault_write("restore_image", image_rel, f"← {op.get('archive_rel','')}")
        except (OSError, journal.ScopeError) as exc:
            return {"ok": False, "status": "failed",
                    "message": f"could not restore original ({type(exc).__name__})"}

    # 2) note body back from snapshot (re-embeds the original).
    if restore_note:
        try:
            snap = journal.read_snapshot(vault_root, cfg, snap_rel)
            journal.assert_under(note_path, vault_root)
            write_bytes_atomic(str(note_path), snap)
            journal.log_vault_write("restore_note", note_rel, "thumbnail→embed")
        except (OSError, journal.ScopeError) as exc:
            # CRITICAL — do NOT fall through to the thumbnail deletion. The
            # pre-flight chose restore_note=True because the note still embeds the
            # *thumbnail*; the snapshot write that would re-point it at the original
            # just failed, so the note STILL embeds the thumbnail. Deleting that
            # thumbnail in step 3 would orphan a live embed (the exact corruption
            # this "atomic-or-nothing" reverter exists to prevent). Abort here with
            # the thumbnail + archive copy left in place and the op still "applied"
            # so the user can retry: step 1 (move original back) is idempotent — the
            # restored original is detected and skipped on the next attempt — so the
            # only side effect left behind is a now-unreferenced original copy, which
            # is harmless and self-heals on retry.
            return {"ok": False, "status": "failed",
                    "message": (f"could not restore the note from its snapshot "
                                f"({type(exc).__name__}); thumbnail left in place to avoid a "
                                "broken embed — resolve the snapshot issue and retry the restore.")}

    # 3) drop the thumbnail (best-effort). SAFE ONLY HERE: every path that reaches
    #    this point has the note embedding the ORIGINAL again — restore_note
    #    succeeded (its failure path returns above), or restore_note was False
    #    because the note is already pre-archive / was deleted. So the thumbnail can
    #    never still be a live embed at this point.
    if thumb_rel:
        try:
            tp = vault_root / thumb_rel
            journal.assert_under(tp, vault_root)
            if tp.exists():
                os.unlink(tp)
                # m4: audit the in-vault thumbnail removal (write was logged; so
                # is the delete now) so a vault change stays attributable.
                journal.log_vault_write("delete_thumb", thumb_rel, "archive restore")
        except (OSError, journal.ScopeError):
            warnings.append("thumbnail not removed")

    # 4) remove the now-redundant archive copy — ONLY when the archived bytes
    #    are provably back in the vault (A2, 2026-07-13). Deleting it
    #    unconditionally could destroy the only copy: after a crash-stale flag
    #    step 1 may not have run, or the file at image_rel may hold DIFFERENT
    #    bytes (scenario B — changed original left in place). Guard on: image_rel
    #    exists AND its sha256 == the archived digest; otherwise keep + warn.
    try:
        if Path(arch_src).exists():
            dest_now = vault_root / image_rel
            restored_ok = False
            try:
                restored_ok = (
                    dest_now.is_file()
                    and sha256_bytes(dest_now.read_bytes()) == op.get("digest")
                )
            except OSError:
                restored_ok = False
            if restored_ok:
                os.unlink(arch_src)
            else:
                warnings.append(
                    "archive copy kept: the vault original differs from the archived bytes"
                )
    except OSError:
        pass

    op["state"] = "reverted"
    op["reverted_ts"] = journal.now_iso()
    msg = "image restored to the vault"
    if warnings:
        msg += " (" + "; ".join(warnings) + ")"
    return {"ok": True, "status": "reverted", "message": msg}
