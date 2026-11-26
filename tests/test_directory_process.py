import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_backup_dependency_process import process_exited
from tests.test_ui_route_components import TOKEN, isolated_app


class DirectoryProcessTests(unittest.TestCase):
    def make_process(self, base, command, **overrides):
        from fls_manager.directory_process import DirectoryProcess

        options = {
            "workspace": base / "scripts", "cwd": base / "scripts", "env": os.environ.copy(),
            "lease_path": base / ".backup_native_restore.lock",
        }
        options.update(overrides)
        return DirectoryProcess(command, **options)

    def assert_lease_available(self, base):
        from fls_manager.backup_lease import release_process_lease, try_process_lease

        fd = try_process_lease(base / ".backup_native_restore.lock")
        try:
            self.assertIsNotNone(fd)
        finally:
            release_process_lease(fd)

    def assert_exited(self, pid):
        deadline = time.monotonic() + 10
        while not process_exited(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(process_exited(pid), f"process {pid} did not exit")

    def test_start_gate_and_constructor_stop_never_execute_the_command(self):
        with isolated_app() as (_, base):
            marker = base / "started"
            process = self.make_process(base, [
                sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).touch()",
            ])
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.15)
                self.assertFalse(marker.exists())
            finally:
                process.stop()
                process.stderr.close()
            self.assertFalse(marker.exists())
            self.assert_exited(process.pid)
            self.assert_lease_available(base)

    def test_command_exit_cleans_descendants_before_result_and_preserves_stderr(self):
        with isolated_app() as (_, base):
            marker = base / "children.json"
            command = (
                "import json,os,pathlib,subprocess,sys\n"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]))\n"
                "sys.stderr.write('Receiving objects: 100% (2/2)\\n');sys.stderr.flush()\n"
            )
            process = self.make_process(base, [sys.executable, "-c", command])
            try:
                process.start()
                self.assertEqual(process.wait(timeout=10), 0)
                for pid in json.loads(marker.read_text()):
                    self.assertTrue(process_exited(pid))
                self.assertIn(b"Receiving objects", process.stderr.read())
                self.assert_lease_available(base)
                self.assertNotIn("command", process.result_path.read_text())
            finally:
                process.stop()
                process.stderr.close()

    def test_stop_cleans_command_and_descendants_and_does_not_execute_twice(self):
        with isolated_app() as (_, base):
            marker = base / "children.json"
            command = (
                "import json,os,pathlib,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]))\n"
                "time.sleep(60)\n"
            )
            process = self.make_process(base, [sys.executable, "-c", command])
            try:
                process.start()
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists())
                from fls_manager.backup_lease import try_process_lease

                self.assertIsNone(try_process_lease(base / ".backup_native_restore.lock"))
                with self.assertRaises(RuntimeError):
                    process.start()
                process.stop()
                process.stop()
                for pid in json.loads(marker.read_text()):
                    self.assertTrue(process_exited(pid))
                self.assert_lease_available(base)
            finally:
                process.stop()
                process.stderr.close()

    def test_spawn_failure_releases_supervisor_resources_and_lease(self):
        with isolated_app() as (_, base):
            process = self.make_process(base, [str(base / "missing-command")])
            try:
                try:
                    process.start()
                except RuntimeError:
                    pass
                else:
                    self.assertNotEqual(process.wait(timeout=10), 0)
            finally:
                process.stop()
                process.stderr.close()
            self.assert_exited(process.pid)
            self.assert_lease_available(base)

    def test_native_repo_route_runs_real_git_clone_through_supervisor(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.scripts import native_repo_pull
            from tests.test_native_script_repo_pull_api import NativeScriptRepoPullApiTests

            helper = NativeScriptRepoPullApiTests()
            source = helper._source_repo(base)
            client = app.test_client()

            def command(_url, destination, _proxy_id, **_options):
                return ["git", "clone", "--no-local", "--progress", str(source), str(destination)]

            with patch.object(native_repo_pull, "build_repo_pull_command", side_effect=command):
                accepted = helper._submit(client)
                self.assertEqual(accepted.status_code, 202, accepted.json)
                job, _, _ = helper._wait(client, accepted.json["job"]["id"], {"complete", "failed", "unknown"}, timeout=10)
            self.assertEqual(job["status"], "complete", job)
            self.assertEqual((base / "scripts" / "repo" / "README.md").read_text(), "fixture repository\n")
            self.assertTrue((base / "scripts" / "repo" / ".git").is_dir())
            self.assertEqual(list((base / "scripts").glob(".fls-repo-*.tmp")), [])

    def test_stop_timeout_keeps_supervisor_lease_until_tree_cleanup_finishes(self):
        with isolated_app() as (_, base):
            from fls_manager import directory_process
            from fls_manager.backup_lease import release_process_lease, try_process_lease

            entered = base / "cleanup-entered"
            release = base / "release-cleanup"
            module_path = str(Path(directory_process.__file__).resolve())
            supervisor = (
                "import pathlib,sys,time\n"
                "from fls_manager import directory_process as guard\n"
                "if sys.platform=='win32':\n"
                " original=guard.WindowsJob.terminate\n"
                "else:\n original=guard._posix_group_running\n"
                "def paused(*args,**kwargs):\n"
                f" pathlib.Path({str(entered)!r}).touch()\n"
                f" while not pathlib.Path({str(release)!r}).exists():time.sleep(0.01)\n"
                " return original(*args,**kwargs)\n"
                "if sys.platform=='win32':guard.WindowsJob.terminate=paused\n"
                "else:guard._posix_group_running=paused\n"
                "guard._supervise(pathlib.Path(sys.argv[1]))\n"
            )
            popen = subprocess.Popen

            def spawn(command, **kwargs):
                if len(command) == 4 and command[2] == module_path:
                    command = [sys.executable, "-u", "-c", supervisor, command[3]]
                    env = dict(kwargs["env"])
                    env["PYTHONPATH"] = str(Path(module_path).parents[1])
                    kwargs["env"] = env
                return popen(command, **kwargs)

            with patch.object(directory_process.subprocess, "Popen", side_effect=spawn):
                process = self.make_process(base, [sys.executable, "-c", "import time;time.sleep(60)"])
            try:
                process.start()
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.stop(timeout=0.1)
                deadline = time.monotonic() + 5
                while not entered.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(entered.exists())
                self.assertIsNone(process._process.poll())
                fd = try_process_lease(base / ".backup_native_restore.lock")
                try:
                    self.assertIsNone(fd, "supervisor released the lease before cleanup")
                finally:
                    release_process_lease(fd)
            finally:
                release.touch()
                process.stop()
                process.stderr.close()
            self.assert_lease_available(base)

    @unittest.skipUnless(os.name == "posix", "descriptor-based script staging")
    def test_command_retains_pinned_directory_after_request_owner_closes_its_fd(self):
        with isolated_app() as (_, base):
            parent_fd = os.open(base / "scripts", os.O_RDONLY | os.O_DIRECTORY)
            process = None
            release = base / "release-command"
            try:
                process = self.make_process(base, [
                    sys.executable, "-c",
                    "from pathlib import Path;import time\n"
                    f"while not Path({str(release)!r}).exists():time.sleep(0.01)\n"
                    f"Path('/proc/self/fd/{parent_fd}/pinned').write_bytes(b'ok')",
                ], pass_fds=(parent_fd,))
                process.start()
                os.close(parent_fd)
                parent_fd = None
                release.touch()
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertEqual((base / "scripts" / "pinned").read_bytes(), b"ok")
            finally:
                if process is not None:
                    process.stop()
                    process.stderr.close()
                if parent_fd is not None:
                    os.close(parent_fd)
            self.assert_lease_available(base)

    @contextlib.contextmanager
    def paused_owner(self, code):
        with tempfile.TemporaryFile(mode="w+b") as output, tempfile.TemporaryFile(mode="w+b") as errors:
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", code], stdin=subprocess.PIPE, stdout=output, stderr=errors,
                text=True, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            )
            try:
                deadline = time.monotonic() + 15
                ready = None
                while process.poll() is None and time.monotonic() < deadline:
                    output.seek(0)
                    for line in output.read().splitlines():
                        if line.startswith(b"OWNER_READY "):
                            ready = json.loads(line[len(b"OWNER_READY "):])
                            break
                    if ready is not None:
                        break
                    time.sleep(0.01)
                errors.seek(0)
                self.assertIsNotNone(ready, errors.read().decode(errors="replace"))
                yield process, ready
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)

    def test_panel_owner_hard_exit_reaps_git_tree_before_restore_lease_becomes_available(self):
        with isolated_app() as (_, base):
            marker = base / "children.json"
            command = (
                "import json,os,pathlib,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]))\n"
                "sys.stderr.write('Receiving objects: 50% (1/2)\\r');sys.stderr.flush()\n"
                "time.sleep(60)\n"
            )
            owner = (
                "import json,os,pathlib,sys,time\n"
                "from fls_manager.app import create_app\n"
                "from fls_manager.routes.scripts import native_repo_pull as repo\n"
                "processes=[]\noriginal=repo.DirectoryProcess\n"
                "def spawn(*args,**kwargs):\n"
                " process=original(*args,**kwargs)\n processes.append(process)\n return process\n"
                "repo.DirectoryProcess=spawn\n"
                f"repo.build_repo_pull_command=lambda *args,**kwargs:[sys.executable,'-c',{command!r}]\n"
                "app=create_app()\n"
                "response=app.test_client().post('/api/scripts/repo-pull',json={\n"
                " 'request_id':'a'*32,'url':'https://example.invalid/repo.git','path':'repo'},\n"
                f" headers={{'X-Token':{TOKEN!r}}})\n"
                "assert response.status_code==202,response.json\n"
                "deadline=time.monotonic()+10\n"
                f"while not pathlib.Path({str(marker)!r}).exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
                f"children=json.loads(pathlib.Path({str(marker)!r}).read_text())\n"
                "print('OWNER_READY '+json.dumps({'supervisor':processes[0].pid,'children':children}),flush=True)\n"
                "sys.stdin.readline()\nos._exit(23)\n"
            )
            from fls_manager.backup_lease import release_process_lease, try_process_lease

            with self.paused_owner(owner) as (process, ready):
                try:
                    self.assertIsNone(try_process_lease(base / ".backup_native_restore.lock"))
                    self.assertTrue(all(not process_exited(pid) for pid in ready["children"]))
                    process.kill()
                    process.wait(timeout=10)
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        fd = try_process_lease(base / ".backup_native_restore.lock")
                        if fd is not None:
                            try:
                                for pid in ready["children"]:
                                    self.assertTrue(process_exited(pid), "restore lease released before the Git tree exited")
                            finally:
                                release_process_lease(fd)
                            break
                        time.sleep(0.01)
                    else:
                        self.fail("supervisor did not finish Git cleanup")
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)
                    for pid in ready["children"] + [ready["supervisor"]]:
                        self.assert_exited(pid)
            self.assertFalse((base / "scripts" / "repo").exists())


if __name__ == "__main__":
    unittest.main()
