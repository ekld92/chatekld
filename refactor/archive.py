"""Per-image archive: move a full-res original OUT of the vault + leave a thumbnail.

This is an **explicit, per-image** action (never part of the bulk callout apply).
For one image embedded by exactly one in-scope note it:

1. reads the original bytes (materializing an iCloud placeholder first);
2. runs a **vault-wide reference-safety check** — if any *other* note embeds the
   same file the move is refused (a shared image is flagged, never moved);
3. writes a small PNG thumbnail into the excluded ``<scope>/_thumbs/`` folder;
4. copies the original into the archive dir and verifies the copy;
5. swaps that note's embed to point at the thumbnail (atomic note write);
6. deletes the original from the vault.

Every step is journalled (``refactor.journal``) so an interrupted run is recoverable
and the whole thing is reversible (:func:`revert_archive_image`). All in-vault writes
are scope-locked; the only write outside the scope is the archive copy, which lands
outside the vault entirely.
"""
from __future__ import annotations

import io
import os
from pathlib import Path

from PIL import Image

from core.constants import VAULT_IMAGE_EXTS
from core.utils import write_bytes_atomic, write_text_atomic

from refactor import extract, journal
from refactor.resolver import build_name_index, excluded_dirs, scan_embeds
from refactor.result import sha256_bytes, sha256_text


# --- vault-wide reference safety -------------------------------------------

# Dirs the reference walk skips. Deliberately MINIMAL (only VCS/app internals):
# `.git` holds no user markdown embeds, `.obsidian` is config/plugins. Everything
# else — including the user's `vault_exclude_dirs` and `.trash` — IS scanned (see
# `other_referencing_notes`).
_REF_WALK_SKIP_DIRS = frozenset({".git", ".obsidian"})


def other_referencing_notes(
    vault_root: Path,
    image_rel: str,
    note_rel: str,
    name_index: dict,
) -> list[str]:
    """Vault-relative notes **other than** *note_rel* that embed/link *image_rel*.

    The move-safety gate: a non-empty result means the image is shared and must
    NOT be archived. Walks every ``.md`` in the vault but cheaply prunes any note
    whose text does not even contain the image's basename before doing the full
    (resolver-accurate) embed scan, so the common case is fast.

    SAFETY — this walk is intentionally **more conservative than the planner's
    view**: archiving physically *moves* the file out of the vault, so an embed in
    ANY note breaks, including notes inside the user's ``vault_exclude_dirs`` or
    ``.trash`` (which the analyzer would skip). Excluding a folder from *indexing*
    must never weaken *move-safety*, so this gate does NOT apply ``vault_exclude_dirs``
    — it scans every ``.md`` except ``.git``/``.obsidian`` (which structurally hold
    no user image embeds). The shared resolver ``name_index`` still maps bare
    ``![[img.png]]`` to the central attachment, so references from excluded notes
    resolve correctly here even though those notes are not indexed.
    """
    basename = os.path.basename(image_rel)
    bn_lower = basename.lower()
    out: list[str] = []
    for p in vault_root.rglob("*.md"):
        if not p.is_file():
            continue
        if any(part in _REF_WALK_SKIP_DIRS for part in p.parts):
            continue
        try:
            rel = p.relative_to(vault_root).as_posix()
        except ValueError:
            continue
        if rel == note_rel:          # the note being edited is allowed to embed it
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if bn_lower not in text.lower():
            continue  # cheap prune: the file can't reference it
        for occ in scan_embeds(text, p, vault_root, name_index):
            if occ["rel_path"] == image_rel:
                out.append(rel)
                break
    return out


# --- thumbnail -------------------------------------------------------------

def make_thumbnail(img_bytes: bytes, max_side: int) -> bytes:
    """Return PNG bytes of *img_bytes* downscaled so its longest side ≤ *max_side*.

    Always re-encodes to PNG (so the ``<stem>.png`` thumbnail name is honest even
    for a JPEG source) and always produces a small image. Mirrors the downscale
    approach in ``services.vision._downscale_base64_png`` but is byte-oriented and
    PNG-guaranteed.

    Raises (``UnidentifiedImageError`` / ``OSError`` / ``ValueError``) on any source
    PIL cannot decode: HEIC without ``pillow-heif``, and the **vector/icon extensions
    that are in ``VAULT_IMAGE_EXTS`` but Pillow does not rasterise** — ``.svg`` and
    typically ``.ico`` / ``.img``. ``archive_image`` catches this and refuses the
    archive with a clean "could not build thumbnail" message (a graceful no-op, not
    a crash): such images simply cannot be archived until a thumbnail can be made.
    """
    with Image.open(io.BytesIO(img_bytes)) as im:
        im.load()
        w, h = im.size
        longest = max(w, h)
        if longest > max_side and longest > 0:
            scale = max_side / float(longest)
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        # PNG can't save every PIL mode (e.g. CMYK); normalise the exotic ones.
        if im.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            im = im.convert("RGBA")
        out = io.BytesIO()
        im.save(out, format="PNG")
        return out.getvalue()


# --- embed rewrite ---------------------------------------------------------

def _swap_embeds(text: str, note_path: Path, vault_root: Path, name_index: dict,
                 image_rel: str, thumb_link: str) -> tuple[str, int, bool]:
    """Replace every embed of *image_rel* in *text* with ``![](thumb_link)``.

    Returns ``(new_text, n_replaced, has_plain_link)``. ``has_plain_link`` is True
    if the image is referenced by a non-embed markdown link (``[lbl](img)`` with no
    leading ``!``) — the caller refuses to archive in that case rather than turn a
    link into an embed.

    Span handling (mirrors ``resolver.scan_embeds``' two regexes):
      * The **wikilink** regex is ``!?\\[\\[…\\]\\]`` — its ``!?`` is greedy, so an
        embed ``![[x]]`` has the ``!`` *inside* the match span (``text[start] == "!"``)
        while a plain link ``[[x]]`` starts at ``[``.
      * The **inline** regex is ``\\[…\\]\\(…\\)`` — it never includes a leading
        ``!``, so an embed ``![](x)`` matches starting at ``[`` with the ``!`` sitting
        at ``start - 1``; a plain link ``[lbl](x)`` has no ``!`` there.
    So an occurrence is an embed iff ``text[start] == "!"`` OR ``text[start-1] == "!"``;
    in the inline-embed case we fold that preceding ``!`` into the replaced span.
    Replacements are spliced right-to-left so earlier spans stay valid.

    DELIBERATE FIDELITY CHOICE: the replacement is the canonical empty-alt embed
    ``![](thumb_link)`` (the §7 target shape) — any original alt text ``![alt](x)``
    or Obsidian wikilink alias/size ``![[x|alias]]`` is **not** preserved. This is
    intentional: parsing/re-emitting arbitrary alt/alias text inside a *destructive*
    note rewrite is more risk (a parser bug corrupts the note) than value for the
    target corpus (all embeds are bare ``![](UUID.png)``). If alt/alias preservation
    is ever needed, do it here with explicit per-occurrence capture + tests.
    """
    repls: list[tuple[int, int]] = []   # (start, end) spans to replace
    has_plain_link = False
    for occ in scan_embeds(text, note_path, vault_root, name_index):
        if occ["rel_path"] != image_rel:
            continue
        start, end = occ["start"], occ["end"]
        if text[start] == "!":
            # wikilink-form embed: the regex span already covers the "!".
            repls.append((start, end))
        elif start > 0 and text[start - 1] == "!":
            # inline-form embed "![](x)": fold the preceding "!" into the span.
            repls.append((start - 1, end))
        else:
            has_plain_link = True  # plain "[lbl](x)" link — not an embed
    new_text = text
    for start, end in sorted(repls, reverse=True):
        new_text = new_text[:start] + f"![]({thumb_link})" + new_text[end:]
    return new_text, len(repls), has_plain_link


def _note_relative(thumb_abs: Path, note_path: Path) -> str:
    """POSIX markdown link from *note_path*'s folder to *thumb_abs*."""
    return os.path.relpath(thumb_abs, note_path.parent).replace(os.sep, "/")


def _unique_dest(path: Path, suffix_seed: str) -> Path:
    """Return *path* if free, else ``<stem>-<seed8><ext>`` (deterministic)."""
    if not path.exists():
        return path
    return path.with_name(f"{path.stem}-{suffix_seed[:8]}{path.suffix}")


def _rollback_artifacts(manifest: dict, op: dict, *paths: Path) -> None:
    """Undo a partial archive after a pre-write abort: delete the durable files
    we wrote (thumbnail + archive copy) and drop the just-appended ``op`` from the
    manifest so the vault + manifest are left exactly as we found them.

    Safe because the caller holds the obsidian op-lock (single writer) and ``op``
    is the op it appended moments earlier and nothing else references — popping it
    cannot disturb another operation. Caller persists the manifest afterward.
    """
    for p in paths:
        try:
            if p is not None and Path(p).exists():
                os.unlink(p)
        except OSError:
            pass
    try:
        manifest["ops"].remove(op)
    except ValueError:
        pass


# --- archive ---------------------------------------------------------------

def archive_image(vault_root: Path, cfg: dict, scope: str, note_rel: str,
                  image_rel: str, content_sha256: str) -> dict:
    """Archive one image referenced by exactly one in-scope note.

    Caller holds the obsidian operation lock and has scope-validated *note_rel*
    (a ``.md`` under *scope*) and *image_rel* (an image under the vault root).
    Returns a result dict; on the shared/refused paths it makes **no** writes.
    """
    vault_root = Path(vault_root)
    res = {"ok": False, "status": "failed", "message": "", "shared": False}

    note_path = vault_root / note_rel
    try:
        raw_note = note_path.read_bytes()
    except OSError as exc:
        res["message"] = f"unreadable note ({type(exc).__name__})"
        return res
    if sha256_bytes(raw_note) != content_sha256:
        res["status"] = "skipped"
        res["message"] = "stale: note changed on disk since the plan ran"
        return res
    try:
        text = raw_note.decode("utf-8")
    except UnicodeDecodeError:
        res["status"] = "skipped"
        res["message"] = "note is not valid UTF-8; refusing to rewrite"
        return res

    if os.path.splitext(image_rel)[1].lower() not in VAULT_IMAGE_EXTS:
        res["message"] = "not an image path"
        return res

    # Read original bytes (materialises an iCloud placeholder) + enforce the cap.
    try:
        data, digest = extract._read_image(image_rel, vault_root)
    except (OSError, ValueError) as exc:
        res["message"] = f"cannot read image ({exc})"
        return res

    excluded = excluded_dirs(vault_root)
    name_index = build_name_index(vault_root, excluded)

    # This note must actually embed the image, and no OTHER note may reference it.
    _preview, n_here, has_plain = _swap_embeds(
        text, note_path, vault_root, name_index, image_rel, "PLACEHOLDER")
    if n_here == 0:
        res["message"] = ("note does not embed that image"
                          if not has_plain else "image is only plain-linked, not embedded")
        return res
    if has_plain:
        res["message"] = "note also plain-links the image; refusing to rewrite a link into an embed"
        return res
    others = other_referencing_notes(vault_root, image_rel, note_rel, name_index)
    if others:
        res["status"] = "shared"
        res["shared"] = True
        res["others"] = others[:20]
        res["message"] = f"shared image — also referenced by {len(others)} other note(s); not moved"
        return res

    manifest = journal.load(vault_root, cfg)
    op_id = journal.new_op_id(manifest)

    # 1) thumbnail → <scope>/_thumbs/<stem>.png (PNG, scope-locked).
    try:
        thumb_bytes = make_thumbnail(data, int(cfg.get("refactor_thumb_max_side") or 384))
    except Exception as exc:  # noqa: BLE001 — undecodable source (e.g. HEIC)
        res["message"] = f"could not build thumbnail ({type(exc).__name__})"
        return res
    tdir = journal.thumb_dir(vault_root, scope)
    thumb_abs = _unique_dest(tdir / (Path(image_rel).stem + ".png"), digest)
    try:
        journal.assert_under(thumb_abs.parent, vault_root / scope)
    except journal.ScopeError as exc:
        res["message"] = str(exc)
        return res

    # Keep the indexer out of _thumbs/ (idempotent) BEFORE writing into it.
    _ensure_thumbs_excluded(vault_root, scope, cfg)
    write_bytes_atomic(str(thumb_abs), thumb_bytes)
    thumb_rel = thumb_abs.relative_to(vault_root).as_posix()
    journal.log_vault_write("write_thumb", thumb_rel, f"{len(thumb_bytes)}B")

    # 2) copy original → <archive_dir>/attachments/<image_rel> + verify.
    arch_root = journal.archive_dir(vault_root, cfg)
    arch_abs = _unique_dest(arch_root / "attachments" / image_rel, digest)
    journal.assert_under(arch_abs, arch_root)
    write_bytes_atomic(str(arch_abs), data)
    if sha256_bytes(Path(arch_abs).read_bytes()) != digest:
        res["message"] = "archive copy failed verification; original left in place"
        # Nothing was journalled and the original is untouched; tidy the failed
        # copy AND the now-orphan thumbnail so a retry starts clean.
        for stray in (arch_abs, thumb_abs):
            try:
                os.unlink(stray)
            except OSError:
                pass
        return res
    archive_rel = arch_abs.relative_to(arch_root).as_posix()

    # 3) record the op (state "copied": durable artifacts written, note not yet swapped).
    op = {
        "id": op_id,
        "kind": "archive_image",
        "ts": journal.now_iso(),
        "note_rel": note_rel,
        "scope": scope,
        "image_rel": image_rel,
        "archive_rel": archive_rel,
        "thumb_rel": thumb_rel,
        "digest": digest,
        "size": len(data),
        "note_hash_before": content_sha256,
        "note_hash_after": "",
        "note_snapshot_rel": "",
        "original_deleted": False,
        "state": "copied",
    }
    manifest["ops"].append(op)
    journal.save(vault_root, cfg, manifest)

    # 4) Re-verify the note BEFORE the destructive write. The thumbnail/copy work
    #    above (esp. make_thumbnail decoding a large image) is a window in which an
    #    external editor could change the note — and `text` (read at the top) would
    #    then be stale, so writing `new_text` would clobber those edits and we'd
    #    delete a file the edited note may now reference differently. The obsidian
    #    op-lock blocks the indexer, so this only guards a concurrent human edit, but
    #    the destructive path must not race it. On mismatch, ROLL BACK the durable
    #    artifacts (thumbnail + archive copy) and drop the just-appended op so the
    #    vault + manifest are left exactly as we found them, then refuse.
    try:
        raw_now = note_path.read_bytes()
    except OSError as exc:
        _rollback_artifacts(manifest, op, thumb_abs, arch_abs)
        journal.save(vault_root, cfg, manifest)
        res["message"] = f"note became unreadable ({type(exc).__name__}); aborted (no changes made)"
        return res
    if sha256_bytes(raw_now) != content_sha256:
        _rollback_artifacts(manifest, op, thumb_abs, arch_abs)
        journal.save(vault_root, cfg, manifest)
        res["status"] = "skipped"
        res["message"] = "note changed during archiving; aborted (no changes made)"
        return res

    # 5) swap the embed(s) → atomic note write (journal-before-write). `text` is
    #    valid: raw_now == content_sha256 == the bytes `text` was decoded from.
    thumb_link = _note_relative(thumb_abs, note_path)
    new_text, _n, _pl = _swap_embeds(text, note_path, vault_root, name_index, image_rel, thumb_link)
    snapshot_rel = journal.write_note_snapshot(vault_root, cfg, note_rel, op_id, raw_note)
    op["note_snapshot_rel"] = snapshot_rel
    op["note_hash_after"] = sha256_text(new_text)
    journal.save(vault_root, cfg, manifest)
    journal.assert_under(note_path, vault_root / scope)
    write_text_atomic(str(note_path), new_text)
    journal.log_vault_write("write_note", note_rel, "embed→thumbnail")

    # 6) delete the original (the destructive step, last + non-fatal). assert_under
    #    is defence-in-depth over the route's `_resolve_image_rel` validation — the
    #    unlink target must provably stay under the vault root.
    try:
        orig_abs = journal.assert_under(vault_root / image_rel, vault_root)
        os.unlink(orig_abs)
        op["original_deleted"] = True
        journal.log_vault_write("move_out", image_rel, f"→ {archive_rel}")
    except (OSError, journal.ScopeError) as exc:
        # Archive copy is durable + the note no longer embeds the original, so a
        # lingering (now-unreferenced) original is harmless; report a warning.
        op["original_deleted"] = False
        res["warning"] = f"original could not be deleted ({type(exc).__name__}); it is now unreferenced"
    op["state"] = "applied"
    journal.save(vault_root, cfg, manifest)

    res.update({
        "ok": True, "status": "archived", "op_id": op_id,
        "thumb_rel": thumb_rel, "archive_rel": archive_rel,
        "note_body": new_text, "note_hash_after": op["note_hash_after"],
        "message": "image archived; embed now points at the thumbnail",
    })
    return res


def _ensure_thumbs_excluded(vault_root: Path, scope: str, cfg: dict) -> None:
    """Add ``<scope>/_thumbs`` to ``vault_exclude_dirs`` (idempotent persist).

    So the indexer never describes the thumbnails the archiver writes. Done via
    ``core.config.save_config`` (a stat-cached read + atomic write). Best-effort:
    a persist failure does not abort the archive (the embed is note-relative and
    still resolves; the only cost is the indexer might describe the tiny PNG).
    """
    from core.config import load_config, save_config
    from core.constants import REFACTOR_THUMBS_DIRNAME
    rel = f"{scope}/{REFACTOR_THUMBS_DIRNAME}"
    try:
        live = load_config()
        excl = list(live.get("vault_exclude_dirs", []) or [])
        if rel in excl:
            return
        excl.append(rel)
        live["vault_exclude_dirs"] = excl
        save_config(live)
    except Exception:  # noqa: BLE001 — non-fatal
        pass


# --- restore ---------------------------------------------------------------

def revert_archive_image(vault_root: Path, cfg: dict, op: dict) -> dict:
    """Reverse one archive op — **atomic-or-nothing**.

    A *partial* revert is dangerous: e.g. moving the original back and deleting the
    thumbnail while the note still embeds that thumbnail would leave a BROKEN EMBED;
    and blindly writing the original back could clobber a *different* file the user
    re-created at that path. So this runs every conflict check FIRST and, if any
    would force an inconsistent or destructive outcome, refuses the whole op WITHOUT
    touching anything (mirroring ``revert_apply_note``). Only when all checks pass
    does it mutate, in the safe order: restore the original file → restore the note
    body → drop the thumbnail → remove the archive copy. By the time the thumbnail
    is deleted the note is guaranteed to embed the *original* again (restored,
    already-original, or gone), so the deletion can never orphan a live embed.
    Mutates ``op['state']``; caller persists the manifest.
    """
    vault_root = Path(vault_root)
    arch_root = journal.archive_dir(vault_root, cfg)
    note_rel = op.get("note_rel", "")
    note_path = vault_root / note_rel
    image_rel = op.get("image_rel", "")
    arch_src = arch_root / op.get("archive_rel", "")
    thumb_rel = op.get("thumb_rel", "")
    snap_rel = op.get("note_snapshot_rel", "")

    # --- PRE-FLIGHT (no writes): decide what we WILL do, or refuse outright. ---

    # (a) Note conflict. Decide whether to restore the note body. If the note was
    #     edited after archiving and is neither the body we wrote nor already back
    #     at its pre-archive form, restoring would clobber those edits — and the
    #     note still embeds the thumbnail we are about to delete, so half-reverting
    #     would break that embed. Refuse the whole op. (A deleted note ⇒ skip the
    #     body restore but still reclaim the original/thumbnail.)
    restore_note = False
    if snap_rel and note_path.exists():
        try:
            cur_hash = sha256_bytes(note_path.read_bytes())
        except OSError as exc:
            return {"ok": False, "status": "failed",
                    "message": f"unreadable note ({type(exc).__name__})"}
        if cur_hash == op.get("note_hash_after"):
            restore_note = True            # still the body we wrote → safe to revert
        elif cur_hash == op.get("note_hash_before"):
            restore_note = False           # already pre-archive (embeds original) → nothing to do
        else:
            return {"ok": False, "status": "skipped",
                    "message": "Note changed since archiving; not reverting (would break the "
                               "thumbnail embed or clobber later edits). Resolve it by hand first."}

    # (b) Original-path conflict. If we deleted the original and something now
    #     occupies that path which is NOT our archived bytes, refuse rather than
    #     overwrite a file the user may have created there meanwhile.
    move_original = False
    if op.get("original_deleted"):
        dest = vault_root / image_rel
        if not dest.exists():
            move_original = True
            if not Path(arch_src).exists():       # nothing to move back
                return {"ok": False, "status": "failed",
                        "message": "Archived original is missing; cannot restore."}
        else:
            try:
                already = sha256_bytes(dest.read_bytes()) == op.get("digest")
            except OSError as exc:
                return {"ok": False, "status": "failed",
                        "message": f"cannot inspect the original path ({type(exc).__name__})"}
            if not already:
                return {"ok": False, "status": "skipped",
                        "message": "A different file now occupies the original path; not "
                                   "overwriting it. Move it aside, then restore."}
            move_original = False                 # identical bytes already there → idempotent skip

    # --- MUTATE (all checks passed; safe order) -----------------------------
    warnings: list[str] = []

    # 1) original back to its vault path.
    if move_original:
        try:
            journal.assert_under(arch_src, arch_root)
            data = Path(arch_src).read_bytes()
            dest = vault_root / image_rel
            journal.assert_under(dest, vault_root)
            write_bytes_atomic(str(dest), data)
            journal.log_vault_write("restore_image", image_rel, f"← {op.get('archive_rel','')}")
        except (OSError, journal.ScopeError) as exc:
            return {"ok": False, "status": "failed",
                    "message": f"could not restore original ({type(exc).__name__})"}

    # 2) note body back from snapshot (re-embeds the original).
    if restore_note:
        try:
            snap = journal.read_snapshot(vault_root, cfg, snap_rel)
            journal.assert_under(note_path, vault_root)
            write_bytes_atomic(str(note_path), snap)
            journal.log_vault_write("restore_note", note_rel, "thumbnail→embed")
        except (OSError, journal.ScopeError) as exc:
            # The original is already back; surface a warning but don't fail hard.
            warnings.append(f"note not restored ({type(exc).__name__})")

    # 3) drop the thumbnail (best-effort). Safe: by here the note embeds the
    #    original again (restored / already-original / deleted), never the thumb.
    if thumb_rel:
        try:
            tp = vault_root / thumb_rel
            journal.assert_under(tp, vault_root)
            if tp.exists():
                os.unlink(tp)
        except (OSError, journal.ScopeError):
            warnings.append("thumbnail not removed")

    # 4) remove the now-redundant archive copy (best-effort).
    try:
        if Path(arch_src).exists():
            os.unlink(arch_src)
    except OSError:
        pass

    op["state"] = "reverted"
    op["reverted_ts"] = journal.now_iso()
    msg = "image restored to the vault"
    if warnings:
        msg += " (" + "; ".join(warnings) + ")"
    return {"ok": True, "status": "reverted", "message": msg}
