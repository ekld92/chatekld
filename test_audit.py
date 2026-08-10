"""End-to-end tests for the Library Audit subsystem.

Covers:

- the :func:`audit.config.load_settings` adapter (ChatEKLD keys ->
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
    """``audit.config.load_settings`` reads ChatEKLD config and produces
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
            json.dumps({"obsidian_vault_path": str(self.vault)}),
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

    @mock.patch("api.routes.audit.subprocess.Popen")
    def test_rejects_path_outside_allowed_roots(self, popen_mock):
        r = self.client.post(
            "/api/audit/reveal",
            json={"open": str(self.outside)},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("outside the configured roots", r.get_json().get("error", ""))
        popen_mock.assert_not_called()

    @mock.patch("api.routes.audit.subprocess.Popen")
    def test_allows_path_inside_vault_root(self, popen_mock):
        r = self.client.post(
            "/api/audit/reveal",
            json={"open": str(self.allowed)},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        popen_mock.assert_called_once()

    @mock.patch("api.routes.audit.subprocess.Popen")
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

    @mock.patch("api.routes.audit.subprocess.Popen")
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
        matches, _no_match, hashes = self._state()
        self.assertIn("smith2020", matches)
        self.assertTrue(hashes)   # anchored on write

        self.assertTrue(bridge.clear_curation(self.mapping, self.pdf, self.vault))
        matches, no_match, hashes = self._state()
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
        self.client = _build_audit_only_app().test_client()
        self.headers = {"X-Requested-With": "ChatEKLD"}

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
            json={"pdf": "att/paper.pdf", "citation_key": "smith2020"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIn("smith2020", bridge._load_mapping_full(settings.mapping_file)[0])

        r = self.client.post(
            "/api/audit/mapping",
            json={"pdf": "att/paper.pdf", "clear": True},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["action"], "clear")
        self.assertTrue(d["changed"])
        self.assertNotIn("smith2020", bridge._load_mapping_full(settings.mapping_file)[0])

    def test_clear_wins_over_a_leftover_citation_key(self):
        """An undo body carrying the key it reverses must not re-add it."""
        from audit.engine import bridge
        from audit.config import load_settings
        settings = load_settings()
        r = self.client.post(
            "/api/audit/mapping",
            json={"pdf": "att/paper.pdf", "citation_key": "smith2020", "clear": True},
            headers=self.headers,
        )
        self.assertEqual(r.get_json()["action"], "clear")
        self.assertEqual(bridge._load_mapping_full(settings.mapping_file)[0], {})


if __name__ == "__main__":
    unittest.main()
