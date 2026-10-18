"""Selection lifecycle fixtures; no listeners or personal sources."""

import copy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from control_everything.profiles.profile import Profile, ProfileError
from control_everything.profiles import profile_import
from control_everything.workspace.project_selection import WorkspaceSelection
from control_everything.workspace.service import WorkspaceService


def candidate(identity='cl-' + '1' * 24, path='C:/example/shared'):
    return {'id': identity, 'name': 'Imported', 'archived': False,
            'directories': [{'id': 'main', 'path': path}],
            'source': {'kind': 'none', 'directory_id': None, 'relative_path': None}}


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = Profile.open(self.root / 'profile')
        self.selection = WorkspaceSelection(self.profile)
        self.row = candidate()
        self.reader = patch('control_everything.workspace.project_selection.project_sources.read_confirmed',
            side_effect=lambda *_: {'status': 'available', 'reason': None, 'complete': True,
                                  'source_id': 'fixture', 'projects': [copy.deepcopy(self.row)]})
        self.reader.start()
        self.addCleanup(self.reader.stop)

    def join(self):
        self.selection.invalidate()
        page = self.selection.candidates('claude')
        return self.selection.change({'action': 'join', 'provider': 'claude', 'candidate_id': self.row['id'],
            'snapshot_token': page['snapshot_token'], 'selection_token': self.selection.token()})

    def change(self, action, token=None):
        return self.selection.change({'action': action, 'project_id': self.row['id'],
                                      'selection_token': token or self.selection.token()})

    def test_read_only_default_and_four_join_states(self):
        before = (self.profile.config_path.read_bytes(), self.profile.store.list_revision())
        self.selection.overview()
        self.selection.candidates('claude')
        self.assertEqual(before, (self.profile.config_path.read_bytes(), self.profile.store.list_revision()))
        first = self.join()
        self.assertEqual((first['selected'], first['changed'], first['requires_rejoin']), (True, True, False))
        self.assertEqual(self.profile.config()['version'], 2)
        self.assertFalse(self.join()['changed'])
        revision = self.profile.store.list_revision()
        self.change('remove')
        self.assertEqual(self.profile.store.list_revision(), revision)
        self.assertEqual(WorkspaceService(profile=self.profile).registered_projects(), [])
        self.assertTrue(self.join()['selected'])
        self.assertEqual(self.profile.store.list_revision(), revision)
        self.assertEqual(len(Profile.open(self.profile.data_dir).store.list_projects()), 1)

    def test_dangling_exclusion_two_step_and_failed_rejoin_survives_restart(self):
        with self.profile.selection_guard():
            value = self.profile.workbench_config()
            value['excluded_project_ids'] = [self.row['id'], 'missing-id']
            self.profile.write_workbench_locked(self.profile.config(), value)
        original = self.profile.config_path.read_bytes()
        first = self.join()
        self.assertEqual((first['selected'], first['changed'], first['requires_rejoin']), (False, True, True))
        self.assertEqual(original, self.profile.config_path.read_bytes())
        with patch('control_everything.profiles.profile.os.replace', side_effect=OSError('fixture')):
            with self.assertRaises(ProfileError):
                self.change('rejoin', first['selection_token'])
        self.assertEqual(original, self.profile.config_path.read_bytes())
        self.profile = Profile.open(self.profile.data_dir)
        self.selection = WorkspaceSelection(self.profile)
        with patch('control_everything.sessions.project_sources.read_confirmed', side_effect=AssertionError('must not read')):
            second = self.change('rejoin')
        self.assertTrue(second['selected'])
        self.assertEqual(self.profile.workbench_config()['excluded_project_ids'], ['missing-id'])
        self.assertEqual(len(self.profile.store.list_projects()), 1)
        self.assertFalse(self.change('rejoin')['changed'])

    def test_cas_and_changed_snapshot_reject_without_writes(self):
        token = self.selection.token()
        self.join()
        with self.assertRaises(ProfileError) as caught:
            self.change('remove', token)
        self.assertEqual(caught.exception.status, 409)
        page = self.selection.candidates('claude')
        self.row['name'] = 'Changed'
        with self.assertRaises(ProfileError):
            self.selection.change({'action': 'join', 'provider': 'claude', 'candidate_id': self.row['id'],
                'snapshot_token': page['snapshot_token'], 'selection_token': self.selection.token()})

    def test_sql_insert_failure_rolls_back_all_rows(self):
        revision = self.profile.store.list_revision()
        with self.profile.store.connection() as db:
            db.execute("CREATE TEMP TRIGGER unused BEFORE INSERT ON projects BEGIN SELECT 1; END")
        with patch('control_everything.workspace.store.tasks.bump_list_revision', side_effect=RuntimeError('fixture')):
            with self.assertRaises(RuntimeError):
                self.join()
        self.assertEqual(self.profile.store.list_projects(), [])
        self.assertEqual(self.profile.store.list_revision(), revision)

    def test_imported_shared_directory_edit_archive_backup_lifecycle(self):
        self.join()
        identity = self.row['id']
        service = WorkspaceService(profile=self.profile)
        item = service.save_item({'title': 'Keep identity', 'project_id': identity})['item']
        self.row = candidate('oc-' + '2' * 24)
        self.join()
        first = next(row for row in self.profile.store.list_projects() if row['id'] == identity)
        body = {key: first[key] for key in ('id', 'name', 'archived', 'directories', 'source')}
        for kind, relative in [('none', None), ('text', 'progress.txt'), ('json', 'progress.json'), ('wfc', 'docs/current')]:
            body.update(name='Renamed', source={'kind': kind, 'directory_id': None if kind == 'none' else 'main', 'relative_path': relative})
            self.profile.store.save_project(body)
        for archived in (True, False):
            body['archived'] = archived
            self.profile.store.save_project(body)
        other = self.profile.store.save_project({**candidate('ignored', 'C:/example/other'), 'id': None})
        for archived in (False, True):
            bad = {**body, 'archived': archived, 'directories': [{'id': 'main', 'path': other['root']}]}
            with self.assertRaises(ProfileError):
                self.profile.store.save_project(bad)
        with self.assertRaises(ProfileError):
            self.profile.store.save_project({**body, 'directories': [body['directories'][0], {'id': 'd-' + '3' * 32, 'path': 'C:/example/shared'}]})
        body['directories'] = [{'id': 'main', 'path': 'C:/example/new'}]
        self.profile.store.save_project(body)
        self.change('remove')
        backup = profile_import.create_backup(self.profile)
        profile_import.validate_backup(backup)
        restored = Profile.open(profile_import.restore_backup(str(backup), str(self.root / 'restored')))
        row = next(row for row in restored.store.list_projects() if row['id'] == identity)
        self.assertEqual((row['name'], row['source'], row['directories']), (body['name'], body['source'], body['directories']))
        self.assertEqual(restored.store.get_item(item['id'])['project_id'], identity)
        self.assertEqual(restored.config()['version'], 4)

    def test_reserved_public_create_restore_and_ordinary_conflict_stay_rejected(self):
        for restore in (False, True):
            body = candidate()
            if restore:
                body['project_id'] = body.pop('id')
                body.pop('archived')
            with self.assertRaises(ProfileError):
                self.profile.store.save_project(body, restore=restore)
        self.join()
        with self.assertRaises(ProfileError):
            self.profile.store.save_project({**candidate(), 'id': None})

    def test_v4_writers_preserve_domains_and_restore_revokes_source_consent(self):
        self.profile.save_source({'expected_revision': 0, 'provider': 'claude', 'enabled': True, 'path': 'C:/example/claude.json'})
        self.profile.save_source({'expected_revision': 1, 'provider': 'opencode', 'enabled': True, 'path': str(self.root / 'opencode.db')})
        config = self.profile.config()
        self.profile.settings({'codex': {'enabled': True, 'db_path': str(self.root / 'codex.sqlite')}})
        self.assertEqual(self.profile.workbench_config()['revision'], 3)
        self.assertEqual(self.profile.workbench_config()['sources'], config['workbench']['sources'])
        backup = profile_import.create_backup(self.profile)
        restored = Profile.open(profile_import.restore_backup(str(backup), str(self.root / 'restored')))
        self.assertFalse(restored.workbench_config()['sources']['claude']['enabled'])
        self.assertFalse(restored.workbench_config()['sources']['opencode']['enabled'])
        self.assertFalse(restored.config()['codex']['enabled'])
        self.assertEqual(restored.workbench_config()['sources']['claude']['path'], 'C:/example/claude.json')

    def test_legacy_codex_source_change_between_sample_and_guard_rejects_join(self):
        for version in (2, 3):
            with self.subTest(version=version):
                profile = Profile.open(self.root / f'legacy-{version}')
                config = profile.config()
                config['codex'] = {'enabled': True, 'db_path': 'C:/example/old.sqlite'}
                if version == 3:
                    config.update(version=3, observations={'revision': 1, 'registrations': []})
                profile._write_config(config)
                selection = WorkspaceSelection(profile)
                self.row = candidate('cx-' + '4' * 24)
                self.row['source'] = {'kind': 'codex', 'directory_id': None, 'relative_path': None}
                page = selection.candidates('codex')
                guard = profile.selection_guard
                @contextmanager
                def switch():
                    profile.settings({'codex': {'enabled': False, 'db_path': 'C:/example/new.sqlite'}})
                    with guard():
                        yield
                with patch.object(profile, 'selection_guard', switch):
                    with self.assertRaises(ProfileError) as error:
                        selection.change({'action': 'join', 'provider': 'codex', 'candidate_id': self.row['id'],
                            'snapshot_token': page['snapshot_token'], 'selection_token': selection.token()})
                self.assertEqual(error.exception.reason, 'candidate_changed')
                self.assertEqual(profile.store.list_projects(), [])
                self.assertEqual(profile.workbench_config()['revision'], 0)

    def test_hidden_history_edits_and_v4_import_settings_preserve_intent(self):
        self.join()
        service = WorkspaceService(profile=self.profile)
        old = service.save_item({'title': 'Keep hidden history', 'project_id': self.row['id']})['item']
        self.change('remove')
        page = service.page_items()
        self.assertTrue(page['items'][0]['project_excluded'])
        updated = service.save_item({'id': old['id'], 'expected_revision': 1, 'changes': {'state': 'review'}}, update=True)['item']
        self.assertEqual(updated['project_id'], old['project_id'])
        with self.assertRaises(ProfileError):
            service.save_item({'title': 'No hidden new association', 'project_id': old['project_id']})
        before = self.profile.workbench_config()
        self.profile.import_settings({'version': 2, 'integrations': self.profile.integrations()})
        self.assertEqual(self.profile.workbench_config(), before)
        settings = self.profile.observation_settings()
        self.profile.save_observations({'expected_revision': settings['revision'], 'expected_hash': settings['hash'], 'registrations': []})
        self.assertEqual(self.profile.workbench_config(), before)

    def test_changing_imported_binding_identity_rechecks_shared_directory(self):
        self.row['directories'].append({'id': 'd-' + '2' * 32, 'path': 'C:/example/shared-extra'})
        self.join()
        first = copy.deepcopy(self.row)
        self.row = candidate('oc-' + '3' * 24, 'C:/example/shared-extra')
        self.join()
        first['name'] = 'Rename with unchanged extra'
        self.profile.store.save_project(first)
        first['directories'][1]['id'] = 'd-' + '4' * 32
        for archived in (False, True):
            first['archived'] = archived
            with self.assertRaises(ProfileError) as caught:
                self.profile.store.save_project(first)
            self.assertEqual(caught.exception.reason, 'directory_conflict')


if __name__ == '__main__':
    unittest.main()
