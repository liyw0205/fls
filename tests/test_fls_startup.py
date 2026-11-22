import errno
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


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
                        timeout=90,
                    )

                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(f"已在运行，PID: {proc.pid}", result.stdout)
                    self.assertEqual(pid_file.read_text(encoding="ascii").strip(), str(proc.pid))
                finally:
                    proc.terminate()
                    proc.wait(timeout=5)

class ManagerInstanceLockTests(unittest.TestCase):
    def test_second_entrypoint_exits_before_app_setup_and_lock_releases_on_exit(self):
        with tempfile.TemporaryDirectory(prefix="fls-manager-lock-") as temp_dir:
            base_dir = Path(temp_dir)
            env = os.environ.copy()
            env.update({
                "FLS_BASE_DIR": str(base_dir),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            })
            ready = base_dir / "lock-ready"
            holder_code = (
                "import pathlib, sys, time; from fls_manager.instance_lock import acquire_instance_lock, release_instance_lock; "
                "fd=acquire_instance_lock(); pathlib.Path(sys.argv[1]).touch(); "
                "time.sleep(60); release_instance_lock(fd) if fd is not None else None"
            )
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_code, str(ready)], cwd=ROOT, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 30
                while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                error = holder.stderr.read().decode("utf-8", errors="replace") if not ready.exists() else ""
                self.assertTrue(ready.exists(), error)

                secret_key = base_dir / "data" / "secret_key.txt"
                duplicate = subprocess.run(
                    [sys.executable, str(ROOT / "fls-manager.py")],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=90,
                )
                self.assertEqual(duplicate.returncode, 0, duplicate.stdout + duplicate.stderr)
                self.assertIn("已有实例正在运行", duplicate.stdout)
                self.assertFalse(secret_key.exists(), "duplicate entrypoint ran app initialization")
            finally:
                if holder.poll() is None:
                    holder.terminate()
                holder.wait(timeout=10)

            from fls_manager import instance_lock

            with patch.object(instance_lock, "DATA_DIR", base_dir / "data"):
                descriptor = instance_lock.acquire_instance_lock()
                self.assertIsNotNone(descriptor)
                instance_lock.release_instance_lock(descriptor)

    @unittest.skipUnless(os.name == "posix", "symlink lock validation is POSIX-specific")
    def test_lock_symlink_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory(prefix="fls-manager-lock-link-") as temp_dir:
            base_dir = Path(temp_dir)
            data_dir = base_dir / "data"
            data_dir.mkdir()
            target = base_dir / "target"
            target.write_text("untouched", encoding="ascii")
            (data_dir / "fls-manager.lock").symlink_to(target)
            env = os.environ.copy()
            env["FLS_BASE_DIR"] = str(base_dir)

            from fls_manager import instance_lock

            with patch.object(instance_lock, "DATA_DIR", data_dir):
                with self.assertRaisesRegex(RuntimeError, "符号链接"):
                    instance_lock.acquire_instance_lock()
            self.assertEqual(target.read_text(encoding="ascii"), "untouched")

    def test_windows_lock_backend_acquires_releases_and_reports_contention(self):
        from fls_manager import instance_lock

        with tempfile.TemporaryDirectory(prefix="fls-manager-windows-lock-") as temp_dir:
            data_dir = Path(temp_dir) / "data"
            calls = []
            windows_lock = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2)

            def record_locking(fd, operation, length):
                calls.append((os.lseek(fd, 0, os.SEEK_CUR), operation, length))

            windows_lock.locking = Mock(side_effect=record_locking)
            with patch.object(instance_lock, "DATA_DIR", data_dir), \
                    patch.object(instance_lock.os, "name", "nt"), \
                    patch.dict(sys.modules, {"msvcrt": windows_lock}):
                descriptor = instance_lock.acquire_instance_lock()
                self.assertIsNotNone(descriptor)
                instance_lock.release_instance_lock(descriptor)

            self.assertEqual((data_dir / "fls-manager.lock").read_bytes(), b"\0")
            self.assertEqual(calls, [(0, 1, 1), (0, 2, 1)])

            windows_lock.locking = Mock(side_effect=OSError(errno.EACCES, "locked"))
            with patch.object(instance_lock, "DATA_DIR", data_dir), \
                    patch.object(instance_lock.os, "name", "nt"), \
                    patch.dict(sys.modules, {"msvcrt": windows_lock}), \
                    patch.object(instance_lock.os, "close", wraps=os.close) as close:
                self.assertIsNone(instance_lock.acquire_instance_lock())
                close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
