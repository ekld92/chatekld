"""Disposable recovery evidence for the inactive upgrade snapshot primitive."""
import sqlite3
import fcntl
import json
import os
import shutil

import pytest

from core.upgrade_snapshot import create_offline_snapshot, restore_to_empty, verify_snapshot


def test_snapshot_verifies_and_restores_matching_state(tmp_path):
    source = tmp_path / "app"
    (source / "obsidian_storage" / "pdf_cache").mkdir(parents=True)
    (source / "obsidian_storage" / "vectors.bin").write_bytes(b"old vectors")
    (source / "obsidian_storage" / "pdf_cache" / "paper.txt").write_text("old extraction")
    (source / "config.json").write_text('{"embed":"old"}')
    with sqlite3.connect(source / "uploads.db") as db:
        db.execute("CREATE TABLE items (value TEXT)")
        db.execute("INSERT INTO items VALUES ('baseline')")
    snapshot = tmp_path / "backup"
    manifest = create_offline_snapshot(source, snapshot, source_state="fixture stopped; no writers")
    assert verify_snapshot(snapshot) == manifest
    assert {row["path"] for row in manifest["files"]} == {
        "obsidian_storage/pdf_cache/paper.txt", "obsidian_storage/vectors.bin",
        "config.json", "uploads.db", "server.json.lock"}
    restored = tmp_path / "restored"
    assert restore_to_empty(snapshot, restored) == manifest
    with sqlite3.connect(restored / "uploads.db") as db:
        assert db.execute("SELECT value FROM items").fetchone() == ("baseline",)
    assert (restored / "obsidian_storage" / "vectors.bin").read_bytes() == b"old vectors"
    with pytest.raises(ValueError, match="must be new"):
        restore_to_empty(snapshot, restored)


def test_snapshot_refuses_changed_or_unsafe_evidence(tmp_path, monkeypatch):
    source = tmp_path / "app"
    source.mkdir()
    (source / "cache.txt").write_text("first")
    import core.upgrade_snapshot as module

    original = module.shutil.copytree

    def change_source(src, dst):
        result = original(src, dst)
        if src == source:
            (source / "cache.txt").write_text("second")
        return result

    monkeypatch.setattr(module.shutil, "copytree", change_source)
    with pytest.raises(ValueError, match="Source changed"):
        create_offline_snapshot(source, tmp_path / "backup", source_state="fixture stopped")
    assert not (tmp_path / "backup").exists()
    monkeypatch.setattr(module.shutil, "copytree", original)
    snapshot = tmp_path / "backup"
    create_offline_snapshot(source, snapshot, source_state="fixture stopped")
    (snapshot / "data" / "cache.txt").write_text("tampered")
    with pytest.raises(ValueError, match="inventory mismatch"):
        restore_to_empty(snapshot, tmp_path / "restored")


def test_snapshot_refuses_symlink_state(tmp_path):
    source = tmp_path / "app"
    source.mkdir()
    (source / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="Symlink refused"):
        create_offline_snapshot(source, tmp_path / "backup", source_state="fixture stopped")


def test_snapshot_excludes_server_for_entire_copy(tmp_path, monkeypatch):
    source = tmp_path / "app"
    source.mkdir()
    (source / "config.json").write_text("{}")
    import core.upgrade_snapshot as module

    original = module.shutil.copytree

    def check_lease(src, dst):
        if src == source:
            with (source / "server.json.lock").open("rb") as stream:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return original(src, dst)

    monkeypatch.setattr(module.shutil, "copytree", check_lease)
    create_offline_snapshot(source, tmp_path / "backup", source_state="fixture stopped")
    with (source / "server.json.lock").open("rb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_snapshot_refuses_active_or_unverifiable_server(tmp_path):
    source = tmp_path / "app"
    source.mkdir()
    lock = source / "server.json.lock"
    with lock.open("wb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        with pytest.raises(ValueError, match="lease is held"):
            create_offline_snapshot(source, tmp_path / "backup", source_state="fixture stopped")
    (source / "server.json").write_text(json.dumps({"pid": os.getpid()}))
    with pytest.raises(ValueError, match="server is running"):
        create_offline_snapshot(source, tmp_path / "backup", source_state="fixture stopped")
    (source / "server.json").write_text("{invalid")
    with pytest.raises(ValueError, match="Unverifiable server record"):
        create_offline_snapshot(source, tmp_path / "backup", source_state="fixture stopped")
    assert not (tmp_path / "backup").exists()


def test_snapshot_refuses_orphan_sqlite_sidecar(tmp_path):
    source = tmp_path / "app"
    source.mkdir()
    (source / "uploads.db-wal").write_bytes(b"orphan")
    with pytest.raises(ValueError, match="Orphan SQLite sidecar"):
        create_offline_snapshot(source, tmp_path / "backup", source_state="fixture stopped")


def test_sqlite_integrity_checks_never_create_source_or_backup_sidecars(tmp_path):
    source = tmp_path / "app"
    source.mkdir()
    db_path = source / "uploads.db"
    db = sqlite3.connect(db_path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE items (value TEXT)")
        db.execute("INSERT INTO items VALUES ('baseline')")
        db.commit()
    finally:
        db.close()
    assert not (source / "uploads.db-wal").exists()
    snapshot = tmp_path / "backup"
    manifest = create_offline_snapshot(source, snapshot, source_state="fixture stopped")
    assert {row["path"] for row in manifest["files"]} == {"uploads.db", "server.json.lock"}
    assert not (source / "uploads.db-wal").exists()
    assert not (source / "uploads.db-shm").exists()
    assert not (snapshot / "data" / "uploads.db-wal").exists()
    assert not (snapshot / "data" / "uploads.db-shm").exists()
    assert verify_snapshot(snapshot) == manifest
    assert not (snapshot / "data" / "uploads.db-shm").exists()


def test_snapshot_restores_frozen_uncheckpointed_wal(tmp_path):
    live = tmp_path / "live.db"
    db = sqlite3.connect(live)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("CREATE TABLE items (value TEXT)")
        db.execute("INSERT INTO items VALUES ('in wal')")
        db.commit()
        source = tmp_path / "app"
        source.mkdir()
        for path in tmp_path.glob("live.db*"):
            shutil.copy2(path, source / path.name.replace("live.db", "uploads.db"))
    finally:
        db.close()
    snapshot = tmp_path / "backup"
    manifest = create_offline_snapshot(source, snapshot, source_state="frozen WAL fixture")
    assert {"uploads.db", "uploads.db-wal", "uploads.db-shm"}.issubset(
        {row["path"] for row in manifest["files"]})
    restored = tmp_path / "restored"
    restore_to_empty(snapshot, restored)
    with sqlite3.connect(restored / "uploads.db") as checked:
        assert checked.execute("SELECT value FROM items").fetchone() == ("in wal",)
    assert verify_snapshot(snapshot) == manifest
