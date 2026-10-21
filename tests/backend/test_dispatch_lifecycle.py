"""Execution lifecycle contracts over disposable profiles and injected workers."""
import json
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from control_everything.execution import service as execution
from control_everything.execution.store import ExecutionStore
from control_everything.input_validation import ProfileError
from control_everything.workspace import management
from control_everything.workspace.store import WorkspaceError
from tests.backend import test_dispatch_execution_context as fixtures
from tests.backend.test_execution_store import FakeOwner, HoldingWorker


class DispatchLifecycleTests(unittest.TestCase):
    save_agent = fixtures.DispatchExecutionContextTests.save_agent
    context = fixtures.DispatchExecutionContextTests.context
    update = fixtures.DispatchExecutionContextTests.update
    dump = fixtures.DispatchExecutionContextTests.dump

    def setUp(self):
        fixtures.DispatchExecutionContextTests.setUp(self)
        self.ledger = ExecutionStore(self.service.store)
        self.body = {'context_hash': self.context()['context_hash'], 'confirmed': True}
        self.workers = []
        self.owner = FakeOwner(None)
        # Park background reconciliation so each transition is deterministic.
        with patch.object(execution.ExecutionService, '_reconcile_loop',
                          lambda service: service._reconcile_stop.wait()):
            self.runtime = execution.ExecutionService(
                self.service, store=self.ledger, owner_factory=lambda _: self.owner,
                resolver=lambda: 'synthetic.exe', job_query=lambda _: True,
                worker_factory=self.worker_factory, platform='win32')
        self.addCleanup(self.runtime.close)
        self.assertTrue(self.runtime.capability()['available'])

    def worker_factory(self, **kwargs):
        # A different connection must already see every admitted binding.
        with self.profile.store.connection() as conn:
            row = conn.execute('SELECT external_ref,run_id FROM task_dispatches WHERE id=?',
                               (self.dispatch['id'],)).fetchone()
            self.assertEqual(row['external_ref'], kwargs['run']['id'])
            self.assertIsNotNone(conn.execute('SELECT id FROM agent_runs WHERE id=?',
                                             (row['run_id'],)).fetchone())
        worker = HoldingWorker(**kwargs)
        self.workers.append(worker)
        return worker

    def admit(self):
        result = execution.admit_dispatch_execution(
            self.service, self.ledger, self.dispatch['id'], self.body,
            owner_id=self.runtime.owner_id, require_owner=self.runtime._require_owner)
        self.run_id, self.agent_run_id = result['run']['id'], result['agent_run_id']
        return result

    def facts(self):
        with self.profile.store.connection() as conn:
            conn.execute('BEGIN')
            item = dict(conn.execute('SELECT * FROM task_items WHERE id=?', (self.item['id'],)).fetchone())
            run = dict(conn.execute('SELECT * FROM agent_runs WHERE id=?', (self.agent_run_id,)).fetchone())
            events = [dict(row) for row in conn.execute(
                'SELECT * FROM task_events WHERE run_id=? ORDER BY created_at,id', (self.agent_run_id,))]
        return item, run, events

    def complete(self):
        self.ledger.append_event(self.run_id, 'agent_message', {'final': True, 'text': 'Synthetic result'})
        self.ledger.update_run(self.run_id, state='completed', cleanup_state='verified',
                               turn_send_started=True, thread_id='thread', turn_id='turn')
        self.runtime._project(self.run_id)

    def review(self, verdict='approved'):
        item = self.profile.store.get_item(self.item['id'])
        return management.save_review(self.profile.store, {
            'task_id': item['id'], 'expected_revision': item['revision'],
            'verdict': verdict, 'reason': 'Human decision', 'confirmed': True})

    def test_submit_sees_committed_rows_and_replay_starts_one_worker(self):
        first = self.runtime.submit_dispatch(self.dispatch['id'], self.body)
        replay = self.runtime.submit_dispatch(self.dispatch['id'], self.body)
        self.assertFalse(first['replayed'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(first['execution_run_id'], replay['execution_run_id'])
        self.assertEqual(len(self.workers), 1)
        self.runtime.close()
        self.assertTrue(self.runtime.submit_dispatch(self.dispatch['id'], self.body)['replayed'])

    def test_modern_dispatch_root_validates_after_admission(self):
        result = self.admit()
        self.assertTrue(self.runtime.validate_root(result['run']),
                        'Modern project directories must validate before CodexRun can start')

    def test_runtime_validation_rejects_disabled_agent(self):
        result = self.admit()
        self.save_agent(enabled=False)
        self.assertFalse(self.runtime.validate_root(result['run']))

    def test_runtime_validation_rejects_broken_dispatch_binding(self):
        result = self.admit()
        self.update('task_dispatches', self.dispatch['id'], external_ref='broken')
        self.assertFalse(self.runtime.validate_root(result['run']))

    def test_replay_is_readable_after_service_policy_changes(self):
        first = self.runtime.submit_dispatch(self.dispatch['id'], self.body)
        self.runtime.policy['network_access'] = True
        replay = self.runtime.submit_dispatch(self.dispatch['id'], self.body)
        self.assertEqual(first['execution_run_id'], replay['execution_run_id'])
        self.assertTrue(replay['replayed'])

    def test_completed_verified_projects_once_and_never_auto_completes(self):
        self.admit(); self.complete()
        item, run, _events = self.facts()
        self.assertEqual((item['state'], run['status']), ('review', 'waiting_review'))
        before = self.dump()
        self.runtime._project(self.run_id)
        self.assertEqual(self.dump(), before)

    def test_late_projection_preserves_human_approval(self):
        self.admit(); self.complete(); self.review()
        before = self.dump()
        self.runtime._project(self.run_id)
        self.assertEqual(self.dump(), before, 'Reconciliation must never undo human approval')

    def test_late_projection_preserves_human_changes_requested(self):
        self.admit(); self.complete(); self.review('changes_requested')
        before = self.dump()
        self.runtime._project(self.run_id)
        self.assertEqual(self.dump(), before, 'Reconciliation must never resubmit rejected work')

    def test_human_review_records_the_implicit_current_run(self):
        self.admit(); self.complete()
        result = self.review()
        self.assertEqual(result['review']['run_id'], self.agent_run_id)

    def test_completed_without_cleanup_emits_no_waiting_review(self):
        self.admit()
        self.ledger.update_run(self.run_id, state='completed', cleanup_state='pending')
        self.runtime._project(self.run_id)
        item, run, events = self.facts()
        self.assertEqual(item['state'], 'blocked')
        self.assertIn(run['status'], ('claimed', 'running'))
        self.assertNotIn('waiting_review', [row['kind'] for row in events])

    def test_send_attempt_without_confirmed_turn_is_not_delivered(self):
        self.admit()
        self.ledger.update_run(self.run_id, state='cancel_requested', turn_send_started=True)
        self.runtime._project(self.run_id)
        self.assertNotEqual(self.service.task_dispatch(self.dispatch['id'])['delivery_state'], 'delivered')

    def test_final_message_after_first_event_page_is_in_review_summary(self):
        self.admit()
        # Use one real transaction to seed a long tool event stream.
        with self.ledger.connection(immediate=True) as conn:
            conn.executemany('INSERT INTO execution_events(run_id,kind,payload_json,observed_at) VALUES(?,?,?,?)',
                [(self.run_id, 'command', json.dumps({'command': 'synthetic'}), '2026-10-20T00:00:00Z')]*205)
        self.complete()
        self.assertEqual(self.facts()[1]['summary'], 'Synthetic result')

    def test_current_pending_approval_is_projected_instead_of_old_approval(self):
        self.admit()
        self.ledger.update_run(self.run_id, state='waiting_approval', thread_id='thread', turn_id='turn',
                               turn_send_started=True)
        def approval(native):
            return self.ledger.add_approval(self.run_id, self.runtime.owner_id, native,
                'item/commandExecution/requestApproval', 'thread', 'turn', native,
                {'command': 'synthetic'}, True)
        old = approval('1')
        self.ledger.update_approval(self.run_id, old['id'], status='expired')
        current = approval('2')
        self.runtime._project(self.run_id)
        payloads = [json.loads(row['payload_json']) for row in self.facts()[2]]
        waits = [row for row in payloads if row.get('stage') == 'waiting_user']
        self.assertEqual([row['request_id'] for row in waits], [current['id']])

    def test_close_after_commit_retains_owner_until_final_write(self):
        original_admit = execution.admit_dispatch_execution
        original_update = self.ledger.update_run
        owner_at_write = []
        def admit_then_close(*args, **kwargs):
            result = original_admit(*args, **kwargs)
            self.runtime.close()
            self.assertTrue(self.owner.is_owner, 'Inflight admission must retain its owner')
            return result
        def observe_update(*args, **kwargs):
            owner_at_write.append(self.owner.is_owner)
            return original_update(*args, **kwargs)
        with patch.object(execution, 'admit_dispatch_execution', side_effect=admit_then_close), patch.object(
                self.ledger, 'update_run', side_effect=observe_update):
            result = self.runtime.submit_dispatch(self.dispatch['id'], self.body)
        self.assertEqual(result['run']['state'], 'cancelled_before_send')
        self.assertEqual(self.workers, [])
        self.assertTrue(owner_at_write)
        self.assertTrue(all(owner_at_write), 'Owner was released before the committed run was finalized')

    def test_manual_writes_cannot_release_or_submit_an_execution_run(self):
        self.admit()
        item = self.profile.store.get_item(self.item['id'])
        calls = [lambda: self.service.cancel_agent_run(item['id'], item['revision'], 'Manual release'),
                 lambda: self.service.progress_agent_task(self.agent['id'], item['id'], item['revision'],
                    'implementing', run_id=self.agent_run_id),
                 lambda: self.service.submit_agent_review(self.agent['id'], item['id'], item['revision'],
                    'Manual result', run_id=self.agent_run_id)]
        for call in calls:
            with self.subTest(call=call), self.assertRaisesRegex(WorkspaceError, 'execution_managed_run'):
                call()
        before = self.dump()
        with self.assertRaises(ProfileError) as caught:
            self.profile.store.save_item({'state': 'done'}, item_id=item['id'], expected_revision=item['revision'])
        self.assertEqual(caught.exception.reason, 'execution_managed_item')
        self.assertEqual(self.dump(), before)
        edited = self.profile.store.save_item({'title': 'Renamed', 'priority': 0, 'focus': 1},
            item_id=item['id'], expected_revision=item['revision'])
        self.assertEqual((edited['title'], edited['priority'], edited['focus']), ('Renamed', 0, 1))

    def test_manual_dispatch_human_review_remains_supported(self):
        self.dispatch = self.service.create_task_dispatch(self.item['id'], {
            'agent_id': self.agent['id'], 'directory_id': 'main', 'expected_brief_revision': 1,
            'confirmed': True, 'delivery_mode': 'manual'})['dispatch']
        self.service.mark_dispatch_sent(self.dispatch['id'])
        item = self.profile.store.get_item(self.item['id'])
        claimed = self.service.claim_agent_task(self.agent['id'], item['id'], item['revision'])
        self.service.submit_agent_review(self.agent['id'], item['id'], claimed['item']['revision'],
            'Manual agent result', run_id=claimed['run']['id'])
        self.assertEqual(self.review()['item']['state'], 'done')

    def test_terminal_failure_sets_agent_run_end_time(self):
        self.admit()
        self.ledger.update_run(self.run_id, state='failed', cleanup_state='verified',
                               reason='Synthetic error', ended_at='2026-10-20T00:00:00Z')
        self.runtime._project(self.run_id)
        item, run, _events = self.facts()
        self.assertEqual((item['state'], run['status']), ('blocked', 'failed'))
        self.assertIsNotNone(run['ended_at'])

    def test_native_file_change_shape_reaches_review_evidence(self):
        self.admit()
        self.ledger.append_event(self.run_id, 'file_change', {
            'item_id': 'file-item', 'status': 'completed',
            'changes': [{'path': 'report.md', 'kind': 'add', 'diff': '+Synthetic',
                         'path_resolved': True, 'scope_verified': True}]})
        self.complete()
        self.assertIn('report.md', json.loads(self.facts()[1]['evidence_json']))

    def test_review_requires_waiting_review_run_not_only_item_state(self):
        self.admit()
        self.ledger.update_run(self.run_id, state='completed', cleanup_state='verified')
        self.update('task_items', self.item['id'], state='review')
        with self.assertRaises(ProfileError) as caught:
            self.review()
        self.assertEqual(caught.exception.reason, 'execution_review_conflict')

    def test_startup_recovers_without_starting_or_resending_worker(self):
        self.admit()
        self.ledger.update_run(self.run_id, turn_send_started=True)
        self.runtime.close()
        created = []
        def forbidden_worker(**kwargs):
            created.append(kwargs)
            raise AssertionError('Recovery must never start a worker')
        with patch.object(execution.ExecutionService, '_reconcile_loop',
                          lambda service: service._reconcile_stop.wait()):
            recovered = execution.ExecutionService(
                self.service, store=self.ledger, owner_factory=lambda _: FakeOwner(None),
                resolver=lambda: 'synthetic.exe', job_query=lambda _: True,
                worker_factory=forbidden_worker, platform='win32')
        self.addCleanup(recovered.close)
        self.assertTrue(recovered.capability()['available'])
        self.assertEqual(self.ledger.get_run(self.run_id)['state'], 'unknown')
        self.assertEqual(self.facts()[0]['state'], 'blocked')
        self.assertIn(self.facts()[1]['status'], ('claimed', 'running'))
        self.assertEqual(created, [])
        self.assertTrue(recovered.submit_dispatch(self.dispatch['id'], self.body)['replayed'])

    def test_projection_failure_retries_only_projection(self):
        self.admit()
        self.ledger.update_run(self.run_id, state='completed', cleanup_state='verified')
        with patch.object(self.service, 'project_execution_state', side_effect=WorkspaceError('synthetic')):
            self.runtime._project(self.run_id)
        self.assertEqual(self.facts()[0]['state'], 'doing')
        self.runtime._reconcile_open()
        self.assertEqual(self.facts()[0]['state'], 'review')
        self.assertEqual(self.workers, [])

    def test_old_unprojected_run_is_not_hidden_by_history_page_limit(self):
        self.admit()
        self.ledger.update_run(self.run_id, state='completed', cleanup_state='verified')
        seed = self.ledger.get_run(self.run_id)
        with self.ledger.connection(immediate=True) as conn:
            for _index in range(50):
                row = {**seed, 'id': str(uuid4()), 'request_id': str(uuid4()),
                       'created_at': '2099-01-01T00:00:00Z'}
                conn.execute('INSERT INTO execution_runs (' + ','.join(row) + ') VALUES (' +
                             ','.join('?' for _ in row) + ')', tuple(row.values()))
        self.runtime._reconcile_open()
        self.assertEqual(self.facts()[0]['state'], 'review',
                         'A pending projection must not disappear behind the 50-run display page')

    def test_review_rejects_broken_reverse_dispatch_binding(self):
        self.admit(); self.complete()
        self.update('task_dispatches', self.dispatch['id'], run_id=None)
        with self.assertRaises(ProfileError):
            self.review()

    def test_stale_projection_cannot_clear_new_approval_wait(self):
        self.admit()
        original_facts = self.ledger.projection_facts_in_transaction
        attempts = []
        def read_while_competing_writer_waits(connection, run_id):
            self.assertTrue(connection.in_transaction)
            def competing_writer():
                peer = sqlite3.connect(self.profile.store.path, timeout=.05)
                try:
                    peer.execute('BEGIN IMMEDIATE')
                    attempts.append('writer_acquired')
                except sqlite3.OperationalError as error:
                    attempts.append('writer_blocked' if 'locked' in str(error) else str(error))
                finally:
                    peer.rollback(); peer.close()
            thread = threading.Thread(target=competing_writer, daemon=True)
            thread.start(); thread.join(2)
            self.assertFalse(thread.is_alive())
            return original_facts(connection, run_id)
        # Ledger reads and item writes must share the same SQLite write transaction.
        with patch.object(self.ledger, 'projection_facts_in_transaction',
                          side_effect=read_while_competing_writer_waits):
            self.runtime._project(self.run_id)
        self.assertEqual(attempts, ['writer_blocked'])
        self.assertNotIn(self.run_id, self.runtime._project_errors)
        self.ledger.update_run(self.run_id, state='waiting_approval',
            turn_send_started=True, thread_id='thread', turn_id='turn')
        self.ledger.add_approval(self.run_id, self.runtime.owner_id, '1',
            'item/commandExecution/requestApproval', 'thread', 'turn', 'item',
            {'command': 'synthetic'}, True)
        self.runtime._project(self.run_id)
        self.assertEqual(self.ledger.get_run(self.run_id)['state'], 'waiting_approval')
        self.assertEqual(self.facts()[0]['state'], 'blocked',
                         'The next transaction must project the newer approval wait')

    def test_active_unverified_cleanup_blocks_the_item(self):
        self.admit()
        self.ledger.update_run(self.run_id, state='running', cleanup_state='unverified',
                               turn_send_started=True, thread_id='thread', turn_id='turn')
        self.runtime._project(self.run_id)
        item, run, _events = self.facts()
        self.assertEqual(item['state'], 'blocked')
        self.assertIn(run['status'], ('claimed', 'running'))

    def test_ordinary_starting_pending_cleanup_does_not_emit_unknown_block(self):
        self.admit()
        self.runtime._project(self.run_id)
        item, run, events = self.facts()
        self.assertEqual((item['state'], run['status']), ('doing', 'claimed'))
        self.assertNotIn('blocked', [row['kind'] for row in events],
                         'Normal active cleanup pending is not an unknown execution result')

    def test_long_final_summary_respects_limit_with_explicit_truncation(self):
        self.admit()
        self.ledger.append_event(self.run_id, 'agent_message', {'final': True, 'text': 'x'*5000})
        self.ledger.update_run(self.run_id, state='completed', cleanup_state='verified')
        self.runtime._project(self.run_id)
        summary = self.facts()[1]['summary']
        self.assertIn('[truncated]', summary)
        self.assertLessEqual(len(summary), 4000)

    def test_close_retains_owner_until_background_scan_finishes(self):
        entered, release = threading.Event(), threading.Event()
        def blocked_page(*args, **kwargs):
            entered.set()
            if not release.wait(4):
                raise RuntimeError('Synthetic scan was not released')
            return []
        with patch.object(self.ledger, 'projection_run_ids', side_effect=blocked_page):
            thread = threading.Thread(target=self.runtime._reconcile_open, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.runtime.close()
                self.assertTrue(self.owner.is_owner, 'An inflight scan must retain its owner')
            finally:
                release.set(); thread.join(4)
            self.assertFalse(thread.is_alive())
        self.assertFalse(self.owner.is_owner)

    def test_projection_write_failure_rolls_back_every_linked_record(self):
        self.admit()
        self.ledger.update_run(self.run_id, state='completed', cleanup_state='verified')
        before = self.dump()
        original_project = self.service.project_execution_state
        def fail_after_writes(*args, **kwargs):
            original_project(*args, **kwargs)
            raise WorkspaceError('synthetic_after_projection_writes')
        with patch.object(self.service, 'project_execution_state', side_effect=fail_after_writes):
            self.runtime._project(self.run_id)
        self.assertEqual(self.dump(), before)
        self.assertIn(self.run_id, self.runtime._project_errors)
        self.runtime._project(self.run_id)
        self.assertEqual(self.facts()[0]['state'], 'review')
        self.assertNotIn(self.run_id, self.runtime._project_errors)
        self.assertEqual(self.workers, [])

    def test_concurrent_dispatch_submissions_start_one_worker(self):
        barrier = threading.Barrier(4)
        def submit(_index):
            barrier.wait(timeout=4)
            return self.runtime.submit_dispatch(self.dispatch['id'], self.body)
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(submit, range(4)))
        self.assertEqual(len({row['execution_run_id'] for row in receipts}), 1)
        self.assertEqual(sum(not row['replayed'] for row in receipts), 1)
        self.assertEqual(len(self.workers), 1)

    def test_cleanup_matrix_preserves_occupancy_until_safe_terminal(self):
        self.admit()
        active = ('starting', 'running', 'waiting_approval', 'cancel_requested')
        terminal = ('completed', 'failed', 'interrupted', 'cancelled_before_send')
        for state in (*active, *terminal, 'unknown'):
            for cleanup in ('pending', 'unverified', 'verified'):
                with self.subTest(state=state, cleanup=cleanup):
                    # Seed each case independently; runtime updates deliberately disallow rewinding terminals.
                    with self.profile.store.connection() as conn:
                        conn.execute('BEGIN IMMEDIATE')
                        conn.execute("UPDATE execution_runs SET state=?,cleanup_state=?,turn_id='turn' WHERE id=?",
                                     (state, cleanup, self.run_id))
                        conn.execute("UPDATE agent_runs SET status='claimed',ended_at=NULL,summary='',evidence_json='[]' WHERE id=?",
                                     (self.agent_run_id,))
                        conn.execute("UPDATE task_items SET state='doing' WHERE id=?", (self.item['id'],))
                        conn.execute("UPDATE task_dispatches SET delivery_state='sent' WHERE id=?", (self.dispatch['id'],))
                        conn.execute("DELETE FROM task_events WHERE run_id=? AND kind!='claimed'", (self.agent_run_id,))
                    self.runtime._project(self.run_id)
                    item, run, events = self.facts()
                    if cleanup == 'unverified' or state == 'unknown' or state in terminal and cleanup == 'pending':
                        self.assertEqual(item['state'], 'blocked')
                        self.assertIn(run['status'], ('claimed', 'running'))
                        self.assertIsNone(run['ended_at'])
                        self.assertNotIn('waiting_review', [row['kind'] for row in events])
                    elif state in active:
                        self.assertEqual(item['state'], 'blocked' if state == 'waiting_approval' else 'doing')
                        self.assertIsNone(run['ended_at'])
                        self.assertNotIn('blocked', [row['kind'] for row in events])
                    elif state == 'completed':
                        self.assertEqual((item['state'], run['status']), ('review', 'waiting_review'))
                    else:
                        self.assertEqual(item['state'], 'blocked')
                        self.assertEqual(run['status'], 'failed' if state == 'failed' else 'cancelled')
                        self.assertIsNotNone(run['ended_at'])
                    self.assertNotIn(self.run_id, self.runtime._project_errors)


if __name__ == '__main__':
    unittest.main()
