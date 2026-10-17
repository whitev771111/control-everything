"""Build command contracts against a temporary asset-only source tree."""

import ast
import importlib.util
from importlib.machinery import PathFinder
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import tomllib
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from setuptools import Distribution
from setuptools.command.editable_wheel import editable_wheel

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("build_assets", ROOT / "tools/packaging/build_assets.py")
assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assets)


class PackagingTests(TestCase):
    def test_asset_set_matches_http_allowlist_and_sdist(self):
        tree = ast.parse((ROOT / "src/control_everything/http/gateway.py").read_text(encoding="utf-8"))
        declarations = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "static" for target in node.targets)]
        self.assertEqual(len(declarations), 1)
        http = ast.literal_eval(declarations[0].value)
        self.assertEqual(set(assets.WEB_ASSETS), {value[0] for value in http.values()})
        self.assertEqual(len(assets.WEB_ASSETS), len(set(assets.WEB_ASSETS)))
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
        included = {name for line in manifest if line.startswith("include web/") for name in line.split()[1:]}
        self.assertEqual(included, {"web/" + relative for relative in assets.WEB_ASSETS})

    def test_metadata_has_one_runtime_dependency_and_version_source(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["project"]["name"], "control-everything")
        self.assertEqual(metadata["project"]["version"], "0.1.0a1")
        setuptools = metadata["tool"]["setuptools"]
        self.assertEqual(setuptools["dynamic"]["dependencies"], {"file": ["requirements.txt"]})
        self.assertEqual(setuptools["package-dir"], {"": "src"})
        self.assertEqual(setuptools["packages"]["find"], {"where": ["src"], "include": ["control_everything*"]})
        self.assertNotIn("cmdclass", setuptools)

    def test_setup_registers_only_the_fixed_build_helper(self):
        with patch("setuptools.setup") as setup:
            runpy.run_path(str(ROOT / "setup.py"))
        setup.assert_called_once()
        self.assertEqual(setup.call_args.args, ())
        self.assertEqual(set(setup.call_args.kwargs), {"cmdclass"})
        commands = setup.call_args.kwargs["cmdclass"]
        self.assertEqual(set(commands), {"build_py"})
        self.assertTrue(issubclass(commands["build_py"], assets.build_py))
        self.assertIn("include setup.py", (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines())

    def test_default_editable_avoids_old_namespace_with_simple_src_layout(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            # Preserve a former package with ignored cache directories, as in a real checkout.
            (root / "control_everything/workspace/__pycache__").mkdir(parents=True)
            package = root / "src/control_everything"
            (package / "workspace").mkdir(parents=True)
            (package / "__init__.py").touch()
            (package / "workspace/__init__.py").touch()
            source = package / "workspace/sources.py"
            source.touch()
            distribution = Distribution({"packages": ["control_everything", "control_everything.workspace"],
                                         "package_dir": metadata["tool"]["setuptools"]["package-dir"]})
            distribution.src_root = str(root)
            command = editable_wheel(distribution)
            command.ensure_finalized()
            strategy = command._select_strategy("control_everything-0.1.0a1", "py3-none-any", root / "build")
            wheel = Mock()
            strategy(wheel, [], {})
            filename, contents = wheel.writestr.call_args.args
            self.assertTrue(filename.endswith(".pth"))
            entries = contents.decode("utf-8").splitlines()
            self.assertEqual(entries, [str((root / "src").resolve())])
            # PathFinder skips earlier namespace candidates once it sees the real regular package.
            spec = PathFinder.find_spec("control_everything", [str(root), *entries])
            self.assertEqual(Path(spec.origin), package / "__init__.py")
            child = PathFinder.find_spec("control_everything.workspace", spec.submodule_search_locations)
            leaf = PathFinder.find_spec("control_everything.workspace.sources", child.submodule_search_locations)
            self.assertEqual(Path(leaf.origin), source)
            self.assertTrue((root / "control_everything/workspace/__pycache__").is_dir())

    def command(self, root):
        command = assets.BuildPy(Distribution())
        command.ensure_finalized()
        command.build_lib = str(root / "output")
        return command

    def write_assets(self, root):
        for relative in assets.WEB_ASSETS:
            path = root / "web" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("asset:" + relative).encode("utf-8"))

    def test_mapping_is_available_before_run_and_copies_exact_bytes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.write_assets(root)
            with patch.object(assets, "__file__", str(root / "tools/packaging/build_assets.py")):
                command = self.command(root)
                mapping = command.get_output_mapping()
                self.assertEqual(len(mapping), len(assets.WEB_ASSETS))
                self.assertEqual(set(command.get_source_files()),
                                 {Path(source).relative_to(root).as_posix() for source in mapping.values()})
                self.assertEqual(set(command.get_outputs()), set(mapping))
                self.assertFalse((root / "output").exists())
                command.run()
                for destination, source in mapping.items():
                    self.assertEqual(Path(destination).read_bytes(), Path(source).read_bytes())

    def test_sdist_source_list_uses_relative_posix_names(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.write_assets(root)
            with patch.object(assets, "__file__", str(root / "tools/packaging/build_assets.py")):
                sources = self.command(root).get_source_files()
                self.assertEqual(set(sources), {"web/" + relative for relative in assets.WEB_ASSETS})
                self.assertTrue(all(not Path(source).is_absolute() and "\\" not in source for source in sources))

    def test_editable_does_not_copy_assets_into_source_or_build_tree(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.write_assets(root)
            with patch.object(assets, "__file__", str(root / "tools/packaging/build_assets.py")):
                command = self.command(root)
                command.editable_mode = True
                self.assertEqual(len(command.get_output_mapping()), len(assets.WEB_ASSETS))
                command.run()
                self.assertFalse((root / "output").exists())
                self.assertFalse((root / "src").exists())

    def test_missing_linked_and_outside_assets_are_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(assets, "__file__", str(root / "tools/packaging/build_assets.py")):
                command = self.command(root)
                with self.assertRaisesRegex(ValueError, "missing_web_asset"):
                    command.get_output_mapping()
                self.write_assets(root)
                original = Path.is_symlink
                linked = root / "web/workspace"
                with patch.object(Path, "is_symlink", lambda path: path == linked or original(path)), \
                        self.assertRaisesRegex(ValueError, "linked_web_asset"):
                    command.get_output_mapping()
                for relative in ("../private.json", "/outside.html", "workspace/../../outside.js", "workspace/source.ts"):
                    with patch.object(assets, "WEB_ASSETS", (relative,)), \
                            self.assertRaisesRegex(ValueError, "invalid_web_asset"):
                        command.get_output_mapping()

    def test_worker_uses_real_interpreter_and_packaged_source_without_spawning(self):
        from control_everything.workspace import news_worker
        api = object.__new__(news_worker.NewsWindowsAPI)
        api.base = Mock()
        api.base.spawn.return_value = (11, 12, 13)
        owned = SimpleNamespace(child_handles=(1, 2, 3))
        news_worker.NewsWindowsAPI.spawn(api, owned, "ai", "a" * 32)
        executable, argv, cwd, handles = api.base.spawn.call_args.args
        self.assertEqual(executable, news_worker.sys.executable)
        self.assertEqual(argv[:4], ["-I", "-B", "-X", "utf8"])
        self.assertEqual(Path(argv[4]), Path(news_worker.__file__).resolve())
        self.assertEqual(cwd, Path(news_worker.__file__).resolve().parents[2])
        self.assertEqual(handles, (1, 2, 3))
        for name in ("source.py", "remote_probe.py"):
            self.assertTrue((ROOT / "src/control_everything/sessions" / name).is_file())
