"""Strict current-pane matching and the single explicit focus action."""

import copy
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from control_everything.sessions.source import (
    TERMINAL_TIMEOUT, CACHE_SECONDS, canonical_uuid,
)


def terminal_id(pane):
    if pane.get("is_plugin") is not False or type(pane.get("id")) is not int or pane["id"] < 0:
        return None
    return f"terminal_{pane['id']}"


def parse_slot_command(pane, terminal_config=None, remote_config=None):
    if (not terminal_config or not terminal_config.get("enabled")
            or not remote_config or not remote_config.get("enabled")
            or pane.get("tab_name") != terminal_config["tab"] or not terminal_id(pane)):
        return None
    command = pane.get("terminal_command")
    if not isinstance(command, str):
        return None
    for executable in terminal_config["ssh_executables"]:
        forms = [executable] if not any(char.isspace() for char in executable) else []
        forms.append(f'"{executable}"')
        for slot in remote_config["slot_files"]:
            suffix = f' -t {remote_config["source"]["host"]} bash {terminal_config["pane_script"]} {slot}'
            if any(command == form + suffix for form in forms):
                return slot
    return None


def parse_clients(text):
    if not isinstance(text, str):
        return None
    lines = text.strip().splitlines()
    if not lines or lines[0].split() != ["CLIENT_ID", "ZELLIJ_PANE_ID", "RUNNING_COMMAND"]:
        return None
    clients, seen = [], set()
    for line in lines[1:]:
        fields = line.split(maxsplit=2)
        if (len(fields) < 2 or not re.fullmatch(r"[0-9]{1,5}", fields[0]) or not 0 <= int(fields[0]) <= 65535
                or int(fields[0]) in seen or not re.fullmatch(r"(?:terminal|plugin)_[0-9]+", fields[1])):
            return None
        seen.add(int(fields[0]))
        clients.append({"client_id": fields[0], "pane_id": fields[1]})
    return clients


def _focus_candidates(panes, fresh, terminal_config=None, remote_config=None):
    target = [row for row in panes if terminal_id(row) == fresh["pane_id"]]
    if len(target) != 1 or parse_slot_command(target[0], terminal_config, remote_config) != fresh["slot"]:
        return None
    tab_id = target[0].get("tab_id")
    rows = [row for row in panes if row.get("tab_name") == terminal_config["tab"]]
    flags = ("is_plugin", "is_selectable", "is_floating", "is_suppressed", "is_fullscreen", "exited", "is_held")
    geometry = ("pane_x", "pane_y", "pane_rows", "pane_columns")
    if (type(tab_id) is not int or tab_id < 0 or target[0].get("is_held") is not False
            or any(row.get("tab_id") != tab_id or type(row.get("tab_id")) is not int
                   or type(row.get("id")) is not int or row["id"] < 0
                   or any(type(row.get(flag)) is not bool for flag in flags)
                   or any(type(row.get(field)) is not int for field in geometry)
                   or row["pane_x"] < 0 or row["pane_y"] < 0 or row["pane_rows"] <= 0 or row["pane_columns"] <= 0
                   or row.get("terminal_command") is not None and not isinstance(row["terminal_command"], str)
                   for row in rows)
            or any(row.get("tab_id") == tab_id and row.get("tab_name") != terminal_config["tab"] for row in panes)
            or any(row["is_floating"] or row["is_fullscreen"] for row in rows)
            or any(row["is_selectable"] and (row["is_plugin"] or row["is_suppressed"] or row["exited"]) for row in rows)):
        return None
    identities = [f"{'plugin' if row['is_plugin'] else 'terminal'}_{row['id']}" for row in rows]
    candidates = frozenset(identity for identity, row in zip(identities, rows) if row["is_selectable"])
    if (len(set(identities)) != len(rows) or fresh["pane_id"] not in candidates
            or len({(row["pane_y"], row["pane_x"]) for row in rows if row["is_selectable"]}) != len(candidates)):
        return None
    fingerprint = tuple(sorted((identity, row["tab_name"], *(row[flag] for flag in flags), row.get("terminal_command"),
                                tuple(row[field] for field in geometry))
                               for identity, row in zip(identities, rows)))
    return tab_id, candidates, fingerprint


class Terminal:
    def __init__(self, runner=subprocess.run, clock=time.monotonic, workbench=None, *, config=None, remote_config=None):
        self.runner, self.clock = runner, clock
        self.config = copy.deepcopy(config) if config and config.get("enabled") else None
        self.remote_config = copy.deepcopy(remote_config) if remote_config and remote_config.get("enabled") else None
        self.source = copy.deepcopy(self.remote_config["source"]) if self.remote_config else None
        self.workbench = workbench

    def _run(self, arguments, timeout=TERMINAL_TIMEOUT):
        return self.runner([self.config["zellij"], *arguments], capture_output=True, text=True,
                           encoding="utf-8", errors="strict", timeout=timeout)

    def list_panes(self, deadline=None):
        if not self.config or not self.remote_config:
            return {"ok": False, "reason": "not_configured"}
        started = self.clock()
        deadline = min(deadline, started + TERMINAL_TIMEOUT) if deadline is not None else started + TERMINAL_TIMEOUT
        try:
            remaining = deadline - self.clock()
            if remaining <= 0:
                return {"ok": False, "reason": "terminal_timeout"}
            version = self._run(["--version"], remaining)
            if version.returncode:
                return {"ok": False, "reason": "terminal_version_mismatch", "exit_code": version.returncode}
            if version.stdout.strip() != f"zellij {self.config['version']}":
                return {"ok": False, "reason": "terminal_version_mismatch"}
            remaining = deadline - self.clock()
            if remaining <= 0:
                return {"ok": False, "reason": "terminal_timeout"}
            result = self._run(["--session", self.config["session"], "action", "list-panes", "--all", "--json"], remaining)
            if result.returncode:
                return {"ok": False, "reason": "terminal_unavailable", "exit_code": result.returncode}
            inventory = json.loads(result.stdout)
            panes = inventory if isinstance(inventory, list) else inventory.get("panes")
            if not isinstance(panes, list) or not all(isinstance(pane, dict) for pane in panes):
                raise ValueError("invalid pane inventory")
            return {"ok": True, "panes": panes, "session": self.config["session"],
                    "version": self.config["version"]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": "terminal_timeout"}
        except (OSError, ValueError, UnicodeError, AttributeError):
            return {"ok": False, "reason": "terminal_unavailable"}

    def focus_session(self, service, session_id):
        canonical_uuid(session_id)
        result = {"focused": False, "status": "unknown", "session_id": session_id,
                  "source": self.source, "observed_at": datetime.now(timezone.utc).isoformat()}
        if not self.config or not self.remote_config:
            return {**result, "reason": "not_configured"}
        if self.workbench is None:
            return {**result, "reason": "capability_unavailable"}
        if not self.workbench.action_lock.acquire(blocking=False):
            return {**result, "reason": "terminal_action_busy"}
        try:
            if self.workbench.cleanup_latch:
                return {**result, "reason": "cleanup_unverified"}
            initial = self.workbench.observe()
            if not initial["can_focus"]:
                return {**result, "reason": initial["reason"] or "workbench_association_unknown"}
            return self._focus_session(service, session_id, initial, result)
        finally:
            self.workbench.action_lock.release()

    def connect_workbench(self):
        if not self.config or not self.remote_config or self.workbench is None:
            return {"connected": False, "launched": False, "status": "unknown",
                    "reason": "not_configured" if not self.config or not self.remote_config else "capability_unavailable"}
        return self.workbench.connect()

    def _focus_next(self, candidates, client_id, fresh, refresh_finished, deadline, current_pane_id):
        geometry = {entry[0]: entry[-1] for entry in candidates[2]}
        current = current_pane_id if current_pane_id in candidates[1] else None
        seen = {current} if current is not None else set()
        for step in range(len(candidates[1]) + 1):
            remaining = deadline - self.clock()
            if remaining <= 0 or fresh["age_ms"] + (self.clock() - refresh_finished) * 1000 > CACHE_SECONDS * 1000:
                return None
            if current == fresh["pane_id"]:
                return 0
            use_up = False
            if current is None:
                arguments = ["go-to-tab-by-id", str(candidates[0])]
            else:
                x, y, rows, columns = geometry[current]
                above = [identity for identity in candidates[1]
                         if geometry[identity][1] + geometry[identity][2] == y
                         and max(x, geometry[identity][0]) < min(x + columns, geometry[identity][0] + geometry[identity][3])]
                use_up = len(above) == 1 and above[0] == fresh["pane_id"]
                arguments = ["move-focus", "up"] if use_up else ["focus-next-pane"]
            action = self._run(["--session", self.config["session"], "action", *arguments], remaining)
            if action.returncode:
                return action.returncode
            remaining = deadline - self.clock()
            if remaining <= 0 or fresh["age_ms"] + (self.clock() - refresh_finished) * 1000 > CACHE_SECONDS * 1000:
                return None
            clients = self._run(["--session", self.config["session"], "action", "list-clients"], remaining)
            if clients.returncode:
                return clients.returncode
            values = parse_clients(clients.stdout)
            if values is None or len(values) != 1 or values[0]["client_id"] != client_id:
                return None
            after = self.list_panes(deadline)
            if not after.get("ok") and after.get("exit_code"):
                return after["exit_code"]
            if (not after.get("ok") or _focus_candidates(after["panes"], fresh, self.config, self.remote_config) != candidates
                    or self.clock() >= deadline
                    or fresh["age_ms"] + (self.clock() - refresh_finished) * 1000 > CACHE_SECONDS * 1000):
                return None
            pane_id = values[0]["pane_id"]
            if pane_id not in candidates[1]:
                return None
            if use_up and pane_id != fresh["pane_id"]:
                return None
            if pane_id == fresh["pane_id"]:
                return 0
            if pane_id in seen:
                return None
            seen.add(pane_id)
            current = pane_id
        return None

    def _focus_session(self, service, session_id, initial, result):
        from control_everything.sessions.workbench import same_association
        requested = service.get_session_status(session_id)
        refreshed, inventory = service.refresh_observation(force=True, _include_inventory=True)
        refresh_finished = self.clock()
        fresh = next((row for row in refreshed["sessions"] if row["session_id"] == session_id), {})
        required = ("instance_key", "pane_id", "slot")
        if (requested.get("terminal_binding") != "confirmed" or fresh.get("terminal_binding") != "confirmed"
                or fresh.get("identity_state") != "verified"
                or fresh.get("age_ms") is None or fresh["age_ms"] > CACHE_SECONDS * 1000
                or any(not fresh.get(field) or fresh[field] != requested.get(field) for field in required)):
            return {**result, "reason": "binding_unknown"}
        matches = [pane for pane in inventory.get("panes", []) if parse_slot_command(pane, self.config, self.remote_config) == fresh["slot"]]
        if not inventory.get("ok") or len(matches) != 1 or terminal_id(matches[0]) != fresh["pane_id"]:
            return {**result, "reason": "binding_unknown"}
        if fresh["age_ms"] + (self.clock() - refresh_finished) * 1000 > CACHE_SECONDS * 1000:
            return {**result, "reason": "binding_unknown"}
        deadline = self.clock() + TERMINAL_TIMEOUT
        if not self.workbench.integrity(deadline):
            return {**result, "reason": "workbench_integrity_failed"}
        pre = self.workbench.observe(deadline)
        if not pre["can_focus"] or not same_association(initial, pre):
            return {**result, "reason": pre["reason"] or "workbench_association_changed"}
        if fresh["age_ms"] + (self.clock() - refresh_finished) * 1000 > CACHE_SECONDS * 1000:
            return {**result, "reason": "binding_unknown"}
        remaining = deadline - self.clock()
        if remaining <= 0:
            return {**result, "reason": "workbench_timeout"}
        try:
            candidates = _focus_candidates(inventory["panes"], fresh, self.config, self.remote_config)
            if candidates is not None:
                fallback_exit = self._focus_next(candidates, pre["_clients"][0]["client_id"], fresh, refresh_finished, deadline,
                                                 pre["_clients"][0]["pane_id"])
                if fallback_exit is None:
                    return {**result, "reason": "focus_sent_unverified", "pane_id": fresh["pane_id"], "exit_code": 0}
                if fallback_exit:
                    return {**result, "reason": "focus_failed", "exit_code": fallback_exit}
            else:
                action = self._run(["--session", self.config["session"], "action", "focus-pane-id", fresh["pane_id"]], remaining)
                if action.returncode:
                    return {**result, "reason": "focus_failed", "exit_code": action.returncode}
            with ThreadPoolExecutor(max_workers=1) as executor:
                after_future = executor.submit(self.list_panes, deadline)
                post = self.workbench.observe(deadline)
                after = after_future.result()
            if candidates is not None and not after.get("ok") and after.get("exit_code"):
                return {**result, "reason": "focus_failed", "exit_code": after["exit_code"]}
            after_matches = [pane for pane in after.get("panes", []) if parse_slot_command(pane, self.config, self.remote_config) == fresh["slot"]]
            if (not post["can_focus"] or not same_association(pre, post)
                    or post["_clients"][0]["pane_id"] != fresh["pane_id"]
                    or not after.get("ok")
                    or len(after_matches) != 1 or terminal_id(after_matches[0]) != fresh["pane_id"]
                    or candidates is not None and _focus_candidates(after["panes"], fresh, self.config, self.remote_config) != candidates
                    or self.clock() > deadline
                    or fresh["age_ms"] + (self.clock() - refresh_finished) * 1000 > CACHE_SECONDS * 1000):
                return {**result, "reason": "focus_sent_unverified", "pane_id": fresh["pane_id"], "exit_code": 0}
            return {**result, "focused": True, "status": "confirmed", "pane_id": fresh["pane_id"], "exit_code": 0}
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            return {**result, "reason": "focus_sent_unverified", "pane_id": fresh["pane_id"]}


def focus_session(service, session_id):
    return service.terminal.focus_session(service, session_id)
