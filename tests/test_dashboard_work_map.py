"""Adversarial contracts for the restart work-map dashboard."""

from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import re
import tempfile
import unittest
from unittest import mock
from wcwidth import wcswidth

import session_search as ss
import ss_dashboard as dashboard


def _seed_session(
    conn,
    *,
    source: str,
    session_id: str,
    cwd: str,
    title: str,
    about: str,
    state: str,
    resume: str,
    ts: int,
    archived: bool = False,
) -> None:
    docs = [
        ss.Document(
            doc_id=f"{source}:{session_id}:user",
            source=source,
            session_id=session_id,
            title=title,
            path=f"/tmp/{source}/{session_id}.jsonl",
            cwd=cwd,
            role="user",
            ts=ts,
            text=f"{about} {state} Next: {resume}",
            meta={},
        ),
        ss.Document(
            doc_id=f"{source}:{session_id}:assistant",
            source=source,
            session_id=session_id,
            title=title,
            path=f"/tmp/{source}/{session_id}.jsonl",
            cwd=cwd,
            role="assistant",
            ts=ts + 1,
            text=f"Progress: {state}. Suggested next step: {resume}",
            meta={},
        ),
    ]
    ss.upsert_documents(conn, docs)
    if archived:
        ss.set_session_archive_status(conn, source, session_id, True, "manual")


class DashboardWorkMapContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.db = self.home / "sessions.sqlite"
        self.conn = ss.connect_db(self.db)
        ss.init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_explicit_limit_returns_exactly_n_sessions(self) -> None:
        for index in range(12):
            _seed_session(
                self.conn,
                source="codex",
                session_id=f"s{index}",
                cwd=f"/Users/alex/workspace/os/research/topic-{index}",
                title=f"Topic {index}",
                about=f"Research topic {index}",
                state=f"Collected notes for topic {index}",
                resume=f"Write the summary for topic {index}",
                ts=1_700_000_000 + index,
            )
        results = ss.recent_session_results(self.conn, 3, "all")
        self.assertEqual(len(results), 3)
        args = argparse.Namespace(
            db=str(self.db),
            home=str(self.home),
            no_refresh=True,
            archived=False,
            limit=3,
            source="all",
        )
        with (
            mock.patch.object(ss, "session_lock", return_value=contextlib.nullcontext()),
            mock.patch.object(ss, "dashboard_prompt", return_value=0),
            mock.patch.object(ss, "expand", side_effect=lambda value: pathlib.Path(value).expanduser()),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(ss.cmd_dashboard(args), 0)
        text = out.getvalue()
        # Open numbers 1..3 only for the listed threads.
        self.assertIn("Open: ss open 1", text)
        self.assertIn("Open: ss open 3", text)
        self.assertNotIn("Open: ss open 4", text)

    def test_dashboard_keeps_about_state_resume_on_threads(self) -> None:
        _seed_session(
            self.conn,
            source="claude",
            session_id="claude-a",
            cwd="/Users/alex/workspace/os/_shared/session-search",
            title="Dashboard plan",
            about="Design the SS restart work map",
            state="Drafted group layout and kept cards honest",
            resume="Implement exact limit behavior",
            ts=1_700_000_100,
        )
        results = ss.recent_session_results(self.conn, 5, "all")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ss.print_dashboard(self.conn, results)
        text = out.getvalue()
        self.assertIn("About:", text)
        self.assertIn("State:", text)
        self.assertIn("Resume:", text)
        self.assertIn("SS", text)
        self.assertIn("About:", text)
        self.assertIn("Open: ss open", text)

    def test_project_grouping_is_deterministic_and_human_readable(self) -> None:
        self.assertEqual(
            ss.dashboard_project_label("/Users/alex/workspace/os/_shared/session-search"),
            "Shared / Session Search",
        )
        self.assertEqual(
            ss.dashboard_project_label("/Users/alex/workspace/os/organic-growth"),
            "Organic Growth",
        )
        self.assertEqual(
            ss.dashboard_project_label("/Users/alex/code/session-search"),
            "Code / Session Search",
        )
        _seed_session(
            self.conn,
            source="claude",
            session_id="a",
            cwd="/Users/alex/workspace/os/_shared/session-search",
            title="A",
            about="First shared session",
            state="Indexed cards",
            resume="Open the thread",
            ts=100,
        )
        _seed_session(
            self.conn,
            source="codex",
            session_id="b",
            cwd="/Users/alex/workspace/os/_shared/session-search",
            title="B",
            about="Second shared session",
            state="Added tests",
            resume="Ship the dashboard",
            ts=200,
        )
        results = ss.recent_session_results(self.conn, 10, "all")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ss.print_dashboard(self.conn, results)
        text = out.getvalue()
        self.assertGreaterEqual(text.count("Shared / Session Search"), 1)
        self.assertIn("About:", text)
        self.assertNotIn("Open | Project", text)

    def test_full_index_project_discovery_keeps_older_projects(self) -> None:
        # 5 newest sessions in project A, 1 older project B that must still appear
        # when building the project map with a small thread limit.
        for index in range(5):
            _seed_session(
                self.conn,
                source="codex",
                session_id=f"new-{index}",
                cwd="/Users/alex/workspace/os/agency/lakeside-clinic",
                title=f"Agency {index}",
                about=f"Lakeside Clinic thread {index}",
                state="Recent client work",
                resume="Continue the audit",
                ts=1_000 + index,
            )
        _seed_session(
            self.conn,
            source="claude",
            session_id="old-robots",
            cwd="/Users/alex/workspace/os/robots",
            title="Robots battery",
            about="Ohmni battery recovery plan",
            state="Measured remaining stock",
            resume="Price the next lot",
            ts=50,
        )
        projects = ss.dashboard_project_summaries(self.conn, source_name="all", thread_limit=3)
        names = [item["project"] for item in projects]
        self.assertIn("Agency / Lakeside Clinic", names)
        self.assertIn("Robots", names)

    def test_older_project_summary_wraps_without_truncation(self) -> None:
        _seed_session(
            self.conn,
            source="codex",
            session_id="visible",
            cwd="/Users/alex/workspace/os/research/visible",
            title="Visible",
            about="Current work",
            state="Current state",
            resume="Continue",
            ts=200,
        )
        results = ss.recent_session_results(self.conn, 1, "all")
        older = [
            {
                "project": "Robots",
                "repo": "/Users/alex/workspace/os/robots",
                "latest_ts": 100,
                "count": 3,
                "latest_about": (
                    "Compare every remaining battery pack before publishing "
                    "the complete resale inventory."
                ),
            }
        ]
        out = io.StringIO()
        with (
            mock.patch.object(ss, "dashboard_terminal_width", return_value=40),
            contextlib.redirect_stdout(out),
        ):
            ss.print_dashboard(self.conn, results, project_summaries=older)
        rendered = out.getvalue()
        self.assertIn("Older projects still saved:", rendered)
        self.assertNotIn("…", rendered)
        for word in "Compare remaining battery publishing complete inventory".split():
            self.assertIn(word, rendered)
        self.assertTrue(all(wcswidth(line) <= 40 for line in rendered.splitlines()))

    def test_open_numbers_match_saved_selector_order(self) -> None:
        for index, name in enumerate(("alpha", "beta", "gamma"), start=1):
            _seed_session(
                self.conn,
                source="codex",
                session_id=name,
                cwd=f"/Users/alex/workspace/os/research/{name}",
                title=name,
                about=f"About {name}",
                state=f"State {name}",
                resume=f"Resume {name}",
                ts=index * 10,
            )
        results = ss.recent_session_results(self.conn, 3, "all")
        ss.save_last_results(results, "dashboard", self.db)
        payload = ss.load_last_results(self.db)
        ranks = [item["rank"] for item in payload["results"]]
        session_ids = [item["session_id"] for item in payload["results"]]
        self.assertEqual(ranks, [1, 2, 3])
        # newest first
        self.assertEqual(session_ids, ["gamma", "beta", "alpha"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ss.print_dashboard(self.conn, results)
        text = out.getvalue()
        self.assertLess(text.index("Open: ss open 1"), text.index("Open: ss open 2"))
        self.assertIn("Research / Gamma", text.split("Open: ss open 1", 1)[0])

    def test_archived_sessions_are_separate_and_marked(self) -> None:
        _seed_session(
            self.conn,
            source="codex",
            session_id="active",
            cwd="/Users/alex/workspace/os/research/active",
            title="Active",
            about="Active research",
            state="Still open",
            resume="Keep going",
            ts=200,
            archived=False,
        )
        _seed_session(
            self.conn,
            source="codex",
            session_id="done",
            cwd="/Users/alex/workspace/os/research/done",
            title="Done",
            about="Finished research",
            state="Closed out",
            resume="Leave archived",
            ts=100,
            archived=True,
        )
        active = ss.recent_session_results(self.conn, 10, "all", archived_only=False)
        archived = ss.recent_session_results(self.conn, 10, "all", archived_only=True)
        self.assertEqual(len(active), 1)
        self.assertEqual(len(archived), 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ss.print_dashboard(self.conn, archived, archived_view=True)
        text = out.getvalue()
        self.assertIn("SS archived", text)
        self.assertIn("[ARCHIVED]", text)

    def test_tables_fit_common_terminal_widths(self) -> None:
        long_about = "A" * 240
        _seed_session(
            self.conn,
            source="claude",
            session_id="wide",
            cwd="/Users/alex/workspace/os/_shared/session-search",
            title="Wide",
            about=long_about,
            state="B" * 180,
            resume="C" * 180,
            ts=10,
        )
        results = ss.recent_session_results(self.conn, 5, "all")
        for width in (80, 100, 140):
            out = io.StringIO()
            with mock.patch.object(ss, "dashboard_terminal_width", return_value=width):
                with contextlib.redirect_stdout(out):
                    ss.print_dashboard(self.conn, results)
            text = out.getvalue()
            for line in text.splitlines():
                self.assertLessEqual(len(line), width + 5, msg=line)

    def test_restart_cards_wrap_every_field_without_losing_words(self) -> None:
        fields = [
            ("About:", "Recover the forgotten deployment notes and 日本語 labels from the original agent session."),
            ("State:", "The parser indexed every message and retained the final verified decision."),
            ("Resume:", "Compare the staging output before publishing the repaired package."),
            ("Clue:", "Path: references/release-validation-checklist.md"),
            ("Open:", "ss open 7"),
        ]
        expected_words = {
            word
            for _label, value in fields
            for word in value.replace("/", " ").replace(".", "").split()
        }
        for width in (32, 39, 40, 51, 52, 80, 112, 160):
            with self.subTest(width=width):
                lines = dashboard.dashboard_restart_card_lines(
                    "7 · Claude 👩🏽‍💻 · 12m ago",
                    fields,
                    width,
                )
                rendered = "\n".join(lines)
                normalized = re.sub(r"[\s│|┌┐└┘+\-─]", "", rendered)
                for word in expected_words - {"release-validation-checklistmd"}:
                    self.assertIn(word, rendered.replace("/", " ").replace(".", ""))
                self.assertIn("releasevalidationchecklist.md", normalized)
                self.assertNotIn("…", rendered)
                self.assertTrue(all(wcswidth(line) == width for line in lines))
                self.assertIn("7 · Claude", rendered)
                self.assertIn("👩🏽‍💻", rendered)
                self.assertIn("ss open 7", rendered)

    def test_dashboard_uses_bordered_restart_cards_and_complete_wrapped_copy(self) -> None:
        _seed_session(
            self.conn,
            source="claude",
            session_id="wrapped",
            cwd="/Users/alex/workspace/os/_shared/session-search",
            title="Wrapped recovery",
            about="placeholder about",
            state="placeholder state",
            resume="placeholder resume",
            ts=100,
        )
        results = ss.recent_session_results(self.conn, 5, "all")
        fields = (
            "Recover the forgotten deployment notes from the original agent session.",
            "The parser indexed every message and retained the final verified decision.",
            "Compare the staging output before publishing the repaired package.",
            "Path: references/release-validation-checklist.md",
        )
        out = io.StringIO()
        with (
            mock.patch.object(ss, "dashboard_terminal_width", return_value=64),
            mock.patch.object(ss, "dashboard_summary", return_value=fields),
            contextlib.redirect_stdout(out),
        ):
            ss.print_dashboard(self.conn, results)
        rendered = out.getvalue()
        self.assertIn("┌─ 1 · Claude", rendered)
        self.assertIn("└" + ("─" * 62) + "┘", rendered)
        self.assertIn("Open: ss open 1", rendered)
        self.assertNotIn("…", rendered)
        for word in "forgotten deployment original retained verified staging repaired validation".split():
            self.assertIn(word, rendered)
        self.assertTrue(all(len(line) <= 64 for line in rendered.splitlines()))

    def test_oversized_token_keeps_joined_emoji_intact(self) -> None:
        token = ("a" * 24) + "👩🏽‍💻" + ("b" * 8)
        lines = dashboard.dashboard_wrapped_lines(token, 26)
        self.assertEqual("".join(lines), token)
        self.assertTrue(any("👩🏽‍💻" in line for line in lines))
        self.assertFalse(any(line.endswith("👩") for line in lines))
        self.assertTrue(all(wcswidth(line) <= 26 for line in lines))

    def test_narrow_dashboard_uses_safe_ascii_card(self) -> None:
        _seed_session(
            self.conn,
            source="codex",
            session_id="narrow",
            cwd="/Users/alex/workspace/os/research/narrow",
            title="Narrow recovery",
            about="Recover a narrow terminal session",
            state="All source evidence remains available",
            resume="Open the exact original session",
            ts=100,
        )
        results = ss.recent_session_results(self.conn, 5, "all")
        out = io.StringIO()
        with (
            mock.patch.object(ss, "dashboard_terminal_width", return_value=34),
            contextlib.redirect_stdout(out),
        ):
            ss.print_dashboard(self.conn, results)
        rendered = out.getvalue()
        self.assertIn("+--------------------------------+", rendered)
        self.assertIn("1 · Codex", rendered)
        self.assertIn("ss open 1", rendered)
        self.assertIn("About:", rendered)
        self.assertTrue(all(len(line) <= 34 for line in rendered.splitlines()))

    def test_unicode_and_missing_folder_do_not_crash(self) -> None:
        _seed_session(
            self.conn,
            source="claude",
            session_id="uni",
            cwd="",
            title="unicode café — 日本語",
            about="Discussed café pricing and 日本語 labels",
            state="Captured notes without a folder",
            resume="Ask where this belonged",
            ts=11,
        )
        results = ss.recent_session_results(self.conn, 5, "all")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ss.print_dashboard(self.conn, results)
        text = out.getvalue()
        self.assertIn("café", text)
        self.assertIn("About:", text)

    def test_search_compatibility_still_prints_result_cards(self) -> None:
        _seed_session(
            self.conn,
            source="codex",
            session_id="search-me",
            cwd="/Users/alex/workspace/os/research/needle",
            title="Needle",
            about="Unique zucchini battery inventory",
            state="Counted cells",
            resume="Order replacements",
            ts=99,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ss.cmd_search(
                argparse.Namespace(
                    db=str(self.db),
                    home=str(self.home),
                    query="zucchini battery",
                    limit=5,
                    source="all",
                    mode="local",
                    no_refresh=True,
                )
            )
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("Best matches for:", text)
        self.assertIn("zucchini", text.lower())


    def test_default_dashboard_is_sparse_and_grouped(self) -> None:
        _seed_session(
            self.conn,
            source="claude",
            session_id="a",
            cwd="/Users/alex/workspace/os/_shared/session-search",
            title="A",
            about="First shared session",
            state="Indexed cards",
            resume="Open the thread",
            ts=200,
        )
        _seed_session(
            self.conn,
            source="codex",
            session_id="b",
            cwd="/Users/alex/workspace/os/organic-growth",
            title="B",
            about="Second growth session",
            state="Mapped folders",
            resume="Choose market",
            ts=100,
        )
        # noisy junk should not dominate
        _seed_session(
            self.conn,
            source="codex",
            session_id="noise",
            cwd="/Users/alex/.codex/history.jsonl",
            title="noise",
            about="history row",
            state="ignore",
            resume="ignore",
            ts=50,
        )
        results = ss.recent_session_results(self.conn, 10, "all")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ss.print_dashboard(self.conn, results)
        text = out.getvalue()
        self.assertNotIn("SS WORK MAP", text)
        self.assertNotIn("PROJECTS", text)
        self.assertNotIn("Open |", text)
        self.assertNotIn("Latest work", text)
        self.assertIn("Shared / Session Search", text)
        self.assertIn("Organic Growth", text)
        self.assertIn("About:", text)
        self.assertIn("State:", text)
        self.assertIn("Resume:", text)
        # No giant ASCII table separators
        self.assertNotIn("-----+-+-", text)
        # Noise path should not appear as a main project header
        self.assertNotIn("History.Jsonl", text)


if __name__ == "__main__":
    unittest.main()
