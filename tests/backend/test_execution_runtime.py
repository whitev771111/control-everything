"""Dedicated runtime contracts with synthetic credentials and disposable profiles."""
import copy
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from control_everything.execution import codex
from control_everything.execution.owned_process import OwnedProcess
from control_everything.execution.runtime import build_runtime
from control_everything.execution.service import ExecutionService
from control_everything.input_validation import ProfileError
from control_everything.platform.windows_jobs import environment_block, ProcessError
from tests.backend import test_codex_execution as protocol
from tests.backend import test_dispatch_admission as admission
from tests.backend.test_execution_store import FakeOwner


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.profile = SimpleNamespace(data_dir=self.root/'profile')

    def test_readonly_description_and_explicit_creation(self):
        runtime = build_runtime(self.profile)
        self.assertFalse(runtime.home.exists())
        ready = build_runtime(self.profile, create=True)
        self.assertTrue((ready.home/'sqlite').is_dir())
        self.assertEqual(ready.source_instance, runtime.source_instance)

    def test_environment_filters_credentials_and_overrides_home_without_parent_mutation(self):
        synthetic = {'SystemRoot': 'C:/example/Windows', 'PATH': 'C:/example/Tools',
            'OPENAI_API_KEY': 'synthetic-key', 'CODEX_ACCESS_TOKEN': 'synthetic-token',
            'CODEX_SQLITE_HOME': 'C:/example/other', 'CODEX_HOME': 'C:/example/personal',
            'CODEX_LOG_DIR': 'C:/example/personal/logs', 'UNRELATED_SECRET': 'synthetic-secret'}
        with patch.dict(os.environ, synthetic, clear=True):
            before = dict(os.environ)
            runtime = build_runtime(self.profile)
            self.assertTrue(dict(os.environ) == before, 'Runtime construction mutated the parent environment')
        self.assertEqual(runtime.child_env['CODEX_HOME'], str(runtime.home))
        self.assertEqual(runtime.child_env['CODEX_SQLITE_HOME'], str(runtime.home/'sqlite'))
        self.assertFalse(set(runtime.child_env) & {'OPENAI_API_KEY', 'CODEX_ACCESS_TOKEN', 'UNRELATED_SECRET', 'CODEX_LOG_DIR'})

    def test_environment_block_unicode_duplicates_and_empty_block(self):
        block = ''.join(environment_block({'Path': 'first', 'PATH': 'second', 'Z': '中文'})[:])
        self.assertIn('PATH=second\0', block)
        self.assertNotIn('first', block)
        self.assertIn('Z=中文\0', block)
        self.assertTrue(block.endswith('\0\0'))
        self.assertTrue(''.join(environment_block({})[:]).endswith('\0\0'))
        for bad in ({'A=B': 'bad'}, {'': 'bad'}, {'A': 'bad\0value'}, {'A\0': 'bad'}, {'A': 1}):
            with self.subTest(bad=bad), self.assertRaises(ProcessError):
                environment_block(bad)

    def test_runtime_environment_is_immutable(self):
        runtime = build_runtime(self.profile)
        with self.assertRaises(TypeError):
            runtime.child_env['CODEX_HOME'] = 'C:/example/different-home'

    def test_windows_user_identity_survives_runtime_and_setup_environment_filter(self):
        synthetic = {'Username': 'fixture-user', 'UserDomain': 'FIXTURE-DOMAIN',
                     'OPENAI_API_KEY': 'synthetic-secret', 'CODEX_HOME': 'C:/example/personal'}
        with patch.dict(os.environ, synthetic, clear=True):
            before = dict(os.environ)
            runtime = build_runtime(self.profile)
            spec = runtime.setup_spec('C:/example/codex.exe')
            self.assertEqual(dict(os.environ), before)
        for environment in (runtime.child_env, spec['env']):
            self.assertEqual(environment.get('USERNAME'), 'fixture-user')
            self.assertEqual(environment.get('USERDOMAIN'), 'FIXTURE-DOMAIN')
            self.assertNotIn('OPENAI_API_KEY', environment)
            self.assertEqual(environment['CODEX_HOME'], str(runtime.home))

    def test_login_description_targets_same_home_and_file_credentials(self):
        runtime = build_runtime(self.profile)
        text = ' '.join(runtime.login_argv)
        self.assertIn('cli_auth_credentials_store', text)
        self.assertIn('file', text)

    def test_login_spec_uses_requested_executable_and_independent_environment(self):
        runtime = build_runtime(self.profile)
        spec = runtime.login_spec('C:/example/native-codex.exe')
        self.assertEqual(spec['argv'][0], 'C:/example/native-codex.exe')
        self.assertEqual(spec['env']['CODEX_HOME'], str(runtime.home))
        spec['env']['CODEX_HOME'] = 'C:/example/changed'
        self.assertEqual(runtime.child_env['CODEX_HOME'], str(runtime.home))

    @unittest.skipUnless(sys.platform == 'win32', 'Actual Windows Job verification')
    def test_actual_owned_processes_receive_distinct_environments(self):
        parents = dict(os.environ)
        processes = []
        try:
            for index in range(2):
                runtime = build_runtime(SimpleNamespace(data_dir=self.root/str(index)), create=True)
                code = ("import os,json,sys;print(json.dumps({'home':os.getenv('CODEX_HOME'),"
                        "'leaked':any(k in os.environ for k in ('OPENAI_API_KEY','CODEX_ACCESS_TOKEN'))}),flush=True);"
                        "sys.stdin.readline()")
                process = OwnedProcess(sys.executable, ['-I', '-u', '-c', code], str(self.root),
                    'Local\\workbench_runtime_test.'+uuid4().hex, env=runtime.child_env).start()
                processes.append((process, runtime))
            observed = []
            for process, runtime in processes:
                result = process.read(timeout=5)
                self.assertEqual(result, {'home': str(runtime.home), 'leaked': False})
                observed.append(result['home'])
            self.assertNotEqual(*observed)
        finally:
            cleanup = [process.close(grace=0) for process, _runtime in processes]
        self.assertTrue(all(cleanup), 'Owned process cleanup was not verified')
        self.assertTrue(dict(os.environ) == parents, 'Child launch mutated parent environment')


class RuntimeDispatchTests(unittest.TestCase):
    save_agent = admission.DispatchAdmissionTests.save_agent
    context = admission.DispatchAdmissionTests.context
    update = admission.DispatchAdmissionTests.update
    dump = admission.DispatchAdmissionTests.dump
    admit = admission.DispatchAdmissionTests.admit

    def setUp(self):
        admission.DispatchAdmissionTests.setUp(self)

    def test_manual_confirmed_snapshot_cannot_be_admitted_as_auto(self):
        self.dispatch = self.service.create_task_dispatch(self.item['id'], {
            'agent_id': self.agent['id'], 'directory_id': 'main', 'expected_brief_revision': 1,
            'confirmed': True, 'delivery_mode': 'manual'})['dispatch']
        self.body['context_hash'] = self.context()['context_hash']
        before = self.dump()
        with self.assertRaises(ProfileError) as caught:
            self.admit()
        self.assertEqual(caught.exception.reason, 'execution_mode_conflict')
        self.assertEqual(self.dump(), before)

    def test_auto_snapshot_is_distinct_and_contains_no_manual_claim_instructions(self):
        manual = self.service.create_task_dispatch(self.item['id'], {
            'agent_id': self.agent['id'], 'directory_id': 'main', 'expected_brief_revision': 1,
            'confirmed': True, 'delivery_mode': 'manual'})['dispatch']
        auto = self.service.create_task_dispatch(self.item['id'], {
            'agent_id': self.agent['id'], 'directory_id': 'main', 'expected_brief_revision': 1,
            'confirmed': True, 'delivery_mode': 'auto'})['dispatch']
        self.assertNotEqual(auto['id'], manual['id'])
        self.assertNotEqual(auto['brief_sha256'], manual['brief_sha256'])
        self.assertNotIn('task claim', auto['content_text'])
        self.assertNotIn('WORKBENCH_TOKEN', auto['content_text'])
        from control_everything.execution.service import dispatch_execution_context
        body = {'confirmed': True, 'context_hash': dispatch_execution_context(self.service, auto['id'])['context_hash']}
        self.assertFalse(self.admit(dispatch_id=auto['id'], body=body)['replayed'])

    def test_arbitrary_registered_command_is_rejected_before_execution(self):
        self.save_agent(command='unrelated-custom-program')
        with self.assertRaises(ProfileError) as caught:
            self.admit()
        self.assertEqual(caught.exception.reason, 'execution_unsupported')

    def test_preparation_rejects_unsupported_agent_configuration(self):
        for changes in ({'command': 'unsupported-program', 'connection': {}},
                        {'command': 'codex', 'connection': {'args': ['unsupported']}}):
            with self.subTest(changes=changes):
                self.save_agent(**changes)
                with self.assertRaises(ProfileError) as caught:
                    self.context()
                self.assertEqual(caught.exception.reason, 'execution_unsupported')

    def test_runtime_recheck_rejects_unsupported_agent_configuration(self):
        service = ExecutionService(self.service, store=self.ledger, worker_factory=lambda **_: None,
            owner_factory=lambda _: FakeOwner(None), resolver=lambda: 'synthetic.exe',
            job_query=lambda _: True, platform='win32')
        self.addCleanup(service.close)
        result = self.admit()
        self.assertTrue(service.validate_root(result['run']))
        for changes in ({'command': 'unsupported-program', 'connection': {}},
                        {'command': 'codex', 'connection': {'args': ['unsupported']}}):
            with self.subTest(changes=changes):
                self.save_agent(**changes)
                self.assertFalse(service.validate_root(result['run']),
                                 'A later unsupported configuration must fail before creating a turn')


class RuntimeProtocolTests(unittest.TestCase):
    worker = protocol.CodexExecutionTests.worker
    data = protocol.CodexExecutionTests.data

    def setUp(self):
        protocol.CodexExecutionTests.setUp(self)
        self.addCleanup(self.temp.cleanup)

    def run_with_account(self, account, *, unsafe_config=False, credential_store='file'):
        worker, transport = self.worker()
        runtime = build_runtime(SimpleNamespace(data_dir=self.root/'profile'), create=True)
        worker.runtime = runtime
        original_write = transport.write
        def write(value):
            if value.get('method') == 'account/read':
                transport.sent.append(copy.deepcopy(value))
                transport.messages.append({'id': value['id'], 'result': {'account': account, 'requiresOpenaiAuth': True}})
            else:
                original_write(value)
        transport.write = write
        changed = protocol.config()
        changed['config'].update(cli_auth_credentials_store=credential_store, mcp_servers={})
        changed['config'].setdefault('windows', {'sandbox': runtime.windows_sandbox_mode})
        if unsafe_config:
            changed['config']['mcp_servers'] = {'unexpected': {'enabled': True, 'command': 'synthetic'}}
        transport.changes['config/read'] = changed
        launch = {}
        def factory(exe, args, cwd, name, **kwargs):
            launch.update(args=args, env=kwargs.get('env'))
            return transport
        worker.process_factory = factory
        worker.run()
        return worker, transport, launch, runtime

    def test_no_account_fails_before_thread_or_turn(self):
        worker, transport, _launch, _runtime = self.run_with_account(None)
        row = self.data(worker)['run']
        self.assertEqual((row['state'], row['reason'], row['cleanup_state']), ('failed', 'login_required', 'verified'))
        self.assertNotIn('thread/start', [row.get('method') for row in transport.sent])

    def test_account_check_order_and_no_account_details_persisted(self):
        worker, transport, launch, runtime = self.run_with_account({'type': 'chatgpt', 'email': 'synthetic@example.invalid'})
        methods = [row.get('method') for row in transport.sent]
        self.assertLess(methods.index('account/read'), methods.index('thread/start'))
        self.assertEqual(launch['env']['CODEX_HOME'], str(runtime.home))
        self.assertNotIn('synthetic@example.invalid', json.dumps(self.data(worker)))

    def test_actual_launch_pins_file_credential_store(self):
        _worker, _transport, launch, _runtime = self.run_with_account({'type': 'chatgpt'})
        self.assertTrue(any('cli_auth_credentials_store' in arg and 'file' in arg for arg in launch['args']),
                        'Runtime must enforce file credentials, not just describe them')

    def test_unexpected_mcp_configuration_is_rejected_before_turn(self):
        worker, transport, _launch, _runtime = self.run_with_account({'type': 'chatgpt'}, unsafe_config=True)
        self.assertNotIn('turn/start', [row.get('method') for row in transport.sent])
        self.assertEqual(self.data(worker)['run']['reason'], 'effective_policy_mismatch')

    def test_effective_runtime_source_is_persisted_with_execution(self):
        worker, _transport, _launch, runtime = self.run_with_account({'type': 'chatgpt'})
        effective = self.data(worker)['run']['effective_policy']
        self.assertEqual(effective.get('runtime', {}).get('source_instance'), runtime.source_instance)

    def test_effective_keyring_store_is_rejected_before_authentication(self):
        worker, transport, _launch, _runtime = self.run_with_account({'type': 'chatgpt'}, credential_store='keyring')
        self.assertNotIn('account/read', [row.get('method') for row in transport.sent])
        self.assertNotIn('turn/start', [row.get('method') for row in transport.sent])
        self.assertEqual(self.data(worker)['run']['reason'], 'effective_policy_mismatch')


if __name__ == '__main__':
    unittest.main()
