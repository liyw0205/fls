"""Keep directory restores out of admitted file jobs, including worker cleanup."""

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from functools import wraps

from .backup_lease import release_process_lease, try_process_lease
from .data_access import PanelStorageBusy, directory_access


class DirectoryJobHistoryError(RuntimeError):
    pass


class DirectoryJobLock:
    """Serialize each history across workers and refresh before using cached rows."""

    def __init__(self, domain, refresh):
        from .paths import BASE_DIR

        self.base = BASE_DIR
        self.domain = domain
        self.refresh = refresh
        self.local = threading.local()
        self.mutex = threading.RLock()
        self.owners = {}

    def _path(self, name):
        directory = self.base / ".script_job_locks"
        if directory.is_symlink():
            raise DirectoryJobHistoryError("invalid script job lock directory")
        directory.mkdir(mode=0o700, exist_ok=True)
        return directory / f"{self.domain}-{name}.lock"

    def lease_path(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("invalid directory job ID")
        return self._path(job_id)

    def __enter__(self):
        depth = getattr(self.local, "depth", 0)
        if depth:
            self.mutex.acquire()
            self.local.depth += 1
            return self
        access = directory_access(self.base, write=True)
        access.__enter__()
        self.mutex.acquire()
        fd = None
        try:
            deadline = time.monotonic() + 5
            while fd is None:
                fd = try_process_lease(self._path("history"))
                if fd is None:
                    if time.monotonic() >= deadline:
                        raise PanelStorageBusy()
                    time.sleep(0.01)
            self.local.depth = 1
            self.refresh()
            self.local.fd, self.local.access = fd, access
            return self
        except BaseException:
            self.local.depth = 0
            release_process_lease(fd)
            self.mutex.release()
            access.__exit__(None, None, None)
            raise

    def __exit__(self, *error):
        self.local.depth -= 1
        if self.local.depth:
            self.mutex.release()
            return
        try:
            release_process_lease(self.local.fd)
        finally:
            self.mutex.release()
            self.local.access.__exit__(*error)

    def claim(self, job_id):
        fd = try_process_lease(self.lease_path(job_id), shared=True)
        if fd is None:
            raise DirectoryJobHistoryError("script job identity is busy")
        try:
            os.lseek(fd, 1, os.SEEK_SET)
            os.write(fd, b"1")
        except BaseException:
            release_process_lease(fd)
            raise
        owner = DirectoryJobOwner(self, job_id, fd)
        with self.mutex:
            self.owners[job_id] = owner
        return owner

    @contextmanager
    def probe(self, job_id):
        path = self.lease_path(job_id)
        try:
            fd = try_process_lease(path, create=False)
        except FileNotFoundError:
            yield False
            return
        if fd is None:
            yield True
            return
        try:
            # Invalidate before cleanup so a late supervisor cannot open its gate.
            os.lseek(fd, 1, os.SEEK_SET)
            os.write(fd, b"0")
            yield False
        finally:
            release_process_lease(fd)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


class DirectoryJobOwner:
    def __init__(self, lock, job_id, fd):
        self.lock, self.job_id, self.fd = lock, job_id, fd

    def close(self):
        with self.lock.mutex:
            if self.fd is None:
                return
            fd, self.fd = self.fd, None
            self.lock.owners.pop(self.job_id, None)
            release_process_lease(fd)
            with self.lock.probe(self.job_id):
                pass


class DirectoryJobCancel(threading.Event):
    def __init__(self, lock):
        super().__init__()
        self.lock = lock

    def is_set(self):
        if not super().is_set():
            with self.lock:
                pass
        return super().is_set()


def directory_job_rows(path, lock, jobs, cancels):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = []
    except (OSError, ValueError) as exc:
        raise DirectoryJobHistoryError("script job history cannot be read") from exc
    if not isinstance(raw, list):
        raise DirectoryJobHistoryError("script job history is not a list")
    for row in raw:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(key), str) and re.fullmatch(r"[a-f0-9]{32}", row[key])
            for key in ("id", "request_id")
        ):
            continue
        job_id = row["id"]
        if job_id in lock.owners and job_id in jobs:
            local = jobs[job_id]
            if row.get("status") == "cancelling" and job_id in cancels:
                cancels[job_id].set()
                local.update(status="cancelling", updated_at=row.get("updated_at", ""))
                if "phase" in local:
                    local["phase"] = "cancelling"
            yield dict(local, _worker_active=True)
        else:
            with lock.probe(job_id) as active:
                yield dict(row, _worker_active=active)


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
            try:
                return function(*args, **kwargs)
            except DirectoryJobHistoryError:
                from flask import jsonify

                response = jsonify(ok=False, code="history_unavailable", msg="脚本作业记录无法读取")
                response.status_code = 503
                response.headers["Cache-Control"] = "no-store"
                return response

    return guarded


def start_directory_job(target, *, args=(), name, owner=None):
    # Reserve a separate worker lease before starting, while the request owns its lease.
    fd = _acquire_job_lease()

    def run():
        try:
            target(*args)
        finally:
            try:
                if owner is not None:
                    owner.close()
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
