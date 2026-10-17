"""Explicit local Codex identity metadata, never transcript or live task status."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from session_panel.sessions.source import canonical_uuid

LIMIT = 12
KINDS = ("cli", "exec", "vscode")
COVERAGE_KEYS = ("total_count", "archived_count", "excluded_subagent_count", "excluded_other_source_count")


def unavailable(offset):
    return {"available": False, "reason": "codex_index_unavailable", "source": "codex_local", "sampled_at": None,
            "items": [], "offset": offset, "limit": LIMIT, "total_visible": None, "next_offset": None,
            "coverage": dict.fromkeys(COVERAGE_KEYS)}


def utc_timestamp(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid_timestamp")
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def list_threads(offset=0, db_path=None):
    # An absent source must never inspect an author-specific database.
    if type(offset) is not int or not 0 <= offset <= 999999999:
        raise ValueError("invalid_offset")
    if db_path is None:
        return unavailable(offset)
    try:
        path = Path(db_path).resolve()
        if not path.is_file():
            return unavailable(offset)
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            if not {"id", "cwd", "created_at", "updated_at", "archived", "source"} <= columns:
                raise ValueError("schema_unavailable")
            total, archived, visible, subagents, others = connection.execute("""
                SELECT COUNT(*), COALESCE(SUM(archived=1),0),
                    COALESCE(SUM(archived=0 AND source IN ('cli','exec','vscode')),0),
                    COALESCE(SUM(archived=0 AND substr(source,1,12)='{"subagent":'),0),
                    COALESCE(SUM(archived=0 AND (source IS NULL OR
                        (source NOT IN ('cli','exec','vscode') AND substr(source,1,12)!='{"subagent":'))),0)
                FROM threads
            """).fetchone()
            if total != archived + visible + subagents + others:
                raise ValueError("coverage_unavailable")
            rows = connection.execute("""
                SELECT id,cwd,created_at,updated_at,archived,source FROM threads
                WHERE archived=0 AND source IN ('cli','exec','vscode')
                ORDER BY updated_at DESC,id ASC LIMIT ? OFFSET ?
            """, (LIMIT, offset))
            items = []
            seen = set()
            for thread_id, cwd, created, updated, is_archived, kind in rows:
                canonical_uuid(thread_id)
                if thread_id in seen or not isinstance(cwd, str) or type(is_archived) is not int or is_archived != 0 or kind not in KINDS:
                    raise ValueError("invalid_row")
                seen.add(thread_id)
                items.append({"thread_id": thread_id, "session_key": f"codex:{thread_id}", "cwd": cwd,
                              "created_at": utc_timestamp(created), "updated_at": utc_timestamp(updated),
                              "archived": False, "source_kind": kind, "activity_state": "unknown", "task_completion": "unknown"})
            return {"available": True, "reason": None, "source": "codex_local", "sampled_at": datetime.now(timezone.utc).isoformat(),
                    "items": items, "offset": offset, "limit": LIMIT, "total_visible": visible,
                    "next_offset": offset + len(items) if items and offset + len(items) < visible else None,
                    "coverage": dict(zip(COVERAGE_KEYS, (total, archived, subagents, others)))}
        finally:
            connection.close()
    except (sqlite3.Error, ValueError, TypeError, OverflowError, OSError):
        return unavailable(offset)
