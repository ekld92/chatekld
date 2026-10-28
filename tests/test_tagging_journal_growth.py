"""Journal growth: bounded stats, paging, and the retention/compaction policy.

Batch F (2026-08-26). The problem these cover: applied Finder/Zotero ops are
exempt from the ``_MAX_OPS`` cap because their prior tag lists live INLINE in
the record with no snapshot file behind them, so that population grows for the
life of the library — measured at 11,451 of 11,651 ops, 8 MB re-parsed by
``load()`` on every single write.

Two invariants run through every test here, and both are about not destroying
undo records:

* nothing in this batch may drop a note or vocab op — they keep their file
  snapshots and their existing evictable window;
* retention is OFF unless explicitly enabled, and a malformed setting must
  drop NOTHING rather than guess.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from tagging import journal


def _roots(tmp_path):
    vault = tmp_path / "vault"
    archive = tmp_path / "archive"
    vault.mkdir()
    return vault, {"tagging_archive_dir": str(archive)}


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _op(op_id, kind="zotero_tag", state="applied", days_ago=1.0, **extra):
    value = {
        "op_id": op_id, "kind": kind, "state": state,
        "citation_key": f"key-{op_id}",
        "before": ["old"], "after": ["old", "new"],
        "ts": _ts(days_ago),
    }
    value.update(extra)
    return value


def _seed(vault, cfg, ops):
    manifest = journal.load(vault, cfg)
    for op in ops:
        manifest["ops"].append(op)
    journal.save(vault, cfg, manifest)
    return manifest


# --- stats -----------------------------------------------------------------

def test_stats_counts_without_parsing_payloads(tmp_path):
    vault, cfg = _roots(tmp_path)
    _seed(vault, cfg, [
        _op("a", kind="zotero_tag"),
        _op("b", kind="finder_tag"),
        _op("c", kind="note_tag"),
        _op("d", kind="zotero_tag", state="failed"),
    ])
    st = journal.stats(vault, cfg)
    assert st["ops"] == 4
    assert st["bytes"] > 0
    # `protected` is the never-evicted population: applied external ops only.
    # The failed one is not protected (prune drops spent ops), nor is the note.
    assert st["protected"] == 2
    assert st["evictable"] == 2
    assert st["by_kind_state"]["zotero_tag/applied"] == 1
    assert st["by_kind_state"]["zotero_tag/failed"] == 1
    assert st["oldest_ts"] and st["newest_ts"]


def test_stats_on_a_journal_that_does_not_exist_yet(tmp_path):
    """The Restore panel asks before the first write ever happens."""
    vault, cfg = _roots(tmp_path)
    st = journal.stats(vault, cfg)
    assert st == {
        "ops": 0, "bytes": 0, "by_kind_state": {}, "oldest_ts": None,
        "newest_ts": None, "protected": 0, "evictable": 0, "tombstones": 0,
    }


def test_stats_is_a_pure_reader_and_never_blocks_on_a_writer(tmp_path):
    """Same rule as ``load``: creating schema on a read path takes a RESERVED
    lock and turns an ordinary read into SQLITE_BUSY against a concurrent
    ``BEGIN IMMEDIATE`` — which is what WAL exists to prevent."""
    vault, cfg = _roots(tmp_path)
    _seed(vault, cfg, [_op("a")])
    db = journal.manifest_path(vault, cfg)
    holder = journal._connect(db, create=False)
    try:
        holder.execute("BEGIN IMMEDIATE")
        st = journal.stats(vault, cfg)      # must not raise
        assert st["ops"] == 1
    finally:
        holder.execute("ROLLBACK")
        holder.close()


# --- paging ----------------------------------------------------------------

def test_load_page_returns_newest_first_with_a_working_cursor(tmp_path):
    vault, cfg = _roots(tmp_path)
    _seed(vault, cfg, [_op(f"op{i:03d}") for i in range(25)])

    first = journal.load_page(vault, cfg, limit=10)
    assert first["total"] == 25
    assert [o["op_id"] for o in first["ops"]] == [
        f"op{i:03d}" for i in range(24, 14, -1)]
    assert first["next_before"] is not None

    second = journal.load_page(vault, cfg, limit=10,
                               before_seq=first["next_before"])
    assert [o["op_id"] for o in second["ops"]] == [
        f"op{i:03d}" for i in range(14, 4, -1)]
    # No overlap and no gap between consecutive pages.
    assert not ({o["op_id"] for o in first["ops"]}
                & {o["op_id"] for o in second["ops"]})

    third = journal.load_page(vault, cfg, limit=10,
                              before_seq=second["next_before"])
    assert len(third["ops"]) == 5
    # A short page is the end of the history.
    assert third["next_before"] is None


def test_load_page_never_costs_the_whole_history(tmp_path):
    """The point of the batch: page cost is O(limit), not O(history)."""
    vault, cfg = _roots(tmp_path)
    _seed(vault, cfg, [_op(f"op{i:04d}") for i in range(2000)])

    t0 = time.perf_counter()
    journal.load(vault, cfg)
    full = time.perf_counter() - t0

    t0 = time.perf_counter()
    page = journal.load_page(vault, cfg, limit=100)
    paged = time.perf_counter() - t0

    assert len(page["ops"]) == 100 and page["total"] == 2000
    # Generous factor: this asserts the ORDER of cost, not a benchmark. The
    # live measurement was ~110 ms against ~1 ms.
    assert paged < full / 3, f"paged={paged:.4f}s full={full:.4f}s"


def test_load_page_is_clamped_and_tolerates_a_dead_cursor(tmp_path):
    vault, cfg = _roots(tmp_path)
    _seed(vault, cfg, [_op("a"), _op("b")])
    assert len(journal.load_page(vault, cfg, limit=10_000)["ops"]) == 2
    assert len(journal.load_page(vault, cfg, limit=0)["ops"]) == 1
    assert journal.load_page(vault, cfg, before_seq=1)["ops"] == []


# --- retention: what it may and may not drop -------------------------------

def test_retention_never_touches_note_or_vocab_ops(tmp_path):
    """Note/vocab ops keep file snapshots and their own evictable window; a
    retention pass that reached them would delete a restorable note's only
    pre-write copy."""
    vault, cfg = _roots(tmp_path)
    ops = [
        _op("note", kind="note_tag", days_ago=900),
        _op("vocab", kind="vocab_add", days_ago=900),
        _op("zot", kind="zotero_tag", days_ago=900),
    ]
    victims = journal._retention_victims(ops, {
        "tagging_journal_retention_policy": "age_days",
        "tagging_journal_retention_days": 30,
    })
    assert [v["op_id"] for v in victims] == ["zot"]


def test_retention_never_touches_a_non_applied_op(tmp_path):
    """A failed/reverted op is prune's business and has nothing to restore."""
    ops = [_op("f", state="failed", days_ago=900),
           _op("r", state="reverted", days_ago=900),
           _op("a", state="applied", days_ago=900)]
    victims = journal._retention_victims(ops, {
        "tagging_journal_retention_policy": "age_days",
        "tagging_journal_retention_days": 30,
    })
    assert [v["op_id"] for v in victims] == ["a"]


@pytest.mark.parametrize("cfg_in", [
    {},
    {"tagging_journal_retention_policy": "nonsense",
     "tagging_journal_retention_days": 1},
    {"tagging_journal_retention_policy": "age_days",
     "tagging_journal_retention_days": "not a number"},
    {"tagging_journal_retention_policy": "age_days",
     "tagging_journal_retention_days": 0},
    {"tagging_journal_retention_policy": "keep_newest",
     "tagging_journal_retention_keep": -5},
])
def test_a_malformed_retention_setting_drops_nothing(cfg_in):
    """Guessing here means deleting the only record of how to undo a write."""
    ops = [_op(f"op{i}", days_ago=900) for i in range(10)]
    assert journal._retention_victims(ops, cfg_in) == []


def test_an_unparseable_timestamp_keeps_the_op():
    ops = [_op("good", days_ago=900), _op("bad", days_ago=900, ts="garbage"),
           _op("none", days_ago=900)]
    ops[2].pop("ts")
    victims = journal._retention_victims(ops, {
        "tagging_journal_retention_policy": "age_days",
        "tagging_journal_retention_days": 30,
    })
    assert [v["op_id"] for v in victims] == ["good"]


def test_keep_newest_keeps_the_newest_n(tmp_path):
    ops = [_op(f"op{i:02d}", days_ago=100 - i) for i in range(10)]
    victims = journal._retention_victims(ops, {
        "tagging_journal_retention_policy": "keep_newest",
        "tagging_journal_retention_keep": 4,
    })
    assert [v["op_id"] for v in victims] == [f"op{i:02d}" for i in range(6)]


# --- retention inside prune ------------------------------------------------

def test_prune_does_not_apply_retention_unless_it_is_enabled(tmp_path):
    """Ships off. A default that quietly trims undo history would be the worst
    possible default for this particular file."""
    vault, cfg = _roots(tmp_path)
    manifest = _seed(vault, cfg, [_op(f"op{i}", days_ago=900) for i in range(5)])
    cfg_off = dict(cfg, tagging_journal_retention_policy="age_days",
                   tagging_journal_retention_days=1)
    journal.prune(vault, cfg_off, manifest)
    assert len(manifest["ops"]) == 5


def test_prune_applies_retention_when_enabled(tmp_path):
    vault, cfg = _roots(tmp_path)
    manifest = _seed(vault, cfg, [
        _op("old1", days_ago=900), _op("old2", days_ago=900),
        _op("fresh", days_ago=1),
    ])
    cfg_on = dict(cfg, tagging_journal_retention_enabled=True,
                  tagging_journal_retention_policy="age_days",
                  tagging_journal_retention_days=30)
    dropped = journal.prune(vault, cfg_on, manifest)
    assert dropped == 2
    assert [o["op_id"] for o in manifest["ops"]] == ["fresh"]
    journal.save(vault, cfg, manifest)
    assert journal.stats(vault, cfg)["ops"] == 1


def test_retention_spares_the_batch_this_request_just_wrote(tmp_path):
    """`protect_batch_id` exists because a batch that evicted its own earliest
    ops would make the "undo in one click" it just promised silently partial.
    Retention must honour that exemption, not route around it."""
    vault, cfg = _roots(tmp_path)
    manifest = _seed(vault, cfg, [
        _op("mine1", days_ago=900, batch_id="B"),
        _op("mine2", days_ago=900, batch_id="B"),
        _op("other", days_ago=900, batch_id="A"),
    ])
    cfg_on = dict(cfg, tagging_journal_retention_enabled=True,
                  tagging_journal_retention_policy="age_days",
                  tagging_journal_retention_days=30)
    journal.prune(vault, cfg_on, manifest, protect_batch_id="B")
    assert sorted(o["op_id"] for o in manifest["ops"]) == ["mine1", "mine2"]


def test_retention_spares_this_requests_own_freshly_appended_ops(tmp_path):
    vault, cfg = _roots(tmp_path)
    manifest = _seed(vault, cfg, [
        _op("keep", days_ago=900), _op("drop", days_ago=900)])
    cfg_on = dict(cfg, tagging_journal_retention_enabled=True,
                  tagging_journal_retention_policy="age_days",
                  tagging_journal_retention_days=30)
    journal.prune(vault, cfg_on, manifest, protect_op_ids={"keep"})
    assert [o["op_id"] for o in manifest["ops"]] == ["keep"]


# --- compaction ------------------------------------------------------------

def test_compact_dry_run_writes_nothing(tmp_path):
    vault, cfg = _roots(tmp_path)
    _seed(vault, cfg, [_op(f"op{i}", days_ago=900) for i in range(6)])
    before = journal.stats(vault, cfg)["ops"]
    res = journal.compact(vault, cfg, policy="keep_newest", value=2,
                          dry_run=True)
    assert res["would_drop"] == 4 and res["dropped"] == 0
    assert res["candidates"] == 6
    assert journal.stats(vault, cfg)["ops"] == before


def test_compact_drops_and_tombstones_through_the_normal_removal_path(tmp_path):
    """Routed through load → retain → save so it reuses the tested
    tombstone-then-delete transaction; a raw DELETE would be a second write
    path through the most safety-critical module in the app."""
    vault, cfg = _roots(tmp_path)
    _seed(vault, cfg, [_op(f"op{i}", days_ago=900) for i in range(6)])
    res = journal.compact(vault, cfg, policy="keep_newest", value=2,
                          dry_run=False)
    assert res["dropped"] == 4 and res["after"] == 2
    st = journal.stats(vault, cfg)
    assert st["ops"] == 2
    # Durable tombstones are what stop a stale writer resurrecting a dropped op.
    assert st["tombstones"] == 4
    assert [o["op_id"] for o in journal.load(vault, cfg)["ops"]] == ["op4", "op5"]


def test_compact_leaves_note_history_alone(tmp_path):
    vault, cfg = _roots(tmp_path)
    _seed(vault, cfg, [
        _op("n1", kind="note_tag", days_ago=900),
        _op("n2", kind="note_tag", days_ago=900),
        _op("z1", days_ago=900), _op("z2", days_ago=900), _op("z3", days_ago=900),
    ])
    journal.compact(vault, cfg, policy="keep_newest", value=1, dry_run=False)
    kinds = sorted(o["op_id"] for o in journal.load(vault, cfg)["ops"])
    assert kinds == ["n1", "n2", "z3"]


@pytest.mark.parametrize("policy,value", [
    ("nonsense", 5), ("age_days", 0), ("age_days", -1), ("keep_newest", "x"),
])
def test_compact_refuses_a_malformed_request(tmp_path, policy, value):
    vault, cfg = _roots(tmp_path)
    _seed(vault, cfg, [_op("a", days_ago=900)])
    with pytest.raises(ValueError):
        journal.compact(vault, cfg, policy=policy, value=value, dry_run=False)
    assert journal.stats(vault, cfg)["ops"] == 1


# ── the legacy (not-yet-migrated) journal ──────────────────────────────────
#
# `load()` migrates a legacy manifest.json on first touch; the read-only
# projections added in this batch did not, and every surface that shows the
# journal's size or history goes through THEM. On an install upgrading from
# before the SQLite migration the Restore panel therefore reported an empty
# history over a full one, `journal-stats` reported 0 bytes (which also
# suppressed the Compact control, since the header renders nothing at ops==0),
# and `papermind status` dropped its Tag Journal line.

def _legacy_manifest(vault_root, cfg, n_ops):
    """Write a pre-SQLite manifest.json with *n_ops* applied external ops."""
    arch = journal.archive_dir(vault_root, cfg)
    arch.mkdir(parents=True, exist_ok=True)
    ops = [
        {"op_id": f"legacy{i:04d}", "kind": "finder_tag", "state": "applied",
         "ts": "2026-01-01T00:00:00+00:00", "citation_key": f"k{i}",
         "added": ["dep"], "before_raw": [], "after_raw": ["dep"]}
        for i in range(n_ops)
    ]
    (arch / "manifest.json").write_text(
        json.dumps({"version": 1, "vault_key": journal._vault_key(vault_root),
                    "ops": ops}),
        encoding="utf-8")
    return ops


def test_stats_reports_a_legacy_journal_rather_than_zero(tmp_path):
    vault, cfg = _roots(tmp_path)
    _legacy_manifest(vault, cfg, 7)
    assert not journal.manifest_path(vault, cfg).exists()
    st = journal.stats(vault, cfg)
    assert st["ops"] == 7, "an unmigrated history must not report as empty"
    assert st["protected"] == 7
    assert st["bytes"] > 0


def test_load_page_serves_a_legacy_journal_rather_than_zero(tmp_path):
    vault, cfg = _roots(tmp_path)
    _legacy_manifest(vault, cfg, 7)
    page = journal.load_page(vault, cfg, limit=100)
    assert page["total"] == 7
    assert len(page["ops"]) == 7
    # newest first, like the route's contract
    assert page["ops"][0]["op_id"] == "legacy0006"


def test_the_projections_still_report_zero_when_there_is_no_journal(tmp_path):
    """The fast path must survive: no db AND no legacy file is a real zero."""
    vault, cfg = _roots(tmp_path)
    assert journal.stats(vault, cfg)["ops"] == 0
    assert journal.load_page(vault, cfg)["total"] == 0
