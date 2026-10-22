"""One headless DSH run in the confirmed project directory."""

import json
import os
from pathlib import Path
import re
import shutil
import threading
from uuid import uuid4

from control_everything.execution.owned_process import OwnedProcess, ProcessError
from control_everything.workspace.store import WorkspaceError

MAX_FINAL = 4000


def native_dsh():
    override = os.environ.get("DSH_CLI")
    if override and Path(override).is_file():
        return Path(override).resolve()
    for name in ("dsh.cmd", "dsh.exe", "dsh"):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    raise ProcessError("cli_not_found")


def _text(value, limit=4000):
    if isinstance(value, dict):
        for key in ("text", "content", "message", "result"):
            nested = _text(value.get(key), limit)
            if nested:
                return nested
        return None
    if isinstance(value, list):
        parts = [_text(item, limit) for item in value]
        joined = "\n".join(part for part in parts if part)
        return joined[:limit] if len(joined) > limit else joined or None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if len(value) > limit else value or None


class DshRun:
    def __init__(self, store, run, owner_id, validate_root, policy=None, process_factory=None, resolver=None,
                 runtime=None):
        self.store, self.record, self.owner_id = store, dict(run), owner_id
        self.id, self.cwd = run["id"], run["cwd"]
        self.runtime = runtime
        self.validate_root = validate_root
        self.process_factory = process_factory or OwnedProcess
        self.resolver = resolver or native_dsh
        self.process = None
        self.lock = threading.RLock()
        self.finished = threading.Event()
        self.cancelled = bool(run.get("cancel_requested"))
        self.terminal = None
        self.texts = []
        self.final_text = None
        self.final_seen = False
        self.terminal_status = None
        self.native_error = False
        self.session_id = None
        self.auth_status = None

    def event(self, kind, **payload):
        self.store.append_event(self.id, kind, payload)

    def _update(self, **fields):
        self.store.update_run(self.id, **fields)
        self.record.update(fields)

    def _require_root(self):
        if not self.validate_root(self.record):
            raise ProcessError("context_changed")

    def _finish(self, state, reason=None):
        with self.lock:
            if self.terminal:
                return
            self._update(state=state, reason=reason, ended_at=self._now())
            self.terminal = state
            self.event("turn_finished", state=state, reason=reason, cancel_requested=self.cancelled)

    @staticmethod
    def _now():
        from control_everything.workspace.store import now_utc
        return now_utc()

    def _emit_final(self):
        text = self.final_text if self.final_text is not None else "未返回最终文本"
        if len(text) > MAX_FINAL:
            marker = "\n…[truncated]"
            text = text[:MAX_FINAL - len(marker)] + marker
        self.event("agent_message", item_id=None, text=text, final=True, truncated=len(text) >= MAX_FINAL)

    def _handle(self, message):
        if not isinstance(message, dict):
            return
        kind = message.get("type") or message.get("kind") or message.get("event")
        if not isinstance(kind, str):
            return
        name = kind.casefold()
        if name == "session":
            session = message.get("sessionId") or message.get("session_id")
            if isinstance(session, str) and 0 < len(session) <= 1024:
                self.session_id = session
                self._update(thread_id=session)
            return
        if name == "status":
            phase = message.get("phase")
            if phase == "turn_end":
                reason = message.get("reason")
                terminal = reason.get("kind") if isinstance(reason, dict) else None
                if terminal in ("completed", "error", "cancelled", "interrupted"):
                    self.terminal_status = terminal
                    if terminal == "error":
                        self.native_error = True
                        error = reason.get("error") if isinstance(reason, dict) else None
                        code = error.get("code") if isinstance(error, dict) else None
                        if code in ("MISSING_CREDENTIAL", "ACCOUNT_SIGN_IN_REQUIRED"):
                            self.auth_status = "login_required"
                            self.event("phase", phase="auth", status="login_required")
                        elif code:
                            self.auth_status = "error"
            return
        if name == "text":
            text = _text(message.get("text") or message.get("content"))
            if text:
                self.texts.append(text)
                self.event("agent_message", item_id=None, text=text, final=False,
                           truncated=len(text) >= MAX_FINAL)
            return
        if name == "thinking":
            self.event("phase", phase="native_thinking")
            return
        if name in {"final", "result", "done", "completion", "completed"}:
            self.final_seen = True
            final = _text(message.get("text") or message.get("result") or message.get("content")
                          or message.get("message"))
            if final is not None:
                self.final_text = final
            self.event("phase", phase="native_final")
            return
        if name in {"error", "failure"}:
            self.native_error = True
            self.terminal_status = "error"
            self.event("error", reason="native_error")
            return
        if name in {"tool_call", "tool_result", "tool", "tool_start", "command", "exec"}:
            self.event("phase", phase="native_tool", native_kind=name[:64])
            return
        if name in {"file_change", "filechange"}:
            changes = message.get("changes")
            paths = []
            if isinstance(changes, list):
                for change in changes:
                    path = change.get("path") if isinstance(change, dict) else None
                    if isinstance(path, str) and 0 < len(path) <= 2048:
                        paths.append(path)
            if paths:
                self.event("file_change", changes=[{"path": path} for path in paths[:20]])
            return
        self.event("phase", phase="native_event", native_kind=re.sub(r"[^a-z0-9_.-]", "", name)[:64])

    def run(self):
        try:
            if self.cancelled:
                self._finish("cancelled_before_send", "cancelled_before_send")
                return
            self._require_root()
            executable = str(self.resolver())
            if self.cancelled:
                self._finish("cancelled_before_send", "cancelled_before_send")
                return
            command = os.environ.get("COMSPEC")
            if not command:
                system_root = os.environ.get("SYSTEMROOT")
                if not system_root:
                    raise ProcessError("shell_not_found")
                command = str(Path(system_root) / "System32" / "cmd.exe")
            job_name = f"Local\\mianban_dsh.{self.owner_id}.{self.id}.{uuid4()}"
            self._update(job_name=job_name)
            arguments = ["/d", "/s", "/c", executable]
            if self.runtime is not None:
                arguments += ["headless", "--patch", str(self.runtime.overlay_path), "--json", "-"]
            else:
                arguments += ["headless", "--json", "-"]
            if '"' in executable:
                raise ProcessError("invalid_launcher")
            if self.runtime is None:
                command_line = f'/d /s /c ""{executable}" headless --json -"'
            else:
                overlay = str(self.runtime.overlay_path)
                if '"' in overlay:
                    raise ProcessError("invalid_launcher")
                command_line = f'/d /s /c ""{executable}" headless --patch "{overlay}" --json -"'
            self.process = self.process_factory(command, arguments, self.cwd, job_name,
                                                command_line=command_line,
                                                line_prefixes=("[llm-deepseek-account] ",))
            self.process.start()
            with self.lock:
                if self.cancelled:
                    self._finish("cancelled_before_send", "cancelled_before_send")
                    return
                self._update(turn_send_started=True)
                self.process.write_text(self.record["prompt"], close=True)
            self.event("phase", phase="started", adapter="dsh")
            while True:
                try:
                    message = self.process.read(timeout=.1)
                except ProcessError as error:
                    if str(error) == "process_eof":
                        break
                    raise
                if message is not None:
                    self._handle(message)
                elif self.cancelled:
                    break
            if self.terminal:
                return
            if self.cancelled:
                self._finish("interrupted", "cancelled")
                return
            code = self.process.exit_code() if hasattr(self.process, "exit_code") else None
            success = (code == 0 and self.final_seen and self.terminal_status == "completed"
                       and not self.native_error)
            if success:
                self._emit_final()
                self._finish("completed")
            else:
                reason = ("login_required" if self.auth_status == "login_required" else
                          "native_exit_failed" if code not in (None, 0)
                          else "native_error" if self.native_error or self.terminal_status == "error"
                          else "no_final_text")
                self._finish("failed", reason)
        except Exception as error:
            reason = str(error) if isinstance(error, ProcessError) else (
                "storage_unavailable" if isinstance(error, WorkspaceError) else "execution_failed")
            try:
                self.event("error", reason=reason)
                if not self.terminal:
                    if self.cancelled:
                        self._finish("interrupted", "cancelled")
                    else:
                        self._finish("unknown" if self.record.get("turn_send_started") else "failed", reason)
            except WorkspaceError:
                pass
        finally:
            self.finalize()

    def interrupt(self):
        with self.lock:
            self.cancelled = True
        try:
            if self.process is not None:
                self.process.close(grace=0)
        except Exception:
            pass

    def finalize(self):
        try:
            verified = self.process.close(grace=2) if self.process is not None else True
            self._update(cleanup_state="verified" if verified else "unverified")
            self.event("cleanup", verified=bool(verified))
        except Exception:
            try:
                self._update(cleanup_state="unverified")
            except WorkspaceError:
                pass
        finally:
            self.finished.set()

    def close(self):
        try:
            if self.process is not None:
                self.process.close(grace=2)
        finally:
            self.finished.set()
