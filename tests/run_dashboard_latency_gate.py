#!/usr/bin/env python3
"""Deterministic dashboard latency and forbidden-model-call gate."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import pathlib
import sqlite3
import statistics
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import session_search as ss


def measure(session_count: int, runs: int) -> dict[str, float | int]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ss.init_db(conn)
    now = 1_800_000_000
    documents = [
        ss.Document(
            doc_id=f"latency:{index}",
            source=ss.SUPPORTED_SOURCES[index % len(ss.SUPPORTED_SOURCES)],
            session_id=f"latency-session-{index}",
            title=f"Dashboard latency fixture {index}",
            path=f"/fixture/session-{index}.jsonl",
            cwd=f"/fixture/os/domain-{index % 20}/project-{index % 80}",
            role="user",
            ts=now - index,
            text=(
                f"Review dashboard latency fixture {index}. "
                "Continue with the deterministic acceptance check."
            ),
            meta={},
        )
        for index in range(session_count)
    ]
    ss.upsert_documents(conn, documents)

    model_calls = 0
    original_summarize = ss.llm_summarize

    def reject_model_call(*_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("dashboard called the model")

    ss.llm_summarize = reject_model_call
    timings: list[float] = []
    project_count = 0
    result_count = 0
    try:
        for _run in range(max(1, runs)):
            started = time.perf_counter()
            results = ss.recent_session_results(conn, 10)
            summaries = ss.dashboard_project_summaries(
                conn,
                source_name="all",
                thread_limit=10,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                ss.print_dashboard(
                    conn,
                    results,
                    project_summaries=summaries,
                )
            timings.append((time.perf_counter() - started) * 1000)
            project_count = len(summaries)
            result_count = len(results)
    finally:
        ss.llm_summarize = original_summarize
        conn.close()

    median_ms = statistics.median(timings)
    return {
        "sessions": session_count,
        "runs": len(timings),
        "results": result_count,
        "projects": project_count,
        "model_calls": model_calls,
        "median_ms": median_ms,
        "max_ms": max(timings),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=1000)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--max-median-ms", type=float, default=750.0)
    parser.add_argument("--max-run-ms", type=float, default=1500.0)
    parser.add_argument("--max-scale-ratio", type=float, default=6.0)
    args = parser.parse_args()

    large = measure(max(4, args.sessions), args.runs)
    baseline = measure(max(1, args.sessions // 4), args.runs)
    scale_ratio = float(large["median_ms"]) / max(
        0.001,
        float(baseline["median_ms"]),
    )
    evidence = {
        "schema_version": 1,
        "input_sessions": large["sessions"],
        "baseline_sessions": baseline["sessions"],
        "runs": large["runs"],
        "dashboard_results": large["results"],
        "project_summaries": large["projects"],
        "model_calls": int(large["model_calls"]) + int(baseline["model_calls"]),
        "median_ms": round(float(large["median_ms"]), 3),
        "max_ms": round(float(large["max_ms"]), 3),
        "baseline_median_ms": round(float(baseline["median_ms"]), 3),
        "scale_ratio": round(scale_ratio, 3),
        "budget_ms": args.max_median_ms,
        "max_run_budget_ms": args.max_run_ms,
        "max_scale_ratio": args.max_scale_ratio,
    }
    print(json.dumps(evidence, indent=2, sort_keys=True))
    if evidence["model_calls"]:
        return 1
    if large["results"] != min(10, args.sessions):
        return 1
    if float(large["median_ms"]) > args.max_median_ms:
        return 1
    if float(large["max_ms"]) > args.max_run_ms:
        return 1
    if scale_ratio > args.max_scale_ratio:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
