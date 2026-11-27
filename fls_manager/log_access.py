"""Coordinate log handles and deletion across panel workers."""

import hashlib
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

from .backup_lease import release_process_lease, try_process_lease


class LogFileBusy(RuntimeError):
    pass


def file_version(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("invalid log file")
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return hashlib.sha256(repr(identity).encode("ascii")).hexdigest()


def _is_link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


@contextmanager
def log_file_access(path, *, exclusive=False, restore_lease_fd=None, legacy_root=False, daemon=False):
    from .directory_jobs import _acquire_job_lease
    from .paths import BASE_DIR, LOG_DIR

    path = Path(path).absolute()
    log_root = BASE_DIR if legacy_root and os.path.samefile(path.parent, BASE_DIR) else LOG_DIR
    if (not path.name.endswith(".log") or ".." in path.parts or _is_link(path.parent)
        or not os.path.samefile(path.parent, log_root)):
        raise ValueError("invalid log path")
    if _is_link(path) or ":" in path.name:
        raise ValueError("invalid log file")
    if daemon and (path != LOG_DIR / "fls-manager-daemon.log" or exclusive or restore_lease_fd is not None):
        raise ValueError("invalid daemon log path")
    # Normalize Windows short-name aliases before choosing the shared lock.
    if os.name == "nt":
        path = path.resolve()
        if not os.path.samefile(path.parent, log_root):
            raise ValueError("invalid log path")
    restore = None
    if daemon:
        # This fixed log is outside the data/scripts restore set. A permanent
        # restore lease would make restoring a running panel impossible.
        pass
    elif restore_lease_fd is None:
        restore = _acquire_job_lease()
    else:
        held = os.fstat(restore_lease_fd)
        expected = (BASE_DIR / ".backup_native_restore.lock").stat()
        if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError("invalid restore owner lease")
    lock = directory = None
    try:
        lock_root = BASE_DIR / ".log_file_locks"
        if _is_link(lock_root):
            raise ValueError("invalid log lock directory")
        lock_root.mkdir(mode=0o700, exist_ok=True)
        # Old installation receipts can refer to a log directly in BASE_DIR.
        scope = "legacy/" if log_root == BASE_DIR else ""
        name = hashlib.sha256((scope + os.path.normcase(path.name)).encode("utf-8")).hexdigest()
        lock_path = lock_root / f"{name}.lock"
        lock = try_process_lease(lock_path, shared=not exclusive)
        if lock is None:
            raise LogFileBusy("log file is in use")
        if os.name == "posix":
            directory = os.open(log_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        expected = os.stat(log_root, follow_symlinks=False)
        if not stat.S_ISDIR(expected.st_mode) or _is_link(log_root):
            raise ValueError("invalid log directory")
        if directory is not None:
            pinned = os.fstat(directory)
            if (pinned.st_dev, pinned.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError("log directory changed")
        access = LogAccess(path, directory, expected, lock_path)
        access.check_directory()
        yield access
    finally:
        if directory is not None:
            os.close(directory)
        release_process_lease(lock)
        release_process_lease(restore)


class LogAccess:
    def __init__(self, path, directory, expected, lock_path):
        self.path, self.directory = path, directory
        self.expected, self.lock_path = expected, lock_path

    def check_directory(self):
        current = os.stat(self.path.parent, follow_symlinks=False)
        if (_is_link(self.path.parent) or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (self.expected.st_dev, self.expected.st_ino)):
            raise ValueError("log directory changed")

    def info(self):
        self.check_directory()
        if self.directory is not None:
            info = os.stat(self.path.name, dir_fd=self.directory, follow_symlinks=False)
        else:
            info = self.path.lstat()
        if _is_link(self.path):
            raise ValueError("invalid log file")
        file_version(info)
        return info

    def unlink(self):
        self.check_directory()
        if self.directory is None:
            self.path.unlink()
        else:
            os.unlink(self.path.name, dir_fd=self.directory)


class LeasedLogFile:
    def __init__(self, stream, access, context):
        self.stream, self.access, self.context = stream, access, context
        self.log_lease_path = access.lock_path
        self.name = str(access.path)
        self.close_lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __iter__(self):
        return iter(self.stream)

    def close(self):
        with self.close_lock:
            try:
                self.stream.close()
            finally:
                if self.stream.closed and self.context is not None:
                    context, self.context = self.context, None
                    context.__exit__(None, None, None)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()


def open_log_file(path, mode="ab", buffering=-1, *, restore_lease_fd=None, legacy_root=False, **kwargs):
    if mode not in ("a", "ab", "rb", "xb"):
        raise ValueError("unsupported log mode")
    context = log_file_access(path, restore_lease_fd=restore_lease_fd, legacy_root=legacy_root)
    return _open_log_file(context, mode, buffering, **kwargs)


def open_daemon_log(path):
    return _open_log_file(log_file_access(path, daemon=True), "ab", 0)


def _open_log_file(context, mode, buffering, **kwargs):
    access = context.__enter__()
    stream = None
    fd = None
    try:
        flags = os.O_RDONLY if mode == "rb" else os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if mode == "xb":
            flags |= os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        target = access.path.name if access.directory is not None else access.path
        options = {"dir_fd": access.directory} if access.directory is not None else {}
        fd = os.open(target, flags, 0o600, **options)
        opened = os.fstat(fd)
        file_version(opened)
        current = access.info()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("log file changed while opening")
        stream = os.fdopen(fd, mode, buffering=buffering, **kwargs)
        fd = None
        return LeasedLogFile(stream, access, context)
    except BaseException:
        if fd is not None:
            os.close(fd)
        if stream is not None:
            stream.close()
        context.__exit__(None, None, None)
        raise


def delete_log_file(path, expected_version):
    """Check and unlink under one exclusive lease; never retry another object."""
    name = Path(path).name
    try:
        with log_file_access(path, exclusive=True) as access:
            try:
                actual = file_version(access.info())
            except FileNotFoundError:
                return "missing", 0, ""
            if actual != expected_version:
                return "conflict", 0, f"{name}: 文件在删除前已变化"
            error = None
            try:
                access.unlink()
            except FileNotFoundError:
                return "missing", 0, ""
            except OSError as exc:
                error = exc
            try:
                after = file_version(access.info())
            except FileNotFoundError:
                return "deleted", 1, ""
            except (OSError, ValueError):
                return "unknown", 0, f"{name}: 删除结果无法确认"
            if after != expected_version:
                return "conflict", 0, f"{name}: 删除期间文件已变化，未删除新文件"
            return "failed", 0, f"{name}: {error or '删除后文件仍存在'}"
    except LogFileBusy:
        return "busy", 0, f"{name}: 日志正在使用，请稍后重新选择"
    except (OSError, ValueError) as exc:
        return "failed", 0, f"{name}: {exc}"
