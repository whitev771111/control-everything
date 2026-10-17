"""Register the build command without adding tools to the package layout."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from setuptools import setup


spec = spec_from_file_location("control_everything_build_assets", Path(__file__).resolve().parent / "tools/packaging/build_assets.py")
build_assets = module_from_spec(spec)
spec.loader.exec_module(build_assets)
setup(cmdclass={"build_py": build_assets.BuildPy})
