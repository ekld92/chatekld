"""Library Audit HTTP endpoints.

Every route requires the ``X-Requested-With: ChatEKLD`` header check via
:func:`api.security.origin_is_local`. No endpoint triggers a scan as a
side effect of a GET — only ``POST /api/audit/scan`` does.
"""

from __future__ import annotations

import logging
import json
import sqlite3
import os
import re
import stat
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, jsonify, request, send_file

from api.routes.open import run_open
from api.security import sanitise_error_msg
from api.validators import (
    coerce_bool,
    coerce_int_in_range,
    coerce_non_empty_string,
    coerce_string_max_len,
)
from core.config import load_config, save_config
from core.paths import resolve_under_root

from audit.config import (
    DEFAULT_ANNOTATIONS_READ_THRESHOLD,
    DEFAULT_NEAR_DUP_RATIO,
    DEFAULT_ATTACHMENTS_SUBDIR,
    DEFAULT_BIBLIO_ARTICLES_SUBDIR,
    DEFAULT_BIBLIO_SKIP_PREFIX,
    DEFAULT_MASTER_BIB_PATH,
    DEFAULT_ZOTERO_NOTES_SUBDIR,
    DEFAULT_ZOTERO_SQLITE,
    DEFAULT_ZOTERO_STORAGE,
    AuditConfigError,
    load_settings,
    mapping_file_path,
)
from audit.engine import bridge as eng_bridge
from audit.engine.reports import (
    dead_curation as r_dead_curation,
    multi_pdf as r_multi_pdf,
    finder_tag_drift as r_finder_tag_drift,
    note_tag_drift as r_note_tag_drift,
    read_unzoterod as r_read_unzoterod,
    tag_harmony as r_tag_harmony,
    tag_hygiene as r_tag_hygiene,
    unread_unzoterod as r_unread_unzoterod,
    zotero_no_pdf as r_zotero_no_pdf,
    zotero_unread as r_zotero_unread,
)
from audit.manager import audit_manager
from audit import search as audit_search
from audit import serialize

logger = logging.getLogger(__name__)

audit_bp = Blueprint("audit", __name__)

# Per-key validation rules for the audit settings.  Subpaths are POSIX
# relative paths inside the vault; absolute paths point at the Zotero
# data dir.  A short path string is preferred so a user pasting a long
# vault path does not accidentally land in the wrong field.
_PATH_MAX_LEN = 1024
_PREFIX_RE = re.compile(r"[A-Za-z0-9_.\-]{0,32}")
_THRESHOLD_MIN, _THRESHOLD_MAX = 0, 10_000

#: The reports whose tab shows the PDF observation provenance
#: (``pdf_reconciliation``) — the ONE definition; ``static/js/audit.js``'s
#: ``_PDF_REPORTS`` is pinned against it (tests/test_cli_proof_batch_c.py).
PDF_PROJECTION_REPORTS = frozenset({
    "duplicates", "near_duplicates", "multi_pdf", "read_unzoterod",
    "unread_unzoterod", "zotero_no_pdf", "not_in_zotero", "pdf_review",
    "ambiguous_pdfs", "skipped_pdfs", "dead_curation", "zotero_attachments",
})

_REPORT_NAMES = frozenset({
    "note_tag_drift",
    "finder_tag_drift",
    "unread_unzoterod",
    "zotero_unread",
    "read_unzoterod",
    "zotero_no_pdf",
    "duplicates",
    "near_duplicates",
    "ambiguous_pdfs",
    "skipped_pdfs",
    "tag_hygiene",
    "tag_harmony",
    "orphan_notes",
    "orphan_zotero_items",
    "zotero_attachments",
    # 2026-09-06 harmonization reports (read-only; plan
    # docs/plan_2026-09-06_library_harmonization.md Phase 1).
    "multi_pdf",
    "not_in_zotero",
    # Phase 2 Option A (2026-09-10): the per-file identification workspace is
    # its own tab. Same enriched payload as not_in_zotero (curated queue +
    # review-store rows); the client mounts pendingReview.js here only.
    "pdf_review",
    "dead_curation",
})

#: Item types offered for bulk "needs no PDF" dismissal, as a comma string
#: (the `audit_no_pdf_types` config key's default). Measured against the live
#: library's 1,502 PDF-less entries; `book`/`bookSection` are deliberately
#: absent — see `_no_pdf_types`.
DEFAULT_NO_PDF_TYPES = (
    "webpage,blogPost,videoRecording,computerProgram,forumPost,presentation,"
    "statute,dataset,newspaperArticle,magazineArticle,dictionaryEntry"
)

#: Bulk-dismissal request cap. The whole no-PDF list is ~1,500 rows, so this is
#: a request-size backstop, not a product limit.
_PDF_DECISION_MAX_KEYS = 5_000

_AUDIT_KEYS = (
    "audit_attachments_subdir",
    "audit_biblio_articles_subdir",
    "audit_zotero_notes_subdir",
    "audit_master_bib_path",
    "audit_zotero_sqlite",
    "audit_zotero_storage",
    "audit_biblio_skip_prefix",
    "audit_annotations_read_threshold",
    "audit_drift_include_parent_tags",
    "audit_near_dup_ratio",
    "audit_no_pdf_types",
)

_AUDIT_DEFAULTS: dict[str, Any] = {
    "audit_attachments_subdir": DEFAULT_ATTACHMENTS_SUBDIR,
    "audit_biblio_articles_subdir": DEFAULT_BIBLIO_ARTICLES_SUBDIR,
    "audit_zotero_notes_subdir": DEFAULT_ZOTERO_NOTES_SUBDIR,
    "audit_master_bib_path": DEFAULT_MASTER_BIB_PATH,
    "audit_zotero_sqlite": DEFAULT_ZOTERO_SQLITE,
    "audit_zotero_storage": DEFAULT_ZOTERO_STORAGE,
    "audit_biblio_skip_prefix": DEFAULT_BIBLIO_SKIP_PREFIX,
    "audit_annotations_read_threshold": DEFAULT_ANNOTATIONS_READ_THRESHOLD,
    "audit_drift_include_parent_tags": False,
    "audit_near_dup_ratio": DEFAULT_NEAR_DUP_RATIO,
    "audit_no_pdf_types": DEFAULT_NO_PDF_TYPES,
}


def _normalise_rel_path(raw: Any) -> str | None:
    """Coerce *raw* to a vault-relative POSIX path; reject traversal.

    B3 (2026-07-13): a denylist, not an ASCII allowlist. The old
    ``[A-Za-z0-9 _.\\-/]`` regex 400'd any vault path with an accent, apostrophe,
    parenthesis, ``&`` etc. — so a parked ambiguous PDF such as
    ``biblio/étude_dépression.pdf`` scanned fine but could never be curated (the
    picker POSTed that same rel back and got "Invalid pdf path"). Reject
    NUL/newline/carriage-return/backslash, absolute paths, and any ``..``
    segment; length is already capped by coerce_string_max_len.
    """
    s = coerce_string_max_len(raw, _PATH_MAX_LEN)
    if s is None or not s:
        return None
    if any(c in s for c in ("\x00", "\n", "\r", "\\")):
        return None
    # Reject absolute paths — the path is interpreted under the vault root.
    if s.startswith("/"):
        return None
    parts = Path(s).parts
    if any(part == ".." for part in parts):
        return None
    return s


def _normalise_abs_path(raw: Any) -> str | None:
    """Coerce *raw* to a non-empty *absolute* path string.

    Zotero sqlite/storage settings are documented as absolute. Accepting
    a relative path here would silently anchor it to the server's CWD at
    use-time, and that anchored path could then be added to the allowed
    roots that bound ``/api/audit/reveal`` — widening reveal scope to
    arbitrary files under the launcher's working directory.

    Tildes are expanded before the absolute-path check so ``~/Zotero/...``
    is accepted as long as ``$HOME`` itself is absolute (it always is on
    macOS / Linux / Windows).
    """
    s = coerce_string_max_len(raw, _PATH_MAX_LEN)
    if s is None or not s:
        return None
    if any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
    try:
        expanded = Path(s).expanduser()
    except (OSError, RuntimeError):
        return None
    if not expanded.is_absolute():
        return None
    return s


def _normalise_item_types(raw: Any) -> str | None:
    """Coerce a comma-separated Zotero item-type list, or None if malformed.

    Item types are Zotero's own identifiers — bare ASCII camelCase, no spaces
    — so anything else is a typo, and a typo that silently persists would make
    the bulk-dismiss offer quietly stop appearing for a whole class. An EMPTY
    string is valid and means "offer no bulk dismissals".
    """
    if not isinstance(raw, str) or len(raw) > 2048:
        return None
    parts = [p.strip() for p in raw.split(",")]
    parts = [p for p in parts if p]
    for part in parts:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", part):
            return None
    # Dedupe, preserving the user's order.
    seen: list[str] = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    return ",".join(seen)


def _normalise_prefix(raw: Any) -> str | None:
    """Coerce the biblio skip-prefix: 0-32 chars of ``[A-Za-z0-9_.-]`` (else None).

    An empty string is valid (no skip prefix); the regex allows zero-length.
    """
    s = coerce_string_max_len(raw, 32)
    if s is None:
        return None
    return s if _PREFIX_RE.fullmatch(s) else None


def _normalise_threshold(raw: Any) -> int | None:
    """Coerce the annotations "read" threshold to an int in [0, 10000], else None."""
    return coerce_int_in_range(raw, _THRESHOLD_MIN, _THRESHOLD_MAX)


def _audit_config_view(cfg: dict | None = None) -> dict[str, Any]:
    """Project the persisted config into the audit-settings response shape.

    Falls back to ``_AUDIT_DEFAULTS`` for any unset audit key, and adds the
    derived ``mapping_file`` path and the shared ``obsidian_vault_path`` so the
    Library Audit UI can render the full settings panel from one payload.
    """
    if cfg is None:
        cfg = load_config()
    out: dict[str, Any] = {}
    for key in _AUDIT_KEYS:
        out[key] = cfg.get(key, _AUDIT_DEFAULTS[key])
    out["mapping_file"] = str(mapping_file_path())
    out["obsidian_vault_path"] = cfg.get("obsidian_vault_path", "")
    return out


@audit_bp.route("/api/audit/config")
def api_audit_get_config():
    """Return the current audit settings (with defaults filled in)."""
    return jsonify(_audit_config_view())


@audit_bp.route("/api/audit/config", methods=["POST"])
def api_audit_save_config():
    """Validate and persist the audit settings (the only audit write endpoint).

    This is the dedicated, path-traversal-aware entry point for the ``audit_*``
    keys — the generic ``/api/config`` strips them precisely so they can only be
    set here. Each key is run through its specific normaliser (relative subpaths
    reject traversal; the Zotero sqlite/storage paths must be absolute so they
    can't anchor to the CWD and widen ``reveal`` scope; prefix/threshold are
    shape/range checked). Any single invalid value 400s the whole request rather
    than being silently dropped; only the validated subset is then saved.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400

    update: dict[str, Any] = {}
    if "audit_attachments_subdir" in data:
        v = _normalise_rel_path(data["audit_attachments_subdir"])
        if v is None:
            return jsonify({"error": "Invalid attachments subdir"}), 400
        update["audit_attachments_subdir"] = v
    if "audit_biblio_articles_subdir" in data:
        v = _normalise_rel_path(data["audit_biblio_articles_subdir"])
        if v is None:
            return jsonify({"error": "Invalid biblio articles subdir"}), 400
        update["audit_biblio_articles_subdir"] = v
    if "audit_zotero_notes_subdir" in data:
        v = _normalise_rel_path(data["audit_zotero_notes_subdir"])
        if v is None:
            return jsonify({"error": "Invalid Zotero notes subdir"}), 400
        update["audit_zotero_notes_subdir"] = v
    if "audit_master_bib_path" in data:
        v = _normalise_rel_path(data["audit_master_bib_path"])
        if v is None:
            return jsonify({"error": "Invalid master bib path"}), 400
        update["audit_master_bib_path"] = v
    if "audit_zotero_sqlite" in data:
        v = _normalise_abs_path(data["audit_zotero_sqlite"])
        if v is None:
            return jsonify({"error": "Invalid Zotero sqlite path"}), 400
        update["audit_zotero_sqlite"] = v
    if "audit_zotero_storage" in data:
        v = _normalise_abs_path(data["audit_zotero_storage"])
        if v is None:
            return jsonify({"error": "Invalid Zotero storage path"}), 400
        update["audit_zotero_storage"] = v
    if "audit_biblio_skip_prefix" in data:
        v = _normalise_prefix(data["audit_biblio_skip_prefix"])
        if v is None:
            return jsonify({"error": "Invalid biblio skip prefix"}), 400
        update["audit_biblio_skip_prefix"] = v
    if "audit_annotations_read_threshold" in data:
        v = _normalise_threshold(data["audit_annotations_read_threshold"])
        if v is None:
            return jsonify({"error": "Invalid annotations read threshold"}), 400
        update["audit_annotations_read_threshold"] = v
    if "audit_drift_include_parent_tags" in data:
        v = coerce_bool(data["audit_drift_include_parent_tags"])
        if v is None:
            return jsonify({"error": "Invalid drift parent-tags flag"}), 400
        update["audit_drift_include_parent_tags"] = v
    if "audit_near_dup_ratio" in data:
        v = coerce_int_in_range(data["audit_near_dup_ratio"], 60, 100)
        if v is None:
            return jsonify({"error": "Invalid near-duplicate similarity cutoff"}), 400
        update["audit_near_dup_ratio"] = v

    if "audit_no_pdf_types" in data:
        v = _normalise_item_types(data["audit_no_pdf_types"])
        if v is None:
            return jsonify({"error": "Invalid item-type list"}), 400
        update["audit_no_pdf_types"] = v

    if update:
        before = load_config()
        save_config(update)
        if any(before.get(key) != value for key, value in update.items()):
            audit_manager.reset_to_idle()
    return jsonify({"ok": True, "config": _audit_config_view()})


@audit_bp.route("/api/audit/status")
def api_audit_status():
    """Return the audit manager's lifecycle/progress payload (read-only poll)."""
    return jsonify(audit_manager.get_status_payload())


@audit_bp.route("/api/audit/dup-decision", methods=["POST"])
def api_audit_dup_decision():
    """Record / undo a "not a duplicate" decision (plan 2026-08-05/5).

    Body: ``{pair_id, dismiss: true}`` or ``{pair_id, clear: true}`` —
    ``clear`` is the undo and is checked FIRST (the ``mapping`` route's
    precedent: an undo body must never be read as a re-add). Writes only
    ``BASE_DIR/audit/dup_decisions.json`` (a decision sidecar, not the vault —
    same class as ``mapping``, hence no ``confirm`` gate at the API level; the
    UI still runs its confirmInline ceremony). Like every mapping edit, the
    cached report reflects the decision on the NEXT scan; the client marks the
    row in place meanwhile.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    from audit.engine import dup_decisions as eng_dup_decisions
    if "pair_ids" in data:
        return _dup_decision_many(data, eng_dup_decisions)
    pair_id = coerce_string_max_len(data.get("pair_id"), 4096)
    if not pair_id or any(c in pair_id for c in ("\x00", "\n", "\r")):
        return jsonify({"error": "Invalid pair_id"}), 400
    try:
        if coerce_bool(data.get("clear")) is True:
            changed = eng_dup_decisions.clear_dismissed(pair_id)
            return jsonify({"ok": True, "cleared": True, "changed": changed})
        if coerce_bool(data.get("dismiss")) is True:
            changed = eng_dup_decisions.add_dismissed(pair_id)
            return jsonify({"ok": True, "dismissed": True, "changed": changed})
    except (OSError, ValueError) as exc:
        # ValueError = the decisions file exists but is corrupt; the RMW
        # refuses rather than restarting the (unregenerable) list from empty.
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({"error": "Body must set dismiss:true or clear:true"}), 400


#: Bound on the list form of ``dup-decision``. Generous against the live
#: library (tens of candidate groups) while keeping one request's validation
#: and write small; the store itself refuses past its own backstop.
_DUP_DECISION_MAX_IDS = 2000


def _dup_decision_many(data, eng_dup_decisions):
    """``{pair_ids: [...], dismiss|clear: true}`` — the list form (2026-09-17,
    annual cleanup T1). The whole list is validated BEFORE any write and lands
    in one atomic write, the ``pdf-decision`` posture: a partial bulk write
    would leave no way to tell which half landed. ``clear`` is checked first,
    as in the single form."""
    raw = data.get("pair_ids")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "pair_ids must be a non-empty list"}), 400
    if len(raw) > _DUP_DECISION_MAX_IDS:
        return jsonify({"error": f"At most {_DUP_DECISION_MAX_IDS} pair_ids per request."}), 400
    ids = []
    for item in raw:
        pid = coerce_string_max_len(item, 4096)
        if not pid or any(c in pid for c in ("\x00", "\n", "\r")):
            return jsonify({"error": "Invalid pair_id in pair_ids"}), 400
        ids.append(pid)
    try:
        if coerce_bool(data.get("clear")) is True:
            changed = eng_dup_decisions.clear_dismissed_many(ids)
            return jsonify({"ok": True, "cleared": True, "changed": changed,
                            "count": len(set(ids))})
        if coerce_bool(data.get("dismiss")) is True:
            changed = eng_dup_decisions.add_dismissed_many(ids)
            return jsonify({"ok": True, "dismissed": True, "changed": changed,
                            "count": len(set(ids))})
    except (OSError, ValueError) as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({"error": "Body must set dismiss:true or clear:true"}), 400


@audit_bp.route("/api/audit/pdf-decision", methods=["POST"])
def api_audit_pdf_decision():
    """Record / undo "this entry needs no local PDF" (Batch B, 2026-08-26).

    Body: ``{citation_keys: [...], dismiss: true}``,
    ``{citation_keys: [...], clear: true}``, or ``{clear_all: true}`` —
    the undo verbs are checked FIRST, the ``mapping``/``dup-decision``
    precedent (an undo body carrying the same keys must never be read as a
    re-add).

    This is the MIRROR of ``mapping.json``'s ``no_match`` list, which says "this
    PDF is deliberately not in the bibliography". Nothing said the same thing in
    the other direction, so *Bib Entries Without PDFs* could only grow — 1,502
    rows on the maintainer's library, 293 of them item types that never have a
    PDF at all.

    A LIST, not one key, so the bulk "dismiss every webpage" action is one
    atomic write: a partial bulk write would leave the user unable to tell
    which half landed. Writes only ``BASE_DIR/audit/pdf_wanted.json`` — a
    decision sidecar, not the vault, hence no ``confirm`` gate at the API level
    (the UI runs its confirmInline ceremony). Unlike a mapping edit this takes
    effect on the next report LOAD, not the next scan.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    from audit.engine import pdf_wanted as _pw

    # `clear_all` is the report's "show them again": the report ships the COUNT
    # of what it hid, never the keys (they are not rows), so the client cannot
    # name them. An explicit all-or-nothing verb is honest about that; a
    # partial undo the client cannot scope would be worse.
    if coerce_bool(data.get("clear_all")) is True:
        try:
            changed = _pw.clear_dismissed(_pw.load_dismissed())
        except (OSError, ValueError) as exc:
            return jsonify({"error": sanitise_error_msg(exc)}), 500
        return jsonify({"ok": True, "cleared": True, "changed": changed})

    raw = data.get("citation_keys")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "citation_keys must be a non-empty list"}), 400
    if len(raw) > _PDF_DECISION_MAX_KEYS:
        return jsonify({
            "error": f"At most {_PDF_DECISION_MAX_KEYS} keys per request."}), 400
    keys = []
    for item in raw:
        key = coerce_string_max_len(item, 512)
        if not key or any(c in key for c in ("\x00", "\n", "\r")):
            return jsonify({"error": "Invalid citation key"}), 400
        keys.append(key)

    from audit.engine import pdf_wanted

    try:
        if coerce_bool(data.get("clear")) is True:
            changed = pdf_wanted.clear_dismissed(keys)
            return jsonify({"ok": True, "cleared": True, "changed": changed})
        if coerce_bool(data.get("dismiss")) is True:
            changed = pdf_wanted.add_dismissed(keys)
            return jsonify({"ok": True, "dismissed": True, "changed": changed})
    except (OSError, ValueError) as exc:
        # ValueError = the decisions file exists but is corrupt; the RMW
        # refuses rather than restarting the (unregenerable) list from empty.
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({"error": "Body must set dismiss:true or clear:true"}), 400


def _build_pdf_text_provider():
    """Build the near-duplicate tiers' page-1 text provider (plan 2026-08-05/4).

    App-side injection over ``tagging/text_source.py`` — cache-first via the
    indexer's PDF text cache, fresh extraction bounded to the FIRST page with
    the ``.notext`` negative-cache marker, so the scan never pays a whole-file
    extraction. Returns ``Callable[[Path], str | None]`` (None on no/failed
    text) or ``None`` when the vault path is unavailable — the engine then
    degrades those tiers to hash-only. The vendored ``audit/`` tree receives
    the callable and never imports ``tagging``/``rag`` itself (the
    ``search.build_corpus`` seam pattern).
    """
    cfg = load_config()
    vault_root_str = str(cfg.get("obsidian_vault_path") or "").strip()
    if not vault_root_str:
        return None

    def provider(pdf_path):
        try:
            from tagging.text_source import read_pdf_text

            text, _truncated = read_pdf_text(
                Path(pdf_path), Path(vault_root_str), 20_000,
                fresh_max_pages=1)
            return text or None
        except Exception:  # noqa: BLE001 — text is advisory, never fatal
            return None

    return provider


@audit_bp.route("/api/audit/scan", methods=["POST"])
def api_audit_scan():
    """Start a background reconciliation scan — the ONLY scan trigger.

    No other code path may call ``audit_manager.start_scan()`` (pinned by
    ``TestFlaskAppBootDoesNotScan``), so the scan is strictly manual. The body's
    ``count_annotations`` / ``include_duplicates`` / ``include_near_duplicates``
    are coerced to bool (defaulting
    to True when absent/invalid); a non-object body is treated as ``{}``. Returns
    409 when a scan is already running, 400 when the manager rejects the config,
    else ok. The scan itself is read-only against all external stores.
    """
    # An empty body is valid (use defaults). A non-object payload (list,
    # number, string, null) is not — coerce to {} so subsequent .get()
    # calls cannot raise AttributeError and bubble out as a 500.
    raw = request.get_json(silent=True)
    data = raw if isinstance(raw, dict) else {}
    count_annotations = coerce_bool(data.get("count_annotations"))
    include_duplicates = coerce_bool(data.get("include_duplicates"))
    include_near = coerce_bool(data.get("include_near_duplicates"))
    from api.routes.pending import workspace as refresh_reconciliation
    started, msg = audit_manager.start_scan(
        count_annotations=count_annotations if count_annotations is not None else True,
        include_duplicates=(
            include_duplicates if include_duplicates is not None else True
        ),
        include_near_duplicates=include_near if include_near is not None else True,
        pdf_text_provider=_build_pdf_text_provider(),
        reconciliation_refresh=lambda settings: refresh_reconciliation(settings, force=True),
    )
    if not started:
        # 409 Conflict when already scanning, 400 when config rejected.
        if audit_manager.is_scanning():
            return jsonify({"error": msg, "started": False}), 409
        return jsonify({"error": msg, "started": False}), 400
    return jsonify({"ok": True, "started": True, "message": msg})


@audit_bp.route("/api/audit/cancel", methods=["POST"])
def api_audit_cancel():
    """Request cancellation of an in-flight scan (no-op if none is running)."""
    cancelled = audit_manager.request_cancel()
    return jsonify({"ok": True, "cancelled": cancelled})


# PERF (2026-09-02): the 6 000-row inventory serialises once per scan, not
# once per request.
# Before: every GET /api/audit/inventory re-flattened every Record. Measured
#   on a synthetic 6 000-record inventory (the maintainer's library is
#   ~6 150): 75.3 ms of serialisation plus 10.9 ms of json.dumps for a
#   3.71 MB payload, paid again on every tab switch that refetches.
# Change: a memo keyed on the IDENTITY of the (Inventory, Settings) objects
#   the manager hands out — the search-corpus discipline — dropped by the
#   same result listener, so a superseded Inventory is never pinned. Same
#   inventory: 75.3 ms -> 0.0 ms on every request after the first.
# Safe because:
#   A report is a view of ONE cached scan, so the rows cannot change while
#   the Inventory object does not; a new scan replaces both objects and the
#   `is` check misses. The cache holds strong references to the very
#   objects it was built from, so a recycled id() cannot fool it. The rows
#   list is handed to jsonify read-only and never mutated.
# Preserves: "a report is a view of ONE cached scan" — and the memo is
#   released by the SAME listener that releases the search corpus, so the
#   old Inventory is not held past its replacement.
# Lock/thread contract: one module lock held across the dict read/write
#   only, never across the serialisation itself; runs on the request thread
#   as before.
# Pinned by: test_audit.py::TestInventoryRowsMemo (red before the change:
#   "2 != 1" serialisations for two identical GETs).
_INVENTORY_ROWS_CACHE: tuple | None = None
_INVENTORY_ROWS_LOCK = threading.Lock()


def _inventory_rows(inv, settings) -> list:
    """``serialize.inventory_records`` memoised on the scan's identity."""
    global _INVENTORY_ROWS_CACHE
    with _INVENTORY_ROWS_LOCK:
        cached = _INVENTORY_ROWS_CACHE
    if cached is not None and cached[0] is inv and cached[1] is settings:
        return cached[2]
    rows = serialize.inventory_records(inv, settings)
    # Display text only: bib fields are LaTeX source and the board renders
    # them verbatim (`Comment \\'evolue`, `R{\\'e}gis`). The Record itself is
    # never touched — its fingerprints and joins key on the raw value.
    from widget.textclean import clean_display
    for row in rows:
        for field in ("title", "first_author"):
            if isinstance(row.get(field), str) and row[field]:
                row[field] = clean_display(row[field]) or row[field]
    with _INVENTORY_ROWS_LOCK:
        _INVENTORY_ROWS_CACHE = (inv, settings, rows)
    return rows


def _clear_inventory_rows_cache() -> None:
    """Drop the serialised rows (and their Inventory reference)."""
    global _INVENTORY_ROWS_CACHE
    with _INVENTORY_ROWS_LOCK:
        _INVENTORY_ROWS_CACHE = None


audit_manager.register_result_listener(_clear_inventory_rows_cache)


def _write_paper_index() -> None:
    """Result listener: persist the paper-label index for RAG after a scan.

    ``rag/paper_index.py`` turns the scan's PDF↔bib↔note join into labels for
    vault chat and citations for the deck/article generators; ``rag`` may not
    import the audit manager, so the write is driven from here. A clear/reset
    fires with ``(None, None)`` and is a no-op — the last index keeps serving,
    exactly like the widget snapshot. Cheap (measured 2026-09-23: a 0.135 s name
    walk + a 0.41 s build + a 0.015 s write, 3.36 MB), so it runs inline. Any failure only logs: labels are an
    enrichment, and a broken listener must never look like a broken scan.
    """
    try:
        inv, settings = audit_manager.get_inventory()
        if inv is None or settings is None:
            return
        from api.routes._audit_tagwrite import ambiguous_pdf_basenames
        from rag import paper_index

        data = paper_index.build(
            inv, settings,
            ambiguous_basenames=ambiguous_pdf_basenames(settings.vault_root),
        )
        paper_index.write(data)
    except Exception:  # noqa: BLE001 — see docstring
        logger.exception("Paper-label index write failed; labels unchanged.")


audit_manager.register_result_listener(_write_paper_index)


@audit_bp.route("/api/audit/inventory")
def api_audit_inventory():
    """Return the cached per-citation-key inventory from the last scan.

    404 with a "Run a scan first" message when no completed scan is cached
    (also the post-reset empty state) — there is no implicit scan on read.
    """
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        _clear_inventory_rows_cache()
        return jsonify({"error": "No scan results. Run a scan first."}), 404
    return jsonify({
        "summary": serialize.inventory_summary(inv),
        "pdf_reconciliation": _pdf_projection_summary(settings),
        "records": _inventory_rows(inv, settings),
    })


@audit_bp.route("/api/audit/summary")
def api_audit_summary():
    """Just the headline counters — the dashboard's own feed (2026-08-13).

    Same payload as ``/api/audit/inventory``'s ``summary`` key, without the
    thousands-of-rows ``records`` array. It exists because the summary tiles
    used to be a by-product of the Inventory TAB: ``_lastSummary`` was set only
    on that branch, so the tiles stayed blank until the user happened to load a
    table they may not want, and the first-scan milestone had no count to show.
    A dashboard that reports on the whole library should not require fetching
    the whole library.
    """
    inv, settings = audit_manager.get_inventory()
    if inv is None:
        return jsonify({"error": "No scan results. Run a scan first."}), 404
    # `scanned_at` (additive, 2026-08-13): the served snapshot's BUILD time,
    # read off the Inventory itself (2026-08-18) — one atomic read, so mid-scan
    # the previous snapshot keeps its true stamp (get_scan_times()' _finished
    # is zeroed by start_scan) and an errored rescan cannot re-stamp a stale
    # snapshot as fresh (_finalise stamps _finished_at even with inv=None).
    # Never get_status_payload(), which drains the one-shot status feed the UI
    # polls. The vault-maintenance GUI used to fetch the FULL tagging
    # inventory — thousands of rows — solely to read this one float.
    _built = getattr(inv, "built_at", 0.0)
    return jsonify({
        "summary": serialize.inventory_summary(inv),
        "pdf_reconciliation": _pdf_projection_summary(settings),
        "scanned_at": _built if isinstance(_built, (int, float)) else 0.0,
        # Additive (2026-09-23): when the paper-label index for this vault was
        # built, or null when there is none. Read through the stat-cached
        # loader, so this is one stat per poll. Labels reflect the LAST scan.
        "paper_index_built_at": _paper_index_built_at(settings),
    })


def _paper_index_built_at(settings: Any) -> Optional[str]:
    """ISO build stamp of the vault's paper-label index, or ``None``."""
    try:
        from rag import paper_index

        idx = paper_index.load(getattr(settings, "vault_root", None))
    except Exception:  # noqa: BLE001 — a missing index is not a summary failure
        return None
    built = getattr(idx, "built_at", None) if idx is not None else None
    return built if isinstance(built, str) and built else None


#: Parsed-_tags.md memo, keyed on the file's own (path, size, mtime_ns) so an
#: edit applies on the next call. Tiny (one vault, one file in practice); the
#: bound exists only so a pathological caller cannot grow it without limit.
_SYNONYM_MEMO: dict[tuple, dict[str, str]] = {}
_SYNONYM_MEMO_LOCK = threading.Lock()
_SYNONYM_MEMO_MAX = 8


def _drift_synonyms(settings: Any) -> dict[str, str] | None:
    """Synonym equivalence classes for the drift comparisons (plan 2026-08-05/3).

    Built from the vault's curated ``_tags.md`` (the SAME
    ``vault_thesaurus_tags_path`` file the tagging vocabulary and Tag Hygiene
    use): each SINGLE-tag row's description phrases are FR/EN synonyms of that
    tag, so ``sommeil`` on a note satisfies a Zotero ``sleep`` as a *variant*
    (reason ``synonym``) instead of drift. Multi-tag rows are skipped —
    ``rag/thesaurus.py``'s own rule: they assert co-occurring facets, not
    synonyms. Returns ``{tag_key(surface): canonical tag_key}`` or ``None``
    when the file is unconfigured/missing/unreadable. App-side injection: the
    vendored ``audit/`` tree never resolves vault content itself.
    """
    try:
        from audit.engine.normalize import tag_key
        from rag.thesaurus import parse_tags

        cfg = load_config()
        raw_rel = str(cfg.get("vault_thesaurus_tags_path") or "_tags.md").strip()
        if not raw_rel:
            return None
        rel = resolve_under_root(
            raw_rel, str(settings.vault_root), must_exist=True, must_be_file=True)
        if rel is None:
            return None
        path = settings.vault_root / rel
        # PERF (2026-09-02): the _tags.md read + parse is memoised on the
        # file's own (size, mtime_ns).
        # Before: every call re-read and re-parsed the file. Measured on the
        #   maintainer's 105 620-byte _tags.md (339 synonym entries): 1.47 ms
        #   per call, and a drift report makes three (the vocab-id resolver,
        #   the report build, the request-time options tuple) while the
        #   cross-report search makes one PER KEYSTROKE behind a 220 ms
        #   debounce.
        # Change: a module memo keyed on (path, size, mtime_ns) — the
        #   parse_bib_keys idiom. Same file: 1.47 ms -> 0.002 ms (a stat and
        #   a shallow copy).
        # Safe because:
        #   The key is the file's own identity, so an edit to _tags.md is
        #   picked up on the very next call with no rescan — the property
        #   this function's request-time posture exists for. Every caller
        #   receives a FRESH dict (a shallow copy of str -> str), so no
        #   caller can reach the cached value, and the mapping is rebuilt
        #   from scratch on any miss.
        # Preserves: "An edit to _tags.md still applies without a rescan
        #   because mtime is the key."
        # Lock/thread contract: one module lock held across the dict
        #   read/write only, never across the file read or the parse.
        # Pinned by: test_audit.py::TestDriftSynonymMemo (red before the
        #   change: "2 != 1" file reads for two identical calls).
        try:
            st = path.stat()
            memo_key = (str(path), st.st_size, st.st_mtime_ns)
        except OSError:
            return None
        with _SYNONYM_MEMO_LOCK:
            hit = _SYNONYM_MEMO.get(memo_key)
        if hit is not None:
            return dict(hit) or None
        text = path.read_text(encoding="utf-8")
        mapping: dict[str, str] = {}
        for row in parse_tags(text):
            if len(row.tags) != 1:
                continue
            canonical = tag_key(row.tags[0])
            if not canonical:
                continue
            for surface in [row.tags[0], *row.phrases]:
                k = tag_key(surface)
                if k:
                    mapping.setdefault(k, canonical)
        with _SYNONYM_MEMO_LOCK:
            if len(_SYNONYM_MEMO) >= _SYNONYM_MEMO_MAX:
                _SYNONYM_MEMO.clear()
            _SYNONYM_MEMO[memo_key] = mapping
        return dict(mapping) or None
    except Exception:  # noqa: BLE001 — synonyms are advisory, never fatal
        return None


def _vocab_id_resolver(settings: Any):
    """Callable ``drift surface -> exact _tags.md id | None`` (drift-fix, 2026-08-06).

    The finder-drift fix buttons POST to the tagging writers, whose vocabulary
    gate matches on ``nfc().lower()`` of the LITERAL ``_tags.md`` id — it folds
    neither accents nor separators. The report, meanwhile, compares by the
    OpenRefine fingerprint. Bridging the two identity rules happens here, once,
    server-side: each vocabulary id is indexed by ``canonical_tag_key`` built
    with the SAME synonym map as the drift comparison (the ``apply-tag-drift``
    CRITICAL lesson — a different key recipe makes synonym-remapped tags
    permanently unapplyable), and a drift surface resolves to the id sharing
    its canonical key. So the UI sends ids the writer accepts, and the written
    id shares the surface's CANONICAL key (fingerprint, or fingerprint via the
    synonym map) — which is what closes the row at the next scan instead of
    re-offering it forever (a synonym-bridged pair resurfaces only as an
    informational ``variants`` entry, the note_tag_drift semantics). Returns
    ``None`` when ``_tags.md`` is unavailable (report degrades to read-only).
    """
    try:
        from audit.engine.normalize import canonical_tag_key, clean_tag
        from api.routes.tagging import _read_vocab

        vocab, _rel = _read_vocab(load_config(), settings.vault_root)
        if not vocab:
            return None
        synonyms = _drift_synonyms(settings)
        by_key: dict[str, str] = {}
        for vocab_id in vocab:
            k = canonical_tag_key(vocab_id, synonyms)
            if k:
                by_key.setdefault(k, vocab_id)

        def _resolve(surface: str) -> str | None:
            c = clean_tag(str(surface or ""))
            if not c:
                return None
            return by_key.get(canonical_tag_key(c, synonyms))

        return _resolve
    except Exception:  # noqa: BLE001 — the mapping is advisory, never fatal
        return None


def _drift_include_parent_tags() -> bool:
    """Fresh read of the parent-tags toggle — request-time like the hygiene
    vocab, so flipping the checkbox applies without a rescan (the parent tags
    are already in the cached ``zotero_item.tags``)."""
    return bool(load_config().get("audit_drift_include_parent_tags", False))


def _no_pdf_types() -> tuple[str, ...]:
    """Item types the user considers to never need a local PDF.

    Read fresh at request time, like the drift toggle, so an edit in Audit
    Settings applies on the next report load rather than the next scan. The
    default deliberately EXCLUDES `book`/`bookSection`: 112 of those lack a PDF
    on the maintainer's library and many are ones you would in fact want, so
    offering to dismiss them in bulk by default would be the tool making a
    collection-policy decision on the user's behalf. Adding them is one edit.
    """
    raw = load_config().get("audit_no_pdf_types", DEFAULT_NO_PDF_TYPES)
    if not isinstance(raw, str):
        return tuple(DEFAULT_NO_PDF_TYPES.split(","))
    return tuple(t.strip() for t in raw.split(",") if t.strip())


def _accession_resolution_for(inv):
    """``callable(path) -> dict | None``: the cached accession resolution of a
    PDF, joined by content hash from the MEMO (never a hashing pass) and
    revalidated against the current bib like ``/api/accession/rows``.

    Built here, app-side, and injected into the vendored serializer — the
    ``vocab_id_for`` pattern — because ``audit/`` must never import
    ``accession``. Returns ``None`` for an unmemoised or unresolved PDF.
    """
    from accession import cache as accession_cache
    from accession.candidates import Resolution, _apply_bib_check
    from api.routes.accession import _bib_doi_index
    from audit.core import hash_cache as core_hash_cache
    from audit.core import hashing as core_hashing

    memo = core_hash_cache.load_cache()
    acc = accession_cache.load_cache()
    bib_index = _bib_doi_index(inv)

    def resolution_for(path):
        sha = core_hashing._memo_hit(path, memo)
        if not sha:
            return None
        stored = accession_cache.lookup(acc, sha)
        if stored is None:
            return None
        res = Resolution.from_jsonable(stored)
        if res is None:
            return None
        res = _apply_bib_check(res, bib_index)
        payload = res.to_jsonable()
        payload["queried_at"] = accession_cache.lookup_queried_at(acc, sha)
        return payload

    return resolution_for


@audit_bp.route("/api/audit/reports/<name>")
def api_audit_report(name: str):
    """Build and return one of the reconciliation reports by name.

    The ``<name>`` path segment is allow-listed against ``_REPORT_NAMES`` (404
    otherwise) and dispatched to the matching report builder over the cached
    inventory (404 if no scan is cached). The annotation-based reports
    (``unread_unzoterod`` / ``read_unzoterod``) serve from the scan's precomputed
    ``unmapped_annotations`` to avoid re-opening thousands of PDFs on this
    request thread, with a lazy per-path disk fallback. ``duplicates`` and
    ``near_duplicates`` each 404 if their phase was skipped on the last run.
    """
    if name not in _REPORT_NAMES:
        return jsonify({"error": "Unknown report"}), 404
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        if name in ('pdf_review','not_in_zotero'):
            from api.routes.pending import context, workspace
            try:
                _, settings = context()
                return jsonify(name=name, **workspace(settings))
            except (OSError, ValueError):
                pass
        return jsonify({"error": "No scan results. Run a scan first."}), 404

    if name == "note_tag_drift":
        include_parent = _drift_include_parent_tags()
        note_drift_rows = r_note_tag_drift.find_drift(
            inv,
            include_parent_tags=include_parent,
            synonyms=_drift_synonyms(settings),
        )
        return _report_reply(name, settings, {
            "name": name,
            "rows": serialize.report_note_tag_drift(note_drift_rows),
            "include_parent_tags": include_parent,
        })
    if name == "finder_tag_drift":
        finder_drift_rows = r_finder_tag_drift.find_drift(
            inv, synonyms=_drift_synonyms(settings))
        return _report_reply(name, settings, {"name": name, **serialize.report_finder_tag_drift(
            finder_drift_rows, settings,
            vocab_id_for=_vocab_id_resolver(settings))})
    if name == "tag_harmony":
        # Same two route-injected options as both pairwise drift reports, so
        # "the same tag" means one thing across all three. Read-only: the
        # writes this report feeds go through the tagging subsystem's
        # journalled writers, never a new audit write path.
        harmony_rows = r_tag_harmony.find_harmony(
            inv, synonyms=_drift_synonyms(settings))
        return _report_reply(name, settings, {"name": name, **serialize.report_tag_harmony(
            harmony_rows, settings,
            vocab_id_for=_vocab_id_resolver(settings))})
    if name in {"read_unzoterod", "unread_unzoterod"} and inv.zotero_error:
        return jsonify(error="Zotero observation is unavailable; absence cannot be established. "
                             "Refresh the configured Zotero source and run a new scan.",
                       reason="zotero_observation_unavailable"), 503
    if name == "unread_unzoterod":
        # Serve from the annotation counts the scan precomputed for unmapped
        # PDFs (Inventory.unmapped_annotations) instead of opening thousands
        # of PDFs on this request thread.  find() falls back to a lazy disk
        # read for any path absent from the dict (empty when the scan ran
        # with count_annotations=False or was cancelled mid-phase), so this
        # is an optimisation, never a correctness dependency.
        unread_report = r_unread_unzoterod.find(
            inv, settings, annotations=inv.unmapped_annotations
        )
        return _report_reply(name, settings, {
            "name": name,
            **serialize.report_unread_unzoterod(
                unread_report, settings,
                provenance=getattr(inv.bridge, "provenance", None)),
        })
    if name == "zotero_unread":
        zotero_unread_report = r_zotero_unread.find(inv)
        return _report_reply(name, settings, {
            "name": name,
            **serialize.report_zotero_unread(zotero_unread_report),
        })
    if name == "read_unzoterod":
        # Same precomputed-cache + lazy-fallback contract as unread_unzoterod
        # above; both reports iterate the same unmapped-PDF set.
        read_report = r_read_unzoterod.find(
            inv, settings, annotations=inv.unmapped_annotations
        )
        return _report_reply(name, settings, {
            "name": name,
            **serialize.report_read_unzoterod(
                read_report, settings,
                provenance=getattr(inv.bridge, "provenance", None)),
        })
    if name == "zotero_no_pdf":
        # Read fresh at request time (the _drift_include_parent_tags pattern):
        # a dismissal must take effect on the next report load, never at the
        # next scan.
        from audit.engine import pdf_wanted

        zotero_no_pdf_report = r_zotero_no_pdf.find(
            inv, dismissed=pdf_wanted.load_dismissed())
        return _report_reply(name, settings, {
            "name": name,
            **serialize.report_zotero_no_pdf(
                zotero_no_pdf_report, _no_pdf_types()),
        })
    if name == "tag_hygiene":
        # The controlled vocabulary is optional context (the report still
        # renders frequencies without it). Resolved via tagging's _read_vocab
        # — the SAME vault_thesaurus_tags_path key — so the hygiene flags and
        # the Tagging view can never disagree on which _tags.md wins.
        from api.routes.tagging import _read_vocab
        try:
            vocab, _rel = _read_vocab(load_config(), settings.vault_root)
        except Exception:  # noqa: BLE001 — vocab is advisory here
            vocab = None
        hygiene_report = r_tag_hygiene.find(inv, vocab)
        return _report_reply(name, settings, {
            "name": name,
            **serialize.report_tag_hygiene(hygiene_report),
        })
    if name == "ambiguous_pdfs":
        # Read-only list served straight off the cached bridge result; the UI
        # resolves each ambiguity by POSTing a pick to /api/audit/mapping.
        return _report_reply(name, settings, {"name": name, **serialize.report_ambiguous_pdfs(inv, settings)})
    if name == "skipped_pdfs":
        return _report_reply(name, settings, {"name": name, **serialize.report_skipped_pdfs(inv, settings)})
    if name == "orphan_notes":
        return _report_reply(name, settings, {"name": name, **serialize.orphan_notes(inv, settings)})
    if name == "orphan_zotero_items":
        return _report_reply(name, settings, {"name": name, **serialize.orphan_zotero_items(inv)})
    if name == "zotero_attachments":
        # Read-only view of what Zotero holds files for. The de-link verb the
        # UI puts on these rows posts to /api/zotlink/*, NOT here: the vendored
        # audit tree keeps its read-only posture.
        return _report_reply(name, settings, {"name": name, **serialize.zotero_attachments(inv)})
    if name == "multi_pdf":
        # The Possible Duplicates decision for the same group rides each row,
        # from the hash MEMO only — a report load never hashes the library.
        from audit.core import hash_cache as core_hash_cache
        from audit.core import hashing as core_hashing
        from audit.engine import dup_decisions

        memo = core_hash_cache.load_cache()
        multi_rep = r_multi_pdf.find(
            inv, dismissed=dup_decisions.load_dismissed(),
            sha_for=lambda p: core_hashing._memo_hit(p, memo))
        return _report_reply(name, settings, {"name": name, **serialize.report_multi_pdf(multi_rep, settings)})
    if name == "not_in_zotero" or name == "pdf_review":
        from api.routes.pending import workspace
        return _report_reply(name, settings, {"name": name, **workspace(settings)})
    if name == "dead_curation":
        dead_rep = r_dead_curation.find(inv, settings)
        return _report_reply(name, settings, {"name": name, **serialize.report_dead_curation(dead_rep, settings)})
    if name == "near_duplicates":
        near, _ = audit_manager.get_near_duplicates()
        if near is None:
            return jsonify({"error": "Near-duplicate scan was skipped on the last run."}), 404
        return _report_reply(name, settings, {"name": name, **serialize.report_near_duplicates(near, settings)})
    # duplicates
    duplicates, _ = audit_manager.get_duplicates()
    if duplicates is None:
        return jsonify({"error": "Duplicate scan was skipped on the last run."}), 404
    return _report_reply(name, settings, {"name": name, **serialize.report_duplicates(duplicates, settings)})


# --- cross-report search -------------------------------------------------
#
# Building the search corpus runs every report builder once, and the two
# annotation reports fall back to per-PDF disk reads for any path the scan did
# not precompute — a `count_annotations=False` (or cancelled) scan can make that
# build take MINUTES on a large vault. The corpus is therefore memoised per
# cached scan AND built OFF the request thread (finding, 2026-07-20 audit):
# building it inline under a lock convoyed every debounced keystroke behind the
# one slow build, each keystroke pinning a Waitress worker until it finished —
# potentially freezing the whole app (health/status polls included). Now a
# cache miss kicks ONE background build and the route returns 202 {building},
# which the client shows as "Preparing search index…" and retries.
#
# The cache key is the *identity* of the Inventory + Settings objects the
# manager hands out, deliberately not an id()/timestamp: the cache holds strong
# references to the very objects it was built from, so `is` cannot be fooled by
# a recycled id, and a new scan (which replaces both objects wholesale) fails
# the check and rebuilds. The manager also fires a result listener on
# replace/clear (registered below) so a superseded Inventory is not pinned in
# the memo until the next search (finding 4).
_SEARCH_CACHE: tuple[Any, Any, Any, list] | None = None
_SEARCH_CACHE_LOCK = threading.Lock()
_SEARCH_BUILDING = False          # True while a background build thread runs
_SEARCH_BUILD_ERROR: str | None = None   # last build error, surfaced once
# PERF (2026-09-02): an in-flight corpus build can be told to stop.
# Before: the build had no cancel. A scan finishing mid-build left the old
#   Inventory pinned by the build's own arguments for the REST of a run whose
#   result is then discarded by the supersession check — two multi-hundred-MB
#   inventories alive at once, for minutes on a large vault.
# Change: _clear_search_cache (the manager's result listener) sets this event;
#   build_corpus polls it between report sections and raises CorpusCancelled.
# Safe because: the build's result was ALREADY discarded in this situation, so
#   stopping early changes nothing but when the references are dropped; a
#   cancelled build is not recorded as an error, and the next request re-kicks
#   with the current objects. A partial corpus is never returned — build_corpus
#   raises rather than handing back a short list.
# Lock/thread contract: the event is set under _SEARCH_CACHE_LOCK and polled
#   on the build thread; the build itself still runs OFF the request thread.
# Pinned by: test_audit.py::TestSearchCorpusCancel.
_SEARCH_CANCEL = threading.Event()


def _build_search_corpus(inv: Any, settings: Any, drift_opts: tuple) -> list:
    """Assemble the corpus (the slow part). Runs off the request thread.

    ``drift_opts`` is the ``(include_parent_tags, synonyms)`` pair captured by
    the caller — passed in (and stored in the cache key) rather than re-read
    here, so the memo can tell when a Settings toggle / ``_tags.md`` edit made
    the cached corpus disagree with what the drift tabs now render.
    """
    # The controlled vocabulary is optional context for tag_hygiene, and
    # duplicates may have been skipped on the last run — resolved exactly as the
    # corresponding report routes do.
    try:
        from api.routes.tagging import _read_vocab
        vocab, _rel = _read_vocab(load_config(), settings.vault_root)
    except Exception:  # noqa: BLE001 — vocab is advisory here
        vocab = None
    duplicates, _ = audit_manager.get_duplicates()
    near, _ = audit_manager.get_near_duplicates()
    include_parent, synonyms = drift_opts
    corpus = audit_search.build_corpus(
        inv, settings, vocab=vocab, duplicates=duplicates,
        near_duplicates=near,
        drift_include_parent_tags=include_parent,
        drift_synonyms=synonyms,
        should_cancel=_SEARCH_CANCEL.is_set,
    )
    _clean_corpus_display(corpus)
    return corpus


def _clean_corpus_display(corpus) -> None:
    """Make the search find what the tables SHOW (2026-09-19 review).

    The inventory and tagging rows display bib fields through
    ``widget.textclean.clean_display`` (`R{\\'e}gis` → `Régis`), while the
    vendored corpus indexes the raw LaTeX source — so typing the name on
    screen found nothing. The cleaned label and detail are ADDED to the
    haystack (the raw text stays matchable) and replace the displayed ones.
    Lives here, not in ``audit/search.py``: the vendored tree may not import
    ``widget``.
    """
    from widget.textclean import clean_display
    for entry in corpus:
        extra = []
        for key in ("label", "detail"):
            raw = entry.row.get(key)
            if isinstance(raw, str) and raw and ("\\" in raw or "{" in raw or "$" in raw or "~" in raw):
                cleaned = clean_display(raw)
                if cleaned and cleaned != raw:
                    entry.row[key] = cleaned
                    extra.append(cleaned)
        if extra:
            entry.haystack = entry.haystack + " " + audit_search.fold(" ".join(extra))


def _get_search_corpus_or_kick(inv: Any, settings: Any):
    """Return ``(corpus, status)``.

    ``status`` is ``"ready"`` with the corpus on a cache hit; ``"building"``
    (corpus None) when a build was started or is already running; ``"error"``
    (corpus None) with the last build error surfaced once. NEVER runs the
    minutes-long build on the caller's (request) thread — that convoy is the
    whole bug this fixes.

    The cache key is the Inventory/Settings identity pair PLUS the drift
    options (2026-08-05 verification finding): the corpus bakes in
    ``_drift_include_parent_tags``/``_drift_synonyms`` at build time, and the
    parent-tags checkbox is documented as applying "on the next report load,
    no rescan" — so a toggle (or a ``_tags.md`` edit) must miss the memo and
    rebuild, or search would return rows the drift tabs no longer render.
    """
    global _SEARCH_CACHE, _SEARCH_BUILDING, _SEARCH_BUILD_ERROR
    drift_opts = (_drift_include_parent_tags(), _drift_synonyms(settings))
    with _SEARCH_CACHE_LOCK:
        cached = _SEARCH_CACHE
        if (cached is not None and cached[0] is inv and cached[1] is settings
                and cached[2] == drift_opts):
            return cached[3], "ready"
        # Stale cache (a new scan replaced the objects, or the drift options
        # changed): drop the old refs now so the superseded Inventory isn't
        # held until the rebuild lands.
        if cached is not None:
            _SEARCH_CACHE = None
        if _SEARCH_BUILD_ERROR is not None:
            err = _SEARCH_BUILD_ERROR
            _SEARCH_BUILD_ERROR = None   # surface once, then let a retry re-kick
            return None, ("error", err)
        if _SEARCH_BUILDING:
            return None, "building"      # a build is already in flight
        _SEARCH_BUILDING = True
        _SEARCH_CANCEL.clear()

    def _build():
        global _SEARCH_CACHE, _SEARCH_BUILDING, _SEARCH_BUILD_ERROR
        try:
            corpus = _build_search_corpus(inv, settings, drift_opts)
            # Discard a SUPERSEDED result (2026-07-22 audit L2): if a new scan
            # replaced the manager's Inventory mid-build, storing ours would
            # re-pin the multi-hundred-MB old Inventory that the result
            # listener (_clear_search_cache) just released — it would linger
            # until the next search request missed the identity check.
            current_inv, _cs = audit_manager.get_inventory()
            if current_inv is not inv:
                logger.info("audit search corpus build superseded by a new scan; "
                            "result discarded")
                return
            with _SEARCH_CACHE_LOCK:
                _SEARCH_CACHE = (inv, settings, drift_opts, corpus)
                _SEARCH_BUILD_ERROR = None
        except audit_search.CorpusCancelled:
            # The scan this corpus describes was replaced mid-build; the
            # result would have been discarded by the check above anyway, so
            # stopping early is not an error — the next request re-kicks
            # against the current objects.
            logger.info("audit search corpus build cancelled by a new scan")
        except Exception as exc:  # noqa: BLE001 — surfaced to the next request
            logger.exception("audit search corpus build failed")
            with _SEARCH_CACHE_LOCK:
                _SEARCH_BUILD_ERROR = sanitise_error_msg(str(exc))
        finally:
            with _SEARCH_CACHE_LOCK:
                _SEARCH_BUILDING = False

    threading.Thread(target=_build, name="audit-search-corpus", daemon=True).start()
    return None, "building"


def _clear_search_cache() -> None:
    """Drop the memoised corpus (and its Inventory references).

    Registered as a manager result listener (below) so a replaced/cleared scan
    releases the old Inventory promptly, and also called on the no-results path.
    A build in flight is left to finish and reset its own flag; its result will
    be for the now-stale objects and simply miss the identity check next time.
    """
    global _SEARCH_CACHE, _SEARCH_BUILD_ERROR
    _clear_library_search_cache()
    with _SEARCH_CACHE_LOCK:
        _SEARCH_CACHE = None
        _SEARCH_BUILD_ERROR = None
        # Tell an in-flight build to stop: its result is for objects that are
        # already superseded, and until it returns it holds them alive.
        _SEARCH_CANCEL.set()


# Release the corpus (and its Inventory refs) the moment a scan result is
# replaced or cleared, not lazily on the next search (finding 4).
audit_manager.register_result_listener(_clear_search_cache)


# The Library documents half of the search corpus (2026-09-17 review, C2).
# It used to be rebuilt on the request thread for EVERY keystroke — measured
# warm on the live library: projection.read 0.64 s + library.documents 1.48 s
# + library.search_corpus 2.12 s. It is now built once, off-thread, per
# (Inventory, Settings) identity plus the size/mtime of the decision stores the
# projection joins, so a decision recorded since the build still invalidates it
# without an rglob per keystroke. Like the report corpus, a PDF added or edited
# on disk since the build is reflected after the next scan or decision.
_LIBRARY_SEARCH_LOCK = threading.Lock()
_LIBRARY_SEARCH: dict[str, Any] = {"key": None, "value": None, "building": False, "error": None}


def _clear_library_search_cache() -> None:
    """Dropped with the report corpus (``_clear_search_cache``) — the same
    result listener and no-results path — so a superseded Inventory is not
    pinned by this memo either."""
    with _LIBRARY_SEARCH_LOCK:
        _LIBRARY_SEARCH.update(key=None, value=None, error=None, combined=None)


def _library_store_signature(settings: Any) -> tuple:
    from accession import reconciliation as _rec, review as _review
    from zotcreate import attempts as _attempts
    paths = []
    for store in (_rec.store_path(), _review.store_path(), _attempts.store_path()):
        paths += [Path(store), Path(str(store) + "-wal")]
    paths += [Path(p) for p in (getattr(settings, "mapping_file", None), getattr(settings, "master_bib", None)) if p]
    signature: list[tuple[str, int | None, int | None]] = []
    for path in paths:
        try:
            st = os.stat(path)
            signature.append((str(path), st.st_size, st.st_mtime_ns))
        except OSError:
            signature.append((str(path), None, None))
    return tuple(signature)


def _library_search_or_kick(inv: Any, settings: Any):
    """``(value, status)`` — the memoised ``(library entries, projection)``,
    or ``None`` with ``"building"`` / ``("error", message)``."""
    signature = _library_store_signature(settings)
    with _LIBRARY_SEARCH_LOCK:
        key = _LIBRARY_SEARCH["key"]
        if key is not None and key[0] is inv and key[1] is settings and key[2] == signature:
            return _LIBRARY_SEARCH["value"], "ready"
        error = _LIBRARY_SEARCH["error"]
        if error is not None:
            _LIBRARY_SEARCH["error"] = None
            # Only an error from a build of THESE objects is this request's.
            if error[0] is inv and error[1] is settings:
                return None, ("error", error[2])
        if _LIBRARY_SEARCH["building"]:
            return None, "building"
        _LIBRARY_SEARCH["building"] = True

    def _build():
        try:
            from accession import library, projection
            current = projection.read(settings)
            view = library.documents(settings, inv, pdf_workspace=current)
            value = (library.search_corpus(view), current)
            with _LIBRARY_SEARCH_LOCK:
                _LIBRARY_SEARCH.update(key=(inv, settings, signature), value=value, error=None)
        except Exception as exc:  # noqa: BLE001 — surfaced to the next request
            logger.exception("library search corpus build failed")
            with _LIBRARY_SEARCH_LOCK:
                _LIBRARY_SEARCH["error"] = (inv, settings, sanitise_error_msg(str(exc)))
        finally:
            with _LIBRARY_SEARCH_LOCK:
                _LIBRARY_SEARCH["building"] = False

    thread = threading.Thread(target=_build, name="library-search-corpus", daemon=True)
    _LIBRARY_SEARCH["thread"] = thread
    thread.start()
    return None, "building"


def join_library_search_build(timeout: float = 5.0) -> None:
    """Wait for an in-flight Library search build (tests; the widget
    ``join_snapshot_refresh`` precedent)."""
    thread = _LIBRARY_SEARCH.get("thread")
    if thread is not None and thread.is_alive():
        thread.join(timeout)





@audit_bp.route("/api/audit/search")
def api_audit_search():
    """Free-text search across every report of the cached scan.

    ``?q=`` is matched accent- and case-insensitively; all whitespace-separated
    tokens must match (AND). Results are grouped by report and capped — each
    group reports its true match ``count`` alongside the (possibly shorter) row
    list, so the UI never silently hides matches.

    404s with the same "Run a scan first" message as the report routes when no
    scan is cached; the reports are views of that one snapshot, so a mapping
    edit is not reflected until the next scan (matching the report tabs).
    """
    q = coerce_non_empty_string(
        request.args.get("q"), max_len=audit_search.MAX_QUERY_LEN
    )
    if q is None or len(q) < audit_search.MIN_QUERY_LEN:
        return jsonify({
            "error": f"Query must be at least {audit_search.MIN_QUERY_LEN} characters."
        }), 400

    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        _clear_search_cache()
        return jsonify({"error": "No scan results. Run a scan first."}), 404

    corpus, status = _get_search_corpus_or_kick(inv, settings)
    if corpus is None:
        # The build runs off-thread (never on this request). 202 tells the
        # client to show "Preparing search index…" and retry; 500 surfaces a
        # build error once.
        if isinstance(status, tuple) and status[0] == "error":
            return jsonify({"error": status[1]}), 500
        return jsonify({"building": True, "q": q}), 202
    # Read the no-PDF dismissals fresh, like the report route does: they apply
    # on the next LOAD, not the next scan, and the corpus is memoised per scan.
    # Without this a dismissed entry stayed findable here and led to a report
    # that no longer lists it — the corpus's "a hit is a row you can go and
    # look at" invariant.
    from audit.engine import pdf_wanted

    from accession import projection
    library_part, status = _library_search_or_kick(inv, settings)
    if library_part is None:
        if isinstance(status, tuple) and status[0] == "error":
            return jsonify({"error": status[1]}), 500
        return jsonify({"building": True, "q": q}), 202
    library_entries, current = library_part
    with _LIBRARY_SEARCH_LOCK:
        combined = _LIBRARY_SEARCH.get("combined")
        if combined is None or combined[0] is not corpus or combined[1] is not library_part:
            combined = (corpus, library_part, [*library_entries, *projection.search_corpus(corpus, current)])
            _LIBRARY_SEARCH["combined"] = combined
    result = audit_search.search(combined[2], q, hidden_keys=pdf_wanted.load_dismissed())
    result['pdf_reconciliation'] = {k:v for k,v in current.items() if k != 'rows'}
    return jsonify(result)


@audit_bp.route("/api/audit/pdf")
def api_audit_pdf():
    """Serve a vault PDF's bytes for the ambiguous-PDF triage preview.

    ``?rel=`` is a vault-relative path. Unlike ``/api/tagging/pdf`` this cannot
    resolve by citation key — an ambiguous PDF has none by definition — so the
    client supplies the path and the server re-derives trust from scratch:
    shape check (``_normalise_rel_path``) → ``resolve_under_root`` → an
    INDEPENDENT realpath re-check that the resolved file still sits under the
    vault root and is a regular ``.pdf``. The realpath step is what guards a
    symlink planted since the scan (the same posture as tagging's resolver).

    Containment is the vault root rather than ``biblio_articles/``, which is
    deliberately WIDER than this route's data needs. A previous version of this
    docstring justified it by claiming ambiguous PDFs come from the whole
    scanned attachments tree — they do not: ``inventory._walk_biblio_pdfs``
    walks ``biblio_articles_dir`` and nothing else, so every path the bridge can
    park as ambiguous is already biblio-scoped. Keeping the vault root is
    defence in depth (it costs nothing and survives a future report reusing this
    route for a wider PDF set); narrowing it would be safe but is a change to a
    security boundary, not a tidy-up. Corrected 2026-07-31.

    The app-level origin guard 403s any ``/api`` route without the
    ``X-Requested-With`` header, so the client MUST fetch this with
    ``secureFetch`` and render via a ``blob:`` URL — an ``<iframe src>``
    pointing here cannot work.
    """
    rel = _normalise_rel_path(request.args.get("rel"))
    if not rel:
        return jsonify({"error": "Invalid rel path"}), 400
    try:
        settings = load_settings()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400

    resolved = resolve_under_root(rel, settings.vault_root)
    if not resolved:
        return jsonify({"error": "rel path escapes vault root"}), 400

    candidate = settings.vault_root / resolved
    try:
        real = Path(os.path.realpath(candidate))
        real_root = Path(os.path.realpath(settings.vault_root))
        if not (real == real_root or real_root in real.parents):
            return jsonify({"error": "rel path escapes vault root"}), 400
        if not real.is_file() or real.suffix.lower() != ".pdf":
            return jsonify({"error": "Not a PDF"}), 404
    except OSError as exc:
        return jsonify({"error": sanitise_error_msg(str(exc))}), 404

    return send_file(str(real), mimetype="application/pdf", conditional=True)


#: Bound on ``POST /api/audit/pdf-facts``: each PDF costs a hash, a pikepdf
#: open and a page-1 text read, all on the request thread.
_PDF_FACTS_MAX = 50
_PDF_FACTS_EXCERPT_CHARS = 900


@audit_bp.route("/api/audit/pdf-facts", methods=["POST"])
def api_audit_pdf_facts():
    """Read-only per-PDF evidence for a duplicate decision (annual cleanup,
    2026-09-17): ``{pdfs: [vault-relative rel, ≤50]}`` → ``{facts: [...]}``.

    Each fact carries ``sha256``/``size``, ``pages`` and ``klass`` (the bridge's
    provenance class), ``embedded_doi``, ``printed_identifier`` (page 1),
    ``page1_excerpt`` and ``biblio_rel`` (the form the authenticate route
    takes). The provenance and page-1 text come through ``zotcreate``'s own
    helpers, so the evidence matches what the reports and the create form
    show. A path that is not a regular PDF under ``biblio_articles/`` (after a
    realpath re-check) answers ``error`` for THAT row only, so one moved file
    does not hide the rest. Writes nothing beyond the regenerable text and
    DOI memos those helpers already maintain; refused while a scan runs,
    since the scan writes the same memos.
    """
    import hashlib

    data = request.get_json(silent=True) or {}
    pdfs = data.get("pdfs")
    if (not isinstance(pdfs, list) or not pdfs or len(pdfs) > _PDF_FACTS_MAX
            or not all(isinstance(p, str) for p in pdfs)):
        return jsonify({"error": f"pdfs must be a list of 1–{_PDF_FACTS_MAX} paths."}), 400
    if audit_manager.is_scanning():
        return jsonify({"error": "A scan is running. Wait for it to finish."}), 409
    try:
        settings = load_settings()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400

    from api.routes.zotcreate import _page1_text, _provenance
    from audit.core import pdf_identifiers as pdf_ids

    real_biblio = Path(os.path.realpath(settings.biblio_articles_dir))
    facts = []
    for raw in pdfs:
        fact: dict[str, Any] = {"rel": raw}
        rel = _normalise_rel_path(raw)
        resolved = resolve_under_root(rel, settings.vault_root) if rel else None
        real = Path(os.path.realpath(settings.vault_root / resolved)) if resolved else None
        if (real is None or real_biblio not in real.parents or real.suffix.lower() != ".pdf"
                or not real.is_file()):
            fact["error"] = "Not a PDF under the bibliography folder."
            facts.append(fact)
            continue
        try:
            digest = hashlib.sha256()
            with open(real, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            prov = _provenance(real, settings)
            text = _page1_text(real) or ""
            try:
                embedded = pdf_ids.extract_embedded_doi(real) or None
            except Exception:  # noqa: BLE001 — advisory evidence
                embedded = None
            fact.update(
                biblio_rel=real.relative_to(real_biblio).as_posix(),
                sha256=digest.hexdigest(), size=real.stat().st_size,
                pages=prov.get("pages"), klass=prov.get("klass"),
                embedded_doi=embedded,
                printed_identifier=pdf_ids.extract_printed_identifier(text) if text else None,
                page1_excerpt=text[:_PDF_FACTS_EXCERPT_CHARS])
        except OSError as exc:
            fact = {"rel": raw, "error": sanitise_error_msg(str(exc))}
        facts.append(fact)
    return jsonify({"facts": facts})


#: Bound on the list form of the mapping route. One atomic RMW under
#: ``_MAPPING_RMW_LOCK``; the whole Not-in-Zotero bucket measured 291.
_MAPPING_MAX_PDFS = 500

#: Surfaced on detach responses that also trash Zotero linked attachments.
_DETACH_ZOTERO_NOTE = (
    "Desktop writes take effect locally; Web API writes require Zotero sync. "
    "Unmap undo is [clear] on Not in Zotero; Zotero trash undo is "
    "Tagging → Restore."
)


def _detach_parent_item_keys(inv, citation_key: str) -> set[str] | None:
    """Parent Zotero item keys that a detach may trash links under.

    ``None`` means detach_all — every parent that currently links the PDF.
    An empty set means the key has no Zotero parent in the cached scan
    (nothing to filter to; other parents' links are left alone).
    """
    if citation_key == eng_bridge.DETACH_ALL:
        return None
    if inv is None:
        return set()
    rec = inv.records.get(citation_key)
    zot = getattr(rec, "zotero_item", None) if rec is not None else None
    if zot is None or not getattr(zot, "item_key", None):
        return set()
    return {zot.item_key}


def _trash_prepared_attachments(prepared, settings, cfg):
    """Journal + PATCH already-prepared trash plans.

    Prepare runs at the call site BEFORE :func:`eng_bridge.detach` so a
    refuse leaves mapping untouched. Returns
    ``(trashed_rows, batch_id, error_response_or_None)``.
    """
    from rag.vault import obsidian_manager
    from tagging import journal
    from tagging.writers import (
        zotero_attachment_journal,
        zotero_attachment_patch,
    )

    if not prepared:
        return [], "", None

    vault_root = Path(settings.vault_root)
    op_epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not op_epoch:
        return [], "", (jsonify({
            "error": "Indexing is in progress; try again shortly.",
        }), 503)
    batch_id = journal.new_op_id({})
    patch_jobs: list[tuple[dict, str]] = []
    try:
        try:
            manifest = journal.load_for_ops(vault_root, cfg)
        except (journal.ScopeError, journal.JournalUnreadableError) as exc:
            return [], "", (jsonify({"error": sanitise_error_msg(exc)}), 500)
        for plan in prepared:
            op = zotero_attachment_journal(
                prepared=plan, manifest=manifest, batch_id=batch_id)
            patch_jobs.append((plan, op["op_id"]))
        journal.save(vault_root, cfg, manifest)
    finally:
        obsidian_manager.release_lock(op_epoch)

    trashed: list[dict] = []
    outcomes: list[tuple[str, dict]] = []
    for plan, op_id in patch_jobs:
        try:
            r, status, new_version = zotero_attachment_patch(plan)
        except Exception:  # noqa: BLE001 — per-key truth
            logger.exception("detach trash patch failed for %s",
                             plan["commit"]["item_key"])
            outcomes.append((op_id, {"state": "failed"}))
            continue
        if status == 200 and r.get("ok"):
            c = plan["commit"]
            trashed.append({
                "item_key": c["item_key"],
                "parent_key": c.get("parent_item"),
                "path": c.get("filename") or "",
            })
            outcomes.append((op_id, {"version_after": new_version}))
        else:
            outcomes.append((op_id, {"state": "failed"}))

    op_epoch2 = obsidian_manager.try_acquire_lock(ttl=60)
    if op_epoch2:
        try:
            try:
                manifest2 = journal.load(vault_root, cfg)
            except (journal.ScopeError, journal.JournalUnreadableError):
                manifest2 = None
            if manifest2 is not None:
                for op_id, mut in outcomes:
                    op2 = journal.find_op(manifest2, op_id)
                    if op2 is not None:
                        op2.update(mut)
                journal.prune(vault_root, cfg, manifest2,
                              protect_batch_id=batch_id)
                journal.save(vault_root, cfg, manifest2)
        finally:
            obsidian_manager.release_lock(op_epoch2)

    return trashed, batch_id, None


def _zotero_link_warning(settings, rels: list[str]) -> str | None:
    """If the last scan paired any of *rels* via a Zotero link, say so.

    Clearing or detaching ``mapping.json`` does not change that pairing
    (Zotero wins, reported). The write still lands; the warning is
    observational so the Inventory unmap verbs cannot look like they
    undid the live link.
    """
    from audit.engine.bridge import _rel_to_vault
    from audit.manager import audit_manager

    inv, _s = audit_manager.get_inventory()
    if inv is None:
        return None
    wanted = set(rels)
    hit: list[str] = []
    bridge = getattr(inv, "bridge", None)
    for p, src in (getattr(bridge, "source_per_pdf", None) or {}).items():
        if src != "zotero_link":
            continue
        rel = _rel_to_vault(p, settings.vault_root)
        if rel in wanted and rel not in hit:
            hit.append(rel)
    if not hit:
        return None
    names = ", ".join(Path(r).name for r in hit)
    return (f"Zotero currently links {names}. Changing mapping.json does not "
            "change that pairing (Zotero wins, reported). Trash the linked "
            "attachment if you want the ladder to reclaim the file.")


def _mapping_action(data: dict):
    """Pick the one mapping action a body names, or return an error tuple.

    Six actions (2026-09-06: ``pending`` and ``detach_from``; 2026-09-07:
    ``detach_all``). Exactly one selector is allowed and every field must
    belong to that action — a body carrying two verbs is refused rather than
    interpreted, and ``clear`` (an undo) is checked FIRST so an undo body that
    also carries a key can never be read as a re-add.
    """
    selectors = [name for name in ("clear", "detach_all", "detach_from",
                                   "citation_key", "no_match", "pending")
                 if name in data]
    if len(selectors) != 1:
        return None, ("Choose exactly one mapping action: citation_key, "
                      "no_match, pending, clear, detach_from, or detach_all.")
    action = selectors[0]
    allowed = {
        # accept_excluded: rematch a key previously detached for this PDF
        # (2026-09-11). trash_zotero_links: also move matching linked_file
        # attachments to Zotero trash in the same confirm (opt-in, explicit).
        "citation_key": {"pdf", "citation_key", "source", "confirm",
                         "accept_excluded"},
        "no_match": {"pdf", "pdfs", "no_match", "confirm"},
        "pending": {"pdf", "pdfs", "pending", "confirm"},
        "clear": {"pdf", "pdfs", "clear", "confirm"},
        "detach_from": {"pdf", "detach_from", "confirm", "trash_zotero_links"},
        "detach_all": {"pdf", "detach_all", "confirm", "trash_zotero_links"},
    }[action]
    unknown = sorted(set(data) - allowed)
    if unknown:
        return None, ("Unexpected field(s) for this mapping action: "
                      + ", ".join(unknown))
    for flag in ("no_match", "pending", "clear", "detach_all"):
        if action == flag and coerce_bool(data.get(flag)) is not True:
            return None, f"{flag} must be true."
    return action, None


def _mapping_pdf_list(data: dict):
    """The request's PDF rels: ``pdfs: [...]`` (list form) or ``pdf``.

    Returns ``(rels, error)``. The two forms are exclusive so a body cannot
    name a file twice with different intent; the list is de-duplicated in
    order and bounded.
    """
    if "pdfs" in data and "pdf" in data:
        return None, "Give either pdf or pdfs, not both."
    if "pdfs" in data:
        raw = data.get("pdfs")
        if not isinstance(raw, list) or not raw:
            return None, "pdfs must be a non-empty list"
        if len(raw) > _MAPPING_MAX_PDFS:
            return None, f"At most {_MAPPING_MAX_PDFS} PDFs per request."
        rels = []
        for item in raw:
            rel = _normalise_rel_path(item)
            if not rel:
                return None, "Invalid pdf path in pdfs"
            if rel not in rels:
                rels.append(rel)
        return rels, None
    rel = _normalise_rel_path(data.get("pdf"))
    if not rel:
        return None, "Invalid pdf path"
    return [rel], None


@audit_bp.route("/api/audit/mapping", methods=["POST"])
def api_audit_mapping():
    """Curate the PDF↔bib mapping (``BASE_DIR/audit/mapping.json``).

    Body shapes (every one needs ``confirm: true``; ``pdf`` may be ``pdfs:
    [...]`` — ≤ 500, one atomic write — on the three bucket verbs)::

        {"pdf": "...", "citation_key": "smith2020"}   # link (one PDF)
        {"pdf"|"pdfs": ..., "no_match": true}         # "not a paper for Zotero"
        {"pdf"|"pdfs": ..., "pending": true}          # "add to Zotero later"
        {"pdf"|"pdfs": ..., "clear": true}            # undo: un-curate
        {"pdf": "...", "detach_from": "smith2020"}    # detach from one key
        {"pdf": "...", "detach_all": true}            # ...from every key

    ``no_match`` / ``pending`` are the two curated states (2026-09-06 split):
    both leave every queue at the next scan, and moving between them is the
    same verb with the other flag. ``detach_from`` records an exclusion so no
    automatic rung re-claims that PDF for that key — the multi-PDF review's
    verb — and the file returns to the orphan queue. ``detach_all``
    (2026-09-07) does the same for EVERY key at once: per-key detaching is
    whack-a-mole, since the ladder just finds the next candidate, and a file
    that belongs to no bib entry had to be detached once per reachable key.
    Unlike ``no_match`` / ``pending`` it leaves the PDF on the pairings board
    as an unmapped file, so it stays a visible open question rather than
    being filed away. ``clear`` undoes any of them. A list request is
    validated in full BEFORE any write: a partial bulk write would leave the
    user unable to tell which half landed.

    Paths are interpreted relative to the configured vault root.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Mapping change must be explicitly confirmed "
                                 "(confirm: true)."}), 400
    action, err = _mapping_action(data)
    if err:
        return jsonify({"error": err}), 400
    rels, err = _mapping_pdf_list(data)
    if err:
        return jsonify({"error": err}), 400
    assert rels is not None

    try:
        settings = load_settings()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400

    resolved_abs: list[Path] = []
    for pdf_rel in rels:
        pdf_rel_resolved = resolve_under_root(pdf_rel, settings.vault_root)
        if not pdf_rel_resolved:
            return jsonify({"error": "pdf path escapes vault root"}), 400
        resolved_abs.append(settings.vault_root / pdf_rel_resolved)

    # NOTE (2026-07-20 audit finding 2): the per-POST hash refresh no longer
    # re-hashes EVERY curated entry. The writers store the written PDF's own
    # current hash, and the full re-anchor of *other* entries moved to scan
    # time (audit/manager._run). `clear` needs no hash at all.
    if action == "clear":
        # Clearing is deliberately lexical and does not require the file to
        # exist: moved/deleted PDFs and stale keys must remain removable.
        changed = eng_bridge.clear_curations(
            settings.mapping_file, resolved_abs, settings.vault_root
        )
        warning = _zotero_link_warning(settings, rels)
        if len(rels) == 1:
            body = {"ok": True, "action": "clear", "pdf": rels[0],
                    "changed": changed > 0}
        else:
            body = {"ok": True, "action": "clear", "pdfs": rels,
                    "changed": changed}
        if warning:
            body["warning"] = warning
        return jsonify(body)

    # Forward decisions require a current regular PDF inside the configured
    # bibliography scope. Realpath containment is re-derived at mutation time;
    # a symlink or a stale scan cannot widen the server's authority. Every
    # PDF of a list is checked before any write.
    real_pdfs: list[Path] = []
    try:
        real_biblio = settings.biblio_articles_dir.resolve(strict=True)
    except (OSError, ValueError):
        return jsonify({"error": "Bibliography folder is not readable."}), 400
    for pdf_rel, pdf_abs in zip(rels, resolved_abs):
        try:
            real_pdf = pdf_abs.resolve(strict=True)
            real_pdf.relative_to(real_biblio)
            mode = real_pdf.stat().st_mode
        except FileNotFoundError:
            return jsonify({"error": f"PDF not found: {Path(pdf_rel).name}"}), 404
        except (OSError, ValueError):
            return jsonify({"error": f"PDF is outside the bibliography scope: "
                                     f"{Path(pdf_rel).name}"}), 400
        if not stat.S_ISREG(mode) or real_pdf.suffix.casefold() != ".pdf":
            return jsonify({"error": f"Not a regular PDF: {Path(pdf_rel).name}"}), 400
        real_pdfs.append(real_pdf)

    if action in ("no_match", "pending"):
        try:
            eng_bridge.set_curated_state(
                settings.mapping_file, real_pdfs, settings.vault_root, action,
                require_hash=True)
        except ValueError:
            return jsonify({"error": "A PDF in the request is not readable."}), 409
        if len(rels) == 1:
            return jsonify({"ok": True, "action": action, "pdf": rels[0]})
        return jsonify({"ok": True, "action": action, "pdfs": rels,
                        "changed": len(rels)})

    pdf_rel, pdf_abs = rels[0], real_pdfs[0]

    if action in ("detach_from", "detach_all"):
        if action == "detach_all":
            key = eng_bridge.DETACH_ALL
        else:
            key = coerce_non_empty_string(data.get("detach_from"), max_len=128)
            if not key:
                return jsonify({"error": "detach_from must be a citation key."}), 400
            if key == eng_bridge.DETACH_ALL:
                # The wildcard is not a citation key and must be reached by
                # its own verb, so a body cannot smuggle a blanket refusal in
                # where the caller (and the confirm text) meant one key.
                return jsonify({"error": "Use detach_all: true to detach from "
                                         "every key."}), 400
        # Explicit bool from the UI (default OFF when omitted — never trash
        # Zotero attachments without an affirmative request). When true and
        # matching linked_file attachments exist, refuse the WHOLE request if
        # Zotero cannot be read/written so the UI cannot claim "unmapped"
        # while the links remain. Order: prepare → detach → journal/PATCH.
        trash = coerce_bool(data.get("trash_zotero_links")) is True
        prepared: list = []
        cfg = None
        if trash:
            from tagging.writers import (
                find_linked_attachments,
                zotero_attachment_prepare,
            )
            from tagging.zotero_write import resolve_transport

            refs, zerr = find_linked_attachments(settings, str(pdf_abs))
            if zerr:
                return jsonify({
                    "error": zerr,
                    "reason": "zotero_unreadable",
                }), 409
            inv, _ = audit_manager.get_inventory()
            if key != eng_bridge.DETACH_ALL and inv is None and refs:
                return jsonify({
                    "error": ("Run a Library Audit scan first so unmap can "
                              "identify which Zotero parent owns this PDF."),
                    "reason": "no_scan",
                }), 409
            parent_keys = _detach_parent_item_keys(inv, key)
            filtered = (list(refs) if parent_keys is None
                        else [r for r in refs
                              if getattr(r, "parent_key", None) in parent_keys])
            if filtered:
                cfg = load_config()
                transport = resolve_transport(cfg)
                if not transport.can_write:
                    return jsonify({
                        "error": (
                            "Zotero linked attachments exist for this PDF, but "
                            "the configured transport cannot write "
                            f"({transport.kind}). Configure a writable "
                            "transport, or uncheck “Also trash Zotero linked "
                            "attachment(s)” to unmap only."
                        ),
                        "reason": "zotero_not_writable",
                        "transport": transport.kind,
                    }), 409
                for ref in filtered:
                    try:
                        plan = zotero_attachment_prepare(
                            cfg, ref.item_key, ref)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception(
                            "detach trash prepare failed for %s",
                            getattr(ref, "item_key", "?"))
                        return jsonify({
                            "error": sanitise_error_msg(exc),
                            "reason": "zotero_prepare_failed",
                        }), 409
                    if "early" in plan:
                        early = plan["early"]
                        if not early.get("ok"):
                            return jsonify({
                                "error": early.get("message")
                                         or "Zotero refuse.",
                                "reason": early.get("status")
                                          or "zotero_refuse",
                            }), 409
                        continue
                    prepared.append(plan)

        changed = eng_bridge.detach(settings.mapping_file, pdf_abs, key,
                                    settings.vault_root)
        trashed: list[dict] = []
        batch_id = ""
        zotero_error = None
        if prepared:
            # Detach already landed — never answer 5xx for a trash follow-up
            # failure (that made the UI claim "Unmap failed" while exclude
            # was already written). Always 200 with zotero_error disclosed.
            trashed, batch_id, err = _trash_prepared_attachments(
                prepared, settings, cfg)
            if err is not None:
                body, _status = err
                payload = body.get_json() if hasattr(body, "get_json") else {}
                if not isinstance(payload, dict):
                    payload = {}
                zotero_error = (payload.get("error")
                                or "Zotero trash failed after unmap.")
            elif len(trashed) < len(prepared):
                failed_n = len(prepared) - len(trashed)
                zotero_error = (
                    f"Unmapped, but {failed_n} of {len(prepared)} Zotero "
                    "linked attachment(s) could not be moved to trash. "
                    "Check Zotero (and Tagging → Restore for any applied "
                    "trash ops)."
                )
        out = {
            "ok": True,
            "action": action.replace("_from", ""),
            "pdf": pdf_rel,
            "citation_key": key,
            "changed": changed,
            "trashed": trashed,
            "batch_id": batch_id or None,
        }
        if trash:
            out["zotero_note"] = _DETACH_ZOTERO_NOTE
        if zotero_error:
            out["zotero_error"] = zotero_error
        warning = _zotero_link_warning(settings, [pdf_rel])
        if warning:
            out["warning"] = warning
        return jsonify(out)

    citation_key = coerce_non_empty_string(data.get("citation_key"), max_len=128)
    if not citation_key:
        return jsonify({"error": "citation_key must be a non-empty string."}), 400

    # Memoised on the bib's (size, mtime_ns): this used to re-parse a
    # 6141-entry / 7.2 MB _master.bib (3900 ms measured) on EVERY triage click,
    # for one set-membership test. An edit to the bib still applies on the next
    # click without a rescan, because mtime is the key.
    from audit.core.bib import parse_bib_keys
    authoritative_keys = parse_bib_keys(settings.master_bib)
    if citation_key not in authoritative_keys:
        # This client-correctable validation error names both sides of the
        # rejected relationship, but only the basename: absolute vault paths
        # are implementation detail and must not escape into the renderer.
        return jsonify({"error": f"Cannot link {Path(pdf_rel).name}: citation "
                                 f"key is not in the current bibliography: "
                                 f"{citation_key}"}), 409

    # Optional provenance annotation (2026-08-19): "accession" marks a link
    # written by the accession UI so the next scan's pairings board can tell
    # it from hand curation. Allowlisted — an arbitrary client string must
    # not become a scan-visible source label.
    source = data.get("source")
    if source == 'accession':
        from accession import review
        if review.snapshot(settings.vault_root).get(pdf_rel):
            return jsonify(error='This PDF has saved review decisions. Use the PDF workspace to reconcile its confirmed identity.'), 409
    if source is not None and source != "accession":
        return jsonify({"error": "source must be 'accession' when given"}), 400

    # Excluded-key rematch gate (2026-09-11): detach writes exclude[rel];
    # add_matches historically lifted that exclusion on any hand link, which
    # let Accession "Link selected" silently re-map the same wrong key. Refuse
    # unless the caller explicitly acknowledges (accept_excluded: true). Bulk
    # Link never sends the ack; a per-row "link anyway" may.
    accept_excluded = coerce_bool(data.get("accept_excluded")) is True
    mapping_data = eng_bridge._load_mapping_all(settings.mapping_file)
    if eng_bridge.link_would_lift_exclude(
            mapping_data.exclude.get(pdf_rel), citation_key
    ) and not accept_excluded:
        return jsonify({
            "error": (
                f"{Path(pdf_rel).name} was unmapped from "
                f"{citation_key if citation_key in mapping_data.exclude.get(pdf_rel, []) else 'every key'}; "
                "linking it again needs an explicit acknowledgement "
                "(accept_excluded: true)."
            ),
            "reason": "excluded",
            "citation_key": citation_key,
            "excluded_keys": list(mapping_data.exclude.get(pdf_rel, [])),
        }), 409

    try:
        eng_bridge.add_match(
            settings.mapping_file, pdf_abs, citation_key, settings.vault_root,
            source=source or "manual", require_hash=True,
        )
    except ValueError:
        return jsonify({"error": f"PDF is not readable: "
                                 f"{Path(pdf_rel).name}"}), 409
    return jsonify({
        "ok": True,
        "action": "match",
        "pdf": pdf_rel,
        "citation_key": citation_key,
    })


@audit_bp.route("/api/audit/apply-tag-drift", methods=["POST"])
def api_audit_apply_tag_drift():
    """Write missing Zotero tags into an Obsidian note's frontmatter (Zotero→Obsidian).

    Body: ``{citation_key, tags: [...], confirm: true}``. This is the audit's
    FIRST and only vault-mutating action. Guardrails:

    * ``confirm: true`` is required (400 otherwise).
    * The note is looked up from the cached scan (``citation_key`` → record →
      ``obsidian_note.path``); a scan must have run (404 otherwise).
    * The requested tags are intersected server-side with the record's actual
      drift set (fingerprint-keyed ``missing_by_key`` — a tag the note already
      carries under another spelling is refused) — the client cannot inject an
      arbitrary tag; only genuinely-missing Zotero tags are ever written.
    * The write holds the obsidian op-lock (503 if indexing), backs the note up
      to ``<note>.md.bak`` first, writes atomically, and emits a
      ``log_vault_write`` audit line. The ``.bak`` is the recovery path.
    * Only the ``tags:`` frontmatter field is edited; every other byte is
      preserved (``merge_tags_into_frontmatter`` refuses rather than reformat).
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Tag write must be explicitly confirmed (confirm: true)."}), 400

    citation_key = coerce_non_empty_string(data.get("citation_key"), max_len=128)
    if not citation_key:
        return jsonify({"error": "citation_key required"}), 400

    raw_tags = data.get("tags")
    if not isinstance(raw_tags, list) or not raw_tags:
        return jsonify({"error": "tags must be a non-empty list"}), 400
    requested = [coerce_string_max_len(t, 128) or "" for t in raw_tags[:200]]

    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "No scan results. Run a scan first."}), 404

    rec = inv.records.get(citation_key)
    if rec is None or rec.obsidian_note is None:
        return jsonify({"error": "No Obsidian note for that citation key."}), 404

    # Server-authoritative drift: only tags Zotero has that Obsidian lacks may
    # be written. Keyed on the SAME fingerprint the report uses
    # (normalize.tag_key via missing_by_key — plan 2026-08-05/2), so a tag the
    # note already carries under another spelling (a report "variant") is
    # refused here exactly as the report refuses to offer it: report and write
    # path can no longer disagree on what "missing" means.
    from audit.engine.normalize import canonical_tag_key, clean_tag
    from audit.engine.reports.note_tag_drift import missing_by_key

    # Same options as the report dispatch (parent-tags toggle + synonym map),
    # so what the report offers and what this route accepts stay one set.
    # CRITICAL: the requested tags must be looked up via canonical_tag_key
    # with the SAME synonym map the drift map was built with — a plain
    # tag_key lookup against a synonym-canonicalised map made every
    # synonym-remapped missing tag unapplyable (400 on the very surface the
    # report offered, forever — the 2026-08-05 verification pass's finding).
    synonyms = _drift_synonyms(settings)
    zotero_side = set(rec.zotero_note_tags)
    if _drift_include_parent_tags() and rec.zotero_item:
        zotero_side |= set(rec.zotero_item.tags)
    drift_map = missing_by_key(zotero_side, rec.obs_tags, synonyms=synonyms)
    to_write = []
    for t in requested:
        c = clean_tag(t)
        if not c:
            continue
        surface = drift_map.get(canonical_tag_key(c, synonyms))
        if surface and surface not in to_write:
            to_write.append(surface)
    if not to_write:
        return jsonify({"error": "None of the requested tags are in this note's drift set."}), 400

    note_path = rec.obsidian_note.path
    # Confirm the note still resolves under the vault root (defence in depth).
    try:
        rel = note_path.resolve().relative_to(settings.vault_root.resolve())
    except (OSError, ValueError):
        return jsonify({"error": "Note path escapes the vault root."}), 400

    from rag.vault import obsidian_manager

    op_epoch = obsidian_manager.try_acquire_lock(ttl=3600)
    if not op_epoch:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    try:
        from api.routes._audit_tagwrite import merge_tags_into_frontmatter
        from core.utils import log_vault_write, write_text_atomic

        try:
            original = note_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return jsonify({"error": f"Cannot read note: {sanitise_error_msg(exc)}"}), 400

        new_text, added, reason = merge_tags_into_frontmatter(original, to_write)
        if new_text is None:
            return jsonify({"error": f"Cannot safely edit frontmatter: {reason}"}), 422
        if not added:
            return jsonify({"ok": True, "added": [], "note": str(rel), "message": "Tags already present."})

        # Back up the original before overwriting — the .bak is the recovery
        # path (parity with deck compile-fix). NON-CLOBBERING: only written if
        # absent, so the FIRST backup — the true pre-audit original — survives
        # repeated applies to the same note (a second apply would otherwise
        # overwrite it with the once-modified text, losing the original). A
        # failed backup aborts the write.
        bak_path = str(note_path) + ".bak"
        if not os.path.exists(bak_path):
            try:
                write_text_atomic(bak_path, original)
            except OSError as exc:
                return jsonify({"error": f"Backup failed, write aborted: {sanitise_error_msg(exc)}"}), 500

        write_text_atomic(str(note_path), new_text)
        log_vault_write("audit_tag_add", str(note_path), f"+{len(added)} tags: {', '.join(added)}")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)

    # `still_missing` lets the UI update the drift row in place (strike the
    # written tags) without a rescan: the scan-time drift set minus everything
    # this request just wrote. The cached inventory itself stays stale until
    # the next scan — by design; the scan is the only source of truth.
    added_keys = {canonical_tag_key(t, synonyms) for t in added}
    still_missing = sorted(
        surface for k, surface in drift_map.items() if k not in added_keys)
    return jsonify({
        "ok": True,
        "added": added,
        "still_missing": still_missing,
        "note": str(rel),
        "citation_key": citation_key,
    })


@audit_bp.route("/api/audit/reveal", methods=["POST"])
def api_audit_reveal():
    """Open a local file in Finder / the default app, or jump to a Zotero item.

    Three modes:

    - ``{"path": "<abs>"}`` reveals the file in Finder (``open -R``)
    - ``{"open": "<abs>"}`` opens the file with the default app (``open``)
    - ``{"zotero_key": "<key>"}`` opens ``zotero://select/library/items/<key>``;
      with an additional ``{"zotero_group_id": <int>}`` it opens
      ``zotero://select/groups/<id>/items/<key>`` — the personal-library form
      selects nothing for a group-library item (2026-08-18)

    The browser-side equivalent (``window.open``) cannot reach the
    filesystem or trigger custom URL handlers in PyWebView's sandboxed
    renderer, so this route shells out on its behalf. Refuses paths
    outside the configured vault root (or Zotero storage) to keep the
    blast radius bounded.
    """
    if sys.platform != "darwin":
        return jsonify({"error": "Reveal is supported on macOS only"}), 501

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400

    try:
        settings = load_settings()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400

    zotero_key = data.get("zotero_key")
    if isinstance(zotero_key, str) and re.fullmatch(r"[A-Za-z0-9]{1,16}", zotero_key):
        group_id = data.get("zotero_group_id")
        if isinstance(group_id, int) and not isinstance(group_id, bool) \
                and 0 < group_id < 10**12:
            url = f"zotero://select/groups/{group_id}/items/{zotero_key}"
        else:
            url = f"zotero://select/library/items/{zotero_key}"
        try:
            run_open(["open", url])  # noqa: S603,S607
            return jsonify({"ok": True})
        except OSError as exc:
            return jsonify({"error": sanitise_error_msg(exc)}), 500

    target_raw = data.get("path") or data.get("open")
    action = "reveal" if data.get("path") else "open"
    if not isinstance(target_raw, str) or not target_raw.strip():
        return jsonify({"error": "path / open / zotero_key required"}), 400

    try:
        target = Path(target_raw).expanduser().resolve()
    except OSError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 400

    # Bound the reveal to known-safe roots: the vault and the Zotero
    # storage tree. Anything else gets rejected so a stray request body
    # cannot point the action at arbitrary user files.
    allowed_roots = [settings.vault_root.resolve()]
    try:
        allowed_roots.append(settings.zotero_storage.resolve())
    except OSError:
        pass
    if not any(resolve_under_root(str(target), str(root)) is not None for root in allowed_roots):
        return jsonify({"error": "Path is outside the configured roots"}), 400
    if not target.exists():
        return jsonify({"error": "File not found"}), 404

    # The 'open' action LAUNCHES the file with its default handler (opening a
    # PDF in Preview is the intended use), so a script/executable placed in the
    # vault/Zotero tree would EXECUTE. Reveal (-R) only highlights in Finder and
    # is always safe. A DENYLIST here failed open (2026-07-20 audit finding 7):
    # it missed `.tool`/`.fish`/`.csh`/`.ksh` and, critically, EXTENSION-LESS
    # Mach-O binaries and scripts, which LaunchServices happily runs. This is now
    # a fail-closed ALLOWLIST of non-executable document/media types — the only
    # things a "reveal-open" legitimately targets (this route's own callers only
    # ever pass a PDF); anything else (unknown or no extension) is refused and
    # the user reveals it in Finder instead.
    _LAUNCH_ALLOW_EXTS = {
        ".pdf", ".txt", ".md", ".markdown", ".rtf", ".csv", ".json", ".bib",
        ".tex", ".xml", ".html", ".htm",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".tiff",
        ".tif", ".bmp", ".svg",
        ".epub", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".odt",
        ".mp3", ".wav", ".m4a", ".mp4", ".mov", ".m4v",
    }
    if action == "open" and target.suffix.lower() not in _LAUNCH_ALLOW_EXTS:
        return jsonify({
            "error": "Refusing to launch this file type (only document/media "
                     "files can be opened); reveal it in Finder instead."
        }), 400

    args = ["open"]
    if action == "reveal":
        args.append("-R")
    args.append(str(target))
    try:
        run_open(args)  # noqa: S603,S607
    except OSError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({"ok": True, "action": action})


# Re-export so tests can patch the module-level singleton if needed.
__all__ = ["audit_bp"]


def _pdf_projection_summary(settings):
    from accession import projection
    try:
        return {k:v for k,v in projection.read(settings).items() if k != 'rows'}
    except (OSError, ValueError, sqlite3.Error):
        return dict(counts={}, freshness=dict(available=False, fresh=False,
                    error='PDF reconciliation unavailable; scan totals are historical.'))


def _report_reply(name, settings, payload):
    """A report's 200 response, with the PDF observation provenance only for
    the reports that render it (``PDF_PROJECTION_REPORTS``).

    CLI proof run 2026-09-23 (C3): an ``after_request`` hook used to parse
    EVERY 200 report response, add ``pdf_reconciliation`` and re-serialise it
    — a full ``get_json`` + ``json.dumps`` of payloads as large as
    ``not_in_zotero`` (45 MB), plus the projection itself (~0.45 s) on reports
    that never show it. Now it is added to the payload before the one
    serialisation, and only where the client reads it.
    """
    if name in PDF_PROJECTION_REPORTS and settings is not None:
        payload = dict(payload, pdf_reconciliation=_pdf_projection_summary(settings))
    return jsonify(payload)


@audit_bp.route('/api/audit/library/<view>', methods=['GET'])
def api_library_view(view):
    from accession import library
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify(error='No scan results. Run a scan explicitly first.'), 404
    if view == 'links':
        from accession import dependent_links
        return jsonify(dependent_links.inspect(settings, inv, library.documents(settings, inv)['documents']))
    if view == 'checks':
        return jsonify(library.checks(settings, inv))
    if view == 'storage':
        # Phase 6 reduced (2026-09-16): a read-only control, never a conversion.
        from accession import reconciliation, storage_check
        keys = {}
        for key, record in inv.records.items():
            zot = getattr(record, 'zotero_item', None)
            if zot is not None and getattr(zot, 'item_key', None):
                keys[(getattr(zot, 'library_id', None), zot.item_key)] = key
        return jsonify(storage_check.check(settings, reconciliation.saved(settings), citation_keys=keys))
    if view in ('documents', 'overview', 'history'):
        result = library.documents(settings, inv)
        if view == 'history':
            from accession import pdf_management
            from notesync import campaigns
            with campaigns.connection() as con:
                stored = [json.loads(r[0]) for r in con.execute('SELECT body FROM campaigns')] if con else []
            result = dict(pdf_operations=pdf_management.operations(settings),
                note_campaigns=[c for c in stored if c['scope'] == str(Path(settings.vault_root).resolve())],
                freshness=result['freshness'])
        return jsonify(result)
    return jsonify(error='Unknown Library view.'), 404


def _biblio_pdf_resolver(settings):
    from tagging.writers import resolve_biblio_pdf_rel

    def resolve(rel):
        abs_path, canonical, err = resolve_biblio_pdf_rel(rel, settings)
        if err is None:
            return abs_path, canonical, None
        body = err[0]
        body = body.get_json() if hasattr(body, 'get_json') else body
        return None, None, (body.get('error') if isinstance(body, dict) else str(body))
    return resolve


@audit_bp.route('/api/audit/library/bibliography/plan', methods=['POST'])
def api_library_bibliography_plan():
    from accession import bibliography_campaign
    data = request.get_json(silent=True) or {}
    try:
        # Validate before consulting current settings or sending any content.
        bibliography_campaign.validate(data)
        settings = load_settings()
        # A record may name its PDF (biblio-dir-relative); the path is validated
        # here and authenticated deterministically before any model call.
        resolve = _biblio_pdf_resolver(settings)
        pdf_paths = {}
        for record in data['corpus']:
            if isinstance(record.get('pdf'), str):
                abs_path, _rel, err = resolve(record['pdf'])
                if err is not None:
                    return jsonify(error=f"{record['id']}: {err}"), 400
                pdf_paths[record['id']] = abs_path
        return jsonify(bibliography_campaign.prepare(data, str(settings.vault_root.resolve()), load_config(),
                                                     pdf_paths=pdf_paths or None))
    except (ValueError, AuditConfigError) as exc:
        return jsonify(error=str(exc)), 400


@audit_bp.route('/api/audit/library/bibliography/authenticate', methods=['POST'])
def api_library_bibliography_authenticate():
    """Deterministic PDF ⇄ record authentication: no model, no network, no write."""
    from accession import bibliography_campaign
    data = request.get_json(silent=True) or {}
    try:
        settings = load_settings()
        return jsonify(reports=bibliography_campaign.authenticate_records(data.get('records'), _biblio_pdf_resolver(settings)),
                       applicable=False, independent_of_catalogue=True)
    except (ValueError, AuditConfigError) as exc:
        return jsonify(error=str(exc)), 400


@audit_bp.route('/api/audit/library/bibliography/<identifier>/apply', methods=['POST'])
def api_library_bibliography_apply(identifier):
    """Write explicitly approved fields; closed in production (501) pending
    the executor's separate validation. Shares the campaign's flock."""
    from accession import metadata_executor
    from notesync import campaigns
    data = request.get_json(silent=True) or {}
    if data.get('confirm') is not True:
        return jsonify(error='Confirm the exact approved field writes.'), 400
    if not metadata_executor.application_available():
        return jsonify(error='Metadata application is closed pending its separate validation.',
                       reason='executor_validation_required', applicable=False), 501
    try:
        settings = load_settings()
    except AuditConfigError as exc:
        return jsonify(error=str(exc)), 400
    with campaigns.recovery_hold(identifier) as held:
        if not held:
            return jsonify(error='Another operation on this campaign is running; inspect after it finishes.'), 409
        try:
            return jsonify(metadata_executor.apply(identifier, str(settings.vault_root.resolve()), load_config(),
                                                   data.get('decisions'), vault_root=settings.vault_root, confirm=True))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400


@audit_bp.route('/api/audit/library/bibliography/<identifier>/decisions', methods=['POST'])
def api_library_bibliography_decisions(identifier):
    from accession import bibliography_campaign
    data = request.get_json(silent=True) or {}
    if data.get('confirm') is not True:
        return jsonify(error='Confirm the exact selected decisions.'), 400
    try:
        settings = load_settings()
        return jsonify(bibliography_campaign.save_decisions(identifier, str(settings.vault_root.resolve()), data.get('decisions')))
    except (ValueError, AuditConfigError) as exc:
        return jsonify(error=str(exc)), 400
