import unittest
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN
from tests import test_native_backup_directory_recovery as directory_tests
from tests.test_notify_action_recovery import isolated_app


class NativeDirectoryMaintenanceTests(unittest.TestCase):
    request_id = "a" * 32
    prepare = directory_tests.NativeBackupDirectoryRecoveryTests.prepare
    prefix = "/api/backup/native/directory-maintenance"

    def get(self, client, suffix=""):
        return client.get(self.prefix + suffix, headers={"X-Token": TOKEN})

    def recover(self, client, version, **changes):
        return client.post(
            self.prefix + "/" + self.request_id + "/recover",
            json=dict({"confirm": True, "version": version}, **changes),
            headers={"X-Token": TOKEN},
        )

    def damage_receipt(self, *, missing=False):
        from fls_manager.routes.backup import native_api

        receipt = native_api._native_restore_path(self.request_id)
        if missing:
            receipt.unlink()
        else:
            receipt.write_bytes(b"{")
        return receipt

    def test_independent_recovery_uses_real_journal_when_receipt_is_lost(self):
        for committed in (False, True):
            for missing in (False, True):
                with self.subTest(committed=committed, missing=missing), isolated_app() as (app, base):
                    from fls_manager import backup_commit

                    entry = self.prepare(base, committed=committed)
                    receipt = self.damage_receipt(missing=missing)
                    client = app.test_client()
                    journal = base / backup_commit.JOURNAL_NAME / (self.request_id + ".json")
                    original = journal.read_bytes()
                    with patch.object(backup_commit, "recover", side_effect=AssertionError("GET mutated evidence")):
                        listing = self.get(client)
                        self.assertEqual(listing.status_code, 200, listing.json)
                        self.assertEqual(listing.headers["Cache-Control"], "no-store")
                        self.assertEqual(listing.json["total"], 1)
                        row = listing.json["items"][0]
                        self.assertEqual(row["receipt_state"], "missing" if missing else "unavailable")
                        self.assertTrue(row["recover_allowed"])
                        self.assertTrue(row["evidence_retained"])
                        self.assertEqual(row["restore_items"], ["data"])
                        self.assertEqual(self.get(client, "/" + self.request_id).json["directory"], row)
                    self.assertNotIn(str(base), str(listing.json))
                    self.assertNotIn("inode", str(listing.json))
                    self.assertEqual(journal.read_bytes(), original)
                    response = self.recover(client, row["version"])
                    self.assertEqual(response.status_code, 200, response.json)
                    result = response.json["directory"]
                    self.assertEqual(result["outcome"], "restored" if committed else "not_applied")
                    self.assertTrue(result["evidence_retained"])
                    self.assertEqual((base / "data/config.json").read_bytes(), b"new-config" if committed else b"original-config")
                    self.assertEqual((base / "data/secret_key.txt").read_bytes(), b"original-key")
                    self.assertTrue(journal.exists())
                    self.assertTrue(entry["workspace"].exists())
                    self.assertEqual(receipt.exists(), not missing)
                    if not missing:
                        self.assertEqual(receipt.read_bytes(), b"{")
                    # A lost POST response is resolved by an independent read.
                    self.assertEqual(self.get(client, "/" + self.request_id).json["directory"], result)
                    durable = journal.read_bytes()
                    self.assertEqual(self.recover(client, result["version"]).status_code, 200)
                    self.assertEqual(journal.read_bytes(), durable)
                    with self.assertRaises(RuntimeError):
                        backup_commit.assert_data_accessible(base, write=True)

    def test_original_receipt_and_active_owner_are_not_bypassed(self):
        with isolated_app() as (app, base):
            from fls_manager import backup_commit
            from fls_manager.routes.backup import native_api

            self.prepare(base)
            client = app.test_client()
            row = self.get(client).json["items"][0]
            self.assertEqual(row["receipt_state"], "available")
            self.assertFalse(row["recover_allowed"])
            self.assertEqual(self.recover(client, row["version"]).status_code, 409)
            self.damage_receipt()
            row = self.get(client).json["items"][0]
            fd = native_api._try_process_lease(native_api.RESTORE_PROCESS_LOCK_PATH)
            self.assertIsNotNone(fd)
            try:
                self.assertTrue(self.get(client).json["items"][0]["active"])
                self.assertFalse(self.get(client).json["items"][0]["recover_allowed"])
                self.assertEqual(self.recover(client, row["version"]).status_code, 409)
                self.assertEqual(backup_commit.read(base, self.request_id)["state"], "prepared")
            finally:
                native_api._release_process_lease(fd)

    def test_changed_confirmation_and_invalid_requests_have_no_side_effects(self):
        with isolated_app() as (app, base):
            from fls_manager import backup_commit

            self.prepare(base)
            self.damage_receipt()
            client = app.test_client()
            row = self.get(client).json["items"][0]
            journal = base / backup_commit.JOURNAL_NAME / (self.request_id + ".json")
            before = journal.read_bytes()
            for changes, status in (({"version": "f" * 64}, 409), ({"confirm": False}, 400), ({"confirm": 1}, 400), ({"extra": True}, 400)):
                body = dict({"confirm": True, "version": row["version"]}, **changes)
                response = client.post(self.prefix + "/" + self.request_id + "/recover", json=body, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, status, response.json)
            self.assertEqual(self.get(client, "?limit=101").status_code, 400)
            self.assertEqual(self.get(client, "?offset=-1").status_code, 400)
            self.assertEqual(self.get(client, "?limit=1&limit=2").status_code, 400)
            self.assertEqual(self.get(client, "?unknown=1").status_code, 400)
            self.assertEqual(self.get(client, "/bad").status_code, 400)
            self.assertEqual(self.get(client, "/" + "f" * 32).status_code, 404)
            self.assertEqual(journal.read_bytes(), before)
            backup_commit.recover(base, self.request_id)
            self.assertEqual(self.recover(client, row["version"]).status_code, 409)

    def test_invalid_journal_and_replaced_directory_preserve_all_evidence(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid), isolated_app() as (app, base):
                from fls_manager import backup_commit

                entry = self.prepare(base)
                self.damage_receipt()
                client = app.test_client()
                row = self.get(client).json["items"][0]
                journal = base / backup_commit.JOURNAL_NAME / (self.request_id + ".json")
                if invalid:
                    journal.write_bytes(b"{")
                    damaged = self.get(client).json["items"][0]
                    self.assertEqual(damaged["state"], "unavailable")
                    self.assertFalse(damaged["recover_allowed"])
                    self.assertEqual(damaged["version"], "")
                else:
                    (base / "data").mkdir()
                    (base / "data/new-owner").write_bytes(b"keep")
                before = journal.read_bytes()
                response = self.recover(client, row["version"])
                self.assertEqual(response.status_code, 503, response.json)
                self.assertEqual(journal.read_bytes(), before)
                self.assertTrue(entry["workspace"].exists())
                self.assertEqual((entry["workspace"] / "previous/secret_key.txt").read_bytes(), b"original-key")

    def test_authentication_is_required(self):
        with isolated_app() as (app, _):
            client = app.test_client()
            self.assertEqual(client.get(self.prefix).status_code, 401)
            self.assertIn(client.post(self.prefix + "/" + self.request_id + "/recover", json={"confirm": True, "version": "f" * 64}).status_code, (400, 401))

    def test_native_session_requires_csrf_and_empty_reads_create_no_receipts(self):
        with isolated_app() as (app, base):
            from fls_manager.native_sessions import create_native_session

            with app.app_context():
                session_id, csrf, _ = create_native_session(TOKEN, remember=True, verified=True)
            client = app.test_client()
            client.set_cookie("fls_native_session", session_id)
            response = client.get(self.prefix)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json["items"], [])
            self.assertFalse((base / ".backup_native_commits").exists())
            self.assertFalse((base / ".backup_native_restores").exists())
            endpoint = self.prefix + "/" + self.request_id + "/recover"
            body = {"confirm": True, "version": "f" * 64}
            self.assertEqual(client.post(endpoint, json=body).status_code, 400)
            self.assertEqual(client.post(endpoint, json=body, headers={"X-CSRF-Token": csrf}).status_code, 404)
