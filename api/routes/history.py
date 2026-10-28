"""Independent vault history listing and explicitly confirmed restoration."""
import os
from flask import Blueprint, jsonify, request
from api.security import sanitise_error_msg
from api.validators import coerce_bool, coerce_string_max_len
from core.config import load_config
from rag.vault import obsidian_manager
from vault_history import journal

history_bp = Blueprint("history", __name__)
_WRITE_LOCK_TTL_S = 600

def _vault_root_or_error():
    """Return ``(vault_root_str, None)`` or ``(None, (json, status))``."""
    vault_path = obsidian_manager.get_vault_path()
    if not vault_path:
        return None, (jsonify({"error": "Set the Obsidian vault path first."}), 400)
    real = os.path.realpath(os.path.expanduser(vault_path))
    if not os.path.isdir(real):
        return None, (jsonify({"error": "Configured vault path is not a directory."}), 400)
    return real, None


def _archive_dir_ok_or_error(vault_root: str, cfg: dict):
    """Return ``None`` if the configured archive dir resolves outside the vault,
    else an ``(json, status)`` 400 error. A misconfigured ``refactor_archive_dir``
    (e.g. pointed inside the vault) is a user error worth a clear message rather
    than a sanitised 500 from deep in the writer."""
    from pathlib import Path
    try:
        journal.archive_dir(Path(vault_root), cfg)
    except journal.ScopeError:
        return (jsonify({
            "error": "refactor_archive_dir must resolve to a folder OUTSIDE the vault."
        }), 400)
    return None


@history_bp.route("/api/history/manifest", methods=["GET"])
def api_history_manifest():
    """Return the restore manifest's op list (newest last) for the restore UI."""
    vault_root, err = _vault_root_or_error()
    if err:
        return err
    cfg = load_config()
    if (e := _archive_dir_ok_or_error(vault_root, cfg)):
        return e
    try:
        from pathlib import Path
        manifest = journal.load(Path(vault_root), cfg)
    except journal.JournalUnreadableError as exc:
        # Read-only route, but the same message matters: the history exists and
        # is intact on disk — nothing should "fix" it by overwriting.
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({"ok": True, "ops": manifest.get("ops", [])})


@history_bp.route("/api/history/restore", methods=["POST"])
def api_history_restore():
    """Reverse one manifest op (``{op_id}``) or every applied op (``{all: true}``).

    Restores note bodies from snapshots and archived originals back to the vault,
    removing thumbnails. Holds the op lock; 503 if indexing is in progress."""
    vault_root, err = _vault_root_or_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    # B8 (2026-07-13): restore mutates the vault (rewrites note bodies, moves
    # images, DELETES docgen-created notes) — gate it on confirm like the other
    # vault-write endpoints. The JS client already sends confirm: true; the
    # server never enforced it (doc/contract drift).
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Restore must be explicitly confirmed (confirm: true)."}), 400
    cfg = load_config()
    if (e := _archive_dir_ok_or_error(vault_root, cfg)):
        return e
    op_id = coerce_string_max_len(data.get("op_id"), 128)
    do_all = coerce_bool(data.get("all")) is True
    if not op_id and not do_all:
        return jsonify({"error": "Provide op_id or all: true."}), 400

    # Per-operation epoch token (improvement plan 2026-07-04, item 1.5): the
    # token stays on this request's stack — release/heartbeat below pass it
    # back, so a zombie holder (TTL-expired or cancelled) can never release or
    # extend a newer acquisition, and a newer acquisition can never clobber the
    # token a still-running previous holder captured (the old shared
    # ``_lock_epoch`` attribute allowed exactly that).
    op_epoch = obsidian_manager.try_acquire_lock(ttl=_WRITE_LOCK_TTL_S)
    if not op_epoch:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    try:
        from pathlib import Path
        root = Path(vault_root)
        manifest = journal.load(root, cfg)
        # Rename publication is a multi-entry hard-link transaction. A process
        # crash can leave its durable journal in ``pending`` or an applied
        # restore with both names linked to the same inode. Reconcile only
        # while this route owns the vault writer lock, before selecting restore
        # targets; foreign files are never removed because reconciliation is
        # anchored to the recorded inode/hash.
        if any(op.get("kind") == "rename_attachment"
               for op in manifest.get("ops", [])):
            # The private-only import remains lazy and conditional so a public
            # artifact with Maintenance stripped can still restore every
            # public journal kind.
            from maintenance.rename import reconcile_pending_renames
            reconcile_pending_renames(root, cfg, manifest)
        if do_all:
            # Newest first so a note touched by an apply then an archive reverts
            # in reverse order of application.
            targets = [op for op in reversed(manifest.get("ops", []))
                       if op.get("state") != "reverted"]
        else:
            op = journal.find_op(manifest, op_id.strip())
            if op is None:
                return jsonify({"error": "Unknown op_id."}), 404
            targets = [op]
        results = []
        for op in targets:
            # B8 (2026-07-13): heartbeat per op so a long {all:true} restore over
            # many ops (large archived images copied back on a slow disk) cannot
            # let the 600 s op-lock TTL lapse mid-restore and admit a second
            # writer — parity with the apply/normalize/archive writers.
            # Finding 1 (2026-07-20 audit): act on the heartbeat's return. If it
            # is False the TTL already lapsed and another operation took the
            # lock, so any further revert would write as a second concurrent
            # writer. Stop cleanly — the manifest is already persisted per-op, so
            # the reverts done so far are durable — and surface the truncation.
            if not obsidian_manager.heartbeat(op_epoch):
                results.append({"op_id": None, "status": "aborted",
                                "message": "lock_lost: restore stopped to avoid "
                                           "a concurrent writer; retry it."})
                break
            r = journal.revert_op(root, cfg, op)
            results.append({"op_id": op.get("id"), **r})
            # Persist after EACH revert (not once after the loop): revert_op has
            # already mutated the vault + flipped op['state'], so a crash or an
            # exception before a single end-of-loop save would leave those on-disk
            # reverts unrecorded in the manifest. Per-op saves keep the manifest
            # consistent with disk at every step (mirrors apply_notes).
            journal.save(root, cfg, manifest)
        # Reverted ops are now spent — prune them (and their snapshots) plus any
        # over-cap history so the manifest/`.bak` disk stays bounded.
        journal.prune(root, cfg, manifest)
        journal.save(root, cfg, manifest)
    except journal.JournalUnreadableError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)

    reverted = sum(1 for r in results if r.get("status") == "reverted")
    if reverted:
        # A restore may have reversed an archive (moved an image back into the
        # vault + removed its thumbnail), changing the file set. Drop the cached
        # whole-vault index so the next plan/analyze sees the restored files.
        from core.vault_resolver import invalidate_index_cache
        invalidate_index_cache(vault_root)
    return jsonify({"ok": True, "reverted": reverted, "results": results})
