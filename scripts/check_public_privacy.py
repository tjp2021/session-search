#!/usr/bin/env python3
"""Fail when the public export contains private paths or private fixtures."""

from __future__ import annotations

import pathlib
import re
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


def main() -> int:
    failures: list[str] = []
    for path in public_paths():
        if not path.is_file():
            failures.append(f"missing manifest file: {path.relative_to(ROOT)}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append(f"non-text file is not allowed: {path.relative_to(ROOT)}")
            continue
        for label, pattern in {**BANNED, **SECRET_PATTERNS}.items():
            if pattern.search(text):
                failures.append(f"{path.relative_to(ROOT)}: {label}")
    if failures:
        print("Public privacy check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"PASS: {len(public_paths())} allowlisted public files are clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
