#!/usr/bin/env python3
"""Report local adapter drift without printing session text or store paths."""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import session_search as ss


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default="~")
    parser.add_argument(
        "--source",
        default="all",
        choices=["all", *ss.SUPPORTED_SOURCES],
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail when a candidate store produces zero documents.",
    )
    args = parser.parse_args()
    sources = ss.normalize_sources(args.source)
    results = ss.adapter_health(ss.expand(args.home), sources)
    print(
        json.dumps(
            {
                "schema_version": 1,
                "adapters": [dataclasses.asdict(result) for result in results],
            },
            indent=2,
            sort_keys=True,
        )
    )
    drifted = any(
        result.status in {"candidate_store_zero_content", "partial_store_drift"}
        for result in results
    )
    return 1 if args.strict and drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())
