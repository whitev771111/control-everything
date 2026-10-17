"""Admission and HTTP-facing operations for owned Codex execution."""

import hashlib
import os
import re
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from session_panel.execution.store import ACTIVE_STATES, ExecutionStore, encode, public_approval, public_run
from session_panel.sessions.source import canonical_uuid
from session_panel.workspace.sources import native_codex
from session_panel.workspace.store import WorkspaceError

POLICY = {"mode": "workspace-write", "approval_policy": "on-request", "approvals_reviewer": "user",
          "network_access": False, "writable_roots": [],
          "inheritance_note": "本次使用本机 Codex 账号与设置；内建命令采用所示沙箱。独立 MCP 等外部服务可能继承本机设置，其副作用不由项目目录限制覆盖。"}
POLICY_VERSION = "workspace-codex-v1"
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


class ExecutionService:
    def __init__(self, workspace, *, store=None, worker_factory=None, owner_factory=None,
                 resolver=None, job_query=None, policy=None, platform=None):
        self.workspace = workspace
        self.store = store or ExecutionStore(workspace.store)
        self.policy = {**POLICY, **(policy or {})}
        self.owner_id = str(uuid4())
        self.owner = None
        self._workers = {}
        self._lock = threading.RLock()
        self._closed = False
        self._reason = None
        self._resolver = resolver or native_codex
        self._worker_factory = worker_factory
        try:
            if (platform or sys.platform) != "win32":
                self._reason = "platform_unsupported"
                return
            if owner_factory is None or job_query is None:
                from session_panel.execution.owned_process import ExecutionOwner, query_old_job
                owner_factory = owner_factory or ExecutionOwner
                job_query = job_query or query_old_job
            self.owner = owner_factory(self.store.path.resolve())
            if not self.owner.acquire():
                self._reason = "other_instance"
                return
            self.store.recover(self.owner_id, job_query)
            self._resolver()
            if self._worker_factory is None:
                from session_panel.execution.codex import CodexRun
                self._worker_factory = CodexRun
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
        return {"implemented": True, "available": self.is_owner and self._reason is None,
                "reason": "execution_unavailable" if self._closed else self._reason,
                "is_owner": self.is_owner, "max_active": 2}

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
            project = next((item for item in self.workspace.registered_projects() if item["id"] == run["project_id"]), None)
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

    def submit(self, body):
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

    def _start_worker(self, run):
        worker = None
        try:
            worker = self._worker_factory(store=self.store, run=run, owner_id=self.owner_id,
                                          validate_root=self.validate_root, policy=self.policy, resolver=self._resolver)
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
                self.store.update_run(run["id"], state="failed", reason="worker_start_failed", cleanup_state="verified")

    def _run_worker(self, run_id, worker):
        try:
            worker.run()
        except Exception:
            try:
                row = self.store.get_run(run_id)
                if row["state"] in ACTIVE_STATES:
                    self.store.update_run(run_id, state="unknown" if row["turn_send_started"] else "failed",
                                          reason="execution_error")
            except WorkspaceError:
                pass
        finally:
            try:
                worker.close()
            finally:
                with self._lock:
                    self._workers.pop(run_id, None)
                    if self._closed and not self._workers and self.owner is not None:
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
        finally:
            # A late worker keeps the owner until its final storage write is done.
            with self._lock:
                if not self._workers and self.owner is not None:
                    self.owner.close()
