"""Actual CLI/profile/server objects with synthetic platform boundaries only."""

import copy
import hashlib
import http.client
import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from session_panel.http.gateway import build_profile_server
from session_panel.profiles.profile import Profile
from session_panel.sessions.service import SessionService
from session_panel.sessions.terminal import Terminal
from session_panel.sessions.workbench import Workbench
from session_panel.integrations.launchers import LauncherCatalog
from tests.fixtures.integration_config import integration_config
from tests.backend.test_observation import MAIN, registration
from tests.backend.test_workbench import process, GUI_TIME, CHILD_TIME


class ConfiguredIntegrationsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        headlines = patch("session_panel.http.gateway.headlines", return_value=[])
        headlines.start(); self.addCleanup(headlines.stop)

    def import_config(self, name="one", *, host="example.invalid", enabled=True):
        directory = self.root / name
        settings = integration_config()["integrations"] if enabled else dict.fromkeys(("remote", "terminal", "workbench", "launchers"))
        if enabled:
            settings["remote"]["source"].update(id=name, host=host)
            tools = self.root / (name + "-tools"); tools.mkdir()
            for role, filename in (("zellij", "zellij.exe"), ("wezterm_gui", "wezterm-gui.exe"),
                                   ("wezterm_lua", "wezterm.lua"), ("zellij_config", "zellij.kdl")):
                path = tools / filename; path.write_bytes(("synthetic-" + role).encode())
                section = settings["terminal"] if role == "zellij" else settings["workbench"]
                section[role] = path.as_posix()
                settings["workbench"]["pins"][role] = hashlib.sha256(path.read_bytes()).hexdigest()
            settings["workbench"]["workdir"] = tools.as_posix()
            settings["workbench"]["unmanaged_mux"] = (tools / "wezterm-mux-server.exe").as_posix()
            website = settings["launchers"]["items"][-1]
            website["probe"] = ""; website["target"] = "https://example.invalid/" + name
            settings["launchers"]["items"] = [website]
        document = {"version": 2, "integrations": settings}
        file = self.root / (name + ".json"); file.write_text(json.dumps(document), encoding="utf-8")
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", "session_panel.profile", "import-settings", "--file", str(file),
                                 "--data-dir", str(directory)], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[0])["restart_required"])
        return directory, settings

    def server(self, directory):
        server = build_profile_server(str(directory), 0, quota_source=Mock(), news_source=Mock())
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def close():
            server.shutdown(); server.server_close(); thread.join(3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(close)
        return server

    def request(self, server, route, body=None, headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        supplied = {"X-Session-Client": "panel"}
        if body is not None:
            supplied.update({"Content-Type": "application/json", "Origin": server.origin,
                             "X-Session-CSRF": server.csrf_token})
        supplied.update(headers or {})
        try:
            connection.request("POST" if body is not None else "GET", route,
                               body=json.dumps(body) if body is not None else None, headers=supplied)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def platform(self, server):
        service = server.service; workbench = service.terminal.workbench
        source = copy.deepcopy(service.source)
        remote_calls, terminal_calls, workbench_calls = [], [], []
        def remote(argv, **kwargs):
            remote_calls.append((argv, kwargs))
            if argv[-1] == "python3 -":
                value = {"ok": True, "source": source, "registrations": [registration()],
                         "registration_count": 1, "process_count": 1}
            else:
                value = {"available": True, "source": source, "items": [{"message": "synthetic message"}],
                         "total_visible": 1, "next_offset": None}
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
        pane = {"id": 2, "is_plugin": False, "tab_name": service.terminal.config["tab"],
                "terminal_command": f"ssh.exe -t {source['host']} bash {service.terminal.config['pane_script']} main"}
        def terminal(argv, **kwargs):
            terminal_calls.append(argv)
            text = "zellij 0.45.1" if argv[-1] == "--version" else json.dumps([pane])
            return subprocess.CompletedProcess(argv, 0, text, "")
        def run_workbench(argv, **kwargs):
            workbench_calls.append(argv)
            text = ("zellij 0.45.1" if argv[-1] == "--version" else
                    f"{workbench.session} [Created 1day ago]" if "list-sessions" in argv else
                    "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_2 ssh.exe\n")
            return subprocess.CompletedProcess(argv, 0, text, "")
        marker = self.root / (source["id"] + "-marker"); marker.write_text("10", encoding="ascii")
        workbench.marker = marker.as_posix()
        rows = [process(10, 1, [workbench.zellij, "--server", workbench.marker]),
                process(20, 3, workbench.new_gui, GUI_TIME),
                process(30, 20, [workbench.zellij, "attach"], CHILD_TIME)]
        service.remote.runner = remote
        service.terminal.runner = terminal
        workbench.runner = run_workbench
        workbench.provider = lambda _deadline: copy.deepcopy(rows)
        workbench.popen = Mock(side_effect=AssertionError("already connected; must not spawn"))
        return remote_calls, terminal_calls, workbench_calls

    def test_cli_import_reaches_actual_services_and_published_http_routes(self):
        directory, _ = self.import_config()
        server = self.server(directory)
        self.assertIsInstance(server.service, SessionService)
        self.assertIsInstance(server.service.terminal, Terminal)
        self.assertIsInstance(server.service.terminal.workbench, Workbench)
        self.assertIsInstance(server.launchers, LauncherCatalog)
        self.assertIsNone(server.execution)
        calls = self.platform(server)
        boot = self.request(server, "/api/bootstrap")[1]
        self.assertTrue(all(boot["capabilities"][key] for key in ("remote_sessions", "terminal", "workbench", "personal_launchers")))
        self.assertFalse(boot["capabilities"]["execution"])
        row = self.request(server, "/api/sessions")[1]["sessions"][0]
        self.assertEqual(row["terminal_binding"], "confirmed")
        self.assertEqual(row["source"], server.source)
        self.assertTrue(self.request(server, f"/api/sessions/{MAIN}/messages")[1]["available"])
        self.assertTrue(self.request(server, "/api/workbench")[1]["can_focus"])
        self.assertTrue(self.request(server, f"/api/sessions/{MAIN}/focus", {})[1]["focused"])
        self.assertTrue(self.request(server, "/api/workbench/connect", {})[1]["connected"])
        with patch("session_panel.integrations.launchers.os.startfile") as launch:
            self.assertEqual(self.request(server, "/api/launch", {"id": "website"})[0], 200)
            launch.assert_called_once_with("https://example.invalid/one")
        self.assertTrue(all(calls))
        self.assertTrue(any("focus-pane-id" in argv for argv in calls[1]))
        self.assertTrue(all("StrictHostKeyChecking=yes" in argv for argv, _ in calls[0]))
        public = self.request(server, "/api/profile")[1]
        desk = self.request(server, "/api/desk")[1]
        combined = json.dumps([public, desk])
        for forbidden in ("reader_python", "wezterm_gui", "pins", "target", "argv"):
            self.assertNotIn(forbidden, combined)
        self.assertFalse(public["restart_required"])

    def test_empty_profile_never_constructs_or_queries_optional_integrations(self):
        targets = ["session_panel.sessions.service.RemoteClient", "session_panel.sessions.service.SessionService",
                   "session_panel.sessions.terminal.Terminal", "session_panel.sessions.workbench.Workbench",
                   "session_panel.http.gateway.LauncherCatalog"]
        for target in targets:
            item = patch(target, side_effect=AssertionError("unconfigured")); item.start(); self.addCleanup(item.stop)
        server = self.server(self.root / "empty")
        boot = self.request(server, "/api/bootstrap")[1]
        self.assertFalse(any(boot["capabilities"][key] for key in ("remote_sessions", "terminal", "workbench", "personal_launchers")))
        for route in ("/api/sessions", "/api/workbench", f"/api/sessions/{MAIN}/messages"):
            self.assertEqual(self.request(server, route)[1]["reason"], "capability_unavailable")
        for route, body in (("/api/workbench/connect", {}), (f"/api/sessions/{MAIN}/focus", {}), ("/api/launch", {"id": "website"})):
            self.assertEqual(self.request(server, route, body)[1]["reason"], "capability_unavailable")

    def test_remote_only_and_launcher_only_capabilities_are_independent(self):
        for kind in ("remote", "launchers"):
            directory, settings = self.import_config(kind)
            profile = Profile.open(directory)
            profile.import_settings({"version": 2, "integrations": {
                name: value if name == kind else None for name, value in settings.items()}})
            server = self.server(directory)
            boot = self.request(server, "/api/bootstrap")[1]["capabilities"]
            self.assertEqual(boot["remote_sessions"], kind == "remote")
            self.assertEqual(boot["personal_launchers"], kind == "launchers")
            self.assertFalse(boot["terminal"]); self.assertFalse(boot["workbench"])
            if kind == "remote":
                server.service.remote.runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
                    "source": server.source, "available": True, "items": []}), ""))
                self.assertTrue(self.request(server, f"/api/sessions/{MAIN}/messages")[1]["available"])
                self.assertIsNone(server.service.terminal.workbench)
            else:
                self.assertIsNone(server.service)
                with patch("session_panel.integrations.launchers.os.startfile") as launch:
                    self.assertEqual(self.request(server, "/api/launch", {"id": "website"})[0], 200)
                launch.assert_called_once_with("https://example.invalid/launchers")

    def test_instances_and_restart_snapshot_do_not_share_configuration(self):
        one_dir, one_settings = self.import_config()
        two_dir, _ = self.import_config("two", host="second.invalid")
        one, two = self.server(one_dir), self.server(two_dir)
        self.platform(one); self.platform(two)
        self.assertEqual(self.request(one, "/api/sessions")[1]["source"]["host"], "example.invalid")
        self.assertEqual(self.request(two, "/api/sessions")[1]["source"]["host"], "second.invalid")
        one_settings["remote"]["source"]["host"] = "changed.invalid"
        Profile.open(one_dir).import_settings({"version": 2, "integrations": one_settings})
        self.assertTrue(self.request(one, "/api/profile")[1]["restart_required"])
        self.assertFalse(self.request(two, "/api/profile")[1]["restart_required"])
        saved = self.request(one, "/api/profile/settings", {"codex": {"enabled": False, "db_path": None}})
        self.assertEqual(saved[0], 200); self.assertTrue(saved[1]["restart_required"])
        self.assertEqual(one.profile.integrations()["remote"]["source"]["host"], "changed.invalid")
        self.assertEqual(one.service.source["host"], "example.invalid")
        self.assertEqual(two.service.source["host"], "second.invalid")

    def test_enabled_optional_routes_keep_security_and_unknown_route_guards(self):
        directory, _ = self.import_config()
        server = self.server(directory)
        remote, terminal, workbench = self.platform(server)
        for headers, reason in (({"Host": "evil.invalid"}, "host_rejected"),
                                ({"Origin": "https://evil.invalid"}, "origin_rejected"),
                                ({"X-Session-Client": ""}, "client_header_required"),
                                ({"X-Session-CSRF": ""}, "csrf_rejected")):
            self.assertEqual(self.request(server, "/api/workbench/connect", {}, headers)[1]["reason"], reason)
        for route in ("/api/sessions/unknown/path", "/api/workbench/unknown", "/api/launch/unknown"):
            self.assertEqual(self.request(server, route)[0], 404)
        self.assertEqual(self.request(server, "/api/sessions?extra=1")[0], 404)
        self.assertEqual(self.request(server, f"/api/sessions/{MAIN}/messages?limit=51")[0], 400)
        self.assertEqual(self.request(server, "/api/workbench?extra=1")[0], 400)
        self.assertEqual(self.request(server, "/api/workbench/connect", {"argv": []})[0], 400)
        self.assertEqual(self.request(server, "/api/launch", {"id": "website", "target": "https://evil.invalid"})[0], 400)
        self.assertEqual(self.request(server, "/api/profile/settings", {"codex": {"enabled": False, "db_path": None}, "integrations": {}})[0], 422)
        self.assertEqual(self.request(server, "/api/launch", {"id": "unknown"})[1]["reason"], "unknown_launcher")
        self.assertEqual((remote, terminal, workbench), ([], [], []))


if __name__ == "__main__":
    unittest.main()
