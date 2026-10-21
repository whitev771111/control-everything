"""Project sandbox preparation, immutable recovery evidence and confirmation snapshots."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from uuid import uuid4

from control_everything.platform import windows_acl

SCHEMA_VERSION = 2
_LOCK = threading.RLock()
_STATE_NAME = 'sandbox-preparations.json'


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _key(project_id, directory_id='main'):
    if any(not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', value) for value in (project_id, directory_id)):
        raise ValueError('invalid_project_directory')
    return project_id if directory_id == 'main' else project_id + '.' + directory_id


def _read(data_dir):
    try:
        value = json.loads((Path(data_dir) / _STATE_NAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'schema_version': SCHEMA_VERSION, 'items': {}}
    if not isinstance(value, dict) or value.get('schema_version') != SCHEMA_VERSION or not isinstance(value.get('items'), dict):
        return {'schema_version': SCHEMA_VERSION, 'items': {}}
    return value


def _write(data_dir, value):
    path = Path(data_dir) / _STATE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _blocked(path):
    if path == Path(path.anchor):
        return True
    protected = [Path(path.anchor) / part for part in ('Windows', 'Program Files', 'Program Files (x86)', 'ProgramData', 'System Volume Information')]
    return any(path == root or root in path.parents for root in protected)


def _target(path):
    raw = Path(path).absolute()
    if str(raw).startswith('\\\\'):
        raise ValueError('unsupported_directory')
    for entry in (raw, *raw.parents):
        if entry.is_symlink() or getattr(entry.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('reparse_path')
    target = raw.resolve()
    if not target.is_dir() or _blocked(target):
        raise ValueError('unsupported_directory')
    return target


def status(data_dir, project_id, path, *, directory_id='main'):
    key = _key(project_id, directory_id)
    try:
        target = _target(path)
        if not windows_acl.local_ntfs(target):
            return {'status': 'failed', 'reason': 'unsupported_filesystem'}
        identity = windows_acl.file_identity(target)
        sid, owner = windows_acl.current_user_sid(), windows_acl.owner_sid(target)
        current_sha = _digest(windows_acl.sddl(target))
        if windows_acl.file_identity(target) != identity:
            raise ValueError('directory_changed')
        with _LOCK:
            record = _read(data_dir)['items'].get(key)
        record = record if isinstance(record, dict) else {}
        generation = record.get('generation')
        facts = {'project_id': project_id, 'directory_id': directory_id, 'path': str(target),
                 'identity': identity, 'user_sid': sid, 'owner_sid': owner, 'acl': current_sha,
                 'generation': generation, 'version': SCHEMA_VERSION}
        confirmation = _digest(json.dumps(facts, sort_keys=True))
        ready = owner == sid and windows_acl.has_preparation_access(target, sid)
        state, reason = ('ready', None) if ready else ('required', 'preparation_rights_required')
        if owner != sid:
            state, reason = 'failed', 'owner_mismatch'
        elif record and (record.get('identity') != identity or record.get('canonical_path') != str(target)
                        or record.get('user_sid') != sid or record.get('after_sddl_sha256') != current_sha
                        or record.get('status') != 'ready'):
            state, reason = 'stale', 'directory_or_permission_changed'
        return {'status': state, 'reason': reason, 'project_id': project_id, 'directory_id': directory_id,
                'canonical_path': str(target), 'generation': generation,
                'confirmation_hash': confirmation, 'after_sddl_sha256': current_sha,
                'acl_ready': state == 'ready', 'owner_sid_matches': owner == sid}
    except ValueError:
        return {'status': 'failed', 'reason': 'unsupported_directory'}
    except OSError:
        return {'status': 'unknown', 'reason': 'acl_unreadable'}


def prepare(data_dir, project_id, path, *, directory_id='main', confirmation_hash=None):
    key, target = _key(project_id, directory_id), _target(path)
    if not windows_acl.local_ntfs(target):
        raise ValueError('unsupported_filesystem')
    # All mutations take the directory lease before the state lock.
    with windows_acl.directory_lock(target), windows_acl.state_lock(data_dir), _LOCK:
        current = status(data_dir, project_id, target, directory_id=directory_id)
        if confirmation_hash is not None and current.get('confirmation_hash') != confirmation_hash:
            raise ValueError('directory_changed')
        sid, owner = windows_acl.current_user_sid(), windows_acl.owner_sid(target)
        if owner != sid:
            raise ValueError('owner_mismatch')
        state = _read(data_dir)
        old = state['items'].get(key)
        if isinstance(old, dict) and current['status'] == 'ready':
            return current
        before, identity = windows_acl.sddl(target), windows_acl.file_identity(target)
        generation = uuid4().hex
        backup_dir = Path(data_dir) / 'sandbox-acl-backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f'{key}-{generation}.sddl'
        with backup.open('x', encoding='utf-8') as stream:
            stream.write(before)
        record = {'status': 'preparing', 'project_id': project_id, 'directory_id': directory_id,
                  'canonical_path': str(target), 'user_sid': sid, 'owner_sid': owner,
                  'identity': identity, 'generation': generation, 'format': 'root-rights-v2',
                  'prepared_at': datetime.now(timezone.utc).isoformat(),
                  'before_sddl_sha256': _digest(before), 'after_sddl_sha256': None,
                  'backup': str(backup), 'added_permission': False, 'reason': None}
        state['items'][key] = record
        _write(data_dir, state)  # No permission side effect before durable recovery evidence.
        try:
            if not windows_acl.has_preparation_access(target, sid):
                record['added_permission'] = True
                _write(data_dir, state)
                if not windows_acl.grant_current_user(target, sid):
                    raise ValueError('acl_grant_unverified')
            after = windows_acl.sddl(target)
            if windows_acl.file_identity(target) != identity or not windows_acl.has_preparation_access(target, sid):
                raise ValueError('acl_grant_unverified')
            record.update(status='ready', after_sddl_sha256=_digest(after))
            _write(data_dir, state)
        except (OSError, ValueError):
            record.update(status='failed', reason='acl_preparation_failed')
            _write(data_dir, state)
            raise
        return status(data_dir, project_id, target, directory_id=directory_id)


def require_ready(data_dir, project_id, path):
    value = status(data_dir, project_id, path)
    if value.get('status') != 'ready':
        raise ValueError('windows_sandbox_acl_required')
    return value


def restore(data_dir, project_id, *, directory_id='main'):
    key = _key(project_id, directory_id)
    with _LOCK:
        record = _read(data_dir)['items'].get(key)
    if not isinstance(record, dict):
        raise ValueError('preparation_not_found')
    target = _target(record['canonical_path'])
    with windows_acl.directory_lock(target), windows_acl.state_lock(data_dir), _LOCK:
        state = _read(data_dir)
        if state['items'].get(key) != record:
            raise ValueError('preparation_stale')
        if (windows_acl.file_identity(target) != record['identity']
                or windows_acl.current_user_sid() != record['user_sid']
                or windows_acl.owner_sid(target) != record['owner_sid']):
            raise ValueError('directory_changed')
        current = windows_acl.sddl(target)
        if record.get('after_sddl_sha256') != _digest(current):
            raise ValueError('acl_changed')
        if record.get('added_permission'):
            if record.get('format') != 'root-rights-v2':
                raise ValueError('acl_restore_requires_review')
            backup = Path(record['backup'])
            expected = Path(data_dir) / 'sandbox-acl-backups' / f"{key}-{record['generation']}.sddl"
            if backup != expected:
                raise ValueError('invalid_acl_backup')
            before = backup.read_text(encoding='utf-8')
            if _digest(before) != record['before_sddl_sha256'] or not windows_acl.restore_dacl(target, before):
                raise ValueError('acl_restore_failed')
        del state['items'][key]
        _write(data_dir, state)
        return {'status': 'restored_partial', 'reason': 'dsh_standing_acl_not_restored'}
