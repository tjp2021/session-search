#!/usr/bin/env python3
"""Evaluate deterministic session-card cleanup against frozen public cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from card_quality import quality_gate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", type=pathlib.Path)
    args = parser.parse_args()
    corpus_path = ROOT / "evals" / "public-card-quality-corpus.json"
    corpus_bytes = corpus_path.read_bytes()
    corpus = json.loads(corpus_bytes)
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
        "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
    }
    print(json.dumps(result, indent=2))
    if failures:
        return 1
    if args.expected:
        expected = json.loads(args.expected.read_text(encoding="utf-8"))
        comparable = {
            "corpus_version": result["version"],
            "cases": result["cases"],
            "passed": result["passed"],
            "failed": result["failed"],
            "corpus_sha256": result["corpus_sha256"],
        }
        if comparable != {key: expected.get(key) for key in comparable}:
            print("card-quality evidence mismatch", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
