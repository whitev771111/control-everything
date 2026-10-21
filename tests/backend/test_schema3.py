"""Synthetic schema3 contracts; no user profile or external service is used."""

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from control_everything.profiles.profile import DEFAULT_CONFIG, Profile, ProfileError
from control_everything.profiles import profile_import, task_migration
from control_everything.workspace import task_schema
from control_everything.workspace.store import WorkspaceError


def project_body(root):
    (root / "work").mkdir(exist_ok=True)
    return {
        "id": None,
        "name": "Synthetic",
        "archived": False,
        "directories": [{"id": "main", "path": str(root / "work")}],
        "source": {"kind": "none", "directory_id": None, "relative_path": None},
    }


class Schema3Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_fresh_profile_has_schema3_objects(self):
        profile = Profile.open(str(self.root / "fresh"))
        with closing(sqlite3.connect(profile.store.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], task_schema.SCHEMA_VERSION)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(task_items)")}
            self.assertLessEqual({"priority", "focus", "assigned_agent_id"}, columns)
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertLessEqual({"task_reviews", "project_decisions", "agent_runs", "task_events",
                                  "task_briefs", "task_dispatches", "task_sessions"}, tables)

    def test_schema2_profile_requires_explicit_migration_and_keeps_rows(self):
        old = self.root / "old"
        old.mkdir()
        (old / "profile.json").write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8")
        with closing(sqlite3.connect(old / "workspace-state.sqlite3")) as connection:
            task_schema.install_schema2(connection, fresh=True)
            connection.execute("INSERT INTO projects VALUES('p1','Old',0,NULL,NULL,0)")
            connection.execute("INSERT INTO project_directories VALUES('p1','main','C:/example/example','c:/example/example',1)")
            connection.execute("INSERT INTO project_sources VALUES('p1','none',NULL,NULL)")
            connection.execute("""INSERT INTO task_items(id,origin,origin_id,title,project_id,provider_id,
                state,session_key,source,created_at,updated_at,todo_position,sort_index,resume_state,revision)
                VALUES('i1','task','t1','Old task','p1',NULL,'todo',NULL,'manual',NULL,NULL,NULL,0,'todo',1)""")
            connection.commit()
        with self.assertRaises(ProfileError) as error:
            Profile.open(str(old))
        self.assertEqual(error.exception.reason, "migration_required")
        result = task_migration.migrate_tasks(str(old))
        self.assertEqual((result["status"], result["schema_version"]), ("ok", task_schema.SCHEMA_VERSION))
        self.assertIsNone(result["receipt"])
        profile = Profile.open(str(old))
        row = profile.store.list_items()["items"][0]
        self.assertEqual((row["title"], row["priority"], row["focus"]), ("Old task", None, 0))
        self.assertEqual(profile_import.validate_backup(result["backup"])["version"], 2)

    def test_priority_and_focus_roundtrip_and_bounds(self):
        profile = Profile.open(str(self.root / "item"))
        project = profile.store.save_project(project_body(self.root / "item"))
        item = profile.store.save_item({"title": "Task", "project_id": project["id"], "provider_id": None,
                                        "state": "todo", "session_key": None, "priority": 0, "focus": 1})
        self.assertEqual((item["priority"], item["focus"]), (0, 1))
        changed = profile.store.save_item({"priority": 2, "focus": 0}, item_id=item["id"],
                                          expected_revision=item["revision"])
        self.assertEqual((changed["priority"], changed["focus"]), (2, 0))
        cleared = profile.store.save_item({"priority": None}, item_id=item["id"],
                                          expected_revision=changed["revision"])
        self.assertIsNone(cleared["priority"])
        for invalid in ({"priority": 3}, {"priority": -1}, {"priority": "1"}, {"focus": 2}, {"focus": True}):
            with self.subTest(invalid=invalid), self.assertRaises(WorkspaceError):
                profile.store.save_item(invalid, item_id=item["id"], expected_revision=cleared["revision"])

    def test_backup_counts_and_restore_include_schema3_tables(self):
        profile = Profile.open(str(self.root / "backup"))
        project = profile.store.save_project(project_body(self.root / "backup"))
        item = profile.store.save_item({"title": "Task", "project_id": project["id"], "provider_id": None,
                                        "state": "review", "session_key": None})
        with profile.store.connection() as connection:
            connection.execute("INSERT INTO task_reviews VALUES('r1',?,'run-1','approved','ok',NULL,'manual','now')",
                               (item["id"],))
            connection.execute("""INSERT INTO project_decisions VALUES('d1',?,'Choose SQLite','Keep local',
                'Simple','[]','accepted','[]','now','now')""", (project["id"],))
        backup = profile_import.create_backup(profile)
        manifest = profile_import.validate_backup(backup)
        self.assertEqual(manifest["schema_version"], task_schema.SCHEMA_VERSION)
        self.assertLessEqual({"task_reviews", "project_decisions", "agent_runs", "task_events",
                              "task_briefs", "task_dispatches", "task_sessions"}, set(manifest["counts"]))
        restored = profile_import.restore_backup(str(backup), str(self.root / "restored"))
        reopened = Profile.open(str(restored))
        with closing(sqlite3.connect(reopened.store.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM task_reviews").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_decisions").fetchone()[0], 1)
