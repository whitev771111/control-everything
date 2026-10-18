"""Pure project validation and ownership rules; no storage or service calls."""

import math
import ntpath
import re

from control_everything.input_validation import ProfileError, absolute_path


def project_id_valid(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value) is not None


def path_key(value):
    value = ntpath.normcase(ntpath.normpath(value))
    if value.startswith("\\\\?\\unc\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\") and re.match(r"[a-z]:\\", value[4:]):
        return value[4:]
    return value


def imported_id_valid(value):
    return type(value) is str and re.fullmatch(r"(?:cl|oc)-[0-9a-f]{24}", value) is not None


def validate_project(body, *, restore=False, allow_imported_id=False):
    fields = {"project_id", "name", "directories", "source"} if restore else {"id", "name", "archived", "directories", "source"}
    def invalid(field, reason="invalid_project"):
        raise ProfileError(reason, field)
    if not isinstance(body, dict) or set(body) != fields:
        invalid("project")
    identity = body["project_id"] if restore else body["id"]
    if identity is not None and (not project_id_valid(identity) or identity.startswith("cx-")
            or identity.startswith(("cl-", "oc-")) and not (allow_imported_id and imported_id_valid(identity))):
        invalid("id")
    if restore and identity is None:
        invalid("project_id")
    name = body["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
        invalid("name")
    archived = False if restore else body["archived"]
    if type(archived) is not bool or identity is None and archived:
        invalid("archived")
    directories = body["directories"]
    if not isinstance(directories, list) or not 1 <= len(directories) <= 8:
        invalid("directories")
    ids, paths, checked = set(), set(), []
    for index, binding in enumerate(directories):
        field = f"directories[{index}]"
        if not isinstance(binding, dict) or set(binding) != {"id", "path"}:
            invalid(field)
        key, path = binding["id"], binding["path"]
        if (not isinstance(key, str) or key != "main" and not re.fullmatch(r"d-[0-9a-f]{32}", key)
                or key in ids or not absolute_path(path)):
            invalid(field)
        normalized = path_key(path)
        if normalized in paths:
            invalid(field + ".path")
        ids.add(key)
        paths.add(normalized)
        checked.append({"id": key, "path": path, "path_key": normalized})
    if "main" not in ids:
        invalid("directories.main")
    source = body["source"]
    if not isinstance(source, dict) or set(source) != {"kind", "directory_id", "relative_path"}:
        invalid("source", "invalid_source")
    kind, binding, relative = (source[key] for key in ("kind", "directory_id", "relative_path"))
    if kind not in ("none", "wfc", "text", "json"):
        invalid("source.kind", "invalid_source")
    if kind == "none":
        if binding is not None or relative is not None:
            invalid("source", "invalid_source")
    else:
        if not isinstance(binding, str) or binding not in ids:
            invalid("source.directory_id", "source_binding_missing")
        if (not isinstance(relative, str) or not 1 <= len(relative) <= 512 or "\x00" in relative
                or "\\" in relative or ":" in relative or relative.startswith("/")
                or ".." in relative.split("/") or kind == "wfc" and relative != "docs/current"):
            invalid("source.relative_path", "invalid_source")
    return {"id": identity, "name": name.strip(), "archived": archived, "directories": checked, "source": source}


def validate_stored_imported_project(body):
    if not isinstance(body, dict) or not imported_id_valid(body.get("id")):
        raise ProfileError("invalid_project", "id")
    return validate_project(body, allow_imported_id=True)


def validate_candidate_project(body):
    value = validate_stored_imported_project(body)
    if value["archived"] or value["source"] != {"kind": "none", "directory_id": None, "relative_path": None}:
        raise ProfileError("invalid_project", "source")
    return value


def validate_codex_project(body):
    """Validate an internal projection or restored marker, never a public project edit."""
    if (not isinstance(body, dict) or not isinstance(body.get("id"), str)
            or re.fullmatch(r"cx-[0-9a-f]{24}", body["id"]) is None
            or body.get("source") != {"kind": "codex", "directory_id": None, "relative_path": None}):
        raise ProfileError("invalid_project", "source")
    checked = validate_project({"id": "internal-codex", "name": body.get("name"),
        "archived": body.get("archived", False), "directories": body.get("directories"),
        "source": {"kind": "none", "directory_id": None, "relative_path": None}})
    return {**checked, "id": body["id"], "source": dict(body["source"])}


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


def observation_owner(cwd, projects):
    matches = []
    for project in projects:
        if project.get("archived"):
            continue
        for binding in project["directories"]:
            if inside_project(cwd, binding["path"]):
                matches.append((len(path_key(binding["path"])), project["id"]))
    if not matches:
        return None
    longest = max(length for length, _identity in matches)
    owners = {identity for length, identity in matches if length == longest}
    return next(iter(owners)) if len(owners) == 1 else None
