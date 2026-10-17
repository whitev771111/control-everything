"""Cooperative installation maintenance; identity failures block instead of force."""

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import tempfile
import uuid

from . import manifest, registry

OPERATIONS = {'install', 'upgrade', 'rollback', 'uninstall'}


class Blocked(ValueError):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


def report_file(path, document):
    path = registry.checked_path(str(path))
    if path.exists():
        raise Blocked(26, 'report_exists')
    registry.write_record(path, document)
    # Inno consumes a small projection, never a second authority or JSON parser.
    lines = ['[maintenance]']
    for name in ('status', 'operation', 'version_id', 'closure'):
        value = document.get(name, '')
        if type(value) is str and '\n' not in value and '\r' not in value:
            lines.append(name + '=' + value)
    if 'mutex' in document:
        lines.extend(['[mutex]', 'name=' + document['mutex']['name'], 'sddl=' + document['mutex']['sddl']])
    with path.with_suffix('.ini').open('x', encoding='utf-8', newline='\n') as stream:
        stream.write('\n'.join(lines) + '\n')


def new_report_path(path, root):
    path = registry.checked_path(str(path))
    if path.suffix != '.json' or path.is_relative_to(root) or path.exists() or path.with_suffix('.ini').exists():
        raise Blocked(26, 'new_external_report_required')
    return path


def same_process(actual, expected):
    registry.process_record(expected)
    return actual.identity == expected and actual.alive()


def canonical_temp_root():
    original = registry.checked_path(str(Path(tempfile.gettempdir()).absolute()))
    return registry.checked_path(str(original.resolve(strict=True)))


def bootstrap_context(api):
    process = api.current_identity()
    context = registry.ProductContext.load(Path(process['image']['path']).parent.parent)
    manifest.verify_payload(context.version_dir, strict_native=True)
    if (process['image'] != api.file_identity(context.version_dir / 'runtime/python.exe')
            or context.version_dir.is_relative_to(canonical_temp_root()) is False
            or context.install_root.name != '{app}'):
        raise Blocked(21, 'bootstrap_location')
    return context


def prepare_invocation(install_root, operation, *, api):
    """Read-only OS hints for Inno; this neither acquires nor authenticates a lock."""
    root = registry.checked_path(str(install_root))
    if operation not in OPERATIONS:
        raise Blocked(24, 'operation')
    context, own = bootstrap_context(api), api.current_identity()
    with api.process_identity(own['parent_pid']) as installer:
        parent = installer.identity
        if any(parent[key] != own[key] for key in ('sid', 'authentication', 'session')):
            raise Blocked(21, 'maintenance_owner')
        original = uninstaller = None
        if operation == 'uninstall':
            uninstaller = api.registered_uninstaller(root)
            with api.process_identity(parent['parent_pid']) as first:
                original = first.identity
                if original['image'] != uninstaller:
                    raise Blocked(21, 'uninstaller_origin')
        return {'schema': 1, 'operation': operation, 'nonce': uuid.uuid4().hex, 'install_root': str(root),
                'installer': parent, 'original': original, 'uninstaller': uninstaller,
                'bootstrap': str(context.version_dir), 'bootstrap_closure': context.document['closure'],
                'helper': own['image'], 'mutex': api.mutex_spec(root), 'status': 'prepared',
                'version_id': context.version_id, 'closure': context.document['closure']}


def verify_maintenance_invocation(invocation, api, stack):
    fields = {'schema', 'operation', 'nonce', 'install_root', 'installer', 'original', 'uninstaller',
              'bootstrap', 'bootstrap_closure', 'helper', 'mutex', 'status', 'version_id', 'closure'}
    manifest.exact(invocation, fields, 'invocation_fields')
    if (type(invocation['schema']) is not int or invocation['schema'] != 1 or invocation['operation'] not in OPERATIONS
            or type(invocation['nonce']) is not str or not re.fullmatch('[0-9a-f]{32}', invocation['nonce'])
            or invocation['status'] != 'prepared'):
        raise Blocked(21, 'invocation_identity')
    root, own = registry.checked_path(invocation['install_root']), api.current_identity()
    context = bootstrap_context(api)
    if (str(context.version_dir) != invocation['bootstrap'] or context.document['closure'] != invocation['bootstrap_closure']
            or invocation['closure'] != context.document['closure'] or invocation['version_id'] != context.version_id
            or own['image'] != invocation['helper'] or invocation['mutex'] != api.mutex_spec(root)):
        raise Blocked(21, 'bootstrap_identity')
    parent = stack.enter_context(api.process_identity(own['parent_pid']))
    if not same_process(parent, invocation['installer']):
        raise Blocked(21, 'maintenance_parent')
    participants = [parent]
    if any(parent.identity[key] != own[key] for key in ('sid', 'authentication', 'session')):
        raise Blocked(21, 'maintenance_owner')
    if invocation['operation'] == 'uninstall':
        original = stack.enter_context(api.process_identity(parent.identity['parent_pid']))
        registered = api.registered_uninstaller(root)
        if (not same_process(original, invocation['original']) or registered != invocation['uninstaller']
                or original.identity['image'] != registered
                or any(original.identity[key] != own[key] for key in ('sid', 'authentication', 'session'))
                or not Path(parent.identity['image']['path']).is_relative_to(canonical_temp_root())
                or manifest.sha256(parent.identity['image']['path']) != manifest.sha256(registered['path'])):
            raise Blocked(21, 'uninstaller_chain')
        participants.append(original)
    elif invocation['original'] is not None or invocation['uninstaller'] is not None:
        raise Blocked(21, 'unexpected_maintenance_parent')
    return participants


def lock_scope(root, operation, invocation, api, stack):
    if invocation is None:
        stack.enter_context(api.install_mutex(root))
        return []
    if invocation.get('install_root') != str(root) or invocation.get('operation') != operation:
        raise Blocked(21, 'invocation_target')
    # The actual Inno parent holds the mutex until all children exit. Do not Wait/Release it here.
    return verify_maintenance_invocation(invocation, api, stack)


def exact_maintenance(process, participants):
    return any(process.identity == participant.identity and participant.alive() for participant in participants)


def verify_native(association, context, api):
    registry.validate_association(association, context.install_root)
    current = registry.native_association(context, api)
    if not association['available'] or association != current:
        raise Blocked(21, 'original_native_identity')
    return Path(association['image']['path'])


def stop_profile(profile, versions, api, stopped):
    with ExitStack() as stack:
        for key, association in profile['associations'].items():
            context = registry.ProductContext.load(versions[key]['directory']['path'])
            verify_native(association, context, api)
        peer = api.pipe_peer(profile['profile_key'])
        if peer is None:
            return None
        stack.enter_context(peer)
        matching = [(key, value) for key, value in profile['associations'].items() if value['image'] == peer.identity['image']]
        own = api.current_identity()
        if len(matching) != 1 or any(peer.identity[key] != own[key] for key in ('sid', 'authentication', 'session')):
            raise Blocked(21, 'profile_peer_identity')
        key, association = matching[0]
        context = registry.ProductContext.load(versions[key]['directory']['path'])
        binary = verify_native(association, context, api)
        with api.readonly_native(binary):
            verify_native(association, context, api)
            try:
                status = api.native_command(binary, 'status', profile['directory']['path'])
            except (OSError, subprocess.SubprocessError) as error:
                raise Blocked(22, 'native_status_failed') from error
            if status.returncode:
                raise Blocked(22, 'native_status_failed')
            from session_panel.sessions import observer_protocol
            try:
                value = manifest.strict_json(status.stdout)
                observer_protocol.hello(value, profile['profile_key'], association['build'])
            except ValueError as error:
                raise Blocked(22, 'native_status_identity') from error
            if not peer.alive():
                raise Blocked(22, 'native_changed_before_stop')
            try:
                result = api.native_command(binary, 'stop', profile['directory']['path'])
            except (OSError, subprocess.SubprocessError) as error:
                raise Blocked(22, 'native_stop_failed') from error
            try:
                accepted = not result.returncode and manifest.strict_json(result.stdout) == {'status': 'available'}
            except ValueError:
                accepted = False
            if not accepted:
                raise Blocked(22, 'native_stop_failed')
            verify_native(association, context, api)
        if not peer.wait():
            raise Blocked(22, 'native_stop_not_exited')
        stopped.append({'profile_key': profile['profile_key'], 'version_id': key, 'process': peer.identity})
        return peer.identity


def scan_processes(root, http, profiles, participants, api, *, after=False):
    expected = []
    with ExitStack() as stack:
        if not after:
            for profile in profiles.values():
                peer = api.pipe_peer(profile['profile_key'])
                if peer is not None:
                    stack.enter_context(peer)
                    expected.append(peer.identity)
        pids = [record['process']['pid'] for record in http]
        peers, denied = stack.enter_context(api.installation_processes(root, pids))
        for peer in peers:
            if exact_maintenance(peer, participants) or not peer.alive():
                continue
            image = Path(peer.identity['image']['path'])
            if not image.is_relative_to(root):
                continue
            if any(peer.identity == record['process'] for record in http):
                raise Blocked(20, 'http_running')
            if not after and peer.identity in expected:
                continue
            raise Blocked(21, 'unknown_installation_process')
        return denied


def project_assets(root, versions, *, pending=None):
    paths = []
    directories = {Path(record['directory']['path']) for record in versions.values()}
    if pending is not None:
        directories.add(pending.version_dir)
    for directory in sorted(directories):
        document = manifest.verify_payload(directory, strict_native=True)
        paths.extend(directory / row['path'] for row in document['files'])
        paths.append(directory / 'product.json')
    if (root / 'session-panel.cmd').exists():
        paths.append(root / 'session-panel.cmd')
    if (root / '.install').exists():
        files, _directories = manifest.inventory(root / '.install')
        paths.extend(files.values())
    return sorted(set(paths))


def preflight(install_root, operation, report_path, *, api, invocation=None, payload=None):
    root = registry.checked_path(str(install_root))
    new_report_path(report_path, root)
    stopped, receipt = [], {'schema': 1, 'operation': operation, 'status': 'blocked', 'stopped': []}
    code = 26
    try:
        if operation not in OPERATIONS:
            raise Blocked(24, 'operation')
        receipt.update(install_root=str(root), nonce=invocation['nonce'] if invocation else uuid.uuid4().hex,
                       parent=api.directory_identity(root.parent), root=None, install_id=str(uuid.uuid4()),
                       launcher_sha256=None, expected_closure=None, version_id='', closure='')
        with ExitStack() as stack:
            participants = lock_scope(root, operation, invocation, api, stack)
            if invocation:
                receipt.update(expected_closure=invocation['bootstrap_closure'], closure=invocation['bootstrap_closure'],
                               version_id=invocation['version_id'])
            elif payload is not None:
                document = manifest.verify_payload(payload, strict_native=True)
                receipt.update(expected_closure=document['closure'], closure=document['closure'], version_id=registry.version_id(document))
            if not root.exists() or not any(root.iterdir()):
                if operation != 'install' or not receipt['expected_closure']:
                    raise Blocked(24, 'empty_install_target')
            else:
                owner, versions, profiles, http = registry.load_records(root, api)
                receipt.update(root=owner['directory'], install_id=owner['install_id'])
                launcher = root / 'session-panel.cmd'
                if not launcher.is_file():
                    raise Blocked(24, 'launcher_missing')
                receipt['launcher_sha256'] = manifest.sha256(launcher)
                scan_processes(root, http, profiles, participants, api)
                for key in sorted(versions):
                    context = registry.ProductContext.load(versions[key]['directory']['path'])
                    verify_native(versions[key]['native'], context, api)
                for key in sorted(profiles):
                    stop_profile(profiles[key], versions, api, stopped)
                receipt['denied_unrelated_pids'] = scan_processes(root, http, profiles, participants, api, after=True)
                try:
                    with api.exclusive_asset_handles(project_assets(root, versions)):
                        pass
                except OSError as error:
                    raise Blocked(23, 'assets_in_use') from error
            receipt.update(status='ready', reason='ready', exit=0)
            code = 0
    except Blocked as error:
        code, receipt['reason'] = error.code, error.reason
    except (registry.RegistryError, manifest.ManifestError, ValueError, KeyError, TypeError):
        code, receipt['reason'] = 24, 'invalid_installation'
    except OSError as error:
        reason = getattr(error, 'reason', 'io_or_incomplete')
        code = 25 if reason == 'maintenance_busy' else 21 if reason in ('recorded_process_unreadable', 'owned_process_unreadable', 'process_snapshot', 'pipe_unavailable') else 26
        receipt['reason'] = reason
    receipt.update(exit=code, stopped=stopped)
    report_file(report_path, receipt)
    return code


def verify_install(context, install_root, *, api, invocation=None):
    root = registry.checked_path(str(install_root))
    if context.install_root != root:
        raise Blocked(24, 'payload_location')
    manifest.verify_payload(context.version_dir, strict_native=True)
    if invocation is not None and context.document['closure'] != invocation['bootstrap_closure']:
        raise Blocked(24, 'payload_invocation_identity')
    if (root / '.install/root.json').exists():
        registry.load_records(root, api, pending=context)
    else:
        # Initial [Files] may contain only the selected version and exact Inno metadata.
        uninstaller = api.registered_uninstaller(root)
        inno = Path(uninstaller['path'])
        allowed = {'versions', inno.name, inno.with_suffix('.dat').name, inno.with_suffix('.msg').name}
        if {p.name for p in root.iterdir()} - allowed or {p.name for p in (root / 'versions').iterdir()} != {context.version_id}:
            raise Blocked(24, 'unknown_initial_assets')
    return context


def final_commit_check(context, root, participants, api, *, existing):
    """A ready receipt does not prove that the installation is still idle."""
    versions, profiles, http = {}, {}, []
    if existing:
        _owner, versions, profiles, http = registry.load_records(root, api, pending=context)
        for record in versions.values():
            old = registry.ProductContext.load(record['directory']['path'])
            verify_native(record['native'], old, api)
    # The new version is also in scope before its first root/version record exists.
    verify_native(registry.native_association(context, api), context, api)
    scan_processes(root, http, profiles, participants, api, after=True)
    try:
        with api.exclusive_asset_handles(project_assets(root, versions, pending=context)):
            pass
    except OSError as error:
        raise Blocked(23, 'assets_in_use') from error


def commit_install(context, install_root, *, invocation=None, receipt=None, api=None):
    api = registry.default_api() if api is None else api
    root = registry.checked_path(str(install_root))
    if (type(receipt) is not dict or receipt.get('status') != 'ready' or type(receipt.get('exit')) is not int
            or receipt.get('exit') != 0 or receipt.get('operation') not in OPERATIONS):
        raise Blocked(24, 'receipt_required')
    required = {'schema', 'operation', 'status', 'stopped', 'install_root', 'nonce', 'parent', 'root', 'install_id',
                'launcher_sha256', 'expected_closure', 'version_id', 'closure', 'reason', 'exit'}
    if (set(receipt) - {'denied_unrelated_pids'} != required or type(receipt['schema']) is not int or receipt['schema'] != 1
            or receipt['reason'] != 'ready' or not isinstance(receipt['nonce'], str)
            or not re.fullmatch('[0-9a-f]{32}', receipt['nonce']) or type(receipt['stopped']) is not list):
        raise Blocked(24, 'receipt_fields')
    registry.identity(receipt['parent'])
    if receipt['root'] is not None:
        registry.identity(receipt['root'])
    if str(uuid.UUID(receipt['install_id'])) != receipt['install_id']:
        raise Blocked(24, 'receipt_install_id')
    operation = receipt['operation']
    if receipt['root'] is None and operation != 'install':
        raise Blocked(24, 'receipt_initial_operation')
    with ExitStack() as stack:
        participants = lock_scope(root, operation, invocation, api, stack)
        if (receipt['install_root'] != str(root) or receipt['parent'] != api.directory_identity(root.parent)
                or receipt['expected_closure'] != context.document['closure']
                or receipt['closure'] != context.document['closure'] or receipt['version_id'] != context.version_id
                or invocation is not None and receipt['nonce'] != invocation['nonce']):
            raise Blocked(24, 'receipt_identity')
        verify_install(context, root, api=api, invocation=invocation)
        launcher = root / 'session-panel.cmd'
        if receipt['root'] is not None:
            owner = registry.load_root(root, api)
            if (owner['directory'] != receipt['root'] or owner['install_id'] != receipt['install_id']
                    or not launcher.is_file() or manifest.sha256(launcher) != receipt['launcher_sha256']):
                raise Blocked(24, 'installation_changed')
        else:
            if launcher.exists() or (root / '.install').exists():
                raise Blocked(24, 'initial_target_changed')
            owner = {'schema': 1, 'install_id': receipt['install_id'], 'owner': api.current_identity()['sid'],
                     'directory': api.directory_identity(root), 'uninstaller': api.registered_uninstaller(root)}
        current = registry.version_record(context, api)
        version_path = root / '.install/versions' / (context.version_id + '.json')
        if version_path.exists() and registry.read_record(version_path) != current:
            raise Blocked(24, 'version_registration_changed')
        final_commit_check(context, root, participants, api, existing=receipt['root'] is not None)
        if receipt['root'] is None:
            registry.write_record(root / '.install/root.json', owner)
        if operation == 'uninstall':
            files, directories = manifest.inventory(root / '.install')
            # Product payload and Inno EXE/DAT/MSG remain exclusively Inno's responsibility.
            removed = []
            for name, path in sorted(files.items()):
                path.unlink()
                removed.append('.install/' + name)
            launcher.unlink()
            removed.append('session-panel.cmd')
            for name in sorted(directories, key=lambda value: value.count('/'), reverse=True):
                (root / '.install' / name).rmdir()
            (root / '.install').rmdir()
            return {'schema': 1, 'status': 'committed', 'operation': operation, 'version_id': context.version_id,
                    'closure': context.document['closure'], 'exit': 0, 'removed_management_files': removed}
        owner['uninstaller'] = api.registered_uninstaller(root)
        registry.write_record(root / '.install/root.json', owner)
        registry.write_record(version_path, current)
        if launcher.exists():
            raw = launcher.read_bytes()
            backup = root / '.install/backups' / (hashlib.sha256(raw).hexdigest() + '.cmd')
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                with backup.open('xb') as stream:
                    stream.write(raw)
        raw = ('@echo off\r\n"%~dp0versions\\' + context.version_id
               + '\\runtime\\python.exe" -I -B -X utf8 -m session_panel.gateway %*\r\n').encode('ascii')
        temporary = root / ('.session-panel-' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, launcher)
        finally:
            if temporary.exists():
                temporary.unlink()
    return {'schema': 1, 'status': 'committed', 'operation': operation, 'version_id': context.version_id,
            'closure': context.document['closure'], 'exit': 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'preflight', 'verify', 'commit'))
    parser.add_argument('--install-root', type=Path, required=True)
    parser.add_argument('--operation', choices=sorted(OPERATIONS), required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--invocation-file', type=Path)
    parser.add_argument('--receipt-file', type=Path)
    parser.add_argument('--payload', type=Path)
    args = parser.parse_args(argv)
    try:
        new_report_path(args.report, registry.checked_path(str(args.install_root)))
        api = registry.default_api()
        invocation = registry.read_record(args.invocation_file) if args.invocation_file else None
        if args.command == 'prepare':
            result = prepare_invocation(args.install_root, args.operation, api=api)
        elif args.command == 'preflight':
            return preflight(args.install_root, args.operation, args.report, api=api, invocation=invocation, payload=args.payload)
        else:
            if args.payload is None:
                raise Blocked(24, 'payload_required')
            context = registry.ProductContext.load(args.payload)
            if args.command == 'commit':
                if args.receipt_file is None:
                    raise Blocked(24, 'receipt_required')
                receipt = registry.read_record(args.receipt_file)
                if receipt.get('operation') != args.operation:
                    raise Blocked(24, 'receipt_operation')
                result = commit_install(context, args.install_root, api=api, invocation=invocation, receipt=receipt)
            else:
                with ExitStack() as stack:
                    lock_scope(args.install_root, args.operation, invocation, api, stack)
                    verify_install(context, args.install_root, api=api, invocation=invocation)
                result = {'schema': 1, 'status': 'ready', 'operation': args.operation, 'version_id': context.version_id,
                          'closure': context.document['closure'], 'exit': 0}
        report_file(args.report, result)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        code = error.code if isinstance(error, Blocked) else 26
        reason = getattr(error, 'reason', 'io_or_incomplete')
        print(json.dumps({'status': 'blocked', 'reason': reason, 'exit': code}), file=sys.stderr)
        return code


if __name__ == '__main__':
    raise SystemExit(main())
