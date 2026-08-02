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

    def test_interactive_prompts_fit_narrow_and_wide_terminals(self) -> None:
        cases = (
            (40, False, "Open N, Pn, search, or q: "),
            (40, True, "Open N, n, b, search, or q: "),
            (80, False, "Open N, choose project Pn, type search words, or q: "),
            (80, True, "Open N, n next, b back, type search words, or q: "),
        )
        for width, in_project, expected in cases:
            with self.subTest(width=width, in_project=in_project):
                with mock.patch.object(ss, "dashboard_terminal_width", return_value=width):
                    prompt = ss.dashboard_prompt_text(in_project=in_project)
                self.assertEqual(prompt, expected)
                self.assertLessEqual(wcswidth(prompt), width)

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
        with mock.patch.object(
            ss,
            "session_card_for_result",
            side_effect=AssertionError("project footer rebuilt a full session card"),
        ):
            projects = ss.dashboard_project_summaries(
                self.conn,
                source_name="all",
                thread_limit=3,
            )
        names = [item["project"] for item in projects]
        self.assertIn("Agency / Lakeside Clinic", names)
        self.assertIn("Robots", names)
        robots = next(item for item in projects if item["project"] == "Robots")
        self.assertEqual(robots["count"], 1)
        self.assertIn("Robots battery", robots["latest_about"])

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
            choices = ss.print_dashboard(self.conn, results, project_summaries=older)
        rendered = out.getvalue()
        self.assertIn("Older projects still saved:", rendered)
        self.assertIn("P1 · Robots", rendered)
        self.assertIn("Choose project: pN", rendered)
        self.assertEqual([item["project"] for item in choices], ["Robots"])
        self.assertNotIn("…", rendered)
        for word in "Compare remaining battery publishing complete inventory".split():
            self.assertIn(word, rendered)
        self.assertTrue(all(wcswidth(line) <= 40 for line in rendered.splitlines()))

    def test_project_results_include_only_the_selected_project(self) -> None:
        for index in range(3):
            _seed_session(
                self.conn,
                source="codex",
                session_id=f"career-{index}",
                cwd="/Users/alex/workspace/os/personal/career",
                title=f"Career {index}",
                about=f"Career work {index}",
                state="Prepared evidence",
                resume="Continue the application",
                ts=100 + index,
            )
        _seed_session(
            self.conn,
            source="claude",
            session_id="osmo-1",
            cwd="/Users/alex/workspace/os/_shared/osmo",
            title="Osmo",
            about="Build learning cards",
            state="Imported topics",
            resume="Run the next lesson",
            ts=200,
        )

        name, results, total = ss.dashboard_project_results(self.conn, "personal / career")

        self.assertEqual(name, "Personal / Career")
        self.assertEqual(len(results), 3)
        self.assertEqual(total, 3)
        self.assertEqual(
            {str(row["session_id"]) for row, _score, _label in results},
            {"career-0", "career-1", "career-2"},
        )

    def test_project_command_renders_cards_and_saves_the_expanded_ranks(self) -> None:
        for index in range(2):
            _seed_session(
                self.conn,
                source="codex",
                session_id=f"career-{index}",
                cwd="/Users/alex/workspace/os/personal/career",
                title=f"Career {index}",
                about=f"Career work {index}",
                state="Prepared evidence",
                resume="Continue the application",
                ts=100 + index,
            )
        out = io.StringIO()
        args = argparse.Namespace(
            db=str(self.db),
            home=str(self.home),
            project="Personal / Career",
            limit=200,
            source="all",
            mode="hybrid",
            no_refresh=True,
        )
        with (
            mock.patch.object(ss, "dashboard_is_interactive", return_value=False),
            mock.patch.object(ss, "save_last_results") as save,
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(ss.cmd_project(args), 0)

        rendered = out.getvalue()
        self.assertIn("SS project · Personal / Career", rendered)
        self.assertIn("Open: ss open 1", rendered)
        self.assertIn("Open: ss open 2", rendered)
        save.assert_called_once()
        saved_results, saved_query, saved_db = save.call_args.args
        self.assertEqual(saved_query, "project: Personal / Career")
        self.assertEqual(saved_db.resolve(), self.db.resolve())
        self.assertEqual(
            {str(row["session_id"]) for row, _score, _label in saved_results},
            {"career-0", "career-1"},
        )

    def test_interactive_project_selector_opens_its_bucket(self) -> None:
        project_choices = [
            {"project": "Personal"},
            {"project": "Shared / Osmo"},
        ]
        args = argparse.Namespace(
            db=str(self.db),
            home=str(self.home),
            limit=10,
            source="all",
            mode="hybrid",
            no_refresh=True,
            project_choices=project_choices,
        )
        with (
            mock.patch.object(ss, "dashboard_is_interactive", return_value=True),
            mock.patch("builtins.input", side_effect=["p2", "q"]),
            mock.patch.object(ss, "cmd_project", return_value=0) as project,
        ):
            self.assertEqual(ss.dashboard_prompt(args), 0)
        self.assertEqual(project.call_args.args[0].project, "Shared / Osmo")
        self.assertEqual(project.call_args.args[0].project_choices, project_choices)

    def test_project_aggregation_counts_beyond_old_scan_and_page_caps(self) -> None:
        for index in range(300):
            _seed_session(
                self.conn,
                source="codex",
                session_id=f"scaled-{index}",
                cwd="/Users/alex/workspace/os/personal/career",
                title=f"Scaled {index}",
                about=f"Scaled work {index}",
                state="Stored evidence",
                resume="Continue",
                ts=10_000 + index,
            )
        with mock.patch.object(
            ss,
            "session_card_for_result",
            side_effect=AssertionError("aggregation constructed cards"),
        ):
            projects = ss.dashboard_project_summaries(
                self.conn,
                source_name="all",
                thread_limit=10,
                project_scan_limit=1,
            )
            name, first_page, total = ss.dashboard_project_results(
                self.conn,
                "Personal / Career",
                limit=500,
            )
            _name, second_page, second_total = ss.dashboard_project_results(
                self.conn,
                "Personal / Career",
                limit=200,
                offset=200,
            )

        career = next(item for item in projects if item["project"] == "Personal / Career")
        self.assertEqual(career["count"], 300)
        self.assertEqual(name, "Personal / Career")
        self.assertEqual(total, 300)
        self.assertEqual(second_total, 300)
        self.assertEqual(len(first_page), 200)
        self.assertEqual(len(second_page), 100)
        self.assertTrue(
            {str(row["session_id"]) for row, _score, _label in first_page}.isdisjoint(
                {str(row["session_id"]) for row, _score, _label in second_page}
            )
        )

    def test_recent_active_sessions_survive_a_large_archived_prefix(self) -> None:
        for index in range(10):
            _seed_session(
                self.conn,
                source="codex",
                session_id=f"active-{index}",
                cwd="/Users/alex/workspace/os/personal/active",
                title=f"Active {index}",
                about="Active work",
                state="Indexed",
                resume="Continue",
                ts=100 + index,
            )
        for index in range(60):
            _seed_session(
                self.conn,
                source="codex",
                session_id=f"archived-{index}",
                cwd="/Users/alex/workspace/os/personal/archive",
                title=f"Archived {index}",
                about="Archived work",
                state="Finished",
                resume="None",
                ts=1000 + index,
                archived=True,
            )

        results = ss.recent_session_results(self.conn, 10, "all")

        self.assertEqual(len(results), 10)
        self.assertTrue(
            all(str(row["session_id"]).startswith("active-") for row, _score, _label in results)
        )

    def test_all_project_sources_include_vscode_and_cursor(self) -> None:
        for source in ss.SUPPORTED_SOURCES:
            _seed_session(
                self.conn,
                source=source,
                session_id=f"{source}-project",
                cwd="/Users/alex/workspace/os/research/all-tools",
                title=f"{source} project",
                about=f"{source} work",
                state="Indexed",
                resume="Continue",
                ts=100,
            )

        name, results, total = ss.dashboard_project_results(
            self.conn,
            "Research / All Tools",
            source_name="all",
        )

        self.assertEqual(name, "Research / All Tools")
        self.assertEqual(total, 5)
        self.assertEqual(
            {str(row["source"]) for row, _score, _label in results},
            set(ss.SUPPORTED_SOURCES),
        )
        projects = ss.dashboard_project_summaries(self.conn, source_name="all")
        project = next(item for item in projects if item["project"] == "Research / All Tools")
        self.assertEqual(project["count"], 5)

    def test_project_aggregation_includes_real_non_user_adapter_roles(self) -> None:
        roles = {
            "codex": "session",
            "claude": "assistant",
            "pi": "user",
            "vscode": "state",
            "cursor": "turn",
        }
        ss.upsert_documents(
            self.conn,
            [
                ss.Document(
                    doc_id=f"{source}:role-shaped",
                    source=source,
                    session_id=f"{source}-role-shaped",
                    title=f"{source} role-shaped session",
                    path=f"/tmp/{source}/state",
                    cwd="/Users/alex/workspace/os/research/role-shaped",
                    role=role,
                    ts=100,
                    text=f"Indexed {source} through its real adapter role.",
                    meta={},
                )
                for source, role in roles.items()
            ],
        )

        name, results, total = ss.dashboard_project_results(
            self.conn,
            "Research / Role Shaped",
            source_name="all",
        )

        self.assertEqual(name, "Research / Role Shaped")
        self.assertEqual(total, 5)
        self.assertEqual(
            {str(row["source"]) for row, _score, _label in results},
            set(roles),
        )
        recent = ss.recent_session_results(self.conn, 10, "all")
        self.assertEqual(
            {str(row["source"]) for row, _score, _label in recent},
            set(roles),
        )

    def test_project_grouping_ignores_stale_cached_repo(self) -> None:
        _seed_session(
            self.conn,
            source="codex",
            session_id="moved-project",
            cwd="/Users/alex/workspace/os/labs/alpha",
            title="Moved project",
            about="Initial location",
            state="Indexed",
            resume="Continue",
            ts=100,
        )
        row = self.conn.execute(
            "SELECT * FROM documents WHERE doc_id = ?",
            ("codex:moved-project:user",),
        ).fetchone()
        card = ss.session_card_for_result(self.conn, row, "", persist=False)
        ss.store_session_card(
            self.conn,
            card,
            ss.session_card_hash(ss.session_rows(self.conn, row)),
        )
        ss.upsert_documents(
            self.conn,
            [
                ss.Document(
                    doc_id="codex:moved-project:new",
                    source="codex",
                    session_id="moved-project",
                    title="Moved project",
                    path="/tmp/codex/moved-project.jsonl",
                    cwd="/Users/alex/workspace/os/labs/beta",
                    role="user",
                    ts=200,
                    text="The session moved to the beta project.",
                    meta={},
                )
            ],
        )

        projects = ss.dashboard_project_summaries(self.conn)
        counts = {item["project"]: item["count"] for item in projects}
        self.assertEqual(counts.get("Labs / Beta"), 1)
        self.assertNotIn("Labs / Alpha", counts)

    def test_project_command_honors_limit_and_discloses_total(self) -> None:
        for index in range(3):
            _seed_session(
                self.conn,
                source="codex",
                session_id=f"limited-{index}",
                cwd="/Users/alex/workspace/os/personal",
                title=f"Limited {index}",
                about="Bounded page",
                state="Indexed",
                resume="Continue",
                ts=100 + index,
            )
        args = argparse.Namespace(
            db=str(self.db),
            home=str(self.home),
            project="Personal",
            limit=1,
            page=1,
            source="all",
            mode="hybrid",
            no_refresh=True,
            prompt=False,
        )
        out = io.StringIO()
        with (
            mock.patch.object(ss, "save_last_results") as save,
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(ss.cmd_project(args), 0)

        saved_results = save.call_args.args[0]
        self.assertEqual(len(saved_results), 1)
        self.assertIn("1-1 shown of 3 threads", out.getvalue())
        self.assertNotIn("Open: ss open 2", out.getvalue())

    def test_project_switching_uses_one_real_prompt_loop(self) -> None:
        for index, cwd in enumerate(
            (
                "/Users/alex/workspace/os/personal",
                "/Users/alex/workspace/os/_shared/osmo",
                "/Users/alex/workspace/os/research",
            ),
            1,
        ):
            _seed_session(
                self.conn,
                source="codex",
                session_id=f"switch-{index}",
                cwd=cwd,
                title=f"Switch {index}",
                about=f"Project {index}",
                state="Indexed",
                resume="Continue",
                ts=100 + index,
            )
        choices = [
            {"project": "Personal"},
            {"project": "Shared / Osmo"},
            {"project": "Research"},
        ]
        args = argparse.Namespace(
            db=str(self.db),
            home=str(self.home),
            limit=10,
            source="all",
            mode="hybrid",
            no_refresh=True,
            project_choices=choices,
        )
        output = io.StringIO()
        with (
            mock.patch.object(ss, "dashboard_is_interactive", return_value=True),
            mock.patch("builtins.input", side_effect=["p1", "p2", "p3", "q"]),
            mock.patch.object(ss, "save_last_results"),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(ss.dashboard_prompt(args), 0)

        rendered = output.getvalue()
        self.assertIn("SS project · Personal", rendered)
        self.assertIn("SS project · Shared / Osmo", rendered)
        self.assertIn("SS project · Research", rendered)

    def test_project_next_page_replaces_the_saved_selector_page(self) -> None:
        for index in range(3):
            _seed_session(
                self.conn,
                source="codex",
                session_id=f"page-{index}",
                cwd="/Users/alex/workspace/os/personal",
                title=f"Page {index}",
                about="Paged project",
                state="Indexed",
                resume="Continue",
                ts=100 + index,
            )
        args = argparse.Namespace(
            db=str(self.db),
            home=str(self.home),
            limit=10,
            source="all",
            mode="hybrid",
            no_refresh=True,
            project_choices=[],
            current_project="Personal",
            project_page=1,
            project_total=3,
            project_limit=1,
        )
        saved_pages: list[list[str]] = []

        def capture(results, _query, _db) -> None:
            saved_pages.append(
                [str(row["session_id"]) for row, _score, _label in results]
            )

        with (
            mock.patch.object(ss, "dashboard_is_interactive", return_value=True),
            mock.patch("builtins.input", side_effect=["n", "q"]),
            mock.patch.object(ss, "save_last_results", side_effect=capture),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(ss.dashboard_prompt(args), 0)

        self.assertEqual(saved_pages, [["page-1"]])
        self.assertEqual(args.project_page, 2)

    def test_back_from_first_project_page_returns_to_dashboard(self) -> None:
        _seed_session(
            self.conn,
            source="codex",
            session_id="back-project",
            cwd="/Users/alex/workspace/os/personal",
            title="Back project",
            about="Return to dashboard",
            state="Indexed",
            resume="Continue",
            ts=100,
        )
        args = argparse.Namespace(
            db=str(self.db),
            home=str(self.home),
            limit=10,
            source="all",
            mode="hybrid",
            no_refresh=True,
            project_choices=[],
            current_project="Personal",
            project_page=1,
            project_total=1,
            project_limit=200,
        )
        output = io.StringIO()
        with (
            mock.patch.object(ss, "dashboard_is_interactive", return_value=True),
            mock.patch("builtins.input", side_effect=["b", "q"]),
            mock.patch.object(ss, "save_last_results"),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(ss.dashboard_prompt(args), 0)

        self.assertEqual(args.current_project, "")
        self.assertIn("\nSS\n", output.getvalue())

    def test_project_command_keeps_parent_selectors_for_another_choice(self) -> None:
        _seed_session(
            self.conn,
            source="codex",
            session_id="personal-1",
            cwd="/Users/alex/workspace/os/personal",
            title="Personal",
            about="Personal work",
            state="Prepared evidence",
            resume="Continue the work",
            ts=100,
        )
        project_choices = [
            {"project": "Personal"},
            {"project": "Shared / Osmo"},
        ]
        args = argparse.Namespace(
            db=str(self.db),
            home=str(self.home),
            project="Personal",
            project_choices=project_choices,
            limit=200,
            source="all",
            mode="hybrid",
            no_refresh=True,
        )
        with (
            mock.patch.object(ss, "save_last_results"),
            mock.patch.object(ss, "dashboard_prompt", return_value=0) as prompt,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(ss.cmd_project(args), 0)

        self.assertEqual(prompt.call_args.args[0].project_choices, project_choices)

    def test_direct_project_command_routes_to_project_view(self) -> None:
        with mock.patch.object(ss, "cmd_project", return_value=0) as project:
            self.assertEqual(ss.main(["project", "Personal / Career"]), 0)
        self.assertEqual(project.call_args.args[0].project, ["Personal / Career"])

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
