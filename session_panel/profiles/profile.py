"""Per-user management profile and explicit metadata configuration."""

import argparse
import json
import math
import os
import sys
import tempfile
import threading
from pathlib import Path

from session_panel.sessions import codex_index
from session_panel.workspace.store import WorkspaceStore

DEFAULT_CONFIG = {"version": 1, "codex": {"enabled": False, "db_path": None}}
CONFIG_MAX_BYTES = 16384


class ProfileError(ValueError):
    def __init__(self, reason, field=None, *, status=422, message=None):
        super().__init__(reason)
        self.reason, self.field, self.status, self.message = reason, field, status, message


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_key")
        value[key] = item
    return value


def strict_json(data):
    def constant(_value):
        raise ValueError("invalid_number")
    value = json.loads(data, object_pairs_hook=unique_object, parse_constant=constant)
    def check(item):
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("invalid_number")
        if isinstance(item, dict):
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
    check(value)
    return value


def absolute_path(value):
    return isinstance(value, str) and 1 <= len(value) <= 2048 and "\x00" not in value and Path(value).is_absolute()


def resolve_data_dir(data_dir=None, *, environ=None, platform=None, home=None):
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else Path(home)
    selected = data_dir if data_dir is not None else environ.get("SESSION_PANEL_DATA_DIR") or None
    if selected is not None:
        if not absolute_path(str(selected)):
            raise ProfileError("invalid_config", "data_dir")
        return Path(selected).resolve()
    if platform == "win32":
        base = Path(environ["LOCALAPPDATA"]) if environ.get("LOCALAPPDATA") else home / "AppData" / "Local"
        return (base / "SessionPanel").resolve()
    base = Path(environ["XDG_DATA_HOME"]) if environ.get("XDG_DATA_HOME") else home / ".local" / "share"
    return (base / "session-panel").resolve()


def validate_config(config):
    if not isinstance(config, dict) or set(config) != {"version", "codex"} or type(config["version"]) is not int or config["version"] != 1:
        raise ProfileError("invalid_config", "version")
    codex = config["codex"]
    if (not isinstance(codex, dict) or set(codex) != {"enabled", "db_path"}
            or type(codex["enabled"]) is not bool
            or codex["db_path"] is not None and not absolute_path(codex["db_path"])
            or codex["enabled"] and codex["db_path"] is None):
        raise ProfileError("invalid_config", "codex")
    return config


class Profile:
    def __init__(self, directory):
        self.data_dir = Path(directory)
        self.config_path = self.data_dir / "profile.json"
        self.config_lock = threading.RLock()
        self.store = WorkspaceStore(self.data_dir / "workspace-state.sqlite3", profile=True)

    @classmethod
    def open(cls, data_dir=None):
        profile = cls(resolve_data_dir(data_dir))
        profile.data_dir.mkdir(parents=True, exist_ok=True)
        with profile.config_lock:
            if not profile.config_path.exists():
                profile._write_config(DEFAULT_CONFIG)
            profile.config()
        with profile.store.connection():
            pass
        return profile

    def config(self):
        with self.config_lock:
            try:
                with self.config_path.open("rb") as stream:
                    data = stream.read(CONFIG_MAX_BYTES + 1)
                if len(data) > CONFIG_MAX_BYTES:
                    raise ValueError("config_too_large")
                return validate_config(strict_json(data.decode("utf-8")))
            except (OSError, UnicodeError, ValueError, RecursionError):
                raise ProfileError("invalid_config", "profile.json") from None

    def _write_config(self, config):
        validate_config(config)
        data = (json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        if len(data) > CONFIG_MAX_BYTES:
            raise ProfileError("invalid_config", "profile.json")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.data_dir, prefix=".profile-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config_path)
        except OSError:
            raise ProfileError("storage_error", status=500) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def settings(self, body):
        if not isinstance(body, dict) or set(body) != {"codex"}:
            raise ProfileError("invalid_config", "codex")
        with self.config_lock:
            self.config()
            self._write_config({"version": 1, "codex": body["codex"]})
        return self.public()

    def codex_threads(self, offset=0):
        config = self.config()["codex"]
        value = codex_index.unavailable(offset)
        status = "not_configured"
        if config["enabled"]:
            try:
                if not Path(config["db_path"]).exists():
                    status = "missing"
                else:
                    value = codex_index.list_threads(offset, db_path=Path(config["db_path"]))
                    status = "available" if value["available"] else "error"
            except OSError:
                status = "error"
        value.update({"status": status, "reason": None if status == "available" else status})
        return value

    def public(self):
        receipt = self.store.import_receipt()
        empty = self.store.is_empty()
        return {"version": 1, "data_dir": str(self.data_dir), "config": self.config(),
                "codex_source": self.codex_threads(), "can_import": empty, "import_receipt": receipt,
                "import_reason": "already_imported" if receipt else None if empty else "profile_not_empty"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="恢复管理备份到另一个空目录")
    commands = parser.add_subparsers(dest="command", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--data-dir", required=True)
    args = parser.parse_args(argv)
    from session_panel.profiles.profile_import import restore_backup
    try:
        path = restore_backup(args.backup, args.data_dir)
    except (ProfileError, OSError, ValueError) as error:
        print(f"恢复失败：{error}", file=sys.stderr)
        return 1
    print(f'恢复目录：{path}\n启动：python -m session_panel.gateway --data-dir "{path}" --port 14401')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
