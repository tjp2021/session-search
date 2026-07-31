import importlib.util
import pathlib
import unittest

from retrieval_eval import QueryResult, summarize_results


SCRIPT_PATH = pathlib.Path(__file__).with_name("run_public_retrieval_eval.py")
SPEC = importlib.util.spec_from_file_location("run_public_retrieval_eval", SCRIPT_PATH)
assert SPEC and SPEC.loader
RUN_PUBLIC_RETRIEVAL_EVAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN_PUBLIC_RETRIEVAL_EVAL)


class RetrievalMetricTest(unittest.TestCase):
    def test_summary_reports_top_one_recall_five_and_mrr_ten(self):
        summary = summarize_results(
            [
                QueryResult("q1", "exact", 1),
                QueryResult("q2", "fuzzy", 2),
                QueryResult("q3", "semantic", 6),
                QueryResult("q4", "semantic", None),
            ]
        )
        self.assertEqual(summary.queries, 4)
        self.assertEqual(summary.top_one, 0.25)
        self.assertEqual(summary.recall_at_five, 0.5)
        self.assertAlmostEqual(summary.mrr_at_ten, (1 + 0.5 + (1 / 6)) / 4)
        self.assertEqual(summary.categories["semantic"]["queries"], 2)

    def test_empty_input_is_safe(self):
        summary = summarize_results([])
        self.assertEqual(summary.queries, 0)
        self.assertEqual(summary.top_one, 0.0)
        self.assertEqual(summary.recall_at_five, 0.0)
        self.assertEqual(summary.mrr_at_ten, 0.0)

    def test_evidence_gate_accepts_selected_mode_only(self):
        report = {
            "schema_version": 1,
            "corpus": {"sessions": 120, "queries": 80},
            "modes": {"hybrid": {"top_one": 0.9}},
        }
        expected = {
            **report,
            "modes": {
                "fts": {"top_one": 0.6},
                "hybrid": {"top_one": 0.9},
            },
        }
        self.assertIsNone(
            RUN_PUBLIC_RETRIEVAL_EVAL.evidence_mismatch(report, expected)
        )

    def test_evidence_gate_rejects_metric_drift(self):
        report = {
            "schema_version": 1,
            "corpus": {"sessions": 120, "queries": 80},
            "modes": {"hybrid": {"top_one": 0.89}},
        }
        expected = {
            **report,
            "modes": {"hybrid": {"top_one": 0.9}},
        }
        self.assertEqual(
            RUN_PUBLIC_RETRIEVAL_EVAL.evidence_mismatch(report, expected),
            "hybrid retrieval metrics changed",
        )


if __name__ == "__main__":
    unittest.main()
