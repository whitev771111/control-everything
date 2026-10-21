"""Task-to-session links; metadata only, never chat content."""

from datetime import datetime
from urllib.parse import urlsplit
from uuid import uuid4

from control_everything.input_validation import ProfileError
from control_everything.workspace.store import now_utc


def _task(connection, task_id):
    row = connection.execute("SELECT * FROM task_items WHERE id=? AND origin='task'", (task_id,)).fetchone()
    if row is None:
        raise ProfileError("task_not_found", status=404)
    return dict(row)


def _text(value, field, maximum, *, required=False):
    if value is None:
        if required:
            raise ProfileError("invalid_session", field)
        return None
    if type(value) is not str or len(value) > maximum or "\0" in value or required and not value.strip():
        raise ProfileError("invalid_session", field)
    return value


def _time(value, field):
    if value is None:
        return None
    if type(value) is not str or len(value) > 80:
        raise ProfileError("invalid_session", field)
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError):
        raise ProfileError("invalid_session", field) from None
    if parsed.tzinfo is None:
        raise ProfileError("invalid_session", field)
    return value


def _url(value):
    value = _text(value, "url", 2048)
    if value is None or not value:
        return value
    try:
        parts = urlsplit(value)
    except ValueError:
        raise ProfileError("invalid_session", "url") from None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ProfileError("invalid_session", "url")
    return value


def _view(row):
    value = dict(row)
    value["project_id"] = value.pop("current_project_id", value.get("project_id"))
    return value


def _legacy(task_id, project_id, session_key):
    return {"id": None, "task_id": task_id, "project_id": project_id, "agent_id": None,
            "source": "codex", "source_instance": "", "external_id": session_key, "title": None,
            "url": None, "last_seen_at": None, "created_at": None, "updated_at": None, "legacy": True}


def task_sessions(store, task_id):
    with store.connection() as connection:
        connection.execute("BEGIN")
        item = _task(connection, task_id)
        rows = connection.execute(
            "SELECT s.*,CASE WHEN s.task_id IS NOT NULL THEN t.project_id ELSE s.project_id END AS current_project_id "
            "FROM task_sessions s LEFT JOIN task_items t ON t.id=s.task_id "
            "WHERE s.task_id=? ORDER BY s.updated_at DESC,s.id DESC",
            (task_id,)).fetchall()
        items = [_view(row) for row in rows]
        if item["session_key"] and not any(
                row["source"] == "codex" and row["source_instance"] == "" and
                row["external_id"] == item["session_key"] for row in items):
            items.append(_legacy(task_id, item["project_id"], item["session_key"]))
        return {"item": item, "items": items, "legacy_session_key": item["session_key"]}


def list_sessions(store, project_id=None):
    with store.connection() as connection:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT s.*,CASE WHEN s.task_id IS NOT NULL THEN t.project_id ELSE s.project_id END AS current_project_id "
            "FROM task_sessions s LEFT JOIN task_items t ON t.id=s.task_id "
            "ORDER BY s.updated_at DESC,s.id DESC").fetchall()
        items = [_view(row) for row in rows]
        if project_id is not None:
            items = [row for row in items if row["project_id"] == project_id]
        legacy_rows = connection.execute(
            "SELECT id,project_id,session_key FROM task_items WHERE origin='task' AND session_key IS NOT NULL"
            + (" AND project_id=?" if project_id is not None else ""),
            ((project_id,) if project_id is not None else ())).fetchall()
        for row in legacy_rows:
            if not any(item["source"] == "codex" and item["source_instance"] == "" and
                       item["external_id"] == row["session_key"] for item in items):
                items.append(_legacy(row["id"], row["project_id"], row["session_key"]))
        return {"items": items}


def link_session(store, task_id, body):
    required = {"source", "external_id"}
    allowed = required | {"source_instance", "title", "url", "agent_id", "last_seen_at"}
    if type(body) is not dict or not required <= set(body) or set(body) - allowed:
        raise ProfileError("invalid_session", "body")
    source = _text(body["source"], "source", 32, required=True)
    source_instance = _text(body.get("source_instance", ""), "source_instance", 64) or ""
    external_id = _text(body["external_id"], "external_id", 256, required=True)
    title = _text(body.get("title"), "title", 200)
    url = _url(body.get("url"))
    agent_id = _text(body.get("agent_id"), "agent_id", 64)
    last_seen = _time(body.get("last_seen_at"), "last_seen_at")
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = _task(connection, task_id)
        existing = connection.execute(
            "SELECT * FROM task_sessions WHERE source=? AND source_instance=? AND external_id=?",
            (source, source_instance, external_id)).fetchone()
        stamp = now_utc()
        if existing is not None:
            if existing["task_id"] not in (None, task_id):
                raise ProfileError("session_conflict", "external_id", status=409)
            connection.execute(
                "UPDATE task_sessions SET task_id=?,project_id=?,agent_id=?,title=?,url=?,last_seen_at=?,updated_at=? "
                "WHERE id=?",
                (task_id, item["project_id"], agent_id or existing["agent_id"], title or existing["title"],
                 url or existing["url"], last_seen or existing["last_seen_at"], stamp, existing["id"]))
            return _view(connection.execute("SELECT * FROM task_sessions WHERE id=?", (existing["id"],)).fetchone())
        identity = str(uuid4())
        connection.execute(
            "INSERT INTO task_sessions(id,task_id,project_id,agent_id,source,source_instance,external_id,title,url,"
            "last_seen_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity, task_id, item["project_id"], agent_id, source, source_instance, external_id, title, url,
             last_seen, stamp, stamp))
        return _view(connection.execute("SELECT * FROM task_sessions WHERE id=?", (identity,)).fetchone())


def unlink_session(store, session_id, body):
    if type(body) is not dict or set(body) != {"expected_task_id", "expected_updated_at"}:
        raise ProfileError("invalid_session", "body")
    expected_task_id = _text(body["expected_task_id"], "expected_task_id", 64, required=True)
    expected_updated_at = _time(body["expected_updated_at"], "expected_updated_at")
    if expected_updated_at is None:
        raise ProfileError("invalid_session", "expected_updated_at")
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM task_sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ProfileError("session_not_found", status=404)
        if row["task_id"] != expected_task_id or row["updated_at"] != expected_updated_at:
            raise ProfileError("session_changed", status=409)
        connection.execute("UPDATE task_sessions SET task_id=NULL,project_id=NULL,updated_at=? WHERE id=?",
                           (now_utc(), session_id))
        return _view(connection.execute("SELECT * FROM task_sessions WHERE id=?", (session_id,)).fetchone())
