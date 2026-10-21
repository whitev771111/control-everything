"""Windows runtime preparation and effective-policy regression contracts."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from control_everything.execution import codex
from control_everything.execution.runtime import build_runtime
from control_everything.execution.runtime_readiness import readiness
from control_everything.execution.service import dispatch_context_digest
from tests.backend import test_codex_execution as protocol
from tests.backend import test_execution_runtime as runtime_tests


class SetupDescriptionTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runtime = build_runtime(SimpleNamespace(data_dir=self.root/'profile'))

    def test_setup_description_is_pure_and_binds_runtime_home_and_executable(self):
        with patch('subprocess.run', side_effect=AssertionError('Implicit system setup')):
            spec = self.runtime.setup_spec('C:/example/codex.exe')
        self.assertFalse(self.runtime.home.exists())
        self.assertEqual(spec['argv'], ['C:/example/codex.exe', '-c',
            'cli_auth_credentials_store="file"', 'sandbox', 'setup', '--elevated',
            '--current-user', '--codex-home', str(self.runtime.home)])
        self.assertIs(spec['requires_user_confirmation'], True)
        self.assertEqual(spec['sandbox_mode'], self.runtime.windows_sandbox_mode)
        self.assertEqual(spec['env']['CODEX_HOME'], str(self.runtime.home))
        self.assertEqual(spec['home'], str(self.runtime.home))

    def test_setup_environment_is_a_copy(self):
        spec = self.runtime.setup_spec('C:/example/codex.exe')
        spec['env']['CODEX_HOME'] = 'C:/example/unrelated'
        self.assertEqual(self.runtime.child_env['CODEX_HOME'], str(self.runtime.home))

    def test_unknown_or_invalid_observations_do_not_imply_ready(self):
        for observed in (None, 'started', 'authenticated', True, {}):
            with self.subTest(observed=observed):
                self.assertEqual(readiness(observed), 'unknown')

    def test_mode_changes_confirmed_context_hash(self):
        dispatch = {'id': 'dispatch', 'content_text': 'fixture', 'delivery_state': 'prepared',
                    'directory_id': 'main', 'directory_path': str(self.root)}
        task = dict(id='task', updated_at='fixture', project_id='project',
                    assigned_agent_id='agent', state='todo')
        project = {'id': 'project', 'name': 'fixture'}
        agent = dict(id='agent', adapter='codex', enabled=True)
        args = (dispatch, task, project, agent, str(self.root), 'fixture-identity')
        self.assertNotEqual(dispatch_context_digest(*args, self.runtime),
            dispatch_context_digest(*args, replace(self.runtime, windows_sandbox_mode='mxc')))

    def test_missing_or_malformed_windows_configuration_is_rejected(self):
        for windows in (None, {}, [], 'elevated', {'sandbox': None}, {'sandbox': False}):
            with self.subTest(windows=windows):
                response = protocol.config()
                response['config']['windows'] = windows
                with self.assertRaisesRegex(codex.ProcessError, 'effective_policy_mismatch'):
                    codex.check_config(response, expected_windows_mode='elevated')
        with self.assertRaisesRegex(codex.ProcessError, 'effective_policy_mismatch'):
            codex.check_config(protocol.config(), expected_windows_mode='elevated')


class WindowsRuntimeProtocolTests(unittest.TestCase):
    setUp = runtime_tests.RuntimeProtocolTests.setUp
    data = runtime_tests.RuntimeProtocolTests.data
    run_with_account = runtime_tests.RuntimeProtocolTests.run_with_account

    def worker(self):
        worker, transport = protocol.CodexExecutionTests.worker(self)
        if getattr(self, 'thread_change', None):
            response = protocol.thread_result(worker.cwd)
            response.update(self.thread_change)
            transport.changes['thread/start'] = response
        return worker, transport

    def run_observed(self, windows_mode='elevated'):
        base = protocol.config()
        base['config']['windows'] = {'sandbox': windows_mode} if windows_mode is not None else {}
        with patch.object(protocol, 'config', return_value=base):
            return self.run_with_account({'type': 'chatgpt'})

    def test_launch_pins_described_windows_mode(self):
        worker, _transport, launch, runtime = self.run_observed()
        self.assertIn('windows.sandbox="elevated"', launch['args'])
        self.assertEqual(self.data(worker)['run']['effective_policy']['runtime'], runtime.public())

    def test_readonly_downgrade_blocks_before_turn_and_cleans(self):
        self.thread_change = {'sandbox': codex.sandbox_policy('read-only')}
        worker, transport, _launch, _runtime = self.run_observed()
        row = self.data(worker)['run']
        self.assertEqual((row['state'], row['reason'], row['cleanup_state']),
            ('failed', 'windows_sandbox_setup_required', 'verified'))
        self.assertNotIn('turn/start', [message.get('method') for message in transport.sent])
        self.assertFalse(worker.turn_sent)

    def test_broader_policy_remains_rejected(self):
        self.thread_change = {'sandbox': {'type': 'dangerFullAccess'}}
        worker, transport, _launch, _runtime = self.run_observed()
        self.assertEqual(self.data(worker)['run']['reason'], 'effective_policy_mismatch')
        self.assertNotIn('turn/start', [message.get('method') for message in transport.sent])

    def test_actual_windows_mode_must_match_description_before_turn(self):
        for mode in ('mxc', 'unelevated', None, True, [], {}):
            with self.subTest(actual_mode=mode):
                worker, transport, _launch, _runtime = self.run_observed(mode)
                self.assertNotIn('turn/start', [message.get('method') for message in transport.sent],
                    'Requested mode alone must not substitute for effective config verification')
                self.assertEqual(self.data(worker)['run']['reason'], 'effective_policy_mismatch')
                self.assertNotIn('account/read', [message.get('method') for message in transport.sent])

    def test_readonly_with_other_policy_errors_is_not_reported_as_setup_required(self):
        cases = ({'cwd': str(self.root/'missing')}, {'approvalPolicy': 'never'},
                 {'approvalsReviewer': 'auto_review'}, {'activePermissionProfile': {'id': 'other'}},
                 {'sandbox': {'type': 'readOnly', 'networkAccess': True}})
        for changes in cases:
            with self.subTest(changes=changes):
                self.thread_change = {'sandbox': codex.sandbox_policy('read-only'), **changes}
                worker, transport, _launch, _runtime = self.run_observed()
                row = self.data(worker)['run']
                self.assertEqual((row['reason'], row['cleanup_state']),
                                 ('effective_policy_mismatch', 'verified'))
                self.assertNotIn('turn/start', [message.get('method') for message in transport.sent])

    def test_malformed_thread_preserves_protocol_error(self):
        self.thread_change = {'model': None}
        worker, transport, _launch, _runtime = self.run_observed()
        self.assertEqual(self.data(worker)['run']['reason'], 'protocol_error')
        self.assertNotIn('turn/start', [message.get('method') for message in transport.sent])


if __name__ == '__main__':
    unittest.main()
