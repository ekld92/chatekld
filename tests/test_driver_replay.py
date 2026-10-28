"""Replay harness: drive the REAL routes with the driver's OWN requests.

The body-key half of the contract ratchet (2026-07-22 audit T1/C4b). The v1
ratchet checked that routes exist; it could not see that ``generate_deck``
sent a body missing ``template_tex`` — so the deck/docgen generate tools
shipped 400-ing on every call and no test noticed. Here every driver HTTP call
is intercepted at ``urllib.request.urlopen`` and replayed against the Flask
test client (validation runs for real; the heavy SSE workers are stubbed to an
immediate ``[DONE]`` that still releases their op locks), with the rule:

* 405 / route-level 404 (non-JSON body) are ALWAYS failures;
* a 400 is a failure when its message says a field the driver should have sent
  is missing/required — unless the case's ``allowed_400`` regex explains it as
  environmental (no vault configured in the sandbox, etc.).

This file would have failed on the pre-2026-07-22 generate bodies
("A Beamer template is required.").
"""
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from flask import Response  # noqa: E402

_MISSING_FIELD_RE = re.compile(r"required|missing", re.IGNORECASE)


class _FakeHTTPResponse:
    """Duck-types what core.driver expects from urlopen's return value."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        # _http_sse iterates the response line by line.
        return iter(self._body.splitlines(keepends=True))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _endpoint_keys_for(path: str, method: str) -> set:
    """The ENDPOINTS registry rows a replayed request matches (path template
    ``<x>`` segments are wildcards; the query string is ignored)."""
    from core.driver import ENDPOINTS
    bare = path.split("?", 1)[0]
    keys = set()
    for key, (template, verb) in ENDPOINTS.items():
        if verb != method:
            continue
        pattern = "^" + re.sub(r"<[^>]+>", "[^/]+", re.escape(template).replace(r"\<", "<").replace(r"\>", ">")) + "$"
        if re.match(pattern, bare):
            keys.add(key)
    return keys


class TestDriverReplay(unittest.TestCase):
    #: Which replay case proves each ENDPOINTS row (2026-09-17 review, E1).
    #: ``test_every_endpoint_row_has_a_replay_case`` fails on a row missing
    #: here, and ``tearDown`` fails a case that does not actually send a
    #: request matching every row it claims — so the table cannot lie.
    CLAIMS: dict = {
        "online_runs": "test_online_history_rows_reach_live_routes",
        "online_run": "test_online_history_rows_reach_live_routes",
        "audit_inventory": "test_audit_read_rows_reach_live_routes",
        "audit_mapping": "test_audit_mapping_and_pdf_decision_rows_reach_live_routes",
        "audit_pdf_decision": "test_audit_mapping_and_pdf_decision_rows_reach_live_routes",
        "audit_report": "test_audit_read_rows_reach_live_routes",
        "audit_scan": "test_audit_scan_body_accepted",
        "audit_summary": "test_audit_read_rows_reach_live_routes",
        "config_get": "test_get_status_composes_from_real_routes",
        "config_set": "test_set_config_body_accepted",
        "deck_generate": "test_generate_deck_sends_template_content",
        "deck_load_template": "test_generate_deck_sends_template_content",
        "docgen_generate": "test_generate_doc_latex_sends_template_content",
        "docgen_generate_md": "test_generate_doc_markdown_body_accepted",
        "docgen_load_template": "test_generate_doc_latex_sends_template_content",
        "health": "test_health_probe_reaches_the_route",
        "library_bibliography_apply": "test_library_apply_replays_through_closed_production_gate",
        "library_bibliography_authenticate": "test_library_authenticate_driver_reaches_route",
        "library_bibliography_decisions": "test_library_bibliography_decisions_body_accepted",
        "library_bibliography_plan": "test_library_acquisition_driver_replays_explicit_payload",
        "library_checks": "test_library_links_driver_reaches_shared_observational_service",
        "notesync_campaign_apply": "test_notesync_campaign_apply_replays_through_closed_production_gate",
        "notesync_campaign_inspect": "test_notesync_campaign_read_and_plan_rows_reach_live_routes",
        "notesync_campaign_plan": "test_notesync_campaign_read_and_plan_rows_reach_live_routes",
        "notesync_campaign_recover": "test_notesync_recovery_replays_through_closed_production_gate",
        "notesync_campaign_review": "test_notesync_campaign_read_and_plan_rows_reach_live_routes",
        "notesync_campaign_settle": "test_notesync_settlement_replays_through_closed_production_gate",
        "notesync_campaign_undo": "test_notesync_undo_replays_through_closed_production_gate",
        "notesync_sync_status": "test_notesync_status_row_reaches_the_live_route",
        "obsidian_chat": "test_vault_ask_body_accepted",
        "obsidian_index": "test_vault_index_body_accepted",
        "obsidian_status": "test_get_status_composes_from_real_routes",
        "status": "test_get_status_composes_from_real_routes",
        "summarise": "test_summarise_request_reaches_the_route",
        "tagging_apply": "test_tagging_propose_and_apply_bodies_accepted",
        "tagging_inventory": "test_tagging_inventory_query_string_reaches_the_route",
        "tagging_journal_stats": "test_tagging_journal_stats_route_answers",
        "tagging_pdf_links": "test_tagging_pdf_links_row_reaches_the_live_route",
        "tagging_propose": "test_tagging_propose_and_apply_bodies_accepted",
        "tagging_restore": "test_tagging_restore_body_accepted",
        "upload": "test_summarize_paper_upload_request_shape",
        "zotlink_attach": "test_zotlink_attach_rows_reach_live_routes",
        "zotlink_attach_candidates": "test_zotlink_attach_rows_reach_live_routes",
        "zotlink_repoint": "test_zotlink_repoint_rows_reach_live_routes",
        "zotlink_repoint_candidates": "test_zotlink_repoint_rows_reach_live_routes",
        # Annual library cleanup T1 (2026-09-17).
        "audit_status": "test_cleanup_audit_rows_reach_live_routes",
        "audit_dup_decision": "test_cleanup_audit_rows_reach_live_routes",
        "audit_pdf_facts": "test_cleanup_audit_rows_reach_live_routes",
        "pdfs_entry": "test_cleanup_pdfs_rows_reach_live_routes",
        "pdfs_candidates": "test_cleanup_pdfs_rows_reach_live_routes",
        "pdfs_preview": "test_cleanup_pdfs_rows_reach_live_routes",
        "pdfs_apply": "test_cleanup_pdfs_rows_reach_live_routes",
        "pdfs_restore": "test_cleanup_pdfs_rows_reach_live_routes",
        "pdfs_inspect": "test_cleanup_pdfs_rows_reach_live_routes",
        "pdfs_complete": "test_cleanup_pdfs_rows_reach_live_routes",
        "pdfs_holding": "test_cleanup_pdfs_rows_reach_live_routes",
        "accession_rows": "test_cleanup_accession_rows_reach_live_routes",
        "accession_identify": "test_cleanup_accession_rows_reach_live_routes",
        "accession_batch": "test_cleanup_accession_rows_reach_live_routes",
        "notesync_legacy_status": "test_cleanup_notesync_rows_reach_live_routes",
        "notesync_preview": "test_cleanup_notesync_rows_reach_live_routes",
        "notesync_create": "test_cleanup_notesync_rows_reach_live_routes",
        "notesync_update_preview": "test_cleanup_notesync_rows_reach_live_routes",
        "notesync_update": "test_cleanup_notesync_rows_reach_live_routes",
        "notesync_migrate": "test_cleanup_notesync_rows_reach_live_routes",
        "notesync_unmigrate": "test_cleanup_notesync_rows_reach_live_routes",
        "notesync_bootstrap": "test_cleanup_notesync_sync_rows_reach_live_routes",
        "notesync_pull": "test_cleanup_notesync_sync_rows_reach_live_routes",
        "notesync_refresh_all": "test_cleanup_notesync_sync_rows_reach_live_routes",
        "notesync_mirror": "test_cleanup_notesync_sync_rows_reach_live_routes",
        "notesync_push": "test_cleanup_notesync_sync_rows_reach_live_routes",
        "notesync_merge_preview": "test_cleanup_notesync_sync_rows_reach_live_routes",
        "notesync_merge": "test_cleanup_notesync_sync_rows_reach_live_routes",
        "notesync_create_attempts": "test_cleanup_notesync_sync_rows_reach_live_routes",
        "notesync_key_changes": "test_cleanup_notesync_sync_rows_reach_live_routes",
        "notesync_rename_note": "test_cleanup_notesync_sync_rows_reach_live_routes",
        "notesync_inspect_create_attempt": "test_cleanup_notesync_sync_rows_reach_live_routes",
        "notesync_create_zotero_note": "test_cleanup_notesync_sync_rows_reach_live_routes",
        "zotcreate_preview": "test_cleanup_zotcreate_and_zotlink_rows_reach_live_routes",
        "zotcreate_create": "test_cleanup_zotcreate_and_zotlink_rows_reach_live_routes",
        "zotcreate_batch": "test_cleanup_zotcreate_and_zotlink_rows_reach_live_routes",
        "zotcreate_attempts": "test_cleanup_zotcreate_and_zotlink_rows_reach_live_routes",
        "zotcreate_settle_never_created": "test_cleanup_zotcreate_and_zotlink_rows_reach_live_routes",
        "zotcreate_reinstate_attempt": "test_cleanup_zotcreate_and_zotlink_rows_reach_live_routes",
        "zotlink_status": "test_cleanup_zotcreate_and_zotlink_rows_reach_live_routes",
        "zotlink_trash": "test_cleanup_zotcreate_and_zotlink_rows_reach_live_routes",
        "zotlink_retitle": "test_cleanup_zotcreate_and_zotlink_rows_reach_live_routes",
        "tagging_pdf_links_status": "test_cleanup_zotcreate_and_zotlink_rows_reach_live_routes",
        "zotrecord_retitle": "test_cleanup_zotrecord_rows_reach_live_routes",
        "zotrecord_merge_preview": "test_cleanup_zotrecord_rows_reach_live_routes",
        "zotrecord_merge": "test_cleanup_zotrecord_rows_reach_live_routes",
        "zotrecord_trash": "test_cleanup_zotrecord_rows_reach_live_routes",
        # Library-workflow run (lots 3 and 5, 2026-09-21).
        "library_session_create": "test_library_run_rows_reach_live_routes",
        "library_session_get": "test_library_run_rows_reach_live_routes",
        "library_session_llm": "test_library_run_rows_reach_live_routes",
        "library_observe": "test_library_run_rows_reach_live_routes",
        "library_steps": "test_library_run_rows_reach_live_routes",
        "library_analyze": "test_library_run_rows_reach_live_routes",
        "library_apply": "test_library_run_rows_reach_live_routes",
    }

    @classmethod
    def setUpClass(cls):
        from app import create_app
        cls.app = create_app()
        cls.app.config["TESTING"] = True

    def setUp(self):
        self._hit: set = set()

    def tearDown(self):
        claimed = {k for k, test in self.CLAIMS.items() if test == self._testMethodName}
        missing = claimed - self._hit
        self.assertFalse(missing, f"{self._testMethodName} claims {sorted(missing)} but sent no such request")

    def _stub_sse(self):
        """Patch every route module's run_sse_worker to an instant [DONE].

        The stub still calls ``release=`` so op locks don't leak across cases.
        Validation in each route runs BEFORE run_sse_worker, which is exactly
        the part this harness exists to exercise.
        """
        def _stub(worker, *, consumer_timeout_s=None, preflight_msgs=None,
                  release=None, **kw):
            if release is not None:
                try:
                    release()
                except Exception:
                    pass
            return Response("data: [DONE]\n\n", mimetype="text/event-stream")

        patches = []
        for mod in ("api.routes.vault", "api.routes.deck", "api.routes.docgen",
                    "api.routes.paper",
                    "api.routes.plainchat", "api.routes.accession",
                    "api.routes.zotlink", "api.routes.library_workflow"):
            patches.append(patch(f"{mod}.run_sse_worker", side_effect=_stub))
        # zotcreate imports it at call time (its batch is SSE since T2).
        patches.append(patch("api.sse.run_sse_worker", side_effect=_stub))
        return patches

    def _run(self, fn, allowed_400: str = "", expect_min_requests: int = 1):
        """Invoke *fn* with urlopen replayed through the test client."""
        client = self.app.test_client()
        seen: list = []

        def _fake_urlopen(req, timeout=None):
            url = req.full_url
            path = url.split("127.0.0.1:1", 1)[-1] if "127.0.0.1:1" in url else url
            # Strip scheme/host generically.
            m = re.search(r"(/api/.*)$", url)
            path = m.group(1) if m else path
            method = req.get_method()
            headers = {k: v for k, v in req.header_items()}
            resp = client.open(path, method=method, data=req.data,
                               headers=headers)
            seen.append((path, method, resp))
            self._hit |= _endpoint_keys_for(path, method)
            status = resp.status_code
            body = resp.get_data()
            if status >= 400:
                raise urllib.error.HTTPError(url, status, resp.status, resp.headers,
                                             io.BytesIO(body))
            return _FakeHTTPResponse(status, body)

        patches = self._stub_sse()

        network: list = []

        def _no_network(req, *_a, **_k):
            network.append(getattr(req, "full_url", req))
            # Session 10 (2026-09-18): the zotcreate batch replay ran a REAL
            # five-minute Web API observation of the maintainer's library
            # because its SSE worker was not stubbed. A replay exercises the
            # route's validation; it must never reach Zotero.
            raise AssertionError("a driver replay reached Zotero over the network")
        patches.append(patch("tagging.zotero_write._urlopen", side_effect=_no_network))
        with patch("core.driver.urllib.request.urlopen", side_effect=_fake_urlopen), \
                patch("core.driver.resolve_base_url",
                      return_value="http://127.0.0.1:1"):
            for p in patches:
                p.start()
            try:
                result = fn()
            finally:
                for p in patches:
                    p.stop()

        # Raised in a worker thread the error would only become an SSE frame,
        # so the hit is recorded and asserted here, on the test's thread.
        self.assertEqual(network, [], "a driver replay reached Zotero over the network")
        self.assertGreaterEqual(len(seen), expect_min_requests,
                                f"driver made no HTTP request: {result}")
        for path, method, resp in seen:
            self.assertNotEqual(resp.status_code, 405,
                                f"{method} {path}: method not allowed")
            body = resp.get_data(as_text=True)
            if resp.status_code == 404:
                # A domain 404 (JSON error body) is legitimate ("run a scan
                # first"); a routing 404 (Flask HTML) means a phantom endpoint.
                self.assertTrue(body.strip().startswith("{"),
                                f"{method} {path}: routing 404 (phantom endpoint)")
            if resp.status_code == 400:
                try:
                    err = (json.loads(body) or {}).get("error", "")
                except ValueError:
                    err = body
                if allowed_400 and re.search(allowed_400, err, re.IGNORECASE):
                    continue
                self.assertFalse(
                    _MISSING_FIELD_RE.search(err or ""),
                    f"{method} {path}: 400 for a missing field the driver "
                    f"should send — {err!r}")
        return result, seen

    # ------------------------------------------------------------------

    def test_get_status_composes_from_real_routes(self):
        from core.driver import get_status
        result, seen = self._run(get_status, expect_min_requests=3)
        self.assertTrue(result.get("server_running"))
        self.assertIn("config", result)
        self.assertIn("vault_index", result)
        # The rich payload shape, not a bare string.
        self.assertIsInstance(result["vault_index"], dict)

    def test_set_config_body_accepted(self):
        from core.driver import set_config
        result, _ = self._run(lambda: set_config({"chat_temperature": 0.4}))
        self.assertEqual(result.get("status"), "ok")

    def test_vault_ask_body_accepted(self):
        from core.driver import vault_ask
        # The stubbed SSE returns no tokens; the point is the request validates.
        self._run(lambda: vault_ask("what is depression?"))

    def test_vault_index_body_accepted(self):
        from core.driver import vault_index
        # Sandbox has no vault configured — an environmental 400 naming the
        # vault is fine; a missing-field 400 is not.
        self._run(vault_index, allowed_400=r"vault|folder|path")

    def test_tagging_propose_and_apply_bodies_accepted(self):
        from core.driver import tagging_apply, tagging_propose
        # No audit scan in the sandbox → domain 404/409 allowed by the rules.
        self._run(lambda: tagging_propose("smith2020"),
                  allowed_400=r"scan|vault|inventory")
        self._run(lambda: tagging_apply("smith2020", ["dep"], targets=["note"]),
                  allowed_400=r"scan|vault|inventory")

    def test_notesync_status_row_reaches_the_live_route(self):
        """Note sync (2026-09-04): one GET row. No scan in the sandbox → the
        route's own 404 ("Run a Library Audit scan first") is the domain
        answer, and a routing 404 would read differently."""
        from core.driver import notesync_status
        _res, seen = self._run(notesync_status, allowed_400=r"scan|vault|inventory")
        paths = [p for p, _m, _r in seen]
        self.assertTrue(any(p.startswith("/api/notesync/sync-status") for p in paths), paths)

    def test_notesync_recovery_replays_through_closed_production_gate(self):
        from core.driver import notesync_campaign
        result, seen = self._run(lambda: notesync_campaign('recover', identifier='fixture',
            data={'confirm': True, 'citation_key': 'one', 'result': 'exact',
                  'evidence': 'current', 'outcome_evidence': 'retained'}))
        self.assertTrue(any(p.endswith('/fixture/recover') and r.status_code == 501 for p, _m, r in seen))

    def test_notesync_settlement_replays_through_closed_production_gate(self):
        from core.driver import notesync_campaign
        result, seen = self._run(lambda: notesync_campaign('settle', identifier='fixture',
            data={'confirm': True, 'citation_key': 'one', 'result': 'exact',
                  'evidence': 'current', 'outcome_evidence': 'retained'}))
        self.assertTrue(any(p.endswith('/fixture/settle') and r.status_code == 501 for p, _m, r in seen))

    def test_notesync_undo_replays_through_closed_production_gate(self):
        from core.driver import notesync_campaign
        result, seen = self._run(lambda: notesync_campaign('undo', identifier='fixture',
            data={'confirm': True, 'citation_key': 'one', 'outcome_evidence': 'retained'}))
        self.assertTrue(any(p.endswith('/fixture/undo') and r.status_code == 501 for p, _m, r in seen))

    def test_tagging_inventory_query_string_reaches_the_route(self):
        """The first driver call carrying a query string.

        `?scope=` rides the URL, not the JSON body, so the registry stores the
        bare path and only a replay can prove the composed URL still routes —
        a typo'd query separator would produce a routing 404 that the contract
        ratchet (which only compares registry paths to the url_map) cannot see.
        """
        from core.driver import tagging_inventory
        _res, seen = self._run(lambda: tagging_inventory(scope="all"),
                               allowed_400=r"scan|vault|inventory")
        paths = [p for p, _m, _r in seen]
        self.assertTrue(any("scope=all" in p for p in paths),
                        f"scope did not reach the request URL: {paths}")

    def test_library_acquisition_driver_replays_explicit_payload(self):
        from core import driver
        from accession.sources import Candidate
        data = dict(provider='ollama', model='fixture', max_tokens=128, resolve_dois=True,
                    corpus=[dict(id='r', doi='10.1234/fixture', current={'title': 'old'}, sources=[])])
        with tempfile.TemporaryDirectory() as directory, patch('core.config.load_config', return_value={'obsidian_vault_path': directory}), patch('accession.catalogue_evidence.CrossrefSource.run', return_value=[Candidate(source='crossref', title='Fixture', doi='10.1234/fixture')]) as source, patch('core.llm.chat.stream_chat_messages', return_value=iter(['[]'])):
            self._run(lambda: driver.library_campaign('bibliography', data))
        source.assert_called_once_with('doi', '10.1234/fixture', rows=1, timeout_s=5.0)

    def test_library_apply_replays_through_closed_production_gate(self):
        from core import driver
        _res, seen = self._run(lambda: driver.library_campaign('apply', {'confirm': True, 'decisions': [
            {'record_id': 'r', 'field': 'title', 'evidence': 'e'}]}, 'fixture'))
        self.assertTrue(any(p.endswith('/fixture/apply') and r.status_code == 501 for p, _m, r in seen))

    def test_library_authenticate_driver_reaches_route(self):
        from core import driver
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            (vault / 'biblio_articles').mkdir()
            cfg = {'obsidian_vault_path': str(vault), 'audit_attachments_subdir': '.'}
            with patch('core.config.load_config', return_value=cfg):
                res, seen = self._run(lambda: driver.library_campaign('authenticate', {'records': [
                    {'id': 'r', 'pdf': 'missing.pdf', 'current': {'title': 'x'}}]}))
        self.assertTrue(any(p.endswith('/bibliography/authenticate') and r.status_code == 200 for p, _m, r in seen))
        self.assertEqual(res['reports']['r']['status'], 'unavailable')

    def test_library_storage_driver_reaches_read_only_control(self):
        from core import driver
        from types import SimpleNamespace
        control = {'results': [], 'findings': [], 'counts': {}, 'writes': False}
        with patch('api.routes.audit.audit_manager.get_inventory', return_value=(SimpleNamespace(records={}), SimpleNamespace())), \
                patch('accession.reconciliation.saved', return_value={}), \
                patch('accession.storage_check.check', return_value=control) as check:
            res, seen = self._run(lambda: driver.library_campaign('storage'))
        check.assert_called_once()
        self.assertTrue(any(p.endswith('/library/storage') and r.status_code == 200 for p, _m, r in seen))
        self.assertFalse(res['writes'])

    def test_library_links_driver_reaches_shared_observational_service(self):
        from core import driver
        from types import SimpleNamespace
        fixture = {'notes': [], 'writes': False, 'known_notes': 0}
        with patch('api.routes.audit.audit_manager.get_inventory', return_value=(SimpleNamespace(records={}), SimpleNamespace())), patch('accession.library.documents', return_value={'documents': []}), patch('accession.dependent_links.inspect', return_value=fixture) as inspect:
            self._run(lambda: driver.library_campaign('links'))
        inspect.assert_called_once()

    def test_audit_read_rows_reach_live_routes(self):
        """Three registry rows added in one batch (Batch G, 2026-08-26). The
        contract ratchet only compares paths to the url_map, so a row can be
        wrong about its METHOD or its composed URL and still pass — only a
        replay proves the request routes. `audit_report` is the first row with
        a Flask converter segment, so its substitution is the part that could
        silently 404."""
        from core.driver import audit_inventory, audit_report, audit_summary
        self._run(lambda: audit_summary(), allowed_400=r"scan|vault|audit")
        self._run(lambda: audit_inventory(), allowed_400=r"scan|vault|audit")
        _res, seen = self._run(lambda: audit_report("tag_hygiene"),
                               allowed_400=r"scan|vault|audit|report")
        paths = [p for p, _m, _r in seen]
        self.assertTrue(any(p.endswith("/api/audit/reports/tag_hygiene")
                            for p in paths),
                        f"converter segment not substituted: {paths}")

    def test_audit_report_refuses_an_unknown_name_before_any_request(self):
        """The report name is a URL path segment; the route's own allowlist
        must not be the only thing between a tool argument and the url_map."""
        from core.driver import audit_report
        with patch("core.driver._http_json") as http:
            res = audit_report("../../etc/passwd")
        self.assertIn("error", res)
        http.assert_not_called()

    def test_audit_mapping_and_pdf_decision_rows_reach_live_routes(self):
        """The two write rows added for the harmonization plan (2026-09-06).
        Replayed through the real routes: a 400 here may only be the
        sandbox's missing vault/config, never a missing body field."""
        from core.driver import audit_mapping, audit_pdf_decision
        self._run(lambda: audit_mapping("pending", ["att/a.pdf"]),
                  allowed_400=r"vault|config|audit|not found|scope|readable")
        self._run(lambda: audit_mapping("detach", ["att/a.pdf"], "k2020"),
                  allowed_400=r"vault|config|audit|not found|scope|readable")
        self._run(lambda: audit_mapping("detach_all", ["att/a.pdf"]),
                  allowed_400=r"vault|config|audit|not found|scope|readable")
        self._run(lambda: audit_mapping("clear", ["att/a.pdf", "att/b.pdf"]),
                  allowed_400=r"vault|config|audit")
        self._run(lambda: audit_pdf_decision("dismiss", ["k2020"]),
                  allowed_400=r"vault|config|audit")
        self._run(lambda: audit_pdf_decision("clear_all"),
                  allowed_400=r"vault|config|audit")

    def test_tagging_pdf_links_row_reaches_the_live_route(self):
        from core.driver import tagging_pdf_links
        self._run(lambda: tagging_pdf_links(["k2020"]),
                  allowed_400=r"vault|config|audit|scan|journal|list")

    def test_zotlink_repoint_rows_reach_live_routes(self):
        """The broken-link repair pair (2026-09-07). A 400/404 here may only be
        the sandbox's missing scan, never a missing body field or query key."""
        from core.driver import zotlink_repoint, zotlink_repoint_candidates
        self._run(lambda: zotlink_repoint_candidates("ATT1", "smith"),
                  allowed_400=r"vault|config|audit|scan|attachment|linked")
        self._run(lambda: zotlink_repoint("ATT1", "smith_2020.pdf"),
                  allowed_400=r"vault|config|audit|scan|attachment|linked|required")

    def test_zotlink_attach_rows_reach_live_routes(self):
        """Phase 4 (2026-09-06): the candidates GET and the attach POST."""
        from core.driver import zotlink_attach, zotlink_attach_candidates
        self._run(lambda: zotlink_attach_candidates(),
                  allowed_400=r"scan|vault|audit")
        self._run(lambda: zotlink_attach(["smith2020"], dry_run=True),
                  allowed_400=r"scan|vault|audit|record|Zotero")

    # -- Annual library cleanup T1 (2026-09-17) ---------------------------
    # The sandbox has no scan and no vault, so most of these answer 400/404/
    # 403/409 domain refusals. What each case proves is that the driver's
    # request ROUTES (no 405, no routing 404) and that no refusal names a field
    # the driver should have sent.

    #: Deliberately EMPTY: every sandbox refusal these routes give ("Run a
    #: scan first", "Choose an entry from the current inventory") names no
    #: field, so any 400 mentioning a required/missing field is a driver fault.
    _ENV = ""

    def test_cleanup_audit_rows_reach_live_routes(self):
        from core.driver import audit_dup_decision, audit_status
        res, _ = self._run(lambda: audit_status())
        self.assertIn("state", res)
        with tempfile.TemporaryDirectory() as tmp, \
                patch("audit.engine.dup_decisions.decisions_path",
                      return_value=__import__("pathlib").Path(tmp) / "d.json"):
            res, _ = self._run(lambda: audit_dup_decision("dismiss", ["a", "b"]))
            self.assertEqual(res.get("changed"), 2, res)
            self._run(lambda: audit_dup_decision("clear", ["a"]))
        from core.driver import audit_pdf_facts
        # No vault in the sandbox: the route answers 400 on settings or 200
        # with a per-row error; either way the body shape was accepted.
        self._run(lambda: audit_pdf_facts(["a/b.pdf"]), allowed_400=self._ENV)

    def test_cleanup_pdfs_rows_reach_live_routes(self):
        from core import driver
        env = self._ENV
        self._run(lambda: driver.pdfs_entry("k2020"), allowed_400=env)
        self._run(lambda: driver.pdfs_candidates("smith"), allowed_400=env)
        self._run(lambda: driver.pdfs_preview("k2020", "link", pdf="a/b.pdf"), allowed_400=env)
        self._run(lambda: driver.pdfs_apply("k2020", "cleanup", "tok", survivor="a.pdf",
                                            actions={"b.pdf": "delete"}), allowed_400=env)
        self._run(lambda: driver.pdfs_restore("op1"), allowed_400=env)
        self._run(lambda: driver.pdfs_inspect("op1"), allowed_400=env)
        self._run(lambda: driver.pdfs_complete("op1", "tok"), allowed_400=env)
        self._run(lambda: driver.pdfs_holding(), allowed_400=env)
        # T6: decision_ref rides a cleanup body without tripping the route.
        self._run(lambda: driver.pdfs_apply("k2020", "cleanup", "tok", survivor="a.pdf",
                                            actions={"b.pdf": "delete"}, decision_ref="row-1"),
                  allowed_400=env)

    def test_cleanup_accession_rows_reach_live_routes(self):
        from core import driver
        env = self._ENV
        self._run(lambda: driver.accession_rows(), allowed_400=env)
        # The query string is invisible to the contract ratchet (the registry
        # stores the bare path), so every scope is replayed against the real
        # route: an unknown value there answers 400, and a `pending` the route
        # did not accept would silently become the orphan set.
        for scope in driver.ACCESSION_ROW_SCOPES:
            self._run(lambda s=scope: driver.accession_rows(s), allowed_400=env)
        self._run(lambda: driver.accession_identify("a/b.pdf"), allowed_400=env)
        self._run(lambda: driver.accession_batch(["a/b.pdf"], force=True), allowed_400=env)

    def test_cleanup_notesync_rows_reach_live_routes(self):
        from core import driver
        env = self._ENV
        self._run(lambda: driver.notesync_legacy_status(), allowed_400=env)
        self._run(lambda: driver.notesync_preview("k2020"), allowed_400=env)
        self._run(lambda: driver.notesync_create(["k2020"]), allowed_400=env)
        self._run(lambda: driver.notesync_update_preview("k2020"), allowed_400=env)
        self._run(lambda: driver.notesync_update("k2020", "a" * 64, "b" * 64), allowed_400=env)
        self._run(lambda: driver.notesync_layout("migrate", dry_run=True), allowed_400=env)
        self._run(lambda: driver.notesync_layout("unmigrate", dry_run=True), allowed_400=env)

    def test_cleanup_notesync_sync_rows_reach_live_routes(self):
        from core import driver
        env = self._ENV
        self._run(lambda: driver.notesync_bootstrap(["k2020"]), allowed_400=env)
        self._run(lambda: driver.notesync_pull(["k2020"]), allowed_400=env)
        self._run(lambda: driver.notesync_refresh_all(dry_run=True), allowed_400=env)
        self._run(lambda: driver.notesync_mirror(dry_run=True), allowed_400=env)
        self._run(lambda: driver.notesync_push(["k2020"], accept_conflict=True), allowed_400=env)
        self._run(lambda: driver.notesync_merge_preview("k2020"), allowed_400=env)
        self._run(lambda: driver.notesync_merge("k2020", "text", "a" * 64, "ev"), allowed_400=env)
        self._run(lambda: driver.notesync_create_attempts(), allowed_400=env)
        self._run(lambda: driver.notesync_key_changes(), allowed_400=env)
        self._run(lambda: driver.notesync_rename_note("k2020", dry_run=True), allowed_400=env)
        self._run(lambda: driver.notesync_inspect_create_attempt("k2020"), allowed_400=env)
        self._run(lambda: driver.notesync_create_zotero_note("k2020"), allowed_400=env)

    def test_a_replay_that_reaches_zotero_fails_even_from_a_worker_thread(self):
        import threading
        import urllib.request as ur

        from core import driver
        from tagging import zotero_write as zw

        def _touch_zotero():
            def _call():
                try:
                    zw._urlopen(ur.Request("https://api.zotero.org/users/1/items"), 1)
                except Exception:
                    pass  # swallowed, as an SSE worker would turn it into a frame
            t = threading.Thread(target=_call)
            t.start()
            t.join()
            return driver.zotlink_status()

        with self.assertRaisesRegex(AssertionError, "reached Zotero"):
            self._run(_touch_zotero, allowed_400=self._ENV)

    def test_cleanup_zotcreate_and_zotlink_rows_reach_live_routes(self):
        from core import driver
        env = self._ENV
        self._run(lambda: driver.zotcreate_preview("a/b.pdf"), allowed_400=env)
        self._run(lambda: driver.zotcreate_create(
            {"pdf": "a/b.pdf", "item_type": "report", "fields": {"title": "T"},
             "creators": []}, dry_run=True), allowed_400=env)
        self._run(lambda: driver.zotcreate_batch(
            [{"id": "r1", "pdf": "a/b.pdf", "item_type": "report", "fields": {"title": "T"},
              "creators": [], "decision_ref": "02-orphans.jsonl#r1"}], dry_run=True),
            allowed_400=env)
        self._run(lambda: driver.zotcreate_attempts(), allowed_400=env)
        self._run(lambda: driver.zotcreate_settle_never_created("a/b.pdf"), allowed_400=env)
        self._run(lambda: driver.zotcreate_reinstate_attempt(1), allowed_400=env)
        self._run(lambda: driver.zotlink_status(), allowed_400=env)
        self._run(lambda: driver.zotlink_trash(["ABCD1234"]), allowed_400=env)
        self._run(lambda: driver.zotlink_retitle(["ABCD1234"], dry_run=True),
                  allowed_400=env)
        self._run(lambda: driver.tagging_pdf_links_status("absent"), allowed_400=env)

    def test_library_run_rows_reach_live_routes(self):
        """Each row with the body the driver builds. Without a scan and a
        configured vault the routes answer a domain 400/404; the point is
        that the request reached a registered route with no missing field."""
        from core import driver
        env = r"vault|config|session|Unknown|scan|obsidian"
        self._run(lambda: driver.library_session_create(
            llm={"provider": "ollama", "model": "llama3"}, label="t"), allowed_400=env)
        self._run(lambda: driver.library_session("0" * 32), allowed_400=env)
        self._run(lambda: driver.library_session_llm("0" * 32, {"provider": "ollama",
                                                               "model": "llama3"}),
                  allowed_400=env)
        self._run(lambda: driver.library_observe("0" * 32), allowed_400=env)
        self._run(lambda: driver.library_record_step("0" * 32, "attachment:1:A", "tag", "done",
                                                      confirm=True),
                  allowed_400=env)
        self._run(lambda: driver.library_analyze("0" * 32, propose_tags=True), allowed_400=env)
        self._run(lambda: driver.library_apply("0" * 32, {"renames": []}, confirm=True),
                  allowed_400=env)

    def test_cleanup_zotrecord_rows_reach_live_routes(self):
        """Session 3 (T8/T9): the retitle and merge verbs, dry-run and
        preview forms, must land on registered routes with the bodies the
        driver builds (a scan-less 404 is the domain answer here)."""
        from core import driver
        env = self._ENV
        self._run(lambda: driver.zotrecord_retitle("k", "A chapter title", dry_run=True), allowed_400=env)
        self._run(lambda: driver.zotrecord_merge_preview("k1", "k2"), allowed_400=env)
        self._run(lambda: driver.zotrecord_merge("k1", "k2", dry_run=True), allowed_400=env)
        self._run(lambda: driver.zotrecord_trash("k1", dry_run=True), allowed_400=env)

    def test_tagging_journal_stats_route_answers(self):
        """A registry row with no replay case passes the contract ratchet
        silently — the ratchet only compares paths to the url_map. This proves
        the composed request actually reaches a live route."""
        from core.driver import tagging_journal_stats
        self._run(lambda: tagging_journal_stats(),
                  allowed_400=r"vault|archive|journal")


    def test_generate_deck_sends_template_content(self):
        """THE regression this harness exists for: the old driver body had no
        template_tex/out_dir and 400'd on every call, forever."""
        from core.driver import generate_deck
        with tempfile.TemporaryDirectory() as td:
            tpl = os.path.join(td, "template.tex")
            with open(tpl, "w", encoding="utf-8") as fh:
                fh.write("\\documentclass{beamer}\n\\begin{document}\n"
                         "\\end{document}\n")
            out_dir = os.path.join(td, "out")
            os.makedirs(out_dir)
            result, seen = self._run(
                lambda: generate_deck("Test topic", template_path=tpl,
                                      out_dir=out_dir),
                expect_min_requests=2)
        paths = [p for p, _m, _r in seen]
        self.assertIn("/api/deck/load-template", paths)
        self.assertIn("/api/deck/generate", paths)
        # Both requests validated (no missing-field 400 per the harness rules).
        self.assertNotIn("error", result)

    def test_generate_doc_latex_sends_template_content(self):
        from core.driver import generate_doc
        with tempfile.TemporaryDirectory() as td:
            tpl = os.path.join(td, "article.tex")
            with open(tpl, "w", encoding="utf-8") as fh:
                fh.write("\\documentclass{article}\n\\begin{document}\n"
                         "\\end{document}\n")
            out_dir = os.path.join(td, "out")
            os.makedirs(out_dir)
            result, seen = self._run(
                lambda: generate_doc("Test topic", mode="latex",
                                     template_path=tpl, out_dir=out_dir),
                expect_min_requests=2)
        paths = [p for p, _m, _r in seen]
        self.assertIn("/api/docgen/load-template", paths)
        self.assertIn("/api/docgen/generate", paths)

    def test_generate_doc_markdown_body_accepted(self):
        from core.driver import generate_doc
        self._run(lambda: generate_doc("Test topic", mode="markdown"),
                  allowed_400=r"vault|folder|path")

    # --- 2026-09-17 review, E1: every registry row has a replay case --------

    def test_online_history_rows_reach_live_routes(self):
        from core import driver
        from api.routes import online_ocr
        from rag.storage_io import vault_key
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            vault = root / "vault"
            vault.mkdir()
            run_id = "a" * 32
            directory = root / "online_index_runs" / run_id
            directory.mkdir(parents=True)
            (directory / "run.json").write_text(json.dumps({
                "vault_key": vault_key(vault), "source_count": 0}))
            with patch.object(online_ocr, "BASE_DIR", str(root)), \
                    patch.object(online_ocr, "load_config", return_value={"obsidian_vault_path": str(vault)}):
                result, _ = self._run(driver.online_runs)
                self.assertEqual(result["runs"][0]["run_id"], run_id)
                detail, _ = self._run(lambda: driver._http_json(
                    "online_run", base_url="http://127.0.0.1:1", path_params={"run_id": run_id}))
                self.assertEqual(detail["run_id"], run_id)
                self.assertFalse(detail["promotion_barrier"])

    def test_health_probe_reaches_the_route(self):
        from core.driver import is_server_running
        self._run(lambda: self.assertTrue(is_server_running("http://127.0.0.1:1")))

    def test_audit_scan_body_accepted(self):
        from core.driver import audit_scan
        with patch("audit.manager.audit_manager.start_scan", return_value=(False, "fixture: no scan started")):
            self._run(audit_scan, allowed_400=r"vault|config|path|scan")

    def test_tagging_restore_body_accepted(self):
        from core.driver import tagging_restore
        self._run(lambda: tagging_restore("fixture-op"), allowed_400=r"vault|scan|journal|archive|op")


    def test_summarise_request_reaches_the_route(self):
        from core.driver import summarize_paper
        with tempfile.TemporaryDirectory() as td:
            pdf = os.path.join(td, "paper.pdf")
            with open(pdf, "wb") as fh:
                fh.write(b"%PDF-fixture")
            with patch("core.driver._upload_pdf", return_value={"upload_id": "fixture-upload"}):
                self._run(lambda: summarize_paper(pdf), allowed_400=r"upload|pdf|file|model|provider")

    def test_notesync_campaign_read_and_plan_rows_reach_live_routes(self):
        from core.driver import notesync_campaign
        self._run(lambda: notesync_campaign("plan", data={"citation_keys": ["one"]}),
                  allowed_400=r"scan|vault|key")
        self._run(lambda: notesync_campaign("review", identifier="fixture"), allowed_400=r"campaign")
        self._run(lambda: notesync_campaign("inspect", identifier="fixture"), allowed_400=r"campaign")

    def test_notesync_campaign_apply_replays_through_closed_production_gate(self):
        from core.driver import notesync_campaign
        _res, seen = self._run(lambda: notesync_campaign("apply", identifier="fixture", data={
            "confirm": True, "decisions": [{"citation_key": "one", "evidence": "e", "approve": True, "result": "x"}]}))
        self.assertTrue(any(p.endswith("/fixture/apply") and r.status_code == 501 for p, _m, r in seen))

    def test_library_bibliography_decisions_body_accepted(self):
        from core import driver
        with tempfile.TemporaryDirectory() as directory, \
                patch("core.config.load_config", return_value={"obsidian_vault_path": directory}):
            self._run(lambda: driver.library_campaign("decisions", {"confirm": True, "decisions": [
                {"record_id": "r", "field": "title", "evidence": "e", "approve": False}]}, "fixture"),
                allowed_400=r"campaign|kind|scope|unknown|not found")

    def test_every_endpoint_row_has_a_replay_case(self):
        """The coverage ratchet CLAUDE.md recorded as missing: a new ENDPOINTS
        row without a replay case fails here instead of in a review. The
        private-only Maintenance rows are replayed in
        maintenance/tests/test_driver_maintenance.py (stripped with them)."""
        from core.driver import ENDPOINTS, _OPTIONAL_ENDPOINT_KEYS
        unclaimed = set(ENDPOINTS) - set(self.CLAIMS) - set(_OPTIONAL_ENDPOINT_KEYS)
        self.assertFalse(unclaimed, f"ENDPOINTS rows without a replay case: {sorted(unclaimed)}")
        methods = {name for name in dir(self) if name.startswith("test_")}
        self.assertFalse(set(self.CLAIMS.values()) - methods, "CLAIMS names a test that does not exist")

    def test_summarize_paper_upload_request_shape(self):
        from core.driver import summarize_paper
        # A non-PDF body: the upload route must reject it as a DOMAIN error
        # (extension/content), never a routing/method failure — proving the
        # multipart request (the old f-string blindspot) reaches the real route.
        with tempfile.TemporaryDirectory() as td:
            pdf = os.path.join(td, "paper.pdf")
            with open(pdf, "wb") as fh:
                fh.write(b"%PDF-1.4 not really a pdf")
            self._run(lambda: summarize_paper(pdf),
                      allowed_400=r"pdf|extract|upload|file|invalid")


if __name__ == "__main__":
    unittest.main()
