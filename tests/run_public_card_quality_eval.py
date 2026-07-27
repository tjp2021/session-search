#!/usr/bin/env python3
"""Evaluate deterministic session-card cleanup against frozen public cases."""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from card_quality import quality_gate


def main() -> int:
    corpus = json.loads(
        (ROOT / "evals" / "public-card-quality-corpus.json").read_text(encoding="utf-8")
    )
    failures: list[dict[str, object]] = []
    for case in corpus["cases"]:
        actual = list(quality_gate(*case["input"]).__dict__.values())
        if actual != case["expected"]:
            failures.append(
                {"id": case["id"], "expected": case["expected"], "actual": actual}
            )
    result = {
        "version": corpus["version"],
        "cases": len(corpus["cases"]),
        "passed": len(corpus["cases"]) - len(failures),
        "failed": len(failures),
        "failures": failures,
    }
    print(json.dumps(result, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
