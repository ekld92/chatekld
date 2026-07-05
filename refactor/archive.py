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
import threading
from pathlib import Path

from typing import Callable, Optional

from PIL import Image

from core.constants import VAULT_IMAGE_EXTS
from core.utils import write_bytes_atomic, write_text_atomic

from refactor import extract, journal
from refactor.resolver import (
    build_name_index,
    excluded_dirs,
    link_target_basenames,
    scan_embeds,
)
from refactor.result import sha256_bytes, sha256_text


# --- vault-wide reference safety -------------------------------------------

# Dirs the reference walk skips. Deliberately MINIMAL (only VCS/app internals):
# `.git` holds no user markdown embeds, `.obsidian` is config/plugins. Everything
# else — including the user's `vault_exclude_dirs` and `.trash` — IS scanned (see
# `other_referencing_notes`).
_REF_WALK_SKIP_DIRS = frozenset({".git", ".obsidian"})

# --- reference-sweep index (Track 5.3, 2026-07-04) ---------------------------
# DEFECT: every archive click full-read every `.md` AND `.canvas` in the vault
# (tens of thousands of files on a large vault, worst on iCloud where a read
# can trigger a download) just to prove the image is not referenced elsewhere —
# and a user archiving N images from one note paid that whole-vault read N times.
# FIX: a per-session reverse-reference index. Each sweep stat-walks the tree
# (cheap: directory listings + one stat per file, no reads), re-reads ONLY files
# whose (size, mtime_ns) signature changed since the last sweep, and stores per
# `.md` file the set of link-target basenames (`resolver.link_target_basenames`)
# / per `.canvas` file its lowered text. Candidates — files whose stored data
# mentions the image's basename — are then re-read fresh and verified with the
# full resolver-accurate scan, exactly as before.
# SAFETY (this gate refuses a destructive move, so it must never under-report):
# the index can only serve data validated against the file's CURRENT
# (size, mtime_ns) in THIS sweep — a changed/new file is always re-read, a
# deleted file's entry is dropped because the sweep rebuilds the root's map from
# what it actually walked. The candidate set is a provable superset of anything
# the verify scan could resolve (see link_target_basenames' docstring); indeed
# it is *stricter* than the old whole-text substring prune, which missed
# percent-encoded targets (`![](Fig%201.png)`) that the resolver decodes — the
# index catches those, so a genuinely shared image is now refused in a case the
# old sweep let through. The remaining read-then-move TOCTOU window is the same
# one the old full-read sweep had. mtime_ns is nanosecond-resolution on APFS; a
# same-size same-mtime_ns content swap is not achievable by normal editing.
# INVARIANT (pinned by test_refactor.py::test_ref_sweep_index_*): a sweep's
# result equals a from-scratch full-read sweep's result for the current on-disk
# state — the index is a pure read-amplification optimization.
_REF_INDEX_MAX_ROOTS = 2
_ref_index_lock = threading.Lock()
# {vault_root_str: {"md": {rel: (size, mtime_ns, frozenset_basenames)},
#                   "canvas": {rel: (size, mtime_ns, lowered_text)}}}
_ref_index: dict[str, dict] = {}


def clear_ref_index() -> None:
    """Drop the reverse-reference index (tests / explicit resets)."""
    with _ref_index_lock:
        _ref_index.clear()


def _walk_ref_files(vault_root: Path, heartbeat: Optional[Callable[[], None]]):
    """Yield ``(rel, kind, size, mtime_ns)`` for every ``.md``/``.canvas`` file.

    One pruned ``os.scandir`` walk (no reads): ``_REF_WALK_SKIP_DIRS`` are never
    descended (the old ``rglob`` listed their contents and part-checked each
    path). Symlinked dirs ARE followed (a symlinked notes folder may hold real
    referencers) with a realpath cycle guard; symlinked files are included via
    a follow-symlinks stat — both match what the old ``rglob``+``is_file()``
    walk saw on Python ≤3.12. Heartbeats every 200 entries so a huge vault
    cannot let the caller's op-lock TTL lapse mid-sweep.
    """
    seen_dirs = {os.path.realpath(str(vault_root))}
    stack = [(str(vault_root), "")]
    seen_entries = 0
    while stack:
        dir_abs, dir_rel = stack.pop()
        try:
            entries = os.scandir(dir_abs)
        except OSError:
            continue
        with entries:
            for entry in entries:
                seen_entries += 1
                if heartbeat is not None and seen_entries % 200 == 0:
                    heartbeat()
                rel = f"{dir_rel}{entry.name}"
                try:
                    if entry.is_dir(follow_symlinks=True):
                        if entry.name in _REF_WALK_SKIP_DIRS:
                            continue
                        real = os.path.realpath(entry.path)
                        if real in seen_dirs:
                            continue  # symlink cycle / duplicate route
                        seen_dirs.add(real)
                        stack.append((entry.path, rel + "/"))
                        continue
                    if not entry.name.endswith((".md", ".canvas")):
                        continue
                    if not entry.is_file(follow_symlinks=True):
                        continue
                    st = entry.stat(follow_symlinks=True)
                except OSError:
                    continue
                kind = "md" if entry.name.endswith(".md") else "canvas"
                yield rel, kind, st.st_size, st.st_mtime_ns


def other_referencing_notes(
    vault_root: Path,
    image_rel: str,
    note_rel: str,
    name_index: dict,
    heartbeat: Optional[Callable[[], None]] = None,
) -> list[str]:
    """Vault-relative notes **other than** *note_rel* that embed/link *image_rel*.

    The move-safety gate: a non-empty result means the image is shared and must
    NOT be archived. Served through the reference-sweep index (see the Track 5.3
    notes above): a stat-walk validates/refreshes the per-file index, candidate
    notes are located by the image's basename in their stored link targets, and
    only those candidates get the full fresh-read resolver-accurate embed scan.

    SAFETY — this sweep is intentionally **more conservative than the planner's
    view**: archiving physically *moves* the file out of the vault, so an embed in
    ANY note breaks, including notes inside the user's ``vault_exclude_dirs`` or
    ``.trash`` (which the analyzer would skip). Excluding a folder from *indexing*
    must never weaken *move-safety*, so this gate does NOT apply ``vault_exclude_dirs``
    — it sweeps every ``.md`` except ``.git``/``.obsidian`` (which structurally hold
    no user image embeds). The shared resolver ``name_index`` still maps bare
    ``![[img.png]]`` to the central attachment, so references from excluded notes
    resolve correctly here even though those notes are not indexed.
    """
    basename = os.path.basename(image_rel)
    bn_lower = basename.lower()
    root_key = str(vault_root)

    # Sweep: stat-walk the tree, reuse signature-matched index entries, re-read
    # only new/changed files. The fresh maps are rebuilt from what this walk
    # actually saw, so deleted files drop out naturally.
    with _ref_index_lock:
        prev = _ref_index.get(root_key) or {"md": {}, "canvas": {}}
    fresh_md: dict = {}
    fresh_canvas: dict = {}
    for rel, kind, size, mtime_ns in _walk_ref_files(vault_root, heartbeat):
        bucket = fresh_md if kind == "md" else fresh_canvas
        cur = prev[kind].get(rel)
        if cur is not None and cur[0] == size and cur[1] == mtime_ns:
            bucket[rel] = cur
            continue
        try:
            text = (vault_root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if kind == "md":
            bucket[rel] = (size, mtime_ns, link_target_basenames(text))
        else:
            bucket[rel] = (size, mtime_ns, text.lower())
    with _ref_index_lock:
        _ref_index.pop(root_key, None)
        _ref_index[root_key] = {"md": fresh_md, "canvas": fresh_canvas}
        while len(_ref_index) > _REF_INDEX_MAX_ROOTS:
            _ref_index.pop(next(iter(_ref_index)))

    out: list[str] = []
    # Verify pass on candidates only: a fresh read + the same resolver-accurate
    # scan the old whole-vault sweep ran, so the decision bytes are current.
    for rel, (_size, _mt, basenames) in fresh_md.items():
        if rel == note_rel:          # the note being edited is allowed to embed it
            continue
        if bn_lower not in basenames:
            continue
        p = vault_root / rel
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for occ in scan_embeds(text, p, vault_root, name_index):
            if occ["rel_path"] == image_rel:
                out.append(rel)
                break

    # Obsidian .canvas files are JSON (not markdown), so the resolver's embed scan
    # does not apply — but a canvas node can embed the image, and moving the file
    # out would break that canvas. The canvas stores the vault-relative path as a
    # JSON string, so a conservative basename substring match (over the lowered
    # text this sweep validated) is enough to REFUSE the move. A false positive
    # only makes archiving more cautious (never less safe), which is the right
    # bias for a destructive move.
    for rel, (_size, _mt, lowered) in fresh_canvas.items():
        if bn_lower in lowered:
            out.append(rel)
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
        # resize()/convert() each allocate a NEW PIL image; the `with` only closes
        # the ORIGINAL opened image, so track the derived ones and close them in a
        # finally (they would otherwise linger until GC). Low-frequency path, but
        # keeps the close-discipline consistent with vision._downscale_base64_png.
        cur = im
        derived: list = []
        if longest > max_side and longest > 0:
            scale = max_side / float(longest)
            cur = cur.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            derived.append(cur)
        # PNG can't save every PIL mode (e.g. CMYK); normalise the exotic ones.
        if cur.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            cur = cur.convert("RGBA")
            derived.append(cur)
        try:
            out = io.BytesIO()
            cur.save(out, format="PNG")
            return out.getvalue()
        finally:
            for d in derived:
                try:
                    d.close()
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass


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


def _unlink_and_audit(path, vault_root: Path, detail: str) -> None:
    """``os.unlink`` *path* (best-effort); if it was an IN-VAULT file that is now
    gone, emit ``log_vault_write("delete_thumb", …)`` so the removal is
    attributable even if the restore manifest is lost (2026-07-05 audit m4 — the
    thumbnail WRITE was already logged, the DELETE was not). Out-of-vault paths
    (the archive copy — ``archive_dir`` is refused inside the vault) unlink
    silently: vault-write auditing covers vault mutations only. All callers are
    cleanup/rollback/restore paths, so a failed unlink is swallowed.
    """
    p = Path(path)
    try:
        rel = p.resolve().relative_to(Path(vault_root).resolve()).as_posix()
    except ValueError:
        rel = None  # not under the vault (archive copy) — unlink without an audit line
    try:
        if p.exists():
            os.unlink(p)
            if rel is not None:
                journal.log_vault_write("delete_thumb", rel, detail)
    except OSError:
        pass


def _rollback_artifacts(manifest: dict, op: dict, vault_root: Path, *paths: Path) -> None:
    """Undo a partial archive after a pre-write abort: delete the durable files
    we wrote (thumbnail + archive copy) and drop the just-appended ``op`` from the
    manifest so the vault + manifest are left exactly as we found them.

    Safe because the caller holds the obsidian op-lock (single writer) and ``op``
    is the op it appended moments earlier and nothing else references — popping it
    cannot disturb another operation. Caller persists the manifest afterward. The
    in-vault thumbnail deletion is audited (m4); the archive copy is out of scope.
    """
    for p in paths:
        if p is not None:
            _unlink_and_audit(p, vault_root, "archive rollback")
    try:
        manifest["ops"].remove(op)
    except ValueError:
        pass


# --- archive ---------------------------------------------------------------

def archive_image(vault_root: Path, cfg: dict, scope: str, note_rel: str,
                  image_rel: str, content_sha256: str,
                  heartbeat: Optional[Callable[[], None]] = None) -> dict:
    """Archive one image referenced by exactly one in-scope note.

    Caller holds the obsidian operation lock and has scope-validated *note_rel*
    (a ``.md`` under *scope*) and *image_rel* (an image under the vault root).
    Returns a result dict; on the shared/refused paths it makes **no** writes.

    *heartbeat* (optional) is forwarded to the whole-vault reference walk so a
    large vault cannot let the caller's op-lock expire during the move-safety scan.
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
    others = other_referencing_notes(vault_root, image_rel, note_rel, name_index, heartbeat)
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
            _unlink_and_audit(stray, vault_root, "archive verify-fail cleanup")
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
        _rollback_artifacts(manifest, op, vault_root, thumb_abs, arch_abs)
        journal.save(vault_root, cfg, manifest)
        res["message"] = f"note became unreadable ({type(exc).__name__}); aborted (no changes made)"
        return res
    if sha256_bytes(raw_now) != content_sha256:
        _rollback_artifacts(manifest, op, vault_root, thumb_abs, arch_abs)
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
        # TOCTOU guard (improvement plan 1.2): `digest` is the hash of the bytes
        # read at the top — the bytes the archive copy holds. The thumbnail /
        # copy work above is a window in which an external editor could have
        # replaced the image; re-hash the on-disk original immediately before
        # the unlink and, on mismatch, LEAVE IT IN PLACE (deleting it would
        # destroy the only copy of the post-change bytes — the archive has the
        # pre-change version). The note now embeds the thumbnail either way, so
        # the changed original is merely unreferenced, never lost.
        if sha256_bytes(Path(orig_abs).read_bytes()) != digest:
            op["original_deleted"] = False
            res["warning"] = (
                "original image changed during archiving; left in place "
                "(the archive holds the pre-change copy)"
            )
        else:
            os.unlink(orig_abs)
            op["original_deleted"] = True
            journal.log_vault_write("move_out", image_rel, f"→ {archive_rel}")
    except (OSError, journal.ScopeError) as exc:
        # Archive copy is durable + the note no longer embeds the original, so a
        # lingering (now-unreferenced) original is harmless; report a warning.
        op["original_deleted"] = False
        res["warning"] = f"original could not be deleted ({type(exc).__name__}); it is now unreferenced"
    op["state"] = "applied"
    # Prune spent/over-cap ops before the final persist (this applied archive op is
    # never evicted — it holds the only restore mapping for the moved-out original).
    journal.prune(vault_root, cfg, manifest)
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
        # Item 2.9 (improvement plan 2026-07-04): pass ONLY the delta.
        # save_config re-loads fresh under its write lock and merges the given
        # keys — the previous ``save_config(live)`` handed it the WHOLE
        # pre-load snapshot, so any key another thread changed between our
        # load_config() and the save (a Settings write, a config POST) was
        # silently reverted to this snapshot's stale value. One key in, one
        # key merged; every other key keeps whatever is freshest on disk.
        save_config({"vault_exclude_dirs": excl})
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
            # CRITICAL — do NOT fall through to the thumbnail deletion. The
            # pre-flight chose restore_note=True because the note still embeds the
            # *thumbnail*; the snapshot write that would re-point it at the original
            # just failed, so the note STILL embeds the thumbnail. Deleting that
            # thumbnail in step 3 would orphan a live embed (the exact corruption
            # this "atomic-or-nothing" reverter exists to prevent). Abort here with
            # the thumbnail + archive copy left in place and the op still "applied"
            # so the user can retry: step 1 (move original back) is idempotent — the
            # restored original is detected and skipped on the next attempt — so the
            # only side effect left behind is a now-unreferenced original copy, which
            # is harmless and self-heals on retry.
            return {"ok": False, "status": "failed",
                    "message": (f"could not restore the note from its snapshot "
                                f"({type(exc).__name__}); thumbnail left in place to avoid a "
                                "broken embed — resolve the snapshot issue and retry the restore.")}

    # 3) drop the thumbnail (best-effort). SAFE ONLY HERE: every path that reaches
    #    this point has the note embedding the ORIGINAL again — restore_note
    #    succeeded (its failure path returns above), or restore_note was False
    #    because the note is already pre-archive / was deleted. So the thumbnail can
    #    never still be a live embed at this point.
    if thumb_rel:
        try:
            tp = vault_root / thumb_rel
            journal.assert_under(tp, vault_root)
            if tp.exists():
                os.unlink(tp)
                # m4: audit the in-vault thumbnail removal (write was logged; so
                # is the delete now) so a vault change stays attributable.
                journal.log_vault_write("delete_thumb", thumb_rel, "archive restore")
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
