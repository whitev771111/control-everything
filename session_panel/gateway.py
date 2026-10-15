"""Loopback HTTP surface for one shared SessionService."""

import json
import re
import secrets
import socket
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .source import HOST, PORT, SOURCE, canonical_uuid
from .codex_index import list_threads as list_codex_threads
from .progress import MANUAL, REGISTRY, TODOS, add_todo, board, set_progress, toggle_todo, load_todos
from .launchers import catalog, start
from .desk import headlines, pulse
from .garden import GardenService
from .garden_native import GardenError, actor_from_environment
from .garden_public import read_public_messages
from .workspace import WorkspaceService
from .workspace_store import WorkspaceError

APP = "remote-claude-session-panel"
WEB = Path(__file__).resolve().parent.parent / "web"
SESSION_ROUTE = re.compile(r"/api/sessions/([^/]+)(?:/(messages|focus))?\Z")
GARDEN_PUBLIC_ROUTE = re.compile(r"/api/garden/actors/([^/]+)/public-messages\Z")
EXECUTION_RUN_ROUTE = re.compile(r"/api/workspace/execution/runs/([^/]+)\Z")
EXECUTION_ACTION_ROUTE = re.compile(r"/api/workspace/execution/runs/([^/]+)/(cancel|approvals/([^/]+))\Z")
GARDEN_POST = {
    "/api/garden/request-review": ("request_review", {"task_id", "version", "artifact_sha256", "reviewer_id"}),
    "/api/garden/accept": ("accept_artifact", {"task_id", "version", "artifact_sha256", "summary"}),
}


def invalid_json_constant(_value):
    raise ValueError("invalid_json")


def codex_offset(query):
    values = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    if set(values) - {"offset"} or any(len(value) != 1 for value in values.values()):
        raise ValueError("invalid_query")
    value = values.get("offset", ["0"])[0]
    if not re.fullmatch(r"[0-9]{1,9}", value):
        raise ValueError("invalid_query")
    return int(value)


def message_params(query):
    values = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    if set(values) - {"limit", "offset", "revision"} or any(len(v) != 1 for v in values.values()):
        raise ValueError("invalid_query")
    result = {"limit": 20, "offset": None, "revision": None}
    for name in ("limit", "offset"):
        if name in values:
            value = values[name][0]
            if not re.fullmatch(r"[0-9]{1,10}", value):
                raise ValueError("invalid_query")
            result[name] = int(value)
    if not 1 <= result["limit"] <= 50 or (result["offset"] is not None and result["offset"] < 0):
        raise ValueError("invalid_query")
    if "revision" in values:
        revision = values["revision"][0]
        if not revision or len(revision) > 160 or any(ord(c) < 32 for c in revision):
            raise ValueError("invalid_query")
        result["revision"] = revision
    return result


def result_status(result):
    reason = result.get("reason")
    if reason == "not_found":
        return 404
    if reason in {"workbench_launch_failed", "workbench_timeout"}:
        return 503
    if (reason in {"identity_conflict", "messages_changed", "revision_mismatch", "binding_unknown", "binding_changed",
                   "terminal_action_busy", "cleanup_unverified"} or isinstance(reason, str) and reason.startswith("workbench_")):
        return 409
    return 200


class PanelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        # Request paths may identify a private conversation. Do not log them.
        pass

    def reply(self, status, payload, content_type="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'none'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def reject(self, status, reason):
        self._discard_rejected_post_body()
        self.reply(status, {"status": "unknown", "reason": reason, "source": SOURCE,
                            "observed_at": datetime.now(timezone.utc).isoformat()})

    def _discard_rejected_post_body(self):
        if not getattr(self, "_post_body_pending", False):
            return
        self._post_body_pending = False
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding", []) or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            return
        remaining = int(lengths[0])
        if not 1 <= remaining <= 4096:
            return
        previous_timeout = self.connection.gettimeout()
        deadline = time.monotonic() + 0.25
        try:
            while remaining:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    break
                self.connection.settimeout(budget)
                chunk = self.rfile.read1(remaining)
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        finally:
            self.connection.settimeout(previous_timeout)

    def allowed(self, *, api=False, focus=False):
        if self.headers.get_all("Host", []) != [self.server.expected_host]:
            self.reject(403, "host_rejected")
            return False
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [self.server.origin]:
            self.reject(403, "origin_rejected")
            return False
        if api and self.headers.get_all("X-Session-Client", []) not in (["panel"], ["stdio"]):
            self.reject(403, "client_header_required")
            return False
        if focus and (origins != [self.server.origin] or self.headers.get_all("X-Session-CSRF", []) != [self.server.csrf_token]):
            self.reject(403, "csrf_rejected")
            return False
        return True

    def do_OPTIONS(self):
        self.reject(403, "cross_origin_not_allowed")

    def do_GET(self):
        path = urlsplit(self.path)
        if not self.allowed(api=path.path.startswith("/api/")):
            return
        if path.path == "/health" and not path.query:
            return self.reply(200, {"app": APP, "protocol_version": 1})
        static = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
            "/cockpit.js": ("cockpit.js", "text/javascript; charset=utf-8"),
            "/robot3d.js": ("robot3d.js", "text/javascript; charset=utf-8"),
            "/garden.js": ("garden.js", "text/javascript; charset=utf-8"),
            "/progress": ("progress.html", "text/html; charset=utf-8"),
            "/progress.js": ("progress.js", "text/javascript; charset=utf-8"),
            "/progress.css": ("progress.css", "text/css; charset=utf-8"),
            "/workspace": ("workspace.html", "text/html; charset=utf-8"),
            "/workspace.js": ("workspace.js", "text/javascript; charset=utf-8"),
            "/execution.js": ("execution.js", "text/javascript; charset=utf-8"),
            "/workspace.css": ("workspace.css", "text/css; charset=utf-8"),
            "/vendor/three-0.186.0/three.module.js": ("vendor/three-0.186.0/three.module.js", "text/javascript; charset=utf-8"),
            "/vendor/three-0.186.0/three.core.js": ("vendor/three-0.186.0/three.core.js", "text/javascript; charset=utf-8"),
            "/vendor/three-0.186.0/LICENSE": ("vendor/three-0.186.0/LICENSE", "text/plain; charset=utf-8"),
        }
        if path.path in static and not path.query:
            filename, mime = static[path.path]
            try:
                body = (WEB / filename).read_bytes()
            except OSError:
                return self.reject(404, "route_not_found")
            return self.reply(200, body, mime)
        try:
            if (path.path in {"/api/workspace/execution", "/api/workspace/execution/context"}
                    or EXECUTION_RUN_ROUTE.fullmatch(path.path)):
                return self.execution_reply(path.path, query=path.query)
            if path.path in {"/api/workspace", "/api/workspace/usage", "/api/workspace/news"}:
                if path.query:
                    raise ValueError("invalid_query")
                return self.workspace_reply(path.path)
            if path.path == "/api/progress":
                if path.query:
                    raise ValueError("invalid_query")
                return self.reply(200, {"projects": board(REGISTRY, MANUAL)})
            if path.path == "/api/desk":
                if path.query:
                    raise ValueError("invalid_query")
                return self.reply(200, {"launchers": catalog(), "pulse": pulse(), "news": headlines(), "todos": load_todos(TODOS)})
            if path.path == "/api/garden":
                if path.query:
                    raise ValueError("invalid_query")
                return self.garden_reply()
            if match := GARDEN_PUBLIC_ROUTE.fullmatch(path.path):
                if path.query:
                    raise ValueError("invalid_query")
                return self.garden_public_reply(canonical_uuid(match[1]))
            if path.path == "/api/codex/threads":
                result = list_codex_threads(codex_offset(path.query))
                return self.reply(200 if result["available"] else 503, result)
            if path.path == "/api/bootstrap" and not path.query:
                value = {"csrf_token": self.server.csrf_token, "app": APP}
                if getattr(self.server, "garden", None):
                    value["garden_root"] = self.server.garden.context.root_thread_id
                return self.reply(200, value)
            if path.path == "/api/sessions" and not path.query:
                result = self.server.service.list_sessions()
            elif path.path == "/api/workbench":
                if path.query:
                    raise ValueError("invalid_query")
                return self.reply(200, self.server.service.terminal.workbench.public_status())
            else:
                match = SESSION_ROUTE.fullmatch(path.path)
                if not match or match[2] == "focus":
                    return self.reject(404, "route_not_found")
                session_id = canonical_uuid(match[1])
                if match[2] == "messages":
                    result = self.server.service.get_session_messages(session_id, **message_params(path.query))
                elif path.query:
                    raise ValueError("invalid_query")
                else:
                    result = self.server.service.get_session_status(session_id)
            self.reply(result_status(result), result)
        except ValueError:
            self.reject(400, "invalid_request")
        except Exception:
            self.reject(503, "gateway_query_failed")

    def do_POST(self):
        self._post_body_pending = True
        if not self.allowed(api=True, focus=True):
            return
        path = urlsplit(self.path)
        if path.path == "/api/workspace/execution/runs" or EXECUTION_ACTION_ROUTE.fullmatch(path.path):
            if path.query:
                return self.reject(400, "invalid_request")
            try:
                body = self.garden_body(max_bytes=24576 if path.path == "/api/workspace/execution/runs" else 4096)
            except (ValueError, UnicodeDecodeError, OSError):
                return self.reject(400, "invalid_request")
            return self.execution_reply(path.path, body)
        if path.path in {"/api/workspace/tasks", "/api/workspace/tasks/update",
                         "/api/workspace/records", "/api/workspace/usage/refresh"}:
            if path.query:
                return self.reject(400, "invalid_request")
            if self.server.workspace is None:
                return self.reject(503, "workspace_unavailable")
            try:
                body = self.garden_body()
            except (ValueError, UnicodeDecodeError, OSError):
                return self.reject(400, "invalid_request")
            return self.workspace_reply(path.path, body)
        if path.path == "/api/progress/set":
            if path.query:
                return self.reject(400, "invalid_request")
            try:
                body = self.garden_body()
                if set(body) != {"project_id", "state", "note", "percent"}:
                    raise ValueError("invalid_body")
                if not set_progress(body["project_id"], body["state"], body["note"], body["percent"], REGISTRY, MANUAL):
                    return self.reject(404, "project_not_found")
            except (ValueError, UnicodeDecodeError, OSError):
                return self.reject(400, "invalid_request")
            return self.reply(200, {"projects": board(REGISTRY, MANUAL)})
        if path.path == "/api/launch":
            if path.query:
                return self.reject(400, "invalid_request")
            try:
                body = self.garden_body()
                if set(body) != {"id"} or not isinstance(body["id"], str) or not start(body["id"]):
                    raise ValueError("invalid_body")
            except (ValueError, UnicodeDecodeError, OSError):
                return self.reject(400, "invalid_request")
            return self.reply(200, {"launchers": catalog()})
        if path.path in {"/api/todos", "/api/todos/toggle"}:
            if path.query:
                return self.reject(400, "invalid_request")
            try:
                body = self.garden_body()
                if path.path == "/api/todos":
                    if set(body) != {"text", "project_id"}:
                        raise ValueError("invalid_body")
                    items = add_todo(body["text"], body["project_id"], TODOS, REGISTRY)
                else:
                    if set(body) != {"id"}:
                        raise ValueError("invalid_body")
                    items = toggle_todo(body["id"], TODOS)
                if items is None:
                    return self.reject(404, "todo_not_found")
            except (ValueError, UnicodeDecodeError, OSError):
                return self.reject(400, "invalid_request")
            return self.reply(200, {"todos": items})
        if path.path in GARDEN_POST:
            if path.query:
                return self.reject(400, "invalid_request")
            try:
                body = self.garden_body()
                method, fields = GARDEN_POST[path.path]
                if set(body) != fields:
                    raise ValueError("invalid_body")
                return self.garden_reply(method, body)
            except (ValueError, UnicodeDecodeError, OSError):
                return self.reject(400, "invalid_request")
        match = SESSION_ROUTE.fullmatch(path.path)
        connect = path.path == "/api/workbench/connect"
        if (not connect and (not match or match[2] != "focus")) or path.query:
            return self.reject(404, "route_not_found")
        try:
            session_id = None if connect else canonical_uuid(match[1])
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                raise ValueError("invalid_body")
            length = int(lengths[0])
            if length > 4096:
                return self.reject(413, "request_too_large")
            if self.headers.get("Content-Type") != "application/json":
                raise ValueError("invalid_body")
            self._post_body_pending = False
            if json.loads(self.rfile.read(length)) != {}:
                raise ValueError("invalid_body")
            result = (self.server.service.terminal.connect_workbench() if connect else
                      self.server.service.terminal.focus_session(self.server.service, session_id))
            self.reply(result_status(result), result)
        except (ValueError, UnicodeDecodeError):
            self.reject(400, "invalid_request")
        except Exception:
            self.reject(503, "workbench_launch_failed" if connect else "focus_failed")

    def workspace_reply(self, path, body=None):
        workspace = self.server.workspace
        if workspace is None:
            return self.reject(503, "workspace_unavailable")
        try:
            if path == "/api/workspace":
                value = workspace.snapshot()
            elif path == "/api/workspace/usage":
                value = workspace.usage()
            elif path == "/api/workspace/news":
                value = workspace.news()
            elif path == "/api/workspace/usage/refresh":
                if body != {}:
                    raise ValueError("invalid_request")
                value = workspace.usage(refresh=True)
            elif path == "/api/workspace/records":
                value = workspace.save_record(body)
            else:
                value = workspace.save_task(body, update=path == "/api/workspace/tasks/update")
            return self.reply(200, value)
        except WorkspaceError as error:
            reason = str(error)
            status = (404 if reason in {"task_not_found", "record_not_found"} else
                      409 if reason in {"session_not_in_sample", "session_project_mismatch"} else 503)
            return self.reject(status, reason)
        except (ValueError, TypeError):
            return self.reject(400, "invalid_request")
        except Exception:
            return self.reject(503, "workspace_unavailable")

    def execution_reply(self, path, body=None, *, query=""):
        execution = getattr(self.server, "execution", None)
        status, reason, value = 200, None, None
        try:
            if execution is None:
                raise WorkspaceError(getattr(self.server, "execution_reason", None) or "execution_unavailable")
            values = parse_qs(query, keep_blank_values=True, strict_parsing=True)
            if any(len(value) != 1 for value in values.values()):
                raise ValueError("invalid_request")
            status = 200
            if path == "/api/workspace/execution":
                if set(values) - {"request_id"}:
                    raise ValueError("invalid_request")
                value = execution.snapshot(request_id=canonical_uuid(values["request_id"][0])) if values else execution.snapshot()
            elif path == "/api/workspace/execution/context":
                if set(values) != {"task_id"}:
                    raise ValueError("invalid_request")
                value = execution.context(canonical_uuid(values["task_id"][0]))
            elif body is not None:
                if path == "/api/workspace/execution/runs":
                    value = execution.submit(body)
                else:
                    match = EXECUTION_ACTION_ROUTE.fullmatch(path)
                    value = execution.act(canonical_uuid(match[1]), body,
                                          approval_id=canonical_uuid(match[3]) if match[3] else None)
                status = 200 if value["replayed"] else 202
            else:
                match = EXECUTION_RUN_ROUTE.fullmatch(path)
                if set(values) - {"after"}:
                    raise ValueError("invalid_request")
                after = values.get("after", ["0"])[0]
                if not re.fullmatch(r"[0-9]{1,19}", after) or int(after) > 9223372036854775807:
                    raise ValueError("invalid_request")
                value = execution.snapshot(canonical_uuid(match[1]), int(after))
        except WorkspaceError as error:
            reason = str(error)
            missing = {"task_not_found", "run_not_found", "approval_not_found", "request_not_found"}
            conflict = {"idempotency_conflict", "context_changed", "project_busy", "capacity_busy",
                        "approval_expired", "decision_conflict", "other_instance"}
            if reason not in missing | conflict | {"storage_unavailable", "execution_unavailable"}:
                reason = "execution_unavailable"
            status = 404 if reason in missing else 409 if reason in conflict else 503
        except (ValueError, TypeError):
            status, reason = 400, "invalid_request"
        except Exception:
            status, reason = 503, "execution_unavailable"
        try:
            return self.reject(status, reason) if reason else self.reply(status, value)
        except OSError:
            # A disconnected client does not change an already committed receipt.
            return None

    def garden_body(self, max_bytes=4096):
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get_all("Transfer-Encoding", []) or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,10}", lengths[0])
                or self.headers.get_all("Content-Type", []) != ["application/json"]):
            raise ValueError("invalid_body")
        length = int(lengths[0])
        if not 1 <= length <= max_bytes:
            raise ValueError("invalid_body")
        self._post_body_pending = False
        previous_timeout = self.connection.gettimeout()
        deadline, data = time.monotonic() + 0.25, bytearray()
        try:
            while len(data) < length:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    raise ValueError("body_timeout")
                self.connection.settimeout(budget)
                chunk = self.rfile.read1(length - len(data))
                if not chunk:
                    raise ValueError("invalid_body")
                data.extend(chunk)
        finally:
            self.connection.settimeout(previous_timeout)
        value = json.loads(data, parse_constant=invalid_json_constant)
        if not isinstance(value, dict):
            raise ValueError("invalid_body")
        return value

    def garden_reply(self, operation=None, body=None):
        garden = getattr(self.server, "garden", None)
        if garden is None:
            return self.reply(503, {"available": False, "reason": "garden_unbound", "root_thread_id": None})
        try:
            if garden.context.actor_id != garden.context.root_thread_id:
                raise GardenError("garden_unbound")
            value = (getattr(garden, operation)(**body, source="panel_web") if operation else garden.snapshot())
            return self.reply(200, value)
        except GardenError as error:
            reason = str(error)
            status = 503 if reason.startswith(("native_", "ledger_", "garden_unbound", "parent_")) else 409
            return self.reply(status, {"available": False, "reason": reason, "root_thread_id": garden.context.root_thread_id})
        except (ValueError, TypeError):
            return self.reply(400, {"available": False, "reason": "invalid_request", "root_thread_id": garden.context.root_thread_id})
        except Exception:
            return self.reply(503, {"available": False, "reason": "garden_unavailable", "root_thread_id": garden.context.root_thread_id})

    def garden_public_reply(self, actor_id):
        garden = getattr(self.server, "garden", None)
        if garden is None or garden.context.actor_id != garden.context.root_thread_id:
            return self.reply(503, {"available": False, "reason": "garden_unbound", "root_thread_id": None, "actor_id": actor_id})
        try:
            return self.reply(200, read_public_messages(garden, actor_id))
        except GardenError as error:
            reason = str(error)
            status = 404 if reason == "actor_not_in_domain" else 503
            return self.reply(status, {"available": False, "reason": reason,
                                       "root_thread_id": garden.context.root_thread_id, "actor_id": actor_id})


class PanelServer(ThreadingHTTPServer):
    def server_close(self):
        execution = getattr(self, "execution", None)
        self.execution = None
        try:
            if execution is not None:
                execution.close()
        finally:
            super().server_close()

    def server_bind(self):
        if sys.platform == "win32":
            # Windows SO_REUSEADDR can share a listener. Reserve it before bind.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(service, address=(HOST, PORT), *, garden=None, workspace=None, execution=None):
    # The address injection exists for temporary fixture tests. Production is fixed.
    if address[0] != HOST:
        raise ValueError("loopback_only")
    try:
        server = PanelServer(address, PanelHandler)
    except Exception:
        if execution is not None:
            execution.close()
        raise
    server.daemon_threads = True
    server.service = service
    server.garden = garden
    server.workspace = workspace
    server.execution = execution
    server.execution_reason = None
    server.expected_host = f"{HOST}:{server.server_address[1]}"
    server.origin = f"http://{server.expected_host}"
    server.csrf_token = secrets.token_urlsafe(32)
    return server


def serve():
    from .service import SessionService

    try:
        garden = None
        try:
            context = actor_from_environment()
            if context.actor_id == context.root_thread_id:
                garden = GardenService(context)
        except GardenError:
            pass
        server = make_server(SessionService(), garden=garden, workspace=WorkspaceService())
    except OSError:
        print("会话网关无法监听 127.0.0.1:8766，请检查端口是否被占用。", file=sys.stderr)
        return 1
    try:
        from .execution import ExecutionService
        server.execution = ExecutionService(server.workspace)
    except Exception:
        server.execution_reason = "execution_unavailable"
    print(f"工作台：http://{HOST}:{PORT}/workspace；会话面板：http://{HOST}:{PORT}/；项目进度：http://{HOST}:{PORT}/progress；Ctrl+C 停止。", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
