import errno
import os
import stat

from .paths import DATA_DIR
from .data_access import directory_access


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
    descriptor = os.open(path, flags, 0o600)
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
