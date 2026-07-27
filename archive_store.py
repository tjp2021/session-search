"""Transactional boundary for SS archive state."""

from __future__ import annotations

import contextlib
import itertools
import os
import pathlib
import sqlite3
import tempfile
import time
from collections.abc import Iterator


class StorageFailure(RuntimeError):
    pass


_SAVEPOINTS = itertools.count()


@contextlib.contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    owned = not conn.in_transaction
    savepoint = f"ss_nested_{next(_SAVEPOINTS)}"
    try:
        if owned:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute(f"SAVEPOINT {savepoint}")
        yield
        if owned:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        if owned and conn.in_transaction:
            conn.rollback()
        elif conn.in_transaction:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def quick_check(conn: sqlite3.Connection) -> None:
    result = conn.execute("PRAGMA quick_check").fetchone()
    if not result or str(result[0]).lower() != "ok":
        raise StorageFailure(f"SQLite integrity check failed: {result[0] if result else 'no result'}")


def backup_database(
    conn: sqlite3.Connection,
    backup_dir: pathlib.Path,
    keep: int = 5,
) -> pathlib.Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"session-search-{time.time_ns()}.sqlite"
    fd, temporary = tempfile.mkstemp(prefix=".session-search-", suffix=".sqlite", dir=backup_dir)
    os.close(fd)
    temporary_path = pathlib.Path(temporary)
    destination = sqlite3.connect(temporary_path)
    try:
        conn.backup(destination)
        destination.commit()
        destination.close()
        os.replace(temporary_path, target)
    except Exception:
        destination.close()
        temporary_path.unlink(missing_ok=True)
        raise
    backups = sorted(backup_dir.glob("session-search-*.sqlite"), reverse=True)
    for stale in backups[keep:]:
        stale.unlink()
    return target
