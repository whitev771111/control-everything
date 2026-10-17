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

from control_everything.platform.windows_jobs import (
    CREATE_SUSPENDED, CREATE_NO_WINDOW, EXTENDED_STARTUPINFO_PRESENT, STARTF_USESTDHANDLES,
    HANDLE_FLAG_INHERIT, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_OBJECT_QUERY,
    ERROR_FILE_NOT_FOUND, ERROR_BROKEN_PIPE, INVALID_HANDLE_VALUE, ProcessError,
    SecurityAttributes, StartupInfo, StartupInfoEx, ProcessInformation,
    BasicLimits, IOCounters, ExtendedLimits, JobAccounting,
    WindowsAPI,
)

MAX_LINE = 1024 * 1024


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
