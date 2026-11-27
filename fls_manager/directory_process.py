"""Supervise file-job commands independently of the panel process lifetime."""

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fls_manager.backup_lease import (
    check_restore_owner_lease, release_process_lease, restore_command_lease_path, try_process_lease,
)
from fls_manager.process_tree import WindowsJob, _posix_group_running


class DirectoryProcess:
    def __init__(self, command, *, workspace, cwd, env, lease_path, pass_fds=(), job_lease_path=None,
                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, shell=False, stop_grace_seconds=0,
                 restore_lease_fd=None):
        if not 0 <= stop_grace_seconds <= 10:
            raise ValueError("invalid command stop grace period")
        if os.name != "nt" and not (
            os.name == "posix" and Path("/proc").is_dir()
            and all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "WNOHANG", "CLD_EXITED"))
        ):
            raise RuntimeError("file job process supervision is unavailable on this platform")
        self.result_path = Path(workspace) / ".process-result.json"
        self.ready_path = Path(workspace) / ".process-ready.json"
        self.lease_ready_path = Path(workspace) / ".process-lease-ready.json"
        self._restore_lease_fd = restore_lease_fd
        self._lease_path = Path(lease_path)
        if restore_lease_fd is not None:
            self._check_restore_owner()
        self._result_lock = threading.Lock()
        self._returncode = None
        self._configuration = {
            "command": command, "cwd": str(cwd), "lease_path": str(lease_path),
            "pass_fds": list(pass_fds),
            "job_lease_path": str(job_lease_path) if job_lease_path is not None else None,
            "shell": shell, "stop_grace_seconds": stop_grace_seconds,
            "restore_owner": restore_lease_fd is not None,
            "log_lease_paths": sorted({
                str(stream.log_lease_path) for stream in (stdout, stderr)
                if isinstance(getattr(stream, "log_lease_path", None), (str, os.PathLike))
            }),
        }
        options = {"pass_fds": tuple(pass_fds)} if os.name == "posix" else {}
        self._process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).resolve()), str(self.result_path)],
            cwd=tempfile.gettempdir(), env=env, stdin=subprocess.PIPE,
            stdout=stdout, stderr=stderr, **options,
        )
        self.stderr = self._process.stderr

    @property
    def pid(self):
        return self._process.pid

    def _check_restore_owner(self):
        check_restore_owner_lease(self._lease_path, self._restore_lease_fd)

    def start(self, timeout=10):
        configuration, self._configuration = self._configuration, None
        if configuration is None:
            raise RuntimeError("file job command has already started")
        self._process.stdin.write(json.dumps(configuration).encode("utf-8") + b"\n")
        self._process.stdin.flush()
        deadline = time.monotonic() + timeout
        acknowledged = False
        while not self.ready_path.is_file():
            if self._restore_lease_fd is not None and not acknowledged and self.lease_ready_path.is_file():
                # The independent guard must be held before the owner opens the command gate.
                self._check_restore_owner()
                self._process.stdin.write(b"start\n")
                self._process.stdin.flush()
                acknowledged = True
            if self._process.poll() is not None:
                raise RuntimeError("file job supervisor failed to start")
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("file job start", timeout)
            time.sleep(0.01)

    def wait(self, timeout=None):
        if self._process.wait(timeout=timeout) != 0:
            raise RuntimeError("file job supervisor failed")
        # Other waiters can outlive the owner's temporary workspace cleanup.
        with self._result_lock:
            if self._returncode is not None:
                return self._returncode
            fd = os.open(self.result_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise RuntimeError("invalid file job result")
                result = json.loads(stream.read(1024))
            if not isinstance(result, dict) or type(result.get("returncode")) is not int:
                raise RuntimeError("invalid file job result")
            self._returncode = result["returncode"]
            return self._returncode

    def poll(self):
        return None if self._process.poll() is None else self.wait(timeout=0)

    def stop(self, timeout=10):
        if not self._process.stdin.closed:
            try:
                self._process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        # The supervisor owns the lease until its whole command tree has exited.
        self._process.wait(timeout=timeout)


def _write_status(path, value):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _command_returncode(process):
    if os.name == "nt":
        return process.poll()
    # Retain the command leader's PID until all descendants have been signalled.
    result = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if result is None:
        return None
    return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status


def _supervise(result_path):
    control = sys.stdin.buffer
    raw = control.readline(128 * 1024 + 1)
    if not raw:
        return
    if len(raw) > 128 * 1024 or not raw.endswith(b"\n"):
        raise ValueError("invalid file job configuration")
    configuration = json.loads(raw)
    inherited = tuple(configuration["pass_fds"])
    job_lease = None
    if configuration.get("job_lease_path"):
        job_lease = try_process_lease(configuration["job_lease_path"], create=False, shared=True)
        if job_lease is None:
            raise RuntimeError("file job identity is no longer active")
        os.lseek(job_lease, 1, os.SEEK_SET)
        if os.read(job_lease, 1) != b"1":
            release_process_lease(job_lease)
            raise RuntimeError("file job identity has been retired")
    restore_owner = configuration.get("restore_owner", False)
    lease_path = configuration["lease_path"]
    if restore_owner:
        lease = try_process_lease(restore_command_lease_path(lease_path))
    else:
        lease = try_process_lease(lease_path, shared=True)
    if lease is None:
        release_process_lease(job_lease)
        raise RuntimeError("directory restore is active")
    log_leases = []
    try:
        for path in configuration.get("log_lease_paths", []):
            log_lease = try_process_lease(path, create=False, shared=True)
            if log_lease is None:
                raise RuntimeError("log file is being deleted")
            log_leases.append(log_lease)
    except BaseException:
        for log_lease in reversed(log_leases):
            release_process_lease(log_lease)
        release_process_lease(job_lease)
        release_process_lease(lease)
        raise
    disconnected = threading.Event()
    process = None
    job = None
    assigned = False
    signalled = False
    terminate_deadline = None
    returncode = None

    def watch_owner():
        try:
            os.read(control.fileno(), 1)
        finally:
            disconnected.set()

    try:
        if restore_owner:
            _write_status(result_path.with_name(".process-lease-ready.json"), {"ready": True})
            if control.readline(16) != b"start\n":
                return
        threading.Thread(target=watch_owner, daemon=True).start()
        if disconnected.is_set():
            return
        if os.name == "nt":
            job = WindowsJob()
            # The wrapper cannot spawn the command before it belongs to the Job Object.
            process = subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).resolve()), "--command"],
                cwd=configuration["cwd"], stdin=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            job.assign(process)
            assigned = True
            if not disconnected.is_set():
                process.stdin.write(json.dumps({
                    "command": configuration["command"], "shell": configuration.get("shell", False),
                }).encode("utf-8") + b"\n")
                process.stdin.flush()
            process.stdin.close()
        else:
            process = subprocess.Popen(
                configuration["command"], cwd=configuration["cwd"], stdin=subprocess.DEVNULL,
                start_new_session=True, pass_fds=inherited, shell=configuration.get("shell", False),
            )
        _write_status(result_path.with_name(".process-ready.json"), {"ready": True})
        while not disconnected.is_set():
            code = _command_returncode(process)
            if code is not None:
                returncode = code
                break
            disconnected.wait(0.02)
    finally:
        if process is not None:
            # A failed cleanup must retain the lease; the panel may already be gone.
            while True:
                try:
                    if os.name == "nt":
                        if assigned:
                            job.terminate(10)
                        elif process.poll() is None:
                            process.kill()
                    else:
                        if not signalled:
                            grace = configuration.get("stop_grace_seconds", 0)
                            if disconnected.is_set() and grace:
                                if terminate_deadline is None:
                                    try:
                                        os.killpg(process.pid, signal.SIGTERM)
                                    except ProcessLookupError:
                                        pass
                                    terminate_deadline = time.monotonic() + grace
                                if _posix_group_running(process.pid) and time.monotonic() < terminate_deadline:
                                    time.sleep(0.01)
                                    continue
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            signalled = True
                        if _posix_group_running(process.pid):
                            time.sleep(0.01)
                            continue
                    actual_returncode = process.wait(timeout=10)
                    if returncode is None:
                        returncode = actual_returncode
                    break
                except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError):
                    time.sleep(0.1)
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
        if job is not None:
            job.close()
        try:
            _write_status(result_path, {"returncode": returncode if returncode is not None else -1})
        finally:
            for fd in inherited:
                os.close(fd)
            # An orphaned supervisor must close inherited logs before restore can proceed.
            for stream in (sys.stdout, sys.stderr):
                while not stream.closed:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        if not stream.closed:
                            time.sleep(0.05)
            for log_lease in reversed(log_leases):
                release_process_lease(log_lease)
            release_process_lease(job_lease)
            release_process_lease(lease)


def _run_command():
    raw = sys.stdin.buffer.readline(128 * 1024 + 1)
    if not raw:
        return 1
    if len(raw) > 128 * 1024 or not raw.endswith(b"\n"):
        raise ValueError("invalid command configuration")
    configuration = json.loads(raw)
    return subprocess.call(configuration["command"], stdin=subprocess.DEVNULL, shell=configuration.get("shell", False))


if __name__ == "__main__":
    if sys.argv[1] == "--command":
        sys.exit(_run_command())
    _supervise(Path(sys.argv[1]))
