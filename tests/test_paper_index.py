"""rag/paper_index.py — the paper-label index built from the audit join."""
from __future__ import annotations

import json
import os
import tempfile
import unicodedata
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def _rec(key, *, vault, pdfs=(), note=None, title=None, authors=(), year=None,
         zot_title=None):
    bib = None
    if title or authors or year:
        bib = SimpleNamespace(title=title, authors=list(authors), year=year)
    return SimpleNamespace(
        citation_key=key,
        bib_entry=bib,
        zotero_item=SimpleNamespace(title=zot_title) if zot_title else None,
        obsidian_note=SimpleNamespace(path=vault / note) if note else None,
        pdf_paths=[vault / p for p in pdfs],
    )


class _Base(unittest.TestCase):
    def setUp(self):
        from rag import paper_index
        self.pi = paper_index
        paper_index.clear_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.vault = (self.tmp / "vault").resolve()
        self.vault.mkdir()
        self.base = self.tmp / "base"
        self._p = mock.patch("core.constants.BASE_DIR", str(self.base))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.pi.clear_cache()
        self._tmp.cleanup()

    def settings(self):
        return SimpleNamespace(vault_root=self.vault)

    def inv(self, *recs):
        return SimpleNamespace(records={r.citation_key: r for r in recs})


class TestBuild(_Base):
    def test_sources_basenames_and_labels(self):
        data = self.pi.build(self.inv(
            _rec("smithTitle2020", vault=self.vault,
                 pdfs=["Z_attachments/biblio_articles/smith_2020.pdf"],
                 note="Z_Zotero_Notes/smithTitle2020.md",
                 title="A {Study} of Things", authors=["Smith, Ann", "Doe, J"],
                 year="2020"),
        ), self.settings(), built_at="t")
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["vault"], str(self.vault))
        self.assertEqual(data["sources"], {
            "Z_attachments/biblio_articles/smith_2020.pdf": "smithTitle2020",
            "Z_Zotero_Notes/smithTitle2020.md": "smithTitle2020",
        })
        self.assertEqual(data["basenames"]["smith_2020.pdf"], "smithTitle2020")
        self.assertEqual(data["papers"]["smithTitle2020"], {
            "title": "A Study of Things", "authors": "Smith & Doe",
            "year": "2020", "label": "Smith & Doe, 2020, A Study of Things",
        })

    def test_latex_title_and_author_are_cleaned(self):
        data = self.pi.build(self.inv(
            _rec("k", vault=self.vault, pdfs=["a.pdf"],
                 title="Schizophr\\'enie et {\\'e}tat", authors=["Dub\\'e, F",
                 "B, x", "C, y"], year="2019"),
        ), self.settings())
        self.assertEqual(data["papers"]["k"]["label"],
                         "Dubé et al., 2019, Schizophrénie et état")

    def test_missing_parts_are_skipped_cleanly(self):
        data = self.pi.build(self.inv(
            _rec("k1", vault=self.vault, pdfs=["a.pdf"], title="Only title"),
            _rec("k2", vault=self.vault, pdfs=["b.pdf"], zot_title="Zot title"),
            _rec("k3", vault=self.vault, pdfs=["c.pdf"], year="2001"),
            _rec("k4", vault=self.vault),  # nothing mapped → not indexed
        ), self.settings())
        self.assertEqual(data["papers"]["k1"]["label"], "Only title")
        self.assertEqual(data["papers"]["k2"]["label"], "Zot title")
        self.assertEqual(data["papers"]["k3"]["label"], "2001")
        self.assertNotIn("k4", data["papers"])

    def test_ambiguous_basenames_are_dropped(self):
        data = self.pi.build(self.inv(
            _rec("k1", vault=self.vault, pdfs=["x/same.pdf"], title="A"),
            _rec("k2", vault=self.vault, pdfs=["y/same.pdf"], title="B"),
            _rec("k3", vault=self.vault, pdfs=["x/dup.pdf"], title="C"),
            _rec("k4", vault=self.vault, pdfs=["x/ok.pdf"], title="D"),
        ), self.settings(), ambiguous_basenames={"DUP.pdf"})
        self.assertNotIn("same.pdf", data["basenames"])  # two keys here
        self.assertNotIn("dup.pdf", data["basenames"])   # vault-wide duplicate
        self.assertEqual(data["basenames"]["ok.pdf"], "k4")
        # Full paths stay exact even when the basename is ambiguous.
        self.assertEqual(data["sources"]["x/same.pdf"], "k1")
        self.assertEqual(data["sources"]["y/same.pdf"], "k2")

    def test_a_path_claimed_by_two_keys_is_dropped(self):
        data = self.pi.build(self.inv(
            _rec("k1", vault=self.vault, pdfs=["x/a.pdf"], title="A"),
            _rec("k2", vault=self.vault, pdfs=["x/a.pdf"], title="B"),
        ), self.settings())
        self.assertNotIn("x/a.pdf", data["sources"])
        self.assertNotIn("a.pdf", data["basenames"])

    def test_nfd_paths_are_stored_nfc_and_looked_up_either_way(self):
        nfd = unicodedata.normalize("NFD", "x/lévy_2020.pdf")
        data = self.pi.build(self.inv(
            _rec("levy", vault=self.vault, pdfs=[nfd], title="T"),
        ), self.settings())
        nfc = unicodedata.normalize("NFC", "x/lévy_2020.pdf")
        self.assertIn(nfc, data["sources"])
        idx = self.pi.PaperIndex(data)
        self.assertEqual(idx.key_for_source(nfd), "levy")
        self.assertEqual(idx.key_for_source(nfc), "levy")
        self.assertEqual(idx.resolve(unicodedata.normalize("NFD", "LÉVY_2020.pdf")),
                         "levy")


class TestLoad(_Base):
    def _write(self, **over):
        data = self.pi.build(self.inv(
            _rec("k", vault=self.vault, pdfs=["a.pdf"], title="T"),
        ), self.settings())
        data.update(over)
        path = self.pi.write(data)
        return path

    def test_round_trip(self):
        path = self._write()
        self.assertTrue(path.startswith(str(self.base)))
        idx = self.pi.load(self.vault)
        self.assertIsNotNone(idx)
        self.assertEqual(idx.paper_for_source("a.pdf"), {"key": "k", "label": "T"})

    def test_missing_is_none(self):
        self.assertIsNone(self.pi.load(self.vault))
        self.assertIsNone(self.pi.load(""))

    def test_corrupt_is_none(self):
        path = self._write()
        Path(path).write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.pi.load(self.vault))

    def test_wrong_version_is_none(self):
        self._write(version=2)
        self.assertIsNone(self.pi.load(self.vault))

    def test_wrong_vault_is_none(self):
        path = self._write()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["vault"] = "/somewhere/else"
        Path(path).write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(self.pi.load(self.vault))

    def test_cache_invalidates_when_the_file_changes(self):
        path = self._write()
        self.assertEqual(self.pi.load(self.vault).label("k"), "T")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["papers"]["k"]["label"] = "Changed label"
        Path(path).write_text(json.dumps(data), encoding="utf-8")
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        self.assertEqual(self.pi.load(self.vault).label("k"), "Changed label")

    def test_cache_hit_does_not_reparse(self):
        self._write()
        first = self.pi.load(self.vault)
        with mock.patch("json.load", side_effect=AssertionError("reparsed")):
            self.assertIs(self.pi.load(self.vault), first)


class TestListener(_Base):
    def setUp(self):
        super().setUp()
        import api.routes.audit as mod
        self.mod = mod

    def test_writes_on_results(self):
        from audit.manager import audit_manager
        pair = (self.inv(_rec("k", vault=self.vault, pdfs=["a.pdf"],
                              title="T")), self.settings())
        with mock.patch.object(audit_manager, "get_inventory", return_value=pair):
            self.mod._write_paper_index()
        idx = self.pi.load(self.vault)
        self.assertIsNotNone(idx)
        self.assertEqual(idx.key_for_source("a.pdf"), "k")

    def test_noop_on_clear(self):
        from audit.manager import audit_manager
        with mock.patch.object(audit_manager, "get_inventory",
                               return_value=(None, None)), \
                mock.patch.object(self.pi, "write") as spy:
            self.mod._write_paper_index()
        spy.assert_not_called()
        self.assertFalse(self.base.exists())

    def test_failure_only_logs(self):
        from audit.manager import audit_manager
        pair = (self.inv(_rec("k", vault=self.vault, pdfs=["a.pdf"])),
                self.settings())
        with mock.patch.object(audit_manager, "get_inventory", return_value=pair), \
                mock.patch.object(self.pi, "build", side_effect=RuntimeError("x")), \
                self.assertLogs("api.routes.audit", level="ERROR"):
            self.mod._write_paper_index()  # must not raise

    def test_registered_on_the_manager(self):
        from audit.manager import audit_manager
        self.assertIn(self.mod._write_paper_index,
                      audit_manager._result_listeners)


if __name__ == "__main__":
    unittest.main()
