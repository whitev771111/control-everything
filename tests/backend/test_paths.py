"""Pure path contracts for checkouts, ordinary wheels, and fixed products."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

from session_panel import paths
from session_panel.sessions import observer_pipe


class PathsTests(TestCase):
    def test_checkout_resource_and_legacy_locations_are_unchanged(self):
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(paths.source_root(), root)
        self.assertEqual(paths.web_root(), root / "web")
        self.assertEqual(paths.legacy_data_root(), root)
        self.assertEqual(paths.execution_root(), root)
        self.assertEqual(paths.observer_install_dir(), root / "work/native-observer/install")

    def test_installed_resource_and_data_never_use_cwd(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            package = root / "site-packages/session_panel"
            resources = MagicMock()
            with patch.object(paths, "__file__", str(package / "paths.py")), \
                    patch.object(paths, "files", return_value=resources) as loader, \
                    patch.object(paths, "default_profile_dir", return_value=root / "user"):
                self.assertIsNone(paths.source_root())
                self.assertEqual(paths.web_root(), resources / "_assets" / "web")
                loader.assert_called_once_with("session_panel")
                self.assertEqual(paths.legacy_data_root(), root / "user/legacy")
                self.assertEqual(paths.execution_root(), package)
                self.assertFalse((root / "user").exists())

    def test_source_shape_requires_all_fixed_markers(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(paths, "__file__", str(root / "src/session_panel/paths.py")):
                self.assertIsNone(paths.source_root())
                (root / "pyproject.toml").touch()
                (root / "web").mkdir()
                self.assertIsNone(paths.source_root())
                (root / "native/observer").mkdir(parents=True)
                self.assertEqual(paths.source_root(), root)

    def test_default_profile_calculation_keeps_platform_fallbacks(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for platform, environment, expected in (
                    ("win32", {}, root / "AppData/Local/SessionPanel"),
                    ("win32", {"LOCALAPPDATA": str(root / "local")}, root / "local/SessionPanel"),
                    ("linux", {}, root / ".local/share/session-panel"),
                    ("linux", {"XDG_DATA_HOME": str(root / "xdg")}, root / "xdg/session-panel")):
                with self.subTest(platform=platform, environment=environment):
                    self.assertEqual(paths.default_profile_dir(
                        platform=platform, environ=environment, home=root), expected.resolve())
            self.assertEqual(list(root.iterdir()), [])

    def test_ordinary_wheel_has_no_implicit_native_fallback(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(paths, "__file__", str(root / "site/session_panel/paths.py")), \
                    patch.object(paths.sys, "executable", str(root / "python/python.exe")):
                self.assertIsNone(paths.observer_install_dir())

    def test_product_native_location_is_fixed_and_marker_mismatch_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            marker = root / "product.json"
            manifest = {"schema": 1, "product": "session-panel", "target": "windows-x86_64",
                        "native": {"relative": "native/observer/session-panel-observer.exe"}}
            marker.write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(paths, "__file__", str(root / "runtime/Lib/site-packages/session_panel/paths.py")), \
                    patch.object(paths.sys, "executable", str(root / "runtime/python.exe")):
                self.assertEqual(paths.observer_install_dir(), root / "native/observer")
                for invalid in ({**manifest, "schema": True}, {**manifest, "product": "other"},
                                {**manifest, "native": {"relative": "../other.exe"}}, []):
                    marker.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                        paths.observer_install_dir()
                marker.write_text("{", encoding="utf-8")
                with self.assertRaises(ValueError):
                    paths.observer_install_dir()
                marker.write_text(json.dumps(manifest), encoding="utf-8")
                with patch.object(paths, "__file__", str(root / "elsewhere/session_panel/paths.py")), \
                        self.assertRaises(ValueError):
                    paths.observer_install_dir()

    def test_missing_native_and_invalid_marker_fail_before_process_access(self):
        pipe = object.__new__(observer_pipe.WindowsPipe)
        pipe.close = Mock()
        for value, error, reason in ((None, None, "native_missing"),
                                     (None, ValueError("bad_marker"), "protocol_mismatch")):
            with patch.object(observer_pipe, "observer_install_dir", return_value=value, side_effect=error), \
                    self.assertRaises(observer_pipe.PipeError) as caught:
                pipe.connect()
            self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(pipe.close.call_count, 2)
