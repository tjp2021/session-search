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
                mock.patch.object(ss, "save_last_results"),
                mock.patch.object(ss, "print_dashboard"),
                mock.patch.object(ss, "dashboard_prompt", return_value=0),
            ):
                self.assertEqual(ss.cmd_dashboard(args), 0)
            recent.assert_called_once_with(connection, 3, None)

    def test_dashboard_keeps_about_state_and_resume(self):
        source = pathlib.Path(ss.__file__).read_text(encoding="utf-8")
        dashboard_start = source.index("def print_dashboard(")
        dashboard_end = source.index("\ndef dashboard_is_interactive", dashboard_start)
        dashboard = source[dashboard_start:dashboard_end]
        self.assertIn('print(f"   About: {about}")', dashboard)
        self.assertIn('print(f"   State: {state}")', dashboard)
        self.assertIn('print(f"   Resume: {resume}")', dashboard)


if __name__ == "__main__":
    unittest.main()
