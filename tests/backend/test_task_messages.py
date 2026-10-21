"""Human supplements, waiting-user answers and manual message delivery."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_everything.input_validation import ProfileError
from control_everything.profiles.profile import Profile
from control_everything.workspace.service import WorkspaceService
from tests.backend.test_profile_http import ProfileHTTPTests
from tests.backend.test_project_management import project_body


def agent():
    return {"id": None, "name": "Codex", "kind": "codex", "command": "codex", "enabled": True}


class MessageTests(unittest.TestCase):
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
            "title": "Message task", "project_id": self.project["id"], "provider_id": None,
            "assigned_agent_id": self.agent["id"], "state": "todo", "session_key": None})
        self.service = WorkspaceService(profile=self.profile)

    def message(self, **fields):
        body = {"kind": "supplement", "body_text": "补充要求", "agent_id": self.agent["id"], **fields}
        return self.service.create_task_message(self.item["id"], body)

    def test_supplement_idempotency_and_delivery_state(self):
        created = self.message(client_token="ui-1")
        self.assertEqual((created["status"], created["message"]["delivery_state"]), ("created", "previewed"))
        duplicate = self.message(client_token="ui-1")
        self.assertEqual((duplicate["status"], duplicate["message"]["id"]),
                         ("existing", created["message"]["id"]))
        sent = self.service.mark_message_sent(created["message"]["id"])
        self.assertEqual((sent["delivery_state"], sent["sent_at"] is not None), ("sent", True))
        with self.assertRaises(ProfileError) as error:
            self.service.cancel_task_message(created["message"]["id"])
        self.assertEqual(error.exception.reason, "message_state_conflict")

    def test_waiting_user_and_agent_detail_messages(self):
        claimed = self.service.claim_agent_task(self.agent["id"], self.item["id"], self.item["revision"])
        progress = self.service.progress_agent_task(
            self.agent["id"], self.item["id"], claimed["item"]["revision"], "waiting_user", 50,
            "需要授权", wait_kind="tool_authorization", request_id="req-1", agent_hint="请在原会话确认",
            run_id=claimed["run"]["id"])
        self.assertEqual((progress["item"]["state"], progress["run"]["status"]), ("blocked", "running"))
        data = self.service.task_messages(self.item["id"])
        self.assertEqual((data["waiting_user"]["wait_kind"], data["waiting_user"]["request_id"]),
                         ("tool_authorization", "req-1"))
        self.message(kind="user_input", body_text="允许执行", run_id=claimed["run"]["id"])
        detail = self.service.get_agent_task(self.agent["id"], self.item["id"])
        self.assertEqual([row["body_text"] for row in detail["messages"]], ["允许执行"])

    def test_invalid_message_and_unknown_agent(self):
        for body in ({"kind": "bogus", "body_text": "x"}, {"kind": "supplement", "body_text": " "}):
            with self.subTest(body=body), self.assertRaises(ProfileError):
                self.service.create_task_message(self.item["id"], body)
        with self.assertRaises(ProfileError) as error:
            self.service.create_task_message(self.item["id"], {
                "kind": "supplement", "body_text": "x", "agent_id": "00000000-0000-0000-0000-000000000000"})
        self.assertEqual(error.exception.reason, "agent_not_found")


class MessageHTTPTests(unittest.TestCase):
    request = ProfileHTTPTests.request

    def setUp(self):
        ProfileHTTPTests.setUp(self)
        self.profile.save_agents({"expected_revision": 0, "items": [agent()]})
        self.agent = self.profile.agent_settings()["items"][0]
        (self.root / "work").mkdir()
        self.project = self.profile.store.save_project(project_body(self.root / "work"))
        self.item = self.profile.store.save_item({
            "title": "API message", "project_id": self.project["id"], "provider_id": None,
            "state": "todo", "session_key": None})

    def test_message_routes_and_csrf(self):
        status, created = self.request(f"/api/workspace/tasks/{self.item['id']}/messages", "POST", {
            "kind": "supplement", "body_text": "补充", "agent_id": self.agent["id"], "client_token": "h-1"})
        self.assertEqual((status, created["status"]), (200, "created"))
        status, listed = self.request(f"/api/workspace/tasks/{self.item['id']}/messages")
        self.assertEqual((status, len(listed["items"])), (200, 1))
        status, sent = self.request(f"/api/workspace/messages/{created['message']['id']}/sent", "POST", {})
        self.assertEqual((status, sent["message"]["delivery_state"]), (200, "sent"))
        self.assertEqual(self.request(f"/api/workspace/tasks/{self.item['id']}/messages", "POST", {
            "kind": "supplement", "body_text": "x"}, {"X-Session-CSRF": ""})[0], 403)
