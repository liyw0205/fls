import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_backup_dependency_cleanup_workers as cleanup_tests
from tests.test_notify_action_recovery import isolated_app


class DependencyEvidenceTests(unittest.TestCase):
    request_id = "b" * 32
    operation_id = "c" * 32

    def fixture(self, base):
        from fls_manager.routes.backup import dependency_evidence as evidence

        root = base / ".backup_native_work"
        root.mkdir(exist_ok=True)
        workspace = Path(tempfile.mkdtemp(prefix="deps-" + self.request_id[:8] + "-", dir=root))
        info = workspace.stat()
        identity = {"name": workspace.name, "device": info.st_dev, "inode": info.st_ino}
        return evidence, workspace, evidence.create(base, self.request_id, self.operation_id, identity)

    def test_read_only_snapshots_bind_instance_and_keep_resource_result_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            evidence, workspace, prepared = self.fixture(base)
            guarded = evidence.advance(base, prepared, "guarded", guard={"kind": "posix_group", "pid": 12345})
            started = evidence.advance(base, guarded, "start_requested")
            path = evidence._path(base, self.request_id, self.operation_id)
            before = path.read_bytes()
            for _ in range(3):
                self.assertEqual(evidence.read(base, self.request_id, self.operation_id), started)
                self.assertEqual(evidence.ids(base), [(self.request_id, self.operation_id)])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(started["revision"], 3)
            self.assertEqual(started["workspace"]["name"], workspace.name)
            self.assertFalse({"output", "archive_sha256", "preflight_id", "installation_result"} & set(started))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_stale_revision_changed_guard_and_replaced_workspace_cannot_open_start_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            evidence, workspace, prepared = self.fixture(base)
            guarded = evidence.advance(base, prepared, "guarded", guard={"kind": "posix_group", "pid": 12345})
            with self.assertRaises(RuntimeError):
                evidence.advance(base, prepared, "guarded", guard={"kind": "posix_group", "pid": 67890})
            with self.assertRaises(RuntimeError):
                evidence.advance(base, guarded, "start_requested", guard={"kind": "posix_group", "pid": 67890})
            original = workspace.with_name(workspace.name + "-original")
            workspace.rename(original)
            workspace.mkdir()
            with self.assertRaises(RuntimeError):
                evidence.advance(base, guarded, "start_requested")
            self.assertTrue(original.is_dir())
            self.assertTrue(workspace.is_dir())
            self.assertEqual(evidence.read(base, self.request_id, self.operation_id), guarded)

    def test_corrupt_oversized_duplicate_and_foreign_records_are_retained_and_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            evidence, _, prepared = self.fixture(base)
            path = evidence._path(base, self.request_id, self.operation_id)
            good = path.read_bytes()
            for invalid in (b"{bad", b" " * (evidence.RECORD_LIMIT + 1),
                            good[:-1] + b',"version":1}',
                            good.replace(b'"inode":', b'"foreign_inode":', 1)):
                with self.subTest(invalid=invalid[:40]):
                    path.write_bytes(invalid)
                    with self.assertRaises((RuntimeError, ValueError)):
                        evidence.read(base, self.request_id, self.operation_id)
                    self.assertEqual(path.read_bytes(), invalid)
            path.write_bytes(good)
            with patch.object(evidence, "HISTORY_LIMIT", 1):
                with self.assertRaises(RuntimeError):
                    evidence.create(base, self.request_id, "d" * 32, prepared["workspace"])
            self.assertEqual(evidence.read(base, self.request_id, self.operation_id), prepared)

    def test_real_command_starts_only_after_durable_guard_and_start_intent(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import dependency_evidence as evidence, native_api as api

            helper = cleanup_tests.BackupDependencyCleanupWorkerTests()
            client, headers, path = helper.prepare(app)
            marker = base / "real-command-started"
            real_process, real_gate = api.DependencyProcess, api.DependencyProcess.start
            real_thread_start = threading.Thread.start

            def factory(_command, **options):
                return real_process([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"], **options)

            def gate(process):
                record = evidence.read(base, self.request_id, self.operation_id)
                self.assertIsNotNone(record)
                self.assertEqual(record["state"], "start_requested")
                self.assertEqual(record["guard"], process.identity)
                self.assertEqual(record["workspace"], api._dependency_workspace_identity(process.result_path.parent))
                self.assertFalse(marker.exists())
                return real_gate(process)

            def start(thread):
                if thread.name == "backup-native-restore-deps-" + self.request_id[:8]:
                    return thread.run()
                return real_thread_start(thread)

            with patch.object(api, "DependencyProcess", side_effect=factory), patch.object(
                real_process, "start", autospec=True, side_effect=gate,
            ), patch.object(threading.Thread, "start", autospec=True, side_effect=start):
                response = client.post(path + "/dependencies", json={
                    "operation_id": self.operation_id, "confirm_retry": False,
                }, headers=headers)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json["restore"]["dependencies"]["state"], "succeeded", response.json)
            self.assertTrue(marker.exists())
            record = evidence.read(base, self.request_id, self.operation_id)
            self.assertEqual(record["state"], "cleaned")
            self.assertEqual(record["revision"], 4)
            self.assertFalse((api.PREFLIGHT_WORK_DIR / record["workspace"]["name"]).exists())
            helper.assert_available(api)

    def test_failed_durable_start_intent_never_runs_real_command(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import dependency_evidence as evidence, native_api as api

            helper = cleanup_tests.BackupDependencyCleanupWorkerTests()
            client, headers, path = helper.prepare(app)
            marker = base / "must-never-start"
            real_process, publish = api.DependencyProcess, evidence._publish
            real_thread_start = threading.Thread.start

            def factory(_command, **options):
                return real_process([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"], **options)

            def fail_start(base, record):
                if record["state"] == "start_requested":
                    raise OSError("independent start receipt unavailable")
                return publish(base, record)

            def start(thread):
                if thread.name == "backup-native-restore-deps-" + self.request_id[:8]:
                    return thread.run()
                return real_thread_start(thread)

            with patch.object(api, "DependencyProcess", side_effect=factory), patch.object(
                evidence, "_publish", side_effect=fail_start,
            ), patch.object(threading.Thread, "start", autospec=True, side_effect=start):
                response = client.post(path + "/dependencies", json={
                    "operation_id": self.operation_id, "confirm_retry": False,
                }, headers=headers)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertFalse(marker.exists(), "command ran without its durable start evidence")
            self.assertEqual(response.json["restore"]["dependencies"]["state"], "failed")
            self.assertEqual(evidence.read(base, self.request_id, self.operation_id)["state"], "cleaned")
            helper.assert_available(api)

    def test_publication_error_after_prepared_replace_never_creates_a_supervisor(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import dependency_evidence as evidence, native_api as api

            helper = cleanup_tests.BackupDependencyCleanupWorkerTests()
            client, headers, path = helper.prepare(app)
            publish = evidence._publish

            def ambiguous_publish(base, record):
                publish(base, record)
                if record["state"] == "prepared":
                    raise OSError("prepared publication acknowledgement lost")

            def start(thread):
                if thread.name == "backup-native-restore-deps-" + self.request_id[:8]:
                    return thread.run()
                raise AssertionError("failed preparation must not need a reader or cleanup watcher")

            with patch.object(api, "DependencyProcess") as no_process, patch.object(
                evidence, "_publish", side_effect=ambiguous_publish,
            ), patch.object(threading.Thread, "start", autospec=True, side_effect=start):
                response = client.post(path + "/dependencies", json={
                    "operation_id": self.operation_id, "confirm_retry": False,
                }, headers=headers)
                no_process.assert_not_called()
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json["restore"]["dependencies"]["state"], "failed")
            record = evidence.read(base, self.request_id, self.operation_id)
            self.assertEqual(record["state"], "cleaned")
            self.assertIsNone(record["guard"])
            helper.assert_available(api)

    def test_ambiguous_cleanup_publication_keeps_lease_until_an_explicit_durable_retry(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import dependency_evidence as evidence, native_api as api

            helper = cleanup_tests.BackupDependencyCleanupWorkerTests()
            client, headers, path = helper.prepare(app)
            marker = base / "installation-once"
            real_process, publish, real_thread_start = api.DependencyProcess, evidence._publish, threading.Thread.start

            def factory(_command, **options):
                return real_process([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('once')"], **options)

            def ambiguous_publish(base, record):
                publish(base, record)
                if record["state"] == "cleaned":
                    raise OSError("cleanup publication acknowledgement lost")

            def start(thread):
                if thread.name == "backup-native-restore-deps-" + self.request_id[:8]:
                    return thread.run()
                return real_thread_start(thread)

            with patch.object(api, "DependencyProcess", side_effect=factory), patch.object(
                evidence, "_publish", side_effect=ambiguous_publish,
            ), patch.object(threading.Thread, "start", autospec=True, side_effect=start):
                response = client.post(path + "/dependencies", json={
                    "operation_id": self.operation_id, "confirm_retry": False,
                }, headers=headers)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertTrue(response.json["restore"]["dependencies"]["cleanup_required"])
            self.assertEqual(response.json["restore"]["dependencies"]["state"], "unknown")
            self.assertIsNone(api._try_process_lease(api.RESTORE_PROCESS_LOCK_PATH))
            record = evidence.read(base, self.request_id, self.operation_id)
            self.assertEqual(record["state"], "cleaned")
            independent = evidence._path(base, self.request_id, self.operation_id)
            before = independent.read_bytes()
            for _ in range(3):
                self.assertTrue(client.get(path, headers=headers).json["restore"]["dependencies"]["cleanup_required"])
            self.assertEqual(independent.read_bytes(), before)
            replace = api.backup_commit._replace
            with patch.object(api.backup_commit, "_replace", wraps=replace) as durable_publish:
                completed = client.post(path + "/dependencies/cleanup", json={
                    "operation_id": self.operation_id,
                }, headers=headers)
            self.assertEqual(completed.status_code, 200, completed.json)
            self.assertFalse(completed.json["restore"]["dependencies"]["cleanup_required"])
            self.assertIn(independent, [Path(call.args[1]) for call in durable_publish.call_args_list])
            self.assertEqual(marker.read_text(), "once")
            helper.assert_available(api)

    def test_absent_workspace_does_not_release_directory_or_followup_gate_without_resource_receipt(self):
        with isolated_app() as (_, base):
            from fls_manager.routes.backup import native_api as api

            evidence, workspace, record = self.fixture(base)
            workspace.rmdir()
            self.assertTrue(api._orphan_dependency_evidence(self.request_id))
            self.assertTrue(api._dependency_cleanup_pending())
            evidence.advance(base, record, "cleaned")
            self.assertFalse(api._orphan_dependency_evidence(self.request_id))
            self.assertFalse(api._dependency_cleanup_pending())
