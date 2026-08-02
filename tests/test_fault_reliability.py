import argparse
import json
import errno
import contextlib
import io
import pathlib
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

import archive_intent
import archive_store
import platform_lock
import schema_migrations
import session_search as ss


def crash_writer(path: str, phase: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO status VALUES('archived')")
    if phase == "status":
        os._exit(91)
    conn.execute("INSERT INTO events VALUES('close')")
    if phase == "event":
        os._exit(92)
    conn.commit()


def hold_file_lock(path: str, ready, release) -> None:
    with platform_lock.file_lock(pathlib.Path(path), timeout=2):
        ready.set()
        release.wait(5)


class TransactionFaultTest(unittest.TestCase):
    def test_real_process_crashes_leave_no_partial_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "crash.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE status(value TEXT)")
            conn.execute("CREATE TABLE events(value TEXT)")
            conn.commit()
            conn.close()
            context = multiprocessing.get_context("spawn")
            for phase in ("status", "event"):
                process = context.Process(target=crash_writer, args=(str(path), phase))
                process.start()
                process.join(10)
                self.assertIn(process.exitcode, {91, 92})
                check = sqlite3.connect(path)
                self.assertEqual(check.execute("SELECT COUNT(*) FROM status").fetchone()[0], 0)
                self.assertEqual(check.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
                self.assertEqual(check.execute("PRAGMA quick_check").fetchone()[0], "ok")
                check.close()

    def test_actual_sqlite_full_rolls_back(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE payload(value BLOB)")
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        conn.execute(f"PRAGMA max_page_count = {pages + 1}")
        with self.assertRaises(sqlite3.OperationalError):
            with archive_store.immediate_transaction(conn):
                conn.execute("INSERT INTO payload VALUES(randomblob(1000000))")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM payload").fetchone()[0], 0)
        conn.close()

    def test_read_only_database_rejects_write(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "readonly.sqlite"
            writable = sqlite3.connect(path)
            writable.execute("CREATE TABLE proof(value TEXT)")
            writable.close()
            readonly = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            with self.assertRaises(sqlite3.OperationalError):
                with archive_store.immediate_transaction(readonly):
                    readonly.execute("INSERT INTO proof VALUES('no')")
            readonly.close()
    def test_nested_failure_preserves_outer_transaction(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE proof(value TEXT)")
        conn.execute("BEGIN")
        conn.execute("INSERT INTO proof VALUES('outer')")
        with self.assertRaises(RuntimeError):
            with archive_store.immediate_transaction(conn):
                conn.execute("INSERT INTO proof VALUES('inner')")
                raise RuntimeError("inner failure")
        self.assertTrue(conn.in_transaction)
        self.assertEqual(conn.execute("SELECT value FROM proof").fetchall(), [("outer",)])
        conn.rollback()
        conn.close()

    def test_nested_success_obeys_outer_rollback(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE proof(value TEXT)")
        conn.execute("BEGIN")
        with archive_store.immediate_transaction(conn):
            conn.execute("INSERT INTO proof VALUES('inner')")
        self.assertTrue(conn.in_transaction)
        conn.rollback()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM proof").fetchone()[0], 0)
        conn.close()

    def test_successful_transaction_commits(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE proof(value TEXT)")
        with archive_store.immediate_transaction(conn):
            conn.execute("INSERT INTO proof VALUES('committed')")
        self.assertEqual(conn.execute("SELECT value FROM proof").fetchone()[0], "committed")
        archive_store.quick_check(conn)
        conn.close()

    def test_failure_between_status_and_event_rolls_back(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE status(value TEXT)")
        conn.execute("CREATE TABLE events(value TEXT)")
        with self.assertRaises(RuntimeError):
            with archive_store.immediate_transaction(conn):
                conn.execute("INSERT INTO status VALUES('archived')")
                raise RuntimeError("injected event failure")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM status").fetchone()[0], 0)
        conn.close()

    def test_integrity_failure_is_typed(self):
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = ("corrupt",)
        with self.assertRaises(archive_store.StorageFailure):
            archive_store.quick_check(conn)

    def test_backup_restores_and_retains_five(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            database = root / "source.sqlite"
            conn = sqlite3.connect(database)
            conn.execute("CREATE TABLE proof(value TEXT)")
            conn.execute("INSERT INTO proof VALUES('intact')")
            conn.commit()
            latest = None
            for _ in range(7):
                latest = archive_store.backup_database(conn, root / "backups")
            conn.close()
            self.assertEqual(len(list((root / "backups").glob("session-search-*.sqlite"))), 5)
            restored = sqlite3.connect(latest)
            self.assertEqual(restored.execute("SELECT value FROM proof").fetchone()[0], "intact")
            restored.close()

    def test_backup_failure_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = mock.Mock()
            conn.backup.side_effect = sqlite3.OperationalError("full")
            with self.assertRaises(sqlite3.OperationalError):
                archive_store.backup_database(conn, pathlib.Path(temp))
            self.assertEqual(list(pathlib.Path(temp).glob(".session-search-*")), [])


class MigrationFaultTest(unittest.TestCase):
    def test_incomplete_statement_is_rejected(self):
        conn = sqlite3.connect(":memory:")
        with self.assertRaises(schema_migrations.MigrationFailure):
            schema_migrations.execute_statements(conn, "CREATE TABLE unfinished(")
        conn.close()

    def test_completed_cards_survive_an_interrupt_mid_build(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ss.init_db(conn)
        ss.upsert_documents(
            conn,
            [
                ss.Document(
                    doc_id=f"d{i}", source="codex", session_id=f"s{i}", title=f"S{i}",
                    path="/tmp/p", cwd="/tmp/w", role="user", ts=i,
                    text=f"Work item {i} needs a summary.", meta={},
                )
                for i in range(5)
            ],
        )
        calls = {"n": 0}

        def flaky(prompt: str, max_tokens: int = 220) -> str:
            calls["n"] += 1
            if calls["n"] > 4:
                raise KeyboardInterrupt("user pressed ctrl-c")
            return "Summarized the work item."

        with mock.patch.object(ss, "llm_summarize", side_effect=flaky):
            with mock.patch.object(ss, "llm_available", return_value=True):
                with self.assertRaises(KeyboardInterrupt):
                    ss.ensure_session_cards(conn)
        stored = conn.execute("SELECT COUNT(*) FROM session_cards").fetchone()[0]
        self.assertGreaterEqual(stored, 2, "cards completed before the interrupt must be durable")
        conn.close()

    def test_card_writes_work_inside_a_command_that_already_holds_the_lock(self):
        # cmd_dashboard holds the exclusive lock for its whole run and then asks
        # catch-up to build cards, which takes the lock per write. Without a
        # reentrant lock that inner acquire waits on this same process and the
        # dashboard dies with a lock timeout.
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ss.init_db(conn)
        ss.upsert_documents(
            conn,
            [
                ss.Document(
                    doc_id="d1", source="codex", session_id="s1", title="S1",
                    path="/tmp/p", cwd="/tmp/w", role="user", ts=1,
                    text="Work item needs a summary.", meta={},
                )
            ],
        )
        results = ss.recent_session_results(conn, 1)
        with mock.patch.object(ss, "llm_summarize", return_value="Summarized the work item."):
            with mock.patch.object(ss, "llm_available", return_value=True):
                with ss.session_lock(shared=False):
                    built = ss.catch_up_dashboard_cards(conn, results)
        self.assertEqual(built, 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session_cards").fetchone()[0], 1)
        conn.close()

    def test_card_building_does_not_hold_the_write_lock_across_a_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "cards.sqlite"
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            ss.init_db(conn)
            ss.upsert_documents(
                conn,
                [
                    ss.Document(
                        doc_id=f"d{i}", source="codex", session_id=f"s{i}", title=f"S{i}",
                        path="/tmp/p", cwd="/tmp/w", role="user", ts=i,
                        text=f"Work item {i} needs a summary.", meta={},
                    )
                    for i in range(4)
                ],
            )
            conn.commit()
            blocked: list[str] = []

            def probe(prompt: str, max_tokens: int = 220) -> str:
                # Stands in for time spent waiting on the provider.
                other = sqlite3.connect(path, timeout=0.2)
                try:
                    other.execute("BEGIN IMMEDIATE")
                    other.rollback()
                except sqlite3.OperationalError as error:
                    blocked.append(str(error))
                finally:
                    other.close()
                return "Summarized the work item."

            with mock.patch.object(ss, "llm_summarize", side_effect=probe):
                with mock.patch.object(ss, "llm_available", return_value=True):
                    ss.ensure_session_cards(conn)
            self.assertEqual(
                blocked, [], "the write transaction was held open across a model call"
            )
            conn.close()

    def test_v4_card_cache_is_rebuilt_with_a_provenance_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "legacy.sqlite"
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            ss.init_db(conn)
            conn.execute("DROP TABLE session_cards")
            conn.execute(
                """
                CREATE TABLE session_cards (
                    source TEXT NOT NULL, session_id TEXT NOT NULL,
                    text_hash TEXT NOT NULL, title TEXT NOT NULL, repo TEXT NOT NULL,
                    last_active INTEGER, what_this_was TEXT NOT NULL,
                    what_happened TEXT NOT NULL, next_clue TEXT NOT NULL,
                    mentioned_paths_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
                    built_at INTEGER NOT NULL, PRIMARY KEY (source, session_id)
                )
                """
            )
            conn.execute(
                "INSERT INTO session_cards VALUES('codex','s1','h','t','r',1,'stale','','','[]','[]',1)"
            )
            conn.execute("PRAGMA user_version = 4")
            conn.commit()
            conn.close()

            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            ss.init_db(conn)
            self.assertEqual(
                schema_migrations.schema_version(conn), schema_migrations.CURRENT_SCHEMA_VERSION
            )
            columns = schema_migrations.table_columns(conn, "session_cards")
            self.assertIn("summary_source", columns)
            # The cache is disposable; a legacy row must not survive claiming a
            # provenance it never had.
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM session_cards").fetchone()[0], 0)
            conn.close()

    def test_a_card_cache_missing_provenance_fails_validation(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ss.init_db(conn)
        conn.execute("ALTER TABLE session_cards DROP COLUMN summary_source")
        with self.assertRaises(schema_migrations.MigrationFailure):
            schema_migrations.validate_current_schema(conn)
        conn.close()

    def test_current_schema_is_fully_validated(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ss.init_db(conn)
        schema_migrations.validate_current_schema(conn)
        conn.execute("DROP TABLE session_archive_events")
        with self.assertRaises(schema_migrations.MigrationFailure):
            schema_migrations.validate_current_schema(conn)
        conn.close()

    def test_non_fts_documents_table_is_rejected(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ss.init_db(conn)
        conn.execute("DROP TABLE documents_fts")
        conn.execute("CREATE TABLE documents_fts(value TEXT)")
        with self.assertRaises(schema_migrations.MigrationFailure):
            schema_migrations.validate_schema(conn)
        conn.close()

    def test_partial_migration_rolls_back_and_retries(self):
        conn = sqlite3.connect(":memory:")
        bad = "CREATE TABLE first(value TEXT);\nCREATE INDEX broken ON missing(value);"
        with self.assertRaises(sqlite3.OperationalError):
            with archive_store.immediate_transaction(conn):
                schema_migrations.execute_statements(conn, bad)
        self.assertIsNone(
            conn.execute("SELECT name FROM sqlite_master WHERE name='first'").fetchone()
        )
        with archive_store.immediate_transaction(conn):
            schema_migrations.execute_statements(conn, "CREATE TABLE first(value TEXT);")
        self.assertIsNotNone(
            conn.execute("SELECT name FROM sqlite_master WHERE name='first'").fetchone()
        )
        conn.close()

    def test_incomplete_legacy_schema_is_not_stamped(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE session_archive_status(source TEXT)")
        with self.assertRaises(schema_migrations.MigrationFailure):
            schema_migrations.preflight_schema(conn)
        self.assertEqual(schema_migrations.schema_version(conn), 0)
        conn.close()

    def test_newer_schema_fails_closed(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA user_version = 99")
        with self.assertRaises(schema_migrations.MigrationFailure):
            schema_migrations.preflight_schema(conn)
        conn.close()

    def test_fake_current_schema_is_rejected(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(f"PRAGMA user_version = {schema_migrations.CURRENT_SCHEMA_VERSION}")
        with self.assertRaises(schema_migrations.MigrationFailure):
            schema_migrations.validate_current_schema(conn)
        conn.close()


class HostileIntentTest(unittest.TestCase):
    def test_controls_and_oversized_prompts_fail_closed(self):
        cases = (
            "Close\u200b this session.",
            "\u202eClose this session.",
            "Close this session.\x00",
            ("background " * 7000) + "Close this session.",
            "`close this session",
        )
        for text in cases:
            with self.subTest(text=text[:30]):
                self.assertIs(
                    archive_intent.classify_session_intent(text).kind,
                    archive_intent.IntentKind.NONE,
                )


class PlatformLockTest(unittest.TestCase):
    def test_real_competing_process_times_out(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(pathlib.Path(temp) / "lock")
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            process = context.Process(target=hold_file_lock, args=(path, ready, release))
            process.start()
            self.assertTrue(ready.wait(5))
            try:
                with self.assertRaises(platform_lock.LockTimeout):
                    with platform_lock.file_lock(pathlib.Path(path), timeout=0.05):
                        pass
            finally:
                release.set()
                process.join(5)
            self.assertEqual(process.exitcode, 0)

    def test_mac_lock_acquires_and_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            with platform_lock.file_lock(pathlib.Path(temp) / "lock", timeout=0.1):
                self.assertTrue((pathlib.Path(temp) / "lock").exists())

    def test_unsupported_platform_fails_clearly(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            platform_lock.sys, "platform", "win32"
        ):
            with self.assertRaises(platform_lock.UnsupportedPlatform):
                with platform_lock.file_lock(pathlib.Path(temp) / "lock"):
                    pass

    def test_busy_lock_times_out(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "fcntl.flock", side_effect=OSError(errno.EAGAIN, "busy")
        ), mock.patch.object(platform_lock.time, "monotonic", side_effect=(0.0, 1.0)):
            with self.assertRaises(platform_lock.LockTimeout):
                with platform_lock.file_lock(pathlib.Path(temp) / "lock", timeout=0.1):
                    pass

    def test_busy_lock_retries_then_acquires(self):
        busy = OSError(errno.EAGAIN, "busy")
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "fcntl.flock", side_effect=(busy, None, None)
        ), mock.patch.object(platform_lock.time, "monotonic", side_effect=(0.0, 0.01, 0.02)):
            with platform_lock.file_lock(pathlib.Path(temp) / "lock", timeout=0.1):
                pass

    def test_unexpected_lock_error_propagates(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "fcntl.flock", side_effect=OSError(errno.EPERM, "denied")
        ):
            with self.assertRaises(OSError):
                with platform_lock.file_lock(pathlib.Path(temp) / "lock"):
                    pass


class DamagedIndexRecoveryTest(unittest.TestCase):
    """An interrupted index must not trap the user in an unusable state."""

    def test_reset_quarantines_an_unreadable_index(self):
        with tempfile.TemporaryDirectory() as temp:
            db = pathlib.Path(temp) / "sessions.sqlite"
            db.write_bytes(b"SQLite format 3\x00interrupted-index" + b"\x00" * 512)
            (db.parent / f"{db.name}-wal").write_bytes(b"stale")
            damaged = ss.quarantine_damaged_index(db)
            self.assertEqual(damaged, db.parent / "sessions.sqlite.damaged")
            self.assertFalse(db.exists())
            self.assertFalse((db.parent / f"{db.name}-wal").exists())
            self.assertTrue(damaged.exists())

    def test_healthy_index_is_never_moved_aside(self):
        with tempfile.TemporaryDirectory() as temp:
            db = pathlib.Path(temp) / "sessions.sqlite"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE keep (id INTEGER)")
            conn.commit()
            conn.close()
            self.assertIsNone(ss.quarantine_damaged_index(db))
            self.assertTrue(db.exists())
            self.assertFalse((db.parent / "sessions.sqlite.damaged").exists())

    def test_healthy_locked_index_is_never_quarantined(self):
        with tempfile.TemporaryDirectory() as temp:
            db = pathlib.Path(temp) / "sessions.sqlite"
            holder = sqlite3.connect(db)
            holder.execute("CREATE TABLE proof(value TEXT)")
            holder.execute("INSERT INTO proof VALUES('intact')")
            holder.commit()
            holder.execute("BEGIN EXCLUSIVE")
            holder.execute("INSERT INTO proof VALUES('inflight')")

            real_connect = sqlite3.connect

            def connect_with_short_timeout(path):
                return real_connect(path, timeout=0.05)

            try:
                with mock.patch.object(
                    ss.sqlite3, "connect", side_effect=connect_with_short_timeout
                ):
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        ss.quarantine_damaged_index(db)
            finally:
                holder.rollback()
                holder.close()

            self.assertTrue(db.exists())
            self.assertFalse(db.with_name(db.name + ".damaged").exists())
            check = sqlite3.connect(db)
            self.assertEqual(
                check.execute("SELECT value FROM proof").fetchall(), [("intact",)]
            )
            check.close()

    def test_torn_page_is_quarantined_not_just_a_bad_header(self):
        """An interrupted write usually tears a page, not the header.

        PRAGMA schema_version reads only the header cookie, so it passes a
        torn page and the rebuild then dies. The probe must walk the b-trees.
        """
        with tempfile.TemporaryDirectory() as temp:
            db = pathlib.Path(temp) / "sessions.sqlite"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT)")
            conn.executemany(
                "INSERT INTO documents (text) VALUES (?)", [("x" * 400,) for _ in range(1500)]
            )
            conn.commit()
            conn.close()
            page = 4096
            raw = bytearray(db.read_bytes())
            self.assertGreater(len(raw), page * 4, "fixture must span several pages")
            raw[page * 3 : page * 4] = b"\x00" * page
            db.write_bytes(bytes(raw))

            # The old probe cannot see this damage.
            conn = sqlite3.connect(db)
            self.assertIsNotNone(conn.execute("PRAGMA schema_version").fetchone())
            conn.close()

            damaged = ss.quarantine_damaged_index(db)
            self.assertEqual(damaged, db.parent / "sessions.sqlite.damaged")
            self.assertFalse(db.exists())

    def test_torn_page_index_still_rebuilds_through_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            db = root / "sessions.sqlite"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT)")
            conn.executemany(
                "INSERT INTO documents (text) VALUES (?)", [("x" * 400,) for _ in range(1500)]
            )
            conn.commit()
            conn.close()
            page = 4096
            raw = bytearray(db.read_bytes())
            raw[page * 3 : page * 4] = b"\x00" * page
            db.write_bytes(bytes(raw))

            args = argparse.Namespace(
                db=str(db), home=str(root), source="all", reset=True, quiet=True
            )
            self.assertEqual(ss.cmd_index(args), 0)
            self.assertTrue((root / "sessions.sqlite.damaged").exists())
            conn = sqlite3.connect(db)
            try:
                tables = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            finally:
                conn.close()
            self.assertIn("documents", tables)

    def test_missing_index_needs_no_quarantine(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(ss.quarantine_damaged_index(pathlib.Path(temp) / "absent.sqlite"))

    def test_index_reset_rebuilds_after_an_interrupted_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            db = root / "sessions.sqlite"
            db.write_bytes(b"SQLite format 3\x00interrupted-index" + b"\x00" * 512)
            args = argparse.Namespace(
                db=str(db), home=str(root), source="all", reset=True, quiet=True
            )
            self.assertEqual(ss.cmd_index(args), 0)
            conn = sqlite3.connect(db)
            try:
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            finally:
                conn.close()
            self.assertIn("documents", tables)
            self.assertTrue((root / "sessions.sqlite.damaged").exists())


class CliFailureTest(unittest.TestCase):
    def test_lock_timeout_returns_exit_four(self):
        with mock.patch.object(ss, "_main", side_effect=platform_lock.LockTimeout("busy")):
            self.assertEqual(ss.main([]), 4)

    def test_migration_failure_returns_exit_five(self):
        with mock.patch.object(
            ss, "_main", side_effect=schema_migrations.MigrationFailure("partial")
        ):
            self.assertEqual(ss.main([]), 5)

    def test_storage_failure_returns_exit_three(self):
        with mock.patch.object(
            ss, "_main", side_effect=archive_store.StorageFailure("read only")
        ):
            self.assertEqual(ss.main([]), 3)

    def test_directory_fsync_failure_keeps_committed_selector(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "context.json"
            real_fsync = ss.os.fsync
            calls = 0

            def fail_directory(fd):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("directory sync failed")
                return real_fsync(fd)

            stderr = io.StringIO()
            with mock.patch.object(ss.os, "fsync", side_effect=fail_directory), contextlib.redirect_stderr(stderr):
                ss.atomic_write_json(path, {"schema_version": 2, "results": []})
            self.assertEqual(json.loads(path.read_text())["schema_version"], 2)
            self.assertIn("committed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
