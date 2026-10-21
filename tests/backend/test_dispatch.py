"""Task brief and manual dispatch contracts over disposable profiles."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_everything.input_validation import ProfileError
from control_everything.profiles.profile import Profile
from control_everything.workspace import dispatch
from control_everything.workspace.service import WorkspaceService
from tests.backend.test_profile_http import ProfileHTTPTests
from tests.backend.test_project_management import project_body


def agent(**fields):
    return {"id": None, "name": "Codex", "kind": "codex", "command": "codex", "enabled": True, **fields}


class DispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = Profile.open(str(self.root / "profile"))
        self.profile.save_agents({"expected_revision": 0, "items": [agent()]})
        self.agent = self.profile.agent_settings()["items"][0]
        (self.root / "work").mkdir()
        self.project = self.profile.store.save_project(project_body(self.root / "work"))
        self.item = self.profile.store.save_item({
            "title": "M2 task", "project_id": self.project["id"], "provider_id": None,
            "state": "todo", "session_key": None})
        self.service = WorkspaceService(profile=self.profile)

    def brief(self, revision=0):
        return self.service.save_task_brief(self.item["id"], {
            "expected_revision": revision, "request_text": "实现一个小功能",
            "acceptance_text": "测试通过", "constraints_text": "不要覆盖文件",
            "references": ["README.md"], "execution_directory_id": "main"})

    def test_brief_roundtrip_and_version_conflict(self):
        saved = self.brief()
        self.assertEqual((saved["brief"]["revision"], saved["brief"]["request_text"]), (1, "实现一个小功能"))
        loaded = self.service.get_task_brief(self.item["id"])
        self.assertEqual(loaded["brief"]["references"], ["README.md"])
        self.assertEqual(loaded["directories"][0]["binding_id"], "main")
        with self.assertRaises(ProfileError) as error:
            self.brief(revision=0)
        self.assertEqual(error.exception.reason, "brief_changed")

    def test_preview_create_duplicate_and_claim_link(self):
        self.brief()
        preview = self.service.preview_task_dispatch(self.item["id"], {
            "agent_id": self.agent["id"], "directory_id": "main"})
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(preview["dispatch"]["brief_revision"], 1)
        self.assertIn("实现一个小功能", preview["content_text"])
        self.assertNotIn("bearer", preview["content_text"].lower())
        self.assertNotIn("ag_", preview["content_text"])
        created = self.service.create_task_dispatch(self.item["id"], {
            "agent_id": self.agent["id"], "directory_id": "main",
            "expected_brief_revision": 1, "confirmed": True,
            "preview_sha256": preview["brief_sha256"]})
        self.assertEqual((created["status"], created["dispatch"]["delivery_state"]), ("previewed", "previewed"))
        with self.assertRaises(ProfileError) as changed:
            self.service.create_task_dispatch(self.item["id"], {
                "agent_id": self.agent["id"], "directory_id": "main",
                "expected_brief_revision": 1, "confirmed": True,
                "preview_sha256": "0" * 64})
        self.assertEqual(changed.exception.reason, "dispatch_preview_changed")
        duplicate = self.service.create_task_dispatch(self.item["id"], {
            "agent_id": self.agent["id"], "directory_id": "main",
            "expected_brief_revision": 1, "confirmed": True})
        self.assertTrue(duplicate["duplicate"])
        sent = self.service.mark_dispatch_sent(created["dispatch"]["id"])
        self.assertEqual((sent["delivery_state"], sent["sent_at"] is not None), ("sent", True))
        item = self.profile.store.get_item(self.item["id"])
        claimed = self.service.claim_agent_task(self.agent["id"], self.item["id"], item["revision"])
        self.assertEqual(claimed["run"]["dispatch_id"], created["dispatch"]["id"])
        linked = self.service.task_dispatch(created["dispatch"]["id"])
        self.assertEqual(linked["run_id"], claimed["run"]["id"])

    def test_cancel_and_confirmation_bounds(self):
        self.brief()
        created = self.service.create_task_dispatch(self.item["id"], {
            "agent_id": self.agent["id"], "directory_id": "main",
            "expected_brief_revision": 1, "confirmed": True})
        cancelled = self.service.cancel_task_dispatch(created["dispatch"]["id"])
        self.assertEqual(cancelled["delivery_state"], "cancelled")
        with self.assertRaises(ProfileError) as error:
            self.service.mark_dispatch_sent(created["dispatch"]["id"])
        self.assertEqual(error.exception.reason, "dispatch_state_conflict")
        with self.assertRaises(ProfileError) as error:
            self.service.create_task_dispatch(self.item["id"], {
                "agent_id": self.agent["id"], "directory_id": "main",
                "expected_brief_revision": 1, "confirmed": False})
        self.assertEqual(error.exception.reason, "confirmation_required")

    def test_recreate_after_claimed_dispatch_is_new_snapshot(self):
        self.brief()
        first = self.service.create_task_dispatch(self.item["id"], {
            "agent_id": self.agent["id"], "directory_id": "main",
            "expected_brief_revision": 1, "confirmed": True})
        self.service.mark_dispatch_sent(first["dispatch"]["id"])
        item = self.profile.store.get_item(self.item["id"])
        claimed = self.service.claim_agent_task(self.agent["id"], self.item["id"], item["revision"])
        self.service.cancel_agent_run(self.item["id"], claimed["item"]["revision"], "test reset")
        second = self.service.create_task_dispatch(self.item["id"], {
            "agent_id": self.agent["id"], "directory_id": "main",
            "expected_brief_revision": 1, "confirmed": True})
        self.assertFalse(second["duplicate"])
        self.assertNotEqual(second["dispatch"]["id"], first["dispatch"]["id"])

    def test_review_material_projects_snapshot_files_and_missing_tests(self):
        self.brief()
        created = self.service.create_task_dispatch(self.item["id"], {
            "agent_id": self.agent["id"], "directory_id": "main",
            "expected_brief_revision": 1, "confirmed": True})
        self.service.mark_dispatch_sent(created["dispatch"]["id"])
        item = self.profile.store.get_item(self.item["id"])
        claimed = self.service.claim_agent_task(self.agent["id"], self.item["id"], item["revision"])
        (self.root / "work" / "report.md").write_text("evidence", encoding="utf-8")
        submitted = self.service.submit_agent_review(
            self.agent["id"], self.item["id"], claimed["item"]["revision"], "完成了报告",
            ["report.md"], run_id=claimed["run"]["id"])
        detail = self.service.agent_task_detail(self.item["id"])
        material = detail["review_material"]
        self.assertEqual(material["request"]["request_text"], "实现一个小功能")
        self.assertEqual(material["request"]["brief_sha256"], created["dispatch"]["brief_sha256"])
        self.assertEqual(submitted["item"]["state"], "review")
        self.assertEqual(material["agent_result"]["source"], "agent_reported")
        self.assertEqual(material["files"], [{"path": "report.md", "exists": True, "source": "agent_reported"}])
        self.assertEqual((material["tests"]["status"], material["tests"]["commands"]), ("missing", []))
        self.assertEqual(material["verification"]["status"], "unverified")
        self.assertTrue(material["completeness"]["request"] and material["completeness"]["files"])
        self.assertFalse(material["completeness"]["tests"])


class DispatchHTTPTests(unittest.TestCase):
    request = ProfileHTTPTests.request

    def setUp(self):
        ProfileHTTPTests.setUp(self)
        self.profile.save_agents({"expected_revision": 0, "items": [agent()]})
        self.agent = self.profile.agent_settings()["items"][0]
        (self.root / "work").mkdir()
        self.project = self.profile.store.save_project(project_body(self.root / "work"))
        self.item = self.profile.store.save_item({
            "title": "API M2", "project_id": self.project["id"], "provider_id": None,
            "state": "todo", "session_key": None})

    def test_brief_preview_dispatch_and_sent_routes(self):
        status, saved = self.request(f"/api/workspace/tasks/{self.item['id']}/brief", "POST", {
            "expected_revision": 0, "request_text": "需求", "acceptance_text": "验收",
            "constraints_text": "约束", "references": ["README.md"], "execution_directory_id": "main"})
        self.assertEqual((status, saved["brief"]["revision"]), (200, 1))
        status, loaded = self.request(f"/api/workspace/tasks/{self.item['id']}/brief")
        self.assertEqual((status, loaded["brief"]["request_text"]), (200, "需求"))
        status, preview = self.request(f"/api/workspace/tasks/{self.item['id']}/dispatch-preview", "POST",
                                      {"agent_id": self.agent["id"], "directory_id": "main"})
        self.assertEqual((status, preview["status"]), (200, "preview"))
        status, created = self.request(f"/api/workspace/tasks/{self.item['id']}/dispatch", "POST", {
            "agent_id": self.agent["id"], "directory_id": "main",
            "expected_brief_revision": 1, "confirmed": True})
        self.assertEqual((status, created["dispatch"]["delivery_state"]), (200, "previewed"))
        status, listed = self.request(f"/api/workspace/tasks/{self.item['id']}/dispatches")
        self.assertEqual((status, len(listed["items"])), (200, 1))
        status, sent = self.request(f"/api/workspace/dispatches/{created['dispatch']['id']}/sent", "POST", {})
        self.assertEqual((status, sent["delivery_state"]), (200, "sent"))
        status, dispatch_one = self.request(f"/api/workspace/dispatches/{created['dispatch']['id']}")
        self.assertEqual((status, dispatch_one["delivery_state"]), (200, "sent"))

    def test_dispatch_routes_require_csrf_and_valid_confirmation(self):
        self.assertEqual(self.request(f"/api/workspace/tasks/{self.item['id']}/brief", "POST",
                                      {"expected_revision": 0, "request_text": "x", "acceptance_text": "",
                                       "constraints_text": "", "references": [], "execution_directory_id": None},
                                      {"X-Session-CSRF": ""})[0], 403)
        status, _ = self.request(f"/api/workspace/tasks/{self.item['id']}/brief", "POST", {
            "expected_revision": 0, "request_text": "x", "acceptance_text": "",
            "constraints_text": "", "references": [], "execution_directory_id": None})
        self.assertEqual(status, 200)
        self.assertEqual(self.request(f"/api/workspace/tasks/{self.item['id']}/dispatch", "POST", {
            "agent_id": self.agent["id"], "directory_id": "main",
            "expected_brief_revision": 1, "confirmed": False})[0], 422)
