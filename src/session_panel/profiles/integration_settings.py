"""Strict, offline validation for the four supported local integrations."""

import copy
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from session_panel.integrations.launchers import validate_launcher_target
from session_panel.sessions.source import ZELLIJ_VERSION, validate_remote_payload

INTEGRATION_NAMES = ("remote", "terminal", "workbench", "launchers")
EMPTY_INTEGRATIONS = dict.fromkeys(INTEGRATION_NAMES)


class InvalidSettings(ValueError):
    def __init__(self, field, message=None):
        super().__init__(field)
        self.field, self.message = field, message


def require(condition, field, message=None):
    if not condition:
        raise InvalidSettings(field, message)


def text(value, field, limit=2048, *, empty=False):
    require(isinstance(value, str) and (empty or bool(value)) and len(value) <= limit
            and not any(ord(char) < 32 or ord(char) == 127 for char in value), field)


def identifier(value, field):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value)
            and value not in (".", ".."), field)


def keys(value, expected, field):
    require(isinstance(value, dict) and set(value) == set(expected), field)


def local_path(value, field):
    text(value, field)
    require(Path(value).is_absolute(), field)


def posix_path(value, field):
    text(value, field)
    require(PurePosixPath(value).is_absolute() and not value.startswith("//") and str(PurePosixPath(value)) == value
            and not any(part in (".", "..") for part in value.split("/")), field)


def http_url(value, field):
    text(value, field)
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
                 and not parsed.username and not parsed.password
                 and not any(char.isspace() for char in value))
        parsed.port
    except ValueError:
        valid = False
    require(valid, field)


def validate_integrations(value):
    keys(value, INTEGRATION_NAMES, "integrations")
    result = copy.deepcopy(value)
    for name, item in result.items():
        if item is None:
            continue
        field = f"integrations.{name}"
        require(isinstance(item, dict) and type(item.get("enabled")) is bool, field + ".enabled")
        if name == "remote":
            keys(item, ("enabled", "source", "home", "ssh", "reader_python", "reader_mcp_version", "slot_files"), field)
            payload = {key: item[key] for key in ("source", "home", "reader_mcp_version", "slot_files")}
            payload.update(mode="observe", request=None)
            try:
                validate_remote_payload(payload)
            except ValueError as error:
                raise InvalidSettings(field + "." + str(error)) from None
            text(item["ssh"], field + ".ssh")
            require(Path(item["ssh"]).is_absolute() or re.fullmatch(r"[A-Za-z0-9_.-]+", item["ssh"]), field + ".ssh")
            posix_path(item["reader_python"], field + ".reader_python")
        elif name == "terminal":
            keys(item, ("enabled", "zellij", "session", "tab", "version", "pane_script", "ssh_executables"), field)
            local_path(item["zellij"], field + ".zellij")
            identifier(item["session"], field + ".session")
            text(item["tab"], field + ".tab", 128)
            require(item["version"] == ZELLIJ_VERSION, field + ".version", "unsupported_version")
            script = item["pane_script"]
            text(script, field + ".pane_script")
            require(re.fullmatch(r"(?:/|~/)[\w./-]+", script)
                    and not any(part in (".", "..") for part in script.split("/")), field + ".pane_script")
            executables = item["ssh_executables"]
            require(isinstance(executables, list) and 1 <= len(executables) <= 4, field + ".ssh_executables")
            for index, executable in enumerate(executables):
                text(executable, f"{field}.ssh_executables.{index}")
                require('"' not in executable and (Path(executable).is_absolute()
                        or re.fullmatch(r"[A-Za-z0-9_.-]+", executable)), f"{field}.ssh_executables.{index}")
            require(len(set(executables)) == len(executables), field + ".ssh_executables")
        elif name == "workbench":
            paths = ("wezterm_gui", "wezterm_lua", "zellij_config", "workdir", "unmanaged_mux")
            keys(item, ("enabled", "pins", *paths), field)
            for key in paths:
                local_path(item[key], field + "." + key)
            keys(item["pins"], ("zellij", "wezterm_gui", "wezterm_lua", "zellij_config"), field + ".pins")
            for role, digest in item["pins"].items():
                require(isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest), field + ".pins." + role)
        else:
            keys(item, ("enabled", "groups", "items"), field)
            require(isinstance(item["groups"], list) and len(item["groups"]) <= 16, field + ".groups")
            require(isinstance(item["items"], list) and len(item["items"]) <= 32, field + ".items")
            groups, ids = set(), set()
            for index, group in enumerate(item["groups"]):
                group_field = f"{field}.groups.{index}"
                keys(group, ("id", "name", "blurb"), group_field)
                identifier(group["id"], group_field + ".id")
                require(group["id"] not in groups, group_field + ".id")
                groups.add(group["id"])
                text(group["name"], group_field + ".name", 128)
                text(group["blurb"], group_field + ".blurb", 512, empty=True)
            for index, entry in enumerate(item["items"]):
                entry_field = f"{field}.items.{index}"
                keys(entry, ("id", "group", "name", "blurb", "kind", "target", "probe"), entry_field)
                identifier(entry["id"], entry_field + ".id")
                require(entry["id"] not in ids, entry_field + ".id")
                ids.add(entry["id"])
                require(isinstance(entry["group"], str) and entry["group"] in groups, entry_field + ".group")
                text(entry["name"], entry_field + ".name", 128)
                text(entry["blurb"], entry_field + ".blurb", 512, empty=True)
                require(isinstance(entry["kind"], str) and entry["kind"] in ("cmd", "py", "task", "url"), entry_field + ".kind")
                try:
                    entry["target"] = validate_launcher_target(entry["kind"], entry["target"])
                except ValueError as error:
                    raise InvalidSettings(entry_field + ".target", str(error)) from None
                text(entry["probe"], entry_field + ".probe", empty=True)
                if entry["probe"].startswith(("http:", "https:")):
                    http_url(entry["probe"], entry_field + ".probe")
                else:
                    require(not entry["probe"] or re.fullmatch(r"[A-Za-z0-9_.-]+\.exe", entry["probe"], re.I), entry_field + ".probe")
    for child, parent in (("terminal", "remote"), ("workbench", "terminal")):
        if result[child] is not None and result[child]["enabled"]:
            require(result[parent] is not None and result[parent]["enabled"], f"integrations.{child}.enabled", f"requires_{parent}")
    return result


def integration_summary(value):
    result = {}
    for name in INTEGRATION_NAMES:
        item = value[name]
        enabled = item is not None and item["enabled"]
        status = "not_configured" if item is None else "configured" if enabled else "disabled"
        result[name] = {"configured": item is not None, "enabled": enabled, "status": status,
                        "reason": "reader_upgrade_recommended" if name == "remote" and item is not None
                        and item["reader_mcp_version"] == "1.26.0" else None}
    return result
