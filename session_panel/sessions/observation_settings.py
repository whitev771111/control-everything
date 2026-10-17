"""Strict local registration intent; no discovery, execution, or OS probing."""

import copy
import hashlib
import json
import ntpath
import re
import unicodedata

from session_panel.input_validation import ProfileError

MAX_REVISION = (1 << 53) - 1
MAX_BYTES = 48 * 1024
ROW_FIELDS = {"id", "category", "provider_id", "environment_id", "label", "locator_kind", "path", "enabled", "binding_state"}
EMPTY = {"revision": 0, "registrations": []}


def invalid(field="observations", *, reason="invalid_config", status=422):
    raise ProfileError(reason, field, status=status)


def clean_text(value, minimum, maximum):
    return (type(value) is str and minimum <= len(value) <= maximum
            and not any(unicodedata.category(char) in ("Cc", "Cs") for char in value))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def validate(value):
    if (type(value) is not dict or set(value) != {"revision", "registrations"}
            or type(value["revision"]) is not int or not 0 <= value["revision"] <= MAX_REVISION
            or type(value["registrations"]) is not list or len(value["registrations"]) > 32
            or value["revision"] == 0 and value["registrations"]):
        invalid()
    ids, paths = set(), set()
    for row in value["registrations"]:
        if type(row) is not dict or set(row) != ROW_FIELDS:
            invalid()
        identity = row["id"]
        if type(identity) is not str or not re.fullmatch(r"[0-9a-f]{32}", identity) or identity in ids:
            invalid("observations.id")
        ids.add(identity)
        if (any(type(row[key]) is not str for key in ("category", "environment_id", "locator_kind", "binding_state"))
                or row["environment_id"] != "windows:local" or not clean_text(row["label"], 1, 80)
                or not clean_text(row["path"], 1, 2048) or not re.match(r"^[A-Za-z]:[\\/]", row["path"])
                or type(row["enabled"]) is not bool
                or row["binding_state"] not in ("confirmed", "rebind_required")):
            invalid()
        if row["category"] == "builtin":
            allowed = {"codex": {"npm_command", "codex_desktop"}, "claude": {"npm_command"},
                       "pi": {"npm_command"}, "hermes": {"hermes_launcher"}}
            if type(row["provider_id"]) is not str or row["locator_kind"] not in allowed.get(row["provider_id"], set()):
                invalid("observations.locator_kind")
        elif row["category"] == "custom_exe":
            if row["provider_id"] is not None or row["locator_kind"] != "executable" or ntpath.splitext(row["path"])[1].lower() != ".exe":
                invalid("observations.path")
        else:
            invalid("observations.category")
        path = ntpath.normcase(ntpath.normpath(row["path"]))
        if path in paths:
            invalid("observations.path", reason="duplicate_installation", status=409)
        paths.add(path)
    result = copy.deepcopy(value)
    result["registrations"].sort(key=lambda item: item["id"])
    if len(canonical(result)) > MAX_BYTES:
        invalid()
    return result


def from_config(config):
    return validate(config.get("observations", EMPTY))


def pair(value):
    value = validate(value)
    return {"config_revision": value["revision"], "config_hash": hashlib.sha256(canonical(value)).hexdigest()}


def restore_intent(value):
    result = validate(value)
    if result["revision"] == MAX_REVISION:
        invalid("observations.revision")
    result["revision"] += 1
    for row in result["registrations"]:
        row["binding_state"] = "rebind_required"
    return validate(result)
