"""Unit tests for core/driver.py (the HTTP-first CLI/MCP driver).

The driver is a thin client over the app's HTTP API. These tests pin the
behaviour that the first (reverted-in-spirit) cut got wrong: the auth header is
always sent, writes require the running server, and path inputs are validated.
The contract test (test_driver_contracts.py) additionally guarantees every real
API the driver targets actually exists.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core import driver  # noqa: E402
from core.driver import (  # noqa: E402
    get_config,
    get_status,
    is_server_running,
    refactor_plan,
    set_config,
    summarize_paper,
    tagging_apply,
    vault_ask,
)


class TestDriver(unittest.TestCase):

    def test_is_server_running_returns_bool(self):
        # A dead port is not reachable.
        self.assertFalse(is_server_running(base_url="http://127.0.0.1:59999"))

    def test_health_probe_sends_the_app_header(self):
        # The origin guard 403s any /api request missing X-Requested-With, so
        # the health probe MUST send it (the first cut did not — every request
        # 403'd and the code always thought the server was down).
        captured = {}

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            return _Resp()

        with patch("core.driver.urllib.request.urlopen", side_effect=_fake_urlopen):
            self.assertTrue(is_server_running())
        # urllib title-cases header names.
        self.assertEqual(captured["headers"].get("X-requested-with"), "ChatEKLD")

    def test_get_config_returns_dict(self):
        cfg = get_config()
        self.assertIsInstance(cfg, dict)
        self.assertIn("provider", cfg)

    @patch("core.driver.is_server_running", return_value=False)
    @patch("rag.vault.obsidian_manager.get_status", return_value="idle")
    def test_get_status_in_process_fallback(self, _status, _srv):
        res = get_status()
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res.get("in_process"))
        self.assertIn("config", res)
        self.assertIn("vault_index", res)

    @patch("core.driver.is_server_running", return_value=False)
    def test_writes_require_the_server(self, _srv):
        # Every mutating op must refuse when the server is down rather than do
        # something unsafe in-process.
        for res in (
            set_config({"x": 1}),
            vault_ask("hello"),
            tagging_apply("smith2020", ["method"]),
        ):
            self.assertIn("error", res)
            self.assertIn("server", res["error"].lower())

    def test_summarize_paper_nonexistent_pdf(self):
        res = summarize_paper("/nonexistent/file.pdf")
        self.assertIn("error", res)

    def test_refactor_plan_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as vault:
            with patch("core.config.load_config",
                       return_value={"obsidian_vault_path": vault}):
                res = refactor_plan(subfolder="../../etc/passwd")
        self.assertIn("error", res)
        self.assertIn("escaping", res["error"].lower())

    def test_tagging_apply_builds_confirmed_fanout_body(self):
        # The write must carry confirm:true and route to the confirm-gated
        # fan-out endpoint (server-side vocab gate + journal are the safety net).
        sent = {}

        def _fake_http_json(endpoint_key, data=None, **k):
            sent["endpoint"] = driver.ENDPOINTS[endpoint_key][0]
            sent["data"] = data
            return {"results": {}}

        with patch("core.driver.resolve_base_url",
                   return_value="http://127.0.0.1:1"), \
                patch("core.driver._http_json", side_effect=_fake_http_json):
            res = tagging_apply("smith2020", ["method"], targets=["note"])
        self.assertEqual(res["status"], "ok")
        self.assertEqual(sent["endpoint"], "/api/tagging/apply-all")
        self.assertTrue(sent["data"]["confirm"])
        self.assertEqual(sent["data"]["targets"],
                         {"note": True, "finder": False, "zotero": False})

    def test_sse_consumer_assembles_tokens_and_terminal(self):
        # The SSE helper joins token frames and captures the terminal frame,
        # ignoring [DONE].
        lines = [
            b'data: {"info": "starting"}\n',
            b'data: {"token": "Hel"}\n',
            b'data: {"token": "lo"}\n',
            b'data: {"deck": {"tex_path": "/x/y.tex"}}\n',
            b'data: [DONE]\n',
        ]

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter(lines)

        with patch("core.driver.urllib.request.urlopen", return_value=_Resp()):
            out = driver._http_sse("deck_generate", {"topic": "x"},
                                   base_url="http://127.0.0.1:1")
        self.assertEqual(out["tokens"], "Hello")
        self.assertEqual(out["terminal"]["deck"]["tex_path"], "/x/y.tex")
        self.assertNotIn("error", out)

    def test_resolve_base_url_env_is_authoritative(self):
        # A set-but-dead CHATEKLD_API_URL must yield None with NO fallback —
        # silent fallback would defeat both user intent and test hermeticity.
        with patch.dict(os.environ, {"CHATEKLD_API_URL": "http://127.0.0.1:9"}), \
                patch("core.driver.is_server_running", return_value=False) as probe:
            self.assertIsNone(driver.resolve_base_url())
        probe.assert_called_once_with("http://127.0.0.1:9")

    def test_resolve_base_url_uses_live_server_info_file(self):
        from core import server_info
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CHATEKLD_API_URL", None)
            server_info.write_server_info(43210)
            try:
                with patch("core.driver.is_server_running",
                           side_effect=lambda u: u == "http://127.0.0.1:43210"):
                    self.assertEqual(driver.resolve_base_url(),
                                     "http://127.0.0.1:43210")
            finally:
                server_info.clear_server_info()

    def test_resolve_base_url_skips_stale_pid_then_probes_legacy(self):
        from core import server_info
        os.environ.pop("CHATEKLD_API_URL", None)
        server_info.write_server_info(43211)
        # Fake a dead recorded pid: rewrite with an unlikely-live pid.
        import json as _json
        raw = _json.loads(open(server_info.SERVER_INFO_FILE).read())
        raw["pid"] = 2 ** 22 + 12345
        open(server_info.SERVER_INFO_FILE, "w").write(_json.dumps(raw))
        try:
            probes = []
            with patch("core.driver.is_server_running",
                       side_effect=lambda u: probes.append(u) or False):
                self.assertIsNone(driver.resolve_base_url())
            # The dead-pid candidate was never probed; the legacy URL was.
            self.assertEqual(probes, [driver.LEGACY_SERVER_URL])
        finally:
            try:
                os.unlink(server_info.SERVER_INFO_FILE)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
