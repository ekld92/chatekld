"""``/api/notesync/*`` — Obsidian reference notes kept in step with Zotero.

Batch E (2026-08-26). A sub-view of the Library Audit in the same sense the
Tagging tab is: it reuses the audit's cached ``Record`` join and lives in its
own blueprint so the vendored ``audit/`` tree keeps its read-only posture and
its single documented vault-write exception. The write here is not a new write
PATH either — it goes through ``vault_history/create_note.py``, which is already
journalled, confirm-gated, refuses to overwrite and is reversible from the
History and Restore UI.

Two rules the endpoints enforce, both from measurement (see ``notesync``'s
package docstring):

* **Never rewrite what the user wrote.** Originally this was "create only",
  which was the right rule under the only measurement then available. A finer
  one replaced it 2026-08-28: the note is split at its ``---`` rule
  (:func:`notesync.compare.split_note`) and only the region BELOW it — the
  Zotero reading note's territory — is ever rewritten, leaving frontmatter,
  tags and the header block byte-identical. Even then the region is rewritten
  only when it contains no words of the user's own; when it does, the note is
  named and the overwrite demands a separate explicit acknowledgement. Live
  counts at the time of writing: 1 note updatable outright, 15 carrying the
  user's writing, which is why there is no bulk update verb.
* **Never invent work.** A record whose Zotero item carries no reading note is
  in step by definition. The maintainer's rule is that an Obsidian note exists
  exactly when a reading note does, and 5,078 records have no reading note.
"""

from __future__ import annotations

import json

import dataclasses
import os
import hashlib
import logging
import re
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from flask import Blueprint, jsonify, request

from api.security import origin_is_local, sanitise_error_msg
from api.sse import online_aware_floor_s, run_sse_worker
from api.validators import coerce_bool, coerce_float_in_range, coerce_string_max_len
from audit.config import AuditConfigError, load_settings, settings_context
from audit.manager import audit_manager
from core.config import load_config
from core.constants import SSE_STALL_MARGIN_S
from notesync.compare import (
    DEFAULT_CONTAINMENT,
    REGION_BEGIN,
    compare_note,
    region_delta,
    split_note_parts,
)
from notesync import base_store, git_undo, zotero_notes
from notesync.safety import content_issues, representable
from notesync.convert import CONVERTER_ID, html_to_md, md_to_html, same_note_html
from notesync.merge import has_conflict_markers, three_way
from notesync.migrate import (
    FM_ITEM_KEY,
    TEMPLATE_HINT,
    migrate_note,
    set_frontmatter_keys,
    unmigrate_note,
)
from notesync.state import BaseRecord, classify, equivalent
from notesync.render import render_note, render_region, zotero_note_text
from vault_history.journal import _MAX_OPS as _REFACTOR_MAX_OPS

logger = logging.getLogger(__name__)

notesync_bp = Blueprint("notesync", __name__)

#: Cap on notes created in one confirmed request.
#:
#: Bounded by the REFACTOR JOURNAL's retention window, not by taste. Every
#: created note is journalled as an evictable ``create_note`` op, and
#: ``prune``'s per-call ``protect_op_ids`` exemption only shields the batch
#: from ITS OWN prune — on the next refactor write of any kind the batch
#: becomes normally evictable again. A batch larger than half the window would
#: therefore start losing its earliest undo records to later writes while the
#: confirm dialog says "Reversible from Settings → History and Restore". Refusing is
#: the honest answer, exactly as `_HARMONIZE_MAX_NOTE_OPS` does for the tagging
#: journal: a promise the journal cannot keep is worse than two batches.
_MAX_CREATE = _REFACTOR_MAX_OPS // 2

#: Manifest checkpoint cadence inside a batch (refactor/apply.py's precedent).
_JOURNAL_FLUSH_EVERY = 25


@notesync_bp.before_request
def _guard():
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    return None


def _threshold(cfg: dict) -> float:
    raw = cfg.get("notesync_containment")
    val = coerce_float_in_range(raw, 0.1, 1.0)
    return val if val is not None else DEFAULT_CONTAINMENT


#: Memoised :func:`_states` result, keyed on the IDENTITY of the cached
#: ``Inventory`` (the Zotero side of every comparison comes from it) and, per
#: note, on the file's ``(size, mtime_ns)``. Dropped wholesale by the manager's
#: result listener below, exactly as the audit search corpus is.
_STATES_CACHE: dict | None = None
_STATES_CACHE_LOCK = threading.Lock()
#: Two-way sync-row memo (sibling of ``_STATES_CACHE``). Cleared by the same
#: helper so a scan replacement or a successful write never serves stale rows.
_SYNC_ROWS_CACHE: dict | None = None


def _clear_states_cache() -> None:
    """Drop the memoised note states / sync rows (and their Inventory refs)."""
    global _STATES_CACHE, _FRESH_NOTES, _SYNC_ROWS_CACHE
    with _STATES_CACHE_LOCK:
        _STATES_CACHE = None
        _SYNC_ROWS_CACHE = None
    with _FRESH_NOTES_LOCK:
        _FRESH_NOTES = None


# Release the memo — and the Inventory reference it pins — the moment a scan
# result is replaced or cleared, not lazily on the next poll.
audit_manager.register_result_listener(_clear_states_cache)


def _note_sig(path: Path):
    """``(size, mtime_ns)`` of one note, or ``None`` when it cannot be stat'd.

    Cache key only. This is NOT the "has this note drifted?" question, which
    ``notesync/compare.py`` documents as unanswerable from a timestamp in this
    vault (the August 2026 tagging campaigns rewrote every note's frontmatter);
    it is the app-wide "has this file changed since I read it?" idiom that
    ``load_config`` and the deckgen template cache already use.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _became_symlink(path: Path) -> bool:
    """Whether a note the writer is about to replace is (now) a symlink.

    Checked with ``lstat`` UNDER the op-lock, before the re-read (audit
    remediation 2026-09-23, N8). The re-read follows a link, so a note swapped
    for a symlink to identical bytes passed the "unchanged on disk" check —
    and the atomic replace then turned the link into a regular file, writing
    into the vault what the user had pointed elsewhere. Refusing is the
    conservative answer; an unreadable path is left to the re-read's own
    error handling.
    """
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError:
        return False


class NotesFolderUnreadable(OSError):
    """The notes folder could not be listed; a plan built on it would lie."""


@notesync_bp.errorhandler(NotesFolderUnreadable)
def _notes_folder_unreadable(exc):
    """Every route that plans over the notes folder refuses the same way (N11)."""
    return jsonify({"error": str(exc), "reason": "notes_folder_unreadable"}), 503


@notesync_bp.errorhandler(base_store.StoreBusy)
def _base_store_busy(exc):
    """A base-store lock held past its bound is a busy refusal (P12), never
    an unexplained 500 or an unbounded wait on a request thread."""
    return jsonify({"error": str(exc), "reason": "store_busy"}), 503


def _note_dir_names(notes_dir) -> frozenset:
    """The notes folder's EXACT entry names, listed once per sweep.

    An unreadable folder RAISES (audit remediation 2026-09-23, N11): it used to
    answer an empty set, which reads as "no note was created since the scan"
    — so every note written since would be planned for creation again, and
    the create refused one by one. A plan built on a folder it cannot list is
    refused instead ("notes folder unreadable").
    """
    try:
        return frozenset(os.listdir(notes_dir))
    except FileNotFoundError:
        # No folder yet is a real, empty answer: nothing was created in it.
        return frozenset()
    except OSError as exc:
        raise NotesFolderUnreadable(f"The notes folder cannot be read: {exc}") from exc


def _created_since_scan(notes_dir, key: str, names: frozenset):
    """The note a record's key names on disk, when the cached scan has none.

    Matched by the EXACT directory entry name, never by a path lookup: the
    audit joins notes to records by case-sensitive stem, while an APFS volume
    answers ``is_file()`` for ``vanderx2020.md`` when only ``VanDerX2020.md``
    exists — an orphan of ANOTHER key, which a path test would have handed to
    this record and rewritten under its key (review 2026-09-21, D1/D2: it also
    blocked every git-gated sweep, since git's lookup IS case-sensitive).
    Only a regular, non-symlink file — what `create_note` refuses to overwrite.
    """
    name = f"{key}.md"
    if name not in names:
        return None
    candidate = Path(notes_dir) / name
    if candidate.is_symlink() or not candidate.is_file():
        return None
    return candidate


def _states(inv, settings, threshold: float):
    """Compare every record with a Zotero reading note. Read-only.

    # PERF (2026-09-02): the whole-library note sweep ran on every request.
    #   Before:    every GET /api/notesync/status re-read and re-compared every
    #              reference note with no memo of any kind. Measured over 1235
    #              notes (the live library's count) with a 120-word reading
    #              note each: 467 ms per request, of which only 26 ms is disk —
    #              the cost is the tokenising comparison, not the I/O the plan
    #              attributed it to. That is 467 ms of a Waitress worker per
    #              poll, and the panel polls.
    #   Change:    a module memo keyed on the cached Inventory's IDENTITY plus,
    #              per note, the file's (size, mtime_ns). A repeat call with
    #              nothing edited recomputes nothing; a note the user edited
    #              recomputes that note alone.
    #   Safe because:
    #              the Zotero half of every comparison is read from the
    #              Inventory, so it cannot change while the identity holds (a
    #              new scan replaces the object AND fires the listener that
    #              drops the memo), and the Obsidian half is re-derived the
    #              moment the file's size or mtime moves. `threshold` is part
    #              of the key, so a Settings change applies on the next poll.
    #              A note that cannot be stat'd is never cached.
    #   Preserves: "a report is a view of ONE cached scan" — the memo is keyed
    #              to that scan and dropped with it — and the unreadable-is-not-
    #              missing rule below, which still runs per computed note.
    #   Lock/thread contract:
    #              a short module lock guards the dict only; every read and
    #              comparison happens OUTSIDE it, so two concurrent pollers may
    #              duplicate work but can never block each other on I/O.
    #   Pinned by: test_notesync.py::TestStatusMemo
    """
    global _STATES_CACHE
    with _STATES_CACHE_LOCK:
        cache = _STATES_CACHE
        if (cache is not None and cache["inv"] is inv
                and cache["settings"] is settings
                and cache["threshold"] == threshold):
            prev = cache["by_key"]
        else:
            prev = {}

    notes_dir = settings.zotero_notes_dir
    out = []
    fresh: dict = {}
    dir_names = _note_dir_names(notes_dir)
    for key, rec in inv.records.items():
        z_text = zotero_note_text(rec)
        if not z_text.strip():
            continue                      # no reading note ⇒ nothing to sync
        note = rec.obsidian_note
        if note is None:
            # Same rule as `_sync_rows`: a note created since the scan is not
            # "missing" (review 2026-09-21, D3 — the Missing list kept offering
            # `[create]` for a note that existed, under a false explanation).
            created = _created_since_scan(notes_dir, key, dir_names)
            if created is not None:
                note = SimpleNamespace(path=str(created))
        raw = None
        rel = None
        sig = None
        if note is not None:
            path = Path(note.path)
            sig = _note_sig(path)
            hit = prev.get(key)
            if hit is not None and sig is not None and hit[0] == sig:
                out.append(hit[1])
                fresh[key] = hit
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
                rel = str(path.relative_to(settings.vault_root))
            except (OSError, ValueError):
                # Unreadable is NOT missing: creating over it would refuse
                # anyway (create_note never overwrites), and reporting it as
                # missing would invite exactly that.
                raw = ""
        else:
            rel = str((notes_dir / f"{key}.md").relative_to(settings.vault_root))
        state = compare_note(key, z_text, raw, note_rel=rel,
                             threshold=threshold)
        out.append(state)
        if sig is not None:
            fresh[key] = (sig, state)

    with _STATES_CACHE_LOCK:
        _STATES_CACHE = {"inv": inv, "settings": settings,
                         "threshold": threshold, "by_key": fresh}
    return out


#: Why :func:`notesync.render.render_region` could not identify a region, in
#: words. Kept beside the endpoint rather than in the pure module: the reason
#: codes are the contract, the prose is UI.
_REGION_REFUSALS = {
    "template_shape": (
        "The configured note template produces no single '---' rule, so an "
        "update cannot tell the generated header from the Zotero note. Add a "
        "rule to the template, or reconcile this note by hand."),
    "notes_not_below_rule": (
        "The configured note template puts the Zotero note ABOVE its '---' "
        "rule, so the region below it is not the reading note's. Reconcile "
        "this note by hand."),
}

#: Op kind for the region refresh. Registered in ``vault_history.journal.revert_op``
#: beside the other whole-note writers, so the existing History and Restore
#: UI reverts it with no per-kind knowledge.
_UPDATE_OP_KIND = "notesync_update"


def _sha256_text(text: str) -> str:
    """Digest of the note's UTF-8 bytes — the same currency the refactor
    writers' stale-diff and WYSIWYG guards use."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _created_note_context(settings, cfg) -> dict:
    """What a created note's frontmatter needs, computed ONCE per plan/create.

    ``ambiguous``: the vault's duplicated PDF basenames (one walk), so a
    ``pdf:`` entry is written bare or as a path exactly as ``pdf-links-status``
    expects it. ``vocab``: the controlled vocabulary, or ``None`` when
    ``_tags.md`` is unavailable — then Zotero tags are NOT copied (an unfiltered
    tag write is worse than none; today's behaviour, obs tags only).
    """
    from api.routes._audit_tagwrite import ambiguous_pdf_basenames
    from tagging.writers import _read_vocab
    vault_root = Path(settings.vault_root)
    vocab, _rel = _read_vocab(cfg, vault_root)
    return {"vault_root": vault_root, "ambiguous": ambiguous_pdf_basenames(vault_root),
            "vocab": vocab}


def _render_created_note(rec, template: str, ctx: dict, extra_tags=()) -> tuple[str, dict]:
    """``(text, notes)`` for a CREATED note — the ONE render behind
    ``/preview``, the mirror's plan digest and the write (follow-ups lot 2,
    2026-09-22).

    *notes* (audit remediation 2026-09-23, N4) says what the render could NOT
    honour, because both used to vanish silently: ``rejected_tags`` — ticked
    *extra_tags* the vocabulary gate dropped (the user asked for them, so the
    mirror reports them in ``note_tags_unwritten``); ``pdf_links`` — the
    ``set_pdf_links`` refusal reason when the ``pdf:`` list could not be
    inserted (the note is still written without it; the mirror reports it in
    ``pdf_links_unwritten``). Both are ``None``/empty on the normal path.

    Frontmatter on top of :func:`render_note`'s: ``tags:`` = the record's
    Obsidian tags ∪ its Zotero parent's tags ∪ *extra_tags*, passed through
    the tagging writers' vocabulary gate (out-of-vocab dropped; nothing added
    when the vocabulary is unavailable), and ``pdf:`` = the record's mapped
    PDFs in the exact shape ``apply-pdf-links`` writes (so the note reads
    ``current`` at once, and the rename route can rewrite it). The digest is
    over the render WITHOUT *extra_tags*: those are the tags the user ticked
    for THIS run, reviewed rather than digest-bound, and the only permitted
    difference between the reviewed text and the written bytes.
    """
    from api.routes._audit_tagwrite import expected_pdf_links, set_pdf_links
    from tagging.writers import _canonicalise_tags
    obs = sorted(getattr(rec, "obs_tags", None) or [])
    vocab = ctx.get("vocab")
    notes: dict = {"rejected_tags": [], "pdf_links": None}
    if vocab is None:
        tags = obs
    else:
        zot = list(getattr(getattr(rec, "zotero_item", None), "tags", None) or [])
        valid, _rejected = _canonicalise_tags([*obs, *zot, *list(extra_tags or ())], vocab)
        tags = sorted(valid)
        # Only the TICKED tags' refusals are reported: the record's own
        # Obsidian/Zotero tags outside the vocabulary are dropped by design
        # (they were never asked for in this run).
        _ok, notes["rejected_tags"] = _canonicalise_tags(list(extra_tags or ()), vocab)
    text = render_note(rec, template, tags=tags)
    names = expected_pdf_links(getattr(rec, "pdf_paths", None) or [],
                               ctx["vault_root"], ctx["ambiguous"])
    if names:
        linked, reason = set_pdf_links(text, names)
        if linked is not None:
            text = linked
        else:
            notes["pdf_links"] = reason or "pdf_links_refused"
    return text, notes


def _plan_update(rec, raw: str, template: str) -> tuple[dict | None, dict | None]:
    """Compute an update proposal for one note; return ``(plan, refusal)``.

    Exactly one of the two is non-None. Shared by the preview and the write so
    the bytes the user approves are the bytes that land — the preview cannot
    drift from the writer because there is one code path.
    """
    split = split_note_parts(raw)
    if split is None:
        return None, {"reason": "unparsed", "message": (
            "This note is not in the template's shape (a marker pair, or one "
            "'---' rule between the header and the Zotero note), so there is "
            "no region to replace safely. Reconcile it by hand.")}
    # Three parts, not two: a marked note's suffix holds the END marker and
    # whatever the user wrote below it, and both must land back byte-for-byte.
    prefix, region, suffix = split
    new_region, reason = render_region(rec, template,
                                       marked_target=REGION_BEGIN in raw)
    if new_region is None:
        # render_region's contract is that exactly one of the pair is set.
        # Narrowed rather than assumed: a future refusal path that forgot its
        # reason would otherwise key the message map on None and fall through
        # to the generic sentence with no way to tell which branch fired.
        code = reason or "template_shape"
        return None, {"reason": code, "message": _REGION_REFUSALS.get(
            code, "The note template's shape cannot be resolved.")}
    proposed = prefix + new_region + suffix
    # Measure against the region that will ACTUALLY replace this one, not
    # against the raw Zotero note text the read-only listing uses. The two put
    # every one of the 1,234 live notes in the same bucket today (measured
    # 2026-08-29), so this is not a behaviour change — but the listing's basis
    # is only equivalent while the template adds no words of its own around the
    # `{{notes}}` slot, and `notesync_template` is user-editable. The write is
    # the one place that must not depend on that holding.
    missing_words, extra_words = region_delta(region, new_region)
    return {
        "prefix": prefix,
        "current_region": region,
        "proposed_region": new_region,
        "proposed": proposed,
        "changed": proposed != raw,
        "missing_words": missing_words,
        "extra_words": extra_words,
        "content_sha256": _sha256_text(raw),
        "proposed_sha256": _sha256_text(proposed),
    }, None


def _resolve_for_update(data: dict):
    """Shared prologue for the two update endpoints.

    Returns ``(rec, raw, rel, settings, cfg)`` or a ``(payload, status)`` tuple
    to return. Kept together so preview and write agree on every precondition
    — a preview that resolves a note the write would refuse is a lie.
    """
    key = coerce_string_max_len(data.get("citation_key"), 512)
    if not key or any(c in key for c in ("\x00", "\n", "\r", "/", "\\")):
        return None, (jsonify({"error": "citation_key is required."}), 400)
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return None, (jsonify({"error": "Run a Library Audit scan first."}), 404)
    rec = inv.records.get(key)
    if rec is None:
        return None, (jsonify({"error": "No record for that citation key."}), 404)
    if not zotero_note_text(rec).strip():
        return None, (jsonify({
            "error": "That Zotero item carries no reading note; there is "
                     "nothing to import."}), 400)
    note = rec.obsidian_note
    if note is None:
        return None, (jsonify({
            "error": "No Obsidian note for that key — create it instead."}), 404)
    try:
        # read_bytes + explicit decode, NOT read_text: the latter applies
        # universal-newline translation, so a CRLF note would come back as LF
        # and the write would "helpfully" reformat every line the user never
        # touched. That is the same defect recorded against apply-tag-drift.
        raw_bytes = Path(note.path).read_bytes()
        raw = raw_bytes.decode("utf-8")
        rel = str(Path(note.path).relative_to(settings.vault_root))
    except UnicodeDecodeError:
        # Never lossily decode a note we may write back (read_doc_strict's rule).
        return None, (jsonify({
            "error": "That note is not valid UTF-8; refusing to rewrite it."}), 422)
    except (OSError, ValueError) as exc:
        return None, (jsonify({"error": sanitise_error_msg(exc)}), 500)
    return (rec, raw, rel, settings, load_config()), None


@notesync_bp.route("/api/notesync/status")
def api_notesync_status():
    """Which reference notes are missing, drifted, or in step. Read-only."""
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "Run a Library Audit scan first."}), 404
    cfg = load_config()
    threshold = _threshold(cfg)
    states = _states(inv, settings, threshold)
    missing = [s for s in states if s.state == "missing"]
    drifted = sorted((s for s in states if s.state == "drifted"),
                     key=lambda s: s.containment)
    # The update buckets (2026-08-28) are a DIFFERENT question from `drifted`,
    # not a refinement of it: drift asks "has this note diverged?", these ask
    # "is there something to import, and is there something of yours to lose?".
    # A note can be perfectly in step by containment and still carry 80 words
    # of the user's own writing, and a note two words short of complete is not
    # drifted at any sane threshold. Both are reported; the UI acts on these.
    def _row(s, **extra):
        return {"citation_key": s.citation_key, "note_rel": s.note_rel,
                "containment": s.containment, "zotero_chars": s.zotero_chars,
                "note_chars": s.note_chars, "missing_words": s.missing_words,
                "extra_words": s.extra_words, **extra}

    by_sync = {}
    for st in states:
        by_sync.setdefault(st.sync, []).append(st)
    # Worst first in both actionable buckets: most to import, then most at risk.
    def _rank(s):
        return (-s.missing_words, -s.extra_words, s.citation_key)

    return jsonify({
        "threshold": threshold,
        "with_reading_note": len(states),
        "ok": sum(1 for s in states if s.state == "ok"),
        # Phase 2 layout (2026-09-04): how many existing notes carry the
        # marker pair. Drives the tab's migrate / undo offer.
        "layout": {
            "marked": sum(1 for s in states if s.marked),
            "unmarked": sum(1 for s in states
                            if s.state != "missing" and not s.marked),
        },
        "missing": [
            {"citation_key": s.citation_key, "note_rel": s.note_rel,
             "zotero_chars": s.zotero_chars}
            for s in missing
        ],
        "drifted": [
            {"citation_key": s.citation_key, "note_rel": s.note_rel,
             "containment": s.containment, "zotero_chars": s.zotero_chars,
             "note_chars": s.note_chars}
            for s in drifted
        ],
        # Zotero has text this note lacks, and the note's region holds nothing
        # the user wrote — the only case an update may run without a second
        # acknowledgement.
        "updatable": [_row(s) for s in sorted(by_sync.get("update", []), key=_rank)],
        # Same, but the region also carries the user's own writing.
        "conflicts": [_row(s) for s in sorted(by_sync.get("conflict", []), key=_rank)],
        # The user annotated; Zotero has nothing new. Counted, not listed:
        # there is no action, and 32 rows of "nothing to do" is noise.
        "annotated_count": len(by_sync.get("annotated", [])),
        # Shape not recognised, so no region can be identified — listed,
        # because silence would read as "in step".
        "unparsed": [_row(s) for s in sorted(by_sync.get("unparsed", []),
                                             key=lambda s: s.citation_key)],
    })


@notesync_bp.route("/api/notesync/preview", methods=["POST"])
def api_notesync_preview():
    """The exact text a create would write, for ONE key. Writes nothing.

    Preview-before-write, like every other generated body in the app: the user
    sees the bytes rather than a description of them.
    """
    data = request.get_json(silent=True) or {}
    key = coerce_string_max_len(data.get("citation_key"), 512)
    if not key:
        return jsonify({"error": "citation_key is required."}), 400
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "Run a Library Audit scan first."}), 404
    rec = inv.records.get(key)
    if rec is None:
        return jsonify({"error": "No record for that citation key."}), 404
    cfg = load_config()
    text, _notes = _render_created_note(rec, cfg.get("notesync_template") or "",
                                        _created_note_context(settings, cfg))
    rel = str((settings.zotero_notes_dir / f"{key}.md")
              .relative_to(settings.vault_root))
    return jsonify({"citation_key": key, "rel": rel, "text": text,
                    "exists": rec.obsidian_note is not None})


def _create_notes(inv, settings, cfg, keys: list[str], *, fresh=None,
                  should_cancel=None, render_ctx=None, note_tags=None):
    """Write the missing reference notes for *keys*. ``(payload, status)``.

    Split out of :func:`api_notesync_create` (2026-09-20) so ``/mirror``'s
    first phase is this exact writer rather than a second one — the rule that
    a note is only ever CREATED, never rewritten, is enforced here and again
    by ``create_note`` itself.

    *fresh* (``{item_key: [note]}``) is TODAY's Zotero read, and when given it
    is authoritative for the reading note (library-workflow plan lot 1,
    defect 5, 2026-09-21): no fresh note → ``no_reading_note`` (deleted in
    Zotero since the scan), two → ``multiple_notes``, one → its text is what
    is rendered. The scan still owns the citation-key ↔ item JOIN; only the
    note text comes from the fresh read. Before, a record whose fresh list was
    empty fell back to the SCAN's note and was created from text Zotero no
    longer held. ``/create`` and ``/mirror`` both pass it now, so the CLI's
    create (cleanup 05m) and the interactive mirror decide from one source.

    *should_cancel* is polled before each create (defect 8). A cancel stops
    the loop, the journal checkpoint + prune below still run — they are the
    recovery boundary for the creates that DID land — and the keys never
    reached are returned in ``not_reached``, never reported as failures.

    *render_ctx* (:func:`_created_note_context`) is the mirror's OWN context,
    the one its plan digest was rendered with, so digest and written bytes
    come from one render; a standalone ``/create`` computes its own.
    *note_tags* (``{citation_key: [tags]}``) are the tags the user ticked for
    a note this call creates — merged through the vocabulary gate, never
    applied to a note that already exists (follow-ups lot 2, 2026-09-22).
    """
    template = cfg.get("notesync_template") or ""
    vault_root = Path(settings.vault_root)
    render_ctx = render_ctx or _created_note_context(settings, cfg)
    note_tags = note_tags or {}

    from rag.vault import obsidian_manager
    from vault_history import journal
    from vault_history.create_note import create_note

    op_epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not op_epoch:
        return {"error": "Indexing is in progress; try again shortly."}, 503

    results: dict[str, dict] = {}
    created = 0
    created_op_ids: set[str] = set()
    created_bases: list[dict] = []
    cancelled = False
    lock_lost = False
    not_reached: list[str] = []
    try:
        jman = journal.load(vault_root, cfg)
        for pos, key in enumerate(keys):
            if should_cancel is not None and should_cancel():
                cancelled = True
                not_reached = list(keys[pos:])
                break
            rec = inv.records.get(key)
            if rec is None:
                results[key] = {"ok": False, "reason": "no_record"}
                continue
            note = None
            item_key = str(getattr(getattr(rec, "zotero_item", None),
                                   "item_key", "") or "")
            if fresh is not None:
                notes = (fresh.get(item_key) or []) if item_key else []
                if len(notes) > 1:
                    # Two reading notes under one item: which one the mirror
                    # should hold is a decision, not a default.
                    results[key] = {"ok": False, "reason": "multiple_notes"}
                    continue
                if not notes:
                    # Today's Zotero holds no reading note for this item (it
                    # was deleted since the scan, or the record has no item):
                    # never fall back to the scan's copy of a deleted note.
                    results[key] = {"ok": False, "reason": "no_reading_note"}
                    continue
                note = notes[0]
                rec = _fresh_record(rec, note)
            if not zotero_note_text(rec).strip():
                results[key] = {"ok": False, "reason": "no_reading_note"}
                continue
            if rec.obsidian_note is not None:
                # The rule: never rewrite. A drifted note is the user's edited
                # copy, and regenerating it would delete their work.
                results[key] = {"ok": False, "reason": "already_exists"}
                continue
            rel = str((settings.zotero_notes_dir / f"{key}.md")
                      .relative_to(vault_root))
            text, render_notes = _render_created_note(rec, template, render_ctx,
                                                      extra_tags=note_tags.get(key) or ())
            # prune=False: pruning per note lets the cap evict the batch's own
            # earliest ops (snapshots included) while each response still hands
            # back an op_id. One protected prune runs after the loop instead.
            # save=False + a checkpoint every _JOURNAL_FLUSH_EVERY notes is
            # refactor/apply.py's cadence — the once-per-batch extreme leaves a
            # mid-batch crash invisible to Restore, the per-note extreme is
            # O(N) whole-manifest rewrites.
            op_id, status, err = create_note(
                vault_root, cfg, rel, text, jman, prune=False, save=False)
            if status == "applied":
                created += 1
                created_op_ids.add(op_id)
                results[key] = {"ok": True, "op_id": op_id, "rel": rel,
                                "render_notes": render_notes}
                if note is not None:
                    created_bases.append({
                        "key": key, "item_key": item_key,
                        "note_key": str(getattr(note, "note_key", "") or ""),
                        "version": int(getattr(note, "version", 0) or 0),
                        "html": getattr(note, "body_html", "") or "",
                        "text": text})
                if created % _JOURNAL_FLUSH_EVERY == 0:
                    # The lock is held for the whole batch; a create run
                    # longer than its TTL could have it stolen (audit
                    # remediation 2026-09-23, N12). Beat at each checkpoint;
                    # on refusal stop creating and report what was not
                    # reached, never write on as if the lock were ours.
                    if not obsidian_manager.heartbeat(op_epoch):
                        lock_lost = True
                        not_reached = list(keys[pos + 1:])
                        break
                    journal.save(vault_root, cfg, jman)
            elif status == "uncertain":
                # Publication happened, but durability was not acknowledged.
                # Keep the receipt and protect its Restore record just like
                # an applied create; do not establish a successful sync base.
                created_op_ids.add(op_id)
                results[key] = {"ok": False, "status": "uncertain",
                                "reason": "publication_uncertain", "message": err,
                                "op_id": op_id, "rel": rel,
                                "render_notes": render_notes}
            else:
                results[key] = {"ok": False, "reason": err or "write_failed"}
        if lock_lost:
            # Our ops only, merged into what the new holder saved; no prune
            # (our view of everything else is stale).
            journal.save_merged(vault_root, cfg, jman, created_op_ids)
        else:
            # ONE prune for the batch, with this request's ops exempt from the cap.
            journal.prune(vault_root, cfg, jman, protect_op_ids=created_op_ids)
            journal.save(vault_root, cfg, jman)
    except Exception as exc:  # noqa: BLE001 — surface any write failure cleanly
        logger.exception("notesync create failed")
        if isinstance(exc, journal.JournalUnreadableError) and not created_op_ids:
            # No effect yet: never replace unreadable recovery history.
            return {"error": sanitise_error_msg(exc)}, 503
        # 2026-09-21 review: notes created since the last 25-note checkpoint
        # exist on disk; save their ops before answering, or they have no undo.
        if created_op_ids:
            try:
                journal.prune(vault_root, cfg, jman, protect_op_ids=created_op_ids)
                journal.save(vault_root, cfg, jman)
            except Exception:  # noqa: BLE001 — the original error is the answer
                logger.exception("notesync create: journal save after failure failed")
                message = ("Note publication may have completed, but its recovery journal "
                           "could not be confirmed. Do not assume Restore is available.")
                for receipt in results.values():
                    if receipt.get("op_id") in created_op_ids:
                        receipt.update(ok=False, status="uncertain",
                                       reason="publication_uncertain", message=message,
                                       recovery_confirmed=False)
                created = sum(receipt.get("ok") is True for receipt in results.values())
        return {"error": sanitise_error_msg(exc), "created": created,
                "results": results}, 500
    finally:
        obsidian_manager.release_lock(op_epoch)
        if created_op_ids:
            # Published files also invalidate caches on a journal-error return.
            try:
                from core.vault_resolver import invalidate_index_cache
                invalidate_index_cache(vault_root)
            except Exception:  # noqa: BLE001 — advisory cache only
                pass
            _clear_states_cache()

    return ({"ok": True, "created": created, "results": results,
             "created_bases": created_bases, "cancelled": cancelled,
             "not_reached": not_reached,
             **({"aborted": "lock_lost"} if lock_lost else {})}, 200)


@notesync_bp.route("/api/notesync/create", methods=["POST"])
def api_notesync_create():
    """Create the MISSING reference notes for the given keys. Confirm-gated.

    ``{citation_keys: [...], confirm: true}``. Each note is written through
    ``vault_history.create_note`` — journalled, ``log_vault_write``-audited, and
    reversible from Settings → History and Restore — under the obsidian op-lock held
    once for the whole batch.

    A key whose note already EXISTS is skipped with a reason rather than
    overwritten, whatever its drift state. That is the batch's central rule and
    it is enforced twice: here, and again by ``create_note`` itself, which
    refuses a path that exists.
    """
    data = request.get_json(silent=True) or {}
    raw = data.get("citation_keys")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "citation_keys must be a non-empty list"}), 400
    if len(raw) > _MAX_CREATE:
        return jsonify({"error": (
            f"At most {_MAX_CREATE} notes per request. The restore journal "
            f"retains {_REFACTOR_MAX_OPS} evictable operations, so a larger "
            "batch could not keep the undo this action promises — run it in "
            "several passes instead.")}), 400
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "confirm: true is required."}), 400
    keys = []
    for item in raw:
        key = coerce_string_max_len(item, 512)
        if not key or any(c in key for c in ("\x00", "\n", "\r", "/", "\\")):
            return jsonify({"error": "Invalid citation key"}), 400
        keys.append(key)

    # Today's Zotero read, not the scan's (defect 5): an unreadable database
    # refuses with 503 rather than creating from a possibly-stale scan copy.
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    inv, settings, fresh = ctx
    try:
        if settings_context(settings) != settings_context(load_settings()):
            return jsonify({"error": "Library context changed. Run a fresh audit before writing.",
                            "reason": "stale_audit_context"}), 409
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400

    payload, status = _create_notes(inv, settings, load_config(), keys, fresh=fresh)
    payload.pop("created_bases", None)   # internal plumbing for /mirror
    return jsonify(payload), status


@notesync_bp.route("/api/notesync/update-preview", methods=["POST"])
def api_notesync_update_preview():
    """The exact bytes an update would write for ONE key. Writes nothing.

    ``{citation_key}`` → the current and proposed region, both hashes, and the
    two word counts. The client passes the hashes straight back to
    ``/api/notesync/update``; it never sends note bytes, so the server owns
    what lands (``apply-staged``'s posture).
    """
    data = request.get_json(silent=True) or {}
    resolved, err = _resolve_for_update(data)
    if err is not None:
        return err
    rec, raw, rel, _settings, cfg = resolved
    plan, refusal = _plan_update(rec, raw, cfg.get("notesync_template") or "")
    if refusal is not None:
        return jsonify({"citation_key": rec.citation_key, "rel": rel,
                        "can_update": False, **refusal}), 200
    return jsonify({
        "citation_key": rec.citation_key,
        "rel": rel,
        "can_update": True,
        "changed": plan["changed"],
        "current_region": plan["current_region"],
        "proposed_region": plan["proposed_region"],
        "proposed": plan["proposed"],
        "content_sha256": plan["content_sha256"],
        "proposed_sha256": plan["proposed_sha256"],
        "missing_words": plan["missing_words"],
        "extra_words": plan["extra_words"],
        # Restated per row rather than inferred client-side: this flag is what
        # gates the second acknowledgement, and a client that computed it from
        # extra_words could get the rule wrong in one place and not the other.
        "needs_overwrite_ack": plan["extra_words"] > 0,
    })


@notesync_bp.route("/api/notesync/update", methods=["POST"])
def api_notesync_update():
    """Rewrite ONE note's Zotero region from its Zotero reading note.

    ``{citation_key, content_sha256, proposed_sha256, confirm: true,
    accept_overwrite?: bool}``.

    Everything outside the region — frontmatter (and therefore the tagging
    subsystem's tags), the title/author/journal header, the rule itself — is
    preserved byte-for-byte by construction: the write is ``prefix + region``
    where ``prefix`` came from the file on disk.

    Four gates, in order:

    * ``confirm: true`` — as every vault write in this app.
    * **stale-diff**: the note on disk must still hash to ``content_sha256``,
      or someone edited it since the preview (409).
    * **WYSIWYG**: the recomputed proposal must hash to ``proposed_sha256``,
      or the bytes changed since the preview (409). Together these two are the
      refactor writers' guarantee that what was approved is what lands.
    * **overwrite acknowledgement**: a region carrying words of the user's own
      needs ``accept_overwrite: true`` on top of ``confirm`` (409 otherwise).
      Two gates rather than one because the two decisions are different — "yes,
      write" and "yes, delete what I wrote there" — and collapsing them would
      make the destructive case indistinguishable from the safe one.
    """
    data = request.get_json(silent=True) or {}
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "confirm: true is required."}), 400
    client_content = coerce_string_max_len(data.get("content_sha256"), 64) or ""
    client_proposed = coerce_string_max_len(data.get("proposed_sha256"), 64) or ""
    if not client_content or not client_proposed:
        return jsonify({
            "error": "content_sha256 and proposed_sha256 are required "
                     "(take them from /api/notesync/update-preview)."}), 400
    accept_overwrite = coerce_bool(data.get("accept_overwrite")) is True

    resolved, err = _resolve_for_update(data)
    if err is not None:
        return err
    rec, raw, rel, settings, cfg = resolved
    try:
        if settings_context(settings) != settings_context(load_settings()):
            return jsonify({"error": "Library context changed. Run a fresh audit before writing.",
                            "reason": "stale_audit_context"}), 409
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400

    plan, refusal = _plan_update(rec, raw, cfg.get("notesync_template") or "")
    if refusal is not None:
        return jsonify({"error": refusal["message"], "reason": refusal["reason"]}), 409
    if plan["content_sha256"] != client_content:
        return jsonify({"error": (
            "That note changed on disk since the preview. Re-open the preview "
            "and check the diff again."), "reason": "stale"}), 409
    if plan["proposed_sha256"] != client_proposed:
        return jsonify({"error": (
            "The proposal changed since the preview (the Zotero note or the "
            "template was edited). Re-open the preview."),
            "reason": "proposal_changed"}), 409
    if not plan["changed"]:
        return jsonify({"ok": True, "changed": False, "rel": rel,
                        "reason": "already_in_step"})
    if plan["extra_words"] > 0 and not accept_overwrite:
        # Plain language: this reaches a person through `_showError`, and
        # "re-send with accept_overwrite: true" is an instruction to an API
        # caller, not something a user can act on — there is no field to type
        # it into. The UI reads `reason` and offers the escalated confirm as a
        # BUTTON; a CLI/MCP caller still has the machine-readable reason.
        return jsonify({"error": (
            f"That note's Zotero region contains {plan['extra_words']} things "
            "you wrote that are not in the Zotero note. Updating would delete "
            "them. Open the diff, read what would go, and confirm the "
            "overwrite if that is what you want."),
            "reason": "would_overwrite_user_text",
            "extra_words": plan["extra_words"]}), 409

    vault_root = Path(settings.vault_root)
    from rag.vault import obsidian_manager
    from vault_history import journal
    from vault_history._write import journalled_write_note

    op_epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not op_epoch:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    try:
        jman = journal.load(vault_root, cfg)
        # Re-read under the lock. The plan above was computed from bytes read
        # before it, and the guards only prove nothing changed BEFORE we held
        # the lock; a concurrent writer between the two reads would otherwise
        # have its work silently overwritten.
        current = (vault_root / rel).read_bytes()
        if _sha256_text(raw) != hashlib.sha256(current).hexdigest():
            return jsonify({"error": (
                "That note changed while the write was starting. Nothing was "
                "written; re-open the preview."), "reason": "stale"}), 409
        # pre_write_persist prunes + saves before the note write, so the
        # snapshot's op record is durable first; a single-op write can never be
        # evicted by its own prune (it is the newest), so no protect_op_ids is
        # needed here — unlike the batched create path above. Same call shape
        # as refactor/llm_apply.py.
        op_id, status, err_msg = journalled_write_note(
            vault_root, cfg, rel, current,
            plan["content_sha256"], plan["proposed_sha256"],
            plan["proposed"], _UPDATE_OP_KIND, jman,
            log_category="notesync_update",
            pre_write_persist=True, post_write_persist_on_failure=True,
        )
    except journal.JournalUnreadableError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    except OSError as exc:
        logger.exception("notesync update failed")
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    finally:
        obsidian_manager.release_lock(op_epoch)

    if status != "applied":
        return jsonify({"error": err_msg or "write failed", "op_id": op_id}), 500
    # Phase 3 (2026-09-04): an update is a pull from the SCAN's text; record
    # that as the base so the two-way listing agrees with what just landed.
    try:
        notes = list(getattr(rec.zotero_item, "child_notes", None) or [])
        parts = split_note_parts(plan["proposed"])
        if len(notes) == 1 and parts is not None:
            _save_base(Path(settings.vault_root), key=rec.citation_key,
                       item_key=str(getattr(rec.zotero_item, "item_key", "") or ""),
                       note_key=str(getattr(notes[0], "note_key", "") or ""),
                       version=int(getattr(notes[0], "version", 0) or 0),
                       html=getattr(notes[0], "body_html", "") or "", region=parts[1])
    except Exception:  # noqa: BLE001 — the base is a convenience here, never the write
        logger.exception("notesync update: base record not written")
    return jsonify({"ok": True, "changed": True, "rel": rel, "op_id": op_id,
                    "imported_words": plan["missing_words"],
                    "overwrote_words": plan["extra_words"]})


# ---------------------------------------------------------------------------
# Layout migration (Phase 2, 2026-09-04)
# ---------------------------------------------------------------------------

#: Heartbeat cadence inside the migration loop (refactor/apply.py's precedent).
_MIGRATE_HEARTBEAT_EVERY = 25


def _git_undo_check(vault_root: Path, files: dict) -> tuple[bool, str, str | None]:
    """``(ok, detail, commit)``: can the vault's git restore every file in *files*?

    *files* maps each note a sweep will REPLACE to the exact text it planned
    against. Per file, against one named commit — see ``notesync/git_undo.py``
    for why the former folder-level gate (``git status`` + "``ls-files`` lists
    something") let a partly git-ignored folder through (library-workflow plan
    lot 1, defect 1). Kept as a route-level name so tests patch it here.

    The sweeps that call this are NOT journalled (1,235 notes cannot ride the
    refactor journal's 200-op window); by maintainer decision (2026-09-04) the
    vault's git is their undo, and this is what makes that decision true for
    every replaced file rather than for the folder as a whole. A sweep that
    replaces nothing (creation only) passes an empty mapping and needs no
    repository at all.
    """
    return git_undo.check(vault_root, files)


def _run_layout_change(direction: str):
    """Shared body of the migrate and unmigrate endpoints.

    ``{confirm?: bool, dry_run?: bool}``. Without ``confirm: true`` the request
    is a DRY RUN that plans every note and writes nothing; with it, the plan
    is applied under the obsidian op-lock. The plan is recomputed on the write
    path from the bytes on disk — never carried over from a dry run.
    """
    data = request.get_json(silent=True) or {}
    confirm = coerce_bool(data.get("confirm")) is True
    dry_run = not confirm or coerce_bool(data.get("dry_run")) is True
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "Run a Library Audit scan first."}), 404
    try:
        load_settings()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    vault_root = Path(settings.vault_root)
    notes_dir = Path(settings.zotero_notes_dir)
    fn = migrate_note if direction == "migrate" else unmigrate_note

    def _plan_one(rec):
        note = getattr(rec, "obsidian_note", None)
        if note is None:
            return None, "no_note", None
        item = getattr(rec, "zotero_item", None)
        child = list(getattr(item, "child_notes", None) or [])
        if not child:
            return None, "no_reading_note", None
        path = Path(note.path)
        try:
            path.resolve().relative_to(notes_dir.resolve())
        except ValueError:
            return None, "outside_notes_dir", None
        try:
            raw = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return None, "not_utf8", None
        except OSError:
            return None, "unreadable", None
        if direction == "migrate":
            if len(child) != 1:
                return None, "multiple_notes", None
            new, reason = fn(raw, item_key=str(getattr(item, "item_key", "") or ""),
                             note_key=str(getattr(child[0], "note_key", "") or ""))
        else:
            new, reason = fn(raw)
            if new is not None and reason == "tail_kept":
                reason = None
        return new, reason, (path, raw)

    skipped: dict[str, list[str]] = {}
    planned: list[tuple[str, Path, str, str]] = []
    for key, rec in sorted(inv.records.items()):
        new, reason, ctx = _plan_one(rec)
        if new is None:
            if reason not in ("no_note", "no_reading_note"):
                skipped.setdefault(reason, []).append(key)
            continue
        planned.append((key, ctx[0], ctx[1], new))

    payload = {
        "direction": direction,
        "dry_run": dry_run,
        "planned": len(planned),
        "skipped": {k: sorted(v) for k, v in skipped.items()},
        "template_hint": TEMPLATE_HINT,
    }
    if dry_run:
        return jsonify(payload)

    # Per replaced file, against the exact bytes planned above. The write
    # loop below re-reads each file under the op-lock and skips any that no
    # longer equals `raw`, so "commit X holds what was replaced" stays true for
    # every file actually written.
    clean, detail, commit = _git_undo_check(
        vault_root, {path: raw for _key, path, raw, _new in planned})
    if not clean:
        return jsonify({"error": f"Refusing to change the note layout: {detail}.",
                        "reason": "git_not_clean", **payload}), 409
    payload["git_restore_commit"] = commit

    from core.utils import log_vault_write, write_text_atomic
    from rag.vault import obsidian_manager

    op_epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not op_epoch:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    written = 0
    failed: dict[str, str] = {}
    aborted = False
    try:
        for i, (key, path, raw, new) in enumerate(planned, 1):
            if i % _MIGRATE_HEARTBEAT_EVERY == 0 and not obsidian_manager.heartbeat(op_epoch):
                aborted = True
                break
            try:
                if _became_symlink(path):
                    failed[key] = "became_symlink"
                    continue
                # Re-read: the plan came from bytes read before the lock.
                if path.read_bytes().decode("utf-8") != raw:
                    failed[key] = "changed_on_disk"
                    continue
                rel = str(path.relative_to(vault_root))
                log_vault_write(f"notesync_{direction}", rel,
                                f"{len(raw)}→{len(new)} bytes")
                write_text_atomic(str(path), new)
                written += 1
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                failed[key] = type(exc).__name__
    finally:
        obsidian_manager.release_lock(op_epoch)
    _clear_states_cache()
    payload.update({"written": written, "failed": failed, "aborted": aborted})
    return jsonify(payload)


@notesync_bp.route("/api/notesync/migrate", methods=["POST"])
def api_notesync_migrate():
    """Wrap every reference note's Zotero region in the marker pair and stamp
    the Zotero item + note keys into its frontmatter. Confirm-gated; a request
    without ``confirm: true`` is a dry run. Refuses unless the notes folder is
    committed in the vault's git (the undo)."""
    return _run_layout_change("migrate")


@notesync_bp.route("/api/notesync/unmigrate", methods=["POST"])
def api_notesync_unmigrate():
    """The exact inverse of ``/migrate`` for every marked note."""
    return _run_layout_change("unmigrate")


# ---------------------------------------------------------------------------
# Two-way sync state (Phase 3, 2026-09-04)
# ---------------------------------------------------------------------------

#: Memo of the FRESH Zotero child-note read, keyed on the SQLite file's
#: database and WAL/SHM identity. ``read_items`` over a copied snapshot costs 0.49 s on
#: the live library — cheap enough to run per sync pass without an audit scan,
#: too expensive to run per poll. The cached scan stays the source of the
#: citekey ↔ item JOIN; only the note TEXT and VERSION come from here.
_FRESH_NOTES: dict | None = None
_FRESH_NOTES_LOCK = threading.Lock()


def _fresh_zotero_notes(settings) -> dict[str, list]:
    """``{item_key: [ZoteroChildNote, …]}`` read from the CURRENT SQLite.

    Raises ``OSError``/``RuntimeError`` when the database cannot be read; the
    caller answers 503 rather than classifying against stale text.
    """
    global _FRESH_NOTES
    from audit.core.zotero import read_items
    sqlite_path = Path(settings.zotero_sqlite)
    def signature():
        stamps = []
        for suffix in ('', '-wal', '-shm'):
            try:
                st = Path(str(sqlite_path) + suffix).stat()
                stamps.append((st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
            except FileNotFoundError:
                if not suffix:
                    raise
                stamps.append(None)
        return tuple(stamps)
    try:
        sig = signature()
    except OSError as exc:
        raise OSError(f"Zotero database unreadable: {exc}") from exc
    with _FRESH_NOTES_LOCK:
        if _FRESH_NOTES is not None and _FRESH_NOTES["sig"] == sig \
                and _FRESH_NOTES["path"] == str(sqlite_path):
            return _FRESH_NOTES["by_key"]
    items = read_items(sqlite_path, Path(settings.zotero_storage))
    if signature() != sig:
        raise RuntimeError('Zotero changed while reading notes. Retry after it finishes writing.')
    by_key = {it.item_key: list(it.child_notes) for it in items if it.child_notes}
    with _FRESH_NOTES_LOCK:
        _FRESH_NOTES = {"sig": sig, "path": str(sqlite_path), "by_key": by_key}
    return by_key


def _sync_rows(inv, settings, fresh: dict[str, list]) -> list[dict]:
    """One row per record that has a reference note OR a reading note.

    Each row carries the classification plus the context a verb needs
    (``ctx``: the note path, the region parts, the fresh note, the base), which
    the listing strips before serialising.

    # PERF (2026-09-11): every sync-status / pull / push / merge call re-read
    #   every reference note and re-ran classify(). Known Issues recorded the
    #   residual after the one-way ``_states`` memo: the two-way path still
    #   paid a full sweep per request.
    #   Before:    each GET /api/notesync/sync-status decoded every note,
    #              split regions, converted Zotero HTML, loaded the base, and
    #              classified — even when nothing had changed since the last
    #              poll. Same Waitress-worker cost class as the pre-memo
    #              ``_states`` sweep (measured hundreds of ms over ~1.2k notes).
    #   Change:    module memo keyed on Inventory/Settings identity plus, per
    #              citation key, the note's (size, mtime_ns), the base file's
    #              (size, mtime_ns), and a fingerprint of the fresh Zotero
    #              child note(s) for that item (note_key, version, body hash).
    #   Safe because:
    #              the citekey↔item JOIN is owned by the cached Inventory (a
    #              new scan replaces the object AND fires the listener that
    #              drops this memo); the Obsidian half invalidates on file
    #              sig; the base half invalidates on its fsynced JSON sig;
    #              the Zotero half invalidates when the fresh-read fingerprint
    #              moves. Successful pull/push/merge/bootstrap/create already
    #              call ``_clear_states_cache``, so a write never leaves a
    #              stale classification for the next poll.
    #   Preserves: "a report is a view of ONE cached scan"; unreadable /
    #              unparsed / missing-side rows still compute the same states;
    #              ``ctx`` is retained for write routes (listing strips it via
    #              ``_public_row``).
    #   Lock/thread contract:
    #              short module lock around the dict only; note reads and
    #              classify run outside it — concurrent pollers may duplicate
    #              work but never block each other on I/O.
    #   Pinned by: test_notesync_route.py::TestSyncRowsMemo
    """
    global _SYNC_ROWS_CACHE
    template = load_config().get("notesync_template") or ""

    def _notes_fp(notes: list) -> tuple:
        out = []
        for n in notes or ():
            html = getattr(n, "body_html", "") or ""
            out.append((
                str(getattr(n, "note_key", "") or ""),
                int(getattr(n, "version", 0) or 0),
                hash(html),
            ))
        return tuple(out)

    with _STATES_CACHE_LOCK:
        cache = _SYNC_ROWS_CACHE
        if (cache is not None and cache["inv"] is inv
                and cache["settings"] is settings):
            prev = cache["by_key"]
        else:
            prev = {}

    rows: list[dict] = []
    vault_root = Path(settings.vault_root)
    fresh_by_key: dict = {}
    dir_names = _note_dir_names(settings.zotero_notes_dir)
    for key, rec in inv.records.items():
        item = getattr(rec, "zotero_item", None)
        item_key = str(getattr(item, "item_key", "") or "")
        notes = fresh.get(item_key, []) if item_key else []
        obs = getattr(rec, "obsidian_note", None)
        if obs is None and not notes:
            continue

        note_sig = None
        path = None
        if obs is not None:
            path = Path(obs.path)
            note_sig = _note_sig(path)
        elif notes:
            # A note created SINCE the scan — by `/create`, the mirror's first
            # phase, or the Zotero Integration plugin — lives exactly where a
            # create would write it, and the cached scan cannot know it
            # exists. Without this, a second mirror run straight after the
            # first proposed every just-created note again and `create_note`
            # refused each one ("a note already exists"), a red run over a
            # library that was in step (library-workflow lot 4, plan §8: a
            # second run without change writes nothing). Its (size, mtime)
            # joins the memo fingerprint below, so the row stays fresh.
            candidate = _created_since_scan(settings.zotero_notes_dir, key, dir_names)
            if candidate is not None:
                obs = SimpleNamespace(path=str(candidate))
                path = candidate
                note_sig = _note_sig(candidate)
        base_sig = _note_sig(base_store._path(vault_root, key))
        notes_fp = _notes_fp(notes)
        fp = (note_sig, base_sig, notes_fp, item_key, template,
              getattr(item, "server_id", None), getattr(item, "library_id", None))
        hit = prev.get(key)
        if hit is not None and hit[0] == fp:
            row = hit[1]
            # Refresh the live Zotero note object from THIS fresh map so a
            # write route never holds a note from a superseded fresh read
            # when the fingerprint still matched (same content, new objects).
            if row.get("ctx") and notes:
                row["ctx"]["note"] = notes[0]
                row["ctx"]["html"] = getattr(notes[0], "body_html", "") or ""
                row["ctx"]["md_now"] = (
                    html_to_md(row["ctx"]["html"]) if row["ctx"]["html"]
                    else str(getattr(notes[0], "body_plain", "") or "")
                )
                row["ctx"]["note_key"] = str(getattr(notes[0], "note_key", "") or "")
                row["zotero_version"] = int(getattr(notes[0], "version", 0) or 0)
            rows.append(row)
            fresh_by_key[key] = (fp, row)
            continue

        row = {"citation_key": key, "item_key": item_key, "note_rel": None,
               "state": None, "zotero_changed": False, "region_changed": False,
               "missing_words": 0, "extra_words": 0, "baselined": False,
               "marked": False, "zotero_version": 0, "base_synced_at": None,
               "ctx": {}}
        if obs is None and len(notes) > 1:
            # Two reading notes under one item and no mirror yet: which one
            # the mirror should hold is a decision, so it is not "missing"
            # (2026-09-21 review: 05m proposed it on every run and /create
            # refused it every time).
            row["state"] = "multiple_notes"
            row["note_rel"] = str((settings.zotero_notes_dir / f"{key}.md")
                                  .relative_to(vault_root))
            rows.append(row)
            fresh_by_key[key] = (fp, row)
            continue
        if obs is None:
            row["state"] = "missing_obsidian"
            row["note_rel"] = str((settings.zotero_notes_dir / f"{key}.md")
                                  .relative_to(vault_root))
            rows.append(row)
            fresh_by_key[key] = (fp, row)
            continue
        assert path is not None
        try:
            row["note_rel"] = str(path.relative_to(vault_root))
        except ValueError:
            row["note_rel"] = str(path)
        if not notes:
            row["state"] = "missing_zotero"
            rows.append(row)
            fresh_by_key[key] = (fp, row)
            continue
        if len(notes) != 1:
            row["state"] = "multiple_notes"
            rows.append(row)
            fresh_by_key[key] = (fp, row)
            continue
        note = notes[0]
        try:
            raw = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            row["state"] = "unparsed"
            rows.append(row)
            fresh_by_key[key] = (fp, row)
            continue
        parts = split_note_parts(raw)
        if parts is None:
            row["state"] = "unparsed"
            rows.append(row)
            fresh_by_key[key] = (fp, row)
            continue
        region = parts[1]
        html = getattr(note, "body_html", "") or ""
        md_now = html_to_md(html) if html else str(getattr(note, "body_plain", "") or "")
        base = base_store.load(vault_root, key)
        md_base = html_to_md(base.base_html) if base is not None else None
        # "Equal after the current converter" (plan Phase 5): the region as
        # Zotero would hold it after a push, re-read through the converter.
        rendered = html_to_md(md_to_html(region.strip("\n")))
        d = classify(base=base, zotero_md_now=md_now, zotero_md_base=md_base,
                     region_now=region, converter_id=CONVERTER_ID,
                     region_rendered=rendered)
        # Decision D4 (a), 2026-09-17: only content INSIDE the region can raise
        # an issue. Text above the begin marker (a plugin-rendered header that
        # differs from ours) and below the end marker is the user's, and every
        # writer preserves both byte-for-byte (split_note_parts). The former
        # scope check refused 1,194 of 1,237 live notes on header cosmetics.
        issues = content_issues(html, region)
        # 2026-09-17 review, B6: a base written by a previous converter is
        # read as absent. It can be carried over — same stored HTML, version
        # and region — only while the CURRENT converter's rendering of its
        # stored HTML still equals the region; an unbaselined note is ready
        # for a first base only on the same normalised-text equality.
        legacy = base is not None and base.converter_id != CONVERTER_ID
        if legacy:
            ready = equivalent(md_base, region, rendered)
        else:
            ready = base is None and equivalent(md_now, region, rendered)
        row.update({
            "legacy_base": legacy, "bootstrap_ready": bool(ready and d.state == "unbaselined" and not issues),
            "content_issues": issues, "comparison_scope": "recognized personal-content region",
            "native_scope": {"database": getattr(item, "server_id", None), "library": getattr(item, "library_id", None), "item": item_key},
            "state": "unparsed" if issues else d.state, "zotero_changed": d.zotero_changed,
            "region_changed": d.region_changed, "missing_words": d.missing_words,
            "extra_words": d.extra_words, "baselined": d.baselined,
            "marked": REGION_BEGIN in raw,
            "zotero_version": int(getattr(note, "version", 0) or 0),
            "base_synced_at": base.synced_at if base is not None else None,
            "ctx": {"path": path, "raw": raw, "parts": parts, "note": note,
                    "html": html, "md_now": md_now, "base": base,
                    "note_key": str(getattr(note, "note_key", "") or "")},
        })
        rows.append(row)
        fresh_by_key[key] = (fp, row)

    with _STATES_CACHE_LOCK:
        _SYNC_ROWS_CACHE = {"inv": inv, "settings": settings, "by_key": fresh_by_key}
    return rows


def _public_row(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "ctx"}


def _load_sync_context():
    """Shared prologue: cached scan + fresh Zotero read, or an error tuple."""
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return None, (jsonify({"error": "Run a Library Audit scan first."}), 404)
    try:
        fresh = _fresh_zotero_notes(settings)
    except (OSError, RuntimeError) as exc:
        return None, (jsonify({"error": sanitise_error_msg(exc),
                               "reason": "zotero_unreadable"}), 503)
    except Exception as exc:  # noqa: BLE001 — sqlite3 errors et al.
        logger.exception("fresh Zotero read failed")
        return None, (jsonify({"error": sanitise_error_msg(exc),
                               "reason": "zotero_unreadable"}), 503)
    return (inv, settings, fresh), None


@notesync_bp.route("/api/notesync/sync-status")
def api_notesync_sync_status():
    """Every note's two-way state against a FRESH Zotero read. Read-only."""
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    inv, settings, fresh = ctx
    rows = [_public_row(r) for r in _sync_rows(inv, settings, fresh)]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1

    def _rank(r):
        return (-(r["missing_words"] + r["extra_words"]), r["citation_key"])

    return jsonify({
        "converter_id": CONVERTER_ID,
        "authoritative": _authoritative(load_config()),
        "legacy_bases": sum(1 for r in rows if r.get("legacy_base")),
        "total": len(rows),
        "counts": counts,
        "rows": sorted(rows, key=_rank),
    })


def _authoritative(cfg: dict) -> bool:
    return coerce_bool(cfg.get("notesync_zotero_authoritative")) is True


def _refresh_plan(inv, settings, fresh, cfg) -> dict:
    """What a Zotero-authoritative sweep WOULD do. Reads notes; writes nothing.

    Split out of :func:`api_notesync_refresh_all` (2026-09-20) so the one-click
    mirror can present a real plan in its confirm and then execute it, rather
    than re-deriving the sweep a second way. The route's behaviour is
    unchanged: it calls this, then :func:`_refresh_write`.
    """
    template = cfg.get("notesync_template") or ""
    vault_root = Path(settings.vault_root)
    notes_dir = Path(settings.zotero_notes_dir)

    planned: list[dict] = []
    keep_base: list[dict] = []
    listed: dict[str, list[str]] = {}
    lossy: list[str] = []
    rows = _sync_rows(inv, settings, fresh)
    for row in rows:
        key = row["citation_key"]
        state = row["state"]
        if state == "missing_obsidian":
            listed.setdefault("missing_obsidian", []).append(key)
            continue
        if state == "missing_zotero":
            listed.setdefault("deleted_in_zotero", []).append(key)
            continue
        if state == "multiple_notes" or not row.get("ctx"):
            listed.setdefault(state, []).append(key)
            continue
        c = row["ctx"]
        plan, refusal = _plan_update(_fresh_record(inv.records[key], c["note"]),
                                     c["raw"], template)
        if refusal is not None or plan is None:
            # `plan is None` is the same condition, spelled out: the pair is
            # exclusive by construction and this keeps the narrowing visible.
            refusal = refusal or {"reason": "no_plan"}
            listed.setdefault(refusal["reason"], []).append(key)
            continue
        if row.get("content_issues"):
            # Images / unsupported markup: Zotero's rendering is still what
            # the mirror should show; disclosed, since it is not a round trip.
            lossy.append(key)
        entry = {"key": key, "path": c["path"], "raw": c["raw"], "row": row,
                 "proposed": plan["proposed"], "region": plan["proposed_region"]}
        # "Changed" means the TEXT differs after the converter — the same
        # equality `sync-status` calls in_sync — never a byte comparison.
        # Measured on the first live dry run (2026-09-19): a byte plan wanted
        # to rewrite 1,222 of 1,236 notes, almost all for the marker framing
        # and Markdown spelling (`n=29` → `n\=29`, `**_x_**` → `***x***`) —
        # a readability regression on every note and a git diff that buries
        # the nine real changes.
        region_now = c["parts"][1]
        rendered = html_to_md(md_to_html(region_now.strip("\n")))
        same = not plan["changed"] or equivalent(c["md_now"], region_now, rendered)
        (keep_base if same else planned).append(entry)

    known = set(inv.records)
    orphans = sorted(p.stem for p in notes_dir.glob("*.md") if p.stem not in known)
    if orphans:
        listed["no_record"] = orphans
    live_keys = {e["key"] for e in planned} | {e["key"] for e in keep_base}
    # A base is stale only when its note is gone from disk. A note that is
    # merely LISTED (reading note not synced down yet, several notes,
    # unparsed) keeps its sync point, or it would come back unbaselined.
    stale_bases = sorted(k for k in base_store.load_all(vault_root)
                         if k not in live_keys and not (notes_dir / f"{k}.md").exists())
    # The REVISION of each base planned for deletion (library-workflow plan
    # lot 1, defect 7). The writer deletes only while the file still has this
    # digest and the note is still absent, so a base recorded after this plan
    # — a concurrent mirror recreating the note — is never removed by it.
    stale_digests = {k: base_store.file_digest(vault_root, k) for k in stale_bases}

    payload = {
        "planned": len(planned),
        "planned_keys": sorted(e["key"] for e in planned),
        "unchanged": len(keep_base),
        "lossy_markup": sorted(lossy),
        "listed": {k: sorted(v) for k, v in listed.items()},
        "stale_bases": len(stale_bases),
    }
    return {"rows": rows, "planned": planned, "keep_base": keep_base,
            "stale_bases": stale_bases, "stale_base_digests": stale_digests,
            "notes_dir": notes_dir, "payload": payload}


def _plan_digest(plan: dict, cfg: dict, create_entries: list | None = None) -> str:
    """Identity of a reviewed plan: what a confirm is allowed to execute.

    Library-workflow plan lot 1, defect 9 (2026-09-21). A dry run used to be
    advisory: the confirm recomputed the plan from whatever the world looked
    like at confirm time and executed THAT, so a Zotero edit, a new reading
    note or an Obsidian edit between the review and the click was written
    without ever being shown. Now the dry run returns this digest, the confirm
    must send it back, and the server recomputes the plan and refuses
    (``plan_changed``) unless the two agree.

    What is in it — everything that decides WHICH file is written with WHICH
    bytes, or which record is removed:

    * each rewrite: key, destination path, sha256 of the bytes it replaces and
      of the bytes it writes (the proposed text already embeds the Zotero
      note's rendering, so a Zotero edit moves it);
    * the lossy-markup list (a conversion loss the reviewer accepted);
    * each unchanged note that will get a sync point, with its bytes;
    * each stale base planned for deletion, with its file revision;
    * each note to CREATE (mirror only): key, the Zotero item and note keys,
      the note's version and a hash of its HTML, plus the template and
      converter that render it.

    What is NOT in it: listed-but-untouched rows and counts derived from the
    above. A change there cannot make the confirm write anything the reviewer
    did not see, so refusing on it would only cost a needless re-review.

    Preserves state: computing it reads nothing new — every input is already
    in the plan — so adding it changes no plan and no write.
    """
    def sha(text: str) -> str:
        return _sha256_text(text)

    ident = {
        "converter": CONVERTER_ID,
        "rewrite": sorted([e["key"], str(e["path"]), sha(e["raw"]), sha(e["proposed"])]
                          for e in plan["planned"]),
        "lossy": sorted(plan["payload"]["lossy_markup"]),
        "baseline": sorted([e["key"], sha(e["raw"])] for e in plan["keep_base"]
                           if _needs_base(e["row"])),
        "stale_bases": sorted([k, plan["stale_base_digests"].get(k)]
                              for k in plan["stale_bases"]),
    }
    if create_entries is not None:
        ident["create"] = sorted(create_entries)
        ident["template"] = sha(cfg.get("notesync_template") or "")
    return hashlib.sha256(json.dumps(ident, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def _needs_base(row: dict) -> bool:
    """Whether an unchanged row still has to have its sync point (re)written.

    Two ways to need nothing, either sufficient:

    * the historical one — a current-converter base and an ``in_sync`` row;
    * the stored base ALREADY HOLDS exactly what a write would store (same
      item and note keys, Zotero version, note HTML, region, converter).

    The second exists for the rows ``in_sync`` never covers — ``unparsed``
    notes (images, unsupported markup: 156 on the maintainer's library) and
    the like — which were planned into ``keep_base`` on every run and had
    their base rewritten each time although nothing had changed
    (library-workflow lot 4, 2026-09-21; plan §8: "a second run without
    change rewrites nothing"). It compares the base the plan read with the
    values ``_record_base`` would write, field for field, so it can only skip
    a write whose result would be byte-identical in content (``synced_at``
    aside); any Zotero re-save (a version bump), a new converter or an
    edited region still writes. The comparison is against the base read by
    THIS plan; a base changed between plan and write is overwritten exactly
    as before — the store's per-vault lock serialises the two writers, it
    does not make the plan fresher.
    """
    ctx = row.get("ctx") or {}
    base = ctx.get("base")
    if (row.get("baselined") and not row.get("legacy_base") and row["state"] == "in_sync"
            and (base is None or base.note_key == ctx.get("note_key"))):
        # (The note-key test, review 2026-09-21: a reading note re-created in
        # Zotero with the same text stays `in_sync`, and the shortcut kept the
        # dead note key in the sync point forever.)
        return False
    if base is None or row.get("legacy_base"):
        return True
    parts = ctx.get("parts") or ("", None, "")
    return not (
        base.converter_id == CONVERTER_ID
        and base.item_key == row.get("item_key")
        and base.note_key == ctx.get("note_key")
        and int(base.zotero_version) == int(row.get("zotero_version") or 0)
        and base.base_html == ctx.get("html")
        and base.base_region == parts[1]
    )


def _refresh_write(vault_root: Path, plan: dict, *,
                   progress=None, should_cancel=None) -> dict | None:
    """Apply a :func:`_refresh_plan`. ``None`` when the op-lock is held.

    The lock covers only the NOTE writes: the base-store writes and the
    stale-base deletions that follow touch nothing in the vault (they take the
    base store's own lock instead, see ``notesync/base_store.py``).
    *progress* is called with ``(done, total)`` every
    ``_MIGRATE_HEARTBEAT_EVERY`` notes so a streaming caller can feed the SSE
    stall guard; the synchronous route passes none.

    Every effect is counted separately (library-workflow plan lot 1, defect 6,
    2026-09-21). Before, a note written and then a failed base save landed in
    ``written`` AND in ``bases_written`` (computed as ``written + bases``), and
    a failed stale-base unlink escaped as an exception. Now:

    * ``written`` — notes whose region was replaced;
    * ``bases_written`` — base records actually saved;
    * ``base_failed`` — ``{key: error}`` for base saves that failed (the note
      itself WAS written; the next status reads it as unbaselined);
    * ``stale_bases_removed`` / ``stale_base_failed`` / ``stale_bases_kept`` —
      deletions performed, failed per key, and skipped because the base or
      the note moved since the plan (defect 7);
    * ``failed`` — every failure, keyed, prefixed by its effect (``base:``,
      ``stale_base:``) when it is not the note write, so a caller that only
      reads ``failed`` still sees each one.

    Cancellation (defect 8): *should_cancel* is polled BEFORE each note. A
    cancel never interrupts the boundary already engaged — a note just written
    still gets its base saved in the same iteration, because that base is the
    only record of what was written until the next status read — and then the
    loop stops. The unchanged-note baselines and the stale-base deletions
    that follow are not engaged yet, so a cancelled (or lock-lost) run skips
    them; ``cancelled``/``aborted`` say so.
    """
    from core.utils import log_vault_write, write_text_atomic
    from rag.vault import obsidian_manager

    planned = plan["planned"]
    keep_base = plan["keep_base"]
    stale_bases = plan["stale_bases"]
    stale_digests = plan.get("stale_base_digests") or {}
    notes_dir = Path(plan.get("notes_dir") or vault_root)

    def _base_for(entry: dict, region: str) -> None:
        row = entry["row"]
        _save_base(vault_root, key=entry["key"], item_key=row["item_key"],
                   note_key=row["ctx"]["note_key"], version=row["zotero_version"],
                   html=row["ctx"]["html"], region=region)

    written = 0
    # The keys actually rewritten, not just their count: the mirror's
    # `settled_keys` (audit remediation 2026-09-23, L1) closes a tracked note
    # unit only on this positive evidence.
    written_keys: list[str] = []
    bases = 0
    failed: dict[str, str] = {}
    base_failed: dict[str, str] = {}
    aborted = False
    cancelled = False
    not_reached: list[str] = []

    def _record_base(entry: dict, region: str) -> None:
        nonlocal bases
        try:
            _base_for(entry, region)
            bases += 1
        except OSError as exc:
            base_failed[entry["key"]] = type(exc).__name__
            failed[entry["key"]] = f"base: {type(exc).__name__}"

    # The op-lock covers the NOTE writes and nothing else (2026-09-20 review):
    # a plan with no region to rewrite needs no lock — `/mirror` calls this
    # unconditionally, and used to stop at "indexing" for a run that only had
    # a sync point to record.
    op_epoch: int = 0
    if planned:
        acquired = obsidian_manager.try_acquire_lock(ttl=600)
        if not acquired:
            return None
        op_epoch = acquired
    try:
        for i, entry in enumerate(planned, 1):
            if should_cancel is not None and should_cancel():
                cancelled = True
                not_reached = [e["key"] for e in planned[i - 1:]]
                break
            if i % _MIGRATE_HEARTBEAT_EVERY == 0:
                if not obsidian_manager.heartbeat(op_epoch):
                    aborted = True
                    not_reached = [e["key"] for e in planned[i - 1:]]
                    break
                if progress is not None:
                    progress(i, len(planned))
            path: Path = entry["path"]
            try:
                if _became_symlink(path):
                    failed[entry["key"]] = "became_symlink"
                    continue
                # Re-read under the lock: the plan (and the git check, which
                # proved commit X holds exactly `raw`) came from bytes read
                # before it. A file that moved since is skipped, never written.
                if path.read_bytes().decode("utf-8") != entry["raw"]:
                    failed[entry["key"]] = "changed_on_disk"
                    continue
                rel = str(path.relative_to(vault_root))
                log_vault_write("notesync_refresh", rel,
                                f"{len(entry['raw'])}→{len(entry['proposed'])} bytes")
                write_text_atomic(str(path), entry["proposed"])
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                failed[entry["key"]] = type(exc).__name__
                continue
            written += 1
            written_keys.append(entry["key"])
            _record_base(entry, entry["region"])
    finally:
        if op_epoch:
            obsidian_manager.release_lock(op_epoch)

    removed = 0
    stale_failed: dict[str, str] = {}
    stale_kept: dict[str, str] = {}
    if not aborted and not cancelled:
        for i, entry in enumerate(keep_base):
            if should_cancel is not None and should_cancel():
                cancelled = True
                not_reached.extend(e['key'] for e in keep_base[i:])
                break
            if _needs_base(entry["row"]):
                _record_base(entry, entry["row"]["ctx"]["parts"][1])
        for i, key in enumerate(stale_bases):
            # Each deletion is a new recovery boundary, even when there
            # were no note replacements to enter the loop above.
            if cancelled or (should_cancel is not None and should_cancel()):
                cancelled = True
                not_reached.extend(stale_bases[i:])
                break
            note_path = notes_dir / f"{key}.md"

            def _note_still_absent(p: Path = note_path) -> bool:
                return not p.exists()
            try:
                outcome = base_store.delete_if_unchanged(
                    vault_root, key, stale_digests.get(key),
                    still_stale=_note_still_absent)
            except OSError as exc:
                stale_failed[key] = type(exc).__name__
                failed[key] = f"stale_base: {type(exc).__name__}"
                continue
            if outcome == "deleted":
                removed += 1
            elif outcome != "gone":
                stale_kept[key] = outcome
    _clear_states_cache()
    return {"written": written, "written_keys": written_keys,
            "failed": failed, "base_failed": base_failed,
            "aborted": aborted, "cancelled": cancelled, "not_reached": not_reached,
            "bases_written": bases,
            "stale_bases_removed": removed, "stale_base_failed": stale_failed,
            "stale_bases_kept": stale_kept}


@notesync_bp.route("/api/notesync/refresh-all", methods=["POST"])
def api_notesync_refresh_all():
    """Zotero → Obsidian for EVERY reference note, in one sweep.

    ``{confirm?: bool, dry_run?: bool}``. The verb of the zotero-authoritative
    mode (maintainer decision 2026-09-19: the reading notes are edited only in
    Zotero, the folder is a read-only mirror): every note whose marked region
    differs from the converter's rendering of today's Zotero note gets that
    rendering, whatever it held — there is no "your words at risk" question,
    because by decision none of the words are the user's. Frontmatter, the
    header and anything below the end marker stay byte-for-byte (the same
    `split_note_parts` contract as `pull`).

    Deliberately NOT journalled and NOT bounded — 1,236 notes cannot ride the
    refactor journal's 200-op window, so like `/migrate` it refuses unless the
    notes folder is committed in the vault's git, which is the undo. The
    sync base is (re)written for every note the sweep leaves equal to Zotero,
    and a base whose note no longer exists is removed.

    Two things it never does, by the same decision: it never creates a note
    (`missing_obsidian` rows are listed for `/create`) and it never deletes
    one — a note whose Zotero item or reading note is gone is LISTED
    (``deleted_in_zotero`` / ``no_record``), never removed. ``/mirror`` is the
    one verb that also creates and baselines.
    """
    data = request.get_json(silent=True) or {}
    confirm = coerce_bool(data.get("confirm")) is True
    dry_run = not confirm or coerce_bool(data.get("dry_run")) is True
    cfg = load_config()
    if not _authoritative(cfg):
        return jsonify({"error": "Zotero-authoritative note sync is off (Settings → "
                        "Zotero → Obsidian note sync).", "reason": "mode_off"}), 409
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    inv, settings, fresh = ctx
    try:
        load_settings()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    vault_root = Path(settings.vault_root)

    reviewed = coerce_string_max_len(data.get("plan_digest"), 128) or ""
    if not dry_run and not reviewed:
        # Library-workflow plan lot 1, defect 9: a confirm executes the plan
        # the reviewer saw, never one recomputed unseen. Decided before any
        # note is read so a client that skipped the dry run learns it at once.
        return jsonify({"error": "Run a dry run first and confirm with its plan_digest.",
                        "reason": "plan_digest_required"}), 400

    plan = _refresh_plan(inv, settings, fresh, cfg)
    digest = _plan_digest(plan, cfg)
    rewrite = {e["path"]: e["raw"] for e in plan["planned"]}
    payload = {"dry_run": dry_run, "plan_digest": digest, **plan["payload"]}
    if dry_run:
        # Disclosed at review time so the confirm can say whether git can
        # undo the rewrites before the user commits to them. Advisory here;
        # the confirm re-runs the check against the bytes it will replace.
        ok, detail, commit = (_git_undo_check(vault_root, rewrite) if rewrite
                              else (True, "", None))
        payload.update({"git_restorable": ok, "git_detail": detail,
                        "git_restore_commit": commit})
        return jsonify(payload)

    if reviewed != digest:
        # The new digest is withheld (2026-09-21 review): a client that retried
        # with whatever digest a refusal carried would execute a plan nobody
        # reviewed. Only a dry run hands one out.
        payload.pop("plan_digest", None)
        return jsonify({"error": "The notes or Zotero changed since the dry run. "
                                 "Review the new plan before confirming.",
                        "reason": "plan_changed", **payload}), 409
    clean, detail, commit = (_git_undo_check(vault_root, rewrite) if rewrite
                             else (True, "", None))
    if not clean:
        return jsonify({"error": f"Refusing to refresh the notes: {detail}.",
                        "reason": "git_not_clean", **payload}), 409

    done = _refresh_write(vault_root, plan)
    if done is None:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    payload.update(done)
    payload["git_restore_commit"] = commit
    # One truth for scripts (2026-09-21 review): the CLI exits non-zero on
    # `ok: false`, so a partial sweep is no longer reported as success.
    payload["ok"] = not (done["failed"] or done["aborted"] or done["cancelled"])
    return jsonify(payload)


def _bootstrap_bases(rows: list[dict], vault_root: Path,
                     wanted: set[str] | None = None) -> dict:
    """Write a base for every unbaselined-and-ready row. Writes no vault file.

    Split out of :func:`api_notesync_bootstrap` so ``/mirror``'s third phase
    is the same code rather than a second implementation of it.
    """
    written: list[str] = []
    rebaselined: list[str] = []
    skipped: dict[str, list[str]] = {}
    for row in rows:
        key = row["citation_key"]
        if wanted is not None and key not in wanted:
            continue
        if row["state"] != "unbaselined":
            skipped.setdefault(row["state"], []).append(key)
            continue
        if not row.get("bootstrap_ready"):
            skipped.setdefault("needs_decision", []).append(key)
            continue
        c = row["ctx"]
        if row.get("legacy_base"):
            # Carry the previous converter's sync point over unchanged: a Zotero
            # edit made since it stays visible as a pull.
            rec = dataclasses.replace(c["base"], converter_id=CONVERTER_ID)
        else:
            rec = BaseRecord(
                citation_key=key, item_key=row["item_key"], note_key=c["note_key"],
                zotero_version=row["zotero_version"], base_html=c["html"],
                base_region=c["parts"][1], synced_at=_now_iso(),
                converter_id=CONVERTER_ID)
        try:
            base_store.save(vault_root, rec)
            (rebaselined if row.get("legacy_base") else written).append(key)
        except OSError as exc:
            skipped.setdefault("write_failed", []).append(f"{key}: {type(exc).__name__}")
    _clear_states_cache()
    return {"ok": True, "written": len(written) + len(rebaselined),
            "keys": written + rebaselined, "rebaselined": rebaselined,
            "skipped": {k: sorted(v) for k, v in skipped.items()}}


@notesync_bp.route("/api/notesync/bootstrap", methods=["POST"])
def api_notesync_bootstrap():
    """Write a base record for every UNBASELINED note that is in step today.

    ``{citation_keys?: [...], all?: true, confirm: true}``. Writes only to the
    app's base store — never the vault, never Zotero. A note whose bootstrap
    test finds something to pull or push is left unbaselined for a decision.
    """
    data = request.get_json(silent=True) or {}
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "confirm: true is required."}), 400
    wanted: set[str] | None = None
    if coerce_bool(data.get("all")) is not True:
        raw = data.get("citation_keys")
        if not isinstance(raw, list) or not raw:
            return jsonify({"error": "citation_keys (non-empty list) or all: true is required."}), 400
        wanted = set()
        for item in raw:
            key = coerce_string_max_len(item, 512)
            if not key:
                return jsonify({"error": "Invalid citation key"}), 400
            wanted.add(key)
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    inv, settings, fresh = ctx
    return jsonify(_bootstrap_bases(_sync_rows(inv, settings, fresh),
                                    Path(settings.vault_root), wanted))


# ---------------------------------------------------------------------------
# Mirror from Zotero — one confirmed verb for create + refresh + baseline
# (2026-09-20). The three writers already existed and all three were reachable
# from the CLI, but the tab offered them as three buttons over two different
# data sources, and one of them — bootstrap — was rendered only when
# zotero-authoritative mode was OFF (`_renderTwoWaySync`), which is the mode
# the maintainer runs. So a reading note written in Zotero could be mirrored
# only by running `papermind notesync create` by hand, and the note it created
# could not be baselined from the app at all. This closes that.
# ---------------------------------------------------------------------------

#: One mirror sweep at a time (the `_ZOTLINK_GATE` posture): the three phases
#: take the obsidian op-lock in turn, so two concurrent sweeps would interleave
#: creates and region writes over one plan each computed before the other ran.
_MIRROR_GATE = threading.Lock()


def _built_at(inv) -> float:
    """The cached scan's own build stamp, or 0 when it cannot be read."""
    try:
        return float(getattr(inv, "built_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _mirror_create_plan(rows: list[dict], fresh: dict) -> tuple[list[str], dict]:
    """Keys whose Obsidian mirror is missing, plus why a row is not offered.

    Driven by the SAME ``_sync_rows`` read as the refresh and the baseline, so
    the sweep has one data source. (The tab's separate "Missing" list comes
    from ``/status``, i.e. the cached scan's Zotero side; here the Zotero half
    is today's read, so a reading note deleted in Zotero since the scan is not
    re-created and one written since IS.)
    """
    keys: list[str] = []
    skipped: dict[str, list[str]] = {}
    for row in rows:
        if row["state"] != "missing_obsidian":
            continue
        key = row["citation_key"]
        notes = (fresh.get(row["item_key"]) or []) if row["item_key"] else []
        if len(notes) != 1:
            # Two reading notes under one item: which one the mirror should
            # hold is a decision. None at all cannot reach here today (a row
            # exists only when one side has something), but saying so beats
            # creating an empty note if that ever changes.
            skipped.setdefault("multiple_notes" if notes else "no_reading_note",
                               []).append(key)
            continue
        keys.append(key)
    return sorted(keys), {k: sorted(v) for k, v in skipped.items()}


def _mirror_plan(inv, settings, fresh, cfg, render_ctx=None) -> tuple[dict, dict]:
    """``(execution plan, public counts)``. Reads every note; writes nothing.

    The counts are what the confirm quotes, so each must be the number the
    sweep will actually produce — ``to_baseline`` in particular is derived,
    not guessed: every created note gets a base from the text we write, every
    refreshed region gets one after its write, and every unchanged row whose
    base is missing or was written by an older converter gets one too.
    """
    plan = _refresh_plan(inv, settings, fresh, cfg)
    rows = plan["rows"]
    create_keys, create_skipped = _mirror_create_plan(rows, fresh)
    create_total = len(create_keys)
    # Bounded by the refactor journal, never chunked past: a second chunk's
    # prune makes the first chunk's ops evictable again, which is exactly the
    # undo loss `_MAX_CREATE` exists to prevent. 100 of N, and say so.
    plan["create_keys"] = create_keys[:_MAX_CREATE]
    # What each create will render from, for the plan digest: the reading note
    # the reviewer was shown (item, note, version, HTML hash). A note edited
    # or replaced in Zotero between review and confirm moves it.
    # 2026-09-21 review: the header comes from the SCAN's record (title,
    # authors, year, DOI) and the destination from the settings, so both are
    # bound too — the sha of the exact text `_render_created_note` would write (pure,
    # cheap) and the vault-relative path. A rescan or a notes-folder change
    # between review and confirm now refuses instead of writing unseen text.
    template = cfg.get("notesync_template") or ""
    render_ctx = render_ctx or _created_note_context(settings, cfg)
    by_key = {r["citation_key"]: r for r in rows}
    create_entries = []
    for key in plan["create_keys"]:
        item_key = by_key[key]["item_key"]
        note = (fresh.get(item_key) or [None])[0]
        rendered = (_render_created_note(_fresh_record(inv.records[key], note), template,
                                         render_ctx)[0]
                    if note is not None and key in inv.records else "")
        create_entries.append([
            key, item_key, str(getattr(note, "note_key", "") or ""),
            int(getattr(note, "version", 0) or 0),
            _sha256_text(getattr(note, "body_html", "") or
                         str(getattr(note, "body_plain", "") or "")),
            _sha256_text(rendered), by_key[key]["note_rel"]])
    plan["digest"] = _plan_digest(plan, cfg, create_entries)

    refresh_bases = len(plan["planned"]) + sum(
        1 for e in plan["keep_base"] if _needs_base(e["row"]))
    covered = ({e["key"] for e in plan["planned"]}
               | {e["key"] for e in plan["keep_base"]})
    # Rows the refresh will not reach (normally none — it baselines every
    # comparable row it touches). Computed as a set difference rather than
    # assumed to be empty, so the number stays honest if that ever changes.
    plan["stragglers"] = sorted(
        r["citation_key"] for r in rows
        if r["state"] == "unbaselined" and r.get("bootstrap_ready")
        and r["citation_key"] not in covered)

    listed = dict(plan["payload"]["listed"])
    # `missing_obsidian` is what this verb CREATES, so reporting it as
    # "listed, not touched" (the refresh's honest answer) would be a lie here.
    listed.pop("missing_obsidian", None)
    if create_total > _MAX_CREATE:
        listed['create_deferred'] = create_keys[_MAX_CREATE:]
    counts = {
        "to_create": len(plan["create_keys"]),
        "create_total": create_total,
        "create_capped_at": _MAX_CREATE if create_total > _MAX_CREATE else None,
        "to_refresh": plan["payload"]["planned"],
        "refresh_keys": plan["payload"]["planned_keys"],
        "to_baseline": len(plan["create_keys"]) + refresh_bases + len(plan["stragglers"]),
        "create_keys": list(plan["create_keys"]),
        "unchanged": plan["payload"]["unchanged"],
        "lossy_markup": plan["payload"]["lossy_markup"],
        "listed": {k: sorted(v) for k, v in listed.items()},
        "stale_bases": plan["payload"]["stale_bases"],
        "skipped": {"create": create_skipped},
        "plan_digest": plan["digest"],
    }
    return plan, counts


def _run_mirror(put, cancel, inv, settings, fresh, cfg, *, dry_run: bool,
                plan_digest: str = "", note_tags: dict | None = None) -> None:
    """Create → refresh → baseline, in that order, reporting each part.

    The order is load-bearing. A freshly created note already holds the
    converter's rendering, so the refresh finds it unchanged — but it has no
    sync point, and the cached scan does not know it exists, so only the third
    phase can give it one (from the text phase 1 wrote, which is the only
    place that evidence lives until the next scan).

    Gates, all decided BEFORE the first write (library-workflow plan lot 1):

    * the plan must be the one the reviewer saw (``plan_digest``, defect 9);
    * git must be able to restore every region this run will REPLACE — per
      file, against one named commit (defect 1). A run that only creates notes
      (journalled, reversible from Settings → History and Restore) and records sync
      points (app state, not the vault) replaces nothing and never consults
      git (lot 4: creation must not be blocked by an unrelated git state).

    Cancellation (defect 8) reaches INSIDE each phase: the creator and the
    refresher poll it before every note. A cancel after some creates still
    records those notes' sync points (cheap, local, and the only place their
    evidence lives until the next scan), then stops; the terminal frame says
    ``stopped: "cancelled"`` with every partial count.
    """
    vault_root = Path(settings.vault_root)
    put({"info": "Reading every note and comparing it with Zotero…"})
    # ONE render context for the plan's digest and the creates below, so the
    # reviewed text and the written bytes come from the same inputs.
    render_ctx = _created_note_context(settings, cfg)
    try:
        plan, out = _mirror_plan(inv, settings, fresh, cfg, render_ctx=render_ctx)
    except NotesFolderUnreadable as exc:
        # Nothing planned, nothing written: a clean stop the orchestrator
        # reads as `failed` (no effect), never an error frame it cannot place.
        put({"mirror": {"ok": False, "dry_run": dry_run, "stopped": "notes_folder_unreadable",
                        "error": str(exc), "created": 0, "refreshed": 0, "bases_written": 0,
                        "failed": {}, "listed": {}, "skipped": {"create": {}},
                        "settled_keys": {}, "pdf_links_unwritten": {},
                        "note_tags_unwritten": {k: "the mirror did not run (notes folder unreadable)"
                                                for k in (note_tags or {})}}})
        return
    rewrite = {e["path"]: e["raw"] for e in plan["planned"]}
    out.update({"dry_run": dry_run, "ok": True, "stopped": None,
                "scanned_at": _built_at(inv),
                "created": 0, "refreshed": 0, "bootstrapped": 0,
                "bases_written": 0, "stale_bases_removed": 0,
                "failed": {}, "aborted": False, "cancelled": False,
                "git_restore_commit": None})
    # Only a run that REPLACES a region needs git; a creation-only run (every
    # create is journalled) must not depend on the repository's state at all.
    git_clean, git_detail, git_commit = (_git_undo_check(vault_root, rewrite)
                                         if rewrite else (True, "", None))
    out.update({"git_clean": bool(git_clean), "git_detail": git_detail or "",
                "git_restore_commit": git_commit})
    if dry_run:
        put({"info": f"Dry run — would create {out['to_create']}, refresh "
                     f"{out['to_refresh']}, baseline {out['to_baseline']}."})
        put({"mirror": out})
        return

    note_tags = note_tags or {}
    created_ok: set[str] = set()
    deferred = set((out.get("listed") or {}).get("create_deferred") or [])
    plan_creates = set(plan["create_keys"])
    # What each created note's render could not honour (N4): ticked tags the
    # vocabulary refused, and a `pdf:` list that could not be inserted.
    render_notes: dict[str, dict] = {}
    # Positive per-key evidence for `settled_keys` (L1). `html_by_key` is the
    # Zotero note HTML each decision was made from: its sha256 is what the
    # import tracker compares with the note it observed (same bytes — the
    # stored `itemNotes.note` column), so a note edited in Zotero while the
    # sweep ran reopens instead of being closed on stale text.
    created_html: dict[str, str] = {}
    refreshed_keys: list[str] = []
    in_sync_keys: list[str] = []
    html_by_key = {e["key"]: e["row"]["ctx"]["html"]
                   for e in (*plan["planned"], *plan["keep_base"])}
    bootstrap_skipped: set[str] = set()

    def _settled() -> dict:
        """``{citation_key: note_sha256}`` for every note this run left
        mirrored: created, refreshed, or already equal to Zotero — minus any
        key that also failed (a failed base save counts: it has no sync point)
        and any the baseline phase skipped.

        Audit remediation 2026-09-23 (L1). The orchestrator used to close a
        note unit whenever its key was absent from ``failed`` and ``listed``,
        so a record the cached scan does not know (no sync row at all) was
        closed ``done`` and never reopened. Only these keys may close a unit.
        """
        failed = set(out["failed"])
        settled: dict[str, str] = {}
        for key, html in created_html.items():
            settled[key] = _sha256_text(html)
        for key in (*refreshed_keys, *in_sync_keys):
            settled[key] = _sha256_text(html_by_key.get(key) or "")
        return {k: v for k, v in sorted(settled.items())
                if k not in failed and k not in bootstrap_skipped}

    def _unwritten() -> dict:
        """Ticked note tags that did not land in a created note, and why.

        A key whose note already exists is not listed: its tags are the tag
        step's job. Reported rather than silent (review 2026-09-22), because
        the tag step then reads the note target as "skipped". A ticked tag
        the vocabulary refused is reported too (N4, 2026-09-23) — the note
        was created without it.
        """
        miss: dict[str, str] = {}
        for key in note_tags:
            if key in created_ok:
                if render_ctx.get("vocab") is None:
                    miss[key] = "the tag vocabulary (_tags.md) could not be read"
                elif (render_notes.get(key) or {}).get("rejected_tags"):
                    miss[key] = ("not in the tag vocabulary: "
                                 + ", ".join(render_notes[key]["rejected_tags"]))
            elif key in deferred:
                miss[key] = "the note was deferred to the next run (over the create limit)"
            elif key in plan_creates:
                miss[key] = "the note was not created in this run"
        return miss

    def _pdf_unwritten() -> dict:
        """Created notes whose ``pdf:`` list could not be inserted (N4)."""
        return {k: f"the pdf: list could not be written ({n['pdf_links']})"
                for k, n in sorted(render_notes.items())
                if k in created_ok and n.get("pdf_links")}

    def _finish_frame() -> None:
        out["note_tags_unwritten"] = _unwritten()
        out["pdf_links_unwritten"] = _pdf_unwritten()
        out["settled_keys"] = _settled()

    def _stop(reason: str, error: str | None = None) -> None:
        out.update({"ok": False, "stopped": reason})
        if error:
            out["error"] = error
        _finish_frame()
        put({"mirror": out})

    if plan_digest != plan["digest"]:
        out.pop("plan_digest", None)       # never a digest to replay (see refresh-all)
        _stop("plan_changed", "The notes or Zotero changed since the dry run. "
                              "Review the new plan before confirming.")
        return
    if not git_clean:
        _stop("git_not_clean", f"Refusing to mirror the notes: {git_detail}.")
        return

    # --- 1. create the missing mirrors -----------------------------------
    created_bases: list[dict] = []
    create_lock_lost = False
    if plan["create_keys"]:
        if cancel.is_set():
            _stop("cancelled")
            return
        put({"info": f"Creating {len(plan['create_keys'])} missing note(s)…"})
        res, status = _create_notes(inv, settings, cfg, plan["create_keys"],
                                    fresh=fresh, should_cancel=cancel.is_set,
                                    render_ctx=render_ctx, note_tags=note_tags)
        # A journal failure can follow landed creates. Preserve their exact
        # counts and per-key evidence before testing the HTTP status; the
        # caller must not interpret a stopped sweep as zero side effects.
        out["created"] = res.get("created", 0)
        out["create_results"] = res.get("results") or {}
        for key, r in (res.get("results") or {}).items():
            if r.get("ok"):
                created_ok.add(key)
                render_notes[key] = r.get("render_notes") or {}
            else:
                out["failed"][key] = f"create: {r.get('reason', 'failed')}"
        if status != 200:
            # Do not start unrelated refreshes after a creation/journal
            # failure. Already-created notes remain visible in the outcome.
            _stop("create_failed", res.get("error"))
            return
        created_bases = res["created_bases"]
        created_html.update({b["key"]: b["html"] for b in created_bases})
        if res.get("not_reached"):
            out["not_reached"] = {"create": list(res["not_reached"])}
        create_lock_lost = res.get("aborted") == "lock_lost"
        for key, r in res["results"].items():
            if not r.get("ok"):
                out["failed"][key] = f"create: {r.get('reason', 'failed')}"
        put({"info": f"Created {res['created']} note(s)."})

    def _base_created() -> None:
        """Sync points for the notes phase 1 wrote, from the text it wrote."""
        n = 0
        for b in created_bases:
            parts = split_note_parts(b["text"])
            if parts is None:
                # A custom template with no marker pair and no single rule: the
                # note is written, but there is no region to record a base for.
                out["failed"][b["key"]] = "base: no region in the written note"
                continue
            try:
                _save_base(vault_root, key=b["key"], item_key=b["item_key"],
                           note_key=b["note_key"], version=b["version"],
                           html=b["html"], region=parts[1])
                n += 1
            except OSError as exc:
                out["failed"][b["key"]] = f"base: {type(exc).__name__}"
        out["bootstrapped"] += n
        out["bases_written"] += n

    if create_lock_lost:
        # N12: record the sync points of what landed (cheap, local), then
        # stop — the refresh must not run on a lock that is someone else's.
        _base_created()
        out["aborted"] = True
        _stop("lock_lost", "The vault lock was lost while creating notes; "
                           "the rest was not reached.")
        return

    # --- 2. refresh every region that differs ----------------------------
    if cancel.is_set():
        _base_created()
        out["cancelled"] = True
        _stop("cancelled")
        return
    put({"info": f"Refreshing {len(plan['planned'])} region(s) from Zotero…"})
    done = _refresh_write(
        vault_root, plan,
        progress=lambda i, n: put({"info": f"Refreshed {i} of {n} region(s)…"}),
        should_cancel=cancel.is_set)
    if done is None:
        _base_created()
        _stop("indexing", "Indexing is in progress; try again shortly.")
        return
    if done.get("not_reached"):
        out.setdefault("not_reached", {})["refresh"] = done["not_reached"]
    refreshed_keys.extend(done.get("written_keys") or [])
    if not done["aborted"] and not done["cancelled"]:
        # Phase 2 walked every already-equal note (and baselined the ones that
        # needed it), so they are mirrored too.
        in_sync_keys.extend(e["key"] for e in plan["keep_base"])
    out.update({"refreshed": done["written"], "aborted": done["aborted"],
                "bases_written": done["bases_written"],
                "stale_bases_removed": done["stale_bases_removed"],
                "stale_bases_kept": done["stale_bases_kept"]})
    for key, reason in done["failed"].items():
        out["failed"][key] = f"refresh: {reason}"
    put({"info": f"Refreshed {done['written']} note(s)."})

    # --- 3. record a sync point for everything still without one ---------
    put({"info": "Recording sync points…"})
    _base_created()
    if done["cancelled"] or cancel.is_set():
        out["cancelled"] = True
        _clear_states_cache()
        _stop("cancelled")
        return
    # Re-read: phases 1 and 2 moved every fingerprint the row memo keys on.
    rows2 = _sync_rows(inv, settings, fresh)
    wanted = {r["citation_key"] for r in rows2
              if r["state"] == "unbaselined" and r.get("bootstrap_ready")}
    if wanted:
        res = _bootstrap_bases(rows2, vault_root, wanted)
        out["bootstrapped"] += res["written"]
        out["bases_written"] += res["written"]
        for reason, keys in (res.get("skipped") or {}).items():
            out["skipped"].setdefault("bootstrap", {}).setdefault(reason, []).extend(keys)
            bootstrap_skipped.update(keys)
    _clear_states_cache()
    out["ok"] = not out["failed"] and not out["aborted"]
    _finish_frame()
    put({"mirror": out})


#: A request-size guard only. The route applies tags to the notes it CREATES
#: and ignores every other key, so this is not tied to ``_MAX_CREATE``: tying
#: it made a run with more than 100 tagged papers lose its whole mirror
#: (review 2026-09-22). The orchestrator also narrows to the reviewed
#: ``create_keys`` when it knows them.
_NOTE_TAGS_MAX_KEYS = 10_000
_NOTE_TAGS_MAX_PER_KEY = 50


def _parse_note_tags(raw):
    """``(dict, None)`` or ``(None, (response, 400))`` for the mirror's ``note_tags``."""
    if raw is None:
        return {}, None
    bad = (jsonify({"error": "note_tags must be an object {citation_key: [tags]} "
                             f"with at most {_NOTE_TAGS_MAX_KEYS} keys and "
                             f"{_NOTE_TAGS_MAX_PER_KEY} tags each."}), 400)
    if not isinstance(raw, dict) or len(raw) > _NOTE_TAGS_MAX_KEYS:
        return None, bad
    out: dict[str, list[str]] = {}
    for key, tags in raw.items():
        k = coerce_string_max_len(key, 512)
        if not k or any(c in k for c in ("\x00", "\n", "\r", "/", "\\")):
            return None, bad
        if not isinstance(tags, list) or len(tags) > _NOTE_TAGS_MAX_PER_KEY:
            return None, bad
        clean = []
        for t in tags:
            tag = coerce_string_max_len(t, 128)
            if not tag:
                return None, bad
            clean.append(tag)
        out[k] = clean
    return out, None


@notesync_bp.route("/api/notesync/mirror", methods=["POST"])
def api_notesync_mirror():
    """Create, refresh and baseline the Obsidian mirror — one confirmed verb.

    ``{dry_run: true}`` or ``{confirm: true, plan_digest}`` → SSE, terminal
    frame ``{"mirror": {...}}``. A dry run plans all three phases, writes
    nothing and returns the ``plan_digest`` the confirm must send back; the
    confirm executes only that plan (``stopped: "plan_changed"`` otherwise).

    Three phases, in this order: ``create`` the missing notes (journalled,
    reversible from Settings → History and Restore), rewrite every region that
    differs (NOT journalled — the vault's git is its undo, proven per file
    against a named commit), then a base for everything still without a sync
    point (writes only the app's own records). Mixed undo semantics by
    construction, which is why the confirm has to name all three.

    Gate order: ``mode_off``, a missing digest, no scan and an unreadable
    Zotero are refused with a real status before the stream. The digest match
    and the git proof need the plan, so they run first thing in the worker —
    still before the first note is created (see :func:`_run_mirror`).

    ``note_tags`` (optional, ``{citation_key: [tags]}``, follow-ups lot 2,
    2026-09-22): tags the user ticked in an "Update my library" run for notes
    THIS sweep creates. Bounded and validated here, vocabulary-gated at the
    render, merged only into created notes and never digest-bound (the user
    reviewed them). A malformed value is a 400 before anything runs.

    Delegates to the three existing writers; no new write surface.
    """
    data = request.get_json(silent=True) or {}
    confirm = coerce_bool(data.get("confirm")) is True
    dry_run = not confirm or coerce_bool(data.get("dry_run")) is True
    cfg = load_config()
    if not _authoritative(cfg):
        return jsonify({"error": "Zotero-authoritative note sync is off (Settings → "
                        "Zotero → Obsidian note sync).", "reason": "mode_off"}), 409
    reviewed = coerce_string_max_len(data.get("plan_digest"), 128) or ""
    if not dry_run and not reviewed:
        return jsonify({"error": "Run a dry run first and confirm with its plan_digest.",
                        "reason": "plan_digest_required"}), 400
    note_tags, err = _parse_note_tags(data.get("note_tags"))
    if err is not None:
        return err
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    inv, settings, fresh = ctx
    try:
        if settings_context(settings) != settings_context(load_settings()):
            return jsonify({"error": "Library context changed. Run a fresh audit before writing.",
                            "reason": "stale_audit_context"}), 409
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400

    # Acquired LAST, after every pre-stream refusal has had its chance to
    # return — an earlier acquire leaks the gate on each `return jsonify(...)`
    # (the 2026-09-02 zotlink 429-until-restart leak).
    if not _MIRROR_GATE.acquire(blocking=False):
        return jsonify({"error": "Another note mirror sweep is running; "
                                 "wait for it to finish."}), 429

    consumer_timeout_s = online_aware_floor_s(cfg) + SSE_STALL_MARGIN_S

    def _worker(put, cancel):
        _run_mirror(put, cancel, inv, settings, fresh, cfg, dry_run=dry_run,
                    plan_digest=reviewed, note_tags=note_tags)

    # release= is owned by the skeleton's _SSEOpGuard: a client that takes the
    # 200 and disconnects without draining never runs a worker `finally`.
    return run_sse_worker(_worker, consumer_timeout_s=consumer_timeout_s,
                          release=_MIRROR_GATE.release)


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Pull / push / merge / create-in-Zotero (Phase 4, 2026-09-04)
# ---------------------------------------------------------------------------

#: Bulk pull rides the refactor journal like create; same bound, same reason.
_PULL_MAX_KEYS = _MAX_CREATE
#: Bulk push: every note written to Zotero is a permanently non-evictable
#: tagging-journal op (the attachment de-link's bound and reason).
_PUSH_MAX_KEYS = 50
#: A merge result is one note's region; anything larger is not a merge.
_MERGE_MAX_CHARS = 400_000


def _parse_keys(data: dict, cap: int):
    raw = data.get("citation_keys")
    if not isinstance(raw, list) or not raw:
        return None, (jsonify({"error": "citation_keys must be a non-empty list"}), 400)
    if len(raw) > cap:
        return None, (jsonify({"error": f"At most {cap} notes per request."}), 400)
    keys = []
    for item in raw:
        key = coerce_string_max_len(item, 512)
        if not key or any(c in key for c in ("\x00", "\n", "\r", "/", "\\")):
            return None, (jsonify({"error": "Invalid citation key"}), 400)
        keys.append(key)
    return list(dict.fromkeys(keys)), None


def _fresh_record(rec, note):
    """*rec* with its Zotero item's child notes replaced by the FRESH note, so
    the region renderer reads today's text rather than the scan's."""
    import dataclasses
    from types import SimpleNamespace
    item = rec.zotero_item
    try:
        item2 = dataclasses.replace(item, child_notes=[note])
    except TypeError:
        item2 = SimpleNamespace(**{**vars(item), "child_notes": [note]})
    try:
        return dataclasses.replace(rec, zotero_item=item2)
    except TypeError:
        return SimpleNamespace(**{**vars(rec), "zotero_item": item2})


def _framed(text: str) -> str:
    """A merge result framed the way every region is: one blank line inside
    each boundary. ``split_note_parts`` hands the same shape back."""
    return "\n" + (text or "").replace("\r\n", "\n").strip("\n") + "\n"


def _save_base(vault_root: Path, *, key: str, item_key: str, note_key: str,
               version: int, html: str, region: str, push_version: int = 0) -> None:
    base_store.save(vault_root, BaseRecord(
        citation_key=key, item_key=item_key, note_key=note_key,
        zotero_version=int(version or 0), base_html=html, base_region=region,
        synced_at=_now_iso(), converter_id=CONVERTER_ID,
        last_push_version=int(push_version or 0)))


def _region_writes(vault_root: Path, cfg: dict, items: list[dict], category: str) -> dict:
    """Write several notes' regions under ONE op-lock hold, each journalled
    (refactor journal, kind ``notesync_update``) and re-read under the lock.

    *items*: ``[{key, path, raw, proposed}]``. Returns ``{key: {ok, op_id |
    reason}}``. Checkpoints every 25 writes and prunes once with this batch's
    ops protected — the create route's cadence.
    """
    from rag.vault import obsidian_manager
    from vault_history import journal
    from vault_history._write import journalled_write_note

    results: dict[str, dict] = {}
    op_epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not op_epoch:
        return {it["key"]: {"ok": False, "reason": "indexing"} for it in items}
    op_ids: set[str] = set()
    written = 0
    try:
        jman = journal.load(vault_root, cfg)
        for i, it in enumerate(items, 1):
            if i % _JOURNAL_FLUSH_EVERY == 0 and not obsidian_manager.heartbeat(op_epoch):
                results[it["key"]] = {"ok": False, "reason": "lock_lost"}
                break
            path: Path = it["path"]
            if _became_symlink(path):
                results[it["key"]] = {"ok": False, "reason": "became_symlink"}
                continue
            try:
                current = path.read_bytes()
                rel = str(path.relative_to(vault_root))
            except (OSError, ValueError) as exc:
                results[it["key"]] = {"ok": False, "reason": type(exc).__name__}
                continue
            if hashlib.sha256(current).hexdigest() != _sha256_text(it["raw"]):
                results[it["key"]] = {"ok": False, "reason": "changed_on_disk"}
                continue
            op_id, status, err = journalled_write_note(
                vault_root, cfg, rel, current, _sha256_text(it["raw"]),
                _sha256_text(it["proposed"]), it["proposed"], _UPDATE_OP_KIND, jman,
                log_category=category, pre_write_persist=bool(it.get('before_write')),
                before_write=(lambda: obsidian_manager.heartbeat(op_epoch) and it['before_write']())
                    if it.get('before_write') else None)
            if status == "applied":
                written += 1
                op_ids.add(op_id)
                results[it["key"]] = {"ok": True, "op_id": op_id, "rel": rel}
                if written % _JOURNAL_FLUSH_EVERY == 0:
                    journal.save(vault_root, cfg, jman)
            else:
                results[it["key"]] = {"ok": False, "reason": err or "write_failed"}
        journal.prune(vault_root, cfg, jman, protect_op_ids=op_ids)
        journal.save(vault_root, cfg, jman)
    finally:
        obsidian_manager.release_lock(op_epoch)
    return results


#: The states with a comparable region AND a comparable Zotero note — the
#: only rows either direction can act on. Everything else (`missing_*`,
#: `unparsed`, `multiple_notes`) has no region to write or nothing to write
#: from, and is refused by the `ctx` test the routes apply first.
_COMPARABLE_STATES = frozenset({"in_sync", "pull", "push", "conflict", "unbaselined"})


def _needs_accept(row: dict, direction: str) -> bool:
    """Would this direction DELETE something the other side has?

    Decided by what changed, never by the state's NAME (2026-09-07). The
    state is a summary of the two booleans below, so testing it was a lossy
    restatement — and it left a real gap: a row summarised as ``push`` could
    never be pulled, not even with ``accept_conflict``, so "throw my edit
    away and take Zotero's" was inexpressible on precisely the rows where a
    user most wants to say it.

    * ``pull`` overwrites the region, so it needs acceptance whenever the
      region moved since the sync point — or, with no sync point, whenever
      the region holds words Zotero does not have.
    * ``push`` overwrites the Zotero note, so it needs acceptance whenever
      Zotero moved — or, unbaselined, whenever Zotero holds words the region
      lacks.

    (``_push_many`` keeps its own, orthogonal acceptance check against the
    *live API* HTML, which catches a Zotero edit the local SQLite has not
    synced down yet.)
    """
    if row.get("baselined"):
        return bool(row["region_changed"] if direction == "pull"
                    else row["zotero_changed"])
    return bool(row["extra_words"] if direction == "pull"
                else row["missing_words"])


def _pullable(row: dict, accept_conflict: bool) -> bool:
    if row["state"] not in _COMPARABLE_STATES:
        return False
    return accept_conflict or not _needs_accept(row, "pull")


def _pushable(row: dict, accept_conflict: bool) -> bool:
    if row["state"] not in _COMPARABLE_STATES:
        return False
    return accept_conflict or not _needs_accept(row, "push")


@notesync_bp.route("/api/notesync/pull", methods=["POST"])
def api_notesync_pull():
    """Zotero → Obsidian for the given keys. ``{citation_keys, confirm,
    accept_conflict?}``. ANY comparable row may be pulled; one whose region
    has moved since the sync point needs ``accept_conflict: true`` — "yes,
    write" and "yes, delete what I wrote there" stay two decisions.
    Journalled as ``notesync_update``; the base is written after each
    success."""
    data = request.get_json(silent=True) or {}
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "confirm: true is required."}), 400
    keys, err = _parse_keys(data, _PULL_MAX_KEYS)
    if err is not None:
        return err
    accept = coerce_bool(data.get("accept_conflict")) is True
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    inv, settings, fresh = ctx
    try:
        load_settings()
    except AuditConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    cfg = load_config()
    template = cfg.get("notesync_template") or ""
    vault_root = Path(settings.vault_root)
    rows = {r["citation_key"]: r for r in _sync_rows(inv, settings, fresh)}
    results: dict[str, dict] = {}
    items: list[dict] = []
    for key in keys:
        row = rows.get(key)
        if row is None or not row.get("ctx"):
            results[key] = {"ok": False, "reason": (row or {}).get("state") or "no_record"}
            continue
        if not _pullable(row, accept):
            results[key] = {"ok": False, "reason": row["state"],
                            "needs_accept": _needs_accept(row, "pull")}
            continue
        c = row["ctx"]
        rec_fresh = _fresh_record(inv.records[key], c["note"])
        plan, refusal = _plan_update(rec_fresh, c["raw"], template)
        if refusal is not None:
            results[key] = {"ok": False, "reason": refusal["reason"]}
            continue
        if not plan["changed"]:
            _save_base(vault_root, key=key, item_key=row["item_key"], note_key=c["note_key"],
                       version=row["zotero_version"], html=c["html"], region=c["parts"][1])
            results[key] = {"ok": True, "changed": False}
            continue
        items.append({"key": key, "path": c["path"], "raw": c["raw"],
                      "proposed": plan["proposed"], "row": row})
    if items:
        for key, res in _region_writes(vault_root, cfg, items, "notesync_pull").items():
            results[key] = res
        for it in items:
            if results.get(it["key"], {}).get("ok"):
                parts = split_note_parts(it["proposed"])
                row = it["row"]
                _save_base(vault_root, key=it["key"], item_key=row["item_key"],
                           note_key=row["ctx"]["note_key"], version=row["zotero_version"],
                           html=row["ctx"]["html"],
                           region=parts[1] if parts else it["proposed"])
    _clear_states_cache()
    pulled = sum(1 for r in results.values() if r.get("ok") and r.get("changed", True))
    return jsonify({"ok": True, "pulled": pulled, "results": results})


def _settle_unknown_note_write(vault_root, cfg, key):
    """``None`` when ``key`` has no ``outcome_unknown`` note write (or one was
    just proven to have landed), else a refusal dict.

    Decision D2 (2026-09-17 review, B2): before refusing, read the note ONCE,
    lock-free. If it holds exactly the journalled ``after_html`` the op is
    flipped to ``applied`` (recording the observed version) under a short
    hold on a RELOADED manifest; otherwise the refusal names what was found."""
    from rag.vault import obsidian_manager
    from tagging import journal as tjournal
    try:
        manifest = tjournal.load(vault_root, cfg)
    except (tjournal.JournalUnreadableError, tjournal.ScopeError) as exc:
        return {"ok": False, "status": "error", "message": sanitise_error_msg(exc)}
    unknown = [op for op in manifest.get("ops", []) if op.get("kind") == zotero_notes.OP_WRITE
               and op.get("citation_key") == key and op.get("state") == "outcome_unknown"]
    if not unknown:
        return None
    if len(unknown) > 1:
        return {"ok": False, "status": "outcome_unknown",
                "message": "Several earlier note writes have unknown outcomes; review them in Tagging → Restore."}
    op = dict(unknown[0])
    seen = zotero_notes.observe_unknown(op)
    if seen["status"] == "unreadable":
        return {"ok": False, "status": "outcome_unknown",
                "message": "An earlier note write has an unknown outcome and Zotero could not be read to verify it: "
                           + seen.get("message", "")}
    if seen["status"] == "different":
        return {"ok": False, "status": "outcome_unknown", "found_version": seen.get("version"),
                "message": f"An earlier note write has an unknown outcome and Zotero holds different text "
                           f"(version {seen.get('version')}); review it in Tagging → Restore before another write."}
    epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not epoch:
        return {"ok": False, "status": "busy", "message": "Indexing is in progress; try again shortly."}
    try:
        latest = tjournal.load(vault_root, cfg)
        retained = tjournal.find_op(latest, op["op_id"])
        if retained is None or retained.get("state") != "outcome_unknown":
            return None
        retained["state"] = "applied"
        retained["version_after"] = seen.get("version")
        retained["observed_after_unknown"] = True
        tjournal.save(vault_root, cfg, latest)
    except (tjournal.JournalUnreadableError, tjournal.ScopeError) as exc:
        return {"ok": False, "status": "error", "message": sanitise_error_msg(exc)}
    finally:
        obsidian_manager.release_lock(epoch)
    return None


def _push_many(vault_root: Path, cfg: dict, jobs: list[dict]) -> dict[str, dict]:
    """The push choreography for ``[{key, zot, note_key, after_html, base, row}]``:
    reads lock-free → ONE hold to journal → PATCHes lock-free → ONE hold to
    record outcomes. Returns ``{key: {ok, status, version?, op_id?}}``."""
    from rag.vault import obsidian_manager
    from tagging import journal as tjournal

    results: dict[str, dict] = {}
    prepared: dict[str, dict] = {}
    # Phase 1 — reads, no lock.
    for j in jobs:
        refusal = _settle_unknown_note_write(vault_root, cfg, j["key"])
        if refusal is not None:
            results[j["key"]] = refusal
            continue
        p = zotero_notes.prepare_write(cfg, zot=j["zot"], note_key=j["note_key"],
                                       after_html=j["after_html"], **({"expected_html": j["expected_html"]} if "expected_html" in j else {}),
                                       **({'expected_version': j['expected_version']} if 'expected_version' in j else {}))
        if "early" in p:
            results[j["key"]] = p["early"]
            continue
        base = j.get("base")
        if base is not None and not same_note_html(p["commit"]["before_html"], base.base_html) \
                and not j.get("accept_conflict"):
            # The API is fresher than the local SQLite: Zotero moved since the
            # base and the listing could not see it. A push here would delete
            # that edit — exactly the conflict case, decided by the user.
            results[j["key"]] = {"ok": False, "status": "conflict", "needs_accept": True,
                                 "message": "The Zotero note changed since the last sync."}
            continue
        prepared[j["key"]] = p
    if not prepared:
        return results
    # Phase 2 — journal every write under one brief hold, persist, release.
    op_epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not op_epoch:
        for k in prepared:
            results[k] = {"ok": False, "status": "busy",
                          "message": "Indexing is in progress; try again shortly."}
        return results
    ops: dict[str, dict] = {}
    try:
        manifest = tjournal.load(vault_root, cfg)
        for k, p in list(prepared.items()):
            if any(op.get('kind') == zotero_notes.OP_WRITE and op.get('citation_key') == k
                   and op.get('state') == 'outcome_unknown' for op in manifest.get('ops', [])):
                results[k] = {'ok': False, 'status': 'outcome_unknown', 'message': 'An earlier note write requires inspection; no replay.'}
                del prepared[k]
                continue
            ops[k] = zotero_notes.journal_write(prepared=p, citation_key=k, manifest=manifest)
        tjournal.save(vault_root, cfg, manifest)
    except (tjournal.JournalUnreadableError, tjournal.ScopeError) as exc:
        for k in prepared:
            results[k] = {"ok": False, "status": "error", "message": sanitise_error_msg(exc)}
        return results
    finally:
        obsidian_manager.release_lock(op_epoch)
    # Phase 3 — PATCHes, lock-free.
    outcomes: dict[str, tuple[dict, int | None]] = {}
    for k, p in prepared.items():
        job = next(j for j in jobs if j['key'] == k)
        guard = job.get('before_write')
        if guard is not None and guard() is not True:
            outcomes[k] = ({'ok': False, 'status': 'conflict',
                            'message': 'Local note, identity, converter or base changed before dispatch.'}, None)
        else:
            outcomes[k] = zotero_notes.patch_write(p)
    # Phase 4 — record outcomes on a RELOADED manifest.
    recorded = set()
    epoch2 = obsidian_manager.try_acquire_lock(ttl=600)
    if epoch2:
        try:
            manifest = tjournal.load(vault_root, cfg)
            for k, (res, version) in outcomes.items():
                op = tjournal.find_op(manifest, ops[k]["op_id"])
                if op is None:
                    continue
                if res.get("ok"):
                    op["version_after"] = version
                    op["state"] = "applied"
                else:
                    op["state"] = ("failed" if res.get("status") in zotero_notes.NOT_WRITTEN_STATUSES
                                   else "outcome_unknown")
            tjournal.save(vault_root, cfg, manifest)
            recorded = {k for k in outcomes if tjournal.find_op(manifest, ops[k]["op_id"]) is not None}
        except (tjournal.JournalUnreadableError, tjournal.ScopeError):
            pass
        finally:
            obsidian_manager.release_lock(epoch2)
    for k, (res, version) in outcomes.items():
        out = dict(res)
        out["op_id"] = ops[k]["op_id"]
        if k not in recorded:
            out.update(ok=False, status="outcome_unknown", message="Destination result could not be journalled; inspect before recovery.")
        if out.get("ok"):
            out["version"] = version
            j = next(x for x in jobs if x["key"] == k)
            if not j.get("defer_base"):
                _save_base(vault_root, key=k, item_key=j["row"]["item_key"], note_key=j["note_key"],
                           version=version or 0, html=j["after_html"], region=j["region"],
                           push_version=version or 0)
        results[k] = out
    return results


@notesync_bp.route("/api/notesync/push", methods=["POST"])
def api_notesync_push():
    """Obsidian → Zotero for the given keys. ``{citation_keys, confirm,
    accept_conflict?}``. ANY comparable row may be pushed; one whose Zotero
    note has moved since the sync point needs ``accept_conflict: true``.
    Each write is a ``zotero_note_write`` op in the tagging journal,
    reversible from Tagging → Restore."""
    if _authoritative(load_config()):
        return jsonify({"error": "Zotero is authoritative for reading notes (Settings → "
                        "Zotero → Obsidian note sync): the Obsidian note is a mirror and "
                        "is never written to Zotero. Use refresh-all instead.",
                        "reason": "authoritative"}), 409
    data = request.get_json(silent=True) or {}
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "confirm: true is required."}), 400
    keys, err = _parse_keys(data, _PUSH_MAX_KEYS)
    if err is not None:
        return err
    accept = coerce_bool(data.get("accept_conflict")) is True
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    inv, settings, fresh = ctx
    cfg = load_config()
    vault_root = Path(settings.vault_root)
    rows = {r["citation_key"]: r for r in _sync_rows(inv, settings, fresh)}
    results: dict[str, dict] = {}
    jobs: list[dict] = []
    for key in keys:
        row = rows.get(key)
        if row is None or not row.get("ctx"):
            results[key] = {"ok": False, "status": (row or {}).get("state") or "no_record"}
            continue
        if not _pushable(row, accept):
            results[key] = {"ok": False, "status": row["state"],
                            "needs_accept": _needs_accept(row, "push")}
            continue
        c = row["ctx"]
        region = c["parts"][1]
        jobs.append({"key": key, "zot": inv.records[key].zotero_item, "note_key": c["note_key"],
                     "after_html": md_to_html(region.strip("\n")), "region": region,
                     "base": c["base"], "row": row, "accept_conflict": accept})
    if jobs:
        results.update(_push_many(vault_root, cfg, jobs))
    _clear_states_cache()
    pushed = sum(1 for r in results.values() if r.get("ok") and r.get("status") == "applied")
    return jsonify({"ok": True, "pushed": pushed, "results": results,
                    "note": ("A push reaches the local Zotero database only after "
                             "Zotero syncs down; until then the listing may still "
                             "show the previous text.")})


def _merge_evidence(row):
    c = row['ctx']
    return _sha256_text(json.dumps({
        'raw': c['raw'], 'path': str(c['path'].resolve()), 'html': c['html'], 'version': row['zotero_version'],
        'item': row['item_key'], 'scope': row.get('native_scope'), 'note': c['note_key'], 'converter': CONVERTER_ID,
        'base': c['base'].to_json() if c['base'] else None,
    }, sort_keys=True))


@notesync_bp.route("/api/notesync/merge-preview", methods=["POST"])
def api_notesync_merge_preview():
    return _merge_preview(request.get_json(silent=True) or {})


def _merge_preview(data, *, context=None):
    """The three texts of one note and a pre-filled three-way merge. Writes nothing."""
    key = coerce_string_max_len(data.get("citation_key"), 512)
    if not key:
        return jsonify({"error": "citation_key is required."}), 400
    ctx, err = (context, None) if context is not None else _load_sync_context()
    if err is not None:
        return err
    inv, settings, fresh = ctx
    row = next((r for r in _sync_rows(inv, settings, fresh) if r["citation_key"] == key), None)
    if row is None or not row.get("ctx"):
        return jsonify({"error": "No comparable note for that key.",
                        "state": (row or {}).get("state")}), 404
    c = row["ctx"]
    ours = c["parts"][1].replace("\r\n", "\n").strip("\n")
    theirs = c["md_now"].strip("\n")
    base = c["base"].base_region.replace("\r\n", "\n").strip("\n") if c["base"] else ""
    merged, conflicts = three_way(base, ours, theirs)
    return jsonify({
        "citation_key": key, "note_rel": row["note_rel"], "state": row["state"],
        "baselined": row["baselined"], "base": base, "ours": ours, "theirs": theirs,
        "identity": {"scope": row.get("native_scope"), "note_key": c["note_key"], "path": str(c["path"].resolve())},
        "merged": merged, "conflicts": conflicts,
        "evidence": _merge_evidence(row), "content_issues": row.get("content_issues", []),
        "converter_id": CONVERTER_ID,
        "content_sha256": _sha256_text(c["raw"]), "zotero_version": row["zotero_version"],
        "base_evidence": _sha256_text(json.dumps(c['base'].to_json() if c['base'] else None, sort_keys=True)),
    })


@notesync_bp.route("/api/notesync/merge", methods=["POST"])
def api_notesync_merge():
    return _apply_merge(request.get_json(silent=True) or {})


def _apply_merge(data):
    """Shared GUI/campaign writer; guards and journals stay authoritative."""
    if _authoritative(load_config()):
        return jsonify({"error": "Zotero is authoritative for reading notes (Settings → "
                        "Zotero → Obsidian note sync): the Obsidian note is a mirror and "
                        "is never written to Zotero. Use refresh-all instead.",
                        "reason": "authoritative"}), 409
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "confirm: true is required."}), 400
    key = coerce_string_max_len(data.get("citation_key"), 512)
    if not key:
        return jsonify({"error": "citation_key is required."}), 400
    result = data.get("result")
    if not isinstance(result, str) or len(result) > _MERGE_MAX_CHARS:
        return jsonify({"error": "result must be a string within the size cap."}), 400
    if has_conflict_markers(result):
        return jsonify({"error": "The result still contains conflict markers; resolve them first.",
                        "reason": "conflict_markers"}), 400
    targets = data.get("targets") if isinstance(data.get("targets"), dict) else {}
    to_obsidian = coerce_bool(targets.get("obsidian")) is True
    to_zotero = coerce_bool(targets.get("zotero")) is True
    if not to_obsidian and not to_zotero:
        return jsonify({"error": "Choose at least one target."}), 400
    client_sha = coerce_string_max_len(data.get("content_sha256"), 64) or ""
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    inv, settings, fresh = ctx
    cfg = load_config()
    vault_root = Path(settings.vault_root)
    _clear_states_cache()
    row = next((r for r in _sync_rows(inv, settings, fresh) if r["citation_key"] == key), None)
    if row is None or not row.get("ctx"):
        return jsonify({"error": "No comparable note for that key."}), 404
    c = row["ctx"]
    converter = CONVERTER_ID
    original_base = c['base']
    original_identity = (row.get('native_scope'), row['item_key'], c['note_key'], str(c['path'].resolve()))
    def local_guard(expected):
        # Reuse bounded descriptor reads: no symlink or concurrent replacement.
        from accession.dependent_links import _read, MAX_NOTE_BYTES
        try:
            # Resolved on BOTH sides (2026-09-17 review, F12): a notes folder that
            # is a symlink inside the vault reads through its real path, and one
            # pointing outside the vault fails relative_to — never followed.
            root = vault_root.resolve()
            raw, _ = _read(root, c['path'].resolve().relative_to(root).as_posix(), MAX_NOTE_BYTES)
            zot = inv.records[key].zotero_item
            identity = ({'database': getattr(zot, 'server_id', None),
                         'library': getattr(zot, 'library_id', None), 'item': zot.item_key},
                        zot.item_key, c['note_key'], str(c['path'].resolve()))
            if original_base is None and base_store._path(vault_root, key).exists():
                return False  # An unreadable/corrupt record is not an absent base.
            return (raw == expected and base_store.load(vault_root, key) == original_base
                    and CONVERTER_ID == converter and identity == original_identity)
        except (OSError, ValueError):
            return False
    if client_sha != _sha256_text(c["raw"]):
        return jsonify({"error": "That note changed on disk since the preview.",
                        "reason": "stale"}), 409
    if data.get('evidence') != _merge_evidence(row):
        return jsonify(error='The notes or sync base changed; obtain a new preview.', reason='stale'), 409
    issues = row.get('content_issues', []) + content_issues(c['html'], result)
    if issues or not representable(result.strip('\n')):
        return jsonify(error='Content needs review before synchronization.', content_issues=issues,
                       reason='unsupported_content'), 409
    if not (to_obsidian and to_zotero):
        return jsonify(error='A completed merge must target both notes.', reason='both_targets_required'), 400
    refusal = _settle_unknown_note_write(vault_root, cfg, key)
    if refusal is not None:
        return jsonify(error=refusal['message'], reason=refusal['status'],
                       **({'found_version': refusal['found_version']} if 'found_version' in refusal else {})), 409
    # Read the API before any local write. Its version guard remains in the
    # actual shared writer; a second change can still yield a partial result.
    check = zotero_notes.prepare_write(cfg, zot=inv.records[key].zotero_item,
                note_key=c['note_key'], after_html=md_to_html(result.strip('\n')),
                expected_html=c['html'], expected_version=row['zotero_version'])
    if 'early' in check and not check['early'].get('ok'):
        return jsonify(ok=False, zotero=check['early']), 409
    if not local_guard(c['raw']):
        return jsonify(ok=False, reason='stale', error='Local note, identity or base changed before writing.'), 409
    out: dict = {"citation_key": key}
    new_region = c["parts"][1]
    if to_obsidian:
        prefix, _region, suffix = c["parts"]
        new_region = _framed(result)
        proposed = prefix + new_region + suffix
        if proposed == c["raw"]:
            out["obsidian"] = {"ok": True, "changed": False}
        else:
            res = _region_writes(vault_root, cfg, [{"key": key, "path": c["path"],
                                                    "raw": c["raw"], "proposed": proposed,
                                                    "before_write": lambda: local_guard(c['raw'])}],
                                 "notesync_merge")[key]
            out["obsidian"] = res
            if not res.get("ok"):
                return jsonify({"ok": False, **out}), 409 if res.get("reason") == "changed_on_disk" else 500
    html_after = c["html"]
    version = row["zotero_version"]
    if to_zotero:
        res = _push_many(vault_root, cfg, [{
            "key": key, "zot": inv.records[key].zotero_item, "note_key": c["note_key"],
            "after_html": md_to_html(result.strip("\n")), "region": new_region,
            "base": None, "row": row, "accept_conflict": True,
            "before_write": lambda: local_guard(c['parts'][0] + new_region + c['parts'][2]),
            "expected_html": c["html"], "expected_version": row['zotero_version'], "defer_base": True}])[key]
        out["zotero"] = res
        if res.get("ok") and res.get("status") == "applied":
            html_after = md_to_html(result.strip("\n"))
            version = res.get("version") or version
        elif res.get("status") != "skipped":
            _clear_states_cache()
            return jsonify({"ok": False, **out}), 502
    if not local_guard(c['parts'][0] + new_region + c['parts'][2]):
        _clear_states_cache()
        return jsonify(ok=False, status='incomplete', error='Obsidian changed during application.', **out), 409
    if res.get('status') == 'skipped':
        html_after = md_to_html(result.strip('\n'))
    verified = zotero_notes.prepare_write(cfg, zot=inv.records[key].zotero_item,
        note_key=c['note_key'], after_html=html_after, expected_html=html_after,
        expected_version=res.get('version') or version)
    if not verified.get('early', {}).get('ok') or not local_guard(c['parts'][0] + new_region + c['parts'][2]):
        _clear_states_cache()
        return jsonify(ok=False, status='incomplete', error='Final concordance was not verified; inspect both notes.', **out), 409
    version = verified['early']['version']
    from rag.vault import obsidian_manager
    epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not epoch:
        return jsonify(ok=False, status='incomplete', error='Sync base could not be reserved; inspect before recovery.', **out), 409
    try:
        if not local_guard(c['parts'][0] + new_region + c['parts'][2]):
            return jsonify(ok=False, status='incomplete', error='Note or base changed before settlement.', **out), 409
        _save_base(vault_root, key=key, item_key=row["item_key"], note_key=c["note_key"],
                   version=version, html=html_after, region=new_region)
    finally:
        obsidian_manager.release_lock(epoch)
    _clear_states_cache()
    return jsonify({"ok": True, **out})


@notesync_bp.route("/api/notesync/inspect-create-attempt", methods=["POST"])
@notesync_bp.route("/api/notesync/repair-create-journal", methods=["POST"])
@notesync_bp.route("/api/notesync/repair-create-keys", methods=["POST"])
@notesync_bp.route("/api/notesync/resolve-create-attempt", methods=["POST"])
@notesync_bp.route("/api/notesync/mark-create-satisfied", methods=["POST"])
@notesync_bp.route("/api/notesync/mark-create-uncertain", methods=["POST"])
@notesync_bp.route("/api/notesync/allow-create-retry", methods=["POST"])
@notesync_bp.route("/api/notesync/adopt-create-key", methods=["POST"])
def api_notesync_inspect_create_attempt():
    """Inspection, repairs, or Phase-3 dispositions; no remote write except GET."""
    from notesync import create_attempts
    from tagging import zotero_write
    data = request.get_json(silent=True) or {}
    repair_keys = request.path.endswith('/repair-create-keys')
    resolving = request.path.endswith('/resolve-create-attempt')
    mark_satisfied = request.path.endswith('/mark-create-satisfied')
    mark_uncertain = request.path.endswith('/mark-create-uncertain')
    allow_retry = request.path.endswith('/allow-create-retry')
    adopt_key = request.path.endswith('/adopt-create-key')
    dispositioning = mark_satisfied or mark_uncertain or allow_retry or adopt_key
    repairing = resolving or repair_keys or request.path.endswith('/repair-create-journal')
    if ((repairing or dispositioning)
            and (coerce_bool(data.get('confirm')) is not True or not data.get('evidence_token'))):
        return jsonify({'error': 'A reviewed evidence token and confirm: true are required.'}), 400
    key = coerce_string_max_len(data.get('citation_key'), 512)
    if not key:
        return jsonify({'error': 'citation_key is required.'}), 400
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({'error': 'Cached Library Audit context is required.'}), 404
    rec = inv.records.get(key)
    if rec is None or rec.zotero_item is None:
        return jsonify({'error': 'Original item is absent from the cached inventory; manual review required.'}), 409
    connection, error = zotero_notes._target(load_config(), rec.zotero_item)
    if error:
        return jsonify(error), 503
    transport, api_key, library = connection
    target = dict(base=transport.base_url, library=library,
                  server_id=None if transport.is_web else getattr(rec.zotero_item, 'server_id', None),
                  parent=rec.zotero_item.item_key)
    def read_item(note_key):
        return zotero_write.get_item(transport.base_url, library, note_key, api_key=api_key,
            **({'server_id': target['server_id'] or ''} if not transport.is_web else {}))
    # The Zotero read runs BEFORE the vault operation lease, like every other
    # write path in this module (`_region_writes`, `_push_many` phase 1, the
    # create route): `get_item` honours a `Backoff` hint by SLEEPING, and
    # `zotero_write._await_backoff` documents that sleeping under the op-lock
    # stalls indexing and every other vault write. Every repair entry point
    # re-reads the retained record under the evidence token and re-hashes the
    # source under the lease, so moving the remote OBSERVATION out weakens no
    # guard -- it was always a point-in-time read (see `inspect`'s docstring).
    try:
        inspected = create_attempts.inspect(settings.vault_root, key,
                                            target=target, read_item=read_item)
    except Exception:
        logger.exception('Cannot inspect note creation attempt')
        return jsonify({'error': 'Retained evidence cannot be inspected; retry remains blocked.'}), 500
    if inspected is None:
        return jsonify({'error': 'No retained creation attempt for this citation key.'}), 404
    if not repairing and not dispositioning:
        return jsonify(inspected), 200
    # Dispositions write only the attempt store (no vault lease).
    if dispositioning:
        try:
            token = data['evidence_token']
            if mark_satisfied:
                result = create_attempts.mark_satisfied(
                    settings.vault_root, key, target=target, read_item=read_item,
                    token=token, inspected=inspected)
            elif mark_uncertain:
                result = create_attempts.mark_uncertain(
                    settings.vault_root, key, target=target, read_item=read_item,
                    token=token, inspected=inspected)
            elif allow_retry:
                result = create_attempts.allow_retry_after_trash(
                    settings.vault_root, key, target=target, read_item=read_item,
                    token=token, inspected=inspected)
            else:
                note_key = coerce_string_max_len(data.get('note_key'), 64)
                if not note_key:
                    return jsonify({'error': 'note_key is required to adopt.'}), 400
                result = create_attempts.adopt_note_key(
                    settings.vault_root, key, note_key=note_key, target=target,
                    read_item=read_item, token=token, inspected=inspected)
        except ValueError as exc:
            return jsonify({'error': str(exc), 'ok': False}), 409
        except Exception:
            logger.exception('Cannot record create-attempt disposition')
            return jsonify({'error': 'Disposition could not be saved; creation remains blocked.'}), 500
        return jsonify(result), 200
    from rag.vault import obsidian_manager
    epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not epoch:
        return jsonify({'error': 'Indexing is in progress; try again shortly.'}), 503
    try:
        cfg = load_config()
        if resolving:
            if rec.obsidian_note is None:
                return jsonify({'error': 'Current note mapping is missing.'}), 409
            result = create_attempts.resolve(settings.vault_root, key,
                target=target, read_item=read_item, token=data['evidence_token'],
                source_sha256=data.get('source_sha256'), expected_path=rec.obsidian_note.path,
                cfg=cfg, inspected=inspected)
        elif repair_keys:
            from vault_history import journal as rjournal
            from vault_history._write import journalled_write_note
            if rec.obsidian_note is None:
                return jsonify({'error': 'Current note mapping is missing.'}), 409
            proposal = create_attempts.prepare_frontmatter(settings.vault_root, key,
                target=target, read_item=read_item, token=data['evidence_token'],
                source_sha256=data.get('source_sha256'), expected_path=rec.obsidian_note.path,
                inspected=inspected)
            if proposal['unchanged']:
                result = {'ok': True, 'status': 'already_keyed'}
            else:
                path = proposal['path']
                sha = hashlib.sha256(proposal['raw']).hexdigest()
                def unchanged():
                    # Named reasons: "source_changed_before_write" previously
                    # covered config/lease/inventory drift too, which sent
                    # operators to re-edit a note that had not moved.
                    try:
                        current_inv, current_settings = audit_manager.get_inventory()
                        current_rec = current_inv.records.get(key) if current_inv else None
                        if current_settings is None or Path(current_settings.vault_root).resolve() != Path(settings.vault_root).resolve():
                            return 'inventory_changed_before_write'
                        if (current_rec is None or current_rec.obsidian_note is None
                                or Path(current_rec.obsidian_note.path).resolve() != path
                                or current_rec.zotero_item is None
                                or current_rec.zotero_item.item_key != target['parent']):
                            return 'mapping_changed_before_write'
                        if load_config() != cfg:
                            return 'config_changed_before_write'
                        if not obsidian_manager.heartbeat(epoch):
                            return 'vault_lease_lost_before_write'
                        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                            return 'source_changed_before_write'
                        return True
                    except OSError:
                        return 'source_changed_before_write'
                manifest = rjournal.load(Path(settings.vault_root), cfg)
                op_id, status, reason = journalled_write_note(Path(settings.vault_root), cfg,
                    str(path.relative_to(Path(settings.vault_root).resolve())), proposal['raw'], sha,
                    _sha256_text(proposal['proposed']), proposal['proposed'], _UPDATE_OP_KIND, manifest,
                    log_category='notesync_recover_keys', pre_write_persist=True, before_write=unchanged)
                rjournal.save(Path(settings.vault_root), cfg, manifest)
                result = {'ok': status == 'applied', 'status': status, 'op_id': op_id, 'error': reason}
            _clear_states_cache()
        else:
            result = create_attempts.repair_journal(settings.vault_root, key,
                target=target, read_item=read_item, token=data['evidence_token'], cfg=cfg,
                inspected=inspected)
    except ValueError as exc:
        return jsonify({'error': str(exc), 'ok': False}), 409
    except Exception:
        logger.exception('Cannot repair note creation attempt')
        return jsonify({'error': 'Repair could not finish; its write may have completed. '
                                 'Inspect again. Creation remains blocked.'}), 500
    finally:
        obsidian_manager.release_lock(epoch)
    return jsonify(result), (409 if result.get('ok') is False else 200)


@notesync_bp.route("/api/notesync/create-attempts")
def api_notesync_create_attempts():
    """App-owned evidence only; independent of fresh Zotero availability."""
    from notesync import create_attempts
    inv, settings = audit_manager.get_inventory()
    if settings is None:
        return jsonify({'error': 'Cached vault settings are required.'}), 404
    try:
        rows = create_attempts.list_attempts(settings.vault_root, inv.records if inv else ())
    except Exception:
        logger.exception('Cannot list retained note attempts')
        return jsonify({'error': 'Retained attempts cannot be read; retry remains blocked.'}), 500
    return jsonify({'rows': rows, 'legacy_scope': 'Older hashed subjects require their original citation key in the cached inventory.'})


@notesync_bp.route("/api/notesync/create-zotero-note", methods=["POST"])
def api_notesync_create_zotero_note():
    """Create the child note in Zotero for a reference note that has none.
    ``{citation_key, confirm}``. Opt-in and per row: it is the only verb that
    creates a Zotero item. Journalled as ``zotero_note_create`` (undo = trash);
    the note's frontmatter then gains the two keys (journalled note write)."""
    # Zotero-authoritative mode (library-workflow plan lot 1, defect 2,
    # 2026-09-21): the Obsidian note is a read-only mirror, so NOTHING may
    # travel Obsidian → Zotero. push and merge were already refused; this verb
    # sends the Obsidian region to Zotero as a new child note and was not.
    # Refused before any read, reservation or POST. The creation-attempt
    # RECOVERY verbs stay open: they reconcile an attempt that already
    # happened and never create a note.
    if _authoritative(load_config()):
        return jsonify({"error": "Zotero is authoritative for reading notes (Settings → "
                        "Zotero → Obsidian note sync): the Obsidian note is a mirror and "
                        "is never written to Zotero. Create the note in Zotero instead.",
                        "reason": "authoritative"}), 409
    data = request.get_json(silent=True) or {}
    if coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "confirm: true is required."}), 400
    key = coerce_string_max_len(data.get("citation_key"), 512)
    if not key:
        return jsonify({"error": "citation_key is required."}), 400
    # Recovery evidence must remain available even when the fresh Zotero read
    # is unavailable or the scan no longer classifies this note as missing.
    from notesync import create_attempts
    _inv, cached_settings = audit_manager.get_inventory()
    if cached_settings is not None:
        try:
            retained = create_attempts.lookup(cached_settings.vault_root, key)
        except Exception:
            logger.exception('Cannot read retained note creation attempt')
            return jsonify({"ok": False, "error": "Cannot read retained creation evidence; no POST was sent."}), 500
        if retained is not None and not retained.get('automatic_retry_allowed'):
            return jsonify({"ok": False, "status": "reconciliation_required",
                "note_key": retained.get('note_key'), "attempt": retained,
                "error": create_attempts.block_message(retained)}), 409
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    inv, settings, fresh = ctx
    cfg = load_config()
    vault_root = Path(settings.vault_root)
    row = next((r for r in _sync_rows(inv, settings, fresh) if r["citation_key"] == key), None)
    if row is None or row["state"] != "missing_zotero":
        return jsonify({"error": "That note already has a Zotero reading note (or no record).",
                        "state": (row or {}).get("state")}), 409
    rec = inv.records[key]
    item = rec.zotero_item
    if item is None or not row["item_key"]:
        return jsonify({"error": "No Zotero item for that key."}), 404
    path = Path(rec.obsidian_note.path)
    try:
        raw = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    parts = split_note_parts(raw)
    if parts is None:
        return jsonify({"error": "This note is not in a recognised shape.", "reason": "unparsed"}), 409
    region = parts[1]
    html = md_to_html(region.strip("\n"))
    prepared = zotero_notes.prepare_create(cfg, zot=item, parent_key=row["item_key"], html=html)
    if "early" in prepared:
        return jsonify({"ok": False, **prepared["early"]}), 502
    try:
        attempt, previous = create_attempts.reserve(vault_root, key, path, prepared['commit'])
    except Exception:
        logger.exception('Cannot durably reserve note creation')
        return jsonify({"ok": False, "error": "Cannot save the note creation attempt; no POST was sent."}), 500
    if previous is not None:
        faux = dict(note_key=previous.get('note_key'), automatic_retry_allowed=False,
                    block_reason='retained' if previous.get('note_key') else 'uncertain')
        return jsonify({"ok": False, "status": "reconciliation_required",
                        "note_key": previous.get('note_key'),
                        "error": create_attempts.block_message(faux)}), 409
    def retain_acknowledgement(created):
        create_attempts.acknowledge(attempt, key=created,
            version=getattr(created, 'creation_version', None),
            creation_data=getattr(created, 'creation_data', None))
    res, note_key, version = zotero_notes.post_create(prepared, on_created=retain_acknowledgement)
    if not res.get("ok") or not note_key:
        return jsonify({"ok": False, **res}), 502
    if res.get('acknowledgement_failed'):
        _clear_states_cache()
        return jsonify({"ok": True, "status": "created", "verification_pending": True,
                        "note_key": note_key, "error": "Note created; its durable attempt remains unresolved. Save this key and reconcile manually before retrying."}), 500
    from rag.vault import obsidian_manager
    from tagging import journal as tjournal
    op_id = None
    # The note now EXISTS in Zotero. Every path below that fails to record its
    # undo must say so out loud: an absent `op_id` alone is not a sentence a
    # user reads, and the only other record is `create_attempts`' reservation.
    journal_warning = None
    _NO_UNDO = ("The Zotero note was created, but its undo record was not saved. "
                "Inspect the retained creation attempt before retrying.")
    op_epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if op_epoch:
        try:
            manifest = tjournal.load_for_ops(vault_root, cfg)
            op = zotero_notes.journal_create(prepared=prepared, citation_key=key,
                                             note_key=note_key, version=version or 0,
                                             manifest=manifest)
            # Share identity with conservative recovery so a lost response or
            # delayed original request cannot create a second journal entry.
            # `journal_create` returns the very object it appended and
            # `_TrackedOps.append` preserves identity, so this mutates the
            # record that is in the manifest.
            op['op_id'] = op_id = 'note-create-' + attempt
            tjournal.save_merged(vault_root, cfg, {'ops': [op]})
            # `save_merged` SKIPS a tombstoned or already-present op_id with a
            # bare `continue`, where `save` RAISED -- and this id is now derived
            # from (vault, citation_key) alone, so unlike a minted one it CAN
            # collide with a durable tombstone left by an earlier reverted and
            # pruned creation. Verify rather than report an op_id that names no
            # row; `create_attempts.repair_journal` already checks its own merge
            # the same way.
            if tjournal.peek_op(vault_root, cfg, op_id) is None:
                logger.error("create-zotero-note: journal record %s did not land for %s",
                             op_id, key)
                op_id, journal_warning = None, _NO_UNDO
        except (tjournal.JournalUnreadableError, tjournal.ScopeError) as exc:
            logger.warning("create-zotero-note: journal append failed: %s", exc)
            op_id, journal_warning = None, _NO_UNDO
        finally:
            obsidian_manager.release_lock(op_epoch)
    else:
        logger.warning("create-zotero-note: no vault lease; %s is not journalled", note_key)
        journal_warning = _NO_UNDO
    keyed, reason = set_frontmatter_keys(raw, {"zotero-key": row["item_key"],
                                               "zotero-note-key": note_key})
    fm: dict = {"ok": False, "reason": reason}
    if keyed is not None and keyed != raw:
        fm = _region_writes(vault_root, cfg, [{"key": key, "path": path, "raw": raw,
                                              "proposed": keyed}], "notesync_keys")[key]
    elif keyed is not None:
        fm = {"ok": True, "changed": False}
    if not res.get("verification_pending"):
        _save_base(vault_root, key=key, item_key=row["item_key"], note_key=note_key,
                   version=version or 0, html=html, region=region, push_version=version or 0)
    _clear_states_cache()
    return jsonify({**res, "note_key": note_key, "version": version, "op_id": op_id,
                    "frontmatter": fm,
                    **({"journal_warning": journal_warning} if journal_warning else {})})


@notesync_bp.route('/api/notesync/campaign/plan', methods=['POST'])
def api_notesync_campaign_plan():
    from notesync import campaigns
    data = request.get_json(silent=True) or {}
    keys, err = _parse_keys(data, 100)
    if err is not None:
        return err
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    _, settings, _ = ctx
    proposals = {}
    for key in keys:
        response = _merge_preview({'citation_key': key})
        response, status = response if isinstance(response, tuple) else (response, 200)
        proposals[key] = dict(response.get_json(), preview_status=status)
    scope = str(Path(settings.vault_root).resolve())
    return jsonify(campaigns.create(scope, proposals))


@notesync_bp.route('/api/notesync/campaign/<identifier>', methods=['GET'])
def api_notesync_campaign_review(identifier):
    from notesync import campaigns
    if (err := _guard()) is not None:
        return err
    try:
        return jsonify(campaigns.read(identifier))
    except ValueError as exc:
        return jsonify(error=str(exc)), 404


@notesync_bp.route('/api/notesync/campaign/<identifier>/inspect', methods=['POST'])
def api_notesync_campaign_inspect(identifier):
    from notesync import campaigns
    try:
        campaign = campaigns.read(identifier)
    except ValueError as exc:
        return jsonify(error=str(exc)), 404
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    inv, settings, fresh = ctx
    if str(Path(settings.vault_root).resolve()) != campaign['scope']:
        return jsonify(error='Campaign belongs to a different vault.'), 409
    if campaign.get('kind', 'notes') != 'notes':
        return jsonify(error='This is not a notes campaign.'), 409
    # Bypass memoized local text on explicit recovery inspection, including
    # same-size edits with preserved timestamps. One fresh Zotero observation
    # is shared by the previews in this inspection; none authorizes a write.
    _clear_states_cache()
    current_rows = {r['citation_key']: _public_row(r) for r in _sync_rows(inv, settings, fresh)}
    inspections = {}
    for key, proposal in campaign['proposals'].items():
        response = _merge_preview({'citation_key': key}, context=ctx)
        response, status = response if isinstance(response, tuple) else (response, 200)
        current = dict(response.get_json(), preview_status=status)
        inspections[key] = campaigns.inspect_proposal(proposal, campaign['outcomes'].get(key), current)
        if inspections[key]['status'] == 'completed' and campaigns.undoable_outcome(campaign['outcomes'].get(key)):
            inspections[key]['undo'] = _undo_evidence(
                key, proposal, campaign['outcomes'][key], current, inv, Path(settings.vault_root), load_config())['verdict']
        if inspections[key]['status'] == 'base_changed':
            inspections[key]['settlement'] = _settlement_evidence(
                key, proposal, campaign['outcomes'].get(key), current, Path(settings.vault_root), load_config())
    return jsonify(campaign=campaign, inspections=inspections,
                   current={key: current_rows[key] for key in campaign['proposals'] if key in current_rows},
                   note='Fresh inspection never replays writes, settles a journal or advances a sync base.')


def _acquire_lock_bounded(obsidian_manager, attempts=20):
    """retire-tag's bounded re-acquire for a hold that follows network I/O."""
    for attempt in range(attempts):
        epoch = obsidian_manager.try_acquire_lock(ttl=600)
        if epoch:
            return epoch
        if attempt + 1 < attempts:
            time.sleep(1)
    return None


def _settlement_evidence(key, proposal, outcome, current, vault_root, cfg):
    """Read-only evidence for post-base/pre-outcome settlement: the current base
    record, this note's refactor-journal writes and this key's Zotero note writes."""
    from notesync import campaigns
    from vault_history import journal as rjournal
    from tagging import journal as tjournal
    base = None
    if not (base_store._path(vault_root, key).exists() and base_store.load(vault_root, key) is None):
        rec = base_store.load(vault_root, key)
        base = rec.to_json() if rec is not None else None
    rel = None
    try:
        rel = str(Path((current.get('identity') or {}).get('path') or '').relative_to(vault_root.resolve()))
    except ValueError:
        rel = None
    try:
        local_ops = [op for op in rjournal.load(vault_root, cfg).get('ops', [])
                     if op.get('kind') == _UPDATE_OP_KIND and rel is not None and op.get('note_rel') == rel]
        native_ops = [op for op in tjournal.load(vault_root, cfg).get('ops', [])
                      if op.get('kind') == zotero_notes.OP_WRITE and op.get('citation_key') == key]
    except (tjournal.JournalUnreadableError, tjournal.ScopeError, OSError, ValueError):
        return dict(applicable=False, status='blocked', reasons=['journal_unreadable'],
                    next_action='Restore journal access before any settlement; the retained outcome stays.')
    return campaigns.settlement_check(proposal, outcome, current, base=base, local_ops=local_ops,
                                      native_ops=native_ops, dispatch_at=(outcome or {}).get('at'))


@notesync_bp.route('/api/notesync/campaign/<identifier>/settle', methods=['POST'])
def api_notesync_campaign_settle(identifier):
    """Record a dispatch that completed both writes and its base save but crashed
    before its outcome was recorded. Writes no note, base or journal entry."""
    from notesync import campaigns
    data = request.get_json(silent=True) or {}
    if data.get('confirm') is not True:
        return jsonify(error='confirm: true is required.'), 400
    if not campaigns.application_available():
        return jsonify(error='Settlement is closed pending disposable native and human acceptance.', applicable=False), 501
    with campaigns.recovery_hold(identifier) as held:
        if not held:
            return jsonify(error='Campaign application, recovery or settlement is already running; inspect after it finishes.'), 409
        return _settle_campaign(identifier, data)


def _settle_campaign(identifier, data):
    from notesync import campaigns
    from rag.vault import obsidian_manager
    inspected = api_notesync_campaign_inspect(identifier)
    response, status = inspected if isinstance(inspected, tuple) else (inspected, 200)
    if status != 200:
        return inspected
    inspection = response.get_json()
    key = data.get('citation_key')
    if not isinstance(key, str) or key not in inspection['inspections']:
        return jsonify(error='Select one exact inspected note.'), 400
    item = inspection['inspections'][key]
    settlement = item.get('settlement') or {}
    current = item['current']
    if (item['status'] != 'base_changed' or not settlement.get('applicable')
            or data.get('evidence') != current.get('evidence')
            or data.get('outcome_evidence') != item['outcome_evidence']
            or data.get('result') != item['intended']):
        return jsonify(error='Settlement requires the exact current inspection, its settleable evidence and the original approved result.',
                       inspection=item), 409
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    inv, settings, _ = ctx
    vault_root, cfg = Path(settings.vault_root), load_config()
    campaign = campaigns.read(identifier)
    proposal, outcome = campaign['proposals'][key], campaign['outcomes'].get(key)
    if campaigns.outcome_evidence(outcome) != item['outcome_evidence']:
        return jsonify(error='Outcome changed since inspection; inspect again.'), 409
    # The native read runs BEFORE the op-lock (2026-09-17 review, B1): a
    # Zotero GET can sleep on a Backoff hint, and this settlement writes no
    # Zotero state, so the lock is needed only for the local re-observation.
    html = md_to_html(item['intended'].strip('\n'))
    check = zotero_notes.prepare_write(cfg, zot=inv.records[key].zotero_item,
        note_key=current['identity']['note_key'], after_html=html, expected_html=html,
        expected_version=current['zotero_version'])
    if not check.get('early', {}).get('ok'):
        return jsonify(error='Native note changed before settlement; the retained outcome stays.'), 409
    epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not epoch:
        return jsonify(error='Settlement is busy; inspect again.'), 409
    try:
        # Re-observe both destinations, the base and the journals under the
        # op-lock so a write landing between inspection and settlement is seen.
        _clear_states_cache()
        preview = _merge_preview({'citation_key': key})
        body, preview_status = preview if isinstance(preview, tuple) else (preview, 200)
        fresh = dict(body.get_json(), preview_status=preview_status)
        if preview_status != 200 or fresh.get('evidence') != current.get('evidence'):
            return jsonify(error='Notes or base changed before settlement; the retained outcome stays.'), 409
        verdict = _settlement_evidence(key, proposal, outcome, fresh, vault_root, cfg)
        if not verdict.get('applicable'):
            return jsonify(error='Settlement evidence changed before recording; the retained outcome stays.', settlement=verdict), 409
        if not obsidian_manager.heartbeat(epoch):
            return jsonify(error='The settlement lease was lost; the retained outcome stays.'), 409
        try:
            campaigns.settle(identifier, key, item['outcome_evidence'], verdict['evidence'])
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
    finally:
        obsidian_manager.release_lock(epoch)
    _clear_states_cache()
    return jsonify(ok=True, citation_key=key, status=campaigns.SETTLED_STATUS, settlement=verdict['evidence'],
                   note='No note, base or journal entry was written; the dispatch record is retained beneath this outcome.')


def _undo_evidence(key, proposal, outcome, current, inv, vault_root, cfg):
    """Read-only evidence for a full campaign undo: the local journal op, the
    native journal op with a fresh read of the note it wrote, and the verdict.
    Returns ``{verdict, local_op, native_op, prepared}``; ``prepared`` is the
    native revert's read side (``{"commit"}`` or ``{"early"}``) when a native
    write exists."""
    from notesync import campaigns
    from vault_history import journal as rjournal
    from tagging import journal as tjournal
    local_op = native_op = prepared = None
    native_html = native_version = None
    try:
        if outcome.get('obsidian', {}).get('op_id'):
            local_op = rjournal.find_op(rjournal.load(vault_root, cfg), outcome['obsidian']['op_id'])
        if outcome.get('zotero', {}).get('op_id'):
            native_op = tjournal.peek_op(vault_root, cfg, outcome['zotero']['op_id'])
    except (tjournal.JournalUnreadableError, tjournal.ScopeError, rjournal.ScopeError, OSError, ValueError):
        return dict(verdict=dict(applicable=False, status='blocked', reasons=['journal_unreadable'],
                                 next_action='Restore journal access before any undo; the applied outcome stays.'),
                    local_op=None, native_op=None, prepared=None)
    zot = inv.records[key].zotero_item if key in inv.records else None
    unreadable = False
    if native_op is not None:
        prepared = zotero_notes.revert_prepare(cfg, native_op)
        if 'commit' in prepared:
            native_html, native_version = native_op.get('after_html'), int(prepared['commit']['version'])
        elif prepared.get('early', {}).get('status') == 'skipped':
            native_html = None  # changed after the write: the verdict names it
        else:
            unreadable = True  # the note could not be read: never "changed"
    elif zot is not None and outcome.get('zotero', {}).get('status') == 'skipped':
        html = md_to_html((campaigns.approved_result(outcome) or '').strip('\n'))
        check = zotero_notes.prepare_write(cfg, zot=zot, note_key=(current.get('identity') or {}).get('note_key', ''),
                                           after_html=html, expected_html=html, expected_version=current.get('zotero_version'))
        if check.get('early', {}).get('ok'):
            native_html, native_version = html, check['early'].get('version')
        elif check.get('early', {}).get('status') in ('unreachable', 'error'):
            unreadable = True
    verdict = campaigns.undo_check(proposal, outcome, current, local_op=local_op,
                                   native_version=native_version, native_html=native_html)
    if unreadable and 'zotero_changed_after_write' in verdict.get('reasons', []):
        # 2026-09-17 review, F10: an unreadable native note is its own reason.
        verdict = dict(verdict, reasons=['native_unreadable' if r == 'zotero_changed_after_write' else r
                                         for r in verdict['reasons']])
    return dict(verdict=verdict, local_op=local_op, native_op=native_op, prepared=prepared)


@notesync_bp.route('/api/notesync/campaign/<identifier>/undo', methods=['POST'])
def api_notesync_campaign_undo(identifier):
    """Undo one applied campaign note on BOTH destinations, restoring the
    before-images and the pre-campaign base. Admissible only while both notes
    still carry the exact written image; production admission stays closed."""
    from notesync import campaigns
    data = request.get_json(silent=True) or {}
    if data.get('confirm') is not True:
        return jsonify(error='confirm: true is required.'), 400
    if not campaigns.application_available():
        return jsonify(error='Undo is closed pending disposable native and human acceptance.', applicable=False), 501
    with campaigns.recovery_hold(identifier) as held:
        if not held:
            return jsonify(error='Campaign application, recovery, settlement or undo is already running; inspect after it finishes.'), 409
        return _undo_campaign(identifier, data)


def _undo_campaign(identifier, data):
    from notesync import campaigns
    from vault_history import journal as rjournal
    from tagging import journal as tjournal
    from rag.vault import obsidian_manager
    try:
        campaign = campaigns.read(identifier)
    except ValueError as exc:
        return jsonify(error=str(exc)), 404
    key = data.get('citation_key')
    if not isinstance(key, str) or key not in campaign['proposals']:
        return jsonify(error='Select one exact campaign note.'), 400
    outcome = campaign['outcomes'].get(key)
    if not campaigns.undoable_outcome(outcome):
        return jsonify(error='Only an application that succeeded on both notes can be undone.', outcome=outcome), 409
    if data.get('outcome_evidence') != campaigns.outcome_evidence(outcome):
        return jsonify(error='Outcome changed since inspection; inspect again before undo.'), 409
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    inv, settings, _ = ctx
    vault_root, cfg = Path(settings.vault_root), load_config()
    if str(vault_root.resolve()) != campaign['scope']:
        return jsonify(error='Campaign belongs to a different vault.'), 409
    proposal = campaign['proposals'][key]
    _clear_states_cache()
    preview = _merge_preview({'citation_key': key}, context=ctx)
    body, preview_status = preview if isinstance(preview, tuple) else (preview, 200)
    current = dict(body.get_json(), preview_status=preview_status)
    evidence = _undo_evidence(key, proposal, outcome, current, inv, vault_root, cfg)
    verdict = evidence['verdict']
    if not verdict.get('applicable'):
        return jsonify(error='Undo is not admissible.', undo=verdict), 409
    local_op, native_op, prepared = evidence['local_op'], evidence['native_op'], evidence['prepared']
    result = dict(status=campaigns.UNDONE_STATUS, ok=True, undo={})

    def incomplete(message):
        result.update(status=campaigns.UNDO_INCOMPLETE_STATUS, ok=False)
        campaigns.record_undo(identifier, key, data['outcome_evidence'], result)
        _clear_states_cache()
        return jsonify(ok=False, citation_key=key, status=result['status'], undo=result['undo'], error=message), 502

    path = Path((current.get('identity') or {}).get('path') or '')
    expected_sha = local_op.get('hash_after') if local_op is not None else proposal.get('content_sha256')

    def local_unchanged():
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha
        except OSError:
            return False

    # Four phases (2026-09-17 review, B1): the native read already ran
    # lock-free in _undo_evidence; the native PATCH runs lock-free too, and the
    # op-lock covers only the local re-observation and the local writes. The
    # native write goes first because its version guard is the one that can
    # refuse; a failed or uncertain native revert is retained as
    # undo_incomplete, never retried.
    if not local_unchanged():
        return jsonify(error='Local note changed before undo; nothing was changed.'), 409
    if native_op is not None:
        try:
            if tjournal.peek_op(vault_root, cfg, native_op['op_id']) != native_op:
                return jsonify(error='Native journal changed before undo; nothing was changed.'), 409
        except (tjournal.JournalUnreadableError, tjournal.ScopeError) as exc:
            return jsonify(error='Native journal unreadable before undo; nothing was changed. ' + sanitise_error_msg(exc)), 409
        reverted = dict(native_op)
        result['undo']['zotero'] = zotero_notes.revert_commit(prepared, reverted)
    else:
        result['undo']['zotero'] = dict(ok=True, status='no_write')

    epoch = _acquire_lock_bounded(obsidian_manager)
    if not epoch:
        return incomplete('The op-lock could not be re-acquired after the native undo; the local note, '
                          'journal and base were not changed and the outcome is retained for inspection.')
    try:
        if native_op is not None:
            # Record the native outcome on a RELOADED manifest, never the
            # object read before the PATCH.
            try:
                tman = tjournal.load_for_ops(vault_root, cfg, [native_op['op_id']])
                op = tjournal.find_op(tman, native_op['op_id'])
                if op is not None and reverted.get('state') == 'reverted':
                    op['state'] = 'reverted'
                    tjournal.save(vault_root, cfg, tman)
            except (tjournal.JournalUnreadableError, tjournal.ScopeError) as exc:
                result['undo']['zotero'] = dict(result['undo']['zotero'], journal_error=sanitise_error_msg(exc))
            if not result['undo']['zotero'].get('ok'):
                return incomplete('Native undo did not complete; the outcome is retained for inspection.')
        # Re-read the local note under the lock: a write landing while the
        # native PATCH ran must refuse, not be clobbered.
        if not local_unchanged() or not obsidian_manager.heartbeat(epoch):
            result['undo']['obsidian'] = dict(ok=False, status='changed_before_undo')
            return incomplete('The local note changed during the native undo; it was not overwritten and the outcome is retained for inspection.')
        if local_op is not None:
            rman = rjournal.load(vault_root, cfg)
            op = rjournal.find_op(rman, local_op['id'])  # refactor ops are keyed `id`, tagging ops `op_id`
            local = rjournal.revert_op(vault_root, cfg, op) if op is not None else dict(ok=False, status='missing')
            result['undo']['obsidian'] = local
            if op is not None:
                rjournal.save(vault_root, cfg, rman)
            if not local.get('ok'):
                return incomplete('Local undo did not complete after the native revert; the outcome is retained for inspection.')
        else:
            result['undo']['obsidian'] = dict(ok=True, status='no_write')
        previous = outcome['dispatch'].get('previous_base')
        if previous is None:
            base_store.delete(vault_root, key)
            result['undo']['base'] = dict(ok=True, status='removed')
        else:
            rec = BaseRecord.from_json(previous)
            if rec is None:
                result['undo']['base'] = dict(ok=False, status='previous_base_invalid')
                return incomplete('Both notes were restored but the pre-campaign base could not be; inspect the base record.')
            base_store.save(vault_root, rec)
            result['undo']['base'] = dict(ok=True, status='restored')
        try:
            campaigns.record_undo(identifier, key, data['outcome_evidence'], result)
        except ValueError as exc:
            return jsonify(error=str(exc), undo=result['undo']), 409
    finally:
        obsidian_manager.release_lock(epoch)
    _clear_states_cache()
    return jsonify(ok=True, citation_key=key, status=campaigns.UNDONE_STATUS, undo=result['undo'],
                   note='Both notes carry their before-images and the pre-campaign base is back; the applied outcome is retained beneath this record.')


@notesync_bp.route('/api/notesync/campaign/<identifier>/apply', methods=['POST'])
def api_notesync_campaign_apply(identifier):
    from notesync import campaigns
    data = request.get_json(silent=True) or {}
    if data.get('confirm') is not True:
        return jsonify(error='confirm: true is required.'), 400
    if not campaigns.application_available():
        return jsonify(error='Campaign application is closed pending disposable native and human note-content acceptance.',
                       reason='native_human_acceptance_required', applicable=False), 501
    # Share the non-expiring hold with recovery: a durable reservation alone
    # does not mean the first client has stopped writing or recording outcomes.
    with campaigns.recovery_hold(identifier) as held:
        if not held:
            return jsonify(error='Campaign application or recovery is already running; inspect after it finishes.'), 409
        return _apply_campaign(identifier, data)


def _apply_campaign(identifier, data):
    from notesync import campaigns
    decisions = data.get('decisions')
    if not isinstance(decisions, list) or not decisions or len(decisions) > 100:
        return jsonify(error='Select 1–100 exact decisions.'), 400
    try:
        campaign = campaigns.read(identifier)
    except ValueError as exc:
        return jsonify(error=str(exc)), 404
    if campaign.get('kind', 'notes') != 'notes':
        return jsonify(error='This campaign has no note executor.'), 409
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    if str(Path(ctx[1].vault_root).resolve()) != campaign['scope']:
        return jsonify(error='Campaign belongs to a different vault.'), 409
    keys = [d.get('citation_key') for d in decisions if isinstance(d, dict)]
    if len(keys) != len(decisions) or any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys):
        return jsonify(error='Decisions must name distinct citation keys.'), 400
    for d in decisions:
        p = campaign['proposals'].get(d['citation_key'])
        if (not p or p.get('preview_status') != 200 or d.get('approve') is not True
                or not isinstance(d.get('result'), str) or d.get('evidence') != p.get('evidence')):
            return jsonify(error='Each selected decision needs approval, exact edited result and preview evidence.'), 400
        if d['citation_key'] in campaign['outcomes']:
            return jsonify(error='Previously dispatched proposal: inspect before recovery; no replay.'), 409
    results = {}
    vault_root = Path(ctx[1].vault_root)
    for d in decisions:
        key = d['citation_key']
        p = campaign['proposals'][key]
        # Retain the pre-campaign base beside the reservation: a full undo
        # restores it exactly, and an unreadable record is recorded as such
        # rather than read as "no base".
        previous = base_store.load(vault_root, key)
        unreadable = previous is None and base_store._path(vault_root, key).exists()
        reservation = {'status': 'dispatched_or_unknown', 'decision': d,
                       'previous_base': previous.to_json() if previous is not None else None}
        if unreadable:
            reservation['previous_base_unreadable'] = True
        try:
            campaigns.record(identifier, key, reservation, reserve=True)
        except ValueError as exc:
            return jsonify(error=str(exc), results=results), 409
        # Persist before entering the existing writer. A process interruption
        # leaves a durable no-replay barrier even before either destination.
        response = _apply_merge(dict(citation_key=key, result=d['result'], evidence=p['evidence'],
                           content_sha256=p['content_sha256'], confirm=True,
                           targets={'obsidian': True, 'zotero': True}))
        response, status = response if isinstance(response, tuple) else (response, 200)
        result = dict(response.get_json(), http_status=status)
        campaigns.record(identifier, key, result)
        results[key] = result
    return jsonify(ok=all(r.get('ok') is True for r in results.values()), results=results)


@notesync_bp.route('/api/notesync/campaign/<identifier>/recover', methods=['POST'])
def api_notesync_campaign_recover(identifier):
    """Explicit inspected completion; admission remains closed in production.

    Never creates a note. Unknown remote writes can be settled only when the
    exact retained after-image is currently observed in the bound destination.
    """
    from notesync import campaigns
    data = request.get_json(silent=True) or {}
    if data.get('confirm') is not True:
        return jsonify(error='confirm: true is required.'), 400
    if not campaigns.application_available():
        return jsonify(error='Recovery is closed pending disposable native and human acceptance.', applicable=False), 501
    with campaigns.recovery_hold(identifier) as held:
        if not held:
            return jsonify(error='Recovery is already running; inspect after it finishes.'), 409
        return _recover_campaign(identifier, data)


def _recover_campaign(identifier, data):
    from notesync import campaigns
    from tagging import journal as tjournal
    from rag.vault import obsidian_manager
    inspected = api_notesync_campaign_inspect(identifier)
    response, status = inspected if isinstance(inspected, tuple) else (inspected, 200)
    if status != 200:
        return inspected
    inspection = response.get_json()
    key = data.get('citation_key')
    if not isinstance(key, str) or key not in inspection['inspections']:
        return jsonify(error='Select one exact inspected note.'), 400
    item = inspection['inspections'][key]
    current = item['current']
    if (item['status'] not in ('incomplete', 'concordant_observed')
            or not current.get('base_evidence')
            or data.get('evidence') != current.get('evidence')
            or data.get('outcome_evidence') != item['outcome_evidence']
            or data.get('result') != item['intended']):
        return jsonify(error='Recovery requires the exact current inspection and original approved result.', inspection=item), 409
    ctx, err = _load_sync_context()
    if err is not None:
        return err
    inv, settings, _ = ctx
    vault_root, cfg = Path(settings.vault_root), load_config()
    html = md_to_html(item['intended'].strip('\n'))
    manifest = tjournal.load(vault_root, cfg)
    unknown = [op for op in manifest.get('ops', []) if op.get('kind') == zotero_notes.OP_WRITE
               and op.get('citation_key') == key and op.get('state') == 'outcome_unknown']
    if len(unknown) > 1:
        return jsonify(error='Multiple unknown writes require individual journal review.'), 409
    if unknown:
        op = unknown[0]
        identity = current['identity']
        scope = identity['scope']
        zot = inv.records[key].zotero_item
        check = zotero_notes.prepare_write(cfg, zot=zot, note_key=identity['note_key'],
                                           after_html=html, expected_html=html,
                                           expected_version=current['zotero_version'])
        target, target_error = zotero_notes._target(cfg, zot)
        if (not check.get('early', {}).get('ok') or op.get('after_html') != html
                or op.get('item_key') != identity['note_key'] or op.get('parent_item') != scope['item']
                or not scope.get('database') or op.get('server_id') != scope['database']
                or op.get('transport') != 'local' or target_error or target is None
                or target[0].kind != op.get('transport') or target[0].base_url != op.get('base_url')
                or target[2] != op.get('lib_path')):
            return jsonify(error='Unknown journal destination is not exactly observed; no replay or settlement.'), 409
    try:
        campaigns.reserve_recovery(identifier, key, item['outcome_evidence'],
            dict(result=item['intended'], evidence=current['evidence'], approve=True))
    except ValueError as exc:
        return jsonify(error=str(exc)), 409
    if unknown:
        # Reservation can take time: recheck the native note at the settlement
        # boundary, but BEFORE the op-lock (2026-09-17 review, B1).
        check = zotero_notes.prepare_write(cfg, zot=zot, note_key=identity['note_key'],
            after_html=html, expected_html=html, expected_version=current['zotero_version'])
        if not check.get('early', {}).get('ok'):
            return jsonify(error='Native note changed before journal settlement; reservation retained.'), 409
        epoch = obsidian_manager.try_acquire_lock(ttl=600)
        if not epoch:
            return jsonify(error='Recovery reserved but journal settlement is busy; inspect again.'), 409
        try:
            latest = tjournal.load(vault_root, cfg)
            retained = tjournal.find_op(latest, unknown[0]['op_id'])
            if retained != unknown[0]:
                return jsonify(error='Journal changed after inspection; reservation retained.'), 409
            # Reservation and lock acquisition can take time. Recheck both
            # destinations, base and identity at the settlement boundary.
            _clear_states_cache()
            preview = _merge_preview({'citation_key': key})
            preview_body, preview_status = preview if isinstance(preview, tuple) else (preview, 200)
            unreadable_base = base_store._path(vault_root, key).exists() and base_store.load(vault_root, key) is None
            if unreadable_base or preview_status != 200 or preview_body.get_json().get('evidence') != current['evidence']:
                return jsonify(error='Notes or base changed before journal settlement; reservation retained.'), 409
            if not obsidian_manager.heartbeat(epoch):
                return jsonify(error='Settlement lease was lost; reservation retained.'), 409
            # No undo version is inferred. This distinct terminal state retains
            # the uncertain dispatch and prohibits automatic restore/replay.
            retained['state'] = 'observed_after_recovery'
            retained['recovery_campaign'] = identifier
            retained['recovery_evidence'] = current['evidence']
            tjournal.save(vault_root, cfg, latest)
        finally:
            obsidian_manager.release_lock(epoch)
    response = _apply_merge(dict(citation_key=key, result=item['intended'], evidence=current['evidence'],
        content_sha256=current['content_sha256'], confirm=True, targets={'obsidian': True, 'zotero': True}))
    body, status = response if isinstance(response, tuple) else (response, 200)
    campaigns.record(identifier, key, dict(body.get_json(), http_status=status, recovery=True))
    return response


# ---------------------------------------------------------------------------
# Citation-key changes (2026-09-19)
# ---------------------------------------------------------------------------
#
# A reference note is named after its citation key. When Better BibTeX
# regenerates a key (an author, year or title edited in Zotero), the next scan
# no longer joins the note to its record: it becomes an orphan under the old
# name while the record under the new key has no note. The note's own
# frontmatter says which Zotero ITEM it belongs to (``zotero-key``, written by
# the layout migration — 1,237 of 1,238 live notes carry it), and an item key
# never changes, so the new name is DERIVED, never guessed: the record whose
# Zotero item has that key.
#
# The routine proposes these renames (cleanup phase ``03k``); the maintainer
# approves each. A rename refuses rather than guesses when the new name is
# taken, and when other notes link to the old name — Obsidian resolves a
# wikilink by FILE name, not by alias, so renaming would break them; renaming
# in Obsidian instead rewrites them, and the next routine run then finds
# nothing to do.

_WIKILINK_TARGET_RE = re.compile(r"\[\[([^\]|#^\n]+)")
# Markdown links, three spellings Obsidian resolves (2026-09-21 review): the
# bare `(note.md)`, the angle-bracketed `(<my note.md>)` (spaces allowed), and
# either followed by a CommonMark title `(note.md "t")`.
_MDLINK_TARGET_RE = re.compile(
    r"\]\(\s*(?:<([^>\n]+?)\.md(?:#[^>\n]*)?>|([^)\s<>]+?)\.md(?:#[^)\s]*)?)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)")


def _canvas_targets(text: str) -> list[str]:
    """Every note target a ``.canvas`` file references. Raises ``ValueError``
    on anything that is not a JSON Canvas document.

    Library-workflow plan lot 1, defect 4 (2026-09-21). A canvas is JSON, not
    Markdown: a note placed on it is a ``{"type": "file", "file":
    "folder/note.md"}`` node whose path the old regex sweep could not see
    (it looked for ``[[…]]`` and ``](….md)`` shapes). Two sources, both read:

    * ``file`` nodes — a vault-relative path; its basename is the reference
      (Obsidian renders the node from that exact file, so renaming it breaks
      the node);
    * ``text`` nodes — Markdown, so wikilinks and markdown links inside them
      are references exactly as in a note.

    Link and group nodes carry no note reference. A node list that is not a
    list, or a node that is not an object, is malformed: the caller treats
    that as "could not inspect", never as "links nothing".
    """
    # Tolerated, not refused (2026-09-21 review — one such file would otherwise
    # block every rename vault-wide): an empty file (a canvas never opened),
    # a UTF-8 BOM, and a null node list. All three reference nothing.
    text = text.lstrip("\ufeff")
    if not text.strip():
        return []
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not a canvas object")
    nodes = data.get("nodes") or []
    if not isinstance(nodes, list):
        raise ValueError("canvas nodes is not a list")
    out: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("canvas node is not an object")
        if node.get("type") == "file" and isinstance(node.get("file"), str):
            name = node["file"].rsplit("/", 1)[-1]
            if name.lower().endswith(".md"):
                out.append(name[:-3])
        elif node.get("type") == "text" and isinstance(node.get("text"), str):
            out.extend(_markdown_targets(node["text"]))
    return out


def _markdown_targets(text: str) -> list[str]:
    """Wikilink and markdown-link note targets in Markdown text, by basename."""
    from urllib.parse import unquote

    hits: list[str] = []
    for m in _WIKILINK_TARGET_RE.finditer(text):
        # `[[note\|alias]]` is how a wikilink is written inside a table; the
        # escaping backslash is not part of the target (2026-09-21 review).
        t = m.group(1).strip().rstrip("\\").rsplit("/", 1)[-1]
        hits.append(t[:-3] if t.endswith(".md") else t)
    for m in _MDLINK_TARGET_RE.finditer(text):
        hits.append(unquote(m.group(1) or m.group(2)).rsplit("/", 1)[-1])
    return hits


#: The link walk's budget (audit remediation 2026-09-23, N9, maintainer
#: decision: follow symlinked folders — Obsidian indexes them — but BOUNDED).
#: A symlink to a huge tree (a home folder, a mounted volume) used to make the
#: walk unbounded while the key-changes listing waited on it. Measured on the
#: live vault 2026-09-21: the whole walk took 0.15 s; these bounds are ~1000x
#: that, so they only ever bite on a tree that is not a vault. On overflow the
#: walk stops and says so in its "not inspected" list, which refuses the
#: rename exactly as an unreadable file does — never "nothing links here".
_LINK_WALK_MAX_FILES = 200_000
_LINK_WALK_MAX_S = 60.0


def _incoming_links(vault_root: Path, stems: set[str],
                    exclude: set[Path]) -> tuple[dict[str, list[str]], list[str]]:
    """``({stem: [vault-relative file linking to it]}, [files not inspected])``.

    *exclude* names the candidate notes themselves: a link from one of them to
    ITS OWN stem is ignored, every other link it holds still counts.

    Over every ``.md`` and ``.canvas`` file outside hidden directories. A
    wikilink's target is compared by basename, the way Obsidian resolves a
    bare link; a markdown link by its ``.md`` basename; a canvas by its file
    and text nodes (:func:`_canvas_targets`).

    The second list is the point of the 2026-09-21 change (plan lot 1, defect
    4). A file that could not be read, or a canvas that is not valid JSON
    Canvas, used to be skipped with ``continue`` — i.e. counted as "links to
    nothing", which is the answer that lets a rename go ahead and break the
    link. Such a file is now returned as UNINSPECTED, and the caller refuses
    the rename while any exists. Bytes that are not valid UTF-8 are still
    decoded with replacement: a link target is ASCII-compatible in practice
    and the replacement never creates a false "no link".
    """
    out: dict[str, list[str]] = {}
    unverified: list[str] = []
    if not stems:
        return out, unverified
    # Obsidian's resolver is case-insensitive on APFS; keep the original
    # stem as the dict key so the caller still matches ``old_key``.
    folded = {s.casefold(): s for s in stems}
    root = str(vault_root)

    def _rel(path: str) -> str:
        return os.path.relpath(path, root).replace(os.sep, "/")

    def _walk_error(err: OSError) -> None:
        # A folder that cannot be listed may hold a link: report it, never
        # treat it as empty (rglob swallowed this silently).
        unverified.append(_rel(err.filename) if err.filename else "?")

    # os.walk rather than rglob (2026-09-21 review): it reports unreadable
    # folders through `onerror`, and it can follow symlinked folders — Obsidian
    # indexes them — while `seen` stops a symlink cycle from looping.
    seen: set[str] = set()
    deadline = time.monotonic() + _LINK_WALK_MAX_S
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        visited += len(filenames)
        if visited > _LINK_WALK_MAX_FILES or time.monotonic() > deadline:
            unverified.append(
                f"(the vault walk stopped after {visited} files / {_LINK_WALK_MAX_S:g} s "
                "budget — a symlinked folder may point outside the vault)")
            break
        for name in filenames:
            lower = name.lower()
            if name.startswith(".") or not (lower.endswith(".md") or lower.endswith(".canvas")):
                continue
            p = Path(dirpath) / name
            rel = _rel(str(p))
            try:
                text = p.read_bytes().decode("utf-8", errors="replace")
            except OSError:
                unverified.append(rel)
                continue
            if lower.endswith(".canvas"):
                try:
                    targets = _canvas_targets(text)
                except (ValueError, TypeError):
                    unverified.append(rel)
                    continue
            else:
                targets = _markdown_targets(text)
            is_candidate = p in exclude
            for h in set(targets):
                stem = folded.get(h.casefold())
                # A candidate note linking to ITSELF does not block its own
                # rename; its links to OTHER candidates still count.
                if stem is None or (is_candidate and p.stem.casefold() == stem.casefold()):
                    continue
                out.setdefault(stem, []).append(rel)
    return out, sorted(unverified)


def _key_change_candidates(inv) -> list[Path]:
    """Reference notes that no longer join to a live record."""
    paths = list(getattr(inv, "notes_without_record", None) or [])
    for rec in inv.records.values():
        note = getattr(rec, "obsidian_note", None)
        if note is not None and rec.bib_entry is None and rec.zotero_item is None:
            paths.append(Path(note.path))
    return sorted(set(paths))


def _key_change_rows(inv, settings, only: str | None = None) -> list[dict]:
    """One row per candidate note: where its item now lives, and whether the
    rename can happen. ``state``: ``ready`` · ``no_item_key`` (the note carries
    no ``zotero-key``) · ``no_item`` (no record's Zotero item has that key in
    the last scan — the item was deleted, or Better BibTeX has not exported
    it yet) · ``target_exists`` (a note already has the new name) ·
    ``linked_from`` (other notes link to the old name)."""
    from audit.core import obsidian as core_obs

    vault_root = Path(settings.vault_root)
    by_item = {}
    for key, rec in inv.records.items():
        zi = rec.zotero_item
        if zi is not None and rec.bib_entry is not None and getattr(zi, "item_key", None):
            by_item[zi.item_key] = key
    # "entry", not "row": tests/js/auditTerms.test.js collects the note-sync
    # STATES by grepping this file for row["state"] assignments, and these
    # are a different vocabulary.
    notes = [p for p in _key_change_candidates(inv) if only is None or p.stem == only]
    rows: list[dict[str, Any]] = []
    for path in notes:
        info = core_obs.read_note(path)
        item_key = str(((info.frontmatter if info else {}) or {}).get(FM_ITEM_KEY) or "")
        entry: dict[str, Any] = {"old_key": path.stem, "note_rel": str(path.relative_to(vault_root)),
                                 "item_key": item_key or None, "new_key": None,
                                 "state": "ready"}
        if not item_key:
            entry["state"] = "no_item_key"
        elif item_key not in by_item:
            entry["state"] = "no_item"
        else:
            entry["new_key"] = by_item[item_key]
            entry["new_rel"] = str(path.with_name(f"{entry['new_key']}.md").relative_to(vault_root))
            if (vault_root / entry["new_rel"]).exists():
                entry["state"] = "target_exists"
        rows.append(entry)
    ready = {r["old_key"] for r in rows if r["state"] == "ready"}
    # The candidates' own paths are passed only so a note's link to ITSELF is
    # ignored (2026-09-21 review: they used to be skipped wholesale, which hid
    # a link from one orphan note to another, so the listing offered a rename
    # that the single-note POST then refused).
    links, unverified = _incoming_links(vault_root, ready,
                                        {vault_root / r["note_rel"] for r in rows})
    for r in rows:
        if r["old_key"] in links:
            r["state"] = "linked_from"
            r["linked_from"] = sorted(links[r["old_key"]])[:20]
        elif r["state"] == "ready" and unverified:
            # Some file could not be inspected, so "nothing links here" is not
            # known. Refuse rather than rename past a link we did not read.
            r["state"] = "links_unverified"
            r["unverified"] = unverified[:20]
    return rows


def _renamed_note_text(raw: str, old: str, new: str) -> tuple[str | None, list[str], str | None]:
    """The note's text under its new key: the old key added to ``aliases``
    (so Obsidian's search and link autocomplete still find it), and the
    generated header line ``>old`` rewritten to ``>new``. Returns
    ``(text, changes, refusal)``."""
    from api.routes._audit_tagwrite import add_aliases

    text, added, reason = add_aliases(raw, [old])
    if text is None:
        return None, [], f"aliases: {reason}"
    changes = [f"alias {old}"] if added else []
    parts = split_note_parts(text)
    if parts is not None:
        prefix = parts[0]
        rewritten = prefix
        pat = re.compile(rf"(?m)^>{re.escape(old)}[ \t]*$")
        if len(pat.findall(rewritten)) == 1:
            rewritten = pat.sub(f">{new}", rewritten, count=1)
            changes.append("header line")
        pat_ck = re.compile(rf"(?m)^citekey:[ \t]*{re.escape(old)}[ \t]*$")
        if len(pat_ck.findall(rewritten)) == 1:
            rewritten = pat_ck.sub(f"citekey: {new}", rewritten, count=1)
            changes.append("citekey")
        if rewritten != prefix:
            text = rewritten + text[len(prefix):]
    return text, changes, None


@notesync_bp.route("/api/notesync/key-changes")
def api_notesync_key_changes():
    """Reference notes whose citation key changed in Zotero. Read-only."""
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "Run a Library Audit scan first."}), 404
    rows = _key_change_rows(inv, settings)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    return jsonify({"rows": rows, "count": len(rows), "counts": counts})


@notesync_bp.route("/api/notesync/rename-note", methods=["POST"])
def api_notesync_rename_note():
    """Rename one reference note to its record's new citation key.

    ``{citation_key: <old key>, confirm: true | dry_run: true}``. The new key
    is derived server-side from the note's ``zotero-key``; the client names
    only the note. Journalled as ``rename_note`` and reverted from Note
    Refactor → Restore.
    """
    data = request.get_json(silent=True) or {}
    old = coerce_string_max_len(data.get("citation_key"), 512)
    if not old or any(c in old for c in ("\x00", "\n", "\r", "/", "\\")):
        return jsonify({"error": "citation_key is required."}), 400
    dry_run = coerce_bool(data.get("dry_run")) is True
    if not dry_run and coerce_bool(data.get("confirm")) is not True:
        return jsonify({"error": "confirm: true (or dry_run: true) is required."}), 400
    if audit_manager.is_scanning():
        return jsonify({"error": "A scan is running; try again when it ends."}), 409
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return jsonify({"error": "Run a Library Audit scan first."}), 404
    rows = _key_change_rows(inv, settings, only=old)
    if not rows:
        return jsonify({"error": f"No reference note named {old} is orphaned "
                                 "by the last scan."}), 404
    row = rows[0]
    if row["state"] != "ready":
        messages = {
            "no_item_key": "The note carries no zotero-key, so its item cannot be found.",
            "no_item": (f"No record's Zotero item has key {row['item_key']} in the last "
                        "scan (deleted, or not exported by Better BibTeX yet)."),
            "target_exists": f"A note named {row.get('new_key')}.md already exists.",
            "linked_from": ("Other notes link to this one by its old name. Rename it in "
                            "Obsidian instead, which rewrites those links."),
            "links_unverified": ("Some vault files could not be inspected for links to "
                                 "this note (unreadable, or a canvas that is not valid "
                                 f"JSON): {', '.join(row.get('unverified') or [])}. Fix "
                                 "or remove them, or rename the note in Obsidian."),
        }
        return jsonify({"error": messages.get(row["state"], row["state"]),
                        "reason": row["state"], "row": row}), 409
    vault_root = Path(settings.vault_root)
    old_rel, new_rel, new = row["note_rel"], row["new_rel"], row["new_key"]
    try:
        before = (vault_root / old_rel).read_bytes()
        raw = before.decode("utf-8")
    except UnicodeDecodeError:
        return jsonify({"error": "That note is not valid UTF-8; refusing to rewrite it."}), 422
    except OSError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 500
    text, changes, refusal = _renamed_note_text(raw, old, new)
    if refusal is not None:
        return jsonify({"error": f"The note's frontmatter cannot be edited safely ({refusal}).",
                        "reason": "unsafe_frontmatter"}), 422
    plan = {"old_key": old, "new_key": new, "old_rel": old_rel, "new_rel": new_rel,
            "item_key": row["item_key"], "changes": changes}
    if dry_run:
        return jsonify({"ok": True, "dry_run": True, "plan": plan})

    from rag.vault import obsidian_manager
    from vault_history import journal
    from vault_history.rename_note import rename_note

    cfg = load_config()
    op_epoch = obsidian_manager.try_acquire_lock(ttl=600)
    if not op_epoch:
        return jsonify({"error": "Indexing is in progress; try again shortly."}), 503
    try:
        jman = journal.load(vault_root, cfg)
        op_id, status, err = rename_note(
            vault_root, cfg, old_rel, new_rel, before, text.encode("utf-8"), jman,
            detail=f"citation key {old} → {new} (Zotero item {row['item_key']})")
    except journal.JournalUnreadableError as exc:
        return jsonify({"error": sanitise_error_msg(exc)}), 503
    finally:
        obsidian_manager.release_lock(op_epoch)
    if status != "applied":
        return jsonify({"error": err or "rename failed", "op_id": op_id or None}), 409
    _clear_states_cache()
    body = {"ok": True, "op_id": op_id, "plan": plan,
            "note": ("The next scan joins the note to its record. Its note-sync "
                     "sync point was kept under the old key, so re-baseline it "
                     "(papermind notesync bootstrap).")}
    if err:
        body["warning"] = err
    return jsonify(body)
