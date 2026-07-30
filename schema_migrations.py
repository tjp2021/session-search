"""Versioned and validated SS schema migrations."""

from __future__ import annotations

import hashlib
import sqlite3

CURRENT_SCHEMA_VERSION = 5


class MigrationFailure(RuntimeError):
    pass


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def preflight_schema(conn: sqlite3.Connection) -> None:
    current = schema_version(conn)
    if current > CURRENT_SCHEMA_VERSION:
        raise MigrationFailure(
            f"database schema v{current} is newer than supported v{CURRENT_SCHEMA_VERSION}"
        )
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    if current > 0 and not {"documents", "documents_fts", "session_archive_status"} <= tables:
        raise MigrationFailure(f"schema v{current} is missing required tables")
    if "session_archive_status" in tables:
        base = {"source", "session_id", "archived", "origin", "status_at", "evidence_ts", "evidence"}
        if not base <= table_columns(conn, "session_archive_status"):
            raise MigrationFailure("legacy archive status schema is incompatible")


def execute_statements(conn: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            statement = ""
            if sql:
                conn.execute(sql)
    if statement.strip():
        raise MigrationFailure("incomplete schema statement")


def validate_schema(conn: sqlite3.Connection) -> None:
    required = {
        "documents": {"doc_id", "source", "session_id", "text", "indexed_at"},
        "session_archive_status": {
            "source", "session_id", "archived", "origin", "status_at",
            "evidence", "evidence_doc_id", "parser_version",
        },
        "session_archive_events": {"event_id", "source", "session_id", "archived"},
        "session_archive_meta": {"key", "value"},
        "schema_migrations": {"version", "checksum", "applied_at"},
        "session_cards": {
            "source", "session_id", "text_hash", "what_this_was",
            "next_clue", "summary_source", "built_at",
        },
    }
    for table, columns in required.items():
        actual = table_columns(conn, table)
        if not columns <= actual:
            raise MigrationFailure(f"{table} is missing columns: {sorted(columns - actual)}")
    fts = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'documents_fts' AND type = 'table'"
    ).fetchone()
    if not fts or "VIRTUAL TABLE" not in str(fts[0]).upper() or "FTS5" not in str(fts[0]).upper():
        raise MigrationFailure("documents_fts is not an FTS5 virtual table")


def finish_migration(conn: sqlite3.Connection, schema_text: str, applied_at: int) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at INTEGER NOT NULL
        )
        """
    )
    validate_schema(conn)
    checksum = hashlib.sha256(schema_text.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT OR REPLACE INTO schema_migrations(version, checksum, applied_at) VALUES(?, ?, ?)",
        (CURRENT_SCHEMA_VERSION, checksum, applied_at),
    )
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")


def validate_current_schema(conn: sqlite3.Connection) -> None:
    preflight_schema(conn)
    if schema_version(conn) == CURRENT_SCHEMA_VERSION:
        validate_schema(conn)
