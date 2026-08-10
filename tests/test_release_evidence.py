from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_human_usability_gate", ROOT / "tests" / "run_human_usability_gate.py"
)
assert SPEC and SPEC.loader
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

BENCHMARK_SPEC = importlib.util.spec_from_file_location(
    "benchmark_public", ROOT / "tests" / "benchmark_public.py"
)
assert BENCHMARK_SPEC and BENCHMARK_SPEC.loader
BENCHMARK = importlib.util.module_from_spec(BENCHMARK_SPEC)
BENCHMARK_SPEC.loader.exec_module(BENCHMARK)


def passing_records() -> list[dict[str, object]]:
    return [
        {
            "participant": f"P{participant:02d}",
            "task": task,
            "completed_without_help": True,
            "elapsed_seconds": 60,
            "unintended_archive_change": False,
            "unsafe_access": False,
        }
        for participant in range(1, 6)
        for task in range(1, 8)
    ]


class HumanUsabilityGateTest(unittest.TestCase):
    def test_complete_five_person_pilot_passes(self):
        self.assertEqual(GATE.evaluate({"records": passing_records()}), [])

    def test_missing_participant_and_unsafe_access_fail(self):
        records = passing_records()[:-7]
        records[0]["unsafe_access"] = True
        failures = GATE.evaluate({"records": records})
        self.assertTrue(any("5 participants" in item for item in failures))
        self.assertTrue(any("unsafe" in item for item in failures))


class PerformanceEvidenceGateTest(unittest.TestCase):
    def test_embedding_limit_uses_the_regression_factor(self):
        self.assertAlmostEqual(
            BENCHMARK.regression_limit(6807.257, 6.0, 30000.0),
            40843.542,
        )


if __name__ == "__main__":
    unittest.main()
