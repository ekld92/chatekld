"""Unit tests for cli/main.py command line interface."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from click.testing import CliRunner

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from unittest import mock  # noqa: E402

from cli.main import cli  # noqa: E402


class TestCLI(unittest.TestCase):

    def setUp(self):
        self.runner = CliRunner()
        # Hermeticity (2026-07-22 audit M6): the status path previously probed
        # the REAL port 5000, so a live server there flipped these tests. The
        # env override is authoritative and read at call time (C0), so the
        # discard port makes every probe fail fast and deterministic.
        self._env = mock.patch.dict(os.environ,
                                    {"CHATEKLD_API_URL": "http://127.0.0.1:9"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_status_command(self):
        result = self.runner.invoke(cli, ["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("CHERQAiN Status", result.output)
        # C3: the printer reads keys that actually exist in the shape.
        self.assertNotIn("Status: None", result.output)
        self.assertIn("Server: not running", result.output)

    def test_vault_search_prints_source_and_label_never_none(self):
        """The rows are the chat ``retrieval`` frame's ``{source, score,
        is_image, paper?}``; the printer used to read ``file_path``/``text``
        and printed ``None`` for every row (review 2026-09-23). Mocked at the
        DRIVER seam, never above it."""
        res = {"status": "ok", "query": "q", "count": 1, "answer": "",
               "results": [{"source": "Z_attachments/biblio_articles/smith_2020.pdf",
                            "score": 0.91, "is_image": False,
                            "paper": {"key": "smithKey2020",
                                      "label": "Smith, 2020, A Title"}}]}
        with mock.patch("cli.main.vault_search", return_value=res):
            result = self.runner.invoke(cli, ["vault", "search", "q"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Z_attachments/biblio_articles/smith_2020.pdf", result.output)
        self.assertIn("Smith, 2020, A Title", result.output)
        self.assertIn("0.91", result.output)
        self.assertNotIn("None", result.output)

    def test_status_command_json(self):
        result = self.runner.invoke(cli, ["status", "--json-out"])
        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.output)
        self.assertEqual(data["status"], "ok")
        # The in-process fallback returns the RICH vault_index shape (C3).
        self.assertIsInstance(data["vault_index"], dict)
        self.assertIn("state", data["vault_index"])

    def test_config_command(self):
        result = self.runner.invoke(cli, ["config"])
        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.output)
        self.assertIn("provider", data)

    def test_paper_summarize_missing_file(self):
        result = self.runner.invoke(cli, ["paper", "summarize", "/invalid/path.pdf"])
        self.assertNotEqual(result.exit_code, 0)

    def test_deck_generate_requires_template(self):
        # C1: --template is a required option now — the old command could
        # never succeed (the route hard-400s without template_tex).
        result = self.runner.invoke(cli, ["deck", "generate", "Topic"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--template", result.output)

    def test_docgen_latex_requires_template(self):
        result = self.runner.invoke(
            cli, ["docgen", "generate", "Topic", "--mode", "latex"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--template", result.output)

    def test_vault_index_has_no_force_flag(self):
        # C1: --force sent a body key the server never read; it must be gone
        # rather than silently inert.
        result = self.runner.invoke(cli, ["vault", "index", "--force"])
        self.assertNotEqual(result.exit_code, 0)

    def test_server_refuses_non_loopback_bind(self):
        # C5: --unsafe-bind removed; a non-loopback host is a hard error
        # (the origin guard fail-closes on remote clients anyway).
        result = self.runner.invoke(cli, ["server", "--host", "0.0.0.0"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("origin guard", result.output)

    def test_server_refuses_second_instance(self):
        # C0: two servers = two independent op-locks over one vault.
        from core import server_info
        server_info.write_server_info(43219)
        # Fake the recorded pid as alive + healthy.
        with mock.patch("core.server_info.pid_alive", return_value=True), \
                mock.patch("core.driver.is_server_running", return_value=True):
            try:
                result = self.runner.invoke(cli, ["server"])
            finally:
                try:
                    os.unlink(server_info.SERVER_INFO_FILE)
                except OSError:
                    pass
        self.assertEqual(result.exit_code, 1)
        self.assertIn("already running", result.output)


class TestTaggingBatchTargets(unittest.TestCase):
    """`papermind tagging batch` target selection (2026-08-12 review, H1/L5).

    The names are patched on ``cli.main`` because the CLI binds the driver
    functions at import time (``from core.driver import ...``).
    """

    def setUp(self):
        self.runner = CliRunner()
        self._env = mock.patch.dict(os.environ,
                                    {"CHATEKLD_API_URL": "http://127.0.0.1:9"})
        self._env.start()
        self.addCleanup(self._env.stop)

    @staticmethod
    def _inv(rows):
        return {"rows": rows, "count": len(rows),
                "pdfless_count": sum(1 for r in rows if r.get("has_pdf") is False)}

    @staticmethod
    def _row(key, *, has_pdf=True, finder=(), note_tags=(), zot_tags=()):
        return {
            "citation_key": key, "has_pdf": has_pdf,
            "finder_tags": list(finder),
            "note": {"present": True, "tags": list(note_tags)},
            "zotero": {"present": True, "parent_tags": list(zot_tags)},
        }

    def test_unknown_target_is_rejected_before_any_call(self):
        with mock.patch("cli.main.tagging_inventory") as inv:
            result = self.runner.invoke(cli, ["tagging", "batch",
                                              "--targets", "notes"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Unknown target", result.output)
        inv.assert_not_called()

    def test_unknown_target_is_rejected_for_single_apply_too(self):
        with mock.patch("cli.main.tagging_apply") as ap:
            result = self.runner.invoke(cli, ["tagging", "apply", "smith2020",
                                              "--tags", "method",
                                              "--targets", "zotero,noet"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("noet", result.output)
        ap.assert_not_called()

    def test_finder_only_over_pdfless_rows_costs_no_llm_calls(self):
        """The H1 repro: `--targets finder --no-pdf-entries`.

        Every selected row would have had its only requested target dropped,
        leaving an empty list that the driver used to serialise as "all targets
        ON" — writing note + Zotero tags library-wide. Selection must now come
        out empty, and no propose call may be made.
        """
        rows = [self._row("a2020", has_pdf=False),
                self._row("b2021", has_pdf=False)]
        with mock.patch("cli.main.tagging_inventory",
                        return_value=self._inv(rows)), \
                mock.patch("cli.main.tagging_propose") as prop, \
                mock.patch("cli.main.tagging_apply") as ap:
            result = self.runner.invoke(cli, ["tagging", "batch",
                                              "--no-pdf-entries",
                                              "--targets", "finder"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("To process: 0", result.output)
        prop.assert_not_called()
        ap.assert_not_called()

    def test_pdfless_row_applies_without_the_finder_target(self):
        rows = [self._row("a2020", has_pdf=False)]
        sent = {}

        def _apply(key, tags, targets):
            sent["targets"] = targets
            return {"results": {"note": {"ok": True}}}

        with mock.patch("cli.main.tagging_inventory",
                        return_value=self._inv(rows)), \
                mock.patch("cli.main.tagging_propose",
                           return_value={"proposal": {
                               "proposed": [{"tag": "method", "confidence": 1.0}],
                               "status": "ok", "source": "metadata"}}), \
                mock.patch("cli.main.tagging_apply", side_effect=_apply):
            result = self.runner.invoke(cli, ["tagging", "batch",
                                              "--no-pdf-entries", "--sleep", "0"])
        self.assertEqual(result.exit_code, 0)
        # Non-empty and finder-free: the server would skip finder with a reason,
        # but an EMPTY list is what used to invert into all-targets-on.
        self.assertEqual(sent["targets"], ["note", "zotero"])

    def test_selection_is_scoped_to_the_requested_targets(self):
        # `--targets note` must not select a paper whose note is already tagged
        # just because its Finder tags are empty (a full propose + a no-op union).
        rows = [self._row("done2020", finder=(), note_tags=("method",),
                          zot_tags=("method",))]
        with mock.patch("cli.main.tagging_inventory",
                        return_value=self._inv(rows)), \
                mock.patch("cli.main.tagging_propose") as prop:
            result = self.runner.invoke(cli, ["tagging", "batch",
                                              "--targets", "note"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("To process: 0", result.output)
        prop.assert_not_called()

    def test_include_tagged_without_a_selection_is_a_usage_error(self):
        """An unset selection is never "everything" (the `_parse_targets` rule).

        Without the guard the flag would drop the already-tagged gate over the
        WHOLE inventory, i.e. re-propose every paper in the library.
        """
        with mock.patch("cli.main.tagging_inventory") as inv:
            result = self.runner.invoke(cli, ["tagging", "batch",
                                              "--include-tagged"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--include-tagged needs an explicit", result.output)
        inv.assert_not_called()

    def test_include_tagged_reaches_an_already_tagged_row(self):
        """The under-tagged cohort: one tag on every surface, still selectable."""
        rows = [self._row("done2020", finder=("cog",), note_tags=("cog",),
                          zot_tags=("cog",))]
        inv = self._inv(rows)
        with mock.patch("cli.main.tagging_inventory", return_value=inv), \
                mock.patch("cli.main.tagging_propose") as prop:
            # Without the flag the row is gated out ...
            gated = self.runner.invoke(cli, ["tagging", "batch",
                                             "--keys", "done2020", "--dry-run"])
        self.assertEqual(gated.exit_code, 0)
        self.assertIn("To process: 0", gated.output)
        prop.assert_not_called()

        with mock.patch("cli.main.tagging_inventory", return_value=inv), \
                mock.patch("cli.main.tagging_propose",
                           return_value={"proposal": {
                               "proposed": [{"tag": "scz", "confidence": 1.0}],
                               "status": "ok", "source": "pdf"}}) as prop2:
            # ... and reaches the work list with it.
            result = self.runner.invoke(cli, ["tagging", "batch",
                                              "--keys", "done2020",
                                              "--include-tagged", "--dry-run",
                                              "--sleep", "0"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("To process: 1", result.output)
        prop2.assert_called_once()


class TestTaggingBatchSnapshotAndBackoff(unittest.TestCase):
    """Snapshot-age disclosure + propose backoff (2026-08-12 review, H2/M4)."""

    def setUp(self):
        self.runner = CliRunner()
        self._env = mock.patch.dict(os.environ,
                                    {"CHATEKLD_API_URL": "http://127.0.0.1:9"})
        self._env.start()
        self.addCleanup(self._env.stop)

    @staticmethod
    def _row(key):
        return {"citation_key": key, "has_pdf": True, "finder_tags": [],
                "note": {"present": True, "tags": []},
                "zotero": {"present": True, "parent_tags": []}}

    def test_stale_snapshot_is_disclosed_with_the_rescan_remedy(self):
        """The H2 disclosure: 'already tagged' comes from a frozen snapshot.

        No tagging write updates the cached audit Record, so a batch re-run
        straight after an interrupted one re-proposes everything. The age plus
        the remedy is what makes that visible instead of silently expensive.
        """
        import time as _t
        inv = {"rows": [], "count": 0, "pdfless_count": 0,
               "scanned_at": _t.time() - 3 * 3600}
        with mock.patch("cli.main.tagging_inventory", return_value=inv):
            result = self.runner.invoke(cli, ["tagging", "batch"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Snapshot: audit scan from", result.output)
        self.assertIn("3 h ago", result.output)
        self.assertIn("papermind audit scan", result.output)

    def test_fresh_snapshot_does_not_nag(self):
        import time as _t
        inv = {"rows": [], "count": 0, "pdfless_count": 0,
               "scanned_at": _t.time() - 60}
        with mock.patch("cli.main.tagging_inventory", return_value=inv):
            result = self.runner.invoke(cli, ["tagging", "batch"])
        self.assertIn("Snapshot: audit scan from", result.output)
        self.assertNotIn("NOT reflected", result.output)

    def test_a_server_without_the_field_still_says_it_may_be_stale(self):
        inv = {"rows": [], "count": 0, "pdfless_count": 0}
        with mock.patch("cli.main.tagging_inventory", return_value=inv):
            result = self.runner.invoke(cli, ["tagging", "batch"])
        self.assertIn("scan time unknown", result.output)

    def test_a_wedged_provider_aborts_instead_of_sweeping_the_library(self):
        """M4: one stuck propose used to fail every remaining paper at ~1/s."""
        rows = [self._row(f"k{i}") for i in range(40)]
        inv = {"rows": rows, "count": len(rows), "pdfless_count": 0,
               "scanned_at": 0}
        calls = []

        def _propose(key):
            calls.append(key)
            return {"error": "Propose busy (429)."}

        with mock.patch("cli.main.tagging_inventory", return_value=inv), \
                mock.patch("cli.main.tagging_propose", side_effect=_propose), \
                mock.patch("time.sleep") as slept:
            result = self.runner.invoke(cli, ["tagging", "batch", "--sleep", "0.5"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Aborting", result.output)
        # Stopped at the cap instead of walking all 40 papers.
        self.assertEqual(len(calls), 5)
        # And it backed off between attempts rather than retrying at --sleep.
        waits = [c.args[0] for c in slept.call_args_list]
        self.assertTrue(any(w > 0.5 for w in waits), waits)

    def test_a_transient_failure_does_not_latch(self):
        # One failure followed by successes must not count toward the abort.
        rows = [self._row(f"k{i}") for i in range(4)]
        inv = {"rows": rows, "count": len(rows), "pdfless_count": 0,
               "scanned_at": 0}
        seen = []

        def _propose(key):
            seen.append(key)
            if len(seen) == 1:
                return {"error": "transient"}
            return {"proposal": {"proposed": [{"tag": "method", "confidence": 1.0}],
                                 "status": "ok", "source": "pdf"}}

        with mock.patch("cli.main.tagging_inventory", return_value=inv), \
                mock.patch("cli.main.tagging_propose", side_effect=_propose), \
                mock.patch("cli.main.tagging_apply",
                           return_value={"results": {"note": {"ok": True}}}), \
                mock.patch("time.sleep"):
            result = self.runner.invoke(cli, ["tagging", "batch", "--sleep", "0"])
        self.assertEqual(len(seen), 4)
        self.assertIn("3 applied, 0 skipped, 1 failed", result.output)
        self.assertNotEqual(result.exit_code, 0)  # exits 1 when anything failed


if __name__ == "__main__":
    unittest.main()


class TestTaggingBatchKeySelection(unittest.TestCase):
    """`--keys` / `--keys-file` subset selection (2026-08-13).

    Before this, the only narrowing was `--limit`, a head slice of a list
    sorted by (year, key) — so "tag these twelve" was impossible, and repeated
    limited runs re-proposed the SAME oldest papers because no tag write
    refreshes the audit snapshot the selection reads.
    """

    def setUp(self):
        self.runner = CliRunner()
        self._env = mock.patch.dict(os.environ,
                                    {"CHATEKLD_API_URL": "http://127.0.0.1:9"})
        self._env.start()
        self.addCleanup(self._env.stop)

    @staticmethod
    def _row(key):
        return {
            "citation_key": key, "has_pdf": True, "finder_tags": [],
            "note": {"present": True, "tags": []},
            "zotero": {"present": True, "parent_tags": []},
        }

    def _inv(self, keys):
        rows = [self._row(k) for k in keys]
        return {"rows": rows, "count": len(rows), "pdfless_count": 0}

    def _run(self, args, keys=("a2020", "b2021", "c2022")):
        # The propose mock returns the DRIVER shape ({"proposal": {...}}), not
        # the route's unwrapped body — the pre-2026-08-18 mock used the route
        # shape, so every test here silently exercised only the no-tags skip
        # path (the "mock above the seam" class the review documents).
        with mock.patch("cli.main.tagging_inventory",
                        return_value=self._inv(keys)), \
             mock.patch("cli.main.tagging_propose",
                        return_value={"proposal": {
                            "proposed": [{"tag": "method", "confidence": 1.0}],
                            "status": "ok", "source": "pdf"}}) as prop, \
             mock.patch("cli.main.tagging_apply", return_value={}) as ap, \
             mock.patch("cli.main.tagging_model_info", return_value={}):
            result = self.runner.invoke(
                cli, ["tagging", "batch", "--dry-run", "--sleep", "0"] + args)
        return result, prop, ap

    def test_keys_narrows_to_the_named_papers(self):
        result, prop, _ap = self._run(["--keys", "a2020,c2022"])
        self.assertEqual(result.exit_code, 0, result.output)
        called = sorted(c.args[0] for c in prop.call_args_list)
        self.assertEqual(called, ["a2020", "c2022"])
        # Proves the propose path is genuinely exercised (not the skip path):
        # a dry run over a real proposal prints its would-apply line.
        self.assertIn("would apply", result.output)

    def test_keys_file_is_read_with_comments_and_blanks_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "keys.txt"
            f.write_text("# exported from CHERQAiN\n\nb2021\n  c2022  # note\n",
                         encoding="utf-8")
            result, prop, _ap = self._run(["--keys-file", str(f)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(sorted(c.args[0] for c in prop.call_args_list),
                         ["b2021", "c2022"])

    def test_keys_and_keys_file_are_unioned(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "keys.txt"
            f.write_text("c2022\n", encoding="utf-8")
            result, prop, _ap = self._run(["--keys", "a2020", "--keys-file", str(f)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(sorted(c.args[0] for c in prop.call_args_list),
                         ["a2020", "c2022"])

    def test_an_unmatched_key_is_named_not_silently_dropped(self):
        # A typo'd or stale key would otherwise look like "already tagged".
        result, prop, _ap = self._run(["--keys", "a2020,ghost1999"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("ghost1999", result.output)
        self.assertEqual([c.args[0] for c in prop.call_args_list], ["a2020"])

    def test_keys_file_with_a_bom_still_matches_its_first_key(self):
        """A BOM glued the (invisible) \\ufeff to the first key, which then
        matched nothing and was printed unreadably in the diagnostic."""
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "keys.txt"
            f.write_bytes(b"\xef\xbb\xbfb2021\nc2022\n")  # BOM + two keys
            result, prop, _ap = self._run(["--keys-file", str(f)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(sorted(c.args[0] for c in prop.call_args_list),
                         ["b2021", "c2022"])

    def test_a_non_utf8_keys_file_is_a_usage_error_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "keys.txt"
            f.write_bytes(b"\xff\xfe broken \x00 latin\n")
            result, _prop, _ap = self._run(["--keys-file", str(f)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("UTF-8", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_a_key_removed_by_the_pdf_filter_blames_the_filter_not_scope(self):
        """`--no-with-pdf --keys X` where X HAS a PDF: X is in scope=all —
        the client-side has_pdf filter removed it, and the diagnostic must
        say so instead of 'outside scope' (2026-08-18 review)."""
        rows = [dict(self._row("a2020"), has_pdf=True),
                dict(self._row("orphan2021"), has_pdf=False)]
        inv = {"rows": rows, "count": 2, "pdfless_count": 1}
        with mock.patch("cli.main.tagging_inventory", return_value=inv), \
             mock.patch("cli.main.tagging_propose",
                        return_value={"proposal": {"proposed": [],
                                                   "status": "empty"}}), \
             mock.patch("cli.main.tagging_apply", return_value={}), \
             mock.patch("cli.main.tagging_model_info", return_value={}):
            result = self.runner.invoke(
                cli, ["tagging", "batch", "--dry-run", "--sleep", "0",
                      "--no-with-pdf", "--no-pdf-entries",
                      "--keys", "a2020,ghost1999"])
        self.assertEqual(result.exit_code, 0, result.output)
        out = result.output
        # a2020 was filtered client-side; ghost1999 is genuinely unknown.
        self.assertIn("filter", out)
        self.assertIn("a2020", out.split("Not in the inventory")[0])
        self.assertIn("ghost1999", out)

    def test_an_empty_selection_is_refused_not_read_as_everything(self):
        # The empty-vs-unset distinction that already bit --targets on the
        # write path: an empty --keys-file must never mean "the whole library".
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "empty.txt"
            f.write_text("# nothing here\n\n", encoding="utf-8")
            with mock.patch("cli.main.tagging_inventory") as inv:
                result = self.runner.invoke(
                    cli, ["tagging", "batch", "--keys-file", str(f)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("empty", result.output.lower())
        inv.assert_not_called()

    def test_no_selection_flags_processes_the_whole_scope(self):
        result, prop, _ap = self._run([])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(prop.call_args_list), 3)

    def test_the_model_is_named_before_the_spend(self):
        # A batch is the app's largest single LLM spend and nothing anywhere
        # said which model it used.
        with mock.patch("cli.main.tagging_inventory",
                        return_value=self._inv(["a2020"])), \
             mock.patch("cli.main.tagging_propose",
                        return_value={"proposal": {"proposed": [],
                                                   "status": "empty"}}), \
             mock.patch("cli.main.tagging_apply", return_value={}), \
             mock.patch("cli.main.tagging_model_info",
                        return_value={"provider": "openai",
                                      "tagging_model": "gpt-5-mini"}):
            result = self.runner.invoke(
                cli, ["tagging", "batch", "--dry-run", "--sleep", "0"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("gpt-5-mini", result.output)
        self.assertIn("openai", result.output)

    def test_the_disclosure_never_touches_the_draining_status_feed(self):
        """2026-08-18 review: the disclosure used get_status(), which GETs
        /api/obsidian/status — a DESTRUCTIVE drain of the one-shot vault
        status feed the GUI polls — for two config fields. It must read
        config only."""
        with mock.patch("cli.main.tagging_inventory",
                        return_value=self._inv(["a2020"])), \
             mock.patch("cli.main.tagging_propose",
                        return_value={"proposal": {"proposed": [],
                                                   "status": "empty"}}), \
             mock.patch("cli.main.tagging_apply", return_value={}), \
             mock.patch("cli.main.tagging_model_info",
                        return_value={"provider": "openai",
                                      "tagging_model": "m"}), \
             mock.patch("cli.main.get_status") as gs:
            result = self.runner.invoke(
                cli, ["tagging", "batch", "--dry-run", "--sleep", "0"])
        self.assertEqual(result.exit_code, 0, result.output)
        gs.assert_not_called()

    def test_a_model_lookup_failure_does_not_stop_the_run(self):
        with mock.patch("cli.main.tagging_inventory",
                        return_value=self._inv(["a2020"])), \
             mock.patch("cli.main.tagging_propose",
                        return_value={"proposal": {"proposed": [],
                                                   "status": "empty"}}) as prop, \
             mock.patch("cli.main.tagging_apply", return_value={}), \
             mock.patch("cli.main.tagging_model_info",
                        side_effect=RuntimeError("down")):
            result = self.runner.invoke(
                cli, ["tagging", "batch", "--dry-run", "--sleep", "0"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(prop.call_args_list), 1)


class TestStatusTaggingModel(unittest.TestCase):
    """`papermind status` reports the TAGGING model when it differs.

    It printed only the chat model, which is the wrong answer precisely when
    `tagging_propose_model` is set — i.e. exactly when the user has expressed
    a preference about which model spends their tokens.
    """

    def setUp(self):
        self.runner = CliRunner()

    def _status(self, cfg):
        with mock.patch("cli.main.get_status",
                        return_value={"status": "ok", "server_running": True,
                                      "config": cfg}):
            return self.runner.invoke(cli, ["status"])

    def test_printed_when_it_differs_from_the_chat_model(self):
        r = self._status({"provider": "openai", "model": "gpt-5",
                          "tagging_model": "gpt-5-mini"})
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("Tagging Model: gpt-5-mini", r.output)

    def test_omitted_when_identical(self):
        # An extra line repeating the one above is noise; a silent divergence
        # is the bug. Only the divergence gets a line.
        r = self._status({"provider": "openai", "model": "gpt-5",
                          "tagging_model": "gpt-5"})
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertNotIn("Tagging Model", r.output)


class TestRunBatchExtraction(unittest.TestCase):
    """tagging/batch.py::run_batch — the loop extracted from the CLI
    (Maintenance Phase 2). The CLI-level behaviour is pinned by the
    TestTaggingBatch* classes above; these cover the seams only the extraction
    added (cancellation, the outcome dataclass)."""

    @staticmethod
    def _row(key):
        return {"citation_key": key, "has_pdf": True, "finder_tags": [],
                "note": {"present": True, "tags": []},
                "zotero": {"present": True, "parent_tags": []}}

    def test_should_cancel_stops_between_papers(self):
        from tagging.batch import BatchOptions, run_batch
        rows = [self._row(f"k{i}") for i in range(5)]
        seen = []

        def _propose(key):
            seen.append(key)
            return {"proposal": {"proposed": [{"tag": "m", "confidence": 1.0}],
                                 "status": "ok", "source": "pdf"}}

        with mock.patch("time.sleep"):
            out = run_batch(
                BatchOptions(sleep_s=0),
                inventory=lambda **kw: {"rows": rows, "count": 5,
                                        "pdfless_count": 0, "scanned_at": 0},
                propose=_propose,
                apply=lambda k, t, w: {"results": {"note": {"ok": True}}},
                echo=lambda *a, **k: None,
                should_cancel=lambda: len(seen) >= 2,
            )
        self.assertTrue(out.cancelled)
        self.assertEqual(len(seen), 2)
        self.assertEqual(out.applied, 2)

    def test_inventory_error_sets_outcome_error(self):
        from tagging.batch import BatchOptions, run_batch
        lines = []
        out = run_batch(
            BatchOptions(),
            inventory=lambda **kw: {"error": "Run a scan first."},
            propose=lambda k: {}, apply=lambda k, t, w: {},
            echo=lambda msg, err=False: lines.append(msg),
        )
        self.assertEqual(out.error, "Run a scan first.")
        self.assertIn("Error: Run a scan first.", lines)


class TestTaggingBatchDepthCohorts(unittest.TestCase):
    """`--min-tags` / `--max-tags` (Batch A, 2026-08-26).

    The selector that makes the under-tagged cohort reachable. Two things must
    hold: the DEFAULT run is byte-identical to before (the historical
    "some requested target has zero tags" gate), and a bounded range needs no
    key list because it cannot select the whole library by accident.
    """

    def setUp(self):
        self.runner = CliRunner()
        self._env = mock.patch.dict(os.environ,
                                    {"CHATEKLD_API_URL": "http://127.0.0.1:9"})
        self._env.start()
        self.addCleanup(self._env.stop)

    @staticmethod
    def _inv(rows):
        return {"rows": rows, "count": len(rows), "pdfless_count": 0}

    @staticmethod
    def _row(key, note_tags=()):
        return {
            "citation_key": key, "has_pdf": True, "finder_tags": list(note_tags),
            "note": {"present": True, "tags": list(note_tags)},
            "zotero": {"present": True, "parent_tags": list(note_tags)},
        }

    def _run(self, args, rows):
        proposed = []

        def _propose(key):
            proposed.append(key)
            return {"proposal": {"proposed": [], "status": "empty",
                                 "source": "pdf"}}

        with mock.patch("cli.main.tagging_inventory",
                        return_value=self._inv(rows)), \
                mock.patch("cli.main.tagging_propose", side_effect=_propose), \
                mock.patch("cli.main.tagging_apply") as ap, \
                mock.patch("time.sleep"):
            result = self.runner.invoke(cli, ["tagging", "batch"] + args)
        return result, proposed, ap

    def _corpus(self):
        return [
            self._row("none2020"),                       # 0 tags
            self._row("one2021", ["dep"]),               # 1
            self._row("two2022", ["dep", "scz"]),        # 2
            self._row("many2023", ["a", "b", "c", "d"]),  # 4
        ]

    def test_max_tags_two_selects_the_under_tagged_cohort(self):
        result, proposed, _ = self._run(["--max-tags", "2", "--dry-run"],
                                        self._corpus())
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(proposed, ["none2020", "one2021", "two2022"])
        self.assertIn("Cohort:", result.output)

    def test_max_tags_zero_is_exactly_the_untagged_cohort(self):
        _result, proposed, _ = self._run(["--max-tags", "0", "--dry-run"],
                                         self._corpus())
        self.assertEqual(proposed, ["none2020"])

    def test_min_tags_selects_the_other_end(self):
        _result, proposed, _ = self._run(["--min-tags", "3", "--dry-run"],
                                         self._corpus())
        self.assertEqual(proposed, ["many2023"])

    def test_a_depth_range_needs_no_key_selection(self):
        """Unlike --include-tagged: a bounded range cannot mean "everything"."""
        result, proposed, _ = self._run(["--max-tags", "1", "--dry-run"],
                                        self._corpus())
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(proposed, ["none2020", "one2021"])

    def test_default_run_is_unchanged_by_the_new_flags_existing(self):
        """The historical gate: a paper whose every present requested target
        carries tags is skipped, whatever its depth."""
        _result, proposed, _ = self._run(["--dry-run"], self._corpus())
        self.assertEqual(proposed, ["none2020"])

    def test_inverted_range_is_a_usage_error_before_any_call(self):
        with mock.patch("cli.main.tagging_inventory") as inv:
            result = self.runner.invoke(cli, ["tagging", "batch",
                                              "--min-tags", "5",
                                              "--max-tags", "2"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("selects nothing", result.output)
        inv.assert_not_called()

    def test_negative_bound_is_a_usage_error(self):
        with mock.patch("cli.main.tagging_inventory") as inv:
            result = self.runner.invoke(cli, ["tagging", "batch",
                                              "--max-tags", "-1"])
        self.assertNotEqual(result.exit_code, 0)
        inv.assert_not_called()

    def test_include_tagged_and_a_depth_range_are_mutually_exclusive(self):
        with mock.patch("cli.main.tagging_inventory") as inv:
            result = self.runner.invoke(cli, ["tagging", "batch",
                                              "--include-tagged",
                                              "--keys", "a2020",
                                              "--max-tags", "2"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("two different", result.output)
        inv.assert_not_called()


class TestNotesyncStatusCommand(unittest.TestCase):
    """`papermind notesync status` (2026-09-04): read-only, exit 1 on a conflict."""

    def _run(self, args, payload):
        from click.testing import CliRunner
        from cli.main import cli
        with mock.patch("cli.main.notesync_status", return_value=payload):
            return CliRunner().invoke(cli, ["notesync", "status", *args])

    def test_prints_counts_and_rows_and_exits_1_on_a_conflict(self):
        payload = {"status": "ok", "total": 3, "counts": {"in_sync": 2, "conflict": 1},
                   "rows": [{"citation_key": "a2020", "state": "conflict",
                             "missing_words": 3, "extra_words": 2},
                            {"citation_key": "b2021", "state": "in_sync",
                             "missing_words": 0, "extra_words": 0}]}
        r = self._run([], payload)
        self.assertEqual(r.exit_code, 1, r.output)
        self.assertIn("conflict 1", r.output)
        self.assertIn("a2020", r.output)
        self.assertNotIn("b2021", r.output)          # in-step rows are counted, not listed

    def test_state_filter_and_json(self):
        payload = {"status": "ok", "total": 2, "counts": {"pull": 1, "push": 1},
                   "rows": [{"citation_key": "a", "state": "pull", "missing_words": 1, "extra_words": 0},
                            {"citation_key": "b", "state": "push", "missing_words": 0, "extra_words": 1}]}
        r = self._run(["--state", "push", "--json"], payload)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertEqual([x["citation_key"] for x in json.loads(r.output)["rows"]], ["b"])

    def test_server_down_exits_2(self):
        r = self._run([], {"error": "The CHERQAiN server is not running."})
        self.assertEqual(r.exit_code, 2)


class TestAuditMappingCli(unittest.TestCase):
    """`papermind audit mapping` / `audit pdf-decision` (2026-09-06): exactly
    one verb, an EMPTY selection is a usage error before any request, and the
    driver is called with the shape the route expects."""

    def setUp(self):
        self.runner = CliRunner()
        self._env = mock.patch.dict(os.environ, {"CHATEKLD_API_URL": "http://127.0.0.1:9"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_exactly_one_verb(self):
        for args in (["audit", "mapping", "--pdfs", "a.pdf"],
                     ["audit", "mapping", "--pdfs", "a.pdf", "--pending", "--no-match"]):
            with mock.patch("cli.main.audit_mapping") as drv:
                r = self.runner.invoke(cli, args)
            self.assertEqual(r.exit_code, 2, r.output)
            drv.assert_not_called()

    def test_empty_selection_is_a_usage_error_not_everything(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "pdfs.txt"
            f.write_text("# only comments\n", encoding="utf-8")
            with mock.patch("cli.main.audit_mapping") as drv:
                r = self.runner.invoke(cli, ["audit", "mapping", "--pdfs-file", str(f),
                                             "--pending"])
        self.assertEqual(r.exit_code, 2, r.output)
        drv.assert_not_called()

    def test_bucket_verb_unions_pdfs_and_file_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "pdfs.txt"
            f.write_text("b.pdf\n# c\nc.pdf\na.pdf\n", encoding="utf-8")
            with mock.patch("cli.main.audit_mapping",
                            return_value={"status": "ok", "action": "pending",
                                          "changed": 3}) as drv:
                r = self.runner.invoke(cli, ["audit", "mapping", "--pdfs", "a.pdf",
                                             "--pdfs-file", str(f), "--pending"])
        self.assertEqual(r.exit_code, 0, r.output)
        drv.assert_called_once_with("pending", ["a.pdf", "b.pdf", "c.pdf"], None)

    def test_link_and_detach_take_one_pdf(self):
        with mock.patch("cli.main.audit_mapping") as drv:
            r = self.runner.invoke(cli, ["audit", "mapping", "--pdfs", "a.pdf,b.pdf",
                                         "--detach", "k2020"])
        self.assertEqual(r.exit_code, 2, r.output)
        drv.assert_not_called()
        with mock.patch("cli.main.audit_mapping",
                        return_value={"status": "ok", "action": "detach"}) as drv:
            r = self.runner.invoke(cli, ["audit", "mapping", "--pdfs", "a.pdf",
                                         "--detach", "k2020"])
        self.assertEqual(r.exit_code, 0, r.output)
        drv.assert_called_once_with("detach", ["a.pdf"], "k2020")

    def test_pdf_decision_verbs(self):
        with mock.patch("cli.main.audit_pdf_decision",
                        return_value={"status": "ok", "verb": "dismiss", "changed": 2}) as drv:
            r = self.runner.invoke(cli, ["audit", "pdf-decision", "--keys", "b,a", "--dismiss"])
        self.assertEqual(r.exit_code, 0, r.output)
        drv.assert_called_once_with("dismiss", ["a", "b"])
        with mock.patch("cli.main.audit_pdf_decision") as drv:
            r = self.runner.invoke(cli, ["audit", "pdf-decision", "--dismiss"])
            self.assertEqual(r.exit_code, 2, r.output)
            r = self.runner.invoke(cli, ["audit", "pdf-decision", "--clear-all", "--keys", "a"])
            self.assertEqual(r.exit_code, 2, r.output)
        drv.assert_not_called()
        with mock.patch("cli.main.audit_pdf_decision",
                        return_value={"status": "ok", "verb": "clear_all", "changed": 5}) as drv:
            r = self.runner.invoke(cli, ["audit", "pdf-decision", "--clear-all"])
        self.assertEqual(r.exit_code, 0, r.output)
        drv.assert_called_once_with("clear_all", None)


class TestPdfLinksCli(unittest.TestCase):
    """`papermind tagging pdf-links` — an UNSET selection is a refusal.

    `_read_key_selection` answers `None` for "no subset", which every other
    command reads as the whole scope. There is no whole-library meaning here:
    the route is bounded by what the restore journal can undo, so treating an
    unset selection as "everything" would offer an undo it cannot keep.
    """

    def setUp(self):
        self.runner = CliRunner()
        self._env = mock.patch.dict(os.environ,
                                    {"CHATEKLD_API_URL": "http://127.0.0.1:9"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_no_selection_is_a_usage_error_before_any_request(self):
        with mock.patch("cli.main.tagging_pdf_links") as drv:
            r = self.runner.invoke(cli, ["tagging", "pdf-links"])
        self.assertEqual(r.exit_code, 2, r.output)
        drv.assert_not_called()

    def test_an_empty_file_is_also_refused(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "keys.txt"
            f.write_text("# nothing here\n", encoding="utf-8")
            with mock.patch("cli.main.tagging_pdf_links") as drv:
                r = self.runner.invoke(cli, ["tagging", "pdf-links",
                                             "--keys-file", str(f)])
        self.assertEqual(r.exit_code, 2, r.output)
        drv.assert_not_called()

    def test_keys_reach_the_driver_sorted_and_deduped(self):
        with mock.patch("cli.main.tagging_pdf_links",
                        return_value={"status": "ok", "written": 2}) as drv:
            r = self.runner.invoke(cli, ["tagging", "pdf-links",
                                         "--keys", "b2020, a2020 ,b2020"])
        self.assertEqual(r.exit_code, 0, r.output)
        drv.assert_called_once_with(["a2020", "b2020"])


class TestZotlinkCli(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self._env = mock.patch.dict(os.environ, {"CHATEKLD_API_URL": "http://127.0.0.1:9"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_attach_requires_a_selection_never_the_whole_library(self):
        with mock.patch("cli.main.zotlink_attach") as drv:
            r = self.runner.invoke(cli, ["zotlink", "attach"])
        self.assertEqual(r.exit_code, 2, r.output)
        drv.assert_not_called()

    def test_attach_passes_sorted_keys_and_dry_run(self):
        with mock.patch("cli.main.zotlink_attach",
                        return_value={"status": "ok", "attached": 0, "dry_run": True}) as drv:
            r = self.runner.invoke(cli, ["zotlink", "attach", "--keys", "b,a", "--dry-run"])
        self.assertEqual(r.exit_code, 0, r.output)
        args, kwargs = drv.call_args
        self.assertEqual(args[0], ["a", "b"])
        self.assertTrue(kwargs["dry_run"])

    def test_candidates_prints_the_counts(self):
        with mock.patch("cli.main.zotlink_attach_candidates",
                        return_value={"status": "ok", "count": 3, "to_link": 2,
                                      "group_count": 1, "transport": "web",
                                      "can_write": True, "path_form": "absolute",
                                      "batch_max": 200,
                                      "rows": [{"citation_key": "a", "to_link": 2}]}):
            r = self.runner.invoke(cli, ["zotlink", "candidates"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("PDFs not yet linked in Zotero:      2", r.output)
        self.assertIn("a  (2 to link)", r.output)


class TestNotesyncCampaignBounds(unittest.TestCase):
    """2026-09-17 review, E1: the server's 1–100 bounds are enforced before
    any request, like ``_parse_targets``."""

    def setUp(self):
        self.runner = CliRunner()

    def test_plan_refuses_more_than_100_keys_before_any_request(self):
        args = ["notesync", "plan"] + [a for i in range(101) for a in ("--key", f"k{i}")]
        with mock.patch("cli.main.notesync_campaign") as call:
            result = self.runner.invoke(cli, args)
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("at most 100", result.output)
        call.assert_not_called()

    def test_apply_refuses_an_empty_or_oversized_decision_list(self):
        with tempfile.TemporaryDirectory() as td:
            for payload in ([], [{"citation_key": f"k{i}"} for i in range(101)], {"not": "a list"}):
                path = Path(td) / "decisions.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with mock.patch("cli.main.notesync_campaign") as call:
                    result = self.runner.invoke(cli, ["notesync", "apply", "--plan", "p", "--decisions", str(path), "--confirm"])
                self.assertEqual(result.exit_code, 2, result.output)
                call.assert_not_called()
