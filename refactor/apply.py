"""Callout-only batch note writer (the bulk Phase 2 apply) + note restore.

What the user approves in the preview diff is **exactly** what gets written: the
advisory ``> [!extracted]`` callout inlined beneath each described embed, with the
original embeds untouched. The proposed body is recomputed here from the *same*
analyzer the planner uses (``refactor.plan.analyze_note``) — sharing one transform
is what makes preview == apply.

Two guards protect every write:

* **stale-diff** — the note's on-disk bytes must still hash to the
  ``content_sha256`` the planner returned (the note was not edited since the plan);
* **WYSIWYG / drift** — the recomputed ``proposed_sha256`` must equal the value the
  UI previewed (the image cache / ignore-list did not change the proposed body
  out from under the user).

A note is additionally only written when its bytes are a clean UTF-8 round-trip, so
a strict-decode-then-encode reproduces the file exactly and the callout insertion
can never silently rewrite an undecodable byte.

Writes are journal-before-write (``refactor.journal``): a pre-write snapshot and a
manifest op are recorded *before* the atomic note write, so every vault change is
traceable and reversible and a crash never leaves an un-journaled mutation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from core.utils import write_bytes_atomic

from refactor import flags as flags_mod
from refactor import ignore, journal
from refactor._write import journalled_write_note
from refactor.plan import analyze_note
from refactor.resolver import build_name_index, excluded_dirs
from refactor.result import sha256_bytes


def _apply_one(vault_root: Path, cfg: dict, rel: str, content_sha256: str,
               proposed_sha256: str, name_index: dict, ignored: set,
               image_flags: dict, manifest: dict, strip_default: bool = False) -> dict:
    """Apply one approved note. Returns a per-note result dict (never raises)."""
    res = {"rel": rel, "status": "failed", "message": ""}
    note_path = vault_root / rel
    try:
        raw = note_path.read_bytes()
    except OSError as exc:
        res["message"] = f"unreadable note ({type(exc).__name__})"
        return res

    if sha256_bytes(raw) != content_sha256:
        res["status"] = "skipped"
        res["message"] = "stale: note changed on disk since the plan ran"
        return res

    # Only write notes that round-trip cleanly through UTF-8 — otherwise the
    # callout insertion (done on the decoded text) would re-encode replacement
    # characters and corrupt bytes the planner only ever saw lossily.
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        res["status"] = "skipped"
        res["message"] = "note is not valid UTF-8; refusing to rewrite"
        return res

    # analyze_note re-reads the file (a second read after the hash check above) and
    # recomputes the callout body server-side — we deliberately do NOT trust a body
    # from the client. The double read opens a tiny TOCTOU window (the note could
    # change between the two reads), but it is fully closed by the proposed_sha256
    # guard below: if the second read differs from what the plan saw, the recomputed
    # proposed body won't match the previewed hash and the write is skipped as drift.
    try:
        proposal, _doses = analyze_note(
            note_path, vault_root, name_index, ignored, image_flags,
            strip_default=strip_default)
    except OSError as exc:
        res["message"] = f"re-analysis failed ({type(exc).__name__})"
        return res

    if proposal.proposed_sha256 != proposed_sha256:
        res["status"] = "skipped"
        res["message"] = "preview drifted since the plan ran — re-run the plan"
        return res
    if not proposal.changed:
        res["status"] = "noop"
        res["message"] = "nothing to apply (no callouts proposed)"
        return res

    # journal-before-write: SNAPSHOT and manifest op are written before note write.
    op_id, write_status, err_msg = journalled_write_note(
        vault_root=vault_root,
        cfg=cfg,
        rel=rel,
        raw_bytes=raw,
        hash_before=content_sha256,
        hash_after=proposal.proposed_sha256,
        proposed_text=proposal.proposed,
        op_kind="apply_note",
        manifest=manifest,
        log_category="write_note",
    )
    if write_status == "failed":
        res["message"] = err_msg or "write failed"
        return res

    res["status"] = "applied"
    res["op_id"] = op_id
    res["message"] = "callout(s) applied"
    return res


# Manifest checkpoint cadence for batch writers (item 2.8d): bounds the
# crash-window of applied-but-unjournaled notes to this many, while keeping
# the whole-manifest rewrite cost at O(N / cadence) instead of O(N²).
JOURNAL_FLUSH_EVERY = 25


def apply_notes(vault_root: Path, cfg: dict, approved: list[dict],
                heartbeat: Optional[Callable[[], None]] = None) -> list[dict]:
    """Apply the callout-only transform to each approved note independently.

    *approved* is ``[{rel, content_sha256, proposed_sha256}, ...]`` (already
    scope-validated by the route). Builds the resolver name-index + ignore-set
    once, then applies each note; one note's failure never aborts the rest.
    Caller holds the obsidian operation lock (single writer).

    *heartbeat* (optional) is called once per note so the caller's op-lock does
    not passively expire on a large batch (each note re-reads + re-analyzes the
    file). Without it a batch over the lock TTL could be stolen by a concurrent
    indexing run and corrupt the vault / journal.
    """
    vault_root = Path(vault_root)
    excluded = excluded_dirs(vault_root)
    name_index = build_name_index(vault_root, excluded)
    ignored = ignore.load_ignored(vault_root)
    image_flags = flags_mod.load_flags(vault_root)
    # Same config source the plan read, so the recomputed callout body (and thus
    # proposed_sha256) matches the preview even when the scope-wide strip default
    # is on. A flip between plan and apply is caught by the WYSIWYG guard.
    strip_default = bool(cfg.get("refactor_strip_preamble_default"))
    manifest = journal.load(vault_root, cfg)

    results: list[dict] = []
    for i, item in enumerate(approved, start=1):
        if heartbeat is not None:
            heartbeat()
        rel = item.get("rel", "")
        results.append(_apply_one(
            vault_root, cfg, rel,
            item.get("content_sha256", ""), item.get("proposed_sha256", ""),
            name_index, ignored, image_flags, manifest, strip_default,
        ))
        # Item 2.8d (improvement plan 2026-07-04): checkpoint the manifest
        # every JOURNAL_FLUSH_EVERY notes. The once-after-the-batch save meant
        # a crash mid-batch left every already-APPLIED note invisible to
        # Restore (its snapshot .bak existed, but no op-record pointed at it —
        # only manual recovery). Per-note saves were the previous extreme
        # (O(N²) rewrite cost, the reason the batch save exists); every-25
        # bounds the restore blind spot to at most 25 notes while keeping the
        # manifest rewrite cost O(N/25). Safe: journal.save is the same
        # fsync'd atomic writer, the caller holds the op-lock (single writer),
        # and re-saving a manifest that later gains more ops is idempotent —
        # the final save below remains authoritative.
        if i % JOURNAL_FLUSH_EVERY == 0:
            journal.save(vault_root, cfg, manifest)
    # Final persist + prune (spent/over-cap ops + their snapshots) so the
    # manifest and `.bak` disk stay bounded over the app's lifetime.
    journal.prune(vault_root, cfg, manifest)
    journal.save(vault_root, cfg, manifest)
    return results


def revert_apply_note(vault_root: Path, cfg: dict, op: dict) -> dict:
    """Restore a note from its pre-apply snapshot. Mutates ``op['state']``.

    Conservative: only reverts when the note still matches the bytes this op
    wrote (``hash_after``). If it was edited again afterwards, it skips rather
    than clobber the newer content. Caller persists the manifest.
    """
    rel = op.get("note_rel", "")
    note_path = vault_root / rel
    try:
        cur = note_path.read_bytes()
    except OSError as exc:
        return {"ok": False, "status": "failed", "message": f"unreadable note ({type(exc).__name__})"}

    cur_hash = sha256_bytes(cur)
    if cur_hash == op.get("hash_before"):
        op["state"] = "reverted"
        op["reverted_ts"] = journal.now_iso()
        return {"ok": True, "status": "already_original", "message": "Note already at its pre-apply content."}
    if cur_hash != op.get("hash_after"):
        return {"ok": False, "status": "skipped",
                "message": "Note changed since apply; not reverting to avoid clobbering newer edits."}

    try:
        snap = journal.read_snapshot(vault_root, cfg, op.get("snapshot_rel", ""))
    except (OSError, journal.ScopeError) as exc:
        return {"ok": False, "status": "failed", "message": f"snapshot unavailable ({type(exc).__name__})"}

    journal.assert_under(note_path, vault_root)
    write_bytes_atomic(str(note_path), snap)
    journal.log_vault_write("restore_note", rel, f"→{op.get('hash_before','')[:8]}")
    op["state"] = "reverted"
    op["reverted_ts"] = journal.now_iso()
    return {"ok": True, "status": "reverted", "message": "Note restored from snapshot."}
