"""Unit tests for core/driver.py (the HTTP-first CLI/MCP driver).

The driver is a thin client over the app's HTTP API. These tests pin the
behaviour that the first (reverted-in-spirit) cut got wrong: the auth header is
always sent, writes require the running server, and path inputs are validated.
The contract test (test_driver_contracts.py) additionally guarantees every real
API the driver targets actually exists.
"""
import json
import os
import sys
import time
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

    def test_tagging_apply_empty_targets_writes_nothing_not_everything(self):
        """An EXPLICIT empty selection must serialise as all-false.

        The server reads an omitted `targets` key as "every target ON", so the
        old truthiness test (`if targets:`) turned an empty list into the exact
        opposite of what the caller asked for. Reachable from `papermind tagging
        batch --targets finder --no-pdf-entries` (the finder target is dropped
        per PDF-less row, emptying the list) and from any MCP client sending
        `"targets": []` — in both cases note + Zotero were written library-wide.
        """
        sent = {}

        def _fake_http_json(endpoint_key, data=None, **k):
            sent["data"] = data
            return {"results": {}}

        with patch("core.driver.resolve_base_url",
                   return_value="http://127.0.0.1:1"), \
                patch("core.driver._http_json", side_effect=_fake_http_json):
            tagging_apply("smith2020", ["method"], targets=[])
        self.assertEqual(sent["data"]["targets"],
                         {"note": False, "finder": False, "zotero": False})

    def test_tagging_apply_unset_targets_still_defaults_to_all(self):
        # `None` (unset) keeps the historical contract: omit the key and let
        # the server default every target ON. Only [] is "nothing selected".
        sent = {}

        def _fake_http_json(endpoint_key, data=None, **k):
            sent["data"] = data
            return {"results": {}}

        with patch("core.driver.resolve_base_url",
                   return_value="http://127.0.0.1:1"), \
                patch("core.driver._http_json", side_effect=_fake_http_json):
            tagging_apply("smith2020", ["method"])
        self.assertNotIn("targets", sent["data"])

    def test_tagging_apply_rejects_an_unknown_target_name(self):
        # A typo used to map every REAL target to false: the call wrote nothing,
        # returned 200 and reported success after paying for the LLM pass.
        with patch("core.driver.resolve_base_url",
                   return_value="http://127.0.0.1:1"), \
                patch("core.driver._http_json") as http:
            res = tagging_apply("smith2020", ["method"], targets=["notes"])
        self.assertIn("notes", res["error"])
        http.assert_not_called()

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


class TestTaggingModelMirror(unittest.TestCase):
    """_tagging_model must disclose what the route actually CALLS.

    The route resolves the override through coerce_string_max_len(…, 120) —
    non-string → unset, over-length → truncated. The driver mirrors those
    semantics (2026-08-13 review: it used the full string, so a >120-char
    override was disclosed in full while the route used its truncation).
    """

    def test_override_wins(self):
        cfg = {"provider": "openai", "openai_model": "gpt-5",
               "tagging_propose_model": "gpt-5-mini"}
        self.assertEqual(driver._tagging_model(cfg, "openai"), "gpt-5-mini")

    def test_empty_and_non_string_fall_through_to_the_chat_model(self):
        for override in ("", "   ", None, 42):
            cfg = {"provider": "openai", "openai_model": "gpt-5",
                   "tagging_propose_model": override}
            self.assertEqual(driver._tagging_model(cfg, "openai"), "gpt-5",
                             f"override={override!r}")

    def test_over_length_override_is_truncated_like_the_route(self):
        long_id = "m" * 130
        cfg = {"provider": "openai", "openai_model": "gpt-5",
               "tagging_propose_model": long_id}
        self.assertEqual(driver._tagging_model(cfg, "openai"), "m" * 120)


class TestDriverResponseProjections(unittest.TestCase):
    """Response-side projection ratchet (2026-08-18 review, B2).

    The replay ratchet covers REQUEST bodies; nothing ratcheted the driver's
    response projections — so `tagging_inventory`'s whitelist silently dropped
    `scanned_at` and the CLI batch's staleness disclosure was dead end-to-end
    while its tests passed (they mocked `cli.main.tagging_inventory`, above
    the seam). These tests mock at `_http_json` — the real driver projection
    runs — and assert every field a CLI consumer reads is forwarded.

    RATCHET RULE: when a CLI/MCP consumer starts reading a new field off a
    driver result, add that field to the expectation here IN THE SAME COMMIT.
    """

    def _call(self, fn, *args, route_payload=None, **kwargs):
        with patch("core.driver.resolve_base_url",
                   return_value="http://127.0.0.1:1"), \
                patch("core.driver._http_json",
                      return_value=route_payload or {}):
            return fn(*args, **kwargs)

    def test_vault_search_and_ask_forward_the_paper_field(self):
        """The chat ``retrieval`` frame's additive ``paper: {key, label}`` must
        survive both driver projections (paper labels, 2026-09-23). Fed through
        a fake SSE stream at ``urlopen`` so the real ``_http_sse`` runs."""
        row = {"source": "Z_attachments/biblio_articles/smith_2020.pdf",
               "score": 0.91, "is_image": False,
               "paper": {"key": "smithKey2020", "label": "Smith, 2020, A Title"}}
        lines = [f"data: {json.dumps({'retrieval': [row]})}\n".encode(),
                 b'data: {"token": "hi"}\n', b"data: [DONE]\n"]

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter(lines)

        with patch("core.driver.resolve_base_url", return_value="http://127.0.0.1:1"), \
                patch("core.driver.urllib.request.urlopen", side_effect=lambda *a, **k: _Resp()):
            search = driver.vault_search("q")
            ask = driver.vault_ask("q")
        self.assertEqual(search["results"][0]["paper"], row["paper"])
        self.assertEqual(search["results"][0]["source"], row["source"])
        self.assertEqual(ask["sources"][0]["paper"], row["paper"])

    def test_audit_summary_forwards_the_summary_block(self):
        payload = {"summary": {"record_count": 6311, "pdfs_unmapped": 371,
                               "tag_coverage": {"any_tagged": 5880}},
                   "records": ["a huge array that must NOT ride along"]}
        res = self._call(driver.audit_summary, route_payload=payload)
        self.assertEqual(res["summary"]["record_count"], 6311)
        self.assertEqual(res["summary"]["tag_coverage"]["any_tagged"], 5880)
        # The whole point of using /summary over /inventory.
        self.assertNotIn("records", res)

    def test_audit_inventory_forwards_records_and_summary(self):
        payload = {"summary": {"record_count": 2},
                   "records": [{"citation_key": "a2020"}]}
        res = self._call(driver.audit_inventory, route_payload=payload)
        self.assertEqual(res["records"], payload["records"])
        self.assertEqual(res["summary"], payload["summary"])

    def test_audit_report_echoes_the_name_and_keeps_the_body(self):
        """Reports differ in shape by design, so the body passes through — but
        `name` is echoed so a consumer can never mistake one report's rows for
        another's."""
        payload = {"name": "tag_hygiene", "rows": [{"fingerprint": "dep"}],
                   "vocab_size": 172}
        res = self._call(driver.audit_report, "tag_hygiene",
                         route_payload=payload)
        self.assertEqual(res["name"], "tag_hygiene")
        self.assertEqual(res["rows"], payload["rows"])
        self.assertEqual(res["vocab_size"], 172)

    def test_tagging_journal_stats_forwards_every_field_the_cli_reads(self):
        payload = {"ops": 11651, "bytes": 10661888, "protected": 11451,
                   "evictable": 200, "oldest_ts": "2026-07-18T20:19:45+00:00",
                   "newest_ts": "2026-08-25T12:22:00+00:00",
                   "retention": {"enabled": True, "policy": "age_days",
                                 "days": 365, "keep": 5000},
                   "by_kind_state": {"zotero_tag/applied": 7080},
                   "extra_route_field": "ignored"}
        res = self._call(driver.tagging_journal_stats, route_payload=payload)
        # cli/main.py::status_cmd reads: ops, bytes, protected, retention
        # (enabled/policy/days/keep). oldest/newest/evictable are forwarded for
        # the same consumer's future use and pinned so a whitelist edit is loud.
        self.assertEqual(res["ops"], 11651)
        self.assertEqual(res["bytes"], 10661888)
        self.assertEqual(res["protected"], 11451)
        self.assertEqual(res["evictable"], 200)
        self.assertEqual(res["oldest_ts"], payload["oldest_ts"])
        self.assertEqual(res["newest_ts"], payload["newest_ts"])
        self.assertEqual(res["retention"]["policy"], "age_days")
        self.assertEqual(res["retention"]["days"], 365)
        self.assertNotIn("extra_route_field", res)

    def test_notesync_status_forwards_every_field_the_cli_reads(self):
        payload = {"converter_id": "notesync-convert/1", "total": 3,
                   "counts": {"in_sync": 2, "conflict": 1},
                   "rows": [{"citation_key": "a2020", "state": "conflict",
                             "missing_words": 3, "extra_words": 2}],
                   "extra_route_field": "ignored"}
        res = self._call(driver.notesync_status, route_payload=payload)
        # cli/main.py::notesync_status_cmd reads: counts, total, rows[].state /
        # citation_key / missing_words / extra_words; converter_id is forwarded
        # for the MCP consumer.
        self.assertEqual(res["counts"], payload["counts"])
        self.assertEqual(res["total"], 3)
        self.assertEqual(res["rows"], payload["rows"])
        self.assertEqual(res["converter_id"], "notesync-convert/1")
        self.assertNotIn("extra_route_field", res)
        # cleanup/phase05.py reads `authoritative` to refuse per-note pushes.
        self.assertIs(res["authoritative"], False)
        res = self._call(driver.notesync_status,
                         route_payload={**payload, "authoritative": True})
        self.assertIs(res["authoritative"], True)

    def test_tagging_inventory_forwards_every_field_the_cli_reads(self):
        payload = {"rows": [{"citation_key": "a2020"}], "count": 1,
                   "pdfless_count": 3, "scanned_at": 1755500000.0,
                   "extra_route_field": "ignored"}
        res = self._call(driver.tagging_inventory, route_payload=payload)
        # cli/main.py::tagging_batch_cmd reads: rows, count, pdfless_count,
        # scanned_at (the snapshot-age disclosure), scope.
        self.assertEqual(res["rows"], payload["rows"])
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["pdfless_count"], 3)
        self.assertEqual(res["scanned_at"], 1755500000.0)
        self.assertEqual(res["scope"], "with_pdf")

    def test_tagging_inventory_scanned_at_defaults_to_zero_on_old_servers(self):
        res = self._call(driver.tagging_inventory,
                         route_payload={"rows": [], "count": 0})
        self.assertEqual(res["scanned_at"], 0)

    def test_tagging_propose_forwards_the_whole_route_payload_as_proposal(self):
        payload = {"proposed": [{"tag": "method", "confidence": 0.9}],
                   "status": "ok", "source": "pdf", "has_pdf": True,
                   "truncated": False, "provider": "openai", "model": "gpt-5"}
        res = self._call(driver.tagging_propose, "a2020",
                         route_payload=payload)
        # cli reads proposal.{proposed, status, source}; the driver forwards
        # the full payload, so new route fields can never be stripped here.
        self.assertEqual(res["proposal"], payload)

    def test_tagging_model_info_reads_config_only_and_resolves_the_override(self):
        """The batch's disclosure line must cost ONE /api/config read — never
        get_status(), whose /api/obsidian/status GET drains the one-shot vault
        status feed the GUI polls (2026-08-18 review)."""
        calls = []

        def _fake_http_json(endpoint_key, data=None, **k):
            calls.append(endpoint_key)
            return {"provider": "openai", "openai_model": "gpt-5",
                    "tagging_propose_model": "gpt-5-mini"}

        with patch("core.driver.resolve_base_url",
                   return_value="http://127.0.0.1:1"), \
                patch("core.driver._http_json", side_effect=_fake_http_json):
            res = driver.tagging_model_info()
        self.assertEqual(calls, ["config_get"])
        self.assertNotIn("obsidian_status", calls)
        self.assertEqual(res["provider"], "openai")
        self.assertEqual(res["model"], "gpt-5")
        self.assertEqual(res["tagging_model"], "gpt-5-mini")


if __name__ == "__main__":
    unittest.main()


class TestSseWallClock(unittest.TestCase):
    """TIMEOUT CHAIN (2026-09-02): `_http_sse`'s *timeout* is a wall clock.

    It used to be passed straight to urlopen, where it bounds each individual
    socket READ — while every call site (and the parameter's name) reads it as
    "this call takes at most N seconds". A server emitting a frame every few
    seconds therefore had no bound at all.
    """

    class _Resp:
        def __init__(self, lines):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            return iter(self._lines)

    def test_a_stream_that_outlives_its_budget_is_abandoned(self):
        """Driven by REAL elapsed time, so it fails against pre-change code on
        the assertion rather than on a patch target that is merely new."""
        from core import driver

        def _slow_frames():
            for i in range(100):
                time.sleep(0.01)         # a frame every 10 ms: never a stall
                yield b'data: {"info": "step %d"}\n' % i
            yield b'data: [DONE]\n'

        with patch("core.driver.urllib.request.urlopen",
                   return_value=self._Resp(_slow_frames())):
            out = driver._http_sse("deck_generate", {"topic": "x"},
                                   base_url="http://127.0.0.1:1", timeout=0.05)
        self.assertIn("error", out)
        self.assertIn("budget", out["error"])
        # The frames gathered before the cut are still returned — the shape
        # every caller already handles.
        self.assertLess(len(out["events"]), 100)
        self.assertGreater(len(out["events"]), 0)

    def test_a_stream_inside_its_budget_is_untouched(self):
        from core import driver
        frames = [b'data: {"token": "Hello"}\n',
                  b'data: {"deck": {"tex_path": "/x/y.tex"}}\n',
                  b'data: [DONE]\n']
        with patch("core.driver.urllib.request.urlopen",
                   return_value=self._Resp(frames)):
            out = driver._http_sse("deck_generate", {"topic": "x"},
                                   base_url="http://127.0.0.1:1", timeout=900.0)
        self.assertEqual(out["tokens"], "Hello")
        self.assertEqual(out["terminal"]["deck"]["tex_path"], "/x/y.tex")
        self.assertNotIn("error", out)


class TestAuditMappingDriver(unittest.TestCase):
    """`audit_mapping` / `audit_pdf_decision` (2026-09-06, Phase 2): the
    request bodies the route expects, argument validation BEFORE any request
    (the `_parse_targets` posture), and the response projection."""

    def _sent(self, fn, *args, reply=None, **kw):
        sent = {}

        def _fake(endpoint_key, data=None, **k):
            sent["endpoint"] = driver.ENDPOINTS[endpoint_key][0]
            sent["data"] = data
            return reply or {"ok": True, "action": "x"}

        with patch("core.driver.resolve_base_url", return_value="http://127.0.0.1:1"), \
                patch("core.driver._http_json", side_effect=_fake):
            res = fn(*args, **kw)
        return sent, res

    def test_bucket_verbs_send_the_list_form_with_confirm(self):
        sent, res = self._sent(driver.audit_mapping, "pending", ["a.pdf", " b.pdf "],
                               reply={"ok": True, "action": "pending",
                                      "pdfs": ["a.pdf", "b.pdf"], "changed": 2})
        self.assertEqual(sent["endpoint"], "/api/audit/mapping")
        self.assertEqual(sent["data"], {"confirm": True, "pdfs": ["a.pdf", "b.pdf"],
                                        "pending": True})
        self.assertEqual((res["status"], res["action"], res["changed"]), ("ok", "pending", 2))

    def test_link_and_detach_take_one_pdf_and_a_key(self):
        sent, _ = self._sent(driver.audit_mapping, "link", ["a.pdf"], "smith2020")
        self.assertEqual(sent["data"], {"confirm": True, "pdf": "a.pdf",
                                        "citation_key": "smith2020"})
        sent, _ = self._sent(driver.audit_mapping, "detach", ["a.pdf"], "smith2020")
        self.assertEqual(sent["data"], {"confirm": True, "pdf": "a.pdf",
                                        "detach_from": "smith2020"})

    def test_detach_can_ask_the_route_to_trash_the_zotero_links(self):
        sent, _ = self._sent(driver.audit_mapping, "detach", ["a.pdf"], "smith2020",
                             trash_zotero_links=True)
        self.assertEqual(sent["data"], {"confirm": True, "pdf": "a.pdf",
                                        "detach_from": "smith2020",
                                        "trash_zotero_links": True})
        with patch("core.driver._http_json") as http:
            self.assertIn("error", driver.audit_mapping("link", ["a.pdf"], "k",
                                                        trash_zotero_links=True))
        http.assert_not_called()

    def test_a_trash_failure_after_detach_is_not_projected_away(self):
        """The route answers 200 when the detach landed but the Zotero trash
        did not; a projection that strips ``zotero_error`` reports that as
        complete success (annual cleanup session 2)."""
        _, res = self._sent(driver.audit_mapping, "detach", ["a.pdf"], "k",
                            trash_zotero_links=True,
                            reply={"ok": True, "action": "detach", "changed": True,
                                   "trashed": [], "batch_id": None,
                                   "zotero_error": "could not trash"})
        self.assertEqual(res["zotero_error"], "could not trash")
        self.assertEqual(res["trashed"], [])

    def test_detach_all_takes_one_pdf_and_refuses_a_key(self):
        """It means "no key at all", so accepting one would read as its
        opposite — and the server would then apply the wildcard anyway."""
        sent, _ = self._sent(driver.audit_mapping, "detach_all", ["a.pdf"])
        self.assertEqual(sent["data"], {"confirm": True, "pdf": "a.pdf",
                                        "detach_all": True})
        with patch("core.driver._http_json") as http:
            self.assertIn("error", driver.audit_mapping("detach_all", ["a.pdf"], "k2020"))
            self.assertIn("error", driver.audit_mapping("detach_all", ["a.pdf", "b.pdf"]))
        http.assert_not_called()

    def test_argument_shapes_are_refused_before_any_request(self):
        with patch("core.driver._http_json") as http:
            self.assertIn("error", driver.audit_mapping("maybe", ["a.pdf"]))
            self.assertIn("error", driver.audit_mapping("pending", []))
            self.assertIn("error", driver.audit_mapping("link", ["a.pdf", "b.pdf"], "k"))
            self.assertIn("error", driver.audit_mapping("detach", ["a.pdf"]))
            self.assertIn("error", driver.audit_mapping("clear", ["a.pdf"], "k"))
            self.assertIn("error", driver.audit_pdf_decision("maybe", ["k"]))
            self.assertIn("error", driver.audit_pdf_decision("dismiss", []))
            self.assertIn("error", driver.audit_pdf_decision("clear_all", ["k"]))
        http.assert_not_called()

    def test_pdf_decision_bodies(self):
        sent, res = self._sent(driver.audit_pdf_decision, "dismiss", ["k1", "k2"],
                               reply={"ok": True, "dismissed": True, "changed": 2})
        self.assertEqual(sent["endpoint"], "/api/audit/pdf-decision")
        self.assertEqual(sent["data"], {"citation_keys": ["k1", "k2"], "dismiss": True})
        self.assertEqual(res, {"status": "ok", "verb": "dismiss", "changed": 2})
        sent, _ = self._sent(driver.audit_pdf_decision, "clear_all")
        self.assertEqual(sent["data"], {"clear_all": True})


class TestZotlinkAttachDriver(unittest.TestCase):
    """`zotlink_attach` / `zotlink_attach_candidates` (2026-09-06, Phase 4)."""

    def test_attach_sends_confirm_and_projects_the_terminal_frame(self):
        sent = {}

        def _fake_sse(endpoint_key, data, **k):
            sent["endpoint"] = driver.ENDPOINTS[endpoint_key][0]
            sent["data"] = data
            return {"tokens": "", "events": [], "error": None,
                    "terminal": {"zotlink_attach": {
                        "attached": 2, "batch_id": "b1", "results": {"a": {}},
                        "failures": [], "path_form": "absolute", "note": "n",
                        "dry_run": False, "cancelled": False}}}

        with patch("core.driver.resolve_base_url", return_value="http://127.0.0.1:1"), \
                patch("core.driver._http_sse", side_effect=_fake_sse):
            res = driver.zotlink_attach(["a", " b "], dry_run=True)
        self.assertEqual(sent["endpoint"], "/api/zotlink/attach")
        self.assertEqual(sent["data"], {"citation_keys": ["a", "b"], "confirm": True,
                                        "dry_run": True})
        self.assertEqual((res["status"], res["attached"], res["batch_id"],
                          res["path_form"], res["note"]), ("ok", 2, "b1", "absolute", "n"))

    def test_attach_reads_the_real_stream_terminal_frame(self):
        # Found live 2026-09-18: the test above mocks `_http_sse` and hands
        # back a `terminal` key the real consumer never set for this frame,
        # so every real attach — dry run AND write — was reported as "ended
        # without a terminal frame" after the server had answered.
        frame = {"zotlink_attach": {"ok": True, "dry_run": False, "attached": 1,
                                    "batch_id": "b1", "results": {"a": {"ok": True}},
                                    "failures": []}}
        lines = [f"data: {json.dumps(frame)}\n".encode(), b"data: [DONE]\n"]

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter(lines)

        with patch("core.driver.resolve_base_url", return_value="http://127.0.0.1:1"), \
                patch("core.driver.urllib.request.urlopen", return_value=_Resp()):
            res = driver.zotlink_attach(["a"])
        self.assertEqual((res.get("status"), res.get("attached"), res.get("batch_id")),
                         ("ok", 1, "b1"), res)

    def test_attach_refuses_an_empty_selection_before_any_request(self):
        with patch("core.driver._http_sse") as sse:
            self.assertIn("error", driver.zotlink_attach([]))
        sse.assert_not_called()

    def test_candidates_projection_keeps_every_cli_field(self):
        payload = {"rows": [{"citation_key": "a", "to_link": 1}], "count": 1,
                   "to_link": 1, "group_count": 0, "batch_max": 200,
                   "path_form": "absolute", "can_write": True, "transport": "web"}
        with patch("core.driver.resolve_base_url", return_value="http://127.0.0.1:1"), \
                patch("core.driver._http_json", return_value=payload):
            res = driver.zotlink_attach_candidates()
        for k in payload:
            self.assertEqual(res[k], payload[k])


class TestWebObservationTimeouts(unittest.TestCase):
    """A route that runs a COMPLETE Web observation must be waited for.

    `web_snapshot.observe` pages the whole library and its trash; on the
    maintainer's ~14,000-item library that is tens of minutes, and the server
    sends nothing until it has an answer, so the driver's timeout behaves as
    a wall clock. Measured 2026-09-17: `zotcreate preview` on the web
    transport timed out at the old 300 s and could never return.

    The asymmetry is why this is pinned rather than left to a literal.
    Waiting too long costs patience; giving up too early on the CREATE costs
    the PDF — the server finishes the observation and POSTs while the client
    is gone, the response is lost, and the attempt store holds a reservation
    it never saw acknowledged (`STAGE_PARENT_UNKNOWN`, no automatic exit).
    """

    def _timeout_for(self, call):
        seen = {}

        def _fake(endpoint_key, data=None, **kw):
            seen["key"] = endpoint_key
            seen["timeout"] = kw.get("timeout")
            return {"ok": True}

        with patch("core.driver.resolve_base_url", return_value="http://x"), \
                patch("core.driver._http_json", side_effect=_fake):
            call()
        return seen

    def test_preview_waits_for_a_complete_observation(self):
        seen = self._timeout_for(lambda: driver.zotcreate_preview("a/b.pdf"))
        self.assertEqual(seen["key"], "zotcreate_preview")
        self.assertGreaterEqual(seen["timeout"], driver.WEB_OBSERVATION_TIMEOUT_S)

    def test_settle_waits_for_a_complete_observation(self):
        seen = self._timeout_for(
            lambda: driver.zotcreate_settle_never_created("a/b.pdf", dry_run=True))
        self.assertGreaterEqual(seen["timeout"], driver.WEB_OBSERVATION_TIMEOUT_S)

    def test_a_real_create_waits_longer_than_one_observation(self):
        """It runs the observation again as `before_dispatch`, then POSTs."""
        seen = self._timeout_for(
            lambda: driver.zotcreate_create({"pdf": "a/b.pdf"}, dry_run=False))
        self.assertEqual(seen["key"], "zotcreate_create")
        self.assertGreater(seen["timeout"], driver.WEB_OBSERVATION_TIMEOUT_S)

    def test_a_dry_run_create_does_not_pay_for_one(self):
        """The route returns before any observation, so it stays quick."""
        seen = self._timeout_for(
            lambda: driver.zotcreate_create({"pdf": "a/b.pdf"}, dry_run=True))
        self.assertLess(seen["timeout"], driver.WEB_OBSERVATION_TIMEOUT_S)

    def test_the_budget_covers_a_library_of_this_size(self):
        """~14,000 items at 100 per page, items plus trash, paced — tens of
        minutes. Five is not a plausible bound for it."""
        self.assertGreaterEqual(driver.WEB_OBSERVATION_TIMEOUT_S, 1800.0)


class TestJsonResponseIntegrity(unittest.TestCase):
    """Exercise real driver projections above the HTTP response seam."""

    def test_unusable_write_responses_never_report_success_or_invite_retry(self):
        from io import BytesIO
        for body in (b'', b'not-json', b'null', b'[]', b'"ok"', b'42'):
            with self.subTest(body=body), \
                    patch('core.driver.resolve_base_url', return_value='http://127.0.0.1:1'), \
                    patch('core.driver.urllib.request.urlopen', return_value=BytesIO(body)):
                result = driver.audit_dup_decision('dismiss', ['fixture-pair'])
            self.assertIn('error', result)
            self.assertNotEqual(result.get('status'), 'ok')
            self.assertIn(driver._MUTATING_TIMEOUT_NOTE, result['error'])

    def test_unusable_read_responses_return_structured_errors(self):
        from io import BytesIO
        for body in (b'', b'null', b'[]', b'false', b'{'):
            with self.subTest(body=body), \
                    patch('core.driver.resolve_base_url', return_value='http://127.0.0.1:1'), \
                    patch('core.driver.urllib.request.urlopen', return_value=BytesIO(body)):
                result = driver.audit_status()
            self.assertIn('error', result)
            self.assertNotIn(driver._MUTATING_TIMEOUT_NOTE, result['error'])

    def test_valid_write_response_retains_recovery_fields(self):
        from io import BytesIO
        payload = {'changed': 1, 'operation_id': 'fixture-op', 'recovery': {'inspect': True}}
        with patch('core.driver.resolve_base_url', return_value='http://127.0.0.1:1'), \
                patch('core.driver.urllib.request.urlopen',
                      return_value=BytesIO(json.dumps(payload).encode())):
            result = driver.audit_dup_decision('dismiss', ['fixture-pair'])
        self.assertEqual(result, {'status': 'ok', **payload})
