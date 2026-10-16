"""The single fixed source and resource limits for this first release."""

from uuid import UUID

SOURCE = {
    "id": "example@192.0.2.10:/home/example/.claude",
    "host": "192.0.2.10",
    "user": "example",
    "config_dir": "/home/example/.claude",
    "claude_version": "2.1.278",
}
HOST, PORT = "127.0.0.1", 8766
CLIENT_ORIGIN = f"http://{HOST}:{PORT}"
SSH = "ssh.exe"
ZELLIJ = "C:/example/private-6b494d44.exe"
ZELLIJ_SESSION, ZELLIJ_TAB, ZELLIJ_VERSION = "workbench-v4", "Work-for-Linux", "0.45.1"
READER_PYTHON = "/home/example/.local/share/session-panel-reader/.venv/bin/python"
READER_VERSION, MCP_VERSION = "0.2.163", "1.26.0"
OBSERVE_TIMEOUT, TERMINAL_TIMEOUT, MESSAGE_TIMEOUT = 15, 5, 20
CACHE_SECONDS, STALE_SECONDS = 5, 30
MAX_TRANSCRIPT_BYTES, MAX_RESPONSE_BYTES = 16 * 1024 * 1024, 512 * 1024
SLOT_FILES = {
    slot: f"/home/example/.local/state/terminal-workbench/{slot}.cwd"
    for slot in ("main", "dev", "files")
}
COVERAGE_WARNINGS = [
    "Registry is an internal Claude Code 2.1.278 format; coverage is not a machine-wide total.",
    "claude agents --json omits an observed interactive session; it is not this index's source.",
    "Process presence and message history do not prove activity or task completion.",
]


def canonical_uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("session_id must be a canonical UUID")
    return value


def message_arguments(session_id, offset=None, limit=20, revision=None):
    canonical_uuid(session_id)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer between 1 and 50")
    if offset is not None and (type(offset) is not int or offset < 0):
        raise ValueError("offset must be a nonnegative integer")
    if revision is not None and (not isinstance(revision, str) or len(revision) > 160):
        raise ValueError("revision must be a bounded string")
