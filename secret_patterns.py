"""Credential shapes redacted before session text leaves the machine.

These patterns mirror the ones the author's private pre-commit gate enforces, added
2026-03-04 after an audit found a deployment token, an API key, and a plaintext
password in committed files. They live here as well because SS installs
standalone through pipx and cannot import anything from that private workspace.
`tests/test_secret_redaction.py` fails if the two sets drift apart.

What this covers is credential *shapes*. It cannot recognize a person's name, a
client, or a business detail, and those still leave the machine. The only
complete mitigation is leaving OPENROUTER_API_KEY unset.
"""

from __future__ import annotations

import re

# Each entry is (hook_pattern_number, label, compiled regex, replacement).
# A replacement keeps any surrounding words so the redacted sentence still
# reads, because a summary built from mangled text is worse than no summary.
PATTERNS: tuple[tuple[int, str, re.Pattern[str], str], ...] = (
    (
        1,
        "api-key",
        re.compile(
            r"\b(?:sk_live_[a-zA-Z0-9]{20,}|sk_test_[a-zA-Z0-9]{20,}"
            r"|rk_live_[a-zA-Z0-9]{20,}|rk_test_[a-zA-Z0-9]{20,}"
            r"|pk_live_[a-zA-Z0-9]{20,}|sk-[a-zA-Z0-9]{40,}"
            r"|gh[pousr]_[a-zA-Z0-9_]{36,}"
            r"|glpat-[a-zA-Z0-9]{20,}|xoxb-[a-zA-Z0-9-]{20,}|xoxp-[a-zA-Z0-9-]{20,}"
            r"|github_pat_[a-zA-Z0-9_]{20,}|xapp-[a-zA-Z0-9-]{20,}"
            r"|AIza[a-zA-Z0-9_-]{35}|hf_[a-zA-Z0-9]{20,}"
            r"|sk-ant-[a-zA-Z0-9-]{20,}|sk-proj-[a-zA-Z0-9-]{20,}"
            r"|sk-or-v1-[a-zA-Z0-9]{20,}|AKIA[A-Z0-9]{16})"
        ),
        "[redacted api key]",
    ),
    (
        2,
        "cli-token",
        re.compile(r"(--token[ =])[a-zA-Z0-9_-]{20,}"),
        r"\1[redacted token]",
    ),
    (
        3,
        "hex-secret",
        re.compile(r"\b((?:api[_-]?key|apikey|key|token|secret)[\"' :=]+)[a-f0-9]{32,}", re.I),
        r"\1[redacted secret]",
    ),
    (
        4,
        "notion-key",
        re.compile(r"\b(?:secret_[a-zA-Z0-9]{32,}|ntn_[a-zA-Z0-9]{20,})"),
        "[redacted notion key]",
    ),
    (
        0,
        "quoted-password",
        re.compile(
            r"\b((?:password|passwd|pwd)\s*[:=]\s*)([\"'])([^\"'\r\n]{8,})([\"'])",
            re.I,
        ),
        r"\1\2[redacted password]\4",
    ),
    (
        5,
        "password",
        re.compile(r"\b((?:password|passwd|pwd)[\"' ]*[:=][\"' ]*)[^\s\"']{8,}", re.I),
        r"\1[redacted password]",
    ),
    (
        6,
        "bearer-token",
        re.compile(r"\b(Bearer )[a-zA-Z0-9_.\-]{20,}"),
        r"\1[redacted token]",
    ),
    (
        7,
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----.*?"
            r"(?:-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----)?",
            re.S,
        ),
        "[redacted private key]",
    ),
    # Beyond the commit gate: an adversarial review found a database URL with
    # inline credentials reaching the provider, which none of the above match.
    (
        0,
        "url-credentials",
        re.compile(r"\b([a-z][a-z0-9+.\-]*://[^\s:@/]+:)[^\s@/]+(@)"),
        r"\1[redacted password]\2",
    ),
    (
        0,
        "assigned-secret",
        re.compile(
            r"\b((?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|"
            r"client[_-]?secret|aws_secret_access_key)"
            r"[\"' ]*[:=][\"' ]*)[a-zA-Z0-9_./+=-]{16,}",
            re.I,
        ),
        r"\1[redacted secret]",
    ),
)

HOOK_PATTERN_NUMBERS = frozenset(number for number, _, _, _ in PATTERNS if number)


def redact(text: str) -> tuple[str, int]:
    """Return the text with credential shapes replaced, and how many were hit."""
    if not text:
        return "", 0
    total = 0
    for _number, _label, pattern, replacement in PATTERNS:
        text, hits = pattern.subn(replacement, text)
        total += hits
    return text, total
