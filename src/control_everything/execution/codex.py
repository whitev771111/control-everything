"""One owned app-server, one new thread, and one non-replayed turn per run."""

from collections import deque
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from control_everything.execution.owned_process import OwnedProcess, ProcessError
from control_everything.execution.runtime_readiness import classify_native_failure
from control_everything.workspace.sources import native_codex
from control_everything.workspace.store import WorkspaceError

FEATURES = ("hooks", "plugins", "apps", "multi_agent", "goals")
WORKSPACE_CONFIG = {"writable_roots": [], "network_access": False,
                    "exclude_tmpdir_env_var": True, "exclude_slash_tmp": True}
COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"
TERMINAL = {"completed", "interrupted", "failed", "unknown", "cancelled_before_send"}
TOKEN_FIELDS = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens",
                "totalTokens", "cacheWriteInputTokens")


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def native_key(identifier):
    if type(identifier) not in (int, str):
        raise ProcessError("protocol_error")
    return encoded(identifier)


def clipped(value, limit=4096):
    if not isinstance(value, str):
        return "", False
    raw = value.encode("utf-8")
    return raw[:limit].decode("utf-8", errors="ignore"), len(raw) > limit


def launch_arguments(disabled_mcp_ids=()):
    args = ["app-server", "--listen", "stdio://"]
    for feature in FEATURES:
        args.extend(["--disable", feature])
    for override in ('notify=[]', 'web_search="disabled"', 'sandbox_workspace_write.writable_roots=[]',
                     'sandbox_workspace_write.network_access=false',
                     'sandbox_workspace_write.exclude_tmpdir_env_var=true',
                     'sandbox_workspace_write.exclude_slash_tmp=true'):
        args.extend(["-c", override])
    for identifier in disabled_mcp_ids:
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
            raise ProcessError("invalid_test_policy")
        args.extend(["-c", f"mcp_servers.{identifier}.enabled=false"])
    return args


def check_config(response, disabled_mcp_ids=(), *, isolated_runtime=False, expected_windows_mode=None):
    config = response.get("config")
    if not isinstance(config, dict):
        raise ProcessError("effective_policy_mismatch")
    features, sandbox = config.get("features"), config.get("sandbox_workspace_write")
    checks = {"features_disabled": isinstance(features, dict) and all(features.get(name) is False for name in FEATURES),
              "notify_disabled": config.get("notify") == [],
              "web_search_disabled": config.get("web_search") == "disabled",
              "sandbox_overrides_match": isinstance(sandbox, dict) and all(
                  sandbox.get(key) == value and type(sandbox.get(key)) is type(value)
                  for key, value in WORKSPACE_CONFIG.items())}
    if isolated_runtime:
        checks["credential_store_file"] = config.get("cli_auth_credentials_store") == "file"
        checks["mcp_empty"] = config.get("mcp_servers") in (None, {})
    if expected_windows_mode is not None:
        windows = config.get("windows")
        checks["windows_sandbox_mode_match"] = (
            isinstance(windows, dict) and windows.get("sandbox") == expected_windows_mode)
    if disabled_mcp_ids:
        servers = config.get("mcp_servers")
        checks["test_mcp_disabled"] = isinstance(servers, dict) and set(servers) == set(disabled_mcp_ids) and all(
            isinstance(row, dict) and row.get("enabled") is False for row in servers.values())
    if not all(checks.values()):
        raise ProcessError("effective_policy_mismatch")
    return checks


def readonly_downgrade(response, cwd):
    actual = response.get("sandbox")
    if not isinstance(actual, dict) or actual.get("type") != "readOnly" \
            or actual.get("networkAccess") is not False:
        return False
    try:
        same_root = Path(response["cwd"]).resolve(strict=True) == Path(cwd).resolve(strict=True)
    except (KeyError, TypeError, ValueError, OSError):
        return False
    return bool(same_root and response.get("approvalPolicy") == "on-request"
                and response.get("approvalsReviewer") == "user"
                and response.get("activePermissionProfile") is None)


def sandbox_policy(mode):
    if mode == "read-only":
        return {"type": "readOnly", "networkAccess": False}
    if mode != "workspace-write":
        raise ProcessError("effective_policy_mismatch")
    return {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
            "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}


def check_thread(response, cwd, mode):
    expected = sandbox_policy(mode)
    actual = response.get("sandbox")
    try:
        same_root = Path(response["cwd"]).resolve(strict=True) == Path(cwd).resolve(strict=True)
        roots = actual.get("writableRoots") if isinstance(actual, dict) else None
        roots_match = isinstance(roots, list) and all(
            isinstance(root, str) and Path(root).resolve(strict=True) == Path(cwd).resolve(strict=True) for root in roots)
    except (KeyError, TypeError, ValueError, OSError):
        raise ProcessError("effective_policy_mismatch") from None
    matches = isinstance(actual, dict) and all(actual.get(key) == value and type(actual.get(key)) is type(value)
              for key, value in expected.items() if key != "writableRoots")
    if (not same_root or response.get("approvalPolicy") != "on-request" or
            response.get("approvalsReviewer") != "user" or not matches or
            mode == "workspace-write" and not roots_match or response.get("activePermissionProfile") is not None):
        raise ProcessError("effective_policy_mismatch")
    if not isinstance(response.get("model"), str) or not isinstance(response.get("modelProvider"), str):
        raise ProcessError("protocol_error")
    return {"mode": mode, "approval_policy": "on-request", "approvals_reviewer": "user",
            "network_access": False, "writable_roots": [], "thread_policy_checked": True,
            "model_provider": response["modelProvider"]}


class CodexRun:
    def __init__(self, store, run, owner_id, validate_root, policy=None, process_factory=None, resolver=None,
                 runtime=None):
        self.store, self.record, self.owner_id = store, dict(run), owner_id
        self.runtime = runtime
        self.id, self.cwd = run["id"], run["cwd"]
        self.validate_root = validate_root
        self.mode = (policy or {}).get("mode", "workspace-write")
        self.disabled_mcp_ids = tuple((policy or {}).get("_disabled_mcp_ids", ()))
        if self.disabled_mcp_ids and self.mode != "read-only":
            raise ProcessError("invalid_test_policy")
        self.process_factory, self.resolver = process_factory or OwnedProcess, resolver or native_codex
        self.process = None
        self.lock = threading.RLock()
        self.finished = threading.Event()
        self.cancelled = bool(run.get("cancel_requested"))
        self.cancel_deadline = None
        self.interrupt_sent = False
        self.turn_sent = False
        self.thread_id = self.turn_id = None
        self.terminal = None
        self.actions, self.queued = deque(), set()
        self.pending, self.responses, self.response_hashes = {}, {}, {}
        self.requests, self.approval_ids = {}, {}
        self.early, self.items = [], {}
        self.next_rpc = 0
        self.last_usage = None

    def event(self, kind, **payload):
        self.store.append_event(self.id, kind, payload)

    def _update(self, **fields):
        self.store.update_run(self.id, **fields)
        self.record.update(fields)

    def _require_root(self):
        if not self.validate_root(self.record):
            raise ProcessError("context_changed")

    def run(self):
        try:
            if self.cancelled:
                self._finish("cancelled_before_send", "cancelled_before_send")
                return
            self._require_root()
            executable = str(Path(self.resolver()).resolve(strict=True))
            job_name = f"Local\\mianban_exec.{self.owner_id}.{self.id}.{uuid4()}"
            self._update(job_name=job_name)
            arguments = launch_arguments(self.disabled_mcp_ids)
            if self.runtime is not None:
                arguments = [*arguments, "-c", 'cli_auth_credentials_store="file"',
                             "-c", f'windows.sandbox="{self.runtime.windows_sandbox_mode}"']
            if self.runtime is None:
                self.process = self.process_factory(executable, arguments, self.cwd, job_name)
            else:
                self.process = self.process_factory(executable, arguments, self.cwd, job_name,
                                                    env=dict(self.runtime.child_env))
            self.process.start()
            init = self._rpc("initialize", {"clientInfo": {"name": "mianban_execution", "version": "1.0"}})
            version = re.search(r"\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.]+)?", str(init.get("userAgent", "")))
            self.event("phase", phase="initialized", executable=executable,
                       server_version=version.group(0) if version else None)
            self.process.write({"method": "initialized"})
            checks = check_config(self._rpc("config/read", {"cwd": self.cwd, "includeLayers": False}),
                                  self.disabled_mcp_ids, isolated_runtime=self.runtime is not None,
                                  expected_windows_mode=(self.runtime.windows_sandbox_mode
                                                         if self.runtime is not None else None))
            if self.runtime is not None:
                self._persist_runtime(checks)
                self._authenticate()
            if self.cancelled:
                self._finish("cancelled_before_send", "cancelled_before_send")
                return
            self._require_root()
            params = {"cwd": self.cwd, "sandbox": self.mode, "approvalPolicy": "on-request",
                      "approvalsReviewer": "user", "ephemeral": True}
            if self.mode == "workspace-write":
                params["config"] = {"sandbox_workspace_write": WORKSPACE_CONFIG}
            created = self._rpc("thread/start", params)
            try:
                effective = check_thread(created, self.cwd, self.mode)
            except ProcessError as error:
                if (self.runtime is not None and str(error) == "effective_policy_mismatch"
                        and readonly_downgrade(created, self.cwd)):
                    raise ProcessError("windows_sandbox_setup_required") from None
                raise
            effective["config_checks"] = checks
            if self.runtime is not None:
                effective["runtime"] = self.runtime.public()
            try:
                prior = json.loads(self.record.get("effective_policy_json") or "{}")
            except (TypeError, ValueError):
                prior = {}
            if isinstance(prior, dict):
                for key in ("policy", "agent_policy", "adapter", "dsh_runtime", "confirmation_version"):
                    if key in prior:
                        effective[key] = prior[key]
            self.thread_id = self._identifier(created.get("thread", {}).get("id"))
            self._update(thread_id=self.thread_id, model=created["model"], effective_policy_json=encoded(effective))
            self._flush_early()
            self._require_root()
            with self.lock:
                if self.cancelled:
                    self._finish("cancelled_before_send", "cancelled_before_send")
                    return
                self._update(turn_send_started=True)
                self.turn_sent = True
                turn_request = self._send_rpc("turn/start", {"threadId": self.thread_id,
                    "input": [{"type": "text", "text": self.record["prompt"]}], "cwd": self.cwd,
                    "approvalPolicy": "on-request", "approvalsReviewer": "user", "sandboxPolicy": sandbox_policy(self.mode)})
            started = self._wait_rpc(turn_request)
            self._bind_turn(started.get("turn", {}).get("id"))
            if not self.terminal and self.record.get("state") != "waiting_approval":
                self._update(state="cancel_requested" if self.cancelled else "running")
            while not self.terminal:
                self._tick()
                message = self.process.read(timeout=.1)
                if message is not None:
                    self.handle_message(message)
            # Consume already-received facts without waiting for another turn.
            for _ in range(128):
                try:
                    message = self.process.read(timeout=0)
                except ProcessError as error:
                    if str(error) == "process_eof":
                        break
                    raise
                if message is None:
                    break
                self.handle_message(message)
        except Exception as error:
            reason = str(error) if isinstance(error, ProcessError) else (
                "storage_unavailable" if isinstance(error, WorkspaceError) else "execution_failed")
            native = classify_native_failure(getattr(error, "native_hint", None) or reason)
            if native is not None:
                try:
                    self.event("phase", phase="native_failure", native_phase=native["phase"],
                               code=native["code"], status=native["status"], summary=native["summary"])
                except WorkspaceError:
                    pass
                reason = native["status"]
            try:
                self.event("error", reason=reason)
                if not self.terminal:
                    state = "unknown" if self.turn_sent else "cancelled_before_send" if self.cancelled else "failed"
                    self._finish(state, "cancelled_before_send" if state == "cancelled_before_send" else reason)
            except WorkspaceError:
                pass
        finally:
            self.finalize()

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise ProcessError("protocol_error")
        return value

    def _send_rpc(self, method, params):
        self.next_rpc += 1
        identifier = self.next_rpc
        self.pending[native_key(identifier)] = method
        self.process.write({"id": identifier, "method": method, "params": params})
        return native_key(identifier)

    def _rpc(self, method, params):
        key = self._send_rpc(method, params)
        return self._wait_rpc(key)

    def _wait_rpc(self, key):
        deadline = time.monotonic() + 20
        while key not in self.responses:
            self._tick()
            if time.monotonic() >= deadline:
                raise ProcessError("rpc_timeout")
            message = self.process.read(timeout=.1)
            if message is not None:
                self.handle_message(message)
        message = self.responses.pop(key)
        if "error" in message:
            error = ProcessError("rpc_error")
            payload = message.get("error")
            detail = payload.get("message") if isinstance(payload, dict) else None
            if isinstance(detail, str) and len(detail) <= 4096:
                error.native_hint = detail
            raise error
        if not isinstance(message.get("result"), dict):
            raise ProcessError("protocol_error")
        return message["result"]

    def handle_message(self, message):
        if not isinstance(message, dict):
            raise ProcessError("protocol_error")
        if "method" in message:
            if not isinstance(message["method"], str) or "result" in message or "error" in message:
                raise ProcessError("protocol_error")
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise ProcessError("protocol_error")
            if "id" in message:
                native_key(message["id"])
                self._server_request(message)
            else:
                self._notification(message["method"], params)
            return
        if "id" not in message or ("result" in message) == ("error" in message):
            raise ProcessError("protocol_error")
        key = native_key(message["id"])
        fingerprint = hashlib.sha256(encoded(message).encode()).hexdigest()
        if key in self.response_hashes:
            if self.response_hashes[key] != fingerprint:
                raise ProcessError("conflicting_rpc_response")
            return
        method = self.pending.pop(key, None)
        if method is None:
            raise ProcessError("unexpected_rpc_response")
        self.response_hashes[key] = fingerprint
        if method == "turn/interrupt":
            if "error" in message:
                self.event("error", reason="interrupt_rejected")
        else:
            self.responses[key] = message

    def _buffer(self, message):
        if len(self.early) >= 32:
            raise ProcessError("protocol_buffer_full")
        self.early.append(message)

    def _flush_early(self):
        messages, self.early = self.early, []
        for message in messages:
            self.handle_message(message)

    def _bind_turn(self, identifier):
        identifier = self._identifier(identifier)
        if self.turn_id and self.turn_id != identifier:
            raise ProcessError("turn_identity_mismatch")
        if not self.turn_id:
            self.turn_id = identifier
            self._update(turn_id=identifier)
            self._flush_early()

    def _notification(self, method, params):
        thread = params.get("threadId")
        if not thread:
            return
        if not self.thread_id:
            self._buffer({"method": method, "params": params})
            return
        if thread != self.thread_id:
            return
        if method == "serverRequest/resolved":
            key = native_key(params.get("requestId"))
            with self.lock:
                approval = self.approval_ids.get(key)
                if approval:
                    self.store.update_approval(self.id, approval, status="resolved")
                    self.event("approval", approval_id=approval, status="resolved")
                    if not self.terminal and not self.cancelled and not any(
                            self.store.get_approval(self.id, identifier)["status"] in {"pending", "queued", "sending", "sent"}
                            for identifier in self.approval_ids.values()):
                        self._update(state="running")
            return
        if method == "turn/started":
            if self.turn_sent:
                self._bind_turn(params.get("turn", {}).get("id"))
            return
        turn = params.get("turn", {}).get("id") if method == "turn/completed" else params.get("turnId")
        if not turn:
            return
        if not self.turn_id and self.turn_sent:
            self._buffer({"method": method, "params": params})
            return
        if turn != self.turn_id:
            return
        if method == "turn/completed":
            status = params["turn"].get("status")
            if status not in {"completed", "interrupted", "failed"}:
                raise ProcessError("protocol_error")
            self._finish(status)
        elif method in {"item/started", "item/completed"}:
            self._item(params.get("item"), method == "item/completed")
        elif method in {"item/agentMessage/delta", "item/commandExecution/outputDelta"}:
            self._delta(params, method == "item/agentMessage/delta")
        elif method == "thread/tokenUsage/updated":
            self._tokens(params.get("tokenUsage"))
        elif method == "error":
            self.event("error", reason="native_error", will_retry=params.get("willRetry") is True)

    def _path(self, value):
        if not isinstance(value, str) or not value:
            return value, False
        try:
            path = Path(value)
            path = (path if path.is_absolute() else Path(self.cwd) / path).resolve(strict=False)
            return str(path), True
        except (ValueError, OSError):
            return value, False

    def _changes(self, changes):
        if not isinstance(changes, list):
            return [], False
        result, complete = [], bool(changes)
        for row in changes:
            if not isinstance(row, dict):
                complete = False
                continue
            path, resolved = self._path(row.get("path"))
            kind = row.get("kind")
            entry = {"path": path, "diff": row.get("diff"), "kind": kind,
                     "path_resolved": resolved, "scope_verified": resolved}
            if isinstance(kind, dict) and kind.get("move_path"):
                entry["move_path"], move_resolved = self._path(kind["move_path"])
                entry["scope_verified"] = resolved and move_resolved
            complete = complete and isinstance(row.get("diff"), str) and isinstance(path, str) and bool(path)
            result.append(entry)
        return result, complete

    def _item(self, item, final):
        if not isinstance(item, dict):
            raise ProcessError("protocol_error")
        identifier, kind = self._identifier(item.get("id")), item.get("type")
        if kind not in {"agentMessage", "commandExecution", "fileChange"}:
            return
        if len(self.items) >= 2000 and identifier not in self.items:
            raise ProcessError("protocol_item_limit")
        stored = self.items.setdefault(identifier, {})
        if kind == "agentMessage":
            text, truncated = clipped(item.get("text"), 32768 if final else 4096)
            stored.update(text=text, final=final, type=kind, truncated=truncated)
            self.event("agent_message", item_id=identifier, text=text, final=final, truncated=truncated)
        elif kind == "commandExecution":
            output, truncated = clipped(item.get("aggregatedOutput"))
            stored.update(type=kind, output=output, command=item.get("command"), cwd=item.get("cwd"), final=final)
            self.event("command", item_id=identifier, command=item.get("command"), cwd=item.get("cwd"),
                       output=output, status=item.get("status"), exit_code=item.get("exitCode"), truncated=truncated)
        else:
            changes, complete = self._changes(item.get("changes"))
            oversized = len(encoded(changes).encode()) > 65536
            stored.update(type=kind, changes=changes if not oversized else [], detail_complete=complete and not oversized)
            self.event("file_change", item_id=identifier, changes=changes if not oversized else [],
                       status=item.get("status"), truncated=oversized)

    def _delta(self, params, agent):
        identifier = self._identifier(params.get("itemId"))
        if len(self.items) >= 2000 and identifier not in self.items:
            raise ProcessError("protocol_item_limit")
        item = self.items.setdefault(identifier, {})
        if item.get("final"):
            return
        key = "text" if agent else "output"
        delta = params.get("delta")
        if not isinstance(delta, str):
            raise ProcessError("protocol_error")
        value, truncated = clipped(item.get(key, "") + delta)
        item[key] = value
        item["truncated"] = item.get("truncated", False) or truncated
        payload = {"item_id": identifier, key: value, "truncated": item["truncated"]}
        if agent:
            payload["final"] = False
        else:
            payload.update(command=item.get("command"), cwd=item.get("cwd"))
        self.event("agent_message" if agent else "command", **payload)

    def _tokens(self, usage):
        if not isinstance(usage, dict):
            return
        clean = {"source": "codex_app_server"}
        for group in ("total", "last"):
            row = usage.get(group)
            clean[group] = {key: row[key] if type(row.get(key)) is int and row[key] >= 0 else None
                            for key in TOKEN_FIELDS} if isinstance(row, dict) else None
        if self.last_usage and clean["total"] and self.last_usage["total"]:
            previous, current = self.last_usage["total"].get("totalTokens"), clean["total"].get("totalTokens")
            if previous is not None and current is not None and current < previous:
                self.event("token_usage", **clean, usage_inconsistent=True)
                self._update(token_json=encoded({**self.last_usage, "usage_inconsistent": True}))
                return
        self.last_usage = clean
        self._update(token_json=encoded(clean), token_observed_at=now())
        self.event("token_usage", **clean)

    def _server_request(self, message):
        method, params, key = message["method"], message.get("params", {}), native_key(message["id"])
        fingerprint = hashlib.sha256(encoded(message).encode()).hexdigest()
        if key in self.requests:
            if self.requests[key] != fingerprint:
                raise ProcessError("conflicting_server_request")
            return
        if len(self.requests) >= 2000:
            raise ProcessError("protocol_request_limit")
        if method not in {COMMAND_APPROVAL, FILE_APPROVAL}:
            self.requests[key] = fingerprint
            self.event("error", reason="unsupported_request")
            self.process.write({"id": message["id"], "error": {"code": -32601, "message": "unsupported_request"}})
            self.store.request_cancel(self.id, self.owner_id)
            self.interrupt()
            return
        if not self.thread_id or self.turn_sent and not self.turn_id:
            self._buffer(message)
            return
        if params.get("threadId") != self.thread_id or params.get("turnId") != self.turn_id or not self.turn_id:
            raise ProcessError("approval_identity_mismatch")
        self.requests[key] = fingerprint
        item_id = self._identifier(params.get("itemId"))
        if method == COMMAND_APPROVAL:
            cwd, resolved = self._path(params.get("cwd"))
            detail = {"command": params.get("command"), "cwd": cwd, "reason": params.get("reason"),
                      "scope_verified": resolved, "kind": params.get("kind", "command")}
            network = params.get("networkApprovalContext")
            if isinstance(network, dict):
                detail["network_approval_context"] = {name: network.get(name) for name in ("host", "protocol")}
            complete = isinstance(detail["command"], str) and bool(detail["command"]) and isinstance(cwd, str) and bool(cwd)
        else:
            item = self.items.get(item_id, {})
            detail = {"changes": item.get("changes", []), "reason": params.get("reason"),
                      "scope_verified": all(row.get("scope_verified") for row in item.get("changes", []))}
            complete = item.get("type") == "fileChange" and item.get("detail_complete", False)
        if len(encoded(detail).encode()) > 65536:
            detail, complete = {"reason": "approval_detail_too_large"}, False
        with self.lock:
            if self.cancelled or self.terminal:
                return
            row = self.store.add_approval(self.id, self.owner_id, key, method, self.thread_id,
                                          self.turn_id, item_id, detail, bool(complete))
            self.approval_ids[key] = row["id"]
            self._update(state="waiting_approval")
            self.event("approval", approval_id=row["id"], status="pending")

    def answer(self, approval_id, decision):
        with self.lock:
            row, replayed = self.store.claim_approval(self.id, approval_id, self.owner_id, decision)
            if not replayed and approval_id not in self.queued:
                if len(self.actions) >= 128:
                    self.store.update_approval(self.id, approval_id, status="unknown")
                    self.interrupt()
                    raise WorkspaceError("execution_unavailable")
                self.actions.append(approval_id)
                self.queued.add(approval_id)
            if decision == "cancel":
                self.store.request_cancel(self.id, self.owner_id)
                self.interrupt()
            return row, replayed

    def interrupt(self):
        with self.lock:
            self.cancelled = True

    def _tick(self):
        with self.lock:
            while self.actions:
                identifier = self.actions.popleft()
                row = self.store.get_approval(self.id, identifier)
                if row["status"] != "queued":
                    continue
                if self.cancelled or self.terminal or row["owner_id"] != self.owner_id or row["turn_id"] != self.turn_id:
                    self.store.update_approval(self.id, identifier, status="expired")
                    continue
                self.store.update_approval(self.id, identifier, status="sending")
                try:
                    self.process.write({"id": json.loads(row["native_id_json"]), "result": {"decision": row["decision"]}})
                except Exception:
                    self.store.update_approval(self.id, identifier, status="unknown")
                    raise
                self.store.update_approval(self.id, identifier, status="sent")
                self.event("approval", approval_id=identifier, status="sent", decision=row["decision"])
            if self.cancelled and self.turn_id and not self.interrupt_sent and not self.terminal:
                self.interrupt_sent = True
                self.cancel_deadline = time.monotonic() + 5
                self._send_rpc("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})
                self.event("phase", phase="interrupt_requested")
            if self.cancelled and self.cancel_deadline is not None and time.monotonic() >= self.cancel_deadline and not self.terminal:
                raise ProcessError("cancel_timeout")

    def _persist_runtime(self, config_checks=None):
        current = {}
        if self.record.get("effective_policy_json"):
            try:
                current = json.loads(self.record["effective_policy_json"])
            except (TypeError, ValueError):
                current = {}
        if not isinstance(current, dict):
            current = {}
        current["runtime"] = self.runtime.public()
        if config_checks is not None:
            current["config_checks"] = config_checks
        self._update(effective_policy_json=encoded(current))

    def _authenticate(self):
        try:
            response = self._rpc("account/read", {"refreshToken": False})
        except ProcessError:
            raise ProcessError("auth_check_failed") from None
        account = response.get("account") if isinstance(response, dict) else None
        if not account:
            raise ProcessError("login_required")
        self.event("phase", phase="authenticated")

    def _finish(self, state, reason=None):
        with self.lock:
            if self.terminal:
                if self.terminal != state:
                    raise ProcessError("conflicting_terminal")
                return
            self._update(state=state, reason=reason, ended_at=now())
            self.terminal = state
            self.store.expire_approvals(self.id)
            self.event("turn_finished", state=state, reason=reason, cancel_requested=self.cancelled)

    def finalize(self):
        try:
            verified = self.process.close(grace=2) if self.process else True
            self.store.expire_approvals(self.id)
            self._update(cleanup_state="verified" if verified else "unverified")
            self.event("cleanup", verified=verified)
        except Exception:
            try:
                self._update(cleanup_state="unverified")
            except WorkspaceError:
                pass
        finally:
            self.finished.set()

    def close(self):
        self.interrupt()
        if not self.finished.wait(5.5) and self.process:
            self.process.close(grace=0)
        return self.finished.is_set()
