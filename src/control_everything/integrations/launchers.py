"""Explicit local work entrances, selected only by registered ids."""

import copy
import ctypes
import ntpath
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen


def validate_launcher_target(kind, target):
    """Validate syntax without reading files or executing a configured target."""
    error = "unsafe_batch_path" if kind == "cmd" else "invalid_target"
    if (not isinstance(target, str) or not 1 <= len(target) <= 2048
            or any(ord(char) < 32 or ord(char) == 127 for char in target)):
        raise ValueError(error)
    if kind in {"cmd", "py"}:
        value = target.replace("/", "\\")
        if not re.match(r"^[A-Za-z]:\\", value):
            raise ValueError(error)
        parts = value[3:].split("\\")
        forbidden = '"<>|?*:' + ("&^%!()" if kind == "cmd" else "")
        if (any(char in value[2:] for char in forbidden)
                or any(not part or part in {".", ".."} or part.endswith((".", " "))
                       or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
                       for part in parts)):
            raise ValueError(error)
        if ntpath.splitext(value)[1].lower() not in ({".cmd", ".bat"} if kind == "cmd" else {".py", ".pyw"}):
            raise ValueError("unsupported_target_type")
        return value
    if kind == "task":
        return target
    if kind == "url":
        try:
            parsed = urlsplit(target)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or any(char.isspace() for char in target) or "\\" in target):
                raise ValueError(error)
            parsed.port
        except ValueError:
            raise ValueError(error) from None
        return target
    raise ValueError("unsupported_target_type")


def system_directory():
    """Use Windows' system directory, never PATH or an environment override."""
    if os.name != "nt":
        raise OSError("unsupported_platform")
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError("system_directory_unavailable")
    return Path(buffer.value)


def batch_command_line(target, cmd_exe):
    target = validate_launcher_target("cmd", target)
    return f'"{cmd_exe}" /d /v:off /s /k ""{target}""'


def reachable(url):
    try:
        with urlopen(url, timeout=0.6) as response:
            return 200 <= response.status < 500
    except Exception:
        return False


def process_up(name):
    try:
        completed = subprocess.run([str(system_directory() / "tasklist.exe"), "/FI", f"IMAGENAME eq {name}", "/NH"],
                                   capture_output=True, timeout=3, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = (completed.stdout or b"").decode("oem", "replace")
    return name.lower() in output.lower()


def status_of(item):
    if item["kind"] in {"cmd", "py"} and not Path(item["target"]).is_file():
        return "missing"
    probe = item["probe"]
    if probe.startswith(("http://", "https://")):
        return "up" if reachable(probe) else "ready"
    if probe.lower().endswith(".exe"):
        return "up" if process_up(probe) else "ready"
    return "ready"


class LauncherCatalog:
    def __init__(self, config=None):
        self.config = copy.deepcopy(config) if config and config.get("enabled") else None
        self.groups = copy.deepcopy(self.config["groups"]) if self.config else []
        self.items = copy.deepcopy(self.config["items"]) if self.config else []
        self.last_error = None

    def catalog(self):
        return [{**{key: item[key] for key in ("id", "group", "name", "blurb")}, "status": status_of(item)}
                for item in self.items]

    def start(self, launcher_id, spawn=subprocess.Popen):
        self.last_error = None
        item = next((entry for entry in self.items if entry["id"] == launcher_id), None)
        if item is None:
            self.last_error = "unknown_launcher" if self.config else "not_configured"
            return False
        try:
            kind = item["kind"]
            target = validate_launcher_target(kind, item["target"])
            if kind in {"cmd", "task"} and os.name != "nt":
                self.last_error = "unsupported_platform"
                return False
            if kind in {"cmd", "py"}:
                path = Path(target)
                if not path.is_file():
                    self.last_error = "missing"
                    return False
                target = validate_launcher_target(kind, str(path.resolve(strict=True)))
                if not Path(target).is_file():
                    self.last_error = "missing"
                    return False
            if kind == "cmd":
                cmd_exe = system_directory() / "cmd.exe"
                spawn(batch_command_line(target, cmd_exe), executable=str(cmd_exe), shell=False,
                      creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)
            elif kind == "py":
                spawn([str(Path(sys.executable).resolve(strict=True)), target], shell=False,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif kind == "task":
                spawn([str(system_directory() / "schtasks.exe"), "/Run", "/TN", target], shell=False,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.startfile(target)
            return True
        except ValueError as error:
            self.last_error = str(error)
        except OSError:
            self.last_error = "launch_failed"
        return False


# Legacy module callers have no implicit personal configuration.
CATALOG = ()
GROUPS = ()


def catalog():
    return LauncherCatalog().catalog()


def start(launcher_id, spawn=subprocess.Popen):
    return LauncherCatalog().start(launcher_id, spawn=spawn)
