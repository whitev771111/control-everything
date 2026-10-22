"""Compatibility writes must preserve automatic execution and human review."""
import unittest
from uuid import uuid4

from control_everything.input_validation import ProfileError
from control_everything.workspace import agent_runs, management
from tests.backend import test_dispatch_execution_context as fixtures
from tests.backend import test_profile_http as http_fixtures


class ExecutionItemCompatibilityTests(unittest.TestCase):
    save_agent = fixtures.DispatchExecutionContextTests.save_agent
    request = http_fixtures.ProfileHTTPTests.request

    def setUp(self):
        fixtures.DispatchExecutionContextTests.setUp(self)
        self.store = self.profile.store
        self.execution_id = str(uuid4())
        item = self.store.get_item(self.item['id'])
        with self.store.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                'INSERT INTO execution_runs(id,request_id,request_hash,owner_id,task_id,task_title,'
                'project_id,project_name,cwd,root_identity,mode,prompt,state,cleanup_state,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (self.execution_id, self.dispatch['id'], 'synthetic-hash', 'synthetic-owner',
                 item['id'], item['title'], self.project['id'], self.project['name'],
                 self.dispatch['directory_path'], '{}', 'workspace-write', 'Synthetic prompt',
                 'running', 'open', '2026-10-22T00:00:00Z', '2026-10-22T00:00:00Z'))
            claimed = agent_runs.claim_dispatch_in_transaction(
                self.store, connection, dispatch_id=self.dispatch['id'], task_id=item['id'],
                agent_id=self.agent['id'], execution_run_id=self.execution_id,
                expected_revision=item['revision'])
        self.agent_run_id = claimed['agent_run_id']

    def legacy_body(self, **changes):
        item = self.store.get_item(self.item['id'])
        return {**{key: item[key] for key in ('title', 'project_id', 'provider_id', 'state', 'session_key')},
                'id': item['origin_id'], **changes}

    def dump(self):
        with self.store.connection() as connection:
            return list(connection.iterdump())

    def test_legacy_task_store_rejects_state_and_execution_binding_changes(self):
        before = self.dump()
        for changes in ({'state': 'done'}, {'project_id': None}, {'provider_id': 'codex'},
                        {'session_key': 'codex:' + str(uuid4())}):
            with self.subTest(changes=changes):
                with self.assertRaises(ProfileError) as caught:
                    body = self.legacy_body(**changes)
                    self.store.save_task(body, body['id'])
                self.assertEqual(caught.exception.reason, 'execution_managed_item')
                self.assertEqual(self.dump(), before)

    def test_new_and_legacy_task_http_writes_both_reject_early_completion(self):
        item = self.store.get_item(self.item['id'])
        before = self.dump()
        requests = [('/api/workspace/items/update', {
            'id': item['id'], 'expected_revision': item['revision'], 'changes': {'state': 'done'}}),
            ('/api/workspace/tasks/update', self.legacy_body(state='done'))]
        for route, body in requests:
            with self.subTest(route=route):
                status, result = self.request(route, 'POST', body)
                self.assertEqual((status, result.get('reason')), (409, 'execution_managed_item'))
                self.assertEqual(self.dump(), before)

    def test_legacy_todo_toggle_cannot_complete_an_executing_item(self):
        # A historical todo can be dispatched through the unified task center.
        with self.store.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute("UPDATE task_items SET origin='todo',origin_id='syntodo' WHERE id=?",
                               (self.item['id'],))
        before = self.dump()
        status, result = self.request('/api/todos/toggle', 'POST', {'id': 'syntodo'})
        self.assertEqual((status, result.get('reason')), (409, 'execution_managed_item'))
        self.assertEqual(self.dump(), before)

    def test_legacy_title_edit_still_allows_execution_projection_and_human_review(self):
        body = self.legacy_body(title='Renamed synthetic task')
        renamed = self.store.save_task(body, body['id'])
        self.assertEqual(renamed['title'], 'Renamed synthetic task')
        with self.store.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute("UPDATE execution_runs SET state='completed',cleanup_state='verified' WHERE id=?",
                               (self.execution_id,))
            result = agent_runs.project_execution_state(
                self.store, connection, execution_run_id=self.execution_id, task_id=self.item['id'],
                agent_run_id=self.agent_run_id, dispatch_id=self.dispatch['id'],
                state='completed', cleanup_state='verified', turn_send_started=True,
                turn_id='synthetic-turn')
        self.assertEqual(result['item']['state'], 'review')
        reviewed = management.save_review(self.store, {
            'task_id': self.item['id'], 'expected_revision': result['item']['revision'],
            'verdict': 'approved', 'reason': 'Synthetic human approval', 'confirmed': True})
        self.assertEqual((reviewed['item']['state'], reviewed['review']['run_id']),
                         ('done', self.agent_run_id))
