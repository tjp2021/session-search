#!/usr/bin/env python3
"""Run frozen retrieval evaluation against deterministic synthetic sessions."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import session_search as ss
from public_corpus import corpus_path, documents, load_corpus
from retrieval_eval import QueryResult, summarize_results


VARIANTS = ("exact", "fuzzy", "semantic", "time")


def build_connection(payload: dict[str, object], semantic: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ss.init_db(conn)
    ss.upsert_documents(conn, documents(payload))
    if semantic:
        ss.ensure_session_embeddings(conn, quiet=True)
        ss.ensure_embeddings(conn, quiet=True)
    return conn


def evaluate_mode(payload: dict[str, object], mode: str) -> dict[str, object]:
    conn = build_connection(payload, semantic=mode == "hybrid")
    results: list[QueryResult] = []
    try:
        for topic in payload["topics"]:
            assert isinstance(topic, dict)
            target = str(topic["id"])
            for category in VARIANTS:
                query_id = f"{target}:{category}"
                _display, matches = ss.run_search(
                    conn,
                    str(topic[category]),
                    10,
                    "all",
                    mode,
                )
                rank = next(
                    (
                        index
                        for index, (row, _score, _label) in enumerate(matches, 1)
                        if str(row["session_id"]) == target
                    ),
                    None,
                )
                results.append(QueryResult(query_id, category, rank))
    finally:
        conn.close()
    return dataclasses.asdict(summarize_results(results))


def evidence_mismatch(
    report: dict[str, object],
    expected: dict[str, object],
) -> str | None:
    if report["schema_version"] != expected.get("schema_version"):
        return "schema_version changed"
    if report["corpus"] != expected.get("corpus"):
        return "corpus metadata changed"
    expected_modes = expected.get("modes")
    if not isinstance(expected_modes, dict):
        return "expected evidence has no modes object"
    report_modes = report["modes"]
    assert isinstance(report_modes, dict)
    for mode, metrics in report_modes.items():
        if expected_modes.get(mode) != metrics:
            return f"{mode} retrieval metrics changed"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        default="all",
        choices=["all", "fts", "local", "hybrid"],
    )
    parser.add_argument(
        "--expected",
        type=pathlib.Path,
        help="Fail if the selected modes differ from this evidence file.",
    )
    args = parser.parse_args()
    payload = load_corpus()
    modes = ("fts", "local", "hybrid") if args.mode == "all" else (args.mode,)
    report = {
        "schema_version": 1,
        "corpus": {
            "sessions": int(payload["sessions"]),
            "queries": int(payload["queries"]),
            "sha256": hashlib.sha256(corpus_path().read_bytes()).hexdigest(),
        },
        "modes": {mode: evaluate_mode(payload, mode) for mode in modes},
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.expected:
        expected = json.loads(args.expected.read_text(encoding="utf-8"))
        mismatch = evidence_mismatch(report, expected)
        if mismatch:
            print(f"Retrieval evidence mismatch: {mismatch}", file=sys.stderr)
            return 1
    if "hybrid" not in report["modes"]:
        return 0
    hybrid = report["modes"]["hybrid"]
    return 0 if (
        hybrid["top_one"] >= 0.85
        and hybrid["recall_at_five"] >= 0.95
        and hybrid["mrr_at_ten"] >= 0.90
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
