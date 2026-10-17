"""Offline fixture contracts; never execute cargo, pip, Python payloads or an EXE."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MANIFEST = load('bundle_manifest_fixture', ROOT / 'src/session_panel/installation/manifest.py')
BUILD = load('windows_bundle_fixture', ROOT / 'tools/packaging/build_windows.py')
NOTICES = load('bundle_notices_fixture', ROOT / 'tools/packaging/notices.py')
OBSERVER = load('bundle_observer_fixture', ROOT / 'scripts/build_observer.py')
SOURCE = {'commit': 'a' * 40, 'dirty': False, 'files_hash': 'b' * 64}


def wheel(directory, name='example-dep', version='1.0', requires=()):
    distribution = name.replace('-', '_')
    info = f'{distribution}-{version}.dist-info'
    path = directory / f'{distribution}-{version}-py3-none-any.whl'
    contents = {distribution + '/__init__.py': b'# fixture\n',
                info + '/METADATA': (f'Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nLicense-File: LICENSE.txt\n'
                                      + ''.join(f'Requires-Dist: {value}\n' for value in requires)).encode(),
                info + '/licenses/LICENSE.txt': b'Example license text.\n'}
    rows = [(key, 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode(), str(len(raw)))
            for key, raw in contents.items()]
    rows.append((info + '/RECORD', '', ''))
    stream = io.StringIO()
    csv.writer(stream, lineterminator='\n').writerows(rows)
    contents[info + '/RECORD'] = stream.getvalue().encode()
    with zipfile.ZipFile(path, 'w') as archive:
        for key, value in contents.items():
            archive.writestr(key, value)
    return path


class PayloadFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.contract_patch = patch.object(BUILD, 'contract', return_value=MANIFEST)
        self.contract_patch.start()
        self.addCleanup(self.contract_patch.stop)

    def payload(self):
        stage = self.root / 'payload'
        stage.mkdir()
        for name in MANIFEST.BASE_FILES | MANIFEST.NATIVE_FILES:
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
        native = {'protocol': 2, 'build': 'c' * 64, 'toolchain': OBSERVER.TOOLCHAIN,
                  'binary': 'session-panel-observer.exe', 'sha256': MANIFEST.sha256(stage / MANIFEST.NATIVE_EXE)}
        (stage / MANIFEST.NATIVE_MANIFEST).write_text(json.dumps(native), encoding='utf-8')
        document = BUILD.write_product_manifest(stage, '0.1.0a1', SOURCE)
        return stage, document

    def publish(self, stage, document):
        document['closure'] = MANIFEST.manifest_closure(document)
        (stage / 'product.json').write_bytes(MANIFEST.canonical_json(document))


class ManifestTests(PayloadFixture):
    def test_complete_closure_and_deterministic_version_identity(self):
        stage, document = self.payload()
        self.assertEqual(MANIFEST.verify_payload(stage, strict_native=True), document)
        self.assertEqual(MANIFEST.manifest_closure(document), document['closure'])
        self.assertEqual([row['path'] for row in document['files']], sorted(row['path'] for row in document['files']))

    def test_missing_or_corrupt_native_is_runtime_degradation_but_build_failure(self):
        stage, document = self.payload()
        for name in MANIFEST.NATIVE_FILES:
            (stage / name).unlink()
        self.assertEqual(MANIFEST.verify_payload(stage), document)
        with self.assertRaisesRegex(MANIFEST.ManifestError, 'missing_asset'):
            MANIFEST.verify_payload(stage, strict_native=True)
        (stage / MANIFEST.NATIVE_MANIFEST).write_bytes(b'wrong native manifest')
        self.assertEqual(MANIFEST.verify_payload(stage), document)
        with self.assertRaises(MANIFEST.ManifestError):
            MANIFEST.verify_payload(stage, strict_native=True)

    def test_base_mismatch_and_unknown_file_or_directory_are_never_degraded(self):
        stage, _document = self.payload()
        extra = stage / 'unlisted.txt'
        extra.write_bytes(b'unknown')
        with self.assertRaisesRegex(MANIFEST.ManifestError, 'unexpected_asset'):
            MANIFEST.verify_payload(stage)
        extra.unlink()
        extra.mkdir()
        with self.assertRaisesRegex(MANIFEST.ManifestError, 'unexpected_asset'):
            MANIFEST.verify_payload(stage)
        extra.rmdir()
        (stage / 'runtime/python.exe').write_bytes(b'tampered')
        with self.assertRaisesRegex(MANIFEST.ManifestError, 'asset_mismatch'):
            MANIFEST.verify_payload(stage)

    def test_strict_fields_duplicate_json_and_closure_tamper_fail(self):
        stage, document = self.payload()
        with self.assertRaisesRegex(MANIFEST.ManifestError, 'duplicate_key'):
            MANIFEST.strict_json('{"schema":1,"schema":1}')
        document['unknown'] = True
        self.publish(stage, document)
        with self.assertRaisesRegex(MANIFEST.ManifestError, 'manifest_fields'):
            MANIFEST.verify_payload(stage)
        del document['unknown']
        document['source']['dirty'] = True
        (stage / 'product.json').write_text(json.dumps(document), encoding='utf-8')
        with self.assertRaisesRegex(MANIFEST.ManifestError, 'closure_mismatch'):
            MANIFEST.verify_payload(stage)

    def test_paths_case_collisions_file_directory_collisions_and_bool_sizes_fail(self):
        _stage, original = self.payload()
        for value in ('../escape', '/root', 'C:/example/root', 'a\\b', '.', 'x/../y', 'a//b', 'NUL.txt', 'file:stream', 'tail.'):
            with self.subTest(value=value), self.assertRaises(MANIFEST.ManifestError):
                MANIFEST.relative_path(value)
        for rows in ([{'path': 'Runtime/extra.txt', 'size': 1, 'sha256': 'a' * 64}],
                     [{'path': 'runtime', 'size': 1, 'sha256': 'a' * 64}]):
            document = json.loads(json.dumps(original))
            document['files'] = sorted(document['files'] + rows, key=lambda row: row['path'])
            document['closure'] = MANIFEST.manifest_closure(document)
            with self.assertRaises(MANIFEST.ManifestError):
                MANIFEST.validate_manifest(document)
        original['files'][0]['size'] = True
        original['closure'] = MANIFEST.manifest_closure(original)
        with self.assertRaisesRegex(MANIFEST.ManifestError, 'file_identity'):
            MANIFEST.validate_manifest(original)

    def test_reparse_asset_is_refused_even_when_native_is_degraded(self):
        stage, _document = self.payload()
        real_stat = Path.lstat
        native_path = stage / MANIFEST.NATIVE_EXE
        def lstat(path):
            value = real_stat(path)
            if path == native_path:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value
        with patch.object(Path, 'lstat', lstat), self.assertRaisesRegex(MANIFEST.ManifestError, 'linked_path'):
            MANIFEST.verify_payload(stage)

    def test_product_native_pin_and_exact_five_keys_are_both_enforced(self):
        stage, document = self.payload()
        native = json.loads((stage / MANIFEST.NATIVE_MANIFEST).read_text())
        native['extra'] = 'not allowed'
        (stage / MANIFEST.NATIVE_MANIFEST).write_text(json.dumps(native), encoding='utf-8')
        document = BUILD.write_product_manifest(stage, '0.1.0a1', SOURCE)
        with self.assertRaisesRegex(MANIFEST.ManifestError, 'native_manifest_fields'):
            MANIFEST.verify_payload(stage, strict_native=True)
        document['native']['manifest_sha256'] = 'd' * 64
        self.publish(stage, document)
        with self.assertRaisesRegex(MANIFEST.ManifestError, 'native_manifest_pin'):
            MANIFEST.verify_payload(stage)


class BuilderTests(PayloadFixture):
    def test_offline_install_commands_remove_only_generated_host_path_outputs(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            site = Path(command[command.index('--target') + 1])
            if not site.exists():
                info = site / 'example-1.0.dist-info'
                info.mkdir(parents=True)
                (info / 'direct_url.json').write_text('{"url":"file:///build-only"}')
                (info / 'RECORD').write_text('example-1.0.dist-info/direct_url.json,,\n../../bin/mcp.exe,,\nexample.py,,\n')
                (site / 'example.py').write_text('# immutable example\n')
                (site / 'bin').mkdir()
                (site / 'bin/mcp.exe').write_bytes(b'generated host launcher')
                (site / 'pywin32.pth').write_text('import pywin32_bootstrap\n')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        site = BUILD.install_wheels(runtime, self.root / 'product.whl', self.root / 'house', self.root / 'lock', runner)
        self.assertEqual(len(calls), 2)
        for command in calls:
            self.assertTrue({'--no-index', '--no-deps', '--no-compile', '--platform', '--abi', '--target'}.issubset(command))
            self.assertEqual(command[command.index('--python-version') + 1], '3.13')
        self.assertIn('--require-hashes', calls[0])
        self.assertFalse((site / 'bin').exists())
        self.assertFalse((site / 'example-1.0.dist-info/direct_url.json').exists())
        self.assertEqual((site / 'example-1.0.dist-info/RECORD').read_text(), 'example.py,,\n')
        self.assertTrue((site / 'pywin32.pth').is_file())
        self.assertIn('sys.dont_write_bytecode = True', (site / 'sitecustomize.py').read_text())

    def test_compile_command_uses_checked_hash_and_virtual_filenames(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        BUILD.compile_runtime(self.root / 'runtime', self.root, runner)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:5], ['-I', '-B', '-X', 'utf8'])
        code = calls[0][-1]
        self.assertIn('CHECKED_HASH', code)
        self.assertIn("prependdir='session-panel'", code)
        self.assertIn('stripdir=str(root)', code)
        self.assertNotIn(str(self.root), code)

    def test_full_fixture_assembly_uses_one_manifest_and_preserves_no_sidecar(self):
        product = wheel(self.root, 'session-panel', '0.1.0a1', ['example-dep==1.0'])
        house = self.root / 'house'
        house.mkdir()
        dependency = wheel(house)
        lock = {'example-dep': {'version': '1.0', 'sha256': MANIFEST.sha256(dependency)}}
        archive = self.root / 'runtime.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            for name in BUILD.RUNTIME_FILES:
                output.writestr(name, b'fixture upstream asset')
        license_file = self.root / 'inno-license'
        license_file.write_text('Example installer license.')
        crate = self.root / 'crate'
        crate.mkdir()
        (crate / 'Cargo.toml').write_text('[package]\nname="example"\n')
        (crate / 'LICENSE').write_text('Example crate license.')
        cargo = self.root / 'cargo.json'
        cargo.write_text(json.dumps([{'name': 'example', 'version': '1.0', 'license': 'MIT',
                                      'manifest_path': str(crate / 'Cargo.toml'), 'license_file': None}]))
        prepared = self.root / 'release-install'
        prepared.mkdir()
        native = prepared / 'session-panel-observer.exe'
        native.write_bytes(b'MZ fixture release')
        (prepared / 'manifest.json').write_text(json.dumps({'protocol': 2, 'build': 'c' * 64,
            'toolchain': OBSERVER.TOOLCHAIN, 'binary': native.name, 'sha256': MANIFEST.sha256(native)}))
        build_info = {'schema': 1, 'mode': 'release', 'flags': ['--manifest-path', 'native/observer/Cargo.toml', '--locked', '--release'],
                      'rustflags': '-C target-feature=+crt-static', 'encoded_rustflags': '', 'source_hash': 'c' * 64, 'inputs': []}
        (prepared / 'build-info.json').write_text(json.dumps(build_info))
        producer = SimpleNamespace(source_hash=lambda: 'c' * 64, production_inputs=lambda: [], TOOLCHAIN=OBSERVER.TOOLCHAIN)
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            if '--target' in command:
                site = Path(command[command.index('--target') + 1])
                (site / 'session_panel').mkdir(parents=True, exist_ok=True)
                (site / 'session_panel/__init__.py').write_text('# fixture\n')
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            result = {'environment': {'python_version': '3.13', 'sys_platform': 'win32'}, 'distributions': [
                {'name': 'session-panel', 'version': '0.1.0a1', 'requires': ['example-dep==1.0']},
                {'name': 'example-dep', 'version': '1.0', 'requires': []},
            ]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr='')
        stage = self.root / 'completed'
        with patch.object(BUILD, 'source_identity', return_value=(dict(SOURCE), [])), \
                patch.object(BUILD, 'verify_product_sources') as source_binding, \
                patch.object(BUILD, 'product_version', return_value='0.1.0a1'), \
                patch.object(BUILD, 'locked_requirements', return_value=lock), \
                patch.object(BUILD, 'observer_builder', return_value=producer), \
                patch.dict(BUILD.DOWNLOADS['python'], {'sha256': MANIFEST.sha256(archive)}):
            receipt = BUILD.build_payload(product, house, archive, prepared, stage, cargo, license_file, runner=runner)
        document = MANIFEST.verify_payload(stage, strict_native=True)
        self.assertEqual(receipt['closure'], document['closure'])
        source_binding.assert_called_once_with(product, [], '0.1.0a1')
        self.assertEqual(receipt['version_id'], '0.1.0a1-' + document['closure'][:16])
        self.assertEqual(len(calls), 4)
        self.assertNotIn(str(self.root), (stage / 'BUILD-INPUTS.json').read_text())
        self.assertFalse((stage / 'native/observer/build-info.json').exists())

    def test_version_mapping_and_range(self):
        self.assertEqual(tuple(map(int, BUILD.version_info('0.1.0a1').split('.'))), (0, 1, 0, 1))
        self.assertEqual(tuple(map(int, BUILD.version_info('1.2.3').split('.'))), (1, 2, 3, 0))
        for value in ('0.1', '1.0.0+local', '65536.0.0', '0.1.0a10000', '0.1.0.post1'):
            with self.subTest(value=value), self.assertRaises(BUILD.BuildError):
                BUILD.version_info(value)

    def test_dirty_release_rejected_before_creating_output_or_executing_builds(self):
        responses = iter(['a' * 40 + '\n', ' M source.py\n'])
        runner = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=next(responses), stderr='')
        with self.assertRaisesRegex(BUILD.BuildError, 'dirty_release_source'):
            BUILD.build_payload(self.root / 'wheel', self.root / 'house', self.root / 'runtime',
                                self.root / 'native', self.root / 'output', self.root / 'cargo', self.root / 'license',
                                release=True, runner=runner)
        self.assertFalse((self.root / 'output').exists())

    def test_output_must_be_new_and_absolute(self):
        for value in (Path('relative'), self.root):
            with self.assertRaisesRegex(BUILD.BuildError, 'new_absolute_output_required'):
                BUILD.build_payload(value, value, value, value, value, value, value)

    def test_runtime_hash_and_zip_escape_fail_without_running_anything(self):
        path = self.root / 'runtime.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('../escape', b'bad')
        with self.assertRaisesRegex(BUILD.BuildError, 'runtime_archive_hash'):
            BUILD.prepare_runtime(path, self.root)
        with patch.dict(BUILD.DOWNLOADS['python'], {'sha256': MANIFEST.sha256(path)}), \
                self.assertRaises(MANIFEST.ManifestError):
            BUILD.prepare_runtime(path, self.root)
        self.assertFalse((self.root.parent / 'escape').exists())

    def test_real_wheel_record_and_lock_are_checked(self):
        product = wheel(self.root, 'session-panel', '0.1.0a1')
        house = self.root / 'house'
        house.mkdir()
        dependency = wheel(house)
        lock = {'example-dep': {'version': '1.0', 'sha256': MANIFEST.sha256(dependency)}}
        selected, identities = BUILD.verify_wheels(product, house, lock, '0.1.0a1')
        self.assertEqual(selected, [dependency])
        self.assertEqual({row['name'] for row in identities}, {'session-panel', 'example-dep'})
        lock['example-dep']['sha256'] = 'f' * 64
        with self.assertRaisesRegex(BUILD.BuildError, 'unlocked_wheel'):
            BUILD.verify_wheels(product, house, lock, '0.1.0a1')
        with self.assertRaisesRegex(BUILD.BuildError, 'product_wheel_version'):
            BUILD.verify_wheels(product, house, lock, '0.2.0')

    def test_wheel_record_corruption_and_wrong_abi_are_rejected(self):
        path = wheel(self.root)
        with zipfile.ZipFile(path) as archive:
            values = {name: archive.read(name) for name in archive.namelist()}
        values['example_dep/__init__.py'] = b'changed'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, value in values.items():
                archive.writestr(name, value)
        with self.assertRaisesRegex(BUILD.BuildError, 'wheel_record_mismatch'):
            BUILD.wheel_info(path)
        wrong = self.root / 'example_dep-1.0-cp312-cp312-win_amd64.whl'
        path.rename(wrong)
        with self.assertRaisesRegex(BUILD.BuildError, 'wheel_target_mismatch'):
            BUILD.wheel_info(wrong)

    def test_target_marker_extras_closure_uses_target_not_host(self):
        probe = {'environment': {'python_version': '3.13', 'sys_platform': 'win32'}, 'distributions': [
            {'name': 'session-panel', 'version': '0.1.0a1', 'requires': ['example-dep[cli]==1.0']},
            {'name': 'example-dep', 'version': '1.0', 'requires': ['leaf==2.0; extra == "cli" and python_version >= "3.13"']},
            {'name': 'leaf', 'version': '2.0', 'requires': []},
        ]}
        identities = [{'name': row['name'], 'version': row['version']} for row in probe['distributions']]
        BUILD.validate_target_metadata(probe, identities)
        probe['environment']['python_version'] = '3.12'
        with self.assertRaisesRegex(BUILD.BuildError, 'target_dependency_extra_distribution'):
            BUILD.validate_target_metadata(probe, identities)

    def test_native_release_copies_only_two_runtime_files_and_refuses_debug(self):
        prepared = self.root / 'release-install'
        prepared.mkdir()
        binary = prepared / 'session-panel-observer.exe'
        binary.write_bytes(b'MZ fixture release')
        native = {'protocol': 2, 'build': 'a' * 64, 'toolchain': OBSERVER.TOOLCHAIN,
                  'binary': binary.name, 'sha256': MANIFEST.sha256(binary)}
        (prepared / 'manifest.json').write_text(json.dumps(native))
        inputs = [{'path': 'native/observer/Cargo.toml', 'sha256': 'b' * 64}]
        provenance = {'schema': 1, 'mode': 'release', 'flags': ['--manifest-path', 'native/observer/Cargo.toml', '--locked', '--release'],
                      'rustflags': '-C target-feature=+crt-static', 'encoded_rustflags': '', 'source_hash': 'a' * 64, 'inputs': inputs}
        (prepared / 'build-info.json').write_text(json.dumps(provenance))
        producer = SimpleNamespace(source_hash=lambda: 'a' * 64, production_inputs=lambda: inputs, TOOLCHAIN=OBSERVER.TOOLCHAIN)
        stage = self.root / 'stage'
        stage.mkdir()
        with patch.object(BUILD, 'observer_builder', return_value=producer):
            BUILD.add_release_observer(prepared, stage)
            self.assertEqual({path.name for path in (stage / 'native/observer').iterdir()}, {binary.name, 'manifest.json'})
            for change in ({'mode': 'debug'}, {'rustflags': ''},
                           {'encoded_rustflags': '-C\x1ftarget-feature=-crt-static'}):
                (prepared / 'build-info.json').write_text(json.dumps({**provenance, **change}))
                with self.subTest(change=change), self.assertRaisesRegex(BUILD.BuildError, 'release_observer_identity'):
                    BUILD.release_observer_info(prepared)
        with self.assertRaisesRegex(BUILD.BuildError, 'release_observer_directory_required'):
            BUILD.release_observer_info(self.root / 'install')

    def test_lock_is_fixed_target_input_and_not_a_host_freeze(self):
        values = BUILD.locked_requirements(ROOT / 'tools/packaging/windows-cp313.lock')
        self.assertEqual(len(values), 31)
        self.assertEqual(values['mcp']['version'], '1.30.0')
        self.assertEqual(values['defusedxml']['version'], '0.7.1')


class ProductSourceTests(PayloadFixture):
    def sources(self):
        source = self.root / 'source'
        assets = BUILD.helper('build_assets').WEB_ASSETS
        files = {'src/session_panel/__init__.py': b'# current product\n',
                 'src/session_panel/installation/maintenance.py': b'# current maintenance\n',
                 'pyproject.toml': b'[project]\nname="session-panel"\nversion="0.1.0a1"\n'
                                  b'requires-python=">=3.12"\ndescription="Fixture"\nreadme="README.md"\n',
                 'requirements.txt': b'example-dep==1.0\n', 'README.md': 'Fixture 中文说明\n'.encode('utf-8'),
                 'tools/packaging/build_assets.py': (ROOT / 'tools/packaging/build_assets.py').read_bytes()}
        files.update({'web/' + name: ('current:' + name).encode() for name in assets})
        for name, raw in files.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        inputs = [{'path': name, 'sha256': hashlib.sha256(raw).hexdigest()} for name, raw in files.items()]
        contents = {name.removeprefix('src/'): raw for name, raw in files.items() if name.startswith('src/')}
        contents.update({'session_panel/_assets/web/' + name: files['web/' + name] for name in assets})
        info = 'session_panel-0.1.0a1.dist-info/'
        contents.update({info + 'METADATA': b'Metadata-Version: 2.4\nName: session-panel\nVersion: 0.1.0a1\n'
                         b'Summary: Fixture\nRequires-Python: >=3.12\nRequires-Dist: example-dep==1.0\n\n'
                         + 'Fixture 中文说明\n'.encode('utf-8'),
                         info + 'WHEEL': b'Wheel-Version: 1.0\nTag: py3-none-any\n',
                         info + 'top_level.txt': b'session_panel\n'})
        return source, inputs, contents

    def write_wheel(self, contents):
        record = 'session_panel-0.1.0a1.dist-info/RECORD'
        stream = io.StringIO()
        rows = [(name, 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode(), str(len(raw)))
                for name, raw in contents.items()]
        csv.writer(stream, lineterminator='\n').writerows([*rows, (record, '', '')])
        path = self.root / 'session_panel-0.1.0a1-py3-none-any.whl'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, raw in {**contents, record: stream.getvalue().encode()}.items():
                archive.writestr(name, raw)
        return path

    def test_current_source_wheel_is_accepted_and_self_consistent_stale_wheels_are_rejected(self):
        source, inputs, contents = self.sources()
        with patch.object(BUILD, 'ROOT', source):
            BUILD.verify_product_sources(self.write_wheel(contents), inputs, '0.1.0a1')
            for name in ('session_panel/__init__.py', 'session_panel/_assets/web/workspace/news.js',
                         'session_panel-0.1.0a1.dist-info/METADATA', 'session_panel-0.1.0a1.dist-info/top_level.txt'):
                raw = (contents[name].replace(b'example-dep==1.0', b'example-dep==0.9')
                       if name.endswith('/METADATA') else b'old but internally hash-consistent\n')
                stale = {**contents, name: raw}
                wheel_path = self.write_wheel(stale)
                BUILD.wheel_info(wheel_path)  # Valid RECORD alone cannot establish provenance.
                with self.subTest(name=name), self.assertRaises(BUILD.BuildError):
                    BUILD.verify_product_sources(wheel_path, inputs, '0.1.0a1')

    def test_missing_current_module_extra_module_and_changed_source_snapshot_are_rejected(self):
        source, inputs, contents = self.sources()
        old = {name: raw for name, raw in contents.items() if name != 'session_panel/installation/maintenance.py'}
        with patch.object(BUILD, 'ROOT', source):
            for changed in (old, {**contents, 'session_panel/unknown.py': b'# unowned\n'}):
                with self.assertRaisesRegex(BUILD.BuildError, 'product_source_closure'):
                    BUILD.verify_product_sources(self.write_wheel(changed), inputs, '0.1.0a1')
            (source / 'src/session_panel/__init__.py').write_bytes(b'# changed after snapshot\n')
            with self.assertRaisesRegex(BUILD.BuildError, 'product_source_changed'):
                BUILD.verify_product_sources(self.write_wheel(contents), inputs, '0.1.0a1')

    def test_source_rejection_precedes_output_creation_and_any_payload_command(self):
        source, inputs, contents = self.sources()
        stale = self.write_wheel({**contents, 'session_panel/__init__.py': b'# stale\n'})
        output = self.root / 'never-created'
        with patch.object(BUILD, 'ROOT', source), \
                patch.object(BUILD, 'source_identity', return_value=(dict(SOURCE), inputs)), \
                patch.object(BUILD, 'locked_requirements', return_value={}), \
                patch.object(BUILD, 'verify_wheels', return_value=([], [])), \
                self.assertRaisesRegex(BUILD.BuildError, 'product_source_mismatch'):
            BUILD.build_payload(stale, self.root, self.root / 'zip', self.root / 'native', output,
                                self.root / 'cargo', self.root / 'license',
                                runner=lambda *args, **kwargs: self.fail('payload command before source validation'))
        self.assertFalse(output.exists())


class ReleaseBuildTests(unittest.TestCase):
    def test_release_flag_selects_release_output_and_keeps_debug_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / 'target'
            for mode in ('release', 'debug'):
                (target / mode).mkdir(parents=True)
                (target / mode / 'session-panel-observer.exe').write_bytes(('MZ ' + mode).encode())
            install = root / 'work/native-observer/install'
            install.mkdir(parents=True)
            (install / 'keep.txt').write_bytes(b'debug sentinel')
            environment = {'CARGO_TARGET_DIR': str(target), 'RUSTFLAGS': '-C target-feature=-crt-static',
                           'CARGO_ENCODED_RUSTFLAGS': '-C\x1ftarget-feature=-crt-static'}
            with patch.object(OBSERVER, 'ROOT', root), \
                    patch.object(OBSERVER, 'prepared_environment', return_value=(environment, {'cargo': 'owned-cargo'})), \
                    patch.object(OBSERVER, 'source_hash', return_value='a' * 64), \
                    patch.object(OBSERVER, 'production_inputs', return_value=[]), \
                    patch.object(OBSERVER.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
                    patch('sys.argv', ['build_observer.py', 'build', '--release']):
                self.assertEqual(OBSERVER.main(), 0)
            self.assertIn('--release', run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs['env']['RUSTFLAGS'], '-C target-feature=+crt-static')
            self.assertNotIn('CARGO_ENCODED_RUSTFLAGS', run.call_args.kwargs['env'])
            release = root / 'work/native-observer/release-install'
            self.assertEqual((release / 'session-panel-observer.exe').read_bytes(), b'MZ release')
            self.assertEqual((install / 'keep.txt').read_bytes(), b'debug sentinel')
            self.assertEqual(set(json.loads((release / 'manifest.json').read_text())), {'protocol', 'build', 'toolchain', 'binary', 'sha256'})
            self.assertEqual(json.loads((release / 'build-info.json').read_text())['mode'], 'release')
            with patch.object(OBSERVER, 'ROOT', root), \
                    patch.object(OBSERVER, 'prepared_environment', return_value=(environment, {'cargo': 'owned-cargo'})), \
                    patch.object(OBSERVER, 'source_hash', return_value='a' * 64), \
                    patch.object(OBSERVER.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
                    patch('sys.argv', ['build_observer.py', 'build']):
                self.assertEqual(OBSERVER.main(), 0)
            self.assertNotIn('--release', run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs['env']['RUSTFLAGS'], '-C target-feature=-crt-static')
            self.assertEqual(run.call_args.kwargs['env']['CARGO_ENCODED_RUSTFLAGS'], '-C\x1ftarget-feature=-crt-static')
            self.assertEqual((install / 'session-panel-observer.exe').read_bytes(), b'MZ debug')
            self.assertFalse((install / 'build-info.json').exists())

    def test_release_flag_on_other_commands_is_rejected_before_tool_preparation(self):
        with patch('sys.argv', ['build_observer.py', 'test', '--release']), \
                patch.object(OBSERVER, 'prepared_environment') as prepare, self.assertRaises(SystemExit) as caught:
            OBSERVER.main()
        self.assertEqual(caught.exception.code, 2)
        prepare.assert_not_called()


class NoticesTests(unittest.TestCase):
    def test_actual_wheel_cpython_inno_and_registry_license_texts_are_collected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            dependency = wheel(root)
            crate = root / 'registry/example-crate'
            crate.mkdir(parents=True)
            (crate / 'Cargo.toml').write_text('[package]\nname="example-crate"\n')
            (crate / 'LICENSE-MIT').write_text('Example crate license.')
            packages = root / 'cargo.json'
            packages.write_text(json.dumps([{'name': 'example-crate', 'version': '1.0', 'license': 'MIT',
                                            'manifest_path': str(crate / 'Cargo.toml'), 'license_file': None}]))
            runtime, inno = root / 'python-license', root / 'inno-license'
            runtime.write_text('Example CPython license.')
            inno.write_text('Example Inno license.')
            rows = NOTICES.collect_notices(root, [dependency], runtime, packages, inno)
            self.assertEqual({row['component'] for row in rows}, {'CPython', 'Inno Setup', 'example-dep', 'example-crate'})
            text = (root / 'THIRD-PARTY-NOTICES.txt').read_text(encoding='utf-8')
            self.assertIn('not declared', text)
            self.assertIn('unsigned', text)
            self.assertNotIn(str(root), text)
            values = json.loads(packages.read_text())
            values.append({'name': 'session-panel-observer', 'version': '0.1.0', 'license': None,
                           'license_file': None, 'manifest_path': str(crate / 'Cargo.toml')})
            packages.write_text(json.dumps(values))
            updated = NOTICES.collect_notices(root, [dependency], runtime, packages, inno)
            first_party = next(row for row in updated if row['component'] == 'session-panel-observer')
            self.assertEqual(first_party['license'], 'not declared')
            self.assertTrue(first_party['first_party'])
            (crate / 'LICENSE-MIT').unlink()
            with self.assertRaisesRegex(NOTICES.NoticeError, 'cargo_license_missing'):
                NOTICES.collect_notices(root, [dependency], runtime, packages, inno)


if __name__ == '__main__':
    unittest.main()
