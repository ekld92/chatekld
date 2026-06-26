"""
smoke_test.py — Smoke tests for ChatEKLD Flask API
====================================================
Validates core API endpoints (status, models, config, uploads, CSRF protection)
without requiring a live Ollama server.  Requires the real
modules to be importable (run inside the activated venv).

Usage:
    python -m pytest smoke_test.py -v
"""
import io
import os
import json
import unittest
from app import app


class ChatEKLDSmokeTest(unittest.TestCase):
    """Integration tests exercising Flask routes via the Werkzeug test client.

    Each test sets the ``X-Requested-With: ChatEKLD`` header required by the
    CSRF guard.  Tests are numbered to suggest a logical reading order, not to
    enforce execution sequence (pytest may reorder).
    """

    def setUp(self):
        """Create a Flask test client and prepare the CSRF header dict.

        Sets ``TESTING = True`` on the Flask app so that error handlers
        propagate exceptions instead of returning generic HTML pages.
        """
        app.config['TESTING'] = True
        # Ensure all requests include the CSRF protection header required by _origin_is_local
        self.client = app.test_client()
        self.headers = {'X-Requested-With': 'ChatEKLD'}

    def test_01_status(self):
        """GET /api/status must return 200 with an ``ollama_ok`` key."""
        resp = self.client.get('/api/status', headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn('ollama_ok', data)

    def test_02_models(self):
        """GET /api/models must return 200 with a ``models`` list."""
        resp = self.client.get('/api/models', headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn('models', data)

    def test_03_config(self):
        """GET /api/config returns expected keys; POST accepts valid JSON and rejects malformed payloads."""
        # GET returns expected keys
        resp = self.client.get('/api/config', headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn('llm', data)
        self.assertIn('embed', data)

        # POST valid config
        resp = self.client.post('/api/config',
                                json={"llm": "llama3.2", "embed": "mxbai-embed-large"},
                                content_type='application/json',
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200)

        # POST malformed JSON → 400 (not a 500 crash)
        resp = self.client.post('/api/config',
                                data='not-json',
                                content_type='application/json',
                                headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_03b_online_llm_save_preserves_local_selection(self):
        """Saving a chat model while an ONLINE provider is active must land in
        the per-provider key (openai_model / ...) and must NOT overwrite the
        local providers' shared ``llm`` field.

        Regression: the route used to setdefault() the per-provider key but
        still persist ``llm``, so picking e.g. an Anthropic model clobbered
        the saved Ollama/LM Studio selection (observed in a real config as
        provider="ollama" + llm="claude-sonnet-4-6", which Ollama 404s on).
        """
        # Establish a known local selection, then switch to an online provider.
        resp = self.client.post('/api/config',
                                json={"provider": "ollama", "llm": "llama3.2"},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post('/api/config',
                                json={"provider": "openai"},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200)

        # The UI keeps sending the generic "llm" field for the active provider.
        resp = self.client.post('/api/config',
                                json={"llm": "gpt-4o"},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200)

        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(data.get('openai_model'), 'gpt-4o')
        self.assertEqual(data.get('llm'), 'llama3.2',
                         "online model save must not clobber the local llm field")

        # Restore a local provider so later tests are unaffected.
        resp = self.client.post('/api/config',
                                json={"provider": "ollama"},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200)

    def test_03c_llm_param_defaults_and_validation(self):
        """The Settings-window keys ship as defaults and POST /api/config clamps
        out-of-range numerics / drops malformed enums (rather than persisting
        garbage), per api/routes/config.py::_validate_llm_config_keys."""
        # New persisted defaults are present.
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        for key in ("online_timeout_s", "online_max_retries", "online_max_tokens",
                    "paper_temperature", "paper_num_ctx", "deck_max_sections",
                    "deck_agent_max_iterations", "agent_wall_clock_s",
                    "local_request_timeout_s", "vault_wikilink_expansion",
                    "vault_wikilink_neighbor_cap", "vault_wikilink_node_cap",
                    "vault_wikilink_score_decay",
                    "vision_timeout_s", "vision_max_tokens", "ocr_max_tokens"):
            self.assertIn(key, data, f"missing default config key: {key}")

        # In-range value persists verbatim.
        self.client.post('/api/config', json={"online_timeout_s": 120}, headers=self.headers)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(data.get("online_timeout_s"), 120)

        # Out-of-range numerics are CLAMPED to the allowed range.
        self.client.post('/api/config',
                         json={"online_timeout_s": 99999, "paper_temperature": 5.0},
                         headers=self.headers)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(data.get("online_timeout_s"), 600)   # clamped to max
        self.assertEqual(data.get("paper_temperature"), 2.0)  # clamped to max

        # Wall-clock + local-timeout knobs clamp; 0 is a valid local timeout.
        self.client.post('/api/config',
                         json={"agent_wall_clock_s": 5000, "local_request_timeout_s": 99999},
                         headers=self.headers)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(data.get("agent_wall_clock_s"), 1800)       # clamped to max
        self.assertEqual(data.get("local_request_timeout_s"), 3600)  # clamped to max
        self.client.post('/api/config', json={"local_request_timeout_s": 0}, headers=self.headers)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(data.get("local_request_timeout_s"), 0)     # 0 = disabled, valid

        # Vision/OCR bounds: in-range persists, out-of-range clamps. Unlike
        # local_request_timeout_s there is no "0 = off" — the min is 5.
        self.client.post('/api/config',
                         json={"vision_timeout_s": 90, "vision_max_tokens": 2048},
                         headers=self.headers)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(data.get("vision_timeout_s"), 90)
        self.assertEqual(data.get("vision_max_tokens"), 2048)
        self.client.post('/api/config',
                         json={"vision_timeout_s": 99999, "ocr_max_tokens": 1},
                         headers=self.headers)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(data.get("vision_timeout_s"), 600)   # clamped to max
        self.assertEqual(data.get("ocr_max_tokens"), 64)      # clamped to min

        # Wikilink-expansion caps clamp into range (config-only knobs).
        self.client.post('/api/config',
                         json={"vault_wikilink_neighbor_cap": 99999,
                               "vault_wikilink_score_decay": 5.0},
                         headers=self.headers)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(data.get("vault_wikilink_neighbor_cap"), 100)  # clamped to max
        self.assertEqual(data.get("vault_wikilink_score_decay"), 1.0)   # clamped to max

        # A malformed enum is DROPPED — the previously-saved value survives.
        self.client.post('/api/config', json={"vault_reranker_device": "auto"}, headers=self.headers)
        self.client.post('/api/config', json={"vault_reranker_device": "gpu"}, headers=self.headers)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(data.get("vault_reranker_device"), "auto")

        # fallback_on is filtered to the allowed transient-error categories.
        self.client.post('/api/config',
                         json={"fallback_on": ["timeout", "bogus", "rate_limit"]},
                         headers=self.headers)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(data.get("fallback_on"), ["timeout", "rate_limit"])

        # Restore the default timeout so later tests see a clean value.
        self.client.post('/api/config', json={"online_timeout_s": 60}, headers=self.headers)

    def test_04_report_types(self):
        """GET /api/report-types must return 200 with a ``report_types`` list."""
        resp = self.client.get('/api/report-types', headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn('report_types', data)

    def test_07_pull_invalid_model_name(self):
        """api_pull must reject model names with shell-special characters."""
        resp = self.client.post('/api/pull',
                                json={"model": "; rm -rf ~"},
                                content_type='application/json',
                                headers=self.headers)
        # It should be 400 (Bad Request) because the header IS present but name is invalid.
        self.assertEqual(resp.status_code, 400)

    def test_07b_pull_rejects_non_string_model(self):
        """JSON null / number / list must NOT be stringified into a plausible
        model name and dispatched to the provider.  Before the validator
        tightening the route would coerce ``None`` to the literal string
        ``"None"``, which matched the model-name regex and reached Ollama.
        """
        for bad in (None, 42, ["foo"], {"name": "foo"}):
            with self.subTest(bad=bad):
                resp = self.client.post(
                    '/api/pull',
                    json={"model": bad},
                    content_type='application/json',
                    headers=self.headers,
                )
                self.assertEqual(resp.status_code, 400, f"accepted bad model: {bad!r}")

    def test_08_upload(self):
        """Upload a minimal valid-looking PDF byte sequence."""
        if not os.path.exists('test_article.pdf'):
            self.skipTest('test_article.pdf not present — skipping upload test')
        with open('test_article.pdf', 'rb') as f:
            resp = self.client.post('/api/upload',
                                    data={'file': (f, 'test_article.pdf')},
                                    headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn('upload_id', data)

    def test_09_upload_non_pdf_rejected(self):
        """Non-PDF files must be rejected with 400."""
        fake_txt = io.BytesIO(b'hello world')
        resp = self.client.post('/api/upload',
                                data={'file': (fake_txt, 'document.txt')},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_10_summarise_missing_upload_id_returns_400(self):
        """POST /api/summarise without upload_id must return 400."""
        resp = self.client.post('/api/summarise',
                                json={"model": "llama3.2"},
                                content_type='application/json',
                                headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertIn('error', data)
        self.assertIn('upload_id', data['error'])

    def test_11_summarise_valid_upload_streams_sse(self):
        """POST /api/summarise returns an SSE stream for a stored upload."""
        import sqlite3 as _sqlite3
        import unittest.mock as mock
        from core.constants import DB_PATH
        from core.database import DB_LOCK

        with DB_LOCK:
            with _sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO uploads (upload_id, filename, extracted_text) "
                    "VALUES (?, ?, ?)",
                    ("test-summary-id", "paper.pdf", "extracted paper text"),
                )
                conn.commit()

        with mock.patch("api.routes.paper.summarise_stream", return_value=iter(["ok"])):
            resp = self.client.post(
                "/api/summarise",
                json={"upload_id": "test-summary-id", "model": "llama3.2"},
                content_type="application/json",
                headers=self.headers,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/event-stream")
        body = resp.get_data(as_text=True)
        self.assertIn('data: {"token": "ok"}', body)
        self.assertIn("data: [DONE]", body)

    def test_12_about_returns_html_field(self):
        """GET /api/about with a local origin must return 200 with an 'html' field."""
        resp = self.client.get('/api/about', headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn('html', data)
        # The rendered content should be a non-empty string
        self.assertIsInstance(data['html'], str)
        self.assertGreater(len(data['html']), 0)

    def test_13_about_external_origin_forbidden(self):
        """GET /api/about with an external Origin must return 403."""
        h = dict(self.headers)
        h['Origin'] = 'https://attacker.example.com'
        resp = self.client.get('/api/about', headers=h)
        self.assertEqual(resp.status_code, 403)
        data = json.loads(resp.data)
        self.assertEqual(data.get('error'), 'Forbidden')

    # ------------------------------------------------------------------
    # Tests 18–20: POST /api/native-pick-folder
    # ------------------------------------------------------------------

    def test_18_native_pick_folder_csrf_guard(self):
        """POST /api/native-pick-folder with an external Origin must return 403.

        The endpoint opens the native OS dialog and returns a filesystem path,
        so it must be CSRF-guarded even though it accepts no body parameters.
        """
        h = {'X-Requested-With': 'ChatEKLD', 'Origin': 'http://evil.example.com'}
        resp = self.client.post('/api/native-pick-folder', headers=h)
        self.assertEqual(resp.status_code, 403)
        data = json.loads(resp.data)
        self.assertEqual(data.get('error'), 'Forbidden')

    def test_19_native_pick_folder_no_window_returns_503(self):
        """POST /api/native-pick-folder returns 503 when no PyWebView window is active.

        During tests there is no live webview.start() call, so webview.windows
        is empty.  The endpoint must detect this and return 503 rather than
        raising an IndexError.
        """
        import unittest.mock as mock
        with mock.patch.dict('sys.modules', {'webview': mock.MagicMock(windows=[])}):
            resp = self.client.post('/api/native-pick-folder', headers=self.headers)
        self.assertEqual(resp.status_code, 503)
        data = json.loads(resp.data)
        self.assertIn('error', data)

    def test_20_native_pick_folder_cancelled_returns_cancelled(self):
        """POST /api/native-pick-folder returns {"cancelled": true} when the dialog is dismissed.

        Simulates the user pressing Cancel in the OS folder picker.
        create_file_dialog() returns None on cancel; the endpoint must translate
        this into a {"cancelled": true} JSON response (not an error).
        """
        import unittest.mock as mock
        fake_window = mock.MagicMock()
        # create_file_dialog returns None when the user cancels.
        fake_window.create_file_dialog.return_value = None
        fake_webview = mock.MagicMock()
        fake_webview.windows = [fake_window]
        fake_webview.FOLDER_DIALOG = 20  # PyWebView constant value
        with mock.patch.dict('sys.modules', {'webview': fake_webview}):
            resp = self.client.post('/api/native-pick-folder', headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data.get('cancelled'))

    def test_21_native_pick_folder_happy_path(self):
        """POST /api/native-pick-folder returns the chosen path when the user selects a folder.

        Simulates the user picking an existing directory in the OS dialog.
        The endpoint must validate that the returned path is a real directory
        and echo it back as {"ok": true, "path": "..."}.
        """
        import unittest.mock as mock
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_window = mock.MagicMock()
            # create_file_dialog returns a tuple of selected paths.
            fake_window.create_file_dialog.return_value = (tmpdir,)
            fake_webview = mock.MagicMock()
            fake_webview.windows = [fake_window]
            fake_webview.FOLDER_DIALOG = 20
            with mock.patch.dict('sys.modules', {'webview': fake_webview}):
                resp = self.client.post('/api/native-pick-folder', headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data.get('ok'))
        self.assertEqual(data.get('path'), tmpdir)


    # ------------------------------------------------------------------
    # /api/reset
    # ------------------------------------------------------------------

    def test_22_reset_requires_csrf_header(self):
        """POST /api/reset without the X-Requested-With header must return 403."""
        resp = self.client.post(
            '/api/reset',
            content_type='application/json',
            data=json.dumps({'confirm': 'reset'}),
        )
        self.assertEqual(resp.status_code, 403)

    def test_23_reset_requires_confirm_token(self):
        """POST /api/reset without the confirmation token must return 400."""
        resp = self.client.post(
            '/api/reset',
            headers=self.headers,
            content_type='application/json',
            data=json.dumps({}),
        )
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertIn('error', data)

    def test_24_reset_wrong_confirm_token(self):
        """POST /api/reset with a wrong confirmation token must return 400."""
        resp = self.client.post(
            '/api/reset',
            headers=self.headers,
            content_type='application/json',
            data=json.dumps({'confirm': 'yes_please'}),
        )
        self.assertEqual(resp.status_code, 400)

    def test_25_reset_happy_path_clears_uploads_table(self):
        """POST /api/reset must return ok=true and wipe the uploads DB.

        Also verifies that the status deques are cleared so subsequent
        operation polls see no stale messages.
        """
        import sqlite3 as _sqlite3
        from core.constants import DB_PATH
        from core.database import DB_LOCK
        from rag.vault import obsidian_manager as _obsidian_mgr
        _obsidian_status_updates = _obsidian_mgr._status_messages
        _obsidian_status_lock = _obsidian_mgr._messages_lock

        # Seed a dummy upload row so we can confirm it is wiped.
        with DB_LOCK:
            with _sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO uploads (upload_id, filename, extracted_text) "
                    "VALUES (?, ?, ?)",
                    ('test-reset-id', 'dummy.pdf', 'test content'),
                )

        with _obsidian_status_lock:
            _obsidian_status_updates.append('stale obsidian progress message')

        resp = self.client.post(
            '/api/reset',
            headers=self.headers,
            content_type='application/json',
            data=json.dumps({'confirm': 'reset'}),
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data.get('ok'))
        # uploads_db must appear in the deleted list.
        self.assertIn('uploads_db', data.get('deleted', []))

        # Verify the uploads table is empty.
        with DB_LOCK:
            with _sqlite3.connect(DB_PATH) as conn:
                row_count = conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
        self.assertEqual(row_count, 0, "uploads table must be empty after reset")

        with _obsidian_status_lock:
            self.assertEqual(len(_obsidian_status_updates), 0,
                             "_obsidian_status_updates must be empty after reset")

    def test_27_log_rate_limit(self):
        """/api/log returns throttled=true after the 100-message rolling-window cap."""
        from api.routes.config import _log_rate_bucket, _log_rate_lock
        # Clear the bucket so prior tests do not skew the count.
        with _log_rate_lock:
            _log_rate_bucket.clear()

        throttled_count = 0
        for i in range(110):
            resp = self.client.post(
                "/api/log",
                json={"msg": f"msg-{i}", "level": "info"},
                content_type="application/json",
                headers=self.headers,
            )
            self.assertEqual(resp.status_code, 200)
            if json.loads(resp.data).get("throttled"):
                throttled_count += 1

        # 10 of the last 110 must be throttled — the cap is 100 per window.
        self.assertGreaterEqual(throttled_count, 10)

    def test_28_delete_upload(self):
        """DELETE /api/upload/<id> removes the stored row."""
        import sqlite3 as _sqlite3
        from core.constants import DB_PATH
        from core.database import DB_LOCK

        upload_id = "test-delete-id"
        with DB_LOCK:
            with _sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO uploads (upload_id, filename, extracted_text) "
                    "VALUES (?, ?, ?)",
                    (upload_id, "paper.pdf", "text"),
                )
                conn.commit()

        resp = self.client.delete(f"/api/upload/{upload_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.data).get("ok"))

        with DB_LOCK:
            with _sqlite3.connect(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT 1 FROM uploads WHERE upload_id = ?", (upload_id,)
                ).fetchone()
        self.assertIsNone(row)

    def test_29_delete_upload_rejects_invalid_id(self):
        """DELETE /api/upload/<id> rejects an id that violates the format."""
        # 200 characters > 128-character regex cap.
        resp = self.client.delete("/api/upload/" + "x" * 200, headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_30_reset_returns_503_when_indexer_busy(self):
        """POST /api/reset must refuse with 503 when the indexer thread cannot
        be joined within the wait_for_indexing timeout."""
        import unittest.mock as mock
        from rag.vault import obsidian_manager

        with mock.patch.object(obsidian_manager, "wait_for_indexing", return_value=False):
            resp = self.client.post(
                "/api/reset",
                json={"confirm": "reset"},
                content_type="application/json",
                headers=self.headers,
            )
        self.assertEqual(resp.status_code, 503)
        data = json.loads(resp.data)
        self.assertIn("error", data)

    def test_30b_index_returns_503_when_prior_run_unfinished(self):
        """POST /api/obsidian/index must refuse with 503 when a prior indexing
        thread has not finished (wait_for_indexing False).  Closes the
        cancel-then-reindex race: a cancel force-releases the op lock before its
        thread's final persist, so the index route must wait for it to exit."""
        import unittest.mock as mock
        from rag.vault import obsidian_manager

        with (
            mock.patch.object(obsidian_manager, "get_vault_path", return_value="/tmp/vault"),
            mock.patch.object(obsidian_manager, "wait_for_indexing", return_value=False),
            mock.patch.object(obsidian_manager, "try_acquire_lock") as acq,
        ):
            resp = self.client.post(
                "/api/obsidian/index",
                json={},
                content_type="application/json",
                headers=self.headers,
            )
        self.assertEqual(resp.status_code, 503)
        self.assertIn("error", json.loads(resp.data))
        acq.assert_not_called()  # bailed out before acquiring the lock

    def test_26_reset_wipe_feedback_and_config(self):
        """POST /api/reset with wipe_feedback=true and wipe_config=true deletes
        the corresponding files when they exist.
        """
        import tempfile, unittest.mock as mock
        from app import FEEDBACK_FILE, CONFIG_FILE

        # Create temporary stand-ins so we do not touch real user files.
        with (
            tempfile.NamedTemporaryFile(delete=False) as fb,
            tempfile.NamedTemporaryFile(delete=False) as cfg,
        ):
            fake_feedback = fb.name
            fake_config   = cfg.name

        try:
            with (
                mock.patch('app.FEEDBACK_FILE', fake_feedback),
                mock.patch('app.CONFIG_FILE',   fake_config),
            ):
                resp = self.client.post(
                    '/api/reset',
                    headers=self.headers,
                    content_type='application/json',
                    data=json.dumps({
                        'confirm': 'reset',
                        'wipe_feedback': True,
                        'wipe_config': True,
                    }),
                )
            self.assertEqual(resp.status_code, 200)
            data = json.loads(resp.data)
            self.assertTrue(data.get('ok'))
            self.assertIn('feedback.jsonl', data.get('deleted', []))
            self.assertIn('config.json',   data.get('deleted', []))
            # Both files must be gone.
            self.assertFalse(os.path.exists(fake_feedback))
            self.assertFalse(os.path.exists(fake_config))
        finally:
            # Best-effort cleanup; files may already be deleted by the reset.
            for path in (fake_feedback, fake_config):
                if os.path.exists(path):
                    os.unlink(path)


class ExtractAllPagesTest(unittest.TestCase):
    """Unit tests for services.pdf_service._extract_all_pages.

    Uploads above EXTRACT_MAX_PAGES_PER_CALL (1000) pages used to hard-fail
    with the extractor's internal "Page range too large" error; the worker
    now mirrors the vault indexer's range loop and concatenates. These pin
    the range arithmetic, the single-call fast path, the shared
    PDF_MAX_PAGES ceiling, and the sections fallback.
    """

    def _patch_extractor(self, page_count, extract_side_effect):
        from unittest import mock
        return (
            mock.patch("pdf_extractor.get_pdf_page_count", return_value=page_count),
            mock.patch(
                "pdf_extractor.extract_structured_from_pdf",
                side_effect=extract_side_effect,
            ),
        )

    @staticmethod
    def _sections(full_text, sections=None):
        from unittest import mock
        s = mock.MagicMock()
        s.full_text = full_text
        s.sections = sections or {}
        return s

    def test_large_pdf_loops_ranges_and_concatenates(self):
        from services.pdf_service import _extract_all_pages
        count_patch, extract_patch = self._patch_extractor(
            2500,
            lambda *a, **kw: self._sections(
                f"pages {kw['start_page']}-{kw['end_page']}"
            ),
        )
        with count_patch, extract_patch as extract_mock:
            text = _extract_all_pages("/tmp/fake.pdf", ocr_cb=None)
        self.assertEqual(extract_mock.call_count, 3)
        ranges = [
            (kw["start_page"], kw["end_page"])
            for _, kw in extract_mock.call_args_list
        ]
        self.assertEqual(ranges, [(0, 1000), (1000, 2000), (2000, 2500)])
        self.assertEqual(
            text, "pages 0-1000\n\npages 1000-2000\n\npages 2000-2500"
        )

    def test_small_pdf_uses_single_call_without_range_args(self):
        from services.pdf_service import _extract_all_pages
        count_patch, extract_patch = self._patch_extractor(
            42, lambda *a, **kw: self._sections("whole document")
        )
        with count_patch, extract_patch as extract_mock:
            text = _extract_all_pages("/tmp/fake.pdf", ocr_cb=None)
        self.assertEqual(text, "whole document")
        self.assertEqual(extract_mock.call_count, 1)
        # The fast path must stay identical to the pre-range-loop call shape:
        # no start_page/end_page kwargs at all.
        _, kwargs = extract_mock.call_args
        self.assertNotIn("start_page", kwargs)
        self.assertNotIn("end_page", kwargs)

    def test_page_ceiling_rejected_with_clean_error(self):
        from services.pdf_service import _extract_all_pages
        from core.constants import PDF_MAX_PAGES
        count_patch, extract_patch = self._patch_extractor(
            PDF_MAX_PAGES + 1, lambda *a, **kw: self.fail("must not extract")
        )
        with count_patch, extract_patch:
            with self.assertRaisesRegex(ValueError, str(PDF_MAX_PAGES)):
                _extract_all_pages("/tmp/fake.pdf", ocr_cb=None)

    def test_sections_fallback_per_range(self):
        """A range with no flat text falls back to joining its sections."""
        from services.pdf_service import _extract_all_pages

        def extract(*a, **kw):
            if kw["start_page"] == 0:
                return self._sections("", sections={"Intro": "alpha"})
            return self._sections("plain text")

        count_patch, extract_patch = self._patch_extractor(1500, extract)
        with count_patch, extract_patch:
            text = _extract_all_pages("/tmp/fake.pdf", ocr_cb=None)
        self.assertEqual(text, "## Intro\nalpha\n\nplain text")


class WriteTextAtomicTest(unittest.TestCase):
    """Unit tests for core.utils.write_text_atomic (crash-safe text writes)."""

    def test_writes_content(self):
        """Happy path: file lands with exact content, no temp litter."""
        import tempfile as _tempfile
        from core.utils import write_text_atomic
        with _tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "out.txt")
            write_text_atomic(target, "hello ✓")
            with open(target, encoding="utf-8") as f:
                self.assertEqual(f.read(), "hello ✓")
            # The mkstemp sibling must have been promoted, not abandoned.
            self.assertEqual(os.listdir(d), ["out.txt"])

    def test_failed_replace_preserves_original(self):
        """A crash at the promote step must leave the old file untouched."""
        import tempfile as _tempfile
        from unittest import mock
        from core.utils import write_text_atomic
        with _tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "out.txt")
            with open(target, "w", encoding="utf-8") as f:
                f.write("original")
            with mock.patch("core.utils.os.replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    write_text_atomic(target, "replacement")
            with open(target, encoding="utf-8") as f:
                self.assertEqual(f.read(), "original")
            # Failure path must also clean up its temp file.
            self.assertEqual(os.listdir(d), ["out.txt"])

    def test_creates_missing_parent_dir(self):
        import tempfile as _tempfile
        from core.utils import write_text_atomic
        with _tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "nested", "deeper", "out.txt")
            write_text_atomic(target, "x")
            with open(target, encoding="utf-8") as f:
                self.assertEqual(f.read(), "x")


class ConfigCacheTest(unittest.TestCase):
    """Regression tests for the stat-keyed load_config() cache.

    The cache must be invisible: every observable behaviour (fresh values
    after save_config, after an external rewrite of config.json, isolation
    from caller-side mutation) must match the uncached implementation.
    """

    def setUp(self):
        # Point core.config at a private temp config.json.  The pytest
        # conftest already makes the suite hermetic via CHATEKLD_BASE_DIR,
        # but a direct `python smoke_test.py` run bypasses conftest — and
        # these tests must never touch the user's real config.  Patching
        # core.config.CONFIG_FILE covers load_config and save_config (both
        # read the module-level binding); the cache key includes the path,
        # so the patched file gets its own cache entries and cannot
        # cross-contaminate other tests.
        import tempfile as _tempfile
        from unittest import mock
        self._tmpdir = _tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.config_path = os.path.join(self._tmpdir.name, "config.json")
        patcher = mock.patch("core.config.CONFIG_FILE", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_save_config_invalidates_cache(self):
        from core.config import load_config, save_config
        load_config()  # prime the cache
        save_config({"llm": "cache-test-model-a"})
        self.assertEqual(load_config().get("llm"), "cache-test-model-a")

    def test_external_rewrite_is_picked_up(self):
        """A write that bypasses save_config (no explicit invalidation) must
        still be observed via the (size, mtime_ns) key change."""
        from core.config import load_config
        current = load_config()  # prime the cache
        current["llm"] = "cache-test-model-b"
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=4)
        self.assertEqual(load_config().get("llm"), "cache-test-model-b")

    def test_returned_dict_is_isolated_from_cache(self):
        from core.config import load_config
        first = load_config()
        first["llm"] = "caller-side-mutation"
        first["vault_exclude_dirs"].append("caller-side-mutation")
        second = load_config()
        self.assertNotEqual(second.get("llm"), "caller-side-mutation")
        self.assertNotIn("caller-side-mutation", second.get("vault_exclude_dirs", []))


class SaveFeedbackNoStallTest(unittest.TestCase):
    """save_feedback must not busy-wait on a contended flock.

    The old implementation retried LOCK_NB for up to 2 seconds (holding the
    module thread-lock, on the request thread) and then appended anyway.
    The new one tries once and proceeds — same outcome, no stall.
    """

    def test_held_flock_does_not_delay_append(self):
        import fcntl
        import tempfile as _tempfile
        import time
        from unittest import mock
        from core.feedback import save_feedback, load_feedback
        # Run against a private temp feedback file (Codex review on PR #85):
        # under pytest the conftest's CHATEKLD_BASE_DIR keeps this hermetic,
        # but a direct `python smoke_test.py` run bypasses conftest and the
        # append would permanently pollute the user's real feedback history.
        # save_feedback and load_feedback both read the module-level
        # FEEDBACK_FILE binding, so one patch covers both.
        with _tempfile.TemporaryDirectory() as d:
            feedback_path = os.path.join(d, "feedback.jsonl")
            with mock.patch("core.feedback.FEEDBACK_FILE", feedback_path):
                # Hold the flock from a second descriptor, as a concurrent
                # app instance would.
                holder = open(feedback_path, "a")
                try:
                    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    start = time.monotonic()
                    save_feedback(rating="up", comment="no-stall-test")
                    elapsed = time.monotonic() - start
                    # Old behaviour took >= 2 s here; allow generous headroom for CI.
                    self.assertLess(elapsed, 1.0)
                    records = load_feedback()
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0].get("comment"), "no-stall-test")
                finally:
                    fcntl.flock(holder, fcntl.LOCK_UN)
                    holder.close()


class ReadExtractResultTest(unittest.TestCase):
    """Unit tests for services.pdf_service._read_extract_result.

    The worker writes its result atomically, so the parent should only ever
    see an empty file (worker died) or complete JSON — but every conceivable
    on-disk state must still map to a clean RuntimeError for the route's
    error handling, never a raw JSONDecodeError.
    """

    def _tmp(self, content: bytes) -> str:
        import tempfile as _tempfile
        fd, path = _tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_ok_payload(self):
        from services.pdf_service import _read_extract_result
        path = self._tmp(b'{"status": "ok", "text": "extracted"}')
        self.assertEqual(_read_extract_result(path, 0), "extracted")

    def test_error_payload(self):
        from services.pdf_service import _read_extract_result
        path = self._tmp(b'{"status": "error", "error": "bad pdf"}')
        with self.assertRaisesRegex(RuntimeError, "bad pdf"):
            _read_extract_result(path, 0)

    def test_truncated_json_maps_to_runtime_error(self):
        """A torn write (pre-atomic-write failure mode) must not leak JSONDecodeError."""
        from services.pdf_service import _read_extract_result
        path = self._tmp(b'{"status": "ok", "te')
        with self.assertRaisesRegex(RuntimeError, "unreadable result"):
            _read_extract_result(path, 0)

    def test_empty_file_nonzero_exit_is_worker_failure(self):
        from services.pdf_service import _read_extract_result
        path = self._tmp(b"")
        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            _read_extract_result(path, 1)

    def test_missing_file_nonzero_exit_is_worker_failure(self):
        from services.pdf_service import _read_extract_result
        path = self._tmp(b"")
        os.remove(path)
        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            _read_extract_result(path, 1)


if __name__ == '__main__':
    unittest.main()
