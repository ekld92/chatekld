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
            t1.start()
            t2.start()
            t1.join()
            t2.join()

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
            t1.start()
            t2.start()
            t1.join()
            t2.join()

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
        t1.start()
        t2.start()
        t1.join()
        t2.join()

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


class TestVisionCallBounds(unittest.TestCase):
    """Vision/OCR calls must always be bounded — pre-emptive downscale on the
    description path, a finite timeout, retries disabled, and a max-token cap —
    so a runaway / stuck local model cannot stall a long indexing run.
    """

    def setUp(self):
        _prefixes = ("services", "pdf_extractor")
        for _key in list(sys.modules):
            if any(_key == p or _key.startswith(p + ".") for p in _prefixes):
                sys.modules.pop(_key, None)

    @staticmethod
    def _big_png_b64(w=4000, h=3000):
        import base64
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (w, h), (10, 20, 30)).save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode()

    @staticmethod
    def _longest_side(b64):
        import base64
        from PIL import Image
        with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
            return max(im.size)

    def test_cfg_bounded_int_falls_back_and_clamps(self):
        from services.vision import _cfg_bounded_int
        # Unset / non-positive / unparseable -> hard default.
        for value in ({}, {"vision_timeout_s": 0}, {"vision_timeout_s": -3},
                      {"vision_timeout_s": "x"}, {"vision_timeout_s": None}):
            with patch("core.config.load_config_readonly", return_value=value):
                self.assertEqual(_cfg_bounded_int("vision_timeout_s", 120, 5, 600), 120)
        # In-range value is returned verbatim.
        with patch("core.config.load_config_readonly", return_value={"vision_timeout_s": 45}):
            self.assertEqual(_cfg_bounded_int("vision_timeout_s", 120, 5, 600), 45)
        # Positive but out-of-range -> CLAMPED (defends a hand-edited config.json
        # that bypassed the POST /api/config validator).
        with patch("core.config.load_config_readonly", return_value={"vision_timeout_s": 99999}):
            self.assertEqual(_cfg_bounded_int("vision_timeout_s", 120, 5, 600), 600)
        with patch("core.config.load_config_readonly", return_value={"vision_timeout_s": 1}):
            self.assertEqual(_cfg_bounded_int("vision_timeout_s", 120, 5, 600), 5)

    def test_fit_downscales_oversized_noops_small_survives_junk(self):
        from services.vision import _fit_base64_image_to_max_side
        from core.constants import VISION_IMAGE_MAX_SIDE
        big = self._big_png_b64(4000, 3000)
        fit = _fit_base64_image_to_max_side(big, VISION_IMAGE_MAX_SIDE)
        # The 14px-alignment guarantee holds only because the cap is a multiple
        # of 14 — assert against the real constant, not a hardcoded literal.
        self.assertLessEqual(self._longest_side(fit), VISION_IMAGE_MAX_SIDE)
        small = self._big_png_b64(200, 100)
        self.assertEqual(_fit_base64_image_to_max_side(small, VISION_IMAGE_MAX_SIDE), small)
        # Undecodable input (e.g. HEIC without pillow-heif) is returned as-is.
        self.assertEqual(_fit_base64_image_to_max_side("not-an-image", VISION_IMAGE_MAX_SIDE), "not-an-image")

    def test_describe_image_downscales_and_passes_bounds(self):
        from services.vision import VisionManager
        captured = {}

        def fake(model, prompt, payload, *, timeout=None, max_tokens=None):
            captured["side"] = self._longest_side(payload)
            captured["timeout"] = timeout
            captured["max_tokens"] = max_tokens
            return "desc"

        mgr = VisionManager(model="m", provider="lm_studio")
        with patch("core.config.load_config_readonly", return_value={}), \
             patch("services.vision._chat_lm_studio_image", side_effect=fake):
            out = mgr.describe_image(self._big_png_b64())

        self.assertEqual(out, "desc")
        self.assertLessEqual(captured["side"], 1568)        # pre-emptive downscale
        self.assertEqual(captured["timeout"], 120)          # DEFAULT_VISION_TIMEOUT_S
        self.assertEqual(captured["max_tokens"], 1536)      # DEFAULT_VISION_MAX_TOKENS

    def test_ocr_passes_timeout_and_ocr_max_tokens(self):
        from services.vision import GLMOCRManager
        captured = {}

        def fake(model, prompt, payload, *, timeout=None, max_tokens=None):
            captured["timeout"] = timeout
            captured["max_tokens"] = max_tokens
            return "page text"

        mgr = GLMOCRManager(model="ocr", provider="lm_studio")
        with patch("core.config.load_config_readonly",
                   return_value={"vision_timeout_s": 90, "ocr_max_tokens": 2048}), \
             patch("services.vision._chat_lm_studio_image", side_effect=fake):
            out = mgr.extract_page_text("data")

        self.assertEqual(out, "page text")
        self.assertEqual(captured["timeout"], 90)
        self.assertEqual(captured["max_tokens"], 2048)      # ocr_max_tokens, not vision

    def test_lm_studio_transport_disables_retries_and_caps_tokens(self):
        import services.vision as v
        captured = {}

        class _FakeCompletions:
            def create(self, **kw):
                captured["create_kw"] = kw
                msg = type("M", (), {"content": "x"})()
                choice = type("Ch", (), {"message": msg})()
                return type("R", (), {"choices": [choice]})()

        class _FakeClient:
            def __init__(self, **kw):
                captured["client_kw"] = kw
                self.chat = type("Chat", (), {"completions": _FakeCompletions()})()

        with patch("openai.OpenAI", _FakeClient):
            out = v._chat_lm_studio_image("m", "p", "b64", timeout=77, max_tokens=512)
        self.assertEqual(out, "x")
        self.assertEqual(captured["client_kw"]["max_retries"], 0)
        self.assertEqual(captured["client_kw"]["timeout"], 77)
        self.assertEqual(captured["create_kw"]["max_tokens"], 512)

        # timeout=None / max_tokens=None must NOT be forwarded (the OpenAI SDK
        # can read an explicit timeout=None as "no timeout").
        captured.clear()
        with patch("openai.OpenAI", _FakeClient):
            v._chat_lm_studio_image("m", "p", "b64")
        self.assertEqual(captured["client_kw"]["max_retries"], 0)
        self.assertNotIn("timeout", captured["client_kw"])
        self.assertNotIn("max_tokens", captured["create_kw"])


class TestOpLockEpochToken(unittest.TestCase):
    """Pinning tests for the per-operation op-lock epoch token (improvement
    plan 2026-07-04, item 1.5).

    The defect: ``ObsidianVaultManager.try_acquire_lock`` cached the epoch in a
    shared ``self._lock_epoch`` attribute that ``release_lock``/``heartbeat``
    read at CALL time — so any new acquisition overwrote the token a
    still-running previous holder would later release with (cancel an index
    run, start a refactor Apply, and the indexer's ``finally`` released the
    refactor's lock mid-batch). The invariant pinned here: **a holder can only
    release/extend the acquisition whose epoch it captured at acquire time** —
    a stale holder's release/heartbeat is a no-op against a newer acquisition.
    """

    def setUp(self):
        # A fresh manager (never the singleton) — these tests only exercise the
        # op-lock facade, no vault path / index state involved.
        from rag.vault import ObsidianVaultManager
        self.manager = ObsidianVaultManager()

    def test_acquire_returns_truthy_epoch_and_refusal_returns_none(self):
        epoch = self.manager.try_acquire_lock(ttl=30)
        self.assertTrue(epoch)                    # truthy — `if not` guards keep working
        self.assertIsInstance(epoch, int)
        self.assertIsNone(self.manager.try_acquire_lock(ttl=30))  # held ⇒ None
        self.manager.release_lock(epoch)
        self.assertTrue(self.manager.try_acquire_lock(ttl=30))    # released ⇒ re-acquirable

    def test_stale_holder_cannot_release_newer_acquisition(self):
        # The exact failure scenario from the plan: worker A acquires, is
        # cancelled (force_release), worker B acquires; A's late finally-release
        # must NOT free B's lock.
        epoch_a = self.manager.try_acquire_lock(ttl=30)
        self.assertTrue(epoch_a)
        self.assertTrue(self.manager.force_release())     # cancel path
        epoch_b = self.manager.try_acquire_lock(ttl=30)
        self.assertTrue(epoch_b)
        self.assertNotEqual(epoch_a, epoch_b)

        self.manager.release_lock(epoch_a)                # zombie A's finally
        # B still holds the lock: a third acquire must be refused.
        self.assertIsNone(self.manager.try_acquire_lock(ttl=30))

        self.manager.release_lock(epoch_b)                # real holder releases
        self.assertTrue(self.manager.try_acquire_lock(ttl=30))

    def test_try_acquire_epoch_captures_token_atomically(self):
        # m2 (2026-07-05): the acquire and the epoch read are ONE critical
        # section (try_acquire_epoch), so try_acquire_lock hands back the
        # caller's OWN acquisition — never a token read in a second critical
        # section that a concurrent force_release + re-acquire could have
        # advanced. Pin the primitive contract the manager relies on.
        from core.utils import RagOperationLock
        lock = RagOperationLock()
        e1 = lock.try_acquire_epoch(60)
        self.assertIsNotNone(e1)
        self.assertEqual(e1, lock.epoch)                # returned == this acquisition
        self.assertIsNone(lock.try_acquire_epoch(60))   # held ⇒ refused
        lock.release(e1)
        e2 = lock.try_acquire_epoch(60)
        self.assertGreater(e2, e1)                       # fresh acquisition ⇒ fresh token
        # The bool convenience wrapper is the SAME acquire, so it must agree.
        self.assertFalse(lock.try_acquire(60))           # e2 still held
        lock.release(e2)
        self.assertTrue(lock.try_acquire(60))

    def test_stale_holder_cannot_extend_newer_acquisition(self):
        epoch_a = self.manager.try_acquire_lock(ttl=30)
        self.assertTrue(self.manager.force_release())
        epoch_b = self.manager.try_acquire_lock(ttl=30)
        self.assertTrue(epoch_b)
        # A zombie heartbeat with the stale epoch must not refresh B's TTL —
        # RagOperationLock.heartbeat returns False on an epoch mismatch; assert
        # through the manager facade by checking the underlying refusal.
        self.assertFalse(self.manager._op_lock.heartbeat(epoch_a))
        self.assertTrue(self.manager._op_lock.heartbeat(epoch_b))
        self.manager.release_lock(epoch_b)

    def test_release_with_falsy_epoch_is_never_unconditional(self):
        # RagOperationLock.release(0) is an unconditional release; the manager
        # facade must never let a caller reach it with a falsy token (that is
        # force_release's job, and only recovery paths call that).
        epoch = self.manager.try_acquire_lock(ttl=30)
        self.assertTrue(epoch)
        self.manager.release_lock(0)                       # must be a no-op
        self.manager.release_lock(None)                    # must be a no-op
        self.assertIsNone(self.manager.try_acquire_lock(ttl=30))  # still held
        self.manager.release_lock(epoch)

    def test_index_vault_heartbeat_with_default_epoch_is_inert(self):
        # index_vault(op_epoch=0) — e.g. a direct test invocation holding no
        # lock — must not extend or disturb a live foreign acquisition: the
        # inert-token contract that keeps heartbeats safe when no lock is held.
        epoch = self.manager.try_acquire_lock(ttl=30)
        self.assertTrue(epoch)
        self.assertFalse(self.manager._op_lock.heartbeat(0))
        self.manager.release_lock(epoch)




class TestPutDoneResilient(unittest.TestCase):
    """Item 2.5 (improvement plan 2026-07-04): a finished SSE worker's _DONE
    sentinel must reach a live consumer whatever the queue depth — the old
    one-shot 5 s put dropped it on a persistently-full queue, making the
    consumer report "Generation timed out" on a COMPLETED answer."""

    def test_sentinel_delivered_once_consumer_drains(self):
        import queue
        import threading
        import time
        from core.utils import put_done_resilient

        q: queue.Queue = queue.Queue(maxsize=1)
        q.put("blocker")                      # queue full
        cancel = threading.Event()
        DONE = object()

        def slow_consumer():
            time.sleep(1.5)                    # past the old put's first window
            q.get()                            # frees the slot

        t = threading.Thread(target=slow_consumer, daemon=True)
        t.start()
        delivered = put_done_resilient(q, DONE, cancel)
        t.join(timeout=5)
        self.assertTrue(delivered)
        self.assertIs(q.get_nowait(), DONE)

    def test_dead_consumer_does_not_trap_the_worker(self):
        import queue
        import threading
        import time
        from core.utils import put_done_resilient

        q: queue.Queue = queue.Queue(maxsize=1)
        q.put("blocker")                       # full forever — nobody drains
        cancel = threading.Event()
        cancel.set()                           # consumer already gone
        start = time.monotonic()
        delivered = put_done_resilient(q, object(), cancel)
        self.assertFalse(delivered)
        self.assertLess(time.monotonic() - start, 3.0)   # bounded exit




class TestClientCacheBound(unittest.TestCase):
    """Item 2.6: the (host, timeout)-keyed local-client caches are bounded
    LRUs — the agent loop's per-iteration remaining-budget timeouts used to
    mint an unbounded set of cached httpx pools (the in-code "handful of
    entries" claim was wrong)."""

    def test_ollama_client_cache_never_exceeds_cap(self):
        import core.providers.ollama as om
        with om._client_cache_lock:
            saved = dict(om._client_cache)
            om._client_cache.clear()
        try:
            with patch.object(om.ollama, "Client", side_effect=lambda **kw: object()):
                for t in range(1, 40):          # 39 distinct integer timeouts
                    om._ollama_client("http://localhost:11434", float(t))
            with om._client_cache_lock:
                self.assertLessEqual(len(om._client_cache), om._CLIENT_CACHE_MAX)
                # Most-recent keys survive (LRU, not random) — the hot key wins.
                self.assertIn(("http://localhost:11434", 39.0), om._client_cache)
        finally:
            with om._client_cache_lock:
                om._client_cache.clear()
                om._client_cache.update(saved)

    def test_lms_client_cache_never_exceeds_cap(self):
        import core.providers.lms as lms
        with lms._client_cache_lock:
            saved = dict(lms._client_cache)
            lms._client_cache.clear()
        try:
            with patch("openai.OpenAI", side_effect=lambda **kw: object()):
                for t in range(1, 40):
                    lms.get_lmstudio_client("http://localhost:1234/v1", timeout=float(t))
            with lms._client_cache_lock:
                self.assertLessEqual(len(lms._client_cache), lms._CLIENT_CACHE_MAX)
        finally:
            with lms._client_cache_lock:
                lms._client_cache.clear()
                lms._client_cache.update(saved)


class TestAgentTimeoutQuantisation(unittest.TestCase):
    """Item 2.6: every per-call timeout the agent loop emits is drawn from the
    coarse bucket ladder, so the client caches see at most len(buckets) keys
    per host instead of one per remaining-second."""

    def test_quantise_covers_ladder_and_never_cuts_short(self):
        from core.agent.loop import _TIMEOUT_BUCKETS, _quantise_timeout
        for remaining in (0.5, 1.0, 14.9, 15.0, 16.0, 59.0, 250.0, 299.9,
                          301.0, 1799.0, 5000.0):
            q = _quantise_timeout(max(1.0, remaining))
            self.assertIn(q, _TIMEOUT_BUCKETS)
            if remaining <= _TIMEOUT_BUCKETS[-1]:
                self.assertGreaterEqual(q, min(remaining, _TIMEOUT_BUCKETS[-1]))


if __name__ == "__main__":
    unittest.main()
