"""Owned installation records are evidence, never process-control authority."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import uuid

from . import manifest

MAX_RECORD = 1024 * 1024
TOKEN = re.compile(r'[A-Za-z0-9_.-]{1,128}\Z')


class RegistryError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def checked_path(value):
    if type(value) is not str or len(value) > 32760 or not Path(value).is_absolute():
        raise RegistryError('absolute_path_required')
    path = manifest.no_links(value)
    if str(path) != value:
        raise RegistryError('noncanonical_path')
    return path


def identity(value):
    manifest.exact(value, {'path', 'volume', 'file_id'}, 'file_identity_fields')
    checked_path(value['path'])
    if (type(value['volume']) is not int or not 0 <= value['volume'] < 2**64
            or type(value['file_id']) is not str or not re.fullmatch('[0-9a-f]{32}', value['file_id'])):
        raise RegistryError('file_identity')
    return value


def contained(root, value):
    path = checked_path(value)
    if not path.is_relative_to(root) or path == root:
        raise RegistryError('outside_installation')
    return path


def read_record(path):
    with manifest.no_links(path).open('rb') as stream:
        raw = stream.read(MAX_RECORD + 1)
    if len(raw) > MAX_RECORD:
        raise RegistryError('record_oversize')
    return manifest.strict_json(raw.decode('utf-8'))


def write_record(path, value):
    path = manifest.no_links(path)
    raw = manifest.canonical_json(value) + b'\n'
    if len(raw) > MAX_RECORD:
        raise RegistryError('record_oversize')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def version_id(document):
    return document['version'] + '-' + document['closure'][:16]


def default_api():
    from control_everything.platform.windows_installation import InstallationAPI
    return InstallationAPI()


def profile_key(directory, api):
    from control_everything.sessions.observer_protocol import profile_key as calculate
    value = api.directory_identity(directory)
    return calculate(bytes.fromhex(api.current_identity()['sid']), value['volume'], bytes.fromhex(value['file_id']))


@dataclass(frozen=True)
class ProductContext:
    version_dir: Path
    install_root: Path
    document: dict
    version_id: str

    @classmethod
    def load(cls, version_dir):
        path = manifest.no_links(version_dir)
        document = manifest.verify_payload(path, strict_native=False)
        key = version_id(document)
        if path.name != key or path.parent.name != 'versions':
            raise RegistryError('version_location')
        return cls(path, path.parent.parent, document, key)


def load_root(root, api):
    root = manifest.no_links(root)
    record = read_record(root / '.install/root.json')
    manifest.exact(record, {'schema', 'install_id', 'owner', 'directory', 'uninstaller'}, 'root_fields')
    if (type(record['schema']) is not int or record['schema'] != 1
            or str(uuid.UUID(record['install_id'])) != record['install_id']
            or record['owner'] != api.current_identity()['sid']):
        raise RegistryError('root_owner')
    identity(record['directory'])
    if record['directory'] != api.directory_identity(root):
        raise RegistryError('root_identity')
    if record['uninstaller'] is not None:
        identity(record['uninstaller'])
        contained(root, record['uninstaller']['path'])
    return record


def native_association(context, api):
    rows = {row['path']: row for row in context.document['files']}
    executable = context.version_dir / manifest.NATIVE_EXE
    expected = rows[manifest.NATIVE_EXE]['sha256']
    result = {'image': None, 'sha256': expected, 'manifest_sha256': context.document['native']['manifest_sha256'],
              'build': None, 'available': False}
    try:
        native_path = context.version_dir / manifest.NATIVE_MANIFEST
        if manifest.sha256(native_path) != result['manifest_sha256'] or manifest.sha256(executable) != expected:
            return result
        native = read_record(native_path)
        manifest.exact(native, {'protocol', 'build', 'toolchain', 'binary', 'sha256'}, 'native_fields')
        if (type(native['protocol']) is not int or native['protocol'] != 2 or native['sha256'] != expected
                or native['binary'] != executable.name or native['toolchain'] != '1.97.1-x86_64-pc-windows-msvc'
                or type(native['build']) is not str or not manifest.HEX.fullmatch(native['build'])):
            return result
        result.update(image=api.file_identity(executable), build=native['build'], available=True)
    except (OSError, ValueError):
        pass
    return result


def version_record(context, api):
    return {'schema': 1, 'version_id': context.version_id, 'directory': api.directory_identity(context.version_dir),
            'product_sha256': manifest.sha256(context.version_dir / 'product.json'), 'closure': context.document['closure'],
            'runtime': api.file_identity(context.version_dir / 'runtime/python.exe'), 'native': native_association(context, api)}


def validate_version(root, key, record, api):
    if type(key) is not str or not TOKEN.fullmatch(key):
        raise RegistryError('version_key')
    manifest.exact(record, {'schema', 'version_id', 'directory', 'product_sha256', 'closure', 'runtime', 'native'}, 'version_fields')
    directory = contained(root, identity(record['directory'])['path'])
    if directory != root / 'versions' / key or type(record['schema']) is not int or record['schema'] != 1 or record['version_id'] != key:
        raise RegistryError('version_location')
    context = ProductContext.load(directory)
    expected = version_record(context, api)
    # Missing native is tolerated at runtime, but records must retain the original identity.
    if any(record[field] != expected[field] for field in ('schema', 'version_id', 'directory', 'product_sha256', 'closure', 'runtime')):
        raise RegistryError('version_identity')
    validate_association(record['native'], root)
    native = record['native']
    if (native['sha256'] != expected['native']['sha256'] or native['manifest_sha256'] != expected['native']['manifest_sha256']
            or native['image'] is not None and Path(native['image']['path']) != directory / manifest.NATIVE_EXE):
        raise RegistryError('native_association')
    return context


def validate_association(value, root):
    manifest.exact(value, {'image', 'sha256', 'manifest_sha256', 'build', 'available'}, 'association_fields')
    if (type(value['available']) is not bool or not isinstance(value['sha256'], str)
            or not manifest.HEX.fullmatch(value['sha256']) or not isinstance(value['manifest_sha256'], str)
            or not manifest.HEX.fullmatch(value['manifest_sha256'])):
        raise RegistryError('association_identity')
    if value['image'] is not None:
        contained(root, identity(value['image'])['path'])
    if value['available'] and (value['image'] is None or type(value['build']) is not str or not manifest.HEX.fullmatch(value['build'])):
        raise RegistryError('association_identity')


def process_record(value):
    manifest.exact(value, {'pid', 'created', 'parent_pid', 'image', 'sid', 'authentication', 'session'}, 'process_fields')
    identity(value['image'])
    for field in ('pid', 'created', 'parent_pid', 'authentication', 'session'):
        if type(value[field]) is not int or value[field] < 0 or value[field] >= 2**64:
            raise RegistryError('process_identity')
    if type(value['sid']) is not str or not re.fullmatch('[0-9a-f]{2,2048}', value['sid']):
        raise RegistryError('process_owner')
    return value


def load_records(root, api, *, pending=None):
    root = manifest.no_links(root)
    owner = load_root(root, api)
    directory = root / '.install'
    files, directories = manifest.inventory(directory)
    if directories - {'versions', 'profiles', 'http', 'backups'}:
        raise RegistryError('unknown_record_directory')
    versions, profiles, http = {}, {}, []
    for name, path in sorted(files.items()):
        if name == 'root.json':
            continue
        prefix, separator, filename = name.partition('/')
        if not separator or '/' in filename or not filename.endswith('.json'):
            # Launcher backups are exact named bytes owned by commit_install.
            if prefix == 'backups' and separator and re.fullmatch(r'[0-9a-f]{64}\.cmd', filename):
                if manifest.sha256(path) != filename[:-4]:
                    raise RegistryError('backup_identity')
                continue
            raise RegistryError('unknown_record')
        key, record = filename[:-5], read_record(path)
        if prefix == 'versions':
            validate_version(root, key, record, api)
            versions[key] = record
        elif prefix == 'profiles':
            manifest.exact(record, {'schema', 'profile_key', 'directory', 'associations'}, 'profile_fields')
            if (type(record['schema']) is not int or record['schema'] != 1 or not re.fullmatch('[0-9a-f]{64}', key) or record['profile_key'] != key
                    or type(record['associations']) is not dict or not record['associations']):
                raise RegistryError('profile_identity')
            identity(record['directory'])
            if (record['directory'] != api.directory_identity(record['directory']['path'])
                    or key != profile_key(record['directory']['path'], api)):
                raise RegistryError('profile_identity')
            for association in record['associations'].values():
                validate_association(association, root)
            profiles[key] = record
        elif prefix == 'http':
            manifest.exact(record, {'schema', 'version_id', 'profile_key', 'process'}, 'http_fields')
            process_record(record['process'])
            if type(record['schema']) is not int or record['schema'] != 1 or key != str(record['process']['pid']) + '-' + str(record['process']['created']):
                raise RegistryError('http_identity')
            http.append(record)
        else:
            raise RegistryError('unknown_record')
    if not versions:
        raise RegistryError('versions_missing')
    for profile in profiles.values():
        for key, association in profile['associations'].items():
            if key not in versions or association != versions[key]['native']:
                raise RegistryError('profile_version_identity')
    for record in http:
        if (record['version_id'] not in versions or record['profile_key'] not in profiles
                or record['process']['image'] != versions[record['version_id']]['runtime']
                or record['process']['sid'] != owner['owner']):
            raise RegistryError('http_association')
    known = set(versions)
    if pending is not None:
        if pending.install_root != root:
            raise RegistryError('pending_location')
        known.add(pending.version_id)
    version_root = root / 'versions'
    if not version_root.is_dir() or {path.name for path in version_root.iterdir()} != known:
        raise RegistryError('unknown_version_directory')
    return owner, versions, profiles, http


class RegistrationLease:
    def __init__(self, path, record):
        self.path, self.record = path, record

    def close(self):
        # A reused PID cannot authorize removing another process's record.
        if self.path.exists() and read_record(self.path) == self.record:
            self.path.unlink()


def register_http(context, profile, *, api=None):
    """Caller holds install_mutex until observer.start has returned."""
    api = default_api() if api is None else api
    root, versions, profiles, _http = load_records(context.install_root, api)
    current = version_record(context, api)
    if (context.version_id not in versions
            or any(versions[context.version_id][field] != current[field] for field in current if field != 'native')):
        raise RegistryError('unregistered_version')
    directory = api.directory_identity(profile)
    key = profile_key(profile, api)
    record = profiles.get(key, {'schema': 1, 'profile_key': key, 'directory': directory, 'associations': {}})
    if record['directory'] != directory:
        raise RegistryError('profile_identity')
    native = versions[context.version_id]['native']
    old = record['associations'].get(context.version_id)
    if old is not None and old != native:
        raise RegistryError('profile_association_changed')
    record['associations'][context.version_id] = native
    write_record(context.install_root / '.install/profiles' / (key + '.json'), record)
    process = api.current_identity()
    process_record(process)
    if process['sid'] != root['owner'] or process['image'] != current['runtime']:
        raise RegistryError('runtime_identity')
    record = {'schema': 1, 'version_id': context.version_id, 'profile_key': key, 'process': process}
    path = context.install_root / '.install/http' / (str(process['pid']) + '-' + str(process['created']) + '.json')
    if path.exists() and read_record(path) != record:
        raise RegistryError('http_record_collision')
    write_record(path, record)
    return RegistrationLease(path, record)
