"""Session-store adapters: discovery and text extraction for codex, claude, pi, vscode, and cursor.

Split from session_search.py. Project symbols resolve through the
session_search facade at call time (``ss.<name>``) so tests that patch or
mutate facade attributes keep intercepting the behavior of this module.
"""

from __future__ import annotations

import dataclasses
import glob
import json
import os
import pathlib
import sqlite3
import urllib.parse
from typing import Any, Iterator

import session_search as ss


def _vscode_user_roots(home: pathlib.Path, source: str) -> list[pathlib.Path]:
    """Return macOS and XDG editor roots in deterministic order."""
    app = "Code" if source == "vscode" else "Cursor"
    roots = [home / "Library" / "Application Support" / app / "User"]
    xdg = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    roots.append(xdg / app / "User")
    if source == "vscode":
        roots.append(xdg / "Code - OSS" / "User")
    return list(dict.fromkeys(roots))


@dataclasses.dataclass(frozen=True)
class AdapterHealth:
    """Content-free health result for one local session-store adapter."""

    source: str
    status: str
    candidate_stores: int
    parsed_documents: int
    zero_content_stores: int
    error_stores: int


def _adapter_candidates(home: pathlib.Path, source: str) -> list[pathlib.Path]:
    """Return candidate store files without reading or exposing their content."""
    if source == "claude":
        return [
            *home.glob(".claude/projects/**/*.jsonl"),
            *home.glob(".claude/sessions/*.json"),
        ]
    if source == "codex":
        return [
            path
            for path in (
                home / ".codex" / "state_5.sqlite",
                home / ".codex" / "history.jsonl",
            )
            if path.is_file()
        ]
    if source == "pi":
        return list(home.glob(".pi/agent/sessions/**/*.jsonl"))
    if source in {"vscode", "cursor"}:
        return [
            path
            for root in _vscode_user_roots(home, source)
            for path in (
                *root.glob("globalStorage/emptyWindowChatSessions/*.jsonl"),
                *root.glob("globalStorage/state.vscdb"),
                *root.glob("workspaceStorage/*/state.vscdb"),
            )
        ]
    return []


def _candidate_documents(
    home: pathlib.Path,
    source: str,
    candidate: pathlib.Path,
) -> Iterator[ss.Document]:
    if source == "claude":
        if candidate.suffix == ".jsonl":
            yield from ss.iter_claude_jsonl(candidate, source)
        else:
            yield from ss.iter_json_session_file(candidate, source)
    elif source == "codex":
        if candidate.name == "state_5.sqlite":
            yield from ss.iter_codex_threads(home)
        else:
            yield from ss.iter_codex_history(home, ss.codex_thread_context(home))
    elif source == "pi":
        yield from ss.iter_pi_jsonl(candidate)
    elif candidate.suffix == ".jsonl":
        yield from ss.iter_vscode_chat_jsonl(candidate, source)
    else:
        yield from ss.iter_vscode_state_db(candidate, source)


def _candidate_has_format_errors(candidate: pathlib.Path) -> bool:
    """Detect malformed serialized records without returning their contents."""
    if candidate.suffix == ".jsonl":
        with candidate.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    return True
                if not isinstance(parsed, dict):
                    return True
        return False
    if candidate.suffix == ".json":
        json.loads(candidate.read_text(encoding="utf-8", errors="ignore"))
    return False


def adapter_health(
    home: pathlib.Path,
    sources: set[str] | None = None,
) -> list[AdapterHealth]:
    """Classify adapter stores without returning local paths or session text."""
    requested = sources or set(ss.SUPPORTED_SOURCES)
    results: list[AdapterHealth] = []
    for source in ss.SUPPORTED_SOURCES:
        if source not in requested:
            continue
        candidates = ss._adapter_candidates(home, source)
        parsed = 0
        zero_content = 0
        errors = 0
        for candidate in candidates:
            try:
                count = sum(
                    1
                    for _document in _candidate_documents(
                        home,
                        source,
                        candidate,
                    )
                )
                if _candidate_has_format_errors(candidate):
                    errors += 1
            # Diagnostics must degrade without returning parser exceptions,
            # because those messages can contain private archive paths.
            except Exception:
                errors += 1
                count = 0
            parsed += count
            if count == 0:
                zero_content += 1
        if not candidates:
            status = "missing_store"
        elif parsed and not zero_content and not errors:
            status = "parsed_documents"
        elif parsed:
            status = "partial_store_drift"
        else:
            status = "candidate_store_zero_content"
        results.append(
            AdapterHealth(
                source=source,
                status=status,
                candidate_stores=len(candidates),
                parsed_documents=parsed,
                zero_content_stores=zero_content,
                error_stores=errors,
            )
        )
    return results


def iter_codex(home: pathlib.Path) -> Iterator[Document]:
    thread_context = ss.codex_thread_context(home)
    yield from ss.iter_codex_threads(home)
    yield from ss.iter_codex_history(home, thread_context)


def codex_thread_context(home: pathlib.Path) -> dict[str, dict[str, Any]]:
    db = home / ".codex" / "state_5.sqlite"
    conn = ss.connect_ro_sqlite(db)
    if conn is None:
        return {}
    try:
        columns = ss.table_columns(conn, "threads")
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
    conn = ss.connect_ro_sqlite(db)
    if conn is None:
        return
    try:
        columns = ss.table_columns(conn, "threads")
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
            title = ss.sanitize_text(row["title"] or "")
            text = ss.unique_join(
                [
                    title,
                    row["first_user_message"] if "first_user_message" in row.keys() else "",
                    row["preview"] if "preview" in row.keys() else "",
                ]
            )
            if not text:
                continue
            meta = {k: row[k] for k in row.keys() if k not in {"first_user_message", "preview"}}
            yield ss.Document(
                doc_id=f"codex-thread:{session_id}",
                source="codex",
                session_id=session_id,
                title=ss.derive_document_title(title, text),
                path=str(db),
                cwd=ss.sanitize_text(row["cwd"] or ""),
                role="session",
                ts=ss.parse_ts(row["updated_at"]),
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
            text = ss.sanitize_text(item.get("text", ""))
            if not ss.useful_text(text):
                continue
            session_id = str(item.get("session_id") or "unknown")
            context = thread_context.get(session_id, {})
            ts = ss.parse_ts(item.get("ts"))
            meta = {"line": line_no}
            if context:
                meta["thread"] = {
                    key: context[key]
                    for key in ("title", "updated_at", "created_at", "git_branch", "git_origin_url")
                    if key in context
                }
            yield ss.Document(
                doc_id=f"codex-history:{session_id}:{ts or line_no}:{ss.stable_hash(text)}",
                source="codex",
                session_id=session_id,
                title=ss.derive_document_title("", text),
                path=str(path),
                cwd=ss.sanitize_text(context.get("cwd", "")),
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
            yield from ss.iter_claude_jsonl(pathlib.Path(file_path), source="claude")

    sessions_dir = home / ".claude" / "sessions"
    if sessions_dir.exists():
        for file_path in sessions_dir.glob("*.json"):
            yield from ss.iter_json_session_file(file_path, source="claude")


def iter_claude_jsonl(path: pathlib.Path, source: str) -> Iterator[Document]:
    docs: list[ss.Document] = []
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
                cwd = ss.sanitize_text(obj["cwd"])
            if obj.get("type") == "ai-title" and obj.get("aiTitle"):
                title = ss.sanitize_text(obj["aiTitle"])
                continue

            role, text = ss.extract_claude_turn(obj)
            if not text:
                continue
            ts = ss.parse_ts(obj.get("timestamp"))
            uuid = obj.get("uuid") or obj.get("requestId") or line_no
            docs.append(
                ss.Document(
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
        yield dataclasses.replace(doc, title=ss.document_title_with_fallback(doc.title, doc.text, fallback_title))


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
        text = ss.extract_content_text(message.get("content"))
        return role, text
    if isinstance(message, str):
        return role, ss.sanitize_text(message)
    return "", ""


def iter_pi(home: pathlib.Path) -> Iterator[Document]:
    sessions_dir = home / ".pi" / "agent" / "sessions"
    if not sessions_dir.exists():
        return
    pattern = str(sessions_dir / "*" / "*.jsonl")
    for file_path in glob.iglob(pattern, recursive=False):
        yield from ss.iter_pi_jsonl(pathlib.Path(file_path))


def iter_pi_jsonl(path: pathlib.Path) -> Iterator[Document]:
    """Index the active branch of a Pi session tree.

    Pi sessions are JSONL trees (id/parentId). SS only indexes the path from the
    current leaf back to the root so abandoned forks are not double-counted.
    """
    header: dict[str, Any] | None = None
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            obj_type = str(obj.get("type") or "")
            if obj_type == "session":
                header = obj
                continue
            if "id" not in obj:
                continue
            obj["_line_no"] = line_no
            entries.append(obj)

    if not entries and header is None:
        return

    session_id = ""
    if header and header.get("id"):
        session_id = str(header["id"])
    if not session_id:
        # Fallback: filename stem is "<timestamp>_<uuid>".
        stem = path.stem
        session_id = stem.split("_", 1)[-1] if "_" in stem else stem

    cwd = ss.sanitize_text((header or {}).get("cwd") or "")
    title = ss.pi_session_name(entries) or session_id
    active_entries = ss.pi_active_branch_entries(entries)

    docs: list[ss.Document] = []
    for entry in active_entries:
        role, text = ss.extract_pi_entry_text(entry)
        if not text:
            continue
        entry_id = str(entry.get("id") or entry.get("_line_no") or len(docs))
        ts = ss.parse_ts(entry.get("timestamp"))
        if ts is None:
            message = entry.get("message")
            if isinstance(message, dict):
                ts = ss.parse_ts(message.get("timestamp"))
        docs.append(
            ss.Document(
                doc_id=f"pi:{session_id}:{entry_id}",
                source="pi",
                session_id=session_id,
                title=title,
                path=str(path),
                cwd=cwd,
                role=role,
                ts=ts,
                text=text,
                meta={
                    "line": entry.get("_line_no"),
                    "entry_type": entry.get("type", ""),
                    "parent_id": entry.get("parentId"),
                    "leaf_id": active_entries[-1].get("id") if active_entries else "",
                },
            )
        )

    for doc in docs:
        yield dataclasses.replace(doc, title=ss.document_title_with_fallback(doc.title, doc.text, title))


def pi_session_name(entries: list[dict[str, Any]]) -> str:
    name = ""
    for entry in entries:
        if entry.get("type") != "session_info":
            continue
        candidate = ss.sanitize_text(entry.get("name") or "")
        if candidate:
            name = candidate
    return name


def pi_active_branch_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return root→leaf entries for the current Pi leaf.

    The leaf is any entry whose id is never referenced as a parentId. If the
    tree has multiple tips (concurrent forks that were not pruned), prefer the
    newest timestamp so abandoned older forks stay out of the active branch.
    """
    by_id: dict[str, dict[str, Any]] = {}
    referenced_as_parent: set[str] = set()
    for entry in entries:
        entry_id = str(entry.get("id") or "")
        if not entry_id:
            continue
        by_id[entry_id] = entry
        parent_id = entry.get("parentId")
        if parent_id is not None and parent_id != "":
            referenced_as_parent.add(str(parent_id))

    if not by_id:
        return []

    tips = [entry for entry_id, entry in by_id.items() if entry_id not in referenced_as_parent]
    if not tips:
        # Cycles or missing links: fall back to newest entry by timestamp/order.
        tips = list(by_id.values())

    def tip_sort_key(entry: dict[str, Any]) -> tuple[int, int]:
        ts = ss.parse_ts(entry.get("timestamp")) or 0
        line_no = int(entry.get("_line_no") or 0)
        return ts, line_no

    leaf = max(tips, key=tip_sort_key)
    path: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: dict[str, Any] | None = leaf
    while current is not None:
        entry_id = str(current.get("id") or "")
        if not entry_id or entry_id in seen:
            break
        seen.add(entry_id)
        path.append(current)
        parent_id = current.get("parentId")
        if parent_id is None or parent_id == "":
            break
        current = by_id.get(str(parent_id))
    path.reverse()
    return path


def extract_pi_entry_text(entry: dict[str, Any]) -> tuple[str, str]:
    entry_type = str(entry.get("type") or "")
    if entry_type == "message":
        message = entry.get("message")
        if not isinstance(message, dict):
            return "", ""
        role = str(message.get("role") or "").strip()
        if role in {"toolResult", "tool_result", "bashExecution"}:
            return "", ""
        if role == "user":
            return "user", ss.extract_pi_content_text(message.get("content"))
        if role == "assistant":
            return "assistant", ss.extract_pi_content_text(message.get("content"))
        if role in {"branchSummary", "compactionSummary", "custom"}:
            return "assistant", ss.extract_pi_content_text(message.get("content") or message.get("summary"))
        return "", ""
    if entry_type == "compaction":
        return "assistant", ss.sanitize_text(entry.get("summary") or "")
    if entry_type == "branch_summary":
        return "assistant", ss.sanitize_text(entry.get("summary") or "")
    if entry_type == "custom_message":
        return "user", ss.extract_pi_content_text(entry.get("content"))
    return "", ""


def extract_pi_content_text(content: Any) -> str:
    if isinstance(content, str):
        return ss.sanitize_text(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").lower()
            if item_type in {"image", "toolcall", "tool_use", "tool_result", "thinking"}:
                continue
            if "text" in item:
                parts.append(str(item["text"]))
            elif "thinking" in item and item_type == "":
                parts.append(str(item["thinking"]))
        return ss.sanitize_text("\n\n".join(parts))
    return ""


def extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return ss.sanitize_text(content)
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
        return ss.sanitize_text("\n\n".join(parts))
    return ""


def iter_vscode(home: pathlib.Path) -> Iterator[Document]:
    for root in _vscode_user_roots(home, "vscode"):
        yield from ss.iter_vscode_user_root(root, source="vscode")


def iter_cursor(home: pathlib.Path) -> Iterator[Document]:
    for root in _vscode_user_roots(home, "cursor"):
        yield from ss.iter_vscode_user_root(root, source="cursor")


def iter_vscode_user_root(root: pathlib.Path, source: str) -> Iterator[Document]:
    empty_sessions = root / "globalStorage" / "emptyWindowChatSessions"
    if empty_sessions.exists():
        for file_path in empty_sessions.glob("*.jsonl"):
            yield from ss.iter_vscode_chat_jsonl(file_path, source=source)

    dbs: list[pathlib.Path] = []
    global_db = root / "globalStorage" / "state.vscdb"
    if global_db.exists():
        dbs.append(global_db)
    workspace_storage = root / "workspaceStorage"
    if workspace_storage.exists():
        dbs.extend(workspace_storage.glob("*/state.vscdb"))

    for db in dbs:
        yield from ss.iter_vscode_state_db(db, source=source)


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
            if ss.is_patch_key(obj, "customTitle") and isinstance(v, str):
                title = ss.sanitize_text(v)

            for doc in ss.extract_vscode_docs(obj, source, path, session_id, title, line_no):
                if doc.doc_id in seen:
                    continue
                seen.add(doc.doc_id)
                yield doc


def iter_vscode_state_db(path: pathlib.Path, source: str) -> Iterator[Document]:
    conn = ss.connect_ro_sqlite(path)
    if conn is None:
        return
    cwd = vscode_workspace_cwd(path)
    try:
        try:
            if "ItemTable" not in ss.table_names(conn):
                return
            rows = conn.execute("SELECT key, value FROM ItemTable")
        except sqlite3.Error:
            return
        for row in rows:
            try:
                key = str(row["key"])
                if key.startswith("secret://"):
                    continue
                if not any(hint.lower() in key.lower() for hint in ss.CHAT_KEY_HINTS):
                    continue
                value = ss.decode_sqlite_value(row["value"])
                if not value:
                    continue
                parsed = ss.parse_json_maybe(value)
                if parsed is None:
                    continue
                session_id = ss.stable_hash(str(path) + key)
                title = key
                for i, text in enumerate(ss.extract_generic_chat_text(parsed)):
                    text = ss.sanitize_text(text)
                    if not ss.useful_text(text):
                        continue
                    yield ss.Document(
                        doc_id=f"{source}-state:{session_id}:{i}:{ss.stable_hash(text)}",
                        source=source,
                        session_id=session_id,
                        title=ss.derive_document_title(title, text),
                        path=str(path),
                        cwd=cwd,
                        role="state",
                        ts=None,
                        text=text,
                        meta={"state_key": key},
                    )
            except sqlite3.Error:
                continue
    finally:
        conn.close()


def vscode_workspace_cwd(path: pathlib.Path) -> str:
    """Resolve a VS Code workspace-store path without exposing storage hashes."""
    metadata = path.parent / "workspace.json"
    if not metadata.is_file():
        return ""
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    raw = payload.get("folder") or payload.get("workspace") or ""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme == "file":
        resolved = pathlib.Path(urllib.parse.unquote(parsed.path))
    elif parsed.scheme:
        return ""
    else:
        resolved = pathlib.Path(raw)
    if resolved.suffix == ".code-workspace":
        resolved = resolved.parent
    return ss.sanitize_text(str(resolved))


def iter_json_session_file(path: pathlib.Path, source: str) -> Iterator[Document]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return
    session_id = path.stem
    for i, text in enumerate(ss.extract_generic_chat_text(parsed)):
        text = ss.sanitize_text(text)
        if not ss.useful_text(text):
            continue
        yield ss.Document(
            doc_id=f"{source}-json:{session_id}:{i}:{ss.stable_hash(text)}",
            source=source,
            session_id=session_id,
            title=ss.derive_document_title(session_id, text),
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
        for request in ss.extract_request_objects(v):
            yield from ss.vscode_request_docs(request, source, path, session_id, title, line_no)
    elif isinstance(v, list):
        for request in v:
            if isinstance(request, dict):
                yield from ss.vscode_request_docs(request, source, path, session_id, title, line_no)


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
        or ss.stable_hash(json.dumps(request, sort_keys=True, default=str))
    )
    ts = ss.parse_ts(request.get("timestamp") or request.get("timeSpentWaiting"))
    if request.get("sessionId"):
        session_id = str(request["sessionId"])

    user_text = ss.extract_vscode_user_message(request)
    assistant_text = ss.extract_vscode_assistant_response(request)
    text = ss.unique_join(
        [
            f"USER:\n{user_text}" if user_text else "",
            f"ASSISTANT:\n{assistant_text}" if assistant_text else "",
        ]
    )
    if not ss.useful_text(text):
        return
    yield ss.Document(
        doc_id=f"{source}-chat:{session_id}:{req_id}",
        source=source,
        session_id=session_id,
        title=title or ss.sanitize_text(user_text[:120]),
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
        if ss.looks_like_vscode_request(obj):
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
        return ss.unique_join(parts, sep="\n")
    if isinstance(message, str):
        return ss.sanitize_text(message)
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
    return ss.unique_join(parts)


def extract_generic_chat_text(obj: Any) -> list[str]:
    out: list[str] = []

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            lowered = {str(k).lower() for k in value.keys()}
            if lowered & ss.SKIP_KEYS:
                return
            if ss.looks_like_vscode_request(value):
                user_text = ss.extract_vscode_user_message(value)
                assistant_text = ss.extract_vscode_assistant_response(value)
                text = ss.unique_join([user_text, assistant_text])
                if text:
                    out.append(text)
            for k, v in value.items():
                key = str(k)
                if ss.should_skip_key(key):
                    continue
                if key in ss.TEXT_KEYS and isinstance(v, str) and ss.is_chat_path(path):
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
        text = ss.sanitize_text(text)
        if not ss.useful_text(text):
            continue
        key = ss.stable_hash(text)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned


def should_skip_key(key: str) -> bool:
    lower = key.lower()
    return any(skip in lower for skip in ss.SKIP_KEYS)


def is_chat_path(path: tuple[str, ...]) -> bool:
    if not path:
        return False
    joined = ".".join(path).lower()
    return any(hint.lower() in joined for hint in ss.CHAT_KEY_HINTS + ("request", "message", "response"))


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
        yield from ss.iter_codex(home)
    if "claude" in sources:
        yield from ss.iter_claude(home)
    if "pi" in sources:
        yield from ss.iter_pi(home)
    if "vscode" in sources:
        yield from ss.iter_vscode(home)
    if "cursor" in sources:
        yield from ss.iter_cursor(home)
