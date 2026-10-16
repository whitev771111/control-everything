import copy
import http.client
import json
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from session_panel.sessions.codex_index import unavailable
from session_panel.http.gateway import make_server
from session_panel.workspace.service import WorkspaceService, discover_providers, inside_project
from session_panel.workspace.store import WorkspaceError, WorkspaceStore

SESSION_ID = "b7ed3430-9064-4153-894d-d295a388a572"
MISSING_ID = "6ee281b5-e4f7-43ce-b1b7-92da287e5809"
TASK = {"title": "核对工作台", "project_id": "sample", "provider_id": None, "state": "todo", "session_key": None}
RECORD = {"id": None, "provider_id": "grok", "product": "subscription", "day": "2026-10-01",
          "metric": "quota_used_percent", "value": 12.5, "currency": None, "window_label": "本周", "note": "人工观察"}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry, self.manual, self.todos = (self.root / name for name in ("registry.json", "manual.json", "todos.json"))
        self.registry.write_text(json.dumps({"projects": [{"id": "sample", "name": "样例", "root": str(self.root)}]}), encoding="utf-8")
        self.sample = {**unavailable(0), "available": True, "reason": None, "total_visible": 30, "next_offset": 12,
                       "items": [{"session_key": "codex:" + SESSION_ID, "thread_id": SESSION_ID,
                                  "cwd": str(self.root / "child"), "activity_state": "unknown", "task_completion": "unknown"}]}
        self.reader = Mock(side_effect=lambda: copy.deepcopy(self.sample))
        self.quota, self.news = Mock(), Mock()
        self.quota.get_cached.return_value = {"status": "not_checked", "buckets": []}
        self.quota.refresh.return_value = {"status": "unavailable", "reason": "rpc_error", "error_code": -32603, "buckets": []}
        self.news.get.return_value = {"status": "unavailable", "items": [], "sources": []}
        self.service = WorkspaceService(self.root / "state.sqlite3", registry_path=self.registry,
                                        manual_path=self.manual, todos_path=self.todos, session_reader=self.reader,
                                        quota_source=self.quota, news_source=self.news)
        for name, value in (("codex_cwds", []), ("grok_cwds", [])):
            patcher = patch("session_panel.workspace.service.progress." + name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("session_panel.workspace.service.shutil.which", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_snapshot_keeps_missing_progress_unknown_and_index_envelope(self):
        result = self.service.snapshot()
        project = result["projects"]["items"][0]
        self.assertEqual((project["manual_recorded"], project["percent"], project["manual_reason"]), (False, None, "not_recorded"))
        self.assertEqual((project["records"], project["records_available"]), ([], False))
        self.assertEqual(result["sessions"], self.sample)
        self.assertEqual((result["tasks"]["total"], result["tasks"]["items"]), (0, []))
        self.assertEqual(self.quota.mock_calls, [])
        self.assertEqual(self.news.mock_calls, [])

    def test_manual_zero_invalid_and_unavailable_are_distinct(self):
        for value, recorded, reason in (({"state": "在做", "percent": 0}, True, None),
                                         ({"state": "在做", "percent": True}, False, "invalid_record"),
                                         ({"state": "完成", "percent": float("nan")}, False, "invalid_record"),
                                         ({"state": "unexpected", "percent": 1}, False, "invalid_record"),
                                         ([], False, "invalid_record")):
            with self.subTest(value=value):
                self.manual.write_text(json.dumps({"sample": value}), encoding="utf-8")
                project = self.service.snapshot()["projects"]["items"][0]
                self.assertEqual((project["manual_recorded"], project["manual_reason"]), (recorded, reason))
                self.assertEqual(project["percent"], 0 if recorded else None)
        self.manual.write_text("{ broken", encoding="utf-8")
        result = self.service.snapshot()
        self.assertEqual(result["projects"]["status"], "available")
        self.assertEqual(result["projects"]["items"][0]["manual_reason"], "source_unavailable")

    def test_sources_fail_independently_and_do_not_overwrite_legacy_files(self):
        self.manual.write_text('{"sample":{"state":"在做","percent":25}}', encoding="utf-8")
        self.todos.write_text("not-json", encoding="utf-8")
        original = {path: path.read_bytes() for path in (self.registry, self.manual, self.todos)}
        self.reader.side_effect = OSError("private")
        result = self.service.snapshot()
        self.assertEqual((result["projects"]["status"], result["todos"]["status"], result["sessions"]["available"]),
                         ("available", "unavailable", False))
        self.service.save_task(TASK)
        self.service.save_record(RECORD)
        self.assertEqual(original, {path: path.read_bytes() for path in original})
        self.registry.write_text("broken", encoding="utf-8")
        self.assertEqual(self.service.snapshot()["projects"]["status"], "unavailable")

    def test_create_update_and_reopen_store(self):
        task = self.service.save_task({**TASK, "title": "  核对工作台  "})["task"]
        self.assertEqual((task["title"], task["source"], task["session_link_state"]), (TASK["title"], "manual", "none"))
        updated = self.service.save_task({**TASK, "id": task["id"], "state": "done", "provider_id": "dsh"}, update=True)["task"]
        reopened = WorkspaceStore(self.root / "state.sqlite3").get_task(task["id"])
        self.assertEqual((updated["state"], reopened["state"], reopened["provider_id"]), ("done", "done", "dsh"))
        self.assertEqual(task["created_at"], updated["created_at"])
        with self.assertRaisesRegex(WorkspaceError, "task_not_found"):
            self.service.save_task({**TASK, "id": MISSING_ID}, update=True)
        self.assertEqual(self.service.store.list_tasks()[1], 1)

    def test_invalid_tasks_never_write(self):
        bad_values = [({"title": " "}), ({"title": "x" * 161}), ({"project_id": "missing"}),
                      ({"provider_id": "agent"}), ({"state": "finished"}), ({"state": []}),
                      ({"session_key": "codex:" + SESSION_ID}), ({"source": "automatic"}),
                      ({"id": MISSING_ID}), ({"provider_id": "codex", "session_key": "codex:bad"})]
        for change in bad_values:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.service.save_task({**TASK, **change})
        self.assertEqual(self.service.store.list_tasks(), ([], 0))

    def test_session_association_requires_current_sample_and_project_containment(self):
        body = {**TASK, "provider_id": "codex", "session_key": "codex:" + SESSION_ID}
        task = self.service.save_task(body)["task"]
        self.assertEqual(task["session_link_state"], "verified")
        self.sample["items"] = []
        stale = self.service.save_task({**body, "id": task["id"], "state": "review"}, update=True)["task"]
        self.assertEqual((stale["state"], stale["session_link_state"]), ("review", "not_in_current_sample"))
        with self.assertRaisesRegex(WorkspaceError, "session_not_in_sample"):
            self.service.save_task(body)
        self.sample["items"] = [{"session_key": body["session_key"], "cwd": str(self.root) + "-other"}]
        self.assertEqual(self.service.snapshot()["tasks"]["items"][0]["session_link_state"], "project_mismatch")
        with self.assertRaisesRegex(WorkspaceError, "session_project_mismatch"):
            self.service.save_task(body)

    def test_windows_containment_is_normalized_and_not_a_prefix(self):
        self.assertTrue(inside_project("c:/example/PROJECT/child", r"C:\example\project"))
        self.assertTrue(inside_project("C:/example/project/child/..", "C:/example/project"))
        for path in ("C:/example/project-other", "C:/example/project/../else", "D:/example/project", "project/child"):
            self.assertFalse(inside_project(path, "C:/example/project"))

    def test_windows_extended_paths_match_drive_and_unc_aliases(self):
        self.assertTrue(inside_project(r"\\?\C:\example\panel", "C:/example/panel"))
        self.assertTrue(inside_project(r"\\?\C:\example\project\child", "C:/example/project"))
        self.assertTrue(inside_project("C:/example/project/child", r"\\?\C:\example\project"))
        self.assertTrue(inside_project(r"\\?\UNC\server\share\project\child", r"\\server\share\project"))
        self.assertFalse(inside_project(r"\\?\C:\example\project-other", "C:/example/project"))
        self.assertFalse(inside_project(r"\\?\UNC\server\share\project-other", r"\\server\share\project"))
        body = {**TASK, "provider_id": "codex", "session_key": "codex:" + SESSION_ID}
        self.sample["items"][0]["cwd"] = "\\\\?\\" + str(self.root / "child")
        self.assertEqual(self.service.save_task(body)["task"]["session_link_state"], "verified")

    def test_record_create_update_reopen_and_no_automatic_sum(self):
        first = self.service.save_record(RECORD)["record"]
        record = self.service.save_record({**RECORD, "id": first["id"], "value": 15})["record"]
        self.service.save_record({**RECORD, "provider_id": "codex", "value": 30})
        rows, total = WorkspaceStore(self.root / "state.sqlite3").list_records()
        self.assertEqual((total, len(rows), record["value"], record["source"]), (2, 2, 15, "manual"))
        self.assertEqual(record["created_at"], first["created_at"])
        usage = self.service.usage()
        self.assertEqual((usage["manual_total"], usage["codex"]["status"]), (2, "not_checked"))
        self.quota.refresh.assert_not_called()
        with self.assertRaisesRegex(WorkspaceError, "record_not_found"):
            self.service.save_record({**RECORD, "id": MISSING_ID})

    def test_record_validation_and_valid_product_metric_combinations(self):
        bad = [{"id": "bad"}, {"product": "local"}, {"day": "2026-02-30"}, {"day": "2999-01-01"},
               {"day": "2026-1-01"}, {"value": True}, {"value": float("nan")}, {"value": float("inf")},
               {"value": -1}, {"value": 101}, {"currency": "USD"}, {"window_label": " "},
               {"window_label": "x" * 25}, {"note": "x" * 121}, {"unknown": 1},
               {"metric": "daily_tokens", "value": 1.2, "window_label": None},
               {"metric": "daily_tokens", "value": 10**30, "window_label": None},
               {"metric": "daily_tokens", "value": 9223372036854775808, "window_label": None},
               {"metric": "daily_cost", "currency": "GBP", "window_label": None}]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.service.save_record({**RECORD, **change})
        self.assertEqual(self.service.store.list_records()[1], 0)
        for provider, product in (("grok", "cli"), ("grok", "api"), ("claude", "subscription"),
                                  ("dsh", "local"), ("opencode", "local")):
            self.service.save_record({**RECORD, "provider_id": provider, "product": product,
                                      "metric": "daily_tokens", "window_label": None, "value": 100})
        self.service.save_record({**RECORD, "metric": "daily_cost", "currency": "CNY", "window_label": None, "value": 1.5})
        self.assertEqual(self.service.store.list_records()[1], 6)

    def test_numeric_storage_boundaries_remain_explicit(self):
        tokens = self.service.save_record({**RECORD, "metric": "daily_tokens", "window_label": None,
                                           "value": 9223372036854775807})["record"]
        self.assertEqual(tokens["value"], 9223372036854775807)
        cost = self.service.save_record({**RECORD, "metric": "daily_cost", "window_label": None,
                                         "currency": "USD", "value": 10**30})["record"]
        self.assertEqual(cost["value"], 1e30)

    def test_concurrent_operations_use_independent_connections(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            tasks = list(pool.map(lambda index: self.service.save_task({**TASK, "title": str(index)})["task"], range(24)))
        self.assertEqual(len({row["id"] for row in tasks}), 24)
        self.assertEqual(self.service.store.list_tasks()[1], 24)

    def test_list_limits_report_total(self):
        for table, count, fields in (("tasks", 501, TASK), ("manual_records", 101, RECORD)):
            with self.service.store.connection() as connection:
                for index in range(count):
                    if table == "tasks":
                        connection.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,'manual',?,?)",
                                           (str(index), "sample", "sample", None, "todo", None, "now", "now"))
                    else:
                        connection.execute("INSERT INTO manual_records VALUES (?,?,?,?,?,?,?,?,?,'manual',?,?)",
                                           (str(index), "codex", "subscription", "2026-10-01", "daily_tokens", 1, None, None, "", "now", "now"))
        result, usage = self.service.snapshot(), self.service.usage()
        self.assertEqual((len(result["tasks"]["items"]), result["tasks"]["total"]), (500, 501))
        self.assertEqual((len(usage["manual_records"]), usage["manual_total"]), (100, 101))

    def test_corrupt_storage_is_not_recreated_and_does_not_hide_other_sources(self):
        self.service.store.path.write_bytes(b"corrupt fixture")
        result = self.service.snapshot()
        self.assertEqual((result["projects"]["status"], result["tasks"]["reason"]), ("available", "storage_unavailable"))
        self.assertEqual(self.service.store.path.read_bytes(), b"corrupt fixture")
        self.assertEqual(self.service.usage()["manual_status"], "unavailable")
        with self.assertRaisesRegex(WorkspaceError, "storage_unavailable"):
            self.service.save_task(TASK)

    def test_busy_storage_returns_failure_without_successful_write(self):
        self.service.store.list_tasks()
        blocker = sqlite3.connect(self.service.store.path)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(WorkspaceError, "storage_unavailable"):
                self.service.save_task(TASK)
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(self.service.store.list_tasks()[1], 0)

    def test_provider_discovery_is_separate_from_capabilities(self):
        with patch("session_panel.workspace.service.shutil.which", side_effect=lambda command: "fixture" if command in ("dsh-tui", "grok") else None):
            providers = {row["id"]: row for row in discover_providers()}
        self.assertEqual(providers["dsh"]["discovery"]["commands"], ["dsh-tui"])
        self.assertEqual(providers["codex"]["discovery"]["status"], "not_found")
        self.assertTrue(providers["codex"]["capabilities"]["read_quota"])
        self.assertTrue(all(not row["capabilities"]["dispatch"] for row in providers.values()))


class WorkspaceGatewayTests(unittest.TestCase):
    def setUp(self):
        WorkspaceTests.setUp(self)
        self.server = make_server(Mock(), ("127.0.0.1", 0), workspace=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        supplied = {"X-Session-Client": "panel"}
        if method == "POST":
            supplied.update({"Content-Type": "application/json", "Origin": self.server.origin,
                             "X-Session-CSRF": self.server.csrf_token})
        supplied.update(headers or {})
        try:
            connection.request(method, path, body=json.dumps(body) if not isinstance(body, str) and body is not None else body,
                               headers=supplied)
            response = connection.getresponse()
            data = response.read()
            return response.status, json.loads(data) if "application/json" in response.getheader("Content-Type") else data
        finally:
            connection.close()

    def test_http_all_workspace_routes(self):
        self.assertEqual(self.request("/api/workspace")[0], 200)
        status, payload = self.request("/api/workspace/tasks", "POST", TASK)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/api/workspace/tasks/update", "POST", {**TASK, "id": payload["task"]["id"], "state": "done"})[0], 200)
        self.assertEqual(self.request("/api/workspace/records", "POST", RECORD)[0], 200)
        self.assertEqual(self.request("/api/workspace/usage")[1]["codex"]["status"], "not_checked")
        self.assertEqual(self.request("/api/workspace/usage/refresh", "POST", {})[1]["codex"]["error_code"], -32603)
        self.assertEqual(self.request("/api/workspace/news")[1]["status"], "unavailable")
        self.quota.refresh.assert_called_once_with()

    def test_http_auth_body_and_path_boundaries(self):
        for headers, reason in (({"Host": "evil.test"}, "host_rejected"),
                                ({"Origin": "https://evil.test"}, "origin_rejected"),
                                ({"X-Session-Client": ""}, "client_header_required"),
                                ({"X-Session-CSRF": ""}, "csrf_rejected")):
            status, payload = self.request("/api/workspace/tasks", "POST", TASK, headers)
            self.assertEqual((status, payload["reason"]), (403, reason))
        for body in ("[]", "null", "NaN", '{"title":NaN}', "{" , " " * 4097, {**TASK, "extra": 1}):
            self.assertEqual(self.request("/api/workspace/tasks", "POST", body)[0], 400)
        self.assertEqual(self.request("/api/workspace?refresh=1")[0], 400)
        self.assertEqual(self.request("/api/workspace/usage/refresh", "POST", {"force": True})[0], 400)
        self.assertEqual(self.request("/api/workspace/tasks/unknown", "POST", TASK)[0], 404)
        self.assertEqual(self.service.store.list_tasks()[1], 0)

    def test_http_missing_storage_and_link_errors(self):
        self.assertEqual(self.request("/api/workspace/tasks/update", "POST", {**TASK, "id": MISSING_ID})[0], 404)
        self.assertEqual(self.request("/api/workspace/records", "POST", {**RECORD, "id": MISSING_ID})[0], 404)
        body = {**TASK, "provider_id": "codex", "session_key": "codex:" + MISSING_ID}
        self.assertEqual(self.request("/api/workspace/tasks", "POST", body)[0], 409)
        self.server.workspace = None
        for path in ("/api/workspace", "/api/workspace/usage", "/api/workspace/news"):
            self.assertEqual(self.request(path), (503, unittest.mock.ANY))
        self.assertEqual(self.request("/api/workspace/tasks", "POST", TASK)[0], 503)
        self.assertEqual(self.request("/health")[0], 200)

    def test_http_static_routes_are_exact(self):
        with TemporaryDirectory() as directory, patch("session_panel.http.gateway.WEB", Path(directory)):
            for route, name in (("/workspace", "workspace.html"), ("/workspace.js", "workspace.js"), ("/workspace.css", "workspace.css")):
                (Path(directory) / "workspace").mkdir(exist_ok=True)
                (Path(directory) / "workspace" / name).write_bytes(b"fixture")
                self.assertEqual(self.request(route), (200, b"fixture"))
                self.assertEqual(self.request(route + "?v=1")[0], 404)
        self.assertEqual(self.request("/workspace/../workspace.js")[0], 404)


if __name__ == "__main__":
    unittest.main()
