"""One process-wide Windows overlapped I/O owner with bounded retirement."""

import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import threading
import time

from session_panel.paths import observer_install_dir
from session_panel.sessions import observer_protocol as protocol


class PipeError(OSError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class CleanupPending(PipeError):
    def __init__(self):
        super().__init__("io_cleanup_pending")


class Overlapped(C.Structure):
    _fields_ = [("Internal", C.c_size_t), ("InternalHigh", C.c_size_t), ("Offset", W.DWORD),
                ("OffsetHigh", W.DWORD), ("hEvent", W.HANDLE)]


class FileId(C.Structure):
    _fields_ = [("volume", C.c_uint64), ("identifier", C.c_ubyte * 16)]


class Luid(C.Structure):
    _fields_ = [("low", W.DWORD), ("high", W.LONG)]


class TokenStatistics(C.Structure):
    _fields_ = [("token", Luid), ("authentication", Luid), ("expiration", C.c_int64),
                ("type", W.DWORD), ("impersonation", W.DWORD), ("dynamic_charged", W.DWORD),
                ("dynamic_available", W.DWORD), ("groups", W.DWORD), ("privileges", W.DWORD), ("modified", Luid)]


class Win32:
    """Loaded only on explicit start. All handles have one explicit owner."""
    def __init__(self):
        if os.name != "nt":
            raise PipeError("unsupported_platform")
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.security = C.WinDLL("advapi32", use_last_error=True)
        declarations = {
            "CreateFileW": ([W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
            "CloseHandle": ([W.HANDLE], W.BOOL),
            "CreateEventW": ([C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
            "ResetEvent": ([W.HANDLE], W.BOOL),
            "VirtualAlloc": ([C.c_void_p, C.c_size_t, W.DWORD, W.DWORD], C.c_void_p),
            "VirtualFree": ([C.c_void_p, C.c_size_t, W.DWORD], W.BOOL),
            "ReadFile": ([W.HANDLE, C.c_void_p, W.DWORD, C.c_void_p, C.POINTER(Overlapped)], W.BOOL),
            "WriteFile": ([W.HANDLE, C.c_void_p, W.DWORD, C.c_void_p, C.POINTER(Overlapped)], W.BOOL),
            "GetOverlappedResult": ([W.HANDLE, C.POINTER(Overlapped), C.POINTER(W.DWORD), W.BOOL], W.BOOL),
            "CancelIoEx": ([W.HANDLE, C.POINTER(Overlapped)], W.BOOL),
            "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD),
            "WaitNamedPipeW": ([W.LPCWSTR, W.DWORD], W.BOOL),
            "GetNamedPipeServerProcessId": ([W.HANDLE, C.POINTER(W.ULONG)], W.BOOL),
            "OpenProcess": ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            "GetProcessTimes": ([W.HANDLE, C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME)], W.BOOL),
            "ProcessIdToSessionId": ([W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
            "QueryFullProcessImageNameW": ([W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
            "GetFileInformationByHandleEx": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
            setattr(self, name, function)
        for name, arguments, result in (
                ("OpenProcessToken", [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL),
                ("GetTokenInformation", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
                ("GetLengthSid", [C.c_void_p], W.DWORD)):
            function = getattr(self.security, name)
            function.argtypes, function.restype = arguments, result
            setattr(self, name, function)

    def file_identity(self, path, directory=False):
        handle = self.CreateFileW(str(path), 0x80, 7, None, 3, 0x02000000 if directory else 0, None)
        if handle in (None, C.c_void_p(-1).value):
            raise PipeError("identity_mismatch")
        try:
            identity = FileId()
            if not self.GetFileInformationByHandleEx(handle, 18, C.byref(identity), C.sizeof(identity)):
                raise PipeError("identity_mismatch")
            return identity.volume, bytes(identity.identifier)
        finally:
            self.CloseHandle(handle)

    def process(self, pid):
        handle = self.OpenProcess(0x100000 | 0x1000, False, pid)
        if not handle:
            raise PipeError("identity_mismatch")
        return handle

    def credentials(self, pid, handle):
        token = W.HANDLE()
        if not self.OpenProcessToken(handle, 8, C.byref(token)):
            raise PipeError("identity_mismatch")
        try:
            size = W.DWORD()
            self.GetTokenInformation(token, 1, None, 0, C.byref(size))
            if not 0 < size.value <= 65536:
                raise PipeError("identity_mismatch")
            buffer = C.create_string_buffer(size.value)
            if not self.GetTokenInformation(token, 1, buffer, size, C.byref(size)):
                raise PipeError("identity_mismatch")
            pointer = C.cast(buffer, C.POINTER(C.c_void_p))[0]
            length = self.GetLengthSid(pointer)
            if not 0 < length <= 1024:
                raise PipeError("identity_mismatch")
            sid = C.string_at(pointer, length)
            statistics = TokenStatistics()
            if not self.GetTokenInformation(token, 10, C.byref(statistics), C.sizeof(statistics), C.byref(size)):
                raise PipeError("identity_mismatch")
            session = W.DWORD()
            if not self.ProcessIdToSessionId(pid, C.byref(session)):
                raise PipeError("identity_mismatch")
            auth = statistics.authentication
            return sid, (auth.high & 0xFFFFFFFF) << 32 | auth.low, session.value
        finally:
            self.CloseHandle(token)

    def created(self, process):
        times = [W.FILETIME() for _ in range(4)]
        if not self.GetProcessTimes(process, *(C.byref(value) for value in times)):
            raise PipeError("identity_mismatch")
        return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime


class Arena:
    SIZE = 1024 * 1024

    def __init__(self, api):
        self.api = api
        self.address = api.VirtualAlloc(None, self.SIZE, 0x3000, 4)
        if not self.address:
            raise PipeError("io_unavailable")
        self.event = api.CreateEventW(None, True, False, None)
        if not self.event:
            api.VirtualFree(self.address, 0, 0x8000)
            raise PipeError("io_unavailable")
        self.overlapped = Overlapped.from_address(self.address)
        self.buffer = self.address + 64
        self.pipe = None
        self.operation_id = 0
        self.freed = False

    def completion(self):
        transferred = W.DWORD()
        if self.api.GetOverlappedResult(self.pipe, C.byref(self.overlapped), C.byref(transferred), False):
            return "success", transferred.value
        error = C.get_last_error()
        # These are documented terminal completion results, never invalid-handle guesses.
        if error in (109, 232, 233, 995, 38):
            return "terminal", error
        return "pending", error

    def dispose(self):
        if self.freed:
            raise RuntimeError("arena_double_free")
        self.freed = True
        if self.pipe:
            self.api.CloseHandle(self.pipe)
            self.pipe = None
        self.api.CloseHandle(self.event)
        self.api.VirtualFree(self.address, 0, 0x8000)


class IoGate:
    """The module's GATE is the only production gate; injectable instances serve unit tests."""
    def __init__(self, arena_factory=None):
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.worker_done = threading.Event()
        self.worker_done.set()
        self.clients = []
        self.worker = self.arena = self.retired = self.owner = None
        self.state = "idle"
        self.arena_factory = arena_factory

    def start(self, client):
        self.reap_retired()
        with self.lock:
            if self.retired is not None:
                return "io_cleanup_pending"
            if client in self.clients:
                return None
            if len(self.clients) >= 16:
                return "client_limit"
            if self.worker is not None and self.worker_done.is_set():
                return "busy"
            self.clients.append(client)
            if self.worker is None:
                self.worker_done.clear()
                self.worker = threading.Thread(target=self._worker, name="native-observer-io", daemon=True)
                self.worker.start()
            self.wake.set()
        return None

    def close(self, client):
        client.closed.set()
        self.wake.set()
        with self.lock:
            active = self.owner is client
        if active:
            client.detached.wait(1.5)
        self.reap_retired()

    def reap_retired(self):
        with self.lock:
            record = self.retired
            # The worker first detaches every transport from the published record.
            # Reaping during that handoff would leave a stale, already-closed pipe.
            if self.worker is not None or not self.worker_done.is_set() or record is None or record.completion()[0] == "pending":
                return False
            self.retired = None
            record.dispose()
            self.state = "idle"
            return True

    def operation(self, client, pipe, data=None, size=None, deadline=None):
        with self.lock:
            if self.state != "idle" or client.closed.is_set() or time.monotonic() >= deadline:
                raise PipeError("pipe_closed")
            if self.arena is None:
                self.arena = (self.arena_factory or (lambda: Arena(Win32())))()
            record = self.arena
            self.state, self.owner = "active", client
            record.pipe = pipe
            record.operation_id += 1
        api = record.api
        issued = False
        outcome = ("pending", 0)
        try:
            C.memset(record.address, 0, 64)
            record.overlapped.hEvent = record.event
            api.ResetEvent(record.event)
            if data is not None:
                size = len(data)
                C.memmove(record.buffer, data, size)
            function = api.WriteFile if data is not None else api.ReadFile
            issued = True
            immediate = function(pipe, record.buffer, size, None, C.byref(record.overlapped))
            error = C.get_last_error()
            if not immediate and error != 997:
                # No operation was queued when issue itself returned a terminal error.
                if error in (109, 232, 233, 995, 38):
                    outcome = ("terminal", error)
                else:
                    outcome = ("pending", error)
            while outcome[0] == "pending" and (immediate or error == 997):
                outcome = record.completion()
                if outcome[0] != "pending" or client.closed.is_set() or time.monotonic() >= deadline:
                    break
                api.WaitForSingleObject(record.event, 20)
            if outcome[0] == "pending":
                api.CancelIoEx(pipe, C.byref(record.overlapped))
                cancellation_deadline = time.monotonic() + 1
                while time.monotonic() < cancellation_deadline:
                    outcome = record.completion()
                    if outcome[0] != "pending":
                        break
                    api.WaitForSingleObject(record.event, 20)
        except BaseException:
            if issued:
                try:
                    api.CancelIoEx(pipe, C.byref(record.overlapped))
                    until = time.monotonic() + 1
                    while time.monotonic() < until:
                        outcome = record.completion()
                        if outcome[0] != "pending":
                            break
                        api.WaitForSingleObject(record.event, 20)
                except BaseException:
                    outcome = ("pending", 0)
            else:
                outcome = ("terminal", 0)
            raise
        finally:
            with self.lock:
                if outcome[0] == "pending":
                    self.retired, self.arena = record, None
                    self.state, self.owner = "retired", None
                else:
                    record.pipe = None
                    self.state, self.owner = "idle", None
        if outcome[0] == "pending":
            raise CleanupPending()
        if outcome[0] != "success" or client.closed.is_set():
            raise PipeError("pipe_closed" if client.closed.is_set() else "pipe_timeout")
        count = outcome[1]
        if not 0 < count <= size:
            raise PipeError("pipe_closed")
        return count if data is not None else C.string_at(record.buffer, count)

    def _worker(self):
        current_worker = threading.current_thread()
        try:
            while True:
                with self.lock:
                    clients = list(self.clients)
                    if not clients:
                        # Admission and the final exit decision share the same lock.
                        # A new client either reuses this worker or starts its successor.
                        self._finish_worker_locked()
                        return
                for client in clients:
                    if client.closed.is_set():
                        client._detach()
                        with self.lock:
                            if client in self.clients:
                                self.clients.remove(client)
                        continue
                    try:
                        client._step(self)
                    except Exception:
                        client._failed("io_cleanup_pending" if self.state == "retired" else "observer_unavailable")
                    if self.state == "retired":
                        with self.lock:
                            stopped, self.clients = self.clients, []
                        for item in stopped:
                            item._failed("io_cleanup_pending")
                            item._detach(retired=self.retired.pipe)
                        return
                self.wake.wait(0.02)
                self.wake.clear()
        finally:
            with self.lock:
                if self.worker is current_worker:
                    self._finish_worker_locked()

    def _finish_worker_locked(self):
        if self.arena is not None and self.state == "idle":
            self.arena.dispose()
            self.arena = None
        self.worker = None
        self.worker_done.set()


GATE = IoGate()


class WindowsPipe:
    def __init__(self, client, gate):
        self.client, self.gate = client, gate
        self.api = Win32()
        self.pipe = self.peer = None
        self.created_at = None
        self.key = self.build = None

    def connect(self):
        try:
            install = observer_install_dir()
            if install is None:
                raise PipeError("native_missing")
            binary = install / "session-panel-observer.exe"
            if not binary.is_file():
                raise PipeError("native_missing")
            if (install / "manifest.json").stat().st_size > 4096:
                raise PipeError("protocol_mismatch")
            manifest = protocol.decode((install / "manifest.json").read_bytes(), 4096)
            protocol.exact(manifest, {"protocol", "build", "toolchain", "binary", "sha256"})
            protocol.require(manifest["protocol"] == protocol.VERSION and manifest["binary"] == binary.name
                             and manifest["toolchain"] == "1.97.1-x86_64-pc-windows-msvc")
            protocol.require(type(manifest["sha256"]) is str and hashlib.sha256(binary.read_bytes()).hexdigest() == manifest["sha256"])
            self.build = manifest["build"]
            protocol.token(self.build)
            volume, identifier = self.api.file_identity(self.client.directory, True)
            own = self.api.process(os.getpid())
            try:
                credentials = self.api.credentials(os.getpid(), own)
            finally:
                self.api.CloseHandle(own)
            self.key = protocol.profile_key(credentials[0], volume, identifier)
            name = "\\\\.\\pipe\\session-panel-observer-v1-" + self.key
            deadline = time.monotonic() + 1
            spawned = False
            while not self.client.closed.is_set():
                handle = self.api.CreateFileW(name, 0xC0000000, 0, None, 3, 0x40000000, None)
                if handle not in (None, C.c_void_p(-1).value):
                    self.pipe = handle
                    break
                error = C.get_last_error()
                if error == 2 and not spawned:
                    # Start only our fixed, hash-checked artifact; never search PATH or build here.
                    subprocess.Popen([str(binary), "daemon", "--data-dir", str(self.client.directory)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=0x08000000 | 0x00000200, close_fds=True)
                    spawned, deadline = True, time.monotonic() + 3
                elif error not in (2, 231):
                    raise PipeError("pipe_unavailable")
                if time.monotonic() >= deadline:
                    raise PipeError("pipe_timeout")
                self.api.WaitNamedPipeW(name, 25)
                self.client.closed.wait(0.025)
            if self.pipe is None:
                raise PipeError("pipe_closed")
            pid = W.ULONG()
            if not self.api.GetNamedPipeServerProcessId(self.pipe, C.byref(pid)):
                raise PipeError("identity_mismatch")
            self.peer = self.api.process(pid.value)
            self.created_at = self.api.created(self.peer)
            image, length = C.create_unicode_buffer(32768), W.DWORD(32768)
            if (self.api.credentials(pid.value, self.peer) != credentials
                    or not self.api.QueryFullProcessImageNameW(self.peer, 0, image, C.byref(length))
                    or self.api.file_identity(image.value) != self.api.file_identity(binary)
                    or hashlib.sha256(Path(image.value).read_bytes()).hexdigest() != manifest["sha256"]
                    or self.api.created(self.peer) != self.created_at
                    or self.api.WaitForSingleObject(self.peer, 0) != 258):
                raise PipeError("identity_mismatch")
        except (OSError, ValueError) as error:
            self.close()
            raise error if isinstance(error, PipeError) else PipeError("protocol_mismatch") from None

    def rpc(self, request, timeout=2):
        if self.peer is None:
            raise PipeError("pipe_closed")
        peer_state = self.api.WaitForSingleObject(self.peer, 0)
        if peer_state == 0:
            # A once-verified process exiting is transport loss, not an impostor.
            raise PipeError("pipe_closed")
        if peer_state != 258 or self.api.created(self.peer) != self.created_at:
            raise PipeError("identity_mismatch")
        data = protocol.encode(request)
        outgoing = struct.pack("<I", len(data)) + data
        rpc_deadline = time.monotonic() + timeout
        write_deadline = min(rpc_deadline, time.monotonic() + 1)
        offset = 0
        while offset < len(outgoing):
            offset += self.gate.operation(self.client, self.pipe, outgoing[offset:], deadline=write_deadline)
        deadline = min(rpc_deadline, time.monotonic() + 1)
        def read(size):
            result = bytearray()
            while len(result) < size:
                result.extend(self.gate.operation(self.client, self.pipe, size=size - len(result), deadline=deadline))
            return result
        size = struct.unpack("<I", read(4))[0]
        if not 0 < size <= protocol.RESPONSE_MAX:
            raise PipeError("frame_size")
        if request["method"] == "list_window_choices" and size > 65536:
            raise PipeError("frame_size")
        incoming = read(size)
        try:
            return protocol.decode(incoming)
        finally:
            incoming[:] = b"\0" * len(incoming)
            if self.gate.arena is not None:
                C.memset(self.gate.arena.buffer, 0, size)

    def close(self, retired=None):
        if self.pipe and self.pipe != retired:
            self.api.CloseHandle(self.pipe)
        if self.peer:
            self.api.CloseHandle(self.peer)
        self.pipe = self.peer = None
