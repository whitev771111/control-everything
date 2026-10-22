"""Restored source paths retain intent while requiring renewed read permission."""
import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_everything.profiles.profile import Profile
from control_everything.profiles.profile_import import create_backup, restore_backup
from control_everything.profiles.workbench_settings import EMPTY
from tests.backend.test_dispatch import agent


class RestoreSourcePermissionTests(unittest.TestCase):
    def test_v4_and_agent_upgraded_v5_restore_disable_all_read_sources(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            profile = Profile.open(str(root / 'source'))
            config = profile.config()
            config.update(version=4, observations={'revision': 0, 'registrations': []},
                          workbench={**copy.deepcopy(EMPTY), 'revision': 1})
            config['codex'] = {'enabled': True, 'db_path': str(root / 'synthetic-codex.sqlite')}
            for provider, extension in (('claude', 'json'), ('opencode', 'sqlite')):
                config['workbench']['sources'][provider] = {
                    'enabled': True, 'path': str(root / ('synthetic-' + provider + '.' + extension))}
            profile._write_config(config)
            for version in (4, 5):
                with self.subTest(version=version):
                    if version == 5:
                        profile.save_agents({'expected_revision': 0, 'items': [agent()]})
                    original = profile.config()
                    self.assertEqual(original['version'], version)
                    backup = create_backup(profile)
                    restored = Profile.open(str(restore_backup(str(backup), str(root / f'restored-v{version}'))))
                    actual = restored.config()
                    self.assertFalse(actual['codex']['enabled'])
                    self.assertEqual(actual['codex']['db_path'], original['codex']['db_path'])
                    for provider in ('claude', 'opencode'):
                        self.assertEqual(actual['workbench']['sources'][provider],
                                         {**original['workbench']['sources'][provider], 'enabled': False})
                    self.assertEqual(actual['version'], version)
                    self.assertEqual(profile.config(), original)
