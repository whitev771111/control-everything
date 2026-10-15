"""One metadata index shared by the HTTP page and the stdio queries."""

import copy
import hashlib
import json
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .source import (
    SOURCE, SSH, READER_PYTHON, OBSERVE_TIMEOUT, MESSAGE_TIMEOUT,
    MAX_RESPONSE_BYTES, CACHE_SECONDS, STALE_SECONDS, COVERAGE_WARNINGS,
    READER_VERSION, canonical_uuid, message_arguments,
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class RemoteClient:
    def __init__(self, runner=subprocess.run):
        self.runner = runner

    def _call(self, mode, request=None):
        # The helper imports this exact local source module without deploying files.
        directory = Path(__file__).parent
        source_text = (directory / "source.py").read_text(encoding="utf-8")
        probe_text = (directory / "remote_probe.py").read_text(encoding="utf-8")
        script = ("import sys, types\n"
                  "package = types.ModuleType('session_panel'); package.__path__ = []\n"
                  "sys.modules['session_panel'] = package\n"
                  "source = types.ModuleType('session_panel.source')\n"
                  "sys.modules['session_panel.source'] = source\n"
                  f"exec(compile({source_text!r}, 'source.py', 'exec'), source.__dict__)\n"
                  + probe_text)
        python = "python3" if mode == "observe" else READER_PYTHON
        command = f"{python} - {mode}"
        if request is not None:
            command += " " + shlex.quote(json.dumps(request, separators=(",", ":")))
        argv = [SSH, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o",
                "ServerAliveInterval=10", "-o", "StrictHostKeyChecking=yes",
                SOURCE["host"], command]
        try:
            result = self.runner(argv, input=script, capture_output=True, text=True,
                                 encoding="utf-8", errors="strict",
                                 timeout=OBSERVE_TIMEOUT if mode == "observe" else MESSAGE_TIMEOUT)
            if result.returncode:
                reason = "source_unavailable" if mode == "observe" else "sdk_unavailable"
                return {"ok": False, "available": False, "reason": reason,
                        "exit_code": result.returncode, "source": SOURCE}
            if len(result.stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
                return {"ok": False, "available": False, "reason": "response_too_large", "source": SOURCE}
            response = json.loads(result.stdout)
            if not isinstance(response, dict) or response.get("source") != SOURCE:
                raise ValueError("invalid helper source")
            return response
        except subprocess.TimeoutExpired:
            return {"ok": False, "available": False, "reason": "observe_timeout"
                    if mode == "observe" else "reader_timeout", "source": SOURCE}
        except (OSError, ValueError, UnicodeError):
            return {"ok": False, "available": False, "reason": "source_unavailable", "source": SOURCE}

    def observe_source(self):
        return self._call("observe")

    def read_messages(self, session_id, offset=None, limit=20, revision=None):
        message_arguments(session_id, offset, limit, revision)
        result = self._call("messages", {"session_id": session_id, "offset": offset,
                                         "limit": limit, "revision": revision})
        if "items" not in result:
            result.update(available=False, status="unknown", session_id=session_id, items=[],
                          total_visible=None, next_offset=None, reader_version=READER_VERSION,
                          read_at=utc_now())
        return result


def instance_key(record):
    if not all(record.get(field) is not None for field in ("boot_id", "pid", "process_start_ticks")):
        return None
    return f"{SOURCE['id']}:{record['boot_id']}:{record['pid']}:{record['process_start_ticks']}"


def mark_unknown(record, reason):
    record.update(status="unknown", identity_state="unknown", process_state="unknown",
                  terminal_binding="unknown", pane_id=None, activity_state="unknown",
                  task_completion="unknown")
    record["reason_codes"] = list(dict.fromkeys([*record.get("reason_codes", []), reason]))
    record["reason"] = reason
    return record


def merge_observation(remote, inventory, previous, observed_at):
    """Merge immutable candidates; no directory, title or timestamp identity guesses."""
    from .terminal import parse_slot_command, terminal_id

    source_ok = remote.get("ok") is True and remote.get("source") == SOURCE
    terminal_ok = inventory.get("ok") is True
    old = {row["session_id"]: row for row in previous.get("sessions", [])}
    grouped, invalid = {}, []
    for raw in remote.get("registrations", []):
        try:
            canonical_uuid(raw.get("session_id"))
        except (ValueError, TypeError, AttributeError):
            invalid.append(copy.deepcopy(raw))
            continue
        grouped.setdefault(raw["session_id"], []).append(raw)
    slots = {}
    for rows in grouped.values():
        for row in rows:
            if row.get("slot"):
                slots.setdefault(row["slot"], []).append(row)
    pane_slots = {}
    if terminal_ok:
        for pane in inventory.get("panes", []):
            slot = parse_slot_command(pane)
            if slot and terminal_id(pane):
                pane_slots.setdefault(slot, []).append(pane)
    records = []
    for session_id in sorted(set(old) | set(grouped)):
        candidates = grouped.get(session_id, [])
        raw = candidates[0] if candidates else old[session_id]
        record = {field: copy.deepcopy(raw.get(field)) for field in (
            "cwd", "pid", "process_start_ticks", "registered_started_at", "slot", "boot_id")}
        record.update(session_id=session_id, source=copy.deepcopy(SOURCE), observed_at=observed_at,
                      reason_codes=list(dict.fromkeys(raw.get("reason_codes", []))) if source_ok and candidates else [],
                      registration_state=("present" if candidates else "missing") if source_ok else "unknown",
                      coverage_warnings=list(COVERAGE_WARNINGS),
                      provenance={"registrations": copy.deepcopy(candidates),
                                  "terminal_source": "Zellij current list-panes"},
                      activity_state="unknown", task_completion="unknown", pane_id=None,
                      terminal_binding="unknown", instance_key=instance_key(raw))
        prior = old.get(session_id)
        if prior:
            record["last_evidence"] = {key: copy.deepcopy(prior.get(key)) for key in (
                "observed_at", "identity_state", "process_state", "terminal_binding", "pane_id",
                "instance_key", "slot", "reason", "reason_codes", "registration_state")}
        if not source_ok:
            mark_unknown(record, remote.get("reason") or "source_unavailable")
        elif not candidates:
            mark_unknown(record, "registration_missing")
        elif len(candidates) != 1:
            mark_unknown(record, "identity_conflict")
        elif record["reason_codes"] or not raw.get("process_ticks_stable") or not record["instance_key"]:
            mark_unknown(record, record["reason_codes"][0] if record["reason_codes"] else "process_unverified")
        elif prior and prior.get("instance_key") and prior["instance_key"] != record["instance_key"]:
            mark_unknown(record, "instance_changed")
        elif not terminal_ok:
            mark_unknown(record, inventory.get("reason") or "terminal_unavailable")
        else:
            record.update(status="observed", identity_state="verified", process_state="observed_running")
            slot = record["slot"]
            if slot and len(slots.get(slot, [])) == 1 and len(pane_slots.get(slot, [])) == 1:
                record.update(terminal_binding="confirmed", pane_id=terminal_id(pane_slots[slot][0]))
            else:
                record["reason_codes"].append("binding_unknown")
        records.append(record)
    complete = source_ok and terminal_ok
    return {"source": copy.deepcopy(SOURCE), "status": "observed" if complete else "unknown",
            "reason": None if complete else remote.get("reason") or inventory.get("reason") or "source_unavailable",
            "sessions": records, "observed_at": observed_at,
            "coverage": {"registration_count": remote.get("registration_count"),
                         "process_count": remote.get("process_count"),
                         "confirmed_binding_count": sum(row["terminal_binding"] == "confirmed" for row in records),
                         "warnings": list(COVERAGE_WARNINGS), "invalid_registrations": invalid}}


class SessionService:
    def __init__(self, remote=None, terminal=None, clock=time.monotonic, utc_now=utc_now):
        from .terminal import Terminal
        self.remote = remote if remote is not None else RemoteClient()
        self.terminal = terminal if terminal is not None else Terminal()
        self.clock, self.utc_now = clock, utc_now
        self._observation_lock, self._message_lock = threading.Lock(), threading.Lock()
        self._snapshot = {"sessions": [], "source": SOURCE, "status": "unknown"}
        self._started = None
        self._last_success_at = None

    def refresh_observation(self, force=False, *, _include_inventory=False):
        if _include_inventory and not force:
            raise ValueError("fresh inventory requires force")
        with self._observation_lock:
            if not force and self._started is not None and self.clock() - self._started < CACHE_SECONDS:
                return self._view()
            started, observed_at = self.clock(), self.utc_now()
            if _include_inventory:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    remote_future = executor.submit(self.remote.observe_source)
                    try:
                        inventory = self.terminal.list_panes()
                    except Exception:
                        inventory = {"ok": False, "reason": "terminal_unavailable"}
                    try:
                        remote = remote_future.result()
                    except Exception:
                        remote = {"ok": False, "reason": "source_unavailable"}
            else:
                try:
                    remote = self.remote.observe_source()
                except Exception:
                    remote = {"ok": False, "reason": "source_unavailable"}
                try:
                    inventory = self.terminal.list_panes()
                except Exception:
                    inventory = {"ok": False, "reason": "terminal_unavailable"}
            snapshot = merge_observation(remote, inventory, self._snapshot, observed_at)
            if snapshot["status"] == "observed":
                self._last_success_at = self.utc_now()
            snapshot["last_success_at"] = self._last_success_at
            revision_data = [(row["session_id"], row["instance_key"], row["pane_id"], row["reason_codes"])
                             for row in snapshot["sessions"]]
            snapshot["observation_revision"] = hashlib.sha256(
                json.dumps([observed_at, revision_data]).encode()).hexdigest()
            self._snapshot, self._started = snapshot, started
            view = self._view()
            return (view, inventory) if _include_inventory else view

    def _view(self):
        result = copy.deepcopy(self._snapshot)
        age = max(0, int((self.clock() - self._started) * 1000)) if self._started is not None else None
        result["age_ms"] = age
        for row in result["sessions"]:
            row.update(age_ms=age, last_success_at=self._last_success_at)
            if age is None or age > STALE_SECONDS * 1000:
                row["registration_state"] = "unknown"
                mark_unknown(row, "observation_stale")
        if age is None or age > STALE_SECONDS * 1000:
            result.update(status="unknown", reason="observation_stale")
            if "coverage" in result:
                result["coverage"]["confirmed_binding_count"] = 0
        return result

    def list_sessions(self):
        return self.refresh_observation()

    def get_session_status(self, session_id):
        canonical_uuid(session_id)
        snapshot = self.list_sessions()
        return next((row for row in snapshot["sessions"] if row["session_id"] == session_id),
                    {"status": "unknown", "reason": "not_found", "reason_codes": ["not_found"],
                     "session_id": session_id, "source": copy.deepcopy(SOURCE),
                     "observed_at": snapshot.get("observed_at"), "age_ms": snapshot.get("age_ms"),
                     "terminal_binding": "unknown", "identity_state": "unknown", "process_state": "unknown"})

    def get_session_messages(self, session_id, offset=None, limit=20, revision=None):
        message_arguments(session_id, offset, limit, revision)
        with self._message_lock:
            try:
                return self.remote.read_messages(session_id, offset=offset, limit=limit, revision=revision)
            except Exception:
                return {"available": False, "status": "unknown", "reason": "reader_failed",
                        "session_id": session_id, "source": copy.deepcopy(SOURCE), "items": [],
                        "total_visible": None, "next_offset": None, "reader_version": READER_VERSION,
                        "read_at": self.utc_now()}
