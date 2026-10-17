"""Bounded, read-only observations from explicitly selected project sources."""

import json
import ntpath
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

FILE_BYTES = 256 * 1024
PROJECT_BYTES = 2 * 1024 * 1024
REQUEST_BYTES = 8 * 1024 * 1024
REQUEST_FILES = 512
PROJECT_ENTRIES = 256
REQUEST_ENTRIES = 2048
WFC_DIRECTORIES = 64
WFC_CANDIDATES = 128
DISPLAY_RECORDS = 20
SUMMARY_CHARS = 2000
STAGE = re.compile(r"^\s*(?:stage|阶段)\s*[:：]\s*(.+)$", re.I | re.M)
WFC_STAGE = re.compile(r"当前阶段(?:\*\*)?\s*[:：]\s*([^\n]+)")
WFC_HEADING = re.compile(r"^#{1,6}\s+当前阶段\s*$", re.M)
WFC_STATUS = re.compile(r"^\s*(?:\*\*)?(?:当前状态|状态)(?:\*\*)?\s*[:：]\s*(.+)$", re.M)


class ObservationError(Exception):
    def __init__(self, reason, status="error"):
        self.reason, self.status = reason, status


@dataclass
class RequestBudget:
    """Share one instance across projects in their stable snapshot order."""

    bytes_read: int = 0
    files_read: int = 0
    entries_checked: int = 0


@dataclass
class _ProjectBudget:
    request: RequestBudget
    bytes_read: int = 0
    entries_checked: int = 0

    def entry(self):
        # Count before inspecting even a skipped entry or an overflow sentinel.
        self.entries_checked += 1
        self.request.entries_checked += 1
        if self.entries_checked > PROJECT_ENTRIES or self.request.entries_checked > REQUEST_ENTRIES:
            raise ObservationError("budget_exceeded")


def _relative_path(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 512
            or "\x00" in value or "\\" in value or ":" in value
            or value.startswith("/") or ntpath.splitdrive(value)[0]
            or ".." in value.split("/")):
        raise ObservationError("invalid_source_path")
    return value


def _contained(binding, path):
    """Resolve links before any target enumeration or open."""
    target = path.resolve()
    try:
        inside = os.path.commonpath((os.path.normcase(str(binding)), os.path.normcase(str(target))))
    except ValueError:
        raise ObservationError("path_outside_binding") from None
    if inside != os.path.normcase(str(binding)):
        raise ObservationError("path_outside_binding")
    return target


def _directory(binding, path):
    target = _contained(binding, path)
    if not stat.S_ISDIR(target.stat().st_mode):
        raise ObservationError("invalid_source_type")
    return target


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_bounded(binding, path, budget):
    target = _contained(binding, path)
    before = target.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ObservationError("invalid_source_type")
    request = budget.request
    limit = min(FILE_BYTES, PROJECT_BYTES - budget.bytes_read, REQUEST_BYTES - request.bytes_read)
    if request.files_read >= REQUEST_FILES or before.st_size > limit or limit < 0:
        raise ObservationError("budget_exceeded")
    request.files_read += 1
    # fstat detects common replacement/growth cases; this is not a cross-platform
    # guarantee against a malicious local process racing every filesystem call.
    with target.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
            raise ObservationError("source_changed")
        raw = stream.read(limit + 1)
        budget.bytes_read += len(raw)
        request.bytes_read += len(raw)
        if len(raw) > limit:
            raise ObservationError("budget_exceeded")
        if _identity(opened) != _identity(os.fstat(stream.fileno())) or len(raw) != opened.st_size:
            raise ObservationError("source_changed")
    return raw.decode("utf-8-sig"), before.st_mtime


def _summary(text):
    return text.strip()[:SUMMARY_CHARS]


def _record(path, text, *, feature=None, wfc=False):
    stage = STAGE.search(text)
    label, picked = "文本摘要", None
    if wfc:
        heading = WFC_HEADING.search(text)
        if heading:
            picked = " ".join(text[heading.end():].strip().split("\n\n", 1)[0].split())
        match = WFC_STAGE.search(text) or stage
        if not picked and match:
            picked = match.group(1).strip()
        if picked:
            label = "当前阶段"
        else:
            match = WFC_STATUS.search(text)
            if match:
                label, picked = "状态", match.group(1).strip()
    elif stage:
        label, picked = "阶段", stage.group(1).strip()
    return {"feature": feature or path.name, "label": label,
            "text": _summary(picked or text), "path": str(path)}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ObservationError("invalid_json")
        result[key] = value
    return result


def _constant(_value):
    raise ObservationError("invalid_json")


def _json_report(text):
    try:
        data = json.loads(text, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, RecursionError):
        raise ObservationError("invalid_json") from None
    allowed = {"version", "state", "stage", "percent", "note", "updated_at"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ObservationError("invalid_json_fields")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ObservationError("unsupported_source_version")
    if "state" in data and data["state"] not in ("planned", "active", "blocked", "completed"):
        raise ObservationError("invalid_json_fields")
    for key, limit in (("stage", 120), ("note", 2000)):
        if key in data and (not isinstance(data[key], str) or len(data[key]) > limit):
            raise ObservationError("invalid_json_fields")
    percent = data.get("percent")
    if percent is not None and (type(percent) is not int or not 0 <= percent <= 100):
        raise ObservationError("invalid_json_fields")
    stamp = data.get("updated_at")
    if stamp is not None:
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if isinstance(stamp, str) else None
            if parsed is None or parsed.utcoffset() is None:
                raise ValueError
        except (ValueError, OverflowError):
            raise ObservationError("invalid_json_fields") from None
    if not any((data.get("state"), data.get("stage", "").strip(), percent is not None,
                data.get("note", "").strip())):
        raise ObservationError("empty_report", "unrecognized")
    return data


def _wfc_records(binding, path, budget):
    if (budget.request.entries_checked > REQUEST_ENTRIES
            or budget.request.files_read >= REQUEST_FILES
            or budget.request.bytes_read >= REQUEST_BYTES):
        raise ObservationError("budget_exceeded")
    root = _directory(binding, path)
    directories, candidates = [], []
    with os.scandir(root) as entries:
        for entry in entries:
            budget.entry()
            # Reject escaping links even if they would otherwise be skipped.
            target = _contained(binding, Path(entry.path))
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            _directory(binding, target)
            directories.append(target)
            if len(directories) > WFC_DIRECTORIES:
                raise ObservationError("budget_exceeded")
            for name in ("README.md", "progress.md"):
                candidates.append((target / name, entry.name))
                if len(candidates) > WFC_CANDIDATES:
                    raise ObservationError("budget_exceeded")
    # Enumeration has succeeded within every bound before any sorting occurs.
    candidates.sort(key=lambda item: str(item[0]))
    found = []
    for candidate, feature in candidates:
        try:
            text, mtime = _read_bounded(binding, candidate, budget)
        except FileNotFoundError:
            continue
        if text.strip():
            found.append((mtime, str(candidate), _record(candidate, text, feature=feature, wfc=True)))
    found.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in found]


def observe(project, budget=None):
    """Return an independent observation; never update project/manual/task data."""
    request = budget if budget is not None else RequestBudget()
    source = project.get("source") or {"kind": "none"}
    kind = source.get("kind") if isinstance(source, dict) else None
    result = {"kind": kind, "status": "not_configured", "reason": "not_configured",
              "directory_id": source.get("directory_id") if isinstance(source, dict) else None,
              "path": None, "observed_at": datetime.now(timezone.utc).isoformat(),
              "records": [], "reported": None, "total": 0, "truncated": False}
    if kind == "none":
        return result
    try:
        if kind not in ("wfc", "text", "json"):
            raise ObservationError("invalid_source")
        relative = _relative_path(source.get("relative_path"))
        if kind == "wfc" and relative != "docs/current":
            raise ObservationError("invalid_source_path")
        selected = next((item for item in project.get("directories", [])
                         if item.get("id") == source.get("directory_id")), None)
        if selected is None:
            raise ObservationError("source_binding_missing")
        binding = Path(selected["path"]).resolve(strict=True)
        if not binding.is_dir():
            raise ObservationError("invalid_source_type")
        target = _contained(binding, binding / relative)
        result["path"] = str(target)
        local = _ProjectBudget(request)
        if kind == "wfc":
            records = _wfc_records(binding, target, local)
        else:
            text, _mtime = _read_bounded(binding, target, local)
            if kind == "json":
                result["reported"] = _json_report(text)
                records = []
            else:
                records = [_record(target, text)] if text.strip() else []
        if not records and result["reported"] is None:
            raise ObservationError("empty_source", "unrecognized")
        result.update(status="available", reason=None, records=records[:DISPLAY_RECORDS],
                      total=len(records), truncated=len(records) > DISPLAY_RECORDS)
    except ObservationError as error:
        result.update(status=error.status, reason=error.reason, reported=None)
    except FileNotFoundError:
        result.update(status="missing", reason="source_missing")
    except UnicodeError:
        result.update(status="error", reason="invalid_encoding")
    except (OSError, RuntimeError):
        result.update(status="error", reason="source_unavailable")
    return result
