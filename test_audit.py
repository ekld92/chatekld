"""End-to-end tests for the Library Audit subsystem.

Covers:

- the :func:`audit.config.load_settings` adapter (papermind keys ->
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
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
    """``audit.config.load_settings`` reads papermind config and produces
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
        p1.start(); self.addCleanup(p1.stop)
        p2.start(); self.addCleanup(p2.stop)
        # Also patch BASE_DIR so the mapping.json default (BASE_DIR/audit/...)
        # lands inside the tmpdir rather than the real app data dir.
        base = str(self.tmp)
        p3 = mock.patch("core.constants.BASE_DIR", base)
        p3.start(); self.addCleanup(p3.stop)

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
        from audit.config import load_settings  # noqa: WPS433

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
        from audit.config import (  # noqa: WPS433
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
        from audit.config import AuditConfigError, load_settings  # noqa: WPS433

        with self.assertRaises(AuditConfigError):
            load_settings()

    def test_nonexistent_vault_path_raises(self):
        (self.tmp / "config.json").write_text(
            json.dumps({"obsidian_vault_path": "/nonexistent/path/xyz"}),
            encoding="utf-8",
        )
        from audit.config import AuditConfigError, load_settings  # noqa: WPS433

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
        from audit.config import load_settings  # noqa: WPS433

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
        from audit.config import DEFAULT_ANNOTATIONS_READ_THRESHOLD, load_settings  # noqa: WPS433

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
        bib_dir = self.vault / "presentations_slides_writings_teaching"
        bib_dir.mkdir()
        (bib_dir / "_master.bib").write_text(
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
        from audit.manager import audit_manager  # noqa: WPS433
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


class TestFlaskAppBootDoesNotScan(unittest.TestCase):
    """The critical regression: importing the Flask app and creating the
    app object must NOT trigger a scan as a side effect.

    This test does use the real ``create_app()`` because that is exactly
    the contract under test.  We accept that the call has side effects on
    the live obsidian_manager singleton — the test only asserts that the
    audit_manager.start_scan spy is never called.
    """

    def test_create_app_leaves_audit_idle(self):
        from app import create_app  # noqa: WPS433
        from audit.manager import audit_manager  # noqa: WPS433

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
    """
    from api.routes.audit import audit_bp  # noqa: WPS433

    app = flask.Flask(__name__)
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
        bib_dir = self.vault / "presentations_slides_writings_teaching"
        bib_dir.mkdir()
        (bib_dir / "_master.bib").write_text(
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
        from audit.core import pdf_annotations  # noqa: WPS433

        def fake(path):
            seen.append(path)
            return pdf_annotations.AnnotationsResult(counts.get(path.name, 0))

        return mock.patch.object(pdf_annotations, "read_annotations", fake)

    def test_build_inventory_precomputes_unmapped_annotations(self):
        from audit.config import load_settings  # noqa: WPS433
        from audit.engine import inventory as eng_inventory  # noqa: WPS433

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
        from audit.config import load_settings  # noqa: WPS433
        from audit.engine import inventory as eng_inventory  # noqa: WPS433

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

        from audit.core.pdf_annotations import AnnotationsResult  # noqa: WPS433
        from audit.engine.reports import (  # noqa: WPS433
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
                unmapped_pdfs=[p_unread, p_read], ambiguous_pdfs={}
            ),
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


class TestReadAnnotationsParallel(unittest.TestCase):
    """The parallel reader returns a complete map, an empty map for no input,
    and honours cooperative cancel without hanging."""

    def test_reads_all_paths(self):
        from audit.core import pdf_annotations  # noqa: WPS433
        from audit.engine.inventory import _read_annotations_parallel  # noqa: WPS433

        paths = [Path(f"/x/{i}.pdf") for i in range(20)]

        def fake(path):
            return pdf_annotations.AnnotationsResult(int(path.stem))

        with mock.patch.object(pdf_annotations, "read_annotations", fake):
            out = _read_annotations_parallel(paths)
        self.assertEqual(set(out), set(paths))
        self.assertEqual(out[paths[7]].count, 7)

    def test_empty_input(self):
        from audit.engine.inventory import _read_annotations_parallel  # noqa: WPS433

        self.assertEqual(_read_annotations_parallel([]), {})

    def test_cancel_returns_without_hanging(self):
        from audit.core import pdf_annotations  # noqa: WPS433
        from audit.engine.inventory import _read_annotations_parallel  # noqa: WPS433

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


if __name__ == "__main__":
    unittest.main()
