import unittest
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN
from tests.test_notify_action_recovery import isolated_app


class NativeBackupDirectoryRecoveryTests(unittest.TestCase):
    request_id = "a" * 32

    def prepare(self, base, *, committed=False):
        from fls_manager import backup_commit
        from fls_manager.routes.backup import native_api, restore

        (restore.DATA_DIR / "config.json").write_bytes(b"original-config")
        (restore.DATA_DIR / "secret_key.txt").write_bytes(b"original-key")
        source = base / "archive" / "data"
        source.mkdir(parents=True)
        (source / "config.json").write_bytes(b"new-config")
        (source / "secret_key.txt").write_bytes(b"foreign-key")
        entry = restore._stage_restore_directory(source, restore.DATA_DIR)
        native_api._ensure_private_directory(native_api.RESTORE_DIR)
        native_api._write_native_restore({
            "version": 1, "request_id": self.request_id, "preflight_id": "b" * 32,
            "archive_sha256": "c" * 64, "archive_size_bytes": 12, "filename": "full.tar.gz",
            "restore_items": ["data"], "restore_dependencies": False,
            "state": "committing", "live_state_unchanged": False,
            "scheduler_state": "pending", "scheduler_operation_id": "", "scheduler_error": "",
            "dependency_state": "not_requested", "dependency_operation_id": "",
            "dependency_error": "", "dependency_output": "", "error": "",
            "created_at": 1, "updated_at": 2,
        })
        if committed:
            restore._commit_restore_directories([entry], preserve_runtime_identity=True, request_id=self.request_id)
        else:
            rename = backup_commit._rename

            def interrupt(source, destination, identity):
                rename(source, destination, identity)
                raise KeyboardInterrupt("process interrupted")

            with patch.object(backup_commit, "_rename", side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    restore._commit_restore_directories([entry], preserve_runtime_identity=True, request_id=self.request_id)
        return entry

    def post(self, client, body=None):
        return client.post(
            f"/api/backup/native/restores/{self.request_id}/recover-directories",
            json={"confirm": True} if body is None else body, headers={"X-Token": TOKEN},
        )

    def test_read_only_status_and_confirmed_recovery_use_the_original_transaction(self):
        for committed in (False, True):
            with self.subTest(committed=committed), isolated_app() as (app, base):
                from fls_manager import backup_commit
                from fls_manager.routes.backup import native_api

                entry = self.prepare(base, committed=committed)
                client = app.test_client()
                receipt = native_api._native_restore_path(self.request_id)
                journal = base / backup_commit.JOURNAL_NAME / (self.request_id + ".json")
                before = receipt.read_bytes(), journal.read_bytes()
                for path in (f"/api/backup/native/restores/{self.request_id}", "/api/backup/native/restores"):
                    response = client.get(path, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 200, response.json)
                    row = response.json["restore"] if "restore" in response.json else response.json["items"][0]
                    self.assertTrue(row["directory_recovery_required"])
                self.assertEqual((receipt.read_bytes(), journal.read_bytes()), before)
                self.assertEqual(self.post(client, {"confirm": False}).status_code, 400)
                rejected = client.post(
                    f"/api/backup/native/restores/{self.request_id}/resolve",
                    json={"outcome": "not_applied", "confirm": True}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(rejected.status_code, 409)
                self.assertEqual(rejected.json["code"], "directory_recovery_required")
                self.assertEqual((receipt.read_bytes(), journal.read_bytes()), before)
                recovered = self.post(client)
                self.assertEqual(recovered.status_code, 200, recovered.json)
                self.assertEqual(recovered.json["restore"]["state"], "restored" if committed else "failed")
                self.assertFalse(recovered.json["restore"]["directory_recovery_required"])
                self.assertEqual((base / "data/config.json").read_bytes(), b"new-config" if committed else b"original-config")
                self.assertEqual((base / "data/secret_key.txt").read_bytes(), b"original-key")
                self.assertFalse(entry["workspace"].exists())
                self.assertFalse(journal.exists())
                durable = receipt.read_bytes()
                self.assertTrue(self.post(client).json["replayed"])
                self.assertEqual(receipt.read_bytes(), durable)

    def test_active_owner_blocks_recovery_and_new_restore(self):
        with isolated_app() as (app, base):
            from fls_manager import backup_commit
            from fls_manager.routes.backup import native_api

            self.prepare(base)
            lease = native_api._try_process_lease(native_api.RESTORE_PROCESS_LOCK_PATH)
            self.assertIsNotNone(lease)
            native_api._mark_restore_lease(lease, self.request_id, "restore")
            try:
                self.assertEqual(self.post(app.test_client()).status_code, 409)
                self.assertIsNotNone(backup_commit.read(base, self.request_id))
                self.assertFalse((base / "data").exists())
            finally:
                native_api._release_process_lease(lease)
            response = app.test_client().post("/api/backup/native/restores", json={
                "request_id": "d" * 32, "preflight_id": "b" * 32,
                "archive_sha256": "c" * 64, "restore_items": ["data"],
                "restore_dependencies": False, "confirm_data_impact": True,
            }, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 409, response.json)
            self.assertEqual(response.json["code"], "directory_recovery_required")

    def test_receipt_write_failure_preserves_recovered_identity_and_retry_evidence(self):
        with isolated_app() as (app, base):
            from fls_manager import backup_commit
            from fls_manager.routes.backup import native_api

            entry = self.prepare(base)
            with patch.object(native_api, "_write_native_restore", side_effect=OSError("disk full")):
                response = self.post(app.test_client())
            self.assertEqual(response.status_code, 503)
            self.assertEqual(backup_commit.read(base, self.request_id)["state"], "rolled_back")
            self.assertTrue(entry["workspace"].exists())
            self.assertEqual((base / "data/secret_key.txt").read_bytes(), b"original-key")
            self.assertEqual(self.post(app.test_client()).status_code, 200)
            self.assertFalse(entry["workspace"].exists())

    def test_corrupt_evidence_or_scope_mismatch_is_retained_for_maintenance(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), isolated_app() as (app, base):
                from fls_manager import backup_commit
                from fls_manager.routes.backup import native_api

                entry = self.prepare(base)
                journal = base / backup_commit.JOURNAL_NAME / (self.request_id + ".json")
                if corrupt:
                    journal.write_bytes(b"{")
                else:
                    record = native_api._read_native_restore(self.request_id)
                    record["restore_items"] = ["scripts"]
                    native_api._write_native_restore(record)
                before = journal.read_bytes()
                response = app.test_client().get(
                    f"/api/backup/native/restores/{self.request_id}", headers={"X-Token": TOKEN},
                )
                self.assertEqual(response.status_code, 200, response.json)
                self.assertTrue(response.json["restore"]["directory_recovery_required"])
                response = self.post(app.test_client())
                self.assertEqual(response.status_code, 503 if corrupt else 409, response.json)
                self.assertEqual(journal.read_bytes(), before)
                self.assertEqual((entry["workspace"] / "previous/secret_key.txt").read_bytes(), b"original-key")
