import os
import pathlib
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

import adapter_capabilities
import card_quality
import session_search as ss
import ss_config


class PortableConfigurationTest(unittest.TestCase):
    def test_package_declares_ss_console_script_and_python_floor(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        with (root / "pyproject.toml").open("rb") as handle:
            payload = tomllib.load(handle)
        self.assertEqual(payload["project"]["scripts"]["ss"], "session_search:main")
        self.assertEqual(payload["project"]["requires-python"], ">=3.11")

    def test_default_paths_use_application_support(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = pathlib.Path(tmpdir)
            paths = ss_config.resolve_paths(home=home, environ={})
        self.assertEqual(
            paths.data_dir,
            home / "Library" / "Application Support" / "session-search",
        )
        self.assertEqual(paths.db, paths.data_dir / "session-search.sqlite")
        self.assertEqual(paths.model_cache, paths.data_dir / "models")

    def test_environment_data_directory_overrides_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir) / "private-ss"
            paths = ss_config.resolve_paths(
                home=pathlib.Path(tmpdir),
                environ={"SS_DATA_DIR": str(data_dir)},
            )
        self.assertEqual(paths.data_dir, data_dir)
        self.assertEqual(paths.last_results, data_dir / "last-results.json")

    def test_config_file_data_directory_is_supported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = pathlib.Path(tmpdir)
            config = home / ".config" / "session-search" / "config.toml"
            config.parent.mkdir(parents=True)
            configured = home / "ss-data"
            config.write_text(
                f'[storage]\ndata_dir = "{configured}"\n',
                encoding="utf-8",
            )
            paths = ss_config.resolve_paths(home=home, environ={})
        self.assertEqual(paths.data_dir, configured)

    def test_environment_wins_over_config_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = pathlib.Path(tmpdir)
            config = home / ".config" / "session-search" / "config.toml"
            config.parent.mkdir(parents=True)
            config.write_text(
                f'[storage]\ndata_dir = "{home / "config-data"}"\n',
                encoding="utf-8",
            )
            env_data = home / "env-data"
            paths = ss_config.resolve_paths(
                home=home,
                environ={"SS_DATA_DIR": str(env_data)},
            )
        self.assertEqual(paths.data_dir, env_data)


class AdapterCapabilityTest(unittest.TestCase):
    def test_capability_matrix_is_executable_truth(self):
        matrix = adapter_capabilities.capability_matrix()
        self.assertEqual(set(matrix), {"claude", "codex", "vscode", "cursor"})
        self.assertTrue(matrix["claude"].native_reopen)
        self.assertTrue(matrix["codex"].native_reopen)
        self.assertFalse(matrix["vscode"].native_reopen)
        self.assertFalse(matrix["cursor"].native_reopen)
        self.assertEqual(matrix["codex"].assistant_messages, "partial")

    def test_capabilities_render_without_private_paths(self):
        rendered = adapter_capabilities.render_capabilities()
        self.assertIn("Claude Code", rendered)
        self.assertIn("packet", rendered.lower())
        self.assertNotIn("/Users/", rendered)
        self.assertNotIn("private-workspace-marker", rendered)

    def test_cli_capabilities_command_uses_capability_matrix(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(ss.main(["capabilities"]), 0)
        self.assertIn("VS Code/Copilot", stdout.getvalue())

    def test_demo_is_isolated_from_native_archive_discovery(self):
        stdout = StringIO()
        with mock.patch.object(
            ss,
            "build_docs",
            side_effect=AssertionError("demo touched native discovery"),
        ):
            with redirect_stdout(stdout):
                self.assertEqual(ss.main(["demo"]), 0)
        rendered = stdout.getvalue()
        self.assertIn("120 synthetic records", rendered)
        self.assertIn("Archive lifecycle: archived=true, active=true", rendered)
        self.assertIn("temporary and has been removed", rendered)


class CardQualityTest(unittest.TestCase):
    def test_unmatched_quote_is_removed_without_losing_text(self):
        result = card_quality.clean_card_field('" we need to focus on this please')
        self.assertEqual(result, "We need to focus on this please.")

    def test_known_prompt_noise_and_midword_fragment_are_rejected(self):
        self.assertEqual(card_quality.clean_card_field("aftrer you are done."), "")
        self.assertEqual(card_quality.clean_card_field("Resume: implemen..."), "")

    def test_duplicate_fields_fall_back_instead_of_repeating(self):
        fields = card_quality.quality_gate(
            about="Repair session search.",
            state="Repair session search.",
            resume="Repair session search.",
        )
        self.assertEqual(fields.about, "Repair session search.")
        self.assertEqual(fields.state, card_quality.NO_CLEAR_STATE)
        self.assertEqual(fields.resume, card_quality.NO_CLEAR_RESUME)

    def test_contained_state_is_not_repeated_as_resume(self):
        fields = card_quality.quality_gate(
            about="Build x402 payment guard.",
            state="Implemented replay protection and receipt validation.",
            resume=(
                "Build x402 payment guard. Implemented replay protection "
                "and receipt validation."
            ),
        )
        self.assertEqual(fields.resume, card_quality.NO_CLEAR_RESUME)

    def test_card_fields_are_balanced_and_complete(self):
        result = card_quality.clean_card_field("fixed selector routing (with tests")
        self.assertEqual(result, "Fixed selector routing.")


if __name__ == "__main__":
    unittest.main()
