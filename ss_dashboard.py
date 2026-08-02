"""Dashboard: work map, field pickers, labels, tables, relative time, and results rendering.

Split from session_search.py. Project symbols resolve through the
session_search facade at call time (``ss.<name>``) so tests that patch or
mutate facade attributes keep intercepting the behavior of this module.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import pathlib
import re
import shutil
import sqlite3
import sys
import unicodedata
from typing import Any, Iterator
from wcwidth import wcwidth, wcswidth

from card_quality import quality_gate

import session_search as ss


def dashboard_sentences(text: str) -> list[str]:
    clean = ss.sanitize_text(text)
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", clean) if part.strip()]


def dashboard_topic(card: SessionCard) -> str:
    title = re.sub(r"^\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+[—-]\s+", "", card.title)
    first = ss.dashboard_sentences(title)[0] if ss.dashboard_sentences(title) else title
    prefix = first.split(":", 1)[0].strip()
    if ":" in first and 2 <= len(prefix.split()) <= 8:
        return ss.compact(prefix, 120)
    return ss.compact(first, 160)


def dashboard_key_terms(text: str, limit: int = 4) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)+|[A-Za-z][A-Za-z0-9]{3,}", text):
        term = raw.strip("-/")
        key = term.lower()
        if key in ss.DASHBOARD_CLUE_STOPWORDS or key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def _card_field_ready(value: str) -> str:
    return re.sub(r"(?:\.{3}|…)+\s*$", "", value or "").strip()


def _dedupe_card_path(path: str) -> str:
    path = ss.sanitize_text(path)
    if not path:
        return ""
    if len(path) % 2 == 0:
        half = len(path) // 2
        if path[:half] == path[half:]:
            return path[:half]
    for token in ("/tmp/", "/Users/", "/home/"):
        idx = path.find(token, 1)
        if idx <= 0:
            continue
        left = path[:idx].rstrip("/")
        right = path[idx:]
        if left.split("/")[-1] and left.split("/")[-1] == right.rstrip("/").split("/")[-1]:
            return right
    return path


def _short_topic(text: str) -> str:
    clean = re.sub(r"\s+", " ", ss.sanitize_text(text)).strip()
    if not clean:
        return ""
    if ":" in clean:
        head = clean.split(":", 1)[0].strip()
        if 1 <= len(head.split()) <= 8:
            return head
    parts = re.split(r"(?<=[.!?])\s+", clean)
    return parts[0].strip() if parts else clean


def _strip_card_prefixes(text: str) -> str:
    return re.sub(
        r"^\s*(?:what\s+happened|state|resume|about|next(?:\s+clue)?)\s*:\s*",
        "",
        text or "",
        flags=re.I,
    ).strip()


def dashboard_terminal_width() -> int:
    try:
        width = int(shutil.get_terminal_size(fallback=(100, 24)).columns)
    except (TypeError, ValueError, OSError):
        width = 100
    return max(32, min(width, 200))


def dashboard_clean_text(value: str) -> str:
    """Strip control junk that makes terminal paste unreadable.

    Do not use sanitize_text() here: it line-strips and would destroy the
    intentional left indentation of dashboard rows.
    """
    if value is None:
        clean = ""
    elif isinstance(value, str):
        clean = value
    else:
        clean = str(value)
    clean = re.sub(r"\[[0-9;?]*[A-Za-z]", " ", clean)
    clean = re.sub(r"\[[0-9;]{1,12}[A-Za-z]", " ", clean)
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", clean)
    leading = len(clean) - len(clean.lstrip(" "))
    core = re.sub(r"\s+", " ", clean.strip())
    if not core:
        return ""
    return (" " * min(leading, 8)) + core


def _humanize_path_part(part: str) -> str:
    clean = ss.sanitize_text(part).strip().strip("_").replace("-", " ").replace("_", " ")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean.title() if clean else "Unknown"


def dashboard_project_is_noise(project: str, repo: str = "") -> bool:
    """Drop path debris that should not dominate the restart map."""
    name = ss.dashboard_clean_text(project).lower()
    path = ss.dashboard_clean_text(repo).lower()
    if not name or name in {"unknown folder", "untitled session"}:
        return True
    noisy = (
        "history.jsonl",
        "codex history",
        ".codex",
        "node_modules",
        "untitled",
    )
    if any(token in name for token in noisy):
        return True
    if any(token in path for token in ("history.jsonl", "/.codex/", "/node_modules/")):
        return True
    return False


def dashboard_short_tool(source_label_text: str) -> str:
    mapping = {
        "Claude Code": "Claude",
        "Codex": "Codex",
        "VS Code / Copilot": "Copilot",
        "Cursor": "Cursor",
        "Pi": "Pi",
    }
    return mapping.get(source_label_text, source_label_text)


def dashboard_relative_time(ts: int | None) -> str:
    if not ts:
        return "unknown"
    now = ss.now_ts()
    delta = max(0, int(now) - int(ts))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    days = delta // 86400
    if days < 14:
        return f"{days}d ago"
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


def dashboard_pick_about(messages: list[str], card: SessionCard) -> str:
    # A model-written summary outranks every heuristic; an evidence card
    # falls through to the title and message heuristics below.
    if card.summary_source == ss.SUMMARY_SOURCE_LLM and card.what_this_was:
        line = ss.clean_card_line(card.what_this_was, 160)
        if line:
            return line
    bare_title = re.sub(
        r"^\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+[—-]\s+",
        "",
        card.title or "",
    )
    for candidate in (ss.dashboard_topic(card), ss._short_topic(bare_title), bare_title):
        line = ss.clean_card_line(candidate or "", 120)
        if line and line.lower().strip("().") != "untitled session":
            return line
    ranked: list[tuple[float, str]] = []
    for message in messages:
        line = ss.clean_card_line(ss._short_topic(message), 140)
        if not line or len(line) > 140:
            continue
        ranked.append((ss.line_quality_score(line) + min(len(line), 80) / 80.0, line))
    if ranked:
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1]
    project = ss.dashboard_project_label(card.repo or "")
    tool = ss.source_label(card.source)
    if project and project not in {"Unknown folder", "OS root"}:
        return f"{tool} work in {project}."
    return f"{tool} session."


def dashboard_pick_state(rows: list[sqlite3.Row], about: str, card: SessionCard) -> str:
    blocked = re.sub(r"\W+", "", about or "").lower()
    for row in sorted(rows or [], key=ss.row_sort_key):
        if str(ss.row_field(row, "role", "")).lower() not in {"assistant", "system"}:
            continue
        for line in ss.meaningful_lines(str(ss.row_field(row, "text", ""))):
            if ss.is_question_like(line):
                continue
            if not (ss.STATE_SENTENCE_RE.search(line) or ss.ACTION_WORD_RE.search(line) or ss.REPORTED_CHANGE_RE.search(line)):
                continue
            cleaned = ss.clean_card_line(ss._strip_card_prefixes(line), 160)
            if not cleaned or cleaned.startswith(("|", "```", "#")):
                continue
            if re.sub(r"\W+", "", cleaned).lower() == blocked:
                continue
            return cleaned
    for candidate in (card.what_happened, card.what_this_was):
        cleaned = ss.clean_card_line(ss._strip_card_prefixes(candidate or ""), 160)
        if cleaned and not cleaned.startswith(("|", "```", "#")) and re.sub(r"\W+", "", cleaned).lower() != blocked:
            return cleaned
    return ""


def dashboard_pick_resume(
    messages: list[str],
    rows: list[sqlite3.Row],
    about: str,
    state: str,
    card: SessionCard,
) -> str:
    blocked = {re.sub(r"\W+", "", about or "").lower(), re.sub(r"\W+", "", state or "").lower()}
    intent = re.compile(
        r"\b(?:need(?:ed|s)?\s+to|should|please|fix|implement|resume|continue|find|open|next|still|unfinished|before|let'?s)\b",
        re.I,
    )
    for message in messages:
        for sentence in ss.dashboard_sentences(message):
            if not (ss.RESUME_SENTENCE_RE.search(sentence) or intent.search(sentence)):
                continue
            cleaned = ss.clean_card_line(ss._strip_card_prefixes(sentence), 160)
            if not cleaned or cleaned.lower().startswith("stop") or cleaned.startswith(("|", "```", "#", ">")):
                continue
            if re.sub(r"\W+", "", cleaned).lower() in blocked:
                continue
            return cleaned
    for row in sorted(rows or [], key=ss.row_sort_key, reverse=True):
        if str(ss.row_field(row, "role", "")).lower() not in {"assistant", "user"}:
            continue
        for line in ss.meaningful_lines(str(ss.row_field(row, "text", ""))):
            if not (ss.RESUME_SENTENCE_RE.search(line) or ss.NEXT_WORD_RE.search(line) or intent.search(line)):
                continue
            cleaned = ss.clean_card_line(ss._strip_card_prefixes(line), 160)
            if not cleaned or cleaned.startswith(("|", "```", "#", ">")):
                continue
            if re.sub(r"\W+", "", cleaned).lower() in blocked:
                continue
            return cleaned
    cleaned = ss.clean_card_line(ss._strip_card_prefixes(card.next_clue or ""), 160)
    if cleaned and re.sub(r"\W+", "", cleaned).lower() not in blocked and not cleaned.startswith(("|", "```", "#", ">")):
        return cleaned
    return ""


PROJECT_PAGE_MAX = 200


def _dashboard_sources(source_name: str) -> tuple[str, ...]:
    return tuple(ss.SUPPORTED_SOURCES) if source_name == "all" else (source_name,)


def _register_dashboard_sql_functions(conn: sqlite3.Connection) -> None:
    conn.create_function(
        "_ss_dashboard_project",
        1,
        lambda repo: ss.dashboard_project_label(str(repo or "")),
        deterministic=True,
    )
    conn.create_function(
        "_ss_dashboard_project_key",
        1,
        lambda project: ss.dashboard_clean_text(str(project or "")).casefold(),
        deterministic=True,
    )
    conn.create_function(
        "_ss_dashboard_project_noise",
        2,
        lambda project, repo: int(
            ss.dashboard_project_is_noise(str(project or ""), str(repo or ""))
        ),
        deterministic=True,
    )


def dashboard_project_results(
    conn: sqlite3.Connection,
    project_name: str,
    limit: int = 200,
    source_name: str = "all",
    offset: int = 0,
) -> tuple[str, list[tuple[sqlite3.Row, float, str]], int]:
    """Return one bounded page and the full active total for a project."""
    requested = ss.dashboard_clean_text(project_name).casefold()
    if not requested:
        return "", [], 0
    _register_dashboard_sql_functions(conn)
    sources = _dashboard_sources(source_name)
    placeholders = ",".join("?" for _source in sources)
    page_limit = min(PROJECT_PAGE_MAX, max(1, int(limit)))
    page_offset = max(0, int(offset))
    rows = conn.execute(
        f"""
        WITH ranked_user_rows AS (
            SELECT d.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.source, d.session_id
                       ORDER BY COALESCE(d.ts, 0) DESC, d.doc_id DESC
                   ) AS row_rank
            FROM documents d
            LEFT JOIN session_archive_status archive
              ON archive.source = d.source
             AND archive.session_id = d.session_id
            WHERE d.source IN ({placeholders})
              AND TRIM(COALESCE(d.text, '')) != ''
              AND d.path NOT LIKE '%/subagents/%'
              AND COALESCE(archive.archived, 0) = 0
        ),
        labeled AS (
            SELECT ranked_user_rows.*,
                   '' AS cached_repo,
                   0 AS cached_last_active,
                   '' AS cached_about,
                   COALESCE(
                       NULLIF(ranked_user_rows.cwd, ''),
                       ranked_user_rows.path
                   ) AS project_repo
            FROM ranked_user_rows
            WHERE ranked_user_rows.row_rank = 1
        ),
        matching AS (
            SELECT labeled.*,
                   _ss_dashboard_project(project_repo) AS project,
                   MAX(
                       COALESCE(labeled.ts, 0),
                       COALESCE(labeled.cached_last_active, 0)
                   ) AS activity_ts
            FROM labeled
            WHERE _ss_dashboard_project_key(
                      _ss_dashboard_project(project_repo)
                  ) = ?
              AND _ss_dashboard_project_noise(
                      _ss_dashboard_project(project_repo),
                      project_repo
                  ) = 0
        )
        SELECT matching.*,
               COUNT(*) OVER () AS project_total
        FROM matching
        ORDER BY activity_ts DESC, source, session_id
        LIMIT ? OFFSET ?
        """,
        [*sources, requested, page_limit, page_offset],
    ).fetchall()
    if not rows:
        return "", [], 0
    results: list[tuple[sqlite3.Row, float, str]] = []
    for row in rows:
        activity_ts = max(
            int(row["ts"] or 0),
            int(row["cached_last_active"] or 0),
        )
        results.append((row, ss.recency_boost(activity_ts), "project"))
    return str(rows[0]["project"]), results, int(rows[0]["project_total"])


def dashboard_project_summaries(
    conn: sqlite3.Connection,
    source_name: str = "all",
    thread_limit: int = 10,
    project_scan_limit: int = 250,
) -> list[dict[str, Any]]:
    """Aggregate every active indexed session without constructing cards."""
    del thread_limit, project_scan_limit
    _register_dashboard_sql_functions(conn)
    sources = _dashboard_sources(source_name)
    placeholders = ",".join("?" for _source in sources)
    rows = conn.execute(
        f"""
        WITH ranked_user_rows AS (
            SELECT d.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.source, d.session_id
                       ORDER BY COALESCE(d.ts, 0) DESC, d.doc_id DESC
                   ) AS row_rank
            FROM documents d
            LEFT JOIN session_archive_status archive
              ON archive.source = d.source
             AND archive.session_id = d.session_id
            WHERE d.source IN ({placeholders})
              AND TRIM(COALESCE(d.text, '')) != ''
              AND d.path NOT LIKE '%/subagents/%'
              AND COALESCE(archive.archived, 0) = 0
        ),
        labeled AS (
            SELECT ranked_user_rows.*,
                   '' AS cached_repo,
                   0 AS cached_last_active,
                   '' AS cached_about,
                   COALESCE(
                       NULLIF(ranked_user_rows.cwd, ''),
                       ranked_user_rows.path
                   ) AS project_repo,
                   _ss_dashboard_project(
                       COALESCE(
                           NULLIF(ranked_user_rows.cwd, ''),
                           ranked_user_rows.path
                       )
                   ) AS project
            FROM ranked_user_rows
            WHERE ranked_user_rows.row_rank = 1
        ),
        project_rows AS (
            SELECT labeled.*,
                   MAX(
                       COALESCE(labeled.ts, 0),
                       COALESCE(labeled.cached_last_active, 0)
                   ) AS activity_ts,
                   COUNT(*) OVER (PARTITION BY project) AS project_count,
                   ROW_NUMBER() OVER (
                       PARTITION BY project
                       ORDER BY
                           MAX(
                               COALESCE(labeled.ts, 0),
                               COALESCE(labeled.cached_last_active, 0)
                           ) DESC,
                           source,
                           session_id
                   ) AS project_rank
            FROM labeled
            WHERE _ss_dashboard_project_noise(project, project_repo) = 0
        )
        SELECT *
        FROM project_rows
        WHERE project_rank = 1
        ORDER BY activity_ts DESC, project COLLATE NOCASE
        """,
        [*sources],
    ).fetchall()
    projects: list[dict[str, Any]] = []
    for row in rows:
        about = ss.clean_card_line(str(row["cached_about"] or ""), 160)
        if not about:
            title = ss.clean_card_line(str(row["title"] or ""), 160)
            if title and not ss.is_weak_title(title):
                about = title
            else:
                about = ss.clean_card_line(str(row["text"] or "")[:500], 160)
        projects.append(
            {
                "project": str(row["project"]),
                "count": int(row["project_count"]),
                "latest_ts": int(row["activity_ts"] or 0),
                "latest_about": about,
                "latest_session_id": str(row["session_id"]),
                "latest_source": str(row["source"]),
                "repo": str(row["project_repo"] or ""),
            }
        )
    return projects


def dashboard_summary(
    card: SessionCard,
    rows: list[sqlite3.Row],
    fallback: sqlite3.Row,
) -> tuple[str, str, str, str]:
    requests = ss.recent_user_messages(rows, fallback, limit=6)
    about_raw = ss.dashboard_pick_about(requests, card)
    state_raw = ss.dashboard_pick_state(rows, about_raw, card)
    resume_raw = ss.dashboard_pick_resume(requests, rows, about_raw, state_raw, card)
    clue = ""
    if card.mentioned_paths:
        path = ss._dedupe_card_path(card.mentioned_paths[0])
        if path and "history.jsonl" not in path.lower():
            clue = f"Path: {path}"
    fields = quality_gate(ss._card_field_ready(about_raw), ss._card_field_ready(state_raw), ss._card_field_ready(resume_raw))
    return (
        ss.compact(fields.about, 140),
        ss.compact(fields.state, 160),
        ss.compact(fields.resume, 160),
        ss.compact(clue, 120),
    )


def result_card(conn: sqlite3.Connection, row: sqlite3.Row, query: str, label: str) -> dict[str, Any]:
    rows = ss.session_rows(conn, row)
    session_card = ss.session_card_for_result(conn, row, query, persist=True)
    return {
        "row": row,
        "rows": rows,
        "session_card": session_card,
        "source": session_card.source,
        "source_label": ss.source_label(session_card.source),
        "session_id": session_card.session_id,
        "archived": ss.session_is_archived(conn, session_card.source, session_card.session_id),
        "title": session_card.title,
        "repo": session_card.repo,
        "last_active": session_card.last_active,
        "hits": ss.hit_count(label),
        "why": ss.why_line(query, row, label, rows),
        "evidence": ss.snippet(row["text"], query),
    }


def card_visible_text(card: dict[str, Any]) -> str:
    session_card: ss.SessionCard = card["session_card"]
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
    current_terms = set(ss.evidence_terms_from_text(query, ss.card_visible_text(current), limit=12))
    strong_terms = {term for term in ss.query_anchor_terms(query) if term in ss.ACTION_ANCHOR_TERMS}
    candidates: list[tuple[float, int, dict[str, Any]]] = []
    for rank, card in enumerate(cards, 1):
        if rank == current_rank:
            continue
        row = card["row"]
        if row["source"] == current_row["source"] and row["session_id"] == current_row["session_id"]:
            continue
        if not ss.is_side_task_query(query) and ss.is_side_task_session(card["rows"], row):
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
        other_terms = set(ss.evidence_terms_from_text(query, ss.card_visible_text(card), limit=12))
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
        marker = ss.session_status_marker(bool(card["archived"]))
        lines.append(f"{rank}. [{card['source_label']}]{marker} {ss.compact(str(card['title']), 80)}")
    return lines


def owner_action_label(source: str, session_id: str) -> str:
    if ss.native_resume_available(source, session_id):
        return f"opens exactly in {ss.source_label(source)}"
    return "can continue from a context packet"


def alternate_harness_line(rank: int, source: str) -> str:
    targets = ss.handoff_targets_for(source)
    if not targets:
        return ""
    labels = [ss.target_label(target) for target in targets]
    if len(labels) == 1:
        target = targets[0]
        return f"continue in {labels[0]}: ss continue {rank} in {target}"
    parts = [f"{label}: ss continue {rank} in {target}" for label, target in zip(labels, targets)]
    return "continue elsewhere: " + " | ".join(parts)


def location_label(row: sqlite3.Row) -> str:
    cwd = ss.sanitize_text(row["cwd"] or "")
    if cwd:
        return cwd
    path = ss.sanitize_text(row["path"] or "")
    if path:
        return path
    return "unknown"


def evidence_terms_from_text(query: str, text: str, limit: int = 8) -> list[str]:
    haystack = text.lower()
    terms: list[str] = []
    seen: set[str] = set()
    for token in ss.query_anchor_terms(query):
        clean = token.strip().lower()
        if clean in seen:
            continue
        if ss.term_present(clean, haystack):
            terms.append(clean)
            seen.add(clean)
        if len(terms) >= limit:
            break
    return terms


def evidence_terms(query: str, row: sqlite3.Row, limit: int = 8) -> list[str]:
    return ss.evidence_terms_from_text(
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
    terms = ss.evidence_terms_from_text(query, ss.full_session_text(rows)) if rows else ss.evidence_terms(query, row)
    if terms:
        quoted = ", ".join(f'"{term}"' for term in terms)
        return f"Found remembered words/ideas: {quoted}."
    return "Closest candidate from the indexed session text; check the evidence snippet."


def snippet(text: str, query: str, width: int = 280) -> str:
    clean = re.sub(r"\s+", " ", ss.sanitize_text(text))
    if len(clean) <= width:
        return clean
    tokens = ss.tokenize(query)
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
    cards = [ss.result_card(conn, row, query, label) for row, _score, label in results]
    for i, card in enumerate(cards, 1):
        session_card: ss.SessionCard = card["session_card"]
        source = card["source"]
        session_id = card["session_id"]
        repo = card["repo"]
        hits = card["hits"]
        archive_marker = "[ARCHIVED] " if card["archived"] else ""
        print(f"{i}. {archive_marker}{card['title']}")
        print(f"   Found in: {card['source_label']} ({ss.owner_action_label(source, session_id)})")
        print(f"   Work folder: {ss.repo_label(repo)}")
        print(f"   Last touched: {ss.iso_date(card['last_active'])}")
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
        related = ss.related_result_lines(cards, i, query) if i <= 3 else []
        if related:
            print()
            print("   Related sessions worth checking:")
            for line in related:
                print(f"     {line}")
        print()
        print("   What you can do:")
        native_lines = ss.native_resume_lines(source, session_id, repo)
        if native_lines:
            print(f"     open exact session: ss open {i}")
        else:
            print("     exact reopen is not available for this result")
            print(f"     why: {ss.packet_only_note(source)}")
        alternate = ss.alternate_harness_line(i, source)
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
    sources = tuple(ss.SUPPORTED_SOURCES) if source_name == "all" else (source_name,)
    placeholders = ",".join("?" for _ in sources)
    candidates = conn.execute(
        f"""
        SELECT d.source, d.session_id,
               MAX(COALESCE(d.ts, 0)) AS last_activity_ts
        FROM documents d
        LEFT JOIN session_archive_status archive
          ON archive.source = d.source
         AND archive.session_id = d.session_id
        WHERE d.source IN ({placeholders})
          AND d.path NOT LIKE '%/subagents/%'
          AND COALESCE(archive.archived, 0) = ?
        GROUP BY d.source, d.session_id
        ORDER BY last_activity_ts DESC
        LIMIT ?
        """,
        [*sources, int(archived_only), max(limit * 5, 50)],
    )
    results: list[tuple[sqlite3.Row, float, str]] = []
    for candidate in candidates:
        row = ss.representative_session_row(conn, str(candidate["source"]), str(candidate["session_id"]))
        if row is None:
            continue
        archived = ss.session_is_archived(conn, str(row["source"]), str(row["session_id"]))
        if archived != archived_only:
            continue
        rows = ss.session_rows(conn, row)
        activity_ts = ss.last_user_prompt_ts(rows, row)
        results.append((row, ss.recency_boost(activity_ts), "recent"))
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
    return [(row, ss.recency_boost(row["archive_status_at"]), "archived") for row in rows], int(missing)


def catch_up_dashboard_cards(
    conn: sqlite3.Connection,
    results: list[tuple[sqlite3.Row, float, str]],
    limit: int = 10,
    quiet: bool = True,
) -> int:
    """Build missing/stale cards for only the newest visible dashboard sessions."""
    candidates: list[tuple[list[sqlite3.Row], sqlite3.Row, str]] = []
    seen: set[tuple[str, str]] = set()
    for row, _score, _label in results:
        identity = (str(row["source"]), str(row["session_id"]))
        if identity in seen:
            continue
        seen.add(identity)
        rows = ss.session_rows(conn, row)
        text_hash = ss.session_card_hash(rows)
        cached = conn.execute(
            """
            SELECT summary_source
            FROM session_cards
            WHERE source = ? AND session_id = ? AND text_hash = ?
            """,
            (*identity, text_hash),
        ).fetchone()
        if ss.cached_card_is_current(cached):
            continue
        candidates.append((rows, row, text_hash))
        if len(candidates) >= limit:
            break

    if candidates and not ss.llm_available():
        ss.warn_llm_unavailable()
    if candidates and not quiet:
        refresh_line = f"Refreshing {len(candidates)} recent session summaries..."
        for line in dashboard_wrapped_lines(refresh_line, ss.dashboard_terminal_width()):
            print(line)
        sys.stdout.flush()

    stored = 0
    for rows, row, text_hash in candidates:
        card = ss.build_session_card(rows, row, "", use_llm=True)
        with ss.session_lock(shared=False):
            with conn:
                ss.store_session_card(conn, card, text_hash)
        stored += 1
    return stored


def print_dashboard(
    conn: sqlite3.Connection,
    results: list[tuple[sqlite3.Row, float, str]],
    archived_view: bool = False,
    project_summaries: list[dict[str, Any]] | None = None,
    heading: str = "",
    total_threads: int | None = None,
    page_offset: int = 0,
) -> list[dict[str, Any]]:
    """Restart recovery screen: sparse, grouped, scannable."""
    width = ss.dashboard_terminal_width()
    title = heading or ("SS archived" if archived_view else "SS")
    print(title)
    if not results:
        print("No saved Claude/Codex sessions yet.")
        print("Run: ss fresh what did I work on recently")
        return []

    thread_entries: list[dict[str, Any]] = []
    for rank, (row, _score, label) in enumerate(results, 1):
        card = ss.session_card_for_result(conn, row, "", persist=False, use_llm=False)
        rows = ss.session_rows(conn, row)
        about, state, resume, clue = ss.dashboard_summary(card, rows, row)
        repo = card.repo or ss.location_label(row)
        project = ss.dashboard_project_label(repo)
        archived = ss.session_is_archived(conn, card.source, card.session_id)
        if archived and not project.endswith(" [ARCHIVED]"):
            project_display = f"{project} [ARCHIVED]"
        else:
            project_display = project
        thread_entries.append(
            {
                "rank": rank,
                "row": row,
                "card": card,
                "about": ss.dashboard_clean_text(about),
                "state": ss.dashboard_clean_text(state),
                "resume": ss.dashboard_clean_text(resume),
                "clue": ss.dashboard_clean_text(clue),
                "project": project_display,
                "source": ss.source_label(str(row["source"])),
                "when": ss.dashboard_relative_time(card.last_active),
                "repo": repo,
                "archived": archived,
            }
        )

    # Group the visible threads under projects. This is the main scan surface.
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for entry in thread_entries:
        key = str(entry["project"])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(entry)
    for key in order:
        groups.append((key, grouped[key]))

    if project_summaries is None and not archived_view:
        project_summaries = ss.dashboard_project_summaries(
            conn,
            source_name="all",
            thread_limit=len(results),
        )

    # Quiet "more projects" footer: only projects not already on screen.
    visible_projects = {name.removesuffix(" [ARCHIVED]") for name, _items in groups}
    more_projects: list[dict[str, Any]] = []
    if project_summaries:
        for item in project_summaries:
            name = str(item.get("project") or "")
            if not name or name in visible_projects:
                continue
            if ss.dashboard_project_is_noise(name, str(item.get("repo") or "")):
                continue
            more_projects.append(item)
        more_projects = more_projects[:8]

    total = len(thread_entries) if total_threads is None else max(len(thread_entries), int(total_threads))
    if total > len(thread_entries) or page_offset:
        first = page_offset + 1
        last = page_offset + len(thread_entries)
        count_line = f"{first}-{last} shown of {total} threads · open with: ss open N"
    else:
        count_line = f"{len(thread_entries)} recent thread{'s' if len(thread_entries) != 1 else ''} · open with: ss open N"
    for line in dashboard_wrapped_lines(count_line, width):
        print(line)
    print()

    card_width = min(width, 112)
    for project_name, items in groups:
        latest = items[0]
        header = f"{project_name}  ·  {latest['when']}  ·  {len(items)} shown"
        for line in dashboard_wrapped_lines(header, width):
            print(line)
        # Keep one short folder line only when it adds information.
        folder = ss.repo_label(latest["repo"])
        if folder and folder not in {"unknown", "unknown from index"}:
            for line in dashboard_wrapped_lines(folder, max(1, width - 2)):
                print(f"  {line}")
        for entry in items:
            tool = ss.dashboard_short_tool(str(entry["source"]))
            marker = " [ARCHIVED]" if entry["archived"] and "[ARCHIVED]" not in project_name else ""
            print()
            about = entry["about"] or "Untitled session."
            state = entry["state"] or "No clear completed work found in local evidence."
            resume = entry["resume"] or "No clear next step found in local evidence."
            fields = [
                ("About:", about),
                ("State:", state),
                ("Resume:", resume),
            ]
            if entry["clue"] and not entry["clue"].lower().startswith("key terms:"):
                fields.append(("Clue:", entry["clue"]))
            fields.append(("Open:", f"ss open {entry['rank']}"))
            title_line = f"{entry['rank']} · {tool}{marker} · {entry['when']}"
            for line in dashboard_restart_card_lines(title_line, fields, card_width):
                print(line)
        print()

    if more_projects:
        print("Older projects still saved:")
        for project_rank, item in enumerate(more_projects, 1):
            when = ss.dashboard_relative_time(item.get("latest_ts"))
            summary = (
                f"P{project_rank} · {item['project']} · {when} · "
                f"{item.get('count', 0)} threads · "
                f"{item.get('latest_about') or 'No summary available.'}"
            )
            for line in dashboard_wrapped_lines(summary, max(1, width - 2)):
                print(f"  {line}")
        for line in dashboard_wrapped_lines(
            "Choose project: pN · Search: ss <words> · See finished: ss archived",
            max(1, width - 2),
        ):
            print(f"  {line}")
        print()

    for line in dashboard_wrapped_lines(
        "ss <search> · ss open N · ss look at N · ss archive N · ss archived",
        width,
    ):
        print(line)
    return more_projects


def dashboard_cell(value: str, width: int) -> str:
    clean = ss.dashboard_clean_text(value)
    if width <= 1:
        return clean[:1]
    if len(clean) <= width:
        return clean
    return clean[: max(1, width - 1)].rstrip() + "…"


def dashboard_wrapped_lines(value: str, width: int) -> list[str]:
    """Wrap terminal copy without discarding words."""
    clean = ss.dashboard_clean_text(value)
    if not clean:
        return [""]
    line_width = max(1, int(width))
    lines: list[str] = []
    current = ""
    for word in clean.split():
        candidate = word if not current else f"{current} {word}"
        if dashboard_display_width(candidate) <= line_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        if dashboard_display_width(word) <= line_width:
            current = word
            continue
        chunks = _dashboard_split_display_chunks(word, line_width)
        lines.extend(chunks[:-1])
        current = chunks[-1]
    if current:
        lines.append(current)
    return lines or [""]


def dashboard_display_width(value: str) -> int:
    """Return the terminal cell width of Unicode text."""
    measured = wcswidth(value)
    if measured >= 0:
        return measured
    return sum(max(0, wcwidth(char)) for char in value)


def _dashboard_split_display_chunks(value: str, width: int) -> list[str]:
    """Split only an unbroken token that cannot fit on one line."""
    chunks: list[str] = []
    current: list[str] = []
    current_width = 0
    for cluster in _dashboard_grapheme_clusters(value):
        cluster_width = dashboard_display_width(cluster)
        if current and current_width + cluster_width > width:
            chunks.append("".join(current).rstrip())
            current = []
            current_width = 0
            if cluster.isspace():
                continue
        current.append(cluster)
        current_width += cluster_width
    if current:
        chunks.append("".join(current).rstrip())
    return chunks or [""]


def _dashboard_grapheme_clusters(value: str) -> list[str]:
    """Keep common joined Unicode sequences intact during emergency splits."""
    clusters: list[str] = []
    current = ""
    regional_count = 0
    for char in value:
        codepoint = ord(char)
        is_regional = 0x1F1E6 <= codepoint <= 0x1F1FF
        is_extension = (
            unicodedata.category(char).startswith("M")
            or 0xFE00 <= codepoint <= 0xFE0F
            or 0x1F3FB <= codepoint <= 0x1F3FF
            or 0xE0020 <= codepoint <= 0xE007F
        )
        if not current:
            current = char
            regional_count = 1 if is_regional else 0
            continue
        if char == "\u200d" or current.endswith("\u200d") or is_extension:
            current += char
            continue
        if is_regional and regional_count == 1:
            current += char
            regional_count = 0
            continue
        clusters.append(current)
        current = char
        regional_count = 1 if is_regional else 0
    if current:
        clusters.append(current)
    return clusters


def dashboard_restart_card_lines(
    title: str,
    fields: list[tuple[str, str]],
    width: int,
) -> list[str]:
    """Render one complete restart card within the requested terminal width."""
    card_width = max(12, int(width))
    narrow = card_width < 40
    top_left, horizontal, top_right = ("+", "-", "+") if narrow else ("┌", "─", "┐")
    bottom_left, bottom_right, vertical = ("+", "+", "|") if narrow else ("└", "┘", "│")
    inner_width = card_width - 4
    clean_title = ss.dashboard_clean_text(title)

    lines: list[str] = []
    title_room = card_width - 6
    if clean_title and dashboard_display_width(clean_title) <= title_room:
        remaining = card_width - dashboard_display_width(clean_title) - 5
        lines.append(f"{top_left}{horizontal} {clean_title} {horizontal * remaining}{top_right}")
    else:
        lines.append(f"{top_left}{horizontal * (card_width - 2)}{top_right}")

    def bordered(content: str) -> str:
        padding = max(0, inner_width - dashboard_display_width(content))
        return f"{vertical} {content}{' ' * padding} {vertical}"

    if clean_title and dashboard_display_width(clean_title) > title_room:
        for part in dashboard_wrapped_lines(clean_title, inner_width):
            lines.append(bordered(part))

    stacked_fields = card_width < 52
    for label, value in fields:
        clean_value = ss.dashboard_clean_text(value)
        if stacked_fields:
            lines.append(bordered(label))
            body_width = max(1, inner_width - 2)
            for part in dashboard_wrapped_lines(clean_value, body_width):
                lines.append(bordered(f"  {part}"))
            continue

        prefix = f"{label} "
        body_width = max(1, inner_width - len(prefix))
        wrapped = dashboard_wrapped_lines(clean_value, body_width)
        lines.append(bordered(prefix + wrapped[0]))
        continuation = " " * len(prefix)
        for part in wrapped[1:]:
            lines.append(bordered(continuation + part))

    lines.append(f"{bottom_left}{horizontal * (card_width - 2)}{bottom_right}")
    return lines


def dashboard_project_label(repo: str) -> str:
    """Turn a working directory into a stable, human-sized project name."""
    raw = ss.sanitize_text(repo)
    if not raw or raw in {"unknown", "unknown from index"}:
        return "Unknown folder"
    normalized = raw.replace("\\", "/").lower()
    if normalized.endswith("/globalstorage/state.vscdb"):
        tool = "Cursor" if "/cursor/" in normalized else "VS Code"
        return f"{tool} / Global"
    if "/workspacestorage/" in normalized and normalized.endswith("/state.vscdb"):
        tool = "Cursor" if "/cursor/" in normalized else "VS Code"
        return f"{tool} / Unknown workspace"
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
            return "OS root"
        if len(relative) >= 2:
            return f"{ss._humanize_path_part(relative[0])} / {ss._humanize_path_part(relative[1])}"
        return ss._humanize_path_part(relative[0])

    useful = [part for part in parts if part not in {".", ""}]
    # Hide machine-history paths and bare home noise from the main map labels.
    joined = "/".join(useful).lower()
    if joined.endswith("history.jsonl") or "/.codex/" in f"/{joined}/":
        return "Codex history"
    if len(useful) >= 2:
        return " / ".join(ss._humanize_path_part(part) for part in useful[-2:])
    return ss._humanize_path_part(useful[-1])


def print_dashboard_table(headers: list[str], rows: list[list[str]], width: int | None = None) -> None:
    """Render a plain terminal table when a compact matrix is actually useful."""
    if not headers:
        return
    term_width = ss.dashboard_terminal_width() if width is None else max(40, width)
    col_count = len(headers)
    separator_tax = 3 * max(0, col_count - 1)
    available = max(col_count * 4, term_width - separator_tax)
    widths = [max(len(header), 4) for header in headers]
    while sum(widths) > available and any(value > 4 for value in widths[1:]):
        for index in range(col_count - 1, 0, -1):
            if widths[index] > 4 and sum(widths) > available:
                widths[index] -= 1
    remainder = max(0, available - sum(widths))
    widths[-1] += remainder
    print(" | ".join(headers[i].ljust(widths[i]) for i in range(col_count)))
    print("-+-".join("-" * widths[i] for i in range(col_count)))
    for row in rows:
        fitted = [ss.dashboard_cell(str(cell), widths[i]) for i, cell in enumerate(row)]
        print(" | ".join(fitted[i].ljust(widths[i]) for i in range(col_count)))


def dashboard_is_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def dashboard_prompt_text(*, in_project: bool) -> str:
    """Keep the input prompt inside the same terminal-width contract as cards."""
    width = ss.dashboard_terminal_width()
    if in_project:
        long = "Open N, n next, b back, type search words, or q: "
        short = "Open N, n, b, search, or q: "
    else:
        long = "Open N, choose project Pn, type search words, or q: "
        short = "Open N, Pn, search, or q: "
    return long if dashboard_display_width(long) <= width else short


def dashboard_prompt(args: argparse.Namespace) -> int:
    if not ss.dashboard_is_interactive():
        return 0
    while True:
        try:
            prompt = ss.dashboard_prompt_text(
                in_project=bool(getattr(args, "current_project", ""))
            )
            choice = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not choice or choice.lower() in {"q", "quit", "exit"}:
            return 0
        project_match = re.fullmatch(r"p(\d+)", choice, flags=re.I)
        if project_match:
            project_choices = list(getattr(args, "project_choices", []) or [])
            project_index = int(project_match.group(1)) - 1
            if project_index < 0 or project_index >= len(project_choices):
                print(f"Project selector not found: {choice}")
                continue
            print()
            project_args = argparse.Namespace(
                db=args.db,
                home=args.home,
                project=str(project_choices[project_index]["project"]),
                project_choices=project_choices,
                limit=ss.PROJECT_PAGE_MAX,
                source=args.source,
                mode=args.mode,
                no_refresh=True,
                page=1,
                prompt=False,
            )
            status = ss.cmd_project(project_args)
            if status == 0:
                args.current_project = getattr(project_args, "current_project", "")
                args.project_page = getattr(project_args, "project_page", 1)
                args.project_total = getattr(project_args, "project_total", 0)
                args.project_limit = getattr(project_args, "project_limit", ss.PROJECT_PAGE_MAX)
            continue
        if choice.lower() in {"n", "next", "b", "back", "prev", "previous"} and getattr(
            args, "current_project", ""
        ):
            is_back = choice.lower() in {"b", "back", "prev", "previous"}
            current_page = int(getattr(args, "project_page", 1))
            if is_back and current_page <= 1:
                print()
                dashboard_args = argparse.Namespace(
                    db=args.db,
                    home=args.home,
                    no_refresh=True,
                    archived=False,
                    limit=args.limit,
                    source=args.source,
                    mode=args.mode,
                    prompt=False,
                )
                status = ss.cmd_dashboard(dashboard_args)
                if status == 0:
                    args.project_choices = list(
                        getattr(dashboard_args, "project_choices", []) or []
                    )
                    args.current_project = ""
                    args.project_page = 1
                    args.project_total = 0
                continue
            direction = -1 if is_back else 1
            next_page = max(1, current_page + direction)
            page_limit = int(getattr(args, "project_limit", ss.PROJECT_PAGE_MAX))
            if (next_page - 1) * page_limit >= int(getattr(args, "project_total", 0)):
                print("No more project sessions.")
                continue
            print()
            project_args = argparse.Namespace(
                db=args.db,
                home=args.home,
                project=args.current_project,
                project_choices=list(getattr(args, "project_choices", []) or []),
                limit=page_limit,
                source=args.source,
                mode=args.mode,
                no_refresh=True,
                page=next_page,
                prompt=False,
            )
            status = ss.cmd_project(project_args)
            if status == 0:
                args.project_page = project_args.project_page
                args.project_total = project_args.project_total
                args.project_limit = project_args.project_limit
            continue
        if choice.isdigit():
            print()
            return ss.cmd_resume(argparse.Namespace(db=args.db, selector=choice))
        tokens = choice.split()
        if tokens and tokens[0].lower() in {"ss", "sessions"}:
            tokens = tokens[1:]
        if tokens and tokens[0].lower() == "archived":
            print()
            return ss.cmd_archived(
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
        if tokens and tokens[0].lower() == "project":
            project_value = " ".join(tokens[1:]).strip()
            if project_value:
                print()
                project_args = argparse.Namespace(
                    db=args.db,
                    home=args.home,
                    project=project_value,
                    project_choices=list(getattr(args, "project_choices", []) or []),
                    limit=ss.PROJECT_PAGE_MAX,
                    source=args.source,
                    mode=args.mode,
                    no_refresh=True,
                    page=1,
                    prompt=False,
                )
                status = ss.cmd_project(project_args)
                if status == 0:
                    args.current_project = project_args.current_project
                    args.project_page = project_args.project_page
                    args.project_total = project_args.project_total
                    args.project_limit = project_args.project_limit
                continue
        followup = ss.parse_natural_followup(tokens)
        if followup is not None:
            followup.db = args.db
            print()
            return int(followup.func(followup))
        if tokens and tokens[0].lower() in {"fresh", "refresh", "new"}:
            print()
            return ss.cmd_natural(
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
            return ss.cmd_search(
                argparse.Namespace(
                    db=args.db,
                    query=query,
                    limit=args.limit,
                    source=args.source,
                    mode=args.mode,
                )
            )


def print_session_card_detail(card: SessionCard, archived: bool = False) -> None:
    print("Session card")
    print(f"Title: {card.title}")
    print(f"Found in: {ss.source_label(card.source)}")
    print(f"Work folder: {ss.repo_label(card.repo)}")
    print(f"Session: {ss.session_ref(card.source, card.session_id)}")
    print(f"Status: {ss.session_status_text(archived)}")
    print(f"Last touched: {ss.iso_date(card.last_active)}")
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
        print(f"- {item.label} [{item.role}, {ss.iso_date(item.ts)}]: {item.text}")
    print()


def iter_dashboard_updates(conn: sqlite3.Connection, home: pathlib.Path) -> Iterator[Document]:
    """Yield only changed Claude files and recent Codex records for a fast dashboard sync."""
    codex_cutoff = int(
        conn.execute("SELECT COALESCE(MAX(ts), 0) FROM documents WHERE source = 'codex'").fetchone()[0]
    )
    thread_context = ss.codex_thread_context(home)
    for document in ss.iter_codex_threads(home):
        if document.ts is None or document.ts >= max(0, codex_cutoff - 3600):
            yield document
    for document in ss.iter_codex_history(home, thread_context):
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
            for document in ss.iter_claude_jsonl(file_path, source="claude"):
                if last_indexed == 0 or document.ts is None or document.ts > last_ts:
                    yield document


def refresh_dashboard_index(conn: sqlite3.Connection, home: pathlib.Path) -> int:
    count = ss.upsert_documents(conn, ss.iter_dashboard_updates(conn, home))
    ss.sync_detected_archive_states(conn)
    return count
