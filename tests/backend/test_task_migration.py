"""Schema migration fault contracts using synthetic files in temporary directories."""

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from session_panel.profiles.profile import DEFAULT_CONFIG, Profile, ProfileError
from session_panel.profiles import task_migration as migration, profile_import as backups
from session_panel.workspace import task_schema as schema
from session_panel.workspace.store import WorkspaceError


def schema1_profile(root):
    root.mkdir()
    (root / 'profile.json').write_text(json.dumps(DEFAULT_CONFIG), encoding='utf-8')
    connection = sqlite3.connect(root / 'workspace-state.sqlite3')
    try:
        for statement in schema.OLD_STATEMENTS:
            connection.execute(statement)
        connection.execute('PRAGMA user_version=1')
        # Cross-source ID collisions and duplicate titles are intentional.
        connection.executemany('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)', [
            ('same', '历史 😀' * 200, 'orphan', 'codex', 'review', 'codex:old', 'manual', 'old', 'z'),
            ('second', 'Duplicate', 'gone', None, 'done', None, 'original', 'older', 'a')])
        connection.executemany('INSERT INTO todos VALUES(?,?,?,?,?,?,?)', [
            ('', None, '  First\r\n😀  ', 0, None, None, 2),
            ('same', '', 'Duplicate', 1, None, None, 7)])
        connection.execute("INSERT INTO project_manual VALUES('orphan','等我',NULL,'Old',NULL)")
        connection.execute("INSERT INTO project_manual_history VALUES('history','orphan','等我',NULL,'Old',NULL,'import')")
        connection.commit()
    finally:
        connection.close()
    return root


class TaskMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = schema1_profile(self.root / 'old')

    def facts(self, version):
        with closing(migration.raw_connection(self.path / 'workspace-state.sqlite3', readonly=True)) as conn:
            return schema.table_facts(conn, version)

    def test_explicit_migration_retains_values_orders_raw_ids_and_old_backup(self):
        before = self.facts(1)
        with self.assertRaises(ProfileError) as error:
            Profile.open(str(self.path))
        self.assertEqual(error.exception.reason, 'migration_required')
        result = migration.migrate_tasks(str(self.path))
        profile = Profile.open(str(self.path))
        store = profile.store
        rows = store.list_items()['items']
        self.assertEqual([(row['origin'], row['origin_id']) for row in rows],
                         [('todo', ''), ('todo', 'same'), ('task', 'same'), ('task', 'second')])
        self.assertEqual(rows[0]['title'], '  First\r\n😀  ')
        self.assertIsNone(rows[0]['created_at'])
        self.assertIsNone(rows[0]['project_id'])
        self.assertEqual(rows[1]['project_id'], '')
        self.assertEqual(rows[2]['state'], 'review')
        self.assertEqual(store.get_task('same')['session_key'], 'codex:old')
        after = self.facts(2)
        for table in schema.KEPT_TABLES:
            self.assertEqual(after[table], before[table])
        self.assertEqual(backups.validate_backup(result['backup'])['schema_version'], 1)
        restored = backups.restore_backup(result['backup'], str(self.root / 'restored'))
        self.assertEqual(migration.migration_status(str(restored))['schema_version'], 1)
        with self.assertRaises(ProfileError):
            Profile.open(str(restored))
        old_backups = set((self.path / 'backups').iterdir())
        self.assertEqual(migration.migrate_tasks(str(self.path))['status'], 'already_current')
        self.assertEqual(set((self.path / 'backups').iterdir()), old_backups)

    def test_copy_failure_rolls_back_and_keeps_valid_backup(self):
        before = self.facts(1)
        real = migration.copy_items
        def fail_after_copy(connection):
            real(connection)
            raise WorkspaceError('fixture_copy_failure')
        with patch.object(migration, 'copy_items', side_effect=fail_after_copy):
            with self.assertRaises(ProfileError) as error:
                migration.migrate_tasks(str(self.path))
        self.assertEqual(error.exception.phase, 'copy')
        self.assertFalse(error.exception.committed)
        self.assertEqual(self.facts(1), before)
        with closing(migration.raw_connection(self.path / 'workspace-state.sqlite3', readonly=True)) as conn:
            schema.validate_schema(conn, 1)
        for backup in (self.path / 'backups').iterdir():
            self.assertEqual(backups.validate_backup(backup)['version'], 1)

    def test_backup_gap_writer_is_detected_and_not_lost(self):
        real = backups.create_backup
        def changed(profile):
            backup = real(profile)
            with closing(sqlite3.connect(profile.store.path)) as writer:
                writer.execute("UPDATE todos SET text='External change' WHERE id=''")
                writer.commit()
            return backup
        with patch.object(backups, 'create_backup', side_effect=changed):
            with self.assertRaises(ProfileError) as error:
                migration.migrate_tasks(str(self.path))
        self.assertEqual(error.exception.reason, 'migration_source_changed')
        with closing(migration.raw_connection(self.path / 'workspace-state.sqlite3', readonly=True)) as conn:
            schema.validate_schema(conn, 1)
            self.assertEqual(conn.execute("SELECT text FROM todos WHERE id=''").fetchone()[0], 'External change')

    def test_held_old_connection_cached_statements_reject_after_commit(self):
        old = sqlite3.connect(self.path / 'workspace-state.sqlite3')
        try:
            # These SQL strings populate the same connection's statement cache.
            old.execute("UPDATE todos SET done=done WHERE id=''")
            old.execute("UPDATE project_manual SET note=note WHERE project_id='orphan'")
            old.commit()
            migration.migrate_tasks(str(self.path))
            for sql in ("UPDATE todos SET done=done WHERE id=''",
                        "UPDATE project_manual SET note=note WHERE project_id='orphan'"):
                with self.assertRaises(sqlite3.OperationalError):
                    old.execute(sql)
                old.rollback()
        finally:
            old.close()

    def test_wide_edit_roundtrip_and_new_data_preserved_before_old_restore(self):
        migration.migrate_tasks(str(self.path))
        profile = Profile.open(str(self.path))
        old = next(row for row in profile.store.list_items()['items'] if row['origin'] == 'task')
        same = profile.store.save_item({'title': old['title']}, item_id=old['id'], expected_revision=old['revision'])
        self.assertEqual(same, old)
        changed = profile.store.save_item({'state': 'blocked'}, item_id=old['id'], expected_revision=old['revision'])
        self.assertEqual(changed['title'], old['title'])
        preserved = migration.preserve_tasks(str(self.path))
        self.assertEqual(backups.validate_backup(preserved['backup'])['version'], 2)
        new_dir = backups.restore_backup(preserved['backup'], str(self.root / 'schema2-restored'))
        self.assertEqual(Profile.open(str(new_dir)).store.get_item(old['id']), changed)
        migration_receipt = preserved['original_migration']
        backup1 = self.path / migration_receipt['backup']
        old_dir = backups.restore_backup(str(backup1), str(self.root / 'old-restore'))
        self.assertEqual(migration.migration_status(str(old_dir))['schema_version'], 1)
        self.assertEqual(Profile.open(str(self.path)).store.get_item(old['id']), changed)

    def test_unknown_objects_and_edit_budget_refuse_without_touching_source(self):
        with closing(sqlite3.connect(self.path / 'workspace-state.sqlite3')) as conn:
            conn.execute('CREATE TABLE private_unknown(secret TEXT)')
            conn.execute("INSERT INTO private_unknown VALUES('Synthetic private sentinel')")
            conn.commit()
        raw = (self.path / 'workspace-state.sqlite3').read_bytes()
        with self.assertRaises(ProfileError) as error:
            migration.migrate_tasks(str(self.path))
        self.assertEqual(error.exception.reason, 'schema_error')
        self.assertEqual((self.path / 'workspace-state.sqlite3').read_bytes(), raw)
        for text in ('中' * 2000, '😀' * 2000, '\\"\n' * 2000):
            schema.check_text(text)
        with self.assertRaisesRegex(WorkspaceError, 'migration_edit_budget'):
            schema.check_text('x' * 50000)

    def test_schema_and_guard_tamper_cannot_be_restored_with_rehashed_manifest(self):
        migration.migrate_tasks(str(self.path))
        backup = backups.create_backup(Profile.open(str(self.path)))
        with closing(sqlite3.connect(backup / 'management.sqlite3')) as conn:
            conn.execute('DROP TRIGGER guard_project_sources_delete')
            conn.commit()
        manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
        manifest['files']['management.sqlite3'] = dict(size=(backup / 'management.sqlite3').stat().st_size,
                                                       sha256=backups.digest(backup / 'management.sqlite3'))
        (backup / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(ProfileError):
            backups.restore_backup(str(backup), str(self.root / 'must-not-publish'))
        self.assertFalse((self.root / 'must-not-publish').exists())

    def test_install_and_final_validation_faults_are_atomic(self):
        before = self.facts(1)
        finish = schema.finish_schema2
        def late_failure(connection, receipt):
            finish(connection, receipt)
            raise WorkspaceError('fixture_after_ddl')
        with patch.object(schema, 'finish_schema2', side_effect=late_failure):
            with self.assertRaises(ProfileError):
                migration.migrate_tasks(str(self.path))
        self.assertEqual(self.facts(1), before)
        validate = schema.validate_schema
        def invalid_target(connection, version):
            validate(connection, version)
            if version == 2:
                raise WorkspaceError('fixture_final_validation')
        with patch.object(schema, 'validate_schema', side_effect=invalid_target):
            with self.assertRaises(ProfileError) as error:
                migration.migrate_tasks(str(self.path))
        self.assertEqual(error.exception.phase, 'validate')
        self.assertEqual(self.facts(1), before)

    def test_commit_failure_rollback_and_lost_reply_outcome_unknown(self):
        actual = migration.raw_connection
        before = self.facts(1)
        class CommitFault:
            def __init__(self, connection, after):
                self.connection, self.after = connection, after
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def execute(self, sql, *args):
                if sql == 'COMMIT':
                    if self.after:
                        self.connection.execute(sql, *args)
                    raise sqlite3.OperationalError('fixture_commit_reply')
                return self.connection.execute(sql, *args)
        for after in (False, True):
            def connect(path, *, readonly=False):
                result = actual(path, readonly=readonly)
                return result if readonly else CommitFault(result, after)
            with patch.object(migration, 'raw_connection', side_effect=connect):
                with self.assertRaises(ProfileError) as error:
                    migration.migrate_tasks(str(self.path))
            self.assertEqual(error.exception.phase, 'commit')
            if after:
                self.assertEqual(error.exception.reason, 'migration_outcome_unknown')
                self.assertTrue(error.exception.committed)
                self.assertEqual(migration.migration_status(str(self.path))['status'], 'current')
                self.assertEqual(migration.migrate_tasks(str(self.path))['status'], 'already_current')
            else:
                self.assertFalse(error.exception.committed)
                self.assertEqual(self.facts(1), before)
        # A committed migration must not inspect the already-closed writer when reopening fails.
        # Match the canonical writer path when TEMP uses a Windows 8.3 alias.
        reload_path = schema1_profile(self.root / 'reload-fault').resolve()
        main_reads = 0
        def unreadable_after_commit(path, *, readonly=False):
            nonlocal main_reads
            if readonly and Path(path) == reload_path / 'workspace-state.sqlite3':
                main_reads += 1
                if main_reads == 2:
                    raise OSError('fixture_post_commit_read')
            return actual(path, readonly=readonly)
        with patch.object(migration, 'raw_connection', side_effect=unreadable_after_commit):
            with self.assertRaises(ProfileError) as error:
                migration.migrate_tasks(str(reload_path))
        self.assertEqual(error.exception.reason, 'migration_outcome_unknown')
        self.assertTrue(error.exception.committed)
        self.assertEqual(migration.migration_status(str(reload_path))['status'], 'current')

    def test_both_backup_versions_rebind_v3_only_in_restored_copy(self):
        from tests.backend.test_observation_settings import registration, save
        profile = Profile(self.path)  # Explicit schema1 fixture; never implicit upgrade.
        save(profile, [registration()])
        original_revision = profile.observation_config()['revision']
        for version in (1, 2):
            if version == 2:
                migration.migrate_tasks(str(self.path))
                profile = Profile.open(str(self.path))
            backup = backups.create_backup(profile)
            before = {file.name: backups.digest(file) for file in backup.iterdir()}
            target = backups.restore_backup(str(backup), str(self.root / f'restore-v{version}'))
            restored = Profile(target)
            config = restored.config()
            self.assertEqual(config['version'], 3)
            self.assertEqual(config['observations']['revision'], original_revision + 1)
            self.assertEqual(config['observations']['registrations'][0]['binding_state'], 'rebind_required')
            self.assertEqual(migration.migration_status(str(target))['schema_version'], version)
            self.assertEqual({file.name: backups.digest(file) for file in backup.iterdir()}, before)
            self.assertEqual(profile.observation_config()['registrations'][0]['binding_state'], 'confirmed')
