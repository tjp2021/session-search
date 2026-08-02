#!/usr/bin/env python3
"""Black-box user journeys against an installed ``ss`` console command."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import unicodedata

# Preparing a pilot archive uses only the standard library, so a facilitator
# can build one from a bare clone. The journeys below need both extras.
try:
    import pexpect
except ModuleNotFoundError:
    pexpect = None

try:
    from wcwidth import wcswidth
except ModuleNotFoundError:
    wcswidth = None


TIMEOUT = 20
WIDTHS = (40, 60, 80, 120)
# C0 except tab and newline, DEL, and the whole C1 block. U+009B alone is a
# single-character CSI, so C1 text can drive the terminal without an escape.
CONTROL_CHARS = frozenset(
    chr(code)
    for code in [*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), *range(0x7F, 0xA0)]
)


def iso(epoch: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def write_claude_session(
    home: pathlib.Path,
    *,
    session_id: str,
    cwd: pathlib.Path,
    timestamp: int,
    title: str,
    user: str,
    assistant: str,
) -> None:
    store = home / ".claude" / "projects" / cwd.name
    store.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "sessionId": session_id,
            "cwd": str(cwd),
            "type": "user",
            "timestamp": iso(timestamp),
            "message": {"role": "user", "content": user},
        },
        {"type": "ai-title", "aiTitle": title},
        {
            "sessionId": session_id,
            "cwd": str(cwd),
            "type": "assistant",
            "timestamp": iso(timestamp + 30),
            "message": {"role": "assistant", "content": assistant},
        },
    ]
    (store / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def write_other_adapter_stores(home: pathlib.Path, now: int) -> None:
    """Keep all public adapters inside the installed user journey fixture."""
    cwd = home / "workspace" / "os" / "research" / "cross-tool"
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
            "codex-human-task",
            now - 4 * 86400,
            str(cwd),
            "Codex recovery fixture",
            "Recover a cross-tool Codex session.",
            "Prepared the Codex recovery evidence.",
            now - 4 * 86400,
            "main",
        ),
    )
    conn.commit()
    conn.close()

    pi = home / ".pi" / "agent" / "sessions" / "fixture"
    pi.mkdir(parents=True)
    pi_records = [
        {"type": "session", "id": "pi-human-task", "cwd": str(cwd)},
        {"type": "session_info", "id": "pi-info", "name": "Pi recovery fixture"},
        {
            "type": "message",
            "id": "pi-message",
            "timestamp": iso(now - 4 * 86400 - 60),
            "message": {"role": "user", "content": "Recover a cross-tool Pi session."},
        },
    ]
    (pi / "pi-human-task.jsonl").write_text(
        "\n".join(json.dumps(record) for record in pi_records) + "\n",
        encoding="utf-8",
    )

    for app, message in (
        ("Code", "Recover a VS Code chat session."),
        ("Cursor", "Recover a Cursor chat session."),
    ):
        storage = home / "Library" / "Application Support" / app / "User" / "globalStorage"
        storage.mkdir(parents=True)
        conn = sqlite3.connect(storage / "state.vscdb")
        conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            ("workbench.chat.sessions.human-task", json.dumps({"chatSessions": [{"text": message}]})),
        )
        conn.commit()
        conn.close()


def build_home(home: pathlib.Path, now: int) -> dict[str, pathlib.Path]:
    career = home / "workspace" / "os" / "personal" / "career"
    osmo = home / "workspace" / "os" / "_shared" / "osmo"
    resume_text = (
        "Where did I change the resume? I changed the resume summary for the Braze application. "
        "The final claim must use verified evidence only."
    )
    write_claude_session(
        home,
        session_id="resume-target",
        cwd=career,
        timestamp=now - 60,
        title="Braze resume evidence repair",
        user=resume_text,
        assistant=(
            "Completed the verified resume summary. Next: rebuild the PDF and submit the Braze packet."
        ),
    )
    write_claude_session(
        home,
        session_id="resume-distractor",
        cwd=home / "workspace" / "os" / "research" / "resume-formats",
        timestamp=now - 3600,
        title="Resume file format research",
        user="Compare resume file formats and typography without changing any application.",
        assistant="Documented font and export options. Next: keep researching file formats.",
    )
    for index in range(10):
        write_claude_session(
            home,
            session_id=f"recent-ops-{index + 1}",
            cwd=home / "workspace" / "os" / "_ops" / "recent-work",
            timestamp=now - 120 - index * 60,
            title=f"Recent operations task {index + 1}",
            user=f"Review recent operations task {index + 1} and verify its deployment evidence.",
            assistant=f"Verified operations task {index + 1}. Next: monitor deployment {index + 1}.",
        )
    for index in range(205):
        # Plant ESC, BEL, DEL, and two C1 bytes. U+009B is a bare CSI and
        # U+0090 opens a device-control string, so both drive a terminal
        # without any ESC prefix.
        marker = (
            "Unicode café 🚀 \x1b[31munsafe\x1b[0m \x07 \x9b31m \x7f \x90"
            if index == 0
            else f"older learning task {index + 1}"
        )
        write_claude_session(
            home,
            session_id=f"osmo-old-{index + 1}",
            cwd=osmo,
            timestamp=now - (3 * 86400) - index * 60,
            title=f"Osmo curriculum task {index + 1}",
            user=(
                f"{marker}. Review the agent curriculum with a deliberately-long-token-"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 and remove unsafe control text."
            ),
            assistant=f"Completed curriculum review {index + 1}. Next: publish lesson {index + 1}.",
        )
    write_other_adapter_stores(home, now)
    return {"career": career, "osmo": osmo}


def run(
    executable: pathlib.Path,
    args: list[str],
    env: dict[str, str],
    *,
    expected: int = 0,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    completed = subprocess.run(
        [str(executable), *args],
        cwd=env["HOME"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT,
    )
    elapsed = time.monotonic() - started
    if completed.returncode != expected:
        raise AssertionError(
            f"{args!r} exited {completed.returncode}, expected {expected}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    if elapsed >= TIMEOUT:
        raise AssertionError(f"journey step exceeded {TIMEOUT}s: {args!r}")
    return completed


def require(text: str, needle: str, step: str) -> None:
    if needle not in text:
        raise AssertionError(f"{step} did not contain {needle!r}:\n{text}")


def card_field(block: str, label: str, following: tuple[str, ...]) -> str:
    start = block.find(f"│ {label}: ")
    if start < 0:
        return ""
    content = block[start + len(f"│ {label}: ") :]
    stops = [content.find(f"│ {name}: ") for name in following]
    stops = [stop for stop in stops if stop >= 0]
    if stops:
        content = content[: min(stops)]
    lines = [re.sub(r"^│\s*", "", line).rsplit("│", 1)[0].strip() for line in content.splitlines()]
    return " ".join(part for part in lines if part).strip()


def visible_width(line: str) -> int:
    """Return the printed width, or -1 when the line carries a control byte.

    wcswidth returns -1 for any wcwidth-illegal character. Clamping that to 0
    would exempt exactly the lines that carry control text from the width
    contract, so the caller must treat -1 as a fault instead.
    """
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).rstrip("\r")
    return wcswidth(unicodedata.normalize("NFC", clean))


def pty_dashboard(
    executable: pathlib.Path,
    common: list[str],
    env: dict[str, str],
    width: int,
    actions: list[tuple[str, object]],
) -> str:
    pty_env = dict(env, COLUMNS=str(width), LINES="30")
    child = pexpect.spawn(
        str(executable),
        common,
        cwd=env["HOME"],
        env=pty_env,
        encoding="utf-8",
        timeout=TIMEOUT,
        echo=False,
        dimensions=(30, width),
    )
    transcript: list[str] = []
    root_prompt = (
        "Open N, choose project Pn, type search words, or q: "
        if width >= 52
        else "Open N, Pn, search, or q: "
    )
    project_prompt = (
        "Open N, n next, b back, type search words, or q: "
        if width >= 49
        else "Open N, n, b, search, or q: "
    )
    child.expect(root_prompt)
    transcript.append(child.before)
    reached_eof = False
    for sent, expected in actions:
        child.sendline(sent)
        pattern = root_prompt if expected == "root" else project_prompt if expected == "project" else expected
        child.expect(pattern)
        transcript.append(child.before)
        if isinstance(child.after, str):
            transcript.append(child.after)
        if expected is pexpect.EOF:
            reached_eof = True
    if not reached_eof:
        child.expect(pexpect.EOF)
        transcript.append(child.before)
    # Reap the child first. Before close() the exit status is always None, so
    # checking it here without waiting can never observe a failure.
    child.close()
    if child.exitstatus != 0 or child.signalstatus is not None:
        raise AssertionError(
            f"interactive dashboard exited {child.exitstatus}, "
            f"signal {child.signalstatus}: {''.join(transcript)}"
        )
    return "".join(transcript)


PROBE_SOURCE = '''
"""Run the installed package under an audit hook that refuses outside writes."""

import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ["SS_PROBE_ROOT"]).resolve()
WRITE_MODES = frozenset("wax+")
# os.open() and tempfile.mkstemp() raise the ``open`` audit event with mode
# None and the raw flags instead. Guarding on mode alone misses every one of
# them, and those calls write this product's durable selector state.
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
PATH_EVENTS = frozenset(
    {"os.mkdir", "os.rmdir", "os.remove", "os.truncate", "os.chmod", "sqlite3.connect"}
)
PAIR_EVENTS = frozenset({"os.rename", "os.link", "os.symlink"})
# Empty means enforce every event. A negative control narrows this so it can
# prove one branch fires, instead of always tripping on the earliest event.
ENFORCED = frozenset(name for name in os.environ.get("SS_PROBE_EVENTS", "").split(",") if name)


def contained(path):
    if isinstance(path, int) or path is None:
        return True
    try:
        resolved = pathlib.Path(os.fsdecode(path)).resolve()
    except (OSError, ValueError, TypeError):
        return True
    return resolved == ROOT or ROOT in resolved.parents


def refuse(event, path):
    sys.stderr.write("SS_WRITE_ESCAPE %s %s\\n" % (event, path))
    sys.stderr.flush()
    os._exit(97)


def hook(event, args):
    if ENFORCED and event not in ENFORCED:
        return
    if event == "open":
        path = args[0]
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        if mode:
            writing = bool(WRITE_MODES & frozenset(mode))
        else:
            writing = isinstance(flags, int) and bool(flags & WRITE_FLAGS)
        if writing and not contained(path):
            refuse(event, path)
    elif event in PATH_EVENTS:
        if not contained(args[0]):
            refuse(event, args[0])
    elif event in PAIR_EVENTS:
        for target in args[:2]:
            if not contained(target):
                refuse(event, target)


sys.addaudithook(hook)

sys.argv[0] = "ss"
from session_search import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
'''


def installed_python(executable: pathlib.Path) -> pathlib.Path:
    header = executable.read_bytes().split(b"\n", 1)[0]
    if not header.startswith(b"#!"):
        raise AssertionError(f"installed ss has no interpreter shebang: {executable}")
    # Keep the shebang path unresolved. Resolving it follows the virtual
    # environment symlink to the base interpreter and drops site-packages.
    interpreter = pathlib.Path(header[2:].strip().decode())
    if not interpreter.is_file():
        raise AssertionError(f"installed interpreter is missing: {interpreter}")
    return interpreter


def write_boundary_probe(
    executable: pathlib.Path,
    root: pathlib.Path,
    db: pathlib.Path,
    home: pathlib.Path,
    env: dict[str, str],
) -> None:
    """Prove the installed package writes only below the disposable root."""
    interpreter = installed_python(executable)
    probe = root / "write_boundary_probe.py"
    probe.write_text(PROBE_SOURCE, encoding="utf-8")
    probe_env = dict(env, SS_PROBE_ROOT=str(root), PYTHONDONTWRITEBYTECODE="1")

    # Negative controls. Each must trip, and each names the audit event it
    # must trip on. A control that only ever fires on os.mkdir would leave the
    # branches that matter unproven.
    sentinel = root / "probe-sentinel"
    sentinel.mkdir()
    # The product writes its selector state through tempfile.mkstemp, which
    # reaches the audit hook as an ``open`` event carrying flags, not a mode.
    # Point the data directory outside the boundary and pre-create it, so the
    # first uncontained operation is that write and nothing earlier.
    outside = root.parent / f"{root.name}-outside"
    (outside / "result-contexts").mkdir(parents=True, exist_ok=True)
    controls = (
        (
            ["--db", str(db), "--home", str(home), "index", "--reset"],
            dict(probe_env, SS_PROBE_ROOT=str(sentinel)),
            "os.mkdir",
        ),
        (
            [
                "--db", str(db), "--home", str(home), "--no-refresh",
                "where", "was", "the", "Braze", "claim", "verified",
            ],
            dict(probe_env, SS_DATA_DIR=str(outside), SS_PROBE_EVENTS="open"),
            "open",
        ),
    )
    try:
        for journey, control_env, expected_event in controls:
            control = subprocess.run(
                [str(interpreter), str(probe), *journey],
                cwd=root,
                env=control_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=TIMEOUT,
            )
            if control.returncode != 97 or "SS_WRITE_ESCAPE" not in control.stderr:
                raise AssertionError(
                    f"the write-boundary probe missed an escaping write "
                    f"(exit {control.returncode})\nstderr:\n{control.stderr}"
                )
            if f"SS_WRITE_ESCAPE {expected_event} " not in control.stderr:
                raise AssertionError(
                    f"the probe never exercised its {expected_event} branch\n"
                    f"stderr:\n{control.stderr}"
                )
    finally:
        shutil.rmtree(outside, ignore_errors=True)

    journeys = (
        ["--db", str(db), "--home", str(home), "index", "--reset"],
        ["--db", str(db), "--home", str(home), "--no-refresh", "where", "was", "the", "Braze", "claim", "verified"],
        ["--db", str(db), "look", "at", "1"],
        ["--db", str(db), "archive", "1"],
        ["--db", str(db), "unarchive", "1"],
    )
    for journey in journeys:
        completed = subprocess.run(
            [str(interpreter), str(probe), *journey],
            cwd=root,
            env=probe_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT,
        )
        if "SS_WRITE_ESCAPE" in completed.stderr or completed.returncode == 97:
            raise AssertionError(
                f"installed package wrote outside {root} during {journey!r}:\n{completed.stderr}"
            )
        if completed.returncode != 0:
            raise AssertionError(
                f"write-boundary probe {journey!r} exited {completed.returncode}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ss")
    parser.add_argument("--prepare-pilot", type=pathlib.Path)
    args = parser.parse_args()
    if args.prepare_pilot:
        target = args.prepare_pilot.expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        if any(target.iterdir()):
            raise SystemExit(f"Pilot directory must be empty: {target}")
        home = target / "home"
        build_home(home, int(time.time()))
        malformed = home / ".claude" / "projects" / "broken" / "bad.jsonl"
        malformed.parent.mkdir(parents=True)
        malformed.write_text("not-json\n", encoding="utf-8")
        print(f"Pilot fixture created: {target}")
        print(f"export HOME={home}")
        print(f"export SS_DATA_DIR={target / 'data'}")
        print("export SS_SUMMARIES=off")
        print("Run: ss index --reset")
        return 0
    if not args.ss:
        parser.error("--ss is required unless --prepare-pilot is used")
    missing = [name for name, module in (("pexpect", pexpect), ("wcwidth", wcswidth)) if module is None]
    if missing:
        raise SystemExit(
            f"The acceptance journeys need {' and '.join(missing)}. "
            f"Install with: pip install {' '.join(missing)}"
        )
    signal.signal(signal.SIGALRM, lambda *_args: (_ for _ in ()).throw(TimeoutError("task acceptance exceeded 90 seconds")))
    signal.alarm(90)
    executable = pathlib.Path(args.ss).resolve()
    if not executable.is_file():
        raise SystemExit(f"Installed ss executable not found: {executable}")

    with tempfile.TemporaryDirectory(prefix="ss-human-tasks-") as tmpdir:
        root = pathlib.Path(tmpdir).resolve()
        home = root / "home"
        data = root / "data"
        db = data / "sessions.sqlite"
        projects = build_home(home, int(time.time()))
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(home),
                "SS_DATA_DIR": str(data),
                "SS_SUMMARIES": "off",
                "PYTHONNOUSERSITE": "1",
                "COLUMNS": "80",
                "LINES": "30",
            }
        )
        env.pop("PYTHONPATH", None)
        env.pop("OPENROUTER_API_KEY", None)
        env.pop("SS_PROBE_EVENTS", None)
        common = ["--db", str(db), "--home", str(home), "--no-refresh"]

        indexed = run(executable, ["--db", str(db), "--home", str(home), "index", "--reset"], env)
        require(indexed.stdout, "Indexed", "index")
        status = run(executable, ["--db", str(db), "status"], env)
        counts = {
            match.group(1): int(match.group(2))
            for match in re.finditer(r"^(\w+): (\d+) newest=", status.stdout, re.MULTILINE)
        }
        for source in ("claude", "codex", "pi", "vscode", "cursor"):
            if counts.get(source, 0) < 1:
                raise AssertionError(f"{source} adapter indexed no documents:\n{status.stdout}")
        require(status.stdout, "semantic:", "semantic status line")

        for query, expected in (
            (("cross-tool", "Codex"), "codex resume codex-human-task"),
            (("cross-tool", "Pi"), "pi --session"),
        ):
            found = run(executable, [*common, *query], env)
            require(found.stdout, query[-1], f"{query[-1]} installed search")
            native = run(executable, ["--db", str(db), "open", "1"], env)
            require(native.stdout, expected, f"{query[-1]} native recovery")
        for query, source_label in (("VS Code chat", "VS Code"), ("Cursor chat", "Cursor")):
            found = run(executable, [*common, *query.split()], env)
            require(found.stdout, source_label, f"{source_label} installed search")

        # 1. Find remembered work from natural language.
        search = run(executable, [*common, "where", "was", "the", "Braze", "claim", "verified"], env)
        require(search.stdout, "Braze application", "remembered-work search")
        # Require the rank-2 separator. Without this guard a render change
        # that drops it turns "ranked first" into "appears anywhere".
        if "\n2. " not in search.stdout:
            raise AssertionError(
                f"the result list has no second rank, so first place is unverifiable:\n{search.stdout}"
            )
        first_result = search.stdout.split("\n2. ", 1)[0]
        if "Braze application" not in first_result:
            raise AssertionError(f"the intended resume session did not rank first:\n{search.stdout}")

        # 3. Inspect evidence and recover exact work from the saved search selector.
        detail = run(executable, ["--db", str(db), "look", "at", "1"], env)
        require(detail.stdout, "verified resume summary", "supporting evidence")
        opened = run(executable, ["--db", str(db), "open", "1"], env)
        require(opened.stdout, "claude --resume resume-target", "native recovery")
        require(opened.stdout, f"cd {projects['career']}", "native recovery directory")
        archived = run(executable, ["--db", str(db), "archive", "1"], env)
        require(archived.stdout, "Archived:", "archive transition")
        archived_view = run(
            executable,
            ["--db", str(db), "--home", str(home), "archived", "--no-refresh"],
            env,
        )
        require(archived_view.stdout, "Braze application", "archived recovery")
        unarchived = run(executable, ["--db", str(db), "unarchive", "1"], env)
        require(unarchived.stdout, "Unarchived:", "unarchive transition")
        require(unarchived.stdout, "Braze resume evidence repair", "unarchive names the session")
        restored = run(
            executable,
            ["--db", str(db), "--home", str(home), "archived", "--no-refresh"],
            env,
        )
        if "Braze resume evidence repair" in restored.stdout:
            raise AssertionError(f"unarchive left the session archived:\n{restored.stdout}")

        # 4. Understand the dashboard recovery card without conflating its fields.
        dashboard = run(executable, common, env)
        for label in ("About:", "State:", "Resume:"):
            require(dashboard.stdout, label, "recovery card")
        target_card = dashboard.stdout.split("Open: ss open 1", 1)[0]
        if "..." in target_card:
            raise AssertionError("the target recovery card truncated required information")
        about = card_field(target_card, "About", ("State", "Resume", "Clue", "Open"))
        state = card_field(target_card, "State", ("Resume", "Clue", "Open"))
        resume = card_field(target_card, "Resume", ("Clue", "Open"))
        if len({about, state, resume}) != 3 or any(not value for value in (about, state, resume)):
            raise AssertionError(f"card fields are empty or duplicated: {(about, state, resume)!r}")
        require(about, "change the resume", "About meaning")
        require(state, "Completed the verified resume summary", "State meaning")
        require(resume, "Braze application", "Resume meaning")

        # 2 and 5. Browse without terms, reject a mistake, select an older project, and return.
        transcript = pty_dashboard(
            executable,
            common,
            env,
            80,
            [
                ("p999", "Project selector not found: p999"),
                ("p2", "project"),
                ("n", "project"),
                ("b", "project"),
                ("b", "root"),
                ("q", pexpect.EOF),
            ],
        )
        require(transcript, "Older projects still saved:", "older-project discovery")
        require(transcript, "SS project · Shared / Osmo", "older-project selection")
        require(transcript, "201-205 shown of 205 threads", "older-project final page")
        require(transcript, "Project selector not found", "mistake recovery")

        # Ctrl-C must stop cleanly without a traceback.
        child = pexpect.spawn(str(executable), common, cwd=env["HOME"], env=env, encoding="utf-8", timeout=TIMEOUT)
        child.expect("Open N, choose project Pn, type search words, or q: ")
        child.sendcontrol("c")
        child.expect(pexpect.EOF)
        interrupted = child.before
        child.close()
        if "Traceback" in interrupted or child.exitstatus not in (0, None):
            raise AssertionError("Ctrl-C exposed a traceback")
        eof = pexpect.spawn(str(executable), common, cwd=env["HOME"], env=env, encoding="utf-8", timeout=TIMEOUT)
        eof.expect("Open N, choose project Pn, type search words, or q: ")
        eof.sendeof()
        eof.expect(pexpect.EOF)
        eof.close()
        if eof.exitstatus not in (0, None):
            raise AssertionError(f"direct EOF exited {eof.exitstatus}")

        # 6. Every visible output line must fit the terminal width.
        for width in WIDTHS:
            rendered = pty_dashboard(executable, common, env, width, [("p2", "project"), ("q", pexpect.EOF)])
            measured = [(visible_width(line), line) for line in rendered.splitlines()]
            unmeasurable = [line for size, line in measured if size < 0]
            if unmeasurable:
                raise AssertionError(
                    f"{width}-column output carried a control character: {unmeasurable[:2]!r}"
                )
            too_wide = [(size, line) for size, line in measured if size > width]
            if too_wide:
                raise AssertionError(f"{width}-column output overflowed: {too_wide[:3]}")
            leaked = sorted({ch for ch in rendered if ch in CONTROL_CHARS})
            if leaked:
                raise AssertionError(
                    f"{width}-column output retained control characters: "
                    f"{[hex(ord(ch)) for ch in leaked]}"
                )
            require(rendered, "About:", f"{width}-column card")
            require(rendered, "Resume:", f"{width}-column card")

        # 7. Diagnose malformed stores and missing selector state with recovery guidance.
        malformed = home / ".claude" / "projects" / "broken" / "bad.jsonl"
        malformed.parent.mkdir(parents=True)
        malformed.write_text("not-json\n", encoding="utf-8")
        doctor = run(
            executable,
            ["--db", str(db), "--home", str(home), "doctor", "--strict"],
            env,
            expected=1,
        )
        require(doctor.stdout, "malformed", "doctor malformed-store diagnosis")
        (data / "last-results.json").unlink(missing_ok=True)
        shutil.rmtree(data / "result-contexts", ignore_errors=True)
        missing = run(executable, ["--db", str(db), "open", "1"], env, expected=2)
        require(missing.stderr, "Run a fresh search first", "selector recovery")

        # 8. The installed tool must never write outside the directories it was given.
        write_boundary_probe(executable, root, db, home, env)

        # 9. An interrupted index leaves a damaged file. Explain it, then recover.
        db.write_bytes(b"SQLite format 3\x00interrupted-index" + b"\x00" * 512)
        damaged = run(executable, ["--db", str(db), "status"], env, expected=3)
        require(damaged.stderr, "SS storage unavailable", "damaged index diagnosis")
        rebuilt = run(executable, ["--db", str(db), "--home", str(home), "index", "--reset"], env)
        require(rebuilt.stdout, "Indexed", "damaged index recovery")
        # The damaged file must be kept for recovery, not silently deleted.
        require(rebuilt.stdout, "moved aside", "damaged index is quarantined, not destroyed")
        quarantined = db.with_name(db.name + ".damaged")
        if not quarantined.is_file():
            raise AssertionError(f"the damaged index was not preserved at {quarantined}")
        recovered = run(executable, [*common, "where", "was", "the", "Braze", "claim", "verified"], env)
        require(recovered.stdout, "Braze application", "search works after recovery")

        # 10. Read-only storage must explain itself instead of crashing.
        data.chmod(0o500)
        try:
            blocked = data / "writable-check"
            try:
                blocked.touch()
            except PermissionError:
                pass
            else:
                blocked.unlink()
                raise AssertionError(
                    "read-only storage acceptance needs a non-root user; "
                    "this process can still write to a read-only directory"
                )
            denied = run(
                executable,
                ["--db", str(db), "--home", str(home), "index", "--reset"],
                env,
                expected=3,
            )
            require(denied.stderr, "SS storage unavailable", "read-only storage diagnosis")
        finally:
            data.chmod(0o700)

    signal.alarm(0)
    print("Task usability acceptance passed: ten installed human journeys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
