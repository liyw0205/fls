import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN
from tests.test_notify_action_recovery import isolated_app
from tests import test_native_backup_api as native_tests


class BackupActivityLeaseTests(unittest.TestCase):
    @contextlib.contextmanager
    def holder(self, mode, *, hard_exit=False):
        setup, cleanup = {
            "backup": ("backup.begin_backup_activity()", "backup.end_backup_activity()"),
            "legacy": ("assert backup.try_begin_restore_activity()", "backup.end_restore_activity()"),
            "native": (
                "fd=api._try_process_lease(api.RESTORE_PROCESS_LOCK_PATH)\nassert fd is not None\n"
                "api._mark_restore_lease(fd,'a'*32,'restore')",
                "api._release_process_lease(fd)",
            ),
        }[mode]
        script = (
            "import os,sys\n"
            "from fls_manager.routes.backup import _common as backup, native_api as api\n"
            + setup + "\nprint('ready',flush=True)\nsys.stdin.readline()\n"
            + ("os._exit(23)" if hard_exit else cleanup) + "\n"
        )
        with tempfile.TemporaryFile(mode="w+b") as output, tempfile.TemporaryFile(mode="w+b") as errors:
            process = subprocess.Popen(
                [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=output, stderr=errors,
                text=True,
            )
            try:
                deadline = time.monotonic() + 15
                while process.poll() is None and time.monotonic() < deadline:
                    output.seek(0)
                    if b"ready\n" in output.read().replace(b"\r\n", b"\n"):
                        break
                    time.sleep(0.01)
                else:
                    errors.seek(0)
                    self.fail("activity owner did not start: " + errors.read().decode(errors="replace"))
                yield process
                if process.poll() is None:
                    process.communicate("\n", timeout=15)
                self.assertEqual(process.returncode, 23 if hard_exit else 0)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)

    def test_shared_backups_exclude_every_restore_entry_across_workers(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup, native_api

            helper = native_tests.NativeBackupApiTests()
            client = app.test_client()
            prepared = helper.submit_preflight(client, "b" * 32)
            self.assertEqual(prepared.json["preflight"]["status"], "ready")
            with self.holder("backup"):
                self.assertFalse(backup.restore_activity_active())
                backup.begin_backup_activity()
                try:
                    self.assertEqual(backup.ACTIVE_BACKUP_WORKERS, 1)
                    self.assertFalse(backup.try_begin_restore_activity())
                    rejected = helper.submit_restore(client, "c" * 32, "b" * 32,
                                                     prepared.json["preflight"]["archive_sha256"])
                    self.assertEqual(rejected.status_code, 409, rejected.json)
                    self.assertEqual(rejected.json["code"], "restore_busy")
                    recovered = client.post(
                        "/api/backup/native/restores/" + "c" * 32 + "/recover-directories",
                        json={"confirm": True}, headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(recovered.status_code, 409)
                    legacy = client.post("/api/backup/import", data={
                        "file": (io.BytesIO(helper.archive()), "full.tar.gz"), "restore_items": ["scripts"],
                    }, headers={"X-Token": TOKEN})
                    self.assertEqual(legacy.status_code, 409, legacy.json)
                    self.assertFalse((backup.SCRIPT_DIR / "example.py").exists())
                finally:
                    backup.end_backup_activity()
                self.assertFalse(backup.try_begin_restore_activity())
                self.assertIsNone(native_api._try_process_lease(native_api.RESTORE_PROCESS_LOCK_PATH))
            self.assertTrue(backup.try_begin_restore_activity())
            backup.end_restore_activity()

    def test_remote_restore_blocks_backup_admission_and_legacy_reload(self):
        for mode in ("legacy", "native"):
            with self.subTest(mode=mode), isolated_app() as (app, _):
                from fls_manager.routes.backup import _common as backup

                with self.holder(mode), patch.object(backup, "persist_backup_job") as persist:
                    self.assertFalse(backup.RESTORE_ACTIVE)
                    self.assertTrue(backup.restore_activity_active())
                    with self.assertRaises(backup.BackupJobStartError):
                        backup.start_backup_job(["data"])
                    persist.assert_not_called()
                    self.assertEqual(backup.ACTIVE_BACKUP_WORKERS, 0)
                    self.assertFalse(backup.try_begin_restore_activity())
                    response = app.test_client().post(
                        "/api/backup/restore/" + "a" * 32 + "/reload", headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(response.status_code, 409, response.json)
                self.assertFalse(backup.restore_activity_active())

    def test_hard_exit_releases_shared_and_exclusive_leases(self):
        for mode in ("backup", "legacy", "native"):
            with self.subTest(mode=mode), isolated_app():
                from fls_manager.routes.backup import _common as backup

                with self.holder(mode, hard_exit=True) as process:
                    self.assertFalse(backup.try_begin_restore_activity())
                    process.stdin.write("\n")
                    process.stdin.flush()
                    process.wait(timeout=10)
                    self.assertTrue(backup.try_begin_restore_activity())
                    backup.end_restore_activity()

    def test_queued_backup_holds_shared_lease_until_worker_finishes_and_failures_release_it(self):
        with isolated_app():
            from fls_manager.routes.backup import _common as backup
            from fls_manager.backup_lease import try_process_lease, release_process_lease

            threads = []
            with patch.object(threading.Thread, "start", autospec=True, side_effect=threads.append), \
                    patch.object(backup, "_create_backup_worker"):
                backup.start_backup_job(["data"])
                backup.start_backup_job(["scripts"])
                self.assertEqual(backup.ACTIVE_BACKUP_WORKERS, 2)
                self.assertIsNone(try_process_lease(backup.BACKUP_ACTIVITY_LOCK_PATH))
                threads[0].run()
                self.assertEqual(backup.ACTIVE_BACKUP_WORKERS, 1)
                self.assertIsNone(try_process_lease(backup.BACKUP_ACTIVITY_LOCK_PATH))
                threads[1].run()
            for target in ("persist_backup_job", "thread_start"):
                with self.subTest(failure=target):
                    mock = patch.object(backup, target, side_effect=OSError("unavailable")) if target != "thread_start" else \
                        patch.object(threading.Thread, "start", side_effect=OSError("unavailable"))
                    with mock, self.assertRaises(backup.BackupJobStartError):
                        backup.start_backup_job(["data"])
                    self.assertEqual(backup.ACTIVE_BACKUP_WORKERS, 0)
                    lease = try_process_lease(backup.BACKUP_ACTIVITY_LOCK_PATH)
                    self.assertIsNotNone(lease)
                    release_process_lease(lease)

    def test_pending_directory_evidence_blocks_legacy_restore_and_backups_without_cleanup(self):
        with isolated_app() as (app, base):
            from fls_manager import backup_commit
            from fls_manager.routes.backup import _common as backup

            directory = base / backup_commit.JOURNAL_NAME
            directory.mkdir()
            journal = directory / ("a" * 32 + ".json")
            journal.write_text("{broken", encoding="ascii")
            for begin in (backup.begin_backup_activity, lambda: backup.start_backup_job(["data"])):
                with self.assertRaises(backup.BackupJobStartError):
                    begin()
            self.assertFalse(backup.try_begin_restore_activity())
            self.assertEqual(backup.ACTIVE_BACKUP_WORKERS, 0)
            self.assertIsNone(backup.BACKUP_ACTIVITY_LEASE)
            self.assertIsNone(backup.RESTORE_ACTIVITY_LEASE)
            helper = native_tests.NativeBackupApiTests()
            response = app.test_client().post("/api/backup/import", data={
                "file": (io.BytesIO(helper.archive()), "full.tar.gz"), "restore_items": ["scripts"],
            }, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 409, response.json)
            self.assertEqual(journal.read_bytes(), b"{broken")
            self.assertFalse((backup.SCRIPT_DIR / "example.py").exists())

    def test_shared_backup_does_not_revive_stale_native_restore_owner(self):
        with isolated_app():
            from fls_manager.routes.backup import _common as backup, native_api

            fd = native_api._try_process_lease(native_api.RESTORE_PROCESS_LOCK_PATH)
            native_api._mark_restore_lease(fd, "a" * 32, "restore")
            native_api._release_process_lease(fd)
            with self.holder("backup"):
                self.assertFalse(native_api._restore_lease_matches({"request_id": "a" * 32}, "restore"))
            with self.holder("legacy"):
                self.assertFalse(native_api._restore_lease_matches({"request_id": "a" * 32}, "restore"))

    def test_legacy_get_does_not_mark_another_worker_commit_unknown(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore

            request_id = "d" * 32
            script = (
                "import io,json,sys\n"
                "from unittest.mock import patch\n"
                "from fls_manager.app import create_app\n"
                "from fls_manager.routes.backup import restore\n"
                "from tests.test_native_backup_api import NativeBackupApiTests\n"
                "commit=restore._commit_restore_directories\n"
                "def paused(entries):\n"
                " print('ready',flush=True)\n sys.stdin.readline()\n commit(entries)\n"
                "with patch.object(restore,'_commit_restore_directories',side_effect=paused):\n"
                " response=create_app().test_client().post('/api/backup/import',data={\n"
                "  'file':(io.BytesIO(NativeBackupApiTests().archive()),'full.tar.gz'),\n"
                f"  'restore_items':['scripts'],'restore_request_id':{request_id!r}\n"
                f" }},headers={{'X-Token':{TOKEN!r}}})\n"
                " assert response.status_code==200,response.get_json()\n"
            )
            with tempfile.TemporaryFile(mode="w+b") as output, tempfile.TemporaryFile(mode="w+b") as errors:
                process = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=output,
                                           stderr=errors, text=True)
                try:
                    deadline = time.monotonic() + 15
                    while process.poll() is None and time.monotonic() < deadline:
                        output.seek(0)
                        if b"ready\n" in output.read().replace(b"\r\n", b"\n"):
                            break
                        time.sleep(0.01)
                    else:
                        errors.seek(0)
                        self.fail(errors.read().decode(errors="replace"))
                    receipt = restore._restore_request_path(request_id)
                    before = receipt.read_bytes()
                    client = app.test_client()
                    for url in ("/api/backup/restore/" + request_id, "/api/backup/restore-history"):
                        response = client.get(url, headers={"X-Token": TOKEN})
                        self.assertEqual(response.status_code, 409 if "/restore/" in url else 200, response.json)
                        row = response.json if "state" in response.json else response.json["items"][0]
                        self.assertEqual(row["state"], "committing")
                        self.assertEqual(receipt.read_bytes(), before)
                    process.communicate("\n", timeout=15)
                    errors.seek(0)
                    self.assertEqual(process.returncode, 0, errors.read().decode(errors="replace"))
                    self.assertEqual((base / "scripts/example.py").read_bytes(), b"print('ok')\n")
                    self.assertEqual(json.loads(receipt.read_text())["state"], "restored")
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=10)

    def test_native_followups_do_not_claim_receipts_while_another_worker_backs_up(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import native_api

            helper = native_tests.NativeBackupApiTests()
            client = app.test_client()
            prepared = helper.submit_preflight(client, "b" * 32)
            with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()):
                restored = helper.submit_restore(client, "c" * 32, "b" * 32,
                                                prepared.json["preflight"]["archive_sha256"], restore_dependencies=True)
            self.assertEqual(restored.status_code, 200, restored.json)
            path = native_api._native_restore_path("c" * 32)
            before = path.read_bytes()
            with self.holder("backup"), patch("fls_manager.scheduler.reload_scheduler") as reload_scheduler, \
                    patch.object(native_api, "DependencyProcess") as dependency_process:
                for route in ("scheduler-reload", "dependencies"):
                    response = client.post("/api/backup/native/restores/" + "c" * 32 + "/" + route, json={
                        "operation_id": "d" * 32, "confirm_retry": False,
                    }, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 409, response.json)
                    self.assertEqual(response.json["code"], "restore_busy")
                    self.assertEqual(path.read_bytes(), before)
                reload_scheduler.assert_not_called()
                dependency_process.assert_not_called()

    def test_legacy_dependency_install_keeps_exclusive_lease_and_releases_after_response(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup, restore, native_api

            def install(_):
                self.assertTrue(backup.RESTORE_ACTIVE)
                self.assertIsNone(native_api._try_process_lease(native_api.RESTORE_PROCESS_LOCK_PATH, shared=True))
                with self.assertRaises(backup.BackupJobStartError):
                    backup.start_backup_job(["data"])
                return True, "installed fixture"

            helper = native_tests.NativeBackupApiTests()
            with patch.object(restore, "install_dependencies", side_effect=install) as dependencies:
                response = app.test_client().post("/api/backup/import", data={
                    "file": (io.BytesIO(helper.archive()), "full.tar.gz"),
                    "restore_items": ["scripts"], "restore_deps": "1",
                }, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200, response.json)
            dependencies.assert_called_once()
            self.assertFalse(backup.restore_activity_active())
