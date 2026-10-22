"""Local workspace projections and validation; never dispatches an agent task."""

import re
import shutil
import copy
import hashlib
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone

from control_everything.workspace import progress
from control_everything.sessions.codex_index import list_threads, unavailable
from control_everything.sessions.source import canonical_uuid
from control_everything.workspace.sources import CodexQuotaSource, NewsSource
from control_everything.input_validation import ProfileError
from control_everything.workspace.store import WorkspaceError, WorkspaceStore
from control_everything.workspace import management
from control_everything.workspace import project_dirs
from control_everything.workspace import dispatch
from control_everything.workspace import task_sessions
from control_everything.workspace import task_messages
from control_everything.workspace.validation import (
    finite_number, windows_path, inside_project, observation_owner, path_key, project_id_valid, validate_codex_project,
)

DEFAULT_DB = progress.REGISTRY.with_name("workspace-state.sqlite3")
BEIJING = timezone(timedelta(hours=8))
PROVIDERS = ("codex", "claude", "grok", "dsh", "opencode")
TASK_PROVIDERS = (*PROVIDERS, "hermes", "pi")
TASK_STATES = ("todo", "doing", "blocked", "review", "done")
TASK_FIELDS = {"title", "project_id", "provider_id", "state", "session_key"}
ITEM_FIELDS = TASK_FIELDS | {"priority", "focus", "assigned_agent_id"}
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
                 session_reader=None, quota_source=None, news_source=None, profile=None, observer=None,
                 desktop_reader=None):
        self.profile = profile
        self.observer = observer
        self.store = profile.store if profile is not None else WorkspaceStore(db_path)
        self.registry_path, self.manual_path, self.todos_path = registry_path, manual_path, todos_path
        self.session_reader = profile.codex_threads if profile is not None else session_reader or list_threads
        self.quota_source = quota_source or CodexQuotaSource()
        self.news_source = news_source or NewsSource()
        self.desktop_reader = desktop_reader or (profile.codex_desktop if profile is not None else None)
        self._desktop_lock = threading.RLock()
        self._desktop_config = None
        self._desktop_cached = None
        self._desktop_good = None
        self._desktop_until = 0
        if profile is not None:
            from control_everything.workspace.project_selection import WorkspaceSelection
            self.selection = WorkspaceSelection(profile)

    def desktop_snapshot(self):
        """Keep one source's last usable metadata; stale facts can never authorize binding."""
        with self._desktop_lock:
            config = self.profile.config()["codex"] if self.profile is not None else {"enabled": False, "db_path": None}
            pair = (config["enabled"], path_key(config["db_path"]) if config["db_path"] else None)
            if pair != self._desktop_config:
                self._desktop_config = pair
                self._desktop_cached = self._desktop_good = None
                self._desktop_until = 0
            if self._desktop_cached is not None and time.monotonic() < self._desktop_until:
                return copy.deepcopy(self._desktop_cached)
            empty = {"available": False, "reason": "not_configured", "sampled_at": None,
                     "source_id": None, "complete": False, "projects": [], "items": [], "coverage": {}}
            if not pair[0]:
                value = {**empty, "status": "not_configured"}
            else:
                try:
                    current = self.desktop_reader() if self.desktop_reader else empty
                except Exception:
                    current = {**empty, "reason": "source_unavailable"}
                latest = self.profile.config()["codex"]
                latest_pair = (latest["enabled"], path_key(latest["db_path"]) if latest["db_path"] else None)
                if latest_pair != pair:
                    self._desktop_good = None
                    current = {**empty, "reason": "source_changed" if latest_pair[0] else "not_configured"}
                if current["available"]:
                    value = {**copy.deepcopy(current), "status": "available"}
                    self._desktop_good = copy.deepcopy(value)
                elif self._desktop_good is not None:
                    value = {**copy.deepcopy(self._desktop_good), "available": False,
                             "status": "stale", "reason": current["reason"]}
                else:
                    value = {**copy.deepcopy(current), "status": "not_configured" if current["reason"] == "not_configured" else "unavailable"}
            if value["status"] != "available" or not value["complete"]:
                for row in (*value["projects"], *value["items"]):
                    row["binding_available"] = False
            self._desktop_cached, self._desktop_until = value, time.monotonic() + 2
            return copy.deepcopy(value)

    @staticmethod
    def desktop_summary(value):
        return {key: value[key] for key in ("status", "reason", "sampled_at", "source_id", "complete", "coverage")}

    def _desktop_projects(self, desktop):
        projects = []
        for row in desktop["projects"]:
            directories = [{"id": "main" if binding["role"] == "main" else
                            "d-" + hashlib.sha256(path_key(binding["path"]).encode("utf-8")).hexdigest()[:32],
                            "path": binding["path"]} for binding in row["directories"]]
            project = {**row, "directories": directories,
                       "root": next((value["path"] for value in directories if value["id"] == "main"), None),
                       "source": {"kind": "codex", "directory_id": None, "relative_path": None},
                       "read_only": True, "source_available": desktop["status"] == "available",
                       "binding_available": bool(row.get("binding_available") and desktop["complete"]
                                                 and desktop["status"] == "available")}
            try:
                validate_codex_project(project)
            except ProfileError:
                project["binding_available"] = False
            projects.append(project)
        return projects

    def page_codex_sessions(self, *, project_id=None, scope="active", offset=0, limit=20):
        if (scope not in ("active", "archived", "projectless", "unknown")
                or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100
                or project_id is not None and (not project_id_valid(project_id) or scope in ("projectless", "unknown"))):
            raise ProfileError("invalid_query")
        desktop = self.desktop_snapshot()
        registry = self.registered_projects(desktop=desktop)
        project = next((row for row in registry if row["id"] == project_id), None)
        items = []
        for row in desktop["items"]:
            assignment = row["assignment_state"]
            if scope == "projectless" and assignment != "projectless":
                continue
            if scope == "unknown" and assignment not in ("unknown", "conflict"):
                continue
            if scope in ("active", "archived") and bool(row["archived"]) != (scope == "archived"):
                continue
            if project_id is not None:
                if project is None:
                    continue
                if project.get("read_only"):
                    if assignment != "assigned" or row["project_id"] != project_id:
                        continue
                elif assignment != "projectless" or observation_owner(row["cwd"], [p for p in registry if not p.get("read_only")]) != project_id:
                    continue
            if project is not None and not project.get("read_only"):
                row = {**row, "binding_available": bool(desktop["status"] == "available" and desktop["complete"]
                                                        and not project.get("archived"))}
            elif project is not None:
                row = {**row, "binding_available": bool(row.get("binding_available") and project.get("binding_available"))}
            items.append(row)
        revision = hashlib.sha256(json.dumps([desktop["source_id"], desktop["status"], desktop["complete"],
            [(row["session_key"], row.get("project_id"), row["assignment_state"], row["archived"])
             for row in items]], separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
        return {**{key: desktop[key] for key in ("status", "reason", "sampled_at", "source_id", "complete")},
                "items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit,
                "revision": revision}

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
        context = self._project_context() if self.profile is not None else {"visible": [], "history": [], "stored": {}}
        registry = context["visible"]
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
        return value, context

    def registered_projects(self, *, desktop=None, stored=None, include_excluded=False):
        if self.profile is not None:
            desktop = self.desktop_snapshot() if desktop is None else desktop
            external = self._desktop_projects(desktop)
            by_id = {row["id"]: row for row in external}
            excluded = set(self.profile.workbench_config()["excluded_project_ids"]) if not include_excluded else set()
            local = []
            for saved in self.store.list_projects() if stored is None else stored:
                row = dict(saved)
                if row["id"] in excluded:
                    continue
                if row["source"]["kind"] == "codex":
                    if row["id"] in by_id:
                        row = {**by_id[row["id"]], "archived": row["archived"]}
                    else:
                        row.update(read_only=True, source_available=False, binding_available=False)
                local.append(row)
            return local
        try:
            return progress.load_registry(self.registry_path)
        except (OSError, ValueError, TypeError):
            raise WorkspaceError("workspace_unavailable") from None

    def _project_context(self, *, desktop=None):
        """Reuse one stored registry for visible choices and complete history in a request."""
        desktop = self.desktop_snapshot() if desktop is None else desktop
        stored = self.store.list_projects()
        excluded = set(self.profile.workbench_config()["excluded_project_ids"])
        history = self.registered_projects(desktop=desktop, stored=stored, include_excluded=True)
        return {"stored": {row["id"]: row for row in stored}, "history": history,
                "visible": [row for row in history if row["id"] not in excluded], "desktop": desktop}

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

    def link_state(self, task, sessions, registry, desktop=None):
        if task["session_key"] is None:
            return "none"
        if self.profile is not None:
            desktop = self.desktop_snapshot() if desktop is None else desktop
            full = next((row for row in desktop["items"] if row["session_key"] == task["session_key"]), None)
            project = next((row for row in registry if row["id"] == task["project_id"]), None)
            if project and (project.get("read_only") or project.get("source", {}).get("kind") == "codex"):
                if full is None or desktop["status"] != "available":
                    return "not_in_current_sample"
                return "verified" if full["assignment_state"] == "assigned" and full["project_id"] == project["id"] else "project_mismatch"
            if full is not None and full["assignment_state"] == "assigned":
                return "project_mismatch"
            if full is not None and full["assignment_state"] in ("unknown", "conflict"):
                return "not_in_current_sample"
            if full is not None and desktop["status"] == "available":
                sessions = {"available": True, "items": [full]}
        session = next((row for row in sessions["items"] if row["session_key"] == task["session_key"]), None)
        if session is None or not sessions["available"]:
            return "not_in_current_sample"
        project = next((row for row in registry if row["id"] == task["project_id"]), None)
        if self.profile is not None:
            return "verified" if project and observation_owner(session["cwd"], [row for row in registry if not row.get("read_only")]) == project["id"] else "project_mismatch"
        return "verified" if project and inside_project(session["cwd"], project["root"]) else "project_mismatch"

    def profile_projects(self, registry=None, sessions=None):
        from control_everything.workspace.project_progress import RequestBudget, observe
        registry = self.registered_projects() if registry is None else registry
        sessions = self.sessions() if sessions is None else sessions
        saved, counts, budget = self.store.manual_rows(), self.store.task_counts(), RequestBudget()
        result = []
        desktop = self.desktop_snapshot()
        for project in registry:
            manual = saved.get(project["id"])
            observation = observe(project, budget)
            candidates = desktop["items"] if project.get("read_only") or desktop["status"] == "available" else sessions["items"]
            hits = [{"tool": "Codex", "place": row["cwd"], "href": "/workspace", "session_key": row["session_key"]}
                    for row in candidates if (row.get("assignment_state") == "assigned" and row.get("project_id") == project["id"] if project.get("read_only") else
                    row.get("assignment_state", "projectless") == "projectless" and
                    observation_owner(row["cwd"], [value for value in registry if not value.get("read_only")]) == project["id"])][:12]
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
        sessions = self.sessions()
        context = self._project_context() if self.profile is not None else None
        registry = context["history"] if context is not None else self.registered_projects()
        return self._task_page(rows, total, sessions, registry,
                               offset=params.get("offset", 0), limit=params.get("limit", 50),
                               desktop=context["desktop"] if context is not None else None)

    def _task_page(self, rows, total, sessions, registry, *, offset=0, limit=50, desktop=None):
        desktop = self.desktop_snapshot() if desktop is None else desktop
        return {"items": [{**row, "session_link_state": self.link_state(row, sessions, registry, desktop)} for row in rows],
                "total": total, "offset": offset, "limit": limit}

    def page_items(self, include_assignment=False, **params):
        if self.profile is None:
            raise ValueError("profile_required")
        if (params.get("provider_id") is not None and params["provider_id"] not in TASK_PROVIDERS
                or params.get("state") is not None and params["state"] not in TASK_STATES
                or "project_id" in params and not project_id_valid(params["project_id"])):
            raise ProfileError("invalid_query")
        page = self.store.list_items(**params)
        return self._item_page(page, self.sessions(), self._project_context(), assignment=include_assignment)

    def list_agent_tasks(self, agent_id, state=None, project_ids=None):
        from control_everything.workspace import agent_runs
        return agent_runs.list_tasks(self.store, agent_id, state, project_ids)

    def get_agent_task(self, agent_id, task_id, project_ids=None):
        from control_everything.workspace import agent_runs
        return agent_runs.get_task(self.store, agent_id, task_id, project_ids)

    def claim_agent_task(self, agent_id, task_id, expected_revision, project_ids=None):
        from control_everything.workspace import agent_runs
        return agent_runs.claim(self.store, agent_id, task_id, expected_revision, project_ids)

    def claim_dispatch_in_transaction(self, connection, *, dispatch_id, task_id, agent_id,
                                      execution_run_id, expected_revision):
        from control_everything.workspace import agent_runs
        return agent_runs.claim_dispatch_in_transaction(
            self.store, connection, dispatch_id=dispatch_id, task_id=task_id, agent_id=agent_id,
            execution_run_id=execution_run_id, expected_revision=expected_revision)

    def project_execution_state(self, connection, **facts):
        from control_everything.workspace import agent_runs
        return agent_runs.project_execution_state(self.store, connection, **facts)

    def progress_agent_task(self, agent_id, task_id, expected_revision, stage, percent=None, message=None,
                            project_ids=None, wait_kind=None, request_id=None, agent_hint=None, run_id=None):
        from control_everything.workspace import agent_runs
        return agent_runs.progress(self.store, agent_id, task_id, expected_revision, stage, percent, message,
                                   project_ids, wait_kind, request_id, agent_hint, run_id)

    def submit_agent_review(self, agent_id, task_id, expected_revision, summary, evidence=None, project_ids=None,
                            run_id=None):
        from control_everything.workspace import agent_runs
        return agent_runs.submit_review(self.store, agent_id, task_id, expected_revision, summary, evidence,
                                        project_ids, run_id)

    def cancel_agent_run(self, task_id, expected_revision, reason):
        from control_everything.workspace import agent_runs
        return agent_runs.cancel_run(self.store, task_id, expected_revision, reason)

    def _item_projection(self, row, sessions, context):
        visible = {project["id"] for project in context["visible"]}
        stored = context["stored"]
        return {**row, "project_excluded": row["project_id"] in stored and row["project_id"] not in visible,
                "project_name": stored.get(row["project_id"], {}).get("name"),
                "session_link_state": self.link_state(row, sessions, context["history"], context["desktop"])}

    def _item_page(self, page, sessions, context, assignment=False):
        states = {}
        if assignment:
            from control_everything.workspace import agent_runs
            states = agent_runs.assignment_states(self.store, [row["id"] for row in page["items"]])
        items = []
        for row in page["items"]:
            projected = self._item_projection(row, sessions, context)
            if assignment:
                info = states.get(row["id"]) or {}
                projected["assignment_state"] = self._assignment_state(row, info)
                projected["connection_state"] = self._connection_state(info)
                projected["last_contact_at"] = info.get("heartbeat_at")
            items.append(projected)
        return {**page, "items": items}

    @staticmethod
    def _assignment_state(row, info):
        info = info or {}
        run_status = info.get("run_status")
        last_event = info.get("last_event") or {}
        waiting = (last_event.get("kind") == "progress"
                   and (last_event.get("payload") or {}).get("stage") == "waiting_user")
        if waiting:
            return "waiting_user"
        if row["state"] == "done":
            return "done"
        if row["state"] == "review":
            return "waiting_review"
        if row["state"] == "blocked":
            return "blocked"
        if run_status == "waiting_review":
            return "waiting_review"
        if run_status in ("claimed", "running"):
            return "running"
        if run_status in ("failed", "cancelled"):
            return "failed"
        return "assigned" if row.get("assigned_agent_id") else "unassigned"

    @staticmethod
    def _connection_state(info):
        if not info or info.get("run_status") is None:
            return "not_applicable"
        from control_everything.workspace import agent_runs
        return agent_runs.connection_state(info.get("heartbeat_at"), info.get("run_status"))

    def agent_task_detail(self, task_id):
        from control_everything.workspace import agent_runs
        return agent_runs.task_detail(self.store, task_id)

    def project_has_active_execution(self, project_id):
        with self.store.connection() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT 1 FROM execution_runs WHERE project_id=? AND "
                "(state IN ('starting','running','waiting_approval','cancel_requested') "
                "OR cleanup_state <> 'verified') LIMIT 1",
                (project_id,)).fetchone()
        return row is not None

    def project_directory(self, project_id, directory_id=None):
        with self.store.connection() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT path FROM project_directories WHERE project_id=? "
                + ("AND binding_id=? " if directory_id is not None else "")
                + "ORDER BY is_primary DESC, binding_id LIMIT 1",
                (project_id, directory_id) if directory_id is not None else (project_id,)).fetchone()
        if row is None:
            raise WorkspaceError("project_not_found")
        return row["path"]

    def inspect_project_directory(self, body):
        return project_dirs.inspect(body)

    def preview_project_directory(self, body):
        return project_dirs.preview(body)

    def register_project_directory(self, body):
        return project_dirs.register(self.store, body)

    def create_project_directory(self, body):
        return project_dirs.create(self.store, body)

    def pick_project_directory(self, body):
        return project_dirs.pick(body)

    def get_task_brief(self, task_id):
        return dispatch.get_brief(self.profile, task_id)

    def save_task_brief(self, task_id, body):
        return dispatch.save_brief(self.profile, task_id, body)

    def preview_task_dispatch(self, task_id, body):
        return dispatch.preview_dispatch(self.profile, task_id, body)

    def create_task_dispatch(self, task_id, body):
        return dispatch.create_dispatch(self.profile, task_id, body)

    def task_dispatches(self, task_id):
        return dispatch.list_dispatches(self.store, task_id)

    def task_dispatch(self, dispatch_id):
        return dispatch.get_dispatch(self.store, dispatch_id)

    def dispatch_execution_facts(self, dispatch_id, *, connection=None, agent_settings=None):
        return dispatch.execution_facts(self.profile, self.store, dispatch_id,
                                        connection=connection, agent_settings=agent_settings)

    def execution_run_facts(self, **facts):
        return dispatch.execution_run_facts(self.profile, self.store, **facts)

    def execution_binding(self, connection, *, dispatch_id, execution_run_id, task_id):
        return dispatch.execution_binding(connection, dispatch_id=dispatch_id,
                                          execution_run_id=execution_run_id, task_id=task_id)

    def mark_dispatch_sent(self, dispatch_id):
        return dispatch.mark_sent(self.store, dispatch_id)

    def cancel_task_dispatch(self, dispatch_id):
        return dispatch.cancel_dispatch(self.store, dispatch_id)

    def task_sessions(self, task_id):
        return task_sessions.task_sessions(self.store, task_id)

    def link_task_session(self, task_id, body):
        if type(body) is dict and body.get("agent_id") is not None:
            agent_id = body["agent_id"]
            if not any(row["id"] == agent_id and row["enabled"] for row in self.profile.agent_settings()["items"]):
                raise ProfileError("agent_not_found", "agent_id", status=404)
        return task_sessions.link_session(self.store, task_id, body)

    def unlink_task_session(self, session_id, body):
        return task_sessions.unlink_session(self.store, session_id, body)

    def list_task_sessions(self, project_id=None):
        return task_sessions.list_sessions(self.store, project_id)

    def task_messages(self, task_id):
        return task_messages.list_messages(self.store, task_id)

    def create_task_message(self, task_id, body):
        if type(body) is dict and body.get("agent_id") is not None:
            agent_id = body["agent_id"]
            if not any(row["id"] == agent_id and row["enabled"] for row in self.profile.agent_settings()["items"]):
                raise ProfileError("agent_not_found", "agent_id", status=404)
        return task_messages.create_message(self.store, task_id, body)

    def mark_message_sent(self, message_id):
        return task_messages.mark_sent(self.store, message_id)

    def cancel_task_message(self, message_id):
        return task_messages.cancel_message(self.store, message_id)

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
            if not isinstance(fields, dict) or not fields or set(fields) - ITEM_FIELDS:
                invalid("changes")
            old = self.store.get_item(item_id)
            # A stale request is never validated against a newer row's grandfather limits.
            if old["revision"] != revision:
                raise WorkspaceError("item_changed")
        else:
            if "title" not in body or set(body) - ITEM_FIELDS:
                invalid()
            fields = {"project_id": None, "provider_id": None, "state": "todo", "session_key": None, **body}
            old, item_id, revision = None, None, None
        # Validate before equality checks: bool compares equal to integer flags.
        management.validate_item_flags(fields)
        if update:
            fields = {key: value for key, value in fields.items() if value != old[key]}
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
        desktop = self.desktop_snapshot()
        context = self._project_context(desktop=desktop)
        registry = context["visible"]
        if "project_id" in fields and fields["project_id"] is not None:
            project = next((row for row in registry if row["id"] == fields["project_id"]), None)
            if project is None or project["archived"]:
                invalid("project_id")
        link_changed = old is None or any(name in fields for name in ("project_id", "provider_id", "session_key"))
        project = next((row for row in registry if row["id"] == merged["project_id"]), None)
        projection = None
        if project and project.get("read_only") and link_changed and merged["session_key"] is not None:
            if not project.get("binding_available"):
                raise WorkspaceError("session_not_in_sample")
            projection = project
        key = merged["session_key"]
        sessions = self.sessions() if key is not None else unavailable(0)
        session_cwd = None
        session_project_id = None
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
            session = next((row for row in desktop["items"] if row["session_key"] == key), None)
            # Legacy-only installations keep their existing sample contract.
            if session is None and not project.get("read_only") and desktop["status"] == "not_configured":
                session = next((row for row in sessions["items"] if row["session_key"] == key), None)
                available = sessions["available"]
            else:
                available = (desktop["status"] == "available" and desktop["complete"] and session is not None
                             and (session.get("binding_available", False) or not project.get("read_only")
                                  and session.get("assignment_state") == "projectless"))
            if session is None or not available:
                raise WorkspaceError("session_not_in_sample")
            if project.get("read_only"):
                if session.get("assignment_state") != "assigned" or session.get("project_id") != project["id"]:
                    raise WorkspaceError("session_project_mismatch")
                session_project_id = project["id"]
            elif (session.get("assignment_state") == "assigned" or
                  observation_owner(session["cwd"], [row for row in registry if not row.get("read_only")]) != merged["project_id"]):
                raise WorkspaceError("session_project_mismatch")
            session_cwd = session["cwd"]
        # Preserve no-op CAS: the store receives an unchanged allowed field, never an empty patch.
        with self.profile.agent_settings_guard() as agents:
            assignment_changed = old is None or any(key in fields for key in ("project_id", "assigned_agent_id"))
            agent_id = merged.get("assigned_agent_id")
            if assignment_changed and agent_id is not None:
                agent = next((row for row in agents["items"] if row["id"] == agent_id and row["enabled"]), None)
                if (not isinstance(agent_id, str) or agent is None
                        or agent["project_ids"] and merged["project_id"] not in agent["project_ids"]):
                    invalid("assigned_agent_id")
            item = self.store.save_item(fields or {"title": old["title"]}, item_id=item_id,
                                        expected_revision=revision, session_cwd=session_cwd,
                                        codex_project=projection, codex_session_project_id=session_project_id)
        projected = self._item_projection(item, sessions, context)
        from control_everything.workspace import agent_runs
        info = agent_runs.assignment_states(self.store, [item["id"]]).get(item["id"]) or {}
        projected["assignment_state"] = self._assignment_state(item, info)
        projected["connection_state"] = self._connection_state(info)
        projected["last_contact_at"] = info.get("heartbeat_at")
        return {"item": projected}

    def page_decisions(self, **params):
        return management.list_decisions(self.store, **params)

    def save_decision(self, body, *, update=False):
        # These are local annotations; selecting a project never dispatches a tool.
        if not update:
            management.validate_decision_body(body)
            project = next((row for row in self.registered_projects()
                            if row['id'] == body['project_id'] and not row['archived']), None)
            if project is None:
                raise ProfileError('invalid_decision', 'project_id')
        return management.save_decision(self.store, body, update=update)

    def page_reviews(self, **params):
        return management.list_reviews(self.store, **params)

    def save_review(self, body):
        return management.save_review(self.store, body)

    def page_records(self, **params):
        rows, total = self.store.list_records(**params)
        return {"items": rows, "total": total, "offset": params.get("offset", 0), "limit": params.get("limit", 50)}

    def snapshot(self):
        sessions = self.sessions()
        if self.profile is not None:
            observations, context = self._observations_with_projects()
            registry = context["visible"]
            from control_everything.workspace.observations import summary
            todos = self.store.list_todos()
            rows, total = self.store.list_tasks()
            tasks = self._task_page(rows, total, sessions, context["history"], desktop=context["desktop"])
            items = self._item_page(self.store.list_items(), sessions, context)
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
                    "codex_desktop": self.desktop_summary(self.desktop_snapshot()),
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
        context = self._project_context() if self.profile is not None else None
        registry = context["visible"] if context is not None else self.registered_projects()
        project = next((row for row in registry if row["id"] == project_id), None)
        if self.profile is not None and (project_id.startswith("cx-") or project and project.get("read_only")):
            raise ValueError("invalid_request")
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
            desktop = context["desktop"] if context is not None else None
            if desktop is not None and desktop["status"] != "not_configured":
                session = next((row for row in desktop["items"] if row["session_key"] == key), None)
                if session is None or desktop["status"] != "available" or not desktop["complete"]:
                    raise WorkspaceError("session_not_in_sample")
                if session["assignment_state"] == "assigned":
                    raise WorkspaceError("session_project_mismatch")
                if session["assignment_state"] != "projectless":
                    raise WorkspaceError("session_not_in_sample")
            else:
                session = next((row for row in sessions["items"] if row["session_key"] == key), None)
                if session is None or not sessions["available"]:
                    raise WorkspaceError("session_not_in_sample")
            owns = (observation_owner(session["cwd"], [row for row in registry if not row.get("read_only")]) == project_id
                    if self.profile is not None else inside_project(session["cwd"], project["root"]))
            if not owns:
                raise WorkspaceError("session_project_mismatch")
        if self.profile is not None:
            with self.profile.agent_settings_guard() as agents:
                task = self.store.save_task({**body, "title": title.strip()}, task_id, agent_settings=agents)
        else:
            task = self.store.save_task({**body, "title": title.strip()}, task_id)
        return {"status": "ok", "task": {**task, "session_link_state": self.link_state(task, sessions,
            context["history"] if context is not None else registry, context["desktop"] if context is not None else None)}}

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
