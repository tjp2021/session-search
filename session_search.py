#!/usr/bin/env python3
"""Cross-tool local AI session search.

Indexes local session stores into a private SQLite FTS database. The importer is
intentionally conservative: it extracts text-like fields and skips binary,
encrypted, auth, and media payloads.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import functools
import glob
import hashlib
import itertools
import json
import math
import os
import pathlib
import re
import shlex
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from typing import Any, Iterable, Iterator

from archive_intent import PARSER_VERSION as ARCHIVE_INTENT_VERSION
from archive_intent import IntentKind, classify_session_intent, has_session_intent_candidate
from archive_store import StorageFailure, backup_database, immediate_transaction, quick_check
from adapter_capabilities import render_capabilities
from card_quality import quality_gate
from platform_lock import LockTimeout, UnsupportedPlatform, file_lock
from schema_migrations import (
    CURRENT_SCHEMA_VERSION,
    MigrationFailure,
    execute_statements,
    finish_migration,
    preflight_schema,
    schema_version,
    validate_current_schema,
)
from ss_config import resolve_paths


_DEFAULT_PATHS = resolve_paths()
DEFAULT_DB = str(_DEFAULT_PATHS.db)
DEFAULT_LAST_RESULTS = str(_DEFAULT_PATHS.last_results)
DEFAULT_LOCK = str(_DEFAULT_PATHS.lock)
DEFAULT_HANDOFF_DIR = str(_DEFAULT_PATHS.handoff_dir)
DEFAULT_MODEL_CACHE = str(_DEFAULT_PATHS.model_cache)
DEFAULT_EVALS = pathlib.Path(__file__).resolve().with_name("evals") / "session-search-evals.json"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_MODEL_VERSION = "fastembed:" + EMBED_MODEL
CARD_VERSION = "local-card-v9"
EMBED_TEXT_CHARS = 5000
MAX_DOC_CHARS = 16000
CHUNK_OVERLAP = 800
HANDOFF_MAX_ROWS = 24
HANDOFF_MAX_ROW_CHARS = 1800

TEXT_KEYS = {
    "text",
    "lastPrompt",
    "customTitle",
    "inputText",
    "response",
    "value",
}

SKIP_KEYS = {
    "auth",
    "authorization",
    "data",
    "encrypted",
    "image",
    "key",
    "password",
    "raw",
    "secret",
    "source",
    "token",
}

CHAT_KEY_HINTS = (
    "chat",
    "copilot",
    "interactive-session",
    "agentSessions",
    "claude",
    "continue",
)

ALIASES = {
    "c#": ("csharp", "c3"),
    "csharp": ("c#", "c3"),
    "c3": ("c#", "csharp"),
    "vs": ("vscode", "visualstudio"),
    "vscode": ("vs", "visualstudio"),
    "copilot": ("githubcopilot",),
    "ohmni": ("omni", "ohmnibot"),
    "omni": ("ohmni", "ohmnibot"),
}
IMPORTANT_SHORT_TERMS = {
    "ai",
    "c#",
    "c++",
    "c3",
    "go",
    "js",
    "ml",
    "p0",
    "p1",
    "p2",
    "p3",
    "qa",
    "ts",
    "ui",
    "ux",
}
ACTION_ANCHOR_TERMS = {
    "build",
    "class",
    "cloudflare",
    "deploy",
    "deployment",
    "fix",
    "login",
    "open",
    "push",
    "redeploy",
    "resume",
    "signin",
    "structure",
    "vercel",
}

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+#.-]*", re.I)
BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{300,}={0,2}")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
STOP_WORDS = {
    "a",
    "about",
    "am",
    "and",
    "are",
    "did",
    "do",
    "doing",
    "for",
    "from",
    "had",
    "have",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "what",
    "where",
    "with",
    "work",
    "working",
}

ACTION_WORD_RE = re.compile(
    r"\b(?:built|changed|checked|confirmed|created|deployed|fixed|found|implemented|"
    r"moved|opened|pushed|ran|updated|verified|wrote)\b",
    re.I,
)
REPORTED_CHANGE_RE = re.compile(
    r"\b(?:what\s+i\s+changed|changed\s+locally|added\s+|updated\s+|deployed|"
    r"pushed\s+to|build\s+artifact\s+confirms|verified)\b",
    re.I,
)
NEXT_WORD_RE = re.compile(
    r"\b(?:next|todo|to-do|need(?:s|ed)?\s+to|should|continue|before|blocked|"
    r"waiting|check|verify|run|deploy|redeploy|open|ask|follow\s+up|resume)\b",
    re.I,
)
GOAL_WORD_RE = re.compile(
    r"\b(?:task|goal|objective|need\s+to|i\s+need|we\s+need|working\s+on|work\s+on|"
    r"teach\s+me|learn|build|create|design|deploy|fix|rewrite|implement|resume)\b",
    re.I,
)
TASK_CLAUSE_RE = re.compile(r"\b(?:task|goal|objective|your\s+task)\s*:\s*(.+)", re.I)
TASK_STOP_RE = re.compile(
    r"\b(?:do\s+not|don't|requirements?|final\s+response|return\s+only|write\s+scope|"
    r"own\s+only|you\s+are\s+not\s+alone|context:)\b",
    re.I,
)
LOW_SIGNAL_SESSION_LINE_RE = re.compile(
    r"\b(?:this\s+session\s+is\s+being\s+continued|ran\s+out\s+of\s+context|"
    r"seven\s+commits\s+today|it\s+worked|pushed\s+as|verified:|summary below covers|"
    r"read\s+but\s+not\s+modified)\b",
    re.I,
)
CONFIG_LINE_RE = re.compile(r"^[A-Z][A-Z0-9_]{3,}\s*=")
SUMMARY_TAG_RE = re.compile(r"^<summary>.*</summary>$", re.I)
PATH_RE = re.compile(
    r"(?:/Users/[^\s'\"<>`]+|(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]+|"
    r"[\w.-]+\.(?:cs|css|html|js|json|jsx|md|py|sh|sql|toml|ts|tsx|yaml|yml))"
)
NOISE_LINE_RE = re.compile(
    r"^(?:<local-command-|<command-|Traceback\b|File \"/|usage: |\[osmo:|Output:|Chunk ID:|"
    r"python3 -m py_compile\b)",
    re.I,
)
INJECTED_USER_MESSAGE_RE = re.compile(
    r"^\s*(?:Base directory for this skill:|#\s*AGENTS\.md\s+instructions\b|<(?:environment_context|permissions instructions|"
    r"collaboration_mode|apps_instructions|plugins_instructions|skills_instructions)\b)",
    re.I,
)
CORPUS_DUMP_RE = re.compile(
    r"\b(?:full backup of a local|contains live secrets|inventory dump mentioning|"
    r"scraped page text|repository-wide corpus dump|bulk archive dump)\b",
    re.I,
)
CORPUS_DUMP_QUERY_RE = re.compile(r"\b(?:backup|corpus|inventory|archive dump|scraped page|secrets?)\b", re.I)
META_QUERY_RE = re.compile(
    r"\b(?:ss|session\s+search|search\s+tool|result\s+card|result\s+shape|"
    r"context\s+packet|semantic\s+command|natural\s+command)\b",
    re.I,
)
META_SESSION_RE = re.compile(
    r"\b(?:session\s+search\s+tool|session_search\.py|ss\s+--|ss\s+open|ss\s+continue|"
    r"ss\s+handoff|native\s+resume|cross-tool\s+resume|result\s+shape|"
    r"semantic\s+command|natural\s+command|context\s+packet|find\s+the\s+session|"
    r"find\s+that\s+session|resume\s+that\s+session|which\s+session|example\s+shape|"
    r"match:\s+hybrid)\b",
    re.I,
)
SIDE_TASK_SESSION_RE = re.compile(
    r"\b(?:bounded\s+sidecar\s+task|subagent|you\s+are\s+worker\s+[a-z0-9]+|"
    r"do\s+not\s+edit\s+files|quickly\s+review\s+only|"
    r"produce\s+a\s+concise|inspect\s+the\s+current\s+repo\s+state|you\s+own\s+only)\b",
    re.I,
)
SIDE_TASK_QUERY_RE = re.compile(
    r"\b(?:sidecar|subagent|checklist|review|audit|p0|p1|p2|p3|blocker|production\s+risk)\b",
    re.I,
)


@dataclasses.dataclass(frozen=True)
class Document:
    doc_id: str
    source: str
    session_id: str
    title: str
    path: str
    cwd: str
    role: str
    ts: int | None
    text: str
    meta: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class EvidenceLine:
    label: str
    doc_id: str
    role: str
    ts: int | None
    text: str


@dataclasses.dataclass(frozen=True)
class SessionCard:
    title: str
    source: str
    session_id: str
    repo: str
    last_active: int | None
    last_user_message: str
    what_this_was: str
    what_happened: str
    next_clue: str
    mentioned_paths: tuple[str, ...]
    evidence: tuple[EvidenceLine, ...]


def expand(path: str | pathlib.Path) -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(str(path))).resolve()


def now_ts() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def parse_ts(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n > 10_000_000_000:
            n = n / 1000
        return int(n)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if value.isdigit():
            return parse_ts(int(value))
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp())
    return None


def iso_date(ts: int | None) -> str:
    if not ts:
        return "unknown"
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def local_day_bounds(days_ago: int = 0) -> tuple[int, int]:
    now = dt.datetime.now().astimezone()
    day = (now - dt.timedelta(days=days_ago)).date()
    start = dt.datetime.combine(day, dt.time.min, tzinfo=now.tzinfo)
    end = start + dt.timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def recent_bounds(days: int) -> tuple[int, int]:
    now = dt.datetime.now().astimezone()
    start = now - dt.timedelta(days=days)
    return int(start.timestamp()), int(now.timestamp())


def stable_hash(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:length]


def sanitize_text(text: Any) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = CONTROL_RE.sub(" ", text)
    text = BASE64_RUN_RE.sub("[base64 omitted]", text)
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if looks_like_binary_or_media(stripped):
            continue
        lines.append(stripped)
    text = "\n".join(lines)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def looks_like_binary_or_media(text: str) -> bool:
    if len(text) > 1000 and len(TOKEN_RE.findall(text)) < 5:
        return True
    if text.startswith("iVBOR") or text.startswith("/9j/"):
        return True
    if text.count("/") > 80 and len(text) > 500:
        return True
    return False


def useful_text(text: str) -> bool:
    text = sanitize_text(text)
    if len(text) < 3:
        return False
    if text in {"[]", "{}", "true", "false", "null"}:
        return False
    if len(text) > 200 and len(TOKEN_RE.findall(text)) < 3:
        return False
    return True


def unique_join(parts: Iterable[str], sep: str = "\n\n") -> str:
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        part = sanitize_text(part)
        if not part:
            continue
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(part)
    return sep.join(out)


def connect_db(db_path: pathlib.Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextlib.contextmanager
def session_lock(shared: bool = False) -> Iterator[None]:
    with file_lock(expand(DEFAULT_LOCK), shared=shared):
        yield


def connect_ro_sqlite(path: pathlib.Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    preflight_schema(conn)
    if schema_version(conn) == CURRENT_SCHEMA_VERSION:
        validate_current_schema(conn)
        return
    database_path = str(conn.execute("PRAGMA database_list").fetchone()[2] or "")
    existing_tables = int(
        conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
    )
    if database_path and existing_tables and schema_version(conn) < CURRENT_SCHEMA_VERSION:
        backup_database(conn, pathlib.Path(database_path).parent / "backups")
    schema_text = """
        CREATE TABLE IF NOT EXISTS documents (
            doc_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            session_id TEXT NOT NULL,
            title TEXT NOT NULL,
            path TEXT NOT NULL,
            cwd TEXT NOT NULL,
            role TEXT NOT NULL,
            ts INTEGER,
            text TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            meta_json TEXT NOT NULL,
            indexed_at INTEGER NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
            doc_id UNINDEXED,
            title,
            text,
            source,
            cwd,
            tokenize = 'unicode61 remove_diacritics 2'
        );

        CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source);
        CREATE INDEX IF NOT EXISTS idx_documents_session ON documents(session_id);
        CREATE INDEX IF NOT EXISTS idx_documents_ts ON documents(ts);

        CREATE TABLE IF NOT EXISTS embeddings (
            doc_id TEXT PRIMARY KEY,
            model TEXT NOT NULL,
            dim INTEGER NOT NULL,
            vector BLOB NOT NULL,
            text_hash TEXT NOT NULL,
            embedded_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);

        CREATE TABLE IF NOT EXISTS session_embeddings (
            source TEXT NOT NULL,
            session_id TEXT NOT NULL,
            model TEXT NOT NULL,
            dim INTEGER NOT NULL,
            vector BLOB NOT NULL,
            text_hash TEXT NOT NULL,
            embedded_at INTEGER NOT NULL,
            PRIMARY KEY (source, session_id, model)
        );

        CREATE INDEX IF NOT EXISTS idx_session_embeddings_model
            ON session_embeddings(model);

        CREATE TABLE IF NOT EXISTS session_cards (
            source TEXT NOT NULL,
            session_id TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            title TEXT NOT NULL,
            repo TEXT NOT NULL,
            last_active INTEGER,
            what_this_was TEXT NOT NULL,
            what_happened TEXT NOT NULL,
            next_clue TEXT NOT NULL,
            mentioned_paths_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            built_at INTEGER NOT NULL,
            PRIMARY KEY (source, session_id)
        );

        CREATE TABLE IF NOT EXISTS session_archive_status (
            source TEXT NOT NULL,
            session_id TEXT NOT NULL,
            archived INTEGER NOT NULL,
            origin TEXT NOT NULL,
            status_at INTEGER NOT NULL,
            evidence_ts INTEGER,
            evidence TEXT NOT NULL,
            PRIMARY KEY (source, session_id)
        );

        CREATE INDEX IF NOT EXISTS idx_session_archive_status_archived
            ON session_archive_status(archived);

        CREATE TABLE IF NOT EXISTS session_archive_events (
            event_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            session_id TEXT NOT NULL,
            previous_archived INTEGER NOT NULL,
            archived INTEGER NOT NULL,
            intent TEXT NOT NULL,
            origin TEXT NOT NULL,
            event_ts INTEGER NOT NULL,
            evidence_doc_id TEXT NOT NULL,
            evidence TEXT NOT NULL,
            parser_version INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_session_archive_events_session
            ON session_archive_events(source, session_id, event_ts);

        CREATE TABLE IF NOT EXISTS session_archive_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    with immediate_transaction(conn):
        execute_statements(conn, schema_text)
        archive_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(session_archive_status)")
        }
        if "evidence_doc_id" not in archive_columns:
            conn.execute("ALTER TABLE session_archive_status ADD COLUMN evidence_doc_id TEXT NOT NULL DEFAULT ''")
        if "parser_version" not in archive_columns:
            conn.execute("ALTER TABLE session_archive_status ADD COLUMN parser_version INTEGER NOT NULL DEFAULT 1")
        finish_migration(conn, schema_text, now_ts())


def reset_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS documents;
        DROP TABLE IF EXISTS documents_fts;
        """
    )
    init_db(conn)


def chunk_document(doc: Document) -> Iterator[Document]:
    text = sanitize_text(doc.text)
    if len(text) <= MAX_DOC_CHARS:
        yield dataclasses.replace(doc, text=text)
        return

    start = 0
    chunk_index = 1
    while start < len(text):
        end = min(len(text), start + MAX_DOC_CHARS)
        if end < len(text):
            boundary = text.rfind("\n\n", start, end)
            if boundary > start + 4000:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            yield dataclasses.replace(
                doc,
                doc_id=f"{doc.doc_id}:chunk{chunk_index}",
                text=chunk,
                meta={**doc.meta, "chunk": chunk_index},
            )
            chunk_index += 1
        if end >= len(text):
            break
        start = max(0, end - CHUNK_OVERLAP)


def upsert_documents(conn: sqlite3.Connection, docs: Iterable[Document]) -> int:
    count = 0
    previous_indexed = int(
        conn.execute("SELECT MAX(COALESCE(indexed_at, 0)) FROM documents").fetchone()[0] or 0
    )
    indexed_at = max(now_ts(), previous_indexed + 1)
    with conn:
        for source_doc in docs:
            for doc in chunk_document(source_doc):
                if not useful_text(doc.text):
                    continue
                text_hash = stable_hash(doc.text, 32)
                meta_json = json.dumps(doc.meta, sort_keys=True, ensure_ascii=False)
                conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (doc.doc_id,))
                conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc.doc_id,))
                conn.execute(
                    """
                    INSERT INTO documents (
                        doc_id, source, session_id, title, path, cwd, role, ts,
                        text, text_hash, meta_json, indexed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc.doc_id,
                        doc.source,
                        doc.session_id,
                        doc.title,
                        doc.path,
                        doc.cwd,
                        doc.role,
                        doc.ts,
                        doc.text,
                        text_hash,
                        meta_json,
                        indexed_at,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO documents_fts (doc_id, title, text, source, cwd)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (doc.doc_id, doc.title, doc.text, doc.source, doc.cwd),
                )
                count += 1
    return count


def iter_codex(home: pathlib.Path) -> Iterator[Document]:
    thread_context = codex_thread_context(home)
    yield from iter_codex_threads(home)
    yield from iter_codex_history(home, thread_context)


def codex_thread_context(home: pathlib.Path) -> dict[str, dict[str, Any]]:
    db = home / ".codex" / "state_5.sqlite"
    conn = connect_ro_sqlite(db)
    if conn is None:
        return {}
    try:
        columns = table_columns(conn, "threads")
        if not {"id", "cwd"}.issubset(columns):
            return {}
        optional = ["title", "updated_at", "created_at", "git_branch", "git_origin_url"]
        selected = ["id", "cwd"] + [c for c in optional if c in columns]
        rows = conn.execute(f"SELECT {', '.join(selected)} FROM threads")
        return {str(row["id"]): {key: row[key] for key in row.keys()} for row in rows}
    finally:
        conn.close()


def iter_codex_threads(home: pathlib.Path) -> Iterator[Document]:
    db = home / ".codex" / "state_5.sqlite"
    conn = connect_ro_sqlite(db)
    if conn is None:
        return
    try:
        columns = table_columns(conn, "threads")
        required = {"id", "updated_at", "cwd", "title"}
        if not required.issubset(columns):
            return
        optional = [
            "created_at",
            "source",
            "model_provider",
            "model",
            "git_branch",
            "first_user_message",
            "preview",
            "agent_nickname",
            "agent_role",
        ]
        selected = ["id", "updated_at", "cwd", "title"] + [c for c in optional if c in columns]
        query = f"SELECT {', '.join(selected)} FROM threads"
        for row in conn.execute(query):
            session_id = row["id"]
            title = sanitize_text(row["title"] or "")
            text = unique_join(
                [
                    title,
                    row["first_user_message"] if "first_user_message" in row.keys() else "",
                    row["preview"] if "preview" in row.keys() else "",
                ]
            )
            if not text:
                continue
            meta = {k: row[k] for k in row.keys() if k not in {"first_user_message", "preview"}}
            yield Document(
                doc_id=f"codex-thread:{session_id}",
                source="codex",
                session_id=session_id,
                title=title or text[:120],
                path=str(db),
                cwd=sanitize_text(row["cwd"] or ""),
                role="session",
                ts=parse_ts(row["updated_at"]),
                text=text,
                meta=meta,
            )
    finally:
        conn.close()


def iter_codex_history(
    home: pathlib.Path,
    thread_context: dict[str, dict[str, Any]] | None = None,
) -> Iterator[Document]:
    path = home / ".codex" / "history.jsonl"
    if not path.exists():
        return
    thread_context = thread_context or {}
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = sanitize_text(item.get("text", ""))
            if not useful_text(text):
                continue
            session_id = str(item.get("session_id") or "unknown")
            context = thread_context.get(session_id, {})
            ts = parse_ts(item.get("ts"))
            meta = {"line": line_no}
            if context:
                meta["thread"] = {
                    key: context[key]
                    for key in ("title", "updated_at", "created_at", "git_branch", "git_origin_url")
                    if key in context
                }
            yield Document(
                doc_id=f"codex-history:{session_id}:{ts or line_no}:{stable_hash(text)}",
                source="codex",
                session_id=session_id,
                title=text[:120],
                path=str(path),
                cwd=sanitize_text(context.get("cwd", "")),
                role="user",
                ts=ts,
                text=text,
                meta=meta,
            )


def iter_claude(home: pathlib.Path) -> Iterator[Document]:
    projects_dir = home / ".claude" / "projects"
    if projects_dir.exists():
        pattern = str(projects_dir / "**" / "*.jsonl")
        for file_path in glob.iglob(pattern, recursive=True):
            yield from iter_claude_jsonl(pathlib.Path(file_path), source="claude")

    sessions_dir = home / ".claude" / "sessions"
    if sessions_dir.exists():
        for file_path in sessions_dir.glob("*.json"):
            yield from iter_json_session_file(file_path, source="claude")


def iter_claude_jsonl(path: pathlib.Path, source: str) -> Iterator[Document]:
    docs: list[Document] = []
    title = ""
    session_id = path.stem
    cwd = ""
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if obj.get("sessionId"):
                session_id = str(obj["sessionId"])
            if obj.get("cwd"):
                cwd = sanitize_text(obj["cwd"])
            if obj.get("type") == "ai-title" and obj.get("aiTitle"):
                title = sanitize_text(obj["aiTitle"])
                continue

            role, text = extract_claude_turn(obj)
            if not text:
                continue
            ts = parse_ts(obj.get("timestamp"))
            uuid = obj.get("uuid") or obj.get("requestId") or line_no
            docs.append(
                Document(
                    doc_id=f"{source}:{session_id}:{uuid}",
                    source=source,
                    session_id=session_id,
                    title=title,
                    path=str(path),
                    cwd=cwd,
                    role=role,
                    ts=ts,
                    text=text,
                    meta={
                        "line": line_no,
                        "type": obj.get("type", ""),
                        "is_sidechain": bool(obj.get("isSidechain", False)),
                    },
                )
            )

    fallback_title = title or session_id
    for doc in docs:
        yield dataclasses.replace(doc, title=doc.title or fallback_title)


def extract_claude_turn(obj: dict[str, Any]) -> tuple[str, str]:
    obj_type = obj.get("type", "")
    if obj_type in {
        "attachment",
        "file-history-snapshot",
        "last-prompt",
        "mode",
        "permission-mode",
        "system",
    }:
        return "", ""

    message = obj.get("message")
    role = obj_type
    if isinstance(message, dict):
        role = str(message.get("role") or obj_type)
        text = extract_content_text(message.get("content"))
        return role, text
    if isinstance(message, str):
        return role, sanitize_text(message)
    return "", ""


def extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return sanitize_text(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                item_type = str(item.get("type", "")).lower()
                if item_type in {"image", "tool_use", "tool_result"}:
                    continue
                if "text" in item:
                    parts.append(str(item["text"]))
        return sanitize_text("\n\n".join(parts))
    return ""


def iter_vscode(home: pathlib.Path) -> Iterator[Document]:
    code_root = home / "Library" / "Application Support" / "Code" / "User"
    yield from iter_vscode_user_root(code_root, source="vscode")


def iter_cursor(home: pathlib.Path) -> Iterator[Document]:
    cursor_root = home / "Library" / "Application Support" / "Cursor" / "User"
    yield from iter_vscode_user_root(cursor_root, source="cursor")


def iter_vscode_user_root(root: pathlib.Path, source: str) -> Iterator[Document]:
    empty_sessions = root / "globalStorage" / "emptyWindowChatSessions"
    if empty_sessions.exists():
        for file_path in empty_sessions.glob("*.jsonl"):
            yield from iter_vscode_chat_jsonl(file_path, source=source)

    dbs: list[pathlib.Path] = []
    global_db = root / "globalStorage" / "state.vscdb"
    if global_db.exists():
        dbs.append(global_db)
    workspace_storage = root / "workspaceStorage"
    if workspace_storage.exists():
        dbs.extend(workspace_storage.glob("*/state.vscdb"))

    for db in dbs:
        yield from iter_vscode_state_db(db, source=source)


def iter_vscode_chat_jsonl(path: pathlib.Path, source: str) -> Iterator[Document]:
    session_id = path.stem
    title = ""
    seen: set[str] = set()
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            v = obj.get("v")
            if isinstance(v, dict) and v.get("sessionId"):
                session_id = str(v["sessionId"])
            if is_patch_key(obj, "customTitle") and isinstance(v, str):
                title = sanitize_text(v)

            for doc in extract_vscode_docs(obj, source, path, session_id, title, line_no):
                if doc.doc_id in seen:
                    continue
                seen.add(doc.doc_id)
                yield doc


def iter_vscode_state_db(path: pathlib.Path, source: str) -> Iterator[Document]:
    conn = connect_ro_sqlite(path)
    if conn is None:
        return
    try:
        try:
            if "ItemTable" not in table_names(conn):
                return
            rows = conn.execute("SELECT key, value FROM ItemTable")
        except sqlite3.Error:
            return
        for row in rows:
            try:
                key = str(row["key"])
                if key.startswith("secret://"):
                    continue
                if not any(hint.lower() in key.lower() for hint in CHAT_KEY_HINTS):
                    continue
                value = decode_sqlite_value(row["value"])
                if not value:
                    continue
                parsed = parse_json_maybe(value)
                if parsed is None:
                    continue
                session_id = stable_hash(str(path) + key)
                title = key
                for i, text in enumerate(extract_generic_chat_text(parsed)):
                    text = sanitize_text(text)
                    if not useful_text(text):
                        continue
                    yield Document(
                        doc_id=f"{source}-state:{session_id}:{i}:{stable_hash(text)}",
                        source=source,
                        session_id=session_id,
                        title=title,
                        path=str(path),
                        cwd="",
                        role="state",
                        ts=None,
                        text=text,
                        meta={"state_key": key},
                    )
            except sqlite3.Error:
                continue
    finally:
        conn.close()


def iter_json_session_file(path: pathlib.Path, source: str) -> Iterator[Document]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return
    session_id = path.stem
    for i, text in enumerate(extract_generic_chat_text(parsed)):
        text = sanitize_text(text)
        if not useful_text(text):
            continue
        yield Document(
            doc_id=f"{source}-json:{session_id}:{i}:{stable_hash(text)}",
            source=source,
            session_id=session_id,
            title=session_id,
            path=str(path),
            cwd="",
            role="session",
            ts=None,
            text=text,
            meta={},
        )


def extract_vscode_docs(
    obj: dict[str, Any],
    source: str,
    path: pathlib.Path,
    session_id: str,
    title: str,
    line_no: int,
) -> Iterator[Document]:
    v = obj.get("v")
    if isinstance(v, dict):
        for request in extract_request_objects(v):
            yield from vscode_request_docs(request, source, path, session_id, title, line_no)
    elif isinstance(v, list):
        for request in v:
            if isinstance(request, dict):
                yield from vscode_request_docs(request, source, path, session_id, title, line_no)


def vscode_request_docs(
    request: dict[str, Any],
    source: str,
    path: pathlib.Path,
    session_id: str,
    title: str,
    line_no: int,
) -> Iterator[Document]:
    req_id = str(
        request.get("requestId")
        or request.get("responseId")
        or request.get("id")
        or stable_hash(json.dumps(request, sort_keys=True, default=str))
    )
    ts = parse_ts(request.get("timestamp") or request.get("timeSpentWaiting"))
    if request.get("sessionId"):
        session_id = str(request["sessionId"])

    user_text = extract_vscode_user_message(request)
    assistant_text = extract_vscode_assistant_response(request)
    text = unique_join(
        [
            f"USER:\n{user_text}" if user_text else "",
            f"ASSISTANT:\n{assistant_text}" if assistant_text else "",
        ]
    )
    if not useful_text(text):
        return
    yield Document(
        doc_id=f"{source}-chat:{session_id}:{req_id}",
        source=source,
        session_id=session_id,
        title=title or sanitize_text(user_text[:120]),
        path=str(path),
        cwd="",
        role="turn",
        ts=ts,
        text=text,
        meta={"line": line_no, "request_id": req_id},
    )


def extract_request_objects(obj: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        requests = obj.get("requests")
        if isinstance(requests, list):
            out.extend([r for r in requests if isinstance(r, dict)])
        if looks_like_vscode_request(obj):
            out.append(obj)
    return out


def looks_like_vscode_request(obj: dict[str, Any]) -> bool:
    return any(k in obj for k in ("requestId", "responseId")) and any(
        k in obj for k in ("message", "result", "response")
    )


def extract_vscode_user_message(request: dict[str, Any]) -> str:
    message = request.get("message")
    if isinstance(message, dict):
        parts = []
        if isinstance(message.get("text"), str):
            parts.append(message["text"])
        for part in message.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return unique_join(parts, sep="\n")
    if isinstance(message, str):
        return sanitize_text(message)
    return ""


def extract_vscode_assistant_response(request: dict[str, Any]) -> str:
    parts: list[str] = []
    result = request.get("result")
    if isinstance(result, dict):
        for round_item in result.get("toolCallRounds") or []:
            if isinstance(round_item, dict) and isinstance(round_item.get("response"), str):
                parts.append(round_item["response"])
    response = request.get("response")
    if isinstance(response, list):
        for item in response:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).lower()
            if "thinking" in kind:
                continue
            if isinstance(item.get("value"), str):
                parts.append(item["value"])
    return unique_join(parts)


def extract_generic_chat_text(obj: Any) -> list[str]:
    out: list[str] = []

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            lowered = {str(k).lower() for k in value.keys()}
            if lowered & SKIP_KEYS:
                return
            if looks_like_vscode_request(value):
                user_text = extract_vscode_user_message(value)
                assistant_text = extract_vscode_assistant_response(value)
                text = unique_join([user_text, assistant_text])
                if text:
                    out.append(text)
            for k, v in value.items():
                key = str(k)
                if should_skip_key(key):
                    continue
                if key in TEXT_KEYS and isinstance(v, str) and is_chat_path(path):
                    out.append(v)
                else:
                    walk(v, path + (key,))
        elif isinstance(value, list):
            for item in value:
                walk(item, path)

    walk(obj, ())
    cleaned: list[str] = []
    seen: set[str] = set()
    for text in out:
        text = sanitize_text(text)
        if not useful_text(text):
            continue
        key = stable_hash(text)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned


def should_skip_key(key: str) -> bool:
    lower = key.lower()
    return any(skip in lower for skip in SKIP_KEYS)


def is_chat_path(path: tuple[str, ...]) -> bool:
    if not path:
        return False
    joined = ".".join(path).lower()
    return any(hint.lower() in joined for hint in CHAT_KEY_HINTS + ("request", "message", "response"))


def is_patch_key(obj: dict[str, Any], key: str) -> bool:
    k = obj.get("k")
    return isinstance(k, list) and k and k[-1] == key


def decode_sqlite_value(value: Any) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "ignore")
        except UnicodeDecodeError:
            return ""
    if isinstance(value, str):
        return value
    return ""


def parse_json_maybe(value: str) -> Any | None:
    value = value.strip()
    if not value or value[0] not in "[{\"":
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def build_docs(home: pathlib.Path, sources: set[str]) -> Iterator[Document]:
    if "codex" in sources:
        yield from iter_codex(home)
    if "claude" in sources:
        yield from iter_claude(home)
    if "vscode" in sources:
        yield from iter_vscode(home)
    if "cursor" in sources:
        yield from iter_cursor(home)


def iter_dashboard_updates(conn: sqlite3.Connection, home: pathlib.Path) -> Iterator[Document]:
    """Yield only changed Claude files and recent Codex records for a fast dashboard sync."""
    codex_cutoff = int(
        conn.execute("SELECT COALESCE(MAX(ts), 0) FROM documents WHERE source = 'codex'").fetchone()[0]
    )
    thread_context = codex_thread_context(home)
    for document in iter_codex_threads(home):
        if document.ts is None or document.ts >= max(0, codex_cutoff - 3600):
            yield document
    for document in iter_codex_history(home, thread_context):
        if document.ts is None or document.ts >= max(0, codex_cutoff - 3600):
            yield document

    indexed_paths = {
        str(row["path"]): (int(row["last_indexed"] or 0), int(row["last_ts"] or 0))
        for row in conn.execute(
            """
            SELECT path, MAX(indexed_at) AS last_indexed, MAX(COALESCE(ts, 0)) AS last_ts
            FROM documents
            WHERE source = 'claude'
            GROUP BY path
            """
        )
    }
    projects_dir = home / ".claude" / "projects"
    if projects_dir.exists():
        for file_path in projects_dir.glob("**/*.jsonl"):
            try:
                changed_at = int(file_path.stat().st_mtime)
            except OSError:
                continue
            last_indexed, last_ts = indexed_paths.get(str(file_path), (0, 0))
            if changed_at <= last_indexed:
                continue
            for document in iter_claude_jsonl(file_path, source="claude"):
                if last_indexed == 0 or document.ts is None or document.ts > last_ts:
                    yield document


def refresh_dashboard_index(conn: sqlite3.Connection, home: pathlib.Path) -> int:
    count = upsert_documents(conn, iter_dashboard_updates(conn, home))
    sync_detected_archive_states(conn)
    return count


def normalize_sources(raw: str) -> set[str]:
    if raw == "all":
        return {"codex", "claude", "vscode", "cursor"}
    return {part.strip() for part in raw.split(",") if part.strip()}


def fts_query(query: str) -> str:
    tokens = expand_query_tokens(tokenize(query))
    if not tokens:
        return quote_fts(query)
    return " OR ".join(f"{quote_fts(t)}*" for t in tokens[:12])


def quote_fts(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def tokenize(text: str) -> list[str]:
    return [normalize_token(m.group(0)) for m in TOKEN_RE.finditer(text.lower()) if len(m.group(0)) >= 2]


def expand_query_tokens(tokens: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        for expanded in (token, *ALIASES.get(token, ())):
            if expanded and expanded not in seen:
                seen.add(expanded)
                out.append(expanded)
    return out


def normalize_token(token: str) -> str:
    token = token.lower().strip("._-")
    for suffix in ("ing", "ed", "es", "s"):
        if suffix == "s" and token.endswith("ss"):
            continue
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def similarity_vector(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    tokens = expand_query_tokens(tokenize(text))
    for token in tokens:
        counts[f"w:{token}"] += 2
        if len(token) >= 5:
            for i in range(len(token) - 2):
                counts[f"g:{token[i:i+3]}"] += 1
    return counts


def cosine(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def semantic_text(row: sqlite3.Row) -> str:
    return unique_join(
        [
            str(row["title"] or ""),
            str(row["cwd"] or ""),
            str(row["text"] or "")[:EMBED_TEXT_CHARS],
        ],
        sep="\n",
    )


@functools.lru_cache(maxsize=1)
def embedding_backend() -> Any | None:
    try:
        from fastembed import TextEmbedding  # type: ignore
    except Exception:
        return None
    try:
        return TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(expand(DEFAULT_MODEL_CACHE)))
    except Exception:
        return None


def normalize_embedding(vector: Any) -> bytes:
    import numpy as np

    arr = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm:
        arr = arr / norm
    return arr.astype(np.float32).tobytes()


def vector_from_blob(blob: bytes) -> Any:
    import numpy as np

    return np.frombuffer(blob, dtype=np.float32)


def dot_blob(blob: bytes, query_vector: Any) -> float:
    import numpy as np

    vector = vector_from_blob(blob)
    if vector.shape != query_vector.shape:
        return 0.0
    return float(np.dot(vector, query_vector))


def embedding_query_vector(model: Any, query: str) -> Any:
    import numpy as np

    vector = next(iter(model.embed([query])))
    arr = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm:
        arr = arr / norm
    return arr


def ensure_embeddings(conn: sqlite3.Connection, limit: int | None = None, quiet: bool = True) -> int:
    model = embedding_backend()
    if model is None:
        if not quiet:
            print("Semantic search unavailable: local fastembed dependency is not installed.")
        return 0

    limit_clause = "LIMIT ?" if limit is not None else ""
    params: list[Any] = [EMBED_MODEL_VERSION]
    if limit is not None:
        params.append(limit)
    rows = list(
        conn.execute(
            f"""
            SELECT d.doc_id, d.title, d.cwd, d.text, d.text_hash
            FROM documents d
            LEFT JOIN embeddings e
              ON e.doc_id = d.doc_id
             AND e.model = ?
             AND e.text_hash = d.text_hash
            WHERE e.doc_id IS NULL
            ORDER BY COALESCE(d.ts, 0) DESC
            {limit_clause}
            """,
            params,
        )
    )
    if not rows:
        return 0

    texts = [semantic_text(row) for row in rows]
    count = 0
    embedded_at = now_ts()
    with conn:
        for row, vector in zip(rows, model.embed(texts), strict=False):
            vector_blob = normalize_embedding(vector)
            dim = len(vector_blob) // 4
            conn.execute(
                """
                INSERT OR REPLACE INTO embeddings (
                    doc_id, model, dim, vector, text_hash, embedded_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row["doc_id"], EMBED_MODEL_VERSION, dim, vector_blob, row["text_hash"], embedded_at),
            )
            count += 1
    if not quiet:
        print(f"Embedded {count} documents with {EMBED_MODEL}")
    return count


def session_groups(conn: sqlite3.Connection, limit: int | None = None) -> list[tuple[str, str]]:
    limit_clause = "LIMIT ?" if limit is not None else ""
    params: list[Any] = []
    if limit is not None:
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT source, session_id, max(COALESCE(ts, 0)) AS newest
        FROM documents
        GROUP BY source, session_id
        ORDER BY newest DESC
        {limit_clause}
        """,
        params,
    )
    return [(str(row["source"]), str(row["session_id"])) for row in rows]


def rows_for_session(conn: sqlite3.Connection, source: str, session_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM documents
            WHERE source = ? AND session_id = ?
            ORDER BY COALESCE(ts, 0) ASC, doc_id ASC
            """,
            (source, session_id),
        )
    )


def session_embedding_text(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return ""
    titles = [str(row_field(row, "title", "")) for row in rows if not is_weak_title(str(row_field(row, "title", "")))]
    cwds = [str(row_field(row, "cwd", "")) for row in rows if row_field(row, "cwd", "")]
    lines: list[str] = []
    for row in rows[:6]:
        line = best_line_for_row(row)
        if line:
            lines.append(line)
    for row in rows[-10:]:
        line = best_line_for_row(row)
        if line:
            lines.append(line)
    paths = list(mentioned_paths(rows, limit=8))
    return unique_join([*titles[:4], *cwds[:2], *lines, *paths], sep="\n")[:EMBED_TEXT_CHARS]


def semantic_score(session_score: float, document_score: float) -> float:
    """Blend broad session meaning with the strongest individual turn.

    The max keeps a focused mid-session turn from being diluted by a long
    transcript. The small agreement bonus rewards sessions whose overall
    subject also matches the query.
    """
    strongest = max(session_score, document_score)
    agreement = min(session_score, document_score)
    return strongest + (0.15 * agreement)


def representative_session_row(conn: sqlite3.Connection, source: str, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM documents
        WHERE source = ? AND session_id = ?
        ORDER BY COALESCE(ts, 0) DESC, doc_id DESC
        LIMIT 1
        """,
        (source, session_id),
    ).fetchone()


def ensure_session_embeddings(conn: sqlite3.Connection, limit: int | None = None, quiet: bool = True) -> int:
    model = embedding_backend()
    if model is None:
        if not quiet:
            print("Semantic search unavailable: local model is not installed or cached.")
        return 0

    pending: list[tuple[str, str, str, str]] = []
    for source, session_id in session_groups(conn, limit=None):
        rows = rows_for_session(conn, source, session_id)
        text = session_embedding_text(rows)
        if not text:
            continue
        text_hash = stable_hash(text, 32)
        existing = conn.execute(
            """
            SELECT 1
            FROM session_embeddings
            WHERE source = ? AND session_id = ? AND model = ? AND text_hash = ?
            """,
            (source, session_id, EMBED_MODEL_VERSION, text_hash),
        ).fetchone()
        if existing:
            continue
        pending.append((source, session_id, text, text_hash))
        if limit is not None and len(pending) >= limit:
            break

    if not pending:
        return 0

    embedded_at = now_ts()
    texts = [item[2] for item in pending]
    count = 0
    with conn:
        for (source, session_id, _text, text_hash), vector in zip(pending, model.embed(texts), strict=False):
            vector_blob = normalize_embedding(vector)
            dim = len(vector_blob) // 4
            conn.execute(
                """
                INSERT OR REPLACE INTO session_embeddings (
                    source, session_id, model, dim, vector, text_hash, embedded_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (source, session_id, EMBED_MODEL_VERSION, dim, vector_blob, text_hash, embedded_at),
            )
            count += 1
    if not quiet:
        print(f"Embedded {count} sessions with {EMBED_MODEL}")
    return count


def search_fts(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    source: str | None,
    since: int | None = None,
    until: int | None = None,
) -> list[sqlite3.Row]:
    match = fts_query(query)
    params: list[Any] = [match]
    source_clause = ""
    if source:
        source_clause = "AND d.source = ?"
        params.append(source)
    time_clause = ""
    if since is not None:
        time_clause += " AND d.ts >= ?"
        params.append(since)
    if until is not None:
        time_clause += " AND d.ts < ?"
        params.append(until)
    params.append(limit)
    try:
        return list(
            conn.execute(
                f"""
                SELECT d.*, bm25(documents_fts) AS score
                FROM documents_fts
                JOIN documents d ON d.doc_id = documents_fts.doc_id
                WHERE documents_fts MATCH ? {source_clause} {time_clause}
                ORDER BY score ASC, d.ts DESC
                LIMIT ?
                """,
                params,
            )
        )
    except sqlite3.OperationalError:
        return []


def search_local(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    source: str | None,
    since: int | None = None,
    until: int | None = None,
) -> list[tuple[sqlite3.Row, float]]:
    qv = similarity_vector(query)
    if not qv:
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if source:
        clauses.append("source = ?")
        params.append(source)
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("ts < ?")
        params.append(until)
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT *
        FROM documents
        {where_clause}
        ORDER BY COALESCE(ts, 0) DESC
        """,
        params,
    )
    scored: list[tuple[sqlite3.Row, float]] = []
    for row in rows:
        text = f"{row['title']}\n{row['cwd']}\n{row['text'][:40000]}"
        score = cosine(qv, similarity_vector(text))
        if score > 0:
            scored.append((row, score))
    scored.sort(key=lambda item: (item[1], item[0]["ts"] or 0), reverse=True)
    return scored[:limit]


def search_semantic(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    source: str | None,
    since: int | None = None,
    until: int | None = None,
) -> list[tuple[sqlite3.Row, float]]:
    model = embedding_backend()
    if model is None:
        return []
    ensure_session_embeddings(conn, quiet=True)
    query_vector = embedding_query_vector(model, query)
    clauses = ["e.model = ?"]
    params: list[Any] = [EMBED_MODEL_VERSION]
    if source:
        clauses.append("e.source = ?")
        params.append(source)
    session_rows = conn.execute(
        f"""
        SELECT e.source, e.session_id, e.vector AS embedding_vector
        FROM session_embeddings e
        WHERE {' AND '.join(clauses)}
        """,
        params,
    )
    by_session: dict[tuple[str, str], dict[str, Any]] = {}
    for row in session_rows:
        key = (str(row["source"]), str(row["session_id"]))
        by_session[key] = {
            "session_score": max(0.0, dot_blob(row["embedding_vector"], query_vector)),
            "document_score": 0.0,
            "row": None,
        }

    doc_clauses = ["e.model = ?"]
    doc_params: list[Any] = [EMBED_MODEL_VERSION]
    if source:
        doc_clauses.append("d.source = ?")
        doc_params.append(source)
    if since is not None:
        doc_clauses.append("d.ts >= ?")
        doc_params.append(since)
    if until is not None:
        doc_clauses.append("d.ts < ?")
        doc_params.append(until)
    document_rows = conn.execute(
        f"""
        SELECT d.*, e.vector AS embedding_vector
        FROM embeddings e
        JOIN documents d ON d.doc_id = e.doc_id
        WHERE {' AND '.join(doc_clauses)}
        """,
        doc_params,
    )
    for row in document_rows:
        key = (str(row["source"]), str(row["session_id"]))
        entry = by_session.setdefault(
            key,
            {"session_score": 0.0, "document_score": 0.0, "row": None},
        )
        score = max(0.0, dot_blob(row["embedding_vector"], query_vector))
        if score > entry["document_score"]:
            entry["document_score"] = score
            entry["row"] = row

    scored: list[tuple[sqlite3.Row, float]] = []
    for (row_source, session_id), entry in by_session.items():
        doc_row = entry["row"] or representative_session_row(conn, row_source, session_id)
        if doc_row is None:
            continue
        activity_row = representative_session_row(conn, row_source, session_id)
        activity_ts = activity_row["ts"] if activity_row is not None else doc_row["ts"]
        if since is not None and (activity_ts is None or activity_ts < since):
            continue
        if until is not None and (activity_ts is None or activity_ts >= until):
            continue
        score = semantic_score(entry["session_score"], entry["document_score"])
        if score > 0:
            scored.append((doc_row, score))
    scored.sort(key=lambda item: (item[1], item[0]["ts"] or 0), reverse=True)
    return scored[:limit]


def search_recent(
    conn: sqlite3.Connection,
    limit: int,
    source: str | None,
    since: int | None,
    until: int | None,
) -> list[tuple[sqlite3.Row, float, str]]:
    clauses = ["ts IS NOT NULL"]
    params: list[Any] = []
    if source:
        clauses.append("source = ?")
        params.append(source)
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("ts < ?")
        params.append(until)
    rows = conn.execute(
        f"""
        SELECT *
        FROM documents
        WHERE {' AND '.join(clauses)}
        ORDER BY ts DESC
        LIMIT ?
        """,
        [*params, limit],
    )
    return [(row, 1.0, "recent") for row in rows]


def infer_source_from_query(query: str, explicit_source: str) -> tuple[str, str]:
    if explicit_source != "all":
        return explicit_source, query.strip()

    leading_sources = [
        ("claude", r"^\s*(?:claude(?:\s+code)?|cc)\b"),
        ("codex", r"^\s*codex\b"),
        ("vscode", r"^\s*(?:github\s+copilot|copilot|vscode|vs\s+code)\b"),
        ("cursor", r"^\s*cursor\b"),
    ]
    for source, pattern in leading_sources:
        if re.search(pattern, query, flags=re.I):
            cleaned = re.sub(pattern, " ", query, count=1, flags=re.I)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            return source, cleaned or query.strip()

    patterns = [
        ("claude", r"\b(?:in|from|only)\s+claude(?:\s+code)?\b|\bclaude\s+code\s+session\b"),
        ("codex", r"\b(?:in|from|only)\s+codex\b|\bcodex\s+session\b"),
        (
            "vscode",
            r"\b(?:in|from|only)\s+(?:vscode|vs\s+code|copilot|github\s+copilot)\b|"
            r"\b(?:vscode|vs\s+code|copilot|github\s+copilot)\s+session\b",
        ),
        ("cursor", r"\b(?:in|from|only)\s+cursor\b|\bcursor\s+session\b"),
    ]

    cleaned = query
    for source, pattern in patterns:
        if re.search(pattern, cleaned, flags=re.I):
            cleaned = re.sub(pattern, " ", cleaned, flags=re.I)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            return source, cleaned or query.strip()
    return "all", query.strip()


def infer_time_and_intent(query: str) -> tuple[str, int | None, int | None, bool]:
    cleaned = query.strip()
    since: int | None = None
    until: int | None = None

    if re.search(r"\btoday\b", cleaned, flags=re.I):
        since, until = local_day_bounds(0)
        cleaned = re.sub(r"\btoday\b", " ", cleaned, flags=re.I)
    elif re.search(r"\byesterday\b", cleaned, flags=re.I):
        since, until = local_day_bounds(1)
        cleaned = re.sub(r"\byesterday\b", " ", cleaned, flags=re.I)
    elif re.search(r"\b(?:recent|recently|last\s+few\s+days)\b", cleaned, flags=re.I):
        since, until = recent_bounds(7)
        cleaned = re.sub(r"\b(?:recent|recently|last\s+few\s+days)\b", " ", cleaned, flags=re.I)
    elif re.search(r"\b(?:this\s+week|last\s+7\s+days)\b", cleaned, flags=re.I):
        since, until = recent_bounds(7)
        cleaned = re.sub(r"\b(?:this\s+week|last\s+7\s+days)\b", " ", cleaned, flags=re.I)

    activity_pattern = (
        r"\b(?:what\s+did\s+i\s+work\s+on|what\s+was\s+i\s+working\s+on|"
        r"what\s+have\s+i\s+worked\s+on|where\s+did\s+i\s+leave\s+off|"
        r"what\s+was\s+i\s+doing)\b"
    )
    activity = bool(re.search(activity_pattern, cleaned, flags=re.I))
    cleaned = re.sub(activity_pattern, " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, since, until, activity


def merge_results(
    fts_rows: list[sqlite3.Row],
    local_rows: list[tuple[sqlite3.Row, float]],
    semantic_rows: list[tuple[sqlite3.Row, float]],
    limit: int,
) -> list[tuple[sqlite3.Row, float, str]]:
    by_id: dict[str, tuple[sqlite3.Row, float, str]] = {}
    for rank, row in enumerate(fts_rows, 1):
        score = max(0.0, 1.0 - (rank - 1) * 0.05)
        by_id[row["doc_id"]] = (row, score, "fts")
    for row, local_score in local_rows:
        existing = by_id.get(row["doc_id"])
        if existing:
            by_id[row["doc_id"]] = (row, existing[1] + local_score, "hybrid")
        else:
            by_id[row["doc_id"]] = (row, local_score, "local")
    for row, semantic_score in semantic_rows:
        existing = by_id.get(row["doc_id"])
        weighted_score = semantic_score * 1.25
        if existing:
            merged_mode = existing[2] if "semantic" in existing[2] else f"{existing[2]}+semantic"
            by_id[row["doc_id"]] = (row, existing[1] + weighted_score, merged_mode)
        else:
            by_id[row["doc_id"]] = (row, weighted_score, "semantic")
    merged = list(by_id.values())
    merged.sort(key=lambda item: (item[1], item[0]["ts"] or 0), reverse=True)
    return merged[:limit]


def group_results_by_session(
    results: list[tuple[sqlite3.Row, float, str]],
    limit: int,
) -> list[tuple[sqlite3.Row, float, str]]:
    grouped: dict[tuple[str, str], tuple[sqlite3.Row, float, str, int]] = {}
    for row, score, mode in results:
        key = (row["source"], row["session_id"])
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = (row, score, mode, 1)
            continue
        best_row, best_score, best_mode, count = existing
        if score > best_score or (
            score >= best_score - 0.2 and row_context_quality(row) > row_context_quality(best_row)
        ):
            if score > best_score:
                grouped[key] = (row, score, mode, count + 1)
            else:
                grouped[key] = (row, best_score, best_mode, count + 1)
        else:
            grouped[key] = (best_row, best_score, best_mode, count + 1)

    out: list[tuple[sqlite3.Row, float, str]] = []
    for row, score, mode, count in grouped.values():
        label = f"{mode}, {count} hits" if count > 1 else mode
        out.append((row, score, label))
    out.sort(key=lambda item: (item[1], item[0]["ts"] or 0), reverse=True)
    return out[:limit]


def is_meta_query(query: str) -> bool:
    return bool(META_QUERY_RE.search(query))


def is_meta_session(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    rows = session_rows(conn, row)
    sample = "\n".join(
        compact(f"{item['title']}\n{item['text']}", 1000)
        for item in rows[:20]
    )
    return bool(META_SESSION_RE.search(sample))


def session_text_sample(conn: sqlite3.Connection, row: sqlite3.Row, max_rows: int = 24) -> str:
    rows = session_rows(conn, row)
    return "\n".join(
        compact(f"{item['title']}\n{item['cwd']}\n{item['text']}", 1200)
        for item in rows[:max_rows]
    ).lower()


def full_session_text(rows: list[sqlite3.Row]) -> str:
    return "\n".join(
        compact(
            f"{row_field(item, 'title')}\n{row_field(item, 'cwd')}\n{row_field(item, 'text')}",
            1200,
        )
        for item in rows
    ).lower()


def query_anchor_terms(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for match in TOKEN_RE.finditer(query):
        term = normalize_token(match.group(0))
        if not term or term in seen or term in STOP_WORDS:
            continue
        if len(term) < 3 and term not in IMPORTANT_SHORT_TERMS:
            continue
        seen.add(term)
        terms.append(term)
    return terms


def term_variants(term: str) -> tuple[str, ...]:
    variants: list[str] = []
    seen: set[str] = set()
    for value in (term, *ALIASES.get(term, ())):
        normalized = normalize_token(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            variants.append(normalized)
    return tuple(variants)


def term_present(term: str, text: str) -> bool:
    return any(variant in text for variant in term_variants(term))


def matched_anchor_terms(terms: list[str], text: str) -> list[str]:
    return [term for term in terms if term_present(term, text)]


def query_term_weight(term: str, proper_terms: set[str]) -> float:
    weight = 1.0
    if term in proper_terms:
        weight += 0.8
    if term in IMPORTANT_SHORT_TERMS:
        weight += 0.5
    if term in ACTION_ANCHOR_TERMS:
        weight += 0.4
    if len(term) >= 7:
        weight += 0.2
    return weight


def anchor_weight(terms: list[str], proper_terms: set[str]) -> float:
    return sum(query_term_weight(term, proper_terms) for term in terms)


def is_side_task_query(query: str) -> bool:
    return bool(SIDE_TASK_QUERY_RE.search(query))


def is_side_task_session(rows: list[sqlite3.Row], fallback: sqlite3.Row) -> bool:
    sample = "\n".join(
        compact(f"{row_field(item, 'title')}\n{row_field(item, 'text')}", 1000)
        for item in [fallback, *rows[:12]]
    )
    return bool(SIDE_TASK_SESSION_RE.search(sample))


def is_corpus_dump_session(rows: list[sqlite3.Row], fallback: sqlite3.Row) -> bool:
    sample = "\n".join(
        compact(f"{row_field(item, 'title')}\n{row_field(item, 'text')}", 1800)
        for item in [fallback, *rows[:20]]
    )
    return bool(CORPUS_DUMP_RE.search(sample))


def is_corpus_dump_card(rows: list[sqlite3.Row], fallback: sqlite3.Row, query: str) -> bool:
    card = build_session_card(rows, fallback, query)
    sample = "\n".join(
        [card.title, card.last_user_message, card.what_this_was, card.what_happened, card.next_clue]
    )
    return bool(CORPUS_DUMP_RE.search(sample))


def proper_query_terms(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for match in TOKEN_RE.finditer(query):
        raw = match.group(0)
        if not raw[:1].isupper():
            continue
        token = normalize_token(raw)
        if (len(token) < 3 and token not in IMPORTANT_SHORT_TERMS) or token in STOP_WORDS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def exact_literal_query(query: str) -> str:
    cleaned = sanitize_text(query).strip().lower()
    if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", cleaned):
        return cleaned
    if re.fullmatch(r"https?://\S+", cleaned):
        return cleaned
    return ""


def recency_boost(timestamp: int | None, current_time: int | None = None) -> float:
    """Return a bounded tie-breaking boost for recent user activity."""
    if timestamp is None:
        return 0.0
    now = int(current_time if current_time is not None else now_ts())
    age_seconds = max(0, now - int(timestamp))
    age_days = age_seconds / 86_400
    return 0.35 * math.exp(-age_days / 21.0)


def rerank_session_results(
    conn: sqlite3.Connection,
    results: list[tuple[sqlite3.Row, float, str]],
    query: str,
    limit: int,
) -> list[tuple[sqlite3.Row, float, str]]:
    proper_terms = set(proper_query_terms(query))
    anchor_terms = query_anchor_terms(query)
    total_anchor_weight = anchor_weight(anchor_terms, proper_terms)
    meta_query = is_meta_query(query)
    literal = exact_literal_query(query)
    exact_session_keys: set[tuple[str, str]] = set()
    if literal:
        for candidate, _score, _label in results:
            candidate_rows = session_rows(conn, candidate)
            if literal in full_session_text(candidate_rows).lower():
                exact_session_keys.add((str(candidate["source"]), str(candidate["session_id"])))
    reranked: list[tuple[sqlite3.Row, float, str, int | None]] = []
    for row, score, label in results:
        if exact_session_keys and (str(row["source"]), str(row["session_id"])) not in exact_session_keys:
            continue
        rows = session_rows(conn, row)
        adjusted = score
        anchor_coverage = 1.0
        card_coverage = 1.0
        action_misses: list[str] = []
        meta_session = is_meta_session(conn, row)
        if meta_session and not meta_query:
            continue
        penalty = 0.0 if meta_query else (10.0 if meta_session else 0.0)
        if meta_query and meta_session:
            adjusted += 2.5
        if proper_terms:
            sample = full_session_text(rows)
            missing_proper_terms = [term for term in proper_terms if not term_present(term, sample)]
            penalty += 2.5 * len(missing_proper_terms)
            card = build_session_card(rows, row, query)
            card_text = "\n".join(
                [
                    card.title,
                    card.what_this_was,
                    card.what_happened,
                    card.next_clue,
                    " ".join(item.text for item in card.evidence),
                ]
            ).lower()
            weak_card_terms = [term for term in proper_terms if not term_present(term, card_text)]
            penalty += 1.5 * len(weak_card_terms)

        if anchor_terms and total_anchor_weight:
            sample = full_session_text(rows)
            card = build_session_card(rows, row, query)
            card_text = "\n".join(
                [
                    card.title,
                    card.what_this_was,
                    card.what_happened,
                    card.next_clue,
                    " ".join(card.mentioned_paths),
                    " ".join(item.text for item in card.evidence),
                ]
            ).lower()
            matched_session = matched_anchor_terms(anchor_terms, sample)
            matched_card = matched_anchor_terms(anchor_terms, card_text)
            session_weight = anchor_weight(matched_session, proper_terms)
            card_weight = anchor_weight(matched_card, proper_terms)
            missing_terms = [term for term in anchor_terms if term not in matched_session]
            missing_weight = anchor_weight(missing_terms, proper_terms)
            anchor_coverage = session_weight / total_anchor_weight
            card_coverage = card_weight / total_anchor_weight
            adjusted += 0.75 * (session_weight / total_anchor_weight)
            adjusted += 0.45 * (card_weight / total_anchor_weight)
            adjusted -= 0.22 * missing_weight
            action_misses = [term for term in missing_terms if term in ACTION_ANCHOR_TERMS]
            adjusted -= 0.35 * len(action_misses)

        if not is_side_task_query(query) and is_side_task_session(rows, row):
            continue
        if not CORPUS_DUMP_QUERY_RE.search(query):
            if is_corpus_dump_session(rows, row) or is_corpus_dump_card(rows, row, query):
                continue

        enough_query_match = not anchor_terms or anchor_coverage >= 0.45
        if not meta_query and len(anchor_terms) >= 3 and card_coverage < 0.6:
            enough_query_match = False
        action_terms = [term for term in anchor_terms if term in ACTION_ANCHOR_TERMS]
        if action_terms and action_misses == action_terms:
            enough_query_match = False
        strong_fuzzy_match = (
            any(mode in label for mode in ("local", "hybrid"))
            and score >= 0.25
        )
        strong_semantic_match = "semantic" in label and score >= 0.75
        if strong_fuzzy_match or strong_semantic_match:
            enough_query_match = True
        if enough_query_match:
            activity_ts = last_user_prompt_ts(rows, row)
            combined_score = adjusted - penalty + recency_boost(activity_ts)
            reranked.append((row, combined_score, label, activity_ts))
    reranked.sort(
        key=lambda item: (
            item[1],
            item[3] or 0,
            row_field(item[0], "ts", 0) or 0,
            str(row_field(item[0], "doc_id", "")),
        ),
        reverse=True,
    )
    return [(row, score, label) for row, score, label, _activity_ts in reranked[:limit]]


def row_context_quality(row: sqlite3.Row) -> int:
    quality = 0
    if sanitize_text(row["cwd"] or ""):
        quality += 3
    title = sanitize_text(row["title"] or "")
    if title and not is_weak_title(title):
        quality += 2
    path = sanitize_text(row["path"] or "")
    if path and not path.endswith(("history.jsonl", "state_5.sqlite")):
        quality += 1
    return quality


def source_label(source: str) -> str:
    return {
        "claude": "Claude Code",
        "codex": "Codex",
        "vscode": "VS Code / Copilot",
        "cursor": "Cursor",
    }.get(source, source)


def compact(text: str, limit: int = 96) -> str:
    text = re.sub(r"\s+", " ", sanitize_text(text)).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def is_machine_title(title: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9-]{24,}", title.strip(), flags=re.I))


def is_weak_title(title: str) -> bool:
    title = sanitize_text(title)
    if not title or is_machine_title(title):
        return True
    if LOW_SIGNAL_SESSION_LINE_RE.search(title):
        return True
    if title.lower() in {"ls", "pwd", "cd", "git status", "status"}:
        return True
    return len(tokenize(title)) <= 1 and len(title) < 12


def candidate_title(row: sqlite3.Row) -> str:
    title = sanitize_text(row["title"] or "")
    if title and not is_weak_title(title):
        return clean_card_line(title, 110)
    text = sanitize_text(row["text"] or "")
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return clean_card_line(first_line, 110) or "(untitled session)"


def row_meta(row: sqlite3.Row) -> dict[str, Any]:
    try:
        parsed = json.loads(row["meta_json"] or "{}")
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def session_rows(conn: sqlite3.Connection, row: sqlite3.Row) -> list[sqlite3.Row]:
    rows = list(
        conn.execute(
            """
            SELECT *
            FROM documents
            WHERE source = ? AND session_id = ?
            ORDER BY COALESCE(ts, 0) DESC, doc_id DESC
            """,
            (row["source"], row["session_id"]),
        )
    )
    return rows or [row]


def best_session_title(rows: list[sqlite3.Row], fallback: sqlite3.Row) -> str:
    fallback_title = candidate_title(fallback)
    if fallback_title and fallback_title != "(untitled session)":
        return fallback_title
    for row in rows:
        title = sanitize_text(row["title"] or "")
        if title and not is_weak_title(title):
            return compact(title, 110)
    return fallback_title


def best_session_ts(rows: list[sqlite3.Row], fallback: sqlite3.Row) -> int | None:
    timestamps = [row["ts"] for row in rows if row["ts"]]
    if timestamps:
        return int(max(timestamps))
    return fallback["ts"]


def is_user_prompt_row(row: sqlite3.Row) -> bool:
    role = str(row_field(row, "role", "")).lower()
    if role == "user":
        return True
    text = str(row_field(row, "text", ""))
    return role == "turn" and bool(re.search(r"(?:^|\n)\s*USER\s*:", text, flags=re.I))


def last_user_prompt_ts(rows: list[sqlite3.Row], fallback: sqlite3.Row) -> int | None:
    timestamps = [
        row_field(row, "ts", None)
        for row in rows
        if is_user_prompt_row(row) and row_field(row, "ts", None)
    ]
    if timestamps:
        return int(max(timestamps))
    if is_user_prompt_row(fallback) and row_field(fallback, "ts", None):
        return int(row_field(fallback, "ts"))
    return best_session_ts(rows, fallback)


def full_user_message_text(row: sqlite3.Row) -> str:
    if not is_user_prompt_row(row):
        return ""
    if "/subagents/" in str(row_field(row, "path", "")):
        return ""
    text = sanitize_text(row_field(row, "text", ""))
    if str(row_field(row, "role", "")).lower() == "turn":
        matches = list(re.finditer(r"(?:^|\n)\s*USER\s*:\s*", text, flags=re.I))
        if matches:
            text = text[matches[-1].end() :]
            text = re.split(r"\n\s*(?:ASSISTANT|SYSTEM)\s*:\s*", text, maxsplit=1, flags=re.I)[0]
    if re.match(
        r"^\s*<(?:task-notification|local-command|command-name|command-message|command-args|turn_aborted)\b",
        text,
        flags=re.I,
    ):
        return ""
    if INJECTED_USER_MESSAGE_RE.match(text):
        return ""
    return text


def user_message_text(row: sqlite3.Row) -> str:
    return compact(full_user_message_text(row), 320)


def last_user_message(rows: list[sqlite3.Row], fallback: sqlite3.Row) -> str:
    candidates = sorted(
        (row for row in rows if is_user_prompt_row(row)),
        key=lambda row: (row_field(row, "ts", 0) or 0, str(row_field(row, "doc_id", ""))),
        reverse=True,
    )
    for row in candidates:
        text = user_message_text(row)
        if text:
            return text
    return user_message_text(fallback)


def recent_user_messages(
    rows: list[sqlite3.Row],
    fallback: sqlite3.Row,
    limit: int = 3,
) -> list[str]:
    messages: list[str] = []
    seen: set[str] = set()
    candidates = sorted(
        (row for row in rows if is_user_prompt_row(row)),
        key=lambda row: (row_field(row, "ts", 0) or 0, str(row_field(row, "doc_id", ""))),
        reverse=True,
    )
    for row in candidates:
        message = user_message_text(row)
        key = message.lower()
        if not message or key in seen:
            continue
        seen.add(key)
        messages.append(message)
        if len(messages) >= limit:
            break
    if not messages:
        message = user_message_text(fallback)
        if message:
            messages.append(message)
    return messages


def is_close_session_message(text: str) -> bool:
    return classify_session_intent(sanitize_text(text)).kind is IntentKind.CLOSE


def session_archive_row(conn: sqlite3.Connection, source: str, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM session_archive_status WHERE source = ? AND session_id = ?",
        (source, session_id),
    ).fetchone()


def session_is_archived(conn: sqlite3.Connection, source: str, session_id: str) -> bool:
    row = session_archive_row(conn, source, session_id)
    return bool(row and row["archived"])


def session_status_text(archived: bool) -> str:
    return "ARCHIVED" if archived else "ACTIVE"


def session_status_marker(archived: bool) -> str:
    return " [ARCHIVED]" if archived else ""


def set_session_archive_status(
    conn: sqlite3.Connection,
    source: str,
    session_id: str,
    archived: bool,
    origin: str,
    evidence: str = "",
    evidence_ts: int | None = None,
    status_at: int | None = None,
    evidence_doc_id: str = "",
    parser_version: int = ARCHIVE_INTENT_VERSION,
    force: bool = True,
) -> None:
    apply_archive_transition(
        conn,
        source,
        session_id,
        archived,
        origin,
        evidence=evidence,
        evidence_ts=evidence_ts,
        evidence_doc_id=evidence_doc_id,
        parser_version=parser_version,
        status_at=status_at,
        force=force,
    )


def apply_archive_transition(
    conn: sqlite3.Connection,
    source: str,
    session_id: str,
    archived: bool,
    origin: str,
    evidence: str = "",
    evidence_ts: int | None = None,
    evidence_doc_id: str = "",
    parser_version: int = ARCHIVE_INTENT_VERSION,
    status_at: int | None = None,
    force: bool = False,
) -> bool:
    current = session_archive_row(conn, source, session_id)
    event_ts = int(evidence_ts or status_at or now_ts())
    current_ts = int(current["status_at"] or 0) if current else 0
    if not force and event_ts < current_ts:
        return False
    if not force and current and event_ts == current_ts:
        if str(current["origin"] or "") == "manual":
            return False
        current_doc_id = str(current["evidence_doc_id"] or "")
        if evidence_doc_id and current_doc_id and evidence_doc_id <= current_doc_id:
            return False
    previous = bool(current and current["archived"])
    event_id = stable_hash(
        "|".join(
            (
                source,
                session_id,
                origin,
                str(event_ts),
                evidence_doc_id,
                "1" if archived else "0",
            )
        ),
        32,
    )
    if conn.execute("SELECT 1 FROM session_archive_events WHERE event_id = ?", (event_id,)).fetchone():
        return False
    conn.execute(
        """
        INSERT INTO session_archive_status (
            source, session_id, archived, origin, status_at, evidence_ts, evidence,
            evidence_doc_id, parser_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, session_id) DO UPDATE SET
            archived = excluded.archived,
            origin = excluded.origin,
            status_at = excluded.status_at,
            evidence_ts = excluded.evidence_ts,
            evidence = excluded.evidence,
            evidence_doc_id = excluded.evidence_doc_id,
            parser_version = excluded.parser_version
        """,
        (
            source,
            session_id,
            1 if archived else 0,
            origin,
            int(status_at or event_ts),
            evidence_ts,
            compact(evidence, 320),
            evidence_doc_id,
            parser_version,
        ),
    )
    conn.execute(
        """
        INSERT INTO session_archive_events (
            event_id, source, session_id, previous_archived, archived, intent,
            origin, event_ts, evidence_doc_id, evidence, parser_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            source,
            session_id,
            1 if previous else 0,
            1 if archived else 0,
            "close" if archived else "resume",
            origin,
            event_ts,
            evidence_doc_id,
            compact(evidence, 320),
            parser_version,
            now_ts(),
        ),
    )
    return True


def archive_migration_audit(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    proposals: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    rows = conn.execute(
        """
        SELECT * FROM session_archive_status
        WHERE origin IN ('detected-close', 'detected-resume')
          AND parser_version < ?
        ORDER BY source, session_id
        """,
        (ARCHIVE_INTENT_VERSION,),
    ).fetchall()
    for current in rows:
        source = str(current["source"])
        session_id = str(current["session_id"])
        evidence_doc_id = str(current["evidence_doc_id"] or "")
        evidence_row = conn.execute(
            """
            SELECT * FROM documents
            WHERE source = ? AND session_id = ? AND doc_id = ?
            """,
            (source, session_id, evidence_doc_id),
        ).fetchone()
        if evidence_row is None:
            unresolved.append(
                {
                    "source": source,
                    "session_id": session_id,
                    "evidence_doc_id": evidence_doc_id,
                    "reason": "original-evidence-missing",
                }
            )
            continue
        message = full_user_message_text(evidence_row)
        decision = classify_session_intent(message)
        target = decision.kind is IntentKind.CLOSE
        current_archived = bool(current["archived"])
        if decision.kind is IntentKind.NONE and not current_archived:
            continue
        if target != current_archived:
            proposals.append(
                {
                    "source": source,
                    "session_id": session_id,
                    "evidence_doc_id": evidence_doc_id,
                    "evidence_ts": int(row_field(evidence_row, "ts", 0) or 0),
                    "archived": target,
                    "reason": decision.reason,
                    "evidence": message,
                }
            )
    return proposals, unresolved


def sync_detected_archive_states(conn: sqlite3.Connection) -> int:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    changed = 0
    stored_version_row = conn.execute(
        "SELECT value FROM session_archive_meta WHERE key = 'intent_parser_version'"
    ).fetchone()
    stored_version = int(stored_version_row["value"]) if stored_version_row else 0
    full_recheck = stored_version != ARCHIVE_INTENT_VERSION
    scan_row = conn.execute(
        "SELECT value FROM session_archive_meta WHERE key = 'archive_scan_indexed_at'"
    ).fetchone()
    last_scan = int(scan_row["value"]) if scan_row else 0
    max_indexed = int(
        conn.execute("SELECT MAX(COALESCE(indexed_at, 0)) FROM documents").fetchone()[0] or 0
    )

    if full_recheck:
        proposals, _unresolved = archive_migration_audit(conn)
        for proposal in proposals:
            if apply_archive_transition(
                conn,
                str(proposal["source"]),
                str(proposal["session_id"]),
                bool(proposal["archived"]),
                "migration-repair",
                evidence=str(proposal["evidence"]),
                evidence_ts=int(proposal["evidence_ts"]),
                evidence_doc_id=str(proposal["evidence_doc_id"]),
                force=True,
            ):
                changed += 1

    if full_recheck:
        rows = conn.execute(
            """
            SELECT * FROM documents
            WHERE role = 'user'
            ORDER BY source, session_id, COALESCE(ts, 0), doc_id
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            WITH changed AS (
                SELECT DISTINCT source, session_id
                FROM documents
                WHERE role = 'user' AND indexed_at > ?
            )
            SELECT d.*
            FROM documents d
            JOIN changed c ON c.source = d.source AND c.session_id = d.session_id
            WHERE d.role = 'user'
            ORDER BY d.source, d.session_id, COALESCE(d.ts, 0), d.doc_id
            """,
            (last_scan,),
        ).fetchall()

    for (source, session_id), grouped in itertools.groupby(
        rows,
        key=lambda row: (str(row["source"]), str(row["session_id"])),
    ):
        direct_rows = [
            (row, message)
            for row in grouped
            if (message := full_user_message_text(row))
        ]
        if not direct_rows:
            continue
        for message_row, message in direct_rows:
            if not has_session_intent_candidate(message):
                continue
            message_ts = int(row_field(message_row, "ts", 0) or 0)
            decision = classify_session_intent(message)
            if decision.kind is IntentKind.NONE:
                continue
            archived = decision.kind is IntentKind.CLOSE
            if apply_archive_transition(
                conn,
                source,
                session_id,
                archived,
                "detected-close" if archived else "detected-resume",
                evidence=message,
                evidence_ts=message_ts,
                evidence_doc_id=str(row_field(message_row, "doc_id", "")),
                status_at=message_ts or now_ts(),
            ):
                changed += 1
    conn.execute(
        """
        INSERT INTO session_archive_meta(key, value) VALUES('intent_parser_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(ARCHIVE_INTENT_VERSION),),
    )
    conn.execute(
        """
        INSERT INTO session_archive_meta(key, value) VALUES('archive_scan_indexed_at', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(max_indexed),),
    )
    conn.commit()
    return changed


STATE_SENTENCE_RE = re.compile(
    r"\b(?:decided|completed|finished|fixed|implemented|built|validated|closed|shipped|done)\b",
    re.I,
)
RESUME_SENTENCE_RE = re.compile(
    r"\b(?:still\s+need(?:ed|s)?|remain(?:s|ed|ing)?|next|continue|resume|before|unfinished|left|need(?:ed|s)?\s+to)\b",
    re.I,
)
DASHBOARD_CLUE_STOPWORDS = {
    "about", "after", "again", "also", "before", "being", "could", "current",
    "everything", "first", "from", "have", "into", "needed", "really", "session",
    "should", "still", "that", "their", "there", "these", "this", "those", "want",
    "what", "when", "where", "which", "with", "work", "would", "your",
}


def dashboard_sentences(text: str) -> list[str]:
    clean = sanitize_text(text)
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", clean) if part.strip()]


def dashboard_topic(card: SessionCard) -> str:
    title = re.sub(r"^\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+[—-]\s+", "", card.title)
    first = dashboard_sentences(title)[0] if dashboard_sentences(title) else title
    prefix = first.split(":", 1)[0].strip()
    if ":" in first and 2 <= len(prefix.split()) <= 8:
        return compact(prefix, 120)
    return compact(first, 160)


def dashboard_key_terms(text: str, limit: int = 4) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)+|[A-Za-z][A-Za-z0-9]{3,}", text):
        term = raw.strip("-/")
        key = term.lower()
        if key in DASHBOARD_CLUE_STOPWORDS or key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def dashboard_summary(
    card: SessionCard,
    rows: list[sqlite3.Row],
    fallback: sqlite3.Row,
) -> tuple[str, str, str, str]:
    requests = recent_user_messages(rows, fallback, limit=3)
    latest = requests[0] if requests else card.last_user_message
    latest_sentences = dashboard_sentences(latest)

    state = next((sentence for sentence in latest_sentences if STATE_SENTENCE_RE.search(sentence)), "")
    if not state:
        state = card.what_happened or card.what_this_was

    resume = next((sentence for sentence in latest_sentences if RESUME_SENTENCE_RE.search(sentence)), "")
    if not resume:
        resume = card.next_clue or latest
    resume = re.sub(r"\s+i\s+want\s+this\s+one\.?$", ".", resume, flags=re.I)

    clue = ""
    if card.mentioned_paths:
        clue = f"Mentioned path: {card.mentioned_paths[0]}"
    else:
        terms = dashboard_key_terms(" ".join((dashboard_topic(card), latest, state, resume)))
        if terms:
            clue = "Key terms: " + " · ".join(terms)
        elif card.repo:
            clue = f"Work folder: {card.repo}"

    fields = quality_gate(dashboard_topic(card), state, resume)
    return (
        compact(fields.about, 180),
        compact(fields.state, 240),
        compact(fields.resume, 260),
        compact(clue, 200),
    )


def timestamped_session_title(title: str, timestamp: int | None) -> str:
    clean = re.sub(r"^\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+[—-]\s+", "", sanitize_text(title))
    clean = clean or "Untitled session"
    if timestamp is None:
        return compact(clean, 110)
    stamp = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).astimezone().strftime("%d/%m/%y %H:%M:%S")
    return compact(f"{stamp} — {clean}", 110)


def best_repo(rows: list[sqlite3.Row], fallback: sqlite3.Row) -> str:
    candidates: list[str] = []
    for row in [fallback, *rows]:
        cwd = sanitize_text(row["cwd"] or "")
        if cwd:
            candidates.append(cwd)
        meta = row_meta(row)
        for key in ("cwd", "workspace", "workspaceFolder"):
            value = meta.get(key)
            if isinstance(value, str) and value:
                candidates.append(sanitize_text(value))

    seen: set[str] = set()
    unique_candidates: list[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique_candidates.append(candidate)

    for candidate in unique_candidates:
        if candidate.startswith("/") and pathlib.Path(candidate).exists():
            return candidate
    for candidate in unique_candidates:
        if candidate.startswith("/"):
            return candidate
    return ""


def row_field(row: sqlite3.Row, key: str, default: Any = "") -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def row_sort_key(row: sqlite3.Row) -> tuple[int, str]:
    return (int(row_field(row, "ts", 0) or 0), str(row_field(row, "doc_id", "")))


def normalize_card_typos(text: str) -> str:
    replacements = {
        r"\bfo rreview\b": "for review",
        r"\bhtihnk\b": "think",
        r"\bstrucutre\b": "structure",
        r"\bstructrue\b": "structure",
        r"\bprefernces\b": "preferences",
        r"\bknkow\b": "know",
        r"\bworkihg\b": "working",
        r"\bapopintment\b": "appointment",
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


def extract_task_clause(text: str) -> str:
    match = TASK_CLAUSE_RE.search(text)
    if not match:
        return text
    clause = match.group(1).strip()
    stop = TASK_STOP_RE.search(clause)
    if stop:
        clause = clause[: stop.start()].strip()
    return clause.rstrip(" .;") + ("." if clause and not clause.endswith((".", "?", "!")) else "")


def strip_card_filler(text: str) -> str:
    text = re.sub(r"^\[Image #\d+\]\s*", "", text)
    text = re.sub(r"^(?:ok|okay|yea|yeah|honestly|dude|bro|first honest to god)[,.\s]+", "", text, flags=re.I)
    return text.strip()


def clean_card_line(text: str, limit: int = 220) -> str:
    text = sanitize_text(text)
    text = re.sub(r"^❯\s*", "", text)
    text = extract_task_clause(text)
    text = strip_card_filler(text)
    text = normalize_card_typos(text)
    text = re.sub(r"\s+", " ", text).strip()
    return compact(text, limit)


def meaningful_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in sanitize_text(text).splitlines():
        line = line.strip()
        if not line or NOISE_LINE_RE.search(line):
            continue
        if line.startswith("{") and line.endswith("}") and len(line) > 120:
            continue
        if len(tokenize(line)) < 3 and len(line) < 24:
            continue
        lines.append(line)
    return lines


def is_question_like(line: str) -> bool:
    stripped = line.strip().lower()
    return "?" in stripped or stripped.startswith(
        (
            "are you",
            "can you",
            "do we",
            "does this",
            "is this",
            "so is",
            "what should",
        )
    )


def is_fragment_like(line: str) -> bool:
    stripped = line.strip().lower()
    return stripped.endswith((" and", " or", " to", " with", " for", " from", " then"))


def line_quality_score(line: str) -> float:
    score = 0.0
    stripped = line.strip()
    lower = stripped.lower()
    if TASK_CLAUSE_RE.search(stripped):
        score += 3.0
    if GOAL_WORD_RE.search(stripped):
        score += 1.5
    if ACTION_WORD_RE.search(stripped):
        score += 0.8
    if PATH_RE.search(stripped):
        score += 0.4
    if CONFIG_LINE_RE.search(stripped):
        score -= 2.5
    if SUMMARY_TAG_RE.search(stripped):
        score -= 2.0
    if LOW_SIGNAL_SESSION_LINE_RE.search(stripped):
        score -= 2.0
    if is_question_like(stripped):
        score -= 0.5
    if len(stripped) < 24:
        score -= 0.5
    if sum(1 for char in stripped if char.isupper()) > max(12, len(stripped) * 0.6):
        score -= 0.7
    if re.search(r"\b(?:fuck|fucking|shit|goddamn)\b", lower):
        score -= 0.4
    return score


def query_line_score(line: str, query: str) -> float:
    lower = line.lower()
    score = line_quality_score(line)
    for token in query_anchor_terms(query):
        if term_present(token, lower):
            score += query_term_weight(token, set())
            if token in ACTION_ANCHOR_TERMS:
                score += 1.0
    return score


def row_match_score(row: sqlite3.Row, query: str) -> float:
    haystack = f"{row_field(row, 'title')}\n{row_field(row, 'cwd')}\n{row_field(row, 'text')}".lower()
    score = 0.0
    for token in query_anchor_terms(query):
        if term_present(token, haystack):
            score += query_term_weight(token, set())
            if token in ACTION_ANCHOR_TERMS:
                score += 1.25
    return score


def best_line_for_row(
    row: sqlite3.Row,
    query: str = "",
    pattern: re.Pattern[str] | None = None,
    allow_questions: bool = True,
    avoid_texts: set[str] | None = None,
) -> str:
    avoid = avoid_texts or set()
    lines = meaningful_lines(str(row_field(row, "text", "")))
    if not lines:
        cleaned = clean_card_line(str(row_field(row, "text", "")))
        return "" if cleaned in avoid else cleaned
    if pattern:
        matches: list[tuple[float, int, str]] = []
        for i, line in enumerate(lines):
            if pattern.search(line) and (
                allow_questions or (not is_question_like(line) and not is_fragment_like(line))
            ):
                cleaned = clean_card_line(line)
                if cleaned in avoid:
                    continue
                score = query_line_score(line, query) if query else line_quality_score(line)
                matches.append((score, -i, line))
        if matches:
            matches.sort(reverse=True)
            return clean_card_line(matches[0][2])
        if avoid:
            return ""
        if not allow_questions:
            return ""
    if query_anchor_terms(query):
        scored: list[tuple[float, int, str]] = []
        for i, line in enumerate(lines):
            scored.append((query_line_score(line, query), -i, line))
        scored.sort(reverse=True)
        if scored and scored[0][0] > 0:
            return clean_card_line(scored[0][2])
    lines.sort(key=lambda line: line_quality_score(line), reverse=True)
    for line in lines:
        cleaned = clean_card_line(line)
        if cleaned not in avoid:
            return cleaned
    return ""


def base_topic_score(row: sqlite3.Row) -> float:
    text = str(row_field(row, "text", ""))
    title = str(row_field(row, "title", ""))
    line = best_line_for_row(row)
    combined = f"{title}\n{text}\n{line}"
    score = line_quality_score(line)
    role = str(row_field(row, "role", "")).lower()
    if role == "user":
        score += 1.0
    elif role == "session":
        score += 0.6
    if title and not is_weak_title(title):
        score += 0.4
    if LOW_SIGNAL_SESSION_LINE_RE.search(combined):
        score -= 2.0
    return score


def choose_base_topic_row(rows: list[sqlite3.Row], best_row: sqlite3.Row) -> sqlite3.Row:
    candidates = [best_row, *rows]
    candidates.sort(
        key=lambda row: (
            base_topic_score(row),
            1 if str(row_field(row, "role", "")).lower() in {"user", "session"} else 0,
            -row_sort_key(row)[0],
        ),
        reverse=True,
    )
    return candidates[0]


def choose_topic_row(rows: list[sqlite3.Row], best_row: sqlite3.Row, query: str) -> sqlite3.Row:
    if not query:
        return choose_base_topic_row(rows, best_row)
    candidates = [best_row, *rows]
    candidates.sort(
        key=lambda row: (
            row_match_score(row, query),
            1 if str(row_field(row, "role", "")).lower() == "user" else 0,
            row_sort_key(row)[0],
        ),
        reverse=True,
    )
    if meaningful_lines(str(row_field(best_row, "text", ""))) and (
        not query or row_match_score(best_row, query) >= row_match_score(candidates[0], query)
    ):
        return best_row
    return candidates[0]


def choose_pattern_row(
    rows: list[sqlite3.Row],
    pattern: re.Pattern[str],
    preferred_roles: set[str],
    exclude_doc_ids: set[str],
    query: str = "",
    require_query_match: bool = False,
    require_line_query_match: bool = False,
    line_match_bypass_doc_ids: set[str] | None = None,
    avoid_texts: set[str] | None = None,
) -> sqlite3.Row | None:
    matches: list[tuple[float, float, int, int, str, sqlite3.Row]] = []
    anchors = query_anchor_terms(query)
    proper_terms = set(proper_query_terms(query))
    bypass_doc_ids = line_match_bypass_doc_ids or set()
    for row in rows:
        doc_id = str(row_field(row, "doc_id", ""))
        if doc_id in exclude_doc_ids:
            continue
        role = str(row_field(row, "role", "")).lower()
        if preferred_roles and role not in preferred_roles:
            continue
        text = str(row_field(row, "text", ""))
        if not pattern.search(text):
            continue
        best_line = best_line_for_row(
            row,
            query=query,
            pattern=pattern,
            allow_questions=False,
            avoid_texts=avoid_texts,
        )
        if not best_line:
            continue
        haystack = f"{row_field(row, 'title')}\n{row_field(row, 'cwd')}\n{text}\n{best_line}".lower()
        matched = matched_anchor_terms(anchors, haystack) if anchors else []
        line_matched = matched_anchor_terms(anchors, best_line.lower()) if anchors else []
        if require_query_match and anchors and not matched:
            continue
        if require_line_query_match and anchors and not line_matched and doc_id not in bypass_doc_ids:
            continue
        query_score = anchor_weight(matched, proper_terms) if anchors else 0.0
        matches.append(
            (
                query_score,
                line_quality_score(best_line),
                1 if role in preferred_roles else 0,
                row_sort_key(row)[0],
                str(row_field(row, "doc_id", "")),
                row,
            )
        )
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][5]


def mentioned_paths(rows: list[sqlite3.Row], limit: int = 6) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for match in PATH_RE.finditer(str(row_field(row, "text", ""))):
            path = match.group(0).strip(".,;:)")
            if not path or path in seen or noisy_path(path):
                continue
            seen.add(path)
            found.append(path)
            if len(found) >= limit:
                return tuple(found)
    return tuple(found)


def paths_from_evidence_rows(rows: list[sqlite3.Row], evidence: Iterable[EvidenceLine]) -> tuple[str, ...]:
    by_doc_id = {str(row_field(row, "doc_id", "")): row for row in rows}
    evidence_rows = [by_doc_id[item.doc_id] for item in evidence if item.doc_id in by_doc_id]
    return mentioned_paths(evidence_rows or rows)


def noisy_path(path: str) -> bool:
    if "/.codex/" in path or "/.claude/" in path:
        return True
    if path.endswith(("history.jsonl", "state_5.sqlite", "session_search.py")):
        return True
    if re.fullmatch(r"[a-f0-9-]{16,}\.[A-Za-z0-9]+", pathlib.Path(path).name, flags=re.I):
        return True
    return False


def evidence_line(
    label: str,
    row: sqlite3.Row,
    query: str = "",
    pattern: re.Pattern[str] | None = None,
    allow_questions: bool = True,
    avoid_texts: set[str] | None = None,
) -> EvidenceLine:
    return EvidenceLine(
        label=label,
        doc_id=str(row_field(row, "doc_id", "")),
        role=str(row_field(row, "role", "")),
        ts=row_field(row, "ts", None),
        text=best_line_for_row(
            row,
            query=query,
            pattern=pattern,
            allow_questions=allow_questions,
            avoid_texts=avoid_texts,
        ),
    )


def unique_evidence(items: Iterable[EvidenceLine]) -> tuple[EvidenceLine, ...]:
    out: list[EvidenceLine] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item.doc_id, item.label)
        if key in seen or not item.text:
            continue
        seen.add(key)
        out.append(item)
    return tuple(out)


def build_session_card(rows: list[sqlite3.Row], best_row: sqlite3.Row, query: str) -> SessionCard:
    ordered = sorted(rows or [best_row], key=row_sort_key)
    topic_row = choose_topic_row(ordered, best_row, query)
    topic_evidence = evidence_line("what this was", topic_row, query=query)
    excluded = {topic_evidence.doc_id}

    require_query_evidence = bool(query_anchor_terms(query))
    happened_row = choose_pattern_row(
        ordered,
        ACTION_WORD_RE,
        {"assistant", "system"},
        excluded,
        query=query,
        require_query_match=require_query_evidence,
    )
    if happened_row is None:
        happened_row = choose_pattern_row(
            ordered,
            REPORTED_CHANGE_RE,
            {"user"},
            excluded,
            query=query,
            require_query_match=require_query_evidence,
        )
    happened_pattern = (
        ACTION_WORD_RE
        if happened_row is not None and ACTION_WORD_RE.search(str(row_field(happened_row, "text", "")))
        else REPORTED_CHANGE_RE
    )
    happened_evidence = (
        evidence_line("what happened", happened_row, query=query, pattern=happened_pattern, allow_questions=False)
        if happened_row is not None
        else None
    )
    if happened_evidence is not None and not happened_evidence.text:
        happened_evidence = None
    if happened_evidence:
        excluded.add(happened_evidence.doc_id)

    next_excluded = set(excluded)
    next_excluded.discard(topic_evidence.doc_id)
    topic_key = row_sort_key(topic_row)
    next_candidates = [row for row in ordered if row_sort_key(row) >= topic_key]
    next_row = choose_pattern_row(
        next_candidates,
        NEXT_WORD_RE,
        {"assistant", "user"},
        next_excluded,
        query=query,
        require_query_match=require_query_evidence,
        require_line_query_match=require_query_evidence,
        line_match_bypass_doc_ids={topic_evidence.doc_id},
        avoid_texts={topic_evidence.text},
    )
    next_evidence = (
        evidence_line("next clue", next_row, query=query, pattern=NEXT_WORD_RE, avoid_texts={topic_evidence.text})
        if next_row is not None
        else None
    )
    if next_evidence is not None and next_evidence.text == topic_evidence.text:
        next_evidence = None

    evidence = unique_evidence(
        item for item in (topic_evidence, happened_evidence, next_evidence) if item is not None
    )
    active_ts = last_user_prompt_ts(ordered, best_row)
    return SessionCard(
        title=timestamped_session_title(best_session_title(ordered, topic_row), active_ts),
        source=str(row_field(best_row, "source", "")),
        session_id=str(row_field(best_row, "session_id", "")),
        repo=best_repo(ordered, best_row),
        last_active=active_ts,
        last_user_message=last_user_message(ordered, best_row),
        what_this_was=topic_evidence.text,
        what_happened=happened_evidence.text if happened_evidence else "",
        next_clue=next_evidence.text if next_evidence else "",
        mentioned_paths=paths_from_evidence_rows(ordered, evidence),
        evidence=evidence,
    )


def session_card_hash(rows: list[sqlite3.Row]) -> str:
    parts: list[str] = [CARD_VERSION]
    for row in sorted(rows, key=row_sort_key):
        text_hash = row_field(row, "text_hash", "") or stable_hash(str(row_field(row, "text", "")), 32)
        parts.append(
            "|".join(
                [
                    str(row_field(row, "doc_id", "")),
                    str(row_field(row, "ts", "")),
                    str(text_hash),
                ]
            )
        )
    return stable_hash("\n".join(parts), 32)


def evidence_to_json(evidence: Iterable[EvidenceLine]) -> str:
    return json.dumps(
        [
            {
                "label": item.label,
                "doc_id": item.doc_id,
                "role": item.role,
                "ts": item.ts,
                "text": item.text,
            }
            for item in evidence
        ],
        ensure_ascii=False,
    )


def evidence_from_json(value: str) -> tuple[EvidenceLine, ...]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    out: list[EvidenceLine] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        out.append(
            EvidenceLine(
                label=sanitize_text(item.get("label", "")),
                doc_id=sanitize_text(item.get("doc_id", "")),
                role=sanitize_text(item.get("role", "")),
                ts=parse_ts(item.get("ts")),
                text=sanitize_text(item.get("text", "")),
            )
        )
    return tuple(out)


def card_from_cache_row(row: sqlite3.Row) -> SessionCard:
    try:
        paths = json.loads(row["mentioned_paths_json"] or "[]")
    except json.JSONDecodeError:
        paths = []
    if not isinstance(paths, list):
        paths = []
    return SessionCard(
        title=str(row["title"] or ""),
        source=str(row["source"] or ""),
        session_id=str(row["session_id"] or ""),
        repo=str(row["repo"] or ""),
        last_active=row["last_active"],
        last_user_message="",
        what_this_was=str(row["what_this_was"] or ""),
        what_happened=str(row["what_happened"] or ""),
        next_clue=str(row["next_clue"] or ""),
        mentioned_paths=tuple(sanitize_text(path) for path in paths if sanitize_text(path)),
        evidence=evidence_from_json(str(row["evidence_json"] or "[]")),
    )


def store_session_card(conn: sqlite3.Connection, card: SessionCard, text_hash: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO session_cards (
            source, session_id, text_hash, title, repo, last_active,
            what_this_was, what_happened, next_clue, mentioned_paths_json,
            evidence_json, built_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card.source,
            card.session_id,
            text_hash,
            card.title,
            card.repo,
            card.last_active,
            card.what_this_was,
            card.what_happened,
            card.next_clue,
            json.dumps(list(card.mentioned_paths), ensure_ascii=False),
            evidence_to_json(card.evidence),
            now_ts(),
        ),
    )


def cached_base_session_card(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    best_row: sqlite3.Row,
    persist: bool = False,
) -> SessionCard:
    text_hash = session_card_hash(rows or [best_row])
    cached = conn.execute(
        """
        SELECT *
        FROM session_cards
        WHERE source = ? AND session_id = ? AND text_hash = ?
        """,
        (best_row["source"], best_row["session_id"], text_hash),
    ).fetchone()
    if cached is not None:
        return dataclasses.replace(
            card_from_cache_row(cached),
            last_user_message=last_user_message(rows, best_row),
        )

    card = build_session_card(rows, best_row, "")
    if persist:
        store_session_card(conn, card, text_hash)
        conn.commit()
    return card


def merge_query_card(base: SessionCard, query_card: SessionCard, query: str) -> SessionCard:
    if not query:
        return base
    return SessionCard(
        title=query_card.title or base.title,
        source=base.source,
        session_id=base.session_id,
        repo=query_card.repo or base.repo,
        last_active=query_card.last_active or base.last_active,
        last_user_message=query_card.last_user_message or base.last_user_message,
        what_this_was=query_card.what_this_was or base.what_this_was,
        what_happened=query_card.what_happened,
        next_clue=query_card.next_clue,
        mentioned_paths=query_card.mentioned_paths or base.mentioned_paths,
        evidence=query_card.evidence or base.evidence,
    )


def session_card_for_result(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    query: str = "",
    persist: bool = False,
) -> SessionCard:
    rows = session_rows(conn, row)
    base = cached_base_session_card(conn, rows, row, persist=persist)
    if not query:
        return base
    query_card = build_session_card(rows, row, query)
    return merge_query_card(base, query_card, query)


def ensure_session_cards(conn: sqlite3.Connection, limit: int | None = None, quiet: bool = True) -> int:
    count = 0
    with conn:
        for source, session_id in session_groups(conn, limit=None):
            row = representative_session_row(conn, source, session_id)
            if row is None:
                continue
            rows = rows_for_session(conn, source, session_id)
            text_hash = session_card_hash(rows)
            existing = conn.execute(
                """
                SELECT 1
                FROM session_cards
                WHERE source = ? AND session_id = ? AND text_hash = ?
                """,
                (source, session_id, text_hash),
            ).fetchone()
            if existing:
                continue
            card = build_session_card(rows, row, "")
            store_session_card(conn, card, text_hash)
            count += 1
            if limit is not None and count >= limit:
                break
    if not quiet:
        if count:
            print(f"Built {count} session cards.")
        else:
            print("Session cards are already up to date.")
    return count


def repo_label(repo: str) -> str:
    return repo or "unknown from index"


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def session_ref(source: str, session_id: str) -> str:
    return f"{source}: {session_id or 'unknown'}"


def native_resume_available(source: str, session_id: str) -> bool:
    return source in {"codex", "claude"} and bool(session_id and session_id != "unknown")


def native_resume_status(source: str, session_id: str) -> str:
    if native_resume_available(source, session_id):
        return f"exact in {source_label(source)}"
    return "unavailable"


def cross_tool_status(source: str) -> str:
    if source in {"codex", "claude"}:
        return "context packet only outside native owner"
    return "context packet only"


def packet_only_note(source: str) -> str:
    if source == "vscode":
        return "VS Code/Copilot exact chat reopen is not known yet; use a context packet to continue in Codex or Claude Code."
    if source == "cursor":
        return "Cursor exact chat reopen is not known yet; use a context packet to continue in Codex or Claude Code."
    return "Exact native reopen is not available from the indexed local data; use a context packet."


def native_resume_lines(source: str, session_id: str, repo: str) -> list[str]:
    if not native_resume_available(source, session_id):
        return []
    lines: list[str] = []
    if repo:
        lines.append(f"cd {shell_quote(repo)}")
    if source == "codex":
        lines.append(f"codex resume {shell_quote(session_id)}")
    elif source == "claude":
        lines.append(f"claude --resume {shell_quote(session_id)}")
    return lines


def handoff_targets_for(source: str) -> list[str]:
    targets = ["codex", "claude"]
    return [target for target in targets if target != source]


def target_label(target: str) -> str:
    return {"codex": "Codex", "claude": "Claude Code"}.get(target, target)


def normalize_target(raw: str) -> str:
    normalized = raw.strip().lower()
    aliases = {
        "cc": "claude",
        "claude": "claude",
        "claude-code": "claude",
        "claude_code": "claude",
        "codex": "codex",
        "codecs": "codex",
        "codecx": "codex",
    }
    if normalized not in aliases:
        raise ValueError(f"unsupported target: {raw}. Use codex or claude.")
    return aliases[normalized]


def infer_target_from_tokens(tokens: list[str]) -> str:
    for token in tokens:
        cleaned = re.sub(r"[^a-z_-]", "", token.lower())
        if not cleaned:
            continue
        try:
            return normalize_target(cleaned)
        except ValueError:
            continue
    return ""


def result_card(conn: sqlite3.Connection, row: sqlite3.Row, query: str, label: str) -> dict[str, Any]:
    rows = session_rows(conn, row)
    session_card = session_card_for_result(conn, row, query, persist=True)
    return {
        "row": row,
        "rows": rows,
        "session_card": session_card,
        "source": session_card.source,
        "source_label": source_label(session_card.source),
        "session_id": session_card.session_id,
        "archived": session_is_archived(conn, session_card.source, session_card.session_id),
        "title": session_card.title,
        "repo": session_card.repo,
        "last_active": session_card.last_active,
        "hits": hit_count(label),
        "why": why_line(query, row, label, rows),
        "evidence": snippet(row["text"], query),
    }


def card_visible_text(card: dict[str, Any]) -> str:
    session_card: SessionCard = card["session_card"]
    return "\n".join(
        [
            str(card["title"]),
            str(card["source_label"]),
            str(card["repo"]),
            session_card.last_user_message,
            session_card.what_this_was,
            session_card.what_happened,
            session_card.next_clue,
            " ".join(session_card.mentioned_paths),
            " ".join(item.text for item in session_card.evidence),
        ]
    )


def related_result_lines(
    cards: list[dict[str, Any]],
    current_rank: int,
    query: str,
    limit: int = 2,
) -> list[str]:
    if current_rank < 1 or current_rank > len(cards):
        return []
    current = cards[current_rank - 1]
    current_row = current["row"]
    current_repo = str(current["repo"] or "")
    current_terms = set(evidence_terms_from_text(query, card_visible_text(current), limit=12))
    strong_terms = {term for term in query_anchor_terms(query) if term in ACTION_ANCHOR_TERMS}
    candidates: list[tuple[float, int, dict[str, Any]]] = []
    for rank, card in enumerate(cards, 1):
        if rank == current_rank:
            continue
        row = card["row"]
        if row["source"] == current_row["source"] and row["session_id"] == current_row["session_id"]:
            continue
        if not is_side_task_query(query) and is_side_task_session(card["rows"], row):
            continue
        score = 0.0
        other_repo = str(card["repo"] or "")
        if current_repo and other_repo:
            if current_repo == other_repo:
                score += 2.0
            elif current_repo.startswith(other_repo) or other_repo.startswith(current_repo):
                score += 1.2
        if card["source"] != current["source"]:
            score += 0.4
        other_terms = set(evidence_terms_from_text(query, card_visible_text(card), limit=12))
        if strong_terms and not (other_terms & strong_terms):
            continue
        score += 0.35 * len(current_terms & other_terms)
        if score < 1.8:
            continue
        candidates.append((score, -rank, card))
    candidates.sort(reverse=True)
    lines: list[str] = []
    for _score, neg_rank, card in candidates[:limit]:
        rank = -neg_rank
        marker = session_status_marker(bool(card["archived"]))
        lines.append(f"{rank}. [{card['source_label']}]{marker} {compact(str(card['title']), 80)}")
    return lines


def owner_action_label(source: str, session_id: str) -> str:
    if native_resume_available(source, session_id):
        return f"opens exactly in {source_label(source)}"
    return "can continue from a context packet"


def alternate_harness_line(rank: int, source: str) -> str:
    targets = handoff_targets_for(source)
    if not targets:
        return ""
    labels = [target_label(target) for target in targets]
    if len(labels) == 1:
        target = targets[0]
        return f"continue in {labels[0]}: ss continue {rank} in {target}"
    parts = [f"{label}: ss continue {rank} in {target}" for label, target in zip(labels, targets)]
    return "continue elsewhere: " + " | ".join(parts)


def location_label(row: sqlite3.Row) -> str:
    cwd = sanitize_text(row["cwd"] or "")
    if cwd:
        return cwd
    path = sanitize_text(row["path"] or "")
    if path:
        return path
    return "unknown"


def evidence_terms_from_text(query: str, text: str, limit: int = 8) -> list[str]:
    haystack = text.lower()
    terms: list[str] = []
    seen: set[str] = set()
    for token in query_anchor_terms(query):
        clean = token.strip().lower()
        if clean in seen:
            continue
        if term_present(clean, haystack):
            terms.append(clean)
            seen.add(clean)
        if len(terms) >= limit:
            break
    return terms


def evidence_terms(query: str, row: sqlite3.Row, limit: int = 8) -> list[str]:
    return evidence_terms_from_text(
        query,
        f"{row['title']}\n{row['cwd']}\n{row['text']}",
        limit=limit,
    )


def hit_count(label: str) -> int:
    match = re.search(r"(\d+)\s+hits", label)
    if not match:
        return 1
    return int(match.group(1))


def why_line(
    query: str,
    row: sqlite3.Row,
    label: str,
    rows: list[sqlite3.Row] | None = None,
) -> str:
    if label.startswith("recent"):
        return "Recent activity from this session."
    terms = evidence_terms_from_text(query, full_session_text(rows)) if rows else evidence_terms(query, row)
    if terms:
        quoted = ", ".join(f'"{term}"' for term in terms)
        return f"Found remembered words/ideas: {quoted}."
    return "Closest candidate from the indexed session text; check the evidence snippet."


def snippet(text: str, query: str, width: int = 280) -> str:
    clean = re.sub(r"\s+", " ", sanitize_text(text))
    if len(clean) <= width:
        return clean
    tokens = tokenize(query)
    lower = clean.lower()
    positions = [lower.find(token.lower()) for token in tokens if lower.find(token.lower()) >= 0]
    start = min(positions) if positions else 0
    start = max(0, start - 60)
    end = min(len(clean), start + width)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(clean) else ""
    return prefix + clean[start:end] + suffix


def print_results(conn: sqlite3.Connection, results: list[tuple[sqlite3.Row, float, str]], query: str) -> None:
    if not results:
        print("No results.")
        return
    print(f"Best matches for: {query}")
    print()
    cards = [result_card(conn, row, query, label) for row, _score, label in results]
    for i, card in enumerate(cards, 1):
        session_card: SessionCard = card["session_card"]
        source = card["source"]
        session_id = card["session_id"]
        repo = card["repo"]
        hits = card["hits"]
        archive_marker = "[ARCHIVED] " if card["archived"] else ""
        print(f"{i}. {archive_marker}{card['title']}")
        print(f"   Found in: {card['source_label']} ({owner_action_label(source, session_id)})")
        print(f"   Work folder: {repo_label(repo)}")
        print(f"   Last touched: {iso_date(card['last_active'])}")
        print()
        print("   Session card:")
        print(f"     Last message from you: {session_card.last_user_message or '(not available in local evidence)'}")
        print(f"     What this was: {session_card.what_this_was}")
        if session_card.what_happened:
            print(f"     What happened: {session_card.what_happened}")
        if session_card.next_clue:
            print(f"     Next clue: {session_card.next_clue}")
        if session_card.mentioned_paths:
            print(f"     Mentioned paths: {', '.join(session_card.mentioned_paths[:3])}")
        print()
        print("   Why this might be it:")
        print(f"     {card['why']}")
        if hits > 1:
            print(f"     I found {hits} matching turns in this same session.")
        related = related_result_lines(cards, i, query) if i <= 3 else []
        if related:
            print()
            print("   Related sessions worth checking:")
            for line in related:
                print(f"     {line}")
        print()
        print("   What you can do:")
        native_lines = native_resume_lines(source, session_id, repo)
        if native_lines:
            print(f"     open exact session: ss open {i}")
        else:
            print("     exact reopen is not available for this result")
            print(f"     why: {packet_only_note(source)}")
        alternate = alternate_harness_line(i, source)
        if alternate:
            print(f"     {alternate}")
        print(f"     read more: ss look at {i}")
        print()


def recent_session_results(
    conn: sqlite3.Connection,
    limit: int,
    source_name: str = "all",
    archived_only: bool = False,
) -> list[tuple[sqlite3.Row, float, str]]:
    sources = ("claude", "codex") if source_name == "all" else (source_name,)
    placeholders = ",".join("?" for _ in sources)
    candidates = conn.execute(
        f"""
        SELECT source, session_id, MAX(COALESCE(ts, 0)) AS last_user_ts
        FROM documents
        WHERE source IN ({placeholders})
          AND role = 'user'
          AND path NOT LIKE '%/subagents/%'
        GROUP BY source, session_id
        ORDER BY last_user_ts DESC
        LIMIT ?
        """,
        [*sources, max(limit * 5, 50)],
    )
    results: list[tuple[sqlite3.Row, float, str]] = []
    for candidate in candidates:
        row = representative_session_row(conn, str(candidate["source"]), str(candidate["session_id"]))
        if row is None:
            continue
        archived = session_is_archived(conn, str(row["source"]), str(row["session_id"]))
        if archived != archived_only:
            continue
        rows = session_rows(conn, row)
        if not last_user_message(rows, row):
            continue
        activity_ts = last_user_prompt_ts(rows, row)
        results.append((row, recency_boost(activity_ts), "recent"))
        if len(results) >= limit:
            break
    return results


def archived_session_results(
    conn: sqlite3.Connection,
    limit: int,
    source_name: str = "all",
) -> tuple[list[tuple[sqlite3.Row, float, str]], int]:
    source_clause = "" if source_name == "all" else "AND s.source = ?"
    params: list[Any] = [] if source_name == "all" else [source_name]
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT d.*, s.status_at AS archive_status_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.source, d.session_id
                       ORDER BY COALESCE(d.ts, 0) DESC, d.doc_id DESC
                   ) AS archive_rank
            FROM session_archive_status s
            JOIN documents d
              ON d.source = s.source AND d.session_id = s.session_id
            WHERE s.archived = 1 {source_clause}
        )
        SELECT * FROM ranked
        WHERE archive_rank = 1
        ORDER BY archive_status_at DESC, source, session_id
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    missing = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM session_archive_status s
        WHERE s.archived = 1 {source_clause}
          AND NOT EXISTS (
              SELECT 1 FROM documents d
              WHERE d.source = s.source AND d.session_id = s.session_id
          )
        """,
        params,
    ).fetchone()[0]
    return [(row, recency_boost(row["archive_status_at"]), "archived") for row in rows], int(missing)


def dashboard_terminal_width() -> int:
    try:
        width = int(shutil.get_terminal_size(fallback=(100, 24)).columns)
    except (TypeError, ValueError, OSError):
        width = 100
    return max(60, min(width, 200))


def dashboard_cell(value: str, width: int) -> str:
    clean = re.sub(r"\s+", " ", sanitize_text(value)).strip()
    if width <= 1:
        return clean[:1]
    if len(clean) <= width:
        return clean
    return clean[: max(1, width - 1)].rstrip() + "…"


def _humanize_path_part(part: str) -> str:
    clean = sanitize_text(part).strip().strip("_").replace("-", " ").replace("_", " ")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean.title() if clean else "Unknown"


def dashboard_project_label(repo: str) -> str:
    """Turn a working directory into a stable, human-sized project name."""
    raw = sanitize_text(repo)
    if not raw or raw in {"unknown", "unknown from index"}:
        return "Unknown folder"
    path = pathlib.PurePath(raw)
    parts = [part for part in path.parts if part not in {"/", "\\"}]
    if not parts:
        return "Unknown folder"

    # Prefer the local multitool layout: .../<workspace>/os/<domain>/<project>
    try:
        os_at = parts.index("os")
    except ValueError:
        os_at = -1
    if os_at >= 0:
        relative = parts[os_at + 1 :]
        if not relative:
            return "Workspace home"
        if len(relative) >= 2:
            return f"{_humanize_path_part(relative[0])} / {_humanize_path_part(relative[1])}"
        return _humanize_path_part(relative[0])

    # Other paths: keep last two meaningful parts.
    useful = [part for part in parts if part not in {".", ""}]
    if len(useful) >= 2:
        return " / ".join(_humanize_path_part(part) for part in useful[-2:])
    return _humanize_path_part(useful[-1])


def print_dashboard_table(headers: list[str], rows: list[list[str]], width: int | None = None) -> None:
    """Render a plain terminal table and shrink cells to fit the live width."""
    if not headers:
        return
    term_width = dashboard_terminal_width() if width is None else max(40, width)
    col_count = len(headers)
    separator_tax = 3 * max(0, col_count - 1)  # " | " between columns
    available = max(col_count * 4, term_width - separator_tax)
    # Give earlier columns a stable minimum; pour remainder into the last column.
    min_widths = []
    for index, header in enumerate(headers):
        if index == 0:
            min_widths.append(max(len(header), 4))
        elif index == col_count - 1:
            min_widths.append(max(len(header), 12))
        else:
            min_widths.append(max(len(header), 8))
    while sum(min_widths) > available and any(value > 4 for value in min_widths[1:]):
        # Shrink supporting columns before the Open index.
        for index in range(col_count - 2, 0, -1):
            if min_widths[index] > 4 and sum(min_widths) > available:
                min_widths[index] -= 1
    remainder = max(0, available - sum(min_widths))
    widths = list(min_widths)
    widths[-1] += remainder

    fitted_rows: list[list[str]] = []
    for row in rows:
        fitted = []
        for index, cell in enumerate(row):
            fitted.append(dashboard_cell(str(cell), widths[index]))
        fitted_rows.append(fitted)

    print(" | ".join(headers[i].ljust(widths[i]) for i in range(col_count)))
    print("-+-".join("-" * widths[i] for i in range(col_count)))
    for row in fitted_rows:
        print(" | ".join(row[i].ljust(widths[i]) for i in range(col_count)))


def dashboard_project_summaries(
    conn: sqlite3.Connection,
    source_name: str = "all",
    thread_limit: int = 10,
    project_scan_limit: int = 500,
) -> list[dict[str, Any]]:
    """Build project rows from a wider scan than the visible thread feed.

    The visible thread list stays limit-honest. This companion map may look
    farther back so older projects do not disappear after a restart.
    """
    scan_limit = max(int(thread_limit), int(project_scan_limit), 1)
    scanned = recent_session_results(conn, scan_limit, source_name, archived_only=False)
    groups: dict[str, dict[str, Any]] = {}
    for row, _score, _label in scanned:
        card = session_card_for_result(conn, row, "", "recent")
        rows = session_rows(conn, row)
        about, _state, _resume, _clue = dashboard_summary(card, rows, row)
        repo = card.repo or location_label(row)
        project = dashboard_project_label(repo)
        bucket = groups.setdefault(
            project,
            {
                "project": project,
                "count": 0,
                "latest_ts": card.last_active or 0,
                "latest_about": about,
                "latest_session_id": card.session_id,
                "latest_source": card.source,
            },
        )
        bucket["count"] += 1
        ts = card.last_active or 0
        if ts >= int(bucket["latest_ts"] or 0):
            bucket["latest_ts"] = ts
            bucket["latest_about"] = about
            bucket["latest_session_id"] = card.session_id
            bucket["latest_source"] = card.source
    ordered = sorted(
        groups.values(),
        key=lambda item: (-int(item["latest_ts"] or 0), str(item["project"]).lower()),
    )
    return ordered


def print_dashboard(
    conn: sqlite3.Connection,
    results: list[tuple[sqlite3.Row, float, str]],
    archived_view: bool = False,
    project_summaries: list[dict[str, Any]] | None = None,
) -> None:
    width = dashboard_terminal_width()
    print("SS WORK MAP")
    print("Archived sessions" if archived_view else "Your saved work, organized by project")
    print("Nothing on this screen needs an open terminal tab. Closed chats stay saved.")
    print()
    if not results:
        print("No Claude Code or Codex sessions are indexed yet.")
        print("Run: ss fresh what did I work on recently")
        return

    thread_entries: list[dict[str, Any]] = []
    for rank, (row, _score, label) in enumerate(results, 1):
        card = session_card_for_result(conn, row, "", label)
        rows = session_rows(conn, row)
        about, state, resume, clue = dashboard_summary(card, rows, row)
        repo = card.repo or location_label(row)
        project = dashboard_project_label(repo)
        archived = session_is_archived(conn, card.source, card.session_id)
        if archived:
            project_display = f"{project} [ARCHIVED]"
        else:
            project_display = project
        thread_entries.append(
            {
                "rank": rank,
                "row": row,
                "card": card,
                "about": about,
                "state": state,
                "resume": resume,
                "clue": clue,
                "project": project_display,
                "source": source_label(str(row["source"])),
                "when": iso_date(card.last_active),
                "repo": repo,
                "archived": archived,
            }
        )

    # Project map may include older folders beyond the visible thread limit.
    if project_summaries is None:
        if archived_view:
            project_summaries = []
            grouped: dict[str, dict[str, Any]] = {}
            for entry in thread_entries:
                bucket = grouped.setdefault(
                    entry["project"],
                    {
                        "project": entry["project"],
                        "count": 0,
                        "latest_ts": entry["card"].last_active or 0,
                        "latest_about": entry["about"],
                        "latest_open": entry["rank"],
                    },
                )
                bucket["count"] += 1
                ts = entry["card"].last_active or 0
                if ts >= int(bucket["latest_ts"] or 0):
                    bucket["latest_ts"] = ts
                    bucket["latest_about"] = entry["about"]
                    bucket["latest_open"] = entry["rank"]
            project_summaries = sorted(
                grouped.values(),
                key=lambda item: (-int(item["latest_ts"] or 0), str(item["project"]).lower()),
            )
        else:
            project_summaries = dashboard_project_summaries(
                conn,
                source_name="all",
                thread_limit=len(results),
            )

    # Attach the open number of the newest *visible* thread for each project.
    visible_open_by_project: dict[str, int] = {}
    for entry in thread_entries:
        base_project = entry["project"].removesuffix(" [ARCHIVED]")
        if base_project not in visible_open_by_project:
            visible_open_by_project[base_project] = int(entry["rank"])

    print("PROJECTS")
    print("Newest visible thread number is the Fast open. Older projects can still appear here.")
    project_rows: list[list[str]] = []
    for item in project_summaries:
        project_name = str(item["project"])
        base_name = project_name.removesuffix(" [ARCHIVED]")
        open_rank = item.get("latest_open") or visible_open_by_project.get(base_name)
        open_label = str(open_rank) if open_rank else "-"
        project_rows.append(
            [
                open_label,
                project_name,
                iso_date(item.get("latest_ts")),
                str(item.get("count") or 0),
                str(item.get("latest_about") or ""),
            ]
        )
    print_dashboard_table(
        ["Open", "Project", "Last worked", "Threads", "Latest work"],
        project_rows,
        width=width,
    )

    print()
    print("THREADS")
    print(f"Showing {len(thread_entries)} session{'s' if len(thread_entries) != 1 else ''}. Open numbers match this screen only.")
    body_width = max(40, width - 4)
    for entry in thread_entries:
        marker = session_status_marker(bool(entry["archived"]))
        print()
        title_line = f"{entry['rank']}. [{entry['source']}]{marker} {entry['card'].title}"
        print(dashboard_cell(title_line, width))
        print(dashboard_cell(f"   Project: {entry['project']}", width))
        print(dashboard_cell(f"   Folder: {repo_label(entry['repo'])}", width))
        print(dashboard_cell(f"   Last worked: {entry['when']}", width))
        about = entry["about"]
        state = entry["state"]
        resume = entry["resume"]
        clue = entry["clue"]
        # Keep the required About/State/Resume markers; trim values to terminal width.
        about = dashboard_cell(about, max(12, body_width - len("About: ")))
        state = dashboard_cell(state, max(12, body_width - len("State: ")))
        resume = dashboard_cell(resume, max(12, body_width - len("Resume: ")))
        print(f"   About: {about}")
        print(f"   State: {state}")
        print(f"   Resume: {resume}")
        if clue:
            print(dashboard_cell(f"   Clue: {clue}", width))
        print(f"   Open: ss open {entry['rank']}    Details: ss look at {entry['rank']}")

    print()
    print("Commands: ss <search> | ss open N | ss look at N | ss archive N | ss archived")


def dashboard_is_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def dashboard_prompt(args: argparse.Namespace) -> int:
    if not dashboard_is_interactive():
        return 0
    while True:
        try:
            choice = input("Open a number, type search words, or q: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not choice or choice.lower() in {"q", "quit", "exit"}:
            return 0
        if choice.isdigit():
            print()
            return cmd_resume(argparse.Namespace(db=args.db, selector=choice))
        tokens = choice.split()
        if tokens and tokens[0].lower() in {"ss", "sessions"}:
            tokens = tokens[1:]
        if tokens and tokens[0].lower() == "archived":
            print()
            return cmd_archived(
                argparse.Namespace(
                    db=args.db,
                    home=args.home,
                    no_refresh=getattr(args, "no_refresh", False),
                    archived=True,
                    limit=args.limit,
                    source=args.source,
                    mode=args.mode,
                )
            )
        followup = parse_natural_followup(tokens)
        if followup is not None:
            followup.db = args.db
            print()
            return int(followup.func(followup))
        if tokens and tokens[0].lower() in {"fresh", "refresh", "new"}:
            print()
            return cmd_natural(
                argparse.Namespace(
                    db=args.db,
                    home=args.home,
                    no_refresh=getattr(args, "no_refresh", False),
                    fresh=False,
                    query=tokens,
                    limit=args.limit,
                    source=args.source,
                    mode=args.mode,
                )
            )
        query = " ".join(tokens)
        query = query[1:].strip() if query.startswith("/") else query
        if query:
            print()
            return cmd_search(
                argparse.Namespace(
                    db=args.db,
                    query=query,
                    limit=args.limit,
                    source=args.source,
                    mode=args.mode,
                )
            )


def cmd_dashboard(args: argparse.Namespace) -> int:
    with session_lock(shared=False):
        db_path = expand(args.db)
        if not db_path.exists():
            print(f"Index not found: {db_path}", file=sys.stderr)
            return 2
        conn = connect_db(db_path)
        init_db(conn)
        if not getattr(args, "no_refresh", False):
            refresh_dashboard_index(conn, expand(args.home))
        archived_view = bool(getattr(args, "archived", False))
        missing_archived = 0
        if archived_view:
            results, missing_archived = archived_session_results(conn, args.limit, args.source)
            project_summaries = None
        else:
            # Exact limit for the thread list and open numbers.
            results = recent_session_results(conn, args.limit, args.source)
            # Wider project scan so older folders still appear after a restart.
            project_summaries = dashboard_project_summaries(
                conn,
                source_name=getattr(args, "source", "all") or "all",
                thread_limit=args.limit,
            )
        save_last_results(results, "recent sessions dashboard", db_path)
        print_dashboard(
            conn,
            results,
            archived_view=archived_view,
            project_summaries=project_summaries,
        )
        if missing_archived:
            print(f"{missing_archived} archived session(s) are missing from the index. Run: ss fresh archived")
        conn.close()
    return dashboard_prompt(args)


def cmd_archived(args: argparse.Namespace) -> int:
    args.archived = True
    args.no_refresh = getattr(args, "no_refresh", False)
    args.home = getattr(args, "home", "~")
    return cmd_dashboard(args)


def cmd_archive_audit(args: argparse.Namespace) -> int:
    with session_lock(shared=True):
        db_path = expand(args.db)
        if not db_path.exists():
            print(f"Index not found: {db_path}", file=sys.stderr)
            return 2
        conn = connect_db(db_path)
        init_db(conn)
        quick_check(conn)
        proposals, unresolved = archive_migration_audit(conn)
        print(f"Archive parser migration audit: v{ARCHIVE_INTENT_VERSION}")
        print(f"Proposed state repairs: {len(proposals)}")
        print(f"Unresolved evidence: {len(unresolved)}")
        for proposal in proposals:
            state = "ARCHIVED" if proposal["archived"] else "ACTIVE"
            print(f"  {proposal['source']}:{proposal['session_id']} -> {state} ({proposal['reason']})")
        for item in unresolved:
            print(
                f"  REVIEW {item['source']}:{item['session_id']} "
                f"missing {item['evidence_doc_id'] or 'evidence document id'}"
            )
        conn.close()
    return 0


def cmd_set_archive(args: argparse.Namespace) -> int:
    with session_lock(shared=False):
        db_path, conn = connect_selector_db(args.selector, args.db)
        if conn is None:
            print_selector_db_error(db_path)
            return 2
        row = selected_row(conn, args.selector)
        if row is None:
            print(f"Not found: {args.selector}", file=sys.stderr)
            return 2
        archived = bool(args.archived)
        try:
            with immediate_transaction(conn):
                set_session_archive_status(
                    conn,
                    str(row["source"]),
                    str(row["session_id"]),
                    archived,
                    "manual",
                    evidence=f"manual {'archive' if archived else 'unarchive'} via selector {args.selector}",
                )
        except Exception:
            conn.rollback()
            conn.close()
            raise
        card = session_card_for_result(conn, row, query_from_selector(args.selector))
        print(f"{'Archived' if archived else 'Unarchived'}: {card.title}")
        print(f"Session: {session_ref(str(row['source']), str(row['session_id']))}")
        conn.close()
    return 0


def print_session_card_detail(card: SessionCard, archived: bool = False) -> None:
    print("Session card")
    print(f"Title: {card.title}")
    print(f"Found in: {source_label(card.source)}")
    print(f"Work folder: {repo_label(card.repo)}")
    print(f"Session: {session_ref(card.source, card.session_id)}")
    print(f"Status: {session_status_text(archived)}")
    print(f"Last touched: {iso_date(card.last_active)}")
    print()
    print(f"Last message from you: {card.last_user_message or 'not available in local evidence'}")
    print(f"What this was: {card.what_this_was}")
    if card.what_happened:
        print(f"What happened: {card.what_happened}")
    else:
        print("What happened: no clear progress line found in local evidence")
    if card.next_clue:
        print(f"Next clue: {card.next_clue}")
    else:
        print("Next clue: no clear next-action line found in local evidence")
    if card.mentioned_paths:
        print(f"Mentioned paths: {', '.join(card.mentioned_paths)}")
    print()
    print("Evidence")
    for item in card.evidence:
        print(f"- {item.label} [{item.role}, {iso_date(item.ts)}]: {item.text}")
    print()


def load_last_results(db_path: pathlib.Path | None = None) -> dict[str, Any]:
    """Load the most relevant selector mapping for this terminal/agent context."""
    candidates = [result_context_path(), expand(DEFAULT_LAST_RESULTS)]
    if db_path is not None:
        # Prefer mappings written against the same private database when present.
        preferred = []
        for candidate in candidates:
            preferred.append(candidate)
        candidates = preferred
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if db_path is not None and sanitize_text(payload.get('db') or '') not in {'', str(db_path)}:
            continue
        if isinstance(payload, dict) and isinstance(payload.get('results'), list):
            return payload
    return {"results": []}


def save_last_results(results: list[tuple[sqlite3.Row, float, str]], query: str, db_path: pathlib.Path) -> None:
    context_kind, _context_value = result_context_identity()
    payload = {
        "schema_version": 2,
        "query": query,
        "db": str(db_path),
        "saved_at": now_ts(),
        "context_kind": context_kind,
        "results": [
            {
                "rank": i,
                "doc_id": row["doc_id"],
                "source": row["source"],
                "session_id": row["session_id"],
                "title": row["title"],
                "ts": row["ts"],
                "score": score,
                "match": mode,
            }
            for i, (row, score, mode) in enumerate(results, 1)
        ],
    }
    context_path = result_context_path()
    global_path = expand(DEFAULT_LAST_RESULTS)
    atomic_write_json(context_path, payload)
    if context_path != global_path:
        atomic_write_json(global_path, payload)


def result_context_identity() -> tuple[str, str]:
    candidates = (
        ("explicit", "SS_RESULT_CONTEXT"),
        ("codex", "CODEX_THREAD_ID"),
        ("claude", "CLAUDE_CODE_SESSION_ID"),
        ("claude", "CLAUDE_SESSION_ID"),
        ("terminal", "TERM_SESSION_ID"),
        ("terminal", "ITERM_SESSION_ID"),
    )
    for kind, name in candidates:
        value = sanitize_text(os.environ.get(name, ""))
        if value:
            return kind, value

    for stream in (sys.stdin, sys.stdout):
        try:
            if stream.isatty():
                value = os.ttyname(stream.fileno())
                if value:
                    return "tty", value
        except (AttributeError, OSError, ValueError):
            continue
    return "global", "global"


def result_context_path() -> pathlib.Path:
    kind, value = result_context_identity()
    global_path = expand(DEFAULT_LAST_RESULTS)
    if kind == "global":
        return global_path
    digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()[:24]
    return global_path.parent / "result-contexts" / f"{kind}-{digest}.json"


def atomic_write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                print(
                    f"Warning: selector context committed but directory durability sync failed: {exc}",
                    file=sys.stderr,
                )
        finally:
            os.close(directory_fd)
    finally:
        tmp_path.unlink(missing_ok=True)


def last_results_payload() -> dict[str, Any]:
    path = result_context_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("schema_version") not in {None, 2}:
        return {}
    return payload


def last_result_context(selector: str) -> dict[str, Any] | None:
    if not selector.isdigit():
        return None
    payload = last_results_payload()
    rank = int(selector)
    for item in payload.get("results", []):
        if isinstance(item, dict) and item.get("rank") == rank and item.get("doc_id"):
            context = dict(item)
            context["query"] = sanitize_text(payload.get("query", ""))
            context["db"] = sanitize_text(payload.get("db", ""))
            return context
    return None


def query_from_selector(selector: str) -> str:
    context = last_result_context(selector)
    return sanitize_text(context.get("query", "")) if context is not None else ""


def db_path_from_selector(selector: str, default_db: str) -> pathlib.Path:
    context = last_result_context(selector)
    if context is not None:
        saved_db = sanitize_text(context.get("db", ""))
        if saved_db and saved_db != ":memory:":
            return expand(saved_db)
    return expand(default_db)


def connect_selector_db(selector: str, default_db: str) -> tuple[pathlib.Path, sqlite3.Connection | None]:
    db_path = db_path_from_selector(selector, default_db)
    if not db_path.is_file():
        return db_path, None
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_db(db_path)
        conn.execute("SELECT 1 FROM documents LIMIT 0").fetchone()
    except sqlite3.Error:
        if conn is not None:
            conn.close()
        return db_path, None
    return db_path, conn


def print_selector_db_error(db_path: pathlib.Path) -> None:
    print(f"Session index unavailable: {db_path}", file=sys.stderr)
    print("Run a fresh search first: ss fresh <what you remember>", file=sys.stderr)


def selected_row(conn: sqlite3.Connection, selector: str) -> sqlite3.Row | None:
    if not selector.isdigit():
        return conn.execute("SELECT * FROM documents WHERE doc_id = ?", (selector,)).fetchone()

    context = last_result_context(selector)
    if context is None:
        return None

    doc_id = str(context.get("doc_id", ""))
    if doc_id:
        row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        if row is not None:
            return row

    source = str(context.get("source", ""))
    session_id = str(context.get("session_id", ""))
    if source and session_id:
        return representative_session_row(conn, source, session_id)
    return None


def print_resume_instructions(conn: sqlite3.Connection, row: sqlite3.Row, selector: str, query: str = "") -> None:
    rows = session_rows(conn, row)
    card = session_card_for_result(conn, row, query)
    repo = card.repo or best_repo(rows, row)
    source = row["source"]
    session_id = row["session_id"]
    title = card.title or best_session_title(rows, row)
    print(f"Found in: {source_label(source)}")
    print(f"Work folder: {repo_label(repo)}")
    print(f"Session: {session_ref(source, session_id)}")
    print(f"Status: {session_status_text(session_is_archived(conn, str(source), str(session_id)))}")
    print(f"Name: {title}")
    if query:
        print(f"Search query: {query}")
    print()
    lines = native_resume_lines(source, session_id, repo)
    if lines:
        print("Open exact session:")
        for line in lines:
            print(f"  {line}")
        return
    print("I cannot reopen this exact session from the local data.")
    print(packet_only_note(source))
    for target in handoff_targets_for(source):
        print(f"Continue in {target_label(target)} with context: ss continue {selector} in {target}")


def mark_cli_resume(conn: sqlite3.Connection, row: sqlite3.Row, selector: str, origin: str) -> bool:
    source = str(row["source"])
    session_id = str(row["session_id"])
    try:
        with immediate_transaction(conn):
            if not session_is_archived(conn, source, session_id):
                return False
            return apply_archive_transition(
                conn,
                source,
                session_id,
                False,
                origin,
                evidence=f"explicit resume via selector {selector}",
                evidence_ts=now_ts(),
                evidence_doc_id=str(row["doc_id"]),
                force=True,
            )
    except Exception:
        conn.rollback()
        raise


def handoff_rows(rows: list[sqlite3.Row], best_row: sqlite3.Row) -> list[sqlite3.Row]:
    by_id: dict[str, sqlite3.Row] = {best_row["doc_id"]: best_row}
    ordered = sorted(rows, key=lambda row: (row["ts"] or 0, row["doc_id"]))
    if len(ordered) <= HANDOFF_MAX_ROWS:
        for row in ordered:
            by_id[row["doc_id"]] = row
    else:
        for row in ordered[:4]:
            by_id[row["doc_id"]] = row
        for row in ordered[-(HANDOFF_MAX_ROWS - 4) :]:
            by_id[row["doc_id"]] = row
    return sorted(by_id.values(), key=lambda row: (row["ts"] or 0, row["doc_id"]))


def handoff_packet_text(
    row: sqlite3.Row,
    target: str,
    query: str,
    selector: str,
    packet_rows: list[sqlite3.Row],
    card: SessionCard | None = None,
    archived: bool = False,
) -> str:
    if card is None:
        card = build_session_card(packet_rows, row, query)
    repo = card.repo
    title = card.title
    source = row["source"]
    session_id = row["session_id"]
    lines = [
        "# Session Context Packet",
        "",
        "This is a cross-harness context packet. It is not a native session resume.",
        "",
        "## Original Session",
        "",
        f"- Owner: {source_label(source)}",
        f"- Native session: {session_ref(source, session_id)}",
        f"- Session name: {title}",
        f"- Status: {session_status_text(archived)}",
        f"- Last active: {iso_date(best_session_ts(packet_rows, row))}",
        f"- Repo: {repo_label(repo)}",
        f"- Source path: {row['path']}",
        f"- Search selector: {selector}",
        f"- Search query: {query or '(unknown)'}",
        f"- Target harness: {target_label(target)}",
        "",
        "## Local Session Card",
        "",
        f"- Last message from you: {card.last_user_message or 'Not available in local evidence.'}",
        f"- What this was: {card.what_this_was}",
        f"- What happened: {card.what_happened or 'No clear progress line found in local evidence.'}",
        f"- Next clue: {card.next_clue or 'No clear next-action line found in local evidence.'}",
        f"- Mentioned paths: {', '.join(card.mentioned_paths) if card.mentioned_paths else 'None found.'}",
        "",
        "## Card Evidence",
        "",
    ]
    for item in card.evidence:
        lines.append(f"- {item.label} [{item.role}, {iso_date(item.ts)}]: {item.text}")
    lines.extend(
        [
            "",
            "## Resume Instructions",
            "",
            "Continue the work from the context below. Treat the source session as evidence,",
            "not as a live native session. Preserve the original repo/workspace boundary if",
            "the repo is known. If the repo is unknown, ask for the intended working directory",
            "before editing files.",
            "",
            "## Best Matching Evidence",
            "",
            snippet(row["text"], query or title, width=900),
            "",
            "## Indexed Session Context",
            "",
        ]
    )
    for item in packet_rows:
        text = compact(item["text"], HANDOFF_MAX_ROW_CHARS)
        lines.extend(
            [
                f"### {iso_date(item['ts'])} | {item['role']} | {item['doc_id']}",
                "",
                text,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def handoff_path(row: sqlite3.Row, target: str) -> pathlib.Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    short_id = stable_hash(f"{row['source']}:{row['session_id']}")[:10]
    filename = f"{stamp}_{row['source']}_{short_id}_to_{target}.md"
    return expand(DEFAULT_HANDOFF_DIR) / filename


def launch_lines_for_handoff(target: str, repo: str, packet_path: pathlib.Path) -> list[str]:
    packet_dir = str(packet_path.parent)
    prompt = f"Read the context packet at {packet_path} and continue the work."
    lines: list[str] = []
    if target == "codex":
        parts = ["codex"]
        if repo:
            parts.extend(["-C", shell_quote(repo)])
        parts.extend(["--add-dir", shell_quote(packet_dir), shell_quote(prompt)])
        lines.append(" ".join(parts))
    elif target == "claude":
        if repo:
            lines.append(f"cd {shell_quote(repo)}")
        lines.append(f"claude --add-dir {shell_quote(packet_dir)} {shell_quote(prompt)}")
    return lines


def cmd_index(args: argparse.Namespace) -> int:
    with session_lock(shared=False):
        db_path = expand(args.db)
        home = expand(args.home)
        conn = connect_db(db_path)
        init_db(conn)
        if args.reset:
            reset_db(conn)
        sources = normalize_sources(args.source)
        count = upsert_documents(conn, build_docs(home, sources))
        sync_detected_archive_states(conn)
        if not args.quiet:
            print(f"Indexed {count} documents into {db_path}")
    return 0


def run_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    source_name: str = "all",
    mode: str = "hybrid",
) -> tuple[str, list[tuple[sqlite3.Row, float, str]]]:
    source_name, cleaned_query = infer_source_from_query(query, source_name)
    cleaned_query, since, until, activity = infer_time_and_intent(cleaned_query)
    source = None if source_name == "all" else source_name
    search_limit = max(limit * 4, 25)

    if activity and not cleaned_query:
        results = group_results_by_session(
            search_recent(conn, search_limit, source, since, until),
            search_limit,
        )
        return query, rerank_session_results(conn, results, query, limit)

    fts_rows: list[sqlite3.Row] = []
    local_rows: list[tuple[sqlite3.Row, float]] = []
    semantic_rows: list[tuple[sqlite3.Row, float]] = []
    if mode in {"fts", "hybrid"}:
        fts_rows = search_fts(conn, cleaned_query, search_limit, source, since, until)
    if mode in {"local", "hybrid"}:
        local_rows = search_local(conn, cleaned_query, search_limit, source, since, until)
    if mode == "hybrid":
        semantic_rows = search_semantic(conn, cleaned_query, search_limit, source, since, until)

    if mode == "fts":
        results = [
            (row, 1.0 - (i * 0.05), "fts")
            for i, row in enumerate(fts_rows[:search_limit])
        ]
    elif mode == "local":
        results = [(row, score, "local") for row, score in local_rows[:search_limit]]
    else:
        results = merge_results(fts_rows, local_rows, semantic_rows, search_limit)
    results = group_results_by_session(results, search_limit)
    return cleaned_query, rerank_session_results(conn, results, cleaned_query, limit)


def cmd_search(args: argparse.Namespace) -> int:
    with session_lock(shared=False):
        db_path = expand(args.db)
        if not db_path.exists():
            print(f"Index not found: {db_path}", file=sys.stderr)
            print("Run: python3 session_search.py index --reset", file=sys.stderr)
            return 2
        conn = connect_db(db_path)
        init_db(conn)
        if not getattr(args, "no_refresh", False):
            refresh_dashboard_index(conn, expand(getattr(args, "home", "~")))
        else:
            sync_detected_archive_states(conn)
        display_query, results = run_search(conn, args.query, args.limit, args.source, args.mode)
        save_last_results(results, display_query, db_path)
        print_results(conn, results, display_query)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with session_lock(shared=True):
        db_path, conn = connect_selector_db(args.doc_id, args.db)
        if conn is None:
            print_selector_db_error(db_path)
            return 2
        row = selected_row(conn, args.doc_id)
        if row is None:
            print(f"Not found: {args.doc_id}", file=sys.stderr)
            return 2
        query = query_from_selector(args.doc_id)
        card = session_card_for_result(conn, row, query)
        print_session_card_detail(
            card,
            session_is_archived(conn, str(row["source"]), str(row["session_id"])),
        )
        if query:
            print(f"Search query: {query}")
            print()
        print(f"[{row['source']}] {iso_date(row['ts'])} {row['title']}")
        print(f"id: {row['doc_id']}")
        print(f"path: {row['path']}")
        if row["cwd"]:
            print(f"cwd: {row['cwd']}")
        print(f"role: {row['role']}")
        print()
        print(row["text"])
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    with session_lock(shared=False):
        db_path, conn = connect_selector_db(args.selector, args.db)
        if conn is None:
            print_selector_db_error(db_path)
            return 2
        row = selected_row(conn, args.selector)
        if row is None:
            conn.close()
            print(f"Not found: {args.selector}", file=sys.stderr)
            return 2
        mark_cli_resume(conn, row, args.selector, "cli-open")
        print_resume_instructions(conn, row, args.selector, query_from_selector(args.selector))
        conn.close()
    return 0


def cmd_continue(args: argparse.Namespace) -> int:
    target = ""
    if getattr(args, "target", ""):
        try:
            target = normalize_target(args.target)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    with session_lock(shared=False):
        db_path, conn = connect_selector_db(args.selector, args.db)
        if conn is None:
            print_selector_db_error(db_path)
            return 2
        row = selected_row(conn, args.selector)
        if row is None:
            conn.close()
            print(f"Not found: {args.selector}", file=sys.stderr)
            return 2
        mark_cli_resume(conn, row, args.selector, "cli-continue")
        if not target or (target == row["source"] and native_resume_available(row["source"], row["session_id"])):
            print_resume_instructions(conn, row, args.selector, query_from_selector(args.selector))
            conn.close()
            return 0
        conn.close()

    return cmd_handoff(argparse.Namespace(db=args.db, selector=args.selector, target=target))


def cmd_handoff(args: argparse.Namespace) -> int:
    try:
        target = normalize_target(args.target)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    with session_lock(shared=True):
        db_path, conn = connect_selector_db(args.selector, args.db)
        if conn is None:
            print_selector_db_error(db_path)
            return 2
        row = selected_row(conn, args.selector)
        if row is None:
            print(f"Not found: {args.selector}", file=sys.stderr)
            return 2

        query = query_from_selector(args.selector)

        rows = session_rows(conn, row)
        packet_rows = handoff_rows(rows, row)
        repo = best_repo(packet_rows, row)
        if target == row["source"] and native_resume_available(row["source"], row["session_id"]):
            print("That is the native owner. Open the exact session instead:")
            for line in native_resume_lines(row["source"], row["session_id"], repo):
                print(f"  {line}")
            return 0

        packet_path = handoff_path(row, target)
        packet_path.parent.mkdir(parents=True, exist_ok=True)
        card = session_card_for_result(conn, row, query)
        packet_path.write_text(
            handoff_packet_text(
                row,
                target,
                query,
                args.selector,
                packet_rows,
                card,
                archived=session_is_archived(conn, str(row["source"]), str(row["session_id"])),
            ),
            encoding="utf-8",
        )

        print(f"Context packet: {packet_path}")
        print(f"Original owner: {source_label(row['source'])}")
        print(f"Original session: {session_ref(row['source'], row['session_id'])}")
        print(f"Target harness: {target_label(target)}")
        print(f"Repo: {repo_label(repo)}")
        print()
        print(f"Continue in {target_label(target)}:")
        for line in launch_lines_for_handoff(target, repo, packet_path):
            print(f"  {line}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with session_lock(shared=True):
        db_path = expand(args.db)
        if not db_path.exists():
            print(f"Index not found: {db_path}")
            return 0
        conn = connect_db(db_path)
        init_db(conn)
        total = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
        print(f"db: {db_path}")
        print(f"documents: {total}")
        for row in conn.execute(
            """
            SELECT source, count(*) AS n, max(ts) AS newest
            FROM documents
            GROUP BY source
                ORDER BY source
                """
        ):
            print(f"{row['source']}: {row['n']} newest={iso_date(row['newest'])}")
        if embedding_backend() is None:
            print("semantic: unavailable (local model is not installed or cached)")
        else:
            embedded_sessions = conn.execute(
                """
                SELECT count(*)
                FROM session_embeddings e
                WHERE e.model = ?
                  AND EXISTS (
                    SELECT 1
                    FROM documents d
                    WHERE d.source = e.source AND d.session_id = e.session_id
                  )
                """,
                (EMBED_MODEL_VERSION,),
            ).fetchone()[0]
            total_sessions = conn.execute(
                "SELECT count(*) FROM (SELECT 1 FROM documents GROUP BY source, session_id)"
            ).fetchone()[0]
            print(f"semantic: {embedded_sessions}/{total_sessions} sessions embedded with {EMBED_MODEL}")
            embedded_turns = conn.execute(
                "SELECT count(*) FROM embeddings WHERE model = ?",
                (EMBED_MODEL_VERSION,),
            ).fetchone()[0]
            print(f"semantic turns: {embedded_turns}/{total} cached (run `ss embed` to backfill)")
        card_rows = conn.execute(
            """
            SELECT count(*)
            FROM session_cards c
            WHERE EXISTS (
                SELECT 1
                FROM documents d
                WHERE d.source = c.source AND d.session_id = c.session_id
            )
            """
        ).fetchone()[0]
        total_sessions = conn.execute(
            "SELECT count(*) FROM (SELECT 1 FROM documents GROUP BY source, session_id)"
        ).fetchone()[0]
        print(f"cards: {card_rows}/{total_sessions} sessions cached")
    return 0


def cmd_capabilities(_args: argparse.Namespace) -> int:
    print(render_capabilities())
    return 0


def cmd_demo(_args: argparse.Namespace) -> int:
    from demo_runner import run_demo

    print(run_demo())
    return 0


def cmd_cards(args: argparse.Namespace) -> int:
    with session_lock(shared=False):
        db_path = expand(args.db)
        if not db_path.exists():
            print(f"Index not found: {db_path}", file=sys.stderr)
            return 2
        conn = connect_db(db_path)
        init_db(conn)
        ensure_session_cards(conn, limit=args.limit, quiet=False)
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    with session_lock(shared=False):
        db_path = expand(args.db)
        if not db_path.exists():
            print(f"Index not found: {db_path}", file=sys.stderr)
            return 2
        conn = connect_db(db_path)
        init_db(conn)
        session_count = ensure_session_embeddings(conn, limit=args.limit, quiet=False)
        document_count = ensure_embeddings(conn, limit=args.limit, quiet=False)
        if session_count == 0 and document_count == 0 and embedding_backend() is not None:
            print("Semantic index is already up to date.")
    return 0


def eval_text_for_result(conn: sqlite3.Connection, row: sqlite3.Row, query: str, label: str = "") -> str:
    rows = session_rows(conn, row)
    card = session_card_for_result(conn, row, query)
    parts = [
        str(row["source"]),
        source_label(str(row["source"])),
        owner_action_label(str(row["source"]), str(row["session_id"])),
        repo_label(card.repo),
        card.title,
        card.what_this_was,
        card.what_happened,
        card.next_clue,
        " ".join(card.mentioned_paths),
        " ".join(item.text for item in card.evidence),
        why_line(query, row, label, rows),
    ]
    return "\n".join(parts).lower()


def normalize_eval_terms(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [values.lower()]
    if isinstance(values, list):
        return [str(value).lower() for value in values]
    return [str(values).lower()]


def eval_rule_matches(row: sqlite3.Row, text: str, rule: Any) -> bool:
    if isinstance(rule, str):
        return rule.lower() in text
    if isinstance(rule, list):
        return all(str(value).lower() in text for value in rule)
    if not isinstance(rule, dict):
        return False

    source = rule.get("source")
    if source and str(row["source"]) != str(source):
        return False
    session_id = rule.get("session_id")
    if session_id and str(row["session_id"]) != str(session_id):
        return False

    contains = normalize_eval_terms(rule.get("contains"))
    if any(term not in text for term in contains):
        return False
    contains_all = normalize_eval_terms(rule.get("contains_all"))
    if any(term not in text for term in contains_all):
        return False
    contains_any = normalize_eval_terms(rule.get("contains_any"))
    if contains_any and not any(term in text for term in contains_any):
        return False
    not_contains = normalize_eval_terms(rule.get("not_contains"))
    if any(term in text for term in not_contains):
        return False
    return bool(source or session_id or contains or contains_all or contains_any or not_contains)


def load_eval_cases(path: pathlib.Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"Cannot read eval file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Eval file is not valid JSON: {path}") from exc
    cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(cases, list):
        raise RuntimeError("Eval file must be a JSON list or an object with a 'cases' list.")
    cleaned: list[dict[str, Any]] = []
    for item in cases:
        if not isinstance(item, dict) or not item.get("query"):
            raise RuntimeError("Every eval case must be an object with a query.")
        cleaned.append(item)
    return cleaned


def result_rank_for_rules(
    conn: sqlite3.Connection,
    results: list[tuple[sqlite3.Row, float, str]],
    query: str,
    rules: list[Any],
) -> tuple[int | None, sqlite3.Row | None]:
    for rank, (row, _score, label) in enumerate(results, 1):
        text = eval_text_for_result(conn, row, query, label)
        if any(eval_rule_matches(row, text, rule) for rule in rules):
            return rank, row
    return None, None


def eval_title(conn: sqlite3.Connection, row: sqlite3.Row | None, query: str) -> str:
    if row is None:
        return "(none)"
    card = session_card_for_result(conn, row, query)
    return compact(card.title or str(row["title"] or row["text"]), 90)


def cmd_eval(args: argparse.Namespace) -> int:
    eval_path = expand(args.file)
    cases = load_eval_cases(eval_path)
    with session_lock(shared=False):
        db_path = expand(args.db)
        if not db_path.exists():
            print(f"Index not found: {db_path}", file=sys.stderr)
            return 2
        conn = connect_db(db_path)
        init_db(conn)
        if args.refresh:
            reset_db(conn)
            upsert_documents(conn, build_docs(expand(args.home), normalize_sources("all")))

        passed = 0
        failed = 0
        print(f"Session search evals: {eval_path}")
        print()
        for i, case in enumerate(cases, 1):
            query = sanitize_text(case["query"])
            limit = int(case.get("limit", args.limit))
            source = str(case.get("source", "all"))
            mode = str(case.get("mode", args.mode))
            display_query, results = run_search(conn, query, limit, source, mode)
            accept_rules = list(case.get("accept") or [])
            reject_rules = list(case.get("reject") or [])
            expected_within = int(case.get("expected_within", 1))
            bad_before = int(case.get("bad_before", 1))

            good_rank, good_row = result_rank_for_rules(conn, results[:expected_within], display_query, accept_rules)
            bad_rank, bad_row = result_rank_for_rules(conn, results[:bad_before], display_query, reject_rules)

            ok = (not accept_rules or good_rank is not None) and bad_rank is None
            status = "PASS" if ok else "FAIL"
            if ok:
                passed += 1
            else:
                failed += 1

            top = results[0][0] if results else None
            print(f"{status} {i}. {query}")
            print(f"   top: {eval_title(conn, top, display_query)}")
            if accept_rules:
                found = f"rank {good_rank}: {eval_title(conn, good_row, display_query)}" if good_rank else "not found"
                print(f"   expected by rank {expected_within}: {found}")
            if reject_rules:
                found = f"rank {bad_rank}: {eval_title(conn, bad_row, display_query)}" if bad_rank else "none"
                print(f"   rejected before rank {bad_before}: {found}")
            if getattr(args, "verbose", False):
                for rank, (row, _score, label) in enumerate(results[: min(limit, 5)], 1):
                    print(f"   {rank}. [{row['source']}] {eval_title(conn, row, display_query)} ({label})")
            print()

        print(f"Summary: {passed} passed, {failed} failed")
        return 0 if failed == 0 else 1


def cmd_natural(args: argparse.Namespace) -> int:
    query_parts = list(args.query)
    force_refresh = bool(getattr(args, "fresh", False))
    if query_parts and query_parts[0].lower() in {"fresh", "refresh", "new"}:
        force_refresh = True
        query_parts = query_parts[1:]
    query = " ".join(query_parts).strip()
    db_path = expand(args.db)
    if not args.no_refresh and (force_refresh or not db_path.exists()):
        cmd_index(
            argparse.Namespace(
                db=args.db,
                home=args.home,
                source="all",
                reset=True,
                quiet=not force_refresh,
            )
        )
    if not query:
        return cmd_dashboard(args)
    return cmd_search(
        argparse.Namespace(
            db=args.db,
            query=query,
            limit=args.limit,
            source=args.source,
            mode=args.mode,
            home=args.home,
            no_refresh=args.no_refresh,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search local AI session history.")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite index path. Default: {DEFAULT_DB}")
    parser.add_argument("--home", default="~", help="Home directory containing .codex/.claude/Library.")
    sub = parser.add_subparsers(dest="command", required=True)

    index = sub.add_parser("index", help="Build or refresh the search index.")
    index.add_argument("--source", default="all", help="all or comma list: codex,claude,vscode,cursor")
    index.add_argument("--reset", action="store_true", help="Drop and rebuild the index first.")
    index.add_argument("--quiet", action="store_true")
    index.set_defaults(func=cmd_index)

    search = sub.add_parser("search", help="Search indexed sessions.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--source", default="all", choices=["all", "codex", "claude", "vscode", "cursor"])
    search.add_argument("--mode", default="hybrid", choices=["fts", "local", "hybrid"])
    search.set_defaults(func=cmd_search)

    show = sub.add_parser("show", help="Show a full indexed document by id.")
    show.add_argument("doc_id")
    show.set_defaults(func=cmd_show)

    resume = sub.add_parser("resume", help="Print exact native resume instructions for a search result.")
    resume.add_argument("selector", help="Result rank from the last search, or a document id.")
    resume.set_defaults(func=cmd_resume)

    archived = sub.add_parser("archived", help="Show sessions marked archived.")
    archived.add_argument("--limit", type=int, default=10)
    archived.add_argument("--source", default="all", choices=["all", "codex", "claude", "vscode", "cursor"])
    archived.add_argument("--no-refresh", action="store_true")
    archived.set_defaults(func=cmd_archived, archived=True, mode="hybrid")

    archive_audit = sub.add_parser("archive-audit", help="Preview parser migration repairs without changing state.")
    archive_audit.set_defaults(func=cmd_archive_audit)

    archive = sub.add_parser("archive", help="Mark a numbered session archived.")
    archive.add_argument("selector")
    archive.set_defaults(func=cmd_set_archive, archived=True)

    unarchive = sub.add_parser("unarchive", help="Return a numbered session to the active dashboard.")
    unarchive.add_argument("selector")
    unarchive.set_defaults(func=cmd_set_archive, archived=False)

    handoff = sub.add_parser("handoff", help="Create a cross-harness context packet for a search result.")
    handoff.add_argument("selector", help="Result rank from the last search, or a document id.")
    handoff.add_argument("target", help="Target harness: codex or claude.")
    handoff.set_defaults(func=cmd_handoff)

    status = sub.add_parser("status", help="Show index counts.")
    status.set_defaults(func=cmd_status)

    capabilities = sub.add_parser("capabilities", help="Show supported behavior for each session source.")
    capabilities.set_defaults(func=cmd_capabilities)

    demo = sub.add_parser("demo", help="Run an isolated demonstration with synthetic sessions.")
    demo.set_defaults(func=cmd_demo)

    cards = sub.add_parser("cards", help="Build cached local session cards.")
    cards.add_argument("--limit", type=int, default=None)
    cards.set_defaults(func=cmd_cards)

    embed = sub.add_parser("embed", help="Build local semantic embeddings.")
    embed.add_argument("--limit", type=int, default=None)
    embed.set_defaults(func=cmd_embed)

    eval_cmd = sub.add_parser("eval", help="Run local ranking evals.")
    eval_cmd.add_argument("--file", default=str(DEFAULT_EVALS), help="JSON eval case file.")
    eval_cmd.add_argument("--limit", type=int, default=10)
    eval_cmd.add_argument("--mode", default="hybrid", choices=["fts", "local", "hybrid"])
    eval_cmd.add_argument("--refresh", action="store_true", help="Refresh index before running evals.")
    eval_cmd.add_argument("--verbose", action="store_true")
    eval_cmd.set_defaults(func=cmd_eval)

    return parser


def build_natural_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ss",
        description="Search AI sessions with natural English. Example: ss what was I doing with northstar clinic",
        epilog=(
            "Examples:\n"
            "  ss\n"
            "  ss northstar clinic deploy\n"
            "  ss fresh what did I work on today\n"
            "  ss open 1\n"
            "  ss look at 1\n"
            "  ss archive 1\n"
            "  ss archived\n"
            "  ss continue 1 in claude\n"
            "  ss continue 1 in codex"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=argparse.SUPPRESS)
    parser.add_argument("--home", default="~", help=argparse.SUPPRESS)
    parser.add_argument("-f", "--fresh", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-refresh", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-n", "--limit", type=int, default=10, help=argparse.SUPPRESS)
    parser.add_argument("--mode", default="hybrid", choices=["fts", "local", "hybrid"], help=argparse.SUPPRESS)
    parser.add_argument(
        "--source",
        default="all",
        choices=["all", "codex", "claude", "vscode", "cursor"],
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--claude", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--codex", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--copilot", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--vscode", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("query", nargs="*")
    return parser


def parse_natural(argv: list[str]) -> argparse.Namespace:
    parser = build_natural_parser()
    args = parser.parse_args(argv)
    selected = [name for name in ("claude", "codex", "copilot", "vscode") if getattr(args, name)]
    if selected:
        choice = selected[-1]
        args.source = "vscode" if choice in {"copilot", "vscode"} else choice
    args.func = cmd_natural
    return args


def first_numeric_selector(tokens: list[str]) -> str:
    for token in tokens:
        cleaned = token.strip().strip(".,:#")
        if cleaned.isdigit():
            return cleaned
    return ""


def parse_natural_followup(argv: list[str]) -> argparse.Namespace | None:
    if not argv:
        return None
    lower = [token.lower() for token in argv]
    first = lower[0]
    selector = first_numeric_selector(argv)
    if not selector:
        return None

    look_words = {"details", "inspect", "look", "read", "show"}
    open_words = {"open", "resume"}
    continue_words = {"bring", "continue", "move", "switch", "take", "use"}

    if first in {"archive", "unarchive"}:
        return argparse.Namespace(
            db=DEFAULT_DB,
            selector=selector,
            archived=first == "archive",
            func=cmd_set_archive,
        )

    if first in look_words:
        return argparse.Namespace(db=DEFAULT_DB, doc_id=selector, func=cmd_show)

    if first in open_words:
        return argparse.Namespace(db=DEFAULT_DB, selector=selector, target="", func=cmd_continue)

    if first in continue_words:
        target = infer_target_from_tokens(lower)
        return argparse.Namespace(db=DEFAULT_DB, selector=selector, target=target, func=cmd_continue)

    if first.isdigit():
        target = infer_target_from_tokens(lower[1:])
        return argparse.Namespace(db=DEFAULT_DB, selector=selector, target=target, func=cmd_continue)

    return None


def _main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return int(cmd_natural(parse_natural([])))
    invoked_as = pathlib.Path(sys.argv[0]).name
    natural_surface = invoked_as in {"ss", "sessions"} or os.environ.get("SS_NATURAL") == "1"
    if argv in (["-h"], ["--help"], ["help"]) and natural_surface:
        build_natural_parser().print_help()
        return 0
    known = {
        "index", "search", "show", "resume", "handoff", "status", "capabilities", "demo",
        "cards", "embed", "eval",
        "archived", "archive", "unarchive", "archive-audit",
    }
    if argv == ["refresh"]:
        argv = ["index", "--reset", *argv[1:]]
    natural_followup = parse_natural_followup(argv)
    if natural_followup is not None:
        return int(natural_followup.func(natural_followup))
    if argv and argv[0] not in known and argv[0] not in {"-h", "--help"}:
        args = parse_natural(argv)
        return int(args.func(args))
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except LockTimeout as exc:
        print(f"SS lock unavailable: {exc}. Retry after the other SS command finishes.", file=sys.stderr)
        return 4
    except UnsupportedPlatform as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except MigrationFailure as exc:
        print(f"SS migration blocked: {exc}. Restore the latest private database backup.", file=sys.stderr)
        return 5
    except (StorageFailure, sqlite3.DatabaseError, OSError) as exc:
        print(f"SS storage unavailable: {exc}. Run ss status or restore the private index backup.", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
