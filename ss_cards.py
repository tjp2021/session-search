"""Session cards: build, cache, provenance, titles, summaries, and the model-summary gate.

Split from session_search.py. Project symbols resolve through the
session_search facade at call time (``ss.<name>``) so tests that patch or
mutate facade attributes keep intercepting the behavior of this module.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import sys
from typing import Any, Iterable
from archive_intent import IntentKind, classify_session_intent
from card_quality import TRUNCATED_RE
from secret_patterns import redact as redact_secrets

import session_search as ss


def source_label(source: str) -> str:
    return {
        "claude": "Claude Code",
        "codex": "Codex",
        "pi": "Pi",
        "vscode": "VS Code / Copilot",
        "cursor": "Cursor",
    }.get(source, source)


def compact(text: str, limit: int = 96) -> str:
    text = re.sub(r"\s+", " ", ss.sanitize_text(text)).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def is_machine_title(title: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9-]{24,}", title.strip(), flags=re.I))


def is_weak_title(title: str) -> bool:
    title = ss.sanitize_text(title)
    if not title or ss.is_machine_title(title):
        return True
    if ss.LOW_SIGNAL_SESSION_LINE_RE.search(title):
        return True
    if title.lower() in {"ls", "pwd", "cd", "git status", "status"}:
        return True
    if ss.PROMPT_STARTER_RE.search(title):
        return True
    if len(title.split()) > 12 and any(
        word in title.lower() for word in ("need", "you to", "help me", "analyze", "resume the", "fucking")
    ):
        return True
    return len(ss.tokenize(title)) <= 1 and len(title) < 12


def synthesize_resume_from_text(text: str) -> str:
    """Attempts to synthesize a meaningful resume/next-action from raw session text using an LLM."""
    cleaned_text = ss.sanitize_text(text)
    truncated_text = cleaned_text[:4000]
    prompt = f"Summarize the next action or resume point from this session text in 1 sentence: {truncated_text}"
    summary = ss.llm_summarize(prompt, max_tokens=120)
    if summary:
        return ss.clean_card_line(summary, 260)
    return ""


def derive_document_title(original_title: str, full_text: str) -> str:
    """Derives a suitable Document title, prioritizing original if strong, else a simple fallback.

    LLM title synthesis is intentionally NOT done here (indexing is bulk).
    It runs later in build_session_card / synthesize_title_from_text on demand.
    """
    if original_title and not ss.is_weak_title(original_title):
        return ss.clean_card_line(original_title, 110)
    # Simple non-LLM fallback: only the FIRST meaningful line may become a title.
    # Scanning deeper leaks arbitrary transcript content into the session name.
    cleaned = ss.sanitize_text(full_text)
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        if ss.is_weak_title(line):
            break
        return ss.clean_card_line(line, 110)
    return ss.UNTITLED_SESSION


def document_title_with_fallback(original_title: str, full_text: str, fallback: str) -> str:
    """Use the owning session's own title when no safe title can be derived.

    derive_document_title() returns the UNTITLED_SESSION sentinel, which is
    truthy, so call sites cannot fall back with a plain `or`.
    """
    derived = ss.derive_document_title(original_title, full_text)
    if derived and derived != ss.UNTITLED_SESSION:
        return derived
    candidate = ss.sanitize_text(fallback)
    if candidate and not ss.is_weak_title(candidate):
        return ss.clean_card_line(candidate, 110)
    return ss.UNTITLED_SESSION


def _load_openrouter_api_key() -> str:
    """Read OPENROUTER_API_KEY from the environment only, never logging it.

    No config-file fallback: a public package must not read another
    product's dotfiles for a credential.
    """
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def summaries_enabled() -> bool:
    """True only when the user opted this tool into model summaries.

    A key in the environment is not consent; plenty of unrelated tools use
    OPENROUTER_API_KEY. SS_SUMMARIES=openrouter is the explicit switch.
    """
    return os.environ.get("SS_SUMMARIES", "").strip().lower() == "openrouter"


def llm_available() -> bool:
    """True when the user opted in and a key is present."""
    return ss.summaries_enabled() and bool(ss._load_openrouter_api_key())


def warn_llm_unavailable() -> None:
    """Say once why summaries are degraded. Silent degradation is a bug."""
    if ss._LLM_WARNED:
        return
    ss._LLM_WARNED = True
    print(
        "Model summaries are off, so session cards fall back to local evidence "
        "lines. To turn them on, set SS_SUMMARIES=openrouter and OPENROUTER_API_KEY. "
        "Cards are rebuilt automatically once both are set.",
        file=sys.stderr,
    )


def llm_summarize(prompt: str, max_tokens: int = 220) -> str:
    """Call OpenRouter chat completions (gpt-4.1-nano) to summarize the given text.

    Returns an empty string on any failure so callers fall back to regex heuristics.
    """
    if not ss.llm_available():
        return ""
    import requests

    api_key = ss._load_openrouter_api_key()
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-4.1-nano",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content", "")).strip()
    except Exception as e:
        print(f"OpenRouter summarize error: {e}", file=sys.stderr)
        return ""


def synthesize_title_from_text(text: str) -> str:
    """Attempts to synthesize a meaningful title from raw session text using an LLM."""
    cleaned_text = ss.sanitize_text(text)
    truncated_text = cleaned_text[:4000]
    prompt = f"Summarize this session text in 1 sentence for a title: {truncated_text}"
    summary = ss.llm_summarize(prompt, max_tokens=80)
    if summary:
        return ss.clean_card_line(summary, 110)
    return ss.UNTITLED_SESSION


def candidate_title(row: sqlite3.Row) -> str:
    title = ss.sanitize_text(row["title"] or "")
    text = ss.sanitize_text(row["text"] or "")
    return ss.derive_document_title(title, text)


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
    fallback_title = ss.candidate_title(fallback)
    if fallback_title and fallback_title != ss.UNTITLED_SESSION:
        return fallback_title
    for row in rows:
        title = ss.sanitize_text(row["title"] or "")
        if title and not ss.is_weak_title(title):
            return ss.compact(title, 110)
    return fallback_title


def best_session_ts(rows: list[sqlite3.Row], fallback: sqlite3.Row) -> int | None:
    timestamps = [row["ts"] for row in rows if row["ts"]]
    if timestamps:
        return int(max(timestamps))
    return fallback["ts"]


def is_user_prompt_row(row: sqlite3.Row) -> bool:
    role = str(ss.row_field(row, "role", "")).lower()
    if role == "user":
        return True
    text = str(ss.row_field(row, "text", ""))
    return role == "turn" and bool(re.search(r"(?:^|\n)\s*USER\s*:", text, flags=re.I))


def last_user_prompt_ts(rows: list[sqlite3.Row], fallback: sqlite3.Row) -> int | None:
    timestamps = [
        ss.row_field(row, "ts", None)
        for row in rows
        if ss.is_user_prompt_row(row) and ss.row_field(row, "ts", None)
    ]
    if timestamps:
        return int(max(timestamps))
    if ss.is_user_prompt_row(fallback) and ss.row_field(fallback, "ts", None):
        return int(ss.row_field(fallback, "ts"))
    return ss.best_session_ts(rows, fallback)


def full_user_message_text(row: sqlite3.Row) -> str:
    if not ss.is_user_prompt_row(row):
        return ""
    if "/subagents/" in str(ss.row_field(row, "path", "")):
        return ""
    text = ss.sanitize_text(ss.row_field(row, "text", ""))
    if str(ss.row_field(row, "role", "")).lower() == "turn":
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
    if ss.INJECTED_USER_MESSAGE_RE.match(text):
        return ""
    return text


def user_message_text(row: sqlite3.Row) -> str:
    return ss.compact(ss.full_user_message_text(row), 320)


def last_user_message(rows: list[sqlite3.Row], fallback: sqlite3.Row) -> str:
    candidates = sorted(
        (row for row in rows if ss.is_user_prompt_row(row)),
        key=lambda row: (ss.row_field(row, "ts", 0) or 0, str(ss.row_field(row, "doc_id", ""))),
        reverse=True,
    )
    for row in candidates:
        text = ss.user_message_text(row)
        if text:
            return text
    return ss.user_message_text(fallback)


def recent_user_messages(
    rows: list[sqlite3.Row],
    fallback: sqlite3.Row,
    limit: int = 3,
) -> list[str]:
    messages: list[str] = []
    seen: set[str] = set()
    candidates = sorted(
        (row for row in rows if ss.is_user_prompt_row(row)),
        key=lambda row: (ss.row_field(row, "ts", 0) or 0, str(ss.row_field(row, "doc_id", ""))),
        reverse=True,
    )
    for row in candidates:
        message = ss.user_message_text(row)
        key = message.lower()
        if not message or key in seen:
            continue
        seen.add(key)
        messages.append(message)
        if len(messages) >= limit:
            break
    if not messages:
        message = ss.user_message_text(fallback)
        if message:
            messages.append(message)
    return messages


def is_close_session_message(text: str) -> bool:
    return classify_session_intent(ss.sanitize_text(text)).kind is IntentKind.CLOSE


def timestamped_session_title(title: str, timestamp: int | None) -> str:
    clean = re.sub(r"^\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+[—-]\s+", "", ss.sanitize_text(title))
    clean = clean or "Untitled session"
    if timestamp is None:
        return ss.compact(clean, 110)
    stamp = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).astimezone().strftime("%d/%m/%y %H:%M:%S")
    return ss.compact(f"{stamp} — {clean}", 110)


def best_repo(rows: list[sqlite3.Row], fallback: sqlite3.Row) -> str:
    candidates: list[str] = []
    for row in [fallback, *rows]:
        cwd = ss.sanitize_text(row["cwd"] or "")
        if cwd:
            candidates.append(cwd)
        meta = ss.row_meta(row)
        for key in ("cwd", "workspace", "workspaceFolder"):
            value = meta.get(key)
            if isinstance(value, str) and value:
                candidates.append(ss.sanitize_text(value))

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
    return (int(ss.row_field(row, "ts", 0) or 0), str(ss.row_field(row, "doc_id", "")))


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
        r"\bapplciation\b": "application",
        r"\bssession\b": "session",
        r"\bstqate\b": "state",
        r"\bworkign\b": "working",
        r"\bwereew\b": "were",
        r"\borkign\b": "working",
        r"\bfoudnit\b": "found it",
        r"\besle\b": "else",
        r"\beveyrthing\b": "everything",
        r"\btaht\b": "that",
        r"\bcotnext\b": "context",
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


def extract_task_clause(text: str) -> str:
    match = ss.TASK_CLAUSE_RE.search(text)
    if not match:
        return text
    clause = match.group(1).strip()
    stop = ss.TASK_STOP_RE.search(clause)
    if stop:
        clause = clause[: stop.start()].strip()
    return clause.rstrip(" .;") + ("." if clause and not clause.endswith((".", "?", "!")) else "")


def strip_card_filler(text: str) -> str:
    text = re.sub(r"^\[Image #\d+\]\s*", "", text)
    text = re.sub(r"^(?:ok|okay|yea|yeah|honestly|dude|bro|first honest to god)[,.\s]+", "", text, flags=re.I)
    return text.strip()


def clean_card_line(text: str, limit: int = 220) -> str:
    text = ss.sanitize_text(text)
    text = re.sub(r"^❯\s*", "", text)
    text = ss.extract_task_clause(text)
    text = ss.strip_card_filler(text)
    text = ss.normalize_card_typos(text)
    text = re.sub(r"\s+", " ", text).strip()
    return ss.compact(text, limit)


def first_card_sentence(text: str, minimum: int = 40) -> str:
    """First real sentence, ignoring boundaries that are only abbreviations."""
    for boundary in re.finditer(r"(?<=[.!?])\s", text):
        head = text[: boundary.start()].strip()
        if len(head) < minimum or ss.ABBREVIATION_END_RE.search(head):
            continue
        return head
    return text


def strip_prompt_echo(answer: str, instruction: str) -> str:
    """Drop leading sentences that only restate the instruction.

    Small models sometimes repeat the request before answering. Without this the
    first-sentence rule picks the instruction and the real summary is discarded.
    """
    clean = re.sub(r"\s+", " ", ss.sanitize_text(answer)).strip()
    if not clean:
        return ""
    instruction_words = set(re.findall(r"[a-z]+", instruction.lower()))
    sentences = [part for part in re.split(r"(?<=[.!?])\s+", clean) if part.strip()]
    for index, sentence in enumerate(sentences):
        words = re.findall(r"[a-z]+", sentence.lower())
        if not words:
            continue
        borrowed = sum(1 for word in words if word in instruction_words) / len(words)
        # A real summary names things the instruction never mentions, so total
        # vocabulary borrowing is what identifies an echo.
        if borrowed < 1.0:
            remainder = " ".join(sentences[index:]).strip()
            return remainder or clean
    return clean


def card_about_line(about: str, limit: int = 300) -> str:
    """Reduce an LLM About answer to one clean sentence with no ellipsis.

    quality_gate() discards any field that arrives ellipsis-truncated, so a long
    model answer must be cut at a sentence boundary here rather than mid-word.
    """
    clean = re.sub(r"\s+", " ", ss.sanitize_text(about)).strip()
    if not clean:
        return ""
    line = ss.clean_card_line(ss.first_card_sentence(clean), limit)
    if TRUNCATED_RE.search(line):
        line = TRUNCATED_RE.sub("", line).rstrip().rstrip(",;:-")
        if line and line[-1] not in ".!?":
            line += "."
    if not re.search(r"[A-Za-z0-9]", line):
        return ""
    return line


def meaningful_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in ss.sanitize_text(text).splitlines():
        line = line.strip()
        if not line or ss.NOISE_LINE_RE.search(line):
            continue
        if line.startswith("{") and line.endswith("}") and len(line) > 120:
            continue
        if len(ss.tokenize(line)) < 3 and len(line) < 24:
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
    if ss.TASK_CLAUSE_RE.search(stripped):
        score += 3.0
    if ss.GOAL_WORD_RE.search(stripped):
        score += 1.5
    if ss.ACTION_WORD_RE.search(stripped):
        score += 0.8
    if ss.PATH_RE.search(stripped):
        score += 0.4
    if ss.CONFIG_LINE_RE.search(stripped):
        score -= 2.5
    if ss.SUMMARY_TAG_RE.search(stripped):
        score -= 2.0
    if ss.LOW_SIGNAL_SESSION_LINE_RE.search(stripped):
        score -= 2.0
    if ss.is_question_like(stripped):
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
    score = ss.line_quality_score(line)
    for token in ss.query_anchor_terms(query):
        if ss.term_present(token, lower):
            score += ss.query_term_weight(token, set())
            if token in ss.ACTION_ANCHOR_TERMS:
                score += 1.0
    return score


def row_match_score(row: sqlite3.Row, query: str) -> float:
    haystack = f"{ss.row_field(row, 'title')}\n{ss.row_field(row, 'cwd')}\n{ss.row_field(row, 'text')}".lower()
    score = 0.0
    for token in ss.query_anchor_terms(query):
        if ss.term_present(token, haystack):
            score += ss.query_term_weight(token, set())
            if token in ss.ACTION_ANCHOR_TERMS:
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
    lines = ss.meaningful_lines(str(ss.row_field(row, "text", "")))
    if not lines:
        cleaned = ss.clean_card_line(str(ss.row_field(row, "text", "")))
        return "" if cleaned in avoid else cleaned
    if pattern:
        matches: list[tuple[float, int, str]] = []
        for i, line in enumerate(lines):
            if pattern.search(line) and (
                allow_questions or (not ss.is_question_like(line) and not ss.is_fragment_like(line))
            ):
                cleaned = ss.clean_card_line(line)
                if cleaned in avoid:
                    continue
                score = ss.query_line_score(line, query) if query else ss.line_quality_score(line)
                matches.append((score, -i, line))
        if matches:
            matches.sort(reverse=True)
            return ss.clean_card_line(matches[0][2])
        if avoid:
            return ""
        if not allow_questions:
            return ""
    if ss.query_anchor_terms(query):
        scored: list[tuple[float, int, str]] = []
        for i, line in enumerate(lines):
            scored.append((ss.query_line_score(line, query), -i, line))
        scored.sort(reverse=True)
        if scored and scored[0][0] > 0:
            return ss.clean_card_line(scored[0][2])
    lines.sort(key=lambda line: ss.line_quality_score(line), reverse=True)
    for line in lines:
        cleaned = ss.clean_card_line(line)
        if cleaned not in avoid:
            return cleaned
    return ""


def base_topic_score(row: sqlite3.Row) -> float:
    text = str(ss.row_field(row, "text", ""))
    title = str(ss.row_field(row, "title", ""))
    line = ss.best_line_for_row(row)
    combined = f"{title}\n{text}\n{line}"
    score = ss.line_quality_score(line)
    role = str(ss.row_field(row, "role", "")).lower()
    if role == "user":
        score += 1.0
    elif role == "session":
        score += 0.6
    if title and not ss.is_weak_title(title):
        score += 0.4
    if ss.LOW_SIGNAL_SESSION_LINE_RE.search(combined):
        score -= 2.0
    return score


def choose_base_topic_row(rows: list[sqlite3.Row], best_row: sqlite3.Row) -> sqlite3.Row:
    candidates = [best_row, *rows]
    candidates.sort(
        key=lambda row: (
            ss.base_topic_score(row),
            1 if str(ss.row_field(row, "role", "")).lower() in {"user", "session"} else 0,
            -ss.row_sort_key(row)[0],
        ),
        reverse=True,
    )
    return candidates[0]


def choose_topic_row(rows: list[sqlite3.Row], best_row: sqlite3.Row, query: str) -> sqlite3.Row:
    if not query:
        return ss.choose_base_topic_row(rows, best_row)
    candidates = [best_row, *rows]
    candidates.sort(
        key=lambda row: (
            ss.row_match_score(row, query),
            1 if str(ss.row_field(row, "role", "")).lower() == "user" else 0,
            ss.row_sort_key(row)[0],
        ),
        reverse=True,
    )
    if ss.meaningful_lines(str(ss.row_field(best_row, "text", ""))) and (
        not query or ss.row_match_score(best_row, query) >= ss.row_match_score(candidates[0], query)
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
    anchors = ss.query_anchor_terms(query)
    proper_terms = set(ss.proper_query_terms(query))
    bypass_doc_ids = line_match_bypass_doc_ids or set()
    for row in rows:
        doc_id = str(ss.row_field(row, "doc_id", ""))
        if doc_id in exclude_doc_ids:
            continue
        role = str(ss.row_field(row, "role", "")).lower()
        if preferred_roles and role not in preferred_roles:
            continue
        text = str(ss.row_field(row, "text", ""))
        if not pattern.search(text):
            continue
        best_line = ss.best_line_for_row(
            row,
            query=query,
            pattern=pattern,
            allow_questions=False,
            avoid_texts=avoid_texts,
        )
        if not best_line:
            continue
        haystack = f"{ss.row_field(row, 'title')}\n{ss.row_field(row, 'cwd')}\n{text}\n{best_line}".lower()
        matched = ss.matched_anchor_terms(anchors, haystack) if anchors else []
        line_matched = ss.matched_anchor_terms(anchors, best_line.lower()) if anchors else []
        if require_query_match and anchors and not matched:
            continue
        if require_line_query_match and anchors and not line_matched and doc_id not in bypass_doc_ids:
            continue
        query_score = ss.anchor_weight(matched, proper_terms) if anchors else 0.0
        matches.append(
            (
                query_score,
                ss.line_quality_score(best_line),
                1 if role in preferred_roles else 0,
                ss.row_sort_key(row)[0],
                str(ss.row_field(row, "doc_id", "")),
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
        for match in ss.PATH_RE.finditer(str(ss.row_field(row, "text", ""))):
            path = match.group(0).strip(".,;:)")
            if not path or path in seen or ss.noisy_path(path):
                continue
            seen.add(path)
            found.append(path)
            if len(found) >= limit:
                return tuple(found)
    return tuple(found)


def paths_from_evidence_rows(rows: list[sqlite3.Row], evidence: Iterable[EvidenceLine]) -> tuple[str, ...]:
    by_doc_id = {str(ss.row_field(row, "doc_id", "")): row for row in rows}
    evidence_rows = [by_doc_id[item.doc_id] for item in evidence if item.doc_id in by_doc_id]
    return ss.mentioned_paths(evidence_rows or rows)


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
    return ss.EvidenceLine(
        label=label,
        doc_id=str(ss.row_field(row, "doc_id", "")),
        role=str(ss.row_field(row, "role", "")),
        ts=ss.row_field(row, "ts", None),
        text=ss.best_line_for_row(
            row,
            query=query,
            pattern=pattern,
            allow_questions=allow_questions,
            avoid_texts=avoid_texts,
        ),
    )


def unique_evidence(items: Iterable[EvidenceLine]) -> tuple[EvidenceLine, ...]:
    out: list[ss.EvidenceLine] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item.doc_id, item.label)
        if key in seen or not item.text:
            continue
        seen.add(key)
        out.append(item)
    return tuple(out)


def build_session_card(rows: list[sqlite3.Row], best_row: sqlite3.Row, query: str, use_llm: bool = True) -> SessionCard:
    ordered = sorted(rows or [best_row], key=ss.row_sort_key)
    topic_row = ss.choose_topic_row(ordered, best_row, query)
    topic_evidence = ss.evidence_line("what this was", topic_row, query=query)
    excluded = {topic_evidence.doc_id}

    require_query_evidence = bool(ss.query_anchor_terms(query))
    happened_row = ss.choose_pattern_row(
        ordered,
        ss.ACTION_WORD_RE,
        {"assistant", "system"},
        excluded,
        query=query,
        require_query_match=require_query_evidence,
    )
    if happened_row is None:
        happened_row = ss.choose_pattern_row(
            ordered,
            ss.REPORTED_CHANGE_RE,
            {"user"},
            excluded,
            query=query,
            require_query_match=require_query_evidence,
        )
    happened_pattern = (
        ss.ACTION_WORD_RE
        if happened_row is not None and ss.ACTION_WORD_RE.search(str(ss.row_field(happened_row, "text", "")))
        else ss.REPORTED_CHANGE_RE
    )
    happened_evidence = (
        ss.evidence_line("what happened", happened_row, query=query, pattern=happened_pattern, allow_questions=False)
        if happened_row is not None
        else None
    )
    if happened_evidence is not None and not happened_evidence.text:
        happened_evidence = None
    if happened_evidence:
        excluded.add(happened_evidence.doc_id)

    next_excluded = set(excluded)
    next_excluded.discard(topic_evidence.doc_id)
    topic_key = ss.row_sort_key(topic_row)
    next_candidates = [row for row in ordered if ss.row_sort_key(row) >= topic_key]
    next_row = ss.choose_pattern_row(
        next_candidates,
        ss.NEXT_WORD_RE,
        {"assistant", "user"},
        next_excluded,
        query=query,
        require_query_match=require_query_evidence,
        require_line_query_match=require_query_evidence,
        line_match_bypass_doc_ids={topic_evidence.doc_id},
        avoid_texts={topic_evidence.text},
    )
    next_evidence = (
        ss.evidence_line("next clue", next_row, query=query, pattern=ss.NEXT_WORD_RE, avoid_texts={topic_evidence.text})
        if next_row is not None
        else None
    )
    if next_evidence is not None and next_evidence.text == topic_evidence.text:
        next_evidence = None

    evidence = ss.unique_evidence(
        item for item in (topic_evidence, happened_evidence, next_evidence) if item is not None
    )
    active_ts = ss.last_user_prompt_ts(ordered, best_row)
    # LLM synthesis for About / Next clue; regex evidence stays as fallback.
    # Skip LLM when use_llm=False (dashboard bulk display) to keep it fast.
    what_this_was = topic_evidence.text
    summary_source = ss.SUMMARY_SOURCE_EVIDENCE
    next_clue = next_evidence.text if next_evidence else ""
    session_text = "\n".join(
        str(ss.row_field(r, "text", "")) for r in ordered if str(ss.row_field(r, "text", "")).strip()
    )[:4000]
    # This is the only place an outbound payload is assembled, so redaction
    # happens here rather than at each prompt. Local storage keeps the original.
    session_text, _redacted = redact_secrets(session_text)
    if use_llm and session_text.strip():
        about = ss.llm_summarize(f"{ss.ABOUT_INSTRUCTION}{session_text}", max_tokens=140)
        about = ss.strip_prompt_echo(about, ss.ABOUT_INSTRUCTION)
        about_line = ss.card_about_line(about) if about else ""
        if about_line:
            what_this_was = about_line
            summary_source = ss.SUMMARY_SOURCE_LLM
        nxt = ss.strip_prompt_echo(
            ss.llm_summarize(f"{ss.NEXT_INSTRUCTION}{session_text}"), ss.NEXT_INSTRUCTION
        )
        if nxt:
            next_clue = ss.clean_card_line(nxt, 260)
    return ss.SessionCard(
        title=ss.timestamped_session_title(ss.best_session_title(ordered, topic_row), active_ts),
        source=str(ss.row_field(best_row, "source", "")),
        session_id=str(ss.row_field(best_row, "session_id", "")),
        repo=ss.best_repo(ordered, best_row),
        last_active=active_ts,
        last_user_message=ss.last_user_message(ordered, best_row),
        what_this_was=what_this_was,
        what_happened=happened_evidence.text if happened_evidence else "",
        next_clue=next_clue,
        mentioned_paths=ss.paths_from_evidence_rows(ordered, evidence),
        evidence=evidence,
        summary_source=summary_source,
    )


def session_card_hash(rows: list[sqlite3.Row]) -> str:
    parts: list[str] = [ss.CARD_VERSION]
    for row in sorted(rows, key=ss.row_sort_key):
        text_hash = ss.row_field(row, "text_hash", "") or ss.stable_hash(str(ss.row_field(row, "text", "")), 32)
        parts.append(
            "|".join(
                [
                    str(ss.row_field(row, "doc_id", "")),
                    str(ss.row_field(row, "ts", "")),
                    str(text_hash),
                ]
            )
        )
    return ss.stable_hash("\n".join(parts), 32)


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
    out: list[ss.EvidenceLine] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        out.append(
            ss.EvidenceLine(
                label=ss.sanitize_text(item.get("label", "")),
                doc_id=ss.sanitize_text(item.get("doc_id", "")),
                role=ss.sanitize_text(item.get("role", "")),
                ts=ss.parse_ts(item.get("ts")),
                text=ss.sanitize_text(item.get("text", "")),
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
    return ss.SessionCard(
        title=str(row["title"] or ""),
        source=str(row["source"] or ""),
        session_id=str(row["session_id"] or ""),
        repo=str(row["repo"] or ""),
        last_active=row["last_active"],
        last_user_message="",
        what_this_was=str(row["what_this_was"] or ""),
        what_happened=str(row["what_happened"] or ""),
        next_clue=str(row["next_clue"] or ""),
        mentioned_paths=tuple(ss.sanitize_text(path) for path in paths if ss.sanitize_text(path)),
        evidence=ss.evidence_from_json(str(row["evidence_json"] or "[]")),
        summary_source=str(row["summary_source"] or ss.SUMMARY_SOURCE_EVIDENCE),
    )


def store_session_card(conn: sqlite3.Connection, card: SessionCard, text_hash: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO session_cards (
            source, session_id, text_hash, title, repo, last_active,
            what_this_was, what_happened, next_clue, mentioned_paths_json,
            evidence_json, summary_source, built_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ss.evidence_to_json(card.evidence),
            card.summary_source,
            ss.now_ts(),
        ),
    )


def cached_card_is_current(cached: sqlite3.Row | None) -> bool:
    """A cached card is reusable unless its summary is a repairable fallback.

    An evidence card was built while the model was unreachable. It is stale as
    soon as a key exists, so one outage cannot freeze a summary forever.
    """
    if cached is None:
        return False
    if str(cached["summary_source"] or ss.SUMMARY_SOURCE_EVIDENCE) == ss.SUMMARY_SOURCE_LLM:
        return True
    return not ss.llm_available()


def cached_base_session_card(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    best_row: sqlite3.Row,
    persist: bool = False,
    use_llm: bool = True,
) -> SessionCard:
    text_hash = ss.session_card_hash(rows or [best_row])
    cached = conn.execute(
        """
        SELECT *
        FROM session_cards
        WHERE source = ? AND session_id = ? AND text_hash = ?
        """,
        (best_row["source"], best_row["session_id"], text_hash),
    ).fetchone()
    if ss.cached_card_is_current(cached):
        return dataclasses.replace(
            ss.card_from_cache_row(cached),
            last_user_message=ss.last_user_message(rows, best_row),
        )

    card = ss.build_session_card(rows, best_row, "", use_llm=use_llm)
    if persist:
        ss.store_session_card(conn, card, text_hash)
        conn.commit()
    return card


def merge_query_card(base: SessionCard, query_card: SessionCard, query: str) -> SessionCard:
    """Overlay query-specific evidence on the cached card.

    The model prompts never contain the query, so a query card can only repeat
    the base card's summary at full price. The summary is therefore taken from
    the base card whenever the base card has a real one.
    """
    if not query:
        return base
    keep_base_summary = base.summary_source == ss.SUMMARY_SOURCE_LLM and bool(base.what_this_was)
    return ss.SessionCard(
        title=query_card.title or base.title,
        source=base.source,
        session_id=base.session_id,
        repo=query_card.repo or base.repo,
        last_active=query_card.last_active or base.last_active,
        last_user_message=query_card.last_user_message or base.last_user_message,
        what_this_was=(
            base.what_this_was if keep_base_summary
            else (query_card.what_this_was or base.what_this_was)
        ),
        what_happened=query_card.what_happened,
        next_clue=base.next_clue if keep_base_summary else query_card.next_clue,
        mentioned_paths=query_card.mentioned_paths or base.mentioned_paths,
        evidence=query_card.evidence or base.evidence,
        summary_source=base.summary_source if keep_base_summary else query_card.summary_source,
    )


def session_card_for_result(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    query: str = "",
    persist: bool = False,
    use_llm: bool = True,
) -> SessionCard:
    rows = ss.session_rows(conn, row)
    base = ss.cached_base_session_card(conn, rows, row, persist=persist, use_llm=use_llm)
    if not query:
        return base
    # The query card only re-picks evidence lines. Asking the model again would
    # send an identical prompt and pay twice for the same sentence.
    query_card = ss.build_session_card(rows, row, query, use_llm=False)
    return ss.merge_query_card(base, query_card, query)


def ensure_session_cards(
    conn: sqlite3.Connection,
    limit: int | None = None,
    quiet: bool = True,
    horizon: int | None = None,
) -> int:
    """Build missing cards for the newest sessions.

    `limit` caps this run. `horizon` caps how far back routine backfill is
    willing to summarize at all; older sessions are summarized on demand when a
    search surfaces them. It is a parameter rather than a module lookup so a
    test can set it without patching global state.
    """
    count = 0
    if horizon is None:
        horizon = ss.CARD_BACKFILL_SESSIONS
    for source, session_id in ss.session_groups(conn, limit=horizon):
        row = ss.representative_session_row(conn, source, session_id)
        if row is None:
            continue
        rows = ss.rows_for_session(conn, source, session_id)
        text_hash = ss.session_card_hash(rows)
        existing = conn.execute(
            """
            SELECT summary_source
            FROM session_cards
            WHERE source = ? AND session_id = ? AND text_hash = ?
            """,
            (source, session_id, text_hash),
        ).fetchone()
        if ss.cached_card_is_current(existing):
            continue
        if not ss.llm_available():
            ss.warn_llm_unavailable()
        card = ss.build_session_card(rows, row, "")
        # One card per transaction, and the lock is taken only for the
        # write. An interrupt during the next model call keeps this card.
        with ss.session_lock(shared=False):
            with conn:
                ss.store_session_card(conn, card, text_hash)
        count += 1
        if limit is not None and count >= limit:
            break
    if not quiet:
        if count:
            print(f"Built {count} session cards.")
        else:
            print("Session cards are already up to date.")
    return count
