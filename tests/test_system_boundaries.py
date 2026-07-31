"""Hard boundary tests for adapters, native commands, and CLI dispatch."""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

import session_search as ss


class NativeCommandContractTest(unittest.TestCase):
    def test_native_resume_lines_are_exact_and_shell_quoted(self):
        cases = [
            (
                "codex",
                "session $1",
                "/tmp/Fixture Project",
                "",
                ["cd '/tmp/Fixture Project'", "codex resume 'session $1'"],
            ),
            (
                "claude",
                "session $1",
                "/tmp/Fixture Project",
                "",
                ["cd '/tmp/Fixture Project'", "claude --resume 'session $1'"],
            ),
            (
                "pi",
                "session $1",
                "/tmp/Fixture Project",
                "/tmp/Pi Sessions/session file.jsonl",
                [
                    "cd '/tmp/Fixture Project'",
                    "pi --session '/tmp/Pi Sessions/session file.jsonl'",
                ],
            ),
            ("vscode", "session $1", "/tmp/Fixture Project", "", []),
            ("cursor", "session $1", "/tmp/Fixture Project", "", []),
            ("codex", "", "/tmp/Fixture Project", "", []),
            ("codex", "unknown", "/tmp/Fixture Project", "", []),
        ]
        for source, session_id, repo, path, expected in cases:
            with self.subTest(source=source, session_id=session_id):
                self.assertEqual(
                    ss.native_resume_lines(source, session_id, repo, path),
                    expected,
                )

    def test_handoff_launch_lines_are_exact_and_shell_quoted(self):
        packet = pathlib.Path("/tmp/Packet Dir/context.md")
        prompt = (
            "'Read the context packet at /tmp/Packet Dir/context.md "
            "and continue the work.'"
        )
        cases = {
            "codex": [
                "codex -C '/tmp/Fixture Project' "
                f"--add-dir '/tmp/Packet Dir' {prompt}"
            ],
            "claude": [
                "cd '/tmp/Fixture Project'",
                f"claude --add-dir '/tmp/Packet Dir' {prompt}",
            ],
            "pi": [
                "cd '/tmp/Fixture Project'",
                f"pi {prompt}",
            ],
        }
        for target, expected in cases.items():
            with self.subTest(target=target):
                self.assertEqual(
                    ss.launch_lines_for_handoff(
                        target,
                        "/tmp/Fixture Project",
                        packet,
                    ),
                    expected,
                )


class AdapterHealthTest(unittest.TestCase):
    def test_health_distinguishes_missing_zero_content_and_parsed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = pathlib.Path(tmpdir)
            missing = ss.adapter_health(home, {"cursor"})[0]
            self.assertEqual(missing.status, "missing_store")
            self.assertEqual(missing.candidate_stores, 0)
            self.assertEqual(missing.parsed_documents, 0)
            self.assertEqual(missing.zero_content_stores, 0)
            self.assertEqual(missing.error_stores, 0)

            storage = (
                home
                / "Library"
                / "Application Support"
                / "Cursor"
                / "User"
                / "globalStorage"
            )
            storage.mkdir(parents=True)
            db = storage / "state.vscdb"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
            conn.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                ("workbench.chat.sessions.observed", json.dumps({"metadata": "only"})),
            )
            conn.commit()
            conn.close()

            zero = ss.adapter_health(home, {"cursor"})[0]
            self.assertEqual(zero.status, "candidate_store_zero_content")
            self.assertEqual(zero.candidate_stores, 1)
            self.assertEqual(zero.parsed_documents, 0)
            self.assertEqual(zero.zero_content_stores, 1)

            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                (
                    "workbench.chat.sessions.observed-content",
                    json.dumps(
                        {
                            "sessions": [
                                {
                                    "requests": [
                                        {
                                            "requestId": "request-1",
                                            "message": {
                                                "parts": [
                                                    {
                                                        "text": (
                                                            "Explain the sanitized "
                                                            "adapter health fixture."
                                                        )
                                                    }
                                                ]
                                            },
                                            "response": [
                                                {
                                                    "kind": "markdownContent",
                                                    "value": (
                                                        "The fixture contains no "
                                                        "private user data."
                                                    ),
                                                }
                                            ],
                                        }
                                    ]
                                }
                            ]
                        }
                    ),
                ),
            )
            conn.commit()
            conn.close()

            parsed = ss.adapter_health(home, {"cursor"})[0]
            self.assertEqual(parsed.status, "parsed_documents")
            self.assertEqual(parsed.candidate_stores, 1)
            self.assertGreater(parsed.parsed_documents, 0)
            self.assertEqual(parsed.zero_content_stores, 0)

            serialized = json.dumps(parsed.__dict__, sort_keys=True)
            self.assertNotIn(str(home), serialized)
            self.assertNotIn("adapter health fixture", serialized)

    def test_health_reports_partial_and_malformed_store_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = pathlib.Path(tmpdir)
            projects = home / ".claude" / "projects" / "fixture"
            projects.mkdir(parents=True)
            (projects / "valid.jsonl").write_text(
                json.dumps(
                    {
                        "sessionId": "valid",
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": "Parse this sanitized candidate record.",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (projects / "malformed.jsonl").write_text("[]\n", encoding="utf-8")
            (projects / "directory.jsonl").mkdir()

            health = ss.adapter_health(home, {"claude"})[0]

        self.assertEqual(health.status, "partial_store_drift")
        self.assertEqual(health.candidate_stores, 3)
        self.assertGreater(health.parsed_documents, 0)
        self.assertEqual(health.zero_content_stores, 2)
        self.assertEqual(health.error_stores, 2)

    def test_health_reports_malformed_records_inside_a_parsed_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = pathlib.Path(tmpdir)
            projects = home / ".claude" / "projects" / "fixture"
            projects.mkdir(parents=True)
            (projects / "mixed.jsonl").write_text(
                json.dumps(
                    {
                        "sessionId": "mixed",
                        "type": "user",
                        "message": {"role": "user", "content": "Valid record."},
                    }
                )
                + "\n{malformed\n",
                encoding="utf-8",
            )

            health = ss.adapter_health(home, {"claude"})[0]

        self.assertEqual(health.status, "partial_store_drift")
        self.assertEqual(health.parsed_documents, 1)
        self.assertEqual(health.zero_content_stores, 0)
        self.assertEqual(health.error_stores, 1)

    def test_doctor_reports_zero_content_drift_without_private_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = pathlib.Path(tmpdir)
            storage = (
                home
                / "Library"
                / "Application Support"
                / "Cursor"
                / "User"
                / "globalStorage"
            )
            storage.mkdir(parents=True)
            conn = sqlite3.connect(storage / "state.vscdb")
            conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
            conn.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                ("workbench.chat.sessions.drift", json.dumps({"metadata": "only"})),
            )
            conn.commit()
            conn.close()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = ss.main(
                    [
                        "--home",
                        str(home),
                        "doctor",
                        "--source",
                        "cursor",
                        "--strict",
                    ]
                )

        self.assertEqual(code, 1)
        rendered = output.getvalue()
        self.assertIn("Cursor: ATTENTION", rendered)
        self.assertIn("no session documents parsed", rendered)
        self.assertNotIn(str(home), rendered)

    def test_doctor_rejects_unknown_sources(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                ss.main(["doctor", "--source", "typo-source", "--strict"])
        self.assertEqual(raised.exception.code, 2)

    def test_observed_vscode_jsonl_request_shape_is_parsed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "observed.jsonl"
            records = [
                {"k": ["sessionId"], "v": {"sessionId": "observed-session"}},
                {"k": ["customTitle"], "v": "Sanitized observed fixture"},
                {
                    "k": ["requests"],
                    "v": [
                        {
                            "requestId": "request-2",
                            "message": {"text": "How does the parser handle this shape?"},
                            "result": {
                                "toolCallRounds": [
                                    {"response": "It extracts the response safely."}
                                ]
                            },
                        }
                    ],
                },
            ]
            path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )
            documents = list(ss.iter_vscode_chat_jsonl(path, "vscode"))
        combined = " ".join(document.text for document in documents)
        self.assertIn("parser handle this shape", combined)
        self.assertIn("extracts the response safely", combined)

    def test_vscode_workspace_metadata_maps_state_docs_to_the_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = pathlib.Path(tmpdir) / "workspaceStorage" / "hash"
            workspace.mkdir(parents=True)
            (workspace / "workspace.json").write_text(
                json.dumps({"folder": "file:///Users/alex/workspace/os/research/mapped"}),
                encoding="utf-8",
            )
            db = workspace / "state.vscdb"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
            conn.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                (
                    "workbench.chat.sessions.mapped",
                    json.dumps(
                        {
                            "sessions": [
                                {
                                    "requests": [
                                        {
                                            "requestId": "mapped-request",
                                            "message": {
                                                "parts": [
                                                    {"text": "Map this chat to its workspace."}
                                                ]
                                            },
                                        }
                                    ]
                                }
                            ]
                        }
                    ),
                ),
            )
            conn.commit()
            conn.close()

            documents = list(ss.iter_vscode_state_db(db, "vscode"))

        self.assertTrue(documents)
        self.assertEqual(documents[0].cwd, "/Users/alex/workspace/os/research/mapped")


class LeadingGlobalOptionDispatchTest(unittest.TestCase):
    def test_explicit_command_after_global_options_indexes_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            home = root / "home"
            store = home / ".claude" / "projects" / "fixture"
            store.mkdir(parents=True)
            (store / "session.jsonl").write_text(
                json.dumps(
                    {
                        "sessionId": "dispatch-session",
                        "cwd": "/tmp/dispatch-fixture",
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": "Index this explicit dispatch fixture.",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            db = root / "index.sqlite"
            with mock.patch.object(ss, "DEFAULT_LOCK", str(root / "index.lock")):
                code = ss.main(
                    [
                        "--db",
                        str(db),
                        "--home",
                        str(home),
                        "index",
                        "--reset",
                        "--quiet",
                    ]
                )
            self.assertEqual(code, 0)
            conn = sqlite3.connect(db)
            count = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
            conn.close()
            self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
