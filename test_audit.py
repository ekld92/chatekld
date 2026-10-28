"""End-to-end tests for the Library Audit subsystem.

Covers:

- the :func:`audit.config.load_settings` adapter (CHERQAiN keys ->
  kb_harmonizer Settings, including the configurable subpaths)
- the :class:`audit.manager.AuditManager` state machine
- the critical regression that ``create_app()`` never starts a scan as
  a side effect — the audit must remain ``idle`` until the user clicks
  Run Scan.
- the validation surface on ``/api/audit/config`` and the auth gate on
  every audit route.

Isolation rationale
-------------------
We deliberately do NOT mutate ``core.constants`` path attributes here.
Doing so caused stale module-level closures in ``api/routes/config.py``
(captured at smoke_test collection time) to keep pointing at the old
obsidian_manager singleton after test_audit cleaned up, which broke
``test_30_reset_returns_503_when_indexer_busy`` and friends.

Instead, the manager / route tests build a minimal Flask app that only
registers ``audit_bp``, and use ``mock.patch`` on ``core.config.CONFIG_FILE``
(plus ``core.constants.CONFIG_FILE`` for the value-bound copy) so
nothing outside the audit subsystem sees a patched path.  The single
test that *requires* the real ``create_app()`` (``TestFlaskAppBootDoesNotScan``)
runs it once, asserts the spy was not called, and tolerates whatever
side effects ``create_app`` has on shared singletons — it never tries
to clean them up.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from audit.manager import audit_manager

import flask

# Ensure repo root is on sys.path (mirrors tests/audit/conftest.py for the
# project-root location of this file).
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _patched_config_file(tmp_dir: Path) -> mock._patch:
    """Return an unstarted patcher that points CONFIG_FILE at *tmp_dir*.

    ``core.config.load_config`` value-imports ``CONFIG_FILE`` at module
    load time, so we have to patch both the source (``core.constants``)
    and the local rebinding (``core.config``).
    """
    target_str = str(tmp_dir / "config.json")
    return mock.patch.multiple(
        "core.constants",
        CONFIG_FILE=target_str,
    )


class TestSettingsAdapter(unittest.TestCase):
    """``audit.config.load_settings`` reads CHERQAiN config and produces
    a Settings dataclass whose paths reflect the configured subdirs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-test-settings-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()

        # Patch CONFIG_FILE on both binding sites for the duration of
        # the test, then auto-restore in tearDown via addCleanup.
        cfg_path = str(self.tmp / "config.json")
        p1 = mock.patch("core.constants.CONFIG_FILE", cfg_path)
        p2 = mock.patch("core.config.CONFIG_FILE", cfg_path)
        p1.start()
        self.addCleanup(p1.stop)
        p2.start()
        self.addCleanup(p2.stop)
        # Also patch BASE_DIR so the mapping.json default (BASE_DIR/audit/...)
        # lands inside the tmpdir rather than the real app data dir.
        base = str(self.tmp)
        p3 = mock.patch("core.constants.BASE_DIR", base)
        p3.start()
        self.addCleanup(p3.stop)

        (self.tmp / "config.json").write_text(
            json.dumps({
                "obsidian_vault_path": str(self.vault),
                "audit_attachments_subdir": "MyAttach",
                "audit_biblio_articles_subdir": "Papers",
                "audit_zotero_notes_subdir": "ZNotes",
                "audit_master_bib_path": "refs/master.bib",
                "audit_zotero_sqlite": "~/Zotero/zotero.sqlite",
                "audit_zotero_storage": "~/Zotero/storage",
                "audit_annotations_read_threshold": 7,
                "audit_biblio_skip_prefix": "skip_",
            }),
            encoding="utf-8",
        )

    def test_subpaths_are_configurable(self):
        from audit.config import load_settings

        s = load_settings()
        self.assertEqual(s.vault_root, self.vault.resolve())
        self.assertEqual(s.attachments_subdir, "MyAttach")
        self.assertEqual(s.biblio_articles_subdir, "Papers")
        self.assertEqual(s.zotero_notes_subdir, "ZNotes")
        self.assertEqual(s.master_bib_path, "refs/master.bib")
        self.assertEqual(s.annotations_read_threshold, 7)
        self.assertEqual(s.biblio_skip_prefix, "skip_")
        self.assertEqual(s.attachments_dir, self.vault.resolve() / "MyAttach")
        self.assertEqual(
            s.biblio_articles_dir, self.vault.resolve() / "MyAttach" / "Papers"
        )
        self.assertEqual(s.zotero_notes_dir, self.vault.resolve() / "ZNotes")
        self.assertEqual(s.master_bib, self.vault.resolve() / "refs" / "master.bib")

    def test_defaults_fall_through_when_keys_missing(self):
        (self.tmp / "config.json").write_text(
            json.dumps({"obsidian_vault_path": str(self.vault)}),
            encoding="utf-8",
        )
        from audit.config import (
            DEFAULT_ATTACHMENTS_SUBDIR,
            DEFAULT_BIBLIO_ARTICLES_SUBDIR,
            DEFAULT_MASTER_BIB_PATH,
            DEFAULT_ZOTERO_NOTES_SUBDIR,
            load_settings,
        )

        s = load_settings()
        self.assertEqual(s.attachments_subdir, DEFAULT_ATTACHMENTS_SUBDIR)
        self.assertEqual(s.biblio_articles_subdir, DEFAULT_BIBLIO_ARTICLES_SUBDIR)
        self.assertEqual(s.zotero_notes_subdir, DEFAULT_ZOTERO_NOTES_SUBDIR)
        self.assertEqual(s.master_bib_path, DEFAULT_MASTER_BIB_PATH)

    def test_missing_vault_path_raises(self):
        (self.tmp / "config.json").write_text(
            json.dumps({"obsidian_vault_path": ""}),
            encoding="utf-8",
        )
        from audit.config import AuditConfigError, load_settings

        with self.assertRaises(AuditConfigError):
            load_settings()

    def test_nonexistent_vault_path_raises(self):
        (self.tmp / "config.json").write_text(
            json.dumps({"obsidian_vault_path": "/nonexistent/path/xyz"}),
            encoding="utf-8",
        )
        from audit.config import AuditConfigError, load_settings

        with self.assertRaises(AuditConfigError):
            load_settings()

    def test_empty_skip_prefix_is_preserved(self):
        """Regression: ``cfg.get(...) or DEFAULT`` would coerce a
        user-saved empty string back to ``z_item``, conflicting with the
        engine's documented "empty prefix means skip nothing" behaviour."""
        (self.tmp / "config.json").write_text(
            json.dumps({
                "obsidian_vault_path": str(self.vault),
                "audit_biblio_skip_prefix": "",
            }),
            encoding="utf-8",
        )
        from audit.config import load_settings

        s = load_settings()
        self.assertEqual(s.biblio_skip_prefix, "")

    def test_invalid_threshold_falls_back_to_default(self):
        (self.tmp / "config.json").write_text(
            json.dumps({
                "obsidian_vault_path": str(self.vault),
                "audit_annotations_read_threshold": "garbage",
            }),
            encoding="utf-8",
        )
        from audit.config import DEFAULT_ANNOTATIONS_READ_THRESHOLD, load_settings

        s = load_settings()
        self.assertEqual(s.annotations_read_threshold, DEFAULT_ANNOTATIONS_READ_THRESHOLD)


class TestAuditManagerStateMachine(unittest.TestCase):
    """The manager flips through the right state transitions and exposes
    cached results without recomputing on every read."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-test-mgr-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        # Lay down the conventional subdirs so the inventory walk doesn't
        # short-circuit before producing any state.
        (self.vault / "Z_attachments" / "biblio_articles").mkdir(parents=True)
        (self.vault / "Z_Zotero_Notes").mkdir()
        # _master.bib at the vault root matches the neutral default
        # audit_master_bib_path ("_master.bib"); the config below sets only the
        # vault path, so the scan must resolve the bib through that default.
        (self.vault / "_master.bib").write_text(
            "@article{smith2020, title={Test}, year={2020}, author={Smith, Jane}}",
            encoding="utf-8",
        )

        cfg_path = str(self.tmp / "config.json")
        (self.tmp / "config.json").write_text(
            json.dumps({"obsidian_vault_path": str(self.vault),
                        "audit_zotero_sqlite": str(self.tmp / "absent.sqlite"),
                        "audit_zotero_storage": str(self.tmp / "absent-storage")}),
            encoding="utf-8",
        )

        p1 = mock.patch("core.constants.CONFIG_FILE", cfg_path)
        p2 = mock.patch("core.config.CONFIG_FILE", cfg_path)
        p3 = mock.patch("core.constants.BASE_DIR", str(self.tmp))
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

        # Reset the audit_manager singleton between tests.  The module
        # itself stays cached; we only zero out per-test state so we
        # don't leak inventories or thread state across tests.
        from audit.manager import audit_manager
        with audit_manager._state_lock:
            audit_manager._state = "idle"
            audit_manager._inventory = None
            audit_manager._duplicates = None
            audit_manager._settings = None
            audit_manager._error = ""
            audit_manager._started_at = 0.0
            audit_manager._finished_at = 0.0
            audit_manager._thread = None
        audit_manager.clear_messages()
        self.audit_manager = audit_manager

    def test_starts_idle(self):
        payload = self.audit_manager.get_status_payload()
        self.assertEqual(payload["state"], "idle")
        self.assertFalse(payload["has_results"])
        self.assertFalse(payload["has_duplicates"])

    def test_scan_transitions_to_done(self):
        started, _ = self.audit_manager.start_scan(
            count_annotations=False, include_duplicates=False
        )
        self.assertTrue(started)
        self.assertTrue(self.audit_manager.wait_for_idle(timeout=10.0))
        payload = self.audit_manager.get_status_payload()
        self.assertEqual(payload["state"], "done")
        self.assertTrue(payload["has_results"])
        inv, settings = self.audit_manager.get_inventory()
        self.assertIsNotNone(inv)
        self.assertIsNotNone(settings)
        # Test bib has one entry -> at least one record.
        self.assertGreaterEqual(len(inv.records), 1)

    def test_second_scan_blocked_while_first_running(self):
        # Two scans in quick succession on a tiny vault.  We only pin
        # that the manager doesn't enter an inconsistent state — both
        # complete in a terminal state without raising.
        self.audit_manager.start_scan(count_annotations=False, include_duplicates=False)
        self.audit_manager.start_scan(count_annotations=False, include_duplicates=False)
        self.audit_manager.wait_for_idle(timeout=10.0)
        self.assertIn(
            self.audit_manager.get_status_payload()["state"], {"done", "error"}
        )

    def test_missing_config_yields_error_state(self):
        (self.tmp / "config.json").write_text(json.dumps({}), encoding="utf-8")
        started, msg = self.audit_manager.start_scan(
            count_annotations=False, include_duplicates=False
        )
        self.assertFalse(started)
        self.assertIn("vault", msg.lower())
        self.assertEqual(self.audit_manager.get_status_payload()["state"], "error")

    def test_skip_duplicates_clears_prior_duplicate_cache(self):
        """Regression: ``_finalise`` previously only set ``_duplicates``
        when fresh duplicates were computed, so a follow-up scan run
        with ``include_duplicates=False`` left the old cache in place
        and ``/api/audit/status`` lied (``has_duplicates: true``
        against a fresh inventory)."""
        # First scan: produce duplicates.
        self.audit_manager.start_scan(count_annotations=False, include_duplicates=True)
        self.assertTrue(self.audit_manager.wait_for_idle(timeout=10.0))
        # Inject a fake duplicates list so we have something to clear
        # (the test vault is empty so the real duplicate set is []).
        with self.audit_manager._state_lock:
            self.audit_manager._duplicates = ["fake-duplicate-set"]
        self.assertTrue(self.audit_manager.get_status_payload()["has_duplicates"])

        # Second scan: explicitly skip duplicates.
        self.audit_manager.start_scan(
            count_annotations=False, include_duplicates=False
        )
        self.assertTrue(self.audit_manager.wait_for_idle(timeout=10.0))
        self.assertFalse(
            self.audit_manager.get_status_payload()["has_duplicates"],
            "stale duplicate cache must be cleared when scan opts out",
        )

    def test_cancel_before_duplicates_clears_prior_duplicate_cache(self):
        # Run a scan that produces duplicates, then inject a fake cache.
        self.audit_manager.start_scan(count_annotations=False, include_duplicates=True)
        self.assertTrue(self.audit_manager.wait_for_idle(timeout=10.0))
        with self.audit_manager._state_lock:
            self.audit_manager._duplicates = ["fake-duplicate-set"]

        # Start a fresh scan and cancel it immediately so the worker
        # exits after inventory but before duplicates run.
        self.audit_manager.start_scan(count_annotations=False, include_duplicates=True)
        self.audit_manager.request_cancel()
        self.assertTrue(self.audit_manager.wait_for_idle(timeout=10.0))
        payload = self.audit_manager.get_status_payload()
        # The cancel may or may not catch the worker before duplicates
        # depending on timing on a tiny vault.  What we pin: the cache
        # is in a consistent state (either fresh dupes from this run,
        # or cleared — never the stale ``fake-duplicate-set``).
        if payload["has_duplicates"]:
            dupes, _ = self.audit_manager.get_duplicates()
            self.assertNotIn("fake-duplicate-set", dupes)

    def test_reset_to_idle_drops_results_and_bumps_run_id(self):
        # Run a scan to populate results, then reset and confirm the
        # cached inventory is gone and the state is back to idle.
        started, _ = self.audit_manager.start_scan(
            count_annotations=False, include_duplicates=False
        )
        self.assertTrue(started)
        self.assertTrue(self.audit_manager.wait_for_idle(timeout=10.0))
        self.assertTrue(self.audit_manager.get_status_payload()["has_results"])

        prev_run_id = self.audit_manager._run_id
        self.audit_manager.reset_to_idle()
        payload = self.audit_manager.get_status_payload()
        self.assertEqual(payload["state"], "idle")
        self.assertFalse(payload["has_results"])
        self.assertFalse(payload["has_duplicates"])
        self.assertEqual(payload["error"], "")
        # run_id must increment so a stale _finalise from a parallel
        # worker (theoretical) is rejected.
        self.assertGreater(self.audit_manager._run_id, prev_run_id)

    def test_clear_results_drops_near_duplicates_too(self):
        # 2026-08-09: clear_results dropped _inventory and _duplicates but
        # kept _near_duplicates, so has_near_duplicates stayed True and the
        # report served a stale run against a cleared inventory.
        self.audit_manager._inventory = object()
        self.audit_manager._duplicates = ["x"]
        self.audit_manager._near_duplicates = ["y"]
        self.audit_manager.clear_results()
        payload = self.audit_manager.get_status_payload()
        self.assertFalse(payload["has_results"])
        self.assertFalse(payload["has_duplicates"])
        self.assertFalse(payload["has_near_duplicates"])


class TestFlaskAppBootDoesNotScan(unittest.TestCase):
    """The critical regression: importing the Flask app and creating the
    app object must NOT trigger a scan as a side effect.

    This test does use the real ``create_app()`` because that is exactly
    the contract under test.  We accept that the call has side effects on
    the live obsidian_manager singleton — the test only asserts that the
    audit_manager.start_scan spy is never called.
    """

    def test_create_app_leaves_audit_idle(self):
        from app import create_app
        from audit.manager import audit_manager

        with mock.patch.object(
            audit_manager, "start_scan", wraps=audit_manager.start_scan
        ) as start_spy:
            create_app()

        start_spy.assert_not_called()
        self.assertEqual(audit_manager.get_status_payload()["state"], "idle")


def _build_audit_only_app():
    """Build a minimal Flask app with ONLY the audit blueprint.

    Avoids importing services.vision / rag.vault / etc. so test_audit
    cannot affect the shared singletons that smoke_test relies on.

    Item 4.1: register the app-level origin guard so the CSRF-header tests
    continue to exercise the real gate.
    """
    from api.routes.audit import audit_bp
    from api.security import register_origin_guard

    app = flask.Flask(__name__)
    register_origin_guard(app)
    app.register_blueprint(audit_bp)
    return app


class TestAuditRoutesAuth(unittest.TestCase):
    """All /api/audit/* routes require the X-Requested-With header."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-test-routes-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.tmp / "config.json").write_text(
            json.dumps({"obsidian_vault_path": str(self.vault)}),
            encoding="utf-8",
        )
        cfg_path = str(self.tmp / "config.json")
        p1 = mock.patch("core.constants.CONFIG_FILE", cfg_path)
        p2 = mock.patch("core.config.CONFIG_FILE", cfg_path)
        p3 = mock.patch("core.constants.BASE_DIR", str(self.tmp))
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

        app = _build_audit_only_app()
        self.client = app.test_client()

    def _no_xrw_returns_403(self, method: str, url: str):
        if method == "GET":
            r = self.client.get(url)
        else:
            r = self.client.post(url, json={})
        self.assertEqual(r.status_code, 403, f"{method} {url} returned {r.status_code}")

    def test_get_routes_require_xrw_header(self):
        self._no_xrw_returns_403("GET", "/api/audit/config")
        self._no_xrw_returns_403("GET", "/api/audit/status")
        self._no_xrw_returns_403("GET", "/api/audit/inventory")
        self._no_xrw_returns_403("GET", "/api/audit/reports/note_tag_drift")

    def test_post_routes_require_xrw_header(self):
        self._no_xrw_returns_403("POST", "/api/audit/scan")
        self._no_xrw_returns_403("POST", "/api/audit/cancel")
        self._no_xrw_returns_403("POST", "/api/audit/config")
        self._no_xrw_returns_403("POST", "/api/audit/mapping")
        self._no_xrw_returns_403("POST", "/api/audit/reveal")
        self._no_xrw_returns_403("POST", "/api/audit/apply-tag-drift")

    def test_summary_scanned_at_is_the_served_snapshots_build_time(self):
        """`scanned_at` is the served Inventory's OWN build stamp (2026-08-18).

        The manager's get_scan_times() lies in exactly the two states where
        staleness matters: start_scan zeroes _finished_at while deliberately
        keeping the previous inventory live (mid-scan the old data read as
        scanned_at 0), and _finalise stamps _finished_at even on an errored
        scan with inv=None (a stale snapshot read as freshly scanned). Pinned
        against REAL manager time state — the old test mocked get_scan_times,
        which pinned the plumbing while the semantics were broken.
        """
        headers = {"X-Requested-With": "ChatEKLD"}
        inv = mock.MagicMock()
        inv.records = {}
        inv.built_at = 2000.0
        saved = (audit_manager._started_at, audit_manager._finished_at)
        try:
            with mock.patch.object(
                audit_manager, "get_inventory",
                return_value=(inv, mock.MagicMock()),
            ), mock.patch(
                "audit.serialize.inventory_summary",
                return_value={"record_count": 0},
            ):
                # Mid-scan: a new scan zeroed _finished_at; the served (old)
                # snapshot must keep its true stamp, never read as 0.
                audit_manager._started_at = 3000.0
                audit_manager._finished_at = 0.0
                r = self.client.get("/api/audit/summary", headers=headers)
                self.assertEqual(r.status_code, 200)
                body = r.get_json()
                self.assertEqual(body["scanned_at"], 2000.0)
                self.assertIn("summary", body)
                # Errored rescan: _finalise freshly stamped _finished_at while
                # the old inventory stayed cached; stale must not read fresh.
                audit_manager._finished_at = 4000.0
                r = self.client.get("/api/audit/summary", headers=headers)
                self.assertEqual(r.get_json()["scanned_at"], 2000.0)
        finally:
            (audit_manager._started_at, audit_manager._finished_at) = saved
        # And 404 with no cached scan, unchanged.
        with mock.patch.object(
            audit_manager, "get_inventory", return_value=(None, None)
        ):
            r = self.client.get("/api/audit/summary", headers=headers)
        self.assertEqual(r.status_code, 404)

    def test_summary_carries_the_paper_index_build_stamp(self):
        """Additive `paper_index_built_at` (2026-09-23): the ISO stamp of the
        vault's paper-label index, or null when none — so the audit tab can
        say how stale the labels a chat sees are."""
        headers = {"X-Requested-With": "ChatEKLD"}
        inv = mock.MagicMock()
        inv.records = {}
        inv.built_at = 2000.0

        class _Idx:
            built_at = "2026-09-23T10:00:00+00:00"

        with mock.patch.object(
            audit_manager, "get_inventory", return_value=(inv, mock.MagicMock()),
        ), mock.patch("audit.serialize.inventory_summary", return_value={}), \
                mock.patch("rag.paper_index.load", return_value=_Idx()):
            body = self.client.get("/api/audit/summary", headers=headers).get_json()
        self.assertEqual(body["paper_index_built_at"], "2026-09-23T10:00:00+00:00")
        with mock.patch.object(
            audit_manager, "get_inventory", return_value=(inv, mock.MagicMock()),
        ), mock.patch("audit.serialize.inventory_summary", return_value={}), \
                mock.patch("rag.paper_index.load", return_value=None):
            body = self.client.get("/api/audit/summary", headers=headers).get_json()
        self.assertIsNone(body["paper_index_built_at"])

    def test_apply_tag_drift_requires_confirm_and_scan(self):
        headers = {"X-Requested-With": "ChatEKLD"}
        # confirm missing → 400 before any scan lookup.
        r = self.client.post(
            "/api/audit/apply-tag-drift",
            json={"citation_key": "smith2020", "tags": ["a"]},
            headers=headers,
        )
        self.assertEqual(r.status_code, 400)
        # Confirmed but no cached scan → deterministic 404 (inventory pinned
        # empty for this test, so the status can't depend on suite ordering).
        with mock.patch.object(
            audit_manager, "get_inventory", return_value=(None, None)
        ):
            r = self.client.post(
                "/api/audit/apply-tag-drift",
                json={"citation_key": "smith2020", "tags": ["a"], "confirm": True},
                headers=headers,
            )
        self.assertEqual(r.status_code, 404)

    def _drift_fixture(self, tmp: Path):
        """A minimal (inventory, settings) pair with one real drifted note."""
        from types import SimpleNamespace
        from audit.core.obsidian import NoteInfo
        from audit.engine.inventory import Record

        vault = tmp / "vault"
        vault.mkdir(parents=True, exist_ok=True)
        note = vault / "smith2020.md"
        note.write_text("---\ntags:\n  - existing\n---\nbody\n", encoding="utf-8")

        rec = Record(citation_key="smith2020")
        rec.obsidian_note = NoteInfo(
            path=note, tags=["existing"], has_frontmatter=True)
        # Zotero note tags via a stub item: drift = {anxiety, sleep}.
        rec.zotero_item = SimpleNamespace(child_notes=[
            SimpleNamespace(tags=["existing", "anxiety", "sleep"]),
        ])
        inv = mock.MagicMock()
        inv.records = {"smith2020": rec}
        settings = SimpleNamespace(vault_root=vault)
        return inv, settings, note

    def test_apply_tag_drift_happy_path_writes_and_backs_up(self):
        """Happy path: drift tags land in the frontmatter, the .bak holds the
        ORIGINAL, and a second apply neither duplicates nor clobbers the .bak."""
        headers = {"X-Requested-With": "ChatEKLD"}
        tmp = Path(tempfile.mkdtemp(prefix="audit-tagdrift-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        inv, settings, note = self._drift_fixture(tmp)
        original = note.read_text(encoding="utf-8")

        with mock.patch.object(
            audit_manager, "get_inventory", return_value=(inv, settings)
        ):
            r = self.client.post(
                "/api/audit/apply-tag-drift",
                json={"citation_key": "smith2020",
                      "tags": ["anxiety", "not-in-drift"], "confirm": True},
                headers=headers,
            )
            self.assertEqual(r.status_code, 200, r.get_json())
            d = r.get_json()
            self.assertEqual(d["added"], ["anxiety"])   # drift-validated
            self.assertEqual(d["still_missing"], ["sleep"])
            import yaml
            fm = yaml.safe_load(note.read_text(encoding="utf-8").split("---")[1])
            self.assertEqual(fm["tags"], ["existing", "anxiety"])
            bak = Path(str(note) + ".bak")
            self.assertEqual(bak.read_text(encoding="utf-8"), original)

            # Second apply: the .bak must still hold the ORIGINAL (non-clobber).
            r = self.client.post(
                "/api/audit/apply-tag-drift",
                json={"citation_key": "smith2020", "tags": ["sleep"],
                      "confirm": True},
                headers=headers,
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(bak.read_text(encoding="utf-8"), original)

    def test_apply_tag_drift_rejects_out_of_drift_tags(self):
        headers = {"X-Requested-With": "ChatEKLD"}
        tmp = Path(tempfile.mkdtemp(prefix="audit-tagdrift-rej-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        inv, settings, note = self._drift_fixture(tmp)
        before = note.read_text(encoding="utf-8")
        with mock.patch.object(
            audit_manager, "get_inventory", return_value=(inv, settings)
        ):
            r = self.client.post(
                "/api/audit/apply-tag-drift",
                json={"citation_key": "smith2020",
                      "tags": ["injected-tag"], "confirm": True},
                headers=headers,
            )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(note.read_text(encoding="utf-8"), before)  # untouched

    def test_apply_tag_drift_refuses_variant_spellings(self):
        """Plan 2026-08-05/2 regression: a tag the note already carries under
        another spelling is NOT drift — the old ``.lower()`` intersection let a
        case-variant through, the writer's dedup then no-opped it ("Tags
        already present"), and the report row could never be cleared. The
        fingerprint-keyed intersection refuses it up front (400, no write)."""
        headers = {"X-Requested-With": "ChatEKLD"}
        tmp = Path(tempfile.mkdtemp(prefix="audit-tagdrift-var-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        inv, settings, note = self._drift_fixture(tmp)
        # The note now carries "Anxiety" — a case variant of Zotero's
        # "anxiety" — so only "sleep" is genuinely missing.
        rec = inv.records["smith2020"]
        rec.obsidian_note.tags = ["existing", "Anxiety"]
        before = note.read_text(encoding="utf-8")
        with mock.patch.object(
            audit_manager, "get_inventory", return_value=(inv, settings)
        ):
            r = self.client.post(
                "/api/audit/apply-tag-drift",
                json={"citation_key": "smith2020",
                      "tags": ["anxiety"], "confirm": True},
                headers=headers,
            )
            self.assertEqual(r.status_code, 400)
            self.assertEqual(note.read_text(encoding="utf-8"), before)
            # A fingerprint-variant REQUEST for a genuinely-missing tag still
            # lands, written with the drift set's (Zotero's) spelling.
            r = self.client.post(
                "/api/audit/apply-tag-drift",
                json={"citation_key": "smith2020",
                      "tags": ["SLEEP"], "confirm": True},
                headers=headers,
            )
            self.assertEqual(r.status_code, 200, r.get_json())
            d = r.get_json()
            self.assertEqual(d["added"], ["sleep"])
            self.assertEqual(d["still_missing"], [])


class TestAuditConfigValidation(unittest.TestCase):
    """``POST /api/audit/config`` rejects path-traversal and absolute
    paths in the vault-relative fields."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-test-validation-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.tmp / "config.json").write_text(
            json.dumps({"obsidian_vault_path": str(self.vault)}),
            encoding="utf-8",
        )
        cfg_path = str(self.tmp / "config.json")
        p1 = mock.patch("core.constants.CONFIG_FILE", cfg_path)
        p2 = mock.patch("core.config.CONFIG_FILE", cfg_path)
        p3 = mock.patch("core.constants.BASE_DIR", str(self.tmp))
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

        app = _build_audit_only_app()
        self.client = app.test_client()
        self.headers = {
            "X-Requested-With": "ChatEKLD",
            "Content-Type": "application/json",
        }

    def _post_config(self, body):
        return self.client.post(
            "/api/audit/config", json=body, headers=self.headers
        )

    def test_rejects_path_traversal_in_subdir(self):
        r = self._post_config({"audit_attachments_subdir": "../escape"})
        self.assertEqual(r.status_code, 400)

    def test_rejects_absolute_path_in_subdir(self):
        r = self._post_config({"audit_attachments_subdir": "/etc/passwd"})
        self.assertEqual(r.status_code, 400)

    def test_rejects_huge_threshold(self):
        r = self._post_config({"audit_annotations_read_threshold": 10**12})
        # coerce_int_in_range clamps rather than rejecting; confirm the
        # value lands inside [0, 10_000].
        self.assertEqual(r.status_code, 200)
        cfg = self.client.get("/api/audit/config", headers=self.headers).get_json()
        self.assertLessEqual(cfg["audit_annotations_read_threshold"], 10_000)

    def test_rejects_non_finite_threshold(self):
        r = self._post_config({"audit_annotations_read_threshold": "nan"})
        self.assertEqual(r.status_code, 400)

    def test_accepts_valid_relative_subdir(self):
        r = self._post_config({"audit_attachments_subdir": "MyDocs"})
        self.assertEqual(r.status_code, 200)
        cfg = self.client.get("/api/audit/config", headers=self.headers).get_json()
        self.assertEqual(cfg["audit_attachments_subdir"], "MyDocs")

    def test_accepts_accented_relative_subdir(self):
        """B3 (2026-07-13): accented French paths are valid vault paths and must
        no longer be rejected by an ASCII-only charset gate."""
        r = self._post_config({"audit_attachments_subdir": "Études/Dépression (2020)"})
        self.assertEqual(r.status_code, 200)
        cfg = self.client.get("/api/audit/config", headers=self.headers).get_json()
        self.assertEqual(cfg["audit_attachments_subdir"], "Études/Dépression (2020)")

    def test_dup_decision_verbs_and_validation(self):
        """Plan 2026-08-05/5: dismiss/clear round-trip; clear checked first;
        garbage bodies 400."""
        import audit.engine.dup_decisions as dd
        with mock.patch.object(
            dd, "decisions_path", lambda: self.tmp / "dup_decisions.json"
        ):
            post = lambda body: self.client.post(  # noqa: E731
                "/api/audit/dup-decision", json=body, headers=self.headers)
            self.assertEqual(post({}).status_code, 400)
            self.assertEqual(post({"pair_id": "k:a+b"}).status_code, 400)
            r = post({"pair_id": "k:a+b", "dismiss": True})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["changed"])
            self.assertEqual(dd.load_dismissed(), {"k:a+b"})
            # An undo body carrying dismiss too must be read as CLEAR.
            r = post({"pair_id": "k:a+b", "clear": True, "dismiss": True})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["cleared"])
            self.assertEqual(dd.load_dismissed(), set())

    def test_pdf_decision_verbs_and_validation(self):
        """Batch B: dismiss/clear/clear_all round-trip; undo verbs checked
        first; garbage bodies 400 without touching the sidecar."""
        import audit.engine.pdf_wanted as pw
        with mock.patch.object(
            pw, "decisions_path", lambda: self.tmp / "pdf_wanted.json"
        ):
            post = lambda body: self.client.post(  # noqa: E731
                "/api/audit/pdf-decision", json=body, headers=self.headers)
            self.assertEqual(post({}).status_code, 400)
            self.assertEqual(post({"citation_keys": []}).status_code, 400)
            self.assertEqual(post({"citation_keys": "a2020"}).status_code, 400)
            # A verb is required: a well-formed key list alone does nothing.
            self.assertEqual(post({"citation_keys": ["a2020"]}).status_code, 400)
            self.assertEqual(pw.load_dismissed(), set())

            r = post({"citation_keys": ["a2020", "b2021"], "dismiss": True})
            self.assertEqual(r.status_code, 200, r.get_json())
            self.assertEqual(r.get_json()["changed"], 2)
            self.assertEqual(pw.load_dismissed(), {"a2020", "b2021"})

            # An undo body carrying dismiss too must be read as CLEAR — the
            # mapping/dup-decision precedent.
            r = post({"citation_keys": ["a2020"], "clear": True, "dismiss": True})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["cleared"])
            self.assertEqual(pw.load_dismissed(), {"b2021"})

            # clear_all is the report's "show them again": it needs no keys,
            # because the report ships the COUNT of what it hid, not the keys.
            r = post({"clear_all": True})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["changed"], 1)
            self.assertEqual(pw.load_dismissed(), set())

    def test_pdf_decision_rejects_control_characters_and_oversized_batches(self):
        import audit.engine.pdf_wanted as pw
        with mock.patch.object(
            pw, "decisions_path", lambda: self.tmp / "pdf_wanted.json"
        ):
            post = lambda body: self.client.post(  # noqa: E731
                "/api/audit/pdf-decision", json=body, headers=self.headers)
            self.assertEqual(
                post({"citation_keys": ["a\x00b"], "dismiss": True}).status_code, 400)
            self.assertEqual(
                post({"citation_keys": ["a\nb"], "dismiss": True}).status_code, 400)
            from api.routes.audit import _PDF_DECISION_MAX_KEYS
            too_many = [f"k{i}" for i in range(_PDF_DECISION_MAX_KEYS + 1)]
            self.assertEqual(
                post({"citation_keys": too_many, "dismiss": True}).status_code, 400)
            self.assertEqual(pw.load_dismissed(), set())

    def test_no_pdf_types_config_round_trip_and_validation(self):
        """The bulk-dismiss offer is driven by a user-editable item-type list;
        a typo must 400 rather than silently stop offering a whole class."""
        r = self._post_config({"audit_no_pdf_types": "webpage, blogPost ,webpage"})
        self.assertEqual(r.status_code, 200, r.get_json())
        cfg = self.client.get("/api/audit/config", headers=self.headers).get_json()
        # Trimmed, deduped, order preserved.
        self.assertEqual(cfg["audit_no_pdf_types"], "webpage,blogPost")
        # Empty is valid and means "offer no bulk dismissals".
        self.assertEqual(self._post_config({"audit_no_pdf_types": ""}).status_code, 200)
        for bad in ("web page", "web-page", "9webpage", "x" * 3000):
            self.assertEqual(
                self._post_config({"audit_no_pdf_types": bad}).status_code, 400, bad)

    def test_build_pdf_text_provider(self):
        """Plan 2026-08-05/4 (F4.5): the route-built provider is None without
        a vault path, a working cache-first callable with one, and swallows
        extraction errors into None (text is advisory, never fatal)."""
        from api.routes.audit import _build_pdf_text_provider

        with mock.patch("api.routes.audit.load_config",
                        return_value={"obsidian_vault_path": ""}):
            self.assertIsNone(_build_pdf_text_provider())

        with mock.patch("api.routes.audit.load_config",
                        return_value={"obsidian_vault_path": str(self.vault)}):
            provider = _build_pdf_text_provider()
        self.assertIsNotNone(provider)
        with mock.patch("tagging.text_source.read_pdf_text",
                        return_value=("page one text", False)) as rpt:
            self.assertEqual(provider(Path("/x/a.pdf")), "page one text")
        # fresh extraction bounded to the FIRST page — the whole point of
        # the seam being cheap enough to run per scan.
        self.assertEqual(rpt.call_args.kwargs.get("fresh_max_pages"), 1)
        with mock.patch("tagging.text_source.read_pdf_text",
                        side_effect=RuntimeError("boom")):
            self.assertIsNone(provider(Path("/x/a.pdf")))
        with mock.patch("tagging.text_source.read_pdf_text",
                        return_value=("", False)):
            self.assertIsNone(provider(Path("/x/a.pdf")))  # empty → None

    def test_near_duplicates_report_404_when_skipped(self):
        with mock.patch.object(
            audit_manager, "get_inventory",
            return_value=(mock.MagicMock(), mock.MagicMock()),
        ), mock.patch.object(
            audit_manager, "get_near_duplicates", return_value=(None, None)
        ):
            r = self.client.get(
                "/api/audit/reports/near_duplicates", headers=self.headers)
        self.assertEqual(r.status_code, 404)
        self.assertIn("skipped", r.get_json()["error"])

    def test_apply_tag_drift_synonym_offered_surface_is_applyable(self):
        """2026-08-05 verification HIGH finding: the drift map's keys are
        synonym-canonicalised, so the route's lookup of the REQUESTED tag must
        canonicalise too — a plain tag_key lookup 400'd the very surface the
        report offered, forever (the unresolvable-row loop, synonym edition).
        """
        from types import SimpleNamespace
        from audit.core.obsidian import NoteInfo
        from audit.engine.inventory import Record
        from audit.engine.normalize import tag_key

        vault = self.vault
        note = vault / "smith2020.md"
        note.write_text("---\ntags:\n  - existing\n---\nbody\n", encoding="utf-8")
        rec = Record(citation_key="smith2020")
        rec.obsidian_note = NoteInfo(path=note, tags=["existing"],
                                     has_frontmatter=True)
        rec.zotero_item = SimpleNamespace(child_notes=[
            SimpleNamespace(tags=["existing", "sleep"])])
        inv = mock.MagicMock()
        inv.records = {"smith2020": rec}
        settings = SimpleNamespace(vault_root=vault)
        # _tags.md maps the phrase "sleep" to the canonical tag "sommeil" —
        # the drift map is keyed on the CANONICAL key.
        synonyms = {tag_key("sleep"): tag_key("sommeil")}
        with mock.patch.object(audit_manager, "get_inventory",
                               return_value=(inv, settings)), \
             mock.patch("api.routes.audit._drift_synonyms",
                        return_value=synonyms):
            r = self.client.post(
                "/api/audit/apply-tag-drift",
                json={"citation_key": "smith2020", "tags": ["sleep"],
                      "confirm": True},
                headers=self.headers,
            )
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["added"], ["sleep"])
        self.assertEqual(d["still_missing"], [])  # canonical-keyed too

    def test_zotero_attachments_report_serves_the_delink_facts(self):
        """The report exists to answer one question — "what does Zotero hold a
        file for, and what would removing it cost?" — so the two facts a
        removal turns on must reach the client: which side owns the bytes, and
        how many annotations ride along."""
        from types import SimpleNamespace
        from audit.engine.inventory import ZoteroAttachmentRow

        inv = mock.MagicMock()
        inv.records = {}
        inv.zotero_attachments = [
            ZoteroAttachmentRow(
                item_key="ATT1", parent_key="P1", parent_title="A paper",
                parent_citation_key="smith2020", parent_item_type="journalArticle",
                link_mode="imported_file", content_type="application/pdf",
                filename="smith.pdf", annotation_count=4,
                file_owned_by_zotero=True, library_type="user"),
            ZoteroAttachmentRow(
                item_key="ATT2", parent_key="P2", parent_citation_key="jones2019",
                link_mode="linked_file", content_type="application/pdf",
                filename="jones.pdf", file_owned_by_zotero=False),
        ]
        settings = SimpleNamespace(vault_root=self.vault)
        with mock.patch.object(audit_manager, "get_inventory",
                               return_value=(inv, settings)):
            r = self.client.get("/api/audit/reports/zotero_attachments",
                                headers=self.headers)
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["count"], 2)
        self.assertEqual(d["owned"], 1)
        self.assertEqual(d["with_annotations"], 1)
        rows = {row["item_key"]: row for row in d["rows"]}
        self.assertEqual(rows["ATT1"]["annotation_count"], 4)
        self.assertTrue(rows["ATT1"]["file_owned_by_zotero"])
        self.assertFalse(rows["ATT2"]["file_owned_by_zotero"])

    def test_zotero_attachments_report_is_read_only(self):
        """The verb on its rows posts to /api/zotlink/*, outside the vendored
        tree — the audit blueprint must expose no write for it."""
        from api.routes import audit as audit_routes
        self.assertIn("zotero_attachments", audit_routes._REPORT_NAMES)

    def test_finder_tag_drift_report_carries_fix_fields(self):
        """Drift-fix (2026-08-06): the report ships ``variants`` /
        ``note_rel`` / ``vocab_ids`` — each drift surface resolved to the
        exact ``_tags.md`` id via the audit fingerprint (separator-folding),
        so the UI POSTs ids the tagging writers' literal-id gate accepts;
        an out-of-vocab surface maps to ``null`` (badged, not applyable)."""
        from types import SimpleNamespace
        from audit.core.obsidian import NoteInfo
        from audit.engine.inventory import Record

        vault = self.vault
        (vault / "_tags.md").write_text(
            "| Description | Tags |\n|---|---|\n"
            "| Sleep quality | #sleep-quality |\n",
            encoding="utf-8")
        note = vault / "smith2020.md"
        note.write_text("---\ntags: [cognition]\n---\n", encoding="utf-8")
        rec = Record(citation_key="smith2020")
        rec.obsidian_note = NoteInfo(path=note, tags=["cognition"],
                                     has_frontmatter=True)
        rec.finder_tag_set = {"sleep quality", "brandnew"}
        rec.pdf_paths = [vault / "a.pdf"]
        inv = mock.MagicMock()
        inv.records = {"smith2020": rec}
        settings = SimpleNamespace(vault_root=vault)
        with mock.patch.object(audit_manager, "get_inventory",
                               return_value=(inv, settings)):
            r = self.client.get("/api/audit/reports/finder_tag_drift",
                                headers=self.headers)
        self.assertEqual(r.status_code, 200, r.get_json())
        row = r.get_json()["rows"][0]
        self.assertEqual(row["note_rel"], "smith2020.md")
        self.assertEqual(row["vocab_ids"]["sleep quality"], "sleep-quality")
        self.assertIsNone(row["vocab_ids"]["brandnew"])
        self.assertIsNone(row["vocab_ids"]["cognition"])
        self.assertEqual(row["variants"], [])

    def test_near_dup_ratio_is_range_validated(self):
        """Plan 2026-08-05/6: the similarity cutoff clamps into [60, 100]
        (coerce_int_in_range semantics, like the annotations threshold) and
        rejects garbage."""
        r = self._post_config({"audit_near_dup_ratio": "nan"})
        self.assertEqual(r.status_code, 400)
        r = self._post_config({"audit_near_dup_ratio": 92})
        self.assertEqual(r.status_code, 200)
        cfg = self.client.get("/api/audit/config", headers=self.headers).get_json()
        self.assertEqual(cfg["audit_near_dup_ratio"], 92)
        r = self._post_config({"audit_near_dup_ratio": 10**9})
        self.assertEqual(r.status_code, 200)
        cfg = self.client.get("/api/audit/config", headers=self.headers).get_json()
        self.assertLessEqual(cfg["audit_near_dup_ratio"], 100)

    def test_drift_parent_tags_flag_is_bool_validated(self):
        """Plan 2026-08-05/3: the parent-tags toggle is a strict bool — a
        garbage value 400s (never stored), a valid one round-trips."""
        r = self._post_config({"audit_drift_include_parent_tags": "banana"})
        self.assertEqual(r.status_code, 400)
        r = self._post_config({"audit_drift_include_parent_tags": True})
        self.assertEqual(r.status_code, 200)
        cfg = self.client.get("/api/audit/config", headers=self.headers).get_json()
        self.assertIs(cfg["audit_drift_include_parent_tags"], True)

    def test_still_rejects_control_chars_and_traversal(self):
        """B3: the denylist still rejects NUL/newline and ``..`` traversal."""
        self.assertEqual(self._post_config({"audit_attachments_subdir": "a\x00b"}).status_code, 400)
        self.assertEqual(self._post_config({"audit_attachments_subdir": "a\nb"}).status_code, 400)
        self.assertEqual(self._post_config({"audit_attachments_subdir": "../escape/x"}).status_code, 400)

    def test_rejects_relative_zotero_path(self):
        """Zotero sqlite/storage must be absolute — otherwise
        ``Path(...).resolve()`` anchors to the server CWD and widens
        the allowed roots that bound ``/api/audit/reveal``."""
        r = self._post_config({"audit_zotero_sqlite": "relative/path.sqlite"})
        self.assertEqual(r.status_code, 400)

    def test_accepts_tilde_expanded_absolute_zotero_path(self):
        r = self._post_config({"audit_zotero_sqlite": "~/Zotero/zotero.sqlite"})
        self.assertEqual(r.status_code, 200)

    def test_rejects_relative_zotero_storage_path(self):
        r = self._post_config({"audit_zotero_storage": "relative/storage"})
        self.assertEqual(r.status_code, 400)

    def test_scan_rejects_non_object_body(self):
        """The /api/audit/scan handler must not 500 when the client
        sends a non-object JSON payload (list, number, string, null)."""
        r = self.client.post(
            "/api/audit/scan",
            data=json.dumps([1, 2, 3]),
            headers=self.headers,
        )
        # Empty body would 400 (no vault) — for a list we still want a
        # graceful 4xx, not a 500 AttributeError.
        self.assertLess(r.status_code, 500)


class TestAuditRevealPathBounds(unittest.TestCase):
    """`/api/audit/reveal` enforces configured-root bounds before shelling out."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="audit-test-reveal-")
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.zotero_storage = self.tmp / "zotero-storage"
        self.zotero_storage.mkdir()
        self.allowed = self.vault / "inside.txt"
        self.allowed.write_text("ok", encoding="utf-8")
        self.outside = self.tmp / "outside.txt"
        self.outside.write_text("nope", encoding="utf-8")
        (self.tmp / "config.json").write_text(
            json.dumps({
                "obsidian_vault_path": str(self.vault),
                "audit_zotero_storage": str(self.zotero_storage),
            }),
            encoding="utf-8",
        )
        cfg_path = str(self.tmp / "config.json")
        p1 = mock.patch("core.constants.CONFIG_FILE", cfg_path)
        p2 = mock.patch("core.config.CONFIG_FILE", cfg_path)
        p3 = mock.patch("core.constants.BASE_DIR", str(self.tmp))
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

        app = _build_audit_only_app()
        self.client = app.test_client()
        self.headers = {
            "X-Requested-With": "ChatEKLD",
            "Content-Type": "application/json",
        }
        self.platform_patch = mock.patch("api.routes.audit.sys.platform", "darwin")
        self.platform_patch.start()
        self.addCleanup(self.platform_patch.stop)

    @mock.patch("api.routes.audit.run_open")
    def test_rejects_path_outside_allowed_roots(self, popen_mock):
        r = self.client.post(
            "/api/audit/reveal",
            json={"open": str(self.outside)},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("outside the configured roots", r.get_json().get("error", ""))
        popen_mock.assert_not_called()

    @mock.patch("api.routes.audit.run_open")
    def test_allows_path_inside_vault_root(self, popen_mock):
        r = self.client.post(
            "/api/audit/reveal",
            json={"open": str(self.allowed)},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        popen_mock.assert_called_once()

    @mock.patch("api.routes.audit.run_open")
    def test_zotero_reveal_routes_group_items_to_the_group_library(self, popen_mock):
        """2026-08-18: the personal-library deep link selects NOTHING for a
        group-library item, so an orphan row carrying a group_id must produce
        the groups/<id> form. Without the field, the user form is kept."""
        r = self.client.post(
            "/api/audit/reveal",
            json={"zotero_key": "ABCD1234", "zotero_group_id": 484650},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        url = popen_mock.call_args.args[0][1]
        self.assertEqual(url, "zotero://select/groups/484650/items/ABCD1234")
        popen_mock.reset_mock()
        r = self.client.post(
            "/api/audit/reveal",
            json={"zotero_key": "ABCD1234"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        url = popen_mock.call_args.args[0][1]
        self.assertEqual(url, "zotero://select/library/items/ABCD1234")
        # A malformed group id degrades to the user form, never a 500.
        popen_mock.reset_mock()
        r = self.client.post(
            "/api/audit/reveal",
            json={"zotero_key": "ABCD1234", "zotero_group_id": "nope"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("library/items", popen_mock.call_args.args[0][1])

    @mock.patch("api.routes.audit.run_open")
    def test_open_refuses_non_document_types_failclosed(self, popen_mock):
        """Finding 7: the launch check is now a fail-closed ALLOWLIST — an
        extension-less binary or an unlisted script type inside the vault is
        refused for `open` (LaunchServices would run it), while `reveal` is safe.
        """
        # An extension-less file (Mach-O/script) the old denylist would MISS.
        binary = self.vault / "helper"          # no extension
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        fish = self.vault / "run.fish"           # not in the old denylist
        fish.write_text("echo hi", encoding="utf-8")
        for target in (binary, fish):
            r = self.client.post(
                "/api/audit/reveal", json={"open": str(target)}, headers=self.headers)
            self.assertEqual(r.status_code, 400, target)
            self.assertIn("Refusing", r.get_json().get("error", ""))
        popen_mock.assert_not_called()
        # …but reveal (-R, highlight only) is fine for the same files.
        r = self.client.post(
            "/api/audit/reveal", json={"path": str(binary)}, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        popen_mock.assert_called_once()

    @mock.patch("api.routes.audit.run_open")
    def test_open_allows_a_pdf(self, popen_mock):
        pdf = self.vault / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        r = self.client.post(
            "/api/audit/reveal", json={"open": str(pdf)}, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        popen_mock.assert_called_once()


class TestUnmappedAnnotationPrecompute(unittest.TestCase):
    """A+C: the scan precomputes annotation counts for *unmapped* PDFs into
    ``Inventory.unmapped_annotations`` (in parallel) so the unread/read
    reports serve from memory instead of re-opening every PDF on the
    request thread."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-test-annot-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.biblio = self.vault / "Z_attachments" / "biblio_articles"
        self.biblio.mkdir(parents=True)
        (self.vault / "Z_Zotero_Notes").mkdir()
        # _master.bib at the vault root matches the neutral default path.
        (self.vault / "_master.bib").write_text(
            "@article{smith2020, title={Test}, year={2020}, author={Smith, Jane}}",
            encoding="utf-8",
        )
        # Two stray PDFs the bridge cannot resolve to smith2020 -> unmapped.
        # Contents are irrelevant: read_annotations is monkeypatched below,
        # so these never reach pikepdf.  Names avoid the ``z_item`` skip
        # prefix and carry no author/year the bridge could match on.
        self.pdf_a = self.biblio / "unmapped_alpha.pdf"
        self.pdf_b = self.biblio / "unmapped_beta.pdf"
        self.pdf_a.write_bytes(b"%PDF-1.4 not-a-real-pdf")
        self.pdf_b.write_bytes(b"%PDF-1.4 not-a-real-pdf")

        cfg_path = str(self.tmp / "config.json")
        (self.tmp / "config.json").write_text(
            json.dumps({
                "obsidian_vault_path": str(self.vault),
                # Point Zotero at a path that does not exist so the inventory
                # skips it entirely (keeps the test hermetic regardless of a
                # live Zotero — see the known tests-hang-on-Zotero gotcha).
                "audit_zotero_sqlite": str(self.tmp / "nope.sqlite"),
                "audit_zotero_storage": str(self.tmp / "nope_storage"),
                "audit_annotations_read_threshold": 5,
            }),
            encoding="utf-8",
        )
        for target in ("core.constants.CONFIG_FILE", "core.config.CONFIG_FILE"):
            p = mock.patch(target, cfg_path)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("core.constants.BASE_DIR", str(self.tmp))
        p.start()
        self.addCleanup(p.stop)

    def _patch_reader(self, counts: dict[str, int], seen: list[Path]):
        """Patch read_annotations to a deterministic, disk-free stub that
        records which paths it was asked to read."""
        from audit.core import pdf_annotations

        def fake(path):
            seen.append(path)
            return pdf_annotations.AnnotationsResult(counts.get(path.name, 0))

        return mock.patch.object(pdf_annotations, "read_annotations", fake)

    def test_build_inventory_precomputes_unmapped_annotations(self):
        from audit.config import load_settings
        from audit.engine import inventory as eng_inventory

        settings = load_settings()
        seen: list[Path] = []
        with self._patch_reader({"unmapped_alpha.pdf": 0, "unmapped_beta.pdf": 9}, seen):
            inv = eng_inventory.build_inventory(settings, count_annotations=True)

        self.assertEqual(
            set(inv.unmapped_annotations), set(inv.bridge.unmapped_pdfs),
            "every unmapped PDF must have a precomputed annotation result",
        )
        # Compare by filename: load_settings() resolves the vault path, so the
        # keys are the /private/var realpath form on macOS, not the /var
        # symlink form self.pdf_a carries.
        by_name = {p.name: res.count for p, res in inv.unmapped_annotations.items()}
        self.assertEqual(by_name, {"unmapped_alpha.pdf": 0, "unmapped_beta.pdf": 9})
        # Both unmapped PDFs were actually read during the scan.
        self.assertTrue(
            {"unmapped_alpha.pdf", "unmapped_beta.pdf"} <= {p.name for p in seen}
        )

    def test_count_annotations_false_leaves_cache_empty(self):
        from audit.config import load_settings
        from audit.engine import inventory as eng_inventory

        settings = load_settings()
        seen: list[Path] = []
        with self._patch_reader({}, seen):
            inv = eng_inventory.build_inventory(settings, count_annotations=False)

        self.assertEqual(inv.unmapped_annotations, {})
        # No unmapped PDF was read when annotations are opted out.
        self.assertNotIn(self.pdf_a, seen)
        self.assertNotIn(self.pdf_b, seen)


class TestUnzoterodReportsUseCache(unittest.TestCase):
    """The two unzoterod reports consume ``inv.unmapped_annotations`` and do
    not touch disk when the cache covers every unmapped PDF."""

    def test_reports_serve_from_cache_without_disk_reads(self):
        from types import SimpleNamespace

        from audit.core.pdf_annotations import AnnotationsResult
        from audit.engine.reports import (
            read_unzoterod as r_read,
            unread_unzoterod as r_unread,
        )

        p_unread = Path("/vault/Z_attachments/biblio_articles/a.pdf")
        p_read = Path("/vault/Z_attachments/biblio_articles/b.pdf")
        cache = {
            p_unread: AnnotationsResult(1),   # below threshold (5) -> "unread"
            p_read: AnnotationsResult(40),    # well-annotated -> "read"
        }
        inv = SimpleNamespace(
            bridge=SimpleNamespace(
                unmapped_pdfs=[p_unread, p_read],
                ambiguous_pdfs={Path("/vault/Z_attachments/biblio_articles/amb.pdf"): ["k1", "k2"]},
                confirmed_no_match_pdfs=[Path("/vault/Z_attachments/biblio_articles/nm.pdf")],
            ),
            pdfs_skipped=[Path("/vault/Z_attachments/biblio_articles/z_item_x.pdf")],
            unmapped_annotations=cache,
            zotero_error=None,
        )
        settings = SimpleNamespace(annotations_read_threshold=5)

        with mock.patch(
            "audit.core.pdf_annotations.read_annotations",
            side_effect=AssertionError("must not hit disk when cache is complete"),
        ):
            unread = r_unread.find(inv, settings, annotations=inv.unmapped_annotations)
            read = r_read.find(inv, settings, annotations=inv.unmapped_annotations)

        self.assertEqual([r.pdf for r in unread.rows], [p_unread])
        self.assertEqual({r.pdf for r in read.rows}, {p_unread, p_read})
        # read_unzoterod sorts by annotation count desc.
        self.assertEqual([r.pdf for r in read.rows], [p_read, p_unread])
        # ...and it is the SUPERSET: unread_unzoterod is a filtered view of the
        # same source list, never its complement. Pinning the containment
        # because the two tabs were named as if they partitioned the set.
        self.assertTrue({r.pdf for r in unread.rows} <= {r.pdf for r in read.rows})

        # Exclusion disclosure (2026-07-31): the buckets that are NOT in `rows`
        # must still be counted, or the tab silently under-reports what is
        # missing from Zotero. Counts only — never rows.
        self.assertEqual(read.ambiguous_count, 1)
        self.assertEqual(read.confirmed_no_match_count, 1)
        self.assertEqual(read.skipped_count, 1)
        self.assertNotIn(
            Path("/vault/Z_attachments/biblio_articles/nm.pdf"),
            {r.pdf for r in read.rows},
        )


class TestReadAnnotationsParallel(unittest.TestCase):
    """The parallel reader returns a complete map, an empty map for no input,
    and honours cooperative cancel without hanging."""

    def test_reads_all_paths(self):
        from audit.core import pdf_annotations
        from audit.engine.inventory import _read_annotations_parallel

        paths = [Path(f"/x/{i}.pdf") for i in range(20)]

        def fake(path):
            return pdf_annotations.AnnotationsResult(int(path.stem))

        with mock.patch.object(pdf_annotations, "read_annotations", fake):
            out = _read_annotations_parallel(paths)
        self.assertEqual(set(out), set(paths))
        self.assertEqual(out[paths[7]].count, 7)

    def test_empty_input(self):
        from audit.engine.inventory import _read_annotations_parallel

        self.assertEqual(_read_annotations_parallel([]), {})

    def test_cancel_returns_without_hanging(self):
        from audit.core import pdf_annotations
        from audit.engine.inventory import _read_annotations_parallel

        paths = [Path(f"/x/{i}.pdf") for i in range(50)]

        def fake(path):
            return pdf_annotations.AnnotationsResult(0)

        with mock.patch.object(pdf_annotations, "read_annotations", fake):
            out = _read_annotations_parallel(paths, cancel_fn=lambda: True)
        # This is primarily a *hang guard*: if cancel deadlocked the pool
        # (e.g. shutdown(cancel_futures=True) regressed) this test would hang
        # and pytest would time out / never finish.  The len assertion is a
        # weak sanity bound only — with a trivially fast stub the pool may
        # drain all 50 reads before the first as_completed iteration observes
        # the cancel, so we deliberately do NOT assert a strict partial count
        # (that would be timing-flaky).
        self.assertIsInstance(out, dict)
        self.assertLessEqual(len(out), len(paths))


class TestAuditSearch(unittest.TestCase):
    """GET /api/audit/search — the cross-report free-text index."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-test-search-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.tmp / "config.json").write_text(
            json.dumps({"obsidian_vault_path": str(self.vault)}), encoding="utf-8",
        )
        cfg_path = str(self.tmp / "config.json")
        for target in ("core.constants.CONFIG_FILE", "core.config.CONFIG_FILE"):
            p = mock.patch(target, cfg_path)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("core.constants.BASE_DIR", str(self.tmp))
        p.start()
        self.addCleanup(p.stop)
        # The Library half of the search corpus builds off-thread (2026-09-17,
        # C2); join it before this test's BASE_DIR patch is undone so it never
        # runs against another test's stores.
        from api.routes import audit as _audit_routes
        join = getattr(_audit_routes, "join_library_search_build", lambda: None)
        join()
        self.addCleanup(join)

        self.client = _build_audit_only_app().test_client()
        self.headers = {"X-Requested-With": "ChatEKLD"}
        # The corpus is memoised per (Inventory, Settings) identity; a stale
        # entry from another test's fixture would mask a rebuild bug here.
        from api.routes.audit import _clear_search_cache
        _clear_search_cache()
        self.addCleanup(_clear_search_cache)

    def _fixture(self):
        """A real (Inventory, Settings) pair spanning three reports.

        Not a MagicMock: build_corpus runs every report builder over it, so the
        inventory has to behave like the real dataclass.
        """
        from types import SimpleNamespace
        from audit.core.bib import BibEntry
        from audit.core.obsidian import NoteInfo
        from audit.engine.bridge import BridgeResult
        from audit.engine.inventory import Inventory, Record

        note = self.vault / "dupont2019.md"
        note.write_text("---\ntags:\n  - insomnie\n---\n", encoding="utf-8")
        pdf = self.vault / "attachments" / "dupont_2019.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4\n")

        rec = Record(citation_key="dupont2019")
        rec.bib_entry = BibEntry(
            citation_key="dupont2019",
            entry_type="article",
            title="Étude sur le sommeil",
            year="2019",
            authors=["Dupont, Marie"],
        )
        rec.obsidian_note = NoteInfo(
            path=note, tags=["insomnie"], has_frontmatter=True)
        rec.pdf_paths = [pdf]
        # A second record that must NOT match the queries below.
        other = Record(citation_key="smith2020")
        other.bib_entry = BibEntry(
            citation_key="smith2020", entry_type="article",
            title="Cardiac outcomes", year="2020", authors=["Smith, John"],
        )

        ambiguous = self.vault / "attachments" / "unclear_2019.pdf"
        ambiguous.write_bytes(b"%PDF-1.4\n")
        bridge = BridgeResult()
        bridge.ambiguous_pdfs = {ambiguous: ["dupont2019", "smith2020"]}

        inv = Inventory(
            records={"dupont2019": rec, "smith2020": other},
            bridge=bridge,
            pdfs_skipped=[],
            zotero_matched_by_title=0,
        )
        settings = SimpleNamespace(
            vault_root=self.vault,
            biblio_skip_prefix="z_item",
            annotations_read_threshold=3,
            biblio_articles_dir=self.vault / 'attachments',
            # Read by the Library half of the corpus (projection scope/join).
            zotero_sqlite=self.tmp / "zotero.sqlite",
            zotero_storage=self.tmp / "storage",
            mapping_file=self.tmp / "mapping.json",
            master_bib=self.vault / "_master.bib",
        )
        return inv, settings

    def _search(self, q):
        # The corpus is now built OFF the request thread (finding: the inline
        # build convoyed keystrokes and could exhaust the WSGI pool). The first
        # request 202s while the background build runs; poll until it lands,
        # mirroring what the JS client does. The fixture corpus is tiny, so this
        # resolves in a few ms.
        import time as _time
        with mock.patch.object(
            audit_manager, "get_inventory", return_value=self._inv_settings
        ), mock.patch.object(
            audit_manager, "get_duplicates", return_value=(None, None)
        ):
            for _ in range(200):
                r = self.client.get(f"/api/audit/search?q={q}", headers=self.headers)
                if r.status_code != 202:
                    return r
                _time.sleep(0.01)
            return r

    def test_requires_xrw_header(self):
        r = self.client.get("/api/audit/search?q=dupont")
        self.assertEqual(r.status_code, 403)

    def test_short_or_missing_query_is_400(self):
        for url in ("/api/audit/search", "/api/audit/search?q=", "/api/audit/search?q=a"):
            r = self.client.get(url, headers=self.headers)
            self.assertEqual(r.status_code, 400, url)

    def test_no_scan_is_404(self):
        with mock.patch.object(
            audit_manager, "get_inventory", return_value=(None, None)
        ):
            r = self.client.get("/api/audit/search?q=dupont", headers=self.headers)
        self.assertEqual(r.status_code, 404)

    def test_warm_search_does_not_rebuild_the_library_documents_view(self):
        """2026-09-17 review, C2: every keystroke rebuilt library.documents +
        library.search_corpus on the request thread (~4 s warm, live)."""
        from accession import library
        self._inv_settings = self._fixture()
        self.assertEqual(self._search("dupont").status_code, 200)
        with mock.patch.object(library, "documents", wraps=library.documents) as spy:
            for q in ("dupont", "etude", "sommeil"):
                self.assertEqual(self._search(q).status_code, 200)
        self.assertEqual(spy.call_count, 0)

    def test_matches_across_reports(self):
        self._inv_settings = self._fixture()
        r = self._search("dupont")
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        reports = {g["report"] for g in d["groups"]}
        # The key appears as an inventory record AND as an ambiguous candidate.
        self.assertIn("inventory", reports)
        self.assertIn("ambiguous_pdfs", reports)
        self.assertGreaterEqual(d["total"], 2)

    def test_accent_and_case_insensitive(self):
        """'etude' must find 'Étude' — the vault is bilingual FR/EN."""
        self._inv_settings = self._fixture()
        for q in ("etude", "ÉTUDE", "Étude"):
            d = self._search(q).get_json()
            inv_rows = [g for g in d["groups"] if g["report"] == "inventory"]
            self.assertTrue(inv_rows, f"{q!r} found nothing")
            self.assertEqual(inv_rows[0]["rows"][0]["citation_key"], "dupont2019")

    def test_all_tokens_must_match(self):
        """Multi-token queries AND, so adding a word narrows the result.

        Note the ambiguous-PDF row legitimately carries BOTH candidates' titles
        in one haystack, so ``etude cardiac`` correctly still matches it — the
        narrowing assertion below uses a tag that only one record has.
        """
        self._inv_settings = self._fixture()
        self.assertGreaterEqual(self._search("etude sommeil").get_json()["total"], 1)
        # A token present nowhere zeroes the whole query.
        self.assertEqual(self._search("etude zebra").get_json()["total"], 0)
        # Narrowing: 'cardiac' alone hits, but not together with a tag that
        # belongs solely to the other record.
        self.assertGreaterEqual(self._search("cardiac").get_json()["total"], 1)
        self.assertEqual(self._search("cardiac insomnie").get_json()["total"], 0)

    def test_matches_tags_and_pdf_paths(self):
        self._inv_settings = self._fixture()
        self.assertGreaterEqual(self._search("insomnie").get_json()["total"], 1)
        self.assertGreaterEqual(self._search("dupont_2019.pdf").get_json()["total"], 1)

    def test_corpus_is_memoised_per_scan_identity(self):
        """A second query reuses the corpus; a new scan object rebuilds it."""
        from audit import search as audit_search
        from api.routes import audit as audit_routes

        self._inv_settings = self._fixture()
        with mock.patch.object(
            audit_search, "build_corpus", wraps=audit_search.build_corpus
        ) as spy:
            self._search("dupont")
            self._search("etude")
            self.assertEqual(spy.call_count, 1)   # same scan → one build
            self._inv_settings = self._fixture()  # a "new scan"
            self._search("dupont")
            self.assertEqual(spy.call_count, 2)
        # The 404 path must drop the cached Inventory reference.
        with mock.patch.object(
            audit_manager, "get_inventory", return_value=(None, None)
        ):
            self.client.get("/api/audit/search?q=dupont", headers=self.headers)
        self.assertIsNone(audit_routes._SEARCH_CACHE)

    def test_first_query_202s_while_the_corpus_builds_off_thread(self):
        """The corpus build must never run on the request thread (convoy fix).

        A slow build (a count_annotations=off scan falling back to per-PDF
        reads) previously blocked the request under a lock, so every debounced
        keystroke queued behind it and could exhaust the WSGI pool. Now the
        build is kicked to a background thread and the route 202s until ready.
        """
        from api.routes import audit as audit_routes
        import threading as _th

        audit_routes._clear_search_cache()
        with audit_routes._SEARCH_CACHE_LOCK:
            audit_routes._SEARCH_BUILDING = False
            audit_routes._SEARCH_BUILD_ERROR = None
        self._inv_settings = self._fixture()

        # Make the build block until we release it, so we can observe the 202.
        gate = _th.Event()
        real_build = audit_routes.audit_search.build_corpus

        def _slow_build(*a, **k):
            gate.wait(2.0)
            return real_build(*a, **k)

        with mock.patch.object(
            audit_manager, "get_inventory", return_value=self._inv_settings
        ), mock.patch.object(
            audit_manager, "get_duplicates", return_value=(None, None)
        ), mock.patch.object(audit_routes.audit_search, "build_corpus", _slow_build):
            first = self.client.get("/api/audit/search?q=dupont", headers=self.headers)
            self.assertEqual(first.status_code, 202, first.get_json())
            self.assertTrue(first.get_json().get("building"))
            # A concurrent keystroke also 202s (does NOT block on the build).
            second = self.client.get("/api/audit/search?q=dupont", headers=self.headers)
            self.assertEqual(second.status_code, 202)
            gate.set()  # let the build finish; a retry now lands 200.
            r = self._search("dupont")
            self.assertEqual(r.status_code, 200, r.get_json())

    def test_result_listener_drops_cache_on_scan_replacement(self):
        """A replaced scan result must release the memoised corpus promptly.

        The manager fires its result listeners on replace/clear; the route
        registers _clear_search_cache, so a superseded Inventory is not pinned
        in the memo until the next search (finding 4).
        """
        from api.routes import audit as audit_routes

        self._inv_settings = self._fixture()
        self._search("dupont")
        self.assertIsNotNone(audit_routes._SEARCH_CACHE)
        # Simulate a new scan replacing the cached result.
        audit_manager._fire_result_listeners()
        self.assertIsNone(audit_routes._SEARCH_CACHE)

    def test_group_reports_true_count_when_capped(self):
        """A capped group still reports how many it actually matched."""
        from audit import search as audit_search

        corpus = [
            audit_search._Entry("inventory", audit_search.fold(f"paper {i}"),
                                {"label": f"paper {i}"})
            for i in range(audit_search.MAX_ROWS_PER_GROUP + 25)
        ]
        out = audit_search.search(corpus, "paper")
        g = out["groups"][0]
        self.assertEqual(len(g["rows"]), audit_search.MAX_ROWS_PER_GROUP)
        self.assertEqual(g["count"], audit_search.MAX_ROWS_PER_GROUP + 25)
        self.assertTrue(g["truncated"])
        self.assertTrue(out["truncated"])
        self.assertEqual(out["total"], audit_search.MAX_ROWS_PER_GROUP + 25)


class TestClearCuration(unittest.TestCase):
    """bridge.clear_curation — the ambiguous-PDF triage deck's undo primitive.

    The two existing writers can only REPLACE a decision with the other kind;
    only this one returns a PDF to its un-curated state so the next scan
    re-derives it.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-clear-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        (self.vault / "att").mkdir(parents=True)
        self.pdf = self.vault / "att" / "smith_2020.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\n")
        self.mapping = self.tmp / "mapping.json"

    def _state(self):
        from audit.engine.bridge import _load_mapping_full
        return _load_mapping_full(self.mapping)

    def test_clears_a_match_and_prunes_its_hash(self):
        from audit.engine import bridge
        bridge.add_match(self.mapping, self.pdf, "smith2020", self.vault)
        matches, _no_match, hashes, _sources = self._state()
        self.assertIn("smith2020", matches)
        self.assertTrue(hashes)   # anchored on write

        self.assertTrue(bridge.clear_curation(self.mapping, self.pdf, self.vault))
        matches, no_match, hashes, _sources = self._state()
        # The key's last path went, so the key goes with it.
        self.assertNotIn("smith2020", matches)
        self.assertNotIn("att/smith_2020.pdf", no_match)
        # save_mapping prunes hash entries no longer referenced.
        self.assertNotIn("att/smith_2020.pdf", hashes)

    def test_clears_a_no_match(self):
        from audit.engine import bridge
        bridge.add_no_match(self.mapping, self.pdf, self.vault)
        self.assertIn("att/smith_2020.pdf", self._state()[1])
        self.assertTrue(bridge.clear_curation(self.mapping, self.pdf, self.vault))
        self.assertNotIn("att/smith_2020.pdf", self._state()[1])

    def test_keeps_sibling_paths_under_the_same_key(self):
        """Clearing one PDF must not un-curate the key's other copies."""
        from audit.engine import bridge
        other = self.vault / "att" / "smith_2020_copy.pdf"
        other.write_bytes(b"%PDF-1.4\n")
        bridge.add_match(self.mapping, self.pdf, "smith2020", self.vault)
        bridge.add_match(self.mapping, other, "smith2020", self.vault)

        bridge.clear_curation(self.mapping, self.pdf, self.vault)
        matches = self._state()[0]
        self.assertEqual(matches["smith2020"], ["att/smith_2020_copy.pdf"])

    def test_is_idempotent_on_an_uncurated_pdf(self):
        from audit.engine import bridge
        self.assertFalse(bridge.clear_curation(self.mapping, self.pdf, self.vault))
        bridge.add_match(self.mapping, self.pdf, "smith2020", self.vault)
        self.assertTrue(bridge.clear_curation(self.mapping, self.pdf, self.vault))
        self.assertFalse(bridge.clear_curation(self.mapping, self.pdf, self.vault))


class TestAuditPdfAndClearRoutes(unittest.TestCase):
    """GET /api/audit/pdf and the mapping route's `clear` action."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-pdfroute-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        (self.vault / "att").mkdir(parents=True)
        self.pdf = self.vault / "att" / "paper.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\nfake\n")
        (self.vault / "_master.bib").write_text(
            "@article{smith2020, title={Current entry}}\n", encoding="utf-8")
        (self.tmp / "config.json").write_text(
            json.dumps({"obsidian_vault_path": str(self.vault),
                        "audit_attachments_subdir": "att",
                        "audit_biblio_articles_subdir": "."}), encoding="utf-8",
        )
        cfg_path = str(self.tmp / "config.json")
        for target in ("core.constants.CONFIG_FILE", "core.config.CONFIG_FILE"):
            p = mock.patch(target, cfg_path)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("core.constants.BASE_DIR", str(self.tmp))
        p.start()
        self.addCleanup(p.stop)
        self.client = _build_audit_only_app().test_client()
        self.headers = {"X-Requested-With": "ChatEKLD"}

    def _mapping_bytes(self):
        from audit.config import load_settings
        path = load_settings().mapping_file
        return path.read_bytes() if path.exists() else None

    def test_requires_xrw_header(self):
        self.assertEqual(self.client.get("/api/audit/pdf?rel=att/paper.pdf").status_code, 403)

    def test_serves_pdf_bytes(self):
        r = self.client.get("/api/audit/pdf?rel=att/paper.pdf", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertTrue(r.data.startswith(b"%PDF"))

    def test_rejects_traversal_and_absolute_paths(self):
        for rel in ("../secret.pdf", "att/../../secret.pdf", "/etc/passwd",
                    "att/paper.pdf\x00.png", ""):
            r = self.client.get(
                f"/api/audit/pdf?rel={rel}", headers=self.headers)
            self.assertEqual(r.status_code, 400, rel)

    def test_rejects_non_pdf_and_missing(self):
        (self.vault / "att" / "notes.md").write_text("x", encoding="utf-8")
        for rel in ("att/notes.md", "att/nope.pdf"):
            r = self.client.get(f"/api/audit/pdf?rel={rel}", headers=self.headers)
            self.assertEqual(r.status_code, 404, rel)

    def test_rejects_a_symlink_escaping_the_vault(self):
        """The realpath re-check is what guards a symlink planted post-scan."""
        outside = self.tmp / "outside.pdf"
        outside.write_bytes(b"%PDF-1.4\nsecret\n")
        link = self.vault / "att" / "link.pdf"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        r = self.client.get("/api/audit/pdf?rel=att/link.pdf", headers=self.headers)
        self.assertEqual(r.status_code, 400)
        self.assertNotIn(b"secret", r.data)

    def test_mapping_clear_action_round_trips(self):
        from audit.engine import bridge
        from audit.config import load_settings
        settings = load_settings()
        r = self.client.post(
            "/api/audit/mapping",
            json={"pdf": "att/paper.pdf", "citation_key": "smith2020",
                  "confirm": True},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIn("smith2020", bridge._load_mapping_full(settings.mapping_file)[0])

        r = self.client.post(
            "/api/audit/mapping",
            json={"pdf": "att/paper.pdf", "clear": True, "confirm": True},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["action"], "clear")
        self.assertTrue(d["changed"])
        self.assertNotIn("smith2020", bridge._load_mapping_full(settings.mapping_file)[0])

    def test_conflicting_clear_and_key_is_rejected_without_mutation(self):
        """Separate action schemas prevent a stale UI field changing intent."""
        from audit.engine import bridge
        from audit.config import load_settings
        settings = load_settings()
        before = settings.mapping_file.read_bytes() \
            if settings.mapping_file.exists() else None
        r = self.client.post(
            "/api/audit/mapping",
            json={"pdf": "att/paper.pdf", "citation_key": "smith2020",
                  "clear": True, "confirm": True},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(bridge._load_mapping_full(settings.mapping_file)[0], {})
        after = settings.mapping_file.read_bytes() \
            if settings.mapping_file.exists() else None
        self.assertEqual(after, before)

    def test_mapping_source_annotation_is_allowlisted_and_persisted(self):
        """`source: "accession"` marks an accession-made link (2026-08-19);
        any other label is refused — an arbitrary client string must never
        become a scan-visible provenance label."""
        from audit.engine import bridge
        from audit.config import load_settings
        settings = load_settings()
        r = self.client.post(
            "/api/audit/mapping",
            json={"pdf": "att/paper.pdf", "citation_key": "smith2020",
                  "source": "accession", "confirm": True},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.get_json())
        sources = bridge._load_mapping_full(settings.mapping_file)[3]
        self.assertEqual(sources, {"att/paper.pdf": "accession"})

        r = self.client.post(
            "/api/audit/mapping",
            json={"pdf": "att/paper.pdf", "citation_key": "smith2020",
                  "source": "evil", "confirm": True},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 400)

    def test_mapping_requires_confirmation_without_touching_the_file(self):
        before = self._mapping_bytes()
        r = self.client.post(
            "/api/audit/mapping", headers=self.headers,
            json={"pdf": "att/paper.pdf", "citation_key": "smith2020"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._mapping_bytes(), before)

    def test_invalid_forward_pdf_shapes_leave_mapping_byte_identical(self):
        directory = self.vault / "att" / "folder.pdf"
        directory.mkdir()
        (self.vault / "att" / "notes.md").write_text("x", encoding="utf-8")
        (self.vault / "outside.pdf").write_bytes(b"%PDF-1.4\noutside\n")
        cases = [
            ("att/missing.pdf", 404),
            ("att/folder.pdf", 400),
            ("att/notes.md", 400),
            ("outside.pdf", 400),
        ]
        for rel, status in cases:
            before = self._mapping_bytes()
            r = self.client.post(
                "/api/audit/mapping", headers=self.headers,
                json={"pdf": rel, "no_match": True, "confirm": True})
            self.assertEqual(r.status_code, status, (rel, r.get_json()))
            self.assertEqual(self._mapping_bytes(), before, rel)

    def test_symlink_escaping_biblio_scope_cannot_be_curated(self):
        outside = self.vault / "outside.pdf"
        outside.write_bytes(b"%PDF-1.4\noutside\n")
        link = self.vault / "att" / "linked.pdf"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        before = self._mapping_bytes()
        r = self.client.post(
            "/api/audit/mapping", headers=self.headers,
            json={"pdf": "att/linked.pdf", "no_match": True,
                  "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._mapping_bytes(), before)

    def test_stale_or_absent_bib_key_cannot_be_curated(self):
        before = self._mapping_bytes()
        r = self.client.post(
            "/api/audit/mapping", headers=self.headers,
            json={"pdf": "att/paper.pdf", "citation_key": "stale2020",
                  "confirm": True})
        self.assertEqual(r.status_code, 409)
        error = r.get_json()["error"]
        self.assertIn("stale2020", error)
        self.assertIn("paper.pdf", error)
        self.assertNotIn(str(self.vault), error)
        self.assertEqual(self._mapping_bytes(), before)

    def test_unhashable_pdf_cannot_create_a_durable_decision(self):
        before = self._mapping_bytes()
        with mock.patch("audit.engine.bridge._hash_entry", return_value=None):
            r = self.client.post(
                "/api/audit/mapping", headers=self.headers,
                json={"pdf": "att/paper.pdf", "no_match": True,
                      "confirm": True})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self._mapping_bytes(), before)

    def test_clear_remains_available_after_the_pdf_moves(self):
        from audit.config import load_settings
        from audit.engine import bridge
        r = self.client.post(
            "/api/audit/mapping", headers=self.headers,
            json={"pdf": "att/paper.pdf", "citation_key": "smith2020",
                  "confirm": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.pdf.rename(self.vault / "att" / "moved.pdf")
        cleared = self.client.post(
            "/api/audit/mapping", headers=self.headers,
            json={"pdf": "att/paper.pdf", "clear": True,
                  "confirm": True})
        self.assertEqual(cleared.status_code, 200, cleared.get_json())
        self.assertEqual(
            bridge._load_mapping_full(load_settings().mapping_file)[0], {})


if __name__ == "__main__":
    unittest.main()


class TestBibKeyMemo(unittest.TestCase):
    """PERF (2026-09-02): a full bib parse per triage click.

    ``POST /api/audit/mapping`` re-parsed the WHOLE bibliography on every click
    just to test one citation key for membership — 3900 ms on the maintainer's
    6141-entry / 7.2 MB ``_master.bib``, on a Waitress worker.

    The parse count is asserted through the ROUTE, at ``parse_bib``, which both
    the pre- and post-change code reach: a counter on the new helper would make
    the red mean only "that helper is new".
    """

    def setUp(self):
        from audit.core import bib
        self.tmp = Path(tempfile.mkdtemp(prefix="bibmemo-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bib = self.tmp / "_master.bib"
        self._write(("a2020", "b2021"))
        memo = getattr(bib, "_KEYS_MEMO", None)
        if memo is not None:
            memo.clear()
            self.addCleanup(memo.clear)

    def _write(self, keys):
        self.bib.write_text(
            "\n".join(f"@article{{{k},\n  title = {{T {k}}},\n  year = {{2020}}\n}}"
                       for k in keys), encoding="utf-8")

    def test_repeat_validations_parse_the_bib_once(self):
        from audit.core import bib
        parses = []
        real = bib.parse_bib

        def _counted(path):
            parses.append(path)
            return real(path)

        with mock.patch.object(bib, "parse_bib", _counted):
            keys = None
            for _ in range(5):
                keys = self._keys()
        self.assertEqual(set(keys), {"a2020", "b2021"})
        self.assertEqual(len(parses), 1, f"{len(parses)} whole-bib parses")

    def _keys(self):
        """Whichever accessor the route uses, so this runs on both versions."""
        from audit.core import bib
        fn = getattr(bib, "parse_bib_keys", None)
        if fn is not None:
            return fn(self.bib)
        return {e.citation_key for e in bib.parse_bib(self.bib)}

    def test_an_edited_bib_is_reparsed_without_a_rescan(self):
        """The live-bib rejection guarantee: mtime is the key."""
        self.assertIn("b2021", self._keys())
        time.sleep(0.01)
        self._write(("a2020",))          # the user removed an entry
        self.assertNotIn("b2021", self._keys())

    def test_the_result_is_immutable(self):
        """A shared parsed ENTRY list could be mutated by a caller; a frozenset
        of keys cannot."""
        from audit.core.bib import parse_bib_keys
        self.assertIsInstance(parse_bib_keys(self.bib), frozenset)

    def test_the_route_validates_through_the_memoised_accessor(self):
        """Source ratchet: the triage route must not re-parse the whole bib."""
        import inspect

        import api.routes.audit as auditmod
        src = inspect.getsource(auditmod.api_audit_mapping)
        self.assertIn("parse_bib_keys(", src)
        self.assertNotIn("for entry in parse_bib(", src)


class TestInventoryRowsMemo(unittest.TestCase):
    """PERF (2026-09-02): the 6 000-row inventory serialises once per scan.

    Counted at ``serialize.inventory_records`` — the level both the pre- and
    post-change code reach.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-invmemo-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.client = _build_audit_only_app().test_client()
        self.headers = {"X-Requested-With": "ChatEKLD"}
        import api.routes.audit as audit_routes
        clear = getattr(audit_routes, "_clear_inventory_rows_cache", None)
        if clear is not None:
            clear()
            self.addCleanup(clear)

    def _fixture(self):
        from types import SimpleNamespace
        from audit.core.bib import BibEntry
        from audit.engine.bridge import BridgeResult
        from audit.engine.inventory import Inventory, Record

        rec = Record(citation_key="dupont2019")
        rec.bib_entry = BibEntry(citation_key="dupont2019", entry_type="article",
                                 title="T", year="2019", authors=["Dupont, M"])
        inv = Inventory(records={"dupont2019": rec}, bridge=BridgeResult(),
                        pdfs_skipped=[], zotero_matched_by_title=0)
        settings = SimpleNamespace(vault_root=self.vault,
                                   biblio_skip_prefix="z_item",
                                   annotations_read_threshold=3)
        return inv, settings

    def _get_twice(self, pair):
        from audit import serialize
        calls = []
        real = serialize.inventory_records

        def _counted(inv, settings):
            calls.append(inv)
            return real(inv, settings)

        # This serializer memo fixture intentionally lacks reconciliation settings.
        # Isolate that independent projection even if another test populated its store.
        with mock.patch("api.routes.audit._pdf_projection_summary", return_value={}), \
                mock.patch.object(serialize, "inventory_records", _counted), \
                mock.patch.object(audit_manager, "get_inventory",
                                  return_value=pair):
            first = self.client.get("/api/audit/inventory", headers=self.headers)
            second = self.client.get("/api/audit/inventory", headers=self.headers)
        return calls, first, second

    def test_two_identical_gets_serialise_once(self):
        calls, first, second = self._get_twice(self._fixture())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["records"], second.get_json()["records"])
        self.assertEqual(len(calls), 1, f"{len(calls)} serialisations")

    def test_a_new_scan_reserialises(self):
        """A replaced Inventory must not be served from the old rows."""
        self._get_twice(self._fixture())
        calls, first, _ = self._get_twice(self._fixture())
        self.assertEqual(len(calls), 1)
        self.assertEqual(first.status_code, 200)

    def test_the_result_listener_releases_the_rows(self):
        """The memo must not outlive the Inventory it was built from."""
        import api.routes.audit as audit_routes
        pair = self._fixture()
        self._get_twice(pair)
        cache = getattr(audit_routes, "_INVENTORY_ROWS_CACHE", None)
        if cache is None:
            self.skipTest("no rows memo on this build")
        audit_manager._fire_result_listeners()
        self.assertIsNone(audit_routes._INVENTORY_ROWS_CACHE)


class TestDriftSynonymMemo(unittest.TestCase):
    """PERF (2026-09-02): _tags.md is read and parsed once per (size, mtime_ns).

    Counted at ``rag.thesaurus.parse_tags`` — reached by both versions.
    """

    def setUp(self):
        from types import SimpleNamespace
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-synmemo-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.tags = self.vault / "_tags.md"
        self._write("sommeil", "sleep, insomnie")
        self.settings = SimpleNamespace(vault_root=self.vault)
        import api.routes.audit as audit_routes
        memo = getattr(audit_routes, "_SYNONYM_MEMO", None)
        if memo is not None:
            memo.clear()
            self.addCleanup(memo.clear)

    def _write(self, tag, phrases):
        self.tags.write_text(
            "| Description | Tag |\n| --- | --- |\n"
            f"| {phrases} | #{tag} |\n", encoding="utf-8")

    def _parses(self, times):
        from rag import thesaurus
        import api.routes.audit as audit_routes
        calls = []
        real = thesaurus.parse_tags

        def _counted(text, **kw):
            calls.append(text)
            return real(text, **kw)

        with mock.patch.object(thesaurus, "parse_tags", _counted), \
                mock.patch("core.config.load_config",
                           return_value={"vault_thesaurus_tags_path": "_tags.md"}):
            out = [audit_routes._drift_synonyms(self.settings)
                   for _ in range(times)]
        return calls, out

    def test_repeat_calls_parse_the_tag_file_once(self):
        calls, out = self._parses(4)
        self.assertEqual(len(calls), 1, f"{len(calls)} tag-file parses")
        self.assertTrue(all(o == out[0] for o in out))
        self.assertEqual(out[0].get("sleep"), "sommeil")

    def test_an_edited_tag_file_is_reparsed_without_a_rescan(self):
        first, _ = self._parses(1)
        self.assertEqual(len(first), 1)
        time.sleep(0.01)
        self._write("sommeil", "sleep, insomnie, dormir")
        calls, out = self._parses(1)
        self.assertEqual(len(calls), 1, "an edited _tags.md must be re-read")
        self.assertEqual(out[0].get("dormir"), "sommeil")

    def test_the_caller_cannot_reach_the_cached_mapping(self):
        _calls, out = self._parses(2)
        out[0]["poisoned"] = "x"
        again = self._parses(1)[1][0]
        self.assertNotIn("poisoned", again)


class TestSearchCorpusCancel(unittest.TestCase):
    """PERF (2026-09-02): an in-flight corpus build can be told to stop.

    Before, a scan finishing mid-build left the OLD Inventory pinned by the
    build's own arguments for the rest of a run whose result the supersession
    check then discarded — two multi-hundred-MB inventories at once.
    """

    def test_build_corpus_stops_between_report_sections(self):
        from types import SimpleNamespace

        from audit import search as audit_search
        from audit.engine.bridge import BridgeResult
        from audit.engine.inventory import Inventory

        inv = Inventory(records={}, bridge=BridgeResult(), pdfs_skipped=[],
                        zotero_matched_by_title=0)
        settings = SimpleNamespace(vault_root=Path("/vault"),
                                   biblio_skip_prefix="z_item",
                                   annotations_read_threshold=3)
        cancelled = getattr(audit_search, "CorpusCancelled", RuntimeError)
        # False on the first poll, True after: a build that only checked once,
        # at entry, would run to completion — so this pins the poll BETWEEN
        # sections, not merely the existence of the parameter.
        state = {"n": 0}

        def _cancel():
            state["n"] += 1
            return state["n"] > 1

        with self.assertRaises(cancelled):
            audit_search.build_corpus(inv, settings, should_cancel=_cancel)
        self.assertGreater(state["n"], 1, "the cancel was polled only once")

    def test_a_complete_build_is_unaffected(self):
        """The poll must not change what a normal build returns."""
        from types import SimpleNamespace

        from audit import search as audit_search
        from audit.engine.bridge import BridgeResult
        from audit.engine.inventory import Inventory

        inv = Inventory(records={}, bridge=BridgeResult(), pdfs_skipped=[],
                        zotero_matched_by_title=0)
        settings = SimpleNamespace(vault_root=Path("/vault"),
                                   biblio_skip_prefix="z_item",
                                   annotations_read_threshold=3)
        plain = audit_search.build_corpus(inv, settings)
        polled = audit_search.build_corpus(inv, settings,
                                           should_cancel=lambda: False)
        self.assertEqual(len(plain), len(polled))

    def test_clearing_the_cache_arms_the_cancel(self):
        import api.routes.audit as audit_routes
        event = getattr(audit_routes, "_SEARCH_CANCEL", None)
        if event is None:
            self.fail("no corpus cancel event")
        event.clear()
        audit_routes._clear_search_cache()
        self.assertTrue(event.is_set())


class TestHarmonizationReportRoutes(unittest.TestCase):
    """The three 2026-09-06 read-only reports over a mocked cached scan:
    ``multi_pdf`` / ``not_in_zotero`` / ``dead_curation``. Each is served by
    name, 404s before a scan like every report, and reaches the client with
    its disclosure counts intact."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-test-harm-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.tmp / "config.json").write_text(
            json.dumps({"obsidian_vault_path": str(self.vault)}), encoding="utf-8")
        cfg_path = str(self.tmp / "config.json")
        for p in (mock.patch("core.constants.CONFIG_FILE", cfg_path),
                  mock.patch("core.config.CONFIG_FILE", cfg_path),
                  mock.patch("core.constants.BASE_DIR", str(self.tmp))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.client = _build_audit_only_app().test_client()
        self.headers = {"X-Requested-With": "ChatEKLD"}

    def _inv(self, records, bridge):
        from audit.engine.inventory import Inventory
        return Inventory(records=records, bridge=bridge, pdfs_skipped=[],
                         zotero_matched_by_title=0)

    def test_all_three_are_declared_and_404_without_a_scan(self):
        from api.routes import audit as audit_routes
        for name in ("multi_pdf", "not_in_zotero", "dead_curation"):
            self.assertIn(name, audit_routes._REPORT_NAMES)
            with mock.patch.object(audit_manager, "get_inventory",
                                   return_value=(None, None)):
                r = self.client.get(f"/api/audit/reports/{name}", headers=self.headers)
            self.assertEqual(r.status_code, 404, name)

    def test_multi_pdf_serves_per_file_provenance_and_the_dup_decision(self):
        from types import SimpleNamespace
        from audit.config import Settings
        from audit.engine.bridge import BridgeResult
        a1, a2 = self.vault / "a_1.pdf", self.vault / "a_2.pdf"
        for p in (a1, a2):
            p.write_bytes(b"%PDF")
        rec = SimpleNamespace(
            citation_key="a2020", pdf_paths=[a1, a2], obsidian_note=None,
            bib_entry=SimpleNamespace(title="T", year="2020", authors=["A"]),
            zotero_item=None)
        inv = self._inv({"a2020": rec}, BridgeResult(
            source_per_pdf={a1: "doi", a2: "authoryear"}))
        settings = Settings(vault_root=self.vault)
        memo = {str(a1): {"size": 4, "mtime_ns": a1.stat().st_mtime_ns, "sha256": "aa"},
                str(a2): {"size": 4, "mtime_ns": a2.stat().st_mtime_ns, "sha256": "bb"}}
        with mock.patch.object(audit_manager, "get_inventory",
                               return_value=(inv, settings)), \
             mock.patch("audit.core.hash_cache.load_cache", return_value=memo), \
             mock.patch("audit.engine.dup_decisions.load_dismissed",
                        return_value={"multi_pdf:aa+bb"}):
            r = self.client.get("/api/audit/reports/multi_pdf", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["mixed_count"], 1)
        self.assertEqual(d["dismissed_count"], 1)
        row = d["rows"][0]
        self.assertTrue(row["mixed"] and row["dismissed"])
        self.assertEqual({f["name"]: f["source"] for f in row["files"]},
                         {"a_1.pdf": "doi", "a_2.pdf": "authoryear"})

    def test_pdf_views_consume_the_same_reconciled_workspace(self):
        """Both entry points use current relationship state, never cached absence."""
        from audit.config import Settings
        from audit.engine.bridge import BridgeResult
        settings = Settings(vault_root=self.vault)
        payload = dict(rows=[dict(state='Awaiting bibliography',can_create=False)],
                       count=1,counts={'Awaiting bibliography':1},review_scope='fixture')
        with mock.patch.object(audit_manager, 'get_inventory', return_value=(self._inv({}, BridgeResult()), settings)), \
                mock.patch('api.routes.pending.workspace', return_value=payload) as workspace:
            for name in ('pdf_review','not_in_zotero'):
                response = self.client.get('/api/audit/reports/' + name, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()['rows'], payload['rows'])
            self.assertEqual(workspace.call_count, 2)

    def test_dead_curation_reads_mapping_json_beside_the_scan(self):
        from types import SimpleNamespace
        from audit.config import Settings
        from audit.engine.bridge import BridgeResult
        mapping = self.tmp / "audit" / "mapping.json"
        mapping.parent.mkdir()
        mapping.write_text(json.dumps({
            "matches": {"gone2020": ["gone.pdf"]}, "no_match": ["nm_gone.pdf"]}),
            encoding="utf-8")
        rec = SimpleNamespace(citation_key="gone2020", pdf_paths=[], obsidian_note=None,
                              bib_entry=SimpleNamespace(title="T", year="2020", authors=[]),
                              zotero_item=None)
        inv = self._inv({"gone2020": rec}, BridgeResult(mapping_orphans=2))
        settings = Settings(vault_root=self.vault, mapping_file_override=mapping)
        with mock.patch.object(audit_manager, "get_inventory",
                               return_value=(inv, settings)):
            r = self.client.get("/api/audit/reports/dead_curation", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["count"], 2)
        self.assertEqual(d["by_kind"], {"match_missing_file": 1, "no_match_missing_file": 1})
        self.assertEqual(d["mapping_orphans"], 2)


class TestMappingStatesAndDetach(unittest.TestCase):
    """The mapping route's Phase 2 verbs (2026-09-06): the ``pending`` state,
    the list form of the bucket verbs (validated in full before ONE write),
    and ``detach_from``."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-mapstate-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        (self.vault / "att").mkdir(parents=True)
        self.a = self.vault / "att" / "a_2020.pdf"
        self.b = self.vault / "att" / "b_2020.pdf"
        for p in (self.a, self.b):
            p.write_bytes(b"%PDF-1.4\n" + p.name.encode())
        (self.vault / "_master.bib").write_text(
            "@article{smith2020, title={Current entry}}\n", encoding="utf-8")
        (self.tmp / "config.json").write_text(
            json.dumps({"obsidian_vault_path": str(self.vault),
                        "audit_attachments_subdir": "att",
                        "audit_biblio_articles_subdir": "."}), encoding="utf-8")
        cfg_path = str(self.tmp / "config.json")
        for target in ("core.constants.CONFIG_FILE", "core.config.CONFIG_FILE"):
            p = mock.patch(target, cfg_path)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("core.constants.BASE_DIR", str(self.tmp))
        p.start()
        self.addCleanup(p.stop)
        self.client = _build_audit_only_app().test_client()
        self.headers = {"X-Requested-With": "ChatEKLD"}

    def _post(self, body):
        return self.client.post("/api/audit/mapping", json=body, headers=self.headers)

    def _mapping(self):
        from audit.config import load_settings
        path = load_settings().mapping_file
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def test_pending_is_a_first_class_state_and_moves_are_the_same_verb(self):
        r = self._post({"pdf": "att/a_2020.pdf", "pending": True, "confirm": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["action"], "pending")
        self.assertEqual(self._mapping()["pending"], ["att/a_2020.pdf"])
        r = self._post({"pdf": "att/a_2020.pdf", "no_match": True, "confirm": True})
        self.assertEqual(r.status_code, 200)
        d = self._mapping()
        self.assertEqual(d["no_match"], ["att/a_2020.pdf"])
        self.assertNotIn("pending", d)

    def test_list_form_is_validated_in_full_before_any_write(self):
        """One bad path refuses the whole request — a partial bulk write would
        leave the user unable to tell which half landed."""
        r = self._post({"pdfs": ["att/a_2020.pdf", "att/missing.pdf"],
                        "pending": True, "confirm": True})
        self.assertEqual(r.status_code, 404, r.get_json())
        self.assertIsNone(self._mapping())
        r = self._post({"pdfs": ["att/a_2020.pdf", "att/b_2020.pdf", "att/a_2020.pdf"],
                        "pending": True, "confirm": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["changed"], 2)
        self.assertEqual(self._mapping()["pending"],
                         ["att/a_2020.pdf", "att/b_2020.pdf"])

    def test_list_form_clear_and_the_bound(self):
        self._post({"pdfs": ["att/a_2020.pdf", "att/b_2020.pdf"],
                    "no_match": True, "confirm": True})
        r = self._post({"pdfs": ["att/a_2020.pdf", "att/b_2020.pdf", "att/gone.pdf"],
                        "clear": True, "confirm": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["changed"], 2)   # gone.pdf: nothing to clear
        self.assertEqual(self._mapping()["no_match"], [])
        from api.routes import audit as audit_routes
        too_many = [f"att/x{i}.pdf" for i in range(audit_routes._MAPPING_MAX_PDFS + 1)]
        r = self._post({"pdfs": too_many, "clear": True, "confirm": True})
        self.assertEqual(r.status_code, 400)

    def test_pdf_and_pdfs_together_or_two_verbs_are_refused(self):
        r = self._post({"pdf": "att/a_2020.pdf", "pdfs": ["att/b_2020.pdf"],
                        "clear": True, "confirm": True})
        self.assertEqual(r.status_code, 400)
        r = self._post({"pdf": "att/a_2020.pdf", "pending": True, "no_match": True,
                        "confirm": True})
        self.assertEqual(r.status_code, 400)
        # link stays single-PDF: the list form is not offered for it.
        r = self._post({"pdfs": ["att/a_2020.pdf"], "citation_key": "smith2020",
                        "confirm": True})
        self.assertEqual(r.status_code, 400)

    def test_detach_writes_the_exclusion_and_clear_undoes_it(self):
        r = self._post({"pdf": "att/a_2020.pdf", "detach_from": "smith2020",
                        "confirm": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["action"], "detach")
        self.assertTrue(r.get_json()["changed"])
        self.assertEqual(self._mapping()["exclude"], {"att/a_2020.pdf": ["smith2020"]})
        r = self._post({"pdf": "att/a_2020.pdf", "detach_from": "smith2020",
                        "confirm": True})
        self.assertFalse(r.get_json()["changed"])
        r = self._post({"pdf": "att/a_2020.pdf", "clear": True, "confirm": True})
        self.assertTrue(r.get_json()["changed"])
        self.assertNotIn("exclude", self._mapping())

    def test_detach_all_writes_the_wildcard_and_clear_undoes_it(self):
        r = self._post({"pdf": "att/a_2020.pdf", "detach_all": True,
                        "confirm": True})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["action"], "detach_all")
        self.assertEqual(body["citation_key"], "*")
        self.assertEqual(self._mapping()["exclude"],
                         {"att/a_2020.pdf": ["*"]})
        r = self._post({"pdf": "att/a_2020.pdf", "clear": True, "confirm": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._mapping().get("exclude", {}), {})

    def test_the_wildcard_cannot_ride_in_on_detach_from(self):
        """It is not a citation key, and reaching it through the per-key field
        would let a body impose a blanket refusal where the caller — and the
        confirm text the user read — meant exactly one key."""
        r = self._post({"pdf": "att/a_2020.pdf", "detach_from": "*",
                        "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("detach_all", r.get_json()["error"])
        # No mapping file at all is the strongest form of "nothing written".
        self.assertEqual((self._mapping() or {}).get("exclude", {}), {})

    def test_detach_all_is_confirm_gated_and_exclusive(self):
        r = self._post({"pdf": "att/a_2020.pdf", "detach_all": True})
        self.assertEqual(r.status_code, 400)
        r = self._post({"pdf": "att/a_2020.pdf", "detach_all": False,
                        "confirm": True})
        self.assertEqual(r.status_code, 400)
        r = self._post({"pdf": "att/a_2020.pdf", "detach_all": True,
                        "detach_from": "smith2020", "confirm": True})
        self.assertEqual(r.status_code, 400)
        r = self._post({"pdfs": ["att/a_2020.pdf"], "detach_all": True,
                        "confirm": True})
        self.assertEqual(r.status_code, 400)   # single-PDF verb
        self.assertEqual((self._mapping() or {}).get("exclude", {}), {})

    def test_detach_needs_a_key_and_one_existing_pdf(self):
        r = self._post({"pdf": "att/a_2020.pdf", "detach_from": "", "confirm": True})
        self.assertEqual(r.status_code, 400)
        r = self._post({"pdf": "att/missing.pdf", "detach_from": "smith2020",
                        "confirm": True})
        self.assertEqual(r.status_code, 404)
        r = self._post({"pdfs": ["att/a_2020.pdf"], "detach_from": "smith2020",
                        "confirm": True})
        self.assertEqual(r.status_code, 400)

    def test_forward_verbs_still_need_confirm_and_containment(self):
        r = self._post({"pdf": "att/a_2020.pdf", "pending": True})
        self.assertEqual(r.status_code, 400)
        r = self._post({"pdf": "../a_2020.pdf", "pending": True, "confirm": True})
        self.assertEqual(r.status_code, 400)

    def test_excluded_key_rematch_needs_accept_excluded(self):
        """Accession Link selected must not silently lift a detach exclude."""
        self._post({"pdf": "att/a_2020.pdf", "detach_from": "smith2020",
                    "confirm": True})
        r = self._post({"pdf": "att/a_2020.pdf", "citation_key": "smith2020",
                        "confirm": True, "source": "accession"})
        self.assertEqual(r.status_code, 409, r.get_json())
        body = r.get_json()
        self.assertEqual(body["reason"], "excluded")
        self.assertEqual(body["citation_key"], "smith2020")
        self.assertEqual(self._mapping()["exclude"],
                         {"att/a_2020.pdf": ["smith2020"]})
        r = self._post({"pdf": "att/a_2020.pdf", "citation_key": "smith2020",
                        "confirm": True, "source": "accession",
                        "accept_excluded": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertNotIn("exclude", self._mapping())
        self.assertEqual(self._mapping()["matches"]["smith2020"],
                         ["att/a_2020.pdf"])

    def test_detach_all_wildcard_also_blocks_rematch_without_ack(self):
        self._post({"pdf": "att/a_2020.pdf", "detach_all": True, "confirm": True})
        r = self._post({"pdf": "att/a_2020.pdf", "citation_key": "smith2020",
                        "confirm": True})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "excluded")

    def test_detach_without_trash_flag_never_touches_zotero(self):
        with mock.patch(
            "tagging.writers.find_linked_attachments"
        ) as find:
            r = self._post({"pdf": "att/a_2020.pdf", "detach_from": "smith2020",
                            "confirm": True})
            self.assertEqual(r.status_code, 200, r.get_json())
            find.assert_not_called()
            body = r.get_json()
            self.assertEqual(body.get("trashed"), [])
            self.assertIsNone(body.get("batch_id"))

    def test_detach_trash_refuses_when_links_exist_but_transport_cannot_write(self):
        from types import SimpleNamespace
        ref = SimpleNamespace(item_key="ATT1", parent_key="PARENT1")
        inv = SimpleNamespace(records={
            "smith2020": SimpleNamespace(
                zotero_item=SimpleNamespace(item_key="PARENT1")),
        })
        transport = SimpleNamespace(kind="local", can_write=False)
        with mock.patch("tagging.writers.find_linked_attachments",
                        return_value=([ref], None)), \
             mock.patch("audit.manager.audit_manager.get_inventory",
                        return_value=(inv, None)), \
             mock.patch("tagging.zotero_write.resolve_transport",
                        return_value=transport), \
             mock.patch("tagging.writers.zotero_attachment_prepare") as prep, \
             mock.patch("audit.engine.bridge.detach") as detach_fn:
            r = self._post({
                "pdf": "att/a_2020.pdf", "detach_from": "smith2020",
                "confirm": True, "trash_zotero_links": True,
            })
            self.assertEqual(r.status_code, 409, r.get_json())
            self.assertEqual(r.get_json()["reason"], "zotero_not_writable")
            prep.assert_not_called()
            detach_fn.assert_not_called()
            self.assertIsNone(self._mapping())

    def test_detach_trash_filters_to_the_detached_parent_only(self):
        from types import SimpleNamespace
        mine = SimpleNamespace(item_key="ATT_MINE", parent_key="PARENT1")
        other = SimpleNamespace(item_key="ATT_OTHER", parent_key="OTHER")
        inv = SimpleNamespace(records={
            "smith2020": SimpleNamespace(
                zotero_item=SimpleNamespace(item_key="PARENT1")),
        })
        transport = SimpleNamespace(kind="web", can_write=True)
        plan = {
            "commit": {
                "item_key": "ATT_MINE",
                "parent_item": "PARENT1",
                "filename": str(self.a),
            },
        }

        def _prepare(cfg, item_key, ref):
            self.assertEqual(item_key, "ATT_MINE")
            self.assertIs(ref, mine)
            return plan

        with mock.patch("tagging.writers.find_linked_attachments",
                        return_value=([mine, other], None)), \
             mock.patch("audit.manager.audit_manager.get_inventory",
                        return_value=(inv, None)), \
             mock.patch("tagging.zotero_write.resolve_transport",
                        return_value=transport), \
             mock.patch("tagging.writers.zotero_attachment_prepare",
                        side_effect=_prepare), \
             mock.patch("api.routes.audit._trash_prepared_attachments",
                        return_value=([{
                            "item_key": "ATT_MINE",
                            "parent_key": "PARENT1",
                            "path": str(self.a),
                        }], "batch-1", None)) as trash:
            r = self._post({
                "pdf": "att/a_2020.pdf", "detach_from": "smith2020",
                "confirm": True, "trash_zotero_links": True,
            })
            self.assertEqual(r.status_code, 200, r.get_json())
            body = r.get_json()
            self.assertEqual(body["trashed"][0]["item_key"], "ATT_MINE")
            self.assertEqual(body["batch_id"], "batch-1")
            trash.assert_called_once()
            prepared = trash.call_args[0][0]
            self.assertEqual(len(prepared), 1)
            self.assertEqual(self._mapping()["exclude"],
                             {"att/a_2020.pdf": ["smith2020"]})

    def test_detach_trash_refuses_before_unmap_when_zotero_unreadable(self):
        with mock.patch("tagging.writers.find_linked_attachments",
                        return_value=([], "Zotero database unreadable: boom")), \
             mock.patch("audit.engine.bridge.detach") as detach_fn:
            r = self._post({
                "pdf": "att/a_2020.pdf", "detach_from": "smith2020",
                "confirm": True, "trash_zotero_links": True,
            })
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.get_json()["reason"], "zotero_unreadable")
            detach_fn.assert_not_called()

    def test_post_detach_trash_failure_still_reports_unmap_ok(self):
        """Detach landed; trash journal lock busy must not look like unmap failed."""
        from types import SimpleNamespace
        ref = SimpleNamespace(item_key="ATT1", parent_key="PARENT1")
        inv = SimpleNamespace(records={
            "smith2020": SimpleNamespace(
                zotero_item=SimpleNamespace(item_key="PARENT1")),
        })
        transport = SimpleNamespace(kind="web", can_write=True)
        plan = {"commit": {"item_key": "ATT1", "parent_item": "PARENT1",
                           "filename": str(self.a)}}
        err_body = mock.Mock()
        err_body.get_json.return_value = {
            "error": "Indexing is in progress; try again shortly.",
        }
        with mock.patch("tagging.writers.find_linked_attachments",
                        return_value=([ref], None)), \
             mock.patch("audit.manager.audit_manager.get_inventory",
                        return_value=(inv, None)), \
             mock.patch("tagging.zotero_write.resolve_transport",
                        return_value=transport), \
             mock.patch("tagging.writers.zotero_attachment_prepare",
                        return_value=plan), \
             mock.patch("api.routes.audit._trash_prepared_attachments",
                        return_value=([], "", (err_body, 503))):
            r = self._post({
                "pdf": "att/a_2020.pdf", "detach_from": "smith2020",
                "confirm": True, "trash_zotero_links": True,
            })
            self.assertEqual(r.status_code, 200, r.get_json())
            body = r.get_json()
            self.assertTrue(body["ok"])
            self.assertTrue(body["changed"])
            self.assertEqual(body["trashed"], [])
            self.assertIn("Indexing", body["zotero_error"])
            self.assertEqual(self._mapping()["exclude"],
                             {"att/a_2020.pdf": ["smith2020"]})

    def test_post_detach_partial_patch_sets_zotero_error(self):
        from types import SimpleNamespace
        ref = SimpleNamespace(item_key="ATT1", parent_key="PARENT1")
        inv = SimpleNamespace(records={
            "smith2020": SimpleNamespace(
                zotero_item=SimpleNamespace(item_key="PARENT1")),
        })
        transport = SimpleNamespace(kind="web", can_write=True)
        plan = {"commit": {"item_key": "ATT1", "parent_item": "PARENT1",
                           "filename": str(self.a)}}
        with mock.patch("tagging.writers.find_linked_attachments",
                        return_value=([ref], None)), \
             mock.patch("audit.manager.audit_manager.get_inventory",
                        return_value=(inv, None)), \
             mock.patch("tagging.zotero_write.resolve_transport",
                        return_value=transport), \
             mock.patch("tagging.writers.zotero_attachment_prepare",
                        return_value=plan), \
             mock.patch("api.routes.audit._trash_prepared_attachments",
                        return_value=([], "batch-x", None)):
            r = self._post({
                "pdf": "att/a_2020.pdf", "detach_from": "smith2020",
                "confirm": True, "trash_zotero_links": True,
            })
            self.assertEqual(r.status_code, 200)
            body = r.get_json()
            self.assertEqual(body["trashed"], [])
            self.assertIn("could not be moved to trash", body["zotero_error"])

    def test_mapping_detach_never_uses_http_delete(self):
        from pathlib import Path as _P
        src = (_P(__file__).resolve().parent / "api" / "routes" / "audit.py"
               ).read_text(encoding="utf-8")
        # Trash must stay PATCH {"deleted": 1} via the shared writers — never
        # a DELETE verb invented on the mapping route.
        self.assertNotIn('method="DELETE"', src)
        self.assertNotIn("method='DELETE'", src)


class TestZoteroLinkWarning(unittest.TestCase):
    def test_a_zotero_linked_rel_is_named(self):
        from types import SimpleNamespace
        from api.routes import audit as auditmod

        tmp = Path(tempfile.mkdtemp(prefix="zotlink-warn-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        vault = tmp / "vault"
        pdf = vault / "att" / "paper.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"%PDF")
        rel = "att/paper.pdf"
        inv = SimpleNamespace(bridge=SimpleNamespace(source_per_pdf={pdf: "zotero_link"}))
        settings = SimpleNamespace(vault_root=vault)
        with mock.patch("audit.manager.audit_manager.get_inventory",
                        return_value=(inv, settings)):
            msg = auditmod._zotero_link_warning(settings, [rel])
        self.assertIsNotNone(msg)
        self.assertIn("paper.pdf", msg)
        self.assertIn("Zotero wins", msg)

    def test_a_manual_pairing_is_silent(self):
        from types import SimpleNamespace
        from api.routes import audit as auditmod

        tmp = Path(tempfile.mkdtemp(prefix="zotlink-warn-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        vault = tmp / "vault"
        pdf = vault / "att" / "paper.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"%PDF")
        inv = SimpleNamespace(bridge=SimpleNamespace(source_per_pdf={pdf: "manual"}))
        settings = SimpleNamespace(vault_root=vault)
        with mock.patch("audit.manager.audit_manager.get_inventory",
                        return_value=(inv, settings)):
            self.assertIsNone(auditmod._zotero_link_warning(settings, ["att/paper.pdf"]))

    def test_an_inventory_without_a_usable_bridge_is_silent(self):
        """The warning is observational, so a bridge it cannot read must make
        it say nothing — never raise into the mapping route, which WRITES.

        `Inventory.bridge` is a required field, so a real scan always has one;
        this is defence in depth for a partially built inventory (the shape
        `reports/multi_pdf.py` already guards for with `is not None`) and for
        the stubs the detach tests hand the route. Before 2026-09-19 the bare
        `inv.bridge` turned either into a 500 on a confirmed write."""
        from types import SimpleNamespace
        from api.routes import audit as auditmod

        settings = SimpleNamespace(vault_root=Path("/tmp"))
        for inv in (SimpleNamespace(), SimpleNamespace(bridge=None)):
            with self.subTest(inv=inv), \
                    mock.patch("audit.manager.audit_manager.get_inventory",
                               return_value=(inv, settings)):
                self.assertIsNone(auditmod._zotero_link_warning(
                    settings, ["att/paper.pdf"]))
