import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path

from tests.test_bulk_workflows import TOKEN
from tests.test_notify_action_recovery import isolated_app


class NativeBackupApiTests(unittest.TestCase):
    def client(self, app):
        return app.test_client()

    def headers(self):
        return {"X-Token": TOKEN}

    def archive(self, members=None):
        members = members or {
            "fls/data/config.json": b'{"theme":"dark"}',
            "fls/scripts/example.py": b"print('ok')\n",
            "fls/data/requirements.txt": b"requests==2.0\n",
        }
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, content in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return output.getvalue()

    def zip_archive(self, members):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        return output.getvalue()

    def submit_preflight(self, client, request_id, content=None, filename="full.tar.gz"):
        content = self.archive() if content is None else content
        response = client.post(
            "/api/backup/native/preflights",
            data={
                "preflight_id": request_id,
                "file": (io.BytesIO(content), filename),
            },
            headers=self.headers(),
            content_type="multipart/form-data",
        )
        if response.status_code == 202:
            return self.wait_preflight(client, request_id)
        return response

    def wait_preflight(self, client, request_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = client.get(
                f"/api/backup/native/preflights/{request_id}", headers=self.headers(),
            )
            state = response.json.get("preflight", {}).get("status")
            if state not in ("uploading", "checking"):
                return response
            time.sleep(0.01)
        self.fail("preflight worker did not reach a terminal state")

    def submit_restore(self, client, request_id, preflight_id, digest, items=("data",), **values):
        body = {
            "request_id": request_id,
            "preflight_id": preflight_id,
            "archive_sha256": digest,
            "restore_items": list(items),
            "restore_dependencies": False,
            "confirm_data_impact": "data" in items,
        }
        body.update(values)
        return client.post("/api/backup/native/restores", json=body, headers=self.headers())

    def wait_restore(self, client, request_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = client.get(f"/api/backup/native/restores/{request_id}", headers=self.headers())
            state = response.json.get("restore", {}).get("state")
            if state not in ("accepted", "extracting", "staging", "committing"):
                return response
            time.sleep(0.01)
        self.fail("restore worker did not reach a terminal state")

    def test_archive_target_canonicalizes_root_and_rejects_escape(self):
        with isolated_app() as (_, base):
            from fls_manager.routes.backup import _common as backup

            root = base / "unused" / ".." / "archive"
            self.assertEqual(
                backup._archive_member_target(root, "fls/data/config.json"),
                (base / "archive/fls/data/config.json").resolve(),
            )
            for name in ("../outside", "/outside", "C:/outside", "..\\outside"):
                with self.subTest(name=name), self.assertRaises(RuntimeError):
                    backup._archive_member_target(root, name)

    def test_preflight_stages_and_validates_archive_without_touching_live_data(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api
            from fls_manager.paths import CONFIG_FILE
            CONFIG_FILE.write_text('{"live":true}', encoding="utf-8")
            request_id = "a" * 32
            content = self.archive()
            response = self.submit_preflight(self.client(app), request_id, content)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["ok"])
            result = response.json["preflight"]
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["items"], ["data", "scripts"])
            self.assertEqual(result["dependency_files"], ["requirements.txt"])
            self.assertIn("data_restore_may_change_panel_access", result["warnings"])
            self.assertEqual(result["archive_size_bytes"], len(content))
            self.assertEqual(result["archive_sha256"], __import__("hashlib").sha256(content).hexdigest())
            self.assertEqual(CONFIG_FILE.read_text(encoding="utf-8"), '{"live":true}')
            staged = base / ".backup_native_staging" / f"{request_id}.archive"
            self.assertEqual(staged.read_bytes(), content)
            record = base / ".backup_native_preflights" / f"{request_id}.json"
            before = (record.read_bytes(), staged.read_bytes())
            queried = self.client(app).get(
                f"/api/backup/native/preflights/{request_id}", headers=self.headers(),
            )
            self.assertEqual(queried.status_code, 200)
            self.assertEqual(queried.headers["Cache-Control"], "no-store")
            self.assertEqual((record.read_bytes(), staged.read_bytes()), before)
            replay = self.submit_preflight(self.client(app), request_id, content)
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["preflight"]["replayed"])
            conflicting = self.submit_preflight(
                self.client(app), request_id,
                self.archive({"fls/data/config.json": b"different"}),
            )
            self.assertEqual(conflicting.status_code, 409)
            self.assertEqual(conflicting.json["code"], "preflight_id_conflict")

    def test_preflight_rejects_unsafe_archive_and_removes_partial_snapshot(self):
        with isolated_app() as (app, base):
            request_id = "b" * 32
            response = self.submit_preflight(
                self.client(app), request_id,
                self.archive({"fls/data/config.json": b"ok", "../outside": b"bad"}),
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["preflight"]["error_code"], "unsafe_archive_entry")
            self.assertFalse((base / ".backup_native_staging" / f"{request_id}.archive").exists())
            queried = self.client(app).get(
                f"/api/backup/native/preflights/{request_id}", headers=self.headers(),
            )
            self.assertEqual(queried.json["preflight"]["status"], "failed")

    def test_preflight_inspects_zip_archives(self):
        with isolated_app() as (app, _):
            content = self.zip_archive({
                "fls/data/config.json": b'{"theme":"dark"}',
                "fls/scripts/example.py": b"print('ok')\n",
            })
            response = self.submit_preflight(
                self.client(app), "0" * 32, content, filename="full.zip",
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["preflight"]["status"], "ready")
            self.assertEqual(response.json["preflight"]["format"], "zip")
            self.assertEqual(response.json["preflight"]["items"], ["data", "scripts"])

    def test_preflight_rejects_declared_expansion_when_disk_space_is_insufficient(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api
            request_id = "6" * 32
            with patch.object(
                native_api.shutil,
                "disk_usage",
                return_value=type("Usage", (), {"free": 0, "total": 0, "used": 0})(),
            ):
                response = self.submit_preflight(self.client(app), request_id)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["preflight"]["error_code"], "insufficient_storage")
            self.assertFalse((base / ".backup_native_staging" / f"{request_id}.archive").exists())

    def test_preflight_can_cancel_an_active_check_and_cleans_the_snapshot(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api
            client = self.client(app)
            request_id = "8" * 32
            entered = threading.Event()

            def wait_for_cancel(archive_path, suffix, work_dir, cancel_event):
                entered.set()
                if not cancel_event.wait(2):
                    raise AssertionError("cancel signal was not delivered")
                raise ValueError("preflight_cancelled")

            with patch.object(native_api, "_inspect_preflight_archive", side_effect=wait_for_cancel):
                content = self.archive()
                response = client.post(
                    "/api/backup/native/preflights",
                    data={"preflight_id": request_id, "file": (io.BytesIO(content), "full.tar.gz")},
                    headers=self.headers(), content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 202)
                self.assertTrue(entered.wait(2))
                conflicting = client.post(
                    "/api/backup/native/preflights",
                    data={"preflight_id": request_id, "file": (io.BytesIO(self.archive({"fls/data/config.json": b"different"})), "full.tar.gz")},
                    headers=self.headers(), content_type="multipart/form-data",
                )
                self.assertEqual(conflicting.status_code, 409)
                self.assertEqual(conflicting.json["code"], "preflight_id_conflict")
                cancelled = client.post(
                    f"/api/backup/native/preflights/{request_id}/cancel", headers=self.headers(),
                )
                self.assertEqual(cancelled.status_code, 202)
                result = self.wait_preflight(client, request_id)

            self.assertEqual(result.json["preflight"]["status"], "cancelled")
            self.assertEqual(result.json["preflight"]["error_code"], "preflight_cancelled")
            self.assertFalse((base / ".backup_native_staging" / f"{request_id}.archive").exists())

    def test_preflight_reports_orphaned_check_as_unknown_without_mutation(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api
            request_id = "9" * 32
            native_api._ensure_private_directory(native_api.PREFLIGHT_DIR)
            native_api._ensure_private_directory(native_api.PREFLIGHT_ARCHIVE_DIR)
            record = {
                "request_id": request_id, "state": "checking", "filename": "full.tar.gz",
                "archive_format": "tar.gz", "archive_size_bytes": 3, "archive_sha256": "a" * 64,
                "items": [], "entry_count": 0, "expanded_bytes": 0, "dependency_files": [],
                "warnings": [], "error_code": "", "created_at": 1, "expires_at": 4_000_000_000,
                "retention_until": 5_000_000_000,
            }
            native_api._write_preflight_record(request_id, record)
            archive_path = base / ".backup_native_staging" / f"{request_id}.archive"
            archive_path.write_bytes(b"abc")
            before = (native_api._preflight_record_path(request_id).read_bytes(), archive_path.read_bytes())

            result = self.client(app).get(
                f"/api/backup/native/preflights/{request_id}", headers=self.headers(),
            )

            self.assertEqual(result.json["preflight"]["status"], "unknown")
            self.assertFalse(result.json["preflight"]["active"])
            self.assertEqual((native_api._preflight_record_path(request_id).read_bytes(), archive_path.read_bytes()), before)

    def test_expired_preflight_get_does_not_rewrite_or_delete_evidence(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api
            request_id = "7" * 32
            self.submit_preflight(self.client(app), request_id)
            record_path = base / ".backup_native_preflights" / f"{request_id}.json"
            archive_path = base / ".backup_native_staging" / f"{request_id}.archive"
            before = (record_path.read_bytes(), archive_path.read_bytes())
            with patch.object(native_api.time, "time", return_value=2_000_000_000):
                response = self.client(app).get(
                    f"/api/backup/native/preflights/{request_id}", headers=self.headers(),
                )
            self.assertEqual(response.json["preflight"]["status"], "expired")
            self.assertEqual((record_path.read_bytes(), archive_path.read_bytes()), before)

    def test_preflight_enforces_archive_limits_and_retained_snapshot_capacity(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import _common as backup
            client = self.client(app)
            with patch.object(backup, "MAX_BACKUP_UPLOAD_BYTES", 32):
                too_large = self.submit_preflight(client, "c" * 32, b"x" * 128)
            self.assertEqual(too_large.status_code, 413)
            self.assertEqual(too_large.json["preflight"]["error_code"], "archive_limit_exceeded")
            for request_id in ("d" * 32, "e" * 32):
                ready = self.submit_preflight(client, request_id)
                self.assertEqual(ready.status_code, 200)
            full = self.submit_preflight(client, "f" * 32)
            self.assertEqual(full.status_code, 429)
            self.assertEqual(full.json["code"], "preflight_capacity")
            self.assertFalse((base / ".backup_native_staging" / f"{'f' * 32}.archive").exists())

    def test_native_restore_is_async_idempotent_and_preserves_runtime_identity(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import _common as backup
            from fls_manager.routes.backup import native_api
            client = self.client(app)
            backup.DATA_DIR.mkdir(parents=True, exist_ok=True)
            (backup.DATA_DIR / "config.json").write_text('{"live":true}', encoding="utf-8")
            (backup.DATA_DIR / "secret_key.txt").write_text("live-instance-key", encoding="utf-8")
            (backup.DATA_DIR / "native_sessions.sqlite3").write_bytes(b"live-session-db")
            (backup.DATA_DIR / "native_sessions.sqlite3-wal").write_bytes(b"live-session-wal")
            live_requests = backup.DATA_DIR / "proxy_requests"
            live_requests.mkdir()
            (live_requests / ("f" * 32 + ".json")).write_text('{"live":"receipt"}', encoding="utf-8")
            content = self.archive({
                "fls/data/config.json": b'{"restored":true}',
                "fls/data/secret_key.txt": b"attacker-instance-key",
                "fls/data/native_sessions.sqlite3": b"attacker-session-db",
                "fls/data/proxy_requests/" + "f" * 32 + ".json": b'{"archive":"receipt"}',
                "fls/scripts/example.py": b"print('restored')\n",
            })
            preflight_id = "1" * 32
            prepared = self.submit_preflight(client, preflight_id, content)
            self.assertEqual(prepared.status_code, 200)
            digest = prepared.json["preflight"]["archive_sha256"]
            request_id = "2" * 32

            with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()):
                response = self.submit_restore(client, request_id, preflight_id, digest)

            self.assertEqual(response.status_code, 200)
            restored = response.json["restore"]
            self.assertEqual(restored["state"], "restored")
            self.assertTrue(restored["committed"])
            self.assertEqual(restored["scheduler"]["state"], "pending")
            self.assertEqual((backup.DATA_DIR / "config.json").read_text(encoding="utf-8"), '{"restored":true}')
            self.assertEqual((backup.DATA_DIR / "secret_key.txt").read_text(encoding="utf-8"), "live-instance-key")
            self.assertEqual((backup.DATA_DIR / "native_sessions.sqlite3").read_bytes(), b"live-session-db")
            self.assertEqual((backup.DATA_DIR / "native_sessions.sqlite3-wal").read_bytes(), b"live-session-wal")
            self.assertEqual(
                (backup.DATA_DIR / "proxy_requests" / ("f" * 32 + ".json")).read_text(encoding="utf-8"),
                '{"live":"receipt"}',
            )
            self.assertTrue((backup.SCRIPT_DIR / "example.py").exists() is False)
            self.assertTrue((base / ".backup_native_restores" / f"{request_id}.json").is_file())

            receipt = native_api._native_restore_path(request_id).read_bytes()
            queried = client.get(f"/api/backup/native/restores/{request_id}", headers=self.headers())
            history = client.get("/api/backup/native/restores?limit=10&offset=0", headers=self.headers())
            self.assertEqual(queried.status_code, 200)
            self.assertEqual(queried.headers["Cache-Control"], "no-store")
            self.assertEqual(history.json["total"], 1)
            self.assertEqual(receipt, native_api._native_restore_path(request_id).read_bytes())

            (backup.DATA_DIR / "config.json").write_text('{"later":true}', encoding="utf-8")
            replay = self.submit_restore(client, request_id, preflight_id, digest)
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["replayed"])
            self.assertEqual((backup.DATA_DIR / "config.json").read_text(encoding="utf-8"), '{"later":true}')

    def test_native_restore_requires_explicit_data_confirmation_and_binds_preflight(self):
        with isolated_app() as (app, base):
            client = self.client(app)
            preflight_id = "3" * 32
            prepared = self.submit_preflight(client, preflight_id)
            digest = prepared.json["preflight"]["archive_sha256"]
            missing_confirmation = self.submit_restore(
                client, "4" * 32, preflight_id, digest, confirm_data_impact=False,
            )
            self.assertEqual(missing_confirmation.status_code, 400)
            self.assertFalse((base / ".backup_native_restores").exists())

            invalid_scope = self.submit_restore(
                client, "5" * 32, preflight_id, "f" * 64,
            )
            self.assertEqual(invalid_scope.status_code, 409)
            self.assertFalse((base / ".backup_native_restores").exists())

    def test_restore_start_failures_release_activity_and_process_lease_even_if_receipt_write_fails(self):
        for failure in ("construct", "start", "failed_receipt"):
            with self.subTest(failure=failure), isolated_app() as (app, _):
                from fls_manager.routes.backup import _common as backup
                from fls_manager.routes.backup import native_api

                client = self.client(app)
                preflight_id, request_id = "3" * 32, "4" * 32
                prepared = self.submit_preflight(client, preflight_id)
                digest = prepared.json["preflight"]["archive_sha256"]
                backup.DATA_DIR.mkdir(parents=True, exist_ok=True)
                (backup.DATA_DIR / "config.json").write_bytes(b"live-config")
                live_config = (backup.DATA_DIR / "config.json").read_bytes()
                write = native_api._write_native_restore

                def reject_failure_receipt(record):
                    if record["state"] == "failed":
                        raise OSError("receipt storage unavailable")
                    return write(record)

                with contextlib.ExitStack() as stack:
                    if failure == "construct":
                        stack.enter_context(patch.object(native_api.threading, "Thread", side_effect=RuntimeError("no thread")))
                    else:
                        stack.enter_context(patch.object(threading.Thread, "start", side_effect=RuntimeError("no worker")))
                    if failure == "failed_receipt":
                        stack.enter_context(patch.object(native_api, "_write_native_restore", side_effect=reject_failure_receipt))
                    response = self.submit_restore(client, request_id, preflight_id, digest)

                self.assertEqual(response.status_code, 503, response.json)
                self.assertFalse(backup.RESTORE_ACTIVE)
                self.assertNotIn(request_id, native_api.RESTORE_ACTIVE)
                self.assertNotIn(preflight_id, native_api.PREFLIGHT_RETAINED_FOR_RESTORE)
                lease = native_api._try_process_lease(native_api.RESTORE_PROCESS_LOCK_PATH)
                self.assertIsNotNone(lease)
                native_api._release_process_lease(lease)
                self.assertEqual((backup.DATA_DIR / "config.json").read_bytes(), live_config)
                queried = client.get(f"/api/backup/native/restores/{request_id}", headers=self.headers())
                self.assertEqual(queried.json["restore"]["state"], "failed")
                self.assertTrue(queried.json["restore"]["live_state_unchanged"])

    def test_restore_get_maps_orphaned_mutation_to_unknown_without_writes(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api
            native_api._ensure_private_directory(native_api.RESTORE_DIR)
            request_id = "6" * 32
            record = {
                "version": 1, "request_id": request_id, "preflight_id": "7" * 32,
                "archive_sha256": "a" * 64, "archive_size_bytes": 12, "filename": "full.tar.gz",
                "restore_items": ["data"], "restore_dependencies": False,
                "state": "committing", "live_state_unchanged": False,
                "scheduler_state": "pending", "scheduler_operation_id": "", "scheduler_error": "",
                "dependency_state": "not_requested", "dependency_operation_id": "",
                "dependency_error": "", "dependency_output": "", "error": "",
                "created_at": 1, "updated_at": 2,
            }
            native_api._write_native_restore(record)
            path = base / ".backup_native_restores" / f"{request_id}.json"
            before = path.read_bytes()

            result = self.client(app).get(
                f"/api/backup/native/restores/{request_id}", headers=self.headers(),
            )

            self.assertEqual(result.status_code, 200, result.json)
            self.assertEqual(result.json["restore"]["state"], "unknown")
            self.assertTrue(result.json["restore"]["restore_outcome_unknown"])
            self.assertEqual(path.read_bytes(), before)

            resolved = self.client(app).post(
                f"/api/backup/native/restores/{request_id}/resolve",
                json={"outcome": "not_applied", "confirm": True},
                headers=self.headers(),
            )
            self.assertEqual(resolved.status_code, 200)
            self.assertEqual(resolved.json["restore"]["state"], "failed")
            self.assertTrue(resolved.json["restore"]["live_state_unchanged"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["operator_resolution"], "not_applied")

    def test_restore_lease_blocks_new_mutations_and_preserves_read_only_active_status(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import native_api

            client = self.client(app)
            preflight_id, request_id = "3" * 32, "4" * 32
            prepared = self.submit_preflight(client, preflight_id)
            digest = prepared.json["preflight"]["archive_sha256"]
            with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()):
                self.submit_restore(client, request_id, preflight_id, digest)
            native_api._restore_record_update(request_id, state="committing")
            path = native_api._native_restore_path(request_id)
            before = path.read_bytes()
            lease = native_api._try_process_lease(native_api.RESTORE_PROCESS_LOCK_PATH)
            self.assertIsNotNone(lease)
            native_api._mark_restore_lease(lease, request_id, "restore")
            try:
                result = client.get(f"/api/backup/native/restores/{request_id}", headers=self.headers())
                self.assertTrue(result.json["restore"]["active"])
                self.assertEqual(result.json["restore"]["state"], "committing")
                resolved = client.post(
                    f"/api/backup/native/restores/{request_id}/resolve",
                    json={"outcome": "not_applied", "confirm": True}, headers=self.headers(),
                )
                self.assertEqual(resolved.status_code, 409)
                new = self.submit_restore(client, "5" * 32, preflight_id, digest)
                self.assertEqual(new.status_code, 409)
                self.assertEqual(new.json["code"], "restore_busy")
                self.assertEqual(path.read_bytes(), before)
            finally:
                native_api._release_process_lease(lease)
            result = client.get(f"/api/backup/native/restores/{request_id}", headers=self.headers())
            self.assertEqual(result.json["restore"]["state"], "unknown")
            self.assertFalse(result.json["restore"]["committed"])
            self.assertEqual(path.read_bytes(), before)

    def test_restore_archive_retention_survives_expiry_and_lost_in_memory_references(self):
        for state, dependencies in (("unknown", False), ("restored", True)):
            with self.subTest(state=state), isolated_app() as (app, _):
                from fls_manager.routes.backup import native_api

                client = self.client(app)
                preflight_id, request_id = "3" * 32, "4" * 32
                prepared = self.submit_preflight(client, preflight_id)
                digest = prepared.json["preflight"]["archive_sha256"]
                with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()):
                    self.submit_restore(
                        client, request_id, preflight_id, digest, restore_dependencies=dependencies,
                    )
                native_api._restore_record_update(request_id, state=state)
                preflight = native_api._read_preflight_record(preflight_id)
                preflight["expires_at"] = int(time.time()) - 1
                native_api._write_preflight_record(preflight_id, preflight)
                snapshot = native_api._preflight_archive_path(preflight_id)
                original = snapshot.read_bytes()

                with patch.object(native_api, "PREFLIGHT_RETAINED_FOR_RESTORE", set()):
                    self.assertEqual(native_api._expire_preflight_records(int(time.time())), 0)
                    self.assertEqual(snapshot.read_bytes(), original)
                    second = self.submit_preflight(client, "5" * 32)
                    self.assertEqual(second.json["preflight"]["status"], "ready")
                    third = self.submit_preflight(client, "6" * 32)
                    self.assertEqual(third.status_code, 429)
                    self.assertEqual(third.json["code"], "preflight_capacity")

                native_api._restore_record_update(
                    request_id, state="restored", dependency_state="succeeded" if dependencies else "not_requested",
                )
                self.assertEqual(native_api._expire_preflight_records(int(time.time())), 1)
                self.assertFalse(snapshot.exists())
                expired = client.get(f"/api/backup/native/preflights/{preflight_id}", headers=self.headers())
                self.assertEqual(expired.status_code, 200)
                self.assertEqual(expired.json["preflight"]["status"], "expired")

    def test_native_restore_maintenance_get_is_read_only(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore
            event_id = "b" * 32
            state = {
                "version": 1,
                "event_id": event_id,
                "required": False,
                "fingerprint": "c" * 64,
                "target_digest": "d" * 64,
                "kind": "publication",
                "acknowledged": False,
            }
            restore.BACKUP_RESTORE_REQUESTS_DIR.mkdir(parents=True)
            restore._write_restore_archive_maintenance_state(state)
            path = base / ".backup_restore_requests" / restore.RESTORE_ARCHIVE_MAINTENANCE_NAME
            before = path.read_bytes()

            response = self.client(app).get(
                "/api/backup/native/restore-maintenance", headers=self.headers(),
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["maintenance"]["event_id"], event_id)
            self.assertFalse(response.json["maintenance"]["required"])
            self.assertEqual(path.read_bytes(), before)

    def test_restore_scheduler_reload_is_an_independent_confirmed_operation(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup
            client = self.client(app)
            preflight_id = "8" * 32
            prepared = self.submit_preflight(client, preflight_id)
            digest = prepared.json["preflight"]["archive_sha256"]
            request_id = "9" * 32
            with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()):
                restored = self.submit_restore(client, request_id, preflight_id, digest)
            self.assertEqual(restored.json["restore"]["scheduler"]["state"], "pending")
            restored_config = (backup.DATA_DIR / "config.json").read_bytes()

            first_action = {"operation_id": "a" * 32, "confirm_retry": False}
            with patch("fls_manager.scheduler.reload_scheduler", side_effect=RuntimeError("scheduler unavailable")):
                failed = client.post(
                    f"/api/backup/native/restores/{request_id}/scheduler-reload",
                    json=first_action, headers=self.headers(),
                )
            self.assertEqual(failed.status_code, 503)
            self.assertEqual(failed.json["restore"]["scheduler"]["state"], "failed")
            self.assertEqual((backup.DATA_DIR / "config.json").read_bytes(), restored_config)

            retry = {"operation_id": "b" * 32, "confirm_retry": False}
            rejected = client.post(
                f"/api/backup/native/restores/{request_id}/scheduler-reload",
                json=retry, headers=self.headers(),
            )
            self.assertEqual(rejected.status_code, 409)
            retry["confirm_retry"] = True
            with patch("fls_manager.scheduler.reload_scheduler") as reload_scheduler:
                succeeded = client.post(
                    f"/api/backup/native/restores/{request_id}/scheduler-reload",
                    json=retry, headers=self.headers(),
                )
            self.assertEqual(succeeded.status_code, 200)
            self.assertEqual(succeeded.json["restore"]["scheduler"]["state"], "succeeded")
            reload_scheduler.assert_called_once()
            with patch("fls_manager.scheduler.reload_scheduler") as reload_scheduler:
                old_replay = client.post(
                    f"/api/backup/native/restores/{request_id}/scheduler-reload",
                    json=dict(first_action, confirm_retry=True), headers=self.headers(),
                )
            self.assertEqual(old_replay.status_code, 200)
            self.assertTrue(old_replay.json["replayed"])
            self.assertEqual(old_replay.json["operation"]["state"], "failed")
            self.assertEqual(old_replay.json["restore"]["scheduler"]["operation_id"], retry["operation_id"])
            reload_scheduler.assert_not_called()

    def test_restore_dependency_install_runs_separately_with_bounded_output(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import native_api
            client = self.client(app)
            preflight_id = "c" * 32
            prepared = self.submit_preflight(client, preflight_id)
            digest = prepared.json["preflight"]["archive_sha256"]
            request_id = "d" * 32
            real_start = threading.Thread.start

            def run_restore_inline(thread):
                if thread.name.endswith("-output-" + request_id[:8]):
                    return real_start(thread)
                return thread.run()

            with patch.object(threading.Thread, "start", autospec=True, side_effect=run_restore_inline):
                restored = self.submit_restore(
                    client, request_id, preflight_id, digest,
                    restore_dependencies=True,
                )
                self.assertEqual(restored.status_code, 200)
                self.assertEqual(restored.json["restore"]["dependencies"]["state"], "pending")

                class CompletedProcess:
                    def __init__(self, *args, **kwargs):
                        self.stdout = io.BytesIO(b"Installing dependencies\n")
                        self.identity = {"kind": "posix_group", "pid": 12345}

                    def start(self):
                        pass

                    def wait(self, timeout=None):
                        return 0

                    def poll(self):
                        return 0

                    def kill(self):
                        raise AssertionError("completed dependency process must not be killed")

                with patch.object(native_api, "DependencyProcess", CompletedProcess), patch.object(
                    native_api, "_stop_restore_dependency_process",
                ) as stop_process:
                    result = client.post(
                        f"/api/backup/native/restores/{request_id}/dependencies",
                        json={"operation_id": "e" * 32, "confirm_retry": False},
                        headers=self.headers(),
                    )

            self.assertEqual(result.status_code, 200, result.json)
            state = result.json["restore"]["dependencies"]
            self.assertEqual(state["state"], "succeeded")
            self.assertIn("Installing dependencies", state["output"])
            self.assertLessEqual(len(state["output"].encode("utf-8")), native_api.RESTORE_DEPENDENCY_OUTPUT_LIMIT)
            stop_process.assert_called_once()

    def test_restore_output_is_limited_by_utf8_bytes_and_scrubs_credentials(self):
        with isolated_app() as (_, _):
            from fls_manager.routes.backup import native_api

            result = native_api._bounded_text("中" * 20000, 32768)
            self.assertLessEqual(len(result.encode("utf-8")), 32768)
            sanitized = native_api._bounded_text(
                "https://user:password@example.com/index?token=private-token password=private-password", 1000,
            )
            self.assertNotIn("private-token", sanitized)
            self.assertNotIn("private-password", sanitized)
            self.assertNotIn("user:password", sanitized)
            output = bytearray()
            stream = io.BytesIO(
                b"normal output\npassword=" + b"x" * 40000 + b"\n"
                + b"token=private-token\n" + "中\n".encode("utf-8") * 20000,
            )
            native_api._drain_restore_dependency_output(stream, output, threading.Lock())
            self.assertTrue(stream.closed)
            self.assertLessEqual(len(output), native_api.RESTORE_DEPENDENCY_OUTPUT_LIMIT)
            self.assertNotIn(b"private-token", output)
            self.assertNotIn(b"x" * 100, output)

    def test_dependency_cleanup_is_explicit_idempotent_and_keeps_lease_until_receipt_saved(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import native_api

            client = self.client(app)
            preflight_id, request_id, operation_id = "3" * 32, "4" * 32, "5" * 32
            prepared = self.submit_preflight(client, preflight_id)
            real_start = threading.Thread.start

            def start(thread):
                return real_start(thread) if "-output-" in thread.name else thread.run()

            class Process:
                def __init__(self, *args, **kwargs):
                    self.stdout = io.BytesIO(b"completed output\n")
                    self.identity = {"kind": "posix_group", "pid": 12345}
                    self.stop_attempts = 0

                def start(self):
                    pass

                def wait(self, timeout):
                    return 0

                def stop(self, timeout):
                    self.stop_attempts += 1
                    if self.stop_attempts == 1:
                        raise PermissionError("process cleanup temporarily unavailable")

            path = f"/api/backup/native/restores/{request_id}"
            with patch.object(threading.Thread, "start", autospec=True, side_effect=start):
                self.submit_restore(
                    client, request_id, preflight_id, prepared.json["preflight"]["archive_sha256"],
                    restore_dependencies=True,
                )
                with patch.object(native_api, "DependencyProcess", side_effect=Process) as create_process:
                    submitted = client.post(path + "/dependencies", json={
                        "operation_id": operation_id, "confirm_retry": False,
                    }, headers=self.headers())
                    self.assertEqual(submitted.status_code, 200, submitted.json)
                    self.assertTrue(submitted.json["restore"]["dependencies"]["cleanup_required"])
                    self.assertEqual(submitted.json["restore"]["dependencies"]["state"], "unknown")
                    saved_before = native_api._native_restore_path(request_id).read_bytes()
                    with patch.object(native_api, "_cleanup_dependency_resources") as cleanup:
                        queried = client.get(path, headers=self.headers())
                        cleanup.assert_not_called()
                    self.assertTrue(queried.json["restore"]["dependencies"]["cleanup_required"])
                    self.assertEqual(native_api._native_restore_path(request_id).read_bytes(), saved_before)
                    self.assertIsNone(native_api._try_process_lease(native_api.RESTORE_PROCESS_LOCK_PATH))
                    rejected = client.post(path + "/dependencies", json={
                        "operation_id": "6" * 32, "confirm_retry": True,
                    }, headers=self.headers())
                    self.assertEqual(rejected.status_code, 409)
                    self.assertEqual(rejected.json["code"], "restore_busy")
                    wrong = client.post(path + "/dependencies/cleanup", json={
                        "operation_id": "6" * 32,
                    }, headers=self.headers())
                    self.assertEqual(wrong.status_code, 409)
                    write = native_api._write_native_restore

                    def fail_success(record):
                        if not record.get("dependency_cleanup_pending", False):
                            raise OSError("cleanup receipt storage unavailable")
                        return write(record)

                    with patch.object(native_api, "_write_native_restore", side_effect=fail_success):
                        failed = client.post(path + "/dependencies/cleanup", json={
                            "operation_id": operation_id,
                        }, headers=self.headers())
                    self.assertEqual(failed.status_code, 503, failed.json)
                    self.assertIn(request_id, native_api.RESTORE_DEPENDENCY_CLEANUP)
                    self.assertIsNone(native_api._try_process_lease(native_api.RESTORE_PROCESS_LOCK_PATH))
                    completed = client.post(path + "/dependencies/cleanup", json={
                        "operation_id": operation_id,
                    }, headers=self.headers())
                    self.assertEqual(completed.status_code, 200, completed.json)
                    self.assertFalse(completed.json["restore"]["dependencies"]["cleanup_required"])
                    self.assertEqual(completed.json["restore"]["dependencies"]["state"], "succeeded")
                    self.assertNotIn(request_id, native_api.RESTORE_DEPENDENCY_CLEANUP)
                    replayed = client.post(path + "/dependencies/cleanup", json={
                        "operation_id": operation_id,
                    }, headers=self.headers())
                    self.assertEqual(replayed.status_code, 200)
                    self.assertTrue(replayed.json["replayed"])
                    create_process.assert_called_once()
                    lease = native_api._try_process_lease(native_api.RESTORE_PROCESS_LOCK_PATH)
                    self.assertIsNotNone(lease)
                    native_api._release_process_lease(lease)

    def test_dependency_reader_failure_cannot_report_success(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import native_api

            client = self.client(app)
            preflight_id, request_id = "3" * 32, "4" * 32
            prepared = self.submit_preflight(client, preflight_id)
            real_start = threading.Thread.start

            def start(thread):
                return real_start(thread) if "-output-" in thread.name else thread.run()

            class BrokenStream(io.BytesIO):
                def read1(self, size):
                    raise OSError("stream failed")

            class Process:
                def __init__(self, *args, **kwargs):
                    self.stdout = BrokenStream()
                    self.identity = {"kind": "posix_group", "pid": 12345}

                def start(self):
                    pass

                def wait(self, timeout):
                    return 0

                def stop(self, timeout):
                    pass

            with patch.object(threading.Thread, "start", autospec=True, side_effect=start):
                self.submit_restore(
                    client, request_id, preflight_id, prepared.json["preflight"]["archive_sha256"],
                    restore_dependencies=True,
                )
                with patch.object(native_api, "DependencyProcess", Process):
                    result = client.post(f"/api/backup/native/restores/{request_id}/dependencies", json={
                        "operation_id": "5" * 32, "confirm_retry": False,
                    }, headers=self.headers())
            self.assertEqual(result.status_code, 200, result.json)
            self.assertEqual(result.json["restore"]["dependencies"]["state"], "failed")
            self.assertIn("输出读取中断", result.json["restore"]["dependencies"]["error"])
            self.assertFalse(result.json["restore"]["dependencies"]["cleanup_required"])

    @unittest.skipUnless(os.name == "posix", "POSIX dependency restart recovery")
    def test_dependency_cleanup_after_restart_never_restarts_installation_or_deletes_replaced_workspace(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import native_api

            client = self.client(app)
            preflight_id, request_id, operation_id = "3" * 32, "4" * 32, "5" * 32
            prepared = self.submit_preflight(client, preflight_id)
            real_start, real_process = threading.Thread.start, native_api.DependencyProcess

            def start(thread):
                return real_start(thread) if "-output-" in thread.name else thread.run()

            def command(_command, **kwargs):
                return real_process([sys.executable, "-c", "print('installation finished')"], **kwargs)

            path = f"/api/backup/native/restores/{request_id}"
            with patch.object(threading.Thread, "start", autospec=True, side_effect=start):
                self.submit_restore(
                    client, request_id, preflight_id, prepared.json["preflight"]["archive_sha256"],
                    restore_dependencies=True,
                )
                with patch.object(native_api, "DependencyProcess", side_effect=command), patch.object(
                    native_api.shutil, "rmtree", side_effect=PermissionError("workspace currently unavailable"),
                ):
                    response = client.post(path + "/dependencies", json={
                        "operation_id": operation_id, "confirm_retry": False,
                    }, headers=self.headers())
            self.assertTrue(response.json["restore"]["dependencies"]["cleanup_required"])
            record = native_api._read_native_restore(request_id)
            entry = native_api.RESTORE_DEPENDENCY_CLEANUP.pop(request_id)
            native_api._release_process_lease(entry["lease_fd"])
            workspace = native_api.PREFLIGHT_WORK_DIR / record["dependency_workspace"]["name"]
            original = workspace.with_name(workspace.name + "-original")
            workspace.rename(original)
            workspace.mkdir()
            sentinel = workspace / "keep.txt"
            sentinel.write_text("replacement must survive")
            with patch.object(native_api, "DependencyProcess") as no_install:
                queried = client.get(path, headers=self.headers())
                self.assertTrue(queried.json["restore"]["dependencies"]["cleanup_required"])
                failed = client.post(path + "/dependencies/cleanup", json={
                    "operation_id": operation_id,
                }, headers=self.headers())
                self.assertEqual(failed.status_code, 503, failed.json)
                self.assertEqual(sentinel.read_text(), "replacement must survive")
                self.assertTrue(original.is_dir())
                sentinel.unlink()
                workspace.rmdir()
                original.rename(workspace)
                completed = client.post(path + "/dependencies/cleanup", json={
                    "operation_id": operation_id,
                }, headers=self.headers())
                no_install.assert_not_called()
            self.assertEqual(completed.status_code, 200, completed.json)
            dependencies = completed.json["restore"]["dependencies"]
            self.assertFalse(dependencies["cleanup_required"])
            self.assertEqual(dependencies["state"], "unknown")
            self.assertEqual(dependencies["operation_id"], operation_id)
            self.assertFalse(workspace.exists())
            lease = native_api._try_process_lease(native_api.RESTORE_PROCESS_LOCK_PATH)
            self.assertIsNotNone(lease)
            native_api._release_process_lease(lease)

    def test_followup_process_lease_protects_live_state_and_replays_without_mutation(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api

            client = self.client(app)
            preflight_id, request_id, operation_id = "3" * 32, "4" * 32, "5" * 32
            prepared = self.submit_preflight(client, preflight_id)
            self.assertEqual(prepared.json["preflight"]["status"], "ready", prepared.json)
            with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()):
                restored = self.submit_restore(
                    client, request_id, preflight_id, prepared.json["preflight"]["archive_sha256"],
                    restore_dependencies=True,
                )
            self.assertEqual(restored.status_code, 200, restored.json)
            self.assertEqual(restored.json["restore"]["state"], "restored", restored.json)
            script = (
                "import json,sys\n"
                "from flask import Flask\n"
                "from fls_manager.routes.backup import native_api as api\n"
                "with Flask('lease-test').app_context():\n"
                f" record,fd,response=api._claim_restore_followup({request_id!r},'scheduler',{operation_id!r},False)\n"
                " print(json.dumps({'claimed':fd is not None}),flush=True)\n"
                " if fd is not None:\n"
                "  sys.stdin.readline()\n"
                f"  api._restore_record_update({request_id!r},scheduler_state='succeeded')\n"
                "  api._release_process_lease(fd)\n"
            )
            with tempfile.TemporaryFile(mode="w+b") as stdout:
                process = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=stdout, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 10
                    while stdout.seek(0, 2) == 0 and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    stdout.seek(0)
                    self.assertEqual(json.loads(stdout.readline()), {"claimed": True})
                    self.assertIsNone(process.poll())
                    before = native_api._native_restore_path(request_id).read_bytes()
                    path = f"/api/backup/native/restores/{request_id}"
                    queried = client.get(path, headers=self.headers())
                    self.assertEqual(queried.json["restore"]["scheduler"]["state"], "running")
                    replayed = client.post(path + "/scheduler-reload", json={
                        "operation_id": operation_id, "confirm_retry": False,
                    }, headers=self.headers())
                    self.assertTrue(replayed.json["replayed"])
                    self.assertEqual(replayed.json["operation"]["state"], "running")
                    for endpoint in ("dependencies", "scheduler-reload"):
                        rejected = client.post(path + "/" + endpoint, json={
                            "operation_id": "6" * 32, "confirm_retry": True,
                        }, headers=self.headers())
                        self.assertEqual(rejected.status_code, 409, rejected.json)
                        self.assertEqual(rejected.json["code"], "restore_busy")
                    self.assertEqual(native_api._native_restore_path(request_id).read_bytes(), before)
                    _, stderr = process.communicate(b"finish\n", timeout=10)
                    self.assertEqual(process.returncode, 0, stderr)
                    record = native_api._read_native_restore(request_id)
                    self.assertEqual(record["operations"][operation_id]["state"], "succeeded")
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=10)

    def test_followup_rereads_receipt_after_another_process_completes_before_lock(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import native_api

            client = self.client(app)
            preflight_id, request_id = "3" * 32, "4" * 32
            prepared = self.submit_preflight(client, preflight_id)
            self.assertEqual(prepared.json["preflight"]["status"], "ready", prepared.json)
            with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()):
                restored = self.submit_restore(client, request_id, preflight_id, prepared.json["preflight"]["archive_sha256"])
            self.assertEqual(restored.status_code, 200, restored.json)
            self.assertEqual(restored.json["restore"]["state"], "restored", restored.json)
            script = (
                "import json,sys\n"
                "from flask import Flask\n"
                "from fls_manager.routes.backup import native_api as api\n"
                "acquire=api._try_process_lease\n"
                "def paused_acquire(path,**kwargs):\n"
                " print('ready',flush=True)\n"
                " sys.stdin.readline()\n"
                " return acquire(path,**kwargs)\n"
                "api._try_process_lease=paused_acquire\n"
                "with Flask('race-test').app_context():\n"
                f" record,fd,response=api._claim_restore_followup({request_id!r},'scheduler',{'6' * 32!r},False)\n"
                " print(json.dumps({'claimed':fd is not None,'code':response.get_json()['code'] if response else ''}),flush=True)\n"
                " if fd is not None: api._release_process_lease(fd)\n"
            )
            with tempfile.TemporaryFile(mode="w+b") as stdout:
                process = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=stdout, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 10
                    while stdout.seek(0, 2) == 0 and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    stdout.seek(0)
                    self.assertEqual(stdout.readline().rstrip(b"\r\n"), b"ready")
                    with patch("fls_manager.scheduler.reload_scheduler") as reload_scheduler:
                        result = client.post(f"/api/backup/native/restores/{request_id}/scheduler-reload", json={
                            "operation_id": "5" * 32, "confirm_retry": False,
                        }, headers=self.headers())
                        self.assertEqual(result.status_code, 200, result.json)
                        reload_scheduler.assert_called_once()
                    before = native_api._native_restore_path(request_id).read_bytes()
                    _, stderr = process.communicate(b"continue\n", timeout=10)
                    self.assertEqual(process.returncode, 0, stderr)
                    stdout.seek(0)
                    lines = stdout.readlines()
                    self.assertEqual(json.loads(lines[-1]), {"claimed": False, "code": "scheduler_retry_confirmation_required"})
                    self.assertEqual(native_api._native_restore_path(request_id).read_bytes(), before)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=10)

    @unittest.skipUnless(os.name == "posix", "POSIX process-group integration")
    def test_restore_dependency_timeout_and_exited_parent_reap_the_process_group(self):
        for parent_exits in (False, True):
            with self.subTest(parent_exits=parent_exits), isolated_app() as (app, base):
                from fls_manager.routes.backup import native_api

                client = self.client(app)
                preflight_id, request_id = "3" * 32, "4" * 32
                prepared = self.submit_preflight(client, preflight_id)
                digest = prepared.json["preflight"]["archive_sha256"]
                with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()):
                    self.submit_restore(client, request_id, preflight_id, digest, restore_dependencies=True)
                child_pid_file = base / "dependency-child.pid"
                script = (
                    "import subprocess,sys,time,pathlib\n"
                    "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                    f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid))\n"
                    "print('dependency child running', flush=True)\n"
                    + ("" if parent_exits else "time.sleep(60)\n")
                )
                real_process, real_start = native_api.DependencyProcess, threading.Thread.start

                def command(_command, **kwargs):
                    return real_process([sys.executable, "-c", script], **kwargs)

                def start(thread):
                    if "-output-" in thread.name:
                        return real_start(thread)
                    return thread.run()

                try:
                    with patch.object(native_api, "DependencyProcess", side_effect=command), patch.object(
                        threading.Thread, "start", autospec=True, side_effect=start,
                    ), patch.object(native_api, "RESTORE_WORKER_TIMEOUT_SECONDS", 0.5):
                        response = client.post(
                            f"/api/backup/native/restores/{request_id}/dependencies",
                            json={"operation_id": "5" * 32, "confirm_retry": False}, headers=self.headers(),
                        )
                    self.assertEqual(response.status_code, 200, response.json)
                    expected_state = "succeeded" if parent_exits else "failed"
                    self.assertEqual(response.json["restore"]["dependencies"]["state"], expected_state)
                    self.assertIn("dependency child running", response.json["restore"]["dependencies"]["output"])
                    child_pid = int(child_pid_file.read_text())
                    state_path = Path(f"/proc/{child_pid}/stat")
                    deadline = time.monotonic() + 2
                    while state_path.exists() and time.monotonic() < deadline:
                        if state_path.read_text().split(") ", 1)[1].split()[0] == "Z":
                            break
                        time.sleep(0.01)
                    if state_path.exists():
                        self.assertEqual(state_path.read_text().split(") ", 1)[1].split()[0], "Z")
                    self.assertFalse(any(
                        thread.is_alive() and "backup-native-restore-deps-output-" in thread.name
                        for thread in threading.enumerate()
                    ))
                    self.assertNotIn(request_id, native_api.RESTORE_DEPENDENCY_ACTIVE)
                    self.assertFalse(list(native_api.PREFLIGHT_WORK_DIR.glob("deps-*")))
                    lease = native_api._try_process_lease(native_api.RESTORE_PROCESS_LOCK_PATH)
                    self.assertIsNotNone(lease)
                    native_api._release_process_lease(lease)
                finally:
                    if child_pid_file.exists():
                        try:
                            os.kill(int(child_pid_file.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    @contextlib.contextmanager
    def inline_worker(self):
        from fls_manager.routes.backup import _common as backup
        with patch.object(backup, "write_dependencies_file"), patch.object(
            threading.Thread, "start", autospec=True,
            side_effect=lambda thread: thread.run(),
        ) as start:
            yield start

    def test_capabilities_and_empty_pages_are_bounded_and_no_store(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup
            client = self.client(app)
            self.assertFalse(backup.BACKUP_DIR.exists())
            capabilities = client.get("/api/backup/native/capabilities", headers=self.headers())
            self.assertEqual(capabilities.status_code, 200)
            self.assertEqual(capabilities.headers["Cache-Control"], "no-store")
            self.assertEqual(capabilities.json["items"], ["data", "scripts"])
            self.assertEqual(capabilities.json["upload_limit_bytes"], 512 * 1024 * 1024)
            files = client.get("/api/backup/native/files", headers=self.headers())
            jobs = client.get("/api/backup/native/jobs", headers=self.headers())
            self.assertEqual(files.json["items"], [])
            self.assertEqual(jobs.json["items"], [])
            self.assertEqual(jobs.headers["Cache-Control"], "no-store")
            self.assertFalse(backup.BACKUP_DIR.exists())

    def test_file_pages_bind_version_and_download_open_verified_file(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup
            target = backup.backup_dir() / "native.tar.gz"
            target.write_bytes(b"native archive")
            client = self.client(app)
            page = client.get("/api/backup/native/files?limit=1", headers=self.headers())
            item = page.json["items"][0]
            self.assertEqual(item["name"], target.name)
            self.assertEqual(item["size_bytes"], len(b"native archive"))
            path = f"/api/backup/native/files/{target.name}/download?version={item['version']}"
            response = client.get(path, headers=self.headers())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"native archive")
            self.assertEqual(response.content_length, len(b"native archive"))
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            response.close()
            target.write_bytes(b"replaced archive")
            stale = client.get(path, headers=self.headers())
            self.assertEqual(stale.status_code, 409)
            self.assertNotIn(b"replaced archive", stale.data)

    def test_file_pagination_is_stable_and_download_keeps_the_verified_descriptor(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup
            from fls_manager.routes import backup as backup_routes
            directory = backup.backup_dir()
            targets = []
            for name in ("alpha.tar.gz", "bravo.tar.gz", "charlie.tar.gz"):
                target = directory / name
                target.write_bytes(name.encode("ascii"))
                targets.append(target)
            client = self.client(app)
            first = client.get("/api/backup/native/files?limit=2&page=1", headers=self.headers())
            second = client.get("/api/backup/native/files?limit=2&page=2", headers=self.headers())
            self.assertEqual(first.json["total"], 3)
            self.assertEqual(first.json["pages"], 2)
            self.assertEqual(len(first.json["items"]), 2)
            self.assertEqual(len(second.json["items"]), 1)
            all_files = client.get("/api/backup/native/files?limit=100", headers=self.headers())
            item = next(item for item in all_files.json["items"] if item["name"] == "alpha.tar.gz")
            original = targets[0].read_bytes()
            send_file = backup_routes.native_api.send_file

            def replace_path_after_open(source, **kwargs):
                targets[0].unlink()
                targets[0].symlink_to(directory / "bravo.tar.gz")
                return send_file(source, **kwargs)

            with patch.object(backup_routes.native_api, "send_file", side_effect=replace_path_after_open):
                response = client.get(
                    f"/api/backup/native/files/alpha.tar.gz/download?version={item['version']}",
                    headers=self.headers(),
                )
            self.assertEqual(response.data, original)
            response.close()

    def test_native_job_get_does_not_normalize_restart_or_cleanup_staging(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import _common as backup
            job_id = "a" * 32
            info = {
                "id": job_id,
                "items": ["scripts"],
                "type_text": "仅脚本",
                "running": True,
                "status": "正在压缩",
                "filename": "",
                "size": 0,
                "error": "/private/path secret-token",
                "created_at": "2026-11-26 10:00:00",
                "updated_at": "2026-11-26 10:01:00",
            }
            backup.persist_backup_job(info)
            backup.BACKUP_JOBS.clear()
            backup.BACKUP_CANCEL_EVENTS.clear()
            persisted = backup.BACKUP_JOBS_FILE.read_bytes()
            staging = backup.backup_dir() / backup._backup_staging_name(job_id)
            staging.write_bytes(b"preserve evidence")
            with patch.object(backup, "_remove_orphaned_backup_staging") as cleanup:
                response = self.client(app).get(
                    f"/api/backup/native/jobs/{job_id}", headers=self.headers(),
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["job"]["status"], "unknown")
            self.assertFalse(response.json["job"]["active"])
            self.assertNotIn("error", response.json["job"])
            cleanup.assert_not_called()
            self.assertEqual(backup.BACKUP_JOBS_FILE.read_bytes(), persisted)
            self.assertEqual(staging.read_bytes(), b"preserve evidence")

    def test_request_query_reads_receipt_without_replay_or_writeback(self):
        with isolated_app() as (app, base), self.inline_worker() as start:
            from fls_manager.routes.backup import _common as backup
            request_id = "b" * 32
            request_directory = base / "data/backup_requests"
            request_directory.mkdir(parents=True)
            receipt = request_directory / f"{request_id}.json"
            receipt.write_text(
                '{"request_id":"' + request_id + '","items":["scripts"],"state":"submitted"}',
            )
            backup.persist_backup_job({
                "id": request_id,
                "items": ["scripts"],
                "type_text": "仅脚本",
                "running": False,
                "status": "已完成",
                "filename": "fixture.tar.gz",
                "size": 123,
                "created_at": "2026-11-26 10:00:00",
                "updated_at": "2026-11-26 10:01:00",
            })
            before = (receipt.read_bytes(), backup.BACKUP_JOBS_FILE.read_bytes())
            with patch("fls_manager.routes.backup.native_api.submit_backup_creation") as submit:
                submit.side_effect = [
                    ({"ok": True, "msg": "备份已提交", "job_id": request_id}, 200),
                    ({"ok": True, "msg": "已找到原备份作业，未重复创建", "job_id": request_id}, 200),
                ]
                first = self.client(app).post(
                    "/api/backup/native/jobs",
                    json={"request_id": request_id, "items": ["scripts"]},
                    headers=self.headers(),
                )
                replay = self.client(app).post(
                    "/api/backup/native/jobs",
                    json={"request_id": request_id, "items": ["scripts"]},
                    headers=self.headers(),
                )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(replay.status_code, 200)
            self.assertFalse(first.json["replayed"])
            self.assertTrue(replay.json["replayed"])
            submit.assert_has_calls([
                unittest.mock.call({"request_id": request_id, "items": ["scripts"]}),
                unittest.mock.call({"request_id": request_id, "items": ["scripts"]}),
            ])
            queried = self.client(app).get(
                f"/api/backup/native/requests/{request_id}", headers=self.headers(),
            )
            self.assertEqual(queried.status_code, 200)
            self.assertEqual(queried.json["request"]["status"], "succeeded")
            self.assertEqual((receipt.read_bytes(), backup.BACKUP_JOBS_FILE.read_bytes()), before)

    def test_native_delete_receipt_makes_lost_response_replay_safe(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import _common as backup
            target = backup.backup_dir() / "delete-me.tar.gz"
            target.write_bytes(b"original archive")
            version = backup.backup_file_version(target.stat())
            request_id = "f" * 32
            client = self.client(app)
            body = {"request_id": request_id, "filename": target.name, "version": version}
            first = client.post("/api/backup/native/files/delete", json=body, headers=self.headers())
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.json["deleted"])
            self.assertFalse(first.json["replayed"])
            self.assertFalse(target.exists())
            receipt_path = base / ".backup_native_delete_requests" / f"{request_id}.json"
            self.assertEqual(receipt_path.stat().st_size < 4096, True)
            queried = client.get(f"/api/backup/native/requests/{request_id}", headers=self.headers())
            self.assertEqual(queried.json["request"]["kind"], "delete")
            self.assertEqual(queried.json["request"]["status"], "succeeded")
            target.write_bytes(b"replacement archive")
            replay = client.post("/api/backup/native/files/delete", json=body, headers=self.headers())
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["replayed"])
            self.assertTrue(target.exists())
            mismatch = client.post(
                "/api/backup/native/files/delete",
                json=dict(body, version="0" * 64), headers=self.headers(),
            )
            self.assertEqual(mismatch.status_code, 409)
            self.assertEqual(target.read_bytes(), b"replacement archive")

    def test_native_delete_unknown_receipt_never_deletes_replacement(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup
            from fls_manager.routes.backup import native_api
            target = backup.backup_dir() / "uncertain.tar.gz"
            target.write_bytes(b"original archive")
            body = {
                "request_id": "9" * 32,
                "filename": target.name,
                "version": backup.backup_file_version(target.stat()),
            }
            write_json = native_api.write_json

            def fail_terminal_receipt(path, receipt):
                if receipt.get("state") == "deleted":
                    raise OSError("fixture receipt failure")
                return write_json(path, receipt)

            client = self.client(app)
            with patch.object(native_api, "write_json", side_effect=fail_terminal_receipt):
                response = client.post("/api/backup/native/files/delete", json=body, headers=self.headers())
            self.assertEqual(response.status_code, 503)
            self.assertIsNone(response.json["deleted"])
            self.assertFalse(target.exists())
            target.write_bytes(b"replacement archive")
            replay = client.post("/api/backup/native/files/delete", json=body, headers=self.headers())
            self.assertEqual(replay.status_code, 503)
            self.assertIsNone(replay.json["deleted"])
            self.assertEqual(target.read_bytes(), b"replacement archive")
            queried = client.get(
                f"/api/backup/native/requests/{body['request_id']}", headers=self.headers(),
            )
            self.assertEqual(queried.json["request"]["status"], "unknown")

    def test_native_delete_does_not_claim_success_when_file_disappears_before_unlink(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup
            target = backup.backup_dir() / "vanishing.tar.gz"
            target.write_bytes(b"original archive")
            body = {
                "request_id": "8" * 32,
                "filename": target.name,
                "version": backup.backup_file_version(target.stat()),
            }
            entry_stat = backup.backup_entry_stat
            calls = 0

            def vanish(directory, filename):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise FileNotFoundError(filename)
                return entry_stat(directory, filename)

            with patch.object(backup, "backup_entry_stat", side_effect=vanish):
                response = self.client(app).post(
                    "/api/backup/native/files/delete", json=body, headers=self.headers(),
                )
            self.assertEqual(response.status_code, 503)
            self.assertIsNone(response.json["deleted"])
            self.assertTrue(target.exists())

    def test_cancel_response_keeps_acceptance_distinct_from_terminal_state(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup
            job_id = "1" * 32
            info = {
                "id": job_id, "items": ["data"], "type_text": "仅数据",
                "running": True, "status": "正在压缩", "filename": "", "size": 0,
                "size_text": "-", "error": "", "created_at": "2026-11-26 10:00:00",
                "updated_at": "2026-11-26 10:00:00",
            }
            event = threading.Event()
            backup.BACKUP_JOBS[job_id] = info
            backup.BACKUP_CANCEL_EVENTS[job_id] = event
            backup.persist_backup_job(info)
            client = self.client(app)
            response = client.post(
                f"/api/backup/native/jobs/{job_id}/cancel", json={}, headers=self.headers(),
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["accepted"])
            self.assertEqual(response.json["job"]["status"], "cancelling")
            self.assertTrue(event.is_set())
            self.assertTrue(response.json["job"]["active"])
            info.update(running=False, status="已取消", updated_at="2026-11-26 10:02:00")
            backup.persist_backup_job(info)
            backup.BACKUP_CANCEL_EVENTS.pop(job_id)
            terminal = client.get(f"/api/backup/native/jobs/{job_id}", headers=self.headers())
            self.assertEqual(terminal.json["job"]["status"], "cancelled")
            self.assertFalse(terminal.json["job"]["active"])

    def test_mutations_reject_duplicate_or_unknown_json_fields_and_queries(self):
        with isolated_app() as (app, _):
            client = self.client(app)
            duplicate = client.post(
                "/api/backup/native/jobs",
                data='{"request_id":"' + "c" * 32 + '","items":["data"],"items":["scripts"]}',
                content_type="application/json",
                headers=self.headers(),
            )
            self.assertEqual(duplicate.status_code, 400)
            extra = client.post(
                "/api/backup/native/jobs",
                json={"request_id": "d" * 32, "items": ["data"], "path": "/secret"},
                headers=self.headers(),
            )
            self.assertEqual(extra.status_code, 400)
            missing_delete_identity = client.post(
                "/api/backup/native/files/delete",
                json={"filename": "fixture.tar.gz", "version": "a" * 64},
                headers=self.headers(),
            )
            self.assertEqual(missing_delete_identity.status_code, 400)
            repeated = client.get(
                "/api/backup/native/files?page=1&page=2", headers=self.headers(),
            )
            self.assertEqual(repeated.status_code, 400)

    def test_request_receipt_corruption_is_not_reported_as_missing(self):
        with isolated_app() as (app, base):
            request_id = "e" * 32
            directory = base / "data/backup_requests"
            directory.mkdir(parents=True)
            (directory / f"{request_id}.json").write_text("not-json")
            response = self.client(app).get(
                f"/api/backup/native/requests/{request_id}", headers=self.headers(),
            )
            self.assertEqual(response.status_code, 500)
            self.assertNotEqual(response.json["code"], "request_not_found")


if __name__ == "__main__":
    unittest.main()
