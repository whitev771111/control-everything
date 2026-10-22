"""Compatibility writes must preserve automatic execution and human review."""
import unittest
import sqlite3
from unittest.mock import patch
from uuid import uuid4

from control_everything.input_validation import ProfileError
from control_everything.workspace import agent_runs, dispatch, management
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

    def execution_facts(self):
        with self.store.connection() as connection:
            execution = dict(connection.execute('SELECT * FROM execution_runs WHERE id=?',
                                                 (self.execution_id,)).fetchone())
            dispatch = dict(connection.execute('SELECT * FROM task_dispatches WHERE id=?',
                                                (self.dispatch['id'],)).fetchone())
        return execution, dispatch

    def project_execution(self):
        with self.store.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            execution = connection.execute('SELECT * FROM execution_runs WHERE id=?',
                                           (self.execution_id,)).fetchone()
            return agent_runs.project_execution_state(
                self.store, connection, execution_run_id=self.execution_id, task_id=self.item['id'],
                agent_run_id=self.agent_run_id, dispatch_id=self.dispatch['id'],
                state=execution['state'], cleanup_state=execution['cleanup_state'],
                turn_send_started=bool(execution['turn_send_started']), turn_id=execution['turn_id'],
                reason=execution['reason'])

    def unknown_execution(self):
        with self.store.connection() as connection:
            connection.execute("UPDATE execution_runs SET state='unknown',cleanup_state='verified',"
                               "reason='protocol_error',turn_send_started=1 WHERE id=?", (self.execution_id,))
        return self.project_execution()

    def release(self, reason='Unknown result acknowledged; release this business run'):
        item = self.store.get_item(self.item['id'])
        return self.request('/api/workspace/agent-tasks/' + item['id'] + '/cancel', 'POST',
                            {'expected_revision': item['revision'], 'reason': reason})

    def test_unknown_verified_human_release_preserves_facts_and_allows_new_dispatch(self):
        projected = self.unknown_execution()
        self.assertEqual(projected['item']['state'], 'blocked')
        original_facts = self.execution_facts()
        self.assertEqual(original_facts[1]['delivery_state'], 'unknown')
        reason = 'Execution outcome is unknown; cleanup verified; request a fresh dispatch'
        status, released = self.release(reason)
        self.assertEqual(status, 200, released)
        self.assertEqual((released['item']['state'], released['run']['status']), ('todo', 'cancelled'))
        self.assertEqual(released['run']['id'], self.agent_run_id)
        self.assertIsNotNone(released['run']['ended_at'])
        detail = agent_runs.task_detail(self.store, self.item['id'])
        release_event = next(event for event in detail['events']
                             if event['payload'].get('reason') == 'human_released_unknown')
        self.assertEqual(release_event['payload']['detail'], reason)
        self.assertEqual(release_event['payload']['execution_run_id'], self.execution_id)
        self.assertEqual(self.execution_facts(), original_facts)

        before = self.dump()
        projected = self.project_execution()
        self.assertEqual((projected['changed'], projected['reason']), (False, 'human_closed'))
        self.assertEqual(self.dump(), before)
        next_dispatch = self.service.create_task_dispatch(self.item['id'], {
            'agent_id': self.agent['id'], 'directory_id': 'main', 'expected_brief_revision': 1,
            'confirmed': True, 'delivery_mode': 'auto'})['dispatch']
        self.assertNotEqual(next_dispatch['id'], self.dispatch['id'])
        next_execution_id = str(uuid4())
        with self.store.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            execution, _dispatch = original_facts
            columns = list(execution)
            next_execution = {**execution, 'id': next_execution_id, 'request_id': next_dispatch['id'],
                              'state': 'starting', 'cleanup_state': 'pending', 'reason': None,
                              'turn_send_started': 0}
            connection.execute('INSERT INTO execution_runs(' + ','.join(columns) + ') VALUES('
                               + ','.join('?' for _column in columns) + ')',
                               [next_execution[column] for column in columns])
            claimed = agent_runs.claim_dispatch_in_transaction(
                self.store, connection, dispatch_id=next_dispatch['id'], task_id=self.item['id'],
                agent_id=self.agent['id'], execution_run_id=next_execution_id,
                expected_revision=released['item']['revision'])
        self.assertNotEqual(claimed['agent_run_id'], self.agent_run_id)
        before = self.dump()
        projected = self.project_execution()
        self.assertEqual((projected['changed'], projected['reason']), (False, 'newer_run'))
        self.assertEqual(self.dump(), before)
        detail = agent_runs.task_detail(self.store, self.item['id'])
        self.assertEqual((detail['item']['state'], detail['run']['id'], detail['run']['status']),
                         ('doing', claimed['agent_run_id'], 'claimed'))
        self.assertEqual(self.execution_facts(), original_facts)

    def test_human_release_rejects_other_execution_states_and_unverified_cleanup(self):
        states = ('starting', 'running', 'waiting_approval', 'cancel_requested', 'completed',
                  'failed', 'interrupted', 'cancelled_before_send', 'unknown')
        for state in states:
            for cleanup in ('pending', 'unverified', 'verified'):
                if (state, cleanup) == ('unknown', 'verified'):
                    continue
                with self.subTest(state=state, cleanup=cleanup):
                    with self.store.connection() as connection:
                        connection.execute('UPDATE execution_runs SET state=?,cleanup_state=? WHERE id=?',
                                           (state, cleanup, self.execution_id))
                    before = self.dump()
                    status, response = self.release()
                    self.assertEqual((status, response.get('reason')), (409, 'execution_managed_run'))
                    self.assertEqual(self.dump(), before)

    def test_unknown_verified_release_rejects_binding_conflicts_without_writes(self):
        self.unknown_execution()
        other_item = self.store.save_item({
            'title': 'Another synthetic task', 'project_id': self.project['id'],
            'provider_id': None, 'state': 'todo', 'session_key': None})
        changes = [('task_dispatches', self.dispatch['id'], 'run_id', None),
                   ('task_dispatches', self.dispatch['id'], 'agent_id', str(uuid4())),
                   ('task_dispatches', self.dispatch['id'], 'task_id', other_item['id']),
                   ('task_dispatches', self.dispatch['id'], 'external_ref', str(uuid4())),
                   ('task_dispatches', self.dispatch['id'], 'external_ref', None),
                   ('task_dispatches', self.dispatch['id'], 'delivery_mode', 'manual'),
                   ('agent_runs', self.agent_run_id, 'dispatch_id', None),
                   ('agent_runs', self.agent_run_id, 'agent_id', str(uuid4())),
                   ('execution_runs', self.execution_id, 'request_id', str(uuid4())),
                   ('execution_runs', self.execution_id, 'task_id', str(uuid4())),
                   ('execution_runs', self.execution_id, 'project_id', str(uuid4())),
                   ('task_items', self.item['id'], 'project_id', None),
                   ('task_items', self.item['id'], 'assigned_agent_id', None)]
        for table, identity, column, value in changes:
            with self.subTest(table=table, column=column):
                with self.store.connection() as connection:
                    previous = connection.execute('SELECT ' + column + ' FROM ' + table + ' WHERE id=?',
                                                  (identity,)).fetchone()[0]
                    connection.execute('UPDATE ' + table + ' SET ' + column + '=? WHERE id=?',
                                       (value, identity))
                before = self.dump()
                status, response = self.release()
                self.assertEqual((status, response.get('reason')), (409, 'execution_link_conflict'))
                self.assertEqual(self.dump(), before)
                with self.store.connection() as connection:
                    connection.execute('UPDATE ' + table + ' SET ' + column + '=? WHERE id=?',
                                       (previous, identity))

    def test_active_execution_with_missing_external_ref_cannot_be_human_released(self):
        with self.store.connection() as connection:
            connection.execute('UPDATE task_dispatches SET external_ref=NULL WHERE id=?',
                               (self.dispatch['id'],))
        before = self.dump()
        status, response = self.release()
        self.assertEqual((status, response.get('reason')), (409, 'execution_link_conflict'))
        self.assertEqual(self.dump(), before)

    def test_active_execution_with_manual_delivery_mode_cannot_be_human_released(self):
        with self.store.connection() as connection:
            connection.execute("UPDATE task_dispatches SET delivery_mode='manual' WHERE id=?",
                               (self.dispatch['id'],))
        before = self.dump()
        status, response = self.release()
        self.assertEqual((status, response.get('reason')), (409, 'execution_link_conflict'))
        self.assertEqual(self.dump(), before)

    def test_historical_execution_does_not_block_a_new_manual_run_release(self):
        self.unknown_execution()
        self.assertEqual(self.release()[0], 200)
        original_facts = self.execution_facts()
        manual_dispatch = self.service.create_task_dispatch(self.item['id'], {
            'agent_id': self.agent['id'], 'directory_id': 'main', 'expected_brief_revision': 1,
            'confirmed': True, 'delivery_mode': 'manual'})['dispatch']
        self.service.mark_dispatch_sent(manual_dispatch['id'])
        item = self.store.get_item(self.item['id'])
        claimed = self.service.claim_agent_task(self.agent['id'], item['id'], item['revision'])
        self.assertEqual(claimed['run']['dispatch_id'], manual_dispatch['id'])
        status, released = self.release()
        self.assertEqual(status, 200, released)
        self.assertEqual((released['item']['state'], released['run']['status']), ('todo', 'cancelled'))
        self.assertEqual(released['run']['id'], claimed['run']['id'])
        self.assertEqual(self.execution_facts(), original_facts)
        detail = agent_runs.task_detail(self.store, self.item['id'])
        self.assertTrue(any(event['run_id'] == claimed['run']['id']
                            and event['payload'].get('reason') == 'human_cancelled'
                            for event in detail['events']))

    def test_unknown_release_checks_binding_while_holding_the_write_transaction(self):
        self.unknown_execution()
        original = dispatch.execution_binding
        checked = []

        def check_locked(connection, **binding):
            self.assertTrue(connection.in_transaction)
            peer = sqlite3.connect(self.store.path, timeout=0)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                    peer.execute('BEGIN IMMEDIATE')
            finally:
                peer.rollback()
                peer.close()
            checked.append(True)
            return original(connection, **binding)

        with patch.object(dispatch, 'execution_binding', side_effect=check_locked):
            status, response = self.release()
        self.assertEqual(status, 200, response)
        self.assertEqual(checked, [True])

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
