"""Owned Windows Job, exclusive database owner, and bounded JSONL pipes."""

import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 1
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_QUERY = 0x0004
ERROR_FILE_NOT_FOUND = 2
ERROR_BROKEN_PIPE = 109
INVALID_HANDLE_VALUE = c.c_void_p(-1).value
MAX_LINE = 1024 * 1024


class ProcessError(Exception):
    """Only a fixed reason crosses the process boundary."""


class SecurityAttributes(c.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", w.LPVOID), ("inherit", w.BOOL)]


class StartupInfo(c.Structure):
    _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                ("title", w.LPWSTR), ("x", w.DWORD), ("y", w.DWORD),
                ("width", w.DWORD), ("height", w.DWORD), ("chars_x", w.DWORD),
                ("chars_y", w.DWORD), ("fill", w.DWORD), ("flags", w.DWORD),
                ("show", w.WORD), ("reserved_size", w.WORD), ("reserved_data", w.LPVOID),
                ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]


class StartupInfoEx(c.Structure):
    _fields_ = [("startup", StartupInfo), ("attributes", w.LPVOID)]


class ProcessInformation(c.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


class BasicLimits(c.Structure):
    _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong),
                ("flags", w.DWORD), ("min_ws", c.c_size_t), ("max_ws", c.c_size_t),
                ("active_limit", w.DWORD), ("affinity", c.c_size_t),
                ("priority", w.DWORD), ("scheduling", w.DWORD)]


class IOCounters(c.Structure):
    _fields_ = [(key, c.c_ulonglong) for key in
                ("reads", "writes", "others", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimits(c.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IOCounters), ("process_memory", c.c_size_t),
                ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]


class JobAccounting(c.Structure):
    _fields_ = [(key, c.c_longlong) for key in ("user", "kernel", "period_user", "period_kernel")] + [
        (key, w.DWORD) for key in ("page_faults", "total", "active", "terminated")]


class WindowsAPI:
    """Small typed Win32 boundary; tests replace this boundary, never spawn."""

    def __init__(self):
        if sys.platform != "win32":
            raise ProcessError("platform_unsupported")
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateFileW": ([w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "CreateJobObjectW": ([w.LPVOID, w.LPCWSTR], w.HANDLE),
            "OpenJobObjectW": ([w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, c.c_int, w.LPVOID, w.DWORD], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, c.c_int, w.LPVOID, w.DWORD, w.LPVOID], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "CreatePipe": ([c.POINTER(w.HANDLE), c.POINTER(w.HANDLE), w.LPVOID, w.DWORD], w.BOOL),
            "SetHandleInformation": ([w.HANDLE, w.DWORD, w.DWORD], w.BOOL),
            "InitializeProcThreadAttributeList": ([w.LPVOID, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)], w.BOOL),
            "UpdateProcThreadAttribute": ([w.LPVOID, w.DWORD, c.c_size_t, w.LPVOID, c.c_size_t, w.LPVOID, w.LPVOID], w.BOOL),
            "DeleteProcThreadAttributeList": ([w.LPVOID], None),
            "CreateProcessW": ([w.LPCWSTR, w.LPWSTR, w.LPVOID, w.LPVOID, w.BOOL,
                                w.DWORD, w.LPVOID, w.LPCWSTR, w.LPVOID, c.POINTER(ProcessInformation)], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "ResumeThread": ([w.HANDLE], w.DWORD),
            "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
            "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
            "PeekNamedPipe": ([w.HANDLE, w.LPVOID, w.DWORD, w.LPVOID, c.POINTER(w.DWORD), w.LPVOID], w.BOOL),
            "ReadFile": ([w.HANDLE, w.LPVOID, w.DWORD, c.POINTER(w.DWORD), w.LPVOID], w.BOOL),
            "WriteFile": ([w.HANDLE, w.LPVOID, w.DWORD, c.POINTER(w.DWORD), w.LPVOID], w.BOOL),
            "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "CancelSynchronousIo": ([w.HANDLE], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result

    def checked(self, result):
        if not result or result == INVALID_HANDLE_VALUE:
            raise ProcessError("process_failed")
        return result

    def close(self, handle):
        if handle:
            self.kernel.CloseHandle(handle)

    def owner(self, path):
        handle = self.kernel.CreateFileW(str(path), 0xC0000000, 0, None, 4, 0x80, None)
        return None if handle == INVALID_HANDLE_VALUE else handle

    def create_job(self, name):
        handle = self.checked(self.kernel.CreateJobObjectW(None, name))
        try:
            # A collision must never attach this run to an existing Job.
            if c.get_last_error() == 183:
                raise ProcessError("job_name_collision")
            limits = ExtendedLimits()
            limits.basic.flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            self.checked(self.kernel.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits)))
            return handle
        except BaseException:
            self.close(handle)
            raise

    def open_job(self, name):
        handle = self.kernel.OpenJobObjectW(JOB_OBJECT_QUERY, False, name)
        if not handle and c.get_last_error() != ERROR_FILE_NOT_FOUND:
            raise ProcessError("job_query_failed")
        return handle

    def active(self, job):
        accounting = JobAccounting()
        self.checked(self.kernel.QueryInformationJobObject(job, 1, c.byref(accounting), c.sizeof(accounting), None))
        return accounting.active

    def pipes(self):
        handles = []
        attributes = SecurityAttributes(c.sizeof(SecurityAttributes), None, True)
        try:
            child_in, parent_in, parent_out, child_out = (w.HANDLE() for _ in range(4))
            self.checked(self.kernel.CreatePipe(c.byref(child_in), c.byref(parent_in), c.byref(attributes), 0))
            handles.extend([child_in.value, parent_in.value])
            self.checked(self.kernel.CreatePipe(c.byref(parent_out), c.byref(child_out), c.byref(attributes), 0))
            handles.extend([parent_out.value, child_out.value])
            self.checked(self.kernel.SetHandleInformation(parent_in, HANDLE_FLAG_INHERIT, 0))
            self.checked(self.kernel.SetHandleInformation(parent_out, HANDLE_FLAG_INHERIT, 0))
            nul = self.checked(self.kernel.CreateFileW("NUL", 0x40000000, 3, c.byref(attributes), 3, 0x80, None))
            handles.append(nul)
            return parent_in.value, parent_out.value, (child_in.value, child_out.value, nul)
        except BaseException:
            for handle in handles:
                self.close(handle)
            raise

    def spawn(self, exe, argv, cwd, child_handles):
        size = c.c_size_t()
        self.kernel.InitializeProcThreadAttributeList(None, 1, 0, c.byref(size))
        storage = c.create_string_buffer(size.value)
        self.checked(self.kernel.InitializeProcThreadAttributeList(storage, 1, 0, c.byref(size)))
        try:
            inherited = (w.HANDLE * 3)(*child_handles)
            self.checked(self.kernel.UpdateProcThreadAttribute(storage, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                                               inherited, c.sizeof(inherited), None, None))
            startup, process = StartupInfoEx(), ProcessInformation()
            startup.startup.cb = c.sizeof(startup)
            startup.startup.flags = STARTF_USESTDHANDLES
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = child_handles
            startup.attributes = c.cast(storage, w.LPVOID)
            command = c.create_unicode_buffer(subprocess.list2cmdline([str(exe), *argv]))
            flags = CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT
            self.checked(self.kernel.CreateProcessW(str(exe), command, None, None, True, flags,
                                                    None, str(cwd), c.byref(startup), c.byref(process)))
            return process.process, process.thread, process.pid
        finally:
            self.kernel.DeleteProcThreadAttributeList(storage)

    def assign(self, job, process):
        self.checked(self.kernel.AssignProcessToJobObject(job, process))

    def resume(self, thread):
        if self.kernel.ResumeThread(thread) == 0xFFFFFFFF:
            raise ProcessError("process_failed")

    def terminate_process(self, process):
        self.kernel.TerminateProcess(process, 1)
        self.kernel.WaitForSingleObject(process, 2000)

    def terminate_job(self, job):
        self.checked(self.kernel.TerminateJobObject(job, 1))

    def read_available(self, pipe):
        available = w.DWORD()
        if not self.kernel.PeekNamedPipe(pipe, None, 0, None, c.byref(available), None):
            if c.get_last_error() == ERROR_BROKEN_PIPE:
                return b""
            raise ProcessError("pipe_read_failed")
        if not available.value:
            return None
        data, read = c.create_string_buffer(min(65536, available.value)), w.DWORD()
        self.checked(self.kernel.ReadFile(pipe, data, len(data), c.byref(read), None))
        return data.raw[:read.value]

    def write(self, pipe, data):
        written = w.DWORD()
        self.checked(self.kernel.WriteFile(pipe, data, len(data), c.byref(written), None))
        return written.value

    def cancel_io(self, thread_id):
        handle = self.kernel.OpenThread(1, False, thread_id)
        if handle:
            try:
                self.kernel.CancelSynchronousIo(handle)
            finally:
                self.close(handle)


class ExecutionOwner:
    def __init__(self, db_path, api=None):
        self.path = Path(str(Path(db_path).resolve()) + ".execution.lock")
        self.api, self.handle = api, None
        self.is_owner = False

    def acquire(self):
        if self.is_owner:
            return True
        if self.api is None:
            if sys.platform != "win32":
                return False
            self.api = WindowsAPI()
        self.handle = self.api.owner(self.path)
        self.is_owner = bool(self.handle)
        return self.is_owner

    def close(self):
        if self.handle:
            self.api.close(self.handle)
            self.handle = None
        self.is_owner = False


def query_old_job(job_name, api=None):
    """Query and immediately release; never adopt or terminate an old Job."""
    if not job_name:
        return False
    handle = None
    try:
        api = api or WindowsAPI()
        handle = api.open_job(job_name)
        return not handle or api.active(handle) == 0
    except (ProcessError, OSError):
        return False
    finally:
        if handle:
            api.close(handle)


class OwnedProcess:
    def __init__(self, exe, argv, cwd, job_name, api=None):
        self.exe, self.argv, self.cwd, self.job_name = exe, argv, cwd, job_name
        self.api = api
        self.job = self.process = self.stdin = self.stdout = None
        self.pid = None
        self.reader = self.writer = None
        self.events, self.writes = queue.Queue(128), queue.Queue(1)
        self.stop = threading.Event()
        self.failure = None
        self._close_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        self.cleanup_verified = False

    def start(self):
        if self.job or self._closed:
            raise ProcessError("process_already_started")
        if self.api is None:
            self.api = WindowsAPI()
        thread, child_handles = None, ()
        try:
            self.job = self.api.create_job(self.job_name)
            self.stdin, self.stdout, child_handles = self.api.pipes()
            self.process, thread, self.pid = self.api.spawn(self.exe, self.argv, self.cwd, child_handles)
            self.api.assign(self.job, self.process)
            self.api.resume(thread)
            self.reader = threading.Thread(target=self._read_loop, name="execution-reader", daemon=True)
            self.writer = threading.Thread(target=self._write_loop, name="execution-writer", daemon=True)
            self.reader.start()
            self.writer.start()
        except BaseException:
            if self.process:
                self.api.terminate_process(self.process)
            self.close(grace=0)
            raise
        finally:
            if thread:
                self.api.close(thread)
            for handle in child_handles:
                self.api.close(handle)
        return self

    def _read_loop(self):
        pending = bytearray()
        try:
            while not self.stop.is_set():
                data = self.api.read_available(self.stdout)
                if data is None:
                    self.stop.wait(.01)
                    continue
                if not data:
                    raise ProcessError("protocol_error" if pending else "process_eof")
                pending.extend(data)
                while b"\n" in pending:
                    line, _, pending = pending.partition(b"\n")
                    if len(line) > MAX_LINE:
                        raise ProcessError("protocol_line_too_large")
                    value = json.loads(line.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if not isinstance(value, dict):
                        raise ProcessError("protocol_error")
                    try:
                        self.events.put_nowait(value)
                    except queue.Full:
                        raise ProcessError("protocol_queue_full") from None
                if len(pending) > MAX_LINE:
                    raise ProcessError("protocol_line_too_large")
        except (ProcessError, ValueError, UnicodeDecodeError, OSError) as error:
            self.failure = error if isinstance(error, ProcessError) else ProcessError("protocol_error")

    def _write_loop(self):
        try:
            while not self.stop.is_set():
                try:
                    task = self.writes.get(timeout=.05)
                except queue.Empty:
                    continue
                if task is None:
                    return
                data, done, outcome = task
                try:
                    if self.api.write(self.stdin, data) != len(data):
                        raise ProcessError("partial_write")
                except (ProcessError, OSError) as error:
                    outcome.append(error if isinstance(error, ProcessError) else ProcessError("pipe_write_failed"))
                finally:
                    done.set()
        finally:
            # The writer owns stdin after start; no Python stream locks are involved.
            if self.stdin:
                self.api.close(self.stdin)
                self.stdin = None

    def read(self, timeout=.1):
        if self.failure and str(self.failure) != "process_eof":
            raise self.failure
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            if self.failure:
                raise self.failure
            return None

    def write(self, message):
        with self._write_lock:
            if self._closed or not self.writer or not self.writer.is_alive():
                raise ProcessError("pipe_write_failed")
            data = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            done, outcome = threading.Event(), []
            self.writes.put_nowait((data, done, outcome))
            if not done.wait(5):
                raise ProcessError("pipe_write_timeout")
            if outcome:
                raise outcome[0]

    def close(self, grace=2):
        with self._close_lock:
            if self._closed:
                return self.cleanup_verified
            self._closed = True
            if self.api is None:
                self.cleanup_verified = True
                return True
            try:
                self.writes.put_nowait(None)
            except queue.Full:
                pass
            deadline = time.monotonic() + max(0, min(grace, 2))
            empty = self.job is None
            try:
                while self.job:
                    empty = self.api.active(self.job) == 0
                    if empty or time.monotonic() >= deadline:
                        break
                    time.sleep(.02)
                if self.job and not empty:
                    self.api.terminate_job(self.job)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        empty = self.api.active(self.job) == 0
                        if empty:
                            break
                        time.sleep(.02)
            except (ProcessError, OSError):
                empty = False
            self.stop.set()
            for thread in (self.writer, self.reader):
                if thread and thread.ident:
                    if thread.is_alive():
                        self.api.cancel_io(thread.native_id)
                    thread.join(timeout=1)
            joined = all(not thread or not thread.is_alive() for thread in (self.writer, self.reader))
            self.cleanup_verified = empty and joined
            # Closing the last Job handle is the final owned-tree backstop.
            for name in ("job", "process", "stdout"):
                self.api.close(getattr(self, name))
                setattr(self, name, None)
            if (not self.writer or not self.writer.ident) and self.stdin:
                self.api.close(self.stdin)
                self.stdin = None
            return self.cleanup_verified
