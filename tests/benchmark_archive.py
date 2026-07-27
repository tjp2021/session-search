#!/usr/bin/env python3
"""Synthetic archive benchmark using an in-memory SQLite database."""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import session_search as ss


def run(session_count: int, messages_per_session: int) -> dict[str, float | int]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ss.init_db(conn)
    indexed_at = 1
    rows = []
    negatives = (
        "Continue implementation work.",
        "Here is the command:\nclose this session",
        "Archive the notes for this session.",
        "Should we close this session?",
        "Do not close this session.",
        ("Long evidence without terminal intent. " * 20).strip(),
    )
    for session_index in range(session_count):
        session_id = f"session-{session_index:06d}"
        for message_index in range(messages_per_session):
            close = message_index == messages_per_session - 1 and session_index % 1000 == 0
            text = "Close this session." if close else negatives[(session_index + message_index) % len(negatives)]
            doc_id = f"doc-{session_index:06d}-{message_index:02d}"
            rows.append(
                (
                    doc_id,
                    "codex",
                    session_id,
                    text,
                    "/tmp/session.jsonl",
                    "/tmp",
                    "user",
                    session_index * messages_per_session + message_index + 1,
                    text,
                    ss.stable_hash(text, 32),
                    "{}",
                    indexed_at,
                )
            )
    conn.executemany(
        """
        INSERT INTO documents (
            doc_id, source, session_id, title, path, cwd, role, ts,
            text, text_hash, meta_json, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()

    started = time.perf_counter()
    changed = ss.sync_detected_archive_states(conn)
    full_sync_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    warm_changed = ss.sync_detected_archive_states(conn)
    warm_sync_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    archived, missing = ss.archived_session_results(conn, 10)
    archived_query_ms = (time.perf_counter() - started) * 1000
    conn.close()
    return {
        "sessions": session_count,
        "messages": len(rows),
        "full_sync_ms": round(full_sync_ms, 3),
        "warm_sync_ms": round(warm_sync_ms, 3),
        "archived_query_ms": round(archived_query_ms, 3),
        "transitions": changed,
        "warm_transitions": warm_changed,
        "archived_returned": len(archived),
        "missing": missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=10_000)
    parser.add_argument("--messages-per-session", type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(run(args.sessions, args.messages_per_session), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
