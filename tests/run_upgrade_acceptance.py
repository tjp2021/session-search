#!/usr/bin/env python3
"""Prove that v0.2.0 state remains usable after a candidate-wheel upgrade."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import subprocess
import tempfile


def checked(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args, env=env, cwd=cwd, text=True, capture_output=True, check=False, timeout=30
    )
    if result.returncode:
        raise AssertionError(f"{args!r} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


def main() -> int:
    signal.signal(signal.SIGALRM, lambda *_args: (_ for _ in ()).throw(TimeoutError("upgrade acceptance exceeded 90 seconds")))
    signal.alarm(90)
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous-wheel", required=True)
    parser.add_argument("--candidate-wheel", required=True)
    args = parser.parse_args()
    # pip refuses to reinstall a same-version wheel and still exits 0, so
    # identical inputs would make every assertion below read the old build.
    previous = pathlib.Path(args.previous_wheel).resolve()
    candidate = pathlib.Path(args.candidate_wheel).resolve()
    if previous == candidate:
        raise SystemExit(f"previous and candidate wheels are the same file: {candidate}")
    if "0.2.0" not in previous.name or "0.2.2" not in candidate.name:
        raise SystemExit(
            f"expected a 0.2.0 previous wheel and a 0.2.2 candidate, got "
            f"{previous.name} and {candidate.name}"
        )

    with tempfile.TemporaryDirectory(prefix="ss-upgrade-") as tmpdir:
        root = pathlib.Path(tmpdir)
        venv = root / "venv"
        home = root / "home"
        data = root / "data"
        project = home / "workspace" / "os" / "personal" / "career"
        store = home / ".claude" / "projects" / "upgrade"
        store.mkdir(parents=True)
        record = {
            "sessionId": "upgrade-session",
            "cwd": str(project),
            "type": "user",
            "timestamp": "2026-07-30T12:00:00Z",
            "message": {"role": "user", "content": "Preserve the upgrade acceptance selector."},
        }
        (store / "upgrade-session.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        # Every child runs from the disposable root. The repository working
        # copy must never leak onto sys.path and impersonate the wheel.
        work = str(root)
        checked([os.sys.executable, "-m", "venv", str(venv)], cwd=work)
        python = venv / "bin" / "python"
        ss = venv / "bin" / "ss"
        checked([str(python), "-m", "pip", "install", args.previous_wheel], cwd=work)
        env = dict(os.environ, HOME=str(home), SS_DATA_DIR=str(data), SS_SUMMARIES="off", PYTHONNOUSERSITE="1")
        env.pop("PYTHONPATH", None)
        checked([str(ss), "index", "--reset"], env=env, cwd=work)
        checked([str(ss), "--no-refresh", "upgrade", "acceptance", "selector"], env=env, cwd=work)
        checked([str(ss), "archive", "1"], env=env, cwd=work)
        selector_before = (data / "last-results.json").read_text(encoding="utf-8")

        checked([str(python), "-m", "pip", "install", "--upgrade", args.candidate_wheel], cwd=work)
        active = checked(
            [
                str(python),
                "-P",
                "-c",
                (
                    "import importlib.metadata, session_search; "
                    "assert importlib.metadata.version('session-search') == '0.2.2'; "
                    "assert hasattr(session_search, 'dashboard_prompt_text'); "
                    "assert hasattr(session_search, 'quarantine_damaged_index'); "
                    "print(session_search.__file__)"
                ),
            ],
            env=env,
            cwd=work,
        )
        if str(venv) not in active.stdout:
            raise AssertionError(f"candidate module did not load from the upgrade venv: {active.stdout}")
        if (data / "last-results.json").read_text(encoding="utf-8") != selector_before:
            raise AssertionError("wheel installation rewrote historical selector state")

        archived = checked([str(ss), "archived", "--no-refresh"], env=env, cwd=work)
        if "Preserve the upgrade acceptance selector" not in archived.stdout:
            raise AssertionError(f"archived v0.2.0 data disappeared after upgrade:\n{archived.stdout}")
        reopened_search = checked([str(ss), "--no-refresh", "upgrade", "acceptance", "selector"], env=env, cwd=work)
        if "Preserve the upgrade acceptance selector" not in reopened_search.stdout:
            raise AssertionError(f"old indexed data is not searchable after upgrade:\n{reopened_search.stdout}")
        opened = checked([str(ss), "open", "1"], env=env, cwd=work)
        if "claude --resume upgrade-session" not in opened.stdout:
            raise AssertionError(f"upgraded selector did not reopen the old session:\n{opened.stdout}")
        project_view = checked([str(ss), "project", "Personal", "/", "Career"], env=env, cwd=work)
        if "SS project · Personal / Career" not in project_view.stdout:
            raise AssertionError(f"old project data is not browsable after upgrade:\n{project_view.stdout}")

    signal.alarm(0)
    print("Upgrade acceptance passed: v0.2.0 state -> candidate wheel")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
