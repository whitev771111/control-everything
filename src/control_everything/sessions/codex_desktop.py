"""Bounded, read-only Codex desktop project and thread metadata snapshots."""

import hashlib
import json
import ntpath
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from control_everything.sessions.codex_index import KINDS, utc_timestamp
from control_everything.sessions.source import canonical_uuid

MAX_CONFIG_BYTES = 8 * 1024 * 1024
MAX_THREADS = 10000
MAX_PROJECTS = 1000
MAX_TITLE = 4096
QUERY_SECONDS = 1.0
QUERY_STEPS = 2000000
CONFIG_KEYS = ("local-projects", "thread-project-assignments", "projectless-thread-ids", "project-order",
               "app-server-project-id-by-legacy-project-id-by-host", "app-server-projects-migration-by-host")


def _empty(reason, source_id=None):
    return {"available": False, "reason": reason, "sampled_at": None, "source_id": source_id,
            "projects": [], "items": [], "coverage": {}, "complete": False}


def _text(value, maximum=4096, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not value and not empty) or "\0" in value:
        raise ValueError("invalid_metadata")
    value.encode("utf-8")
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_config")
        result[key] = value
    return result


def _signature(path):
    try:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
    except FileNotFoundError:
        return None


def _config(path):
    if not path.exists():
        return None
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("config_budget_exceeded")
    with path.open("rb") as stream:
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("config_budget_exceeded")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_config")))
    if not isinstance(value, dict):
        raise ValueError("invalid_config")
    return {key: value[key] for key in CONFIG_KEYS if key in value}


def _host_value(config, key, directory):
    hosts = config.get(key, {})
    if not isinstance(hosts, dict):
        raise ValueError("invalid_config")
    matches = [value for host, value in hosts.items()
               if isinstance(host, str) and host.startswith("local:")
               and ntpath.normcase(ntpath.normpath(host[6:])) == ntpath.normcase(ntpath.normpath(str(directory)))]
    if len(matches) > 1:
        raise ValueError("invalid_mapping")
    return matches[0] if matches else None


def _desktop_config(config, directory):
    if config is None:
        return {}, {}, set(), [], {}, None
    projects = config.get("local-projects", {})
    assignments = config.get("thread-project-assignments", {})
    projectless = config.get("projectless-thread-ids", [])
    order = config.get("project-order", [])
    mapping = _host_value(config, CONFIG_KEYS[4], directory)
    migration = _host_value(config, CONFIG_KEYS[5], directory)
    if not isinstance(projects, dict) or len(projects) > MAX_PROJECTS or not isinstance(assignments, dict) or len(assignments) > MAX_THREADS * 4:
        raise ValueError("invalid_config")
    if not isinstance(projectless, list) or len(projectless) > MAX_THREADS * 4 or not isinstance(order, list) or len(order) > MAX_PROJECTS:
        raise ValueError("invalid_config")
    if mapping is not None:
        if not isinstance(mapping, dict) or len(mapping) > MAX_PROJECTS:
            raise ValueError("invalid_mapping")
        for old, new in mapping.items():
            canonical_uuid(old)
            canonical_uuid(new)
        if len(set(mapping.values())) != len(mapping):
            raise ValueError("invalid_mapping")
    if migration is not None:
        if not isinstance(migration, dict) or type(migration.get("version")) is not int or migration["version"] != 1:
            raise ValueError("unsupported_migration")
        if any(type(migration.get(key)) is not bool for key in ("projectsMigrated", "threadAssignmentsMigrated")):
            raise ValueError("invalid_config")
    for identity, project in projects.items():
        canonical_uuid(identity)
        if not isinstance(project, dict) or project.get("id") != identity:
            raise ValueError("invalid_config")
        _text(project.get("name"), 512)
        roots = project.get("rootPaths")
        if not isinstance(roots, list) or len(roots) > 32:
            raise ValueError("invalid_config")
        for root in roots:
            _text(root)
    for identity, assignment in assignments.items():
        canonical_uuid(identity)
        if not isinstance(assignment, dict) or not isinstance(assignment.get("projectKind"), str):
            raise ValueError("invalid_config")
        if assignment["projectKind"] == "local":
            canonical_uuid(assignment.get("projectId"))
    for identity in projectless + order:
        canonical_uuid(identity)
    if len(set(order)) != len(order) or len(set(projectless)) != len(projectless):
        raise ValueError("invalid_config")
    return projects, assignments, set(projectless), order, mapping or {}, migration


def _columns(connection, table):
    return {row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")}


def _select_text(column, maximum):
    # Bound bytes materialized by SQLite while retaining invalid-type detection.
    return ("CASE WHEN " + column + " IS NULL THEN NULL WHEN typeof(" + column + ")='text' THEN substr("
            + column + ",1," + str(maximum + 1) + ") ELSE 0 END")


def _database(path):
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
    deadline = time.monotonic() + QUERY_SECONDS
    steps = 0
    def progress():
        nonlocal steps
        steps += 1000
        return int(steps > QUERY_STEPS or time.monotonic() > deadline)
    connection.set_progress_handler(progress, 1000)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        columns = _columns(connection, "threads")
        if not {"id", "cwd", "created_at", "updated_at", "archived", "source", "project_id"} <= columns:
            raise ValueError("schema_unavailable")
        counts = connection.execute("""SELECT COUNT(*), COALESCE(SUM(archived=1),0),
            COALESCE(SUM(substr(source,1,12)='{"subagent":'),0),
            COALESCE(SUM(source IS NULL OR (source NOT IN ('cli','exec','vscode')
                AND substr(source,1,12)!='{"subagent":')),0),
            COALESCE(SUM(source IN ('cli','exec','vscode')),0) FROM threads""").fetchone()
        optional = [_select_text(field, MAX_TITLE if field != "project_id" else 36) if field in columns else "NULL"
                    for field in ("title", "name", "project_id")]
        rows = connection.execute("SELECT " + _select_text("id", 36) + "," + _select_text("cwd", 4096)
                                  + ",created_at,updated_at,archived,source," + ",".join(optional)
                                  + " FROM threads WHERE source IN ('cli','exec','vscode')"
                                  + " ORDER BY updated_at DESC,id ASC LIMIT ?", (MAX_THREADS + 1,)).fetchall()
        if (not {"id", "name", "position"} <= _columns(connection, "projects")
                or not {"project_id", "position", "path"} <= _columns(connection, "project_roots")):
            raise ValueError("schema_unavailable")
        projects = connection.execute("SELECT " + _select_text("id", 36) + "," + _select_text("name", 512)
                                      + ",position FROM projects ORDER BY position,id LIMIT ?",
                                      (MAX_PROJECTS + 1,)).fetchall()
        roots = connection.execute("SELECT " + _select_text("project_id", 36) + ",position," + _select_text("path", 4096)
                                   + " FROM project_roots ORDER BY project_id,position LIMIT ?",
                                   (MAX_PROJECTS * 32 + 1,)).fetchall()
        return counts, rows, projects, roots, "project_id" in columns
    finally:
        connection.close()


def _merge(source_id, directory, config, database):
    legacy, assignments, projectless, order, mapping, migration = _desktop_config(config, directory)
    counts, rows, db_projects, db_roots, has_project_column = database
    complete = len(rows) <= MAX_THREADS and len(db_projects) <= MAX_PROJECTS and len(db_roots) <= MAX_PROJECTS * 32
    reason = None if complete else "row_budget_exceeded"
    if any(old not in legacy for old in mapping):
        raise ValueError("invalid_mapping")
    known = {}
    root_map = {}
    for identity, position, root in db_roots:
        canonical_uuid(identity)
        if type(position) is not int or position < 0:
            raise ValueError("invalid_metadata")
        root_map.setdefault(identity, []).append(_text(root))
    for identity, name, position in db_projects[:MAX_PROJECTS]:
        canonical_uuid(identity)
        if identity in known or type(position) is not int:
            raise ValueError("invalid_metadata")
        known[identity] = {"name": _text(name, 512), "roots": root_map.get(identity, []), "order": position, "binding": True}
    if any(target not in known for target in mapping.values()) or any(identity not in known for identity in root_map):
        raise ValueError("invalid_mapping")
    for old, project in legacy.items():
        identity = mapping.get(old, old)
        if identity not in known:
            if db_projects:
                # A missing cross-version mapping cannot create a second project.
                reason = reason or "mapping_incomplete"
                continue
            # Legacy identities remain visible but cannot create a durable binding before migration.
            known[identity] = {"name": project["name"], "roots": project["rootPaths"],
                               "order": order.index(old) if old in order else len(order), "binding": False}
        elif not migration or not migration["projectsMigrated"]:
            known[identity].update(name=project["name"], roots=project["rootPaths"],
                                   order=order.index(old) if old in order else len(order))
        if identity in known and not (migration and migration["projectsMigrated"] and migration["threadAssignmentsMigrated"]):
            known[identity]["order"] = order.index(old) if old in order else len(order)
    if len(known) > MAX_PROJECTS:
        raise ValueError("project_budget_exceeded")
    def external(identity):
        return "cx-" + hashlib.sha256((source_id + "\0" + identity).encode("utf-8")).hexdigest()[:24]
    projects = [{"id": external(identity), "codex_project_id": identity, "name": project["name"],
                 "directories": [{"path": root, "role": "main" if index == 0 else "extra"}
                                 for index, root in enumerate(project["roots"])],
                 "sort_order": project["order"], "archived": False, "read_only": True,
                 "binding_available": complete and project["binding"]}
                for identity, project in sorted(known.items(), key=lambda item: (item[1]["order"], item[0]))]
    items = []
    seen = set()
    migrated = config is None or bool(migration and migration["threadAssignmentsMigrated"])
    for identity, cwd, created, updated, archived, kind, title, name, db_project in rows[:MAX_THREADS]:
        canonical_uuid(identity)
        if identity in seen or type(archived) is not int or archived not in (0, 1) or kind not in KINDS:
            raise ValueError("invalid_metadata")
        seen.add(identity)
        _text(cwd, empty=True)
        for value in (title, name):
            if value is not None:
                _text(value, MAX_TITLE + 1, empty=True)
        display_title = name or title or ""
        if db_project is not None:
            canonical_uuid(db_project)
        assignment = assignments.get(identity)
        cfg_known = identity in projectless or assignment is not None
        cfg_project = None
        invalid_assignment = False
        if assignment is not None:
            if assignment["projectKind"] == "local":
                old = assignment["projectId"]
                cfg_project = mapping.get(old, old)
                invalid_assignment = cfg_project not in known
            else:
                invalid_assignment = True
        conflict = (invalid_assignment or (identity in projectless and assignment is not None)
                    or (cfg_known and db_project is not None and cfg_project != db_project)
                    or (migrated and cfg_project is not None and has_project_column and db_project is None)
                    or (db_project is not None and db_project not in known))
        selected = db_project if migrated and has_project_column else (cfg_project if cfg_known else db_project)
        if selected not in known:
            selected = None
        state = "conflict" if conflict else ("assigned" if selected else
                ("projectless" if (identity in projectless or (migrated and has_project_column and not assignment)) else "unknown"))
        items.append({"thread_id": identity, "session_key": "codex:" + identity, "cwd": cwd,
                      "created_at": utc_timestamp(created), "updated_at": utc_timestamp(updated),
                      "archived": bool(archived), "source_kind": kind, "activity_state": "unknown",
                      "task_completion": "unknown", "title": display_title[:MAX_TITLE],
                      "title_truncated": len(display_title) > MAX_TITLE,
                      "project_id": external(selected) if selected else None, "assignment_state": state,
                      "binding_available": bool(complete and state == "assigned" and known[selected]["binding"])})
    coverage = dict(zip(("total_count", "archived_count", "excluded_subagent_count", "excluded_other_source_count", "ordinary_count"), counts))
    coverage.update(loaded_count=len(items), assigned_count=sum(item["assignment_state"] == "assigned" for item in items),
                    projectless_count=sum(item["assignment_state"] == "projectless" for item in items),
                    unknown_count=sum(item["assignment_state"] == "unknown" for item in items),
                    conflict_count=sum(item["assignment_state"] == "conflict" for item in items))
    return {"available": True, "reason": reason, "sampled_at": datetime.now(timezone.utc).isoformat(),
            "source_id": source_id, "projects": projects, "items": items, "coverage": coverage, "complete": complete}


def read_desktop(db_path=None):
    """Read only explicitly selected local metadata; never discover a personal source."""
    if db_path is None:
        return _empty("source_disabled")
    source_id = None
    try:
        path = Path(db_path).resolve()
        source_id = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
        if not path.is_file():
            return _empty("source_unavailable", source_id)
        config_path = path.parent / ".codex-global-state.json"
        watched = (path, path.with_name(path.name + "-wal"), config_path)
        for _ in range(2):
            before = tuple(_signature(file) for file in watched)
            config = _config(config_path)
            database = _database(path)
            result = _merge(source_id, path.parent, config, database)
            if before == tuple(_signature(file) for file in watched):
                return result
        return _empty("source_changed", source_id)
    except sqlite3.OperationalError as error:
        return _empty("query_budget_exceeded" if "interrupted" in str(error) else "source_unavailable", source_id)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError, OSError, sqlite3.Error) as error:
        reason = str(error) if isinstance(error, ValueError) and str(error) in {
            "invalid_config", "invalid_mapping", "unsupported_migration", "schema_unavailable", "invalid_metadata",
            "config_budget_exceeded", "project_budget_exceeded"} else "source_unavailable"
        return _empty(reason, source_id)
