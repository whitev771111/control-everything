"""Cooperative lifecycle fixtures; every process and lock is fake."""

import copy
import json
from pathlib import Path
import shutil
from unittest.mock import patch

from tests.backend.test_installation_registry import InstallationFixture, REGISTRY, MAINTENANCE, MANIFEST


class MaintenanceTests(InstallationFixture):
    def test_temp_reparse_is_rejected_before_alias_resolution(self):
        import stat
        from types import SimpleNamespace
        raw = self.root / 'synthetic temp junction'
        original_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            if path == raw:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original_lstat(path, *args, **kwargs)
        with patch.object(MAINTENANCE.tempfile, 'gettempdir', return_value=str(raw)), \
                patch.object(Path, 'lstat', lstat), \
                patch.object(Path, 'resolve', side_effect=AssertionError('reparse must be rejected before resolve')) as resolve, \
                self.assertRaises(MAINTENANCE.manifest.ManifestError) as caught:
            MAINTENANCE.canonical_temp_root()
        self.assertEqual(caught.exception.reason, 'linked_path')
        resolve.assert_not_called()

    def test_short_temp_alias_keeps_prepare_and_uninstall_chain_valid(self):
        import ctypes as C
        from ctypes import wintypes as W
        import os
        import tempfile
        if os.name != 'nt':
            self.skipTest('Windows 8.3 path alias contract')
        with tempfile.TemporaryDirectory(prefix='Session Panel canonical temp ', dir=self.root) as directory:
            canonical = Path(directory).resolve()
            shorten = C.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
            shorten.argtypes, shorten.restype = [W.LPCWSTR, W.LPWSTR, W.DWORD], W.DWORD
            buffer = C.create_unicode_buffer(32768)
            size = shorten(str(canonical), buffer, len(buffer))
            self.assertTrue(0 < size < len(buffer))
            short = Path(buffer.value)
            if str(short).casefold() == str(canonical).casefold():
                self.skipTest('Test volume does not provide a distinct 8.3 alias')
            self.assertEqual(short.resolve(), canonical)
            self.assertEqual((short.stat().st_dev, short.stat().st_ino),
                             (canonical.stat().st_dev, canonical.stat().st_ino))
            with patch.object(tempfile, 'tempdir', str(short)):
                case = MaintenanceTests(methodName='test_exact_inno_two_stage_chain_does_not_wait_or_lock_own_dat')
                case.setUp()
                try:
                    invocation = case.invocation(uninstall=True)
                    code, receipt = case.run_preflight('uninstall', invocation=invocation)
                    self.assertEqual((code, receipt['status']), (0, 'ready'))
                    self.assertTrue(Path(invocation['bootstrap']).is_relative_to(canonical))
                    self.assertEqual(case.api.mutex_waits, 0)
                finally:
                    case.doCleanups()

    def installation_bytes(self, root=None):
        root = self.install if root is None else root
        return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob('*') if path.is_file()}

    def run_preflight(self, operation='upgrade', *, invocation=None, payload=None):
        report = self.root / ('report-' + str(len(list(self.root.glob('report-*.json')))) + '.json')
        code = MAINTENANCE.preflight(self.install, operation, report, api=self.api, invocation=invocation, payload=payload)
        return code, REGISTRY.read_record(report)

    def test_two_profiles_stop_with_original_exes_before_exclusive_assets(self):
        self.profile('one', pid=201)
        self.profile('two', pid=202)
        code, report = self.run_preflight()
        self.assertEqual(code, 0)
        self.assertEqual(len(report['stopped']), 2)
        self.assertEqual(self.api.mutex_waits, 1)
        self.assertEqual(self.api.mutex_releases, 1)
        names = [item[0] for item in self.api.calls]
        self.assertEqual(names, ['readonly_open', 'status', 'stop', 'readonly_close'] * 2 + ['exclusive'])
        assets = self.api.calls[-1][1]
        self.assertNotIn(self.install / 'unins000.exe', assets)
        self.assertNotIn(self.install / 'unins000.dat', assets)

    def test_same_profile_keeps_old_association_and_uses_peer_version(self):
        path = self.profile('one')
        second = self.make_version('0.1.0a2')
        REGISTRY.write_record(self.install / '.install/versions' / (second.version_id + '.json'), REGISTRY.version_record(second, self.api))
        self.profile('one', context=second, pid=202)
        code, report = self.run_preflight()
        self.assertEqual(code, 0)
        stops = [item for item in self.api.calls if item[0] == 'stop']
        self.assertEqual(stops, [('stop', str(second.version_dir / MANIFEST.NATIVE_EXE), str(path))])
        self.assertEqual(report['stopped'][0]['version_id'], second.version_id)

    def test_http_only_blocks_and_unknown_reused_pid_never_authorizes_stop(self):
        profile = self.profile()
        self.api.add_process(100, self.context.version_dir / 'runtime/python.exe')
        self.api.current_pid = 100
        lease = REGISTRY.register_http(self.context, profile, api=self.api)
        self.api.current_pid = 999
        self.assertEqual(self.run_preflight()[0], 20)
        self.assertFalse(self.api.calls)
        self.api.processes[100].identity['created'] += 1
        self.assertEqual(self.run_preflight()[0], 21)
        self.assertFalse(self.api.calls)
        self.assertTrue(lease.path.exists())

    def test_pipe_missing_but_unknown_daemon_or_copied_exe_blocks(self):
        profile = self.profile()
        self.api.add_process(201, self.context.version_dir / MANIFEST.NATIVE_EXE)
        self.assertEqual(self.run_preflight()[0], 21)
        self.api.processes[201].running = False
        copied = self.root / 'copy.exe'
        shutil.copyfile(self.context.version_dir / MANIFEST.NATIVE_EXE, copied)
        self.api.pipes[self.api.profile_key(profile)] = self.api.add_process(202, copied)
        self.assertEqual(self.run_preflight()[0], 21)
        self.assertFalse(any(call[0] == 'stop' for call in self.api.calls))

    def test_stop_zero_without_exit_and_partial_stop_failure_preserve_launcher(self):
        old = (self.install / 'session-panel.cmd').read_bytes()
        self.profile('one', pid=201)
        self.api.stop_exits = False
        code, report = self.run_preflight()
        self.assertEqual(code, 22)
        self.assertEqual(report['reason'], 'native_stop_not_exited')
        self.assertEqual((self.install / 'session-panel.cmd').read_bytes(), old)
        self.assertFalse(any(call[0] == 'exclusive' for call in self.api.calls))

    def test_exit_23_24_25_26_and_missing_native_are_explicit(self):
        self.api.exclusive_error = True
        self.assertEqual(self.run_preflight()[0], 23)
        self.api.exclusive_error = False
        self.api.busy = True
        self.assertEqual(self.run_preflight()[0], 25)
        self.api.busy = False
        (self.context.version_dir / MANIFEST.NATIVE_EXE).unlink()
        self.assertEqual(self.run_preflight()[0], 21)
        with patch.object(REGISTRY, 'load_records', side_effect=OSError('read failure')):
            self.assertEqual(self.run_preflight()[0], 26)
        with patch.object(REGISTRY, 'load_records', side_effect=ValueError('bad record')):
            self.assertEqual(self.run_preflight()[0], 24)

    def invocation(self, *, uninstall=False):
        destination = self.root / '{app}' / 'versions' / self.context.version_id
        shutil.copytree(self.context.version_dir, destination)
        setup = self.root / 'setup.exe'
        setup.write_bytes(b'Inno fixture')
        parent = self.api.add_process(500, setup)
        if uninstall:
            original = self.api.add_process(501, self.install / 'unins000.exe')
            parent.identity['parent_pid'] = 501
        self.api.add_process(999, destination / 'runtime/python.exe', parent=500)
        self.api.current_pid = 999
        context = MAINTENANCE.prepare_invocation(self.install, 'uninstall' if uninstall else 'upgrade', api=self.api)
        return context

    def test_exact_inno_two_stage_chain_does_not_wait_or_lock_own_dat(self):
        invocation = self.invocation(uninstall=True)
        code, report = self.run_preflight('uninstall', invocation=invocation)
        self.assertEqual(code, 0)
        self.assertEqual(self.api.mutex_waits, 0)
        self.assertEqual(self.api.mutex_releases, 0)
        self.assertTrue(self.api.processes[500].running)
        self.assertTrue(self.api.processes[501].running)
        self.api.add_process(777, self.context.version_dir / 'runtime/python.exe')
        self.assertEqual(self.run_preflight('uninstall', invocation=invocation)[0], 21)

    def test_fake_parent_pid_reuse_or_bootstrap_tamper_never_skips_lock(self):
        invocation = self.invocation()
        fake = copy.deepcopy(invocation)
        fake['installer']['created'] += 1
        self.assertEqual(self.run_preflight(invocation=fake)[0], 21)
        self.assertEqual(self.api.mutex_waits, 0)
        (Path(invocation['bootstrap']) / 'runtime/python313.dll').write_bytes(b'tampered')
        self.assertEqual(self.run_preflight(invocation=invocation)[0], 24)

    def test_new_install_receipt_required_and_unknown_data_preserved(self):
        target = self.root / 'new install'
        payload = self.context.version_dir
        report_path = self.root / 'initial.json'
        self.assertEqual(MAINTENANCE.preflight(target, 'install', report_path, api=self.api, payload=payload), 0)
        receipt = REGISTRY.read_record(report_path)
        destination = target / 'versions' / self.context.version_id
        shutil.copytree(payload, destination)
        (target / 'unins000.exe').write_bytes(b'new Inno fixture')
        (target / 'unins000.dat').write_bytes(b'Inno DAT')
        context = REGISTRY.ProductContext.load(destination)
        with self.assertRaises(MAINTENANCE.Blocked):
            MAINTENANCE.commit_install(context, target, api=self.api)
        result = MAINTENANCE.commit_install(context, target, api=self.api, receipt=receipt)
        self.assertEqual(result['status'], 'committed')
        self.assertIn(self.context.version_id.encode(), (target / 'session-panel.cmd').read_bytes())
        (target / 'user notes.txt').write_bytes(b'unknown data')
        self.assertEqual(MAINTENANCE.preflight(target, 'uninstall', self.root / 'after.json', api=self.api), 0)
        self.assertEqual((target / 'user notes.txt').read_bytes(), b'unknown data')

    def test_upgrade_commit_requires_unchanged_launcher_and_activates_last(self):
        code, receipt = self.run_preflight(payload=self.context.version_dir)
        self.assertEqual(code, 0)
        launcher = self.install / 'session-panel.cmd'
        original = launcher.read_bytes()
        launcher.write_bytes(b'changed by someone else')
        with self.assertRaises(MAINTENANCE.Blocked):
            MAINTENANCE.commit_install(self.context, self.install, api=self.api, receipt=receipt)
        self.assertEqual(launcher.read_bytes(), b'changed by someone else')
        launcher.write_bytes(original)
        MAINTENANCE.commit_install(self.context, self.install, api=self.api, receipt=receipt)
        backups = list((self.install / '.install/backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_read_only_prepare_projection_and_report_no_overwrite(self):
        context = self.invocation()
        report = self.root / 'context.json'
        before = (self.install / 'session-panel.cmd').read_bytes()
        MAINTENANCE.report_file(report, context)
        self.assertIn('[mutex]', report.with_suffix('.ini').read_text(encoding='utf-8'))
        self.assertEqual(self.api.mutex_waits, 0)
        self.assertEqual((self.install / 'session-panel.cmd').read_bytes(), before)
        with self.assertRaises(MAINTENANCE.Blocked): MAINTENANCE.report_file(report, context)

    def test_partial_stop_receipt_and_report_prevalidation_keep_observations(self):
        self.profile('one', pid=201)
        self.profile('two', pid=202)
        command = self.api.native_command
        count = 0
        def partial(binary, action, profile):
            nonlocal count
            if action == 'stop':
                count += 1
                if count == 2:
                    self.api.stop_code = 1
            return command(binary, action, profile)
        with patch.object(self.api, 'native_command', side_effect=partial):
            code, receipt = self.run_preflight()
        self.assertEqual(code, 22)
        self.assertEqual(len(receipt['stopped']), 1)
        before = len(self.api.calls)
        with self.assertRaises(MAINTENANCE.Blocked):
            MAINTENANCE.preflight(self.install, 'upgrade', self.install / 'bad-report.json', api=self.api)
        self.assertEqual(len(self.api.calls), before)
        self.assertFalse((self.install / 'bad-report.json').exists())

    def test_uninstall_commit_removes_only_exact_management_files(self):
        note = self.install / 'user note.txt'
        note.write_bytes(b'keep me')
        code, receipt = self.run_preflight('uninstall', payload=self.context.version_dir)
        self.assertEqual(code, 0)
        result = MAINTENANCE.commit_install(self.context, self.install, api=self.api, receipt=receipt)
        self.assertEqual(result['status'], 'committed')
        self.assertFalse((self.install / '.install').exists())
        self.assertFalse((self.install / 'session-panel.cmd').exists())
        self.assertTrue((self.context.version_dir / 'product.json').exists())
        self.assertTrue((self.install / 'unins000.exe').exists())
        self.assertTrue((self.install / 'unins000.dat').exists())
        self.assertEqual(note.read_bytes(), b'keep me')

    def test_commit_rechecks_new_cooperative_http_for_every_operation(self):
        profile = self.profile()
        for operation in ('install', 'upgrade', 'rollback', 'uninstall'):
            with self.subTest(operation=operation):
                code, receipt = self.run_preflight(operation, payload=self.context.version_dir)
                self.assertEqual(code, 0)
                self.api.add_process(100, self.context.version_dir / 'runtime/python.exe')
                self.api.current_pid = 100
                lease = REGISTRY.register_http(self.context, profile, api=self.api)
                self.api.current_pid = 999
                before = self.installation_bytes()
                try:
                    with self.assertRaises(MAINTENANCE.Blocked) as caught:
                        MAINTENANCE.commit_install(self.context, self.install, api=self.api, receipt=receipt)
                    self.assertEqual(caught.exception.code, 20)
                    self.assertEqual(self.installation_bytes(), before)
                finally:
                    lease.close()
                    self.api.processes[100].running = False

    def test_commit_blocks_new_native_without_stopping_it_for_every_operation(self):
        profile = self.profile()
        for operation in ('install', 'upgrade', 'rollback', 'uninstall'):
            with self.subTest(operation=operation):
                code, receipt = self.run_preflight(operation, payload=self.context.version_dir)
                self.assertEqual(code, 0)
                peer = self.api.add_process(201, self.context.version_dir / MANIFEST.NATIVE_EXE)
                self.api.pipes[self.api.profile_key(profile)] = peer
                before, calls = self.installation_bytes(), len(self.api.calls)
                try:
                    with self.assertRaises(MAINTENANCE.Blocked) as caught:
                        MAINTENANCE.commit_install(self.context, self.install, api=self.api, receipt=receipt)
                    self.assertEqual(caught.exception.code, 21)
                    self.assertTrue(peer.running)
                    self.assertEqual(self.api.calls[calls:], [])
                    self.assertEqual(self.installation_bytes(), before)
                finally:
                    peer.running = False

    def test_commit_rechecks_asset_occupancy_for_every_existing_operation(self):
        for operation in ('install', 'upgrade', 'rollback', 'uninstall'):
            with self.subTest(operation=operation):
                code, receipt = self.run_preflight(operation, payload=self.context.version_dir)
                self.assertEqual(code, 0)
                before = self.installation_bytes()
                self.api.exclusive_error = True
                try:
                    with self.assertRaises(MAINTENANCE.Blocked) as caught:
                        MAINTENANCE.commit_install(self.context, self.install, api=self.api, receipt=receipt)
                    self.assertEqual(caught.exception.code, 23)
                    self.assertEqual(self.installation_bytes(), before)
                finally:
                    self.api.exclusive_error = False

    def test_initial_commit_blocks_new_runtime_native_and_asset_occupancy_before_records(self):
        for kind in ('runtime', 'native', 'assets'):
            with self.subTest(kind=kind):
                target = self.root / ('initial ' + kind)
                report = self.root / ('initial-' + kind + '.json')
                self.assertEqual(MAINTENANCE.preflight(target, 'install', report, api=self.api, payload=self.context.version_dir), 0)
                receipt = REGISTRY.read_record(report)
                destination = target / 'versions' / self.context.version_id
                shutil.copytree(self.context.version_dir, destination)
                (target / 'unins000.exe').write_bytes(b'Inno fixture')
                (target / 'unins000.dat').write_bytes(b'Inno data')
                context = REGISTRY.ProductContext.load(destination)
                before = self.installation_bytes(target)
                peer = None
                if kind == 'assets':
                    self.api.exclusive_error = True
                else:
                    relative = 'runtime/python.exe' if kind == 'runtime' else MANIFEST.NATIVE_EXE
                    peer = self.api.add_process(301, destination / relative)
                try:
                    with self.assertRaises(MAINTENANCE.Blocked) as caught:
                        MAINTENANCE.commit_install(context, target, api=self.api, receipt=receipt)
                    self.assertEqual(caught.exception.code, 23 if kind == 'assets' else 21)
                    self.assertEqual(self.installation_bytes(target), before)
                    self.assertFalse((target / '.install').exists())
                    self.assertFalse((target / 'session-panel.cmd').exists())
                    if kind == 'assets':
                        assets = self.api.calls[-1][1]
                        self.assertIn(destination / 'product.json', assets)
                        self.assertIn(destination / 'runtime/python.exe', assets)
                        self.assertNotIn(target / 'unins000.exe', assets)
                        self.assertNotIn(target / 'unins000.dat', assets)
                finally:
                    self.api.exclusive_error = False
                    if peer is not None: peer.running = False

    def test_parent_held_commit_rechecks_unknown_runtime_without_wait_or_stop(self):
        invocation = self.invocation()
        code, receipt = self.run_preflight(invocation=invocation)
        self.assertEqual(code, 0)
        self.api.add_process(777, self.context.version_dir / 'runtime/python.exe')
        before, calls = self.installation_bytes(), len(self.api.calls)
        with self.assertRaises(MAINTENANCE.Blocked) as caught:
            MAINTENANCE.commit_install(self.context, self.install, api=self.api, receipt=receipt, invocation=invocation)
        self.assertEqual(caught.exception.code, 21)
        self.assertEqual(self.api.mutex_waits, 0)
        self.assertEqual(self.api.mutex_releases, 0)
        self.assertEqual(self.api.calls[calls:], [])
        self.assertEqual(self.installation_bytes(), before)
