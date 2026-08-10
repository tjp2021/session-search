"""Every claim and command in README.md must match what the code really does.

This exists because two documented commands shipped broken and nothing caught
them. `pipx install session-search` named a package that is not on PyPI, and
eight developer commands invoked `.venv/bin/python` with no instruction to
create `.venv`. Reading the README never finds these. Executing it does.
"""

from __future__ import annotations

import json
import pathlib
import re
import unittest

import session_search as ss

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
CODE_BLOCK = re.compile(r"```(?:bash|shell)\n(.*?)```", re.DOTALL)
TABLE_COMMAND = re.compile(r"\|\s*`(ss|sessions) ([a-z-]+)")
REPO_PATH = re.compile(r"(?<![\w./])((?:tests|evals|docs|evidence|scripts)/[\w./-]+|[\w-]+\.py)")


def readme_text() -> str:
    return README.read_text(encoding="utf-8")


def command_lines() -> list[str]:
    lines: list[str] = []
    for block in CODE_BLOCK.findall(readme_text()):
        for raw in block.splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines


class ReadmeCommandTest(unittest.TestCase):
    def test_readme_exists(self):
        self.assertTrue(README.is_file(), f"README.md is missing at {README}")

    def test_every_documented_subcommand_routes_somewhere(self):
        """Each verb in the 'What to type' table must reach a real handler.

        Some reach argparse subcommands. Others, like `open N` and `look at N`,
        reach the natural-language follow-up parser. Both count. A verb that
        reaches neither is a documented command that cannot run.
        """
        parser = ss.build_parser()
        subcommands = set()
        for action in parser._subparsers._group_actions:  # noqa: SLF001
            subcommands.update(action.choices)
        unroutable = []
        for _, verb in TABLE_COMMAND.findall(readme_text()):
            if verb in subcommands or verb == "fresh":
                continue
            if ss.parse_natural_followup([verb, "1"]) is not None:
                continue
            unroutable.append(verb)
        self.assertFalse(
            unroutable, f"README documents commands that reach no handler: {sorted(unroutable)}"
        )

    def test_pipx_install_uses_the_repository_url(self):
        """The package is not published on PyPI, so a bare name cannot install."""
        for line in command_lines():
            if not line.startswith("pipx install"):
                continue
            self.assertIn(
                "git+https",
                line,
                f"pipx line cannot work, session-search is not on PyPI: {line}",
            )

    def test_venv_commands_document_how_to_create_the_venv(self):
        """A fresh clone has no .venv, so every .venv command needs a setup step."""
        text = readme_text()
        uses_venv = any(line.startswith(".venv/bin/") for line in command_lines())
        if not uses_venv:
            return
        creates_venv = re.search(r"(python3?\s+-m\s+venv|uv\s+venv)", text)
        self.assertTrue(
            creates_venv,
            "README runs .venv/bin/... but never shows how to create .venv",
        )

    def test_referenced_repository_paths_exist(self):
        """Every repo-relative path named in a command block must be present."""
        missing = []
        for line in command_lines():
            for candidate in REPO_PATH.findall(line):
                if candidate.startswith(".venv"):
                    continue
                if not (ROOT / candidate).exists():
                    missing.append((candidate, line))
        self.assertFalse(missing, f"README names paths that do not ship: {missing}")

    def test_default_eval_file_ships_with_the_public_repository(self):
        """The installed default must not point at a private-only fixture."""
        default = pathlib.Path(str(ss.DEFAULT_EVALS))
        shipped = ROOT / "evals" / default.name
        self.assertTrue(
            shipped.is_file(),
            f"default eval file does not ship: {shipped}",
        )
        self.assertIn("evals/session-search-evals.json", (ROOT / "public-files.txt").read_text())


class ReadmeNumberTest(unittest.TestCase):
    """The published benchmark table must equal the recorded evidence."""

    def setUp(self):
        self.text = readme_text()
        self.retrieval = json.loads(
            (ROOT / "evidence" / "public-retrieval-v0.1.0.json").read_text(encoding="utf-8")
        )

    def test_corpus_size_matches_evidence(self):
        corpus = self.retrieval["corpus"]
        self.assertIn(f"{corpus['sessions']} synthetic sessions", self.text)
        self.assertIn(f"{corpus['queries']} frozen", self.text)

    def test_retrieval_table_matches_evidence(self):
        rows = re.findall(
            r"\|\s*(Exact words \(FTS\)|Local fuzzy|Combined local search)\s*\|"
            r"\s*([\d.]+)%\s*\|\s*([\d.]+)%\s*\|\s*([\d.]+)\s*\|",
            self.text,
        )
        self.assertEqual(len(rows), 3, "the README retrieval table lost a row")
        modes = {"Exact words (FTS)": "fts", "Local fuzzy": "local", "Combined local search": "hybrid"}
        for label, top_one, recall, mrr in rows:
            actual = self.retrieval["modes"][modes[label]]
            self.assertAlmostEqual(float(top_one) / 100, actual["top_one"], places=4, msg=label)
            self.assertAlmostEqual(float(recall) / 100, actual["recall_at_five"], places=4, msg=label)
            self.assertAlmostEqual(float(mrr), actual["mrr_at_ten"], places=3, msg=label)

    def test_derived_counts_match_the_table(self):
        hybrid = self.retrieval["modes"]["hybrid"]
        queries = self.retrieval["corpus"]["queries"]
        self.assertIn(f"{round(hybrid['top_one'] * queries)} of {queries}", self.text)
        self.assertIn(f"{round(hybrid['recall_at_five'] * queries)} of {queries}", self.text)


class ReadmeConstantTest(unittest.TestCase):
    """Constants quoted in prose must equal the constants in code."""

    def setUp(self):
        self.text = readme_text()

    def test_embedding_model_matches(self):
        self.assertIn(ss.EMBED_MODEL, self.text)

    def test_backfill_and_page_limits_match(self):
        self.assertIn(f"only the {ss.CARD_BACKFILL_SESSIONS} newest sessions", self.text)
        self.assertIn(f"more than {ss.PROJECT_PAGE_MAX} sessions", self.text)

    def test_entry_point_matches_pyproject(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('ss = "session_search:main"', pyproject)
        self.assertIn("session_search:main", self.text)


if __name__ == "__main__":
    unittest.main()
