"""Library Audit HTTP endpoints.

Every route requires the ``X-Requested-With: ChatEKLD`` header check via
:func:`api.security.origin_is_local`. No endpoint triggers a scan as a
side effect of a GET — only ``POST /api/audit/scan`` does.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

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
    note_tag_drift as r_note_tag_drift,
    read_unzoterod as r_read_unzoterod,
    unread_unzoterod as r_unread_unzoterod,
    zotero_no_pdf as r_zotero_no_pdf,
    zotero_unread as r_zotero_unread,
)
from audit.manager import audit_manager
from audit import serialize

audit_bp = Blueprint("audit", __name__)

# Per-key validation rules for the audit settings.  Subpaths are POSIX
# relative paths inside the vault; absolute paths point at the Zotero
# data dir.  A short path string is preferred so a user pasting a long
# vault path does not accidentally land in the wrong field.
_REL_PATH_RE = re.compile(r"[A-Za-z0-9 _.\-/]{1,255}")
_PATH_MAX_LEN = 1024
_PREFIX_RE = re.compile(r"[A-Za-z0-9_.\-]{0,32}")
_THRESHOLD_MIN, _THRESHOLD_MAX = 0, 10_000

_REPORT_NAMES = frozenset({
    "note_tag_drift",
    "unread_unzoterod",
    "zotero_unread",
    "read_unzoterod",
    "zotero_no_pdf",
    "duplicates",
})

_AUDIT_KEYS = (
    "audit_attachments_subdir",
    "audit_biblio_articles_subdir",
    "audit_zotero_notes_subdir",
    "audit_master_bib_path",
    "audit_zotero_sqlite",
    "audit_zotero_storage",
    "audit_biblio_skip_prefix",
    "audit_annotations_read_threshold",
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
}


def _normalise_rel_path(raw: Any) -> str | None:
    """Coerce *raw* to a vault-relative POSIX path; reject traversal."""
    s = coerce_string_max_len(raw, _PATH_MAX_LEN)
    if s is None or not s:
        return None
    # Reject absolute paths and ``..`` traversal — the path is interpreted
    # under the vault root by the engine.
    if s.startswith(("/", "\\")):
        return None
    parts = Path(s).parts
    if any(part == ".." for part in parts):
        return None
    if not _REL_PATH_RE.fullmatch(s):
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

    if update:
        save_config(update)
    return jsonify({"ok": True, "config": _audit_config_view()})


@audit_bp.route("/api/audit/status")
def api_audit_status():
    """Return the audit manager's lifecycle/progress payload (read-only poll)."""
    return jsonify(audit_manager.get_status_payload())


@audit_bp.route("/api/audit/scan", methods=["POST"])
def api_audit_scan():
    """Start a background reconciliation scan — the ONLY scan trigger.

    No other code path may call ``audit_manager.start_scan()`` (pinned by
    ``TestFlaskAppBootDoesNotScan``), so the scan is strictly manual. The body's
    ``count_annotations`` / ``include_duplicates`` are coerced to bool (defaulting
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
    started, msg = audit_manager.start_scan(
        count_annotations=count_annotations if count_annotations is not None else True,
        include_duplicates=(
            include_duplicates if include_duplicates is not None else True
        ),
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


@audit_bp.route("/api/audit/inventory")
def api_audit_inventory():
    """Return the cached per-citation-key inventory from the last scan.

    404 with a "Run a scan first" message when no completed scan is cached
    (also the post-reset empty state) — there is no implicit scan on read.
    """
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "No scan results. Run a scan first."}), 404
    return jsonify({
        "summary": serialize.inventory_summary(inv),
        "records": serialize.inventory_records(inv, settings),
    })


@audit_bp.route("/api/audit/reports/<name>")
def api_audit_report(name: str):
    """Build and return one of the six reconciliation reports by name.

    The ``<name>`` path segment is allow-listed against ``_REPORT_NAMES`` (404
    otherwise) and dispatched to the matching report builder over the cached
    inventory (404 if no scan is cached). The annotation-based reports
    (``unread_unzoterod`` / ``read_unzoterod``) serve from the scan's precomputed
    ``unmapped_annotations`` to avoid re-opening thousands of PDFs on this
    request thread, with a lazy per-path disk fallback. ``duplicates`` 404s if
    that phase was skipped on the last run.
    """
    if name not in _REPORT_NAMES:
        return jsonify({"error": "Unknown report"}), 404
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "No scan results. Run a scan first."}), 404

    if name == "note_tag_drift":
        return jsonify({
            "name": name,
            "rows": serialize.report_note_tag_drift(r_note_tag_drift.find_drift(inv)),
        })
    if name == "unread_unzoterod":
        # Serve from the annotation counts the scan precomputed for unmapped
        # PDFs (Inventory.unmapped_annotations) instead of opening thousands
        # of PDFs on this request thread.  find() falls back to a lazy disk
        # read for any path absent from the dict (empty when the scan ran
        # with count_annotations=False or was cancelled mid-phase), so this
        # is an optimisation, never a correctness dependency.
        rep = r_unread_unzoterod.find(
            inv, settings, annotations=inv.unmapped_annotations
        )
        return jsonify({
            "name": name,
            **serialize.report_unread_unzoterod(rep, settings),
        })
    if name == "zotero_unread":
        rep = r_zotero_unread.find(inv)
        return jsonify({"name": name, **serialize.report_zotero_unread(rep)})
    if name == "read_unzoterod":
        # Same precomputed-cache + lazy-fallback contract as unread_unzoterod
        # above; both reports iterate the same unmapped-PDF set.
        rep = r_read_unzoterod.find(
            inv, settings, annotations=inv.unmapped_annotations
        )
        return jsonify({
            "name": name,
            **serialize.report_read_unzoterod(rep, settings),
        })
    if name == "zotero_no_pdf":
        rep = r_zotero_no_pdf.find(inv)
        return jsonify({"name": name, **serialize.report_zotero_no_pdf(rep)})
    # duplicates
    duplicates, _ = audit_manager.get_duplicates()
    if duplicates is None:
        return jsonify({"error": "Duplicate scan was skipped on the last run."}), 404
    return jsonify({"name": name, **serialize.report_duplicates(duplicates, settings)})


@audit_bp.route("/api/audit/mapping", methods=["POST"])
def api_audit_mapping():
    """Add a manual PDF<->bib override.

    Body shape::

        {"pdf": "Z_attachments/biblio_articles/smith_2020.pdf",
         "citation_key": "smith2020"}        # add a match
        {"pdf": "...",  "no_match": true}     # add to no_match list

    The path is interpreted relative to the configured vault root.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400

    pdf_rel = _normalise_rel_path(data.get("pdf"))
    if not pdf_rel:
        return jsonify({"error": "Invalid pdf path"}), 400

    try:
        settings = load_settings()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400

    pdf_rel_resolved = resolve_under_root(pdf_rel, settings.vault_root)
    if not pdf_rel_resolved:
        return jsonify({"error": "pdf path escapes vault root"}), 400
    pdf_abs = settings.vault_root / pdf_rel_resolved

    no_match = coerce_bool(data.get("no_match"))
    citation_key = coerce_non_empty_string(data.get("citation_key"), max_len=128)

    if no_match:
        eng_bridge.add_no_match(settings.mapping_file, pdf_abs, settings.vault_root)
        return jsonify({"ok": True, "action": "no_match", "pdf": pdf_rel})

    if not citation_key:
        return jsonify({"error": "citation_key required when no_match is not set"}), 400

    eng_bridge.add_match(
        settings.mapping_file, pdf_abs, citation_key, settings.vault_root
    )
    return jsonify({
        "ok": True,
        "action": "match",
        "pdf": pdf_rel,
        "citation_key": citation_key,
    })


@audit_bp.route("/api/audit/reveal", methods=["POST"])
def api_audit_reveal():
    """Open a local file in Finder / the default app, or jump to a Zotero item.

    Three modes:

    - ``{"path": "<abs>"}`` reveals the file in Finder (``open -R``)
    - ``{"open": "<abs>"}`` opens the file with the default app (``open``)
    - ``{"zotero_key": "<key>"}`` opens ``zotero://select/library/items/<key>``

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
        url = f"zotero://select/library/items/{zotero_key}"
        try:
            subprocess.Popen(["open", url])  # noqa: S603,S607
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

    args = ["open"]
    if action == "reveal":
        args.append("-R")
    args.append(str(target))
    try:
        subprocess.Popen(args)  # noqa: S603,S607
    except OSError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({"ok": True, "action": action})


# Re-export so tests can patch the module-level singleton if needed.
__all__ = ["audit_bp"]
