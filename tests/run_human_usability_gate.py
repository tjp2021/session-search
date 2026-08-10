#!/usr/bin/env python3
"""Validate sanitized results from the required five-person usability pilot."""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys


TASKS = set(range(1, 8))


def evaluate(payload: object) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        return ["results must contain a records list"]
    records = payload["records"]
    participants = {str(row.get("participant")) for row in records if isinstance(row, dict)}
    failures: list[str] = []
    if len(participants) < 5:
        failures.append(f"expected at least 5 participants, found {len(participants)}")
    complete = 0
    for participant in participants:
        rows = [row for row in records if str(row.get("participant")) == participant]
        tasks = {int(row.get("task", 0)) for row in rows}
        if tasks == TASKS and all(bool(row.get("completed_without_help")) for row in rows):
            complete += 1
    if complete < 4:
        failures.append(f"only {complete} participants completed every task without help")
    for task in sorted(TASKS):
        rows = [row for row in records if int(row.get("task", 0)) == task]
        if len(rows) < 5:
            failures.append(f"task {task} has only {len(rows)} participant records")
            continue
        success = sum(bool(row.get("completed_without_help")) for row in rows) / len(rows)
        if success < 0.8:
            failures.append(f"task {task} unassisted success is {success:.0%}")
        elapsed = [float(row.get("elapsed_seconds", 0)) for row in rows]
        if statistics.median(elapsed) >= 120:
            failures.append(f"task {task} median time is {statistics.median(elapsed):g} seconds")
    for row in records:
        if bool(row.get("unintended_archive_change")) or bool(row.get("unsafe_access")):
            failures.append("a participant caused an unsafe state change or access")
            break
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=pathlib.Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.results.read_text(encoding="utf-8"))
    failures = evaluate(payload)
    if failures:
        print("Human usability gate failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("PASS: human usability pilot meets every release threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
