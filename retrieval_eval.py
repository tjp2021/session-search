"""Reusable retrieval metrics for frozen public and private evaluations."""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from collections.abc import Iterable


@dataclasses.dataclass(frozen=True)
class QueryResult:
    query_id: str
    category: str
    relevant_rank: int | None


@dataclasses.dataclass(frozen=True)
class RetrievalSummary:
    queries: int
    top_one: float
    recall_at_five: float
    mrr_at_ten: float
    categories: dict[str, dict[str, float | int]]


def _metrics(results: list[QueryResult]) -> dict[str, float | int]:
    count = len(results)
    if not count:
        return {"queries": 0, "top_one": 0.0, "recall_at_five": 0.0, "mrr_at_ten": 0.0}
    top_one = sum(result.relevant_rank == 1 for result in results) / count
    recall = sum(
        result.relevant_rank is not None and result.relevant_rank <= 5
        for result in results
    ) / count
    reciprocal = sum(
        (1 / result.relevant_rank)
        if result.relevant_rank is not None and result.relevant_rank <= 10
        else 0.0
        for result in results
    ) / count
    return {
        "queries": count,
        "top_one": top_one,
        "recall_at_five": recall,
        "mrr_at_ten": reciprocal,
    }


def summarize_results(results: Iterable[QueryResult]) -> RetrievalSummary:
    rows = list(results)
    overall = _metrics(rows)
    grouped: dict[str, list[QueryResult]] = defaultdict(list)
    for result in rows:
        grouped[result.category].append(result)
    return RetrievalSummary(
        queries=int(overall["queries"]),
        top_one=float(overall["top_one"]),
        recall_at_five=float(overall["recall_at_five"]),
        mrr_at_ten=float(overall["mrr_at_ten"]),
        categories={name: _metrics(items) for name, items in sorted(grouped.items())},
    )
