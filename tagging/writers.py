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
import logging
import os
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
                       manifest=None):
    """Union ``valid`` tags into a record's note frontmatter. Caller holds the lock.

    Returns ``(result_dict, http_status)``. Snapshots + journals the note before
    the atomic write so the op is reversible; honours an optional ``want_sha``
    stale-diff guard (409). ``rec.obsidian_note`` MUST be present (caller checks).

    ``manifest``: a caller-owned journal manifest (apply-all loads once and
    prunes/saves once for the whole fan-out); ``None`` = load/prune/save here,
    the standalone-endpoint behaviour. In both modes the op record is SAVED
    right after it is appended, before the target write runs (crash-trail).
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
    journal.save(vault_root, cfg, manifest)

    log_vault_write("tagging_note_tag_add", str(note_path),
                    f"+{len(added)} tags: {', '.join(added)}")
    try:
        write_text_atomic(str(note_path), new_text)
    except OSError as exc:
        op["state"] = "failed"
        journal.save(vault_root, cfg, manifest)
        return {"error": f"Write failed: {sanitise_error_msg(exc)}"}, 500

    return {
        "ok": True, "added": added, "rejected": sorted(set(rejected)),
        "note": rel, "citation_key": citation_key, "op_id": op_id,
        "content_sha256": hash_after,
    }, 200

def _finder_write_locked(*, rec, valid, rejected, cfg, vault_root, biblio_dir, citation_key,
                         manifest=None):
    """Union ``valid`` into each biblio PDF's Finder tags. Caller holds the lock.

    Returns ``(result_dict, http_status)``. macOS-only (caller gates); the
    before-lists ARE the restore snapshot (only the xattr changes). A PDF already
    carrying the tags is skipped. Returns 404 when the key has no biblio PDF.
    ``manifest`` as in :func:`_note_write_locked`.
    """
    from api.routes import _finder_tagwrite
    from core.utils import log_vault_write

    biblio_pdfs = [Path(p) for p in rec.pdf_paths if _is_under(Path(p), biblio_dir)]
    if not biblio_pdfs:
        return {"error": "No biblio_articles PDF for that citation key."}, 404

    changes: list[dict] = []
    read_errors: list[dict] = []
    for pdf in biblio_pdfs:
        try:
            pdf_rel = str(pdf.resolve().relative_to(vault_root.resolve()))
        except (OSError, ValueError):
            continue  # defence in depth — skip a PDF that escapes the vault
        if not _is_under(pdf.resolve(), biblio_dir.resolve()):
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

    if read_errors and not changes:
        return {"error": "Could not read the PDF's current Finder tags.",
                "pdfs": read_errors}, 500

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
    manifest["ops"].append(op)
    if not shared:
        # protect_op_ids: never evict the op THIS call just appended —
        # with a manifest full of un-evictable applied finder/zotero ops
        # the cap loop would drop it (and unlink its snapshot) before the
        # target write, 404-ing the undo the response hands back.
        journal.prune(vault_root, cfg, manifest,
                      protect_op_ids={op["op_id"]})
    journal.save(vault_root, cfg, manifest)

    results = [dict(e) for e in read_errors]  # unreadable PDFs surface per-PDF
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
        journal.save(vault_root, cfg, manifest)
        return {"error": "All Finder-tag writes failed.", "pdfs": results}, 500

    # Truth over optimism (2026-07-20 audit): `added` reports only tags that
    # actually LANDED on some PDF — the pre-fix union over all planned changes
    # claimed failed writes as applied, and apply-all (which keeps only the
    # summary fields, not `pdfs`) then reported a clean success to the deck.
    # The op record's summary is corrected too (it feeds the Restore UI and
    # the propose exemplars); its per-PDF before-lists stay complete, and the
    # revert is already per-PDF conservative about a write that never landed.
    partial = failed_writes > 0 or bool(read_errors)
    if sorted(ok_added) != op["added"]:
        op["added"] = sorted(ok_added)
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
    for ch in op.get("pdfs", []):
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
        lib_path = _zotero_lib_path(transport, zot)
        # Full (dict) read, not name-only: the PATCH below rewrites the item's
        # whole tag array, so the existing tags must round-trip verbatim or
        # their metadata (automatic/manual `type`, colours) is silently reset.
        version, before_raw = zotero_write.get_item_tags_full(
            base, lib_path, item_key, api_key=transport.api_key)
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

def _zotero_commit_locked(*, prepared, rejected, cfg, vault_root, citation_key,
                          manifest=None):
    """WRITE phase — caller holds the lock: journal, then the guarded PATCH.

    Only the journal mutation and the single version-guarded PATCH run under
    the op-lock (worst case one network call, not the read+write pair). 409 on
    a version conflict / 503 became-unreachable / 502 other.
    ``manifest`` as in :func:`_note_write_locked`.
    """
    from core.utils import log_vault_write

    base = prepared["base"]
    lib_path = prepared["lib_path"]
    item_key = prepared["item_key"]
    version = prepared["version"]
    before_raw = prepared["before_raw"]
    before = prepared["before"]
    added = prepared["added"]
    after = prepared["after"]
    after_payload = prepared["after_payload"]

    shared = manifest is not None
    try:
        if not shared:
            manifest = journal.load(vault_root, cfg)
        op_id = journal.new_op_id(manifest)
    except journal.ScopeError as exc:
        return {"error": f"Tagging archive misconfigured: {exc}"}, 400
    op = {
        "op_id": op_id, "kind": "zotero_tag", "citation_key": citation_key,
        "item_key": item_key, "lib_path": lib_path, "base_url": base,
        # `before`/`after` are NAME lists (manifest UI + the revert's
        # "unchanged since our write?" comparison); `before_raw` is the exact
        # dict array the revert PATCHes back so metadata survives the
        # round-trip. Ops written before 2026-07-18 lack before_raw — their
        # revert falls back to names (metadata was already lost at write time).
        "before": before, "before_raw": before_raw, "after": after, "added": added,
        # `transport` lets the revert re-derive whether an API key is needed.
        # The key ITSELF is never journalled — it is re-read from the
        # environment at revert time (provider-key discipline).
        "transport": prepared.get("transport") or "local",
        "version_before": version, "ts": journal.now_iso(), "state": "applied",
    }
    manifest["ops"].append(op)
    if not shared:
        # protect_op_ids: never evict the op THIS call just appended —
        # with a manifest full of un-evictable applied finder/zotero ops
        # the cap loop would drop it (and unlink its snapshot) before the
        # target write, 404-ing the undo the response hands back.
        journal.prune(vault_root, cfg, manifest,
                      protect_op_ids={op["op_id"]})
    journal.save(vault_root, cfg, manifest)

    try:
        new_version = zotero_write.set_item_tags(
            base, lib_path, item_key, after_payload, version,
            api_key=prepared.get("api_key"))
    except zotero_write.ZoteroVersionConflict:
        op["state"] = "failed"
        journal.save(vault_root, cfg, manifest)
        return {"error": "The item changed in Zotero; reload and retry."}, 409
    except zotero_write.ZoteroUnavailable as exc:
        op["state"] = "failed"
        journal.save(vault_root, cfg, manifest)
        return {"error": f"Zotero became unreachable: {sanitise_error_msg(exc)}"}, 503
    except zotero_write.ZoteroError as exc:
        op["state"] = "failed"
        journal.save(vault_root, cfg, manifest)
        return {"error": sanitise_error_msg(exc)}, 502
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
    try:
        cur_version, cur_tags = zotero_write.get_item_tags(
            base, lib_path, item_key, api_key=api_key)
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

def _prepare_tag_removal(data):
    """Shared validation for the tag-removal endpoints.

    Mirrors :func:`_prepare_tag_write` minus the vocabulary gate: returns
    ``(ctx, None)`` or ``(None, (response_dict, status))``. ``ctx.valid`` holds
    the cleaned removal targets; per-target presence ("is that tag actually on
    this note/PDF/item?") is each writer's own check so the fan-out can skip.
    """
    if not isinstance(data, dict):
        return None, ({"error": "Invalid JSON"}, 400)
    if coerce_bool(data.get("confirm")) is not True:
        return None, ({"error": "Tag removal must be explicitly confirmed (confirm: true)."}, 400)
    citation_key = coerce_non_empty_string(data.get("citation_key"), max_len=128)
    if not citation_key:
        return None, ({"error": "citation_key required"}, 400)
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
    rec = inv.records.get(citation_key)
    if rec is None:
        return None, ({"error": "Unknown citation key."}, 404)

    cfg = load_config()
    return {
        "rec": rec, "settings": settings, "cfg": cfg,
        "vault_root": settings.vault_root,
        "biblio_dir": settings.biblio_articles_dir, "valid": valid,
        "rejected": [], "citation_key": citation_key,
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
    this helper's own manifest saves — a retire across N records otherwise
    paid ~1-2 full fsync'd manifest rewrites PER record under the op-lock.
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
                          manifest=None, batch_id=None, defer_save=False):
    """Remove ``valid`` from each biblio PDF's Finder tags. Caller holds the lock.

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
        lib_path = _zotero_lib_path(transport, zot)
        version, before_raw = zotero_write.get_item_tags_full(
            base, lib_path, item_key, api_key=transport.api_key)
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
    PATCH keeps the crash trail conservative: an op whose PATCH never ran
    reverts by re-adding tags that were never removed — an idempotent no-op.
    """
    op = {
        "op_id": journal.new_op_id(manifest), "kind": "zotero_untag",
        "citation_key": citation_key,
        "item_key": prepared["item_key"], "lib_path": prepared["lib_path"],
        "base_url": prepared["base"],
        "before": prepared["before"], "before_raw": prepared["before_raw"],
        "after": prepared["after"], "removed": prepared["removed"],
        "transport": prepared.get("transport") or "local",
        "version_before": prepared["version"], "ts": journal.now_iso(),
        "state": "applied",
    }
    if batch_id:
        op["batch_id"] = batch_id
    manifest["ops"].append(op)
    return op


def _zotero_remove_patch(prepared):
    """The lock-free network half of a Zotero tag removal.

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
    except zotero_write.ZoteroUnavailable as exc:
        return {"error": f"Zotero became unreachable: {sanitise_error_msg(exc)}"}, 503, None
    except zotero_write.ZoteroError as exc:
        return {"error": sanitise_error_msg(exc)}, 502, None
    return {"ok": True}, 200, new_version


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
        op["state"] = "failed"
        journal.save(vault_root, cfg, manifest)
        return result, status
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
