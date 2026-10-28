"""The deck/article citation pass reads the paper index for the configured vault."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class TestRouteWiring(unittest.TestCase):
    def setUp(self):
        from rag import paper_index
        self.pi = paper_index
        paper_index.clear_cache()
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.vault = (tmp / "vault").resolve()
        self.vault.mkdir()
        self._p = mock.patch("core.constants.BASE_DIR", str(tmp / "base"))
        self._p.start()
        rec = SimpleNamespace(
            citation_key="smithStudy2020",
            bib_entry=SimpleNamespace(title="A Study", authors=["Smith, A"],
                                      year="2020"),
            zotero_item=None, obsidian_note=None,
            pdf_paths=[self.vault / "Z_attachments/biblio_articles/smith_2020.pdf"],
        )
        paper_index.write(paper_index.build(
            SimpleNamespace(records={"smithStudy2020": rec}),
            SimpleNamespace(vault_root=self.vault)))
        self._cfg = mock.patch(
            "core.config.load_config",
            return_value={"obsidian_vault_path": str(self.vault)})
        self._cfg.start()

    def tearDown(self):
        self._cfg.stop()
        self._p.stop()
        self.pi.clear_cache()
        self._tmp.cleanup()

    BODY = ("\\begin{frame}{T}\n\\item A (source: smith_2020.pdf)\n"
            "\\end{frame}\n")

    def test_deck_rewriter_cites_a_pdf_through_the_index(self):
        from api.routes import deck
        out = deck._source_rewriter({"smithStudy2020": ()})(self.BODY)
        self.assertIn("\\citefoot{smithStudy2020}", out)

    def test_deck_rewriter_labels_the_footer_with_citations_off(self):
        from api.routes import deck
        out = deck._source_rewriter({}, footer_mode="vfill")(self.BODY)
        self.assertIn("Source : Smith, 2020, A Study", out)
        self.assertNotIn("\\citefoot", out)

    def test_docgen_rewriter_cites_a_pdf_through_the_index(self):
        from api.routes import docgen
        out = docgen._source_rewriter({"smithStudy2020": ()})(
            "\\section{S}\nA (source: smith_2020.pdf).\n")
        self.assertIn("\\autocite{smithStudy2020}", out)

    def test_no_index_for_the_vault_leaves_the_filename(self):
        from api.routes import deck
        self.pi.clear_cache()
        with mock.patch("core.config.load_config",
                        return_value={"obsidian_vault_path": "/nowhere"}):
            out = deck._source_rewriter({})(self.BODY)
        self.assertIn("smith\\_2020.pdf", out)


if __name__ == "__main__":
    unittest.main()
