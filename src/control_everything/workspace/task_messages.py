"""Human messages to an Agent; manual handoff state never claims delivery."""

import json
from uuid import uuid4

from control_everything.input_validation import ProfileError
from control_everything.workspace.store import now_utc

KINDS = ("supplement", "return", "user_input", "note")


def _task(connection, task_id):
    row = connection.execute("SELECT * FROM task_items WHERE id=? AND origin='task'", (task_id,)).fetchone()
    if row is None:
        raise ProfileError("task_not_found", status=404)
    return dict(row)


def _text(value, field, maximum, *, required=False):
    if value is None:
        if required:
            raise ProfileError("invalid_message", field)
        return None
    if type(value) is not str or len(value) > maximum or "\0" in value or required and not value.strip():
        raise ProfileError("invalid_message", field)
    return value


def _waiting(connection, task_id):
    row = connection.execute(
        "SELECT * FROM task_events WHERE task_id=? AND kind='progress' ORDER BY created_at DESC,id DESC LIMIT 20",
        (task_id,)).fetchall()
    for event in row:
        try:
            payload = json.loads(event["payload_json"])
        except (TypeError, ValueError):
            continue
        if payload.get("stage") == "waiting_user":
            return {"run_id": event["run_id"], "created_at": event["created_at"],
                    "wait_kind": payload.get("wait_kind"), "request_id": payload.get("request_id"),
                    "agent_hint": payload.get("agent_hint"), "message": payload.get("message")}
    return None


def list_messages(store, task_id):
    with store.connection() as connection:
        connection.execute("BEGIN")
        item = _task(connection, task_id)
        rows = connection.execute(
            "SELECT * FROM task_messages WHERE task_id=? ORDER BY created_at DESC,id DESC LIMIT 100",
            (task_id,)).fetchall()
        return {"item": item, "items": [dict(row) for row in rows], "waiting_user": _waiting(connection, task_id)}


def create_message(store, task_id, body):
    required = {"kind", "body_text"}
    allowed = required | {"agent_id", "run_id", "client_token"}
    if type(body) is not dict or not required <= set(body) or set(body) - allowed:
        raise ProfileError("invalid_message", "body")
    kind = body["kind"]
    if kind not in KINDS:
        raise ProfileError("invalid_message", "kind")
    text = _text(body["body_text"], "body_text", 10000, required=True)
    agent_id = _text(body.get("agent_id"), "agent_id", 64)
    run_id = _text(body.get("run_id"), "run_id", 64)
    client_token = _text(body.get("client_token"), "client_token", 128)
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = _task(connection, task_id)
        if client_token is not None:
            existing = connection.execute(
                "SELECT * FROM task_messages WHERE task_id=? AND client_token=?", (task_id, client_token)).fetchone()
            if existing is not None:
                return {"status": "existing", "message": dict(existing)}
        if run_id is not None:
            run = connection.execute("SELECT id FROM agent_runs WHERE id=? AND task_id=?", (run_id, task_id)).fetchone()
            if run is None:
                raise ProfileError("run_not_found", "run_id", status=404)
        stamp = now_utc()
        identity = str(uuid4())
        connection.execute(
            "INSERT INTO task_messages(id,task_id,run_id,agent_id,client_token,direction,kind,body_text,"
            "delivery_mode,delivery_state,created_at,sent_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity, task_id, run_id, agent_id, client_token, "human_to_agent", kind, text,
             "manual", "previewed", stamp, None, stamp))
        return {"status": "created",
                "message": dict(connection.execute("SELECT * FROM task_messages WHERE id=?", (identity,)).fetchone())}


def mark_sent(store, message_id):
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM task_messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            raise ProfileError("message_not_found", status=404)
        if row["delivery_state"] != "previewed":
            raise ProfileError("message_state_conflict", "delivery_state", status=409)
        stamp = now_utc()
        connection.execute("UPDATE task_messages SET delivery_state='sent',sent_at=?,updated_at=? WHERE id=?",
                           (stamp, stamp, message_id))
        return dict(connection.execute("SELECT * FROM task_messages WHERE id=?", (message_id,)).fetchone())


def cancel_message(store, message_id):
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM task_messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            raise ProfileError("message_not_found", status=404)
        if row["delivery_state"] != "previewed":
            raise ProfileError("message_state_conflict", "delivery_state", status=409)
        connection.execute("UPDATE task_messages SET delivery_state='cancelled',updated_at=? WHERE id=?",
                           (now_utc(), message_id))
        return dict(connection.execute("SELECT * FROM task_messages WHERE id=?", (message_id,)).fetchone())
