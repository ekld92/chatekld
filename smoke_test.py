"""
smoke_test.py — Smoke tests for CHERQAiN Flask API
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


class CHERQAiNSmokeTest(unittest.TestCase):
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

    def test_03a_config_body_shape_edges(self):
        """2026-07-22 audit M7: a JSON ARRAY body must 400 cleanly (it used to
        pass the truthiness check and 500 on .startswith deeper in), and `{}`
        is a legitimate no-op patch, not "Invalid JSON"."""
        resp = self.client.post('/api/config', json=[1, 2],
                                headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post('/api/config', json=["audit_x"],
                                headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post('/api/config', json={},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200)

    def test_03c_provider_case_normalised_before_routing(self):
        """2026-07-22 audit M7c: {"provider": "OPENAI", "llm": "x"} used to
        route `llm` into openai_model (consuming the local selection) and then
        DROP the provider change because the validator required exact case.
        One up-front normalisation makes routing and validation agree."""
        resp = self.client.post('/api/config',
                                json={"provider": "ollama", "llm": "llama3.2"},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post('/api/config',
                                json={"provider": "OPENAI", "llm": "gpt-4o"},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(data.get('provider'), 'openai')
        self.assertEqual(data.get('openai_model'), 'gpt-4o')
        self.assertEqual(data.get('llm'), 'llama3.2')   # local selection intact

    def test_03d_pricing_overrides_validated(self):
        """2026-07-22 audit M2: llm_pricing_overrides gets a shape/size gate —
        garbage or oversized values are DROPPED (prior value survives), valid
        rows persist with numbers coerced at write time."""
        ok = {"gpt-4o-mini": {"input": 1.0, "output": "2.5",
                              "cached_input": "0.5"}}
        resp = self.client.post('/api/config',
                                json={"llm_pricing_overrides": ok},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        row = data.get('llm_pricing_overrides', {}).get('gpt-4o-mini')
        self.assertEqual(row, {"input": 1.0, "output": 2.5, "cached_input": 0.5})

        # A malformed patch is dropped — the valid row above survives.
        for bad in (
            {"m": "not-a-dict"},
            {"m": {"input": "x", "output": 1}},
            {"m": {"input": 1}},                    # missing output
            {"m" * 300: {"input": 1, "output": 1}},  # key too long
            "not-a-dict",
        ):
            resp = self.client.post('/api/config',
                                    json={"llm_pricing_overrides": bad},
                                    headers=self.headers)
            self.assertEqual(resp.status_code, 200, bad)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(
            data.get('llm_pricing_overrides', {}).get('gpt-4o-mini'),
            {"input": 1.0, "output": 2.5, "cached_input": 0.5})

        # Clean up: the CHATEKLD_BASE_DIR sandbox is per-SESSION, so a
        # persisted override would leak into other files' cost tests
        # (usage_tracker.record resolves persisted overrides itself).
        resp = self.client.post('/api/config',
                                json={"llm_pricing_overrides": {}},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200)

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

        # B7 (2026-07-13): the provider selectors are now enum-validated — a
        # garbage `provider` is DROPPED, the prior value survives.
        self.client.post('/api/config', json={"provider": "ollama"}, headers=self.headers)
        self.client.post('/api/config', json={"provider": "not-a-provider"}, headers=self.headers)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(data.get("provider"), "ollama")
        # B7: an over-long system prompt is capped, not persisted whole.
        self.client.post('/api/config',
                         json={"vault_chat_system_prompt": "x" * 99999},
                         headers=self.headers)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertLessEqual(len(data.get("vault_chat_system_prompt", "")), 4000)
        # B7: a control-char-laden model id is dropped (prior value survives).
        self.client.post('/api/config', json={"vision_model": "qwen3-vl"}, headers=self.headers)
        self.client.post('/api/config', json={"vision_model": "bad\x00id"}, headers=self.headers)
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(data.get("vision_model"), "qwen3-vl")

        # Restore the default timeout so later tests see a clean value.
        self.client.post('/api/config', json={"online_timeout_s": 60}, headers=self.headers)

    def test_03e_dropped_keys_are_reported_to_the_client(self):
        """2026-07-27 a11y audit: a rejected value must be NAMED in the response.

        The validator drops an out-of-range key and keeps the prior persisted
        value — correct, but until now the route answered a bare 200, so the
        Settings window showed the rejected text sitting in the field all
        session while the server held something else (a WCAG 3.3.1 Error
        Identification failure: the error is detected and never identified).
        `dropped` is what lets settings.js mark the offending control and
        announce it.  Purely additive — `ok` and the status code are unchanged,
        which is why every assertion in test_03* above still holds.
        """
        # An UNPARSEABLE value is dropped and named. Note the deliberate
        # asymmetry in api/validators.py, which this test also pins: a numeric
        # knob out of range is CLAMPED (see below), only garbage is dropped —
        # so `dropped` reports the cases where the user's input was discarded
        # entirely, which are exactly the ones a silent 200 made invisible.
        resp = self.client.post('/api/config',
                                json={"vault_top_k": "not-a-number"},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.data)
        self.assertTrue(body.get("ok"))
        self.assertIn("vault_top_k", body.get("dropped", []))

        # An unknown ENUM value is dropped too (coerce_enum has no clamp to
        # fall back on) — this is the `vault_reranker_device: "cuda:0"` shape
        # the eval-sweep audit flagged as persistable through a non-validating
        # writer.
        resp = self.client.post('/api/config',
                                json={"vault_reranker_device": "cuda:0"},
                                headers=self.headers)
        self.assertIn("vault_reranker_device", json.loads(resp.data).get("dropped", []))
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertIn(data.get("vault_reranker_device"), ("auto", "cpu", "mps"))

        # Out-of-range numerics are CLAMPED, not dropped — so they must NOT be
        # reported, or the UI would flag an error for a value the server did
        # accept (in clamped form). Pinning both halves keeps the client's
        # error marking honest.
        resp = self.client.post('/api/config',
                                json={"vault_top_k": 9999},   # range is 1..32
                                headers=self.headers)
        self.assertNotIn("vault_top_k", json.loads(resp.data).get("dropped", []))
        data = json.loads(self.client.get('/api/config', headers=self.headers).data)
        self.assertEqual(int(data.get("vault_top_k", 0)), 32)

        # A clean save reports an empty list rather than omitting the key, so
        # the client can distinguish "nothing dropped" from "old server".
        resp = self.client.post('/api/config',
                                json={"vault_top_k": 8}, headers=self.headers)
        self.assertEqual(json.loads(resp.data).get("dropped"), [])

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
        import fitz
        with fitz.open() as document:
            document.new_page().insert_text((72, 72), 'Disposable upload fixture')
            raw = document.tobytes()
        resp = self.client.post('/api/upload',
                                data={'file': (io.BytesIO(raw), 'fixture.pdf')},
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

    def test_27b_log_tail_reads_and_redacts(self):
        """GET /api/log/tail tails the log, redacts keys, and is local-origin gated."""
        from core.constants import LOG_FILE
        # Seed the canonical log file (LOG_FILE honours CHATEKLD_BASE_DIR, which
        # conftest pins to a temp dir, so this never touches the real log).
        with open(LOG_FILE, "w", encoding="utf-8") as fh:
            for i in range(30):
                fh.write(f"line {i}\n")
            fh.write("oops leaked sk-ant-api03-ABCDEF1234567890SECRETKEY here\n")

        resp = self.client.get("/api/log/tail?lines=5", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data["exists"])
        self.assertEqual(data["lines"], 5)
        # The API key must never reach the UI.
        self.assertNotIn("sk-ant-api03-ABCDEF", data["text"])
        self.assertIn("line 29", data["text"])

        # Garbage ``lines`` param falls back to the default (does not 500).
        self.assertEqual(
            self.client.get("/api/log/tail?lines=xyz", headers=self.headers).status_code, 200)

        # Local-origin gate: the X-Requested-With header is mandatory.
        self.assertEqual(self.client.get("/api/log/tail").status_code, 403)

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

    def test_30c_index_route_threads_force_fresh(self):
        """(2026-07-29 audit 0.1) POST /api/obsidian/index accepts the
        `force_fresh` body field — the in-app "yes, rebuild" the embed-backend
        gate's refusal requires — and coerces it strictly: only a JSON `true`
        reaches the manager as True (a garbage value must never trigger an
        hours-long re-embed)."""
        import unittest.mock as mock
        from rag.vault import obsidian_manager

        for body_value, expected in ((True, True), ("yes-ish", False), (None, False)):
            with (
                mock.patch.object(obsidian_manager, "get_vault_path",
                                  return_value="/tmp/vault"),
                mock.patch.object(obsidian_manager, "wait_for_indexing",
                                  return_value=True),
                mock.patch.object(obsidian_manager, "try_acquire_lock",
                                  return_value=7),
                mock.patch.object(obsidian_manager, "release_lock"),
                mock.patch.object(obsidian_manager, "register_index_thread"),
                mock.patch.object(obsidian_manager, "index_vault") as iv,
                # Patched so this stays a pure force_fresh-threading test: the
                # real one mutates the manager singleton's status, which would
                # leak "scanning" into every later test in the file.
                mock.patch.object(obsidian_manager, "begin_index_run"),
                mock.patch("threading.Thread") as thread_cls,
            ):
                # Run the "background" thread synchronously so the mock call
                # is recorded before the assertion.
                thread_cls.side_effect = lambda target=None, daemon=None: mock.Mock(
                    start=target, name="fake-thread")
                body = {} if body_value is None else {"force_fresh": body_value}
                resp = self.client.post(
                    "/api/obsidian/index",
                    json=body,
                    content_type="application/json",
                    headers=self.headers,
                )
                self.assertEqual(resp.status_code, 200, resp.data)
                self.assertEqual(
                    iv.call_args.kwargs.get("force_fresh"), expected,
                    f"body {body_value!r} must thread as {expected}",
                )

    def test_30d_index_route_publishes_running_state_before_returning(self):
        """(2026-08-12) The 200 from POST /api/obsidian/index must not be
        observable while the status is still the PREVIOUS run's terminal state.

        The transition used to live on the spawned thread, and the client polls
        /api/obsidian/status with zero delay on the 200 — so that first poll
        read the leftover 'done' (served instantly: get_status's lazy recovery
        materialises it into _index_state), flashed "Indexing complete.",
        stopped polling and restored the idle buttons while the run it had just
        started carried on invisibly. Two assertions, both of which fail on the
        pre-fix route: the state is already non-terminal when `t.start()` runs,
        and a real status GET after the POST reports the run.
        """
        import unittest.mock as mock
        from rag.vault import obsidian_manager

        # The manager is a process-wide singleton and this test drives its REAL
        # status machine, so snapshot/restore everything the route touches.
        saved = (
            obsidian_manager._index_state,
            obsidian_manager._current_phase,
            obsidian_manager._index_op_epoch,
            obsidian_manager._index_run_begun_epoch,
            obsidian_manager._index_error_code,
            obsidian_manager._index_error_detail,
        )

        def _restore():
            (obsidian_manager._index_state,
             obsidian_manager._current_phase,
             obsidian_manager._index_op_epoch,
             obsidian_manager._index_run_begun_epoch,
             obsidian_manager._index_error_code,
             obsidian_manager._index_error_detail) = saved
        self.addCleanup(_restore)

        # Pre-fix precondition: the previous run left a terminal state behind.
        obsidian_manager._index_state = "done"
        obsidian_manager._index_error_code = "index_failed"
        obsidian_manager._index_error_detail = "stale reason from the last run"

        state_at_thread_start = []
        with (
            mock.patch.object(obsidian_manager, "get_vault_path",
                              return_value="/tmp/vault"),
            mock.patch.object(obsidian_manager, "wait_for_indexing",
                              return_value=True),
            mock.patch.object(obsidian_manager, "try_acquire_lock",
                              return_value=11),
            mock.patch.object(obsidian_manager, "release_lock"),
            mock.patch.object(obsidian_manager, "register_index_thread"),
            mock.patch.object(obsidian_manager, "index_vault"),
            mock.patch("threading.Thread") as thread_cls,
        ):
            # The daemon never runs: the point is that the ROUTE published the
            # state, not the thread. `start` only records what a caller would
            # have seen at that instant.
            thread_cls.side_effect = lambda target=None, daemon=None: mock.Mock(
                start=lambda: state_at_thread_start.append(
                    obsidian_manager.get_status()),
                name="fake-thread")
            resp = self.client.post(
                "/api/obsidian/index", json={},
                content_type="application/json", headers=self.headers,
            )
            self.assertEqual(resp.status_code, 200, resp.data)
            self.assertEqual(state_at_thread_start, ["scanning"])

            status = self.client.get("/api/obsidian/status", headers=self.headers)
            payload = json.loads(status.data)

        self.assertEqual(payload["state"], "scanning")
        # The stale error record must not ride the new run either — the client
        # keys its recovery affordances on these two fields.
        self.assertEqual(payload["error_code"], "")
        self.assertEqual(payload["error_detail"], "")

    def test_26_reset_wipe_feedback_and_config(self):
        """POST /api/reset with wipe_feedback=true and wipe_config=true deletes
        the corresponding files when they exist.
        """
        import tempfile
        import unittest.mock as mock
        # Importability canary, not a real use: CLAUDE.md documents that
        # FEEDBACK_FILE / CONFIG_FILE stay importable from `app` because this
        # test patches them at app.FEEDBACK_FILE / app.CONFIG_FILE below. The
        # import failing IS the regression this line exists to catch.
        from app import FEEDBACK_FILE, CONFIG_FILE  # noqa: F401

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

    # ------------------------------------------------------------------
    # Item 4.1 — app-level origin guard (before_request hook)
    # ------------------------------------------------------------------

    def test_all_routes_gated(self):
        """Every ``/api/*`` route returns 403 without the CSRF header.

        Item 4.1: the origin guard is now a single ``before_request`` hook
        (``api.security.register_origin_guard``) instead of 67 inline checks.
        This test introspects ``app.url_map`` and confirms that every ``/api/``
        rule is rejected when the required ``X-Requested-With: ChatEKLD``
        header is absent — so a new blueprint cannot silently forget the gate.

        Defect and failure scenario: a new /api/* route registered without an
        origin check would be remotely accessible from any webpage with a
        matching Origin (CSRF attack via a malicious page opened alongside
        the app).

        Why the change is safe: the before_request hook runs before any
        handler code; the response shape (``{"error": "Forbidden"}``, 403)
        is byte-identical to the old inline pattern; no migration needed.

        Invariant: request.path.startswith("/api/") AND NOT origin_is_local()
        → HTTP 403 with ``{"error": "Forbidden"}``.
        """
        # Use a fresh test client WITHOUT the CSRF header.
        client = app.test_client()
        seen = set()
        for rule in app.url_map.iter_rules():
            if not rule.rule.startswith("/api/"):
                continue
            # Normalise to just the path (skip HEAD which Flask auto-adds).
            for method in (rule.methods - {"OPTIONS", "HEAD"}):
                key = (rule.rule, method)
                if key in seen:
                    continue
                seen.add(key)
                if method == "GET":
                    resp = client.get(rule.rule)
                elif method == "POST":
                    resp = client.post(rule.rule)
                elif method == "DELETE":
                    resp = client.delete(rule.rule)
                else:
                    continue
                self.assertEqual(
                    resp.status_code, 403,
                    f"{method} {rule.rule} returned {resp.status_code}, "
                    f"expected 403 without X-Requested-With header",
                )

    def test_origin_guard_allows_with_csrf_header(self):
        """``GET /api/status`` succeeds with the correct CSRF header."""
        resp = self.client.get("/api/status", headers=self.headers)
        self.assertEqual(resp.status_code, 200)

    def test_dns_rebinding_foreign_host_is_forbidden(self):
        """2026-07-20 audit H1: a DNS-rebound page is same-origin (custom
        header allowed, no preflight), sends no Origin/Referer, and hits a
        loopback remote_addr — but its Host header is the attacker's domain.
        The Host allowlist must reject it (403) so config/log reads can't be
        exfiltrated."""
        resp = self.client.get(
            "/api/config",
            headers={"X-Requested-With": "ChatEKLD", "Host": "evil.example.com"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_localhost_host_variants_allowed(self):
        """The PyWebView shapes (localhost / 127.0.0.1, with or without a
        port) still pass the Host allowlist."""
        for host in ("localhost", "127.0.0.1", "localhost:5000", "127.0.0.1:8080"):
            resp = self.client.get(
                "/api/status",
                headers={"X-Requested-With": "ChatEKLD", "Host": host},
            )
            self.assertEqual(resp.status_code, 200, host)


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

    def test_load_config_readonly_reflects_values_and_blocks_mutation(self):
        # W7b: the read-only view returns the same values as load_config, does no
        # deepcopy, and blocks top-level mutation so it can't poison the cache.
        from core.config import load_config, load_config_readonly, save_config
        save_config({"llm": "ro-test-model"})
        ro = load_config_readonly()
        self.assertEqual(ro.get("llm"), "ro-test-model")
        with self.assertRaises(TypeError):      # MappingProxyType is read-only
            ro["llm"] = "should-fail"
        # A subsequent save is reflected (the cache is replaced, not mutated).
        save_config({"llm": "ro-test-model-2"})
        self.assertEqual(load_config_readonly().get("llm"), "ro-test-model-2")
        # The read-only view never leaks a mutation into what load_config returns.
        self.assertEqual(load_config().get("llm"), "ro-test-model-2")


class TestConfigVersionMigration(unittest.TestCase):
    """Track 5.7 pinning: config_version stamping + the migration chain.

    Invariant: after any load, the persisted file is at CONFIG_VERSION and
    contains only KNOWN_KEYS; a client can never set config_version; a
    future-versioned file (downgraded app) is never migrated backwards.
    """

    def setUp(self):
        import tempfile as _tempfile
        from unittest import mock
        self._tmpdir = _tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.config_path = os.path.join(self._tmpdir.name, "config.json")
        patcher = mock.patch("core.config.CONFIG_FILE", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_raw(self, data: dict) -> None:
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def _read_raw(self) -> dict:
        with open(self.config_path, encoding="utf-8") as f:
            return json.load(f)

    def test_every_persisted_key_is_known(self):
        # KNOWN_KEYS must cover every key any code path persists — the 0->1
        # prune migration deletes unknown keys, and the POST /api/config
        # whitelist drops them. obsidian_vault_path was the historical gap:
        # persisted by rag/vault.py + the config route but absent from
        # _DEFAULTS, so the whitelist silently un-persisted a vault path set
        # via the route, and the migration would have wiped it from existing
        # configs. Add new persisted keys to _DEFAULTS, never around it.
        from core.config import KNOWN_KEYS

        # Forward guard for the un-persist FAMILY (2026-07-05 audit m1): the
        # old test only checked two hardcoded keys, so a NEW Settings knob given
        # a validator but no _DEFAULTS entry would silently un-persist with a
        # green suite (the exact 4.10 class). EVERY key the Settings route
        # validates + persists must be in KNOWN_KEYS — otherwise POST
        # /api/config validates it and then the same request's whitelist drops
        # it. This now fails HERE instead of shipping.
        from api.routes.config import _CONFIG_VALIDATORS
        undefaulted = sorted(set(_CONFIG_VALIDATORS) - set(KNOWN_KEYS))
        self.assertEqual(
            undefaulted, [],
            "Settings validators persist keys absent from core.config._DEFAULTS "
            f"(they will be silently un-persisted): {undefaulted}",
        )

        # Route-persisted keys that live outside the validator map.
        self.assertIn("obsidian_vault_path", KNOWN_KEYS)
        self.assertIn("config_version", KNOWN_KEYS)

    def test_tagging_zotero_api_base_shape_validated(self):
        # 2026-07-20 audit M3: the Zotero base is a stored dial target, so it
        # gets the http(s) host shape gate, not a bare length cap.
        from api.routes.config import _CONFIG_VALIDATORS
        v = _CONFIG_VALIDATORS["tagging_zotero_api_base"]
        self.assertEqual(v("http://127.0.0.1:23119/api"), "http://127.0.0.1:23119/api")
        self.assertEqual(v(""), "")                      # empty kept ⇒ default
        self.assertIsNone(v("not a url with spaces"))    # garbage dropped
        self.assertIsNone(v("http://evil\ninternal"))    # control chars dropped

    def test_tagging_zotero_web_api_base_is_https_only(self):
        # E2 (2026-07-20 audit): the WEB base carries the API key, so plain http
        # (a cleartext-credential dial target) and non-http schemes are dropped;
        # the loopback LOCAL base keeps http (127.0.0.1).
        from api.routes.config import _CONFIG_VALIDATORS
        v = _CONFIG_VALIDATORS["tagging_zotero_web_api_base"]
        self.assertEqual(v("https://api.zotero.org"), "https://api.zotero.org")
        self.assertEqual(v(""), "")                        # empty kept ⇒ default
        self.assertIsNone(v("http://api.zotero.org"))      # cleartext dropped
        self.assertIsNone(v("file:///etc/passwd"))         # non-http dropped

    def test_tagging_zotero_transport_enum_validated(self):
        """The transport reaches network routing, so an unknown value must be
        dropped (keeping the prior setting) rather than stored."""
        from api.routes.config import _CONFIG_VALIDATORS
        v = _CONFIG_VALIDATORS["tagging_zotero_transport"]
        for good in ("auto", "local", "web"):
            self.assertEqual(v(good), good)
        self.assertIsNone(v("sqlite"))   # never a valid transport
        self.assertIsNone(v("WEB "))     # not silently normalised

    def test_zotero_api_key_is_never_persisted_to_config(self):
        """Provider-key discipline: the Zotero key is env-only, like every
        other API key — it must not become a config default or a stored key."""
        from core.config import _DEFAULTS
        self.assertNotIn("tagging_zotero_api_key", _DEFAULTS)
        self.assertNotIn("zotero_api_key", _DEFAULTS)
        for key, val in _DEFAULTS.items():
            if "zotero" in key and isinstance(val, str):
                self.assertNotIn("Zotero-API-Key", val)

    def test_tagging_propose_model_rejects_control_chars(self):
        from api.routes.config import _CONFIG_VALIDATORS
        v = _CONFIG_VALIDATORS["tagging_propose_model"]
        self.assertEqual(v("gemini-2.5-flash"), "gemini-2.5-flash")
        self.assertEqual(v(""), "")                      # empty ⇒ chat model
        self.assertIsNone(v("model\nwith\nnewlines"))

    def test_unversioned_file_is_migrated_pruned_and_stamped(self):
        from core.config import CONFIG_VERSION, load_config
        # A pre-version config: one real user key + one key retired long ago.
        self._write_raw({"llm": "user-model", "some_retired_knob_2024": 5})
        cfg = load_config()
        self.assertEqual(cfg.get("llm"), "user-model")
        self.assertNotIn("some_retired_knob_2024", cfg)
        on_disk = self._read_raw()
        self.assertEqual(on_disk.get("config_version"), CONFIG_VERSION)
        self.assertNotIn("some_retired_knob_2024", on_disk)  # pruned, persisted
        self.assertEqual(on_disk.get("llm"), "user-model")   # user value kept

    def test_version_1_config_retires_library_root(self):
        """2026-07-13: the 1→2 prune migration drops `library_root` (a declared
        but unreferenced key) from an existing version-1 config."""
        from core.config import CONFIG_VERSION, load_config
        self._write_raw({"config_version": 1, "llm": "m", "library_root": "/x"})
        cfg = load_config()
        self.assertNotIn("library_root", cfg)
        self.assertEqual(cfg.get("llm"), "m")
        on_disk = self._read_raw()
        self.assertEqual(on_disk.get("config_version"), CONFIG_VERSION)
        self.assertNotIn("library_root", on_disk)

    def test_version_3_config_resets_retired_deepseek_alias(self):
        """2026-07-30 (fix-plan Plan 2): the 3→4 migration resets a persisted
        legacy DeepSeek alias — the provider's 2026-07-24 sunset stopped the
        curated list OFFERING deepseek-chat/reasoner, but a selection persisted
        before it kept 404ing every call. Any other value is user-chosen and
        must survive."""
        from core.config import _DEFAULTS, CONFIG_VERSION, load_config
        self._write_raw({"config_version": 3, "deepseek_model": "deepseek-chat",
                         "llm": "m"})
        cfg = load_config()
        self.assertEqual(cfg.get("deepseek_model"), _DEFAULTS["deepseek_model"])
        self.assertEqual(cfg.get("llm"), "m")  # unrelated user value kept
        on_disk = self._read_raw()
        self.assertEqual(on_disk.get("config_version"), CONFIG_VERSION)
        self.assertEqual(on_disk.get("deepseek_model"), _DEFAULTS["deepseek_model"])

    def test_version_3_config_keeps_a_real_deepseek_model(self):
        from core.config import load_config
        self._write_raw({"config_version": 3, "deepseek_model": "deepseek-v4-pro"})
        self.assertEqual(load_config().get("deepseek_model"), "deepseek-v4-pro")

    def test_version_4_resets_only_retired_model_selections(self):
        from core.config import CONFIG_VERSION, _DEFAULTS, load_config
        self._write_raw({
            "config_version": 4,
            "openai_model": "o1-mini",
            "google_model": "gemini-2.0-flash",
            "deepseek_model": "deepseek-v4-flash",
            "anthropic_model": "claude-haiku-4-5",
            "fallback_provider": "google",
            "deck_review_model": "o1-preview",
            "refactor_extract_model": "gemini-1.5-pro",
            "refactor_review_model": "gemini-2.0-flash-exp",
            "tagging_propose_model": "deepseek-v4-flash",
        })
        cfg = load_config()
        self.assertEqual(CONFIG_VERSION, 5)
        for key in ("openai_model", "google_model", "deepseek_model"):
            self.assertEqual(cfg[key], _DEFAULTS[key])
        self.assertEqual(cfg["anthropic_model"], "claude-haiku-4-5")
        self.assertEqual(cfg["fallback_provider"], "google")
        for key in ("deck_review_model", "tagging_propose_model"):
            self.assertEqual(cfg[key], _DEFAULTS[key])
        self.assertEqual(self._read_raw()["config_version"], 5)

    def test_migrate_3_to_4_resets_both_aliases(self):
        from core.config import _DEFAULTS, _migrate_3_to_4
        for alias in ("deepseek-chat", "deepseek-reasoner"):
            out = _migrate_3_to_4({"deepseek_model": alias})
            self.assertEqual(out["deepseek_model"], _DEFAULTS["deepseek_model"])

    def test_current_version_file_is_not_rewritten(self):
        from core.config import CONFIG_VERSION, load_config
        self._write_raw({"config_version": CONFIG_VERSION, "llm": "m"})
        before = os.stat(self.config_path).st_mtime_ns
        load_config()
        self.assertEqual(os.stat(self.config_path).st_mtime_ns, before)

    def test_future_version_is_left_alone(self):
        from core.config import CONFIG_VERSION, load_config
        self._write_raw({"config_version": CONFIG_VERSION + 7, "llm": "m"})
        before = os.stat(self.config_path).st_mtime_ns
        cfg = load_config()
        # Never migrated backwards, never rewritten; the newer stamp survives
        # into the merged view (an older app must not clobber a newer schema).
        self.assertEqual(os.stat(self.config_path).st_mtime_ns, before)
        self.assertEqual(cfg.get("config_version"), CONFIG_VERSION + 7)

    def test_save_config_restamps_version_and_survives_old_file(self):
        from core.config import CONFIG_VERSION, load_config, save_config
        # Old, unversioned file + a save that tries to forge the version:
        # exercises the migrate-inside-save reentrancy (RLock) AND the
        # server-owned stamp in one flow.
        self._write_raw({"llm": "old-model", "zombie_key": 1})
        save_config({"llm": "new-model", "config_version": 0})
        on_disk = self._read_raw()
        self.assertEqual(on_disk.get("config_version"), CONFIG_VERSION)
        self.assertEqual(on_disk.get("llm"), "new-model")
        self.assertNotIn("zombie_key", on_disk)
        self.assertEqual(load_config().get("llm"), "new-model")


class Plan3CorrectnessFixesTest(unittest.TestCase):
    """2026-07-30 fix-plan Plan 3: four small correctness deferrals from the
    2026-07-20 audit (init_db swallowing, atomic-writer mode clobber,
    load_config_readonly's empty proxy, the Lua-filter trust gap)."""

    # ---- P3.4: init_db fails loudly ---------------------------------------

    def test_init_db_raises_instead_of_booting_without_a_table(self):
        import sqlite3
        from unittest import mock
        from core.database import init_db
        # A failed CREATE TABLE used to log one line and let the app boot with
        # no uploads table, surfacing as a distant 500 on the first upload.
        with mock.patch("core.database.sqlite3.connect",
                        side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                init_db()

    # ---- P3.6: atomic writers preserve an existing file's mode ------------

    def test_write_text_atomic_preserves_destination_mode(self):
        import tempfile as _tempfile
        from core.utils import write_text_atomic
        with _tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "note.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("original")
            os.chmod(path, 0o644)
            write_text_atomic(path, "rewritten")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "rewritten")

    def test_write_bytes_atomic_preserves_destination_mode(self):
        import tempfile as _tempfile
        from core.utils import write_bytes_atomic
        with _tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "img.png")
            with open(path, "wb") as fh:
                fh.write(b"old")
            os.chmod(path, 0o664)
            write_bytes_atomic(path, b"new")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o664)

    def test_atomic_writers_keep_0600_for_a_new_file(self):
        # A file we CREATE must not be invented as world-readable — only an
        # existing destination's own mode is inherited.
        import tempfile as _tempfile
        from core.utils import write_bytes_atomic, write_text_atomic
        with _tempfile.TemporaryDirectory() as d:
            text_path = os.path.join(d, "fresh.json")
            write_text_atomic(text_path, "{}")
            self.assertEqual(os.stat(text_path).st_mode & 0o777, 0o600)
            bytes_path = os.path.join(d, "fresh.bin")
            write_bytes_atomic(bytes_path, b"x")
            self.assertEqual(os.stat(bytes_path).st_mode & 0o777, 0o600)

    def test_mode_inherit_failure_does_not_fail_the_write(self):
        # Best-effort by design: an exotic FS that refuses chmod must still get
        # the content the caller asked to persist.
        import tempfile as _tempfile
        from unittest import mock
        from core.utils import write_text_atomic
        with _tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "note.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("old")
            with mock.patch("core.utils.os.chmod", side_effect=OSError("nope")):
                write_text_atomic(path, "new")
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "new")

    # ---- P3.7: load_config_readonly never returns an empty mapping --------

    def test_load_config_readonly_survives_a_concurrent_invalidation(self):
        from unittest import mock
        import core.config as cfgmod
        from core.config import load_config_readonly
        # Simulate the race the audit found: every attempt to read the cache
        # finds it nulled by a concurrent save_config. The old code fell
        # through to MappingProxyType({}) — a reader that iterated saw no keys.
        with mock.patch.object(cfgmod, "_config_cache", None), \
                mock.patch.object(cfgmod, "_config_cache_key", None), \
                mock.patch.object(cfgmod, "load_config",
                                  side_effect=lambda: {"provider": "ollama"}):
            proxy = load_config_readonly()
        self.assertTrue(len(proxy) > 0)
        self.assertEqual(proxy.get("provider"), "ollama")

    def test_load_config_readonly_returns_a_populated_proxy_normally(self):
        from core.config import load_config_readonly
        proxy = load_config_readonly()
        self.assertIn("provider", proxy)
        with self.assertRaises(TypeError):   # still read-only
            proxy["provider"] = "x"

    # The P3.9 trust-contract test (check_tex vs the GUI copy) moved to
    # maintenance/tests/test_steps_phase3.py::TestLatexSweep when
    # scripts/vault_maintain/ was decommissioned (2026-08-21): the GUI no
    # longer exists, and the surviving copy is
    # maintenance/steps/latex.py::_check_file_trust.


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
