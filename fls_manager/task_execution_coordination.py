"""Keep task ownership and restore exclusion alive through command and log cleanup."""

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import time

from .backup_lease import release_process_lease, try_process_lease
from .directory_jobs import DirectoryJobHistoryError, DirectoryJobLock, _acquire_job_lease
from .directory_process import DirectoryProcess
from .paths import BASE_DIR, LOG_DIR


EXECUTIONS = DirectoryJobLock("task-execution", lambda: None)
IDENTITIES = DirectoryJobLock("task-command", lambda: None)
IN_FLIGHT = frozenset(("starting", "delaying", "running", "retry_wait", "stopping"))
TERMINAL = frozenset(("success", "failed", "timeout", "start_failed", "stopped", "unknown"))


def _receipt_path(task_id):
    directory = BASE_DIR / ".task_executions"
    if directory.is_symlink():
        raise DirectoryJobHistoryError("invalid task execution directory")
    return directory / (hashlib.sha256(task_id.encode("utf-8")).hexdigest() + ".json")


def _read(path):
    if path.is_symlink():
        raise DirectoryJobHistoryError("invalid task execution receipt")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                     | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise DirectoryJobHistoryError("invalid task execution receipt")
            raw = stream.read(16385)
    except FileNotFoundError:
        return None
    try:
        row = json.loads(raw) if len(raw) <= 16384 else None
        if (not isinstance(row, dict) or not isinstance(row.get("task_id"), str)
                or _receipt_path(row["task_id"]) != path
                or not re.fullmatch(r"[a-f0-9]{32}", str(row.get("history_id", "")))
                or row.get("status") not in IN_FLIGHT | TERMINAL
                or type(row.get("stop_requested")) is not bool
                or (row.get("previous_history_id") is not None
                    and not re.fullmatch(r"[a-f0-9]{32}", str(row["previous_history_id"])))
                or not isinstance(row.get("log_name", ""), str)
                or row.get("log_name") in (".", "..")
                or any(char in row.get("log_name", "") for char in ("/", "\\", "\x00"))):
            raise ValueError("invalid task execution receipt")
        return row
    except (ValueError, TypeError) as exc:
        raise DirectoryJobHistoryError("task execution receipt cannot be read") from exc


def _write(row):
    path = _receipt_path(row["task_id"])
    path.parent.mkdir(mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".task-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            json.dump(row, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _active(history_id):
    directory = IDENTITIES.base / ".script_job_locks"
    if directory.is_symlink():
        raise DirectoryJobHistoryError("invalid task identity directory")
    try:
        fd = try_process_lease(directory / f"task-command-{history_id}.lock", create=False)
    except FileNotFoundError:
        return False
    if fd is None:
        return True
    release_process_lease(fd)
    return False


def execution_snapshot(task_ids=None):
    # Atomic files and read-only identity probes also work under the history storage lock.
    paths = ((_receipt_path(str(task_id)) for task_id in set(task_ids)) if task_ids is not None
             else (BASE_DIR / ".task_executions").glob("*.json"))
    result = {}
    for path in paths:
        row = _read(path)
        if row is None:
            continue
        active = _active(row["history_id"])
        status = row["status"]
        if status in IN_FLIGHT and not active:
            status = "unknown"
        elif active and row["stop_requested"]:
            status = "stopping"
        result[row["task_id"]] = dict(row, status=status, active=active)
    return result


def request_task_stop(task_id, *, retry_only=False):
    with EXECUTIONS:
        row = _read(_receipt_path(task_id))
        if row is None or not _active(row["history_id"]):
            return False
        if retry_only and row["status"] != "retry_wait":
            return False
        if not row["stop_requested"]:
            _write(dict(row, stop_requested=True))
        return True


class TaskExecution:
    def __init__(self, task_id, history_id, *, previous=None):
        self.task_id, self.history_id = task_id, history_id
        self.mutex = threading.RLock()
        self.references = 1
        self.retry_ready = threading.Event()
        self.watcher_handoff = False
        self.process = self.log_fp = self.workspace = None
        self.terminal = "unknown"
        self.failed = False
        self.lease = _acquire_job_lease()
        self.owner = None
        try:
            with EXECUTIONS:
                row = _read(_receipt_path(task_id))
                if row is not None and _active(row["history_id"]):
                    if (previous is None or row["history_id"] != previous.history_id
                            or row["status"] != "retry_wait" or row["stop_requested"]):
                        raise DirectoryJobHistoryError("task execution is still active")
                snapshots = execution_snapshot().values()
                known = {item["history_id"] for item in snapshots}
                known.update(item["previous_history_id"] for item in snapshots
                             if item.get("previous_history_id"))
                for path in (IDENTITIES.base / ".script_job_locks").glob("task-command-*.lock"):
                    identity = path.name[len("task-command-"):-len(".lock")]
                    if identity not in known and re.fullmatch(r"[a-f0-9]{32}", identity) and _active(identity):
                        raise DirectoryJobHistoryError("active task execution receipt is missing")
                self.owner = IDENTITIES.claim(history_id)
                _write(dict(task_id=task_id, history_id=history_id, status="starting", stop_requested=False,
                            previous_history_id=previous.history_id if previous is not None else None))
        except BaseException:
            if self.owner is not None:
                self.owner.close()
            release_process_lease(self.lease)
            raise

    def retain(self):
        with self.mutex:
            if self.references <= 0:
                raise RuntimeError("task execution has already closed")
            self.references += 1

    def phase(self, status):
        with EXECUTIONS:
            row = _read(_receipt_path(self.task_id))
            if row is None or row["history_id"] != self.history_id:
                raise DirectoryJobHistoryError("active task execution receipt is missing")
            _write(dict(row, status=status))

    def bind_log(self, path):
        if path.parent.resolve() != LOG_DIR.resolve():
            raise ValueError("invalid task log directory")
        with EXECUTIONS:
            row = _read(_receipt_path(self.task_id))
            if row is None or row["history_id"] != self.history_id:
                raise DirectoryJobHistoryError("active task execution receipt is missing")
            _write(dict(row, log_name=path.name))

    def stopping(self):
        with EXECUTIONS:
            row = _read(_receipt_path(self.task_id))
            if row is None or row["history_id"] != self.history_id:
                raise DirectoryJobHistoryError("active task execution receipt is missing")
            return row["stop_requested"]

    def release(self):
        with self.mutex:
            self.references -= 1
            if self.references:
                return
        # Neither a stop timeout nor an unclosed log proves the directory is idle.
        if self.process is not None:
            while True:
                try:
                    self.process.stop(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
                except Exception:
                    self.failed = True
                    if self.process._process.poll() is not None:
                        break
                    time.sleep(0.05)
        if self.log_fp is not None:
            while not self.log_fp.closed:
                try:
                    self.log_fp.close()
                except Exception:
                    self.failed = True
                    time.sleep(0.05)
        if self.workspace is not None:
            try:
                self.workspace.cleanup()
            except OSError:
                self.failed = True
        try:
            with EXECUTIONS:
                row = _read(_receipt_path(self.task_id))
                if row is not None and row["history_id"] == self.history_id:
                    _write(dict(row, status="unknown" if self.failed else self.terminal))
        finally:
            try:
                self.owner.close()
            finally:
                release_process_lease(self.lease)


class TaskProcess:
    """Consume durable stop intent only in the command's owning worker."""

    def __init__(self, execution, command, *, cwd, env, shell, stdout, on_stop):
        self.execution, self.on_stop = execution, on_stop
        execution.workspace = tempfile.TemporaryDirectory(prefix="fls-task-command-")
        self.inner = DirectoryProcess(
            command, workspace=execution.workspace.name, cwd=cwd, env=env, shell=shell,
            stdout=stdout, stderr=subprocess.STDOUT,
            lease_path=BASE_DIR / ".backup_native_restore.lock",
            job_lease_path=IDENTITIES.lease_path(execution.history_id), stop_grace_seconds=1,
        )
        execution.process = self.inner
        self.inner.start()

    @property
    def pid(self):
        return self.inner.pid

    def poll(self):
        return self.inner.poll()

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            # Check the intent before observing exit so an accepted stop is retained.
            if self.execution.stopping():
                self.on_stop()
                try:
                    self.inner.stop(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            code = self.inner.poll()
            if code is not None:
                return code
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("task command", timeout)
            time.sleep(0.05)

    def stop(self, timeout=10):
        self.inner.stop(timeout=timeout)


class TaskRetryTimer:
    def __init__(self, interval, callback, execution, on_cancel, timer_factory):
        self.interval, self.callback = interval, callback
        self.execution, self.on_cancel = execution, on_cancel
        self.cancelled = threading.Event()
        self.daemon = True
        self.started = False
        self.worker = timer_factory(0, self._run)
        self.worker.daemon = True

    def _run(self):
        try:
            deadline = time.monotonic() + self.interval
            while not self.cancelled.wait(min(0.05, max(0.01, deadline - time.monotonic()))):
                if self.execution.failed or self.execution.stopping():
                    self.cancelled.set()
                    break
                if time.monotonic() >= deadline and self.execution.retry_ready.is_set():
                    self.callback()
                    return
            self.on_cancel()
        except Exception:
            self.execution.failed = True
            self.on_cancel()
        finally:
            self.execution.release()

    def start(self):
        self.execution.retain()
        try:
            self.worker.name = f"fls-task-retry-{self.execution.history_id[:8]}"
            self.worker.start()
            self.started = True
        except BaseException:
            self.execution.release()
            raise

    def cancel(self):
        self.cancelled.set()
        self.worker.cancel()
