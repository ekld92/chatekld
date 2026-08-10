"""Library Audit HTTP endpoints.

Every route requires the ``X-Requested-With: ChatEKLD`` header check via
:func:`api.security.origin_is_local`. No endpoint triggers a scan as a
side effect of a GET — only ``POST /api/audit/scan`` does.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file

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
    finder_tag_drift as r_finder_tag_drift,
    note_tag_drift as r_note_tag_drift,
    read_unzoterod as r_read_unzoterod,
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
    "audit_drift_include_parent_tags",
    "audit_near_dup_ratio",
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

    if update:
        save_config(update)
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
    pair_id = coerce_string_max_len(data.get("pair_id"), 4096)
    if not pair_id or any(c in pair_id for c in ("\x00", "\n", "\r")):
        return jsonify({"error": "Invalid pair_id"}), 400
    from audit.engine import dup_decisions as eng_dup_decisions
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
    started, msg = audit_manager.start_scan(
        count_annotations=count_annotations if count_annotations is not None else True,
        include_duplicates=(
            include_duplicates if include_duplicates is not None else True
        ),
        include_near_duplicates=include_near if include_near is not None else True,
        pdf_text_provider=_build_pdf_text_provider(),
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
        text = (settings.vault_root / rel).read_text(encoding="utf-8")
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
        return mapping or None
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
        return jsonify({"error": "No scan results. Run a scan first."}), 404

    if name == "note_tag_drift":
        include_parent = _drift_include_parent_tags()
        rows = r_note_tag_drift.find_drift(
            inv,
            include_parent_tags=include_parent,
            synonyms=_drift_synonyms(settings),
        )
        return jsonify({
            "name": name,
            "rows": serialize.report_note_tag_drift(rows),
            "include_parent_tags": include_parent,
        })
    if name == "finder_tag_drift":
        rows = r_finder_tag_drift.find_drift(
            inv, synonyms=_drift_synonyms(settings))
        return jsonify({"name": name, **serialize.report_finder_tag_drift(
            rows, settings, vocab_id_for=_vocab_id_resolver(settings))})
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
        rep = r_tag_hygiene.find(inv, vocab)
        return jsonify({"name": name, **serialize.report_tag_hygiene(rep)})
    if name == "ambiguous_pdfs":
        # Read-only list served straight off the cached bridge result; the UI
        # resolves each ambiguity by POSTing a pick to /api/audit/mapping.
        return jsonify({"name": name, **serialize.report_ambiguous_pdfs(inv, settings)})
    if name == "skipped_pdfs":
        return jsonify({"name": name, **serialize.report_skipped_pdfs(inv, settings)})
    if name == "near_duplicates":
        near, _ = audit_manager.get_near_duplicates()
        if near is None:
            return jsonify({"error": "Near-duplicate scan was skipped on the last run."}), 404
        return jsonify({"name": name, **serialize.report_near_duplicates(near, settings)})
    # duplicates
    duplicates, _ = audit_manager.get_duplicates()
    if duplicates is None:
        return jsonify({"error": "Duplicate scan was skipped on the last run."}), 404
    return jsonify({"name": name, **serialize.report_duplicates(duplicates, settings)})


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
    return audit_search.build_corpus(
        inv, settings, vocab=vocab, duplicates=duplicates,
        near_duplicates=near,
        drift_include_parent_tags=include_parent,
        drift_synonyms=synonyms,
    )


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
    with _SEARCH_CACHE_LOCK:
        _SEARCH_CACHE = None
        _SEARCH_BUILD_ERROR = None


# Release the corpus (and its Inventory refs) the moment a scan result is
# replaced or cleared, not lazily on the next search (finding 4).
audit_manager.register_result_listener(_clear_search_cache)


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
    return jsonify(audit_search.search(corpus, q))


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


@audit_bp.route("/api/audit/mapping", methods=["POST"])
def api_audit_mapping():
    """Add a manual PDF<->bib override.

    Body shape::

        {"pdf": "Z_attachments/biblio_articles/smith_2020.pdf",
         "citation_key": "smith2020"}        # add a match
        {"pdf": "...",  "no_match": true}     # add to no_match list
        {"pdf": "...",  "clear": true}        # undo: un-curate this PDF

    ``clear`` is the triage deck's undo: the other two actions can only
    *replace* a decision with the other kind, never return the PDF to its
    un-curated state so the next scan re-derives it.

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
    clear = coerce_bool(data.get("clear"))
    citation_key = coerce_non_empty_string(data.get("citation_key"), max_len=128)

    # NOTE (2026-07-20 audit finding 2): the per-POST hash refresh no longer
    # re-hashes EVERY curated entry. `add_match`/`add_no_match` already store the
    # written PDF's current hash, and the full re-anchor of *other* entries moved
    # to scan time (audit/manager._run) — running it on every mapping POST was
    # O(curated) full-file hashes per triage click, so a deck session's Assign
    # latency grew with the whole curation set. `clear` needs no hash at all.
    if clear:
        # Checked before no_match/citation_key so an undo body carrying a
        # leftover key from the request it reverses can't be read as a re-add.
        changed = eng_bridge.clear_curation(
            settings.mapping_file, pdf_abs, settings.vault_root
        )
        return jsonify({
            "ok": True, "action": "clear", "pdf": pdf_rel, "changed": changed,
        })

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
        subprocess.Popen(args)  # noqa: S603,S607
    except OSError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({"ok": True, "action": action})


# Re-export so tests can patch the module-level singleton if needed.
__all__ = ["audit_bp"]
