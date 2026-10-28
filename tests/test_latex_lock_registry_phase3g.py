"""AP-11 canonical-path serialization, concurrency-cap, and cleanup tests."""

from __future__ import annotations

import threading
import time
import unittest

from api.routes._latexdoc import LatexPathLockRegistry


def test_aliases_of_the_same_path_serialize_and_cleanup(tmp_path):
    registry = LatexPathLockRegistry(global_cap=2)
    project = tmp_path / "project"
    project.mkdir()
    doc = project / "paper.tex"
    doc.write_text("x", encoding="utf-8")
    alias = project / ".." / "project" / "paper.tex"

    first = registry.acquire(doc)
    assert first is not None
    assert registry.acquire(alias) is None
    assert registry.active_key_count() == 1
    first.release()
    assert registry.active_key_count() == 0


def test_distinct_paths_progress_within_global_cap_and_third_is_bounded(tmp_path):
    registry = LatexPathLockRegistry(global_cap=2)
    first = registry.acquire(tmp_path / "a.tex")
    second = registry.acquire(tmp_path / "b.tex")
    assert first is not None
    assert second is not None
    assert registry.acquire(tmp_path / "c.tex") is None

    first.release()
    third = registry.acquire(tmp_path / "c.tex")
    assert third is not None
    second.release()
    third.release()
    assert registry.active_key_count() == 0


def test_two_unrelated_operations_can_execute_concurrently(tmp_path):
    registry = LatexPathLockRegistry(global_cap=2)
    entered = threading.Barrier(3)
    release = threading.Event()
    failures = []

    def run(path):
        lease = registry.acquire(path)
        if lease is None:
            failures.append(path)
            return
        try:
            entered.wait(timeout=2)
            release.wait(2)
        finally:
            lease.release()

    threads = [
        threading.Thread(target=run, args=(tmp_path / "a.tex",)),
        threading.Thread(target=run, args=(tmp_path / "b.tex",)),
    ]
    for thread in threads:
        thread.start()
    entered.wait(timeout=2)
    release.set()
    for thread in threads:
        thread.join(2)
    assert not failures
    assert registry.active_key_count() == 0


def test_lease_release_is_idempotent_for_overlapping_cleanup_paths(tmp_path):
    registry = LatexPathLockRegistry(global_cap=1)
    lease = registry.acquire(tmp_path / "a.tex")
    assert lease is not None
    lease.release()
    lease.release()
    assert registry.active_key_count() == 0
    again = registry.acquire(tmp_path / "a.tex")
    assert again is not None
    again.release()


class TestLeaseTtlSteal(unittest.TestCase):
    """PERF (2026-09-02): the lease had no TTL, so a wedged worker kept it
    forever.

    Release fired only when the worker exited, and with
    ``local_request_timeout_s`` defaulting to 0 a worker wedged in
    ``client.chat()`` never returns — one lease AND one of the two global slots
    were consumed until the process restarted. ``RagOperationLock`` exists
    precisely because a zombie must not keep a lock forever; this is its model.
    """

    def test_an_expired_lease_is_stolen_and_its_slot_inherited(self):
        reg = LatexPathLockRegistry(global_cap=2)
        a = reg.acquire("/tmp/deck.tex", ttl_s=0.05)
        self.assertIsNotNone(a)
        # While the TTL is live, a second acquirer still gets the 409 path.
        self.assertIsNone(reg.acquire("/tmp/deck.tex", ttl_s=0.05))
        time.sleep(0.1)
        b = reg.acquire("/tmp/deck.tex", ttl_s=60)
        self.assertIsNotNone(b, "an expired lease was never reclaimable")
        # The steal INHERITED the slot rather than taking a second one, so the
        # other global slot is still free — otherwise two steals would exhaust
        # the semaphore permanently, the same leak one level down.
        other = reg.acquire("/tmp/other.tex", ttl_s=60)
        self.assertIsNotNone(other, "the steal consumed a second global slot")
        assert other is not None
        other.release()
        assert b is not None
        b.release()

    def test_a_stolen_lease_can_neither_release_nor_heartbeat(self):
        reg = LatexPathLockRegistry(global_cap=2)
        a = reg.acquire("/tmp/deck.tex", ttl_s=0.05)
        assert a is not None
        time.sleep(0.1)
        b = reg.acquire("/tmp/deck.tex", ttl_s=60)
        assert b is not None
        self.assertFalse(a.valid)
        self.assertFalse(a.heartbeat())
        a.release()          # the zombie finally returns
        # …and must NOT have freed the lock the new holder owns.
        self.assertIsNone(reg.acquire("/tmp/deck.tex", ttl_s=60))
        self.assertTrue(b.valid)
        b.release()
        self.assertIsNotNone(reg.acquire("/tmp/deck.tex", ttl_s=60))

    def test_a_heartbeating_holder_is_never_stolen(self):
        reg = LatexPathLockRegistry(global_cap=2)
        a = reg.acquire("/tmp/deck.tex", ttl_s=0.2)
        assert a is not None
        for _ in range(5):
            time.sleep(0.1)
            self.assertTrue(a.heartbeat())
            self.assertIsNone(reg.acquire("/tmp/deck.tex", ttl_s=0.2),
                              "a live, heartbeating holder was stolen")
        a.release()

    def test_repeated_steals_do_not_exhaust_the_semaphore(self):
        reg = LatexPathLockRegistry(global_cap=2)
        lease = None
        for _ in range(6):
            lease = reg.acquire("/tmp/deck.tex", ttl_s=0.02)
            self.assertIsNotNone(lease, "the global slots leaked across steals")
            time.sleep(0.04)
        # Six steals charged exactly ONE slot (each inherited the last), so the
        # other is still free; releasing the survivor frees both.
        x = reg.acquire("/tmp/a.tex", ttl_s=60)
        self.assertIsNotNone(x)
        assert lease is not None and x is not None
        lease.release()
        x.release()
        y = reg.acquire("/tmp/b.tex", ttl_s=60)
        z = reg.acquire("/tmp/c.tex", ttl_s=60)
        self.assertIsNotNone(y)
        self.assertIsNotNone(z)


class TestSseHeartbeatStopsAStolenWorker(unittest.TestCase):
    """A stolen lease must stop its worker BEFORE it writes.

    ``run_sse_worker`` beats the lease on every frame and sets the worker's
    cancel event when the beat is refused, so every existing
    ``if cancel.is_set(): return`` gate in front of a write covers the steal
    case with no per-site change.
    """

    def test_a_refused_beat_cancels_the_worker(self):
        import json

        from flask import Flask

        from api.sse import run_sse_worker

        wrote = []
        beats = []

        def _heartbeat():
            beats.append(1)
            return len(beats) < 2          # the second beat is refused

        def _worker(put, cancel):
            for i in range(5):
                put({"info": f"stage {i}"})
                if cancel.is_set():
                    return                 # the existing pre-write gate
                wrote.append(i)

        app = Flask(__name__)
        with app.test_request_context("/"):
            resp = run_sse_worker(_worker, consumer_timeout_s=5,
                                  heartbeat=_heartbeat)
            body = "".join(
                chunk.decode() if isinstance(chunk, bytes) else chunk
                for chunk in resp.response)
        frames = [json.loads(ln[6:]) for ln in body.splitlines()
                  if ln.startswith("data: ") and ln[6:] != "[DONE]"]
        self.assertEqual(wrote, [0], f"the worker wrote after the steal: {wrote}")
        self.assertTrue(any("reclaimed" in str(f.get("error", "")) for f in frames),
                        f"the lock loss was silent: {frames}")
