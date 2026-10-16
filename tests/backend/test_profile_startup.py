"""Startup assembly is checked without opening a listening socket."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from session_panel.http import gateway


class ProfileStartupTests(unittest.TestCase):
    def test_assembly_selects_profile_before_any_personal_or_execution_constructor(self):
        with TemporaryDirectory() as directory:
            fake = Mock()
            fake.server_address = ("127.0.0.1", 14401)
            with patch.object(gateway, "PanelServer", return_value=fake), patch.object(gateway, "actor_from_environment", side_effect=AssertionError("actor")), patch.object(gateway, "GardenService", side_effect=AssertionError("garden")), patch("session_panel.sessions.service.SessionService", side_effect=AssertionError("ssh")), patch("session_panel.execution.service.ExecutionService", side_effect=AssertionError("owner")):
                server = gateway.build_profile_server(directory, 14401, quota_source=Mock(), news_source=Mock())
            self.assertIsNone(server.service)
            self.assertIsNone(server.execution)
            self.assertIsNone(server.garden)
            self.assertEqual(server.profile.data_dir, Path(directory))
            self.assertEqual(set(path.name for path in Path(directory).iterdir()), {"profile.json", "workspace-state.sqlite3"})

    def test_cli_exact_port_and_data_arguments(self):
        with patch.object(gateway, "serve", return_value=0) as serve:
            self.assertEqual(gateway.main(["--data-dir", "C:/temporary/example", "--port", "14401"]), 0)
            serve.assert_called_once_with(data_dir="C:/temporary/example", port=14401)
        for args in (["--port", "1023"], ["--port", "65536"], ["--host", "192.0.2.1"]):
            with patch("sys.stderr"), self.assertRaises(SystemExit) as error:
                gateway.main(args)
            self.assertEqual(error.exception.code, 2)

    def test_bind_failure_never_retries_or_takes_over(self):
        with patch.object(gateway, "build_profile_server", side_effect=OSError("occupied")) as build, patch("sys.stderr"):
            self.assertEqual(gateway.serve("C:/temporary/profile", 14401), 1)
        build.assert_called_once_with("C:/temporary/profile", 14401)
