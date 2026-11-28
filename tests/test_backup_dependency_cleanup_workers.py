import contextlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests import test_native_backup_api as native_tests
from tests.test_notify_action_recovery import isolated_app


WORKER = r'''
import contextlib, os, pathlib, sys, threading, time
from unittest.mock import patch
from fls_manager.app import create_app
from fls_manager.paths import BASE_DIR
from fls_manager.routes.backup import native_api as api
request_id, operation_id = sys.argv[1:]
real_process, real_stop, real_write = api.DependencyProcess, api._stop_restore_dependency_process, api._write_native_restore
def command(_command, **options):
    code = (
        'from pathlib import Path\n'
        'import time\n'
        f'p=Path({str(BASE_DIR / "install-count")!r})\n'
        'with p.open("ab") as out: out.write(b"installation\\n")\n'
        f'while Path({str(BASE_DIR / "pause-install")!r}).exists(): time.sleep(.01)\n'
        "print('installation completed',flush=True)\n"
    )
    return real_process([sys.executable, '-c', code], **options)
def stop(process):
    count = BASE_DIR / 'cleanup-count'
    with count.open('ab') as out: out.write(b'attempt\n')
    marker = BASE_DIR / 'initial-cleanup-failed'
    if not marker.exists():
        marker.touch()
        raise PermissionError('initial process cleanup unavailable')
    (BASE_DIR / 'cleanup-entered').touch()
    deadline = time.monotonic() + 30
    while (BASE_DIR / 'pause-cleanup').exists():
        if time.monotonic() >= deadline: raise RuntimeError('cleanup barrier expired')
        time.sleep(.01)
    real_stop(process)
def write(record):
    if (BASE_DIR / 'fail-cleanup-receipt').exists() and not record.get('dependency_cleanup_pending',False):
        (BASE_DIR / 'cleanup-receipt-failed').touch()
        raise OSError('cleanup receipt unavailable')
    return real_write(record)
api.DependencyProcess, api._stop_restore_dependency_process, api._write_native_restore = command, stop, write
with patch('fls_manager.app.dispatch_frontend_open_event'):
    app = create_app()
try:
    response = app.test_client().post('/api/backup/native/restores/' + request_id + '/dependencies',
        json={'operation_id':operation_id,'confirm_retry':False},headers={'X-Token':os.environ['FLS_TOKEN']})
    if response.status_code not in (200,202): raise AssertionError(str(response.json))
    sys.stdin.readline()
finally:
    (BASE_DIR / 'pause-cleanup').unlink(missing_ok=True)
    with api.RESTORE_LOCK:
        entries = list(api.RESTORE_DEPENDENCY_CLEANUP.items())
        api.RESTORE_DEPENDENCY_CLEANING.update(key for key, _ in entries)
    for key, entry in entries:
        with contextlib.suppress(Exception): api._cleanup_dependency_resources(entry)
        with api.RESTORE_LOCK:
            api.RESTORE_DEPENDENCY_CLEANUP.pop(key,None)
            api._release_process_lease(entry['lease_fd'])
            entry['lease_fd'] = None
    for thread in threading.enumerate():
        if thread.name.startswith(('backup-native-cleanup-owner-', 'backup-native-restore-deps-')):
            thread.join(15)
            if thread.is_alive(): raise AssertionError('dependency owner did not exit')
'''


class BackupDependencyCleanupWorkerTests(unittest.TestCase):
    preflight_id = "a" * 32
    request_id = "b" * 32
    operation_id = "c" * 32

    def prepare(self, app):
        helper = native_tests.NativeBackupApiTests()
        client = app.test_client()
        with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()):
            prepared = helper.submit_preflight(client, self.preflight_id)
            self.assertEqual(prepared.status_code, 200, prepared.json)
            restored = helper.submit_restore(
                client, self.request_id, self.preflight_id, prepared.json["preflight"]["archive_sha256"],
                restore_dependencies=True,
            )
        self.assertEqual(restored.status_code, 200, restored.json)
        return client, helper.headers(), "/api/backup/native/restores/" + self.request_id

    def wait_for(self, predicate, message):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.02)
        self.fail(message)

    @contextlib.contextmanager
    def owner(self, base):
        from fls_manager.routes.backup import native_api as api

        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", WORKER, self.request_id, self.operation_id],
                stdin=subprocess.PIPE, stdout=output, stderr=errors,
                env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
            )
            try:
                def ready():
                    if process.poll() is not None:
                        errors.seek(0)
                        self.fail("dependency owner exited: " + errors.read().decode(errors="replace"))
                    record = api._read_native_restore(self.request_id)
                    if (base / "pause-install").exists():
                        return ((base / "install-count").exists() and record is not None
                                and record.get("dependency_state") == "running"
                                and api._restore_lease_matches(record, "dependency"))
                    return (record is not None and record.get("dependency_state") == "unknown"
                            and api._restore_lease_matches(record, "dependency-cleanup"))

                self.wait_for(ready, "dependency cleanup owner did not become ready")
                yield process
            except BaseException as exc:
                errors.seek(0)
                raise AssertionError(str(exc) + "\n" + errors.read().decode(errors="replace")) from exc
            finally:
                (base / "pause-cleanup").unlink(missing_ok=True)
                (base / "pause-install").unlink(missing_ok=True)
                if process.poll() is None:
                    process.stdin.write(b"\n")
                    process.stdin.flush()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                        self.fail("dependency owner did not shut down")
                process.stdin.close()

    def wait_cleaned(self, client, path, headers):
        def cleaned():
            response = client.get(path, headers=headers)
            self.assertEqual(response.status_code, 200, response.json)
            result = response.json["restore"]
            return result if not result["dependencies"]["cleanup_required"] else None

        return self.wait_for(cleaned, "original dependency resources were not cleaned")

    def assert_available(self, api):
        def available():
            fd = api._try_process_lease(api.RESTORE_PROCESS_LOCK_PATH)
            if fd is None:
                return False
            api._release_process_lease(fd)
            return True

        self.wait_for(available, "completed cleanup did not release the original lease")

    def test_restore_receipt_publication_keeps_open_reader_on_previous_snapshot(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import native_api as api

            self.prepare(app)
            path = api._native_restore_path(self.request_id)
            before = path.read_bytes()
            with os.fdopen(api.open_receipt(path), "rb") as previous:
                record = api._read_native_restore(self.request_id)
                record["dependency_error"] = "new snapshot"
                api._write_native_restore(record)
                self.assertEqual(previous.read(), before)
            self.assertEqual(api._read_native_restore(self.request_id)["dependency_error"], "new snapshot")

    def test_peer_requests_original_owner_cleanup_and_gets_never_trigger_it(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api as api

            client, headers, path = self.prepare(app)
            with self.owner(base) as owner:
                saved = api._native_restore_path(self.request_id).read_bytes()
                for _ in range(3):
                    queried = client.get(path, headers=headers)
                    self.assertEqual(queried.status_code, 200, queried.json)
                    self.assertTrue(queried.json["restore"]["dependencies"]["cleanup_required"])
                self.assertEqual(api._native_restore_path(self.request_id).read_bytes(), saved)
                self.assertFalse(api._dependency_cleanup_intent_path(self.request_id).exists())
                rejected = client.post(path + "/dependencies/cleanup", json={"operation_id": "d" * 32}, headers=headers)
                self.assertEqual(rejected.status_code, 409, rejected.json)
                self.assertFalse(api._dependency_cleanup_intent_path(self.request_id).exists())
                accepted = client.post(path + "/dependencies/cleanup", json={"operation_id": self.operation_id}, headers=headers)
                self.assertEqual(accepted.status_code, 202, accepted.json)
                self.assertTrue(accepted.json["cleanup_requested"])
                result = self.wait_cleaned(client, path, headers)
                self.assertEqual(result["dependencies"]["state"], "succeeded")
                self.assertIsNone(owner.poll())
                self.wait_for(lambda: not list(api.PREFLIGHT_WORK_DIR.glob("deps-*")), "workspace survived cleanup")
                self.assert_available(api)
                replay = client.post(path + "/dependencies/cleanup", json={"operation_id": self.operation_id}, headers=headers)
                self.assertEqual(replay.status_code, 200, replay.json)
                self.assertTrue(replay.json["replayed"])
                self.assertEqual((base / "cleanup-count").read_bytes(), b"attempt\nattempt\n")
                self.assertEqual((base / "install-count").read_bytes().count(b"installation"), 1)

    def test_live_install_cannot_be_cleaned_by_a_peer(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api as api

            client, headers, path = self.prepare(app)
            (base / "pause-install").touch()
            with self.owner(base):
                queried = client.get(path, headers=headers)
                self.assertEqual(queried.json["restore"]["dependencies"]["state"], "running")
                self.assertFalse(queried.json["restore"]["dependencies"]["cleanup_required"])
                rejected = client.post(path + "/dependencies/cleanup", json={"operation_id": self.operation_id}, headers=headers)
                self.assertEqual(rejected.status_code, 409, rejected.json)
                self.assertFalse(api._dependency_cleanup_intent_path(self.request_id).exists())
                (base / "pause-install").unlink()
                self.wait_for(
                    lambda: api._restore_lease_matches(api._read_native_restore(self.request_id), "dependency-cleanup"),
                    "finished install did not retain its cleanup owner",
                )
                accepted = client.post(path + "/dependencies/cleanup", json={"operation_id": self.operation_id}, headers=headers)
                self.assertEqual(accepted.status_code, 202, accepted.json)
                self.wait_cleaned(client, path, headers)
                self.assert_available(api)

    def test_previous_intent_cannot_automatically_retry_a_new_cleanup_owner(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api as api

            client, headers, path = self.prepare(app)
            api._publish_dependency_cleanup_intent(self.request_id, self.operation_id)
            with self.owner(base):
                before = (base / "cleanup-count").read_bytes()
                time.sleep(0.3)
                self.assertEqual((base / "cleanup-count").read_bytes(), before)
                self.assertTrue(client.get(path, headers=headers).json["restore"]["dependencies"]["cleanup_required"])
                accepted = client.post(path + "/dependencies/cleanup", json={"operation_id": self.operation_id}, headers=headers)
                self.assertEqual(accepted.status_code, 202, accepted.json)
                self.wait_cleaned(client, path, headers)
                self.assert_available(api)

    def test_failed_intent_and_receipt_keep_ownership_and_need_another_explicit_request(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api as api

            client, headers, path = self.prepare(app)
            with self.owner(base):
                with patch.object(api, "_publish_dependency_cleanup_intent", side_effect=OSError("storage unavailable")):
                    failed = client.post(path + "/dependencies/cleanup", json={"operation_id": self.operation_id}, headers=headers)
                self.assertEqual(failed.status_code, 503, failed.json)
                self.assertFalse(api._dependency_cleanup_intent_path(self.request_id).exists())
                (base / "fail-cleanup-receipt").touch()
                accepted = client.post(path + "/dependencies/cleanup", json={"operation_id": self.operation_id}, headers=headers)
                self.assertEqual(accepted.status_code, 202, accepted.json)
                self.wait_for(lambda: (base / "cleanup-receipt-failed").exists(), "receipt fault was not reached")
                self.wait_for(lambda: "cleanup receipt unavailable" in api._read_native_restore(self.request_id)["dependency_error"],
                              "receipt fault was not retained")
                (base / "fail-cleanup-receipt").unlink()
                before = (base / "cleanup-count").read_bytes()
                time.sleep(0.3)
                self.assertEqual((base / "cleanup-count").read_bytes(), before)
                queried = client.get(path, headers=headers)
                self.assertTrue(queried.json["restore"]["dependencies"]["cleanup_required"])
                self.assertIsNone(api._try_process_lease(api.RESTORE_PROCESS_LOCK_PATH))
                retried = client.post(path + "/dependencies/cleanup", json={"operation_id": self.operation_id}, headers=headers)
                self.assertEqual(retried.status_code, 202, retried.json)
                self.wait_cleaned(client, path, headers)
                self.assert_available(api)
                self.assertEqual((base / "install-count").read_bytes().count(b"installation"), 1)

    def test_owner_crash_during_accepted_cleanup_allows_explicit_peer_recovery(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api as api

            client, headers, path = self.prepare(app)
            (base / "pause-cleanup").touch()
            with self.owner(base) as owner:
                accepted = client.post(path + "/dependencies/cleanup", json={"operation_id": self.operation_id}, headers=headers)
                self.assertEqual(accepted.status_code, 202, accepted.json)
                self.wait_for(lambda: (base / "cleanup-entered").exists(), "owner never consumed cleanup intent")
                owner.kill()
                owner.wait(timeout=10)

                def available():
                    fd = api._try_process_lease(api.RESTORE_PROCESS_LOCK_PATH)
                    if fd is None:
                        return False
                    api._release_process_lease(fd)
                    return True

                self.wait_for(available, "crashed owner or descendants retained the lease")
                self.assertTrue(client.get(path, headers=headers).json["restore"]["dependencies"]["cleanup_required"])
                recovered = client.post(path + "/dependencies/cleanup", json={"operation_id": self.operation_id}, headers=headers)
                self.assertEqual(recovered.status_code, 200, recovered.json)
                self.assertFalse(recovered.json["restore"]["dependencies"]["cleanup_required"])
                self.assertEqual(recovered.json["restore"]["dependencies"]["state"], "unknown")
                self.assert_available(api)
                self.assertEqual((base / "install-count").read_bytes().count(b"installation"), 1)
