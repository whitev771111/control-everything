"""Desktop projections and atomic management writes with synthetic metadata only."""

import copy
import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import Mock, patch
from uuid import uuid4

from control_everything.profiles import profile_import
from control_everything.profiles.profile import Profile, ProfileError
from control_everything.sessions.codex_index import unavailable
from control_everything.workspace.project_progress import observe
from control_everything.workspace.store import WorkspaceError, WorkspaceStore
from control_everything.workspace.validation import validate_project
from tests.backend import test_profile_http as http_fixtures
from tests.backend.test_project_management import project_body


class CodexDesktopWorkspaceTests(unittest.TestCase):
    request = http_fixtures.ProfileHTTPTests.request

    def setUp(self):
        http_fixtures.ProfileHTTPTests.setUp(self)
        self.ids = ['cx-' + str(index) * 24 for index in (1, 2)]
        shared = str(self.root / 'shared')
        projects = [dict(id=identity, codex_project_id='project-' + str(index), name='Project ' + str(index),
                         directories=[dict(path=shared, role='main')], sort_order=index,
                         archived=False, read_only=True, binding_available=True)
                    for index, identity in enumerate(self.ids)]
        items = []
        for index in range(34):
            identity = str(uuid4())
            assignment = 'projectless' if index == 32 else 'unknown' if index == 33 else 'assigned'
            items.append(dict(thread_id=identity, session_key='codex:' + identity, title='Chat ' + str(index),
                              cwd=str(self.root / 'worktrees' / str(index)), created_at='2026-10-01T00:00:00Z',
                              updated_at='2026-10-02T00:00:00Z', archived=index == 31, source_kind='cli',
                              activity_state='unknown', task_completion='unknown',
                              project_id=self.ids[index % 2] if assignment == 'assigned' else None,
                              assignment_state=assignment, binding_available=assignment == 'assigned'))
        self.source = dict(available=True, reason=None, sampled_at='2026-10-18T00:00:00Z', source_id='source-a',
                           projects=projects, items=items, coverage={'threads': 34}, complete=True)
        self.reader = Mock(side_effect=lambda: copy.deepcopy(self.source))
        self.workspace.desktop_reader = self.reader
        self.workspace.session_reader = Mock(return_value=unavailable(0))
        self.profile.settings({'codex': {'enabled': True, 'db_path': str(self.root / 'synthetic.sqlite')}})

    def refresh(self):
        self.workspace._desktop_until = 0

    def create(self, index=28, project=None):
        session = self.source['items'][index]
        identity = project or session['project_id']
        if identity in self.ids:
            self.select(identity)
        return self.workspace.save_item(dict(title='Explicit item', provider_id='codex',
            project_id=identity, session_key=session['session_key']))['item']

    def select(self, identity):
        if any(row['id'] == identity for row in self.profile.store.list_projects()):
            return
        desktop = self.workspace.desktop_snapshot()
        projection = next(row for row in self.workspace._desktop_projects(desktop) if row['id'] == identity)
        if not projection['binding_available']:
            raise WorkspaceError('session_not_in_sample')
        self.profile.store.join_candidate(projection, expected_revision=self.profile.store.list_revision())

    def test_reads_do_not_materialize_and_source_summary_never_contains_full_items(self):
        before = self.profile.store.list_items()['list_revision']
        page = self.workspace.page_codex_sessions(limit=20)
        self.assertEqual((page['total'], len(page['items'])), (33, 20))
        second = self.workspace.page_codex_sessions(offset=20, limit=20)
        self.assertEqual(len(second['items']), 13)
        self.assertEqual(len({row['session_key'] for row in page['items'] + second['items']}), 33)
        snapshot = self.workspace.snapshot()
        self.assertEqual(len(snapshot['projects']['items']), 0)
        self.assertNotIn('items', snapshot['codex_desktop'])
        self.assertEqual(snapshot['sessions']['items'], [])
        self.assertEqual(self.profile.store.list_projects(), [])
        self.assertEqual(self.profile.store.list_items()['list_revision'], before)
        self.assertEqual(self.reader.call_count, 1)

    def test_explicit_same_root_identity_worktree_and_more_than_twelve_bind(self):
        first = self.create(28)
        second = self.create(29)
        self.assertEqual((first['project_id'], second['project_id']), tuple(self.ids))
        self.assertEqual(first['session_link_state'], 'verified')
        rows = self.profile.store.list_projects()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['root'], rows[1]['root'])
        self.assertEqual({row['source']['kind'] for row in rows}, {'codex'})
        self.assertEqual(self.workspace.page_items(scope='active')['total'], 2)
        self.assertEqual(self.workspace.page_items(scope='orphan')['total'], 0)
        self.assertEqual(self.workspace.page_codex_sessions(project_id=self.ids[0])['total'], 16)
        self.assertEqual(self.workspace.page_codex_sessions(project_id=self.ids[1], scope='archived')['total'], 1)
        with self.assertRaisesRegex(WorkspaceError, 'session_project_mismatch'):
            self.create(28, self.ids[1])

    def test_unknown_conflict_and_explicit_projectless_are_separate(self):
        self.select(self.ids[0])
        self.source['items'].append({**self.source['items'][33], 'session_key': 'codex:' + str(uuid4()),
                                     'assignment_state': 'conflict', 'project_id': self.ids[0]})
        self.refresh()
        self.assertEqual(self.workspace.page_codex_sessions(scope='projectless')['total'], 1)
        self.assertEqual(self.workspace.page_codex_sessions(scope='unknown')['total'], 2)
        self.assertEqual(self.workspace.page_codex_sessions(project_id=self.ids[0])['total'], 16)
        for index in (32, 33):
            with self.assertRaisesRegex(WorkspaceError, 'session_not_in_sample'):
                self.create(index, self.ids[0])

    def test_pagination_revision_tracks_membership_not_sampling_clock(self):
        first = self.workspace.page_codex_sessions()
        self.source['sampled_at'] = '2026-10-18T00:00:03Z'
        self.source['items'][0]['title'] = 'Renamed'
        self.source['items'][0]['updated_at'] = '2026-10-18T00:00:02Z'
        self.refresh()
        second = self.workspace.page_codex_sessions(offset=20)
        self.assertNotEqual(first['sampled_at'], second['sampled_at'])
        self.assertEqual(first['revision'], second['revision'])
        self.source['items'].append({**self.source['items'][0], 'session_key': 'codex:' + str(uuid4())})
        self.refresh()
        third = self.workspace.page_codex_sessions(offset=20)
        self.assertNotEqual(second['revision'], third['revision'])

    def test_external_binding_does_not_misassign_a_manual_project_at_same_path(self):
        manual = self.profile.store.save_project(project_body(self.root / 'shared'))
        self.source['items'][0]['cwd'] = str(self.root / 'shared')
        self.assertEqual(self.workspace.page_codex_sessions(project_id=manual['id'])['total'], 0)
        with self.assertRaisesRegex(WorkspaceError, 'session_project_mismatch'):
            self.create(0, manual['id'])
        external = self.create(0)
        self.assertEqual(external['project_id'], self.ids[0])
        with self.assertRaises(ProfileError):
            self.profile.store.save_project(project_body(self.root / 'shared', id=manual['id']))

    def test_manual_projects_accept_only_explicit_projectless_desktop_sessions(self):
        manual = self.profile.store.save_project(project_body(self.root / 'manual'))
        for index in (32, 33):
            self.source['items'][index]['cwd'] = str(self.root / 'manual' / 'child')
        conflict = {**self.source['items'][33], 'session_key': 'codex:' + str(uuid4()),
                    'assignment_state': 'conflict', 'project_id': self.ids[0]}
        self.source['items'].append(conflict)
        page = self.workspace.page_codex_sessions(project_id=manual['id'])
        self.assertEqual((page['total'], page['items'][0]['assignment_state'], page['items'][0]['binding_available']),
                         (1, 'projectless', True))
        item = self.create(32, manual['id'])
        self.assertEqual(item['session_link_state'], 'verified')
        for index in (33, 34):
            with self.assertRaisesRegex(WorkspaceError, 'session_not_in_sample'):
                self.create(index, manual['id'])
            legacy = {**item, 'session_key': self.source['items'][index]['session_key']}
            self.assertEqual(self.workspace.link_state(legacy, unavailable(0), self.workspace.registered_projects()),
                             'not_in_current_sample')

    def test_stale_disabled_path_switch_and_incomplete_forbid_new_binding(self):
        old = self.create()
        self.source.update(available=False, reason='source_unavailable', projects=[], items=[])
        self.refresh()
        stale = self.workspace.page_codex_sessions()
        self.assertEqual((stale['status'], stale['total']), ('stale', 33))
        self.assertTrue(all(not row['binding_available'] for row in stale['items']))
        manual = self.workspace.save_item({'title': 'Unlinked manual item', 'project_id': self.ids[0]})['item']
        self.assertIsNone(manual['session_key'])
        with self.assertRaisesRegex(WorkspaceError, 'session_not_in_sample'):
            self.workspace.save_item({'title': 'Cannot link stale session', 'project_id': old['project_id'],
                                      'provider_id': 'codex', 'session_key': old['session_key']})
        updated = self.workspace.save_item(dict(id=old['id'], expected_revision=1,
                                               changes={'state': 'blocked'}), update=True)['item']
        self.assertEqual(updated['session_key'], old['session_key'])
        self.profile.settings({'codex': {'enabled': False, 'db_path': str(self.root / 'synthetic.sqlite')}})
        calls = self.reader.call_count
        self.assertEqual(self.workspace.page_codex_sessions()['status'], 'not_configured')
        self.assertEqual(self.reader.call_count, calls)
        self.assertFalse(self.workspace.registered_projects()[0]['binding_available'])
        self.profile.settings({'codex': {'enabled': True, 'db_path': str(self.root / 'other.sqlite')}})
        self.assertEqual(self.workspace.page_codex_sessions()['status'], 'unavailable')
        self.assertEqual(self.workspace.page_codex_sessions()['items'], [])
        self.source.update(available=True, reason=None, projects=[], items=[], complete=False)
        self.refresh()
        self.assertFalse(self.workspace.page_codex_sessions()['complete'])

    def test_excluded_codex_history_retains_verified_identity_and_state_edit(self):
        item = self.create()
        selection = self.workspace.selection
        selection.change({'action': 'remove', 'project_id': item['project_id'], 'selection_token': selection.token()})
        self.assertEqual(self.workspace.registered_projects(), [])
        page = self.workspace.page_items()
        self.assertTrue(page['items'][0]['project_excluded'])
        self.assertEqual(page['items'][0]['session_link_state'], 'verified')
        updated = self.workspace.save_item({'id': item['id'], 'expected_revision': 1, 'changes': {'state': 'review'}}, update=True)['item']
        self.assertEqual(updated['session_key'], item['session_key'])
        self.assertEqual(updated['session_link_state'], 'verified')
        self.assertEqual(updated, self.workspace.page_items(include_assignment=True)['items'][0])
        for read in (self.workspace.snapshot, self.workspace.page_items):
            with patch.object(self.profile.store, 'list_projects', wraps=self.profile.store.list_projects) as registry:
                value = read()
            self.assertEqual(registry.call_count, 1)
            projected = value['items_page']['items'][0] if 'items_page' in value else value['items'][0]
            self.assertTrue(projected['project_excluded'])
            self.assertEqual(projected['session_link_state'], 'verified')

    def test_incomplete_projection_budget_and_failure_without_cache(self):
        self.source['complete'] = False
        page = self.workspace.page_codex_sessions()
        self.assertFalse(page['complete'])
        self.assertTrue(all(not row['binding_available'] for row in page['items']))
        with self.assertRaisesRegex(WorkspaceError, 'session_not_in_sample'):
            self.create()
        self.source['complete'] = True
        self.source['projects'][0]['name'] = 'x' * 81
        self.refresh()
        self.assertEqual(len(self.workspace.registered_projects()), 0)
        with self.assertRaisesRegex(WorkspaceError, 'session_not_in_sample'):
            self.create()

    def test_item_failure_preserves_explicitly_selected_project(self):
        self.select(self.ids[0])
        actual = self.profile.store._create_item
        with patch.object(self.profile.store, '_create_item', side_effect=WorkspaceError('text_budget')):
            with self.assertRaisesRegex(WorkspaceError, 'text_budget'):
                self.create()
        self.assertEqual(len(self.profile.store.list_projects()), 1)
        self.assertEqual(self.profile.store.list_items()['total'], 0)
        self.assertEqual(self.profile.store.list_items()['list_revision'], 2)
        with patch.object(self.profile.store, '_create_item', wraps=actual):
            self.create()
        self.assertEqual(len(self.profile.store.list_projects()), 1)

    def test_materialization_failure_and_untrusted_body_leave_no_rows(self):
        actual = self.profile.store._materialize_codex_project
        def fail_after_insert(connection, projection):
            actual(connection, projection)
            raise WorkspaceError('storage_error')
        with patch.object(self.profile.store, '_materialize_codex_project', side_effect=fail_after_insert):
            with self.assertRaisesRegex(WorkspaceError, 'storage_error'):
                self.select(self.ids[0])
        self.assertEqual(self.profile.store.list_projects(), [])
        self.assertEqual(self.profile.store.list_items()['total'], 0)
        with self.assertRaises(ProfileError):
            self.workspace.save_item({'title': 'Bad', 'project_id': self.ids[0],
                                      'codex_project': self.workspace._desktop_projects(self.workspace.desktop_snapshot())[0]})
        with self.assertRaises(ValueError):
            self.workspace.save_task(dict(title='Legacy', project_id=self.ids[0], provider_id=None,
                                          state='todo', session_key=None))
        self.assertEqual(self.profile.store.list_projects(), [])

    def test_configuration_changes_during_read_cannot_authorize_old_projection(self):
        def disable_during_read():
            self.profile.settings({'codex': {'enabled': False, 'db_path': str(self.root / 'synthetic.sqlite')}})
            return copy.deepcopy(self.source)
        self.workspace.desktop_reader = disable_during_read
        result = self.workspace.page_codex_sessions()
        self.assertEqual((result['status'], result['items']), ('not_configured', []))
        self.assertEqual(self.profile.store.list_projects(), [])

    def test_stale_cas_does_not_materialize_and_move_preserves_uuid(self):
        self.select(self.ids[0])
        old = self.workspace.save_item({'title': 'Global'})['item']
        actual = self.profile.store.save_item
        def race(fields, **kwargs):
            actual({'state': 'review'}, item_id=old['id'], expected_revision=1)
            return actual(fields, **kwargs)
        changes = dict(project_id=self.ids[0], provider_id='codex', session_key=self.source['items'][0]['session_key'])
        with patch.object(self.profile.store, 'save_item', side_effect=race):
            with self.assertRaisesRegex(WorkspaceError, 'item_changed'):
                self.workspace.save_item(dict(id=old['id'], expected_revision=1, changes=changes), update=True)
        self.assertEqual(len(self.profile.store.list_projects()), 1)
        moved = self.workspace.save_item(dict(id=old['id'], expected_revision=2, changes=changes), update=True)['item']
        self.assertEqual((moved['id'], moved['revision'], moved['state']), (old['id'], 3, 'review'))

    def test_materialized_projects_are_read_only_and_reserved_ids_cannot_be_forged(self):
        self.create()
        projection = self.profile.store.list_projects()[0]
        with self.assertRaises(ValueError):
            self.profile.store.save_todo({'text': 'Legacy bypass', 'project_id': projection['id']})
        self.assertEqual(observe(projection)['status'], 'not_configured')
        for source in ({'kind': 'none', 'directory_id': None, 'relative_path': None}, projection['source']):
            with self.assertRaises(ProfileError):
                self.profile.store.save_project(dict(id=projection['id'], name='Edited', archived=False,
                                                     directories=projection['directories'], source=source))
        with self.assertRaises(ProfileError):
            validate_project(project_body(self.root / 'x', id=self.ids[0]))
        # A non-reserved historical marker must still be protected by the transaction.
        manual = self.profile.store.save_project(project_body(self.root / 'legacy'))
        with self.profile.store.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute("UPDATE project_sources SET kind='codex' WHERE project_id=?", (manual['id'],))
        with self.assertRaises(ProfileError):
            self.profile.store.save_project(project_body(self.root / 'legacy', id=manual['id']))

    def test_backup_restore_retains_marker_and_rejects_malformed_marker(self):
        old = self.create()
        backup = profile_import.create_backup(self.profile)
        manifest = profile_import.validate_backup(backup)
        self.assertEqual((manifest['version'], manifest['schema_version']), (9, 9))
        restored_dir = self.root / 'restored'
        profile_import.restore_backup(str(backup), str(restored_dir))
        restored = Profile.open(str(restored_dir))
        self.assertEqual(restored.store.get_item(old['id'])['project_id'], self.ids[0])
        self.assertEqual(restored.store.list_projects()[0]['source']['kind'], 'codex')
        # Re-sign a malformed synthetic backup to exercise semantic, rather than checksum, validation.
        database = backup / 'management.sqlite3'
        with closing(sqlite3.connect(database)) as connection:
            connection.create_function('task_center_writer_version', 0, lambda: 9)
            connection.execute("UPDATE project_sources SET relative_path='unexpected'")
            connection.commit()
        manifest['files']['management.sqlite3'] = {'size': database.stat().st_size,
                                                  'sha256': hashlib.sha256(database.read_bytes()).hexdigest()}
        (backup / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(ProfileError):
            profile_import.validate_backup(backup)

    def test_http_params_pagination_and_write_security(self):
        status, page = self.request('/api/workspace/codex/sessions?limit=5&offset=20')
        self.assertEqual((status, page['total'], len(page['items']), page['offset']), (200, 33, 5, 20))
        for query in ('limit=0', 'limit=101', 'offset=-1', 'scope=all', 'limit=1&limit=2',
                      'unknown=x', 'project_id=Bad!', 'scope=unknown&project_id=' + self.ids[0]):
            self.assertEqual(self.request('/api/workspace/codex/sessions?' + query)[0], 422, query)
        body = {'title': 'Unsafe', 'project_id': self.ids[0]}
        for headers in ({'X-Session-CSRF': ''}, {'Origin': ''}, {'Host': 'bad'}):
            self.assertNotEqual(self.request('/api/workspace/items', 'POST', body, headers)[0], 200)
        self.assertEqual(self.profile.store.list_projects(), [])
        self.assertEqual(self.request('/api/workspace/items', 'POST', body)[0], 422)
        self.select(self.ids[0])
        self.assertEqual(self.request('/api/workspace/items', 'POST', body)[0], 200)

    def test_legacy_task_cannot_bind_an_external_assignment_to_a_manual_project(self):
        manual = self.profile.store.save_project(project_body(self.root / 'manual'))
        session = self.source['items'][0]
        session['cwd'] = str(self.root / 'manual' / 'child')
        self.workspace.session_reader = Mock(return_value={**unavailable(0), 'available': True,
            'items': [{'session_key': session['session_key'], 'cwd': session['cwd']}]})
        body = dict(title='Legacy bypass', project_id=manual['id'], provider_id='codex', state='todo',
                    session_key=session['session_key'])
        with self.assertRaisesRegex(WorkspaceError, 'session_project_mismatch'):
            self.workspace.save_task(body)
        self.assertEqual(self.profile.store.list_items()['total'], 0)
        old = self.workspace.save_task({**body, 'session_key': None})['task']
        with self.assertRaisesRegex(WorkspaceError, 'session_project_mismatch'):
            self.workspace.save_task({**body, 'id': old['id']}, update=True)
        self.assertIsNone(self.profile.store.get_task(old['id'])['session_key'])

    def test_legacy_task_rejects_unknown_conflict_partial_and_stale_sources(self):
        manual = self.profile.store.save_project(project_body(self.root / 'manual'))
        session = self.source['items'][0]
        session['cwd'] = str(self.root / 'manual' / 'child')
        self.workspace.session_reader = Mock(return_value={**unavailable(0), 'available': True,
            'items': [{'session_key': session['session_key'], 'cwd': session['cwd']}]})
        body = dict(title='Legacy bypass', project_id=manual['id'], provider_id='codex', state='todo',
                    session_key=session['session_key'])
        for state, complete, available in (('unknown', True, True), ('conflict', True, True),
                                            ('projectless', False, True), ('projectless', True, False)):
            with self.subTest(state=state, complete=complete, available=available):
                session.update(assignment_state=state, project_id=None, binding_available=False)
                self.source.update(complete=complete, available=available, reason=None if available else 'source_unavailable')
                self.refresh()
                with self.assertRaisesRegex(WorkspaceError, 'session_not_in_sample'):
                    self.workspace.save_task(body)
        self.assertEqual(self.profile.store.list_items()['total'], 0)

    def test_legacy_task_keeps_state_edit_when_original_association_is_unchanged(self):
        manual = self.profile.store.save_project(project_body(self.root / 'manual'))
        session = self.source['items'][32]
        session['cwd'] = str(self.root / 'manual' / 'child')
        self.workspace.session_reader = Mock(return_value={**unavailable(0), 'available': True,
            'items': [{'session_key': session['session_key'], 'cwd': session['cwd']}]})
        body = dict(title='Existing legacy', project_id=manual['id'], provider_id='codex', state='todo',
                    session_key=session['session_key'])
        task = self.workspace.save_task(body)['task']
        self.source.update(available=False, reason='source_unavailable')
        self.refresh()
        updated = self.workspace.save_task({**body, 'id': task['id'], 'state': 'blocked'}, update=True)['task']
        self.assertEqual((updated['id'], updated['state'], updated['session_key']),
                         (task['id'], 'blocked', task['session_key']))


if __name__ == '__main__':
    unittest.main()
