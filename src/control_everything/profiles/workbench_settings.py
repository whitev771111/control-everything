"""Versioned workbench intent, independent of project storage and discovery."""

import copy
import re

from control_everything.input_validation import ProfileError

MAX_REVISION = (1 << 53) - 1
EMPTY = {"revision": 0, "sources": {"claude": {"enabled": False, "path": None},
         "opencode": {"enabled": False, "path": None}}, "excluded_project_ids": []}


def local_path(value):
    return (type(value) is str and 1 <= len(value) <= 2048
            and re.match(r"^[A-Za-z]:[\\/]", value) is not None
            and not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value))


def validate(value):
    if (type(value) is not dict or set(value) != set(EMPTY)
            or type(value["revision"]) is not int or not 0 <= value["revision"] <= MAX_REVISION
            or type(value["sources"]) is not dict or set(value["sources"]) != {"claude", "opencode"}
            or type(value["excluded_project_ids"]) is not list or len(value["excluded_project_ids"]) > 1000):
        raise ProfileError("invalid_config", "workbench")
    for source in value["sources"].values():
        if (type(source) is not dict or set(source) != {"enabled", "path"}
                or type(source["enabled"]) is not bool
                or source["path"] is not None and not local_path(source["path"])
                or source["enabled"] and source["path"] is None):
            raise ProfileError("invalid_config", "workbench.sources")
    if any(type(identity) is not str or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", identity) is None
           for identity in value["excluded_project_ids"]):
        raise ProfileError("invalid_config", "workbench.excluded_project_ids")
    result = copy.deepcopy(value)
    result["excluded_project_ids"] = sorted(set(result["excluded_project_ids"]))
    return result


def from_config(config):
    return validate(config.get("workbench", EMPTY))
