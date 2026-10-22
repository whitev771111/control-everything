"""Loopback HTTP surface for one shared SessionService."""

import json
import copy
import argparse
import re
import secrets
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from control_everything.paths import web_root, product_version_dir
from control_everything.installation.registry import ProductContext, register_http, default_api, RegistryError
from control_everything.installation.manifest import ManifestError
from control_everything.platform.windows_installation import InstallationError
from urllib.parse import parse_qs, urlsplit

from control_everything.sessions.source import HOST, PORT, canonical_uuid
from control_everything.sessions.codex_index import list_threads as list_codex_threads
from control_everything.sessions.observer_client import ObserverClient
from control_everything.http import observation_routes, onboarding_routes, sandbox_routes
from control_everything.workspace.progress import MANUAL, REGISTRY, TODOS, add_todo, board, set_progress, toggle_todo, load_todos
from control_everything.integrations.launchers import LauncherCatalog, catalog, start
from control_everything.integrations.desk import headlines, pulse
from control_everything.workspace.service import WorkspaceService
from control_everything.execution.service import dispatch_execution_context
from control_everything.workspace import project_dirs
from control_everything.workspace.news_source import NewsSource
from control_everything.workspace.store import WorkspaceError
from control_everything.profiles.profile import Profile, ProfileError, strict_json
from control_everything.profiles import agent_adapters

APP = "remote-claude-session-panel"
WEB = web_root()
SESSION_ROUTE = re.compile(r"/api/sessions/([^/]+)(?:/(messages|focus))?\Z")
EXECUTION_RUN_ROUTE = re.compile(r"/api/workspace/execution/runs/([^/]+)\Z")
EXECUTION_ACTION_ROUTE = re.compile(r"/api/workspace/execution/runs/([^/]+)/(cancel|approvals/([^/]+))\Z")
AGENT_TASK_ROUTE = re.compile(r"/api/agent/tasks/([^/]+)(?:/(claim|progress|submit-review|review))?\Z")
AGENT_BROWSER_TASK_ROUTE = re.compile(r"/api/workspace/agent-tasks/([^/]+)\Z")
AGENT_BROWSER_CANCEL_ROUTE = re.compile(r"/api/workspace/agent-tasks/([^/]+)/cancel\Z")
TASK_BRIEF_ROUTE = re.compile(r"/api/workspace/tasks/([^/]+)/brief\Z")
TASK_DISPATCH_ROUTE = re.compile(r"/api/workspace/tasks/([^/]+)/dispatch(-preview)?\Z")
TASK_DISPATCHES_ROUTE = re.compile(r"/api/workspace/tasks/([^/]+)/dispatches\Z")
DISPATCH_ROUTE = re.compile(r"/api/workspace/dispatches/([^/]+)(?:/(sent|execute|cancel))?\Z")
DISPATCH_EXECUTION_CONTEXT_ROUTE = re.compile(r"/api/workspace/dispatches/([^/]+)/execution-context\Z")
TASK_SESSIONS_ROUTE = re.compile(r"/api/workspace/tasks/([^/]+)/sessions\Z")
PROJECT_SANDBOX_ROUTE = re.compile(r"/api/workspace/projects/([^/]+)/sandbox-preparation\Z")
TASK_SESSION_ROUTE = re.compile(r"/api/workspace/sessions/([^/]+)(?:/unlink)?\Z")
SESSIONS_ROUTE = re.compile(r"/api/workspace/sessions\Z")
TASK_MESSAGES_ROUTE = re.compile(r"/api/workspace/tasks/([^/]+)/messages\Z")
MESSAGE_ROUTE = re.compile(r"/api/workspace/messages/([^/]+)(?:/(sent|cancel))?\Z")
PROFILE_POST = {"/api/profile/settings", "/api/profile/import", "/api/profile/observations", "/api/workspace/projects/save",
                "/api/workspace/projects/restore", "/api/workspace/progress",
                "/api/workspace/items", "/api/workspace/items/update",
                "/api/workspace/decisions", "/api/workspace/decisions/update", "/api/workspace/reviews",
                "/api/workspace/agents", "/api/workspace/agents/token",
                "/api/workspace/directories/pick", "/api/workspace/projects/inspect",
                "/api/workspace/projects/register", "/api/workspace/projects/create-preview",
                "/api/workspace/projects/create"}
PROFILE_POST.update(onboarding_routes.POST_ROUTES)
PROFILE_CAPABILITIES = {"profile": True, "project_management": True, "execution": False,
                        "personal_launchers": False, "remote_sessions": False, "terminal": False, "workbench": False,
                        "quota": True, "news": True}


def optional_capabilities(route, method):
    """Map only published optional routes; unknown paths remain ordinary 404s."""
    if route == "/api/launch":
        return ("personal_launchers",)
    if method == "GET" and route == "/api/sessions":
        return ("remote_sessions",)
    if method == "GET" and route == "/api/workbench" or method == "POST" and route == "/api/workbench/connect":
        return ("workbench",)
    match = SESSION_ROUTE.fullmatch(route)
    if match:
        if method == "POST" and match[2] == "focus":
            return ("terminal", "workbench")
        if method == "GET" and match[2] in (None, "messages"):
            return ("remote_sessions",)
    return None


class BodyTooLarge(ValueError):
    pass


def page_params(query, *, extra=(), default=50):
    values = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    if set(values) - {"offset", "limit", *extra} or any(len(value) != 1 for value in values.values()):
        raise ValueError("invalid_query")
    result = {"offset": 0, "limit": default}
    for key in ("offset", "limit"):
        if key in values:
            if not re.fullmatch(r"[0-9]{1,10}", values[key][0]):
                raise ValueError("invalid_query")
            result[key] = int(values[key][0])
    if not 1 <= result["limit"] <= 100:
        raise ValueError("invalid_query")
    result.update({key: values[key][0] for key in extra if key in values})
    return result


def invalid_json_constant(_value):
    raise ValueError("invalid_json")


def news_stream(query):
    values = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    if not values:
        return 'ai'
    if set(values) != {'stream'} or len(values['stream']) != 1 or values['stream'][0] not in ('ai', 'hn'):
        raise ValueError('invalid_query')
    return values['stream'][0]


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
    def profile(self):
        # Mock fixtures must not gain profile behavior through dynamic attributes.
        return self.server.__dict__.get("profile")

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
        self.reply(status, {"status": "unknown", "reason": reason, "source": self.server.__dict__.get("source"),
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
        if path.path.startswith("/api/agent/"):
            return self.agent_get(path)
        if not self.allowed(api=path.path.startswith("/api/")):
            return
        if path.path == "/health" and not path.query:
            return self.reply(200, {"app": APP, "protocol_version": 1})
        if path.path == "/progress" and not path.query and self.profile() is not None:
            self.send_response(302)
            self.send_header("Location", "/workspace#projects")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        static = {
            "/": ("sessions/index.html", "text/html; charset=utf-8"),
            "/app.js": ("sessions/app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("sessions/style.css", "text/css; charset=utf-8"),
            "/progress": ("progress/progress.html", "text/html; charset=utf-8"),
            "/progress.js": ("progress/progress.js", "text/javascript; charset=utf-8"),
            "/progress.css": ("progress/progress.css", "text/css; charset=utf-8"),
            "/product": ("product/index.html", "text/html; charset=utf-8"),
            "/product.css": ("product/product.css", "text/css; charset=utf-8"),
            "/product.js": ("product/product.js", "text/javascript; charset=utf-8"),
            "/workspace": ("workspace/workspace.html", "text/html; charset=utf-8"),
            "/workspace.js": ("workspace/workspace.js", "text/javascript; charset=utf-8"),
            "/onboarding.js": ("workspace/onboarding.js", "text/javascript; charset=utf-8"),
            "/agents.js": ("workspace/agents.js", "text/javascript; charset=utf-8"),
            "/project-space.js": ("workspace/project-space.js", "text/javascript; charset=utf-8"),
            "/task-center.js": ("workspace/task-center.js", "text/javascript; charset=utf-8"),
            "/execution.js": ("workspace/execution.js", "text/javascript; charset=utf-8"),
            "/observation.js": ("workspace/observation.js", "text/javascript; charset=utf-8"),
            "/news.js": ("workspace/news.js", "text/javascript; charset=utf-8"),
            "/quota.js": ("workspace/quota.js", "text/javascript; charset=utf-8"),
            "/workspace.css": ("workspace/workspace.css", "text/css; charset=utf-8"),
        }
        if path.path in static and not path.query:
            filename, mime = static[path.path]
            try:
                body = (WEB / filename).read_bytes()
            except OSError:
                return self.reject(404, "route_not_found")
            return self.reply(200, body, mime)
        try:
            if path.path == '/api/desk/news':
                if path.query:
                    raise ValueError('invalid_query')
                source = self.server.__dict__.get('news_source') or NewsSource()
                return self.reply(200, source.get('hn'))
            if path.path == '/api/workspace/news':
                return self.workspace_reply(path.path, stream=news_stream(path.query))
            if path.path == "/api/workspace/observations":
                if path.query:
                    raise ValueError("invalid_query")
                return self.reply(200, self.server.workspace.observations())
            if self.profile() is not None and self.profile_get(path):
                return
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
                return self.reply(200, {"launchers": self.server.launchers.catalog() if self.server.launchers is not None else catalog(), "pulse": pulse(), "news": headlines(), "news_state": NewsSource().cached('hn'), "todos": load_todos(TODOS)})
            if path.path == "/api/codex/threads":
                result = list_codex_threads(codex_offset(path.query))
                return self.reply(200 if result["available"] else 503, result)
            if path.path == "/api/bootstrap" and not path.query:
                value = {"csrf_token": self.server.csrf_token, "app": APP}
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
        except ProfileError as error:
            self.profile_error(error)
        except WorkspaceError as error:
            self.profile_error(error)
        except ValueError:
            self.reject(400, "invalid_request")
        except Exception:
            self.reject(503, "gateway_query_failed")

    def do_POST(self):
        self._post_body_pending = True
        path = urlsplit(self.path)
        if path.path.startswith('/api/agent/'):
            return self.agent_post(path)
        if path.path == '/api/workspace/agents/heartbeat':
            return self.agent_heartbeat(path)
        if not self.allowed(api=True, focus=True):
            return
        if AGENT_BROWSER_CANCEL_ROUTE.fullmatch(path.path):
            return self.agent_cancel(path)
        if (TASK_BRIEF_ROUTE.fullmatch(path.path) or TASK_DISPATCH_ROUTE.fullmatch(path.path)
                or DISPATCH_ROUTE.fullmatch(path.path)):
            return self.dispatch_post(path)
        if TASK_SESSIONS_ROUTE.fullmatch(path.path) or TASK_SESSION_ROUTE.fullmatch(path.path):
            return self.sessions_post(path)
        if TASK_MESSAGES_ROUTE.fullmatch(path.path) or MESSAGE_ROUTE.fullmatch(path.path):
            return self.messages_post(path)
        if path.path == '/api/workspace/news/refresh':
            if self.server.workspace is None:
                return self.reject(503, 'workspace_unavailable')
            try:
                if path.query:
                    raise ValueError('invalid_query')
                body = self.read_json_body(max_bytes=1024, strict=True)
                if set(body) != {'stream'} or body['stream'] not in ('ai', 'hn'):
                    raise ValueError('invalid_body')
                return self.reply(200, self.server.workspace.news(body['stream'], refresh=True))
            except BodyTooLarge:
                return self.reject(413, 'body_too_large')
            except (ValueError, TypeError, UnicodeError, OSError):
                return self.reject(400, 'invalid_request')
        if path.path in {"/api/workspace/observations/return", "/api/workspace/observations/window-choices",
                         "/api/workspace/observations/window-choices/close"}:
            return self.observation_post(path)
        if self.profile() is not None:
            if self.profile_post(path):
                return
        if path.path == "/api/workspace/execution/runs" or EXECUTION_ACTION_ROUTE.fullmatch(path.path):
            if path.query:
                return self.reject(400, "invalid_request")
            try:
                body = self.read_json_body(max_bytes=24576 if path.path == "/api/workspace/execution/runs" else 4096)
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
                body = self.read_json_body()
            except (ValueError, UnicodeDecodeError, OSError):
                return self.reject(400, "invalid_request")
            return self.workspace_reply(path.path, body)
        if path.path == "/api/progress/set":
            if path.query:
                return self.reject(400, "invalid_request")
            try:
                body = self.read_json_body()
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
                body = self.read_json_body()
                if set(body) != {"id"} or not isinstance(body["id"], str):
                    raise ValueError("invalid_body")
                launched = self.server.launchers.start(body["id"]) if self.server.launchers is not None else start(body["id"])
                if not launched:
                    reason = self.server.launchers.last_error if self.server.launchers is not None else None
                    known = {"unknown_launcher", "not_configured", "unsupported_platform", "missing", "launch_failed",
                             "unsafe_batch_path", "unsupported_target_type", "invalid_target"}
                    return self.reject(400, reason if reason in known else "invalid_request")
            except (ValueError, UnicodeDecodeError, OSError):
                return self.reject(400, "invalid_request")
            return self.reply(200, {"launchers": self.server.launchers.catalog() if self.server.launchers is not None else catalog()})
        if path.path in {"/api/todos", "/api/todos/toggle"}:
            if path.query:
                return self.reject(400, "invalid_request")
            try:
                body = self.read_json_body()
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

    def observation_post(self, path):
        return observation_routes.handle_post(self, path)

    def workspace_reply(self, path, body=None, *, stream='ai'):
        workspace = self.server.workspace
        if workspace is None:
            return self.reject(503, "workspace_unavailable")
        try:
            if path == "/api/workspace":
                value = workspace.snapshot()
            elif path == "/api/workspace/usage":
                value = workspace.usage()
            elif path == "/api/workspace/news":
                value = workspace.news() if stream == 'ai' else workspace.news(stream)
            elif path == "/api/workspace/usage/refresh":
                if body != {}:
                    raise ValueError("invalid_request")
                value = workspace.usage(refresh=True)
            elif path == "/api/workspace/records":
                value = workspace.save_record(body)
            else:
                value = workspace.save_task(body, update=path == "/api/workspace/tasks/update")
            return self.reply(200, value)
        except ProfileError as error:
            return self.profile_error(error)
        except WorkspaceError as error:
            reason = str(error)
            if self.profile() is not None and reason in {"store_busy", "storage_error"}:
                return self.profile_error(error)
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

    def read_json_body(self, max_bytes=4096, *, strict=False):
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get_all("Transfer-Encoding", []) or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,10}", lengths[0])
                or self.headers.get_all("Content-Type", []) != ["application/json"]):
            raise ValueError("invalid_body")
        length = int(lengths[0])
        if length > max_bytes and strict:
            raise BodyTooLarge("body_too_large")
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
        value = strict_json(data) if strict else json.loads(data, parse_constant=invalid_json_constant)
        if not isinstance(value, dict):
            raise ValueError("invalid_body")
        return value

    def profile_error(self, error):
        if isinstance(error, ProfileError):
            reason, status = error.reason, error.status
            extra = {key: value for key, value in (("field", error.field), ("message", error.message)) if value is not None}
        else:
            reason = str(error)
            status = (404 if reason in {"item_not_found", "task_not_found"} else
                      409 if reason in {"store_busy", "item_changed", "list_changed", "order_exhausted", "revision_exhausted",
                                        "session_not_in_sample", "session_project_mismatch"} else
                      422 if reason in {"invalid_item", "text_budget"} else 500)
            reason = reason if reason in {"store_busy", "storage_error", "schema_error", "item_not_found", "task_not_found",
                "item_changed", "list_changed", "order_exhausted", "revision_exhausted", "session_not_in_sample",
                "session_project_mismatch", "invalid_item", "text_budget"} else "storage_error"
            extra = {}
        self._discard_rejected_post_body()
        self.reply(status, {"status": "unknown", "reason": reason, "source": self.server.__dict__.get("source"),
                            "observed_at": datetime.now(timezone.utc).isoformat(), **extra})

    def _agent_seen(self, identity):
        seen = getattr(self.server, "agent_seen", None)
        if seen is not None:
            seen(identity)
            return
        presence = getattr(self.server, "agent_presence", None)
        if presence is None:
            presence = {}
            self.server.agent_presence = presence
        presence[identity] = time.time()

    def _agent_last_seen(self, identity):
        last = getattr(self.server, "agent_last_seen", None)
        if last is not None:
            return last(identity)
        return getattr(self.server, "agent_presence", {}).get(identity)

    def agent_heartbeat(self, path):
        if path.query:
            return self.reject(400, "invalid_request")
        if self.headers.get_all("Host", []) != [self.server.expected_host]:
            return self.reject(403, "host_rejected")
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [self.server.origin]:
            return self.reject(403, "origin_rejected")
        profile = self.profile()
        if profile is None:
            return self.reject(404, "route_not_found")
        authorization = self.headers.get("Authorization", "")
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            return self.reject(401, "agent_unauthorized")
        token = authorization[7:]
        try:
            body = self.read_json_body(max_bytes=1024, strict=True)
        except BodyTooLarge:
            return self.reject(413, "body_too_large")
        except (ValueError, TypeError, UnicodeError, OSError):
            return self.reject(400, "invalid_request")
        if set(body) != {"id"} or type(body["id"]) is not str:
            return self.reject(400, "invalid_request")
        agent = profile.agent_heartbeat(token)
        if agent is None or agent["id"] != body["id"]:
            return self.reject(401, "agent_unauthorized")
        self._agent_seen(agent["id"])
        return self.reply(200, {"status": "ok", "agent_id": agent["id"],
                                "last_seen_at": datetime.now(timezone.utc).isoformat()})

    def authenticated_agent(self):
        if self.headers.get_all("Host", []) != [self.server.expected_host]:
            self.reject(403, "host_rejected")
            return None
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [self.server.origin]:
            self.reject(403, "origin_rejected")
            return None
        profile = self.profile()
        if profile is None:
            self.reject(404, "route_not_found")
            return None
        authorization = self.headers.get("Authorization", "")
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            self.reject(401, "agent_unauthorized")
            return None
        agent = profile.agent_by_token(authorization[7:])
        if agent is None:
            self.reject(401, "agent_unauthorized")
            return None
        self._agent_seen(agent["id"])
        return agent

    def agent_error(self, error):
        reason = str(error)
        status = (404 if reason in {"task_not_found", "run_not_found"} else
                  403 if reason in {"not_assigned", "project_forbidden"} else
                  409 if reason in {"run_conflict", "run_mismatch", "item_changed", "review_not_ready",
                                    "review_already_submitted", "execution_managed_run", "execution_link_conflict"} else 422)
        self.reject(status, reason if reason in {
            "task_not_found", "run_not_found", "run_mismatch", "not_assigned", "project_forbidden", "run_conflict",
            "item_changed", "review_not_ready", "review_already_submitted", "invalid_agent_request",
            "execution_managed_run", "execution_link_conflict"} else "invalid_agent_request")

    def agent_get(self, path):
        agent = self.authenticated_agent()
        if agent is None:
            return
        try:
            if path.path == "/api/agent/tasks":
                values = parse_qs(path.query, keep_blank_values=True, strict_parsing=True)
                if set(values) - {"state"} or any(len(value) != 1 for value in values.values()):
                    raise ValueError("invalid_query")
                state = values.get("state", [None])[0]
                return self.reply(200, self.server.workspace.list_agent_tasks(
                    agent["id"], state, agent.get("project_ids")))
            match = AGENT_TASK_ROUTE.fullmatch(path.path)
            if not match or path.query:
                raise ValueError("invalid_query")
            task_id = canonical_uuid(match[1])
            if match[2] in (None, "review"):
                value = self.server.workspace.get_agent_task(agent["id"], task_id, agent.get("project_ids"))
                return self.reply(200, value if match[2] is None else
                                  {"item": value["item"], "run": value["run"], "reviews": value["reviews"],
                                   "connection_state": value["connection_state"],
                                   "last_contact_at": value["last_contact_at"]})
            return self.reject(404, "route_not_found")
        except WorkspaceError as error:
            self.agent_error(error)
        except (ValueError, TypeError):
            self.reject(400, "invalid_request")

    def agent_post(self, path):
        agent = self.authenticated_agent()
        if agent is None:
            return
        if path.query:
            return self.reject(400, "invalid_request")
        match = AGENT_TASK_ROUTE.fullmatch(path.path)
        if not match or match[2] not in ("claim", "progress", "submit-review"):
            return self.reject(404, "route_not_found")
        try:
            body = self.read_json_body(max_bytes=16384, strict=True)
            task_id = canonical_uuid(match[1])
            if match[2] == "claim":
                if set(body) != {"expected_revision"}:
                    raise ValueError("invalid_request")
                value = self.server.workspace.claim_agent_task(
                    agent["id"], task_id, body["expected_revision"], agent.get("project_ids"))
            elif match[2] == "progress":
                if (not {"expected_revision", "run_id", "stage"} <= set(body)
                        or set(body) - {"expected_revision", "run_id", "stage", "percent", "message",
                                        "wait_kind", "request_id", "agent_hint"}):
                    raise ValueError("invalid_request")
                value = self.server.workspace.progress_agent_task(
                    agent["id"], task_id, body["expected_revision"], body["stage"],
                    body.get("percent"), body.get("message"), agent.get("project_ids"),
                    body.get("wait_kind"), body.get("request_id"), body.get("agent_hint"), body["run_id"])
            else:
                if (not {"expected_revision", "run_id", "summary"} <= set(body)
                        or set(body) - {"expected_revision", "run_id", "summary", "evidence"}):
                    raise ValueError("invalid_request")
                value = self.server.workspace.submit_agent_review(
                    agent["id"], task_id, body["expected_revision"], body["summary"], body.get("evidence"),
                    agent.get("project_ids"), body["run_id"])
            return self.reply(200, value)
        except WorkspaceError as error:
            self.agent_error(error)
        except BodyTooLarge:
            self.reject(413, "body_too_large")
        except (ValueError, TypeError, UnicodeError):
            self.reject(400, "invalid_request")

    def messages_post(self, path):
        if path.query:
            return self.reject(400, "invalid_request")
        if self.profile() is None:
            return self.reject(404, "route_not_found")
        try:
            body = self.read_json_body(max_bytes=20480, strict=True)
            task_match = TASK_MESSAGES_ROUTE.fullmatch(path.path)
            message_match = MESSAGE_ROUTE.fullmatch(path.path)
            if task_match:
                value = self.server.workspace.create_task_message(canonical_uuid(task_match[1]), body)
            elif message_match and message_match[2] == "sent":
                if body:
                    raise ValueError("invalid_request")
                value = {"status": "sent", "message": self.server.workspace.mark_message_sent(
                    canonical_uuid(message_match[1]))}
            elif message_match and message_match[2] == "cancel":
                if body:
                    raise ValueError("invalid_request")
                value = {"status": "cancelled", "message": self.server.workspace.cancel_task_message(
                    canonical_uuid(message_match[1]))}
            else:
                return self.reject(404, "route_not_found")
            return self.reply(200, value)
        except BodyTooLarge:
            self.reject(413, "body_too_large")
        except (ProfileError, WorkspaceError) as error:
            self.profile_error(error)
        except (ValueError, TypeError, UnicodeError):
            self.reject(400, "invalid_request")

    def sessions_post(self, path):
        if path.query:
            return self.reject(400, "invalid_request")
        if self.profile() is None:
            return self.reject(404, "route_not_found")
        try:
            body = self.read_json_body(max_bytes=16384, strict=True)
            task_match = TASK_SESSIONS_ROUTE.fullmatch(path.path)
            session_match = TASK_SESSION_ROUTE.fullmatch(path.path)
            if task_match:
                value = self.server.workspace.link_task_session(canonical_uuid(task_match[1]), body)
            elif session_match and path.path.endswith("/unlink"):
                value = self.server.workspace.unlink_task_session(canonical_uuid(session_match[1]), body)
            else:
                return self.reject(404, "route_not_found")
            return self.reply(200, value)
        except BodyTooLarge:
            self.reject(413, "body_too_large")
        except (ProfileError, WorkspaceError) as error:
            self.profile_error(error)
        except (ValueError, TypeError, UnicodeError):
            self.reject(400, "invalid_request")

    def dispatch_post(self, path):
        if path.query:
            return self.reject(400, "invalid_request")
        if self.profile() is None:
            return self.reject(404, "route_not_found")
        try:
            body = self.read_json_body(max_bytes=65536, strict=True)
            brief = TASK_BRIEF_ROUTE.fullmatch(path.path)
            task_dispatch = TASK_DISPATCH_ROUTE.fullmatch(path.path)
            dispatch_action = DISPATCH_ROUTE.fullmatch(path.path)
            if brief:
                task_id = canonical_uuid(brief[1])
                value = self.server.workspace.save_task_brief(task_id, body)
            elif task_dispatch:
                task_id = canonical_uuid(task_dispatch[1])
                value = (self.server.workspace.preview_task_dispatch(task_id, body) if task_dispatch[2]
                         else self.server.workspace.create_task_dispatch(task_id, body))
            elif dispatch_action and dispatch_action[2] in ("sent", "cancel"):
                dispatch_id = canonical_uuid(dispatch_action[1])
                if body:
                    raise ValueError("invalid_request")
                value = (self.server.workspace.mark_dispatch_sent(dispatch_id) if dispatch_action[2] == "sent"
                         else self.server.workspace.cancel_task_dispatch(dispatch_id))
            elif dispatch_action and dispatch_action[2] == "execute":
                dispatch_id = canonical_uuid(dispatch_action[1])
                execution = getattr(self.server, "execution", None)
                if execution is None:
                    raise ProfileError("execution_unavailable", status=503)
                value = execution.submit_dispatch(dispatch_id, body)
                return self.reply(200 if value.get("replayed") else 202, value)
            else:
                return self.reject(404, "route_not_found")
            return self.reply(200, value)
        except BodyTooLarge:
            self.reject(413, "body_too_large")
        except (ProfileError, WorkspaceError) as error:
            self.profile_error(error)
        except (ValueError, TypeError, UnicodeError):
            self.reject(400, "invalid_request")

    def sandbox_preparation_post(self, path):
        if path.query:
            self.reject(400, "invalid_request")
            return True
        profile = self.profile()
        if profile is None:
            self.reject(404, "route_not_found")
            return True
        try:
            body = self.read_json_body(max_bytes=4096, strict=True)
            match = PROJECT_SANDBOX_ROUTE.fullmatch(path.path)
            value = sandbox_routes.post(self.server.workspace, profile, match[1], body)
            self.reply(200, value)
        except BodyTooLarge:
            self.reject(413, "body_too_large")
        except ValueError as error:
            reason = str(error)
            self.reject(409 if reason in {"unsupported_directory", "unsupported_filesystem",
                                          "owner_mismatch", "acl_grant_unverified",
                                          "reparse_path", "reparse_ancestor", "preparation_stale",
                                          "acl_restore_failed", "acl_changed", "directory_changed",
                                          "project_busy"} else 400,
                        reason)
        except OSError as error:
            self.reject(409 if str(error) == "sandbox_busy" else 503,
                        "project_busy" if str(error) == "sandbox_busy" else "acl_unavailable")
        except (ProfileError, WorkspaceError) as error:
            self.profile_error(error)
        return True

    def agent_cancel(self, path):
        if path.query:
            return self.reject(400, "invalid_request")
        if self.profile() is None:
            return self.reject(404, "route_not_found")
        match = AGENT_BROWSER_CANCEL_ROUTE.fullmatch(path.path)
        try:
            body = self.read_json_body(max_bytes=4096, strict=True)
            if set(body) != {"expected_revision", "reason"}:
                raise ValueError("invalid_request")
            task_id = canonical_uuid(match[1])
            value = self.server.workspace.cancel_agent_run(task_id, body["expected_revision"], body["reason"])
            return self.reply(200, value)
        except WorkspaceError as error:
            self.agent_error(error)
        except BodyTooLarge:
            self.reject(413, "body_too_large")
        except (ValueError, TypeError, UnicodeError):
            self.reject(400, "invalid_request")

    def profile_get(self, path):
        profile, workspace = self.profile(), self.server.workspace
        route = path.path
        if route in onboarding_routes.GET_ROUTES:
            onboarding_routes.handle_get(self, path)
        elif route == "/api/bootstrap":
            if path.query:
                raise ValueError("invalid_query")
            self.reply(200, {"csrf_token": self.server.csrf_token, "app": APP, "capabilities": self.server.capabilities})
        elif route == "/api/profile":
            if path.query:
                raise ValueError("invalid_query")
            self.reply(200, profile.public(startup_integrations=self.server.startup_integrations))
        elif route in ("/api/profile/observations", "/api/profile/observations/candidates"):
            if path.query:
                raise ValueError("invalid_query")
            if route.endswith("/candidates"):
                observer = self.server.__dict__.get("observer")
                value = observer.request("list_installation_candidates", {}) if observer else {"status": "unavailable", "reason": "observer_unavailable"}
            else:
                value = workspace.observation_settings()
            self.reply(200, value)
        elif route == "/api/progress":
            if path.query:
                raise ValueError("invalid_query")
            self.reply(200, {"projects": workspace.profile_projects()})
        elif route == "/api/desk":
            if path.query:
                raise ValueError("invalid_query")
            todos = profile.store.list_todos()
            self.reply(200, {"launchers": self.server.launchers.catalog() if self.server.launchers is not None else [],
                             "pulse": [], "personal_launchers_unavailable": not self.server.capabilities["personal_launchers"],
                             "pulse_unavailable": True, "news": headlines(), "news_state": NewsSource().cached('hn'), "todos": todos["items"], "todos_total": todos["total"]})
        elif route == "/api/codex/threads":
            value = profile.codex_threads(codex_offset(path.query))
            self.reply(200 if value["available"] else 503, value)
        elif route == "/api/workspace/codex/sessions":
            try:
                params = page_params(path.query, extra=("project_id", "scope"), default=20)
                self.reply(200, workspace.page_codex_sessions(**params))
            except ValueError:
                raise ProfileError("invalid_query") from None
        elif route == "/api/workspace/items":
            try:
                params = page_params(path.query, extra=("project_id", "scope", "provider_id", "state", "list_revision",
                                                       "focus", "priority", "sort"))
                for key, allowed in (("focus", ("0", "1")), ("priority", ("0", "1", "2"))):
                    if key in params:
                        if params[key] not in allowed:
                            raise ValueError("invalid_query")
                        params[key] = int(params[key])
                if "list_revision" in params:
                    if not re.fullmatch(r"[0-9]{1,16}", params["list_revision"]):
                        raise ValueError("invalid_query")
                    params["list_revision"] = int(params["list_revision"])
                    if not 1 <= params["list_revision"] <= 9007199254740991:
                        raise ValueError("invalid_query")
                if params.get("scope", "all") not in ("all", "active", "archived", "orphan", "global", "attention"):
                    raise ValueError("invalid_query")
                self.reply(200, workspace.page_items(include_assignment=True, **params))
            except ValueError:
                raise ProfileError("invalid_query") from None
        elif route in ("/api/workspace/decisions", "/api/workspace/reviews"):
            try:
                if route.endswith("/decisions"):
                    params = page_params(path.query, extra=("project_id", "status"))
                    value = workspace.page_decisions(**params)
                else:
                    params = page_params(path.query, extra=("project_id", "task_id", "verdict"))
                    value = workspace.page_reviews(**params)
                self.reply(200, value)
            except ProfileError:
                raise
            except ValueError:
                raise ProfileError("invalid_query") from None
        elif TASK_SESSIONS_ROUTE.fullmatch(route):
            if path.query:
                raise ValueError("invalid_query")
            match = TASK_SESSIONS_ROUTE.fullmatch(route)
            self.reply(200, workspace.task_sessions(canonical_uuid(match[1])))
        elif TASK_MESSAGES_ROUTE.fullmatch(route):
            if path.query:
                raise ValueError("invalid_query")
            match = TASK_MESSAGES_ROUTE.fullmatch(route)
            self.reply(200, workspace.task_messages(canonical_uuid(match[1])))
        elif SESSIONS_ROUTE.fullmatch(route):
            values = parse_qs(path.query, keep_blank_values=True, strict_parsing=True)
            if set(values) - {"project_id"} or any(len(value) != 1 for value in values.values()):
                raise ValueError("invalid_query")
            project_id = values.get("project_id", [None])[0]
            if project_id is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", project_id):
                raise ValueError("invalid_query")
            self.reply(200, workspace.list_task_sessions(project_id))
        elif PROJECT_SANDBOX_ROUTE.fullmatch(route):
            match = PROJECT_SANDBOX_ROUTE.fullmatch(route)
            self.reply(200, sandbox_routes.get(workspace, workspace.profile, match[1], path.query))
        elif TASK_BRIEF_ROUTE.fullmatch(route):
            if path.query:
                raise ValueError("invalid_query")
            match = TASK_BRIEF_ROUTE.fullmatch(route)
            self.reply(200, workspace.get_task_brief(canonical_uuid(match[1])))
        elif TASK_DISPATCHES_ROUTE.fullmatch(route):
            if path.query:
                raise ValueError("invalid_query")
            match = TASK_DISPATCHES_ROUTE.fullmatch(route)
            self.reply(200, workspace.task_dispatches(canonical_uuid(match[1])))
        elif DISPATCH_EXECUTION_CONTEXT_ROUTE.fullmatch(route):
            if path.query:
                raise ValueError("invalid_query")
            match = DISPATCH_EXECUTION_CONTEXT_ROUTE.fullmatch(route)
            self.reply(200, dispatch_execution_context(workspace, canonical_uuid(match[1])))
        elif DISPATCH_ROUTE.fullmatch(route) and DISPATCH_ROUTE.fullmatch(route)[2] is None:
            if path.query:
                raise ValueError("invalid_query")
            match = DISPATCH_ROUTE.fullmatch(route)
            self.reply(200, workspace.task_dispatch(canonical_uuid(match[1])))
        elif route == "/api/workspace/agent-adapters":
            if path.query:
                raise ValueError("invalid_query")
            self.reply(200, agent_adapters.catalog())
        elif AGENT_BROWSER_TASK_ROUTE.fullmatch(route):
            if path.query:
                raise ValueError("invalid_query")
            match = AGENT_BROWSER_TASK_ROUTE.fullmatch(route)
            self.reply(200, workspace.agent_task_detail(canonical_uuid(match[1])))
        elif route == "/api/workspace/agents":
            if path.query:
                raise ValueError("invalid_query")
            value = profile.agent_settings()
            now = time.time()
            for item in value["items"]:
                last = self._agent_last_seen(item["id"])
                item["online"] = bool(last is not None and now - last <= 60)
                item["last_seen_at"] = (datetime.fromtimestamp(last, timezone.utc).isoformat()
                                        if last is not None else None)
            self.reply(200, value)
        elif route == "/api/workspace/tasks":
            params = page_params(path.query, extra=("project_id", "scope"))
            if params.get("scope", "all") not in ("all", "active", "archived", "orphan"):
                raise ValueError("invalid_query")
            from control_everything.workspace.store import project_id_valid
            if "project_id" in params and not project_id_valid(params["project_id"]):
                raise ValueError("invalid_query")
            self.reply(200, workspace.page_tasks(**params))
        elif route == "/api/workspace/records":
            self.reply(200, workspace.page_records(**page_params(path.query)))
        elif route == "/api/workspace/todos":
            self.reply(200, profile.store.list_todos(**page_params(path.query, extra=("project_id",), default=100)))
        elif route == "/api/workspace/progress/history":
            params = page_params(path.query, extra=("project_id",), default=20)
            if "project_id" not in params:
                raise ValueError("invalid_query")
            self.reply(200, profile.store.manual_history(**params))
        elif route == "/api/workspace/compatibility":
            params = page_params(path.query, extra=("kind",), default=20)
            if params.get("kind", "projects") not in ("projects", "manual", "todos"):
                raise ValueError("invalid_query")
            self.reply(200, profile.store.compatibility(**params))
        elif optional_capabilities(route, "GET") is not None:
            if all(self.server.capabilities[name] for name in optional_capabilities(route, "GET")):
                return False
            self.reject(503, "capability_unavailable")
        else:
            return False
        return True

    def profile_post(self, path):
        route, profile = path.path, self.profile()
        required = optional_capabilities(route, "POST")
        if required is not None:
            if all(self.server.capabilities[name] for name in required):
                return False
            self.reject(503, "capability_unavailable")
            return True
        legacy = route in ("/api/progress/set", "/api/todos", "/api/todos/toggle")
        if PROJECT_SANDBOX_ROUTE.fullmatch(route):
            return self.sandbox_preparation_post(path)
        if route not in PROFILE_POST and not legacy:
            return False
        if path.query:
            self.reject(400, "invalid_request")
            return True
        try:
            body = self.read_json_body(max_bytes=262144 if route in ("/api/workspace/items", "/api/workspace/items/update",
                "/api/workspace/decisions", "/api/workspace/decisions/update", "/api/workspace/reviews") else
                65536 if route == "/api/profile/observations" or route == "/api/workspace/agents" else 16384 if route in PROFILE_POST else 4096, strict=route in PROFILE_POST)
            if route in onboarding_routes.POST_ROUTES:
                value = onboarding_routes.handle_post(self, route, body)
            elif route in ("/api/workspace/items", "/api/workspace/items/update"):
                value = self.server.workspace.save_item(body, update=route.endswith("/update"))
            elif route == "/api/workspace/agents":
                value = profile.save_agents(body)
            elif route == "/api/workspace/agents/token":
                value = profile.rotate_agent_token(body)
            elif route in ("/api/workspace/decisions", "/api/workspace/decisions/update"):
                value = self.server.workspace.save_decision(body, update=route.endswith("/update"))
            elif route == "/api/workspace/reviews":
                value = self.server.workspace.save_review(body)
            elif route == "/api/profile/settings":
                value = profile.settings(body)
                self.server.workspace.selection.invalidate()
                value["restart_required"] = self.server.startup_integrations is not None and profile.integrations() != self.server.startup_integrations
            elif route == "/api/profile/observations":
                value = self.server.workspace.save_observation_settings(body)
            elif route == "/api/profile/import":
                if set(body) != {"source_dir"}:
                    raise ProfileError("invalid_import", "source_dir")
                from control_everything.profiles.profile_import import import_legacy
                value = import_legacy(profile, body["source_dir"])
            elif route in ("/api/workspace/projects/save", "/api/workspace/projects/restore"):
                value = {"status": "ok", "project": profile.store.save_project(body, restore=route.endswith("/restore"))}
            elif route == "/api/workspace/projects/inspect":
                value = self.server.workspace.inspect_project_directory(body)
            elif route == "/api/workspace/projects/create-preview":
                value = self.server.workspace.preview_project_directory(body)
            elif route == "/api/workspace/projects/register":
                value = self.server.workspace.register_project_directory(body)
            elif route == "/api/workspace/projects/create":
                value = self.server.workspace.create_project_directory(body)
            elif route == "/api/workspace/directories/pick":
                value = self.server.workspace.pick_project_directory(body)
            elif route in ("/api/workspace/progress", "/api/progress/set"):
                value = profile.store.save_manual(body)
                if legacy:
                    value = {"projects": self.server.workspace.profile_projects()}
            else:
                value = profile.store.save_todo(body, toggle=route.endswith("/toggle"))
            self.reply(200, value)
        except BodyTooLarge:
            self.reject(413, "body_too_large")
        except project_dirs.DirectoryError as error:
            self.reply(error.status, {"status": "error", "reason": error.reason,
                                      "source": self.server.__dict__.get("source"),
                                      "observed_at": datetime.now(timezone.utc).isoformat()})
        except (ProfileError, WorkspaceError) as error:
            self.profile_error(error)
        except (ValueError, TypeError, UnicodeError, OSError, RecursionError):
            self.reject(400, "invalid_request")
        return True


class PanelServer(ThreadingHTTPServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.agent_presence = {}
        self.agent_presence_lock = threading.Lock()

    def agent_seen(self, identity):
        with self.agent_presence_lock:
            self.agent_presence[identity] = time.time()

    def agent_last_seen(self, identity):
        with self.agent_presence_lock:
            return self.agent_presence.get(identity)

    def start_observer(self):
        observer = self.__dict__.get("observer")
        if observer is not None:
            version = product_version_dir()
            if version is None:
                observer.start()
            else:
                context = ProductContext.load(version)
                api = default_api()
                with api.install_mutex(context.install_root):
                    self._installation_lease = register_http(context, self.profile.data_dir, api=api)
                    observer.start()

    def server_close(self):
        try:
            source = self.__dict__.get('news_source')
            lease = self.__dict__.pop('_news_lease', None)
            if source is not None and lease is not None:
                source.release(lease)
            observer = self.__dict__.get("observer")
            if observer is not None:
                observer.close_client()
            execution = getattr(self, "execution", None)
            self.execution = None
            if execution is not None:
                execution.close()
        finally:
            try:
                installation = self.__dict__.pop('_installation_lease', None)
                if installation is not None:
                    installation.close()
            finally:
                super().server_close()

    def server_bind(self):
        if sys.platform == "win32":
            # Windows SO_REUSEADDR can share a listener. Reserve it before bind.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(service, address=(HOST, PORT), *, workspace=None, execution=None, profile=None,
                launchers=None, capabilities=None, startup_integrations=None, source=None, observer=None):
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
    server.workspace = workspace
    server.observer = observer
    server.execution = execution
    server.execution_reason = None
    server.profile = profile
    server.launchers = launchers
    server.capabilities = copy.deepcopy(capabilities if capabilities is not None else PROFILE_CAPABILITIES if profile is not None else None)
    server.startup_integrations = copy.deepcopy(startup_integrations)
    service_source = getattr(service, "source", None)
    server.source = copy.deepcopy(source if source is not None else service_source if isinstance(service_source, dict) else None)
    server.expected_host = f"{HOST}:{server.server_address[1]}"
    server.origin = f"http://{server.expected_host}"
    server.csrf_token = secrets.token_urlsafe(32)
    server.news_source = workspace.news_source if workspace is not None else NewsSource()
    server._news_lease = server.news_source.acquire()
    return server


def build_profile_server(data_dir=None, port=8766, *, quota_source=None, news_source=None):
    profile = Profile.open(data_dir)
    workspace = WorkspaceService(profile=profile, quota_source=quota_source, news_source=news_source)
    observer = ObserverClient(profile.data_dir, scope_supplier=workspace.observation_scope,
                              config_supplier=profile.observation_config, config_guard=profile.observation_guard)
    workspace.observer = observer
    agents = profile.agent_settings().get("items", [])
    codex_enabled = any(row.get("enabled") and row.get("adapter") == "codex" for row in agents)
    dsh_enabled = any(row.get("enabled") and row.get("adapter") == "dsh" for row in agents)
    if dsh_enabled:
        try:
            from control_everything.execution.dsh import native_dsh
            native_dsh()
        except Exception:
            dsh_enabled = False
    execution = None
    if dsh_enabled or codex_enabled:
        from control_everything.execution.service import ExecutionService
        execution = ExecutionService(workspace, codex_required=False, require_sandbox_acl=False)
    integrations = profile.integrations()
    enabled = {name: value is not None and value["enabled"] for name, value in integrations.items()}
    service, launchers = None, None
    if enabled["remote"]:
        from control_everything.sessions.service import RemoteClient, SessionService
        from control_everything.sessions.terminal import Terminal
        from control_everything.sessions.workbench import Workbench
        workbench = Workbench(config=integrations["workbench"], terminal_config=integrations["terminal"]) if enabled["workbench"] else None
        terminal = Terminal(config=integrations["terminal"], remote_config=integrations["remote"], workbench=workbench)
        remote = RemoteClient(config=integrations["remote"])
        service = SessionService(remote=remote, terminal=terminal)
    if enabled["launchers"]:
        launchers = LauncherCatalog(integrations["launchers"])
    capabilities = {**PROFILE_CAPABILITIES, "remote_sessions": enabled["remote"],
                    "terminal": enabled["terminal"], "workbench": enabled["workbench"],
                    "personal_launchers": enabled["launchers"], "execution": execution is not None}
    return make_server(service, (HOST, port), workspace=workspace, profile=profile, execution=execution,
                       launchers=launchers, capabilities=capabilities, startup_integrations=integrations, observer=observer)


def serve(data_dir=None, port=8766):
    server = None
    try:
        server = build_profile_server(data_dir, port)
        server.start_observer()
        print(f"工作台：http://{HOST}:{port}/workspace；项目进度：http://{HOST}:{port}/progress；profile：{server.profile.data_dir}。旧记录可在来源设置中显式导入；Ctrl+C 停止。", file=sys.stderr)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (RegistryError, ManifestError, InstallationError) as error:
        print(f"安装版本无法启动（{error.reason}）。请等待安装维护结束；若文件损坏，请保留资料并用可信安装器修复此版本。", file=sys.stderr)
        return 1
    except (OSError, ValueError, WorkspaceError) as error:
        print(f"工作台启动失败（{type(error).__name__}）。请检查profile配置或端口 {port}，可显式选择另一端口。", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.server_close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="本机项目工作台")
    parser.add_argument("--data-dir", default=None)
    def port_number(value):
        number = int(value)
        if not 1024 <= number <= 65535:
            raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
        return number
    parser.add_argument("--port", type=port_number, default=8766)
    args = parser.parse_args(argv)
    return serve(data_dir=args.data_dir, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
