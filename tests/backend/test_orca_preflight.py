import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import webbrowser


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "orca_preflight.py"
SPEC = importlib.util.spec_from_file_location("orca_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def candidate():
    root = "C:\\orca-p1-fixture"
    return {
        "target": "win32-x64", "route": "direct-node-entry",
        "argv": ["--bind", "127.0.0.1", "--port", "0", "--json", "--no-pairing"],
        "environment": {"source": "synthetic", "inherit": False, "root": root,
                        "values": {key: root + "\\" + leaf for key, leaf in (
                            ("ORCA_USER_DATA", "profile"), ("USERPROFILE", "home"),
                            ("HOME", "home"), ("APPDATA", "appdata"),
                            ("LOCALAPPDATA", "localappdata"), ("TEMP", "tmp"), ("TMP", "tmp"))}},
    }


def metadata(info, **changes):
    values = {key: getattr(info, key) for key in (
        "st_mode", "st_nlink", "st_dev", "st_ino", "st_size", "st_mtime_ns")}
    values["st_file_attributes"] = getattr(info, "st_file_attributes", 0)
    values.update(changes)
    return SimpleNamespace(**values)


class OrcaPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def fixture(self, with_candidate=True):
        if with_candidate:
            self.write("candidate.json", json.dumps(candidate()).encode())
        self.write("source/package.json", b'{"name":"orca","version":"1.4.214","engines":{"node":"24"}}')
        self.write("slot/.server-target", b"win32-x64\r\n")
        self.write("slot/.runtime-node", preflight.NODE_SHA256.encode() + b"\n")
        for filename in ("orcad.js", "orcad-server.js", "daemon-entry.js"):
            self.write("slot/" + filename, b"synthetic fixture; never executed")

    def run_main(self, argv=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = preflight.main(["--materials", str(self.root)] if argv is None else argv, stdout, stderr)
        output = stdout.getvalue()
        report = json.loads(output)
        self.assertEqual(output.count("\n"), 1)
        self.assertLessEqual(len(output.encode()), preflight.REPORT_BYTES)
        return code, report, output, stderr.getvalue()

    def checks(self, report):
        self.assertEqual(tuple(item["id"] for item in report["checks"]), preflight.CHECK_IDS)
        return {item["id"]: item for item in report["checks"]}

    def synthetic_node(self, size=None, digest=None):
        # Branch-only fixture: no actual pinned Node is present or verified.
        original = preflight.inspect_file

        def inspect(root, relative, limit, capture=False, **kwargs):
            if relative == preflight.NODE_PATH:
                return preflight._record("matched", "nonempty_file",
                                         size=preflight.NODE_SIZE if size is None else size,
                                         sha256=preflight.NODE_SHA256 if digest is None else digest)
            return original(root, relative, limit, capture, **kwargs)
        return patch.object(preflight, "inspect_file", side_effect=inspect)

    def assert_blocked(self, report):
        self.assertEqual(report["runtime_status"], "unverified")
        self.assertEqual(report["provenance"], "unverified")
        self.assertEqual(report["launch_admission"], "blocked")
        self.assertEqual(report["not_covered"], list(preflight.NOT_COVERED))
        self.assertEqual(report["runtime_checks"], [
            {"id": f"P1-U{index}", "status": "unverified"} for index in range(1, 6)])

    def test_material_absence_and_summary(self):
        code, report, _, error = self.run_main()
        self.assertEqual(code, 4)
        self.assertEqual(report["static_result"], "missing")
        checks = self.checks(report)
        for check_id in preflight.CHECK_IDS[1:9]:
            self.assertEqual(checks[check_id]["status"], "missing")
        self.assertEqual(checks["slot.no_bun_marker"]["status"], "matched")
        for check_id in preflight.FILE_IDS:
            self.assertEqual(checks[check_id]["integrity"], "unverified")
            self.assertIn("size", checks[check_id])
            self.assertIn("sha256", checks[check_id])
        self.assertNotIn("sha256", checks["candidate.input"])
        self.assertEqual(error, "")
        self.assert_blocked(report)

    def test_bounded_hash(self):
        path = self.write("abc", b"abc")
        with patch.object(preflight, "CHUNK_BYTES", 1):
            result = preflight.inspect_file(self.root, "abc", 3, True)
        self.assertEqual(result["sha256"], "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertEqual(result["_raw"], b"abc")
        with patch.object(Path, "open", side_effect=AssertionError("must not open")):
            self.assertEqual(preflight.inspect_file(self.root, "abc", 2)["reason"], "too_large")
            self.assertEqual(preflight.inspect_file(self.root, "abc", 4, _budget=[2])["reason"], "too_large")
        budget = [3]
        self.assertEqual(preflight.inspect_file(self.root, "abc", 3, _budget=budget)["status"], "matched")
        self.assertEqual(budget, [0])
        info = path.stat()
        for change in ({"st_size": 4}, {"st_mtime_ns": info.st_mtime_ns + 1}, {"st_ino": info.st_ino + 1}):
            with self.subTest(change=change), patch.object(preflight.os, "fstat", side_effect=[info, metadata(info, **change)]):
                self.assertEqual(preflight.inspect_file(self.root, "abc", 3)["reason"], "changed_during_read")
        original_open = Path.open
        reads = []

        class Stream:
            def __init__(self, stream, fail=False, growth=False):
                self.stream, self.fail, self.growth = stream, fail, growth
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def fileno(self):
                return self.stream.fileno()
            def read(self, count):
                reads.append(count)
                if self.fail:
                    raise OSError("private error")
                if self.growth:
                    self.growth = False
                    return b"abcd"
                return self.stream.read(count)

        opened = []
        def open_stream(file, mode, **kwargs):
            stream = Stream(original_open(file, mode, **kwargs), fail=True)
            opened.append(stream.stream)
            return stream
        with patch.object(Path, "open", open_stream):
            self.assertEqual(preflight.inspect_file(self.root, "abc", 3)["reason"], "read_failed")
        self.assertTrue(opened[0].closed)
        self.assertLessEqual(max(reads), 4)
        with patch.object(Path, "open", lambda file, mode: Stream(original_open(file, mode), growth=True)):
            self.assertEqual(preflight.inspect_file(self.root, "abc", 3)["reason"], "changed_during_read")

    def test_total_budget_across_materials(self):
        self.write("slot/orcad.js", b"abc")
        self.write("slot/orcad-server.js", b"def")
        self.write("slot/daemon-entry.js", b"g")
        with patch.object(preflight, "TOTAL_BYTES", 5):
            checks = {item["id"]: item for item in preflight.inspect_materials(self.root)}
        self.assertEqual(checks["slot.launcher"]["status"], "matched")
        self.assertEqual(checks["slot.server"]["reason"], "too_large")
        self.assertEqual(checks["slot.daemon"]["status"], "matched")
        self.assertEqual(checks["slot.daemon"]["size"], 1)
        original = Path.lstat
        launcher = self.root / "slot/orcad.js"
        def oversized(path):
            info = original(path)
            return metadata(info, st_size=64 * 1024 * 1024 + 1) if path == launcher else info
        with patch.object(Path, "lstat", oversized):
            checks = {item["id"]: item for item in preflight.inspect_materials(self.root)}
        self.assertEqual(checks["slot.launcher"]["reason"], "too_large")

    def test_slot_slice(self):
        self.fixture()
        checks = {item["id"]: item for item in preflight.inspect_materials(self.root)}
        self.assertEqual(checks["slot.target"]["status"], "matched")
        self.assertEqual(checks["slot.launcher"]["integrity"], "unverified")
        for raw in (b"", b"win32-arm64", b"win32-x64\n\n", b"win32-x64 ", b"win32-x64\r", b"\xff"):
            with self.subTest(raw=raw):
                self.write("slot/.server-target", raw)
                checks = {item["id"]: item for item in preflight.inspect_materials(self.root)}
                self.assertEqual(checks["slot.target"]["status"], "mismatch")
        self.write("slot/.runtime-node", preflight.NODE_SHA256.upper().encode())
        self.write("slot/orcad-server.js", b"")
        (self.root / "slot/orcad.js").unlink()
        checks = {item["id"]: item for item in preflight.inspect_materials(self.root)}
        self.assertEqual(checks["slot.node_marker"]["status"], "mismatch")
        self.assertEqual(checks["slot.server"]["status"], "mismatch")
        self.assertEqual(checks["slot.launcher"]["status"], "missing")
        marker = self.write("slot/.build-target", b"")
        with patch.object(Path, "open", wraps=Path.open) as opened:
            # The forbidden marker itself is never opened.
            checks = {item["id"]: item for item in preflight.inspect_materials(self.root)}
            self.assertFalse(any(str(marker) in str(call) for call in opened.call_args_list))
        self.assertEqual(checks["slot.no_bun_marker"]["status"], "mismatch")
        marker.unlink()
        marker.mkdir()
        checks = {item["id"]: item for item in preflight.inspect_materials(self.root)}
        self.assertEqual(checks["slot.no_bun_marker"]["status"], "mismatch")
        original = Path.lstat
        def linked_marker(path):
            info = original(path)
            return metadata(info, st_mode=stat.S_IFLNK) if path == marker else info
        with patch.object(Path, "lstat", linked_marker):
            checks = {item["id"]: item for item in preflight.inspect_materials(self.root)}
        self.assertEqual(checks["slot.no_bun_marker"]["status"], "mismatch")

    def test_node_pin(self):
        self.assertEqual(preflight.NODE_SIZE, 93580104)
        self.assertEqual(preflight.NODE_VERSION, "24.21.0")
        self.assertEqual(preflight.NODE_SHA256, "ba4e6d110e8c1592a1ecd390f6b05f3da124b13871a5be62b341a07a853c6c32")
        self.write(preflight.NODE_PATH, b"abc")
        checks = {item["id"]: item for item in preflight.inspect_materials(self.root)}
        self.assertEqual(checks["node.executable"]["reason"], "size_mismatch")
        for size, digest, reason in ((3, "0" * 64, "size_mismatch"),
                                     (preflight.NODE_SIZE, "0" * 64, "hash_mismatch"),
                                     (preflight.NODE_SIZE, preflight.NODE_SHA256, "pin_matched")):
            with self.subTest(reason=reason), self.synthetic_node(size, digest):
                checks = {item["id"]: item for item in preflight.inspect_materials(self.root)}
                self.assertEqual(checks["node.executable"]["reason"], reason)
                self.assertEqual(checks["node.executable"]["integrity"],
                                 "pin_matched" if reason == "pin_matched" else "unverified")

    def test_package_fields(self):
        package = {"name": "orca", "version": "1.4.214", "engines": {"node": "24"}}
        variants = [(json.dumps(package).encode(), "matched")]
        for key, value in (("name", "other"), ("version", "1.4.213"), ("engines", {"node": "23"})):
            changed = copy.deepcopy(package)
            changed[key] = value
            variants.append((json.dumps(changed).encode(), "mismatch"))
        for key in package:
            changed = copy.deepcopy(package)
            del changed[key]
            variants.append((json.dumps(changed).encode(), "mismatch"))
        variants += [(raw, "mismatch") for raw in (
            b'{"name":"orca","name":"orca"}', b"\xff", b"{", b"[]", b"NaN", b"Infinity", b"-Infinity")]
        for raw, status in variants:
            with self.subTest(raw=raw):
                self.write("source/package.json", raw)
                item = next(item for item in preflight.inspect_materials(self.root) if item["id"] == "source.package")
                self.assertEqual(item["status"], status)
                self.assertEqual(item["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(item["integrity"], "unverified")

    def test_cli_root_boundary(self):
        for argv in ([], ["--materials"], ["--unknown", "secret"],
                     ["--materials", str(self.root), "--materials", str(self.root)],
                     ["--materials", "relative"], ["--materials", "C:relative"],
                     ["--materials", "\\\\server\\share"], ["--materials", "\\\\?\\C:\\fixture"],
                     ["--materials", "\\\\.\\C:\\fixture"], ["--materials", "//server/share"],
                     ["--materials", "bad\0path"], ["--materials", str(self.root / "absent")]):
            with self.subTest(argv=argv), patch.object(Path, "open", side_effect=AssertionError("no materials")):
                code, report, _, _ = self.run_main(argv)
                self.assertEqual(code, 2)
                checks = self.checks(report)
                self.assertEqual(checks["input"]["reason"], "input_invalid")
                for check_id in preflight.CHECK_IDS[1:]:
                    self.assertEqual(checks[check_id]["status"], "not_checked")
                    self.assertEqual(checks[check_id]["reason"], "input_unavailable")
                self.assert_blocked(report)
        file_root = self.write("file", b"abc")
        self.assertEqual(self.run_main(["--materials", str(file_root)])[0], 2)
        original = Path.lstat
        for link in (stat.FILE_ATTRIBUTE_REPARSE_POINT, 0):
            def lstat(path):
                info = original(path)
                return metadata(info, st_file_attributes=link, st_mode=stat.S_IFLNK) if path == self.root else info
            with patch.object(Path, "lstat", lstat):
                self.assertEqual(self.run_main()[0], 2)
        def ancestor_lstat(path):
            info = original(path)
            return metadata(info, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT) if path == self.root.parent else info
        with patch.object(Path, "lstat", ancestor_lstat), patch.object(Path, "open", side_effect=AssertionError("no materials")):
            self.assertEqual(self.run_main()[0], 2)

    def test_candidate_invalid_continues_materials(self):
        self.fixture()
        malformed = [b"x" * (preflight.CANDIDATE_BYTES + 1), b'{"unknown":true}', b"[]", b"{", b"\xff",
                     b"[" * 1100 + b"0" + b"]" * 1100, b'{"target":1,"target":2}', b"NaN"]
        wrong = candidate()
        wrong["extra"] = True
        malformed.append(json.dumps(wrong).encode())
        for raw in malformed:
            with self.subTest(length=len(raw)):
                self.write("candidate.json", raw)
                code, report, _, _ = self.run_main()
                self.assertEqual(code, 2)
                checks = self.checks(report)
                self.assertEqual(checks["candidate.input"]["status"], "error")
                self.assertEqual(checks["candidate.input"]["reason"], "input_invalid")
                self.assertEqual(checks["source.package"]["status"], "matched")
                self.assertEqual(checks["slot.target"]["status"], "matched")
                self.assertEqual(checks["node.executable"]["status"], "missing")
                self.assertEqual(checks["candidate.args"]["status"], "not_checked")
                self.assertEqual(checks["candidate.environment"]["status"], "not_checked")
        raw = json.dumps(candidate()).encode()
        self.write("candidate.json", raw + b" " * (preflight.CANDIDATE_BYTES - len(raw)))
        checks = self.checks(self.run_main()[1])
        self.assertEqual(checks["candidate.input"]["status"], "matched")

    def test_candidate_missing_continues_materials(self):
        self.fixture(False)
        for extra, code in ((None, 4), ("mismatch", 1), ("error", 3)):
            original = preflight.inspect_file
            def inspect(root, relative, limit, capture=False, **kwargs):
                if relative == "slot/orcad.js" and extra:
                    return preflight._record(extra, "read_failed" if extra == "error" else "content_mismatch")
                return original(root, relative, limit, capture, **kwargs)
            with self.subTest(extra=extra), patch.object(preflight, "inspect_file", side_effect=inspect):
                actual, report, _, _ = self.run_main()
                self.assertEqual(actual, code)
                checks = self.checks(report)
                self.assertEqual(checks["candidate.input"]["status"], "missing")
                self.assertEqual(checks["candidate.input"]["reason"], "absent")
                self.assertEqual(checks["source.package"]["status"], "matched")
                self.assertEqual(checks["candidate.args"]["reason"], "input_unavailable")
        with self.synthetic_node():
            self.assertEqual(self.run_main()[0], 4)

    def test_path_rejection(self):
        path = self.write("ordinary", b"abc")
        info = path.stat()
        original = Path.lstat
        for changes, reason in (({"st_mode": stat.S_IFLNK}, "path_rejected"),
                                ({"st_file_attributes": stat.FILE_ATTRIBUTE_REPARSE_POINT}, "path_rejected"),
                                ({"st_nlink": 2}, "path_rejected"),
                                ({"st_mode": stat.S_IFIFO}, "not_regular")):
            def lstat(file):
                return metadata(info, **changes) if file == path else original(file)
            with self.subTest(changes=changes), patch.object(Path, "lstat", lstat), patch.object(Path, "open", side_effect=AssertionError("reject before open")):
                self.assertEqual(preflight.inspect_file(self.root, "ordinary", 4)["reason"], reason)
        with patch.object(Path, "lstat", side_effect=PermissionError("private")):
            self.assertEqual(preflight.inspect_file(self.root, "ordinary", 4)["status"], "error")
        with patch.object(Path, "open", side_effect=PermissionError("private")):
            self.assertEqual(preflight.inspect_file(self.root, "ordinary", 4)["reason"], "read_failed")
        with patch.object(preflight.os, "fstat", return_value=metadata(info, st_nlink=2)):
            self.assertEqual(preflight.inspect_file(self.root, "ordinary", 4)["reason"], "changed_during_read")
        self.assertEqual(preflight.inspect_file(self.root, "ordinary/file", 4)["reason"], "path_rejected")
        with patch.object(preflight.os, "fstat", return_value=metadata(info, st_ino=info.st_ino + 1)):
            self.assertEqual(preflight.inspect_file(self.root, "ordinary", 4)["reason"], "changed_during_read")

    def test_args_policy(self):
        good = candidate()["argv"]
        invalid = []
        for flag in ("--bind", "--port", "--json", "--no-pairing"):
            changed = good.copy()
            index = changed.index(flag)
            del changed[index:index + (2 if flag in ("--bind", "--port") else 1)]
            invalid.append(changed)
            invalid.append(good + [flag])
        invalid += [["--bind", "--port", "0", "--json", "--no-pairing"],
                    ["--bind=127.0.0.1", "--port", "0", "--json", "--no-pairing"],
                    good + ["--help"], ["orca", "serve"], [" ".join(good)]]
        for address in ("0.0.0.0", "localhost", "::1", "127.0.0.1 "):
            changed = good.copy()
            changed[1] = address
            invalid.append(changed)
        for port in ("65536", "01", "", "-1", "1.0", "1e2", " true", "١", "0 "):
            changed = good.copy()
            changed[3] = port
            invalid.append(changed)
        for argv in invalid:
            with self.subTest(argv=argv):
                value = candidate()
                value["argv"] = argv
                self.assertEqual(preflight.check_candidate(value)[0]["status"], "mismatch")
        for port in ("0", "1", "65535"):
            value = candidate()
            value["argv"] = ["--no-pairing", "--port", port, "--json", "--bind", "127.0.0.1"]
            self.assertEqual(preflight.check_candidate(value)[0]["status"], "matched")
        for key, value in (("route", "orca-serve"), ("target", "win32-arm64")):
            changed = candidate()
            changed[key] = value
            self.assertEqual(preflight.check_candidate(changed)[0]["status"], "mismatch")
        for argv in ([True], ["x" * 65], ["--json"] * 9, "shell string"):
            value = candidate()
            value["argv"] = argv
            with self.assertRaises(preflight.InputInvalid):
                preflight.parse_candidate(json.dumps(value).encode())

    def test_environment_policy(self):
        for inherit in (0, "false", None):
            value = candidate()
            value["environment"]["inherit"] = inherit
            with self.assertRaises(preflight.InputInvalid):
                preflight.parse_candidate(json.dumps(value).encode())
        for key in ("TOKEN", "PATH", "home"):
            value = candidate()
            value["environment"]["values"][key] = "secret"
            with self.assertRaises(preflight.InputInvalid):
                preflight.parse_candidate(json.dumps(value).encode())
        for bad in ("\\\\host\\share\\profile", "\\\\?\\C:\\fixture", "C:relative", "profile",
                    "C:\\orca-p1-fixture\\p:stream", "C:\\orca-p1-fixture\\NUL.txt",
                    "C:\\orca-p1-fixture\\COM1", "C:\\orca-p1-fixture\\..\\p",
                    "C:\\orca-p1-fixture\\.\\p", "C:\\orca-p1-fixture\\profile.",
                    "C:\\orca-p1-fixture\\profile ", "C:\\orca-p1-fixture-other\\profile",
                    "C:\\orca-p1-fixture\\%TOKEN%", "C:\\orca-p1-fixture\\$TOKEN",
                    "C:\\orca-p1-fixture\\p\0", "C:\\orca-p1-fixture\\p\n", "C:\\orca-p1-fixture"):
            with self.subTest(path=bad):
                value = candidate()
                value["environment"]["values"]["ORCA_USER_DATA"] = bad
                self.assertEqual(preflight.check_candidate(value)[1]["status"], "mismatch")
        for key, new in (("inherit", True), ("source", "host"), ("root", "C:\\")):
            value = candidate()
            value["environment"][key] = new
            self.assertEqual(preflight.check_candidate(value)[1]["status"], "mismatch")
        for key, new in (("HOME", "C:\\orca-p1-fixture\\different"),
                         ("TMP", "C:\\orca-p1-fixture\\different"),
                         ("ORCA_USER_DATA", "C:\\orca-p1-fixture\\home"),
                         ("ORCA_USER_DATA", "C:\\orca-p1-fixture\\home\\nested")):
            value = candidate()
            value["environment"]["values"][key] = new
            self.assertEqual(preflight.check_candidate(value)[1]["status"], "mismatch")
        value = candidate()
        value["environment"]["root"] = "c:/合成 工作台"
        value["environment"]["values"] = {key: path.replace("C:\\orca-p1-fixture", "C:\\合成 工作台")
                                                   for key, path in value["environment"]["values"].items()}
        self.assertEqual(preflight.check_candidate(value)[1]["status"], "matched")

    def test_reserved_device_space_variants(self):
        for leaf in ("CON .txt", "COM1 .log", "LPT1 .log", "con .TXT", "com1 .LOG",
                     "lpt1 .LOG", "cOn .tXt", "CoM1 .lOg", "LpT1 .lOg", "CON .txt\\child"):
            with self.subTest(leaf=leaf):
                value = candidate()
                value["environment"]["values"]["ORCA_USER_DATA"] += "\\" + leaf
                parsed = preflight.parse_candidate(json.dumps(value).encode())
                self.assertEqual(preflight.check_candidate(parsed)[1]["status"], "mismatch")
        for leaf in ("ordinary name.txt", "CON folder.txt", "COM1 logs.txt", "LPT1 logs.txt"):
            with self.subTest(leaf=leaf):
                value = candidate()
                value["environment"]["values"]["ORCA_USER_DATA"] += "\\" + leaf
                self.assertEqual(preflight.check_candidate(value)[1]["status"], "matched")

    def test_reporting_never_admits_runtime(self):
        issues = [("missing", "absent"), ("mismatch", "content_mismatch"),
                  ("error", "read_failed"), ("error", "input_invalid")]
        for count, expected in ((1, 4), (2, 1), (3, 3), (4, 2)):
            checks = [{"id": preflight.CHECK_IDS[index], "status": status, "reason": reason}
                      for index, (status, reason) in enumerate(issues[:count])]
            report = preflight.build_report(checks)
            self.assertEqual(preflight.exit_code_for(report["checks"]), expected)
            self.assertEqual([(item["status"], item["reason"]) for item in report["checks"][:count]], issues[:count])
            self.assert_blocked(report)
        self.fixture()
        with self.synthetic_node():
            code, report, _, _ = self.run_main()
            self.assertEqual(code, 0)
            self.assertEqual(report["static_result"], "matched")
            self.assert_blocked(report)
        value = candidate()
        value["runtime_safe"] = True
        with self.assertRaises(preflight.InputInvalid):
            preflight.parse_candidate(json.dumps(value).encode())

    def test_no_side_effects_or_secrets(self):
        self.fixture()
        sentinel = "UNIQUE_SECRET_0cf6f5bc"
        value = candidate()
        value["environment"]["values"]["TOKEN"] = sentinel
        self.write("candidate.json", json.dumps(value).encode())
        accessed = []
        original = Path.open
        def opened(path, *args, **kwargs):
            accessed.append(path.relative_to(self.root).as_posix())
            return original(path, *args, **kwargs)
        class ForbiddenEnvironment:
            def __getattribute__(self, _name):
                raise AssertionError("host environment accessed")
            def __getitem__(self, _key):
                raise AssertionError("host environment accessed")
        with patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess")), \
                patch.object(subprocess, "run", side_effect=AssertionError("subprocess")), \
                patch.object(os, "system", side_effect=AssertionError("system")), \
                patch.object(socket, "socket", side_effect=AssertionError("socket")), \
                patch.object(webbrowser, "open", side_effect=AssertionError("browser")), \
                patch.object(os, "environ", ForbiddenEnvironment()), patch.object(Path, "open", opened):
            code, report, output, error = self.run_main()
        self.assertEqual(code, 2)
        self.assertNotIn(sentinel, output + error)
        self.assertEqual(accessed, ["candidate.json", "source/package.json", "slot/.server-target",
                                    "slot/.runtime-node", "slot/orcad.js", "slot/orcad-server.js", "slot/daemon-entry.js"])
        self.assert_blocked(report)
        with patch.object(os, "environ", ForbiddenEnvironment()):
            self.assertEqual(preflight.main(["--help"], io.StringIO(), io.StringIO()), 0)
        for argv in (["--unknown", sentinel], ["--materials", sentinel]):
            _, _, output, error = self.run_main(argv)
            self.assertNotIn(sentinel, output + error)
        with patch.object(preflight, "inspect_materials", side_effect=RuntimeError(sentinel)):
            code, report, output, error = self.run_main()
            self.assertEqual(code, 3)
            self.assertEqual(error, "internal_error\n")
            self.assertNotIn(sentinel, output + error)
            self.assert_blocked(report)

    def test_cli_output_failure(self):
        first = self.run_main()
        self.assertEqual(first, self.run_main())
        class BrokenOutput:
            def write(self, _value):
                raise OSError("SECRET_WRITE_FAILURE")
        error = io.StringIO()
        self.assertEqual(preflight.main(["--materials", str(self.root)], BrokenOutput(), error), 3)
        self.assertEqual(error.getvalue(), "report_write_failed\n")
        class BufferedFailure(io.StringIO):
            def flush(self):
                raise OSError("SECRET_FLUSH_FAILURE")
        error = io.StringIO()
        self.assertEqual(preflight.main([], BufferedFailure(), error), 3)
        self.assertEqual(error.getvalue(), "report_write_failed\n")
        class ShortOutput:
            def write(self, _value):
                return 1
        self.assertEqual(preflight.main([], ShortOutput(), io.StringIO()), 3)
        output = io.StringIO()
        self.assertEqual(preflight.main(["--help"], output, io.StringIO()), 0)
        self.assertIn("no exit code admits runtime launch", output.getvalue())
        with patch.object(preflight, "REPORT_BYTES", 1):
            error = io.StringIO()
            self.assertEqual(preflight.main([], io.StringIO(), error), 3)
            self.assertEqual(error.getvalue(), "report_write_failed\n")


if __name__ == "__main__":
    unittest.main()
