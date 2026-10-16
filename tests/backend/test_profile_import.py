"""Complete management imports from synthetic files and disposable databases."""
import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from session_panel.profiles.profile import Profile, ProfileError
from session_panel.profiles import profile_import as importer
from session_panel.workspace.store import BASE_SCHEMA


class ProfileImportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.profile = Profile.open(str(self.root / "destination"))
        self.write("progress-registry.json", {"projects": [{"id": "old", "name": "Old", "root": str(self.root / "project")} ]})

    def write(self, name, value):
        (self.source / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def source_database(self, tasks=501, records=101):
        stamp = "2026-01-01T00:00:00Z"
        path = self.source / "workspace-state.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(BASE_SCHEMA + "CREATE TABLE execution_private(secret TEXT);")
            connection.execute("INSERT INTO execution_private VALUES('private fixture sentinel')")
            connection.executemany("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)", [(str(uuid4()), "task", "orphan" if index == tasks - 1 else "old", "codex", "done" if index == tasks - 1 else "todo", "codex:" + str(uuid4()), "manual", stamp, stamp) for index in range(tasks)])
            connection.executemany("INSERT INTO manual_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [(str(uuid4()), "codex", "subscription", "2026-01-01", "daily_tokens", index, None, None, "note", "manual", stamp, stamp) for index in range(records)])
            connection.commit()
        return path

    def test_full_import_retains_orphans_empty_todo_id_times_and_snapshot_backup(self):
        self.write("progress-manual.json", {"old": {"state": "在做", "percent": 0, "note": "known"}, "orphan": {"state": "等我", "percent": None, "note": "orphan"}})
        todos = [{"id": "" if index == 0 else f"{index:08x}", "text": "long " * 20, "project_id": "orphan", "done": bool(index % 2)} for index in range(31)]
        self.write("progress-todos.json", todos)
        path = self.source_database()
        before = {file.name: importer.digest(file) for file in self.source.iterdir()}
        real_connect = sqlite3.connect
        accessed = []
        def guarded_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            if str(args[0]).startswith(path.resolve().as_uri()):
                def authorize(action, table, _column, _database, _trigger):
                    if action == sqlite3.SQLITE_READ:
                        accessed.append(table)
                        if table.startswith("execution"):
                            return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                connection.set_authorizer(authorize)
            return connection
        original_backup = importer.create_backup
        def checked_backup(profile):
            with closing(real_connect(profile.store.path, timeout=0)) as competitor:
                with self.assertRaises(sqlite3.OperationalError):
                    competitor.execute("INSERT INTO projects VALUES('race','Race',0,NULL,NULL,99)")
            return original_backup(profile)
        with patch.object(importer.sqlite3, "connect", side_effect=guarded_connect), patch.object(importer, "create_backup", side_effect=checked_backup):
            result = importer.import_legacy(self.profile, str(self.source))
        self.assertEqual(result["counts"], {"projects": 1, "manual": 2, "todos": 31, "tasks": 501, "manual_records": 101})
        self.assertFalse(any(table.startswith("execution") for table in accessed))
        self.assertEqual(before, {file.name: importer.digest(file) for file in self.source.iterdir()})
        self.assertEqual(self.profile.store.list_todos(limit=100)["items"][0]["id"], "")
        self.assertEqual(self.profile.store.manual_history("orphan")["items"][0]["recorded_at"], None)
        self.assertEqual(self.profile.store.list_tasks(offset=500)[1], 501)
        self.assertEqual(self.profile.store.list_records(offset=100)[1], 101)
        self.assertEqual(self.profile.store.compatibility()["items"][0]["project_id"], "orphan")
        with closing(sqlite3.connect(Path(result["backup_path"]) / "management.sqlite3")) as backup:
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM profile_imports").fetchone()[0], 0)
        restored = importer.restore_backup(result["backup_path"], str(self.root / "restored"))
        self.assertTrue(Profile.open(str(restored)).store.is_empty())
        self.assertEqual(self.profile.store.list_tasks()[1], 501)
        with self.assertRaises(ProfileError):
            importer.restore_backup(result["backup_path"], str(restored))
        with self.assertRaises(ProfileError) as error:
            importer.import_legacy(self.profile, str(self.source))
        self.assertEqual(error.exception.reason, "already_imported")

    def test_bad_last_row_and_duplicate_unknown_fields_reject_entire_batch(self):
        variants = [
            [{"id": "good", "text": "valid", "project_id": "", "done": False}, {"id": "bad", "text": "valid", "project_id": "", "done": 0}],
            [{"id": "", "text": "valid", "project_id": "", "done": False}] * 2,
            [{"id": "good", "text": "valid", "project_id": "", "done": False, "extra": "x"}],
        ]
        for rows in variants:
            self.write("progress-todos.json", rows)
            with self.assertRaises(ProfileError):
                importer.import_legacy(self.profile, str(self.source))
            self.assertTrue(self.profile.store.is_empty())
        (self.source / "progress-todos.json").write_text('[{"id":"a","id":"b"}]', encoding="utf-8")
        with self.assertRaises(ProfileError):
            importer.import_legacy(self.profile, str(self.source))
        for name in ("progress-manual.json", "progress-todos.json"):
            self.write("progress-todos.json", [])
            self.write(name, None)
            with self.assertRaises(ProfileError):
                importer.import_legacy(self.profile, str(self.source))
            (self.source / name).unlink()

    def test_unknown_sql_schema_and_invalid_sql_values_reject(self):
        path = self.source_database(tasks=2, records=1)
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("UPDATE tasks SET title='' WHERE rowid=2")
            connection.commit()
        with self.assertRaises(ProfileError):
            importer.import_legacy(self.profile, str(self.source))
        self.assertTrue(self.profile.store.is_empty())
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("UPDATE tasks SET title='task'")
            connection.execute("ALTER TABLE tasks ADD COLUMN extra TEXT")
            connection.commit()
        with self.assertRaises(ProfileError):
            importer.import_legacy(self.profile, str(self.source))
        self.assertTrue(self.profile.store.is_empty())

    def test_budgets_overlap_missing_and_failure_rollback(self):
        with patch.dict(importer.ROW_LIMITS, {"projects": 0}):
            with self.assertRaises(ProfileError):
                importer.import_legacy(self.profile, str(self.source))
        for source in ("relative", str(self.root), str(self.profile.data_dir)):
            with self.assertRaises(ProfileError):
                importer.import_legacy(self.profile, source)
        with patch.object(importer, "create_backup", side_effect=ProfileError("backup_failed", status=500)):
            with self.assertRaises(ProfileError) as error:
                importer.import_legacy(self.profile, str(self.source))
        self.assertEqual(error.exception.reason, "backup_failed")
        self.assertTrue(self.profile.store.is_empty())
        with patch.object(importer, "insert_batch", side_effect=sqlite3.IntegrityError("fixture failure")):
            with self.assertRaises(ProfileError):
                importer.import_legacy(self.profile, str(self.source))
        self.assertTrue(self.profile.store.is_empty())
        result = importer.import_legacy(self.profile, str(self.source))
        self.assertEqual(len(result["missing_optional"]), 3)

    def test_busy_and_locked_deadline_callbacks_and_backup_connection_cleanup(self):
        for status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            with patch.object(importer.time, "monotonic", side_effect=[4, 5]):
                callback = importer.backup_progress(5)
                callback(status, 10, 10)
                with self.assertRaises(importer.BackupTimeout):
                    callback(status, 10, 10)
        actual_connect = sqlite3.connect
        connections, sources = [], []
        class ConnectionProxy:
            def __init__(self, value, reader):
                self.value, self.reader, self.closed = value, reader, False
            def execute(self, *args):
                return self.value.execute(*args)
            def backup(self, destination, **kwargs):
                sources.append(self)
                self.assert_reader()
                with patch.object(importer.time, "monotonic", return_value=100):
                    kwargs["progress"](sqlite3.SQLITE_BUSY, 1, 1)
            def assert_reader(self):
                if not self.reader or self.value.in_transaction:
                    raise AssertionError("backup source must be independent readonly reader")
            def close(self):
                self.closed = True
                self.value.close()
        def connect(*args, **kwargs):
            value = actual_connect(*args, **kwargs)
            # Only create_backup's R/B are wrapped; W remains an ordinary connection.
            if kwargs.get("timeout") == 0:
                value = ConnectionProxy(value, "mode=ro" in str(args[0]))
                connections.append(value)
            return value
        with patch.object(importer.sqlite3, "connect", side_effect=connect), patch.object(importer.time, "monotonic", return_value=0):
            with self.assertRaises(ProfileError) as error:
                importer.import_legacy(self.profile, str(self.source))
        self.assertEqual((error.exception.reason, error.exception.message), ("backup_failed", "backup_timeout"))
        self.assertEqual(len(sources), 1)
        self.assertTrue(all(connection.closed for connection in connections))
        self.assertTrue(self.profile.store.is_empty())
        self.assertTrue(self.profile.store.write_lock.acquire(blocking=False))
        self.profile.store.write_lock.release()
        self.assertTrue(self.profile.config_lock.acquire(blocking=False))
        self.profile.config_lock.release()

    def test_manifest_tampering_refuses_restore(self):
        result = importer.import_legacy(self.profile, str(self.source))
        backup = Path(result["backup_path"])
        (backup / "profile.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ProfileError):
            importer.restore_backup(str(backup), str(self.root / "restore"))
        self.assertFalse((self.root / "restore").exists())

    def test_source_change_during_backup_rolls_back_and_begin_busy_never_backs_up(self):
        original_backup = importer.create_backup
        def changed_source(profile):
            backup = original_backup(profile)
            self.write("progress-registry.json", {"projects": []})
            return backup
        with patch.object(importer, "create_backup", side_effect=changed_source):
            with self.assertRaises(ProfileError):
                importer.import_legacy(self.profile, str(self.source))
        self.assertTrue(self.profile.store.is_empty())
        # A real competing writer keeps the final empty check inside SQLite's lock.
        writer = sqlite3.connect(self.profile.store.path, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        try:
            with patch.object(importer, "create_backup") as backup:
                with self.assertRaises(ProfileError) as error:
                    importer.import_legacy(self.profile, str(self.source))
            self.assertEqual(error.exception.reason, "store_busy")
            backup.assert_not_called()
        finally:
            writer.execute("ROLLBACK")
            writer.close()
