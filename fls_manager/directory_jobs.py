"""Keep directory restores out of admitted file jobs, including worker cleanup."""

import threading
from contextlib import contextmanager
from functools import wraps

from .backup_lease import release_process_lease, try_process_lease
from .data_access import PanelStorageBusy, directory_access


def _acquire_job_lease():
    from .paths import BASE_DIR

    fd = try_process_lease(BASE_DIR / ".backup_native_restore.lock", shared=True)
    if fd is None:
        raise PanelStorageBusy()
    try:
        with directory_access(write=True):
            pass
    except BaseException:
        release_process_lease(fd)
        raise
    return fd


@contextmanager
def directory_job():
    fd = _acquire_job_lease()
    try:
        yield
    finally:
        release_process_lease(fd)


def directory_job_operation(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        with directory_job():
            return function(*args, **kwargs)

    return guarded


def start_directory_job(target, *, args=(), name):
    # Reserve a separate worker lease before starting, while the request owns its lease.
    fd = _acquire_job_lease()

    def run():
        try:
            target(*args)
        finally:
            release_process_lease(fd)

    worker = None
    try:
        worker = threading.Thread(target=run, daemon=True, name=name)
        worker.start()
    except BaseException:
        if worker is None or worker.ident is None:
            release_process_lease(fd)
        raise
    return worker
