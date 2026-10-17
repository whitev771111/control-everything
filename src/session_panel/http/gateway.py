"""Loopback HTTP surface for one shared SessionService."""

import json
import copy
import argparse
import re
import secrets
import socket
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from session_panel.paths import web_root
from urllib.parse import parse_qs, urlsplit

from session_panel.sessions.source import HOST, PORT, canonical_uuid
from session_panel.sessions.codex_index import list_threads as list_codex_threads
from session_panel.sessions.observer_client import ObserverClient
from session_panel.http import observation_routes
from session_panel.workspace.progress import MANUAL, REGISTRY, TODOS, add_todo, board, set_progress, toggle_todo, load_todos
from session_panel.integrations.launchers import LauncherCatalog, catalog, start
from session_panel.integrations.desk import headlines, pulse
from session_panel.workspace.service import WorkspaceService
from session_panel.workspace.news_source import NewsSource
from session_panel.workspace.store import WorkspaceError
from session_panel.profiles.profile import Profile, ProfileError, strict_json

APP = "remote-claude-session-panel"
WEB = web_root()
SESSION_ROUTE = re.compile(r"/api/sessions/([^/]+)(?:/(messages|focus))?\Z")
EXECUTION_RUN_ROUTE = re.compile(r"/api/workspace/execution/runs/([^/]+)\Z")
EXECUTION_ACTION_ROUTE = re.compile(r"/api/workspace/execution/runs/([^/]+)/(cancel|approvals/([^/]+))\Z")
PROFILE_POST = {"/api/profile/settings", "/api/profile/import", "/api/profile/observations", "/api/workspace/projects/save",
                "/api/workspace/projects/restore", "/api/workspace/progress",
                "/api/workspace/items", "/api/workspace/items/update"}
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
            "/workspace": ("workspace/workspace.html", "text/html; charset=utf-8"),
            "/workspace.js": ("workspace/workspace.js", "text/javascript; charset=utf-8"),
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
        if not self.allowed(api=True, focus=True):
            return
        path = urlsplit(self.path)
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
            status = (404 if reason == "item_not_found" else
                      409 if reason in {"store_busy", "item_changed", "list_changed", "order_exhausted", "revision_exhausted",
                                        "session_not_in_sample", "session_project_mismatch"} else
                      422 if reason in {"invalid_item", "text_budget"} else 500)
            reason = reason if reason in {"store_busy", "storage_error", "schema_error", "item_not_found", "item_changed",
                "list_changed", "order_exhausted", "revision_exhausted", "session_not_in_sample", "session_project_mismatch",
                "invalid_item", "text_budget"} else "storage_error"
            extra = {}
        self._discard_rejected_post_body()
        self.reply(status, {"status": "unknown", "reason": reason, "source": self.server.__dict__.get("source"),
                            "observed_at": datetime.now(timezone.utc).isoformat(), **extra})

    def profile_get(self, path):
        profile, workspace = self.profile(), self.server.workspace
        route = path.path
        if route == "/api/bootstrap":
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
        elif route == "/api/workspace/items":
            try:
                params = page_params(path.query, extra=("project_id", "scope", "provider_id", "state", "list_revision"))
                if "list_revision" in params:
                    if not re.fullmatch(r"[0-9]{1,16}", params["list_revision"]):
                        raise ValueError("invalid_query")
                    params["list_revision"] = int(params["list_revision"])
                    if not 1 <= params["list_revision"] <= 9007199254740991:
                        raise ValueError("invalid_query")
                if params.get("scope", "all") not in ("all", "active", "archived", "orphan", "global"):
                    raise ValueError("invalid_query")
                self.reply(200, workspace.page_items(**params))
            except ValueError:
                raise ProfileError("invalid_query") from None
        elif route == "/api/workspace/tasks":
            params = page_params(path.query, extra=("project_id", "scope"))
            if params.get("scope", "all") not in ("all", "active", "archived", "orphan"):
                raise ValueError("invalid_query")
            from session_panel.workspace.store import project_id_valid
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
        if route not in PROFILE_POST and not legacy:
            return False
        if path.query:
            self.reject(400, "invalid_request")
            return True
        try:
            body = self.read_json_body(max_bytes=262144 if route in ("/api/workspace/items", "/api/workspace/items/update") else
                65536 if route == "/api/profile/observations" else 16384 if route in PROFILE_POST else 4096, strict=route in PROFILE_POST)
            if route in ("/api/workspace/items", "/api/workspace/items/update"):
                value = self.server.workspace.save_item(body, update=route.endswith("/update"))
            elif route == "/api/profile/settings":
                value = profile.settings(body)
                value["restart_required"] = self.server.startup_integrations is not None and profile.integrations() != self.server.startup_integrations
            elif route == "/api/profile/observations":
                value = self.server.workspace.save_observation_settings(body)
            elif route == "/api/profile/import":
                if set(body) != {"source_dir"}:
                    raise ProfileError("invalid_import", "source_dir")
                from session_panel.profiles.profile_import import import_legacy
                value = import_legacy(profile, body["source_dir"])
            elif route in ("/api/workspace/projects/save", "/api/workspace/projects/restore"):
                value = {"status": "ok", "project": profile.store.save_project(body, restore=route.endswith("/restore"))}
            elif route in ("/api/workspace/progress", "/api/progress/set"):
                value = profile.store.save_manual(body)
                if legacy:
                    value = {"projects": self.server.workspace.profile_projects()}
            else:
                value = profile.store.save_todo(body, toggle=route.endswith("/toggle"))
            self.reply(200, value)
        except BodyTooLarge:
            self.reject(413, "body_too_large")
        except (ProfileError, WorkspaceError) as error:
            self.profile_error(error)
        except (ValueError, TypeError, UnicodeError, OSError, RecursionError):
            self.reject(400, "invalid_request")
        return True


class PanelServer(ThreadingHTTPServer):
    def start_observer(self):
        observer = self.__dict__.get("observer")
        if observer is not None:
            observer.start()

    def server_close(self):
        source = self.__dict__.get('news_source')
        lease = self.__dict__.pop('_news_lease', None)
        if source is not None and lease is not None:
            source.release(lease)
        observer = self.__dict__.get("observer")
        if observer is not None:
            observer.close_client()
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
    integrations = profile.integrations()
    enabled = {name: value is not None and value["enabled"] for name, value in integrations.items()}
    service, launchers = None, None
    if enabled["remote"]:
        from session_panel.sessions.service import RemoteClient, SessionService
        from session_panel.sessions.terminal import Terminal
        from session_panel.sessions.workbench import Workbench
        workbench = Workbench(config=integrations["workbench"], terminal_config=integrations["terminal"]) if enabled["workbench"] else None
        terminal = Terminal(config=integrations["terminal"], remote_config=integrations["remote"], workbench=workbench)
        remote = RemoteClient(config=integrations["remote"])
        service = SessionService(remote=remote, terminal=terminal)
    if enabled["launchers"]:
        launchers = LauncherCatalog(integrations["launchers"])
    capabilities = {**PROFILE_CAPABILITIES, "remote_sessions": enabled["remote"],
                    "terminal": enabled["terminal"], "workbench": enabled["workbench"],
                    "personal_launchers": enabled["launchers"]}
    return make_server(service, (HOST, port), workspace=workspace, profile=profile,
                       launchers=launchers, capabilities=capabilities, startup_integrations=integrations, observer=observer)


def serve(data_dir=None, port=8766):
    try:
        server = build_profile_server(data_dir, port)
    except (OSError, ValueError, WorkspaceError) as error:
        print(f"工作台启动失败（{type(error).__name__}）。请检查profile配置或端口 {port}，可显式选择另一端口。", file=sys.stderr)
        return 1
    print(f"工作台：http://{HOST}:{port}/workspace；项目进度：http://{HOST}:{port}/progress；profile：{server.profile.data_dir}。旧记录可在来源设置中显式导入；Ctrl+C 停止。", file=sys.stderr)
    server.start_observer()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
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
