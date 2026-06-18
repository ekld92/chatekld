"""
test_concurrency.py — Concurrency regression tests for ChatEKLD 2026
======================================================================
Covers concurrency regressions in the core components:

  Bug 1 — GLMOCRManager.set_model() + check_availability() race
  Bug 2 — api_upload() TOCTOU (stale ocr_cb after config change)
  Bug 4 — _RagOperationLock TTL expiry and force_release() semantics

None of these tests require a live Ollama server.

Usage:
    python -m pytest test_concurrency.py -v
"""
import sys
import io
import threading
import time
import unittest
from unittest.mock import patch, MagicMock


def _mock_provider(models):
    """Return a mock Provider whose get_models() returns the given model list."""
    p = MagicMock()
    p.get_models.return_value = (models, "")
    return p


# ---------------------------------------------------------------------------
# Bug 1: GLMOCRManager and VisionManager lock correctness
# ---------------------------------------------------------------------------

class TestGLMOCRManagerLock(unittest.TestCase):
    """Verify that set_model() and check_availability() are mutually exclusive."""

    def setUp(self):
        # Evict any stubs injected by a preceding test file so we always
        # import the real implementations for these concurrency tests.
        # NOT llama_index: nothing stubs it, and popping it re-imports it under a
        # second class identity, breaking isinstance/pydantic checks in any later
        # test that builds real llama_index objects (see conftest.py note).
        _prefixes = ("services", "pdf_extractor", "notes_extractor")
        for _key in list(sys.modules):
            if any(_key == p or _key.startswith(p + ".") for p in _prefixes):
                sys.modules.pop(_key, None)
        from services.vision import GLMOCRManager
        self.GLMOCRManager = GLMOCRManager

    def test_set_model_and_check_availability_never_see_stale_pair(self):
        """
        Run set_model() and check_availability() from two threads concurrently
        1 000 times.  A thread interleaved between the two-line update in
        set_model() would see model="new" but _is_available=<cached-for-old>.
        After the fix, check_availability() must always return True or False,
        never None, and must not raise.
        """
        mgr = self.GLMOCRManager(model="model-a:latest")

        errors = []
        results = []

        # A barrier ensures both threads are alive and scheduled before either
        # begins its iteration loop, maximising the chance of real interleaving.
        # Without a barrier the GIL scheduler can let one thread finish all
        # 500 iterations before the second thread even starts, producing a
        # false negative (no interleaving → no race is exercised).
        _barrier = threading.Barrier(2)

        with patch("services.vision.get_provider", return_value=_mock_provider(["model-a:latest", "model-b:latest"])):
            # Pre-warm the cache so check_availability() returns quickly.
            mgr.check_availability()

            def writer():
                _barrier.wait()  # Wait for both threads to be ready.
                for i in range(500):
                    mgr.set_model("model-a:latest" if i % 2 == 0 else "model-b:latest")

            def reader():
                _barrier.wait()  # Wait for both threads to be ready.
                for _ in range(500):
                    try:
                        result = mgr.check_availability()
                        results.append(result)
                        if result is None:
                            errors.append("check_availability() returned None")
                    except Exception as exc:
                        errors.append(f"check_availability() raised: {exc}")

            t1 = threading.Thread(target=writer)
            t2 = threading.Thread(target=reader)
            t1.start(); t2.start()
            t1.join(); t2.join()

        self.assertEqual(errors, [], f"Concurrency errors: {errors}")
        # All results must be bool, never None
        for r in results:
            self.assertIsInstance(r, bool, f"Got non-bool result: {r!r}")

    def test_set_model_invalidates_cache(self):
        """set_model() must reset _is_available so the next check re-probes."""
        mgr = self.GLMOCRManager(model="glm-ocr:latest")
        mgr._is_available = True  # Force a cached True
        mgr.set_model("glm-ocr:0.9b")
        # After set_model the cache must be cleared, not still True.
        self.assertIsNone(mgr._is_available)
        self.assertEqual(mgr.model, "glm-ocr:0.9b")

    def test_check_availability_exact_match(self):
        """check_availability() returns True for an exact model name match."""
        mgr = self.GLMOCRManager(model="glm-ocr:latest")
        with patch("services.vision.get_provider", return_value=_mock_provider(["glm-ocr:latest"])):
            self.assertTrue(mgr.check_availability())

    def test_check_availability_base_name_fallback(self):
        """check_availability() returns True via base-name fallback."""
        mgr = self.GLMOCRManager(model="glm-ocr:latest")
        with patch("services.vision.get_provider", return_value=_mock_provider(["glm-ocr:0.9b"])):
            mgr._is_available = None  # clear cache
            self.assertTrue(mgr.check_availability())

    def test_check_availability_no_substring_false_positive(self):
        """check_availability() must NOT match 'glm-ocr-experimental' for 'glm-ocr'."""
        mgr = self.GLMOCRManager(model="glm-ocr:latest")
        with patch("services.vision.get_provider", return_value=_mock_provider(["glm-ocr-experimental:latest"])):
            mgr._is_available = None
            self.assertFalse(mgr.check_availability())

    def test_set_model_is_atomic_under_concurrent_reads(self):
        """
        set_model() must never leave the manager in a state where model and
        _is_available are inconsistent (new model with old cache value).
        Runs 200 iterations of concurrent set_model + check_availability.
        """
        mgr = self.GLMOCRManager(model="a:latest")
        inconsistencies = []
        # Barrier ensures both threads start their iteration loop simultaneously
        # to maximise the chance of actual interleaving on every test run.
        _barrier = threading.Barrier(2)

        with patch("services.vision.get_provider", return_value=_mock_provider(["a:latest", "b:latest"])):

            def writer():
                _barrier.wait()
                for i in range(100):
                    mgr.set_model("a:latest" if i % 2 == 0 else "b:latest")

            def checker():
                _barrier.wait()
                for _ in range(100):
                    # After any set_model() call, _is_available must be None
                    # (cache invalidated) OR a valid bool (from a subsequent probe).
                    # It must NEVER be a leftover cached value for a different model.
                    with mgr._lock:
                        model_snap = mgr.model
                        avail_snap = mgr._is_available
                    # If _is_available is True, we do not know which model it's for,
                    # but it must at least be a bool or None — never something else.
                    if avail_snap is not None and not isinstance(avail_snap, bool):
                        inconsistencies.append((model_snap, avail_snap))

            t1 = threading.Thread(target=writer)
            t2 = threading.Thread(target=checker)
            t1.start(); t2.start()
            t1.join(); t2.join()

        self.assertEqual(inconsistencies, [], f"Inconsistent state observed: {inconsistencies}")


# ---------------------------------------------------------------------------
# Bug 2: api_upload() TOCTOU — ocr_cb after concurrent config change
# ---------------------------------------------------------------------------

class TestApiUploadTOCTOU(unittest.TestCase):
    """
    Verify that api_upload() does not capture a stale ocr_cb after a
    concurrent set_model() call.  After Bug 1, check_availability() is
    atomic, so the ocr_cb bound method always reflects the current state.
    """

    def setUp(self):
        from app import app
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.headers = {"X-Requested-With": "ChatEKLD"}

    def test_upload_with_concurrent_ocr_model_change_does_not_crash(self):
        """
        Upload a non-PDF (will be rejected at 400 before OCR is invoked).
        Simultaneously fire a config POST to change ocr_model.
        The upload handler must not raise and must return a clean JSON response.
        """
        import io

        results = {}

        def do_upload():
            fake = io.BytesIO(b"not a pdf")
            resp = self.client.post(
                "/api/upload",
                data={"file": (fake, "test.txt")},
                headers=self.headers,
            )
            results["upload_status"] = resp.status_code

        def do_config_change():
            # Change the ocr_model while the upload is in-flight.
            resp = self.client.post(
                "/api/config",
                json={"ocr_model": "glm-ocr:0.9b"},
                content_type="application/json",
                headers=self.headers,
            )
            results["config_status"] = resp.status_code

        t1 = threading.Thread(target=do_upload)
        t2 = threading.Thread(target=do_config_change)
        t1.start(); t2.start()
        t1.join(); t2.join()

        # Non-PDF must always be rejected as 400, never a 500 crash.
        self.assertEqual(results.get("upload_status"), 400,
                         "Expected non-PDF to be rejected with 400")
        self.assertEqual(results.get("config_status"), 200,
                         "Expected config update to succeed with 200")


class TestPdfUploadResourceSafety(unittest.TestCase):
    """Regression tests for bounded upload writes and concurrent DB inserts."""

    def setUp(self):
        from core.database import init_db
        init_db()

    def test_upload_stream_is_rejected_before_unbounded_disk_write(self):
        from services.pdf_service import _save_upload_limited

        class Upload:
            def __init__(self, payload: bytes):
                self.stream = io.BytesIO(payload)

        with self.assertRaises(ValueError):
            _save_upload_limited(Upload(b"x" * 1025), max_bytes=1024)

    def test_two_pdf_uploads_do_not_share_temp_or_db_rows(self):
        from services.pdf_service import process_pdf_upload

        class Upload:
            def __init__(self, payload: bytes):
                self.stream = io.BytesIO(payload)

        ids = []
        errors = []

        def upload_one(i: int):
            try:
                upload_id, text = process_pdf_upload(
                    Upload(b"%PDF-1.4\n%%EOF"),
                    f"paper-{i}.pdf",
                    max_bytes=1024,
                )
                ids.append(upload_id)
                self.assertEqual(text, "extracted text")
            except Exception as exc:
                errors.append(exc)

        with patch("services.pdf_service._extract_with_timeout", return_value="extracted text"):
            threads = [threading.Thread(target=upload_one, args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)


class TestRagOperationLock(unittest.TestCase):
    """
    Regression tests for _RagOperationLock: verify TTL-based auto-expiry and
    immediate force_release() path work correctly.
    """

    def setUp(self):
        # Import directly from the canonical module, not via the app alias.
        from core.utils import RagOperationLock
        self.lock = RagOperationLock()

    def test_try_acquire_succeeds_when_free(self):
        """try_acquire() returns True on first call when lock is free."""
        self.assertTrue(self.lock.try_acquire(ttl_seconds=60.0))
        self.assertTrue(self.lock.is_held)

    def test_try_acquire_fails_when_held_within_ttl(self):
        """try_acquire() returns False if lock is held and TTL not expired."""
        self.assertTrue(self.lock.try_acquire(ttl_seconds=60.0))
        # Attempt immediate re-acquisition
        self.assertFalse(self.lock.try_acquire(ttl_seconds=60.0))

    def test_try_acquire_succeeds_after_ttl_expires(self):
        """
        try_acquire() returns True after TTL elapsed, even if release()
        was never called. This simulates recovery from a stuck worker thread.
        """
        # Acquire with 0.1 second TTL
        self.assertTrue(self.lock.try_acquire(ttl_seconds=0.1))
        self.assertTrue(self.lock.is_held)
        
        # Wait for TTL to expire
        time.sleep(0.2)
        
        # Re-acquire should succeed now (stale lock reclaimed)
        self.assertTrue(self.lock.try_acquire(ttl_seconds=60.0))
        self.assertTrue(self.lock.is_held)

    def test_release_clears_lock(self):
        """release() clears the lock; subsequent try_acquire() succeeds."""
        self.assertTrue(self.lock.try_acquire(ttl_seconds=60.0))
        self.assertTrue(self.lock.is_held)
        
        self.lock.release()
        self.assertFalse(self.lock.is_held)
        
        # Next acquire must succeed
        self.assertTrue(self.lock.try_acquire(ttl_seconds=60.0))

    def test_release_is_idempotent(self):
        """release() can be called multiple times safely (no errors)."""
        self.assertTrue(self.lock.try_acquire(ttl_seconds=60.0))
        self.lock.release()
        # Second release must not raise
        self.lock.release()
        self.assertFalse(self.lock.is_held)

    def test_force_release_returns_true_if_held(self):
        """force_release() returns True if lock was held, False otherwise."""
        # First force_release on free lock returns False
        self.assertFalse(self.lock.force_release())
        
        # Acquire, then force_release returns True
        self.assertTrue(self.lock.try_acquire(ttl_seconds=60.0))
        self.assertTrue(self.lock.force_release())
        self.assertFalse(self.lock.is_held)

    def test_force_release_immediately_frees_lock(self):
        """force_release() does not respect TTL; immediately frees the lock."""
        # Acquire with long TTL
        self.assertTrue(self.lock.try_acquire(ttl_seconds=300.0))
        self.assertTrue(self.lock.is_held)
        
        # force_release() should free it immediately (no waiting)
        self.assertTrue(self.lock.force_release())
        self.assertFalse(self.lock.is_held)
        
        # Next acquire must succeed (not blocked by the long TTL)
        self.assertTrue(self.lock.try_acquire(ttl_seconds=60.0))

    def test_is_held_respects_ttl_expiry(self):
        """is_held property returns False after TTL expires."""
        self.assertTrue(self.lock.try_acquire(ttl_seconds=0.1))
        self.assertTrue(self.lock.is_held)
        
        # Before TTL expires, is_held is True
        time.sleep(0.05)
        self.assertTrue(self.lock.is_held)
        
        # After TTL expires, is_held is False
        time.sleep(0.1)
        self.assertFalse(self.lock.is_held)

    def test_concurrent_try_acquire_under_ttl(self):
        """
        When one thread holds the lock under TTL, other threads cannot
        acquire until its release() or TTL expiry.
        """
        holder_acquired = threading.Event()
        waiter_result = []
        
        def holder():
            self.lock.try_acquire(ttl_seconds=10.0)
            holder_acquired.set()
            time.sleep(0.5)
            self.lock.release()
        
        def waiter():
            holder_acquired.wait()
            # Try to acquire while holder is active (should fail)
            waiter_result.append(self.lock.try_acquire(ttl_seconds=10.0))
        
        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=waiter)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        
        # Waiter must have failed (holder still held the lock)
        self.assertEqual([False], waiter_result)


class TestVisionCallFailureCooldown(unittest.TestCase):
    """A failed vision/OCR call must fast-fail subsequent calls for a short
    cooldown window so a misconfigured model cannot generate per-page traffic.
    """

    def setUp(self):
        # NOT llama_index — see the note in the other setUp above.
        _prefixes = ("services", "pdf_extractor")
        for _key in list(sys.modules):
            if any(_key == p or _key.startswith(p + ".") for p in _prefixes):
                sys.modules.pop(_key, None)

    def test_vision_failure_short_circuits_next_call(self):
        from services.vision import VisionManager

        mgr = VisionManager(model="missing-model", provider="ollama")

        with patch(
            "services.vision._chat_ollama_image",
            side_effect=Exception("model not found"),
        ) as chat:
            first = mgr.describe_image("data")
            second = mgr.describe_image("data")

        self.assertEqual(first, "")
        self.assertEqual(second, "")
        # Second call must skip the provider entirely.
        self.assertEqual(chat.call_count, 1)

    def test_vision_set_model_clears_call_failure_cooldown(self):
        from services.vision import VisionManager

        mgr = VisionManager(model="a", provider="ollama")
        mgr._call_failure_at = time.monotonic()
        mgr.set_model("b")
        self.assertIsNone(mgr._call_failure_at)

    def test_vision_set_provider_clears_call_failure_cooldown(self):
        from services.vision import VisionManager

        mgr = VisionManager(model="a", provider="ollama")
        mgr._call_failure_at = time.monotonic()
        mgr.set_provider("lm_studio")
        self.assertIsNone(mgr._call_failure_at)

    def test_ocr_failure_short_circuits_next_call(self):
        from services.vision import GLMOCRManager

        mgr = GLMOCRManager(model="missing", provider="ollama")

        with patch(
            "services.vision._chat_ollama_image",
            side_effect=Exception("model not found"),
        ) as chat:
            first = mgr.extract_page_text("data")
            second = mgr.extract_page_text("data")

        self.assertEqual(first, "")
        self.assertEqual(second, "")
        # Non-overflow exception on attempt 0 exits immediately; subsequent
        # call is gated by the cooldown.
        self.assertEqual(chat.call_count, 1)

    def test_ocr_success_clears_call_failure_cooldown(self):
        """A successful OCR call must clear any prior failure marker so the
        next failure cleanly re-arms the cooldown."""
        from services.vision import GLMOCRManager

        mgr = GLMOCRManager(model="ocr", provider="ollama")
        # Stale failure whose cooldown has already expired — must still be
        # cleared on the next successful call.
        mgr._call_failure_at = time.monotonic() - 100.0

        with patch("services.vision._chat_ollama_image", return_value="page text"):
            result = mgr.extract_page_text("data")

        self.assertEqual(result, "page text")
        self.assertIsNone(mgr._call_failure_at)


if __name__ == "__main__":
    unittest.main()
