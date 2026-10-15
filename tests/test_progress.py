import http.client
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from session_panel.gateway import make_server
from session_panel.launchers import start
from session_panel.progress import REGISTRY, add_todo, board, set_progress, toggle_todo


class ProgressBoardTests(unittest.TestCase):
    def files(self, directory):
        root = Path(directory)
        registry = root / "registry.json"
        manual = root / "manual.json"
        registry.write_text(json.dumps({"projects": [
            {"id": "biji", "name": "笔记", "root": "E:/project-funny/biji"},
            {"id": "n8n-for-me", "name": "n8n", "root": "E:/project-funny/n8n-for-me"},
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
        projects = board(REGISTRY)
        self.assertEqual([item["name"] for item in projects],
                         ["笔记", "工作面板", "n8n", "运维智能体", "终端工作台", "开发工作流"])
        self.assertTrue(all(item["state"] in {"未开始", "在做", "等我", "完成"} for item in projects))
        self.assertNotIn("\"path\"", json.dumps(projects))

    def test_page_sets_progress_and_requires_csrf(self):
        with TemporaryDirectory() as directory:
            registry, manual = self.files(directory)
            server = make_server(Mock(), ("127.0.0.1", 0))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch("session_panel.gateway.REGISTRY", registry), patch("session_panel.gateway.MANUAL", manual):
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


if __name__ == "__main__":
    unittest.main()
