"""Read-only dispatch preparation contracts; no native execution or personal data."""
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from control_everything.input_validation import ProfileError
from control_everything.http.gateway import PROFILE_CAPABILITIES
from control_everything.execution.service import dispatch_execution_context, prepare_dispatch_facts
from tests.backend import test_dispatch as fixtures
from tests.backend import test_execution_http as http_fixtures


class DispatchExecutionContextTests(unittest.TestCase):
    request = http_fixtures.ExecutionHTTPTests.request

    def setUp(self):
        fixtures.DispatchTests.setUp(self)
        self.save_agent(adapter='codex')
        fixtures.DispatchTests.brief(self)
        self.dispatch = self.service.create_task_dispatch(self.item['id'], {
            'agent_id': self.agent['id'], 'directory_id': 'main',
            'expected_brief_revision': 1, 'confirmed': True, 'delivery_mode': 'auto'})['dispatch']
        self.route = '/api/workspace/dispatches/' + self.dispatch['id'] + '/execution-context'
        self.server = SimpleNamespace(profile=self.profile, workspace=self.service,
            execution=None, capabilities=dict(PROFILE_CAPABILITIES),
            expected_host='127.0.0.1:1234', origin='http://127.0.0.1:1234', csrf_token='token')

    def save_agent(self, **changes):
        current = self.profile.agent_settings()
        row = {key: self.agent[key] for key in
               ('id', 'name', 'kind', 'command', 'enabled', 'project_ids', 'adapter', 'connection')}
        row.update(changes)
        self.agent = self.profile.save_agents({'expected_revision': current['revision'], 'items': [row]})['items'][0]

    def context(self):
        return dispatch_execution_context(self.service, self.dispatch['id'])

    def update(self, table, identity, **fields):
        with self.profile.store.connection() as conn:
            conn.execute('UPDATE ' + table + ' SET ' + ','.join(key + '=?' for key in fields) + ' WHERE id=?',
                         (*fields.values(), identity))

    def rejected(self, reason):
        with self.assertRaises(ProfileError) as caught:
            self.context()
        self.assertEqual(caught.exception.reason, reason)

    def dump(self):
        with self.profile.store.connection() as conn:
            return list(conn.iterdump())

    def test_exact_snapshot_hash_and_no_write_or_worker(self):
        before = self.dump()
        with patch('control_everything.execution.service.ExecutionService',
                   side_effect=AssertionError('GET must not construct an execution service')), patch(
                   'control_everything.execution.service.ExecutionService._start_worker',
                   side_effect=AssertionError('GET must not start a worker')):
            status, value = self.request(self.route)
            self.assertEqual(status, 200, value)
        self.assertEqual(value['prompt'], self.dispatch['content_text'])
        self.assertEqual(value['prompt_sha256'], hashlib.sha256(value['prompt'].encode('utf8')).hexdigest())
        self.assertEqual(value['request_id'], self.dispatch['id'])
        self.assertEqual(value['task_id'], self.item['id'])
        self.assertEqual(value['agent'], {'id': self.agent['id'], 'adapter': 'codex'})
        self.assertEqual(self.dump(), before)
        self.assertFalse(PROFILE_CAPABILITIES['execution'])

    def test_later_brief_cannot_replace_confirmed_prompt(self):
        before = self.context()
        self.service.save_task_brief(self.item['id'], {'expected_revision': 1,
            'request_text': 'Completely different request', 'acceptance_text': '',
            'constraints_text': '', 'references': [], 'execution_directory_id': 'main'})
        after = self.context()
        self.assertEqual(before['prompt'], after['prompt'])
        self.assertEqual(before['context_hash'], after['context_hash'])

    def test_boundaries_and_no_truncation(self):
        for text, accepted in [('x'*4000, True), ('😀'*4000, True), ('x'*4001, False), ('😀'*4001, False)]:
            with self.subTest(length=len(text), bytes=len(text.encode('utf8'))):
                self.update('task_dispatches', self.dispatch['id'], content_text=text)
                if accepted:
                    self.assertEqual(self.context()['prompt'], text)
                else:
                    self.rejected('prompt_too_long')
        self.update('task_dispatches', self.dispatch['id'], content_text='  ')
        self.rejected('prompt_required')

    def test_dispatch_states_and_bindings_are_rejected(self):
        for state in ['sent', 'delivered', 'cancelled']:
            with self.subTest(state=state):
                self.update('task_dispatches', self.dispatch['id'], delivery_state=state)
                self.rejected('dispatch_state_conflict')
        self.update('task_dispatches', self.dispatch['id'], delivery_state='previewed', external_ref='existing')
        self.rejected('dispatch_state_conflict')
        self.update('task_dispatches', self.dispatch['id'], external_ref=None)
        item = self.profile.store.get_item(self.item['id'])
        run = self.service.claim_agent_task(self.agent['id'], item['id'], item['revision'])['run']
        self.update('task_dispatches', self.dispatch['id'], run_id=run['id'])
        self.rejected('dispatch_state_conflict')

    def test_task_states_and_project_mismatch(self):
        for state in ['review', 'done']:
            self.update('task_items', self.item['id'], state=state)
            self.rejected('dispatch_state_conflict')
        self.update('task_items', self.item['id'], state='todo', project_id=None)
        self.rejected('project_required')

    def test_disabled_unsupported_scoped_and_reassigned_agent(self):
        self.save_agent(enabled=False)
        self.rejected('agent_not_found')
        self.save_agent(enabled=True, adapter='manual')
        self.rejected('execution_unsupported')
        self.save_agent(adapter='codex', project_ids=['other-project'])
        self.rejected('project_forbidden')
        self.save_agent(project_ids=[])
        self.update('task_items', self.item['id'], assigned_agent_id=str(uuid4()))
        self.rejected('run_conflict')

    def test_directory_rebinding_is_rejected(self):
        (self.root/'other').mkdir()
        with self.profile.store.connection() as conn:
            conn.execute('UPDATE project_directories SET path=? WHERE project_id=?',
                         (str(self.root/'other'), self.project['id']))
        self.rejected('context_changed')

    def test_claimed_run_is_rejected(self):
        item = self.profile.store.get_item(self.item['id'])
        self.service.claim_agent_task(self.agent['id'], item['id'], item['revision'])
        self.rejected('run_conflict')

    def test_nonempty_connection_is_rejected_in_preparation(self):
        self.save_agent(connection={'args': ['--example-mode']})
        self.rejected('execution_unsupported')

    def test_connection_key_order_is_stable_and_private(self):
        # Pure encoding still covers future connection fields; the public entry rejects them today.
        facts = self.service.dispatch_execution_facts(self.dispatch['id'])
        facts['agent'] = {**facts['agent'], 'connection': {'mode': 'example', 'args': ['--example-mode']}}
        before, _ = prepare_dispatch_facts(facts)
        facts['agent']['connection'] = {'args': ['--example-mode'], 'mode': 'example'}
        after, _ = prepare_dispatch_facts(facts)
        self.assertEqual(before['context_hash'], after['context_hash'])
        self.assertEqual(set(after['agent']), {'id', 'adapter'})
        self.assertNotIn('--example-mode', str(after))

    def test_project_scope_is_hashed_with_stable_order(self):
        before = self.context()['context_hash']
        self.save_agent(project_ids=[self.project['id']])
        self.assertNotEqual(before, self.context()['context_hash'])
        self.save_agent(project_ids=[self.project['id'], 'other-project'])
        scoped = self.context()['context_hash']
        self.save_agent(project_ids=['other-project', self.project['id']])
        self.assertEqual(scoped, self.context()['context_hash'])

    def test_clearing_assignment_invalidates_old_dispatch(self):
        self.update('task_items', self.item['id'], assigned_agent_id=None)
        self.rejected('run_conflict')

    def test_route_guards_and_no_post_execution(self):
        for headers in [{'Host': 'evil'}, {'Origin': 'https://evil'}, {'X-Session-Client': ''}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(self.route, headers=headers)[0], 403)
        self.assertEqual(self.request(self.route+'?extra=1')[0], 400)
        self.assertEqual(self.request(self.route.replace(self.dispatch['id'], str(uuid4())))[0], 404)
        self.assertNotEqual(self.request(self.route.replace('execution-context', 'execution'), 'POST', {})[0], 200)


if __name__ == '__main__':
    unittest.main()
