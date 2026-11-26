"""Coordinate live data connections with directory replacement across workers."""

import threading
from contextlib import contextmanager
from pathlib import Path

from .backup_lease import release_process_lease, try_process_lease


class PanelStorageBusy(RuntimeError):
    def __init__(self):
        super().__init__("面板正在恢复目录或仍有待处理现场，请稍后查询原请求")


_ACCESS = threading.local()


def _check_pending(base, write):
    from .backup_commit import assert_data_accessible

    try:
        assert_data_accessible(base, write=write)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PanelStorageBusy() from exc


@contextmanager
def directory_access(base=None, *, exclusive=False, write=False, recovery=False):
    if base is None:
        from .paths import BASE_DIR
        base = BASE_DIR
    base = Path(base).resolve()
    held = getattr(_ACCESS, "held", None)
    if held is None:
        held = _ACCESS.held = {}
    owner = held.get(base)
    if owner is not None:
        if exclusive and not owner["exclusive"]:
            raise PanelStorageBusy()
        if not owner["exclusive"]:
            _check_pending(base, write)
        yield
        return

    # Never wait while a caller may already hold a model lock.
    fd = try_process_lease(base / ".backup_data_io.lock", shared=not exclusive)
    if fd is None:
        raise PanelStorageBusy()
    try:
        if not (exclusive and recovery):
            _check_pending(base, write or exclusive)
        held[base] = {"exclusive": exclusive}
        try:
            yield
        finally:
            held.pop(base, None)
    finally:
        release_process_lease(fd)
