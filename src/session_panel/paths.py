"""Explicit checkout, installed resource, and user data locations."""

from importlib.resources import files
import hashlib
import json
import os
from pathlib import Path
import sys
import re


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


def _unlinked(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise ValueError("invalid_product_location")
    return path


def product_version_dir():
    """Locate only the fixed product; the installation layer validates its full closure."""
    runtime = Path(sys.executable).absolute().parent
    product = runtime.parent
    marker = product / "product.json"
    if not marker.exists() and not marker.is_symlink():
        return None
    package = Path(__file__).absolute().parent
    if (runtime.name != "runtime" or package != runtime / "Lib" / "site-packages" / "session_panel"
            or marker.stat().st_size > 16 * 1024 * 1024):
        raise ValueError("invalid_product_location")
    _unlinked(marker)
    _unlinked(package)
    _unlinked(sys.executable)
    return product


def observer_install_dir():
    product = product_version_dir()
    if product is None:
        root = source_root()
        return root / "work" / "native-observer" / "install" if root is not None else None
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("invalid_product_manifest")
            result[key] = value
        return result
    document = json.loads((product / "product.json").read_text(encoding="utf-8"), object_pairs_hook=pairs)
    if (type(document) is not dict or type(document.get("schema")) is not int
            or document["schema"] != 1 or document.get("product") != "session-panel"
            or document.get("target") != "windows-x86_64" or type(document.get("native")) is not dict
            or document["native"].get("relative") != "native/observer/session-panel-observer.exe"
            or type(document["native"].get("manifest_sha256")) is not str
            or not re.fullmatch('[0-9a-f]{64}', document["native"]["manifest_sha256"])):
        raise ValueError("invalid_product_manifest")
    native = _unlinked(product / "native/observer/manifest.json")
    if native.stat().st_size > 4096 or hashlib.sha256(native.read_bytes()).hexdigest() != document['native']['manifest_sha256']:
        raise ValueError("invalid_product_native_pin")
    return product / "native" / "observer"


# Retained for callers that inspect the source checkout; never a writable installed data root.
REPOSITORY_ROOT = execution_root()
