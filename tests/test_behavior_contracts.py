import argparse
import contextlib
import inspect
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
        # Resolve the source from the function itself so the contract follows
        # print_dashboard wherever the facade split places it.
        dashboard = inspect.getsource(ss.print_dashboard)
        self.assertTrue(
            'About: {about}' in dashboard or 'f"About: {about}"' in dashboard or "About: {about}" in dashboard
        )
        self.assertTrue('State: {state}' in dashboard or 'f"State: {state}"' in dashboard)
        self.assertTrue('Resume: {resume}' in dashboard or 'f"Resume: {resume}"' in dashboard)
        # Restart screen identity markers.
        self.assertIn('print(title)', dashboard)
        self.assertIn('ss open N', dashboard)


if __name__ == "__main__":
    unittest.main()
