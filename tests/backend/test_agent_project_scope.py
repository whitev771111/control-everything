"""Assignment scope contracts over disposable profiles and project directories."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_everything.profiles.profile import Profile, ProfileError
from control_everything.workspace.service import WorkspaceService
from tests.backend.test_agents import agent
from tests.backend.test_project_management import project_body


class AgentProjectScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = Profile.open(str(self.root / "profile"))
        projects = []
        for name in ("first", "second"):
            directory = self.root / name
            directory.mkdir()
            projects.append(self.profile.store.save_project(project_body(directory)))
        self.first, self.second = projects
        registry = self.profile.save_agents({"expected_revision": 0, "items": [
            agent(name="Scoped", project_ids=[self.first["id"]]),
            agent(name="Unrestricted"),
        ]})
        self.scoped, self.unrestricted = registry["items"]
        self.service = WorkspaceService(profile=self.profile)

    def create(self, project_id, assigned_agent_id):
        return self.service.save_item({"title": "Synthetic task", "project_id": project_id,
                                       "assigned_agent_id": assigned_agent_id})["item"]

    def update(self, item, **changes):
        return self.service.save_item({"id": item["id"], "expected_revision": item["revision"],
                                       "changes": changes}, update=True)["item"]

    def assert_scope_rejected(self, operation):
        with self.assertRaises(ProfileError) as caught:
            operation()
        self.assertEqual((caught.exception.reason, caught.exception.field),
                         ("invalid_item", "assigned_agent_id"))

    def test_new_assignment_rejects_foreign_project_and_global_scope(self):
        for project_id in (self.second["id"], None):
            with self.subTest(project_id=project_id):
                self.assert_scope_rejected(lambda: self.create(project_id, self.scoped["id"]))
        self.assertEqual(self.profile.store.list_items()["total"], 0)

    def test_allowed_and_unrestricted_assignments_remain_available(self):
        self.assertEqual(self.create(self.first["id"], self.scoped["id"])["assigned_agent_id"],
                         self.scoped["id"])
        for project_id in (self.second["id"], None):
            with self.subTest(project_id=project_id):
                self.assertEqual(self.create(project_id, self.unrestricted["id"])["assigned_agent_id"],
                                 self.unrestricted["id"])

    def test_assigning_an_existing_foreign_task_is_rejected_without_write(self):
        item = self.create(self.second["id"], None)
        self.assert_scope_rejected(lambda: self.update(item, assigned_agent_id=self.scoped["id"]))
        self.assertEqual(self.profile.store.get_item(item["id"])["revision"], item["revision"])

    def test_project_move_rechecks_unchanged_assignment(self):
        item = self.create(self.first["id"], self.scoped["id"])
        self.assert_scope_rejected(lambda: self.update(item, project_id=self.second["id"]))
        self.assert_scope_rejected(lambda: self.update(item, project_id=None))
        self.assertEqual(self.profile.store.get_item(item["id"])["project_id"], self.first["id"])

    def test_project_move_can_explicitly_clear_or_replace_assignment(self):
        item = self.create(self.first["id"], self.scoped["id"])
        moved = self.update(item, project_id=self.second["id"], assigned_agent_id=None)
        self.assertIsNone(moved["assigned_agent_id"])
        item = self.create(self.first["id"], self.scoped["id"])
        moved = self.update(item, project_id=self.second["id"],
                            assigned_agent_id=self.unrestricted["id"])
        self.assertEqual(moved["assigned_agent_id"], self.unrestricted["id"])

    def legacy_update(self, item, **changes):
        body = {key: item[key] for key in
                ("title", "project_id", "provider_id", "state", "session_key")}
        return self.service.save_task({**body, "id": item["origin_id"], **changes}, update=True)

    def test_legacy_project_move_rejects_retained_out_of_scope_assignment_without_write(self):
        item = self.create(self.first["id"], self.scoped["id"])
        with self.profile.store.connection() as connection:
            before = list(connection.iterdump())
        self.assert_scope_rejected(lambda: self.legacy_update(item, project_id=self.second["id"]))
        with self.profile.store.connection() as connection:
            self.assertEqual(list(connection.iterdump()), before)

    def test_legacy_project_move_allows_unrestricted_and_unassigned_tasks(self):
        for agent_id in (None, self.unrestricted["id"]):
            with self.subTest(agent_id=agent_id):
                item = self.create(self.first["id"], agent_id)
                moved = self.legacy_update(item, project_id=self.second["id"])["task"]
                self.assertEqual(moved["project_id"], self.second["id"])
                self.assertEqual(self.profile.store.get_item(item["id"])["assigned_agent_id"], agent_id)

    def test_historical_out_of_scope_assignment_allows_unrelated_edits_and_unassignment(self):
        item = self.profile.store.save_item({"title": "Historical task", "project_id": self.second["id"],
            "provider_id": None, "session_key": None, "state": "todo",
            "assigned_agent_id": self.scoped["id"]})
        edited = self.update(item, title="Updated historical task")
        self.assertEqual(edited["assigned_agent_id"], self.scoped["id"])
        self.legacy_update(edited, title="Legacy historical title")
        edited = self.profile.store.get_item(item["id"])
        unchanged = self.update(edited, assigned_agent_id=self.scoped["id"])
        self.assertEqual(unchanged["revision"], edited["revision"])
        self.assertIsNone(self.update(unchanged, assigned_agent_id=None)["assigned_agent_id"])


if __name__ == "__main__":
    unittest.main()
