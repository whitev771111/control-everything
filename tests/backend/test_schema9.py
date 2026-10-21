"""Schema9 migration and execution-ledger contracts over disposable profiles."""
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from control_everything.execution.store import ExecutionStore
from control_everything.profiles.profile import DEFAULT_CONFIG, Profile, ProfileError
from control_everything.profiles import task_migration as migration, profile_import as backups
from control_everything.workspace import task_schema as schema
from control_everything.workspace.store import WorkspaceStore, WorkspaceError
from tests.backend.test_task_migration import schema1_profile


class Schema9Tests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def old_profile(self, version):
        root = self.root / ('v' + str(version))
        if version == 1:
            return schema1_profile(root)
        root.mkdir()
        (root/'profile.json').write_text(json.dumps(DEFAULT_CONFIG), encoding='utf8')
        with closing(sqlite3.connect(root/'workspace-state.sqlite3')) as conn:
            getattr(schema, 'install_schema' + str(version))(conn, fresh=True)
            conn.execute("INSERT INTO task_items(id,origin,origin_id,title,state,sort_index,resume_state,revision) "
                         "VALUES('task','task','original','Synthetic preserved task','todo',0,'todo',1)")
            conn.commit()
        return root

    def admit(self, profile):
        store = ExecutionStore(profile.store)
        run, replayed = store.admit({'request_id': str(uuid4()), 'task_id': 'task', 'prompt': 'Synthetic prompt'},
            'hash', 'owner', lambda conn: {'task_title': 'Synthetic task', 'project_id': 'project',
                'project_name': 'Synthetic project', 'cwd': str(self.root), 'root_identity': '{}', 'mode': 'read-only'})
        self.assertFalse(replayed)
        store.update_run(run['id'], state='running', thread_id='thread', turn_id='turn')
        store.append_event(run['id'], 'phase', {'phase': 'synthetic'})
        store.add_approval(run['id'], 'owner', '1', 'item/commandExecution/requestApproval',
                           'thread', 'turn', 'item', {'command': 'synthetic'}, True)
        return store, run

    def test_fresh_profile_execution_reopen_and_complete_backup_restore(self):
        profile = Profile.open(str(self.root/'fresh'))
        store, run = self.admit(profile)
        before = store.snapshot(run['id'])
        for _ in range(3):
            reopened = Profile.open(str(profile.data_dir))
            self.assertEqual(ExecutionStore(reopened.store).snapshot(run['id']), before)
        backup = backups.create_backup(profile)
        manifest = backups.validate_backup(backup)
        self.assertEqual(manifest['version'], 9)
        for name in ['execution_runs', 'execution_events', 'execution_approvals']:
            self.assertEqual(manifest['counts'][name], 1)
        target = backups.restore_backup(str(backup), str(self.root/'restored'))
        restored = Profile.open(str(target))
        self.assertEqual(ExecutionStore(restored.store).snapshot(run['id']), before)
        self.assertEqual(migration.preserve_tasks(str(target))['schema_version'], 9)

    def test_all_old_schema_versions_upgrade_preserve_facts_and_idempotency(self):
        for version in range(1, 9):
            with self.subTest(version=version):
                root = self.old_profile(version)
                with closing(migration.raw_connection(root/'workspace-state.sqlite3', readonly=True)) as conn:
                    before = schema.table_facts(conn, version)
                    original_rows = {}
                    if version > 1:
                        for table in schema.tables_for(version):
                            columns = [row[1] for row in conn.execute('PRAGMA table_info('+table+')')]
                            original_rows[table] = (columns, [tuple(row) for row in conn.execute(
                                'SELECT '+','.join(columns)+' FROM '+table+' ORDER BY '+columns[0])])
                result = migration.migrate_tasks(str(root))
                self.assertEqual(result['schema_version'], 9)
                self.assertEqual(backups.validate_backup(result['backup'])['version'], version)
                profile = Profile.open(str(root))
                with profile.store.connection() as conn:
                    schema.validate_schema(conn, 9)
                    after = schema.table_facts(conn, 9)
                kept = schema.KEPT_TABLES if version == 1 else [x for x in schema.tables_for(version) if x != 'task_center_meta']
                for table in kept:
                    if version == 1:
                        self.assertEqual(before[table], after[table], table)
                    else:
                        columns, rows = original_rows[table]
                        with profile.store.connection() as conn:
                            preserved = [tuple(row) for row in conn.execute(
                                'SELECT '+','.join(columns)+' FROM '+table+' ORDER BY '+columns[0])]
                        self.assertEqual(rows, preserved, table)
                        self.assertEqual(before[table]['count'], after[table]['count'], table)
                self.assertEqual(after['execution_runs']['count'], 0)
                self.assertEqual(migration.migrate_tasks(str(root))['status'], 'already_current')

    def test_schema8_requires_explicit_migration_and_execution_cannot_change_it(self):
        root = self.old_profile(8)
        path = root/'workspace-state.sqlite3'
        before = path.read_bytes()
        store = ExecutionStore(WorkspaceStore(path, profile=True))
        with self.assertRaises(WorkspaceError) as caught:
            store.find_request(str(uuid4()))
        self.assertEqual(str(caught.exception), 'migration_required')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(migration.migration_status(str(root))['schema_version'], 8)

    def test_populated_schema8_handoff_and_review_records_are_unchanged(self):
        root = self.old_profile(8)
        with closing(migration.raw_connection(root/'workspace-state.sqlite3')) as conn:
            schema.register_writer(conn, 8)
            conn.execute("INSERT INTO task_briefs VALUES('task','Request','Acceptance','Constraints','[]','main',1,'stamp')")
            conn.execute("INSERT INTO agent_runs(id,task_id,agent_id,status,started_at,heartbeat_at,ended_at,summary,evidence_json,dispatch_id) "
                         "VALUES('run','task','agent','completed','stamp','stamp','stamp','Summary','[]','dispatch')")
            conn.execute("INSERT INTO task_dispatches VALUES('dispatch','task','agent',NULL,'main',?,'{}',?,'Request',"
                         "'manual','delivered',NULL,NULL,'run','stamp','stamp','stamp')", (str(self.root), '0'*64))
            conn.execute("INSERT INTO task_sessions(id,task_id,agent_id,source,source_instance,external_id,created_at,updated_at) "
                         "VALUES('session','task','agent','codex','synthetic-home','native','stamp','stamp')")
            conn.execute("INSERT INTO task_messages VALUES('message','task','run','agent','token','human_to_agent','note',"
                         "'Keep this text','manual','sent','stamp','stamp','stamp')")
            conn.execute("INSERT INTO task_reviews VALUES('review','task','run','approved','Reviewed','[]','human','stamp')")
            conn.execute("INSERT INTO task_events VALUES('event','task','run','completed','{}','stamp')")
            conn.commit()
            before = schema.table_facts(conn, 8)
        migration.migrate_tasks(str(root))
        with Profile.open(str(root)).store.connection() as conn:
            after = schema.table_facts(conn, 9)
            for table in schema.SCHEMA8_TABLES:
                self.assertEqual(before[table], after[table], table)

    def test_failed_partial_ddl_rolls_back_and_keeps_valid_backup(self):
        root = self.old_profile(8)
        def broken(conn):
            conn.execute(schema.EXECUTION_RUN_DDL)
            raise WorkspaceError('synthetic_partial_ddl_failure')
        with patch.object(schema, 'add_schema9', side_effect=broken):
            with self.assertRaises(ProfileError) as caught:
                migration.migrate_tasks(str(root))
        self.assertFalse(caught.exception.committed)
        with closing(migration.raw_connection(root/'workspace-state.sqlite3', readonly=True)) as conn:
            schema.validate_schema(conn, 8)
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='execution_runs'").fetchone())
        for backup in (root/'backups').iterdir():
            self.assertEqual(backups.validate_backup(backup)['schema_version'], 8)

    def test_old_writer_rejected_for_business_and_execution_tables(self):
        profile = Profile.open(str(self.root/'guarded'))
        self.admit(profile)
        with closing(sqlite3.connect(profile.store.path)) as conn:
            schema.register_writer(conn, 8)
            for table in ['task_center_meta', 'execution_runs', 'execution_events', 'execution_approvals']:
                with self.subTest(table=table):
                    column = conn.execute('PRAGMA table_info(' + table + ')').fetchone()[1]
                    with self.assertRaisesRegex(sqlite3.IntegrityError, 'writer_version_mismatch'):
                        conn.execute('UPDATE ' + table + ' SET ' + column + '=' + column)
                    conn.rollback()

    def test_unknown_extra_table_remains_rejected(self):
        profile = Profile.open(str(self.root/'extra'))
        with closing(sqlite3.connect(profile.store.path)) as conn:
            conn.execute('CREATE TABLE unrelated(synthetic TEXT)')
            conn.commit()
        with self.assertRaises(WorkspaceError) as caught:
            with profile.store.connection():
                pass
        self.assertEqual(str(caught.exception), 'schema_error')

    def test_restore_schema8_then_upgrade_again(self):
        root = self.old_profile(8)
        first = migration.migrate_tasks(str(root))
        target = backups.restore_backup(first['backup'], str(self.root/'restored-v8'))
        self.assertEqual(migration.migration_status(str(target))['schema_version'], 8)
        self.assertEqual(migration.migrate_tasks(str(target))['schema_version'], 9)
        Profile.open(str(target))


if __name__ == '__main__':
    unittest.main()
