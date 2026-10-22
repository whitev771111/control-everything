"""Versioned task briefs and human-confirmed dispatch snapshots."""

import hashlib
import json
from uuid import uuid4

from control_everything.input_validation import ProfileError
from control_everything.workspace.store import WorkspaceError, now_utc
from control_everything.workspace.task_schema import MAX_REVISION

BRIEF_FIELDS = {"request_text", "acceptance_text", "constraints_text", "references", "execution_directory_id"}
DISPATCH_ACTIVE = ("previewed", "sent", "delivered")


def require_supported_execution_agent(agent):
    adapter = agent.get("adapter")
    if adapter not in ("codex", "dsh"):
        raise ProfileError("execution_unsupported", "agent.adapter", status=409)
    if agent.get("connection"):
        raise ProfileError("execution_unsupported", "agent.connection", status=409)
    commands = {"codex": (None, "", "codex"), "dsh": (None, "", "dsh")}
    if agent.get("command") not in commands[adapter]:
        raise ProfileError("execution_unsupported", "agent.command", status=409)


def _task(connection, task_id):
    row = connection.execute("SELECT * FROM task_items WHERE id=? AND origin='task'", (task_id,)).fetchone()
    if row is None:
        raise ProfileError("task_not_found", status=404)
    return dict(row)


def _text(value, field, maximum, *, required=False):
    if type(value) is not str or len(value) > maximum or "\0" in value or required and not value.strip():
        raise ProfileError("invalid_brief", field)
    return value


def _references(value):
    if type(value) is not list or len(value) > 20:
        raise ProfileError("invalid_brief", "references")
    return [_text(item, "references", 2048, required=True) for item in value]


def _directory(connection, project_id, directory_id):
    if project_id is None:
        raise ProfileError("project_required", "project_id", status=409)
    rows = connection.execute(
        "SELECT binding_id,path FROM project_directories WHERE project_id=? ORDER BY is_primary DESC,binding_id",
        (project_id,)).fetchall()
    if not rows:
        raise ProfileError("directory_required", "directory_id", status=409)
    if directory_id is None:
        if len(rows) != 1:
            raise ProfileError("directory_required", "directory_id", status=409)
        directory_id = rows[0]["binding_id"]
    row = next((row for row in rows if row["binding_id"] == directory_id), None)
    if row is None:
        raise ProfileError("invalid_brief", "execution_directory_id")
    return dict(row)


def _brief(connection, task_id):
    row = connection.execute("SELECT * FROM task_briefs WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        return {"task_id": task_id, "request_text": "", "acceptance_text": "", "constraints_text": "",
                "references": [], "execution_directory_id": None, "revision": 0, "updated_at": None}
    value = dict(row)
    value["references"] = json.loads(value.pop("references_json") or "[]")
    return value


def _brief_body(body):
    if type(body) is not dict or set(body) != {"expected_revision"} | BRIEF_FIELDS:
        raise ProfileError("invalid_brief", "body")
    revision = body["expected_revision"]
    if type(revision) is not int or not 0 <= revision <= MAX_REVISION:
        raise ProfileError("invalid_brief", "expected_revision")
    request = _text(body["request_text"], "request_text", 20000, required=True)
    acceptance = _text(body["acceptance_text"], "acceptance_text", 10000)
    constraints = _text(body["constraints_text"], "constraints_text", 10000)
    references = _references(body["references"])
    directory = body["execution_directory_id"]
    if directory is not None:
        directory = _text(directory, "execution_directory_id", 64, required=True)
    return revision, request, acceptance, constraints, references, directory


def get_brief(profile, task_id):
    with profile.store.connection() as connection:
        connection.execute("BEGIN")
        item = _task(connection, task_id)
        brief = _brief(connection, task_id)
        directories = [dict(row) for row in connection.execute(
            "SELECT binding_id,path,is_primary FROM project_directories WHERE project_id=? ORDER BY is_primary DESC,binding_id",
            (item["project_id"],))]
        return {"item": item, "brief": brief, "directories": directories}


def save_brief(profile, task_id, body):
    expected, request, acceptance, constraints, references, directory = _brief_body(body)
    with profile.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = _task(connection, task_id)
        if directory is not None:
            _directory(connection, item["project_id"], directory)
        current = _brief(connection, task_id)
        if current["revision"] != expected:
            raise ProfileError("brief_changed", status=409)
        if current["revision"] >= MAX_REVISION:
            raise ProfileError("revision_exhausted", "brief", status=409)
        stamp = now_utc()
        revision = current["revision"] + 1
        connection.execute(
            "INSERT INTO task_briefs(task_id,request_text,acceptance_text,constraints_text,references_json,"
            "execution_directory_id,revision,updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET request_text=excluded.request_text,"
            "acceptance_text=excluded.acceptance_text,constraints_text=excluded.constraints_text,"
            "references_json=excluded.references_json,execution_directory_id=excluded.execution_directory_id,"
            "revision=excluded.revision,updated_at=excluded.updated_at",
            (task_id, request, acceptance, constraints, json.dumps(references, ensure_ascii=False),
             directory, revision, stamp))
    return get_brief(profile, task_id)


def _agent(agent_settings, agent_id):
    agent = next((row for row in agent_settings["items"] if row["id"] == agent_id), None)
    if agent is None or not agent["enabled"]:
        raise ProfileError("agent_not_found", "agent_id", status=404)
    return agent


def _snapshot(connection, task_id, agent_id, directory_id, agent_settings, delivery_mode="manual"):
    item = _task(connection, task_id)
    agent = _agent(agent_settings, agent_id)
    if agent["project_ids"] and item["project_id"] not in agent["project_ids"]:
        raise ProfileError("project_forbidden", "agent_id", status=403)
    directory = _directory(connection, item["project_id"], directory_id)
    brief = _brief(connection, task_id)
    if not brief["request_text"].strip():
        raise ProfileError("brief_required", "request_text", status=409)
    if brief["execution_directory_id"] is not None and brief["execution_directory_id"] != directory["binding_id"]:
        raise ProfileError("brief_changed", "execution_directory_id", status=409)
    project = connection.execute("SELECT name FROM projects WHERE id=?", (item["project_id"],)).fetchone()
    payload = {
        "task_id": item["id"], "title": item["title"], "project_id": item["project_id"],
        "project_name": project["name"] if project else None,
        "agent_id": agent["id"], "agent_name": agent["name"],
        "permission": agent.get("permission", "workspace-write"),
        "network": agent.get("network", "allowed"),
        "approval": agent.get("approval", "request_user"),
        "directory_id": directory["binding_id"], "directory_path": directory["path"],
        "brief_revision": brief["revision"], "request_text": brief["request_text"],
        "acceptance_text": brief["acceptance_text"], "constraints_text": brief["constraints_text"],
        "references": brief["references"], "delivery_mode": delivery_mode,
    }
    review = connection.execute(
        "SELECT id,verdict,reason FROM task_reviews WHERE task_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (task_id,)).fetchone()
    if review is not None and review["verdict"] in ("changes_requested", "rejected"):
        payload["revision_request"] = dict(review)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    lines = [
        "# Workbench Task",
        f"Task: {payload['title']}",
        f"Task ID: {payload['task_id']}",
        f"Project: {payload['project_name'] or payload['project_id']}",
        f"Working directory: {payload['directory_path']}",
        f"Agent: {payload['agent_name']}",
        f"Execution permission: {payload['permission']}",
        f"Network: {payload['network']}",
        "",
        "## Request",
        payload["request_text"],
        "",
        "## Acceptance criteria",
        payload["acceptance_text"] or "(not provided)",
        "",
        "## Constraints",
        payload["constraints_text"] or "(not provided)",
        "",
        "## References",
        *([f"- {entry}" for entry in payload["references"]] or ["(none)"]),
    ]
    if "revision_request" in payload:
        lines += ["", "## Human revision request", payload["revision_request"]["reason"],
                  "Continue from the existing project files. Address the human's reason above,",
                  "then report your changes for another human review."]
    if delivery_mode == "manual":
        lines += ["", "## How to work",
                  "This is a manual handoff. Configure WORKBENCH_URL and WORKBENCH_TOKEN in the",
                  "agent's private environment, then use:",
                  "  python -m control_everything.integrations.agent_cli task list",
                  "  python -m control_everything.integrations.agent_cli task claim <task-id> --expected-revision <revision>",
                  "  python -m control_everything.integrations.agent_cli task progress ... --run-id <run-id>",
                  "  python -m control_everything.integrations.agent_cli task submit ... --run-id <run-id>",
                  "Do not reuse an old run ID. The human must approve before the task is done."]
    else:
        lines += ["", "## Execution instructions",
                  "Work only in the working directory above. Report observable results and",
                  "leave the task waiting for human review; do not mark it done yourself."]
    content = "\n".join(lines)
    return payload, canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), content


def execution_facts(profile, store, dispatch_id, *, connection=None, agent_settings=None):
    """Read and validate business facts; execution owns path/hash/assembly."""
    if connection is not None:
        if agent_settings is None:
            raise ProfileError("agent_snapshot_required", "agent_settings", status=409)
        return _execution_facts(connection, profile, dispatch_id, agent_settings)
    settings = agent_settings if agent_settings is not None else profile.agent_settings()
    with store.connection() as connection:
        connection.execute("BEGIN")
        return _execution_facts(connection, profile, dispatch_id, settings)


def _execution_facts(connection, profile, dispatch_id, agent_settings):
    row = connection.execute("SELECT * FROM task_dispatches WHERE id=?", (dispatch_id,)).fetchone()
    if row is None:
        raise ProfileError("dispatch_not_found", status=404)
    dispatch = dict(row)
    if (dispatch["delivery_state"] != "previewed" or dispatch["run_id"] is not None
            or dispatch["external_ref"] is not None):
        raise ProfileError("dispatch_state_conflict", "delivery_state", status=409)
    task = _task(connection, dispatch["task_id"])
    if task["state"] in ("review", "done"):
        raise ProfileError("dispatch_state_conflict", "state", status=409)
    if connection.execute(
            "SELECT 1 FROM agent_runs WHERE task_id=? AND status IN ('claimed','running','waiting_review')",
            (task["id"],)).fetchone() is not None:
        raise ProfileError("run_conflict", "task_id", status=409)
    if dispatch["project_id"] is None or task["project_id"] != dispatch["project_id"]:
        raise ProfileError("project_required", "project_id", status=409)
    project = connection.execute("SELECT id,name FROM projects WHERE id=?", (dispatch["project_id"],)).fetchone()
    if project is None:
        raise ProfileError("project_not_found", "project_id", status=404)
    directory = connection.execute(
        "SELECT binding_id,path FROM project_directories WHERE project_id=? AND binding_id=?",
        (dispatch["project_id"], dispatch["directory_id"])).fetchone()
    if directory is None or dispatch["directory_path"] is None:
        raise ProfileError("directory_required", "directory_id", status=409)
    settings = agent_settings if agent_settings is not None else profile.agent_settings()
    agent = next((item for item in settings["items"] if item["id"] == dispatch["agent_id"]), None)
    if agent is None or not agent["enabled"]:
        raise ProfileError("agent_not_found", "agent_id", status=404)
    if agent["project_ids"] and dispatch["project_id"] not in agent["project_ids"]:
        raise ProfileError("project_forbidden", "agent_id", status=403)
    if task["assigned_agent_id"] != dispatch["agent_id"]:
        raise ProfileError("run_conflict", "assigned_agent_id", status=409)
    require_supported_execution_agent(agent)
    return {"dispatch": dispatch, "task": task, "project": dict(project),
            "directory": dict(directory), "agent": agent}


def execution_binding(connection, *, dispatch_id, execution_run_id, task_id):
    dispatch = connection.execute("SELECT * FROM task_dispatches WHERE id=?", (dispatch_id,)).fetchone()
    if dispatch is None:
        raise ProfileError("execution_link_conflict", "dispatch_id", status=409)
    run_id = dispatch["run_id"]
    agent_run = connection.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone() if run_id else None
    if (dispatch["external_ref"] != execution_run_id or dispatch["task_id"] != task_id
            or run_id is None or agent_run is None
            or agent_run["task_id"] != task_id or agent_run["agent_id"] != dispatch["agent_id"]
            or agent_run["dispatch_id"] != dispatch_id):
        raise ProfileError("execution_link_conflict", "binding", status=409)
    return {"dispatch_id": dispatch_id, "agent_run_id": run_id}


def execution_run_facts(profile, store, *, execution_run_id, agent_run_id, dispatch_id, task_id,
                        connection=None, agent_settings=None):
    """Read facts for an already admitted dispatch execution without re-admission checks."""
    if connection is not None:
        if agent_settings is None:
            raise ProfileError("agent_snapshot_required", "agent_settings", status=409)
        return _execution_run_facts(connection, dispatch_id, execution_run_id, agent_run_id, task_id,
                                    agent_settings)
    settings = agent_settings if agent_settings is not None else profile.agent_settings()
    with store.connection() as connection:
        connection.execute("BEGIN")
        return _execution_run_facts(connection, dispatch_id, execution_run_id, agent_run_id, task_id, settings)


def _execution_run_facts(connection, dispatch_id, execution_run_id, agent_run_id, task_id, agent_settings):
    dispatch = connection.execute("SELECT * FROM task_dispatches WHERE id=?", (dispatch_id,)).fetchone()
    if dispatch is None:
        raise ProfileError("execution_link_conflict", "dispatch_id", status=409)
    dispatch = dict(dispatch)
    agent_run = connection.execute("SELECT * FROM agent_runs WHERE id=?", (agent_run_id,)).fetchone()
    if (dispatch["external_ref"] != execution_run_id or dispatch["run_id"] != agent_run_id
            or dispatch["task_id"] != task_id or agent_run is None
            or agent_run["task_id"] != task_id or agent_run["agent_id"] != dispatch["agent_id"]
            or agent_run["dispatch_id"] != dispatch_id):
        raise ProfileError("execution_link_conflict", "binding", status=409)
    execution = connection.execute("SELECT * FROM execution_runs WHERE id=?", (execution_run_id,)).fetchone()
    if (execution is None or execution["request_id"] != dispatch_id or execution["task_id"] != task_id
            or execution["project_id"] != dispatch["project_id"]):
        raise ProfileError("execution_link_conflict", "execution", status=409)
    task = _task(connection, task_id)
    if task["project_id"] != dispatch["project_id"] or task["assigned_agent_id"] != dispatch["agent_id"]:
        raise ProfileError("execution_link_conflict", "task", status=409)
    project = connection.execute("SELECT id,name FROM projects WHERE id=?", (dispatch["project_id"],)).fetchone()
    directory = connection.execute(
        "SELECT binding_id,path FROM project_directories WHERE project_id=? AND binding_id=?",
        (dispatch["project_id"], dispatch["directory_id"])).fetchone()
    if project is None or directory is None:
        raise ProfileError("execution_link_conflict", "directory", status=409)
    agent = next((item for item in agent_settings["items"] if item["id"] == dispatch["agent_id"]), None)
    if agent is None or not agent["enabled"]:
        raise ProfileError("execution_link_conflict", "agent", status=409)
    require_supported_execution_agent(agent)
    try:
        stored_adapter = json.loads(execution["effective_policy_json"] or "{}").get("adapter", "codex")
    except (TypeError, ValueError):
        raise ProfileError("execution_link_conflict", "adapter", status=409) from None
    if stored_adapter != agent["adapter"]:
        raise ProfileError("execution_link_conflict", "adapter", status=409)
    if agent["project_ids"] and dispatch["project_id"] not in agent["project_ids"]:
        raise ProfileError("execution_link_conflict", "agent", status=409)
    return {"dispatch": dispatch, "task": task, "project": dict(project),
            "directory": dict(directory), "agent": agent, "execution": dict(execution)}


def preview_dispatch(profile, task_id, body):
    if type(body) is not dict or set(body) not in ({"agent_id", "directory_id"},
                                                   {"agent_id", "directory_id", "delivery_mode"}):
        raise ProfileError("invalid_dispatch", "body")
    delivery_mode = body.get("delivery_mode", "manual")
    if delivery_mode not in ("manual", "auto"):
        raise ProfileError("invalid_dispatch", "delivery_mode")
    agent_settings = profile.agent_settings()
    with profile.store.connection() as connection:
        connection.execute("BEGIN")
        item = _task(connection, task_id)
        payload, _, digest, content = _snapshot(connection, task_id, body["agent_id"], body["directory_id"],
                                                agent_settings, delivery_mode)
        return {"status": "preview", "item": item, "dispatch": payload, "brief_sha256": digest, "content_text": content}


def create_dispatch(profile, task_id, body):
    required = {"agent_id", "directory_id", "expected_brief_revision", "confirmed"}
    if type(body) is not dict or not required <= set(body) or set(body) - required - {"preview_sha256", "delivery_mode"}:
        raise ProfileError("invalid_dispatch", "body")
    delivery_mode = body.get("delivery_mode", "manual")
    if delivery_mode not in ("manual", "auto"):
        raise ProfileError("invalid_dispatch", "delivery_mode")
    preview_sha256 = body.get("preview_sha256")
    if preview_sha256 is not None and (type(preview_sha256) is not str
                                      or len(preview_sha256) != 64
                                      or any(char not in "0123456789abcdef" for char in preview_sha256)):
        raise ProfileError("invalid_dispatch", "preview_sha256")
    if body["confirmed"] is not True:
        raise ProfileError("confirmation_required", "confirmed", status=422)
    revision = body["expected_brief_revision"]
    if type(revision) is not int or not 0 <= revision <= MAX_REVISION:
        raise ProfileError("invalid_dispatch", "expected_brief_revision")
    agent_settings = profile.agent_settings()
    with profile.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = _task(connection, task_id)
        if item["state"] in ("review", "done"):
            raise ProfileError("dispatch_state_conflict", status=409)
        brief = _brief(connection, task_id)
        if brief["revision"] != revision:
            raise ProfileError("brief_changed", status=409)
        if connection.execute(
                "SELECT 1 FROM agent_runs WHERE task_id=? AND status IN ('claimed','running','waiting_review')",
                (task_id,)).fetchone() is not None:
            raise ProfileError("run_conflict", status=409)
        payload, canonical, digest, content = _snapshot(connection, task_id, body["agent_id"],
                                                        body["directory_id"], agent_settings, delivery_mode)
        if preview_sha256 is not None and preview_sha256 != digest:
            raise ProfileError("dispatch_preview_changed", "preview_sha256", status=409)
        existing = connection.execute(
            "SELECT * FROM task_dispatches WHERE task_id=? AND agent_id=? AND brief_sha256=? "
            "AND delivery_mode=? AND delivery_state IN ('previewed','sent','delivered') AND run_id IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (task_id, body["agent_id"], digest, delivery_mode)).fetchone()
        if item["assigned_agent_id"] != payload["agent_id"]:
            profile.store._update_item(connection, item, {"assigned_agent_id": payload["agent_id"]}, item["revision"])
        if existing is not None:
            return {"status": "existing", "dispatch": dict(existing), "duplicate": True}
        identity = str(uuid4())
        stamp = now_utc()
        connection.execute(
            "INSERT INTO task_dispatches(id,task_id,agent_id,project_id,directory_id,directory_path,brief_json,"
            "brief_sha256,content_text,delivery_mode,delivery_state,external_ref,session_id,run_id,created_at,"
            "sent_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity, task_id, payload["agent_id"], payload["project_id"], payload["directory_id"],
             payload["directory_path"], canonical, digest, content, delivery_mode, "previewed", None, None, None,
             stamp, None, stamp))
        value = dict(connection.execute("SELECT * FROM task_dispatches WHERE id=?", (identity,)).fetchone())
        return {"status": "previewed", "dispatch": value, "duplicate": False}


def list_dispatches(store, task_id):
    with store.connection() as connection:
        connection.execute("BEGIN")
        _task(connection, task_id)
        return {"items": [dict(row) for row in connection.execute(
            "SELECT * FROM task_dispatches WHERE task_id=? ORDER BY created_at DESC,id DESC", (task_id,))]}


def get_dispatch(store, dispatch_id):
    with store.connection() as connection:
        connection.execute("BEGIN")
        row = connection.execute("SELECT * FROM task_dispatches WHERE id=?", (dispatch_id,)).fetchone()
        if row is None:
            raise ProfileError("dispatch_not_found", status=404)
        return dict(row)


def mark_sent(store, dispatch_id):
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM task_dispatches WHERE id=?", (dispatch_id,)).fetchone()
        if row is None:
            raise ProfileError("dispatch_not_found", status=404)
        if row["delivery_state"] != "previewed":
            raise ProfileError("dispatch_state_conflict", status=409)
        stamp = now_utc()
        connection.execute("UPDATE task_dispatches SET delivery_state='sent',sent_at=?,updated_at=? WHERE id=?",
                           (stamp, stamp, dispatch_id))
        return dict(connection.execute("SELECT * FROM task_dispatches WHERE id=?", (dispatch_id,)).fetchone())


def cancel_dispatch(store, dispatch_id):
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM task_dispatches WHERE id=?", (dispatch_id,)).fetchone()
        if row is None:
            raise ProfileError("dispatch_not_found", status=404)
        if row["delivery_state"] not in DISPATCH_ACTIVE:
            raise ProfileError("dispatch_state_conflict", status=409)
        stamp = now_utc()
        connection.execute("UPDATE task_dispatches SET delivery_state='cancelled',updated_at=? WHERE id=?",
                           (stamp, dispatch_id))
        return dict(connection.execute("SELECT * FROM task_dispatches WHERE id=?", (dispatch_id,)).fetchone())


def link_claim(connection, task_id, agent_id, run_id):
    dispatch = connection.execute(
        "SELECT * FROM task_dispatches WHERE task_id=? AND agent_id=? AND run_id IS NULL "
        "AND delivery_state IN ('sent','delivered') ORDER BY created_at DESC,id DESC LIMIT 1",
        (task_id, agent_id)).fetchone()
    if dispatch is None:
        return None
    connection.execute("UPDATE task_dispatches SET run_id=?,updated_at=? WHERE id=?",
                       (run_id, now_utc(), dispatch["id"]))
    return dispatch["id"]
