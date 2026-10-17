"""Local workspace projections and validation; never dispatches an agent task."""

import re
import shutil
from datetime import date, datetime, timedelta, timezone

from control_everything.workspace import progress
from control_everything.sessions.codex_index import list_threads, unavailable
from control_everything.sessions.source import canonical_uuid
from control_everything.workspace.sources import CodexQuotaSource, NewsSource
from control_everything.input_validation import ProfileError
from control_everything.workspace.store import WorkspaceError, WorkspaceStore
from control_everything.workspace.validation import (
    finite_number, windows_path, inside_project, observation_owner, path_key, project_id_valid,
)

DEFAULT_DB = progress.REGISTRY.with_name("workspace-state.sqlite3")
BEIJING = timezone(timedelta(hours=8))
PROVIDERS = ("codex", "claude", "grok", "dsh", "opencode")
TASK_PROVIDERS = (*PROVIDERS, "hermes", "pi")
TASK_STATES = ("todo", "doing", "blocked", "review", "done")
TASK_FIELDS = {"title", "project_id", "provider_id", "state", "session_key"}
RECORD_FIELDS = {"id", "provider_id", "product", "day", "metric", "value", "currency", "window_label", "note"}
PRODUCTS = {"codex": ("subscription",), "claude": ("subscription",),
            "grok": ("subscription", "cli", "api"), "dsh": ("local",), "opencode": ("local",)}


def today():
    return datetime.now(BEIJING).date().isoformat()


def discover_providers(installations=None):
    definitions = (
        ("codex", "Codex", ("codex",), "/", "会话", "本机身份元数据；活动和任务完成状态未知"),
        ("claude", "Claude Code", ("claude",), "/", "原会话页", "原会话页远端观察，当前连接需在那里核验"),
        ("grok", "Grok", ("grok",), "https://grok.com/", "Grok 原服务", "结构化会话观察未接入；订阅、CLI、API 分开记录"),
        ("dsh", "DSH", ("dsh", "dsh-tui", "dst"), "https://github.com/deepseek-ai/deepseek-harness", "官方文档", "结构化会话观察与执行未接入"),
        ("opencode", "OpenCode", ("opencode",), None, None, "结构化会话观察与执行未接入"),
    )
    result = []
    for provider_id, name, candidates, href, label, note in definitions:
        if installations is not None:
            installation = next((item for item in installations if item["provider_id"] == provider_id), None)
            discovery = {"status": {"available": "found", "not_found": "not_found"}.get(installation["status"], "unknown") if installation else "unknown",
                         "commands": installation["commands"] if installation else [],
                         "reason": installation["reason"] if installation else "observation_unavailable"}
        else:
            discovery = None
        try:
            if discovery is None:
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
                 session_reader=None, quota_source=None, news_source=None, profile=None, observer=None):
        self.profile = profile
        self.observer = observer
        self.store = profile.store if profile is not None else WorkspaceStore(db_path)
        self.registry_path, self.manual_path, self.todos_path = registry_path, manual_path, todos_path
        self.session_reader = profile.codex_threads if profile is not None else session_reader or list_threads
        self.quota_source = quota_source or CodexQuotaSource()
        self.news_source = news_source or NewsSource()

    def sessions(self):
        try:
            return self.session_reader()
        except Exception:
            return unavailable(0)

    def observation_scope(self):
        from control_everything.workspace.observations import scope_roots
        return scope_roots(self.registered_projects())

    def observation_settings(self):
        value = self.profile.observation_settings()
        observed = self.observations()
        pair = (value["revision"], value["hash"])
        matching = (observed.get("config_revision"), observed.get("config_hash")) == pair
        value["application"] = {"status": observed["status"] if matching else "pending",
                                "reason": observed["reason"] if matching else "config_pending"}
        facts = {row["registration_id"]: row for row in observed.get("registrations", [])} if matching and observed["status"] == "available" else {}
        value["validation"] = [facts.get(row["id"], {"registration_id": row["id"], "status": "unknown",
                                "reason": "rebind_required" if row["binding_state"] == "rebind_required" else "config_pending",
                                "source": "profile_intent", "sampled_at_ms": None, "duplicate_of_id": None})
                               for row in value["registrations"]]
        return value

    def save_observation_settings(self, body):
        from control_everything.sessions.observer_client import ObserverClient
        observed = self.observation_settings()
        duplicates = {row["registration_id"] for row in observed["validation"] if row["reason"] == "duplicate_installation"}
        self.profile.save_observations(body, duplicate_ids=duplicates,
            on_commit=lambda value: ObserverClient.invalidate_profile(self.profile.data_dir, value))
        return self.observation_settings()

    def observations(self):
        return self._observations_with_projects()[0]

    def _observations_with_projects(self):
        from control_everything.workspace.observations import project_observations
        cached = self.observer.cached() if self.observer else {
            "status": "unavailable", "reason": "not_started", "snapshot": None, "scope_revision": 0, "scope_hash": ""}
        configured = {}
        if self.profile is not None:
            from control_everything.sessions import observation_settings
            try:
                current = self.profile.observation_config()
                configured = {row["id"]: row for row in current["registrations"]}
                cached["config_pair"] = tuple(observation_settings.pair(current).values())
            except ValueError:
                cached["config_pair"] = None
                cached["reason"] = "config_unavailable"
        registry = self.registered_projects() if self.profile is not None else []
        value = project_observations(cached, registry)
        if self.profile is not None:
            for row in value["registrations"]:
                intent = configured.get(row["registration_id"], {})
                row.update(label=intent.get("label", ""), category=intent.get("category"),
                           enabled=intent.get("enabled", False), binding_state=intent.get("binding_state"),
                           can_pick=(row["status"] == "available" and intent.get("enabled", False)
                                     and intent.get("binding_state") == "confirmed"
                                     and (intent.get("category") == "custom_exe" or intent.get("locator_kind") == "codex_desktop")))
            for instance in value["instances"]:
                instance["registration_labels"] = [configured[identity]["label"] for identity in instance["registration_ids"] if identity in configured]
        return value, registry

    def registered_projects(self):
        if self.profile is not None:
            return self.store.list_projects()
        try:
            return progress.load_registry(self.registry_path)
        except (OSError, ValueError, TypeError):
            raise WorkspaceError("workspace_unavailable") from None

    def projects(self, registry):
        if self.profile is not None:
            return self.profile_projects(registry)
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
        if self.profile is not None:
            return "verified" if project and observation_owner(session["cwd"], registry) == project["id"] else "project_mismatch"
        return "verified" if project and inside_project(session["cwd"], project["root"]) else "project_mismatch"

    def profile_projects(self, registry=None, sessions=None):
        from control_everything.workspace.project_progress import RequestBudget, observe
        registry = self.registered_projects() if registry is None else registry
        sessions = self.sessions() if sessions is None else sessions
        saved, counts, budget = self.store.manual_rows(), self.store.task_counts(), RequestBudget()
        result = []
        for project in registry:
            manual = saved.get(project["id"])
            observation = observe(project, budget)
            hits = [{"tool": "Codex", "place": row["cwd"], "href": "/workspace", "session_key": row["session_key"]}
                    for row in sessions["items"] if observation_owner(row["cwd"], registry) == project["id"]]
            result.append({**project, "manual_recorded": manual is not None,
                           "manual_reason": None if manual else "not_recorded",
                           "state": manual["state"] if manual else None,
                           "percent": manual["percent"] if manual else None,
                           "note": manual["note"] if manual else "", "updated_at": manual["updated_at"] if manual else None,
                           "observation": observation, "records": observation["records"],
                           "records_available": observation["status"] == "available", "sessions": hits,
                           "task_counts": counts.get(project["id"], dict.fromkeys(("total", *TASK_STATES), 0)),
                           "history_url": "/api/workspace/progress/history?project_id=" + project["id"]})
        return result

    def page_tasks(self, **params):
        rows, total = self.store.list_tasks(**params)
        sessions, registry = self.sessions(), self.registered_projects()
        return self._task_page(rows, total, sessions, registry,
                               offset=params.get("offset", 0), limit=params.get("limit", 50))

    def _task_page(self, rows, total, sessions, registry, *, offset=0, limit=50):
        return {"items": [{**row, "session_link_state": self.link_state(row, sessions, registry)} for row in rows],
                "total": total, "offset": offset, "limit": limit}

    def page_items(self, **params):
        if self.profile is None:
            raise ValueError("profile_required")
        if (params.get("provider_id") is not None and params["provider_id"] not in TASK_PROVIDERS
                or params.get("state") is not None and params["state"] not in TASK_STATES
                or "project_id" in params and not project_id_valid(params["project_id"])):
            raise ProfileError("invalid_query")
        page = self.store.list_items(**params)
        return self._item_page(page, self.sessions(), self.registered_projects())

    def _item_page(self, page, sessions, registry):
        return {**page, "items": [{**row, "session_link_state": self.link_state(row, sessions, registry)}
                                  for row in page["items"]]}

    def save_item(self, body, *, update=False):
        from control_everything.workspace.task_schema import MAX_REVISION
        def invalid(field=None):
            raise ProfileError("invalid_item", field)
        if self.profile is None:
            raise ValueError("profile_required")
        if not isinstance(body, dict):
            invalid()
        if update:
            if set(body) != {"id", "expected_revision", "changes"}:
                invalid()
            try:
                item_id = canonical_uuid(body["id"])
            except (ValueError, TypeError):
                invalid("id")
            revision = body["expected_revision"]
            if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
                invalid("expected_revision")
            fields = body["changes"]
            if not isinstance(fields, dict) or not fields or set(fields) - TASK_FIELDS:
                invalid("changes")
            old = self.store.get_item(item_id)
            # A stale request is never validated against a newer row's grandfather limits.
            if old["revision"] != revision:
                raise WorkspaceError("item_changed")
            fields = {key: value for key, value in fields.items() if value != old[key]}
        else:
            if "title" not in body or set(body) - TASK_FIELDS:
                invalid()
            fields = {"project_id": None, "provider_id": None, "state": "todo", "session_key": None, **body}
            old, item_id, revision = None, None, None
        if "title" in fields:
            title = fields["title"]
            maximum = max(2000, len(old["title"])) if old else 2000
            if not isinstance(title, str) or not title.strip() or len(title) > maximum:
                invalid("title")
            try:
                self.store._check_title(title)
            except WorkspaceError as error:
                if str(error) == "invalid_item":
                    invalid("title")
                raise
            if not update:
                fields["title"] = title.strip()
        for name, allowed in (("provider_id", (*TASK_PROVIDERS, None)), ("state", TASK_STATES)):
            if name in fields and fields[name] not in allowed:
                invalid(name)
        if "project_id" in fields and fields["project_id"] is not None and not project_id_valid(fields["project_id"]):
            invalid("project_id")
        merged = {**old, **fields} if old else fields
        registry = self.registered_projects()
        if "project_id" in fields and fields["project_id"] is not None:
            project = next((row for row in registry if row["id"] == fields["project_id"]), None)
            if project is None or project["archived"]:
                invalid("project_id")
        link_changed = old is None or any(name in fields for name in ("project_id", "provider_id", "session_key"))
        key = merged["session_key"]
        sessions = self.sessions() if key is not None else unavailable(0)
        session_cwd = None
        if key is not None and link_changed:
            if merged["provider_id"] != "codex" or merged["project_id"] is None or not isinstance(key, str) or not key.startswith("codex:"):
                invalid("session_key")
            try:
                canonical_uuid(key[6:])
            except (ValueError, TypeError):
                invalid("session_key")
            project = next((row for row in registry if row["id"] == merged["project_id"]), None)
            if project is None or project["archived"]:
                invalid("project_id")
            session = next((row for row in sessions["items"] if row["session_key"] == key), None)
            if session is None or not sessions["available"]:
                raise WorkspaceError("session_not_in_sample")
            if observation_owner(session["cwd"], registry) != merged["project_id"]:
                raise WorkspaceError("session_project_mismatch")
            session_cwd = session["cwd"]
        # Preserve no-op CAS: the store receives an unchanged allowed field, never an empty patch.
        item = self.store.save_item(fields or {"title": old["title"]}, item_id=item_id,
                                    expected_revision=revision, session_cwd=session_cwd)
        return {"item": {**item, "session_link_state": self.link_state(item, sessions, registry)}}

    def page_records(self, **params):
        rows, total = self.store.list_records(**params)
        return {"items": rows, "total": total, "offset": params.get("offset", 0), "limit": params.get("limit", 50)}

    def snapshot(self):
        sessions = self.sessions()
        if self.profile is not None:
            observations, registry = self._observations_with_projects()
            from control_everything.workspace.observations import summary
            todos = self.store.list_todos()
            rows, total = self.store.list_tasks()
            tasks = self._task_page(rows, total, sessions, registry)
            items = self._item_page(self.store.list_items(), sessions, registry)
            records = self.page_records()
            return {"status": "ok", "sampled_at": datetime.now(timezone.utc).isoformat(), "today": today(),
                    "projects": {"status": "available", "reason": None, "items": self.profile_projects(registry, sessions)},
                    "todos": {"status": "available", "reason": None, **todos},
                    "tasks": {"status": "available", "reason": None, **tasks},
                    "items": items["items"], "items_page": items, "task_providers": list(TASK_PROVIDERS),
                    "records": records["items"],
                    "records_total": records["total"], "records_limit": records["limit"],
                    "providers": [{**item, "capabilities": {**item["capabilities"], "open_existing_ui": False,
                                                            "observe_sessions": item["id"] == "codex"}, "entrypoints": []}
                                  for item in discover_providers(observations["installations"])], "sessions": sessions,
                    "observations": summary(observations),
                    "entrypoints": [{"id": "progress", "label": "项目进度与待办", "href": "/progress"}],
                    "data_dir": str(self.profile.data_dir)}
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
                    {"id": "sessions", "label": "会话", "href": "/"},
                    {"id": "progress", "label": "原进度、待办与启动器", "href": "/progress"},
                    {"id": "grok", "label": "Grok 原服务", "href": "https://grok.com/"}]}

    def save_task(self, body, *, update=False):
        if not isinstance(body, dict) or set(body) != TASK_FIELDS | ({"id"} if update else set()):
            raise ValueError("invalid_request")
        title, project_id, provider_id, state, key = (body[name] for name in
                                                     ("title", "project_id", "provider_id", "state", "session_key"))
        if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 160
                or not isinstance(project_id, str) or provider_id not in (*TASK_PROVIDERS, None)
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
        if self.profile is not None and not project_id_valid(project_id):
            raise ValueError("invalid_request")
        if self.profile is not None:
            if (existing is None or existing["project_id"] != project_id) and (project is None or project["archived"]):
                raise ValueError("invalid_request")
        elif project is None:
            raise ValueError("invalid_request")
        unchanged = existing and all(existing[name] == body[name] for name in ("project_id", "provider_id", "session_key"))
        sessions = self.sessions() if key is not None else unavailable(0)
        if key is not None and not unchanged:
            session = next((row for row in sessions["items"] if row["session_key"] == key), None)
            if session is None or not sessions["available"]:
                raise WorkspaceError("session_not_in_sample")
            owns = observation_owner(session["cwd"], registry) == project_id if self.profile is not None else inside_project(session["cwd"], project["root"])
            if not owns:
                raise WorkspaceError("session_project_mismatch")
        task = self.store.save_task({**body, "title": title.strip()}, task_id)
        return {"status": "ok", "task": {**task, "session_link_state": self.link_state(task, sessions, registry)}}

    def save_record(self, body, *, _validate_only=False):
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
        if _validate_only:
            return fields
        return {"status": "ok", "record": self.store.save_record(fields)}

    def usage(self, *, refresh=False):
        quota = self.quota_source.refresh() if refresh else self.quota_source.get_cached()
        try:
            records, total = self.store.list_records(limit=100) if self.profile is not None else self.store.list_records()
            status, reason = "available", None
        except WorkspaceError:
            records, total, status, reason = [], None, "unavailable", "storage_unavailable"
        return {"status": "ok", "codex": quota, "manual_records": records,
                "manual_status": status, "manual_reason": reason, "manual_total": total, "manual_limit": 100}

    def news(self, stream='ai', *, refresh=False):
        if stream not in ('ai', 'hn'):
            raise ValueError('invalid_stream')
        if refresh:
            return self.news_source.refresh(stream)
        return self.news_source.get() if stream == 'ai' else self.news_source.get(stream)
