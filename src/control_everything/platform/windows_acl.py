"""Typed Windows directory security and cross-process execution leases."""
from contextlib import contextmanager, ExitStack
import ctypes as c
from ctypes import wintypes as w
import hashlib
import os
from pathlib import Path
import platform

PREPARATION_ACCESS = 0x000E0000  # READ_CONTROL | WRITE_DAC | WRITE_OWNER
SECURITY_PARTS = 0x17  # Owner, group, DACL and mandatory label, without audit privileges.


class Trustee(c.Structure):
    _fields_ = [('multiple', w.LPVOID), ('operation', c.c_int), ('form', c.c_int), ('kind', c.c_int), ('name', w.LPVOID)]


class ExplicitAccess(c.Structure):
    _fields_ = [('permissions', w.DWORD), ('mode', c.c_int), ('inheritance', w.DWORD), ('trustee', Trustee)]


class Overlapped(c.Structure):
    _fields_ = [('internal', c.c_size_t), ('internal_high', c.c_size_t), ('offset', w.DWORD), ('offset_high', w.DWORD), ('event', w.HANDLE)]


class ACLAPI:
    def __init__(self):
        if os.name != 'nt':
            raise OSError('platform_unsupported')
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.security = c.WinDLL('advapi32', use_last_error=True)
        pointer = c.POINTER(w.LPVOID)
        signatures = {
            'GetNamedSecurityInfoW': ([w.LPCWSTR, c.c_int, w.DWORD, pointer, pointer, pointer, pointer, pointer], w.DWORD),
            'SetNamedSecurityInfoW': ([w.LPWSTR, c.c_int, w.DWORD, w.LPVOID, w.LPVOID, w.LPVOID, w.LPVOID], w.DWORD),
            'ConvertSecurityDescriptorToStringSecurityDescriptorW': ([w.LPVOID, w.DWORD, w.DWORD, pointer, c.POINTER(w.DWORD)], w.BOOL),
            'ConvertStringSecurityDescriptorToSecurityDescriptorW': ([w.LPCWSTR, w.DWORD, pointer, c.POINTER(w.DWORD)], w.BOOL),
            'ConvertSidToStringSidW': ([w.LPVOID, pointer], w.BOOL),
            'ConvertStringSidToSidW': ([w.LPCWSTR, pointer], w.BOOL),
            'OpenProcessToken': ([w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)], w.BOOL),
            'GetTokenInformation': ([w.HANDLE, c.c_int, w.LPVOID, w.DWORD, c.POINTER(w.DWORD)], w.BOOL),
            'GetSecurityDescriptorDacl': ([w.LPVOID, c.POINTER(w.BOOL), pointer, c.POINTER(w.BOOL)], w.BOOL),
            'GetSecurityDescriptorControl': ([w.LPVOID, c.POINTER(w.WORD), c.POINTER(w.DWORD)], w.BOOL),
            'SetEntriesInAclW': ([w.ULONG, c.POINTER(ExplicitAccess), w.LPVOID, pointer], w.DWORD),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.security, name)
            function.argtypes, function.restype = args, result
        for name, args, result in (
            ('LocalFree', [w.LPVOID], w.LPVOID), ('CloseHandle', [w.HANDLE], w.BOOL), ('GetCurrentProcess', [], w.HANDLE),
            ('CreateFileW', [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            ('GetVolumePathNameW', [w.LPCWSTR, w.LPWSTR, w.DWORD], w.BOOL),
            ('GetVolumeInformationW', [w.LPCWSTR, w.LPWSTR, w.DWORD, w.LPVOID, w.LPVOID, w.LPVOID, w.LPWSTR, w.DWORD], w.BOOL),
            ('GetDriveTypeW', [w.LPCWSTR], w.UINT),
            ('LockFileEx', [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, c.POINTER(Overlapped)], w.BOOL),
            ('UnlockFileEx', [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, c.POINTER(Overlapped)], w.BOOL),
        ):
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result


def _require(value, reason):
    if not value:
        raise OSError(c.get_last_error(), reason)


@contextmanager
def _descriptor(path):
    api = ACLAPI()
    descriptor, owner, dacl = w.LPVOID(), w.LPVOID(), w.LPVOID()
    code = api.security.GetNamedSecurityInfoW(str(path), 1, SECURITY_PARTS, c.byref(owner), None, c.byref(dacl), None, c.byref(descriptor))
    if code:
        raise OSError(code, 'acl_read_failed')
    try:
        yield api, descriptor, owner, dacl
    finally:
        api.kernel.LocalFree(descriptor)


def _sid_text(api, sid):
    text = w.LPVOID()
    _require(api.security.ConvertSidToStringSidW(sid, c.byref(text)), 'sid_unavailable')
    try:
        return c.wstring_at(text)
    finally:
        api.kernel.LocalFree(text)


def current_user_sid():
    api, token = ACLAPI(), w.HANDLE()
    _require(api.security.OpenProcessToken(api.kernel.GetCurrentProcess(), 8, c.byref(token)), 'current_user_unavailable')
    try:
        needed = w.DWORD()
        api.security.GetTokenInformation(token, 1, None, 0, c.byref(needed))
        buffer = c.create_string_buffer(needed.value)
        _require(api.security.GetTokenInformation(token, 1, buffer, len(buffer), c.byref(needed)), 'current_user_unavailable')
        return _sid_text(api, c.cast(buffer, c.POINTER(w.LPVOID))[0])
    finally:
        api.kernel.CloseHandle(token)


def sddl(path):
    with _descriptor(path) as (api, descriptor, _owner, _dacl):
        value = w.LPVOID()
        _require(api.security.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, SECURITY_PARTS, c.byref(value), None), 'acl_read_failed')
        try:
            return c.wstring_at(value)
        finally:
            api.kernel.LocalFree(value)


def owner_sid(path):
    with _descriptor(path) as (api, _value, owner, _dacl):
        return _sid_text(api, owner)


def has_preparation_access(path, sid):
    """Check specific preparation rights with the effective Windows token, not ACL text."""
    if sid != current_user_sid():
        return False
    api = ACLAPI()
    handle = api.kernel.CreateFileW(str(path), PREPARATION_ACCESS, 7, None, 3, 0x02200000, None)
    if handle in (None, c.c_void_p(-1).value):
        if c.get_last_error() == 5:
            return False
        raise OSError(c.get_last_error(), 'acl_access_unavailable')
    api.kernel.CloseHandle(handle)
    return True


def grant_current_user(path, sid):
    """Grant only root preparation rights; never propagate user grants to children."""
    if sid != current_user_sid():
        raise OSError('user_changed')
    with _descriptor(path) as (api, _value, _owner, dacl):
        trustee, merged = w.LPVOID(), w.LPVOID()
        _require(api.security.ConvertStringSidToSidW(sid, c.byref(trustee)), 'sid_unavailable')
        try:
            entry = ExplicitAccess(PREPARATION_ACCESS, 1, 0, Trustee(None, 0, 0, 1, trustee))
            code = api.security.SetEntriesInAclW(1, c.byref(entry), dacl, c.byref(merged))
            if code:
                raise OSError(code, 'acl_grant_failed')
            code = api.security.SetNamedSecurityInfoW(str(path), 1, 4, None, None, merged, None)
            if code:
                raise OSError(code, 'acl_grant_failed')
        finally:
            if merged:
                api.kernel.LocalFree(merged)
            api.kernel.LocalFree(trustee)
    return has_preparation_access(path, sid)


def restore_dacl(path, before):
    """Restore a root DACL; caller must first verify its exact identity and current state."""
    api, descriptor = ACLAPI(), w.LPVOID()
    _require(api.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(before, 1, c.byref(descriptor), None), 'invalid_acl_backup')
    try:
        present, defaulted, dacl = w.BOOL(), w.BOOL(), w.LPVOID()
        control, revision = w.WORD(), w.DWORD()
        _require(api.security.GetSecurityDescriptorDacl(descriptor, c.byref(present), c.byref(dacl), c.byref(defaulted)), 'invalid_acl_backup')
        _require(api.security.GetSecurityDescriptorControl(descriptor, c.byref(control), c.byref(revision)), 'invalid_acl_backup')
        if not present or not dacl:
            raise OSError('invalid_acl_backup')
        flags = 4 | (0x80000000 if control.value & 0x1000 else 0x20000000)
        code = api.security.SetNamedSecurityInfoW(str(path), 1, flags, None, None, dacl, None)
        if code:
            raise OSError(code, 'acl_restore_failed')
    finally:
        api.kernel.LocalFree(descriptor)
    return sddl(path) == before


def local_ntfs(path):
    if os.name != 'nt' or str(path).startswith('\\\\'):
        return False
    api, root, fs = ACLAPI(), c.create_unicode_buffer(32768), c.create_unicode_buffer(32)
    if not api.kernel.GetVolumePathNameW(str(path), root, len(root)) or api.kernel.GetDriveTypeW(root) != 3:
        return False
    return bool(api.kernel.GetVolumeInformationW(root, None, 0, None, None, None, fs, len(fs))) and fs.value.casefold() == 'ntfs'


def file_identity(path):
    info = os.stat(path)
    return {'st_dev': info.st_dev, 'st_ino': info.st_ino, 'machine': hashlib.sha256(platform.node().encode('utf-8')).hexdigest()}


@contextmanager
def _file_lock(key, exclusive):
    root = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'control-everything' / 'sandbox-locks'
    root.mkdir(parents=True, exist_ok=True)
    with open(root / (hashlib.sha256(key.encode('utf-8')).hexdigest() + '.lock'), 'a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            api, overlap = ACLAPI(), Overlapped()
            native = msvcrt.get_osfhandle(handle.fileno())
            if not api.kernel.LockFileEx(native, 1 | (2 if exclusive else 0), 0, 1, 0, c.byref(overlap)):
                raise OSError('sandbox_busy')
            try:
                yield
            finally:
                api.kernel.UnlockFileEx(native, 0, 1, 0, c.byref(overlap))
        else:
            import fcntl
            try:
                fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OSError('sandbox_busy') from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def directory_lock(path):
    """Nonblocking lease: exclusive target, shared ancestors, and identity alias keys."""
    target, keys = Path(path).resolve(), {}
    for index, directory in enumerate((target, *target.parents)):
        info = os.stat(directory)
        for key in (os.path.normcase(str(directory)), f'identity:{info.st_dev}:{info.st_ino}'):
            keys[key] = keys.get(key, False) or index == 0
    with ExitStack() as stack:
        for key, exclusive in sorted(keys.items()):
            stack.enter_context(_file_lock(key, exclusive))
        yield


def state_lock(data_dir):
    """Serialize read-modify-write of one profile cache across processes."""
    return _file_lock('sandbox-state:' + os.path.normcase(str(Path(data_dir).resolve())), True)
