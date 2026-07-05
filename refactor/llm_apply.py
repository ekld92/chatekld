"""Generic journalled writer for applyable LLM proposals (rewrite / PDF summary).

Sibling of ``apply.py`` / ``format_fix.py``, but where those recompute a
deterministic body server-side at apply time, an LLM proposal is non-deterministic
and was therefore generated + cached server-side at generate time
(``refactor.staging``). This writer reads that staged body back (never trusting a
body from the client) and lays it down under the same two guards:

* **stale-diff** — the note's on-disk bytes must still hash to ``content_sha256``
  (the note was not edited since the proposal was generated);
* **WYSIWYG / drift** — the staged body must still hash to the ``proposed_sha256``
  the UI previewed, and the staging record must be the one computed against this
  exact note version.

A note is only written when its bytes are a clean UTF-8 round-trip, and the write
is journal-before-write (snapshot + manifest op, kind ``llm_note``) exactly like
apply/normalize — so an LLM edit is just as traceable and reversible (restore goes
through ``journal.revert_op`` → ``apply.revert_apply_note``).
"""
from __future__ import annotations

from pathlib import Path

from refactor import journal, staging
from refactor._write import journalled_write_note
from refactor.result import sha256_bytes, sha256_text


def apply_staged_note(vault_root: Path, cfg: dict, rel: str, content_sha256: str,
                      proposed_sha256: str, action: str) -> dict:
    """Apply one staged LLM proposal to *rel*. Returns a per-note result (never raises).

    Caller holds the obsidian operation lock (single writer).
    """
    vault_root = Path(vault_root)
    res = {"rel": rel, "status": "failed", "message": "", "action": action}
    note_path = vault_root / rel
    try:
        raw = note_path.read_bytes()
    except OSError as exc:
        res["message"] = f"unreadable note ({type(exc).__name__})"
        return res

    if sha256_bytes(raw) != content_sha256:
        res["status"] = "skipped"
        res["message"] = "stale: note changed on disk since the proposal was generated"
        return res

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        res["status"] = "skipped"
        res["message"] = "note is not valid UTF-8; refusing to rewrite"
        return res

    staged = staging.load_staged(vault_root, rel, action)
    if staged is None:
        res["status"] = "skipped"
        res["message"] = "no staged proposal found — re-generate before applying"
        return res
    proposed = staged["proposed"]

    # The staged proposal must be the one computed against THIS note version, and
    # must still match the previewed hash (drift / WYSIWYG guard).
    if staged.get("content_sha256") != content_sha256:
        res["status"] = "skipped"
        res["message"] = "staged proposal is for a different note version — re-generate"
        return res
    recomputed = sha256_text(proposed)
    if recomputed != proposed_sha256 or recomputed != staged.get("proposed_sha256"):
        res["status"] = "skipped"
        res["message"] = "preview drifted since generation — re-generate"
        return res
    if proposed == decoded:
        res["status"] = "noop"
        res["message"] = "nothing to apply (proposal is identical to the note)"
        return res

    # journal-before-write: SNAPSHOT and manifest op are written before note write.
    manifest = journal.load(vault_root, cfg)
    op_id, write_status, err_msg = journalled_write_note(
        vault_root=vault_root,
        cfg=cfg,
        rel=rel,
        raw_bytes=raw,
        hash_before=content_sha256,
        hash_after=proposed_sha256,
        proposed_text=proposed,
        op_kind="llm_note",
        manifest=manifest,
        action=action,
        log_category=f"llm_{action}",
        pre_write_persist=True,
    )
    if write_status == "failed":
        res["message"] = err_msg or "write failed"
        return res

    staging.clear(vault_root, rel, action)
    res["status"] = "applied"
    res["op_id"] = op_id
    res["message"] = f"{action} applied"
    return res
