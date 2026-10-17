"""Explicit checkout, installed resource, and user data locations."""

from importlib.resources import files
import json
import os
from pathlib import Path
import sys


def source_root():
    package = Path(__file__).resolve().parent
    root = package.parent.parent
    if (package == root / "src" / "session_panel"
            and (root / "pyproject.toml").is_file()
            and (root / "web").is_dir() and (root / "native" / "observer").is_dir()):
        return root
    return None


def web_root():
    root = source_root()
    return root / "web" if root is not None else files("session_panel") / "_assets" / "web"


def default_profile_dir(*, environ=None, platform=None, home=None):
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else Path(home)
    if platform == "win32":
        base = Path(environ["LOCALAPPDATA"]) if environ.get("LOCALAPPDATA") else home / "AppData" / "Local"
        return (base / "SessionPanel").resolve()
    base = Path(environ["XDG_DATA_HOME"]) if environ.get("XDG_DATA_HOME") else home / ".local" / "share"
    return (base / "session-panel").resolve()


def legacy_data_root():
    root = source_root()
    return root if root is not None else default_profile_dir() / "legacy"


def execution_root():
    root = source_root()
    return root if root is not None else Path(__file__).resolve().parent


def observer_install_dir():
    root = source_root()
    if root is not None:
        return root / "work" / "native-observer" / "install"
    runtime = Path(sys.executable).resolve().parent
    product = runtime.parent
    marker = product / "product.json"
    if not marker.exists():
        return None
    package = Path(__file__).resolve().parent
    if (runtime.name != "runtime" or package != runtime / "Lib" / "site-packages" / "session_panel"
            or marker.is_symlink() or marker.is_junction() or marker.stat().st_size > 1024 * 1024):
        raise ValueError("invalid_product_location")
    manifest = json.loads(marker.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or type(manifest.get("schema")) is not int
            or manifest["schema"] != 1 or manifest.get("product") != "session-panel"
            or manifest.get("target") != "windows-x86_64"
            or not isinstance(manifest.get("native"), dict)
            or manifest["native"].get("relative") != "native/observer/session-panel-observer.exe"):
        raise ValueError("invalid_product_manifest")
    return product / "native" / "observer"


# Retained for callers that inspect the source checkout; never a writable installed data root.
REPOSITORY_ROOT = execution_root()
