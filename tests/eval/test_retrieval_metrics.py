"""Hermetic tests for the retrieval-metrics layer (no index, no models).

Every case here is hand-computed: the point is to pin the *semantics*
(doc-level dedupe, NFC/case folding, the nDCG denominator, empty-input zeros)
so a later refactor of the harness cannot quietly move the numbers the knob
sweeps are compared on.
"""
import json
import math
import tempfile
import unittest
from pathlib import Path

from tests.eval.retrieval_metrics import (
    UNIDENTIFIED_PREFIX,
    QueryScore,
    RetrievalQuery,
    aggregate,
    dedupe_ranked_sources,
    format_cells_table,
    load_golden,
    norm_source,
    score_query,
)

# 1/log2(3) — the discount of a rank-2 hit; spelled out so the expected nDCG
# values below stay readable as arithmetic rather than magic constants.
_D2 = 1.0 / math.log2(3)


def _q(relevant, qid="q1"):
    return RetrievalQuery(id=qid, query="q", lang="en", relevant_sources=list(relevant))


class TestNormSource(unittest.TestCase):
    def test_nfd_and_nfc_are_the_same_document(self):
        # macOS hands back the decomposed form; a hand-typed golden file is NFC.
        nfd = "café.md"
        nfc = "café.md"
        self.assertNotEqual(nfd, nfc)
        self.assertEqual(norm_source(nfd), norm_source(nfc))

    def test_case_insensitive(self):
        self.assertEqual(norm_source("Notes/Café.MD"), norm_source("notes/café.md"))

    def test_empty_and_none_like(self):
        self.assertEqual(norm_source(""), "")


class TestDedupe(unittest.TestCase):
    def test_chunks_of_one_doc_collapse_to_its_best_rank(self):
        # Three chunks of A at raw ranks 1-3 occupy rank 1 alone; B, at raw
        # rank 4, moves up to deduped rank 2.
        ranked = dedupe_ranked_sources(["a.md", "a.md", "a.md", "b.md", "c.md"])
        self.assertEqual(ranked, ["a.md", "b.md", "c.md"])

    def test_dedupe_happens_after_normalization(self):
        ranked = dedupe_ranked_sources(["Café.md", "café.md", "b.md"])
        self.assertEqual(ranked, [norm_source("café.md"), "b.md"])

    def test_empty_input(self):
        self.assertEqual(dedupe_ranked_sources([]), [])

    def test_sourceless_chunk_keeps_its_rank_slot(self):
        # Changed by the 2026-07-27 audit: an unusable source used to be
        # DROPPED, which silently promoted "  a.md" from rank 2 to rank 1. It
        # now becomes an opaque placeholder that occupies the slot.
        out = dedupe_ranked_sources(["", "  a.md"])
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0].startswith(UNIDENTIFIED_PREFIX))
        self.assertEqual(out[1], "  a.md")

    def test_each_sourceless_chunk_gets_its_own_slot(self):
        # Two sourceless chunks prove nothing about sharing a document, so they
        # do not collapse — collapsing would re-introduce the promotion above.
        out = dedupe_ranked_sources(["", "", "b.md"])
        self.assertEqual(len(out), 3)
        self.assertNotEqual(out[0], out[1])
        self.assertEqual(out[2], "b.md")

    def test_placeholders_cannot_collide_with_a_real_path(self):
        # NUL is unrepresentable in a filesystem path, so no normalized real
        # source can ever equal a placeholder (⇒ it can never score as a hit).
        self.assertNotIn(UNIDENTIFIED_PREFIX, norm_source("notes/unidentified#0.md"))


class TestScoreQuery(unittest.TestCase):
    def test_hit_at_rank_one(self):
        s = score_query(["a.md", "b.md", "c.md"], _q(["a.md"]), ks=[1, 5, 10])
        self.assertEqual(s.hit_ranks, [1])
        self.assertAlmostEqual(s.rr, 1.0)
        self.assertAlmostEqual(s.recall_at[1], 1.0)
        self.assertAlmostEqual(s.recall_at[5], 1.0)
        self.assertAlmostEqual(s.ndcg_at[1], 1.0)
        self.assertAlmostEqual(s.ndcg_at[10], 1.0)

    def test_hit_deeper_in_the_list(self):
        s = score_query(["a.md", "b.md", "c.md"], _q(["c.md"]), ks=[1, 5])
        self.assertEqual(s.hit_ranks, [3])
        self.assertAlmostEqual(s.rr, 1.0 / 3.0)
        self.assertAlmostEqual(s.recall_at[1], 0.0)
        self.assertAlmostEqual(s.recall_at[5], 1.0)
        self.assertAlmostEqual(s.ndcg_at[1], 0.0)
        # dcg = 1/log2(4) = 0.5, idcg = 1 (one relevant doc).
        self.assertAlmostEqual(s.ndcg_at[5], 0.5)

    def test_complete_miss(self):
        s = score_query(["a.md", "b.md"], _q(["z.md"]), ks=[1, 5])
        self.assertEqual(s.hit_ranks, [])
        self.assertAlmostEqual(s.rr, 0.0)
        self.assertAlmostEqual(s.recall_at[5], 0.0)
        self.assertAlmostEqual(s.ndcg_at[5], 0.0)

    def test_dedupe_lifts_the_second_relevant_doc_to_rank_two(self):
        # The headline case: A's three chunks must not push B out of the top-2.
        s = score_query(
            ["a.md", "a.md", "a.md", "b.md", "c.md"],
            _q(["a.md", "b.md"]),
            ks=[1, 2, 5],
        )
        self.assertEqual(s.hit_ranks, [1, 2])
        self.assertAlmostEqual(s.rr, 1.0)
        self.assertAlmostEqual(s.recall_at[1], 0.5)
        self.assertAlmostEqual(s.recall_at[2], 1.0)
        # Both relevant docs at the ideal positions ⇒ perfect nDCG at k ≥ 2.
        self.assertAlmostEqual(s.ndcg_at[2], 1.0)
        self.assertAlmostEqual(s.ndcg_at[5], 1.0)
        # At k=1 the ideal is a single hit, and there is one ⇒ 1.0.
        self.assertAlmostEqual(s.ndcg_at[1], 1.0)

    def test_multi_relevant_partial_recall(self):
        s = score_query(["a.md", "x.md", "b.md"], _q(["a.md", "b.md", "z.md"]), ks=[1, 3, 10])
        self.assertEqual(s.hit_ranks, [1, 3])
        self.assertAlmostEqual(s.recall_at[1], 1.0 / 3.0)
        self.assertAlmostEqual(s.recall_at[3], 2.0 / 3.0)
        self.assertAlmostEqual(s.recall_at[10], 2.0 / 3.0)
        # dcg = 1/log2(2) + 1/log2(4) = 1.5; idcg = min(3, 3) ideal hits.
        idcg3 = 1.0 + _D2 + 1.0 / 2.0
        self.assertAlmostEqual(s.ndcg_at[3], 1.5 / idcg3)

    def test_k_larger_than_retrieved_list(self):
        s = score_query(["a.md", "b.md"], _q(["b.md"]), ks=[10])
        self.assertEqual(s.hit_ranks, [2])
        self.assertAlmostEqual(s.recall_at[10], 1.0)
        self.assertAlmostEqual(s.rr, 0.5)
        self.assertAlmostEqual(s.ndcg_at[10], _D2)

    def test_matching_is_nfc_and_case_insensitive(self):
        s = score_query(["notes/Café.MD"], _q(["notes/café.md"]), ks=[1])
        self.assertEqual(s.hit_ranks, [1])
        self.assertAlmostEqual(s.recall_at[1], 1.0)

    def test_empty_retrieved_scores_zero_without_raising(self):
        s = score_query([], _q(["a.md"]), ks=[1, 5, 10])
        self.assertEqual(s.hit_ranks, [])
        self.assertAlmostEqual(s.rr, 0.0)
        self.assertEqual(set(s.recall_at.values()), {0.0})
        self.assertEqual(set(s.ndcg_at.values()), {0.0})

    def test_sourceless_chunk_does_not_inflate_the_ranks_below_it(self):
        # The audit's exact case: rank 1 went to an unidentifiable chunk, so
        # b.md really is at rank 2 ⇒ rr 0.5, not the 1.0 the drop used to give.
        s = score_query(["", "b.md"], _q(["b.md"]), ks=[1, 5])
        self.assertEqual(s.hit_ranks, [2])
        self.assertAlmostEqual(s.rr, 0.5)
        self.assertAlmostEqual(s.recall_at[1], 0.0)  # nothing relevant in top-1
        self.assertAlmostEqual(s.recall_at[5], 1.0)  # but the doc was found
        self.assertAlmostEqual(s.ndcg_at[5], _D2)

    def test_a_sourceless_chunk_is_never_itself_a_hit(self):
        # Golden sets can't contain an empty source (score_query discards it),
        # so a placeholder has nothing to match and scores a clean miss.
        s = score_query(["", ""], _q([""]), ks=[1, 5])
        self.assertEqual(s.hit_ranks, [])
        self.assertAlmostEqual(s.rr, 0.0)

    def test_duplicate_relevant_sources_count_once(self):
        # A hand-curated golden entry listing one document twice (or in two
        # spellings) must not halve the achievable recall: the denominator is
        # the deduped relevant SET, so a run that found it scores a full 1.0.
        s = score_query(["a.md"], _q(["a.md", "a.md", "A.MD"]), ks=[1, 5])
        self.assertEqual(s.hit_ranks, [1])
        self.assertAlmostEqual(s.recall_at[1], 1.0)
        self.assertAlmostEqual(s.ndcg_at[5], 1.0)

    def test_ideal_dcg_is_measured_against_the_full_relevant_set(self):
        # The recall-aware denominator: one perfect hit out of three relevant
        # documents is NOT a perfect ranking. Under a precision-only ideal
        # (min(n_relevant, len(ranked), k) = 1) this would read 1.0 and a cell
        # that lost two documents would look identical to one that lost none.
        s = score_query(["a.md"], _q(["a.md", "b.md", "c.md"]), ks=[5])
        idcg3 = 1.0 + _D2 + 0.5
        self.assertAlmostEqual(s.ndcg_at[5], 1.0 / idcg3)
        self.assertLess(s.ndcg_at[5], 1.0)

    def test_k_beyond_the_list_does_not_rescale_the_denominator(self):
        # k=100 over a 1-item list: recall saturates at what was found and nDCG
        # keeps the full-relevant-set ideal — a short list stays a short list.
        s = score_query(["a.md"], _q(["a.md", "b.md"]), ks=[100])
        self.assertAlmostEqual(s.recall_at[100], 0.5)
        self.assertAlmostEqual(s.ndcg_at[100], 1.0 / (1.0 + _D2))

    def test_empty_relevant_scores_zero_without_raising(self):
        s = score_query(["a.md"], _q([]), ks=[1, 5])
        self.assertEqual(s.hit_ranks, [])
        self.assertAlmostEqual(s.recall_at[5], 0.0)
        self.assertAlmostEqual(s.ndcg_at[5], 0.0)


class TestAggregate(unittest.TestCase):
    def test_means_across_queries(self):
        hit = score_query(["a.md", "b.md"], _q(["a.md"], "q1"), ks=[1, 5])
        miss = score_query(["a.md", "b.md"], _q(["z.md"], "q2"), ks=[1, 5])
        agg = aggregate([hit, miss], ks=[1, 5])
        self.assertAlmostEqual(agg["recall@1"], 0.5)
        self.assertAlmostEqual(agg["recall@5"], 0.5)
        self.assertAlmostEqual(agg["mrr"], 0.5)
        self.assertAlmostEqual(agg["ndcg@5"], 0.5)
        self.assertEqual(agg["n"], 2)

    def test_mrr_mean_of_distinct_ranks(self):
        first = score_query(["a.md"], _q(["a.md"], "q1"), ks=[1])
        third = score_query(["x.md", "y.md", "a.md"], _q(["a.md"], "q2"), ks=[1])
        agg = aggregate([first, third], ks=[1])
        self.assertAlmostEqual(agg["mrr"], (1.0 + 1.0 / 3.0) / 2.0)

    def test_empty_scores_are_zeros_with_n_zero(self):
        agg = aggregate([], ks=[1, 5, 10])
        self.assertEqual(agg["n"], 0)
        for key in ("recall@1", "recall@5", "recall@10", "mrr", "ndcg@10"):
            self.assertAlmostEqual(agg[key], 0.0, msg=key)

    def test_missing_cutoff_counts_as_zero(self):
        # A score computed at k=1 only, aggregated at k=5: the absent cutoff is
        # a miss, not a KeyError.
        s = QueryScore(id="q1", hit_ranks=[1], recall_at={1: 1.0}, rr=1.0, ndcg_at={1: 1.0})
        agg = aggregate([s], ks=[1, 5])
        self.assertAlmostEqual(agg["recall@1"], 1.0)
        self.assertAlmostEqual(agg["recall@5"], 0.0)


class TestLoadGolden(unittest.TestCase):
    def setUp(self):
        # Context-managed (via addCleanup) rather than a bare mkdtemp: these
        # tests write a file per case and a leaked dir per run adds up in
        # /var/folders on a machine that runs the suite all day.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._n = 0

    def _write(self, payload, *, raw=None):
        self._n += 1
        tmp = Path(self._tmp.name) / f"golden_{self._n}.json"
        tmp.write_text(json.dumps(payload) if raw is None else raw, encoding="utf-8")
        return tmp

    def test_round_trip(self):
        path = self._write({
            "schema_version": 1,
            "generated_at": "2026-07-26T10:00:00Z",
            "vault_path": "/Users/x/vault",
            "embed": "nomic-embed-text",
            "generator": {"provider": "ollama", "model": "m", "seed": 0},
            "queries": [
                {
                    "id": "q1",
                    "query": "insuffisance cardiaque à FEVG réduite",
                    "lang": "fr",
                    "relevant_sources": ["notes/café.md"],
                    "origin": "inverted",
                    "chunk_preview": "extrait",
                },
                {"id": "q2", "query": "heart failure", "lang": "en",
                 "relevant_sources": ["a.md", "b.md"], "origin": "curated"},
            ],
        })
        header, queries = load_golden(path)
        self.assertEqual(header["vault_path"], "/Users/x/vault")
        self.assertEqual(header["embed"], "nomic-embed-text")
        self.assertNotIn("queries", header)
        self.assertEqual([q.id for q in queries], ["q1", "q2"])
        self.assertEqual(queries[0].lang, "fr")
        self.assertEqual(queries[0].relevant_sources, ["notes/café.md"])
        self.assertEqual(queries[0].chunk_preview, "extrait")
        self.assertEqual(queries[1].origin, "curated")
        # Absent optional fields fall back to the documented defaults.
        self.assertEqual(queries[1].chunk_preview, "")

    def test_missing_queries_key_yields_empty_list(self):
        header, queries = load_golden(self._write({"schema_version": 1}))
        self.assertEqual(queries, [])
        self.assertEqual(header["schema_version"], 1)

    def test_wrong_schema_version_is_rejected(self):
        for bad in (2, 0, "1", None):
            path = self._write({"schema_version": bad, "queries": []})
            with self.assertRaises(ValueError):
                load_golden(path)

    def test_non_object_payload_is_rejected(self):
        path = self._write([{"id": "q1"}])
        with self.assertRaises(ValueError):
            load_golden(path)

    def test_non_object_query_entry_raises_valueerror_naming_the_index(self):
        # The realistic hand-edit corruption. It used to escape as a TypeError
        # past the runner's `except (OSError, ValueError, KeyError)` and print
        # a raw traceback; the contract is ValueError, and the message must
        # locate the entry in a 50-query file.
        for bad in ("oops", None, 5, ["a"]):
            path = self._write({"schema_version": 1, "queries": [
                {"id": "q1", "query": "ok", "relevant_sources": ["a.md"]},
                bad,
            ]})
            with self.assertRaises(ValueError) as ctx:
                load_golden(path)
            self.assertIn("[1]", str(ctx.exception), msg=repr(bad))

    def test_query_entry_missing_a_required_field_raises_valueerror(self):
        # Previously a KeyError — caught by the runner, but with no index in
        # the message. Now a ValueError naming both the index and the field.
        for missing in ("id", "query"):
            entry = {"id": "q1", "query": "ok"}
            entry.pop(missing)
            path = self._write({"schema_version": 1, "queries": [entry]})
            with self.assertRaises(ValueError) as ctx:
                load_golden(path)
            self.assertIn("[0]", str(ctx.exception))
            self.assertIn(missing, str(ctx.exception))

    def test_string_relevant_sources_is_rejected_not_split_into_characters(self):
        # "a.md" is iterable: the old comprehension produced four one-character
        # "documents", loading clean and then scoring 0.0 forever.
        path = self._write({"schema_version": 1, "queries": [
            {"id": "q1", "query": "ok", "relevant_sources": "a.md"},
        ]})
        with self.assertRaises(ValueError) as ctx:
            load_golden(path)
        self.assertIn("relevant_sources", str(ctx.exception))

    def test_malformed_entries_are_never_silently_skipped(self):
        # The documented alternative (skip-with-count) is deliberately NOT the
        # behaviour: a run that silently scored fewer queries is not comparable
        # to the baseline it is diffed against, and nothing records the loss.
        path = self._write({"schema_version": 1, "queries": [
            {"id": "q1", "query": "ok"}, "oops",
        ]})
        with self.assertRaises(ValueError):
            load_golden(path)

    def test_valid_entries_still_load_leniently(self):
        # The gate is about SHAPE only — values stay str()-coerced, so a
        # numeric id or a missing optional field is not an error.
        path = self._write({"schema_version": 1, "queries": [
            {"id": 7, "query": "ok", "relevant_sources": ["a.md"]},
        ]})
        _, queries = load_golden(path)
        self.assertEqual(queries[0].id, "7")
        self.assertEqual(queries[0].relevant_sources, ["a.md"])
        self.assertEqual(queries[0].origin, "inverted")


class TestFormatCellsTable(unittest.TestCase):
    def _cells(self):
        return [
            {"name": "baseline", "metrics": {"recall@1": 0.5, "recall@5": 0.8, "mrr": 0.6,
                                             "ndcg@10": 0.7, "n": 10}},
            {"name": "rerank-off", "metrics": {"recall@1": 0.4, "recall@5": 0.75, "mrr": 0.52,
                                               "ndcg@10": 0.65, "n": 10}},
        ]

    def test_baseline_row_has_no_delta_and_others_do(self):
        table = format_cells_table(self._cells())
        lines = table.splitlines()
        base_line = next(ln for ln in lines if ln.startswith("baseline "))
        off_line = next(ln for ln in lines if ln.startswith("rerank-off"))
        self.assertNotIn("(", base_line)
        self.assertIn("0.400 (-0.100)", off_line)
        self.assertIn("0.750 (-0.050)", off_line)
        self.assertIn("baseline: baseline", table)

    def test_columns_are_fixed_width_and_ordered(self):
        table = format_cells_table(self._cells())
        header = table.splitlines()[0].split()
        self.assertEqual(header, ["cell", "recall@1", "recall@5", "mrr", "ndcg@10", "n"])
        # Four metric columns carry a Δ; n is a count, printed plain.
        off_line = next(ln for ln in table.splitlines() if ln.startswith("rerank-off"))
        self.assertEqual(off_line.count("("), 4)
        self.assertTrue(off_line.rstrip().endswith("10"))
        # Fixed width: every body row is padded to the same length as the rule.
        rule = table.splitlines()[1]
        self.assertTrue(set(rule.replace(" ", "")) == {"-"})

    def test_explicit_baseline_selection(self):
        table = format_cells_table(self._cells(), baseline="rerank-off")
        base_line = next(ln for ln in table.splitlines() if ln.startswith("rerank-off"))
        other = next(ln for ln in table.splitlines() if ln.startswith("baseline "))
        self.assertNotIn("(", base_line)
        self.assertIn("0.500 (+0.100)", other)

    def test_unknown_baseline_falls_back_to_first_cell(self):
        table = format_cells_table(self._cells(), baseline="nope")
        self.assertIn("baseline: baseline", table)

    def test_empty_cells(self):
        self.assertEqual(format_cells_table([]), "(no cells)")


if __name__ == "__main__":
    unittest.main()
