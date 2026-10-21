"""Agent claim/progress/review contracts over disposable profiles; no process control."""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from control_everything.profiles.profile import Profile
from control_everything.input_validation import ProfileError
from control_everything.workspace import agent_runs, management
from control_everything.workspace.service import WorkspaceService
from control_everything.workspace.store import WorkspaceError
from tests.backend.test_profile_http import ProfileHTTPTests
from tests.backend.test_project_management import project_body


def agent(**fields):
    return {"id": None, "name": "Agent", "kind": "codex", "command": "codex", "enabled": True, **fields}


class AgentRunTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = Profile.open(str(self.root / "profile"))
        self.profile.save_agents({"expected_revision": 0, "items": [agent(name="Codex")]})
        self.agent_id = self.profile.agent_settings()["items"][0]["id"]
        (self.root / "work").mkdir()
        self.project = self.profile.store.save_project(project_body(self.root / "work"))
        self.item = self.profile.store.save_item({
            "title": "Task", "project_id": self.project["id"], "provider_id": None,
            "state": "todo", "session_key": None, "assigned_agent_id": self.agent_id})
        self.service = WorkspaceService(profile=self.profile)

    def test_claim_progress_submit_and_human_approval(self):
        claimed = self.service.claim_agent_task(self.agent_id, self.item["id"], self.item["revision"])
        self.assertEqual((claimed["item"]["state"], claimed["run"]["status"]), ("doing", "claimed"))
        with self.assertRaises(ProfileError) as conflict:
            self.profile.store.save_item({"assigned_agent_id": None}, item_id=self.item["id"],
                                          expected_revision=claimed["item"]["revision"])
        self.assertEqual(conflict.exception.reason, "agent_run_conflict")
        with self.assertRaises(WorkspaceError) as error:
            self.service.claim_agent_task(self.agent_id, self.item["id"], claimed["item"]["revision"])
        self.assertEqual(str(error.exception), "run_conflict")
        progressed = self.service.progress_agent_task(
            self.agent_id, self.item["id"], claimed["item"]["revision"], "implementing", 40, "working",
            run_id=claimed["run"]["id"])
        self.assertEqual((progressed["item"]["state"], progressed["run"]["status"]), ("doing", "running"))
        submitted = self.service.submit_agent_review(
            self.agent_id, self.item["id"], progressed["item"]["revision"], "done", ["tests"],
            run_id=claimed["run"]["id"])
        self.assertEqual((submitted["item"]["state"], submitted["run"]["status"]), ("review", "waiting_review"))
        with self.assertRaises(WorkspaceError) as repeated:
            self.service.submit_agent_review(
                self.agent_id, self.item["id"], submitted["item"]["revision"], "replaced", [],
                run_id=claimed["run"]["id"])
        self.assertEqual(str(repeated.exception), "review_already_submitted")
        review = management.save_review(self.profile.store, {
            "task_id": self.item["id"], "expected_revision": submitted["item"]["revision"],
            "verdict": "approved", "reason": "ok", "evidence": ["manual"], "reviewer": "me", "confirmed": True})
        self.assertEqual(review["item"]["state"], "done")
        detail = self.service.get_agent_task(self.agent_id, self.item["id"])
        self.assertEqual(detail["run"]["status"], "completed")
        self.assertEqual([event["kind"] for event in detail["events"]], ["claimed", "progress", "waiting_review", "completed"])

    def test_project_scope_filters_tasks(self):
        (self.root / "second").mkdir()
        second = self.profile.store.save_project(project_body(self.root / "second"))
        current = self.profile.agent_settings()
        added = self.profile.save_agents({"expected_revision": current["revision"], "items": [
            *[{key: row[key] for key in ("id", "name", "kind", "command", "enabled", "project_ids")}
              for row in current["items"]],
            agent(name="Scoped", project_ids=[self.project["id"]])]})
        scoped = next(row for row in added["items"] if row["name"] == "Scoped")
        first = self.profile.store.save_item({
            "title": "Allowed", "project_id": self.project["id"], "provider_id": None,
            "state": "todo", "session_key": None, "assigned_agent_id": scoped["id"]})
        second_item = self.profile.store.save_item({
            "title": "Denied", "project_id": second["id"], "provider_id": None,
            "state": "todo", "session_key": None, "assigned_agent_id": scoped["id"]})
        listed = self.service.list_agent_tasks(scoped["id"], project_ids=[self.project["id"]])
        self.assertEqual([row["id"] for row in listed["items"]], [first["id"]])
        with self.assertRaises(WorkspaceError) as error:
            self.service.get_agent_task(scoped["id"], second_item["id"], project_ids=[self.project["id"]])
        self.assertEqual(str(error.exception), "project_forbidden")

    def test_long_running_contact_unknown_keeps_occupancy(self):
        claimed = self.service.claim_agent_task(self.agent_id, self.item["id"], self.item["revision"])
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with self.profile.store.connection() as connection:
            connection.execute("UPDATE agent_runs SET heartbeat_at=? WHERE id=?", (old, claimed["run"]["id"]))
        detail = self.service.get_agent_task(self.agent_id, self.item["id"])
        self.assertEqual((detail["item"]["state"], detail["run"]["status"]), ("doing", "claimed"))
        self.assertEqual(detail["connection_state"], "unknown")
        projected = self.service.page_items(include_assignment=True)["items"][0]
        self.assertEqual((projected["assignment_state"], projected["connection_state"]), ("running", "unknown"))
        with self.assertRaises(WorkspaceError) as error:
            self.service.claim_agent_task(self.agent_id, self.item["id"], detail["item"]["revision"])
        self.assertEqual(str(error.exception), "run_conflict")

    def test_waiting_user_is_distinct_from_long_running(self):
        claimed = self.service.claim_agent_task(self.agent_id, self.item["id"], self.item["revision"])
        waiting = self.service.progress_agent_task(
            self.agent_id, self.item["id"], claimed["item"]["revision"], "waiting_user", None,
            "需要允许运行 npm test", None, "tool_authorization", "req-1", "回到 Codex 终端授权",
            run_id=claimed["run"]["id"])
        self.assertEqual((waiting["item"]["state"], waiting["run"]["status"]), ("blocked", "running"))
        detail = self.service.get_agent_task(self.agent_id, self.item["id"])
        self.assertEqual(detail["events"][-1]["payload"]["wait_kind"], "tool_authorization")
        self.assertEqual(detail["events"][-1]["payload"]["request_id"], "req-1")
        projected = self.service.page_items(include_assignment=True)["items"][0]
        self.assertEqual(projected["assignment_state"], "waiting_user")
        resumed = self.service.progress_agent_task(
            self.agent_id, self.item["id"], waiting["item"]["revision"], "implementing", 20, "授权后继续",
            run_id=claimed["run"]["id"])
        self.assertEqual((resumed["item"]["state"], resumed["run"]["status"]), ("doing", "running"))
        self.assertEqual(self.service.page_items(include_assignment=True)["items"][0]["assignment_state"], "running")

    def test_human_cancel_releases_run_and_old_agent_writes_fail(self):
        claimed = self.service.claim_agent_task(self.agent_id, self.item["id"], self.item["revision"])
        cancelled = self.service.cancel_agent_run(
            self.item["id"], claimed["item"]["revision"], "外部工具无响应，人工释放")
        self.assertEqual((cancelled["item"]["state"], cancelled["run"]["status"]), ("todo", "cancelled"))
        self.assertEqual(cancelled["run"]["id"], claimed["run"]["id"])
        detail = self.service.get_agent_task(self.agent_id, self.item["id"])
        self.assertEqual(detail["events"][-1]["payload"]["reason"], "human_cancelled")
        with self.assertRaises(WorkspaceError) as error:
            self.service.submit_agent_review(
                self.agent_id, self.item["id"], detail["item"]["revision"], "stale", [],
                run_id=claimed["run"]["id"])
        self.assertEqual(str(error.exception), "run_not_found")
        reclaimed = self.service.claim_agent_task(self.agent_id, self.item["id"], detail["item"]["revision"])
        self.assertNotEqual(reclaimed["run"]["id"], claimed["run"]["id"])
        with self.assertRaises(WorkspaceError) as stale:
            self.service.progress_agent_task(
                self.agent_id, self.item["id"], reclaimed["item"]["revision"], "testing", 90,
                run_id=claimed["run"]["id"])
        self.assertEqual(str(stale.exception), "run_mismatch")

    def test_waiting_review_is_not_expired(self):
        claimed = self.service.claim_agent_task(self.agent_id, self.item["id"], self.item["revision"])
        progressed = self.service.progress_agent_task(
            self.agent_id, self.item["id"], claimed["item"]["revision"], "testing", 80,
            run_id=claimed["run"]["id"])
        submitted = self.service.submit_agent_review(
            self.agent_id, self.item["id"], progressed["item"]["revision"], "ready", [],
            run_id=claimed["run"]["id"])
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with self.profile.store.connection() as connection:
            connection.execute("UPDATE agent_runs SET heartbeat_at=? WHERE id=?",
                               (old, submitted["run"]["id"]))
        detail = self.service.get_agent_task(self.agent_id, self.item["id"])
        self.assertEqual((detail["item"]["state"], detail["run"]["status"]), ("review", "waiting_review"))
        self.assertEqual(detail["connection_state"], "unknown")

    def test_claim_rejects_wrong_agent_and_unassigned_task(self):
        current = self.profile.agent_settings()
        added = self.profile.save_agents({"expected_revision": current["revision"], "items": [
            *[{key: row[key] for key in ("id", "name", "kind", "command", "enabled")} for row in current["items"]],
            agent(name="Other")]})
        other_id = next(row["id"] for row in added["items"] if row["name"] == "Other")
        with self.assertRaises(WorkspaceError) as error:
            self.service.claim_agent_task(other_id, self.item["id"], self.item["revision"])
        self.assertEqual(str(error.exception), "not_assigned")
        unassigned = self.profile.store.save_item({
            "title": "No agent", "project_id": self.project["id"], "provider_id": None,
            "state": "todo", "session_key": None})
        with self.assertRaises(WorkspaceError) as error:
            self.service.claim_agent_task(self.agent_id, unassigned["id"], unassigned["revision"])
        self.assertEqual(str(error.exception), "not_assigned")


class AgentAPITests(unittest.TestCase):
    request = ProfileHTTPTests.request

    def setUp(self):
        ProfileHTTPTests.setUp(self)
        self.profile.save_agents({"expected_revision": 0, "items": [agent(name="Codex")]})
        self.agent = self.profile.agent_settings()["items"][0]
        self.token = self.profile.rotate_agent_token({"id": self.agent["id"]})["token"]
        (self.root / "work").mkdir()
        self.project = self.profile.store.save_project(project_body(self.root / "work"))
        self.item = self.profile.store.save_item({
            "title": "API task", "project_id": self.project["id"], "provider_id": None,
            "state": "todo", "session_key": None, "assigned_agent_id": self.agent["id"]})

    def headers(self):
        return {"Authorization": "Bearer " + self.token}

    def test_agent_api_claim_progress_submit_and_review(self):
        status, body = self.request("/api/agent/tasks", headers=self.headers())
        self.assertEqual((status, body["total"]), (200, 1))
        status, agents = self.request("/api/workspace/agents")
        self.assertEqual(status, 200)
        self.assertTrue(next(row for row in agents["items"] if row["id"] == self.agent["id"])["online"])
        status, claimed = self.request(f"/api/agent/tasks/{self.item['id']}/claim", "POST",
                                      {"expected_revision": self.item["revision"]}, self.headers())
        self.assertEqual((status, claimed["run"]["status"]), (200, "claimed"))
        status, progressed = self.request(f"/api/agent/tasks/{self.item['id']}/progress", "POST",
            {"expected_revision": claimed["item"]["revision"], "run_id": claimed["run"]["id"],
             "stage": "testing", "percent": 70}, self.headers())
        self.assertEqual((status, progressed["run"]["status"]), (200, "running"))
        status, page = self.request("/api/workspace/items")
        projected = next(row for row in page["items"] if row["id"] == self.item["id"])
        self.assertEqual((status, projected["assignment_state"]), (200, "running"))
        status, detail = self.request(f"/api/workspace/agent-tasks/{self.item['id']}")
        self.assertEqual((status, detail["run"]["status"]), (200, "running"))
        self.assertEqual(detail["events"][0]["kind"], "claimed")
        status, submitted = self.request(f"/api/agent/tasks/{self.item['id']}/submit-review", "POST",
            {"expected_revision": progressed["item"]["revision"], "run_id": claimed["run"]["id"],
             "summary": "finished", "evidence": ["tests"]}, self.headers())
        self.assertEqual((status, submitted["item"]["state"]), (200, "review"))
        status, review = self.request(f"/api/agent/tasks/{self.item['id']}/review", headers=self.headers())
        self.assertEqual((status, review["run"]["status"]), (200, "waiting_review"))
        self.assertIn("connection_state", review)
        self.assertEqual(self.request("/api/agent/tasks", headers={"Authorization": "Bearer wrong"})[0], 401)

    def test_agent_api_requires_bearer_and_allows_no_browser_csrf(self):
        self.assertEqual(self.request("/api/agent/tasks")[0], 401)
        self.assertEqual(self.request("/api/agent/tasks", headers={"Authorization": "Bearer wrong"})[0], 401)
        status, body = self.request("/api/agent/tasks", headers=self.headers())
        self.assertEqual(status, 200)

    def test_waiting_user_and_human_cancel_over_browser(self):
        status, claimed = self.request(f"/api/agent/tasks/{self.item['id']}/claim", "POST",
                                      {"expected_revision": self.item["revision"]}, self.headers())
        self.assertEqual(status, 200)
        status, waiting = self.request(f"/api/agent/tasks/{self.item['id']}/progress", "POST",
            {"expected_revision": claimed["item"]["revision"], "run_id": claimed["run"]["id"],
             "stage": "waiting_user", "wait_kind": "tool_authorization", "message": "需要授权",
             "request_id": "req-1"}, self.headers())
        self.assertEqual((status, waiting["item"]["state"]), (200, "blocked"))
        status, detail = self.request(f"/api/workspace/agent-tasks/{self.item['id']}")
        self.assertEqual((status, detail["events"][-1]["payload"]["wait_kind"]), (200, "tool_authorization"))
        status, cancelled = self.request(f"/api/workspace/agent-tasks/{self.item['id']}/cancel", "POST",
            {"expected_revision": waiting["item"]["revision"], "reason": "人工释放"})
        self.assertEqual((status, cancelled["item"]["state"], cancelled["run"]["status"]),
                         (200, "todo", "cancelled"))
        status, body = self.request(f"/api/agent/tasks/{self.item['id']}/submit-review", "POST",
            {"expected_revision": cancelled["item"]["revision"], "run_id": claimed["run"]["id"],
             "summary": "stale"}, self.headers())
        self.assertEqual(status, 404)
