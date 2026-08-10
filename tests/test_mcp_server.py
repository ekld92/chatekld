"""Unit tests for mcp_server/server.py MCP JSON-RPC protocol implementation."""
import json
import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server.server import handle_request  # noqa: E402


class TestMCPServer(unittest.TestCase):

    def setUp(self):
        # Hermeticity (2026-07-22 audit M6): the in-process branches previously
        # probed the REAL port 5000 — a live server on it flipped test results.
        # Since C0 the driver reads CHATEKLD_API_URL at call time, so pointing
        # it at the discard port makes the probe fail fast and deterministic.
        self._env = mock.patch.dict(os.environ,
                                    {"CHATEKLD_API_URL": "http://127.0.0.1:9"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_initialize_request(self):
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
        res = handle_request(req)
        self.assertEqual(res["jsonrpc"], "2.0")
        self.assertEqual(res["id"], 1)
        self.assertIn("capabilities", res["result"])
        self.assertEqual(res["result"]["serverInfo"]["name"], "papermind-mcp")

    def test_initialize_echoes_client_protocol_version(self):
        req = {"jsonrpc": "2.0", "id": 10, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18"}}
        res = handle_request(req)
        self.assertEqual(res["result"]["protocolVersion"], "2025-06-18")

    def test_non_object_params_is_invalid_params_not_silence(self):
        """2026-07-22 audit M2: params of the wrong type previously escaped
        handle_request as an AttributeError and the client hung forever."""
        req = {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
               "params": "bogus"}
        res = handle_request(req)
        self.assertEqual(res["id"], 11)
        self.assertEqual(res["error"]["code"], -32602)

    def test_unknown_tool_is_invalid_params_not_internal_error(self):
        req = {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
               "params": {"name": "no_such_tool", "arguments": {}}}
        res = handle_request(req)
        self.assertEqual(res["error"]["code"], -32602)
        self.assertIn("no_such_tool", res["error"]["message"])

    def test_non_string_tool_name_rejected(self):
        req = {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
               "params": {"name": 42, "arguments": {}}}
        res = handle_request(req)
        self.assertEqual(res["error"]["code"], -32602)

    def test_resources_read_status_failure_answers_with_error(self):
        """A raising get_status() must produce a -32603 WITH the id, never a
        dropped request (audit M2)."""
        with mock.patch("mcp_server.server.get_status",
                        side_effect=RuntimeError("boom /Users/secret/path")):
            res = handle_request({"jsonrpc": "2.0", "id": 14,
                                  "method": "resources/read",
                                  "params": {"uri": "papermind://status"}})
        self.assertEqual(res["id"], 14)
        self.assertEqual(res["error"]["code"], -32603)
        # sanitised: the raw /Users/<name>/ path is redacted
        self.assertNotIn("/Users/secret/", res["error"]["message"])

    def test_process_line_answers_even_when_handler_crashes(self):
        from mcp_server import server as srv
        sent = []
        with mock.patch.object(srv, "handle_request",
                               side_effect=RuntimeError("crash")), \
                mock.patch.object(srv, "send_response", side_effect=sent.append):
            srv.process_line(json.dumps({"jsonrpc": "2.0", "id": 15,
                                         "method": "ping"}))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["id"], 15)
        self.assertEqual(sent[0]["error"]["code"], -32603)

    def test_write_semaphore_bounded_and_not_double_released(self):
        """2026-07-22 audit M9: a held write slot must yield a busy error, not
        an unbounded pool-starving wait — and the timeout path must not
        release someone else's slot."""
        from mcp_server import server as srv
        srv._write_semaphore.acquire()
        try:
            with mock.patch.object(srv, "WRITE_LOCK_TIMEOUT_S", 0.05):
                with self.assertRaises(srv.ServerBusyError):
                    srv.handle_tool_call("biblio_apply_tags",
                                         {"citation_key": "x", "tags": ["t"]})
        finally:
            srv._write_semaphore.release()
        # The slot is free again exactly once (no double release).
        self.assertTrue(srv._write_semaphore.acquire(timeout=1))
        srv._write_semaphore.release()

    def test_markdown_generate_document_is_a_write_call(self):
        """2026-07-22 audit M3: the markdown mode writes a vault note and must
        ride the write semaphore + audit line; latex mode is emit-only."""
        from mcp_server.server import _is_write_call
        self.assertTrue(_is_write_call("generate_document", {}))
        self.assertTrue(_is_write_call("generate_document", {"mode": "markdown"}))
        self.assertFalse(_is_write_call("generate_document", {"mode": "latex"}))
        self.assertTrue(_is_write_call("biblio_apply_tags", {}))
        self.assertFalse(_is_write_call("vault_search", {}))

    def test_generate_tool_schemas_carry_template_path(self):
        """2026-07-22 audit H1: the generate tools were dead on arrival — the
        schemas must now surface template_path (required for the deck)."""
        from mcp_server.server import MCP_TOOLS
        by_name = {t["name"]: t for t in MCP_TOOLS}
        deck = by_name["generate_deck"]["inputSchema"]
        self.assertIn("template_path", deck["properties"])
        self.assertIn("template_path", deck["required"])
        doc = by_name["generate_document"]["inputSchema"]
        self.assertIn("template_path", doc["properties"])
        # vault_search no longer advertises the silently-ignored mode.
        self.assertNotIn("mode", by_name["vault_search"]["inputSchema"]["properties"])

    def test_notification_ignored(self):
        req = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        res = handle_request(req)
        self.assertIsNone(res)

    def test_tools_list_request(self):
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        res = handle_request(req)
        self.assertEqual(res["jsonrpc"], "2.0")
        self.assertEqual(res["id"], 2)
        tools = res["result"]["tools"]
        self.assertTrue(len(tools) >= 5)
        tool_names = [t["name"] for t in tools]
        self.assertIn("vault_search", tool_names)
        self.assertIn("vault_ask", tool_names)
        self.assertIn("summarize_paper", tool_names)
        self.assertIn("audit_library", tool_names)

    def test_resources_list_request(self):
        req = {"jsonrpc": "2.0", "id": 3, "method": "resources/list"}
        res = handle_request(req)
        self.assertEqual(res["jsonrpc"], "2.0")
        self.assertIn("resources", res["result"])
        uris = [r["uri"] for r in res["result"]["resources"]]
        self.assertIn("papermind://status", uris)

    def test_resources_read_status(self):
        req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": "papermind://status"},
        }
        res = handle_request(req)
        self.assertEqual(res["jsonrpc"], "2.0")
        contents = res["result"]["contents"]
        self.assertEqual(len(contents), 1)
        payload = json.loads(contents[0]["text"])
        self.assertEqual(payload["status"], "ok")


class TestInstallMcpCursorRegistration(unittest.TestCase):
    """scripts/install_mcp.py Cursor half (2026-08-08): a JSON-file merge, so
    the conservative-IO contract is what needs pinning — other servers and
    unrelated keys survive, papermind is created-or-refreshed, and an
    unparseable existing file is NEVER overwritten (snippet printed instead)."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "cursor", "mcp.json")

    def test_merge_preserves_other_servers_and_keys(self):
        from scripts.install_mcp import SERVER_NAME, merge_cursor_config, server_entry
        existing = {"mcpServers": {"other": {"command": "x"}}, "unrelated": 1}
        merged = merge_cursor_config(existing)
        self.assertEqual(merged["mcpServers"]["other"], {"command": "x"})
        self.assertEqual(merged["unrelated"], 1)
        self.assertEqual(merged["mcpServers"][SERVER_NAME], server_entry())
        # A stale papermind entry (moved repo/venv) is refreshed, not kept.
        stale = {"mcpServers": {SERVER_NAME: {"command": "/old/python"}}}
        self.assertEqual(
            merge_cursor_config(stale)["mcpServers"][SERVER_NAME], server_entry())
        self.assertEqual(stale["mcpServers"][SERVER_NAME]["command"],
                         "/old/python")  # input not mutated

    def test_register_creates_file_and_parent_dir(self):
        from scripts.install_mcp import SERVER_NAME, register_cursor, server_entry
        self.assertTrue(register_cursor(config_path=self.path))
        with open(self.path, encoding="utf-8") as fh:
            written = json.load(fh)
        self.assertEqual(written["mcpServers"][SERVER_NAME], server_entry())

    def test_register_merges_into_existing_config(self):
        from scripts.install_mcp import SERVER_NAME, register_cursor
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {"other": {"command": "x"}}}, fh)
        self.assertTrue(register_cursor(config_path=self.path))
        with open(self.path, encoding="utf-8") as fh:
            written = json.load(fh)
        self.assertIn("other", written["mcpServers"])
        self.assertIn(SERVER_NAME, written["mcpServers"])

    def test_register_writes_through_symlink(self):
        """2026-08-08 review B4: a dotfiles-managed mcp.json is a symlink;
        the write must go through to the TARGET (realpath) so the link
        survives — os.replace on the link path would swap the link itself
        for a regular file and silently detach it from the dotfiles repo."""
        from scripts.install_mcp import SERVER_NAME, register_cursor
        os.makedirs(os.path.dirname(self.path))
        real = os.path.join(self._tmp.name, "dotfiles-mcp.json")
        with open(real, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {"other": {"command": "x"}}}, fh)
        os.symlink(real, self.path)
        self.assertTrue(register_cursor(config_path=self.path))
        self.assertTrue(os.path.islink(self.path))  # link preserved
        with open(real, encoding="utf-8") as fh:
            written = json.load(fh)
        self.assertIn("other", written["mcpServers"])
        self.assertIn(SERVER_NAME, written["mcpServers"])

    def test_register_refuses_to_clobber_unparseable_file(self):
        from scripts.install_mcp import register_cursor
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertFalse(register_cursor(config_path=self.path))
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{not json")  # byte-identical

    def test_register_refuses_non_object_top_level(self):
        from scripts.install_mcp import register_cursor
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("[1, 2]")
        self.assertFalse(register_cursor(config_path=self.path))
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "[1, 2]")


if __name__ == "__main__":
    unittest.main()
