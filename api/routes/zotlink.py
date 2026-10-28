"""``/api/zotlink/*`` — remove a file from a Zotero item without deleting it.

A Library Audit sub-view in the same sense the Tagging and Note Sync tabs are:
it reads the audit's cached inventory and lives in its own blueprint so the
vendored ``audit/`` tree keeps its read-only posture.

**Why this exists.** An attachment Zotero holds points at a file the vault
already owns under ``biblio_articles/``; removing it never touches that file
or ``mapping.json``. **It can change pairing, since 2026-09-19:** a
``linked_file`` attachment is the bridge's FIRST rung (the file Zotero links
under an item belongs to that item), so trashing one hands the PDF back to the
rest of the ladder — the maintainer's hand decisions, the note's ``pdf:`` list,
the DOI, the filename — at the next scan. Before that date the join never
consulted an attachment at all, and this paragraph used to argue safety from
it; the verb is still safe because trash is reversible, not because pairing
ignores it.

**Trash, never delete.** ``PATCH {"deleted": 1}`` is the only write here.

* It is exactly reversible (``{"deleted": 0}``) and keeps the item's KEY, so
  its relations and any annotations survive the round trip. ``DELETE
  /items/<key>`` destroys all three, and re-creating the attachment afterwards
  mints a NEW key — that is not an undo.
* For an ``imported_*`` attachment the bytes live in ``~/Zotero/storage/<key>/``
  and would go with a permanent delete. Trashing leaves them on disk until the
  user empties Zotero's trash, which is what makes "without deleting the PDF"
  true for every link mode rather than only for linked files.

**Annotations are the disclosed cost.** Zotero highlights hang off the
ATTACHMENT, not the parent, and are trashed with it. The count rides every row
and every confirm; a row that carries annotations needs a second explicit
acknowledgement, exactly as the notesync region overwrite does.

The write is journalled in ``tagging/journal.py`` (kind
``zotero_attachment_trash``) beside the other external Zotero writes, so the
existing Tagging → Restore panel reverts it with no new UI — the same reason
the Maintenance renamer put its op kind in the refactor journal.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from api.routes._batch_journal import _BatchJournalKeeper
from api.security import sanitise_error_msg
from api.sse import online_aware_floor_s, run_sse_worker
from api.validators import coerce_bool, coerce_string_max_len
from audit.manager import audit_manager
from core.config import load_config
from core.constants import SSE_STALL_MARGIN_S

logger = logging.getLogger(__name__)

zotlink_bp = Blueprint("zotlink", __name__)

#: One de-link operation at a time (the tagging-propose posture). Each request
#: is a short sequence of network round-trips under an op-lock; overlapping them
#: would interleave journal appends for no benefit.
_ZOTLINK_GATE = threading.Lock()

#: Request-size bound. The whole point of the verb is bulk cleanup after a
#: Zotero "Add Item by Identifier" run, but every trashed attachment adds a
#: permanently non-evictable journal op (its restore mapping lives inline), so
#: the batch is bounded for the same reason ``_HARMONIZE_MAX_KEYS`` is.
_MAX_KEYS = 200

#: Zotero item keys are base32-ish uppercase alphanumerics.
_KEY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                       "abcdefghijklmnopqrstuvwxyz0123456789")


@zotlink_bp.route('/api/zotlink/local-status')
def local_status():
    """Read capabilities without asking Zotero for permission or writing items."""
    from tagging import local_api, zotero_write
    try:
        base = load_config().get('tagging_zotero_api_base') or zotero_write.DEFAULT_BASE_URL
        return jsonify(local_api.capabilities(base, opener=zotero_write._opener))
    except Exception:
        return jsonify(error='Zotero Desktop status is unavailable.'), 503


@zotlink_bp.route('/api/zotlink/authorize-local', methods=['POST'])
def authorize_local():
    from tagging import local_api, zotero_write
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get('confirm') is not True:
        return jsonify(error='Explicit authorization confirmation is required.'), 400
    server_id = data.get('server_id')
    if not isinstance(server_id, str) or not 1 <= len(server_id) <= 128:
        return jsonify(error='Read Zotero Desktop status before authorizing.'), 400
    try:
        base = load_config().get('tagging_zotero_api_base') or zotero_write.DEFAULT_BASE_URL
        return jsonify(local_api.authorize(base, server_id, opener=zotero_write._opener))
    except Exception:
        # Never expose authorization response bodies, which can contain keys.
        return jsonify(error='Zotero authorization was denied, expired, or unavailable. Refresh its status and retry.'), 409


# NOTE: no local origin guard. `app.py::create_app` installs
# `api.security.register_origin_guard`, a before_request hook that rejects every
# `/api/*` request from a non-local origin — new blueprints are covered
# automatically. A per-blueprint copy was here until 2026-08-30; its two sibling
# sub-views (notesync, chat_attachment) never had one, and the inconsistency
# implied the app-level hook was optional, which is exactly the drift class that
# hook replaced.


def _attachment_index(inv):
    """``{attachment item_key: row}`` over the cached scan.

    Membership in this map is the authorisation check: a client may only trash
    an attachment the last scan actually saw, so a key invented by the caller
    reaches no network call at all.
    """
    return {a.item_key: a for a in getattr(inv, "zotero_attachments", ())}


@zotlink_bp.route("/api/zotlink/status")
def api_zotlink_status():
    """What Zotero holds files for, from the cached scan. Read-only.

    Mirrors ``/api/audit/reports/zotero_attachments`` but adds whether a write
    could actually run, so the UI can explain a disabled verb instead of
    failing at click time.
    """
    inv, _settings = audit_manager.get_inventory()
    if inv is None:
        return jsonify({"error": "Run a Library Audit scan first."}), 404
    from tagging import zotero_write
    cfg = load_config()
    transport = zotero_write.resolve_transport(cfg)
    rows = list(getattr(inv, "zotero_attachments", ()))
    return jsonify({
        "count": len(rows),
        "owned": sum(1 for r in rows if r.file_owned_by_zotero),
        "with_annotations": sum(1 for r in rows if r.annotation_count),
        "transport": transport.kind,
        # `can_write` is about the TRANSPORT, not about permission on any one
        # item: the local API is read-only before Zotero 10, so the honest
        # answer for it is "no" before a single request is made.
        "can_write": bool(transport.can_write),
        "note": ("The Zotero local API is read-only; set ZOTERO_API_KEY and use "
                 "the web transport to remove attachments."
                 if not transport.can_write else ""),
    })


# --- re-point a broken linked file (2026-09-07) -----------------------------
#
# A linked attachment is only a path, so anything that renames or moves the
# file orphans it silently. Zotero's own rename preference did that to five
# vault files on 2026-09-06 and the Attanger plugin to four more the next day;
# both are gated now, and both were repaired by hand with exactly this call.
# The report reads 0 of 5 242 today, which is the right moment to build it.

#: How many candidate targets the picker offers. Enough to choose from,
#: short enough to read.
_REPOINT_MAX_CANDIDATES = 20


def _biblio_pdf_rels(settings) -> list[str]:
    """Every biblio PDF as a path relative to the bibliography dir."""
    root = settings.biblio_articles_dir
    try:
        from audit.core.pdf_paths import pdf_files
        return sorted(str(p.relative_to(root)) for p in pdf_files(root))
    except OSError:
        return []


def _rank_candidates(stored_name: str, rels: list[str]) -> list[dict]:
    """Biblio PDFs ranked by similarity to the name Zotero remembers.

    `difflib` over the stem, accent- and case-folded on both sides — the
    library is bilingual FR/EN and the incidents that break these links are
    renames, so the old name is usually the best clue to the new one
    (`adès_.pdf` → `ades_2017.pdf` was one of the nine).
    """
    import difflib

    from audit.search import fold

    target = fold(Path(stored_name).stem)
    scored = []
    for rel in rels:
        stem = fold(Path(rel).stem)
        ratio = difflib.SequenceMatcher(None, target, stem).ratio()
        scored.append((ratio, rel))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [{"rel": rel, "score": round(ratio, 3)}
            for ratio, rel in scored[:_REPOINT_MAX_CANDIDATES]]


@zotlink_bp.route("/api/zotlink/repoint-candidates")
def api_zotlink_repoint_candidates():
    """Candidate files for a broken linked attachment. Read-only.

    ``?item_key=`` must name an attachment the LAST SCAN saw — the same
    authorisation rule as every other verb here, so a key invented by the
    caller reaches nothing. ``?q=`` narrows by substring; without it the
    ranking against the stored filename is the offer.
    """
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "Run a Library Audit scan first."}), 404
    item_key = (request.args.get("item_key") or "").strip()
    row = _attachment_index(inv).get(item_key)
    if row is None:
        return jsonify({"error": "That attachment is not in the last scan."}), 404
    if row.link_mode != "linked_file":
        return jsonify({"error": "Only a linked file has a path to re-point."}), 400
    rels = _biblio_pdf_rels(settings)
    q = (request.args.get("q") or "").strip()
    if q:
        from audit.search import fold
        folded = fold(q)
        rels = [r for r in rels if folded in fold(r)]
    return jsonify({
        "item_key": item_key,
        "stored_path": row.raw_path,
        "stored_exists": bool(row.raw_path and os.path.isfile(row.raw_path)),
        "total": len(rels),
        "candidates": _rank_candidates(row.filename or "", rels),
    })


@zotlink_bp.route("/api/zotlink/repoint", methods=["POST"])
def api_zotlink_repoint():
    """Point one linked attachment at ``rel`` (biblio-dir-relative).

    ``{item_key, rel, confirm: true}``. Synchronous: it is ONE GET and ONE
    PATCH, not a batch, so the SSE machinery would buy nothing. Journalled as
    ``zotero_attachment_repath`` and reversible from Tagging → Restore, the
    same op kind and revert the rename path uses — there is one definition of
    "move this pointer" in the app.

    The obsidian op-lock is taken only to append the journal record, never
    across the network call (the version guard is what prevents a lost
    update), and never for the read.
    """
    from rag.vault import obsidian_manager
    from tagging import journal as tjournal
    from tagging import writers as zw
    from tagging.writers import resolve_biblio_pdf_rel

    data = request.get_json(silent=True) or {}
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "confirm: true is required."}), 400
    item_key = coerce_string_max_len(data.get("item_key"), 64) or ""
    rel = coerce_string_max_len(data.get("rel"), 1024) or ""
    if not item_key or not rel:
        return jsonify({"error": "item_key and rel are required."}), 400
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "Run a Library Audit scan first."}), 404
    row = _attachment_index(inv).get(item_key)
    if row is None:
        return jsonify({"error": "That attachment is not in the last scan."}), 404
    if row.link_mode != "linked_file":
        return jsonify({"error": "Only a linked file has a path to re-point."}), 400
    abs_path, _biblio_rel, err = resolve_biblio_pdf_rel(rel, settings)
    if err is not None:
        body, status = err
        return jsonify(body), status

    if not _ZOTLINK_GATE.acquire(blocking=False):
        return jsonify({"error": "Another Zotero link operation is running."}), 429
    try:
        cfg = load_config()
        prepared = zw.zotero_attachment_repath_prepare(
            cfg, item_key, str(abs_path), row)
        if "early" in prepared:
            return jsonify({**prepared["early"], "item_key": item_key}), 200
        op_id = None
        epoch = obsidian_manager.try_acquire_lock(ttl=600)
        if epoch:
            try:
                manifest = tjournal.load_for_ops(settings.vault_root, cfg)
                op = zw.zotero_attachment_repath_journal(
                    prepared=prepared, manifest=manifest,
                    citation_key=row.parent_citation_key)
                op_id = op["op_id"]
                tjournal.save(settings.vault_root, cfg, manifest)
            except (tjournal.JournalUnreadableError, tjournal.ScopeError) as exc:
                logger.warning("repoint: journal append failed: %s", exc)
            finally:
                obsidian_manager.release_lock(epoch)
        result, status, version = zw.zotero_attachment_repath_patch(prepared)
        if op_id:
            epoch2 = obsidian_manager.try_acquire_lock(ttl=600)
            if epoch2:
                try:
                    manifest = tjournal.load_for_ops(settings.vault_root, cfg, [op_id])
                    op = tjournal.find_op(manifest, op_id)
                    if op is not None:
                        if status == 200:
                            op["version_after"] = version
                        else:
                            op["state"] = "failed"
                    tjournal.save(settings.vault_root, cfg, manifest)
                except (tjournal.JournalUnreadableError, tjournal.ScopeError):
                    pass
                finally:
                    obsidian_manager.release_lock(epoch2)
        return jsonify({**result, "item_key": item_key, "op_id": op_id,
                        "note": _SYNC_NOTE}), 200 if status == 200 else status
    finally:
        _ZOTLINK_GATE.release()


@zotlink_bp.route("/api/zotlink/trash", methods=["POST"])
def api_zotlink_trash():
    """Move one or more Zotero ATTACHMENTS to Zotero's trash. Confirm-gated.

    ``{item_keys: [...], confirm: true, accept_annotation_loss?: bool}``.

    Gates, in order:

    * ``confirm: true``.
    * every key must belong to the last scan's attachment set (404 otherwise) —
      the client cannot name an arbitrary Zotero item.
    * a key whose attachment carries ANNOTATIONS additionally needs
      ``accept_annotation_loss: true`` (409 otherwise): trashing takes the
      highlights with it, and "yes, remove the file" is a different decision
      from "yes, and bin my highlights too".

    Never touches a file, and never deletes: see the module docstring.
    """
    data = request.get_json(silent=True) or {}
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "confirm: true is required."}), 400
    raw = data.get("item_keys")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "item_keys must be a non-empty list"}), 400
    if len(raw) > _MAX_KEYS:
        return jsonify({"error": (
            f"At most {_MAX_KEYS} attachments per request — each one leaves a "
            "permanent restore record in the journal.")}), 400
    keys = []
    for item in raw:
        key = coerce_string_max_len(item, 64)
        # ASCII alnum, not str.isalnum(): the latter accepts any Unicode letter
        # or digit ('٣'.isalnum() is True). Membership in the scan's attachment
        # index below is the real authorisation check, so this is only shape —
        # but a shape check should mean what a Zotero item key actually is.
        if not key or not all(c in _KEY_CHARS for c in key):
            return jsonify({"error": "Invalid Zotero item key"}), 400
        keys.append(key)
    # Deduplicate before ANY count is derived from the list. `prepared` in
    # _run_trash is keyed by item key and so collapsed repeats silently, while
    # the annotation 409 below summed `annotation_count` per LISTED key — so a
    # key sent twice told the user they would lose 8 highlights when the real
    # number was 4. `_MAX_KEYS` above stays a request-SHAPE bound on the raw
    # list; this is the set that actually gets written.
    keys = list(dict.fromkeys(keys))
    accept_annotation_loss = coerce_bool(data.get("accept_annotation_loss")) is True

    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "Run a Library Audit scan first."}), 404
    index = _attachment_index(inv)
    unknown = [k for k in keys if k not in index]
    if unknown:
        return jsonify({
            "error": ("Not an attachment in the last scan: "
                      + ", ".join(unknown[:5])
                      + ("…" if len(unknown) > 5 else "")
                      + ". Re-run the scan if you added it since.")}), 404
    annotated = [k for k in keys if index[k].annotation_count]
    if annotated and not accept_annotation_loss:
        total = sum(index[k].annotation_count for k in annotated)
        return jsonify({
            # Plain language — this reaches a person, and "re-send with
            # accept_annotation_loss: true" names a field they cannot type
            # into. The UI keys on `reason` and offers the second confirm; a
            # CLI/MCP caller still has the machine-readable reason.
            "error": (f"{len(annotated)} of these attachments carry {total} Zotero "
                      "annotation(s). Trashing the attachment trashes its "
                      "highlights too. Confirm again if that is intended."),
            "reason": "would_trash_annotations",
            "annotated_keys": annotated, "annotation_count": total}), 409

    cfg = load_config()

    # Validate the restore journal's archive location BEFORE the stream opens.
    # It is a configuration error (an archive dir resolving inside the vault
    # raises ScopeError), so it can be decided while an HTTP status still means
    # something — the notesync/accession policy. Left to phase 2 it would
    # surface as an error frame on a 200 response, after the run had already
    # announced itself as started. Every other journal failure is genuinely
    # mid-run and stays an in-stream error.
    try:
        from tagging import journal as _journal
        _journal.archive_dir(Path(settings.vault_root), cfg)
    except _journal.ScopeError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    except Exception as exc:  # noqa: BLE001
        logger.exception("zotlink journal precheck failed")
        return jsonify({"error": sanitise_error_msg(exc)}), 500

    # The gate is taken LAST, after every refusal above has had its chance to
    # return. Acquiring it earlier meant each of those `return jsonify(...)`
    # paths leaked it — one malformed request and every later de-link 429'd for
    # the process's lifetime, with nothing running.
    if not _ZOTLINK_GATE.acquire(blocking=False):
        return jsonify({"error": "Another de-link is running; try again."}), 429

    consumer_timeout_s = online_aware_floor_s(cfg) + SSE_STALL_MARGIN_S

    def _worker(put, cancel):
        _run_trash(put, cancel, keys, index, settings, cfg)

    # PERF (2026-09-02): the de-link gate no longer leaks on a client disconnect.
    #   Before:    the release lived in _worker's own `finally`. run_sse_worker
    #              spawns that worker on the generator's FIRST iteration, so a
    #              client that took the 200 and went away without draining never
    #              ran it — and every later de-link answered 429 for the rest of
    #              the process's life, with nothing running. One abandoned
    #              request was enough; only a restart cleared it.
    #   Change:    release is handed to the skeleton's _SSEOpGuard, which already
    #              covers the never-iterated-generator case via call_on_close.
    #              accession.py passes `release=` for exactly this reason.
    #   Safe because:
    #              the guard fires release exactly once and only when the stream
    #              has ended AND (no worker was spawned OR the worker exited) —
    #              strictly stronger sequencing than the `finally` it replaces,
    #              which could not observe the never-spawned case at all.
    #   Preserves: "Release fires exactly once, and never while a worker that may
    #              still PATCH is alive" — the zombie-writer rule the guard's own
    #              docstring states. A release callback must stay trivial
    #              (_maybe_release_locked calls it under the guard's mutex);
    #              Lock.release is.
    #   Lock/thread contract:
    #              _ZOTLINK_GATE is acquired on the request thread as before and
    #              released from whichever of the generator-finally /
    #              call_on_close / worker-exit paths completes the pair. The
    #              obsidian op-lock choreography inside _run_trash is untouched.
    #   Pinned by: test_zotlink.py::TestTheGateIsNotLeaked
    #              ::test_a_client_that_disconnects_before_draining_leaves_the_gate_free
    return run_sse_worker(_worker, consumer_timeout_s=consumer_timeout_s,
                          release=_ZOTLINK_GATE.release)


# ── attachment RETITLE (annual cleanup T4, 2026-09-18) ─────────────────────
#
# Decision D3: a linked attachment's Zotero title equals its vault filename.
# The request carries KEYS ONLY; the new title is the basename of the path
# Zotero holds at read time (``tagging.writers.zotero_attachment_retitle_prepare``),
# so no client can write an arbitrary string into Zotero through this verb.

#: Request-size bound. Every retitle leaves a permanent restore record (the
#: before-title lives inline, so an applied op is never evicted), which is the
#: reason trash is bounded. Set higher than trash's 200 because a retitle is a
#: cheap, idempotent one-field write, and because the measured population
#: (653 mismatched titles on 2026-09-18, plus up to 56 renames) must fit ONE
#: request to be undoable as ONE batch.
_RETITLE_MAX_KEYS = 1000


@zotlink_bp.route("/api/zotlink/retitle", methods=["POST"])
def api_zotlink_retitle():
    """Set linked attachments' titles to their filenames. SSE.

    ``{item_keys: [...], confirm: true}`` or ``{item_keys: [...], dry_run: true}``.

    Gates, in order: exactly one of ``confirm``/``dry_run``; ≤
    ``_RETITLE_MAX_KEYS`` well-formed keys (deduplicated); every key a
    ``linked_file`` attachment of the LAST SCAN (404 otherwise — a key the
    caller invented reaches no network call). A dry run reads each attachment
    and reports what would change, writing nothing. Terminal frame
    ``{"zotlink_retitle": {ok, dry_run, batch_id, retitled, would_retitle,
    results, cancelled, failures, note}}``; undo with
    ``POST /api/tagging/restore {batch_id}``.
    """
    data = request.get_json(silent=True) or {}
    dry_run = coerce_bool(data.get("dry_run")) is True
    confirm = coerce_bool(data.get("confirm")) is True
    if dry_run == confirm:
        return jsonify({"error": "Send exactly one of confirm: true or dry_run: true."}), 400
    raw = data.get("item_keys")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "item_keys must be a non-empty list"}), 400
    if len(raw) > _RETITLE_MAX_KEYS:
        return jsonify({"error": (
            f"At most {_RETITLE_MAX_KEYS} attachments per request — each one "
            "leaves a permanent restore record in the journal.")}), 400
    keys = []
    for item in raw:
        key = coerce_string_max_len(item, 64)
        if not key or not all(c in _KEY_CHARS for c in key):
            return jsonify({"error": "Invalid Zotero item key"}), 400
        keys.append(key)
    keys = list(dict.fromkeys(keys))

    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "Run a Library Audit scan first."}), 404
    index = _attachment_index(inv)
    unknown = [k for k in keys if k not in index]
    if unknown:
        return jsonify({
            "error": ("Not an attachment in the last scan: "
                      + ", ".join(unknown[:5])
                      + ("…" if len(unknown) > 5 else "")
                      + ". Re-run the scan if you added it since.")}), 404
    not_linked = [k for k in keys if index[k].link_mode != "linked_file"]
    if not_linked:
        return jsonify({
            "error": ("Only a linked-file attachment is retitled to its filename: "
                      + ", ".join(not_linked[:5])
                      + ("…" if len(not_linked) > 5 else "")),
            "reason": "not_linked_file", "keys": not_linked}), 400

    cfg = load_config()
    if not dry_run:
        # Decide a misconfigured archive while a status code still means
        # something (the trash route's rule).
        try:
            from tagging import journal as _journal
            _journal.archive_dir(Path(settings.vault_root), cfg)
        except _journal.ScopeError as exc:
            return jsonify({"error": sanitise_error_msg(exc)}), 500
        except Exception as exc:  # noqa: BLE001
            logger.exception("zotlink retitle journal precheck failed")
            return jsonify({"error": sanitise_error_msg(exc)}), 500

    # Taken LAST, after every refusal above (the trash route's lesson), and
    # released by the SSE guard, never by the worker's own `finally`.
    if not _ZOTLINK_GATE.acquire(blocking=False):
        return jsonify({"error": "Another Zotero link operation is running."}), 429

    consumer_timeout_s = online_aware_floor_s(cfg) + SSE_STALL_MARGIN_S

    def _worker(put, cancel):
        _run_retitle(put, cancel, keys, index, settings, cfg, dry_run)

    return run_sse_worker(_worker, consumer_timeout_s=consumer_timeout_s,
                          release=_ZOTLINK_GATE.release)


def _run_retitle(put, cancel, keys, index, settings, cfg, dry_run):
    """Four-phase retitle — ``_run_trash``'s choreography, one field.

    1. Reads, lock-free. 2. Journal appends under ONE hold (checkpointed,
    heartbeat acted on). 3. PATCHes, lock-free. 4. Outcomes recorded on a
    RELOADED manifest; an op whose PATCH never ran or failed is flipped to
    ``failed``, so the journal never claims a retitle that did not happen.
    A dry run stops after phase 1.
    """
    from rag.vault import obsidian_manager
    from tagging import journal
    from tagging.writers import (
        zotero_attachment_retitle_journal,
        zotero_attachment_retitle_patch,
        zotero_attachment_retitle_prepare,
    )

    vault_root = Path(settings.vault_root)
    results: dict[str, dict] = {}
    prepared: dict[str, dict] = {}
    total = len(keys)

    # ---- phase 1: reads, lock-free ---------------------------------------
    for n, key in enumerate(keys, 1):
        if cancel.is_set():
            break
        put({"info": f"[{n}/{total}] reading {key}"})
        try:
            plan = zotero_attachment_retitle_prepare(cfg, key, index[key])
        except Exception as exc:  # noqa: BLE001 — one bad row must not sink the batch
            logger.exception("zotlink retitle prepare failed for %s", key)
            results[key] = {"ok": False, "status": "error",
                            "message": sanitise_error_msg(exc)}
            continue
        if "early" in plan:
            results[key] = plan["early"]
        else:
            prepared[key] = plan
            if dry_run:
                c = plan["commit"]
                results[key] = {"ok": True, "status": "would_retitle",
                                "before_title": c["before_title"],
                                "title": c["after_title"]}

    def _terminal(batch_id, failures, journal_warning=None):
        retitled = sum(1 for r in results.values() if r.get("status") == "retitled")
        put({"zotlink_retitle": {
            "ok": True,
            "dry_run": dry_run,
            "batch_id": batch_id if retitled else "",
            "retitled": retitled,
            "would_retitle": sum(1 for r in results.values()
                                 if r.get("status") == "would_retitle"),
            "results": results,
            "cancelled": cancel.is_set(),
            "failures": failures,
            **({"journal_warning": journal_warning} if journal_warning else {}),
            "note": _SYNC_NOTE,
        }})

    if dry_run or not prepared:
        _terminal("", [])
        return

    # ---- phase 2: journal appends under one hold -------------------------
    op_epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not op_epoch:
        put({"error": "Indexing is in progress; try again shortly."})
        return
    batch_id = journal.new_op_id({})
    patch_jobs: list[tuple[str, dict, str]] = []
    failures: list[str] = []
    try:
        try:
            manifest = journal.load(vault_root, cfg)
        except (journal.ScopeError, journal.JournalUnreadableError) as exc:
            put({"error": sanitise_error_msg(exc)})
            return
        keeper = _BatchJournalKeeper(
            vault_root=vault_root, cfg=cfg, manifest=manifest,
            op_epoch=op_epoch, put=put, total=len(prepared),
            manager=obsidian_manager)
        for key, plan in prepared.items():
            if cancel.is_set() or keeper.lock_lost:
                break
            keeper.tick(f"journalling {key}")
            if keeper.lock_lost:
                break
            op = zotero_attachment_retitle_journal(
                prepared=plan, manifest=manifest, batch_id=batch_id,
                citation_key=getattr(index[key], "parent_citation_key", None))
            patch_jobs.append((key, plan, op["op_id"]))
            keeper.bump_save()
        keeper.bump_save(force=True)
        if keeper.lock_lost:
            failures.append(
                "op-lock TTL lapsed mid-batch; the remaining attachments were "
                "not touched, and the records already written were merged into "
                "the on-disk manifest")
    finally:
        obsidian_manager.release_lock(op_epoch)

    # ---- phase 3: the PATCHes, lock-free ---------------------------------
    outcomes: list[tuple[str, dict]] = []
    for done, (key, plan, op_id) in enumerate(patch_jobs, 1):
        if cancel.is_set():
            outcomes.append((op_id, {"state": "failed"}))
            results[key] = {"ok": False, "status": "cancelled",
                            "message": "Cancelled before the write.",
                            "op_id": op_id}
            continue
        put({"info": f"[{done}/{len(patch_jobs)}] retitling {key}"})
        try:
            r, status, new_version = zotero_attachment_retitle_patch(plan)
        except Exception as exc:  # noqa: BLE001 — per-key truth, never a dead stream
            logger.exception("zotlink retitle patch failed for %s", key)
            r, status, new_version = (
                {"ok": False, "status": "error",
                 "message": sanitise_error_msg(exc)}, 500, None)
        results[key] = {**r, "op_id": op_id}
        if status == 200:
            outcomes.append((op_id, {"version_after": new_version}))
        else:
            outcomes.append((op_id, {"state": "failed"}))
            failures.append(f"{key}: {r.get('message')}")
    # Journalled but never reached (a lost lock stopped phase 2 mid-way leaves
    # no such op; cancellation above flips its own) — nothing else to flip.

    # ---- phase 4: record outcomes + prune under a fresh short hold -------
    journal_warning = None
    epoch2 = None
    for _ in range(20):
        epoch2 = obsidian_manager.try_acquire_lock(ttl=600)
        if epoch2:
            break
        time.sleep(1)
    if not epoch2:
        journal_warning = (
            "could not re-acquire the op-lock to record the outcomes; a failed "
            "retitle may show as applied in the journal (reverting it restores "
            "a title that was never changed, which the revert detects)")
    else:
        try:
            manifest2 = journal.load(vault_root, cfg)
            for op_id, mut in outcomes:
                op2 = journal.find_op(manifest2, op_id)
                if op2 is not None:
                    op2.update(mut)
            journal.prune(vault_root, cfg, manifest2, protect_batch_id=batch_id)
            journal.save(vault_root, cfg, manifest2)
        except Exception as exc:  # noqa: BLE001 — Zotero has been written to
            logger.exception("zotlink retitle journal finalize failed")
            journal_warning = sanitise_error_msg(exc)
        finally:
            obsidian_manager.release_lock(epoch2)

    _terminal(batch_id, failures, journal_warning)


# ── linked-file ATTACH (harmonization Phase 4, 2026-09-06) ─────────────────
#
# The forward verb the de-link has been the undo of all along. A `linked_file`
# attachment is a POST carrying a PATH and no bytes: Zotero stores nothing for
# it, so it costs no cloud storage, and it syncs down to the desktop within
# seconds (spike S1). Absolute vault paths by maintainer decision (the Linked
# Attachment Base Directory is unset, so a relative form has nothing to
# resolve against). Personal library only — linked files cannot be used in a
# group library, and a group answers 403 on its file-editing policy (S2), so
# the gate is `library_type`, never the status code.

#: Raised from 200 to 7 500 on 2026-09-06 (maintainer request — the library
#: is ~6 400 records and ~5 300 mapped PDFs, and the whole thing should fit
#: in ONE run): a linked file is a cheap, idempotent write. The
#: journal-growth argument behind the trash cap still holds — each create is
#: a permanent restore record — but the retention pass exists for exactly
#: that population. At ~1–2 s per PDF a full run is a few hours; progress
#: frames keep the SSE stall guard fed throughout.
_ATTACH_MAX_KEYS = 7500


def _attach_candidate_rows(inv):
    """Every record that could receive a linked file, with what Zotero holds.

    A record qualifies when it has a Zotero item AND ≥1 mapped PDF. Each row
    says which of its PDFs Zotero already links (by absolute path, from the
    cached attachment set) so a re-run after sync-down is idempotent, and
    whether the item sits in a group library (never writable for linked
    files). Read-only over the cached scan.
    """
    linked_by_parent: dict[str, set[str]] = {}
    for a in getattr(inv, "zotero_attachments", ()):
        if (a.link_mode or "") == "linked_file" and a.parent_key:
            linked_by_parent.setdefault(a.parent_key, set()).add(str(a.raw_path or ""))
    rows = []
    for key, rec in inv.records.items():
        zot = rec.zotero_item
        if zot is None or not rec.pdf_paths:
            continue
        have = linked_by_parent.get(zot.item_key, set())
        pdfs = [{"abs": str(p), "name": p.name, "linked": str(p) in have}
                for p in rec.pdf_paths]
        rows.append({
            "citation_key": key,
            "item_key": zot.item_key,
            "library_type": getattr(zot, "library_type", None) or "user",
            "group_id": getattr(zot, "group_id", None),
            "title": (rec.bib_entry.title if rec.bib_entry else None) or getattr(zot, "title", None),
            "year": rec.bib_entry.year if rec.bib_entry else None,
            "pdfs": pdfs,
            "to_link": sum(1 for x in pdfs if not x["linked"]),
        })
    rows.sort(key=lambda r: ((r["year"] or ""), r["citation_key"]))
    return rows


def _zotero_prefs_paths():
    """Every Zotero profile ``prefs.js`` on this Mac (read-only)."""
    import glob
    return [Path(p) for p in glob.glob(os.path.expanduser(
        "~/Library/Application Support/Zotero/Profiles/*/prefs.js"))]


def zotero_renames_linked_files() -> bool | None:
    """Whether Zotero's "Rename linked files" preference is ON.

    Read from ``prefs.js`` (``extensions.zotero.autoRenameFiles.linked``).
    ``None`` when no profile is readable. Learned the hard way on 2026-09-07:
    with it on, Zotero renames the VAULT file the moment a linked attachment
    is created, per its own template — five files became ``_.pdf`` …
    ``_ 5.pdf`` because their institutional parents had no author or year —
    which breaks the app's naming convention and every mapping that keys on
    the filename. The app cannot change the preference (Zotero rewrites
    prefs.js on exit); it can refuse to run until the user does.
    """
    found = None
    for path in _zotero_prefs_paths():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = False
        for line in text.splitlines():
            if "extensions.zotero.autoRenameFiles.linked" in line:
                found = "true" in line.split(",", 1)[-1].lower()
    return found


#: Zotero plugins known to rename or move attachment files on their own
#: rules when an attachment is added — Attanger (`zoteroattanger`) did so on
#: 2026-09-07 with Zotero's own preference already off (`ades_2017.pdf` →
#: `adès_.pdf`, `bland_1_1998.pdf` → `bland_1998.pdf`). The app cannot read
#: their settings reliably (absent prefs are plugin defaults), so an
#: installed plugin from this list is a refusal the caller must override
#: explicitly with ``acknowledge_plugins: true`` after disabling its
#: auto-rename/auto-move.
_RENAMING_PLUGINS = ("zoteroattanger", "zotfile", "zotmoov")


def zotero_renaming_plugins() -> list[str]:
    """Names from :data:`_RENAMING_PLUGINS` present in any Zotero profile's
    ``prefs.js`` (the ``extensions.webextensions.uuids`` registry lists every
    installed plugin id). Empty when none, or when no profile is readable."""
    found: set[str] = set()
    for path in _zotero_prefs_paths():
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        for name in _RENAMING_PLUGINS:
            if name in text:
                found.add(name)
    return sorted(found)


#: The vault's PDF naming convention (maintenance/naming.py::PATTERN without
#: the extension), offered in the refusal as the rule to paste into
#: Attanger. No comma inside: Attanger splits its rule list on commas.
SUGGESTED_SKIP_RULE = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*_\d{4}$"

#: Either field protects a linked file from a RENAME (read from Attanger
#: 1.5.0's own code; both are comma-separated JS regexes tested against the
#: extension-less filename). ``filenameSkipRenameRules`` is checked inside
#: ``renameFile`` only, so an imported web download that already carries a
#: conventional name is still MOVED into the vault; that is the field to
#: recommend for a connector-based import flow. ``filenameSkipAutoMoveRenameRules``
#: returns from ``processAddedAttachment`` before the move too, which would
#: strand such a download in Zotero's storage.
#: The two fields, keyed by the role they play. ``rename`` is the one to
#: recommend; ``auto_move`` protects from renaming too but returns from
#: ``processAddedAttachment`` BEFORE the move, stranding a connector download
#: in Zotero's storage — see :func:`attanger_rule_problems`.
_ATTANGER_SKIP_PREFS_BY_FIELD = {
    "rename": "extensions.zotero.zoteroattanger.filenameSkipRenameRules",
    "auto_move": "extensions.zotero.zoteroattanger.filenameSkipAutoMoveRenameRules",
}
_ATTANGER_SKIP_PREFS = tuple(_ATTANGER_SKIP_PREFS_BY_FIELD.values())


#: Mozilla's prefs.js string escapes. The file is UTF-8 and non-ASCII is
#: written RAW, so the value must never be round-tripped through a
#: byte-oriented codec: ``.encode("utf-8").decode("unicode_escape")`` is
#: latin-1-based and turns a rule containing ``pérez`` into ``pÃ©rez`` — a
#: valid regex that matches nothing, which in this gate means every PDF is
#: skipped as ``plugin_would_rename`` and the run writes nothing while
#: blaming the files. That is the same "a rule that protects nothing" class
#: :func:`attanger_rule_problems` exists to catch, reached by a route it
#: cannot see. It also emitted a DeprecationWarning for every ``\d``.
_PREF_ESCAPES = {"\\": "\\", '"': '"', "'": "'",
                 "n": "\n", "r": "\r", "t": "\t"}


def _unescape_pref_string(raw: str) -> str:
    r"""Decode one ``user_pref`` string literal the way Mozilla's parser does.

    Handles ``\\``, ``\"``, ``\'``, ``\n``, ``\r``, ``\t``, ``\uXXXX`` and
    ``\xXX``. An **unknown** escape is kept VERBATIM, backslash included —
    load-bearing here, because these prefs hold regexes: a rule written
    ``\d{4}`` (rather than Mozilla's escaped ``\\d{4}``) must still come back
    as ``\d{4}``, and both spellings therefore decode to the same rule.
    """
    if "\\" not in raw:
        return raw
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt in _PREF_ESCAPES:
            out.append(_PREF_ESCAPES[nxt])
            i += 2
            continue
        if nxt in ("u", "x"):
            width = 4 if nxt == "u" else 2
            digits = raw[i + 2:i + 2 + width]
            if len(digits) == width and all(c in "0123456789abcdefABCDEF" for c in digits):
                out.append(chr(int(digits, 16)))
                i += 2 + width
                continue
        # Unknown escape: keep it as written (a regex metacharacter).
        out.append(ch)
        out.append(nxt)
        i += 2
    return "".join(out)


def attanger_skip_rules_by_field() -> dict[str, list[str]]:
    r"""Attanger's skip rules from ``prefs.js``, split by which field holds them.

    Parsed exactly as Attanger 1.5.0 does (``getRuleList``): split the pref on
    ``,\s*`` and drop empties — and, critically, **no ``strip()``**. The plugin
    feeds each rule straight to ``new RegExp(rule)``, so a rule carrying
    whitespace is used verbatim; trimming here would make this app claim a
    protection the plugin does not actually give, which is the dangerous
    direction. :func:`attanger_rule_problems` reports such a rule instead.
    """
    found: dict[str, list[str]] = {k: [] for k in _ATTANGER_SKIP_PREFS_BY_FIELD}
    for path in _zotero_prefs_paths():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            for field, pref in _ATTANGER_SKIP_PREFS_BY_FIELD.items():
                if pref not in line:
                    continue
                m = re.search(r'user_pref\("[^"]+",\s*"((?:[^"\\]|\\.)*)"\)', line)
                if not m:
                    continue
                raw = _unescape_pref_string(m.group(1))
                bucket = found[field]
                bucket.extend(r for r in re.split(r",\s*", raw)
                              if r and r not in bucket)
    return found


def attanger_skip_rules() -> list[str]:
    """Attanger's skip-rename rules — the union of both skip fields. A file
    whose name matches any of them is never renamed by the plugin. Empty when
    both are unset. See :func:`attanger_skip_rules_by_field` for the split."""
    rules: list[str] = []
    for bucket in attanger_skip_rules_by_field().values():
        rules.extend(r for r in bucket if r not in rules)
    return rules


def attanger_rule_problems() -> list[dict]:
    """Misconfigurations of Attanger's skip fields that are invisible in its UI.

    Both were met live on 2026-09-07 and neither announces itself — the plugin
    logs nothing and the preference pane renders both identically to a working
    one:

    * ``padded`` — a rule carrying leading or trailing whitespace. Attanger
      does not trim (``getRuleList``), so the recommended anchored form becomes
      ``…$ `` — a VALID regex that can never match, since ``$`` is end-of-input
      and nothing can follow it. Protection is silently zero. This is fatal:
      the caller must refuse, because the alternative (trimming it here) would
      assert a protection the plugin does not give.
    * ``auto_move_only`` — the rule sits only in "will not be auto moved or
      renamed". That DOES protect from renaming, so it is not fatal, but
      ``processAddedAttachment`` returns before the move, so a Zotero-connector
      download whose name already matches (which the user's own rename
      template produces) is never moved into the vault. Advisory.
    """
    by_field = attanger_skip_rules_by_field()
    problems: list[dict] = []
    for field, rules in by_field.items():
        for rule in rules:
            if rule != rule.strip():
                problems.append({
                    "kind": "padded", "fatal": True,
                    "field": field, "rule": rule,
                    "detail": (
                        "The Attanger rule %r has leading or trailing "
                        "whitespace. Attanger does not trim its rules, so it "
                        "is used verbatim and — with the anchored form this "
                        "app recommends — can never match any filename, "
                        "leaving every PDF unprotected. Remove the whitespace "
                        "in Zotero (Settings → Attanger), quit and relaunch "
                        "Zotero so prefs.js is rewritten, then retry."
                        % rule),
                })
    if by_field["auto_move"] and not by_field["rename"]:
        problems.append({
            "kind": "auto_move_only", "fatal": False,
            "field": "auto_move", "rule": by_field["auto_move"][0],
            "detail": (
                "Attanger's skip rule is set only in \u201cwill not be auto "
                "moved or renamed\u201d. That protects these files from being "
                "renamed, so this run is safe, but it also stops Attanger "
                "moving newly downloaded attachments into the vault. Prefer "
                "\u201cwill not be renamed\u201d."),
        })
    return problems


def _rule_matches(rules: list[str], stem: str) -> bool:
    """Whether any Attanger rule matches *stem*, using Python's ``re`` as a
    stand-in for JavaScript's — identical for the anchored character-class
    patterns this is about; a rule Python cannot compile counts as no match,
    which is the conservative direction (the plugin would still rename)."""
    for rule in rules:
        try:
            if re.search(rule, stem):
                return True
        except re.error:
            continue
    return False


@zotlink_bp.route("/api/zotlink/attach-candidates")
def api_zotlink_attach_candidates():
    """Records with a Zotero item and ≥1 mapped PDF, and how many of those PDFs
    Zotero does not yet hold as a linked file. Read-only; 404 without a scan."""
    inv, _settings = audit_manager.get_inventory()
    if inv is None:
        return jsonify({"error": "Run a Library Audit scan first."}), 404
    from tagging import zotero_write
    transport = zotero_write.resolve_transport(load_config())
    rows = _attach_candidate_rows(inv)
    # The plugin state is read HERE too, not only at write time: a broken skip
    # rule is invisible in Attanger's own UI, so the tab must be able to say so
    # before the user spends a confirm on a run that would refuse.
    plugins = zotero_renaming_plugins()
    return jsonify({
        "rows": rows,
        "count": len(rows),
        "to_link": sum(r["to_link"] for r in rows),
        "group_count": sum(1 for r in rows if r["library_type"] == "group"),
        "batch_max": _ATTACH_MAX_KEYS,
        "path_form": "absolute",
        "can_write": bool(transport.can_write),
        "transport": transport.kind,
        "renaming_plugins": plugins,
        "rule_problems": attanger_rule_problems() if plugins else [],
    })


def rename_gate(acknowledged: bool):
    """The Zotero/plugin file-RENAMING gate, shared by every verb that makes
    a linked file exist.

    ``(error_response | None, guard_rules, preflight_msgs)``. Checked BEFORE
    the transport, because it applies to a dry run's advice too; and lifted
    out of the attach route in 2026-09-11 so the item-create verb runs the
    SAME gate rather than a second copy of it — an out-of-date copy would
    silently let Zotero rename the vault PDF the moment a link is made, which
    is the incident this whole gate exists because of.

    ``guard_rules`` is ``None`` when no per-file check is needed (nothing
    installed, or the user acknowledged) and otherwise the skip rules a
    filename must match before it may be linked.
    """
    plugins = zotero_renaming_plugins()
    skip_rules = attanger_skip_rules() if "zoteroattanger" in plugins else []
    if plugins and not acknowledged and not skip_rules:
        return (jsonify({"error": (
            "A Zotero plugin that renames attachment files is installed "
            f"({', '.join(plugins)}). On 2026-09-07 it renamed vault PDFs the "
            "moment their links were created, with Zotero's own rename "
            "preference already off. Either add this rule to Attanger's "
            "'skip rename' field (NOT 'skip auto move and rename', which "
            "would also stop web downloads from being moved into the vault) — "
            f"it matches the vault's author_year convention: {SUGGESTED_SKIP_RULE} "
            "— then quit and "
            "relaunch Zotero so the rule is written to prefs.js — or disable "
            "the plugin's automatic rename and re-send with "
            "acknowledge_plugins: true."),
            "reason": "zotero_renaming_plugin", "plugins": plugins,
            "suggested_rule": SUGGESTED_SKIP_RULE}), 409), None, []
    # A rule can be present and still protect NOTHING. Attanger feeds each rule
    # verbatim to `new RegExp(...)` with no trim, so a pasted trailing space
    # turns the recommended anchored form into `…$ ` — valid, and unmatchable.
    # Refusing is the only honest move: trimming it here would assert a
    # protection the plugin does not give, and proceeding would silently skip
    # every file as `plugin_would_rename`.
    rule_problems = attanger_rule_problems() if plugins else []
    fatal = [x for x in rule_problems if x["fatal"]]
    if fatal and not acknowledged:
        return (jsonify({
            "error": fatal[0]["detail"],
            "reason": "zotero_renaming_plugin_rule_broken",
            "plugins": plugins, "problems": rule_problems,
            "suggested_rule": SUGGESTED_SKIP_RULE}), 409), None, []
    if zotero_renames_linked_files() is True:
        return (jsonify({"error": (
            "Zotero is set to rename linked files (Settings → General → File "
            "Renaming → “Rename linked files”). With that on, Zotero renames "
            "the vault PDF the moment the link is created, per its own "
            "template — which breaks the vault's naming convention and the "
            "mapping. Turn it off in Zotero, then retry."),
            "reason": "zotero_renames_linked_files"}), 409), None, []
    # With a rule present, protection is decided PER FILE by the caller (a
    # name the rule does not cover is skipped, never renamed by the plugin).
    guard_rules = None if (acknowledged or not plugins) else skip_rules
    return None, guard_rules, [x["detail"] for x in rule_problems if not x["fatal"]]


@zotlink_bp.route("/api/zotlink/attach", methods=["POST"])
def api_zotlink_attach():
    """Create ``linked_file`` attachments in Zotero for records' mapped PDFs.

    ``{citation_keys: [...], confirm: true, dry_run?: bool}`` → SSE.

    Gates, in order: ``confirm``; ≤ ``_ATTACH_MAX_KEYS`` keys, deduplicated;
    a completed scan; every key must be a record with a Zotero item and a
    mapped PDF (404 otherwise — the client cannot name arbitrary items); the
    transport must be able to write (503, the status route's answer). A
    group-library record is SKIPPED with a reason, never refused as a whole
    — it is the common case in a mixed library. A PDF Zotero already links
    (same absolute path) is skipped, so a re-run after sync-down is
    idempotent. ``dry_run`` reports the plan and touches nothing.

    Never uploads a byte and never deletes: the undo is the trash verb.
    """
    data = request.get_json(silent=True) or {}
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "confirm: true is required."}), 400
    raw = data.get("citation_keys")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "citation_keys must be a non-empty list"}), 400
    if len(raw) > _ATTACH_MAX_KEYS:
        return jsonify({"error": (
            f"At most {_ATTACH_MAX_KEYS} records per request — each linked "
            "file leaves a permanent restore record in the journal.")}), 400
    keys = []
    for item in raw:
        key = coerce_string_max_len(item, 256)
        # NUL / newlines only. A slash is a legitimate BBT key character
        # (the live bib carries `institutions/universities/…2021`), and the key is
        # never used as a path here — membership in the cached record index
        # below is the real authorisation check.
        if not key or any(c in key for c in ("\x00", "\n", "\r")):
            return jsonify({"error": f"Invalid citation key: {item!r}"[:200]}), 400
        keys.append(key)
    keys = list(dict.fromkeys(keys))
    dry_run = coerce_bool(data.get("dry_run")) is True

    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "Run a Library Audit scan first."}), 404
    rows = {r["citation_key"]: r for r in _attach_candidate_rows(inv)}
    unknown = [k for k in keys if k not in rows]
    if unknown:
        return jsonify({
            "error": ("Not a record with a Zotero item and a mapped PDF: "
                      + ", ".join(unknown[:5])
                      + ("…" if len(unknown) > 5 else "")
                      + ". Re-run the scan if you changed the mapping since.")}), 404

    # Optional exact-file selector for pending review. Validate the whole list
    # before narrowing records: selecting a key must not attach its other PDFs.
    if "pdfs" in data:
        raw_pdfs = data["pdfs"]
        if not isinstance(raw_pdfs, list) or not 1 <= len(raw_pdfs) <= 500:
            return jsonify(error="pdfs must select 1–500 vault PDFs."), 400
        from api.routes.pending import target, mapping_state
        from accession import review
        from audit.engine import bridge
        selected_paths = set()
        selected_identity = {}
        review_items = data.get("review_items", {})
        if not isinstance(review_items, dict):
            return jsonify(error="review_items must be an object."), 400
        try:
            for rel in raw_pdfs:
                path, canonical = target(settings, rel)
                owning = [key for key in keys if any(x["abs"] == str(path) for x in rows[key]["pdfs"])]
                if len(owning) != 1 or mapping_state(settings, canonical)["keys"] != owning:
                    return jsonify(error="PDF mapping changed or is absent from the scan. Reconcile and scan again."), 409
                current = review.identity(path)
                mapped_hash = bridge._load_mapping_all(settings.mapping_file).hashes.get(canonical, {}).get("sha256")
                if mapped_hash != current["sha256"]:
                    return jsonify(error="PDF changed since mapping. Review and reconcile it again."), 409
                if canonical in review_items:
                    review.validate(settings.vault_root, canonical, review_items[canonical])
                selected_identity[str(path)] = {"expected_identity": current, "review_token": review_items.get(canonical), "review_rel": canonical}
                selected_paths.add(str(path))
        except review.ReviewConflict as exc:
            return jsonify(error=str(exc)), 409
        except (ValueError, OSError, TypeError):
            return jsonify(error="Invalid or unavailable selected PDF."), 400
        rows = {key: {**rows[key], "review_exact_files": True, "pdfs": [{**x, **selected_identity[x["abs"]]} for x in rows[key]["pdfs"] if x["abs"] in selected_paths]} for key in keys}
        if any(not row["pdfs"] for row in rows.values()):
            return jsonify(error="Each selected entry must own a selected PDF."), 400

    # Refuse while Zotero would rename the vault files (see `rename_gate`).
    err, guard_rules, soft_problems = rename_gate(
        coerce_bool(data.get("acknowledge_plugins")) is True)
    if err is not None:
        return err

    cfg = load_config()
    from tagging import zotero_write
    transport = zotero_write.resolve_transport(cfg)
    if not dry_run and not transport.can_write:
        return jsonify({"error": (
            "Authorize Zotero 10 Desktop, or set ZOTERO_API_KEY for the web "
            "transport, before creating linked files.")}), 503

    try:
        from tagging import journal as _journal
        _journal.archive_dir(Path(settings.vault_root), cfg)
    except _journal.ScopeError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    except Exception as exc:  # noqa: BLE001
        logger.exception("zotlink attach journal precheck failed")
        return jsonify({"error": sanitise_error_msg(exc)}), 500

    if not _ZOTLINK_GATE.acquire(blocking=False):
        return jsonify({"error": "Another Zotero attachment operation is running; try again."}), 429

    consumer_timeout_s = online_aware_floor_s(cfg) + SSE_STALL_MARGIN_S

    def _worker(put, cancel):
        _run_attach(put, cancel, keys, rows, inv, settings, cfg, dry_run,
                    guard_rules=guard_rules)

    return run_sse_worker(_worker, consumer_timeout_s=consumer_timeout_s,
                          preflight_msgs=soft_problems or None,
                          release=_ZOTLINK_GATE.release)


def _run_attach(put, cancel, keys, rows, inv, settings, cfg, dry_run, *,
                guard_rules=None):
    """Read/plan, create or recover, then journal under a short app lock.

    Exact-file reviews reserve a durable client key before dispatch and read
    it before retrying. Legacy citation-key-only requests retain their older
    create behavior. No network request holds the app's operation lock.
    """
    from rag.vault import obsidian_manager
    from tagging import journal
    from tagging.writers import (
        zotero_attachment_create_journal,
        zotero_attachment_create_post,
        zotero_attachment_create_prepare,
    )

    vault_root = Path(settings.vault_root)
    results: dict[str, dict] = {}
    plans: list[tuple[str, str, dict]] = []   # (citation_key, pdf_abs, prepared)
    total = len(keys)

    # ---- phase 1: plan + reads -------------------------------------------
    for n, key in enumerate(keys, 1):
        if cancel.is_set():
            break
        row = rows[key]
        rec = inv.records.get(key)
        zot = rec.zotero_item if rec is not None else None
        per_pdf: dict[str, dict] = {}
        if row["library_type"] == "group":
            results[key] = {"ok": False, "status": "skipped", "reason": "group_library",
                            "message": "Linked files cannot be used in a group library.",
                            "pdfs": {}}
            continue
        todo = [x for x in row["pdfs"] if row.get("review_exact_files") or not x["linked"]]
        for x in row["pdfs"]:
            if x["linked"] and not row.get("review_exact_files"):
                per_pdf[x["abs"]] = {"ok": True, "status": "skipped",
                                     "message": "Already linked in Zotero."}
        if guard_rules is not None:
            # Attanger is installed: only a filename its skip rule covers may
            # be linked, or the plugin renames it on arrival (2026-09-07).
            unprotected = [x for x in todo
                           if not _rule_matches(guard_rules, Path(x["name"]).stem)]
            for x in unprotected:
                per_pdf[x["abs"]] = {"ok": False, "status": "skipped",
                                     "reason": "plugin_would_rename",
                                     "message": ("Attanger's skip rule does not "
                                                 "cover this filename; it would "
                                                 "be renamed on arrival.")}
            todo = [x for x in todo if x not in unprotected]
        if not todo:
            unprot = any(p.get("reason") == "plugin_would_rename" for p in per_pdf.values())
            results[key] = {"ok": not unprot, "status": "skipped",
                            "reason": "plugin_would_rename" if unprot else "already_linked",
                            "message": ("A PDF's name is not covered by Attanger's skip rule."
                                        if unprot else "Every PDF is already linked."),
                            "pdfs": per_pdf}
            continue
        if dry_run:
            for x in todo:
                per_pdf[x["abs"]] = {"ok": True, "status": "planned",
                                     "message": "Would create a linked file."}
            results[key] = {"ok": True, "status": "planned", "pdfs": per_pdf}
            continue
        put({"info": f"[{n}/{total}] reading {key}"})
        prepared_parent = None
        live_paths = set()
        for x in todo:
            try:
                if prepared_parent is None:
                    prepared_parent = zotero_attachment_create_prepare(cfg, zot, x["abs"])
                    if "early" in prepared_parent:
                        break
                    if row.get("review_exact_files"):
                        from tagging import zotero_write
                        c = prepared_parent["commit"]
                        live_paths = zotero_write.linked_file_paths(
                            c["transport"].base_url, c["lib_path"], c["parent_key"], api_key=c["api_key"],
                            **({"server_id": c["server_id"]} if c.get("server_id") else {}))
                # Each PDF gets its own commit dict (its own path/title) over
                # the ONE parent read.
                c = dict(prepared_parent["commit"])
                c["path"] = x["abs"]
                c["title"] = Path(x["abs"]).name
                if row.get("review_exact_files"):
                    c.update({k: x[k] for k in ("expected_identity", "review_token", "review_rel")})
                    from tagging.attachment_intents import reserve
                    c.update(vault_root=str(vault_root), mapping_file=str(settings.mapping_file), citation_key=key)
                    existing = reserve(vault_root, c, create=False)
                    if not existing and x["abs"] in live_paths:
                        per_pdf[x["abs"]] = {"ok": True, "status": "skipped", "message": "Already attached in Zotero (verified live)."}
                        continue
                    c["item_key"] = existing or reserve(vault_root, c)
                plans.append((key, x["abs"], {"commit": c}))
            except Exception as exc:  # noqa: BLE001 — one bad row must not sink the batch
                logger.exception("zotlink attach prepare failed for %s", key)
                per_pdf[x["abs"]] = {"ok": False, "status": "error",
                                     "message": sanitise_error_msg(exc)}
                if row.get("review_exact_files"):
                    # A failed authoritative lookup cannot become an empty
                    # attachment set for the remaining PDFs of this parent.
                    for remaining in todo:
                        per_pdf.setdefault(remaining["abs"], {
                            "ok": False, "status": "error",
                            "message": "Attachment verification failed; retry after refreshing Zotero."})
                    break
        if prepared_parent is not None and "early" in prepared_parent:
            early = prepared_parent["early"]
            results[key] = {**early, "pdfs": per_pdf}
        else:
            results[key] = {"ok": True, "status": "pending", "pdfs": per_pdf}
        if row.get("review_exact_files") and not any(k == key for k, _, _ in plans):
            failed = any(not p.get("ok") for p in per_pdf.values())
            results[key].update(ok=not failed, status="failed" if failed else "skipped")

    if dry_run or not plans:
        put({"zotlink_attach": {
            "ok": True, "dry_run": dry_run, "attached": 0, "batch_id": "",
            "results": results, "cancelled": cancel.is_set(), "failures": [],
            "path_form": "absolute", "note": _SYNC_NOTE}})
        return

    # ---- phase 2: POSTs, lock-free ---------------------------------------
    created: list[tuple[str, dict, str, int]] = []   # (key, prepared, att_key, version)
    failures: list[str] = []
    for i, (key, pdf_abs, prepared) in enumerate(plans, 1):
        if cancel.is_set():
            results[key]["pdfs"][pdf_abs] = {"ok": False, "status": "cancelled",
                                             "message": "Cancelled before the write."}
            continue
        put({"info": f"[{i}/{len(plans)}] linking {Path(pdf_abs).name} → {key}"})
        try:
            r, att_key, version = zotero_attachment_create_post(prepared)
        except Exception as exc:  # noqa: BLE001
            logger.exception("zotlink attach POST failed for %s", key)
            r, att_key, version = ({"ok": False, "status": "error",
                                    "message": sanitise_error_msg(exc)}, None, None)
        results[key]["pdfs"][pdf_abs] = r
        if r.get("ok") and att_key:
            created.append((key, prepared, att_key, int(version or 0)))
        else:
            failures.append(f"{key}/{Path(pdf_abs).name}: {r.get('message')}")

    for key, res in results.items():
        if res.get("status") == "pending":
            oks = [p for p in res["pdfs"].values() if p.get("ok") and p.get("status") == "attached"]
            res["ok"] = bool(oks)
            res["status"] = "attached" if oks else "failed"

    # ---- phase 3: ONE hold to journal the creates ------------------------
    batch_id = journal.new_op_id({})
    journal_warning = None
    restore_batches: set[str] = set()
    if created:
        epoch = None
        for _ in range(20):
            epoch = obsidian_manager.try_acquire_lock(ttl=600)
            if epoch:
                break
            time.sleep(1)
        if not epoch:
            journal_warning = (
                "could not acquire the op-lock to journal the creates; the "
                "linked files exist in Zotero but have no restore record — "
                "remove them from the Zotero Attachments tab if needed")
        else:
            try:
                manifest = journal.load(vault_root, cfg)
                for key, prepared, att_key, version in created:
                    op = zotero_attachment_create_journal(
                        prepared=prepared, citation_key=key, att_key=att_key,
                        version=version, manifest=manifest, batch_id=batch_id)
                    results[key]["pdfs"][prepared["commit"]["path"]]["op_id"] = op["op_id"]
                    if op.get("batch_id"):
                        restore_batches.add(op["batch_id"])
                journal.prune(vault_root, cfg, manifest, protect_batch_id=batch_id)
                journal.save(vault_root, cfg, manifest)
            except Exception as exc:  # noqa: BLE001
                logger.exception("zotlink attach journal failed")
                journal_warning = sanitise_error_msg(exc)
                restore_batches.clear()
            finally:
                obsidian_manager.release_lock(epoch)

    put({"zotlink_attach": {
        "ok": True,
        "dry_run": False,
        "batch_id": batch_id if restore_batches == {batch_id} else "",
        "restore_batch_ids": sorted(restore_batches),
        "attached": sum(not results[key]["pdfs"][prepared["commit"]["path"]].get("recovered") for key, prepared, _, _ in created),
        "recovered": sum(bool(results[key]["pdfs"][prepared["commit"]["path"]].get("recovered")) for key, prepared, _, _ in created),
        "results": results,
        "cancelled": cancel.is_set(),
        "failures": failures,
        **({"journal_warning": journal_warning} if journal_warning else {}),
        "path_form": "absolute",
        "note": _SYNC_NOTE,
    }})


def _run_trash(put, cancel, keys, index, settings, cfg):
    """Four-phase de-link, mirroring ``retire-tag``'s choreography exactly.

    1. **Reads, BEFORE the op-lock.** A Zotero GET can sleep on a ``Backoff``
       header, and sleeping under that lock stalls indexing and every other
       vault write.
    2. **Journal appends under ONE hold**, checkpointed every 25 ops by the
       shared :class:`_BatchJournalKeeper`, whose heartbeat is ACTED ON. The
       previous implementation held the lock across up to 200 sequential
       PATCHes with no heartbeat at all, so a long batch froze every vault
       write and could outrun the 600 s TTL while still appending to the
       manifest — two writers on one journal. That is the pattern the
       2026-07-23 audit removed from retire-tag; it came back here.
    3. **PATCHes, lock-free.** A PATCH touches neither the vault nor the
       manifest, and the ``If-Unmodified-Since-Version`` guard — not the lock
       — is what prevents a lost update.
    4. **A second short hold** records each PATCH outcome and prunes with the
       batch protected from cap-eviction.

    A cancel in phase 3 does NOT skip phase 4: the ops are already durable as
    "applied", so an un-run PATCH must be flipped to ``failed`` or the journal
    would claim a trash that never happened. (Reverting such an op restores an
    item that was never trashed, which is idempotent — but the journal must
    still not assert something untrue.)
    """
    from rag.vault import obsidian_manager
    from tagging import journal
    from tagging.writers import (
        zotero_attachment_journal,
        zotero_attachment_patch,
        zotero_attachment_prepare,
    )

    vault_root = Path(settings.vault_root)
    results: dict[str, dict] = {}
    prepared: dict[str, dict] = {}
    total = len(keys)

    # ---- phase 1: reads, lock-free ---------------------------------------
    for n, key in enumerate(keys, 1):
        if cancel.is_set():
            break
        put({"info": f"[{n}/{total}] reading {key}"})
        try:
            plan = zotero_attachment_prepare(cfg, key, index[key])
        except Exception as exc:  # noqa: BLE001 — one bad row must not sink the batch
            logger.exception("zotlink prepare failed for %s", key)
            results[key] = {"ok": False, "status": "error",
                            "message": sanitise_error_msg(exc)}
            continue
        if "early" in plan:
            results[key] = plan["early"]
        else:
            prepared[key] = plan

    if not prepared:
        # Same key set as the full terminal frame: a client that reads
        # `failures` must not have to special-case the nothing-to-do path.
        put({"zotlink": {"ok": True, "trashed": 0, "results": results,
                         "batch_id": "", "cancelled": cancel.is_set(),
                         "failures": [], "note": _SYNC_NOTE}})
        return

    # ---- phase 2: journal appends under one hold -------------------------
    op_epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not op_epoch:
        put({"error": "Indexing is in progress; try again shortly."})
        return
    batch_id = journal.new_op_id({})
    patch_jobs: list[tuple[str, dict, str]] = []
    failures: list[str] = []
    keeper = None
    try:
        try:
            manifest = journal.load(vault_root, cfg)
        except (journal.ScopeError, journal.JournalUnreadableError) as exc:
            put({"error": sanitise_error_msg(exc)})
            return
        keeper = _BatchJournalKeeper(
            vault_root=vault_root, cfg=cfg, manifest=manifest,
            op_epoch=op_epoch, put=put, total=total,
            manager=obsidian_manager)
        for key, plan in prepared.items():
            if cancel.is_set() or keeper.lock_lost:
                break
            keeper.tick(f"journalling {key}")
            if keeper.lock_lost:
                break
            op = zotero_attachment_journal(prepared=plan, manifest=manifest,
                                           batch_id=batch_id)
            patch_jobs.append((key, plan, op["op_id"]))
            keeper.bump_save()
        keeper.bump_save(force=True)
        if keeper.lock_lost:
            failures.append(
                "op-lock TTL lapsed mid-batch; the remaining attachments were "
                "not touched, and the records already written were merged into "
                "the on-disk manifest")
    finally:
        obsidian_manager.release_lock(op_epoch)

    # ---- phase 3: the PATCHes, lock-free ---------------------------------
    outcomes: list[tuple[str, dict]] = []
    done = 0
    for key, plan, op_id in patch_jobs:
        done += 1
        if cancel.is_set():
            outcomes.append((op_id, {"state": "failed"}))
            results[key] = {"ok": False, "status": "cancelled",
                            "message": "Cancelled before the write.",
                            "op_id": op_id}
            continue
        put({"info": f"[{done}/{len(patch_jobs)}] trashing {key}"})
        try:
            r, status, new_version = zotero_attachment_patch(plan)
        except Exception as exc:  # noqa: BLE001 — per-key truth, never a dead stream
            logger.exception("zotlink patch failed for %s", key)
            r, status, new_version = (
                {"ok": False, "status": "error",
                 "message": sanitise_error_msg(exc)}, 500, None)
        results[key] = {**r, "op_id": op_id}
        if status == 200:
            outcomes.append((op_id, {"version_after": new_version}))
        else:
            outcomes.append((op_id, {"state": "failed"}))
            failures.append(f"{key}: {r.get('message')}")

    # ---- phase 4: record outcomes + prune under a fresh short hold -------
    journal_warning = None
    epoch2 = None
    for _ in range(20):
        epoch2 = obsidian_manager.try_acquire_lock(ttl=600)
        if epoch2:
            break
        time.sleep(1)
    if not epoch2:
        journal_warning = (
            "could not re-acquire the op-lock to record the outcomes; a failed "
            "de-link may show as applied in the journal (reverting it restores "
            "an item that was never trashed, which is harmless)")
    else:
        try:
            # RELOADED, never the phase-2 object: another writer may have taken
            # the lock between our two holds, and saving the stale object would
            # clobber their ops (single-writer discipline).
            manifest2 = journal.load(vault_root, cfg)
            for op_id, mut in outcomes:
                op2 = journal.find_op(manifest2, op_id)
                if op2 is not None:
                    op2.update(mut)
            # protect_batch_id: an applied attachment op is never cap-evicted
            # (its restore mapping lives INLINE, with no snapshot behind it),
            # but the batch is exempted from THIS prune explicitly so the
            # promise of a one-click undo cannot be broken by the very request
            # that made it.
            journal.prune(vault_root, cfg, manifest2, protect_batch_id=batch_id)
            journal.save(vault_root, cfg, manifest2)
        except Exception as exc:  # noqa: BLE001
            # Never fatal: by this point Zotero has been written to, and every
            # op was appended and checkpointed before its own PATCH. Report it
            # beside the per-key truth rather than as a failed request.
            logger.exception("zotlink journal finalize failed")
            journal_warning = sanitise_error_msg(exc)
        finally:
            obsidian_manager.release_lock(epoch2)

    trashed = sum(1 for r in results.values()
                  if r.get("ok") and r.get("status") == "trashed")
    put({"zotlink": {
        "ok": True,
        "batch_id": batch_id if trashed else "",
        "trashed": trashed,
        "results": results,
        "cancelled": cancel.is_set(),
        "failures": failures,
        **({"journal_warning": journal_warning} if journal_warning else {}),
        "note": _SYNC_NOTE,
    }})


#: The app reads Zotero from the local SQLite snapshot, so a web-API write is
#: invisible here until Zotero syncs down AND a new scan runs. Said in the
#: payload rather than only in UI copy, because a CLI or MCP caller needs it
#: just as much.
_SYNC_NOTE = ("Desktop writes take effect locally; Web API writes require Zotero sync. "
              "Re-scan Library Audit to refresh its cached reports.")
