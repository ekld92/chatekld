"""AP-10 SQLite/WAL migration, recovery, and bounded-write regressions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest

from tagging import journal


def _roots(tmp_path):
    vault = tmp_path / "vault"
    archive = tmp_path / "archive"
    vault.mkdir()
    return vault, {"tagging_archive_dir": str(archive)}


def _op(index, **extra):
    value = {
        "op_id": f"op-{index}",
        "kind": "finder_tag" if index % 2 else "zotero_tag",
        "state": "applied",
        "batch_id": f"batch-{index // 3}",
        "before": [f"old-{index}", "shared"],
        "after": [f"new-{index}"],
        "ts": f"2026-08-24T00:00:{index % 60:02d}+00:00",
        "unknown_future_field": {"ordinal": index},
    }
    value.update(extra)
    return value


def _write_legacy(vault, cfg, ops):
    path = journal.archive_dir(vault, cfg) / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "vault_key": journal._vault_key(vault),
        "ops": ops,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    path.write_bytes(raw)
    return path, raw


def test_json_migration_preserves_every_payload_field_order_and_verified_backup(tmp_path):
    vault, cfg = _roots(tmp_path)
    ops = [_op(index) for index in range(12)]
    legacy, raw = _write_legacy(vault, cfg, ops)

    migrated = journal.load(vault, cfg)

    assert list(migrated["ops"]) == ops
    assert journal.manifest_path(vault, cfg).is_file()
    assert journal.migration_backup_path(vault, cfg).read_bytes() == raw
    assert legacy.read_bytes() == raw
    # An idempotent reopen neither rewrites nor duplicates the generation.
    assert list(journal.load(vault, cfg)["ops"]) == ops
    with sqlite3.connect(journal.manifest_path(vault, cfg)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 12


def test_migration_failure_rolls_back_database_and_keeps_both_json_copies(tmp_path):
    vault, cfg = _roots(tmp_path)
    legacy, raw = _write_legacy(vault, cfg, [_op(0), _op(1)])
    real_insert = journal._insert_op
    calls = 0

    def _fail_second(conn, op):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated process boundary")
        return real_insert(conn, op)

    with mock.patch.object(journal, "_insert_op", side_effect=_fail_second):
        with pytest.raises(RuntimeError, match="process boundary"):
            journal.load(vault, cfg)

    assert not journal.manifest_path(vault, cfg).exists()
    assert legacy.read_bytes() == raw
    assert journal.migration_backup_path(vault, cfg).read_bytes() == raw
    assert [op["op_id"] for op in journal.load(vault, cfg)["ops"]] == ["op-0", "op-1"]


def test_crash_after_no_replace_publication_recovers_complete_database(tmp_path):
    vault, cfg = _roots(tmp_path)
    _write_legacy(vault, cfg, [_op(0), _op(1), _op(2)])
    real_fsync_dir = journal._fsync_dir
    calls = 0

    def _crash_after_publish(path):
        nonlocal calls
        calls += 1
        # First call durably publishes the backup; the second follows DB link.
        if calls == 2:
            raise RuntimeError("simulated crash after database publication")
        return real_fsync_dir(path)

    with mock.patch.object(journal, "_fsync_dir", side_effect=_crash_after_publish):
        with pytest.raises(RuntimeError, match="after database publication"):
            journal.load(vault, cfg)

    assert journal.manifest_path(vault, cfg).exists()
    assert [op["op_id"] for op in journal.load(vault, cfg)["ops"]] == [
        "op-0", "op-1", "op-2"
    ]


def test_concurrent_first_open_migrates_one_identical_generation(tmp_path):
    vault, cfg = _roots(tmp_path)
    ops = [_op(index) for index in range(100)]
    _write_legacy(vault, cfg, ops)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(journal.load, vault, cfg) for _ in range(8)]
        loaded = [future.result(timeout=20) for future in futures]

    assert all(list(manifest["ops"]) == ops for manifest in loaded)
    with sqlite3.connect(journal.manifest_path(vault, cfg)) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 100


def test_existing_different_migration_backup_is_never_replaced(tmp_path):
    vault, cfg = _roots(tmp_path)
    _write_legacy(vault, cfg, [_op(0)])
    backup = journal.migration_backup_path(vault, cfg)
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(b"different recovery history")

    with pytest.raises(journal.JournalUnreadableError, match="different content"):
        journal.load(vault, cfg)

    assert backup.read_bytes() == b"different recovery history"
    assert not journal.manifest_path(vault, cfg).exists()


def test_corrupt_legacy_input_never_creates_database_or_backup(tmp_path):
    vault, cfg = _roots(tmp_path)
    legacy = journal.archive_dir(vault, cfg) / "manifest.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{broken", encoding="utf-8")

    with pytest.raises(journal.JournalUnreadableError):
        journal.load(vault, cfg)

    assert legacy.read_text(encoding="utf-8") == "{broken"
    assert not journal.manifest_path(vault, cfg).exists()
    assert not journal.migration_backup_path(vault, cfg).exists()


def test_post_migration_legacy_divergence_fails_closed(tmp_path):
    vault, cfg = _roots(tmp_path)
    legacy, _raw = _write_legacy(vault, cfg, [_op(0)])
    journal.load(vault, cfg)
    legacy.write_text(json.dumps({"version": 1, "ops": [_op(99)]}), encoding="utf-8")

    with pytest.raises(journal.JournalUnreadableError, match="divergent"):
        journal.load(vault, cfg)


def test_failed_wal_update_rolls_back_prior_generation(tmp_path):
    vault, cfg = _roots(tmp_path)
    manifest = journal.load(vault, cfg)
    manifest["ops"].append(_op(0))
    journal.save(vault, cfg, manifest)
    changed = journal.load(vault, cfg)
    changed["ops"][0]["state"] = "reverted"

    with mock.patch.object(
        journal, "_insert_op", side_effect=sqlite3.OperationalError("disk full")
    ):
        with pytest.raises(journal.JournalTransientReadError):
            journal.save(vault, cfg, changed)

    assert journal.load(vault, cfg)["ops"][0]["state"] == "applied"


def test_normal_append_and_update_write_only_changed_rows_at_large_history(tmp_path):
    vault, cfg = _roots(tmp_path)
    manifest = journal.load(vault, cfg)
    manifest["ops"].extend(_op(index) for index in range(5_000))
    journal.save(vault, cfg, manifest)

    loaded = journal.load(vault, cfg)
    loaded["ops"].append(_op(5_000))
    with mock.patch.object(journal, "_insert_op", wraps=journal._insert_op) as insert:
        journal.save(vault, cfg, loaded)
    assert insert.call_count == 1

    loaded = journal.load(vault, cfg)
    loaded["ops"][2_500]["state"] = "reverted"
    with mock.patch.object(journal, "_insert_op", wraps=journal._insert_op) as insert:
        journal.save(vault, cfg, loaded)
    assert insert.call_count == 1
    assert len(journal.load(vault, cfg)["ops"]) == 5_001


def test_loaded_manifest_operation_lookup_uses_the_indexed_identity_map(tmp_path):
    vault, cfg = _roots(tmp_path)
    manifest = journal.load(vault, cfg)
    manifest["ops"].extend(_op(index) for index in range(5_000))
    journal.save(vault, cfg, manifest)

    loaded = journal.load(vault, cfg)
    assert journal.find_op(loaded, "op-4321") == _op(4_321)
    assert journal.find_op(loaded, "missing") is None


def test_concurrent_lost_lock_merges_serialize_without_clobber(tmp_path):
    vault, cfg = _roots(tmp_path)
    seed = journal.load(vault, cfg)
    journal.save(vault, cfg, seed)

    def _merge(index):
        local = journal.load(vault, cfg)
        local["ops"].append(_op(index))
        journal.save_merged(vault, cfg, local)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_merge, index) for index in range(40)]
        for future in futures:
            future.result(timeout=20)

    ids = [op["op_id"] for op in journal.load(vault, cfg)["ops"]]
    assert len(ids) == 40
    assert set(ids) == {f"op-{index}" for index in range(40)}


def test_tombstone_prevents_lost_writer_from_resurrecting_restored_op(tmp_path):
    vault, cfg = _roots(tmp_path)
    manifest = journal.load(vault, cfg)
    manifest["ops"].append(_op(0))
    journal.save(vault, cfg, manifest)
    stale = journal.load(vault, cfg)

    spent = journal.load(vault, cfg)
    spent["ops"][0]["state"] = "reverted"
    journal.prune(vault, cfg, spent)
    journal.save(vault, cfg, spent)
    journal.save_merged(vault, cfg, stale)

    assert journal.load(vault, cfg)["ops"] == []
    with sqlite3.connect(journal.manifest_path(vault, cfg)) as conn:
        assert conn.execute(
            "SELECT op_id FROM operation_tombstones"
        ).fetchall() == [("op-0",)]


def test_migrated_note_operation_remains_byte_exactly_restorable(tmp_path):
    vault, cfg = _roots(tmp_path)
    note = vault / "note.md"
    before = b"---\ntags: [old]\n---\nbody\n"
    after = b"---\ntags: [new]\n---\nbody\n"
    note.write_bytes(after)
    op = _op(
        0,
        kind="note_tag",
        note_rel="note.md",
        snapshot_rel="notes/op-0.bak",
        hash_after=hashlib.sha256(after).hexdigest(),
    )
    _write_legacy(vault, cfg, [op])
    snapshot = journal.archive_dir(vault, cfg) / "notes" / "op-0.bak"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(before)

    manifest = journal.load(vault, cfg)
    result = journal.revert_op(vault, cfg, journal.find_op(manifest, "op-0"))
    journal.save(vault, cfg, manifest)

    assert result["status"] == "reverted"
    assert note.read_bytes() == before


def test_unmounted_or_unopenable_database_is_transient_and_never_replaced(tmp_path):
    vault, cfg = _roots(tmp_path)
    manifest = journal.load(vault, cfg)
    manifest["ops"].append(_op(0))
    journal.save(vault, cfg, manifest)
    db = journal.manifest_path(vault, cfg)
    original = db.read_bytes()

    with mock.patch("tagging.journal.sqlite3.connect", side_effect=sqlite3.OperationalError("offline")):
        with pytest.raises(journal.JournalTransientReadError):
            journal.load(vault, cfg)

    assert db.read_bytes() == original


def test_load_is_a_pure_reader_and_never_blocks_on_a_writer(tmp_path):
    """WAL's contract: a reader must not be blocked by an in-flight writer.

    `load` used to run `_init_schema` — DDL plus `INSERT OR IGNORE` under
    `synchronous=FULL` — which takes a RESERVED lock, so any concurrent
    `BEGIN IMMEDIATE` turned a plain read into `JournalTransientReadError` and a
    503 on the Restore manifest during every harmonize/retire batch.
    """
    vault, cfg = _roots(tmp_path)
    manifest = journal.load(vault, cfg)
    manifest["ops"].append(_op(0))
    journal.save(vault, cfg, manifest)
    db = journal.manifest_path(vault, cfg)
    before_mtime = db.stat().st_mtime_ns

    writer = sqlite3.connect(db, isolation_level=None, timeout=0.2)
    try:
        writer.execute("PRAGMA busy_timeout=200")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO meta(key, value) VALUES('probe', '1')")
        loaded = journal.load(vault, cfg)
        assert [op["op_id"] for op in loaded["ops"]] == ["op-0"]
        writer.execute("ROLLBACK")
    finally:
        writer.close()

    assert db.stat().st_mtime_ns == before_mtime


def test_load_repairs_a_database_file_that_has_no_schema(tmp_path):
    """A crash between file creation and schema commit self-heals once."""
    vault, cfg = _roots(tmp_path)
    journal.save(vault, cfg, journal.load(vault, cfg))
    db = journal.manifest_path(vault, cfg)
    db.unlink()
    db.touch()  # zero-byte file: exists, but carries no tables

    assert list(journal.load(vault, cfg)["ops"]) == []
    assert journal.load(vault, cfg)["ops"] == []


def test_load_for_ops_append_update_and_no_unrelated_deletion(tmp_path):
    vault, cfg = _roots(tmp_path)
    man = journal.load(vault, cfg)
    for i in range(3):
        man["ops"].append({"op_id": f"op-{i}", "kind": "finder_tag", "state": "applied", "ts": journal.now_iso()})
    journal.save(vault, cfg, man)

    # Scoped append with empty op_ids
    scoped_append = journal.load_for_ops(vault, cfg)
    assert getattr(scoped_append, "scoped", False) is True
    assert len(scoped_append["ops"]) == 0
    scoped_append["ops"].append({"op_id": "op-3", "kind": "zotero_tag", "state": "applied", "ts": journal.now_iso()})
    journal.save(vault, cfg, scoped_append)

    # Verify all 4 ops exist
    full = journal.load(vault, cfg)
    assert [op["op_id"] for op in full["ops"]] == ["op-0", "op-1", "op-2", "op-3"]

    # Scoped update for single op
    scoped_update = journal.load_for_ops(vault, cfg, ["op-1"])
    assert getattr(scoped_update, "scoped", False) is True
    assert len(scoped_update["ops"]) == 1
    assert scoped_update["ops"][0]["op_id"] == "op-1"
    scoped_update["ops"][0]["state"] = "reverted"
    journal.save(vault, cfg, scoped_update)

    # Verify update landed and other ops remain unchanged
    reloaded = journal.load(vault, cfg)
    assert [op["op_id"] for op in reloaded["ops"]] == ["op-0", "op-1", "op-2", "op-3"]
    assert reloaded["ops"][1]["state"] == "reverted"
    assert reloaded["ops"][0]["state"] == "applied"
    assert reloaded["ops"][2]["state"] == "applied"


def test_load_for_ops_scoped_guards_and_refusals(tmp_path):
    vault, cfg = _roots(tmp_path)
    man = journal.load(vault, cfg)
    man["ops"].append({"op_id": "op-0", "kind": "finder_tag", "state": "applied", "ts": journal.now_iso()})
    journal.save(vault, cfg, man)

    scoped = journal.load_for_ops(vault, cfg, ["op-0"])
    assert getattr(scoped, "scoped", False) is True

    # prune() must refuse scoped manifest
    with pytest.raises(ValueError, match="prune/compact requires a full manifest"):
        journal.prune(vault, cfg, scoped)

    # compact() must refuse scoped manifest
    with pytest.raises(ValueError, match="prune/compact requires a full manifest"):
        journal.compact(vault, cfg, policy="keep_newest", value=10, dry_run=False, manifest=scoped)

    # Row removals must refuse
    with pytest.raises(ValueError, match="scoped manifest cannot remove operations"):
        del scoped["ops"][0]
    with pytest.raises(ValueError, match="scoped manifest cannot remove operations"):
        scoped["ops"].pop()
    with pytest.raises(ValueError, match="scoped manifest cannot remove operations"):
        scoped["ops"].remove(scoped["ops"][0])
    with pytest.raises(ValueError, match="scoped manifest cannot remove operations"):
        scoped["ops"].clear()
    with pytest.raises(ValueError, match="scoped manifest cannot remove operations"):
        scoped["ops"][:] = []
    with pytest.raises(ValueError, match="scoped manifest cannot remove operations"):
        scoped["ops"] = []


def test_load_for_ops_safety_and_integrity_refusals(tmp_path):
    import sqlite3
    vault, cfg = _roots(tmp_path)
    man = journal.load(vault, cfg)
    man["ops"].append({"op_id": "tombstoned-op", "kind": "finder_tag", "state": "applied", "ts": journal.now_iso()})
    journal.save(vault, cfg, man)

    # Delete op to create a tombstone
    man = journal.load(vault, cfg)
    man["ops"].pop()
    journal.save(vault, cfg, man)

    # Attempting to save tombstoned op via load_for_ops must fail closed
    scoped = journal.load_for_ops(vault, cfg)
    scoped["ops"].append({"op_id": "tombstoned-op", "kind": "finder_tag", "state": "applied", "ts": journal.now_iso()})
    with pytest.raises(journal.JournalUnreadableError, match="resurrect removed operation"):
        journal.save(vault, cfg, scoped)

    # Vault key mismatch
    db = journal.manifest_path(vault, cfg)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value='other_vault_key' WHERE key='vault_key'")
    conn.commit()
    conn.close()
    with pytest.raises(journal.JournalUnreadableError, match="belongs to a different vault"):
        journal.load_for_ops(vault, cfg)

    # Corrupt operation JSON
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value=? WHERE key='vault_key'", (journal._vault_key(vault),))
    conn.execute("INSERT INTO operations(op_id, kind, state, payload) VALUES('bad-json', 'test', 'applied', 'INVALID{JSON')")
    conn.commit()
    conn.close()
    with pytest.raises(journal.JournalUnreadableError, match="corrupt operation JSON"):
        journal.load_for_ops(vault, cfg, ["bad-json"])
