import os
import time
import uuid
from pathlib import Path


def _posix_group_running(group_id):
    if not Path("/proc").is_dir():
        raise RuntimeError("cannot verify dependency descendants on this platform")
    with os.scandir("/proc") as entries:
        for index, entry in enumerate(entries):
            if index >= 131072:
                raise RuntimeError("process scan exceeded its safety limit")
            if not entry.name.isdigit():
                continue
            try:
                with open(entry.path + "/stat", "rb") as stream:
                    encoded = stream.read(4097)
            except (FileNotFoundError, ProcessLookupError):
                continue
            if len(encoded) > 4096:
                raise RuntimeError("invalid process identity")
            fields = encoded.rsplit(b") ", 1)[-1].split()
            if len(fields) < 3:
                raise RuntimeError("incomplete process identity")
            if int(fields[2]) == group_id and fields[0] not in (b"Z", b"X"):
                return True
    return False


class WindowsJob:
    def __init__(self, name=None, *, open_existing=False):
        import ctypes
        from ctypes import wintypes

        class Limits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", Limits), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("times", ctypes.c_int64 * 4), ("faults", wintypes.DWORD),
                ("total", wintypes.DWORD), ("active", wintypes.DWORD),
                ("terminated", wintypes.DWORD),
            ]

        self._ctypes = ctypes
        self._accounting = Accounting
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE),
            "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for function, (arguments, result) in signatures.items():
            getattr(self._api, function).argtypes = arguments
            getattr(self._api, function).restype = result
        self.name = name or ("Local\\FLSBackupDependencies_" + uuid.uuid4().hex)
        self.handle = (self._api.OpenJobObjectW(0x0004 | 0x0008, False, self.name)
                       if open_existing else self._api.CreateJobObjectW(None, self.name))
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if not open_existing:
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self._api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                error = ctypes.WinError(ctypes.get_last_error())
                self.close()
                raise error

    def assign(self, process):
        if not self._api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise self._ctypes.WinError(self._ctypes.get_last_error())

    def terminate(self, timeout):
        if not self._api.TerminateJobObject(self.handle, 1):
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        deadline = time.monotonic() + timeout
        while True:
            accounting = self._accounting()
            if not self._api.QueryInformationJobObject(
                self.handle, 1, self._ctypes.byref(accounting), self._ctypes.sizeof(accounting), None,
            ):
                raise self._ctypes.WinError(self._ctypes.get_last_error())
            if accounting.active == 0:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("dependency job still has active processes")
            time.sleep(0.01)

    def close(self):
        if self.handle:
            self._api.CloseHandle(self.handle)
            self.handle = None

