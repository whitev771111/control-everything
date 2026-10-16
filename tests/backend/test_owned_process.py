import ctypes as c
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from session_panel.execution.owned_process import (CREATE_NO_WINDOW, CREATE_SUSPENDED, EXTENDED_STARTUPINFO_PRESENT,
    ExecutionOwner, OwnedProcess, ProcessError, ProcessInformation, StartupInfoEx, WindowsAPI, query_old_job)


class FakeAPI:
    def __init__(self, fail=None, chunks=()):
        self.fail, self.calls, self.closed = fail, [], []
        self.chunks = list(chunks)
        self.active_count = 1
        self.bytes_written = []

    def step(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise ProcessError("process_failed")

    def create_job(self, _name):
        self.step("job")
        return 11

    def pipes(self):
        self.step("pipes")
        return 12, 13, (14, 15, 16)

    def spawn(self, exe, argv, cwd, handles):
        self.step("suspended")
        self.spawn_values = (exe, argv, cwd, handles)
        return 17, 18, 19

    def assign(self, *_):
        self.step("assign")

    def resume(self, *_):
        self.step("resume")

    def terminate_process(self, *_):
        self.step("terminate_process")
        self.active_count = 0

    def terminate_job(self, *_):
        self.step("terminate_job")
        self.active_count = 0

    def active(self, *_):
        self.step("query")
        return self.active_count

    def read_available(self, *_):
        return self.chunks.pop(0) if self.chunks else None

    def write(self, _pipe, data):
        self.step("write")
        self.bytes_written.append(data)
        return len(data) - 1 if self.fail == "partial" else len(data)

    def cancel_io(self, *_):
        self.calls.append("cancel_io")

    def close(self, handle):
        if handle:
            self.closed.append(handle)


class OwnedProcessTests(unittest.TestCase):
    def process(self, api):
        return OwnedProcess("C:/native.exe", ["fixed", "a b"], "C:/fixture", "owned-job", api)

    def test_suspended_assign_resume_and_each_handle_released_once(self):
        api = FakeAPI()
        process = self.process(api).start()
        try:
            self.assertEqual(api.calls[:5], ["job", "pipes", "suspended", "assign", "resume"])
            self.assertEqual(api.spawn_values[3], (14, 15, 16))
            self.assertEqual(process.pid, 19)
            process.write({"id": 1, "value": "中文"})
            self.assertEqual(json.loads(api.bytes_written[0]), {"id": 1, "value": "中文"})
        finally:
            self.assertTrue(process.close(grace=0))
        self.assertEqual(set(api.closed), set(range(11, 19)))
        self.assertEqual(len(api.closed), len(set(api.closed)))
        self.assertFalse(process.reader.is_alive() or process.writer.is_alive())
        self.assertTrue(process.close())

    def test_start_failures_never_resume_unassigned_or_leak_handles(self):
        expected = {"job": set(), "pipes": {11}, "suspended": set(range(11, 17)),
                    "assign": set(range(11, 19)), "resume": set(range(11, 19))}
        for point, handles in expected.items():
            with self.subTest(point=point):
                api = FakeAPI(fail=point)
                process = self.process(api)
                with self.assertRaises(ProcessError):
                    process.start()
                self.assertEqual(set(api.closed), handles)
                self.assertEqual(len(api.closed), len(set(api.closed)))
                if point != "resume":
                    self.assertNotIn("resume", api.calls)
                if point in {"assign", "resume"}:
                    self.assertIn("terminate_process", api.calls)

    def test_query_failure_cannot_claim_cleanup(self):
        api = FakeAPI(fail="query")
        process = self.process(api).start()
        self.assertFalse(process.close(grace=0))
        self.assertIn(11, api.closed)

    def test_partial_write_is_not_retried(self):
        api = FakeAPI(fail="partial")
        process = self.process(api).start()
        try:
            with self.assertRaisesRegex(ProcessError, "partial_write"):
                process.write({"method": "one"})
            self.assertEqual(len(api.bytes_written), 1)
        finally:
            process.close(grace=0)

    def test_jsonl_fragment_utf8_and_failure_boundaries(self):
        cases = [(b'bad\n', "protocol_error"), (b'\xff\n', "protocol_error"),
                 (b'{"a":NaN}\n', "protocol_error"), (b'[]\n', "protocol_error"),
                 (b'x' * (1024 * 1024 + 1), "protocol_line_too_large"), (b'', "process_eof")]
        for raw, reason in cases:
            with self.subTest(reason=reason):
                process = self.process(FakeAPI(chunks=[raw])).start()
                try:
                    with self.assertRaisesRegex(ProcessError, reason):
                        process.read(timeout=.1)
                finally:
                    process.close(grace=0)
        raw = '{"text":"中文"}\n'.encode()
        process = self.process(FakeAPI(chunks=[raw[:10], raw[10:]])).start()
        try:
            self.assertEqual(process.read(timeout=.2), {"text": "中文"})
        finally:
            process.close(grace=0)

    def test_queue_overflow_stops_reader_without_blocking_close(self):
        process = self.process(FakeAPI(chunks=[b'{"id":1}\n' * 129])).start()
        process.reader.join(1)
        self.assertFalse(process.reader.is_alive())
        self.assertEqual(str(process.failure), "protocol_queue_full")
        try:
            with self.assertRaisesRegex(ProcessError, "protocol_queue_full"):
                process.read(timeout=0)
        finally:
            self.assertTrue(process.close(grace=0))

    def test_owner_exclusion_and_query_only_recovery(self):
        api = Mock()
        api.owner.side_effect = [99, None]
        first, second = ExecutionOwner(Path("fixture.db"), api), ExecutionOwner(Path("fixture.db"), api)
        self.assertTrue(first.acquire())
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.close()
        first.close()
        api.close.assert_called_once_with(99)
        for handle, active, expected in ((None, 0, True), (50, 0, True), (50, 1, False)):
            api.reset_mock()
            api.open_job.return_value, api.active.return_value = handle, active
            self.assertEqual(query_old_job("old-owned-job", api), expected)
            api.terminate_job.assert_not_called()
            if handle:
                api.close.assert_called_once_with(handle)
        api.open_job.side_effect = ProcessError("job_query_failed")
        self.assertFalse(query_old_job("old-owned-job", api))

    def test_non_windows_never_uses_subprocess_fallback(self):
        with patch("session_panel.execution.owned_process.sys.platform", "linux"):
            self.assertFalse(ExecutionOwner("fixture.db").acquire())
            with self.assertRaisesRegex(ProcessError, "platform_unsupported"):
                OwnedProcess("exe", [], ".", "job").start()

    def test_win32_signatures_and_handle_list_flags(self):
        kernel = Mock()
        with patch("session_panel.execution.owned_process.c.WinDLL", return_value=kernel, create=True), \
                patch("session_panel.execution.owned_process.sys.platform", "win32"):
            api = WindowsAPI()
        self.assertIs(kernel.CreateFileW.restype, c.c_void_p)
        self.assertIs(kernel.CreateProcessW.argtypes[-1], c.POINTER(ProcessInformation))
        self.assertEqual(kernel.OpenJobObjectW.argtypes[0], c.c_ulong)
        inherited = []

        def initialize(_buffer, _count, _flags, size):
            c.cast(size, c.POINTER(c.c_size_t))[0] = 128
            return True

        def update(_storage, _flags, _attribute, handles, _size, *_):
            inherited.extend(handles)
            return True

        def create(exe, command, _ps, _ts, inherit, flags, _env, cwd, startup, result):
            self.assertTrue(inherit)
            self.assertEqual(flags, CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT)
            self.assertEqual(exe, "C:/native.exe")
            self.assertIn('"a b"', command.value)
            info = c.cast(startup, c.POINTER(StartupInfoEx)).contents
            self.assertEqual(info.startup.cb, c.sizeof(StartupInfoEx))
            self.assertEqual([info.startup.stdin, info.startup.stdout, info.startup.stderr], [14, 15, 16])
            info = c.cast(result, c.POINTER(ProcessInformation)).contents
            info.process, info.thread, info.pid = 0x123456789, 0x223456789, 123
            return True

        kernel.InitializeProcThreadAttributeList.side_effect = initialize
        kernel.UpdateProcThreadAttribute.side_effect = update
        kernel.CreateProcessW.side_effect = create
        self.assertEqual(api.spawn("C:/native.exe", ["a b"], "C:/fixture", (14, 15, 16)),
                         (0x123456789, 0x223456789, 123))
        self.assertEqual(inherited, [14, 15, 16])
        kernel.DeleteProcThreadAttributeList.assert_called_once()


if __name__ == "__main__":
    unittest.main()
