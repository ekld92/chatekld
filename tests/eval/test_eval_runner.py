"""Hermetic tests for the retrieval eval RUNNER (scripts/eval_retrieval.py).

No network, no models, no real index: the cell resolver and the config
backup/restore helpers are pure, and the sweep itself is driven with a fake
manager returning canned ``RetrievedChunk`` lists — the same de-risking pattern
``test_eval_pipeline.py`` uses for the answer-quality runner.

The script is imported the way ``test_lancedb_migration.py`` imports
``scripts.migrate_vector_store``; the root ``conftest.py`` has already pointed
``CHATEKLD_BASE_DIR`` at a temp dir, so the module-scope ``core.constants``
import can never touch the user's real app data.

Every test that touches the filesystem uses ``TemporaryDirectory`` so nothing
leaks into ``/tmp`` (the old ``_tmp_config`` helper used bare ``mkdtemp`` and
leaked one directory per run).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.llm.types import RetrievedChunk
from scripts.eval_retrieval import (
    BASELINE_CELL_NAME,
    CONFIG_KEYS,
    MAX_CONSECUTIVE_QUERY_FAILURES,
    PRESETS,
    RETRIEVE_KEYS,
    RETRIEVE_NON_KNOBS,
    CellError,
    ConfigUnsafeError,
    apply_config_overrides,
    backup_config,
    baseline_retrieve_kwargs,
    check_stale_sidecar,
    detect_inert_overrides,
    docstore_sources,
    filter_stale_queries,
    resolve_cells,
    restore_config,
    run_cells,
    sidecar_path,
)
from tests.eval.retrieval_metrics import RetrievalQuery

KS = (1, 5, 10)


def _queries() -> list:
    return [
        RetrievalQuery(id="q1", query="what is HFrEF", lang="en",
                       relevant_sources=["notes/heart.md"]),
        RetrievalQuery(id="q2", query="qu'est-ce que la FEVG", lang="fr",
                       relevant_sources=["notes/fevg.md"]),
    ]


class _FakeManager:
    """Stands in for ObsidianVaultManager: canned chunks per query string.

    Honours the exact ``retrieve(message, **kwargs)`` contract the runner calls
    with — including the ``stage_cb`` progress callback — so drift in that call
    shape fails here rather than only on a live index. The reranker cache
    attributes mirror the manager's real ones so the tri-state attribution can
    be exercised without loading a cross-encoder.
    """

    def __init__(self, canned: dict, *, reranker_loaded: str = "",
                 reranker_failed: bool = False, reranker_last_tried: str = "") -> None:
        self._canned = canned
        self._reranker = object() if reranker_loaded else None
        self._reranker_model_loaded = reranker_loaded
        self._reranker_failed = reranker_failed
        self._reranker_last_tried = reranker_last_tried
        self.calls: list = []
        self.status: list = []

    def retrieve(self, message, **kwargs):
        kwargs.pop("stage_cb", None)
        self.calls.append({"message": message, **kwargs})
        return [RetrievedChunk(text="…", source=src, score=score)
                for src, score in self._canned.get(message, [])]

    def drain_status_messages(self):
        out, self.status = list(self.status), []
        return out

    def restore_vault_path(self, path):
        """Part of the contract ``main`` calls on a real manager."""
        self.vault_path = path


def _perfect_manager(**kw) -> _FakeManager:
    """Both golden docs at rank 1 (plus a decoy behind them)."""
    return _FakeManager({
        "what is HFrEF": [("notes/heart.md", 0.9), ("notes/other.md", 0.4)],
        "qu'est-ce que la FEVG": [("notes/fevg.md", 0.8), ("notes/other.md", 0.3)],
    }, **kw)


def _mediocre_manager(**kw) -> _FakeManager:
    """The golden doc only at rank 3 — worse than ``_perfect_manager`` on MRR."""
    return _FakeManager({
        "what is HFrEF": [("notes/a.md", 0.9), ("notes/b.md", 0.5), ("notes/heart.md", 0.2)],
        "qu'est-ce que la FEVG": [("notes/a.md", 0.9), ("notes/b.md", 0.5),
                                  ("notes/fevg.md", 0.2)],
    }, **kw)


class TestResolveCells(unittest.TestCase):
    def test_every_preset_expands_with_baseline_first(self):
        for preset in PRESETS:
            cells = resolve_cells(preset=preset)
            self.assertGreater(len(cells), 1, preset)
            self.assertEqual(cells[0]["name"], BASELINE_CELL_NAME, preset)
            self.assertEqual(cells[0]["retrieve"], {})
            self.assertEqual(cells[0]["config"], {})
            names = [c["name"] for c in cells]
            self.assertEqual(len(names), len(set(names)), f"{preset}: duplicate cell names")

    def test_no_source_yields_baseline_only(self):
        self.assertEqual([c["name"] for c in resolve_cells()], [BASELINE_CELL_NAME])

    def test_unknown_preset_and_double_source_rejected(self):
        with self.assertRaises(CellError):
            resolve_cells(preset="does-not-exist")
        with self.assertRaises(CellError):
            resolve_cells(preset="rerank-onoff", cells_path="x.json")

    def _cells_file(self, d: str, cells: list) -> str:
        path = Path(d, "cells.json")
        path.write_text(json.dumps({"cells": cells}), encoding="utf-8")
        return str(path)

    def test_unknown_retrieve_key_names_the_key(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._cells_file(d, [{"name": "typo",
                                         "retrieve": {"rerank_enabled": False}}])
            with self.assertRaises(CellError) as ctx:
                resolve_cells(cells_path=path)
        self.assertIn("rerank_enabled", str(ctx.exception))

    def test_unknown_config_key_names_the_key(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._cells_file(d, [{"name": "typo",
                                         "config": {"vault_rrf_dense_wieght": 2.0}}])
            with self.assertRaises(CellError) as ctx:
                resolve_cells(cells_path=path)
        self.assertIn("vault_rrf_dense_wieght", str(ctx.exception))

    def test_valid_mixed_cell_accepted_and_baseline_prepended(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._cells_file(d, [{
                "name": "mixed",
                "retrieve": {"reranker_enabled": False, "top_k": 12},
                "config": {"vault_rrf_bm25_weight": 2.0},
            }])
            cells = resolve_cells(cells_path=path)
        self.assertEqual([c["name"] for c in cells], [BASELINE_CELL_NAME, "mixed"])
        self.assertEqual(cells[1]["retrieve"], {"reranker_enabled": False, "top_k": 12})
        self.assertEqual(cells[1]["config"], {"vault_rrf_bm25_weight": 2.0})

    def test_user_supplied_baseline_leads_and_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._cells_file(d, [
                {"name": "other", "retrieve": {"top_k": 4}},
                {"name": BASELINE_CELL_NAME, "retrieve": {"top_k": 8}},
            ])
            cells = resolve_cells(cells_path=path)
        self.assertEqual([c["name"] for c in cells], [BASELINE_CELL_NAME, "other"])
        self.assertEqual(cells[0]["retrieve"], {"top_k": 8})


class TestCellValueValidation(unittest.TestCase):
    """Item 2: a cell must not be able to persist a value /api/config would drop."""

    def _reject(self, key: str, value, *, section: str = "config"):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "cells.json")
            path.write_text(json.dumps({"cells": [
                {"name": "bad", section: {key: value}},
            ]}), encoding="utf-8")
            with self.assertRaises(CellError) as ctx:
                resolve_cells(cells_path=str(path))
        return str(ctx.exception)

    def test_out_of_range_config_values_are_rejected_naming_the_range(self):
        for key, value, needle in (
            ("context_window", -1, "512"),
            ("context_window", 10**9, "131072"),
            ("vault_rrf_dense_weight", 1e308, "10"),
            ("vault_rerank_pool_multiplier", 99, "16"),
            ("vault_wikilink_score_decay", 2.0, "1"),
            ("vault_thesaurus_max_variants", 0, "8"),
        ):
            with self.subTest(key=key, value=value):
                msg = self._reject(key, value)
                self.assertIn(key, msg)
                self.assertIn(needle, msg)

    def test_bad_enum_value_is_rejected_listing_the_choices(self):
        msg = self._reject("vault_reranker_device", "cuda:0")
        self.assertIn("vault_reranker_device", msg)
        self.assertIn("auto", msg)
        self.assertIn("mps", msg)

    def test_in_range_config_values_are_accepted_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "cells.json")
            path.write_text(json.dumps({"cells": [{"name": "ok", "config": {
                "context_window": 8192,
                "vault_reranker_device": "cpu",
                "vault_rrf_dense_weight": 0.0,
                "vault_wikilink_score_decay": 1.0,
            }}]}), encoding="utf-8")
            cells = resolve_cells(cells_path=str(path))
        self.assertEqual(cells[1]["config"]["context_window"], 8192)
        self.assertEqual(cells[1]["config"]["vault_reranker_device"], "cpu")

    def test_out_of_range_retrieve_values_are_rejected(self):
        for key, value, needle in (
            ("top_k", 0, "1"),
            ("top_k", 99, "32"),
            ("similarity_cutoff", 1.5, "1.0"),
            ("num_queries", 12, "5"),
            ("rerank_pool_ceiling", 5, "10"),
            ("mmr_lambda", -0.1, "0.0"),
        ):
            with self.subTest(key=key, value=value):
                msg = self._reject(key, value, section="retrieve")
                self.assertIn(key, msg)
                self.assertIn(needle, msg)

    def test_booleans_must_be_real_booleans(self):
        msg = self._reject("reranker_enabled", "false", section="retrieve")
        self.assertIn("reranker_enabled", msg)
        self.assertIn("true or false", msg)
        # ...and a bool must not sneak into an int knob via int-subclassing.
        self.assertIn("top_k", self._reject("top_k", True, section="retrieve"))

    def test_non_integral_float_is_rejected_not_truncated(self):
        # coerce_int_in_range would silently make this 3; a swept cell that ran
        # at a value the operator never wrote is exactly what item 2 forbids.
        self.assertIn("top_k", self._reject("top_k", 3.5, section="retrieve"))

    def test_reranker_model_path_shapes_are_rejected(self):
        for value in ("/etc/passwd", "../../models/x", "a//b", "a/b/c", "x" * 200):
            with self.subTest(value=value):
                msg = self._reject("reranker_model", value, section="retrieve")
                self.assertIn("reranker_model", msg)

    def test_reranker_model_valid_shapes_are_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "cells.json")
            path.write_text(json.dumps({"cells": [
                {"name": "a", "retrieve": {"reranker_model": "BAAI/bge-reranker-v2-m3"}},
                {"name": "b", "retrieve": {"reranker_model": ""}},
            ]}), encoding="utf-8")
            cells = resolve_cells(cells_path=str(path))
        self.assertEqual(len(cells), 3)

    def test_reranker_model_rule_matches_the_app(self):
        """Parity ratchet against ``api/routes/config.py::_coerce_reranker_model``.

        The regex is duplicated in the runner (importing the route module would
        drag Flask + rag.vault into a stdlib-light script), so this pins the two
        to identical verdicts — a hardening there must not leave the eval
        runner's copy permissive.
        """
        from api.routes.config import _coerce_reranker_model
        from scripts.eval_retrieval import _reranker_model_rule

        vectors = [
            "BAAI/bge-reranker-v2-m3", "model", "ns/name", "", "  ",
            "/etc/passwd", "../x", "a//b", "a/b/c", "a..b", "-lead/x", "x" * 200,
            "cross-encoder/ms-marco-MiniLM-L-6-v2",
        ]
        for value in vectors:
            with self.subTest(value=value):
                app_ok = _coerce_reranker_model(value) is not None
                try:
                    _reranker_model_rule("cell", "reranker_model", value)
                    runner_ok = True
                except CellError:
                    runner_ok = False
                self.assertEqual(app_ok, runner_ok, value)


class TestDeadPresets(unittest.TestCase):
    """Item 5: the two presets that were provably no-ops."""

    def test_cutoff_sweep_disables_the_blockers(self):
        cells = resolve_cells(preset="cutoff-sweep")[1:]
        self.assertTrue(cells)
        for cell in cells:
            self.assertFalse(cell["retrieve"]["reranker_enabled"])
            self.assertFalse(cell["retrieve"]["hybrid_enabled"])
            self.assertFalse(cell["retrieve"]["query_expansion"])
        # ...and the cutoffs still differ, or there would be nothing to sweep.
        cutoffs = {c["retrieve"]["similarity_cutoff"] for c in cells}
        self.assertGreater(len(cutoffs), 1)

    def test_cutoff_sweep_cells_are_not_inert(self):
        base = baseline_retrieve_kwargs({"provider": "ollama", "embed": "e",
                                         "vault_reranker_model": "ns/m"}, [10])
        for cell in resolve_cells(preset="cutoff-sweep")[1:]:
            self.assertEqual(detect_inert_overrides(cell, base, {}), [], cell["name"])

    def test_pool_sweep_covaries_the_multiplier_and_binds(self):
        base = baseline_retrieve_kwargs({"provider": "ollama", "embed": "e",
                                         "vault_reranker_model": "ns/m"}, [10])
        cells = resolve_cells(preset="pool-sweep")[1:]
        self.assertTrue(cells)
        for cell in cells:
            self.assertEqual(cell["config"]["vault_rerank_pool_multiplier"], 16)
            # top_k(10) × 16 = 160 pre-clamp, so every ceiling below that binds.
            self.assertLess(cell["retrieve"]["rerank_pool_ceiling"], 160)
            self.assertEqual(detect_inert_overrides(cell, base, {}), [], cell["name"])


class TestInertOverrideDetector(unittest.TestCase):
    """Item 5b: a knob the engine cannot read must be flagged, not silently Δ=0."""

    def _base(self, **over) -> dict:
        cfg = {"provider": "ollama", "embed": "e", "vault_reranker_model": "ns/m"}
        cfg.update(over)
        return baseline_retrieve_kwargs(cfg, [10])

    def test_cutoff_with_reranker_and_fusion_is_flagged(self):
        cell = {"name": "c", "retrieve": {"similarity_cutoff": 0.4}, "config": {}}
        notes = detect_inert_overrides(cell, self._base(), {})
        self.assertTrue(any("similarity_cutoff" in n and "INERT" in n for n in notes))

    def test_cutoff_on_the_dense_only_path_is_not_flagged(self):
        cell = {"name": "c", "config": {},
                "retrieve": {"similarity_cutoff": 0.4, "reranker_enabled": False,
                             "hybrid_enabled": False, "query_expansion": False}}
        self.assertEqual(detect_inert_overrides(cell, self._base(), {}), [])

    def test_non_binding_pool_ceiling_is_flagged(self):
        # baseline: top_k 10 × multiplier 4 = 40 pre-clamp, so 100 never binds.
        cell = {"name": "c", "retrieve": {"rerank_pool_ceiling": 100}, "config": {}}
        notes = detect_inert_overrides(cell, self._base(), {})
        self.assertTrue(any("rerank_pool_ceiling" in n for n in notes))

    def test_binding_pool_ceiling_is_not_flagged(self):
        cell = {"name": "c", "retrieve": {"rerank_pool_ceiling": 20}, "config": {}}
        self.assertEqual(detect_inert_overrides(cell, self._base(), {}), [])

    def test_pool_knobs_without_a_reranker_are_flagged(self):
        cell = {"name": "c", "config": {},
                "retrieve": {"rerank_pool_ceiling": 20, "reranker_enabled": False}}
        notes = detect_inert_overrides(cell, self._base(), {})
        self.assertTrue(any("rerank_pool_ceiling" in n for n in notes))

    def test_sub_knobs_of_disabled_features_are_flagged(self):
        for cell in (
            {"name": "a", "retrieve": {"mmr_lambda": 0.3}, "config": {}},
            {"name": "b", "retrieve": {"num_queries": 4}, "config": {}},
            {"name": "c", "retrieve": {}, "config": {"vault_wikilink_node_cap": 40}},
            {"name": "d", "retrieve": {}, "config": {"vault_thesaurus_max_variants": 5}},
        ):
            with self.subTest(cell=cell["name"]):
                self.assertTrue(detect_inert_overrides(cell, self._base(), {}))

    def test_rrf_weights_without_fusion_are_flagged(self):
        cell = {"name": "c", "retrieve": {"hybrid_enabled": False},
                "config": {"vault_rrf_dense_weight": 2.0}}
        notes = detect_inert_overrides(cell, self._base(), {})
        self.assertTrue(any("vault_rrf_" in n for n in notes))

    def test_run_cells_records_and_reports_inert_overrides(self):
        lines: list = []
        cell = {"name": "cutoff", "retrieve": {"similarity_cutoff": 0.4}, "config": {}}
        results = run_cells(_perfect_manager(), _queries()[:1],
                            [{"name": BASELINE_CELL_NAME, "retrieve": {}, "config": {}}, cell],
                            KS, warmup=False, echo=lines.append,
                            base_kwargs=self._base())
        self.assertTrue(results[1]["inert_overrides"])
        self.assertIn("INERT OVERRIDE", "\n".join(lines))


class TestRunCells(unittest.TestCase):
    def test_metrics_are_computed_per_cell_with_baseline_first(self):
        cells = resolve_cells(preset="rerank-onoff")
        results = run_cells(_perfect_manager(), _queries(), cells, KS, warmup=False,
                            echo=lambda _m: None)
        self.assertEqual([r["name"] for r in results], [BASELINE_CELL_NAME, "rerank-off"])
        for r in results:
            self.assertEqual(r["metrics"]["recall@1"], 1.0)
            self.assertEqual(r["metrics"]["mrr"], 1.0)
            self.assertEqual(r["metrics"]["n"], 2.0)
        self.assertEqual(results[1]["retrieve_kwargs"], {"reranker_enabled": False})

    def test_results_are_deltaable_between_cells(self):
        # A Δ is only meaningful if two cells can actually differ; drive that by
        # having the manager answer differently per cell.
        good = run_cells(_perfect_manager(), _queries(), resolve_cells(), KS,
                         warmup=False, echo=lambda _m: None)[0]
        poor = run_cells(_mediocre_manager(), _queries(), resolve_cells(), KS,
                         warmup=False, echo=lambda _m: None)[0]
        self.assertEqual(good["metrics"]["mrr"], 1.0)
        self.assertAlmostEqual(poor["metrics"]["mrr"], 1.0 / 3.0)
        self.assertEqual(poor["metrics"]["recall@1"], 0.0)
        self.assertEqual(poor["metrics"]["recall@5"], 1.0)
        self.assertLess(poor["metrics"]["ndcg@10"], good["metrics"]["ndcg@10"])

    def test_per_query_records_ranks_and_retrieved_pairs(self):
        results = run_cells(_mediocre_manager(), _queries(), resolve_cells(), KS,
                            warmup=False, echo=lambda _m: None)
        per_query = results[0]["per_query"]
        self.assertEqual([row["id"] for row in per_query], ["q1", "q2"])
        self.assertEqual(per_query[0]["first_hit_rank"], 3)
        self.assertEqual(per_query[0]["retrieved"][0], ["notes/a.md", 0.9])

    def test_first_hit_source_is_the_deduped_document_not_a_raw_index(self):
        """The payload names the hit instead of leaving readers to mis-derive it.

        ``first_hit_rank`` indexes the DEDUPED document list while ``retrieved``
        is the raw per-chunk list, so ``retrieved[rank - 1]`` is a different (and
        wrong) row whenever a note contributed several chunks.
        """
        mgr = _FakeManager({"what is HFrEF": [
            ("notes/a.md", 0.9), ("notes/a.md", 0.8), ("notes/heart.md", 0.7)]})
        result = run_cells(mgr, _queries()[:1], resolve_cells(), KS, warmup=False,
                           echo=lambda _m: None)[0]
        row = result["per_query"][0]
        self.assertEqual(row["first_hit_rank"], 2)          # deduped: a.md, heart.md
        self.assertEqual(row["first_hit_source"], "notes/heart.md")
        # The naive raw-list lookup would have named the wrong document.
        self.assertEqual(row["retrieved"][row["first_hit_rank"] - 1][0], "notes/a.md")

    def test_missing_query_records_null_first_hit_rank(self):
        mgr = _FakeManager({"what is HFrEF": [], "qu'est-ce que la FEVG": []})
        results = run_cells(mgr, _queries(), resolve_cells(), KS, warmup=False,
                            echo=lambda _m: None)
        self.assertIsNone(results[0]["per_query"][0]["first_hit_rank"])
        self.assertIsNone(results[0]["per_query"][0]["first_hit_source"])
        self.assertEqual(results[0]["metrics"]["mrr"], 0.0)

    def test_latency_fields_present(self):
        result = run_cells(_perfect_manager(), _queries(), resolve_cells(), KS,
                           warmup=False, echo=lambda _m: None)[0]
        self.assertEqual(set(result["latency_ms"]), {"p50", "p95", "mean"})
        for value in result["latency_ms"].values():
            self.assertGreaterEqual(value, 0.0)

    def test_warmup_is_discarded_but_still_calls_retrieve(self):
        mgr = _perfect_manager()
        result = run_cells(mgr, _queries(), resolve_cells(), KS, warmup=True,
                           echo=lambda _m: None)[0]
        # 1 warm-up + 2 scored queries; only the 2 are recorded.
        self.assertEqual(len(mgr.calls), 3)
        self.assertEqual(len(result["per_query"]), 2)

    def test_base_kwargs_merge_with_cell_overrides(self):
        mgr = _perfect_manager()
        cells = resolve_cells(preset="rerank-onoff")
        run_cells(mgr, _queries()[:1], cells, KS, warmup=False, echo=lambda _m: None,
                  base_kwargs={"top_k": 10, "reranker_enabled": True, "embed_name": "e"})
        baseline_call, cell_call = mgr.calls[0], mgr.calls[1]
        self.assertTrue(baseline_call["reranker_enabled"])
        self.assertEqual(baseline_call["top_k"], 10)
        self.assertFalse(cell_call["reranker_enabled"])
        self.assertEqual(cell_call["top_k"], 10)      # untouched keys survive

    def test_stage_cb_is_passed_to_retrieve(self):
        """Item 10: retrieve() gets a progress callback (never a swept knob)."""
        seen: list = []

        class _StageManager(_FakeManager):
            def retrieve(self, message, **kwargs):
                seen.append(callable(kwargs.get("stage_cb")))
                return super().retrieve(message, **kwargs)

        run_cells(_StageManager({}), _queries()[:1], resolve_cells(), KS,
                  warmup=True, echo=lambda _m: None)
        self.assertTrue(seen and all(seen))
        self.assertNotIn("stage_cb", RETRIEVE_KEYS)

    def test_manager_status_messages_are_drained_and_echoed(self):
        mgr = _perfect_manager()
        mgr.status = ["WARNING: Reranker could not be loaded"]
        lines: list = []
        run_cells(mgr, _queries()[:1], resolve_cells(), KS, warmup=False,
                  echo=lines.append)
        self.assertIn("Reranker could not be loaded", "\n".join(lines))
        self.assertEqual(mgr.status, [])   # drained, not merely peeked

    def test_rrf_weight_note_is_printed_for_non_unit_weights(self):
        lines: list = []
        cells = resolve_cells(preset="rrf-sweep")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "config.json")
            path.write_text(json.dumps({"provider": "ollama"}), encoding="utf-8")
            run_cells(_perfect_manager(), _queries()[:1], cells, KS, warmup=False,
                      echo=lines.append, config_path=str(path))
        text = "\n".join(lines)
        self.assertIn("relative_score", text)
        # …and not for the baseline cell, whose weights are the 1.0 defaults.
        self.assertNotIn("relative_score", lines[0])

    def test_config_cell_without_config_path_is_rejected(self):
        with self.assertRaises(CellError):
            run_cells(_perfect_manager(), _queries(), resolve_cells(preset="rrf-sweep"),
                      KS, warmup=False, echo=lambda _m: None)

    def test_results_are_checkpointed_after_every_cell(self):
        """Item 9: a sweep killed at cell 4 must not lose cells 1-3."""
        seen: list = []
        cells = resolve_cells(preset="rerank-onoff")
        run_cells(_perfect_manager(), _queries()[:1], cells, KS, warmup=False,
                  echo=lambda _m: None, checkpoint=lambda r: seen.append(len(r)))
        self.assertEqual(seen, [1, 2])

    def test_a_failing_checkpoint_does_not_lose_the_run(self):
        def _boom(_results):
            raise OSError("disk full")

        results = run_cells(_perfect_manager(), _queries()[:1], resolve_cells(), KS,
                            warmup=False, echo=lambda _m: None, checkpoint=_boom)
        self.assertEqual(len(results), 1)


class TestRerankerTriState(unittest.TestCase):
    """Item 6: attribution must be positive, never inherited or assumed."""

    def _base(self) -> dict:
        return baseline_retrieve_kwargs(
            {"provider": "ollama", "embed": "e", "vault_reranker_model": "ns/m"}, [10])

    def test_cell_that_did_not_request_a_reranker_reports_none(self):
        # The old sticky-flag read fired here: this cell never calls
        # _get_reranker, so it inherited the PREVIOUS cell's failure flag.
        mgr = _perfect_manager(reranker_failed=True, reranker_last_tried="ns/m::auto")
        cell = {"name": "off", "retrieve": {"reranker_enabled": False}, "config": {}}
        result = run_cells(mgr, _queries()[:1], [cell], KS, warmup=False,
                           echo=lambda _m: None, base_kwargs=self._base())[0]
        self.assertEqual(result["reranker"]["state"], "none")
        self.assertFalse(result["reranker"]["requested"])

    def test_loaded_is_determined_positively_from_the_cache_key(self):
        mgr = _perfect_manager(reranker_loaded="ns/m::auto")
        result = run_cells(mgr, _queries()[:1], resolve_cells(), KS, warmup=False,
                           echo=lambda _m: None, base_kwargs=self._base())[0]
        self.assertEqual(result["reranker"]["state"], "loaded")
        self.assertEqual(result["reranker"]["model"], "ns/m")

    def test_failure_is_attributed_only_to_the_key_that_was_tried(self):
        mgr = _perfect_manager(reranker_failed=True, reranker_last_tried="ns/m::auto")
        result = run_cells(mgr, _queries()[:1], resolve_cells(), KS, warmup=False,
                           echo=lambda _m: None, base_kwargs=self._base())[0]
        self.assertEqual(result["reranker"]["state"], "failed")
        # A flag left over from a DIFFERENT model must not be attributed here.
        other = _perfect_manager(reranker_failed=True, reranker_last_tried="other::auto")
        result2 = run_cells(other, _queries()[:1], resolve_cells(), KS, warmup=False,
                            echo=lambda _m: None, base_kwargs=self._base())[0]
        self.assertEqual(result2["reranker"]["state"], "unknown")

    def test_empty_model_never_reports_a_working_reranker(self):
        """The dangerous mis-attribution: enabled-but-model-less ran rerank-free
        while being scored AS the reranked baseline."""
        base = baseline_retrieve_kwargs({"provider": "ollama", "embed": "e",
                                         "vault_reranker_model": ""}, [10])
        self.assertNotIn("reranker_model", base)
        result = run_cells(_perfect_manager(), _queries()[:1], resolve_cells(), KS,
                           warmup=False, echo=lambda _m: None, base_kwargs=base)[0]
        self.assertEqual(result["reranker"]["state"], "none")

    def test_device_mode_is_part_of_the_key(self):
        mgr = _perfect_manager(reranker_loaded="ns/m::cpu")
        result = run_cells(mgr, _queries()[:1], resolve_cells(), KS, warmup=False,
                           echo=lambda _m: None, base_kwargs=self._base(),
                           base_config={"vault_reranker_device": "cpu"})[0]
        self.assertEqual(result["reranker"]["state"], "loaded")
        self.assertEqual(result["reranker"]["device_mode"], "cpu")


class TestPerQueryFailureTolerance(unittest.TestCase):
    """Item 7: a dead embed provider must not kill the sweep on query #1."""

    def _manager(self, fail_ids: set):
        canned = _perfect_manager()._canned

        class _Flaky(_FakeManager):
            def retrieve(self, message, **kwargs):
                if message in fail_ids:
                    raise RuntimeError("connection refused")
                return super().retrieve(message, **kwargs)

        return _Flaky(canned)

    def test_a_failed_query_is_excluded_not_scored_as_a_miss(self):
        mgr = self._manager({"what is HFrEF"})
        result = run_cells(mgr, _queries(), resolve_cells(), KS, warmup=False,
                           echo=lambda _m: None)[0]
        self.assertEqual([f["id"] for f in result["failed_queries"]], ["q1"])
        self.assertEqual(result["scored_queries"], 1)
        self.assertEqual(result["metrics"]["n"], 1.0)
        # Scored as a miss, mrr would be 0.5; excluded, the surviving query's
        # perfect result stands alone.
        self.assertEqual(result["metrics"]["mrr"], 1.0)

    def test_the_failed_query_row_is_marked_excluded(self):
        mgr = self._manager({"what is HFrEF"})
        result = run_cells(mgr, _queries(), resolve_cells(), KS, warmup=False,
                           echo=lambda _m: None)[0]
        row = next(r for r in result["per_query"] if r["id"] == "q1")
        self.assertTrue(row["excluded"])
        self.assertIn("connection refused", row["error"])

    def test_consecutive_failures_abort_the_cell_not_the_run(self):
        queries = [RetrievalQuery(id=f"q{i}", query=f"dead-{i}", lang="en",
                                  relevant_sources=["notes/x.md"])
                   for i in range(MAX_CONSECUTIVE_QUERY_FAILURES + 4)]

        class _Dead(_FakeManager):
            def retrieve(self, message, **kwargs):
                raise RuntimeError("provider down")

        cells = resolve_cells(preset="rerank-onoff")
        results = run_cells(_Dead({}), queries, cells, KS, warmup=False,
                            echo=lambda _m: None)
        # Both cells ran (the RUN survives) and each stopped at the breaker.
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertTrue(r["aborted"])
            self.assertEqual(len(r["failed_queries"]), MAX_CONSECUTIVE_QUERY_FAILURES)

    def test_intermittent_failures_do_not_trip_the_breaker(self):
        queries = []
        for i in range(MAX_CONSECUTIVE_QUERY_FAILURES + 3):
            queries.append(RetrievalQuery(id=f"q{i}",
                                          query=("bad" if i % 2 else "what is HFrEF"),
                                          lang="en", relevant_sources=["notes/heart.md"]))
        mgr = self._manager({"bad"})
        result = run_cells(mgr, queries, resolve_cells(), KS, warmup=False,
                           echo=lambda _m: None)[0]
        self.assertEqual(result["aborted"], "")
        self.assertGreater(result["scored_queries"], 0)


class TestConfigBackupRestore(unittest.TestCase):
    ORIGINAL = '{\n  "provider": "ollama",\n  "vault_rrf_dense_weight": 1.0\n}\n'

    def _config(self, d: str) -> Path:
        path = Path(d, "config.json")
        path.write_text(self.ORIGINAL, encoding="utf-8")
        return path

    def test_backup_restore_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._config(d)
            blob = backup_config(str(path))
            apply_config_overrides(str(path), {"vault_rrf_dense_weight": 2.0})
            merged = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(merged["vault_rrf_dense_weight"], 2.0)
            self.assertEqual(merged["provider"], "ollama")  # other keys preserved
            restore_config(str(path), blob)
            self.assertEqual(path.read_text(encoding="utf-8"), self.ORIGINAL)

    def test_sidecar_is_written_before_the_override_and_removed_after_restore(self):
        """Item 1: the backup must survive a SIGKILL, so it has to be a file."""
        with tempfile.TemporaryDirectory() as d:
            path = self._config(d)
            side = Path(sidecar_path(str(path)))
            blob = backup_config(str(path))
            self.assertTrue(side.exists())
            self.assertEqual(side.read_bytes(), self.ORIGINAL.encode())
            apply_config_overrides(str(path), {"context_window": 4096})
            self.assertTrue(side.exists())          # still there mid-sweep
            restore_config(str(path), blob)
            self.assertFalse(side.exists())         # only after a verified restore

    def test_missing_config_is_recreated_then_removed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "config.json")
            blob = backup_config(str(path))
            self.assertIsNone(blob)
            # A zero-byte sidecar is the unambiguous "there was no config" marker
            # (a real config is a JSON object, so it can never be empty).
            self.assertEqual(Path(sidecar_path(str(path))).read_bytes(), b"")
            apply_config_overrides(str(path), {"context_window": 8192})
            self.assertTrue(path.exists())
            restore_config(str(path), blob)
            self.assertFalse(path.exists())
            self.assertFalse(Path(sidecar_path(str(path))).exists())

    def test_pending_lock_is_reentrant(self):
        """(2026-07-29 audit 1.4) SIGTERM is delivered on the MAIN thread at an
        arbitrary bytecode boundary — including while ``_arm_pending`` holds
        ``_PENDING_LOCK``. The handler calls ``_restore_pending``, which
        re-acquires the lock on the same thread: with a plain Lock that is a
        self-deadlock (the process hangs AND the config is never restored —
        the one abnormal exit this machinery exists to survive). Ratchet: the
        lock must stay reentrant, and the handler path must complete while the
        lock is already held."""
        from scripts import eval_retrieval as mod

        with mod._PENDING_LOCK:
            # A plain Lock would hang HERE forever; RLock re-enters.
            acquired = mod._PENDING_LOCK.acquire(blocking=False)
            self.assertTrue(acquired, "_PENDING_LOCK must be reentrant (RLock)")
            mod._PENDING_LOCK.release()
            # The full handler body must be callable while the lock is held —
            # nothing armed, so it is a no-op, but it must not deadlock.
            mod._restore_pending("test-signal")

    def test_unlink_failure_on_no_config_restore_keeps_the_sidecar(self):
        """(2026-07-29 audit) In the ``blob is None`` branch a failed unlink
        means the sweep's config is still live; 'restored' would be a lie. The
        helper must raise ConfigUnsafeError and LEAVE the sidecar, so the next
        run refuses via check_stale_sidecar instead of sweeping over a state
        the user never got back."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "config.json")
            blob = backup_config(str(path))       # None + zero-byte sidecar
            apply_config_overrides(str(path), {"context_window": 8192})

            real_unlink = Path.unlink

            def deny_config_only(self_path, *a, **k):
                if self_path.name == "config.json":
                    raise PermissionError(13, "Permission denied")
                return real_unlink(self_path, *a, **k)

            with mock.patch.object(Path, "unlink", deny_config_only):
                with self.assertRaises(ConfigUnsafeError) as ctx:
                    restore_config(str(path), blob)
            self.assertIn("by hand", str(ctx.exception))
            self.assertTrue(path.exists(), "the live config must be untouched")
            self.assertTrue(Path(sidecar_path(str(path))).exists(),
                            "the sidecar is the next run's refusal signal")

    def test_sweep_restores_config_after_a_normal_run(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._config(d)
            run_cells(_perfect_manager(), _queries()[:1], resolve_cells(preset="rrf-sweep"),
                      KS, warmup=False, echo=lambda _m: None, config_path=str(path))
            self.assertEqual(path.read_text(encoding="utf-8"), self.ORIGINAL)
            self.assertFalse(Path(sidecar_path(str(path))).exists())

    def test_sweep_restores_config_after_a_midcell_exception(self):
        class _Boom(_FakeManager):
            def retrieve(self, message, **kwargs):
                raise RuntimeError("index exploded")

        with tempfile.TemporaryDirectory() as d:
            path = self._config(d)
            # A raising manager no longer kills the run (item 7) — the cell
            # aborts at the breaker — so the config restore is exercised through
            # the normal path here and through the masking test below.
            run_cells(_Boom({}), _queries()[:1], resolve_cells(preset="rrf-sweep"),
                      KS, warmup=False, echo=lambda _m: None, config_path=str(path))
            self.assertEqual(path.read_text(encoding="utf-8"), self.ORIGINAL)

    def test_a_restore_failure_does_not_mask_the_original_exception(self):
        """Item 1: the inner finally used to raise before clearing ``outstanding``,
        so the outer finally retried the same failing write and its exception
        replaced the real one."""
        from scripts.eval_retrieval import _disarm_pending

        boom = RuntimeError("the real failure")

        class _Boom(_FakeManager):
            def retrieve(self, message, **kwargs):
                raise boom

        # The mocked restore never disarms the module's atexit/SIGTERM guard, so
        # clear it here or the interpreter would try to "restore" a deleted temp
        # path at process exit.
        self.addCleanup(_disarm_pending)
        with tempfile.TemporaryDirectory() as d:
            path = self._config(d)
            calls: list = []

            def _bad_restore(*a, **kw):
                calls.append(a)
                raise OSError("read-only filesystem")

            with mock.patch("scripts.eval_retrieval.restore_config", _bad_restore):
                # The run completes (per-query failures are tolerated); what is
                # pinned here is that the failing restore is attempted EXACTLY
                # once per cell, never re-tried by the outer finally.
                run_cells(_Boom({}), _queries()[:1],
                          resolve_cells(preset="rrf-sweep"), KS, warmup=False,
                          echo=lambda _m: None, config_path=str(path))
            self.assertEqual(len(calls), 3)   # 3 rrf cells, one attempt each

    def test_unparseable_config_aborts_instead_of_obliterating_it(self):
        """Item 2b: the old fallback wrote a config holding ONLY the overrides."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "config.json")
            path.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(ConfigUnsafeError):
                apply_config_overrides(str(path), {"context_window": 4096})
            self.assertEqual(path.read_text(encoding="utf-8"), "{ not json")

    def test_non_object_config_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "config.json")
            path.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(ConfigUnsafeError):
                apply_config_overrides(str(path), {"context_window": 4096})

    def test_stale_sidecar_is_detected_with_both_paths(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._config(d)
            self.assertIsNone(check_stale_sidecar(str(path)))
            Path(sidecar_path(str(path))).write_bytes(b"{}")
            msg = check_stale_sidecar(str(path))
            self.assertIsNotNone(msg)
            self.assertIn(str(path), msg)
            self.assertIn(sidecar_path(str(path)), msg)


class TestDocstoreSources(unittest.TestCase):
    """Item 8: cheapest-first fallback chain, empty-set contract preserved."""

    def test_materials_manifest_is_preferred(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "indexed_materials.json").write_text(json.dumps({
                "materials": [{"source": "notes/Heart.md"}, {"source": "notes/fevg.md"}],
            }), encoding="utf-8")
            # A docstore.json that would raise proves the manifest tier answered.
            Path(d, "docstore.json").write_text("not json", encoding="utf-8")
            sources = docstore_sources(d, echo=lambda _m: None)
        self.assertEqual(sources, {"notes/heart.md", "notes/fevg.md"})  # norm_source folds case

    def test_falls_through_to_streamed_ref_doc_info(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "docstore.json").write_text(json.dumps({
                "docstore/ref_doc_info": {
                    "id-1": {"metadata": {"source": "notes/heart.md"}},
                    "id-2": {"metadata": {"file_path": "/abs/notes/fevg.md"}},
                    "id-3": {"metadata": {}},
                },
            }), encoding="utf-8")
            sources = docstore_sources(d, echo=lambda _m: None)
        self.assertEqual(sources, {"notes/heart.md", "/abs/notes/fevg.md"})

    def test_an_empty_manifest_falls_through_rather_than_dropping_everything(self):
        # An empty manifest must NOT be read as "the index contains nothing" —
        # that would exclude every golden query as stale.
        with tempfile.TemporaryDirectory() as d:
            Path(d, "indexed_materials.json").write_text(
                json.dumps({"materials": []}), encoding="utf-8")
            Path(d, "docstore.json").write_text(json.dumps({
                "docstore/ref_doc_info": {"id-1": {"metadata": {"source": "notes/a.md"}}},
            }), encoding="utf-8")
            sources = docstore_sources(d, echo=lambda _m: None)
        self.assertEqual(sources, {"notes/a.md"})

    def test_unreadable_index_yields_an_empty_set_and_drops_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            sources = docstore_sources(d, echo=lambda _m: None)
        self.assertEqual(sources, set())
        keep, stale = filter_stale_queries(_queries(), sources)
        self.assertEqual(len(keep), 2)
        self.assertEqual(stale, [])


class TestBaselineKwargs(unittest.TestCase):
    def test_top_k_is_max_k_and_explicit(self):
        kwargs = baseline_retrieve_kwargs({"provider": "ollama", "llm": "m", "embed": "e"},
                                          [1, 5, 10])
        self.assertEqual(kwargs["top_k"], 10)
        self.assertIs(kwargs["top_k_explicit"], True)
        self.assertEqual(kwargs["provider_name"], "ollama")
        self.assertEqual(kwargs["llm_name"], "m")
        self.assertEqual(kwargs["embed_name"], "e")

    def test_config_knobs_are_read_and_garbage_degrades_to_defaults(self):
        kwargs = baseline_retrieve_kwargs({
            "provider": "ollama",
            "vault_similarity_cutoff": 0.4,
            "vault_reranker_enabled": False,
            "vault_reranker_model": "  BAAI/bge-reranker-v2-m3  ",
            "vault_num_queries": "not-a-number",
        }, [5])
        self.assertEqual(kwargs["similarity_cutoff"], 0.4)
        self.assertFalse(kwargs["reranker_enabled"])
        self.assertEqual(kwargs["reranker_model"], "BAAI/bge-reranker-v2-m3")
        self.assertEqual(kwargs["num_queries"], 3)

    def test_empty_reranker_model_is_omitted(self):
        kwargs = baseline_retrieve_kwargs({"provider": "ollama",
                                           "vault_reranker_model": "  "}, [5])
        self.assertNotIn("reranker_model", kwargs)

    def test_every_kwarg_is_accepted_by_the_real_retrieve_signature(self):
        # Ratchet: baseline_retrieve_kwargs must never drift from retrieve().
        import inspect

        from rag.vault import ObsidianVaultManager
        params = set(inspect.signature(ObsidianVaultManager.retrieve).parameters)
        for key in baseline_retrieve_kwargs({"provider": "ollama"}, [5]):
            self.assertIn(key, params)


class TestRatchets(unittest.TestCase):
    """Item 13: the silent-drift traps."""

    def test_config_cache_invalidator_still_exists(self):
        """``_drop_config_cache`` swallows ImportError by design.

        If ``core.config._invalidate_config_cache`` were renamed, every config
        cell would silently read the PREVIOUS cell's stat-cached config and the
        sweep would report fabricated deltas with no error anywhere.
        """
        import core.config

        self.assertTrue(callable(getattr(core.config, "_invalidate_config_cache", None)))

    def test_retrieve_keys_match_signature(self):
        """RETRIEVE_KEYS == retrieve()'s signature minus the declared non-knobs."""
        import inspect

        from rag.vault import ObsidianVaultManager
        params = set(inspect.signature(ObsidianVaultManager.retrieve).parameters)
        self.assertEqual(set(RETRIEVE_KEYS), params - set(RETRIEVE_NON_KNOBS))

    def test_every_declared_key_has_a_value_rule(self):
        from scripts.eval_retrieval import _CONFIG_RULES, _RETRIEVE_RULES

        self.assertEqual(set(_RETRIEVE_RULES), set(RETRIEVE_KEYS))
        self.assertEqual(set(_CONFIG_RULES), set(CONFIG_KEYS))

    def test_config_value_ranges_match_the_app(self):
        """The cell ranges must not drift from ``/api/config``'s validators.

        Probes each key at one value just outside the app's accepted range and
        asserts BOTH layers reject it — so widening a range in
        ``api/routes/config.py`` without widening it here (or vice versa) is
        caught, without duplicating the literal bounds a third time.
        """
        from api.routes.config import _CONFIG_VALIDATORS
        from scripts.eval_retrieval import _CONFIG_RULES

        probes = {
            "vault_rrf_dense_weight": 10.5,
            "vault_rrf_bm25_weight": -0.5,
            "vault_rerank_pool_multiplier": 17,
            "vault_rerank_pool_floor": 201,
            "vault_reranker_device": "cuda",
            "vault_wikilink_neighbor_cap": 101,
            "vault_wikilink_node_cap": 201,
            "vault_wikilink_score_decay": 1.5,
            "vault_thesaurus_max_variants": 9,
            "context_window": 511,
        }
        self.assertEqual(set(probes), set(CONFIG_KEYS))
        for key, value in probes.items():
            with self.subTest(key=key):
                app_validator = _CONFIG_VALIDATORS.get(key)
                self.assertIsNotNone(app_validator, key)
                app_result = app_validator(value)
                # The app either drops (None) or clamps to something != value;
                # either way it refuses to store this value verbatim.
                self.assertNotEqual(app_result, value, key)
                with self.assertRaises(CellError, msg=key):
                    _CONFIG_RULES[key]("cell", key, value)


class TestStaleQrels(unittest.TestCase):
    def test_query_with_no_surviving_source_is_excluded(self):
        queries = _queries()
        keep, stale = filter_stale_queries(queries, {"notes/heart.md"})
        self.assertEqual([q.id for q in keep], ["q1"])
        self.assertEqual([q.id for q in stale], ["q2"])

    def test_comparison_is_normalized_on_both_sides(self):
        # macOS hands the docstore an NFD path; the golden file is typed NFC and
        # may differ in case. docstore_sources normalizes ITS side (norm_source),
        # so a golden source differing only in form/case must still count present.
        import unicodedata

        from tests.eval.retrieval_metrics import norm_source
        golden_nfc = unicodedata.normalize("NFC", "notes/Réunion.md")
        on_disk = unicodedata.normalize("NFD", "notes/RÉUNION.md")
        self.assertNotEqual(golden_nfc, on_disk)
        q = RetrievalQuery(id="x", query="q", lang="fr", relevant_sources=[golden_nfc])
        keep, stale = filter_stale_queries([q], {norm_source(on_disk)})
        self.assertEqual(len(keep), 1)
        self.assertEqual(stale, [])

    def test_unknown_docstore_drops_nothing(self):
        keep, stale = filter_stale_queries(_queries(), set())
        self.assertEqual(len(keep), 2)
        self.assertEqual(stale, [])


class TestMainExitCodes(unittest.TestCase):
    """``main`` refuses before it can produce meaningless numbers."""

    def setUp(self):
        # Every test here must see "no live server" unless it says otherwise;
        # a developer running the app while testing must not flip these.
        patcher = mock.patch("scripts.eval_retrieval.detect_live_server", return_value=None)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_unknown_cell_key_exits_2_before_touching_anything(self):
        from scripts.eval_retrieval import main

        with tempfile.TemporaryDirectory() as d:
            cells = Path(d, "cells.json")
            cells.write_text(json.dumps({"cells": [
                {"name": "typo", "retrieve": {"nope": 1}},
            ]}), encoding="utf-8")
            rc = main(["--yes", "--cells", str(cells),
                       "--golden", str(Path(d, "missing.json"))])
        self.assertEqual(rc, 2)

    def test_out_of_range_cell_value_exits_2(self):
        from scripts.eval_retrieval import main

        with tempfile.TemporaryDirectory() as d:
            cells = Path(d, "cells.json")
            cells.write_text(json.dumps({"cells": [
                {"name": "bad", "config": {"context_window": -1}},
            ]}), encoding="utf-8")
            rc = main(["--yes", "--cells", str(cells),
                       "--golden", str(Path(d, "missing.json"))])
        self.assertEqual(rc, 2)

    def test_missing_golden_exits_1(self):
        from scripts.eval_retrieval import main

        with tempfile.TemporaryDirectory() as d:
            rc = main(["--yes", "--golden", str(Path(d, "missing.json"))])
        self.assertEqual(rc, 1)

    def test_live_server_exits_2_even_with_yes(self):
        """Item 4: --yes skips the prompt, never the liveness refusal."""
        from scripts.eval_retrieval import main

        with mock.patch("scripts.eval_retrieval.detect_live_server",
                        return_value="http://127.0.0.1:5000"), \
                tempfile.TemporaryDirectory() as d:
            rc = main(["--yes", "--golden", str(Path(d, "missing.json"))])
        # 2, not 1: it refused before even looking for the golden set.
        self.assertEqual(rc, 2)

    def test_stale_sidecar_exits_2_before_reading_config(self):
        from scripts.eval_retrieval import main

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d, "config.json")
            cfg.write_text("{}", encoding="utf-8")
            Path(sidecar_path(str(cfg))).write_bytes(b"{}")
            with mock.patch("scripts.eval_retrieval.CONFIG_FILE", str(cfg)):
                rc = main(["--yes", "--golden", str(Path(d, "missing.json"))])
        self.assertEqual(rc, 2)

    def _golden(self, d: str) -> str:
        """Minimal valid golden set (see retrieval_metrics.load_golden)."""
        path = Path(d, "golden.json")
        path.write_text(json.dumps({
            "schema_version": 1,
            "vault_path": str(Path(d, "vault")),
            "embed": "nomic-embed-text",
            "queries": [{"id": "q1", "query": "x", "lang": "en",
                         "relevant_sources": ["notes/a.md"]}],
        }), encoding="utf-8")
        return str(path)

    def test_embed_mismatch_exits_1(self):
        """Item 3: query embeddings in a foreign vector space ⇒ every metric garbage."""
        from scripts.eval_retrieval import main

        with tempfile.TemporaryDirectory() as d:
            index = Path(d, "index")
            index.mkdir()
            (index / "obsidian_meta.json").write_text(json.dumps({
                "has_vector_data": True, "embed": "nomic-embed-text",
                "vector_backend": "simple",
            }), encoding="utf-8")
            # ``main`` imports load_config lazily inside the function body, so
            # patching the OWNER module (not a re-export) is what takes effect.
            with mock.patch("core.config.load_config",
                            return_value={"obsidian_vault_path": str(Path(d, "vault")),
                                          "provider": "ollama",
                                          "embed": "mxbai-embed-large"}):
                rc = main(["--yes", "--golden", self._golden(d),
                           "--index-dir", str(index)])
        self.assertEqual(rc, 1)

    def test_missing_embed_model_exits_1(self):
        from scripts.eval_retrieval import main

        with tempfile.TemporaryDirectory() as d:
            index = Path(d, "index")
            index.mkdir()
            (index / "obsidian_meta.json").write_text(json.dumps({
                "has_vector_data": True, "embed": "nomic-embed-text",
            }), encoding="utf-8")
            with mock.patch("core.config.load_config",
                            return_value={"obsidian_vault_path": str(Path(d, "vault")),
                                          "provider": "ollama", "embed": ""}):
                rc = main(["--yes", "--golden", self._golden(d),
                           "--index-dir", str(index)])
        self.assertEqual(rc, 1)

    def test_happy_path_writes_a_non_partial_results_file(self):
        """End-to-end through ``main`` with a stubbed manager.

        Covers the code no unit test reaches — the pre-flight estimate, the
        timed warm-up, the results payload and the Δ table — and pins the
        ``partial`` flag being CLEARED on the final write (a checkpoint left
        marked partial would make a complete sweep look truncated).
        """
        from scripts.eval_retrieval import main

        with tempfile.TemporaryDirectory() as d:
            index = Path(d, "index")
            index.mkdir()
            (index / "obsidian_meta.json").write_text(json.dumps({
                "has_vector_data": True, "embed": "nomic-embed-text",
                "vector_backend": "simple",
            }), encoding="utf-8")
            (index / "indexed_materials.json").write_text(json.dumps({
                "materials": [{"source": "notes/a.md"}],
            }), encoding="utf-8")
            out = Path(d, "results.json")

            mgr = _FakeManager({"x": [("notes/a.md", 0.9)]})
            with mock.patch("core.config.load_config",
                            return_value={"obsidian_vault_path": str(Path(d, "vault")),
                                          "provider": "ollama",
                                          "embed": "nomic-embed-text",
                                          "vault_reranker_model": "ns/m"}), \
                    mock.patch("rag.vault.ObsidianVaultManager", return_value=mgr):
                rc = main(["--yes", "--golden", self._golden(d),
                           "--index-dir", str(index), "--out", str(out),
                           "--preset", "rerank-onoff",
                           "--no-warmup"])
            self.assertEqual(rc, 0)
            # Read INSIDE the with-block: the results file lives in the temp dir.
            payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertIs(payload["partial"], False)
        self.assertEqual([c["name"] for c in payload["cells"]],
                         [BASELINE_CELL_NAME, "rerank-off"])
        self.assertEqual(payload["num_queries"], 1)
        self.assertEqual(payload["cells"][0]["metrics"]["recall@1"], 1.0)

    def test_reranker_ab_without_a_model_exits_2(self):
        """Item 6: both cells would run rerank-free — nothing to A/B."""
        from scripts.eval_retrieval import main

        with tempfile.TemporaryDirectory() as d:
            index = Path(d, "index")
            index.mkdir()
            (index / "obsidian_meta.json").write_text(json.dumps({
                "has_vector_data": True, "embed": "nomic-embed-text",
            }), encoding="utf-8")
            with mock.patch("core.config.load_config",
                            return_value={"obsidian_vault_path": str(Path(d, "vault")),
                                          "provider": "ollama",
                                          "embed": "nomic-embed-text",
                                          "vault_reranker_model": ""}):
                rc = main(["--yes", "--preset", "reranker-ab",
                           "--golden", self._golden(d), "--index-dir", str(index)])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
