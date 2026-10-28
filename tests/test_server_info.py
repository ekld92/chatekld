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


class TestEnsureServerInfo(unittest.TestCase):
    """The self-heal for a vanished server.json (2026-08-12 lead).

    The GUI can serve and answer /api/health while BASE_DIR/server.json is
    absent, which silently disables every CLI/MCP *write*. Cause unknown, so
    /api/health re-asserts the record rather than pretending to fix it.
    """

    def setUp(self):
        self._saved = server_info._OWN_REGISTRATION
        server_info._OWN_REGISTRATION = None

    def tearDown(self):
        server_info._OWN_REGISTRATION = self._saved
        try:
            os.unlink(server_info.SERVER_INFO_FILE)
        except OSError:
            pass

    def test_noop_when_this_process_never_registered(self):
        """A CLI/test import must never conjure a record for a server it isn't."""
        self.assertFalse(server_info.ensure_server_info())
        self.assertIsNone(server_info.read_server_info())

    def test_restores_a_deleted_record(self):
        server_info.write_server_info(44100)
        os.unlink(server_info.SERVER_INFO_FILE)          # the observed failure
        self.assertTrue(server_info.ensure_server_info())
        info = server_info.read_server_info()
        self.assertEqual(info["port"], 44100)
        self.assertEqual(info["pid"], os.getpid())

    def test_noop_when_our_record_is_already_correct(self):
        server_info.write_server_info(44101)
        self.assertFalse(server_info.ensure_server_info())

    def test_never_steals_a_live_other_pids_record(self):
        """Overwriting another server's claim would break clear_server_info's
        pid gate — the whole reason concurrent instances are safe today."""
        server_info.write_server_info(44102)
        with open(server_info.SERVER_INFO_FILE, "w") as fh:
            json.dump({"version": 1, "host": "127.0.0.1", "port": 999,
                       "pid": os.getpid() + 1, "created_at": 0.0}, fh)
        with mock.patch.object(server_info, "pid_alive", return_value=True), \
                mock.patch.object(server_info, "_health_ok", return_value=True):
            self.assertFalse(server_info.ensure_server_info())
        self.assertEqual(server_info.read_server_info()["port"], 999)

    def test_reclaims_a_live_pid_that_is_not_actually_serving(self):
        """Post-crash pid reuse (2026-08-18 review): a record naming a pid that
        happens to be alive but answers no /api/health on its claimed port
        would otherwise disable the self-heal forever. The probe is what the
        driver itself applies before believing the record, so requiring it
        here strengthens the heal without weakening the never-steal rule."""
        server_info.write_server_info(44105)
        with open(server_info.SERVER_INFO_FILE, "w") as fh:
            json.dump({"version": 1, "host": "127.0.0.1", "port": 999,
                       "pid": os.getpid() + 1, "created_at": 0.0}, fh)
        with mock.patch.object(server_info, "pid_alive", return_value=True), \
                mock.patch.object(server_info, "_health_ok", return_value=False):
            self.assertTrue(server_info.ensure_server_info())
        self.assertEqual(server_info.read_server_info()["port"], 44105)

    def test_reclaims_a_dead_pids_record(self):
        server_info.write_server_info(44103)
        with open(server_info.SERVER_INFO_FILE, "w") as fh:
            json.dump({"version": 1, "host": "127.0.0.1", "port": 999,
                       "pid": os.getpid() + 1, "created_at": 0.0}, fh)
        with mock.patch.object(server_info, "pid_alive", return_value=False):
            self.assertTrue(server_info.ensure_server_info())
        self.assertEqual(server_info.read_server_info()["port"], 44103)

    def test_corrects_our_own_stale_port(self):
        server_info.write_server_info(44104)
        with open(server_info.SERVER_INFO_FILE, "w") as fh:
            json.dump({"version": 1, "host": "127.0.0.1", "port": 1,
                       "pid": os.getpid(), "created_at": 0.0}, fh)
        self.assertTrue(server_info.ensure_server_info())
        self.assertEqual(server_info.read_server_info()["port"], 44104)


if __name__ == "__main__":
    unittest.main()
