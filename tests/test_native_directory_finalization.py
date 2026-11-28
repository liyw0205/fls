import unittest
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN
from tests import test_native_directory_maintenance as maintenance_tests
from tests.test_notify_action_recovery import isolated_app


class NativeDirectoryFinalizationTests(unittest.TestCase):
    request_id = "a" * 32
    prefix = "/api/backup/native/directory-maintenance"
    prepare = maintenance_tests.NativeDirectoryMaintenanceTests.prepare
    damage_receipt = maintenance_tests.NativeDirectoryMaintenanceTests.damage_receipt
    get = maintenance_tests.NativeDirectoryMaintenanceTests.get
    recover = maintenance_tests.NativeDirectoryMaintenanceTests.recover

    def finish(self, client, evidence_version, **changes):
        return client.post(
            self.prefix + "/" + self.request_id + "/finalize",
            json=dict({"confirm": True, "version": evidence_version}, **changes), headers={"X-Token": TOKEN},
        )

    def recovered(self, app, base, *, committed=False, missing=False):
        entry = self.prepare(base, committed=committed)
        self.damage_receipt(missing=missing)
        client = app.test_client()
        row = self.get(client).json["items"][0]
        response = self.recover(client, row["version"])
        self.assertEqual(response.status_code, 200, response.json)
        return entry, client, response.json["directory"]

    def test_finalize_preserves_real_copies_and_bad_receipt_and_releases_only_original_gate(self):
        for committed in (False, True):
            for missing in (False, True):
                with self.subTest(committed=committed, missing=missing), isolated_app() as (app, base):
                    from fls_manager import backup_commit
                    from fls_manager.routes.backup import directory_evidence

                    entry, client, row = self.recovered(app, base, committed=committed, missing=missing)
                    self.assertTrue(row["finalize_allowed"])
                    journal = base / backup_commit.JOURNAL_NAME / (self.request_id + ".json")
                    journal_bytes = journal.read_bytes()
                    with patch.object(backup_commit, "finish", side_effect=AssertionError("deleted evidence")):
                        response = self.finish(client, row["version"])
                    self.assertEqual(response.status_code, 200, response.json)
                    result = response.json["directory"]
                    self.assertEqual(result["state"], "finalized")
                    self.assertEqual(result["outcome"], "restored" if committed else "not_applied")
                    self.assertTrue(result["evidence_retained"])
                    self.assertFalse(result["finalize_allowed"])
                    self.assertFalse(result["recover_allowed"])
                    archive = base / directory_evidence.ARCHIVE_NAME / self.request_id
                    self.assertEqual((archive / "journal.json").read_bytes(), journal_bytes)
                    self.assertEqual((archive / "restore.json").exists(), not missing)
                    if not missing:
                        self.assertEqual((archive / "restore.json").read_bytes(), b"{")
                    self.assertTrue(entry["workspace"].exists())
                    copy = entry["workspace"] / ("previous" if committed else "replacement") / "config.json"
                    self.assertEqual(copy.read_bytes(), b"original-config" if committed else b"new-config")
                    self.assertEqual((base / "data/secret_key.txt").read_bytes(), b"original-key")
                    self.assertEqual((base / "data/config.json").read_bytes(), b"new-config" if committed else b"original-config")
                    backup_commit.assert_data_accessible(base, write=True)
                    with patch.object(backup_commit, "_rename", side_effect=AssertionError("read/replay mutated evidence")):
                        self.assertEqual(self.get(client, "/" + self.request_id).json["directory"], result)
                        self.assertEqual(self.get(client).json["items"], [result])
                        self.assertEqual(self.finish(client, row["version"]).json["directory"], result)
                        self.assertEqual(self.finish(client, result["version"]).status_code, 200)
                    self.assertNotIn(str(base), str(response.json))
                    self.assertEqual(client.get("/api/backup/native/restores", headers={"X-Token": TOKEN}).json["items"], [])
                    reused = client.post("/api/backup/native/restores", headers={"X-Token": TOKEN}, json={
                        "request_id": self.request_id, "preflight_id": "b" * 32,
                        "archive_sha256": "c" * 64, "restore_items": ["data"],
                        "restore_dependencies": False, "confirm_data_impact": True,
                    })
                    self.assertEqual(reused.status_code, 409, reused.json)
                    self.assertEqual(reused.json["code"], "restore_id_archived")

    def test_each_interrupted_move_remains_queryable_and_resumes_the_same_intent(self):
        for number in (1, 2):
            for moved in (False, True):
                with self.subTest(number=number, moved=moved), isolated_app() as (app, base):
                    from fls_manager import backup_commit
                    from fls_manager.routes.backup import directory_evidence

                    entry, client, row = self.recovered(app, base)
                    rename = backup_commit._rename
                    calls = 0

                    def interrupt(source, destination, identity):
                        nonlocal calls
                        calls += 1
                        if calls == number and not moved:
                            raise OSError("interrupted before move")
                        rename(source, destination, identity)
                        if calls == number and moved:
                            raise OSError("response lost after durable move")

                    with patch.object(backup_commit, "_rename", side_effect=interrupt):
                        response = self.finish(client, row["version"])
                    self.assertEqual(response.status_code, 503, response.json)
                    archive = base / directory_evidence.ARCHIVE_NAME / self.request_id
                    intent = (archive / "intent.json").read_bytes()
                    with patch.object(backup_commit, "_rename", side_effect=AssertionError("GET mutated evidence")):
                        result = self.get(client, "/" + self.request_id)
                    self.assertEqual(result.status_code, 200, result.json)
                    complete = number == 2 and moved
                    self.assertEqual(result.json["directory"]["state"], "finalized" if complete else "finalizing")
                    self.assertEqual(bool(backup_commit.pending(base)), not complete)
                    self.assertTrue(entry["workspace"].exists())
                    response = self.finish(client, row["version"])
                    self.assertEqual(response.status_code, 200, response.json)
                    self.assertEqual(response.json["directory"]["state"], "finalized")
                    self.assertEqual((archive / "intent.json").read_bytes(), intent)
                    self.assertEqual((archive / "restore.json").read_bytes(), b"{")
                    backup_commit.assert_data_accessible(base, write=True)

    def test_unrecovered_healthy_active_and_dependency_evidence_block_finalization(self):
        with isolated_app() as (app, base):
            from fls_manager import backup_commit
            from fls_manager.routes.backup import native_api

            self.prepare(base)
            client = app.test_client()
            row = self.get(client).json["items"][0]
            self.assertEqual(self.finish(client, row["version"]).status_code, 409)
            self.damage_receipt()
            row = self.get(client).json["items"][0]
            self.assertFalse(row["finalize_allowed"])
            self.assertEqual(self.finish(client, row["version"]).status_code, 409)
            row = self.recover(client, row["version"]).json["directory"]
            fd = native_api._try_process_lease(native_api.RESTORE_PROCESS_LOCK_PATH)
            try:
                self.assertFalse(self.get(client).json["items"][0]["finalize_allowed"])
                self.assertEqual(self.finish(client, row["version"]).status_code, 409)
            finally:
                native_api._release_process_lease(fd)
            workspace = native_api.PREFLIGHT_WORK_DIR / ("deps-" + self.request_id[:8] + "-retained")
            workspace.mkdir(parents=True)
            (workspace / "keep").write_bytes(b"dependency evidence")
            self.assertFalse(self.get(client).json["items"][0]["finalize_allowed"])
            self.assertEqual(self.finish(client, row["version"]).status_code, 409)
            self.assertEqual((workspace / "keep").read_bytes(), b"dependency evidence")
            self.assertEqual(backup_commit.pending(base), [self.request_id])

    def test_raw_damaged_receipt_changes_invalidate_confirmation(self):
        with isolated_app() as (app, base):
            from fls_manager import backup_commit
            from fls_manager.routes.backup import native_api, directory_evidence

            _, client, row = self.recovered(app, base)
            native_api._native_restore_path(self.request_id).write_bytes(b"[[")
            response = self.finish(client, row["version"])
            self.assertEqual(response.status_code, 409, response.json)
            self.assertFalse((base / directory_evidence.ARCHIVE_NAME).exists())
            self.assertEqual(backup_commit.pending(base), [self.request_id])
            current = self.get(client).json["items"][0]
            self.assertNotEqual(current["version"], row["version"])
            self.assertEqual(self.finish(client, current["version"]).status_code, 200)

    def test_bad_archived_evidence_and_replaced_live_directory_cannot_be_finalized(self):
        for damaged in ("intent", "journal", "directory"):
            with self.subTest(damaged=damaged), isolated_app() as (app, base):
                from fls_manager import backup_commit
                from fls_manager.routes.backup import directory_evidence

                entry, client, row = self.recovered(app, base)
                if damaged == "directory":
                    (base / "data").rename(base / "data-retained")
                    (base / "data").mkdir()
                    response = self.finish(client, row["version"])
                    self.assertEqual(response.status_code, 503, response.json)
                    self.assertEqual(backup_commit.pending(base), [self.request_id])
                else:
                    self.assertEqual(self.finish(client, row["version"]).status_code, 200)
                    archive = base / directory_evidence.ARCHIVE_NAME / self.request_id
                    (archive / ("intent.json" if damaged == "intent" else "journal.json")).write_bytes(b"{")
                    result = self.get(client, "/" + self.request_id)
                    self.assertEqual(result.json["directory"]["state"], "unavailable")
                    self.assertFalse(result.json["directory"]["finalize_allowed"])
                    self.assertEqual(self.finish(client, row["version"]).status_code, 503)
                self.assertTrue(entry["workspace"].exists())

    def test_finalization_requires_exact_confirmation_and_session_csrf(self):
        with isolated_app() as (app, base):
            from fls_manager.native_sessions import create_native_session

            with app.app_context():
                session_id, csrf, _ = create_native_session(TOKEN, remember=True, verified=True)
            _, client, row = self.recovered(app, base)
            for changes in ({"confirm": False}, {"confirm": 1}, {"version": "bad"}, {"extra": True}):
                self.assertEqual(self.finish(client, row["version"], **changes).status_code, 400)
            browser = app.test_client()
            browser.set_cookie("fls_native_session", session_id)
            path = self.prefix + "/" + self.request_id + "/finalize"
            body = {"confirm": True, "version": row["version"]}
            self.assertEqual(browser.post(path, json=body).status_code, 400)
            self.assertEqual(browser.post(path, json=body, headers={"X-CSRF-Token": csrf}).status_code, 200)

    def test_intent_publication_failure_keeps_original_evidence_and_write_gate(self):
        with isolated_app() as (app, base):
            from fls_manager import backup_commit
            from fls_manager.routes.backup import directory_evidence, native_api

            entry, client, row = self.recovered(app, base)
            journal = base / backup_commit.JOURNAL_NAME / (self.request_id + ".json")
            original = journal.read_bytes()
            with patch.object(directory_evidence, "publish_receipt", side_effect=OSError("disk full")):
                response = self.finish(client, row["version"])
            self.assertEqual(response.status_code, 503, response.json)
            self.assertEqual(journal.read_bytes(), original)
            self.assertEqual(native_api._native_restore_path(self.request_id).read_bytes(), b"{")
            self.assertTrue(entry["workspace"].exists())
            self.assertEqual(self.get(client).json["items"][0], row)
            with self.assertRaises(RuntimeError):
                backup_commit.assert_data_accessible(base, write=True)
            self.assertEqual(self.finish(client, row["version"]).status_code, 200)
