"""Transactions for workspace-owned tasks and manual usage observations."""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class WorkspaceError(Exception):
    """A public, fixed reason without storage or account details."""


class WorkspaceStore:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self._initialized = False
        self._schema_lock = threading.Lock()

    @contextmanager
    def connection(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=3)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=3000")
            with self._schema_lock:
                if not self._initialized:
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
            with connection:
                yield connection
        except (sqlite3.Error, OSError, OverflowError) as error:
            raise WorkspaceError("storage_unavailable") from error
        finally:
            if connection is not None:
                connection.close()

    def list_tasks(self):
        with self.connection() as connection:
            connection.execute("BEGIN")
            total = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            rows = connection.execute("SELECT * FROM tasks ORDER BY updated_at DESC,id LIMIT 500").fetchall()
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

    def list_records(self):
        with self.connection() as connection:
            connection.execute("BEGIN")
            total = connection.execute("SELECT COUNT(*) FROM manual_records").fetchone()[0]
            rows = connection.execute("SELECT * FROM manual_records ORDER BY updated_at DESC,id LIMIT 100").fetchall()
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
