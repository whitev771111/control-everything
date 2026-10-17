"""Standard-library-only integrity rules shared by build and installed runtime."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat

NATIVE_EXE = 'native/observer/session-panel-observer.exe'
NATIVE_MANIFEST = 'native/observer/manifest.json'
NATIVE_FILES = {NATIVE_EXE, NATIVE_MANIFEST}
BASE_FILES = {'runtime/python.exe', 'runtime/python313.dll', 'runtime/python313.zip', 'runtime/python313._pth',
              'runtime/Lib/site-packages/control_everything/__init__.py', 'runtime/Lib/site-packages/sitecustomize.py',
              'session-panel.cmd', 'BUILD-INPUTS.json', 'THIRD-PARTY-NOTICES.txt', 'THIRD-PARTY-NOTICES.json'}
FIELDS = {'schema', 'product', 'version', 'target', 'python', 'source', 'compatibility', 'native', 'files', 'closure'}
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
HEX = re.compile(r'[0-9a-f]{64}\Z')


class ManifestError(ValueError):
    def __init__(self, reason, path=None):
        super().__init__(reason)
        self.reason, self.path = reason, path


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def manifest_closure(document):
    return hashlib.sha256(canonical_json({key: value for key, value in document.items() if key != 'closure'})).hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ManifestError('duplicate_key')
            result[key] = value
        return result
    def invalid(_value):
        raise ManifestError('invalid_number')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, ManifestError):
            raise
        raise ManifestError('invalid_json') from None


def relative_path(value):
    if (type(value) is not str or not value or '\\' in value or ':' in value
            or any(ord(char) < 32 for char in value)):
        raise ManifestError('invalid_path')
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or str(path) != value or any(part in ('.', '..') for part in path.parts):
        raise ManifestError('invalid_path', value)
    for part in path.parts:
        if (part.endswith(('.', ' ')) or any(char in part for char in '<>"|?*')
                or part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}):
            raise ManifestError('invalid_path', value)
    return path


def no_links(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ManifestError('linked_path')
    return path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def exact(value, fields, reason):
    if type(value) is not dict or set(value) != set(fields):
        raise ManifestError(reason)


def validate_manifest(document):
    exact(document, FIELDS, 'manifest_fields')
    if (type(document['schema']) is not int or document['schema'] != 1 or document['product'] != 'session-panel'
            or document['target'] != 'windows-x86_64' or document['python'] != '3.13.16'
            or type(document['version']) is not str or not re.fullmatch(r'\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?', document['version'])):
        raise ManifestError('manifest_identity')
    source = document['source']
    exact(source, {'commit', 'dirty', 'files_hash'}, 'source_fields')
    if (type(source['commit']) is not str or not re.fullmatch(r'[0-9a-f]{40}', source['commit'])
            or type(source['dirty']) is not bool or type(source['files_hash']) is not str or not HEX.fullmatch(source['files_hash'])):
        raise ManifestError('source_identity')
    if canonical_json(document['compatibility']) != canonical_json({'native_protocol': 2, 'profile_identity': 1, 'management_schema': 2, 'profile_config': [1, 2, 3]}):
        raise ManifestError('compatibility')
    native = document['native']
    exact(native, {'relative', 'manifest_sha256', 'mode'}, 'native_fields')
    if native['relative'] != NATIVE_EXE or native['mode'] != 'release' or type(native['manifest_sha256']) is not str or not HEX.fullmatch(native['manifest_sha256']):
        raise ManifestError('native_identity')
    if type(document['files']) is not list or not document['files']:
        raise ManifestError('files_required')
    names, seen, components = [], set(), {}
    for row in document['files']:
        exact(row, {'path', 'size', 'sha256'}, 'file_fields')
        relative_path(row['path'])
        folded = row['path'].casefold()
        if folded == 'product.json' or folded in seen:
            raise ManifestError('duplicate_path', row['path'])
        seen.add(folded)
        names.append(row['path'])
        for prefix in (PurePosixPath(row['path']), *PurePosixPath(row['path']).parents):
            if str(prefix) == '.':
                continue
            value = str(prefix)
            old = components.setdefault(value.casefold(), value)
            if old != value:
                raise ManifestError('duplicate_path', value)
        if type(row['size']) is not int or row['size'] < 0 or type(row['sha256']) is not str or not HEX.fullmatch(row['sha256']):
            raise ManifestError('file_identity', row['path'])
    if names != sorted(names) or not (NATIVE_FILES | BASE_FILES).issubset(names):
        raise ManifestError('file_order_or_required_missing')
    if any(str(parent).casefold() in seen for name in names for parent in PurePosixPath(name).parents):
        raise ManifestError('file_directory_collision')
    file_map = {row['path']: row for row in document['files']}
    if file_map[NATIVE_MANIFEST]['sha256'] != native['manifest_sha256']:
        raise ManifestError('native_manifest_pin')
    if type(document['closure']) is not str or not HEX.fullmatch(document['closure']) or document['closure'] != manifest_closure(document):
        raise ManifestError('closure_mismatch')
    return document


def inventory(root):
    root = no_links(root)
    if not root.is_dir():
        raise ManifestError('payload_directory_missing')
    files, directories = {}, set()
    folded = set()
    for path in root.rglob('*'):
        no_links(path)
        relative = path.relative_to(root).as_posix()
        relative_path(relative)
        if relative.casefold() in folded:
            raise ManifestError('duplicate_path', relative)
        folded.add(relative.casefold())
        info = path.stat()
        if stat.S_ISDIR(info.st_mode):
            directories.add(relative)
        elif stat.S_ISREG(info.st_mode):
            files[relative] = path
        else:
            raise ManifestError('unsupported_file', relative)
    return files, directories


def verify_payload(version_dir, strict_native=False):
    """Verify immutable base assets; native is separately gated for degraded runtime use."""
    root = no_links(version_dir)
    try:
        manifest_path = no_links(root / 'product.json')
        with manifest_path.open('rb') as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ManifestError('manifest_oversize')
        document = validate_manifest(strict_json(raw.decode('utf-8')))
        files, directories = inventory(root)
        expected = {row['path']: row for row in document['files']}
        extras = set(files) - set(expected) - {'product.json'}
        parents = {str(parent) for name in expected for parent in PurePosixPath(name).parents if str(parent) != '.'}
        if extras or directories - parents:
            raise ManifestError('unexpected_asset', sorted(extras or directories - parents)[0])
        for name, row in expected.items():
            if not strict_native and name in NATIVE_FILES:
                continue
            path = files.get(name)
            if path is None:
                raise ManifestError('missing_asset', name)
            if path.stat().st_size != row['size'] or sha256(path) != row['sha256']:
                raise ManifestError('asset_mismatch', name)
        if strict_native:
            native = strict_json(files[NATIVE_MANIFEST].read_text(encoding='utf-8'))
            exact(native, {'protocol', 'build', 'toolchain', 'binary', 'sha256'}, 'native_manifest_fields')
            if (type(native['protocol']) is not int or native['protocol'] != 2 or native['binary'] != 'session-panel-observer.exe'
                    or native['toolchain'] != '1.97.1-x86_64-pc-windows-msvc'
                    or type(native['build']) is not str or not HEX.fullmatch(native['build'])
                    or native['sha256'] != expected[NATIVE_EXE]['sha256']):
                raise ManifestError('native_manifest_identity')
        return document
    except OSError:
        raise ManifestError('payload_io_error') from None
