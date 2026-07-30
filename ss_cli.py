"""Command layer: subcommand implementations, argparse surfaces, and natural-language command parsing.

Split from session_search.py. Project symbols resolve through the
session_search facade at call time (``ss.<name>``) so tests that patch or
mutate facade attributes keep intercepting the behavior of this module.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
from typing import Any
from archive_intent import PARSER_VERSION as ARCHIVE_INTENT_VERSION
from archive_store import immediate_transaction, quick_check
from adapter_capabilities import render_capabilities

import session_search as ss


def cmd_dashboard(args: argparse.Namespace) -> int:
    with ss.session_lock(shared=False):
        db_path = ss.expand(args.db)
        if not db_path.exists():
            print(f"Index not found: {db_path}", file=sys.stderr)
            return 2
        conn = ss.connect_db(db_path)
        ss.init_db(conn)
        if not getattr(args, "no_refresh", False):
            ss.refresh_dashboard_index(conn, ss.expand(args.home))
        archived_view = bool(getattr(args, "archived", False))
        missing_archived = 0
        if archived_view:
            results, missing_archived = ss.archived_session_results(conn, args.limit, args.source)
            project_summaries = None
        else:
            # Exact limit for the thread list and open numbers.
            results = ss.recent_session_results(conn, args.limit, args.source)
            # Wider project scan so older folders still appear after a restart.
            project_summaries = ss.dashboard_project_summaries(
                conn,
                source_name=getattr(args, "source", "all") or "all",
                thread_limit=args.limit,
            )
        ss.save_last_results(results, "recent sessions dashboard", db_path)
        ss.print_dashboard(
            conn,
            results,
            archived_view=archived_view,
            project_summaries=project_summaries,
        )
        if missing_archived:
            print(f"{missing_archived} archived session(s) are missing from the index. Run: ss fresh archived")
        conn.close()
    return ss.dashboard_prompt(args)


def cmd_archived(args: argparse.Namespace) -> int:
    args.archived = True
    args.no_refresh = getattr(args, "no_refresh", False)
    args.home = getattr(args, "home", "~")
    return ss.cmd_dashboard(args)


def cmd_archive_audit(args: argparse.Namespace) -> int:
    with ss.session_lock(shared=True):
        db_path = ss.expand(args.db)
        if not db_path.exists():
            print(f"Index not found: {db_path}", file=sys.stderr)
            return 2
        conn = ss.connect_db(db_path)
        ss.init_db(conn)
        quick_check(conn)
        proposals, unresolved = ss.archive_migration_audit(conn)
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
    with ss.session_lock(shared=False):
        db_path, conn = ss.connect_selector_db(args.selector, args.db)
        if conn is None:
            ss.print_selector_db_error(db_path)
            return 2
        row = ss.selected_row(conn, args.selector)
        if row is None:
            print(f"Not found: {args.selector}", file=sys.stderr)
            return 2
        archived = bool(args.archived)
        try:
            with immediate_transaction(conn):
                ss.set_session_archive_status(
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
        card = ss.session_card_for_result(conn, row, ss.query_from_selector(args.selector))
        print(f"{'Archived' if archived else 'Unarchived'}: {card.title}")
        print(f"Session: {ss.session_ref(str(row['source']), str(row['session_id']))}")
        conn.close()
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    with ss.session_lock(shared=False):
        db_path = ss.expand(args.db)
        home = ss.expand(args.home)
        conn = ss.connect_db(db_path)
        try:
            if args.reset:
                ss.reset_db(conn)
            else:
                ss.init_db(conn)
            sources = ss.normalize_sources(args.source)
            count = ss.upsert_documents(conn, ss.build_docs(home, sources))
            ss.sync_detected_archive_states(conn)
            if not args.quiet:
                print(f"Indexed {count} documents into {db_path}")
        finally:
            conn.close()
    return 0


def run_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    source_name: str = "all",
    mode: str = "hybrid",
) -> tuple[str, list[tuple[sqlite3.Row, float, str]]]:
    source_name, cleaned_query = ss.infer_source_from_query(query, source_name)
    cleaned_query, since, until, activity = ss.infer_time_and_intent(cleaned_query)
    source = None if source_name == "all" else source_name
    search_limit = max(limit * 4, 25)

    if activity and not cleaned_query:
        results = ss.group_results_by_session(
            ss.search_recent(conn, search_limit, source, since, until),
            search_limit,
        )
        return query, ss.rerank_session_results(conn, results, query, limit)

    fts_rows: list[sqlite3.Row] = []
    local_rows: list[tuple[sqlite3.Row, float]] = []
    semantic_rows: list[tuple[sqlite3.Row, float]] = []
    if mode in {"fts", "hybrid"}:
        fts_rows = ss.search_fts(conn, cleaned_query, search_limit, source, since, until)
    if mode in {"local", "hybrid"}:
        local_rows = ss.search_local(conn, cleaned_query, search_limit, source, since, until)
    if mode == "hybrid":
        semantic_rows = ss.search_semantic(conn, cleaned_query, search_limit, source, since, until)

    if mode == "fts":
        results = [
            (row, 1.0 - (i * 0.05), "fts")
            for i, row in enumerate(fts_rows[:search_limit])
        ]
    elif mode == "local":
        results = [(row, score, "local") for row, score in local_rows[:search_limit]]
    else:
        results = ss.merge_results(fts_rows, local_rows, semantic_rows, search_limit)
    results = ss.group_results_by_session(results, search_limit)
    return cleaned_query, ss.rerank_session_results(conn, results, cleaned_query, limit)


def cmd_search(args: argparse.Namespace) -> int:
    with ss.session_lock(shared=False):
        db_path = ss.expand(args.db)
        if not db_path.exists():
            print(f"Index not found: {db_path}", file=sys.stderr)
            print("Run: python3 session_search.py index --reset", file=sys.stderr)
            return 2
        conn = ss.connect_db(db_path)
        try:
            ss.init_db(conn)
            if not getattr(args, "no_refresh", False):
                ss.refresh_dashboard_index(conn, ss.expand(getattr(args, "home", "~")))
            else:
                ss.sync_detected_archive_states(conn)
            display_query, results = ss.run_search(conn, args.query, args.limit, args.source, args.mode)
            ss.save_last_results(results, display_query, db_path)
            ss.print_results(conn, results, display_query)
        finally:
            conn.close()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with ss.session_lock(shared=True):
        db_path, conn = ss.connect_selector_db(args.doc_id, args.db)
        if conn is None:
            ss.print_selector_db_error(db_path)
            return 2
        row = ss.selected_row(conn, args.doc_id)
        if row is None:
            print(f"Not found: {args.doc_id}", file=sys.stderr)
            return 2
        query = ss.query_from_selector(args.doc_id)
        card = ss.session_card_for_result(conn, row, query)
        ss.print_session_card_detail(
            card,
            ss.session_is_archived(conn, str(row["source"]), str(row["session_id"])),
        )
        if query:
            print(f"Search query: {query}")
            print()
        print(f"[{row['source']}] {ss.iso_date(row['ts'])} {row['title']}")
        print(f"id: {row['doc_id']}")
        print(f"path: {row['path']}")
        if row["cwd"]:
            print(f"cwd: {row['cwd']}")
        print(f"role: {row['role']}")
        print()
        print(row["text"])
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    with ss.session_lock(shared=False):
        db_path, conn = ss.connect_selector_db(args.selector, args.db)
        if conn is None:
            ss.print_selector_db_error(db_path)
            return 2
        row = ss.selected_row(conn, args.selector)
        if row is None:
            conn.close()
            print(f"Not found: {args.selector}", file=sys.stderr)
            return 2
        ss.mark_cli_resume(conn, row, args.selector, "cli-open")
        ss.print_resume_instructions(conn, row, args.selector, ss.query_from_selector(args.selector))
        conn.close()
    return 0


def cmd_continue(args: argparse.Namespace) -> int:
    target = ""
    if getattr(args, "target", ""):
        try:
            target = ss.normalize_target(args.target)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    with ss.session_lock(shared=False):
        db_path, conn = ss.connect_selector_db(args.selector, args.db)
        if conn is None:
            ss.print_selector_db_error(db_path)
            return 2
        row = ss.selected_row(conn, args.selector)
        if row is None:
            conn.close()
            print(f"Not found: {args.selector}", file=sys.stderr)
            return 2
        ss.mark_cli_resume(conn, row, args.selector, "cli-continue")
        if not target or (target == row["source"] and ss.native_resume_available(row["source"], row["session_id"])):
            ss.print_resume_instructions(conn, row, args.selector, ss.query_from_selector(args.selector))
            conn.close()
            return 0
        conn.close()

    return ss.cmd_handoff(argparse.Namespace(db=args.db, selector=args.selector, target=target))


def cmd_handoff(args: argparse.Namespace) -> int:
    try:
        target = ss.normalize_target(args.target)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    with ss.session_lock(shared=True):
        db_path, conn = ss.connect_selector_db(args.selector, args.db)
        if conn is None:
            ss.print_selector_db_error(db_path)
            return 2
        try:
            row = ss.selected_row(conn, args.selector)
            if row is None:
                print(f"Not found: {args.selector}", file=sys.stderr)
                return 2

            query = ss.query_from_selector(args.selector)

            rows = ss.session_rows(conn, row)
            packet_rows = ss.handoff_rows(rows, row)
            repo = ss.best_repo(packet_rows, row)
            if target == row["source"] and ss.native_resume_available(row["source"], row["session_id"]):
                print("That is the native owner. Open the exact session instead:")
                for line in ss.native_resume_lines(
                    row["source"],
                    row["session_id"],
                    repo,
                    path=str(row["path"] or ""),
                ):
                    print(f"  {line}")
                return 0

            packet_path = ss.handoff_path(row, target)
            packet_path.parent.mkdir(parents=True, exist_ok=True)
            card = ss.session_card_for_result(conn, row, query)
            packet_path.write_text(
                ss.handoff_packet_text(
                    row,
                    target,
                    query,
                    args.selector,
                    packet_rows,
                    card,
                    archived=ss.session_is_archived(conn, str(row["source"]), str(row["session_id"])),
                ),
                encoding="utf-8",
            )

            print(f"Context packet: {packet_path}")
            print(f"Original owner: {ss.source_label(row['source'])}")
            print(f"Original session: {ss.session_ref(row['source'], row['session_id'])}")
            print(f"Target harness: {ss.target_label(target)}")
            print(f"Repo: {ss.repo_label(repo)}")
            print()
            print(f"Continue in {ss.target_label(target)}:")
            for line in ss.launch_lines_for_handoff(target, repo, packet_path):
                print(f"  {line}")
        finally:
            conn.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with ss.session_lock(shared=True):
        db_path = ss.expand(args.db)
        if not db_path.exists():
            print(f"Index not found: {db_path}")
            return 0
        conn = ss.connect_db(db_path)
        try:
            ss.init_db(conn)
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
                print(f"{row['source']}: {row['n']} newest={ss.iso_date(row['newest'])}")
            if ss.embedding_backend() is None:
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
                    (ss.EMBED_MODEL_VERSION,),
                ).fetchone()[0]
                total_sessions = conn.execute(
                    "SELECT count(*) FROM (SELECT 1 FROM documents GROUP BY source, session_id)"
                ).fetchone()[0]
                print(f"semantic: {embedded_sessions}/{total_sessions} sessions embedded with {ss.EMBED_MODEL}")
                embedded_turns = conn.execute(
                    "SELECT count(*) FROM embeddings WHERE model = ?",
                    (ss.EMBED_MODEL_VERSION,),
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
        finally:
            conn.close()
    return 0


def cmd_capabilities(_args: argparse.Namespace) -> int:
    print(render_capabilities())
    return 0


def cmd_demo(_args: argparse.Namespace) -> int:
    from demo_runner import run_demo

    print(run_demo())
    return 0


def cmd_cards(args: argparse.Namespace) -> int:
    db_path = ss.expand(args.db)
    if not db_path.exists():
        print(f"Index not found: {db_path}", file=sys.stderr)
        return 2
    with ss.session_lock(shared=False):
        conn = ss.connect_db(db_path)
        ss.init_db(conn)
    # Building is network bound and can run for minutes. Holding the exclusive
    # lock across it made every other ss command fail with a lock timeout.
    ss.ensure_session_cards(conn, limit=args.limit, quiet=False)
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    with ss.session_lock(shared=False):
        db_path = ss.expand(args.db)
        if not db_path.exists():
            print(f"Index not found: {db_path}", file=sys.stderr)
            return 2
        conn = ss.connect_db(db_path)
        ss.init_db(conn)
        session_count = ss.ensure_session_embeddings(conn, limit=args.limit, quiet=False)
        document_count = ss.ensure_embeddings(conn, limit=args.limit, quiet=False)
        if session_count == 0 and document_count == 0 and ss.embedding_backend() is not None:
            print("Semantic index is already up to date.")
    return 0


def eval_text_for_result(conn: sqlite3.Connection, row: sqlite3.Row, query: str, label: str = "") -> str:
    rows = ss.session_rows(conn, row)
    card = ss.session_card_for_result(conn, row, query)
    parts = [
        str(row["source"]),
        ss.source_label(str(row["source"])),
        ss.owner_action_label(str(row["source"]), str(row["session_id"])),
        ss.repo_label(card.repo),
        card.title,
        card.what_this_was,
        card.what_happened,
        card.next_clue,
        " ".join(card.mentioned_paths),
        " ".join(item.text for item in card.evidence),
        ss.why_line(query, row, label, rows),
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

    contains = ss.normalize_eval_terms(rule.get("contains"))
    if any(term not in text for term in contains):
        return False
    contains_all = ss.normalize_eval_terms(rule.get("contains_all"))
    if any(term not in text for term in contains_all):
        return False
    contains_any = ss.normalize_eval_terms(rule.get("contains_any"))
    if contains_any and not any(term in text for term in contains_any):
        return False
    not_contains = ss.normalize_eval_terms(rule.get("not_contains"))
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
        text = ss.eval_text_for_result(conn, row, query, label)
        if any(ss.eval_rule_matches(row, text, rule) for rule in rules):
            return rank, row
    return None, None


def eval_title(conn: sqlite3.Connection, row: sqlite3.Row | None, query: str) -> str:
    if row is None:
        return "(none)"
    card = ss.session_card_for_result(conn, row, query)
    return ss.compact(card.title or str(row["title"] or row["text"]), 90)


def cmd_eval(args: argparse.Namespace) -> int:
    eval_path = ss.expand(args.file)
    cases = ss.load_eval_cases(eval_path)
    with ss.session_lock(shared=False):
        db_path = ss.expand(args.db)
        if not db_path.exists():
            print(f"Index not found: {db_path}", file=sys.stderr)
            return 2
        conn = ss.connect_db(db_path)
        try:
            ss.init_db(conn)
            if args.refresh:
                ss.reset_db(conn)
                ss.upsert_documents(conn, ss.build_docs(ss.expand(args.home), ss.normalize_sources("all")))

            passed = 0
            failed = 0
            print(f"Session search evals: {eval_path}")
            print()
            for i, case in enumerate(cases, 1):
                query = ss.sanitize_text(case["query"])
                limit = int(case.get("limit", args.limit))
                source = str(case.get("source", "all"))
                mode = str(case.get("mode", args.mode))
                display_query, results = ss.run_search(conn, query, limit, source, mode)
                accept_rules = list(case.get("accept") or [])
                reject_rules = list(case.get("reject") or [])
                expected_within = int(case.get("expected_within", 1))
                bad_before = int(case.get("bad_before", 1))

                good_rank, good_row = ss.result_rank_for_rules(
                    conn, results[:expected_within], display_query, accept_rules
                )
                bad_rank, bad_row = ss.result_rank_for_rules(
                    conn, results[:bad_before], display_query, reject_rules
                )

                ok = (not accept_rules or good_rank is not None) and bad_rank is None
                status = "PASS" if ok else "FAIL"
                if ok:
                    passed += 1
                else:
                    failed += 1

                top = results[0][0] if results else None
                print(f"{status} {i}. {query}")
                print(f"   top: {ss.eval_title(conn, top, display_query)}")
                if accept_rules:
                    found = (
                        f"rank {good_rank}: {ss.eval_title(conn, good_row, display_query)}"
                        if good_rank
                        else "not found"
                    )
                    print(f"   expected by rank {expected_within}: {found}")
                if reject_rules:
                    found = (
                        f"rank {bad_rank}: {ss.eval_title(conn, bad_row, display_query)}"
                        if bad_rank
                        else "none"
                    )
                    print(f"   rejected before rank {bad_before}: {found}")
                if getattr(args, "verbose", False):
                    for rank, (row, _score, label) in enumerate(results[: min(limit, 5)], 1):
                        print(f"   {rank}. [{row['source']}] {ss.eval_title(conn, row, display_query)} ({label})")
                print()

            print(f"Summary: {passed} passed, {failed} failed")
            return 0 if failed == 0 else 1
        finally:
            conn.close()


def cmd_natural(args: argparse.Namespace) -> int:
    query_parts = list(args.query)
    force_refresh = bool(getattr(args, "fresh", False))
    if query_parts and query_parts[0].lower() in {"fresh", "refresh", "new"}:
        force_refresh = True
        query_parts = query_parts[1:]
    query = " ".join(query_parts).strip()
    db_path = ss.expand(args.db)
    if not args.no_refresh and (force_refresh or not db_path.exists()):
        ss.cmd_index(
            argparse.Namespace(
                db=args.db,
                home=args.home,
                source="all",
                reset=True,
                quiet=not force_refresh,
            )
        )
    if not query:
        return ss.cmd_dashboard(args)
    return ss.cmd_search(
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
    parser.add_argument("--db", default=ss.DEFAULT_DB, help=f"SQLite index path. Default: {ss.DEFAULT_DB}")
    parser.add_argument("--home", default="~", help="Home directory containing .codex/.claude/Library.")
    sub = parser.add_subparsers(dest="command", required=True)

    index = sub.add_parser("index", help="Build or refresh the search index.")
    index.add_argument("--source", default="all", help="all or comma list: codex,claude,pi,vscode,cursor")
    index.add_argument("--reset", action="store_true", help="Drop and rebuild the index first.")
    index.add_argument("--quiet", action="store_true")
    index.set_defaults(func=ss.cmd_index)

    search = sub.add_parser("search", help="Search indexed sessions.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--source", default="all", choices=list(ss.SOURCE_CHOICES))
    search.add_argument("--mode", default="hybrid", choices=["fts", "local", "hybrid"])
    search.set_defaults(func=ss.cmd_search)

    show = sub.add_parser("show", help="Show a full indexed document by id.")
    show.add_argument("doc_id")
    show.set_defaults(func=ss.cmd_show)

    resume = sub.add_parser("resume", help="Print exact native resume instructions for a search result.")
    resume.add_argument("selector", help="Result rank from the last search, or a document id.")
    resume.set_defaults(func=ss.cmd_resume)

    archived = sub.add_parser("archived", help="Show sessions marked archived.")
    archived.add_argument("--limit", type=int, default=10)
    archived.add_argument("--source", default="all", choices=list(ss.SOURCE_CHOICES))
    archived.add_argument("--no-refresh", action="store_true")
    archived.set_defaults(func=ss.cmd_archived, archived=True, mode="hybrid")

    archive_audit = sub.add_parser("archive-audit", help="Preview parser migration repairs without changing state.")
    archive_audit.set_defaults(func=ss.cmd_archive_audit)

    archive = sub.add_parser("archive", help="Mark a numbered session archived.")
    archive.add_argument("selector")
    archive.set_defaults(func=ss.cmd_set_archive, archived=True)

    unarchive = sub.add_parser("unarchive", help="Return a numbered session to the active dashboard.")
    unarchive.add_argument("selector")
    unarchive.set_defaults(func=ss.cmd_set_archive, archived=False)

    handoff = sub.add_parser("handoff", help="Create a cross-harness context packet for a search result.")
    handoff.add_argument("selector", help="Result rank from the last search, or a document id.")
    handoff.add_argument("target", help="Target harness: codex or claude.")
    handoff.set_defaults(func=ss.cmd_handoff)

    status = sub.add_parser("status", help="Show index counts.")
    status.set_defaults(func=ss.cmd_status)

    capabilities = sub.add_parser("capabilities", help="Show supported behavior for each session source.")
    capabilities.set_defaults(func=ss.cmd_capabilities)

    demo = sub.add_parser("demo", help="Run an isolated demonstration with synthetic sessions.")
    demo.set_defaults(func=ss.cmd_demo)

    cards = sub.add_parser("cards", help="Build cached local session cards.")
    cards.add_argument("--limit", type=int, default=None)
    cards.set_defaults(func=ss.cmd_cards)

    embed = sub.add_parser("embed", help="Build local semantic embeddings.")
    embed.add_argument("--limit", type=int, default=None)
    embed.set_defaults(func=ss.cmd_embed)

    eval_cmd = sub.add_parser("eval", help="Run local ranking evals.")
    eval_cmd.add_argument("--file", default=str(ss.DEFAULT_EVALS), help="JSON eval case file.")
    eval_cmd.add_argument("--limit", type=int, default=10)
    eval_cmd.add_argument("--mode", default="hybrid", choices=["fts", "local", "hybrid"])
    eval_cmd.add_argument("--refresh", action="store_true", help="Refresh index before running evals.")
    eval_cmd.add_argument("--verbose", action="store_true")
    eval_cmd.set_defaults(func=ss.cmd_eval)

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
    parser.add_argument("--db", default=ss.DEFAULT_DB, help=argparse.SUPPRESS)
    parser.add_argument("--home", default="~", help=argparse.SUPPRESS)
    parser.add_argument("-f", "--fresh", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-refresh", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-n", "--limit", type=int, default=10, help=argparse.SUPPRESS)
    parser.add_argument("--mode", default="hybrid", choices=["fts", "local", "hybrid"], help=argparse.SUPPRESS)
    parser.add_argument(
        "--source",
        default="all",
        choices=list(ss.SOURCE_CHOICES),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--claude", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--codex", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--pi", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--copilot", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--vscode", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("query", nargs="*")
    return parser


def parse_natural(argv: list[str]) -> argparse.Namespace:
    parser = ss.build_natural_parser()
    args = parser.parse_args(argv)
    selected = [name for name in ("claude", "codex", "pi", "copilot", "vscode") if getattr(args, name)]
    if selected:
        choice = selected[-1]
        args.source = "vscode" if choice in {"copilot", "vscode"} else choice
    args.func = ss.cmd_natural
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
    selector = ss.first_numeric_selector(argv)
    if not selector:
        return None

    look_words = {"details", "inspect", "look", "read", "show"}
    open_words = {"open", "resume"}
    continue_words = {"bring", "continue", "move", "switch", "take", "use"}

    if first in {"archive", "unarchive"}:
        return argparse.Namespace(
            db=ss.DEFAULT_DB,
            selector=selector,
            archived=first == "archive",
            func=ss.cmd_set_archive,
        )

    if first in look_words:
        return argparse.Namespace(db=ss.DEFAULT_DB, doc_id=selector, func=ss.cmd_show)

    if first in open_words:
        return argparse.Namespace(db=ss.DEFAULT_DB, selector=selector, target="", func=ss.cmd_continue)

    if first in continue_words:
        target = ss.infer_target_from_tokens(lower)
        return argparse.Namespace(db=ss.DEFAULT_DB, selector=selector, target=target, func=ss.cmd_continue)

    if first.isdigit():
        target = ss.infer_target_from_tokens(lower[1:])
        return argparse.Namespace(db=ss.DEFAULT_DB, selector=selector, target=target, func=ss.cmd_continue)

    return None
