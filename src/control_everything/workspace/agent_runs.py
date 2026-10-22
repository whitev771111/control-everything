"""Agent task claims, progress events and review submission; no process control."""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from control_everything.input_validation import ProfileError
from control_everything.workspace.store import WorkspaceError, now_utc
from control_everything.workspace.task_schema import MAX_REVISION, STATES
from control_everything.workspace.dispatch import link_claim

ACTIVE = ("claimed", "running", "waiting_review")
STAGES = ("analyzing", "implementing", "testing", "blocked", "waiting_user")
WAIT_KINDS = ("tool_authorization", "user_input", "external_approval", "unknown")
CONTACT_UNKNOWN_SECONDS = 60


def _text(value, field, maximum, *, required=True):
    if (type(value) is not str or len(value) > maximum or "\0" in value
            or required and not value.strip()):
        raise WorkspaceError("invalid_agent_request")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _event(connection, task_id, run_id, kind, payload):
    connection.execute(
        "INSERT INTO task_events(id,task_id,run_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?)",
        (str(uuid4()), task_id, run_id, kind, _json(payload), now_utc()))


def _active_run(connection, task_id):
    return connection.execute(
        "SELECT * FROM agent_runs WHERE task_id=? AND status IN (?,?,?) ORDER BY started_at DESC LIMIT 1",
        (task_id, *ACTIVE)).fetchone()


def _latest_run(connection, task_id):
    return connection.execute(
        "SELECT * FROM agent_runs WHERE task_id=? ORDER BY started_at DESC,id DESC LIMIT 1",
        (task_id,)).fetchone()


def _item(connection, task_id):
    row = connection.execute("SELECT * FROM task_items WHERE id=? AND origin='task'", (task_id,)).fetchone()
    if row is None:
        raise WorkspaceError("task_not_found")
    return dict(row)


def _agent_item(connection, task_id, agent_id, project_ids=None):
    item = _item(connection, task_id)
    if item["assigned_agent_id"] != agent_id:
        raise WorkspaceError("not_assigned")
    if project_ids and item["project_id"] not in project_ids:
        raise WorkspaceError("project_forbidden")
    return item


def _run_view(row):
    if row is None:
        return None
    result = dict(row)
    result["evidence"] = json.loads(result.pop("evidence_json") or "[]")
    return result


def _event_view(row):
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json") or "{}")
    return result


def _material(connection, item, run_view):
    dispatch = connection.execute(
        "SELECT * FROM task_dispatches WHERE task_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
        (item["id"],)).fetchone()
    request = None
    files = []
    base = None
    if dispatch is not None:
        try:
            brief = json.loads(dispatch["brief_json"])
        except (TypeError, ValueError):
            brief = {}
        request = {"dispatch_id": dispatch["id"], "brief_revision": brief.get("revision"),
                   "request_text": brief.get("request_text"), "acceptance_text": brief.get("acceptance_text"),
                   "constraints_text": brief.get("constraints_text"), "references": brief.get("references", []),
                   "brief_sha256": dispatch["brief_sha256"], "delivery_state": dispatch["delivery_state"]}
        base = Path(dispatch["directory_path"]).resolve()
        for entry in (run_view["evidence"] if run_view else []):
            if type(entry) is not str or not entry or len(entry) > 2048 or Path(entry).is_absolute():
                continue
            try:
                target = (base / entry).resolve()
                if not target.is_relative_to(base):
                    continue
            except (OSError, ValueError):
                continue
            files.append({"path": entry, "exists": target.is_file(), "source": "agent_reported"})
    sessions = [dict(row) for row in connection.execute(
        "SELECT id,task_id,agent_id,source,source_instance,external_id,title,url,last_seen_at,updated_at "
        "FROM task_sessions WHERE task_id=? ORDER BY updated_at DESC,id DESC LIMIT 20", (item["id"],))]
    result = {"request": request, "agent_result": (
        {"summary": run_view.get("summary"), "evidence": run_view.get("evidence", []),
         "status": run_view.get("status"), "source": "agent_reported"} if run_view else None),
        "files": files, "tests": {"status": "missing", "commands": [], "source": "unavailable"},
        "sessions": sessions, "verification": {"status": "unverified", "source": "agent_reported"},
        "completeness": {"request": request is not None, "result": bool(run_view and run_view.get("summary")),
                         "files": bool(files), "tests": False, "sessions": bool(sessions)}}
    return result


def _detail(connection, item):
    run = _latest_run(connection, item["id"])
    run_view = _run_view(run)
    events = [_event_view(row) for row in connection.execute(
        "SELECT * FROM (SELECT * FROM task_events WHERE task_id=? ORDER BY created_at DESC,id DESC LIMIT 100) "
        "ORDER BY created_at,id", (item["id"],))]
    reviews = []
    for row in connection.execute(
            "SELECT * FROM task_reviews WHERE task_id=? ORDER BY created_at DESC,id DESC LIMIT 20", (item["id"],)):
        review = dict(row)
        review["evidence"] = json.loads(review.pop("evidence_json") or "[]")
        reviews.append(review)
    messages = []
    if _has_messages(connection):
        messages = [dict(row) for row in connection.execute(
            "SELECT * FROM task_messages WHERE task_id=? AND direction='human_to_agent' "
            "ORDER BY created_at DESC,id DESC LIMIT 20", (item["id"],))]
    return {"item": item, "run": run_view, "events": events, "reviews": reviews, "messages": messages,
            "review_material": _material(connection, item, run_view),
            "connection_state": connection_state(run_view["heartbeat_at"] if run_view else None,
                                                 run_view["status"] if run_view else None),
            "last_contact_at": run_view["heartbeat_at"] if run_view else None}


def _has_messages(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_messages'").fetchone() is not None


def connection_state(heartbeat_at, status=None):
    if status is not None and status not in ACTIVE:
        return "not_applicable"
    if heartbeat_at is None:
        return "unknown"
    try:
        heartbeat = datetime.fromisoformat(heartbeat_at)
    except (TypeError, ValueError):
        return "unknown"
    age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    return "online" if age <= CONTACT_UNKNOWN_SECONDS else "unknown"


def assignment_states(store, task_ids):
    ids = [task_id for task_id in task_ids if isinstance(task_id, str)]
    if not ids:
        return {}
    with store.connection() as connection:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT task_id,status,heartbeat_at,started_at,id FROM agent_runs WHERE task_id IN ({}) "
            "ORDER BY started_at DESC,id DESC".format(",".join("?" for _ in ids)), ids).fetchall()
        runs = {}
        for row in rows:
            runs.setdefault(row["task_id"], dict(row))
        events = connection.execute(
            "SELECT task_id,kind,payload_json,created_at FROM task_events WHERE task_id IN ({}) "
            "ORDER BY created_at DESC,id DESC".format(",".join("?" for _ in ids)), ids).fetchall()
        latest_events = {}
        for row in events:
            latest_events.setdefault(row["task_id"], _event_view(row))
        return {task_id: {
            "run_status": runs.get(task_id, {}).get("status"),
            "heartbeat_at": runs.get(task_id, {}).get("heartbeat_at"),
            "last_event": latest_events.get(task_id),
        } for task_id in ids}


def task_detail(store, task_id):
    with store.connection() as connection:
        connection.execute("BEGIN")
        return _detail(connection, _item(connection, task_id))


def list_tasks(store, agent_id, state=None, project_ids=None):
    if state is not None and state not in (*STATES, "active"):
        raise WorkspaceError("invalid_agent_request")
    with store.connection() as connection:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT * FROM task_items WHERE origin='task' AND assigned_agent_id=? "
            "AND state IN ('todo','doing','blocked','review') ORDER BY sort_index,id", (agent_id,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            if project_ids and item["project_id"] not in project_ids:
                continue
            if state == "active" and item["state"] == "review":
                pass
            elif state is not None and state != "active" and item["state"] != state:
                continue
            run = _run_view(_active_run(connection, item["id"]))
            last = connection.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (item["id"],)).fetchone()
            items.append({**item, "run": run, "last_event": _event_view(last) if last else None})
        return {"items": items, "total": len(items)}


def get_task(store, agent_id, task_id, project_ids=None):
    with store.connection() as connection:
        connection.execute("BEGIN")
        item = _agent_item(connection, task_id, agent_id, project_ids)
        return _detail(connection, item)


def claim(store, agent_id, task_id, expected_revision, project_ids=None):
    if type(expected_revision) is not int or not 1 <= expected_revision <= MAX_REVISION:
        raise WorkspaceError("invalid_agent_request")
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = _agent_item(connection, task_id, agent_id, project_ids)
        if item["revision"] != expected_revision:
            raise WorkspaceError("item_changed")
        if item["state"] in ("review", "done"):
            raise WorkspaceError("review_not_ready")
        if _active_run(connection, task_id) is not None:
            raise WorkspaceError("run_conflict")
        stamp = now_utc()
        run_id = str(uuid4())
        connection.execute(
            "INSERT INTO agent_runs(id,task_id,agent_id,status,started_at,heartbeat_at,ended_at,summary,evidence_json) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, task_id, agent_id, "claimed", stamp, stamp, None, "", "[]"))
        dispatch_id = link_claim(connection, task_id, agent_id, run_id)
        if dispatch_id is not None:
            connection.execute("UPDATE agent_runs SET dispatch_id=? WHERE id=?", (dispatch_id, run_id))
        item = store._update_item(connection, item, {"state": "doing"}, expected_revision)
        _event(connection, task_id, run_id, "claimed", {"agent_id": agent_id})
        return {"item": item, "run": _run_view(_active_run(connection, task_id))}


def claim_dispatch_in_transaction(store, connection, *, dispatch_id, task_id, agent_id,
                                  execution_run_id, expected_revision):
    """Bind a prepared dispatch and insert its claimed agent run on the caller transaction."""
    if not connection.in_transaction:
        raise WorkspaceError("transaction_required")
    if type(expected_revision) is not int or not 1 <= expected_revision <= MAX_REVISION:
        raise WorkspaceError("invalid_agent_request")
    dispatch = connection.execute("SELECT * FROM task_dispatches WHERE id=?", (dispatch_id,)).fetchone()
    if dispatch is None:
        raise WorkspaceError("dispatch_not_found")
    if (dispatch["task_id"] != task_id or dispatch["agent_id"] != agent_id
            or dispatch["delivery_state"] != "previewed"
            or dispatch["run_id"] is not None or dispatch["external_ref"] is not None):
        raise WorkspaceError("dispatch_state_conflict")
    item = _item(connection, task_id)
    if item["revision"] != expected_revision:
        raise WorkspaceError("item_changed")
    if item["assigned_agent_id"] != agent_id:
        raise WorkspaceError("not_assigned")
    if item["state"] in ("review", "done"):
        raise WorkspaceError("review_not_ready")
    if _active_run(connection, task_id) is not None:
        raise WorkspaceError("run_conflict")
    stamp = now_utc()
    run_id = str(uuid4())
    connection.execute(
        "INSERT INTO agent_runs(id,task_id,agent_id,status,started_at,heartbeat_at,ended_at,summary,"
        "evidence_json,dispatch_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run_id, task_id, agent_id, "claimed", stamp, stamp, None, "", "[]", dispatch_id))
    updated = connection.execute(
        "UPDATE task_dispatches SET delivery_mode='auto',delivery_state='sent',external_ref=?,run_id=?,"
        "sent_at=?,updated_at=? WHERE id=? AND delivery_state='previewed' AND run_id IS NULL "
        "AND external_ref IS NULL",
        (execution_run_id, run_id, stamp, stamp, dispatch_id))
    if updated.rowcount != 1:
        raise WorkspaceError("dispatch_state_conflict")
    item = store._update_item(connection, item, {"state": "doing"}, expected_revision)
    _event(connection, task_id, run_id, "claimed", {
        "source": "execution_bridge", "dispatch_id": dispatch_id, "execution_run_id": execution_run_id})
    return {"item": item, "agent_run_id": run_id}


def project_execution_state(store, connection, *, execution_run_id, task_id, agent_run_id,
                            dispatch_id, state, cleanup_state, turn_send_started, turn_id=None,
                            reason=None, summary=None, evidence=None, events=None, approvals=None,
                            observed_at=None):
    """Project one execution ledger fact onto the item projection, idempotently."""
    from control_everything.workspace import dispatch as dispatch_module
    if not connection.in_transaction:
        raise WorkspaceError("transaction_required")
    binding = dispatch_module.execution_binding(connection, dispatch_id=dispatch_id,
                                                execution_run_id=execution_run_id, task_id=task_id)
    if binding["agent_run_id"] != agent_run_id:
        raise WorkspaceError("execution_link_conflict")
    item = _item(connection, task_id)
    agent_run = connection.execute("SELECT * FROM agent_runs WHERE id=? AND task_id=?", (agent_run_id, task_id)).fetchone()
    if agent_run is None:
        raise WorkspaceError("execution_link_conflict")
    active = connection.execute(
        "SELECT id FROM agent_runs WHERE task_id=? AND status IN ('claimed','running','waiting_review') LIMIT 1",
        (task_id,)).fetchone()
    if active is not None and active["id"] != agent_run_id:
        return {"item": item, "changed": False, "reason": "newer_run"}
    if agent_run["status"] in ("completed", "changes_requested", "failed", "cancelled"):
        return {"item": item, "changed": False, "reason": "human_closed"}
    turn_bound = bool(turn_id)
    if item["state"] in ("review", "done") and not (
            state == "completed" and cleanup_state == "verified"
            and agent_run["status"] == "waiting_review"):
        return {"item": item, "changed": False, "reason": "human_state"}
    target_item, target_run, delivery = "doing", "running", None
    terminal = None
    cleanup_unverified = cleanup_state == "unverified"
    terminal_fact = state in ("completed", "failed", "interrupted", "cancelled_before_send")
    if cleanup_unverified:
        target_item = "blocked"
        target_run = agent_run["status"] if agent_run["status"] in ("claimed", "running", "waiting_review") else "running"
        delivery = None if turn_bound else "unknown"
        terminal = None
    elif state == "unknown":
        target_item = "blocked"
        target_run = agent_run["status"] if agent_run["status"] in ("claimed", "running", "waiting_review") else "running"
        delivery = "unknown" if not turn_bound else None
        terminal = None
    elif state == "starting":
        target_item, target_run, delivery = "doing", "claimed", "sent"
    elif state in ("running", "cancel_requested"):
        target_item, target_run = "doing", "running"
        delivery = "delivered" if turn_bound else "sent"
    elif state == "waiting_approval":
        target_item, target_run = "blocked", "running"
        delivery = "delivered" if turn_bound else "sent"
    elif terminal_fact and cleanup_state != "verified":
        target_item = "blocked"
        target_run = agent_run["status"] if agent_run["status"] in ("claimed", "running", "waiting_review") else "running"
        delivery = None if turn_bound else "unknown"
        terminal = None
    elif state == "completed":
        target_item, target_run = "review", "waiting_review"
        delivery = "delivered" if turn_bound else "sent"
        terminal = "completed"
    elif state == "failed":
        target_item, target_run, delivery, terminal = "blocked", "failed", "failed", "failed"
    elif state in ("interrupted", "cancelled_before_send"):
        target_item, target_run, delivery, terminal = "blocked", "cancelled", "cancelled", "cancelled"
    else:
        return {"item": item, "changed": False, "reason": "unmapped_state"}
    changed = False
    final_text = None
    files = []
    for event in events or []:
        payload = event.get("payload") or {}
        if event.get("kind") == "agent_message" and payload.get("final") is True and payload.get("text"):
            final_text = payload["text"]
        if event.get("kind") == "file_change":
            changes = payload.get("changes")
            candidates = changes if isinstance(changes, list) else [payload]
            for change in candidates:
                path = change.get("path") if isinstance(change, dict) else None
                if isinstance(path, str) and path and len(path) <= 2048 and path not in files:
                    files.append(path)
    summary_value = final_text if final_text is not None else (summary or "")
    if state == "completed" and not summary_value:
        summary_value = "未返回最终文本"
    if len(summary_value) > 4000:
        marker = "\n…[truncated]"
        summary_value = summary_value[:4000 - len(marker)] + marker
    evidence_value = (evidence if isinstance(evidence, list) else [])[:20] or files[:20]
    if item["state"] != target_item:
        item = store._update_item(connection, item, {"state": target_item}, item["revision"])
        changed = True
    if delivery is not None:
        current = connection.execute("SELECT delivery_state FROM task_dispatches WHERE id=?", (dispatch_id,)).fetchone()
        if current is not None and current["delivery_state"] != delivery:
            connection.execute("UPDATE task_dispatches SET delivery_state=?,updated_at=? WHERE id=?",
                               (delivery, observed_at or now_utc(), dispatch_id))
            changed = True
    if (agent_run["status"] != target_run or agent_run["summary"] != summary_value
            or json.loads(agent_run["evidence_json"] or "[]") != evidence_value
            or (terminal is not None and agent_run["ended_at"] is None)):
        ended = agent_run["ended_at"] if terminal is None else (observed_at or now_utc())
        connection.execute(
            "UPDATE agent_runs SET status=?,heartbeat_at=?,ended_at=?,summary=?,evidence_json=? WHERE id=?",
            (target_run, observed_at or now_utc(), ended, summary_value,
             json.dumps(evidence_value, ensure_ascii=False), agent_run_id))
        changed = True
    pending = [row for row in (approvals or []) if row.get("status") == "pending"]
    projected = []
    if state == "waiting_approval":
        projected = [("progress", {"source": "execution_bridge", "execution_run_id": execution_run_id,
                                   "stage": "waiting_user", "wait_kind": "tool_authorization",
                                   "request_id": row.get("id"), "message": reason or "等待工具授权"})
                     for row in pending]
    elif state == "completed" and cleanup_state == "verified":
        projected = [("waiting_review", {"source": "execution_bridge", "execution_run_id": execution_run_id,
                                         "summary": summary_value, "evidence": evidence_value,
                                         "reason": "execution_completed"})]
    elif state == "failed" and cleanup_state == "verified":
        projected = [("failed", {"source": "execution_bridge", "execution_run_id": execution_run_id,
                                 "reason": reason or "execution_failed"})]
    elif state in ("interrupted", "cancelled_before_send") and cleanup_state == "verified":
        projected = [("failed", {"source": "execution_bridge", "execution_run_id": execution_run_id,
                                 "reason": reason or "execution_cancelled"})]
    elif cleanup_state == "unverified" or state == "unknown" or (terminal_fact and cleanup_state != "verified"):
        reason_text = "cleanup_unverified" if cleanup_state == "unverified" else (
            "cleanup_pending" if terminal_fact else "execution_unknown")
        projected = [("blocked", {"source": "execution_bridge", "execution_run_id": execution_run_id,
                                  "reason": reason_text})]
    for kind, payload in projected:
        key = f"execution-projection:{execution_run_id}:{kind}:{payload.get('request_id') or state}:{cleanup_state}"
        event_id = str(uuid5(NAMESPACE_URL, key))
        if connection.execute("SELECT 1 FROM task_events WHERE id=?", (event_id,)).fetchone() is None:
            connection.execute(
                "INSERT INTO task_events(id,task_id,run_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (event_id, task_id, agent_run_id, kind, json.dumps(payload, ensure_ascii=False),
                 observed_at or now_utc()))
            changed = True
    return {"item": item, "changed": changed, "state": target_item, "run_status": target_run}


def _execution_managed(connection, run):
    if run is None or not run["dispatch_id"]:
        return False
    dispatch = connection.execute("SELECT delivery_mode,external_ref FROM task_dispatches WHERE id=?",
                                  (run["dispatch_id"],)).fetchone()
    return bool(dispatch and dispatch["external_ref"] and dispatch["delivery_mode"] == "auto")


def progress(store, agent_id, task_id, expected_revision, stage, percent=None, message=None, project_ids=None,
             wait_kind=None, request_id=None, agent_hint=None, run_id=None):
    if type(expected_revision) is not int or not 1 <= expected_revision <= MAX_REVISION:
        raise WorkspaceError("invalid_agent_request")
    if type(run_id) is not str or not 1 <= len(run_id) <= 64:
        raise WorkspaceError("invalid_agent_request")
    if stage not in STAGES:
        raise WorkspaceError("invalid_agent_request")
    if percent is not None and (type(percent) is not int or not 0 <= percent <= 100):
        raise WorkspaceError("invalid_agent_request")
    if message is not None:
        message = _text(message, "message", 1000, required=False)
    if stage == "waiting_user":
        wait_kind = wait_kind or "unknown"
        if wait_kind not in WAIT_KINDS or not message or not message.strip():
            raise WorkspaceError("invalid_agent_request")
        if request_id is not None:
            request_id = _text(request_id, "request_id", 200, required=False)
        if agent_hint is not None:
            agent_hint = _text(agent_hint, "agent_hint", 500, required=False)
    elif wait_kind is not None or request_id is not None or agent_hint is not None:
        raise WorkspaceError("invalid_agent_request")
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = _agent_item(connection, task_id, agent_id, project_ids)
        if item["revision"] != expected_revision:
            raise WorkspaceError("item_changed")
        if item["state"] in ("review", "done"):
            raise WorkspaceError("review_not_ready")
        run = _active_run(connection, task_id)
        if run is None or run["agent_id"] != agent_id:
            raise WorkspaceError("run_not_found")
        if run["id"] != run_id:
            raise WorkspaceError("run_mismatch")
        if _execution_managed(connection, run):
            raise WorkspaceError("execution_managed_run")
        stamp = now_utc()
        connection.execute("UPDATE agent_runs SET status='running', heartbeat_at=? WHERE id=?", (stamp, run["id"]))
        target_state = "blocked" if stage in ("blocked", "waiting_user") else "doing"
        if item["state"] != target_state:
            item = store._update_item(connection, item, {"state": target_state}, expected_revision)
        payload = {"stage": stage, "percent": percent, "message": message or ""}
        if stage == "waiting_user":
            payload.update(wait_kind=wait_kind, request_id=request_id, agent_hint=agent_hint)
        _event(connection, task_id, run["id"], "progress", payload)
        return {"item": item, "run": _run_view(_active_run(connection, task_id))}


def cancel_run(store, task_id, expected_revision, reason):
    """Human release; it does not terminate any external process."""
    from control_everything.workspace import dispatch as dispatch_module
    reason = _text(reason, "reason", 1000)
    if type(expected_revision) is not int or not 1 <= expected_revision <= MAX_REVISION:
        raise WorkspaceError("invalid_agent_request")
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, task_id)
        if item["revision"] != expected_revision:
            raise WorkspaceError("item_changed")
        run = _active_run(connection, task_id)
        if run is None:
            raise WorkspaceError("run_not_found")
        payload = {"reason": "human_cancelled", "detail": reason}
        managed = connection.execute(
            "SELECT d.* FROM task_dispatches d WHERE (d.id=? OR d.run_id=?) "
            "AND ((d.delivery_mode='auto' AND d.external_ref IS NOT NULL) "
            "OR EXISTS(SELECT 1 FROM execution_runs e WHERE e.request_id=d.id))",
            (run["dispatch_id"], run["id"])).fetchall()
        if managed:
            if len(managed) != 1 or managed[0]["delivery_mode"] != "auto":
                raise WorkspaceError("execution_link_conflict")
            dispatch = managed[0]
            try:
                binding = dispatch_module.execution_binding(
                    connection, dispatch_id=dispatch["id"],
                    execution_run_id=dispatch["external_ref"], task_id=task_id)
            except ProfileError as error:
                raise WorkspaceError("execution_link_conflict") from error
            execution = connection.execute("SELECT * FROM execution_runs WHERE id=?",
                                           (dispatch["external_ref"],)).fetchone()
            if (binding["agent_run_id"] != run["id"] or execution is None
                    or execution["request_id"] != dispatch["id"] or execution["task_id"] != task_id
                    or execution["project_id"] != dispatch["project_id"]
                    or item["project_id"] != dispatch["project_id"]
                    or item["assigned_agent_id"] != dispatch["agent_id"]):
                raise WorkspaceError("execution_link_conflict")
            if execution["state"] != "unknown" or execution["cleanup_state"] != "verified":
                raise WorkspaceError("execution_managed_run")
            # Release only the business claim; preserve the unknown execution and delivery facts.
            payload.update(reason="human_released_unknown", execution_run_id=execution["id"])
        stamp = now_utc()
        connection.execute("UPDATE agent_runs SET status='cancelled', ended_at=?, heartbeat_at=? WHERE id=?",
                           (stamp, stamp, run["id"]))
        if item["state"] in ("doing", "blocked", "review"):
            item = store._update_item(connection, item, {"state": "todo"}, expected_revision)
        _event(connection, task_id, run["id"], "failed", payload)
        return {"item": item, "run": _run_view(_latest_run(connection, task_id))}


def submit_review(store, agent_id, task_id, expected_revision, summary, evidence=None, project_ids=None, run_id=None):
    if type(expected_revision) is not int or not 1 <= expected_revision <= MAX_REVISION:
        raise WorkspaceError("invalid_agent_request")
    if type(run_id) is not str or not 1 <= len(run_id) <= 64:
        raise WorkspaceError("invalid_agent_request")
    summary = _text(summary, "summary", 4000)
    if evidence is None:
        evidence = []
    if type(evidence) is not list or len(evidence) > 20:
        raise WorkspaceError("invalid_agent_request")
    evidence = [_text(entry, "evidence", 2048) for entry in evidence]
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = _agent_item(connection, task_id, agent_id, project_ids)
        if item["revision"] != expected_revision:
            raise WorkspaceError("item_changed")
        run = _active_run(connection, task_id)
        if run is None or run["agent_id"] != agent_id:
            raise WorkspaceError("run_not_found")
        if run["id"] != run_id:
            raise WorkspaceError("run_mismatch")
        if _execution_managed(connection, run):
            raise WorkspaceError("execution_managed_run")
        if item["state"] == "review" or run["status"] == "waiting_review":
            raise WorkspaceError("review_already_submitted")
        stamp = now_utc()
        connection.execute(
            "UPDATE agent_runs SET status='waiting_review', heartbeat_at=?, summary=?, evidence_json=? WHERE id=?",
            (stamp, summary, _json(evidence), run["id"]))
        if item["state"] != "review":
            item = store._update_item(connection, item, {"state": "review"}, expected_revision)
        _event(connection, task_id, run["id"], "waiting_review", {"summary": summary, "evidence": evidence})
        return {"item": item, "run": _run_view(_active_run(connection, task_id))}


def close_run(connection, task_id, verdict):
    """Called by the human review transaction; never lets the agent decide done."""
    run = _active_run(connection, task_id)
    if run is None:
        return
    status = "completed" if verdict == "approved" else "changes_requested"
    stamp = now_utc()
    connection.execute("UPDATE agent_runs SET status=?, heartbeat_at=?, ended_at=? WHERE id=?",
                       (status, stamp, stamp, run["id"]))
    _event(connection, task_id, run["id"], "completed" if status == "completed" else "changes_requested",
           {"verdict": verdict})
