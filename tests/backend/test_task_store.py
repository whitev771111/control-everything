"""Unified task contracts over disposable profiles, without listeners."""

import copy
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from session_panel.profiles.profile import Profile, ProfileError
from session_panel.workspace import task_schema as schema
from session_panel.workspace.store import WorkspaceError, WorkspaceStore
from tests.backend.test_project_management import project_body


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = Profile.open(str(self.root / 'profile'))
        self.store = self.profile.store

    def create(self, **changes):
        return self.store.save_item(dict({'title': 'Fixture', 'project_id': None, 'provider_id': None,
                                         'state': 'todo', 'session_key': None}, **changes))

    def test_single_authority_raw_id_views_and_noop(self):
        row = self.create()
        legacy = self.store.get_task(row['origin_id'])
        self.assertEqual(legacy['title'], row['title'])
        self.assertNotEqual(legacy['id'], row['id'])
        updated = self.store.save_task({**{key: row[key] for key in ('title', 'project_id', 'provider_id', 'state', 'session_key')},
                                       'state': 'doing', 'id': row['origin_id']}, row['origin_id'])
        item = self.store.get_item(row['id'])
        self.assertEqual((updated['state'], item['state']), ('doing', 'doing'))
        before = copy.deepcopy(item)
        self.assertEqual(self.store.save_item({'title': item['title']}, item_id=item['id'], expected_revision=item['revision']), before)
        with self.assertRaisesRegex(WorkspaceError, 'item_changed'):
            self.store.save_item({'state': 'done'}, item_id=item['id'], expected_revision=row['revision'])

    def test_new_project_associations_recheck_activity_but_history_and_cas_keep_priority(self):
        project = self.store.save_project(project_body(self.root / 'active'))
        linked = self.create(project_id=project['id'])
        global_item = self.create()
        active_rebind = self.create()
        rebound = self.store.save_item({'project_id': project['id']}, item_id=active_rebind['id'], expected_revision=1)
        self.assertEqual((rebound['project_id'], rebound['revision']), (project['id'], 2))
        self.store.save_project(project_body(self.root / 'active', id=project['id'], archived=True))
        page = self.store.list_items()
        for item in (None, global_item):
            with self.subTest(create=item is None):
                with self.assertRaises(ProfileError) as error:
                    if item is None:
                        self.create(project_id=project['id'])
                    else:
                        self.store.save_item({'project_id': project['id']}, item_id=item['id'], expected_revision=item['revision'])
                self.assertEqual((error.exception.reason, error.exception.field), ('invalid_item', 'project_id'))
                self.assertEqual(self.store.list_items(), page)
        same = self.store.save_item({'project_id': project['id']}, item_id=linked['id'], expected_revision=linked['revision'])
        self.assertEqual(same, linked)
        changed = self.store.save_item({'state': 'review'}, item_id=linked['id'], expected_revision=linked['revision'])
        self.assertEqual(changed['project_id'], linked['project_id'])
        with self.assertRaisesRegex(WorkspaceError, 'item_changed'):
            self.store.save_item({'project_id': 'missing'}, item_id=linked['id'], expected_revision=linked['revision'])
        detached = self.store.save_item({'project_id': None}, item_id=changed['id'], expected_revision=changed['revision'])
        self.assertIsNone(detached['project_id'])
        for identity in ('', 'historical-orphan'):
            with self.subTest(identity=identity):
                with self.store.connection() as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    historical = self.store._create_item(connection, dict(title='Historical', project_id=identity,
                        provider_id=None, state='todo', session_key=None))
                self.assertEqual(self.store.save_item({'project_id': identity}, item_id=historical['id'], expected_revision=1), historical)
                saved = self.store.save_item({'state': 'doing'}, item_id=historical['id'], expected_revision=1)
                self.assertEqual((saved['project_id'], saved['state']), (identity, 'doing'))

    def test_five_states_two_toggles_restore_and_empty_origin_id(self):
        for state in schema.STATES:
            with self.subTest(state=state):
                raw = '' if state == 'todo' else state
                with self.store.connection() as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    item = self.store._create_item(connection, dict(title='Old', project_id='', provider_id=None,
                        state=state, session_key=None), origin='todo', raw_id=raw, source=None, todo_position=0)
                initial = self.store.get_item(item['id'])
                self.store.save_todo({'id': raw}, toggle=True)
                middle = self.store.get_item(item['id'])
                self.assertEqual(middle['state'], initial['resume_state'] if state == 'done' else 'done')
                self.store.save_todo({'id': raw}, toggle=True)
                self.assertEqual(self.store.get_item(item['id'])['state'], state)
        self.assertEqual(self.store.list_todos()['items'][0]['id'], '')
        with self.assertRaises(ProfileError) as error:
            self.store.save_todo({'id': 'missing'}, toggle=True)
        self.assertEqual((error.exception.reason, error.exception.status), ('todo_not_found', 404))

    def test_project_changes_invalidate_pagination_without_item_writes(self):
        a = self.store.save_project(project_body(self.root / 'a'))
        b = self.store.save_project(project_body(self.root / 'b'))
        for project in (a, b):
            for _ in range(3):
                self.create(project_id=project['id'])
        before = self.store.list_items(limit=100)['items']
        for archived in (True, False):
            page = self.store.list_items(scope='active', limit=2)
            self.store.save_project(project_body(self.root / 'a', id=a['id'], archived=archived))
            with self.assertRaisesRegex(WorkspaceError, 'list_changed'):
                self.store.list_items(scope='active', offset=2, limit=2, list_revision=page['list_revision'])
            refreshed = self.store.list_items(scope='active', limit=100)
            self.assertEqual(refreshed['total'], 3 if archived else 6)
            self.assertEqual(self.store.list_items(limit=100)['items'], before)
        with self.store.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self.store._create_item(connection, dict(title='Historical orphan', project_id='orphan', provider_id=None,
                state='todo', session_key=None))
        page = self.store.list_items(scope='orphan', limit=1)
        all_items = self.store.list_items(limit=100)['items']
        self.store.save_project(dict(project_id='orphan', name='Recovered',
            directories=[{'id': 'main', 'path': str(self.root / 'restored')}],
            source={'kind': 'none', 'directory_id': None, 'relative_path': None}), restore=True)
        with self.assertRaisesRegex(WorkspaceError, 'list_changed'):
            self.store.list_items(scope='orphan', offset=1, list_revision=page['list_revision'])
        self.assertEqual(self.store.list_items(scope='orphan')['total'], 0)
        self.assertEqual(self.store.list_items(scope='active')['total'], 7)
        self.assertEqual(self.store.list_items(limit=100)['items'], all_items)

    def test_project_revision_failure_rolls_back_all_project_writes(self):
        project = self.store.save_project(project_body(self.root / 'original'))
        page = self.store.list_items()
        with patch.object(schema, 'bump_list_revision', side_effect=WorkspaceError('revision_exhausted')):
            with self.assertRaisesRegex(WorkspaceError, 'revision_exhausted'):
                self.store.save_project(project_body(self.root / 'changed', id=project['id'], archived=True))
        self.assertEqual(self.store.list_projects()[0], project)
        self.assertEqual(self.store.list_items()['list_revision'], page['list_revision'])

    def test_each_connection_revalidates_objects_and_legacy_refuses(self):
        self.create()
        with self.assertRaisesRegex(WorkspaceError, 'schema_error'):
            with WorkspaceStore(self.store.path).connection():
                self.fail('legacy must refuse schema2 before DDL')
        connection = sqlite3.connect(self.store.path)
        try:
            connection.execute('DROP TRIGGER guard_projects_update')
            connection.commit()
        finally:
            connection.close()
        self.assertTrue(self.store._initialized)
        with self.assertRaisesRegex(WorkspaceError, 'schema_error'):
            self.store.list_projects()

    def test_all_known_tables_require_per_connection_writer_marker(self):
        connection = sqlite3.connect(self.store.path)
        try:
            for table in schema.SCHEMA2_TABLES:
                columns = [row[1] for row in connection.execute(f'PRAGMA table_info({table})')]
                for action in ('INSERT', 'UPDATE', 'DELETE'):
                    statement = (f'INSERT INTO {table} DEFAULT VALUES' if action == 'INSERT' else
                                 f'UPDATE {table} SET {columns[0]}={columns[0]}' if action == 'UPDATE' else
                                 f'DELETE FROM {table}')
                    # SQLite resolves trigger functions even for an empty matching set.
                    with self.subTest(table=table, action=action), self.assertRaises(sqlite3.OperationalError):
                        connection.execute(statement)
                    connection.rollback()
            for view in ('tasks', 'todos'):
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute(f'DELETE FROM {view}')
        finally:
            connection.close()

    def test_global_counts_and_stable_pages(self):
        self.create(project_id=None)
        self.create(project_id='')
        with self.store.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self.store._create_item(connection, dict(title='Historical orphan', project_id='orphan', provider_id=None,
                state='todo', session_key=None))
        self.assertEqual(self.store.list_items(scope='global')['total'], 2)
        self.assertEqual(self.store.list_items(scope='orphan')['total'], 1)
        page = self.store.list_items(limit=2)
        tail = self.store.list_items(offset=2, limit=2, list_revision=page['list_revision'])
        self.assertEqual(len({item['id'] for item in page['items'] + tail['items']}), 3)
        self.assertEqual(self.store.task_counts()['orphan']['total'], 1)
        self.assertNotIn(None, self.store.task_counts())
