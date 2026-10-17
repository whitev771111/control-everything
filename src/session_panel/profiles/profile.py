"""Per-user management profile and explicit metadata configuration."""

import argparse
import copy
import json
import math
import os
import sys
import tempfile
import threading
from uuid import uuid4
from contextlib import contextmanager
from pathlib import Path

from session_panel.input_validation import ProfileError, absolute_path
from session_panel.paths import default_profile_dir
from session_panel.sessions import codex_index
from session_panel.sessions import observation_settings as observations
from session_panel.workspace.store import WorkspaceStore, WorkspaceError
from session_panel.profiles.integration_settings import (
    EMPTY_INTEGRATIONS, InvalidSettings, integration_summary, validate_integrations,
)

DEFAULT_CONFIG = {"version": 2, "codex": {"enabled": False, "db_path": None}, "integrations": EMPTY_INTEGRATIONS}
CONFIG_MAX_BYTES = 65536


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


def resolve_data_dir(data_dir=None, *, environ=None, platform=None, home=None):
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else Path(home)
    selected = data_dir if data_dir is not None else environ.get("SESSION_PANEL_DATA_DIR") or None
    if selected is not None:
        if not absolute_path(str(selected)):
            raise ProfileError("invalid_config", "data_dir")
        return Path(selected).resolve()
    return default_profile_dir(environ=environ, platform=platform, home=home)


def validate_config(config):
    if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] not in (1, 2, 3):
        raise ProfileError("invalid_config", "version")
    expected = {"version", "codex"} if config["version"] == 1 else {"version", "codex", "integrations"}
    if config["version"] == 3:
        expected.add("observations")
    if set(config) != expected:
        raise ProfileError("invalid_config", "version")
    codex = config["codex"]
    if (not isinstance(codex, dict) or set(codex) != {"enabled", "db_path"}
            or type(codex["enabled"]) is not bool
            or codex["db_path"] is not None and not absolute_path(codex["db_path"])
            or codex["enabled"] and codex["db_path"] is None):
        raise ProfileError("invalid_config", "codex")
    if config["version"] >= 2:
        checked_integrations(config["integrations"])
    if config["version"] == 3:
        observations.validate(config["observations"])
        if config["observations"]["revision"] == 0:
            raise ProfileError("invalid_config", "observations.revision")
    return config


def checked_integrations(value):
    try:
        return validate_integrations(value)
    except InvalidSettings as error:
        raise ProfileError("invalid_config", error.field, message=error.message) from None


def normalized_config(config):
    validate_config(config)
    result = copy.deepcopy(config)
    result["version"] = max(2, result["version"])
    result.setdefault("integrations", copy.deepcopy(EMPTY_INTEGRATIONS))
    return result


def validate_settings_document(document):
    if (not isinstance(document, dict) or set(document) != {"version", "integrations"}
            or type(document["version"]) is not int or document["version"] != 2):
        raise ProfileError("invalid_config", "version")
    return {"version": 2, "integrations": checked_integrations(document["integrations"])}


def read_settings_document(filename):
    if not absolute_path(str(filename)):
        raise ProfileError("invalid_config", "file")
    try:
        with Path(filename).open("rb") as stream:
            raw = stream.read(CONFIG_MAX_BYTES + 1)
        if len(raw) > CONFIG_MAX_BYTES:
            raise ValueError("config_too_large")
        value = strict_json(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ProfileError("invalid_config", "file") from None
    return validate_settings_document(value)


@contextmanager
def config_file_lock(directory):
    """Lock a persistent byte; never unlink it or recursively open a profile."""
    try:
        stream = (Path(directory) / ".profile.lock").open("a+b")
    except OSError:
        raise ProfileError("storage_error", status=500) from None
    locked = False
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise ProfileError("config_busy", status=409) from None
        yield
    finally:
        try:
            if locked:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


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
        with profile.config_lock, config_file_lock(profile.data_dir):
            if not profile.config_path.exists():
                profile._write_config(DEFAULT_CONFIG)
            profile.config()
        try:
            with profile.store.connection():
                pass
        except WorkspaceError as error:
            if str(error) == 'migration_required':
                raise ProfileError('migration_required', status=409, message='migrate-tasks') from error
            reason = str(error)
            raise ProfileError(reason, status=409 if reason == 'store_busy' else
                               422 if reason == 'schema_error' else 500) from error
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
        with self.config_lock, config_file_lock(self.data_dir):
            original = self.config()
            config = normalized_config(original)
            config["codex"] = body["codex"]
            validate_config(config)
            if original["version"] == 1:
                from session_panel.profiles.profile_import import create_backup
                create_backup(self)
            self._write_config(config)
        return self.public()

    @contextmanager
    def observation_guard(self):
        with self.config_lock, config_file_lock(self.data_dir):
            yield

    def observation_config(self):
        return observations.from_config(self.config())

    def observation_settings(self):
        value = self.observation_config()
        return {"revision": value["revision"], "hash": observations.pair(value)["config_hash"],
                "registrations": value["registrations"]}

    def save_observations(self, body, *, on_commit=None, duplicate_ids=()):
        if (type(body) is not dict or set(body) != {"expected_revision", "expected_hash", "registrations"}
                or type(body["expected_revision"]) is not int or type(body["expected_hash"]) is not str
                or type(body["registrations"]) is not list):
            raise ProfileError("invalid_config", "observations")
        with self.config_lock, config_file_lock(self.data_dir):
            original = self.config()
            current = observations.from_config(original)
            if (body["expected_revision"] != current["revision"]
                    or body["expected_hash"] != observations.pair(current)["config_hash"]):
                raise ProfileError("config_conflict", "observations", status=409)
            rows = copy.deepcopy(body["registrations"])
            for row in rows:
                if type(row) is dict and row.get("id", False) is None:
                    row["id"] = uuid4().hex
            proposed = observations.validate({"revision": max(1, current["revision"]), "registrations": rows})
            if any(row["id"] in duplicate_ids for row in proposed["registrations"]):
                raise ProfileError("duplicate_installation", "observations", status=409)
            if proposed["registrations"] == current["registrations"]:
                return self.observation_settings()
            if current["revision"] == observations.MAX_REVISION:
                raise ProfileError("invalid_config", "observations.revision")
            proposed["revision"] = current["revision"] + 1
            config = normalized_config(original)
            config.update(version=3, observations=proposed)
            validate_config(config)
            if original["version"] < 3:
                from session_panel.profiles.profile_import import create_backup
                create_backup(self)
            self._write_config(config)
            if on_commit:
                on_commit(proposed)
            return self.observation_settings()

    def import_settings(self, document):
        update = validate_settings_document(document)
        from session_panel.profiles.profile_import import create_backup
        with self.config_lock, config_file_lock(self.data_dir):
            config = normalized_config(self.config())
            config["integrations"] = update["integrations"]
            validate_config(config)
            backup = create_backup(self)
            self._write_config(config)
        return {"status": "ok", "backup_path": str(backup), "restart_required": True,
                "integrations": integration_summary(config["integrations"])}

    def integrations(self):
        return normalized_config(self.config())["integrations"]

    def status(self):
        return {"data_dir": str(self.data_dir), "integrations": integration_summary(self.integrations())}

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

    def public(self, *, startup_integrations=None):
        receipt = self.store.import_receipt()
        empty = self.store.is_empty()
        config = self.config()
        integrations = normalized_config(config)["integrations"]
        return {"version": 1, "data_dir": str(self.data_dir),
                "config": {"version": config["version"], "codex": copy.deepcopy(config["codex"])},
                "integrations": integration_summary(integrations),
                "restart_required": startup_integrations is not None and integrations != startup_integrations,
                "codex_source": self.codex_threads(), "can_import": empty, "import_receipt": receipt,
                "import_reason": "already_imported" if receipt else None if empty else "profile_not_empty"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="初始化本机工作台、检查或导入接入设置、恢复管理备份")
    commands = parser.add_subparsers(dest="command", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--data-dir", required=True)
    for name in ('migrate-tasks', 'preserve-tasks'):
        command = commands.add_parser(name)
        command.add_argument('--data-dir', required=True)
    for name in ("init", "status", "import-settings", "validate-settings"):
        command = commands.add_parser(name)
        if name != "validate-settings":
            command.add_argument("--data-dir")
        if name in ("import-settings", "validate-settings"):
            command.add_argument("--file", required=True)
    args = parser.parse_args(argv)
    from session_panel.profiles.profile_import import restore_backup
    try:
        if args.command == "restore":
            path = restore_backup(args.backup, args.data_dir)
            from session_panel.profiles.task_migration import migration_status
            state = migration_status(str(path))
            print(json.dumps({'restored': str(path), **state}, ensure_ascii=False))
            if state['schema_version'] == 1:
                print(f'先显式迁移：python -m session_panel.profile migrate-tasks --data-dir "{path}"')
            else:
                print(f'启动：python -m session_panel.gateway --data-dir "{path}" --port 14401')
        elif args.command in ('migrate-tasks', 'preserve-tasks'):
            from session_panel.profiles.task_migration import migrate_tasks, preserve_tasks
            print(json.dumps((migrate_tasks if args.command == 'migrate-tasks' else preserve_tasks)(args.data_dir), ensure_ascii=False))
        elif args.command == "validate-settings":
            document = read_settings_document(args.file)
            print(json.dumps({"status": "valid", "integrations": integration_summary(document["integrations"])}, ensure_ascii=False))
        else:
            document = read_settings_document(args.file) if args.command == "import-settings" else None
            if args.command == 'status':
                from session_panel.profiles.task_migration import migration_status
                state = migration_status(args.data_dir)
                value = state if state['schema_version'] == 1 else Profile.open(args.data_dir).status()
            else:
                profile = Profile.open(args.data_dir)
                value = profile.import_settings(document) if document is not None else profile.status()
            print(json.dumps(value, ensure_ascii=False))
            if args.command in ("init", "import-settings"):
                print(f'启动或重启后生效：python -m session_panel.gateway --data-dir "{profile.data_dir}"')
    except (ProfileError, WorkspaceError, OSError, ValueError) as error:
        detail = ({"reason": error.reason, "field": error.field, "message": error.message}
                  if isinstance(error, ProfileError) else {"reason": str(error) if isinstance(error, WorkspaceError) else "storage_error"})
        for field in ('phase', 'backup', 'committed'):
            if hasattr(error, field):
                detail[field] = getattr(error, field)
        print(json.dumps(detail, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
