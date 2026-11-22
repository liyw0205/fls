import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix", "fls.sh startup behavior is POSIX-specific")
class FlsStartupTests(unittest.TestCase):
    def test_start_reuses_live_manager_when_pid_file_is_missing_or_stale(self):
        for pid_value in (None, str(os.getpid()), "999999999"):
            with self.subTest(pid_value=pid_value), tempfile.TemporaryDirectory(prefix="fls-startup-") as temp_dir:
                base_dir = Path(temp_dir)
                data_dir = base_dir / "data"
                data_dir.mkdir()
                manager = base_dir / "fls-manager.py"
                manager.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
                (base_dir / "fls_manager").mkdir()

                proc = subprocess.Popen(
                    [sys.executable, str(manager)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    deadline = time.monotonic() + 3
                    while proc.poll() is None and time.monotonic() < deadline:
                        try:
                            os.kill(proc.pid, 0)
                            break
                        except OSError:
                            time.sleep(0.01)
                    self.assertIsNone(proc.poll())

                    pid_file = data_dir / "fls-manager.pid"
                    if pid_value is not None:
                        pid_file.write_text(pid_value + "\n", encoding="ascii")

                    env = os.environ.copy()
                    env.update({
                        "FLS_BASE_DIR": str(base_dir),
                        "FLS_PYTHON": sys.executable,
                        "CI": "true",
                    })
                    result = subprocess.run(
                        ["sh", str(ROOT / "fls.sh"), "start"],
                        cwd=ROOT,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )

                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(f"已在运行，PID: {proc.pid}", result.stdout)
                    self.assertEqual(pid_file.read_text(encoding="ascii").strip(), str(proc.pid))
                finally:
                    proc.terminate()
                    proc.wait(timeout=5)

    @unittest.skipUnless(shutil.which("flock"), "flock is optional on supported shells")
    def test_concurrent_start_commands_launch_only_one_manager(self):
        with tempfile.TemporaryDirectory(prefix="fls-startup-race-") as temp_dir:
            base_dir = Path(temp_dir)
            (base_dir / "data").mkdir()
            manager = base_dir / "fls-manager.py"
            manager.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            (base_dir / "fls_manager").mkdir()

            env = os.environ.copy()
            env.update({
                "FLS_BASE_DIR": str(base_dir),
                "FLS_PYTHON": sys.executable,
                "CI": "true",
            })
            command = ["sh", str(ROOT / "fls.sh"), "start"]
            starts = [
                subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
                for _ in range(2)
            ]
            try:
                results = [proc.communicate(timeout=15) for proc in starts]
                self.assertTrue(all(proc.returncode == 0 for proc in starts), results)
                self.assertEqual(sum("启动成功" in output for output, _error in results), 1, results)
                self.assertEqual(sum("正在启动" in output for output, _error in results), 1, results)

                deadline = time.monotonic() + 3
                manager_pids = []
                while time.monotonic() < deadline:
                    listing = subprocess.run(
                        ["ps", "-ww", "-eo", "pid=,args="], capture_output=True, text=True, check=True,
                    ).stdout
                    manager_pids = [
                        int(line.split(None, 1)[0]) for line in listing.splitlines()
                        if str(manager) in line
                    ]
                    if manager_pids:
                        break
                    time.sleep(0.02)

                pid_file = base_dir / "data" / "fls-manager.pid"
                pid_value = pid_file.read_text(encoding="ascii").strip() if pid_file.exists() else "missing"
                process_state = subprocess.run(
                    ["ps", "-p", pid_value, "-o", "pid=,ppid=,comm=,args="],
                    capture_output=True, text=True,
                ).stdout
                daemon_log = base_dir / "log" / "fls-manager-daemon.log"
                log_text = daemon_log.read_text(encoding="utf-8") if daemon_log.exists() else "missing"
                self.assertEqual(len(manager_pids), 1, (results, listing, pid_value, process_state, log_text))
                self.assertEqual(
                    pid_value,
                    str(manager_pids[0]),
                )
            finally:
                for proc in starts:
                    if proc.poll() is None:
                        proc.terminate()
                    proc.wait(timeout=5)
                listing = subprocess.run(
                    ["ps", "-ww", "-eo", "pid=,args="], capture_output=True, text=True,
                ).stdout
                for line in listing.splitlines():
                    if str(manager) in line:
                        try:
                            os.kill(int(line.split(None, 1)[0]), 15)
                        except (OSError, ValueError):
                            pass


if __name__ == "__main__":
    unittest.main()
