"""Task session metadata links over disposable profiles; no chat content."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_everything.input_validation import ProfileError
from control_everything.profiles.profile import Profile
from control_everything.workspace import task_sessions
from tests.backend.test_profile_http import ProfileHTTPTests
from tests.backend.test_project_management import project_body


class SessionLinkTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = Profile.open(str(self.root / "profile"))
        (self.root / "work").mkdir()
        self.project = self.profile.store.save_project(project_body(self.root / "work"))
        self.item = self.profile.store.save_item({
            "title": "Session task", "project_id": self.project["id"], "provider_id": None,
            "state": "doing", "session_key": None})
        with self.profile.store.connection() as connection:
            connection.execute("UPDATE task_items SET session_key=? WHERE id=?",
                               ("codex:old-session", self.item["id"]))

    def test_legacy_two_sources_reload_and_unlink(self):
        data = task_sessions.task_sessions(self.profile.store, self.item["id"])
        self.assertEqual(data["legacy_session_key"], "codex:old-session")
        codex = task_sessions.link_session(self.profile.store, self.item["id"], {
            "source": "codex", "external_id": "thread-1", "title": "Codex thread", "agent_id": "agent-a"})
        dsh = task_sessions.link_session(self.profile.store, self.item["id"], {
            "source": "dsh", "external_id": "thread-1", "title": "DSH thread"})
        self.assertNotEqual(codex["id"], dsh["id"])
        listed = task_sessions.task_sessions(self.profile.store, self.item["id"])
        linked = {(row["source"], row["external_id"]) for row in listed["items"] if not row.get("legacy")}
        self.assertEqual(linked, {("codex", "thread-1"), ("dsh", "thread-1")})
        self.assertTrue(any(row.get("legacy") and row["external_id"] == "codex:old-session"
                            for row in listed["items"]))
        unlinked = task_sessions.unlink_session(self.profile.store, dsh["id"], {
            "expected_task_id": self.item["id"], "expected_updated_at": dsh["updated_at"]})
        self.assertIsNone(unlinked["task_id"])
        project = task_sessions.list_sessions(self.profile.store, self.project["id"])
        self.assertEqual([row["id"] for row in project["items"] if not row.get("legacy")], [codex["id"]])
        self.assertTrue(any(row.get("legacy") for row in project["items"]))

    def test_source_instance_separates_same_external_id(self):
        default = task_sessions.link_session(self.profile.store, self.item["id"], {
            "source": "codex", "external_id": "shared-thread"})
        other = task_sessions.link_session(self.profile.store, self.item["id"], {
            "source": "codex", "source_instance": "work", "external_id": "shared-thread"})
        self.assertNotEqual(default["id"], other["id"])
        listed = task_sessions.task_sessions(self.profile.store, self.item["id"])
        instances = {row["source_instance"] for row in listed["items"]
                     if not row.get("legacy") and row["external_id"] == "shared-thread"}
        self.assertEqual(instances, {"", "work"})

    def test_stale_unlink_cannot_remove_a_relinked_session(self):
        body = {"source": "dsh", "external_id": "cas-session"}
        old = task_sessions.link_session(self.profile.store, self.item["id"], body)
        expected = {"expected_task_id": self.item["id"], "expected_updated_at": old["updated_at"]}
        task_sessions.unlink_session(self.profile.store, old["id"], expected)
        other = self.profile.store.save_item({"title": "Other", "project_id": self.project["id"],
            "provider_id": None, "state": "todo", "session_key": None})
        for destination in (other["id"], self.item["id"]):
            current = task_sessions.link_session(self.profile.store, destination, body)
            with self.assertRaises(ProfileError) as error:
                task_sessions.unlink_session(self.profile.store, old["id"], expected)
            self.assertEqual(error.exception.reason, "session_changed")
            self.assertEqual(task_sessions.task_sessions(self.profile.store, destination)["items"][0]["id"], old["id"])
            task_sessions.unlink_session(self.profile.store, current["id"], {
                "expected_task_id": destination, "expected_updated_at": current["updated_at"]})

    def test_unlink_requires_both_snapshot_fields(self):
        row = task_sessions.link_session(self.profile.store, self.item["id"], {
            "source": "dsh", "external_id": "validate-cas"})
        for body in ({}, {"expected_task_id": self.item["id"]},
                     {"expected_task_id": self.item["id"], "expected_updated_at": None},
                     {"expected_task_id": None, "expected_updated_at": row["updated_at"]}):
            with self.subTest(body=body), self.assertRaises(ProfileError):
                task_sessions.unlink_session(self.profile.store, row["id"], body)
        self.assertTrue(any(value["id"] == row["id"] for value in
                            task_sessions.task_sessions(self.profile.store, self.item["id"])["items"]))

    def test_project_move_uses_current_task_project(self):
        linked = task_sessions.link_session(self.profile.store, self.item["id"], {
            "source": "dsh", "external_id": "move-session"})
        (self.root / "second").mkdir()
        second = self.profile.store.save_project(project_body(self.root / "second"))
        with self.profile.store.connection() as connection:
            connection.execute("UPDATE task_items SET project_id=? WHERE id=?", (second["id"], self.item["id"]))
        first_list = task_sessions.list_sessions(self.profile.store, self.project["id"])
        second_list = task_sessions.list_sessions(self.profile.store, second["id"])
        self.assertFalse(any(row["id"] == linked["id"] for row in first_list["items"]))
        self.assertTrue(any(row["id"] == linked["id"] for row in second_list["items"]))

    def test_project_move_to_unassigned_hides_old_project(self):
        linked = task_sessions.link_session(self.profile.store, self.item["id"], {
            "source": "dsh", "external_id": "unassigned-session"})
        with self.profile.store.connection() as connection:
            connection.execute("UPDATE task_items SET project_id=NULL WHERE id=?", (self.item["id"],))
        first_list = task_sessions.list_sessions(self.profile.store, self.project["id"])
        self.assertFalse(any(row["id"] == linked["id"] for row in first_list["items"]))
        task = task_sessions.task_sessions(self.profile.store, self.item["id"])
        moved = next(row for row in task["items"] if row["id"] == linked["id"])
        self.assertIsNone(moved["project_id"])

    def test_cross_task_collision_and_invalid_metadata(self):
        self.profile.store.save_project({"id": None, "name": "Other", "archived": False,
            "directories": [{"id": "main", "path": str(self.root / "other")}],
            "source": {"kind": "none", "directory_id": None, "relative_path": None}})
        (self.root / "other").mkdir()
        second = self.profile.store.list_projects()[1]
        other = self.profile.store.save_item({
            "title": "Other task", "project_id": second["id"], "provider_id": None,
            "state": "todo", "session_key": None})
        task_sessions.link_session(self.profile.store, self.item["id"], {
            "source": "codex", "external_id": "same"})
        with self.assertRaises(ProfileError) as error:
            task_sessions.link_session(self.profile.store, other["id"], {"source": "codex", "external_id": "same"})
        self.assertEqual(error.exception.reason, "session_conflict")
        for body in ({"source": "codex"}, {"source": "codex", "external_id": "x", "url": "file:///secret"},
                     {"source": "codex", "external_id": "x", "last_seen_at": "not-a-time"}):
            with self.subTest(body=body), self.assertRaises(ProfileError):
                task_sessions.link_session(self.profile.store, self.item["id"], body)


class SessionHTTPTests(unittest.TestCase):
    request = ProfileHTTPTests.request

    def setUp(self):
        ProfileHTTPTests.setUp(self)
        (self.root / "work").mkdir()
        self.project = self.profile.store.save_project(project_body(self.root / "work"))
        self.item = self.profile.store.save_item({
            "title": "API session", "project_id": self.project["id"], "provider_id": None,
            "state": "doing", "session_key": None})
        with self.profile.store.connection() as connection:
            connection.execute("UPDATE task_items SET session_key=? WHERE id=?",
                               ("codex:legacy", self.item["id"]))

    def test_task_sessions_list_link_and_unlink(self):
        status, body = self.request(f"/api/workspace/tasks/{self.item['id']}/sessions", "POST",
                                    {"source": "codex", "external_id": "thread-api", "title": "T"})
        self.assertEqual((status, body["source"]), (200, "codex"))
        status, listed = self.request(f"/api/workspace/tasks/{self.item['id']}/sessions")
        self.assertEqual((status, listed["legacy_session_key"], len(listed["items"])),
                         (200, "codex:legacy", 2))
        status, project = self.request(f"/api/workspace/sessions?project_id={self.project['id']}")
        self.assertEqual((status, len(project["items"])), (200, 2))
        status, unlinked = self.request(f"/api/workspace/sessions/{body['id']}/unlink", "POST", {
            "expected_task_id": self.item["id"], "expected_updated_at": body["updated_at"]})
        self.assertEqual((status, unlinked["task_id"]), (200, None))
        self.assertEqual(self.request(f"/api/workspace/sessions/{body['id']}/unlink", "POST", {
            "expected_task_id": self.item["id"], "expected_updated_at": body["updated_at"]})[0], 409)
        self.assertEqual(self.request(f"/api/workspace/sessions/{body['id']}/unlink", "POST", {})[0], 422)

    def test_session_post_requires_csrf(self):
        self.assertEqual(self.request(f"/api/workspace/tasks/{self.item['id']}/sessions", "POST",
                                      {"source": "codex", "external_id": "x"}, {"X-Session-CSRF": ""})[0], 403)
