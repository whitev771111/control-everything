"""Public native facts and registered-directory ownership, without raw OS metadata."""

import hashlib
import json
import ntpath

from control_everything.workspace.store import path_key


def scope_roots(projects):
    roots = []
    for project in projects:
        if project.get("archived"):
            continue
        for binding in project["directories"]:
            identity = hashlib.sha256((project["id"] + "\0" + path_key(binding["path"])).encode("utf-8")).hexdigest()
            roots.append({"directory_id": identity, "path": binding["path"]})
    return sorted(roots, key=lambda item: item["directory_id"])


def project_observations(cached, projects):
    value = cached["snapshot"]
    result = {"status": cached["status"], "reason": cached["reason"], "environment_id": "windows:local",
              "generation": None, "seq": None, "scope_revision": cached["scope_revision"], "sampled_at_ms": None,
              "metrics_interval_ms": None, "coverage": {"status": "unknown"}, "installations": [],
              "instances": [], "resources": [], "project_refs": [], "unassigned": []}
    result.update(config_revision=None, config_hash=None, registrations=[])
    if value is None:
        return result
    roots = scope_roots(projects)
    digest = hashlib.sha256(json.dumps(roots, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    config_current = (cached.get("config_pair") == cached.get("applied_pair")
                      and cached.get("config_pair") == (value.get("config_revision"), value.get("config_hash")))
    scope_current = cached["scope_hash"] == digest and value["scope_revision"] == cached["scope_revision"]
    directory_owners = {}
    for project in projects:
        if project.get("archived"):
            continue
        for binding in project["directories"]:
            identity = hashlib.sha256((project["id"] + "\0" + path_key(binding["path"])).encode("utf-8")).hexdigest()
            directory_owners[identity] = (len(ntpath.normpath(binding["path"])), project["id"])
    for field in ("generation", "seq", "scope_revision", "sampled_at_ms", "metrics_interval_ms", "coverage", "installations", "resources", "config_revision", "config_hash"):
        result[field] = value[field]
    if config_current and cached["status"] == "available":
        result["registrations"] = value["registrations"]
    references = {}
    for item in value["instances"]:
        matches = [directory_owners[key] for key in item["directory_ids"] if key in directory_owners] if scope_current and config_current else []
        longest = max((length for length, _ in matches), default=0)
        owners = {owner for length, owner in matches if length == longest}
        owner = next(iter(owners)) if len(owners) == 1 and cached["status"] == "available" else None
        public = {key: entry for key, entry in item.items() if key != "directory_ids"}
        public["project_id"] = owner
        if not scope_current or not config_current:
            public["return_action"] = None
            public["registration_ids"] = []
        if cached["status"] != "available":
            public["status"] = "stale"
        result["instances"].append(public)
        if owner:
            references.setdefault(owner, []).append(public["instance_id"])
        else:
            result["unassigned"].append(public["instance_id"])
    result["project_refs"] = [{"project_id": owner, "instance_ids": instances} for owner, instances in references.items()]
    if not config_current:
        result["status"], result["reason"] = "stale", cached["reason"] if cached["reason"] in ("config_unavailable", "config_conflict") else "config_pending"
    elif not scope_current:
        result["status"], result["reason"] = "limited", "scope_pending"
    elif cached["status"] == "available" and value["status"] != "available":
        result["status"], result["reason"] = value["status"], value["reason"]
    return result


def summary(observations):
    return {key: observations[key] for key in ("status", "reason", "generation", "seq", "sampled_at_ms", "coverage")}
