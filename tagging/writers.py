"""Domain writers for the biblio Tagging feature (extracted 2026-07-19).

The per-target tag writers, their revert counterparts, and the shared
validation/canonicalisation that every write endpoint runs — moved out of
``api/routes/tagging.py`` (the 2026-07-19 audit's controller-bloat finding) so
the Flask layer keeps only HTTP parsing, lock acquisition, and thin delegation.

Layering: this module imports app services (``audit.manager``, ``core.config``,
``core.paths``) and the tagging core (``journal``, ``vocab``, ``zotero_write``)
at module level; the Finder/frontmatter writers (``api.routes._finder_tagwrite``,
``api.routes._audit_tagwrite``) stay FUNCTION-LOCAL lazy imports — the same
pattern the route used — so ``tagging/`` never hard-depends on the route layer
and the vendored ``audit/`` tree stays read-only. ``core.utils`` (audit log +
atomic writer) is likewise imported at call time.

Locking contract: every ``*_locked`` function assumes the caller holds the
obsidian op-lock (single manifest writer); ``_zotero_prepare`` is the
deliberate exception — it is the READ phase that must run BEFORE the lock.
The route re-exports these names, so historical ``api.routes.tagging._x``
patch targets in tests keep working.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path

from api.security import sanitise_error_msg
from api.validators import coerce_bool, coerce_non_empty_string, coerce_string_max_len
from audit.manager import audit_manager
from core.config import load_config
from core.paths import resolve_under_root
from core.utils import nfc
from tagging import journal, zotero_write
from tagging.vocab import build_vocab

logger = logging.getLogger(__name__)

# ``_tags.md`` is shared with the vault thesaurus; reuse its config key + default
# so the tag vocabulary and the thesaurus can never diverge on which file wins.
_TAGS_PATH_KEY = "vault_thesaurus_tags_path"
_TAGS_PATH_DEFAULT = "_tags.md"


def _is_under(path: Path, root: Path) -> bool:
    """True iff *path* is *root* or lives beneath it (both already resolved)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

def _read_vocab(cfg: dict, vault_root: Path) -> tuple[dict[str, str] | None, str | None]:
    """Resolve + parse the vault's ``_tags.md`` into ``({tag: gloss}, rel)``.

    Returns ``(None, None)`` when the file is unconfigured, missing, escapes the
    vault, or is unreadable — the caller maps that to a 404 (vocab view) or a 400
    (tag write can't validate against an absent controlled vocabulary).
    """
    rel_raw = str(cfg.get(_TAGS_PATH_KEY) or _TAGS_PATH_DEFAULT).strip()
    if not rel_raw:
        return None, None
    rel = resolve_under_root(rel_raw, vault_root, must_exist=True, must_be_file=True)
    if rel is None:
        return None, None
    try:
        text = (vault_root / rel).read_text(encoding="utf-8")
    except OSError:
        return None, None
    return build_vocab(text), rel

def _tag_key(value) -> str:
    """The comparison key for a tag: NFC-normalised, then lowercased.

    Every cross-store tag comparison in this module goes through here. macOS
    stores Finder tags (and Zotero can hold) accented text in NFD while the
    vault's ``_tags.md`` and the browser send NFC, so a ``.lower()``-only
    comparison read « rétablissement » and « rétablissement » as two different
    tags — unioning a visual duplicate onto a paper, and failing to find the
    tag a removal targeted (2026-07-20 audit; fixed 2026-07-30, same
    normalisation boundary ``core.utils.nfc`` was adopted for filenames in B2).

    KEY ONLY — never a stored value. Finder's raw colour-suffixed entries and
    Zotero's full tag dicts must round-trip byte-identically for Restore, so
    callers compare through this and write back the originals. ``.lower()``
    (not ``casefold()``) deliberately preserves the pre-existing case semantics:
    this fix is about normalisation, not about widening case matching.
    """
    return nfc(str(value or "")).lower()


def _canonicalise_tags(requested, vocab):
    """Return ``(valid, rejected)``: requested tags mapped to canonical vocab ids.

    Case-insensitive, NFC-insensitive (see :func:`_tag_key`), ``#``-stripped,
    deduped. Out-of-vocabulary tags land in ``rejected`` and are dropped — the
    shared server-side gate for every tagging write, so the client can never
    inject an arbitrary tag onto any target.
    """
    vocab_lower = {_tag_key(t): t for t in vocab}
    valid: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for t in requested:
        c = str(t or "").strip().lstrip("#").strip()
        if not c:
            continue
        key = _tag_key(c)
        if key in vocab_lower:
            if key not in seen:
                valid.append(vocab_lower[key])
                seen.add(key)
        else:
            rejected.append(c)
    return valid, rejected

def _prepare_tag_write(data):
    """Shared validation for every tag-write endpoint.

    Returns ``(ctx, None)`` on success or ``(None, (response_dict, status))`` on
    error. ``ctx`` carries ``rec``/``settings``/``cfg``/``vault_root``/
    ``biblio_dir``/``valid``/``rejected``/``citation_key``. Checks confirm, the
    citation key, the non-empty tag list, that a scan ran and the key exists, and
    intersects the requested tags with the ``_tags.md`` vocabulary (the security
    gate). Per-target presence (a note / a Zotero item / a biblio PDF) is left to
    each caller so the fan-out ``apply-all`` can *skip* a missing target rather
    than fail the whole write.
    """
    if not isinstance(data, dict):
        return None, ({"error": "Invalid JSON"}, 400)
    if coerce_bool(data.get("confirm")) is not True:
        return None, ({"error": "Tag write must be explicitly confirmed (confirm: true)."}, 400)
    citation_key = coerce_non_empty_string(data.get("citation_key"), max_len=128)
    if not citation_key:
        return None, ({"error": "citation_key required"}, 400)
    raw_tags = data.get("tags")
    if not isinstance(raw_tags, list) or not raw_tags:
        return None, ({"error": "tags must be a non-empty list"}, 400)
    requested = [coerce_string_max_len(t, 128) or "" for t in raw_tags[:200]]

    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return None, ({"error": "No scan results. Run an audit scan first."}, 404)
    rec = inv.records.get(citation_key)
    if rec is None:
        return None, ({"error": "Unknown citation key."}, 404)

    cfg = load_config()
    vault_root = settings.vault_root
    vocab, _ = _read_vocab(cfg, vault_root)
    if not vocab:
        return None, ({"error": "Tag vocabulary (_tags.md) is unavailable; cannot validate tags."}, 400)
    valid, rejected = _canonicalise_tags(requested, vocab)
    if not valid:
        return None, ({
            "error": "None of the requested tags are in the controlled vocabulary.",
            "rejected": sorted(set(rejected)),
        }, 400)
    return {
        "rec": rec, "settings": settings, "cfg": cfg, "vault_root": vault_root,
        "biblio_dir": settings.biblio_articles_dir, "valid": valid,
        "rejected": rejected, "citation_key": citation_key,
    }, None

def _target_result(result, status):
    """Map a per-target ``(_write_*_locked)`` ``(dict, status)`` into an apply-all sub-result.

    ``partial``/``failed`` (the Finder writer's per-PDF failure detail) must
    survive this projection: apply-all is the only caller that discards the
    full ``pdfs`` array, and dropping the failure summary too made a
    half-failed Finder write read as a clean success in the deck's status line.
    """
    if status == 200:
        out = {
            "ok": True, "added": result.get("added", []),
            "rejected": result.get("rejected", []),
            "op_id": result.get("op_id"), "message": result.get("message"),
        }
        if result.get("partial"):
            out["partial"] = True
            out["failed"] = result.get("failed", [])
        return out
    return {"ok": False, "error": result.get("error"), "status": status}

def _note_write_locked(*, rec, valid, rejected, want_sha, cfg, vault_root, citation_key,
                       manifest=None, batch_id=None, defer_save=False):
    """Union ``valid`` tags into a record's note frontmatter. Caller holds the lock.

    Returns ``(result_dict, http_status)``. Snapshots + journals the note before
    the atomic write so the op is reversible; honours an optional ``want_sha``
    stale-diff guard (409). ``rec.obsidian_note`` MUST be present (caller checks).

    ``manifest``: a caller-owned journal manifest (apply-all loads once and
    prunes/saves once for the whole fan-out); ``None`` = load/prune/save here,
    the standalone-endpoint behaviour. In both modes the op record is SAVED
    right after it is appended, before the target write runs (crash-trail) —
    unless ``defer_save`` opts out (see below).

    ``batch_id`` / ``defer_save`` mirror :func:`_note_remove_locked` exactly,
    added 2026-08-13 for the harmonize batch. ``batch_id`` is stamped HERE,
    before this helper's own append+save, because stamping it afterwards in
    the caller leaves a window where a cancel or crash persists the op
    applied-but-unstamped, dropping it from the one-click batch restore.
    ``defer_save`` (batch callers only, requires a shared ``manifest``) skips
    this helper's journal transactions. SQLite/WAL makes each append
    history-independent, but one FULL-synchronous transaction per record still
    creates avoidable fsync and lock churn. The per-op note SNAPSHOT stays
    durable before the write regardless (it is its own file); only op-record
    persistence is batched, and the caller MUST checkpoint + final-save itself.
    """
    from api.routes._audit_tagwrite import merge_tags_into_frontmatter
    from core.utils import log_vault_write, write_text_atomic

    note_path = rec.obsidian_note.path
    try:
        rel = str(note_path.resolve().relative_to(vault_root.resolve()))
    except (OSError, ValueError):
        return {"error": "Note path escapes the vault root."}, 400
    try:
        raw_bytes = note_path.read_bytes()
        original = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": f"Cannot read note: {sanitise_error_msg(exc)}"}, 400

    cur_sha = hashlib.sha256(raw_bytes).hexdigest()
    if want_sha and want_sha != cur_sha:
        return {
            "error": "Note changed on disk since it was loaded; reload and retry.",
            "expected_sha256": cur_sha,
        }, 409

    new_text, added, reason = merge_tags_into_frontmatter(original, valid)
    if new_text is None:
        return {"error": f"Cannot safely edit frontmatter: {reason}"}, 422
    if not added:
        return {
            "ok": True, "added": [], "rejected": sorted(set(rejected)),
            "note": rel, "content_sha256": cur_sha,
            "message": "Tags already present.",
        }, 200

    new_bytes = new_text.encode("utf-8")
    hash_after = hashlib.sha256(new_bytes).hexdigest()

    # Snapshot + record the op BEFORE the write so a crash leaves a restorable
    # trail (snapshot durable, op in the manifest).
    shared = manifest is not None
    try:
        if not shared:
            manifest = journal.load(vault_root, cfg)
        op_id = journal.new_op_id(manifest)
        snapshot_rel = journal.write_note_snapshot(vault_root, cfg, rel, op_id, raw_bytes)
    except journal.ScopeError as exc:
        return {"error": f"Tagging archive misconfigured: {exc}"}, 400
    op = {
        "op_id": op_id, "kind": "note_tag", "note_rel": rel,
        "citation_key": citation_key, "added": added,
        "hash_before": cur_sha, "hash_after": hash_after,
        "snapshot_rel": snapshot_rel, "ts": journal.now_iso(), "state": "applied",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    if not shared:
        # protect_op_ids: never evict the op THIS call just appended —
        # with a manifest full of un-evictable applied finder/zotero ops
        # the cap loop would drop it (and unlink its snapshot) before the
        # target write, 404-ing the undo the response hands back.
        journal.prune(vault_root, cfg, manifest,
                      protect_op_ids={op["op_id"]})
    # The save after each append stays in BOTH modes (the crash-trail: the op
    # record must be durable before the target write); shared mode only skips
    # the per-target load + prune, which apply-all does once for the batch.
    # `defer_save` batches it further — the snapshot above is already durable.
    if not (shared and defer_save):
        journal.save(vault_root, cfg, manifest)

    log_vault_write("tagging_note_tag_add", str(note_path),
                    f"+{len(added)} tags: {', '.join(added)}")
    try:
        write_text_atomic(str(note_path), new_text)
    except OSError as exc:
        op["state"] = "failed"
        if not (shared and defer_save):
            journal.save(vault_root, cfg, manifest)
        return {"error": f"Write failed: {sanitise_error_msg(exc)}"}, 500

    return {
        "ok": True, "added": added, "rejected": sorted(set(rejected)),
        "note": rel, "citation_key": citation_key, "op_id": op_id,
        "content_sha256": hash_after,
    }, 200

OP_NOTE_PDF_LINKS = "note_pdf_links"


def _note_pdf_links_locked(*, rec, names, want_sha, cfg, vault_root, citation_key,
                           manifest=None, batch_id=None, defer_save=False):
    """Write a record's ``pdf:`` frontmatter list. Caller holds the lock.

    The same shape as :func:`_note_write_locked` — snapshot, journal, atomic
    write — over a different frontmatter key. Kept separate rather than
    generalising that function: tags UNION and are vocabulary-gated, this
    REPLACES and is gated on the file actually being one of the record's
    mapped PDFs, and merging the two would put both policies behind one flag.

    Returns ``(result, status)``. A note whose list this app did not write is
    refused (``dirty_list`` → 422), never overwritten.
    """
    from api.routes._audit_tagwrite import read_pdf_links, set_pdf_links
    from core.utils import log_vault_write, write_text_atomic

    note_path = rec.obsidian_note.path
    try:
        rel = str(note_path.resolve().relative_to(vault_root.resolve()))
    except (OSError, ValueError):
        return {"error": "Note path escapes the vault root."}, 400
    try:
        raw_bytes = note_path.read_bytes()
        original = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": f"Cannot read note: {sanitise_error_msg(exc)}"}, 400

    cur_sha = hashlib.sha256(raw_bytes).hexdigest()
    if want_sha and want_sha != cur_sha:
        return {"error": "Note changed on disk since it was loaded; reload and retry.",
                "expected_sha256": cur_sha}, 409

    if read_pdf_links(original) == list(names):
        return {"ok": True, "note": rel, "links": list(names), "changed": False,
                "content_sha256": cur_sha,
                "message": "Already linked."}, 200
    new_text, reason = set_pdf_links(original, list(names))
    if new_text is None:
        return {"error": f"Cannot safely edit frontmatter: {reason}",
                "reason": reason}, 422

    new_bytes = new_text.encode("utf-8")
    hash_after = hashlib.sha256(new_bytes).hexdigest()
    shared = manifest is not None
    try:
        if not shared:
            manifest = journal.load(vault_root, cfg)
        op_id = journal.new_op_id(manifest)
        snapshot_rel = journal.write_note_snapshot(vault_root, cfg, rel, op_id, raw_bytes)
    except journal.ScopeError as exc:
        return {"error": f"Tagging archive misconfigured: {exc}"}, 400
    except OSError as exc:
        # The snapshot could not be written (disk full, archive unmounted):
        # nothing was changed and nothing journalled, so this note fails ON
        # ITS OWN and a batch continues (audit remediation 2026-09-23, N1).
        # Raised, it aborted the whole batch with a 500 while the notes
        # already written in it had their ops only in memory.
        return {"error": f"Could not snapshot the note before writing: "
                         f"{sanitise_error_msg(exc)}", "reason": "snapshot_failed"}, 500
    op = {
        "op_id": op_id, "kind": OP_NOTE_PDF_LINKS, "note_rel": rel,
        "citation_key": citation_key, "links": list(names),
        "hash_before": cur_sha, "hash_after": hash_after,
        "snapshot_rel": snapshot_rel, "ts": journal.now_iso(), "state": "applied",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    if not shared:
        journal.prune(vault_root, cfg, manifest, protect_op_ids={op["op_id"]})
    if not (shared and defer_save):
        journal.save(vault_root, cfg, manifest)

    log_vault_write("tagging_note_pdf_links", str(note_path),
                    f"pdf: {', '.join(names) or '(cleared)'}")
    try:
        write_text_atomic(str(note_path), new_text)
    except OSError as exc:
        op["state"] = "failed"
        if not (shared and defer_save):
            journal.save(vault_root, cfg, manifest)
        return {"error": f"Write failed: {sanitise_error_msg(exc)}"}, 500
    return {"ok": True, "note": rel, "links": list(names), "changed": True,
            "op_id": op_id, "content_sha256": hash_after}, 200


def _finder_write_locked(*, rec, valid, rejected, cfg, vault_root, biblio_dir, citation_key,
                         manifest=None, batch_id=None, defer_save=False, pdf_paths=None,
                         expect_doc=None):
    """Union ``valid`` into each biblio PDF's Finder tags. Caller holds the lock.

    Returns ``(result_dict, http_status)``. macOS-only (caller gates); the
    before-lists ARE the restore snapshot (only the xattr changes). A PDF already
    carrying the tags is skipped. Returns 404 when the key has no biblio PDF.
    ``manifest`` / ``batch_id`` / ``defer_save`` as in
    :func:`_note_write_locked`.

    One asymmetry with the note writer under ``defer_save``: this op's restore
    mapping is the inline ``before``/``before_raw`` lists, which live only in
    the manifest — there is no sidecar snapshot file. So a crash between the
    xattr write and the caller's next checkpoint loses the colours (the
    `log_vault_write` trail keeps the names). That is the same durability
    window the retire batch already accepts, and it is why the caller's
    checkpoint interval is small.
    """
    from api.routes import _finder_tagwrite
    from core.utils import log_vault_write

    # ``pdf_paths`` (library-workflow lot 3) names the PDF explicitly instead
    # of trusting the cached scan's ``rec.pdf_paths``, which a rename made
    # since the scan leaves pointing at a name that no longer exists. Only
    # `api/routes/tagging.py::_unit_pdf` produces it, from the import
    # tracker's identity check; containment is still re-checked below for
    # every PDF, so the parameter widens WHICH file is named, never where it
    # may live (the `_finder_remove_locked` precedent).
    source = pdf_paths if pdf_paths is not None else rec.pdf_paths
    biblio_pdfs = [Path(p) for p in source if _is_under(Path(p), biblio_dir)]
    if not biblio_pdfs:
        return {"error": "No biblio_articles PDF for that citation key."}, 404

    changes: list[dict] = []
    read_errors: list[dict] = []
    # Kept apart from `read_errors` (audit remediation 2026-09-23, N5): a PDF
    # REPLACED since the import tracker observed it is not a read failure. It
    # used to fall into the "could not read the Finder tags" 500 — which the
    # library run takes as an outcome it cannot know — when the answer is a
    # plain, safe refusal: analyse again.
    identity_errors: list[dict] = []
    for pdf in biblio_pdfs:
        try:
            pdf_rel = str(pdf.resolve().relative_to(vault_root.resolve()))
        except (OSError, ValueError):
            continue  # defence in depth — skip a PDF that escapes the vault
        if not _is_under(pdf.resolve(), biblio_dir.resolve()):
            continue
        if expect_doc is not None:
            # The import tracker's identity, re-checked under the lock (the
            # caller holds it): the file at this path must still be the one
            # observed. What remains is the microseconds between this stat
            # and the xattr write below — no lock can cover an external
            # replacement, and Restore reverts by path either way.
            try:
                from accession import review as _review
                fp = _review.fingerprint(pdf)
                same = f"doc:{fp['ino']}:{fp.get('birth')}" == expect_doc
            except OSError:
                same = False
            if not same:
                identity_errors.append({"rel": pdf_rel,
                                        "error": "The PDF was replaced since it was observed."})
                continue
        # Raw (colour-suffixed) entries, not names: the write below PATCHes the
        # whole xattr array back, so existing tags must round-trip verbatim or
        # every colour the user assigned in Finder is silently reset (the
        # Zotero writer's before_raw rule). A raised read failure REFUSES this
        # PDF instead of unioning against an empty set and clobbering it.
        try:
            before_raw = _finder_tagwrite.read_finder_tags_full(str(pdf))
        except RuntimeError as exc:
            read_errors.append({"rel": pdf_rel, "error": sanitise_error_msg(exc)})
            continue
        before = [_finder_tagwrite.tag_name(v) for v in before_raw]
        before_lower = {_tag_key(t) for t in before}
        added = [t for t in valid if _tag_key(t) not in before_lower]
        if not added:
            continue  # already a superset — nothing to write for this PDF
        # Union with `added`, NOT `valid`: `added` already excludes tags the
        # PDF carries in a different case, so a `neuro` xattr never gains a
        # duplicate `Neuro` sibling (the Zotero writer computes `after` the
        # same way — the two must not diverge). Existing entries ride raw;
        # only the new tags are bare names (a fresh tag has no colour yet).
        after_raw = list(before_raw) + added
        after = before + added
        changes.append({"rel": pdf_rel, "abs": str(pdf),
                        "before": before, "after": after,
                        "before_raw": before_raw, "after_raw": after_raw,
                        "added": added})

    if identity_errors and not read_errors and not changes:
        return {"error": "The PDF was replaced since it was observed; analyse again.",
                "reason": "replaced", "pdfs": identity_errors}, 409
    if (read_errors or identity_errors) and not changes:
        return {"error": "Could not read the PDF's current Finder tags.",
                "pdfs": read_errors + identity_errors}, 500

    if not changes:
        return {
            "ok": True, "added": [], "rejected": sorted(set(rejected)),
            "citation_key": citation_key, "pdfs": [],
            "message": "All PDFs already carry these tags.",
        }, 200

    # Record the op (before-lists are the inline snapshot) BEFORE writing.
    shared = manifest is not None
    try:
        if not shared:
            manifest = journal.load(vault_root, cfg)
        op_id = journal.new_op_id(manifest)
    except journal.ScopeError as exc:
        return {"error": f"Tagging archive misconfigured: {exc}"}, 400
    op = {
        "op_id": op_id, "kind": "finder_tag", "citation_key": citation_key,
        "pdfs": changes, "ts": journal.now_iso(), "state": "applied",
        # a compact 'added' summary for the manifest/restore UI
        "added": sorted({t for ch in changes for t in ch["added"]}),
    }
    if batch_id:
        op["batch_id"] = batch_id
    # Durable BEFORE the xattr writes below (see this function's docstring):
    # a crash after a write but before the manifest checkpoint must still
    # leave the colours recoverable.
    try:
        op["snapshot_rel"] = journal.write_external_snapshot(
            vault_root, cfg, op_id,
            {"kind": "finder_tag", "citation_key": citation_key,
             "pdfs": changes})
    except (OSError, journal.ScopeError):
        # A sidecar failure must not block the write: the inline lists are
        # still recorded, which is exactly the pre-2026-09-02 durability.
        logger.warning("finder-tag restore sidecar could not be written",
                       exc_info=True)
    manifest["ops"].append(op)
    if not shared:
        # protect_op_ids: never evict the op THIS call just appended —
        # with a manifest full of un-evictable applied finder/zotero ops
        # the cap loop would drop it (and unlink its snapshot) before the
        # target write, 404-ing the undo the response hands back.
        journal.prune(vault_root, cfg, manifest,
                      protect_op_ids={op["op_id"]})
    if not (shared and defer_save):
        journal.save(vault_root, cfg, manifest)

    # unreadable (and replaced) PDFs surface per-PDF
    results = [dict(e) for e in read_errors + identity_errors]
    any_ok = False
    ok_added: set[str] = set()
    failed_writes = 0
    for ch in changes:
        try:
            _finder_tagwrite.write_finder_tags(ch["abs"], ch["after_raw"])
            log_vault_write("tagging_finder_tag_add", ch["abs"],
                            f"+{len(ch['added'])} tags: {', '.join(ch['added'])}")
            results.append({"rel": ch["rel"], "added": ch["added"]})
            ok_added.update(ch["added"])
            any_ok = True
        except Exception as exc:  # noqa: BLE001
            failed_writes += 1
            results.append({"rel": ch["rel"], "error": sanitise_error_msg(exc)})
    if not any_ok:
        op["state"] = "failed"
        if not (shared and defer_save):
            journal.save(vault_root, cfg, manifest)
        return {"error": "All Finder-tag writes failed.", "pdfs": results}, 500

    # Truth over optimism (2026-07-20 audit): `added` reports only tags that
    # actually LANDED on some PDF — the pre-fix union over all planned changes
    # claimed failed writes as applied, and apply-all (which keeps only the
    # summary fields, not `pdfs`) then reported a clean success to the deck.
    # The op record's summary is corrected too (it feeds the Restore UI and
    # the propose exemplars); its per-PDF before-lists stay complete, and the
    # revert is already per-PDF conservative about a write that never landed.
    partial = failed_writes > 0 or bool(read_errors) or bool(identity_errors)
    if sorted(ok_added) != op["added"]:
        op["added"] = sorted(ok_added)
        if not (shared and defer_save):
            journal.save(vault_root, cfg, manifest)
    result = {
        "ok": True, "added": op["added"], "rejected": sorted(set(rejected)),
        "citation_key": citation_key, "pdfs": results, "op_id": op_id,
    }
    if partial:
        result["partial"] = True
        result["failed"] = [e for e in results if e.get("error")]
    return result, 200

def _revert_finder_tag(vault_root, cfg, op):
    """Revert a ``finder_tag`` op: per PDF, restore ``before`` if still at ``after``.

    Conservative per PDF — a file whose current Finder tags no longer match what
    we wrote (edited in Finder since) is left untouched. macOS-only.

    The op flips to ``reverted`` ONLY when at least one PDF was actually
    restored: an all-skipped / all-failed pass must keep the op ``applied`` so
    it survives ``journal.prune`` (which reclaims spent ops) and stays
    retryable — the before-lists in this op are the ONLY restore mapping, so a
    state flip without a restore would destroy them. This mirrors the
    deliberate conservatism of ``_revert_zotero_tag`` and the journal's
    ``note_tag`` revert, which likewise never mark an un-restored op spent.
    """
    from api.routes import _finder_tagwrite
    from core.utils import log_vault_write

    if not _finder_tagwrite.is_macos():
        return {"ok": False, "status": "error", "message": "Finder tags are macOS-only."}
    reverted = 0
    skipped = 0
    failed = 0
    # The inline lists stay the first source. The sidecar (2026-09-02) is the
    # recovery-of-last-resort path: an op reconstructed from a crash — or one
    # whose payload was lost — still has its before-lists on disk, named by op
    # id, exactly as a note op has its .bak.
    changes = op.get("pdfs") or []
    if not changes and op.get("snapshot_rel"):
        try:
            raw = journal.read_snapshot(vault_root, cfg, op["snapshot_rel"])
            changes = (json.loads(raw.decode("utf-8")) or {}).get("pdfs") or []
        except (OSError, ValueError, journal.ScopeError):
            changes = []
    for ch in changes:
        abs_path = ch.get("abs")
        if not abs_path:
            skipped += 1
            continue
        # Compare NAMES, not raw entries: a colour change alone (metadata, not
        # tag identity) must not block the revert — mirroring the Zotero
        # revert's name-level "unchanged since our write?" comparison.
        # A deleted PDF is a SKIP (there is nothing left to restore, and an
        # op kept "applied" for it would just retry forever); an EXISTING but
        # unreadable one is a FAILURE — see below.
        if not os.path.exists(abs_path):
            skipped += 1
            continue
        # Read via the RAISING reader, never the []-swallowing display reader:
        # a swallowed read failure (iCloud-dataless timeout) looked like
        # "tags changed" and was tallied `skipped`, so a one-of-two revert
        # could still flip the op to `reverted` and let journal.prune destroy
        # the unrestored PDF's only before-list (2026-07-22 audit M1 — the
        # write path already made this exact read raising; the revert lagged).
        try:
            current = [_finder_tagwrite.tag_name(v)
                       for v in _finder_tagwrite.read_finder_tags_full(abs_path)]
        except RuntimeError:
            logger.warning("finder-tag revert: unreadable xattrs for %s", abs_path,
                           exc_info=True)
            failed += 1
            continue
        after = ch.get("after") or []
        if sorted(current) != sorted(after):
            skipped += 1
            continue
        try:
            # Restore the raw entries so pre-existing colours survive the
            # round-trip; ops journalled before 2026-07-19 stored names only
            # (their colours were already lost at write time).
            _finder_tagwrite.write_finder_tags(
                abs_path, ch.get("before_raw") or ch.get("before") or [])
            log_vault_write("tagging_restore_finder", ch.get("rel") or abs_path,
                            f"{len(after)}→{len(ch.get('before') or [])} tags")
            reverted += 1
        except Exception:  # noqa: BLE001
            # Keep going — other PDFs in the op may still be revertible — but
            # a real I/O failure is a FAILURE, not a skip: tallying it as
            # "skipped" let a one-of-two revert flip the op to `reverted`,
            # after which journal.prune reclaimed the before-lists that were
            # the failed PDF's ONLY restore mapping.
            logger.warning("finder-tag revert failed for %s", abs_path, exc_info=True)
            failed += 1
    # The op is spent only when nothing went wrong: any failure keeps it
    # `applied` (prune-safe, retryable — an already-reverted PDF no longer
    # matches `after` on retry, so it skips idempotently and only the failed
    # one is re-attempted).
    if reverted and not failed:
        op["state"] = "reverted"
        status = "reverted"
    elif failed:
        status = "partial" if reverted else "error"
    else:
        status = "skipped"  # op stays "applied" → retryable, prune-safe
    return {"ok": failed == 0, "status": status,
            "reverted": reverted, "skipped": skipped, "failed": failed,
            "message": (f"Reverted {reverted} PDF(s), skipped {skipped}"
                        + (f", FAILED {failed} — retryable" if failed else "") + ".")}

def _zotero_base(cfg: dict) -> str:
    """The LOCAL-API base. Kept for callers that only display an endpoint;
    anything that talks to Zotero must go through :func:`_zotero_transport`,
    which may select the web API instead."""
    return str(cfg.get("tagging_zotero_api_base") or zotero_write.DEFAULT_BASE_URL)

def _zotero_transport(cfg: dict):
    """Resolve the configured Zotero transport (local loopback vs web API)."""
    return zotero_write.resolve_transport(cfg)

def _zotero_lib_path(transport, zot):
    """Library segment for *zot*, resolving the real userID on the web transport.

    The local API accepts the magic ``users/0``; api.zotero.org does not — it
    needs the numeric userID behind the key. Group items address the same
    ``groups/<id>`` path on both.
    """
    if transport.is_web and not (
            getattr(zot, "library_type", None) == "group" and getattr(zot, "group_id", None)):
        uid = zotero_write.resolve_user_id(transport.base_url, transport.api_key)
        return zotero_write.library_path(zot, user_id=uid)
    return zotero_write.library_path(zot)

def _zotero_read_identity(transport, zot):
    """A Desktop selection cannot authorize a Web lookup of the same key."""
    from audit.core.zotero import ZoteroItem

    server_id = getattr(zot, "server_id", None)
    if transport.is_web:
        if (server_id or isinstance(zot, ZoteroItem)
                or getattr(zot, "source_transport", None) != "web"):
            raise zotero_write.ZoteroError(
                "This selection comes from Zotero Desktop. Writing via Web requires "
                "an independently verified Web selection; no request was sent.")
        return {}
    if not server_id:
        raise zotero_write.ZoteroError(
            "The scanned item has no Zotero database identity. Refresh the Library "
            "Audit from the intended Zotero database before writing; no request was sent.")
    return {"server_id": server_id}


def _zotero_prepare(*, rec, valid, rejected, cfg, citation_key):
    """READ phase — runs with NO lock held: GET the item's tags, compute the union.

    Returns ``(prepared, None, None)`` when there is something to write, or
    ``(None, result_dict, status)`` for every early exit (unreachable / API
    error / tags already present). Running this outside the obsidian op-lock
    is safe because the commit's PATCH carries ``If-Unmodified-Since-Version``
    — a concurrent Zotero edit between read and PATCH surfaces as 412 → 409,
    never a lost update. Pre-2026-07-19 the GET ran inside the lock, so a
    wedged local Zotero (15 s timeout, ×2 calls) stalled ALL indexing and
    every other tag write for the duration.
    """
    zot = rec.zotero_item
    transport = _zotero_transport(cfg)
    if not transport.can_write:
        return None, {"error": (
            "The Zotero web API is selected but no API key is configured. "
            f"Set {zotero_write.API_KEY_ENV} in your .env, or switch "
            "tagging_zotero_transport to 'local'.")}, 503
    base = transport.base_url
    item_key = zot.item_key

    try:
        # Resolving the library path can itself hit the network (the web
        # transport looks up the key's userID), so it shares this guard.
        identity = _zotero_read_identity(transport, zot)
        lib_path = _zotero_lib_path(transport, zot)
        # Full (dict) read, not name-only: the PATCH below rewrites the item's
        # whole tag array, so the existing tags must round-trip verbatim or
        # their metadata (automatic/manual `type`, colours) is silently reset.
        version, before_raw = zotero_write.get_item_tags_full(
            base, lib_path, item_key, api_key=transport.api_key,
            **identity)
    except zotero_write.ZoteroUnavailable as exc:
        return None, {"error": f"Zotero is not reachable: {sanitise_error_msg(exc)}"}, 503
    except zotero_write.ZoteroError as exc:
        return None, {"error": sanitise_error_msg(exc)}, 502

    before = zotero_write.tag_names(before_raw)
    before_lower = {_tag_key(t) for t in before}
    added = [t for t in valid if _tag_key(t) not in before_lower]
    if not added:
        return None, {
            "ok": True, "added": [], "rejected": sorted(set(rejected)),
            "citation_key": citation_key, "message": "Item already carries these tags.",
        }, 200
    # Existing tags ride as their original dicts; only the NEW ones are bare
    # names (set_item_tags wraps those — a fresh tag has no metadata yet).
    return {
        "base": base, "lib_path": lib_path, "item_key": item_key,
        # In-memory only — the key must never reach the journal manifest.
        "api_key": transport.api_key, "transport": transport.kind,
        "version": version, "before_raw": before_raw, "before": before,
        "added": added, "after": before + added,
        "after_payload": list(before_raw) + added,
    }, None, None

def _zotero_journal(*, prepared, citation_key, manifest, batch_id=None):
    """Append (do NOT save) the ``zotero_tag`` op for *prepared*.

    The ADD-side twin of :func:`_zotero_remove_journal`, added 2026-08-13 for
    the harmonize batch, and it exists for exactly the same reason: a batch
    caller journals every Zotero write under ONE brief op-lock hold, persists
    the manifest, RELEASES the lock, and runs the network PATCHes lock-free.
    A PATCH touches neither the vault nor the manifest, and the
    ``If-Unmodified-Since-Version`` guard — not the op-lock — is what prevents
    lost updates on the Zotero side. Holding the lock across N sequential
    PATCHes freezes every other vault write for the whole batch.

    Journalling BEFORE the PATCH keeps the crash trail conservative in the
    other direction from the removal twin: an ADD op whose PATCH never ran is
    conservatively SKIPPED by the revert — ``_revert_zotero_prepare``'s
    equality check sees the item still at ``before``, never ``after``, and
    declines to touch it — so the hazard is not a wrong write. It is a journal
    claiming a write that did not happen: the batch restore's accounting would
    count an op it can never revert, and a later prune could spend its
    before-list for nothing. Hence the batch caller flips an un-run op to
    ``failed`` when it is known not to have run. An intent starts as
    ``outcome_unknown`` and becomes ``applied`` only after a successful reply.
    """
    op = {
        "op_id": journal.new_op_id(manifest), "kind": "zotero_tag",
        "citation_key": citation_key,
        "item_key": prepared["item_key"], "lib_path": prepared["lib_path"],
        "base_url": prepared["base"],
        # `before`/`after` are NAME lists (manifest UI + the revert's
        # "unchanged since our write?" comparison); `before_raw` is the exact
        # dict array the revert PATCHes back so metadata survives the
        # round-trip. Ops written before 2026-07-18 lack before_raw — their
        # revert falls back to names (metadata was already lost at write time).
        "before": prepared["before"], "before_raw": prepared["before_raw"],
        "after": prepared["after"], "added": prepared["added"],
        # `transport` lets the revert re-derive whether an API key is needed.
        # The key ITSELF is never journalled — it is re-read from the
        # environment at revert time (provider-key discipline).
        "transport": prepared.get("transport") or "local",
        "version_before": prepared["version"], "server_id": getattr(prepared["version"], "server_id", None), "ts": journal.now_iso(),
        "after_raw": [t if isinstance(t, dict) else {"tag": t} for t in prepared["after_payload"]],
        "state": "outcome_unknown",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    return op


def _zotero_patch(prepared):
    """The lock-free network half of a Zotero tag ADD.

    Returns ``(result_dict, status, new_version | None)`` — 200 on success,
    the historical 409/503/502 mapping otherwise. Callers record the outcome
    on the journalled op themselves (under whatever lock they hold).
    """
    try:
        new_version = zotero_write.set_item_tags(
            prepared["base"], prepared["lib_path"], prepared["item_key"],
            prepared["after_payload"], prepared["version"],
            api_key=prepared.get("api_key"))
    except zotero_write.ZoteroVersionConflict:
        return {"error": "The item changed in Zotero; reload and retry."}, 409, None
    except zotero_write.ZoteroNotSent as exc:
        return {"error": f"Zotero is not reachable: {sanitise_error_msg(exc)}"}, 503, None
    except zotero_write.ZoteroUnavailable as exc:
        return {"error": f"Zotero response lost: {sanitise_error_msg(exc)}. Outcome unknown; inspect Restore before retrying.", "outcome_unknown": True}, 503, None
    except zotero_write.ZoteroError as exc:
        return {"error": sanitise_error_msg(exc),
                "outcome_unknown": bool(exc.http_code and exc.http_code >= 500)}, 502, None
    return {"ok": True}, 200, new_version


def _zotero_commit_locked(*, prepared, rejected, cfg, vault_root, citation_key,
                          manifest=None, batch_id=None):
    """WRITE phase — caller holds the lock: journal, then the guarded PATCH.

    Only the journal mutation and the single version-guarded PATCH run under
    the op-lock (worst case one network call, not the read+write pair). 409 on
    a version conflict / 503 became-unreachable / 502 other.
    ``manifest`` as in :func:`_note_write_locked`.

    Composed from :func:`_zotero_journal` + :func:`_zotero_patch` since
    2026-08-13; this single-item path keeps the historical
    journal→save→PATCH→save order and result shapes, so apply-all and the
    standalone endpoint are unchanged.
    """
    from core.utils import log_vault_write

    item_key = prepared["item_key"]
    added = prepared["added"]

    shared = manifest is not None
    try:
        if not shared:
            manifest = journal.load(vault_root, cfg)
    except journal.ScopeError as exc:
        return {"error": f"Tagging archive misconfigured: {exc}"}, 400
    op = _zotero_journal(prepared=prepared, citation_key=citation_key,
                         manifest=manifest, batch_id=batch_id)
    op_id = op["op_id"]
    if not shared:
        # protect_op_ids: never evict the op THIS call just appended —
        # with a manifest full of un-evictable applied finder/zotero ops
        # the cap loop would drop it (and unlink its snapshot) before the
        # target write, 404-ing the undo the response hands back.
        journal.prune(vault_root, cfg, manifest,
                      protect_op_ids={op["op_id"]})
    journal.save(vault_root, cfg, manifest)

    r, status, new_version = _zotero_patch(prepared)
    if status != 200:
        op["state"] = "outcome_unknown" if r.get("outcome_unknown") else "failed"
        journal.save(vault_root, cfg, manifest)
        return r, status
    op["state"] = "applied"
    op["version_after"] = new_version
    journal.save(vault_root, cfg, manifest)
    log_vault_write("tagging_zotero_tag_add", item_key,
                    f"+{len(added)} tags: {', '.join(added)}")

    return {
        "ok": True, "added": added, "rejected": sorted(set(rejected)),
        "citation_key": citation_key, "item_key": item_key, "op_id": op_id,
        # A web write lands on zotero.org and reaches the desktop app only on
        # its next sync, so a bare "✓" would be a small lie: the user who
        # alt-tabs to Zotero to spot-check sees nothing and concludes the write
        # failed. The UI appends a "syncing" note when this is set.
        "transport": prepared.get("transport") or "local",
        "sync_pending": (prepared.get("transport") == "web"),
    }, 200

def _revert_zotero_prepare(cfg, op):
    """Read side of a ``zotero_tag`` revert — runs OUTSIDE the obsidian op-lock.

    Does the version GET (which may honour a Zotero ``Backoff`` and sleep) and
    all early-exit checks (missing key, unreachable, changed-in-Zotero). Returns
    a dict: either ``{"early": <result>}`` (nothing to commit) or
    ``{"commit": {base, lib_path, item_key, api_key, cur_version, restore_tags}}``.

    Split out (2026-07-20 audit finding, D4): the old ``_revert_zotero_tag`` ran
    this GET (and its possible backoff sleep) while the route held the obsidian
    op-lock — a wedged/slow Zotero could stall indexing and other vault writes
    for up to the backoff + network window. The PATCH's
    ``If-Unmodified-Since-Version`` guard is what makes reading BEFORE the lock
    safe: a concurrent Zotero edit between this GET and the commit PATCH 412s
    (surfaced as an error, retryable) rather than a lost update — exactly the
    apply path's prepare/commit split.
    """
    base = op.get("base_url") or _zotero_base(cfg)
    lib_path = op.get("lib_path") or "users/0"
    item_key = op.get("item_key")
    if not item_key:
        return {"early": {"ok": False, "status": "invalid",
                          "message": "Op missing item_key."}}
    # The op records WHICH transport wrote it (the base_url + lib_path are
    # transport-specific and must be reused verbatim), but never the key —
    # that is re-read from the environment now. A web-written op therefore
    # becomes un-revertible if the key is removed, which is the correct
    # failure: `status="error"` keeps the op `applied` and retryable rather
    # than silently spending the only restore mapping.
    api_key = (zotero_write.api_key_from_env()
               if op.get("transport") == "web" else None)
    if op.get("transport") == "web" and not api_key:
        return {"early": {"ok": False, "status": "error",
                          "message": (f"This tag was written via the Zotero web API; "
                                      f"set {zotero_write.API_KEY_ENV} to revert it.")}}
    from tagging.local_api import restore_kwargs
    try:
        cur_version, raw_tags = zotero_write.get_item_tags_full(
            base, lib_path, item_key, api_key=api_key, **restore_kwargs(op))
        def canonical(tags):
            return sorted(json.dumps({"type": 0, **tag}, sort_keys=True) for tag in tags)
        if not isinstance(op.get("after_raw"), list):
            return {"early": {"ok": False, "status": "unverified",
                              "message": "This legacy operation has no complete tag after-image. "
                                         "Review the current tags manually; evidence retained."}}
        if canonical(raw_tags) != canonical(op["after_raw"]):
            return {"early": {"ok": False, "status": "outcome_unknown" if op.get("state") == "outcome_unknown" else "changed",
                              "message": "Current tag names or metadata differ from the recorded write. "
                                         "Evidence retained; nothing was written."}}
        cur_tags = zotero_write.tag_names(raw_tags)
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "error",
                          "message": "Zotero is not reachable."}}
    except zotero_write.ZoteroError as exc:
        return {"early": {"ok": False, "status": "error", "message": str(exc)}}

    if sorted(cur_tags) != sorted(op.get("after") or []):
        return {"early": {"ok": True, "status": "skipped",
                          "message": "Item changed in Zotero after the write — not reverting."}}
    # Prefer the raw dicts so pre-existing tags get their metadata back;
    # pre-2026-07-18 ops only stored names (the raw shape didn't exist yet).
    restore_tags = op.get("before_raw") or op.get("before") or []
    return {"commit": {
        "base": base, "lib_path": lib_path, "item_key": item_key,
        "api_key": api_key, "cur_version": cur_version, "restore_tags": restore_tags,
    }}


def _revert_zotero_commit_locked(prepared, op):
    """Write side of a ``zotero_tag`` revert — the caller holds the op-lock.

    Only the single PATCH runs here; the read + backoff already happened in
    ``_revert_zotero_prepare``. Kept minimal so the lock hold is one network
    round-trip, never a sleep.
    """
    from core.utils import log_vault_write

    c = prepared["commit"]
    try:
        zotero_write.set_item_tags(
            c["base"], c["lib_path"], c["item_key"], c["restore_tags"],
            c["cur_version"], api_key=c["api_key"])
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroVersionConflict,
            zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "error", "message": str(exc)}
    log_vault_write("tagging_restore_zotero", c["item_key"],
                    f"{len(op.get('after') or [])}→{len(op.get('before') or [])} tags")
    op["state"] = "reverted"
    return {"ok": True, "status": "reverted", "message": "Zotero tags reverted."}


def _revert_zotero_tag(vault_root, cfg, op):
    """Revert a ``zotero_tag`` op end-to-end (prepare + commit).

    Retained for callers/tests that hold no lock; the route now calls the split
    ``_revert_zotero_prepare`` / ``_revert_zotero_commit_locked`` directly so the
    GET/backoff runs before the op-lock is taken (D4). Both paths are identical
    in outcome.
    """
    prepared = _revert_zotero_prepare(cfg, op)
    if "early" in prepared:
        return prepared["early"]
    return _revert_zotero_commit_locked(prepared, op)


# --- tag REMOVAL (2026-07-22) ----------------------------------------------
#
# The deletion twins of the union writers above. Two deliberate differences:
#
#   1. Validation gate: removal validates against the target's CURRENT tags,
#      never the ``_tags.md`` vocabulary — the tag most worth deleting is
#      often exactly the one that fell out of the vocabulary, and the vocab
#      intersection would refuse it (the same reasoning that has
#      apply-tag-drift validate against the record's drift set). The security
#      property still holds: a removal can only ever DELETE something already
#      present, never inject content.
#   2. Op kinds ``note_untag`` / ``finder_untag`` / ``zotero_untag`` store the
#      SAME before/after state shapes as their add twins, so every existing
#      conservative revert (note snapshot restore, ``_revert_finder_tag``,
#      ``_revert_zotero_prepare``+commit) reverts them UNCHANGED — restoring
#      ``before`` re-adds the deleted tag. Only journal.revert_op's kind
#      dispatch and prune's applied-op carve-out learned the new names.

def _canonicalise_removals(requested):
    """Clean + dedupe removal targets — NO vocab intersection (see above)."""
    out: list[str] = []
    seen: set[str] = set()
    for t in requested:
        c = str(t or "").strip().lstrip("#").strip()
        if c and _tag_key(c) not in seen:
            out.append(c)
            seen.add(_tag_key(c))
    return out

def resolve_biblio_pdf_rel(pdf_rel, settings):
    """Resolve a CLIENT-SUPPLIED vault-relative PDF path under ``biblio_articles/``.

    Returns ``(abs_path_str, canonical_rel, None)`` or ``(None, None, (body, status))``.

    *pdf_rel* is relative to the **biblio dir**, not the vault root — it is
    joined onto ``settings.biblio_articles_dir``. (The docs said vault-relative
    until 2026-08-12, so a caller following them sent
    ``Z_attachments/biblio_articles/x.pdf`` and got a 400 for a file that
    exists. Note the journalled op records a VAULT-relative ``pdf_rel``, by
    design: Restore displays it beside note paths.)

    This is the one place in the tagging subsystem where a filesystem path comes
    from the client rather than from the cached scan, so it carries the full
    discipline of ``/api/tagging/pdf``'s ``_resolve_biblio_pdf``, in the same
    order: ``resolve_under_root`` (rejects traversal, NUL and escaping inputs
    and enforces the ``.pdf`` extension — note an ABSOLUTE input is not rejected
    as such, it is containment-checked like any other) and then an
    **independent realpath re-check** under the resolved biblio dir — the second
    pass is what catches a symlink planted inside the tree between the calls.

    It exists because a biblio PDF that the bridge never mapped to a citation
    key has no ``Record``, so the key-addressed endpoints cannot name it at all;
    its Finder tags are unreachable and (see ``audit/engine/reports/
    tag_hygiene.py``) invisible to the hygiene report. Containment is the
    **biblio dir**, deliberately narrower than ``/api/audit/pdf``'s vault-root
    containment: nothing outside ``biblio_articles/`` is in this feature's
    scope, and a write is a stronger reason to be narrow than a read.
    """
    if settings is None:
        return None, None, ({"error": "No scan results. Run an audit scan first."}, 404)
    biblio_dir = settings.biblio_articles_dir
    rel = resolve_under_root(pdf_rel, biblio_dir, must_exist=True,
                             must_be_file=True, exts=(".pdf",))
    if rel is None:
        return None, None, ({"error": "pdf_rel must name an existing PDF under biblio_articles/."}, 400)
    try:
        real = Path(os.path.realpath(biblio_dir / rel))
        real_dir = Path(os.path.realpath(biblio_dir))
    except (OSError, ValueError):
        return None, None, ({"error": "PDF path could not be resolved."}, 404)
    if (not _is_under(real, real_dir) or not real.is_file()
            or real.suffix.lower() != ".pdf"):
        return None, None, ({"error": "PDF not found."}, 404)
    return str(real), rel, None


def _prepare_tag_removal(data):
    """Shared validation for the tag-removal endpoints.

    Mirrors :func:`_prepare_tag_write` minus the vocabulary gate: returns
    ``(ctx, None)`` or ``(None, (response_dict, status))``. ``ctx.valid`` holds
    the cleaned removal targets; per-target presence ("is that tag actually on
    this note/PDF/item?") is each writer's own check so the fan-out can skip.

    **Two address forms** (exactly one required):

    * ``citation_key`` — the historical form. All three targets reachable.
    * ``pdf_rel`` — a path relative to ``biblio_articles/`` (NOT to the vault
      root; see :func:`resolve_biblio_pdf_rel`), for a PDF the bridge never
      mapped to a key. ``rec`` is then ``None`` and only the
      **Finder** target is addressable: a note and a Zotero item are reached
      THROUGH the record, so with no record there is nothing to name. The route
      skips those two with a reason rather than failing the request, exactly as
      it does for an absent target on the keyed path.

    Supplying both is rejected rather than silently preferring one — they could
    disagree, and a removal that silently targeted a different PDF than the
    caller named is the kind of write this subsystem must never make.
    """
    if not isinstance(data, dict):
        return None, ({"error": "Invalid JSON"}, 400)
    if coerce_bool(data.get("confirm")) is not True:
        return None, ({"error": "Tag removal must be explicitly confirmed (confirm: true)."}, 400)
    citation_key = coerce_non_empty_string(data.get("citation_key"), max_len=128)
    pdf_rel = coerce_non_empty_string(data.get("pdf_rel"), max_len=1024)
    if citation_key and pdf_rel:
        return None, ({"error": "Supply citation_key or pdf_rel, not both."}, 400)
    if not citation_key and not pdf_rel:
        return None, ({"error": "citation_key or pdf_rel required"}, 400)
    raw_tags = data.get("tags")
    if not isinstance(raw_tags, list) or not raw_tags:
        return None, ({"error": "tags must be a non-empty list"}, 400)
    requested = [coerce_string_max_len(t, 128) or "" for t in raw_tags[:200]]
    valid = _canonicalise_removals(requested)
    if not valid:
        return None, ({"error": "No valid tags to remove."}, 400)

    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        return None, ({"error": "No scan results. Run an audit scan first."}, 404)

    rec = None
    pdf_paths = None
    if citation_key:
        rec = inv.records.get(citation_key)
        if rec is None:
            return None, ({"error": "Unknown citation key."}, 404)
    else:
        abs_path, canonical_rel, err = resolve_biblio_pdf_rel(pdf_rel, settings)
        if err is not None:
            return None, err
        pdf_paths = [Path(abs_path)]
        pdf_rel = canonical_rel

    cfg = load_config()
    return {
        "rec": rec, "settings": settings, "cfg": cfg,
        "vault_root": settings.vault_root,
        "biblio_dir": settings.biblio_articles_dir, "valid": valid,
        "rejected": [], "citation_key": citation_key,
        "pdf_rel": pdf_rel if not citation_key else None,
        "pdf_paths": pdf_paths,
    }, None

def _note_remove_locked(*, rec, valid, want_sha, cfg, vault_root, citation_key,
                        manifest=None, batch_id=None, defer_save=False):
    """Remove ``valid`` tags from a record's note frontmatter. Caller holds the lock.

    The mirror of :func:`_note_write_locked`: same stale-diff guard, same
    snapshot-before-write, op kind ``note_untag`` (reverted by the same
    content-agnostic snapshot restore).

    ``batch_id`` (retire-tag) is stamped into the op HERE, before this helper's
    own append+save — stamping it afterwards in the caller left a window where
    a cancel/crash persisted the op applied-but-unstamped, dropping it from the
    one-click batch restore (2026-07-23 audit).

    ``defer_save`` (batch callers only, requires a shared ``manifest``): skip
    this helper's own journal transactions — a retire across N records would
    otherwise pay ~1-2 FULL-synchronous commits per record under the op-lock.
    The per-op note SNAPSHOT stays durable before the note write regardless
    (it is its own file); only the op-record persistence is batched, the
    refactor/apply.py ``JOURNAL_FLUSH_EVERY`` precedent. The caller MUST
    checkpoint + final-save the manifest itself.
    """
    from api.routes._audit_tagwrite import remove_tags_from_frontmatter
    from core.utils import log_vault_write, write_text_atomic

    note_path = rec.obsidian_note.path
    try:
        rel = str(note_path.resolve().relative_to(vault_root.resolve()))
    except (OSError, ValueError):
        return {"error": "Note path escapes the vault root."}, 400
    try:
        raw_bytes = note_path.read_bytes()
        original = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": f"Cannot read note: {sanitise_error_msg(exc)}"}, 400

    cur_sha = hashlib.sha256(raw_bytes).hexdigest()
    if want_sha and want_sha != cur_sha:
        return {
            "error": "Note changed on disk since it was loaded; reload and retry.",
            "expected_sha256": cur_sha,
        }, 409

    new_text, removed, reason = remove_tags_from_frontmatter(original, valid)
    if new_text is None:
        return {"error": f"Cannot safely edit frontmatter: {reason}"}, 422
    if not removed:
        return {
            "ok": True, "removed": [], "note": rel, "content_sha256": cur_sha,
            "message": "None of those tags are on the note.",
        }, 200

    new_bytes = new_text.encode("utf-8")
    hash_after = hashlib.sha256(new_bytes).hexdigest()

    shared = manifest is not None
    try:
        if not shared:
            manifest = journal.load(vault_root, cfg)
        op_id = journal.new_op_id(manifest)
        snapshot_rel = journal.write_note_snapshot(vault_root, cfg, rel, op_id, raw_bytes)
    except journal.ScopeError as exc:
        return {"error": f"Tagging archive misconfigured: {exc}"}, 400
    op = {
        "op_id": op_id, "kind": "note_untag", "note_rel": rel,
        "citation_key": citation_key, "removed": removed,
        "hash_before": cur_sha, "hash_after": hash_after,
        "snapshot_rel": snapshot_rel, "ts": journal.now_iso(), "state": "applied",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    if not shared:
        # protect_op_ids: never evict the op THIS call just appended —
        # with a manifest full of un-evictable applied finder/zotero ops
        # the cap loop would drop it (and unlink its snapshot) before the
        # target write, 404-ing the undo the response hands back.
        journal.prune(vault_root, cfg, manifest,
                      protect_op_ids={op["op_id"]})
    if not (shared and defer_save):
        journal.save(vault_root, cfg, manifest)

    log_vault_write("tagging_note_tag_remove", str(note_path),
                    f"-{len(removed)} tags: {', '.join(removed)}")
    try:
        write_text_atomic(str(note_path), new_text)
    except OSError as exc:
        op["state"] = "failed"
        if not (shared and defer_save):
            journal.save(vault_root, cfg, manifest)
        return {"error": f"Write failed: {sanitise_error_msg(exc)}"}, 500

    return {
        "ok": True, "removed": removed, "note": rel,
        "citation_key": citation_key, "op_id": op_id,
        "content_sha256": hash_after,
    }, 200

def _finder_remove_locked(*, rec, valid, cfg, vault_root, biblio_dir, citation_key,
                          manifest=None, batch_id=None, defer_save=False,
                          pdf_paths=None):
    """Remove ``valid`` from each biblio PDF's Finder tags. Caller holds the lock.

    ``pdf_paths`` (path-addressed removal) supplies the PDFs explicitly instead
    of deriving them from ``rec.pdf_paths``; ``rec`` is then ``None``. The paths
    must already be validated — ``resolve_biblio_pdf_rel`` is the only sanctioned
    producer — and every one is still re-checked under ``biblio_dir`` inside the
    loop below, so this parameter widens *which* PDFs can be named, never the
    containment they must satisfy. The journalled op is byte-identical in shape
    (``pdfs`` entries carrying ``abs``/``before_raw``/``after_raw``), which is
    what lets ``_revert_finder_tag`` handle it with no change at all: the revert
    never reads ``citation_key``.

    Survivor entries ride RAW (colour suffixes preserved); the removed entries'
    raw forms live in the op's ``before_raw`` so Restore re-adds them with
    their colours. Op kind ``finder_untag`` — reverted by the unchanged
    :func:`_revert_finder_tag` (same ``pdfs`` change shape). ``batch_id`` is
    stamped pre-save and ``defer_save`` batches the manifest I/O, both for the
    same reasons as :func:`_note_remove_locked` (the op's before-lists are
    in-memory until the caller's next checkpoint — acceptable for the same
    25-op window the note ops accept).
    """
    from api.routes import _finder_tagwrite
    from core.utils import log_vault_write

    targets = {_tag_key(t) for t in valid}
    # Resolve the containment root once: `biblio_articles_dir` is `vault_root /
    # subdirs`, and only the vault root half is `.resolve()`d, so a symlinked
    # `biblio_articles/` (or `Z_attachments/`) leaves this textual path pointing
    # at the link while `resolve_biblio_pdf_rel` already handed us a REALPATH.
    # Comparing the two filtered every valid path-addressed removal out and
    # 400'd it. The per-PDF loop below always resolved both sides; this makes
    # the pre-filter agree with it.
    real_biblio_dir = biblio_dir.resolve()
    if pdf_paths is not None:
        biblio_pdfs = [Path(p) for p in pdf_paths
                       if _is_under(Path(p), real_biblio_dir)]
        if not biblio_pdfs:
            return {"error": "PDF is not under biblio_articles/."}, 400
    else:
        biblio_pdfs = [Path(p) for p in rec.pdf_paths if _is_under(Path(p), biblio_dir)]
        if not biblio_pdfs:
            return {"error": "No biblio_articles PDF for that citation key."}, 404

    changes: list[dict] = []
    read_errors: list[dict] = []
    for pdf in biblio_pdfs:
        try:
            pdf_rel = str(pdf.resolve().relative_to(vault_root.resolve()))
        except (OSError, ValueError):
            continue
        if not _is_under(pdf.resolve(), biblio_dir.resolve()):
            continue
        try:
            before_raw = _finder_tagwrite.read_finder_tags_full(str(pdf))
        except RuntimeError as exc:
            read_errors.append({"rel": pdf_rel, "error": sanitise_error_msg(exc)})
            continue
        before = [_finder_tagwrite.tag_name(v) for v in before_raw]
        after_raw = [v for v in before_raw
                     if _tag_key(_finder_tagwrite.tag_name(v)) not in targets]
        if len(after_raw) == len(before_raw):
            continue  # nothing to remove on this PDF
        after = [_finder_tagwrite.tag_name(v) for v in after_raw]
        removed = [t for t in before if _tag_key(t) in targets]
        changes.append({"rel": pdf_rel, "abs": str(pdf),
                        "before": before, "after": after,
                        "before_raw": before_raw, "after_raw": after_raw,
                        "removed": removed})

    if read_errors and not changes:
        return {"error": "Could not read the PDF's current Finder tags.",
                "pdfs": read_errors}, 500
    if not changes:
        return {
            "ok": True, "removed": [], "citation_key": citation_key, "pdfs": [],
            "message": "No PDF carries those tags.",
        }, 200

    shared = manifest is not None
    try:
        if not shared:
            manifest = journal.load(vault_root, cfg)
        op_id = journal.new_op_id(manifest)
    except journal.ScopeError as exc:
        return {"error": f"Tagging archive misconfigured: {exc}"}, 400
    op = {
        "op_id": op_id, "kind": "finder_untag", "citation_key": citation_key,
        "pdfs": changes, "ts": journal.now_iso(), "state": "applied",
        "removed": sorted({t for ch in changes for t in ch["removed"]}),
    }
    if citation_key is None and changes:
        # Path-addressed: without this the Restore UI renders the op with no
        # identity at all (it projects citation_key, which is None here).
        op["pdf_rel"] = changes[0]["rel"]
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    if not shared:
        # protect_op_ids: never evict the op THIS call just appended —
        # with a manifest full of un-evictable applied finder/zotero ops
        # the cap loop would drop it (and unlink its snapshot) before the
        # target write, 404-ing the undo the response hands back.
        journal.prune(vault_root, cfg, manifest,
                      protect_op_ids={op["op_id"]})
    if not (shared and defer_save):
        journal.save(vault_root, cfg, manifest)

    results = [dict(e) for e in read_errors]
    any_ok = False
    ok_removed: set[str] = set()
    failed_writes = 0
    for ch in changes:
        try:
            _finder_tagwrite.write_finder_tags(ch["abs"], ch["after_raw"])
            log_vault_write("tagging_finder_tag_remove", ch["abs"],
                            f"-{len(ch['removed'])} tags: {', '.join(ch['removed'])}")
            results.append({"rel": ch["rel"], "removed": ch["removed"]})
            ok_removed.update(ch["removed"])
            any_ok = True
        except Exception as exc:  # noqa: BLE001
            failed_writes += 1
            results.append({"rel": ch["rel"], "error": sanitise_error_msg(exc)})
    if not any_ok:
        op["state"] = "failed"
        if not (shared and defer_save):
            journal.save(vault_root, cfg, manifest)
        return {"error": "All Finder-tag removals failed.", "pdfs": results}, 500

    partial = failed_writes > 0 or bool(read_errors)
    if sorted(ok_removed) != op["removed"]:
        op["removed"] = sorted(ok_removed)
        if not (shared and defer_save):
            journal.save(vault_root, cfg, manifest)
    result = {
        "ok": True, "removed": op["removed"], "citation_key": citation_key,
        "pdfs": results, "op_id": op_id,
    }
    if partial:
        result["partial"] = True
        result["failed"] = [e for e in results if e.get("error")]
    return result, 200

def _zotero_remove_prepare(*, rec, valid, cfg, citation_key):
    """READ phase of a Zotero tag removal — no lock held (mirror of
    :func:`_zotero_prepare`; the version-guarded PATCH is what makes it safe)."""
    targets = {_tag_key(t) for t in valid}
    zot = rec.zotero_item
    transport = _zotero_transport(cfg)
    if not transport.can_write:
        return None, {"error": (
            "The Zotero web API is selected but no API key is configured. "
            f"Set {zotero_write.API_KEY_ENV} in your .env, or switch "
            "tagging_zotero_transport to 'local'.")}, 503
    base = transport.base_url
    item_key = zot.item_key
    try:
        identity = _zotero_read_identity(transport, zot)
        lib_path = _zotero_lib_path(transport, zot)
        version, before_raw = zotero_write.get_item_tags_full(
            base, lib_path, item_key, api_key=transport.api_key,
            **identity)
    except zotero_write.ZoteroUnavailable as exc:
        return None, {"error": f"Zotero is not reachable: {sanitise_error_msg(exc)}"}, 503
    except zotero_write.ZoteroError as exc:
        return None, {"error": sanitise_error_msg(exc)}, 502

    before = zotero_write.tag_names(before_raw)
    after_payload = [d for d in before_raw
                     if _tag_key(str(d.get("tag", "") if isinstance(d, dict) else d))
                     not in targets]
    removed = [t for t in before if _tag_key(t) in targets]
    if not removed:
        return None, {
            "ok": True, "removed": [], "citation_key": citation_key,
            "message": "Item does not carry those tags.",
        }, 200
    after = [t for t in before if _tag_key(t) not in targets]
    return {
        "base": base, "lib_path": lib_path, "item_key": item_key,
        "api_key": transport.api_key, "transport": transport.kind,
        "version": version, "before_raw": before_raw, "before": before,
        "removed": removed, "after": after, "after_payload": after_payload,
    }, None, None

def _zotero_remove_journal(*, prepared, citation_key, manifest, batch_id=None):
    """Append (do NOT save) the ``zotero_untag`` op for *prepared*.

    The journal/patch split exists so a batch caller (retire-tag) can journal
    every Zotero removal under ONE brief op-lock hold, persist the manifest,
    RELEASE the lock, and run the network PATCHes lock-free: a PATCH touches
    neither the vault nor the manifest, and the If-Unmodified-Since-Version
    guard — not the op-lock — is what prevents lost updates on the Zotero
    side (the same argument that already justifies the pre-lock GETs).
    Holding the lock across N sequential PATCHes froze every other vault
    write for the whole batch (2026-07-23 audit). Journalling BEFORE the
    PATCH keeps the crash trail conservative: an intent starts unknown, and
    recovery verifies the exact after-array before any inverse write.
    """
    op = {
        "op_id": journal.new_op_id(manifest), "kind": "zotero_untag",
        "citation_key": citation_key,
        "item_key": prepared["item_key"], "lib_path": prepared["lib_path"],
        "base_url": prepared["base"],
        "before": prepared["before"], "before_raw": prepared["before_raw"],
        "after": prepared["after"], "removed": prepared["removed"],
        "transport": prepared.get("transport") or "local",
        "version_before": prepared["version"], "server_id": getattr(prepared["version"], "server_id", None), "ts": journal.now_iso(),
        "after_raw": [t if isinstance(t, dict) else {"tag": t} for t in prepared["after_payload"]],
        "state": "outcome_unknown",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    return op


def _zotero_remove_patch(prepared):
    """Tag removal uses the same PATCH and uncertainty contract as addition."""
    return _zotero_patch(prepared)


def _zotero_remove_commit_locked(*, prepared, cfg, vault_root, citation_key,
                                 manifest=None, batch_id=None):
    """WRITE phase of a Zotero tag removal — caller holds the lock.

    Op kind ``zotero_untag`` with the SAME before/after fields as the add op,
    so the existing split revert restores the removed tags (metadata included,
    via ``before_raw``). ``batch_id`` is stamped pre-save for the same
    cancel-window reason as :func:`_note_remove_locked`. Composed from
    :func:`_zotero_remove_journal` + :func:`_zotero_remove_patch`; this
    single-item path keeps the historical journal→save→PATCH→save order and
    result shapes (remove-tags / apply-all use it unchanged).
    """
    from core.utils import log_vault_write

    shared = manifest is not None
    try:
        if not shared:
            manifest = journal.load(vault_root, cfg)
    except journal.ScopeError as exc:
        return {"error": f"Tagging archive misconfigured: {exc}"}, 400
    op = _zotero_remove_journal(prepared=prepared, citation_key=citation_key,
                                manifest=manifest, batch_id=batch_id)
    if not shared:
        # protect_op_ids: never evict the op THIS call just appended —
        # with a manifest full of un-evictable applied finder/zotero ops
        # the cap loop would drop it (and unlink its snapshot) before the
        # target write, 404-ing the undo the response hands back.
        journal.prune(vault_root, cfg, manifest,
                      protect_op_ids={op["op_id"]})
    journal.save(vault_root, cfg, manifest)

    result, status, new_version = _zotero_remove_patch(prepared)
    if status != 200:
        op["state"] = "outcome_unknown" if result.get("outcome_unknown") else "failed"
        journal.save(vault_root, cfg, manifest)
        return result, status
    op["state"] = "applied"
    op["version_after"] = new_version
    journal.save(vault_root, cfg, manifest)
    log_vault_write("tagging_zotero_tag_remove", prepared["item_key"],
                    f"-{len(prepared['removed'])} tags: {', '.join(prepared['removed'])}")

    return {
        "ok": True, "removed": prepared["removed"], "citation_key": citation_key,
        "item_key": prepared["item_key"], "op_id": op["op_id"],
        "transport": prepared.get("transport") or "local",
        "sync_pending": (prepared.get("transport") == "web"),
    }, 200


# --- Zotero attachment de-link (2026-08-28) --------------------------------
#
# The third external Zotero write, and the first that touches an ITEM rather
# than an item's tags. Deliberately narrow:
#
#   * It TRASHES, never deletes (see zotero_write.set_item_deleted's comment).
#     So the inverse is exact — the same verb with deleted=False — and the file
#     of an `imported_*` attachment stays on disk until the user empties
#     Zotero's trash.
#   * It never touches a file. "De-link without deleting the PDF" is satisfied
#     for a `linked_*` attachment by construction (Zotero only ever held a
#     path), and for an `imported_*` one by trashing rather than deleting.
#   * It is journalled like the tag writers, so the existing Restore panel
#     reverts it with no new UI.

def zotero_attachment_prepare(cfg, item_key, row):
    """Read an attachment before trashing it — runs BEFORE the op-lock.

    Same prepare/commit split as the tag writers, for the same reason (D4): the
    GET can sleep on Zotero's ``Backoff`` header, and sleeping under the
    obsidian op-lock stalls indexing. The PATCH's ``If-Unmodified-Since-Version``
    guard is what makes reading early safe.

    *row* is an inventory ``ZoteroAttachmentRow`` — it only has to carry
    ``library_type``/``group_id``. Library addressing is resolved HERE through
    the same ``_zotero_lib_path`` the tag writers use, not by the caller: on the
    web transport the personal library needs the real numeric userID, and a
    caller that built ``users/0`` by hand would 404 against api.zotero.org.

    Returns ``{"early": {...}}`` (a finished per-target result) or
    ``{"commit": {...}}`` (everything the locked half needs).
    """
    base = _zotero_base(cfg)
    transport = _zotero_transport(cfg)
    api_key = transport.api_key if transport.is_web else None
    if transport.is_web and not api_key:
        return {"early": {"ok": False, "status": "error",
                          "message": (f"Set {zotero_write.API_KEY_ENV} to write to "
                                      "the Zotero web API.")}}
    try:
        lib_path = _zotero_lib_path(transport, row)
        data, version = zotero_write.get_item(
            transport.base_url, lib_path, item_key, api_key=api_key)
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "unreachable",
                          "message": "Zotero is not reachable."}}
    except zotero_write.ZoteroError as exc:
        return {"early": {"ok": False, "status": "error", "message": str(exc)}}

    if data.get("itemType") != "attachment":
        # Refuse rather than trash: this verb exists to remove FILES from an
        # item, and pointing it at a parent would bin the bibliographic record.
        return {"early": {"ok": False, "status": "error",
                          "message": "That Zotero item is not an attachment."}}
    if data.get("deleted"):
        return {"early": {"ok": True, "status": "skipped",
                          "message": "Already in Zotero's trash."}}
    return {"commit": {
        "base": base, "transport": transport, "api_key": api_key,
        "lib_path": lib_path, "item_key": item_key, "version": version,
        "link_mode": data.get("linkMode"),
        "parent_item": data.get("parentItem"),
        "filename": data.get("filename") or data.get("path") or "",
        "title": data.get("title") or "",
    }}


def zotero_attachment_journal(*, prepared, manifest, batch_id=None):
    """Append (do NOT save) the ``zotero_attachment_trash`` op. Returns the op.

    The journal/patch split exists so a batch caller journals every attachment
    under ONE brief op-lock hold, persists the manifest, RELEASES the lock, and
    runs the network PATCHes lock-free. A PATCH touches neither the vault nor
    the manifest, and the ``If-Unmodified-Since-Version`` guard — not the
    op-lock — is what prevents a lost update, which is the same argument that
    already justifies the pre-lock GETs in
    :func:`zotero_attachment_prepare`. Holding the lock across N sequential
    PATCHes freezes indexing and every other vault write for the whole batch
    (2026-07-23 audit; re-introduced by the de-link route and closed here).

    Journalling BEFORE the PATCH keeps the crash trail conservative: an op
    whose PATCH never ran reverts by restoring an item that was never trashed,
    which is idempotent.
    """
    c = prepared["commit"]
    op = {
        # `op_id`, not `id`: the tagging journal indexes, dedupes and tombstones
        # ops by that key (refactor/journal.py is the one that uses `id`). An op
        # appended under the wrong key is invisible to Restore and to prune.
        "op_id": journal.new_op_id(manifest),
        "kind": "zotero_attachment_trash",
        "ts": journal.now_iso(),
        "item_key": c["item_key"],
        "parent_item": c["parent_item"],
        "lib_path": c["lib_path"],
        "base_url": c["transport"].base_url,
        "transport": c["transport"].kind,
        "link_mode": c["link_mode"],
        "filename": c["filename"],
        "title": c["title"],
        "version_before": c["version"], "server_id": getattr(c["version"], "server_id", None),
        "state": "applied",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    return op


def zotero_attachment_patch(prepared):
    """The lock-free network half of an attachment trash.

    Returns ``(result_dict, status, new_version | None)``. Callers record the
    outcome on the journalled op themselves, under whatever lock they hold.
    """
    from core.utils import log_vault_write

    c = prepared["commit"]
    try:
        new_version = zotero_write.set_item_deleted(
            c["transport"].base_url, c["lib_path"], c["item_key"], True,
            c["version"], api_key=c["api_key"])
    except zotero_write.ZoteroVersionConflict as exc:
        return {"ok": False, "status": "conflict", "message": str(exc)}, 409, None
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "error", "message": str(exc)}, 502, None
    log_vault_write("tagging_zotero_attachment_trash", c["item_key"],
                    f"{c['link_mode']} {c['filename']}")
    return ({"ok": True, "status": "trashed", "item_key": c["item_key"],
             "link_mode": c["link_mode"],
             "message": "Moved to Zotero's trash."}, 200, new_version)


def zotero_attachment_commit_locked(prepared, manifest, *, batch_id=None,
                                    defer_save=False, vault_root=None, cfg=None):
    """Journal + PATCH one attachment in one call — the single-item path.

    Composed from :func:`zotero_attachment_journal` and
    :func:`zotero_attachment_patch` so there is one definition of each half.
    A BATCH caller must not use this: it PATCHes while the caller holds the
    op-lock. Use the two halves and the four-phase choreography instead (see
    ``api/routes/zotlink.py``).
    """
    if not defer_save and (vault_root is None or cfg is None):
        # Silently skipping the save when a caller ASKED for one would leave a
        # PATCHed attachment with no journal record — the one failure this
        # writer's ordering exists to prevent. Fail loudly instead.
        raise ValueError("defer_save=False requires vault_root and cfg")

    op = zotero_attachment_journal(prepared=prepared, manifest=manifest,
                                   batch_id=batch_id)
    if not defer_save and vault_root is not None and cfg is not None:
        journal.save(vault_root, cfg, manifest)

    result, status, new_version = zotero_attachment_patch(prepared)
    if status != 200:
        op["state"] = "failed"
        return {**result, "op_id": op["op_id"]}
    op["version_after"] = new_version
    return {**result, "op_id": op["op_id"]}


# --- linked-file attachment CREATE (harmonization Phase 4, 2026-09-06) ------
#
# The mirror of the de-link: a `linked_file` attachment is a POST carrying a
# path and no bytes (spike S1 — Zotero stores nothing for it, `GET …/file`
# answers 404, it syncs down within seconds). Personal library only: linked
# files cannot be used in a group library (Zotero docs), and the EFPT group
# answered 403 on its file-editing policy in spike S2 — so the gate is on
# `library_type`, never on the status code. The undo is a trash.

OP_ATTACHMENT_CREATE = "zotero_attachment_create"


def zotero_attachment_create_prepare(cfg, zot, pdf_path):
    """Read side — no lock. Resolves the transport + library path, refuses a
    group-library item BEFORE any network, then GETs the parent to confirm it
    is a live bibliographic item. ``{"early"}`` or ``{"commit"}``."""
    if getattr(zot, "library_type", None) == "group":
        return {"early": {"ok": False, "status": "skipped", "reason": "group_library",
                          "message": "Linked files cannot be used in a group library."}}
    transport = _zotero_transport(cfg)
    if not transport.can_write:
        return {"early": {"ok": False, "status": "error", "reason": "read_only_transport",
                          "message": (f"Set {zotero_write.API_KEY_ENV} to write to "
                                      "the Zotero web API.")}}
    api_key = transport.api_key if transport.is_web else None
    try:
        lib_path = _zotero_lib_path(transport, zot)
        data, _version = zotero_write.get_item(
            transport.base_url, lib_path, zot.item_key, api_key=api_key,
            **({"server_id": getattr(zot, "server_id", None) or ""} if not transport.is_web else {}))
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "unreachable",
                          "message": "Zotero is not reachable."}}
    except (zotero_write.ZoteroError, zotero_write.ZoteroVersionConflict) as exc:
        return {"early": {"ok": False, "status": "error", "message": str(exc)}}
    if data.get("itemType") in ("note", "attachment") or data.get("deleted"):
        return {"early": {"ok": False, "status": "error",
                          "message": "The parent is not a live bibliographic item."}}
    from audit.core.bib import normalise_doi
    if (data.get("itemType") != getattr(zot, "item_type", None)
            or (data.get("title") or "") != (getattr(zot, "title", None) or "")
            or normalise_doi(data.get("DOI") or "") != normalise_doi(getattr(zot, "doi", None) or "")):
        return {"early": {"ok": False, "status": "conflict",
                          "message": "Zotero parent identity changed since the scan. Review and scan again."}}
    if not transport.is_web and getattr(_version, "server_id", None) != getattr(zot, "server_id", None):
        return {"early": {"ok": False, "status": "conflict",
                          "message": "Zotero database changed since the scan. Review and scan again."}}
    return {"commit": {
        "transport": transport, "api_key": api_key, "lib_path": lib_path,
        "parent_key": zot.item_key, "parent_version": _version, "parent_data": data, "path": str(pdf_path),
        "title": Path(str(pdf_path)).name, "server_id": getattr(_version, "server_id", None),
    }}


def _validate_attachment_source(c):
    if "expected_identity" not in c:
        return
    from accession import review
    from audit.engine import bridge
    if not review.same_identity(review.identity(Path(c["path"])), c["expected_identity"]):
        raise ValueError("PDF changed before attachment. Review it again.")
    mapping = bridge._load_mapping_all(Path(c["mapping_file"]))
    keys = sorted(k for k, paths in mapping.matches.items() if c["review_rel"] in paths)
    if keys != [c["citation_key"]]:
        raise ValueError("Mapping changed before attachment. Reconcile again.")
    if c.get("review_token") is not None:
        record = review.validate(Path(c["vault_root"]), c["review_rel"], c["review_token"])
        from audit.core.bib import normalise_doi
        if normalise_doi((c.get("parent_data") or {}).get("DOI") or "") != record["chosen"]["doi"]:
            raise ValueError("Confirmed PDF identifier does not match the Zotero parent. Reconcile and scan again.")


def zotero_attachment_create_post(prepared):
    """The lock-free POST. ``(result, attachment_key | None, version | None)``.

    Readback verifies POST object evidence; it never invents a creation baseline."""
    from core.utils import log_vault_write

    c = prepared["commit"]
    if c.get("item_key"):
        try:
            data, version = zotero_write.get_item(
                c["transport"].base_url, c["lib_path"], c["item_key"], api_key=c["api_key"],
                **({"server_id": c["server_id"]} if c.get("server_id") else {}))
        except zotero_write.ZoteroNotFound:
            pass
        except (zotero_write.ZoteroError, zotero_write.ZoteroUnavailable, zotero_write.ZoteroVersionConflict) as exc:
            return ({"ok": False, "status": "verification_pending", "item_key": c["item_key"],
                     "message": "Could not verify the reserved attachment. No create was sent. " + str(exc)}, None, None)
        else:
            if (data.get("itemType") != "attachment" or data.get("linkMode") != "linked_file"
                    or data.get("parentItem") != c["parent_key"] or data.get("path") != c["path"]
                    or data.get("deleted")):
                return ({"ok": False, "status": "conflict", "item_key": c["item_key"],
                         "message": "Reserved attachment was changed or trashed. Review it in Zotero; no duplicate was created."}, None, None)
            _validate_attachment_source(c)
            # Current existence is not a historical creation baseline. An edit
            # may have happened between the lost POST response and this retry.
            # Existing journal entries retain their original baseline; a new
            # recovery record must require manual review before undo.
            return ({"ok": True, "status": "attached", "recovered": True, "item_key": c["item_key"],
                     "undo_review_required": True,
                     "message": "Existing attachment verified; recovering its record. A recovered record without a creation baseline requires manual review before undo."}, c["item_key"], None)
    _validate_attachment_source(c)
    try:
        if "parent_version" in c:
            parent, parent_version = zotero_write.get_item(
                c["transport"].base_url, c["lib_path"], c["parent_key"], api_key=c["api_key"],
                **({"server_id": c["server_id"]} if c.get("server_id") else {}))
            if parent_version != c["parent_version"] or parent != c["parent_data"]:
                raise zotero_write.ZoteroVersionConflict("Parent changed before attachment. Review and scan again.")
        _validate_attachment_source(c)
        if c.get("before_dispatch"):
            c["before_dispatch"]()
        att_key = zotero_write.create_linked_file_attachment(
            c["transport"].base_url, c["lib_path"], c["parent_key"], c["path"],
            c["title"], api_key=c["api_key"], **({"server_id": c["server_id"]} if c.get("server_id") else {}),
            **({"item_key": c["item_key"]} if c.get("item_key") else {}))
    except zotero_write.ZoteroVersionConflict as exc:
        return {"ok": False, "status": "conflict", "message": str(exc)}, None, None
    except zotero_write.ZoteroWriteUnsupported as exc:
        return {"ok": False, "status": "unsupported", "message": str(exc)}, None, None
    except zotero_write.ZoteroUnavailable:
        return {"ok": False, "status": "unreachable",
                "item_key": c.get("item_key"), "verification_pending": bool(c.get("item_key")),
                "message": "Zotero response unavailable. Retry this file to verify its reserved attachment before creating anything else."}, None, None
    except zotero_write.ZoteroError as exc:
        return {"ok": False, "status": "error", "message": str(exc)}, None, None
    if c.get("acknowledge"):
        c["acknowledge"](att_key)
    # A failed readback must not erase the key from a successful POST.
    try:
        _data, version = zotero_write.get_item(
            c["transport"].base_url, c["lib_path"], att_key, api_key=c["api_key"],
            **({"server_id": c["server_id"]} if c.get("server_id") else {}))
    except (zotero_write.ZoteroError, zotero_write.ZoteroUnavailable, zotero_write.ZoteroVersionConflict):
        log_vault_write("tagging_zotero_attachment_create", att_key,
                        "Created; version verification pending")
        return ({"ok": True, "status": "attached", "item_key": att_key,
                 "verification_pending": True,
                 "message": "Attachment created; readback failed. Do not create another copy. Automatic undo requires a verified creation baseline; review this record manually."}, att_key, None)
    log_vault_write("tagging_zotero_attachment_create", att_key,
                    f"linked_file under {c['parent_key']}: {c['path']}")
    baseline = getattr(att_key, "creation_version", None)
    if (baseline is None or version != baseline
            or _data != getattr(att_key, "creation_data", None)):
        return ({"ok": True, "status": "attached", "item_key": str(att_key),
                 "undo_review_required": True,
                 "message": "Attachment created, but its unchanged creation baseline could not be verified. Review it manually before undo."}, str(att_key), None)
    c["creation_data"] = _data
    return ({"ok": True, "status": "attached", "item_key": att_key,
             "message": "Linked in Zotero."}, att_key, version)


def zotero_attachment_create_journal(*, prepared, citation_key, att_key, version,
                                     manifest, batch_id=None):
    """Append/reuse a restore op only after creation or authoritative recovery.

    Exact-file intents reserve a key before POST; an intent alone is not proof
    of creation and must never become an applied restore record.
    """
    c = prepared["commit"]
    for existing in manifest["ops"]:
        if (existing.get("kind") == OP_ATTACHMENT_CREATE and existing.get("state") == "applied"
                and existing.get("item_key") == att_key and existing.get("lib_path") == c["lib_path"]
                and existing.get("base_url") == c["transport"].base_url
                and existing.get("server_id") == c.get("server_id")):
            return existing
    op = {
        "op_id": journal.new_op_id(manifest), "kind": OP_ATTACHMENT_CREATE,
        "ts": journal.now_iso(), "citation_key": citation_key,
        "item_key": att_key, "parent_item": c["parent_key"],
        "lib_path": c["lib_path"], "base_url": c["transport"].base_url,
        "transport": c["transport"].kind, "link_mode": "linked_file",
        "path": c["path"], "title": c["title"],
        "version_after": version, "server_id": c.get("server_id"), "state": "applied",
        "source_identity": c.get("expected_identity"),
        "creation_data": c.get("creation_data"),
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    return op


def _revert_zotero_attachment_create_prepare(cfg, op):
    """Read side of a create revert — OUTSIDE the op-lock. Conservative: an
    item already trashed is a skip, and one whose path changed since the
    write is a skip (someone re-pointed it; trashing would lose that)."""
    api_key = zotero_write.api_key_from_env() if op.get("transport") == "web" else None
    if op.get("transport") == "web" and not api_key:
        return {"early": {"ok": False, "status": "error", "message": (
            "This attachment was created via the Zotero web API; set "
            f"{zotero_write.API_KEY_ENV} to revert it.")}}
    base, lib_path, key = op.get("base_url"), op.get("lib_path") or "users/0", op.get("item_key")
    if not key or not base:
        return {"early": {"ok": False, "status": "invalid",
                          "message": "Op missing item_key."}}
    from tagging.local_api import restore_kwargs
    try:
        data, version = zotero_write.get_item(base, lib_path, key, api_key=api_key, **restore_kwargs(op))
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "error",
                          "message": "Zotero is not reachable."}}
    except zotero_write.ZoteroError as exc:
        return {"early": {"ok": False, "status": "error", "message": str(exc)}}
    if data.get("deleted"):
        return {"early": {"ok": True, "status": "skipped",
                          "message": "Already in Zotero's trash."}, "state": "reverted"}
    if data.get("itemType") != "attachment":
        return {"early": {"ok": False, "status": "error",
                          "message": "That Zotero item is not an attachment."}}
    if str(data.get("path") or "") != str(op.get("path") or ""):
        return {"early": {"ok": True, "status": "skipped",
                          "message": "Attachment re-pointed in Zotero after the write — not trashing."}}
    if op.get("source_identity"):
        from accession import review
        try:
            current_identity = review.identity(Path(op["path"]))
        except (OSError, ValueError):
            current_identity = None
        if not review.same_identity(current_identity, op["source_identity"]):
            return {"early": {"ok": False, "status": "conflict",
                              "message": "The linked PDF changed or is unavailable. Review it before undoing the attachment."}}
    # A path alone does not authorize trashing an attachment someone moved,
    # annotated or edited after creation. Legacy/incomplete records fail closed.
    if (not op.get("parent_item") or data.get("parentItem") != op["parent_item"]
            or data.get("linkMode") != "linked_file"
            or data.get("title") != op.get("title")
            or data.get("note") or data.get("tags") or data.get("relations")
            or not op.get("version_after") or version != op["version_after"]
            or not op.get("creation_data") or data != op["creation_data"]):
        return {"early": {"ok": False, "status": "conflict",
                          "message": "Attachment changed or its creation version is unverified. Review it in Zotero; not trashing."}}
    try:
        children = zotero_write.get_item_children(base, lib_path, key, api_key=api_key, **restore_kwargs(op))
    except (zotero_write.ZoteroError, zotero_write.ZoteroUnavailable) as exc:
        return {"early": {"ok": False, "status": "error", "message": str(exc)}}
    if children:
        return {"early": {"ok": False, "status": "conflict",
                          "message": "Attachment has annotations or child items. Review them in Zotero; not trashing."}}
    return {"commit": {"base": base, "lib_path": lib_path, "key": key,
                       "api_key": api_key, "version": version}}


def _revert_zotero_attachment_create_commit(prepared, op):
    """Write side: ONE guarded PATCH ``deleted=1`` — a trash, never a DELETE."""
    from core.utils import log_vault_write

    c = prepared["commit"]
    from tagging.local_api import origin
    if origin(c['base']):
        # Verified on Desktop 10.0.1: adding an annotation does not increment
        # the attachment clientVersion. HTTP version guards cannot protect the
        # interval after the child read. Only native atomic check-and-trash can.
        return {"ok": False, "status": "unsupported",
                "message": "Automatic Desktop attachment undo requires an atomic annotation check. Review the attachment in Zotero; no trash request was sent."}
    try:
        zotero_write.set_item_deleted(c["base"], c["lib_path"], c["key"], True,
                                      c["version"], api_key=c["api_key"])
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroVersionConflict,
            zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "error", "message": str(exc)}
    log_vault_write("tagging_restore_zotero_attachment_create", c["key"],
                    "created linked file trashed")
    op["state"] = "reverted"
    return {"ok": True, "status": "reverted",
            "message": "Linked file moved to Zotero's trash."}


def _revert_zotero_attachment_create(vault_root, cfg, op):
    """Composed prepare + commit for the single-op restore route."""
    prepared = _revert_zotero_attachment_create_prepare(cfg, op)
    if "early" in prepared:
        if prepared.get("state"):
            op["state"] = prepared["state"]
        return prepared["early"]
    return _revert_zotero_attachment_create_commit(prepared, op)


# --- PARENT ITEM create (2026-09-11) ---------------------------------------
#
# The forward verb beyond attach: an orphan vault PDF becomes a NEW Zotero
# item, with the file linked under it. Two unkeyed POSTs, so two
# lost-response windows — the barrier is `zotcreate.attempts`, reserved
# before the first of them, never a key we chose (Zotero Desktop refuses a
# keyed create; see `zotero_write.create_linked_file_attachment`).
#
# Undo is a trash of the parent, never a DELETE — the note-create argument.

OP_ITEM_CREATE = "zotero_item_create"


class NewParent:
    """The minimum an attachment writer needs to describe a just-made parent.

    `zotero_attachment_create_prepare` takes the scan's `ZoteroItem` and
    re-verifies the parent's identity against it before linking. A freshly
    created item has no scan record, so this stands in for one — carrying
    exactly the values we sent plus the `server_id` the readback reported,
    which is what lets that GATE run unchanged rather than being bypassed
    for the one case where the parent is newest and least verified.
    """

    library_type = "user"
    group_id = None

    def __init__(self, item_key, item_type, title, doi=None, server_id=None):
        self.item_key = item_key
        self.item_type = item_type
        self.title = title
        self.doi = doi
        self.server_id = server_id


def zotero_item_create_prepare(cfg, *, item_type, fields, creators):
    """Read side — no lock. Resolves the transport and the PERSONAL library.

    Personal, always: a linked file cannot be used in a group library (spike
    S2, live 403), so a create that could target a group would mint an item
    its own attachment could never join. `{"early"}` or `{"commit"}`.
    """
    transport = _zotero_transport(cfg)
    if not transport.can_write:
        return {"early": {"ok": False, "status": "error", "reason": "read_only_transport",
                          "message": ("Authorize Zotero 10 Desktop, or set "
                                      f"{zotero_write.API_KEY_ENV} for the web transport, "
                                      "before creating items.")}}
    api_key = transport.api_key if transport.is_web else None
    try:
        # `None` has neither library_type nor group_id, so this resolves the
        # personal library on both transports (`users/0` / `users/<uid>`).
        lib_path = _zotero_lib_path(transport, None)
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "unreachable",
                          "message": "Zotero is not reachable."}}
    except zotero_write.ZoteroError as exc:
        return {"early": {"ok": False, "status": "error", "message": str(exc)}}
    return {"commit": {"transport": transport, "api_key": api_key,
                       "lib_path": lib_path, "item_type": item_type,
                       "fields": dict(fields or {}), "creators": list(creators or [])}}


#: HTTP codes on a parent POST that prove NOTHING was minted, so the caller
#: may retire the reservation and let a later attempt reserve afresh. A closed
#: list on purpose (2026-09-20): "any 4xx" swept in 408 Request Timeout and
#: 409 Conflict, either of which a proxy can return after the body reached
#: Zotero — and retiring on those would authorise a second unkeyed POST for a
#: PDF that may already have an item. Anything not listed stays `error`, which
#: blocks retry until a human looks.
_DEFINITIVE_REFUSAL_CODES = frozenset({400, 401, 403, 404, 405, 410, 413,
                                       414, 415, 422, 501})


def zotero_item_create_post(prepared):
    """The lock-free parent POST plus its readback. ``(result, NewParent|None)``.

    The readback is not a formality: it is where the `server_id` the attach
    gate compares against comes from, and it refuses an item whose type or
    title came back as something other than what we sent rather than linking
    a user's PDF under it.
    """
    from core.utils import log_vault_write

    c = prepared["commit"]
    transport, api_key, lib_path = c["transport"], c["api_key"], c["lib_path"]
    try:
        if c.get("before_dispatch"):
            c["before_dispatch"]()
        key = zotero_write.create_parent_item(
            transport.base_url, lib_path, c["item_type"], c["fields"],
            c["creators"], api_key=api_key,
            **({"server_id": c["server_id"]} if c.get("server_id") else {}))
    except zotero_write.ZoteroUnavailable:
        return {"ok": False, "status": "unreachable",
                "message": "Zotero response unavailable. Creation outcome is uncertain; automatic retry is blocked."}, None
    except zotero_write.ZoteroRateLimited as exc:
        return {"ok": False, "status": "error", "message": str(exc)}, None
    except (zotero_write.ZoteroAuthError, zotero_write.ZoteroNotFound,
            zotero_write.ZoteroWriteUnsupported) as exc:
        # A 4xx we SAW: nothing was minted. The caller retires the
        # reservation as never_created (2026-09-19 review A5).
        code = getattr(exc, "http_code", None) or (
            403 if isinstance(exc, zotero_write.ZoteroAuthError)
            else 404 if isinstance(exc, zotero_write.ZoteroNotFound) else 501)
        return {"ok": False, "status": "refused", "http_code": code,
                "message": str(exc)}, None
    except zotero_write.ZoteroError as exc:
        code = getattr(exc, "http_code", None)
        if code is not None and int(code) in _DEFINITIVE_REFUSAL_CODES:
            return {"ok": False, "status": "refused", "http_code": int(code),
                    "message": str(exc)}, None
        return {"ok": False, "status": "error", "message": str(exc)}, None
    if c.get("acknowledge"):
        c["acknowledge"](key)
    log_vault_write("tagging_zotero_item_create", str(key),
                    f"{c['item_type']} created")
    try:
        data, version = zotero_write.get_item(
            transport.base_url, lib_path, str(key), api_key=api_key,
            **({} if transport.is_web else {"server_id": c.get("server_id") or ""}))
    except (zotero_write.ZoteroError, zotero_write.ZoteroUnavailable) as exc:
        # The item EXISTS — the caller has acknowledged its key — so this is
        # a failed verification, never a failed create. Saying otherwise
        # would invite a retry that duplicates it.
        #
        # The POST's OWN acknowledgement rides along. It is the only creation
        # baseline that will ever exist for this item, and a resumed create
        # journals what the attempt store kept: without it the resume
        # succeeds and its restore op can never be authorised, so the batch
        # would report a clean create whose promised undo refuses forever.
        return {"ok": False, "status": "unverified", "item_key": str(key),
                "creation_data": getattr(key, "creation_data", None),
                "version": getattr(key, "creation_version", None),
                "message": ("The item was created but could not be read back: "
                            f"{exc}. Check it in Zotero before retrying.")}, None
    if data.get("itemType") != c["item_type"] or (data.get("title") or "") != str(
            c["fields"].get("title") or ""):
        # Same evidence, and still safe: the revert compares a later GET
        # against this baseline, so an item that really is something else
        # refuses the trash rather than being taken down on our say-so.
        return {"ok": False, "status": "unverified", "item_key": str(key),
                "creation_data": getattr(key, "creation_data", None),
                "version": getattr(key, "creation_version", None),
                "message": ("The created item did not read back as the item we "
                            "sent; not linking the PDF to it.")}, None
    parent = NewParent(str(key), c["item_type"], data.get("title") or "",
                       doi=data.get("DOI") or "",
                       server_id=getattr(version, "server_id", None))
    return ({"ok": True, "status": "created", "item_key": str(key),
             "version": int(version) if isinstance(version, int) else None,
             "creation_data": getattr(key, "creation_data", None)}, parent)


def zotero_item_create_journal(*, prepared, parent, version, creation_data,
                               pdf_rel, pdf_sha256, attachment_key,
                               manifest, batch_id=None):
    """Append the parent's restore op. Records the attachment key so the
    revert can tell OUR child from one the user added afterwards."""
    c = prepared["commit"]
    op = {
        "op_id": journal.new_op_id(manifest), "kind": OP_ITEM_CREATE,
        "ts": journal.now_iso(), "item_key": parent.item_key,
        "item_type": parent.item_type, "title": parent.title,
        "lib_path": c["lib_path"], "base_url": c["transport"].base_url,
        "transport": c["transport"].kind, "server_id": parent.server_id,
        "attachment_key": attachment_key,
        "pdf_rel": pdf_rel, "pdf_sha256": pdf_sha256,
        "version_after": version, "creation_data": creation_data,
        "state": "applied",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    return op


#: Fields Better BibTeX (and Zotero itself) write back onto a freshly created
#: parent. They are not the user's edit: treating them as one refused the
#: parent revert while the attachment op trashed the file (2026-09-19 review A2).
_CREATE_REVERT_VOLATILE = frozenset({"citationKey", "dateModified", "dateAdded", "version"})


def _creation_image_agrees(data, baseline):
    """True when *data* is still the item we created, ignoring BBT write-back."""
    if not isinstance(data, dict) or not isinstance(baseline, dict):
        return False
    keys = (set(data) | set(baseline)) - _CREATE_REVERT_VOLATILE
    return all(data.get(k) == baseline.get(k) for k in keys)


def _revert_zotero_item_create_prepare(cfg, op):
    """Read side of a parent-create revert — OUTSIDE the op-lock.

    Conservative in the same three ways as the attachment revert, plus one
    that is specific to a parent: it may carry children, and the only child
    this op authorizes trashing is the attachment it created. A note, a
    second file or an annotation added afterwards is the user's work, and a
    parent trash would take it along.
    """
    api_key = zotero_write.api_key_from_env() if op.get("transport") == "web" else None
    if op.get("transport") == "web" and not api_key:
        return {"early": {"ok": False, "status": "error", "message": (
            "This item was created via the Zotero web API; set "
            f"{zotero_write.API_KEY_ENV} to revert it.")}}
    base, lib_path, key = op.get("base_url"), op.get("lib_path") or "users/0", op.get("item_key")
    if not key or not base:
        return {"early": {"ok": False, "status": "invalid",
                          "message": "Op missing item_key."}}
    from tagging.local_api import restore_kwargs
    try:
        data, version = zotero_write.get_item(base, lib_path, key, api_key=api_key,
                                              **restore_kwargs(op))
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "error",
                          "message": "Zotero is not reachable."}}
    except zotero_write.ZoteroError as exc:
        return {"early": {"ok": False, "status": "error", "message": str(exc)}}
    if data.get("deleted"):
        return {"early": {"ok": True, "status": "skipped",
                          "message": "Already in Zotero's trash."}, "state": "reverted"}
    if (data.get("itemType") != op.get("item_type")
            or (data.get("title") or "") != (op.get("title") or "")
            or not op.get("version_after")
            or not op.get("creation_data")
            or not _creation_image_agrees(data, op["creation_data"])):
        return {"early": {"ok": False, "status": "conflict",
                          "message": ("The item changed since it was created, or its "
                                      "creation version is unverified. Review it in "
                                      "Zotero; not trashing.")}}
    try:
        children = zotero_write.get_item_children(base, lib_path, key, api_key=api_key,
                                                  **restore_kwargs(op))
    except (zotero_write.ZoteroError, zotero_write.ZoteroUnavailable) as exc:
        return {"early": {"ok": False, "status": "error", "message": str(exc)}}
    ours = {op.get("attachment_key")} - {None}
    foreign = [c for c in children if c.get("key") not in ours]
    if foreign:
        return {"early": {"ok": False, "status": "conflict",
                          "message": ("The item has notes or attachments added after "
                                      "it was created; a trash would take them too. "
                                      "Review them in Zotero; not trashing.")}}
    return {"commit": {"base": base, "lib_path": lib_path, "key": key,
                       "api_key": api_key, "version": version}}


def _revert_zotero_item_create_commit(prepared, op):
    """Write side: ONE guarded PATCH ``deleted=1`` — a trash, never a DELETE."""
    from core.utils import log_vault_write

    c = prepared["commit"]
    from tagging.local_api import origin
    if origin(c["base"]):
        # Same hole as the attachment undo (verified on Desktop 10.0.1): a
        # child added between the read above and this PATCH does not move the
        # parent's version, so an HTTP guard cannot see it.
        return {"ok": False, "status": "unsupported",
                "message": ("Automatic Desktop undo of an item create requires an "
                            "atomic child check. Move the item to Zotero's trash by "
                            "hand; no trash request was sent.")}
    try:
        zotero_write.set_item_deleted(c["base"], c["lib_path"], c["key"], True,
                                      c["version"], api_key=c["api_key"])
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroVersionConflict,
            zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "error", "message": str(exc)}
    log_vault_write("tagging_restore_zotero_item_create", c["key"],
                    "created item trashed")
    op["state"] = "reverted"
    return {"ok": True, "status": "reverted",
            "message": "Created item moved to Zotero's trash."}


def _revert_zotero_item_create(vault_root, cfg, op):
    """Composed prepare + commit for the single-op restore route."""
    prepared = _revert_zotero_item_create_prepare(cfg, op)
    if "early" in prepared:
        if prepared.get("state"):
            op["state"] = prepared["state"]
        return prepared["early"]
    return _revert_zotero_item_create_commit(prepared, op)


# --- linked-file attachment RE-POINT (2026-09-07) ---------------------------
#
# A `linked_file` attachment stores an absolute path, and this library holds
# 5 242 of them (the 2026-09-06 attach run), so every rename of a biblio PDF
# breaks a real Zotero reference. Nine were repaired by hand after the
# Zotero-preference and Attanger rename incidents; this is that repair as a
# verb, version-guarded and journalled.
#
# Only the `path` field moves. Zotero's DISPLAY title is deliberately left
# alone: the vault filename is authoritative and the display name is nobody's
# business — the maintainer's contract, and it also keeps the diff to one
# field, which is what makes the revert exact.

OP_ATTACHMENT_REPATH = "zotero_attachment_repath"


_LINKED_BY_PATH: dict | None = None
_LINKED_BY_PATH_LOCK = threading.Lock()


def find_linked_attachments(settings, abs_path: str) -> tuple[list, str | None]:
    """Every ``linked_file`` attachment pointing at *abs_path*, read FRESH.

    Returns ``(refs, error)``. The error arm is not decoration: "Zotero holds
    no link to this file" and "Zotero could not be read" are opposite facts,
    and collapsing both to ``[]`` would let a rename report that nothing in
    Zotero needed changing when in truth nothing was looked at — the failure
    this whole path exists to prevent, arrived at from the other side.

    Fresh, not from the cached scan (2026-09-07): the scan's attachment set is
    a snapshot, and a link created since it would be missed. Reading the
    SQLite costs ~0.5 s for 6 400 items and is memoised on
    the identities, sizes and modification times of the database and its
    WAL/SHM sidecars, checked again after the read.

    Each ref carries ``item_key`` and the PARENT's library routing, which is
    what a PATCH needs and what the attachment row alone does not have.
    """
    global _LINKED_BY_PATH
    from audit.core.zotero import linked_path_key, read_items

    raw_sqlite = getattr(settings, "zotero_sqlite", None)
    if not raw_sqlite:
        return [], "No Zotero database is configured."
    sqlite_path = Path(raw_sqlite)
    try:
        def signature():
            stamps = []
            for suffix in ("", "-wal", "-shm"):
                try:
                    st = Path(str(sqlite_path) + suffix).stat()
                    stamps.append((st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns))
                except FileNotFoundError:
                    if not suffix:
                        raise
                    stamps.append(None)
            return tuple(stamps)
        sig = signature()
    except OSError as exc:
        return [], f"Zotero database unreadable: {sanitise_error_msg(exc)}"
    with _LINKED_BY_PATH_LOCK:
        cached = _LINKED_BY_PATH
    def _lookup(store: dict) -> list:
        # Same key the scan's zotero_link_index uses (realpath + NFC +
        # casefold). Matching on the stored string's bytes would miss an
        # NFD or case-variant path the scan had already paired, and a
        # rename would then leave Zotero pointing at a missing file.
        folded = list(store.get(linked_path_key(str(abs_path)), ()))
        if len(folded) < 2:
            return folded
        # The fold is deliberately lossy, so on a CASE-SENSITIVE volume two
        # genuinely different files collide in one bucket and a rename would
        # repath both — aiming one item's pointer at a file it never owned.
        # `zotero_link_index` drops such a clash rather than guessing; here
        # the caller named an exact file, so prefer the refs whose stored path
        # really resolves to it and fall back to the bucket only when none
        # does (the NFD / case-variant case this key exists for).
        want = os.path.realpath(str(abs_path))
        exact = [r for r in folded
                 if os.path.realpath(str(r.raw_path or "")) == want]
        return exact or folded

    if not (cached and cached["sig"] == sig and cached["path"] == str(sqlite_path)):
        by_path: dict[str, list] = {}
        try:
            items = read_items(sqlite_path, Path(getattr(settings, "zotero_storage", "")))
        except Exception as exc:  # noqa: BLE001 — sqlite3 errors et al.
            return [], f"Zotero database unreadable: {sanitise_error_msg(exc)}"
        for item in items:
            for att in item.attachments:
                if att.link_mode != "linked_file":
                    continue
                raw = str(att.raw_path or "")
                if raw and not raw.startswith("attachments:"):
                    by_path.setdefault(linked_path_key(raw), []).append(
                        _AttachmentRef(att, item))
        try:
            if signature() != sig:
                return [], "Zotero changed during attachment lookup. Retry before renaming."
        except OSError as exc:
            return [], f"Zotero database unreadable: {sanitise_error_msg(exc)}"
        cached = {"sig": sig, "path": str(sqlite_path), "by_path": by_path}
        with _LINKED_BY_PATH_LOCK:
            _LINKED_BY_PATH = cached
    return _lookup(cached["by_path"]), None


class _AttachmentRef:
    """One linked attachment plus the library routing its PATCH needs.

    ``_zotero_lib_path`` reads ``library_type``/``group_id`` off whatever it is
    given, and those live on the PARENT item, not on the attachment row — so
    the pair travels together rather than the caller re-deriving it.
    """

    __slots__ = ("item_key", "raw_path", "title", "library_type", "group_id",
                 "parent_key", "annotation_count")

    def __init__(self, att, parent):
        self.item_key = att.item_key
        self.raw_path = att.raw_path
        self.title = getattr(att, "filename", "") or ""
        self.library_type = getattr(parent, "library_type", None)
        self.group_id = getattr(parent, "group_id", None)
        self.parent_key = getattr(parent, "item_key", None)
        self.annotation_count = getattr(att, "annotation_count", 0)


def zotero_attachment_repath_prepare(cfg, item_key, new_path, row=None):
    """Read an attachment before re-pointing it — runs BEFORE the op-lock.

    Same split and the same reason as :func:`zotero_attachment_prepare`: the
    GET may sleep on a Zotero ``Backoff`` header, and the
    ``If-Unmodified-Since-Version`` guard on the PATCH — not the op-lock — is
    what prevents a lost update.

    Returns ``{"early": <finished result>}`` or ``{"commit": {...}}``. The
    stored path is captured for the revert, so the undo restores the exact
    string Zotero held rather than one this app recomputed.
    """
    transport = _zotero_transport(cfg)
    api_key = transport.api_key if transport.is_web else None
    if transport.is_web and not api_key:
        return {"early": {"ok": False, "status": "error",
                          "message": (f"Set {zotero_write.API_KEY_ENV} to write to "
                                      "the Zotero web API.")}}
    try:
        lib_path = _zotero_lib_path(transport, row)
        data, version = zotero_write.get_item(
            transport.base_url, lib_path, item_key, api_key=api_key)
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "unreachable",
                          "message": "Zotero is not reachable."}}
    except zotero_write.ZoteroError as exc:
        return {"early": {"ok": False, "status": "error", "message": str(exc)}}

    if data.get("itemType") != "attachment":
        return {"early": {"ok": False, "status": "error",
                          "message": "That Zotero item is not an attachment."}}
    if data.get("linkMode") != "linked_file":
        # An imported attachment's bytes live in Zotero's own storage and its
        # `path` is a storage: reference; rewriting it would break the item,
        # not repair it.
        return {"early": {"ok": False, "status": "error",
                          "message": "Only a linked-file attachment has a path "
                                     "to re-point."}}
    before = str(data.get("path") or "")
    if before == new_path:
        return {"early": {"ok": True, "status": "skipped",
                          "message": "Already points there."}}
    return {"commit": {
        "transport": transport, "api_key": api_key, "lib_path": lib_path,
        "item_key": item_key, "version": version,
        "before_path": before, "after_path": new_path,
        "parent_item": data.get("parentItem"),
        "title": data.get("title") or "",
    }}


def zotero_attachment_repath_journal(*, prepared, manifest, batch_id=None,
                                     citation_key=None):
    """Append (do NOT save) the ``zotero_attachment_repath`` op. Returns it.

    Journalled BEFORE the PATCH, like every writer here: an op whose PATCH
    never ran reverts by setting the path back to what it already is, which
    is idempotent. ``before_path`` is the whole restore mapping and lives
    inline, which is why the kind is in ``EXTERNAL_OP_KINDS``.
    """
    c = prepared["commit"]
    op = {
        "op_id": journal.new_op_id(manifest),
        "kind": OP_ATTACHMENT_REPATH,
        "ts": journal.now_iso(),
        "citation_key": citation_key,
        "item_key": c["item_key"],
        "parent_item": c["parent_item"],
        "lib_path": c["lib_path"],
        "base_url": c["transport"].base_url,
        "transport": c["transport"].kind,
        "title": c["title"],
        "before_path": c["before_path"],
        "after_path": c["after_path"],
        "server_id": getattr(c["version"], "server_id", None),
        "state": "applied",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    return op


def zotero_attachment_repath_patch(prepared):
    """The lock-free network half. ``(result, status, new_version | None)``."""
    from core.utils import log_vault_write

    c = prepared["commit"]
    try:
        new_version = zotero_write.set_attachment_path(
            c["transport"].base_url, c["lib_path"], c["item_key"],
            c["after_path"], c["version"], api_key=c["api_key"])
    except zotero_write.ZoteroVersionConflict as exc:
        return {"ok": False, "status": "conflict", "message": str(exc)}, 409, None
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "error", "message": str(exc)}, 502, None
    log_vault_write("tagging_zotero_attachment_repath", c["item_key"],
                    f"{c['before_path']} -> {c['after_path']}")
    return ({"ok": True, "status": "repointed", "item_key": c["item_key"],
             "path": c["after_path"],
             "message": "Zotero now points at the new path."}, 200, new_version)


def _revert_zotero_attachment_repath_prepare(cfg, op):
    """Read side of a repath revert — OUTSIDE the op-lock."""
    item_key = op.get("item_key")
    before = op.get("before_path")
    if not item_key or not before:
        return {"early": {"ok": False, "status": "invalid",
                          "message": "Op missing item_key or before_path."},
                "state": None}
    base = op.get("base_url") or _zotero_base(cfg)
    lib_path = op.get("lib_path") or "users/0"
    api_key = (zotero_write.api_key_from_env()
               if op.get("transport") == "web" else None)
    if op.get("transport") == "web" and not api_key:
        return {"early": {"ok": False, "status": "error",
                          "message": (f"This attachment was re-pointed via the Zotero "
                                      f"web API; set {zotero_write.API_KEY_ENV} to "
                                      "put it back.")},
                "state": None}
    from tagging.local_api import restore_kwargs
    try:
        data, version = zotero_write.get_item(base, lib_path, item_key,
                                              api_key=api_key, **restore_kwargs(op))
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "error",
                          "message": "Zotero is not reachable."}, "state": None}
    except zotero_write.ZoteroError as exc:
        if "404" in str(exc):
            return {"early": {"ok": True, "status": "skipped",
                              "message": "Attachment no longer exists in Zotero."},
                    "state": "reverted"}
        return {"early": {"ok": False, "status": "error", "message": str(exc)},
                "state": None}
    current = str(data.get("path") or "")
    if current == before:
        return {"early": {"ok": True, "status": "skipped",
                          "message": "Already points at the original path."},
                "state": "reverted"}
    if current != str(op.get("after_path") or ""):
        # Someone re-pointed it again after us. Putting OUR old path back would
        # silently discard their decision, so refuse and keep the op
        # retryable — the conservative-revert rule the note and tag reverts
        # follow.
        return {"early": {"ok": False, "status": "skipped",
                          "message": "The attachment was re-pointed again after "
                                     "this change; left as it is."},
                "state": None}
    return {"commit": {"base": base, "lib_path": lib_path, "item_key": item_key,
                       "api_key": api_key, "version": version,
                       "after_path": before}}


def _revert_zotero_attachment_repath_commit(prepared, op):
    """Write side of a repath revert — ONE PATCH back to ``before_path``."""
    from core.utils import log_vault_write

    c = prepared["commit"]
    try:
        zotero_write.set_attachment_path(c["base"], c["lib_path"], c["item_key"],
                                         c["after_path"], c["version"],
                                         api_key=c["api_key"])
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroVersionConflict,
            zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "error", "message": str(exc)}
    log_vault_write("tagging_restore_zotero_attachment_repath", c["item_key"],
                    str(c["after_path"]))
    op["state"] = "reverted"
    return {"ok": True, "status": "reverted",
            "message": "Zotero points at the original path again."}


def _revert_zotero_attachment_repath(vault_root, cfg, op):
    """Put a re-pointed attachment back. Composed prepare + commit."""
    prepared = _revert_zotero_attachment_repath_prepare(cfg, op)
    if "early" in prepared:
        if prepared.get("state"):
            op["state"] = prepared["state"]
        return prepared["early"]
    return _revert_zotero_attachment_repath_commit(prepared, op)


# --- linked-file attachment RETITLE (annual cleanup T4, 2026-09-18) ---------
#
# Decision D3 of the 2026-09-17 cleanup plan: a Zotero attachment's display
# title equals its vault filename. Measured before this was built: 653 of the
# 6,246 personal linked attachments carried another title — 505 "Full Text
# PDF"/"PDF" from connector saves, 141 like "Texte intégral", 7 a stale name.
#
# The TARGET title is never taken from the caller. It is the basename of the
# path Zotero holds at the moment of the read, so a request can only ever say
# "make these attachments' titles match their files", never "write this text
# into Zotero" — the keys-only posture of harmonize and apply-pdf-links.

OP_ATTACHMENT_RETITLE = "zotero_attachment_retitle"


def attachment_filename(path: str) -> str:
    """The basename of a Zotero attachment ``path`` (absolute or ``attachments:``)."""
    raw = str(path or "")
    if raw.startswith("attachments:"):
        raw = raw[len("attachments:"):]
    return raw.replace("\\", "/").rsplit("/", 1)[-1]


def zotero_attachment_retitle_prepare(cfg, item_key, row=None):
    """Read an attachment before retitling it — runs BEFORE the op-lock.

    Returns ``{"early": <finished result>}`` or ``{"commit": {...}}``, the
    split every Zotero writer here uses (a GET may sleep on ``Backoff``; the
    version guard on the PATCH, not the lock, prevents a lost update).
    """
    transport = _zotero_transport(cfg)
    api_key = transport.api_key if transport.is_web else None
    if transport.is_web and not api_key:
        return {"early": {"ok": False, "status": "error",
                          "message": (f"Set {zotero_write.API_KEY_ENV} to write to "
                                      "the Zotero web API.")}}
    try:
        lib_path = _zotero_lib_path(transport, row)
        data, version = zotero_write.get_item(
            transport.base_url, lib_path, item_key, api_key=api_key)
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "unreachable",
                          "message": "Zotero is not reachable."}}
    except zotero_write.ZoteroError as exc:
        return {"early": {"ok": False, "status": "error", "message": str(exc)}}

    if data.get("itemType") != "attachment":
        return {"early": {"ok": False, "status": "error",
                          "message": "That Zotero item is not an attachment."}}
    if data.get("linkMode") != "linked_file":
        # Only a linked file has a vault filename to match; an imported copy's
        # name is Zotero's own storage detail.
        return {"early": {"ok": False, "status": "error",
                          "message": "Only a linked-file attachment is retitled "
                                     "to its filename."}}
    if data.get("deleted"):
        return {"early": {"ok": False, "status": "error",
                          "message": "That attachment is in Zotero's trash."}}
    target = attachment_filename(data.get("path") or "")
    if not target:
        return {"early": {"ok": False, "status": "error",
                          "message": "The attachment has no path to take a name from."}}
    before = data.get("title")
    before = before if isinstance(before, str) else ""
    if before == target:
        return {"early": {"ok": True, "status": "skipped",
                          "message": "The title already equals the filename.",
                          "title": target}}
    return {"commit": {
        "transport": transport, "api_key": api_key, "lib_path": lib_path,
        "item_key": item_key, "version": version,
        "before_title": before, "after_title": target,
        "parent_item": data.get("parentItem"),
        "path": str(data.get("path") or ""),
    }}


def zotero_attachment_retitle_journal(*, prepared, manifest, batch_id=None,
                                      citation_key=None):
    """Append (do NOT save) the ``zotero_attachment_retitle`` op. Returns it.

    Journalled BEFORE the PATCH. ``before_title`` is the whole restore mapping
    and lives inline, which is why the kind is in ``EXTERNAL_OP_KINDS``.
    """
    c = prepared["commit"]
    op = {
        "op_id": journal.new_op_id(manifest),
        "kind": OP_ATTACHMENT_RETITLE,
        "ts": journal.now_iso(),
        "citation_key": citation_key,
        "item_key": c["item_key"],
        "parent_item": c["parent_item"],
        "lib_path": c["lib_path"],
        "base_url": c["transport"].base_url,
        "transport": c["transport"].kind,
        "path": c["path"],
        "before_title": c["before_title"],
        "after_title": c["after_title"],
        "server_id": getattr(c["version"], "server_id", None),
        "state": "applied",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    return op


def zotero_attachment_retitle_patch(prepared):
    """The lock-free network half. ``(result, status, new_version | None)``."""
    from core.utils import log_vault_write

    c = prepared["commit"]
    try:
        new_version = zotero_write.set_attachment_title(
            c["transport"].base_url, c["lib_path"], c["item_key"],
            c["after_title"], c["version"], api_key=c["api_key"])
    except zotero_write.ZoteroVersionConflict as exc:
        return {"ok": False, "status": "conflict", "message": str(exc)}, 409, None
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "error", "message": str(exc)}, 502, None
    log_vault_write("tagging_zotero_attachment_retitle", c["item_key"],
                    f"{c['before_title']!r} -> {c['after_title']!r}")
    return ({"ok": True, "status": "retitled", "item_key": c["item_key"],
             "title": c["after_title"], "before_title": c["before_title"],
             "message": "The attachment title now equals its filename."},
            200, new_version)


def _revert_zotero_attachment_retitle_prepare(cfg, op):
    """Read side of a retitle revert — OUTSIDE the op-lock."""
    item_key = op.get("item_key")
    before = op.get("before_title")
    after = op.get("after_title")
    if not item_key or not isinstance(before, str) or not after:
        return {"early": {"ok": False, "status": "invalid",
                          "message": "Op missing item_key or its titles."},
                "state": None}
    base = op.get("base_url") or _zotero_base(cfg)
    lib_path = op.get("lib_path") or "users/0"
    api_key = (zotero_write.api_key_from_env()
               if op.get("transport") == "web" else None)
    if op.get("transport") == "web" and not api_key:
        return {"early": {"ok": False, "status": "error",
                          "message": (f"This attachment was retitled via the Zotero "
                                      f"web API; set {zotero_write.API_KEY_ENV} to "
                                      "put its title back.")},
                "state": None}
    from tagging.local_api import restore_kwargs
    try:
        data, version = zotero_write.get_item(base, lib_path, item_key,
                                              api_key=api_key, **restore_kwargs(op))
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "error",
                          "message": "Zotero is not reachable."}, "state": None}
    except zotero_write.ZoteroError as exc:
        if "404" in str(exc):
            return {"early": {"ok": True, "status": "skipped",
                              "message": "Attachment no longer exists in Zotero."},
                    "state": "reverted"}
        return {"early": {"ok": False, "status": "error", "message": str(exc)},
                "state": None}
    current = data.get("title")
    current = current if isinstance(current, str) else ""
    if current == before:
        return {"early": {"ok": True, "status": "skipped",
                          "message": "The title is already the original one."},
                "state": "reverted"}
    if current != after:
        # Retitled again after us: restoring OUR old title would discard that
        # later decision. Refuse and keep the op retryable (conservative revert).
        return {"early": {"ok": False, "status": "skipped",
                          "message": "The attachment was retitled again after "
                                     "this change; left as it is."},
                "state": None}
    if not before:
        # Zotero cannot hold an empty title through this verb; the write would
        # be refused. Say so instead of issuing a PATCH that cannot succeed.
        return {"early": {"ok": False, "status": "error",
                          "message": "The original title was empty; clear it in "
                                     "Zotero by hand if needed."},
                "state": None}
    return {"commit": {"base": base, "lib_path": lib_path, "item_key": item_key,
                       "api_key": api_key, "version": version,
                       "after_title": before}}


def _revert_zotero_attachment_retitle_commit(prepared, op):
    """Write side of a retitle revert — ONE PATCH back to ``before_title``."""
    from core.utils import log_vault_write

    c = prepared["commit"]
    try:
        zotero_write.set_attachment_title(c["base"], c["lib_path"], c["item_key"],
                                          c["after_title"], c["version"],
                                          api_key=c["api_key"])
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroVersionConflict,
            zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "error", "message": str(exc)}
    log_vault_write("tagging_restore_zotero_attachment_retitle", c["item_key"],
                    repr(c["after_title"]))
    op["state"] = "reverted"
    return {"ok": True, "status": "reverted",
            "message": "The attachment has its original title again."}


def _revert_zotero_attachment_retitle(vault_root, cfg, op):
    """Put a retitled attachment's title back. Composed prepare + commit."""
    prepared = _revert_zotero_attachment_retitle_prepare(cfg, op)
    if "early" in prepared:
        if prepared.get("state"):
            op["state"] = prepared["state"]
        return prepared["early"]
    return _revert_zotero_attachment_retitle_commit(prepared, op)


# --- Bibliography-campaign metadata writes (phase 5, 2026-09-16) -------------
#
# ONE explicitly approved field set per item, written through the shared
# transport under the version guard, journalled before the PATCH and reverted
# from the journal. Zotero normalises on save — observed on 10.0.1: a
# title-only PATCH promoted an ``Extra: Citation Key:`` line into native
# ``citationKey`` — so the writer (a) refuses BEFORE the PATCH when it can
# predict that an unselected field would change, and (b) re-reads the item
# AFTER the PATCH and records every unselected field that changed anyway, so
# the revert can put those back too. Better BibTeX keeps its pinned key in
# ``extra`` or ``citationKey``; neither is ever touched unless explicitly
# selected, and a selected ``extra`` may not silently drop the key line.

OP_METADATA_WRITE = "zotero_metadata_write"
#: Fields whose value the readback comparison ignores: the server owns them.
_METADATA_VOLATILE = frozenset({"version", "dateModified"})
_CITATION_KEY_LINE = "citation key:"


def extra_citation_key(extra) -> str | None:
    """The Better BibTeX ``Citation Key: x`` line inside ``extra``, or None."""
    for line in str(extra or "").splitlines():
        if line.strip().lower().startswith(_CITATION_KEY_LINE):
            return line.split(":", 1)[1].strip() or None
    return None


def predicted_unselected_changes(data: dict, fields: dict) -> list[dict]:
    """Normalisations Zotero is KNOWN to apply on save that would move a field
    the caller did not select. Each entry names the field, the reason and the
    value it would take, so a refusal can be read and acted on."""
    changes = []
    selected = set(fields)
    pinned = extra_citation_key(data.get("extra"))
    native = str(data.get("citationKey") or "")
    if pinned and not native and "citationKey" not in selected:
        changes.append(dict(field="citationKey", reason="extra_citation_key_promotion",
                            current=native, predicted=pinned,
                            detail="Zotero promotes the Extra 'Citation Key:' line into the native citationKey on "
                                   "save; select citationKey explicitly (with that value) or leave this item alone."))
    if "extra" in selected and pinned and extra_citation_key(fields.get("extra")) != pinned \
            and "citationKey" not in selected:
        changes.append(dict(field="citationKey", reason="extra_edit_drops_citation_key",
                            current=pinned, predicted=extra_citation_key(fields.get("extra")) or "",
                            detail="The proposed Extra removes or changes the Better BibTeX 'Citation Key:' line; "
                                   "that is a citation-key change and must be selected as such."))
    return changes


def zotero_metadata_prepare(cfg, item_key, fields: dict, expected_current: dict, *, row=None):
    """Read an item before writing approved fields — runs BEFORE the op-lock.

    ``fields`` maps field → approved value; ``expected_current`` maps the same
    fields → the value the campaign reviewed (the readback must still match
    it, or the review is stale). Returns ``{"early": ...}`` or
    ``{"commit": ...}`` carrying the full before-image for the post-write
    comparison and the revert.
    """
    if not isinstance(fields, dict) or not fields:
        return {"early": {"ok": False, "status": "invalid", "message": "No approved field to write."}}
    reserved = sorted(set(fields) & zotero_write.METADATA_RESERVED_FIELDS)
    if reserved:
        return {"early": {"ok": False, "status": "invalid",
                          "message": "Structural fields cannot be written: " + ", ".join(reserved) + "."}}
    transport = _zotero_transport(cfg)
    api_key = transport.api_key if transport.is_web else None
    if transport.is_web and not api_key:
        return {"early": {"ok": False, "status": "error",
                          "message": f"Set {zotero_write.API_KEY_ENV} to write to the Zotero web API."}}
    try:
        identity = _zotero_read_identity(transport, row)
        lib_path = _zotero_lib_path(transport, row)
        data, version = zotero_write.get_item(transport.base_url, lib_path, item_key,
                                              api_key=api_key, **identity)
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "unreachable", "message": "Zotero is not reachable."}}
    except zotero_write.ZoteroError as exc:
        return {"early": {"ok": False, "status": "error", "message": str(exc)}}
    if data.get("itemType") in ("note", "attachment", "annotation") or data.get("deleted"):
        return {"early": {"ok": False, "status": "error",
                          "message": "Only a live bibliographic item can receive metadata."}}
    stale = {f: dict(reviewed=expected_current.get(f), current=data.get(f))
             for f in fields if data.get(f, "") != (expected_current.get(f) if expected_current.get(f) is not None else "")}
    if stale:
        return {"early": {"ok": False, "status": "stale", "fields": stale,
                          "message": "The item changed since review for: " + ", ".join(sorted(stale)) + "; review again."}}
    predicted = predicted_unselected_changes(data, fields)
    if predicted:
        return {"early": {"ok": False, "status": "unselected_field_would_change", "predicted": predicted,
                          "message": "Writing the selected fields would also change: "
                                     + ", ".join(sorted({c["field"] for c in predicted})) + "."}}
    unchanged = [f for f in fields if data.get(f, "") == fields[f]]
    if len(unchanged) == len(fields):
        return {"early": {"ok": True, "status": "skipped", "message": "Every approved value is already stored."}}
    return {"commit": {
        "transport": transport, "api_key": api_key, "lib_path": lib_path, "item_key": item_key,
        "version": version, "before_data": dict(data),
        "before_fields": {f: data.get(f, "") for f in fields}, "after_fields": dict(fields),
    }}


def zotero_metadata_journal(*, prepared, manifest, batch_id=None, citation_key=None, campaign=None):
    """Append (do NOT save) the ``zotero_metadata_write`` op; journalled BEFORE
    the PATCH. ``before_fields`` is the restore mapping and lives inline."""
    c = prepared["commit"]
    op = {
        "op_id": journal.new_op_id(manifest), "kind": OP_METADATA_WRITE, "ts": journal.now_iso(),
        "citation_key": citation_key, "campaign": campaign, "item_key": c["item_key"],
        "lib_path": c["lib_path"], "base_url": c["transport"].base_url, "transport": c["transport"].kind,
        "before_fields": c["before_fields"], "after_fields": c["after_fields"],
        "version_before": int(c["version"]), "server_id": getattr(c["version"], "server_id", None),
        "state": "outcome_unknown",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    return op


def zotero_metadata_patch(prepared):
    """The lock-free network half: ONE PATCH, then a readback.

    ``(result, status, new_version | None)``. The readback decides what the
    write did: a selected field that does not read back as approved is
    ``readback_differs``; an unselected field that moved is
    ``unselected_field_changed`` and is reported with its before/after so the
    journal keeps it for the revert. Neither is treated as success.
    """
    from core.utils import log_vault_write
    c = prepared["commit"]
    try:
        zotero_write.set_item_fields(c["transport"].base_url, c["lib_path"], c["item_key"],
                                     c["after_fields"], c["version"], api_key=c["api_key"])
    except zotero_write.ZoteroVersionConflict as exc:
        return {"ok": False, "status": "conflict", "message": str(exc)}, 409, None
    except zotero_write.ZoteroWriteUnsupported as exc:
        return {"ok": False, "status": "unsupported", "message": str(exc)}, 501, None
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "outcome_unknown", "message": str(exc)}, 502, None
    try:
        data, version = zotero_write.get_item(
            c["transport"].base_url, c["lib_path"], c["item_key"], api_key=c["api_key"],
            **({"server_id": c["version"].server_id} if getattr(c["version"], "server_id", None) else {}))
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "outcome_unknown",
                "message": "Write sent but readback failed: " + str(exc)}, 502, None
    differs = {f: data.get(f, "") for f, v in c["after_fields"].items() if data.get(f, "") != v}
    unexpected = {}
    for field in set(c["before_data"]) | set(data):
        if field in c["after_fields"] or field in _METADATA_VOLATILE:
            continue
        if c["before_data"].get(field) != data.get(field):
            unexpected[field] = dict(before=c["before_data"].get(field), after=data.get(field))
    log_vault_write("tagging_zotero_metadata_write", c["item_key"],
                    ", ".join(sorted(c["after_fields"])) + (" +unexpected:" + ",".join(sorted(unexpected)) if unexpected else ""))
    if differs:
        return {"ok": False, "status": "readback_differs", "fields": differs, "unexpected_changes": unexpected,
                "message": "Zotero stored something other than the approved value for: " + ", ".join(sorted(differs)) + "."}, 502, version
    if unexpected:
        return {"ok": False, "status": "unselected_field_changed", "unexpected_changes": unexpected,
                "message": "Zotero also changed unselected fields: " + ", ".join(sorted(unexpected))
                           + ". The write is journalled with those changes; review and revert if unwanted."}, 409, version
    return {"ok": True, "status": "applied", "item_key": c["item_key"], "fields": sorted(c["after_fields"]),
            "message": "Approved fields written and read back exactly."}, 200, version


def _revert_zotero_metadata_prepare(cfg, op):
    """Read side of a metadata revert — OUTSIDE the op-lock. Conservative: every
    field this op wrote (and every unexpected change it recorded) must still
    read as the value the op left behind, or the revert refuses."""
    item_key = op.get("item_key")
    before = op.get("before_fields") or {}
    if not item_key or not before:
        return {"early": {"ok": False, "status": "invalid", "message": "Op missing item_key or before_fields."}, "state": None}
    base = op.get("base_url") or _zotero_base(cfg)
    lib_path = op.get("lib_path") or "users/0"
    api_key = zotero_write.api_key_from_env() if op.get("transport") == "web" else None
    if op.get("transport") == "web" and not api_key:
        return {"early": {"ok": False, "status": "error",
                          "message": f"These fields were written via the Zotero web API; set {zotero_write.API_KEY_ENV} to revert."},
                "state": None}
    from tagging.local_api import restore_kwargs
    try:
        data, version = zotero_write.get_item(base, lib_path, item_key, api_key=api_key, **restore_kwargs(op))
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "error", "message": "Zotero is not reachable."}, "state": None}
    except zotero_write.ZoteroError as exc:
        if "404" in str(exc):
            return {"early": {"ok": True, "status": "skipped", "message": "Item no longer exists in Zotero."}, "state": "reverted"}
        return {"early": {"ok": False, "status": "error", "message": str(exc)}, "state": None}
    expected_now = dict(op.get("after_fields") or {})
    for field, change in (op.get("unexpected_changes") or {}).items():
        expected_now[field] = change.get("after")
    restore = dict(before)
    for field, change in (op.get("unexpected_changes") or {}).items():
        restore[field] = change.get("before")
    if all(data.get(f, "") == (v if v is not None else "") for f, v in restore.items()):
        return {"early": {"ok": True, "status": "skipped", "message": "Fields already carry their original values."}, "state": "reverted"}
    moved = [f for f, v in expected_now.items() if data.get(f, "") != (v if v is not None else "")]
    if moved:
        return {"early": {"ok": False, "status": "skipped",
                          "message": "Edited again after this write (" + ", ".join(sorted(moved)) + "); left as it is."},
                "state": None}
    writable = {f: (v if v is not None else "") for f, v in restore.items()
                if f not in zotero_write.METADATA_RESERVED_FIELDS}
    return {"commit": {"base": base, "lib_path": lib_path, "item_key": item_key, "api_key": api_key,
                       "version": version, "fields": writable}}


def _revert_zotero_metadata_commit(prepared, op):
    """Write side of a metadata revert — ONE PATCH back to the before-image."""
    from core.utils import log_vault_write
    c = prepared["commit"]
    try:
        zotero_write.set_item_fields(c["base"], c["lib_path"], c["item_key"], c["fields"], c["version"], api_key=c["api_key"])
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroVersionConflict, zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "error", "message": str(exc)}
    log_vault_write("tagging_restore_zotero_metadata", c["item_key"], ", ".join(sorted(c["fields"])))
    op["state"] = "reverted"
    return {"ok": True, "status": "reverted", "message": "Fields restored in Zotero."}


def _revert_zotero_metadata(vault_root, cfg, op):
    """Put written metadata back. Composed prepare + commit."""
    prepared = _revert_zotero_metadata_prepare(cfg, op)
    if "early" in prepared:
        if prepared.get("state"):
            op["state"] = prepared["state"]
        return prepared["early"]
    return _revert_zotero_metadata_commit(prepared, op)


def _revert_zotero_attachment_prepare(cfg, op):
    """Read side of a ``zotero_attachment_trash`` revert — OUTSIDE the op-lock.

    The exact mirror of :func:`_revert_zotero_prepare` (2026-09-02): the GET
    (which may honour a Zotero ``Backoff`` and sleep) and every early exit run
    lock-free, so a wedged Zotero cannot stall indexing. Returns
    ``{"early": <result>, "state": <flip or None>}`` when there is nothing to
    PATCH, or ``{"commit": {...}}``.

    It deliberately does NOT mutate ``op``: the batch caller prepares against a
    peek of the journal and applies the state flip to a FRESHLY loaded manifest
    in its final hold, so a flip written here would be discarded.
    """
    item_key = op.get("item_key")
    if not item_key:
        return {"early": {"ok": False, "status": "invalid",
                          "message": "Op missing item_key."}, "state": None}
    base = op.get("base_url") or _zotero_base(cfg)
    lib_path = op.get("lib_path") or "users/0"
    api_key = (zotero_write.api_key_from_env()
               if op.get("transport") == "web" else None)
    if op.get("transport") == "web" and not api_key:
        return {"early": {"ok": False, "status": "error",
                          "message": (f"This attachment was trashed via the Zotero web API; "
                                      f"set {zotero_write.API_KEY_ENV} to restore it.")},
                "state": None}
    from tagging.local_api import restore_kwargs
    try:
        data, version = zotero_write.get_item(base, lib_path, item_key,
                                              api_key=api_key, **restore_kwargs(op))
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "error",
                          "message": "Zotero is not reachable."}, "state": None}
    except zotero_write.ZoteroError as exc:
        # A 404 here means the trash was emptied: the attachment is gone for
        # good and no restore is possible. Spending the op is correct — leaving
        # it `applied` would offer an undo that can never succeed.
        if "404" in str(exc):
            return {"early": {"ok": True, "status": "skipped",
                              "message": "Attachment no longer exists in Zotero "
                                         "(the trash was emptied)."},
                    "state": "reverted"}
        return {"early": {"ok": False, "status": "error", "message": str(exc)},
                "state": None}
    if not data.get("deleted"):
        return {"early": {"ok": True, "status": "skipped",
                          "message": "Attachment is not in the trash — nothing to restore."},
                "state": "reverted"}
    return {"commit": {"base": base, "lib_path": lib_path,
                       "item_key": item_key, "api_key": api_key,
                       "version": version}}


def _revert_zotero_attachment_commit(prepared, op):
    """Write side of a ``zotero_attachment_trash`` revert — ONE PATCH.

    Runs lock-free in the batch path (2026-09-02) and under the caller's lock
    on the single-op path; either way the ``If-Unmodified-Since-Version`` guard,
    not the lock, is what prevents a lost update.
    """
    from core.utils import log_vault_write

    c = prepared["commit"]
    try:
        zotero_write.set_item_deleted(c["base"], c["lib_path"], c["item_key"],
                                      False, c["version"], api_key=c["api_key"])
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroVersionConflict,
            zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "error", "message": str(exc)}
    log_vault_write("tagging_restore_zotero_attachment", c["item_key"],
                    str(op.get("filename") or ""))
    op["state"] = "reverted"
    return {"ok": True, "status": "reverted", "message": "Attachment restored."}


def _revert_zotero_attachment(vault_root, cfg, op):
    """Restore a trashed attachment — the exact inverse, ``deleted=0``.

    Composed prepare + commit, retained for the single-op restore route and for
    callers that hold no lock; the batch route calls the split pair directly so
    the GET/backoff and the PATCH both run outside the obsidian op-lock. Both
    paths are identical in outcome.

    Conservative in the same way as the tag reverts: an item that is no longer
    trashed (the user emptied the trash, or restored it in Zotero) is a SKIP,
    not a failure to retry forever; an unreachable Zotero is an ``error``, which
    keeps the op ``applied`` and therefore retryable.
    """
    prepared = _revert_zotero_attachment_prepare(cfg, op)
    if "early" in prepared:
        if prepared.get("state"):
            op["state"] = prepared["state"]
        return prepared["early"]
    return _revert_zotero_attachment_commit(prepared, op)


# --- record merge (annual cleanup T8, 2026-09-17) ---------------------------
#
# Three primitives the ``/api/zotrecord/merge`` route composes under ONE batch
# id, each with its own revert so Tagging → Restore undoes the whole merge
# newest-first: a CHILD re-parented onto the kept item, the retired PARENT
# trashed (never deleted — the key survives, so the un-trash is exact), and
# the kept Obsidian note gaining the retired citation key as an `aliases:`
# entry. Tags and the Better BibTeX alias in Extra reuse the existing
# ``zotero_tag`` and ``zotero_metadata_write`` writers. Every Zotero half is
# split prepare (lock-free read) / journal (under the op-lock) / patch
# (lock-free), the retire-tag discipline every writer in this file follows.

OP_CHILD_REPARENT = "zotero_child_reparent"
OP_ITEM_TRASH = "zotero_item_trash"
OP_NOTE_ALIASES = "note_aliases"


def zotero_child_reparent_journal(*, transport, lib_path, child, before_parent, after_parent,
                                  manifest, batch_id=None, citation_key=None, server_id=None):
    """Append (do NOT save) one ``zotero_child_reparent`` op. *child* is the
    child's data dict from :func:`zotero_write.get_item_children` (its own
    ``version`` is the guard the PATCH carries). ``before_parent`` is the whole
    restore mapping and lives inline."""
    op = {
        "op_id": journal.new_op_id(manifest), "kind": OP_CHILD_REPARENT, "ts": journal.now_iso(),
        "citation_key": citation_key, "item_key": str(child.get("key")),
        "child_type": child.get("itemType"), "link_mode": child.get("linkMode"),
        "title": child.get("title") or "",
        "before_parent": before_parent, "after_parent": after_parent,
        "lib_path": lib_path, "base_url": transport.base_url, "transport": transport.kind,
        "version_before": int(child.get("version") or 0), "server_id": server_id,
        "state": "outcome_unknown",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    return op


def zotero_child_reparent_patch(*, transport, lib_path, op):
    """The lock-free network half of one re-parent. ``(result, status, new_version | None)``."""
    from core.utils import log_vault_write
    from tagging.local_api import Version
    api_key = transport.api_key if transport.is_web else None
    # The guard must be instance-bound on the local transport (a bare int
    # carries no Zotero-Server-ID and the local write layer refuses it).
    version = Version(op["version_before"], op.get("server_id"))
    try:
        new_version = zotero_write.set_item_parent(
            transport.base_url, lib_path, op["item_key"], op["after_parent"],
            version, api_key=api_key)
    except zotero_write.ZoteroVersionConflict as exc:
        return {"ok": False, "status": "conflict", "message": str(exc)}, 409, None
    except zotero_write.ZoteroWriteUnsupported as exc:
        return {"ok": False, "status": "unsupported", "message": str(exc)}, 501, None
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "outcome_unknown", "message": str(exc)}, 502, None
    log_vault_write("tagging_zotero_child_reparent", op["item_key"],
                    f"{op['before_parent']} -> {op['after_parent']}")
    return ({"ok": True, "status": "reparented", "item_key": op["item_key"],
             "parent": op["after_parent"]}, 200, new_version)


def _revert_zotero_child_reparent_prepare(cfg, op):
    """Read side of a re-parent revert — OUTSIDE the op-lock. Conservative: the
    child must still hang off the parent this op gave it, or the revert refuses."""
    item_key, before = op.get("item_key"), op.get("before_parent")
    if not item_key or not before:
        return {"early": {"ok": False, "status": "invalid",
                          "message": "Op missing item_key or before_parent."}, "state": None}
    base = op.get("base_url") or _zotero_base(cfg)
    lib_path = op.get("lib_path") or "users/0"
    api_key = zotero_write.api_key_from_env() if op.get("transport") == "web" else None
    if op.get("transport") == "web" and not api_key:
        return {"early": {"ok": False, "status": "error",
                          "message": f"This child was re-parented via the Zotero web API; "
                                     f"set {zotero_write.API_KEY_ENV} to put it back."}, "state": None}
    from tagging.local_api import restore_kwargs
    try:
        data, version = zotero_write.get_item(base, lib_path, item_key, api_key=api_key, **restore_kwargs(op))
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "error", "message": "Zotero is not reachable."}, "state": None}
    except zotero_write.ZoteroError as exc:
        if "404" in str(exc):
            return {"early": {"ok": True, "status": "skipped", "message": "Child no longer exists in Zotero."},
                    "state": "reverted"}
        return {"early": {"ok": False, "status": "error", "message": str(exc)}, "state": None}
    current = str(data.get("parentItem") or "")
    if current == before:
        return {"early": {"ok": True, "status": "skipped", "message": "Already under the original parent."},
                "state": "reverted"}
    if current != str(op.get("after_parent") or ""):
        return {"early": {"ok": False, "status": "skipped",
                          "message": "The child was moved again after this merge; left as it is."},
                "state": None}
    return {"commit": {"base": base, "lib_path": lib_path, "item_key": item_key, "api_key": api_key,
                       "version": version, "parent": before}}


def _revert_zotero_child_reparent_commit(prepared, op):
    """Write side — ONE PATCH back to ``before_parent``."""
    from core.utils import log_vault_write
    c = prepared["commit"]
    try:
        zotero_write.set_item_parent(c["base"], c["lib_path"], c["item_key"], c["parent"],
                                     c["version"], api_key=c["api_key"])
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroVersionConflict, zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "error", "message": str(exc)}
    log_vault_write("tagging_restore_zotero_child_reparent", c["item_key"], c["parent"])
    op["state"] = "reverted"
    return {"ok": True, "status": "reverted", "message": "Child hangs off its original item again."}


def _revert_zotero_child_reparent(vault_root, cfg, op):
    prepared = _revert_zotero_child_reparent_prepare(cfg, op)
    if "early" in prepared:
        if prepared.get("state"):
            op["state"] = prepared["state"]
        return prepared["early"]
    return _revert_zotero_child_reparent_commit(prepared, op)


def zotero_item_trash_journal(*, transport, lib_path, item_key, version, data,
                              manifest, batch_id=None, citation_key=None):
    """Append (do NOT save) the ``zotero_item_trash`` op for a PARENT item.
    The key is the whole restore mapping (a trash keeps the key); ``title``
    and ``item_type`` ride for the Restore listing only."""
    op = {
        "op_id": journal.new_op_id(manifest), "kind": OP_ITEM_TRASH, "ts": journal.now_iso(),
        "citation_key": citation_key, "item_key": item_key,
        "item_type": data.get("itemType"), "title": data.get("title") or "",
        "lib_path": lib_path, "base_url": transport.base_url, "transport": transport.kind,
        "version_before": int(version), "server_id": getattr(version, "server_id", None),
        "state": "outcome_unknown",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    return op


def zotero_item_trash_patch(*, transport, lib_path, op, version):
    """The lock-free network half: ONE PATCH ``deleted: 1``, guarded by a FRESH
    *version* (the caller re-reads the item right before — re-parenting its
    children may have moved it). ``(result, status, new_version | None)``."""
    from core.utils import log_vault_write
    api_key = transport.api_key if transport.is_web else None
    try:
        new_version = zotero_write.set_item_deleted(
            transport.base_url, lib_path, op["item_key"], True, version, api_key=api_key)
    except zotero_write.ZoteroVersionConflict as exc:
        return {"ok": False, "status": "conflict", "message": str(exc)}, 409, None
    except zotero_write.ZoteroWriteUnsupported as exc:
        return {"ok": False, "status": "unsupported", "message": str(exc)}, 501, None
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "outcome_unknown", "message": str(exc)}, 502, None
    log_vault_write("tagging_zotero_item_trash", op["item_key"], op.get("title") or "")
    return {"ok": True, "status": "trashed", "item_key": op["item_key"]}, 200, new_version


def _revert_zotero_item_trash_prepare(cfg, op):
    """Read side of an item un-trash — OUTSIDE the op-lock."""
    item_key = op.get("item_key")
    if not item_key:
        return {"early": {"ok": False, "status": "invalid", "message": "Op missing item_key."}, "state": None}
    base = op.get("base_url") or _zotero_base(cfg)
    lib_path = op.get("lib_path") or "users/0"
    api_key = zotero_write.api_key_from_env() if op.get("transport") == "web" else None
    if op.get("transport") == "web" and not api_key:
        return {"early": {"ok": False, "status": "error",
                          "message": f"This item was trashed via the Zotero web API; "
                                     f"set {zotero_write.API_KEY_ENV} to restore it."}, "state": None}
    from tagging.local_api import restore_kwargs
    try:
        data, version = zotero_write.get_item(base, lib_path, item_key, api_key=api_key, **restore_kwargs(op))
    except zotero_write.ZoteroUnavailable:
        return {"early": {"ok": False, "status": "error", "message": "Zotero is not reachable."}, "state": None}
    except zotero_write.ZoteroError as exc:
        if "404" in str(exc):
            # The trash was emptied: nothing can bring the item back.
            return {"early": {"ok": True, "status": "skipped",
                              "message": "Item no longer exists in Zotero (trash emptied)."},
                    "state": "reverted"}
        return {"early": {"ok": False, "status": "error", "message": str(exc)}, "state": None}
    if not data.get("deleted"):
        return {"early": {"ok": True, "status": "skipped", "message": "Item is not in the trash."},
                "state": "reverted"}
    return {"commit": {"base": base, "lib_path": lib_path, "item_key": item_key, "api_key": api_key,
                       "version": version}}


def _revert_zotero_item_trash_commit(prepared, op):
    from core.utils import log_vault_write
    c = prepared["commit"]
    try:
        zotero_write.set_item_deleted(c["base"], c["lib_path"], c["item_key"], False, c["version"],
                                      api_key=c["api_key"])
    except (zotero_write.ZoteroUnavailable, zotero_write.ZoteroVersionConflict, zotero_write.ZoteroError) as exc:
        return {"ok": False, "status": "error", "message": str(exc)}
    log_vault_write("tagging_restore_zotero_item_trash", c["item_key"], op.get("title") or "")
    op["state"] = "reverted"
    return {"ok": True, "status": "reverted", "message": "Item restored from Zotero's trash."}


def _revert_zotero_item_trash(vault_root, cfg, op):
    prepared = _revert_zotero_item_trash_prepare(cfg, op)
    if "early" in prepared:
        if prepared.get("state"):
            op["state"] = prepared["state"]
        return prepared["early"]
    return _revert_zotero_item_trash_commit(prepared, op)


def _note_aliases_locked(*, rec, aliases, cfg, vault_root, citation_key,
                         manifest=None, batch_id=None, defer_save=False):
    """Add *aliases* to a record's note frontmatter. Caller holds the lock.

    The same snapshot → journal → atomic-write shape as
    :func:`_note_pdf_links_locked`, over the ``aliases`` key. Returns
    ``(result, status)``; an unrecognised shape is refused (422), never
    reformatted.
    """
    from api.routes._audit_tagwrite import add_aliases
    from core.utils import log_vault_write, write_text_atomic

    note_path = rec.obsidian_note.path
    try:
        rel = str(note_path.resolve().relative_to(vault_root.resolve()))
    except (OSError, ValueError):
        return {"error": "Note path escapes the vault root."}, 400
    try:
        raw_bytes = note_path.read_bytes()
        original = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": f"Cannot read note: {sanitise_error_msg(exc)}"}, 400
    cur_sha = hashlib.sha256(raw_bytes).hexdigest()
    new_text, added, reason = add_aliases(original, list(aliases))
    if new_text is None:
        return {"error": f"Cannot safely edit frontmatter: {reason}", "reason": reason}, 422
    if not added:
        return {"ok": True, "note": rel, "added": [], "changed": False,
                "content_sha256": cur_sha, "message": "Aliases already present."}, 200
    new_bytes = new_text.encode("utf-8")
    hash_after = hashlib.sha256(new_bytes).hexdigest()
    shared = manifest is not None
    try:
        if not shared:
            manifest = journal.load(vault_root, cfg)
        op_id = journal.new_op_id(manifest)
        snapshot_rel = journal.write_note_snapshot(vault_root, cfg, rel, op_id, raw_bytes)
    except journal.ScopeError as exc:
        return {"error": f"Tagging archive misconfigured: {exc}"}, 400
    op = {
        "op_id": op_id, "kind": OP_NOTE_ALIASES, "note_rel": rel, "citation_key": citation_key,
        "added": list(added), "hash_before": cur_sha, "hash_after": hash_after,
        "snapshot_rel": snapshot_rel, "ts": journal.now_iso(), "state": "applied",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    if not shared:
        journal.prune(vault_root, cfg, manifest, protect_op_ids={op_id})
    if not (shared and defer_save):
        journal.save(vault_root, cfg, manifest)
    log_vault_write("tagging_note_aliases", str(note_path), "+" + ", ".join(added))
    try:
        write_text_atomic(str(note_path), new_text)
    except OSError as exc:
        op["state"] = "failed"
        if not (shared and defer_save):
            journal.save(vault_root, cfg, manifest)
        return {"error": f"Write failed: {sanitise_error_msg(exc)}"}, 500
    return {"ok": True, "note": rel, "added": list(added), "changed": True, "op_id": op_id,
            "content_sha256": hash_after}, 200
