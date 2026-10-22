"""Admission and HTTP-facing operations for owned Codex execution."""

from contextlib import ExitStack
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from control_everything.execution.store import ACTIVE_STATES, ExecutionStore, encode, public_approval, public_run
from control_everything.input_validation import ProfileError
from control_everything.platform.windows_acl import directory_lock
from control_everything.sessions.source import canonical_uuid
from control_everything.workspace.sources import native_codex
from control_everything.workspace.store import WorkspaceError

# Keep native directory leases until process exit if child cleanup is unverified.
_UNVERIFIED_LEASES = {}

POLICY = {"mode": "workspace-write", "approval_policy": "on-request", "approvals_reviewer": "user",
          "network_access": False, "writable_roots": [],
          "inheritance_note": "本次使用工作台专用 CODEX_HOME 与官方 file 登录；不继承个人模型凭据或外部扩展覆盖，工具操作仍由本机审批。"}
POLICY_VERSION = "workspace-codex-v2"
DSH_POLICY = {
    "mode": "workspace-write",
    "approval_policy": "request_user",
    "approvals_reviewer": "user",
    "network_access": "allowed",
    "writable_roots": [],
    "inheritance_note": "DSH headless 使用其原生文件沙箱；Windows 上限制写入/删除，读取与网络不在同一沙箱边界内。",
}
SUBMIT_FIELDS = {"request_id", "task_id", "context_hash", "prompt"}


def resolve_root(value):
    try:
        if isinstance(value, str) and value.startswith("\\\\?\\") and re.match(r"[A-Za-z]:\\", value[4:]):
            value = value[4:]
        if not isinstance(value, str) or not Path(value).is_absolute() or value.startswith(("\\\\", "//")):
            raise ValueError("invalid_root")
        path = Path(value).resolve(strict=True)
        if not path.is_dir() or path == Path(path.anchor) or str(path).startswith(("\\\\", "//")):
            raise ValueError("invalid_root")
        info = path.stat()
        if not info.st_ino:
            raise ValueError("invalid_root")
        cwd = os.path.normcase(str(path))
        return cwd, encode({"cwd": cwd, "dev": info.st_dev, "ino": info.st_ino})
    except (OSError, ValueError, RuntimeError):
        raise WorkspaceError("context_changed") from None


def execution_policy(adapter="codex", *, permission="workspace-write", network="allowed",
                     approval="request_user"):
    if adapter == "dsh":
        effective_approval = "never" if permission == "danger-full-access" else approval
        return {
            "mode": permission,
            "approval_policy": effective_approval,
            "approvals_reviewer": "user",
            "network_access": network,
            "writable_roots": [],
            "inheritance_note": "DSH headless 使用其原生文件沙箱；Windows 上限制写入/删除，读取与网络不在同一沙箱边界内。",
        }
    return {**{key: POLICY[key] for key in ("approval_policy", "approvals_reviewer",
                                             "network_access", "writable_roots", "inheritance_note")},
            "mode": permission if permission in ("read-only", "workspace-write") else POLICY["mode"]}


def dispatch_context_digest(dispatch, task, project, agent, cwd, root_identity, runtime=None, policy=None,
                            dsh_runtime=None, sandbox_preparation=None):
    """Digest exactly the facts a dispatch execution is allowed to start from."""
    runtime_facts = None
    if runtime is not None:
        runtime_facts = {**runtime.public(), "home": str(runtime.home)}
    dsh_facts = dsh_runtime.public() if dsh_runtime is not None else None
    effective_policy = policy or execution_policy(
        agent.get("adapter", "codex"),
        permission=agent.get("permission", "workspace-write"),
        network=agent.get("network", "allowed"),
        approval=agent.get("approval", "request_user"))
    return hashlib.sha256(encode({
        "version": POLICY_VERSION,
        "dispatch_id": dispatch["id"],
        "dispatch_content_sha256": hashlib.sha256(dispatch["content_text"].encode("utf-8")).hexdigest(),
        "dispatch_state": dispatch["delivery_state"],
        "task": {key: task[key] for key in ("id", "updated_at", "project_id", "assigned_agent_id", "state")},
        "project": {"id": project["id"], "name": project["name"]},
        "agent": {"id": agent["id"], "adapter": agent["adapter"], "enabled": agent["enabled"],
                  "kind": agent.get("kind"), "command": agent.get("command"),
                  "connection": agent.get("connection"), "project_ids": sorted(agent.get("project_ids") or [])},
        "directory": {"id": dispatch["directory_id"], "path": dispatch["directory_path"]},
        "cwd": cwd,
        "root_identity": root_identity,
        "runtime": runtime_facts,
        "dsh_runtime": dsh_facts,
        "sandbox_preparation": sandbox_preparation,
        "policy": effective_policy,
    }).encode("utf-8")).hexdigest()


def prepare_dispatch_facts(facts, *, policy=None, runtime=None, dsh_runtime=None, sandbox_preparation=None):
    dispatch, task, project, directory, agent = (
        facts["dispatch"], facts["task"], facts["project"], facts["directory"], facts["agent"])
    cwd, identity = resolve_root(directory["path"])
    stored_cwd, stored_identity = resolve_root(dispatch["directory_path"])
    if cwd != stored_cwd or identity != stored_identity:
        raise ProfileError("context_changed", "directory_path", status=409)
    prompt = dispatch["content_text"]
    if not isinstance(prompt, str) or not prompt.strip():
        raise ProfileError("prompt_required", "prompt", status=409)
    if len(prompt) > 4000 or len(prompt.encode("utf-8")) > 16384:
        raise ProfileError("prompt_too_long", "prompt", status=422)
    adapter = agent.get("adapter", "codex")
    permission = agent.get("permission", "workspace-write")
    network = agent.get("network", "allowed")
    approval = agent.get("approval", "request_user")
    if adapter == "codex" and (permission not in ("read-only", "workspace-write")
                               or network != "denied" or approval != "request_user"):
        raise ProfileError("execution_unsupported", "permission", status=409)
    if adapter == "dsh" and network != "allowed":
        raise ProfileError("execution_unsupported", "network", status=409)
    effective_policy = execution_policy(
        adapter, permission=permission, network=network, approval=approval)
    context = {
        "task_id": dispatch["task_id"],
        "dispatch_id": dispatch["id"],
        "request_id": dispatch["id"],
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "project": {"id": project["id"], "name": project["name"], "cwd": cwd},
        "agent": {"id": agent["id"], "adapter": agent["adapter"]},
        "adapter": agent["adapter"],
        "dispatch_state": dispatch["delivery_state"],
        "context_hash": dispatch_context_digest(dispatch, task, project, agent, cwd, identity, runtime,
                                                effective_policy, dsh_runtime, sandbox_preparation),
        "policy": effective_policy,
        "sandbox_preparation": sandbox_preparation,
        "agent_policy": {"permission": permission, "network": network, "approval": approval},
    }
    if dsh_runtime is not None:
        context["dsh_runtime"] = dsh_runtime.public()
    return context, identity


def dispatch_execution_context(workspace, dispatch_id):
    """Pure read-only assembly for a prepared dispatch; no owner/worker is created."""
    facts = workspace.dispatch_execution_facts(dispatch_id)
    runtime = None
    dsh_runtime = None
    if getattr(workspace.store, "profile", False):
        if facts["agent"]["adapter"] == "codex":
            from control_everything.execution.runtime import build_runtime
            runtime = build_runtime(workspace.profile)
        elif facts["agent"]["adapter"] == "dsh":
            from control_everything.execution.dsh_runtime import build_dsh_runtime
            dsh_runtime = build_dsh_runtime(
                workspace.profile, permission=facts["agent"].get("permission", "workspace-write"),
                network=facts["agent"].get("network", "allowed"),
                approval=facts["agent"].get("approval", "request_user"))
    preparation = None
    if (getattr(workspace.store, "profile", False) and facts["agent"]["adapter"] == "dsh"
            and facts["agent"].get("permission", "workspace-write") == "workspace-write"):
        from control_everything.execution import sandbox_preparations
        preparation = sandbox_preparations.status(
            workspace.profile.data_dir, facts["task"]["project_id"], facts["directory"]["path"],
            directory_id=facts["directory"]["binding_id"])
    context, _ = prepare_dispatch_facts(facts, runtime=runtime, dsh_runtime=dsh_runtime,
                                        sandbox_preparation=preparation)
    return context


def _dispatch_request_hash(dispatch_id, context_hash):
    return hashlib.sha256(encode({
        "version": "dispatch-execution-v1", "dispatch_id": dispatch_id,
        "context_hash": context_hash, "confirmed": True}).encode("utf-8")).hexdigest()


def _replay_dispatch(workspace, execution_store, connection, old, request_hash, dispatch_id):
    if old["request_hash"] != request_hash:
        raise ProfileError("idempotency_conflict", "context_hash", status=409)
    dispatch = connection.execute("SELECT task_id FROM task_dispatches WHERE id=?", (dispatch_id,)).fetchone()
    if (old["request_id"] != dispatch_id or dispatch is None or old["task_id"] != dispatch["task_id"]):
        raise ProfileError("execution_link_conflict", "dispatch_id", status=409)
    binding = workspace.execution_binding(connection, dispatch_id=dispatch_id,
                                          execution_run_id=old["id"], task_id=old["task_id"])
    return {"run": old, "dispatch_id": dispatch_id,
            "agent_run_id": binding["agent_run_id"], "replayed": True}


def _bounded_replay_after_config_busy(workspace, execution_store, dispatch_id, request_hash, original):
    deadline = time.monotonic() + 2.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise original
        try:
            with execution_store.receipt_connection(deadline=deadline) as connection:
                old = execution_store.find_request_in_transaction(connection, dispatch_id)
                if old is not None:
                    return _replay_dispatch(workspace, execution_store, connection, old, request_hash, dispatch_id)
        except sqlite3.OperationalError as error:
            text = str(error).lower()
            if not any(token in text for token in ("locked", "busy", "interrupted")):
                raise
            if time.monotonic() >= deadline:
                raise original from None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise original
        time.sleep(min(0.05, remaining))


def admit_dispatch_execution(workspace, execution_store, dispatch_id, body, *, owner_id, require_owner,
                             runtime=None, dsh_runtime=None, require_sandbox_acl=False):
    """Database-only admission; the caller owns commit and must start no worker here."""
    canonical_uuid(dispatch_id)
    if type(body) is not dict or set(body) != {"context_hash", "confirmed"}:
        raise ProfileError("invalid_execution", "body")
    context_hash = body["context_hash"]
    if (type(context_hash) is not str or len(context_hash) != 64
            or any(char not in "0123456789abcdef" for char in context_hash)):
        raise ProfileError("invalid_execution", "context_hash")
    if body["confirmed"] is not True:
        raise ProfileError("confirmation_required", "confirmed", status=422)
    if (execution_store.workspace is not workspace.store
            or not getattr(workspace.store, "profile", False)
            or Path(execution_store.path).resolve() != Path(workspace.store.path).resolve()):
        raise ProfileError("execution_link_conflict", "store", status=409)
    request_hash = _dispatch_request_hash(dispatch_id, context_hash)
    with execution_store.connection() as connection:
        old = execution_store.find_request_in_transaction(connection, dispatch_id)
        if old is not None:
            return _replay_dispatch(workspace, execution_store, connection, old, request_hash, dispatch_id)
    try:
        with workspace.profile.agent_settings_guard() as agent_settings:
            with execution_store.connection(immediate=True) as connection:
                old = execution_store.find_request_in_transaction(connection, dispatch_id)
                if old is not None:
                    return _replay_dispatch(workspace, execution_store, connection, old, request_hash, dispatch_id)
                require_owner()
                facts = workspace.dispatch_execution_facts(
                    dispatch_id, connection=connection, agent_settings=agent_settings)
                if facts["dispatch"]["delivery_mode"] != "auto":
                    raise ProfileError("execution_mode_conflict", "delivery_mode", status=409)
                effective_runtime, effective_dsh = runtime, dsh_runtime
                if facts["agent"]["adapter"] == "dsh":
                    effective_runtime = None
                    if effective_dsh is None and getattr(workspace.store, "profile", False):
                        from control_everything.execution.dsh_runtime import build_dsh_runtime
                        effective_dsh = build_dsh_runtime(
                            workspace.profile,
                            permission=facts["agent"].get("permission", "workspace-write"),
                            network=facts["agent"].get("network", "allowed"),
                            approval=facts["agent"].get("approval", "request_user"))
                elif (effective_runtime is None and getattr(workspace.store, "profile", False)):
                    from control_everything.execution.runtime import build_runtime
                    effective_runtime = build_runtime(workspace.profile)
                preparation = None
                if facts["agent"]["adapter"] == "dsh" and facts["agent"].get("permission", "workspace-write") == "workspace-write":
                    from control_everything.execution import sandbox_preparations
                    preparation = sandbox_preparations.status(
                        workspace.profile.data_dir, facts["task"]["project_id"], facts["directory"]["path"],
                        directory_id=facts["directory"]["binding_id"])
                    if require_sandbox_acl and preparation.get("status") != "ready":
                        raise ProfileError("windows_sandbox_acl_required", "project", status=409)
                context, identity = prepare_dispatch_facts(facts, runtime=effective_runtime,
                                                           dsh_runtime=effective_dsh,
                                                           sandbox_preparation=preparation)
                if context["context_hash"] != context_hash:
                    raise ProfileError("context_changed", "context_hash", status=409)
                run = execution_store.insert_dispatch_run(
                    connection, request_id=dispatch_id, request_hash=request_hash, owner_id=owner_id,
                    context={"task_id": context["task_id"], "task_title": facts["task"]["title"],
                             "project_id": context["project"]["id"], "project_name": context["project"]["name"],
                             "cwd": context["project"]["cwd"], "root_identity": identity,
                             "mode": context["policy"]["mode"], "prompt": context["prompt"],
                             "adapter": context["adapter"],
                             "dsh_runtime": context.get("dsh_runtime"),
                             "policy": context["policy"],
                             "sandbox_preparation": context.get("sandbox_preparation"),
                             "agent_policy": context.get("agent_policy")})
                claimed = workspace.claim_dispatch_in_transaction(
                    connection, dispatch_id=dispatch_id, task_id=context["task_id"],
                    agent_id=context["agent"]["id"], execution_run_id=run["id"],
                    expected_revision=facts["task"]["revision"])
                workspace.execution_binding(connection, dispatch_id=dispatch_id,
                                            execution_run_id=run["id"], task_id=context["task_id"])
                return {"run": run, "dispatch_id": dispatch_id,
                        "agent_run_id": claimed["agent_run_id"], "replayed": False}
    except ProfileError as error:
        if error.reason != "config_busy":
            raise
        return _bounded_replay_after_config_busy(
            workspace, execution_store, dispatch_id, request_hash, error)


class ExecutionService:
    def __init__(self, workspace, *, store=None, worker_factory=None, owner_factory=None,
                 resolver=None, job_query=None, policy=None, platform=None, runtime=None,
                 dsh_worker_factory=None, dsh_resolver=None, codex_required=True, dsh_runtime=None,
                 require_sandbox_acl=False):
        self.workspace = workspace
        self._custom_worker = worker_factory
        self._dsh_resolver = dsh_resolver
        self._codex_required = codex_required
        self.dsh_runtime = dsh_runtime
        self.require_sandbox_acl = require_sandbox_acl
        self.runtime = runtime
        self.store = store or ExecutionStore(workspace.store)
        self.policy = {**POLICY, **(policy or {})}
        self.owner_id = str(uuid4())
        self.owner = None
        self._workers = {}
        self._inflight = set()
        self._closed = False
        self._reconcile_stop = threading.Event()
        self._reconcile_thread = None
        self._project_errors = {}
        self._lock = threading.RLock()
        self._reason = None
        self._resolver = resolver or native_codex
        self._worker_factory = worker_factory
        try:
            if (platform or sys.platform) != "win32":
                self._reason = "platform_unsupported"
                return
            if owner_factory is None or job_query is None:
                from control_everything.execution.owned_process import ExecutionOwner, query_old_job
                owner_factory = owner_factory or ExecutionOwner
                job_query = job_query or query_old_job
            self.owner = owner_factory(self.store.path.resolve())
            if not self.owner.acquire():
                self._reason = "other_instance"
                return
            self.store.recover(self.owner_id, job_query)
            if self._codex_required:
                self._resolver()
            if self._worker_factory is None:
                from control_everything.execution.codex import CodexRun
                self._worker_factory = CodexRun
            self._dsh_worker_factory = dsh_worker_factory
            if self._dsh_worker_factory is None:
                from control_everything.execution.dsh import DshRun
                self._dsh_worker_factory = DshRun
            self._reconcile_thread = threading.Thread(target=self._reconcile_loop,
                                                      name="workspace-execution-reconcile", daemon=True)
            self._reconcile_thread.start()
            self._reconcile_open()
        except Exception as error:
            try:
                if self.owner is not None:
                    self.owner.close()
            finally:
                self._reason = "storage_unavailable" if isinstance(error, WorkspaceError) else "execution_unavailable"

    @property
    def is_owner(self):
        return bool(self.owner is not None and self.owner.is_owner and not self._closed)

    def capability(self):
        value = {"implemented": True, "available": self.is_owner and self._reason is None,
                 "reason": "execution_unavailable" if self._closed else self._reason,
                 "is_owner": self.is_owner, "max_active": 2}
        if self.runtime is not None:
            value["runtime"] = self.runtime.public()
            value["windows_sandbox"] = "unknown"
        return value

    def _require_owner(self):
        if self._reason == "other_instance":
            raise WorkspaceError("other_instance")
        if not self.capability()["available"]:
            raise WorkspaceError("execution_unavailable")

    def _context(self, task_id, connection=None):
        if connection is None:
            task = self.workspace.store.get_task(task_id)
        else:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise WorkspaceError("task_not_found")
            task = dict(row)
        if task["provider_id"] != "codex":
            raise WorkspaceError("context_changed")
        registry = self.workspace.registered_projects()
        project = next((item for item in registry if item["id"] == task["project_id"]), None)
        if project is None:
            raise WorkspaceError("context_changed")
        cwd, identity = resolve_root(project["root"])
        digest = hashlib.sha256(encode({"task": {name: task[name] for name in
                                  ("id", "updated_at", "project_id", "provider_id")},
                                  "root_identity": identity, "policy_version": POLICY_VERSION,
                                  "policy": self.policy}).encode("utf-8")).hexdigest()
        return {"task": {"id": task["id"], "title": task["title"]},
                "project": {"id": project["id"], "name": project["name"], "cwd": cwd},
                "context_hash": digest, "policy": {key: self.policy[key] for key in POLICY}}, identity

    def context(self, task_id):
        canonical_uuid(task_id)
        return self._context(task_id)[0]

    def validate_root(self, run):
        try:
            if getattr(self.workspace.store, "profile", False):
                with self.workspace.store.connection() as connection:
                    connection.execute("BEGIN")
                    dispatch = connection.execute(
                        "SELECT id,run_id,task_id,external_ref FROM task_dispatches WHERE id=?",
                        (run["request_id"],)).fetchone()
                if dispatch is None or dispatch["external_ref"] != run["id"]:
                    return False
                with self.workspace.profile.agent_settings_guard() as agent_settings:
                    with self.workspace.store.connection() as connection:
                        connection.execute("BEGIN")
                        facts = self.workspace.execution_run_facts(
                            execution_run_id=run["id"], agent_run_id=dispatch["run_id"],
                            dispatch_id=dispatch["id"], task_id=dispatch["task_id"],
                            connection=connection, agent_settings=agent_settings)
                cwd, identity = resolve_root(facts["directory"]["path"])
                stored_payload = json.loads(facts["execution"]["effective_policy_json"] or "{}")
                if (stored_payload.get("confirmation_version") == 1
                        and (stored_payload.get("policy") is None
                             or stored_payload.get("agent_policy") is None)):
                    return False
                current_policy = execution_policy(
                    facts["agent"]["adapter"],
                    permission=facts["agent"].get("permission", "workspace-write"),
                    network=facts["agent"].get("network", "denied"),
                    approval=facts["agent"].get("approval", "request_user"))
                if stored_payload.get("policy") is not None and stored_payload["policy"] != current_policy:
                    return False
                current_agent_policy = {
                    "permission": facts["agent"].get("permission", "workspace-write"),
                    "network": facts["agent"].get("network", "denied"),
                    "approval": facts["agent"].get("approval", "request_user")}
                if (stored_payload.get("agent_policy") is not None
                        and stored_payload["agent_policy"] != current_agent_policy):
                    return False
                if (self.require_sandbox_acl and facts['agent']['adapter'] == 'dsh'
                        and current_agent_policy['permission'] == 'workspace-write'):
                    stored_preparation = stored_payload.get('sandbox_preparation')
                    if not isinstance(stored_preparation, dict) or not stored_preparation.get('confirmation_hash'):
                        return False
                    from control_everything.execution import sandbox_preparations
                    current_preparation = sandbox_preparations.status(
                        self.workspace.profile.data_dir, facts["task"]["project_id"],
                        facts["directory"]["path"], directory_id=facts["directory"]["binding_id"])
                    if (current_preparation.get("status") != "ready"
                            or current_preparation.get("confirmation_hash")
                            != stored_preparation.get("confirmation_hash")):
                        return False
                if facts["agent"]["adapter"] == "dsh":
                    from control_everything.execution.dsh_runtime import build_dsh_runtime
                    stored = json.loads(facts["execution"]["effective_policy_json"] or "{}").get("dsh_runtime")
                    current = build_dsh_runtime(
                        self.workspace.profile,
                        permission=facts["agent"].get("permission", "workspace-write"),
                        network=facts["agent"].get("network", "allowed"),
                        approval=facts["agent"].get("approval", "request_user"))
                    if stored != current.public():
                        return False
                return cwd == run["cwd"] and identity == run["root_identity"]
            project = next((item for item in self.workspace.registered_projects()
                            if item["id"] == run["project_id"]), None)
            return bool(project and resolve_root(project["root"]) == (run["cwd"], run["root_identity"]))
        except (WorkspaceError, OSError, ValueError, TypeError):
            return False

    def snapshot(self, run_id=None, after=0, *, request_id=None):
        if request_id is not None:
            canonical_uuid(request_id)
            row = self.store.find_request(request_id)
            if row is None:
                raise WorkspaceError("request_not_found")
            return {"run": public_run(row)}
        if run_id is not None:
            canonical_uuid(run_id)
            if type(after) is not int or not 0 <= after <= 9223372036854775807:
                raise ValueError("invalid_request")
            return self.store.snapshot(run_id, after, can_act=self.is_owner)
        return {"capability": self.capability(), **self.store.listing(),
                "limits": {"max_active": 2, "overlapping_roots": "exclusive", "queue": False}}

    def _project(self, run_id):
        if not getattr(self.workspace.store, "profile", False):
            return
        token = object()
        with self._lock:
            self._inflight.add(token)
        try:
            with self.workspace.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                facts = self.store.projection_facts_in_transaction(connection, run_id)
                run = facts["run"]
                dispatch = connection.execute(
                    "SELECT * FROM task_dispatches WHERE id=? AND external_ref=?",
                    (run["request_id"], run_id)).fetchone()
                if dispatch is None or dispatch["run_id"] is None:
                    self._project_errors[run_id] = "execution_link_conflict"
                    return
                self.workspace.project_execution_state(
                    connection, execution_run_id=run_id, task_id=dispatch["task_id"],
                    agent_run_id=dispatch["run_id"], dispatch_id=dispatch["id"],
                    state=run["state"], cleanup_state=run["cleanup_state"],
                    turn_send_started=bool(run["turn_send_started"]), turn_id=run["turn_id"],
                    reason=run["reason"], events=facts["events"], approvals=facts["approvals"],
                    observed_at=run["updated_at"])
            self._project_errors.pop(run_id, None)
        except Exception as error:
            self._project_errors[run_id] = str(error)[:200]
        finally:
            with self._lock:
                self._inflight.discard(token)
                if self._closed and not self._workers and not self._inflight and self.owner is not None:
                    self.owner.close()

    def _reconcile_open(self):
        if not getattr(self.workspace.store, "profile", False):
            return
        token = object()
        with self._lock:
            self._inflight.add(token)
        try:
            offset = 0
            while not self._closed:
                try:
                    run_ids = self.store.projection_run_ids(offset, 200)
                except WorkspaceError:
                    return
                if not run_ids:
                    return
                for run_id in run_ids:
                    if self._closed:
                        return
                    try:
                        self._project(run_id)
                    except Exception:
                        continue
                offset += len(run_ids)
        finally:
            with self._lock:
                self._inflight.discard(token)
                if self._closed and not self._workers and not self._inflight and self.owner is not None:
                    self.owner.close()

    def _reconcile_loop(self):
        while not self._reconcile_stop.wait(5):
            self._reconcile_open()

    def submit(self, body):
        if getattr(self.workspace.store, "profile", False):
            raise WorkspaceError("execution_managed_endpoint")
        if not isinstance(body, dict) or set(body) != SUBMIT_FIELDS:
            raise ValueError("invalid_request")
        canonical_uuid(body["request_id"])
        canonical_uuid(body["task_id"])
        if (not isinstance(body["context_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", body["context_hash"])
                or not isinstance(body["prompt"], str) or not body["prompt"].strip()
                or len(body["prompt"]) > 4000):
            raise ValueError("invalid_request")
        try:
            if len(body["prompt"].encode("utf-8")) > 16384:
                raise ValueError("invalid_request")
            request_hash = hashlib.sha256(encode(body).encode("utf-8")).hexdigest()
        except UnicodeError:
            raise ValueError("invalid_request") from None

        def prepare(connection):
            self._require_owner()
            context, identity = self._context(body["task_id"], connection)
            if context["context_hash"] != body["context_hash"]:
                raise WorkspaceError("context_changed")
            return {"task_title": context["task"]["title"], "project_id": context["project"]["id"],
                    "project_name": context["project"]["name"], "cwd": context["project"]["cwd"],
                    "root_identity": identity, "mode": self.policy["mode"]}

        with self._lock:
            run, replayed = self.store.admit(body, request_hash, self.owner_id, prepare)
            if not replayed:
                self._start_worker(run)
        return {"run": public_run(run), "replayed": replayed}

    def submit_dispatch(self, dispatch_id, body):
        canonical_uuid(dispatch_id)

        def require_owner():
            if self._closed:
                raise WorkspaceError("execution_closed")
            if self.policy != {**POLICY}:
                raise ProfileError("execution_policy_conflict", "policy", status=409)
            self._require_owner()

        token = object()
        with self._lock:
            self._inflight.add(token)
        try:
            admitted = admit_dispatch_execution(
                self.workspace, self.store, dispatch_id, body, owner_id=self.owner_id,
                require_owner=require_owner, runtime=self.runtime, dsh_runtime=self.dsh_runtime,
                require_sandbox_acl=self.require_sandbox_acl)
            run = admitted["run"]
            if not admitted["replayed"]:
                valid_root = self.validate_root(run)
                with self._lock:
                    if self._closed:
                        self.store.update_run(run["id"], state="cancelled_before_send",
                                              reason="service_closed", cleanup_state="verified")
                        self._project(run["id"])
                    elif not valid_root:
                        self.store.update_run(run["id"], state="failed", reason="context_changed",
                                              cleanup_state="verified")
                        self._project(run["id"])
                    else:
                        self._start_worker(run, validate=False)
            row = self.store.get_run(run["id"])
            public = public_run(row)
            public["dispatch_id"] = admitted["dispatch_id"]
            public["execution_run_id"] = row["id"]
            return {"run": public, "execution_run_id": row["id"],
                    "dispatch_id": admitted["dispatch_id"], "agent_run_id": admitted["agent_run_id"],
                    "replayed": admitted["replayed"]}
        finally:
            with self._lock:
                self._inflight.discard(token)
                if self._closed and not self._workers and not self._inflight and self.owner is not None:
                    self.owner.close()

    def _run_adapter(self, run):
        try:
            value = json.loads(run.get("effective_policy_json") or "{}")
        except (TypeError, ValueError):
            value = {}
        return value.get("adapter") if isinstance(value, dict) else "codex"

    def _dsh_runtime_for_run(self, run):
        try:
            stored = json.loads(run.get("effective_policy_json") or "{}").get("dsh_runtime")
        except (TypeError, ValueError):
            raise WorkspaceError("context_changed") from None
        from control_everything.execution.dsh_runtime import build_dsh_runtime
        current = build_dsh_runtime(
            self.workspace.profile, create=True,
            permission=stored.get("permission", "workspace-write") if isinstance(stored, dict) else "workspace-write",
            network=stored.get("network", "allowed") if isinstance(stored, dict) else "allowed",
            approval=stored.get("approval", "request_user") if isinstance(stored, dict) else "request_user")
        if stored != current.public():
            raise WorkspaceError("context_changed")
        return current

    def _start_worker(self, run, *, validate=True):
        worker = None
        try:
            if validate and not self.validate_root(run):
                self.store.update_run(run["id"], state="failed", reason="context_changed",
                                      cleanup_state="verified")
                self._project(run["id"])
                return
            adapter = self._run_adapter(run)
            run_policy = json.loads(run.get("effective_policy_json") or "{}").get("policy") or self.policy
            if adapter == "dsh":
                factory = self._custom_worker or self._dsh_worker_factory
                dsh_runtime = self.dsh_runtime
                if dsh_runtime is None and getattr(self.workspace.store, "profile", False):
                    dsh_runtime = self._dsh_runtime_for_run(run)
                worker = factory(store=self.store, run=run, owner_id=self.owner_id,
                                 validate_root=self.validate_root, policy=run_policy,
                                 **({"resolver": self._dsh_resolver} if self._dsh_resolver is not None else {}),
                                 **({"runtime": dsh_runtime} if dsh_runtime is not None else {}))
            else:
                factory = self._custom_worker or self._worker_factory
                codex_runtime = self.runtime
                if (codex_runtime is None and self._custom_worker is None
                        and getattr(self.workspace.store, "profile", False)):
                    from control_everything.execution.runtime import build_runtime
                    codex_runtime = build_runtime(self.workspace.profile, create=True)
                worker = factory(store=self.store, run=run, owner_id=self.owner_id,
                                 validate_root=self.validate_root, policy=run_policy, resolver=self._resolver,
                                 **({"runtime": codex_runtime} if codex_runtime is not None else {}))
            thread = threading.Thread(target=self._run_worker, args=(run["id"], worker),
                                      name="workspace-run-" + run["id"], daemon=True)
            self._workers[run["id"]] = (worker, thread)
            thread.start()
        except Exception:
            self._workers.pop(run["id"], None)
            try:
                if worker is not None:
                    worker.close()
            finally:
                has_process = worker is not None and getattr(worker, "process", None) is not None
                self.store.update_run(run["id"], state="failed", reason="worker_start_failed",
                                      cleanup_state="unverified" if has_process else "verified")
                self._project(run["id"])

    def _run_worker(self, run_id, worker):
        lease = ExitStack()
        try:
            row = self.store.get_run(run_id)
            lease.enter_context(directory_lock(row['cwd']))
            if not self.validate_root(row):
                raise WorkspaceError('context_changed')
            worker.run()
        except Exception:
            try:
                row = self.store.get_run(run_id)
                if row["state"] in ACTIVE_STATES:
                    cleanup = ("verified" if getattr(worker, "process", None) is None
                               and not row["turn_send_started"] else row["cleanup_state"])
                    self.store.update_run(run_id, state="unknown" if row["turn_send_started"] else "failed",
                                          reason="execution_error", cleanup_state=cleanup)
            except WorkspaceError:
                pass
        finally:
            try:
                worker.close()
            finally:
                try:
                    row = self.store.get_run(run_id)
                except WorkspaceError:
                    row = {'cleanup_state': 'unverified'}
                if getattr(worker, 'process', None) is not None and row['cleanup_state'] != 'verified':
                    _UNVERIFIED_LEASES[run_id] = lease
                else:
                    lease.close()
                self._project(run_id)
                with self._lock:
                    self._workers.pop(run_id, None)
                    if self._closed and not self._workers and not self._inflight and self.owner is not None:
                        self.owner.close()

    def act(self, run_id, body, *, approval_id=None):
        canonical_uuid(run_id)
        if approval_id is None:
            if body != {}:
                raise ValueError("invalid_request")
        else:
            canonical_uuid(approval_id)
            if not isinstance(body, dict) or set(body) != {"decision"} or body["decision"] not in ("accept", "decline", "cancel"):
                raise ValueError("invalid_request")
        with self._lock:
            self._require_owner()
            run = self.store.get_run(run_id)
            if run["owner_id"] != self.owner_id:
                raise WorkspaceError("other_instance")
            entry = self._workers.get(run_id)
            if approval_id is not None:
                if entry is None:
                    row = self.store.get_approval(run_id, approval_id)
                    if row["decision"] == body["decision"]:
                        return {"approval": public_approval(row, can_act=False), "replayed": True}
                    raise WorkspaceError("decision_conflict" if row["decision"] else "approval_expired")
                row, replayed = entry[0].answer(approval_id, body["decision"])
                return {"approval": public_approval(row, can_act=False), "replayed": replayed}
            if entry is None:
                row, replayed = self.store.request_cancel(run_id, self.owner_id)
            else:
                with entry[0].lock:
                    row, replayed = self.store.request_cancel(run_id, self.owner_id)
                    if not replayed:
                        entry[0].interrupt()
            return {"run": public_run(row), "replayed": replayed}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._reconcile_stop.set()
            entries = list(self._workers.items())
            workers = [entry for _run_id, entry in entries]
        try:
            for run_id, (worker, _thread) in entries:
                try:
                    with worker.lock:
                        try:
                            self.store.request_cancel(run_id, self.owner_id)
                        finally:
                            worker.interrupt()
                except Exception:
                    pass
            deadline = time.monotonic() + 6
            for _worker, thread in workers:
                thread.join(max(0, deadline - time.monotonic()))
            for worker, thread in workers:
                try:
                    worker.close()
                except Exception:
                    pass
                thread.join(timeout=2)
            if self._reconcile_thread is not None:
                self._reconcile_thread.join(timeout=1)
        finally:
            # A late worker keeps the owner until its final storage write is done.
            with self._lock:
                if not self._workers and not self._inflight and self.owner is not None:
                    self.owner.close()
