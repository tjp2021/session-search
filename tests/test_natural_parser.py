#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import datetime as dt
import io
import importlib.util
import json
import os
import pathlib
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "session_search.py"


def load_module():
    spec = importlib.util.spec_from_file_location("session_search", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ss = load_module()

from card_quality import clean_card_field  # noqa: E402  (import after sys.path setup)


def make_row(**values):
    defaults = {
        "doc_id": "doc",
        "source": "codex",
        "session_id": "session-1",
        "title": "",
        "path": "/tmp/source",
        "cwd": "/tmp/work",
        "role": "user",
        "ts": 1,
        "text": "",
        "meta_json": "{}",
    }
    defaults.update(values)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    columns = list(defaults)
    select_list = ", ".join(f"? AS {column}" for column in columns)
    row = conn.execute(f"SELECT {select_list}", [defaults[column] for column in columns]).fetchone()
    conn.close()
    return row


def make_conn_with_docs(docs):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ss.init_db(conn)
    ss.upsert_documents(conn, docs)
    return conn


def doc(**values):
    defaults = {
        "doc_id": "doc",
        "source": "codex",
        "session_id": "session-1",
        "title": "",
        "path": "/tmp/source",
        "cwd": "/tmp/work",
        "role": "user",
        "ts": 1,
        "text": "",
        "meta": {},
    }
    defaults.update(values)
    return ss.Document(**defaults)


class InstalledLauncherTest(unittest.TestCase):
    def test_launcher_exports_natural_mode_before_exec(self):
        launcher_path = ROOT / "ss_launcher.sh"
        if not launcher_path.exists():
            self.skipTest("private operator launcher is not part of the public package")
        launcher = launcher_path.read_text(encoding="utf-8")
        export_at = launcher.index("export SS_NATURAL=1")
        execs = [
            match for match in re.finditer(r"^[ \t]*exec[ \t]+(?P<target>.+)$", launcher, re.M)
        ]
        self.assertTrue(execs, "launcher never execs an interpreter")
        for match in execs:
            target = match.group("target")
            # Every exec must run session-search itself. A launcher that execs
            # anything else is not this launcher.
            self.assertIn("session_search", target, f"launcher execs {target!r}")
            self.assertLess(export_at, match.start(), "natural mode must be exported first")
            # The script's own comment calls this load-bearing: execing from the
            # private mirror lets that copy shadow the installed package.
            cd_root = launcher.rfind("cd /", 0, match.start())
            self.assertGreater(cd_root, export_at, f"exec at {match.start()} is not preceded by cd /")

    def test_bare_python_entrypoint_uses_dashboard_without_environment_flag(self):
        with mock.patch.object(ss, "cmd_natural", return_value=0) as natural:
            self.assertEqual(ss.main([]), 0)
        natural.assert_called_once()
        self.assertEqual(natural.call_args.args[0].query, [])


class NaturalFollowupParserTest(unittest.TestCase):
    def parse(self, words: list[str]):
        parsed = ss.parse_natural_followup(words)
        self.assertIsNotNone(parsed)
        return parsed

    def test_open_exact_session(self):
        parsed = self.parse(["open", "1"])
        self.assertEqual(parsed.selector, "1")
        self.assertEqual(parsed.target, "")
        self.assertEqual(parsed.func.__name__, "cmd_continue")

    def test_bare_number_opens_exact_session(self):
        parsed = self.parse(["1"])
        self.assertEqual(parsed.selector, "1")
        self.assertEqual(parsed.target, "")
        self.assertEqual(parsed.func.__name__, "cmd_continue")

    def test_continue_in_claude(self):
        parsed = self.parse(["continue", "1", "in", "claude"])
        self.assertEqual(parsed.selector, "1")
        self.assertEqual(parsed.target, "claude")
        self.assertEqual(parsed.func.__name__, "cmd_continue")

    def test_continue_in_codex_typo(self):
        parsed = self.parse(["continue", "2", "in", "codecs"])
        self.assertEqual(parsed.selector, "2")
        self.assertEqual(parsed.target, "codex")
        self.assertEqual(parsed.func.__name__, "cmd_continue")

    def test_look_at_result(self):
        parsed = self.parse(["look", "at", "3"])
        self.assertEqual(parsed.doc_id, "3")
        self.assertEqual(parsed.func.__name__, "cmd_show")

    def test_plain_search_does_not_parse_as_followup(self):
        self.assertIsNone(ss.parse_natural_followup(["true", "health", "deploy"]))


class QueryTermTest(unittest.TestCase):
    def test_class_is_not_stemmed_to_clas(self):
        self.assertEqual(ss.normalize_token("class"), "class")
        self.assertEqual(ss.normalize_token("classes"), "class")

    def test_csharp_is_kept_as_short_anchor(self):
        self.assertIn("c#", ss.query_anchor_terms("Alex C# class structure"))

    def test_task_clause_is_extracted_for_card_line(self):
        line = ss.clean_card_line(
            "Context: Northstar Clinic website. Task: rewrite and expand ONLY clients/Northstar/website/services/assessment.html. Do not edit CSS."
        )
        self.assertEqual(
            line,
            "rewrite and expand ONLY clients/Northstar/website/services/assessment.html",
        )


class RerankTest(unittest.TestCase):
    def test_recent_corpus_dump_does_not_beat_query_focused_learning_card(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="recent",
                    session_id="recent",
                    title="Vault migration audit",
                    text=(
                        "Inventory dump mentioning Alex in one section, C# in another, "
                        "plus unrelated class and structure notes."
                    ),
                    ts=200,
                ),
                doc(
                    doc_id="learning",
                    session_id="learning",
                    title="Alex Rust class structure lesson",
                    text="Alex asked about C# class structure, projects, namespaces, and frameworks.",
                    ts=10_000,
                ),
            ]
        )
        recent = conn.execute("SELECT * FROM documents WHERE doc_id = 'recent'").fetchone()
        learning = conn.execute("SELECT * FROM documents WHERE doc_id = 'learning'").fetchone()

        with mock.patch.object(
            ss,
            "build_session_card",
            side_effect=lambda rows, row, query: ss.SessionCard(
                title=str(row["title"]),
                source=str(row["source"]),
                session_id=str(row["session_id"]),
                repo=str(row["cwd"]),
                last_active=row["ts"],
                last_user_message=str(row["text"]),
                what_this_was=(
                    "Migration inventory across unrelated projects"
                    if row["doc_id"] == "recent"
                    else "Alex Rust class structure lesson"
                ),
                what_happened="",
                next_clue="",
                mentioned_paths=(),
                evidence=(),
            ),
        ):
            reranked = ss.rerank_session_results(
                conn,
                [(recent, 9.0, "semantic"), (learning, 1.0, "fts")],
                "Alex Rust class structure",
                2,
            )

        self.assertEqual([row["doc_id"] for row, _score, _label in reranked], ["learning"])
        conn.close()

    def test_corpus_dump_is_available_only_when_the_query_asks_for_it(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="dump",
                    session_id="dump",
                    title="Scraped page text",
                    text="This is a full backup of a local service and contains live secrets.",
                    ts=200,
                )
            ]
        )
        dump = conn.execute("SELECT * FROM documents WHERE doc_id = 'dump'").fetchone()
        hidden = ss.rerank_session_results(conn, [(dump, 9.0, "semantic")], "Alex Rust", 1)
        requested = ss.rerank_session_results(conn, [(dump, 9.0, "semantic")], "local backup", 1)
        self.assertEqual(hidden, [])
        self.assertEqual(requested[0][0]["doc_id"], "dump")
        conn.close()

    def test_exact_email_query_filters_broad_recent_domain_matches(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="recent",
                    session_id="recent",
                    title="Other Gmail work",
                    text="Check another address at example@gmail.com.",
                    ts=200,
                ),
                doc(
                    doc_id="exact",
                    session_id="exact",
                    title="Northstar Clinic alert",
                    text="Send appointment alerts to appointments@northstar.example.",
                    ts=10_000,
                ),
            ]
        )
        recent = conn.execute("SELECT * FROM documents WHERE doc_id = 'recent'").fetchone()
        exact = conn.execute("SELECT * FROM documents WHERE doc_id = 'exact'").fetchone()

        reranked = ss.rerank_session_results(
            conn,
            [(recent, 9.0, "semantic"), (exact, 1.0, "fts")],
            "appointments@northstar.example",
            2,
        )

        self.assertEqual([row["doc_id"] for row, _score, _label in reranked], ["exact"])
        conn.close()

    def test_semantic_score_preserves_focused_mid_session_turn(self):
        self.assertGreater(ss.semantic_score(0.2, 0.9), ss.semantic_score(0.6, 0.1))

    def test_strong_relevance_beats_newer_weak_match(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="old",
                    session_id="old",
                    title="Northstar Clinic Vercel deployment",
                    text="Northstar Clinic Vercel deployment exact match.",
                    ts=10,
                ),
                doc(
                    doc_id="recent",
                    session_id="recent",
                    title="Northstar Clinic deploy follow-up",
                    text="Northstar Clinic deploy follow-up.",
                    ts=50,
                ),
            ]
        )
        old = conn.execute("SELECT * FROM documents WHERE doc_id = 'old'").fetchone()
        recent = conn.execute("SELECT * FROM documents WHERE doc_id = 'recent'").fetchone()

        reranked = ss.rerank_session_results(
            conn,
            [(old, 9.0, "semantic"), (recent, 1.0, "local")],
            "Northstar Clinic deploy",
            2,
        )

        self.assertEqual(reranked[0][0]["doc_id"], "old")
        conn.close()

    def test_recency_breaks_tie_between_similarly_relevant_sessions(self):
        now = 2_000_000
        self.assertGreater(ss.recency_boost(now - 60, now), ss.recency_boost(now - 864_000, now))
        self.assertLessEqual(ss.recency_boost(now, now), 0.35)

        conn = make_conn_with_docs(
            [
                doc(doc_id="old", session_id="old", title="Tether role", text="Tether role application", ts=now - 864_000),
                doc(doc_id="recent", session_id="recent", title="Tether role", text="Tether role application", ts=now - 60),
            ]
        )
        old = conn.execute("SELECT * FROM documents WHERE doc_id = 'old'").fetchone()
        recent = conn.execute("SELECT * FROM documents WHERE doc_id = 'recent'").fetchone()
        with mock.patch.object(ss, "now_ts", return_value=now):
            reranked = ss.rerank_session_results(
                conn,
                [(old, 1.0, "semantic"), (recent, 1.0, "semantic")],
                "Tether role application",
                2,
            )
        self.assertEqual(reranked[0][0]["doc_id"], "recent")
        conn.close()

    def test_main_deployment_beats_broad_side_task(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="side",
                    session_id="side",
                    title="Bounded sidecar task for Northstar Clinic website. Do not edit files.",
                    text="Find public review text for the Northstar Clinic website.",
                    ts=10,
                ),
                doc(
                    doc_id="deploy",
                    session_id="deploy",
                    title="we had a session spinning up a Vercel deployment for Northstar Clinic website",
                    text="The Northstar Clinic deployment had email and domain mistakes to fix.",
                    ts=20,
                ),
            ]
        )
        side = conn.execute("SELECT * FROM documents WHERE doc_id = 'side'").fetchone()
        deploy = conn.execute("SELECT * FROM documents WHERE doc_id = 'deploy'").fetchone()

        reranked = ss.rerank_session_results(
            conn,
            [(side, 1.6, "semantic"), (deploy, 0.95, "hybrid")],
            "Northstar Clinic deploy",
            2,
        )

        self.assertEqual(reranked[0][0]["doc_id"], "deploy")
        conn.close()

    def test_meta_session_is_suppressed_for_non_meta_query(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="meta",
                    session_id="meta",
                    title="ss --codex Alex Rust class structure",
                    text="We are discussing the session search result shape.",
                    ts=10,
                ),
                doc(
                    doc_id="real",
                    session_id="real",
                    title="Alex Rust class structure",
                    text="Alex was asking about C# classes, projects, and structure.",
                    ts=20,
                ),
            ]
        )
        meta = conn.execute("SELECT * FROM documents WHERE doc_id = 'meta'").fetchone()
        real = conn.execute("SELECT * FROM documents WHERE doc_id = 'real'").fetchone()

        reranked = ss.rerank_session_results(
            conn,
            [(meta, 2.0, "semantic"), (real, 0.9, "hybrid")],
            "Alex Rust class structure",
            2,
        )

        self.assertEqual(reranked[0][0]["doc_id"], "real")
        conn.close()

    def test_result_shape_example_is_suppressed_for_domain_query(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="meta",
                    session_id="meta",
                    title="Example shape: 1. [codex] Northstar Clinic deploy",
                    text="match: hybrid, 3 hits lets discuss this example shape.",
                    ts=20,
                ),
                doc(
                    doc_id="real",
                    session_id="real",
                    title="Northstar Clinic Vercel deploy",
                    text="The Northstar Clinic website Vercel deployment is live.",
                    ts=10,
                ),
            ]
        )
        meta = conn.execute("SELECT * FROM documents WHERE doc_id = 'meta'").fetchone()
        real = conn.execute("SELECT * FROM documents WHERE doc_id = 'real'").fetchone()

        reranked = ss.rerank_session_results(
            conn,
            [(meta, 9.0, "semantic"), (real, 1.0, "hybrid")],
            "Northstar Clinic vercel deploy",
            2,
        )

        self.assertEqual(reranked[0][0]["doc_id"], "real")
        conn.close()

    def test_meta_session_is_boosted_for_meta_query(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="generic",
                    session_id="generic",
                    title="Paperclip employment workflow",
                    text="Paperclip employment driver packet search notes.",
                    ts=5,
                ),
                doc(
                    doc_id="meta",
                    session_id="meta",
                    title="Build session search tool",
                    text="session search tool result card context packet ss open ss continue",
                    ts=10,
                ),
            ]
        )
        generic = conn.execute("SELECT * FROM documents WHERE doc_id = 'generic'").fetchone()
        meta = conn.execute("SELECT * FROM documents WHERE doc_id = 'meta'").fetchone()

        reranked = ss.rerank_session_results(
            conn,
            [(generic, 2.0, "fts"), (meta, 0.9, "hybrid")],
            "session search result cards context packet",
            2,
        )

        self.assertEqual(reranked[0][0]["doc_id"], "meta")
        conn.close()

    def test_meta_query_does_not_require_generated_card_to_repeat_every_term(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="meta",
                    session_id="meta",
                    title="Build session search tool",
                    text="session search result cards context packet ss open",
                    ts=10,
                )
            ]
        )
        meta = conn.execute("SELECT * FROM documents WHERE doc_id = 'meta'").fetchone()
        with mock.patch.object(
            ss,
            "build_session_card",
            return_value=ss.SessionCard(
                title="SS design",
                source="codex",
                session_id="meta",
                repo="/tmp/work",
                last_active=10,
                last_user_message="Fix SS.",
                what_this_was="Search design work.",
                what_happened="",
                next_clue="",
                mentioned_paths=(),
                evidence=(),
            ),
        ):
            reranked = ss.rerank_session_results(
                conn,
                [(meta, 1.0, "fts")],
                "session search result cards context packet",
                1,
            )
        self.assertEqual(reranked[0][0]["doc_id"], "meta")
        conn.close()

    def test_assistant_activity_does_not_override_user_recency_for_tied_relevance(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="older-user",
                    session_id="older-session",
                    title="Northstar Clinic deployment",
                    role="user",
                    text="Fix the Northstar Clinic deployment.",
                    ts=100,
                ),
                doc(
                    doc_id="older-assistant",
                    session_id="older-session",
                    title="Northstar Clinic deployment",
                    role="assistant",
                    text="Verified the Northstar Clinic deployment after the prompt.",
                    ts=500,
                ),
                doc(
                    doc_id="recent-user",
                    session_id="recent-session",
                    title="Northstar Clinic deployment followup",
                    role="user",
                    text="Check the Northstar Clinic deployment again.",
                    ts=200,
                ),
            ]
        )
        older = conn.execute("SELECT * FROM documents WHERE doc_id = 'older-user'").fetchone()
        recent = conn.execute("SELECT * FROM documents WHERE doc_id = 'recent-user'").fetchone()

        reranked = ss.rerank_session_results(
            conn,
            [(older, 1.0, "semantic"), (recent, 1.0, "hybrid")],
            "Northstar Clinic deployment",
            2,
        )

        self.assertEqual(reranked[0][0]["doc_id"], "recent-user")
        conn.close()


class SessionCardTest(unittest.TestCase):
    def test_llm_about_uses_one_sentence_prompt(self):
        row = make_row(text="We fixed the session-search dashboard and rebuilt its cache.")
        prompts: list[tuple[str, int]] = []

        def summarize(prompt: str, max_tokens: int = 220) -> str:
            prompts.append((prompt, max_tokens))
            if prompt.startswith("Summarize what this session was about"):
                return "Fixed the session-search dashboard and rebuilt its cached cards."
            return "Verify the refreshed dashboard output."

        with mock.patch.object(ss, "llm_summarize", side_effect=summarize):
            card = ss.build_session_card([row], row, "", use_llm=True)

        about_prompt, about_tokens = prompts[0]
        self.assertIn("exactly 1 sentence", about_prompt)
        self.assertNotIn("2 sentences", about_prompt)
        self.assertLessEqual(about_tokens, 160)
        self.assertEqual(
            card.what_this_was,
            "Fixed the session-search dashboard and rebuilt its cached cards.",
        )

    def test_card_name_uses_last_user_message_timestamp_and_summary_keeps_message(self):
        rows = [
            make_row(doc_id="u1", role="user", ts=1_700_000_000, title="Deploy Northstar Clinic", text="Start the deploy."),
            make_row(doc_id="a1", role="assistant", ts=1_700_000_100, title="Deploy Northstar Clinic", text="Deployment complete."),
            make_row(doc_id="u2", role="user", ts=1_700_000_050, title="Deploy Northstar Clinic", text="Verify Cloudflare and preserve this exact last request."),
        ]

        card = ss.build_session_card(rows, rows[0], "Northstar Clinic")

        expected_stamp = dt.datetime.fromtimestamp(1_700_000_050, tz=dt.timezone.utc).astimezone().strftime("%d/%m/%y %H:%M:%S")
        self.assertTrue(card.title.startswith(expected_stamp + " — "))
        self.assertEqual(card.last_active, 1_700_000_050)
        self.assertIn("preserve this exact last request", card.last_user_message)

    def test_timestamped_title_replaces_old_prefix(self):
        title = ss.timestamped_session_title("01/01/26 10:00 — Existing title", 1_700_000_050)
        self.assertEqual(title.count("—"), 1)

    def test_last_user_message_excludes_subagents_and_command_events(self):
        subagent = make_row(
            doc_id="subagent",
            role="user",
            ts=3,
            path="/tmp/session/subagents/agent-1.jsonl",
            text="You are an adversarial reviewer.",
        )
        command = make_row(
            doc_id="command",
            role="user",
            ts=2,
            text="<command-name>/model</command-name>",
        )
        direct = make_row(doc_id="direct", role="user", ts=1, text="This is Alex's actual message.")

        self.assertEqual(ss.last_user_message([direct, command, subagent], direct), "This is Alex's actual message.")

    def test_card_uses_only_local_evidence(self):
        rows = [
            make_row(
                doc_id="u1",
                role="user",
                ts=1,
                text="I need to deploy the Northstar Clinic site and check clients/Northstar/website/index.html.",
            ),
            make_row(
                doc_id="a1",
                role="assistant",
                ts=2,
                text="Found the Vercel environment mismatch and updated README.md with the deploy notes.",
            ),
            make_row(
                doc_id="a2",
                role="assistant",
                ts=3,
                text="Next: verify DNS before deploying again.",
            ),
        ]
        card = ss.build_session_card(rows, rows[0], "Northstar Clinic deploy")
        self.assertIn("Northstar Clinic", card.what_this_was)
        self.assertIn("Vercel", card.what_happened)
        self.assertIn("verify DNS", card.next_clue)
        self.assertIn("clients/Northstar/website/index.html", card.mentioned_paths)
        self.assertGreaterEqual(len(card.evidence), 3)

    def test_card_does_not_invent_missing_next_action(self):
        row = make_row(text="We talked about C# class structure with Alex.")
        card = ss.build_session_card([row], row, "Alex Rust class structure")
        self.assertIn("C# class structure", card.what_this_was)
        self.assertEqual(card.what_happened, "")
        self.assertEqual(card.next_clue, "")

    def test_question_is_not_progress_evidence(self):
        row = make_row(text="so is the website deployed?")
        card = ss.build_session_card([row], row, "website deploy")
        self.assertEqual(card.what_happened, "")

    def test_fragment_is_not_progress_evidence(self):
        row = make_row(role="assistant", text="have a verified sending domain and")
        card = ss.build_session_card([row], row, "sending domain")
        self.assertEqual(card.what_happened, "")

    def test_card_prefers_query_specific_turn_inside_session(self):
        generic = make_row(
            doc_id="generic",
            title="Client: Northstar Clinic Chiropractic",
            text="Client: Northstar Clinic Chiropractic",
        )
        deploy = make_row(
            doc_id="deploy",
            ts=2,
            title="Northstar Clinic deployment",
            text="Ok, deploy this to some URL so I can send it to the client.",
        )
        card = ss.build_session_card([generic, deploy], generic, "Northstar Clinic deploy")
        self.assertIn("deploy", card.what_this_was.lower())

    def test_query_card_prefers_natural_line_over_config_line(self):
        row = make_row(
            text=(
                "APPOINTMENT_ALERT_EMAIL_TO=appointments@northstar.example\n"
                "Only thing before the flip: Cloudflare Pages needs the email env var before deploy."
            )
        )
        card = ss.build_session_card([row], row, "Northstar Clinic deploy")
        self.assertIn("Cloudflare Pages", card.what_this_was)

    def test_base_card_prefers_goal_over_late_status(self):
        goal = make_row(
            doc_id="goal",
            ts=1,
            text="I need to build a simple Blazor app so Alex can see the C# class structure.",
        )
        status = make_row(
            doc_id="status",
            ts=10,
            text="Seven commits today. Hard stop.",
        )
        card = ss.build_session_card([goal, status], status, "")
        self.assertIn("Blazor", card.what_this_was)

    def test_next_clue_does_not_use_older_matching_line(self):
        older = make_row(
            doc_id="older",
            ts=1,
            text="Next: use the Northstar Clinic logo in the email footer.",
        )
        topic = make_row(
            doc_id="topic",
            ts=2,
            text=(
                "Only thing before the flip: Cloudflare Pages needs the email env vars.\n"
                "Once you add the env vars/redeploy, I will run one final test form."
            ),
        )

        card = ss.build_session_card([older, topic], topic, "Northstar Clinic cloudflare")

        self.assertIn("Cloudflare Pages", card.what_this_was)
        self.assertIn("redeploy", card.next_clue)
        self.assertNotIn("footer", card.next_clue)


class DurableSessionCardTest(unittest.TestCase):
    def test_session_card_cache_round_trip(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="u1",
                    session_id="s1",
                    text="I need to deploy the Northstar Clinic site.",
                    ts=1,
                ),
                doc(
                    doc_id="a1",
                    session_id="s1",
                    role="assistant",
                    text="Updated the deploy notes.",
                    ts=2,
                ),
            ]
        )
        row = conn.execute("SELECT * FROM documents WHERE doc_id = 'u1'").fetchone()

        first = ss.session_card_for_result(conn, row, "", persist=True)
        second = ss.session_card_for_result(conn, row, "", persist=False)

        self.assertEqual(first.what_this_was, second.what_this_was)
        cached = conn.execute("SELECT count(*) FROM session_cards").fetchone()[0]
        self.assertEqual(cached, 1)
        conn.close()

    def test_session_card_rebuilds_when_session_changes(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="u1",
                    session_id="s1",
                    text="I need to deploy the Northstar Clinic site.",
                    ts=1,
                ),
            ]
        )
        row = conn.execute("SELECT * FROM documents WHERE doc_id = 'u1'").fetchone()
        first = ss.session_card_for_result(conn, row, "", persist=True)

        ss.upsert_documents(
            conn,
            [
                doc(
                    doc_id="u1",
                    session_id="s1",
                    text="I need to fix Cloudflare for Northstar Clinic.",
                    ts=2,
                ),
            ],
        )
        changed = conn.execute("SELECT * FROM documents WHERE doc_id = 'u1'").fetchone()
        second = ss.session_card_for_result(conn, changed, "", persist=True)

        self.assertNotEqual(first.what_this_was, second.what_this_was)
        self.assertIn("Cloudflare", second.what_this_was)
        conn.close()


class SelectorContextTest(unittest.TestCase):
    def test_rank_selector_keeps_last_search_query(self):
        old_path = ss.DEFAULT_LAST_RESULTS
        with tempfile.TemporaryDirectory() as tmpdir:
            ss.DEFAULT_LAST_RESULTS = str(pathlib.Path(tmpdir) / "last-results.json")
            try:
                conn = make_conn_with_docs(
                    [
                        doc(
                            doc_id="doc-1",
                            session_id="s1",
                            text="Cloudflare Pages work for Northstar Clinic.",
                            ts=1,
                        ),
                    ]
                )
                row = conn.execute("SELECT * FROM documents WHERE doc_id = 'doc-1'").fetchone()
                ss.save_last_results([(row, 1.0, "hybrid")], "Northstar Clinic cloudflare", pathlib.Path(":memory:"))

                self.assertEqual(ss.selected_row(conn, "1")["doc_id"], "doc-1")
                self.assertEqual(ss.query_from_selector("1"), "Northstar Clinic cloudflare")
                conn.close()
            finally:
                ss.DEFAULT_LAST_RESULTS = old_path

    def test_rank_selector_falls_back_to_session_when_doc_id_changed(self):
        old_path = ss.DEFAULT_LAST_RESULTS
        with tempfile.TemporaryDirectory() as tmpdir:
            ss.DEFAULT_LAST_RESULTS = str(pathlib.Path(tmpdir) / "last-results.json")
            try:
                conn = make_conn_with_docs(
                    [
                        doc(
                            doc_id="old-doc",
                            session_id="s1",
                            text="Old indexed evidence for Northstar Clinic deploy.",
                            ts=1,
                        ),
                    ]
                )
                old_row = conn.execute("SELECT * FROM documents WHERE doc_id = 'old-doc'").fetchone()
                ss.save_last_results([(old_row, 1.0, "hybrid")], "Northstar Clinic deploy", pathlib.Path(":memory:"))
                conn.close()

                conn = make_conn_with_docs(
                    [
                        doc(
                            doc_id="new-doc",
                            session_id="s1",
                            text="New indexed evidence for Northstar Clinic deploy.",
                            ts=2,
                        ),
                    ]
                )

                self.assertEqual(ss.selected_row(conn, "1")["doc_id"], "new-doc")
                conn.close()
            finally:
                ss.DEFAULT_LAST_RESULTS = old_path

    def test_rank_selectors_are_isolated_by_harness_context(self):
        old_path = ss.DEFAULT_LAST_RESULTS
        with tempfile.TemporaryDirectory() as tmpdir:
            ss.DEFAULT_LAST_RESULTS = str(pathlib.Path(tmpdir) / "last-results.json")
            try:
                conn = make_conn_with_docs(
                    [
                        doc(doc_id="doc-a", session_id="session-a", text="Tether campaign A", ts=1),
                        doc(doc_id="doc-b", session_id="session-b", text="Tether campaign B", ts=2),
                    ]
                )
                row_a = conn.execute("SELECT * FROM documents WHERE doc_id = 'doc-a'").fetchone()
                row_b = conn.execute("SELECT * FROM documents WHERE doc_id = 'doc-b'").fetchone()

                with mock.patch.dict(os.environ, {"SS_RESULT_CONTEXT": "codex-thread-a"}):
                    ss.save_last_results([(row_a, 1.0, "hybrid")], "tether a", pathlib.Path("/tmp/a.sqlite"))
                with mock.patch.dict(os.environ, {"SS_RESULT_CONTEXT": "codex-thread-b"}):
                    ss.save_last_results([(row_b, 1.0, "hybrid")], "tether b", pathlib.Path("/tmp/b.sqlite"))

                with mock.patch.dict(os.environ, {"SS_RESULT_CONTEXT": "codex-thread-a"}):
                    self.assertEqual(ss.selected_row(conn, "1")["doc_id"], "doc-a")
                    self.assertEqual(ss.query_from_selector("1"), "tether a")
                    self.assertEqual(
                        ss.db_path_from_selector("1", ss.DEFAULT_DB),
                        pathlib.Path("/tmp/a.sqlite").resolve(),
                    )
                with mock.patch.dict(os.environ, {"SS_RESULT_CONTEXT": "codex-thread-b"}):
                    self.assertEqual(ss.selected_row(conn, "1")["doc_id"], "doc-b")
                    self.assertEqual(ss.query_from_selector("1"), "tether b")
                    self.assertEqual(
                        ss.db_path_from_selector("1", ss.DEFAULT_DB),
                        pathlib.Path("/tmp/b.sqlite").resolve(),
                    )
                conn.close()
            finally:
                ss.DEFAULT_LAST_RESULTS = old_path

    def test_stable_context_fails_closed_but_global_callers_keep_legacy_file(self):
        old_path = ss.DEFAULT_LAST_RESULTS
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            ss.DEFAULT_LAST_RESULTS = str(root / "last-results.json")
            try:
                conn = make_conn_with_docs([doc(doc_id="doc-1", session_id="s1", text="Tether", ts=1)])
                row = conn.execute("SELECT * FROM documents WHERE doc_id = 'doc-1'").fetchone()
                with mock.patch.dict(os.environ, {"SS_RESULT_CONTEXT": "context-a"}):
                    ss.save_last_results([(row, 1.0, "hybrid")], "tether", pathlib.Path("/tmp/tether.sqlite"))
                    context_path = ss.result_context_path()
                    self.assertTrue(context_path.exists())
                    context_path.unlink()
                    self.assertIsNone(ss.selected_row(conn, "1"))
                    self.assertEqual(ss.query_from_selector("1"), "")
                with mock.patch.object(ss, "result_context_identity", return_value=("global", "global")):
                    self.assertEqual(ss.selected_row(conn, "1")["doc_id"], "doc-1")
                    self.assertEqual(ss.query_from_selector("1"), "tether")
                self.assertTrue((root / "last-results.json").exists())
                self.assertEqual(json.loads((root / "last-results.json").read_text())["query"], "tether")
                self.assertEqual(list(root.rglob("*.tmp")), [])
                conn.close()
            finally:
                ss.DEFAULT_LAST_RESULTS = old_path

    def test_corrupt_stable_context_does_not_inherit_global_results(self):
        old_path = ss.DEFAULT_LAST_RESULTS
        with tempfile.TemporaryDirectory() as tmpdir:
            ss.DEFAULT_LAST_RESULTS = str(pathlib.Path(tmpdir) / "last-results.json")
            try:
                conn = make_conn_with_docs([doc(doc_id="doc-1", session_id="s1", text="Tether", ts=1)])
                row = conn.execute("SELECT * FROM documents WHERE doc_id = 'doc-1'").fetchone()
                with mock.patch.dict(os.environ, {"SS_RESULT_CONTEXT": "context-a"}):
                    ss.save_last_results([(row, 1.0, "hybrid")], "tether", pathlib.Path("/tmp/tether.sqlite"))
                    ss.result_context_path().write_text("{", encoding="utf-8")
                    self.assertIsNone(ss.selected_row(conn, "1"))
                conn.close()
            finally:
                ss.DEFAULT_LAST_RESULTS = old_path

    def test_context_identity_precedence_covers_harnesses_and_terminals(self):
        with mock.patch.dict(
            os.environ,
            {
                "SS_RESULT_CONTEXT": "explicit-id",
                "CODEX_THREAD_ID": "codex-id",
                "CLAUDE_CODE_SESSION_ID": "claude-code-id",
                "TERM_SESSION_ID": "term-id",
            },
            clear=True,
        ):
            self.assertEqual(ss.result_context_identity(), ("explicit", "explicit-id"))
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "codex-id"}, clear=True):
            self.assertEqual(ss.result_context_identity(), ("codex", "codex-id"))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "claude-code-id"}, clear=True):
            self.assertEqual(ss.result_context_identity(), ("claude", "claude-code-id"))
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "claude-id"}, clear=True):
            self.assertEqual(ss.result_context_identity(), ("claude", "claude-id"))
        with mock.patch.dict(os.environ, {"TERM_SESSION_ID": "term-id"}, clear=True):
            self.assertEqual(ss.result_context_identity(), ("terminal", "term-id"))

    def test_missing_saved_database_returns_recovery_message_without_creating_it(self):
        old_results = ss.DEFAULT_LAST_RESULTS
        old_lock = ss.DEFAULT_LOCK
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            missing_db = root / "missing.sqlite"
            ss.DEFAULT_LAST_RESULTS = str(root / "last-results.json")
            ss.DEFAULT_LOCK = str(root / "session-search.lock")
            try:
                conn = make_conn_with_docs([doc(doc_id="doc-1", session_id="s1", text="Tether", ts=1)])
                row = conn.execute("SELECT * FROM documents WHERE doc_id = 'doc-1'").fetchone()
                with mock.patch.dict(os.environ, {"SS_RESULT_CONTEXT": "context-a"}):
                    ss.save_last_results([(row, 1.0, "hybrid")], "tether", missing_db)
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        status = ss.cmd_resume(ss.argparse.Namespace(selector="1", db=str(root / "default.sqlite")))
                self.assertEqual(status, 2)
                self.assertIn("Session index unavailable", stderr.getvalue())
                self.assertIn("ss fresh", stderr.getvalue())
                self.assertFalse(missing_db.exists())
                conn.close()
            finally:
                ss.DEFAULT_LAST_RESULTS = old_results
                ss.DEFAULT_LOCK = old_lock

    def test_archive_selectors_stay_isolated_across_contexts(self):
        old_results = ss.DEFAULT_LAST_RESULTS
        old_lock = ss.DEFAULT_LOCK
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            db_path = root / "sessions.sqlite"
            ss.DEFAULT_LAST_RESULTS = str(root / "last-results.json")
            ss.DEFAULT_LOCK = str(root / "session-search.lock")
            try:
                conn = ss.connect_db(db_path)
                ss.init_db(conn)
                ss.upsert_documents(
                    conn,
                    [
                        doc(doc_id="doc-a", session_id="session-a", text="Alpha", ts=100),
                        doc(doc_id="doc-b", session_id="session-b", text="Beta", ts=200),
                    ],
                )
                row_a = conn.execute("SELECT * FROM documents WHERE doc_id = 'doc-a'").fetchone()
                row_b = conn.execute("SELECT * FROM documents WHERE doc_id = 'doc-b'").fetchone()
                with mock.patch.dict(os.environ, {"SS_RESULT_CONTEXT": "context-a"}, clear=True):
                    ss.save_last_results([(row_a, 1.0, "recent")], "a", db_path)
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(
                            ss.cmd_set_archive(ss.argparse.Namespace(db=str(db_path), selector="1", archived=True)),
                            0,
                        )
                with mock.patch.dict(os.environ, {"SS_RESULT_CONTEXT": "context-b"}, clear=True):
                    ss.save_last_results([(row_b, 1.0, "recent")], "b", db_path)
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(
                            ss.cmd_set_archive(ss.argparse.Namespace(db=str(db_path), selector="1", archived=True)),
                            0,
                        )
                self.assertTrue(ss.session_is_archived(conn, "codex", "session-a"))
                self.assertTrue(ss.session_is_archived(conn, "codex", "session-b"))
                with mock.patch.dict(os.environ, {"SS_RESULT_CONTEXT": "context-a"}, clear=True), mock.patch.object(
                    ss, "print_resume_instructions"
                ):
                    self.assertEqual(ss.cmd_resume(ss.argparse.Namespace(db=str(db_path), selector="1")), 0)
                self.assertFalse(ss.session_is_archived(conn, "codex", "session-a"))
                self.assertTrue(ss.session_is_archived(conn, "codex", "session-b"))
                conn.close()
            finally:
                ss.DEFAULT_LAST_RESULTS = old_results
                ss.DEFAULT_LOCK = old_lock


class RefreshBehaviorTest(unittest.TestCase):
    @staticmethod
    def args(db: pathlib.Path, query: list[str], fresh: bool = False):
        return ss.argparse.Namespace(
            db=str(db),
            home="~",
            fresh=fresh,
            no_refresh=False,
            limit=10,
            mode="hybrid",
            source="all",
            query=query,
        )

    def test_normal_search_uses_existing_index_without_rebuild(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = pathlib.Path(tmpdir) / "index.sqlite"
            db.touch()
            with mock.patch.object(ss, "cmd_index") as index, mock.patch.object(ss, "cmd_search", return_value=0):
                self.assertEqual(ss.cmd_natural(self.args(db, ["tether"])), 0)
            index.assert_not_called()

    def test_fresh_word_forces_visible_rebuild(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = pathlib.Path(tmpdir) / "index.sqlite"
            db.touch()
            with mock.patch.object(ss, "cmd_index") as index, mock.patch.object(ss, "cmd_search", return_value=0):
                self.assertEqual(ss.cmd_natural(self.args(db, ["fresh", "tether"])), 0)
            index.assert_called_once()
            call_args = index.call_args.args[0]
            self.assertTrue(call_args.reset)
            self.assertFalse(call_args.quiet)

    def test_missing_index_bootstraps_quietly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = pathlib.Path(tmpdir) / "missing.sqlite"
            with mock.patch.object(ss, "cmd_index") as index, mock.patch.object(ss, "cmd_search", return_value=0):
                self.assertEqual(ss.cmd_natural(self.args(db, ["tether"])), 0)
            index.assert_called_once()
            call_args = index.call_args.args[0]
            self.assertTrue(call_args.reset)
            self.assertTrue(call_args.quiet)


class VisibleEvalTest(unittest.TestCase):
    def test_query_card_does_not_fall_back_to_unrelated_base_next_clue(self):
        base = ss.SessionCard(
            title="Base session",
            source="claude",
            session_id="s1",
            repo="/tmp/work",
            last_active=1,
            last_user_message="Last base request",
            what_this_was="Job hunter skill setup",
            what_happened="",
            next_clue="Open the job-hunter resume output.",
            mentioned_paths=(),
            evidence=(),
        )
        query_card = ss.SessionCard(
            title="Northstar Clinic Cloudflare",
            source="claude",
            session_id="s1",
            repo="/tmp/work",
            last_active=2,
            last_user_message="Last query request",
            what_this_was="Wait for Alex to hook up Cloudflare Pages.",
            what_happened="Repo pushed to omegagrowth/Northstar-website.",
            next_clue="",
            mentioned_paths=("clients/Northstar/website/index.html",),
            evidence=(),
        )

        merged = ss.merge_query_card(base, query_card, "Northstar Clinic cloudflare")

        self.assertEqual(merged.next_clue, "")
        self.assertNotIn("job-hunter", "\n".join(merged.mentioned_paths).lower())

    def test_eval_text_uses_visible_card_not_raw_transcript(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="doc-1",
                    session_id="s1",
                    text="I need to deploy Northstar Clinic.\nraw-only-secret hidden transcript line",
                    ts=1,
                ),
            ]
        )
        row = conn.execute("SELECT * FROM documents WHERE doc_id = 'doc-1'").fetchone()

        visible_text = ss.eval_text_for_result(conn, row, "Northstar Clinic deploy")

        self.assertIn("northstar clinic", visible_text)
        self.assertNotIn("raw-only-secret", visible_text)
        conn.close()


class OutputShapeTest(unittest.TestCase):
    def test_dashboard_renders_without_waiting_for_summary_backfill(self):
        conn = make_conn_with_docs(
            [doc(doc_id="doc-1", session_id="session-1", text="Fresh dashboard work.", ts=1)]
        )
        results = ss.recent_session_results(conn, 1)
        with mock.patch.object(
            ss,
            "catch_up_dashboard_cards",
            side_effect=AssertionError("dashboard waited for summary backfill"),
        ), mock.patch.object(
            ss,
            "llm_summarize",
            side_effect=AssertionError("dashboard made a model call"),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                ss.print_dashboard(conn, results)

        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session_cards").fetchone()[0], 0)
        conn.close()

    def test_dashboard_catch_up_builds_at_most_ten_missing_cards(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id=f"doc-{index}",
                    session_id=f"session-{index}",
                    title=f"Session {index}",
                    text=f"Work item {index} needs a useful summary.",
                    ts=index,
                )
                for index in range(12)
            ]
        )
        results = ss.recent_session_results(conn, 12)

        def summarize(prompt: str, max_tokens: int = 220) -> str:
            if prompt.startswith("Summarize what this session was about"):
                return "Summarized the concrete work in this session."
            return "Resume the next concrete step."

        with mock.patch.object(ss, "llm_summarize", side_effect=summarize) as llm:
            built = ss.catch_up_dashboard_cards(conn, results, limit=10)

        self.assertEqual(built, 10)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session_cards").fetchone()[0], 10)
        self.assertEqual(llm.call_count, 20)
        conn.close()

    def test_dashboard_catch_up_skips_current_cached_cards(self):
        conn = make_conn_with_docs(
            [doc(doc_id="doc-1", session_id="session-1", text="Cached dashboard work.", ts=1)]
        )
        results = ss.recent_session_results(conn, 1)
        with mock.patch.object(
            ss,
            "llm_summarize",
            side_effect=["Summarized the cached work.", "Resume the cached work."],
        ):
            self.assertEqual(ss.catch_up_dashboard_cards(conn, results, limit=10), 1)

        with mock.patch.object(ss, "llm_summarize") as llm:
            self.assertEqual(ss.catch_up_dashboard_cards(conn, results, limit=10), 0)
        llm.assert_not_called()
        conn.close()

    def test_dashboard_refresh_only_reads_changed_claude_files_and_recent_codex(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="existing",
                    source="codex",
                    session_id="existing",
                    role="user",
                    text="Existing Codex prompt",
                    ts=10_000,
                )
            ]
        )
        recent = doc(
            doc_id="recent",
            source="codex",
            session_id="recent",
            role="user",
            text="Recent Codex prompt",
            ts=10_500,
        )
        old = doc(
            doc_id="old",
            source="codex",
            session_id="old",
            role="user",
            text="Old Codex prompt",
            ts=1,
        )
        with mock.patch.object(ss, "codex_thread_context", return_value={}), mock.patch.object(
            ss, "iter_codex_threads", return_value=iter(())
        ), mock.patch.object(ss, "iter_codex_history", return_value=iter((old, recent))):
            updates = list(ss.iter_dashboard_updates(conn, pathlib.Path("/missing-home")))
        self.assertEqual([item.doc_id for item in updates], ["recent"])
        conn.close()

    def test_dashboard_lists_global_sessions_and_resume_commands(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="claude-user",
                    source="claude",
                    session_id="claude-session",
                    title="Claude planning work",
                    cwd="/tmp/claude-work",
                    role="user",
                    text="Finish the Claude planning work.",
                    ts=200,
                ),
                doc(
                    doc_id="codex-user",
                    source="codex",
                    session_id="codex-session",
                    title="Codex implementation",
                    cwd="/tmp/codex-work",
                    role="user",
                    text="Continue the Codex implementation.",
                    ts=100,
                ),
            ]
        )
        results = ss.recent_session_results(conn, 10)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ss.print_dashboard(conn, results)
        text = out.getvalue()
        # The sparse work map replaced the table dashboard: project headers,
        # About/State/Resume blocks, and per-thread open numbers.
        self.assertIn("About: Claude planning work.", text)
        self.assertIn("About: Codex implementation.", text)
        self.assertIn("Open: ss open 1", text)
        self.assertIn("Open: ss open 2", text)
        self.assertIn("open with: ss open N", text)
        self.assertNotIn("SS WORK MAP", text)
        conn.close()

    def test_dashboard_excludes_injected_context_as_last_user_message(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="real",
                    source="codex",
                    session_id="session",
                    role="user",
                    text="Resume the actual work.",
                    ts=100,
                ),
                doc(
                    doc_id="injected",
                    source="codex",
                    session_id="session",
                    role="user",
                    text="# AGENTS.md instructions for /Users/alex/projects",
                    ts=200,
                ),
            ]
        )
        row = conn.execute("SELECT * FROM documents WHERE doc_id = 'injected'").fetchone()
        rows = ss.session_rows(conn, row)
        self.assertEqual(ss.last_user_message(rows, row), "Resume the actual work.")
        conn.close()

    def test_last_user_message_excludes_expanded_skill_instructions(self):
        rows = [
            make_row(doc_id="real", text="Find my actual session.", ts=100),
            make_row(doc_id="skill", text="Base directory for this skill: /Users/alex/.claude/skills/session", ts=200),
        ]
        self.assertEqual(ss.last_user_message(rows, rows[-1]), "Find my actual session.")

    def test_dashboard_recent_requests_exclude_injected_and_duplicate_messages(self):
        rows = [
            make_row(doc_id="old", text="Start the dashboard repair.", ts=100),
            make_row(doc_id="duplicate", text="Start the dashboard repair.", ts=150),
            make_row(doc_id="skill", text="Base directory for this skill: /tmp/skill", ts=175),
            make_row(doc_id="latest", text="Show enough context to identify the session.", ts=200),
        ]
        self.assertEqual(
            ss.recent_user_messages(rows, rows[-1]),
            ["Show enough context to identify the session.", "Start the dashboard repair."],
        )

    def test_close_session_language_is_explicit(self):
        self.assertTrue(ss.is_close_session_message("Name and summarize this session and close it down."))
        self.assertTrue(ss.is_close_session_message("Archive this session for good."))
        self.assertTrue(ss.is_close_session_message("Let's close this out for good."))
        self.assertFalse(ss.is_close_session_message("Name and summarize this session."))
        self.assertFalse(ss.is_close_session_message("Close the ranking plan."))
        self.assertFalse(
            ss.is_close_session_message(
                'I want something where, if I say "name and summarize this session and close it down," it archives it.'
            )
        )

    def test_detected_archive_state_uses_latest_direct_user_message(self):
        conn = make_conn_with_docs(
            [
                doc(doc_id="older", session_id="closed", text="Keep working.", ts=100),
                doc(
                    doc_id="close",
                    session_id="closed",
                    text="Name and summarize this session and close it down.",
                    ts=200,
                ),
                doc(doc_id="active", session_id="active", text="Name and summarize this session.", ts=300),
            ]
        )
        self.assertEqual(ss.sync_detected_archive_states(conn), 1)
        self.assertTrue(ss.session_is_archived(conn, "codex", "closed"))
        self.assertFalse(ss.session_is_archived(conn, "codex", "active"))
        conn.close()

    def test_recent_dashboard_separates_active_and_archived_sessions(self):
        conn = make_conn_with_docs(
            [
                doc(doc_id="active", session_id="active", text="Continue active work.", ts=100),
                doc(doc_id="archived", session_id="archived", text="Finished work.", ts=200),
            ]
        )
        ss.set_session_archive_status(conn, "codex", "archived", True, "manual")
        conn.commit()
        active = ss.recent_session_results(conn, 10)
        archived = ss.recent_session_results(conn, 10, archived_only=True)
        self.assertEqual([row[0]["session_id"] for row in active], ["active"])
        self.assertEqual([row[0]["session_id"] for row in archived], ["archived"])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ss.print_dashboard(conn, archived, archived_view=True)
        self.assertIn("SS archived", out.getvalue())
        self.assertIn("[ARCHIVED]", out.getvalue())
        conn.close()

    def test_search_results_label_archived_sessions(self):
        conn = make_conn_with_docs(
            [doc(doc_id="archived", session_id="archived", text="Finished archive test.", ts=200)]
        )
        ss.set_session_archive_status(conn, "codex", "archived", True, "manual")
        conn.commit()
        row = conn.execute("SELECT * FROM documents WHERE doc_id = 'archived'").fetchone()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ss.print_results(conn, [(row, 1.0, "fts")], "archive test")
        self.assertIn("1. [ARCHIVED]", out.getvalue())
        conn.close()

    def test_archived_provider_is_not_limited_to_recent_session_window(self):
        docs = [
            doc(doc_id=f"recent-{i}", session_id=f"recent-{i}", text=f"Active {i}", ts=1_000 + i)
            for i in range(60)
        ]
        docs.append(doc(doc_id="old-archived", session_id="old-archived", text="Old archive", ts=1))
        conn = make_conn_with_docs(docs)
        ss.set_session_archive_status(
            conn,
            "codex",
            "old-archived",
            True,
            "manual",
            status_at=2_000,
        )
        conn.commit()
        results, missing = ss.archived_session_results(conn, 10)
        self.assertEqual(missing, 0)
        self.assertEqual([row[0]["session_id"] for row in results], ["old-archived"])
        conn.close()

    def test_archived_provider_reports_status_without_indexed_document(self):
        conn = make_conn_with_docs([])
        ss.set_session_archive_status(conn, "codex", "missing", True, "manual")
        conn.commit()
        results, missing = ss.archived_session_results(conn, 10)
        self.assertEqual(results, [])
        self.assertEqual(missing, 1)
        conn.close()

    def test_manual_unarchive_blocks_older_detected_close(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="close",
                    session_id="closed",
                    text="Name and summarize this session and close it down.",
                    ts=200,
                )
            ]
        )
        ss.set_session_archive_status(
            conn,
            "codex",
            "closed",
            False,
            "manual",
            status_at=300,
        )
        conn.commit()
        self.assertEqual(ss.sync_detected_archive_states(conn), 0)
        self.assertFalse(ss.session_is_archived(conn, "codex", "closed"))
        conn.close()

    def test_neutral_message_after_close_does_not_reactivate_session(self):
        conn = make_conn_with_docs(
            [
                doc(doc_id="close", session_id="state", text="Close this session.", ts=100),
                doc(doc_id="thanks", session_id="state", text="Thanks, that looks good.", ts=200),
            ]
        )
        self.assertEqual(ss.sync_detected_archive_states(conn), 1)
        self.assertTrue(ss.session_is_archived(conn, "codex", "state"))
        self.assertEqual(ss.sync_detected_archive_states(conn), 0)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM session_archive_events WHERE session_id = 'state'"
            ).fetchone()[0],
            1,
        )
        conn.close()

    def test_explicit_resume_reactivates_archived_session(self):
        conn = make_conn_with_docs(
            [
                doc(doc_id="close", session_id="state", text="Close this session.", ts=100),
                doc(doc_id="resume", session_id="state", text="Resume this session.", ts=200),
            ]
        )
        self.assertEqual(ss.sync_detected_archive_states(conn), 2)
        self.assertFalse(ss.session_is_archived(conn, "codex", "state"))
        events = conn.execute(
            "SELECT intent FROM session_archive_events WHERE session_id = 'state' ORDER BY event_ts"
        ).fetchall()
        self.assertEqual([row["intent"] for row in events], ["close", "resume"])
        conn.close()

    def test_parser_upgrade_repairs_old_automatic_false_positive(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="meta",
                    session_id="state",
                    text="Explain how the close session detector works.",
                    ts=100,
                )
            ]
        )
        ss.set_session_archive_status(
            conn,
            "codex",
            "state",
            True,
            "detected-close",
            evidence="Explain how the close session detector works.",
            evidence_ts=100,
            evidence_doc_id="meta",
            parser_version=1,
        )
        conn.commit()
        self.assertEqual(ss.sync_detected_archive_states(conn), 1)
        status = ss.session_archive_row(conn, "codex", "state")
        self.assertFalse(status["archived"])
        self.assertEqual(status["origin"], "migration-repair")
        conn.close()

    def test_dashboard_synthesizes_state_resume_and_fallback_clue(self):
        rows = [
            make_row(
                doc_id="older",
                title="Osmo x402: curriculum import",
                text="Recover the Osmo x402 work after the restart.",
                ts=100,
            ),
            make_row(
                doc_id="latest",
                title="Osmo x402: curriculum import",
                text=(
                    "Osmo x402: The curriculum-import architecture was decided. "
                    "The x402 pilot still needed its first complete lesson/card/grade cycle "
                    "before bulk agentic-commerce import. i want this one"
                ),
                ts=200,
            ),
        ]
        card = ss.SessionCard(
            title="22/07/26 12:14:46 — Osmo x402: curriculum import",
            source="codex",
            session_id="osmo",
            repo="/Users/alex/projects",
            last_active=200,
            last_user_message=rows[-1]["text"],
            what_this_was="Osmo x402: The curriculum-import architecture was decided.",
            what_happened="",
            next_clue="before bulk agentic-commerce import. i want this one",
            mentioned_paths=(),
            evidence=(),
        )
        about, state, resume, clue = ss.dashboard_summary(card, rows, rows[-1])
        # This card is regex evidence, not a model summary, so About stays the
        # session topic and the evidence line remains available as state.
        self.assertEqual(about, "Osmo x402.")
        self.assertEqual(state, "Osmo x402: The curriculum-import architecture was decided.")
        self.assertEqual(
            resume,
            "The x402 pilot still needed its first complete lesson/card/grade cycle before bulk agentic-commerce import.",
        )
        # The work map dropped key-terms clues: a clue is shown only when a
        # real path adds information.
        self.assertEqual(clue, "")

    def test_dashboard_about_prefers_cached_llm_summary_over_title(self):
        rows = [
            make_row(
                doc_id="only",
                title="Two answers",
                text="alex@mac ~ % ss --limit 8\nTwo answers.",
                ts=100,
            ),
        ]
        card = ss.SessionCard(
            title="22/07/26 12:14:46 — Two answers",
            source="claude",
            session_id="cards",
            repo="/Users/alex/workspace/os/_shared/session-search",
            last_active=100,
            last_user_message=rows[-1]["text"],
            what_this_was="Cached the one-line session summaries so the dashboard stops calling the LLM.",
            what_happened="",
            next_clue="Fix the candidate title leak.",
            mentioned_paths=(),
            evidence=(),
            summary_source=ss.SUMMARY_SOURCE_LLM,
        )
        about, _state, _resume, _clue = ss.dashboard_summary(card, rows, rows[-1])
        self.assertIn("cached the one-line session summaries", about.lower())
        self.assertNotIn("two answers", about.lower())

    def test_backfill_stops_at_the_horizon(self):
        conn = make_conn_with_docs(
            [
                doc(doc_id=f"d{i}", session_id=f"s{i}", title=f"S{i}", text=f"Work {i}.", ts=i)
                for i in range(8)
            ]
        )
        with mock.patch.object(ss, "llm_summarize", return_value="Summarized the work."):
            with mock.patch.object(ss, "llm_available", return_value=True):
                built = ss.ensure_session_cards(conn, horizon=3)
        self.assertEqual(built, 3, "backfill must not summarize every session it can see")
        newest = {row[0] for row in conn.execute("SELECT session_id FROM session_cards")}
        self.assertEqual(newest, {"s7", "s6", "s5"}, "the horizon must cover the newest sessions")
        conn.close()

    def test_a_session_past_the_horizon_is_summarized_when_searched(self):
        conn = make_conn_with_docs(
            [
                doc(doc_id=f"d{i}", session_id=f"s{i}", title=f"S{i}", text=f"Work {i}.", ts=i)
                for i in range(8)
            ]
        )
        with mock.patch.object(ss, "llm_summarize", return_value="Summarized the work."):
            with mock.patch.object(ss, "llm_available", return_value=True):
                    ss.ensure_session_cards(conn, horizon=3)
                    old = conn.execute("SELECT * FROM documents WHERE session_id = 's0'").fetchone()
                    self.assertIsNone(
                        conn.execute(
                            "SELECT 1 FROM session_cards WHERE session_id = 's0'"
                        ).fetchone()
                    )
                    card = ss.session_card_for_result(conn, old, "work", persist=True)
        self.assertEqual(card.summary_source, ss.SUMMARY_SOURCE_LLM)
        self.assertIsNotNone(
            conn.execute("SELECT 1 FROM session_cards WHERE session_id = 's0'").fetchone(),
            "an on-demand summary must be cached so the text is sent once",
        )
        conn.close()

    def test_the_search_path_itself_caches_an_on_demand_summary(self):
        # Asserting the helper caches is not enough; the production search path
        # must ask it to. If print_results stopped persisting, every search
        # would re-send the same session text forever.
        conn = make_conn_with_docs(
            [doc(doc_id="d0", session_id="s0", title="S0", text="Rebuilt the exporter.", ts=1)]
        )
        results = ss.recent_session_results(conn, 1)
        with mock.patch.object(ss, "llm_summarize", return_value="Rebuilt the exporter.") as llm:
            with mock.patch.object(ss, "llm_available", return_value=True):
                with contextlib.redirect_stdout(io.StringIO()):
                    ss.print_results(conn, results, "exporter")
                first = llm.call_count
                self.assertGreater(first, 0, "the first search must build the card")
                with contextlib.redirect_stdout(io.StringIO()):
                    ss.print_results(conn, results, "exporter")
                self.assertEqual(llm.call_count, first, "a repeated search must not re-send the text")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session_cards").fetchone()[0], 1)
        conn.close()

    def test_backfill_defaults_to_the_configured_horizon(self):
        # Every other horizon test passes one explicitly, so without this the
        # default binding could break and routine backfill would quietly go
        # back to summarizing every session in the index.
        conn = make_conn_with_docs([doc(doc_id="d0", session_id="s0", text="Work.", ts=1)])
        with mock.patch.object(ss, "session_groups", return_value=[]) as groups:
            ss.ensure_session_cards(conn)
        self.assertEqual(groups.call_args.kwargs.get("limit"), ss.CARD_BACKFILL_SESSIONS)
        conn.close()

    def test_the_horizon_covers_every_row_the_dashboard_can_show(self):
        # cmd_dashboard asks for max(args.limit, 50) sessions. A horizon at or
        # below that would leave visible rows permanently unsummarized.
        self.assertGreater(ss.CARD_BACKFILL_SESSIONS, 50)

    def test_edited_session_text_invalidates_the_cached_card(self):
        conn = make_conn_with_docs([doc(doc_id="d1", session_id="s1", text="Rebuilt it.", ts=1)])
        results = ss.recent_session_results(conn, 1)
        with mock.patch.object(ss, "llm_summarize", return_value="Rebuilt the exporter."):
            with mock.patch.object(ss, "llm_available", return_value=True):
                self.assertEqual(ss.catch_up_dashboard_cards(conn, results), 1)
                # Same session, new text: the cached summary is now wrong.
                ss.upsert_documents(
                    conn, [doc(doc_id="d1", session_id="s1", text="Rewrote the payout retry.", ts=1)]
                )
                results = ss.recent_session_results(conn, 1)
                with mock.patch.object(ss, "llm_summarize", return_value="Rewrote the payout retry."):
                    self.assertEqual(ss.catch_up_dashboard_cards(conn, results), 1)
        self.assertEqual(
            conn.execute("SELECT what_this_was FROM session_cards").fetchone()[0],
            "Rewrote the payout retry.",
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session_cards").fetchone()[0], 1)
        conn.close()

    def test_dashboard_never_starts_summary_rebuilds(self):
        conn = make_conn_with_docs(
            [
                doc(doc_id=f"d{i}", session_id=f"s{i}", title=f"S{i}", text=f"Work {i}.", ts=i)
                for i in range(12)
            ]
        )
        results = ss.recent_session_results(conn, 12)
        with mock.patch.object(ss, "catch_up_dashboard_cards", return_value=0) as catch_up:
            with contextlib.redirect_stdout(io.StringIO()):
                ss.print_dashboard(conn, results)
        catch_up.assert_not_called()
        conn.close()

    def test_catch_up_counts_each_session_once(self):
        conn = make_conn_with_docs(
            [
                doc(doc_id="d1", session_id="s1", text="First turn.", ts=1),
                doc(doc_id="d2", session_id="s1", text="Second turn.", ts=2),
            ]
        )
        results = ss.recent_session_results(conn, 10)
        duplicated = [*results, *results]
        with mock.patch.object(ss, "llm_summarize", return_value="Rebuilt it."):
            with mock.patch.object(ss, "llm_available", return_value=True):
                built = ss.catch_up_dashboard_cards(conn, duplicated, limit=10)
        self.assertEqual(built, 1)
        conn.close()

    def test_a_query_result_costs_no_model_calls_beyond_the_base_card(self):
        conn = make_conn_with_docs(
            [doc(doc_id="d1", session_id="s1", text="Rebuilt the billing exporter.", ts=1)]
        )
        row = conn.execute("SELECT * FROM documents WHERE doc_id = 'd1'").fetchone()
        with mock.patch.object(ss, "llm_summarize", return_value="Rebuilt the billing exporter.") as llm:
            with mock.patch.object(ss, "llm_available", return_value=True):
                ss.session_card_for_result(conn, row, "billing", persist=True)
                first = llm.call_count
                self.assertEqual(first, 2, "base card should cost exactly one About and one Next call")
                card = ss.session_card_for_result(conn, row, "billing", persist=True)
                self.assertEqual(llm.call_count, first, "a warm query result must cost nothing")
                ss.session_card_for_result(conn, row, "exporter", persist=False)
                self.assertEqual(llm.call_count, first, "ss show must not re-pay per invocation")
        self.assertEqual(card.summary_source, ss.SUMMARY_SOURCE_LLM)
        self.assertEqual(card.what_this_was, "Rebuilt the billing exporter.")
        conn.close()

    def test_a_query_result_keeps_query_specific_evidence(self):
        conn = make_conn_with_docs(
            [
                doc(doc_id="d1", session_id="s1", text="Rebuilt the billing exporter.", ts=1),
                doc(doc_id="d2", session_id="s1", text="Next I need to fix the payout retry.", ts=2),
            ]
        )
        row = conn.execute("SELECT * FROM documents WHERE doc_id = 'd1'").fetchone()
        with mock.patch.object(ss, "llm_summarize", return_value=""):
            with mock.patch.object(ss, "llm_available", return_value=False):
                card = ss.session_card_for_result(conn, row, "payout retry", persist=False)
        self.assertEqual(card.summary_source, ss.SUMMARY_SOURCE_EVIDENCE)
        self.assertTrue(card.evidence)
        conn.close()

    def test_dashboard_never_shows_raw_evidence_as_about(self):
        rows = [
            make_row(
                doc_id="only",
                title="Osmo x402: curriculum import",
                text="i asked you to reopen",
                ts=100,
            ),
        ]
        card = ss.SessionCard(
            title="22/07/26 12:14:46 — Osmo x402: curriculum import",
            source="codex",
            session_id="osmo",
            repo="/Users/alex/projects",
            last_active=100,
            last_user_message=rows[-1]["text"],
            # An uncached card carries the raw evidence line here.
            what_this_was="i asked you to reopen",
            what_happened="",
            next_clue="",
            mentioned_paths=(),
            evidence=(),
            summary_source=ss.SUMMARY_SOURCE_EVIDENCE,
        )
        about, _state, _resume, _clue = ss.dashboard_summary(card, rows, rows[-1])
        self.assertEqual(about, "Osmo x402.")
        self.assertNotIn("asked you to reopen", about)

    def test_catch_up_rebuilds_an_evidence_card_when_a_key_exists(self):
        conn = make_conn_with_docs([doc(doc_id="d1", session_id="s1", text="Rebuilt it.", ts=1)])
        results = ss.recent_session_results(conn, 1)
        with mock.patch.object(ss, "llm_summarize", return_value=""):
            with mock.patch.object(ss, "llm_available", return_value=True):
                self.assertEqual(ss.catch_up_dashboard_cards(conn, results), 1)
        self.assertEqual(
            conn.execute("SELECT summary_source FROM session_cards").fetchone()[0],
            ss.SUMMARY_SOURCE_EVIDENCE,
        )
        with mock.patch.object(ss, "llm_summarize", return_value="Rebuilt the ATS collector."):
            with mock.patch.object(ss, "llm_available", return_value=True):
                self.assertEqual(ss.catch_up_dashboard_cards(conn, results), 1)
        row = conn.execute("SELECT summary_source, what_this_was FROM session_cards").fetchone()
        self.assertEqual(row[0], ss.SUMMARY_SOURCE_LLM)
        self.assertEqual(row[1], "Rebuilt the ATS collector.")
        conn.close()

    def test_catch_up_does_not_retry_evidence_cards_without_a_key(self):
        conn = make_conn_with_docs([doc(doc_id="d1", session_id="s1", text="Rebuilt it.", ts=1)])
        results = ss.recent_session_results(conn, 1)
        with mock.patch.object(ss, "llm_available", return_value=False):
            with mock.patch.object(ss, "llm_summarize", return_value="") as llm:
                ss.catch_up_dashboard_cards(conn, results)
                first_calls = llm.call_count
                self.assertEqual(ss.catch_up_dashboard_cards(conn, results), 0)
                self.assertEqual(llm.call_count, first_calls)
        conn.close()

    def test_missing_api_key_is_reported_once_not_silently_swallowed(self):
        conn = make_conn_with_docs([doc(doc_id="d1", session_id="s1", text="Rebuilt it.", ts=1)])
        results = ss.recent_session_results(conn, 1)
        stderr = io.StringIO()
        with mock.patch.object(ss, "llm_available", return_value=False):
            with mock.patch.object(ss, "_LLM_WARNED", False):
                with contextlib.redirect_stderr(stderr):
                    with contextlib.redirect_stdout(io.StringIO()):
                        ss.catch_up_dashboard_cards(conn, results, quiet=False)
                        ss.catch_up_dashboard_cards(conn, results, quiet=False)
        self.assertEqual(stderr.getvalue().count("OPENROUTER_API_KEY"), 1)
        conn.close()

    def test_card_records_llm_provenance_when_the_model_answers(self):
        rows = [make_row(doc_id="d1", title="Two answers", text="Rebuilt the collector.", ts=1)]
        with mock.patch.object(ss, "llm_summarize", return_value="Rebuilt the ATS collector end to end."):
            card = ss.build_session_card(rows, rows[0], "", use_llm=True)
        self.assertEqual(card.summary_source, ss.SUMMARY_SOURCE_LLM)

    def test_card_records_evidence_provenance_when_the_model_fails(self):
        rows = [make_row(doc_id="d1", title="Two answers", text="Rebuilt the collector.", ts=1)]
        with mock.patch.object(ss, "llm_summarize", return_value=""):
            card = ss.build_session_card(rows, rows[0], "", use_llm=True)
        self.assertEqual(card.summary_source, ss.SUMMARY_SOURCE_EVIDENCE)

    def test_card_records_evidence_when_only_the_next_leg_answers(self):
        rows = [make_row(doc_id="d1", title="Two answers", text="Rebuilt the collector.", ts=1)]

        def summarize(prompt: str, max_tokens: int = 220) -> str:
            return "" if prompt.startswith("Summarize what this session was about") else "Ship it."

        with mock.patch.object(ss, "llm_summarize", side_effect=summarize):
            card = ss.build_session_card(rows, rows[0], "", use_llm=True)
        self.assertEqual(card.summary_source, ss.SUMMARY_SOURCE_EVIDENCE)
        self.assertEqual(card.next_clue, "Ship it.")

    def test_card_without_llm_is_never_marked_llm(self):
        rows = [make_row(doc_id="d1", title="Two answers", text="Rebuilt the collector.", ts=1)]
        card = ss.build_session_card(rows, rows[0], "", use_llm=False)
        self.assertEqual(card.summary_source, ss.SUMMARY_SOURCE_EVIDENCE)

    def test_provenance_survives_the_card_cache_round_trip(self):
        conn = make_conn_with_docs([doc(doc_id="d1", session_id="s1", text="Rebuilt it.", ts=1)])
        row = conn.execute("SELECT * FROM documents WHERE doc_id = 'd1'").fetchone()
        rows = ss.session_rows(conn, row)
        with mock.patch.object(ss, "llm_summarize", return_value="Rebuilt the ATS collector."):
            card = ss.build_session_card(rows, row, "", use_llm=True)
        with conn:
            ss.store_session_card(conn, card, ss.session_card_hash(rows))
        cached = conn.execute("SELECT * FROM session_cards").fetchone()
        self.assertEqual(ss.card_from_cache_row(cached).summary_source, ss.SUMMARY_SOURCE_LLM)
        conn.close()

    def test_document_title_prefers_session_fallback_over_untitled(self):
        title = ss.document_title_with_fallback(
            "", "I need you to fix the thing.\nraw-only-secret", "Northstar deploy review"
        )
        self.assertEqual(title, "Northstar deploy review")

    def test_document_title_ignores_machine_fallback(self):
        title = ss.document_title_with_fallback(
            "", "I need you to fix the thing.\nraw-only-secret", "0f9c2b1a4d5e6f708192a3b4"
        )
        self.assertEqual(title, "(untitled session)")
        self.assertNotIn("raw-only-secret", title)

    def test_card_about_line_keeps_first_sentence_without_ellipsis(self):
        long_about = (
            "This session focused on transitioning the Lakeside Clinic cash lead-generation strategy "
            "into a repeatable corpus-backed pipeline that produces landing pages, story pools, "
            "and angle libraries without hand-written copy for every single campaign variant. "
            "A second sentence that must be dropped."
        )
        line = ss.card_about_line(long_about)
        self.assertFalse(line.endswith("..."))
        self.assertNotIn("second sentence", line.lower())
        self.assertTrue(line.startswith("This session focused on transitioning"))
        self.assertNotEqual(clean_card_field(line, 180), "")

    def test_card_about_line_does_not_split_on_abbreviations(self):
        # Each input is ONE sentence containing an internal period. The whole
        # sentence must survive, including the words after the abbreviation,
        # otherwise the split happened where it must not.
        cases = {
            "Dr. Smith fixed the reminder pipeline and shipped it to production today.":
                "production today",
            "Rewrote the collector, e.g. the ATS feed, so it retries failed pages cleanly.":
                "retries failed pages cleanly",
            "The scraping lane was rebuilt to compare Camoufox vs. Playwright end to end.":
                "end to end",
            "A long enough clause about the exporter and Dr. Smith who signed it off.":
                "who signed it off",
        }
        for text, tail in cases.items():
            line = ss.card_about_line(text)
            self.assertIn(tail, line, f"abbreviation split dropped the tail: {line!r}")
            self.assertEqual(line, text, f"sentence was altered: {line!r}")
            self.assertNotEqual(clean_card_field(line, 180), "")

    def test_prompt_echo_never_becomes_the_summary(self):
        echoed = (
            "Summarize what this session was about in exactly 1 sentence. "
            "The user debugged the ATS collector and fixed the Greenhouse 403s."
        )
        rows = [make_row(doc_id="d1", title="Two answers", text="Debugged the collector.", ts=1)]
        with mock.patch.object(ss, "llm_summarize", return_value=echoed):
            card = ss.build_session_card(rows, rows[0], "", use_llm=True)
        self.assertIn("Greenhouse", card.what_this_was)
        self.assertNotIn("exactly 1 sentence", card.what_this_was)
        self.assertEqual(card.summary_source, ss.SUMMARY_SOURCE_LLM)

    def test_a_real_summary_about_summaries_is_not_stripped(self):
        # The obvious over-strip: a session whose actual topic is summarization
        # shares most of its vocabulary with the instruction.
        for answer in (
            "This session rewrote the summary prompt so each session card gets exactly one sentence.",
            "The session was about ignoring shell prompts in the summary pipeline.",
        ):
            line = ss.card_about_line(ss.strip_prompt_echo(answer, ss.ABOUT_INSTRUCTION))
            self.assertNotEqual(line, "", f"a real summary was stripped: {answer!r}")
            self.assertIn("session", line.lower())

    def test_card_about_line_skips_a_trivial_leading_sentence(self):
        # A model that opens with an acknowledgement must not have that become
        # the whole summary. The length floor, not the abbreviation rule, is
        # what rejects these heads.
        for text, wanted in (
            ("Done. The session rebuilt the billing exporter end to end.", "billing exporter"),
            ("Sure. The collector now retries Greenhouse pages that return 403.", "Greenhouse"),
        ):
            line = ss.card_about_line(text)
            self.assertIn(wanted, line, f"summary collapsed to the opener: {line!r}")

    def test_card_about_line_repairs_both_ellipsis_forms(self):
        for tail in ("...", "…", " ...", "…  "):
            text = "Rebuilt the ATS collector so Greenhouse stops returning 403" + tail
            line = ss.card_about_line(text)
            self.assertNotEqual(
                clean_card_field(line, 180), "", f"gate rejected the {tail!r} form: {line!r}"
            )
            self.assertIn("Greenhouse", line)

    def test_truncation_is_defined_in_exactly_one_place(self):
        import card_quality

        self.assertIs(ss.TRUNCATED_RE, card_quality.TRUNCATED_RE)

    def test_card_about_line_rejects_content_free_answers(self):
        for text in ("…", "...", "  ", "---"):
            self.assertEqual(ss.card_about_line(text), "")

    def test_card_about_line_survives_a_single_overlong_sentence(self):
        overlong = "This session " + ("rebuilt the cached summary pipeline " * 20) + "end."
        line = ss.card_about_line(overlong)
        self.assertFalse(line.endswith("..."))
        self.assertNotEqual(clean_card_field(line, 180), "")

    def test_dashboard_about_falls_back_to_title_without_cached_summary(self):
        rows = [
            make_row(
                doc_id="only",
                title="Osmo x402: curriculum import",
                text="Osmo x402: the curriculum import was decided.",
                ts=100,
            ),
        ]
        card = ss.SessionCard(
            title="22/07/26 12:14:46 — Osmo x402: curriculum import",
            source="codex",
            session_id="osmo",
            repo="/Users/alex/projects",
            last_active=100,
            last_user_message=rows[-1]["text"],
            what_this_was="",
            what_happened="",
            next_clue="",
            mentioned_paths=(),
            evidence=(),
        )
        about, _state, _resume, _clue = ss.dashboard_summary(card, rows, rows[-1])
        self.assertEqual(about, "Osmo x402.")

    def test_dashboard_prompt_opens_selected_session(self):
        args = ss.argparse.Namespace(db=":memory:", limit=10, source="all", mode="hybrid")
        with mock.patch.object(ss, "dashboard_is_interactive", return_value=True), mock.patch(
            "builtins.input", return_value="2"
        ), mock.patch.object(ss, "cmd_resume", return_value=0) as resume:
            self.assertEqual(ss.dashboard_prompt(args), 0)
        resume.assert_called_once()
        self.assertEqual(resume.call_args.args[0].selector, "2")

    def test_dashboard_prompt_searches_plain_text(self):
        args = ss.argparse.Namespace(db=":memory:", limit=10, source="all", mode="hybrid")
        with mock.patch.object(ss, "dashboard_is_interactive", return_value=True), mock.patch(
            "builtins.input", return_value="what is this"
        ), mock.patch.object(ss, "cmd_search", return_value=0) as search:
            self.assertEqual(ss.dashboard_prompt(args), 0)
        search.assert_called_once()
        self.assertEqual(search.call_args.args[0].query, "what is this")

    def test_dashboard_prompt_keeps_slash_search_compatibility(self):
        args = ss.argparse.Namespace(db=":memory:", limit=10, source="all", mode="hybrid")
        with mock.patch.object(ss, "dashboard_is_interactive", return_value=True), mock.patch(
            "builtins.input", return_value="/what is this"
        ), mock.patch.object(ss, "cmd_search", return_value=0) as search:
            self.assertEqual(ss.dashboard_prompt(args), 0)
        self.assertEqual(search.call_args.args[0].query, "what is this")

    def test_dashboard_prompt_routes_full_ss_look_command(self):
        args = ss.argparse.Namespace(db=":memory:", limit=10, source="all", mode="hybrid")
        with mock.patch.object(ss, "dashboard_is_interactive", return_value=True), mock.patch(
            "builtins.input", return_value="ss look at 4"
        ), mock.patch.object(ss, "cmd_show", return_value=0) as show:
            self.assertEqual(ss.dashboard_prompt(args), 0)
        show.assert_called_once()
        self.assertEqual(show.call_args.args[0].doc_id, "4")
        self.assertEqual(show.call_args.args[0].db, ":memory:")

    def test_dashboard_prompt_routes_full_ss_open_command(self):
        args = ss.argparse.Namespace(db=":memory:", limit=10, source="all", mode="hybrid")
        with mock.patch.object(ss, "dashboard_is_interactive", return_value=True), mock.patch(
            "builtins.input", return_value="ss open 4"
        ), mock.patch.object(ss, "cmd_continue", return_value=0) as opened:
            self.assertEqual(ss.dashboard_prompt(args), 0)
        opened.assert_called_once()
        self.assertEqual(opened.call_args.args[0].selector, "4")

    def test_dashboard_prompt_routes_archive_command(self):
        args = ss.argparse.Namespace(db=":memory:", limit=10, source="all", mode="hybrid")
        with mock.patch.object(ss, "dashboard_is_interactive", return_value=True), mock.patch(
            "builtins.input", return_value="ss archive 4"
        ), mock.patch.object(ss, "cmd_set_archive", return_value=0) as archive:
            self.assertEqual(ss.dashboard_prompt(args), 0)
        archive.assert_called_once()
        self.assertEqual(archive.call_args.args[0].selector, "4")
        self.assertTrue(archive.call_args.args[0].archived)

    def test_archive_followup_parser_supports_manual_correction(self):
        archived = ss.parse_natural_followup(["archive", "4"])
        unarchived = ss.parse_natural_followup(["unarchive", "4"])
        self.assertIs(archived.func, ss.cmd_set_archive)
        self.assertTrue(archived.archived)
        self.assertFalse(unarchived.archived)

    def test_open_unarchives_after_selector_resolution(self):
        conn = make_conn_with_docs(
            [doc(doc_id="archived", session_id="archived", text="Closed work.", ts=100)]
        )
        ss.set_session_archive_status(conn, "codex", "archived", True, "manual")
        conn.commit()
        row = conn.execute("SELECT * FROM documents WHERE doc_id = 'archived'").fetchone()
        observed = {}

        def capture(*_args):
            observed["archived"] = ss.session_is_archived(conn, "codex", "archived")
            observed["event"] = conn.execute(
                "SELECT origin FROM session_archive_events WHERE origin = 'cli-open'"
            ).fetchone()

        with mock.patch.object(ss, "session_lock", return_value=contextlib.nullcontext()) as lock, mock.patch.object(
            ss, "connect_selector_db", return_value=(pathlib.Path(":memory:"), conn)
        ), mock.patch.object(ss, "selected_row", return_value=row), mock.patch.object(
            ss, "query_from_selector", return_value=""
        ), mock.patch.object(ss, "print_resume_instructions", side_effect=capture):
            self.assertEqual(ss.cmd_resume(ss.argparse.Namespace(db=":memory:", selector="1")), 0)
        lock.assert_called_once_with(shared=False)
        self.assertFalse(observed["archived"])
        self.assertIsNotNone(observed["event"])

    def test_continue_unarchives_before_native_resume_output(self):
        conn = make_conn_with_docs(
            [doc(doc_id="archived", session_id="archived", text="Closed work.", ts=100)]
        )
        ss.set_session_archive_status(conn, "codex", "archived", True, "manual")
        conn.commit()
        row = conn.execute("SELECT * FROM documents WHERE doc_id = 'archived'").fetchone()
        observed = {}

        def capture(*_args):
            observed["archived"] = ss.session_is_archived(conn, "codex", "archived")

        with mock.patch.object(ss, "session_lock", return_value=contextlib.nullcontext()) as lock, mock.patch.object(
            ss, "connect_selector_db", return_value=(pathlib.Path(":memory:"), conn)
        ), mock.patch.object(ss, "selected_row", return_value=row), mock.patch.object(
            ss, "query_from_selector", return_value=""
        ), mock.patch.object(ss, "print_resume_instructions", side_effect=capture):
            self.assertEqual(
                ss.cmd_continue(ss.argparse.Namespace(db=":memory:", selector="1", target="")),
                0,
            )
        lock.assert_called_once_with(shared=False)
        self.assertFalse(observed["archived"])

    def test_migration_audit_uses_complete_original_evidence(self):
        long_text = ("Background material. " * 30) + "Close this session."
        conn = make_conn_with_docs(
            [doc(doc_id="full", session_id="full", text=long_text, ts=100)]
        )
        ss.set_session_archive_status(
            conn,
            "codex",
            "full",
            True,
            "detected-close",
            evidence=long_text[:320],
            evidence_ts=100,
            evidence_doc_id="full",
            parser_version=2,
        )
        conn.commit()

        proposals, unresolved = ss.archive_migration_audit(conn)

        self.assertEqual(proposals, [])
        self.assertEqual(unresolved, [])
        conn.close()

    def test_migration_audit_preserves_missing_original_evidence_for_review(self):
        conn = make_conn_with_docs(
            [doc(doc_id="other", session_id="missing", text="Ordinary work.", ts=100)]
        )
        ss.set_session_archive_status(
            conn,
            "codex",
            "missing",
            True,
            "detected-close",
            evidence="Close this session.",
            evidence_ts=100,
            evidence_doc_id="gone",
            parser_version=2,
        )
        conn.commit()

        proposals, unresolved = ss.archive_migration_audit(conn)

        self.assertEqual(proposals, [])
        self.assertEqual(unresolved[0]["reason"], "original-evidence-missing")
        self.assertTrue(ss.session_is_archived(conn, "codex", "missing"))
        conn.close()

    def test_migration_audit_repairs_wrong_object_false_positive(self):
        conn = make_conn_with_docs(
            [doc(doc_id="wrong", session_id="wrong", text="Archive the notes for this session.", ts=100)]
        )
        ss.set_session_archive_status(
            conn,
            "codex",
            "wrong",
            True,
            "detected-close",
            evidence="Archive the notes for this session.",
            evidence_ts=100,
            evidence_doc_id="wrong",
            parser_version=2,
        )
        conn.commit()

        proposals, unresolved = ss.archive_migration_audit(conn)

        self.assertFalse(unresolved)
        self.assertEqual(proposals[0]["archived"], False)
        conn.close()

    def test_look_does_not_unarchive(self):
        conn = make_conn_with_docs(
            [doc(doc_id="archived", session_id="archived", text="Closed work.", ts=100)]
        )
        ss.set_session_archive_status(conn, "codex", "archived", True, "manual")
        conn.commit()
        row = conn.execute("SELECT * FROM documents WHERE doc_id = 'archived'").fetchone()
        with mock.patch.object(ss, "session_lock", return_value=contextlib.nullcontext()), mock.patch.object(
            ss, "connect_selector_db", return_value=(pathlib.Path(":memory:"), conn)
        ), mock.patch.object(ss, "selected_row", return_value=row), mock.patch.object(
            ss, "query_from_selector", return_value=""
        ), mock.patch.object(ss, "print_session_card_detail"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ss.cmd_show(ss.argparse.Namespace(db=":memory:", doc_id="1")), 0)
        self.assertTrue(ss.session_is_archived(conn, "codex", "archived"))
        conn.close()

    def test_result_summary_prints_last_user_message(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="user-last",
                    source="codex",
                    session_id="s-last",
                    title="Agentic commerce audit",
                    role="user",
                    text="Fix session names and preserve my last message.",
                    ts=1_700_000_050,
                ),
            ]
        )
        row = conn.execute("SELECT * FROM documents WHERE doc_id = 'user-last'").fetchone()
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            ss.print_results(conn, [(row, 1.0, "hybrid")], "session names")

        text = out.getvalue()
        self.assertIn("Last message from you: Fix session names", text)
        self.assertRegex(text, r"1\. \d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} — Agentic commerce audit")
        conn.close()

    def test_packet_only_source_explains_context_packet_path(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="vscode-1",
                    source="vscode",
                    session_id="copilot-1",
                    title="GitHub Copilot sign in",
                    text="am i signed into github copilot?",
                    ts=1,
                ),
            ]
        )
        row = conn.execute("SELECT * FROM documents WHERE doc_id = 'vscode-1'").fetchone()
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            ss.print_results(conn, [(row, 1.0, "hybrid")], "github copilot signed in")

        text = out.getvalue()
        self.assertIn("exact reopen is not available", text)
        self.assertIn("VS Code/Copilot exact chat reopen is not known yet", text)
        self.assertIn("ss continue 1 in codex", text)
        self.assertNotIn("score", text.lower())
        self.assertNotIn("match:", text.lower())
        conn.close()

    def test_related_result_lines_are_ranked_user_pointers(self):
        conn = make_conn_with_docs(
            [
                doc(
                    doc_id="claude",
                    source="claude",
                    session_id="s1",
                    title="Northstar Clinic Cloudflare deploy",
                    cwd="/tmp/Northstar",
                    text="Wait for Cloudflare Pages for Northstar Clinic.",
                    ts=1,
                ),
                doc(
                    doc_id="codex",
                    source="codex",
                    session_id="s2",
                    title="Northstar Clinic email env vars",
                    cwd="/tmp/Northstar",
                    text="Cloudflare Pages needs env vars before deploy.",
                    ts=2,
                ),
            ]
        )
        ss.set_session_archive_status(conn, "codex", "s2", True, "manual")
        conn.commit()
        rows = list(conn.execute("SELECT * FROM documents ORDER BY doc_id"))
        cards = [ss.result_card(conn, row, "Northstar Clinic cloudflare", "hybrid") for row in rows]

        related = ss.related_result_lines(cards, 1, "Northstar Clinic cloudflare")

        self.assertEqual(len(related), 1)
        self.assertIn("[Codex]", related[0])
        self.assertIn("[ARCHIVED]", related[0])
        self.assertIn("Northstar Clinic", related[0])
        conn.close()


if __name__ == "__main__":
    unittest.main()
