import contextlib
import threading
import unittest
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN
from tests.test_notify_action_recovery import isolated_app


class NativeBackupApiTests(unittest.TestCase):
    def client(self, app):
        return app.test_client()

    def headers(self):
        return {"X-Token": TOKEN}

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
