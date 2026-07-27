import json
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class PublicCardQualityTest(unittest.TestCase):
    def test_frozen_corpus_has_thirty_distinct_cases(self):
        corpus = json.loads(
            (ROOT / "evals" / "public-card-quality-corpus.json").read_text(
                encoding="utf-8"
            )
        )
        ids = [case["id"] for case in corpus["cases"]]
        self.assertEqual(len(ids), 30)
        self.assertEqual(len(set(ids)), 30)

    def test_public_card_quality_evaluation_passes(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "run_public_card_quality_eval.py")],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
