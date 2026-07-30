#!/usr/bin/env python3
"""Fail when the public repository contains private paths or private fixtures.

Every git-tracked file is scanned. Scanning only the export manifest once let
two committed test files carry private fixture paths straight past this check,
so the manifest now only validates the export list itself.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "public-files.txt"
BANNED = {
    "author home path": re.compile(r"/Users/" + r"tim(?:/|\b)", re.I),
    "private workspace name": re.compile(r"\b" + "YN" + r"G\b"),
    "private clinic fixture": re.compile(
        "true" + r"health|true" + r"\s+health", re.I
    ),
    "private email fixture": re.compile("true" + r"healthchiros@", re.I),
    "private repository": re.compile("tjp2021/" + "yng", re.I),
    "author first name": re.compile(r"\b" + "Ti" + "m" + r"\b"),
    "author shell prompt": re.compile("ti" + "m@", re.I),
    "private clinic typo": re.compile("true" + "hel" + "th", re.I),
}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}


def public_paths() -> list[pathlib.Path]:
    entries = [
        line.strip()
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(entries) != len(set(entries)):
        raise ValueError("public-files.txt contains duplicate entries")
    return [ROOT / entry for entry in entries]


def in_public_repository() -> bool:
    """True when ROOT is the public repository rather than the private mirror.

    The private mirror keeps operator files that never ship, so only the
    export manifest is scanned there. In the public repository every tracked
    file ships, so every tracked file is scanned.
    """
    remotes = subprocess.run(
        ["git", "-C", str(ROOT), "remote", "-v"],
        capture_output=True, text=True, check=True,
    ).stdout
    return "session-search" in remotes


def tracked_paths() -> list[pathlib.Path]:
    if not in_public_repository():
        return [path for path in public_paths() if path.is_file()]
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [ROOT / entry for entry in listing.split("\0") if entry]


def main() -> int:
    failures: list[str] = []
    for path in public_paths():
        if not path.is_file():
            failures.append(f"missing manifest file: {path.relative_to(ROOT)}")
    scanned = 0
    for path in tracked_paths():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append(f"non-text file is not allowed: {path.relative_to(ROOT)}")
            continue
        scanned += 1
        for label, pattern in {**BANNED, **SECRET_PATTERNS}.items():
            if pattern.search(text):
                failures.append(f"{path.relative_to(ROOT)}: {label}")
    if failures:
        print("Public privacy check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"PASS: {scanned} tracked files are clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
