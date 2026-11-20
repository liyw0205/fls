import io
import hashlib
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN
from tests.test_high_side_effect_workflows import restore_test_archive
from tests.test_notify_action_recovery import isolated_app
from tests import test_proxy_action_recovery as proxy_tests


class BackupRestoreIdempotencyTests(unittest.TestCase):
    authenticate = proxy_tests.ProxyActionRecoveryTests.authenticate

    def post(self, client, archive, **fields):
        return client.post("/api/backup/import", data={"file": (io.BytesIO(archive), "restore.tar.gz"),
                           "restore_request_id": "c" * 32, "restore_items": ["data", "scripts"], **fields},
                           headers={"X-Token": TOKEN})

    def test_replay_after_response_loss_preserves_later_edits_and_receipt_survives_data_replace(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore
            raw = restore_test_archive().getvalue()
            with patch.object(restore, "_commit_restore_directories", wraps=restore._commit_restore_directories) as commit:
                response = self.post(app.test_client(), raw)
                self.assertEqual(response.status_code, 200)
                self.assertTrue((base / ".backup_restore_requests" / ("c" * 32 + ".json")).exists())
                script = base / "scripts/demo.py"
                script.write_text("print('later')")
                (base / "data/after.json").write_text("later")
                self.assertEqual(self.post(app.test_client(), raw).status_code, 200)
                self.assertEqual(script.read_text(), "print('later')")
                self.assertEqual((base / "data/after.json").read_text(), "later")
                self.assertEqual(commit.call_count, 1)
            query = app.test_client().get("/api/backup/restore/" + "c" * 32, headers={"X-Token": TOKEN})
            self.assertEqual(query.status_code, 200)
            self.assertTrue(query.json["resolved"])

    def test_same_identity_rejects_changed_file_or_scope_without_mutation(self):
        with isolated_app() as (app, base):
            raw = restore_test_archive().getvalue()
            self.assertEqual(self.post(app.test_client(), raw).status_code, 200)
            before = (base / "data/config.json").read_bytes()
            for archive, fields in ((raw, {"restore_items": ["scripts"]}),
                                    (restore_test_archive({"extra.json": b"{}"}).getvalue(), {}),
                                    (raw, {"restore_deps": "1"})):
                response = self.post(app.test_client(), archive, **fields)
                self.assertEqual(response.status_code, 409)
                self.assertTrue(response.json["request_conflict"])
                self.assertEqual((base / "data/config.json").read_bytes(), before)

    def test_receipt_failures_before_and_after_commit_never_repeat_uncertain_replace(self):
        for mode in ("received_before", "received_after", "committing_before", "committing_after", "restored_before", "restored_after"):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                from fls_manager.routes.backup import restore
                raw = restore_test_archive().getvalue()
                save = restore._save_restore_request
                def receipt(record):
                    if mode.startswith(record["state"]):
                        if mode.endswith("after"):
                            save(record)
                        raise OSError("fixture restore receipt fault")
                    return save(record)
                with patch.object(restore, "_save_restore_request", side_effect=receipt), patch.object(restore, "_commit_restore_directories", wraps=restore._commit_restore_directories) as commit:
                    response = self.post(app.test_client(), raw)
                    committed = mode.startswith("restored")
                    self.assertEqual(commit.call_count, int(committed))
                    self.assertEqual(response.status_code, 503 if committed else 500)
                if committed:
                    (base / "scripts/demo.py").write_text("later")
                with patch.object(restore, "_commit_restore_directories", wraps=restore._commit_restore_directories) as commit:
                    replay = self.post(app.test_client(), raw)
                    if committed:
                        self.assertIn(replay.status_code, (200, 503))
                        self.assertEqual((base / "scripts/demo.py").read_text(), "later")
                        commit.assert_not_called()
                    else:
                        self.assertEqual(replay.status_code, 200)
                        commit.assert_called_once()

    def test_persisted_committing_and_unknown_never_reimport_after_restart(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore
            raw = restore_test_archive().getvalue()
            self.assertEqual(self.post(app.test_client(), raw).status_code, 200)
            path = base / ".backup_restore_requests" / ("c" * 32 + ".json")
            record = json.loads(path.read_text())
            record["state"] = "committing"
            restore._save_restore_request(record)
            (base / "scripts/demo.py").write_text("later")
            with patch.object(restore, "_commit_restore_directories") as commit:
                query = app.test_client().get("/api/backup/restore/" + "c" * 32, headers={"X-Token": TOKEN})
                self.assertEqual(query.status_code, 503)
                self.assertTrue(query.json["restore_outcome_unknown"])
                self.assertEqual(self.post(app.test_client(), raw).status_code, 503)
                commit.assert_not_called()
            self.assertEqual((base / "scripts/demo.py").read_text(), "later")

    def test_scoped_reload_auth_csrf_result_failure_and_idempotent_replay(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore
            raw = restore_test_archive().getvalue()
            with patch.object(restore, "reload_scheduler", side_effect=OSError("scheduler unavailable")):
                self.assertEqual(self.post(app.test_client(), raw).status_code, 503)
            url = "/api/backup/restore/" + "c" * 32 + "/reload"
            client = app.test_client()
            self.assertEqual(client.post(url).status_code, 400)
            self.authenticate(client)
            self.assertEqual(client.post(url, headers={"X-CSRF-Token": "bad"}).status_code, 400)
            with patch("fls_manager.auth.query_token_enabled", return_value=False):
                self.assertEqual(client.post(url + "?token=" + TOKEN, headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 403)
            with client.session_transaction() as session:
                session["token_expire_at"] = 1
            self.assertEqual(client.post(url, headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 401)
            with patch.object(restore, "reload_scheduler", side_effect=OSError("still unavailable")):
                self.assertEqual(client.post(url, headers={"X-Token": TOKEN}).status_code, 503)
            (base / "scripts/demo.py").write_text("later")
            for _ in range(2):
                self.assertEqual(client.post(url, headers={"X-Token": TOKEN}).status_code, 200)
            self.assertEqual(client.get(url.removesuffix("/reload"), headers={"X-Token": TOKEN}).status_code, 200)
            with patch.object(restore, "reload_scheduler") as reload:
                self.assertEqual(client.post(url, headers={"X-Token": TOKEN}).status_code, 200)
                reload.assert_not_called()
            self.assertEqual((base / "scripts/demo.py").read_text(), "later")

    def test_commit_after_effect_fault_stays_unknown_and_preserves_later_change(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore
            raw = restore_test_archive().getvalue()
            commit = restore._commit_restore_directories
            def publish(entries):
                commit(entries)
                raise OSError("fixture confirmation lost")
            with patch.object(restore, "_commit_restore_directories", side_effect=publish):
                response = self.post(app.test_client(), raw)
            self.assertEqual(response.status_code, 503)
            self.assertTrue(response.json["restore_outcome_unknown"])
            (base / "scripts/demo.py").write_text("later")
            with patch.object(restore, "_commit_restore_directories") as commit:
                self.assertEqual(self.post(app.test_client(), raw).status_code, 503)
                commit.assert_not_called()
            self.assertEqual((base / "scripts/demo.py").read_text(), "later")

    def test_retention_archives_only_resolved_successes_and_keeps_recovery_receipts(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore
            request_ids = [f"{number:032x}" for number in range(1, 11)]
            states = (
                ("restored", False, False), ("restored", False, False),
                ("restored", False, False), ("restored", False, False),
                ("failed", False, False), ("unknown", False, False),
                ("restored", True, False), ("restored", False, True),
                ("received", False, False), ("committing", False, False),
            )
            directory = base / ".backup_restore_requests"
            directory.mkdir()
            with patch.object(restore, "RESTORE_RECEIPT_DETAIL_LIMIT", 2):
                for number, (request_id, (state, reload_required, dependencies_failed)) in enumerate(zip(request_ids, states), 1):
                    restore._save_restore_request({
                        "request_id": request_id,
                        "archive_sha256": f"{number:064x}",
                        "filename": f"restore-{number}.tar.gz",
                        "restore_items": ["data", "scripts"],
                        "restore_deps": False,
                        "state": state,
                        "scheduler_reload_required": reload_required,
                        "scheduler_reloaded": not reload_required,
                        "dependencies_failed": dependencies_failed,
                        "created_at": f"2026-11-{number:02d} 00:00:00",
                    })
                self.assertEqual(restore._compact_restore_receipts(), 2)
                for request_id in request_ids[:2]:
                    self.assertFalse(restore._restore_request_path(request_id).exists())
                for request_id in request_ids[2:]:
                    self.assertTrue(restore._restore_request_path(request_id).exists())

                response = app.test_client().get(
                    "/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["total"], 10)
                archived = {item["request_id"] for item in response.json["items"] if item["archived"]}
                self.assertEqual(archived, set(request_ids[:2]))
                for request_id in request_ids[4:]:
                    self.assertIn(request_id, {item["request_id"] for item in response.json["items"]})

    def test_archive_failure_keeps_details_and_history_retries_maintenance(self):
        for fail_after_archive in (False, True):
            with self.subTest(fail_after_archive=fail_after_archive), isolated_app() as (app, base):
                from fls_manager.routes.backup import restore

                directory = base / ".backup_restore_requests"
                directory.mkdir()
                ids = ("a" * 32, "b" * 32)
                for number, request_id in enumerate(ids, 1):
                    restore._save_restore_request({
                        "request_id": request_id,
                        "archive_sha256": f"{number:064x}",
                        "filename": "restore.tar.gz",
                        "restore_items": ["scripts"],
                        "restore_deps": False,
                        "state": "restored",
                        "scheduler_reload_required": False,
                        "scheduler_reloaded": True,
                        "dependencies_failed": False,
                        "created_at": f"2026-11-{number:02d} 00:00:00",
                    })

                with patch.object(restore, "RESTORE_RECEIPT_DETAIL_LIMIT", 1):
                    if fail_after_archive:
                        unlink = Path.unlink
                        target = restore._restore_request_path(ids[0])

                        def fail_unlink(path, *args, **kwargs):
                            if path == target:
                                raise OSError("fixture archived detail cleanup failure")
                            return unlink(path, *args, **kwargs)

                        patcher = patch.object(Path, "unlink", fail_unlink)
                    else:
                        write_json = restore.write_json

                        def fail_index(path, record):
                            if path == restore._restore_archive_index_path():
                                raise OSError("fixture archive index write failure")
                            return write_json(path, record)

                        patcher = patch.object(restore, "write_json", fail_index)

                    with patcher:
                        response = app.test_client().get(
                            "/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN},
                        )
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json["maintenance_error"])
                    archived = [item for item in response.json["items"] if item["archived"]]
                    self.assertEqual(len(archived), int(fail_after_archive))
                    if not fail_after_archive:
                        self.assertTrue(all(restore._restore_request_path(request_id).exists() for request_id in ids))

                    recovered = app.test_client().get(
                        "/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(recovered.status_code, 200)
                    self.assertEqual(recovered.json["maintenance_error"], "")
                    self.assertEqual(sum(item["archived"] for item in recovered.json["items"]), 1)
                    self.assertEqual(sum(restore._restore_request_path(request_id).exists() for request_id in ids), 1)

    def test_corrupt_or_symlinked_archive_index_fails_closed(self):
        for mode in ("invalid_json", "invalid_shape", "symlink"):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                from fls_manager.routes.backup import restore

                directory = base / ".backup_restore_requests"
                directory.mkdir()
                index_path = restore._restore_archive_index_path()
                if mode == "invalid_json":
                    index_path.write_text("{broken", encoding="utf-8")
                elif mode == "invalid_shape":
                    index_path.write_text(json.dumps({"version": 2, "records": {}}), encoding="utf-8")
                else:
                    target = base / "archive-index-target.json"
                    target.write_text(json.dumps({"version": 1, "records": {}}), encoding="utf-8")
                    index_path.symlink_to(target)

                client = app.test_client()
                history = client.get("/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN})
                self.assertEqual(history.status_code, 500)
                with patch.object(restore, "_commit_restore_directories") as commit:
                    replay = self.post(client, restore_test_archive().getvalue())
                    self.assertEqual(replay.status_code, 500)
                    commit.assert_not_called()

                self.assertTrue(index_path.is_symlink() if mode == "symlink" else index_path.exists())

    def test_archived_request_replay_is_rejected_for_api_and_form_without_directory_changes(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore

            request_id = "a" * 32
            raw = restore_test_archive().getvalue()
            directory = base / ".backup_restore_requests"
            directory.mkdir()
            for number, receipt_id in enumerate((request_id, "b" * 32), 1):
                restore._save_restore_request({
                    "request_id": receipt_id,
                    "archive_sha256": hashlib.sha256(raw).hexdigest(),
                    "filename": "restore.tar.gz",
                    "restore_items": ["data", "scripts"],
                    "restore_deps": False,
                    "state": "restored",
                    "scheduler_reload_required": False,
                    "scheduler_reloaded": True,
                    "dependencies_failed": False,
                    "created_at": f"2026-11-{number:02d} 00:00:00",
                })
            with patch.object(restore, "RESTORE_RECEIPT_DETAIL_LIMIT", 1):
                restore._compact_restore_receipts()
            self.assertFalse(restore._restore_request_path(request_id).exists())
            with patch.object(restore, "_commit_restore_directories") as commit:
                for api in (True, False):
                    client = app.test_client()
                    headers = {"X-Token": TOKEN}
                    data = {
                        "file": (io.BytesIO(raw), "restore.tar.gz"),
                        "restore_request_id": request_id,
                        "restore_items": ["data", "scripts"],
                    }
                    if not api:
                        self.authenticate(client)
                        data["csrf_token"] = "proxy-csrf"
                        headers = {}
                    response = client.post("/api/backup/import" if api else "/backup/import", data=data, headers=headers)
                    self.assertEqual(response.status_code, 410)
                    if api:
                        self.assertTrue(response.json["archived"])
                        self.assertIn("不会再次覆盖", response.json["msg"])
                commit.assert_not_called()

    def test_reload_receipt_failure_can_be_queried_and_retried_without_directory_write(self):
        for published in (False, True):
            with self.subTest(published=published), isolated_app() as (app, base):
                from fls_manager.routes.backup import restore
                with patch.object(restore, "reload_scheduler", side_effect=OSError("initial failure")):
                    self.assertEqual(self.post(app.test_client(), restore_test_archive().getvalue()).status_code, 503)
                (base / "scripts/demo.py").write_text("later")
                save = restore._save_restore_request
                def receipt(record):
                    if published:
                        save(record)
                    raise OSError("reload receipt confirmation failure")
                url = "/api/backup/restore/" + "c" * 32
                with patch.object(restore, "_save_restore_request", side_effect=receipt):
                    self.assertEqual(app.test_client().post(url + "/reload", headers={"X-Token": TOKEN}).status_code, 503)
                self.assertEqual(app.test_client().get(url, headers={"X-Token": TOKEN}).status_code, 200 if published else 503)
                with patch.object(restore, "_commit_restore_directories") as commit:
                    self.assertEqual(app.test_client().post(url + "/reload", headers={"X-Token": TOKEN}).status_code, 200)
                    commit.assert_not_called()
                self.assertEqual((base / "scripts/demo.py").read_text(), "later")

    def test_concurrent_duplicate_queries_do_not_reclassify_active_restore_or_deadlock(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import restore
            raw = restore_test_archive().getvalue()
            entered, release = threading.Event(), threading.Event()
            commit = restore._commit_restore_directories
            def delayed(entries):
                entered.set()
                self.assertTrue(release.wait(5))
                return commit(entries)
            with patch.object(restore, "_commit_restore_directories", side_effect=delayed) as apply, ThreadPoolExecutor(3) as pool:
                first = pool.submit(self.post, app.test_client(), raw)
                self.assertTrue(entered.wait(3))
                query = pool.submit(lambda: app.test_client().get("/api/backup/restore/" + "c" * 32, headers={"X-Token": TOKEN}))
                second = pool.submit(self.post, app.test_client(), raw)
                release.set()
                self.assertEqual(first.result(4).status_code, 200)
                self.assertEqual(query.result(4).status_code, 200)
                self.assertEqual(second.result(4).status_code, 200)
                apply.assert_called_once()

    def test_corrupt_or_linked_receipts_and_permissions_preserve_original(self):
        for raw in ("{", "null", "{}", "[]"):
            with self.subTest(raw=raw), isolated_app() as (app, base):
                from fls_manager.routes.backup import restore
                path = base / ".backup_restore_requests" / ("c" * 32 + ".json")
                path.parent.mkdir()
                path.write_text(raw)
                with patch.object(restore, "_commit_restore_directories") as commit:
                    self.assertEqual(self.post(app.test_client(), restore_test_archive().getvalue()).status_code, 500)
                    self.assertEqual(app.test_client().get("/api/backup/restore/" + "c" * 32, headers={"X-Token": TOKEN}).status_code, 500)
                    commit.assert_not_called()
                self.assertEqual(path.read_text(), raw)
        with isolated_app() as (app, base):
            path = base / ".backup_restore_requests"
            path.symlink_to(base / "scripts", target_is_directory=True)
            self.assertEqual(self.post(app.test_client(), restore_test_archive().getvalue()).status_code, 500)
            self.assertFalse((base / "scripts" / ("c" * 32 + ".json")).exists())
        with isolated_app() as (app, base):
            path = base / ".backup_restore_requests" / ("c" * 32 + ".json")
            path.parent.mkdir()
            os.mkfifo(path)
            self.assertEqual(self.post(app.test_client(), restore_test_archive().getvalue()).status_code, 500)
            self.assertEqual(app.test_client().get("/api/backup/restore/" + "c" * 32, headers={"X-Token": TOKEN}).status_code, 500)
        with isolated_app() as (app, _):
            client = app.test_client()
            self.assertEqual(client.get("/api/backup/restore/" + "c" * 32).status_code, 401)
            self.authenticate(client)
            with client.session_transaction() as session:
                session["token_expire_at"] = 1
            self.assertEqual(client.get("/api/backup/restore/" + "c" * 32).status_code, 401)

    def test_invalid_identity_creates_no_receipt_and_never_extracts(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore
            client = app.test_client()
            with patch.object(restore, "extract_archive") as extract:
                self.assertEqual(self.post(client, restore_test_archive().getvalue(), restore_request_id="bad").status_code, 400)
                self.assertEqual(client.get("/api/backup/restore/bad", headers={"X-Token": TOKEN}).status_code, 400)
                self.assertEqual(client.post("/api/backup/restore/bad/reload", headers={"X-Token": TOKEN}).status_code, 400)
                extract.assert_not_called()
            self.assertFalse((base / ".backup_restore_requests").exists())
