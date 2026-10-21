"""Transactional dispatch admission over real disposable SQLite connections."""
import threading
import time
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import Mock, patch

from control_everything.execution.service import (
    admit_dispatch_execution, dispatch_execution_context, _bounded_replay_after_config_busy,
)
from control_everything.execution.store import ExecutionStore
from control_everything.input_validation import ProfileError
from control_everything.profiles.profile import Profile
from control_everything.workspace.service import WorkspaceService
from control_everything.workspace.store import WorkspaceError
from tests.backend import test_dispatch_execution_context as fixtures
from tests.backend.test_project_management import project_body


class DispatchAdmissionTests(unittest.TestCase):
    save_agent = fixtures.DispatchExecutionContextTests.save_agent
    context = fixtures.DispatchExecutionContextTests.context
    update = fixtures.DispatchExecutionContextTests.update
    dump = fixtures.DispatchExecutionContextTests.dump

    def setUp(self):
        fixtures.DispatchExecutionContextTests.setUp(self)
        self.ledger = ExecutionStore(self.service.store)
        self.body = {'confirmed': True, 'context_hash': self.context()['context_hash']}
        self.owner = Mock()

    def admit(self, body=None, workspace=None, ledger=None, dispatch_id=None, owner=None):
        return admit_dispatch_execution(workspace or self.service, ledger or self.ledger,
            dispatch_id or self.dispatch['id'], self.body if body is None else body,
            owner_id='synthetic-owner', require_owner=self.owner if owner is None else owner)

    def test_atomic_binding_replay_and_no_worker(self):
        before = self.profile.store.get_item(self.item['id'])
        with patch('control_everything.execution.service.ExecutionService', side_effect=AssertionError('No worker')):
            result = self.admit()
        self.assertFalse(result['replayed'])
        self.assertEqual(result['run']['prompt'], self.dispatch['content_text'])
        self.assertEqual(result['run']['request_id'], self.dispatch['id'])
        self.assertEqual((result['run']['state'], result['run']['cleanup_state']), ('starting', 'pending'))
        after = self.profile.store.get_item(self.item['id'])
        self.assertEqual(after['state'], 'doing')
        self.assertEqual(after['revision'], before['revision']+1)
        dispatch = self.service.task_dispatch(self.dispatch['id'])
        self.assertEqual((dispatch['delivery_mode'], dispatch['delivery_state']), ('auto', 'sent'))
        self.assertEqual(dispatch['external_ref'], result['run']['id'])
        self.assertEqual(dispatch['run_id'], result['agent_run_id'])
        stable = self.dump()
        replay = self.admit(owner=Mock(side_effect=AssertionError('Replay must ignore owner availability')))
        self.assertTrue(replay['replayed'])
        self.assertEqual(result['run']['id'], replay['run']['id'])
        self.assertEqual(self.dump(), stable)

    def test_replay_ignores_mutable_agent_and_task(self):
        result = self.admit()
        self.save_agent(enabled=False)
        self.update('task_items', self.item['id'], state='done', assigned_agent_id=None)
        self.assertEqual(self.admit()['run']['id'], result['run']['id'])
        self.profile.config_path.write_text('invalid synthetic config', encoding='utf8')
        self.assertEqual(self.admit()['run']['id'], result['run']['id'])

    def test_replay_survives_delivery_and_terminal_state_changes(self):
        result = self.admit()
        for state in ['delivered', 'unknown', 'failed', 'cancelled']:
            with self.subTest(state=state):
                self.update('task_dispatches', self.dispatch['id'], delivery_state=state)
                self.assertEqual(self.admit()['run']['id'], result['run']['id'])

    def test_conflicting_hash_and_broken_binding_never_create_another_run(self):
        self.admit()
        stable = self.dump()
        with self.assertRaises(ProfileError) as caught:
            self.admit(body={**self.body, 'context_hash': '0'*64})
        self.assertEqual(caught.exception.reason, 'idempotency_conflict')
        self.assertEqual(self.dump(), stable)
        self.update('task_dispatches', self.dispatch['id'], external_ref='broken')
        broken = self.dump()
        with self.assertRaises(ProfileError) as caught:
            self.admit()
        self.assertEqual(caught.exception.reason, 'execution_link_conflict')
        self.assertEqual(self.dump(), broken)

    def test_invalid_body_unavailable_owner_and_wrong_store_are_readonly(self):
        stable = self.dump()
        for body in [{}, {**self.body, 'prompt': 'override'}, {**self.body, 'confirmed': 1},
                     {**self.body, 'confirmed': False}, {**self.body, 'context_hash': 'bad'}]:
            with self.subTest(body=body), self.assertRaises(ProfileError):
                self.admit(body=body)
        with self.assertRaises(WorkspaceError):
            self.admit(owner=Mock(side_effect=WorkspaceError('execution_unavailable')))
        peer = WorkspaceService(profile=Profile.open(str(self.root/'other-profile')))
        with self.assertRaises(ProfileError) as caught:
            self.admit(ledger=ExecutionStore(peer.store))
        self.assertEqual(caught.exception.reason, 'execution_link_conflict')
        self.assertEqual(self.dump(), stable)

    def test_each_write_failure_rolls_back_every_table(self):
        for target in ['execution', 'claim', 'item', 'event']:
            with self.subTest(target=target):
                stable = self.dump()
                if target == 'execution':
                    original = self.ledger.insert_dispatch_run
                    def fail(*args, **kwargs):
                        original(*args, **kwargs)
                        raise RuntimeError('synthetic write failure')
                    patcher = patch.object(self.ledger, 'insert_dispatch_run', side_effect=fail)
                elif target == 'claim':
                    original = self.service.claim_dispatch_in_transaction
                    def fail(*args, **kwargs):
                        original(*args, **kwargs)
                        raise RuntimeError('synthetic write failure')
                    patcher = patch.object(self.service, 'claim_dispatch_in_transaction', side_effect=fail)
                elif target == 'item':
                    patcher = patch.object(self.profile.store, '_update_item', side_effect=RuntimeError('synthetic write failure'))
                else:
                    patcher = patch('control_everything.workspace.agent_runs._event', side_effect=RuntimeError('synthetic write failure'))
                with patcher, self.assertRaises(RuntimeError):
                    self.admit()
                self.assertEqual(self.dump(), stable)

    def test_commit_failure_is_not_returned_as_success(self):
        stable = self.dump()
        original = self.service.claim_dispatch_in_transaction
        def deferred_failure(conn, **kwargs):
            result = original(conn, **kwargs)
            conn.execute('PRAGMA defer_foreign_keys=ON')
            conn.execute("INSERT INTO task_reviews VALUES('bad','missing',NULL,'approved','Synthetic','[]','human','stamp')")
            return result
        with patch.object(self.service, 'claim_dispatch_in_transaction', side_effect=deferred_failure):
            with self.assertRaises(WorkspaceError):
                self.admit()
        self.assertEqual(self.dump(), stable)

    def test_two_real_connections_admit_same_request_once(self):
        peer = WorkspaceService(profile=Profile.open(str(self.profile.data_dir)))
        barrier = threading.Barrier(2)
        def call(workspace):
            barrier.wait(timeout=5)
            try:
                return self.admit(workspace=workspace, ledger=ExecutionStore(workspace.store))
            except ProfileError as exc:
                return {'failure': exc.reason}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(call, [self.service, peer]))
        failures = [row for row in results if 'failure' in row]
        admitted = [row for row in results if 'run' in row]
        if failures:
            recovered = self.admit()
            self.assertEqual(recovered['run']['id'], admitted[0]['run']['id'])
        self.assertFalse(failures, failures)
        self.assertEqual(len({row['run']['id'] for row in results}), 1)
        self.assertEqual(len({row['agent_run_id'] for row in results}), 1)
        self.assertEqual(sorted(row['replayed'] for row in results), [False, True])
        with self.profile.store.connection() as conn:
            for table in ['execution_runs', 'agent_runs', 'task_events']:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 1)

    def make_dispatch(self, project):
        task = self.profile.store.save_item({'title': 'Synthetic extra', 'project_id': project['id'],
            'provider_id': None, 'state': 'todo', 'session_key': None})
        self.service.save_task_brief(task['id'], {'expected_revision': 0, 'request_text': 'Synthetic work',
            'acceptance_text': '', 'constraints_text': '', 'references': [], 'execution_directory_id': 'main'})
        dispatch = self.service.create_task_dispatch(task['id'], {'agent_id': self.agent['id'],
            'directory_id': 'main', 'expected_brief_revision': 1, 'confirmed': True, 'delivery_mode': 'auto'})['dispatch']
        context = dispatch_execution_context(self.service, dispatch['id'])
        return dispatch, {'context_hash': context['context_hash'], 'confirmed': True}

    def test_overlapping_roots_and_capacity_rollback(self):
        overlap, overlap_body = self.make_dispatch(self.project)
        candidates = []
        for index in range(2):
            root = self.root/('independent-'+str(index)); root.mkdir()
            project = self.profile.store.save_project(project_body(root))
            candidates.append(self.make_dispatch(project))
        self.admit()
        stable = self.dump()
        with self.assertRaisesRegex(WorkspaceError, 'project_busy'):
            self.admit(dispatch_id=overlap['id'], body=overlap_body)
        self.assertEqual(self.dump(), stable)
        self.admit(dispatch_id=candidates[0][0]['id'], body=candidates[0][1])
        stable = self.dump()
        with self.assertRaisesRegex(WorkspaceError, 'capacity_busy'):
            self.admit(dispatch_id=candidates[1][0]['id'], body=candidates[1][1])
        self.assertEqual(self.dump(), stable)

    def test_stale_configuration_is_rejected_without_partial_rows(self):
        # Use a supported configuration change; nonempty connections are rejected by B2.
        self.save_agent(project_ids=[self.project['id']])
        stable = self.dump()
        with self.assertRaises(ProfileError) as caught:
            self.admit()
        self.assertEqual(caught.exception.reason, 'context_changed')
        self.assertEqual(self.dump(), stable)

    def test_unrelated_config_lock_times_out_without_new_admission(self):
        peer = Profile.open(str(self.profile.data_dir))
        stable = self.dump()
        with peer.agent_settings_guard():
            started = time.monotonic()
            with self.assertRaises(ProfileError) as caught:
                self.admit()
        elapsed = time.monotonic()-started
        self.assertEqual(caught.exception.reason, 'config_busy')
        self.assertGreaterEqual(elapsed, 1.9)
        self.assertLess(elapsed, 3)
        self.owner.assert_not_called()
        self.assertEqual(self.dump(), stable)

    def test_receipt_deadline_includes_sqlite_lock_wait(self):
        block = sqlite3.connect(self.profile.store.path)
        block.execute('BEGIN EXCLUSIVE')
        started = threading.Event()
        result = {}
        def read():
            begin = time.monotonic(); started.set()
            try:
                _bounded_replay_after_config_busy(self.service, self.ledger, self.dispatch['id'],
                    '0'*64, ProfileError('config_busy', status=409))
            except Exception as exc:
                result['reason'] = getattr(exc, 'reason', str(exc))
            finally:
                result['elapsed'] = time.monotonic()-begin
        thread = threading.Thread(target=read, daemon=True)
        try:
            thread.start(); self.assertTrue(started.wait(1))
            time.sleep(2.7)
            waiting = thread.is_alive()
        finally:
            block.rollback(); block.close(); thread.join(6)
        self.assertFalse(waiting, 'The 2s receipt budget must include SQLite lock waits')
        self.assertEqual(result['reason'], 'config_busy')
        self.assertLess(result['elapsed'], 2.5)

    def test_receipt_connection_is_readonly_and_closed(self):
        stable = self.dump()
        with self.ledger.receipt_connection(deadline=time.monotonic()+2) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('UPDATE task_center_meta SET list_revision=list_revision+1')
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')
        self.assertEqual(self.dump(), stable)

    def test_receipt_storage_error_is_not_hidden_as_config_busy(self):
        with patch.object(self.ledger, 'receipt_connection',
                          side_effect=sqlite3.OperationalError('synthetic storage failure')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'synthetic storage failure'):
                _bounded_replay_after_config_busy(self.service, self.ledger, self.dispatch['id'],
                    '0'*64, ProfileError('config_busy', status=409))

    def test_receipt_connection_closes_after_schema_validation_error(self):
        opened = []
        real_connect = sqlite3.connect
        def connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn
        with patch('control_everything.execution.store.sqlite3.connect', side_effect=connect), patch.object(
                self.profile.store, 'validate_schema', side_effect=WorkspaceError('schema_error')):
            with self.assertRaisesRegex(WorkspaceError, 'schema_error'):
                with self.ledger.receipt_connection(deadline=time.monotonic()+2):
                    self.fail('Invalid schema must never be yielded')
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute('SELECT 1')

    def test_two_dispatches_for_one_task_never_create_two_active_runs(self):
        self.service.save_task_brief(self.item['id'], {'expected_revision': 1,
            'request_text': 'Different confirmed request', 'acceptance_text': '',
            'constraints_text': '', 'references': [], 'execution_directory_id': 'main'})
        other = self.service.create_task_dispatch(self.item['id'], {'agent_id': self.agent['id'],
            'directory_id': 'main', 'expected_brief_revision': 2, 'confirmed': True, 'delivery_mode': 'auto'})['dispatch']
        body = {'confirmed': True, 'context_hash': dispatch_execution_context(self.service, other['id'])['context_hash']}
        peer = WorkspaceService(profile=Profile.open(str(self.profile.data_dir)))
        barrier = threading.Barrier(2)
        def call(args):
            workspace, identity, request = args
            barrier.wait(timeout=5)
            try:
                return self.admit(workspace=workspace, ledger=ExecutionStore(workspace.store),
                                  dispatch_id=identity, body=request)
            except (ProfileError, WorkspaceError) as exc:
                return {'failure': exc.reason if isinstance(exc, ProfileError) else str(exc)}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(call, [(self.service, self.dispatch['id'], self.body), (peer, other['id'], body)]))
        self.assertEqual(sum('run' in row for row in results), 1)
        with self.profile.store.connection() as conn:
            for table in ['execution_runs', 'agent_runs', 'task_events']:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 1)

    def test_get_context_and_admission_have_compatible_lock_order(self):
        # Bound the real lock wait so a regression fails instead of hanging the suite.
        class BoundedLock:
            def __init__(self): self.lock = threading.RLock()
            def __enter__(self):
                if not self.lock.acquire(timeout=1): raise RuntimeError('lock order timeout')
                return self
            def __exit__(self, *args): self.lock.release()
        held, proceed, reader_needs_config = threading.Event(), threading.Event(), threading.Event()
        guard = self.profile.agent_settings_guard
        settings = self.profile.agent_settings
        errors = []
        @contextmanager
        def gated_guard():
            with guard() as value:
                held.set()
                if not proceed.wait(3): raise RuntimeError('reader did not start')
                yield value
        def observed_settings():
            if threading.current_thread().name == 'context-reader':
                reader_needs_config.set(); proceed.set()
            return settings()
        def run(call):
            try: call()
            except ProfileError as exc:
                if exc.reason not in ('dispatch_state_conflict', 'run_conflict'):
                    errors.append(exc.reason)
            except Exception as exc: errors.append(str(exc))
        with patch.object(self.profile.store, 'write_lock', BoundedLock()), patch.object(
                self.profile, 'agent_settings_guard', gated_guard), patch.object(
                self.profile, 'agent_settings', observed_settings):
            submitter = threading.Thread(target=lambda: run(self.admit), daemon=True)
            submitter.start(); self.assertTrue(held.wait(3))
            reader = threading.Thread(target=lambda: run(self.context), name='context-reader', daemon=True)
            reader.start()
            reader_needs_config.wait(.2)
            proceed.set()
            submitter.join(5); reader.join(5)
            self.assertFalse(submitter.is_alive() or reader.is_alive())
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
