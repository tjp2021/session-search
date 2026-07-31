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

import requests

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
        "sk-" + "o" * 48,
        "rk_" + "live_" + "r" * 24,
        "ghp_" + "b" * 36,
        "ghs_" + "s" * 36,
        "ghu_" + "u" * 36,
        "ghr_" + "r" * 36,
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
    0: [
        "postgres://admin:" + "swordfish99" + "@db.internal/app",
        "postgres://admin:" + "p:a:ssword99" + "@db.internal/app",
    ],
}
SAMPLES[0].extend(
    [
        "github_" + "pat_" + "g" * 60,
        "xapp-" + "1-" + "A" * 20 + "-" + "2" * 20,
        "AIza" + "G" * 35,
        "hf_" + "h" * 40,
        "Api_Key=" + "MixedCaseValue1234567890",
        "aws_secret_access_key=" + "A" * 40,
        'password = "correct horse battery staple"',
    ]
)


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


class FakeResponse:
    def __init__(self, payload=None, *, http_error: Exception | None = None):
        self.payload = {"choices": [{"message": {"content": "Summarized the session."}}]} if payload is None else payload
        self.http_error = http_error

    def raise_for_status(self):
        if self.http_error:
            raise self.http_error

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FinalHttpBoundaryTest(unittest.TestCase):
    ENV = {
        "OPENROUTER_API_KEY": "test-provider-key",
        "SS_SUMMARIES": "openrouter",
    }

    def _call(self, prompt: str, *, response=None, side_effect=None):
        response = response or FakeResponse()
        with mock.patch.dict(ss.os.environ, self.ENV, clear=False):
            with mock.patch("requests.post", return_value=response, side_effect=side_effect) as post:
                result = ss.llm_summarize(prompt, max_tokens=77)
        return result, post

    def test_final_json_payload_redacts_every_known_credential_shape(self):
        secrets = [sample for samples in SAMPLES.values() for sample in samples]
        prompt = "Set up the deploy. " + " ".join(secrets) + " Then it worked."
        result, post = self._call(prompt)
        self.assertEqual(result, "Summarized the session.")
        post.assert_called_once()
        payload = post.call_args.kwargs["json"]
        outbound = payload["messages"][0]["content"]
        for sample in secrets:
            self.assertNotIn(sample, outbound, f"{sample[:14]}... reached the final HTTP body")
        self.assertEqual(payload["max_tokens"], 77)

    def test_useful_surrounding_text_survives_in_final_json_payload(self):
        result, post = self._call(
            "Rebuilt the ATS collector with password: " + "hunter2hunter2" + " in the config."
        )
        self.assertEqual(result, "Summarized the session.")
        outbound = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("Rebuilt the ATS collector", outbound)
        self.assertIn("in the config", outbound)
        self.assertIn("[redacted password]", outbound)

    def test_success_uses_provider_contract_and_returns_trimmed_content(self):
        response = FakeResponse({"choices": [{"message": {"content": "  Useful summary.  "}}]})
        result, post = self._call("ordinary local text", response=response)
        self.assertEqual(result, "Useful summary.")
        post.assert_called_once_with(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": "Bearer test-provider-key",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-4.1-nano",
                "messages": [{"role": "user", "content": "ordinary local text"}],
                "max_tokens": 77,
                "temperature": 0.2,
            },
            timeout=20,
        )

    def test_empty_choices_returns_empty_fallback(self):
        result, _post = self._call("prompt", response=FakeResponse({"choices": []}))
        self.assertEqual(result, "")

    def test_malformed_json_returns_empty_fallback(self):
        result, _post = self._call("prompt", response=FakeResponse(ValueError("bad JSON")))
        self.assertEqual(result, "")

    def test_http_error_returns_empty_fallback(self):
        response = FakeResponse(http_error=requests.HTTPError("503 Service Unavailable"))
        result, _post = self._call("prompt", response=response)
        self.assertEqual(result, "")

    def test_timeout_returns_empty_fallback(self):
        result, post = self._call("prompt", side_effect=requests.Timeout("timed out"))
        self.assertEqual(result, "")
        post.assert_called_once()


class LocalTextPreservationTest(unittest.TestCase):
    def test_card_builder_keeps_local_text_unchanged_before_network_boundary(self):
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        row = ss.Document(
            doc_id="d1", source="codex", session_id="s1", title="Config work",
            path="/tmp/p", cwd="/tmp/w", role="user", ts=1,
            text=f"Rebuilt the deploy with {secret}.", meta={},
        )
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ss.init_db(conn)
        ss.upsert_documents(conn, [row])
        stored = conn.execute("SELECT * FROM documents").fetchone()
        prompts: list[str] = []
        with mock.patch.object(
            ss,
            "llm_summarize",
            side_effect=lambda prompt, max_tokens=220: prompts.append(prompt) or "",
        ):
            ss.build_session_card([stored], stored, "", use_llm=True)
        self.assertTrue(prompts)
        self.assertTrue(all(secret in prompt for prompt in prompts))
        self.assertIn(secret, conn.execute("SELECT text FROM documents").fetchone()[0])
        conn.close()


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
