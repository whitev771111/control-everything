"""Management behavior over disposable SQLite and explicit metadata fixtures."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from control_everything.profiles.profile import Profile, ProfileError
from control_everything.workspace.service import WorkspaceService, observation_owner


def project_body(root, **values):
    return {"id": None, "name": "Sample", "archived": False, "directories": [{"id": "main", "path": str(root)}],
            "source": {"kind": "none", "directory_id": None, "relative_path": None}, **values}


class ProjectManagementTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = Profile.open(str(self.root / "profile"))
        self.store = self.profile.store
        self.workspace = WorkspaceService(profile=self.profile, quota_source=Mock(), news_source=Mock())

    def create(self, **values):
        return self.store.save_project(project_body(self.root / "project", **values))

    def task(self, identity, **values):
        return {"title": "Task", "project_id": identity, "provider_id": None, "state": "todo", "session_key": None, **values}

    def test_stable_id_multiple_directories_rebind_archive_and_conflicts(self):
        item = self.create()
        extra_id = "d-" + uuid4().hex
        body = project_body(self.root / "moved", id=item["id"], directories=[{"id": "main", "path": str(self.root / "moved")}, {"id": extra_id, "path": str(self.root / "worktree")}], source={"kind": "text", "directory_id": extra_id, "relative_path": "progress.md"})
        updated = self.store.save_project(body)
        self.assertEqual((updated["id"], updated["created_at"]), (item["id"], item["created_at"]))
        with self.assertRaises(ProfileError) as error:
            self.store.save_project(project_body(self.root / "worktree"))
        self.assertEqual(error.exception.reason, "directory_conflict")
        with self.assertRaises(ProfileError) as error:
            self.store.save_project({**body, "directories": body["directories"][:1]})
        self.assertEqual(error.exception.reason, "source_binding_missing")
        archived = self.store.save_project({**body, "archived": True})
        self.assertTrue(archived["archived"])
        self.store.save_project(project_body(self.root / "worktree"))
        with self.assertRaises(ProfileError):
            self.store.save_project(body)
        many = [{"id": "main" if index == 0 else "d-" + uuid4().hex, "path": str(self.root / str(index))} for index in range(8)]
        self.assertEqual(len(self.store.save_project(project_body(self.root, directories=many))["directories"]), 8)
        with self.assertRaises(ProfileError):
            self.store.save_project(project_body(self.root, directories=many + [{"id": "d-" + uuid4().hex, "path": str(self.root / "nine")}]))

    def test_unknown_zero_clear_and_history_only_actual_changes(self):
        item = self.create()
        self.assertIsNone(self.workspace.profile_projects()[0]["percent"])
        body = {"project_id": item["id"], "state": "在做", "percent": None, "note": "note"}
        self.assertTrue(self.store.save_manual(body)["history_added"])
        self.assertFalse(self.store.save_manual(body)["history_added"])
        self.assertTrue(self.workspace.profile_projects()[0]["manual_recorded"])
        self.store.save_manual({**body, "percent": 0})
        self.assertEqual(self.workspace.profile_projects()[0]["percent"], 0)
        self.store.save_manual(body)
        self.assertEqual(self.store.manual_history(item["id"])["total"], 3)
        for value in (True, float("nan"), 101, -1):
            with self.assertRaises(ProfileError):
                self.store.save_manual({**body, "percent": value})

    def test_null_session_skips_reader_and_archived_orphan_edits_preserve_id(self):
        item = self.create()
        with patch.object(self.workspace, "sessions", side_effect=AssertionError("NULL must skip sample")):
            task = self.workspace.save_task(self.task(item["id"]))["task"]
        self.store.save_project(project_body(self.root / "project", id=item["id"], archived=True))
        changed = self.workspace.save_task({**self.task(item["id"], state="done"), "id": task["id"]}, update=True)["task"]
        self.assertEqual(changed["state"], "done")
        with self.assertRaises(ValueError):
            self.workspace.save_task(self.task(item["id"]))
        self.store.save_task(self.task('orphan', provider_id='codex', session_key='codex:' + str(uuid4())), task['id'])
        with patch.object(self.workspace, "sessions", side_effect=AssertionError("NULL must skip sample")):
            edited = self.workspace.save_task({**self.task("orphan"), "id": task["id"]}, update=True)["task"]
        self.assertEqual(edited["project_id"], "orphan")
        self.assertEqual(self.store.compatibility()["items"][0]["project_id"], "orphan")
        restored = self.store.save_project({"project_id": "orphan", "name": "Restored", "directories": [{"id": "main", "path": str(self.root / "restored")}], "source": {"kind": "none", "directory_id": None, "relative_path": None}}, restore=True)
        self.assertEqual(restored["id"], "orphan")
        self.assertEqual(self.store.compatibility()["total"], 0)

    def test_session_owner_longest_and_changed_association_needs_current_sample(self):
        first = self.create()
        second = self.store.save_project(project_body(self.root / "project" / "nested"))
        registry = self.store.list_projects()
        self.assertEqual(observation_owner(str(self.root / "project" / "nested" / "file"), registry), second["id"])
        self.assertEqual(observation_owner(str(self.root / "project" / "file"), registry), first["id"])
        duplicate = {**registry[1], "id": "duplicate"}
        self.assertIsNone(observation_owner(str(self.root / "project" / "nested"), registry + [duplicate]))
        key = "codex:" + str(uuid4())
        sample = {"available": True, "items": [{"session_key": key, "cwd": str(self.root / "project" / "nested")}]}
        with patch.object(self.workspace, "sessions", return_value=sample):
            from control_everything.workspace.store import WorkspaceError
            with self.assertRaises(WorkspaceError):
                self.workspace.save_task(self.task(first["id"], provider_id="codex", session_key=key))
            task = self.workspace.save_task(self.task(second["id"], provider_id="codex", session_key=key))["task"]
        with patch.object(self.workspace, "sessions", return_value={"available": False, "items": []}):
            kept = self.workspace.save_task({**self.task(second["id"], provider_id="codex", session_key=key), "id": task["id"]}, update=True)
        self.assertEqual(kept["task"]["session_link_state"], "not_in_current_sample")

    def test_all_task_statistics_and_pagination_exceed_display_limit(self):
        item = self.create()
        stamp = "2026-01-01T00:00:00Z"
        with self.store.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            for index in range(501):
                self.store._create_item(connection, self.task(item['id'], state='done' if index == 500 else 'todo'),
                                        created_at=stamp, updated_at=stamp)
        self.assertEqual(self.store.task_counts()[item["id"]]["total"], 501)
        self.assertEqual(self.store.task_counts()[item["id"]]["done"], 1)
        page = self.workspace.page_tasks(offset=500, limit=100)
        self.assertEqual((page["total"], len(page["items"])), (501, 1))
        self.assertEqual(self.workspace.snapshot()["tasks"]["limit"], 50)

    def test_orphan_manual_can_edit_and_source_observation_never_changes_management(self):
        with self.store.connection() as connection:
            connection.execute("INSERT INTO project_manual VALUES('orphan','等我',NULL,'imported',NULL)")
        result = self.store.save_manual({"project_id": "orphan", "state": "在做", "percent": None, "note": "edited"})
        self.assertTrue(result["history_added"])
        self.assertEqual(self.store.compatibility(kind="manual")["items"][0]["note"], "edited")
        with self.assertRaises(ProfileError):
            self.store.save_manual({"project_id": "unknown", "state": "在做", "percent": None, "note": "x"})
        project_root = self.root / "observed"
        project_root.mkdir()
        (project_root / "progress.json").write_text('{"version":1,"state":"completed","percent":100}', encoding="utf-8")
        item = self.store.save_project(project_body(project_root, source={"kind": "json", "directory_id": "main", "relative_path": "progress.json"}))
        projected = self.workspace.profile_projects()[0]
        self.assertEqual(projected["observation"]["reported"]["percent"], 100)
        self.assertFalse(projected["manual_recorded"])
        self.assertIsNone(projected["percent"])
        self.assertEqual(self.store.manual_history(item["id"])["total"], 0)
        self.assertEqual(projected["task_counts"]["total"], 0)
