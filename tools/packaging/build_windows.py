"""Build an offline Windows payload from explicitly prepared, verified inputs."""

import argparse
import base64
import csv
from email.parser import BytesParser
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
DOWNLOADS = json.loads((HERE / 'downloads.json').read_text(encoding='utf-8'))
RUNTIME_FILES = {'python.exe', 'pythonw.exe', 'python313.dll', 'python3.dll', 'python313.zip',
                 '_ssl.pyd', '_sqlite3.pyd', '_ctypes.pyd', '_socket.pyd', 'sqlite3.dll',
                 'libssl-3.dll', 'libcrypto-3.dll', 'libffi-8.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'LICENSE.txt'}


class BuildError(ValueError):
    pass


def contract():
    # The standard S9-installed product supplies the one runtime/build validator.
    try:
        from control_everything.installation import manifest
    except ImportError as error:
        raise BuildError('installed_product_contract_missing: prepare the verified product wheel in the build environment') from error
    return manifest


def helper(name):
    spec = importlib.util.spec_from_file_location('control_everything_build_' + name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def version_info(version):
    match = re.fullmatch(r'([0-9]+)\.([0-9]+)\.([0-9]+)(?:(a|b|rc)([0-9]+))?', version)
    if not match:
        raise BuildError('unsupported_product_version')
    major, minor, patch = map(int, match.group(1, 2, 3))
    number = int(match[5] or 0)
    if number >= 10000:
        raise BuildError('prerelease_version_range')
    fourth = {'a': 0, 'b': 10000, 'rc': 20000, None: 0}[match[4]] + number
    values = (major, minor, patch, fourth)
    if any(not 0 <= value <= 65535 for value in values):
        raise BuildError('numeric_version_range')
    return '.'.join(map(str, values))


def product_version():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    if project['name'] != 'control-everything':
        raise BuildError('wrong_source_project')
    version_info(project['version'])
    return project['version']


def run_checked(command, runner=subprocess.run):
    result = runner(list(map(str, command)), cwd=ROOT, capture_output=True, text=True, encoding='utf-8',
                    env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'})
    if result.returncode:
        print(result.stdout or '', end='')
        print(result.stderr or '', file=sys.stderr, end='')
        raise BuildError('build_command_failed:' + str(result.returncode))
    return result.stdout


def locked_requirements(path):
    rows = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        match = re.fullmatch(r'([a-z0-9]+(?:-[a-z0-9]+)*)==([^\s;]+) --hash=sha256:([0-9a-f]{64})', line)
        if not match or match[1] in rows:
            raise BuildError('invalid_windows_lock')
        rows[match[1]] = {'version': match[2], 'sha256': match[3]}
    if not rows:
        raise BuildError('empty_windows_lock')
    return rows


def wheel_info(path):
    from packaging.tags import compatible_tags, cpython_tags
    from packaging.utils import canonicalize_name, parse_wheel_filename
    path = Path(path)
    contract().no_links(path)
    name, version, _build, tags = parse_wheel_filename(path.name)
    supported = set(cpython_tags((3, 13), abis=['cp313'], platforms=['win_amd64']))
    supported.update(compatible_tags((3, 13), interpreter='cp313', platforms=['win_amd64']))
    if not tags & supported:
        raise BuildError('wheel_target_mismatch:' + path.name)
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = [item.filename for item in members if not item.is_dir()]
        if len(names) != len(set(value.casefold() for value in names)):
            raise BuildError('wheel_duplicate_path:' + path.name)
        for item in members:
            contract().relative_path(item.filename.rstrip('/'))
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise BuildError('wheel_link:' + path.name)
        metadata_names = [value for value in names if value.endswith('.dist-info/METADATA')]
        if len(metadata_names) != 1:
            raise BuildError('wheel_metadata_missing')
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        if canonicalize_name(metadata['Name']) != name or metadata['Version'] != str(version):
            raise BuildError('wheel_identity_mismatch')
        record = metadata_names[0].rsplit('/', 1)[0] + '/RECORD'
        indexed = {}
        for filename, digest, size in csv.reader(io.StringIO(archive.read(record).decode('utf-8'))):
            if filename in indexed:
                raise BuildError('wheel_record_duplicate')
            indexed[filename] = True
            if filename == record and not digest and not size:
                continue
            if filename not in names or not digest.startswith('sha256='):
                raise BuildError('wheel_record_invalid')
            raw = archive.read(filename)
            expected = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode('ascii')
            if digest[7:] != expected or size != str(len(raw)):
                raise BuildError('wheel_record_mismatch')
        if set(indexed) != set(names):
            raise BuildError('wheel_record_closure')
    return {'name': str(name), 'version': str(version), 'sha256': contract().sha256(path),
            'requires': metadata.get_all('Requires-Dist', []), 'filename': path.name}


def verify_wheels(wheel, wheelhouse, lock, version):
    product = wheel_info(wheel)
    if product['name'] != 'control-everything' or product['version'] != version:
        raise BuildError('product_wheel_version')
    selected, identities = [], []
    for path in sorted(Path(wheelhouse).glob('*.whl')):
        info = wheel_info(path)
        expected = lock.get(info['name'])
        if expected is None or info['version'] != expected['version'] or info['sha256'] != expected['sha256']:
            raise BuildError('unlocked_wheel:' + path.name)
        if info['name'] in {row['name'] for row in identities}:
            raise BuildError('duplicate_locked_wheel')
        selected.append(path)
        identities.append(info)
    if {row['name'] for row in identities} != set(lock):
        raise BuildError('locked_wheel_missing')
    return selected, [product, *identities]


def verify_product_sources(wheel, inputs, version):
    """Bind wheel contents to the same publication sources recorded in product.json."""
    from packaging.requirements import Requirement
    api = contract()
    candidates = {row['path']: row['sha256'] for row in inputs}

    def source_bytes(relative):
        if relative not in candidates:
            raise BuildError('product_source_missing:' + relative)
        raw = api.no_links(ROOT / relative).read_bytes()
        if hashlib.sha256(raw).hexdigest() != candidates[relative]:
            raise BuildError('product_source_changed:' + relative)
        return raw

    project = tomllib.loads(source_bytes('pyproject.toml').decode('utf-8'))['project']
    if project['name'] != 'control-everything' or project['version'] != version:
        raise BuildError('product_source_identity')
    source_bytes('tools/packaging/build_assets.py')
    expected = {name.removeprefix('src/'): source_bytes(name) for name in candidates
                if name.startswith('src/control_everything/') and name.endswith('.py')}
    if 'control_everything/__init__.py' not in expected:
        raise BuildError('product_source_package_missing')
    for relative in helper('build_assets').WEB_ASSETS:
        expected['control_everything/_assets/web/' + relative] = source_bytes('web/' + relative)
    requirements = sorted(str(Requirement(line.strip())) for line in
                          source_bytes('requirements.txt').decode('utf-8').splitlines()
                          if line.strip() and not line.lstrip().startswith('#'))
    readme = source_bytes(project['readme']).decode('utf-8').replace('\r\n', '\n').rstrip()
    info = 'control_everything-' + version + '.dist-info/'
    metadata_files = {info + name for name in ('METADATA', 'WHEEL', 'top_level.txt', 'RECORD')}
    with zipfile.ZipFile(wheel) as archive:
        if set(archive.namelist()) != set(expected) | metadata_files:
            raise BuildError('product_source_closure')
        for name, raw in expected.items():
            if archive.read(name) != raw:
                raise BuildError('product_source_mismatch:' + name)
        metadata = BytesParser().parsebytes(archive.read(info + 'METADATA'))
        if (metadata['Name'] != project['name'] or metadata['Version'] != version
                or metadata['Requires-Python'] != project['requires-python']
                or metadata['Summary'] != project['description']
                or sorted(str(Requirement(value)) for value in metadata.get_all('Requires-Dist', [])) != requirements
                or metadata.get_payload(decode=True).decode('utf-8').replace('\r\n', '\n').rstrip() != readme
                or archive.read(info + 'top_level.txt').strip() != b'control_everything'):
            raise BuildError('product_source_metadata')


def prepare_runtime(runtime_zip, stage):
    api = contract()
    api.no_links(runtime_zip)
    if api.sha256(runtime_zip) != DOWNLOADS['python']['sha256']:
        raise BuildError('runtime_archive_hash')
    destination = Path(stage) / 'runtime'
    destination.mkdir()
    with zipfile.ZipFile(runtime_zip) as archive:
        seen = set()
        for item in archive.infolist():
            relative = api.relative_path(item.filename.rstrip('/'))
            if item.filename.casefold() in seen or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise BuildError('runtime_archive_path')
            seen.add(item.filename.casefold())
            target = destination.joinpath(*relative.parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as output:
                    output.write(archive.read(item))
    if not all((destination / value).is_file() for value in RUNTIME_FILES):
        raise BuildError('runtime_component_missing')
    (destination / 'python313._pth').write_bytes(b'python313.zip\n.\nLib/site-packages\nimport site\n')
    return destination


def install_wheels(runtime, wheel, wheelhouse, lockfile, runner=subprocess.run):
    site = runtime / 'Lib/site-packages'
    # --no-deps consumes the already target-resolved lock, never host marker resolution.
    common = [sys.executable, '-I', '-B', '-m', 'pip', '--isolated', 'install', '--no-index',
              '--only-binary=:all:', '--no-deps', '--no-compile', '--no-warn-script-location',
              '--platform', 'win_amd64', '--python-version', '3.13', '--implementation', 'cp', '--abi', 'cp313',
              '--target', site]
    run_checked([*common, '--find-links', wheelhouse, '--require-hashes', '-r', lockfile], runner)
    run_checked([*common, wheel], runner)
    # pip's local provenance and generated launchers contain the build interpreter path.
    # They are not application entrypoints. Remove only these known generated outputs.
    removed = set()
    for path in site.glob('*.dist-info/direct_url.json'):
        contract().no_links(path)
        removed.add(path.relative_to(site).as_posix())
        path.unlink()
    for directory in (site / 'bin', site / 'Scripts'):
        if directory.exists():
            files, directories = contract().inventory(directory)
            for path in files.values():
                removed.add(path.relative_to(site).as_posix())
                path.unlink()
            for name in sorted(directories, key=lambda value: value.count('/'), reverse=True):
                (directory / name).rmdir()
            directory.rmdir()
    for record in site.glob('*.dist-info/RECORD'):
        rows = list(csv.reader(io.StringIO(record.read_text(encoding='utf-8'))))
        kept = [row for row in rows if row[0] not in removed and not row[0].startswith(('../../Scripts/', '../../bin/'))]
        stream = io.StringIO(newline='')
        csv.writer(stream, lineterminator='\n').writerows(kept)
        record.write_text(stream.getvalue(), encoding='utf-8', newline='\n')
    (site / 'sitecustomize.py').write_text('"""The installed payload is immutable."""\nimport sys\nsys.dont_write_bytecode = True\n', encoding='utf-8', newline='\n')
    return site


TARGET_PROBE = '''import importlib.metadata as m, json, platform, struct, sys
from pathlib import Path
assert sys.version_info[:3] == (3, 13, 16) and struct.calcsize('P') == 8
import ssl, sqlite3, ctypes, mcp, win32api, pydantic_core
site = Path(sys.executable).parent / 'Lib/site-packages'
rows = [{'name': d.metadata['Name'], 'version': d.version, 'requires': d.requires or []}
        for d in m.distributions(path=[str(site)])]
environment = dict(implementation_name=sys.implementation.name, implementation_version=platform.python_version(),
 os_name='nt', platform_machine=platform.machine(), platform_release=platform.release(), platform_system=platform.system(),
 platform_version=platform.version(), python_full_version=platform.python_version(),
 platform_python_implementation=platform.python_implementation(), python_version='3.13', sys_platform='win32')
print(json.dumps({'environment': environment, 'distributions': rows}))
'''


def validate_target_metadata(probe, identities):
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    expected = {value['name']: value['version'] for value in identities}
    rows = {canonicalize_name(value['name']): value for value in probe['distributions']}
    if len(rows) != len(probe['distributions']) or {name: row['version'] for name, row in rows.items()} != expected:
        raise BuildError('installed_distribution_mismatch')
    extras, pending, reached = {'control-everything': set()}, ['control-everything'], set()
    while pending:
        name = pending.pop()
        reached.add(name)
        for text in rows[name]['requires']:
            requirement = Requirement(text)
            if requirement.marker and not any(requirement.marker.evaluate({**probe['environment'], 'extra': extra})
                                              for extra in {'', *extras[name]}):
                continue
            target = canonicalize_name(requirement.name)
            if requirement.url or target not in rows or not requirement.specifier.contains(rows[target]['version'], prereleases=True):
                raise BuildError('target_dependency_unsatisfied:' + target)
            previous = extras.setdefault(target, set())
            additions = set(requirement.extras) - previous
            previous.update(requirement.extras)
            if target not in reached or additions:
                pending.append(target)
    if reached != set(rows):
        raise BuildError('target_dependency_extra_distribution')


def compile_runtime(runtime, stage, runner=subprocess.run):
    code = '''import compileall, marshal, py_compile, types
from pathlib import Path
import sys
root = Path(sys.executable).parent.parent
site = root / 'runtime/Lib/site-packages'
assert compileall.compile_dir(str(site), quiet=1, force=True, stripdir=str(root), prependdir='control-everything',
 invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
def check(code):
 name=code.co_filename.replace(chr(92), '/')
 assert not Path(code.co_filename).is_absolute() and ':' not in name and name.startswith('control-everything/')
 for item in code.co_consts:
  if isinstance(item, types.CodeType): check(item)
for path in site.rglob('*.pyc'):
 raw=path.read_bytes(); assert int.from_bytes(raw[4:8], 'little') == 3; check(marshal.loads(raw[16:]))
'''
    run_checked([runtime / 'python.exe', '-I', '-B', '-X', 'utf8', '-c', code], runner)


def source_identity(release=False, runner=subprocess.run):
    commit = run_checked(['git', 'rev-parse', 'HEAD'], runner).strip()
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise BuildError('source_commit_invalid')
    dirty = bool(run_checked(['git', 'status', '--porcelain', '--untracked-files=normal'], runner).strip())
    if release and dirty:
        raise BuildError('dirty_release_source')
    names = run_checked(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], runner).split('\0')
    digest, inputs = hashlib.sha256(), []
    for name in sorted(set(filter(None, names))):
        # All tracked candidates are public; ignored data/caches never enter this list.
        path = contract().no_links(ROOT / name)
        if not path.is_file():
            raise BuildError('source_input_missing:' + name)
        raw = path.read_bytes()
        digest.update(name.encode('utf-8') + b'\0' + raw + b'\0')
        inputs.append({'path': name, 'sha256': hashlib.sha256(raw).hexdigest()})
    return {'commit': commit, 'dirty': dirty, 'files_hash': digest.hexdigest()}, inputs


def observer_builder():
    spec = importlib.util.spec_from_file_location('control_everything_observer_builder', ROOT / 'tools/scripts/build_observer.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release_observer_info(prepared):
    api, prepared = contract(), Path(prepared)
    api.no_links(prepared)
    if prepared.name != 'release-install':
        raise BuildError('release_observer_directory_required')
    manifest = api.strict_json(api.no_links(prepared / 'manifest.json').read_text(encoding='utf-8'))
    api.exact(manifest, {'protocol', 'build', 'toolchain', 'binary', 'sha256'}, 'native_manifest_fields')
    producer = observer_builder()
    info = api.strict_json(api.no_links(prepared / 'build-info.json').read_text(encoding='utf-8'))
    api.exact(info, {'schema', 'mode', 'flags', 'rustflags', 'encoded_rustflags', 'source_hash', 'inputs'}, 'native_build_fields')
    if (type(info['schema']) is not int or info['schema'] != 1 or info['mode'] != 'release'
            or info['flags'] != ['--manifest-path', 'native/observer/Cargo.toml', '--locked', '--release']
            or info['rustflags'] != '-C target-feature=+crt-static' or info['encoded_rustflags'] != ''
            or info.get('source_hash') != producer.source_hash() or info.get('inputs') != producer.production_inputs()
            or manifest['build'] != info['source_hash'] or manifest['protocol'] != 2
            or manifest['toolchain'] != producer.TOOLCHAIN or manifest['binary'] != 'session-panel-observer.exe'
            or manifest['sha256'] != api.sha256(api.no_links(prepared / manifest['binary']))):
        raise BuildError('release_observer_identity')
    return info


def add_release_observer(prepared, stage):
    api, prepared = contract(), Path(prepared)
    info = release_observer_info(prepared)
    target = Path(stage) / 'native/observer'
    target.mkdir(parents=True)
    for name in ('session-panel-observer.exe', 'manifest.json'):
        shutil.copyfile(api.no_links(prepared / name), target / name)
    return info


def write_product_manifest(stage, version, source):
    api = contract()
    files, _directories = api.inventory(stage)
    rows = [{'path': name, 'size': path.stat().st_size, 'sha256': api.sha256(path)}
            for name, path in sorted(files.items()) if name != 'product.json']
    document = {'schema': 1, 'product': 'session-panel', 'version': version, 'target': 'windows-x86_64',
                'python': '3.13.16', 'source': source,
                'compatibility': {'native_protocol': 2, 'profile_identity': 1, 'management_schema': 2, 'profile_config': [1, 2, 3]},
                'native': {'relative': api.NATIVE_EXE, 'manifest_sha256': api.sha256(Path(stage) / api.NATIVE_MANIFEST), 'mode': 'release'},
                'files': rows}
    document['closure'] = api.manifest_closure(document)
    api.validate_manifest(document)
    (Path(stage) / 'product.json').write_bytes(api.canonical_json(document) + b'\n')
    return document


def build_payload(wheel, wheelhouse, runtime_zip, observer_release, output, cargo_packages, inno_license,
                  *, release=False, runner=subprocess.run):
    api, output = contract(), Path(output)
    if not output.is_absolute() or output.exists():
        raise BuildError('new_absolute_output_required')
    if any(not Path(path).is_absolute() for path in (wheel, wheelhouse, runtime_zip, observer_release, cargo_packages, inno_license)):
        raise BuildError('absolute_prepared_inputs_required')
    api.no_links(output)
    source, inputs = source_identity(release, runner)
    version = product_version()
    lockfile = HERE / 'windows-cp313.lock'
    lock = locked_requirements(lockfile)
    wheels, identities = verify_wheels(wheel, wheelhouse, lock, version)
    verify_product_sources(wheel, inputs, version)
    release_observer_info(observer_release)
    for path in (runtime_zip, cargo_packages, inno_license):
        api.no_links(path)
        if not Path(path).is_file():
            raise BuildError('prepared_input_missing')
    if api.sha256(runtime_zip) != DOWNLOADS['python']['sha256']:
        raise BuildError('runtime_archive_hash')
    # All work is isolated to this newly owned output; failures remain as evidence.
    output.mkdir(parents=True)
    runtime = prepare_runtime(runtime_zip, output)
    install_wheels(runtime, wheel, wheelhouse, lockfile, runner)
    native = add_release_observer(observer_release, output)
    probe = json.loads(run_checked([runtime / 'python.exe', '-I', '-B', '-X', 'utf8', '-c', TARGET_PROBE], runner))
    validate_target_metadata(probe, identities)
    compile_runtime(runtime, output, runner)
    helper('notices').collect_notices(output, [wheel, *wheels], runtime / 'LICENSE.txt', cargo_packages, inno_license)
    provenance = {'source_inputs': inputs, 'wheel_inputs': identities, 'native_build': native,
                  'runtime': DOWNLOADS['python'], 'installer_tool': DOWNLOADS['inno'],
                  'numeric_version': version_info(version), 'lock_sha256': api.sha256(lockfile),
                  'project_license': 'not declared', 'product_signature': 'unsigned'}
    (output / 'BUILD-INPUTS.json').write_bytes(api.canonical_json(provenance) + b'\n')
    (output / 'session-panel.cmd').write_bytes(b'@echo off\r\n"%~dp0runtime\\python.exe" -I -B -X utf8 -m control_everything.gateway %*\r\n')
    manifest = write_product_manifest(output, version, source)
    api.verify_payload(output, strict_native=True)
    return {'version': version, 'closure': manifest['closure'], 'version_id': version + '-' + manifest['closure'][:16],
            'source': source, 'files': len(manifest['files'])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('wheel', 'wheelhouse', 'runtime-zip', 'observer-release', 'output', 'cargo-packages', 'inno-license'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--release', action='store_true', help='Require a clean, committed source tree.')
    args = parser.parse_args(argv)
    try:
        result = build_payload(args.wheel, args.wheelhouse, args.runtime_zip, args.observer_release,
                               args.output, args.cargo_packages, args.inno_license, release=args.release)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print('windows_build_failed:' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
