import hashlib
import json
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_everything.sessions import codex_desktop as desktop


def identity(index):
    return f"a0000000-0000-4000-8000-{index:012d}"


class CodexDesktopTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite"
        self.config_path = self.path.parent / ".codex-global-state.json"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE threads(id,cwd,created_at,updated_at,archived,source,title,name,project_id,unread)")
            db.execute("CREATE TABLE projects(id,name,position,metadata)")
            db.execute("CREATE TABLE project_roots(project_id,position,path)")
        self.old = identity(100)
        self.new = identity(200)
        self.other_old = identity(101)
        self.other_new = identity(201)
        self.host = "local:" + str(self.path.parent)

    def tearDown(self):
        self.directory.cleanup()

    def project(self, index, *, position=0, roots=("C:/example/project",)):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO projects VALUES(?,?,?,?)", (index, "Project " + index[-3:], position, "never-read"))
            db.executemany("INSERT INTO project_roots VALUES(?,?,?)", [(index, n, root) for n, root in enumerate(roots)])

    def thread(self, index, *, project=None, source="cli", archived=0, cwd="C:/example/project", title="Title", name=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?)", (identity(index), cwd, 1700000000,
                       1700000000 + index, archived, source, title, name, project, "never-read"))

    def config(self, *, completed=False, projects=None, assignments=None, projectless=None, mapping=None):
        value = {
            "local-projects": projects if projects is not None else {
                self.old: {"id": self.old, "name": "Legacy project", "rootPaths": ["C:/example/project"]}},
            "thread-project-assignments": assignments or {}, "projectless-thread-ids": projectless or [],
            "project-order": [self.old],
            "app-server-project-id-by-legacy-project-id-by-host": {self.host: mapping if mapping is not None else {self.old: self.new}},
            "app-server-projects-migration-by-host": {self.host: {"version": 1, "projectsMigrated": True,
                "threadAssignmentsMigrated": completed, "pendingThreadAssignmentIds": []}},
            "unrelated-private-value": "never-return"}
        self.write_config(value)
        return value

    def write_config(self, value):
        self.config_path.write_text(json.dumps(value), encoding="utf-8")

    def read(self):
        return desktop.read_desktop(self.path)

    def test_disabled_does_not_discover_or_read(self):
        with patch.object(Path, "resolve") as resolve, patch.object(desktop.sqlite3, "connect") as connect:
            self.assertEqual(desktop.read_desktop()["reason"], "source_disabled")
        resolve.assert_not_called()
        connect.assert_not_called()

    def test_migration_merges_all_threads_before_pagination_and_preserves_worktree(self):
        self.project(self.new)
        assignments = {}
        for index in range(25):
            self.thread(index, project=self.new if index == 0 else None, cwd="C:/example/worktree")
            assignments[identity(index)] = {"projectKind": "local", "projectId": self.old}
        self.config(assignments=assignments)
        result = self.read()
        self.assertTrue(result["available"])
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["items"]), 25)
        self.assertEqual(result["coverage"]["assigned_count"], 25)
        self.assertEqual([item["thread_id"] for item in result["items"]], [identity(n) for n in reversed(range(25))])
        self.assertTrue(all(item["project_id"] == result["projects"][0]["id"] and item["binding_available"] for item in result["items"]))
        expected_source = hashlib.sha256(os.path.normcase(str(self.path.resolve())).encode()).hexdigest()
        self.assertEqual(result["source_id"], expected_source)
        self.assertEqual(result["projects"][0]["id"], "cx-" + hashlib.sha256((expected_source + "\0" + self.new).encode()).hexdigest()[:24])

    def test_same_directory_is_not_project_identity_and_order_switches_after_migration(self):
        self.project(self.new, position=0, roots=("C:/example/shared", "C:/example/worktree"))
        self.project(self.other_new, position=1, roots=("C:/example/shared",))
        self.thread(1)
        self.thread(2)
        value = self.config(projects={old: {"id": old, "name": "Shared", "rootPaths": ["C:/example/shared"]}
                                      for old in (self.old, self.other_old)},
                            assignments={identity(n): {"projectKind": "local", "projectId": old}
                                         for n, old in ((1, self.old), (2, self.other_old))},
                            mapping={self.old: self.new, self.other_old: self.other_new})
        value["project-order"] = [self.other_old, self.old]
        self.write_config(value)
        result = self.read()
        self.assertEqual([p["codex_project_id"] for p in result["projects"]], [self.other_new, self.new])
        self.assertEqual(len({item["project_id"] for item in result["items"]}), 2)
        self.assertEqual(result["projects"][1]["directories"][1]["role"], "extra")
        value["app-server-projects-migration-by-host"][self.host]["threadAssignmentsMigrated"] = True
        self.write_config(value)
        self.assertEqual([p["codex_project_id"] for p in self.read()["projects"]], [self.new, self.other_new])

    def test_explicit_projectless_unknown_and_conflicts_never_bind(self):
        self.project(self.new)
        self.project(self.other_new)
        self.thread(1)
        self.thread(2)
        self.thread(3, project=self.other_new)
        self.thread(4, project=self.new)
        value = self.config(assignments={identity(3): {"projectKind": "local", "projectId": self.old}},
                            projectless=[identity(1), identity(4)])
        result = self.read()
        states = {item["thread_id"]: item for item in result["items"]}
        self.assertEqual(states[identity(1)]["assignment_state"], "projectless")
        self.assertEqual(states[identity(2)]["assignment_state"], "unknown")
        self.assertEqual(states[identity(3)]["assignment_state"], "conflict")
        self.assertEqual(states[identity(3)]["project_id"], result["projects"][0]["id"])
        self.assertIsNone(states[identity(4)]["project_id"])
        self.assertFalse(any(item["binding_available"] for item in states.values()))
        value["app-server-projects-migration-by-host"][self.host]["threadAssignmentsMigrated"] = True
        self.write_config(value)
        completed = {item["thread_id"]: item for item in self.read()["items"]}
        self.assertEqual(completed[identity(3)]["project_id"], result["projects"][1]["id"])
        self.assertEqual(completed[identity(4)]["assignment_state"], "conflict")

    def test_completed_migration_uses_db_and_flags_null_db_disagreement(self):
        self.project(self.new)
        self.thread(1, project=self.new)
        self.thread(2)
        self.thread(3)
        self.config(completed=True, assignments={identity(3): {"projectKind": "local", "projectId": self.old}})
        items = {item["thread_id"]: item for item in self.read()["items"]}
        self.assertEqual([items[identity(n)]["assignment_state"] for n in (1, 2, 3)], ["assigned", "projectless", "conflict"])

    def test_config_absent_uses_db_and_title_is_literal_without_private_fields(self):
        self.project(self.new)
        self.thread(1, project=self.new, title='<script>literal & "title"</script>')
        self.thread(2, title=None, name="Fallback")
        self.thread(3, archived=1)
        self.thread(4, source='{"subagent":{"id":"synthetic"}}')
        self.thread(5, source="other")
        result = self.read()
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(result["items"][2]["title"], '<script>literal & "title"</script>')
        self.assertEqual(result["items"][1]["title"], "Fallback")
        self.assertTrue(result["items"][0]["archived"])
        self.assertEqual(result["coverage"]["excluded_subagent_count"], 1)
        self.assertEqual(result["coverage"]["excluded_other_source_count"], 1)
        self.assertNotIn("never-read", json.dumps(result))

    def test_legacy_only_schema_is_unavailable_without_false_projectless(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE projects")
            db.execute("DROP TABLE project_roots")
            db.execute("ALTER TABLE threads DROP COLUMN project_id")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?)", (identity(1), "C:/example/project", 1, 2, 0, "cli", "Title", None, "private"))
        self.write_config({"local-projects": {self.old: {"id": self.old, "name": "Legacy", "rootPaths": ["C:/example/project"]}},
                           "thread-project-assignments": {identity(1): {"projectKind": "local", "projectId": self.old}}})
        result = self.read()
        self.assertEqual(result["reason"], "schema_unavailable")
        self.assertFalse(result["available"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["projects"], [])
        self.assertEqual(result["items"], [])

    def test_each_missing_project_table_disables_desktop_with_existing_thread_project_column(self):
        self.thread(1)
        for table in ("projects", "project_roots"):
            with self.subTest(table=table):
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute("ALTER TABLE " + table + " RENAME TO saved_" + table)
                result = self.read()
                self.assertEqual(result["reason"], "schema_unavailable")
                self.assertFalse(result["available"])
                self.assertFalse(result["complete"])
                self.assertEqual(result["items"], [])
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute("ALTER TABLE saved_" + table + " RENAME TO " + table)

    def test_missing_mapping_does_not_duplicate_projects_or_use_other_hosts(self):
        self.project(self.new)
        self.thread(1)
        value = self.config(assignments={identity(1): {"projectKind": "local", "projectId": self.old}})
        value["app-server-project-id-by-legacy-project-id-by-host"] = {"local:C:/example/other-home": {self.old: self.new}}
        self.write_config(value)
        result = self.read()
        self.assertEqual(len(result["projects"]), 1)
        self.assertEqual(result["reason"], "mapping_incomplete")
        self.assertEqual(result["items"][0]["assignment_state"], "conflict")

    def test_unknown_version_invalid_mapping_and_duplicate_json_fail_closed(self):
        self.project(self.new)
        value = self.config()
        value["app-server-projects-migration-by-host"][self.host]["version"] = 2
        self.write_config(value)
        self.assertEqual(self.read()["reason"], "unsupported_migration")
        value["app-server-projects-migration-by-host"][self.host]["version"] = 1
        value["app-server-project-id-by-legacy-project-id-by-host"][self.host][self.old] = identity(999)
        self.write_config(value)
        self.assertEqual(self.read()["reason"], "invalid_mapping")
        self.config_path.write_text('{"local-projects":{},"local-projects":{}}', encoding="utf-8")
        self.assertEqual(self.read()["reason"], "invalid_config")
        self.config_path.write_bytes(b"\xff")
        self.assertFalse(self.read()["available"])

    def test_missing_file_missing_fields_and_invalid_rows_are_unavailable(self):
        self.assertFalse(desktop.read_desktop(self.path.with_name("missing.sqlite"))["available"])
        self.thread(1, title=42)
        self.assertEqual(self.read()["reason"], "invalid_metadata")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE threads")
            db.execute("CREATE TABLE threads(id)")
        self.assertEqual(self.read()["reason"], "schema_unavailable")

    def test_rename_move_and_repeated_read_keep_external_identity(self):
        self.project(self.new)
        self.thread(1, project=self.new)
        before = self.read()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE projects SET name='Renamed'")
            db.execute("UPDATE project_roots SET path='C:/example/moved'")
        after = self.read()
        self.assertEqual(before["projects"][0]["id"], after["projects"][0]["id"])
        self.assertEqual(after["projects"][0]["name"], "Renamed")
        self.assertEqual(after["projects"][0]["directories"][0]["path"], "C:/example/moved")
        self.assertEqual(after["projects"], self.read()["projects"])

    def test_long_title_is_bounded_and_truncation_is_explicit(self):
        self.thread(1, title="x" * 5000)
        self.thread(2, title="y" * 5000, name="")
        result = self.read()
        self.assertTrue(result["available"])
        self.assertTrue(result["complete"])
        self.assertTrue(all(item["title_truncated"] and len(item["title"]) == desktop.MAX_TITLE for item in result["items"]))
        self.assertEqual(result["items"][0]["title"], "y" * desktop.MAX_TITLE)

    def test_explicit_name_overrides_title_including_unused_long_title(self):
        self.thread(1, title="Original prompt title", name="Renamed by user")
        self.thread(2, title="x" * 5000, name="Short name")
        result = self.read()
        self.assertTrue(result["available"])
        self.assertEqual([item["title"] for item in result["items"]], ["Short name", "Renamed by user"])
        self.assertTrue(all(not item["title_truncated"] for item in result["items"]))

    def test_nontext_display_field_still_fails_closed(self):
        self.thread(1)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE threads SET title=?", (sqlite3.Binary(b"private"),))
        self.assertEqual(self.read()["reason"], "invalid_metadata")

    def test_overlap_and_nonlocal_assignment_are_explicit_conflicts(self):
        self.project(self.new)
        self.thread(1)
        self.thread(2)
        self.config(assignments={identity(1): {"projectKind": "local", "projectId": self.old},
                                 identity(2): {"projectKind": "cloud", "projectId": "cloud-project"}},
                    projectless=[identity(1)])
        result = self.read()
        self.assertTrue(all(item["assignment_state"] == "conflict" and not item["binding_available"] for item in result["items"]))

    def test_row_budget_is_partial_and_disables_all_bindings(self):
        self.project(self.new)
        for index in range(4):
            self.thread(index, project=self.new)
        with patch.object(desktop, "MAX_THREADS", 3):
            result = self.read()
        self.assertTrue(result["available"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "row_budget_exceeded")
        self.assertEqual(result["coverage"]["ordinary_count"], 4)
        self.assertFalse(any(item["binding_available"] for item in result["items"] + result["projects"]))

    def test_file_changes_retry_once_and_query_budget_failure_is_bounded(self):
        native = desktop._signature
        signatures = 0
        def unstable(path):
            nonlocal signatures
            signatures += 1
            return (native(path), signatures)
        with patch.object(desktop, "_signature", side_effect=unstable), patch.object(desktop, "_database", wraps=desktop._database) as query:
            result = self.read()
        self.assertEqual(query.call_count, 2)
        self.assertEqual(result["reason"], "source_changed")
        for index in range(30):
            self.thread(index)
        with patch.object(desktop, "QUERY_STEPS", 0):
            self.assertEqual(self.read()["reason"], "query_budget_exceeded")
        with patch.object(desktop, "MAX_CONFIG_BYTES", 2):
            self.config_path.write_text("{\"x\":1}", encoding="utf-8")
            self.assertEqual(self.read()["reason"], "config_budget_exceeded")

    def test_source_files_read_only_and_column_whitelist(self):
        self.project(self.new)
        self.thread(1, project=self.new)
        self.config()
        before = (self.path.read_bytes(), self.config_path.read_bytes())
        native_connect = sqlite3.connect
        opened = []
        class Observed(sqlite3.Connection):
            closed = False
            def execute(inner, sql, *args):
                result = super(Observed, inner).execute(sql, *args)
                if sql == "PRAGMA query_only=ON":
                    self.assertEqual(inner.execute("PRAGMA query_only").fetchone()[0], 1)
                if "SELECT COUNT(*)" in sql:
                    with self.assertRaises(sqlite3.OperationalError):
                        inner.execute("INSERT INTO projects(id) VALUES('blocked')")
                return result
            def close(inner):
                inner.closed = True
                super(Observed, inner).close()
        def connect(database_uri, **kwargs):
            self.assertTrue(database_uri.endswith("?mode=ro"))
            connection = native_connect(database_uri, factory=Observed, **kwargs)
            connection.set_authorizer(lambda action, _table, column, *_: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and column in ("unread", "metadata") else sqlite3.SQLITE_OK)
            opened.append(connection)
            return connection
        with patch.object(desktop.sqlite3, "connect", side_effect=connect):
            result = self.read()
        self.assertTrue(result["available"], result)
        self.assertTrue(all(connection.closed for connection in opened))
        self.assertEqual((self.path.read_bytes(), self.config_path.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
