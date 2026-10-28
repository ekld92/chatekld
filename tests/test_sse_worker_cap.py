"""PERF (2026-09-02): a process-wide cap on LIVE SSE workers.

Every streaming route spawned a daemon worker per request with no
process-wide bound (19 call sites across 9 route modules). A wedged local
backend accumulates one blocked thread per retry, and the threads are
daemons — invisible to the Waitress pool bound.

The cap must refuse with a real 503 (not an error frame inside a 200), must
NOT strand a caller-held op lock when it refuses, and must give the slot back
under the same guard that owns the lock: when the stream has ended AND any
spawned worker has exited.
"""
import threading
import unittest
from unittest import mock

import flask

from api import sse


def _app():
    app = flask.Flask(__name__)
    app.config["TESTING"] = True
    return app


class TestSSEWorkerCap(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        # A small cap keeps the test honest and fast; the constant itself is
        # asserted separately so a future change is deliberate.
        self.cap = 2
        # create=True so the ASSERTIONS carry the red against pre-change code
        # rather than an AttributeError on names that are merely new.
        p = mock.patch.object(sse, "_MAX_LIVE_SSE_WORKERS", self.cap, create=True)
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(sse, "_LIVE_SSE_SLOTS",
                               threading.BoundedSemaphore(self.cap), create=True)
        p2.start()
        self.addCleanup(p2.stop)

    def _stream(self, release=None, on_stream_finished=None, block=None):
        """Start a stream and return (response, first_chunk_iterator)."""
        def worker(put, cancel):
            put({"info": "started"})
            if block is not None:
                block.wait(5)
            put({"done": True})

        with self.app.test_request_context("/x"):
            resp = sse.run_sse_worker(
                worker, consumer_timeout_s=5, release=release,
                on_stream_finished=on_stream_finished,
            )
        return resp

    def test_a_stream_past_the_cap_is_refused_with_503(self):
        block = threading.Event()
        self.addCleanup(block.set)
        held = [self._stream(block=block) for _ in range(self.cap)]
        self.addCleanup(lambda: [r.close() for r in held])
        # Iterate one frame from each so the workers are actually running.
        for r in held:
            next(r.response.__iter__())

        refused = self._stream()
        self.assertEqual(refused.status_code, 503)
        body = refused.get_json()
        self.assertIn("streaming operations", body["error"])

    def test_the_refusal_hands_back_the_caller_s_op_lock(self):
        """A route acquires its lock, then calls us: a refusal must release it
        or the cap would strand exactly the locks Phase 0 stopped leaking."""
        block = threading.Event()
        self.addCleanup(block.set)
        held = [self._stream(block=block) for _ in range(self.cap)]
        self.addCleanup(lambda: [r.close() for r in held])
        for r in held:
            next(r.response.__iter__())

        lock = threading.Lock()
        self.assertTrue(lock.acquire(blocking=False))
        observed = []
        refused = self._stream(release=lock.release,
                               on_stream_finished=lambda: observed.append(1))
        self.assertEqual(refused.status_code, 503)
        self.assertTrue(lock.acquire(blocking=False), "op lock was stranded")
        lock.release()
        self.assertEqual(observed, [1], "the stream observer never ran")

    def test_a_finished_stream_gives_its_slot_back(self):
        for _ in range(self.cap + 3):
            resp = self._stream()
            self.assertEqual(resp.status_code, 200)
            list(resp.response)          # drain: worker runs and the stream ends
            resp.close()
        live = getattr(sse, "live_sse_workers", lambda: 0)
        self.assertEqual(live(), 0)

    def test_response_releases_the_pool_it_acquired(self):
        original = sse._LIVE_SSE_SLOTS
        response = self._stream()
        self.assertEqual(original._value, self.cap - 1)
        replacement = threading.BoundedSemaphore(self.cap)
        with mock.patch.object(sse, "_LIVE_SSE_SLOTS", replacement):
            response.close()
        self.assertEqual(original._value, self.cap)
        self.assertEqual(replacement._value, self.cap)


class TestShippedCap(unittest.TestCase):
    """Asserted outside the patched fixture above, so this reads the real
    constant: small enough to bound a leak, large enough for one vault chat
    plus one deck plus a batch at the same time."""

    def test_the_shipped_cap_is_a_deliberate_number(self):
        cap = getattr(sse, "_MAX_LIVE_SSE_WORKERS", 0)
        self.assertGreaterEqual(cap, 8)
        self.assertLessEqual(cap, 32)


if __name__ == "__main__":
    unittest.main()
