"""The build entry must never repair a missing toolchain by installing one."""

import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("observer_build", ROOT / "scripts/build_observer.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class ObserverBuildTests(unittest.TestCase):
    def test_missing_component_does_not_install_or_fall_back(self):
        with patch.object(BUILD.shutil, "which", return_value="rustup"), \
                patch.object(BUILD.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")) as run:
            with self.assertRaisesRegex(RuntimeError, "prepared_component_missing"):
                BUILD.prepared_environment()
            self.assertEqual(run.call_args.args[0], ["rustup", "which", "--toolchain", BUILD.TOOLCHAIN, "cargo"])
            self.assertEqual(run.call_args.kwargs["env"]["RUSTUP_AUTO_INSTALL"], "0")
            self.assertEqual(run.call_count, 1)

    def test_explicit_prepared_home_and_all_children_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            binary = home / "toolchains" / BUILD.TOOLCHAIN / "bin"
            binary.mkdir(parents=True)
            for name in BUILD.TOOLS:
                (binary / (name + ".exe")).write_bytes(b"fixture")

            def which_tool(command, **kwargs):
                self.assertEqual(command[:3], ["rustup", "which", "--toolchain"])
                self.assertEqual(kwargs["env"]["RUSTUP_HOME"], str(home))
                return SimpleNamespace(returncode=0, stdout=str(binary / (command[-1] + ".exe")) + "\n")

            with patch.dict(os.environ, {"RUSTUP_HOME": str(home)}), \
                    patch.object(BUILD.shutil, "which", return_value="rustup"), \
                    patch.object(BUILD.subprocess, "run", side_effect=which_tool) as run:
                environment, tools = BUILD.prepared_environment()
            self.assertEqual(run.call_count, 7)
            self.assertEqual(environment["RUSTUP_AUTO_INSTALL"], "0")
            self.assertEqual(environment["RUSTUP_HOME"], str(home))
            self.assertEqual(environment["RUSTC"], tools["rustc"])
            self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(binary))
            self.assertTrue(Path(environment["CARGO_HOME"]).is_relative_to(ROOT / "work"))

    def test_a_tool_outside_selected_home_is_rejected(self):
        with patch.object(BUILD.shutil, "which", return_value="rustup"), \
                patch.object(BUILD.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=str(Path(__file__).resolve()))):
            with self.assertRaisesRegex(RuntimeError, "prepared_component_identity"):
                BUILD.prepared_environment()


if __name__ == "__main__":
    unittest.main()
