"""Publish atomic receipts while readers retain a stable previous object."""

import os
import stat

from .directory_jobs import DirectoryJobHistoryError


def open_receipt(path, *, delete_access=False):
    if os.name != "nt":
        return os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class AttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    api.GetFileInformationByHandleEx.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    share = 0x1 | 0x2 | 0x4  # FILE_SHARE_READ, WRITE, DELETE
    desired_access = 0x80000000 | (0x10000 if delete_access else 0)  # GENERIC_READ, DELETE
    handle = api.CreateFileW(str(path), desired_access, share, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = AttributeTagInfo()
        if not api.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise DirectoryJobHistoryError("invalid persisted receipt")
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api.CloseHandle(handle)
        raise


def publish_receipt(temporary, path):
    if os.name != "nt":
        os.replace(temporary, path)
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    filename = str(path.absolute()).encode("utf-16-le")

    class RenameInfo(ctypes.Structure):
        _fields_ = [("flags", wintypes.DWORD), ("root", wintypes.HANDLE),
                    ("length", wintypes.DWORD), ("filename", ctypes.c_ushort * (len(filename) // 2 + 1))]

    info = RenameInfo()
    # MoveFileEx/os.replace rejects open targets despite delete sharing.
    info.flags = 0x1 | 0x2  # FILE_RENAME_REPLACE_IF_EXISTS, FILE_RENAME_POSIX_SEMANTICS
    info.length = len(filename)
    ctypes.memmove(ctypes.addressof(info) + RenameInfo.filename.offset, filename, len(filename))
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    api.SetFileInformationByHandle.restype = wintypes.BOOL
    fd = open_receipt(temporary, delete_access=True)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise DirectoryJobHistoryError("invalid persisted receipt")
        if not api.SetFileInformationByHandle(msvcrt.get_osfhandle(fd), 22, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        os.close(fd)
