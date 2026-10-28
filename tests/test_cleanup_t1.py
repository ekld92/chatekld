"""Annual library cleanup T1 (2026-09-17): headless coverage of GUI-only routes.

Decision D4 of the cleanup plan is that the maintainer does nothing in the GUI,
so every verb the cleanup needs must be reachable from the CLI through the SAME
confirm-gated, journalled route the GUI uses. What these tests pin is the part
that could silently go wrong while every existing suite stayed green:

* the bulk dup-decision is all-or-nothing (a partial bulk write leaves no way
  to tell which half landed) and never truncates the decision store;
* ``pdf-links-status`` says ``current`` only when a write would change nothing;
* the driver keeps a refusal's machine-readable fields instead of collapsing
  them to a status line, and never treats ``"error": null`` as a failure;
* no CLI write command reaches the driver without an explicit ``--confirm``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import io
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flask
from click.testing import CliRunner

from audit.engine import dup_decisions

_HEADERS = {"X-Requested-With": "ChatEKLD"}


class TestDupDecisionsBulk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "dup_decisions.json"
        self.p = mock.patch.object(dup_decisions, "decisions_path", return_value=path)
        self.p.start()
        self.path = path

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_bulk_add_is_one_write_and_counts_only_new_ids(self):
        dup_decisions.add_dismissed("a")
        with mock.patch.object(dup_decisions, "_save", wraps=dup_decisions._save) as save:
            self.assertEqual(dup_decisions.add_dismissed_many(["a", "b", "c", "b"]), 2)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(dup_decisions.load_dismissed(), {"a", "b", "c"})

    def test_bulk_clear_is_the_exact_undo(self):
        dup_decisions.add_dismissed_many(["a", "b", "c"])
        self.assertEqual(dup_decisions.clear_dismissed_many(["b", "c", "z"]), 2)
        self.assertEqual(dup_decisions.load_dismissed(), {"a"})

    def test_a_corrupt_store_refuses_rather_than_restarting_from_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            dup_decisions.add_dismissed_many(["a"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_past_the_backstop_it_refuses_instead_of_silently_truncating(self):
        with mock.patch.object(dup_decisions, "_MAX_IDS", 3):
            dup_decisions.add_dismissed_many(["a", "b"])
            with self.assertRaises(ValueError):
                dup_decisions.add_dismissed_many(["c", "d"])
        self.assertEqual(dup_decisions.load_dismissed(), {"a", "b"})


class TestDupDecisionRouteListForm(unittest.TestCase):
    def setUp(self):
        from api.routes.audit import audit_bp
        app = flask.Flask(__name__)
        app.register_blueprint(audit_bp)
        self.client = app.test_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(dup_decisions, "decisions_path",
                                   return_value=Path(self.tmp.name) / "d.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def _post(self, body):
        return self.client.post("/api/audit/dup-decision", json=body, headers=_HEADERS)

    def test_one_invalid_id_writes_nothing(self):
        r = self._post({"pair_ids": ["good", "bad\nid"], "dismiss": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(dup_decisions.load_dismissed(), set())

    def test_list_dismiss_then_clear(self):
        r = self._post({"pair_ids": ["x", "y"], "dismiss": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["changed"], 2)
        # `clear` is checked first, so an undo body carrying `dismiss` too is
        # never read as a re-add.
        r = self._post({"pair_ids": ["x"], "clear": True, "dismiss": True})
        self.assertTrue(r.get_json()["cleared"])
        self.assertEqual(dup_decisions.load_dismissed(), {"y"})

    def test_empty_and_oversized_lists_are_refused(self):
        self.assertEqual(self._post({"pair_ids": [], "dismiss": True}).status_code, 400)
        from api.routes import audit as audit_route
        with mock.patch.object(audit_route, "_DUP_DECISION_MAX_IDS", 1):
            self.assertEqual(self._post({"pair_ids": ["a", "b"], "dismiss": True}).status_code, 400)

    def test_the_single_form_is_unchanged(self):
        r = self._post({"pair_id": "solo", "dismiss": True})
        self.assertEqual(r.get_json(), {"ok": True, "dismissed": True, "changed": True})


NOTE = "---\ntags: [x]\n---\n**Title**\n"


class TestPdfLinksStatus(unittest.TestCase):
    def setUp(self):
        from api.routes.tagging import tagging_bp
        app = flask.Flask(__name__)
        app.register_blueprint(tagging_bp)
        self.client = app.test_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _note(self, name, text):
        p = self.root / name
        p.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return SimpleNamespace(path=p)

    def _rec(self, pdfs, note):
        return SimpleNamespace(pdf_paths=[self.root / n for n in pdfs], obsidian_note=note)

    def test_each_state_is_classified_as_the_writer_would_see_it(self):
        from api.routes._audit_tagwrite import set_pdf_links
        current, _ = set_pdf_links(NOTE, ["a_2020.pdf"])
        stale, _ = set_pdf_links(NOTE, ["old_2019.pdf"])
        dirty = NOTE.replace("---\n**Title**", "pdf:\n  - my own list\n---\n**Title**")
        records = {
            "current": self._rec(["a_2020.pdf"], self._note("c.md", current)),
            "absent": self._rec(["b_2020.pdf"], self._note("a.md", NOTE)),
            "stale": self._rec(["c_2020.pdf"], self._note("s.md", stale)),
            "dirty": self._rec(["d_2020.pdf"], self._note("d.md", dirty)),
            "unreadable": self._rec(["e_2020.pdf"], self._note("u.md", b"\xff\xfe")),
            "nonote": self._rec(["f_2020.pdf"], None),
            "nopdf": self._rec([], self._note("n.md", NOTE)),
            "neither": self._rec([], None),
        }
        inv = SimpleNamespace(records=records, built_at=7)
        with mock.patch("api.routes.tagging.audit_manager.get_inventory",
                        return_value=(inv, SimpleNamespace(vault_root=self.root))):
            body = self.client.get("/api/tagging/pdf-links-status", headers=_HEADERS).get_json()
        self.assertEqual(body["counts"], {"current": 1, "absent": 1, "stale": 1, "dirty": 1,
                                          "unreadable": 1, "no_note": 1, "no_pdf": 1})
        states = {r["citation_key"]: r["state"] for r in body["rows"]}
        self.assertNotIn("current", states)
        self.assertNotIn("neither", states)
        self.assertEqual(states["nonote"], "no_note")
        self.assertEqual(states["nopdf"], "no_pdf")

    def test_state_filter_and_unknown_state(self):
        inv = SimpleNamespace(records={
            "absent": self._rec(["b_2020.pdf"], self._note("a.md", NOTE)),
            "nonote": self._rec(["f_2020.pdf"], None)}, built_at=0)
        with mock.patch("api.routes.tagging.audit_manager.get_inventory",
                        return_value=(inv, SimpleNamespace(vault_root=self.root))):
            body = self.client.get("/api/tagging/pdf-links-status?state=absent",
                                   headers=_HEADERS).get_json()
            bad = self.client.get("/api/tagging/pdf-links-status?state=nope", headers=_HEADERS)
        self.assertEqual([r["citation_key"] for r in body["rows"]], ["absent"])
        self.assertEqual(body["counts"]["no_note"], 1)
        self.assertEqual(bad.status_code, 400)

    def test_an_ambiguous_name_listed_bare_reads_stale(self):
        """A bare `[[x.pdf]]` can open either of two same-named files, so once
        a second copy exists anywhere in the vault the expected entry is the
        mapped file's vault path — and a note still listing the bare name is
        caught as stale rather than reported current."""
        from api.routes._audit_tagwrite import set_pdf_links
        (self.root / "biblio").mkdir()
        (self.root / "thesis").mkdir()
        (self.root / "biblio" / "caceda_2020.pdf").write_bytes(b"ours")
        (self.root / "thesis" / "caceda_2020.pdf").write_bytes(b"other")
        (self.root / "biblio" / "solo_2021.pdf").write_bytes(b"x")
        bare, _ = set_pdf_links(NOTE, ["caceda_2020.pdf", "solo_2021.pdf"])
        rec = SimpleNamespace(
            pdf_paths=[self.root / "biblio" / "caceda_2020.pdf",
                       self.root / "biblio" / "solo_2021.pdf"],
            obsidian_note=self._note("c.md", bare))
        inv = SimpleNamespace(records={"k": rec}, built_at=0)
        with mock.patch("api.routes.tagging.audit_manager.get_inventory",
                        return_value=(inv, SimpleNamespace(vault_root=self.root))):
            body = self.client.get("/api/tagging/pdf-links-status",
                                   headers=_HEADERS).get_json()
        self.assertEqual(body["rows"][0]["state"], "stale")
        self.assertEqual(body["rows"][0]["expected"],
                         ["biblio/caceda_2020.pdf", "solo_2021.pdf"])

    def test_no_scan_is_404(self):
        with mock.patch("api.routes.tagging.audit_manager.get_inventory",
                        return_value=(None, None)):
            r = self.client.get("/api/tagging/pdf-links-status", headers=_HEADERS)
        self.assertEqual(r.status_code, 404)


class TestZotcreateAttemptsRoute(unittest.TestCase):
    def test_lists_rows_and_counts_stages(self):
        from api.routes.zotcreate import zotcreate_bp
        app = flask.Flask(__name__)
        app.register_blueprint(zotcreate_bp)
        rows = [{"identity": "i1", "sha256": "s", "stage": "parent_unknown", "retained": {}},
                {"identity": "i2", "sha256": "t", "stage": "complete", "retained": {}},
                {"identity": "i3", "sha256": "u", "stage": "complete", "retained": {}}]
        settings = SimpleNamespace(vault_root=Path("/v"))
        with mock.patch("api.routes.zotcreate.audit_manager.get_inventory",
                        return_value=(None, settings)), \
                mock.patch("zotcreate.attempts.list_attempts", return_value=rows) as la:
            body = app.test_client().get("/api/zotcreate/attempts", headers=_HEADERS).get_json()
        la.assert_called_once_with(Path("/v"))
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["stages"], {"parent_unknown": 1, "complete": 2})


def _http_error(status, body):
    return urllib.error.HTTPError("http://x/api", status, "X", {},
                                  io.BytesIO(json.dumps(body).encode()))


class TestDriverRefusalBodies(unittest.TestCase):
    def test_a_refusal_without_an_error_key_keeps_its_fields(self):
        from core import driver
        body = {"ok": False, "stage": "blocked", "message": "Parent outcome unknown."}
        with mock.patch("core.driver.urllib.request.urlopen",
                        side_effect=_http_error(409, body)):
            res = driver._http_json("zotcreate_create", {}, base_url="http://x")
        self.assertEqual(res["error"], "Parent outcome unknown.")
        self.assertEqual(res["stage"], "blocked")
        self.assertEqual(res["http_status"], 409)

    def test_an_sse_refusal_keeps_its_reason(self):
        from core import driver
        body = {"error": "annotations", "reason": "would_trash_annotations",
                "annotated_keys": ["K1"]}
        with mock.patch("core.driver.urllib.request.urlopen",
                        side_effect=_http_error(409, body)):
            res = driver._http_sse("zotlink_trash", {}, base_url="http://x")
        self.assertEqual(res["reason"], "would_trash_annotations")
        self.assertEqual(res["annotated_keys"], ["K1"])

    def test_error_null_is_not_a_failure(self):
        from core import driver
        payload = {"state": "done", "error": None}
        with mock.patch("core.driver.resolve_base_url", return_value="http://x"), \
                mock.patch("core.driver._http_json", return_value=payload):
            res = driver.audit_status()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["state"], "done")


class TestDriverWriteShapes(unittest.TestCase):
    def setUp(self):
        self.base = mock.patch("core.driver.resolve_base_url", return_value="http://x")
        self.base.start()

    def tearDown(self):
        self.base.stop()

    def test_merge_always_sends_both_targets_and_confirm(self):
        from core import driver
        with mock.patch("core.driver._http_json", return_value={"ok": True}) as http:
            driver.notesync_merge("k", "text", "sha", "ev")
        body = http.call_args.args[1]
        self.assertEqual(body["targets"], {"obsidian": True, "zotero": True})
        self.assertIs(body["confirm"], True)

    def test_layout_dry_run_sends_no_confirm(self):
        from core import driver
        with mock.patch("core.driver._http_json", return_value={}) as http:
            driver.notesync_layout("migrate", dry_run=True)
        self.assertNotIn("confirm", http.call_args.args[1])

    def test_bounds_refuse_before_any_request(self):
        from core import driver
        with mock.patch("core.driver._http_json") as http, \
                mock.patch("core.driver._http_sse") as sse:
            self.assertIn("error", driver.notesync_push([f"k{i}" for i in range(51)]))
            self.assertIn("error", driver.notesync_pull([f"k{i}" for i in range(101)]))
            self.assertIn("error", driver.zotlink_trash([f"K{i}" for i in range(201)]))
            self.assertIn("error", driver.notesync_bootstrap(["k"], all_notes=True))
            self.assertIn("error", driver.pdfs_apply("k", "cleanup", ""))
            self.assertIn("error", driver.pdfs_preview("k", "cleanup", pdf="x.pdf"))
            self.assertIn("error", driver.zotcreate_create({"pdf": "x", "key": "EVIL"}))
        http.assert_not_called()
        sse.assert_not_called()

    def test_stream_returns_the_terminal_frame_and_rows(self):
        from core import driver
        events = [{"info": "x"}, {"row": {"pdf": "a"}}, {"row": {"pdf": "b"}},
                  {"accession": {"total": 2}}]
        with mock.patch("core.driver._http_sse", return_value={"events": events}):
            res = driver.accession_batch(["a", "b"])
        self.assertEqual(res["accession"], {"total": 2})
        self.assertEqual([r["pdf"] for r in res["rows"]], ["a", "b"])
        with mock.patch("core.driver._http_sse", return_value={"events": [{"info": "x"}]}):
            self.assertIn("error", driver.accession_batch(["a"]))


class TestCleanupProposeScope(unittest.TestCase):
    def test_propose_takes_an_optional_scope(self):
        import inspect
        from cli.main import cleanup_propose_cmd
        self.assertIn("scope", inspect.signature(cleanup_propose_cmd.callback).parameters)


class TestCliWritesNeedConfirm(unittest.TestCase):
    WRITES = [
        ["pdfs", "apply", "--key", "k", "--action", "link", "--pdf", "a.pdf", "--token", "t"],
        ["pdfs", "restore", "--operation", "o"],
        ["pdfs", "complete", "--operation", "o", "--token", "t"],
        ["accession", "batch", "--pdfs", "a.pdf"],
        ["maintain", "rename", "a.pdf", "b_2020.pdf"],
        ["notesync", "create", "--keys", "k"],
        ["notesync", "bootstrap", "--all"],
        ["notesync", "pull", "--keys", "k"],
        ["notesync", "push", "--keys", "k"],
        ["notesync", "create-zotero-note", "k"],
        ["zotcreate", "create", "--draft-file", "__DRAFT__"],
        ["zotlink", "trash", "--item-keys", "K1"],
        ["zotrecord", "retitle", "k", "--title", "T"],
        ["zotrecord", "merge", "k1", "k2"],
        ["zotrecord", "trash", "k1"],
        ["tagging", "restore-batch", "b1"],
        ["library", "run", "apply", "s1", "--approvals-file", "__DRAFT__"],
    ]

    def test_no_write_reaches_the_driver_without_confirm(self):
        from cli.main import cli
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp) / "d.json"
            draft.write_text("{}", encoding="utf-8")
            with mock.patch("core.driver._http_json") as http, \
                    mock.patch("core.driver._http_sse") as sse, \
                    mock.patch("core.driver.resolve_base_url", return_value="http://x"):
                for argv in self.WRITES:
                    argv = [str(draft) if a == "__DRAFT__" else a for a in argv]
                    res = runner.invoke(cli, argv)
                    self.assertNotEqual(res.exit_code, 0, argv)
                    self.assertIn("--confirm", res.output, argv)
            http.assert_not_called()
            sse.assert_not_called()

    def test_cleanup_preview_builds_the_actions_map(self):
        from cli.main import cli
        with mock.patch("core.driver.pdfs_preview", return_value={"status": "ok"}) as drv:
            res = CliRunner().invoke(cli, [
                "pdfs", "preview", "--key", "k", "--action", "cleanup",
                "--survivor", "a.pdf", "--unmap", "b.pdf", "--delete", "c.pdf"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertEqual(drv.call_args.kwargs["actions"], {"b.pdf": "unmap", "c.pdf": "delete"})
        self.assertIsNone(drv.call_args.kwargs["acknowledge_annotations"])

    def test_a_path_in_both_unmap_and_delete_is_a_usage_error(self):
        from cli.main import cli
        with mock.patch("core.driver.pdfs_preview") as drv:
            res = CliRunner().invoke(cli, [
                "pdfs", "preview", "--key", "k", "--action", "cleanup",
                "--survivor", "a.pdf", "--unmap", "b.pdf", "--delete", "b.pdf"])
        self.assertNotEqual(res.exit_code, 0)
        drv.assert_not_called()

    def test_audit_wait_exit_codes(self):
        from cli.main import cli
        states = iter([{"status": "ok", "state": "scanning"},
                       {"status": "ok", "state": "done", "has_results": True}])
        with mock.patch("core.driver.audit_status", side_effect=lambda: next(states)):
            ok = CliRunner().invoke(cli, ["audit", "wait", "--interval", "0"])
        self.assertEqual(ok.exit_code, 0, ok.output)
        with mock.patch("core.driver.audit_status",
                        return_value={"status": "ok", "state": "error", "error": "boom"}):
            bad = CliRunner().invoke(cli, ["audit", "wait", "--interval", "0"])
        self.assertEqual(bad.exit_code, 1)


if __name__ == "__main__":
    unittest.main()
