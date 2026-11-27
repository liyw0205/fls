import os
import errno
import stat
from pathlib import Path


def _windows_lease(fd, *, shared=False, unlock=False):
    """Lock byte zero without waiting; owner metadata starts at byte one."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    function = api.UnlockFileEx if unlock else api.LockFileEx
    function.restype = wintypes.BOOL
    function.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    if not unlock:
        function.argtypes += [wintypes.DWORD]
    function.argtypes += [wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
    overlap = Overlapped()
    handle = msvcrt.get_osfhandle(fd)
    if unlock:
        success = function(handle, 0, 1, 0, ctypes.byref(overlap))
    else:
        flags = 0x1 | (0 if shared else 0x2)  # FAIL_IMMEDIATELY, EXCLUSIVE_LOCK
        success = function(handle, flags, 0, 1, 0, ctypes.byref(overlap))
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())


def restore_command_lease_path(path):
    return Path(path).with_name(".backup_restore_command.lock")


def check_restore_owner_lease(path, fd):
    path = Path(path)
    try:
        held, expected = os.fstat(fd), path.lstat()
        if (path.name != ".backup_native_restore.lock" or not stat.S_ISREG(expected.st_mode)
            or not stat.S_ISREG(held.st_mode)
            or (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino)):
            raise ValueError("invalid restore owner lease")
        # Probe the primary directly: our child may already hold the command guard.
        probe = _try_process_lease(path, create=False, shared=True)
        if probe is not None:
            release_process_lease(probe)
            raise ValueError("inactive restore owner lease")
    except OSError as exc:
        raise ValueError("invalid restore owner lease") from exc


def try_process_lease(path, *, create=True, shared=False):
    path = Path(path)
    if path.name != ".backup_native_restore.lock":
        return _try_process_lease(path, create=create, shared=shared)
    lease = _try_process_lease(path, create=create, shared=shared)
    if lease is None:
        return None
    guard = None
    try:
        try:
            guard = _try_process_lease(restore_command_lease_path(path), create=create, shared=True)
        except FileNotFoundError:
            # A valid handoff cannot begin while we hold the primary lease.
            pass
        else:
            if guard is None:
                return None
        result, lease = lease, None
        return result
    finally:
        release_process_lease(guard)
        release_process_lease(lease)


def _try_process_lease(path, *, create=True, shared=False):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError("invalid restore process lock")
    flags = os.O_RDWR | (os.O_CREAT if create else 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("restore process lock is not a regular file")
        if os.name == "nt":
            _windows_lease(fd, shared=shared)
        else:
            import fcntl
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        return fd
    except OSError as exc:
        os.close(fd)
        if getattr(exc, "winerror", None) == 33 or (os.name != "nt" and exc.errno in (errno.EACCES, errno.EAGAIN)):
            return None
        raise
    except BaseException:
        os.close(fd)
        raise


def release_process_lease(fd):
    if fd is None:
        return
    try:
        if os.name == "nt":
            _windows_lease(fd, unlock=True)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
