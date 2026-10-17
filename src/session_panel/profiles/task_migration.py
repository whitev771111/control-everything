"""Explicit offline schema1 task migration and schema2 preservation."""

import hashlib
import json
import sqlite3
from pathlib import Path

from session_panel.profiles.profile import Profile, ProfileError, config_file_lock, resolve_data_dir, strict_json
from session_panel.workspace import task_schema as tasks
from session_panel.workspace.store import WorkspaceError, now_utc

LIMITS = {'projects': 1000, 'tasks': 100000, 'todos': 100000,
          'manual_records': 100000, 'project_manual': 10000, 'project_manual_history': 10000}
DECODED_LIMIT = 64 * 1024 * 1024


def existing_profile(data_dir):
    profile = Profile(resolve_data_dir(data_dir))
    if not profile.config_path.is_file() or not profile.store.path.is_file():
        raise ProfileError('profile_missing', status=404)
    profile.config()
    return profile


def raw_connection(path, *, readonly=False):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro' if readonly else path,
                                 uri=readonly, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout=5000')
    connection.execute('PRAGMA foreign_keys=ON')
    if readonly:
        connection.execute('PRAGMA query_only=ON')
    return connection


def integrity(connection):
    if ([tuple(row) for row in connection.execute('PRAGMA integrity_check')] != [('ok',)]
            or connection.execute('PRAGMA foreign_key_check').fetchone() is not None):
        raise WorkspaceError('schema_error')


def check_source(connection):
    tasks.validate_schema(connection, 1)
    integrity(connection)
    consumed = 0
    for table in tasks.SCHEMA1_TABLES:
        count = connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        if table in LIMITS and count > LIMITS[table]:
            raise WorkspaceError('migration_budget')
        for row in connection.execute(f'SELECT * FROM {table}'):
            if table in ('tasks', 'todos'):
                tasks.mapped_item('task' if table == 'tasks' else 'todo', dict(row), 0)
            consumed += len(json.dumps(dict(row), ensure_ascii=False).encode('utf-8'))
            if consumed > DECODED_LIMIT:
                raise WorkspaceError('migration_budget')
    return tasks.table_facts(connection, 1)


def config_digest(profile):
    profile.config()  # Validate the actual bytes, not a cached configuration.
    return hashlib.sha256(profile.config_path.read_bytes()).hexdigest()


def copy_items(connection):
    order = 0
    for origin, table, sort in (('todo', 'todos', 'position,id'), ('task', 'tasks', 'updated_at DESC,id')):
        for raw in connection.execute(f'SELECT * FROM {table} ORDER BY {sort}'):
            row = dict(raw)
            item = tasks.mapped_item(origin, row, order)
            tasks.insert_item(connection, item)
            if origin == 'task':
                restored = {key: item['origin_id'] if key == 'id' else item[key] for key in row}
            else:
                restored = dict(id=item['origin_id'], project_id=item['project_id'], text=item['title'],
                                done=int(item['state'] == 'done'), created_at=item['created_at'],
                                updated_at=item['updated_at'], position=item['todo_position'])
            if row != restored:
                raise WorkspaceError('migration_verification_failed')
            order += 1
    if connection.execute('SELECT COUNT(*) FROM task_items').fetchone()[0] != order:
        raise WorkspaceError('migration_verification_failed')


def _failure(error, phase, backup, *, committed=False):
    reason = str(error) if isinstance(error, WorkspaceError) else (
        'store_busy' if isinstance(error, sqlite3.OperationalError) and
        any(word in str(error).lower() for word in ('busy', 'locked')) else
        error.reason if isinstance(error, ProfileError) else 'storage_error')
    result = ProfileError(reason, status=error.status if isinstance(error, ProfileError) else
                          409 if reason in ('store_busy', 'migration_source_changed') else
                          500 if reason in ('storage_error', 'migration_outcome_unknown') else 422,
                          message=error.message if isinstance(error, ProfileError) else None)
    result.phase, result.backup, result.committed = phase, backup.name if backup else None, committed
    return result


def migrate_tasks(data_dir):
    from session_panel.profiles.profile_import import create_backup, digest
    profile = existing_profile(data_dir)
    phase, backup, connection, committed = 'preflight', None, None, False
    with profile.config_lock, config_file_lock(profile.data_dir), profile.store.write_lock:
        try:
            reader = raw_connection(profile.store.path, readonly=True)
            try:
                reader.execute('BEGIN')
                version = reader.execute('PRAGMA user_version').fetchone()[0]
                if version == 2:
                    tasks.validate_schema(reader, 2)
                    integrity(reader)
                    return {'status': 'already_current', 'schema_version': 2}
                check_source(reader)
            finally:
                reader.close()
            config_hash = config_digest(profile)
            phase = 'backup'
            backup = create_backup(profile)
            if strict_json((backup / 'profile.json').read_text(encoding='utf-8')) != profile.config():
                raise WorkspaceError('migration_source_changed')
            reader = raw_connection(backup / 'management.sqlite3', readonly=True)
            try:
                source_facts = check_source(reader)
            finally:
                reader.close()
            receipt = dict(source_schema=1, source_facts=source_facts,
                           backup=str(backup.relative_to(profile.data_dir)), backup_sha256=digest(backup / 'manifest.json'),
                           config_sha256=config_hash, migrated_at=now_utc())
            phase = 'exclusive'
            connection = raw_connection(profile.store.path)
            connection.execute('BEGIN EXCLUSIVE')
            if check_source(connection) != source_facts or config_digest(profile) != config_hash:
                raise WorkspaceError('migration_source_changed')
            phase = 'copy'
            tasks.install_schema2(connection)
            copy_items(connection)
            if tasks.table_facts(connection, 1) != source_facts:
                raise WorkspaceError('migration_verification_failed')
            # Only the two known source tables are replaced; unknown objects were rejected.
            for table in ('tasks', 'todos'):
                connection.execute(f'DROP TABLE {table}')
            tasks.finish_schema2(connection, receipt)
            phase = 'validate'
            tasks.validate_schema(connection, 2)
            integrity(connection)
            phase = 'commit'
            connection.execute('COMMIT')
            committed = True
            connection.close()
            connection = None
            connection = raw_connection(profile.store.path, readonly=True)
            tasks.validate_schema(connection, 2)
            actual = json.loads(connection.execute('SELECT migration_json FROM task_center_meta').fetchone()[0])
            if actual != receipt:
                raise WorkspaceError('migration_outcome_unknown')
            return {'status': 'ok', 'schema_version': 2, 'backup': str(backup), 'receipt': receipt}
        except Exception as error:
            if phase == 'commit' and connection is not None and not connection.in_transaction:
                committed = True
            if connection is not None and connection.in_transaction:
                connection.execute('ROLLBACK')
            if committed:
                error = WorkspaceError('migration_outcome_unknown')
            raise _failure(error, phase, backup, committed=committed) from error
        finally:
            if connection is not None:
                connection.close()


def preserve_tasks(data_dir):
    from session_panel.profiles.profile_import import create_backup, digest
    profile = existing_profile(data_dir)
    backup, connection = None, None
    with profile.config_lock, config_file_lock(profile.data_dir), profile.store.write_lock:
        try:
            with profile.store.connection():
                pass
            config_hash = config_digest(profile)
            backup = create_backup(profile)
            if strict_json((backup / 'profile.json').read_text(encoding='utf-8')) != profile.config():
                raise WorkspaceError('migration_source_changed')
            reader = raw_connection(backup / 'management.sqlite3', readonly=True)
            try:
                facts = tasks.table_facts(reader, 2)
            finally:
                reader.close()
            connection = raw_connection(profile.store.path)
            connection.execute('BEGIN EXCLUSIVE')
            tasks.validate_schema(connection, 2)
            if tasks.table_facts(connection, 2) != facts or config_digest(profile) != config_hash:
                raise WorkspaceError('migration_source_changed')
            row = connection.execute('SELECT migration_json FROM task_center_meta').fetchone()[0]
            connection.execute('ROLLBACK')  # Read-only preservation checkpoint, no downgrade DDL.
            return {'status': 'preserved', 'schema_version': 2, 'backup': str(backup),
                    'backup_sha256': digest(backup / 'manifest.json'), 'facts': facts,
                    'original_migration': json.loads(row) if row is not None else None}
        except Exception as error:
            if connection is not None and connection.in_transaction:
                connection.execute('ROLLBACK')
            raise _failure(error, 'preserve', backup) from error
        finally:
            if connection is not None:
                connection.close()


def migration_status(data_dir):
    profile = existing_profile(data_dir)
    connection = raw_connection(profile.store.path, readonly=True)
    try:
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        tasks.validate_schema(connection, version)
        return {'status': 'migration_required' if version == 1 else 'current', 'schema_version': version,
                'next_command': 'migrate-tasks' if version == 1 else None}
    except (sqlite3.Error, WorkspaceError) as error:
        raise _failure(error, 'status', None) from error
    finally:
        connection.close()
