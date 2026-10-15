"""Bounded, read-only public child-agent utterances for the bound garden root."""

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from .garden_native import GardenError, NATIVE_DB
from .source import canonical_uuid

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 256 * 1024
MAX_MESSAGES = 6
MAX_TEXT = 1200
PHASES = {"commentary", "final_answer"}


def _rollout_file(raw, actor_id, database):
    if not isinstance(raw, str) or not 1 <= len(raw) <= 1024 or any(ord(char) < 32 for char in raw):
        raise GardenError("public_unavailable")
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    if not re.match(r"^[A-Za-z]:\\", raw) or ".." in PureWindowsPath(raw).parts:
        raise GardenError("public_unavailable")
    candidate = Path(raw)
    root = (Path(database).parent / "sessions").resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    if (not resolved.is_relative_to(root) or not resolved.is_file()
            or not resolved.name.endswith("-" + actor_id + ".jsonl")):
        raise GardenError("public_unavailable")
    current = candidate
    while current != root and current.is_relative_to(root):
        if current.is_symlink():
            raise GardenError("public_unavailable")
        current = current.parent
    return resolved


def _public_message(value):
    if not isinstance(value, dict) or value.get("type") != "response_item":
        return None
    payload = value.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message" or payload.get("role") != "assistant":
        return None
    phase = payload.get("phase")
    if phase not in PHASES:
        return None
    content = payload.get("content")
    if not isinstance(content, list) or not content or any(
            not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str)
            for part in content):
        raise GardenError("public_unavailable")
    text = "\n".join(part["text"] for part in content).strip()
    if not text:
        return None
    timestamp = value.get("timestamp")
    try:
        if not isinstance(timestamp, str) or len(timestamp) > 64:
            raise ValueError
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        timestamp = None
    return {"phase": phase, "text": text[:MAX_TEXT], "truncated": len(text) > MAX_TEXT, "created_at": timestamp}


def read_public_messages(garden, actor_id):
    actor_id = canonical_uuid(actor_id)
    doc = garden.snapshot()
    member = doc["actors"].get(actor_id)
    if (actor_id == garden.context.root_thread_id or not member
            or member["root_thread_id"] != garden.context.root_thread_id
            or garden.native(actor_id).public() != {key: member[key] for key in
                ("actor_id", "root_thread_id", "parent_thread_id", "agent_path", "nickname")}):
        raise GardenError("actor_not_in_domain")
    database = garden.native_db or NATIVE_DB
    try:
        with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            rows = db.execute("SELECT id,rollout_path FROM threads WHERE id=?", (actor_id,)).fetchall()
        if len(rows) != 1 or rows[0][0] != actor_id:
            raise GardenError("public_unavailable")
        path = _rollout_file(rows[0][1], actor_id, database)
        with path.open("rb") as handle:
            before = path.stat()
            if before.st_size > MAX_FILE_BYTES:
                raise GardenError("public_unavailable")
            data = handle.read(MAX_FILE_BYTES + 1)
            after = path.stat()
        if (len(data) > MAX_FILE_BYTES or len(data) != before.st_size
                or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                or data and not data.endswith(b"\n")):
            raise GardenError("public_unavailable")
        messages = []
        for line in data.splitlines():
            if len(line) > MAX_LINE_BYTES:
                raise GardenError("public_unavailable")
            value = json.loads(line.decode("utf-8"))
            message = _public_message(value)
            if message:
                messages.append(message)
        return {"available": True, "root_thread_id": garden.context.root_thread_id, "actor_id": actor_id,
                "coverage": "本机当前记录内最近的有限公开发言，并非完整对话。" if messages else "无可展示的公开发言。",
                "sampled_at": datetime.now(timezone.utc).isoformat(), "messages": messages[-MAX_MESSAGES:]}
    except GardenError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, UnicodeError):
        raise GardenError("public_unavailable") from None
