import errno
import os
import stat

from .paths import DATA_DIR
from .data_access import directory_access


def _open_instance_lock(path, flags):
    if os.name != "nt":
        return os.open(path, flags, 0o600)

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
    # Keep the byte-range lock on the same object while restore moves data/.
    share = 0x1 | 0x2 | 0x4  # FILE_SHARE_READ, WRITE, DELETE
    disposition = 4 if flags & os.O_CREAT else 3  # OPEN_ALWAYS, OPEN_EXISTING
    handle = api.CreateFileW(str(path), 0xC0000000, share, None, disposition, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = AttributeTagInfo()
        if not api.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & (0x10 | 0x400):  # DIRECTORY, REPARSE_POINT
            raise RuntimeError("FLS Manager 实例锁必须是普通文件")
        return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        api.CloseHandle(handle)
        raise


def acquire_instance_lock():
    with directory_access(DATA_DIR.parent):
        return _acquire_instance_lock()


def _acquire_instance_lock():
    path = DATA_DIR / "fls-manager.lock"
    if path.is_symlink():
        raise RuntimeError("FLS Manager 实例锁不能是符号链接")

    flags = os.O_RDWR
    if not path.exists():
        with directory_access(DATA_DIR.parent, write=True):
            DATA_DIR.mkdir(parents=True, exist_ok=True)
        flags |= os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = _open_instance_lock(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("FLS Manager 实例锁必须是普通文件")

        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                with directory_access(DATA_DIR.parent, write=True):
                    os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    os.close(descriptor)
                    return None
                raise
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    os.close(descriptor)
                    return None
                raise

        return descriptor
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def release_instance_lock(descriptor):
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
