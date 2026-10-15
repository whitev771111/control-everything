"""Local workspace projections and validation; never dispatches an agent task."""

import math
import ntpath
import re
import shutil
from datetime import date, datetime, timedelta, timezone

from . import progress
from .codex_index import list_threads, unavailable
from .source import canonical_uuid
from .workspace_sources import CodexQuotaSource, NewsSource
from .workspace_store import WorkspaceError, WorkspaceStore

DEFAULT_DB = progress.REGISTRY.with_name("workspace-state.sqlite3")
BEIJING = timezone(timedelta(hours=8))
PROVIDERS = ("codex", "claude", "grok", "dsh", "opencode")
TASK_STATES = ("todo", "doing", "blocked", "review", "done")
TASK_FIELDS = {"title", "project_id", "provider_id", "state", "session_key"}
RECORD_FIELDS = {"id", "provider_id", "product", "day", "metric", "value", "currency", "window_label", "note"}
PRODUCTS = {"codex": ("subscription",), "claude": ("subscription",),
            "grok": ("subscription", "cli", "api"), "dsh": ("local",), "opencode": ("local",)}


def today():
    return datetime.now(BEIJING).date().isoformat()


def finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def windows_path(value):
    value = ntpath.normcase(ntpath.normpath(value))
    if value.startswith("\\\\?\\unc\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\") and re.match(r"[a-z]:\\", value[4:]):
        return value[4:]
    return value


def inside_project(cwd, root):
    if not isinstance(cwd, str) or not ntpath.isabs(cwd):
        return False
    try:
        cwd, root = windows_path(cwd), windows_path(root)
        return ntpath.commonpath((cwd, root)) == root
    except ValueError:
        return False


def discover_providers():
    definitions = (
        ("codex", "Codex", ("codex",), "/", "会话与小院", "本机身份元数据；活动和任务完成状态未知"),
        ("claude", "Claude Code", ("claude",), "/", "原会话页", "原会话页远端观察，当前连接需在那里核验"),
        ("grok", "Grok", ("grok",), "https://grok.com/", "Grok 原服务", "结构化会话观察未接入；订阅、CLI、API 分开记录"),
        ("dsh", "DSH", ("dsh", "dsh-tui", "dst"), "https://github.com/deepseek-ai/deepseek-harness", "官方文档", "结构化会话观察与执行未接入"),
        ("opencode", "OpenCode", ("opencode",), None, None, "结构化会话观察与执行未接入"),
    )
    result = []
    for provider_id, name, candidates, href, label, note in definitions:
        try:
            commands = [command for command in candidates if shutil.which(command)]
            discovery = {"status": "found" if commands else "not_found", "commands": commands,
                         "reason": None if commands else "cli_not_found"}
        except OSError:
            discovery = {"status": "unknown", "commands": [], "reason": "discovery_unavailable"}
        result.append({"id": provider_id, "name": name, "discovery": discovery,
                       "capabilities": {"observe_sessions": provider_id in ("codex", "claude"),
                                        "open_existing_ui": provider_id in ("codex", "claude", "grok"),
                                        "dispatch": False, "cancel": False, "approve": False,
                                        "read_quota": provider_id == "codex"},
                       "observation_note": note,
                       "entrypoints": [{"label": label, "href": href}] if href else []})
    return result


class WorkspaceService:
    def __init__(self, db_path=DEFAULT_DB, *, registry_path=progress.REGISTRY,
                 manual_path=progress.MANUAL, todos_path=progress.TODOS,
                 session_reader=None, quota_source=None, news_source=None):
        self.store = WorkspaceStore(db_path)
        self.registry_path, self.manual_path, self.todos_path = registry_path, manual_path, todos_path
        self.session_reader = session_reader or list_threads
        self.quota_source = quota_source or CodexQuotaSource()
        self.news_source = news_source or NewsSource()

    def sessions(self):
        try:
            return self.session_reader()
        except Exception:
            return unavailable(0)

    def registered_projects(self):
        try:
            return progress.load_registry(self.registry_path)
        except (OSError, ValueError, TypeError):
            raise WorkspaceError("workspace_unavailable") from None

    def projects(self, registry):
        try:
            saved, source_available = progress.load_manual(self.manual_path), True
        except (OSError, ValueError, TypeError):
            saved, source_available = {}, False
        codex, grok = progress.codex_cwds(), progress.grok_cwds()
        items = []
        for project in registry:
            raw = saved.get(project["id"])
            recorded = (isinstance(raw, dict) and raw.get("state") in progress.STATES
                        and finite_number(raw.get("percent")) and 0 <= raw["percent"] <= 100)
            item = raw if isinstance(raw, dict) else {}
            records = progress.latest_focus(project["root"])
            items.append({"id": project["id"], "name": project["name"],
                          "state": item.get("state") if item.get("state") in progress.STATES else "未开始",
                          "note": item.get("note", "").strip() if isinstance(item.get("note"), str) else "",
                          "percent": item["percent"] if recorded else None,
                          "manual_recorded": recorded,
                          "manual_reason": (None if recorded else "source_unavailable" if not source_available else
                                            "invalid_record" if project["id"] in saved else "not_recorded"),
                          "records": records if isinstance(records, list) else [],
                          "records_available": isinstance(records, list),
                          "sessions": progress.session_hits(project["root"], codex, grok)})
        return items

    def link_state(self, task, sessions, registry):
        if task["session_key"] is None:
            return "none"
        session = next((row for row in sessions["items"] if row["session_key"] == task["session_key"]), None)
        if session is None or not sessions["available"]:
            return "not_in_current_sample"
        project = next((row for row in registry if row["id"] == task["project_id"]), None)
        return "verified" if project and inside_project(session["cwd"], project["root"]) else "project_mismatch"

    def snapshot(self):
        sessions = self.sessions()
        try:
            registry = self.registered_projects()
            projects = {"status": "available", "reason": None, "items": self.projects(registry)}
        except (WorkspaceError, OSError, ValueError, TypeError):
            registry = []
            projects = {"status": "unavailable", "reason": "projects_unavailable", "items": []}
        try:
            todos = {"status": "available", "reason": None, "items": progress.load_todos(self.todos_path)}
        except (OSError, ValueError, TypeError):
            todos = {"status": "unavailable", "reason": "todos_unavailable", "items": []}
        try:
            rows, total = self.store.list_tasks()
            tasks = {"status": "available", "reason": None, "total": total, "limit": 500,
                     "items": [{**row, "session_link_state": self.link_state(row, sessions, registry)} for row in rows]}
        except WorkspaceError:
            tasks = {"status": "unavailable", "reason": "storage_unavailable", "items": [], "total": None, "limit": 500}
        return {"status": "ok", "sampled_at": datetime.now(timezone.utc).isoformat(), "today": today(),
                "projects": projects, "todos": todos, "tasks": tasks, "providers": discover_providers(),
                "sessions": sessions, "entrypoints": [
                    {"id": "sessions", "label": "会话与小院", "href": "/"},
                    {"id": "progress", "label": "原进度、待办与启动器", "href": "/progress"},
                    {"id": "grok", "label": "Grok 原服务", "href": "https://grok.com/"}]}

    def save_task(self, body, *, update=False):
        if not isinstance(body, dict) or set(body) != TASK_FIELDS | ({"id"} if update else set()):
            raise ValueError("invalid_request")
        title, project_id, provider_id, state, key = (body[name] for name in
                                                     ("title", "project_id", "provider_id", "state", "session_key"))
        if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 160
                or not isinstance(project_id, str) or provider_id not in (*PROVIDERS, None)
                or state not in TASK_STATES):
            raise ValueError("invalid_request")
        if key is not None:
            if not isinstance(key, str) or not key.startswith("codex:") or provider_id != "codex":
                raise ValueError("invalid_request")
            canonical_uuid(key[6:])
        task_id = canonical_uuid(body["id"]) if update else None
        existing = self.store.get_task(task_id) if update else None
        registry = self.registered_projects()
        project = next((row for row in registry if row["id"] == project_id), None)
        if project is None:
            raise ValueError("invalid_request")
        sessions = self.sessions()
        unchanged = existing and all(existing[name] == body[name] for name in ("project_id", "provider_id", "session_key"))
        if key is not None and not unchanged:
            session = next((row for row in sessions["items"] if row["session_key"] == key), None)
            if session is None or not sessions["available"]:
                raise WorkspaceError("session_not_in_sample")
            if not inside_project(session["cwd"], project["root"]):
                raise WorkspaceError("session_project_mismatch")
        task = self.store.save_task({**body, "title": title.strip()}, task_id)
        return {"status": "ok", "task": {**task, "session_link_state": self.link_state(task, sessions, registry)}}

    def save_record(self, body):
        if not isinstance(body, dict) or set(body) != RECORD_FIELDS:
            raise ValueError("invalid_request")
        if body["id"] is not None:
            canonical_uuid(body["id"])
        provider, product, metric, value = (body[key] for key in ("provider_id", "product", "metric", "value"))
        if (not isinstance(provider, str) or provider not in PROVIDERS or product not in PRODUCTS[provider]
                or metric not in ("quota_used_percent", "daily_tokens", "daily_cost")
                or not finite_number(value) or value < 0):
            raise ValueError("invalid_request")
        day = body["day"]
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or date.fromisoformat(day).isoformat() > today():
            raise ValueError("invalid_request")
        if not isinstance(body["note"], str) or len(body["note"]) > 120:
            raise ValueError("invalid_request")
        if metric == "quota_used_percent":
            if value > 100 or not isinstance(body["window_label"], str) or not 1 <= len(body["window_label"].strip()) <= 24:
                raise ValueError("invalid_request")
        elif body["window_label"] is not None:
            raise ValueError("invalid_request")
        if metric == "daily_tokens" and (value != int(value) or value > 9223372036854775807):
            raise ValueError("invalid_request")
        if (metric == "daily_cost" and body["currency"] not in ("CNY", "USD")
                or metric != "daily_cost" and body["currency"] is not None):
            raise ValueError("invalid_request")
        fields = dict(body)
        if metric == "daily_tokens":
            fields["value"] = int(value)
        elif metric == "daily_cost":
            fields["value"] = float(value)
        if metric == "quota_used_percent":
            fields["window_label"] = body["window_label"].strip()
        return {"status": "ok", "record": self.store.save_record(fields)}

    def usage(self, *, refresh=False):
        quota = self.quota_source.refresh() if refresh else self.quota_source.get_cached()
        try:
            records, total = self.store.list_records()
            status, reason = "available", None
        except WorkspaceError:
            records, total, status, reason = [], None, "unavailable", "storage_unavailable"
        return {"status": "ok", "codex": quota, "manual_records": records,
                "manual_status": status, "manual_reason": reason, "manual_total": total, "manual_limit": 100}

    def news(self):
        return self.news_source.get()
