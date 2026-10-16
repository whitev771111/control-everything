"""Conservative local evidence and an explicit attach to the existing workbench."""

import copy
import ctypes
import hashlib
import json
import ntpath
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

from session_panel.paths import REPOSITORY_ROOT

from session_panel.sessions.source import TERMINAL_TIMEOUT

PUBLIC = ("session", "status", "server_state", "gui_state", "gui_count",
          "interactive_client_count", "unmanaged_mux_count", "client_scope", "watcher_count",
          "gui_visibility", "association_state", "observed_at", "age_ms", "can_connect",
          "can_focus", "reason", "actions_blocked")


def command_line_argv(text):
    if not isinstance(text, str) or not text or "\x00" in text:
        raise ValueError("unknown command line")
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(text, ctypes.byref(count))
    if not pointer:
        raise OSError(ctypes.get_last_error(), "CommandLineToArgvW failed")
    try:
        return [pointer[i] for i in range(count.value)]
    finally:
        kernel.LocalFree(pointer)


def normalized(value):
    return ntpath.normcase(ntpath.normpath(value)) if ntpath.isabs(value) else value


def profile(argv, expected):
    return [normalized(x) for x in argv] == [normalized(x) for x in expected]


def parse_sessions(text):
    if not isinstance(text, str) or not text.strip():
        return None
    result = {}
    pattern = r"([^\s\[\]\x00-\x1f\x7f]{1,128}) \[Created ((?:\d+(?:days?|h|m|s)\s*)+) ago\]( \(EXITED - attach to resurrect\))?"
    for line in text.strip().splitlines():
        match = re.fullmatch(pattern, line.strip())
        if not match or match[1] in result:
            return None
        result[match[1]] = "exited" if match[3] else "running"
    return result


def created_ticks(text):
    if not isinstance(text, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|\+00:00)", text):
        raise ValueError("unknown creation time")
    delta = datetime.fromisoformat(text.replace("Z", "+00:00")) - datetime(1601, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10000000 + delta.microseconds * 10


def process_identity(row):
    return tuple(row[x] for x in ("ProcessId", "ParentProcessId", "CreatedUtc", "Name", "ExecutablePath", "argv"))


def same_association(left, right):
    return all(left.get(key) == right.get(key) for key in ("_server", "_guis", "_attaches", "_mux", "_client_ids"))


def kernel32():
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, arguments, result in (
        ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        ("GetProcessTimes", [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
        ("TerminateProcess", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        ("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        ("GetExitCodeProcess", [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ("GetModuleFileNameW", [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD], wintypes.DWORD),
    ):
        method = getattr(dll, name); method.argtypes = arguments; method.restype = result
    return dll


def _native_interpreter():
    base = getattr(sys, "_base_executable", None)
    if not isinstance(base, str) or not ntpath.isabs(base) or not ntpath.splitdrive(base)[0]:
        raise ValueError("unknown native interpreter")
    image = ctypes.create_unicode_buffer(32768)
    length = kernel32().GetModuleFileNameW(None, image, len(image))
    if (not 0 < length < len(image) or len(image.value) != length or not ntpath.isabs(image.value) or not ntpath.splitdrive(image.value)[0]
            or normalized(base) != normalized(image.value)):
        raise ValueError("unknown native interpreter")
    return base


def process_times(handle):
    values = [wintypes.FILETIME() for _ in range(4)]
    if not kernel32().GetProcessTimes(handle, *(ctypes.byref(x) for x in values)):
        raise OSError(ctypes.get_last_error(), "GetProcessTimes failed")
    return tuple((x.dwHighDateTime << 32) | x.dwLowDateTime for x in values[:2])


class Workbench:
    def __init__(self, runner=subprocess.run, clock=time.monotonic, provider=None, popen=subprocess.Popen, *, config=None, terminal_config=None):
        self.runner, self.clock, self.popen = runner, clock, popen
        self.provider = provider if provider is not None else self._processes
        self.action_lock = threading.Lock()
        self.cleanup_latch = False
        self._owned_gui = None
        self.config = copy.deepcopy(config) if config and config.get("enabled") else None
        self.terminal_config = copy.deepcopy(terminal_config) if terminal_config and terminal_config.get("enabled") else None
        self.session = self.terminal_config["session"] if self.terminal_config else None
        if self.config and self.terminal_config:
            self.zellij = self.terminal_config["zellij"]
            self.version = self.terminal_config["version"]
            for name in ("wezterm_gui", "wezterm_lua", "zellij_config", "workdir", "unmanaged_mux"):
                setattr(self, name, self.config[name])
            self.marker = str(Path(tempfile.gettempdir()) / "zellij/contract_version_1" / self.session)
            base = [self.wezterm_gui, "--config-file", self.wezterm_lua, "start", "--always-new-process"]
            self.new_gui = [*base, "--no-auto-connect", "--cwd", self.workdir, "--", self.zellij, "attach"]
            self.observed_gui = [*base, "--cwd", self.workdir, "--", self.zellij, "attach", self.session]
            self.legacy_gui = [*self.observed_gui, "--create", "--force-run-commands"]
            self.observed_attach = [self.zellij, "attach", self.session]
            self.pins = copy.deepcopy(self.config["pins"])

    def remaining(self, deadline):
        value = deadline - self.clock()
        if value <= 0:
            raise subprocess.TimeoutExpired("workbench observation", 0)
        return value

    def _run(self, arguments, deadline):
        result = self.runner([self.zellij, *arguments], capture_output=True, text=True,
                             encoding="utf-8", errors="strict", timeout=self.remaining(deadline),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ValueError("workbench command failed")
        return result.stdout

    def _processes(self, deadline):
        result = self.runner([_native_interpreter(), "-X", "utf8", "-B", "-m", "session_panel.native_processes"],
                             cwd=str(REPOSITORY_ROOT), shell=False,
                             capture_output=True, text=True, encoding="utf-8", errors="strict",
                             timeout=self.remaining(deadline), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ValueError("process census failed")
        return json.loads(result.stdout)

    def _rows(self, deadline):
        rows = self.provider(deadline)
        if not isinstance(rows, list):
            raise ValueError("unknown census")
        result, seen = [], set()
        for raw in rows:
            row = dict(raw)
            if (any(type(row.get(x)) is not int or not 0 <= row[x] <= 0xffffffff for x in ("ProcessId", "ParentProcessId"))
                    or row["ProcessId"] == 0 or row["ProcessId"] in seen
                    or any(not isinstance(row.get(x), str) or not row[x] for x in ("Name", "ExecutablePath", "CommandLine", "CreatedUtc"))):
                raise ValueError("unknown process identity")
            seen.add(row["ProcessId"])
            row["_ticks"] = created_ticks(row["CreatedUtc"])
            row["CreatedUtc"] = row["_ticks"]
            row["Name"] = row["Name"].casefold()
            row["ExecutablePath"] = normalized(row["ExecutablePath"])
            row["argv"] = tuple(normalized(x) for x in command_line_argv(row["CommandLine"]))
            result.append(row)
        self.remaining(deadline)
        return result

    def _classify(self, rows):
        servers, guis, attaches, mux = [], [], [], []
        probes = [[self.zellij, "--version"], [self.zellij, "list-sessions", "--no-formatting"],
                  [self.zellij, "--session", self.session, "action", "list-panes", "--all", "--json"],
                  [self.zellij, "--session", self.session, "action", "list-clients"]]
        for row in rows:
            argv, exe, name = row["argv"], row["ExecutablePath"], row["Name"]
            if name == "wezterm-mux-server.exe" and exe == normalized(self.unmanaged_mux) and profile(argv, [self.unmanaged_mux]):
                mux.append(row)
            elif name == "wezterm-gui.exe" and exe == normalized(self.wezterm_gui) and (profile(argv, self.new_gui) or profile(argv, self.legacy_gui) or profile(argv, self.observed_gui)):
                guis.append(row)
            elif name == "zellij.exe" and exe == normalized(self.zellij):
                if profile(argv, [self.zellij, "--server", self.marker]):
                    servers.append(row)
                elif profile(argv, [self.zellij, "attach"]) or profile(argv, [self.zellij, "attach", self.session, "--create", "--force-run-commands"]) or profile(argv, self.observed_attach):
                    attaches.append(row)
                elif not any(profile(argv, probe) for probe in probes):
                    raise ValueError("unaccepted zellij process")
            else:
                raise ValueError("unaccepted terminal process")
        return servers, guis, attaches, mux

    def _marker_pid(self):
        text = Path(self.marker).read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9]{1,10}", text) or not 0 < int(text) <= 0xffffffff:
            raise ValueError("unknown marker")
        return int(text)

    def _state(self, started, observed_at, reason="workbench_observation_unknown", **fields):
        age = max(0, int((self.clock() - started) * 1000))
        return {"session": self.session, "status": "unknown", "server_state": "unknown",
                "gui_state": "unknown", "gui_count": None, "interactive_client_count": None,
                "unmanaged_mux_count": None, "client_scope": "interactive_only", "watcher_count": None,
                "gui_visibility": "unverified", "association_state": "unknown", "observed_at": observed_at,
                "age_ms": age, "can_connect": False, "can_focus": False,
                "reason": "cleanup_unverified" if self.cleanup_latch else reason,
                "actions_blocked": self.cleanup_latch, **fields}

    def public(self, state):
        return {key: state[key] for key in PUBLIC}

    def observe(self, deadline=None):
        started, observed_at = self.clock(), datetime.now(timezone.utc).isoformat()
        if not self.config or not self.terminal_config:
            return self._state(started, observed_at, "not_configured")
        deadline = min(deadline, started + TERMINAL_TIMEOUT) if deadline is not None else started + TERMINAL_TIMEOUT
        try:
            if self._run(["--version"], deadline).strip() != f"zellij {self.version}":
                raise ValueError("version mismatch")
            sessions = parse_sessions(self._run(["list-sessions", "--no-formatting"], deadline))
            if sessions is None:
                raise ValueError("unknown sessions")
            server_state = sessions.get(self.session, "missing")
            if server_state != "running":
                return self._state(started, observed_at, "workbench_not_running", server_state=server_state)
            if [name for name, status in sessions.items() if status == "running"] != [self.session]:
                return self._state(started, observed_at, "workbench_multiple_sessions", server_state="running")
            marker_before, before = self._marker_pid(), self._classify(self._rows(deadline))
            from session_panel.sessions.terminal import parse_clients
            clients = parse_clients(self._run(["--session", self.session, "action", "list-clients"], deadline))
            marker_after, after = self._marker_pid(), self._classify(self._rows(deadline))
            fingerprints = lambda values: tuple(tuple(sorted(process_identity(row) for row in group)) for group in values)
            before_ids, after_ids = fingerprints(before), fingerprints(after)
            if clients is None or marker_before != marker_after or before_ids[0] != after_ids[0] or before_ids[3] != after_ids[3]:
                raise ValueError("unstable observation")
            transition = before_ids != after_ids
            if transition and not all(set(before_ids[i]).issubset(after_ids[i]) and len(after_ids[i]) <= 1 for i in (1, 2)):
                raise ValueError("unstable association")
            servers, guis, attaches, mux = after
            if len(servers) != 1 or servers[0]["ProcessId"] != marker_after:
                raise ValueError("unknown server")
            disconnected = not guis and not attaches and not clients
            unique = len(guis) == len(attaches) == len(clients) == 1
            if unique:
                gui, client = guis[0], attaches[0]
                if profile(gui["argv"], self.new_gui):
                    expected = [self.zellij, "attach"]
                elif profile(gui["argv"], self.observed_gui):
                    expected = self.observed_attach
                else:
                    expected = [self.zellij, "attach", self.session, "--create", "--force-run-commands"]
                unique = (client["ParentProcessId"] == gui["ProcessId"] and client["_ticks"] >= gui["_ticks"]
                          and profile(client["argv"], expected))
            state = self._state(started, observed_at, "workbench_association_unknown", status="observed",
                                server_state="running", gui_state="absent" if not guis else "observed" if len(guis) == 1 else "multiple",
                                gui_count=len(guis), interactive_client_count=len(clients), unmanaged_mux_count=len(mux),
                                association_state="unique_observed" if unique else "disconnected" if disconnected else "ambiguous",
                                _server=process_identity(servers[0]), _guis=after_ids[1],
                                _attaches=after_ids[2], _mux=after_ids[3],
                                _client_ids=tuple(row["client_id"] for row in clients), _clients=clients, _rows=after)
            fresh = state["age_ms"] <= 5000 and not self.cleanup_latch and not transition
            state.update(can_connect=disconnected and fresh, can_focus=unique and fresh,
                         reason="cleanup_unverified" if self.cleanup_latch else None if unique or disconnected else "workbench_association_unknown")
            if transition:
                state.update(status="unknown", reason="workbench_transitioning", _transition=True)
            return state
        except subprocess.TimeoutExpired:
            return self._state(started, observed_at, "workbench_timeout")
        except (OSError, ValueError, UnicodeError, TypeError, AttributeError, KeyError):
            return self._state(started, observed_at)

    def public_status(self):
        if not self.action_lock.acquire(blocking=False):
            return self.public(self._state(self.clock(), datetime.now(timezone.utc).isoformat(), "terminal_action_busy"))
        try:
            return self.public(self.observe())
        finally:
            self.action_lock.release()

    def integrity(self, deadline):
        if not self.config or not self.terminal_config:
            return False
        roles = {"zellij", "wezterm_gui", "wezterm_lua", "zellij_config"}
        if set(self.pins) != roles or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value)
                                         for value in self.pins.values()):
            return False
        try:
            for role, wanted in self.pins.items():
                path = getattr(self, role)
                digest = hashlib.sha256()
                with open(path, "rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        self.remaining(deadline); digest.update(block)
                if digest.hexdigest().upper() != wanted.upper():
                    return False
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _wait_connected(self, deadline, expected_server, expected_gui, expected_mux):
        while self.clock() < deadline:
            state = self.observe(deadline)
            if state.get("_server") is None:
                return state
            if (state["_server"] != expected_server or state.get("_mux") != expected_mux
                    or self._owned_gui["process"].poll() is not None):
                return self._state(self.clock() - state["age_ms"] / 1000, state["observed_at"], "workbench_association_changed")
            guis, attaches = state["_rows"][1:3]
            owned_association = (len(guis) == 1 and guis[0]["ProcessId"] == expected_gui
                                 and guis[0]["_ticks"] // 10 == self._owned_gui["created"] // 10
                                 and profile(guis[0]["argv"], self.new_gui)
                                 and all(row["ParentProcessId"] == expected_gui and row["_ticks"] >= guis[0]["_ticks"]
                                         and profile(row["argv"], [self.zellij, "attach"]) for row in attaches))
            if state["can_focus"]:
                if owned_association:
                    return state
                return self._state(self.clock(), datetime.now(timezone.utc).isoformat())
            starting = (state["association_state"] == "disconnected" and state["gui_count"] == 0
                        and state["interactive_client_count"] == 0)
            starting = starting or (state["association_state"] == "ambiguous" and state["gui_count"] == 1
                                   and state["interactive_client_count"] in (0, 1)
                                   and owned_association)
            if state.get("_transition") and state["gui_count"] == 1 and state["interactive_client_count"] in (0, 1):
                starting = owned_association
            if not starting:
                return state
            time.sleep(min(0.25, max(0, deadline - self.clock())))
        return self._state(self.clock(), datetime.now(timezone.utc).isoformat(), "workbench_connect_timeout")

    def _terminate_client(self, row, deadline):
        dll = kernel32()
        handle = dll.OpenProcess(0x101001, False, row["ProcessId"])
        if not handle:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            actual_created = process_times(handle)[0]
            if (actual_created // 10 != row["_ticks"] // 10 or actual_created < row["_gui_created"]
                    or row["_gui_exited"] is not None and actual_created > row["_gui_exited"]):
                raise ValueError("client identity changed")
            if dll.WaitForSingleObject(handle, 0) != 0 and not dll.TerminateProcess(handle, 1):
                raise OSError(ctypes.get_last_error(), "TerminateProcess failed")
            if dll.WaitForSingleObject(handle, int(self.remaining(deadline) * 1000)) != 0:
                raise ValueError("client exit unverified")
            code = wintypes.DWORD()
            if not dll.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise OSError(ctypes.get_last_error(), "client exit unknown")
        finally:
            dll.CloseHandle(handle)

    def _cleanup_owned(self, owned, deadline):
        verified = True
        process, created, exited = owned["process"], owned.get("created"), None
        def sweep():
            rows = self._rows(deadline)
            self._classify(rows)
            for row in rows:
                if row["ParentProcessId"] != process.pid:
                    if (created is not None and row["Name"] == "zellij.exe" and profile(row["argv"], [self.zellij, "attach"])
                            and row["_ticks"] >= created // 10 * 10 and (exited is None or row["_ticks"] <= exited)):
                        raise ValueError("attach ownership unknown")
                    continue
                if (created is None or row["Name"] != "zellij.exe" or row["ExecutablePath"] != normalized(self.zellij)
                        or not profile(row["argv"], [self.zellij, "attach"]) or row["_ticks"] < created // 10 * 10
                        or (exited is not None and row["_ticks"] > exited)):
                    raise ValueError("owned child identity unknown")
                row.update(_gui_created=created, _gui_exited=exited)
                self._terminate_client(row, deadline)
        try:
            sweep()
        except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.TimeoutExpired):
            verified = False
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=self.remaining(deadline))
            actual_created, exited = process_times(process._handle)
            if created is None or actual_created != created or not exited:
                verified = False
            sweep()
            final = self._rows(deadline)
            self._classify(final)
            if any(row["ProcessId"] == process.pid or row["ParentProcessId"] == process.pid for row in final):
                verified = False
        except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.TimeoutExpired):
            verified = False
        return "verified" if verified else "unverified"

    def connect(self):
        state = self._state(self.clock(), datetime.now(timezone.utc).isoformat(), "terminal_action_busy")
        result = {"connected": False, "launched": False, "status": "unknown", "reason": state["reason"],
                  "workbench": self.public(state), "cleanup": "not_needed"}
        if not self.config or not self.terminal_config:
            state = self._state(self.clock(), datetime.now(timezone.utc).isoformat(), "not_configured")
            return {**result, "reason": "not_configured", "workbench": self.public(state)}
        if not self.action_lock.acquire(blocking=False):
            return result
        try:
            if self.cleanup_latch:
                return {**result, "reason": "cleanup_unverified"}
            deadline = self.clock() + 5
            if not self.integrity(deadline):
                return {**result, "reason": "workbench_integrity_failed"}
            before = self.observe(deadline)
            result.update(workbench=self.public(before), reason=before["reason"])
            if before["can_focus"]:
                return {**result, "connected": True, "status": "unique_observed"}
            if not before["can_connect"]:
                return result
            env = os.environ.copy()
            for name in ("WEZTERM_UNIX_SOCKET", "WEZTERM_PANE", "ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_SOCKET_DIR"):
                env.pop(name, None)
            env.update(WEZTERM_CONFIG_FILE=self.wezterm_lua, ZELLIJ_CONFIG_DIR=str(Path(self.zellij_config).parent))
            try:
                process = self.popen(self.new_gui, shell=False, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                return {**result, "reason": "workbench_launch_failed"}
            owned = self._owned_gui = {"process": process, "created": None}
            result["launched"] = True
            try:
                owned["created"] = process_times(process._handle)[0]
                after = self._wait_connected(self.clock() + 8, before["_server"], process.pid, before["_mux"])
                result.update(workbench=self.public(after), reason=after["reason"] or "workbench_connect_unverified")
                if (after["can_focus"] and after.get("_server") == before["_server"] and after.get("_mux") == before["_mux"]
                        and after["_guis"][0][0] == process.pid and after["_guis"][0][2] // 10 == owned["created"] // 10
                        and profile(after["_rows"][1][0]["argv"], self.new_gui)):
                    return {**result, "connected": True, "status": "unique_observed", "reason": None}
            except (OSError, ValueError, UnicodeError, TypeError, KeyError, AttributeError, subprocess.TimeoutExpired):
                result["reason"] = "workbench_connect_unverified"
            result["cleanup"] = self._cleanup_owned(owned, self.clock() + 3)
            if result["cleanup"] != "verified":
                self.cleanup_latch = True
                result["reason"] = "cleanup_unverified"
                result["workbench"].update(actions_blocked=True, can_connect=False, can_focus=False, reason="cleanup_unverified")
            return result
        finally:
            self.action_lock.release()
