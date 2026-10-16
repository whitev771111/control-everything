"""Offline configuration, CLI and deterministic disposable-process lock checks."""

import copy
import json
import multiprocessing
import os
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from session_panel.profiles import profile as profiles
from session_panel.profiles.integration_settings import InvalidSettings, validate_integrations
from session_panel.profiles.profile_import import create_backup, restore_backup
from tests.fixtures.integration_config import integration_config


def settings_document():
    return {"version": 2, "integrations": integration_config()["integrations"]}


def profile_process(directory, action, ready, release, result):
    """Pause real critical sections without adding hooks to product code."""
    writes = 0
    original_write = profiles.Profile._write_config
    original_lock = profiles.config_file_lock

    def write(profile, config):
        nonlocal writes
        writes += 1
        if action in ("first_pause", "settings_pause"):
            ready.set()
            if not release.wait(15):
                raise AssertionError("release_timeout")
        return original_write(profile, config)

    @contextmanager
    def before_lock(path):
        ready.set()
        if not release.wait(15):
            raise AssertionError("release_timeout")
        with original_lock(path):
            yield

    try:
        if action == "hold":
            with original_lock(directory):
                ready.set()
                release.wait(15)
            result.put({"status": "ok", "writes": writes})
            return
        with patch.object(profiles.Profile, "_write_config", write):
            if action == "before_lock":
                with patch.object(profiles, "config_file_lock", before_lock):
                    profile = profiles.Profile.open(directory)
            else:
                profile = profiles.Profile.open(directory)
            if action == "import":
                profile.import_settings(settings_document())
            elif action == "settings_pause":
                profile.settings({"codex": {"enabled": False, "db_path": str(Path(directory) / "synthetic.sqlite")}})
            result.put({"status": "ok", "writes": writes, "config": profile.config()})
    except profiles.ProfileError as error:
        result.put({"status": error.reason, "writes": writes})
    except BaseException as error:
        result.put({"status": type(error).__name__, "writes": writes})
        raise


class IntegrationSettingsTests(unittest.TestCase):
    def test_roundtrip_offline_and_public_summary_hides_private_values(self):
        with TemporaryDirectory() as directory:
            profile = profiles.Profile.open(directory)
            before = profile.integrations()
            with patch("subprocess.run", side_effect=AssertionError("no process probing")), \
                    patch("session_panel.sessions.codex_index.list_threads", side_effect=AssertionError("no database")):
                result = profile.import_settings(settings_document())
                public = profile.public(startup_integrations=before)
                self.assertEqual(profile.status()["integrations"]["remote"]["status"], "configured")
            self.assertTrue(result["restart_required"])
            self.assertTrue(public["restart_required"])
            self.assertNotIn("integrations", public["config"])
            encoded = json.dumps(public)
            for private in ("example.invalid", "wezterm.lua", "ExampleTask", "a" * 64, "reader/bin/python"):
                self.assertNotIn(private, encoded)
            self.assertFalse(profile.public(startup_integrations=profile.integrations())["restart_required"])
            profile.settings({"codex": {"enabled": False, "db_path": str(Path(directory) / "explicit.sqlite")}})
            self.assertEqual(profile.integrations(), validate_integrations(settings_document()["integrations"]))
            self.assertEqual(profile.config()["codex"]["db_path"], str(Path(directory) / "explicit.sqlite"))

    def test_v1_read_is_unchanged_and_first_save_backup_restores_v1(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = b'{"version":1,"codex":{"enabled":false,"db_path":null}}\n'
            (root / "profile.json").write_bytes(original)
            profile = profiles.Profile.open(directory)
            self.assertEqual(profile.config_path.read_bytes(), original)
            self.assertTrue(all(value is None for value in profile.integrations().values()))
            profile.settings({"codex": {"enabled": False, "db_path": None}})
            self.assertEqual(profile.config()["version"], 2)
            backup = next((root / "backups").iterdir())
            restored = restore_backup(str(backup), str(root / "restored"))
            self.assertEqual(profiles.Profile.open(str(restored)).config()["version"], 1)

    def test_import_failure_preserves_original_and_backup_v2_roundtrips(self):
        with TemporaryDirectory() as directory:
            profile = profiles.Profile.open(directory)
            before = profile.config_path.read_bytes()
            with patch("session_panel.profiles.profile_import.create_backup", side_effect=profiles.ProfileError("backup_failed")):
                with self.assertRaises(profiles.ProfileError):
                    profile.import_settings(settings_document())
            self.assertEqual(profile.config_path.read_bytes(), before)
            with patch.object(profiles.os, "replace", side_effect=OSError("synthetic")):
                with self.assertRaises(profiles.ProfileError):
                    profile.import_settings(settings_document())
            self.assertEqual(profile.config_path.read_bytes(), before)
            profile.import_settings(settings_document())
            backup = create_backup(profile)
            restored = profiles.Profile.open(str(restore_backup(str(backup), str(Path(directory) / "restored"))))
            self.assertEqual(restored.config(), profile.config())
            self.assertEqual(profile.store.list_projects(), [])

    def test_validation_rejects_unknown_types_dependencies_and_unsafe_targets(self):
        base = settings_document()["integrations"]
        cases = []
        def changed(section, field, value):
            item = copy.deepcopy(base)
            item[section][field] = value
            cases.append(item)
        changed("remote", "enabled", 1)
        changed("remote", "ssh", "ssh -o Unsafe")
        changed("remote", "reader_python", "/reader/../python")
        changed("remote", "source", {**base["remote"]["source"], "host": "-proxy"})
        changed("terminal", "session", "../escape")
        changed("terminal", "pane_script", "~/pane.sh & echo extra")
        changed("workbench", "pins", {})
        changed("workbench", "enabled", "true")
        changed("remote", "enabled", False)
        changed("launchers", "groups", [])
        cases.append({**base, "unknown": None})
        for value in cases:
            with self.subTest(value=value), self.assertRaises(InvalidSettings):
                validate_integrations(value)
        for marker in ("&", "%NAME%", "!NAME!", "^", '"', "("):
            item = copy.deepcopy(base)
            item["launchers"]["items"][0]["target"] = f"C:/example/safe{marker}.cmd"
            with self.subTest(marker=marker), self.assertRaises(InvalidSettings) as caught:
                validate_integrations(item)
            self.assertTrue(caught.exception.field.endswith("items.0.target"))
            self.assertEqual(caught.exception.message, "unsafe_batch_path")
        disabled = copy.deepcopy(base)
        for item in disabled.values():
            item["enabled"] = False
        self.assertEqual(validate_integrations(disabled)["remote"]["source"]["id"], base["remote"]["source"]["id"])
        with self.assertRaises(profiles.ProfileError):
            profiles.validate_settings_document({"version": True, "integrations": base})

    def test_large_valid_configuration_uses_same_budget_in_backup_restore(self):
        document = settings_document()
        template = document["integrations"]["launchers"]["items"][0]
        document["integrations"]["launchers"]["items"] = [
            {**template, "id": f"entry-{index}", "name": "n" * 128, "blurb": "b" * 512}
            for index in range(32)
        ]
        with TemporaryDirectory() as directory:
            profile = profiles.Profile.open(directory)
            profile.import_settings(document)
            self.assertGreater(profile.config_path.stat().st_size, 16384)
            self.assertLess(profile.config_path.stat().st_size, profiles.CONFIG_MAX_BYTES)
            backup = create_backup(profile)
            restored = restore_backup(str(backup), str(Path(directory) / "restored"))
            self.assertEqual(profiles.Profile.open(str(restored)).config(), profile.config())

    def test_duplicate_nonfinite_oversized_cli_input_is_rejected_without_initializing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            target = root / "destination"
            for content in ('{"version":2,"version":2}', '{"v":NaN}', '{"v":1e999}', " " * (profiles.CONFIG_MAX_BYTES + 1)):
                settings.write_text(content, encoding="utf-8")
                command = [sys.executable, "-m", "session_panel.profile", "import-settings", "--file", str(settings), "--data-dir", str(target)]
                child = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=20)
                self.assertEqual(child.returncode, 1, child.stderr)
                self.assertIn("invalid_config", child.stderr)
                self.assertFalse(target.exists())

    def test_cli_init_validate_import_status_and_restore(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text(json.dumps(settings_document()), encoding="utf-8")
            data = root / "profile"
            def command(*args):
                child = subprocess.run([sys.executable, "-X", "utf8", "-m", "session_panel.profile", *map(str, args)],
                                       capture_output=True, text=True, encoding="utf-8", timeout=20)
                self.assertEqual(child.returncode, 0, child.stderr)
                return child.stdout
            command("validate-settings", "--file", settings)
            self.assertFalse(data.exists())
            command("init", "--data-dir", data)
            output = command("import-settings", "--file", settings, "--data-dir", data)
            receipt = json.loads(output.splitlines()[0])
            self.assertNotIn("example.invalid", command("status", "--data-dir", data))
            self.assertIn("configured", output)
            command("restore", "--backup", receipt["backup_path"], "--data-dir", root / "restored")
            self.assertTrue(all(value is None for value in profiles.Profile.open(str(root / "restored")).integrations().values()))


class ProfileProcessLockTests(unittest.TestCase):
    def setUp(self):
        self.context = multiprocessing.get_context("spawn")
        self.children = []
        self.queues = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for process in self.children:
            if process.is_alive():
                process.terminate()
            process.join(10)
        for result in self.queues:
            result.close()
            result.join_thread()

    def spawn(self, directory, action):
        ready, release, result = self.context.Event(), self.context.Event(), self.context.Queue()
        process = self.context.Process(target=profile_process, args=(str(directory), action, ready, release, result))
        self.children.append(process)
        self.queues.append(result)
        process.start()
        return process, ready, release, result

    def outcome(self, child):
        value = child[3].get(timeout=20)
        child[0].join(20)
        self.assertFalse(child[0].is_alive(), "child did not finish")
        self.assertEqual(child[0].exitcode, 0)
        return value

    def test_two_first_creators_and_import_competitor_are_serialized(self):
        with TemporaryDirectory() as directory:
            first = self.spawn(directory, "first_pause")
            self.assertTrue(first[1].wait(10))
            for action in ("open", "import"):
                busy = self.outcome(self.spawn(directory, action))
                self.assertEqual(busy, {"status": "config_busy", "writes": 0})
            self.assertFalse((Path(directory) / "profile.json").exists())
            first[2].set()
            self.assertEqual(self.outcome(first)["writes"], 1)
            reopened = self.outcome(self.spawn(directory, "open"))
            self.assertEqual((reopened["status"], reopened["writes"]), ("ok", 0))
            imported = self.outcome(self.spawn(directory, "import"))
            self.assertEqual((imported["status"], imported["writes"]), ("ok", 1))

    def test_waiting_first_creator_does_not_replace_completed_import(self):
        with TemporaryDirectory() as directory:
            waiting = self.spawn(directory, "before_lock")
            self.assertTrue(waiting[1].wait(10))
            imported = self.outcome(self.spawn(directory, "import"))
            self.assertEqual(imported["status"], "ok")
            before = (Path(directory) / "profile.json").read_bytes()
            waiting[2].set()
            final = self.outcome(waiting)
            self.assertEqual((final["status"], final["writes"]), ("ok", 0))
            self.assertEqual((Path(directory) / "profile.json").read_bytes(), before)
            self.assertEqual(final["config"], imported["config"])

    def test_process_exit_releases_persistent_lock(self):
        with TemporaryDirectory() as directory:
            profiles.Profile.open(directory)
            holder = self.spawn(directory, "hold")
            self.assertTrue(holder[1].wait(10))
            self.assertEqual(self.outcome(self.spawn(directory, "open"))["status"], "config_busy")
            holder[0].terminate()
            holder[0].join(10)
            self.assertEqual(self.outcome(self.spawn(directory, "open"))["status"], "ok")
            self.assertTrue((Path(directory) / ".profile.lock").is_file())

    def test_settings_and_import_competitor_do_not_lose_sections(self):
        with TemporaryDirectory() as directory:
            profiles.Profile.open(directory)
            saving = self.spawn(directory, "settings_pause")
            self.assertTrue(saving[1].wait(10))
            self.assertEqual(self.outcome(self.spawn(directory, "import"))["status"], "config_busy")
            saving[2].set()
            saved = self.outcome(saving)
            imported = self.outcome(self.spawn(directory, "import"))
            self.assertEqual(imported["status"], "ok")
            self.assertEqual(imported["config"]["codex"], saved["config"]["codex"])
            self.assertEqual(imported["config"]["integrations"], validate_integrations(settings_document()["integrations"]))
