import json
import os
import pathlib
import re
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from contextlib import redirect_stderr
from io import StringIO
from unittest import mock

import adapter_capabilities
import card_quality
import session_search as ss
import ss_config
import ss_dashboard


class PortableConfigurationTest(unittest.TestCase):
    def test_package_declares_ss_console_script_and_python_floor(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        with (root / "pyproject.toml").open("rb") as handle:
            payload = tomllib.load(handle)
        self.assertEqual(payload["project"]["scripts"]["ss"], "session_search:main")
        self.assertEqual(payload["project"]["requires-python"], ">=3.11")
        self.assertIn("wcwidth>=0.2,<1", payload["project"]["dependencies"])
        self.assertIn("evals", payload["tool"]["setuptools"]["packages"])
        self.assertIn("*.json", payload["tool"]["setuptools"]["package-data"]["evals"])

    def test_default_eval_corpus_is_public_and_sanitized(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        default = root / "evals" / "session-search-evals.json"
        payload = json.loads(default.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(payload["cases"]), 3)
        text = default.read_text(encoding="utf-8").lower()
        self.assertIn("starter cases", text)
        self.assertNotIn("/users/", text)

    def test_readme_documents_the_unpinned_design_partner_install(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        with (root / "pyproject.toml").open("rb") as handle:
            payload = tomllib.load(handle)
        readme = (root / "README.md").read_text(encoding="utf-8")
        repository = payload["project"]["urls"]["Repository"]
        install = (
            f'pipx install "{payload["project"]["name"]}[semantic] '
            f'@ git+{repository}.git"'
        )
        self.assertIn(install, readme)
        self.assertIn("not version-pinned", readme)
        self.assertIn("ss capabilities", readme)
        self.assertIn("ss demo", readme)

    def test_readme_hero_card_matches_the_terminal_renderer(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        rendered = "\n".join(
            ss_dashboard.dashboard_restart_card_lines(
                "5 · Codex · 3d ago",
                [
                    ("About:", "Update the robot battery pricing page."),
                    ("State:", "The pricing table is complete."),
                    ("Resume:", "Verify the mobile layout."),
                    ("Open:", "ss open 5"),
                ],
                64,
            )
        )
        self.assertIn(f"```text\n{rendered}\n```", readme)

    def test_every_facade_concern_module_ships_with_the_package(self):
        # Derived from the facade's own imports, not restated as a list. A
        # module missing from py-modules imports fine from a checkout and
        # fails only after a pipx install; missing from public-files.txt it
        # never reaches the public repository at all.
        root = pathlib.Path(__file__).resolve().parents[1]
        with (root / "pyproject.toml").open("rb") as handle:
            payload = tomllib.load(handle)
        modules = set(payload["tool"]["setuptools"]["py-modules"])
        manifest = (root / "public-files.txt").read_text(encoding="utf-8").splitlines()
        facade = (root / "session_search.py").read_text(encoding="utf-8")
        imported = set(re.findall(r"^from (ss_\w+) import", facade, flags=re.M))
        self.assertTrue(imported, "the facade imports no concern modules")
        for name in sorted(imported):
            self.assertIn(name, modules, f"{name} is missing from py-modules")
            self.assertIn(f"{name}.py", manifest, f"{name}.py is missing from public-files.txt")

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

    def test_linux_defaults_use_xdg_data_home(self):
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            ss_config.sys, "platform", "linux"
        ):
            home = pathlib.Path(tmpdir)
            paths = ss_config.resolve_paths(
                home=home, environ={"XDG_DATA_HOME": str(home / "xdg-data")}
            )
        self.assertEqual(paths.data_dir, home / "xdg-data" / "session-search")

    def test_malformed_config_has_a_bounded_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = pathlib.Path(tmpdir)
            config = home / ".config" / "session-search" / "config.toml"
            config.parent.mkdir(parents=True)
            config.write_text("[storage\ndata_dir = broken", encoding="utf-8")
            stderr = StringIO()
            with redirect_stderr(stderr):
                ss_config.resolve_paths(home=home, environ={})
        self.assertIn("ignored invalid config", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

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
        # Derived from the functions that implement reopen, not restated from
        # the data literal. Restating it let a Pi entry claim a capability the
        # code no longer had, with the suite still green.
        matrix = adapter_capabilities.capability_matrix()
        self.assertEqual(set(matrix), set(ss.SUPPORTED_SOURCES))
        for source, capability in matrix.items():
            self.assertEqual(
                capability.native_reopen,
                ss.native_resume_available(source, "abc123"),
                f"{source} declares native_reopen={capability.native_reopen} "
                "but the reopen path disagrees",
            )
            self.assertEqual(
                capability.native_reopen,
                ss.cross_tool_status(source) == "context packet only outside native owner",
                f"{source} cross-tool status contradicts its declared reopen support",
            )
        self.assertEqual(matrix["codex"].assistant_messages, "partial")

    def test_capabilities_render_without_private_paths(self):
        rendered = adapter_capabilities.render_capabilities()
        self.assertIn("Claude Code", rendered)
        self.assertIn("packet", rendered.lower())
        # Real leak shapes, not a sentinel string that appears nowhere else.
        for private in ("/Users/", "/home/", str(pathlib.Path.home()), "YN" + "G"):
            self.assertNotIn(private, rendered)

    def test_capabilities_stack_within_a_narrow_terminal(self):
        with mock.patch.object(
            adapter_capabilities.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((40, 24)),
        ):
            rendered = adapter_capabilities.render_capabilities()
        self.assertIn("Claude Code", rendered)
        self.assertIn("Native reopen", rendered)
        self.assertTrue(all(len(line) <= 40 for line in rendered.splitlines()))

    def test_cli_capabilities_command_uses_capability_matrix(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(ss.main(["capabilities"]), 0)
        printed = stdout.getvalue()
        # Every declared adapter must appear, so the command cannot pass by
        # printing one hardcoded label.
        for capability in adapter_capabilities.capability_matrix().values():
            self.assertIn(capability.label, printed)
        self.assertIn(adapter_capabilities.render_capabilities().strip(), printed)

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
