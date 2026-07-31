from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

import session_search as ss


class TrackedConnection:
    """Transparent connection proxy that records explicit ownership release."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.connection.close()

    def __enter__(self) -> "TrackedConnection":
        self.connection.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        return self.connection.__exit__(*args)

    def __getattr__(self, name: str) -> object:
        return getattr(self.connection, name)


class CliConnectionOwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.db = self.root / "index.sqlite"
        connection = ss.connect_db(self.db)
        ss.init_db(connection)
        ss.upsert_documents(
            connection,
            [
                ss.Document(
                    doc_id="codex:resource-test",
                    source="codex",
                    session_id="resource-test",
                    title="Resource ownership",
                    path=str(self.root / "session.jsonl"),
                    cwd=str(self.root / "project"),
                    role="user",
                    ts=100,
                    text="Verify every CLI command closes its SQLite connection.",
                    meta={},
                )
            ],
        )
        connection.close()
        self.connections: list[TrackedConnection] = []
        original_connect = ss.connect_db

        def tracked_connect(path: pathlib.Path | str) -> TrackedConnection:
            tracked = TrackedConnection(original_connect(path))
            self.connections.append(tracked)
            return tracked

        self.enterContext(mock.patch.object(ss, "connect_db", side_effect=tracked_connect))
        self.enterContext(mock.patch.object(ss, "session_lock", return_value=contextlib.nullcontext()))

    def run_command(self, command: object, **values: object) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return command(argparse.Namespace(db=str(self.db), **values))

    def assert_all_connections_closed(self) -> None:
        self.assertTrue(self.connections, "the command did not open a database")
        self.assertTrue(
            all(connection.closed for connection in self.connections),
            "the command returned with an owned SQLite connection still open",
        )

    def test_show_closes_the_connection_on_success(self) -> None:
        self.assertEqual(self.run_command(ss.cmd_show, doc_id="codex:resource-test"), 0)
        self.assert_all_connections_closed()

    def test_show_closes_the_connection_when_the_document_is_missing(self) -> None:
        self.assertEqual(self.run_command(ss.cmd_show, doc_id="missing-document"), 2)
        self.assert_all_connections_closed()

    def test_cards_closes_the_connection(self) -> None:
        self.assertEqual(self.run_command(ss.cmd_cards, limit=1), 0)
        self.assert_all_connections_closed()

    def test_embed_closes_the_connection(self) -> None:
        with (
            mock.patch.object(ss, "ensure_session_embeddings", return_value=0),
            mock.patch.object(ss, "ensure_embeddings", return_value=0),
            mock.patch.object(ss, "embedding_backend", return_value=None),
        ):
            self.assertEqual(self.run_command(ss.cmd_embed, limit=1), 0)
        self.assert_all_connections_closed()

    def test_direct_database_commands_close_after_initialization_failure(self) -> None:
        eval_file = self.root / "eval.json"
        eval_file.write_text("[]\n", encoding="utf-8")
        cases = [
            (
                ss.cmd_dashboard,
                {
                    "home": str(self.root),
                    "no_refresh": True,
                    "archived": False,
                    "limit": 10,
                    "source": "all",
                    "mode": "fts",
                },
            ),
            (
                ss.cmd_project,
                {
                    "project": ["Resource", "Ownership"],
                    "limit": 10,
                    "page": 1,
                    "source": "all",
                },
            ),
            (ss.cmd_archive_audit, {}),
            (
                ss.cmd_index,
                {
                    "home": str(self.root),
                    "source": "all",
                    "reset": False,
                    "quiet": True,
                },
            ),
            (
                ss.cmd_search,
                {
                    "query": "resource",
                    "limit": 10,
                    "source": "all",
                    "mode": "fts",
                    "home": str(self.root),
                    "no_refresh": True,
                },
            ),
            (ss.cmd_status, {}),
            (ss.cmd_cards, {"limit": 1}),
            (ss.cmd_embed, {"limit": 1}),
            (
                ss.cmd_eval,
                {
                    "file": str(eval_file),
                    "limit": 10,
                    "mode": "fts",
                    "refresh": False,
                    "home": str(self.root),
                    "verbose": False,
                },
            ),
        ]
        for command, values in cases:
            with self.subTest(command=command.__name__):
                self.connections.clear()
                with mock.patch.object(
                    ss,
                    "init_db",
                    side_effect=RuntimeError("initialization failed"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "initialization failed"):
                        self.run_command(command, **values)
                self.assert_all_connections_closed()

    def test_selector_commands_close_after_selection_failure(self) -> None:
        cases = [
            (ss.cmd_set_archive, {"selector": "codex:resource-test", "archived": True}),
            (ss.cmd_show, {"doc_id": "codex:resource-test"}),
            (ss.cmd_resume, {"selector": "codex:resource-test"}),
            (ss.cmd_continue, {"selector": "codex:resource-test", "target": ""}),
            (ss.cmd_handoff, {"selector": "codex:resource-test", "target": "codex"}),
        ]
        for command, values in cases:
            with self.subTest(command=command.__name__):
                self.connections.clear()
                with mock.patch.object(
                    ss,
                    "selected_row",
                    side_effect=RuntimeError("selection failed"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "selection failed"):
                        self.run_command(command, **values)
                self.assert_all_connections_closed()

    def test_read_only_probe_failure_closes_the_connection(self) -> None:
        candidate = self.root / "candidate.sqlite"
        candidate.touch()
        connection = mock.Mock()
        connection.execute.side_effect = sqlite3.OperationalError("probe failed")
        with mock.patch.object(sqlite3, "connect", return_value=connection):
            self.assertIsNone(ss.connect_ro_sqlite(candidate))
        connection.close.assert_called_once_with()

    def test_writable_pragma_failure_closes_the_connection(self) -> None:
        connection = mock.Mock()
        connection.execute.side_effect = sqlite3.OperationalError("pragma failed")
        with mock.patch.object(sqlite3, "connect", return_value=connection):
            with self.assertRaisesRegex(sqlite3.OperationalError, "pragma failed"):
                ss.connect_db(self.root / "pragma.sqlite")
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
