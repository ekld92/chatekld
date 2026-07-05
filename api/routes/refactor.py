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
import difflib
import os
import threading
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from api.security import sanitise_error_msg
from api.sse import run_sse_worker
from api.validators import (
    coerce_bool,
    coerce_enum,
    coerce_int_in_range,
    coerce_string_max_len,
)
from core.config import load_config
from core.paths import resolve_under_root
from core.constants import (
    SSE_SINGLE_SHOT_FLOOR_S,
    SSE_STALL_MARGIN_S,
    VAULT_IMAGE_EXTS,
    VAULT_MD_EXTS,
)
from rag.vault import obsidian_manager

from refactor import apply as apply_mod
from refactor import archive as archive_mod
from refactor import format_fix as format_fix_mod
from refactor import extract, flags as flags_mod, ignore, journal, review as review_mod
from refactor import llm_apply as llm_apply_mod
from refactor import llm_edit as llm_edit_mod
from refactor import pdfref as pdfref_mod
from refactor import sections as sections_mod
from refactor import staging as staging_mod
from refactor.plan import analyze_one, build_plan
from refactor.result import sha256_bytes

refactor_bp = Blueprint("refactor", __name__)

# Index states (mirror deck.py) — used only to warn, never to block.
_USABLE_STATES = {"done", "paused_partial"}
_IN_PROGRESS_STATES = {"running", "scanning", "embedding", "paused", "paused_scan"}

_SCOPE_MAX = 1024
_REL_MAX = 4096
_INSTRUCTION_MAX = 4000  # free-prompt custom-edit instruction cap
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


def _llm_action_deadline_s(cfg: dict) -> float:
    """Wall-clock bound for a single on-demand LLM action (rewrite/custom/…/review).

    Same model as the SSE routes' consumer timeout: the user-facing
    ``agent_wall_clock_s`` cap, floored at ``SSE_SINGLE_SHOT_FLOOR_S`` so a slow
    first token on a *healthy* local model is never cut off, plus
    ``SSE_STALL_MARGIN_S`` so the bound sits just OUTSIDE a legitimate call. Parsed
    defensively so a hand-edited config can't crash the route.
    """
    cap = coerce_int_in_range(cfg.get("agent_wall_clock_s"), 1, 3600)
    if cap is None:  # missing / malformed hand-edited config → hard default
        cap = 300
    return float(max(cap, SSE_SINGLE_SHOT_FLOOR_S) + SSE_STALL_MARGIN_S)


# Item 2.3 single-flight registry: one live daemon per LLM action. Guarded by
# its own mutex (never held across a join or an LLM call).
_LLM_ACTION_MU = threading.Lock()
_LLM_ACTION_INFLIGHT: dict[str, threading.Thread] = {}


def _run_llm_action_bounded(action: str, fn, cfg: dict):
    """Run an LLM action off the request thread with a wall-clock bound.

    ``fn(should_cancel)`` runs on a daemon thread; *should_cancel* is a
    zero-arg callable the action polls to abort early.

    Why: the on-demand refactor LLM endpoints (rewrite / custom-edit / summarize-pdf
    / chart / review-note) used to run their fully-consumed ``stream_chat_messages``
    loop INLINE on the Waitress worker thread, holding a pool slot (of 32) for the
    entire generation with NO timeout — so a wedged local model (default
    ``local_request_timeout_s=0`` ⇒ unbounded) could pin that slot indefinitely,
    unlike every other LLM path which streams through a daemon-worker + bounded
    consumer. This helper restores parity: ``fn`` runs on a daemon thread and the
    request thread ``join``s with ``_llm_action_deadline_s``.

    Safety / semantics preserved:
    * The LLM call (and its ``_LLM_LOCK`` acquisition inside ``llm_edit._run`` /
      ``review``) run on the DAEMON thread, so the request thread never touches the
      lock — a lingering wedged call blocks only later daemon workers, never a
      request thread.
    * On timeout the request thread is freed (``timed_out=True``); the daemon thread
      keeps running until the underlying provider call returns on its own (bounded
      by ``local_request_timeout_s`` / the provider timeout), then exits — exactly
      the accepted trade-off the SSE routes document. Nothing is written by any of
      these actions, so an abandoned-but-still-running call has no side effect.
    * Exceptions raised by ``fn`` are re-raised on the request thread, so the
      caller's existing ``try/except`` (review-note's 500 path) still behaves
      identically. ``llm_edit`` actions never raise (they return an ``error`` dict),
      so their handlers see the unchanged result object.

    Item 2.3 additions (improvement plan 2026-07-04):

    * ``fn`` now receives a ``should_cancel`` callable. On timeout this helper
      sets the cancel event BEFORE returning 504, and the daemon (which polls
      it before/after taking ``LOCAL_MODEL_LOCK`` and per streamed token)
      aborts instead of running its full dead generation. Pre-fix, each 504'd
      request left a daemon running to completion, and every client retry
      queued ANOTHER daemon on the lock — the pile-up.
    * **Per-action single-flight**: while a previous daemon for the same
      *action* is still alive (wedged transport — the one case token-polling
      can't stop), a new request is REFUSED (``busy=True`` → the route 429s)
      instead of stacking another thread onto the lock. Bound: at most one
      live daemon per action, so the worst-case backlog is the action count
      (5), not one per retry. The registry entry is removed by the daemon's
      ``finally``, so a finished/aborted worker frees its slot even if the
      client vanished.

    Safe: the happy path (fn returns before the deadline) is byte-identical;
    exceptions still re-raise on the request thread. Invariant (pinned by
    ``test_llm_action_single_flight``/``test_llm_action_cancel_propagates``):
    at most one live daemon per action, and a timed-out action's daemon
    observes cancellation.

    Returns ``(result, timed_out, busy)``.
    """
    box: dict = {}
    cancel = threading.Event()

    def _worker() -> None:
        try:
            box["result"] = fn(cancel.is_set)
        except BaseException as exc:  # noqa: BLE001 — ferried back to the request thread
            box["exc"] = exc
        finally:
            with _LLM_ACTION_MU:
                if _LLM_ACTION_INFLIGHT.get(action) is threading.current_thread():
                    del _LLM_ACTION_INFLIGHT[action]

    t = threading.Thread(target=_worker, daemon=True)
    with _LLM_ACTION_MU:
        prev = _LLM_ACTION_INFLIGHT.get(action)
        if prev is not None and prev.is_alive():
            return None, False, True
        _LLM_ACTION_INFLIGHT[action] = t
    t.start()
    t.join(timeout=_llm_action_deadline_s(cfg))
    if t.is_alive():
        # Free the client (504) AND tell the daemon to stop at its next poll
        # point — the propagation half of the fix.
        cancel.set()
        return None, True, False
    if "exc" in box:
        raise box["exc"]
    return box.get("result"), False, False


def _vault_root_or_error():
    """Return ``(vault_root_str, None)`` or ``(None, (json, status))``."""
    vault_path = obsidian_manager.get_vault_path()
    if not vault_path:
        return None, (jsonify({"error": "Set the Obsidian vault path first."}), 400)
    real = os.path.realpath(os.path.expanduser(vault_path))
    if not os.path.isdir(real):
        return None, (jsonify({"error": "Configured vault path is not a directory."}), 400)
    return real, None


def _resolve_scope(raw, vault_root: str):
    """Validate a vault-relative scope sub-folder; return posix rel or None."""
    return resolve_under_root(
        raw, vault_root, must_be_dir=True, deny_root=True, max_len=_SCOPE_MAX
    )


def _abs_to_scope(abs_path, vault_root: str):
    """Convert an absolute path (e.g. from the native folder picker) to a
    vault-relative scope sub-folder, or None if it is the vault root itself or
    sits outside the vault. Keeps the single-sub-folder lock identical to manual
    entry — the picker is a convenience, not a way around ``_resolve_scope``."""
    if not isinstance(abs_path, str) or not abs_path:
        return None
    return resolve_under_root(
        abs_path, vault_root, must_be_dir=True, deny_root=True
    )


def _resolve_image_rel(raw, vault_root: str):
    """Validate a vault-relative image path; return canonical posix rel or None.

    Not scope-locked: attachments live in a central folder outside the refactor
    scope, so any image **under the vault root** is a legitimate read target.
    """
    return resolve_under_root(
        raw, vault_root, must_exist=True, must_be_file=True,
        exts=VAULT_IMAGE_EXTS, max_len=_REL_MAX
    )


def _resolve_pdf_rel(raw, vault_root: str):
    """Validate a vault-relative ``.pdf`` path that exists; canonical posix or None.

    Like ``_resolve_image_rel`` but for PDFs (the summary action reads a PDF's
    cached text). Not scope-locked: a note's attachments live vault-wide.
    """
    return resolve_under_root(
        raw, vault_root, must_exist=True, must_be_file=True,
        exts={".pdf"}, max_len=_REL_MAX
    )


def _resolve_ignore_rel(raw, vault_root: str):
    """Validate an image rel-path for the ignore-list (shape + under-root only).

    Unlike ``_resolve_image_rel`` this does **not** require the file to exist:
    a user may un-ignore (or pre-ignore) a path whose bytes are not present
    locally — e.g. a dataless iCloud placeholder, or an entry left over after a
    file moved. Traversal / absolute / NUL / non-image-extension are still
    rejected, and the resolved real path must still stay under the vault root.
    """
    return resolve_under_root(
        raw, vault_root, exts=VAULT_IMAGE_EXTS, max_len=_REL_MAX
    )


def _resolve_scope_note_rel(raw, vault_root: str, scope: str):
    """Validate a vault-relative ``.md`` note path that MUST live under *scope*.

    The Phase 2 writers only ever rewrite notes inside the approved sub-folder, so
    a note path is scope-locked here (unlike images, which live vault-wide). Rejects
    traversal / absolute / NUL, non-``.md``, and any realpath outside
    ``<vault>/<scope>``. Does not require the file to exist on disk (the apply layer
    handles a vanished note as a per-note skip).

    *raw* is VAULT-relative (the wire contract every route + the JS client use),
    so it resolves against *vault_root* and the scope lock is a prefix check on
    the resolved rel — resolving against ``<vault>/<scope>`` instead (the first
    4.2 migration's mistake) doubled the scope segment and 400'd every
    scope-locked endpoint. Pinned by test_scope_note_rel_validator_rejects_escapes."""
    rel = resolve_under_root(raw, vault_root, exts=VAULT_MD_EXTS, max_len=_REL_MAX)
    if not rel:
        return None
    if not rel.startswith(scope + "/"):
        return None
    return rel


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

    # Scope-wide preamble-strip default — config only (no body override), so the
    # plan and the apply writer resolve it from the same source (WYSIWYG-safe).
    strip_default = bool(cfg.get("refactor_strip_preamble_default"))

    def _refactor_worker(put, cancel):
        try:
            result = build_plan(
                vault_root, scope, on_event=put, stop=cancel.is_set,
                strip_default=strip_default,
            )
            if not cancel.is_set():
                put({"refactor": result.summary_frame()})
        except Exception as exc:  # noqa: BLE001 — surface as an SSE error
            if not cancel.is_set():
                put({"error": sanitise_error_msg(exc)})

    # Preflight: warn (not block) about index state — the plan reuses the
    # on-disk image cache, which a partial/paused index leaves incomplete.
    preflight_msgs = []
    try:
        state = obsidian_manager.get_status()
    except Exception:
        state = ""
    if state in _IN_PROGRESS_STATES:
        preflight_msgs.append('Indexing is in progress — image descriptions may be incomplete; misses show as “not extracted”.')
    elif state not in _USABLE_STATES:
        preflight_msgs.append('No fully-built vault index detected — many images may show as “not extracted”. Index the vault for fuller coverage.')

    return run_sse_worker(
        _refactor_worker,
        consumer_timeout_s=_PLAN_STALL_TIMEOUT_S,
        preflight_msgs=preflight_msgs,
    )


@refactor_bp.route("/api/refactor/extract-image", methods=["POST"])
def api_refactor_extract_image():
    """Run ONE fresh vision pass (table or description) for a single image."""

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


@refactor_bp.route("/api/refactor/review-note", methods=["POST"])
def api_refactor_review_note():
    """Run ONE opt-in, advisory LLM prose/formatting review of a single note.

    Body: ``{rel, scope_subdir?}``. The note is scope-locked to the refactor
    sub-folder (the reviewer only ever reads a note the user is already working
    on). The review writes **nothing** (not even a cache file) — it returns a
    short advisory suggestion list the user reads and acts on manually. One LLM
    call at a time, serialized in ``review.review_note``.
    """

    vault_root, err = _vault_root_or_error()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    cfg = load_config()
    scope = _scope_or_default(data, cfg, vault_root)
    if scope is None:
        return jsonify({"error": "Invalid scope sub-folder."}), 400
    rel = _resolve_scope_note_rel(data.get("rel"), vault_root, scope)
    if rel is None:
        return jsonify({"error": "rel must be a .md note inside the scope sub-folder."}), 400

    try:
        from pathlib import Path
        # Bounded off-thread run so a wedged local model can't pin the request
        # thread; review_note writes nothing, so an abandoned call is side-effect-free.
        res, timed_out, busy = _run_llm_action_bounded(
            "review-note",
            lambda should_cancel: review_mod.review_note(
                rel, Path(vault_root), cfg, should_cancel=should_cancel),
            cfg)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    if busy:
        return jsonify({"error": "A previous review is still running; wait for it to finish or time out."}), 429
    if timed_out:
        return jsonify({"error": "Review timed out; the model may be busy or unavailable."}), 504

    if res.get("error"):
        return jsonify({"error": sanitise_error_msg(res["error"])}), 502
    return jsonify({"ok": True, **res})


def _read_scope_note(data, cfg, vault_root):
    """Resolve + read one scope-locked ``.md`` note (read-only helper).

    Shared by the section listing + the on-demand LLM endpoints. Returns
    ``(rel, raw_bytes, None)`` on success or ``(None, None, (json, status))`` on
    any validation / read error so the caller can ``return err``.
    """
    scope = _scope_or_default(data, cfg, vault_root)
    if scope is None:
        return None, None, (jsonify({"error": "Invalid scope sub-folder."}), 400)
    rel = _resolve_scope_note_rel(data.get("rel"), vault_root, scope)
    if rel is None:
        return None, None, (jsonify({"error": "rel must be a .md note inside the scope sub-folder."}), 400)
    try:
        raw = (Path(vault_root) / rel).read_bytes()
    except OSError as exc:
        return None, None, (jsonify({"error": f"unreadable note ({type(exc).__name__})"}), 400)
    return rel, raw, None


@refactor_bp.route("/api/refactor/sections", methods=["POST"])
def api_refactor_sections():
    """List a note's heading sections for the sub-note scope selector (request f).

    Body: ``{rel, scope_subdir?}``. Read-only. Returns the document-order
    targetable sections (heading + body up to the next same-or-shallower heading,
    plus a synthetic intro block) so the UI can offer "Whole note" or a single
    section as the scope for the on-demand LLM actions.
    """
    vault_root, err = _vault_root_or_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    rel, raw, rerr = _read_scope_note(data, cfg, vault_root)
    if rerr:
        return rerr
    text = raw.decode("utf-8", errors="replace")
    secs = sections_mod.split_sections(text)
    return jsonify({
        "ok": True, "rel": rel,
        "content_sha256": sha256_bytes(raw),
        "sections": [s.to_jsonable() for s in secs],
    })


def _decode_strict_or_error(raw):
    """Decode note bytes strict-UTF-8; return ``(text, None)`` or ``(None, err)``.

    The applyable LLM actions write the whole note back, so a non-UTF-8 note is
    refused up front (apply would skip it anyway) rather than previewed lossily."""
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, (jsonify({"error": "note is not valid UTF-8; cannot edit."}), 400)


def _resolve_section_scope(data, text):
    """Resolve an optional ``section_index`` against *text*.

    Returns ``(section_or_None, body, err)``: when ``section_index`` is absent the
    body is the whole note and section is ``None``; when present and valid the
    body is that section's slice. Returns an error tuple on a bad index.
    """
    raw = data.get("section_index")
    if raw is None or raw == "":
        return None, text, None
    try:
        idx = int(raw)
    except (TypeError, ValueError):
        return None, None, (jsonify({"error": "section_index must be an integer."}), 400)
    sec = sections_mod.find_section(sections_mod.split_sections(text), idx)
    if sec is None:
        return None, None, (jsonify({"error": "Unknown section_index (re-list sections)."}), 400)
    return sec, sections_mod.slice_section(text, sec), None


def _llm_edit_truncation_guard(section, body):
    """Refuse ANY LLM edit whose target body exceeds the LLM input cap (422).

    ``llm_edit`` clips its input at ``REWRITE_MAX_CHARS``; the reformatted HEAD
    would then be staged as the proposal for the WHOLE targeted span and
    applied verbatim — silent, WYSIWYG-guard-proof loss of the tail
    (recoverable only via Restore). Refusing BEFORE the LLM call costs nothing.
    Returns an ``(response, status)`` tuple to bubble, or ``None``.

    Section-scope fix (improvement plan 2026-07-04, item 1.4 — the
    section-scope twin of the fixed 07-02 item 0.1): the previous guard
    returned ``None`` whenever a *section* was targeted, so a single section
    over the cap was clipped, the truncated head was spliced over the whole
    section span by ``replace_section``, and the sha guards *certified* the
    truncated bytes — Apply wrote real data loss. Deck augment refuses this
    exact case (deck.py's AUGMENT_MAX_SOURCE_CHARS guard); this mirrors it.
    Safe w.r.t. existing state: read-only pre-check on the request path, no
    lock held, no on-disk format touched; it can only turn a
    would-have-truncated 200 into a 422 — an under-cap body takes the exact
    path it always took. Invariant (pinned by
    test_section_llm_edit_over_cap_refused_422): no LLM-edit proposal is ever
    generated from a clipped view of the span it will replace.
    """
    if len(body) <= llm_edit_mod.REWRITE_MAX_CHARS:
        return None
    if section is None:
        return (jsonify({"error": (
            f"Note is too large for a whole-note LLM edit "
            f"({len(body):,} chars > {llm_edit_mod.REWRITE_MAX_CHARS:,}): the model "
            f"would only see (and rewrite) the beginning, silently dropping the "
            f"rest on apply. Use the section selector to edit one section at a "
            f"time instead."
        )}), 422)
    return (jsonify({"error": (
        f"This section is too large for a section-scoped LLM edit "
        f"({len(body):,} chars > {llm_edit_mod.REWRITE_MAX_CHARS:,}): the model "
        f"would only see (and rewrite) the beginning of the section, silently "
        f"dropping the rest on apply. Split the section under smaller headings "
        f"first, then edit them one at a time."
    )}), 422)


def _unified_diff(original, proposed, rel):
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True), proposed.splitlines(keepends=True),
        fromfile=rel, tofile=rel))


@refactor_bp.route("/api/refactor/note", methods=["POST"])
def api_refactor_note():
    """Re-analyze ONE note read-only and return its fresh proposal frame.

    Body: ``{rel, scope_subdir?}``. Cheap counterpart of ``/plan`` for the
    per-image OCR-inclusion panel: after the UI toggles an image's ignore /
    keep-handwritten state (via ``/ignore`` / ``/flag``), it calls this to refresh
    just this note's ``proposed`` body + hashes — no full-scope re-plan, no vision,
    no vault writes."""
    vault_root, err = _vault_root_or_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    rel, _raw, rerr = _read_scope_note(data, cfg, vault_root)
    if rerr:
        return rerr
    strip_default = bool(cfg.get("refactor_strip_preamble_default"))
    try:
        proposal = analyze_one(Path(vault_root), rel, strip_default=strip_default)
    except OSError as exc:
        return jsonify({"error": f"unreadable note ({type(exc).__name__})"}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({"ok": True, "note": proposal.frame()})


@refactor_bp.route("/api/refactor/rewrite", methods=["POST"])
def api_refactor_rewrite():
    """Generate an LLM-reformatted body for a note (or one section) — request b.

    Body: ``{rel, scope_subdir?, section_index?}``. One LLM call; the reformatted
    whole-note body is **staged server-side** (``staging``, action ``rewrite``)
    and previewed back to the UI (``proposed`` + ``diff`` + ``proposed_sha256``).
    Writes only the staging cache — the vault write is the separate, confirmed
    ``/apply-staged``. 502 on LLM error."""
    vault_root, err = _vault_root_or_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    rel, raw, rerr = _read_scope_note(data, cfg, vault_root)
    if rerr:
        return rerr
    text, derr = _decode_strict_or_error(raw)
    if derr:
        return derr
    section, body, serr = _resolve_section_scope(data, text)
    if serr:
        return serr
    guard = _llm_edit_truncation_guard(section, body)
    if guard:
        return guard

    # Bounded off-thread run (see _run_llm_action_bounded): frees the request
    # thread on a wedged model; only the staging cache is ever written, later.
    res, timed_out, busy = _run_llm_action_bounded(
        "rewrite",
        lambda should_cancel: llm_edit_mod.rewrite_formatting(
            body, cfg, should_cancel=should_cancel),
        cfg)
    if busy:
        return jsonify({"error": "A previous rewrite is still running; wait for it to finish or time out."}), 429
    if timed_out:
        return jsonify({"error": "Rewrite timed out; the model may be busy or unavailable."}), 504
    if res.get("error"):
        return jsonify({"error": sanitise_error_msg(res["error"])}), 502
    rewritten = res["text"]
    proposed = (sections_mod.replace_section(text, section, rewritten)
                if section is not None else rewritten)
    content_sha256 = sha256_bytes(raw)
    try:
        desc = staging_mod.stage(Path(vault_root), rel, content_sha256, proposed, "rewrite")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({
        "ok": True, "rel": rel, "action": "rewrite",
        "content_sha256": content_sha256, "proposed": proposed,
        "proposed_sha256": desc["proposed_sha256"],
        "diff": _unified_diff(text, proposed, rel),
        "section_index": section.index if section is not None else None,
        "model": res.get("model"), "provider": res.get("provider"),
        "truncated": res.get("truncated", False),
    })


@refactor_bp.route("/api/refactor/custom-edit", methods=["POST"])
def api_refactor_custom_edit():
    """Apply a free-form user instruction to a note (or section) — free-prompt action.

    Body: ``{rel, scope_subdir?, section_index?, instruction}``. One LLM call; the
    resulting whole-note body is **staged server-side** (``staging``, action
    ``custom``) and previewed back (``proposed`` + ``diff`` + ``proposed_sha256``).
    Writes only the staging cache — the vault write is the confirmed
    ``/apply-staged``. 502 on LLM error."""
    vault_root, err = _vault_root_or_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    rel, raw, rerr = _read_scope_note(data, cfg, vault_root)
    if rerr:
        return rerr
    text, derr = _decode_strict_or_error(raw)
    if derr:
        return derr
    instruction = coerce_string_max_len(data.get("instruction"), _INSTRUCTION_MAX)
    if not instruction or not instruction.strip():
        return jsonify({"error": "instruction is required."}), 400
    section, body, serr = _resolve_section_scope(data, text)
    if serr:
        return serr
    guard = _llm_edit_truncation_guard(section, body)
    if guard:
        return guard

    # Bounded off-thread run (see _run_llm_action_bounded): frees the request
    # thread on a wedged model; only the staging cache is ever written, later.
    res, timed_out, busy = _run_llm_action_bounded(
        "custom-edit",
        lambda should_cancel: llm_edit_mod.custom_edit(
            body, instruction, cfg, should_cancel=should_cancel),
        cfg)
    if busy:
        return jsonify({"error": "A previous edit is still running; wait for it to finish or time out."}), 429
    if timed_out:
        return jsonify({"error": "Edit timed out; the model may be busy or unavailable."}), 504
    if res.get("error"):
        return jsonify({"error": sanitise_error_msg(res["error"])}), 502
    edited = res["text"]
    proposed = (sections_mod.replace_section(text, section, edited)
                if section is not None else edited)
    content_sha256 = sha256_bytes(raw)
    try:
        desc = staging_mod.stage(Path(vault_root), rel, content_sha256, proposed, "custom")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({
        "ok": True, "rel": rel, "action": "custom",
        "content_sha256": content_sha256, "proposed": proposed,
        "proposed_sha256": desc["proposed_sha256"],
        "diff": _unified_diff(text, proposed, rel),
        "section_index": section.index if section is not None else None,
        "model": res.get("model"), "provider": res.get("provider"),
        "truncated": res.get("truncated", False),
    })


@refactor_bp.route("/api/refactor/pdf-refs", methods=["POST"])
def api_refactor_pdf_refs():
    """List a note's resolvable PDF embeds for the summary action — request c.

    Body: ``{rel, scope_subdir?}``. Read-only. Returns ``{pdfs: [{target,
    rel_path, line, cached}]}`` (``cached`` ⇒ extracted text already exists)."""
    vault_root, err = _vault_root_or_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    rel, raw, rerr = _read_scope_note(data, cfg, vault_root)
    if rerr:
        return rerr
    text = raw.decode("utf-8", errors="replace")
    from refactor.resolver import get_file_index, excluded_dirs
    root = Path(vault_root)
    # Reuse the cached whole-vault index (warmed by the note's plan run) instead
    # of a fresh full-vault rglob per PDF-refs request; only the link_index half
    # is needed here. See resolver's cache notes for the staleness guarantees.
    _name_index, link_index = get_file_index(root, excluded_dirs(root))
    pdfs = pdfref_mod.list_pdf_refs(text, root / rel, root, link_index)
    return jsonify({"ok": True, "rel": rel, "pdfs": pdfs})


def _summary_callout(basename, bullets_text):
    """Build a ``> [!summary]`` callout (list of lines joined) for a PDF summary."""
    head = f"> [!summary] Résumé du PDF ({basename}) — relire avant d’appliquer"
    body = bullets_text.strip().splitlines() or [""]
    return "\n".join([head] + [("> " + ln).rstrip() for ln in body])


def _insert_after_line(text, lineno, block):
    """Insert *block* (preceded by a blank line) right after 1-based *lineno*."""
    lines = text.split("\n")
    out = []
    for i, ln in enumerate(lines, start=1):
        out.append(ln)
        if i == lineno:
            out.append("")
            out.extend(block.split("\n"))
    return "\n".join(out)


@refactor_bp.route("/api/refactor/summarize-pdf", methods=["POST"])
def api_refactor_summarize_pdf():
    """Summarize an attached PDF into bullets, inlined as a callout — request c.

    Body: ``{rel, scope_subdir?, pdf_rel}``. Reads the PDF's cached text, runs one
    LLM summary, inlines a ``> [!summary]`` callout beneath the PDF embed, and
    **stages** the resulting whole-note body (action ``summarize_pdf``) for a
    later confirmed ``/apply-staged``. Writes only the staging cache. 502 on LLM
    error."""
    vault_root, err = _vault_root_or_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    rel, raw, rerr = _read_scope_note(data, cfg, vault_root)
    if rerr:
        return rerr
    text, derr = _decode_strict_or_error(raw)
    if derr:
        return derr
    pdf_rel = _resolve_pdf_rel(data.get("pdf_rel"), vault_root)
    if pdf_rel is None:
        return jsonify({"error": "pdf_rel must be an existing .pdf inside the vault."}), 400

    try:
        pdf_text, _trunc = pdfref_mod.get_pdf_text(pdf_rel, Path(vault_root))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 400
    if not pdf_text.strip():
        return jsonify({"error": "No extractable text found for this PDF."}), 422

    # Bounded off-thread run (see _run_llm_action_bounded).
    res, timed_out, busy = _run_llm_action_bounded(
        "summarize-pdf",
        lambda should_cancel: llm_edit_mod.summarize_pdf(
            pdf_text, cfg, should_cancel=should_cancel),
        cfg)
    if busy:
        return jsonify({"error": "A previous summary is still running; wait for it to finish or time out."}), 429
    if timed_out:
        return jsonify({"error": "Summary timed out; the model may be busy or unavailable."}), 504
    if res.get("error"):
        return jsonify({"error": sanitise_error_msg(res["error"])}), 502

    callout = _summary_callout(os.path.basename(pdf_rel), res["text"])
    # Inline beneath the PDF embed line if we can find it; else append at EOF.
    from refactor.resolver import get_file_index, excluded_dirs
    root = Path(vault_root)
    # Cached whole-vault index (warmed by the plan / the preceding pdf-refs call)
    # rather than a second full-vault rglob in the same summarize flow.
    _name_index, link_index = get_file_index(root, excluded_dirs(root))
    refs = pdfref_mod.list_pdf_refs(text, root / rel, root, link_index)
    line = next((r["line"] for r in refs if r["rel_path"] == pdf_rel), 0)
    if line:
        proposed = _insert_after_line(text, line, callout)
    else:
        proposed = text.rstrip("\n") + "\n\n" + callout + "\n"

    content_sha256 = sha256_bytes(raw)
    try:
        desc = staging_mod.stage(root, rel, content_sha256, proposed, "summarize_pdf")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({
        "ok": True, "rel": rel, "action": "summarize_pdf", "pdf_rel": pdf_rel,
        "content_sha256": content_sha256, "proposed": proposed,
        "proposed_sha256": desc["proposed_sha256"],
        "diff": _unified_diff(text, proposed, rel),
        "summary": res["text"],
        "model": res.get("model"), "provider": res.get("provider"),
        "truncated": res.get("truncated", False),
    })


@refactor_bp.route("/api/refactor/chart", methods=["POST"])
def api_refactor_chart():
    """Generate an advisory Mermaid diagram for a note/section — request e.

    Body: ``{rel, scope_subdir?, section_index?}``. One LLM call; returns the
    ```mermaid …``` block for **display/copy only** — it is never staged and never
    written to the vault. 502 on LLM error."""
    vault_root, err = _vault_root_or_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    rel, raw, rerr = _read_scope_note(data, cfg, vault_root)
    if rerr:
        return rerr
    text = raw.decode("utf-8", errors="replace")
    section, body, serr = _resolve_section_scope(data, text)
    if serr:
        return serr
    # Bounded off-thread run (see _run_llm_action_bounded); chart is advisory,
    # never staged or written, so an abandoned call is side-effect-free.
    res, timed_out, busy = _run_llm_action_bounded(
        "chart",
        lambda should_cancel: llm_edit_mod.generate_chart(
            body, cfg, should_cancel=should_cancel),
        cfg)
    if busy:
        return jsonify({"error": "A previous chart is still running; wait for it to finish or time out."}), 429
    if timed_out:
        return jsonify({"error": "Chart timed out; the model may be busy or unavailable."}), 504
    if res.get("error"):
        return jsonify({"error": sanitise_error_msg(res["error"])}), 502
    return jsonify({
        "ok": True, "rel": rel, "mermaid": res["text"],
        "section_index": section.index if section is not None else None,
        "model": res.get("model"), "provider": res.get("provider"),
        "truncated": res.get("truncated", False),
    })


@refactor_bp.route("/api/refactor/image", methods=["GET"])
def api_refactor_image():
    """Stream a vault image's bytes (read-only) for side-by-side review."""

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


@refactor_bp.route("/api/refactor/flag", methods=["GET", "POST"])
def api_refactor_flag():
    """Read or mutate the sticky per-vault per-image flag table.

    Generalizes ``/api/refactor/ignore`` to the two per-image opt-ins that change
    the *callout body* rather than dropping the image from the counts:

    * ``strip``            — strip the descriptive preamble from this image's
      extracted-text callout (keep only the transcription);
    * ``keep_handwritten`` — force-inline a callout the handwritten auto-hide
      would otherwise suppress.

    The table is a sidecar JSON under ``BASE_DIR/obsidian_cache/refactor/`` —
    **never** the vault. ``GET`` returns the current table; ``POST {rel, flag,
    action: "add"|"remove"}`` toggles one (rel, flag) pair and returns the new
    table. Rel-paths are shape-validated (``_resolve_ignore_rel``) but not
    required to exist, so a moved/dataless image can still be toggled.
    """

    vault_root, err = _vault_root_or_error()
    if err:
        return err

    from pathlib import Path
    root = Path(vault_root)

    if request.method == "GET":
        return jsonify({"ok": True, "flags": flags_mod.list_flags(root)})

    data = request.get_json(silent=True) or {}
    rel = _resolve_ignore_rel(data.get("rel"), vault_root)
    if rel is None:
        return jsonify({"error": "Invalid image path."}), 400
    flag = coerce_enum(data.get("flag"), tuple(sorted(flags_mod.ALLOWED_FLAGS)))
    if flag is None:
        return jsonify({"error": "flag must be 'strip' or 'keep_handwritten'."}), 400
    action = coerce_enum(data.get("action"), ("add", "remove"))
    if action is None:
        return jsonify({"error": "action must be 'add' or 'remove'."}), 400

    try:
        if action == "add":
            table = flags_mod.add(root, rel, flag)
        else:
            table = flags_mod.remove(root, rel, flag)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({"ok": True, "rel": rel, "flag": flag, "action": action, "flags": table})


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
        results = apply_mod.apply_notes(
            Path(vault_root), cfg, approved, heartbeat=lambda: obsidian_manager.heartbeat(op_epoch))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)

    applied = sum(1 for r in results if r.get("status") == "applied")
    return jsonify({"ok": True, "applied": applied, "results": results})


@refactor_bp.route("/api/refactor/normalize", methods=["POST"])
def api_refactor_normalize():
    """Write the approved deterministic formatting fixes to the vault (batch).

    Body: ``{scope_subdir?, confirm: true, notes: [{rel, content_sha256,
    normalized_sha256}]}``. The second, independent Phase 2 batch action (the
    first is ``/apply``'s callout writer): each note is re-normalized server-side
    and written with a stale-diff + WYSIWYG guard; one note's failure never
    aborts the rest. Holds the op lock; 503 if indexing is in progress. Mirrors
    ``/api/refactor/apply`` field-for-field, with ``normalized_sha256`` in place
    of ``proposed_sha256``."""
    vault_root, err = _vault_root_or_error()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "Fix formatting must be explicitly confirmed (confirm: true)."}), 400
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
        nh = coerce_string_max_len(n.get("normalized_sha256"), 128)
        if rel is None or not ch or not nh:
            return jsonify({"error": f"Invalid note entry: {n.get('rel')!r}"}), 400
        approved.append({"rel": rel, "content_sha256": ch.strip(), "normalized_sha256": nh.strip()})

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
        results = format_fix_mod.apply_normalize(
            Path(vault_root), cfg, approved, heartbeat=lambda: obsidian_manager.heartbeat(op_epoch))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)

    applied = sum(1 for r in results if r.get("status") == "applied")
    return jsonify({"ok": True, "applied": applied, "results": results})


@refactor_bp.route("/api/refactor/apply-staged", methods=["POST"])
def api_refactor_apply_staged():
    """Apply one staged LLM proposal (rewrite / PDF summary) to a note.

    Body: ``{rel, scope_subdir?, action, content_sha256, proposed_sha256,
    confirm: true}``. The proposed body lives server-side in the staging cache
    (written by the matching generate endpoint); this route never accepts a body
    from the client. Stale-diff + WYSIWYG guards in ``llm_apply``. Holds the op
    lock; 503 if indexing is in progress."""
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
    rel = _resolve_scope_note_rel(data.get("rel"), vault_root, scope)
    if rel is None:
        return jsonify({"error": "rel must be a .md note inside the scope sub-folder."}), 400
    action = coerce_enum(data.get("action"), tuple(sorted(staging_mod.ALLOWED_ACTIONS)))
    if action is None:
        return jsonify({"error": "Invalid action."}), 400
    ch = coerce_string_max_len(data.get("content_sha256"), 128)
    ph = coerce_string_max_len(data.get("proposed_sha256"), 128)
    if not ch or not ph:
        return jsonify({"error": "content_sha256 and proposed_sha256 are required."}), 400

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
        result = llm_apply_mod.apply_staged_note(
            Path(vault_root), cfg, rel, ch.strip(), ph.strip(), action)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)

    return jsonify({"ok": result.get("status") == "applied", "result": result})


@refactor_bp.route("/api/refactor/archive", methods=["POST"])
def api_refactor_archive():
    """Move ONE image's full-res original out of the vault + leave a thumbnail.

    Body: ``{scope_subdir?, confirm: true, note_rel, image_rel, content_sha256}``.
    Refuses (no writes) when the image is referenced by any other note. Holds the
    op lock; 503 if indexing is in progress."""
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
        result = archive_mod.archive_image(
            Path(vault_root), cfg, scope, note_rel, image_rel, content_sha256.strip(),
            heartbeat=lambda: obsidian_manager.heartbeat(op_epoch))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)

    if result.get("ok"):
        # A successful archive moved an image OUT of the vault and added a
        # thumbnail — the file set changed, so the cached whole-vault name/link
        # index (resolver.get_file_index) is now stale. Drop this vault's entries
        # so the next plan/analyze rebuilds from disk. Cheap: just clears a dict.
        from refactor.resolver import invalidate_index_cache
        invalidate_index_cache(vault_root)
    status = 200 if result.get("ok") else (409 if result.get("shared") else 400)
    return jsonify(result), status


@refactor_bp.route("/api/refactor/manifest", methods=["GET"])
def api_refactor_manifest():
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
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    return jsonify({"ok": True, "ops": manifest.get("ops", [])})


@refactor_bp.route("/api/refactor/restore", methods=["POST"])
def api_refactor_restore():
    """Reverse one manifest op (``{op_id}``) or every applied op (``{all: true}``).

    Restores note bodies from snapshots and archived originals back to the vault,
    removing thumbnails. Holds the op lock; 503 if indexing is in progress."""
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
        # Reverted ops are now spent — prune them (and their snapshots) plus any
        # over-cap history so the manifest/`.bak` disk stays bounded.
        journal.prune(root, cfg, manifest)
        journal.save(root, cfg, manifest)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)

    reverted = sum(1 for r in results if r.get("status") == "reverted")
    if reverted:
        # A restore may have reversed an archive (moved an image back into the
        # vault + removed its thumbnail), changing the file set. Drop the cached
        # whole-vault index so the next plan/analyze sees the restored files.
        from refactor.resolver import invalidate_index_cache
        invalidate_index_cache(vault_root)
    return jsonify({"ok": True, "reverted": reverted, "results": results})
