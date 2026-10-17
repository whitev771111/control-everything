"""Windows installation identity, bounded handles and cooperative maintenance."""

from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
from pathlib import Path
import subprocess


class InstallationError(OSError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def plain_path(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if item.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise InstallationError('linked_path')
    return path


class ProcessEntry(C.Structure):
    _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('th32ProcessID', W.DWORD),
                ('th32DefaultHeapID', C.c_size_t), ('th32ModuleID', W.DWORD), ('cntThreads', W.DWORD),
                ('th32ParentProcessID', W.DWORD), ('pcPriClassBase', W.LONG), ('dwFlags', W.DWORD),
                ('szExeFile', W.WCHAR * 260)]


class SecurityAttributes(C.Structure):
    _fields_ = [('nLength', W.DWORD), ('lpSecurityDescriptor', C.c_void_p), ('bInheritHandle', W.BOOL)]


class FileId(C.Structure):
    _fields_ = [('volume', C.c_uint64), ('identifier', C.c_ubyte * 16)]


class Luid(C.Structure):
    _fields_ = [('low', W.DWORD), ('high', W.LONG)]


class TokenStatistics(C.Structure):
    _fields_ = [('token', Luid), ('authentication', Luid), ('expiration', C.c_int64), ('type', W.DWORD),
                ('impersonation', W.DWORD), ('dynamic_charged', W.DWORD), ('dynamic_available', W.DWORD),
                ('groups', W.DWORD), ('privileges', W.DWORD), ('modified', Luid)]


class WindowsIdentity:
    """Only Win32 identity primitives; no dependency on sessions or installation."""
    def __init__(self):
        if os.name != 'nt':
            raise InstallationError('unsupported_platform')
        self.kernel, self.security = C.WinDLL('kernel32', use_last_error=True), C.WinDLL('advapi32', use_last_error=True)
        functions = {
            'CreateFileW': ([W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
            'CloseHandle': ([W.HANDLE], W.BOOL), 'WaitForSingleObject': ([W.HANDLE, W.DWORD], W.DWORD),
            'OpenProcess': ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            'GetProcessTimes': ([W.HANDLE, *([C.POINTER(W.FILETIME)] * 4)], W.BOOL),
            'ProcessIdToSessionId': ([W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
            'QueryFullProcessImageNameW': ([W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
            'GetFileInformationByHandleEx': ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
            'GetNamedPipeServerProcessId': ([W.HANDLE, C.POINTER(W.ULONG)], W.BOOL),
        }
        for name, (arguments, result) in functions.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
            setattr(self, name, function)
        for name, arguments, result in (
                ('OpenProcessToken', [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL),
                ('GetTokenInformation', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
                ('GetLengthSid', [C.c_void_p], W.DWORD)):
            function = getattr(self.security, name)
            function.argtypes, function.restype = arguments, result
            setattr(self, name, function)

    def process(self, pid):
        handle = self.OpenProcess(0x100000 | 0x1000, False, pid)
        if not handle:
            raise InstallationError('process_identity')
        return handle

    def created(self, handle):
        times = [W.FILETIME() for _ in range(4)]
        if not self.GetProcessTimes(handle, *(C.byref(value) for value in times)):
            raise InstallationError('process_identity')
        return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime

    def credentials(self, pid, handle):
        token = W.HANDLE()
        if not self.OpenProcessToken(handle, 8, C.byref(token)):
            raise InstallationError('process_owner')
        try:
            size = W.DWORD()
            self.GetTokenInformation(token, 1, None, 0, C.byref(size))
            if not 0 < size.value <= 65536:
                raise InstallationError('process_owner')
            buffer = C.create_string_buffer(size.value)
            if not self.GetTokenInformation(token, 1, buffer, size, C.byref(size)):
                raise InstallationError('process_owner')
            pointer = C.cast(buffer, C.POINTER(C.c_void_p))[0]
            length = self.GetLengthSid(pointer)
            if not 0 < length <= 1024:
                raise InstallationError('process_owner')
            sid = C.string_at(pointer, length)
            statistics = TokenStatistics()
            if not self.GetTokenInformation(token, 10, C.byref(statistics), C.sizeof(statistics), C.byref(size)):
                raise InstallationError('process_owner')
            session = W.DWORD()
            if not self.ProcessIdToSessionId(pid, C.byref(session)):
                raise InstallationError('process_owner')
            auth = statistics.authentication
            return sid, (auth.high & 0xFFFFFFFF) << 32 | auth.low, session.value
        finally:
            self.CloseHandle(token)


class ProcessLease:
    def __init__(self, api, handle, identity):
        self.api, self.handle, self.identity = api, handle, identity

    def alive(self):
        result = self.api.win.WaitForSingleObject(self.handle, 0)
        if result not in (0, 258) or self.api.win.created(self.handle) != self.identity['created']:
            raise InstallationError('process_identity')
        return result == 258

    def wait(self, timeout_ms=5000):
        result = self.api.win.WaitForSingleObject(self.handle, timeout_ms)
        if result not in (0, 258):
            raise InstallationError('process_wait')
        return result == 0

    def close(self):
        if self.handle is not None:
            self.api.win.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class InstallationAPI:
    """Constructed only for explicit registration/maintenance, never on import."""

    def __init__(self, win32=None):
        if win32 is None:
            win32 = WindowsIdentity()
        self.win = win32
        declarations = {
            'GetFinalPathNameByHandleW': ([W.HANDLE, W.LPWSTR, W.DWORD, W.DWORD], W.DWORD),
            'CreateToolhelp32Snapshot': ([W.DWORD, W.DWORD], W.HANDLE),
            'Process32FirstW': ([W.HANDLE, C.POINTER(ProcessEntry)], W.BOOL),
            'Process32NextW': ([W.HANDLE, C.POINTER(ProcessEntry)], W.BOOL),
            'CreateMutexW': ([C.POINTER(SecurityAttributes), W.BOOL, W.LPCWSTR], W.HANDLE),
            'ReleaseMutex': ([W.HANDLE], W.BOOL),
            'LocalFree': ([C.c_void_p], C.c_void_p),
        }
        for name, (args, result) in declarations.items():
            function = getattr(self.win.kernel, name)
            function.argtypes, function.restype = args, result
            setattr(self, name, function)
        for name, args in (
                ('ConvertSidToStringSidW', [C.c_void_p, C.POINTER(W.LPWSTR)]),
                ('ConvertStringSecurityDescriptorToSecurityDescriptorW', [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.POINTER(W.DWORD)])):
            function = getattr(self.win.security, name)
            function.argtypes, function.restype = args, W.BOOL
            setattr(self, name, function)

    def _open_file(self, path, access=0x80, share=7, directory=False):
        path = plain_path(path)
        handle = self.win.CreateFileW(str(path), access, share, None, 3, 0x02000000 if directory else 0, None)
        if handle in (None, C.c_void_p(-1).value):
            raise InstallationError('asset_open_failed')
        return handle

    def _file_identity(self, path, directory=False):
        handle = self._open_file(path, directory=directory)
        try:
            value = FileId()
            if not self.win.GetFileInformationByHandleEx(handle, 18, C.byref(value), C.sizeof(value)):
                raise InstallationError('file_identity')
            buffer = C.create_unicode_buffer(32768)
            length = self.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
            if not 0 < length < len(buffer):
                raise InstallationError('final_path')
            final = buffer.value
            if final.startswith('\\\\?\\UNC\\'):
                final = '\\\\' + final[8:]
            elif final.startswith('\\\\?\\'):
                final = final[4:]
            return {'path': str(plain_path(final)), 'volume': value.volume, 'file_id': bytes(value.identifier).hex()}
        finally:
            self.win.CloseHandle(handle)

    def directory_identity(self, path):
        return self._file_identity(path, True)

    def file_identity(self, path):
        return self._file_identity(path)

    def _parents(self):
        handle = self.CreateToolhelp32Snapshot(2, 0)
        if handle in (None, C.c_void_p(-1).value):
            raise InstallationError('process_snapshot')
        try:
            row = ProcessEntry()
            row.dwSize = C.sizeof(row)
            result, parents = self.Process32FirstW(handle, C.byref(row)), {}
            if not result:
                raise InstallationError('process_snapshot')
            while result:
                parents[row.th32ProcessID] = row.th32ParentProcessID
                result = self.Process32NextW(handle, C.byref(row))
            if C.get_last_error() != 18:
                raise InstallationError('process_snapshot')
            return parents
        finally:
            self.win.CloseHandle(handle)

    def process_identity(self, pid, parent_pid=None):
        handle = self.win.process(pid)
        known_image = None
        try:
            image, length = C.create_unicode_buffer(32768), W.DWORD(32768)
            if not self.win.QueryFullProcessImageNameW(handle, 0, image, C.byref(length)):
                raise InstallationError('process_image')
            known_image = Path(image.value)
            created = self.win.created(handle)
            sid, authentication, session = self.win.credentials(pid, handle)
            if parent_pid is None:
                parent_pid = self._parents().get(pid)
            if parent_pid is None:
                raise InstallationError('process_parent')
            identity = {'pid': pid, 'created': created, 'parent_pid': parent_pid, 'image': self.file_identity(image.value),
                        'sid': sid.hex(), 'authentication': authentication, 'session': session}
            result = ProcessLease(self, handle, identity)
            if not result.alive():
                raise InstallationError('process_exited')
            return result
        except BaseException as error:
            self.win.CloseHandle(handle)
            if isinstance(error, OSError) and known_image is not None:
                # Keep this same-handle observation when later identity queries fail.
                error.image_path = known_image
            raise

    def current_identity(self):
        with self.process_identity(os.getpid()) as process:
            return process.identity

    def mutex_spec(self, install_root):
        root = plain_path(install_root)
        if root.exists():
            final = self.directory_identity(root)['path']
        else:
            final = str(Path(self.directory_identity(root.parent)['path']) / root.name)
        own = self.current_identity()
        sid_bytes = bytes.fromhex(own['sid'])
        pointer, text = C.create_string_buffer(sid_bytes), W.LPWSTR()
        if not self.ConvertSidToStringSidW(pointer, C.byref(text)):
            raise InstallationError('owner_sid')
        try:
            sddl = 'D:P(A;;GA;;;SY)(A;;GA;;;' + text.value + ')'
        finally:
            self.LocalFree(C.cast(text, C.c_void_p))
        digest = hashlib.sha256((own['sid'] + '\0' + os.path.normcase(final)).encode('utf-8')).hexdigest()
        return {'name': 'Global\\SessionPanel.Install.' + digest, 'sddl': sddl}

    @contextmanager
    def install_mutex(self, install_root, timeout_ms=2000):
        spec, descriptor = self.mutex_spec(install_root), C.c_void_p()
        if not self.ConvertStringSecurityDescriptorToSecurityDescriptorW(spec['sddl'], 1, C.byref(descriptor), None):
            raise InstallationError('mutex_acl')
        handle = None
        acquired = False
        try:
            security = SecurityAttributes(C.sizeof(SecurityAttributes), descriptor, False)
            handle = self.CreateMutexW(C.byref(security), False, spec['name'])
            if not handle:
                raise InstallationError('mutex_access')
            result = self.win.WaitForSingleObject(handle, timeout_ms)
            if result == 258:
                raise InstallationError('maintenance_busy')
            # An abandoned mutex means a previous maintenance did not finish.
            if result not in (0, 128):
                raise InstallationError('mutex_wait')
            acquired = True
            yield
        finally:
            if acquired:
                self.ReleaseMutex(handle)
            if handle:
                self.win.CloseHandle(handle)
            self.LocalFree(descriptor)

    @contextmanager
    def installation_processes(self, install_root, recorded_pids=()):
        root, leases, denied = plain_path(install_root), [], []
        try:
            for pid, parent in self._parents().items():
                if not pid:
                    continue
                try:
                    process = self.process_identity(pid, parent)
                except OSError as error:
                    if pid in recorded_pids:
                        raise InstallationError('recorded_process_unreadable') from error
                    known_image = getattr(error, 'image_path', None)
                    if known_image is not None and Path(known_image).is_relative_to(root):
                        raise InstallationError('owned_process_unreadable') from error
                    denied.append(pid)
                    continue
                if Path(process.identity['image']['path']).is_relative_to(root) or pid in recorded_pids:
                    leases.append(process)
                else:
                    process.close()
            yield leases, denied
        finally:
            for process in leases:
                process.close()

    def pipe_peer(self, profile_key):
        if not isinstance(profile_key, str) or len(profile_key) != 64 or any(c not in '0123456789abcdef' for c in profile_key):
            raise InstallationError('profile_key')
        handle = self.win.CreateFileW('\\\\.\\pipe\\session-panel-observer-v1-' + profile_key,
                                      0xC0000000, 0, None, 3, 0x40000000, None)
        if handle in (None, C.c_void_p(-1).value):
            if C.get_last_error() == 2:
                return None
            raise InstallationError('pipe_unavailable')
        try:
            pid = W.ULONG()
            if not self.win.GetNamedPipeServerProcessId(handle, C.byref(pid)):
                raise InstallationError('pipe_identity')
            return self.process_identity(pid.value)
        finally:
            self.win.CloseHandle(handle)

    @contextmanager
    def readonly_native(self, path):
        handle = self._open_file(path, access=0x80000000, share=1)
        try:
            yield
        finally:
            self.win.CloseHandle(handle)

    @contextmanager
    def exclusive_asset_handles(self, paths):
        handles = []
        try:
            for path in paths:
                handles.append(self._open_file(path, access=0xC0010000, share=0))
            yield
        finally:
            for handle in reversed(handles):
                self.win.CloseHandle(handle)

    def native_command(self, binary, command, profile):
        if command not in ('status', 'stop') or not Path(binary).is_absolute() or not Path(profile).is_absolute():
            raise InstallationError('native_command')
        return subprocess.run([str(binary), command, '--data-dir', str(profile)], shell=False,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
                              creationflags=0x08000000, check=False)

    def registered_uninstaller(self, install_root):
        # Inno's exact AppId key is consumed; never search unins*.exe by basename.
        import winreg
        key_name = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{A68E7F38-54DB-4A76-9DD2-89B3554F617D}_is1'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
            command = winreg.QueryValueEx(key, 'UninstallString')[0]
        if not isinstance(command, str) or not command.startswith('"') or not command.endswith('"') or command.count('"') != 2:
            raise InstallationError('uninstaller_registration')
        path = plain_path(command[1:-1])
        if not path.is_relative_to(plain_path(install_root)):
            raise InstallationError('uninstaller_registration')
        return self.file_identity(path)
