"""Retrieval: FTS and semantic search, ranking, embeddings, and query intent.

Split from session_search.py. Project symbols resolve through the
session_search facade at call time (``ss.<name>``) so tests that patch or
mutate facade attributes keep intercepting the behavior of this module.
"""

from __future__ import annotations

import functools
import math
import re
import sqlite3
from collections import Counter
from typing import Any

import session_search as ss


def fts_query(query: str) -> str:
    tokens = ss.expand_query_tokens(ss.tokenize(query))
    if not tokens:
        return ss.quote_fts(query)
    return " OR ".join(f"{ss.quote_fts(t)}*" for t in tokens[:12])


def quote_fts(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def tokenize(text: str) -> list[str]:
    return [ss.normalize_token(m.group(0)) for m in ss.TOKEN_RE.finditer(text.lower()) if len(m.group(0)) >= 2]


def expand_query_tokens(tokens: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        for expanded in (token, *ss.ALIASES.get(token, ())):
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
    tokens = ss.expand_query_tokens(ss.tokenize(text))
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
    return ss.unique_join(
        [
            str(row["title"] or ""),
            str(row["cwd"] or ""),
            str(row["text"] or "")[:ss.EMBED_TEXT_CHARS],
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
        return TextEmbedding(model_name=ss.EMBED_MODEL, cache_dir=str(ss.expand(ss.DEFAULT_MODEL_CACHE)))
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

    vector = ss.vector_from_blob(blob)
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
    model = ss.embedding_backend()
    if model is None:
        if not quiet:
            print("Semantic search unavailable: local fastembed dependency is not installed.")
        return 0

    limit_clause = "LIMIT ?" if limit is not None else ""
    params: list[Any] = [ss.EMBED_MODEL_VERSION]
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

    texts = [ss.semantic_text(row) for row in rows]
    count = 0
    embedded_at = ss.now_ts()
    with conn:
        for row, vector in zip(rows, model.embed(texts), strict=False):
            vector_blob = ss.normalize_embedding(vector)
            dim = len(vector_blob) // 4
            conn.execute(
                """
                INSERT OR REPLACE INTO embeddings (
                    doc_id, model, dim, vector, text_hash, embedded_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row["doc_id"], ss.EMBED_MODEL_VERSION, dim, vector_blob, row["text_hash"], embedded_at),
            )
            count += 1
    if not quiet:
        print(f"Embedded {count} documents with {ss.EMBED_MODEL}")
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
    titles = [str(ss.row_field(row, "title", "")) for row in rows if not ss.is_weak_title(str(ss.row_field(row, "title", "")))]
    cwds = [str(ss.row_field(row, "cwd", "")) for row in rows if ss.row_field(row, "cwd", "")]
    lines: list[str] = []
    for row in rows[:6]:
        line = ss.best_line_for_row(row)
        if line:
            lines.append(line)
    for row in rows[-10:]:
        line = ss.best_line_for_row(row)
        if line:
            lines.append(line)
    paths = list(ss.mentioned_paths(rows, limit=8))
    return ss.unique_join([*titles[:4], *cwds[:2], *lines, *paths], sep="\n")[:ss.EMBED_TEXT_CHARS]


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
    model = ss.embedding_backend()
    if model is None:
        if not quiet:
            print("Semantic search unavailable: local model is not installed or cached.")
        return 0

    pending: list[tuple[str, str, str, str]] = []
    for source, session_id in ss.session_groups(conn, limit=ss.CARD_BACKFILL_SESSIONS):
        rows = ss.rows_for_session(conn, source, session_id)
        text = ss.session_embedding_text(rows)
        if not text:
            continue
        text_hash = ss.stable_hash(text, 32)
        existing = conn.execute(
            """
            SELECT 1
            FROM session_embeddings
            WHERE source = ? AND session_id = ? AND model = ? AND text_hash = ?
            """,
            (source, session_id, ss.EMBED_MODEL_VERSION, text_hash),
        ).fetchone()
        if existing:
            continue
        pending.append((source, session_id, text, text_hash))
        if limit is not None and len(pending) >= limit:
            break

    if not pending:
        return 0

    embedded_at = ss.now_ts()
    texts = [item[2] for item in pending]
    count = 0
    with conn:
        for (source, session_id, _text, text_hash), vector in zip(pending, model.embed(texts), strict=False):
            vector_blob = ss.normalize_embedding(vector)
            dim = len(vector_blob) // 4
            conn.execute(
                """
                INSERT OR REPLACE INTO session_embeddings (
                    source, session_id, model, dim, vector, text_hash, embedded_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (source, session_id, ss.EMBED_MODEL_VERSION, dim, vector_blob, text_hash, embedded_at),
            )
            count += 1
    if not quiet:
        print(f"Embedded {count} sessions with {ss.EMBED_MODEL}")
    return count


def search_fts(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    source: str | None,
    since: int | None = None,
    until: int | None = None,
) -> list[sqlite3.Row]:
    match = ss.fts_query(query)
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
    qv = ss.similarity_vector(query)
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
        score = ss.cosine(qv, ss.similarity_vector(text))
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
    model = ss.embedding_backend()
    if model is None:
        return []
    ss.ensure_session_embeddings(conn, quiet=True)
    query_vector = ss.embedding_query_vector(model, query)
    clauses = ["e.model = ?"]
    params: list[Any] = [ss.EMBED_MODEL_VERSION]
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
            "session_score": max(0.0, ss.dot_blob(row["embedding_vector"], query_vector)),
            "document_score": 0.0,
            "row": None,
        }

    doc_clauses = ["e.model = ?"]
    doc_params: list[Any] = [ss.EMBED_MODEL_VERSION]
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
        score = max(0.0, ss.dot_blob(row["embedding_vector"], query_vector))
        if score > entry["document_score"]:
            entry["document_score"] = score
            entry["row"] = row

    scored: list[tuple[sqlite3.Row, float]] = []
    for (row_source, session_id), entry in by_session.items():
        doc_row = entry["row"] or ss.representative_session_row(conn, row_source, session_id)
        if doc_row is None:
            continue
        activity_row = ss.representative_session_row(conn, row_source, session_id)
        activity_ts = activity_row["ts"] if activity_row is not None else doc_row["ts"]
        if since is not None and (activity_ts is None or activity_ts < since):
            continue
        if until is not None and (activity_ts is None or activity_ts >= until):
            continue
        score = ss.semantic_score(entry["session_score"], entry["document_score"])
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
        ("pi", r"^\s*pi\b"),
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
        ("pi", r"\b(?:in|from|only)\s+pi\b|\bpi\s+session\b"),
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
        since, until = ss.local_day_bounds(0)
        cleaned = re.sub(r"\btoday\b", " ", cleaned, flags=re.I)
    elif re.search(r"\byesterday\b", cleaned, flags=re.I):
        since, until = ss.local_day_bounds(1)
        cleaned = re.sub(r"\byesterday\b", " ", cleaned, flags=re.I)
    elif re.search(r"\b(?:recent|recently|last\s+few\s+days)\b", cleaned, flags=re.I):
        since, until = ss.recent_bounds(7)
        cleaned = re.sub(r"\b(?:recent|recently|last\s+few\s+days)\b", " ", cleaned, flags=re.I)
    elif re.search(r"\b(?:this\s+week|last\s+7\s+days)\b", cleaned, flags=re.I):
        since, until = ss.recent_bounds(7)
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
            score >= best_score - 0.2 and ss.row_context_quality(row) > ss.row_context_quality(best_row)
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
    return bool(ss.META_QUERY_RE.search(query))


def is_meta_session(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    rows = ss.session_rows(conn, row)
    sample = "\n".join(
        ss.compact(f"{item['title']}\n{item['text']}", 1000)
        for item in rows[:20]
    )
    return bool(ss.META_SESSION_RE.search(sample))


def session_text_sample(conn: sqlite3.Connection, row: sqlite3.Row, max_rows: int = 24) -> str:
    rows = ss.session_rows(conn, row)
    return "\n".join(
        ss.compact(f"{item['title']}\n{item['cwd']}\n{item['text']}", 1200)
        for item in rows[:max_rows]
    ).lower()


def full_session_text(rows: list[sqlite3.Row]) -> str:
    return "\n".join(
        ss.compact(
            f"{ss.row_field(item, 'title')}\n{ss.row_field(item, 'cwd')}\n{ss.row_field(item, 'text')}",
            1200,
        )
        for item in rows
    ).lower()


def query_anchor_terms(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for match in ss.TOKEN_RE.finditer(query):
        term = ss.normalize_token(match.group(0))
        if not term or term in seen or term in ss.STOP_WORDS:
            continue
        if len(term) < 3 and term not in ss.IMPORTANT_SHORT_TERMS:
            continue
        seen.add(term)
        terms.append(term)
    return terms


def term_variants(term: str) -> tuple[str, ...]:
    variants: list[str] = []
    seen: set[str] = set()
    for value in (term, *ss.ALIASES.get(term, ())):
        normalized = ss.normalize_token(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            variants.append(normalized)
    return tuple(variants)


def term_present(term: str, text: str) -> bool:
    return any(variant in text for variant in ss.term_variants(term))


def matched_anchor_terms(terms: list[str], text: str) -> list[str]:
    return [term for term in terms if ss.term_present(term, text)]


def query_term_weight(term: str, proper_terms: set[str]) -> float:
    weight = 1.0
    if term in proper_terms:
        weight += 0.8
    if term in ss.IMPORTANT_SHORT_TERMS:
        weight += 0.5
    if term in ss.ACTION_ANCHOR_TERMS:
        weight += 0.4
    if len(term) >= 7:
        weight += 0.2
    return weight


def anchor_weight(terms: list[str], proper_terms: set[str]) -> float:
    return sum(ss.query_term_weight(term, proper_terms) for term in terms)


def is_side_task_query(query: str) -> bool:
    return bool(ss.SIDE_TASK_QUERY_RE.search(query))


def is_side_task_session(rows: list[sqlite3.Row], fallback: sqlite3.Row) -> bool:
    sample = "\n".join(
        ss.compact(f"{ss.row_field(item, 'title')}\n{ss.row_field(item, 'text')}", 1000)
        for item in [fallback, *rows[:12]]
    )
    return bool(ss.SIDE_TASK_SESSION_RE.search(sample))


def is_corpus_dump_session(rows: list[sqlite3.Row], fallback: sqlite3.Row) -> bool:
    sample = "\n".join(
        ss.compact(f"{ss.row_field(item, 'title')}\n{ss.row_field(item, 'text')}", 1800)
        for item in [fallback, *rows[:20]]
    )
    return bool(ss.CORPUS_DUMP_RE.search(sample))


def is_corpus_dump_card(rows: list[sqlite3.Row], fallback: sqlite3.Row, query: str) -> bool:
    card = ss.build_session_card(rows, fallback, query)
    sample = "\n".join(
        [card.title, card.last_user_message, card.what_this_was, card.what_happened, card.next_clue]
    )
    return bool(ss.CORPUS_DUMP_RE.search(sample))


def proper_query_terms(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for match in ss.TOKEN_RE.finditer(query):
        raw = match.group(0)
        if not raw[:1].isupper():
            continue
        token = ss.normalize_token(raw)
        if (len(token) < 3 and token not in ss.IMPORTANT_SHORT_TERMS) or token in ss.STOP_WORDS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def exact_literal_query(query: str) -> str:
    cleaned = ss.sanitize_text(query).strip().lower()
    if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", cleaned):
        return cleaned
    if re.fullmatch(r"https?://\S+", cleaned):
        return cleaned
    return ""


def recency_boost(timestamp: int | None, current_time: int | None = None) -> float:
    """Return a bounded tie-breaking boost for recent user activity."""
    if timestamp is None:
        return 0.0
    now = int(current_time if current_time is not None else ss.now_ts())
    age_seconds = max(0, now - int(timestamp))
    age_days = age_seconds / 86_400
    return 0.35 * math.exp(-age_days / 21.0)


def rerank_session_results(
    conn: sqlite3.Connection,
    results: list[tuple[sqlite3.Row, float, str]],
    query: str,
    limit: int,
) -> list[tuple[sqlite3.Row, float, str]]:
    proper_terms = set(ss.proper_query_terms(query))
    anchor_terms = ss.query_anchor_terms(query)
    total_anchor_weight = ss.anchor_weight(anchor_terms, proper_terms)
    meta_query = ss.is_meta_query(query)
    literal = ss.exact_literal_query(query)
    exact_session_keys: set[tuple[str, str]] = set()
    if literal:
        for candidate, _score, _label in results:
            candidate_rows = ss.session_rows(conn, candidate)
            if literal in ss.full_session_text(candidate_rows).lower():
                exact_session_keys.add((str(candidate["source"]), str(candidate["session_id"])))
    reranked: list[tuple[sqlite3.Row, float, str, int | None]] = []
    for row, score, label in results:
        if exact_session_keys and (str(row["source"]), str(row["session_id"])) not in exact_session_keys:
            continue
        rows = ss.session_rows(conn, row)
        adjusted = score
        anchor_coverage = 1.0
        card_coverage = 1.0
        action_misses: list[str] = []
        meta_session = ss.is_meta_session(conn, row)
        if meta_session and not meta_query:
            continue
        penalty = 0.0 if meta_query else (10.0 if meta_session else 0.0)
        if meta_query and meta_session:
            adjusted += 2.5
        if proper_terms:
            sample = ss.full_session_text(rows)
            missing_proper_terms = [term for term in proper_terms if not ss.term_present(term, sample)]
            penalty += 2.5 * len(missing_proper_terms)
            card = ss.build_session_card(rows, row, query)
            card_text = "\n".join(
                [
                    card.title,
                    card.what_this_was,
                    card.what_happened,
                    card.next_clue,
                    " ".join(item.text for item in card.evidence),
                ]
            ).lower()
            weak_card_terms = [term for term in proper_terms if not ss.term_present(term, card_text)]
            penalty += 1.5 * len(weak_card_terms)

        if anchor_terms and total_anchor_weight:
            sample = ss.full_session_text(rows)
            card = ss.build_session_card(rows, row, query)
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
            matched_session = ss.matched_anchor_terms(anchor_terms, sample)
            matched_card = ss.matched_anchor_terms(anchor_terms, card_text)
            session_weight = ss.anchor_weight(matched_session, proper_terms)
            card_weight = ss.anchor_weight(matched_card, proper_terms)
            missing_terms = [term for term in anchor_terms if term not in matched_session]
            missing_weight = ss.anchor_weight(missing_terms, proper_terms)
            anchor_coverage = session_weight / total_anchor_weight
            card_coverage = card_weight / total_anchor_weight
            adjusted += 0.75 * (session_weight / total_anchor_weight)
            adjusted += 0.45 * (card_weight / total_anchor_weight)
            adjusted -= 0.22 * missing_weight
            action_misses = [term for term in missing_terms if term in ss.ACTION_ANCHOR_TERMS]
            adjusted -= 0.35 * len(action_misses)

        if not ss.is_side_task_query(query) and ss.is_side_task_session(rows, row):
            continue
        if not ss.CORPUS_DUMP_QUERY_RE.search(query):
            if ss.is_corpus_dump_session(rows, row) or ss.is_corpus_dump_card(rows, row, query):
                continue

        enough_query_match = not anchor_terms or anchor_coverage >= 0.45
        if not meta_query and len(anchor_terms) >= 3 and card_coverage < 0.6:
            enough_query_match = False
        action_terms = [term for term in anchor_terms if term in ss.ACTION_ANCHOR_TERMS]
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
            activity_ts = ss.last_user_prompt_ts(rows, row)
            combined_score = adjusted - penalty + ss.recency_boost(activity_ts)
            reranked.append((row, combined_score, label, activity_ts))
    reranked.sort(
        key=lambda item: (
            item[1],
            item[3] or 0,
            ss.row_field(item[0], "ts", 0) or 0,
            str(ss.row_field(item[0], "doc_id", "")),
        ),
        reverse=True,
    )
    return [(row, score, label) for row, score, label, _activity_ts in reranked[:limit]]


def row_context_quality(row: sqlite3.Row) -> int:
    quality = 0
    if ss.sanitize_text(row["cwd"] or ""):
        quality += 3
    title = ss.sanitize_text(row["title"] or "")
    if title and not ss.is_weak_title(title):
        quality += 2
    path = ss.sanitize_text(row["path"] or "")
    if path and not path.endswith(("history.jsonl", "state_5.sqlite")):
        quality += 1
    return quality
