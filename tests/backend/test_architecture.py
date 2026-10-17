"""Ownership policy examples; imports are checked by the real Import Linter."""

import importlib.util
import ast
import sys
from pathlib import Path
from unittest import TestCase

spec = importlib.util.spec_from_file_location('check_architecture', Path(__file__).resolve().parents[2] / 'scripts/check_architecture.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class ArchitectureTests(TestCase):
    def test_shared_manifest_has_only_standard_library_imports(self):
        path = Path(__file__).resolve().parents[2] / 'src/session_panel/installation/manifest.py'
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import):
                names = [alias.name.split('.')[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                names = [node.module.split('.')[0]]
            else:
                continue
            self.assertTrue(set(names).issubset(sys.stdlib_module_names))

    def test_known_owners_allow_legitimate_cross_feature_changes(self):
        for path in ['src/session_panel/workspace/validation.py', 'src/session_panel/profiles/profile_import.py',
                     'src/session_panel/platform/windows_jobs.py', 'src/session_panel/input_validation.py',
                     'src/session_panel/platform/windows_installation.py', 'src/session_panel/installation/registry.py',
                     'src/session_panel/installation/maintenance.py', 'src/session_panel/installation/manifest.py',
                     'web/workspace/quota.js', 'tools/typescript/tsconfig.news.json',
                     'tools/packaging/build_assets.py', 'pyproject.toml', 'MANIFEST.in', 'setup.py',
                     'examples/legacy/progress-manual.example.json', 'tests/e2e/verify_architecture.py',
                     '.github/CODEOWNERS', 'rust-toolchain.toml']:
            with self.subTest(path=path):
                self.assertIsNone(policy.ownership_error(path))

    def test_new_business_files_cannot_accumulate_in_undeclared_locations(self):
        for path, reason in [('new_feature.py', 'root_file_has_no_owner'),
                             ('src/session_panel/new_feature.py', 'package_root_is_for_entries_and_foundations'),
                             ('src/session_panel/utils/feature.py', 'unknown_backend_owner'),
                             ('src/another_app/main.py', 'unknown_backend_owner'),
                             ('session_panel/workspace/service.py', 'unregistered_top_level_directory'),
                             ('tools/misc/check.py', 'unknown_development_tool'),
                             ('another_app/main.py', 'unregistered_top_level_directory'),
                             ('tests/random/test_feature.py', 'unknown_test_type')]:
            with self.subTest(path=path):
                self.assertEqual(policy.ownership_error(path), reason)

    def test_relative_paths_cannot_escape_or_change_windows_meaning(self):
        for path in ['../outside.py', '/outside.py', 'web/../bad.js', 'web//bad.js',
                     'C:/example/file.py', 'web\\workspace\\bad.js', 'web/workspace./bad.js', '', 1]:
            with self.subTest(path=path):
                self.assertEqual(policy.ownership_error(path), 'invalid_relative_path')
