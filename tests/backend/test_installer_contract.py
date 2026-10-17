"""Fixture-only installer contracts; no compiler, EXE, processes or user profiles."""

import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from tests.backend.test_windows_bundle import BUILD, MANIFEST, PayloadFixture, SOURCE


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('installer_include_fixture', ROOT / 'tools/packaging/inno_include.py')
INNO = importlib.util.module_from_spec(spec)
spec.loader.exec_module(INNO)
SCRIPT = (ROOT / 'tools/packaging/session-panel.iss').read_text(encoding='utf-8-sig')


def body(name):
    match = re.search(r'(?m)^(?:function|procedure) ' + name + r'\b', SCRIPT)
    if not match:
        raise AssertionError('missing_script_function: ' + name)
    remainder = SCRIPT[match.end():]
    following = re.search(r'(?m)^(?:function|procedure) \w+\b', remainder)
    return remainder[:following.start()] if following else remainder


class InstallerIncludesTests(PayloadFixture):
    def setUp(self):
        super().setUp()
        prepared = patch.object(INNO.BUILD, 'contract', return_value=MANIFEST)
        prepared.start()
        self.addCleanup(prepared.stop)

    def payload(self):
        stage, _document = super().payload()
        entry = stage / INNO.BOOTSTRAP_ENTRY
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(b'# synthetic maintenance entry\n')
        return stage, BUILD.write_product_manifest(stage, '0.1.0a1', SOURCE)

    def test_exact_files_and_compiled_hash_table_include_the_manifest_itself(self):
        stage, document = self.payload()
        output = self.root / 'owned include output'
        receipt = INNO.generate_includes(stage, output)
        rows = {row['path']: row for row in receipt['files']}
        self.assertEqual(set(rows), {row['path'] for row in document['files']} | {'product.json'})
        self.assertEqual(rows['product.json']['sha256'], MANIFEST.sha256(stage / 'product.json'))
        self.assertEqual(receipt['version_id'], '0.1.0a1-' + document['closure'][:16])
        entries = (output / 'payload-files.iss').read_text(encoding='utf-8-sig')
        actual = [line for line in entries.splitlines() if line.startswith('Source:')]
        self.assertEqual(len(actual), len(rows))
        self.assertTrue(all('DestDir: "{app}\\versions\\' + receipt['version_id'] in line for line in actual))
        self.assertTrue(all('Check: ShouldInstallPayload' in line for line in actual))
        self.assertNotIn('DestDir: "{app}"', entries)
        self.assertNotIn('recursesubdirs', entries)
        self.assertNotIn('*', entries)
        table = (output / 'payload-hashes.iss').read_text(encoding='utf-8-sig')
        paths = [value.replace("''", "'").replace('\\', '/') for value in re.findall(r"PayloadPaths\[\d+\] := '((?:''|[^'])*)';", table)]
        hashes = re.findall(r"PayloadHashes\[\d+\] := '([0-9a-f]{64})';", table)
        self.assertEqual(paths, sorted(rows))
        self.assertEqual(hashes, [rows[name]['sha256'] for name in sorted(rows)])
        for name, expected in receipt['includes'].items():
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), expected)

    def test_unicode_spaces_and_pascal_quotes_are_data_in_the_exact_table(self):
        stage, _document = self.payload()
        name = "runtime/Lib/site-packages/example/资料's.txt"
        extra = stage / name
        extra.parent.mkdir(parents=True)
        extra.write_bytes(b'fixture bytes')
        BUILD.write_product_manifest(stage, '0.1.0a1', SOURCE)
        output = self.root / '包含 空格'
        INNO.generate_includes(stage, output)
        table = (output / 'payload-hashes.iss').read_text(encoding='utf-8-sig')
        self.assertIn("资料''s.txt", table)
        self.assertEqual(INNO.pascal_string("a'b"), "'a''b'")
        for invalid in ('x".txt', 'x{app}.txt', 'x\ny.txt', 'x\0y.txt'):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'invalid_installer_path'):
                INNO.inno_path(invalid)

    def test_missing_bootstrap_tampered_payload_and_extra_files_fail_before_output(self):
        stage, document = super().payload()
        output = self.root / 'no-output'
        with self.assertRaisesRegex(ValueError, 'bootstrap_entry_missing'):
            INNO.generate_includes(stage, output)
        self.assertFalse(output.exists())
        entry = stage / INNO.BOOTSTRAP_ENTRY
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(b'fixture')
        BUILD.write_product_manifest(stage, '0.1.0a1', SOURCE)
        original = (stage / 'runtime/python.exe').read_bytes()
        (stage / 'runtime/python.exe').write_bytes(b'wrong')
        with self.assertRaises(MANIFEST.ManifestError):
            INNO.generate_includes(stage, output)
        self.assertFalse(output.exists())
        (stage / 'runtime/python.exe').write_bytes(original)
        (stage / 'unknown.txt').write_bytes(b'unlisted')
        with self.assertRaises(MANIFEST.ManifestError):
            INNO.generate_includes(stage, output)
        self.assertFalse(output.exists())

    def test_only_new_absolute_separate_outputs_are_allowed(self):
        stage, _document = self.payload()
        for output in (Path('relative'), stage, stage / 'include'):
            with self.subTest(output=output), self.assertRaises(ValueError):
                INNO.generate_includes(stage, output)
        existing = self.root / 'existing'
        existing.mkdir()
        with self.assertRaises(ValueError):
            INNO.generate_includes(stage, existing)
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(INNO.main(['--payload', str(stage), '--output', str(existing)]), 1)
        self.assertIn('installer_include_failed', captured.getvalue())


class InstallerScriptTests(unittest.TestCase):
    def test_single_user_native_target_and_no_generic_shutdown_or_deletion(self):
        for directive in ('PrivilegesRequired=lowest', 'SetupArchitecture=x64', 'ArchitecturesAllowed=x64os',
                          'CloseApplications=no', 'RestartApplications=no', 'UninstallLogMode=append',
                          'DefaultDirName={localappdata}\\Programs\\SessionPanel'):
            self.assertIn(directive, SCRIPT)
        for forbidden in ('taskkill', 'TerminateProcess', 'RestartManager', 'restartreplace',
                          'deleteafterinstall', '[UninstallDelete]', '[InstallDelete]', 'DelTree', '[Run]'):
            self.assertNotIn(forbidden, SCRIPT)
        self.assertIn('Name: "{userprograms}\\Session Panel"', SCRIPT)
        self.assertNotIn('Filename: "python.exe"', SCRIPT)
        self.assertNotIn('current.json', SCRIPT)

    def test_bootstrap_hashes_precede_prepare_mutex_and_preflight(self):
        prepare = body('PrepareToInstall')
        positions = [prepare.index(value) for value in (
            'ExtractTemporaryFiles', 'VerifyTable(BootstrapRoot)', "RunMaintenance('prepare'",
            'AcquireMaintenance', "RunMaintenance('preflight'", 'Prepared := True')]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("+ '{app}\\versions\\{#PayloadId}'", prepare)
        self.assertIn("if not InstallPayload then begin", prepare)
        self.assertIn("RunMaintenance('verify', NewReport('existing')", prepare)
        self.assertIn('Result := GetExceptionMessage', prepare)
        after = body('CurStepChanged')
        self.assertLess(after.index("RunMaintenance('verify'"), after.index("RunMaintenance('commit'"))
        self.assertIn('ssPostInstall', after)
        self.assertIn('PreflightReceipt', after)
        self.assertNotIn('session-panel.cmd', after)

    def test_only_parent_acquires_and_releases_the_actual_mutex(self):
        acquire = body('AcquireMaintenance')
        self.assertLess(acquire.index('ConvertSecurityDescriptor'), acquire.index('NativeCreateMutex'))
        self.assertLess(acquire.index('NativeCreateMutex'), acquire.index('NativeWait'))
        self.assertIn('NativeWait(MaintenanceHandle, 0)', acquire)
        self.assertIn('Attributes.Length <> 24', acquire)
        self.assertIn('NativeReleaseMutex', body('ReleaseMaintenance'))
        self.assertIn('ReleaseMaintenance', body('DeinitializeSetup'))
        self.assertIn('ReleaseMaintenance', body('DeinitializeUninstall'))
        run = body('RunMaintenance')
        self.assertNotIn('NativeWait', run)
        self.assertNotIn('NativeReleaseMutex', run)
        self.assertIn('--invocation-file', run)
        self.assertNotIn('skip-lock', run)
        self.assertLess(run.index('VerifyTable(BootstrapRoot)'), run.index('Started := Exec'))
        self.assertIn("+ 'runtime\\python.exe'", run)
        self.assertIn("'-I -B -X utf8 -m session_panel.installation.maintenance '", run)

    def test_nonzero_or_wrong_reports_cannot_reach_next_install_stage(self):
        run = body('RunMaintenance')
        self.assertIn('if ExitCode <> 0 then begin', run)
        self.assertIn('RaiseException', run)
        self.assertIn('CheckReport(Report, ExpectedStatus)', run)
        check = body('CheckReport')
        for field in ('status', 'operation', 'version_id', 'closure'):
            self.assertIn("'" + field + "'", check)
        self.assertLess(run.index('if ExitCode <> 0'), run.index('CheckReport(Report, ExpectedStatus)'))
        for exit_code in range(20, 27):
            self.assertIn(str(exit_code) + ': RaiseException', run)
        stages = re.findall(r"RunMaintenance\('([^']+)'", body('CurStepChanged'))
        self.assertEqual(stages, ['verify', 'commit'])

    def test_missing_parent_creation_is_limited_to_the_default_user_programs(self):
        parent = body('EnsureDefaultParent')
        self.assertIn("ExpandConstant('{localappdata}')", parent)
        self.assertIn("+ 'Programs'", parent)
        self.assertIn("+ 'SessionPanel'", parent)
        self.assertIn('CreateDir(DefaultParent)', parent)
        self.assertNotIn('ForceDirectories', parent)
        self.assertNotIn('CreateDir(App)', parent)
        self.assertLess(body('PrepareToInstall').index('EnsureDefaultParent'),
                        body('PrepareToInstall').index("RunMaintenance('prepare'"))

    def test_success_exit_requires_committed_launcher(self):
        self.assertIn('InstallCommitted := True', body('CurStepChanged'))
        self.assertIn('if not InstallCommitted then Result := 26', body('GetCustomSetupExitCode'))

    def test_uninstall_copies_only_compiled_assets_and_never_locks_inno_files(self):
        uninstall = body('InitializeUninstall')
        positions = [uninstall.index(value) for value in ('RegQueryStringValue', 'CopyBoundUninstallBootstrap',
                     "RunMaintenance('prepare'", 'AcquireMaintenance', "RunMaintenance('preflight'")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("ExpandConstant('{uninstallexe}')", uninstall)
        self.assertNotIn("RunMaintenance('commit'", uninstall)
        confirmed = body('CurUninstallStepChanged')
        self.assertIn('CurUninstallStep = usUninstall', confirmed)
        self.assertIn("RunMaintenance('commit'", confirmed)
        self.assertIn('not Prepared or not MaintenanceHeld', confirmed)
        copy = body('CopyBoundUninstallBootstrap')
        self.assertIn('GetArrayLength(PayloadPaths)', copy)
        self.assertIn('CopyFile(Source, Destination, True)', copy)
        self.assertLess(copy.index('CopyFile('), copy.index('GetSHA256OfFile(Destination)'))
        self.assertIn('VerifyTable(BootstrapRoot)', copy)
        for forbidden in ('FindFirst', '*.exe', '*.dat', '*.msg', 'native_missing', 'system32', 'profile', 'DeleteFile'):
            self.assertNotIn(forbidden, copy)
        self.assertNotIn('ExtractTemporaryFiles', uninstall)
        self.assertIn('Result := False', uninstall)
