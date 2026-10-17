"""Injected Win32 calls only; no real handles, snapshots or executables."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[2] / 'src/session_panel/platform/windows_installation.py'
SPEC = importlib.util.spec_from_file_location('windows_installation_test_leaf', PATH)
WINDOWS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WINDOWS)


class WindowsInstallationTests(unittest.TestCase):
    def api(self):
        api = WINDOWS.InstallationAPI.__new__(WINDOWS.InstallationAPI)
        api.win = SimpleNamespace(WaitForSingleObject=Mock(return_value=0), CloseHandle=Mock(),
                                  created=Mock(return_value=99))
        return api

    def test_mutex_wait_release_and_timeout_never_use_local_fallback(self):
        api = self.api()
        api.mutex_spec = Mock(return_value={'name': 'Global\\SessionPanel.Install.' + 'a' * 64, 'sddl': 'D:P'})
        api.ConvertStringSecurityDescriptorToSecurityDescriptorW = Mock(return_value=True)
        api.CreateMutexW = Mock(return_value=77)
        api.ReleaseMutex, api.LocalFree = Mock(), Mock()
        with api.install_mutex(Path.cwd()): pass
        api.win.WaitForSingleObject.assert_called_once_with(77, 2000)
        api.ReleaseMutex.assert_called_once_with(77)
        api.win.CloseHandle.assert_called_once_with(77)
        api.win.WaitForSingleObject.return_value = 258
        api.ReleaseMutex.reset_mock()
        with self.assertRaises(WINDOWS.InstallationError) as caught:
            with api.install_mutex(Path.cwd()): pass
        self.assertEqual(caught.exception.reason, 'maintenance_busy')
        api.ReleaseMutex.assert_not_called()
        self.assertEqual(api.CreateMutexW.call_args.args[-1].split('\\')[0], 'Global')

    def test_native_read_only_and_exclusive_handles_have_separate_lifetimes(self):
        api = self.api()
        api._open_file = Mock(side_effect=[10, 20, 21])
        with api.readonly_native('native.exe'): pass
        with api.exclusive_asset_handles(['one', 'two']): pass
        self.assertEqual(api._open_file.call_args_list[0].kwargs, {'access': 0x80000000, 'share': 1})
        self.assertEqual(api._open_file.call_args_list[1].kwargs, {'access': 0xC0010000, 'share': 0})
        self.assertEqual([call.args[0] for call in api.win.CloseHandle.call_args_list], [10, 21, 20])

    def test_process_lease_retains_creation_and_waits_same_handle(self):
        api = self.api()
        api.win.WaitForSingleObject.return_value = 258
        peer = WINDOWS.ProcessLease(api, 12, {'created': 99})
        self.assertTrue(peer.alive())
        api.win.WaitForSingleObject.return_value = 0
        self.assertTrue(peer.wait())
        self.assertEqual(api.win.WaitForSingleObject.call_args.args, (12, 5000))
        api.win.created.return_value = 100
        with self.assertRaises(WINDOWS.InstallationError): peer.alive()
        peer.close(); peer.close()
        api.win.CloseHandle.assert_called_once_with(12)

    def test_missing_pipe_never_spawns_or_waits_and_other_errors_block(self):
        api = self.api()
        api.win.CreateFileW = Mock(return_value=None)
        with patch.object(WINDOWS.C, 'get_last_error', return_value=2):
            self.assertIsNone(api.pipe_peer('a' * 64))
        api.win.WaitForSingleObject.assert_not_called()
        with patch.object(WINDOWS.C, 'get_last_error', return_value=5):
            with self.assertRaises(WINDOWS.InstallationError): api.pipe_peer('a' * 64)

    def test_native_cli_is_exact_absolute_status_or_stop_without_shell(self):
        api = self.api()
        binary, profile = Path.cwd() / 'native.exe', Path.cwd() / 'profile'
        with patch.object(WINDOWS.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            api.native_command(binary, 'stop', profile)
            self.assertEqual(run.call_args.args[0], [str(binary), 'stop', '--data-dir', str(profile)])
            self.assertFalse(run.call_args.kwargs['shell'])
            self.assertEqual(run.call_args.kwargs['timeout'], 5)
            with self.assertRaises(WINDOWS.InstallationError): api.native_command(binary, 'start', profile)
            self.assertEqual(run.call_count, 1)

    def process_api(self, image, *, failure=None):
        api = self.api()
        def query(handle, flags, buffer, length):
            buffer.value = str(image)
            return True
        api.win.process = Mock(return_value=321)
        api.win.QueryFullProcessImageNameW = Mock(side_effect=query)
        api.win.credentials = Mock(return_value=(b'\x01\x02', 9, 1))
        api.win.WaitForSingleObject.return_value = 258
        api.file_identity = Mock(return_value={'path': str(image), 'volume': 1, 'file_id': 'a' * 32})
        api._parents = Mock(return_value={4321: 123})
        if failure == 'credentials':
            api.win.credentials.side_effect = WINDOWS.InstallationError('process_owner')
        elif failure == 'file_identity':
            api.file_identity.side_effect = WINDOWS.InstallationError('file_identity')
        elif failure == 'created':
            api.win.created.side_effect = WINDOWS.InstallationError('process_identity')
        elif failure == 'alive':
            api.win.WaitForSingleObject.return_value = 0
        return api

    def test_known_installation_image_identity_failures_block_and_close_same_handle(self):
        root = Path.cwd() / 'work/fake-only-installation'
        image = root / 'unknown/owned-image.exe'
        for failure in ('credentials', 'file_identity', 'created', 'alive'):
            with self.subTest(failure=failure):
                api = self.process_api(image, failure=failure)
                with self.assertRaises(WINDOWS.InstallationError):
                    with api.installation_processes(root):
                        self.fail('known installation candidate must not be yielded as denied')
                api.win.process.assert_called_once_with(4321)
                self.assertEqual(api.win.QueryFullProcessImageNameW.call_args.args[0], 321)
                api.win.CloseHandle.assert_called_once_with(321)

    def test_unmatched_protected_processes_remain_denied_but_recorded_pids_block(self):
        root = Path.cwd() / 'work/fake-only-installation'
        for mode in ('open_denied', 'image_denied', 'outside_identity_denied'):
            with self.subTest(mode=mode):
                api = self.process_api(root.parent / 'unrelated/system.exe', failure='credentials')
                if mode == 'open_denied':
                    api.win.process.side_effect = WINDOWS.InstallationError('process_identity')
                elif mode == 'image_denied':
                    api.win.QueryFullProcessImageNameW.side_effect = None
                    api.win.QueryFullProcessImageNameW.return_value = False
                with api.installation_processes(root) as (leases, denied):
                    self.assertEqual(leases, [])
                    self.assertEqual(denied, [4321])
                if mode == 'open_denied':
                    api.win.CloseHandle.assert_not_called()
                else:
                    api.win.CloseHandle.assert_called_once_with(321)
                api.win.CloseHandle.reset_mock()
                with self.assertRaises(WINDOWS.InstallationError) as caught:
                    with api.installation_processes(root, [4321]):
                        self.fail('recorded candidate must block even before its image can be read')
                self.assertEqual(caught.exception.reason, 'recorded_process_unreadable')
                if mode == 'open_denied':
                    api.win.CloseHandle.assert_not_called()
                else:
                    api.win.CloseHandle.assert_called_once_with(321)

    def test_later_owned_failure_closes_already_retained_process_lease(self):
        root = Path.cwd() / 'work/fake-only-installation'
        image = root / 'unknown/owned-image.exe'
        api = self.process_api(image)
        api._parents.return_value = {4321: 123, 4322: 123}
        api.win.process.side_effect = [321, 322]
        api.win.credentials.side_effect = [(b'\x01\x02', 9, 1), WINDOWS.InstallationError('process_owner')]
        with self.assertRaises(WINDOWS.InstallationError):
            with api.installation_processes(root):
                self.fail('later owned identity failure must abort enumeration')
        self.assertEqual([call.args[0] for call in api.win.CloseHandle.call_args_list], [322, 321])

    def test_known_owned_identity_failure_blocks_real_preflight_without_changes(self):
        from tests.backend.test_installation_maintenance import MaintenanceTests, MAINTENANCE
        case = MaintenanceTests(methodName='test_upgrade_commit_requires_unchanged_launcher_and_activates_last')
        case.setUp()
        try:
            api = self.process_api(case.install / 'unknown/owned-image.exe', failure='credentials')
            case.api.installation_processes = api.installation_processes
            before = case.installation_bytes()
            code, receipt = case.run_preflight(payload=case.context.version_dir)
            self.assertEqual(code, 21)
            self.assertEqual(receipt['reason'], 'owned_process_unreadable')
            self.assertEqual(case.installation_bytes(), before)
            self.assertEqual(case.api.calls, [])
            api.win.CloseHandle.assert_called_once_with(321)
        finally:
            case.doCleanups()
