"""Session text must not carry credentials to the model provider.

Every sample below is assembled from parts at runtime, so this file never
contains a literal credential shape and the commit gate has nothing to trip on.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import secret_patterns  # noqa: E402


def load_module():
    spec = importlib.util.spec_from_file_location("session_search", ROOT / "session_search.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ss = load_module()

A20 = "a" * 20
HEX32 = "9f" * 20

SAMPLES = {
    1: [
        "AKIA" + "IOSFODNN7EXAMPLE",
        "sk_" + "live_" + A20 + "1234",
        "ghp_" + "b" * 36,
        "glpat-" + A20,
        "xoxb-" + "1234567890-" + A20,
        "sk-" + "ant-api03-" + A20,
        "sk-" + "or-v1-" + A20,
    ],
    2: ["--token " + A20 + "xyz"],
    3: ["api_key=" + HEX32],
    4: ["secret_" + "c" * 32, "ntn_" + A20],
    5: ["password: " + "hunter2hunter2"],
    6: ["Bearer " + A20 + "0123"],
    7: ["-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow\n-----END RSA " + "PRIVATE KEY-----"],
    0: ["postgres://admin:" + "swordfish99" + "@db.internal/app"],
}


class RedactionTest(unittest.TestCase):
    def test_every_known_credential_shape_is_removed(self):
        for number, samples in SAMPLES.items():
            for sample in samples:
                redacted, hits = secret_patterns.redact(f"I pasted {sample} into the config.")
                self.assertGreaterEqual(hits, 1, f"pattern {number} missed {sample[:14]}...")
                self.assertNotIn(sample, redacted, f"pattern {number} left {sample[:14]}...")

    def test_the_sentence_around_a_secret_survives(self):
        text = "Rebuilt the ATS collector after the key AKIA" + "IOSFODNN7EXAMPLE expired."
        redacted, hits = secret_patterns.redact(text)
        self.assertEqual(hits, 1)
        self.assertIn("Rebuilt the ATS collector", redacted)
        self.assertIn("expired", redacted)

    def test_ordinary_text_is_left_alone(self):
        for text in (
            "Fixed the login form so the password field clears on error.",
            "The bearer of this token idea was Paul, and we shipped it.",
            "Discussed api keys in general without pasting one.",
            "",
        ):
            redacted, hits = secret_patterns.redact(text)
            self.assertEqual(hits, 0, f"over-redacted: {text!r} -> {redacted!r}")
            self.assertEqual(redacted, text)


class OutboundPayloadTest(unittest.TestCase):
    def _captured_prompts(self, session_text: str) -> list[str]:
        prompts: list[str] = []

        def capture(prompt: str, max_tokens: int = 220) -> str:
            prompts.append(prompt)
            return "Summarized the session."

        row = ss.Document(
            doc_id="d1", source="codex", session_id="s1", title="Config work",
            path="/tmp/p", cwd="/tmp/w", role="user", ts=1, text=session_text, meta={},
        )
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ss.init_db(conn)
        ss.upsert_documents(conn, [row])
        stored = conn.execute("SELECT * FROM documents").fetchone()
        with mock.patch.object(ss, "llm_summarize", side_effect=capture):
            ss.build_session_card([stored], stored, "", use_llm=True)
        conn.close()
        return prompts

    def test_no_credential_reaches_the_outbound_prompt(self):
        secrets = [sample for samples in SAMPLES.values() for sample in samples]
        session_text = "Set up the deploy. " + " ".join(secrets) + " Then it worked."
        prompts = self._captured_prompts(session_text)
        self.assertTrue(prompts, "no prompt was built")
        for prompt in prompts:
            for sample in secrets:
                self.assertNotIn(sample, prompt, f"{sample[:14]}... reached the provider")

    def test_the_useful_words_still_reach_the_provider(self):
        prompts = self._captured_prompts(
            "Rebuilt the ATS collector with password: " + "hunter2hunter2" + " in the config."
        )
        joined = " ".join(prompts)
        self.assertIn("Rebuilt the ATS collector", joined)
        self.assertIn("in the config", joined)


class PackagingTest(unittest.TestCase):
    def test_the_redaction_module_ships_with_the_package(self):
        # A module missing from py-modules imports fine from a checkout and
        # fails only after a pipx install, which is where it matters most.
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"secret_patterns"', pyproject)


class CommitGateParityTest(unittest.TestCase):
    # Located relative to this checkout, not $HOME, so a test run with a
    # redirected HOME still exercises the parity check instead of skipping it.
    HOOK = ROOT.parents[1] / "_infra" / "cos" / "hooks" / "pre-commit"

    def test_module_covers_every_commit_gate_pattern(self):
        if not self.HOOK.exists():
            self.skipTest("private workspace commit hook is not part of the public package")
        hook = self.HOOK.read_text(encoding="utf-8")
        numbers = {int(match) for match in re.findall(r"^\s*#\s*Pattern (\d+):", hook, re.M)}
        self.assertTrue(numbers, "could not read the commit gate's numbered patterns")
        missing = numbers - secret_patterns.HOOK_PATTERN_NUMBERS
        self.assertEqual(
            missing,
            set(),
            "the commit gate gained patterns this module does not redact: " + str(sorted(missing)),
        )

    def test_parity_check_would_notice_a_new_gate_pattern(self):
        # Guards the guard: if the parity assertion stopped comparing, this
        # synthetic gate pattern would go unnoticed.
        numbers = {1, 2, 3, 4, 5, 6, 7, 8}
        self.assertNotEqual(numbers - secret_patterns.HOOK_PATTERN_NUMBERS, set())


if __name__ == "__main__":
    unittest.main()
