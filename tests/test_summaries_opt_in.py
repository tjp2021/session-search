"""A key in the environment is not consent to send session text anywhere.

Anyone may have OPENROUTER_API_KEY exported for unrelated tools. Model
summaries run only when SS_SUMMARIES=openrouter says this tool specifically
may use it. Without that, SS behaves like v1: local, deterministic, zero
network calls.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_module():
    spec = importlib.util.spec_from_file_location("session_search", ROOT / "session_search.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ss = load_module()


class OptInConsentTest(unittest.TestCase):
    def test_a_key_alone_is_not_consent(self):
        with mock.patch.dict(ss.os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):
            with mock.patch.dict(ss.os.environ, {}, clear=False) as env:
                env.pop("SS_SUMMARIES", None)
                self.assertFalse(ss.llm_available())

    def test_no_network_attempt_without_opt_in(self):
        with mock.patch.dict(ss.os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False) as env:
            env.pop("SS_SUMMARIES", None)
            # llm_summarize imports requests inside the function, so the real
            # module's post is the only honest interception point.
            # llm_summarize swallows every exception by design, so a raising
            # stub cannot prove anything; assert the call count instead.
            with mock.patch("requests.post") as post:
                self.assertEqual(ss.llm_summarize("summarize this"), "")
            post.assert_not_called()

    def test_summaries_and_zero_network_for_the_wide_project_scan(self):
        # The project footer scans up to 250 sessions; a scan must never
        # become a paid fan-out even when summaries are enabled.
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ss.init_db(conn)
        ss.upsert_documents(conn, [ss.Document(
            doc_id="d1", source="codex", session_id="s1", title="S1",
            path="/tmp/p", cwd="/tmp/w/os/research/x", role="user", ts=1,
            text="Rebuilt the exporter.", meta={},
        )])
        with mock.patch.dict(
            ss.os.environ,
            {"OPENROUTER_API_KEY": "sk-or-test", "SS_SUMMARIES": "openrouter"},
            clear=False,
        ):
            with mock.patch.object(
                ss, "llm_summarize",
                side_effect=AssertionError("project scan called the model"),
            ):
                projects = ss.dashboard_project_summaries(conn, thread_limit=3)
        self.assertTrue(projects)
        conn.close()

    def test_opt_in_with_a_key_enables_summaries(self):
        with mock.patch.dict(
            ss.os.environ,
            {"OPENROUTER_API_KEY": "sk-or-test", "SS_SUMMARIES": "openrouter"},
            clear=False,
        ):
            self.assertTrue(ss.llm_available())

    def test_opt_in_without_a_key_stays_disabled(self):
        with mock.patch.dict(ss.os.environ, {"SS_SUMMARIES": "openrouter"}, clear=False) as env:
            env.pop("OPENROUTER_API_KEY", None)
            with mock.patch.object(ss.pathlib.Path, "home", return_value=pathlib.Path("/nonexistent")):
                self.assertFalse(ss.llm_available())

    def test_no_private_config_fallback_for_the_key(self):
        # The key loader once fell back to a private ~/.hermes/.env path. A
        # public package must never read another product's config for a
        # credential, so a key present only there stays invisible.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = pathlib.Path(tmp)
            (fake_home / ".hermes").mkdir()
            (fake_home / ".hermes" / ".env").write_text("OPENROUTER_API_KEY=sk-or-hidden\n")
            with mock.patch.dict(
                ss.os.environ, {"SS_SUMMARIES": "openrouter"}, clear=False
            ) as env:
                env.pop("OPENROUTER_API_KEY", None)
                with mock.patch.object(ss.pathlib.Path, "home", return_value=fake_home):
                    self.assertFalse(ss.llm_available())
                    self.assertEqual(ss._load_openrouter_api_key(), "")


if __name__ == "__main__":
    unittest.main()
