import json
import threading
import time
import unittest
from unittest.mock import patch

from tests import test_backup_dependency_cleanup_workers as cleanup_tests
from tests.test_notify_action_recovery import isolated_app


class DependencyMaintenanceTests(unittest.TestCase):
    def idle_orphan(self, app, base):
        from tests.test_backup_dependency_evidence import DependencyEvidenceTests
        from fls_manager.routes.backup import native_api as api

        helper = cleanup_tests.BackupDependencyCleanupWorkerTests()
        client, headers, _ = helper.prepare(app)
        evidence, workspace, record = DependencyEvidenceTests().fixture(base)
        api._native_restore_path(helper.request_id).write_bytes(b"{damaged original")
        return helper, client, headers, evidence, workspace, record

    def item(self, helper):
        return "/api/backup/native/dependency-maintenance/" + helper.request_id + "/" + helper.operation_id

    def wait_finalized(self, helper, client, path, headers):
        def complete():
            response = client.get(path, headers=headers)
            self.assertEqual(response.status_code, 200, response.json)
            return response.json["dependency"] if response.json["dependency"]["state"] == "finalized" else None

        return helper.wait_for(complete, "independent dependency maintenance did not finalize")

    def test_peer_cleanup_with_corrupt_or_missing_original_preserves_bytes_and_unknown_result(self):
        for missing in (False, True):
            with self.subTest(missing=missing), isolated_app() as (app, base):
                from fls_manager.routes.backup import dependency_evidence as evidence, native_api as api

                helper = cleanup_tests.BackupDependencyCleanupWorkerTests()
                client, headers, restore_path = helper.prepare(app)
                path = self.item(helper)
                with helper.owner(base) as owner:
                    original = api._native_restore_path(helper.request_id)
                    damaged = b"{damaged original restore receipt"
                    if missing:
                        original.unlink()
                    else:
                        original.write_bytes(damaged)
                    before = evidence._path(base, helper.request_id, helper.operation_id).read_bytes()
                    attempts = (base / "cleanup-count").read_bytes()
                    for _ in range(3):
                        response = client.get(path, headers=headers)
                        self.assertEqual(response.status_code, 200, response.json)
                        item = response.json["dependency"]
                        self.assertTrue(item["cleanup_allowed"])
                        self.assertEqual(item["original_result"], "unknown")
                        listed = client.get("/api/backup/native/dependency-maintenance?limit=1&offset=0", headers=headers)
                        self.assertEqual(listed.status_code, 200, listed.json)
                        self.assertEqual(listed.json["items"], [item])
                    self.assertEqual(evidence._path(base, helper.request_id, helper.operation_id).read_bytes(), before)
                    self.assertEqual((base / "cleanup-count").read_bytes(), attempts)
                    accepted = client.post(path + "/cleanup", json={"confirm": True, "version": item["version"]}, headers=headers)
                    self.assertEqual(accepted.status_code, 202, accepted.json)
                    final = self.wait_finalized(helper, client, path, headers)
                    self.assertEqual(final["resource_state"], "cleaned")
                    self.assertEqual(final["original_result"], "unknown")
                    self.assertFalse(final["cleanup_allowed"])
                    self.assertFalse(original.exists())
                    archive = base / ".backup_native_dependency_maintenance" / (helper.request_id + "-" + helper.operation_id)
                    if not missing:
                        self.assertEqual((archive / "restore.json").read_bytes(), damaged)
                    else:
                        self.assertFalse((archive / "restore.json").exists())
                    self.assertIsNone(owner.poll())
                    helper.assert_available(api)
                    self.assertFalse(list(api.PREFLIGHT_WORK_DIR.glob("deps-*")))
                    self.assertEqual((base / "install-count").read_bytes().count(b"installation"), 1)
                    replay = client.post(path + "/cleanup", json={"confirm": True, "version": item["version"]}, headers=headers)
                    self.assertEqual(replay.status_code, 200, replay.json)
                    self.assertEqual(replay.json["dependency"]["state"], "finalized")
                    self.assertEqual((base / "cleanup-count").read_bytes(), b"attempt\nattempt\n")
                    created = client.post("/api/backup/native/restores", json={
                        "request_id": helper.request_id, "preflight_id": helper.preflight_id,
                        "archive_sha256": "a" * 64, "restore_items": ["data"],
                        "restore_dependencies": True, "confirm_data_impact": True,
                    }, headers=headers)
                    self.assertEqual(created.status_code, 409, created.json)
                    self.assertEqual(created.json["code"], "restore_id_archived")
                    self.assertEqual(client.get(restore_path, headers=headers).status_code, 404)

    def test_healthy_receipt_live_install_stale_confirmation_and_unknown_fields_never_cleanup(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api as api

            helper = cleanup_tests.BackupDependencyCleanupWorkerTests()
            client, headers, _ = helper.prepare(app)
            (base / "pause-install").touch()
            path = self.item(helper)
            with helper.owner(base):
                response = client.get(path, headers=headers)
                self.assertEqual(response.status_code, 200, response.json)
                self.assertFalse(response.json["dependency"]["cleanup_allowed"])
                self.assertTrue(response.json["dependency"]["owner_active"])
                original = api._native_restore_path(helper.request_id)
                saved = original.read_bytes()
                original.write_bytes(b"{live install receipt damaged")
                response = client.get(path, headers=headers)
                self.assertFalse(response.json["dependency"]["cleanup_allowed"])
                denied = client.post(path + "/cleanup", json={"confirm": True, "version": response.json["dependency"]["version"]}, headers=headers)
                self.assertEqual(denied.status_code, 409, denied.json)
                original.write_bytes(saved)
                (base / "pause-install").unlink()
                helper.wait_for(lambda: (base / "initial-cleanup-failed").exists(), "cleanup did not fail")
                helper.wait_for(lambda: api._restore_lease_matches(api._read_native_restore(helper.request_id), "dependency-cleanup"), "owner not ready")
                healthy = client.get(path, headers=headers).json["dependency"]
                self.assertFalse(healthy["cleanup_allowed"])
                denied = client.post(path + "/cleanup", json={"confirm": True, "version": healthy["version"]}, headers=headers)
                self.assertEqual(denied.status_code, 409, denied.json)
                original.write_bytes(b"{first damaged bytes")
                stale = client.get(path, headers=headers).json["dependency"]
                original.write_bytes(b"{different damaged bytes")
                denied = client.post(path + "/cleanup", json={"confirm": True, "version": stale["version"]}, headers=headers)
                self.assertEqual(denied.status_code, 409, denied.json)
                self.assertEqual((base / "cleanup-count").read_bytes(), b"attempt\n")
                current = client.get(path, headers=headers).json["dependency"]
                for body in ({"confirm": False, "version": current["version"]},
                             {"confirm": True, "version": current["version"], "pid": 1}):
                    self.assertEqual(client.post(path + "/cleanup", json=body, headers=headers).status_code, 400)

    def test_idle_crashed_owner_requires_explicit_new_attempt_and_never_reinstalls(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api as api

            helper = cleanup_tests.BackupDependencyCleanupWorkerTests()
            client, headers, _ = helper.prepare(app)
            path = self.item(helper)
            (base / "pause-cleanup").touch()
            with helper.owner(base) as owner:
                api._native_restore_path(helper.request_id).write_bytes(b"{bad original")
                item = client.get(path, headers=headers).json["dependency"]
                accepted = client.post(path + "/cleanup", json={"confirm": True, "version": item["version"]}, headers=headers)
                self.assertEqual(accepted.status_code, 202, accepted.json)
                helper.wait_for(lambda: (base / "cleanup-entered").exists(), "owner never consumed request")
                owner.kill()
                owner.wait(timeout=10)
                helper.assert_available(api)
                before = (base / "cleanup-count").read_bytes()
                item = client.get(path, headers=headers).json["dependency"]
                self.assertTrue(item["cleanup_allowed"])
                self.assertEqual((base / "cleanup-count").read_bytes(), before)
                with patch.object(api, "DependencyProcess") as no_install:
                    retried = client.post(path + "/cleanup", json={"confirm": True, "version": item["version"]}, headers=headers)
                    self.assertEqual(retried.status_code, 202, retried.json)
                    self.wait_finalized(helper, client, path, headers)
                    no_install.assert_not_called()
                helper.assert_available(api)
                self.assertEqual((base / "install-count").read_bytes().count(b"installation"), 1)

    def test_failed_lease_mark_never_leaves_a_stale_entry_or_cleaning_flag(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api as api

            helper, client, headers, evidence, workspace, record = self.idle_orphan(app, base)
            path = self.item(helper)
            item = client.get(path, headers=headers).json["dependency"]
            with patch.object(api, "_mark_restore_lease", side_effect=OSError("lease metadata unavailable")):
                response = client.post(path + "/cleanup", json={"confirm": True, "version": item["version"]}, headers=headers)
            self.assertEqual(response.status_code, 503, response.json)
            self.assertNotIn(helper.request_id, api.RESTORE_DEPENDENCY_CLEANUP)
            self.assertNotIn(helper.request_id, api.RESTORE_DEPENDENCY_CLEANING)
            self.assertEqual(evidence.read(base, helper.request_id, helper.operation_id), record)
            self.assertTrue(workspace.is_dir())
            helper.assert_available(api)
            current = client.get(path, headers=headers).json["dependency"]
            self.assertTrue(current["cleanup_allowed"])

    def test_cleanup_failure_and_failed_owner_metadata_keep_explicit_retry_available(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import dependency_maintenance as maintenance, native_api as api

            helper, client, headers, _, workspace, _ = self.idle_orphan(app, base)
            path = self.item(helper)
            item = client.get(path, headers=headers).json["dependency"]
            real_mark = api._mark_restore_lease

            def mark(fd, request_id, kind, operation_id=""):
                if kind == "dependency-cleanup":
                    raise OSError("owner metadata temporarily unavailable")
                return real_mark(fd, request_id, kind, operation_id)

            with patch.object(threading.Thread, "start", autospec=True) as no_start:
                accepted = client.post(path + "/cleanup", json={"confirm": True, "version": item["version"]}, headers=headers)
            self.assertEqual(accepted.status_code, 202, accepted.json)
            self.assertEqual(no_start.call_count, 1)
            entry = api.RESTORE_DEPENDENCY_CLEANUP[helper.request_id]
            intent = maintenance.read(base, helper.request_id, helper.operation_id)
            try:
                with patch.object(api, "_cleanup_dependency_resources", side_effect=OSError("resource still busy")), patch.object(
                    api, "_mark_restore_lease", side_effect=mark,
                ):
                    self.assertFalse(api._execute_orphan_dependency_cleanup(app, helper.request_id, entry, intent))
                self.assertTrue(workspace.is_dir())
                self.assertNotIn(helper.request_id, api.RESTORE_DEPENDENCY_CLEANING)
                api._start_dependency_cleanup_owner(app, helper.request_id, entry)
                helper.wait_for(lambda: client.get(path, headers=headers).json["dependency"]["cleanup_allowed"], "failed cleanup cannot be retried")
                self.assertEqual(maintenance.read(base, helper.request_id, helper.operation_id)["status"], "failed")
                current = client.get(path, headers=headers).json["dependency"]
                response = client.post(path + "/cleanup", json={"confirm": True, "version": current["version"]}, headers=headers)
                self.assertEqual(response.status_code, 202, response.json)
                self.wait_finalized(helper, client, path, headers)
            finally:
                with api.RESTORE_LOCK:
                    api.RESTORE_DEPENDENCY_CLEANUP.pop(helper.request_id, None)
                    api._release_process_lease(entry.get("lease_fd"))
                    entry["lease_fd"] = None

    def test_durable_resource_publication_failure_requires_new_confirmation_without_get_cleanup(self):
        for after_replace in (False, True):
            with self.subTest(after_replace=after_replace), isolated_app() as (app, base):
                from fls_manager.routes.backup import native_api as api

                helper, client, headers, evidence, workspace, _ = self.idle_orphan(app, base)
                path = self.item(helper)
                publish = evidence._publish

                def fail_cleaned(base, record):
                    if record["state"] == "cleaned":
                        if after_replace:
                            publish(base, record)
                        raise OSError("cleaned acknowledgement unavailable")
                    return publish(base, record)

                with patch.object(evidence, "_publish", side_effect=fail_cleaned):
                    item = client.get(path, headers=headers).json["dependency"]
                    response = client.post(path + "/cleanup", json={"confirm": True, "version": item["version"]}, headers=headers)
                    self.assertEqual(response.status_code, 202, response.json)
                    helper.wait_for(lambda: client.get(path, headers=headers).json["dependency"]["state"] == "cleanup_failed", "publication failure was not retained")
                self.assertFalse(workspace.exists())
                original = api._native_restore_path(helper.request_id)
                self.assertEqual(original.read_bytes(), b"{damaged original")
                before = evidence._path(base, helper.request_id, helper.operation_id).read_bytes()
                for _ in range(3):
                    current = client.get(path, headers=headers).json["dependency"]
                    self.assertTrue(current["cleanup_allowed"])
                self.assertEqual(evidence._path(base, helper.request_id, helper.operation_id).read_bytes(), before)
                response = client.post(path + "/cleanup", json={"confirm": True, "version": current["version"]}, headers=headers)
                self.assertEqual(response.status_code, 202, response.json)
                self.wait_finalized(helper, client, path, headers)
                helper.assert_available(api)

    def test_archival_failure_before_or_after_move_can_be_explicitly_finished(self):
        for after_move in (False, True):
            with self.subTest(after_move=after_move), isolated_app() as (app, base):
                from fls_manager.routes.backup import dependency_maintenance as maintenance, native_api as api

                helper, client, headers, _, workspace, _ = self.idle_orphan(app, base)
                path = self.item(helper)
                real_rename = maintenance.backup_commit._rename

                def fail_move(source, destination, identity):
                    if after_move:
                        real_rename(source, destination, identity)
                    raise OSError("archive acknowledgement unavailable")

                with patch.object(maintenance.backup_commit, "_rename", side_effect=fail_move):
                    item = client.get(path, headers=headers).json["dependency"]
                    accepted = client.post(path + "/cleanup", json={"confirm": True, "version": item["version"]}, headers=headers)
                    self.assertEqual(accepted.status_code, 202, accepted.json)
                    helper.wait_for(lambda: helper.request_id not in api.RESTORE_DEPENDENCY_CLEANING
                                    and api._dependency_owner_kind(helper.request_id, helper.operation_id) == "cleanup_owner", "cleanup did not stop")
                current = client.get(path, headers=headers).json["dependency"]
                self.assertEqual(current["state"], "finalizing")
                self.assertTrue(current["cleanup_allowed"])
                self.assertFalse(workspace.exists())
                before = maintenance.read(base, helper.request_id, helper.operation_id)
                for _ in range(3):
                    client.get(path, headers=headers)
                self.assertEqual(maintenance.read(base, helper.request_id, helper.operation_id), before)
                with patch.object(api, "_cleanup_dependency_resources") as no_cleanup:
                    response = client.post(path + "/cleanup", json={"confirm": True, "version": current["version"]}, headers=headers)
                    self.assertEqual(response.status_code, 200, response.json)
                    self.assertEqual(response.json["dependency"]["state"], "finalized")
                    no_cleanup.assert_not_called()
                archived = base / maintenance.DIRECTORY_NAME / (helper.request_id + "-" + helper.operation_id) / "restore.json"
                self.assertEqual(archived.read_bytes(), b"{damaged original")
                helper.assert_available(api)

    def test_replaced_workspace_changed_evidence_and_missing_proof_never_authorize_cleanup(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import native_api as api

            helper, client, headers, evidence, workspace, record = self.idle_orphan(app, base)
            path = self.item(helper)
            item = client.get(path, headers=headers).json["dependency"]
            original = workspace.with_name(workspace.name + "-original")
            workspace.rename(original)
            workspace.mkdir()
            (workspace / "keep").write_bytes(b"replacement")
            with patch.object(api, "_cleanup_dependency_resources") as no_cleanup:
                rejected = client.post(path + "/cleanup", json={"confirm": True, "version": item["version"]}, headers=headers)
                self.assertEqual(rejected.status_code, 409, rejected.json)
                current = client.get(path, headers=headers).json["dependency"]
                self.assertEqual(current["state"], "unavailable")
                self.assertFalse(current["cleanup_allowed"])
                self.assertEqual((workspace / "keep").read_bytes(), b"replacement")
                evidence_path = evidence._path(base, helper.request_id, helper.operation_id)
                evidence_path.write_bytes(b"{broken independent evidence")
                listed = client.get("/api/backup/native/dependency-maintenance", headers=headers)
                self.assertEqual(listed.status_code, 200, listed.json)
                self.assertEqual(listed.json["items"][0]["state"], "unavailable")
                evidence_path.unlink()
                self.assertEqual(client.get(path, headers=headers).status_code, 404)
                self.assertEqual(client.post(path + "/cleanup", json={"confirm": True, "version": item["version"]}, headers=headers).status_code, 404)
                no_cleanup.assert_not_called()

    def test_auth_csrf_pagination_and_two_operation_identity_are_enforced(self):
        with isolated_app() as (app, base):
            from fls_manager.native_sessions import create_native_session
            from fls_manager.routes.backup import native_api as api
            from tests.test_bulk_workflows import TOKEN

            helper, client, headers, evidence, _, record = self.idle_orphan(app, base)
            path = self.item(helper)
            anonymous = app.test_client()
            self.assertEqual(anonymous.get(path).status_code, 401)
            self.assertEqual(anonymous.post(path + "/cleanup", json={}).status_code, 400)
            with app.app_context():
                session_id, csrf, _ = create_native_session(TOKEN, remember=True, verified=True)
            anonymous.set_cookie("fls_native_session", session_id)
            self.assertEqual(anonymous.get(path).status_code, 200)
            item = anonymous.get(path).json["dependency"]
            denied = anonymous.post(path + "/cleanup", json={"confirm": True, "version": item["version"]})
            self.assertEqual(denied.status_code, 400, denied.json)
            self.assertFalse((base / ".backup_native_dependency_maintenance").exists())
            second = evidence.create(base, helper.request_id, "d" * 32, record["workspace"])
            listing = "/api/backup/native/dependency-maintenance"
            first = client.get(listing + "?limit=1&offset=0", headers=headers)
            other = client.get(listing + "?limit=1&offset=1", headers=headers)
            self.assertEqual(first.json["total"], 2)
            self.assertTrue(first.json["has_more"])
            self.assertFalse(other.json["has_more"])
            self.assertEqual(other.json["items"][0]["operation_id"], second["operation_id"])
            self.assertEqual(client.get(listing + "?limit=1&offset=2", headers=headers).json["items"], [])
            for query in ("limit=0", "limit=101", "offset=-1", "offset=100001", "limit=x", "limit=1&limit=2", "unknown=true"):
                self.assertEqual(client.get(listing + "?" + query, headers=headers).status_code, 400, query)
            for bad_path in (path.replace(helper.request_id, "invalid"), path.replace(helper.operation_id, "invalid")):
                self.assertEqual(client.get(bad_path, headers=headers).status_code, 400)
            self.assertTrue(client.get(path, headers=headers).json["dependency"]["cleanup_allowed"])
            self.assertFalse(api.RESTORE_DEPENDENCY_CLEANUP)

    def test_thread_start_failure_and_old_requested_intent_do_not_execute_cleanup(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import dependency_maintenance as maintenance, native_api as api

            helper, client, headers, _, workspace, _ = self.idle_orphan(app, base)
            path = self.item(helper)
            item = client.get(path, headers=headers).json["dependency"]
            with patch.object(threading.Thread, "start", side_effect=RuntimeError("thread unavailable")):
                response = client.post(path + "/cleanup", json={"confirm": True, "version": item["version"]}, headers=headers)
            self.assertEqual(response.status_code, 503, response.json)
            self.assertTrue(workspace.exists())
            entry = api.RESTORE_DEPENDENCY_CLEANUP[helper.request_id]
            try:
                self.assertNotIn(helper.request_id, api.RESTORE_DEPENDENCY_CLEANING)
                current = client.get(path, headers=headers).json["dependency"]
                self.assertTrue(current["cleanup_allowed"])
                with api.RESTORE_LOCK, maintenance.transaction(base):
                    old = maintenance.request_cleanup(base, entry["evidence"], maintenance.read(base, helper.request_id, helper.operation_id)["receipt"], current["version"])
                api._start_dependency_cleanup_owner(app, helper.request_id, entry)
                helper.wait_for(lambda: maintenance.read(base, helper.request_id, helper.operation_id)["status"] == "failed", "old attempt was not rejected")
                time.sleep(0.15)
                self.assertTrue(workspace.exists())
                self.assertEqual(maintenance.read(base, helper.request_id, helper.operation_id)["attempt_id"], old["attempt_id"])
                current = client.get(path, headers=headers).json["dependency"]
                response = client.post(path + "/cleanup", json={"confirm": True, "version": current["version"]}, headers=headers)
                self.assertEqual(response.status_code, 202, response.json)
                self.wait_finalized(helper, client, path, headers)
            finally:
                with api.RESTORE_LOCK:
                    api.RESTORE_DEPENDENCY_CLEANUP.pop(helper.request_id, None)
                    api._release_process_lease(entry.get("lease_fd"))
                    entry["lease_fd"] = None

    def test_maintenance_result_publication_precedes_raw_archival_and_can_be_reconfirmed(self):
        for after_replace in (False, True):
            with self.subTest(after_replace=after_replace), isolated_app() as (app, base):
                from fls_manager.routes.backup import dependency_maintenance as maintenance, native_api as api

                helper, client, headers, _, workspace, _ = self.idle_orphan(app, base)
                path = self.item(helper)
                publish = maintenance._publish

                def fail_result(base, row):
                    if row["status"] == "resources_cleaned":
                        if after_replace:
                            publish(base, row)
                        raise OSError("maintenance result durability acknowledgement lost")
                    return publish(base, row)

                with patch.object(maintenance, "_publish", side_effect=fail_result):
                    item = client.get(path, headers=headers).json["dependency"]
                    response = client.post(path + "/cleanup", json={"confirm": True, "version": item["version"]}, headers=headers)
                    self.assertEqual(response.status_code, 202, response.json)
                    helper.wait_for(lambda: api._dependency_owner_kind(helper.request_id, helper.operation_id) == "cleanup_owner"
                                    and helper.request_id not in api.RESTORE_DEPENDENCY_CLEANING, "resource confirmation did not stop")
                self.assertFalse(workspace.exists())
                original = api._native_restore_path(helper.request_id)
                self.assertEqual(original.read_bytes(), b"{damaged original")
                row = maintenance.read(base, helper.request_id, helper.operation_id)
                self.assertEqual(row["status"], "resources_cleaned" if after_replace else "failed")
                current = client.get(path, headers=headers).json["dependency"]
                response = client.post(path + "/cleanup", json={"confirm": True, "version": current["version"]}, headers=headers)
                self.assertIn(response.status_code, (200, 202), response.json)
                self.wait_finalized(helper, client, path, headers)
                helper.assert_available(api)
