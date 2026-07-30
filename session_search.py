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
import shutil
import shlex
import sqlite3
import sys
import tempfile
from collections import Counter
from typing import Any, Iterable, Iterator

from archive_intent import PARSER_VERSION as ARCHIVE_INTENT_VERSION
from archive_intent import IntentKind, classify_session_intent, has_session_intent_candidate
from archive_store import StorageFailure, backup_database, immediate_transaction, quick_check
from adapter_capabilities import render_capabilities
from card_quality import TRUNCATED_RE, quality_gate
from secret_patterns import redact as redact_secrets
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

# When run as ``python -m session_search`` this module executes as __main__.
# The concern modules below import it by name; registering the running
# instance first keeps facade state (locks, LLM warnings, patched seams) on
# one module object instead of a second shadow copy.
if __name__ == "__main__":
    sys.modules.setdefault("session_search", sys.modules[__name__])


_DEFAULT_PATHS = resolve_paths()
DEFAULT_DB = str(_DEFAULT_PATHS.db)
DEFAULT_LAST_RESULTS = str(_DEFAULT_PATHS.last_results)
DEFAULT_LOCK = str(_DEFAULT_PATHS.lock)
DEFAULT_HANDOFF_DIR = str(_DEFAULT_PATHS.handoff_dir)
DEFAULT_MODEL_CACHE = str(_DEFAULT_PATHS.model_cache)
DEFAULT_EVALS = pathlib.Path(__file__).resolve().with_name("evals") / "session-search-evals.json"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_MODEL_VERSION = "fastembed:" + EMBED_MODEL
CARD_VERSION = "openrouter-card-v18"
UNTITLED_SESSION = "(untitled session)"
# Where a card's About text came from. An `evidence` card is a regex fallback
# built when the model was unavailable, and it is rebuilt once a key works.
SUMMARY_SOURCE_LLM = "llm"
SUMMARY_SOURCE_EVIDENCE = "evidence"
# Every adapter SS can index, and the subset whose native app can reopen a
# session exactly. The CLI choices and the capability matrix both derive from
# these, so a new adapter cannot be half-registered.
SUPPORTED_SOURCES = ("codex", "claude", "pi", "vscode", "cursor")
NATIVE_REOPEN_SOURCES = ("codex", "claude", "pi")
SOURCE_CHOICES = ("all", *SUPPORTED_SOURCES)
# How far back routine backfill will summarize. The dashboard asks for
# max(limit, 50) sessions, so this stays above that. Older sessions are
# summarized on demand when a search surfaces them, and then cached, so their
# text is sent once instead of never being read.
CARD_BACKFILL_SESSIONS = 60
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
    summary_source: str = "evidence"


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


_LOCK_DEPTH = 0


@contextlib.contextmanager
def session_lock(shared: bool = False) -> Iterator[None]:
    """Process-wide advisory lock, reentrant within this process.

    Reentrancy lets a long build take the lock per write while still working
    when an outer command already holds it. Without it the inner acquire waits
    on a lock this same process owns and fails after the timeout.
    """
    global _LOCK_DEPTH
    if _LOCK_DEPTH > 0:
        _LOCK_DEPTH += 1
        try:
            yield
        finally:
            _LOCK_DEPTH -= 1
        return
    with file_lock(expand(DEFAULT_LOCK), shared=shared):
        _LOCK_DEPTH += 1
        try:
            yield
        finally:
            _LOCK_DEPTH -= 1


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

        DROP TABLE IF EXISTS session_cards;

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
            summary_source TEXT NOT NULL DEFAULT 'evidence',
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
        DROP TABLE IF EXISTS documents_fts_data;
        DROP TABLE IF EXISTS documents_fts_idx;
        DROP TABLE IF EXISTS documents_fts_content;
        DROP TABLE IF EXISTS documents_fts_docsize;
        DROP TABLE IF EXISTS documents_fts_config;
        PRAGMA user_version = 0;
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


def normalize_sources(raw: str) -> set[str]:
    if raw == "all":
        return set(SUPPORTED_SOURCES)
    return {part.strip() for part in raw.split(",") if part.strip()}


PROMPT_STARTER_RE = re.compile(r"^(?:i\s+need\s+(?:you\s+)?(?:to|can|help)|can\s+you\s+(?:please)?|please\s+|help me|analyze the current|resume the session we|I need to resume|find the fucking|this is what we were working on|Stop\.?|okay\.?)", re.I)


_LLM_WARNED = False


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


ABBREVIATION_END_RE = re.compile(
    r"(?:\b(?:[A-Za-z]|e\.g|i\.e|etc|vs|approx|dr|mr|mrs|ms|st|jr|sr|fig|no|al)\.)$",
    re.I,
)


ABOUT_INSTRUCTION = (
    "Summarize what this session was about in exactly 1 sentence. "
    "Name the concrete task or topic and the meaningful outcome. "
    "Ignore shell prompts, handoff boilerplate, and meta-discussion about summaries: "
)
NEXT_INSTRUCTION = "What is the next action or resume point from this session? Answer in 1 sentence: "


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


# --- facade re-exports ---------------------------------------------
# Concern modules call back into this facade (ss.<name>); these imports
# bind their public names here so patching ss.<name> stays effective.
#
# The suite loads this file as a fresh module object (importlib +
# sys.modules["session_search"] = instance) and patches THAT instance.
# Evicting the concern modules first forces them to re-execute and bind
# their ``import session_search as ss`` to the instance being loaded, so
# every facade instance owns a consistent, patchable module family.
for _bucket in ("ss_adapters", "ss_retrieval", "ss_cards", "ss_dashboard", "ss_packets", "ss_cli"):
    sys.modules.pop(_bucket, None)
del _bucket

from ss_adapters import (  # noqa: E402
    build_docs, codex_thread_context, decode_sqlite_value, extract_claude_turn,
    extract_content_text, extract_generic_chat_text, extract_pi_content_text,
    extract_pi_entry_text, extract_request_objects, extract_vscode_assistant_response,
    extract_vscode_docs, extract_vscode_user_message, is_chat_path, is_patch_key, iter_claude,
    iter_claude_jsonl, iter_codex, iter_codex_history, iter_codex_threads, iter_cursor,
    iter_json_session_file, iter_pi, iter_pi_jsonl, iter_vscode, iter_vscode_chat_jsonl,
    iter_vscode_state_db, iter_vscode_user_root, looks_like_vscode_request, parse_json_maybe,
    pi_active_branch_entries, pi_session_name, should_skip_key, table_columns, table_names,
    vscode_request_docs,
)

from ss_retrieval import (  # noqa: E402
    anchor_weight, cosine, dot_blob, embedding_backend, embedding_query_vector,
    ensure_embeddings, ensure_session_embeddings, exact_literal_query, expand_query_tokens,
    fts_query, full_session_text, group_results_by_session, infer_source_from_query,
    infer_time_and_intent, is_corpus_dump_card, is_corpus_dump_session, is_meta_query,
    is_meta_session, is_side_task_query, is_side_task_session, matched_anchor_terms,
    merge_results, normalize_embedding, normalize_token, proper_query_terms,
    query_anchor_terms, query_term_weight, quote_fts, recency_boost,
    representative_session_row, rerank_session_results, row_context_quality, rows_for_session,
    search_fts, search_local, search_recent, search_semantic, semantic_score, semantic_text,
    session_embedding_text, session_groups, session_text_sample, similarity_vector,
    term_present, term_variants, tokenize, vector_from_blob,
)

from ss_cards import (  # noqa: E402
    _load_openrouter_api_key, base_topic_score, best_line_for_row, best_repo,
    best_session_title, best_session_ts, build_session_card, cached_base_session_card,
    cached_card_is_current, candidate_title, card_about_line, card_from_cache_row,
    choose_base_topic_row, choose_pattern_row, choose_topic_row, clean_card_line, compact,
    derive_document_title, document_title_with_fallback, ensure_session_cards,
    evidence_from_json, evidence_line, evidence_to_json, extract_task_clause,
    first_card_sentence, full_user_message_text, is_close_session_message, is_fragment_like,
    is_machine_title, is_question_like, is_user_prompt_row, is_weak_title, last_user_message,
    last_user_prompt_ts, line_quality_score, llm_available, llm_summarize, meaningful_lines,
    mentioned_paths, merge_query_card, noisy_path, normalize_card_typos,
    paths_from_evidence_rows, query_line_score, recent_user_messages, row_field,
    row_match_score, row_meta, row_sort_key, session_card_for_result, session_card_hash,
    session_rows, source_label, store_session_card, strip_card_filler, strip_prompt_echo,
    summaries_enabled, synthesize_resume_from_text, synthesize_title_from_text,
    timestamped_session_title, unique_evidence, user_message_text, warn_llm_unavailable,
)

from ss_dashboard import (  # noqa: E402
    _card_field_ready, _dedupe_card_path, _humanize_path_part, _short_topic,
    _strip_card_prefixes, alternate_harness_line, archived_session_results, card_visible_text,
    catch_up_dashboard_cards, dashboard_cell, dashboard_clean_text, dashboard_is_interactive,
    dashboard_key_terms, dashboard_pick_about, dashboard_pick_resume, dashboard_pick_state,
    dashboard_project_is_noise, dashboard_project_label, dashboard_project_summaries,
    dashboard_prompt, dashboard_relative_time, dashboard_sentences, dashboard_short_tool,
    dashboard_summary, dashboard_terminal_width, dashboard_topic, evidence_terms,
    evidence_terms_from_text, hit_count, iter_dashboard_updates, location_label,
    owner_action_label, print_dashboard, print_dashboard_table, print_results,
    print_session_card_detail, recent_session_results, refresh_dashboard_index,
    related_result_lines, result_card, snippet, why_line,
)

from ss_packets import (  # noqa: E402
    apply_archive_transition, archive_migration_audit, atomic_write_json, connect_selector_db,
    cross_tool_status, db_path_from_selector, handoff_packet_text, handoff_path, handoff_rows,
    handoff_targets_for, infer_target_from_tokens, last_result_context, last_results_payload,
    launch_lines_for_handoff, load_last_results, mark_cli_resume, native_resume_available,
    native_resume_lines, native_resume_status, normalize_target, packet_only_note,
    print_resume_instructions, print_selector_db_error, query_from_selector, repo_label,
    result_context_identity, result_context_path, save_last_results, selected_row,
    session_archive_row, session_is_archived, session_ref, session_status_marker,
    session_status_text, set_session_archive_status, shell_quote, sync_detected_archive_states,
    target_label,
)

from ss_cli import (  # noqa: E402
    build_natural_parser, build_parser, cmd_archive_audit, cmd_archived, cmd_capabilities,
    cmd_cards, cmd_continue, cmd_dashboard, cmd_demo, cmd_embed, cmd_eval, cmd_handoff,
    cmd_index, cmd_natural, cmd_resume, cmd_search, cmd_set_archive, cmd_show, cmd_status,
    eval_rule_matches, eval_text_for_result, eval_title, first_numeric_selector,
    load_eval_cases, normalize_eval_terms, parse_natural, parse_natural_followup,
    result_rank_for_rules, run_search,
)

# Back-compat alias: older call sites referenced ollama_summarize().
ollama_summarize = llm_summarize

if __name__ == "__main__":
    raise SystemExit(main())
