"""The suite polices its own execution.

Two test files once shipped without a `unittest.main()` block. Run directly they
imported, printed nothing, and exited 0, so 29 tests looked green while never
executing. These checks make that failure mode impossible to reintroduce.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

TESTS_DIR = pathlib.Path(__file__).resolve().parent


def test_modules() -> list[pathlib.Path]:
    return sorted(TESTS_DIR.glob("test_*.py"))


def defines_test_cases(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
            if name == "TestCase":
                return True
    return False


def calls_unittest_main(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "main":
            owner = getattr(func.value, "id", "")
            if owner == "unittest":
                return True
    return False


class SuiteIntegrityTest(unittest.TestCase):
    def test_every_test_module_executes_when_run_directly(self):
        missing = []
        for path in test_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if defines_test_cases(tree) and not calls_unittest_main(tree):
                missing.append(path.name)
        self.assertEqual(
            missing,
            [],
            "these files define tests but exit 0 without running them: " + ", ".join(missing),
        )

    def test_suite_discovery_sees_every_test_module(self):
        loader = unittest.TestLoader()
        discovered = {
            type(case).__module__
            for suite in loader.discover(str(TESTS_DIR))
            for group in suite
            for case in group
        }
        expected = {path.stem for path in test_modules()}
        self.assertEqual(expected - discovered, set())

    def test_this_module_is_itself_covered(self):
        tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
        self.assertTrue(defines_test_cases(tree))
        self.assertTrue(calls_unittest_main(tree))


if __name__ == "__main__":
    unittest.main()
