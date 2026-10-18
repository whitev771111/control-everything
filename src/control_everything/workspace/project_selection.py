"""Explicit project selection; each request commits either SQLite or profile intent."""

import copy
import hashlib
import json
import threading
import time
from pathlib import Path

from control_everything.input_validation import ProfileError
from control_everything.sessions import project_sources
from control_everything.workspace.validation import project_id_valid, validate_candidate_project, validate_codex_project


class WorkspaceSelection:
    def __init__(self, profile):
        self.profile, self.store = profile, profile.store
        self._cache, self._lock = {}, threading.RLock()

    def token(self):
        return hashlib.sha256(f'{self.profile.workbench_config()["revision"]}:{self.store.list_revision()}'.encode()).hexdigest()

    def _require_token(self, token):
        revision = self.store.list_revision()
        current = hashlib.sha256(f'{self.profile.workbench_config()["revision"]}:{revision}'.encode()).hexdigest()
        if type(token) is not str or token != current:
            raise ProfileError("selection_conflict", status=409)
        return revision

    def source(self, provider, config=None):
        if type(provider) is not str or provider not in project_sources.PROVIDERS:
            raise ProfileError("invalid_query", "provider")
        config = self.profile.config() if config is None else config
        return config["codex"] if provider == "codex" else self.profile.workbench_config()["sources"][provider]

    def locations(self, provider):
        source = self.source(provider)
        return project_sources.discover_locations(provider, source.get("path", source.get("db_path")))

    def invalidate(self):
        with self._lock:
            self._cache.clear()

    def _snapshot(self, provider, *, force=False):
        with self._lock:
            source, revision = self.source(provider), self.profile.workbench_config()["revision"]
            key = (provider, json.dumps(source, sort_keys=True), revision)
            cached = self._cache.get(provider)
            if cached and cached[0] == key and not force and cached[1] > time.monotonic():
                return copy.deepcopy(cached[2])
            value = project_sources.read_confirmed(provider, source)
            valid, invalid = [], False
            for project in value["projects"]:
                try:
                    (validate_codex_project if provider == "codex" else validate_candidate_project)(project)
                    valid.append(project)
                except ProfileError:
                    invalid = True
            value["projects"] = valid
            if invalid:
                value.update(status="limited", complete=False, reason="invalid_metadata")
            if source != self.source(provider) or revision != self.profile.workbench_config()["revision"]:
                value = project_sources.result("unavailable", "source_changed")
            payload = [provider, value["source_id"], revision, value["status"], value["complete"], value["projects"]]
            value["snapshot_token"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            value["source_config"] = copy.deepcopy(source)
            self._cache[provider] = (key, time.monotonic() + 2, copy.deepcopy(value))
            return value

    def candidates(self, provider, *, offset=0, limit=50):
        self.source(provider)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ProfileError("invalid_query")
        value = self._snapshot(provider)
        stored = {row["id"] for row in self.store.list_projects()}
        excluded = set(self.profile.workbench_config()["excluded_project_ids"])
        items = [{"candidate_id": row["id"], "project_id": row["id"], "name": row["name"],
                  "directories": row["directories"], "selected": row["id"] in stored and row["id"] not in excluded,
                  "origin_provider": provider} for row in value["projects"]]
        for item in items[offset:offset + limit]:
            try:
                item["directory_available"] = all(Path(binding["path"]).is_dir() for binding in item["directories"])
            except OSError:
                item["directory_available"] = False
        return {key: value[key] for key in ("status", "reason", "complete", "source_id", "snapshot_token")} | {
            "items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit,
            "selection_token": self.token()}

    def overview(self):
        settings = self.profile.workbench_config()
        stored = self.store.list_projects()
        return {"workbench_revision": settings["revision"], "selection_token": self.token(),
            "tools": [{"provider": provider, "source": {"enabled": self.source(provider)["enabled"],
                       "path": self.source(provider).get("path", self.source(provider).get("db_path"))},
                       "locations": self.locations(provider)} for provider in project_sources.PROVIDERS],
            "excluded_projects": [{"project_id": row["id"], "name": row["name"], "archived": row["archived"]}
                                  for row in stored if row["id"] in settings["excluded_project_ids"]]}

    def _result(self, identity, selected, changed, *, requires_rejoin=False):
        project = next(row for row in self.store.list_projects() if row["id"] == identity)
        return {"status": "ok", "project_id": identity, "selected": selected, "changed": changed,
                "requires_rejoin": requires_rejoin, "selection_token": self.token(), "archived": project["archived"]}

    def change(self, body):
        if type(body) is not dict:
            raise ProfileError("invalid_request")
        action = body.get("action")
        required = {"action", "provider", "candidate_id", "snapshot_token", "selection_token"} if action == "join" else {
            "action", "project_id", "selection_token"}
        if set(body) != required or action not in ("join", "remove", "rejoin"):
            raise ProfileError("invalid_request")
        candidate, revision = None, None
        if action == "join":
            revision = self.profile.workbench_config()["revision"]
            snapshot = self._snapshot(body["provider"], force=True)
            if not snapshot["complete"] or snapshot["status"] != "available" or body["snapshot_token"] != snapshot["snapshot_token"]:
                raise ProfileError("candidate_changed", status=409)
            candidate = next((row for row in snapshot["projects"] if row["id"] == body["candidate_id"]), None)
            if candidate is None:
                raise ProfileError("candidate_changed", status=409)
            identity = candidate["id"]
        else:
            identity = body["project_id"]
            if not project_id_valid(identity):
                raise ProfileError("invalid_project", "id")
        with self.profile.selection_guard(), self.store.write_lock:
            original = self.profile.config()
            current = self.profile.workbench_config()
            if revision is not None and revision != current["revision"]:
                raise ProfileError("candidate_changed", status=409)
            if action == "join" and snapshot["source_config"] != self.source(body["provider"], original):
                raise ProfileError("candidate_changed", status=409)
            existing = next((row for row in self.store.list_projects() if row["id"] == identity), None)
            excluded = identity in current["excluded_project_ids"]
            if existing is None and action != "join":
                raise ProfileError("not_found", "project_id", status=404)
            desired = action != "remove"
            if existing is not None and excluded != desired:
                return self._result(identity, desired, False)
            list_revision = self._require_token(body["selection_token"])
            if existing is None:
                changed = self.store.join_candidate(candidate, expected_revision=list_revision)
                return self._result(identity, not excluded, changed, requires_rejoin=excluded)
            ids = set(current["excluded_project_ids"])
            ids.discard(identity) if desired else ids.add(identity)
            current["excluded_project_ids"] = sorted(ids)
            self.profile.write_workbench_locked(original, current)
            return self._result(identity, desired, True)
