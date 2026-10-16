"""Socket-free profile handler contracts with all external sources replaced."""
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from session_panel.http.gateway import PROFILE_CAPABILITIES
from session_panel.profiles.profile import Profile
from session_panel.workspace.service import WorkspaceService
from tests.backend import test_execution_http
from tests.backend.test_project_management import project_body


class ProfileHTTPTests(unittest.TestCase):
    request = test_execution_http.ExecutionHTTPTests.request

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = Profile.open(str(self.root / "profile"))
        self.quota, self.news = Mock(), Mock()
        self.quota.get_cached.return_value = {"status": "fixture-quota"}
        self.quota.refresh.return_value = {"status": "fixture-refreshed"}
        self.news.get.return_value = {"status": "fixture-news"}
        self.workspace = WorkspaceService(profile=self.profile, quota_source=self.quota, news_source=self.news)
        self.server = SimpleNamespace(profile=self.profile, workspace=self.workspace, service=None, execution=None,
                                      execution_reason=None, capabilities=dict(PROFILE_CAPABILITIES), startup_integrations=self.profile.integrations(),
                                      launchers=None, source=None, expected_host="127.0.0.1:1234", origin="http://127.0.0.1:1234", csrf_token="token")

    def test_default_routes_no_personal_or_execution_source(self):
        targets = ["session_panel.http.gateway.catalog", "session_panel.http.gateway.pulse", "session_panel.http.gateway.start",
                   "session_panel.workspace.progress.codex_cwds", "session_panel.workspace.progress.grok_cwds",
                   "session_panel.http.gateway.list_codex_threads"]
        patches = [patch(target, side_effect=AssertionError("forbidden source")) for target in targets]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch("session_panel.http.gateway.headlines", return_value=[{"title": "Public fixture"}]):
            bootstrap = self.request("/api/bootstrap")[1]
            self.assertTrue(bootstrap["capabilities"]["profile"])
            self.assertFalse(bootstrap["capabilities"]["execution"])
            self.assertEqual(self.request("/api/workspace")[1]["projects"]["items"], [])
            self.assertEqual(self.request("/api/progress")[1]["projects"], [])
            desk = self.request("/api/desk")[1]
            self.assertEqual((desk["pulse"], desk["launchers"]), ([], []))
            self.assertEqual(desk["news"][0]["title"], "Public fixture")
            for route in ("/api/sessions", "/api/sessions/" + str(uuid4()), "/api/workbench", "/api/launch"):
                self.assertEqual(self.request(route)[1]["reason"], "capability_unavailable")
            self.assertEqual(self.request("/api/workspace/execution")[1]["reason"], "execution_unavailable")
            self.assertEqual(self.request("/api/codex/threads")[1]["status"], "not_configured")
            self.assertEqual(self.request("/api/workspace/usage")[1]["codex"]["status"], "fixture-quota")
            self.assertEqual(self.request("/api/workspace/usage/refresh", "POST", {})[1]["codex"]["status"], "fixture-refreshed")
            self.assertEqual(self.request("/api/workspace/news")[1]["status"], "fixture-news")

    def test_new_write_api_security_and_body_budget_is_strict(self):
        routes = ["/api/profile/settings", "/api/profile/import", "/api/workspace/projects/save",
                  "/api/workspace/projects/restore", "/api/workspace/progress"]
        for route in routes:
            for headers, reason in (({"Host": "bad"}, "host_rejected"), ({"Origin": ""}, "origin_rejected"),
                                    ({"X-Session-Client": ""}, "client_header_required"), ({"X-Session-CSRF": ""}, "csrf_rejected")):
                self.assertEqual(self.request(route, "POST", {}, headers)[1]["reason"], reason)
            for raw, headers in ((b"{}", {"Content-Length": ["2", "2"]}), (b"{}", {"Transfer-Encoding": "chunked"}),
                                 (b'{"a":NaN}', {}), (b'{"a":1e999}', {}), (b'{"a":1,"a":2}', {}), (b'[]', {})):
                self.assertEqual(self.request(route, "POST", raw, headers)[0], 400)
            self.assertEqual(self.request(route, "POST", b" " * 16385)[0], 413)
        # Legacy endpoints keep their established 400 response on body overflow.
        self.assertEqual(self.request("/api/workspace/tasks", "POST", b" " * 4097)[0], 400)

    def test_project_manual_history_tasks_todos_and_query_validation(self):
        status, response = self.request("/api/workspace/projects/save", "POST", project_body(self.root / "project"))
        self.assertEqual(status, 200)
        identity = response["project"]["id"]
        self.assertFalse(self.request("/api/profile")[1]["can_import"])
        self.assertEqual(self.request("/api/profile")[1]["import_reason"], "profile_not_empty")
        manual = {"project_id": identity, "state": "在做", "percent": None, "note": "Note"}
        self.assertEqual(self.request("/api/workspace/progress", "POST", manual)[0], 200)
        self.assertIsNone(self.request("/api/progress")[1]["projects"][0]["percent"])
        self.assertEqual(self.request("/api/progress/set", "POST", {**manual, "percent": 0})[1]["projects"][0]["percent"], 0)
        self.assertEqual(self.request("/api/workspace/progress/history?project_id=" + identity)[1]["total"], 2)
        task = {"title": "Task", "project_id": identity, "provider_id": None, "state": "todo", "session_key": None}
        self.assertEqual(self.request("/api/workspace/tasks", "POST", task)[0], 200)
        self.assertEqual(self.request("/api/workspace/tasks?scope=active&limit=1")[1]["total"], 1)
        todo = self.request("/api/todos", "POST", {"text": "Todo", "project_id": identity})[1]["todos"][0]
        self.assertTrue(self.request("/api/todos/toggle", "POST", {"id": todo["id"]})[1]["todos"][0]["done"])
        for route in ("/api/workspace/tasks?limit=0", "/api/workspace/tasks?limit=101", "/api/workspace/tasks?offset=-1",
                      "/api/workspace/tasks?scope=bogus", "/api/workspace/tasks?limit=1&limit=2", "/api/workspace/tasks?unknown=x",
                      "/api/workspace/compatibility?kind=bogus", "/api/workspace/progress/history"):
            self.assertEqual(self.request(route)[0], 400)
        self.assertEqual(self.request("/api/workspace/progress/history?project_id=missing")[0], 404)

    def test_same_explicit_codex_path_in_all_routes_and_disable_rereads(self):
        database = self.root / "threads.sqlite"
        key = str(uuid4())
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE threads(id TEXT,cwd TEXT,created_at INTEGER,updated_at INTEGER,archived INTEGER,source TEXT)")
            connection.execute("INSERT INTO threads VALUES(?,?,1,2,0,'cli')", (key, str(self.root / "project")))
            connection.commit()
        self.profile.store.save_project(project_body(self.root / "project"))
        self.assertEqual(self.request("/api/profile/settings", "POST", {"codex": {"enabled": True, "db_path": str(database)}})[0], 200)
        from session_panel.sessions import codex_index
        original = codex_index.list_threads
        with patch.object(codex_index, "list_threads", wraps=original) as reader:
            self.assertEqual(self.request("/api/workspace")[1]["sessions"]["items"][0]["thread_id"], key)
            self.assertEqual(self.request("/api/progress")[1]["projects"][0]["sessions"][0]["session_key"], "codex:" + key)
            self.assertEqual(self.request("/api/codex/threads")[1]["items"][0]["thread_id"], key)
            self.assertTrue(all(call.kwargs["db_path"] == database for call in reader.call_args_list))
        self.profile.settings({"codex": {"enabled": False, "db_path": str(database)}})
        with patch.object(codex_index, "list_threads", side_effect=AssertionError("disabled")):
            for route in ("/api/workspace", "/api/progress", "/api/codex/threads", "/api/profile"):
                self.request(route)
