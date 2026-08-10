import multiprocessing
import pathlib
import sqlite3
import tempfile
import threading
import time
import unittest

import session_search as ss


def transition_worker(db_path: str, lock_path: str, worker: int, errors) -> None:
    ss.DEFAULT_LOCK = lock_path
    try:
        for index in range(25):
            with ss.session_lock(shared=False):
                conn = ss.connect_db(pathlib.Path(db_path))
                ss.init_db(conn)
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT COALESCE(status_at, 0) FROM session_archive_status "
                    "WHERE source = 'codex' AND session_id = 'contended'"
                ).fetchone()
                event_ts = int(current[0] if current else 0) + 1
                ss.apply_archive_transition(
                    conn,
                    "codex",
                    "contended",
                    bool(event_ts % 2),
                    "concurrency-test",
                    evidence=f"worker {worker} event {index}",
                    evidence_ts=event_ts,
                    evidence_doc_id=f"{event_ts:06d}-{worker:02d}-{index:02d}",
                )
                conn.commit()
                conn.close()
    except Exception as exc:
        errors.put(repr(exc))


class ArchiveConcurrencyTest(unittest.TestCase):
    def test_threads_cannot_bypass_the_reentrant_process_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            old_lock = ss.DEFAULT_LOCK
            ss.DEFAULT_LOCK = str(pathlib.Path(temp) / "sessions.lock")
            first_entered = threading.Event()
            release_first = threading.Event()
            second_entered = threading.Event()

            def first() -> None:
                with ss.session_lock(shared=False):
                    first_entered.set()
                    release_first.wait(2)

            def second() -> None:
                first_entered.wait(2)
                with ss.session_lock(shared=False):
                    second_entered.set()

            first_thread = threading.Thread(target=first)
            second_thread = threading.Thread(target=second)
            try:
                first_thread.start()
                second_thread.start()
                self.assertTrue(first_entered.wait(1))
                time.sleep(0.05)
                self.assertFalse(second_entered.is_set())
                release_first.set()
                first_thread.join(2)
                second_thread.join(2)
                self.assertTrue(second_entered.is_set())
            finally:
                release_first.set()
                first_thread.join(2)
                second_thread.join(2)
                ss.DEFAULT_LOCK = old_lock

    def test_eight_writers_complete_without_lock_errors_or_missing_events(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = pathlib.Path(temp) / "sessions.sqlite"
            lock_path = pathlib.Path(temp) / "sessions.lock"
            conn = ss.connect_db(db_path)
            ss.init_db(conn)
            conn.close()
            context = multiprocessing.get_context("spawn")
            errors = context.Queue()
            workers = [
                context.Process(
                    target=transition_worker,
                    args=(str(db_path), str(lock_path), worker, errors),
                )
                for worker in range(8)
            ]
            for process in workers:
                process.start()
            for process in workers:
                process.join(20)
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(errors.empty())

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            events = conn.execute(
                "SELECT COUNT(*) FROM session_archive_events WHERE session_id = 'contended'"
            ).fetchone()[0]
            status = conn.execute(
                "SELECT archived, status_at FROM session_archive_status WHERE session_id = 'contended'"
            ).fetchone()
            self.assertEqual(events, 200)
            self.assertEqual(status["status_at"], 200)
            self.assertEqual(status["archived"], 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
