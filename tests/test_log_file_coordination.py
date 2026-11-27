import os
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app
from tests import test_directory_process as process_tests


class LogFileCoordinationTests(unittest.TestCase):
    def test_exclusive_cleanup_returns_retryable_busy_for_log_readers(self):
        with isolated_app() as (app, base):
            from fls_manager.log_access import log_file_access

            target = base / "log" / "busy.log"
            target.write_bytes(b"original")
            with log_file_access(target, exclusive=True):
                for endpoint in ("/api/logfile/busy.log?format=json", "/api/logfile/busy.log/download"):
                    with self.subTest(endpoint=endpoint):
                        response = app.test_client().get(endpoint, headers={"X-Token": TOKEN})
                        self.assertEqual(response.status_code, 503)
                        self.assertEqual(response.json["code"], "log_file_busy")
                        self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(target.read_bytes(), b"original")

    def test_restore_owner_can_write_dependency_log_without_releasing_exclusion(self):
        with isolated_app() as (_, base):
            from fls_manager.backup_lease import release_process_lease, try_process_lease
            from fls_manager.routes.backup import _common as backup

            requirements = base / "requirements.txt"
            requirements.write_text("fixture\n", encoding="utf-8")
            self.assertTrue(backup.try_begin_restore_activity())

            def install(*args, **kwargs):
                stream = kwargs["stdout"]
                self.assertIsNone(try_process_lease(base / ".backup_native_restore.lock", shared=True))
                self.assertIsNone(try_process_lease(stream.log_lease_path, create=False))
                stream.write(b"fixture output\n")
                return SimpleNamespace(returncode=0)

            try:
                with patch.object(backup.subprocess, "run", side_effect=install) as run:
                    success, log = backup.install_dependencies(requirements)
                self.assertTrue(success)
                self.assertEqual(run.call_count, 1)
                self.assertIn("fixture output", Path(log).read_text(encoding="utf-8"))
            finally:
                backup.end_restore_activity()
            lease = try_process_lease(base / ".backup_native_restore.lock")
            self.assertIsNotNone(lease)
            release_process_lease(lease)

    def test_open_failure_releases_log_and_restore_leases(self):
        with isolated_app() as (_, base):
            from fls_manager.backup_lease import release_process_lease, try_process_lease
            from fls_manager.log_access import log_file_access, open_log_file

            target = base / "log" / "missing.log"
            with self.assertRaises(FileNotFoundError):
                open_log_file(target, "rb")
            with log_file_access(target, exclusive=True):
                pass
            lease = try_process_lease(base / ".backup_native_restore.lock")
            self.assertIsNotNone(lease)
            release_process_lease(lease)

    def test_independent_writer_blocks_both_web_deletes_but_not_other_files(self):
        with isolated_app() as (app, base):
            from fls_manager.log_access import delete_log_file, file_version
            from fls_manager.routes.logs._common import load_log_groups, log_group_version

            target = base / "log" / "active.log"
            other = base / "log" / "other.log"
            target.write_text("===== 启动任务: Active =====\n", encoding="utf-8")
            other.write_text("other\n", encoding="utf-8")
            code = (
                "import sys\n"
                "from fls_manager.log_access import open_log_file\n"
                "with open_log_file(sys.argv[1], 'ab', buffering=0) as stream:\n"
                " print('ready', flush=True)\n"
                " sys.stdin.readline()\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", code, str(target)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(process.stdout.readline(), "ready\n")
                version = file_version(target.lstat())
                client = app.test_client()
                headers = {"X-Token": TOKEN}
                single = client.post("/logfile/delete/active.log", data={"version": version}, headers=headers)
                groups = load_log_groups()
                grouped = client.post("/api/logs/groups/delete", json={
                    "groups": ["Active"],
                    "group_versions": {"Active": log_group_version(groups["Active"])},
                }, headers=headers)
                self.assertEqual(single.status_code, 409)
                self.assertFalse(grouped.json["ok"])
                self.assertEqual(grouped.json["results"][0]["deleted"], 0)
                self.assertEqual(client.get("/api/logfile/active.log/download", headers=headers).status_code, 200)
                self.assertEqual(delete_log_file(other, file_version(other.lstat()))[0], "deleted")
                self.assertEqual(target.read_text(encoding="utf-8"), "===== 启动任务: Active =====\n")
            finally:
                output, error = process.communicate("done\n", timeout=10)
            self.assertEqual(process.returncode, 0, output + error)
            self.assertEqual(delete_log_file(target, version)[0], "deleted")

    def test_failed_close_keeps_lease_until_real_handle_closes(self):
        with isolated_app() as (_, base):
            from fls_manager.log_access import delete_log_file, file_version, open_log_file

            target = base / "log" / "close.log"
            stream = open_log_file(target, "ab", buffering=0)
            stream.write(b"original\n")
            real = stream.stream

            class FailedClose:
                first = True

                def __getattr__(self, name):
                    return getattr(real, name)

                def close(self):
                    if self.first:
                        self.first = False
                        raise OSError("fixture close failure")
                    real.close()

            stream.stream = FailedClose()
            try:
                with self.assertRaises(OSError):
                    stream.close()
                version = file_version(target.lstat())
                self.assertEqual(delete_log_file(target, version)[0], "busy")
                self.assertFalse(stream.closed)
            finally:
                stream.close()
            self.assertEqual(delete_log_file(target, version)[0], "deleted")

    def test_supervisor_holds_log_after_parent_stream_closes(self):
        with isolated_app() as (_, base), tempfile.TemporaryDirectory(prefix="fls-log-command-") as workspace:
            from fls_manager.directory_process import DirectoryProcess
            from fls_manager.log_access import delete_log_file, file_version, open_log_file

            target = base / "log" / "supervised.log"
            stream = open_log_file(target, "ab", buffering=0)
            stream.write(b"original\n")
            process = None
            try:
                process = DirectoryProcess(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    workspace=workspace, cwd=base, env=os.environ.copy(),
                    lease_path=base / ".backup_native_restore.lock",
                    stdout=stream, stderr=subprocess.STDOUT,
                )
                process.start()
                stream.close()
                version = file_version(target.lstat())
                self.assertEqual(delete_log_file(target, version)[0], "busy")
            finally:
                if process is not None:
                    process.stop(timeout=15)
                    process.wait(timeout=15)
                stream.close()
            self.assertEqual(delete_log_file(target, version)[0], "deleted")

    def test_owner_hard_exit_retains_log_lease_until_entire_command_tree_exits(self):
        with isolated_app() as (_, base):
            from fls_manager.backup_lease import release_process_lease, try_process_lease

            marker = base / "children.json"
            target = base / "log" / "orphan.log"
            command = (
                "import json,os,pathlib,signal,subprocess,sys,time\n"
                + ("signal.signal(signal.SIGTERM,signal.SIG_IGN)\n" if os.name != "nt" else "")
                + "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                + f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]))\n"
                + "time.sleep(60)\n"
            )
            owner = (
                "import json,pathlib,sys,tempfile,time\n"
                "from fls_manager.directory_process import DirectoryProcess\n"
                "from fls_manager.log_access import open_log_file\n"
                f"stream=open_log_file({str(target)!r},'ab',buffering=0)\n"
                "workspace=tempfile.TemporaryDirectory(prefix='fls-orphan-log-')\n"
                f"process=DirectoryProcess([sys.executable,'-c',{command!r}],workspace=workspace.name,"
                f"cwd={str(base)!r},env=None,lease_path={str(base / '.backup_native_restore.lock')!r},"
                "stdout=stream,stderr=-2,stop_grace_seconds=1)\n"
                "process.start()\n"
                f"marker=pathlib.Path({str(marker)!r})\n"
                "deadline=time.monotonic()+10\n"
                "while not marker.exists() and time.monotonic()<deadline:time.sleep(0.01)\n"
                "print('OWNER_READY '+json.dumps({'children':json.loads(marker.read_text()),"
                "'lock':str(stream.log_lease_path),'supervisor':process.pid}),flush=True)\n"
                "sys.stdin.readline()\n"
            )
            helper = process_tests.DirectoryProcessTests()
            with helper.paused_owner(owner) as (process, ready):
                self.assertIsNone(try_process_lease(ready["lock"], create=False))
                process.kill()
                process.wait(timeout=10)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    lease = try_process_lease(ready["lock"], create=False)
                    if lease is not None:
                        try:
                            for pid in ready["children"]:
                                self.assertTrue(process_tests.process_exited(pid), "log lease released before writer exited")
                        finally:
                            release_process_lease(lease)
                        break
                    time.sleep(0.01)
                else:
                    self.fail("orphaned supervisor did not release log lease")
                for pid in ready["children"] + [ready["supervisor"]]:
                    helper.assert_exited(pid)

    def test_pinned_delete_rejects_parent_directory_replacement(self):
        with isolated_app() as (_, base):
            from fls_manager.log_access import log_file_access

            target = base / "log" / "original.log"
            target.write_bytes(b"original")
            with log_file_access(target, exclusive=True) as access:
                old = base / "old-logs"
                try:
                    target.parent.rename(old)
                except PermissionError:
                    self.skipTest("platform pins the open log directory")
                target.parent.mkdir()
                target.write_bytes(b"replacement")
                with self.assertRaises(ValueError):
                    access.unlink()
                self.assertEqual((old / target.name).read_bytes(), b"original")
                self.assertEqual(target.read_bytes(), b"replacement")

    def test_link_and_nonregular_objects_are_never_opened_or_deleted(self):
        with isolated_app() as (app, base):
            from fls_manager.log_access import delete_log_file, open_log_file

            outside = base / "outside.log"
            outside.write_bytes(b"private")
            linked = base / "log" / "linked.log"
            try:
                linked.symlink_to(outside)
            except OSError:
                self.skipTest("symlink creation unavailable")
            for mode in ("rb", "ab"):
                with self.subTest(mode=mode), self.assertRaises((OSError, ValueError)):
                    open_log_file(linked, mode)
            self.assertEqual(delete_log_file(linked, "0" * 64)[0], "failed")
            self.assertTrue(linked.is_symlink())
            self.assertEqual(outside.read_bytes(), b"private")
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            self.assertEqual(client.get("/api/logfile/linked.log/download", headers=headers).status_code, 404)
            listing = client.get("/api/logs", headers=headers)
            self.assertEqual(listing.status_code, 200)
            self.assertNotIn("linked.log", [item["filename"] for item in listing.json["items"]])
            if hasattr(os, "mkfifo"):
                fifo = base / "log" / "pipe.log"
                os.mkfifo(fifo)
                with self.assertRaises((OSError, ValueError)):
                    open_log_file(fifo, "rb")

    def test_automatic_cleanup_skips_open_log_then_deletes_after_close(self):
        with isolated_app() as (_, base):
            from fls_manager import logs
            from fls_manager.log_access import open_log_file

            target = base / "log" / "active.log"
            other = base / "log" / "old.log"
            other.write_bytes(b"old")
            with patch.object(logs, "load_config", return_value={"log_keep_per_task": 0, "log_max_size_mb": 0}):
                with open_log_file(target, "ab", buffering=0) as stream:
                    stream.write(b"active")
                    logs.cleanup_logs()
                    self.assertEqual(target.read_bytes(), b"active")
                    self.assertFalse(other.exists())
                logs.cleanup_logs()
                self.assertFalse(target.exists())

    def test_changed_file_is_preserved_and_pinned_read_holds_lease(self):
        with isolated_app() as (_, base):
            from fls_manager.log_access import delete_log_file, file_version, open_log_file

            target = base / "log" / "read.log"
            target.write_bytes(b"original")
            version = file_version(target.lstat())
            with open_log_file(target, "rb") as stream:
                self.assertEqual(delete_log_file(target, version)[0], "busy")
                self.assertEqual(stream.read(), b"original")
            target.write_bytes(b"replacement")
            self.assertEqual(delete_log_file(target, version)[0], "conflict")
            self.assertEqual(target.read_bytes(), b"replacement")


if __name__ == "__main__":
    unittest.main()
