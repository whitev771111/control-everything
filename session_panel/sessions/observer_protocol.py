"""Strict protocol v2 consumer. No OS or profile access occurs on import."""

import hashlib
import json
import math
import re
import struct

VERSION = 2
REQUEST_MAX = 65536
RESPONSE_MAX = 524288
PROVIDERS = {"codex", "claude", "hermes", "pi", "grok", "dsh", "opencode"}
SNAPSHOT_FIELDS = {"protocol", "profile_key", "generation", "seq", "scope_revision", "environment_id",
                   "sampled_at_ms", "metrics_interval_ms", "status", "reason", "coverage",
                   "installations", "instances", "resources", "config_revision", "config_hash", "registrations"}
INSTALLATION_FIELDS = {"provider_id", "status", "reason", "source", "commands", "version"}
INSTANCE_FIELDS = {"instance_id", "provider_id", "kind", "pid", "resource_id", "directory_ids", "status",
                   "reason", "source", "sampled_at_ms", "session", "pending", "return_action", "classification", "registration_ids"}
RESOURCE_FIELDS = {"resource_id", "scope", "cpu_percent_one_core", "rss_bytes", "threads", "handles",
                   "metrics_interval_ms", "source", "sampled_at_ms"}
GRANT_FIELDS = {"action_id", "issued_seq", "expires_at_ms", "kind", "status", "reason", "scope"}
REGISTRATION_STATUS_FIELDS = {"registration_id", "status", "reason", "source", "sampled_at_ms", "duplicate_of_id"}


class ProtocolError(ValueError):
    pass


def require(condition):
    if not condition:
        raise ProtocolError("invalid_message")


def exact(value, fields):
    require(type(value) is dict and set(value) == set(fields))


def integer(value, *, nullable=False):
    require(value is None and nullable or type(value) is int and 0 <= value <= (1 << 64) - 1)


def token(value, *, nullable=False):
    require(value is None and nullable or type(value) is str and re.fullmatch(r"[A-Za-z0-9_:.-]{1,128}", value))


def code(value):
    require(value is None or type(value) is str and re.fullmatch(r"[a-z0-9_]{1,80}", value))


def registration_id(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value))


def config_pair(value):
    integer(value["config_revision"])
    require(value["config_revision"] <= (1 << 53) - 1 and type(value["config_hash"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", value["config_hash"]))
    return value["config_revision"], value["config_hash"]


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def decode(data, maximum=RESPONSE_MAX):
    require(type(data) in (bytes, bytearray) and 0 < len(data) <= maximum)
    try:
        value = json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=_pairs,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ProtocolError("invalid_message")))
    except (UnicodeError, ValueError, RecursionError):
        raise ProtocolError("invalid_message") from None
    def check(item, depth):
        require(depth <= 12)
        if type(item) is dict:
            for key, child in item.items():
                require(type(key) is str)
                check(child, depth + 1)
        elif type(item) is list:
            for child in item:
                check(child, depth + 1)
        elif type(item) is str:
            require(not any(0xD800 <= ord(char) <= 0xDFFF for char in item))
        elif type(item) in (int, float):
            require(type(item) is int and -(1 << 63) <= item <= (1 << 64) - 1 or
                    type(item) is float and math.isfinite(item))
    check(value, 0)
    require(type(value) is dict)
    return value


def encode(value):
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    require(0 < len(data) <= REQUEST_MAX)
    return data


def profile_key(sid, volume, file_id):
    require(type(sid) is bytes and len(file_id) == 16)
    return hashlib.sha256(b"session-panel-observer/profile/v1\0" + struct.pack("<I", len(sid)) + sid +
                          struct.pack("<Q", volume) + file_id).hexdigest()


def error_response(value):
    if value.get("status") == "unavailable":
        exact(value, {"status", "reason"})
        code(value["reason"])
        require(value["reason"] is not None)
        return True
    return False


def hello(value, key, build):
    if error_response(value):
        raise ProtocolError(value["reason"])
    if value.get("protocol") != VERSION:
        raise ProtocolError("protocol_mismatch")
    exact(value, {"status", "protocol", "profile_key", "generation", "build", "scope_revision", "scope_hash", "config_revision", "config_hash"})
    require(value["status"] == "available" and type(value["protocol"]) is int and value["protocol"] == VERSION and value["profile_key"] == key
            and value["build"] == build)
    token(value["generation"])
    integer(value["scope_revision"])
    require(value["scope_hash"] == "" or re.fullmatch(r"[0-9a-f]{64}", value["scope_hash"]))
    config_pair(value)
    return value


def grant(value, *, choice=False):
    if not choice and type(value) is dict and value.get("action_id") is None:
        exact(value, {"status", "reason", "action_id"})
        require((value["status"], value["reason"]) in (("limited", "grant_capacity"), ("limited", "binding_unknown"), ("stale", "sample_timeout")))
        return
    exact(value, GRANT_FIELDS | ({"title"} if choice else set()))
    token(value["action_id"])
    integer(value["issued_seq"])
    integer(value["expires_at_ms"])
    require(value["kind"] == ("manual_navigation" if choice else "observed_instance"))
    require(value["status"] in ("available", "limited") and value["scope"] in ("application_window", "console_window"))
    code(value["reason"])
    if choice:
        require(value["scope"] == "application_window" and type(value["title"]) is str
                and len(value["title"].encode("utf-16-le")) <= 512)


def snapshot(value, key, generation, revision, config=None):
    exact(value, SNAPSHOT_FIELDS)
    require(type(value["protocol"]) is int and value["protocol"] == VERSION and value["profile_key"] == key and value["generation"] == generation
            and value["scope_revision"] == revision and value["environment_id"] == "windows:local")
    pair = config_pair(value)
    if config is not None:
        require(pair == config)
    for name in ("seq", "scope_revision", "sampled_at_ms", "metrics_interval_ms"):
        integer(value[name])
    require(value["status"] in ("available", "limited", "unknown", "stale"))
    code(value["reason"])
    coverage = value["coverage"]
    require(type(coverage) is dict and set(coverage) <= {"status", "scanned", "recognized", "omitted", "denied", "reason"}
            and coverage.get("status") in ("complete", "limited", "unknown"))
    for name, item in coverage.items():
        if name == "reason":
            code(item)
        elif name != "status":
            integer(item)
    for name, maximum in (("installations", 7), ("instances", 256), ("resources", 256)):
        require(type(value[name]) is list and len(value[name]) <= maximum)
    require(type(value["registrations"]) is list and len(value["registrations"]) <= 32)
    registrations = set()
    for item in value["registrations"]:
        exact(item, REGISTRATION_STATUS_FIELDS)
        registration_id(item["registration_id"])
        require(item["registration_id"] not in registrations)
        registrations.add(item["registration_id"])
        require(item["status"] in ("available", "limited", "not_found", "not_configured", "unknown"))
        code(item["reason"])
        token(item["source"])
        integer(item["sampled_at_ms"])
        if item["duplicate_of_id"] is not None:
            registration_id(item["duplicate_of_id"])
            require(item["reason"] == "duplicate_installation")
    seen = set()
    for item in value["installations"]:
        exact(item, INSTALLATION_FIELDS)
        require(item["provider_id"] in PROVIDERS and item["provider_id"] not in seen)
        seen.add(item["provider_id"])
        require(item["status"] in ("available", "not_found", "unknown"))
        code(item["reason"])
        token(item["source"])
        require(type(item["commands"]) is list and len(item["commands"]) <= 64)
        for command in item["commands"]:
            token(command)
        require(item["version"] is None or type(item["version"]) is str and re.fullmatch(r"[A-Za-z0-9.+_-]{1,80}", item["version"]))
    resources = set()
    for item in value["resources"]:
        exact(item, RESOURCE_FIELDS)
        token(item["resource_id"])
        require(item["resource_id"] not in resources)
        resources.add(item["resource_id"])
        require(item["scope"] in ("process", "shared_host"))
        cpu = item["cpu_percent_one_core"]
        require(cpu is None or type(cpu) in (int, float) and math.isfinite(cpu) and cpu >= 0)
        for name in ("rss_bytes", "threads", "handles", "metrics_interval_ms", "sampled_at_ms"):
            integer(item[name], nullable=name in ("rss_bytes", "threads", "handles"))
        token(item["source"])
    seen = set()
    for item in value["instances"]:
        exact(item, INSTANCE_FIELDS)
        token(item["instance_id"])
        require(item["instance_id"] not in seen)
        require(item["classification"] in ("provider", "custom_exe"))
        require((item["classification"] == "provider" and item["provider_id"] in {"codex", "claude", "hermes", "pi"}
                 and item["kind"] in ("cli", "desktop", "host")) or
                (item["classification"] == "custom_exe" and item["provider_id"] is None and item["kind"] == "custom_exe"))
        require(type(item["registration_ids"]) is list and len(item["registration_ids"]) <= 32)
        for identity in item["registration_ids"]:
            registration_id(identity)
            require(identity in registrations)
        require(len(set(item["registration_ids"])) == len(item["registration_ids"]))
        seen.add(item["instance_id"])
        require(item["status"] in ("available", "limited", "unknown", "stale"))
        integer(item["pid"])
        require(0 < item["pid"] <= 0xFFFFFFFF)
        require(item["resource_id"] in resources)
        require(type(item["directory_ids"]) is list and len(item["directory_ids"]) <= 16)
        for directory_id in item["directory_ids"]:
            token(directory_id)
        code(item["reason"])
        token(item["source"])
        integer(item["sampled_at_ms"])
        for name in ("session", "pending"):
            require(item[name] == {"status": "unknown", "reason": "no_session_source" if name == "session" else "no_pending_source"})
        if item["return_action"] is not None:
            grant(item["return_action"])
    return value


def choices(value, generation, config=None):
    if error_response(value):
        return value
    exact(value, {"status", "reason", "generation", "selection_id", "expires_at_ms", "coverage", "choices", "config_revision", "config_hash"})
    pair = config_pair(value)
    if config is not None:
        require(pair == config)
    require(value["status"] in ("available", "limited") and value["generation"] == generation)
    code(value["reason"])
    token(value["selection_id"])
    integer(value["expires_at_ms"])
    exact(value["coverage"], {"status", "omitted"})
    require(value["coverage"]["status"] in ("complete", "limited"))
    integer(value["coverage"]["omitted"])
    require(type(value["choices"]) is list and len(value["choices"]) <= 32)
    for item in value["choices"]:
        grant(item, choice=True)
    return value


def candidates(value):
    exact(value, {"status", "reason", "config_revision", "config_hash", "candidates", "coverage"})
    config_pair(value)
    require(value["status"] in ("available", "limited"))
    code(value["reason"])
    exact(value["coverage"], {"status", "omitted"})
    require(value["coverage"]["status"] in ("complete", "limited"))
    integer(value["coverage"]["omitted"])
    require(type(value["candidates"]) is list and len(value["candidates"]) <= 64)
    allowed = {"codex": {"npm_command", "codex_desktop"}, "claude": {"npm_command"},
               "pi": {"npm_command"}, "hermes": {"hermes_launcher"}}
    for item in value["candidates"]:
        exact(item, {"provider_id", "locator_kind", "path", "version"})
        require(type(item["provider_id"]) is str and type(item["locator_kind"]) is str
                and item["locator_kind"] in allowed.get(item["provider_id"], set()))
        require(type(item["path"]) is str and 0 < len(item["path"]) <= 2048
                and re.match(r"^[A-Za-z]:[\\/]", item["path"]) and not any(ord(char) < 32 for char in item["path"]))
        require(item["version"] is None or type(item["version"]) is str and re.fullmatch(r"[A-Za-z0-9.+_-]{1,80}", item["version"]))
    return value
