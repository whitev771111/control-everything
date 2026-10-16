"""Bounded read-only management import and offline backup restoration."""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from session_panel.profiles.profile import (CONFIG_MAX_BYTES, ProfileError, absolute_path,
                                          config_file_lock, strict_json, validate_config)
from session_panel.sessions.source import canonical_uuid
from session_panel.workspace.service import WorkspaceService, TASK_STATES, PROVIDERS, finite_number
from session_panel.workspace.store import (BASE_SCHEMA, MANAGEMENT_TABLES, WorkspaceError, WorkspaceStore,
                              now_utc, project_id_valid, validate_project)

TASK_COLUMNS = "id,title,project_id,provider_id,state,session_key,source,created_at,updated_at"
RECORD_COLUMNS = "id,provider_id,product,day,metric,value,currency,window_label,note,source,created_at,updated_at"
JSON_FILE_LIMIT = 16 * 1024 * 1024
JSON_TOTAL_LIMIT = 32 * 1024 * 1024
DECODED_LIMIT = 64 * 1024 * 1024
ROW_LIMITS = {"projects": 1000, "manual": 10000, "todos": 100000, "tasks": 100000, "manual_records": 100000}
EXCLUDED = ["execution", "approvals", "events", "private_messages", "personal_settings"]
BACKUP_FILES = {"management.sqlite3", "profile.json"}
ABSENT = object()


def invalid(field, message=None):
    raise ProfileError("invalid_import", field, message=message)


def contains(root, target):
    try:
        return os.path.commonpath((str(root), str(target))) == str(root)
    except ValueError:
        return False


def bounded_path(root, name, *, required=False):
    target = root / name
    resolved = target.resolve()
    if not contains(root, resolved):
        invalid(name, "path_outside_source")
    if not target.exists():
        if required:
            invalid(name, "missing")
        return None
    if not target.is_file():
        invalid(name, "not_a_file")
    return target


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def json_fingerprint(path):
    with path.open("rb") as stream:
        data = stream.read(JSON_FILE_LIMIT + 1)
    if len(data) > JSON_FILE_LIMIT:
        invalid(path.name, "json_budget_exceeded")
    return hashlib.sha256(data).hexdigest()


def timestamp(value, field):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
        if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone_required")
    except (ValueError, OverflowError):
        invalid(field, "invalid_timestamp")


def read_legacy_batch(source_dir, destination):
    if not absolute_path(source_dir):
        invalid("source_dir")
    root, destination = Path(source_dir).resolve(), Path(destination).resolve()
    if not root.is_dir() or contains(root, destination) or contains(destination, root):
        invalid("source_dir", "overlapping_or_missing_directory")
    batch = {"projects": [], "manual": [], "todos": [], "tasks": [], "manual_records": [],
             "fingerprints": {}, "missing_optional": [], "missing_tables": []}
    json_total, decoded_total = 0, 0

    def consume(category, row, count):
        nonlocal decoded_total
        if count > ROW_LIMITS[category]:
            invalid(category, f"row_budget_exceeded:{count}/{ROW_LIMITS[category]}")
        decoded_total += len(json.dumps(row, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if decoded_total > DECODED_LIMIT:
            invalid(category, "decoded_budget_exceeded")

    def read_json(name, required=False):
        nonlocal json_total
        path = bounded_path(root, name, required=required)
        if path is None:
            batch["missing_optional"].append(name)
            return ABSENT
        before = path.stat()
        with path.open("rb") as stream:
            identity = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino, before.st_size) != (identity.st_dev, identity.st_ino, identity.st_size):
                invalid(name, "source_changed")
            data = stream.read(JSON_FILE_LIMIT + 1)
        after = path.stat()
        json_total += len(data)
        if len(data) > JSON_FILE_LIMIT or json_total > JSON_TOTAL_LIMIT:
            invalid(name, "json_budget_exceeded")
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            invalid(name, "source_changed")
        fingerprint = hashlib.sha256(data).hexdigest()
        if json_fingerprint(path) != fingerprint:
            invalid(name, "source_changed")
        batch["fingerprints"][name] = fingerprint
        try:
            return strict_json(data.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            invalid(name, "invalid_json")

    registry = read_json("progress-registry.json", True)
    if not isinstance(registry, dict) or set(registry) != {"projects"} or not isinstance(registry["projects"], list):
        invalid("projects")
    ids, paths = set(), set()
    for index, row in enumerate(registry["projects"]):
        field = f"projects[{index}]"
        if not isinstance(row, dict) or set(row) != {"id", "name", "root"} or not project_id_valid(row["id"]) or row["id"] in ids:
            invalid(field)
        try:
            item = validate_project({"id": row["id"], "name": row["name"], "archived": False,
                                     "directories": [{"id": "main", "path": row["root"]}],
                                     "source": {"kind": "none", "directory_id": None, "relative_path": None}})
        except ProfileError:
            invalid(field)
        key = item["directories"][0]["path_key"]
        if key in paths:
            invalid(field, "directory_conflict")
        ids.add(row["id"])
        paths.add(key)
        consume("projects", row, index + 1)
        batch["projects"].append(item)
    manual = read_json("progress-manual.json")
    if manual is not ABSENT:
        if not isinstance(manual, dict):
            invalid("manual")
        for index, (identity, row) in enumerate(manual.items()):
            if (not project_id_valid(identity) or not isinstance(row, dict) or set(row) != {"state", "note", "percent"}
                    or row["state"] not in ("未开始", "在做", "等我", "完成") or not isinstance(row["note"], str)
                    or len(row["note"]) > 1000 or row["percent"] is not None and
                    (not finite_number(row["percent"]) or not 0 <= row["percent"] <= 100)):
                invalid(f"manual[{index}]")
            consume("manual", {"project_id": identity, **row}, index + 1)
            batch["manual"].append({"project_id": identity, **row})
    todos = read_json("progress-todos.json")
    if todos is not ABSENT:
        if not isinstance(todos, list):
            invalid("todos")
        seen = set()
        for index, row in enumerate(todos):
            if (not isinstance(row, dict) or set(row) != {"id", "text", "project_id", "done"}
                    or not isinstance(row["id"], str) or len(row["id"]) > 128 or row["id"] in seen
                    or not isinstance(row["text"], str) or not row["text"].strip() or len(row["text"]) > 2000
                    or not isinstance(row["project_id"], str) or row["project_id"] and not project_id_valid(row["project_id"])
                    or type(row["done"]) is not bool):
                invalid(f"todos[{index}]")
            seen.add(row["id"])
            consume("todos", row, index + 1)
            batch["todos"].append(row)

    source_db = bounded_path(root, "workspace-state.sqlite3")
    if source_db is None:
        batch["missing_optional"].append("workspace-state.sqlite3")
    else:
        connection = None
        expected = sqlite3.connect(":memory:")
        expected.executescript(BASE_SCHEMA)
        try:
            connection = sqlite3.connect(source_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            for table, columns in (("tasks", TASK_COLUMNS), ("manual_records", RECORD_COLUMNS)):
                schema = [tuple(row) for row in connection.execute(f"PRAGMA table_info({table})")]
                if not schema:
                    batch["missing_tables"].append(table)
                    continue
                if schema != [tuple(row) for row in expected.execute(f"PRAGMA table_info({table})")]:
                    invalid(table, "incompatible_schema")
                seen = set()
                for index, raw in enumerate(connection.execute(f"SELECT {columns} FROM {table}")):
                    row = dict(raw)
                    field = f"{table}[{index}]"
                    try:
                        canonical_uuid(row["id"])
                        if row["id"] in seen or row["source"] != "manual":
                            raise ValueError("invalid_record")
                        if table == "tasks":
                            if (not isinstance(row["title"], str) or not 1 <= len(row["title"].strip()) <= 160
                                    or not project_id_valid(row["project_id"]) or row["provider_id"] not in (*PROVIDERS, None)
                                    or row["state"] not in TASK_STATES):
                                raise ValueError("invalid_task")
                            if row["session_key"] is not None:
                                if not isinstance(row["session_key"], str) or not row["session_key"].startswith("codex:") or row["provider_id"] != "codex":
                                    raise ValueError("invalid_key")
                                canonical_uuid(row["session_key"][6:])
                        else:
                            fields = {key: value for key, value in row.items() if key not in ("source", "created_at", "updated_at")}
                            WorkspaceService.__new__(WorkspaceService).save_record(fields, _validate_only=True)
                        timestamp(row["created_at"], field + ".created_at")
                        timestamp(row["updated_at"], field + ".updated_at")
                    except (ValueError, TypeError, OverflowError):
                        invalid(field)
                    seen.add(row["id"])
                    consume(table, row, index + 1)
                    batch[table].append(row)
                canonical = sorted(batch[table], key=lambda row: row["id"])
                batch["fingerprints"][table + "_snapshot"] = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        except sqlite3.Error:
            invalid("workspace-state.sqlite3", "source_unavailable")
        finally:
            expected.close()
            if connection is not None:
                connection.close()
    batch["counts"] = {key: len(batch[key]) for key in ROW_LIMITS}
    return batch


class BackupTimeout(Exception):
    pass


def backup_progress(deadline):
    def progress(_status, _remaining, _total):
        if time.monotonic() >= deadline:
            raise BackupTimeout("backup_timeout")
    return progress


def create_backup(profile):
    parent = profile.data_dir / "backups"
    parent.mkdir(exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=parent))
    reader = target = None
    try:
        reader = sqlite3.connect(profile.store.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0)
        reader.execute("PRAGMA query_only=ON")
        reader.execute("PRAGMA busy_timeout=0")
        target = sqlite3.connect(temporary / "management.sqlite3", timeout=0)
        target.execute("PRAGMA busy_timeout=0")
        reader.backup(target, pages=64, progress=backup_progress(time.monotonic() + 5), sleep=0.01)
        target.close()
        target = None
        reader.close()
        reader = None
        (temporary / "profile.json").write_text(json.dumps(profile.config(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        connection = sqlite3.connect(temporary / "management.sqlite3")
        try:
            counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in MANAGEMENT_TABLES}
        finally:
            connection.close()
        manifest = {"version": 1, "schema_version": 1, "created_at": now_utc(), "origin_data_dir": str(profile.data_dir),
                    "files": {name: {"size": (temporary / name).stat().st_size, "sha256": digest(temporary / name)} for name in BACKUP_FILES}, "counts": counts}
        (temporary / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        validate_backup(temporary)
        published = parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12])
        temporary.rename(published)
        return published
    except Exception as error:
        code = "backup_timeout" if isinstance(error, BackupTimeout) else "backup_error"
        raise ProfileError("backup_failed", status=500, message=code) from error
    finally:
        if target is not None:
            target.close()
        if reader is not None:
            reader.close()
        if temporary.exists():
            # Failed backup files remain visibly separate from published backups.
            try:
                (temporary / "FAILED").write_text("backup_failed\n", encoding="utf-8")
            except OSError:
                pass


def insert_batch(connection, batch, receipt):
    for position, project in enumerate(batch["projects"]):
        identity = project["id"]
        connection.execute("INSERT INTO projects VALUES(?,?,?,?,?,?)", (identity, project["name"], 0, None, None, position))
        binding = project["directories"][0]
        connection.execute("INSERT INTO project_directories VALUES(?,?,?,?,?)", (identity, "main", binding["path"], binding["path_key"], 1))
        connection.execute("INSERT INTO project_sources VALUES(?,?,?,?)", (identity, "none", None, None))
    for manual in batch["manual"]:
        values = tuple(manual[key] for key in ("project_id", "state", "percent", "note"))
        connection.execute("INSERT INTO project_manual VALUES(?,?,?,?,?)", (*values, None))
        connection.execute("INSERT INTO project_manual_history VALUES(?,?,?,?,?,?,?)", (str(uuid4()), *values, None, "import"))
    for position, todo in enumerate(batch["todos"]):
        connection.execute("INSERT INTO todos VALUES(?,?,?,?,?,?,?)", (todo["id"], todo["project_id"], todo["text"], int(todo["done"]), None, None, position))
    for table, columns in (("tasks", TASK_COLUMNS), ("manual_records", RECORD_COLUMNS)):
        names = columns.split(",")
        connection.executemany(f"INSERT INTO {table}({columns}) VALUES({','.join('?' for _ in names)})", [tuple(row[key] for key in names) for row in batch[table]])
    connection.execute("INSERT INTO profile_imports VALUES(?,?,?,?,?,?)", (receipt["id"], receipt["imported_at"], receipt["source_path"],
                       json.dumps(receipt["source_fingerprints"], sort_keys=True), json.dumps(receipt["counts"], sort_keys=True), receipt["backup_path"]))


def import_legacy(profile, source_dir):
    try:
        batch = read_legacy_batch(source_dir, profile.data_dir)
    except (OSError, UnicodeError, RecursionError):
        invalid("source_dir", "source_unavailable")
    writer = None
    with profile.config_lock, config_file_lock(profile.data_dir), profile.store.write_lock:
        try:
            writer = sqlite3.connect(profile.store.path, isolation_level=None, timeout=5)
            writer.execute("PRAGMA busy_timeout=5000")
            writer.execute("PRAGMA foreign_keys=ON")
            writer.execute("BEGIN IMMEDIATE")
            if writer.execute("SELECT COUNT(*) FROM profile_imports").fetchone()[0]:
                raise ProfileError("already_imported", status=409)
            if not profile.store.is_empty(writer):
                raise ProfileError("profile_not_empty", status=409)
            backup = create_backup(profile)
            # The SQLite batch is a committed read snapshot; JSON inputs must
            # still match the validated bytes before this transaction writes.
            root = Path(source_dir).resolve()
            for name, fingerprint in batch["fingerprints"].items():
                if not name.endswith(".json"):
                    continue
                path = bounded_path(root, name, required=True)
                with path.open("rb") as stream:
                    data = stream.read(JSON_FILE_LIMIT + 1)
                if len(data) > JSON_FILE_LIMIT or hashlib.sha256(data).hexdigest() != fingerprint:
                    invalid(name, "source_changed")
            receipt = {"id": str(uuid4()), "imported_at": now_utc(), "source_path": str(Path(source_dir).resolve()),
                       "source_fingerprints": batch["fingerprints"], "counts": batch["counts"], "backup_path": str(backup)}
            insert_batch(writer, batch, receipt)
            writer.execute("COMMIT")
            return {"status": "ok", "counts": batch["counts"], "backup_path": str(backup), "receipt": receipt,
                    "excluded_domains": EXCLUDED, "missing_optional": batch["missing_optional"], "missing_tables": batch["missing_tables"]}
        except sqlite3.OperationalError as error:
            busy = "locked" in str(error) or "busy" in str(error)
            raise ProfileError("store_busy" if busy else "storage_error", status=409 if busy else 500) from error
        except sqlite3.Error as error:
            raise ProfileError("storage_error", status=500) from error
        finally:
            if writer is not None:
                if writer.in_transaction:
                    writer.execute("ROLLBACK")
                writer.close()


def validate_backup(directory):
    root = Path(directory).resolve()
    try:
        with os.scandir(root) as entries:
            names = set()
            for index, entry in enumerate(entries):
                if index >= 3:
                    invalid("backup", "unexpected_file")
                names.add(entry.name)
        if names != BACKUP_FILES | {"manifest.json"}:
            invalid("backup", "unexpected_file")
        manifest_path = bounded_path(root, "manifest.json", required=True)
        if manifest_path.stat().st_size > 65536:
            invalid("manifest", "budget_exceeded")
        manifest = strict_json(manifest_path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or set(manifest) != {"version", "schema_version", "created_at", "origin_data_dir", "files", "counts"}
                or type(manifest["version"]) is not int or manifest["version"] != 1
                or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
                or not isinstance(manifest["files"], dict) or set(manifest["files"]) != BACKUP_FILES
                or not isinstance(manifest["counts"], dict) or set(manifest["counts"]) != set(MANAGEMENT_TABLES)
                or not absolute_path(manifest["origin_data_dir"])):
            invalid("manifest")
        timestamp(manifest["created_at"], "manifest.created_at")
        for name in BACKUP_FILES:
            path = bounded_path(root, name, required=True)
            metadata = manifest["files"][name]
            if (not isinstance(metadata, dict) or set(metadata) != {"size", "sha256"}
                    or type(metadata["size"]) is not int or metadata["size"] < 0
                    or path.stat().st_size != metadata["size"] or digest(path) != metadata["sha256"]):
                invalid("manifest.files." + name)
        config_path = root / "profile.json"
        if config_path.stat().st_size > CONFIG_MAX_BYTES:
            invalid("profile.json")
        validate_config(strict_json(config_path.read_text(encoding="utf-8")))
        connection = sqlite3.connect((root / "management.sqlite3").as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                invalid("management.sqlite3")
            WorkspaceStore(root / "management.sqlite3", profile=True).validate_schema(connection)
            for table in MANAGEMENT_TABLES:
                count = manifest["counts"][table]
                if type(count) is not int or count < 0 or connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != count:
                    invalid("manifest.counts." + table)
        finally:
            connection.close()
        return manifest
    except (OSError, sqlite3.Error, WorkspaceError, ValueError, TypeError, KeyError, RecursionError):
        invalid("backup", "invalid_backup")


def restore_backup(backup_dir, data_dir):
    if not absolute_path(backup_dir) or not absolute_path(data_dir):
        invalid("data_dir")
    backup, target = Path(backup_dir).resolve(), Path(data_dir).resolve()
    if contains(backup, target) or contains(target, backup):
        invalid("data_dir", "overlapping_directory")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ProfileError("profile_not_empty", "data_dir", status=409)
    manifest = validate_backup(backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".profile-restore-", dir=target.parent))
    try:
        shutil.copyfile(backup / "profile.json", temporary / "profile.json")
        shutil.copyfile(backup / "management.sqlite3", temporary / "workspace-state.sqlite3")
        for original, copied in (("profile.json", "profile.json"), ("management.sqlite3", "workspace-state.sqlite3")):
            if digest(temporary / copied) != manifest["files"][original]["sha256"]:
                invalid("backup", "source_changed")
        # Recheck source before publishing; restoration never overwrites a profile.
        validate_backup(backup)
        if target.exists():
            target.rmdir()
        temporary.rename(target)
        return target
    finally:
        if temporary.exists():
            if temporary.resolve().parent != target.parent.resolve() or not temporary.name.startswith(".profile-restore-"):
                raise ProfileError("storage_error", status=500)
            shutil.rmtree(temporary)
