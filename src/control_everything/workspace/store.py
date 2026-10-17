"""Transactions for workspace-owned tasks and manual usage observations."""

import sqlite3
import threading
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from control_everything.input_validation import ProfileError
from control_everything.workspace.validation import (
    project_id_valid, path_key, validate_project, finite_number, observation_owner,
)
from control_everything.workspace import task_schema as tasks
from control_everything.workspace.task_schema import BASE_SCHEMA, MANAGEMENT_SCHEMA

MANAGEMENT_TABLES = tasks.SCHEMA2_TABLES


def now_utc():
    return datetime.now(timezone.utc).isoformat()


class WorkspaceError(Exception):
    """A public, fixed reason without storage or account details."""


class WorkspaceStore:
    def __init__(self, db_path, *, profile=False):
        self.path = Path(db_path)
        self._initialized = False
        self._schema_lock = threading.Lock()
        self.profile = profile
        self.write_lock = threading.RLock()

    def validate_schema(self, connection, version=2):
        tasks.validate_schema(connection, version)

    def checked_management_connection(self, connection):
        self.validate_schema(connection)
        tasks.register_writer(connection)

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
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if self.profile:
                    tables = connection.execute("SELECT name FROM sqlite_master").fetchall()
                    if version == 0 and not tables:
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            # A second process might have initialized while we waited.
                            if not connection.execute("SELECT name FROM sqlite_master").fetchone():
                                tasks.install_schema2(connection, fresh=True)
                            connection.commit()
                        except Exception:
                            connection.rollback()
                            raise
                    elif version == 1:
                        self.validate_schema(connection, 1)
                        raise WorkspaceError("migration_required")
                    self.checked_management_connection(connection)
                elif version == 2:
                    # Refuse before a legacy ExecutionStore can create its tables.
                    raise WorkspaceError("schema_error")
                elif not self._initialized:
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
                where.append({"active": "p.archived=0", "archived": "p.archived=1",
                              "orphan": "p.id IS NULL AND t.project_id IS NOT NULL AND t.project_id!=''"}[scope])
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
        if self.profile:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if task_id is None:
                    stamp, task_id = now_utc(), str(uuid4())
                    self._create_item(connection, fields, origin='task', raw_id=task_id,
                                      source='manual', created_at=stamp, updated_at=stamp)
                else:
                    old = self._origin_item(connection, 'task', task_id)
                    # The legacy service includes its raw id in the validated body.
                    changes = {key: fields[key] for key in ('title', 'project_id', 'provider_id', 'state', 'session_key')}
                    self._update_item(connection, old, changes)
                return dict(connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
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
            tasks.bump_list_revision(connection)
        return next(row for row in self.list_projects() if row["id"] == identity)

    def manual_rows(self):
        with self.connection() as connection:
            return {row["project_id"]: dict(row) for row in connection.execute("SELECT * FROM project_manual")}

    def save_manual(self, body):
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
        with self.connection() as connection:
            if not any(connection.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (project_id,)).fetchone() for table, column in (("projects", "id"), ("project_manual", "project_id"), ("tasks", "project_id"), ("todos", "project_id"))):
                raise ProfileError("not_found", "project_id", status=404)
            total = connection.execute("SELECT COUNT(*) FROM project_manual_history WHERE project_id=?", (project_id,)).fetchone()[0]
            rows = connection.execute("SELECT * FROM project_manual_history WHERE project_id=? ORDER BY recorded_at IS NULL,recorded_at DESC,id LIMIT ? OFFSET ?", (project_id, limit, offset)).fetchall()
            return {"items": [dict(row) for row in rows], "total": total, "offset": offset, "limit": limit}

    def task_counts(self):
        result = {}
        with self.connection() as connection:
            for identity, state, count in connection.execute("SELECT project_id,state,COUNT(*) FROM " +
                    ("task_items" if self.profile else "tasks") + " GROUP BY project_id,state"):
                if self.profile and not identity:
                    continue
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
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if toggle:
                if not isinstance(body, dict) or set(body) != {"id"} or not isinstance(body["id"], str):
                    raise ValueError("invalid_request")
                old = self._origin_item(connection, 'todo', body['id'])
                self._update_item(connection, old, {'state': old['resume_state'] if old['state'] == 'done' else 'done'})
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
                self._create_item(connection, {'title': text, 'project_id': identity, 'provider_id': None,
                                  'state': 'todo', 'session_key': None}, origin='todo', raw_id=uuid4().hex[:8],
                                  source=None, created_at=stamp, updated_at=stamp, todo_position=position)
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
                ids = sorted(row[0] for row in rows if row[0] and connection.execute("SELECT 1 FROM projects WHERE id=?", (row[0],)).fetchone() is None)
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
        return all(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
                   for table in MANAGEMENT_TABLES if table != 'task_center_meta')

    @staticmethod
    def _origin_item(connection, origin, raw_id):
        row = connection.execute('SELECT * FROM task_items WHERE origin=? AND origin_id=?', (origin, raw_id)).fetchone()
        if row is None:
            if origin == 'todo':
                raise ProfileError('todo_not_found', status=404)
            raise WorkspaceError('task_not_found')
        return dict(row)

    @staticmethod
    def _check_title(title):
        try:
            tasks.check_text(title)
        except WorkspaceError as error:
            raise WorkspaceError('text_budget' if str(error) == 'migration_edit_budget' else 'invalid_item') from error

    @staticmethod
    def _create_item(connection, fields, *, origin='task', raw_id=None, source='manual',
                     created_at=None, updated_at=None, todo_position=None):
        WorkspaceStore._check_title(fields['title'])
        if todo_position is not None and (type(todo_position) is not int or not -(1 << 63) <= todo_position < (1 << 63)):
            raise WorkspaceError('order_exhausted')
        order = connection.execute('SELECT COALESCE(MIN(sort_index),0)-1 FROM task_items').fetchone()[0]
        if type(order) is not int or order < -(1 << 63):
            raise WorkspaceError('order_exhausted')
        state = fields['state']
        row = dict(id=str(uuid4()), origin=origin, origin_id=raw_id if raw_id is not None else str(uuid4()),
                   title=fields['title'], project_id=fields['project_id'], provider_id=fields['provider_id'],
                   state=state, session_key=fields['session_key'], source=source,
                   created_at=created_at, updated_at=updated_at, todo_position=todo_position,
                   sort_index=order, resume_state=state if state != 'done' else 'todo', revision=1)
        tasks.insert_item(connection, row)
        tasks.bump_list_revision(connection)
        return row

    @staticmethod
    def _update_item(connection, old, changes, expected_revision=None):
        if expected_revision is not None and old['revision'] != expected_revision:
            raise WorkspaceError('item_changed')
        allowed = {'title', 'project_id', 'provider_id', 'state', 'session_key'}
        if not isinstance(changes, dict) or not changes or set(changes) - allowed:
            raise WorkspaceError('invalid_item')
        changed = {key: value for key, value in changes.items() if value != old[key]}
        if not changed:
            return old
        if 'title' in changed:
            WorkspaceStore._check_title(changed['title'])
        state = changed.get('state', old['state'])
        if state not in tasks.STATES:
            raise WorkspaceError('invalid_item')
        if old['revision'] >= tasks.MAX_REVISION:
            raise WorkspaceError('revision_exhausted')
        changed['resume_state'] = state if state != 'done' else old['state'] if old['state'] != 'done' else old['resume_state']
        changed.update(revision=old['revision'] + 1, updated_at=now_utc())
        connection.execute('UPDATE task_items SET ' + ','.join(key + '=?' for key in changed) + ' WHERE id=?',
                           (*changed.values(), old['id']))
        tasks.bump_list_revision(connection)
        return {**old, **changed}

    def get_item(self, item_id):
        with self.connection() as connection:
            row = connection.execute('SELECT * FROM task_items WHERE id=?', (item_id,)).fetchone()
            if row is None:
                raise WorkspaceError('item_not_found')
            return dict(row)

    @staticmethod
    def _check_item_associations(connection, fields, old=None, session_cwd=None):
        merged = {**old, **fields} if old is not None else fields
        project_changed = old is None or merged['project_id'] != old['project_id']
        link_changed = old is None or any(merged[key] != old[key] for key in ('project_id', 'provider_id', 'session_key'))
        check_session = link_changed and merged['session_key'] is not None
        if (project_changed or check_session) and merged['project_id'] not in (None, ''):
            if connection.execute('SELECT 1 FROM projects WHERE id=? AND archived=0', (merged['project_id'],)).fetchone() is None:
                raise ProfileError('invalid_item', 'project_id')
        if check_session:
            if session_cwd is None:
                raise WorkspaceError('session_not_in_sample')
            # Only database facts are refreshed here; the request's session sample stays outside the write transaction.
            projects = {}
            for row in connection.execute('''SELECT p.id,d.path FROM projects p JOIN project_directories d
                                             ON p.id=d.project_id WHERE p.archived=0'''):
                projects.setdefault(row['id'], {'id': row['id'], 'directories': []})['directories'].append({'path': row['path']})
            if observation_owner(session_cwd, list(projects.values())) != merged['project_id']:
                raise WorkspaceError('session_project_mismatch')

    def save_item(self, fields, *, item_id=None, expected_revision=None, session_cwd=None):
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if item_id is None:
                self._check_item_associations(connection, fields, session_cwd=session_cwd)
                stamp = now_utc()
                return self._create_item(connection, fields, created_at=stamp, updated_at=stamp)
            row = connection.execute('SELECT * FROM task_items WHERE id=?', (item_id,)).fetchone()
            if row is None:
                raise WorkspaceError('item_not_found')
            if type(expected_revision) is not int:
                raise WorkspaceError('invalid_item')
            current = dict(row)
            if current['revision'] != expected_revision:
                raise WorkspaceError('item_changed')
            if not isinstance(fields, dict) or not fields or set(fields) - {'title', 'project_id', 'provider_id', 'state', 'session_key'}:
                raise WorkspaceError('invalid_item')
            self._check_item_associations(connection, fields, current, session_cwd)
            return self._update_item(connection, current, fields, expected_revision)

    def list_items(self, *, offset=0, limit=50, scope='all', project_id=None, provider_id=None,
                   state=None, list_revision=None):
        if (type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 100
                or list_revision is not None and (type(list_revision) is not int or not 1 <= list_revision <= tasks.MAX_REVISION)):
            raise WorkspaceError('invalid_item')
        with self.connection() as connection:
            connection.execute('BEGIN')
            revision = connection.execute('SELECT list_revision FROM task_center_meta WHERE id=1').fetchone()[0]
            if offset > 0 and list_revision is None or list_revision is not None and list_revision != revision:
                raise WorkspaceError('list_changed')
            if scope not in ('all', 'active', 'archived', 'orphan', 'global') or not 0 <= offset or not 1 <= limit <= 100:
                raise WorkspaceError('invalid_item')
            where, args = [], []
            clause = {'active': 'p.archived=0', 'archived': 'p.archived=1',
                      'orphan': "p.id IS NULL AND t.project_id IS NOT NULL AND t.project_id!=''",
                      'global': "(t.project_id IS NULL OR t.project_id='')"}.get(scope)
            if clause:
                where.append(clause)
            for key, value in (('project_id', project_id), ('provider_id', provider_id), ('state', state)):
                if value is not None:
                    where.append('t.' + key + '=?')
                    args.append(value)
            source = ' FROM task_items t LEFT JOIN projects p ON p.id=t.project_id' + (' WHERE ' + ' AND '.join(where) if where else '')
            total = connection.execute('SELECT COUNT(*)' + source, args).fetchone()[0]
            rows = connection.execute('SELECT t.*' + source + ' ORDER BY t.sort_index,t.id LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
            return {'items': [dict(row) for row in rows], 'total': total, 'offset': offset, 'limit': limit, 'list_revision': revision}
