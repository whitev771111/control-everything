"""Permission snapshot, admission, and runtime isolation contracts for stage A."""
import json
import re
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from control_everything.execution.dsh_runtime import build_dsh_runtime
from control_everything.execution.codex import CodexRun
from control_everything.execution.service import ExecutionService
from control_everything.input_validation import ProfileError
from tests.backend import test_dsh_execution as fixtures
from tests.backend.test_codex_execution import MemoryTransport
from tests.backend.test_execution_store import FakeOwner, HoldingWorker


class RuntimeArtifactTests(unittest.TestCase):
    def test_native_model_configuration_is_not_overridden_by_permission_patch(self):
        with tempfile.TemporaryDirectory() as root:
            profile = SimpleNamespace(data_dir=Path(root))
            for permission in ('read-only', 'workspace-write', 'danger-full-access'):
                for approval in ('request_user', 'deny', 'never'):
                    with self.subTest(permission=permission, approval=approval):
                        runtime = build_dsh_runtime(profile, create=True,
                                                    permission=permission, approval=approval)
                        content = runtime.overlay_path.read_text(encoding='utf8')
                        self.assertEqual(re.findall(r'^- id: (.+)$', content, re.MULTILINE),
                                         ['sandbox-policy', 'approval'])
                        self.assertIn('mode: '+permission, content)
                        policy = 'ask' if permission != 'danger-full-access' and approval == 'request_user' else 'never'
                        self.assertIn('policy: '+policy, content)
                        public = runtime.public()
                        self.assertEqual(set(public), {'auth_mode', 'provider', 'model', 'overlay_sha256',
                                                      'home_identity', 'permission', 'network',
                                                      'approval', 'policy_version'})
                        self.assertEqual((public['auth_mode'], public['provider'], public['model'],
                                          public['policy_version']),
                                         ('native-config', None, None, 'dsh-headless-v3'))

    def test_preparing_another_permission_cannot_change_an_existing_overlay(self):
        with tempfile.TemporaryDirectory() as root:
            profile = SimpleNamespace(data_dir=Path(root))
            readonly = build_dsh_runtime(profile, create=True, permission='read-only')
            expected = readonly.overlay_path.read_bytes()
            build_dsh_runtime(profile, create=True, permission='danger-full-access')
            self.assertEqual(readonly.overlay_path.read_bytes(), expected,
                             'Concurrent dispatches must not replace a confirmed permission artifact')

    def test_tampered_overlay_is_rejected_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as root:
            profile = SimpleNamespace(data_dir=Path(root))
            runtime = build_dsh_runtime(profile, create=True)
            runtime.overlay_path.write_text('invalid fixture overlay', encoding='utf8')
            with self.assertRaisesRegex(ValueError, 'overlay_integrity_conflict'):
                build_dsh_runtime(profile, create=True)
            self.assertEqual(runtime.overlay_path.read_text(encoding='utf8'), 'invalid fixture overlay')

    def test_concurrent_permission_artifacts_stay_distinct_and_complete(self):
        with tempfile.TemporaryDirectory() as root:
            profile = SimpleNamespace(data_dir=Path(root))
            modes = ['read-only'] * 2 + ['workspace-write'] * 2 + ['danger-full-access'] * 2
            with ThreadPoolExecutor(max_workers=2) as pool:
                runtimes = list(pool.map(lambda mode: build_dsh_runtime(profile, create=True, permission=mode), modes))
            self.assertEqual(len({runtime.overlay_path for runtime in runtimes}), 3,
                             [(mode, str(runtime.overlay_path)) for mode, runtime in zip(modes, runtimes)])
            for mode, runtime in zip(modes, runtimes):
                self.assertIn('mode: '+mode, runtime.overlay_path.read_text(encoding='utf8'))
            self.assertFalse(list((Path(root)/'agent-runtime'/'dsh').glob('*.tmp')))


class PermissionDispatchTests(unittest.TestCase):
    setUp = fixtures.DshDispatchTests.setUp
    save_agent = fixtures.DshDispatchTests.save_agent
    context = fixtures.DshDispatchTests.context
    update = fixtures.DshDispatchTests.update
    dump = fixtures.DshDispatchTests.dump
    admit = fixtures.DshDispatchTests.admit

    def execution(self):
        service = ExecutionService(self.service, store=self.ledger, owner_factory=lambda _: FakeOwner(None),
            job_query=lambda _: True, platform='win32', codex_required=False, dsh_worker_factory=HoldingWorker)
        self.addCleanup(service.close)
        return service

    def test_readonly_permission_is_visible_and_bound_to_runtime(self):
        self.save_agent(permission='read-only')
        context = self.context()
        receipt = self.admit(body={'confirmed': True, 'context_hash': context['context_hash']})
        self.assertEqual(context['policy']['mode'], 'read-only')
        self.assertEqual(json.loads(receipt['run']['effective_policy_json'])['dsh_runtime']['permission'], 'read-only')

    def test_permission_change_before_admission_rejects_old_hash(self):
        self.save_agent(permission='read-only')
        with self.assertRaises(ProfileError) as error:
            self.admit()
        self.assertEqual(error.exception.reason, 'context_changed')

    def test_dsh_permission_change_after_admission_invalidates_execution(self):
        receipt = self.admit()
        service = self.execution()
        self.save_agent(permission='read-only')
        self.assertFalse(service.validate_root(receipt['run']))

    def test_codex_permission_change_after_admission_invalidates_execution(self):
        self.save_agent(adapter='codex', kind='codex', command='codex', network='denied')
        receipt = self.admit(body={'confirmed': True, 'context_hash': self.context()['context_hash']})
        service = self.execution()
        self.save_agent(permission='read-only')
        self.assertFalse(service.validate_root(receipt['run']))

    def test_codex_network_or_approval_change_after_admission_invalidates_execution(self):
        self.save_agent(adapter='codex', kind='codex', command='codex', network='denied')
        run = self.admit(body={'confirmed': True, 'context_hash': self.context()['context_hash']})['run']
        service = self.execution()
        self.assertTrue(service.validate_root(run))
        for changes in ({'network': 'allowed'}, {'approval': 'deny'}):
            with self.subTest(changes=changes):
                self.save_agent(**changes)
                self.assertFalse(service.validate_root(run))
                self.save_agent(network='denied', approval='request_user')
                self.assertTrue(service.validate_root(run))

    def codex_worker(self):
        self.save_agent(adapter='codex', kind='codex', command='codex', network='denied')
        run = self.admit(body={'confirmed': True, 'context_hash': self.context()['context_hash']})['run']
        service = self.execution()
        transport = MemoryTransport(run['cwd'])
        worker = CodexRun(self.ledger, run, run['owner_id'], service.validate_root,
                          process_factory=lambda *_args, **_kwargs: transport, resolver=lambda: sys.executable)
        return run, worker, transport

    def test_codex_handshake_preserves_confirmed_policy_snapshot(self):
        run, worker, _ = self.codex_worker()
        expected = json.loads(run['effective_policy_json'])
        worker.run()
        actual = json.loads(self.ledger.get_run(run['id'])['effective_policy_json'])
        self.assertEqual(actual.get('policy'), expected['policy'])
        self.assertEqual(actual.get('agent_policy'), expected['agent_policy'])
        self.assertEqual(actual.get('confirmation_version'), expected['confirmation_version'])
        self.assertEqual(actual.get('adapter'), expected['adapter'])

    def test_new_run_missing_confirmation_fields_is_rejected(self):
        self.save_agent(adapter='codex', kind='codex', command='codex', network='denied')
        run = self.admit(body={'confirmed': True, 'context_hash': self.context()['context_hash']})['run']
        service = self.execution()
        original = json.loads(run['effective_policy_json'])
        self.assertEqual(original['confirmation_version'], 1)
        for key in ('policy', 'agent_policy'):
            with self.subTest(key=key):
                incomplete = dict(original)
                incomplete.pop(key)
                self.ledger.update_run(run['id'], effective_policy_json=json.dumps(incomplete))
                self.assertFalse(service.validate_root(run))
        self.ledger.update_run(run['id'], effective_policy_json=json.dumps(original))
        self.assertTrue(service.validate_root(run))

    def test_codex_permission_change_during_handshake_blocks_prompt(self):
        _, worker, transport = self.codex_worker()
        original_write = transport.write
        def write(message):
            original_write(message)
            if message.get('method') == 'thread/start':
                self.save_agent(permission='read-only')
        transport.write = write
        worker.run()
        self.assertFalse(any(message.get('method') == 'turn/start' for message in transport.sent),
                         'Permission changes before turn/start must not be hidden by handshake policy updates')

    def test_complete_effective_policy_is_persisted(self):
        context = self.context()
        receipt = self.admit()
        stored = json.loads(receipt['run']['effective_policy_json'])
        self.assertEqual(stored.get('policy'), context['policy'])
        self.assertEqual(stored['adapter'], 'dsh')

    def test_codex_deny_is_enforced_or_explicitly_unsupported(self):
        self.save_agent(adapter='codex', kind='codex', command='codex', network='denied', approval='deny')
        try:
            context = self.context()
        except ProfileError as error:
            self.assertEqual(error.reason, 'execution_unsupported')
        else:
            self.assertEqual(context['policy']['approval_policy'], 'never')

    def test_codex_unsupported_network_and_full_access_do_not_downgrade(self):
        for changes in ({'network':'allowed'}, {'network':'denied','permission':'danger-full-access'}):
            with self.subTest(changes=changes):
                self.save_agent(adapter='codex', kind='codex', command='codex', **changes)
                with self.assertRaises(ProfileError) as error:
                    self.context()
                self.assertEqual(error.exception.reason, 'execution_unsupported')

    def test_dsh_network_deny_is_explicitly_unsupported(self):
        self.save_agent(network='denied')
        with self.assertRaises(ProfileError) as error:
            self.context()
        self.assertEqual(error.exception.reason, 'execution_unsupported')

    def test_dispatch_text_interpolates_the_selected_agent(self):
        self.assertIn('Agent: '+self.agent['name'], self.dispatch['content_text'])
        self.assertNotIn("{payload['agent_name']}", self.dispatch['content_text'])

    def test_changing_other_settings_preserves_permission(self):
        self.save_agent(permission='read-only', approval='deny')
        self.save_agent(name='Renamed fixture')
        row = self.profile.agent_settings()['items'][0]
        self.assertEqual((row.get('permission'), row.get('approval')), ('read-only', 'deny'))

    def test_public_agent_settings_return_the_persisted_permissions(self):
        self.save_agent(permission='read-only', network='allowed', approval='deny')
        saved = self.profile.config()['agents']['items'][0]
        public = self.profile.agent_settings()['items'][0]
        for key in ('permission', 'network', 'approval'):
            with self.subTest(key=key):
                self.assertEqual(public.get(key), saved[key])
