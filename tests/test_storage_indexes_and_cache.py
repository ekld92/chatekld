"""Unit tests for SQLite secondary indexes and reconciliation stat cache."""
import json
import sqlite3
from types import SimpleNamespace
from unittest import mock

from accession import imports, reconciliation, review


def test_import_events_index_upgrade_and_query_plan(tmp_path):
    db_path = tmp_path / "audit" / "reconciliation.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Create legacy store without the new secondary indexes
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE import_units(scope TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(scope, id));
        CREATE TABLE import_sessions(id TEXT PRIMARY KEY, scope TEXT NOT NULL, started_at REAL NOT NULL, data TEXT NOT NULL, llm TEXT, spend_usd REAL NOT NULL DEFAULT 0, spend_unknown INTEGER NOT NULL DEFAULT 0, spend_calls INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE import_scopes(scope TEXT PRIMARY KEY, activated_at REAL NOT NULL, session TEXT NOT NULL, last_observed_at REAL);
        CREATE TABLE import_proposals(scope TEXT NOT NULL, key TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL, PRIMARY KEY(scope, key));
        CREATE TABLE import_events(seq INTEGER PRIMARY KEY, scope TEXT NOT NULL, session TEXT, unit TEXT, at REAL NOT NULL, data TEXT NOT NULL);
        CREATE TABLE import_session_results(seq INTEGER PRIMARY KEY, session TEXT NOT NULL, kind TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL);
        CREATE INDEX import_session_results_by_session ON import_session_results(session, kind, seq);
    """)
    con.commit()

    # _schema_is_current must detect missing indexes
    assert not imports._schema_is_current(con)

    # Running _schema_ddl brings the indexes
    imports._schema_ddl(con)
    con.commit()
    assert imports._schema_is_current(con)

    # Verify query plan for history() queries uses the index
    plan_scope = [row[3] for row in con.execute(
        "EXPLAIN QUERY PLAN SELECT session, unit, at, data FROM import_events WHERE scope=? ORDER BY seq",
        ("test_scope",),
    ).fetchall()]
    assert any("import_events_by_scope_seq" in step for step in plan_scope)

    plan_session = [row[3] for row in con.execute(
        "EXPLAIN QUERY PLAN SELECT session, unit, at, data FROM import_events WHERE scope=? AND session=? ORDER BY seq",
        ("test_scope", "s1"),
    ).fetchall()]
    assert any("import_events_by_scope_session_seq" in step for step in plan_session)
    con.close()


def test_review_events_index_creation_and_query_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATEKLD_BASE_DIR", str(tmp_path))
    with review.connection(write=True) as con:
        con.execute("INSERT INTO events(file_id, at, data) VALUES('f1', 1.0, '{}')")

    with review.connection(write=False) as con:
        indexes = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "events_by_file_id" in indexes

        plan = [row[3] for row in con.execute(
            "EXPLAIN QUERY PLAN SELECT sequence, at, data FROM events WHERE file_id=? ORDER BY sequence",
            ("f1",),
        ).fetchall()]
        assert any("events_by_file_id" in step for step in plan)


def test_reconciliation_saved_stat_cache_and_invalidation(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATEKLD_BASE_DIR", str(tmp_path))
    settings = SimpleNamespace(
        vault_root=tmp_path / "vault",
        zotero_sqlite=tmp_path / "zotero.sqlite",
    )
    settings.vault_root.mkdir()
    settings.zotero_sqlite.touch()

    # Initially store does not exist -> returns None
    assert reconciliation.saved(settings) is None

    # Write initial snapshot
    snap_data = {
        "database": "db1",
        "items": {"item1": {"key": "K1", "title": "Paper 1"}},
        "physical": {},
    }
    with reconciliation.connection(write=True) as con:
        con.execute(
            "INSERT INTO snapshots VALUES (?,?)",
            (reconciliation.scope(settings), json.dumps(snap_data)),
        )

    # First call: cache miss, parses from SQLite
    res1 = reconciliation.saved(settings)
    assert res1 is not None
    assert res1["database"] == "db1"
    assert res1["items"]["item1"]["title"] == "Paper 1"

    # Second call: cache hit! Verify json.loads is not called
    with mock.patch("json.loads", side_effect=AssertionError("cached call should not call json.loads")):
        res2 = reconciliation.saved(settings)
        assert res2 == res1

    # Caller mutating returned copy must NOT poison cache
    res2["database"] = "mutated_db"
    res2["items"]["item2"] = {"key": "K2"}
    res3 = reconciliation.saved(settings)
    assert res3["database"] == "db1"
    assert "item2" not in res3["items"]

    # Calling saved with explicit con bypasses cache
    with reconciliation.connection(write=False) as read_con:
        res_con = reconciliation.saved(settings, con=read_con)
        assert res_con["database"] == "db1"

    # Modifying DB on disk invalidates cache
    snap_data2 = {
        "database": "db2",
        "items": {"item1": {"key": "K1", "title": "Updated"}},
        "physical": {},
    }
    with reconciliation.connection(write=True) as con:
        con.execute(
            "UPDATE snapshots SET data=? WHERE scope=?",
            (json.dumps(snap_data2), reconciliation.scope(settings)),
        )

    # Next call sees updated data
    res4 = reconciliation.saved(settings)
    assert res4["database"] == "db2"
    assert res4["items"]["item1"]["title"] == "Updated"

    # Explicit invalidate_saved_cache
    reconciliation.invalidate_saved_cache()
    res5 = reconciliation.saved(settings)
    assert res5["database"] == "db2"
