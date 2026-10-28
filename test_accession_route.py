"""Route-layer tests for /api/accession/* (Phase 3).

Hermetic: the engine seam (``resolve_queries``) is patched at the route's
import site, every memo path points at a per-test tmp dir, and the gate
tests patch the engine with an AssertionError so passing PROVES the gate
fired before anything could reach the network. SSE responses are drained
INSIDE the patch scope (the worker runs on a daemon thread that only
executes while the response is consumed — the documented suite trap).
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flask

from accession import cache as accession_cache
from accession.candidates import Resolution
from accession.sources import Candidate, default_source_names
from audit.core import doi_cache as core_doi_cache
from audit.core import hash_cache as core_hash_cache
from audit.engine.bridge import BridgeResult
from audit.engine.inventory import Inventory
from audit.manager import audit_manager

import api.routes.accession as acc_route

HEADERS = {"X-Requested-With": "ChatEKLD"}

ENABLED_CFG = {
    "accession_enabled": True,
    "accession_contact_email": "",
    "accession_sources": ["crossref", "europepmc"],
    "accession_max_candidates": 5,
    "accession_timeout_s": 15,
    "accession_batch_max": 3,
}


def _build_app():
    from api.security import register_origin_guard

    app = flask.Flask(__name__)
    register_origin_guard(app)
    app.register_blueprint(acc_route.accession_bp)
    return app


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="accession-route-"))
        self.vault = self.tmp / "vault"
        biblio = self.vault / "Z_attachments" / "biblio_articles"
        biblio.mkdir(parents=True)
        self.pdf = biblio / "myran_2024.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\nfake body\n")
        self.rel = "Z_attachments/biblio_articles/myran_2024.pdf"

        bridge = BridgeResult()
        bridge.unmapped_pdfs = [self.pdf]
        from audit.engine.inventory import Record
        from audit.core.bib import BibEntry

        rec = Record(citation_key="knownKey2025")
        rec.bib_entry = BibEntry(
            citation_key="knownKey2025", entry_type="article",
            title="Known", year="2025", doi="10.1/known.1")
        self.inv = Inventory(
            records={"knownKey2025": rec}, bridge=bridge,
            pdfs_skipped=[], zotero_matched_by_title=0)
        self.settings = SimpleNamespace(vault_root=self.vault)

        # Per-test memo files — never the shared session BASE_DIR.
        for mod, name in ((accession_cache, "accession_cache.json"),
                          (core_hash_cache, "hash_cache.json"),
                          (core_doi_cache, "doi_cache.json")):
            p = mock.patch.object(mod, "cache_path",
                                  lambda n=name: self.tmp / n)
            p.start()
            self.addCleanup(p.stop)
        # No app config / vault needed for the page-1 provider in tests.
        p = mock.patch("api.routes.audit._build_pdf_text_provider",
                       lambda: None)
        p.start()
        self.addCleanup(p.stop)

        self.client = _build_app().test_client()

    @staticmethod
    def _sse_frames(raw: bytes) -> list[dict]:
        out = []
        for line in raw.decode("utf-8").splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                out.append(json.loads(line[len("data: "):]))
        return out

    def _with_scan(self, cfg=None):
        """Patches for: cached scan present, not scanning, config as given."""
        return (
            mock.patch.object(audit_manager, "get_inventory",
                              return_value=(self.inv, self.settings)),
            mock.patch.object(audit_manager, "is_scanning",
                              return_value=False),
            mock.patch.object(acc_route, "load_config",
                              return_value=dict(cfg or ENABLED_CFG)),
        )


class TestAuth(_Base):
    def test_all_routes_require_the_xrw_header(self):
        self.assertEqual(self.client.get("/api/accession/rows").status_code, 403)
        self.assertEqual(
            self.client.post("/api/accession/identify", json={}).status_code, 403)
        self.assertEqual(
            self.client.post("/api/accession/batch", json={}).status_code, 403)


class TestGates(_Base):
    def test_rows_404_without_a_cached_scan(self):
        with mock.patch.object(audit_manager, "get_inventory",
                               return_value=(None, None)), \
                mock.patch.object(audit_manager, "is_scanning",
                                  return_value=False), \
                mock.patch.object(acc_route, "load_config", return_value={}):
            r = self.client.get("/api/accession/rows", headers=HEADERS)
        self.assertEqual(r.status_code, 404)

    def test_identify_403_when_disabled_before_anything_else(self):
        cfg = dict(ENABLED_CFG, accession_enabled=False)
        p1, p2, p3 = self._with_scan(cfg)
        with p1, p2, p3, mock.patch.object(
                acc_route, "resolve_queries",
                side_effect=AssertionError("engine reached while disabled")):
            r = self.client.post("/api/accession/identify",
                                 json={"pdf": self.rel}, headers=HEADERS)
        self.assertEqual(r.status_code, 403)
        self.assertIn("accession_enabled", r.get_json()["error"])

    def test_identify_409_while_a_scan_is_running(self):
        p1, _p2, p3 = self._with_scan()
        with p1, p3, mock.patch.object(
                audit_manager, "is_scanning", return_value=True), \
                mock.patch.object(
                    acc_route, "resolve_queries",
                    side_effect=AssertionError("engine reached mid-scan")):
            r = self.client.post("/api/accession/identify",
                                 json={"pdf": self.rel}, headers=HEADERS)
        self.assertEqual(r.status_code, 409)

    def test_identify_400_for_traversal_and_non_orphans(self):
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3:
            for bad in ("../etc/passwd", "/abs/path.pdf", "nope.pdf",
                        "Z_attachments/biblio_articles/other.pdf", "", None):
                r = self.client.post("/api/accession/identify",
                                     json={"pdf": bad}, headers=HEADERS)
                self.assertEqual(r.status_code, 400, f"{bad!r}")

    def test_identify_accepts_a_pending_pdf_but_not_a_no_match_one(self):
        """2026-09-06: the curated "pending" bucket is identifiable (its
        resolution feeds the DOI hand-off); "not a paper for Zotero" is not."""
        pend = self.pdf.with_name("pending_2020.pdf")
        pend.write_bytes(b"%PDF-1.4 pending")
        nm = self.pdf.with_name("nomatch_2020.pdf")
        nm.write_bytes(b"%PDF-1.4 nomatch")
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3:
            inv, settings = acc_route.audit_manager.get_inventory()
            inv.bridge.pending_pdfs = [pend]
            inv.bridge.confirmed_no_match_pdfs = [nm]
            rel_pend = str(pend.relative_to(settings.vault_root))
            rel_nm = str(nm.relative_to(settings.vault_root))
            self.assertIsNotNone(acc_route._resolve_orphan(inv, settings, rel_pend))
            self.assertIsNone(acc_route._resolve_orphan(inv, settings, rel_nm))
            r = self.client.post("/api/accession/batch",
                                 json={"pdfs": [rel_nm], "confirm": True}, headers=HEADERS)
            self.assertEqual(r.status_code, 400)
            self.assertIn("pending", r.get_json()["error"])

    def test_resolve_orphan_skips_a_zotero_unclaimed_file(self):
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3:
            inv, settings = acc_route.audit_manager.get_inventory()
            inv.bridge.zotero_unexported = {self.pdf: "ABC123"}
            self.assertIsNone(acc_route._resolve_orphan(inv, settings, self.rel))
            inv.bridge.zotero_unexported = {}
            inv.bridge.zotero_multi_links = {self.pdf: ["A", "B"]}
            self.assertIsNone(acc_route._resolve_orphan(inv, settings, self.rel))

    def test_pending_rows_skip_a_zotero_unclaimed_file(self):
        """A pending PDF that Zotero already links (unexported / two parents)
        must not appear in /rows either. Phase 02's default scope is pending,
        and a listed row would propose a create for a file that already has
        an attachment. ``_resolve_orphan`` already skips these; the pending
        loop of ``rows`` did not."""
        pending = self.pdf.parent / "held_2020.pdf"
        pending.write_bytes(b"%PDF-1.4\nheld\n")
        prel = "Z_attachments/biblio_articles/held_2020.pdf"
        self.inv.bridge.pending_pdfs = [pending]
        self.inv.bridge.zotero_unexported = {pending: "ABC123"}
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3:
            only = self.client.get("/api/accession/rows?scope=pending",
                                   headers=HEADERS).get_json()
            both = self.client.get("/api/accession/rows?scope=all",
                                   headers=HEADERS).get_json()
        self.assertNotIn(prel, [r["pdf"]["rel"] for r in only["rows"]])
        self.assertNotIn(prel, [r["pdf"]["rel"] for r in both["rows"]])
        self.inv.bridge.zotero_unexported = {}
        self.inv.bridge.zotero_multi_links = {pending: ["A", "B"]}
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3:
            only = self.client.get("/api/accession/rows?scope=pending",
                                   headers=HEADERS).get_json()
        self.assertNotIn(prel, [r["pdf"]["rel"] for r in only["rows"]])

    def test_batch_force_bypasses_the_cache_consult(self):
        """2026-09-06: bulk re-identify. `force: true` reaches the engine's
        force_refresh for every PDF; a plain batch keeps its cache hits."""
        p1, p2, p3 = self._with_scan()
        seen = []

        def _fake(pdf_path, rel, ctx, force=False, progress_cb=None, should_cancel=None):
            seen.append(force)
            return {"pdf": {"rel": rel, "name": pdf_path.name}, "sha256": None,
                    "resolution": {"outcome": "none"}}

        with p1, p2, p3, mock.patch.object(acc_route, "_identify_one", side_effect=_fake), \
                mock.patch.object(acc_route, "_build_context", return_value=object()):
            r = self.client.post("/api/accession/batch",
                                 json={"pdfs": [self.rel], "confirm": True, "force": True},
                                 headers=HEADERS)
            r.get_data()
            r2 = self.client.post("/api/accession/batch",
                                  json={"pdfs": [self.rel], "confirm": True},
                                  headers=HEADERS)
            r2.get_data()
        self.assertEqual((r.status_code, r2.status_code), (200, 200))
        self.assertEqual(seen, [True, False])

    def test_identify_429_while_another_op_holds_the_gate(self):
        p1, p2, p3 = self._with_scan()
        acc_route._ACCESSION_GATE.acquire()
        try:
            with p1, p2, p3:
                r = self.client.post("/api/accession/identify",
                                     json={"pdf": self.rel}, headers=HEADERS)
            self.assertEqual(r.status_code, 429)
        finally:
            acc_route._ACCESSION_GATE.release()


class TestIdentify(_Base):
    def test_identify_resolves_and_persists_the_memos(self):
        res = Resolution(outcome="link", tier="bib_doi",
                         citation_key="knownKey2025", doi="10.1/known.1")
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3, mock.patch.object(
                acc_route, "resolve_queries", return_value=res) as rq:
            r = self.client.post("/api/accession/identify",
                                 json={"pdf": self.rel}, headers=HEADERS)
            frames = self._sse_frames(r.get_data())
        self.assertEqual(r.status_code, 200)
        d = next(frame["row"] for frame in frames if "row" in frame)
        self.assertEqual(d["pdf"]["rel"], self.rel)
        self.assertEqual(d["resolution"]["outcome"], "link")
        self.assertEqual(d["resolution"]["citation_key"], "knownKey2025")
        self.assertTrue(d["sha256"])
        # The rel→sha binding /rows depends on was persisted.
        self.assertIn(str(self.pdf),
                      json.loads((self.tmp / "hash_cache.json").read_text()))
        # The engine received the CACHED SCAN's bib index (the offline-link
        # guarantee is only as good as the index the route builds).
        self.assertEqual(rq.call_args.kwargs["bib_doi_index"],
                         {"10.1/known.1": ["knownKey2025"]})

    def test_force_reaches_the_engine_and_defaults_off(self):
        res = Resolution(outcome="none")
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3, mock.patch.object(
                acc_route, "resolve_queries", return_value=res) as rq:
            first = self.client.post("/api/accession/identify",
                                     json={"pdf": self.rel}, headers=HEADERS)
            first.get_data()
            self.assertFalse(rq.call_args.kwargs["force_refresh"])
            second = self.client.post("/api/accession/identify",
                                      json={"pdf": self.rel, "force": True},
                                      headers=HEADERS)
            second.get_data()
            self.assertTrue(rq.call_args.kwargs["force_refresh"])

    def test_gate_is_released_after_a_crash(self):
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3, mock.patch.object(
                acc_route, "resolve_queries", side_effect=RuntimeError("boom")):
            r = self.client.post("/api/accession/identify",
                                 json={"pdf": self.rel}, headers=HEADERS)
            frames = self._sse_frames(r.get_data())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any("error" in frame for frame in frames))
        self.assertTrue(acc_route._ACCESSION_GATE.acquire(blocking=False))
        acc_route._ACCESSION_GATE.release()

    def test_remote_ladder_runs_off_the_request_execution_path(self):
        """The response starts while the remote ladder is still blocked.

        Before AP-05 the synchronous route could not return a response object
        until ``resolve_queries`` completed, occupying the request execution
        path for the complete remote-source budget.
        """
        entered = threading.Event()
        release = threading.Event()

        def blocked(*_args, **_kwargs):
            entered.set()
            assert release.wait(5)
            return Resolution(outcome="none")

        p1, p2, p3 = self._with_scan()
        with p1, p2, p3, mock.patch.object(
                acc_route, "resolve_queries", side_effect=blocked):
            response = self.client.post(
                "/api/accession/identify", json={"pdf": self.rel},
                headers=HEADERS, buffered=False,
            )
            self.assertTrue(entered.wait(1))
            self.assertEqual(response.status_code, 200)
            release.set()
            self._sse_frames(b"".join(response.response))
        self.assertTrue(acc_route._ACCESSION_GATE.acquire(blocking=False))
        acc_route._ACCESSION_GATE.release()


class TestRows(_Base):
    def test_rows_join_cached_resolutions_and_revalidate_against_the_bib(self):
        # Seed: a cached ACCESSION answer whose DOI has since entered the bib.
        sha = "ab" * 32
        cache = {}
        stored = Resolution(
            outcome="candidates", tier="xmp_doi", doi="10.1/known.1",
            query_kind="doi", evidence="meta",
            candidates=[Candidate(source="crossref", title="Known")],
            scores=[1.0])
        accession_cache.store(cache, sha, stored.to_jsonable())
        accession_cache.save_cache(cache)
        st = self.pdf.stat()
        core_hash_cache.save_cache({str(self.pdf): {
            "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha}})

        p1, p2, p3 = self._with_scan()
        with p1, p2, p3:
            r = self.client.get("/api/accession/rows", headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["resolved_count"], 1)
        row = d["rows"][0]
        self.assertEqual(row["pdf"]["rel"], self.rel)
        self.assertEqual(row["resolution"]["outcome"], "link")   # flipped
        self.assertEqual(row["resolution"]["citation_key"], "knownKey2025")
        self.assertTrue(row["resolution"]["from_cache"])
        # Cache age (2026-08-19 review): the eviction stamp is surfaced so
        # the UI can render "cached · N d ago".
        self.assertIsInstance(row["resolution"]["queried_at"], float)

    def test_rows_default_to_orphans_and_say_which_bucket_each_row_is(self):
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3:
            d = self.client.get("/api/accession/rows", headers=HEADERS).get_json()
        self.assertEqual(d["scope"], "orphans")
        self.assertEqual([r["pdf"]["rel"] for r in d["rows"]], [self.rel])
        self.assertEqual(d["rows"][0]["state"], "orphan")
        self.assertEqual((d["orphan_count"], d["pending_count"]), (1, 0))

    def test_the_curated_pending_bucket_is_reachable_and_is_not_the_default(self):
        """0 orphans and 296 pending is the live Phase 2 shape, and a read
        that cannot see `pending` cannot serve it."""
        pending = self.pdf.parent / "pending_2020.pdf"
        pending.write_bytes(b"%PDF-1.4\npending\n")
        self.inv.bridge.pending_pdfs = [pending]
        prel = "Z_attachments/biblio_articles/pending_2020.pdf"

        p1, p2, p3 = self._with_scan()
        with p1, p2, p3:
            default = self.client.get("/api/accession/rows", headers=HEADERS).get_json()
            only = self.client.get("/api/accession/rows?scope=pending",
                                   headers=HEADERS).get_json()
            both = self.client.get("/api/accession/rows?scope=all",
                                   headers=HEADERS).get_json()
        # The default is unchanged — an existing client's table cannot grow
        # rows it never asked for.
        self.assertEqual([r["pdf"]["rel"] for r in default["rows"]], [self.rel])
        self.assertEqual([r["pdf"]["rel"] for r in only["rows"]], [prel])
        self.assertEqual(only["rows"][0]["state"], "pending")
        self.assertEqual((only["orphan_count"], only["pending_count"]), (0, 1))
        self.assertEqual({r["pdf"]["rel"] for r in both["rows"]}, {self.rel, prel})
        self.assertEqual((both["orphan_count"], both["pending_count"]), (1, 1))

    def test_a_pdf_in_both_buckets_is_listed_once_as_an_orphan(self):
        self.inv.bridge.pending_pdfs = [self.pdf]
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3:
            d = self.client.get("/api/accession/rows?scope=all", headers=HEADERS).get_json()
        self.assertEqual(len(d["rows"]), 1)
        self.assertEqual(d["rows"][0]["state"], "orphan")

    def test_an_unknown_scope_is_refused_rather_than_silently_defaulted(self):
        """Defaulting would hand a caller that asked for `pending` the orphan
        set — 0 rows on the live library — which reads as "nothing to do"
        instead of as a typo. The tagging `--targets []` rule."""
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3:
            r = self.client.get("/api/accession/rows?scope=pendng", headers=HEADERS)
        self.assertEqual(r.status_code, 400)
        self.assertIn("pending", r.get_json()["error"])

    def test_pending_rows_carry_their_cached_resolution_too(self):
        pending = self.pdf.parent / "grey_2019.pdf"
        pending.write_bytes(b"%PDF-1.4\ngrey\n")
        self.inv.bridge.pending_pdfs = [pending]
        sha = "cd" * 32
        cache = {}
        accession_cache.store(cache, sha, Resolution(
            outcome="candidates", tier="filename_only",
            candidates=[Candidate(source="crossref", title="Maybe")],
            scores=[0.5]).to_jsonable())
        accession_cache.save_cache(cache)
        st = pending.stat()
        core_hash_cache.save_cache({str(pending): {
            "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha}})

        p1, p2, p3 = self._with_scan()
        with p1, p2, p3:
            d = self.client.get("/api/accession/rows?scope=pending",
                                headers=HEADERS).get_json()
        self.assertEqual(d["resolved_count"], 1)
        self.assertEqual(d["rows"][0]["resolution"]["tier"], "filename_only")

    def test_rows_envelope_discloses_the_configured_sources_and_email(self):
        # The panel's egress disclosure is built from the SERVER's resolved
        # source subset — a hard-coded five-source label would overstate
        # egress when accession_sources narrows the set.
        cfg = dict(ENABLED_CFG)
        cfg["accession_sources"] = ["crossref"]
        cfg["accession_contact_email"] = "user@example.org"
        p1, p2, p3 = self._with_scan(cfg)
        with p1, p2, p3:
            r = self.client.get("/api/accession/rows", headers=HEADERS)
        d = r.get_json()
        self.assertEqual(d["sources"], ["crossref"])
        self.assertTrue(d["contact_email_set"])
        # The email itself never rides the payload.
        self.assertNotIn("user@example.org", r.get_data(as_text=True))

    def test_rows_with_no_memoised_sha_render_unresolved_without_hashing(self):
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3, mock.patch.object(
                core_hash_cache, "load_cache", return_value={}), \
                mock.patch.object(
                    acc_route.core_hashing, "full_hash",
                    side_effect=AssertionError("rows must never hash")):
            r = self.client.get("/api/accession/rows", headers=HEADERS)
        d = r.get_json()
        self.assertIsNone(d["rows"][0]["sha256"])
        self.assertIsNone(d["rows"][0]["resolution"])
        self.assertFalse(d["enabled"] and False)   # enabled key present
        self.assertIn("batch_max", d)

    def test_rows_expose_excluded_keys_from_mapping(self):
        """Additive wire field so the UI can refuse bulk rematch of unmaps."""
        mapping = self.tmp / "mapping.json"
        mapping.write_text(json.dumps({
            "matches": {},
            "no_match": [],
            "exclude": {self.rel: ["knownKey2025"]},
        }), encoding="utf-8")
        self.settings.mapping_file = mapping
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3:
            r = self.client.get("/api/accession/rows", headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.get_json())
        row = r.get_json()["rows"][0]
        self.assertEqual(row["excluded_keys"], ["knownKey2025"])
        # Absent exclude → empty list (never omitted / None).
        mapping.write_text(json.dumps({"matches": {}, "no_match": []}),
                           encoding="utf-8")
        with p1, p2, p3:
            r = self.client.get("/api/accession/rows", headers=HEADERS)
        self.assertEqual(r.get_json()["rows"][0]["excluded_keys"], [])


class TestBatch(_Base):
    def test_batch_requires_confirm_and_caps_the_selection(self):
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3:
            r = self.client.post("/api/accession/batch",
                                 json={"pdfs": [self.rel]}, headers=HEADERS)
            self.assertEqual(r.status_code, 400)   # no confirm
            r = self.client.post(
                "/api/accession/batch",
                json={"pdfs": [self.rel] * 4, "confirm": True},
                headers=HEADERS)
            self.assertEqual(r.status_code, 400)   # over batch_max (3)
            self.assertIn("accession_batch_max", r.get_json()["error"])

    def test_batch_refuses_an_unknown_rel_rather_than_thinning_it(self):
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3:
            r = self.client.post(
                "/api/accession/batch",
                json={"pdfs": [self.rel, "nope.pdf"], "confirm": True},
                headers=HEADERS)
        self.assertEqual(r.status_code, 400)
        self.assertIn("nope.pdf", r.get_json()["error"])

    def test_batch_gate_is_released_when_the_context_build_crashes(self):
        """The context build runs between acquire and the SSE guard taking
        ownership — an escaping exception there leaked the gate for the
        process lifetime (every later identify/batch 429'd)."""
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3, mock.patch.object(
                acc_route, "_build_context",
                side_effect=RuntimeError("boom")):
            r = self.client.post(
                "/api/accession/batch",
                json={"pdfs": [self.rel], "confirm": True}, headers=HEADERS)
        self.assertEqual(r.status_code, 502)
        self.assertTrue(acc_route._ACCESSION_GATE.acquire(blocking=False))
        acc_route._ACCESSION_GATE.release()

    def test_batch_stops_between_pdfs_when_a_scan_starts(self):
        """"Nothing runs during a scan" must hold mid-batch, not only at the
        gates: a scan starting between two PDFs stops the run (results
        already paid for are cached, so a re-run resumes for free)."""
        pdf2 = self.pdf.parent / "second_2020.pdf"
        pdf2.write_bytes(b"%PDF-1.4\nsecond body\n")
        rel2 = "Z_attachments/biblio_articles/second_2020.pdf"
        self.inv.bridge.unmapped_pdfs.append(pdf2)

        res = Resolution(outcome="none")
        # False for the network gate, True from the worker's first loop check.
        scan_states = iter([False, True, True, True])
        p1, _p2, p3 = self._with_scan()
        with p1, p3, mock.patch.object(
                audit_manager, "is_scanning",
                side_effect=lambda: next(scan_states)), \
                mock.patch.object(acc_route, "resolve_queries",
                                  return_value=res):
            r = self.client.post(
                "/api/accession/batch",
                json={"pdfs": [self.rel, rel2], "confirm": True},
                headers=HEADERS)
            self.assertEqual(r.status_code, 200)
            frames = self._sse_frames(r.get_data())   # drained IN patch scope
        rows = [f for f in frames if "row" in f]
        self.assertEqual(rows, [])   # stopped before ANY network resolution
        terminal = [f["accession"] for f in frames if "accession" in f]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["done"], 0)
        self.assertTrue(terminal[0]["cancelled"])
        self.assertTrue(any("scan" in f.get("info", "").lower()
                            for f in frames))

    def test_batch_streams_a_row_per_pdf_then_a_summary(self):
        res = Resolution(outcome="candidates", tier="printed_doi",
                         doi="10.1/new.1")
        p1, p2, p3 = self._with_scan()
        with p1, p2, p3, mock.patch.object(
                acc_route, "resolve_queries", return_value=res):
            r = self.client.post(
                "/api/accession/batch",
                json={"pdfs": [self.rel], "confirm": True}, headers=HEADERS)
            self.assertEqual(r.status_code, 200)
            frames = self._sse_frames(r.get_data())   # drained IN patch scope
        rows = [f["row"] for f in frames if "row" in f]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolution"]["tier"], "printed_doi")
        terminal = [f["accession"] for f in frames if "accession" in f]
        self.assertEqual(terminal, [{
            "total": 1, "done": 1,
            "outcomes": {"candidates": 1}, "cancelled": False}])
        # The op gate was released by the SSE guard once the stream ended.
        self.assertTrue(acc_route._ACCESSION_GATE.acquire(blocking=False))
        acc_route._ACCESSION_GATE.release()

    def test_consecutive_failure_terminal_frame_carries_partial_tally(self):
        """The shared client stops reading at {error}; a summary emitted after
        it is unreachable, so the error frame itself must reconcile every row
        already shown to the user."""
        rels = [self.rel]
        for number in range(2, acc_route._MAX_CONSECUTIVE_FAILURES + 1):
            pdf = self.pdf.parent / f"failed_{number}.pdf"
            pdf.write_bytes(b"%PDF-1.4\nfailed body\n")
            self.inv.bridge.unmapped_pdfs.append(pdf)
            rels.append(f"Z_attachments/biblio_articles/failed_{number}.pdf")

        cfg = dict(ENABLED_CFG, accession_batch_max=len(rels))
        p1, p2, p3 = self._with_scan(cfg)
        with p1, p2, p3, mock.patch.object(
                acc_route, "_identify_one",
                side_effect=RuntimeError("catalog unavailable")):
            r = self.client.post(
                "/api/accession/batch",
                json={"pdfs": rels, "confirm": True}, headers=HEADERS)
            self.assertEqual(r.status_code, 200)
            frames = self._sse_frames(r.get_data())

        rows = [frame["row"] for frame in frames if "row" in frame]
        self.assertEqual(len(rows), 5)
        errors = [frame for frame in frames if "error" in frame]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["accession"], {
            "total": 5, "done": 5, "outcomes": {"error": 5},
            "cancelled": False, "failed_early": True,
        })
        self.assertEqual(
            [frame for frame in frames
             if "accession" in frame and "error" not in frame], [])


class TestConfigContracts(unittest.TestCase):
    def test_source_name_registries_agree(self):
        """config.py keeps a literal copy so it never imports the network
        module — this is the pin that keeps the two lists from drifting."""
        from api.routes.config import ACCESSION_SOURCE_NAMES

        self.assertEqual(tuple(ACCESSION_SOURCE_NAMES),
                         default_source_names())

    def test_sources_coercer_filters_dedupes_and_drops_empty(self):
        from api.routes.config import _coerce_accession_sources as c

        self.assertEqual(c(["crossref", "bogus", "crossref", "hal"]),
                         ["crossref", "hal"])
        self.assertIsNone(c(["bogus"]))
        self.assertIsNone(c([]))          # "no sources" is never a choice
        self.assertIsNone(c("crossref"))  # not a list

    def test_batch_max_ceiling_is_one_definition(self):
        """The knob is validated in two places that cannot import each other
        (`POST /api/config` persists it; the accession route re-reads it at
        call time). They carried 7500 and 500, so a persisted 1500 was
        accepted by the config route and then silently clamped to 500 by the
        route that uses it — a setting that saved and did nothing.

        Pinned on BEHAVIOUR, not on the constant: asserting both sites import
        the same name would still pass if one re-clamped afterwards.
        """
        from unittest import mock

        from api.routes.config import _CONFIG_VALIDATORS
        import api.routes.accession as acc
        from core.constants import (
            ACCESSION_BATCH_MAX_CEILING,
            ACCESSION_BATCH_MAX_DEFAULT,
        )

        persist = _CONFIG_VALIDATORS["accession_batch_max"]
        for value in (1, 50, 500, 1500, ACCESSION_BATCH_MAX_CEILING):
            stored = persist(value)
            self.assertEqual(stored, value, f"config route rejected {value}")
            with mock.patch.object(
                    acc, "load_config",
                    lambda stored=stored: {"accession_batch_max": stored}):
                self.assertEqual(
                    acc._accession_cfg()["batch_max"], value,
                    f"a persisted {value} does not survive to the route")

        # Out of range still clamps, and an unset knob keeps its default.
        with mock.patch.object(
                acc, "load_config",
                lambda: {"accession_batch_max": ACCESSION_BATCH_MAX_CEILING + 1}):
            self.assertEqual(acc._accession_cfg()["batch_max"],
                             ACCESSION_BATCH_MAX_CEILING)
        with mock.patch.object(acc, "load_config", lambda: {}):
            self.assertEqual(acc._accession_cfg()["batch_max"],
                             ACCESSION_BATCH_MAX_DEFAULT)

    def test_contact_email_coercer(self):
        from api.routes.config import _coerce_contact_email as c

        self.assertEqual(c(""), "")                      # empty kept
        self.assertEqual(c("me@example.org"), "me@example.org")
        self.assertIsNone(c("not-an-email"))
        self.assertIsNone(c("a b@example.org"))
        self.assertIsNone(c(42))


if __name__ == "__main__":
    unittest.main()
