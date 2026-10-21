"""Explicit offline schema1/schema2 task migration and schema3 preservation."""

import hashlib
import json
import sqlite3
from pathlib import Path

from control_everything.profiles.profile import Profile, ProfileError, config_file_lock, resolve_data_dir, strict_json
from control_everything.workspace import task_schema as tasks
from control_everything.workspace.store import WorkspaceError, now_utc

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
    from control_everything.profiles.profile_import import create_backup, digest
    profile = existing_profile(data_dir)
    phase, backup, connection, committed = 'preflight', None, None, False
    with profile.config_lock, config_file_lock(profile.data_dir), profile.store.write_lock:
        try:
            reader = raw_connection(profile.store.path, readonly=True)
            try:
                reader.execute('BEGIN')
                version = reader.execute('PRAGMA user_version').fetchone()[0]
                if version == 9:
                    tasks.validate_schema(reader, 9)
                    integrity(reader)
                    return {'status': 'already_current', 'schema_version': 9}
                if version not in (1, 2, 3, 4, 5, 6, 7, 8):
                    raise WorkspaceError('schema_error')
                if version == 1:
                    source_facts = check_source(reader)
                elif version == 2:
                    tasks.validate_schema(reader, 2)
                    integrity(reader)
                    source_facts = tasks.table_facts(reader, 2)
                elif version == 3:
                    tasks.validate_schema(reader, 3)
                    integrity(reader)
                    source_facts = tasks.table_facts(reader, 3)
                elif version == 4:
                    tasks.validate_schema(reader, 4)
                    integrity(reader)
                    source_facts = tasks.table_facts(reader, 4)
                elif version == 5:
                    tasks.validate_schema(reader, 5)
                    integrity(reader)
                    source_facts = tasks.table_facts(reader, 5)
                elif version == 6:
                    tasks.validate_schema(reader, 6)
                    integrity(reader)
                    source_facts = tasks.table_facts(reader, 6)
                elif version == 7:
                    tasks.validate_schema(reader, 7)
                    integrity(reader)
                    source_facts = tasks.table_facts(reader, 7)
                else:
                    tasks.validate_schema(reader, 8)
                    integrity(reader)
                    source_facts = tasks.table_facts(reader, 8)
            finally:
                reader.close()
            config_hash = config_digest(profile)
            phase = 'backup'
            backup = create_backup(profile)
            if strict_json((backup / 'profile.json').read_text(encoding='utf-8')) != profile.config():
                raise WorkspaceError('migration_source_changed')
            reader = raw_connection(backup / 'management.sqlite3', readonly=True)
            try:
                if version == 1:
                    if check_source(reader) != source_facts:
                        raise WorkspaceError('migration_source_changed')
                elif version == 2:
                    tasks.validate_schema(reader, 2)
                    integrity(reader)
                    if tasks.table_facts(reader, 2) != source_facts:
                        raise WorkspaceError('migration_source_changed')
                elif version == 3:
                    tasks.validate_schema(reader, 3)
                    integrity(reader)
                    if tasks.table_facts(reader, 3) != source_facts:
                        raise WorkspaceError('migration_source_changed')
                elif version == 4:
                    tasks.validate_schema(reader, 4)
                    integrity(reader)
                    if tasks.table_facts(reader, 4) != source_facts:
                        raise WorkspaceError('migration_source_changed')
                elif version == 5:
                    tasks.validate_schema(reader, 5)
                    integrity(reader)
                    if tasks.table_facts(reader, 5) != source_facts:
                        raise WorkspaceError('migration_source_changed')
                elif version == 6:
                    tasks.validate_schema(reader, 6)
                    integrity(reader)
                    if tasks.table_facts(reader, 6) != source_facts:
                        raise WorkspaceError('migration_source_changed')
                elif version == 7:
                    tasks.validate_schema(reader, 7)
                    integrity(reader)
                    if tasks.table_facts(reader, 7) != source_facts:
                        raise WorkspaceError('migration_source_changed')
                else:
                    tasks.validate_schema(reader, 8)
                    integrity(reader)
                    if tasks.table_facts(reader, 8) != source_facts:
                        raise WorkspaceError('migration_source_changed')
            finally:
                reader.close()
            receipt = None
            if version == 1:
                receipt = dict(source_schema=1, source_facts=source_facts,
                               backup=str(backup.relative_to(profile.data_dir)), backup_sha256=digest(backup / 'manifest.json'),
                               config_sha256=config_hash, migrated_at=now_utc())
            phase = 'exclusive'
            connection = raw_connection(profile.store.path)
            connection.execute('BEGIN EXCLUSIVE')
            if config_digest(profile) != config_hash:
                raise WorkspaceError('migration_source_changed')
            if version == 1:
                if check_source(connection) != source_facts:
                    raise WorkspaceError('migration_source_changed')
                phase = 'copy'
                tasks.create_schema2_tables(connection, fresh=False)
                tasks.add_schema3(connection)
                tasks.add_schema4(connection)
                tasks.add_schema5(connection)
                tasks.add_schema6(connection)
                tasks.add_schema7(connection)
                tasks.add_schema8(connection)
                tasks.add_schema9(connection)
                copy_items(connection)
                if tasks.table_facts(connection, 1) != source_facts:
                    raise WorkspaceError('migration_verification_failed')
                # Only the two known source tables are replaced; unknown objects were rejected.
                for table in ('tasks', 'todos'):
                    connection.execute(f'DROP TABLE {table}')
                tasks.finish_schema9(connection, receipt)
            elif version == 2:
                tasks.validate_schema(connection, 2)
                if tasks.table_facts(connection, 2) != source_facts:
                    raise WorkspaceError('migration_source_changed')
                phase = 'copy'
                tasks.add_schema3(connection)
                tasks.add_schema4(connection)
                tasks.add_schema5(connection)
                tasks.add_schema6(connection)
                tasks.add_schema7(connection)
                tasks.add_schema8(connection)
                tasks.add_schema9(connection)
                tasks.finish_schema9(connection, None)
                after = tasks.table_facts(connection, 9)
                for table in tasks.SCHEMA2_TABLES:
                    if after[table]['count'] != source_facts[table]['count']:
                        raise WorkspaceError('migration_verification_failed')
            elif version == 3:
                tasks.validate_schema(connection, 3)
                if tasks.table_facts(connection, 3) != source_facts:
                    raise WorkspaceError('migration_source_changed')
                phase = 'copy'
                tasks.add_schema4(connection)
                tasks.add_schema5(connection)
                tasks.add_schema6(connection)
                tasks.add_schema7(connection)
                tasks.add_schema8(connection)
                tasks.add_schema9(connection)
                tasks.finish_schema9(connection, None)
                after = tasks.table_facts(connection, 9)
                for table in tasks.SCHEMA3_TABLES:
                    if after[table]['count'] != source_facts[table]['count']:
                        raise WorkspaceError('migration_verification_failed')
            elif version == 4:
                tasks.validate_schema(connection, 4)
                if tasks.table_facts(connection, 4) != source_facts:
                    raise WorkspaceError('migration_source_changed')
                phase = 'copy'
                tasks.add_schema5(connection)
                tasks.add_schema6(connection)
                tasks.add_schema7(connection)
                tasks.add_schema8(connection)
                tasks.add_schema9(connection)
                tasks.finish_schema9(connection, None)
                after = tasks.table_facts(connection, 9)
                for table in tasks.SCHEMA4_TABLES:
                    if after[table]['count'] != source_facts[table]['count']:
                        raise WorkspaceError('migration_verification_failed')
            elif version == 5:
                tasks.validate_schema(connection, 5)
                if tasks.table_facts(connection, 5) != source_facts:
                    raise WorkspaceError('migration_source_changed')
                phase = 'copy'
                tasks.add_schema6(connection)
                tasks.add_schema7(connection)
                tasks.add_schema8(connection)
                tasks.add_schema9(connection)
                tasks.finish_schema9(connection, None)
                after = tasks.table_facts(connection, 9)
                for table in tasks.SCHEMA5_TABLES:
                    if after[table]['count'] != source_facts[table]['count']:
                        raise WorkspaceError('migration_verification_failed')
            elif version == 6:
                tasks.validate_schema(connection, 6)
                if tasks.table_facts(connection, 6) != source_facts:
                    raise WorkspaceError('migration_source_changed')
                phase = 'copy'
                tasks.add_schema7(connection)
                tasks.add_schema8(connection)
                tasks.add_schema9(connection)
                tasks.finish_schema9(connection, None)
                after = tasks.table_facts(connection, 9)
                for table in tasks.SCHEMA6_TABLES:
                    if after[table]['count'] != source_facts[table]['count']:
                        raise WorkspaceError('migration_verification_failed')
            elif version == 7:
                tasks.validate_schema(connection, 7)
                if tasks.table_facts(connection, 7) != source_facts:
                    raise WorkspaceError('migration_source_changed')
                phase = 'copy'
                tasks.add_schema8(connection)
                tasks.add_schema9(connection)
                tasks.finish_schema9(connection, None)
                after = tasks.table_facts(connection, 9)
                for table in tasks.SCHEMA7_TABLES:
                    if after[table]['count'] != source_facts[table]['count']:
                        raise WorkspaceError('migration_verification_failed')
            else:
                tasks.validate_schema(connection, 8)
                if tasks.table_facts(connection, 8) != source_facts:
                    raise WorkspaceError('migration_source_changed')
                phase = 'copy'
                tasks.add_schema9(connection)
                tasks.finish_schema9(connection, None)
                after = tasks.table_facts(connection, 9)
                for table in tasks.SCHEMA8_TABLES:
                    if after[table]['count'] != source_facts[table]['count']:
                        raise WorkspaceError('migration_verification_failed')
            phase = 'validate'
            tasks.validate_schema(connection, 9)
            integrity(connection)
            phase = 'commit'
            connection.execute('COMMIT')
            committed = True
            connection.close()
            connection = None
            connection = raw_connection(profile.store.path, readonly=True)
            tasks.validate_schema(connection, 9)
            if receipt is not None:
                actual = json.loads(connection.execute('SELECT migration_json FROM task_center_meta').fetchone()[0])
                if actual != receipt:
                    raise WorkspaceError('migration_outcome_unknown')
            return {'status': 'ok', 'schema_version': 9, 'backup': str(backup), 'receipt': receipt}
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
    from control_everything.profiles.profile_import import create_backup, digest
    profile = existing_profile(data_dir)
    backup, connection, version = None, None, None
    with profile.config_lock, config_file_lock(profile.data_dir), profile.store.write_lock:
        try:
            reader = raw_connection(profile.store.path, readonly=True)
            try:
                version = reader.execute('PRAGMA user_version').fetchone()[0]
                tasks.validate_schema(reader, version)
                integrity(reader)
            finally:
                reader.close()
            if version not in (2, 3, 4, 5, 6, 7, 8, 9):
                raise WorkspaceError('schema_error')
            config_hash = config_digest(profile)
            backup = create_backup(profile)
            if strict_json((backup / 'profile.json').read_text(encoding='utf-8')) != profile.config():
                raise WorkspaceError('migration_source_changed')
            reader = raw_connection(backup / 'management.sqlite3', readonly=True)
            try:
                facts = tasks.table_facts(reader, version)
            finally:
                reader.close()
            connection = raw_connection(profile.store.path)
            connection.execute('BEGIN EXCLUSIVE')
            tasks.validate_schema(connection, version)
            if tasks.table_facts(connection, version) != facts or config_digest(profile) != config_hash:
                raise WorkspaceError('migration_source_changed')
            row = connection.execute('SELECT migration_json FROM task_center_meta').fetchone()[0]
            connection.execute('ROLLBACK')  # Read-only preservation checkpoint, no downgrade DDL.
            return {'status': 'preserved', 'schema_version': version, 'backup': str(backup),
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
        return {'status': 'migration_required' if version in (1, 2, 3, 4, 5, 6, 7, 8) else 'current', 'schema_version': version,
                'next_command': 'migrate-tasks' if version in (1, 2, 3, 4, 5, 6, 7, 8) else None}
    except (sqlite3.Error, WorkspaceError) as error:
        raise _failure(error, 'status', None) from error
    finally:
        connection.close()
