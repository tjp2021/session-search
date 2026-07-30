"""Session lifecycle: reopen, context packets, selectors, handoff, last-results, and archive transitions.

Split from session_search.py. Project symbols resolve through the
session_search facade at call time (``ss.<name>``) so tests that patch or
mutate facade attributes keep intercepting the behavior of this module.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import os
import pathlib
import re
import shlex
import sqlite3
import sys
import tempfile
from typing import Any
from archive_intent import PARSER_VERSION as ARCHIVE_INTENT_VERSION
from archive_intent import IntentKind, classify_session_intent, has_session_intent_candidate
from archive_store import immediate_transaction

import session_search as ss


def repo_label(repo: str) -> str:
    return repo or "unknown from index"


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def session_ref(source: str, session_id: str) -> str:
    return f"{source}: {session_id or 'unknown'}"


def native_resume_available(source: str, session_id: str) -> bool:
    return source in ss.NATIVE_REOPEN_SOURCES and bool(session_id and session_id != "unknown")


def native_resume_status(source: str, session_id: str) -> str:
    if ss.native_resume_available(source, session_id):
        return f"exact in {ss.source_label(source)}"
    return "unavailable"


def cross_tool_status(source: str) -> str:
    if source in ss.NATIVE_REOPEN_SOURCES:
        return "context packet only outside native owner"
    return "context packet only"


def packet_only_note(source: str) -> str:
    if source == "vscode":
        return "VS Code/Copilot exact chat reopen is not known yet; use a context packet to continue in Codex or Claude Code."
    if source == "cursor":
        return "Cursor exact chat reopen is not known yet; use a context packet to continue in Codex or Claude Code."
    return "Exact native reopen is not available from the indexed local data; use a context packet."


def native_resume_lines(
    source: str,
    session_id: str,
    repo: str,
    path: str = "",
) -> list[str]:
    if not ss.native_resume_available(source, session_id):
        return []
    lines: list[str] = []
    if repo:
        lines.append(f"cd {ss.shell_quote(repo)}")
    if source == "codex":
        lines.append(f"codex resume {ss.shell_quote(session_id)}")
    elif source == "claude":
        lines.append(f"claude --resume {ss.shell_quote(session_id)}")
    elif source == "pi":
        # Prefer the exact JSONL path when known; pi also accepts partial UUIDs.
        session_ref = path.strip() if path.strip() else session_id
        lines.append(f"pi --session {ss.shell_quote(session_ref)}")
    return lines


def handoff_targets_for(source: str) -> list[str]:
    targets = ["codex", "claude", "pi"]
    return [target for target in targets if target != source]


def target_label(target: str) -> str:
    return {"codex": "Codex", "claude": "Claude Code", "pi": "Pi"}.get(target, target)


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
        "pi": "pi",
    }
    if normalized not in aliases:
        raise ValueError(f"unsupported target: {raw}. Use codex, claude, or pi.")
    return aliases[normalized]


def infer_target_from_tokens(tokens: list[str]) -> str:
    for token in tokens:
        cleaned = re.sub(r"[^a-z_-]", "", token.lower())
        if not cleaned:
            continue
        try:
            return ss.normalize_target(cleaned)
        except ValueError:
            continue
    return ""


def load_last_results(db_path: pathlib.Path | None = None) -> dict[str, Any]:
    """Load the most relevant selector mapping for this terminal/agent context."""
    candidates = [ss.result_context_path(), ss.expand(ss.DEFAULT_LAST_RESULTS)]
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
        if db_path is not None and ss.sanitize_text(payload.get('db') or '') not in {'', str(db_path)}:
            continue
        if isinstance(payload, dict) and isinstance(payload.get('results'), list):
            return payload
    return {"results": []}


def save_last_results(results: list[tuple[sqlite3.Row, float, str]], query: str, db_path: pathlib.Path) -> None:
    context_kind, _context_value = ss.result_context_identity()
    payload = {
        "schema_version": 2,
        "query": query,
        "db": str(db_path),
        "saved_at": ss.now_ts(),
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
    context_path = ss.result_context_path()
    global_path = ss.expand(ss.DEFAULT_LAST_RESULTS)
    ss.atomic_write_json(context_path, payload)
    if context_path != global_path:
        ss.atomic_write_json(global_path, payload)


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
        value = ss.sanitize_text(os.environ.get(name, ""))
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
    kind, value = ss.result_context_identity()
    global_path = ss.expand(ss.DEFAULT_LAST_RESULTS)
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
    path = ss.result_context_path()
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
    payload = ss.last_results_payload()
    rank = int(selector)
    for item in payload.get("results", []):
        if isinstance(item, dict) and item.get("rank") == rank and item.get("doc_id"):
            context = dict(item)
            context["query"] = ss.sanitize_text(payload.get("query", ""))
            context["db"] = ss.sanitize_text(payload.get("db", ""))
            return context
    return None


def query_from_selector(selector: str) -> str:
    context = ss.last_result_context(selector)
    return ss.sanitize_text(context.get("query", "")) if context is not None else ""


def db_path_from_selector(selector: str, default_db: str) -> pathlib.Path:
    context = ss.last_result_context(selector)
    if context is not None:
        saved_db = ss.sanitize_text(context.get("db", ""))
        if saved_db and saved_db != ":memory:":
            return ss.expand(saved_db)
    return ss.expand(default_db)


def connect_selector_db(selector: str, default_db: str) -> tuple[pathlib.Path, sqlite3.Connection | None]:
    db_path = ss.db_path_from_selector(selector, default_db)
    if not db_path.is_file():
        return db_path, None
    conn: sqlite3.Connection | None = None
    try:
        conn = ss.connect_db(db_path)
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

    context = ss.last_result_context(selector)
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
        return ss.representative_session_row(conn, source, session_id)
    return None


def print_resume_instructions(conn: sqlite3.Connection, row: sqlite3.Row, selector: str, query: str = "") -> None:
    rows = ss.session_rows(conn, row)
    card = ss.session_card_for_result(conn, row, query)
    repo = card.repo or ss.best_repo(rows, row)
    source = row["source"]
    session_id = row["session_id"]
    title = card.title or ss.best_session_title(rows, row)
    print(f"Found in: {ss.source_label(source)}")
    print(f"Work folder: {ss.repo_label(repo)}")
    print(f"Session: {ss.session_ref(source, session_id)}")
    print(f"Status: {ss.session_status_text(ss.session_is_archived(conn, str(source), str(session_id)))}")
    print(f"Name: {title}")
    if query:
        print(f"Search query: {query}")
    print()
    lines = ss.native_resume_lines(source, session_id, repo, path=str(row["path"] or ""))
    if lines:
        print("Open exact session:")
        for line in lines:
            print(f"  {line}")
        return
    print("I cannot reopen this exact session from the local data.")
    print(ss.packet_only_note(source))
    for target in ss.handoff_targets_for(source):
        print(f"Continue in {ss.target_label(target)} with context: ss continue {selector} in {target}")


def mark_cli_resume(conn: sqlite3.Connection, row: sqlite3.Row, selector: str, origin: str) -> bool:
    source = str(row["source"])
    session_id = str(row["session_id"])
    try:
        with immediate_transaction(conn):
            if not ss.session_is_archived(conn, source, session_id):
                return False
            return ss.apply_archive_transition(
                conn,
                source,
                session_id,
                False,
                origin,
                evidence=f"explicit resume via selector {selector}",
                evidence_ts=ss.now_ts(),
                evidence_doc_id=str(row["doc_id"]),
                force=True,
            )
    except Exception:
        conn.rollback()
        raise


def handoff_rows(rows: list[sqlite3.Row], best_row: sqlite3.Row) -> list[sqlite3.Row]:
    by_id: dict[str, sqlite3.Row] = {best_row["doc_id"]: best_row}
    ordered = sorted(rows, key=lambda row: (row["ts"] or 0, row["doc_id"]))
    if len(ordered) <= ss.HANDOFF_MAX_ROWS:
        for row in ordered:
            by_id[row["doc_id"]] = row
    else:
        for row in ordered[:4]:
            by_id[row["doc_id"]] = row
        for row in ordered[-(ss.HANDOFF_MAX_ROWS - 4) :]:
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
        card = ss.build_session_card(packet_rows, row, query)
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
        f"- Owner: {ss.source_label(source)}",
        f"- Native session: {ss.session_ref(source, session_id)}",
        f"- Session name: {title}",
        f"- Status: {ss.session_status_text(archived)}",
        f"- Last active: {ss.iso_date(ss.best_session_ts(packet_rows, row))}",
        f"- Repo: {ss.repo_label(repo)}",
        f"- Source path: {row['path']}",
        f"- Search selector: {selector}",
        f"- Search query: {query or '(unknown)'}",
        f"- Target harness: {ss.target_label(target)}",
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
        lines.append(f"- {item.label} [{item.role}, {ss.iso_date(item.ts)}]: {item.text}")
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
            ss.snippet(row["text"], query or title, width=900),
            "",
            "## Indexed Session Context",
            "",
        ]
    )
    for item in packet_rows:
        text = ss.compact(item["text"], ss.HANDOFF_MAX_ROW_CHARS)
        lines.extend(
            [
                f"### {ss.iso_date(item['ts'])} | {item['role']} | {item['doc_id']}",
                "",
                text,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def handoff_path(row: sqlite3.Row, target: str) -> pathlib.Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    short_id = ss.stable_hash(f"{row['source']}:{row['session_id']}")[:10]
    filename = f"{stamp}_{row['source']}_{short_id}_to_{target}.md"
    return ss.expand(ss.DEFAULT_HANDOFF_DIR) / filename


def launch_lines_for_handoff(target: str, repo: str, packet_path: pathlib.Path) -> list[str]:
    packet_dir = str(packet_path.parent)
    prompt = f"Read the context packet at {packet_path} and continue the work."
    lines: list[str] = []
    if target == "codex":
        parts = ["codex"]
        if repo:
            parts.extend(["-C", ss.shell_quote(repo)])
        parts.extend(["--add-dir", ss.shell_quote(packet_dir), ss.shell_quote(prompt)])
        lines.append(" ".join(parts))
    elif target == "claude":
        if repo:
            lines.append(f"cd {ss.shell_quote(repo)}")
        lines.append(f"claude --add-dir {ss.shell_quote(packet_dir)} {ss.shell_quote(prompt)}")
    elif target == "pi":
        if repo:
            lines.append(f"cd {ss.shell_quote(repo)}")
        # Pi has no --add-dir equivalent; put the packet path in the startup prompt.
        lines.append(f"pi {ss.shell_quote(prompt)}")
    return lines


def session_archive_row(conn: sqlite3.Connection, source: str, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM session_archive_status WHERE source = ? AND session_id = ?",
        (source, session_id),
    ).fetchone()


def session_is_archived(conn: sqlite3.Connection, source: str, session_id: str) -> bool:
    row = ss.session_archive_row(conn, source, session_id)
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
    ss.apply_archive_transition(
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
    current = ss.session_archive_row(conn, source, session_id)
    event_ts = int(evidence_ts or status_at or ss.now_ts())
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
    event_id = ss.stable_hash(
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
            ss.compact(evidence, 320),
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
            ss.compact(evidence, 320),
            parser_version,
            ss.now_ts(),
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
        message = ss.full_user_message_text(evidence_row)
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
                    "evidence_ts": int(ss.row_field(evidence_row, "ts", 0) or 0),
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
        proposals, _unresolved = ss.archive_migration_audit(conn)
        for proposal in proposals:
            if ss.apply_archive_transition(
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
            if (message := ss.full_user_message_text(row))
        ]
        if not direct_rows:
            continue
        for message_row, message in direct_rows:
            if not has_session_intent_candidate(message):
                continue
            message_ts = int(ss.row_field(message_row, "ts", 0) or 0)
            decision = classify_session_intent(message)
            if decision.kind is IntentKind.NONE:
                continue
            archived = decision.kind is IntentKind.CLOSE
            if ss.apply_archive_transition(
                conn,
                source,
                session_id,
                archived,
                "detected-close" if archived else "detected-resume",
                evidence=message,
                evidence_ts=message_ts,
                evidence_doc_id=str(ss.row_field(message_row, "doc_id", "")),
                status_at=message_ts or ss.now_ts(),
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
