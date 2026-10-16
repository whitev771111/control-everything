"""Transactions for workspace-owned tasks and manual usage observations."""

import sqlite3
import threading
import json
import re
import ntpath
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

MANAGEMENT_SCHEMA = """CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, archived INTEGER NOT NULL,
 created_at TEXT, updated_at TEXT, position INTEGER NOT NULL);
CREATE TABLE project_directories(project_id TEXT NOT NULL REFERENCES projects(id), binding_id TEXT NOT NULL,
 path TEXT NOT NULL, path_key TEXT NOT NULL, is_primary INTEGER NOT NULL, PRIMARY KEY(project_id,binding_id));
CREATE TABLE project_sources(project_id TEXT PRIMARY KEY REFERENCES projects(id), kind TEXT NOT NULL,
 directory_id TEXT, relative_path TEXT);
CREATE TABLE project_manual(project_id TEXT PRIMARY KEY, state TEXT NOT NULL, percent NUMERIC, note TEXT NOT NULL, updated_at TEXT);
CREATE TABLE project_manual_history(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, state TEXT NOT NULL,
 percent NUMERIC, note TEXT NOT NULL, recorded_at TEXT, origin TEXT NOT NULL);
CREATE TABLE todos(id TEXT PRIMARY KEY, project_id TEXT, text TEXT NOT NULL, done INTEGER NOT NULL,
 created_at TEXT, updated_at TEXT, position INTEGER NOT NULL);
CREATE TABLE profile_imports(id TEXT PRIMARY KEY, imported_at TEXT NOT NULL, source_path TEXT NOT NULL,
 source_fingerprints_json TEXT NOT NULL, counts_json TEXT NOT NULL, backup_path TEXT NOT NULL);
"""
BASE_SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, project_id TEXT NOT NULL,
 provider_id TEXT, state TEXT NOT NULL, session_key TEXT, source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE manual_records (id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, product TEXT NOT NULL,
 day TEXT NOT NULL, metric TEXT NOT NULL, value NUMERIC NOT NULL, currency TEXT, window_label TEXT,
 note TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""
MANAGEMENT_TABLES = ("projects", "project_directories", "project_sources", "project_manual", "project_manual_history",
                     "todos", "tasks", "manual_records", "profile_imports")


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def project_id_valid(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value) is not None


def path_key(value):
    value = ntpath.normcase(ntpath.normpath(value))
    if value.startswith("\\\\?\\unc\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\") and re.match(r"[a-z]:\\", value[4:]):
        return value[4:]
    return value


def validate_project(body, *, restore=False):
    from session_panel.profiles.profile import ProfileError, absolute_path
    fields = {"project_id", "name", "directories", "source"} if restore else {"id", "name", "archived", "directories", "source"}
    def invalid(field, reason="invalid_project"):
        raise ProfileError(reason, field)
    if not isinstance(body, dict) or set(body) != fields:
        invalid("project")
    identity = body["project_id"] if restore else body["id"]
    if identity is not None and not project_id_valid(identity):
        invalid("id")
    if restore and identity is None:
        invalid("project_id")
    name = body["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
        invalid("name")
    archived = False if restore else body["archived"]
    if type(archived) is not bool or identity is None and archived:
        invalid("archived")
    directories = body["directories"]
    if not isinstance(directories, list) or not 1 <= len(directories) <= 8:
        invalid("directories")
    ids, paths, checked = set(), set(), []
    for index, binding in enumerate(directories):
        field = f"directories[{index}]"
        if not isinstance(binding, dict) or set(binding) != {"id", "path"}:
            invalid(field)
        key, path = binding["id"], binding["path"]
        if (not isinstance(key, str) or key != "main" and not re.fullmatch(r"d-[0-9a-f]{32}", key)
                or key in ids or not absolute_path(path)):
            invalid(field)
        normalized = path_key(path)
        if normalized in paths:
            invalid(field + ".path")
        ids.add(key)
        paths.add(normalized)
        checked.append({"id": key, "path": path, "path_key": normalized})
    if "main" not in ids:
        invalid("directories.main")
    source = body["source"]
    if not isinstance(source, dict) or set(source) != {"kind", "directory_id", "relative_path"}:
        invalid("source", "invalid_source")
    kind, binding, relative = (source[key] for key in ("kind", "directory_id", "relative_path"))
    if kind not in ("none", "wfc", "text", "json"):
        invalid("source.kind", "invalid_source")
    if kind == "none":
        if binding is not None or relative is not None:
            invalid("source", "invalid_source")
    else:
        if not isinstance(binding, str) or binding not in ids:
            invalid("source.directory_id", "source_binding_missing")
        if (not isinstance(relative, str) or not 1 <= len(relative) <= 512 or "\x00" in relative
                or "\\" in relative or ":" in relative or relative.startswith("/")
                or ".." in relative.split("/") or kind == "wfc" and relative != "docs/current"):
            invalid("source.relative_path", "invalid_source")
    return {"id": identity, "name": name.strip(), "archived": archived, "directories": checked, "source": source}


class WorkspaceError(Exception):
    """A public, fixed reason without storage or account details."""


class WorkspaceStore:
    def __init__(self, db_path, *, profile=False):
        self.path = Path(db_path)
        self._initialized = False
        self._schema_lock = threading.Lock()
        self.profile = profile
        self.write_lock = threading.RLock()

    def validate_schema(self, connection):
        # Compare declared columns, types, nullability and primary keys exactly.
        expected = sqlite3.connect(":memory:")
        try:
            expected.executescript(BASE_SCHEMA + MANAGEMENT_SCHEMA)
            present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if present != set(MANAGEMENT_TABLES):
                raise WorkspaceError("schema_error")
            for table in MANAGEMENT_TABLES:
                actual = [tuple(row) for row in connection.execute(f"PRAGMA table_info({table})")]
                target = [tuple(row) for row in expected.execute(f"PRAGMA table_info({table})")]
                if actual != target:
                    raise WorkspaceError("schema_error")
        finally:
            expected.close()

    @contextmanager
    def connection(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5 if self.profile else 3)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000" if self.profile else "PRAGMA busy_timeout=3000")
            if self.profile:
                connection.execute("PRAGMA foreign_keys=ON")
            with self._schema_lock:
                if not self._initialized:
                    if self.profile:
                        version = connection.execute("PRAGMA user_version").fetchone()[0]
                        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                        if version == 0 and not tables:
                            connection.executescript("BEGIN IMMEDIATE;" + BASE_SCHEMA + MANAGEMENT_SCHEMA + "PRAGMA user_version=1; COMMIT;")
                        elif version != 1:
                            raise WorkspaceError("schema_error")
                        self.validate_schema(connection)
                    else:
                        connection.executescript("""
                        CREATE TABLE IF NOT EXISTS tasks (
                            id TEXT PRIMARY KEY, title TEXT NOT NULL, project_id TEXT NOT NULL,
                            provider_id TEXT, state TEXT NOT NULL, session_key TEXT,
                            source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                        );
                        CREATE TABLE IF NOT EXISTS manual_records (
                            id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, product TEXT NOT NULL,
                            day TEXT NOT NULL, metric TEXT NOT NULL, value NUMERIC NOT NULL,
                            currency TEXT, window_label TEXT, note TEXT NOT NULL,
                            source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                        );
                        """)
                    self._initialized = True
            with self.write_lock, connection:
                yield connection
        except (sqlite3.Error, OSError, OverflowError) as error:
            reason = "store_busy" if self.profile and isinstance(error, sqlite3.OperationalError) and ("locked" in str(error) or "busy" in str(error)) else "storage_error" if self.profile else "storage_unavailable"
            raise WorkspaceError(reason) from error
        finally:
            if connection is not None:
                connection.close()

    def list_tasks(self, *, limit=None, offset=0, project_id=None, scope="all"):
        with self.connection() as connection:
            connection.execute("BEGIN")
            where, values = [], []
            if project_id is not None:
                where.append("t.project_id=?")
                values.append(project_id)
            if self.profile and scope != "all":
                where.append({"active": "p.archived=0", "archived": "p.archived=1", "orphan": "p.id IS NULL"}[scope])
            clause = " WHERE " + " AND ".join(where) if where else ""
            source = "tasks t LEFT JOIN projects p ON p.id=t.project_id" if self.profile else "tasks t"
            total = connection.execute("SELECT COUNT(*) FROM " + source + clause, values).fetchone()[0]
            rows = connection.execute("SELECT t.* FROM " + source + clause + " ORDER BY t.updated_at DESC,t.id LIMIT ? OFFSET ?",
                                      (*values, limit if limit is not None else 50 if self.profile else 500, offset)).fetchall()
            return [dict(row) for row in rows], total

    def get_task(self, task_id):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise WorkspaceError("task_not_found")
            return dict(row)

    def save_task(self, fields, task_id=None):
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as connection:
            if task_id is None:
                task_id = str(uuid4())
                connection.execute("""INSERT INTO tasks
                    (id,title,project_id,provider_id,state,session_key,source,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,'manual',?,?)""",
                    (task_id, fields["title"], fields["project_id"], fields["provider_id"],
                     fields["state"], fields["session_key"], now, now))
            else:
                cursor = connection.execute("""UPDATE tasks SET title=?,project_id=?,provider_id=?,
                    state=?,session_key=?,updated_at=? WHERE id=?""",
                    (fields["title"], fields["project_id"], fields["provider_id"], fields["state"],
                     fields["session_key"], now, task_id))
                if cursor.rowcount != 1:
                    raise WorkspaceError("task_not_found")
            result = dict(connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        return result

    def list_records(self, *, limit=None, offset=0):
        with self.connection() as connection:
            connection.execute("BEGIN")
            total = connection.execute("SELECT COUNT(*) FROM manual_records").fetchone()[0]
            rows = connection.execute("SELECT * FROM manual_records ORDER BY updated_at DESC,id LIMIT ? OFFSET ?",
                                      (limit if limit is not None else 50 if self.profile else 100, offset)).fetchall()
            return [dict(row) for row in rows], total

    def save_record(self, fields):
        now = datetime.now(timezone.utc).isoformat()
        record_id = fields["id"]
        values = tuple(fields[key] for key in ("provider_id", "product", "day", "metric", "value",
                                               "currency", "window_label", "note"))
        with self.connection() as connection:
            if record_id is None:
                record_id = str(uuid4())
                connection.execute("""INSERT INTO manual_records
                    (id,provider_id,product,day,metric,value,currency,window_label,note,source,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,'manual',?,?)""", (record_id, *values, now, now))
            else:
                cursor = connection.execute("""UPDATE manual_records SET provider_id=?,product=?,day=?,
                    metric=?,value=?,currency=?,window_label=?,note=?,updated_at=? WHERE id=?""",
                    (*values, now, record_id))
                if cursor.rowcount != 1:
                    raise WorkspaceError("record_not_found")
            result = dict(connection.execute("SELECT * FROM manual_records WHERE id=?", (record_id,)).fetchone())
        return result

    def list_projects(self, *, include_archived=True):
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM projects" + ("" if include_archived else " WHERE archived=0") + " ORDER BY position,id").fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["archived"] = bool(item["archived"])
                item["directories"] = [{"id": value["binding_id"], "path": value["path"]} for value in connection.execute(
                    "SELECT binding_id,path FROM project_directories WHERE project_id=? ORDER BY is_primary DESC,binding_id", (item["id"],))]
                item["root"] = next(value["path"] for value in item["directories"] if value["id"] == "main")
                item["source"] = dict(connection.execute("SELECT kind,directory_id,relative_path FROM project_sources WHERE project_id=?", (item["id"],)).fetchone())
                items.append(item)
            return items

    def save_project(self, body, *, restore=False):
        from session_panel.profiles.profile import ProfileError
        item = validate_project(body, restore=restore)
        identity, stamp = item["id"], now_utc()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM projects WHERE id=?", (identity,)).fetchone()
            if restore:
                references = sum(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id=?", (identity,)).fetchone()[0]
                                 for table in ("tasks", "project_manual", "todos"))
                if existing is not None or references == 0:
                    raise ProfileError("not_found", "project_id", status=404)
            elif identity is not None and existing is None:
                raise ProfileError("not_found", "id", status=404)
            if existing is None and connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] >= 1000:
                raise ProfileError("invalid_project", "projects", message="project_budget_exceeded")
            if not item["archived"]:
                for binding in item["directories"]:
                    if connection.execute("""SELECT 1 FROM project_directories d JOIN projects p ON p.id=d.project_id
                        WHERE p.archived=0 AND d.path_key=? AND p.id!=?""", (binding["path_key"], identity or "")).fetchone():
                        raise ProfileError("directory_conflict", "directories", status=409)
            if existing is None:
                identity = identity or "p-" + uuid4().hex
                position = connection.execute("SELECT COALESCE(MAX(position),-1)+1 FROM projects").fetchone()[0]
                connection.execute("INSERT INTO projects VALUES(?,?,?,?,?,?)", (identity, item["name"], int(item["archived"]), stamp, stamp, position))
            else:
                connection.execute("UPDATE projects SET name=?,archived=?,updated_at=? WHERE id=?", (item["name"], int(item["archived"]), stamp, identity))
                connection.execute("DELETE FROM project_directories WHERE project_id=?", (identity,))
                connection.execute("DELETE FROM project_sources WHERE project_id=?", (identity,))
            connection.executemany("INSERT INTO project_directories VALUES(?,?,?,?,?)", [(identity, value["id"], value["path"], value["path_key"], int(value["id"] == "main")) for value in item["directories"]])
            connection.execute("INSERT INTO project_sources VALUES(?,?,?,?)", (identity, *(item["source"][key] for key in ("kind", "directory_id", "relative_path"))))
        return next(row for row in self.list_projects() if row["id"] == identity)

    def manual_rows(self):
        with self.connection() as connection:
            return {row["project_id"]: dict(row) for row in connection.execute("SELECT * FROM project_manual")}

    def save_manual(self, body):
        from session_panel.profiles.profile import ProfileError
        from session_panel.workspace.service import finite_number
        if not isinstance(body, dict) or set(body) != {"project_id", "state", "percent", "note"}:
            raise ProfileError("invalid_project", "manual")
        identity, state, percent, note = (body[key] for key in ("project_id", "state", "percent", "note"))
        if (not project_id_valid(identity) or state not in ("未开始", "在做", "等我", "完成")
                or percent is not None and (not finite_number(percent) or not 0 <= percent <= 100)
                or not isinstance(note, str) or len(note) > 1000):
            raise ProfileError("invalid_project", "manual")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT * FROM project_manual WHERE project_id=?", (identity,)).fetchone()
            if current is None and connection.execute("SELECT 1 FROM projects WHERE id=?", (identity,)).fetchone() is None:
                raise ProfileError("not_found", "project_id", status=404)
            changed = current is None or any(current[key] != body[key] for key in ("state", "percent", "note"))
            if changed:
                stamp = now_utc()
                connection.execute("INSERT OR REPLACE INTO project_manual VALUES(?,?,?,?,?)", (identity, state, percent, note, stamp))
                connection.execute("INSERT INTO project_manual_history VALUES(?,?,?,?,?,?,?)", (str(uuid4()), identity, state, percent, note, stamp, "manual"))
            result = dict(connection.execute("SELECT * FROM project_manual WHERE project_id=?", (identity,)).fetchone())
        return {"status": "ok", "manual": result, "history_added": changed}

    def manual_history(self, project_id, *, offset=0, limit=20):
        from session_panel.profiles.profile import ProfileError
        with self.connection() as connection:
            if not any(connection.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (project_id,)).fetchone() for table, column in (("projects", "id"), ("project_manual", "project_id"), ("tasks", "project_id"), ("todos", "project_id"))):
                raise ProfileError("not_found", "project_id", status=404)
            total = connection.execute("SELECT COUNT(*) FROM project_manual_history WHERE project_id=?", (project_id,)).fetchone()[0]
            rows = connection.execute("SELECT * FROM project_manual_history WHERE project_id=? ORDER BY recorded_at IS NULL,recorded_at DESC,id LIMIT ? OFFSET ?", (project_id, limit, offset)).fetchall()
            return {"items": [dict(row) for row in rows], "total": total, "offset": offset, "limit": limit}

    def task_counts(self):
        result = {}
        with self.connection() as connection:
            for identity, state, count in connection.execute("SELECT project_id,state,COUNT(*) FROM tasks GROUP BY project_id,state"):
                counts = result.setdefault(identity, dict.fromkeys(("total", "todo", "doing", "blocked", "review", "done"), 0))
                counts[state] += count
                counts["total"] += count
        return result

    def list_todos(self, *, offset=0, limit=100, project_id=None):
        with self.connection() as connection:
            where, values = (" WHERE project_id=?", (project_id,)) if project_id is not None else ("", ())
            total = connection.execute("SELECT COUNT(*) FROM todos" + where, values).fetchone()[0]
            rows = connection.execute("SELECT * FROM todos" + where + " ORDER BY position,id LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
            return {"items": [{**dict(row), "done": bool(row["done"])} for row in rows], "total": total, "offset": offset, "limit": limit}

    def save_todo(self, body, *, toggle=False):
        from session_panel.profiles.profile import ProfileError
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if toggle:
                if not isinstance(body, dict) or set(body) != {"id"} or not isinstance(body["id"], str):
                    raise ValueError("invalid_request")
                cursor = connection.execute("UPDATE todos SET done=1-done,updated_at=? WHERE id=?", (now_utc(), body["id"]))
                if cursor.rowcount != 1:
                    raise ProfileError("todo_not_found", status=404)
            else:
                if not isinstance(body, dict) or set(body) != {"text", "project_id"}:
                    raise ValueError("invalid_request")
                text, identity = body["text"], body["project_id"]
                if not isinstance(text, str) or not isinstance(identity, str):
                    raise ValueError("invalid_request")
                text = " ".join(text.split())
                if not 1 <= len(text) <= 40:
                    raise ValueError("invalid_request")
                if identity and connection.execute("SELECT 1 FROM projects WHERE id=? AND archived=0", (identity,)).fetchone() is None:
                    raise ValueError("invalid_request")
                position = connection.execute("SELECT COALESCE(MIN(position),0)-1 FROM todos").fetchone()[0]
                stamp = now_utc()
                connection.execute("INSERT INTO todos VALUES(?,?,?,?,?,?,?)", (uuid4().hex[:8], identity, text, 0, stamp, stamp, position))
        return {"todos": self.list_todos()["items"]}

    def compatibility(self, *, kind="projects", offset=0, limit=20):
        with self.connection() as connection:
            if kind in ("manual", "todos"):
                table = "project_manual" if kind == "manual" else "todos"
                clause = f" FROM {table} m WHERE m.project_id IS NOT NULL AND m.project_id!='' AND NOT EXISTS(SELECT 1 FROM projects p WHERE p.id=m.project_id)"
                total = connection.execute("SELECT COUNT(*)" + clause).fetchone()[0]
                items = [dict(row) for row in connection.execute("SELECT m.*" + clause + " ORDER BY m.project_id LIMIT ? OFFSET ?", (limit, offset))]
                if kind == "todos":
                    items = [{**row, "done": bool(row["done"])} for row in items]
            else:
                rows = connection.execute("""SELECT project_id FROM tasks UNION SELECT project_id FROM project_manual
                    UNION SELECT project_id FROM todos WHERE project_id IS NOT NULL AND project_id!=''""").fetchall()
                ids = sorted(row[0] for row in rows if connection.execute("SELECT 1 FROM projects WHERE id=?", (row[0],)).fetchone() is None)
                total, items = len(ids), []
                for identity in ids[offset:offset+limit]:
                    item = {"project_id": identity}
                    for table, key in (("tasks", "task_count"), ("project_manual", "manual_count"), ("todos", "todo_count")):
                        item[key] = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id=?", (identity,)).fetchone()[0]
                    items.append(item)
            return {"items": items, "total": total, "offset": offset, "limit": limit}

    def import_receipt(self):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM profile_imports ORDER BY imported_at LIMIT 1").fetchone()
            if row is None:
                return None
            value = dict(row)
            value["counts"] = json.loads(value.pop("counts_json"))
            value["source_fingerprints"] = json.loads(value.pop("source_fingerprints_json"))
            return value

    def is_empty(self, connection=None):
        if connection is None:
            with self.connection() as connection:
                return self.is_empty(connection)
        return all(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0 for table in MANAGEMENT_TABLES)
