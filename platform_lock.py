"""Bounded POSIX file locking for session-search."""

from __future__ import annotations

import contextlib
import errno
import pathlib
import sys
import time
from collections.abc import Iterator


class LockTimeout(RuntimeError):
    pass


class UnsupportedPlatform(RuntimeError):
    pass


@contextlib.contextmanager
def file_lock(path: pathlib.Path, shared: bool = False, timeout: float = 5.0) -> Iterator[None]:
    if sys.platform not in {"darwin", "linux"}:
        raise UnsupportedPlatform(
            f"session-search locking supports macOS and Linux; found {sys.platform}"
        )
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod((path.parent.stat().st_mode & 0o777) & 0o700)
    except OSError:
        pass
    deadline = time.monotonic() + timeout
    with path.open("a") as handle:
        try:
            path.chmod((path.stat().st_mode & 0o777) & 0o600)
        except OSError:
            pass
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        while True:
            try:
                fcntl.flock(handle, mode | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"session-search lock timed out after {timeout:g}s") from exc
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
