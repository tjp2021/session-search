#!/usr/bin/env python3
"""One-shot aggregate evaluator for a private archive-intent holdout."""

import hashlib
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from archive_intent import classify_session_intent


def main() -> int:
    path = pathlib.Path(os.environ["SS_SEALED_HOLDOUT"])
    expected_hash = os.environ["SS_SEALED_HOLDOUT_SHA256"]
    payload_bytes = path.read_bytes()
    actual_hash = hashlib.sha256(payload_bytes).hexdigest()
    if actual_hash != expected_hash:
        print("sealed holdout hash mismatch", file=sys.stderr)
        return 2
    cases = json.loads(payload_bytes)["cases"]
    totals = {"close": 0, "resume": 0, "none": 0}
    correct = {"close": 0, "resume": 0, "none": 0}
    false_positives = 0
    for case in cases:
        expected = case["expected"]
        actual = classify_session_intent(case["text"]).kind.value
        totals[expected] += 1
        correct[expected] += int(actual == expected)
        false_positives += int(expected == "none" and actual != "none")
    close_recall = correct["close"] / totals["close"] if totals["close"] else 1.0
    resume_recall = correct["resume"] / totals["resume"] if totals["resume"] else 1.0
    result = {
        "cases": len(cases),
        "false_positives": false_positives,
        "close_recall": round(close_recall, 4),
        "resume_recall": round(resume_recall, 4),
    }
    print(json.dumps(result, indent=2))
    return 0 if false_positives == 0 and close_recall >= 0.95 and resume_recall >= 0.95 else 1


if __name__ == "__main__":
    raise SystemExit(main())
