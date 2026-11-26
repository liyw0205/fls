import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path


if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from fls_manager.process_tree import WindowsJob, _posix_group_running


class DependencyProcess:
    def __init__(self, command, *, workspace, cwd, env, lease_fd=None):
        self.result_path = Path(workspace) / "process-result.json"
        self._stopped = False
        self._signal_sent = False
        self._job = WindowsJob() if os.name == "nt" else None
        self._process = None
        options = {"start_new_session": True, "pass_fds": (lease_fd,) if lease_fd is not None else ()}
        if os.name == "nt":
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        try:
            self._process = subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).resolve()), str(self.result_path), *command],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=cwd, env=env, **options,
            )
            self.stdout = self._process.stdout
            if self._job is not None:
                self._job.assign(self._process)
        except BaseException:
            if self._process is not None:
                # The start gate has not opened, so the supervisor has no children.
                self._process.kill()
                self._process.wait(timeout=10)
                self._process.stdin.close()
                self._process.stdout.close()
            if self._job is not None:
                self._job.close()
            raise

    @property
    def identity(self):
        if self._job is not None:
            return {"kind": "windows_job", "name": self._job.name, "pid": self._process.pid}
        return {"kind": "posix_group", "pid": self._process.pid}

    def start(self):
        self._process.stdin.write(b"start\n")
        self._process.stdin.flush()

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(self.result_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except FileNotFoundError:
                pass
            else:
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise RuntimeError("invalid dependency process result")
                    result = json.loads(stream.read(1024))
                if not isinstance(result, dict) or type(result.get("returncode")) is not int:
                    raise RuntimeError("invalid dependency process result")
                return result["returncode"]
            # Do not reap the POSIX session leader before signalling its group.
            if os.name == "nt":
                exited = self._process.poll() is not None
            elif hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
                exited = os.waitid(os.P_PID, self._process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            else:
                exited = False
            if exited:
                raise RuntimeError("dependency supervisor exited without a result")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("dependency process", timeout)
            time.sleep(min(0.02, remaining))

    def stop(self, timeout=10):
        if self._stopped:
            return
        if self._job is not None:
            self._job.terminate(timeout)
        elif not self._signal_sent:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._signal_sent = True
        if self._job is None:
            deadline = time.monotonic() + timeout
            while _posix_group_running(self._process.pid):
                if time.monotonic() >= deadline:
                    raise TimeoutError("dependency descendants have not exited")
                time.sleep(0.01)
        self._process.wait(timeout=timeout)
        self._process.stdin.close()
        self._stopped = True
        if self._job is not None:
            self._job.close()


def cleanup_previous_process(identity):
    if identity["kind"] == "windows_job" and os.name == "nt":
        try:
            job = WindowsJob(identity["name"], open_existing=True)
        except OSError as exc:
            if exc.winerror == 2:
                return
            raise
        try:
            job.terminate(10)
        finally:
            job.close()
    elif identity["kind"] == "posix_group" and os.name == "posix":
        if not _posix_group_running(identity["pid"]):
            return
        raise RuntimeError("dependency process group still exists; its ownership needs verification")
    else:
        raise RuntimeError("dependency process ownership cannot be verified on this platform")


def _supervise(result_path, command):
    control = sys.stdin.buffer
    if control.readline(16) != b"start\n":
        return

    def parent_disconnected():
        try:
            control.read(1)
        finally:
            if os.name == "posix":
                os.killpg(os.getpgrp(), signal.SIGKILL)
            os._exit(1)

    threading.Thread(target=parent_disconnected, daemon=True).start()
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, close_fds=True)
    returncode = process.wait()
    temporary = result_path.with_suffix(".tmp")
    with temporary.open("x", encoding="ascii") as stream:
        json.dump({"returncode": returncode}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, result_path)
    # Keep the owned session leader alive until the parent reaps the whole tree.
    threading.Event().wait()


if __name__ == "__main__":
    _supervise(Path(sys.argv[1]), sys.argv[2:])
