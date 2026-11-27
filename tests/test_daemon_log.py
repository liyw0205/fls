import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_ui_route_components import ROOT, isolated_app


def wait_for(predicate, process=None):
    deadline = time.monotonic() + 10
    while not predicate():
        if process is not None and process.poll() is not None:
            raise AssertionError(f"writer exited before becoming ready: {process.returncode}")
        if time.monotonic() >= deadline:
            raise AssertionError("daemon output did not reach the expected state")
        time.sleep(0.02)


def wait_for_closed_log(path):
    from fls_manager.log_access import LogFileBusy, log_file_access

    def closed():
        try:
            with log_file_access(path, exclusive=True):
                return True
        except LogFileBusy:
            return False

    wait_for(closed)


class DaemonLogTests(unittest.TestCase):
    def test_collector_guards_file_until_pipe_eof_without_blocking_restore(self):
        with isolated_app() as (_, base):
            from fls_manager.backup_lease import release_process_lease, try_process_lease
            from fls_manager.daemon_log import start_daemon_log
            from fls_manager.log_access import delete_log_file, file_version

            target = base / "log" / "fls-manager-daemon.log"
            collector = start_daemon_log()
            try:
                payload = b"daemon output\n" * 16000
                collector.stdin.write(payload)
                collector.stdin.flush()
                wait_for(lambda: target.stat().st_size == len(payload))
                self.assertEqual(delete_log_file(target, file_version(target.stat()))[0], "busy")
                restore = try_process_lease(base / ".backup_native_restore.lock")
                try:
                    self.assertIsNotNone(restore, "daemon output permanently blocked directory restore")
                    tail = b"output during directory restore\n"
                    collector.stdin.write(tail)
                    collector.stdin.flush()
                    payload += tail
                    wait_for(lambda: target.stat().st_size == len(payload))
                finally:
                    release_process_lease(restore)
            finally:
                collector.stdin.close()
                collector.wait(timeout=10)
            self.assertEqual(collector.returncode, 0)
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(delete_log_file(target, file_version(target.stat()))[0], "deleted")

    def test_owner_exit_and_kill_keep_inherited_writer_and_lease_alive(self):
        for exit_mode in ("exit", "kill"):
            with self.subTest(exit_mode=exit_mode), isolated_app() as (_, base):
                from fls_manager.log_access import delete_log_file, file_version

                ready, release = base / "writer-ready", base / "writer-release"
                target = base / "log" / "fls-manager-daemon.log"
                child_code = (
                    "import pathlib,time\n"
                    f"pathlib.Path({str(ready)!r}).touch()\n"
                    f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.02)\n"
                    "print('inherited writer finished',flush=True)\n"
                )
                owner_code = (
                    "import subprocess,sys,time\n"
                    "from fls_manager.daemon_log import redirect_daemon_output\n"
                    "redirect_daemon_output()\n"
                    "print('owner started',flush=True)\n"
                    f"subprocess.Popen([sys.executable,'-u','-c',{child_code!r}])\n"
                    + ("time.sleep(60)\n" if exit_mode == "kill" else "")
                )
                owner = subprocess.Popen(
                    [sys.executable, "-u", "-c", owner_code], cwd=ROOT,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )
                try:
                    wait_for(ready.exists)
                    if exit_mode == "kill":
                        owner.kill()
                    owner.wait(timeout=10)
                    self.assertEqual(delete_log_file(target, file_version(target.stat()))[0], "busy")
                    release.touch()
                    wait_for(lambda: b"inherited writer finished" in target.read_bytes())
                    result = []

                    def delete_after_eof():
                        result[:] = delete_log_file(target, file_version(target.stat()))
                        return result[0] == "deleted"

                    wait_for(delete_after_eof)
                    self.assertFalse(target.exists())
                finally:
                    release.touch()
                    if owner.poll() is None:
                        owner.kill()
                    owner.communicate(timeout=10)

    def test_entrypoint_redirects_bootstrap_stdout_and_stderr(self):
        with isolated_app() as (_, base):
            code = (
                "import runpy,sys\n"
                f"manager=runpy.run_path({str(ROOT / 'fls-manager.py')!r})\n"
                "def bootstrap():\n"
                " print('bootstrap stdout',flush=True)\n"
                " print('bootstrap stderr',file=sys.stderr,flush=True)\n"
                " raise SystemExit(0)\n"
                "manager['main'].__globals__['_bootstrap_deps']=bootstrap\n"
                "sys.argv=['fls-manager.py','--daemon-log']\n"
                "manager['main']()\n"
            )
            result = subprocess.run(
                [sys.executable, "-u", "-c", code], cwd=ROOT,
                capture_output=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            target = base / "log" / "fls-manager-daemon.log"
            wait_for(lambda: target.exists() and b"bootstrap stderr" in target.read_bytes())
            self.assertIn(b"bootstrap stdout", target.read_bytes())
            self.assertNotIn(b"bootstrap stdout", result.stdout)
            self.assertNotIn(b"bootstrap stderr", result.stderr)
            wait_for_closed_log(target)

    def test_control_request_hands_output_to_collector_after_return(self):
        with isolated_app() as (_, base):
            from fls_manager.log_access import delete_log_file, file_version
            from fls_manager.routes.about import helpers

            ready, release = base / "control-ready", base / "control-release"
            target = base / "log" / "fls-manager-daemon.log"
            command = [sys.executable, "-u", "-c", (
                "import pathlib,time\n"
                f"pathlib.Path({str(ready)!r}).touch()\n"
                f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.02)\n"
                "print('detached control finished',flush=True)\n"
            )]
            try:
                with patch.object(helpers.time, "sleep"), \
                        patch.object(helpers, "fls_control_script", return_value=base / "fls.sh"), \
                        patch.object(helpers, "build_fls_control_command", return_value=command):
                    helpers.run_fls_control_later("restart")
                wait_for(ready.exists)
                self.assertEqual(delete_log_file(target, file_version(target.stat()))[0], "busy")
                release.touch()
                wait_for(lambda: b"detached control finished" in target.read_bytes())
                wait_for_closed_log(target)
            finally:
                release.touch()
                if target.exists():
                    wait_for_closed_log(target)

    def test_busy_log_prevents_detached_command_from_starting(self):
        with isolated_app() as (_, base):
            from fls_manager.daemon_log import launch_logged_command
            from fls_manager.log_access import log_file_access

            marker = base / "unexpected-command"
            target = base / "log" / "fls-manager-daemon.log"
            command = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
            with log_file_access(target, exclusive=True), self.assertRaises(RuntimeError):
                launch_logged_command(command)
            self.assertFalse(marker.exists())

    def test_failed_command_spawn_closes_collector_and_releases_log(self):
        with isolated_app() as (_, base):
            from fls_manager import daemon_log
            from fls_manager.log_access import log_file_access

            target = base / "log" / "fls-manager-daemon.log"
            original_popen = subprocess.Popen
            collectors = []

            def spawn(command, **kwargs):
                if collectors:
                    raise OSError("fixture command spawn failure")
                process = original_popen(command, **kwargs)
                collectors.append(process)
                return process

            with patch.object(daemon_log.subprocess, "Popen", side_effect=spawn), \
                    self.assertRaisesRegex(OSError, "fixture command spawn failure"):
                daemon_log.launch_logged_command([sys.executable, "-c", "pass"])
            collectors[0].wait(timeout=10)
            self.assertTrue(collectors[0].stdin.closed)
            with log_file_access(target, exclusive=True):
                pass

    def test_daemon_restore_exception_cannot_open_other_logs(self):
        with isolated_app() as (_, base):
            from fls_manager.log_access import open_daemon_log

            for name in ("other.log", "../outside.log"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    open_daemon_log(base / "log" / name)
            self.assertFalse((base / "log" / "other.log").exists())

    def test_collector_start_timeout_does_not_leave_a_process_or_open_pipe(self):
        with isolated_app():
            from fls_manager import daemon_log

            original_popen = subprocess.Popen
            processes = []

            def unready(command, **kwargs):
                process = original_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
                processes.append(process)
                return process

            with patch.object(daemon_log.subprocess, "Popen", side_effect=unready), \
                    self.assertRaisesRegex(RuntimeError, "failed to become ready"):
                daemon_log.start_daemon_log(timeout=0.05)
            self.assertIsNotNone(processes[0].poll())
            self.assertTrue(processes[0].stdin.closed)
            self.assertTrue(processes[0].stdout.closed)

    def test_redirection_failure_preserves_original_output_and_closes_pipe(self):
        with isolated_app() as (_, base):
            ready = base / "redirection-rollback"
            code = (
                "import os,sys\n"
                "from pathlib import Path\n"
                "from unittest.mock import patch\n"
                "from fls_manager.daemon_log import redirect_daemon_output\n"
                "original=os.dup2\n"
                "count=0\n"
                "def fail_second(*args,**kwargs):\n"
                " global count\n"
                " count+=1\n"
                " if count==2: raise OSError('fixture second descriptor failure')\n"
                " return original(*args,**kwargs)\n"
                "try:\n"
                " with patch('os.dup2',side_effect=fail_second): redirect_daemon_output()\n"
                "except OSError:\n"
                " print('stdout preserved',flush=True)\n"
                " print('stderr preserved',file=sys.stderr,flush=True)\n"
                f" Path({str(ready)!r}).touch()\n"
            )
            result = subprocess.run([sys.executable, "-u", "-c", code], cwd=ROOT, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(ready.exists())
            self.assertIn(b"stdout preserved", result.stdout)
            self.assertIn(b"stderr preserved", result.stderr)
            wait_for_closed_log(base / "log" / "fls-manager-daemon.log")

    def test_control_command_uses_inherited_output_instead_of_reopening_log(self):
        if os.name == "nt":
            self.skipTest("shell control commands are POSIX-specific")
        with isolated_app() as (_, base):
            from fls_manager.routes.about import helpers

            (base / "fls.sh").touch()
            with patch.object(helpers, "systemd_fls_unit", return_value=""):
                for action in ("restart", "stop"):
                    command = helpers.build_fls_control_command(action)
                    self.assertNotIn("fls-manager-daemon.log", command[-1])

    def test_platform_launcher_uses_collector_and_keeps_actual_manager_pid(self):
        with isolated_app() as (_, base):
            from fls_manager.log_access import delete_log_file, file_version

            ready, release = base / "launcher-ready", base / "launcher-release"
            manager = base / "fls-manager.py"
            manager.write_text(
                "import os,subprocess,sys,time\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0,{str(ROOT)!r})\n"
                "if '--daemon-log' in sys.argv:\n"
                " from fls_manager.daemon_log import redirect_daemon_output\n"
                " redirect_daemon_output()\n"
                "print('launcher output',flush=True)\n"
                "print('launcher stderr',file=sys.stderr,flush=True)\n"
                "os.write(1,b'launcher descriptor output\\n')\n"
                "subprocess.run([sys.executable,'-u','-c',\"print('launcher child output',flush=True)\"],check=True)\n"
                f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
                f"while not Path({str(release)!r}).exists(): time.sleep(0.02)\n",
                encoding="utf-8",
            )
            (base / "fls_manager").mkdir()
            target = base / "log" / "fls-manager-daemon.log"
            env = dict(os.environ, FLS_PYTHON=sys.executable, CI="true")
            if os.name == "nt":
                powershell = shutil.which("powershell.exe")
                self.assertIsNotNone(powershell)
                script = str(ROOT / "fls.ps1").replace("'", "''")
                executable = sys.executable.replace("'", "''")
                command = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", (
                    f". '{script}' help; "
                    "function Ensure-PythonEnv { "
                    f"return @{{File='{executable}';Args=@();Text='{executable}'}} }}; "
                    "function Install-BasicDeps($spec) {}; "
                    "function Find-FlsProcesses { return @() }; "
                    "Start-Fls @()"
                )]
            else:
                command = ["sh", str(ROOT / "fls.sh"), "start"]
            try:
                result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                wait_for(ready.exists)
                wait_for(lambda: target.exists() and b"launcher output" in target.read_bytes())
                output = target.read_bytes()
                for expected in (b"launcher stderr", b"launcher descriptor output", b"launcher child output"):
                    self.assertIn(expected, output)
                actual_pid = int(ready.read_text())
                if os.name != "nt":
                    self.assertEqual(int((base / "data" / "fls-manager.pid").read_text()), actual_pid)
                self.assertEqual(delete_log_file(target, file_version(target.stat()))[0], "busy")
            finally:
                release.touch()
                wait_for_closed_log(target)


if __name__ == "__main__":
    unittest.main()
