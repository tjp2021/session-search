import argparse
import contextlib
import io
import pathlib
import tempfile
import unittest
from unittest import mock

import session_search as ss


class DashboardBehaviorContracts(unittest.TestCase):
    def test_explicit_limit_is_passed_through_exactly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            database = pathlib.Path(tmpdir) / "sessions.sqlite"
            database.touch()
            args = argparse.Namespace(
                db=str(database),
                home=tmpdir,
                no_refresh=True,
                archived=False,
                limit=3,
                source=None,
            )
            connection = mock.Mock()
            with (
                mock.patch.object(ss, "session_lock", return_value=contextlib.nullcontext()),
                mock.patch.object(ss, "connect_db", return_value=connection),
                mock.patch.object(ss, "init_db"),
                mock.patch.object(ss, "recent_session_results", return_value=[]) as recent,
                mock.patch.object(ss, "dashboard_project_summaries", return_value=[]) as projects,
                mock.patch.object(ss, "save_last_results"),
                mock.patch.object(ss, "print_dashboard"),
                mock.patch.object(ss, "dashboard_prompt", return_value=0),
            ):
                self.assertEqual(ss.cmd_dashboard(args), 0)
            recent.assert_called_once_with(connection, 3, None)
            projects.assert_called_once()

    def test_dashboard_keeps_about_state_and_resume(self):
        # Render a real dashboard from an indexed document and assert the
        # output. The old version grepped print_dashboard's source text, which
        # could not fail on a behavior regression.
        with tempfile.TemporaryDirectory() as tmpdir:
            old_lock = ss.DEFAULT_LOCK
            ss.DEFAULT_LOCK = str(pathlib.Path(tmpdir) / "session-search.lock")
            try:
                conn = ss.connect_db(pathlib.Path(tmpdir) / "contract.sqlite")
                ss.init_db(conn)
                ss.upsert_documents(
                    conn,
                    [
                        ss.Document(
                            doc_id="contract:doc-1",
                            source="claude",
                            session_id="contract-session",
                            title="Rebuild the exporter",
                            path="/tmp/contract/session.jsonl",
                            cwd="/tmp/contract-repo",
                            role="user",
                            ts=ss.now_ts() - 120,
                            text="Rebuild the exporter so the nightly sync stops dropping rows.",
                            meta={},
                        ),
                        ss.Document(
                            doc_id="contract:doc-2",
                            source="claude",
                            session_id="contract-session",
                            title="Rebuild the exporter",
                            path="/tmp/contract/session.jsonl",
                            cwd="/tmp/contract-repo",
                            role="assistant",
                            ts=ss.now_ts() - 90,
                            text="Updated the exporter so it writes every row without drops.",
                            meta={},
                        ),
                        ss.Document(
                            doc_id="contract:doc-3",
                            source="claude",
                            session_id="contract-session",
                            title="Rebuild the exporter",
                            path="/tmp/contract/session.jsonl",
                            cwd="/tmp/contract-repo",
                            role="user",
                            ts=ss.now_ts() - 60,
                            text="Next, run the nightly sync against the production snapshot.",
                            meta={},
                        ),
                    ],
                )
                results = ss.recent_session_results(conn, 5)
                self.assertTrue(results, "the indexed document must appear as a recent session")
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    ss.print_dashboard(conn, results)
                conn.close()
            finally:
                ss.DEFAULT_LOCK = old_lock
        rendered = out.getvalue()
        self.assertTrue(rendered.startswith("SS\n"), "the restart screen must open with its title line")
        self.assertIn("About: ", rendered)
        self.assertIn("State: ", rendered)
        self.assertIn("Resume: ", rendered)
        self.assertIn("State: Updated the exporter so it writes every row without drops.", rendered)
        self.assertIn("Resume: Next, run the nightly sync against the production snapshot.", rendered)
        self.assertIn("open with: ss open N", rendered)
        self.assertIn("Open: ss open 1", rendered)
        self.assertIn("exporter", rendered, "the session's own content must reach the About surface")


if __name__ == "__main__":
    unittest.main()
