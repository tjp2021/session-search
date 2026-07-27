import unittest

from retrieval_eval import QueryResult, summarize_results


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


if __name__ == "__main__":
    unittest.main()
