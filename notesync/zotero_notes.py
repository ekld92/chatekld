"""Writing a reference note's region BACK to Zotero — the push half of the sync.

Phase 4 of the sync plan (2026-09-04). Two verbs, both journalled in the
TAGGING journal (the one that already holds every external Zotero write and
its restore mapping inline) and both reversible from the Tagging → Restore
panel:

* ``zotero_note_write`` — PATCH an existing child note's HTML. The op stores
  ``before_html`` (read from the API immediately before the write, never from
  the local SQLite, which lags the server) and ``after_html``; the revert
  PATCHes ``before_html`` back if the note still reads ``after_html``.
* ``zotero_note_create`` — POST a new child note. The revert TRASHES it
  (``set_item_deleted``), never deletes: the key survives a round trip.

Choreography is the tag writers' (D4): every network READ runs before the
obsidian op-lock, the journal append is the only thing under it, the PATCH
runs lock-free, and the ``If-Unmodified-Since-Version`` guard — not the lock —
prevents a lost update. Transport, library path and key resolution reuse
``tagging.writers`` verbatim so a caller can never build ``users/0`` by hand
against api.zotero.org.

This module never touches the vault. The caller writes the sync base record
after a successful commit; that base stores the HTML *we sent*, so the next
pass does not flag our own normalisation as a Zotero edit.
"""

from __future__ import annotations

from tagging import journal, zotero_write
# Bound from the real module, not through ``zotero_write``: a transport shim
# carries the historical exception set only.
from tagging.zotero_write import ZoteroNotSent
from tagging.writers import _zotero_lib_path, _zotero_transport

OP_WRITE = "zotero_note_write"
OP_CREATE = "zotero_note_create"


def _target(cfg: dict, zot):
    """``(transport, api_key, lib_path)`` or ``{"early": …}`` when unusable."""
    transport = _zotero_transport(cfg)
    api_key = transport.api_key if transport.is_web else None
    if transport.is_web and not api_key:
        return None, {"ok": False, "status": "error", "message": (
            f"Set {zotero_write.API_KEY_ENV} to write to the Zotero web API.")}
    try:
        lib_path = _zotero_lib_path(transport, zot)
    except zotero_write.ZoteroUnavailable:
        return None, {"ok": False, "status": "unreachable",
                      "message": "Zotero is not reachable."}
    except zotero_write.ZoteroError as exc:
        return None, {"ok": False, "status": "error", "message": str(exc)}
    return (transport, api_key, lib_path), None


def prepare_write(cfg: dict, *, zot, note_key: str, after_html: str, expected_html=None, expected_version=None):
    """Read side of a note-body write (no lock). Returns ``{"early"}`` or
    ``{"commit"}``; the commit carries the CURRENT server HTML as ``before``."""
    tgt, early = _target(cfg, zot)
    if early is not None:
        return {"early": early}
    transport, api_key, lib_path = tgt
    identity_kwargs: dict = ({"server_id": getattr(zot, "server_id", None) or ""}
                             if not transport.is_web else {})
    try:
        data, version = zotero_write.get_item(
            transport.base_url, lib_path, note_key, api_key=api_key,
            **identity_kwargs)
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "unreachable",
                          "message": "Zotero is not reachable."}}
    except zotero_write.ZoteroError as exc:
        return {"early": {"ok": False, "status": "error", "message": str(exc)}}
    if data.get("itemType") != "note":
        return {"early": {"ok": False, "status": "error",
                          "message": "That Zotero item is not a note."}}
    if data.get("deleted"):
        return {"early": {"ok": False, "status": "error",
                          "message": "That Zotero note is in the trash."}}
    if not getattr(zot, 'item_key', None) or data.get('parentItem') != zot.item_key:
        return {"early": {"ok": False, "status": "conflict",
                          "message": "The note moved to another parent. Review and scan again."}}
    before_html = str(data.get("note") or "")
    if expected_version is not None and int(version) != int(expected_version):
        return {"early": {"ok": False, "status": "conflict", "message": "Zotero version changed since the preview."}}
    from notesync.safety import content_issues
    if content_issues(before_html) or content_issues(after_html):
        return {"early": {"ok": False, "status": "unsupported", "message": "Note images or unsupported content require explicit review."}}
    from notesync.convert import same_note_html
    if expected_html is not None and not same_note_html(before_html, expected_html):
        return {"early": {"ok": False, "status": "conflict", "message": "Zotero changed since the preview."}}
    if before_html == after_html:
        return {"early": {"ok": True, "status": "skipped", "version": version,
                          "message": "Zotero already holds this text."}}
    return {"commit": {
        "transport": transport, "api_key": api_key, "lib_path": lib_path,
        "note_key": note_key, "parent_item": data.get("parentItem"),
        "version": version, "before_html": before_html, "after_html": after_html,
    }}


def journal_write(*, prepared, citation_key: str, manifest, batch_id=None) -> dict:
    """Append (do NOT save) the ``zotero_note_write`` op. Caller holds the lock."""
    c = prepared["commit"]
    op = {
        "op_id": journal.new_op_id(manifest), "kind": OP_WRITE,
        "ts": journal.now_iso(), "citation_key": citation_key,
        "item_key": c["note_key"], "parent_item": c["parent_item"],
        "lib_path": c["lib_path"], "base_url": c["transport"].base_url,
        "transport": c["transport"].kind,
        "before_html": c["before_html"], "after_html": c["after_html"],
        "version_before": c["version"], "server_id": getattr(c["version"], "server_id", None), "state": "outcome_unknown",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    return op


def patch_write(prepared) -> tuple[dict, int | None]:
    """The lock-free PATCH. ``(result, new_version | None)``; the version is
    RE-READ after the write (never taken from the write response)."""
    c = prepared["commit"]
    try:
        zotero_write.set_item_note(
            c["transport"].base_url, c["lib_path"], c["note_key"], c["after_html"],
            c["version"], api_key=c["api_key"])
    except ZoteroNotSent as exc:
        # The connection failed before the request left: nothing was sent,
        # so the op is `failed`, never `outcome_unknown` (2026-09-17, B2).
        return {"ok": False, "status": "not_sent",
                "message": "Zotero refused the connection; nothing was sent. " + str(exc)}, None
    except zotero_write.ZoteroVersionConflict:
        return {"ok": False, "status": "conflict",
                "message": "The note changed in Zotero since it was read; refresh and retry."}, None
    except zotero_write.ZoteroWriteUnsupported as exc:
        return {"ok": False, "status": "unsupported", "message": str(exc)}, None
    except zotero_write.ZoteroUnavailable:
        return {"ok": False, "status": "unreachable",
                "message": "Zotero is not reachable."}, None
    except zotero_write.ZoteroError as exc:
        return {"ok": False, "status": "error", "message": str(exc)}, None
    try:
        _data, version = zotero_write.get_item(
            c["transport"].base_url, c["lib_path"], c["note_key"], api_key=c["api_key"],
            **({"server_id": c["version"].server_id} if getattr(c["version"], "server_id", None) else {}))
    except (zotero_write.ZoteroVersionConflict, zotero_write.ZoteroUnavailable, zotero_write.ZoteroError) as exc:
        # The PATCH was sent; only its readback failed. Never read that as a
        # refusal: the outcome is unknown until a live check.
        return {"ok": False, "status": "outcome_unknown",
                "message": "The write was sent but its readback failed; inspect before resuming. " + str(exc)}, None
    if (_data.get('itemType') != 'note' or _data.get('note') != c['after_html'] or _data.get('parentItem') != c['parent_item']
            or _data.get('deleted')):
        return {"ok": False, "status": "outcome_unknown", "message": "Write readback differs; inspect before resuming."}, None
    from core.utils import log_vault_write
    log_vault_write("notesync_zotero_note_write", c["note_key"],
                    f"{len(c['before_html'])}→{len(c['after_html'])} chars")
    return {"ok": True, "status": "applied"}, version


def prepare_create(cfg: dict, *, zot, parent_key: str, html: str):
    """Read side of a child-note CREATE (no lock): resolves the target and
    confirms the parent exists and is not itself a note/attachment."""
    from notesync.safety import content_issues
    if content_issues(html):
        return {"early": {"ok": False, "status": "unsupported", "message": "Note images or unsupported content require explicit review."}}
    tgt, early = _target(cfg, zot)
    if early is not None:
        return {"early": early}
    transport, api_key, lib_path = tgt
    identity_kwargs: dict = ({"server_id": getattr(zot, "server_id", None) or ""}
                             if not transport.is_web else {})
    try:
        data, _version = zotero_write.get_item(
            transport.base_url, lib_path, parent_key, api_key=api_key,
            **identity_kwargs)
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "unreachable",
                          "message": "Zotero is not reachable."}}
    except zotero_write.ZoteroError as exc:
        return {"early": {"ok": False, "status": "error", "message": str(exc)}}
    if data.get("itemType") in ("note", "attachment") or data.get("deleted"):
        return {"early": {"ok": False, "status": "error",
                          "message": "The parent is not a live bibliographic item."}}
    from audit.core.bib import normalise_doi
    if (parent_key != getattr(zot, 'item_key', None)
            or data.get('itemType') != getattr(zot, 'item_type', None)
            or (data.get('title') or '') != (getattr(zot, 'title', None) or '')
            or normalise_doi(data.get('DOI') or '') != normalise_doi(getattr(zot, 'doi', None) or '')):
        return {"early": {"ok": False, "status": "conflict",
                          "message": "Zotero parent identity changed since the scan. Review and scan again."}}
    return {"commit": {"transport": transport, "api_key": api_key,
                       "lib_path": lib_path, "parent_key": parent_key, "html": html,
                       "parent_data": data, "parent_version": _version,
                       "server_id": getattr(_version, 'server_id', None)}}


def post_create(prepared, *, on_created=None) -> tuple[dict, str | None, int | None]:
    """The lock-free POST. ``(result, note_key, version)``."""
    c = prepared["commit"]
    try:
        if 'parent_version' in c:
            bound = {'server_id': c['server_id']} if c.get('server_id') else {}
            children = zotero_write.get_item_children(c['transport'].base_url, c['lib_path'],
                c['parent_key'], api_key=c['api_key'], **bound)
            if any(child.get('itemType') == 'note' and not child.get('deleted') for child in children):
                raise zotero_write.ZoteroVersionConflict('A Zotero note already exists. Refresh before creating another.')
            parent, version = zotero_write.get_item(c['transport'].base_url, c['lib_path'],
                c['parent_key'], api_key=c['api_key'], **bound)
            if parent != c['parent_data'] or version != c['parent_version']:
                raise zotero_write.ZoteroVersionConflict('Parent changed before note creation. Review and scan again.')
        note_key = zotero_write.create_child_note(
            c["transport"].base_url, c["lib_path"], c["parent_key"], c["html"],
            api_key=c["api_key"], **({'server_id': c['server_id']} if c.get('server_id') else {}))
    except zotero_write.ZoteroVersionConflict as exc:
        return {"ok": False, "status": "conflict", "message": str(exc)}, None, None
    except zotero_write.ZoteroWriteUnsupported as exc:
        return {"ok": False, "status": "unsupported", "message": str(exc)}, None, None
    except zotero_write.ZoteroUnavailable:
        return {"ok": False, "status": "unreachable",
                "message": "Zotero is not reachable."}, None, None
    except zotero_write.ZoteroError as exc:
        return {"ok": False, "status": "error", "message": str(exc)}, None, None
    if on_created is not None:
        try:
            on_created(note_key)
        except Exception:
            return {"ok": True, "status": "created", "verification_pending": True,
                    "acknowledgement_failed": True,
                    "message": "Note created but its acknowledgement could not be saved. Retain this key and reconcile manually."}, str(note_key), None
    # The POST has succeeded: retain its key even if verification is unavailable.
    # Callers can journal the known creation without inventing an undo baseline.
    try:
        _data, version = zotero_write.get_item(
            c["transport"].base_url, c["lib_path"], note_key, api_key=c["api_key"],
            **({"server_id": c["server_id"]} if c.get("server_id") else {}))
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroVersionConflict,
            zotero_write.ZoteroError):
        return {"ok": True, "status": "created", "verification_pending": True,
                "message": "The note was created, but verification is pending. Refresh before retrying; automatic undo is unavailable."}, note_key, None
    from core.utils import log_vault_write
    log_vault_write("notesync_zotero_note_create", note_key,
                    f"under {c['parent_key']}, {len(c['html'])} chars")
    baseline = getattr(note_key, 'creation_version', None)
    if baseline is None or version != baseline or _data != getattr(note_key, 'creation_data', None):
        return {"ok": True, "status": "created", "verification_pending": True,
                "message": "Note created; unchanged creation evidence is unavailable. Its key is retained; review manually before undo."}, str(note_key), None
    c['creation_data'] = _data
    return {"ok": True, "status": "applied"}, note_key, version


def journal_create(*, prepared, citation_key: str, note_key: str, version: int,
                   manifest) -> dict:
    """Append the ``zotero_note_create`` op AFTER the POST (there is no key to
    record before it). Caller holds the lock and saves."""
    c = prepared["commit"]
    op = {
        "op_id": journal.new_op_id(manifest), "kind": OP_CREATE,
        "ts": journal.now_iso(), "citation_key": citation_key,
        "item_key": note_key, "parent_item": c["parent_key"],
        "lib_path": c["lib_path"], "base_url": c["transport"].base_url,
        "transport": c["transport"].kind, "html": c["html"],
        "version_after": version, "server_id": c.get("server_id"), "state": "applied",
        "creation_data": c.get('creation_data'),
    }
    manifest["ops"].append(op)
    return op


# --- reverts ---------------------------------------------------------------

def _revert_target(op):
    api_key = zotero_write.api_key_from_env() if op.get("transport") == "web" else None
    if op.get("transport") == "web" and not api_key:
        return None, {"ok": False, "status": "error", "message": (
            f"This note was written via the Zotero web API; set "
            f"{zotero_write.API_KEY_ENV} to revert it.")}
    return (op.get("base_url"), op.get("lib_path") or "users/0",
            op.get("item_key"), api_key), None


def revert_prepare(cfg: dict, op: dict):
    """Read side of a note-op revert (no lock): ``{"early"}`` or ``{"commit"}``."""
    tgt, early = _revert_target(op)
    if early is not None:
        return {"early": early}
    base, lib_path, key, api_key = tgt
    if not key or not base:
        return {"early": {"ok": False, "status": "invalid", "message": "Op missing item_key."}}
    from tagging.local_api import restore_kwargs
    try:
        data, version = zotero_write.get_item(base, lib_path, key, api_key=api_key, **restore_kwargs(op))
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "error", "message": "Zotero is not reachable."}}
    except zotero_write.ZoteroError as exc:
        return {"early": {"ok": False, "status": "error", "message": str(exc)}}
    current = str(data.get("note") or "")
    expected = op.get("after_html") if op.get("kind") == OP_WRITE else op.get("html")
    if current != (expected or ""):
        if op.get("state") == "outcome_unknown":
            return {"early": {"ok": True, "status": "skipped", "version": version,
                              "message": f"Zotero does not hold the written text (version {version}); "
                                         "the uncertain write may never have landed — not reverting."}}
        return {"early": {"ok": True, "status": "skipped",
                          "message": "Note changed in Zotero after the write — not reverting."}}
    if op.get("kind") == OP_CREATE and data.get("deleted"):
        return {"early": {"ok": True, "status": "skipped", "message": "Already in Zotero's trash."}}
    if op.get("kind") == OP_CREATE and (
        data.get("itemType") != "note"
        or not op.get("parent_item")
        or data.get("parentItem") != op["parent_item"]
        or not op.get("version_after")
        or int(version) != int(op["version_after"])
        or data.get("tags") or data.get("relations")
        or not op.get('creation_data') or data != op['creation_data']
    ):
        return {"early": {"ok": True, "status": "skipped",
                          "message": "Created note changed or has no verified creation baseline — review manually before trashing."}}
    return {"commit": {"base": base, "lib_path": lib_path, "key": key,
                       "api_key": api_key, "version": version}}


NOT_WRITTEN_STATUSES = ("conflict", "unsupported", "not_sent")


def observe_unknown(op: dict) -> dict:
    """One lock-free live read of a note op whose outcome is unknown.

    ``{"status": "written", "version"}`` when the note holds exactly the op's
    ``after_html``; ``{"status": "different", "version"}`` when it holds
    anything else; ``{"status": "unreadable", "message"}`` when Zotero could
    not be read. Writes nothing (2026-09-17 review, B2, decision D2)."""
    tgt, early = _revert_target(op)
    if early is not None:
        return {"status": "unreadable", "message": early["message"]}
    base, lib_path, key, api_key = tgt
    if not key or not base:
        return {"status": "unreadable", "message": "The journalled op names no note."}
    from tagging.local_api import restore_kwargs
    try:
        data, version = zotero_write.get_item(base, lib_path, key, api_key=api_key, **restore_kwargs(op))
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroError) as exc:
        return {"status": "unreadable", "message": str(exc)}
    written = (data.get("itemType") == "note" and not data.get("deleted")
               and str(data.get("note") or "") == (op.get("after_html") or "")
               and (not op.get("parent_item") or data.get("parentItem") == op["parent_item"]))
    return {"status": "written" if written else "different", "version": version}


def revert_commit(prepared, op: dict) -> dict:
    """Write side of a note-op revert: ONE guarded PATCH."""
    from core.utils import log_vault_write
    c = prepared["commit"]
    try:
        if op.get("kind") == OP_WRITE:
            zotero_write.set_item_note(c["base"], c["lib_path"], c["key"],
                                       op.get("before_html") or "", c["version"],
                                       api_key=c["api_key"])
            log_vault_write("notesync_restore_zotero_note", c["key"], "body restored")
        else:
            zotero_write.set_item_deleted(c["base"], c["lib_path"], c["key"], True,
                                          c["version"], api_key=c["api_key"])
            log_vault_write("notesync_restore_zotero_note", c["key"], "created note trashed")
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroVersionConflict,
            zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "error", "message": str(exc)}
    op["state"] = "reverted"
    return {"ok": True, "status": "reverted", "message": "Reverted in Zotero."}


def revert(cfg: dict, op: dict) -> dict:
    """prepare + commit, for callers holding no lock."""
    prepared = revert_prepare(cfg, op)
    if "early" in prepared:
        return prepared["early"]
    return revert_commit(prepared, op)
