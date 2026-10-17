"""Pure source contracts shared with the isolated remote reader."""

import posixpath
import re
import unicodedata
from uuid import UUID

SOURCE = None
HOST, PORT = "127.0.0.1", 8766
CLIENT_ORIGIN = f"http://{HOST}:{PORT}"
SUPPORTED_CLAUDE_VERSION = "2.1.278"
ZELLIJ_VERSION = "0.45.1"
READER_VERSION = "0.2.163"
SUPPORTED_READER_MCP_VERSIONS = ("1.26.0", "1.30.0")
MCP_VERSION = "1.30.0"
OBSERVE_TIMEOUT, TERMINAL_TIMEOUT, MESSAGE_TIMEOUT = 15, 5, 20
CACHE_SECONDS, STALE_SECONDS = 5, 30
MAX_TRANSCRIPT_BYTES, MAX_RESPONSE_BYTES = 16 * 1024 * 1024, 512 * 1024
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
    if revision is not None and (not isinstance(revision, str) or not 1 <= len(revision) <= 160
                                 or any(unicodedata.category(char) == "Cc" for char in revision)):
        raise ValueError("revision must be a bounded string")


def validate_remote_payload(payload):
    """Validate only transport data, without filesystem or platform dependencies."""
    def require(condition, field):
        if not condition:
            raise ValueError(field)

    def text(value, maximum):
        return (isinstance(value, str) and 1 <= len(value) <= maximum
                and not any(unicodedata.category(char) == "Cc" for char in value))

    def path(value):
        return (text(value, 2048) and value.startswith("/") and not value.startswith("//")
                and posixpath.normpath(value) == value)

    require(isinstance(payload, dict) and set(payload) == {
        "source", "home", "slot_files", "reader_mcp_version", "mode", "request"}, "payload")
    source = payload["source"]
    require(isinstance(source, dict) and set(source) == {
        "id", "host", "user", "config_dir", "claude_version"}, "source")
    require(text(source["id"], 512), "source.id")
    require(isinstance(source["host"], str) and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}", source["host"]) is not None, "source.host")
    require(text(source["user"], 128) and not any(char.isspace() for char in source["user"]), "source.user")
    require(path(source["config_dir"]), "source.config_dir")
    require(source["claude_version"] == SUPPORTED_CLAUDE_VERSION, "source.claude_version")
    require(path(payload["home"]), "home")
    slots = payload["slot_files"]
    require(isinstance(slots, dict) and len(slots) <= 16, "slot_files")
    for slot, filename in slots.items():
        require(isinstance(slot, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", slot) is not None,
                "slot_files")
        require(path(filename), "slot_files." + slot)
    require(len(set(slots.values())) == len(slots), "slot_files")
    require(payload["reader_mcp_version"] in SUPPORTED_READER_MCP_VERSIONS, "reader_mcp_version")
    require(payload["mode"] in ("observe", "messages"), "mode")
    request = payload["request"]
    if payload["mode"] == "observe":
        require(request is None, "request")
    else:
        require(isinstance(request, dict) and set(request) == {"session_id", "offset", "limit", "revision"},
                "request")
        try:
            message_arguments(**request)
        except (TypeError, ValueError, AttributeError):
            raise ValueError("request") from None
    return payload
