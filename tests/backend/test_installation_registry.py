"""Temporary record fixtures; no Windows, daemon, database or network calls."""

from contextlib import contextmanager
import copy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

from tests.backend.test_windows_bundle import PayloadFixture, BUILD, MANIFEST

DIRECTORY = Path(__file__).resolve().parents[2] / 'src/session_panel/installation'
spec = importlib.util.spec_from_file_location('installation_test_package', DIRECTORY / '__init__.py',
                                             submodule_search_locations=[str(DIRECTORY)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
REGISTRY = importlib.import_module(spec.name + '.registry')
MAINTENANCE = importlib.import_module(spec.name + '.maintenance')


class FakeProcess:
    def __init__(self, identity):
        self.identity, self.running = identity, True

    def alive(self):
        return self.running

    def wait(self, timeout_ms=5000):
        return not self.running

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class FakeAPI:
    def __init__(self, root, runtime):
        self.root, self.runtime = root, runtime
        self.processes, self.pipes, self.calls = {}, {}, []
        self.mutex_waits, self.mutex_releases = 0, 0
        self.stop_exits, self.stop_code, self.exclusive_error = True, 0, False
        self.identity_error, self.busy = False, False
        self.add_process(100, runtime)
        self.current_pid = 100

    def file_identity(self, path):
        path = Path(path).absolute()
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return {'path': str(path), 'volume': 1, 'file_id': hashlib.sha256(str(path).encode()).hexdigest()[:32]}

    def directory_identity(self, path):
        path = Path(path).absolute()
        if not path.is_dir():
            raise FileNotFoundError(str(path))
        return {'path': str(path), 'volume': 1, 'file_id': hashlib.sha256(str(path).encode()).hexdigest()[:32]}

    def add_process(self, pid, image, *, parent=10, created=None):
        identity = {'pid': pid, 'created': created or pid * 100, 'parent_pid': parent, 'image': self.file_identity(image),
                    'sid': '0102', 'authentication': 9, 'session': 1}
        self.processes[pid] = FakeProcess(identity)
        return self.processes[pid]

    def current_identity(self):
        return copy.deepcopy(self.processes[self.current_pid].identity)

    def profile_key(self, path):
        return REGISTRY.profile_key(path, self)

    def process_identity(self, pid):
        return self.processes[pid]

    def mutex_spec(self, root):
        return {'name': 'Global\\SessionPanel.Install.' + 'a' * 64, 'sddl': 'D:P(A;;GA;;;SY)(A;;GA;;;S-1-5-21-1)'}

    @contextmanager
    def install_mutex(self, root):
        self.mutex_waits += 1
        if self.busy:
            error = OSError('maintenance_busy')
            error.reason = 'maintenance_busy'
            raise error
        try:
            yield
        finally:
            self.mutex_releases += 1

    @contextmanager
    def installation_processes(self, root, recorded_pids=()):
        if self.identity_error:
            error = OSError('recorded_process_unreadable')
            error.reason = 'recorded_process_unreadable'
            raise error
        yield [p for p in self.processes.values() if p.running and
               (Path(p.identity['image']['path']).is_relative_to(root) or p.identity['pid'] in recorded_pids)], []

    def pipe_peer(self, key):
        peer = self.pipes.get(key)
        return peer if peer and peer.running else None

    @contextmanager
    def readonly_native(self, path):
        self.calls.append(('readonly_open', str(path)))
        try:
            yield
        finally:
            self.calls.append(('readonly_close', str(path)))

    @contextmanager
    def exclusive_asset_handles(self, paths):
        self.calls.append(('exclusive', list(paths)))
        if self.exclusive_error:
            raise OSError('sharing violation')
        yield

    def native_command(self, binary, command, profile):
        self.calls.append((command, str(binary), str(profile)))
        if command == 'stop':
            if self.stop_exits and not self.stop_code:
                self.pipes[self.profile_key(profile)].running = False
            return SimpleNamespace(returncode=self.stop_code, stdout=b'{"status":"available"}')
        return SimpleNamespace(returncode=0, stdout=json.dumps({'status': 'available', 'protocol': 2,
            'profile_key': self.profile_key(profile), 'generation': 'fixture', 'build': 'c' * 64,
            'scope_revision': 0, 'scope_hash': '', 'config_revision': 0, 'config_hash': 'd' * 64}).encode())

    def registered_uninstaller(self, root):
        return self.file_identity(root / 'unins000.exe')


class InstallationFixture(PayloadFixture):
    def setUp(self):
        super().setUp()
        self.install = self.root / 'owned product'
        self.install.mkdir()
        self.context = self.make_version()
        self.api = FakeAPI(self.install, self.context.version_dir / 'runtime/python.exe')
        self.write_installation()
        # The maintenance helper executes outside the installed root.
        helper = self.root / 'helper.exe'
        helper.write_bytes(b'fixture helper')
        self.api.add_process(999, helper)
        self.api.current_pid = 999

    def make_version(self, version='0.1.0a1'):
        stage, _document = self.payload()
        (stage / 'product.json').unlink()
        document = BUILD.write_product_manifest(stage, version, {'commit': 'a' * 40, 'dirty': False, 'files_hash': 'b' * 64})
        key = REGISTRY.version_id(document)
        target = self.install / 'versions' / key
        target.parent.mkdir(exist_ok=True)
        stage.rename(target)
        return REGISTRY.ProductContext.load(target)

    def write_installation(self):
        uninstaller = self.install / 'unins000.exe'
        uninstaller.write_bytes(b'Inno fixture')
        (self.install / 'unins000.dat').write_bytes(b'Inno locked data fixture')
        (self.install / 'session-panel.cmd').write_bytes(b'old launcher\r\n')
        REGISTRY.write_record(self.install / '.install/root.json', {'schema': 1, 'install_id': str(uuid.uuid4()),
            'owner': '0102', 'directory': self.api.directory_identity(self.install),
            'uninstaller': self.api.file_identity(uninstaller)})
        REGISTRY.write_record(self.install / '.install/versions' / (self.context.version_id + '.json'),
                              REGISTRY.version_record(self.context, self.api))
        self.api.processes[100].running = False

    def profile(self, name='profile one', context=None, pid=None):
        context = context or self.context
        path = self.root / name
        path.mkdir(exist_ok=True)
        self.api.add_process(100, context.version_dir / 'runtime/python.exe')
        self.api.current_pid = 100
        lease = REGISTRY.register_http(context, path, api=self.api)
        lease.close()
        self.api.processes[100].running = False
        self.api.current_pid = 999
        if pid:
            process = self.api.add_process(pid, context.version_dir / MANIFEST.NATIVE_EXE)
            self.api.pipes[self.api.profile_key(path)] = process
        return path


class RegistryTests(InstallationFixture):
    def test_profile_retains_two_version_associations_and_close_is_exact(self):
        profile = self.profile()
        second = self.make_version('0.1.0a2')
        REGISTRY.write_record(self.install / '.install/versions' / (second.version_id + '.json'), REGISTRY.version_record(second, self.api))
        self.profile(context=second)
        _root, versions, profiles, http = REGISTRY.load_records(self.install, self.api)
        self.assertEqual(len(versions), 2)
        self.assertEqual(set(profiles[self.api.profile_key(profile)]['associations']), set(versions))
        self.assertEqual(http, [])

    def test_native_missing_keeps_basic_registration_and_expected_old_association(self):
        original = REGISTRY.read_record(self.install / '.install/versions' / (self.context.version_id + '.json'))
        (self.context.version_dir / MANIFEST.NATIVE_EXE).unlink()
        self.profile()
        _root, _versions, profiles, _http = REGISTRY.load_records(self.install, self.api)
        association = next(iter(profiles.values()))['associations'][self.context.version_id]
        self.assertEqual(association, original['native'])

    def test_wrong_root_owner_unknown_version_and_duplicate_records_are_refused(self):
        unknown = self.install / 'versions/unknown'
        unknown.mkdir()
        with self.assertRaises(ValueError): REGISTRY.load_records(self.install, self.api)
        unknown.rmdir()
        path = self.install / '.install/root.json'
        path.write_text('{"schema":1,"schema":1}', encoding='utf-8')
        with self.assertRaises(ValueError): REGISTRY.load_root(self.install, self.api)

    def test_report_limits_paths_and_lease_cannot_delete_changed_record(self):
        path = self.root / 'record.json'
        path.write_bytes(b' ' * (REGISTRY.MAX_RECORD + 1))
        with self.assertRaises(ValueError): REGISTRY.read_record(path)
        path.write_text('{"changed":true}', encoding='utf-8')
        REGISTRY.RegistrationLease(path, {'original': True}).close()
        self.assertTrue(path.exists())
        with self.assertRaises(ValueError): REGISTRY.checked_path('../escape')
