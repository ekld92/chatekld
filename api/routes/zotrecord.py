"""``/api/zotrecord/*`` — record-level Zotero corrections (annual cleanup, 2026-09-17).

Two verbs the Library Audit's write surfaces did not have, both built for the
annual cleanup plan (T8 record merge, T9 chapter-title correction) and both
following the writers' standing rules: reads lock-free, every write journalled
BEFORE its PATCH under a short obsidian op-lock hold, PATCHes version-guarded
and lock-free, outcomes recorded on a RELOADED manifest, and recovery offered through Tagging → Restore (single op or the merge's one
``batch_id``), subject to verified current state and retained evidence.

**retitle** (T9). Writes ONE field — ``title`` — on a record's Zotero item
through the existing ``zotero_metadata_write`` writer, so its stale-review
guard, its readback and its revert are the campaign executor's. What is added
is the evidence gate the plan asks for: the new title is looked for on the
record's PDF (front matter, case- and accent-folded); a title the document
does not print is refused unless the caller says ``accept_unauthenticated``.

**merge** (T8). Keeps one Zotero record and retires its duplicate: the
retired item's children (attachments, reading notes) are re-parented onto the
kept item — except an attachment whose linked path the kept item already
links, which rides the trash with its parent — the tags are unioned onto the
kept item, the retired citation key is written into the kept item's Extra as
a Better BibTeX alias (``tex.ids``) so existing ``\\cite{}`` still compile,
the same key is added to the kept Obsidian note's ``aliases`` so ``[[links]]``
still resolve, and the retired item is TRASHED (never deleted — the key
survives, which is what makes the undo exact). The merge REFUSES rather than
guess when the retired record owns the Obsidian note (swap the sides: the
note-bearing record is the one to keep) or when both items carry a reading
note (two notes under one item is exactly the ``multiple_notes`` state the
note sync cannot serve). ``mapping.json`` is NOT touched here: re-keying the
retired record's PDFs goes through the existing ``POST /api/audit/mapping``
verb, which the cleanup harness sequences before the merge.

Synchronous, not SSE: one merge is a short bounded sequence of round-trips
(the zotcreate posture). One record op at a time (``_ZOTRECORD_GATE``, 429).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from api.security import sanitise_error_msg
from api.validators import coerce_bool, coerce_non_empty_string, coerce_string_max_len
from audit.manager import audit_manager
from core.config import load_config
from tagging import journal, zotero_write
from tagging.writers import (
    _note_aliases_locked,
    _zotero_lib_path,
    _zotero_read_identity,
    _zotero_transport,
    zotero_child_reparent_journal,
    zotero_child_reparent_patch,
    zotero_item_trash_journal,
    zotero_item_trash_patch,
    zotero_metadata_journal,
    zotero_metadata_patch,
    zotero_metadata_prepare,
)

logger = logging.getLogger(__name__)

zotrecord_bp = Blueprint("zotrecord", __name__)

_ZOTRECORD_GATE = threading.Lock()
_MAX_TITLE_CHARS = 1000
#: Better BibTeX reads citation-key aliases from this Extra line; a merged-away
#: key listed here keeps every existing ``\cite{oldKey}`` compiling.
_TEX_IDS_PREFIX = "tex.ids:"


def _gates():
    """``((inv, settings, cfg), None)`` or ``(None, flask_error)``."""
    if audit_manager.is_scanning():
        return None, (jsonify({"error": "A scan is running. Wait for it to finish."}), 409)
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return None, (jsonify({"error": "No scan results. Run an audit scan first."}), 404)
    return (inv, settings, load_config()), None


def _record(inv, raw):
    key = coerce_non_empty_string(raw, max_len=128)
    if not key:
        return None, None
    return key, inv.records.get(key)


def _hold(fn, *, ttl=600, retries=20):
    """Run *fn(manifest)* under a short op-lock hold with a loaded manifest;
    returns ``(result, error_message | None)``."""
    from rag.vault import obsidian_manager
    epoch = None
    for _ in range(retries):
        epoch = obsidian_manager.try_acquire_lock(ttl=ttl)
        if epoch:
            break
        time.sleep(1)
    if not epoch:
        return None, "could not acquire the op-lock (indexing in progress?)"
    try:
        return fn(epoch), None
    finally:
        obsidian_manager.release_lock(epoch)


#: A landed Extra/metadata PATCH whose readback differs or moved an unselected
#: field is still a write that happened. Treating those as failed flipped a
#: landed Extra and skipped the rest of the merge (2026-09-19 review A3).
_LANDED_METADATA = frozenset({"readback_differs", "unselected_field_changed"})


def _record_outcome(vault_root, cfg, batch_id, *, applied: dict, failed: set,
                    extras: dict | None = None):
    """Second short hold: RELOAD the manifest and record what the lock-free
    phase did — ``version_after`` on the ops that ran, ``failed`` on those
    that never ran (an un-run add would otherwise revert by removing
    something nobody added)."""
    extras = extras or {}
    def _do(_epoch):
        manifest = journal.load(vault_root, cfg)
        for op in manifest["ops"]:
            if op.get("batch_id") != batch_id:
                continue
            if op["op_id"] in failed:
                op["state"] = "failed"
            elif op["op_id"] in applied:
                op["state"] = "applied"
                if applied[op["op_id"]] is not None:
                    op["version_after"] = int(applied[op["op_id"]])
                extra = extras.get(op["op_id"])
                if extra:
                    op.update(extra)
        journal.save(vault_root, cfg, manifest)
        return True
    _, err = _hold(_do, ttl=300)
    return err


# ---------------------------------------------------------------------------
# T9 — retitle
# ---------------------------------------------------------------------------

def _authenticate_title(pdf: Path, title: str) -> dict:
    """Where the proposed *title* is printed in the PDF's front matter, if anywhere."""
    from accession import pdf_authentication as pa
    scan = pa.scan_pdf(pdf, max_pages=pa.FRONT_MATTER_PAGES)
    if scan.error or not scan.pages:
        return {"found": False, "reason": scan.error or "no extractable page text", "pdf": pdf.name}
    hit = pa.locate_text(scan, "chapter_title", title)
    if hit is None:
        return {"found": False, "reason": "not printed in the front matter", "pdf": pdf.name}
    return {"found": True, "page": hit.page, "offset": hit.offset, "excerpt": hit.excerpt,
            "passage_fingerprint": hit.fingerprint, "pdf": pdf.name}


@zotrecord_bp.route("/api/zotrecord/retitle", methods=["POST"])
def api_zotrecord_retitle():
    """Body ``{citation_key, title, expected_title?, pdf?, accept_unauthenticated?,
    confirm | dry_run}``. ``expected_title`` defaults to the title the cached scan
    read from Zotero; the write refuses if Zotero's current title differs from
    it (the review is stale)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    dry_run = coerce_bool(data.get("dry_run")) is True
    if not dry_run and coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Write must be explicitly confirmed (confirm: true)."}), 400
    title = coerce_string_max_len(data.get("title"), _MAX_TITLE_CHARS) or ""
    title = " ".join(title.split())
    if not title:
        return jsonify({"error": "title must be a non-empty string."}), 400
    ctx, err = _gates()
    if err:
        return err
    inv, settings, cfg = ctx
    key, rec = _record(inv, data.get("citation_key"))
    if not key:
        return jsonify({"error": "citation_key must be a non-empty string."}), 400
    if rec is None:
        return jsonify({"error": f"Unknown citation key: {key}"}), 404
    zot = rec.zotero_item
    if zot is None:
        return jsonify({"error": "That record has no Zotero item.", "reason": "no_zotero_item"}), 409
    expected = data.get("expected_title")
    expected = (coerce_string_max_len(expected, _MAX_TITLE_CHARS) if expected is not None
                else (zot.title or ""))

    # Evidence: the title must be printed where the document identifies itself.
    pdfs = [Path(p) for p in (rec.pdf_paths or ())]
    raw_pdf = data.get("pdf")
    if raw_pdf:
        from tagging.writers import resolve_biblio_pdf_rel
        abs_path, _rel, perr = resolve_biblio_pdf_rel(raw_pdf, settings)
        if perr:
            body, status = perr
            return jsonify(body), status
        pdfs = [Path(abs_path)]
    evidence = {"found": False, "reason": "the record has no PDF to check against", "pdf": None}
    for pdf in pdfs:
        evidence = _authenticate_title(pdf, title)
        if evidence["found"]:
            break
    acknowledged = coerce_bool(data.get("accept_unauthenticated")) is True
    if not evidence["found"] and not acknowledged and not dry_run:
        return jsonify({"error": ("The proposed title is not printed in the PDF's front matter "
                                  f"({evidence.get('reason')}). Re-send with accept_unauthenticated: "
                                  "true to write it anyway."),
                        "reason": "title_not_authenticated", "evidence": evidence}), 409

    prepared = zotero_metadata_prepare(cfg, zot.item_key, {"title": title}, {"title": expected}, row=zot)
    if "early" in prepared:
        early = prepared["early"]
        status = 200 if early.get("ok") else {"stale": 409, "unreachable": 503,
                                               "unselected_field_would_change": 409}.get(early.get("status"), 502)
        payload = {**early, "citation_key": key, "evidence": evidence}
        if not early.get("ok"):
            payload.setdefault("error", early.get("message"))
        return jsonify(payload), status
    current_title = prepared["commit"]["before_fields"].get("title", "")
    if dry_run:
        return jsonify({"ok": True, "dry_run": True, "citation_key": key, "item_key": zot.item_key,
                        "current_title": current_title, "title": title, "evidence": evidence,
                        "message": "Nothing written. Confirm to retitle."})

    if not _ZOTRECORD_GATE.acquire(blocking=False):
        return jsonify({"error": "Another record operation is running; try again."}), 429
    try:
        vault_root = settings.vault_root

        def _journal(_epoch):
            manifest = journal.load(vault_root, cfg)
            op = zotero_metadata_journal(prepared=prepared, manifest=manifest, citation_key=key,
                                         campaign="cleanup-T9")
            journal.prune(vault_root, cfg, manifest, protect_op_ids={op["op_id"]})
            journal.save(vault_root, cfg, manifest)
            return op["op_id"]
        op_id, lock_err = _hold(_journal)
        if lock_err:
            return jsonify({"error": lock_err}), 503
        result, status, new_version = zotero_metadata_patch(prepared)

        def _outcome(_epoch):
            # Reload, never save the phase-2 object: a writer that took the
            # lock in between keeps its ops. A PATCH that provably did not
            # land is `failed` (an un-run write must not revert as if it had);
            # a lost response stays `outcome_unknown`, retryable by hand.
            manifest = journal.load(vault_root, cfg)
            op = journal.find_op(manifest, op_id)
            if op is not None:
                if result.get("status") == "outcome_unknown":
                    op["state"] = "outcome_unknown"
                elif not result.get("ok") and new_version is None:
                    op["state"] = "failed"
                else:
                    op["state"] = "applied"
                    if new_version is not None:
                        op["version_after"] = int(new_version)
                journal.save(vault_root, cfg, manifest)
            return True
        _, out_err = _hold(_outcome, ttl=300)
        payload = {**result, "citation_key": key, "item_key": zot.item_key, "op_id": op_id,
                   "before_title": current_title, "title": title, "evidence": evidence}
        if not result.get("ok"):
            payload.setdefault("error", result.get("message"))
        if out_err:
            payload["journal_warning"] = out_err
        return jsonify(payload), status
    except Exception as exc:  # noqa: BLE001
        logger.exception("zotrecord retitle failed")
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        _ZOTRECORD_GATE.release()


# ---------------------------------------------------------------------------
# T8 — merge
# ---------------------------------------------------------------------------

def _tex_ids_after(extra: str, alias: str) -> str:
    """*extra* with *alias* appended to its ``tex.ids:`` line (or a new line)."""
    lines = (extra or "").splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower().startswith(_TEX_IDS_PREFIX):
            existing = [x.strip() for x in line.split(":", 1)[1].replace(";", ",").split(",") if x.strip()]
            if alias in existing:
                return extra
            lines[i] = f"{_TEX_IDS_PREFIX} " + ", ".join(existing + [alias])
            return "\n".join(lines)
    lines.append(f"{_TEX_IDS_PREFIX} {alias}")
    return "\n".join(lines)


def _plan_merge(inv, settings, cfg, keep_key, retire_key):
    """Lock-free reads + the refusal ladder. ``(plan, None)`` or ``(None, (body, status))``."""
    if keep_key == retire_key:
        return None, ({"error": "keep and retire must be different citation keys."}, 400)
    keep = inv.records.get(keep_key)
    retire = inv.records.get(retire_key)
    for name, rec in (("keep", keep), ("retire", retire)):
        if rec is None:
            return None, ({"error": f"Unknown citation key ({name})."}, 404)
        if rec.zotero_item is None:
            return None, ({"error": f"The {name} record has no Zotero item.", "reason": "no_zotero_item"}, 409)
        if rec.zotero_item.library_type == "group":
            return None, ({"error": "Group-library items cannot be merged by this verb.",
                           "reason": "group_library"}, 409)
    if retire.obsidian_note is not None:
        return None, ({"error": ("The retired record owns an Obsidian note. Keep THAT record instead "
                                 "(swap keep/retire): the note is what must survive."),
                       "reason": "note_on_retired_side"}, 409)
    transport = _zotero_transport(cfg)
    if not transport.can_write:
        return None, ({"error": "The Zotero transport cannot write (no API key for the web transport)."}, 503)
    api_key = transport.api_key if transport.is_web else None
    kz, rz = keep.zotero_item, retire.zotero_item
    try:
        keep_identity = _zotero_read_identity(transport, kz)
        retire_identity = _zotero_read_identity(transport, rz)
        if keep_identity != retire_identity:
            raise zotero_write.ZoteroVersionConflict("The merge selections belong to different Zotero databases.")
        lib_path = _zotero_lib_path(transport, kz)
        keep_data, keep_version = zotero_write.get_item(transport.base_url, lib_path, kz.item_key, api_key=api_key, **keep_identity)
        retire_data, retire_version = zotero_write.get_item(transport.base_url, lib_path, rz.item_key, api_key=api_key, **retire_identity)
        keep_children = zotero_write.get_item_children(transport.base_url, lib_path, kz.item_key, api_key=api_key, **keep_identity)
        retire_children = zotero_write.get_item_children(transport.base_url, lib_path, rz.item_key, api_key=api_key, **retire_identity)
    except zotero_write.ZoteroUnavailable as exc:
        return None, ({"error": f"Zotero is not reachable: {sanitise_error_msg(exc)}"}, 503)
    except zotero_write.ZoteroError as exc:
        return None, ({"error": sanitise_error_msg(exc)}, 502)
    for name, d in (("keep", keep_data), ("retire", retire_data)):
        if d.get("deleted"):
            return None, ({"error": f"The {name} item is already in Zotero's trash."}, 409)
        if d.get("itemType") in ("note", "attachment", "annotation"):
            return None, ({"error": f"The {name} item is not a bibliographic item."}, 409)
    keep_paths = {str(c.get("path") or "") for c in keep_children
                  if c.get("itemType") == "attachment" and c.get("path")}
    keep_notes = [c for c in keep_children if c.get("itemType") == "note"]
    retire_notes = [c for c in retire_children if c.get("itemType") == "note"]
    if keep_notes and retire_notes:
        return None, ({"error": ("Both items carry a reading note; merging would put two notes under "
                                 "one item, which the note sync cannot serve. Merge the notes by hand "
                                 "first."), "reason": "both_zotero_notes"}, 409)
    reparent, skipped = [], []
    for c in retire_children:
        if c.get("itemType") == "attachment" and c.get("path") and str(c["path"]) in keep_paths:
            skipped.append(c)          # the kept item already links this file
        else:
            reparent.append(c)
    keep_tags_raw = list(keep_data.get("tags") or [])
    keep_names = {str(t.get("tag")) for t in keep_tags_raw if isinstance(t, dict)}
    tags_added = sorted({str(t.get("tag")) for t in (retire_data.get("tags") or [])
                         if isinstance(t, dict) and str(t.get("tag")) not in keep_names})
    extra_before = str(keep_data.get("extra") or "")
    extra_after = _tex_ids_after(extra_before, retire_key)
    from api.routes._audit_tagwrite import read_aliases
    aliases_added = []
    note_rel = None
    if keep.obsidian_note is not None:
        try:
            text = keep.obsidian_note.path.read_text(encoding="utf-8")
            note_rel = str(keep.obsidian_note.path.resolve().relative_to(settings.vault_root.resolve()))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            return None, ({"error": f"Cannot read the kept note: {sanitise_error_msg(exc)}"}, 400)
        if retire_key not in read_aliases(text):
            aliases_added = [retire_key]
    plan = {
        "keep": {"citation_key": keep_key, "item_key": kz.item_key, "title": keep_data.get("title"),
                 "version": int(keep_version), "note": note_rel, "child_notes": len(keep_notes)},
        "retire": {"citation_key": retire_key, "item_key": rz.item_key, "title": retire_data.get("title"),
                   "version": int(retire_version), "child_notes": len(retire_notes),
                   "pdfs": [str(Path(p).name) for p in (retire.pdf_paths or ())]},
        "reparent": [str(c.get("key")) for c in reparent],
        "reparent_detail": [{"key": c.get("key"), "type": c.get("itemType"), "link_mode": c.get("linkMode"),
                             "title": c.get("title") or ""} for c in reparent],
        "skipped_children": [str(c.get("key")) for c in skipped],
        "tags_added": tags_added,
        "extra_before": extra_before, "extra_after": extra_after,
        "aliases_added": aliases_added,
        "transport": transport.kind,
        "mapping_note": ("mapping.json is not touched here; re-key the retired record's PDFs "
                         "through POST /api/audit/mapping before or after the merge."),
    }
    internals = {"transport": transport, "api_key": api_key, "lib_path": lib_path,
                 "keep": keep, "retire": retire, "keep_data": keep_data, "keep_version": keep_version,
                 "retire_data": retire_data, "retire_version": retire_version,
                 "reparent": reparent, "keep_tags_raw": keep_tags_raw}
    return (plan, internals), None


@zotrecord_bp.route("/api/zotrecord/merge-preview", methods=["POST"])
def api_zotrecord_merge_preview():
    """Body ``{keep, retire}`` → the merge plan; reads only."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    ctx, err = _gates()
    if err:
        return err
    inv, settings, cfg = ctx
    keep_key = coerce_non_empty_string(data.get("keep"), max_len=128)
    retire_key = coerce_non_empty_string(data.get("retire"), max_len=128)
    if not keep_key or not retire_key:
        return jsonify({"error": "keep and retire citation keys are required."}), 400
    planned, perr = _plan_merge(inv, settings, cfg, keep_key, retire_key)
    if perr:
        body, status = perr
        return jsonify(body), status
    plan, _internals = planned
    return jsonify({"ok": True, "plan": plan})


@zotrecord_bp.route("/api/zotrecord/merge", methods=["POST"])
def api_zotrecord_merge():
    """Body ``{keep, retire, confirm | dry_run}``. See the module docstring."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    dry_run = coerce_bool(data.get("dry_run")) is True
    if not dry_run and coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Merge must be explicitly confirmed (confirm: true)."}), 400
    ctx, err = _gates()
    if err:
        return err
    inv, settings, cfg = ctx
    keep_key = coerce_non_empty_string(data.get("keep"), max_len=128)
    retire_key = coerce_non_empty_string(data.get("retire"), max_len=128)
    if not keep_key or not retire_key:
        return jsonify({"error": "keep and retire citation keys are required."}), 400
    planned, perr = _plan_merge(inv, settings, cfg, keep_key, retire_key)
    if perr:
        body, status = perr
        return jsonify(body), status
    plan, it = planned
    if dry_run:
        return jsonify({"ok": True, "dry_run": True, "plan": plan, "message": "Nothing written. Confirm to merge."})
    if not _ZOTRECORD_GATE.acquire(blocking=False):
        return jsonify({"error": "Another record operation is running; try again."}), 429
    try:
        return _run_merge(settings, cfg, plan, it, keep_key, retire_key)
    except Exception as exc:  # noqa: BLE001
        logger.exception("zotrecord merge failed")
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        _ZOTRECORD_GATE.release()


def _run_merge(settings, cfg, plan, it, keep_key, retire_key):
    vault_root = settings.vault_root
    transport, lib_path = it["transport"], it["lib_path"]
    kz = it["keep"].zotero_item
    batch_id = journal.new_op_id({})
    steps: list[dict] = []
    ops: dict[str, list[str]] = {"reparent": [], "tag": [], "extra": [], "trash": [], "aliases": []}

    # Prepared network halves for the kept item's tags and Extra. Both read
    # the item again here (the plan's read may be seconds old); the PATCH
    # version guard is what makes any gap safe.
    tag_prepared = None
    if plan["tags_added"]:
        from tagging.writers import _zotero_prepare
        tag_prepared, tag_early, _st = _zotero_prepare(rec=it["keep"], valid=list(plan["tags_added"]),
                                                       rejected=[], cfg=cfg, citation_key=keep_key)
        if tag_prepared is None and not (tag_early or {}).get("ok"):
            return jsonify({"error": tag_early.get("error") or "Zotero read failed"}), 503
    extra_prepared = None
    if plan["extra_after"] != plan["extra_before"]:
        extra_prepared = zotero_metadata_prepare(cfg, kz.item_key, {"extra": plan["extra_after"]},
                                                 {"extra": plan["extra_before"]}, row=kz)
        if "early" in extra_prepared and not extra_prepared["early"].get("ok"):
            return jsonify({"error": extra_prepared["early"].get("message"),
                            **extra_prepared["early"]}), 409

    # ---- phase 2: ONE hold — the local note write + every op journalled ----
    def _journal(_epoch):
        from tagging.writers import _zotero_journal
        manifest = journal.load(vault_root, cfg)
        if plan["aliases_added"]:
            res, status = _note_aliases_locked(rec=it["keep"], aliases=plan["aliases_added"], cfg=cfg,
                                               vault_root=vault_root, citation_key=keep_key,
                                               manifest=manifest, batch_id=batch_id, defer_save=True)
            steps.append({"step": "note_aliases", **res, "ok": status == 200 and res.get("ok", False)})
            if status != 200:
                journal.save(vault_root, cfg, manifest)
                return False
            if res.get("op_id"):
                ops["aliases"].append(res["op_id"])
        for child in it["reparent"]:
            op = zotero_child_reparent_journal(transport=transport, lib_path=lib_path, child=child,
                                               before_parent=it["retire"].zotero_item.item_key,
                                               after_parent=kz.item_key, manifest=manifest,
                                               batch_id=batch_id, citation_key=keep_key,
                                               server_id=getattr(it["keep_version"], "server_id", None))
            ops["reparent"].append(op["op_id"])
        if extra_prepared is not None and "commit" in extra_prepared:
            op = zotero_metadata_journal(prepared=extra_prepared, manifest=manifest, batch_id=batch_id,
                                         citation_key=keep_key, campaign="cleanup-T8")
            ops["extra"].append(op["op_id"])
        if tag_prepared is not None:
            op = _zotero_journal(prepared=tag_prepared, citation_key=keep_key, manifest=manifest,
                                 batch_id=batch_id)
            ops["tag"].append(op["op_id"])
        op = zotero_item_trash_journal(transport=transport, lib_path=lib_path,
                                       item_key=it["retire"].zotero_item.item_key,
                                       version=it["retire_version"], data=it["retire_data"],
                                       manifest=manifest, batch_id=batch_id, citation_key=retire_key)
        ops["trash"].append(op["op_id"])
        journal.prune(vault_root, cfg, manifest, protect_batch_id=batch_id)
        journal.save(vault_root, cfg, manifest)
        return True
    journalled, lock_err = _hold(_journal)
    if lock_err:
        return jsonify({"error": lock_err}), 503
    if not journalled:
        return jsonify({"ok": False, "batch_id": batch_id, "steps": steps,
                        "error": "The kept note could not be edited; nothing was sent to Zotero."}), 422

    # ---- phase 3: lock-free PATCHes, stop at the first failure ------------
    applied: dict[str, int | None] = {}
    extras: dict[str, dict] = {}
    all_ops = ops["reparent"] + ops["extra"] + ops["tag"] + ops["trash"]
    failed_at = None
    http_status = 200
    manifest_view = journal.load(vault_root, cfg)
    by_id = {op["op_id"]: op for op in manifest_view["ops"] if op.get("batch_id") == batch_id}
    try:
        for op_id in ops["reparent"]:
            res, status, ver = zotero_child_reparent_patch(transport=transport, lib_path=lib_path, op=by_id[op_id])
            steps.append({"step": "reparent", "op_id": op_id, **res})
            if not res.get("ok"):
                failed_at, http_status = op_id, status
                break
            applied[op_id] = ver
        if failed_at is None and ops["extra"]:
            res, status, ver = zotero_metadata_patch(extra_prepared)
            steps.append({"step": "extra", "op_id": ops["extra"][0], **res})
            if res.get("status") in _LANDED_METADATA:
                # The PATCH landed; journal the unexpected/readback fields and
                # keep going. Revert restores those too.
                applied[ops["extra"][0]] = ver
                extra: dict = {}
                if res.get("unexpected_changes"):
                    extra["unexpected_changes"] = res["unexpected_changes"]
                if res.get("status") == "readback_differs":
                    extra["readback_fields"] = res.get("fields")
                if extra:
                    extras[ops["extra"][0]] = extra
            elif not res.get("ok"):
                failed_at, http_status = ops["extra"][0], status
            else:
                applied[ops["extra"][0]] = ver
        if failed_at is None and ops["tag"]:
            from tagging.writers import _zotero_patch
            # The Extra PATCH moved the item's version: refresh the guard.
            try:
                ver_now, raw_now = zotero_write.get_item_tags_full(
                    transport.base_url, lib_path, kz.item_key, api_key=it["api_key"],
                    **({"server_id": getattr(tag_prepared["version"], "server_id", None) or ""}
                       if not transport.is_web else {}))
                if raw_now != tag_prepared["before_raw"]:
                    raise zotero_write.ZoteroVersionConflict(
                        "Tags changed during merge. Inspect the retained partial operation.")
                tag_prepared["version"] = ver_now
            except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroError,
                    zotero_write.ZoteroVersionConflict) as exc:
                steps.append({"step": "tags", "op_id": ops["tag"][0], "ok": False, "status": "error",
                              "message": str(exc)})
                failed_at = ops["tag"][0]
                http_status = 409 if isinstance(exc, zotero_write.ZoteroVersionConflict) else 502
            if failed_at is None:
                res, status, ver = _zotero_patch(tag_prepared)
                steps.append({"step": "tags", "op_id": ops["tag"][0], **res})
                if status != 200:
                    failed_at, http_status = ops["tag"][0], status
                else:
                    applied[ops["tag"][0]] = ver
        if failed_at is None:
            try:
                children = zotero_write.get_item_children(
                    transport.base_url, lib_path, it["retire"].zotero_item.item_key,
                    api_key=it["api_key"],
                    server_id=getattr(it["retire"].zotero_item, "server_id", None))
            except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroError) as exc:
                steps.append({"step": "trash", "op_id": ops["trash"][0], "ok": False, "status": "error",
                              "message": str(exc)})
                failed_at, http_status = ops["trash"][0], 502
            else:
                leftover = [str(c.get("key")) for c in children
                            if c.get("key") and not c.get("deleted")]
                expected = set(plan.get("skipped_children") or [])
                unexpected = [k for k in leftover if k not in expected]
                if unexpected:
                    steps.append({"step": "trash", "op_id": ops["trash"][0], "ok": False,
                                  "status": "leftover_children", "leftover": unexpected,
                                  "message": ("The retired item still has children that this "
                                              "merge did not reparent or skip "
                                              f"({', '.join(unexpected[:6])}); not trashing it.")})
                    failed_at, http_status = ops["trash"][0], 409
        if failed_at is None:
            try:
                _d, ver_now = zotero_write.get_item(transport.base_url, lib_path,
                                                    it["retire"].zotero_item.item_key, api_key=it["api_key"],
                                                    **_zotero_read_identity(transport, it["retire"].zotero_item))
            except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroError) as exc:
                steps.append({"step": "trash", "op_id": ops["trash"][0], "ok": False, "status": "error",
                              "message": str(exc)})
                failed_at, http_status = ops["trash"][0], 502
            else:
                res, status, ver = zotero_item_trash_patch(transport=transport, lib_path=lib_path,
                                                           op=by_id[ops["trash"][0]], version=ver_now)
                steps.append({"step": "trash", "op_id": ops["trash"][0], **res})
                if not res.get("ok"):
                    failed_at, http_status = ops["trash"][0], status
                else:
                    applied[ops["trash"][0]] = ver
    finally:
        # ---- phase 4: record outcomes on a RELOADED manifest ---------------
        if failed_at is None:
            failed = set()
        else:
            idx = all_ops.index(failed_at)
            # A known rejection marks the attempted op failed too. An
            # uncertain outcome preserves its existing journal evidence.
            last = steps[-1] if steps else {}
            failed = set(all_ops[idx + (1 if last.get("status") == "outcome_unknown" or last.get("outcome_unknown") else 0):])
        rec_err = _record_outcome(vault_root, cfg, batch_id, applied=applied, failed=failed,
                                  extras=extras)
    payload = {"ok": failed_at is None, "batch_id": batch_id, "keep": keep_key, "retire": retire_key,
               "steps": steps, "skipped_children": plan["skipped_children"],
               "reparented": [oid for oid in ops["reparent"] if oid in applied],
               "undo": {"route": "POST /api/tagging/restore", "body": {"batch_id": batch_id, "confirm": True}},
               "note": (("The retired item is in Zotero's trash (not deleted). Re-key its PDFs in mapping.json "
                        "through POST /api/audit/mapping; the bibliography drops the retired key at the "
                        "next Better BibTeX export, and the kept entry gains it as `ids`.")
                        if ops["trash"][0] in applied else
                        "The merge stopped before a confirmed trash. Inspect the partial steps and retained journal before recovery.")}
    if failed_at is not None:
        payload["error"] = f"stopped at {steps[-1].get('step')}: {steps[-1].get('message') or steps[-1].get('error')}"
    if rec_err:
        payload["journal_warning"] = rec_err
    return jsonify(payload), (200 if failed_at is None else http_status)


# ---------------------------------------------------------------------------
# trash a standalone record (maintainer decision 2026-09-17: "get rid of" the
# two chapter entries that have no PDF and no note)
# ---------------------------------------------------------------------------

@zotrecord_bp.route("/api/zotrecord/trash", methods=["POST"])
def api_zotrecord_trash():
    """Body ``{citation_key, accept_children?, confirm | dry_run}``. Moves the
    record's Zotero item to the trash (never a DELETE; the undo is exact).
    Refuses a record that still owns a PDF or an Obsidian note — those are
    merges or Phase 2 work — and an item with children unless acknowledged
    (Zotero trashes children with their parent)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    dry_run = coerce_bool(data.get("dry_run")) is True
    if not dry_run and coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Trash must be explicitly confirmed (confirm: true)."}), 400
    ctx, err = _gates()
    if err:
        return err
    inv, settings, cfg = ctx
    key, rec = _record(inv, data.get("citation_key"))
    if not key:
        return jsonify({"error": "citation_key must be a non-empty string."}), 400
    if rec is None:
        return jsonify({"error": f"Unknown citation key: {key}"}), 404
    zot = rec.zotero_item
    if zot is None:
        return jsonify({"error": "That record has no Zotero item.", "reason": "no_zotero_item"}), 409
    if zot.library_type == "group":
        return jsonify({"error": "Group-library items are not trashed by this verb.", "reason": "group_library"}), 409
    if rec.pdf_paths:
        return jsonify({"error": "The record still owns a PDF; re-key or hold it first.",
                        "reason": "record_has_pdf"}), 409
    if rec.obsidian_note is not None:
        return jsonify({"error": "The record has an Obsidian note; this verb is for empty entries.",
                        "reason": "record_has_note"}), 409
    transport = _zotero_transport(cfg)
    if not transport.can_write:
        return jsonify({"error": "The Zotero transport cannot write (no API key for the web transport)."}), 503
    api_key = transport.api_key if transport.is_web else None
    try:
        identity = _zotero_read_identity(transport, zot)
        lib_path = _zotero_lib_path(transport, zot)
        item_data, version = zotero_write.get_item(transport.base_url, lib_path, zot.item_key, api_key=api_key, **identity)
        children = zotero_write.get_item_children(transport.base_url, lib_path, zot.item_key, api_key=api_key, **identity)
    except zotero_write.ZoteroUnavailable as exc:
        return jsonify({"error": f"Zotero is not reachable: {sanitise_error_msg(exc)}"}), 503
    except zotero_write.ZoteroError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 502
    if item_data.get("deleted"):
        return jsonify({"ok": True, "status": "skipped", "message": "Already in Zotero's trash.", "citation_key": key})
    if children and coerce_bool(data.get("accept_children")) is not True:
        return jsonify({"error": f"The item carries {len(children)} child item(s), which Zotero trashes with it; "
                                 "re-send with accept_children: true.", "reason": "item_has_children",
                        "children": [{"key": c.get("key"), "type": c.get("itemType"), "title": c.get("title") or ""}
                                     for c in children]}), 409
    plan = {"citation_key": key, "item_key": zot.item_key, "title": item_data.get("title"),
            "children": len(children), "transport": transport.kind}
    if dry_run:
        return jsonify({"ok": True, "dry_run": True, "plan": plan, "message": "Nothing written. Confirm to trash."})
    if not _ZOTRECORD_GATE.acquire(blocking=False):
        return jsonify({"error": "Another record operation is running; try again."}), 429
    try:
        vault_root = settings.vault_root
        holder: dict = {}

        def _journal(_epoch):
            manifest = journal.load(vault_root, cfg)
            op = zotero_item_trash_journal(transport=transport, lib_path=lib_path, item_key=zot.item_key,
                                           version=version, data=item_data, manifest=manifest, citation_key=key)
            holder["op"] = dict(op)
            journal.prune(vault_root, cfg, manifest, protect_op_ids={op["op_id"]})
            journal.save(vault_root, cfg, manifest)
            return op["op_id"]
        op_id, lock_err = _hold(_journal)
        if lock_err:
            return jsonify({"error": lock_err}), 503
        result, status, new_version = zotero_item_trash_patch(transport=transport, lib_path=lib_path,
                                                              op=holder["op"], version=version)

        def _outcome(_epoch):
            manifest = journal.load(vault_root, cfg)
            op = journal.find_op(manifest, op_id)
            if op is not None:
                if not result.get("ok"):
                    op["state"] = "failed" if result.get("status") in ("conflict", "unsupported") else "outcome_unknown"
                else:
                    op["state"] = "applied"
                    if new_version is not None:
                        op["version_after"] = int(new_version)
                journal.save(vault_root, cfg, manifest)
            return True
        _, out_err = _hold(_outcome, ttl=300)
        payload = {**result, "citation_key": key, "op_id": op_id, "plan": plan,
                   "undo": {"route": "POST /api/tagging/restore", "body": {"op_id": op_id, "confirm": True}}}
        if not result.get("ok"):
            payload.setdefault("error", result.get("message"))
        if out_err:
            payload["journal_warning"] = out_err
        return jsonify(payload), status
    except Exception as exc:  # noqa: BLE001
        logger.exception("zotrecord trash failed")
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        _ZOTRECORD_GATE.release()
