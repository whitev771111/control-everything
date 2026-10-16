import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from session_panel.integrations.launchers import LauncherCatalog, system_directory, validate_launcher_target
from session_panel.profiles.integration_settings import InvalidSettings, validate_integrations
from tests.fixtures.integration_config import integration_config


def launcher_config(kind, target):
    config = integration_config()["integrations"]["launchers"]
    config["items"] = [{"id": "test", "group": "tools", "name": "Test", "blurb": "Synthetic target",
                        "kind": kind, "target": str(target), "probe": ""}]
    return config


class LauncherTests(unittest.TestCase):
    def test_empty_disabled_unknown_and_catalog_hide_targets(self):
        with patch("session_panel.integrations.launchers.status_of") as status:
            for config in (None, {**launcher_config("url", "https://example.invalid/"), "enabled": False}):
                catalog = LauncherCatalog(config)
                self.assertEqual(catalog.catalog(), [])
                spawn = Mock()
                self.assertFalse(catalog.start("test", spawn))
                spawn.assert_not_called()
            status.assert_not_called()
        config = launcher_config("url", "https://example.invalid/")
        catalog = LauncherCatalog(config)
        config["items"][0]["target"] = "https://other.invalid/"
        self.assertNotEqual(catalog.items[0]["target"], config["items"][0]["target"])
        self.assertEqual(catalog.catalog(), [{"id": "test", "group": "tools", "name": "Test",
                                             "blurb": "Synthetic target", "status": "ready"}])
        spawn = Mock()
        self.assertFalse(catalog.start("test & anything", spawn))
        spawn.assert_not_called()

    def test_typed_targets_reject_nonfile_batch_and_bad_urls(self):
        for kind, target in (("cmd", "C:/example/tool.exe"), ("py", "C:/example/tool.cmd"),
                             ("url", "file:///C:/example/tool"), ("url", "https://user:pass@example.invalid/"),
                             ("url", "https://example.invalid:invalid/"), ("task", "task\nother")):
            with self.subTest(kind=kind, target=target), self.assertRaises(ValueError):
                validate_launcher_target(kind, target)
        self.assertEqual(validate_launcher_target("cmd", "C:/example/中文 空格/开始.CMD"),
                         r"C:\example\中文 空格\开始.CMD")

    def test_task_and_url_keep_independent_dispatch(self):
        spawn = Mock()
        with patch("session_panel.integrations.launchers.system_directory", return_value=Path("C:/example/System32")):
            self.assertTrue(LauncherCatalog(launcher_config("task", "Example & task")).start("test", spawn))
        self.assertEqual(spawn.call_args.args[0], [str(Path("C:/example/System32/schtasks.exe")), "/Run", "/TN", "Example & task"])
        self.assertFalse(spawn.call_args.kwargs["shell"])
        with patch("session_panel.integrations.launchers.os.startfile") as browser:
            self.assertTrue(LauncherCatalog(launcher_config("url", "https://example.invalid/")).start("test", spawn))
            browser.assert_called_once_with("https://example.invalid/")
        self.assertEqual(spawn.call_count, 1)

    def test_non_windows_batch_and_task_explicitly_refuse(self):
        with patch("session_panel.integrations.launchers.os.name", "posix"):
            for kind, target in (("cmd", "C:/example/start.cmd"), ("task", "ExampleTask")):
                catalog = LauncherCatalog(launcher_config(kind, target)); spawn = Mock()
                self.assertFalse(catalog.start("test", spawn))
                self.assertEqual(catalog.last_error, "unsupported_platform")
                spawn.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows launcher contract")
class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="launcher-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_batch_spaces_unicode_keeps_cmd_alive_then_exits_cleanly(self):
        directory = self.root / "中文 空格"
        directory.mkdir()
        for suffix in (".cmd", ".bat"):
            with self.subTest(suffix=suffix):
                target = directory / ("启动 文件" + suffix)
                marker = directory / (suffix[1:] + "-marker.txt")
                target.write_bytes((f'@echo off\r\necho synthetic>"%~dp0{marker.name}"\r\nexit /b 0\r\n').encode("ascii"))
                catalog = LauncherCatalog(launcher_config("cmd", target))
                spawned = []
                def hidden_spawn(command, **kwargs):
                    self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NEW_CONSOLE)
                    self.assertFalse(kwargs["shell"])
                    self.assertEqual(kwargs["executable"], str(system_directory() / "cmd.exe"))
                    self.assertEqual(command, f'"{kwargs["executable"]}" /d /v:off /s /k ""{target.resolve()}""')
                    self.assertFalse(any(key in kwargs for key in ("stdin", "stdout", "stderr")))
                    kwargs.update(creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    process = subprocess.Popen(command, **kwargs)
                    spawned.append(process)
                    return process
                try:
                    self.assertTrue(catalog.start("test", hidden_spawn), catalog.last_error)
                    process = spawned[0]
                    deadline = time.monotonic() + 5
                    while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertEqual(marker.read_text().strip(), "synthetic")
                    self.assertIsNone(process.poll(), "exit /b must leave /k running")
                    stdout, stderr = process.communicate(b"exit\r\n", timeout=5)
                    self.assertEqual(process.returncode, 0, (stdout, stderr))
                    print(f"batch {suffix}: marker=synthetic, retained=True, cleanup_exit={process.returncode}")
                finally:
                    for process in spawned:
                        if process.poll() is None:
                            process.kill()
                        process.communicate(timeout=5)

    def test_batch_production_has_new_console_no_redirection_no_wait(self):
        target = self.root / "safe.cmd"; target.write_text("@exit /b 0\n")
        spawn = Mock()
        self.assertTrue(LauncherCatalog(launcher_config("cmd", target)).start("test", spawn))
        self.assertEqual(spawn.call_args.kwargs["creationflags"], subprocess.CREATE_NEW_CONSOLE)
        self.assertTrue(spawn.call_args.kwargs["close_fds"])
        self.assertFalse(spawn.call_args.kwargs["shell"])
        for key in ("stdin", "stdout", "stderr"):
            self.assertNotIn(key, spawn.call_args.kwargs)
        spawn.return_value.wait.assert_not_called()

    def test_unsafe_batch_import_and_start_never_spawn(self):
        marker = self.root / "executed.txt"
        expansion = self.root / "expanded.txt"
        targets = []
        for name in ("safe&other.cmd", "%LAUNCH_TEST_NAME%.cmd", "!LAUNCH_TEST_NAME!.cmd", "a^b.cmd", "a(b).bat"):
            target = self.root / name
            target.write_text(f'@echo forbidden>"{marker}"\nexit /b 0\n')
            targets.append(str(target))
        targets += ["C:/example/quote\".cmd", "C:/example/a>b.cmd", "C:/example/a|b.cmd",
                    "C:/example/a<b.cmd", "C:/example/q?.cmd", "C:/example/a*.cmd",
                    "C:/example/a\n.cmd", "C:/example/a\x00.cmd", "C:/example/start.cmd extra",
                    "C:/example/start.cmd:stream", "C:/example/../start.cmd", "C:/example/./start.cmd",
                    "C:/example/trailing. /start.cmd", "C:/example/trailing./start.cmd", "C:/example/NUL.cmd",
                    "relative.cmd", "C:relative.cmd", "//server/share/start.cmd", "//?/C:/example/start.cmd"]
        with patch.dict(os.environ, {"LAUNCH_TEST_NAME": str(expansion)}):
            for target in targets:
                with self.subTest(target=target):
                    config = launcher_config("cmd", target)
                    integrations = integration_config()["integrations"]
                    integrations["launchers"] = config
                    with self.assertRaises(InvalidSettings) as rejected:
                        validate_integrations(integrations)
                    self.assertEqual(rejected.exception.field, "integrations.launchers.items.0.target")
                    spawn = Mock(); catalog = LauncherCatalog(config)
                    self.assertFalse(catalog.start("test", spawn))
                    self.assertIn(catalog.last_error, {"unsafe_batch_path", "unsupported_target_type"})
                    spawn.assert_not_called()
                    self.assertFalse(marker.exists()); self.assertFalse(expansion.exists())

    def test_resolved_unsafe_batch_is_rejected(self):
        unsafe = self.root / "%LAUNCH_TEST_NAME%.cmd"
        unsafe.write_text("@exit /b 0\n")
        safe = self.root / "safe.cmd"
        try:
            safe.symlink_to(unsafe)
        except OSError as error:
            self.skipTest(f"Windows symlink privilege unavailable: {error.winerror}")
        spawn = Mock(); catalog = LauncherCatalog(launcher_config("cmd", safe))
        self.assertFalse(catalog.start("test", spawn))
        self.assertEqual(catalog.last_error, "unsafe_batch_path")
        spawn.assert_not_called()

    def test_resolved_target_is_revalidated_even_after_existing_file_check(self):
        target = self.root / "safe.cmd"
        target.write_text("@exit /b 0\n")
        spawn = Mock(); catalog = LauncherCatalog(launcher_config("cmd", target))
        with patch("session_panel.integrations.launchers.Path.resolve", return_value=self.root / "unsafe%NAME%.cmd"):
            self.assertFalse(catalog.start("test", spawn))
        self.assertEqual(catalog.last_error, "unsafe_batch_path")
        spawn.assert_not_called()

    def test_python_special_characters_are_literal_argv(self):
        directory = self.root / "中文 空格"
        directory.mkdir()
        target = directory / "script & %LAUNCH_TEST_NAME% !name! ^.py"
        marker = self.root / "python-marker.txt"
        target.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('literal')\n", encoding="utf-8")
        spawned = []
        def hidden_spawn(argv, **kwargs):
            self.assertEqual(argv, [str(Path(sys.executable).resolve()), str(target.resolve())])
            self.assertFalse(kwargs["shell"])
            process = subprocess.Popen(argv, creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)
            spawned.append(process)
            return process
        try:
            self.assertTrue(LauncherCatalog(launcher_config("py", target)).start("test", hidden_spawn))
            self.assertEqual(spawned[0].wait(timeout=5), 0)
            self.assertEqual(marker.read_text(), "literal")
            print("python literal target: marker=literal, cleanup_exit=0")
        finally:
            for process in spawned:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
