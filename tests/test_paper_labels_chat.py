"""Paper labels in vault chat and the agent (rag/engine.py::_label_nodes)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from llama_index.core.schema import MetadataMode, NodeWithScore, TextNode

SRC = "Z_attachments/biblio_articles/smith_2020.pdf"
OTHER = "notes/perso.md"

# What the model is shown today, and must still be shown with labels off or
# with no index: the online render and the local LLM-mode node content.
GOLDEN_ONLINE = (
    "[1] Z_attachments/biblio_articles/smith_2020.pdf\nalpha text\n\n"
    "[2] notes/perso.md\nbeta text"
)
GOLDEN_LOCAL = (
    "source: Z_attachments/biblio_articles/smith_2020.pdf\n\nalpha text"
)
LABEL = "Smith, 2020, A Study [smithStudy2020]"


def _nodes():
    return [
        NodeWithScore(node=TextNode(text="alpha text", id_="a",
                                    metadata={"source": SRC}), score=0.9),
        NodeWithScore(node=TextNode(text="beta text", id_="b",
                                    metadata={"source": OTHER}), score=0.5),
    ]


class _WithIndex(unittest.TestCase):
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
            pdf_paths=[self.vault / SRC],
        )
        paper_index.write(paper_index.build(
            SimpleNamespace(records={"smithStudy2020": rec}),
            SimpleNamespace(vault_root=self.vault)))

    def tearDown(self):
        self._p.stop()
        self.pi.clear_cache()
        self._tmp.cleanup()

    def cfg(self, **over):
        cfg = {"obsidian_vault_path": str(self.vault)}
        cfg.update(over)
        return cfg

    def engine(self, provider="ollama"):
        from rag.engine import SimpleQueryEngine
        engine = SimpleQueryEngine.__new__(SimpleQueryEngine)
        engine.provider_name = provider
        return engine

    def prepared(self, **cfg_over):
        return SimpleNamespace(message="q", cfg=self.cfg(**cfg_over),
                               postprocessors=[], llm=None, primer="",
                               base_template="{context_str}",
                               qa_template="{context_str}")


class TestLabelNodes(_WithIndex):
    def test_labels_copies_and_leaves_the_originals_untouched(self):
        from rag.engine import _label_nodes
        nodes = _nodes()
        before = dict(nodes[0].node.metadata)
        out = _label_nodes(nodes, self.pi.load(self.vault))
        self.assertEqual(out[0].node.metadata["paper"], LABEL)
        self.assertIsNot(out[0].node, nodes[0].node)
        self.assertEqual(nodes[0].node.metadata, before)
        self.assertNotIn("paper", nodes[0].node.metadata)
        self.assertEqual(out[0].score, 0.9)
        self.assertIs(out[1], nodes[1])  # unknown source: untouched

    def test_no_index_returns_the_same_list(self):
        from rag.engine import _label_nodes
        nodes = _nodes()
        self.assertIs(_label_nodes(nodes, None), nodes)

    def test_local_context_carries_the_label(self):
        from rag.engine import _label_nodes
        out = _label_nodes(_nodes(), self.pi.load(self.vault))
        content = out[0].node.get_content(MetadataMode.LLM)
        self.assertIn(f"paper: {LABEL}", content)
        self.assertEqual(_nodes()[0].node.get_content(MetadataMode.LLM),
                         GOLDEN_LOCAL)


class TestFinishPaths(_WithIndex):
    def _render(self, chunks):
        from core.llm.prompt import render_context
        return render_context(chunks, max_chars=100_000)[0]

    def test_finish_retrieve_labels_chunks(self):
        chunks = self.engine().finish_retrieve(self.prepared(), _nodes())
        self.assertEqual(chunks[0].metadata["paper"], LABEL)
        self.assertEqual(
            self._render(chunks).splitlines()[0],
            f"[1] {SRC}, {LABEL}")
        self.assertNotIn("paper", chunks[1].metadata)

    def test_switch_off_is_byte_identical(self):
        chunks = self.engine().finish_retrieve(
            self.prepared(vault_paper_labels=False), _nodes())
        self.assertEqual(self._render(chunks), GOLDEN_ONLINE)

    def test_missing_index_is_byte_identical(self):
        self.pi.clear_cache()
        prepared = self.prepared()
        prepared.cfg["obsidian_vault_path"] = str(self.vault / "nope")
        chunks = self.engine().finish_retrieve(prepared, _nodes())
        self.assertEqual(self._render(chunks), GOLDEN_ONLINE)

    def test_online_finish_query_sends_labelled_chunks(self):
        engine = self.engine("openai")
        with mock.patch.object(engine, "_query_online_from_chunks",
                               return_value="r") as spy:
            engine.finish_query(self.prepared(), _nodes())
        chunks = spy.call_args.kwargs["chunks"]
        self.assertEqual(self._render(chunks).splitlines()[0],
                         f"[1] {SRC}, {LABEL}")

    def test_local_finish_query_sends_labelled_nodes(self):
        engine = self.engine("ollama")
        with mock.patch("rag.engine._MaterializedRetriever") as retr, \
                mock.patch("rag.engine.RetrieverQueryEngine") as rqe, \
                mock.patch("core.prompt_capture.record"):
            rqe.from_args.return_value.query.return_value = "r"
            engine.finish_query(self.prepared(), _nodes())
        nodes = retr.call_args.args[0]
        self.assertIn(f"paper: {LABEL}",
                      nodes[0].node.get_content(MetadataMode.LLM))

    def test_local_finish_query_switch_off_is_byte_identical(self):
        engine = self.engine("ollama")
        with mock.patch("rag.engine._MaterializedRetriever") as retr, \
                mock.patch("rag.engine.RetrieverQueryEngine"), \
                mock.patch("core.prompt_capture.record"):
            engine.finish_query(self.prepared(vault_paper_labels=False), _nodes())
        nodes = retr.call_args.args[0]
        self.assertEqual(nodes[0].node.get_content(MetadataMode.LLM),
                         GOLDEN_LOCAL)


class TestSurfaces(unittest.TestCase):
    def _chunk(self, meta):
        from core.llm.types import RetrievedChunk
        return RetrievedChunk(text="t", source=SRC, score=0.5, metadata=meta)

    def test_vault_search_results_carry_the_paper(self):
        from core.agent.vault_tools import _build_search_tool
        manager = mock.Mock()
        manager.retrieve.return_value = [
            self._chunk({"source": SRC, "paper": LABEL}),
            self._chunk({"source": SRC}),
        ]
        ctx = mock.Mock()
        tool = _build_search_tool(manager, ctx)
        out = json.loads(tool.runner({"query": "q"}))
        self.assertEqual(out["results"][0]["paper"],
                         {"key": "smithStudy2020", "label": "Smith, 2020, A Study"})
        self.assertNotIn("paper", out["results"][1])

    def test_retrieval_frame_carries_the_paper(self):
        from app import app
        from rag.vault import obsidian_manager

        app.config["TESTING"] = True
        chunk = self._chunk({"is_image": False, "paper": LABEL})

        class FakeResponse:
            used_chunks = [chunk]

            @property
            def response_gen(self):
                yield "hello"

        with mock.patch.object(obsidian_manager, "get_status", return_value="done"), \
                mock.patch.object(obsidian_manager, "stream_chat",
                                  return_value=FakeResponse()):
            resp = app.test_client().post(
                "/api/obsidian/chat", json={"message": "hi"},
                headers={"X-Requested-With": "ChatEKLD"}, buffered=True)
        frames = [json.loads(line[6:]) for line in
                  resp.get_data(as_text=True).splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        row = [f for f in frames if "retrieval" in f][0]["retrieval"][0]
        self.assertEqual(row["paper"], {"key": "smithStudy2020",
                                        "label": "Smith, 2020, A Study"})
        self.assertEqual(row["source"], SRC)

    def test_paper_field_rejects_malformed(self):
        from rag.paper_index import paper_field
        self.assertIsNone(paper_field({"paper": "no key here"}))
        self.assertIsNone(paper_field({}))
        self.assertIsNone(paper_field(None))

    def test_paper_labels_switch_is_never_a_body_override(self):
        """`vault_paper_labels` is config-only: a chat body cannot flip it."""
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {"vault_paper_labels": False, "paper_labels": False}, {})
        self.assertNotIn("vault_paper_labels", params)
        self.assertNotIn("paper_labels", params)

    def test_paper_field_strips_a_trailing_space_from_the_label(self):
        from rag.paper_index import paper_field
        self.assertEqual(paper_field({"paper": "Foo  [key]"}),
                         {"key": "key", "label": "Foo"})



if __name__ == "__main__":
    unittest.main()
