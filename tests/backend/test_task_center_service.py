"""Task-center service contracts using temporary profiles and controlled sources."""
import copy
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from session_panel.profiles.profile import ProfileError
from session_panel.sessions.codex_index import unavailable
from session_panel.workspace.service import PROVIDERS
from session_panel.workspace.store import WorkspaceError, WorkspaceStore
from tests.backend import test_profile_http as fixtures
from tests.backend.test_project_management import project_body
from tests.backend.test_workspace import RECORD


class TaskCenterServiceTests(unittest.TestCase):
    def setUp(self):
        fixtures.ProfileHTTPTests.setUp(self)
        self.sample = {**unavailable(0), 'available': True, 'items': []}
        self.reader = Mock(side_effect=lambda: copy.deepcopy(self.sample))
        self.workspace.session_reader = self.reader

    def test_global_quick_create_and_provider_labels_are_management_only(self):
        for provider in (None, 'hermes', 'pi'):
            item = self.workspace.save_item({'title': '  Quick 😀  ', 'provider_id': provider})['item']
            self.assertEqual((item['title'], item['project_id'], item['state'], item['session_link_state']),
                             ('Quick 😀', None, 'todo', 'none'))
        self.assertEqual(self.workspace.page_items(scope='global')['total'], 3)
        for provider in ('hermes', 'pi'):
            self.assertNotIn(provider, PROVIDERS)
            with self.assertRaises(ValueError):
                self.workspace.save_record({**RECORD, 'provider_id': provider})
        self.assertFalse(self.server.capabilities['execution'])

    def test_partial_cas_noop_and_history_text_and_links_preserved(self):
        with self.profile.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = self.profile.store._create_item(conn, dict(title='  History\r\n😀' * 300,
                project_id='missing', provider_id='codex', state='review', session_key='codex:' + str(uuid4())),
                origin='todo', raw_id='', source=None)
        request = dict(id=old['id'], expected_revision=old['revision'], changes={'title': old['title']})
        same = self.workspace.save_item(request, update=True)['item']
        self.assertEqual({k: same[k] for k in old}, old)
        changed = self.workspace.save_item({**request, 'changes': {'state': 'blocked'}}, update=True)['item']
        self.assertEqual((changed['title'], changed['project_id'], changed['origin_id']), (old['title'], 'missing', ''))
        with self.assertRaisesRegex(WorkspaceError, 'item_changed'):
            self.workspace.save_item(request, update=True)
        for title in (old['title'] + 'x',):
            with self.assertRaises(ProfileError):
                self.workspace.save_item(dict(id=changed['id'], expected_revision=changed['revision'], changes={'title': title}), update=True)

    def test_new_links_require_current_facts_but_unchanged_archived_link_can_edit(self):
        project = self.profile.store.save_project(project_body(self.root / 'project'))
        key = 'codex:' + str(uuid4())
        self.sample['items'] = [{'session_key': key, 'cwd': str(self.root / 'project' / 'child')}]
        item = self.workspace.save_item(dict(title='Linked', project_id=project['id'], provider_id='codex', session_key=key))['item']
        self.assertEqual(item['session_link_state'], 'verified')
        self.profile.store.save_project(project_body(self.root / 'project', id=project['id'], archived=True))
        self.sample['items'] = []
        changed = self.workspace.save_item(dict(id=item['id'], expected_revision=1, changes={'state': 'done'}), update=True)['item']
        self.assertEqual((changed['project_id'], changed['session_key'], changed['session_link_state']),
                         (project['id'], key, 'not_in_current_sample'))
        with self.assertRaises(ProfileError):
            self.workspace.save_item(dict(title='New', project_id=project['id']))
        with self.assertRaises(ProfileError):
            self.workspace.save_item(dict(title='New', provider_id='pi', session_key=key))

    def test_new_session_association_rejects_unknown_and_other_project(self):
        project = self.profile.store.save_project(project_body(self.root / 'project'))
        body = dict(title='Linked', project_id=project['id'], provider_id='codex', session_key='codex:' + str(uuid4()))
        with self.assertRaisesRegex(WorkspaceError, 'session_not_in_sample'):
            self.workspace.save_item(body)
        self.sample['items'] = [{'session_key': body['session_key'], 'cwd': str(self.root / 'other')}]
        with self.assertRaisesRegex(WorkspaceError, 'session_project_mismatch'):
            self.workspace.save_item(body)
        self.assertEqual(self.workspace.page_items()['total'], 0)

    def test_cas_rechecks_after_validation_and_independent_read_sees_next_write(self):
        first = self.workspace.save_item({'title': 'Before'})['item']
        actual = self.profile.store.save_item
        def racing_write(fields, **kwargs):
            actual({'state': 'review'}, item_id=first['id'], expected_revision=first['revision'])
            return actual(fields, **kwargs)
        with patch.object(self.profile.store, 'save_item', side_effect=racing_write):
            with self.assertRaisesRegex(WorkspaceError, 'item_changed'):
                self.workspace.save_item(dict(id=first['id'], expected_revision=1, changes={'title': 'Stale'}), update=True)
        current = self.workspace.page_items()['items'][0]
        self.assertEqual((current['title'], current['state'], first['state']), ('Before', 'review', 'todo'))

    def test_archive_between_validation_and_transaction_rejects_create_and_rebind(self):
        other = WorkspaceStore(self.profile.store.path, profile=True)
        for mode in ('create', 'rebind'):
            with self.subTest(mode=mode):
                root = self.root / mode
                project = self.profile.store.save_project(project_body(root))
                old = self.workspace.save_item({'title': 'Existing global'})['item'] if mode == 'rebind' else None
                after_archive = []
                actual = self.profile.store.save_item
                def archive_before_write(fields, **kwargs):
                    other.save_project(project_body(root, id=project['id'], archived=True))
                    after_archive.append(other.list_items())
                    return actual(fields, **kwargs)
                body = (dict(id=old['id'], expected_revision=old['revision'], changes={'project_id': project['id']})
                        if old else dict(title='New linked', project_id=project['id']))
                with patch.object(self.profile.store, 'save_item', side_effect=archive_before_write):
                    with self.assertRaises(ProfileError) as error:
                        self.workspace.save_item(body, update=old is not None)
                self.assertEqual((error.exception.reason, error.exception.field), ('invalid_item', 'project_id'))
                self.assertEqual(other.list_items(), after_archive[0])
                self.assertTrue(next(row for row in other.list_projects() if row['id'] == project['id'])['archived'])

    def test_session_owner_changes_before_transaction_reject_new_associations(self):
        other = WorkspaceStore(self.profile.store.path, profile=True)
        for mode in ('create', 'rebind'):
            for change in ('move', 'longer_owner'):
                with self.subTest(mode=mode, change=change):
                    root = self.root / (mode + '-' + change)
                    project = self.profile.store.save_project(project_body(root))
                    key = 'codex:' + str(uuid4())
                    self.sample['items'] = [{'session_key': key, 'cwd': str(root / 'child')}]
                    old = self.workspace.save_item({'title': 'Existing global'})['item'] if mode == 'rebind' else None
                    fields = dict(project_id=project['id'], provider_id='codex', session_key=key)
                    body = (dict(id=old['id'], expected_revision=old['revision'], changes=fields)
                            if old else dict(title='Linked', **fields))
                    after_project = []
                    actual = self.profile.store.save_item
                    def change_before_write(fields, **kwargs):
                        if change == 'move':
                            other.save_project(project_body(root / 'moved', id=project['id']))
                        else:
                            other.save_project(project_body(root / 'child'))
                        after_project.append(other.list_items())
                        return actual(fields, **kwargs)
                    with patch.object(self.profile.store, 'save_item', side_effect=change_before_write):
                        with self.assertRaisesRegex(WorkspaceError, 'session_project_mismatch'):
                            self.workspace.save_item(body, update=old is not None)
                    self.assertEqual(other.list_items(), after_project[0])

    def test_snapshot_reuses_one_session_and_registry_sample_for_new_items(self):
        self.workspace.save_item({'title': 'Initial'})
        self.reader.reset_mock()
        with patch.object(self.workspace, 'registered_projects', wraps=self.workspace.registered_projects) as registry:
            result = self.workspace.snapshot()
        self.assertEqual((self.reader.call_count, registry.call_count), (1, 1))
        self.assertEqual(result['items'], result['items_page']['items'])
        self.assertEqual(result['items_page']['total'], 1)
        self.assertEqual(result['task_providers'], ['codex', 'claude', 'grok', 'dsh', 'opencode', 'hermes', 'pi'])
        self.reader.reset_mock()
        self.workspace.page_items()
        self.assertEqual(self.reader.call_count, 1)

    def test_legacy_sources_share_authority_without_changing_raw_id_envelopes(self):
        project = self.profile.store.save_project(project_body(self.root / 'project'))
        fields = dict(title='Legacy', project_id=project['id'], provider_id='hermes', state='todo', session_key=None)
        raw = self.workspace.save_task(fields)['task']
        todo = self.profile.store.save_todo({'text': 'Old todo', 'project_id': ''})['todos'][0]
        page = self.workspace.page_items()
        self.assertEqual(page['total'], 2)
        self.assertEqual({(row['origin'], row['origin_id']) for row in page['items']}, {('task', raw['id']), ('todo', todo['id'])})
        self.assertEqual(self.workspace.page_tasks()['items'][0]['id'], raw['id'])
        new = next(row for row in page['items'] if row['origin'] == 'task')
        self.workspace.save_item(dict(id=new['id'], expected_revision=new['revision'], changes={'state': 'review'}), update=True)
        self.assertEqual(self.profile.store.get_task(raw['id'])['state'], 'review')

    def test_project_only_changes_invalidate_page_without_item_changes(self):
        project = self.profile.store.save_project(project_body(self.root / 'project'))
        for _ in range(3):
            self.workspace.save_item({'title': 'Row', 'project_id': project['id']})
        for archived in (True, False):
            before = self.workspace.page_items(scope='active', limit=1)
            rows = self.profile.store.list_items()['items']
            self.profile.store.save_project(project_body(self.root / 'project', id=project['id'], archived=archived))
            with self.assertRaisesRegex(WorkspaceError, 'list_changed'):
                self.workspace.page_items(scope='active', offset=1, list_revision=before['list_revision'])
            self.assertEqual(self.workspace.page_items(scope='active')['total'], 0 if archived else 3)
            self.assertEqual(self.profile.store.list_items()['items'], rows)

    def test_new_validation_refuses_unknown_types_and_preserves_drafts_server_side(self):
        for body in ({}, {'title': ' '}, {'title': 'x' * 2001}, {'title': []}, {'title': '\ud800'},
                     {'title': 'X', 'source': 'auto'}, {'title': 'X', 'project_id': ''}, {'title': 'X', 'provider_id': []}):
            with self.subTest(body=repr(body)), self.assertRaises(ProfileError):
                self.workspace.save_item(body)
        item = self.workspace.save_item({'title': '中' * 2000})['item']
        for changes in ({}, {'origin': 'todo'}, {'state': []}, {'title': 'x' * 2001}):
            with self.assertRaises(ProfileError):
                self.workspace.save_item(dict(id=item['id'], expected_revision=1, changes=changes), update=True)
        self.assertEqual(self.profile.store.get_item(item['id'])['title'], '中' * 2000)

    def test_all_items_and_project_counts_include_todos_over_page_limit(self):
        project = self.profile.store.save_project(project_body(self.root / 'project'))
        with self.profile.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for index in range(103):
                self.profile.store._create_item(conn, dict(title=str(index), project_id=project['id'],
                    provider_id=None, state='done' if index % 2 else 'todo', session_key=None),
                    origin='todo' if index % 2 else 'task', raw_id=str(index))
        page = self.workspace.page_items(limit=100)
        tail = self.workspace.page_items(offset=100, limit=100, list_revision=page['list_revision'])
        self.assertEqual((page['total'], len(page['items']), len(tail['items'])), (103, 100, 3))
        self.assertEqual(self.workspace.profile_projects()[0]['task_counts']['total'], 103)
