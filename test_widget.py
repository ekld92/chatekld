"""Tests for the widget snapshot subsystem (Lot 2, 2026-08-06).

Three layers, all hermetic (no scan, no models, files under a per-test tmp):

* the pure builder (``widget/snapshot.py``) — deterministic under a seed,
  anti-repeat with the all-in-history fallback, the excerpt chain
  (summary-callout → PDF-text → none), biblio-scoped suggestion counts;
* the store (``widget/store.py``) — atomic write/read round-trip, corrupt /
  wrong-version degrade to ``None``, history cap + dedupe;
* the routes + scan listener (``api/routes/widget.py``) — 404/409 pre-scan,
  refresh builds + persists + serves, the result listener no-ops on a
  cleared inventory and NEVER starts a scan.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flask

from audit.engine.inventory import Record
from widget import snapshot as w_snapshot
from widget import store as w_store
from widget.snapshot import build_payload, summary_callout_excerpt
from widget.textclean import clean_prose, delatex, looks_like_latex

_HEADERS = {"X-Requested-With": "ChatEKLD"}
_NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


def _record(key, *, title=None, note=None, biblio_pdf=True, finder=(),
            note_tags=(), vault=Path("/vault")):
    rec = Record(citation_key=key)
    rec.bib_entry = SimpleNamespace(
        title=title or f"Title of {key}", authors=[f"Author-{key}"],
        year="2020")
    if note is not None:
        rec.obsidian_note = SimpleNamespace(
            path=note, tags=list(note_tags))
    base = vault / "Z_attachments" / "biblio_articles" if biblio_pdf \
        else vault / "elsewhere"
    rec.pdf_paths = [base / f"{key}.pdf"]
    rec.finder_tag_set = set(finder)
    return rec


def _settings(vault=Path("/vault")):
    return SimpleNamespace(
        vault_root=vault,
        biblio_articles_dir=vault / "Z_attachments" / "biblio_articles")


def _inv(*records):
    return SimpleNamespace(records={r.citation_key: r for r in records})


class TestSummaryCalloutExcerpt(unittest.TestCase):
    def test_extracts_callout_body(self):
        text = ("---\ntags: []\n---\nintro\n\n"
                "> [!summary] Key points\n> - one\n> - two\n\nafter\n")
        self.assertEqual(summary_callout_excerpt(text),
                         "Key points - one - two")

    def test_none_without_callout(self):
        self.assertIsNone(summary_callout_excerpt("just a note\n> quote\n"))
        self.assertIsNone(summary_callout_excerpt(""))


class TestDelatex(unittest.TestCase):
    """The bib fields reach the payload as LaTeX source (see textclean)."""

    def test_accents_and_special_letters(self):
        for src, want in (
            (r"Approches computationnelles de la schizophr\'enie",
             "Approches computationnelles de la schizophrénie"),
            (r"{\'E}tude sur l'anh{\'e}donie", "Étude sur l'anhédonie"),
            (r'Sch\"onberg & M\"uller', "Schönberg & Müller"),
            (r"Fran\c{c}ois Dub\'e", "François Dubé"),
            (r"Erd\H{o}s, Sk\l odowska, \O stergaard, Wei\ss",
             "Erdős, Skłodowska, Østergaard, Weiß"),
            (r"L\'{\i}nea de base", "Línea de base"),
        ):
            self.assertEqual(delatex(src), want)

    def test_escapes_math_wrappers_and_ligatures(self):
        self.assertEqual(delatex(r"A 50\% response, cost \$5 \& up"),
                         "A 50% response, cost $5 & up")
        self.assertEqual(delatex(r"The 5-HT$_{2A}$ receptor"),
                         "The 5-HT_2A receptor")
        self.assertEqual(delatex(r"\emph{In vivo} imaging of \textbf{cortex}"),
                         "In vivo imaging of cortex")
        self.assertEqual(delatex(r"Deep--brain stimulation---a review"),
                         "Deep–brain stimulation—a review")
        self.assertEqual(delatex(r"``Quoted'' title"), "“Quoted” title")

    def test_unknown_macro_keeps_its_argument(self):
        self.assertEqual(delatex(r"Trial of \unknownmacro{ketamine}"),
                         "Trial of ketamine")

    def test_total_on_junk(self):
        self.assertEqual(delatex(""), "")
        self.assertEqual(delatex(None), "")
        self.assertEqual(delatex("plain text"), "plain text")

    def test_prose_is_untouched_unless_it_is_latex(self):
        # PDF/note prose legitimately carries $ and ~; the gate leaves it be.
        prose = "Coûts de $5 à $10, ~50% des cas — Méd. 2018, 105-114"
        self.assertFalse(looks_like_latex(prose))
        self.assertEqual(clean_prose(prose), prose)
        self.assertEqual(clean_prose(r"un \'etat de base"), "un état de base")


class TestBuildPayload(unittest.TestCase):
    def test_paper_fields_are_delatexed(self):
        rec = _record("jardri2018",
                      title=r"Approches computationnelles de la schizophr\'enie")
        rec.bib_entry.authors = [r"Jardri, Renaud", r"Dub\'e, Fran\c{c}ois"]
        p = build_payload(_inv(rec), _settings(), now=_NOW)
        self.assertEqual(p["paper"]["title"],
                         "Approches computationnelles de la schizophrénie")
        self.assertEqual(p["paper"]["authors"],
                         ["Jardri, Renaud", "Dubé, François"])

    def test_author_line_short_form(self):
        from widget.snapshot import format_authors
        self.assertIsNone(format_authors([]))
        self.assertEqual(format_authors(["Jardri, Renaud"]), "Jardri")
        self.assertEqual(format_authors(["Jardri, Renaud", "Dubé, François"]),
                         "Jardri & Dubé")
        self.assertEqual(
            format_authors(["Jardri, R", "Dubé, F", "Roux, P"]),
            "Jardri et al.")
        # No comma → the last whitespace token is the surname.
        self.assertEqual(format_authors(["Renaud Jardri"]), "Jardri")

    def test_note_uri_is_a_deep_link_or_none(self):
        note = Path("/vault/Z_Zotero_Notes/a b.md")
        p = build_payload(_inv(_record("a", note=note)), _settings(), now=_NOW)
        self.assertEqual(
            p["paper"]["note_uri"],
            "obsidian://open?path=%2Fvault%2FZ_Zotero_Notes%2Fa%20b.md")
        # A paper with no note gets an honest null, not a broken link.
        p = build_payload(_inv(_record("a")), _settings(), now=_NOW)
        self.assertIsNone(p["paper"]["note_uri"])

    def test_excerpt_clips_on_a_word_boundary(self):
        tmp = Path(tempfile.mkdtemp(prefix="widget-clip-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        note = tmp / "a.md"
        note.write_text("> [!summary] " + "computationnelle " * 60,
                        encoding="utf-8")
        p = build_payload(_inv(_record("a", note=note)), _settings(), now=_NOW)
        excerpt = p["paper"]["excerpt"]
        self.assertLessEqual(len(excerpt), 400)
        self.assertTrue(excerpt.endswith("computationnelle…"), excerpt)

    def test_deterministic_under_seed_and_date_default(self):
        inv = _inv(_record("a"), _record("b"), _record("c"))
        kw = dict(now=_NOW)
        p1 = build_payload(inv, _settings(), **kw)
        p2 = build_payload(inv, _settings(), **kw)
        self.assertEqual(p1["paper"]["key"], p2["paper"]["key"])
        # A different seed can pick differently; same explicit seed cannot.
        p3 = build_payload(inv, _settings(), now=_NOW, rng_seed="other-seed")
        p4 = build_payload(inv, _settings(), now=_NOW, rng_seed="other-seed")
        self.assertEqual(p3["paper"]["key"], p4["paper"]["key"])
        self.assertEqual(p1["version"], 1)
        json.dumps(p1)  # payload must be JSON-able

    def test_history_excludes_until_pool_would_empty(self):
        inv = _inv(_record("a"), _record("b"))
        p = build_payload(inv, _settings(), now=_NOW, history=["a"])
        self.assertEqual(p["paper"]["key"], "b")
        # Every candidate in history → history ignored, never an empty pool.
        p = build_payload(inv, _settings(), now=_NOW, history=["a", "b"])
        self.assertIn(p["paper"]["key"], {"a", "b"})

    def test_excerpt_chain(self):
        tmp = Path(tempfile.mkdtemp(prefix="widget-note-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        note = tmp / "a.md"
        note.write_text("> [!summary] The gist\n> bullet\n", encoding="utf-8")
        inv = _inv(_record("a", note=note))
        p = build_payload(inv, _settings(), now=_NOW)
        self.assertEqual(p["paper"]["excerpt_source"], "summary")
        self.assertIn("The gist", p["paper"]["excerpt"])

        # No callout → the injected PDF-text reader is the fallback.
        note.write_text("no callout here\n", encoding="utf-8")
        p = build_payload(inv, _settings(), now=_NOW,
                          pdf_text_reader=lambda _p: "first page words " * 40)
        self.assertEqual(p["paper"]["excerpt_source"], "pdf")
        self.assertLessEqual(len(p["paper"]["excerpt"]), 400)

        # Neither → no excerpt, an honest null.
        p = build_payload(inv, _settings(), now=_NOW)
        self.assertIsNone(p["paper"]["excerpt"])
        self.assertIsNone(p["paper"]["excerpt_source"])

    def test_suggestions_counts_are_biblio_scoped(self):
        inv = _inv(
            _record("untagged1"),                              # counts
            _record("tagged", finder={"dep"}),                 # has Finder tags
            _record("noted", note=Path("/vault/n.md"),
                    note_tags=["dep"]),                        # has note tags
            _record("outside", biblio_pdf=False),              # not biblio
        )
        p = build_payload(inv, _settings(), now=_NOW)
        primary = p["suggestions"][0]
        self.assertEqual(primary["kind"], "tag_batch")
        self.assertEqual(primary["count"], 1)
        self.assertEqual(primary["batch"], 1)
        kinds = {s["kind"] for s in p["suggestions"]}
        self.assertLessEqual({"finder_drift", "note_drift", "unread"}, kinds)

    def test_notion_is_seeded_and_optional(self):
        inv = _inv(_record("a"))
        vocab = {"dep": "Depression", "empty": "", "scz": "Schizophrenia"}
        p1 = build_payload(inv, _settings(), now=_NOW, notions=vocab)
        p2 = build_payload(inv, _settings(), now=_NOW, notions=vocab)
        self.assertEqual(p1["notion"], p2["notion"])
        self.assertIn(p1["notion"]["tag"], {"dep", "scz"})  # gloss-less skipped
        self.assertIsNone(build_payload(inv, _settings(), now=_NOW)["notion"])

    def test_empty_inventory_is_valid(self):
        p = build_payload(_inv(), _settings(), now=_NOW)
        self.assertIsNone(p["paper"])
        self.assertEqual(p["suggestions"][0]["count"], 0)
        json.dumps(p)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="widget-store-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.p_widget = mock.patch.object(
            w_store, "WIDGET_FILE", str(self.tmp / "widget.json"))
        self.p_hist = mock.patch.object(
            w_store, "HISTORY_FILE", str(self.tmp / "widget_history.json"))
        self.p_widget.start()
        self.p_hist.start()
        self.addCleanup(self.p_widget.stop)
        self.addCleanup(self.p_hist.stop)

    def test_round_trip_and_degrade(self):
        self.assertIsNone(w_store.read_snapshot())
        w_store.write_snapshot({"version": 1, "paper": None})
        self.assertEqual(w_store.read_snapshot()["version"], 1)
        Path(w_store.WIDGET_FILE).write_text("{not json", encoding="utf-8")
        self.assertIsNone(w_store.read_snapshot())
        Path(w_store.WIDGET_FILE).write_text(
            json.dumps({"version": 99}), encoding="utf-8")
        self.assertIsNone(w_store.read_snapshot())

    def test_history_cap_and_dedupe(self):
        for i in range(20):
            w_store.append_history(f"k{i}")
        keys = w_store.load_history()
        self.assertEqual(len(keys), 14)
        self.assertEqual(keys[-1], "k19")
        w_store.append_history("k10")  # re-shown → moves to most recent
        keys = w_store.load_history()
        self.assertEqual(keys[-1], "k10")
        self.assertEqual(keys.count("k10"), 1)


class TestWidgetRoutes(unittest.TestCase):
    def setUp(self):
        import api.routes.widget as widget_routes

        self.mod = widget_routes
        self.tmp = Path(tempfile.mkdtemp(prefix="widget-routes-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mod._last_payload = None
        self.addCleanup(setattr, self.mod, "_last_payload", None)
        for name, fname in (("WIDGET_FILE", "widget.json"),
                            ("HISTORY_FILE", "widget_history.json")):
            p = mock.patch.object(w_store, name, str(self.tmp / fname))
            p.start()
            self.addCleanup(p.stop)
        app = flask.Flask(__name__)
        app.register_blueprint(self.mod.widget_bp)
        self.client = app.test_client()
        self.vault = self.tmp / "vault"
        (self.vault / "Z_attachments" / "biblio_articles").mkdir(parents=True)

    def _inv_pair(self):
        inv = _inv(_record("smith2020", vault=self.vault))
        settings = SimpleNamespace(
            vault_root=self.vault,
            biblio_articles_dir=self.vault / "Z_attachments" / "biblio_articles")
        return inv, settings

    def _patches(self, inventory):
        from audit.manager import audit_manager
        return (
            mock.patch.object(audit_manager, "get_inventory",
                              return_value=inventory),
            mock.patch("api.routes.audit._drift_synonyms", return_value=None),
            mock.patch("api.routes.tagging._read_vocab",
                       return_value=({"dep": "Depression"}, "_tags.md")),
            mock.patch.object(self.mod, "_poke_swiftbar"),
        )

    def test_get_404_and_refresh_409_pre_scan(self):
        r = self.client.get("/api/widget", headers=_HEADERS)
        self.assertEqual(r.status_code, 404)
        with contextlib.ExitStack() as st:
            for p in self._patches((None, None)):
                st.enter_context(p)
            r = self.client.post("/api/widget/refresh", json={},
                                 headers=_HEADERS)
        self.assertEqual(r.status_code, 409)

    def test_refresh_builds_persists_and_get_serves(self):
        with contextlib.ExitStack() as st:
            for p in self._patches(self._inv_pair()):
                st.enter_context(p)
            r = self.client.post("/api/widget/refresh", json={},
                                 headers=_HEADERS)
            self.assertEqual(r.status_code, 200, r.get_json())
            d = r.get_json()
            self.assertEqual(d["paper"]["key"], "smith2020")
            self.assertEqual(d["notion"]["tag"], "dep")
            # File landed on disk...
            on_disk = w_store.read_snapshot()
            self.assertEqual(on_disk["paper"]["key"], "smith2020")
            # ...history recorded the shown paper...
            self.assertEqual(w_store.load_history(), ["smith2020"])
            # ...and GET serves it (memory), also after a memory drop (disk).
            self.assertEqual(self.client.get(
                "/api/widget", headers=_HEADERS).status_code, 200)
            self.mod._last_payload = None
            r = self.client.get("/api/widget", headers=_HEADERS)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["paper"]["key"], "smith2020")

    def test_listener_noop_on_cleared_inventory_and_never_scans(self):
        from audit.manager import audit_manager
        with contextlib.ExitStack() as st:
            for p in self._patches((None, None)):
                st.enter_context(p)
            spy = st.enter_context(
                mock.patch.object(audit_manager, "start_scan"))
            self.mod._on_scan_result()
        self.assertIsNone(w_store.read_snapshot())
        spy.assert_not_called()

    def test_listener_writes_on_fresh_inventory(self):
        with contextlib.ExitStack() as st:
            for p in self._patches(self._inv_pair()):
                st.enter_context(p)
            self.mod._on_scan_result()
        self.assertEqual(w_store.read_snapshot()["paper"]["key"], "smith2020")

    def test_listener_is_registered_on_the_manager(self):
        from audit.manager import audit_manager
        self.assertIn(self.mod._on_scan_result,
                      audit_manager._result_listeners)

    def test_swiftbar_poke_failure_is_not_fatal(self):
        with mock.patch.object(w_snapshot, "build_payload",
                               return_value={"version": 1, "paper": None}), \
             contextlib.ExitStack() as st:
            for p in self._patches(self._inv_pair())[:3]:
                st.enter_context(p)
            st.enter_context(mock.patch(
                "subprocess.Popen", side_effect=OSError("no open")))
            r = self.client.post("/api/widget/refresh", json={},
                                 headers=_HEADERS)
        self.assertEqual(r.status_code, 200)

    def test_same_day_refresh_is_stable(self):
        # The pick is appended to its own anti-repeat history, so without the
        # route-level filter a same-day rebuild would EXCLUDE the current
        # paper from the pool and advance on every refresh ("paper of the
        # click"). Two plain refreshes on one day must return the same paper
        # and record exactly one history entry.
        inv = _inv(*[_record(f"k{i}", vault=self.vault) for i in range(6)])
        settings = SimpleNamespace(
            vault_root=self.vault,
            biblio_articles_dir=self.vault / "Z_attachments" / "biblio_articles")
        from audit.manager import audit_manager
        with contextlib.ExitStack() as st:
            for p in self._patches((inv, settings)):
                st.enter_context(p)
            # The singleton's _finished_at may carry a real timestamp from
            # earlier tests in the process; pin the reset-to-epoch state the
            # `or None` guard is about.
            st.enter_context(mock.patch.object(
                audit_manager, "_finished_at", 0.0))
            r1 = self.client.post("/api/widget/refresh", json={},
                                  headers=_HEADERS)
            r2 = self.client.post("/api/widget/refresh", json={},
                                  headers=_HEADERS)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        key1 = r1.get_json()["paper"]["key"]
        self.assertEqual(key1, r2.get_json()["paper"]["key"])
        self.assertEqual(w_store.load_history(), [key1])
        # start_scan resets _finished_at to 0.0 — the payload must carry
        # null, never the epoch (widgets do age arithmetic on it).
        self.assertIsNone(r1.get_json()["scan_finished_at"])

    def test_refresh_500_not_409_when_build_fails_with_cached_scan(self):
        # A cached scan + a crashing build must NOT answer "Run a scan
        # first" — that advice re-runs a multi-minute scan to fix e.g. a
        # full disk. 409 stays reserved for the genuinely-no-scan state.
        with contextlib.ExitStack() as st:
            for p in self._patches(self._inv_pair()):
                st.enter_context(p)
            st.enter_context(mock.patch.object(
                w_snapshot, "build_payload", side_effect=OSError("disk full")))
            r = self.client.post("/api/widget/refresh", json={},
                                 headers=_HEADERS)
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("Run a scan", r.get_json()["error"])

    def test_redraw_forces_a_new_seed(self):
        # Two records so a redraw CAN differ; assert only that the route
        # accepts the flag and still answers a valid payload (the draw itself
        # is random — determinism is the builder tests' job).
        inv = _inv(_record("a", vault=self.vault),
                   _record("b", vault=self.vault))
        settings = SimpleNamespace(
            vault_root=self.vault,
            biblio_articles_dir=self.vault / "Z_attachments" / "biblio_articles")
        with contextlib.ExitStack() as st:
            for p in self._patches((inv, settings)):
                st.enter_context(p)
            r = self.client.post("/api/widget/refresh",
                                 json={"redraw": True}, headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertIn(r.get_json()["paper"]["key"], {"a", "b"})


def _load_swiftbar_plugin():
    """Import packaging/widgets/chatekld.1d.py (dotted filename) by path."""
    import importlib.util

    path = Path(__file__).parent / "packaging" / "widgets" / "chatekld.1d.py"
    spec = importlib.util.spec_from_file_location("chatekld_1d", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSwiftBarPluginHardening(unittest.TestCase):
    """Ratchet over the plugin's payload/server.json trust boundaries: every
    value interpolated into a SwiftBar ``bash=…/param…/href=`` line is either
    validated or whitespace-collapsed, so a corrupt/hostile file cannot
    fabricate menu lines or inject click-time parameters."""

    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_swiftbar_plugin()

    def test_esc_collapses_every_whitespace_form_and_pipe(self):
        self.assertEqual(
            self.plugin.esc("a\nb\rc\vd\fe | f g"), "a b c d e / f g")

    def test_safe_href_accepts_app_built_links_only(self):
        ok = "obsidian://open?path=%2FVault%2FNote%20A.md"
        self.assertEqual(self.plugin.safe_href(ok), ok)
        self.assertEqual(
            self.plugin.safe_href("https://example.org/x"),
            "https://example.org/x")
        for bad in (
            None, "",
            "obsidian://x bash=/bin/sh param1=-c param2=evil",  # space → params
            "file:///etc/passwd",                                # scheme
            "obsidian://x\tterminal=false",                      # control ws
            'obsidian://x"quoted"',                              # quoting games
        ):
            self.assertIsNone(self.plugin.safe_href(bad), bad)

    def test_server_base_validates_like_server_info(self):
        import os as _os
        with tempfile.TemporaryDirectory() as td:
            sj = Path(td) / "server.json"

            def write(info):
                sj.write_text(json.dumps(info), encoding="utf-8")

            alive = _os.getpid()
            write({"version": 1, "pid": alive, "host": "127.0.0.1",
                   "port": 5000})
            self.assertEqual(self.plugin.server_base(td),
                             "http://127.0.0.1:5000")
            # String port (curl-arg injection shape), off-loopback host,
            # missing version, out-of-range port: all refused.
            write({"version": 1, "pid": alive, "host": "127.0.0.1",
                   "port": "5000 param7=-o param8=/tmp/x param9=http://evil/"})
            self.assertIsNone(self.plugin.server_base(td))
            write({"version": 1, "pid": alive, "host": "evil.example",
                   "port": 5000})
            self.assertIsNone(self.plugin.server_base(td))
            write({"pid": alive, "host": "127.0.0.1", "port": 5000})
            self.assertIsNone(self.plugin.server_base(td))
            write({"version": 1, "pid": alive, "host": "127.0.0.1",
                   "port": 99999})
            self.assertIsNone(self.plugin.server_base(td))

    def test_main_degrades_on_wrong_typed_slots(self):
        # A version-1 payload with wrong-typed slots must render, not raise
        # (a SwiftBar plugin error card replaces the whole menu).
        import contextlib as _ctx
        import io
        payload = {"version": 1, "paper": ["not", "a", "dict"],
                   "suggestions": {"kind": "tag_batch"}, "notion": "x",
                   "generated_at": "2026-08-07T00:00:00+00:00"}
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "widget.json").write_text(
                json.dumps(payload), encoding="utf-8")
            with mock.patch.dict("os.environ", {"CHATEKLD_BASE_DIR": td}):
                out = io.StringIO()
                with _ctx.redirect_stdout(out):
                    rc = self.plugin.main()
        self.assertEqual(rc, 0)
        self.assertIn("📚", out.getvalue())


if __name__ == "__main__":
    unittest.main()
