"""Crash-trail contract for vault_history._write.journalled_write_note."""
from __future__ import annotations

from pathlib import Path

from vault_history import journal
from vault_history._write import journalled_write_note


def _cfg(tmp_path: Path) -> dict:
    return {"refactor_archive_dir": str(tmp_path / "archive")}


def test_before_write_exception_after_persist_marks_failed_not_applied(tmp_path):
    """pre_write_persist + raising predicate must not leave state=applied.

    Why: key recovery saves the crash trail before re-checking the world. An
    unexpected exception used to escape after that save, so Restore saw an
    applied op for bytes that never reached disk.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "n.md"
    note.write_text("before\n", encoding="utf-8")
    raw = note.read_bytes()
    cfg = _cfg(tmp_path)
    manifest = journal.load(vault, cfg)

    def boom():
        raise RuntimeError("inventory blew up")

    op_id, status, err = journalled_write_note(
        vault, cfg, "n.md", raw, "a" * 64, "b" * 64, "after\n",
        "notesync_recover_keys", manifest,
        pre_write_persist=True, before_write=boom,
    )
    assert status == "failed"
    assert err == "before_write_error:RuntimeError"
    assert note.read_text(encoding="utf-8") == "before\n"
    reloaded = journal.load(vault, cfg)
    op = next(o for o in reloaded["ops"] if o["id"] == op_id)
    assert op["state"] == "failed"


def test_before_write_named_reason_is_surfaced(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "n.md"
    note.write_text("before\n", encoding="utf-8")
    raw = note.read_bytes()
    cfg = _cfg(tmp_path)
    manifest = journal.load(vault, cfg)

    op_id, status, err = journalled_write_note(
        vault, cfg, "n.md", raw, "a" * 64, "b" * 64, "after\n",
        "notesync_recover_keys", manifest,
        pre_write_persist=True,
        before_write=lambda: "config_changed_before_write",
    )
    assert status == "failed"
    assert err == "config_changed_before_write"
    reloaded = journal.load(vault, cfg)
    op = next(o for o in reloaded["ops"] if o["id"] == op_id)
    assert op["state"] == "failed"
    assert note.read_text(encoding="utf-8") == "before\n"


def test_pre_write_prune_protects_the_op_it_just_appended(tmp_path, monkeypatch):
    """2026-09-17 review, F11: the pre-write prune passed no protect_op_ids,
    so a batch caller whose window was full could evict its own op."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "n.md"
    note.write_text("before\n", encoding="utf-8")
    cfg = _cfg(tmp_path)
    manifest = journal.load(vault, cfg)
    seen = {}
    real = journal.prune

    def spy(*args, protect_op_ids=None, **kwargs):
        seen["protect"] = protect_op_ids
        return real(*args, protect_op_ids=protect_op_ids, **kwargs)

    monkeypatch.setattr(journal, "prune", spy)
    op_id, status, _err = journalled_write_note(
        vault, cfg, "n.md", note.read_bytes(), "a" * 64, "b" * 64, "after\n",
        "notesync_update", manifest, pre_write_persist=True)
    assert status == "applied"
    assert seen["protect"] and op_id in seen["protect"]
