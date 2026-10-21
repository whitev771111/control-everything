"""Temporary profiles only; no user metadata or listening sockets."""
import json
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_everything.profiles.profile import Profile, ProfileError, resolve_data_dir
from control_everything.workspace.store import WorkspaceError, BASE_SCHEMA
from control_everything.workspace.task_schema import SCHEMA_VERSION


class ProfileTests(unittest.TestCase):
    def test_path_priority_and_platform_defaults(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env = {"SESSION_PANEL_DATA_DIR": str(root / "env"), "LOCALAPPDATA": str(root / "local")}
            self.assertEqual(resolve_data_dir(str(root / "cli"), environ=env), root / "cli")
            self.assertEqual(resolve_data_dir(environ=env), root / "env")
            self.assertEqual(resolve_data_dir(environ={}, platform="win32", home=root), root / "AppData" / "Local" / "SessionPanel")
            self.assertEqual(resolve_data_dir(environ={}, platform="linux", home=root), root / ".local" / "share" / "session-panel")
            for value in ("", "relative"):
                with self.assertRaises(ProfileError):
                    resolve_data_dir(value)

    def test_empty_profile_exact_schema_and_config_reloads(self):
        with TemporaryDirectory() as directory:
            profile = Profile.open(directory)
            self.assertEqual(profile.store.list_projects(), [])
            self.assertTrue(profile.public()["can_import"])
            self.assertEqual(profile.codex_threads()["status"], "not_configured")
            with closing(sqlite3.connect(profile.store.path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'execution%'")}
                self.assertEqual(tables, {'execution_runs', 'execution_events', 'execution_approvals'})
            configured = {"codex": {"enabled": False, "db_path": str(Path(directory) / "explicit.sqlite")}}
            profile.settings(configured)
            reopened = Profile.open(directory)
            self.assertEqual(reopened.config()["codex"], configured["codex"])
            with patch("control_everything.sessions.codex_index.list_threads", side_effect=AssertionError("disabled reader")):
                self.assertEqual(profile.codex_threads()["status"], "not_configured")
            profile.settings({"codex": {"enabled": True, "db_path": configured["codex"]["db_path"]}})
            self.assertEqual(profile.codex_threads()["status"], "missing")

    def test_corrupt_unknown_and_atomic_replace_failures_preserve_config(self):
        with TemporaryDirectory() as directory:
            profile = Profile.open(directory)
            original = profile.config_path.read_bytes()
            with patch("control_everything.profiles.profile.os.replace", side_effect=OSError("fixture")):
                with self.assertRaises(ProfileError):
                    profile.settings({"codex": {"enabled": False, "db_path": None}})
            self.assertEqual(profile.config_path.read_bytes(), original)
            self.assertEqual(list(Path(directory).glob(".profile-*.tmp")), [])
            for data in ('{"version":2,"codex":{"enabled":false,"db_path":null}}', '{"version":1,"version":1}', '{"n":1e999}', '[]', 'x', ' ' * 16385):
                profile.config_path.write_text(data, encoding="utf-8")
                with self.assertRaises(ProfileError):
                    Profile.open(directory)
                self.assertEqual(profile.config_path.read_text(encoding="utf-8"), data)

    def test_existing_legacy_future_and_extra_schema_are_rejected(self):
        for mode in ("legacy", "future", "extra", "column"):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                if mode == "legacy":
                    with closing(sqlite3.connect(Path(directory) / "workspace-state.sqlite3")) as connection:
                        connection.executescript(BASE_SCHEMA)
                else:
                    profile = Profile.open(directory)
                    with closing(sqlite3.connect(profile.store.path)) as connection:
                        connection.execute({"future": f"PRAGMA user_version={SCHEMA_VERSION + 1}", "extra": "CREATE TABLE extra(x)", "column": "ALTER TABLE projects ADD COLUMN extra TEXT"}[mode])
                        connection.commit()
                with self.assertRaises(ProfileError):
                    Profile.open(directory)


class ProfileEntryTests(unittest.TestCase):
    def test_legacy_cli_contract(self):
        import runpy
        from unittest.mock import patch

        with patch("control_everything.profiles.profile.main", return_value=7) as main:
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("control_everything.profile", run_name="__main__")
        self.assertEqual(caught.exception.code, 7)
        main.assert_called_once_with()
