"""Keep dependency commands and inherited logs under independent supervision."""

import os
import subprocess
import tempfile
import threading
import time

from .directory_process import DirectoryProcess
from .paths import BASE_DIR


class InstallProcess(DirectoryProcess):
    def __init__(self, command, *, stdout, stderr, cwd, env=None):
        self._workspace = tempfile.TemporaryDirectory(prefix="fls-dependency-install-")
        self._cleanup_lock = threading.Lock()
        try:
            super().__init__(
                command, workspace=self._workspace.name, cwd=cwd,
                env=os.environ.copy() if env is None else env,
                lease_path=BASE_DIR / ".backup_native_restore.lock",
                stdout=stdout, stderr=stderr, stop_grace_seconds=1,
            )
            self.start()
        except BaseException:
            if getattr(self, "_process", None) is not None:
                while True:
                    try:
                        self.stop(timeout=10)
                        break
                    except (OSError, subprocess.SubprocessError):
                        if self._process.poll() is not None:
                            break
                        time.sleep(0.05)
            self._workspace.cleanup()
            raise

    def wait(self, timeout=None):
        returncode = super().wait(timeout=timeout)
        with self._cleanup_lock:
            if self._workspace is not None:
                self._workspace.cleanup()
                self._workspace = None
        return returncode

    def terminate(self):
        # Close the owner channel; never send a signal to the supervisor itself.
        try:
            self.stop(timeout=0)
        except subprocess.TimeoutExpired:
            pass

    def kill(self):
        self.terminate()
