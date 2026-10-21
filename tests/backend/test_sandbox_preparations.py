"""Sandbox preparation safety contracts using synthetic paths and mocked ACL mutations."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import subprocess
import unittest
from unittest.mock import Mock, patch

from control_everything.execution import sandbox_preparations as preparations
from control_everything.execution.service import admit_dispatch_execution
from control_everything.execution.store import ExecutionStore
from control_everything.platform import windows_acl
from tests.backend import test_dispatch_execution_context as dispatch_fixtures


class WindowsAclDetectionTests(unittest.TestCase):
    def test_required_rights_are_checked_by_the_kernel(self):
        sid = 'S-1-5-21-100-200-300-1001'
        api = Mock()
        api.kernel.CreateFileW.return_value = 42
        with patch.object(windows_acl, 'current_user_sid', return_value=sid), patch.object(windows_acl, 'ACLAPI', return_value=api):
            self.assertTrue(windows_acl.has_preparation_access('fixture', sid))
        self.assertEqual(api.kernel.CreateFileW.call_args.args[1], windows_acl.PREPARATION_ACCESS)
        api.kernel.CloseHandle.assert_called_once_with(42)

    def test_another_sid_with_the_same_prefix_is_not_the_current_user(self):
        sid = 'S-1-5-21-100-200-300-1001'
        with patch.object(windows_acl, 'current_user_sid', return_value=sid+'0'), patch.object(windows_acl, 'ACLAPI') as api:
            self.assertFalse(windows_acl.has_preparation_access('fixture', sid))
            api.assert_not_called()

    def test_kernel_denial_cannot_be_overridden_by_an_allow_entry(self):
        sid = 'S-1-5-21-100-200-300-1001'
        api = Mock()
        api.kernel.CreateFileW.return_value = windows_acl.c.c_void_p(-1).value
        with patch.object(windows_acl, 'current_user_sid', return_value=sid), patch.object(windows_acl, 'ACLAPI', return_value=api), patch.object(windows_acl.c, 'get_last_error', return_value=5):
            self.assertFalse(windows_acl.has_preparation_access('fixture', sid))


class PreparationSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / 'project'
        self.target.mkdir()
        self.data_dir = self.root / 'profile'
        self.sid = 'S-1-5-21-100-200-300-1001'
        self.identity = {'st_dev': 1, 'st_ino': 2, 'machine': 'synthetic-machine'}
        for name, value in (
                ('current_user_sid', self.sid), ('owner_sid', self.sid),
                ('file_identity', self.identity), ('local_ntfs', True),
                ('sddl', 'synthetic-security-before'), ('has_preparation_access', True),
                ('grant_current_user', True), ('restore_dacl', True)):
            mock = self.enterContext(patch.object(windows_acl, name, return_value=value))
            setattr(self, name, mock)

    def prepared(self):
        return preparations.prepare(self.data_dir, 'sample', self.target)

    def test_supported_owner_directory_can_be_prepared(self):
        self.assertEqual(self.prepared()['status'], 'ready')
        self.assertEqual(preparations.status(self.data_dir, 'sample', self.target)['status'], 'ready')

    def test_full_control_does_not_mark_foreign_owner_ready(self):
        self.owner_sid.return_value = 'S-1-5-21-999-999-999-1002'
        self.assertNotEqual(preparations.status(self.data_dir, 'sample', self.target)['status'], 'ready')

    def test_unsupported_filesystem_cannot_bypass_preparation(self):
        self.local_ntfs.return_value = False
        self.assertNotEqual(preparations.status(self.data_dir, 'sample', self.target)['status'], 'ready')

    def test_security_change_invalidates_prepared_confirmation(self):
        self.prepared()
        self.sddl.return_value = 'synthetic-security-externally-changed'
        self.assertNotEqual(preparations.status(self.data_dir, 'sample', self.target)['status'], 'ready')

    def test_repeated_prepare_preserves_original_backup(self):
        self.prepared()
        backup = Path(preparations._read(self.data_dir)['items']['sample']['backup'])
        original = backup.read_bytes()
        self.sddl.return_value = 'synthetic-security-after-first-prepare'
        self.prepared()
        self.assertEqual(backup.read_bytes(), original)

    def test_repeated_prepare_reuses_ready_generation(self):
        before = self.prepared()
        after = self.prepared()
        self.assertEqual(before['generation'], after['generation'])

    def test_restore_refuses_a_replaced_directory(self):
        self.prepared()
        self.file_identity.return_value = {**self.identity, 'st_ino': 999}
        with self.assertRaises(ValueError):
            preparations.restore(self.data_dir, 'sample')
        self.restore_dacl.assert_not_called()

    def test_restore_refuses_external_permission_changes(self):
        self.prepared()
        self.sddl.return_value = 'synthetic-security-with-other-user-changes'
        with self.assertRaises(ValueError):
            preparations.restore(self.data_dir, 'sample')
        self.restore_dacl.assert_not_called()

    def test_failed_removal_cannot_be_reported_as_restored(self):
        self.has_preparation_access.return_value = False
        def grant(*_):
            self.has_preparation_access.return_value = True
            return True
        self.grant_current_user.side_effect = grant
        self.prepared()
        self.restore_dacl.return_value = False
        with self.assertRaises((ValueError, OSError)):
            preparations.restore(self.data_dir, 'sample')

    def test_restore_preserves_permission_that_the_product_did_not_add(self):
        self.prepared()
        self.grant_current_user.assert_not_called()
        preparations.restore(self.data_dir, 'sample')
        self.restore_dacl.assert_not_called()

    def test_readback_failure_after_grant_leaves_a_failed_record(self):
        self.has_preparation_access.return_value = False
        self.sddl.side_effect = ['synthetic-before-grant', 'synthetic-before-grant', OSError('acl_read_failed')]
        with self.assertRaises(OSError):
            self.prepared()
        self.grant_current_user.assert_called_once()
        state = preparations._read(self.data_dir)
        self.assertEqual(state['items'].get('sample', {}).get('status'), 'failed')

    def test_already_satisfied_snapshot_tracks_permission_changes(self):
        before = preparations.status(self.data_dir, 'sample', self.target)
        self.sddl.return_value = 'synthetic-security-externally-changed'
        after = preparations.status(self.data_dir, 'sample', self.target)
        self.assertNotEqual(before, after, 'ACL changes must invalidate the dispatch confirmation digest')

    def test_directory_lease_blocks_same_directory_and_ancestor_mutation(self):
        with windows_acl.directory_lock(self.target):
            for directory in (self.target, self.root):
                with self.subTest(directory=str(directory)), self.assertRaises(OSError):
                    with windows_acl.directory_lock(directory):
                        self.fail('An overlapping directory must not be mutable during execution')

    def test_directory_lease_allows_an_unrelated_sibling(self):
        sibling = self.root / 'sibling'
        sibling.mkdir()
        with windows_acl.directory_lock(self.target), windows_acl.directory_lock(sibling):
            self.assertTrue(sibling.is_dir())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction contract')
    def test_authorization_rejects_an_ntfs_junction(self):
        alias = self.root / 'junction'
        command = [os.environ.get('COMSPEC') or str(Path(os.environ['SYSTEMROOT']) / 'System32' / 'cmd.exe'),
                   '/d', '/c', 'mklink', '/J', str(alias), str(self.target)]
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode:
            self.skipTest('The host cannot create an NTFS junction')
        self.assertTrue(alias.is_junction())
        with self.assertRaises(ValueError):
            preparations.prepare(self.data_dir, 'sample', alias)
        self.grant_current_user.assert_not_called()

    def test_authorization_does_not_follow_a_directory_junction(self):
        alias = self.root / 'alias'
        try:
            alias.symlink_to(self.target, target_is_directory=True)
        except OSError:
            self.skipTest('Creating a directory symlink requires host privilege')
        with self.assertRaises(ValueError):
            preparations.prepare(self.data_dir, 'sample', alias)
        self.grant_current_user.assert_not_called()


class PreparationHTTPTests(unittest.TestCase):
    setUp = dispatch_fixtures.DispatchExecutionContextTests.setUp
    save_agent = dispatch_fixtures.DispatchExecutionContextTests.save_agent
    request = dispatch_fixtures.DispatchExecutionContextTests.request

    def confirmation(self):
        route = '/api/workspace/projects/' + self.item['project_id'] + '/sandbox-preparation'
        code, value = self.request(route)
        self.assertEqual(code, 200)
        self.assertIn('confirmation_hash', value)
        return {'confirmed': True, 'path': value['canonical_path'],
                'directory_id': value['directory_id'], 'confirmation_hash': value['confirmation_hash']}

    def test_successful_prepare_has_exactly_one_http_response(self):
        route = '/api/workspace/projects/' + self.item['project_id'] + '/sandbox-preparation'
        with patch.object(preparations, 'prepare', return_value={'status': 'ready'}) as prepare, \
                patch('tests.backend.test_execution_http.Mock', side_effect=lambda: Mock(return_value=None)):
            response = self.request(route, 'POST', self.confirmation())
        self.assertEqual(self.last_handler.reply.call_count, 1)
        self.assertEqual(response[0], 200)
        prepare.assert_called_once()

    def test_acl_os_error_returns_a_structured_http_failure(self):
        route = '/api/workspace/projects/' + self.item['project_id'] + '/sandbox-preparation'
        with patch.object(preparations, 'prepare', side_effect=OSError('acl_read_failed')):
            try:
                response = self.request(route, 'POST', self.confirmation())
            except OSError:
                self.fail('An ACL failure must produce an HTTP error response')
        self.assertGreaterEqual(response[0], 400)

    def active_run(self, state):
        ledger = ExecutionStore(self.service.store)
        context = dispatch_fixtures.dispatch_execution_context(self.service, self.dispatch['id'])
        run = admit_dispatch_execution(self.service, ledger, self.dispatch['id'],
            {'confirmed': True, 'context_hash': context['context_hash']},
            owner_id='synthetic-owner', require_owner=Mock())['run']
        ledger.update_run(run['id'], state=state)

    def prepare_response(self):
        route = '/api/workspace/projects/' + self.item['project_id'] + '/sandbox-preparation'
        return self.request(route, 'POST', self.confirmation())

    def test_running_project_rejects_preparation(self):
        self.active_run('running')
        with patch.object(preparations, 'prepare') as prepare:
            self.assertEqual(self.prepare_response()[0], 409)
            prepare.assert_not_called()

    def test_waiting_approval_project_rejects_preparation(self):
        self.active_run('waiting_approval')
        with patch.object(preparations, 'prepare', return_value={'status': 'ready'}) as prepare:
            self.assertEqual(self.prepare_response()[0], 409)
            prepare.assert_not_called()

    def test_cancelling_project_rejects_preparation(self):
        self.active_run('cancel_requested')
        with patch.object(preparations, 'prepare', return_value={'status': 'ready'}) as prepare:
            self.assertEqual(self.prepare_response()[0], 409)
            prepare.assert_not_called()

    def test_incorrect_confirmed_path_is_rejected(self):
        route = '/api/workspace/projects/' + self.item['project_id'] + '/sandbox-preparation'
        with patch.object(preparations, 'prepare') as prepare:
            response = self.request(route, 'POST', {'confirmed': True, 'path': 'different-project'})
        self.assertEqual(response[0], 409)
        prepare.assert_not_called()

    def test_preparation_requires_a_bound_preflight(self):
        route = '/api/workspace/projects/' + self.item['project_id'] + '/sandbox-preparation'
        with patch.object(preparations, 'prepare', return_value={'status': 'ready'}) as prepare:
            response = self.request(route, 'POST', {'confirmed': True})
        self.assertEqual(response[0], 409)
        prepare.assert_not_called()

    def test_acl_change_after_preflight_is_rejected_before_any_grant(self):
        body = self.confirmation()
        route = '/api/workspace/projects/' + self.item['project_id'] + '/sandbox-preparation'
        with patch.object(windows_acl, 'sddl', return_value='changed-after-confirmation'), \
                patch.object(windows_acl, 'grant_current_user') as grant:
            self.assertEqual(self.request(route, 'POST', body)[0], 409)
            grant.assert_not_called()

    def test_worker_holds_directory_lease_until_close(self):
        import threading
        from control_everything.execution.service import ExecutionService, dispatch_execution_context
        from tests.backend.test_execution_store import HoldingWorker, FakeOwner
        started = threading.Event()
        class Worker(HoldingWorker):
            def run(self):
                started.set()
                super().run()
        execution = ExecutionService(self.service, worker_factory=Worker,
            resolver=lambda: Path('synthetic-cli.exe'), owner_factory=lambda _: FakeOwner(None),
            platform='win32', codex_required=False)
        self.addCleanup(execution.close)
        context = dispatch_execution_context(self.service, self.dispatch['id'])
        execution.submit_dispatch(self.dispatch['id'], {'confirmed': True, 'context_hash': context['context_hash']})
        self.assertTrue(started.wait(5), 'Synthetic worker should enter the leased execution region')
        directory = self.service.project_directory(self.item['project_id'])
        with patch.object(windows_acl, 'grant_current_user') as grant, self.assertRaises(OSError):
            preparations.prepare(self.profile.data_dir, self.item['project_id'], directory)
        grant.assert_not_called()
        execution.close()
        with windows_acl.directory_lock(directory):
            self.assertTrue(Path(directory).is_dir())


if __name__ == '__main__':
    unittest.main()
