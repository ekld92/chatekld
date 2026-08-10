"""Tests for core/server_info.py — the CLI↔GUI port-discovery handshake (C0)."""
import json
import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core import server_info  # noqa: E402


class TestServerInfo(unittest.TestCase):
    def tearDown(self):
        try:
            os.unlink(server_info.SERVER_INFO_FILE)
        except OSError:
            pass

    def test_round_trip(self):
        server_info.write_server_info(43210)
        info = server_info.read_server_info()
        self.assertEqual(info["port"], 43210)
        self.assertEqual(info["host"], "127.0.0.1")
        self.assertEqual(info["pid"], os.getpid())

    def test_malformed_file_reads_none(self):
        with open(server_info.SERVER_INFO_FILE, "w") as fh:
            fh.write("{not json")
        self.assertIsNone(server_info.read_server_info())
        with open(server_info.SERVER_INFO_FILE, "w") as fh:
            json.dump({"version": 1, "port": "not-int", "pid": 1, "host": "x"}, fh)
        self.assertIsNone(server_info.read_server_info())
        with open(server_info.SERVER_INFO_FILE, "w") as fh:
            json.dump({"version": 99, "port": 1, "pid": 1, "host": "x"}, fh)
        self.assertIsNone(server_info.read_server_info())

    def test_clear_refuses_another_pids_record(self):
        """The pid gate is the shutdown-race guard: OUR cleanup must never
        delete a record belonging to a different (possibly live) server."""
        server_info.write_server_info(43210)
        raw = json.loads(open(server_info.SERVER_INFO_FILE).read())
        raw["pid"] = os.getpid() + 1
        with open(server_info.SERVER_INFO_FILE, "w") as fh:
            json.dump(raw, fh)
        server_info.clear_server_info()
        self.assertTrue(os.path.exists(server_info.SERVER_INFO_FILE))

    def test_clear_deletes_own_record(self):
        server_info.write_server_info(43210)
        server_info.clear_server_info()
        self.assertFalse(os.path.exists(server_info.SERVER_INFO_FILE))

    def test_pid_alive(self):
        self.assertTrue(server_info.pid_alive(os.getpid()))
        # A pid far above any realistic pid_max on macOS/Linux desktops.
        self.assertFalse(server_info.pid_alive(2 ** 22 + 54321))

    def test_write_failure_is_swallowed(self):
        with mock.patch("core.server_info.write_text_atomic",
                        side_effect=OSError("disk full")):
            server_info.write_server_info(43210)   # must not raise


class TestInstallCli(unittest.TestCase):
    def test_shim_written_idempotent_and_collision_refused(self):
        import tempfile
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        import install_cli
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(install_cli.install(td, force=False), 0)
            target = os.path.join(td, "papermind")
            self.assertTrue(os.path.exists(target))
            self.assertTrue(os.access(target, os.X_OK))
            content = open(target).read()
            self.assertIn(install_cli.MARKER, content)
            self.assertIn(sys.executable, content)
            # Idempotent re-run.
            self.assertEqual(install_cli.install(td, force=False), 0)
            # A non-shim file at the target is refused without --force.
            with open(target, "w") as fh:
                fh.write("#!/bin/sh\necho user script\n")
            self.assertEqual(install_cli.install(td, force=False), 1)
            self.assertIn("user script", open(target).read())
            # --force overwrites.
            self.assertEqual(install_cli.install(td, force=True), 0)
            self.assertIn(install_cli.MARKER, open(target).read())


if __name__ == "__main__":
    unittest.main()
