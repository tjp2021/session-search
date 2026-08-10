#!/usr/bin/env python3
"""Offline acceptance test for an installed session-search console script."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import tempfile


def run(
    executable: pathlib.Path,
    args: list[str],
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(executable), *args],
        cwd=env["HOME"],
        env=env,
        input="",
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if completed.returncode:
        raise AssertionError(
            f"{args!r} exited {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return completed


def require(text: str, needle: str, step: str) -> None:
    if needle not in text:
        raise AssertionError(f"{step} did not contain {needle!r}:\n{text}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ss", required=True, help="Installed ss console executable.")
    args = parser.parse_args()
    executable = pathlib.Path(args.ss).resolve()
    if not executable.is_file():
        raise SystemExit(f"Installed console executable not found: {executable}")
    installed_python = executable.with_name("python")
    dependency_check = subprocess.run(
        [
            str(installed_python),
            "-c",
            (
                "import importlib.metadata, requests, session_search; "
                "assert importlib.metadata.version('session-search') == '0.2.2'; "
                "assert session_search.DEFAULT_EVALS.is_file(); "
                "print(session_search.__file__)"
            ),
        ],
        cwd=executable.parent,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if dependency_check.returncode:
        raise AssertionError(
            "installed runtime dependency check failed\n"
            f"stdout:\n{dependency_check.stdout}\n"
            f"stderr:\n{dependency_check.stderr}"
        )
    require(dependency_check.stdout, "site-packages", "installed import")

    with tempfile.TemporaryDirectory() as tmpdir:
        root = pathlib.Path(tmpdir)
        home = root / "home"
        project = home / "workspace" / "os" / "labs" / "acceptance"
        store = home / ".claude" / "projects" / "installed-acceptance"
        store.mkdir(parents=True)
        records = [
            {
                "sessionId": "installed-claude-session",
                "cwd": str(project),
                "type": "user",
                "timestamp": "2026-07-30T12:00:00Z",
                "message": {
                    "role": "user",
                    "content": (
                        "Find the offline-acceptance-needle and preserve "
                        "the installed recovery path."
                    ),
                },
            },
            {
                "sessionId": "installed-claude-session",
                "cwd": str(project),
                "type": "assistant",
                "timestamp": "2026-07-30T12:01:00Z",
                "message": {
                    "role": "assistant",
                    "content": "The installed recovery path is ready.",
                },
            },
        ]
        (store / "installed-claude-session.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        db = root / "data" / "acceptance.sqlite"
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(home),
                "SS_DATA_DIR": str(root / "data"),
                "SS_SUMMARIES": "off",
                "PYTHONNOUSERSITE": "1",
            }
        )
        env.pop("PYTHONPATH", None)
        env.pop("OPENROUTER_API_KEY", None)

        indexed = run(
            executable,
            [
                "--db",
                str(db),
                "--home",
                str(home),
                "index",
                "--reset",
            ],
            env,
        )
        require(indexed.stdout, "Indexed", "index")

        searched = run(
            executable,
            [
                "--db",
                str(db),
                "--home",
                str(home),
                "--no-refresh",
                "--mode",
                "fts",
                "offline-acceptance-needle",
            ],
            env,
        )
        # The query itself is echoed in the header, so asserting on it passes
        # even when no result card renders. Assert on rendered card content.
        # The query is echoed in the header, so asserting on it alone passes
        # even when no result card renders. Assert the card itself.
        require(searched.stdout, "offline-acceptance-needle", "natural search")
        require(searched.stdout, "\n1. ", "search rendered a ranked result")
        require(searched.stdout, "Found in: Claude Code", "search rendered a result card")
        require(searched.stdout, "open exact session: ss open 1", "search rendered a recovery action")

        project_view = run(
            executable,
            [
                "--db",
                str(db),
                "--home",
                str(home),
                "project",
                "Labs",
                "/",
                "Acceptance",
            ],
            env,
        )
        require(project_view.stdout, "SS project · Labs / Acceptance", "project")
        require(project_view.stdout, "Open: ss open 1", "project")

        opened = run(
            executable,
            ["--db", str(db), "--home", str(home), "open", "1"],
            env,
        )
        require(opened.stdout, "Open exact session:", "open")
        require(
            opened.stdout,
            "claude --resume installed-claude-session",
            "open",
        )
        require(opened.stdout, f"cd {project}", "open")

    print("Installed acceptance passed: index -> search -> project -> open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
