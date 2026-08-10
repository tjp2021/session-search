#!/usr/bin/env python3
"""Reproducible synthetic performance benchmark for public evidence."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import platform
import sqlite3
import statistics
import sys
import time

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import session_search as ss
from public_corpus import documents, load_corpus


def scaled_documents(session_count: int) -> list[ss.Document]:
    base = list(documents(load_corpus()))
    rows: list[ss.Document] = []
    for index in range(session_count):
        original = base[index % len(base)]
        cycle = index // len(base)
        suffix = f":scale:{cycle}" if cycle else ""
        rows.append(
            dataclasses.replace(
                original,
                doc_id=f"{original.doc_id}{suffix}",
                session_id=f"{original.session_id}{suffix}",
                ts=(original.ts or 0) - cycle,
            )
        )
    return rows


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def timed(callable_) -> tuple[object, float]:
    started = time.perf_counter()
    result = callable_()
    return result, (time.perf_counter() - started) * 1000


def run(session_count: int, semantic: bool) -> dict[str, object]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ss.init_db(conn)
    rows = scaled_documents(session_count)
    _result, cold_index_ms = timed(lambda: ss.upsert_documents(conn, rows))
    _result, incremental_refresh_ms = timed(lambda: ss.upsert_documents(conn, rows))
    _result, card_generation_ms = timed(lambda: ss.ensure_session_cards(conn, quiet=True))
    if semantic:
        _result, embedding_ms = timed(
            lambda: (
                ss.ensure_session_embeddings(conn, quiet=True),
                ss.ensure_embeddings(conn, quiet=True),
            )
        )
    else:
        embedding_ms = 0.0

    dashboard_rows = ss.recent_session_results(conn, 10)
    started = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        ss.print_dashboard(conn, dashboard_rows)
    dashboard_ms = (time.perf_counter() - started) * 1000

    corpus = load_corpus()
    queries = [
        str(topic[variant])
        for topic in corpus["topics"][:5]
        for variant in ("exact", "fuzzy", "semantic", "time")
    ]
    modes = ("fts", "local", "hybrid") if semantic else ("fts", "local")
    timings: dict[str, dict[str, float]] = {}
    for mode in modes:
        values: list[float] = []
        for query in queries:
            _result, elapsed = timed(lambda q=query, m=mode: ss.run_search(conn, q, 10, "all", m))
            values.append(elapsed)
        timings[mode] = {
            "p50_ms": round(statistics.median(values), 3),
            "p95_ms": round(percentile(values, 0.95), 3),
        }
    conn.close()
    return {
        "sessions": session_count,
        "documents": len(rows),
        "cold_index_ms": round(cold_index_ms, 3),
        "incremental_refresh_ms": round(incremental_refresh_ms, 3),
        "card_generation_ms": round(card_generation_ms, 3),
        "embedding_ms": round(embedding_ms, 3),
        "dashboard_ms": round(dashboard_ms, 3),
        "queries": timings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, nargs="+", default=[100, 1000])
    parser.add_argument("--semantic", action="store_true")
    parser.add_argument("--expected", type=pathlib.Path)
    parser.add_argument("--max-regression-factor", type=float, default=6.0)
    args = parser.parse_args()
    report = {
        "schema_version": 1,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "runs": [run(count, args.semantic) for count in args.sessions],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.expected:
        expected = json.loads(args.expected.read_text(encoding="utf-8"))
        expected_runs = {int(run["sessions"]): run for run in expected["runs"]}
        failures: list[str] = []
        for measured_run in report["runs"]:
            baseline = expected_runs.get(int(measured_run["sessions"]))
            if baseline is None:
                failures.append(
                    f"no evidence baseline for {measured_run['sessions']} sessions"
                )
                continue
            for mode, timings in measured_run["queries"].items():
                if mode not in baseline["queries"]:
                    failures.append(
                        f"no {mode} evidence for {measured_run['sessions']} sessions"
                    )
                    continue
                for metric in ("p50_ms", "p95_ms"):
                    limit = max(
                        50.0,
                        float(baseline["queries"][mode][metric])
                        * args.max_regression_factor,
                    )
                    if float(timings[metric]) > limit:
                        failures.append(
                            f"{measured_run['sessions']} {mode} {metric}={timings[metric]} exceeds {limit:.3f}"
                        )
            if args.semantic and float(measured_run["embedding_ms"]) > 30000:
                failures.append(
                    f"{measured_run['sessions']} embedding_ms={measured_run['embedding_ms']} exceeds 30000"
                )
        if failures:
            print("Performance evidence gate failed:", file=sys.stderr)
            for failure in failures:
                print(f"- {failure}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
