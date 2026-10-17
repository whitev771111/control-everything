import http.client
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from control_everything.http.gateway import make_server
from control_everything.integrations.launchers import start
from control_everything.workspace.progress import REGISTRY, add_todo, board, set_progress, toggle_todo


class ProgressBoardTests(unittest.TestCase):
    def setUp(self):
        for name in ("codex_cwds", "grok_cwds"):
            fixture = patch("control_everything.workspace.progress." + name, return_value=[])
            fixture.start()
            self.addCleanup(fixture.stop)

    def files(self, directory):
        root = Path(directory)
        registry = root / "registry.json"
        manual = root / "manual.json"
        registry.write_text(json.dumps({"projects": [
            {"id": "biji", "name": "笔记", "root": str(root / "first")},
            {"id": "n8n-for-me", "name": "n8n", "root": str(root / "second")},
        ]}), encoding="utf-8")
        return registry, manual

    def test_default_board_is_quiet_and_manual_state_persists(self):
        with TemporaryDirectory() as directory:
            registry, manual = self.files(directory)
            self.assertEqual([(item["name"], item["state"], item["note"]) for item in board(registry, manual)],
                             [("笔记", "未开始", ""), ("n8n", "未开始", "")])
            self.assertTrue(set_progress("biji", "在做", "  标签提醒  ", 40, registry, manual))
            self.assertEqual(board(registry, manual)[0]["state"], "在做")
            self.assertEqual(board(registry, manual)[0]["note"], "标签提醒")
            self.assertEqual(board(registry, manual)[0]["percent"], 40)
            self.assertFalse(set_progress("missing", "在做", "", 10, registry, manual))
            with self.assertRaises(ValueError):
                set_progress("biji", "完成百分之八十", "", 10, registry, manual)
            with self.assertRaises(ValueError):
                set_progress("biji", "在做", "字" * 25, 10, registry, manual)
            with self.assertRaises(ValueError):
                set_progress("biji", "在做", "", 120, registry, manual)

    def test_focus_is_the_newest_open_record(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            feature = root / "docs" / "current" / "note-tags"
            feature.mkdir(parents=True)
            readme = feature / "README.md"
            readme.write_text("当前阶段：标签提醒还在做\n", encoding="utf-8")
            done = root / "docs" / "current" / "old"
            done.mkdir()
            (done / "README.md").write_text("当前阶段：已收口\n", encoding="utf-8")
            registry = root / "registry.json"
            manual = root / "manual.json"
            registry.write_text(json.dumps({"projects": [
                {"id": "sample", "name": "样例", "root": str(root)},
            ]}), encoding="utf-8")
            focus = board(registry, manual)[0]["records"][0]
            self.assertEqual(focus["feature"], "note-tags")
            self.assertIn("标签提醒还在做", focus["text"])

    def test_todo_can_be_added_and_toggled(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry, manual = self.files(directory)
            path = root / "todos.json"
            items = add_todo("看一眼笔记", "biji", path, registry)
            self.assertEqual(items[0]["text"], "看一眼笔记")
            self.assertFalse(items[0]["done"])
            toggled = toggle_todo(items[0]["id"], path)
            self.assertTrue(toggled[0]["done"])
            self.assertIsNone(add_todo("不属于", "missing", path, registry))

    def test_unknown_launcher_is_refused(self):
        self.assertFalse(start("not-a-launcher", spawn=lambda *args, **kwargs: None))

    def test_registered_names_stay_in_registry_order(self):
        with TemporaryDirectory() as directory:
            registry, manual = self.files(directory)
            projects = board(registry, manual)
            self.assertEqual([item["name"] for item in projects], ["笔记", "n8n"])
            self.assertTrue(all(item["state"] in {"未开始", "在做", "等我", "完成"} for item in projects))
            self.assertNotIn("\"path\"", json.dumps(projects))

    def test_page_sets_progress_and_requires_csrf(self):
        with TemporaryDirectory() as directory:
            registry, manual = self.files(directory)
            server = make_server(Mock(), ("127.0.0.1", 0))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch("control_everything.http.gateway.REGISTRY", registry), patch("control_everything.http.gateway.MANUAL", manual), patch("control_everything.http.gateway.TODOS", Path(directory) / "todos.json"):
                    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                    connection.request("GET", "/progress")
                    response = connection.getresponse()
                    page = response.read()
                    self.assertEqual(response.status, 200)
                    self.assertIn("工作台".encode(), page)
                    self.assertNotIn("当前阶段".encode(), page)
                    connection.close()
                    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                    connection.request("POST", "/api/progress/set",
                                       body=json.dumps({"project_id": "biji", "state": "等我", "note": "待办", "percent": 60}),
                                       headers={"X-Session-Client": "panel", "Content-Type": "application/json",
                                                "Origin": server.origin, "X-Session-CSRF": server.csrf_token})
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload["projects"][0]["state"], "等我")
                    self.assertEqual(payload["projects"][0]["percent"], 60)
                    connection.close()
                    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                    connection.request("POST", "/api/progress/set",
                                       body=json.dumps({"project_id": "biji", "state": "完成", "note": "", "percent": 100}),
                                       headers={"X-Session-Client": "panel", "Content-Type": "application/json"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 403)
                    response.read()
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()




class ExplicitCodexProgressTests(unittest.TestCase):
    def test_absent_codex_path_never_probes_database(self):
        from control_everything.workspace.progress import codex_cwds
        with patch("control_everything.workspace.progress.sqlite3.connect") as connect, patch.object(Path, "resolve") as resolve:
            self.assertEqual(codex_cwds(), [])
        connect.assert_not_called(); resolve.assert_not_called()

    def test_explicit_synthetic_codex_path_reaches_board(self):
        import sqlite3
        from contextlib import closing
        from control_everything.workspace.progress import codex_cwds
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            db = root / "codex.sqlite"
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute("CREATE TABLE threads(id,cwd,archived,updated_at)")
                connection.execute("INSERT INTO threads VALUES(?,?,0,1)", ("fixture-thread", str(root)))
            self.assertEqual(codex_cwds(db), [{"thread_id": "fixture-thread", "cwd": str(root)}])
            registry = root / "registry.json"
            registry.write_text(json.dumps({"projects": [{"id": "example", "name": "Example", "root": str(root)}]}), encoding="utf-8")
            with patch("control_everything.workspace.progress.grok_cwds", return_value=[]):
                result = board(registry, root / "manual.json", codex_db_path=db)
            self.assertEqual(result[0]["sessions"][0]["tool"], "Codex")


class RepositoryPathsTests(unittest.TestCase):
    def test_data_and_resource_paths_stay_at_repository_root(self):
        from control_everything.paths import REPOSITORY_ROOT
        from control_everything.http.gateway import WEB
        from control_everything.workspace.progress import MANUAL, TODOS
        from control_everything.workspace.service import DEFAULT_DB

        root = Path(__file__).resolve().parents[2]
        self.assertEqual(REPOSITORY_ROOT, root)
        self.assertEqual(WEB, root / "web")
        self.assertEqual(REGISTRY, root / "progress-registry.json")
        self.assertEqual(MANUAL, root / "progress-manual.json")
        self.assertEqual(TODOS, root / "progress-todos.json")
        self.assertEqual(DEFAULT_DB, root / "workspace-state.sqlite3")


if __name__ == "__main__":
    unittest.main()
