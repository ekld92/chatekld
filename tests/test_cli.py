"""Unit tests for cli/main.py command line interface."""
import json
import os
import sys
import unittest
from click.testing import CliRunner

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from unittest import mock  # noqa: E402

from cli.main import cli  # noqa: E402


class TestCLI(unittest.TestCase):

    def setUp(self):
        self.runner = CliRunner()
        # Hermeticity (2026-07-22 audit M6): the status path previously probed
        # the REAL port 5000, so a live server there flipped these tests. The
        # env override is authoritative and read at call time (C0), so the
        # discard port makes every probe fail fast and deterministic.
        self._env = mock.patch.dict(os.environ,
                                    {"CHATEKLD_API_URL": "http://127.0.0.1:9"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_status_command(self):
        result = self.runner.invoke(cli, ["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("PaperMind Status", result.output)
        # C3: the printer reads keys that actually exist in the shape.
        self.assertNotIn("Status: None", result.output)
        self.assertIn("Server: not running", result.output)

    def test_status_command_json(self):
        result = self.runner.invoke(cli, ["status", "--json-out"])
        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.output)
        self.assertEqual(data["status"], "ok")
        # The in-process fallback returns the RICH vault_index shape (C3).
        self.assertIsInstance(data["vault_index"], dict)
        self.assertIn("state", data["vault_index"])

    def test_config_command(self):
        result = self.runner.invoke(cli, ["config"])
        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.output)
        self.assertIn("provider", data)

    def test_paper_summarize_missing_file(self):
        result = self.runner.invoke(cli, ["paper", "summarize", "/invalid/path.pdf"])
        self.assertNotEqual(result.exit_code, 0)

    def test_deck_generate_requires_template(self):
        # C1: --template is a required option now — the old command could
        # never succeed (the route hard-400s without template_tex).
        result = self.runner.invoke(cli, ["deck", "generate", "Topic"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--template", result.output)

    def test_docgen_latex_requires_template(self):
        result = self.runner.invoke(
            cli, ["docgen", "generate", "Topic", "--mode", "latex"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--template", result.output)

    def test_vault_index_has_no_force_flag(self):
        # C1: --force sent a body key the server never read; it must be gone
        # rather than silently inert.
        result = self.runner.invoke(cli, ["vault", "index", "--force"])
        self.assertNotEqual(result.exit_code, 0)

    def test_server_refuses_non_loopback_bind(self):
        # C5: --unsafe-bind removed; a non-loopback host is a hard error
        # (the origin guard fail-closes on remote clients anyway).
        result = self.runner.invoke(cli, ["server", "--host", "0.0.0.0"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("origin guard", result.output)

    def test_server_refuses_second_instance(self):
        # C0: two servers = two independent op-locks over one vault.
        from core import server_info
        server_info.write_server_info(43219)
        # Fake the recorded pid as alive + healthy.
        with mock.patch("core.server_info.pid_alive", return_value=True), \
                mock.patch("core.driver.is_server_running", return_value=True):
            try:
                result = self.runner.invoke(cli, ["server"])
            finally:
                try:
                    os.unlink(server_info.SERVER_INFO_FILE)
                except OSError:
                    pass
        self.assertEqual(result.exit_code, 1)
        self.assertIn("already running", result.output)


if __name__ == "__main__":
    unittest.main()
