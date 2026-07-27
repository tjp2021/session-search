"""Deterministic synthetic corpus shared by evaluation, benchmarks, and demos."""

from __future__ import annotations

import json
import pathlib
import sysconfig
from collections.abc import Iterator
from typing import Any

from session_search import Document, now_ts


def corpus_path() -> pathlib.Path:
    local = pathlib.Path(__file__).resolve().with_name("evals") / "public-retrieval-corpus.json"
    if local.exists():
        return local
    installed = (
        pathlib.Path(sysconfig.get_path("data"))
        / "share"
        / "session-search"
        / "evals"
        / "public-retrieval-corpus.json"
    )
    if installed.exists():
        return installed
    raise FileNotFoundError("Public retrieval corpus is missing from the installation.")


def load_corpus() -> dict[str, Any]:
    return json.loads(corpus_path().read_text(encoding="utf-8"))


def documents(payload: dict[str, Any] | None = None) -> Iterator[Document]:
    corpus = payload or load_corpus()
    timestamp = now_ts() - 60
    for topic_index, topic in enumerate(corpus["topics"]):
        target_id = str(topic["id"])
        source = str(topic["source"])
        yield Document(
            doc_id=f"{target_id}:target",
            source=source,
            session_id=target_id,
            title=str(topic["title"]),
            path=f"/synthetic/{source}/{target_id}.jsonl",
            cwd=f"/synthetic/projects/{target_id}",
            role="user",
            ts=timestamp + topic_index,
            text=f"{topic['title']}. {topic['body']}",
            meta={"synthetic": True},
        )
        for distractor in range(5):
            session_id = f"{target_id}:confuser:{distractor}"
            yield Document(
                doc_id=session_id,
                source=("claude", "codex", "vscode", "cursor")[distractor % 4],
                session_id=session_id,
                title=f"General maintenance batch {topic_index}-{distractor}",
                path=f"/synthetic/confusers/{session_id}.jsonl",
                cwd="/synthetic/projects/maintenance",
                role="user",
                ts=timestamp - 100_000 - (topic_index * 10) - distractor,
                text=(
                    "Reviewed routine configuration, documentation, cleanup, "
                    "and unrelated maintenance tasks without the target project."
                ),
                meta={"synthetic": True, "confuser": True},
            )
