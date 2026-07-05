import base64
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestVaultIndexingRegressions(unittest.TestCase):
    def test_lm_studio_embedding_accepts_local_model_ids(self):
        from core.providers.lms import LMStudioProvider

        provider = LMStudioProvider()
        embedding = provider.get_embedding("text-embedding-nomic-embed-text-v1.5")

        self.assertEqual(embedding.model_name, "text-embedding-nomic-embed-text-v1.5")

    def test_lm_studio_llm_metadata_accepts_local_model_ids(self):
        from core.providers.lms import LMStudioProvider

        provider = LMStudioProvider()
        llm = provider.get_llm("mistralai/ministral-3-3b", context_window=12345)

        self.assertEqual(llm.metadata.context_window, 12345)
        self.assertEqual(llm.metadata.model_name, "mistralai/ministral-3-3b")
        self.assertGreater(len(llm._tokenizer.encode("hello world")), 0)

    def test_index_vault_uses_current_llama_index_insert_api(self):
        from rag.vault import ObsidianVaultManager
        from llama_index.core.embeddings import MockEmbedding

        class FakeProvider:
            def get_embedding(self, _name):
                return MockEmbedding(embed_dim=2)

        class FakeStorageContext:
            def persist(self, persist_dir):
                Path(persist_dir).mkdir(parents=True, exist_ok=True)

        class FakeStorageContextFactory:
            @staticmethod
            def from_defaults(*_args, **_kwargs):
                return FakeStorageContext()

        class FakeVectorStoreIndex:
            def __init__(self):
                self.inserted = []
                self.storage_context = FakeStorageContext()

            @classmethod
            def from_documents(cls, *_args, **kwargs):
                self.assertIn("embed_model", kwargs)
                return cls()

            def insert(self, doc):
                self.inserted.append(doc)

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, tempfile.TemporaryDirectory() as index_dir:
            Path(vault_dir, "note.md").write_text("# Heading\n\nBody text", encoding="utf-8")
            manager = ObsidianVaultManager()
            manager.restore_vault_path(vault_dir)

            with (
                patch("rag.vault.get_provider", return_value=FakeProvider()),
                patch("rag.vault.StorageContext", FakeStorageContextFactory),
                patch("rag.vault.VectorStoreIndex", FakeVectorStoreIndex),
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
            ):
                manager.index_vault("llm", "embed", provider_name="ollama")

            self.assertEqual(manager.get_status(), "done", manager.drain_status_messages())
            self.assertGreater(len(manager._index.inserted), 0)

    def test_vault_exclusions_filter_nested_files(self):
        from rag.vault import ObsidianVaultManager

        with tempfile.TemporaryDirectory() as vault_dir:
            vault = Path(vault_dir)
            (vault / "keep").mkdir()
            (vault / "skip").mkdir()
            (vault / "keep" / "note.md").write_text("keep", encoding="utf-8")
            (vault / "skip" / "note.md").write_text("skip", encoding="utf-8")

            manager = ObsidianVaultManager()
            with patch("rag.vault.load_config", return_value={"vault_exclude_dirs": ["skip"]}):
                docs = list(manager._load_vault_documents(vault_dir))

            sources = {doc.metadata["source"] for doc in docs}
            self.assertEqual(sources, {"keep/note.md"})

    def test_pdf_extraction_cache_skips_reextracting_unchanged_pdf(self):
        from rag.vault import ObsidianVaultManager

        class FakeSections:
            full_text = "cached pdf text"

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, tempfile.TemporaryDirectory() as index_dir, tempfile.TemporaryDirectory() as cache_dir:
            vault = Path(vault_dir)
            (vault / "paper.pdf").write_bytes(b"%PDF fake content")
            manager = ObsidianVaultManager()

            with (
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
                patch("rag.vault.OBSIDIAN_CACHE_DIR", cache_dir),
                patch("rag.vault.load_config", return_value={}),
                patch("rag.vault.extract_structured_from_pdf", return_value=FakeSections()) as extract,
            ):
                first_docs = list(manager._load_vault_documents(vault_dir))

            self.assertEqual(extract.call_count, 1)
            self.assertEqual(first_docs[0].text, "cached pdf text")
            self.assertTrue(any(Path(cache_dir, "pdf_cache").rglob("*.txt")))

            with (
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
                patch("rag.vault.OBSIDIAN_CACHE_DIR", cache_dir),
                patch("rag.vault.load_config", return_value={}),
                patch("rag.vault.extract_structured_from_pdf", side_effect=AssertionError("should use cache")) as extract_again,
            ):
                second_docs = list(manager._load_vault_documents(vault_dir))

            self.assertEqual(extract_again.call_count, 0)
            self.assertEqual(second_docs[0].text, "cached pdf text")

    def test_load_vault_documents_is_a_generator(self):
        """M4: ensure the loader streams rather than pre-materialising.

        ``inspect.isgenerator`` would also accept a list-returning function
        wrapped in ``iter()``, so we check that the return value is the raw
        generator object and that consuming it incrementally works.
        """
        import types
        from rag.vault import ObsidianVaultManager

        with tempfile.TemporaryDirectory() as vault_dir:
            vault = Path(vault_dir)
            for i in range(3):
                (vault / f"note-{i}.md").write_text(f"content {i}", encoding="utf-8")

            manager = ObsidianVaultManager()
            with patch("rag.vault.load_config", return_value={}):
                gen = manager._load_vault_documents(vault_dir)
                self.assertIsInstance(gen, types.GeneratorType)
                docs = list(gen)
            self.assertEqual(len(docs), 3)

    def test_load_vault_documents_streams_pdfs_lazily(self):
        """M4: PDF extraction must not run until the consumer iterates past
        the first MD docs.  Pre-materialising every PDF up front is exactly
        the memory peak the streaming refactor eliminated.
        """
        from rag.vault import ObsidianVaultManager

        class FakeSections:
            full_text = "pdf body"

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, \
             tempfile.TemporaryDirectory() as cache_dir:
            vault = Path(vault_dir)
            (vault / "note.md").write_text("a markdown note", encoding="utf-8")
            (vault / "paper-1.pdf").write_bytes(b"%PDF one")
            (vault / "paper-2.pdf").write_bytes(b"%PDF two")

            manager = ObsidianVaultManager()

            with (
                patch("rag.vault.OBSIDIAN_CACHE_DIR", cache_dir),
                patch("rag.vault.load_config", return_value={}),
                patch(
                    "rag.vault.extract_structured_from_pdf",
                    return_value=FakeSections(),
                ) as extract,
            ):
                gen = manager._load_vault_documents(vault_dir)

                # Pulling the first document should not have triggered any
                # PDF extraction yet — MD docs are yielded first.
                first = next(gen)
                self.assertEqual(first.metadata["extension"], ".md")
                self.assertEqual(extract.call_count, 0)

                # The next pulls extract the PDFs one at a time.
                second = next(gen)
                self.assertEqual(second.metadata["extension"], ".pdf")
                self.assertEqual(extract.call_count, 1)

                third = next(gen)
                self.assertEqual(third.metadata["extension"], ".pdf")
                self.assertEqual(extract.call_count, 2)

                with self.assertRaises(StopIteration):
                    next(gen)

    def test_chunk_ids_stable_across_runs(self):
        """M4 hidden precondition (from the audit): chunk IDs encode the
        per-document enumeration index ``i``, so any change to *within-document*
        chunk emission order would silently invalidate the vector store.

        This test runs the full load + chunk pipeline twice on the same
        fixture vault and asserts that the resulting chunk IDs are
        identical.  A future refactor that subtly reorders nodes would
        flip this assertion before it could ship a corrupt index.
        """
        from rag.vault import ObsidianVaultManager

        with tempfile.TemporaryDirectory() as vault_dir:
            vault = Path(vault_dir)
            (vault / "alpha.md").write_text(
                "# Alpha\n\nFirst paragraph.\n\n# Beta\n\nSecond paragraph.",
                encoding="utf-8",
            )
            (vault / "gamma.md").write_text(
                "# Gamma\n\nA different note with different content here.",
                encoding="utf-8",
            )

            manager = ObsidianVaultManager()

            with patch("rag.vault.load_config", return_value={}):
                first_docs = list(manager._load_vault_documents(vault_dir))
                first_chunks = list(manager._chunk_raw_documents(first_docs, vault_dir))
                second_docs = list(manager._load_vault_documents(vault_dir))
                second_chunks = list(manager._chunk_raw_documents(second_docs, vault_dir))

            first_ids = [c.doc_id for c in first_chunks]
            second_ids = [c.doc_id for c in second_chunks]
            self.assertEqual(first_ids, second_ids)
            # Sanity: chunks actually carry text and IDs are unique.
            self.assertGreater(len(first_ids), 0)
            self.assertEqual(len(set(first_ids)), len(first_ids))

    def test_stream_chat_sets_provider_embedding_before_loading_persisted_index(self):
        from rag.vault import ObsidianVaultManager

        class FakeProvider:
            def get_embedding(self, name):
                return f"embedding:{name}"

        class FakeStorageContextFactory:
            @staticmethod
            def from_defaults(*_args, **_kwargs):
                return object()

        def fake_load_index_from_storage(_storage_ctx, **kwargs):
            self.assertEqual(kwargs.get("embed_model"), "embedding:lm-embed")
            return object()

        class FakeQueryEngine:
            def __init__(self, *_args, **_kwargs):
                pass

            def query(self, message):
                return f"answer:{message}"

        with tempfile.TemporaryDirectory() as index_dir:
            Path(index_dir, "docstore.json").write_text("{}", encoding="utf-8")
            Path(index_dir, "index_store.json").write_text('{"index_store/data":{}}', encoding="utf-8")
            Path(index_dir, "default__vector_store.json").write_text(
                '{"embedding_dict":{"n1":[0.1]},"text_id_to_ref_doc_id":{"n1":"doc"},"metadata_dict":{"n1":{}}}',
                encoding="utf-8",
            )
            manager = ObsidianVaultManager()

            with (
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
                patch("rag.vault.get_provider", return_value=FakeProvider()),
                patch("rag.vault.StorageContext", FakeStorageContextFactory),
                patch("rag.vault.load_index_from_storage", side_effect=fake_load_index_from_storage),
                patch("rag.engine.SimpleQueryEngine", FakeQueryEngine),
            ):
                response = manager.stream_chat(
                    "hello",
                    "lm-chat",
                    "lm-embed",
                    provider_name="lm_studio",
                )

        self.assertEqual(response, "answer:hello")

    def test_index_vault_writes_indexed_materials_manifest(self):
        from rag.vault import ObsidianVaultManager
        from llama_index.core.embeddings import MockEmbedding

        class FakeProvider:
            def get_embedding(self, _name):
                return MockEmbedding(embed_dim=2)

        class FakeStorageContext:
            def persist(self, persist_dir):
                Path(persist_dir).mkdir(parents=True, exist_ok=True)

        class FakeStorageContextFactory:
            @staticmethod
            def from_defaults(*_args, **_kwargs):
                return FakeStorageContext()

        class FakeVectorStoreIndex:
            def __init__(self):
                self.storage_context = FakeStorageContext()

            @classmethod
            def from_documents(cls, *_args, **kwargs):
                self.assertIn("embed_model", kwargs)
                return cls()

            def insert(self, _doc):
                pass

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, tempfile.TemporaryDirectory() as index_dir:
            Path(vault_dir, "note.md").write_text("# Heading\n\nBody text", encoding="utf-8")
            manager = ObsidianVaultManager()
            manager.restore_vault_path(vault_dir)

            with (
                patch("rag.vault.get_provider", return_value=FakeProvider()),
                patch("rag.vault.StorageContext", FakeStorageContextFactory),
                patch("rag.vault.VectorStoreIndex", FakeVectorStoreIndex),
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
            ):
                manager.index_vault("llm", "embed", provider_name="ollama")
                materials = manager.get_indexed_materials()["materials"]

            self.assertEqual(materials[0]["source"], "note.md")
            self.assertGreaterEqual(materials[0]["chunk_count"], 1)

    def test_indexed_materials_falls_back_to_existing_docstore(self):
        from rag.vault import ObsidianVaultManager

        docstore = {
            "docstore/ref_doc_info": {
                "folder/note.md::0": {
                    "metadata": {
                        "file_path": "/vault/folder/note.md",
                        "source": "folder/note.md",
                        "extension": ".md",
                    }
                },
                "folder/note.md::1": {
                    "metadata": {
                        "file_path": "/vault/folder/note.md",
                        "source": "folder/note.md",
                        "extension": ".md",
                    }
                },
                "paper.pdf::flat": {
                    "metadata": {
                        "file_path": "/vault/paper.pdf",
                        "source": "paper.pdf",
                        "extension": ".pdf",
                    }
                },
            }
        }

        with tempfile.TemporaryDirectory() as index_dir:
            Path(index_dir, "docstore.json").write_text(json.dumps(docstore), encoding="utf-8")
            Path(index_dir, "obsidian_meta.json").write_text(
                json.dumps({"indexed_at": "2026-05-10T12:00:00+00:00"}),
                encoding="utf-8",
            )
            manager = ObsidianVaultManager()
            manager.restore_vault_path("/vault")

            with patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir):
                payload = manager.get_indexed_materials()

            self.assertEqual(payload["indexed_at"], "2026-05-10T12:00:00+00:00")
            self.assertEqual(
                payload["materials"],
                [
                    {"source": "folder/note.md", "extension": ".md", "chunk_count": 2},
                    {"source": "paper.pdf", "extension": ".pdf", "chunk_count": 1},
                ],
            )
            self.assertTrue(Path(index_dir, "indexed_materials.json").exists())

    def test_index_vault_skips_unchanged_chunks_and_deletes_stale_chunks(self):
        from rag.vault import ObsidianVaultManager
        from llama_index.core import Document as LlamaDocument

        unchanged = LlamaDocument(
            text="same", doc_id="same.md::0", metadata={"source": "same.md"}
        )
        changed = LlamaDocument(
            text="new", doc_id="changed.md::0", metadata={"source": "changed.md"}
        )
        new = LlamaDocument(
            text="brand new", doc_id="new.md::0", metadata={"source": "new.md"}
        )

        class FakeDocstore:
            def get_document_hash(self, doc_id):
                if doc_id == unchanged.doc_id:
                    return unchanged.hash
                if doc_id == changed.doc_id:
                    return "old-hash"
                return None

            def get_all_ref_doc_info(self):
                return {
                    unchanged.doc_id: object(),
                    changed.doc_id: object(),
                    "deleted.md::0": object(),
                }

        class FakeIndex:
            def __init__(self):
                self.docstore = FakeDocstore()
                self.inserted = []
                self.deleted = []

            def insert(self, doc):
                self.inserted.append(doc.doc_id)

            def delete_ref_doc(self, doc_id, delete_from_docstore=False):
                self.deleted.append((doc_id, delete_from_docstore))

        manager = ObsidianVaultManager()
        index = FakeIndex()

        added, skipped, deleted, failed, manifest_counts = manager._index_documents_streaming(
            index,
            iter([unchanged, changed, new]),
        )

        self.assertEqual((added, skipped, deleted, failed), (2, 1, 1, 0))
        self.assertEqual(index.inserted, [changed.doc_id, new.doc_id])
        self.assertEqual(
            index.deleted,
            [(changed.doc_id, True), ("deleted.md::0", True)],
        )
        # Both the skipped (unchanged) and inserted chunks must be counted.
        self.assertEqual(
            manifest_counts,
            {"same.md": 1, "changed.md": 1, "new.md": 1},
        )

    def test_index_streaming_tolerates_single_insert_failure(self):
        """One transient insert failure must not abort the run."""
        from rag.vault import ObsidianVaultManager
        from llama_index.core import Document as LlamaDocument

        good1 = LlamaDocument(text="a", doc_id="a.md::0", metadata={"source": "a.md"})
        bad = LlamaDocument(text="b", doc_id="b.md::0", metadata={"source": "b.md"})
        good2 = LlamaDocument(text="c", doc_id="c.md::0", metadata={"source": "c.md"})

        class FakeIndex:
            def __init__(self):
                self.docstore = None  # no incremental
                self.inserted = []

            def insert(self, doc):
                if doc.doc_id == bad.doc_id:
                    raise RuntimeError("simulated transient embed failure")
                self.inserted.append(doc.doc_id)

        manager = ObsidianVaultManager()
        index = FakeIndex()
        # Backoff patched to 0 so the single failure's recovery pause doesn't
        # slow the test; the tolerate-and-continue behaviour is unchanged.
        with patch.object(ObsidianVaultManager, "_FAILURE_BACKOFF_BASE_S", 0):
            added, skipped, deleted, failed, manifest = manager._index_documents_streaming(
                index, iter([good1, bad, good2])
            )

        self.assertEqual((added, skipped, deleted, failed), (2, 0, 0, 1))
        self.assertEqual(index.inserted, [good1.doc_id, good2.doc_id])
        # bad doc must not be in the manifest — it never made it in.
        self.assertEqual(manifest, {"a.md": 1, "c.md": 1})

    def test_index_streaming_circuit_breaker_aborts_after_consecutive_failures(self):
        """N consecutive failures must abort the run and set the stop event."""
        from rag.vault import ObsidianVaultManager
        from llama_index.core import Document as LlamaDocument

        # Build more docs than the breaker threshold so we can confirm early-abort.
        threshold = ObsidianVaultManager._MAX_CONSECUTIVE_FAILURES
        docs = [LlamaDocument(text=f"d{i}", doc_id=f"d{i}.md::0") for i in range(threshold + 5)]

        class FakeIndex:
            def __init__(self):
                self.docstore = None
                self.insert_calls = 0

            def insert(self, doc):
                self.insert_calls += 1
                raise RuntimeError("backend down")

        manager = ObsidianVaultManager()
        index = FakeIndex()
        # Backoff patched to 0: the breaker is a wall-clock window in production
        # (~87 s of inter-failure sleeps before the abort), but the count-based
        # abort semantics under test are unchanged, so zero it out for speed.
        with patch.object(ObsidianVaultManager, "_FAILURE_BACKOFF_BASE_S", 0):
            added, skipped, deleted, failed, _ = manager._index_documents_streaming(
                index, iter(docs)
            )

        self.assertEqual(added, 0)
        self.assertEqual(skipped, 0)
        # Stopped at threshold consecutive failures, not after processing every doc.
        self.assertEqual(failed, threshold)
        self.assertEqual(index.insert_calls, threshold)
        self.assertTrue(manager._stop_event.is_set())
        # Reset for subsequent tests in the suite.
        manager._stop_event.clear()

    def test_batch_fallback_records_all_gaps_when_breaker_trips_midbatch(self):
        """Track 5.6 gap-report completeness (2026-07 fix).

        When the consecutive-failure breaker trips *mid* per-doc-fallback on an
        INCREMENTAL re-index, the changed chunks still queued AFTER the trip
        point had their old copy deleted at buffer time, so they are genuine
        content gaps. The pre-fix code returned without recording them, so the
        reinsert-gap warning under-counted; this pins that every deleted-old
        chunk in the aborted batch is reported.
        """
        from rag.vault import ObsidianVaultManager
        from llama_index.core import Document as LlamaDocument

        # 6 changed chunks; the backend rejects every (re-)insert.
        docs = [
            LlamaDocument(text=f"n{i}", doc_id=f"d{i}.md::0", metadata={"source": f"d{i}.md"})
            for i in range(6)
        ]
        doc_ids = [d.doc_id for d in docs]

        class FakeDocstore:
            # Every stored hash differs from the doc's -> all 6 are "changed",
            # so the main loop deletes each old copy before buffering (deleted_old).
            def get_document_hash(self, doc_id):
                return "old-hash"

            def get_all_ref_doc_info(self):
                return {did: object() for did in doc_ids}

        class FakeIndex:
            # Deliberately no `_transformations` attribute: the batch stage-1
            # (run_transformations) raises AttributeError and the flush degrades
            # straight to the per-doc fallback path this test targets.
            def __init__(self):
                self.docstore = FakeDocstore()

            def insert(self, doc):
                raise RuntimeError("backend down")

            def delete_ref_doc(self, doc_id, delete_from_docstore=False):
                return None  # the old-copy delete SUCCEEDS -> deleted_old=True

        manager = ObsidianVaultManager()
        index = FakeIndex()
        # batch_target=6 (one flush holds all 6); breaker at 3; no backoff sleep.
        with patch.object(ObsidianVaultManager, "_embed_batch_size", lambda self: 6), \
                patch.object(ObsidianVaultManager, "_MAX_CONSECUTIVE_FAILURES", 3), \
                patch.object(ObsidianVaultManager, "_FAILURE_BACKOFF_BASE_S", 0):
            manager._index_documents_streaming(index, iter(docs))

        # The breaker fires at the 3rd failure, but all 6 deleted-old chunks are
        # gaps — the warning must count 6, not 3 (the pre-fix under-count).
        warnings = [m for m in manager._status_messages if "could not be re-embedded" in m]
        self.assertTrue(warnings, "expected a reinsert-gap warning to be emitted")
        self.assertIn("6 changed chunk(s)", warnings[-1])
        manager._stop_event.clear()  # reset for the rest of the suite

    def test_index_streaming_backoff_spaces_and_caps_consecutive_failures(self):
        """Each consecutive failure waits a little longer before the next attempt
        (exponential, capped) — turning the breaker into a wall-clock recovery
        window — without sleeping for real in the test."""
        from rag.vault import ObsidianVaultManager
        from llama_index.core import Document as LlamaDocument

        threshold = ObsidianVaultManager._MAX_CONSECUTIVE_FAILURES
        docs = [LlamaDocument(text=f"d{i}", doc_id=f"d{i}.md::0") for i in range(threshold + 5)]

        class FakeIndex:
            def __init__(self):
                self.docstore = None

            def insert(self, doc):
                raise RuntimeError("backend down")

        manager = ObsidianVaultManager()
        # Record the requested backoff intervals instead of really sleeping; the
        # stub returns False (event not set) so the loop keeps going to the abort.
        waits: list[float] = []

        def fake_wait(timeout=None):
            waits.append(timeout)
            return False

        with patch.object(manager._stop_event, "wait", side_effect=fake_wait):
            manager._index_documents_streaming(FakeIndex(), iter(docs))

        # 19 sleeps before the 20th failure aborts (the aborting failure never sleeps).
        self.assertEqual(len(waits), threshold - 1)
        # Exponential base*2**(streak-1) capped at _FAILURE_BACKOFF_CAP_S.
        base = ObsidianVaultManager._FAILURE_BACKOFF_BASE_S
        cap = ObsidianVaultManager._FAILURE_BACKOFF_CAP_S
        self.assertEqual(waits[0], base)
        self.assertEqual(waits[1], base * 2)
        self.assertTrue(all(0 < w <= cap for w in waits))
        # Monotonically non-decreasing and pinned at the cap once reached.
        self.assertEqual(waits, sorted(waits))
        self.assertEqual(waits[-1], cap)
        manager._stop_event.clear()

    def test_index_streaming_backoff_is_interruptible_by_cancel(self):
        """A Cancel/Pause (stop event set) during a backoff sleep aborts the run
        promptly, well before the consecutive-failure threshold."""
        from rag.vault import ObsidianVaultManager
        from llama_index.core import Document as LlamaDocument

        threshold = ObsidianVaultManager._MAX_CONSECUTIVE_FAILURES
        docs = [LlamaDocument(text=f"d{i}", doc_id=f"d{i}.md::0") for i in range(threshold + 5)]

        class FakeIndex:
            def __init__(self):
                self.docstore = None
                self.insert_calls = 0

            def insert(self, doc):
                self.insert_calls += 1
                raise RuntimeError("backend down")

        manager = ObsidianVaultManager()
        index = FakeIndex()

        calls = {"n": 0}

        def fake_wait(timeout=None):
            # Simulate a cancel arriving during the 3rd backoff sleep.
            calls["n"] += 1
            return calls["n"] >= 3

        with patch.object(manager._stop_event, "wait", side_effect=fake_wait):
            added, skipped, deleted, failed, _ = manager._index_documents_streaming(
                index, iter(docs)
            )

        # Aborted on the interrupted backoff, not the breaker: 3 failed inserts.
        self.assertEqual(failed, 3)
        self.assertEqual(index.insert_calls, 3)
        self.assertLess(failed, threshold)
        manager._stop_event.clear()

    def test_mid_run_checkpoint_gated_by_count_and_min_interval(self):
        """Checkpoints require BOTH _PERSIST_EVERY pending inserts AND
        _PERSIST_MIN_INTERVAL_S elapsed since the last attempt; patching the
        interval to 0 restores the old count-only cadence."""
        from rag.vault import ObsidianVaultManager
        from llama_index.core import Document as LlamaDocument

        docs = [
            LlamaDocument(
                text=f"d{i}", doc_id=f"d{i}.md::0", metadata={"source": f"d{i}.md"}
            )
            for i in range(6)
        ]

        class FakeIndex:
            def __init__(self):
                self.docstore = None  # no incremental

            def insert(self, doc):
                pass

        checkpoints: list[dict] = []

        with (
            patch.object(ObsidianVaultManager, "_PERSIST_EVERY", 2),
            patch.object(ObsidianVaultManager, "_PERSIST_MIN_INTERVAL_S", 3600),
        ):
            manager = ObsidianVaultManager()
            manager._index_documents_streaming(
                FakeIndex(), iter(docs),
                lambda counts: checkpoints.append(dict(counts)),
            )
        # Pending count was reached three times over, but the interval never
        # elapsed — no checkpoint may fire.
        self.assertEqual(checkpoints, [])

        with (
            patch.object(ObsidianVaultManager, "_PERSIST_EVERY", 2),
            patch.object(ObsidianVaultManager, "_PERSIST_MIN_INTERVAL_S", 0),
        ):
            manager = ObsidianVaultManager()
            manager._index_documents_streaming(
                FakeIndex(), iter(docs),
                lambda counts: checkpoints.append(dict(counts)),
            )
        # Interval gate disabled: fires at 2, 4, and 6 pending inserts.
        self.assertEqual(len(checkpoints), 3)
        # The callback receives the cumulative manifest counts so far.
        self.assertEqual(checkpoints[-1], {f"d{i}.md": 1 for i in range(6)})

    def test_empty_vault_image_exts_disables_image_indexing(self):
        """vault_image_exts=[] must skip images entirely; no vision call."""
        from rag.vault import ObsidianVaultManager

        with tempfile.TemporaryDirectory() as vault_dir:
            vault = Path(vault_dir)
            (vault / "pic.png").write_bytes(b"\x89PNG fake")
            manager = ObsidianVaultManager()

            with (
                patch("rag.vault.load_config", return_value={"vault_image_exts": []}),
                patch("rag.vault.vision_manager") as mock_vision,
            ):
                docs = list(manager._load_vault_documents(vault_dir))

            self.assertEqual(docs, [])
            mock_vision.check_availability.assert_not_called()
            mock_vision.describe_image.assert_not_called()

    def test_image_description_cache_skips_vision_call_on_repeat(self):
        """A cached description must short-circuit the vision call."""
        from rag.vault import ObsidianVaultManager

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, tempfile.TemporaryDirectory() as index_dir, tempfile.TemporaryDirectory() as cache_dir:
            vault = Path(vault_dir)
            (vault / "pic.png").write_bytes(b"\x89PNG cached image bytes")
            (vault / "note.md").write_text("![[pic.png]]", encoding="utf-8")
            manager = ObsidianVaultManager()

            with (
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
                patch("rag.vault.OBSIDIAN_CACHE_DIR", cache_dir),
                patch("rag.vault.load_config", return_value={"vault_image_exts": [".png"]}),
                patch("rag.vault.vision_manager") as mock_vision,
            ):
                mock_vision.check_availability.return_value = True
                mock_vision.describe_image.return_value = "described image"
                first_docs = list(manager._load_vault_documents(vault_dir))

            self.assertEqual(mock_vision.describe_image.call_count, 1)
            self.assertTrue(any(doc.text == "described image" for doc in first_docs))
            self.assertTrue(any(Path(cache_dir, "image_cache").rglob("*.txt")))

            with (
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
                patch("rag.vault.OBSIDIAN_CACHE_DIR", cache_dir),
                patch("rag.vault.load_config", return_value={"vault_image_exts": [".png"]}),
                patch("rag.vault.vision_manager") as mock_vision_again,
            ):
                mock_vision_again.check_availability.return_value = True
                mock_vision_again.describe_image.side_effect = AssertionError("should use cache")
                second_docs = list(manager._load_vault_documents(vault_dir))

            self.assertEqual(mock_vision_again.describe_image.call_count, 0)
            self.assertTrue(any(doc.text == "described image" for doc in second_docs))

    def test_oversize_image_is_skipped_without_vision_call(self):
        """Images above the size cap are counted as skipped and never embedded."""
        from rag.vault import ObsidianVaultManager

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, tempfile.TemporaryDirectory() as index_dir, tempfile.TemporaryDirectory() as cache_dir:
            vault = Path(vault_dir)
            huge_path = vault / "huge.png"
            huge_path.write_bytes(b"\x00")
            (vault / "note.md").write_text("![[huge.png]]", encoding="utf-8")
            manager = ObsidianVaultManager()

            with (
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
                patch("rag.vault.OBSIDIAN_CACHE_DIR", cache_dir),
                patch("rag.vault.load_config", return_value={"vault_image_exts": [".png"]}),
                patch("rag.vault.vision_manager") as mock_vision,
                patch.object(ObsidianVaultManager, "_IMAGE_MAX_BYTES", 0),
            ):
                mock_vision.check_availability.return_value = True
                mock_vision.describe_image.return_value = "should not be called"
                docs = list(manager._load_vault_documents(vault_dir))

            self.assertEqual([doc.metadata["source"] for doc in docs], ["note.md"])
            mock_vision.describe_image.assert_not_called()
            self.assertEqual(manager._skipped_image_count, 1)

    def test_only_markdown_referenced_images_are_indexed(self):
        """Image files in the vault are ignored unless an included markdown note references them."""
        from rag.vault import ObsidianVaultManager

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, tempfile.TemporaryDirectory() as index_dir, tempfile.TemporaryDirectory() as cache_dir:
            vault = Path(vault_dir)
            (vault / "referenced.png").write_bytes(b"\x89PNG referenced")
            (vault / "orphan.png").write_bytes(b"\x89PNG orphan")
            (vault / "note.md").write_text("See ![[referenced.png]].", encoding="utf-8")
            manager = ObsidianVaultManager()

            with (
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
                patch("rag.vault.OBSIDIAN_CACHE_DIR", cache_dir),
                patch("rag.vault.load_config", return_value={"vault_image_exts": [".png"]}),
                patch("rag.vault.vision_manager") as mock_vision,
            ):
                mock_vision.describe_image.return_value = "referenced description"
                docs = list(manager._load_vault_documents(vault_dir))

            sources = {doc.metadata["source"] for doc in docs}
            self.assertIn("note.md", sources)
            self.assertIn("referenced.png", sources)
            self.assertNotIn("orphan.png", sources)
            self.assertEqual(mock_vision.describe_image.call_count, 1)
            # Orphan images must not be counted as skipped either — they
            # never enter the indexing pipeline in the first place.
            self.assertEqual(manager._skipped_image_count, 0)

    def test_bare_wikilink_resolves_image_in_central_folder(self):
        """A bare ![[image.png]] link resolves to a central attachments folder.

        Obsidian's default "shortest path" link format references images by
        bare filename even when they live in a vault-wide attachments folder
        rather than beside the note. The loader must resolve those via a
        vault-wide basename lookup; the historical parent-relative-only
        behaviour silently dropped every such image.
        """
        from rag.vault import ObsidianVaultManager

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, tempfile.TemporaryDirectory() as index_dir, tempfile.TemporaryDirectory() as cache_dir:
            vault = Path(vault_dir)
            (vault / "Z_attachments").mkdir()
            (vault / "notes").mkdir()
            # Image lives in a central folder; the note lives elsewhere and
            # references it by bare filename — the real-world failing case.
            (vault / "Z_attachments" / "figure.png").write_bytes(b"\x89PNG central")
            (vault / "notes" / "paper.md").write_text(
                "See ![[figure.png]].", encoding="utf-8"
            )
            manager = ObsidianVaultManager()

            with (
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
                patch("rag.vault.OBSIDIAN_CACHE_DIR", cache_dir),
                patch("rag.vault.load_config", return_value={"vault_image_exts": [".png"]}),
                patch("rag.vault.vision_manager") as mock_vision,
            ):
                mock_vision.describe_image.return_value = "central figure description"
                docs = list(manager._load_vault_documents(vault_dir))

            sources = {doc.metadata["source"] for doc in docs}
            self.assertIn("notes/paper.md", sources)
            # Resolved to its true vault-relative location, not notes/figure.png.
            self.assertIn("Z_attachments/figure.png", sources)
            self.assertEqual(mock_vision.describe_image.call_count, 1)
            self.assertEqual(manager._skipped_image_count, 0)

    def test_unreferenced_pdf_is_still_folder_driven(self):
        """PDFs remain indexable by included folder even when no markdown links to them."""
        from rag.vault import ObsidianVaultManager

        class FakeSections:
            full_text = "pdf text"

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, tempfile.TemporaryDirectory() as index_dir, tempfile.TemporaryDirectory() as cache_dir:
            vault = Path(vault_dir)
            (vault / "paper.pdf").write_bytes(b"%PDF fake content")
            manager = ObsidianVaultManager()

            with (
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
                patch("rag.vault.OBSIDIAN_CACHE_DIR", cache_dir),
                patch("rag.vault.load_config", return_value={"vault_image_exts": [".png"]}),
                patch("rag.vault.extract_structured_from_pdf", return_value=FakeSections()) as extract,
            ):
                docs = list(manager._load_vault_documents(vault_dir))

            self.assertEqual(extract.call_count, 1)
            self.assertEqual([(doc.metadata["source"], doc.text) for doc in docs], [("paper.pdf", "pdf text")])

    def test_md_attachment_metadata_extracted_from_chunk(self):
        """Markdown chunks must record wikilink and inline-link targets."""
        from rag.vault import ObsidianVaultManager
        from llama_index.core import Document as LlamaDocument

        manager = ObsidianVaultManager()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir:
            md_path = Path(vault_dir) / "notes" / "foo.md"
            md_path.parent.mkdir(parents=True)
            md_text = (
                "# Heading\n\n"
                "Refs ![[diagram.png]] and [paper](papers/study.pdf) "
                "and external [docs](https://example.com)."
            )
            md_path.write_text(md_text, encoding="utf-8")
            raw = LlamaDocument(
                text=md_text,
                metadata={
                    "file_path": str(md_path),
                    "source": "notes/foo.md",
                    "extension": ".md",
                },
            )
            chunks = list(manager._chunk_raw_documents([raw], vault_dir))

        all_attachments: set[str] = set()
        for chunk in chunks:
            for path in chunk.metadata.get("attachments", []):
                all_attachments.add(path)
        self.assertIn("notes/diagram.png", all_attachments)
        self.assertIn("notes/papers/study.pdf", all_attachments)
        # External URLs must be dropped.
        self.assertFalse(any("example.com" in p for p in all_attachments))

    def test_md_attachments_excluded_from_embed_and_llm_metadata(self):
        """The attachments list is stored but excluded from embed/LLM text.

        A chunk that carries attachments must keep the value in metadata
        (retrieval/manifest use) while listing the key in both excluded-metadata
        sets, so the insert-time metadata-aware splitter does not fold a long
        link list into the per-node metadata budget. Chunks without attachments
        must not gain a spurious exclusion.
        """
        from rag.vault import ObsidianVaultManager
        from llama_index.core import Document as LlamaDocument

        manager = ObsidianVaultManager()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir:
            md_path = Path(vault_dir) / "notes" / "foo.md"
            md_path.parent.mkdir(parents=True)
            md_text = "# Heading\n\nRef ![[diagram.png]] here."
            md_path.write_text(md_text, encoding="utf-8")
            with_attach = LlamaDocument(
                text=md_text,
                metadata={
                    "file_path": str(md_path),
                    "source": "notes/foo.md",
                    "extension": ".md",
                },
            )
            plain_path = Path(vault_dir) / "notes" / "bar.md"
            plain_text = "# Plain\n\nNo links at all."
            plain_path.write_text(plain_text, encoding="utf-8")
            plain = LlamaDocument(
                text=plain_text,
                metadata={
                    "file_path": str(plain_path),
                    "source": "notes/bar.md",
                    "extension": ".md",
                },
            )
            chunks = list(manager._chunk_raw_documents([with_attach, plain], vault_dir))

        attach_chunks = [c for c in chunks if c.metadata.get("attachments")]
        self.assertTrue(attach_chunks, "expected at least one chunk with attachments")
        for chunk in attach_chunks:
            # Value is still present for downstream consumers.
            self.assertIn("notes/diagram.png", chunk.metadata["attachments"])
            # ...but excluded from both embed and LLM metadata strings.
            self.assertIn("attachments", chunk.excluded_embed_metadata_keys)
            self.assertIn("attachments", chunk.excluded_llm_metadata_keys)

        plain_chunks = [c for c in chunks if c.metadata.get("source") == "notes/bar.md"]
        self.assertTrue(plain_chunks)
        for chunk in plain_chunks:
            self.assertNotIn("attachments", chunk.excluded_embed_metadata_keys)

    def test_oversized_attachments_metadata_survives_insert_splitter(self):
        """A huge attachments list must not fail the insert-time splitter.

        Reproduces the ``_tags.md`` failure ("Metadata length (N) is longer than
        chunk size (1024)"): an index note links to 100+ files, so its resolved
        attachments list serialises past the default chunk_size. With the key
        excluded from embed/LLM metadata the metadata-aware splitter ignores it
        and the node parses cleanly; without the exclusion it raises, proving the
        guard is real (and that our fix is what relieves it).
        """
        from llama_index.core import Document as LlamaDocument
        from llama_index.core.node_parser import SentenceSplitter

        # ~120 long note names — comparable to the real _tags.md (1068 tokens).
        big_attachments = [f"notes/long_disorder_name_number_{i:03d}" for i in range(120)]
        splitter = SentenceSplitter(chunk_size=1024, chunk_overlap=64)

        guarded = LlamaDocument(
            text="# Tags\n\nA small index table.",
            metadata={"source": "_tags.md", "attachments": big_attachments},
            excluded_embed_metadata_keys=["attachments"],
            excluded_llm_metadata_keys=["attachments"],
        )
        # Must not raise — the splitter measures the filtered metadata string.
        nodes = splitter.get_nodes_from_documents([guarded])
        self.assertTrue(nodes)

        # Control: the same document WITHOUT the exclusion does raise, so the
        # test is exercising the real guard rather than a no-op.
        unguarded = LlamaDocument(
            text="# Tags\n\nA small index table.",
            metadata={"source": "_tags.md", "attachments": big_attachments},
        )
        with self.assertRaises(ValueError):
            splitter.get_nodes_from_documents([unguarded])

    def test_max_blocks_for_range_scales_with_pages(self):
        """The PDF block cap keeps the 50k floor for small docs and scales up.

        A flat 50k cap skipped legitimately large books (e.g. thoma_2026.pdf:
        845 pages, ~66k blocks). The cap now floors at 50k (so a small, densely
        fragmented PDF is still rejected) but scales with the page range so a
        large document extracts.
        """
        from pdf_extractor import (
            _max_blocks_for_range,
            _MAX_BLOCKS_FLOOR,
            _MAX_BLOCKS_PER_PAGE,
        )

        # Small ranges never drop below the historical guard.
        self.assertEqual(_max_blocks_for_range(1), _MAX_BLOCKS_FLOOR)
        self.assertEqual(_max_blocks_for_range(30), _MAX_BLOCKS_FLOOR)
        self.assertEqual(_max_blocks_for_range(0), _MAX_BLOCKS_FLOOR)

        # Large ranges scale linearly...
        self.assertEqual(_max_blocks_for_range(845), 845 * _MAX_BLOCKS_PER_PAGE)
        # ...with comfortable headroom over the ~66k blocks that file produces.
        self.assertGreater(_max_blocks_for_range(845), 66_000)

    def test_cancel_persists_partial_false(self):
        """A user-initiated cancel mid-run must persist partial=False so a
        cold boot does not surface the Resume button."""
        from rag.vault import ObsidianVaultManager
        from llama_index.core.embeddings import MockEmbedding

        cancel_signal = threading.Event()
        release_signal = threading.Event()

        class FakeProvider:
            def get_embedding(self, _name):
                return MockEmbedding(embed_dim=2)

        class FakeStorageContext:
            def persist(self, persist_dir):
                Path(persist_dir).mkdir(parents=True, exist_ok=True)

        class FakeStorageContextFactory:
            @staticmethod
            def from_defaults(*_args, **_kwargs):
                return FakeStorageContext()

        class BlockingFakeIndex:
            def __init__(self):
                self.storage_context = FakeStorageContext()

            @classmethod
            def from_documents(cls, *_args, **_kwargs):
                return cls()

            def insert(self, _doc):
                cancel_signal.set()
                release_signal.wait(timeout=5)

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, tempfile.TemporaryDirectory() as index_dir:
            Path(vault_dir, "note.md").write_text("# A\n\nbody", encoding="utf-8")
            manager = ObsidianVaultManager()
            manager.restore_vault_path(vault_dir)

            def run_index():
                with (
                    patch("rag.vault.get_provider", return_value=FakeProvider()),
                    patch("rag.vault.StorageContext", FakeStorageContextFactory),
                    patch("rag.vault.VectorStoreIndex", BlockingFakeIndex),
                    patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
                ):
                    manager.index_vault("llm", "embed", provider_name="ollama")

            thread = threading.Thread(target=run_index, daemon=True)
            thread.start()
            self.assertTrue(cancel_signal.wait(timeout=5), "indexer never reached insert")
            manager.cancel_indexing()
            release_signal.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "indexer thread did not exit")

            meta = json.loads(Path(index_dir, "obsidian_meta.json").read_text(encoding="utf-8"))
        self.assertFalse(meta.get("partial"), meta)

    def test_pause_during_scan_does_not_claim_queryable_partial_index(self):
        from rag.vault import ObsidianVaultManager
        from llama_index.core.embeddings import MockEmbedding

        class FakeProvider:
            def get_embedding(self, _name):
                return MockEmbedding(embed_dim=2)

        class FakeStorageContext:
            def persist(self, persist_dir):
                Path(persist_dir).mkdir(parents=True, exist_ok=True)

        class FakeStorageContextFactory:
            @staticmethod
            def from_defaults(*_args, **_kwargs):
                return FakeStorageContext()

        class EmptyFakeIndex:
            def __init__(self):
                self.storage_context = FakeStorageContext()
                self.docstore = None

            @classmethod
            def from_documents(cls, *_args, **_kwargs):
                return cls()

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, tempfile.TemporaryDirectory() as index_dir:
            Path(vault_dir, "note.md").write_text("# A\n\nbody", encoding="utf-8")
            manager = ObsidianVaultManager()
            manager.restore_vault_path(vault_dir)

            def stop_during_scan(_vault, op_epoch=0):
                manager.pause_indexing()
                return []

            with (
                patch("rag.vault.get_provider", return_value=FakeProvider()),
                patch("rag.vault.StorageContext", FakeStorageContextFactory),
                patch("rag.vault.VectorStoreIndex", EmptyFakeIndex),
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
                patch.object(manager, "_load_vault_documents", side_effect=stop_during_scan),
            ):
                manager.index_vault("llm", "embed", provider_name="ollama")

            meta = json.loads(Path(index_dir, "obsidian_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(manager.get_status(), "paused_scan")
            self.assertTrue(meta.get("partial"), meta)
            self.assertFalse(meta.get("has_vector_data"), meta)
            self.assertEqual(meta.get("phase"), "paused_scan")

    def test_paused_scan_recovered_after_restart(self):
        """A fresh manager pointed at a paused_scan meta must recover the state
        and its phase from disk, even without docstore.json on disk."""
        from rag.vault import ObsidianVaultManager, OBSIDIAN_INDEX_VERSION

        with tempfile.TemporaryDirectory() as index_dir:
            meta_path = Path(index_dir) / "obsidian_meta.json"
            meta_path.write_text(json.dumps({
                "version": OBSIDIAN_INDEX_VERSION,
                "indexed_at": "2026-01-01T00:00:00+00:00",
                "embed": "embed-A",
                "provider": "ollama",
                "partial": True,
                "phase": "paused_scan",
                "has_vector_data": False,
                "inserted_this_run": 0,
            }), encoding="utf-8")

            with patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir):
                manager = ObsidianVaultManager()
                state = manager.get_status()
                payload = manager.get_status_payload()

            self.assertEqual(state, "paused_scan")
            self.assertEqual(payload["state"], "paused_scan")
            self.assertEqual(payload["phase"], "paused_scan")

    def test_paused_partial_recovered_after_restart(self):
        """Recovered paused_partial state should surface phase=paused_partial."""
        from rag.vault import ObsidianVaultManager, OBSIDIAN_INDEX_VERSION

        with tempfile.TemporaryDirectory() as index_dir:
            Path(index_dir, "obsidian_meta.json").write_text(json.dumps({
                "version": OBSIDIAN_INDEX_VERSION,
                "indexed_at": "2026-01-01T00:00:00+00:00",
                "embed": "embed-A",
                "provider": "ollama",
                "partial": True,
                "phase": "paused_partial",
                "has_vector_data": True,
                "inserted_this_run": 12,
            }), encoding="utf-8")
            # Minimal docstore that satisfies _index_dir_has_vector_data.
            Path(index_dir, "docstore.json").write_text(json.dumps({
                "docstore/ref_doc_info": {"some-doc-id": {"node_ids": ["n1"]}}
            }), encoding="utf-8")
            Path(index_dir, "index_store.json").write_text('{"index_store/data":{}}', encoding="utf-8")
            Path(index_dir, "default__vector_store.json").write_text(
                '{"embedding_dict":{"n1":[0.1]},"text_id_to_ref_doc_id":{"n1":"some-doc-id"},"metadata_dict":{"n1":{}}}',
                encoding="utf-8",
            )

            with patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir):
                manager = ObsidianVaultManager()
                payload = manager.get_status_payload()

            self.assertEqual(payload["state"], "paused_partial")
            self.assertEqual(payload["phase"], "paused_partial")

    def test_corrupt_vector_tail_does_not_recover_as_queryable_partial(self):
        from rag.vault import ObsidianVaultManager, OBSIDIAN_INDEX_VERSION

        with tempfile.TemporaryDirectory() as index_dir:
            Path(index_dir, "obsidian_meta.json").write_text(json.dumps({
                "version": OBSIDIAN_INDEX_VERSION,
                "indexed_at": "2026-01-01T00:00:00+00:00",
                "embed": "embed-A",
                "provider": "ollama",
                "partial": True,
                "phase": "paused_partial",
                "has_vector_data": True,
                "inserted_this_run": 12,
            }), encoding="utf-8")
            Path(index_dir, "docstore.json").write_text('{"docstore/ref_doc_info":{"doc":{"node_ids":["n1"]}}}', encoding="utf-8")
            Path(index_dir, "index_store.json").write_text('{"index_store/data":{}}', encoding="utf-8")
            Path(index_dir, "default__vector_store.json").write_text('{"embedding_dict":{"n1":[0.1,', encoding="utf-8")

            with patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir):
                manager = ObsidianVaultManager()
                payload = manager.get_status_payload()

            self.assertEqual(payload["state"], "paused_scan")
            self.assertIn("checkpoint", payload["integrity_error"].lower())

    def test_checkpoint_persist_does_not_replace_active_files_when_temp_invalid(self):
        from rag.vault import ObsidianVaultManager

        class BadStorageContext:
            def persist(self, persist_dir):
                root = Path(persist_dir)
                root.mkdir(parents=True, exist_ok=True)
                (root / "docstore.json").write_text('{"docstore/ref_doc_info":{"doc":{"node_ids":["n1"]}}}', encoding="utf-8")
                (root / "index_store.json").write_text('{"index_store/data":{}}', encoding="utf-8")
                (root / "default__vector_store.json").write_text('{"embedding_dict":{"n1":[0.1,', encoding="utf-8")

        class BadIndex:
            storage_context = BadStorageContext()

        with tempfile.TemporaryDirectory() as index_dir:
            active_vector = Path(index_dir, "default__vector_store.json")
            active_vector.write_text('{"embedding_dict":{},"text_id_to_ref_doc_id":{},"metadata_dict":{}}', encoding="utf-8")
            manager = ObsidianVaultManager()

            with patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir):
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    manager._persist_index_checkpoint(BadIndex())

            self.assertEqual(
                active_vector.read_text(encoding="utf-8"),
                '{"embedding_dict":{},"text_id_to_ref_doc_id":{},"metadata_dict":{}}',
            )

    def test_cancel_clears_paused_meta_synchronously(self):
        """cancel_indexing must rewrite obsidian_meta.json with partial=False
        before returning, so a fast get_status() can't flicker back to paused."""
        from rag.vault import ObsidianVaultManager, OBSIDIAN_INDEX_VERSION

        with tempfile.TemporaryDirectory() as index_dir:
            meta_path = Path(index_dir, "obsidian_meta.json")
            meta_path.write_text(json.dumps({
                "version": OBSIDIAN_INDEX_VERSION,
                "indexed_at": "2026-01-01T00:00:00+00:00",
                "embed": "embed-A",
                "provider": "ollama",
                "partial": True,
                "phase": "paused_partial",
                "has_vector_data": True,
                "inserted_this_run": 3,
            }), encoding="utf-8")
            Path(index_dir, "docstore.json").write_text(
                '{"docstore/ref_doc_info":{"doc":{"node_ids":["n1"]}}}',
                encoding="utf-8",
            )
            Path(index_dir, "index_store.json").write_text('{"index_store/data":{}}', encoding="utf-8")
            Path(index_dir, "default__vector_store.json").write_text(
                '{"embedding_dict":{"n1":[0.1]},"text_id_to_ref_doc_id":{"n1":"doc"},"metadata_dict":{"n1":{}}}',
                encoding="utf-8",
            )

            with patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir):
                manager = ObsidianVaultManager()
                # Force the in-memory state into paused_partial so the cancel
                # path takes the disk-rewrite branch.
                manager._index_state = "paused_partial"
                manager.cancel_indexing()

                meta_after = json.loads(meta_path.read_text(encoding="utf-8"))
                self.assertFalse(meta_after.get("partial"), meta_after)
                self.assertEqual(meta_after.get("phase"), "idle")
                # And a get_status() immediately afterwards should not flicker.
                self.assertEqual(manager.get_status(), "done")  # has_vector_data=True

    def test_pdf_signatures_preserved_when_scan_interrupted(self):
        """An interrupted scan must keep prior-run signatures so resume can
        fast-path unchanged PDFs instead of re-hashing them from scratch."""
        from rag.vault import ObsidianVaultManager

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, tempfile.TemporaryDirectory() as cache_dir:
            vault = Path(vault_dir)
            (vault / "a.pdf").write_bytes(b"%PDF a")
            (vault / "b.pdf").write_bytes(b"%PDF b")

            seed = {
                "a.pdf": {"size": 99, "mtime_ns": 1, "sha256": "deadbeef-a"},
                "b.pdf": {"size": 99, "mtime_ns": 1, "sha256": "deadbeef-b"},
            }

            manager = ObsidianVaultManager()
            manager._stop_event.set()

            with (
                patch("rag.vault.OBSIDIAN_CACHE_DIR", cache_dir),
                patch("rag.vault.load_config", return_value={}),
                patch.object(ObsidianVaultManager, "_load_pdf_signature_cache", return_value=dict(seed)),
            ):
                # Exhaust the generator so the try/finally in
                # ``_load_vault_documents`` runs the signature-cache write.
                list(manager._load_vault_documents(vault_dir))

            saved_path = Path(cache_dir) / "pdf_signatures.json"
            self.assertTrue(saved_path.exists(), "signature cache was not written")
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
            self.assertEqual(saved, seed)

    def test_pdf_signatures_preserved_on_early_generator_close(self):
        """Closing the loader generator early (without setting ``_stop_event``)
        used to write a partial ``new_sig_cache`` over the prior-run cache,
        silently evicting signatures for PDFs the consumer didn't reach.
        Track scan-completion separately from the stop event so any early
        exit — stop event, exception, or consumer close — preserves prior
        signatures.
        """
        from rag.vault import ObsidianVaultManager

        class FakeSections:
            full_text = "pdf body"

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, \
             tempfile.TemporaryDirectory() as cache_dir:
            vault = Path(vault_dir)
            (vault / "note.md").write_text("a note", encoding="utf-8")
            (vault / "a.pdf").write_bytes(b"%PDF a")
            (vault / "b.pdf").write_bytes(b"%PDF b")

            seed = {
                "a.pdf": {"size": 1, "mtime_ns": 1, "sha256": "deadbeef-a"},
                "b.pdf": {"size": 1, "mtime_ns": 1, "sha256": "deadbeef-b"},
            }

            manager = ObsidianVaultManager()
            # NB: _stop_event stays cleared.  Generator is closed by the
            # consumer pulling only the first document and dropping the rest.

            with (
                patch("rag.vault.OBSIDIAN_CACHE_DIR", cache_dir),
                patch("rag.vault.load_config", return_value={}),
                patch.object(ObsidianVaultManager, "_load_pdf_signature_cache", return_value=dict(seed)),
                patch("rag.vault.extract_structured_from_pdf", return_value=FakeSections()),
            ):
                gen = manager._load_vault_documents(vault_dir)
                next(gen)  # one MD doc; PDFs not yet touched
                gen.close()  # close() raises GeneratorExit inside the body

            saved_path = Path(cache_dir) / "pdf_signatures.json"
            self.assertTrue(saved_path.exists(), "signature cache was not written")
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
            # Prior signatures survived the early close.  Without the
            # scan_completed guard this would be {} (or partial), evicting
            # b.pdf's signature and forcing a re-hash on the next run.
            self.assertEqual(saved, seed)

    def test_embed_change_forces_fresh_index_when_vector_data_present(self):
        """Changing the embedding model between runs must force a rebuild
        (with a warning + .bak archive) instead of silently going incremental."""
        from rag.vault import ObsidianVaultManager, OBSIDIAN_INDEX_VERSION
        from llama_index.core.embeddings import MockEmbedding

        class FakeProvider:
            def get_embedding(self, _name):
                return MockEmbedding(embed_dim=2)

        class FakeStorageContext:
            def persist(self, persist_dir):
                Path(persist_dir).mkdir(parents=True, exist_ok=True)

        class FakeStorageContextFactory:
            @staticmethod
            def from_defaults(*_args, **_kwargs):
                return FakeStorageContext()

        class EmptyFakeIndex:
            def __init__(self):
                self.storage_context = FakeStorageContext()
                self.docstore = None

            @classmethod
            def from_documents(cls, *_args, **_kwargs):
                return cls()

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, tempfile.TemporaryDirectory() as index_dir:
            Path(vault_dir, "note.md").write_text("# A\n\nbody", encoding="utf-8")
            # Seed prior state: complete index built with embed-A.
            Path(index_dir, "obsidian_meta.json").write_text(json.dumps({
                "version": OBSIDIAN_INDEX_VERSION,
                "indexed_at": "2026-01-01T00:00:00+00:00",
                "embed": "embed-A",
                "provider": "ollama",
                "partial": False,
                "phase": "done",
                "has_vector_data": True,
                "inserted_this_run": 5,
            }), encoding="utf-8")
            Path(index_dir, "docstore.json").write_text(json.dumps({
                "docstore/ref_doc_info": {"old-doc": {"node_ids": ["n1"]}}
            }), encoding="utf-8")
            Path(index_dir, "index_store.json").write_text('{"index_store/data":{}}', encoding="utf-8")
            Path(index_dir, "default__vector_store.json").write_text(
                '{"embedding_dict":{"n1":[0.1]},"text_id_to_ref_doc_id":{"n1":"old-doc"},"metadata_dict":{"n1":{}}}',
                encoding="utf-8",
            )

            manager = ObsidianVaultManager()
            manager.restore_vault_path(vault_dir)

            # Stop after the setup phase so the test focuses on the
            # incremental-decision branch, not the streaming indexer.
            def stop_after_setup(_vault, op_epoch=0):
                manager.request_stop()
                return []

            messages: list[str] = []
            manager.set_status_callback(messages.append)

            with (
                patch("rag.vault.get_provider", return_value=FakeProvider()),
                patch("rag.vault.StorageContext", FakeStorageContextFactory),
                patch("rag.vault.VectorStoreIndex", EmptyFakeIndex),
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
                patch.object(manager, "_load_vault_documents", side_effect=stop_after_setup),
            ):
                manager.index_vault("llm", "embed-B", provider_name="ollama")

            self.assertTrue(
                any("embedding" in m.lower() and "embed-A" in m and "embed-B" in m for m in messages),
                f"expected embed-mismatch warning, got: {messages}",
            )
            siblings = [p.name for p in Path(index_dir).parent.iterdir()]
            self.assertTrue(
                any(s.startswith(Path(index_dir).name + ".bak.") for s in siblings),
                f"expected a .bak.* archive of the old index dir, got: {siblings}",
            )
            new_meta = json.loads(Path(index_dir, "obsidian_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(new_meta.get("embed"), "embed-B")

    def test_embed_change_during_paused_scan_does_not_warn(self):
        """A paused_scan (no persisted vectors) followed by an embed-model
        change has nothing to be incompatible with — start fresh silently."""
        from rag.vault import ObsidianVaultManager, OBSIDIAN_INDEX_VERSION
        from llama_index.core.embeddings import MockEmbedding

        class FakeProvider:
            def get_embedding(self, _name):
                return MockEmbedding(embed_dim=2)

        class FakeStorageContext:
            def persist(self, persist_dir):
                Path(persist_dir).mkdir(parents=True, exist_ok=True)

        class FakeStorageContextFactory:
            @staticmethod
            def from_defaults(*_args, **_kwargs):
                return FakeStorageContext()

        class EmptyFakeIndex:
            def __init__(self):
                self.storage_context = FakeStorageContext()
                self.docstore = None

            @classmethod
            def from_documents(cls, *_args, **_kwargs):
                return cls()

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir, tempfile.TemporaryDirectory() as index_dir:
            Path(vault_dir, "note.md").write_text("# A\n\nbody", encoding="utf-8")
            # Seed prior state: paused_scan — partial=True but no vector data.
            Path(index_dir, "obsidian_meta.json").write_text(json.dumps({
                "version": OBSIDIAN_INDEX_VERSION,
                "indexed_at": "2026-01-01T00:00:00+00:00",
                "embed": "embed-A",
                "provider": "ollama",
                "partial": True,
                "phase": "paused_scan",
                "has_vector_data": False,
                "inserted_this_run": 0,
            }), encoding="utf-8")

            manager = ObsidianVaultManager()
            manager.restore_vault_path(vault_dir)
            messages: list[str] = []
            manager.set_status_callback(messages.append)

            def stop_after_setup(_vault, op_epoch=0):
                manager.request_stop()
                return []

            with (
                patch("rag.vault.get_provider", return_value=FakeProvider()),
                patch("rag.vault.StorageContext", FakeStorageContextFactory),
                patch("rag.vault.VectorStoreIndex", EmptyFakeIndex),
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
                patch.object(manager, "_load_vault_documents", side_effect=stop_after_setup),
            ):
                manager.index_vault("llm", "embed-B", provider_name="ollama")

            # No embed-mismatch warning should have been emitted.
            self.assertFalse(
                any("embed-A" in m and "embed-B" in m and "WARNING" in m for m in messages),
                f"unexpected embed-mismatch warning: {messages}",
            )
            # And no .bak archive either — there was nothing worth preserving.
            siblings = [p.name for p in Path(index_dir).parent.iterdir()]
            self.assertFalse(
                any(s.startswith(Path(index_dir).name + ".bak.") for s in siblings),
                f"unexpected archive of empty paused_scan dir: {siblings}",
            )
            # New meta reflects the new embed model.
            new_meta = json.loads(Path(index_dir, "obsidian_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(new_meta.get("embed"), "embed-B")


class TestEmbedBatchParity(unittest.TestCase):
    """Track 5.6 pinning: batched embedding is a pure round-trip optimization.

    Invariants: (1) a batched run produces an IDENTICAL index to a
    batch-size-1 run — same docstore texts, same per-document hashes, same
    per-text vectors — with strictly fewer embed calls; (2) a re-run over the
    same chunks skips everything by hash with ZERO embed calls (the
    reindex-neutral guarantee); (3) a failing batch falls back to per-doc
    inserts (no chunk lost, no spurious failure counts); (4) a dead backend
    still trips the consecutive-failure breaker at the same bound.

    These drive the REAL VectorStoreIndex + transformation pipeline — the
    other indexer tests use minimal fakes and therefore exercise the per-doc
    fallback path, not the batch path.
    """

    @staticmethod
    def _chunk_docs(n=5):
        from llama_index.core import Document as LlamaDocument
        return [
            LlamaDocument(
                text=f"Chunk body number {i} with distinct content.",
                doc_id=f"note{i}.md::0",
                metadata={"source": f"note{i}.md"},
            )
            for i in range(n)
        ]

    @staticmethod
    def _embedder(batch_log):
        """Deterministic per-text vectors + a log of embed-call batch sizes."""
        import hashlib
        from llama_index.core.embeddings import MockEmbedding

        class HashEmbedding(MockEmbedding):
            def _vec(self, text):
                h = hashlib.sha256(text.encode()).digest()
                return [b / 255.0 for b in h[:8]]

            def _get_text_embedding(self, text):
                return self._vec(text)

            def _get_query_embedding(self, query):
                return self._vec(query)

            def _get_text_embeddings(self, texts):
                batch_log.append(len(texts))
                return [self._vec(t) for t in texts]

        return HashEmbedding(embed_dim=8)

    @staticmethod
    def _index_for(embed_model):
        from llama_index.core import VectorStoreIndex
        return VectorStoreIndex.from_documents([], embed_model=embed_model)

    @staticmethod
    def _text_to_vec(idx):
        """{node text: embedding} — node ids are random UUIDs, text is stable."""
        vecs = idx.vector_store._data.embedding_dict
        return {
            node.get_content(): vecs[node_id]
            for node_id, node in idx.docstore.docs.items()
        }

    def _run(self, idx, docs, batch_size):
        from unittest.mock import patch as _patch
        from rag.vault import ObsidianVaultManager
        manager = ObsidianVaultManager()
        with _patch.object(ObsidianVaultManager, "_embed_batch_size", return_value=batch_size):
            return manager, manager._index_documents_streaming(idx, iter(docs))

    def test_batched_run_is_identical_to_per_doc_run_with_fewer_calls(self):
        docs = self._chunk_docs(5)
        calls_a, calls_b = [], []
        idx_a = self._index_for(self._embedder(calls_a))
        idx_b = self._index_for(self._embedder(calls_b))
        calls_a.clear()   # drop construction-time calls, if any
        calls_b.clear()

        _, (added_a, skipped_a, *_rest_a) = self._run(idx_a, self._chunk_docs(5), 1)
        _, (added_b, skipped_b, *_rest_b) = self._run(idx_b, docs, 3)

        self.assertEqual((added_a, skipped_a), (5, 0))
        self.assertEqual((added_b, skipped_b), (5, 0))
        # Same texts, same per-document hashes, same per-text vectors.
        self.assertEqual(
            sorted(n.get_content() for n in idx_a.docstore.docs.values()),
            sorted(n.get_content() for n in idx_b.docstore.docs.values()),
        )
        for d in docs:
            self.assertEqual(
                idx_a.docstore.get_document_hash(d.doc_id),
                idx_b.docstore.get_document_hash(d.doc_id),
            )
        self.assertEqual(self._text_to_vec(idx_a), self._text_to_vec(idx_b))
        # Legacy: one embed call per chunk. Batched: one per flush (3 + 2).
        self.assertEqual(calls_a, [1, 1, 1, 1, 1])
        self.assertEqual(calls_b, [3, 2])

    def test_rerun_skips_everything_with_zero_embed_calls(self):
        # The reindex-neutral pin: chunk ids/hashes are untouched by batching,
        # so an identical second run must re-embed NOTHING.
        calls = []
        idx = self._index_for(self._embedder(calls))
        self._run(idx, self._chunk_docs(5), 3)
        calls.clear()
        _, (added, skipped, _deleted, failed, _counts) = self._run(idx, self._chunk_docs(5), 3)
        self.assertEqual((added, skipped, failed), (0, 5, 0))
        self.assertEqual(calls, [])

    def test_failed_batch_falls_back_per_doc_without_losing_chunks(self):
        import hashlib
        from llama_index.core.embeddings import MockEmbedding

        class BatchAllergicEmbedding(MockEmbedding):
            """Fails any multi-text call; singles succeed — the shape of a
            backend whose batch endpoint is broken."""
            def _vec(self, text):
                h = hashlib.sha256(text.encode()).digest()
                return [b / 255.0 for b in h[:8]]
            def _get_text_embedding(self, text):
                return self._vec(text)
            def _get_query_embedding(self, query):
                return self._vec(query)
            def _get_text_embeddings(self, texts):
                if len(texts) > 1:
                    raise RuntimeError("batch endpoint broken")
                return [self._vec(texts[0])]

        from rag.vault import ObsidianVaultManager
        idx = self._index_for(BatchAllergicEmbedding(embed_dim=8))
        with (
            patch.object(ObsidianVaultManager, "_embed_batch_size", return_value=3),
            patch.object(ObsidianVaultManager, "_FAILURE_BACKOFF_BASE_S", 0),
            patch.object(ObsidianVaultManager, "_FAILURE_BACKOFF_CAP_S", 0),
        ):
            manager = ObsidianVaultManager()
            added, skipped, _deleted, failed, _c = manager._index_documents_streaming(
                idx, iter(self._chunk_docs(5)))
        # Every chunk landed via the per-doc fallback; the failed BATCH is not
        # a chunk failure (no chunk was lost, so none may be counted).
        self.assertEqual((added, skipped, failed), (5, 0, 0))
        self.assertEqual(len(idx.docstore.docs), 5)

    def test_dead_backend_still_trips_the_breaker(self):
        from llama_index.core.embeddings import MockEmbedding

        class DeadEmbedding(MockEmbedding):
            def _get_text_embedding(self, text):
                raise RuntimeError("backend down")
            def _get_query_embedding(self, query):
                raise RuntimeError("backend down")
            def _get_text_embeddings(self, texts):
                raise RuntimeError("backend down")

        from rag.vault import ObsidianVaultManager
        idx = self._index_for(DeadEmbedding(embed_dim=8))
        with (
            patch.object(ObsidianVaultManager, "_embed_batch_size", return_value=3),
            patch.object(ObsidianVaultManager, "_FAILURE_BACKOFF_BASE_S", 0),
            patch.object(ObsidianVaultManager, "_FAILURE_BACKOFF_CAP_S", 0),
        ):
            manager = ObsidianVaultManager()
            added, _s, _d, failed, _c = manager._index_documents_streaming(
                idx, iter(self._chunk_docs(30)))
        self.assertEqual(added, 0)
        # The per-doc fallback preserves the consecutive-failure bound — one
        # bad batch never costs K breaker strikes.
        self.assertEqual(failed, ObsidianVaultManager._MAX_CONSECUTIVE_FAILURES)
        self.assertTrue(manager._stop_event.is_set())

    def test_flush_over_default_subbatch_is_one_call_only_when_aligned(self):
        """The 'one HTTP call per flush' claim for a flush BIGGER than the embed
        model's default sub-batch (10) — the case the batch-3 parity test can't
        reach. rag.vault aligns embed_model.embed_batch_size to the knob
        post-construction (vault ~955); with that alignment a 15-chunk flush is
        ONE call [15], and WITHOUT it BaseEmbedding.get_text_embedding_batch
        re-splits it into [10, 5]. This pins that the alignment is what delivers
        the round-trip saving (finding: the alignment is silent-fail + untested).
        """
        # Aligned: mirror what rag.vault does to the indexing embed model.
        calls = []
        aligned = self._embedder(calls)
        aligned.embed_batch_size = 15
        idx = self._index_for(aligned)
        calls.clear()
        _, (added, skipped, *_rest) = self._run(idx, self._chunk_docs(15), 15)
        self.assertEqual((added, skipped), (15, 0))
        self.assertEqual(calls, [15])

        # Un-aligned contrast: the default embed_batch_size (10) splits the same
        # 15-chunk flush into two provider round-trips.
        calls2 = []
        idx2 = self._index_for(self._embedder(calls2))
        calls2.clear()
        self._run(idx2, self._chunk_docs(15), 15)
        self.assertEqual(calls2, [10, 5])

    def test_lancedb_batch_flush_inserts_heterogeneous_metadata(self):
        """Track 5.6 finding: the lancedb BATCH-insert path was untested.

        A single flush mixing MD-only chunks (which fix the struct schema
        MD-first) with a PDF-range chunk (page_start/page_end) and an image
        chunk (is_image) goes through ONE insert_nodes ->
        NormalizingLanceDBVectorStore.add on the batch path. The per-node
        projection must drop the schema-absent keys rather than raise the
        schema-drift ValueError that would abort the run on the breaker; all
        four chunks must land in both the docstore and the vector table.
        """
        from rag.lancedb_store import lancedb_available
        if not lancedb_available():
            self.skipTest("lancedb not installed")
        import tempfile
        from llama_index.core import (
            Document as LlamaDocument,
            StorageContext,
            VectorStoreIndex,
        )
        from rag.vault import ObsidianVaultManager, VECTOR_BACKEND_LANCEDB
        from rag.lancedb_store import make_lancedb_vector_store, lancedb_table_count

        docs = [
            LlamaDocument(text="markdown a", doc_id="a.md::0",
                          metadata={"source": "a.md", "extension": ".md",
                                    "header_path": "H", "attachments": ["f.png"]}),
            LlamaDocument(text="markdown b", doc_id="b.md::0",
                          metadata={"source": "b.md", "extension": ".md"}),
            LlamaDocument(text="textbook range chunk", doc_id="book.pdf::0",
                          metadata={"source": "book.pdf", "extension": ".pdf",
                                    "page_start": 1000, "page_end": 2000}),
            LlamaDocument(text="a labelled brain diagram", doc_id="fig.png::0",
                          metadata={"source": "fig.png", "extension": ".png",
                                    "is_image": True}),
        ]
        calls = []
        with tempfile.TemporaryDirectory() as d:
            vs = make_lancedb_vector_store(d)
            storage_ctx = StorageContext.from_defaults(vector_store=vs)
            idx = VectorStoreIndex.from_documents(
                [], storage_context=storage_ctx,
                embed_model=self._embedder(calls), store_nodes_override=True,
            )
            manager = ObsidianVaultManager()
            with patch.object(ObsidianVaultManager, "_embed_batch_size", return_value=4):
                added, skipped, _deleted, failed, _c = manager._index_documents_streaming(
                    idx, iter(docs), vector_backend=VECTOR_BACKEND_LANCEDB,
                )
            # No schema-drift abort: all four landed, batched as one add.
            self.assertEqual((added, skipped, failed), (4, 0, 0))
            self.assertEqual(len(idx.docstore.docs), 4)
            self.assertEqual(lancedb_table_count(d), 4)
            self.assertEqual(calls, [4])


class TestVaultScanWalk(unittest.TestCase):
    """Track 5.4 pinning: the pruned scandir walk that replaced sorted(rglob).

    Invariants: (1) excluded/reserved dirs are pruned at DESCENT time — their
    contents are never even listed; (2) surviving docs and their order are
    identical to the old globally-sorted walk; (3) a symlinked file escaping
    the vault root stays excluded (a vault symlink cannot pull outside content
    into the index) while one resolving inside the root is kept."""

    def test_excluded_dirs_are_never_descended_and_docs_sorted(self):
        from rag.vault import ObsidianVaultManager

        with tempfile.TemporaryDirectory() as vault_dir:
            vault = Path(vault_dir).resolve()
            (vault / "b_dir").mkdir()
            (vault / "a_dir").mkdir()
            (vault / "skipme" / "deep").mkdir(parents=True)
            (vault / ".trash").mkdir()
            (vault / "b_dir" / "z.md").write_text("z", encoding="utf-8")
            (vault / "a_dir" / "y.md").write_text("y", encoding="utf-8")
            (vault / "root.md").write_text("r", encoding="utf-8")
            (vault / "skipme" / "deep" / "hidden.md").write_text("h", encoding="utf-8")
            (vault / ".trash" / "gone.md").write_text("g", encoding="utf-8")

            manager = ObsidianVaultManager()
            scanned_dirs = []
            real_scandir = os.scandir

            def _spy(path, *a, **k):
                scanned_dirs.append(str(path))
                return real_scandir(path, *a, **k)

            with (
                patch("rag.vault.load_config",
                      return_value={"vault_exclude_dirs": ["skipme"]}),
                patch("rag.vault.os.scandir", side_effect=_spy),
            ):
                docs = list(manager._load_vault_documents(str(vault)))

            sources = [d.metadata["source"] for d in docs]
            # Same file set AND same (sorted) order as the old rglob walk.
            self.assertEqual(sources, ["a_dir/y.md", "b_dir/z.md", "root.md"])
            # Pruned, not filtered: the skipped dirs were never even listed.
            self.assertFalse(any("skipme" in d for d in scanned_dirs), scanned_dirs)
            self.assertFalse(any(".trash" in d for d in scanned_dirs), scanned_dirs)

    def test_symlinked_file_escaping_root_is_skipped_inside_is_kept(self):
        from rag.vault import ObsidianVaultManager

        with tempfile.TemporaryDirectory() as vault_dir, \
                tempfile.TemporaryDirectory() as outside:
            vault = Path(vault_dir).resolve()
            (vault / "real.md").write_text("real", encoding="utf-8")
            outside_md = Path(outside) / "leak.md"
            outside_md.write_text("outside", encoding="utf-8")
            (vault / "leak.md").symlink_to(outside_md)          # escapes root
            (vault / "alias.md").symlink_to(vault / "real.md")  # stays inside

            manager = ObsidianVaultManager()
            with patch("rag.vault.load_config", return_value={}):
                docs = list(manager._load_vault_documents(str(vault)))

            sources = {d.metadata["source"] for d in docs}
            self.assertEqual(sources, {"real.md", "alias.md"})


class TestConfigVaultSync(unittest.TestCase):
    def test_report_types_include_builtins(self):
        from app import app

        app.config["TESTING"] = True
        client = app.test_client()
        headers = {"X-Requested-With": "ChatEKLD"}

        resp = client.get("/api/report-types", headers=headers)

        self.assertEqual(resp.status_code, 200)
        names = {item["name"] for item in resp.get_json()["report_types"]}
        self.assertIn("Clinical Trial (RCT)", names)

    def test_config_update_refreshes_live_vault_path_and_normalises_exclusions(self):
        from app import app
        from rag.vault import obsidian_manager

        app.config["TESTING"] = True
        client = app.test_client()
        headers = {"X-Requested-With": "ChatEKLD"}

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vault_dir:
            nested = Path(vault_dir, "folder", "child")
            nested.mkdir(parents=True)

            with patch("api.routes.config.save_config") as save_config:
                resp = client.post(
                    "/api/config",
                    json={
                        "obsidian_vault_path": vault_dir,
                        "vault_exclude_dirs": [str(nested), "relative/path", "../bad"],
                    },
                    headers=headers,
                )

            self.assertEqual(resp.status_code, 200)
            self.assertEqual(obsidian_manager.get_vault_path(), vault_dir)
            saved = save_config.call_args.args[0]
            self.assertEqual(saved["vault_exclude_dirs"], ["folder/child", "relative/path"])

    def test_config_rejects_unsafe_vault_root(self):
        from app import app

        app.config["TESTING"] = True
        client = app.test_client()
        resp = client.post(
            "/api/config",
            json={"obsidian_vault_path": "/"},
            headers={"X-Requested-With": "ChatEKLD"},
        )

        self.assertEqual(resp.status_code, 400)

    def test_models_endpoint_uses_active_provider(self):
        from app import app

        app.config["TESTING"] = True
        client = app.test_client()
        headers = {"X-Requested-With": "ChatEKLD"}

        class FakeProvider:
            def get_models(self):
                return ["lm-model"], ""

        with (
            patch("core.config.load_config", return_value={"provider": "lm_studio"}),
            patch("api.routes.status.get_provider", return_value=FakeProvider()) as get_provider,
        ):
            resp = client.get("/api/models", headers=headers)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["models"], ["lm-model"])
        get_provider.assert_called_once_with("lm_studio")

    def test_vision_models_endpoint_uses_ollama_models(self):
        from app import app

        app.config["TESTING"] = True
        client = app.test_client()
        headers = {"X-Requested-With": "ChatEKLD"}

        class FakeProvider:
            def get_models(self):
                return ["glm-ocr:latest", "qwen3-vl:4b"], ""

        with (
            patch("api.routes.status.get_provider", return_value=FakeProvider()) as get_provider,
            patch("core.config.load_config", return_value={
                "provider": "lm_studio",
                "ocr_provider": "lm_studio",
                "ocr_model": "glm-ocr:latest",
                "vision_provider": "lm_studio",
                "vision_model": "qwen3-vl:4b",
            }),
        ):
            resp = client.get("/api/vision-models?provider=lm_studio", headers=headers)

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["models"], ["glm-ocr:latest", "qwen3-vl:4b"])
        self.assertEqual(data["selected_model"], "glm-ocr:latest")
        self.assertEqual(data["selected_provider"], "lm_studio")
        self.assertEqual(data["ocr_model"], "glm-ocr:latest")
        self.assertEqual(data["ocr_provider"], "lm_studio")
        get_provider.assert_called_once_with("lm_studio")

    def test_vision_models_endpoint_defaults_to_vision_provider_for_vision_kind(self):
        from app import app

        app.config["TESTING"] = True
        client = app.test_client()
        headers = {"X-Requested-With": "ChatEKLD"}

        class FakeProvider:
            def get_models(self):
                return ["qwen3-vl:4b"], ""

        with (
            patch("api.routes.status.get_provider", return_value=FakeProvider()) as get_provider,
            patch("core.config.load_config", return_value={
                "ocr_provider": "ollama",
                "ocr_model": "glm-ocr:latest",
                "vision_provider": "lm_studio",
                "vision_model": "qwen3-vl:4b",
            }),
        ):
            resp = client.get("/api/vision-models?kind=vision", headers=headers)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["selected_provider"], "lm_studio")
        get_provider.assert_called_once_with("lm_studio")

    def test_config_update_refreshes_live_ocr_and_vision_models(self):
        from app import app

        app.config["TESTING"] = True
        client = app.test_client()
        headers = {"X-Requested-With": "ChatEKLD"}

        class FakeManager:
            def __init__(self):
                self.model = ""
                self.provider = ""

            def set_provider(self, provider):
                self.provider = provider

            def set_model(self, model):
                self.model = model

        fake_ocr = FakeManager()
        fake_vision = FakeManager()
        with (
            patch("api.routes.config.save_config"),
            patch("services.vision.glm_ocr_manager", fake_ocr),
            patch("services.vision.vision_manager", fake_vision),
        ):
            resp = client.post(
                "/api/config",
                json={
                    "ocr_provider": "lm_studio",
                    "ocr_model": "glm-ocr:0.9b",
                    "vision_provider": "lm_studio",
                    "vision_model": "qwen3-vl:8b",
                },
                headers=headers,
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(fake_ocr.provider, "lm_studio")
        self.assertEqual(fake_ocr.model, "glm-ocr:0.9b")
        self.assertEqual(fake_vision.provider, "lm_studio")
        self.assertEqual(fake_vision.model, "qwen3-vl:8b")

    def test_native_pick_folder_returns_relative_path_for_exclusion(self):
        import unittest.mock as mock
        from app import app

        app.config["TESTING"] = True
        client = app.test_client()
        headers = {"X-Requested-With": "ChatEKLD"}

        with tempfile.TemporaryDirectory() as vault_dir:
            child = Path(vault_dir, "folder")
            child.mkdir()
            fake_window = mock.MagicMock()
            fake_window.create_file_dialog.return_value = str(child)
            fake_webview = mock.MagicMock()
            fake_webview.windows = [fake_window]
            fake_webview.FOLDER_DIALOG = 20

            with patch.dict("sys.modules", {"webview": fake_webview}):
                resp = client.post(
                    "/api/native-pick-folder",
                    json={"constrain_to_vault": True, "base_path": vault_dir},
                    headers=headers,
                )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["relative_path"], "folder")

    def test_chat_emits_info_event_when_indexing_in_progress(self):
        """When indexing is in flight the chat SSE must lead with an info
        event so the user knows results may be incomplete."""
        from app import app
        from rag.vault import obsidian_manager

        app.config["TESTING"] = True
        client = app.test_client()
        headers = {"X-Requested-With": "ChatEKLD"}

        class FakeResponse:
            @property
            def response_gen(self):
                yield "hello"

        with (
            patch.object(obsidian_manager, "get_status", return_value="running"),
            patch.object(obsidian_manager, "stream_chat", return_value=FakeResponse()),
        ):
            resp = client.post(
                "/api/obsidian/chat",
                json={"message": "hi"},
                headers=headers,
                buffered=True,
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('"info":', body)
        self.assertIn("Indexing is still in progress", body)
        self.assertIn('"token": "hello"', body)
        self.assertIn("[DONE]", body)

    def test_chat_does_not_emit_indexing_in_progress_info_when_idle(self):
        """The 'Indexing is still in progress' banner only fires when an
        indexing run is actually in flight.  A general stage event such as
        'Waiting for model response…' is *expected* on every chat and must
        not regress this assertion."""
        from app import app
        from rag.vault import obsidian_manager

        app.config["TESTING"] = True
        client = app.test_client()
        headers = {"X-Requested-With": "ChatEKLD"}

        class FakeResponse:
            @property
            def response_gen(self):
                yield "hi"

        with (
            patch.object(obsidian_manager, "get_status", return_value="done"),
            patch.object(obsidian_manager, "stream_chat", return_value=FakeResponse()),
        ):
            resp = client.post(
                "/api/obsidian/chat",
                json={"message": "x"},
                headers=headers,
                buffered=True,
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn("Indexing is still in progress", body)


class TestProviderRegressions(unittest.TestCase):
    def test_ocr_manager_routes_image_calls_to_selected_provider(self):
        from services.vision import GLMOCRManager

        manager = GLMOCRManager(model="glm-ocr", provider="lm_studio")
        manager._is_available = True

        with (
            patch("services.vision._chat_lm_studio_image", return_value="text") as lm_call,
            patch("services.vision._chat_ollama_image") as ollama_call,
        ):
            result = manager.extract_page_text("base64")

        self.assertEqual(result, "text")
        lm_call.assert_called_once()
        ollama_call.assert_not_called()

    def test_vision_manager_routes_image_calls_to_selected_provider(self):
        from services.vision import VisionManager

        manager = VisionManager(model="qwen3-vl", provider="lm_studio")
        manager._is_available = True
        manager._availability_checked_at = 10**12

        with (
            patch("services.vision.time.monotonic", return_value=10**12),
            patch("services.vision._chat_lm_studio_image", return_value="description") as lm_call,
            patch("services.vision._chat_ollama_image") as ollama_call,
        ):
            result = manager.describe_image("base64")

        self.assertEqual(result, "description")
        lm_call.assert_called_once()
        ollama_call.assert_not_called()

    def test_vision_failure_cooldown_is_config_driven(self):
        # Improvement plan 1.5: the failed-call fast-fail window comes from
        # vision_failure_cooldown_s (default 30; 0 disables so the next image
        # retries immediately instead of being silently skipped).
        from services.vision import VisionManager, _failure_cooldown_s

        # Reader semantics: default when missing/garbage/negative; 0 = disabled;
        # clamped above 600.
        with patch("core.config.load_config_readonly", return_value={}):
            self.assertEqual(_failure_cooldown_s(), 30.0)
        with patch("core.config.load_config_readonly",
                   return_value={"vision_failure_cooldown_s": 0}):
            self.assertEqual(_failure_cooldown_s(), 0.0)
        with patch("core.config.load_config_readonly",
                   return_value={"vision_failure_cooldown_s": -5}):
            self.assertEqual(_failure_cooldown_s(), 30.0)
        with patch("core.config.load_config_readonly",
                   return_value={"vision_failure_cooldown_s": 10_000}):
            self.assertEqual(_failure_cooldown_s(), 600.0)

        # Behaviour: with the cooldown DISABLED a failure does not fast-fail
        # the next call; with the default it does.
        manager = VisionManager(model="qwen3-vl", provider="ollama")
        calls = {"n": 0}

        def _flaky(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("model not loaded")
            return "described"

        with patch("services.vision._chat_ollama_image", side_effect=_flaky), \
             patch("core.config.load_config_readonly",
                   return_value={"vision_failure_cooldown_s": 0}):
            self.assertEqual(manager.describe_image("base64"), "")          # fails
            self.assertEqual(manager.describe_image("base64"), "described")  # retries NOW
        self.assertEqual(calls["n"], 2)

        manager2 = VisionManager(model="qwen3-vl", provider="ollama")
        with patch("services.vision._chat_ollama_image",
                   side_effect=RuntimeError("down")) as transport, \
             patch("core.config.load_config_readonly", return_value={}):
            self.assertEqual(manager2.describe_image("base64"), "")
            self.assertEqual(manager2.describe_image("base64"), "")  # fast-fail
        transport.assert_called_once()   # second call never reached the model

    def test_summarise_forwards_restored_controls(self):
        from app import app

        app.config["TESTING"] = True
        client = app.test_client()
        headers = {"X-Requested-With": "ChatEKLD"}
        captured = {}

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, *_args):
                return self

            def fetchone(self):
                return ("paper text", "paper.pdf")

        def fake_summarise_stream(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            yield "ok"

        with (
            patch("api.routes.paper.get_db_connection", return_value=FakeConnection()),
            patch("api.routes.paper.summarise_stream", side_effect=fake_summarise_stream),
        ):
            resp = client.post(
                "/api/summarise",
                json={
                    "upload_id": "u1",
                    "model": "llama3.2",
                    "preset": "detailed",
                    "report_type_id": "clinical_trial",
                    "audience": "General public",
                    "focus_question": "What was the primary endpoint?",
                    "temperature": 0.7,
                    "top_p": 0.8,
                    "repeat_penalty": 1.2,
                    "max_tokens": 512,
                    "num_ctx": 8192,
                },
                headers=headers,
                buffered=True,
            )
            body = resp.get_data()

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"ok", body)
        self.assertIn("randomized controlled trials", captured["kwargs"]["system_prompt"])
        self.assertEqual(captured["kwargs"]["audience_modifier"], " Use simple, non-technical language.")
        self.assertEqual(captured["kwargs"]["temperature"], 0.7)
        self.assertEqual(captured["kwargs"]["top_p"], 0.8)
        self.assertEqual(captured["kwargs"]["repeat_penalty"], 1.2)
        self.assertEqual(captured["kwargs"]["max_tokens"], 512)
        self.assertEqual(captured["kwargs"]["num_ctx"], 8192)

    def test_export_summary_supports_markdown(self):
        from app import app

        app.config["TESTING"] = True
        client = app.test_client()
        headers = {"X-Requested-With": "ChatEKLD"}

        with tempfile.TemporaryDirectory() as downloads:
            with patch("api.routes.paper.os.path.expanduser", return_value=downloads):
                resp = client.post(
                    "/api/export-summary",
                    json={"filename": "paper.pdf", "content": "Summary body", "format": "md"},
                    headers=headers,
                )

            self.assertEqual(resp.status_code, 200)
            out_path = Path(resp.get_json()["path"])
            self.assertEqual(out_path.suffix, ".md")
            self.assertTrue(out_path.exists())
            self.assertTrue(out_path.read_text(encoding="utf-8").startswith("# Summary - paper"))

    def test_ollama_stream_chat_sends_generation_parameters_as_options(self):
        from core.providers.ollama import OllamaProvider

        provider = OllamaProvider()
        # stream_chat now routes through an ollama.Client (so the configured
        # local_request_timeout_s can bound the call); assert on that client's
        # .chat rather than the module-level function.
        with (
            patch.object(provider, "resolve_model", return_value="llama3.2:3b"),
            patch("core.providers.ollama.ollama.Client") as ClientCls,
        ):
            ClientCls.return_value.chat.return_value = iter([])
            provider.stream_chat(
                model="llama3.2",
                prompt="Summarize",
                system_prompt="System",
                temperature=0.2,
                top_p=0.8,
                num_ctx=4096,
                num_predict=512,
            )

        kwargs = ClientCls.return_value.chat.call_args.kwargs
        self.assertNotIn("temperature", kwargs)
        self.assertEqual(kwargs["options"]["temperature"], 0.2)
        self.assertEqual(kwargs["options"]["top_p"], 0.8)
        self.assertEqual(kwargs["options"]["num_ctx"], 4096)
        self.assertEqual(kwargs["options"]["num_predict"], 512)

    def test_ollama_client_threads_timeout_and_is_cached(self):
        """local_request_timeout_s>0 must reach ollama.Client (so a hung local
        call is bounded), 0 must omit the kwarg (leave the SDK default), and the
        client must be reused per (host, timeout) rather than rebuilt per call."""
        import core.providers.ollama as ollama_mod
        from core.providers.ollama import OllamaProvider
        from core.config import save_config

        provider = OllamaProvider()
        try:
            # --- positive value reaches the client, and the client is cached ---
            with ollama_mod._client_cache_lock:
                ollama_mod._client_cache.clear()
            save_config({"local_request_timeout_s": 45})
            with (
                patch.object(provider, "resolve_model", return_value="llama3.2:3b"),
                patch("core.providers.ollama.ollama.Client") as ClientCls,
            ):
                ClientCls.return_value.chat.return_value = iter([])
                provider.stream_chat(model="llama3.2", prompt="a")
                provider.stream_chat(model="llama3.2", prompt="b")
                self.assertEqual(ClientCls.call_args.kwargs.get("timeout"), 45.0)
                # Two stream calls, one Client construction → cache hit on #2.
                self.assertEqual(ClientCls.call_count, 1)

            # --- 0 disables: no timeout kwarg, leave the SDK default ---
            with ollama_mod._client_cache_lock:
                ollama_mod._client_cache.clear()
            save_config({"local_request_timeout_s": 0})
            with (
                patch.object(provider, "resolve_model", return_value="llama3.2:3b"),
                patch("core.providers.ollama.ollama.Client") as ClientCls2,
            ):
                ClientCls2.return_value.chat.return_value = iter([])
                provider.stream_chat(model="llama3.2", prompt="c")
                self.assertNotIn("timeout", ClientCls2.call_args.kwargs)
        finally:
            # Restore default + clear the module cache so other tests are clean.
            save_config({"local_request_timeout_s": 0})
            with ollama_mod._client_cache_lock:
                ollama_mod._client_cache.clear()

    def test_lm_studio_summary_truncates_large_inputs(self):
        import rag.summarizer as summarizer

        captured = {}

        class FakeProvider:
            def stream_chat(self, **kwargs):
                captured.update(kwargs)
                class Chunk:
                    choices = [type("Choice", (), {"delta": type("Delta", (), {"content": "ok"})()})()]
                return iter([Chunk()])

        with patch("rag.summarizer.get_provider", return_value=FakeProvider()):
            tokens = list(summarizer.summarise_stream(
                "word " * 12000,
                "mistralai/ministral-3-3b",
                provider_name="lm_studio",
            ))

        self.assertEqual(tokens, ["ok"])
        self.assertLess(len(captured["prompt"]), len("word " * 12000))


class TestGLMOCRRegressions(unittest.TestCase):
    def test_glm_ocr_retries_with_smaller_image_on_context_overflow(self):
        from PIL import Image
        from services.vision import GLMOCRManager

        image = Image.new("RGB", (140, 140), "white")
        output = io.BytesIO()
        image.save(output, format="PNG")
        base64_png = base64.b64encode(output.getvalue()).decode("utf-8")

        manager = GLMOCRManager()
        manager._is_available = True
        # GLMOCRManager.check_availability uses a TTL gate — must seed the
        # check timestamp too, otherwise the cached _is_available is ignored
        # and a real provider lookup is attempted (and fails under the test).
        manager._availability_checked_at = time.monotonic()

        with patch(
            "services.vision._chat_ollama_image",
            side_effect=[
                Exception("request (4129 tokens) exceeds the available context size (4096 tokens)"),
                "ocr text",
            ],
        ) as chat:
            result = manager.extract_page_text(base64_png)

        self.assertEqual(result, "ocr text")
        self.assertEqual(chat.call_count, 2)


class TestVaultChatLiveControls(unittest.TestCase):
    """Phase 1 live retrieval/generation knobs for /api/obsidian/chat.

    These are pure query-time parameters — none of them touch indexing or
    the on-disk vector store, so they can be validated against a stub index.
    """

    def _make_engine(self, **overrides):
        from rag.engine import SimpleQueryEngine

        defaults = dict(
            index=object(),  # opaque; the engine only forwards it to the retriever
            llm_name="dummy-llm",
            embed_name="dummy-embed",
            top_k=6,
            provider_name="ollama",
        )
        defaults.update(overrides)
        with patch("rag.engine.get_provider") as get_provider:
            get_provider.return_value = MagicMock()
            return SimpleQueryEngine(**defaults)

    def test_prompt_mode_selects_correct_template(self):
        from rag.engine import (
            RAG_QA_PROMPT_STRICT,
            RAG_QA_PROMPT_BALANCED,
            RAG_QA_PROMPT_EXPLORATORY,
            _PROMPT_MODES,
        )
        self.assertIs(_PROMPT_MODES["strict"], RAG_QA_PROMPT_STRICT)
        self.assertIs(_PROMPT_MODES["balanced"], RAG_QA_PROMPT_BALANCED)
        self.assertIs(_PROMPT_MODES["exploratory"], RAG_QA_PROMPT_EXPLORATORY)
        # Hallmarks distinguishing the three flavours.
        self.assertIn("say you do not know", RAG_QA_PROMPT_STRICT.template)
        self.assertIn("mark that part clearly", RAG_QA_PROMPT_BALANCED.template)
        self.assertIn("Synthesise", RAG_QA_PROMPT_EXPLORATORY.template)

    def test_unknown_prompt_mode_falls_back_to_strict(self):
        engine = self._make_engine(prompt_mode="bogus")
        self.assertEqual(engine.prompt_mode, "strict")

    def test_explicit_top_k_bypasses_autoscaling(self):
        # _effective_top_k would downscale 12 to 4 at ctx=8192; the explicit
        # flag must override that so the user's choice survives.
        engine = self._make_engine(top_k=12, top_k_explicit=True)
        with patch("rag.engine.load_config") as lc, \
             patch("rag.engine.VectorIndexRetriever") as retriever_cls, \
             patch("rag.engine.RetrieverQueryEngine") as qengine_cls:
            lc.return_value = {"context_window": 8192}
            qengine_cls.from_args.return_value.query.return_value = "ok"
            engine.query("hello")
            kwargs = retriever_cls.call_args.kwargs
            self.assertEqual(kwargs["similarity_top_k"], 12)

    def test_implicit_top_k_still_autoscales(self):
        engine = self._make_engine(top_k=12)  # top_k_explicit=False
        with patch("rag.engine.load_config") as lc, \
             patch("rag.engine.VectorIndexRetriever") as retriever_cls, \
             patch("rag.engine.RetrieverQueryEngine") as qengine_cls:
            lc.return_value = {"context_window": 8192}
            qengine_cls.from_args.return_value.query.return_value = "ok"
            engine.query("hello")
            kwargs = retriever_cls.call_args.kwargs
            self.assertEqual(kwargs["similarity_top_k"], 4)  # capped by autoscaler

    def test_similarity_cutoff_reaches_postprocessor(self):
        from llama_index.core.postprocessor import SimilarityPostprocessor
        engine = self._make_engine(similarity_cutoff=0.55)
        with patch("rag.engine.load_config") as lc, \
             patch("rag.engine.VectorIndexRetriever"), \
             patch("rag.engine.RetrieverQueryEngine") as qengine_cls:
            lc.return_value = {"context_window": 32768}
            qengine_cls.from_args.return_value.query.return_value = "ok"
            engine.query("hello")
            postprocessors = qengine_cls.from_args.call_args.kwargs["node_postprocessors"]
            self.assertEqual(len(postprocessors), 1)
            self.assertIsInstance(postprocessors[0], SimilarityPostprocessor)
            self.assertAlmostEqual(postprocessors[0].similarity_cutoff, 0.55)

    def test_temperature_forwarded_to_get_llm(self):
        engine = self._make_engine(temperature=0.7)
        with patch("rag.engine.load_config") as lc, \
             patch("rag.engine.VectorIndexRetriever"), \
             patch("rag.engine.RetrieverQueryEngine") as qengine_cls:
            lc.return_value = {"context_window": 32768}
            qengine_cls.from_args.return_value.query.return_value = "ok"
            engine.query("hello")
            llm_kwargs = engine._provider.get_llm.call_args.kwargs
            self.assertEqual(llm_kwargs["temperature"], 0.7)

    def test_no_temperature_means_no_kwarg(self):
        engine = self._make_engine(temperature=None)
        with patch("rag.engine.load_config") as lc, \
             patch("rag.engine.VectorIndexRetriever"), \
             patch("rag.engine.RetrieverQueryEngine") as qengine_cls:
            lc.return_value = {"context_window": 32768}
            qengine_cls.from_args.return_value.query.return_value = "ok"
            engine.query("hello")
            llm_kwargs = engine._provider.get_llm.call_args.kwargs
            self.assertNotIn("temperature", llm_kwargs)

    def test_bm25_package_import_is_available(self):
        # requirements.txt should install the BM25 integration by default.
        # The runtime still import-guards it for graceful degradation, but
        # a normal project environment must expose this class.
        from llama_index.retrievers.bm25 import BM25Retriever

        self.assertIsNotNone(BM25Retriever)

    def test_real_bm25_retriever_finds_exact_terms(self):
        from llama_index.core.schema import TextNode
        from llama_index.retrievers.bm25 import BM25Retriever

        retriever = BM25Retriever.from_defaults(
            nodes=[
                TextNode(text="alpha beta common", id_="alpha-node"),
                TextNode(text="zeta needle common", id_="needle-node"),
            ],
            similarity_top_k=1,
        )

        results = retriever.retrieve("needle")

        self.assertEqual(results[0].node.node_id, "needle-node")

    def test_bm25_retriever_enables_rrf_fusion(self):
        bm25 = MagicMock()
        engine = self._make_engine(top_k=5, bm25_retriever=bm25)
        with patch("rag.engine.load_config") as lc, \
             patch("rag.engine.VectorIndexRetriever") as dense_cls, \
             patch("rag.engine.QueryFusionRetriever") as fusion_cls, \
             patch("rag.engine.RetrieverQueryEngine") as qengine_cls:
            lc.return_value = {"context_window": 32768}
            dense_cls.return_value = MagicMock(name="dense")
            fusion_cls.return_value = MagicMock(name="fusion")
            qengine_cls.from_args.return_value.query.return_value = "ok"

            engine.query("hello")

            self.assertEqual(bm25.similarity_top_k, 5)
            fusion_kwargs = fusion_cls.call_args.kwargs
            self.assertEqual(fusion_kwargs["retrievers"], [dense_cls.return_value, bm25])
            self.assertEqual(fusion_kwargs["similarity_top_k"], 5)
            self.assertEqual(fusion_kwargs["num_queries"], 1)
            self.assertEqual(fusion_kwargs["mode"], "reciprocal_rerank")
            qengine_kwargs = qengine_cls.from_args.call_args.kwargs
            self.assertIs(qengine_kwargs["retriever"], fusion_cls.return_value)

    def test_query_expansion_sets_num_queries_for_local_provider(self):
        # Stage 6: opt-in multi-query expansion drives the fusion retriever's
        # num_queries (local provider ⇒ a real LLM is available to rewrite).
        bm25 = MagicMock()
        engine = self._make_engine(
            top_k=5, bm25_retriever=bm25, query_expansion=True, num_queries=3,
        )
        with patch("rag.engine.load_config") as lc, \
             patch("rag.engine.VectorIndexRetriever") as dense_cls, \
             patch("rag.engine.QueryFusionRetriever") as fusion_cls, \
             patch("rag.engine.RetrieverQueryEngine") as qengine_cls:
            lc.return_value = {"context_window": 32768}
            dense_cls.return_value = MagicMock(name="dense")
            fusion_cls.return_value = MagicMock(name="fusion")
            qengine_cls.from_args.return_value.query.return_value = "ok"
            engine.query("hello")
            self.assertEqual(fusion_cls.call_args.kwargs["num_queries"], 3)

    def test_mmr_enabled_sets_mmr_on_dense_retriever(self):
        # Stage 6: MMR is a query-time dense-retriever mode (no reindex),
        # gated by mmr_enabled with mmr_lambda as the threshold.
        engine = self._make_engine(top_k=4, mmr_enabled=True, mmr_lambda=0.5)
        with patch("rag.engine.load_config") as lc, \
             patch("rag.engine.VectorIndexRetriever") as dense_cls, \
             patch("rag.engine.RetrieverQueryEngine") as qengine_cls:
            lc.return_value = {"context_window": 32768}
            qengine_cls.from_args.return_value.query.return_value = "ok"
            engine.query("hello")
            dense_kwargs = dense_cls.call_args.kwargs
            self.assertEqual(dense_kwargs.get("vector_store_query_mode"), "mmr")
            self.assertEqual(dense_kwargs.get("vector_store_kwargs"), {"mmr_threshold": 0.5})

    def test_mmr_disabled_leaves_dense_retriever_in_default_mode(self):
        # A lambda with mmr_enabled=False must NOT switch on MMR.
        engine = self._make_engine(top_k=4, mmr_enabled=False, mmr_lambda=0.5)
        with patch("rag.engine.load_config") as lc, \
             patch("rag.engine.VectorIndexRetriever") as dense_cls, \
             patch("rag.engine.RetrieverQueryEngine") as qengine_cls:
            lc.return_value = {"context_window": 32768}
            qengine_cls.from_args.return_value.query.return_value = "ok"
            engine.query("hello")
            dense_kwargs = dense_cls.call_args.kwargs
            self.assertNotIn("vector_store_query_mode", dense_kwargs)

    def test_rerank_pool_ceiling_override_caps_candidate_pool(self):
        # A live per-request ceiling override bounds the candidate pool fed to
        # the reranker: pool = min(max(top_k*4, 20), ceiling).
        reranker = MagicMock()
        engine = self._make_engine(
            top_k=10, top_k_explicit=True, reranker=reranker, rerank_pool_ceiling=30,
        )
        with patch("rag.engine.load_config") as lc, \
             patch("rag.engine.VectorIndexRetriever") as dense_cls, \
             patch("rag.engine.RetrieverQueryEngine") as qengine_cls:
            lc.return_value = {"context_window": 32768}
            qengine_cls.from_args.return_value.query.return_value = "ok"
            engine.query("hello")
            # min(max(10*4, 20), 30) = 30 (the override caps it below the
            # would-be 40, and below the config default ceiling of 50).
            self.assertEqual(dense_cls.call_args.kwargs["similarity_top_k"], 30)

    def test_reranker_widens_pool_and_removes_similarity_cutoff(self):
        reranker = MagicMock()
        engine = self._make_engine(top_k=6, reranker=reranker)
        with patch("rag.engine.load_config") as lc, \
             patch("rag.engine.VectorIndexRetriever") as retriever_cls, \
             patch("rag.engine.RetrieverQueryEngine") as qengine_cls:
            lc.return_value = {"context_window": 32768}
            qengine_cls.from_args.return_value.query.return_value = "ok"

            engine.query("hello")

            self.assertEqual(retriever_cls.call_args.kwargs["similarity_top_k"], 24)
            self.assertEqual(reranker.top_n, 6)
            postprocessors = qengine_cls.from_args.call_args.kwargs["node_postprocessors"]
            self.assertEqual(postprocessors, [reranker])

    def test_route_clamps_out_of_range_values(self):
        from api.routes.vault import _resolve_chat_params
        cfg = {}
        # Each input is deliberately out of range or malformed.
        params = _resolve_chat_params(
            {
                "top_k": 999,
                "similarity_cutoff": -0.5,
                "prompt_mode": "bogus",
                "temperature": 47.0,
            },
            cfg,
        )
        self.assertEqual(params["top_k"], 32)  # clamped to _TOP_K_MAX
        self.assertTrue(params["top_k_explicit"])
        self.assertEqual(params["similarity_cutoff"], 0.0)
        self.assertNotIn("prompt_mode", params)  # invalid mode is dropped
        self.assertEqual(params["temperature"], 2.0)

    def test_route_falls_back_to_config_then_defaults(self):
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {},  # empty request body
            {
                "vault_top_k": 8,
                "vault_similarity_cutoff": 0.4,
                "vault_prompt_mode": "balanced",
                "vault_chat_temperature": 0.1,
            },
        )
        self.assertEqual(params["top_k"], 8)
        self.assertTrue(params["top_k_explicit"])
        self.assertEqual(params["similarity_cutoff"], 0.4)
        self.assertEqual(params["prompt_mode"], "balanced")
        self.assertEqual(params["temperature"], 0.1)

    def test_route_request_body_wins_over_config(self):
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {"top_k": 3, "prompt_mode": "exploratory"},
            {"vault_top_k": 10, "vault_prompt_mode": "strict"},
        )
        self.assertEqual(params["top_k"], 3)
        self.assertEqual(params["prompt_mode"], "exploratory")

    def test_wikilink_expansion_resolution(self):
        from api.routes.vault import _resolve_chat_params
        # Body wins over config.
        self.assertTrue(
            _resolve_chat_params(
                {"wikilink_expansion": True}, {"vault_wikilink_expansion": False}
            )["wikilink_expansion"]
        )
        # Config fallback when the body omits it.
        self.assertTrue(
            _resolve_chat_params({}, {"vault_wikilink_expansion": True})[
                "wikilink_expansion"
            ]
        )
        # Omitted entirely when neither source sets it, so stream_chat's
        # default (off) applies and behaviour is unchanged.
        self.assertNotIn("wikilink_expansion", _resolve_chat_params({}, {}))

    def test_missing_everything_returns_empty(self):
        # No request fields and no config keys — caller-side defaults apply.
        # The dict must not carry stale keys that would override stream_chat
        # defaults with None.
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params({}, {})
        self.assertEqual(params, {})

    def test_invalid_body_falls_back_to_valid_config(self):
        # Copilot review (PR #71): an invalid body value used to silently
        # drop to the engine default, ignoring a valid persisted config.
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {
                "top_k": "abc",                # non-numeric
                "similarity_cutoff": "xyz",    # non-numeric
                "prompt_mode": "bogus",        # not in enum
                "temperature": "nope",         # non-numeric
            },
            {
                "vault_top_k": 10,
                "vault_similarity_cutoff": 0.4,
                "vault_prompt_mode": "balanced",
                "vault_chat_temperature": 0.7,
            },
        )
        # Each knob falls through to the config value rather than being
        # dropped entirely.
        self.assertEqual(params["top_k"], 10)
        self.assertTrue(params["top_k_explicit"])
        self.assertEqual(params["similarity_cutoff"], 0.4)
        self.assertEqual(params["prompt_mode"], "balanced")
        self.assertEqual(params["temperature"], 0.7)

    def test_invalid_body_and_invalid_config_both_dropped(self):
        # When neither source yields a valid value, the key is omitted so
        # stream_chat's own kwarg default applies.
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {"prompt_mode": "bogus"},
            {"vault_prompt_mode": "also_bogus"},
        )
        self.assertNotIn("prompt_mode", params)

    def test_nan_and_inf_rejected(self):
        # Copilot review (PR #71): _clamp() returned NaN unchanged because
        # NaN comparisons are False.  Python's json module accepts NaN /
        # Infinity literals, so a hand-edited config or non-standard body
        # could otherwise propagate non-finite values into the engine.
        from api.routes.vault import _resolve_chat_params
        nan = float("nan")
        inf = float("inf")
        params = _resolve_chat_params(
            {
                "top_k": nan,
                "similarity_cutoff": inf,
                "temperature": -inf,
            },
            {},
        )
        # All three are rejected because they're non-finite; no valid
        # fallback either, so the keys are omitted.
        self.assertNotIn("top_k", params)
        self.assertNotIn("similarity_cutoff", params)
        self.assertNotIn("temperature", params)

    def test_nan_in_body_falls_back_to_finite_config(self):
        # A non-finite body value should not shadow a valid config value.
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {"similarity_cutoff": float("nan")},
            {"vault_similarity_cutoff": 0.5},
        )
        self.assertEqual(params["similarity_cutoff"], 0.5)


class TestVaultChatHybridAndRerankerResolution(unittest.TestCase):
    """Coverage for the bool + model-name resolution added with hybrid +
    reranker.  Mirrors the precedence semantics of the existing live
    controls (body wins when valid, falls through to config, then to the
    engine default by omitting the key entirely)."""

    def test_coerce_bool_accepts_canonical_string_forms(self):
        from api.validators import coerce_bool as _coerce_bool
        for v in ("true", "True", "TRUE", " true ", "1", "yes", "on"):
            self.assertIs(_coerce_bool(v), True, msg=f"truthy: {v!r}")
        for v in ("false", "False", "FALSE", " false ", "0", "no", "off"):
            self.assertIs(_coerce_bool(v), False, msg=f"falsy: {v!r}")

    def test_coerce_bool_passes_json_booleans_through(self):
        from api.validators import coerce_bool as _coerce_bool
        self.assertIs(_coerce_bool(True), True)
        self.assertIs(_coerce_bool(False), False)

    def test_coerce_bool_accepts_exact_0_and_1(self):
        from api.validators import coerce_bool as _coerce_bool
        self.assertIs(_coerce_bool(0), False)
        self.assertIs(_coerce_bool(1), True)
        # Float forms of the same values are allowed.
        self.assertIs(_coerce_bool(0.0), False)
        self.assertIs(_coerce_bool(1.0), True)

    def test_coerce_bool_rejects_ambiguous_values(self):
        from api.validators import coerce_bool as _coerce_bool
        # Anything other than the canonical forms returns None so the
        # resolver falls through to the next source rather than silently
        # coercing 'maybe' to False or 2 to True.
        for v in (2, -1, "maybe", "yepperino", "", None, [], {}, 0.5, float("nan")):
            self.assertIsNone(_coerce_bool(v), msg=f"reject: {v!r}")

    def test_hybrid_and_reranker_body_booleans_pass_through(self):
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {"hybrid_enabled": True, "reranker_enabled": False},
            {},
        )
        self.assertIs(params["hybrid_enabled"], True)
        self.assertIs(params["reranker_enabled"], False)

    def test_hybrid_and_reranker_body_string_forms_pass_through(self):
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {"hybrid_enabled": "false", "reranker_enabled": "true"},
            {},
        )
        self.assertIs(params["hybrid_enabled"], False)
        self.assertIs(params["reranker_enabled"], True)

    def test_invalid_body_bool_falls_back_to_config(self):
        # 'maybe' is not a recognised bool form, so the body source is
        # skipped and the config value wins.
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {"hybrid_enabled": "maybe", "reranker_enabled": 2},
            {"vault_hybrid_enabled": False, "vault_reranker_enabled": True},
        )
        self.assertIs(params["hybrid_enabled"], False)
        self.assertIs(params["reranker_enabled"], True)

    def test_missing_body_uses_config(self):
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {},
            {"vault_hybrid_enabled": True, "vault_reranker_enabled": False},
        )
        self.assertIs(params["hybrid_enabled"], True)
        self.assertIs(params["reranker_enabled"], False)

    def test_neither_body_nor_config_omits_keys(self):
        # When both sources are missing / invalid, the keys are omitted so
        # stream_chat's own kwarg defaults (both False) take effect rather
        # than a stale None overriding them.
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params({}, {})
        self.assertNotIn("hybrid_enabled", params)
        self.assertNotIn("reranker_enabled", params)
        self.assertNotIn("reranker_model", params)

    def test_invalid_body_and_invalid_config_drops_bool_keys(self):
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {"hybrid_enabled": "perhaps"},
            {"vault_hybrid_enabled": "also-bogus"},
        )
        self.assertNotIn("hybrid_enabled", params)

    def test_reranker_model_resolved_from_config_only(self):
        # The model name is config-only: a body override of
        # `reranker_model` is ignored so a malicious page cannot swap in
        # an arbitrary HuggingFace repo to download.
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {"reranker_model": "evil/model"},
            {"vault_reranker_model": "BAAI/bge-reranker-base"},
        )
        self.assertEqual(params["reranker_model"], "BAAI/bge-reranker-base")

    def test_reranker_model_whitespace_stripped(self):
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {},
            {"vault_reranker_model": "  cross-encoder/ms-marco-MiniLM-L-6-v2  "},
        )
        self.assertEqual(
            params["reranker_model"],
            "cross-encoder/ms-marco-MiniLM-L-6-v2",
        )

    def test_reranker_model_missing_or_blank_config_omits_key(self):
        # An empty / whitespace-only / non-string config value falls
        # through to the engine default rather than being passed as "".
        from api.routes.vault import _resolve_chat_params
        for cfg_value in ("", "   ", None, 42, []):
            with self.subTest(cfg_value=cfg_value):
                cfg = {"vault_reranker_model": cfg_value} if cfg_value is not None else {}
                params = _resolve_chat_params({}, cfg)
                self.assertNotIn("reranker_model", params)

    def test_custom_system_prompt_body_passes_through(self):
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {"system_prompt": "Answer in JSON."},
            {},
        )
        self.assertEqual(params["custom_system_prompt"], "Answer in JSON.")

    def test_custom_system_prompt_falls_back_to_config(self):
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {},
            {"vault_chat_system_prompt": "Always cite line numbers."},
        )
        self.assertEqual(
            params["custom_system_prompt"], "Always cite line numbers.",
        )

    def test_custom_system_prompt_truncated_at_limit(self):
        from api.routes.vault import _resolve_chat_params
        from core.constants import SYSTEM_PROMPT_LIMIT
        overlong = "x" * (SYSTEM_PROMPT_LIMIT + 200)
        params = _resolve_chat_params({"system_prompt": overlong}, {})
        self.assertEqual(len(params["custom_system_prompt"]), SYSTEM_PROMPT_LIMIT)

    def test_custom_system_prompt_empty_body_preserves_empty(self):
        # An explicit empty string from the UI overrides config so a
        # cleared textarea resets to "no custom prompt" rather than
        # silently inheriting an old value.
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {"system_prompt": ""},
            {"vault_chat_system_prompt": "stale value"},
        )
        self.assertEqual(params["custom_system_prompt"], "")

    def test_custom_system_prompt_non_string_falls_back_to_config(self):
        from api.routes.vault import _resolve_chat_params
        params = _resolve_chat_params(
            {"system_prompt": 12345},
            {"vault_chat_system_prompt": "from config"},
        )
        self.assertEqual(params["custom_system_prompt"], "from config")


class TestVaultChatBM25Manager(unittest.TestCase):
    def setUp(self):
        # Isolate the on-disk BM25 sidecar per test: OBSIDIAN_INDEX_DIR is a
        # session-global path, and the sidecar persist/load would otherwise
        # leak state between tests in this class.
        self._index_dir = tempfile.TemporaryDirectory()
        patcher = patch("rag.vault.OBSIDIAN_INDEX_DIR", self._index_dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._index_dir.cleanup)

    def _manager_with_docs(self, count=3):
        from rag.vault import ObsidianVaultManager

        manager = ObsidianVaultManager()
        docs = {f"doc-{i}": object() for i in range(count)}
        manager._index = MagicMock()
        manager._index.docstore.docs = docs
        return manager, docs

    def test_get_bm25_retriever_builds_and_reuses_cache(self):
        manager, _docs = self._manager_with_docs()

        class FakeBM25Retriever:
            calls = []

            @classmethod
            def from_defaults(cls, *, nodes, similarity_top_k):
                retriever = MagicMock()
                retriever.nodes = list(nodes)
                retriever.similarity_top_k = similarity_top_k
                cls.calls.append((retriever.nodes, similarity_top_k, retriever))
                return retriever

        with patch("rag.engine.BM25Retriever", FakeBM25Retriever):
            first = manager._get_bm25_retriever(top_k=4)
            second = manager._get_bm25_retriever(top_k=9)

        self.assertIs(first, second)
        self.assertEqual(len(FakeBM25Retriever.calls), 1)
        self.assertEqual(len(FakeBM25Retriever.calls[0][0]), 3)
        self.assertEqual(FakeBM25Retriever.calls[0][1], 4)
        # Item 2.7: the cached fetch is READ-ONLY — it must NOT retune the
        # shared object (that write raced a concurrent in-flight retrieval).
        # The engine's _build_retrieval_pipeline, inside the mutation-lock
        # hold, is the one tuner (TestSharedRetunerRace pins that half).
        self.assertEqual(second.similarity_top_k, 4)
        self.assertEqual(manager._bm25_cached_doc_count, 3)

    def test_get_bm25_retriever_rebuilds_after_invalidation(self):
        manager, _docs = self._manager_with_docs()

        class FakeBM25Retriever:
            build_count = 0

            @classmethod
            def from_defaults(cls, *, nodes, similarity_top_k):
                cls.build_count += 1
                retriever = MagicMock()
                retriever.similarity_top_k = similarity_top_k
                return retriever

        with patch("rag.engine.BM25Retriever", FakeBM25Retriever):
            first = manager._get_bm25_retriever(top_k=2)
            manager._invalidate_retrieval_caches()
            second = manager._get_bm25_retriever(top_k=2)

        self.assertIsNot(first, second)
        self.assertEqual(FakeBM25Retriever.build_count, 2)

    def test_get_bm25_retriever_returns_none_when_package_missing(self):
        manager, _docs = self._manager_with_docs()

        with patch("rag.engine.BM25Retriever", None):
            self.assertIsNone(manager._get_bm25_retriever(top_k=4))

    def _manager_with_nodes(self, node_map):
        from rag.vault import ObsidianVaultManager

        manager = ObsidianVaultManager()
        manager._index = MagicMock()
        manager._index.docstore.docs = node_map
        return manager

    def test_bm25_sidecar_round_trip_mmap_reload(self):
        """build → persist → mmap-load round trip: a fresh manager (process-
        cold equivalent) must serve identical retrieval from the sidecar
        without ever rebuilding from the docstore."""
        from llama_index.core.schema import TextNode
        from llama_index.retrievers.bm25 import BM25Retriever as RealBM25

        nodes = {
            "alpha-node": TextNode(
                text="alpha beta common", id_="alpha-node",
                metadata={"source": "alpha.md"},
            ),
            "needle-node": TextNode(
                text="zeta needle common", id_="needle-node",
                metadata={"source": "needle.md"},
            ),
            "gamma-node": TextNode(
                text="gamma delta common", id_="gamma-node",
                metadata={"source": "gamma.md"},
            ),
        }

        built = self._manager_with_nodes(nodes)._get_bm25_retriever(top_k=2)
        self.assertIsNotNone(built)
        sidecar = Path(self._index_dir.name, "bm25_index")
        self.assertTrue(sidecar.is_dir())
        self.assertTrue((sidecar / "sidecar_meta.json").exists())
        built_results = built.retrieve("needle")
        self.assertEqual(built_results[0].node.node_id, "needle-node")

        with patch.object(
            RealBM25, "from_defaults",
            side_effect=AssertionError("expected sidecar load, got rebuild"),
        ):
            loaded = self._manager_with_nodes(nodes)._get_bm25_retriever(top_k=2)

        self.assertIsNotNone(loaded)
        self.assertEqual(
            [(r.node.node_id, r.node.metadata.get("source"), round(float(r.score), 6))
             for r in loaded.retrieve("needle")],
            [(r.node.node_id, r.node.metadata.get("source"), round(float(r.score), 6))
             for r in built_results],
        )

    def test_bm25_sidecar_stale_doc_count_triggers_rebuild(self):
        from llama_index.core.schema import TextNode

        three = {
            f"n{i}": TextNode(text=f"text {i} common", id_=f"n{i}")
            for i in range(3)
        }
        four = dict(three)
        four["n3"] = TextNode(text="text 3 common", id_="n3")

        first = self._manager_with_nodes(three)._get_bm25_retriever(top_k=2)
        self.assertIsNotNone(first)
        meta_path = Path(self._index_dir.name, "bm25_index", "sidecar_meta.json")
        self.assertEqual(json.loads(meta_path.read_text())["doc_count"], 3)

        # Docstore grew by one chunk: the sidecar must be rejected and the
        # rebuild must persist a fresh sidecar for the new state.
        second = self._manager_with_nodes(four)._get_bm25_retriever(top_k=2)
        self.assertIsNotNone(second)
        self.assertEqual(json.loads(meta_path.read_text())["doc_count"], 4)

    def test_bm25_sidecar_not_persisted_while_indexing(self):
        from llama_index.core.schema import TextNode

        manager = self._manager_with_nodes({
            "n0": TextNode(text="alpha common", id_="n0"),
            "n1": TextNode(text="beta common", id_="n1"),
        })
        with manager._status_lock:
            manager._index_state = "embedding"

        retriever = manager._get_bm25_retriever(top_k=1)

        self.assertIsNotNone(retriever)
        self.assertFalse(Path(self._index_dir.name, "bm25_index").exists())

    def test_invalidate_retrieval_caches_removes_sidecar(self):
        from rag.vault import ObsidianVaultManager

        sidecar = Path(self._index_dir.name, "bm25_index")
        sidecar.mkdir(parents=True)
        (sidecar / "sidecar_meta.json").write_text("{}", encoding="utf-8")

        ObsidianVaultManager()._invalidate_retrieval_caches()

        self.assertFalse(sidecar.exists())


class TestVaultRerankerDeviceKnob(unittest.TestCase):
    """vault_reranker_device: 'auto' must stay byte-identical to the pre-knob
    construction (no device kwarg → infer_torch_device picks MPS/CPU);
    'cpu'/'mps' pass through; non-CPU failures retry on CPU without setting
    the sticky failure flag; knob changes reset a sticky failure."""

    def _manager(self):
        from rag.vault import ObsidianVaultManager
        return ObsidianVaultManager()

    def test_resolve_device_mode_normalises_and_defaults(self):
        manager = self._manager()
        cases = [
            ("auto", "auto"),
            ("cpu", "cpu"),
            (" MPS ", "mps"),
            ("cuda", "auto"),   # unknown → pre-knob behaviour
            ("", "auto"),
            (None, "auto"),
        ]
        for raw, expected in cases:
            with patch(
                "rag.vault.load_config_readonly",
                return_value={"vault_reranker_device": raw},
            ):
                self.assertEqual(
                    manager._resolve_reranker_device_mode(), expected, raw
                )
        with patch("rag.vault.load_config_readonly", return_value={}):
            self.assertEqual(manager._resolve_reranker_device_mode(), "auto")
        with patch("rag.vault.load_config_readonly", side_effect=OSError("disk")):
            self.assertEqual(manager._resolve_reranker_device_mode(), "auto")

    def test_auto_mode_omits_device_argument(self):
        calls = []

        class FakeRerank:
            def __init__(self, **kwargs):
                calls.append(dict(kwargs))
                self.top_n = kwargs.get("top_n")

        with (
            patch("rag.engine.SentenceTransformerRerank", FakeRerank),
            patch(
                "rag.vault.load_config_readonly",
                return_value={"vault_reranker_device": "auto"},
            ),
        ):
            reranker = self._manager()._get_reranker(model_name="m", top_n=3)

        self.assertIsNotNone(reranker)
        self.assertEqual(calls, [{"model": "m", "top_n": 3}])

    def test_cpu_mode_passes_device_argument(self):
        calls = []

        class FakeRerank:
            def __init__(self, **kwargs):
                calls.append(dict(kwargs))
                self.top_n = kwargs.get("top_n")

        with (
            patch("rag.engine.SentenceTransformerRerank", FakeRerank),
            patch(
                "rag.vault.load_config_readonly",
                return_value={"vault_reranker_device": "cpu"},
            ),
        ):
            reranker = self._manager()._get_reranker(model_name="m", top_n=3)

        self.assertIsNotNone(reranker)
        self.assertEqual(calls, [{"model": "m", "top_n": 3, "device": "cpu"}])

    def test_mps_failure_falls_back_to_cpu_without_sticking(self):
        calls = []

        class FakeRerank:
            def __init__(self, **kwargs):
                calls.append(dict(kwargs))
                if kwargs.get("device") == "mps":
                    raise RuntimeError("Metal allocation failed")
                self.top_n = kwargs.get("top_n")

        manager = self._manager()
        with (
            patch("rag.engine.SentenceTransformerRerank", FakeRerank),
            patch(
                "rag.vault.load_config_readonly",
                return_value={"vault_reranker_device": "mps"},
            ),
        ):
            first = manager._get_reranker(model_name="m", top_n=3)
            self.assertIsNotNone(first)
            self.assertFalse(manager._reranker_failed)
            self.assertEqual([c.get("device") for c in calls], ["mps", "cpu"])
            # The session keeps the CPU instance via the cache — no rebuild.
            # Item 2.7: the cached fetch is READ-ONLY (top_n is now tuned only
            # by the engine's locked pipeline build), so the constructor-time
            # value survives the fetch untouched.
            second = manager._get_reranker(model_name="m", top_n=5)
            self.assertIs(second, first)
            self.assertEqual(len(calls), 2)
            self.assertEqual(first.top_n, 3)

    def test_warmup_failure_on_non_cpu_device_triggers_cpu_retry(self):
        """Construction can succeed while the first forward pass fails —
        the warm-up predict must route that failure into the CPU retry."""
        calls = []

        class ExplodingModel:
            def predict(self, pairs):
                raise RuntimeError("MPS backend out of memory")

        class FakeRerank:
            def __init__(self, **kwargs):
                calls.append(dict(kwargs))
                self.top_n = kwargs.get("top_n")
                self._device = kwargs.get("device") or "mps"  # auto resolves to mps
                self._model = (
                    ExplodingModel() if self._device != "cpu" else object()
                )

        manager = self._manager()
        with (
            patch("rag.engine.SentenceTransformerRerank", FakeRerank),
            patch(
                "rag.vault.load_config_readonly",
                return_value={"vault_reranker_device": "auto"},
            ),
        ):
            reranker = manager._get_reranker(model_name="m", top_n=3)

        self.assertIsNotNone(reranker)
        self.assertEqual(reranker._device, "cpu")
        self.assertFalse(manager._reranker_failed)
        self.assertEqual(len(calls), 2)

    def test_device_change_resets_sticky_failure(self):
        attempts = []

        class AlwaysFail:
            def __init__(self, **kwargs):
                attempts.append(dict(kwargs))
                raise RuntimeError("model unavailable")

        manager = self._manager()
        with patch("rag.engine.SentenceTransformerRerank", AlwaysFail):
            with patch(
                "rag.vault.load_config_readonly",
                return_value={"vault_reranker_device": "cpu"},
            ):
                self.assertIsNone(manager._get_reranker(model_name="m", top_n=3))
                self.assertTrue(manager._reranker_failed)
                self.assertEqual(len(attempts), 1)
                # Same model+device: sticky short-circuit, no new attempt.
                self.assertIsNone(manager._get_reranker(model_name="m", top_n=3))
                self.assertEqual(len(attempts), 1)
            with patch(
                "rag.vault.load_config_readonly",
                return_value={"vault_reranker_device": "auto"},
            ):
                # New device mode = new failure key → retry allowed
                # (auto attempt + its CPU fallback both fail → sticky again).
                self.assertIsNone(manager._get_reranker(model_name="m", top_n=3))
                self.assertTrue(manager._reranker_failed)
                self.assertEqual(len(attempts), 3)


class TestVaultPrewarm(unittest.TestCase):
    """Cover the prewarm() contract: idempotency, skip-when-empty, status payload."""

    def _fresh_manager(self):
        from rag.vault import ObsidianVaultManager
        return ObsidianVaultManager()

    def test_prewarm_skips_when_no_docstore_on_disk(self):
        manager = self._fresh_manager()
        with tempfile.TemporaryDirectory() as td:
            with patch("rag.vault.OBSIDIAN_INDEX_DIR", td):
                manager.prewarm()
        status, message = manager.get_prewarm_state()
        self.assertEqual(status, "skipped")
        self.assertIn("No vault index", message)

    def test_prewarm_disabled_by_config_skips_before_touching_disk(self):
        """vault_prewarm_enabled=False must short-circuit to a terminal
        'skipped' state (vault.js hides the banner and enables Send for it)
        without consulting the index dir — covers indexes that DO exist on
        disk but whose owner deferred the load to first chat."""
        manager = self._fresh_manager()
        with tempfile.TemporaryDirectory() as td:
            # A real docstore exists — only the knob may cause the skip.
            Path(td, "docstore.json").write_text("{}", encoding="utf-8")
            with (
                patch("rag.vault.OBSIDIAN_INDEX_DIR", td),
                patch(
                    "rag.vault.load_config",
                    return_value={"vault_prewarm_enabled": False},
                ),
            ):
                manager.prewarm()
        status, message = manager.get_prewarm_state()
        self.assertEqual(status, "skipped")
        self.assertIn("disabled", message.lower())

    def test_prewarm_is_idempotent(self):
        manager = self._fresh_manager()
        with tempfile.TemporaryDirectory() as td:
            with patch("rag.vault.OBSIDIAN_INDEX_DIR", td):
                manager.prewarm()
                # Second call must short-circuit on the _prewarm_started flag.
                # We assert by ensuring the state is still "skipped" — a second
                # walk through the loader path would still produce "skipped"
                # here, so check that os.path.exists is only consulted once.
                with patch("os.path.exists") as exists:
                    exists.return_value = False
                    manager.prewarm()
                    self.assertEqual(exists.call_count, 0)

    def test_prewarm_status_appears_in_get_status_payload(self):
        manager = self._fresh_manager()
        payload = manager.get_status_payload()
        self.assertIn("prewarm_status", payload)
        self.assertIn("prewarm_message", payload)
        self.assertEqual(payload["prewarm_status"], "idle")

    def test_reset_prewarm_clears_started_flag(self):
        manager = self._fresh_manager()
        with tempfile.TemporaryDirectory() as td:
            with patch("rag.vault.OBSIDIAN_INDEX_DIR", td):
                manager.prewarm()
        self.assertEqual(manager.get_prewarm_state()[0], "skipped")
        manager.reset_prewarm()
        self.assertEqual(manager.get_prewarm_state()[0], "idle")
        # After reset, prewarm() must be allowed to run again.
        with tempfile.TemporaryDirectory() as td:
            with patch("rag.vault.OBSIDIAN_INDEX_DIR", td):
                manager.prewarm()
        self.assertEqual(manager.get_prewarm_state()[0], "skipped")

    def test_reset_prewarm_invalidates_in_flight_set_prewarm(self):
        """A late callback from an in-flight prewarm thread must not
        clobber the post-reset `idle` slot. The generation token in
        _set_prewarm guarantees stale updates are silently dropped."""
        manager = self._fresh_manager()
        # Capture the current generation as a thread-with-stale-token would.
        with manager._prewarm_lock:
            stale_gen = manager._prewarm_generation
        manager.reset_prewarm()
        # Simulate the stale thread trying to write its terminal status.
        manager._set_prewarm("ready", "Vault is ready.", generation=stale_gen)
        # The reset's `idle` must survive.
        self.assertEqual(manager.get_prewarm_state()[0], "idle")
        # A non-stale write (current generation) still goes through.
        with manager._prewarm_lock:
            current_gen = manager._prewarm_generation
        manager._set_prewarm("ready", "Vault is ready.", generation=current_gen)
        self.assertEqual(manager.get_prewarm_state()[0], "ready")


class TestEngineRetrieveExtraction(unittest.TestCase):
    """The agent's vault_search tool calls SimpleQueryEngine.retrieve()
    instead of query() — exercises the pipeline up to the LLM call and
    returns RetrievedChunk objects. These tests pin the contract."""

    def _engine_with_mocks(self, nodes):
        from rag.engine import SimpleQueryEngine

        retriever = MagicMock()
        retriever.retrieve.return_value = nodes
        postprocessor = MagicMock()
        # Identity postprocessor — return what we got.
        postprocessor.postprocess_nodes.return_value = nodes
        llm = MagicMock()

        engine = SimpleQueryEngine.__new__(SimpleQueryEngine)
        engine.index = MagicMock()
        engine.llm_name = "m"
        engine.embed_name = "e"
        engine.top_k = 6
        engine.provider_name = "ollama"
        engine.similarity_cutoff = 0.25
        engine.prompt_mode = "strict"
        engine.temperature = None
        engine.top_k_explicit = False
        engine.bm25_retriever = None
        engine.reranker = None
        engine.custom_system_prompt = ""
        engine._provider = MagicMock()
        return engine, retriever, postprocessor, llm

    def test_nodes_to_chunks_maps_text_score_and_source(self):
        from rag.engine import SimpleQueryEngine

        node_a = MagicMock(text="alpha", score=0.9, metadata={"source": "notes/a.md"})
        node_b = MagicMock(text="beta", score=0.4, metadata={"file_path": "/abs/b.md"})
        chunks = SimpleQueryEngine._nodes_to_chunks([node_a, node_b])
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].text, "alpha")
        self.assertEqual(chunks[0].source, "notes/a.md")
        self.assertAlmostEqual(chunks[0].score, 0.9)
        self.assertEqual(chunks[1].source, "/abs/b.md")

    def test_retrieve_chunks_runs_postprocessor_then_maps(self):
        node = MagicMock(text="x", score=0.7, metadata={"source": "n.md"})
        engine, retriever, postprocessor, _llm = self._engine_with_mocks([node])
        chunks = engine._retrieve_chunks("q", retriever, [postprocessor])
        retriever.retrieve.assert_called_once_with("q")
        postprocessor.postprocess_nodes.assert_called_once()
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, "x")

    def test_retrieve_chunks_handles_old_postprocessor_signature(self):
        """LlamaIndex postprocessors that pre-date the keyword-only
        ``query_bundle`` argument receive the bundle positionally; the
        TypeError fallback path must engage."""
        node = MagicMock(text="x", score=0.0, metadata={})
        engine, retriever, _post, _ = self._engine_with_mocks([node])
        legacy = MagicMock()

        def _legacy(*args, **kwargs):
            if "query_bundle" in kwargs:
                raise TypeError("unexpected keyword argument 'query_bundle'")
            return [node]

        legacy.postprocess_nodes.side_effect = _legacy
        engine._retrieve_chunks("q", retriever, [legacy])
        # Two calls: one kw-attempt that raised, one positional that succeeded.
        self.assertEqual(legacy.postprocess_nodes.call_count, 2)

    def test_retrieve_public_method_calls_build_pipeline_and_returns_chunks(self):
        node = MagicMock(text="y", score=0.5, metadata={"source": "n.md"})
        engine, retriever, postprocessor, llm = self._engine_with_mocks([node])
        with patch.object(
            engine, "_build_retrieval_pipeline",
            return_value=(retriever, [postprocessor], llm),
        ) as mock_build, patch("rag.engine.load_config", return_value={}):
            chunks = engine.retrieve("question")
        # Assert on the captured mock — once the with-block exits, the
        # attribute on the engine is the restored real method again.
        mock_build.assert_called_once_with({})
        self.assertEqual(len(chunks), 1)
        from core.llm.types import RetrievedChunk
        self.assertIsInstance(chunks[0], RetrievedChunk)


class TestVaultRetrieveAndReadNote(unittest.TestCase):
    """Covers ObsidianVaultManager.retrieve() and read_note(): the two
    helpers the agent loop's vault_search / vault_read_note tools call."""

    def _bare_manager(self):
        """An ObsidianVaultManager with no real index — used to test
        method contracts in isolation. Tests that need a populated
        index patch self._index directly."""
        from rag.vault import ObsidianVaultManager
        return ObsidianVaultManager()

    # ---- retrieve() ----------------------------------------------------

    def test_retrieve_lazy_loads_index_then_holds_mutation_lock(self):
        manager = self._bare_manager()
        manager._index = MagicMock()
        call_order: list[str] = []

        fake_engine = MagicMock()
        fake_engine.retrieve.return_value = []

        original_lock = manager._index_mutation_lock

        class _TracingLock:
            def __enter__(self_inner):
                call_order.append("lock_acquire")
                return original_lock.__enter__()
            def __exit__(self_inner, *args):
                call_order.append("lock_release")
                return original_lock.__exit__(*args)
            # The chat path acquires via the 2.1 timed acquire (acquire(timeout=)
            # + release in a finally) rather than the `with` protocol — trace
            # both shapes so this test keeps pinning the ordering either way.
            def acquire(self_inner, timeout=None, blocking=True):
                call_order.append("lock_acquire")
                return original_lock.acquire()
            def release(self_inner):
                call_order.append("lock_release")
                return original_lock.release()

        manager._index_mutation_lock = _TracingLock()

        with patch("rag.vault.SimpleQueryEngine", return_value=fake_engine, create=True), \
             patch.object(manager, "_ensure_index_loaded", return_value=manager._index) as mock_ensure, \
             patch("rag.engine.SimpleQueryEngine", return_value=fake_engine):
            manager.retrieve(
                "q",
                llm_name="m",
                embed_name="e",
                provider_name="ollama",
            )
        mock_ensure.assert_called_once()
        # Retrieval body ran under the lock.
        self.assertIn("lock_acquire", call_order)
        self.assertIn("lock_release", call_order)
        fake_engine.retrieve.assert_called_once_with("q")

    def test_retrieve_raises_when_no_index_on_disk(self):
        manager = self._bare_manager()
        with patch.object(
            manager, "_ensure_index_loaded",
            side_effect=RuntimeError("Index not found. Please index the vault first."),
        ):
            with self.assertRaisesRegex(RuntimeError, "Index not found"):
                manager.retrieve(
                    "q", llm_name="m", embed_name="e", provider_name="ollama",
                )

    def test_retrieve_passes_hybrid_and_reranker_flags(self):
        manager = self._bare_manager()
        manager._index = MagicMock()
        bm25 = MagicMock()
        rerank = MagicMock()
        with patch.object(manager, "_ensure_index_loaded", return_value=manager._index), \
             patch.object(manager, "_get_bm25_retriever", return_value=bm25) as get_bm25, \
             patch.object(manager, "_get_reranker", return_value=rerank) as get_rerank, \
             patch("rag.engine.SimpleQueryEngine") as engine_cls:
            engine_cls.return_value.retrieve.return_value = []
            manager.retrieve(
                "q",
                llm_name="m", embed_name="e", provider_name="ollama",
                top_k=4,
                hybrid_enabled=True,
                reranker_enabled=True, reranker_model="ms-marco",
            )
        get_bm25.assert_called_once_with(top_k=4)
        get_rerank.assert_called_once_with(model_name="ms-marco", top_n=4)
        engine_kwargs = engine_cls.call_args.kwargs
        self.assertIs(engine_kwargs["bm25_retriever"], bm25)
        self.assertIs(engine_kwargs["reranker"], rerank)

    # ---- read_note() ---------------------------------------------------

    def _vault_manager_with_tmp_vault(self, tmpdir: Path):
        manager = self._bare_manager()
        manager._vault_path = str(tmpdir)
        return manager

    def test_read_note_md_returns_text_and_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "notes").mkdir()
            (vault / "notes" / "x.md").write_text("hello world", encoding="utf-8")
            manager = self._vault_manager_with_tmp_vault(vault)
            with patch("rag.vault.load_config", return_value={"vault_exclude_dirs": []}):
                text, truncated = manager.read_note("notes/x.md")
            self.assertEqual(text, "hello world")
            self.assertFalse(truncated)

    def test_read_note_truncates_at_max_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "big.md").write_text("a" * 1000, encoding="utf-8")
            manager = self._vault_manager_with_tmp_vault(vault)
            with patch("rag.vault.load_config", return_value={"vault_exclude_dirs": []}):
                text, truncated = manager.read_note("big.md", max_chars=100)
            self.assertEqual(len(text), 100)
            self.assertTrue(truncated)

    def test_read_note_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            # Create a file OUTSIDE the vault that traversal would reach.
            outside = Path(tmp) / "secret.md"
            outside.write_text("forbidden", encoding="utf-8")
            manager = self._vault_manager_with_tmp_vault(vault)
            with patch("rag.vault.load_config", return_value={"vault_exclude_dirs": []}):
                with self.assertRaisesRegex(ValueError, "outside the vault"):
                    manager.read_note("../secret.md")

    def test_read_note_rejects_excluded_dir(self):
        from rag.vault import OBSIDIAN_EXCLUDED_DIR_NAMES
        # Pick any known excluded dir (e.g. ".obsidian" or ".git").
        excluded = next(iter(OBSIDIAN_EXCLUDED_DIR_NAMES))
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / excluded).mkdir()
            (vault / excluded / "x.md").write_text("hi", encoding="utf-8")
            manager = self._vault_manager_with_tmp_vault(vault)
            with patch("rag.vault.load_config", return_value={"vault_exclude_dirs": []}):
                with self.assertRaisesRegex(ValueError, "excluded directory"):
                    manager.read_note(f"{excluded}/x.md")

    def test_read_note_rejects_user_excluded_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "private").mkdir()
            (vault / "private" / "x.md").write_text("hi", encoding="utf-8")
            manager = self._vault_manager_with_tmp_vault(vault)
            with patch("rag.vault.load_config", return_value={"vault_exclude_dirs": ["private"]}):
                with self.assertRaisesRegex(ValueError, "user-excluded directory"):
                    manager.read_note("private/x.md")

    def test_read_note_rejects_unsupported_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "x.docx").write_bytes(b"binary")
            manager = self._vault_manager_with_tmp_vault(vault)
            with patch("rag.vault.load_config", return_value={"vault_exclude_dirs": []}):
                with self.assertRaisesRegex(ValueError, "Unsupported extension"):
                    manager.read_note("x.docx")

    def test_read_note_rejects_non_existent_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._vault_manager_with_tmp_vault(Path(tmp))
            with patch("rag.vault.load_config", return_value={"vault_exclude_dirs": []}):
                with self.assertRaises(FileNotFoundError):
                    manager.read_note("missing.md")

    def test_read_note_pdf_uses_cache_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            pdf_path = vault / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\nfake-pdf-bytes\n")
            manager = self._vault_manager_with_tmp_vault(vault)
            # Pre-populate the PDF text cache so the bounded fresh extract
            # is never invoked. The cache key is sha256(file contents).
            sig = manager._pdf_file_signature(pdf_path)
            cache_file = manager._pdf_cache_file(vault.resolve(), sig)
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text("cached extract text", encoding="utf-8")
            with patch("rag.vault.load_config", return_value={"vault_exclude_dirs": []}), \
                 patch("rag.vault.extract_structured_from_pdf") as fresh_extract:
                text, truncated = manager.read_note("paper.pdf")
            self.assertEqual(text, "cached extract text")
            self.assertFalse(truncated)
            fresh_extract.assert_not_called()

    def test_read_note_pdf_falls_back_to_fresh_extract_when_uncached(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            pdf_path = vault / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\nfake-pdf\n")
            manager = self._vault_manager_with_tmp_vault(vault)
            fake_sections = MagicMock(full_text="extracted text")
            with patch("rag.vault.load_config", return_value={"vault_exclude_dirs": []}), \
                 patch("rag.vault.extract_structured_from_pdf", return_value=fake_sections) as fresh:
                text, _trunc = manager.read_note("paper.pdf")
            self.assertEqual(text, "extracted text")
            fresh.assert_called_once()
            # Fresh extract must be bounded — no OCR, page cap applied.
            kwargs = fresh.call_args.kwargs
            from rag.vault import EXTRACT_MAX_PAGES_PER_CALL
            self.assertEqual(kwargs.get("end_page"), EXTRACT_MAX_PAGES_PER_CALL)
            self.assertNotIn("ocr_cb", kwargs)

    def test_read_note_pdf_reuses_persisted_signature_without_rehash(self):
        """_read_pdf_text consults the persisted pdf_signatures.json — an
        unchanged PDF is never fully re-hashed just to locate its text cache
        (a multi-hundred-MB read per agent vault_read_note call before)."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            pdf_path = vault / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\nfake-pdf-bytes\n")
            manager = self._vault_manager_with_tmp_vault(vault)
            st = pdf_path.stat()
            fake_sha = "f" * 64
            manager._save_pdf_signature_cache({
                "paper.pdf": {
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "sha256": fake_sha,
                },
            })
            cache_file = manager._pdf_cache_file(vault.resolve(), {"sha256": fake_sha})
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text("cached via persisted signature", encoding="utf-8")
            with patch("rag.vault.load_config", return_value={"vault_exclude_dirs": []}), \
                 patch.object(manager, "_sha256_file") as rehash, \
                 patch("rag.vault.extract_structured_from_pdf") as fresh:
                text, truncated = manager.read_note("paper.pdf")
            self.assertEqual(text, "cached via persisted signature")
            self.assertFalse(truncated)
            rehash.assert_not_called()
            fresh.assert_not_called()

    def test_read_note_pdf_stale_persisted_signature_rehashes(self):
        """A size/mtime mismatch in the persisted map falls back to a real
        content hash (never trusts a stale sha256)."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            pdf_path = vault / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\nfake-pdf-bytes\n")
            manager = self._vault_manager_with_tmp_vault(vault)
            manager._save_pdf_signature_cache({
                "paper.pdf": {"size": 1, "mtime_ns": 1, "sha256": "f" * 64},
            })
            real_sig = {"sha256": manager._sha256_file(pdf_path)}
            cache_file = manager._pdf_cache_file(vault.resolve(), real_sig)
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text("cached under real digest", encoding="utf-8")
            with patch("rag.vault.load_config", return_value={"vault_exclude_dirs": []}), \
                 patch.object(manager, "_sha256_file",
                              wraps=manager._sha256_file) as rehash, \
                 patch("rag.vault.extract_structured_from_pdf") as fresh:
                text, _trunc = manager.read_note("paper.pdf")
            self.assertEqual(text, "cached under real digest")
            rehash.assert_called_once()
            fresh.assert_not_called()

    def test_persisted_pdf_signatures_stat_gate_refreshes_on_rewrite(self):
        """The in-memory signature view is served from cache while the file
        is unchanged and refreshes when pdf_signatures.json is rewritten."""
        manager = self._bare_manager()
        manager._save_pdf_signature_cache(
            {"a.pdf": {"size": 1, "mtime_ns": 2, "sha256": "x" * 64}})
        first = manager._persisted_pdf_signatures()
        self.assertIn("a.pdf", first)
        # Unchanged file → the exact same cached object (no reload).
        self.assertIs(manager._persisted_pdf_signatures(), first)
        manager._save_pdf_signature_cache(
            {"b.pdf": {"size": 3, "mtime_ns": 4, "sha256": "y" * 64},
             "c.pdf": {"size": 5, "mtime_ns": 6, "sha256": "z" * 64}})
        second = manager._persisted_pdf_signatures()
        self.assertIn("b.pdf", second)

    def test_read_note_raises_when_vault_path_not_configured(self):
        manager = self._bare_manager()
        manager._vault_path = None
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            manager.read_note("x.md")

    def test_read_note_rejects_empty_and_null_byte_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._vault_manager_with_tmp_vault(Path(tmp))
            with self.assertRaises(ValueError):
                manager.read_note("")
            with self.assertRaises(ValueError):
                manager.read_note("x\x00.md")


class TestVaultChatAgentMode(unittest.TestCase):
    """Route-level coverage for the opt-in agent path on /api/obsidian/chat."""

    def test_agent_event_to_queue_item_iteration(self):
        from api.routes.vault import _agent_event_to_queue_item
        from core.agent.protocol import IterationEvent
        self.assertEqual(
            _agent_event_to_queue_item(IterationEvent(index=3)),
            {"iteration": 3},
        )

    def test_agent_event_to_queue_item_thought(self):
        from api.routes.vault import _agent_event_to_queue_item
        from core.agent.protocol import ThoughtEvent
        self.assertEqual(
            _agent_event_to_queue_item(ThoughtEvent(text="thinking…")),
            {"thought": "thinking…"},
        )

    def test_agent_event_to_queue_item_tool_call_serialises_dataclass(self):
        from api.routes.vault import _agent_event_to_queue_item
        from core.agent.protocol import ToolCallEvent
        from core.llm.types import ToolCall

        call = ToolCall(
            id="call_x", name="vault_search",
            arguments={"q": "hello"}, raw_arguments='{"q":"hello"}',
        )
        item = _agent_event_to_queue_item(ToolCallEvent(call))
        # The dict shape must be JSON-serialisable for the SSE consumer.
        self.assertEqual(item["tool_call"]["id"], "call_x")
        self.assertEqual(item["tool_call"]["name"], "vault_search")
        self.assertEqual(item["tool_call"]["arguments"], {"q": "hello"})
        # Confirm json.dumps doesn't choke.
        self.assertIn("vault_search", json.dumps(item))

    def test_agent_event_to_queue_item_tool_result_carries_truncated_flag(self):
        from api.routes.vault import _agent_event_to_queue_item
        from core.agent.protocol import ToolResultEvent
        from core.llm.types import ToolResult

        result = ToolResult(tool_call_id="call_x", content="payload", is_error=False)
        item = _agent_event_to_queue_item(ToolResultEvent(result, truncated=True))
        self.assertEqual(item["tool_result"]["tool_call_id"], "call_x")
        self.assertEqual(item["tool_result"]["content"], "payload")
        self.assertFalse(item["tool_result"]["is_error"])
        self.assertTrue(item["tool_result"]["truncated"])

    def test_format_agent_usage_emits_iterations_and_tokens(self):
        from api.routes.vault import _format_agent_usage
        from core.agent.budget import UsageBudget
        from core.llm.types import LLMUsage

        budget = UsageBudget()
        budget.record(LLMUsage(input_tokens=120, output_tokens=40))
        budget.record(LLMUsage(input_tokens=200, output_tokens=80))
        text = _format_agent_usage(budget)
        self.assertIn("2 iterations", text)
        self.assertIn("320 in", text)
        self.assertIn("120 out", text)
        # No cost suffix when cost is zero (local models).
        self.assertNotIn("$", text)

    def test_format_agent_usage_includes_cost_when_nonzero(self):
        from api.routes.vault import _format_agent_usage
        from core.agent.budget import UsageBudget
        from core.llm.types import LLMUsage

        budget = UsageBudget()
        budget.record(LLMUsage(
            input_tokens=120, output_tokens=40, estimated_cost_usd=0.0123,
        ))
        text = _format_agent_usage(budget)
        self.assertIn("1 iteration", text)
        self.assertIn("$0.0123", text)

    def test_agent_event_to_queue_item_token_info_error_done(self):
        from api.routes.vault import _agent_event_to_queue_item
        from core.agent.protocol import (
            DoneEvent, ErrorEvent, InfoEvent, TokenEvent,
        )
        self.assertEqual(_agent_event_to_queue_item(TokenEvent("t")), {"token": "t"})
        self.assertEqual(_agent_event_to_queue_item(InfoEvent("i")), {"info": "i"})
        self.assertEqual(_agent_event_to_queue_item(ErrorEvent("e")), {"error": "e"})
        self.assertIsNone(_agent_event_to_queue_item(DoneEvent()))

    # ---- _resolve_chat_params: agent fields ---------------------------

    def test_resolve_chat_params_picks_agent_enabled_from_body(self):
        from api.routes.vault import _resolve_chat_params
        out = _resolve_chat_params(
            {"agent_enabled": True},
            {"vault_agent_enabled": False},
        )
        self.assertTrue(out.get("agent_enabled"))

    def test_resolve_chat_params_falls_back_to_config_for_agent_enabled(self):
        from api.routes.vault import _resolve_chat_params
        out = _resolve_chat_params({}, {"vault_agent_enabled": True})
        self.assertTrue(out.get("agent_enabled"))

    def test_resolve_chat_params_clamps_agent_max_iterations_high(self):
        from api.routes.vault import _resolve_chat_params
        # coerce_int_in_range CLAMPS out-of-range numerics to the bound
        # (documented contract, pinned in test_validators.py) — the body
        # value still wins, clamped to the 12 cap, rather than falling
        # through to config.
        out = _resolve_chat_params(
            {"agent_max_iterations": 999},
            {"vault_agent_max_iterations": 8},
        )
        self.assertEqual(out.get("agent_max_iterations"), 12)

    def test_resolve_chat_params_clamps_agent_max_iterations_low(self):
        from api.routes.vault import _resolve_chat_params
        out = _resolve_chat_params(
            {"agent_max_iterations": 0},
            {"vault_agent_max_iterations": 4},
        )
        # Clamped to the floor of the 1-12 range, not replaced by config.
        self.assertEqual(out.get("agent_max_iterations"), 1)

    def test_resolve_chat_params_omits_agent_keys_when_absent(self):
        from api.routes.vault import _resolve_chat_params
        out = _resolve_chat_params({}, {})
        self.assertNotIn("agent_enabled", out)
        self.assertNotIn("agent_max_iterations", out)

    def test_resolve_chat_params_reads_new_retrieval_knobs_from_body(self):
        from api.routes.vault import _resolve_chat_params
        out = _resolve_chat_params(
            {"mmr_enabled": True, "mmr_lambda": 0.4,
             "query_expansion": True, "num_queries": 4},
            {},
        )
        self.assertIs(out.get("mmr_enabled"), True)
        self.assertEqual(out.get("mmr_lambda"), 0.4)
        self.assertIs(out.get("query_expansion"), True)
        self.assertEqual(out.get("num_queries"), 4)

    def test_resolve_chat_params_new_knobs_fall_back_to_config(self):
        from api.routes.vault import _resolve_chat_params
        out = _resolve_chat_params(
            {},
            {"vault_mmr_enabled": True, "vault_mmr_lambda": 0.6,
             "vault_query_expansion": True, "vault_num_queries": 2},
        )
        self.assertIs(out.get("mmr_enabled"), True)
        self.assertEqual(out.get("mmr_lambda"), 0.6)
        self.assertIs(out.get("query_expansion"), True)
        self.assertEqual(out.get("num_queries"), 2)

    def test_resolve_chat_params_reads_rerank_pool_ceiling_from_body(self):
        from api.routes.vault import _resolve_chat_params
        out = _resolve_chat_params({"rerank_pool_ceiling": 80}, {})
        self.assertEqual(out.get("rerank_pool_ceiling"), 80)

    # ---- Routing: agent on/off branches ------------------------------

    def test_chat_route_with_agent_disabled_calls_stream_chat(self):
        """Default config has vault_agent_enabled=False → existing
        single-shot RAG path runs unchanged."""
        from app import app
        from rag.vault import obsidian_manager

        app.config["TESTING"] = True
        client = app.test_client()
        headers = {"X-Requested-With": "ChatEKLD"}

        # Stub manager so we don't actually load the index.
        with patch.object(obsidian_manager, "stream_chat") as stream_chat, \
             patch.object(obsidian_manager, "get_status", return_value="done"):
            stream_chat.return_value = MagicMock(response_gen=iter(["hello"]))
            resp = client.post(
                "/api/obsidian/chat",
                json={"message": "q"},
                headers=headers,
            )
            # Drain SSE.
            list(resp.iter_encoded())
        stream_chat.assert_called_once()

    def test_chat_route_with_agent_enabled_calls_run_agent_loop(self):
        """agent_enabled=True must route through the agent worker
        instead of the legacy stream_chat path."""
        from app import app
        from rag.vault import obsidian_manager

        app.config["TESTING"] = True
        client = app.test_client()
        headers = {"X-Requested-With": "ChatEKLD"}

        # Stub run_agent_loop to emit a final TokenEvent.
        def _fake_loop(*, on_event, **_kwargs):
            from core.agent.protocol import DoneEvent, TokenEvent
            from core.agent.budget import UsageBudget
            on_event(TokenEvent("agent answer"))
            on_event(DoneEvent())
            return UsageBudget()

        with patch("api.routes.vault.run_agent_loop", side_effect=_fake_loop) as loop_mock, \
             patch.object(obsidian_manager, "stream_chat") as stream_chat, \
             patch.object(obsidian_manager, "get_status", return_value="done"):
            resp = client.post(
                "/api/obsidian/chat",
                json={"message": "q", "agent_enabled": True},
                headers=headers,
            )
            body = b"".join(resp.iter_encoded()).decode("utf-8")
        loop_mock.assert_called_once()
        stream_chat.assert_not_called()
        # The token event survives end-to-end through SSE.
        self.assertIn("agent answer", body)

    def test_chat_route_emits_placeholder_when_agent_produces_no_tokens(self):
        """When agent mode finishes with only info events (iteration cap
        reached, etc.), the route must emit a placeholder token so the
        frontend can clear its typing indicator. Otherwise the bot
        bubble stays stuck mid-stream."""
        from app import app
        from api.routes.vault import _NO_AGENT_ANSWER_MSG
        from rag.vault import obsidian_manager

        app.config["TESTING"] = True
        client = app.test_client()
        headers = {"X-Requested-With": "ChatEKLD"}

        def _fake_loop(*, on_event, **_kwargs):
            # Emit only an info event + DoneEvent — no tokens.
            from core.agent.protocol import DoneEvent, InfoEvent
            from core.agent.budget import UsageBudget
            on_event(InfoEvent("Agent reached the 6-iteration limit without a final answer."))
            on_event(DoneEvent())
            return UsageBudget()

        with patch("api.routes.vault.run_agent_loop", side_effect=_fake_loop), \
             patch.object(obsidian_manager, "stream_chat"), \
             patch.object(obsidian_manager, "get_status", return_value="done"):
            resp = client.post(
                "/api/obsidian/chat",
                json={"message": "q", "agent_enabled": True},
                headers=headers,
            )
            body = b"".join(resp.iter_encoded()).decode("utf-8")
        # Info event surfaces alongside the placeholder token.
        self.assertIn("iteration limit", body)
        # json.dumps escapes the em dash in the SSE frame ("—"), so
        # parse the frames back instead of substring-matching the raw body.
        tokens = []
        for line in body.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                payload = json.loads(line[len("data: "):])
                if "token" in payload:
                    tokens.append(payload["token"])
        self.assertIn(_NO_AGENT_ANSWER_MSG, tokens)


class TestReindexInvariant(unittest.TestCase):
    """Guardrail: every query-time RAG change must leave the index-time
    identifiers untouched.  These pins fail loudly if a change touches the
    embedding model, the chunker params, the chunk-ID scheme, or the index
    version — any of which would force users to rebuild the vault index.
    """

    def test_index_version_pinned(self):
        from core.constants import OBSIDIAN_INDEX_VERSION
        self.assertEqual(
            OBSIDIAN_INDEX_VERSION, "obsidian-markdown-v3",
            "Bumping OBSIDIAN_INDEX_VERSION forces a full reindex.",
        )

    def test_default_embedding_model_pinned(self):
        # Pin the constant + the config default (NOT load_config(), which
        # reflects the user's own on-disk choice).  Changing the default
        # embedding model invalidates every stored vector.
        import inspect
        import core.config
        from core.constants import DEFAULT_EMBED
        self.assertEqual(DEFAULT_EMBED, "nomic-embed-text")
        self.assertIn('"embed": DEFAULT_EMBED', inspect.getsource(core.config))

    def test_chunker_params_pinned(self):
        import inspect
        import rag.vault
        src = inspect.getsource(rag.vault)
        self.assertIn(
            "SentenceSplitter(chunk_size=512, chunk_overlap=64)", src,
            "Changing PDF chunk size/overlap forces a reindex.",
        )
        self.assertIn("MarkdownNodeParser(include_metadata=True)", src)
        # The MD secondary cap pass is itself a pinned chunking param: removing
        # it un-caps long sections, and changing the cap re-chunks them.
        self.assertIn(
            "SentenceSplitter(chunk_size=MD_MAX_CHUNK_TOKENS, chunk_overlap=64)", src,
            "Removing/altering the MD secondary cap changes chunk ids.",
        )

    def test_chunk_id_scheme_pinned(self):
        import inspect
        import rag.vault
        src = inspect.getsource(rag.vault)
        # Chunk IDs are "{rel_path}::{sha1(i + text)[:16]}"; changing the hash
        # or the 16-hex slice invalidates every stored chunk id.
        self.assertIn("hashlib.sha1(", src)
        self.assertIn("{rel_str}::{chunk_hash}", src)


class TestMdSecondaryCap(unittest.TestCase):
    """Conditional MD secondary split (MD_MAX_CHUNK_TOKENS).

    MarkdownNodeParser splits .md only at heading boundaries, so a long single
    section exceeded the embedding token limit and was silently truncated. The
    chunker now sub-splits ONLY oversized sections while passing every under-cap
    section through byte-for-byte (no chunk-id churn). PDF chunking is untouched.
    """

    def _chunks(self, manager, vault_dir, rel, text):
        from llama_index.core import Document as LlamaDocument
        p = Path(vault_dir) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        raw = LlamaDocument(text=text, metadata={
            "file_path": str(p), "source": rel, "extension": ".md",
        })
        return list(manager._chunk_raw_documents([raw], vault_dir))

    def test_under_cap_sections_are_byte_identical(self):
        """A note with only small sections yields the exact legacy doc_ids
        (sha1(f'{i}\\n{text}')[:16]) — the no-churn promise."""
        import hashlib
        from rag.vault import ObsidianVaultManager
        manager = ObsidianVaultManager()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vd:
            chunks = self._chunks(manager, vd, "small.md",
                                  "# H1\n\nshort body.\n\n# H2\n\nanother section.")
        self.assertGreaterEqual(len(chunks), 1)
        for i, c in enumerate(chunks):
            expected = hashlib.sha1(
                f"{i}\n{c.text}".encode(), usedforsecurity=False
            ).hexdigest()[:16]
            self.assertEqual(c.doc_id, f"small.md::{expected}")

    def test_oversized_section_is_subsplit_under_cap(self):
        from rag.vault import ObsidianVaultManager
        from core.constants import MD_MAX_CHUNK_TOKENS
        manager = ObsidianVaultManager()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vd:
            chunks = self._chunks(manager, vd, "big.md", "# Big\n\n" + ("word " * 6000))
        self.assertGreater(len(chunks), 1, "an oversized section must split")
        self.assertTrue(all(c.doc_id.startswith("big.md::") for c in chunks))
        self.assertEqual(len(set(c.doc_id for c in chunks)), len(chunks),
                         "sub-chunk doc_ids must be unique")
        self.assertTrue(all("header_path" in (c.metadata or {}) for c in chunks),
                        "header_path must propagate to every sub-chunk")
        # Each sub-chunk is within the token cap (same tokenizer the splitter uses).
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        self.assertTrue(all(len(enc.encode(c.text)) <= MD_MAX_CHUNK_TOKENS for c in chunks))

    def test_attachments_land_on_their_own_subchunk(self):
        """An embed only in a split section's tail must appear on the tail
        sub-chunk, not be smeared across every sub-chunk."""
        from rag.vault import ObsidianVaultManager
        manager = ObsidianVaultManager()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vd:
            text = "# A\n\n" + ("para. " * 3000) + "\n\nsee ![[tail_only.png]]"
            chunks = self._chunks(manager, vd, "att.md", text)
        self.assertGreater(len(chunks), 1)
        with_att = [i for i, c in enumerate(chunks) if c.metadata.get("attachments")]
        self.assertEqual(with_att, [len(chunks) - 1],
                         "attachment must land only on the tail sub-chunk")
        for c in chunks:
            if c.metadata.get("attachments"):
                self.assertIn("attachments", c.excluded_embed_metadata_keys)
                self.assertIn("attachments", c.excluded_llm_metadata_keys)

    def test_mixed_document_churn_boundary(self):
        """Pin the i-shift churn boundary in a under -> over -> under document:
        the section BEFORE the oversized one keeps its exact doc_id (no churn),
        the oversized section splits (more chunks), and the section AFTER it
        shifts position and therefore re-hashes."""
        from rag.vault import ObsidianVaultManager
        manager = ObsidianVaultManager()
        intro = "# First\n\nsmall intro section."
        last = "# Last\n\nsmall trailing section."
        small_mid = "# Mid\n\nshort middle."
        big_mid = "# Mid\n\n" + ("word " * 6000)
        # Same rel path in two temp vaults so the "{rel}::" prefix matches and
        # doc_ids are directly comparable.
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vd:
            small_chunks = self._chunks(manager, vd, "mixed.md",
                                        f"{intro}\n\n{small_mid}\n\n{last}")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vd:
            big_chunks = self._chunks(manager, vd, "mixed.md",
                                      f"{intro}\n\n{big_mid}\n\n{last}")
        # Section BEFORE the oversized one is untouched — same id at position 0.
        self.assertEqual(small_chunks[0].doc_id, big_chunks[0].doc_id)
        # The oversized middle section splits → the big variant has more chunks.
        self.assertGreater(len(big_chunks), len(small_chunks))
        # Section AFTER the oversized one shifts position → its id changes.
        small_last = next(c for c in small_chunks if "trailing section" in c.text)
        big_last = next(c for c in big_chunks if "trailing section" in c.text)
        self.assertNotEqual(small_last.doc_id, big_last.doc_id)

    def test_multibyte_under_cap_section_stays_byte_identical(self):
        """A multibyte (CJK) section under the token cap must not split and must
        stay byte-identical — guards the byte-length pre-filter's correctness."""
        import hashlib
        from rag.vault import ObsidianVaultManager
        manager = ObsidianVaultManager()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as vd:
            chunks = self._chunks(manager, vd, "cjk.md", "# 見出し\n\n" + ("文字" * 100))
        self.assertEqual(len(chunks), 1)
        c = chunks[0]
        expected = hashlib.sha1(
            f"0\n{c.text}".encode(), usedforsecurity=False
        ).hexdigest()[:16]
        self.assertEqual(c.doc_id, f"cjk.md::{expected}")


class TestPdfRangeSplitting(unittest.TestCase):
    """Per-range documents for large vault PDFs (>1000 pages).

    Pins the behaviours the design depends on: one document per 1000-page
    range with page metadata, immediate per-range caching (so a cancel
    keeps completed ranges), resume from cached ranges, and the page_start
    salt in the chunk hash (so two ranges of the same file can never
    collide on a doc_id, while single-document files keep their original
    unsalted IDs — i.e. no vault-wide reindex).
    """

    def setUp(self):
        from rag.vault import ObsidianVaultManager
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_dir = str(Path(self.tmp.name) / "cache")
        self.cache_patch = patch("rag.vault.OBSIDIAN_CACHE_DIR", self.cache_dir)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.manager = ObsidianVaultManager()
        self.vault_root = Path(self.tmp.name) / "vault"
        self.vault_root.mkdir()
        self.pdf_path = self.vault_root / "textbook.pdf"
        self.pdf_path.write_bytes(b"%PDF-fake")
        self.signature = {"size": 9, "mtime_ns": 1, "sha256": "f" * 64}

    def _fake_sections(self, text, truncated=False):
        # Mirror ArticleSections: a real result carries truncated=False, so the
        # MagicMock must set it explicitly (an unset MagicMock attr is truthy,
        # which would wrongly look like an incomplete extraction to the loader).
        section = MagicMock()
        section.full_text = text
        section.truncated = truncated
        return section

    def _run_loader(self, page_count, extract_side_effect):
        with patch(
            "rag.vault.extract_structured_from_pdf",
            side_effect=extract_side_effect,
        ) as extract_mock:
            docs = list(
                self.manager._load_pdf_range_documents(
                    self.pdf_path, "textbook.pdf", ".pdf",
                    self.vault_root, self.signature, page_count,
                )
            )
        return docs, extract_mock

    def test_one_document_per_range_with_page_metadata(self):
        docs, extract_mock = self._run_loader(
            2500,
            lambda *a, **kw: self._fake_sections(
                f"pages {kw['start_page']}-{kw['end_page']}"
            ),
        )
        self.assertEqual(extract_mock.call_count, 3)
        self.assertEqual(
            [(d.metadata["page_start"], d.metadata["page_end"]) for d in docs],
            [(0, 1000), (1000, 2000), (2000, 2500)],
        )
        for doc in docs:
            self.assertEqual(doc.metadata["source"], "textbook.pdf")
        # Each range must be cached the moment it is extracted.
        cached = sorted(
            p.name for p in self.manager._pdf_cache_file(
                self.vault_root, self.signature
            ).parent.glob("*-p*.txt")
        )
        self.assertEqual(cached, [
            "f" * 64 + "-p00000-01000.txt",
            "f" * 64 + "-p01000-02000.txt",
            "f" * 64 + "-p02000-02500.txt",
        ])

    def test_cached_ranges_skip_extraction(self):
        self._run_loader(2500, lambda *a, **kw: self._fake_sections("x"))
        docs, extract_mock = self._run_loader(
            2500, lambda *a, **kw: self.fail("cache hit must not extract")
        )
        self.assertEqual(extract_mock.call_count, 0)
        self.assertEqual(len(docs), 3)

    def test_truncated_range_yielded_but_not_cached(self):
        """An incompletely-extracted range (e.g. scanned pages beyond the OCR
        limit) is still yielded but NOT cached, so the next run retries instead
        of permanently serving the partial."""
        docs, extract_mock = self._run_loader(
            1500,
            lambda *a, **kw: self._fake_sections(
                f"partial {kw['start_page']}", truncated=True
            ),
        )
        # Both ranges extracted and yielded (better partial than nothing)…
        self.assertEqual(extract_mock.call_count, 2)
        self.assertEqual(len(docs), 2)
        # …but nothing cached, so a resumed run re-extracts.
        cache_parent = self.manager._pdf_cache_file(
            self.vault_root, self.signature
        ).parent
        self.assertEqual(list(cache_parent.glob("*-p*.txt")), [])
        docs2, extract_mock2 = self._run_loader(
            1500, lambda *a, **kw: self._fake_sections("x", truncated=True)
        )
        self.assertEqual(extract_mock2.call_count, 2)

    def test_full_range_ocr_max_pages_passed(self):
        """The loader must let the OCR fallback cover the whole 1000-page range
        (the old 100-page cap silently dropped 90% of a scanned range)."""
        seen = {}

        def capture(*a, **kw):
            seen["ocr_max_pages"] = kw.get("ocr_max_pages")
            seen["has_page_cb"] = callable(kw.get("page_done_cb"))
            return self._fake_sections("ok")

        self._run_loader(1500, capture)
        from pdf_extractor import EXTRACT_MAX_PAGES_PER_CALL
        self.assertEqual(seen["ocr_max_pages"], EXTRACT_MAX_PAGES_PER_CALL)
        self.assertTrue(seen["has_page_cb"])

    def test_cancel_mid_file_keeps_completed_range_caches(self):
        def extract_then_stop(*a, **kw):
            # Simulate the user cancelling while range 1 is extracting:
            # range 1 completes, the loop must stop before range 2.
            self.manager._stop_event.set()
            return self._fake_sections("partial")

        docs, extract_mock = self._run_loader(2500, extract_then_stop)
        self.assertEqual(extract_mock.call_count, 1)
        self.assertEqual(len(docs), 1)
        cache_parent = self.manager._pdf_cache_file(
            self.vault_root, self.signature
        ).parent
        self.assertEqual(len(list(cache_parent.glob("*-p*.txt"))), 1)
        # A later (resumed) run re-uses range 1's cache and extracts the rest.
        self.manager._stop_event.clear()
        docs, extract_mock = self._run_loader(
            2500, lambda *a, **kw: self._fake_sections("rest")
        )
        self.assertEqual(extract_mock.call_count, 2)
        self.assertEqual(len(docs), 3)

    def test_chunk_hash_salted_only_for_range_documents(self):
        from llama_index.core import Document as LlamaDocument

        text = "Lorem ipsum dolor sit amet. " * 10
        plain = LlamaDocument(text=text, metadata={
            "file_path": str(self.pdf_path), "source": "textbook.pdf",
            "extension": ".pdf",
        })
        ranged = LlamaDocument(text=text, metadata={
            "file_path": str(self.pdf_path), "source": "textbook.pdf",
            "extension": ".pdf", "page_start": 1000, "page_end": 2000,
        })
        plain_ids = [
            d.doc_id for d in self.manager._chunk_raw_documents(
                [plain], str(self.vault_root)
            )
        ]
        ranged_ids = [
            d.doc_id for d in self.manager._chunk_raw_documents(
                [ranged], str(self.vault_root)
            )
        ]
        # Single-document files keep the original unsalted scheme — the
        # no-reindex guarantee. SentenceSplitter may normalise whitespace,
        # so recompute the expected legacy hash from the emitted text.
        self.assertEqual(len(plain_ids), 1)
        self.assertTrue(plain_ids[0].startswith("textbook.pdf::"))
        # Range documents share the rel:: prefix (retrieval joins and the
        # manifest depend on it) but get a page_start-salted hash, so the
        # same (i, text) pair in two ranges cannot produce the same doc_id.
        self.assertEqual(len(ranged_ids), 1)
        self.assertTrue(ranged_ids[0].startswith("textbook.pdf::"))
        self.assertNotEqual(ranged_ids, plain_ids)

    def test_plain_chunk_ids_match_legacy_hash_exactly(self):
        """Recompute the unsalted hash by hand for a single-chunk document.

        This is the strong no-reindex pin: for a document without
        page_start metadata, the doc_id must equal the pre-change formula
        sha1(f"{i}\\n{chunk_text}")[:16] for the text the splitter emits.
        """
        import hashlib
        from llama_index.core import Document as LlamaDocument

        text = "Short single-chunk document."
        plain = LlamaDocument(text=text, metadata={
            "file_path": str(self.pdf_path), "source": "textbook.pdf",
            "extension": ".pdf",
        })
        chunks = list(
            self.manager._chunk_raw_documents([plain], str(self.vault_root))
        )
        self.assertEqual(len(chunks), 1)
        expected = hashlib.sha1(
            f"0\n{chunks[0].text}".encode(), usedforsecurity=False
        ).hexdigest()[:16]
        self.assertEqual(chunks[0].doc_id, f"textbook.pdf::{expected}")

    def test_read_pdf_text_stitches_range_caches(self):
        self._run_loader(
            2500,
            lambda *a, **kw: self._fake_sections(
                f"pages {kw['start_page']}-{kw['end_page']}"
            ),
        )
        with patch("rag.vault.get_pdf_page_count", return_value=2500):
            with patch.object(
                self.manager, "_pdf_file_signature", return_value=self.signature
            ):
                text, truncated = self.manager._read_pdf_text(
                    self.pdf_path, self.vault_root, char_budget=32000
                )
        self.assertEqual(text, "pages 0-1000\n\npages 1000-2000\n\npages 2000-2500")
        self.assertFalse(truncated)

    def test_read_pdf_text_gap_in_ranges_reports_truncated(self):
        """A missing middle range must not pass as full coverage.

        Only checking the maximum cached end page would let caches for
        pages 0-1000 and 2000-2500 stitch to truncated=False while
        silently omitting pages 1000-2000.
        """
        self._run_loader(
            2500,
            lambda *a, **kw: self._fake_sections(
                f"pages {kw['start_page']}-{kw['end_page']}"
            ),
        )
        cache_dir = Path(self.cache_dir)
        middle = list(cache_dir.rglob("*-p01000-02000.txt"))
        self.assertEqual(len(middle), 1)
        middle[0].unlink()
        with patch("rag.vault.get_pdf_page_count", return_value=2500):
            with patch.object(
                self.manager, "_pdf_file_signature", return_value=self.signature
            ):
                text, truncated = self.manager._read_pdf_text(
                    self.pdf_path, self.vault_root, char_budget=32000
                )
        self.assertEqual(text, "pages 0-1000\n\npages 2000-2500")
        self.assertTrue(truncated)

    def test_read_pdf_text_partial_ranges_report_truncated(self):
        def stop_after_first(*a, **kw):
            self.manager._stop_event.set()
            return self._fake_sections("first range only")

        self._run_loader(2500, stop_after_first)
        with patch("rag.vault.get_pdf_page_count", return_value=2500):
            with patch.object(
                self.manager, "_pdf_file_signature", return_value=self.signature
            ):
                text, truncated = self.manager._read_pdf_text(
                    self.pdf_path, self.vault_root, char_budget=32000
                )
        self.assertEqual(text, "first range only")
        self.assertTrue(truncated)


class TestChatEmbedMismatchGuard(unittest.TestCase):
    """At chat time, retrieval must embed the query with the INDEX's recorded
    model, not whatever config says — a mismatch otherwise fuses two vector
    spaces and silently wrecks retrieval (only the UI warning guarded it before).
    """

    def setUp(self):
        from rag.vault import ObsidianVaultManager
        self.manager = ObsidianVaultManager()

    def test_mismatch_uses_index_model_and_warns(self):
        msgs = []
        with patch.object(
            self.manager, "_read_index_meta",
            return_value={"embed": "nomic-embed-text"},
        ):
            eff = self.manager._effective_embed_name("embeddinggemma:300m", msgs.append)
        self.assertEqual(eff, "nomic-embed-text")
        self.assertTrue(any("nomic-embed-text" in m for m in msgs), msgs)

    def test_match_passes_through_without_warning(self):
        msgs = []
        with patch.object(
            self.manager, "_read_index_meta", return_value={"embed": "x:1"},
        ):
            eff = self.manager._effective_embed_name("x:1", msgs.append)
        self.assertEqual(eff, "x:1")
        self.assertEqual(msgs, [])

    def test_missing_meta_falls_back_to_requested(self):
        msgs = []
        with patch.object(self.manager, "_read_index_meta", return_value=None):
            eff = self.manager._effective_embed_name("x:1", msgs.append)
        self.assertEqual(eff, "x:1")
        self.assertEqual(msgs, [])


class TestIndexMetaCache(unittest.TestCase):
    """Regression tests for the stat-keyed obsidian_meta.json read cache.

    One status poll used to open+parse the meta file three times
    (get_status, is_partial_index, get_index_warning); _read_index_meta
    coalesces them behind a (st_size, st_mtime_ns) cache.  The cache must
    be invisible: every rewrite of the file must be observed on the next
    read, and a cache hit must not re-open the file at all.
    """

    def setUp(self):
        from rag.vault import ObsidianVaultManager
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index_dir_patch = patch("rag.vault.OBSIDIAN_INDEX_DIR", self.tmp.name)
        self.index_dir_patch.start()
        self.addCleanup(self.index_dir_patch.stop)
        self.manager = ObsidianVaultManager()
        self.meta_path = Path(self.tmp.name) / "obsidian_meta.json"

    def _write_meta(self, payload: dict) -> None:
        self.meta_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_meta_returns_none(self):
        self.assertIsNone(self.manager._read_index_meta())
        self.assertFalse(self.manager.is_partial_index())

    def test_reads_and_caches(self):
        self._write_meta({"partial": True, "embed": "m1"})
        meta1 = self.manager._read_index_meta()
        self.assertEqual(meta1, {"partial": True, "embed": "m1"})
        self.assertTrue(self.manager.is_partial_index())
        # Cache hit must not re-parse: if json.load runs again the stat key
        # failed to coalesce the second read.
        with patch("rag.vault.json.load", side_effect=AssertionError("re-parsed on cache hit")):
            meta2 = self.manager._read_index_meta()
        self.assertIs(meta2, meta1)

    def test_rewrite_is_picked_up(self):
        self._write_meta({"partial": True})
        self.assertTrue(self.manager.is_partial_index())
        self._write_meta({"partial": False, "padding": "x"})
        self.assertFalse(self.manager.is_partial_index())

    def test_write_index_meta_invalidates(self):
        self._write_meta({"partial": True})
        self.assertTrue(self.manager.is_partial_index())
        self.manager._write_index_meta(
            str(self.meta_path), "embed-model", "ollama",
            partial=False, phase="done", has_vector_data=True,
            inserted_this_run=3,
        )
        self.assertFalse(self.manager.is_partial_index())
        meta = self.manager._read_index_meta()
        self.assertEqual(meta.get("embed"), "embed-model")

    def test_corrupt_meta_returns_none_and_recovers(self):
        self.meta_path.write_text('{"partial": tru', encoding="utf-8")
        self.assertIsNone(self.manager._read_index_meta())
        self._write_meta({"partial": True})
        self.assertTrue(self.manager.is_partial_index())


def _wl_node(source, text="", node_id="n", extension=".md"):
    """Minimal stand-in for a llama-index TextNode for graph-builder tests."""
    from types import SimpleNamespace

    return SimpleNamespace(
        metadata={"source": source, "extension": extension},
        text=text,
        node_id=node_id,
    )


class _FakeWikilinkDocstore:
    def __init__(self, docs):
        self.docs = docs


class _FakeWikilinkIndex:
    def __init__(self, docs):
        self.docstore = _FakeWikilinkDocstore(docs)


class TestWikilinkGraph(unittest.TestCase):
    """Phase 1: the query-time note→note wikilink graph builder.

    Pure in-memory tests over fake docstore nodes — no filesystem, no
    embeddings, no index mutation — so nothing here can trigger a reindex
    or re-embed.  Resolution mirrors Obsidian's shortest-path link semantics
    against the in-memory set of indexed note sources.
    """

    def _build(self, nodes):
        from rag.vault import ObsidianVaultManager

        return ObsidianVaultManager()._build_wikilink_graph(nodes)

    def test_outbound_and_backlinks_both_directions(self):
        nodes = [
            _wl_node("a.md", "links to [[b]]", "a1"),
            _wl_node("b.md", "links to [[c]]", "b1"),
            _wl_node("c.md", "leaf note", "c1"),
        ]
        g = self._build(nodes)
        self.assertEqual(g.outbound("a.md"), ["b.md"])
        self.assertEqual(g.backlinks("b.md"), ["a.md"])
        # neighbours unions both directions for the middle note.
        self.assertEqual(g.neighbors("b.md"), ["a.md", "c.md"])
        self.assertEqual(g.edge_count, 2)

    def test_bare_link_resolves_central_folder(self):
        # Parent-relative notes/concept.md is not indexed; the shortest-path
        # basename lookup finds refs/concept.md anywhere in the vault.
        nodes = [
            _wl_node("notes/paper.md", "see [[concept]]", "p1"),
            _wl_node("refs/concept.md", "the concept", "c1"),
        ]
        g = self._build(nodes)
        self.assertEqual(g.outbound("notes/paper.md"), ["refs/concept.md"])
        self.assertEqual(g.backlinks("refs/concept.md"), ["notes/paper.md"])

    def test_local_copy_wins_over_central(self):
        # When a note beside the linker matches, parent-relative resolution
        # (step 1) wins outright over a vault-wide basename match (step 2).
        nodes = [
            _wl_node("notes/paper.md", "see [[concept]]", "p1"),
            _wl_node("notes/concept.md", "beside", "n1"),
            _wl_node("refs/concept.md", "central", "c1"),
        ]
        g = self._build(nodes)
        self.assertEqual(g.outbound("notes/paper.md"), ["notes/concept.md"])

    def test_ambiguous_basename_locality_tiebreak(self):
        # Neither candidate is beside the linker, so step 2 picks by longest
        # shared directory prefix with the linking note (a/x → a/...).
        nodes = [
            _wl_node("a/x/paper.md", "see [[concept]]", "p1"),
            _wl_node("a/concept.md", "near", "a1"),
            _wl_node("z/concept.md", "far", "z1"),
        ]
        g = self._build(nodes)
        self.assertEqual(g.outbound("a/x/paper.md"), ["a/concept.md"])

    def test_partial_path_link_not_shortest_pathed(self):
        # Documented limitation (mirrors the b11dc65 image resolver): a link
        # carrying a directory component is resolved parent-relative ONLY.
        nodes = [
            _wl_node("notes/paper.md", "see [[sub/concept]]", "p1"),
            _wl_node("refs/concept.md", "the concept", "c1"),
        ]
        g = self._build(nodes)
        self.assertEqual(g.outbound("notes/paper.md"), [])
        self.assertEqual(g.edge_count, 0)

    def test_self_link_ignored(self):
        g = self._build([_wl_node("a.md", "I link to [[a]] myself", "a1")])
        self.assertEqual(g.outbound("a.md"), [])
        self.assertEqual(g.edge_count, 0)

    def test_non_note_targets_excluded(self):
        nodes = [
            _wl_node(
                "a.md",
                "img ![[fig.png]] pdf [study](papers/study.pdf) "
                "ext [docs](https://example.com)",
                "a1",
            ),
        ]
        g = self._build(nodes)
        self.assertEqual(g.outbound("a.md"), [])
        self.assertEqual(g.edge_count, 0)

    def test_inline_markdown_link_to_note_resolved(self):
        nodes = [
            _wl_node("a.md", "see [the other](other.md) note", "a1"),
            _wl_node("other.md", "other", "o1"),
        ]
        g = self._build(nodes)
        self.assertEqual(g.outbound("a.md"), ["other.md"])

    def test_embed_transclusion_counts_as_link(self):
        # ![[note]] transcludes another note — a real relationship.
        nodes = [
            _wl_node("a.md", "embed ![[b]] here", "a1"),
            _wl_node("b.md", "b", "b1"),
        ]
        g = self._build(nodes)
        self.assertEqual(g.outbound("a.md"), ["b.md"])

    def test_alias_and_anchor_stripped(self):
        nodes = [
            _wl_node("a.md", "[[target|nice label]] and [[target#section]]", "a1"),
            _wl_node("target.md", "t", "t1"),
        ]
        g = self._build(nodes)
        self.assertEqual(g.outbound("a.md"), ["target.md"])

    def test_multichunk_note_unions_links_and_node_ids(self):
        nodes = [
            _wl_node("a.md", "chunk one links [[b]]", "a1"),
            _wl_node("a.md", "chunk two links [[c]]", "a2"),
            _wl_node("b.md", "b", "b1"),
            _wl_node("c.md", "c", "c1"),
        ]
        g = self._build(nodes)
        self.assertEqual(g.outbound("a.md"), ["b.md", "c.md"])
        self.assertEqual(sorted(g.node_ids_for("a.md")), ["a1", "a2"])

    def test_pdf_nodes_are_not_graph_nodes(self):
        nodes = [
            _wl_node("a.md", "see [[b]]", "a1"),
            _wl_node("b.md", "b", "b1"),
            _wl_node("paper.pdf", "pdf text [[a]]", "pdf1", extension=".pdf"),
        ]
        g = self._build(nodes)
        self.assertEqual(g.note_count, 2)  # the PDF is excluded
        self.assertEqual(g.node_ids_for("paper.pdf"), [])
        # the PDF's text is never scanned, so it creates no backlink into a.md.
        self.assertEqual(g.backlinks("a.md"), [])

    def test_unknown_note_queries_return_empty(self):
        g = self._build([_wl_node("a.md", "x", "a1")])
        self.assertEqual(g.neighbors("missing.md"), [])
        self.assertEqual(g.node_ids_for("missing.md"), [])
        self.assertEqual(g.outbound("missing.md"), [])

    def test_get_wikilink_index_none_without_index(self):
        from rag.vault import ObsidianVaultManager

        m = ObsidianVaultManager()
        m._index = None
        self.assertIsNone(m._get_wikilink_index())

    def test_get_wikilink_index_caches_by_docstore_size(self):
        from rag.vault import ObsidianVaultManager

        m = ObsidianVaultManager()
        docs = {
            "a1": _wl_node("a.md", "see [[b]]", "a1"),
            "b1": _wl_node("b.md", "b", "b1"),
        }
        m._index = _FakeWikilinkIndex(docs)
        g1 = m._get_wikilink_index()
        g2 = m._get_wikilink_index()
        self.assertIs(g1, g2)  # cached by docstore size, not rebuilt
        self.assertEqual(g1.outbound("a.md"), ["b.md"])
        # A docstore size change forces a rebuild.
        docs["c1"] = _wl_node("c.md", "links [[a]]", "c1")
        g3 = m._get_wikilink_index()
        self.assertIsNot(g1, g3)
        self.assertEqual(g3.backlinks("a.md"), ["c.md"])

    def test_invalidate_retrieval_caches_drops_wikilink_graph(self):
        from rag.vault import ObsidianVaultManager

        m = ObsidianVaultManager()
        m._index = _FakeWikilinkIndex({"a1": _wl_node("a.md", "x", "a1")})
        self.assertIsNotNone(m._get_wikilink_index())
        m._invalidate_retrieval_caches()
        self.assertIsNone(m._wikilink_index)
        self.assertEqual(m._wikilink_cached_doc_count, -1)


class TestWikilinkSidecar(unittest.TestCase):
    """On-disk sidecar for the wikilink graph (BM25-sidecar sibling): a
    process-cold miss loads the persisted adjacency instead of re-sweeping
    the docstore; staleness is judged by doc_count + a TRUTHY indexed_at
    match (stricter than BM25 — a sidecar written without a real index meta
    proves nothing about the docstore state it came from)."""

    _STAMP = "2026-07-02T12:00:00+00:00"

    def setUp(self):
        # OBSIDIAN_INDEX_DIR is session-global; isolate the sidecar + index
        # meta per test (same discipline as TestVaultChatBM25Manager).
        self._index_dir = tempfile.TemporaryDirectory()
        patcher = patch("rag.vault.OBSIDIAN_INDEX_DIR", self._index_dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._index_dir.cleanup)

    def _write_meta(self, indexed_at=_STAMP):
        meta_path = Path(self._index_dir.name, "obsidian_meta.json")
        meta_path.write_text(
            json.dumps({"indexed_at": indexed_at}), encoding="utf-8"
        )

    def _manager(self, docs):
        from rag.vault import ObsidianVaultManager

        m = ObsidianVaultManager()
        m._index = _FakeWikilinkIndex(docs)
        return m

    def _docs(self):
        return {
            "a1": _wl_node("a.md", "see [[b]]", "a1"),
            "b1": _wl_node("b.md", "links [[c]]", "b1"),
            "c1": _wl_node("c.md", "leaf", "c1"),
        }

    def _sidecar_path(self) -> Path:
        return Path(self._index_dir.name, "wikilink_sidecar.json")

    def test_sidecar_round_trip_no_rebuild(self):
        """build → persist → load on a fresh manager: identical graph, and
        the docstore sweep must never run on the loaded path."""
        self._write_meta()
        built = self._manager(self._docs())._get_wikilink_index()
        self.assertIsNotNone(built)
        self.assertTrue(self._sidecar_path().exists())

        cold = self._manager(self._docs())
        with patch.object(
            cold, "_build_wikilink_graph",
            side_effect=AssertionError("expected sidecar load, got rebuild"),
        ):
            loaded = cold._get_wikilink_index()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.outbound("a.md"), built.outbound("a.md"))
        self.assertEqual(loaded.backlinks("b.md"), built.backlinks("b.md"))
        self.assertEqual(loaded.neighbors("b.md"), built.neighbors("b.md"))
        self.assertEqual(loaded.node_ids_for("a.md"), built.node_ids_for("a.md"))
        self.assertEqual(loaded.note_count, built.note_count)
        self.assertEqual(loaded.edge_count, built.edge_count)

    def test_sidecar_stale_doc_count_rebuilds_and_repersists(self):
        self._write_meta()
        self.assertIsNotNone(self._manager(self._docs())._get_wikilink_index())
        payload = json.loads(self._sidecar_path().read_text())
        self.assertEqual(payload["doc_count"], 3)

        grown = self._docs()
        grown["d1"] = _wl_node("d.md", "links [[a]]", "d1")
        g = self._manager(grown)._get_wikilink_index()
        self.assertIsNotNone(g)
        self.assertEqual(g.backlinks("a.md"), ["d.md"])
        payload = json.loads(self._sidecar_path().read_text())
        self.assertEqual(payload["doc_count"], 4)

    def test_sidecar_stale_indexed_at_rebuilds(self):
        self._write_meta("2026-07-01T00:00:00+00:00")
        self.assertIsNotNone(self._manager(self._docs())._get_wikilink_index())
        # A new indexing run stamps a new indexed_at: the old sidecar must
        # be rejected even though the doc count still matches.
        self._write_meta("2026-07-02T00:00:00+00:00")
        cold = self._manager(self._docs())
        with patch.object(
            cold, "_build_wikilink_graph",
            wraps=cold._build_wikilink_graph,
        ) as build:
            self.assertIsNotNone(cold._get_wikilink_index())
        build.assert_called_once()
        payload = json.loads(self._sidecar_path().read_text())
        self.assertEqual(payload["indexed_at"], "2026-07-02T00:00:00+00:00")

    def test_sidecar_requires_truthy_indexed_at(self):
        """No index meta → nothing persisted; a hand-planted sidecar with a
        None stamp is never loaded (the deliberate tightening over BM25)."""
        built = self._manager(self._docs())._get_wikilink_index()
        self.assertIsNotNone(built)
        self.assertFalse(self._sidecar_path().exists())

        graph_payload = built.to_payload()
        graph_payload.update({"doc_count": 3, "indexed_at": None})
        self._sidecar_path().write_text(
            json.dumps(graph_payload), encoding="utf-8"
        )
        cold = self._manager(self._docs())
        with patch.object(
            cold, "_build_wikilink_graph",
            wraps=cold._build_wikilink_graph,
        ) as build:
            self.assertIsNotNone(cold._get_wikilink_index())
        build.assert_called_once()

    def test_sidecar_not_persisted_while_indexing(self):
        self._write_meta()
        m = self._manager(self._docs())
        with m._status_lock:
            m._index_state = "embedding"
        self.assertIsNotNone(m._get_wikilink_index())
        self.assertFalse(self._sidecar_path().exists())

    def test_sidecar_corrupt_json_rebuilds(self):
        self._write_meta()
        self._sidecar_path().write_text("{not json", encoding="utf-8")
        g = self._manager(self._docs())._get_wikilink_index()
        self.assertIsNotNone(g)
        self.assertEqual(g.outbound("a.md"), ["b.md"])
        # The rebuild replaced the corrupt file with a valid one.
        payload = json.loads(self._sidecar_path().read_text())
        self.assertEqual(payload["doc_count"], 3)

    def test_invalidate_retrieval_caches_removes_wikilink_sidecar(self):
        from rag.vault import ObsidianVaultManager

        self._sidecar_path().write_text("{}", encoding="utf-8")
        ObsidianVaultManager()._invalidate_retrieval_caches()
        self.assertFalse(self._sidecar_path().exists())


def _tn(source, text, node_id):
    """Real TextNode so NodeWithScore / docstore lookups behave like prod."""
    from llama_index.core.schema import TextNode

    return TextNode(
        text=text, id_=node_id, metadata={"source": source, "extension": ".md"}
    )


def _seed(node, score):
    from llama_index.core.schema import NodeWithScore

    return NodeWithScore(node=node, score=score)


class _StubInnerRetriever:
    """Returns a fixed seed list (the wrapper only needs ``.retrieve``)."""

    def __init__(self, seeds):
        self._seeds = seeds

    def retrieve(self, query_bundle):
        return list(self._seeds)


class _FakeExpansionDocstore:
    def __init__(self, nodes):
        self._by_id = {n.node_id: n for n in nodes}

    def get_node(self, node_id, raise_error=True):
        node = self._by_id.get(node_id)
        if node is None and raise_error:
            raise ValueError(node_id)
        return node


class TestWikilinkExpansionRetriever(unittest.TestCase):
    """Phase 2: the rerank-gated wikilink expansion retriever.

    Drives ``_WikilinkExpansionRetriever._retrieve`` directly over a real
    ``_WikilinkGraph`` + a fake docstore, so the tests exercise the
    seed→neighbour expansion without any vector store or LLM.
    """

    def _graph(self, nodes):
        from rag.vault import ObsidianVaultManager

        return ObsidianVaultManager()._build_wikilink_graph(nodes)

    def _run(self, seeds, nodes, **kwargs):
        from rag.engine import _WikilinkExpansionRetriever
        from llama_index.core.schema import QueryBundle

        graph = self._graph(nodes)
        docstore = _FakeExpansionDocstore(nodes)
        wrapper = _WikilinkExpansionRetriever(
            _StubInnerRetriever(seeds), graph, docstore, **kwargs
        )
        return wrapper._retrieve(QueryBundle("q"))

    def _ids(self, results):
        return [nws.node.node_id for nws in results]

    def test_pulls_neighbors_both_directions(self):
        a = _tn("a.md", "see [[b]]", "a1")     # a -> b (outbound)
        b = _tn("b.md", "leaf", "b1")
        c = _tn("c.md", "ref [[a]]", "c1")     # c -> a  (=> a backlink)
        out = self._run([_seed(a, 1.0)], [a, b, c])
        # seed a, plus its outbound (b) and its backlink (c).
        self.assertEqual(self._ids(out), ["a1", "b1", "c1"])

    def test_decay_scoring_and_none_seed_score(self):
        a = _tn("a.md", "see [[b]]", "a1")
        b = _tn("b.md", "leaf", "b1")
        out = self._run([_seed(a, 1.0)], [a, b], score_decay=0.25)
        self.assertEqual(out[1].node.node_id, "b1")
        self.assertAlmostEqual(out[1].score, 0.25)
        # A seed with no score contributes neighbours at score 0.0.
        out2 = self._run([_seed(a, None)], [a, b], score_decay=0.5)
        self.assertAlmostEqual(out2[1].score, 0.0)

    def test_respects_node_cap(self):
        a = _tn("a.md", "see [[b]] and [[c]]", "a1")
        b = _tn("b.md", "b", "b1")
        c = _tn("c.md", "c", "c1")
        out = self._run([_seed(a, 1.0)], [a, b, c], neighbor_node_cap=1)
        self.assertEqual(self._ids(out), ["a1", "b1"])  # only one neighbour chunk

    def test_respects_note_cap(self):
        a = _tn("a.md", "see [[b]] and [[c]]", "a1")
        b1 = _tn("b.md", "b chunk one", "b1")
        b2 = _tn("b.md", "b chunk two", "b2")
        c = _tn("c.md", "c", "c1")
        out = self._run(
            [_seed(a, 1.0)], [a, b1, b2, c], neighbor_note_cap=1, neighbor_node_cap=24
        )
        # Only the first neighbour NOTE (b.md) expands — both its chunks — and
        # c.md is never reached.
        self.assertEqual(set(self._ids(out)), {"a1", "b1", "b2"})

    def test_skips_seed_notes(self):
        a = _tn("a.md", "see [[b]]", "a1")     # a -> b
        b = _tn("b.md", "leaf", "b1")
        c = _tn("c.md", "ref [[a]]", "c1")     # c -> a
        # Both a and b are already seeds; only the non-seed neighbour c is added.
        out = self._run([_seed(a, 1.0), _seed(b, 0.9)], [a, b, c])
        self.assertEqual(self._ids(out), ["a1", "b1", "c1"])

    def test_dedups_shared_neighbor(self):
        a = _tn("a.md", "see [[d]]", "a1")
        b = _tn("b.md", "see [[d]]", "b1")
        d = _tn("d.md", "shared", "d1")
        out = self._run([_seed(a, 1.0), _seed(b, 0.9)], [a, b, d])
        # d is a neighbour of both seeds but is appended exactly once.
        self.assertEqual(self._ids(out), ["a1", "b1", "d1"])

    def test_noop_when_no_neighbors(self):
        a = _tn("a.md", "lonely note", "a1")
        seeds = [_seed(a, 1.0)]
        out = self._run(seeds, [a])
        self.assertEqual(self._ids(out), ["a1"])

    def test_missing_docstore_node_is_skipped(self):
        from rag.engine import _WikilinkExpansionRetriever
        from llama_index.core.schema import QueryBundle

        a = _tn("a.md", "see [[b]]", "a1")
        b = _tn("b.md", "leaf", "b1")
        graph = self._graph([a, b])
        # Docstore is missing b1 entirely; expansion must degrade gracefully.
        docstore = _FakeExpansionDocstore([a])
        wrapper = _WikilinkExpansionRetriever(
            _StubInnerRetriever([_seed(a, 1.0)]), graph, docstore
        )
        out = wrapper._retrieve(QueryBundle("q"))
        self.assertEqual([nws.node.node_id for nws in out], ["a1"])

    def test_zero_cap_returns_seeds_unchanged(self):
        a = _tn("a.md", "see [[b]]", "a1")
        b = _tn("b.md", "leaf", "b1")
        out = self._run([_seed(a, 1.0)], [a, b], neighbor_node_cap=0)
        self.assertEqual(self._ids(out), ["a1"])

    def test_pipeline_wraps_retriever_only_when_enabled(self):
        """``_build_retrieval_pipeline`` wraps the retriever in the expansion
        retriever only when the knob is on, a graph is present, AND a reranker
        is active (rerank-gated, F1) — otherwise the retriever is the unwrapped
        dense retriever, byte-identical to the pre-expansion pipeline."""
        from types import SimpleNamespace
        from rag.engine import SimpleQueryEngine, _WikilinkExpansionRetriever

        graph = self._graph([_tn("a.md", "see [[b]]", "a1"), _tn("b.md", "b", "b1")])
        index = SimpleNamespace(docstore=object(), vector_store=None)
        cfg = {"context_window": 8192}

        def build(*, expansion, with_graph, with_reranker):
            with patch("rag.engine.get_provider"), patch(
                "rag.engine.VectorIndexRetriever"
            ) as dense_cls:
                dense_cls.return_value = MagicMock(name="dense_retriever")
                engine = SimpleQueryEngine(
                    index=index,
                    llm_name="l",
                    embed_name="e",
                    provider_name="ollama",
                    # A stub reranker: _build_retrieval_pipeline only sets
                    # .top_n and appends it as a postprocessor.
                    reranker=SimpleNamespace(top_n=0) if with_reranker else None,
                    wikilink_expansion=expansion,
                    wikilink_graph=graph if with_graph else None,
                )
                retriever, _post, _llm = engine._build_retrieval_pipeline(cfg)
                return retriever

        # Off → unwrapped (no behaviour change).
        self.assertNotIsInstance(
            build(expansion=False, with_graph=True, with_reranker=True),
            _WikilinkExpansionRetriever,
        )
        # On but no graph built → unwrapped (safe no-op).
        self.assertNotIsInstance(
            build(expansion=True, with_graph=False, with_reranker=True),
            _WikilinkExpansionRetriever,
        )
        # On with a graph but NO reranker → unwrapped (F1: without a reranker
        # to trim seeds+neighbours back to top_k, expansion would bloat the
        # LLM context, so the wrap is skipped).
        self.assertNotIsInstance(
            build(expansion=True, with_graph=True, with_reranker=False),
            _WikilinkExpansionRetriever,
        )
        # On with a graph AND a reranker → wrapped.
        self.assertIsInstance(
            build(expansion=True, with_graph=True, with_reranker=True),
            _WikilinkExpansionRetriever,
        )


class TestIndexBackupPrune(unittest.TestCase):
    """R1: ``_archive_old_index_dir`` archives the whole prior index to a
    timestamped ``.bak`` sibling on every version bump; ``_prune_old_index_backups``
    must bound how many accumulate so they don't grow unbounded on disk."""

    def test_keeps_newest_and_prunes_older_with_audit(self):
        import os
        from rag.vault import obsidian_manager

        with tempfile.TemporaryDirectory() as tmp:
            index_dir = os.path.join(tmp, "obsidian_storage")
            os.makedirs(index_dir)  # the live dir; must be left untouched
            # Five archived siblings with strictly increasing mtimes.
            baks = []
            for i in range(5):
                d = f"{index_dir}.bak.v{i}.2026010{i}-000000"
                os.makedirs(d)
                (Path(d) / "docstore.json").write_text("{}", encoding="utf-8")
                os.utime(d, (1_700_000_000 + i, 1_700_000_000 + i))
                baks.append(d)
            # An unrelated sibling that must NOT be touched.
            other = os.path.join(tmp, "obsidian_storage_notes")
            os.makedirs(other)

            with patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir), \
                 patch("rag.vault.log_storage_deletion") as mock_log:
                obsidian_manager._prune_old_index_backups(keep=2)

            survivors = [d for d in baks if os.path.isdir(d)]
            self.assertEqual(sorted(survivors), sorted(baks[3:]))  # two newest kept
            for removed in baks[:3]:
                self.assertFalse(os.path.isdir(removed))
            self.assertTrue(os.path.isdir(index_dir))   # live dir untouched
            self.assertTrue(os.path.isdir(other))       # unrelated sibling untouched
            self.assertEqual(mock_log.call_count, 3)    # one audit line per removal

    def test_noop_when_within_keep(self):
        import os
        from rag.vault import obsidian_manager

        with tempfile.TemporaryDirectory() as tmp:
            index_dir = os.path.join(tmp, "obsidian_storage")
            os.makedirs(index_dir)
            d = f"{index_dir}.bak.v0.20260101-000000"
            os.makedirs(d)
            with patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir), \
                 patch("rag.vault.log_storage_deletion") as mock_log:
                obsidian_manager._prune_old_index_backups(keep=2)
            self.assertTrue(os.path.isdir(d))
            mock_log.assert_not_called()


class TestThesaurusExpansionRetriever(unittest.TestCase):
    """The query-time _ThesaurusExpansionRetriever: union seeds + synonym
    variants, dedup by node id keeping max score, cap to pool_size, and
    degrade to seeds-untouched when disabled / no match (the default-off path
    that keeps the pipeline byte-identical)."""

    def _setup(self):
        import hashlib
        from rag.thesaurus import Thesaurus
        from rag.engine import _ThesaurusExpansionRetriever
        from llama_index.core.schema import TextNode, NodeWithScore

        abbr = (
            "| Abréviation | Signification | Notes |\n"
            "| - | - | - |\n"
            "| `dep` | Dépression / Épisode Dépressif Caractérisé | [[dep]] |\n"
        )
        thes = Thesaurus.from_files(abbr, "")

        class _Inner:
            # Distinct node per distinct query; the "dep" variant scores higher
            # so we can assert the merged pool is re-sorted by score.
            def retrieve(self, q):
                qs = q.query_str if hasattr(q, "query_str") else q
                nid = "n_" + hashlib.sha1(qs.encode()).hexdigest()[:8]
                score = 0.6 if " dep" in qs else 0.3
                return [NodeWithScore(node=TextNode(id_=nid, text=qs), score=score)]

        return _ThesaurusExpansionRetriever, thes, _Inner

    def test_unions_variants_and_sorts_by_score(self):
        from llama_index.core.schema import QueryBundle
        Retr, thes, Inner = self._setup()
        r = Retr(Inner(), thes, max_variants=3, pool_size=5)
        res = r._retrieve(QueryBundle("facteurs dépression"))
        self.assertGreaterEqual(len(res), 2)            # seeds + variants
        self.assertEqual(res[0].score, 0.6)             # best-scoring variant first

    def test_no_match_returns_seeds_untouched(self):
        from llama_index.core.schema import QueryBundle
        Retr, thes, Inner = self._setup()
        r = Retr(Inner(), thes, max_variants=3, pool_size=5)
        self.assertEqual(len(r._retrieve(QueryBundle("quantum chromodynamics"))), 1)

    def test_disabled_returns_seeds_untouched(self):
        from llama_index.core.schema import QueryBundle
        Retr, thes, Inner = self._setup()
        r = Retr(Inner(), thes, max_variants=0, pool_size=5)
        self.assertEqual(len(r._retrieve(QueryBundle("facteurs dépression"))), 1)

    def test_pool_size_caps_merged_results(self):
        from llama_index.core.schema import QueryBundle
        Retr, thes, Inner = self._setup()
        r = Retr(Inner(), thes, max_variants=3, pool_size=2)
        self.assertEqual(len(r._retrieve(QueryBundle("facteurs dépression"))), 2)

    def test_variant_failure_never_breaks_seeds(self):
        from llama_index.core.schema import QueryBundle, TextNode, NodeWithScore
        Retr, thes, _ = self._setup()

        class _Flaky:
            def __init__(self):
                self.calls = 0

            def retrieve(self, q):
                self.calls += 1
                if self.calls == 1:  # seed query succeeds
                    return [NodeWithScore(node=TextNode(id_="seed", text="x"), score=0.4)]
                raise RuntimeError("variant retrieval boom")

        r = Retr(_Flaky(), thes, max_variants=3, pool_size=5)
        res = r._retrieve(QueryBundle("facteurs dépression"))
        self.assertEqual([n.node.node_id for n in res], ["seed"])  # seeds survive


class TestVaultPrimerInjection(unittest.TestCase):
    """The #2 system-prompt primer helpers: app-controlled glossary above the
    safety preamble (local) / native system field (online), brace-safe, and a
    no-op on the default (empty) path so the prompt-cache key does not shift."""

    def test_local_primer_above_prefix_preserves_slots_and_brace_safe(self):
        from rag.engine import (
            _apply_vault_primer, _apply_custom_prefix, RAG_QA_PROMPT_STRICT,
        )
        primer = "GLOSSARY:\nEI = Effet(s) {Indésirable}"  # contains a brace
        qa = _apply_custom_prefix(
            _apply_vault_primer(RAG_QA_PROMPT_STRICT, primer), "Be terse"
        )
        txt = qa.template
        self.assertIn("GLOSSARY", txt)
        self.assertIn("USER INSTRUCTIONS", txt)
        self.assertIn("{context_str}", txt)
        self.assertIn("{query_str}", txt)
        # brace in the primer must not break str.format rendering
        out = qa.format(context_str="CTX", query_str="Q?")
        self.assertIn("Indésirable", out)
        self.assertIn("CTX", out)

    def test_empty_primer_returns_template_unchanged(self):
        from rag.engine import _apply_vault_primer, RAG_QA_PROMPT_STRICT
        self.assertIs(_apply_vault_primer(RAG_QA_PROMPT_STRICT, ""), RAG_QA_PROMPT_STRICT)

    def test_online_combine_system_prompt(self):
        from rag.engine import _combine_system_prompt
        self.assertEqual(_combine_system_prompt("PRIMER", "USERP"), "PRIMER\n\nUSERP")
        self.assertEqual(_combine_system_prompt("", "USERP"), "USERP")
        self.assertEqual(_combine_system_prompt("PRIMER", ""), "PRIMER")
        self.assertEqual(_combine_system_prompt("", ""), "")

    def test_build_primer_disabled_returns_empty(self):
        # _build_primer is a no-op unless primer_enabled AND a thesaurus exist.
        from rag.engine import SimpleQueryEngine
        eng = SimpleQueryEngine.__new__(SimpleQueryEngine)
        eng.primer_enabled = False
        eng.thesaurus = object()
        eng.primer_max_chars = 1500
        self.assertEqual(eng._build_primer("q", {}), "")
        eng.primer_enabled = True
        eng.thesaurus = None
        self.assertEqual(eng._build_primer("q", {}), "")


if __name__ == "__main__":
    unittest.main()


class TestLocalHostResolution(unittest.TestCase):
    """Ollama / LM Studio host: config → env → constant, and probe/generation parity.

    Closes the split-brain where /api/status probed OLLAMA_HOST while generation
    used the hardcoded constant (green badge, dead generation on a custom host).
    """

    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k) for k in ("OLLAMA_HOST", "LM_STUDIO_HOST")
        }
        for k in ("OLLAMA_HOST", "LM_STUDIO_HOST"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_resolution_precedence_config_then_env_then_constant(self):
        from core.constants import OLLAMA_HOST
        from core.providers.base import resolve_ollama_host

        # constant when nothing set
        with patch("core.config.load_config", return_value={}):
            self.assertEqual(resolve_ollama_host(), OLLAMA_HOST)
            # env wins over constant; scheme-less is normalised
            os.environ["OLLAMA_HOST"] = "10.0.0.5:11434"
            self.assertEqual(resolve_ollama_host(), "http://10.0.0.5:11434")
        # config wins over env, trailing slash stripped
        with patch("core.config.load_config",
                   return_value={"ollama_host": "http://box.local:11434/"}):
            self.assertEqual(resolve_ollama_host(), "http://box.local:11434")

    def test_check_running_uses_resolved_host_not_module_level(self):
        """The reachability probe must dial OUR host (self._client), not ollama.list()."""
        import core.providers.ollama as omod

        seen = {}

        class _FakeClient:
            def list(self):
                return MagicMock(models=[])

        def _fake_ollama_client(host, timeout):
            seen["host"] = host
            return _FakeClient()

        with patch.object(omod, "_ollama_client", _fake_ollama_client):
            prov = omod.OllamaProvider(host="http://custom:9999")
            ok, _err = prov.check_running()
            self.assertTrue(ok)
            # Proves the probe is host-bound (the split-brain fix): it went through
            # _ollama_client with the provider's host, not the bare ollama.list().
            self.assertEqual(seen["host"], "http://custom:9999")

    def test_lm_studio_base_url_follows_resolved_host(self):
        from core.providers.lms import LMStudioProvider

        with patch("core.config.load_config",
                   return_value={"lm_studio_host": "http://127.0.0.1:4321"}):
            prov = LMStudioProvider()
            self.assertEqual(prov.host, "http://127.0.0.1:4321")
            self.assertEqual(prov.base_url, "http://127.0.0.1:4321/v1")


class TestFusionModeAndWeights(unittest.TestCase):
    """The vault_rrf_* knobs shipped dead: LlamaIndex ignores
    retriever_weights under mode="reciprocal_rerank" (only
    _relative_score_fusion reads them). The engine now switches fusion mode
    when — and only when — a non-default weight is configured, keeping the
    pinned RRF behaviour byte-identical on the default path."""

    def test_default_weights_keep_rrf_and_pass_no_weights(self):
        from rag.engine import _fusion_mode_and_weights
        mode, weights = _fusion_mode_and_weights({}, n_legs=2)
        self.assertEqual(mode, "reciprocal_rerank")
        self.assertIsNone(weights)

    def test_non_default_weight_switches_to_relative_score(self):
        from rag.engine import _fusion_mode_and_weights
        cfg = {"vault_rrf_dense_weight": 1.0, "vault_rrf_bm25_weight": 2.5}
        mode, weights = _fusion_mode_and_weights(cfg, n_legs=2)
        self.assertEqual(mode, "relative_score")
        self.assertEqual(weights, [1.0, 2.5])
        # Single-leg (expansion without BM25): only the dense weight applies.
        mode1, weights1 = _fusion_mode_and_weights(
            {"vault_rrf_dense_weight": 0.5}, n_legs=1)
        self.assertEqual(mode1, "relative_score")
        self.assertEqual(weights1, [0.5])

    def test_garbage_weight_degrades_to_neutral(self):
        from rag.engine import _fusion_mode_and_weights
        # Hand-edited config: non-numeric / out-of-range values must degrade
        # to 1.0 (=> default RRF), never crash the chat at query time.
        for bad in ("banana", None, -3, 99):
            mode, weights = _fusion_mode_and_weights(
                {"vault_rrf_dense_weight": bad, "vault_rrf_bm25_weight": bad},
                n_legs=2,
            )
            self.assertEqual(mode, "reciprocal_rerank", bad)
            self.assertIsNone(weights, bad)


class TestSSEFrameContract(unittest.TestCase):
    """The /api/obsidian/chat consumer must forward EVERY frame type documented
    in CLAUDE.md §SSE Contract (improvement plan 2026-07-04, item 1.3).

    The defect this pins: the worker enqueued the ``{"retrieval": [...]}`` frame
    (shipped Phase 5 B4) but the consumer's dispatch chain had no matching
    branch, so the documented frame was silently discarded and the "Retrieval
    Context" panel never rendered. The dispatch chain is a fall-through of
    per-key ``continue`` branches — a frame type without a branch vanishes with
    no error — so this test enumerates the full documented set end-to-end.
    """

    DOCUMENTED_FRAME_KEYS = {
        # single-shot RAG frames
        "token", "error", "info", "retrieval",
        # agent-mode frames
        "iteration", "thought", "tool_call", "tool_result",
    }

    HEADERS = {"X-Requested-With": "ChatEKLD"}

    @staticmethod
    def _frames(body: str) -> list:
        out = []
        for line in body.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                out.append(json.loads(line[len("data: "):]))
        return out

    def _post(self, client, payload):
        resp = client.post("/api/obsidian/chat", json=payload,
                           headers=self.HEADERS, buffered=True)
        self.assertEqual(resp.status_code, 200)
        return self._frames(resp.get_data(as_text=True))

    def test_retrieval_frame_is_forwarded_before_tokens(self):
        from app import app
        from rag.vault import obsidian_manager

        app.config["TESTING"] = True
        client = app.test_client()

        class FakeChunk:
            source = "notes/psy.md"
            score = 0.87
            metadata = {"is_image": False}

        class FakeResponse:
            used_chunks = [FakeChunk()]

            @property
            def response_gen(self):
                yield "hello"

        with (
            patch.object(obsidian_manager, "get_status", return_value="done"),
            patch.object(obsidian_manager, "stream_chat", return_value=FakeResponse()),
        ):
            frames = self._post(client, {"message": "hi"})

        retrieval_frames = [f for f in frames if "retrieval" in f]
        self.assertEqual(len(retrieval_frames), 1)
        self.assertEqual(
            retrieval_frames[0]["retrieval"],
            [{"source": "notes/psy.md", "score": 0.87, "is_image": False}],
        )
        # Contract: the retrieval frame precedes the token stream.
        kinds = ["retrieval" if "retrieval" in f else ("token" if "token" in f else None)
                 for f in frames]
        self.assertLess(kinds.index("retrieval"), kinds.index("token"))

    def test_consumer_forwards_every_documented_frame_type(self):
        """Union of frames across the three worker paths == the documented set.

        A new documented frame type added to a worker without a consumer branch
        makes this fail (the exact regression class of item 1.3).
        """
        from app import app
        from rag.vault import obsidian_manager

        app.config["TESTING"] = True
        client = app.test_client()
        seen = set()

        # (1) single-shot happy path: info (indexing banner) + retrieval + token
        class FakeChunk:
            source = "n.md"
            score = 0.5
            metadata = {}

        class FakeResponse:
            used_chunks = [FakeChunk()]

            @property
            def response_gen(self):
                yield "tok"

        with (
            patch.object(obsidian_manager, "get_status", return_value="running"),
            patch.object(obsidian_manager, "stream_chat", return_value=FakeResponse()),
        ):
            for f in self._post(client, {"message": "q"}):
                seen.update(k for k in f if k in self.DOCUMENTED_FRAME_KEYS)

        # (2) single-shot error path
        with (
            patch.object(obsidian_manager, "get_status", return_value="done"),
            patch.object(obsidian_manager, "stream_chat", side_effect=RuntimeError("boom")),
        ):
            for f in self._post(client, {"message": "q"}):
                seen.update(k for k in f if k in self.DOCUMENTED_FRAME_KEYS)

        # (3) agent path: every agent event type
        def _fake_loop(*, on_event, **_kwargs):
            from core.agent.protocol import (
                DoneEvent, InfoEvent, IterationEvent, ThoughtEvent,
                TokenEvent, ToolCallEvent, ToolResultEvent,
            )
            from core.agent.budget import UsageBudget
            from core.llm.types import ToolCall, ToolResult
            on_event(IterationEvent(index=1))
            on_event(ThoughtEvent(text="thinking"))
            on_event(ToolCallEvent(ToolCall(
                id="c1", name="vault_search",
                arguments={"q": "x"}, raw_arguments='{"q":"x"}')))
            on_event(ToolResultEvent(
                ToolResult(tool_call_id="c1", content="res", is_error=False),
                truncated=False))
            on_event(InfoEvent("note"))
            on_event(TokenEvent("agent answer"))
            on_event(DoneEvent())
            return UsageBudget()

        with (
            patch("api.routes.vault.run_agent_loop", side_effect=_fake_loop),
            patch.object(obsidian_manager, "stream_chat"),
            patch.object(obsidian_manager, "get_status", return_value="done"),
        ):
            for f in self._post(client, {"message": "q", "agent_enabled": True}):
                seen.update(k for k in f if k in self.DOCUMENTED_FRAME_KEYS)

        self.assertEqual(seen, self.DOCUMENTED_FRAME_KEYS)


class TestQueryEmbedBound(unittest.TestCase):
    """Item 2.1 (improvement plan 2026-07-04): the QUERY-path embed is bounded,
    the INDEXING-path embed stays deliberately unbounded.

    Defect pinned: retrieval embeds the user's query over HTTP while holding
    ``_index_mutation_lock``; with the indexing path's unbounded embed
    defaults, one wedged local call held that lock forever and stranded every
    subsequent chat worker (restart-only recovery).
    """

    def test_ollama_get_embedding_bounds_only_when_asked(self):
        from core.providers.ollama import OllamaProvider
        p = OllamaProvider()
        bounded = p.get_embedding("nomic-embed-text", request_timeout_s=30.0)
        # client_kwargs reach the underlying httpx client of ollama.Client.
        self.assertEqual(bounded._client._client.timeout.connect, 30.0)
        # Indexing parity: without the param the client keeps the library
        # default (no finite bound injected by us).
        unbounded = p.get_embedding("nomic-embed-text")
        self.assertNotEqual(unbounded._client._client.timeout.connect, 30.0)

    def test_lms_get_embedding_bounds_only_when_asked(self):
        from core.providers.lms import LMStudioProvider
        p = LMStudioProvider()
        bounded = p.get_embedding("some-embed", request_timeout_s=30.0)
        self.assertEqual(bounded.timeout, 30.0)
        self.assertEqual(bounded.max_retries, 0)   # one wedged call = one timeout
        unbounded = p.get_embedding("some-embed")
        self.assertNotEqual(unbounded.timeout, 30.0)

    def test_engine_query_path_passes_the_bound(self):
        """The retrieval pipeline constructs its embed model WITH the bound —
        the wiring that makes the provider hook actually protect the lock."""
        from rag import engine as engine_mod

        captured = {}

        class FakeProvider:
            def get_embedding(self, name, **kwargs):
                captured.update(kwargs)
                raise _StopBuild()   # abort construction right after capture

            def get_llm(self, name, **kwargs):
                return object()

        class _StopBuild(Exception):
            pass

        eng = engine_mod.SimpleQueryEngine.__new__(engine_mod.SimpleQueryEngine)
        eng.index = None
        eng.llm_name = "llm"
        eng.embed_name = "embed"
        eng.provider_name = "ollama"
        eng.temperature = None
        eng.top_k = 8
        eng.top_k_explicit = True
        eng.similarity_cutoff = 0.25
        eng.prompt_mode = "balanced"
        eng.custom_system_prompt = ""
        eng.mmr_enabled = False
        eng.mmr_lambda = None
        eng.query_expansion = False
        eng.num_queries = 1
        eng.rerank_pool_ceiling = None
        eng.bm25_retriever = None
        eng.reranker = None
        eng.wikilink_graph = None
        eng.wikilink_expansion = False
        eng.thesaurus = None
        eng.thesaurus_expansion = False
        eng.primer_enabled = False
        eng._provider = FakeProvider()
        with self.assertRaises(_StopBuild):
            eng._build_retrieval_pipeline(cfg={})
        self.assertEqual(
            captured.get("request_timeout_s"), engine_mod.QUERY_EMBED_TIMEOUT_S)

    def test_acquire_retrieval_lock_times_out_cleanly(self):
        """A chat waits at most _RETRIEVAL_LOCK_TIMEOUT_S on the mutation lock,
        then fails with an explanatory error instead of hanging forever."""
        import rag.vault as vault_mod
        from rag.vault import ObsidianVaultManager

        manager = ObsidianVaultManager()
        self.assertTrue(manager._index_mutation_lock.acquire(blocking=False))
        try:
            with patch.object(vault_mod, "_RETRIEVAL_LOCK_TIMEOUT_S", 0.05):
                with self.assertRaises(RuntimeError) as ctx:
                    manager._acquire_retrieval_lock()
            self.assertIn("busy", str(ctx.exception))
        finally:
            manager._index_mutation_lock.release()
        # Lock free again → acquire succeeds and the caller owns the release.
        manager._acquire_retrieval_lock()
        manager._index_mutation_lock.release()


class TestReadNoteFreshExtractBudget(unittest.TestCase):
    """Item 2.4: read_note refuses to START a fresh uncached-PDF extraction
    when the remaining agent budget is below the floor; cache-served and
    unbounded (time_budget_s=None) calls are unaffected."""

    def _manager_with_pdf(self, tmp):
        import pathlib
        from rag.vault import ObsidianVaultManager
        vault = pathlib.Path(tmp)
        (vault / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
        m = ObsidianVaultManager()
        # Direct field set: _normalise_vault_path rejects macOS tmpdirs
        # (/var/folders is under a blocked system root) — path safety is not
        # what this test exercises.
        m._vault_path = str(vault)
        return m

    def test_small_budget_refuses_fresh_extract_before_it_starts(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            m = self._manager_with_pdf(tmp)
            with patch("rag.vault.extract_structured_from_pdf") as extract:
                with self.assertRaises(IOError) as ctx:
                    m.read_note("doc.pdf", time_budget_s=1.0)
            extract.assert_not_called()          # refused BEFORE starting
            self.assertIn("time budget", str(ctx.exception))

    def test_no_budget_keeps_legacy_unbounded_contract(self):
        import tempfile
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            m = self._manager_with_pdf(tmp)
            fake = SimpleNamespace(full_text="extracted text", truncated=False)
            with patch("rag.vault.extract_structured_from_pdf", return_value=fake) as extract:
                text, truncated = m.read_note("doc.pdf")   # no budget
            extract.assert_called_once()
            self.assertEqual(text, "extracted text")
            self.assertFalse(truncated)


class TestSharedRetunerRace(unittest.TestCase):
    """Item 2.7: fetching the cached BM25 retriever / reranker never mutates
    their tuning fields — the engine's _build_retrieval_pipeline (inside the
    mutation-lock hold that also runs the retrieval) is the ONLY writer.

    Defect pinned: the manager's fetch paths retuned the shared singletons
    under no lock (BM25 fast path) or a different lock (reranker load lock),
    so a deck run's vault_search (k=12) could retrim a concurrent user chat's
    in-flight retrieval/rerank (k=8) mid-pass.
    """

    def test_cached_bm25_fetch_is_read_only(self):
        from rag.vault import ObsidianVaultManager
        from types import SimpleNamespace

        m = ObsidianVaultManager()
        fake_bm25 = SimpleNamespace(similarity_top_k=50)
        docs = {f"d{i}": object() for i in range(7)}
        m._index = SimpleNamespace(docstore=SimpleNamespace(docs=docs))
        m._bm25_retriever = fake_bm25
        m._bm25_cached_doc_count = len(docs)

        with patch("rag.engine.BM25Retriever", object()):  # non-None gate
            out = m._get_bm25_retriever(top_k=8)
        self.assertIs(out, fake_bm25)
        self.assertEqual(fake_bm25.similarity_top_k, 50)   # untouched

    def test_cached_reranker_fetch_is_read_only(self):
        from rag.vault import ObsidianVaultManager
        from types import SimpleNamespace

        m = ObsidianVaultManager()
        fake_reranker = SimpleNamespace(top_n=50)
        m._reranker = fake_reranker
        with patch("rag.engine.SentenceTransformerRerank", object()), \
             patch.object(m, "_resolve_reranker_device_mode", return_value="auto"):
            m._reranker_model_loaded = "some-model::auto"
            out = m._get_reranker(model_name="some-model", top_n=8)
        self.assertIs(out, fake_reranker)
        self.assertEqual(fake_reranker.top_n, 50)          # untouched

    def test_engine_pipeline_is_the_one_tuning_writer(self):
        """The locked pipeline build sets both fields to per-query values —
        proving tuning still happens (in the right place) after the fetch
        paths went read-only."""
        from types import SimpleNamespace
        from rag import engine as engine_mod

        fake_bm25 = SimpleNamespace(similarity_top_k=1)
        fake_reranker = SimpleNamespace(top_n=1)

        class FakeProvider:
            def get_embedding(self, name, **kwargs):
                return object()
            def get_llm(self, name, **kwargs):
                return object()

        eng = engine_mod.SimpleQueryEngine.__new__(engine_mod.SimpleQueryEngine)
        eng.index = SimpleNamespace(vector_store=None)
        eng.llm_name = "llm"
        eng.embed_name = "embed"
        eng.provider_name = "ollama"
        eng.temperature = None
        eng.top_k = 8
        eng.top_k_explicit = True
        eng.similarity_cutoff = 0.25
        eng.prompt_mode = "balanced"
        eng.custom_system_prompt = ""
        eng.mmr_enabled = False
        eng.mmr_lambda = None
        eng.query_expansion = False
        eng.num_queries = 1
        eng.rerank_pool_ceiling = None
        eng.bm25_retriever = fake_bm25
        eng.reranker = fake_reranker
        eng.wikilink_graph = None
        eng.wikilink_expansion = False
        eng.thesaurus = None
        eng.thesaurus_expansion = False
        eng.primer_enabled = False
        eng._provider = FakeProvider()

        with patch.object(engine_mod, "VectorIndexRetriever", return_value=object()), \
             patch.object(engine_mod, "QueryFusionRetriever", return_value=object()):
            eng._build_retrieval_pipeline(cfg={})

        # breadth = min(max(8*4, 20), 50) = 32; final top_n = top_k = 8.
        self.assertEqual(fake_bm25.similarity_top_k, 32)
        self.assertEqual(fake_reranker.top_n, 8)


class TestCheckpointPromotionMarker(unittest.TestCase):
    """Item 2.8a: checkpoint promotion is bracketed by a marker so a crash
    between the per-file os.replace calls is DETECTED at load instead of
    silently serving a mixed-generation store (whose stranded chunks the
    document-hash skip would never re-insert)."""

    def _write_minimal_store(self, root):
        import pathlib
        p = pathlib.Path(root)
        (p / "docstore.json").write_text('{"docstore/data": {}}', encoding="utf-8")
        (p / "index_store.json").write_text('{"index_store/data": {}}', encoding="utf-8")

    def test_validator_rejects_promoting_state_and_accepts_complete_or_absent(self):
        import json as _json
        import pathlib
        import tempfile
        from rag.vault import ObsidianVaultManager

        m = ObsidianVaultManager()
        with tempfile.TemporaryDirectory() as tmp:
            self._write_minimal_store(tmp)
            marker = pathlib.Path(tmp) / m._PROMOTION_MARKER

            # Legacy checkpoint (no marker): accepted.
            m._validate_persisted_index_files(
                tmp, full=False, backend="lancedb",
                require_vector_data=False, check_promotion_marker=True)

            # Torn promotion: refused with a clear error.
            marker.write_text(_json.dumps({"state": "promoting"}), encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                m._validate_persisted_index_files(
                    tmp, full=False, backend="lancedb",
                    require_vector_data=False, check_promotion_marker=True)
            self.assertIn("mid-promotion", str(ctx.exception))

            # Completed promotion: accepted.
            marker.write_text(_json.dumps({"state": "complete"}), encoding="utf-8")
            m._validate_persisted_index_files(
                tmp, full=False, backend="lancedb",
                require_vector_data=False, check_promotion_marker=True)

            # The temp-dir validation during checkpointing never passes the
            # flag — a "promoting" marker there must not trip it.
            marker.write_text(_json.dumps({"state": "promoting"}), encoding="utf-8")
            m._validate_persisted_index_files(
                tmp, full=False, backend="lancedb", require_vector_data=False)

    def test_successful_promotion_leaves_marker_complete(self):
        import json as _json
        import pathlib
        import tempfile
        from types import SimpleNamespace
        from rag.vault import ObsidianVaultManager

        m = ObsidianVaultManager()
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "obsidian_storage"

            def fake_persist(persist_dir):
                self._write_minimal_store(persist_dir)

            idx = SimpleNamespace(
                storage_context=SimpleNamespace(persist=fake_persist))
            with patch("rag.vault.OBSIDIAN_INDEX_DIR", str(target)):
                m._persist_index_checkpoint(idx, backend="lancedb")
            state = _json.loads(
                (target / m._PROMOTION_MARKER).read_text(encoding="utf-8"))
            self.assertEqual(state["state"], "complete")
            self.assertTrue((target / "docstore.json").exists())


class TestReindexHonorsPromotionMarker(unittest.TestCase):
    """Item 2.8a follow-up (2026-07-05): the WRITER/reindex path honors the
    promotion marker the readers enforce, so the documented recovery
    ("Re-run indexing to rebuild a consistent checkpoint") actually rebuilds a
    torn (mid-promotion) store instead of loading it incrementally and sealing
    the inconsistency with a fresh "complete" marker."""

    def _write_minimal_store(self, root):
        import pathlib
        p = pathlib.Path(root)
        (p / "docstore.json").write_text('{"docstore/data": {}}', encoding="utf-8")
        (p / "index_store.json").write_text('{"index_store/data": {}}', encoding="utf-8")

    def test_incremental_store_is_intact_reflects_promotion_marker(self):
        import json as _json
        import pathlib
        import tempfile
        from rag.vault import ObsidianVaultManager

        m = ObsidianVaultManager()
        prev_meta = {"vector_backend": "lancedb"}  # no default__vector_store.json needed
        with tempfile.TemporaryDirectory() as tmp:
            self._write_minimal_store(tmp)
            marker = pathlib.Path(tmp) / m._PROMOTION_MARKER
            with patch("rag.vault.OBSIDIAN_INDEX_DIR", tmp):
                # Legacy checkpoint (no marker): incremental load is safe.
                self.assertTrue(m._incremental_store_is_intact(prev_meta))

                # Completed promotion: safe.
                marker.write_text(_json.dumps({"state": "complete"}), encoding="utf-8")
                self.assertTrue(m._incremental_store_is_intact(prev_meta))

                # Torn promotion: the writer must NOT load it incrementally.
                marker.write_text(_json.dumps({"state": "promoting"}), encoding="utf-8")
                self.assertFalse(m._incremental_store_is_intact(prev_meta))

                # Unreadable marker degrades to "not intact" (rebuild), never a
                # silent incremental load of a possibly-torn store.
                marker.write_text("{ not json", encoding="utf-8")
                self.assertFalse(m._incremental_store_is_intact(prev_meta))

    def test_missing_store_files_are_not_intact(self):
        # A structurally-incomplete store (a required file went missing in the
        # crash window) also degrades to a fresh rebuild, not an incremental
        # load that would strand chunks.
        import tempfile
        from rag.vault import ObsidianVaultManager

        m = ObsidianVaultManager()
        with tempfile.TemporaryDirectory() as tmp:
            # docstore.json/index_store.json deliberately absent.
            with patch("rag.vault.OBSIDIAN_INDEX_DIR", tmp):
                self.assertFalse(m._incremental_store_is_intact({"vector_backend": "lancedb"}))


class TestStaleSweepSparesScanFailures(unittest.TestCase):
    """Item 2.8b: a source whose READ failed this run (iCloud dataless miss,
    transient I/O) is never treated as deleted by the stale-doc sweep."""

    def test_failed_source_chunks_survive_the_sweep(self):
        from rag.vault import ObsidianVaultManager

        deleted = []

        class FakeDocstore:
            def get_document_hash(self, doc_id):
                return None
            def get_all_ref_doc_info(self):
                return {"gone.md::aaaa": None, "blip.md::bbbb": None}

        class FakeIdx:
            docstore = FakeDocstore()
            def delete_ref_doc(self, doc_id, delete_from_docstore=False):
                deleted.append(doc_id)

        m = ObsidianVaultManager()
        m._scan_failed_sources = {"blip.md"}   # loader recorded a read failure
        added, skipped, dels, failed, counts = m._index_documents_streaming(
            FakeIdx(), iter(()))               # empty run: nothing re-yielded
        # gone.md (genuinely deleted) is swept; blip.md (read failure) is kept.
        self.assertEqual(deleted, ["gone.md::aaaa"])

    def test_without_failures_the_sweep_is_unchanged(self):
        from rag.vault import ObsidianVaultManager

        deleted = []

        class FakeDocstore:
            def get_document_hash(self, doc_id):
                return None
            def get_all_ref_doc_info(self):
                return {"gone.md::aaaa": None}

        class FakeIdx:
            docstore = FakeDocstore()
            def delete_ref_doc(self, doc_id, delete_from_docstore=False):
                deleted.append(doc_id)

        m = ObsidianVaultManager()
        m._scan_failed_sources = set()
        m._index_documents_streaming(FakeIdx(), iter(()))
        self.assertEqual(deleted, ["gone.md::aaaa"])


class TestFinalPersistCacheInvalidationLockOrder(unittest.TestCase):
    """Item 2.8c: the final persist's _invalidate_retrieval_caches runs
    OUTSIDE the rw write lock — inside it, the invalidation contended the
    BM25 build lock while every reader queued on the write lock, freezing all
    chats for the duration of a concurrent BM25 rebuild (the mid-run
    checkpoint documented this hazard and stayed outside; the two paths had
    drifted)."""

    def test_final_persist_invalidates_caches_outside_write_lock(self):
        import tempfile
        from pathlib import Path as _P
        from rag.vault import ObsidianVaultManager
        from llama_index.core.embeddings import MockEmbedding

        class FakeProvider:
            def get_models(self):
                return [], "probe skipped"
            def get_embedding(self, _name):
                return MockEmbedding(embed_dim=2)

        class FakeStorageContext:
            def persist(self, persist_dir):
                _P(persist_dir).mkdir(parents=True, exist_ok=True)

        class FakeStorageContextFactory:
            @staticmethod
            def from_defaults(*_args, **_kwargs):
                return FakeStorageContext()

        class EmptyFakeIndex:
            def __init__(self):
                self.storage_context = FakeStorageContext()
                self.docstore = None

            @classmethod
            def from_documents(cls, *_args, **_kwargs):
                return cls()

        with tempfile.TemporaryDirectory(dir=_P.cwd()) as vault_dir, \
             tempfile.TemporaryDirectory() as index_dir:
            _P(vault_dir, "note.md").write_text("# A\nbody", encoding="utf-8")
            manager = ObsidianVaultManager()
            manager.restore_vault_path(vault_dir)

            # Track write-lock depth on THIS thread via a wrapped context
            # manager, and record what the invalidation observed.
            state = {"write_depth": 0, "observed": []}
            real_write_lock = manager._rw_lock.write_lock

            class TracingWriteLock:
                def __call__(self):
                    return self

                def __enter__(self):
                    state["write_depth"] += 1
                    self._inner = real_write_lock()
                    return self._inner.__enter__()

                def __exit__(self, *args):
                    state["write_depth"] -= 1
                    return self._inner.__exit__(*args)

            def probe_invalidate():
                state["observed"].append(state["write_depth"])

            with (
                patch("rag.vault.get_provider", return_value=FakeProvider()),
                patch("rag.vault.StorageContext", FakeStorageContextFactory),
                patch("rag.vault.VectorStoreIndex", EmptyFakeIndex),
                patch("rag.vault.OBSIDIAN_INDEX_DIR", index_dir),
                patch.object(manager, "_load_vault_documents",
                             side_effect=lambda _v, op_epoch=0: (
                                 manager.pause_indexing(), [])[1]),
                patch.object(manager._rw_lock, "write_lock", TracingWriteLock()),
                patch.object(manager, "_invalidate_retrieval_caches",
                             side_effect=probe_invalidate),
            ):
                manager.index_vault("llm", "embed", provider_name="ollama")

            self.assertTrue(state["observed"], "invalidation never ran")
            # Every invalidation during the run happened with write depth 0.
            self.assertEqual(set(state["observed"]), {0})
