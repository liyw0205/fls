import contextlib
import io
import tarfile
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

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
