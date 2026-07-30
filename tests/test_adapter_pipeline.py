"""Unpatched pipeline tests over synthetic session stores.

Everything else in the suite fixtures at the Document boundary, so the
adapter parsers and the CLI command bodies never ran: an extract_claude_turn
mutant returning empty text survived the whole suite. These tests write real
session files for all five sources into a temp HOME and drive
index/search/status/handoff/eval through main() with nothing patched.

The fixtures encode this repo's reading of each tool's on-disk format. They
catch regressions in our parsers, not upstream format drift by the apps.
"""

from __future__ import annotations

import contextlib
import gc
import io
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
import warnings
from unittest import mock

import session_search as ss

FIXTURE_REPO = "/tmp/fixture-repo"
CLAUDE_SESSION = "claude-fixture-session"
CODEX_THREAD = "codex-fixture-thread"
PI_SESSION = "pi-fixture-session"
EXCLUDED_CLAUDE = "internal system prompt that must never be indexed"
EXCLUDED_PI = "Abandoned fork question that must stay out of the index."


def iso(ts: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def write_claude_store(home: pathlib.Path, base_ts: int) -> None:
    project = home / ".claude" / "projects" / "fixture-project"
    project.mkdir(parents=True)
    lines = [
        {
            "sessionId": CLAUDE_SESSION,
            "cwd": FIXTURE_REPO,
            "type": "user",
            "uuid": "cu1",
            "timestamp": iso(base_ts),
            "message": {
                "role": "user",
                "content": "Fix the camoufox login flow so the scraper stops getting blocked on the profile page.",
            },
        },
        {"type": "ai-title", "aiTitle": "Camoufox login repair"},
        {
            "type": "assistant",
            "uuid": "ca1",
            "timestamp": iso(base_ts + 60),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "Updated the login selector and persisted the browser profile so cookies survive restarts.",
                    }
                ],
            },
        },
        {
            "type": "system",
            "uuid": "cs1",
            "timestamp": iso(base_ts + 90),
            "message": {"role": "system", "content": EXCLUDED_CLAUDE},
        },
    ]
    path = project / f"{CLAUDE_SESSION}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def write_codex_store(home: pathlib.Path, base_ts: int) -> None:
    codex = home / ".codex"
    codex.mkdir(parents=True)
    conn = sqlite3.connect(codex / "state_5.sqlite")
    conn.execute(
        "CREATE TABLE threads (id TEXT, updated_at INTEGER, cwd TEXT, title TEXT,"
        " first_user_message TEXT, preview TEXT, created_at INTEGER, git_branch TEXT)"
    )
    conn.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            CODEX_THREAD,
            base_ts + 120,
            FIXTURE_REPO,
            "Deploy the robots landing page",
            "Deploy the robots landing page to vercel with the new pricing table.",
            "Deployment plan for the landing page rollout.",
            base_ts,
            "main",
        ),
    )
    conn.commit()
    conn.close()
    history = {
        "session_id": CODEX_THREAD,
        "ts": base_ts + 150,
        "text": "Deploy the robots landing page to vercel and verify the pricing table renders.",
    }
    (codex / "history.jsonl").write_text(json.dumps(history) + "\n", encoding="utf-8")


def write_pi_store(home: pathlib.Path, base_ts: int) -> None:
    sessions = home / ".pi" / "agent" / "sessions" / "fixture-dir"
    sessions.mkdir(parents=True)
    lines = [
        {"type": "session", "id": PI_SESSION, "cwd": FIXTURE_REPO},
        {"type": "session_info", "id": "pi0", "name": "Whoop dashboard rebuild"},
        {
            "type": "message",
            "id": "pi1",
            "timestamp": iso(base_ts),
            "message": {
                "role": "user",
                "content": "Rebuild the whoop dashboard charts so recovery trends show weekly.",
            },
        },
        # Abandoned fork: also a child of pi1, but older than pi2, so the
        # active branch must be pi1 -> pi2 and this text must stay out.
        {
            "type": "message",
            "id": "pi3",
            "parentId": "pi1",
            "timestamp": iso(base_ts + 30),
            "message": {"role": "user", "content": EXCLUDED_PI},
        },
        {
            "type": "message",
            "id": "pi2",
            "parentId": "pi1",
            "timestamp": iso(base_ts + 90),
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Charts rebuilt with weekly aggregation and the recovery trend line."}
                ],
            },
        },
    ]
    path = sessions / f"20260729_{PI_SESSION}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def write_vscode_like_store(root: pathlib.Path, text_a: str, text_b: str) -> None:
    storage = root / "globalStorage"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "state.vscdb")
    conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    payload = {"chatSessions": [{"text": text_a}, {"response": text_b}]}
    conn.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("workbench.chat.sessions.fixture", json.dumps(payload)),
    )
    conn.commit()
    conn.close()


def build_fixture_home(home: pathlib.Path, base_ts: int) -> None:
    write_claude_store(home, base_ts)
    write_codex_store(home, base_ts)
    write_pi_store(home, base_ts)
    write_vscode_like_store(
        home / "Library" / "Application Support" / "Code" / "User",
        "How do I center the pricing card grid without breaking the mobile layout?",
        "Use a CSS grid with auto-fit minmax columns and test the mobile breakpoint.",
    )
    write_vscode_like_store(
        home / "Library" / "Application Support" / "Cursor" / "User",
        "Refactor the tailwind config so dark mode variables load before the theme.",
        "Moved the dark mode variables into the base layer and reloaded the theme.",
    )


def override_defaults(root: pathlib.Path, db: pathlib.Path) -> dict[str, str]:
    """Point the module defaults at the fixture area, exactly as the CLI
    resolves them for a real user; returns the originals for restore."""
    old = {
        "DEFAULT_DB": ss.DEFAULT_DB,
        "DEFAULT_LOCK": ss.DEFAULT_LOCK,
        "DEFAULT_LAST_RESULTS": ss.DEFAULT_LAST_RESULTS,
        "DEFAULT_HANDOFF_DIR": ss.DEFAULT_HANDOFF_DIR,
    }
    ss.DEFAULT_DB = str(db)
    ss.DEFAULT_LOCK = str(root / "session-search.lock")
    ss.DEFAULT_LAST_RESULTS = str(root / "last-results.json")
    ss.DEFAULT_HANDOFF_DIR = str(root / "handoff")
    return old


def restore_defaults(old: dict[str, str]) -> None:
    for name, value in old.items():
        setattr(ss, name, value)


def run_main(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        with contextlib.redirect_stdout(out):
            code = ss.main(argv)
        gc.collect()
    leaked = [item for item in caught if "unclosed database" in str(item.message)]
    if leaked:
        raise AssertionError(f"CLI leaked {len(leaked)} SQLite connection(s): {argv}")
    return code, out.getvalue()


class AdapterPipelineTest(unittest.TestCase):
    """One fixture home, indexed once, asserted from several angles."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        root = pathlib.Path(cls._tmp.name)
        cls.home = root / "home"
        cls.db = root / "index.sqlite"
        cls.base_ts = ss.now_ts() - 3600
        build_fixture_home(cls.home, cls.base_ts)

        cls._old_defaults = override_defaults(root, cls.db)
        cls.addClassCleanup(restore_defaults, cls._old_defaults)
        cls.enterClassContext(mock.patch.dict(os.environ, {"HOME": str(cls.home)}))
        cls.index_exit, cls.index_output = run_main(["index"])

    def all_rows(self) -> list[sqlite3.Row]:
        conn = ss.connect_db(self.db)
        try:
            return list(conn.execute("SELECT * FROM documents"))
        finally:
            conn.close()

    def rows_for(self, source: str) -> list[sqlite3.Row]:
        return [row for row in self.all_rows() if row["source"] == source]

    def test_index_runs_unpatched_and_reports_documents(self):
        self.assertEqual(self.index_exit, 0)
        self.assertIn("Indexed", self.index_output)

    def test_every_source_lands_documents(self):
        by_source = {row["source"] for row in self.all_rows()}
        self.assertEqual(by_source, {"claude", "codex", "pi", "vscode", "cursor"})

    def test_claude_turns_carry_role_text_and_timestamp(self):
        rows = self.rows_for("claude")
        user = [r for r in rows if r["role"] == "user"]
        assistant = [r for r in rows if r["role"] == "assistant"]
        self.assertTrue(any("camoufox login flow" in r["text"] for r in user))
        self.assertTrue(any("persisted the browser profile" in r["text"] for r in assistant))
        self.assertTrue(all(r["session_id"] == CLAUDE_SESSION for r in rows))
        self.assertIn(self.base_ts, {r["ts"] for r in user})

    def test_claude_system_lines_never_reach_the_index(self):
        self.assertFalse(any(EXCLUDED_CLAUDE in (r["text"] or "") for r in self.all_rows()))

    def test_codex_thread_and_history_both_index(self):
        rows = self.rows_for("codex")
        thread = [r for r in rows if r["doc_id"] == f"codex-thread:{CODEX_THREAD}"]
        history = [r for r in rows if r["doc_id"].startswith("codex-history:")]
        self.assertEqual(len(thread), 1)
        self.assertEqual(thread[0]["cwd"], FIXTURE_REPO)
        self.assertTrue(any("verify the pricing table renders" in r["text"] for r in history))
        # History lines inherit the thread's cwd through codex_thread_context.
        self.assertTrue(all(r["cwd"] == FIXTURE_REPO for r in history))

    def test_pi_indexes_the_active_branch_only(self):
        rows = self.rows_for("pi")
        texts = [r["text"] for r in rows]
        self.assertTrue(any("whoop dashboard charts" in t for t in texts))
        self.assertTrue(any("weekly aggregation" in t for t in texts))
        self.assertFalse(any(EXCLUDED_PI in t for t in texts))
        self.assertTrue(any(r["title"] == "Whoop dashboard rebuild" for r in rows))

    def test_vscode_and_cursor_state_chat_text_is_extracted(self):
        vscode_texts = " ".join(r["text"] for r in self.rows_for("vscode"))
        cursor_texts = " ".join(r["text"] for r in self.rows_for("cursor"))
        self.assertIn("pricing card grid", vscode_texts)
        self.assertIn("auto-fit minmax", vscode_texts)
        self.assertIn("dark mode variables", cursor_texts)

    def test_reindex_is_idempotent(self):
        before = len(self.all_rows())
        code, _ = run_main(["index", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.all_rows()), before)


class CliRoundTripTest(unittest.TestCase):
    """search/status/handoff/eval through main() on a fresh fixture index."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        self.home = root / "home"
        self.db = root / "index.sqlite"
        build_fixture_home(self.home, ss.now_ts() - 3600)

        self._old_defaults = override_defaults(root, self.db)
        self.addCleanup(restore_defaults, self._old_defaults)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home)}))
        code, _ = run_main(["index", "--quiet"])
        self.assertEqual(code, 0)

    def test_search_finds_the_fixture_session_and_saves_context(self):
        code, output = run_main(["search", "camoufox", "--mode", "fts"])
        self.assertEqual(code, 0)
        self.assertIn("camoufox", output.lower())
        self.assertIn("Claude Code", output)
        result_path = pathlib.Path(ss.DEFAULT_LAST_RESULTS)
        self.assertTrue(result_path.is_file())
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["query"], "camoufox")
        self.assertEqual(pathlib.Path(payload["db"]).resolve(), self.db.resolve())
        self.assertEqual(payload["results"][0]["rank"], 1)
        self.assertEqual(payload["results"][0]["source"], "claude")
        self.assertEqual(payload["results"][0]["session_id"], CLAUDE_SESSION)

    def test_status_reports_per_source_counts(self):
        conn = ss.connect_db(self.db)
        try:
            expected = {
                row["source"]: row["n"]
                for row in conn.execute("SELECT source, count(*) AS n FROM documents GROUP BY source")
            }
        finally:
            conn.close()
        with mock.patch.object(ss, "embedding_backend", return_value=None):
            code, output = run_main(["status"])
        self.assertEqual(code, 0)
        self.assertIn(f"documents: {sum(expected.values())}", output)
        for source, count in expected.items():
            self.assertIn(f"{source}: {count} newest=", output)

    def test_handoff_writes_a_packet_for_a_cross_tool_target(self):
        code, _ = run_main(["search", "camoufox", "--mode", "fts"])
        self.assertEqual(code, 0)
        code, output = run_main(["handoff", "1", "codex"])
        self.assertEqual(code, 0)
        packets = list(pathlib.Path(ss.DEFAULT_HANDOFF_DIR).glob("*.md"))
        self.assertEqual(len(packets), 1)
        packet_text = packets[0].read_text(encoding="utf-8")
        self.assertIn("Status:", packet_text)
        self.assertIn("camoufox", packet_text.lower())
        self.assertIn(f"Native session: claude: {CLAUDE_SESSION}", packet_text)
        self.assertIn(str(packets[0]), output)

    def test_eval_passes_on_a_matching_rule_and_fails_on_a_wrong_one(self):
        eval_dir = pathlib.Path(self._tmp.name)
        good = eval_dir / "good-eval.json"
        good.write_text(
            json.dumps(
                [
                    {
                        "query": "camoufox login",
                        "accept": [{"source": "claude", "session_id": CLAUDE_SESSION}],
                    }
                ]
            ),
            encoding="utf-8",
        )
        code, output = run_main(["eval", "--file", str(good), "--mode", "fts"])
        self.assertEqual(code, 0)
        self.assertIn("1 passed, 0 failed", output)

        bad = eval_dir / "bad-eval.json"
        bad.write_text(
            json.dumps(
                [
                    {
                        "query": "camoufox login",
                        "accept": [{"source": "claude", "session_id": "missing-session"}],
                    }
                ]
            ),
            encoding="utf-8",
        )
        code, output = run_main(["eval", "--file", str(bad), "--mode", "fts"])
        self.assertEqual(code, 1)
        self.assertIn("0 passed, 1 failed", output)


if __name__ == "__main__":
    unittest.main()
