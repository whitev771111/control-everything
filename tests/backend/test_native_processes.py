import contextlib
import ctypes as C
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from control_everything.sessions import native_processes as native
from control_everything.sessions import workbench
from tests.fixtures.integration_config import integration_config

TICKS = 134040096001304547


class FakeApi:
    """Local buffers and fake functions only; never opens a process."""
    def __init__(self, entries=None):
        self.entries = entries if entries is not None else [(10, 1, "zellij.exe")]
        self.index = 0; self.end_error = 18; self.snapshot = 999; self.ticks = TICKS
        self.wait = 258; self.open_ok = True; self.close_ok = True; self.image_ok = True
        self.basic_status = 0; self.basic_length = C.sizeof(native.BasicInformation)
        self.pid_change = 0; self.parent_change = 0; self.image_override = None
        self.sizing_status = 0xC0000004; self.required_override = None; self.query_status = 0
        self.returned_override = None; self.string_changes = {}; self.raw = None
        self.kernel = Mock(); self.nt = Mock()
        self.kernel.CreateToolhelp32Snapshot.side_effect = lambda flags, pid: self.snapshot
        self.kernel.Process32FirstW.side_effect = self.first
        self.kernel.Process32NextW.side_effect = self.next
        self.kernel.OpenProcess.side_effect = lambda access, inherit, pid: pid + 1000 if self.open_ok else None
        self.kernel.CloseHandle.side_effect = lambda handle: self.close_ok
        self.kernel.GetProcessId.side_effect = lambda handle: handle - 1000 + self.pid_change
        self.kernel.WaitForSingleObject.side_effect = lambda handle, milliseconds: self.wait
        self.kernel.QueryFullProcessImageNameW.side_effect = self.image
        self.kernel.GetProcessTimes.side_effect = self.times
        self.nt.NtQueryInformationProcess.side_effect = self.query

    def assign(self, pointer):
        if self.index >= len(self.entries):
            C.set_last_error(self.end_error); return False
        pid, parent, name = self.entries[self.index]
        pointer._obj.pid, pointer._obj.parent, pointer._obj.name = pid, parent, name
        return True

    def first(self, handle, pointer):
        self.index = 0; return self.assign(pointer)

    def next(self, handle, pointer):
        self.index += 1; return self.assign(pointer)

    def image(self, handle, flags, buffer, size):
        name = next(name for pid, parent, name in self.entries if pid == handle - 1000)
        buffer.value = self.image_override or "C:/example/unknown/" + name
        size._obj.value = len(buffer.value)
        return self.image_ok

    def times(self, handle, creation, exit_time, kernel, user):
        creation._obj.dwHighDateTime = self.ticks >> 32
        creation._obj.dwLowDateTime = self.ticks & 0xffffffff
        return True

    def query(self, handle, information, buffer, capacity, returned):
        if information == 0:
            pointer = buffer._obj
            pointer.pid = handle - 1000 + self.pid_change
            pointer.parent = next(parent for pid, parent, name in self.entries if pid == handle - 1000) + self.parent_change
            returned._obj.value = self.basic_length
            return self.basic_status
        raw = self.raw if self.raw is not None else '"C:/example/unknown/zellij.exe" --server marker'.encode("utf-16-le")
        required = C.sizeof(native.UnicodeString) + len(raw) + 2
        if buffer is None:
            returned._obj.value = required if self.required_override is None else self.required_override
            return self.sizing_status
        value = native.UnicodeString.from_buffer(buffer); base = C.addressof(buffer)
        value.length, value.maximum, value.buffer = len(raw), len(raw) + 2, base + C.sizeof(native.UnicodeString)
        C.memmove(value.buffer, raw, len(raw))
        for key, changed in self.string_changes.items():
            setattr(value, key, changed(base, capacity) if callable(changed) else changed)
        returned._obj.value = capacity if self.returned_override is None else self.returned_override
        return self.query_status


class NativeCensusTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeApi()
        self.clock = patch.object(native.time, "monotonic", return_value=0); self.clock.start(); self.addCleanup(self.clock.stop)
        self.wall = patch.object(native.time, "time_ns", return_value=(TICKS - 116444736000000000) * 100 + 1000)
        self.wall.start(); self.addCleanup(self.wall.stop)
        self.loader = patch.object(native, "load_api", side_effect=lambda: (self.fake.kernel, self.fake.nt))
        self.loader.start(); self.addCleanup(self.loader.stop)

    def census(self):
        return native.census(5)

    def test_all_four_names_unknown_paths_and_unrelated_processes(self):
        self.fake = FakeApi([(10 + i, 1, name) for i, name in enumerate(sorted(native.NAMES))] + [(20, 1, "unrelated.exe")])
        rows = self.census()
        self.assertEqual({row["Name"] for row in rows}, native.NAMES)
        self.assertTrue(all(set(row) == {"ProcessId", "ParentProcessId", "Name", "ExecutablePath", "CommandLine", "CreatedUtc"} for row in rows))
        self.assertTrue(all(row["ExecutablePath"].startswith("C:/example/unknown/") for row in rows))
        self.assertEqual(self.fake.kernel.OpenProcess.call_count, 4)
        self.assertEqual(self.fake.kernel.CloseHandle.call_count, 5)
        self.assertTrue(rows[0]["CreatedUtc"].endswith(".1304547Z"))

    def test_valid_empty_is_not_snapshot_or_enumeration_failure(self):
        self.fake.entries = []
        self.assertEqual(self.census(), [])
        self.fake.end_error = 5
        with self.assertRaisesRegex(ValueError, "api_failed"):
            self.census()
        self.fake.snapshot = C.c_void_p(-1).value
        self.fake.kernel.CloseHandle.reset_mock()
        with self.assertRaisesRegex(ValueError, "api_failed"):
            self.census()
        self.fake.kernel.CloseHandle.assert_not_called()

    def test_later_related_failure_or_bad_next_never_outputs_partial_rows(self):
        self.fake = FakeApi([(10, 1, "zellij.exe"), (11, 1, "wezterm.exe")])
        self.fake.kernel.OpenProcess.side_effect = lambda access, inherit, pid: pid + 1000 if pid == 10 else None
        out, err = io.StringIO(), io.StringIO()
        with patch.object(native.sys, "argv", ["native_processes"]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(native.main(), 1)
        self.assertEqual(out.getvalue(), ""); self.assertEqual(json.loads(err.getvalue())["kind"], "api_failed")
        self.assertEqual(self.fake.kernel.CloseHandle.call_count, 6)
        self.fake = FakeApi(); self.fake.end_error = 5
        with self.assertRaisesRegex(ValueError, "api_failed"):
            self.census()
        self.assertEqual(self.fake.kernel.CloseHandle.call_count, 2)

    def test_denied_query_failure_and_exit_reject_whole_census(self):
        for attribute, value in [("open_ok", False), ("image_ok", False), ("basic_status", -1),
                                 ("wait", 0), ("wait", 0xffffffff), ("wait", 99)]:
            with self.subTest(attribute=attribute, value=value):
                self.fake = FakeApi(); setattr(self.fake, attribute, value)
                with self.assertRaises(ValueError):
                    self.census()
                self.assertEqual(self.fake.kernel.CloseHandle.call_count, 1 if attribute == "open_ok" else 2)
        self.fake = FakeApi()
        self.fake.kernel.WaitForSingleObject.side_effect = [258, 0]
        with self.assertRaisesRegex(ValueError, "invalid_identity"):
            self.census()

    def test_pid_parent_size_image_and_birth_guards(self):
        for attribute, value in [("pid_change", 1), ("parent_change", 1), ("basic_length", 0),
                                 ("image_override", "C:/example/elsewhere/other.exe"), ("ticks", TICKS + 11), ("ticks", 0)]:
            with self.subTest(attribute=attribute):
                self.fake = FakeApi(); setattr(self.fake, attribute, value)
                with self.assertRaisesRegex(ValueError, "invalid_identity"):
                    self.census()
                self.assertEqual(self.fake.kernel.CloseHandle.call_count, 2)

    def test_close_failure_does_not_override_main_error_or_allow_rows(self):
        self.fake.close_ok = False
        with self.assertRaisesRegex(ValueError, "close_failed"):
            self.census()
        self.fake.image_ok = False
        with self.assertRaisesRegex(ValueError, "api_failed") as caught:
            self.census()
        self.assertIn("close_failed", caught.exception.__notes__)

    def test_unrelated_outer_exception_never_suppresses_close_failure(self):
        self.fake.close_ok = False
        try:
            raise RuntimeError("unrelated caller exception")
        except RuntimeError:
            with self.assertRaisesRegex(ValueError, "close_failed"):
                self.census()

    def test_shared_deadline_does_not_start_or_return_late_rows(self):
        with patch.object(native.time, "monotonic", return_value=5):
            with self.assertRaises(TimeoutError):
                self.census()
        self.fake.kernel.CreateToolhelp32Snapshot.assert_not_called()
        with patch.object(native.time, "monotonic", side_effect=[0, 0, 0, 6]):
            with self.assertRaises(TimeoutError):
                self.census()
        self.assertEqual(self.fake.kernel.CloseHandle.call_count, 2)

    def test_main_success_and_failure_are_single_bounded_records(self):
        for error in [None, ValueError("invalid_buffer"), OSError("private-command-or-path")]:
            out, err = io.StringIO(), io.StringIO()
            call = Mock(return_value=[], side_effect=error)
            with patch.object(native.sys, "argv", ["native_processes"]), patch.object(native, "census", call), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = native.main()
            self.assertEqual(code, 0 if error is None else 1)
            if error is None:
                self.assertEqual(json.loads(out.getvalue()), []); self.assertEqual(err.getvalue(), "")
            else:
                self.assertEqual(out.getvalue(), ""); self.assertEqual(len(err.getvalue().splitlines()), 1)
                self.assertLess(len(err.getvalue()), 180); self.assertNotIn("private", err.getvalue())
        with patch.object(native.sys, "argv", ["native_processes", "forbidden"]), patch.object(native, "census") as call, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(native.main(), 1); call.assert_not_called()

    def main_output(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(native.sys, "argv", ["native_processes"]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = native.main()
        return code, out.getvalue(), err.getvalue()

    def test_main_recovers_complete_sample_with_one_earliest_deadline(self):
        final_rows = [{"ProcessId": 42}]
        for errors in ([ValueError("api_failed")], [ValueError("invalid_identity")],
                       [ValueError("api_failed"), ValueError("invalid_identity")]):
            with self.subTest(errors=[str(error) for error in errors]):
                call = Mock(side_effect=[*errors, final_rows])
                with patch.object(native, "census", call), patch.object(native.time, "monotonic", side_effect=[0]) as clock:
                    code, out, err = self.main_output()
                self.assertEqual(code, 0); self.assertEqual(json.loads(out), final_rows)
                self.assertEqual(len(out.splitlines()), 1); self.assertEqual(err, "")
                self.assertEqual([item.args for item in call.call_args_list], [(5,)] * (len(errors) + 1))
                clock.assert_called_once_with()

    def test_main_discards_partial_sample_and_closes_before_new_complete_sample(self):
        first = FakeApi([(10, 1, "zellij.exe"), (11, 1, "wezterm.exe")])
        first.kernel.OpenProcess.side_effect = lambda access, inherit, pid: pid + 1000 if pid == 10 else None
        second = FakeApi([(20, 1, "wezterm-gui.exe"), (21, 1, "zellij.exe")])
        with patch.object(native, "load_api", side_effect=[(first.kernel, first.nt), (second.kernel, second.nt)]) as loader:
            code, out, err = self.main_output()
        self.assertEqual(code, 0); self.assertEqual(err, ""); self.assertEqual(len(out.splitlines()), 1)
        self.assertEqual([row["ProcessId"] for row in json.loads(out)], [20, 21])
        self.assertEqual(loader.call_count, 2)
        self.assertEqual([call.args[0] for call in first.kernel.CloseHandle.call_args_list], [1010, 999])
        self.assertEqual([call.args[0] for call in second.kernel.CloseHandle.call_args_list], [1020, 1021, 999])
        first.kernel.CreateToolhelp32Snapshot.assert_called_once_with(2, 0)
        second.kernel.CreateToolhelp32Snapshot.assert_called_once_with(2, 0)

    def test_main_stops_after_three_retryable_errors_without_fourth_sample(self):
        for kind in ("api_failed", "invalid_identity"):
            with self.subTest(kind=kind):
                call = Mock(side_effect=[ValueError(kind), ValueError(kind), ValueError(kind), []])
                with patch.object(native, "census", call):
                    code, out, err = self.main_output()
                self.assertEqual(code, 1); self.assertEqual(out, ""); self.assertEqual(len(err.splitlines()), 1)
                self.assertEqual(json.loads(err), {"error": "process_census_unknown", "kind": kind})
                self.assertEqual([item.args for item in call.call_args_list], [(5,)] * 3)

    def test_main_nonretryable_error_stops_immediately_or_after_first_retry(self):
        errors = [(ValueError("unsupported"), "unsupported"), (ValueError("invalid_buffer"), "invalid_buffer"),
                  (ValueError("deadline"), "deadline"), (ValueError("close_failed"), "close_failed"),
                  (ValueError("unknown private detail"), "api_failed"), (TimeoutError("deadline"), "deadline"),
                  (OSError("api_failed"), "api_failed"), (RuntimeError("api_failed"), "api_failed")]
        for error, kind in errors:
            with self.subTest(error=type(error).__name__, message=str(error)):
                call = Mock(side_effect=[error, []])
                with patch.object(native, "census", call):
                    code, out, err = self.main_output()
                self.assertEqual(code, 1); self.assertEqual(out, ""); self.assertEqual(len(err.splitlines()), 1)
                self.assertEqual(json.loads(err), {"error": "process_census_unknown", "kind": kind})
                call.assert_called_once_with(5)
        call = Mock(side_effect=[ValueError("api_failed"), ValueError("invalid_buffer"), []])
        with patch.object(native, "census", call):
            code, out, err = self.main_output()
        self.assertEqual(code, 1); self.assertEqual(out, "")
        self.assertEqual(json.loads(err), {"error": "process_census_unknown", "kind": "invalid_buffer"})
        self.assertEqual([item.args for item in call.call_args_list], [(5,)] * 2)

    def test_main_close_failed_note_prevents_retry_and_remains_in_error_payload(self):
        for kind in ("api_failed", "invalid_identity"):
            with self.subTest(kind=kind):
                error = ValueError(kind); error.add_note("close_failed")
                call = Mock(side_effect=[error, []])
                with patch.object(native, "census", call):
                    code, out, err = self.main_output()
                self.assertEqual(code, 1); self.assertEqual(out, ""); self.assertEqual(len(err.splitlines()), 1)
                self.assertEqual(json.loads(err), {"error": "process_census_unknown", "kind": kind, "close_failed": True})
                call.assert_called_once_with(5)

    def test_main_shared_deadline_prevents_a_second_snapshot_after_failure(self):
        self.fake.image_ok = False
        with patch.object(native.time, "monotonic", return_value=0) as clock:
            def close(handle):
                if handle == self.fake.snapshot:
                    clock.return_value = 5
                return True
            self.fake.kernel.CloseHandle.side_effect = close
            with patch.object(native, "census", wraps=native.census) as call:
                code, out, err = self.main_output()
        self.assertEqual(code, 1); self.assertEqual(out, "")
        self.assertEqual(json.loads(err), {"error": "process_census_unknown", "kind": "deadline"})
        self.assertEqual([item.args for item in call.call_args_list], [(5,)] * 2)
        self.fake.kernel.CreateToolhelp32Snapshot.assert_called_once_with(2, 0)
        self.assertEqual([item.args[0] for item in self.fake.kernel.CloseHandle.call_args_list], [1010, 999])

    def test_main_accepts_first_complete_empty_sample_without_retry(self):
        call = Mock(side_effect=[[], ValueError("api_failed")])
        with patch.object(native, "census", call):
            code, out, err = self.main_output()
        self.assertEqual(code, 0); self.assertEqual(out, "[]\n"); self.assertEqual(err, "")
        call.assert_called_once_with(5)


class NativeBufferTests(unittest.TestCase):
    def test_sizing_status_and_limits(self):
        for key, value in [("sizing_status", -1), ("sizing_status", 0), ("required_override", 0),
                           ("required_override", C.sizeof(native.UnicodeString)), ("required_override", 131073)]:
            fake = FakeApi(); setattr(fake, key, value)
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                native.command_line(fake.nt, 1010)
            self.assertEqual(fake.nt.NtQueryInformationProcess.call_count, 1)

    def test_pointer_maximum_and_returned_bounds_are_checked_before_read(self):
        changes = [{"length": 0}, {"length": 3}, {"maximum": 3}, {"maximum": 2}, {"buffer": 0},
                   {"buffer": lambda base, size: base + 1}, {"buffer": lambda base, size: base + size},
                   {"buffer": lambda base, size: base - 2}, {"maximum": 65534}]
        for change in changes:
            fake = FakeApi(); fake.string_changes = change
            with self.subTest(change=change), patch.object(native.C, "string_at") as read:
                with self.assertRaisesRegex(ValueError, "invalid_buffer"):
                    native.command_line(fake.nt, 1010)
                read.assert_not_called()
        for returned in (0, C.sizeof(native.UnicodeString) - 1, 131073):
            fake = FakeApi(); fake.returned_override = returned
            with self.subTest(returned=returned), self.assertRaises(ValueError):
                native.command_line(fake.nt, 1010)

    def test_utf16_embedded_nul_and_query_status_fail_closed(self):
        for raw in (b"\x00\xd8", "a\x00b".encode("utf-16-le")):
            fake = FakeApi(); fake.raw = raw
            with self.assertRaisesRegex(ValueError, "invalid_buffer"):
                native.command_line(fake.nt, 1010)
        fake = FakeApi(); fake.query_status = -1
        with self.assertRaisesRegex(ValueError, "api_failed"):
            native.command_line(fake.nt, 1010)

    def test_unsupported_platform_bits_or_missing_api(self):
        with patch.object(native.sys, "platform", "linux"):
            with self.assertRaisesRegex(ValueError, "unsupported"):
                native.load_api()
        with patch.object(native.C, "sizeof", return_value=4):
            with self.assertRaisesRegex(ValueError, "unsupported"):
                native.load_api()
        with patch.object(native.C, "WinDLL", side_effect=OSError("missing")):
            with self.assertRaisesRegex(ValueError, "unsupported"):
                native.load_api()


class NativeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0, "[]", ""))
        config = integration_config()["integrations"]
        self.workbench = workbench.Workbench(runner=self.runner, clock=lambda: 1,
                                             config=config["workbench"], terminal_config=config["terminal"])

    def test_fixed_validated_base_argv_cwd_and_remaining_timeout(self):
        with patch.object(workbench, "_native_interpreter", return_value="C:/example/fixed/python.exe"):
            self.assertEqual(self.workbench._processes(3), [])
        argv = self.runner.call_args.args[0]; options = self.runner.call_args.kwargs
        script = Path(workbench.__file__).resolve().with_name("native_processes.py")
        self.assertEqual(argv, ["C:/example/fixed/python.exe", "-I", "-B", "-X", "utf8", str(script)])
        self.assertEqual(options["cwd"], str(workbench.execution_root()))
        self.assertEqual(options["timeout"], 2); self.assertFalse(options["shell"])
        self.assertEqual(options["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_base_without_installed_package_runs_fixed_stdlib_script(self):
        import ast
        import sys

        installed = Path("C:/example/product/runtime/Lib/site-packages/control_everything")
        with patch.object(workbench, "_native_interpreter", return_value="C:/example/base/python.exe"), \
                patch.object(workbench.sys, "executable", "C:/example/venv/Scripts/python.exe"), \
                patch.object(workbench, "execution_root", return_value=installed):
            self.assertEqual(self.workbench._processes(3), [])
        argv = self.runner.call_args.args[0]
        self.assertEqual(argv[0], "C:/example/base/python.exe")
        self.assertNotIn("-m", argv)
        script = Path(argv[-1])
        self.assertEqual(script, Path(workbench.__file__).resolve().with_name("native_processes.py"))
        self.assertEqual(self.runner.call_args.kwargs["cwd"], str(installed))
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertTrue(all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names))
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                self.assertIn(node.module.split(".")[0], sys.stdlib_module_names)

    def test_expired_deadline_and_bad_interpreter_never_run(self):
        with patch.object(workbench, "_native_interpreter", return_value="C:/example/fixed/python.exe"):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.workbench._processes(1)
        self.runner.assert_not_called()
        with patch.object(workbench, "_native_interpreter", side_effect=ValueError("unknown")):
            with self.assertRaises(ValueError):
                self.workbench._processes(3)
        self.runner.assert_not_called()

    def test_parent_nonzero_bad_json_and_timeout_stay_unknown(self):
        for result in [subprocess.CompletedProcess([], 1, "private", "private"), subprocess.CompletedProcess([], 0, "invalid-json", "")]:
            self.runner.return_value = result
            with patch.object(workbench, "_native_interpreter", return_value="C:/example/fixed/python.exe"), self.assertRaises(ValueError):
                self.workbench._processes(3)
        self.runner.side_effect = subprocess.TimeoutExpired("private", 2)
        with patch.object(workbench, "_native_interpreter", return_value="C:/example/fixed/python.exe"), self.assertRaises(subprocess.TimeoutExpired):
            self.workbench._processes(3)
        for failure in (subprocess.CompletedProcess([], 1, "private", "private"),
                        subprocess.CompletedProcess([], 0, "invalid-json", ""), subprocess.TimeoutExpired("private", 2)):
            def run(argv, **options):
                if argv[0] == self.workbench.zellij:
                    text = "zellij 0.45.1" if argv[-1] == "--version" else f"{self.workbench.session} [Created 1s ago]"
                    return subprocess.CompletedProcess(argv, 0, text, "")
                if isinstance(failure, Exception):
                    raise failure
                return failure
            self.workbench.runner = run
            with patch.object(workbench, "_native_interpreter", return_value="C:/example/fixed/python.exe"), patch.object(self.workbench, "_marker_pid", return_value=10):
                state = self.workbench.public_status()
            self.assertEqual(state["reason"], "workbench_timeout" if isinstance(failure, Exception) else "workbench_observation_unknown")
            self.assertIsNone(state["gui_count"]); self.assertIsNone(state["interactive_client_count"])
            self.assertIsNone(state["unmanaged_mux_count"]); self.assertFalse(state["can_connect"]); self.assertFalse(state["can_focus"])
            self.assertNotIn("private", str(state))

    def test_actual_image_validation_accepts_only_same_absolute_base(self):
        def kernel(image, length=None):
            dll = Mock()
            def module(handle, buffer, capacity):
                buffer.value = image
                return len(image) if length is None else length
            dll.GetModuleFileNameW.side_effect = module
            return dll
        for base in (None, "", "python.exe", "/fixed/python.exe", 42):
            with patch.object(workbench.sys, "_base_executable", base), patch.object(workbench, "kernel32") as api, self.assertRaises(ValueError):
                workbench._native_interpreter()
            api.assert_not_called()
        for image, length in [("C:/example/fixed/python.exe", 0), ("C:/example/fixed/python.exe", 32768), ("C:/example/fixed/python.exe", 1),
                              ("relative.exe", None), ("C:/example/other/python.exe", None)]:
            with patch.object(workbench.sys, "_base_executable", "C:/example/fixed/python.exe"), patch.object(workbench, "kernel32", return_value=kernel(image, length)), self.assertRaises(ValueError):
                workbench._native_interpreter()
        with patch.object(workbench.sys, "_base_executable", "C:/example/FIXED/python.exe"), patch.object(workbench, "kernel32", return_value=kernel(r"c:\example\fixed\python.exe")):
            self.assertEqual(workbench._native_interpreter(), "C:/example/FIXED/python.exe")




class NativeProcessesEntryTests(unittest.TestCase):
    def test_legacy_cli_contract(self):
        import runpy
        from unittest.mock import patch

        with patch("control_everything.sessions.native_processes.main", return_value=7) as main:
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("control_everything.native_processes", run_name="__main__")
        self.assertEqual(caught.exception.code, 7)
        main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
