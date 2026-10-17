"""Metadata observation and the public SDK reader, executed through SSH stdin."""

import importlib.metadata
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from control_everything.sessions.source import (
    READER_VERSION, MCP_VERSION, MAX_TRANSCRIPT_BYTES,
    MAX_RESPONSE_BYTES, canonical_uuid, message_arguments, validate_remote_payload,
)


SOURCE, HOME, SLOT_FILES = None, None, {}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class ProbeError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def revision_of(path):
    stat = path.stat()
    if not path.is_file():
        raise ProbeError("transcript_unreadable")
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"


def transcript_candidates(session_id, projects):
    canonical_uuid(session_id)
    root = projects.resolve(strict=True)
    if root != projects.absolute():
        raise ProbeError("source_mismatch")
    candidates = set(projects.rglob(session_id + ".jsonl"))
    candidates.update(projects.glob("*/" + session_id + ".jsonl"))
    result = []
    for candidate in sorted(candidates):
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ProbeError("transcript_path_unverified")
        result.append(candidate)
    return result


def transcript_metadata(path, session_id):
    """Read only top-level identity fields; never expose messages or titles."""
    before = revision_of(path)
    found_id, found_cwd, scanned = None, None, 0
    with path.open("rb") as stream:
        while scanned < MAX_RESPONSE_BYTES:
            line = stream.readline(MAX_RESPONSE_BYTES + 1)
            if not line:
                break
            scanned += len(line)
            if len(line) > MAX_RESPONSE_BYTES:
                raise ProbeError("transcript_metadata_unverified")
            try:
                item = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(item, dict):
                continue
            candidate_id = item.get("sessionId")
            candidate_cwd = item.get("cwd")
            if candidate_id is not None:
                if candidate_id != session_id:
                    raise ProbeError("identity_conflict")
                found_id = candidate_id
            if isinstance(candidate_cwd, str):
                if not candidate_cwd.startswith("/"):
                    raise ProbeError("transcript_metadata_unverified")
                found_cwd = candidate_cwd
            if found_id and found_cwd:
                break
    if not found_id or not found_cwd:
        raise ProbeError("transcript_metadata_unverified")
    if revision_of(path) != before:
        raise ProbeError("messages_changed")
    return {"session_id": found_id, "cwd": found_cwd, "revision": before,
            "size_bytes": path.stat().st_size, "path": str(path)}


def fixed_source_check():
    if SOURCE is None or HOME is None:
        raise ProbeError("not_configured")
    import pwd
    if (pwd.getpwuid(os.getuid()).pw_name != SOURCE["user"]
            or str(Path.home()) != HOME
            or os.environ.get("CLAUDE_CONFIG_DIR", SOURCE["config_dir"]) != SOURCE["config_dir"]):
        raise ProbeError("source_mismatch")
    if Path(SOURCE["config_dir"]).resolve(strict=True) != Path(SOURCE["config_dir"]):
        raise ProbeError("source_mismatch")


def process_ticks(process):
    # Field 22 follows the final ')' in /proc/<PID>/stat; comm may contain spaces.
    return (process / "stat").read_text().rsplit(")", 1)[1].split()[19]


def allowed_environment(process):
    result = {}
    for item in (process / "environ").read_bytes().split(b"\0"):
        key, separator, value = item.partition(b"=")
        if separator and key in (b"TWB_CWD_FILE", b"CLAUDE_CONFIG_DIR"):
            result[key.decode()] = value.decode("utf-8", errors="strict")
    return result


def read_registration(path, projects, boot_id, boot_epoch, ticks_per_second, uid):
    record = {"registration_path": str(path), "reason_codes": []}
    try:
        if (not path.resolve(strict=True).is_relative_to(path.parent.resolve(strict=True))
                or not path.is_file() or path.stat().st_size > 16384):
            raise ProbeError("registry_invalid")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or type(data.get("pid")) is not int
                or data["pid"] <= 0 or str(data["pid"]) != path.stem):
            raise ProbeError("registry_invalid")
        pid, session_id = data["pid"], canonical_uuid(data["sessionId"])
        cwd, started = data["cwd"], data["startedAt"]
        if not isinstance(cwd, str) or not cwd.startswith("/") or type(started) not in (int, float):
            raise ProbeError("registry_invalid")
        record.update(session_id=session_id, pid=pid, cwd=cwd,
                      registered_started_at=started, boot_id=boot_id)
        process = Path("/proc") / str(pid)
        first_ticks = process_ticks(process)
        comm = (process / "comm").read_text().strip()
        proc_cwd = str((process / "cwd").resolve(strict=True))
        environment = allowed_environment(process)
        if process.stat().st_uid != uid or comm not in ("claude", "claude.exe"):
            raise ProbeError("process_unverified")
        record.update(process_start_ticks=first_ticks, process_cwd=proc_cwd, process_name=comm)
        if proc_cwd != cwd:
            raise ProbeError("cwd_mismatch")
        if environment.get("CLAUDE_CONFIG_DIR", SOURCE["config_dir"]) != SOURCE["config_dir"]:
            raise ProbeError("source_mismatch")
        record["process_config_dir"] = environment.get("CLAUDE_CONFIG_DIR", SOURCE["config_dir"])
        slot_file = environment.get("TWB_CWD_FILE")
        record["slot"] = next((slot for slot, allowed in SLOT_FILES.items() if slot_file == allowed), None)
        record["slot_file"] = slot_file if slot_file in SLOT_FILES.values() else None
        argv = (process / "cmdline").read_bytes().split(b"\0")
        explicit_ids = []
        for index, argument in enumerate(argv):
            if argument == b"--session-id" and index + 1 < len(argv):
                explicit_ids.append(canonical_uuid(argv[index + 1].decode()))
            elif argument.startswith(b"--session-id="):
                explicit_ids.append(canonical_uuid(argument.split(b"=", 1)[1].decode()))
        record["argv_session_ids"] = explicit_ids
        if any(value != session_id for value in explicit_ids):
            record["reason_codes"].append("identity_conflict")
        paths = transcript_candidates(session_id, projects)
        record["transcript_count"] = len(paths)
        if len(paths) != 1:
            raise ProbeError("identity_conflict" if paths else "transcript_missing")
        metadata = transcript_metadata(paths[0], session_id)
        record["transcript"] = metadata
        if metadata["cwd"] != cwd:
            raise ProbeError("cwd_mismatch")
        second_ticks = process_ticks(process)
        if first_ticks != second_ticks:
            raise ProbeError("instance_changed")
        record["process_ticks_stable"] = True
        # This difference is a warning, never an equality or an instance identifier.
        birth = boot_epoch + int(first_ticks) / ticks_per_second
        delta = started / 1000 - birth
        record["registration_process_delta_seconds"] = round(delta, 3)
        if delta < -1 or delta > 10:
            record["reason_codes"].append("registration_time_unexplained")
    except ProbeError as error:
        record["reason_codes"].append(error.reason)
    except (OSError, ValueError, KeyError, TypeError, IndexError, UnicodeError):
        record["reason_codes"].append("registration_unreadable")
    return record


def observe_source():
    fixed_source_check()
    version_result = subprocess.run(["/usr/local/bin/claude", "--version"], capture_output=True,
                                    text=True, timeout=3)
    version_parts = version_result.stdout.strip().split()
    version_ok = (version_result.returncode == 0 and bool(version_parts)
                  and version_parts[0] == SOURCE["claude_version"])
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    boot_epoch = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                      if line.startswith("btime "))
    root = Path(SOURCE["config_dir"])
    registry = root / "sessions"
    if not registry.is_dir():
        raise ProbeError("registry_unavailable")
    records = [read_registration(path, root / "projects", boot_id, boot_epoch,
                                 os.sysconf("SC_CLK_TCK"), os.getuid())
               for path in sorted(registry.glob("*.json"))]
    process_count = 0
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if process.stat().st_uid == os.getuid() and (process / "comm").read_text().strip() in ("claude", "claude.exe"):
                process_count += 1
        except OSError:
            continue
    return {"ok": version_ok, "reason": None if version_ok else "version_mismatch",
            "source": SOURCE, "claude_version": SOURCE["claude_version"] if version_ok else None,
            "boot_id": boot_id, "registrations": records, "registration_count": len(records),
            "process_count": process_count, "observed_at": utc_now()}


def unavailable(session_id, reason, **extra):
    return {"available": False, "status": "unknown", "reason": reason, "session_id": session_id,
            "source": SOURCE, "items": [], "total_visible": None, "next_offset": None,
            "reader_version": READER_VERSION, "read_at": utc_now(), **extra}


def reader_capability():
    try:
        if (importlib.metadata.version("claude-agent-sdk") != READER_VERSION
                or importlib.metadata.version("mcp") != MCP_VERSION):
            raise ProbeError("sdk_version_mismatch")
        from claude_agent_sdk import get_session_messages
        return get_session_messages
    except (ImportError, importlib.metadata.PackageNotFoundError):
        raise ProbeError("sdk_unavailable") from None


def resolve_exact_uuid(session_id, projects):
    paths = transcript_candidates(session_id, projects)
    if len(paths) != 1:
        raise ProbeError("identity_conflict" if paths else "not_found")
    return paths[0], transcript_metadata(paths[0], session_id)


def require_reader_scope_matches(path, cwd, session_id, projects):
    canonical_cwd = unicodedata.normalize("NFC", os.path.realpath(cwd))
    if not canonical_cwd.isascii() or len(canonical_cwd) > 200 or not Path(canonical_cwd).is_dir():
        raise ProbeError("reader_scope_unverified")
    exact_directory = projects / re.sub(r"[^a-zA-Z0-9]", "-", canonical_cwd)
    if not exact_directory.is_dir() or path.parent != exact_directory:
        raise ProbeError("reader_scope_unverified")
    paths = transcript_candidates(session_id, projects)
    if len(paths) != 1 or paths[0] != path:
        raise ProbeError("reader_scope_unverified")
    if exact_directory.resolve() != exact_directory.absolute():
        raise ProbeError("reader_scope_unverified")
    return canonical_cwd


def read_messages(session_id, offset=None, limit=20, revision=None, *, projects=None, reader=None):
    message_arguments(session_id, offset, limit, revision)
    if projects is None and SOURCE is None:
        return unavailable(session_id, "not_configured")
    projects = projects or Path(SOURCE["config_dir"]) / "projects"
    try:
        if reader is None:
            fixed_source_check()
            os.environ["CLAUDE_CONFIG_DIR"] = SOURCE["config_dir"]
        path, metadata = resolve_exact_uuid(session_id, projects)
        cwd = require_reader_scope_matches(path, metadata["cwd"], session_id, projects)
        if metadata["size_bytes"] > MAX_TRANSCRIPT_BYTES:
            raise ProbeError("transcript_too_large")
        if revision is not None and metadata["revision"] != revision:
            raise ProbeError("revision_mismatch")
        reader = reader or reader_capability()
        for attempt in range(2):
            before = revision_of(path)
            if revision is not None and before != revision:
                raise ProbeError("revision_mismatch")
            if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
                raise ProbeError("transcript_too_large")
            try:
                visible = reader(session_id, directory=cwd, limit=None, offset=0)
            except Exception:
                raise ProbeError("reader_failed") from None
            if not visible:
                raise ProbeError("reader_empty_unverified")
            try:
                require_reader_scope_matches(path, cwd, session_id, projects)
                unchanged = revision_of(path) == before
            except OSError:
                raise ProbeError("messages_changed") from None
            if unchanged:
                break
            if revision is not None:
                raise ProbeError("revision_mismatch")
            if attempt:
                raise ProbeError("messages_changed")
            metadata = transcript_metadata(path, session_id)
            cwd = require_reader_scope_matches(path, metadata["cwd"], session_id, projects)
        actual_offset = max(0, len(visible) - limit) if offset is None else offset
        items = [{field: getattr(item, field) for field in (
            "type", "uuid", "session_id", "message", "parent_tool_use_id", "parent_agent_id")}
            for item in visible[actual_offset:actual_offset + limit]]
        result = {"available": True, "session_id": session_id, "source": SOURCE, "items": items,
                  "revision": before, "offset": actual_offset, "limit": limit,
                  "total_visible": len(visible), "next_offset": actual_offset + limit
                  if actual_offset + limit < len(visible) else None,
                  "reader_version": READER_VERSION, "read_at": utc_now()}
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ProbeError("response_too_large")
        return result
    except ProbeError as error:
        return unavailable(session_id, error.reason)
    except (OSError, ValueError, TypeError, AttributeError):
        return unavailable(session_id, "reader_failed")


def main(payload=None):
    global SOURCE, HOME, SLOT_FILES, MCP_VERSION
    SOURCE, HOME, SLOT_FILES = None, None, {}
    try:
        payload = validate_remote_payload(payload)
        SOURCE, HOME, SLOT_FILES = payload["source"], payload["home"], payload["slot_files"]
        MCP_VERSION = payload["reader_mcp_version"]
        if payload["mode"] == "observe":
            result = observe_source()
        else:
            result = read_messages(**payload["request"])
    except (ProbeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        result = {"ok": False, "status": "unknown",
                  "reason": error.reason if isinstance(error, ProbeError) else
                            "invalid_request" if isinstance(error, ValueError) else "source_unavailable",
                  "source": SOURCE, "observed_at": utc_now()}
    if SOURCE is not None and MCP_VERSION == "1.26.0":
        result["reader_upgrade_required"] = True
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
