"""Deterministic formatting-fix batch writer (the second Phase 2 apply) .

The sibling of ``apply.py``: where apply inlines OCR callouts, this lays down the
``refactor.normalize`` formatting fix (blank lines before headings/lists, blanks
around code fences, trailing-whitespace strip, blank-run collapse, single final
newline). The two are **independent** transforms of the current on-disk note —
each its own journalled, reversible op — so applying one makes the other's
``content_sha256`` stale (re-run the plan between them).

The same two guards as apply protect every write:

* **stale-diff** — the on-disk bytes must still hash to the ``content_sha256``
  the planner returned;
* **WYSIWYG / drift** — the recomputed ``normalized_sha256`` must equal the value
  the UI previewed.

A note is only written when its bytes are a clean UTF-8 round-trip, and the write
is journal-before-write (snapshot + manifest op recorded first), exactly like
``apply.py`` — so a formatting fix is just as traceable and reversible.

Restore is handled by ``journal.revert_op`` dispatching the ``normalize_note`` op
kind to ``apply.revert_apply_note`` (the op carries the same snapshot fields), so
there is no bespoke reverter here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from refactor import apply as apply_mod
from refactor import journal, normalize
from refactor._write import journalled_write_note
from refactor.result import sha256_bytes, sha256_text


def _normalize_one(vault_root: Path, cfg: dict, rel: str, content_sha256: str,
                   normalized_sha256: str, manifest: dict) -> dict:
    """Apply one approved formatting fix. Returns a per-note result (never raises)."""
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

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        res["status"] = "skipped"
        res["message"] = "note is not valid UTF-8; refusing to rewrite"
        return res

    # Recompute the normalized body server-side (never trust a client body).
    normalized = normalize.normalize_text(decoded)
    if sha256_text(normalized) != normalized_sha256:
        res["status"] = "skipped"
        res["message"] = "preview drifted since the plan ran — re-run the plan"
        return res
    if normalized == decoded:
        res["status"] = "noop"
        res["message"] = "nothing to fix (already normalized)"
        return res

    # journal-before-write: SNAPSHOT and manifest op are written before note write.
    op_id, write_status, err_msg = journalled_write_note(
        vault_root=vault_root,
        cfg=cfg,
        rel=rel,
        raw_bytes=raw,
        hash_before=content_sha256,
        hash_after=normalized_sha256,
        proposed_text=normalized,
        op_kind="normalize_note",
        manifest=manifest,
    )
    if write_status == "failed":
        res["message"] = err_msg or "write failed"
        return res

    res["status"] = "applied"
    res["op_id"] = op_id
    res["message"] = "formatting fixed"
    return res


def apply_normalize(vault_root: Path, cfg: dict, approved: list[dict],
                    heartbeat: Optional[Callable[[], None]] = None) -> list[dict]:
    """Apply the deterministic formatting fix to each approved note independently.

    *approved* is ``[{rel, content_sha256, normalized_sha256}, ...]`` (already
    scope-validated by the route). One note's failure never aborts the rest.
    Caller holds the obsidian operation lock (single writer).

    *heartbeat* (optional) is called once per note so a large batch cannot let
    the caller's op-lock expire mid-run (see ``apply.apply_notes``).
    """
    vault_root = Path(vault_root)
    manifest = journal.load(vault_root, cfg)
    results: list[dict] = []
    for i, item in enumerate(approved, start=1):
        if heartbeat is not None:
            heartbeat()
        rel = item.get("rel", "")
        results.append(_normalize_one(
            vault_root, cfg, rel,
            item.get("content_sha256", ""), item.get("normalized_sha256", ""),
            manifest,
        ))
        # Item 2.8d: bound the crash-window of applied-but-unjournaled notes —
        # full rationale at apply.apply_notes (this mirrors it batch-for-batch).
        if i % apply_mod.JOURNAL_FLUSH_EVERY == 0:
            journal.save(vault_root, cfg, manifest)
    # Final persist + prune (bounded manifest/snapshot disk), mirroring apply_notes.
    journal.prune(vault_root, cfg, manifest)
    journal.save(vault_root, cfg, manifest)
    return results
