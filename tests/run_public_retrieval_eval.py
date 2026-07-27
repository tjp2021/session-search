#!/usr/bin/env python3
"""Run frozen retrieval evaluation against deterministic synthetic sessions."""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import session_search as ss
from public_corpus import documents, load_corpus
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        default="all",
        choices=["all", "fts", "local", "hybrid"],
    )
    args = parser.parse_args()
    payload = load_corpus()
    modes = ("fts", "local", "hybrid") if args.mode == "all" else (args.mode,)
    report = {
        "schema_version": 1,
        "corpus": {
            "sessions": int(payload["sessions"]),
            "queries": int(payload["queries"]),
        },
        "modes": {mode: evaluate_mode(payload, mode) for mode in modes},
    }
    print(json.dumps(report, indent=2, sort_keys=True))
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
