"""One bounded, read-only census of four Windows process names."""

import ctypes as C
import json
import ntpath
import sys
import time
from ctypes import wintypes as W
from datetime import datetime, timedelta, timezone

NAMES = {"zellij.exe", "wezterm.exe", "wezterm-gui.exe", "wezterm-mux-server.exe"}


class ProcessEntry(C.Structure):
    _fields_ = [("size", W.DWORD), ("usage", W.DWORD), ("pid", W.DWORD), ("heap", C.c_size_t),
                ("module", W.DWORD), ("threads", W.DWORD), ("parent", W.DWORD),
                ("priority", W.LONG), ("flags", W.DWORD), ("name", W.WCHAR * 260)]


class BasicInformation(C.Structure):
    _fields_ = [("status", W.LONG), ("peb", C.c_void_p), ("affinity", C.c_size_t),
                ("priority", W.LONG), ("pid", C.c_size_t), ("parent", C.c_size_t)]


class UnicodeString(C.Structure):
    _fields_ = [("length", W.USHORT), ("maximum", W.USHORT), ("buffer", C.c_void_p)]


def require(ok, kind):
    if not ok:
        raise ValueError(kind)


def check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError("deadline")


def load_api():
    require(sys.platform == "win32" and C.sizeof(C.c_void_p) == 8, "unsupported")
    try:
        kernel, native = C.WinDLL("kernel32", use_last_error=True), C.WinDLL("ntdll")
        for name, args, result in (
            ("CreateToolhelp32Snapshot", [W.DWORD, W.DWORD], W.HANDLE),
            ("Process32FirstW", [W.HANDLE, C.POINTER(ProcessEntry)], W.BOOL),
            ("Process32NextW", [W.HANDLE, C.POINTER(ProcessEntry)], W.BOOL),
            ("OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            ("CloseHandle", [W.HANDLE], W.BOOL),
            ("QueryFullProcessImageNameW", [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
            ("GetProcessTimes", [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL),
            ("GetProcessId", [W.HANDLE], W.DWORD),
            ("WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
        ):
            method = getattr(kernel, name); method.argtypes = args; method.restype = result
        native.NtQueryInformationProcess.argtypes = [W.HANDLE, W.ULONG, C.c_void_p, W.ULONG, C.POINTER(W.ULONG)]
        native.NtQueryInformationProcess.restype = W.LONG
        return kernel, native
    except (OSError, AttributeError):
        raise ValueError("unsupported") from None


def close_handle(kernel, handle, error):
    try:
        require(kernel.CloseHandle(handle), "close_failed")
    except Exception:
        if error is None:
            raise ValueError("close_failed") from None
        error.add_note("close_failed")


def command_line(native, handle):
    returned = W.ULONG()
    status = native.NtQueryInformationProcess(handle, 60, None, 0, C.byref(returned))
    require(status & 0xffffffff == 0xC0000004 and C.sizeof(UnicodeString) < returned.value <= 131072, "invalid_buffer")
    buffer = C.create_string_buffer(returned.value)
    status = native.NtQueryInformationProcess(handle, 60, buffer, len(buffer), C.byref(returned))
    require(status & 0x80000000 == 0, "api_failed")
    require(C.sizeof(UnicodeString) <= returned.value <= len(buffer), "invalid_buffer")
    value = UnicodeString.from_buffer(buffer); base = C.addressof(buffer)
    require(value.length > 0 and value.length % 2 == 0 and value.maximum % 2 == 0
            and value.length <= value.maximum and value.buffer and value.buffer % 2 == 0
            and base + C.sizeof(UnicodeString) <= value.buffer
            and value.buffer + value.maximum <= base + returned.value
            and value.buffer + value.length <= base + returned.value, "invalid_buffer")
    try:
        text = C.string_at(value.buffer, value.length).decode("utf-16-le", errors="strict")
    except UnicodeError:
        raise ValueError("invalid_buffer") from None
    require("\x00" not in text, "invalid_buffer")
    return text


def query_row(kernel, native, entry, deadline, snapshot_ticks):
    check_deadline(deadline)
    require(entry.pid != 0, "invalid_identity")
    handle = kernel.OpenProcess(0x101000, False, entry.pid)
    require(handle, "api_failed")
    error = None
    try:
        require(kernel.WaitForSingleObject(handle, 0) == 258, "invalid_identity")
        require(kernel.GetProcessId(handle) == entry.pid, "invalid_identity")
        basic, returned = BasicInformation(), W.ULONG()
        status = native.NtQueryInformationProcess(handle, 0, C.byref(basic), C.sizeof(basic), C.byref(returned))
        require(status & 0x80000000 == 0, "api_failed")
        require(returned.value == C.sizeof(basic) and basic.pid == entry.pid and basic.parent == entry.parent, "invalid_identity")
        image = C.create_unicode_buffer(32768); size = W.DWORD(len(image))
        require(kernel.QueryFullProcessImageNameW(handle, 0, image, C.byref(size)), "api_failed")
        require(0 < size.value < len(image) and len(image.value) == size.value
                and ntpath.isabs(image.value) and ntpath.splitdrive(image.value)[0]
                and ntpath.basename(image.value).casefold() == entry.name.casefold(), "invalid_identity")
        times = [W.FILETIME() for _ in range(4)]
        require(kernel.GetProcessTimes(handle, *(C.byref(x) for x in times)), "api_failed")
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        require(0 < ticks <= snapshot_ticks, "invalid_identity")
        text = command_line(native, handle)
        require(kernel.WaitForSingleObject(handle, 0) == 258, "invalid_identity")
        require(kernel.GetProcessId(handle) == entry.pid, "invalid_identity")
        check_deadline(deadline)
        moment = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
        created = moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{ticks % 10000000:07d}Z"
        return {"ProcessId": entry.pid, "ParentProcessId": entry.parent, "Name": entry.name,
                "ExecutablePath": image.value, "CommandLine": text, "CreatedUtc": created}
    except BaseException as caught:
        error = caught
        raise
    finally:
        close_handle(kernel, handle, error)


def census(deadline):
    check_deadline(deadline)
    kernel, native = load_api()
    snapshot_ticks = time.time_ns() // 100 + 116444736000000000
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    require(snapshot not in (None, 0, C.c_void_p(-1).value), "api_failed")
    error = None
    try:
        entry = ProcessEntry(); entry.size = C.sizeof(entry); rows = []
        ok = kernel.Process32FirstW(snapshot, C.byref(entry))
        while ok:
            check_deadline(deadline)
            if entry.name.casefold() in NAMES:
                rows.append(query_row(kernel, native, entry, deadline, snapshot_ticks))
            ok = kernel.Process32NextW(snapshot, C.byref(entry))
        require(C.get_last_error() == 18, "api_failed")
        check_deadline(deadline)
        return rows
    except BaseException as caught:
        error = caught
        raise
    finally:
        close_handle(kernel, snapshot, error)


def main():
    try:
        require(len(sys.argv) == 1, "unsupported")
        deadline = time.monotonic() + 5
        for attempt in range(3):
            try:
                rows = census(deadline)
                break
            except ValueError as error:
                if (str(error) not in {"api_failed", "invalid_identity"}
                        or "close_failed" in getattr(error, "__notes__", [])
                        or attempt == 2):
                    raise
        print(json.dumps(rows, ensure_ascii=True))
        return 0
    except Exception as error:
        kinds = {"unsupported", "api_failed", "invalid_identity", "invalid_buffer", "deadline", "close_failed"}
        kind = "deadline" if isinstance(error, TimeoutError) else str(error) if str(error) in kinds else "api_failed"
        payload = {"error": "process_census_unknown", "kind": kind}
        if "close_failed" in getattr(error, "__notes__", []):
            payload["close_failed"] = True
        print(json.dumps(payload), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
