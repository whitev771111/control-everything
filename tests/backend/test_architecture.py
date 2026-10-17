"""Ownership policy examples; imports are checked by the real Import Linter."""

import importlib.util
from pathlib import Path
from unittest import TestCase

spec = importlib.util.spec_from_file_location('check_architecture', Path(__file__).resolve().parents[2] / 'scripts/check_architecture.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class ArchitectureTests(TestCase):
    def test_known_owners_allow_legitimate_cross_feature_changes(self):
        for path in ['session_panel/workspace/validation.py', 'session_panel/profiles/profile_import.py',
                     'session_panel/platform/windows_jobs.py', 'session_panel/input_validation.py',
                     'web/workspace/quota.js', 'tools/typescript/tsconfig.news.json',
                     'examples/legacy/progress-manual.example.json', 'tests/e2e/verify_architecture.py',
                     '.github/CODEOWNERS', 'rust-toolchain.toml']:
            with self.subTest(path=path):
                self.assertIsNone(policy.ownership_error(path))

    def test_new_business_files_cannot_accumulate_in_undeclared_locations(self):
        for path, reason in [('new_feature.py', 'root_file_has_no_owner'),
                             ('session_panel/new_feature.py', 'package_root_is_for_entries_and_foundations'),
                             ('session_panel/utils/feature.py', 'unknown_backend_owner'),
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
