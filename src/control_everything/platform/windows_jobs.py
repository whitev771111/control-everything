"""Typed Windows Job and process primitives; no execution-owner lifecycle."""

import ctypes as c
from ctypes import wintypes as w
import subprocess
import sys

CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 1
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_QUERY = 0x0004
ERROR_FILE_NOT_FOUND = 2
ERROR_BROKEN_PIPE = 109
INVALID_HANDLE_VALUE = c.c_void_p(-1).value


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


def environment_block(environment):
    if type(environment) is not dict:
        raise ProcessError("invalid_environment")
    keys = {}
    for key, value in environment.items():
        if (not isinstance(key, str) or not key or "=" in key or "\0" in key
                or not isinstance(value, str) or "\0" in value):
            raise ProcessError("invalid_environment")
        keys[key.casefold()] = (key, value)
    block = "".join(f"{key}={value}\0" for key, value in
                    (keys[name] for name in sorted(keys)))
    return c.create_unicode_buffer(block + "\0")


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
            "GetExitCodeProcess": ([w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
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

    def spawn(self, exe, argv, cwd, child_handles, *, env=None, command_line=None):
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
            if command_line is None:
                command = c.create_unicode_buffer(subprocess.list2cmdline([str(exe), *argv]))
            elif isinstance(command_line, str) and command_line and "\0" not in command_line:
                command = c.create_unicode_buffer(command_line)
            else:
                raise ProcessError("invalid_command_line")
            environment = environment_block(env) if env is not None else None
            flags = CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT
            if environment is not None:
                flags |= CREATE_UNICODE_ENVIRONMENT
            self.checked(self.kernel.CreateProcessW(str(exe), command, None, None, True, flags,
                                                    environment, str(cwd), c.byref(startup),
                                                    c.byref(process)))
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

    def exit_code(self, process):
        code = w.DWORD()
        self.checked(self.kernel.GetExitCodeProcess(process, c.byref(code)))
        return code.value

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
