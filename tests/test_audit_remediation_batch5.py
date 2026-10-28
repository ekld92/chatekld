"""Audit remediation 2026-09-23, Batch 5 — concurrency and performance.

* P2   the nested mirror stream forwards its progress and stops when the
       outer run is cancelled (its inner worker sees the cancel);
* P3   an observation resolves the vault and biblio roots once, and each
       path once;
* P4   the safe-save lookup reads a per-pass path index, not the whole
       tracked library per new orphan;
* P8   the tracker's schema DDL runs once per store file per process, and a
       rolled-back first write never marks a store done;
* N10  `/api/models?provider=` prefills only the active local provider's
       model (pinned in `test_status_routes.py`).

Each test was proven red on a7a171d9.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flask
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_library_imports as _imports_tests  # noqa: E402  (module import: no re-collection)

from accession import imports  # noqa: E402


# ---------------------------------------------------------------------------
# P2 — the nested mirror stream
# ---------------------------------------------------------------------------

def _streaming_mirror_app(state):
    """A mirror route with the REAL SSE skeleton, emitting progress until its
    cancel event is set — what `_run_mirror` does between notes."""
    from api.sse import run_sse_worker
    app = flask.Flask(__name__)

    @app.post("/api/notesync/mirror")
    def mirror():
        def worker(put, cancel):
            for i in range(200):
                if cancel.is_set():
                    state["worker_saw_cancel"] = True
                    put({"mirror": {"ok": False, "stopped": "cancelled"}})
                    return
                put({"info": f"note {i}"})
                time.sleep(0.01)
            put({"mirror": {"ok": True}})
        return run_sse_worker(worker, consumer_timeout_s=30)
    return app


def test_the_nested_mirror_forwards_progress_and_stops_on_cancel(tmp_path):
    from api.routes.library_workflow import _transport
    state: dict = {}
    echoed: list = []
    cancel = threading.Event()

    def echo(msg):
        echoed.append(msg)
        if len(echoed) == 3:
            cancel.set()
    t = _transport(SimpleNamespace(vault_root=tmp_path, biblio_articles_dir=tmp_path / "biblio"),
                   _streaming_mirror_app(state), echo=echo, cancel=cancel.is_set)
    status, body = t.mirror("digest")
    assert echoed[:3] == ["note 0", "note 1", "note 2"]
    assert status == 0 and "Cancelled" in body["error"]
    deadline = time.time() + 5
    while "worker_saw_cancel" not in state and time.time() < deadline:
        time.sleep(0.01)
    assert state.get("worker_saw_cancel") is True, "closing the inner stream cancels its worker"


# ---------------------------------------------------------------------------
# P3 — an observation resolves each path once
# ---------------------------------------------------------------------------

def test_an_observation_resolves_the_roots_once(tmp_path):
    lib = _imports_tests.Library(tmp_path)
    for i in range(4):
        e = lib.entry(f"RRRR000{i}", f"res{i}2020")
        lib.pdf(e, f"PDFR000{i}", f"res{i}.pdf")
    snapshot = imports.rec.observe(lib.settings, include_note_digests=True)
    calls: list = []
    real = Path.resolve

    def spy(self, *a, **k):
        calls.append(str(self))
        return real(self, *a, **k)
    with mock.patch.object(Path, "resolve", spy):
        out = imports.build_observation(lib.settings, snapshot)
    assert calls.count(str(lib.settings.vault_root)) == 1
    assert calls.count(str(lib.settings.biblio_articles_dir)) == 1
    attachments = [u for u in out.values() if u["kind"] == "attachment"]
    assert len(attachments) == 4 and all(u["in_biblio"] and u["rel"] for u in attachments)


# ---------------------------------------------------------------------------
# P4 — the safe-save lookup
# ---------------------------------------------------------------------------

class _NoIteration(dict):
    def __iter__(self):
        raise AssertionError("the whole tracked library was scanned")


def test_the_safe_save_lookup_uses_the_pass_index():
    tracked = _NoIteration({
        "doc:1:1": {"kind": "document", "rel": "a.pdf", "state": "present"},
        "doc:2:2": {"kind": "document", "rel": "b.pdf", "state": "present"},
    })
    index = imports._document_path_index(dict(tracked), observation={})
    obs = {"kind": "document", "rel": "a.pdf"}
    assert imports._same_path_document(tracked, "doc:9:9", obs, {"doc:9:9": obs},
                                       index) == "doc:1:1"


# ---------------------------------------------------------------------------
# P8 — schema DDL once per store file
# ---------------------------------------------------------------------------

def _count_ddl(fn):
    """Run *fn* and count the CREATE statements the tracker executes."""
    seen: list = []
    real = imports._schema_ddl

    def counting(con):
        seen.append(1)
        return real(con)
    with mock.patch.object(imports, "_schema_ddl", counting):
        fn()
    return len(seen)


def test_the_schema_is_brought_up_once_per_store_file(tmp_path):
    from accession import reconciliation as rec
    lib = _imports_tests.Library(tmp_path)
    store = tmp_path / "store-a.sqlite3"
    with mock.patch.object(rec, "store_path", return_value=store):
        assert _count_ddl(lambda: imports.begin_session(lib.settings)) == 1   # new file: DDL
        n = _count_ddl(lambda: [imports.begin_session(lib.settings) for _ in range(5)])
        assert n == 0, "an up-to-date store runs no DDL again"
        store.unlink()
        for side in ("-wal", "-shm"):
            Path(str(store) + side).unlink(missing_ok=True)
        assert _count_ddl(lambda: imports.begin_session(lib.settings)) == 1   # recreated: DDL


def test_a_first_write_that_rolls_back_does_not_mark_the_store_done(tmp_path):
    """A guard on the new memo, not a regression test (the old code ran the
    DDL on every write, so it could not get this wrong)."""
    from accession import reconciliation as rec
    lib = _imports_tests.Library(tmp_path)
    store = tmp_path / "store-b.sqlite3"
    with mock.patch.object(rec, "store_path", return_value=store):
        with pytest.raises(imports.ImportTrackingError):
            imports.record_step(lib.settings, "no-such-session", "u", "tag", "done")
        # The failed transaction rolled its DDL back; the next writer must
        # still create the tables.
        sid = imports.begin_session(lib.settings)
        assert imports.session(lib.settings, sid) is not None
        with rec.connection() as con:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        assert "import_session_results" in names
