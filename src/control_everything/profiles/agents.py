"""Versioned local coding-agent registration; no process or task dispatch."""

import copy
import json
import re
from datetime import datetime
from uuid import UUID, uuid4

from control_everything.input_validation import ProfileError


MAX_AGENTS = 20
MAX_REVISION = (1 << 53) - 1
KINDS = ("codex", "dsh", "custom")
EMPTY = {"revision": 0, "items": []}
CORE_FIELDS = {"id", "name", "kind", "command", "enabled", "created_at", "updated_at"}
ITEM_FIELDS = CORE_FIELDS | {"token_hash", "token_prefix", "project_ids", "adapter", "connection",
                             "permission", "network", "approval"}
EDIT_FIELDS = {"id", "name", "kind", "command", "enabled", "created_at", "updated_at", "project_ids",
               "adapter", "connection", "permission", "network", "approval"}
PERMISSIONS = ("read-only", "workspace-write", "danger-full-access")
NETWORKS = ("allowed", "denied")
APPROVALS = ("request_user", "deny", "never")
DEFAULT_PERMISSION = "workspace-write"
DEFAULT_NETWORK = "allowed"
DEFAULT_APPROVAL = "request_user"
ADAPTER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
CONNECTION_KEY_RE = re.compile(r"[a-z0-9_]{1,64}\Z")
SENSITIVE_KEYS = {"token", "secret", "password", "api_key", "apikey", "authorization", "credential"}


def _invalid(field):
    raise ProfileError("invalid_config", "agents." + field if field else "agents")


def _identity(value):
    if type(value) is not str:
        _invalid("id")
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        _invalid("id")
    if str(parsed) != value:
        _invalid("id")
    return value


def _text(value, field, maximum, *, required=True):
    if (type(value) is not str or not 1 <= len(value) <= maximum
            or required and not value.strip()
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        _invalid(field)
    return value


def _projects(value):
    if type(value) is not list or len(value) > 50:
        _invalid("project_ids")
    result = []
    for project_id in value:
        result.append(_text(project_id, "project_ids", 64))
    if len(set(result)) != len(result):
        _invalid("project_ids")
    return result


def _choice(value, field, choices, default):
    if value is None:
        return default
    if type(value) is not str or value not in choices:
        _invalid(field)
    return value


def _adapter(value):
    if value is None:
        return None
    if type(value) is not str or not ADAPTER_RE.fullmatch(value):
        _invalid("adapter")
    return value


def _connection_value(value, field):
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -(1 << 53) <= value <= (1 << 53):
            _invalid(field)
        return value
    if type(value) is str:
        return _text(value, field, 2048, required=False)
    if type(value) is list:
        if len(value) > 20 or any(type(item) is not str or len(item) > 512 for item in value):
            _invalid(field)
        return value
    _invalid(field)


def _connection(value):
    if type(value) is not dict or len(value) > 20:
        _invalid("connection")
    result = {}
    for key, item in value.items():
        if (type(key) is not str or not CONNECTION_KEY_RE.fullmatch(key)
                or key.lower() in SENSITIVE_KEYS):
            _invalid("connection")
        result[key] = _connection_value(item, "connection")
    if len(json.dumps(result, ensure_ascii=False)) > 8192:
        _invalid("connection")
    return result


def _timestamp(value, field):
    if type(value) is not str or not 1 <= len(value) <= 80:
        _invalid(field)
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError, OverflowError):
        _invalid(field)
    if parsed.tzinfo is None:
        _invalid(field)
    return value


def validate(value):
    if (type(value) is not dict or set(value) != {"revision", "items"}
            or type(value["revision"]) is not int or not 0 <= value["revision"] <= MAX_REVISION
            or type(value["items"]) is not list or len(value["items"]) > MAX_AGENTS):
        _invalid("")
    seen = set()
    items = []
    for row in value["items"]:
        if (type(row) is not dict or not CORE_FIELDS <= set(row)
                or set(row) - ITEM_FIELDS):
            _invalid("item")
        identity = _identity(row["id"])
        if identity in seen:
            _invalid("id")
        seen.add(identity)
        name = _text(row["name"], "name", 80)
        kind = row["kind"]
        if kind not in KINDS:
            _invalid("kind")
        command = row["command"]
        if command is not None:
            command = _text(command, "command", 2048)
        if type(row["enabled"]) is not bool:
            _invalid("enabled")
        created = _timestamp(row["created_at"], "created_at")
        updated = _timestamp(row["updated_at"], "updated_at")
        token_hash = row.get("token_hash")
        if token_hash is not None and (type(token_hash) is not str or not re.fullmatch("[0-9a-f]{64}", token_hash)):
            _invalid("token_hash")
        token_prefix = row.get("token_prefix")
        if token_prefix is not None and (type(token_prefix) is not str or not 1 <= len(token_prefix) <= 16):
            _invalid("token_prefix")
        project_ids = _projects(row.get("project_ids", []))
        adapter = _adapter(row.get("adapter"))
        connection = _connection(row.get("connection", {}))
        permission = _choice(row.get("permission"), "permission", PERMISSIONS, DEFAULT_PERMISSION)
        network_default = "allowed" if adapter == "dsh" else "denied"
        network = _choice(row.get("network"), "network", NETWORKS, network_default)
        approval = _choice(row.get("approval"), "approval", APPROVALS, DEFAULT_APPROVAL)
        items.append({"id": identity, "name": name, "kind": kind, "command": command,
                      "enabled": row["enabled"], "created_at": created, "updated_at": updated,
                      "token_hash": token_hash, "token_prefix": token_prefix, "project_ids": project_ids,
                      "adapter": adapter, "connection": connection,
                      "permission": permission, "network": network, "approval": approval})
    return {"revision": value["revision"], "items": items}


def from_config(config):
    return validate(config.get("agents", copy.deepcopy(EMPTY)))


def _item(row, *, stamp):
    adapter = _adapter(row.get("adapter"))
    network_default = "allowed" if adapter == "dsh" else "denied"
    return {
        "id": _identity(row["id"]) if row.get("id") is not None else str(uuid4()),
        "name": _text(row.get("name"), "name", 80),
        "kind": row.get("kind") if row.get("kind") in KINDS else _invalid("kind"),
        "command": None if row.get("command") is None else _text(row["command"], "command", 2048),
        "enabled": row.get("enabled"),
        "created_at": stamp,
        "updated_at": stamp,
        "token_hash": None,
        "token_prefix": None,
        "project_ids": _projects(row.get("project_ids", [])),
        "adapter": adapter,
        "connection": _connection(row.get("connection", {})),
        "permission": _choice(row.get("permission"), "permission", PERMISSIONS, DEFAULT_PERMISSION),
        "network": _choice(row.get("network"), "network", NETWORKS, network_default),
        "approval": _choice(row.get("approval"), "approval", APPROVALS, DEFAULT_APPROVAL),
    }


def normalize_items(rows, current, stamp):
    """Apply client edits while preserving identity, tokens and server timestamps."""
    if type(rows) is not list or len(rows) > MAX_AGENTS:
        _invalid("items")
    existing = {row["id"]: row for row in current["items"]}
    result = []
    for raw in rows:
        if type(raw) is not dict or set(raw) - EDIT_FIELDS:
            _invalid("item")
        identity = raw.get("id")
        if identity is None:
            item = _item(raw, stamp=stamp)
        else:
            identity = _identity(identity)
            old = existing.get(identity)
            if old is None:
                _invalid("id")
            item = _item({**raw, "id": identity}, stamp=stamp)
            item["created_at"] = old["created_at"]
            item["token_hash"] = old.get("token_hash")
            item["token_prefix"] = old.get("token_prefix")
            if "project_ids" not in raw:
                item["project_ids"] = old.get("project_ids", [])
            if "adapter" not in raw:
                item["adapter"] = old.get("adapter")
            if "connection" not in raw:
                item["connection"] = old.get("connection", {})
            if "permission" not in raw:
                item["permission"] = old.get("permission", DEFAULT_PERMISSION)
            if "network" not in raw:
                item["network"] = old.get("network") or ("allowed" if item.get("adapter") == "dsh" else "denied")
            if "approval" not in raw:
                item["approval"] = old.get("approval", DEFAULT_APPROVAL)
            changed = any(item[key] != old[key] for key in
                          ("name", "kind", "command", "enabled", "project_ids", "adapter", "connection",
                           "permission", "network", "approval"))
            item["updated_at"] = stamp if changed else old["updated_at"]
        if type(item["enabled"]) is not bool:
            _invalid("enabled")
        result.append(item)
    return validate({"revision": max(1, current["revision"]), "items": result})
