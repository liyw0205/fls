import json
import os
import runpy
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PROCESS_FILE = Path(__file__).resolve().parents[1] / "fls_manager/routes/backup/dependency_process.py"


def process_exited(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 87
        try:
            code = wintypes.DWORD()
            if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value != 259
        finally:
            api.CloseHandle(handle)
    status = Path(f"/proc/{pid}/stat")
    try:
        return status.read_text().split(") ", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


class BackupDependencyProcessTests(unittest.TestCase):
    def module(self):
        return runpy.run_path(str(PROCESS_FILE))

    def assert_exited(self, pid):
        deadline = time.monotonic() + 5
        while not process_exited(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(process_exited(pid), f"process {pid} did not exit")

    def test_start_gate_and_exited_command_keep_owned_supervisor_until_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            marker = workspace / "started"
            module = self.module()
            process = module["DependencyProcess"](
                [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch(); print('completed')"],
                workspace=workspace, cwd=directory, env=os.environ.copy(),
            )
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.1)
                self.assertFalse(marker.exists())
                process.start()
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertTrue(marker.exists())
                if os.name == "posix":
                    status = os.waitid(os.P_PID, process.identity["pid"], os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    self.assertIsNone(status)
                process.stop()
                self.assertIn(b"completed", process.stdout.read())
                with patch.object(module["os"], "killpg", create=True) as kill_group:
                    process.stop()
                    kill_group.assert_not_called()
            finally:
                process.stop()
                process.stdout.close()

    def test_cleanup_kills_descendant_after_command_parent_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            marker = workspace / "child.pid"
            command = (
                "import pathlib,subprocess,sys\n"
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n"
            )
            process = self.module()["DependencyProcess"](
                [sys.executable, "-c", command], workspace=workspace, cwd=directory, env=os.environ.copy(),
            )
            try:
                process.start()
                self.assertEqual(process.wait(timeout=5), 0)
                pid = int(marker.read_text())
                self.assertFalse(process_exited(pid))
                process.stop()
                self.assert_exited(pid)
            finally:
                process.stop()
                process.stdout.close()

    def test_owner_crash_terminates_supervisor_and_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            marker = workspace / "children.json"
            command = (
                "import json,os,pathlib,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]))\n"
                "time.sleep(60)\n"
            )
            owner = (
                "import json,os,pathlib,runpy,sys,time\n"
                f"module=runpy.run_path({str(PROCESS_FILE)!r})\n"
                "lease=None\n"
                "if os.name == 'posix':\n"
                " import fcntl\n"
                f" lease=os.open({str(workspace / 'owner.lock')!r}, os.O_CREAT|os.O_RDWR, 0o600)\n"
                " fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
                f"process=module['DependencyProcess']([sys.executable,'-c',{command!r}],workspace=pathlib.Path({directory!r}),cwd={directory!r},env=os.environ.copy(),lease_fd=lease)\n"
                "process.start()\n"
                "deadline=time.monotonic()+5\n"
                f"while not pathlib.Path({str(marker)!r}).exists() and time.monotonic()<deadline: time.sleep(0.02)\n"
                "print(json.dumps(process.identity),flush=True)\n"
                "os._exit(0)\n"
            )
            result = subprocess.run([sys.executable, "-c", owner], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            identity = json.loads(result.stdout)
            self.assertTrue(marker.exists(), "owner exited before its command started")
            for pid in json.loads(marker.read_text()):
                self.assert_exited(pid)
            if os.name == "posix":
                self.assert_exited(identity["pid"])
                import fcntl
                fd = os.open(workspace / "owner.lock", os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    @unittest.skipUnless(os.name == "posix", "POSIX process identity")
    def test_retry_after_wait_timeout_does_not_signal_a_reusable_group_again(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.module()
            process = module["DependencyProcess"](
                [sys.executable, "-c", "import time; time.sleep(60)"],
                workspace=Path(directory), cwd=directory, env=os.environ.copy(),
            )
            try:
                process.start()
                wait = process._process.wait
                with patch.object(module["os"], "killpg", wraps=os.killpg) as kill_group:
                    with patch.object(process._process, "wait", side_effect=subprocess.TimeoutExpired("supervisor", 10)):
                        with self.assertRaises(subprocess.TimeoutExpired):
                            process.stop()
                    process.stop()
                    kill_group.assert_called_once()
                    self.assertIsNotNone(wait(timeout=1))
            finally:
                process.stop()
                process.stdout.close()
