"""Confirmed worker policy and pre-process failure regressions on disposable profiles."""

import os
import sys
import unittest
from unittest.mock import patch

from control_everything.execution import service as execution
from control_everything.execution.codex import CodexRun
from control_everything.execution.store import ExecutionStore
from control_everything.input_validation import ProfileError
from control_everything.workspace.store import WorkspaceError
from tests.backend import test_codex_execution as protocol
from tests.backend import test_dispatch_admission as admission
from tests.backend import test_dispatch_execution_context as fixtures
from tests.backend.test_execution_store import FakeOwner


class EchoPolicyTransport(protocol.MemoryTransport):
    def write(self, value):
        if value.get('method') == 'thread/start':
            self.mode = value['params']['sandbox']
        super().write(value)


class UnstartedWorker:
    def __init__(self, process=None):
        self.process = process
        self.ran = self.closed = False

    def run(self):
        self.ran = True
        raise RuntimeError('synthetic worker failure')

    def close(self):
        self.closed = True


class ExecutionAdmissionFailureTests(unittest.TestCase):
    save_agent = fixtures.DispatchExecutionContextTests.save_agent
    context = fixtures.DispatchExecutionContextTests.context
    make_dispatch = admission.DispatchAdmissionTests.make_dispatch

    def setUp(self):
        fixtures.DispatchExecutionContextTests.setUp(self)
        self.ledger = ExecutionStore(self.service.store)
        environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.root/'synthetic-localappdata')})
        environment.start()
        self.addCleanup(environment.stop)

    def execution_service(self, **kwargs):
        service = execution.ExecutionService(
            self.service, store=self.ledger, owner_factory=lambda _: FakeOwner(None),
            job_query=lambda _: True, platform='win32', codex_required=False, **kwargs)
        self.addCleanup(service.close)
        return service

    def admit(self, service):
        return execution.admit_dispatch_execution(
            self.service, self.ledger, self.dispatch['id'],
            {'context_hash': self.context()['context_hash'], 'confirmed': True},
            owner_id=service.owner_id, require_owner=service._require_owner)

    def assert_worker_policy(self, permission):
        self.save_agent(permission=permission)
        context = self.context()
        workers, transports = [], []

        def factory(**kwargs):
            transport = EchoPolicyTransport(kwargs['run']['cwd'])
            kwargs['resolver'] = lambda: sys.executable
            worker = CodexRun(**kwargs, process_factory=lambda *_args, **_kwargs: transport)
            workers.append(worker)
            transports.append(transport)
            return worker

        service = self.execution_service(worker_factory=factory)
        receipt = service.submit_dispatch(self.dispatch['id'], {
            'context_hash': context['context_hash'], 'confirmed': True})
        self.assertTrue(workers[0].finished.wait(4), 'Synthetic worker did not finish')
        row = self.ledger.get_run(receipt['execution_run_id'])
        requests = {message.get('method'): message for message in transports[0].sent}
        self.assertEqual(context['policy']['mode'], permission)
        self.assertEqual(row['mode'], permission)
        self.assertEqual(requests['thread/start']['params']['sandbox'], permission)
        self.assertEqual(requests['turn/start']['params']['sandboxPolicy'],
                         protocol.sandbox_policy(permission))
        self.assertEqual(row['state'], 'completed')

    def test_confirmed_read_only_reaches_native_thread_and_turn(self):
        self.assert_worker_policy('read-only')

    def test_confirmed_workspace_write_reaches_native_thread_and_turn(self):
        self.assert_worker_policy('workspace-write')

    def test_permission_expansion_after_confirmation_is_rejected_before_worker(self):
        self.save_agent(permission='read-only')
        context = self.context()
        self.save_agent(permission='workspace-write')
        service = self.execution_service(worker_factory=lambda **_: self.fail('Worker must not start'))
        with self.assertRaises(ProfileError) as caught:
            service.submit_dispatch(self.dispatch['id'], {
                'context_hash': context['context_hash'], 'confirmed': True})
        self.assertEqual(caught.exception.reason, 'context_changed')
        self.assertIsNone(self.ledger.find_request(self.dispatch['id']))

    def assert_failed_before_process_releases_claim(self, service, admitted, worker):
        row = self.ledger.get_run(admitted['run']['id'])
        self.assertFalse(worker.ran)
        self.assertTrue(worker.closed)
        self.assertIsNone(worker.process)
        self.assertEqual((row['state'], row['cleanup_state']), ('failed', 'verified'))
        with self.profile.store.connection() as connection:
            projected = connection.execute('SELECT status FROM agent_runs WHERE id=?',
                                           (admitted['agent_run_id'],)).fetchone()
        self.assertEqual(projected['status'], 'failed')
        dispatch, body = self.make_dispatch(self.project)
        retry = execution.admit_dispatch_execution(
            self.service, self.ledger, dispatch['id'], body,
            owner_id=service.owner_id, require_owner=service._require_owner)
        self.assertFalse(retry['replayed'])
        self.assertEqual(retry['run']['cwd'], row['cwd'])

    def test_real_directory_lease_failure_releases_pending_claim_for_retry(self):
        service = self.execution_service()
        admitted = self.admit(service)
        worker = UnstartedWorker()
        with execution.directory_lock(admitted['run']['cwd']):
            service._run_worker(admitted['run']['id'], worker)
        self.assert_failed_before_process_releases_claim(service, admitted, worker)

    def test_root_revalidation_failure_releases_pending_claim_for_retry(self):
        service = self.execution_service()
        admitted = self.admit(service)
        worker = UnstartedWorker()
        with patch.object(service, 'validate_root', return_value=False):
            service._run_worker(admitted['run']['id'], worker)
        self.assert_failed_before_process_releases_claim(service, admitted, worker)

    def test_process_with_unverified_cleanup_keeps_claim_and_directory_lease(self):
        service = self.execution_service()
        admitted = self.admit(service)
        worker = UnstartedWorker(process=object())
        run_id = admitted['run']['id']
        try:
            service._run_worker(run_id, worker)
            row = self.ledger.get_run(run_id)
            self.assertTrue(worker.ran)
            self.assertNotEqual(row['cleanup_state'], 'verified')
            self.assertIn(run_id, execution._UNVERIFIED_LEASES)
            with self.assertRaisesRegex(OSError, 'sandbox_busy'):
                with execution.directory_lock(row['cwd']):
                    pass
            with self.profile.store.connection() as connection:
                projected = connection.execute('SELECT status FROM agent_runs WHERE id=?',
                                               (admitted['agent_run_id'],)).fetchone()
            self.assertIn(projected['status'], ('claimed', 'running'))
            dispatch, body = self.make_dispatch(self.project)
            with self.assertRaisesRegex(WorkspaceError, 'project_busy'):
                execution.admit_dispatch_execution(
                    self.service, self.ledger, dispatch['id'], body,
                    owner_id=service.owner_id, require_owner=service._require_owner)
        finally:
            lease = execution._UNVERIFIED_LEASES.pop(run_id, None)
            if lease is not None:
                lease.close()


if __name__ == '__main__':
    unittest.main()
