"""Fixed, consent-gated project metadata readers. Never reads conversations."""

import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import sqlite3
import time
from contextlib import closing

from control_everything.sessions.codex_desktop import read_desktop

PROVIDERS = ("codex", "claude", "opencode")


def local_path(value):
    return (type(value) is str and 1 <= len(value) <= 2048 and re.match(r"^[A-Za-z]:[\\/]", value) is not None
            and not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value))


def canonical_path(value):
    return ntpath.normcase(ntpath.normpath(value))


def discover_locations(provider, saved=None, *, home=None, environ=None):
    """Only stat fixed locations; environment suggestions never grant consent."""
    if provider not in PROVIDERS:
        raise ValueError("invalid_provider")
    home, environ = Path.home() if home is None else Path(home), os.environ if environ is None else environ
    items, seen, limited = [], set(), False
    def add(path, origin):
        nonlocal limited
        value = str(path)
        if not local_path(value):
            limited = True
            return
        key = canonical_path(value)
        if key not in seen:
            seen.add(key)
            if len(items) < 32:
                items.append({"path": value, "exists": Path(value).is_file(), "origin": origin})
            else:
                limited = True
    def enumerate_files(directory, patterns, origin):
        nonlocal limited
        if not local_path(str(directory)):
            limited = True
            return
        try:
            children = []
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= 4096:
                        limited = True
                        break
                    if any(re.fullmatch(pattern, entry.name) for pattern in patterns):
                        children.append(Path(entry.path))
            for child in sorted(children, key=lambda path: path.name):
                add(child, origin)
        except FileNotFoundError:
            pass
        except OSError:
            limited = True
    try:
        if saved:
            add(saved, "saved")
        if provider == "codex":
            if environ.get("CODEX_HOME"):
                enumerate_files(environ["CODEX_HOME"], (r"state_.*\.sqlite",), "CODEX_HOME")
            enumerate_files(home / ".codex", (r"state_.*\.sqlite",), "default")
        elif provider == "claude":
            add(home / ".claude.json", "default")
        else:
            directory = environ.get("XDG_DATA_HOME")
            if directory:
                if local_path(directory):
                    enumerate_files(Path(directory) / "opencode", (r"opencode(?:-[^/\\]+)?\.db",), "XDG_DATA_HOME")
                else:
                    limited = True
            else:
                enumerate_files(home / ".local/share/opencode", (r"opencode(?:-[^/\\]+)?\.db",), "default")
    except OSError:
        limited = True
    hint = environ.get("CLAUDE_CONFIG_DIR") if provider == "claude" else None
    if hint and not local_path(hint):
        limited, hint = True, None
    return {"items": items, "status": "limited" if limited else "available", "reason": "location_limited" if limited else None,
            "directory_hint": hint}


def result(status, reason=None, *, source_id=None, projects=None, complete=False):
    return {"status": status, "reason": reason, "source_id": source_id, "projects": projects or [], "complete": complete}


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("unsupported_format")
        value[key] = item
    return value


def _json(raw):
    value = json.loads(raw, object_pairs_hook=_unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("unsupported_format")))
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise ValueError("metadata_budget")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


def _project(provider, source_id, raw_id, paths, name=None):
    if not paths or len(paths) > 8 or any(not local_path(path) for path in paths):
        raise ValueError("invalid_metadata")
    if len({canonical_path(path) for path in paths}) != len(paths):
        raise ValueError("invalid_metadata")
    if type(name) is not str or not 1 <= len(name.strip()) <= 80 or any(ord(char) < 32 for char in name):
        name = ntpath.basename(paths[0].rstrip("\\/"))
    if not name or len(name) > 80:
        raise ValueError("invalid_metadata")
    prefix = "cl-" if provider == "claude" else "oc-"
    identity = prefix + hashlib.sha256((provider + "\0" + source_id + "\0" + raw_id).encode()).hexdigest()[:24]
    return {"id": identity, "name": name.strip(), "archived": False,
            "directories": [{"id": "main" if index == 0 else "d-" + hashlib.sha256(canonical_path(path).encode()).hexdigest()[:32],
                             "path": path} for index, path in enumerate(paths)],
            "source": {"kind": "none", "directory_id": None, "relative_path": None}}


def _claude(path, source_id):
    with path.open("rb") as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("metadata_budget")
    document = _json(raw.decode("utf-8"))
    if type(document) is not dict or type(document.get("projects")) is not dict:
        raise ValueError("unsupported_format")
    if len(document["projects"]) > 1000:
        raise ValueError("metadata_budget")
    projects, invalid = [], 0
    for project_path in sorted(document["projects"]):
        try:
            projects.append(_project("claude", canonical_path(str(path)), canonical_path(project_path), [project_path]))
        except ValueError:
            invalid += 1
    return result("limited" if invalid else "available", "invalid_metadata" if invalid else None,
                  source_id=source_id, projects=projects, complete=not invalid)


def _opencode(path, source_id):
    deadline = time.monotonic() + 2
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.25)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute("BEGIN")
        table = connection.execute("SELECT type FROM sqlite_master WHERE name='project'").fetchone()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(project)")}
        if table != ("table",) or not {"id", "worktree"} <= columns:
            raise ValueError("unsupported_format")
        def bounded(column, maximum):
            return f"CASE WHEN typeof({column})='text' THEN substr({column},1,{maximum + 1}) WHEN {column} IS NULL THEN NULL ELSE 0 END"
        rows = connection.execute("SELECT " + bounded('id', 2048) + "," + bounded('worktree', 2048) + "," +
            (bounded('name', 80) if "name" in columns else "NULL") + "," +
            (bounded('sandboxes', 32768) if "sandboxes" in columns else "NULL") +
            " FROM project ORDER BY id LIMIT 1001").fetchall()
    if len(rows) > 1000:
        raise ValueError("metadata_budget")
    projects, invalid, seen = [], 0, set()
    for identity, worktree, name, sandboxes in rows:
        try:
            if type(identity) is not str or not 1 <= len(identity) <= 2048 or identity in seen:
                raise ValueError("invalid_metadata")
            seen.add(identity)
            # Global entries do not denote a local project.
            if worktree in ("/", "", None) or identity == "global":
                continue
            extra = [] if sandboxes is None else _json(sandboxes)
            if type(extra) is not list or len(sandboxes or "") > 32768:
                raise ValueError("invalid_metadata")
            projects.append(_project("opencode", canonical_path(str(path)), identity, [worktree, *extra], name))
        except (ValueError, TypeError):
            invalid += 1
    return result("limited" if invalid else "available", "invalid_metadata" if invalid else None,
                  source_id=source_id, projects=projects, complete=not invalid)


def read_confirmed(provider, source):
    if provider not in PROVIDERS:
        raise ValueError("invalid_provider")
    if not source["enabled"]:
        return result("not_configured", "not_configured")
    path_value = source.get("path", source.get("db_path"))
    if not local_path(path_value):
        return result("unavailable", "invalid_path")
    source_id = hashlib.sha256(canonical_path(path_value).encode()).hexdigest()
    try:
        path = Path(path_value).resolve()
        if not local_path(str(path)):
            return result("unavailable", "invalid_path", source_id=source_id)
        if provider == "codex":
            value = read_desktop(path, projects_only=True)
            projects = []
            for row in value["projects"]:
                projects.append({"id": row["id"], "name": row["name"], "archived": False,
                    "directories": [{"id": "main" if root["role"] == "main" else "d-" + hashlib.sha256(canonical_path(root["path"]).encode()).hexdigest()[:32],
                                     "path": root["path"]} for root in row["directories"]],
                    "source": {"kind": "codex", "directory_id": None, "relative_path": None}})
            complete = value["available"] and value["complete"] and all(row.get('binding_available') for row in value['projects'])
            return result("available" if complete else "limited" if value["available"] else "unsupported_format" if value["reason"] == 'schema_unavailable' else "unavailable",
                          value["reason"], source_id=value["source_id"], projects=projects, complete=bool(complete))
        watched = [path, path.with_name(path.name + "-wal")] if provider == "opencode" else [path]
        def signatures():
            return [(item.stat().st_size, item.stat().st_mtime_ns) if item.exists() else None for item in watched]
        before = signatures()
        value = _claude(path, source_id) if provider == "claude" else _opencode(path, source_id)
        return value if before == signatures() else result("unavailable", "source_changed", source_id=source_id)
    except ValueError as error:
        reason = str(error) if str(error) in ("unsupported_format", "metadata_budget", "invalid_metadata") else "unsupported_format"
        return result("unsupported_format" if reason == "unsupported_format" else "limited", reason, source_id=source_id)
    except (OSError, UnicodeError, TypeError, RecursionError, sqlite3.Error):
        return result("unavailable", "source_unavailable", source_id=source_id)
