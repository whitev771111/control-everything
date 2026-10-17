"""Pure ownership and compatibility contracts for the shared module boundaries."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_everything import input_validation
from control_everything.execution import owned_process
from control_everything.platform import windows_jobs
from control_everything.profiles import observation_settings as legacy_settings
from control_everything.profiles import profile
from control_everything.sessions import observation_settings, observer_client
from control_everything.workspace import news_worker, service, store, validation


class InputCompatibilityTests(unittest.TestCase):
    def test_error_identity_fields_and_historical_import_are_preserved(self):
        self.assertIs(profile.ProfileError, input_validation.ProfileError)
        error = input_validation.ProfileError('invalid_config', 'observations', status=409, message='fixture')
        self.assertIsInstance(error, ValueError)
        self.assertEqual(error.args, ('invalid_config',))
        self.assertEqual((error.reason, error.field, error.status, error.message),
                         ('invalid_config', 'observations', 409, 'fixture'))
        with self.assertRaises(profile.ProfileError):
            observation_settings.invalid('observations', reason='duplicate_installation', status=409)

    def test_absolute_path_keeps_bounds_type_nul_and_compatibility_identity(self):
        self.assertIs(profile.absolute_path, input_validation.absolute_path)
        with TemporaryDirectory() as directory:
            self.assertTrue(input_validation.absolute_path(directory))
            anchor = Path(directory).anchor
            self.assertTrue(input_validation.absolute_path(anchor + 'x' * (2048 - len(anchor))))
            self.assertFalse(input_validation.absolute_path(anchor + 'x' * (2049 - len(anchor))))
            self.assertFalse(input_validation.absolute_path(Path(directory)))
        for value in ('', 'relative/path', 'C:relative', 'bad\0path', None, True):
            with self.subTest(value=value):
                self.assertFalse(input_validation.absolute_path(value))

    def test_observation_compatibility_exports_share_one_rule_set(self):
        for name in ('MAX_REVISION', 'MAX_BYTES', 'ROW_FIELDS', 'EMPTY', 'invalid', 'clean_text',
                     'canonical', 'validate', 'from_config', 'pair', 'restore_intent'):
            with self.subTest(name=name):
                self.assertIs(getattr(legacy_settings, name), getattr(observation_settings, name))
        self.assertIs(observer_client.settings, observation_settings)
        self.assertIs(profile.observations, observation_settings)
        self.assertEqual(observation_settings.pair(observation_settings.EMPTY),
                         legacy_settings.pair(legacy_settings.EMPTY))


class WorkspaceRuleCompatibilityTests(unittest.TestCase):
    def test_old_rule_imports_are_aliases_instead_of_copied_implementations(self):
        for name in ('finite_number', 'windows_path', 'inside_project', 'observation_owner',
                     'path_key', 'project_id_valid'):
            with self.subTest(name=name):
                self.assertIs(getattr(service, name), getattr(validation, name))
        for name in ('path_key', 'project_id_valid', 'validate_project', 'finite_number', 'observation_owner'):
            with self.subTest(name=name):
                self.assertIs(getattr(store, name), getattr(validation, name))
        self.assertIs(store.ProfileError, profile.ProfileError)
        self.assertIs(service.ProfileError, profile.ProfileError)

    def test_ownership_preserves_unc_longest_match_ties_and_archived_rules(self):
        projects = [
            {'id': 'root', 'directories': [{'path': r'\\example.invalid\share\project'}]},
            {'id': 'nested', 'directories': [{'path': r'\\example.invalid\share\project\nested'}]},
        ]
        cwd = r'\\?\UNC\example.invalid\share\project\nested\child'
        self.assertEqual(validation.observation_owner(cwd, projects), 'nested')
        self.assertIsNone(validation.observation_owner(cwd, projects + [
            {'id': 'duplicate', 'directories': projects[1]['directories']},
        ]))
        projects[1]['archived'] = True
        self.assertEqual(validation.observation_owner(cwd, projects), 'root')
        self.assertIsNone(validation.observation_owner('relative/path', projects))

    def test_validation_keeps_profile_error_contract_and_finite_number_rules(self):
        with self.assertRaises(profile.ProfileError) as caught:
            validation.validate_project({})
        self.assertEqual((caught.exception.reason, caught.exception.field, caught.exception.status),
                         ('invalid_project', 'project', 422))
        for value in (True, float('nan'), float('inf'), 10 ** 1000):
            self.assertFalse(validation.finite_number(value))
        self.assertTrue(validation.finite_number(0))
        self.assertTrue(validation.finite_number(1.5))


class WindowsLeafCompatibilityTests(unittest.TestCase):
    def test_both_consumers_share_leaf_objects_and_legacy_patch_targets(self):
        for name in ('ProcessError', 'WindowsAPI', 'ExtendedLimits', 'SecurityAttributes'):
            with self.subTest(name=name):
                self.assertIs(getattr(news_worker, name), getattr(windows_jobs, name))
                self.assertIs(getattr(owned_process, name), getattr(windows_jobs, name))
        for name in ('StartupInfo', 'StartupInfoEx', 'ProcessInformation', 'BasicLimits', 'IOCounters',
                     'JobAccounting', 'CREATE_SUSPENDED', 'CREATE_NO_WINDOW', 'EXTENDED_STARTUPINFO_PRESENT'):
            with self.subTest(name=name):
                self.assertIs(getattr(owned_process, name), getattr(windows_jobs, name))
        self.assertIs(owned_process.c, windows_jobs.c)
        self.assertIs(owned_process.sys, windows_jobs.sys)
        self.assertEqual(owned_process.MAX_LINE, 1024 * 1024)

    def test_direct_leaf_api_rejects_non_windows_without_loading_kernel(self):
        with patch.object(windows_jobs.sys, 'platform', 'linux'), \
                patch.object(windows_jobs.c, 'WinDLL', create=True) as loader:
            with self.assertRaisesRegex(owned_process.ProcessError, 'platform_unsupported'):
                windows_jobs.WindowsAPI()
            loader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
