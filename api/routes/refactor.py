"""Note Refactor window — read-only analyzer for an Obsidian sub-folder (Phase 1).

Three endpoints:

* ``POST /api/refactor/plan`` — SSE, **zero vision calls, zero vault writes**.
  Streams ``info`` / ``error`` frames, a ``{"note": {...}}`` frame per analyzed
  note, and a terminal ``{"refactor": {...}}`` summary + advisory discrepancy
  report. Mirrors the ``/api/deck/generate`` worker-thread + queue plumbing.
* ``POST /api/refactor/extract-image`` — the ONLY vision-calling path, **one
  image at a time** (user-triggered). Writes only ``obsidian_cache/``.
* ``GET /api/refactor/image`` — read-only image bytes for the side-by-side
  full-res review in the UI.

This is the app's first write-capable workflow area, so Phase 1 deliberately
keeps every path either read-only (plan, image) or cache-only (extract-image);
no vault file is ever created, moved, or modified. Path validators reject
traversal / absolute / NUL and confirm the resolved real path stays under the
configured vault root (mirroring ``api/routes/deck.py``).
"""
import json
import os
import queue
import threading

from flask import Blueprint, Response, jsonify, request, send_file

from api.security import origin_is_local, sanitise_error_msg
from api.validators import coerce_bool, coerce_enum, coerce_string_max_len
from core.config import load_config
from core.constants import VAULT_IMAGE_EXTS, VAULT_MD_EXTS
from rag.vault import obsidian_manager

from refactor import apply as apply_mod
from refactor import archive as archive_mod
from refactor import extract, ignore, journal
from refactor.plan import build_plan

refactor_bp = Blueprint("refactor", __name__)

# Index states (mirror deck.py) — used only to warn, never to block.
_USABLE_STATES = {"done", "paused_partial"}
_IN_PROGRESS_STATES = {"running", "scanning", "embedding", "paused", "paused_scan"}

_SCOPE_MAX = 1024
_REL_MAX = 4096
_DEFAULT_SCOPE = ""  # no default scope; the user picks a folder (empty ⇒ 400)

# Op-lock TTL for the Phase 2 write paths. Apply/archive/restore are fast (no
# vision, no multi-hour work), but holding the SAME obsidian operation lock the
# indexer uses guarantees a write never races a concurrent index run. A held lock
# (indexing in progress) ⇒ 503; released in finally.
_WRITE_LOCK_TTL_S = 600
# Hard cap on the number of notes one /apply batch will touch — a guard against a
# malformed/oversized payload, not a UX limit (a scope has ~hundreds of notes).
_APPLY_MAX_NOTES = 5000

# The plan makes no vision calls, but hashing every referenced image is I/O; a
# generous stall backstop so a slow disk / large folder does not abort a healthy
# run. Each note still streams a frame, so the drain advances per note.
_PLAN_STALL_TIMEOUT_S = 600


def _vault_root_or_error():
    """Return ``(vault_root_str, None)`` or ``(None, (json, status))``."""
    vault_path = obsidian_manager.get_vault_path()
    if not vault_path:
        return None, (jsonify({"error": "Set the Obsidian vault path first."}), 400)
    real = os.path.realpath(os.path.expanduser(vault_path))
    if not os.path.isdir(real):
        return None, (jsonify({"error": "Configured vault path is not a directory."}), 400)
    return real, None


def _finalize_scope(real: str, vault_root: str):
    """Shared tail for both scope resolvers: reject the vault root itself /
    anything outside it / a non-directory, and return the vault-relative posix
    sub-folder. *real* must already be an absolute ``os.path.realpath`` result,
    as must *vault_root* (``_vault_root_or_error`` guarantees the latter)."""
    if real != vault_root and not real.startswith(vault_root + os.sep):
        return None
    if real == vault_root or not os.path.isdir(real):
        return None
    return os.path.relpath(real, vault_root).replace(os.sep, "/")


def _resolve_scope(raw, vault_root: str):
    """Validate a vault-relative scope sub-folder; return posix rel or None."""
    s = coerce_string_max_len(raw, _SCOPE_MAX)
    if s is None:
        return None
    s = s.strip().strip("/").replace("\\", "/")
    if not s or any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    if os.path.isabs(s) or ".." in s.split("/"):
        return None
    return _finalize_scope(os.path.realpath(os.path.join(vault_root, s)), vault_root)


def _abs_to_scope(abs_path, vault_root: str):
    """Convert an absolute path (e.g. from the native folder picker) to a
    vault-relative scope sub-folder, or None if it is the vault root itself or
    sits outside the vault. Keeps the single-sub-folder lock identical to manual
    entry — the picker is a convenience, not a way around ``_resolve_scope``."""
    if not isinstance(abs_path, str) or not abs_path:
        return None
    return _finalize_scope(os.path.realpath(os.path.expanduser(abs_path)), vault_root)


def _resolve_image_rel(raw, vault_root: str):
    """Validate a vault-relative image path; return canonical posix rel or None.

    Not scope-locked: attachments live in a central folder outside the refactor
    scope, so any image **under the vault root** is a legitimate read target.
    """
    s = coerce_string_max_len(raw, _REL_MAX)
    if s is None:
        return None
    s = s.strip().replace("\\", "/")
    if not s or any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    if os.path.isabs(s) or ".." in s.split("/"):
        return None
    if os.path.splitext(s)[1].lower() not in VAULT_IMAGE_EXTS:
        return None
    real = os.path.realpath(os.path.join(vault_root, s))
    if real != vault_root and not real.startswith(vault_root + os.sep):
        return None
    if not os.path.isfile(real):
        return None
    return os.path.relpath(real, vault_root).replace(os.sep, "/")


def _resolve_ignore_rel(raw, vault_root: str):
    """Validate an image rel-path for the ignore-list (shape + under-root only).

    Unlike ``_resolve_image_rel`` this does **not** require the file to exist:
    a user may un-ignore (or pre-ignore) a path whose bytes are not present
    locally — e.g. a dataless iCloud placeholder, or an entry left over after a
    file moved. Traversal / absolute / NUL / non-image-extension are still
    rejected, and the resolved real path must still stay under the vault root.
    """
    s = coerce_string_max_len(raw, _REL_MAX)
    if s is None:
        return None
    s = s.strip().replace("\\", "/")
    if not s or any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    if os.path.isabs(s) or ".." in s.split("/"):
        return None
    if os.path.splitext(s)[1].lower() not in VAULT_IMAGE_EXTS:
        return None
    real = os.path.realpath(os.path.join(vault_root, s))
    if real != vault_root and not real.startswith(vault_root + os.sep):
        return None
    return os.path.relpath(real, vault_root).replace(os.sep, "/")


def _resolve_scope_note_rel(raw, vault_root: str, scope: str):
    """Validate a vault-relative ``.md`` note path that MUST live under *scope*.

    The Phase 2 writers only ever rewrite notes inside the approved sub-folder, so
    a note path is scope-locked here (unlike images, which live vault-wide). Rejects
    traversal / absolute / NUL, non-``.md``, and any realpath outside
    ``<vault>/<scope>``. Does not require the file to exist on disk (the apply layer
    handles a vanished note as a per-note skip)."""
    s = coerce_string_max_len(raw, _REL_MAX)
    if s is None:
        return None
    s = s.strip().replace("\\", "/")
    if not s or any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    if os.path.isabs(s) or ".." in s.split("/"):
        return None
    if os.path.splitext(s)[1].lower() not in VAULT_MD_EXTS:
        return None
    scope_root = os.path.join(vault_root, scope)
    real = os.path.realpath(os.path.join(vault_root, s))
    if not real.startswith(os.path.realpath(scope_root) + os.sep):
        return None
    return os.path.relpath(real, vault_root).replace(os.sep, "/")


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


@refactor_bp.route("/api/refactor/plan", methods=["POST"])
def api_refactor_plan():
    """Analyze the scoped sub-folder read-only and stream proposals as SSE."""
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403

    vault_root, err = _vault_root_or_error()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    cfg = load_config()
    raw_scope = data.get("scope_subdir")
    if raw_scope is None:
        raw_scope = cfg.get("refactor_scope_subdir", _DEFAULT_SCOPE)
    scope = _resolve_scope(raw_scope, vault_root)
    if scope is None:
        return jsonify({
            "error": "Invalid scope sub-folder (must be an existing folder inside the vault)."
        }), 400

    def generate():
        cancel = threading.Event()
        event_q: queue.Queue = queue.Queue(maxsize=2048)
        _DONE = object()

        def _put(item):
            while not cancel.is_set():
                try:
                    event_q.put(item, timeout=1)
                    return
                except queue.Full:
                    continue

        def _worker():
            try:
                result = build_plan(
                    vault_root, scope, on_event=_put, stop=cancel.is_set,
                )
                if not cancel.is_set():
                    _put({"refactor": result.summary_frame()})
            except Exception as exc:  # noqa: BLE001 — surface as an SSE error
                if not cancel.is_set():
                    _put({"error": sanitise_error_msg(exc)})
            finally:
                try:
                    event_q.put(_DONE, timeout=5)
                except queue.Full:
                    pass

        # Preflight: warn (not block) about index state — the plan reuses the
        # on-disk image cache, which a partial/paused index leaves incomplete.
        try:
            state = obsidian_manager.get_status()
        except Exception:
            state = ""
        if state in _IN_PROGRESS_STATES:
            yield f"data: {json.dumps({'info': 'Indexing is in progress — image descriptions may be incomplete; misses show as “not extracted”.'})}\n\n"
        elif state not in _USABLE_STATES:
            yield f"data: {json.dumps({'info': 'No fully-built vault index detected — many images may show as “not extracted”. Index the vault for fuller coverage.'})}\n\n"

        threading.Thread(target=_worker, daemon=True).start()
        try:
            while True:
                try:
                    item = event_q.get(timeout=_PLAN_STALL_TIMEOUT_S)
                except queue.Empty:
                    cancel.set()
                    yield f"data: {json.dumps({'error': 'Plan analysis stalled. Please try again.'})}\n\n"
                    break
                if item is _DONE:
                    break
                yield f"data: {json.dumps(item)}\n\n"
                if isinstance(item, dict) and item.get("error"):
                    cancel.set()
                    break
        finally:
            cancel.set()
        yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream")


@refactor_bp.route("/api/refactor/extract-image", methods=["POST"])
def api_refactor_extract_image():
    """Run ONE fresh vision pass (table or description) for a single image."""
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403

    vault_root, err = _vault_root_or_error()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    rel = _resolve_image_rel(data.get("rel"), vault_root)
    if rel is None:
        return jsonify({"error": "Invalid or unreadable image path."}), 400
    mode = coerce_enum(data.get("mode"), ("table", "describe", "classify"))
    if mode is None:
        return jsonify({"error": "mode must be 'table', 'describe' or 'classify'."}), 400

    try:
        from pathlib import Path
        root = Path(vault_root)
        if mode == "table":
            cfg = load_config()
            double = coerce_bool(cfg.get("refactor_table_double_read"))
            if double is None:
                double = True
            res = extract.extract_table(rel, root, double_read=double)
        elif mode == "classify":
            res = extract.classify(rel, root)
        else:
            res = extract.redescribe(rel, root)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500

    if res.get("error"):
        return jsonify({"error": sanitise_error_msg(res["error"])}), 502
    return jsonify({"ok": True, "rel": rel, **res})


@refactor_bp.route("/api/refactor/image", methods=["GET"])
def api_refactor_image():
    """Stream a vault image's bytes (read-only) for side-by-side review."""
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403

    vault_root, err = _vault_root_or_error()
    if err:
        return err

    rel = _resolve_image_rel(request.args.get("rel"), vault_root)
    if rel is None:
        return jsonify({"error": "Invalid or unreadable image path."}), 400
    abs_path = os.path.join(vault_root, rel)
    try:
        return send_file(abs_path, conditional=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 400


@refactor_bp.route("/api/refactor/native-pick-folder", methods=["POST"])
def api_refactor_native_pick_folder():
    """Native folder picker → vault-relative scope sub-folder.

    Picks a folder via the PyWebView dialog (mirroring
    ``api/routes/deck.py``), then converts the absolute path to a
    vault-relative sub-folder with ``_abs_to_scope`` — so the picked scope
    obeys the same single-sub-folder lock (root + outside-vault rejected) as a
    manually typed value. The user can still type the scope by hand.
    """
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403

    vault_root, err = _vault_root_or_error()
    if err:
        return err

    import webview
    if not webview.windows:
        return jsonify({"error": "No active window"}), 503
    result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=False)
    if not result:
        return jsonify({"cancelled": True})
    chosen = result[0] if isinstance(result, (list, tuple)) else result
    scope = _abs_to_scope(chosen, vault_root)
    if scope is None:
        return jsonify({
            "error": "Selected folder must be a sub-folder inside the configured vault."
        }), 400
    return jsonify({"ok": True, "scope": scope})


@refactor_bp.route("/api/refactor/ignore", methods=["GET", "POST"])
def api_refactor_ignore():
    """Read or mutate the sticky per-vault image ignore-list.

    The list is a sidecar JSON under ``BASE_DIR/obsidian_cache/refactor/`` —
    **never** the vault (Phase 1 writes zero vault files). ``GET`` returns the
    current list; ``POST {rel, action: "add"|"remove"}`` toggles one image and
    returns the new list. Rel-paths are shape-validated (``_resolve_ignore_rel``)
    but not required to exist, so a moved/dataless image can still be toggled.
    """
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403

    vault_root, err = _vault_root_or_error()
    if err:
        return err

    from pathlib import Path
    root = Path(vault_root)

    if request.method == "GET":
        return jsonify({"ok": True, "ignored": ignore.list_ignored(root)})

    data = request.get_json(silent=True) or {}
    rel = _resolve_ignore_rel(data.get("rel"), vault_root)
    if rel is None:
        return jsonify({"error": "Invalid image path."}), 400
    action = coerce_enum(data.get("action"), ("add", "remove"))
    if action is None:
        return jsonify({"error": "action must be 'add' or 'remove'."}), 400

    try:
        if action == "add":
            ignored = ignore.add(root, rel)
        else:
            ignored = ignore.remove(root, rel)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({"ok": True, "rel": rel, "action": action, "ignored": ignored})


# ===========================================================================
# Phase 2 — vault WRITES. These are the app's first endpoints that modify the
# user's Obsidian vault. Each: requires a local origin, requires explicit
# ``confirm: true``, holds the obsidian operation lock for the duration (so a
# write never races a concurrent index run — a held lock ⇒ 503), and routes
# every mutation through the scope-locked, journalled writers in refactor/.
# ===========================================================================

def _scope_or_default(data, cfg, vault_root):
    raw_scope = data.get("scope_subdir")
    if raw_scope is None:
        raw_scope = cfg.get("refactor_scope_subdir", _DEFAULT_SCOPE)
    return _resolve_scope(raw_scope, vault_root)


@refactor_bp.route("/api/refactor/apply", methods=["POST"])
def api_refactor_apply():
    """Write the approved callout-only proposals to the vault (batch, atomic).

    Body: ``{scope_subdir?, confirm: true, notes: [{rel, content_sha256,
    proposed_sha256}]}``. Each note is independently applied with a stale-diff +
    WYSIWYG guard; one note's failure never aborts the rest. Returns per-note
    results. Holds the op lock; 503 if indexing is in progress."""
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    vault_root, err = _vault_root_or_error()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Apply must be explicitly confirmed (confirm: true)."}), 400
    cfg = load_config()
    if (e := _archive_dir_ok_or_error(vault_root, cfg)):
        return e
    scope = _scope_or_default(data, cfg, vault_root)
    if scope is None:
        return jsonify({"error": "Invalid scope sub-folder."}), 400

    raw_notes = data.get("notes")
    if not isinstance(raw_notes, list) or not raw_notes:
        return jsonify({"error": "notes must be a non-empty list."}), 400
    if len(raw_notes) > _APPLY_MAX_NOTES:
        return jsonify({"error": f"Too many notes (max {_APPLY_MAX_NOTES})."}), 400

    approved = []
    for n in raw_notes:
        if not isinstance(n, dict):
            return jsonify({"error": "Each note entry must be an object."}), 400
        rel = _resolve_scope_note_rel(n.get("rel"), vault_root, scope)
        ch = coerce_string_max_len(n.get("content_sha256"), 128)
        ph = coerce_string_max_len(n.get("proposed_sha256"), 128)
        if rel is None or not ch or not ph:
            return jsonify({"error": f"Invalid note entry: {n.get('rel')!r}"}), 400
        approved.append({"rel": rel, "content_sha256": ch.strip(), "proposed_sha256": ph.strip()})

    if not obsidian_manager.try_acquire_lock(ttl=_WRITE_LOCK_TTL_S):
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    try:
        from pathlib import Path
        results = apply_mod.apply_notes(Path(vault_root), cfg, approved)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock()

    applied = sum(1 for r in results if r.get("status") == "applied")
    return jsonify({"ok": True, "applied": applied, "results": results})


@refactor_bp.route("/api/refactor/archive", methods=["POST"])
def api_refactor_archive():
    """Move ONE image's full-res original out of the vault + leave a thumbnail.

    Body: ``{scope_subdir?, confirm: true, note_rel, image_rel, content_sha256}``.
    Refuses (no writes) when the image is referenced by any other note. Holds the
    op lock; 503 if indexing is in progress."""
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    vault_root, err = _vault_root_or_error()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Archive must be explicitly confirmed (confirm: true)."}), 400
    cfg = load_config()
    if (e := _archive_dir_ok_or_error(vault_root, cfg)):
        return e
    scope = _scope_or_default(data, cfg, vault_root)
    if scope is None:
        return jsonify({"error": "Invalid scope sub-folder."}), 400
    note_rel = _resolve_scope_note_rel(data.get("note_rel"), vault_root, scope)
    if note_rel is None:
        return jsonify({"error": "note_rel must be a .md note inside the scope sub-folder."}), 400
    image_rel = _resolve_image_rel(data.get("image_rel"), vault_root)
    if image_rel is None:
        return jsonify({"error": "Invalid or unreadable image path."}), 400
    content_sha256 = coerce_string_max_len(data.get("content_sha256"), 128)
    if not content_sha256:
        return jsonify({"error": "content_sha256 is required (stale-diff guard)."}), 400

    if not obsidian_manager.try_acquire_lock(ttl=_WRITE_LOCK_TTL_S):
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    try:
        from pathlib import Path
        result = archive_mod.archive_image(
            Path(vault_root), cfg, scope, note_rel, image_rel, content_sha256.strip())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock()

    status = 200 if result.get("ok") else (409 if result.get("shared") else 400)
    return jsonify(result), status


@refactor_bp.route("/api/refactor/manifest", methods=["GET"])
def api_refactor_manifest():
    """Return the restore manifest's op list (newest last) for the restore UI."""
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    vault_root, err = _vault_root_or_error()
    if err:
        return err
    cfg = load_config()
    if (e := _archive_dir_ok_or_error(vault_root, cfg)):
        return e
    try:
        from pathlib import Path
        manifest = journal.load(Path(vault_root), cfg)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({"ok": True, "ops": manifest.get("ops", [])})


@refactor_bp.route("/api/refactor/restore", methods=["POST"])
def api_refactor_restore():
    """Reverse one manifest op (``{op_id}``) or every applied op (``{all: true}``).

    Restores note bodies from snapshots and archived originals back to the vault,
    removing thumbnails. Holds the op lock; 503 if indexing is in progress."""
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    vault_root, err = _vault_root_or_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    if (e := _archive_dir_ok_or_error(vault_root, cfg)):
        return e
    op_id = coerce_string_max_len(data.get("op_id"), 128)
    do_all = coerce_bool(data.get("all")) is True
    if not op_id and not do_all:
        return jsonify({"error": "Provide op_id or all: true."}), 400

    if not obsidian_manager.try_acquire_lock(ttl=_WRITE_LOCK_TTL_S):
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    try:
        from pathlib import Path
        root = Path(vault_root)
        manifest = journal.load(root, cfg)
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
            r = journal.revert_op(root, cfg, op)
            results.append({"op_id": op.get("id"), **r})
            # Persist after EACH revert (not once after the loop): revert_op has
            # already mutated the vault + flipped op['state'], so a crash or an
            # exception before a single end-of-loop save would leave those on-disk
            # reverts unrecorded in the manifest. Per-op saves keep the manifest
            # consistent with disk at every step (mirrors apply_notes).
            journal.save(root, cfg, manifest)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock()

    reverted = sum(1 for r in results if r.get("status") == "reverted")
    return jsonify({"ok": True, "reverted": reverted, "results": results})
