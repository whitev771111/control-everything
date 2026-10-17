"""Offline Orca x64 material preflight; no exit code admits runtime launch.

Use --materials with an explicitly prepared absolute local directory. Exit codes:
0 static match, 1 mismatch, 2 invalid input, 3 I/O/internal error, 4 missing.
No material is executed, acquired, repaired, or connected to the workbench.
"""

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import sys


# stablyai/orca@aceebea0a4453f63d759af9134503220ce0b4753:
# src/shared/node-runtime-pin.ts and src/shared/orcad-artifacts.ts.
ORCA_COMMIT = "aceebea0a4453f63d759af9134503220ce0b4753"
PACKAGE_VERSION = "1.4.214"
NODE_VERSION = "24.21.0"
NODE_SHA256 = "ba4e6d110e8c1592a1ecd390f6b05f3da124b13871a5be62b341a07a853c6c32"
NODE_SIZE = 93580104
NODE_PATH = f"runtimes/node-{NODE_SHA256}/node.exe"
CHUNK_BYTES = 1024 * 1024
TOTAL_BYTES = 300 * 1024 * 1024
CANDIDATE_BYTES = 16 * 1024
REPORT_BYTES = 64 * 1024
CHECK_IDS = (
    "input", "candidate.input", "source.package", "slot.target", "slot.node_marker",
    "slot.launcher", "slot.server", "slot.daemon", "node.executable",
    "slot.no_bun_marker", "candidate.args", "candidate.environment",
)
FILE_IDS = frozenset(CHECK_IDS[2:10])
ENV_KEYS = frozenset(("ORCA_USER_DATA", "USERPROFILE", "HOME", "APPDATA",
                      "LOCALAPPDATA", "TEMP", "TMP"))
NOT_COVERED = (
    "remaining_bundle_artifacts", "native_abi", "web_build_and_http", "other_targets",
    "js_integrity", "bundle_provenance", "concurrent_filesystem_adversary",
    "synthetic_origin", "source_commit_authentication", "bundle_completeness",
    "build_reproducibility", "version_and_install_markers", "licenses",
)
HELP = ("usage: orca_preflight.py [-h] --materials ABSOLUTE_DIRECTORY\n\n"
        + __doc__ + "\noptions:\n  -h, --help  show this help message and exit\n"
        "  --materials ABSOLUTE_DIRECTORY  inspect explicitly supplied local materials\n")


class InputInvalid(ValueError):
    """An input cannot be inspected; never include external text in its message."""


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def validate_root(root):
    """Validate original ancestors with lstat, without resolving any links."""
    if not isinstance(root, str) or "\0" in root or root.startswith(("\\\\", "//")):
        raise InputInvalid()
    path = Path(root)
    if not path.is_absolute():
        raise InputInvalid()
    try:
        for component in reversed((path, *path.parents)):
            info = component.lstat()
            if _linked(info) or not stat.S_ISDIR(info.st_mode):
                raise InputInvalid()
    except OSError:
        raise InputInvalid() from None
    return path


def _record(status, reason, *, size=None, sha256=None):
    return {"status": status, "reason": reason, "size": size,
            "sha256": sha256, "integrity": "unverified"}


def _fixed_lstat(root, relative):
    parts = Path(relative).parts
    if Path(relative).is_absolute() or any(part in (".", "..") for part in parts):
        raise InputInvalid()
    path = root
    for index, part in enumerate(parts):
        path = path / part
        info = path.lstat()
        if _linked(info):
            raise InputInvalid()
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise InputInvalid()
    return path, info


def _same_file(before, after):
    return (stat.S_ISREG(after.st_mode) and not _linked(after) and after.st_nlink == 1
            and all(getattr(before, key) == getattr(after, key)
                    for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns")))


def inspect_file(root, relative, max_bytes, capture=False, *, _budget=None):
    """Read a fixed ordinary file once, bounded by both per-file and total limits."""
    budget = [TOTAL_BYTES] if _budget is None else _budget
    try:
        path, before = _fixed_lstat(root, relative)
        if not stat.S_ISREG(before.st_mode):
            return _record("mismatch", "not_regular", size=before.st_size)
        if before.st_nlink != 1:
            return _record("mismatch", "path_rejected", size=before.st_size)
        if before.st_size > max_bytes or before.st_size > budget[0]:
            return _record("mismatch", "too_large", size=before.st_size)
        digest, count, pieces = hashlib.sha256(), 0, []
        with path.open("rb") as stream:
            if not _same_file(before, os.fstat(stream.fileno())):
                return _record("mismatch", "changed_during_read")
            while budget[0] > 0:
                data = stream.read(min(CHUNK_BYTES, max_bytes - count + 1, budget[0]))
                if not data:
                    break
                count += len(data)
                budget[0] -= len(data)
                if count > max_bytes:
                    return _record("mismatch", "changed_during_read", size=count)
                digest.update(data)
                if capture:
                    pieces.append(data)
            if not _same_file(before, os.fstat(stream.fileno())) or count != before.st_size:
                return _record("mismatch", "changed_during_read", size=count)
        result = _record("matched", "nonempty_file", size=count, sha256=digest.hexdigest())
        if capture:
            result["_raw"] = b"".join(pieces)
        return result
    except FileNotFoundError:
        return _record("missing", "absent")
    except InputInvalid:
        return _record("mismatch", "path_rejected")
    except OSError:
        return _record("error", "read_failed")


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InputInvalid()
            result[key] = value
        return result

    def nonfinite(_value):
        raise InputInvalid()

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=nonfinite)
    except (ValueError, RecursionError, UnicodeDecodeError):
        raise InputInvalid() from None


def inspect_materials(root):
    """Inspect eight files plus one forbidden marker; no recursive discovery."""
    budget = [TOTAL_BYTES]
    candidate = inspect_file(root, "candidate.json", CANDIDATE_BYTES, True, _budget=budget)
    candidate["id"] = "candidate.input"
    if candidate["reason"] == "too_large":
        candidate.update(status="error", reason="input_invalid")
    checks = [candidate]
    # This intentionally covers a small slice, not the upstream artifact manifest.
    for check_id, relative, limit, capture in (
        ("source.package", "source/package.json", 128 * 1024, True),
        ("slot.target", "slot/.server-target", 128, True),
        ("slot.node_marker", "slot/.runtime-node", 128, True),
        ("slot.launcher", "slot/orcad.js", 64 * 1024 * 1024, False),
        ("slot.server", "slot/orcad-server.js", 64 * 1024 * 1024, False),
        ("slot.daemon", "slot/daemon-entry.js", 64 * 1024 * 1024, False),
        ("node.executable", NODE_PATH, 96 * 1024 * 1024, False),
    ):
        item = inspect_file(root, relative, limit, capture, _budget=budget)
        item["id"] = check_id
        if item["status"] == "matched":
            if check_id == "source.package":
                try:
                    package = _strict_json(item["_raw"])
                    valid = (isinstance(package, dict) and package.get("name") == "orca"
                             and package.get("version") == PACKAGE_VERSION
                             and isinstance(package.get("engines"), dict)
                             and package["engines"].get("node") == "24")
                except InputInvalid:
                    valid = False
                item.update(status="matched" if valid else "mismatch",
                            reason="declaration_matched" if valid else "content_mismatch")
            elif check_id in ("slot.target", "slot.node_marker"):
                raw = item["_raw"]
                if raw.endswith(b"\r\n"):
                    raw = raw[:-2]
                elif raw.endswith(b"\n"):
                    raw = raw[:-1]
                expected = b"win32-x64" if check_id == "slot.target" else NODE_SHA256.encode("ascii")
                valid = raw == expected
                item.update(status="matched" if valid else "mismatch",
                            reason="declaration_matched" if valid else "content_mismatch")
            elif check_id == "node.executable":
                if item["size"] != NODE_SIZE:
                    item.update(status="mismatch", reason="size_mismatch")
                elif item["sha256"] != NODE_SHA256:
                    item.update(status="mismatch", reason="hash_mismatch")
                else:
                    item.update(reason="pin_matched", integrity="pin_matched")
            elif item["size"] == 0:
                item.update(status="mismatch", reason="content_mismatch")
        item.pop("_raw", None)
        checks.append(item)
    # The forbidden marker is lstat-only: even links and directories count as present.
    try:
        parent = root / "slot"
        info = parent.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            item = _record("mismatch", "path_rejected")
        else:
            info = (parent / ".build-target").lstat()
            item = _record("mismatch", "content_mismatch", size=info.st_size)
    except FileNotFoundError:
        item = _record("matched", "absent")
    except OSError:
        item = _record("error", "read_failed")
    checks.append({"id": "slot.no_bun_marker", **item})
    return checks


def parse_candidate(raw):
    """Parse only the narrow structural schema; policy checks remain pure data."""
    if len(raw) > CANDIDATE_BYTES:
        raise InputInvalid()
    candidate = _strict_json(raw)
    if not isinstance(candidate, dict) or set(candidate) != {"target", "route", "argv", "environment"}:
        raise InputInvalid()
    if not all(isinstance(candidate[key], str) and len(candidate[key]) <= 64
               for key in ("target", "route")):
        raise InputInvalid()
    argv = candidate["argv"]
    if not isinstance(argv, list) or len(argv) > 8 or not all(
            isinstance(token, str) and len(token) <= 64 for token in argv):
        raise InputInvalid()
    environment = candidate["environment"]
    if not isinstance(environment, dict) or set(environment) != {"source", "inherit", "root", "values"}:
        raise InputInvalid()
    if (not isinstance(environment["source"], str) or len(environment["source"]) > 64
            or type(environment["inherit"]) is not bool
            or not isinstance(environment["root"], str) or len(environment["root"]) > 240):
        raise InputInvalid()
    values = environment["values"]
    if not isinstance(values, dict) or set(values) != ENV_KEYS or not all(
            isinstance(value, str) and len(value) <= 240 for value in values.values()):
        raise InputInvalid()
    return candidate


def _args_valid(candidate):
    if candidate["target"] != "win32-x64" or candidate["route"] != "direct-node-entry":
        return False
    argv, seen, index = candidate["argv"], set(), 0
    while index < len(argv):
        flag = argv[index]
        if flag not in ("--bind", "--port", "--json", "--no-pairing") or flag in seen:
            return False
        seen.add(flag)
        index += 1
        if flag in ("--bind", "--port"):
            if index >= len(argv):
                return False
            value = argv[index]
            index += 1
            if flag == "--bind" and value != "127.0.0.1":
                return False
            if flag == "--port" and (not re.fullmatch(r"0|[1-9][0-9]*", value)
                                     or int(value) > 65535):
                return False
    return seen == {"--bind", "--port", "--json", "--no-pairing"}


def _windows_parts(value):
    # Inspect raw segments before PureWindowsPath can normalize dots away.
    if (not value or len(value) > 240 or value.startswith(("\\\\", "//"))
            or any(ord(char) < 32 or ord(char) == 127 or char in "%$" for char in value)):
        raise InputInvalid()
    segments = value.replace("/", "\\").split("\\")
    if not re.fullmatch(r"[A-Za-z]:", segments[0]) or len(segments) < 2:
        raise InputInvalid()
    if any(segment in (".", "..") or segment.endswith((" ", "."))
           or any(char in '<>:"|?*' for char in segment)
           or PureWindowsPath(segment).is_reserved()
           for segment in segments[1:] if segment):
        raise InputInvalid()
    path = PureWindowsPath(value)
    if not path.is_absolute() or len(path.parts) < 2:
        raise InputInvalid()
    return tuple(part.casefold() for part in path.parts)


def _prefix(left, right):
    return len(left) <= len(right) and left == right[:len(left)]


def _environment_valid(environment):
    if environment["source"] != "synthetic" or environment["inherit"] is not False:
        return False
    try:
        root = _windows_parts(environment["root"])
        paths = {key: _windows_parts(value) for key, value in environment["values"].items()}
    except InputInvalid:
        return False
    if any(not _prefix(root, path) or len(path) <= len(root) for path in paths.values()):
        return False
    if paths["HOME"] != paths["USERPROFILE"] or paths["TEMP"] != paths["TMP"]:
        return False
    groups = [paths[key] for key in ("ORCA_USER_DATA", "HOME", "APPDATA", "LOCALAPPDATA", "TEMP")]
    return not any(_prefix(left, right) or _prefix(right, left)
                   for index, left in enumerate(groups) for right in groups[index + 1:])


def check_candidate(candidate):
    return [{"id": check_id, "status": "matched" if valid else "mismatch",
             "reason": "policy_matched" if valid else "content_mismatch"}
            for check_id, valid in (("candidate.args", _args_valid(candidate)),
                                    ("candidate.environment", _environment_valid(candidate["environment"])))]


def exit_code_for(checks):
    if any(item["status"] == "error" and item["reason"] == "input_invalid" for item in checks):
        return 2
    if any(item["status"] == "error" for item in checks):
        return 3
    if any(item["status"] == "mismatch" for item in checks):
        return 1
    if any(item["status"] == "missing" for item in checks):
        return 4
    return 0


def build_report(checks):
    by_id = {item["id"]: item for item in checks}
    ordered = []
    for check_id in CHECK_IDS:
        item = by_id.get(check_id, {"status": "not_checked", "reason": "input_unavailable"})
        result = {"id": check_id, "status": item["status"], "reason": item["reason"]}
        if check_id in FILE_IDS:
            result.update({key: item.get(key) for key in ("size", "sha256")},
                          integrity=item.get("integrity", "unverified"))
        ordered.append(result)
    code = exit_code_for(ordered)
    return {
        "schema_version": 1, "scope": "p1-a-offline-x64-slice",
        "reference": {"orca_commit": ORCA_COMMIT, "package_version": PACKAGE_VERSION,
                      "node_version": NODE_VERSION},
        "static_result": {0: "matched", 1: "mismatch", 2: "input_invalid", 3: "error", 4: "missing"}[code],
        "checks": ordered, "not_covered": list(NOT_COVERED), "provenance": "unverified",
        "runtime_status": "unverified",
        "runtime_checks": [{"id": f"P1-U{index}", "status": "unverified"} for index in range(1, 6)],
        "launch_admission": "blocked",
    }


def main(argv=None, stdout=None, stderr=None):
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    argv = sys.argv[1:] if argv is None else argv
    # Fixed help avoids argparse/gettext implicitly inspecting locale environment.
    if argv in (["--help"], ["-h"]):
        try:
            written = stdout.write(HELP)
            if written is not None and written != len(HELP):
                raise OSError()
            stdout.flush()
            return 0
        except Exception:
            try:
                stderr.write("report_write_failed\n")
            except Exception:
                pass
            return 3
    checks = []
    try:
        # Exact shape rejects duplicates and unknown tokens without argparse echoing them.
        if len(argv) != 2 or argv[0] != "--materials":
            raise InputInvalid()
        root = validate_root(argv[1])
        checks = [{"id": "input", "status": "matched", "reason": "policy_matched"}]
        checks.extend(inspect_materials(root))
        candidate = checks[1]
        if candidate["status"] == "matched":
            try:
                value = parse_candidate(candidate.pop("_raw"))
                candidate["reason"] = "schema_matched"
                checks.extend(check_candidate(value))
            except InputInvalid:
                candidate.update(status="error", reason="input_invalid")
    except InputInvalid:
        checks = [{"id": "input", "status": "error", "reason": "input_invalid"}]
    except Exception:
        checks.append({"id": "input", "status": "error", "reason": "internal_error"})
        try:
            stderr.write("internal_error\n")
        except Exception:
            pass
    report = build_report(checks)
    try:
        output = json.dumps(report, ensure_ascii=True, separators=(",", ":")) + "\n"
        if len(output.encode("utf-8")) > REPORT_BYTES:
            raise ValueError()
        written = stdout.write(output)
        if written is not None and written != len(output):
            raise OSError()
        stdout.flush()
    except Exception:
        try:
            stderr.write("report_write_failed\n")
        except Exception:
            pass
        return 3
    return exit_code_for(report["checks"])


if __name__ == "__main__":
    raise SystemExit(main())
