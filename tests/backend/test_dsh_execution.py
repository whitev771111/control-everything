"""DSH protocol and dispatch contracts using disposable stores and fake processes."""
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control_everything.execution.dsh import DshRun
from control_everything.execution.owned_process import OwnedProcess, ProcessError
from control_everything.execution.dsh_runtime import build_dsh_runtime
from control_everything.execution.service import ExecutionService
from control_everything.execution.store import ExecutionStore
from control_everything.input_validation import ProfileError
from control_everything.workspace.store import WorkspaceError
from tests.backend import test_codex_execution as protocol
from tests.backend import test_dispatch_admission as admission
from tests.backend.test_execution_store import FakeOwner, HoldingWorker


class FakeDshProcess:
    def __init__(self, messages, exit_code=0, cleanup=True):
        self.messages = list(copy.deepcopy(messages))
        self.code, self.cleanup = exit_code, cleanup
        self.written = []
        self.on_read = None

    def start(self):
        return self

    def write_text(self, text, *, close=False):
        self.written.append((text, close))

    def read(self, timeout=.1):
        if self.on_read:
            self.on_read()
        if self.messages:
            return self.messages.pop(0)
        raise ProcessError('process_eof')

    def exit_code(self):
        return self.code

    def close(self, grace=2):
        return self.cleanup


class DshProtocolTests(unittest.TestCase):
    def setUp(self):
        protocol.CodexExecutionTests.setUp(self)
        environment = patch.dict(os.environ, {'COMSPEC': os.environ.get('COMSPEC') or 'C:/example/Windows/System32/cmd.exe'})
        environment.start()
        self.addCleanup(environment.stop)
    tearDown = protocol.CodexExecutionTests.tearDown
    data = protocol.CodexExecutionTests.data

    def worker(self, messages=None, *, code=0, cleanup=True):
        prototype, _ = protocol.CodexExecutionTests.worker(self)
        process = FakeDshProcess(messages if messages is not None else
                                 [{'type': 'text', 'text': 'fixture final'},
                                  {'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'completed'}},
                                  {'type': 'final', 'text': 'fixture final'}], code, cleanup)
        self.launch = {}
        def factory(exe, args, cwd, name, **kwargs):
            self.launch.update(exe=exe, args=args, cwd=cwd, kwargs=kwargs)
            return process
        worker = DshRun(self.store, prototype.record, self.owner, lambda _: True,
                        process_factory=factory, resolver=lambda: 'C:/example/fixture/dsh.cmd')
        return worker, process

    def test_prompt_uses_stdin_and_confirmed_directory(self):
        worker, process = self.worker()
        worker.run()
        self.assertEqual(process.written, [('fixture prompt', True)])
        self.assertEqual(self.launch['cwd'], worker.cwd)
        self.assertEqual(self.launch['args'][-4:], ['C:/example/fixture/dsh.cmd', 'headless', '--json', '-'])
        self.assertNotIn('fixture prompt', self.launch['args'])
        self.assertEqual(self.data(worker)['run']['cleanup_state'], 'verified')

    def test_missing_comspec_uses_configured_windows_directory(self):
        worker, _ = self.worker()
        with patch.dict(os.environ, {'SYSTEMROOT': 'C:/example/Windows'}, clear=True):
            worker.run()
        self.assertEqual(Path(self.launch['exe']), Path('C:/example/Windows') / 'System32' / 'cmd.exe')
        self.assertEqual(self.data(worker)['run']['state'], 'completed')

    def test_missing_shell_environment_fails_before_start(self):
        worker, process = self.worker()
        with patch.dict(os.environ, {}, clear=True):
            worker.run()
        self.assertFalse(self.launch)
        self.assertFalse(process.written)
        self.assertEqual(self.data(worker)['run']['reason'], 'shell_not_found')

    def test_account_runtime_selects_headless_profile_before_app_arguments(self):
        worker, _ = self.worker()
        worker.runtime = build_dsh_runtime(SimpleNamespace(data_dir=self.root/'profile with spaces'), create=True)
        worker.run()
        arguments = self.launch['args'][4:]
        if arguments[0] == '--profile':
            self.assertEqual(arguments[1], 'headless')
        else:
            self.assertEqual(arguments[0], 'headless',
                             'DSH positional profile shorthand only works as the first CLI argument')

    def test_success_requires_successful_exit_and_final_result(self):
        worker, _ = self.worker()
        worker.run()
        self.assertEqual(self.data(worker)['run']['state'], 'completed')

    def test_installed_headless_final_event_is_recognized(self):
        worker, _ = self.worker([{'type': 'session', 'sessionId': 'session-fixture', 'cwd': str(self.root)},
            {'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'completed'}},
            {'type': 'final', 'text': 'fixture final'}])
        worker.run()
        self.assertEqual(self.data(worker)['run']['state'], 'completed')

    def test_nonzero_exit_is_not_success(self):
        worker, _ = self.worker(code=7)
        worker.run()
        self.assertEqual(self.data(worker)['run']['state'], 'failed')

    def test_missing_exit_code_is_not_success(self):
        worker, _ = self.worker(code=None)
        worker.run()
        self.assertNotEqual(self.data(worker)['run']['state'], 'completed')

    def test_intermediate_text_without_final_result_is_not_success(self):
        worker, _ = self.worker([{'type': 'text', 'text': 'Starting work'}])
        worker.run()
        self.assertNotEqual(self.data(worker)['run']['state'], 'completed')

    def test_final_without_success_terminal_is_not_success(self):
        worker, _ = self.worker([{'type': 'final', 'text': 'Partial answer'}])
        worker.run()
        self.assertNotEqual(self.data(worker)['run']['state'], 'completed')

    def test_text_progress_is_visible_before_final(self):
        worker, _ = self.worker()
        worker._handle({'type': 'text', 'text': 'Working on fixture'})
        messages = [row['payload'] for row in self.data(worker)['events']
                    if row['kind'] == 'agent_message']
        self.assertTrue(any(row.get('text') == 'Working on fixture' and not row.get('final')
                            for row in messages))

    def test_native_terminal_error_is_not_success(self):
        worker, _ = self.worker([{'type': 'text', 'text': 'Starting work'},
                                 {'type': 'error', 'message': 'fixture terminal failure'}])
        worker.run()
        self.assertNotEqual(self.data(worker)['run']['state'], 'completed')

    def test_installed_error_terminal_cannot_complete_with_partial_text(self):
        worker, _ = self.worker([{'type': 'text', 'text': 'Partial answer'},
            {'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'error',
                'error': {'code': 'fixture_error', 'message': 'fixture failure'}}},
            {'type': 'final', 'text': 'Partial answer'}])
        worker.run()
        self.assertNotEqual(self.data(worker)['run']['state'], 'completed')

    def test_native_login_errors_keep_login_reason_for_any_exit_code(self):
        for error_code in ('MISSING_CREDENTIAL', 'ACCOUNT_SIGN_IN_REQUIRED'):
            for exit_code in (0, 7):
                with self.subTest(error_code=error_code, exit_code=exit_code):
                    worker, _ = self.worker([
                        {'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'error',
                            'error': {'code': error_code, 'message': 'fixture sign-in required'}}},
                        {'type': 'final', 'text': 'Partial answer'}], code=exit_code)
                    worker.run()
                    data = self.data(worker)
                    row = data['run']
                    self.assertEqual((row['state'], row['reason'], row['cleanup_state']),
                                     ('failed', 'login_required', 'verified'))
                    self.assertEqual(worker.auth_status, 'login_required')
                    self.assertTrue(any(event['kind'] == 'phase'
                                        and event['payload'].get('phase') == 'auth'
                                        and event['payload'].get('status') == 'login_required'
                                        for event in data['events']))

    def test_unrelated_native_errors_keep_native_failure_reason(self):
        for error_code in ('fixture_error', 'ACCOUNT_QUOTA', 'ACCOUNT_TOKEN_INVALID', None):
            for exit_code, reason in ((0, 'native_error'), (7, 'native_exit_failed')):
                with self.subTest(error_code=error_code, exit_code=exit_code):
                    worker, _ = self.worker([
                        {'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'error',
                            'error': {'code': error_code, 'message': 'fixture failure'}}},
                        {'type': 'final', 'text': 'Partial answer'}], code=exit_code)
                    worker.run()
                    data = self.data(worker)
                    row = data['run']
                    self.assertEqual((row['state'], row['reason'], row['cleanup_state']),
                                     ('failed', reason, 'verified'))
                    self.assertNotEqual(worker.auth_status, 'login_required')
                    self.assertFalse(any(event['kind'] == 'phase'
                                         and event['payload'].get('status') == 'login_required'
                                         for event in data['events']))

    def test_cleanup_failure_remains_unverified(self):
        worker, _ = self.worker(cleanup=False)
        worker.run()
        self.assertEqual(self.data(worker)['run']['cleanup_state'], 'unverified')

    def test_cancellation_does_not_complete_partial_text(self):
        worker, process = self.worker([{'type': 'text', 'text': 'partial'}])
        process.on_read = worker.interrupt
        worker.run()
        self.assertEqual(self.data(worker)['run']['state'], 'interrupted')

    def test_cancelled_quiet_process_finishes_without_waiting_for_eof(self):
        for cleanup, expected in ((True, 'verified'), (False, 'unverified')):
            with self.subTest(cleanup=cleanup):
                worker, process = self.worker([
                    {'type': 'session', 'sessionId': 'cancelled-session'},
                    {'type': 'text', 'text': 'already received'},
                    {'type': 'file_change', 'changes': [{'path': 'fixture.txt'}]}], cleanup=cleanup)
                process.on_read = worker.interrupt
                original_read = process.read
                empty_reads = []
                def quiet_read(timeout=.1):
                    try:
                        return original_read(timeout)
                    except ProcessError as error:
                        if str(error) != 'process_eof':
                            raise
                        if empty_reads:
                            raise ProcessError('fixture_poll_limit')
                        empty_reads.append(timeout)
                        return None
                process.read = Mock(side_effect=quiet_read)
                worker.run()
                data = self.data(worker)
                self.assertEqual((data['run']['state'], data['run']['reason'],
                                  data['run']['cleanup_state']), ('interrupted', 'cancelled', expected))
                self.assertTrue(worker.finished.is_set())
                self.assertEqual(process.read.call_count, 4,
                                 'Cancellation must stop once buffered messages have drained')
                self.assertEqual(data['run']['thread_id'], 'cancelled-session')
                self.assertTrue(any(event['kind'] == 'file_change' for event in data['events']))
                self.assertTrue(any(event['kind'] == 'agent_message'
                                    and event['payload'].get('text') == 'already received'
                                    for event in data['events']))
                self.assertFalse(any(event['kind'] == 'error' for event in data['events']))

    def test_idle_read_without_cancellation_waits_for_later_final(self):
        worker, process = self.worker()
        process.read = Mock(side_effect=[None, *process.messages, ProcessError('process_eof')])
        worker.run()
        self.assertEqual(self.data(worker)['run']['state'], 'completed')
        self.assertEqual(process.read.call_count, 5)

    def test_interrupt_after_completion_keeps_terminal_result(self):
        worker, _ = self.worker()
        worker.run()
        before = self.data(worker)['run']
        worker.interrupt()
        after = self.data(worker)['run']
        self.assertEqual(before['state'], 'completed')
        self.assertEqual(after, before)

    def test_cancellation_does_not_promote_buffered_final_to_success(self):
        worker, process = self.worker()
        process.on_read = worker.interrupt
        worker.run()
        data = self.data(worker)
        self.assertTrue(worker.final_seen)
        self.assertEqual(worker.terminal_status, 'completed')
        self.assertEqual(data['run']['state'], 'interrupted')
        self.assertFalse(any(event['kind'] == 'agent_message' and event['payload'].get('final')
                             for event in data['events']))

    def test_pre_send_cancellation_writes_no_prompt(self):
        worker, process = self.worker()
        worker.interrupt()
        worker.run()
        self.assertFalse(process.written)
        self.assertEqual(self.data(worker)['run']['state'], 'cancelled_before_send')

    def test_cancellation_during_resolve_does_not_launch_or_send(self):
        worker, process = self.worker()
        def resolve():
            worker.interrupt()
            return 'C:/example/fixture/dsh.cmd'
        worker.resolver = resolve
        worker.run()
        self.assertFalse(process.written)
        self.assertFalse(self.launch)
        self.assertEqual((self.data(worker)['run']['state'], self.data(worker)['run']['cleanup_state']),
                         ('cancelled_before_send', 'verified'))

    def test_cancellation_during_process_start_does_not_send(self):
        worker, process = self.worker()
        def start():
            worker.interrupt()
            return process
        process.start = start
        worker.run()
        self.assertFalse(process.written)
        self.assertFalse(self.store.get_run(worker.id)['turn_send_started'])
        self.assertEqual((self.data(worker)['run']['state'], self.data(worker)['run']['cleanup_state']),
                         ('cancelled_before_send', 'verified'))

    def actual_launcher(self, directory, *, account_diagnostic=False):
        prototype, _ = protocol.CodexExecutionTests.worker(self)
        folder = self.root/directory
        folder.mkdir()
        launcher = folder/'dsh.cmd'
        diagnostic = b'echo [llm-deepseek-account] fixture catalog loaded\r\n' if account_diagnostic else b''
        launcher.write_bytes(b'@echo off\r\nset /p "task="\r\n' + diagnostic +
            b'echo {"type":"status","phase":"turn_end","reason":{"kind":"completed"}}\r\n'
            b'echo {"type":"final","text":"fixture final"}\r\nexit /b 0\r\n')
        worker = DshRun(self.store, prototype.record, self.owner, lambda _: True,
                        resolver=lambda: str(launcher))
        worker.run()
        return self.data(worker)['run']

    @unittest.skipUnless(sys.platform == 'win32', 'Actual Windows cmd quoting')
    def test_actual_launcher_path_with_spaces(self):
        row = self.actual_launcher('DSH with spaces')
        self.assertEqual((row['state'], row['cleanup_state']), ('completed', 'verified'), row.get('reason'))

    @unittest.skipUnless(sys.platform == 'win32', 'Actual Windows cmd quoting control')
    def test_actual_launcher_path_without_spaces(self):
        row = self.actual_launcher('DSH-no-spaces')
        self.assertEqual((row['state'], row['cleanup_state']), ('completed', 'verified'), row.get('reason'))

    @unittest.skipUnless(sys.platform == 'win32', 'Actual account provider diagnostic stream')
    def test_account_provider_diagnostic_does_not_corrupt_run(self):
        row = self.actual_launcher('DSH-account-diagnostic', account_diagnostic=True)
        self.assertEqual((row['state'], row['cleanup_state']), ('completed', 'verified'), row.get('reason'))


class DshDispatchTests(unittest.TestCase):
    save_agent = admission.DispatchAdmissionTests.save_agent
    context = admission.DispatchAdmissionTests.context
    update = admission.DispatchAdmissionTests.update
    dump = admission.DispatchAdmissionTests.dump
    admit = admission.DispatchAdmissionTests.admit

    def setUp(self):
        admission.fixtures.DispatchExecutionContextTests.setUp(self)
        self.ledger = ExecutionStore(self.service.store)
        self.owner = Mock()
        self.save_agent(adapter='dsh', command='dsh', kind='dsh', network='allowed')
        self.service.cancel_task_dispatch(self.dispatch['id'])
        self.service.save_task_brief(self.item['id'], {
            'expected_revision': 1, 'request_text': '只回复“收到”。',
            'acceptance_text': '回复内容为收到。', 'constraints_text': '不使用工具，不修改文件。',
            'references': [], 'execution_directory_id': 'main'})
        self.dispatch = self.service.create_task_dispatch(self.item['id'], {
            'agent_id': self.agent['id'], 'directory_id': 'main',
            'expected_brief_revision': 2, 'confirmed': True, 'delivery_mode': 'auto'})['dispatch']
        self.body = {'confirmed': True, 'context_hash': self.context()['context_hash']}

    def test_dsh_admission_records_adapter_without_codex_home(self):
        result = self.admit()
        self.assertEqual(json.loads(result['run']['effective_policy_json'])['adapter'], 'dsh')
        self.assertFalse((self.profile.data_dir/'agent-runtime'/'codex').exists())

    def test_confirmed_native_runtime_is_persisted_and_rechecked_before_start(self):
        expected = self.context()['dsh_runtime']
        self.assertEqual((expected['auth_mode'], expected['provider'], expected['model'],
                          expected['policy_version']),
                         ('native-config', None, None, 'dsh-headless-v3'))
        receipt = self.admit()
        policy = json.loads(receipt['run']['effective_policy_json'])
        self.assertEqual(policy.get('dsh_runtime'), expected)
        service = ExecutionService(self.service, store=self.ledger, owner_factory=lambda _: FakeOwner(None),
            job_query=lambda _: True, platform='win32', codex_required=False,
            dsh_worker_factory=HoldingWorker)
        self.addCleanup(service.close)
        self.assertTrue(service.validate_root(receipt['run']))
        self.assertEqual(service._dsh_runtime_for_run(receipt['run']).public(), expected)

    def test_old_fixed_account_confirmation_cannot_start_native_config_worker(self):
        factory = Mock()
        service = ExecutionService(self.service, store=self.ledger, owner_factory=lambda _: FakeOwner(None),
            job_query=lambda _: True, platform='win32', codex_required=False, dsh_worker_factory=factory)
        self.addCleanup(service.close)
        legacy = replace(build_dsh_runtime(self.profile), auth_mode='desktop-account',
                         provider='deepseek-account', model='deepseek-flash',
                         policy_version='dsh-headless-v2')
        with patch('control_everything.execution.dsh_runtime.build_dsh_runtime', return_value=legacy):
            body = {'confirmed': True, 'context_hash': self.context()['context_hash']}
        before = self.dump()
        with self.assertRaises(ProfileError) as error:
            self.admit(body=body)
        self.assertEqual(error.exception.reason, 'context_changed')
        self.assertEqual(self.dump(), before)
        with patch('control_everything.execution.dsh_runtime.build_dsh_runtime', return_value=legacy):
            receipt = self.admit(body=body)
        run = receipt['run']
        self.assertFalse(service.validate_root(run))
        with self.assertRaises(WorkspaceError) as error:
            service._dsh_runtime_for_run(run)
        self.assertEqual(str(error.exception), 'context_changed')
        service._start_worker(run)
        factory.assert_not_called()
        stored = self.ledger.get_run(run['id'])
        self.assertEqual((stored['state'], stored['reason'], stored['cleanup_state']),
                         ('failed', 'context_changed', 'verified'))
        self.assertEqual(stored['effective_policy_json'], run['effective_policy_json'])

    def test_tampered_native_policy_artifact_cannot_start_worker(self):
        factory = Mock()
        service = ExecutionService(self.service, store=self.ledger, owner_factory=lambda _: FakeOwner(None),
            job_query=lambda _: True, platform='win32', codex_required=False, dsh_worker_factory=factory)
        self.addCleanup(service.close)
        run = self.admit()['run']
        artifact = service._dsh_runtime_for_run(run).overlay_path
        artifact.write_text('invalid fixture overlay', encoding='utf8')
        service._start_worker(run)
        factory.assert_not_called()
        stored = self.ledger.get_run(run['id'])
        self.assertEqual((stored['state'], stored['reason'], stored['cleanup_state']),
                         ('failed', 'worker_start_failed', 'verified'))
        self.assertEqual(artifact.read_text(encoding='utf8'), 'invalid fixture overlay')

    def test_home_change_before_admission_rejects_old_confirmation(self):
        with patch.dict(os.environ, {'DSH_HOME': str(self.root/'different-dsh-home')}):
            with self.assertRaises(ProfileError) as error:
                self.admit()
        self.assertEqual(error.exception.reason, 'context_changed')

    def test_home_change_after_admission_invalidates_execution(self):
        receipt = self.admit()
        service = ExecutionService(self.service, store=self.ledger, owner_factory=lambda _: FakeOwner(None),
            job_query=lambda _: True, platform='win32', codex_required=False,
            dsh_worker_factory=HoldingWorker)
        self.addCleanup(service.close)
        with patch.dict(os.environ, {'DSH_HOME': str(self.root/'different-dsh-home')}):
            self.assertFalse(service.validate_root(receipt['run']),
                             'A confirmed native home must not change before the process starts')
            with self.assertRaises(WorkspaceError) as error:
                service._dsh_runtime_for_run(receipt['run'])
            self.assertEqual(str(error.exception), 'context_changed')

    def test_file_task_is_admitted_under_confirmed_sandbox_policy(self):
        self.update('task_dispatches', self.dispatch['id'], content_text='Write report.md in the project directory.')
        self.body['context_hash'] = self.context()['context_hash']
        factory = Mock(side_effect=lambda **kwargs: HoldingWorker(**kwargs))
        service = ExecutionService(self.service, store=self.ledger, owner_factory=lambda _: FakeOwner(None),
            job_query=lambda _: True, platform='win32', codex_required=False, dsh_worker_factory=factory)
        self.addCleanup(service.close)
        receipt = service.submit_dispatch(self.dispatch['id'], self.body)
        self.assertTrue(receipt['execution_run_id'])
        factory.assert_called_once()
        self.assertEqual(factory.call_args.kwargs['runtime'].permission, 'workspace-write')

    def test_chinese_file_task_uses_sandbox_instead_of_keyword_rejection(self):
        self.service.cancel_task_dispatch(self.dispatch['id'])
        self.service.save_task_brief(self.item['id'], {
            'expected_revision': 2, 'request_text': '在当前目录新建一个文件，写入收到。',
            'acceptance_text': '文件存在。', 'constraints_text': '',
            'references': [], 'execution_directory_id': 'main'})
        self.dispatch = self.service.create_task_dispatch(self.item['id'], {
            'agent_id': self.agent['id'], 'directory_id': 'main',
            'expected_brief_revision': 3, 'confirmed': True, 'delivery_mode': 'auto'})['dispatch']
        receipt = self.admit(body={'confirmed': True, 'context_hash': self.context()['context_hash']})
        policy = json.loads(receipt['run']['effective_policy_json'])
        self.assertEqual(policy['dsh_runtime']['permission'], 'workspace-write')

    def test_manual_snapshot_remains_rejected(self):
        self.dispatch = self.service.create_task_dispatch(self.item['id'], {
            'agent_id': self.agent['id'], 'directory_id': 'main', 'expected_brief_revision': 2,
            'confirmed': True, 'delivery_mode': 'manual'})['dispatch']
        self.body['context_hash'] = self.context()['context_hash']
        with self.assertRaises(ProfileError) as error:
            self.admit()
        self.assertEqual(error.exception.reason, 'execution_mode_conflict')

    def test_unsupported_configuration_is_rejected(self):
        for changes in ({'command': 'arbitrary', 'connection': {}},
                        {'command': 'dsh', 'connection': {'args': ['extra']}}):
            with self.subTest(changes=changes):
                self.save_agent(**changes)
                with self.assertRaises(ProfileError):
                    self.context()

    def test_dsh_context_does_not_claim_codex_policy(self):
        policy = self.context()['policy']
        self.assertNotIn('CODEX_HOME', policy.get('inheritance_note', ''))
        self.assertEqual(policy['mode'], 'workspace-write')
        self.assertIn('读取与网络不在同一沙箱边界', policy['inheritance_note'])

    def test_adapter_changed_after_admission_is_rejected(self):
        receipt = self.admit()
        self.save_agent(adapter='codex', command='codex', kind='codex', network='denied')
        with self.profile.agent_settings_guard() as settings:
            with self.profile.store.connection() as connection:
                with self.assertRaises(ProfileError) as error:
                    self.service.execution_run_facts(connection=connection, agent_settings=settings,
                        execution_run_id=receipt['run']['id'], agent_run_id=receipt['agent_run_id'],
                        dispatch_id=self.dispatch['id'], task_id=self.item['id'])
        self.assertEqual(error.exception.reason, 'execution_link_conflict')

    def test_dsh_only_service_selects_dsh_without_codex_resolver(self):
        codex_factory = Mock()
        dsh_factory = Mock(side_effect=lambda **kwargs: HoldingWorker(**kwargs))
        service = ExecutionService(self.service, store=self.ledger, owner_factory=lambda _: FakeOwner(None),
            job_query=lambda _: True, platform='win32', codex_required=False,
            resolver=Mock(side_effect=AssertionError('DSH must not resolve Codex')),
            dsh_worker_factory=dsh_factory)
        service._worker_factory = codex_factory
        self.addCleanup(service.close)
        receipt = service.submit_dispatch(self.dispatch['id'], self.body)
        self.assertTrue(receipt['execution_run_id'])
        self.assertEqual(dsh_factory.call_count, 1)
        self.assertEqual(codex_factory.call_count, 0)
        self.assertFalse((self.profile.data_dir/'agent-runtime'/'codex').exists())


class DshDiagnosticBoundaryTests(unittest.TestCase):
    def test_unknown_and_malformed_lines_remain_errors_and_codex_stays_strict(self):
        from tests.backend.test_owned_process import FakeAPI
        for raw, prefixes in [
            (b'[unknown-provider] fixture\n', ('[llm-deepseek-account] ',)),
            (b'{broken json}\n', ('[llm-deepseek-account] ',)),
            (b'[llm-deepseek-account] fixture\n', ()),
        ]:
            with self.subTest(raw=raw, prefixes=prefixes):
                process = OwnedProcess('fixture.exe', [], '.', 'fixture',
                    FakeAPI(chunks=[raw]), line_prefixes=prefixes).start()
                try:
                    with self.assertRaisesRegex(ProcessError, 'protocol_error'):
                        process.read(timeout=.2)
                finally:
                    process.close(grace=0)

    def test_oversized_known_diagnostic_is_rejected(self):
        from tests.backend.test_owned_process import FakeAPI
        raw = b'[llm-deepseek-account] ' + b'x' * (1024 * 1024) + b'\n'
        process = OwnedProcess('fixture.exe', [], '.', 'fixture',
            FakeAPI(chunks=[raw]), line_prefixes=('[llm-deepseek-account] ',)).start()
        try:
            with self.assertRaisesRegex(ProcessError, 'protocol_line_too_large'):
                process.read(timeout=.2)
        finally:
            process.close(grace=0)
