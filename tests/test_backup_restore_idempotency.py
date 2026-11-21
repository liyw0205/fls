import io
import hashlib
import json
import os
import subprocess
import sys
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
        for failure_point in ("backup_index", "primary_index", "backup_dir_sync", "primary_dir_sync", "detail_cleanup"):
            with self.subTest(failure_point=failure_point), isolated_app() as (app, base):
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
                    if failure_point == "detail_cleanup":
                        unlink = Path.unlink
                        target = restore._restore_request_path(ids[0])

                        def fail_unlink(path, *args, **kwargs):
                            if path == target:
                                raise OSError("fixture archived detail cleanup failure")
                            return unlink(path, *args, **kwargs)

                        patcher = patch.object(Path, "unlink", fail_unlink)
                    else:
                        write_index = restore._write_restore_archive_index_file
                        target = (restore._restore_archive_backup_path() if failure_point.startswith("backup")
                                  else restore._restore_archive_index_path())

                        def fail_index(path, index):
                            if path == target:
                                if failure_point.endswith("dir_sync"):
                                    with patch.object(restore, "_sync_restore_archive_directory",
                                                      side_effect=OSError("fixture directory fsync failure")):
                                        return write_index(path, index)
                                raise OSError(f"fixture {failure_point} write failure")
                            return write_index(path, index)

                        patcher = patch.object(restore, "_write_restore_archive_index_file", fail_index)

                    with patcher:
                        response = app.test_client().get(
                            "/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN},
                        )
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json["maintenance_error"])
                    self.assertEqual(response.json["maintenance_required"], failure_point in (
                        "backup_index", "primary_index", "backup_dir_sync",
                    ))
                    archived = [item for item in response.json["items"] if item["archived"]]
                    self.assertEqual(len(archived), int(failure_point != "backup_index"))
                    self.assertTrue(all(restore._restore_request_path(request_id).exists() for request_id in ids))

                    if failure_point == "primary_dir_sync":
                        with patch.object(restore, "_sync_restore_archive_directory",
                                          side_effect=OSError("fixture directory fsync still unavailable")):
                            pending = app.test_client().get(
                                "/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN},
                            )
                        self.assertEqual(pending.status_code, 200)
                        self.assertIn("fsync still unavailable", pending.json["maintenance_error"])
                        self.assertTrue(all(restore._restore_request_path(request_id).exists() for request_id in ids))

                    recovered = app.test_client().get(
                        "/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(recovered.status_code, 200)
                    self.assertFalse(recovered.json["maintenance_required"])
                    if failure_point in ("primary_index", "backup_dir_sync", "primary_dir_sync"):
                        self.assertIn("自动合并并修复", recovered.json["maintenance_error"])
                    else:
                        self.assertEqual(recovered.json["maintenance_error"], "")
                    self.assertEqual(sum(item["archived"] for item in recovered.json["items"]), 1)
                    self.assertEqual(sum(restore._restore_request_path(request_id).exists() for request_id in ids), 1)
                    primary = restore._read_restore_archive_index()
                    backup = restore._read_restore_archive_index_file(restore._restore_archive_backup_path())
                    self.assertEqual(primary, backup)
                    settled = app.test_client().get(
                        "/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN},
                    )
                    if recovered.json.get("maintenance"):
                        self.assertEqual(settled.json["maintenance"]["event_id"],
                                         recovered.json["maintenance"]["event_id"])
                        event_id = recovered.json["maintenance"]["event_id"]
                        cleared = app.test_client().post(
                            f"/api/backup/restore-maintenance/{event_id}/ack", headers={"X-Token": TOKEN},
                        )
                        self.assertEqual(cleared.status_code, 200)
                    settled = app.test_client().get(
                        "/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(settled.json["maintenance_error"], "")

    def test_corrupt_index_copy_recovers_from_valid_peer_and_reports_maintenance(self):
        for corrupt, damage in ((copy, mode) for copy in ("primary", "backup")
                                for mode in ("invalid_json", "invalid_shape", "missing")):
            with self.subTest(corrupt=corrupt, damage=damage), isolated_app() as (app, base):
                from fls_manager.routes.backup import restore

                raw = restore_test_archive().getvalue()
                directory = base / ".backup_restore_requests"
                directory.mkdir()
                request_ids = ("a" * 32, "b" * 32)
                for number, request_id in enumerate(request_ids, 1):
                    restore._save_restore_request({
                        "request_id": request_id,
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
                    self.assertEqual(restore._compact_restore_receipts(), 1)
                corrupt_path = (restore._restore_archive_index_path() if corrupt == "primary"
                                else restore._restore_archive_backup_path())
                if damage == "missing":
                    corrupt_path.unlink()
                else:
                    corrupt_path.write_text("{damaged" if damage == "invalid_json" else
                                            json.dumps({"version": 2, "records": {}}), encoding="utf-8")

                query = app.test_client().get(
                    "/api/backup/restore/" + request_ids[0], headers={"X-Token": TOKEN},
                )
                self.assertEqual(query.status_code, 410)
                self.assertTrue(query.json["archived"])
                self.assertFalse(query.json["maintenance_required"])
                self.assertIn("自动合并并修复", query.json["maintenance_error"])
                event_id = query.json["maintenance"]["event_id"]

                history = app.test_client().get(
                    "/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN},
                )
                self.assertEqual(history.status_code, 200)
                self.assertIn("自动合并并修复", history.json["maintenance_error"])
                self.assertEqual(history.json["maintenance"]["event_id"], event_id)
                archived_ids = {item["request_id"] for item in history.json["items"] if item["archived"]}
                self.assertEqual(archived_ids, {request_ids[0]})
                self.assertEqual(restore._read_restore_archive_index(),
                                 restore._read_restore_archive_index_file(restore._restore_archive_backup_path()))

                with patch.object(restore, "_commit_restore_directories") as commit:
                    replay = self.post(app.test_client(), raw, restore_request_id=request_ids[0])
                self.assertEqual(replay.status_code, 410)
                self.assertEqual(replay.json["maintenance"]["event_id"], event_id)
                commit.assert_not_called()

                html = app.test_client().post(
                    "/backup/import",
                    data={
                        "file": (io.BytesIO(raw), "restore.tar.gz"),
                        "restore_request_id": request_ids[0],
                        "restore_items": ["data", "scripts"],
                        "restore_deps": "0",
                    },
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(html.status_code, 410)
                html_text = html.get_data(as_text=True)
                self.assertIn(event_id, html_text)
                self.assertIn(request_ids[0], html_text)
                self.assertIn("确认已读并清除维护提示", html_text)
                self.assertTrue(restore._restore_request_path(request_ids[0]).exists() is False)

    def test_archive_maintenance_event_survives_restart_concurrency_and_ack_retries(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore

            raw = restore_test_archive().getvalue()
            request_id = "e" * 32
            record = {
                "request_id": request_id,
                "archive_sha256": hashlib.sha256(raw).hexdigest(),
                "filename": "restore.tar.gz",
                "restore_items": ["data", "scripts"],
                "restore_deps": False,
                "state": "restored",
                "scheduler_reload_required": False,
                "scheduler_reloaded": True,
                "dependencies_failed": False,
                "archived": True,
                "archived_at": "2026-11-21 00:00:00",
            }
            index = {"version": 1, "records": {request_id: record}}
            directory = base / ".backup_restore_requests"
            directory.mkdir()
            restore._write_restore_archive_index_pair(index)
            restore._restore_archive_index_path().write_text("{damaged", encoding="utf-8")

            process_code = (
                "import json; from fls_manager.routes.backup import restore; "
                "from fls_manager.storage import storage_transaction; "
                "ctx=storage_transaction(); ctx.__enter__(); "
                "lock=restore._restore_archive_process_lock(); lock.__enter__(); "
                "index=restore._read_restore_archive_index(); "
                "state=restore._read_restore_archive_maintenance_state(); "
                "print(json.dumps({'records':len(index['records']),'event_id':state['event_id']})); "
                "lock.__exit__(None,None,None); ctx.__exit__(None,None,None)"
            )

            def recover_in_process(_):
                result = subprocess.run(
                    [sys.executable, "-c", process_code],
                    cwd=Path(__file__).resolve().parents[1],
                    env=os.environ.copy(), capture_output=True, text=True, timeout=30,
                )
                if result.returncode:
                    raise AssertionError(result.stderr)
                return json.loads(result.stdout)

            with ThreadPoolExecutor(4) as pool:
                process_results = list(pool.map(recover_in_process, range(4)))
            self.assertEqual({item["event_id"] for item in process_results},
                             {restore._read_restore_archive_maintenance_state()["event_id"]})

            clients = [app.test_client() for _ in range(4)]
            with ThreadPoolExecutor(4) as pool:
                responses = list(pool.map(
                    lambda client: client.get(
                        "/api/backup/restore/" + request_id, headers={"X-Token": TOKEN},
                    ),
                    clients,
                ))
            self.assertEqual([response.status_code for response in responses], [410] * 4)
            maintenance = [response.json["maintenance"] for response in responses]
            self.assertTrue(all(item == maintenance[0] for item in maintenance))
            first_event = maintenance[0]["event_id"]
            self.assertFalse(maintenance[0]["required"])

            state_path = restore._restore_archive_maintenance_path()
            state_bytes = state_path.read_bytes()
            self.assertNotIn(request_id.encode(), state_bytes)
            self.assertNotIn(b"restore.tar.gz", state_bytes)
            restarted = subprocess.run(
                [sys.executable, "-c", (
                    "import json; from fls_manager.routes.backup import restore; "
                    "print(json.dumps(restore._read_restore_archive_maintenance_state()))"
                )],
                cwd=Path(__file__).resolve().parents[1],
                env=os.environ.copy(), capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            self.assertEqual(json.loads(restarted.stdout)["event_id"], first_event)

            missing_csrf = app.test_client().post(
                f"/api/backup/restore-maintenance/{first_event}/ack",
            )
            self.assertEqual(missing_csrf.status_code, 400)
            with patch.object(restore, "_commit_restore_directories") as commit:
                replay = self.post(app.test_client(), raw, restore_request_id=request_id)
            self.assertEqual(replay.status_code, 410)
            self.assertEqual(replay.json["maintenance"]["event_id"], first_event)
            commit.assert_not_called()

            cleared = app.test_client().post(
                f"/api/backup/restore-maintenance/{first_event}/ack", headers={"X-Token": TOKEN},
            )
            self.assertEqual(cleared.status_code, 200)
            self.assertTrue(restore._read_restore_archive_maintenance_state()["acknowledged"])

            restore._restore_archive_index_path().write_text("{damaged again", encoding="utf-8")
            second = app.test_client().get(
                "/api/backup/restore/" + request_id, headers={"X-Token": TOKEN},
            )
            self.assertEqual(second.status_code, 410)
            second_event = second.json["maintenance"]["event_id"]
            self.assertNotEqual(second_event, first_event)
            stale = app.test_client().post(
                f"/api/backup/restore-maintenance/{first_event}/ack", headers={"X-Token": TOKEN},
            )
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json["maintenance"]["event_id"], second_event)
            self.assertEqual(restore._read_restore_archive_maintenance_state()["event_id"], second_event)

            with patch.object(restore, "_write_restore_archive_maintenance_state",
                              side_effect=OSError("fixture maintenance acknowledgement write failure")):
                failed_clear = app.test_client().post(
                    f"/api/backup/restore-maintenance/{second_event}/ack", headers={"X-Token": TOKEN},
                )
            self.assertEqual(failed_clear.status_code, 503)
            self.assertTrue(state_path.exists())
            self.assertEqual(failed_clear.json["maintenance"]["event_id"], second_event)
            retried_clear = app.test_client().post(
                f"/api/backup/restore-maintenance/{second_event}/ack", headers={"X-Token": TOKEN},
            )
            self.assertEqual(retried_clear.status_code, 200)
            self.assertTrue(restore._read_restore_archive_maintenance_state()["acknowledged"])

    def test_maintenance_ack_directory_fsync_failure_recovers_after_restart(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore

            directory = base / ".backup_restore_requests"
            directory.mkdir()
            restore._write_restore_archive_index_pair({"version": 1, "records": {}})
            restore._restore_archive_index_path().write_text("{damaged", encoding="utf-8")
            history = app.test_client().get(
                "/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN},
            )
            self.assertEqual(history.status_code, 200)
            event_id = history.json["maintenance"]["event_id"]
            state_path = restore._restore_archive_maintenance_path()

            sync_calls = 0

            def fail_ack_sync():
                nonlocal sync_calls
                sync_calls += 1
                if sync_calls >= 2:
                    raise OSError("fixture acknowledgement directory fsync failure")

            with patch.object(restore, "_sync_restore_archive_directory", side_effect=fail_ack_sync):
                failed = app.test_client().post(
                    f"/api/backup/restore-maintenance/{event_id}/ack", headers={"X-Token": TOKEN},
                )
            self.assertEqual(failed.status_code, 503)
            self.assertEqual(failed.json["maintenance"]["event_id"], event_id)
            self.assertTrue(state_path.exists())
            self.assertTrue(restore._read_restore_archive_maintenance_state().get("acknowledged"))

            restarted = subprocess.run(
                [sys.executable, "-c", (
                    "import json; from fls_manager.routes.backup import restore; "
                    "state=restore._read_restore_archive_maintenance_state(); "
                    "payload=restore._restore_archive_maintenance_payload(state); "
                    "print(json.dumps({'state':state,'payload':payload}))"
                )],
                cwd=Path(__file__).resolve().parents[1],
                env=os.environ.copy(), capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            restarted_state = json.loads(restarted.stdout)
            self.assertEqual(restarted_state["state"]["event_id"], event_id)
            self.assertIsNone(restarted_state["payload"])

            retry = app.test_client().post(
                f"/api/backup/restore-maintenance/{event_id}/ack", headers={"X-Token": TOKEN},
            )
            self.assertEqual(retry.status_code, 200)
            self.assertTrue(retry.json["cleared"])
            self.assertTrue(restore._read_restore_archive_maintenance_state()["acknowledged"])
            queried = app.test_client().get(
                "/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN},
            )
            self.assertIsNone(queried.json["maintenance"])

    def test_archive_maintenance_publication_failure_and_corrupt_receipt_fail_closed(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore

            raw = restore_test_archive().getvalue()
            request_id = "f" * 32
            record = {
                "request_id": request_id,
                "archive_sha256": hashlib.sha256(raw).hexdigest(),
                "filename": "restore.tar.gz",
                "restore_items": ["scripts"],
                "restore_deps": False,
                "state": "restored",
                "scheduler_reload_required": False,
                "scheduler_reloaded": True,
                "dependencies_failed": False,
                "archived": True,
            }
            directory = base / ".backup_restore_requests"
            directory.mkdir()
            restore._write_restore_archive_index_pair({"version": 1, "records": {request_id: record}})
            primary = restore._restore_archive_index_path()
            backup_before = restore._restore_archive_backup_path().read_bytes()
            primary.write_text("{damaged", encoding="utf-8")

            with patch.object(restore, "_write_restore_archive_maintenance_state",
                              side_effect=OSError("fixture maintenance publication failure")):
                failed = app.test_client().get(
                    "/api/backup/restore/" + request_id, headers={"X-Token": TOKEN},
                )
            self.assertEqual(failed.status_code, 500)
            self.assertTrue(failed.json["maintenance_required"])
            self.assertTrue(failed.json["maintenance"]["notice"])
            self.assertEqual(primary.read_text(encoding="utf-8"), "{damaged")
            self.assertEqual(restore._restore_archive_backup_path().read_bytes(), backup_before)

            recovered = app.test_client().get(
                "/api/backup/restore/" + request_id, headers={"X-Token": TOKEN},
            )
            self.assertEqual(recovered.status_code, 410)
            self.assertFalse(recovered.json["maintenance_required"])
            event_id = recovered.json["maintenance"]["event_id"]

            state_path = restore._restore_archive_maintenance_path()
            state_path.write_text("{damaged maintenance", encoding="utf-8")
            primary_before = primary.read_bytes()
            backup_before = restore._restore_archive_backup_path().read_bytes()
            blocked = app.test_client().post(
                "/api/backup/import",
                data={"file": (io.BytesIO(raw), "restore.tar.gz"), "restore_request_id": request_id,
                      "restore_items": ["scripts"]},
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(blocked.status_code, 500)
            self.assertTrue(blocked.json["maintenance_required"])
            self.assertEqual(primary.read_bytes(), primary_before)
            self.assertEqual(restore._restore_archive_backup_path().read_bytes(), backup_before)
            self.assertTrue(state_path.exists())

    def test_html_archive_replay_maintenance_ack_requires_session_csrf(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore
            from tests.test_auth_backup import csrf_token_from_html

            raw = restore_test_archive().getvalue()
            request_id = "d" * 32
            record = {
                "request_id": request_id,
                "archive_sha256": hashlib.sha256(raw).hexdigest(),
                "filename": "restore.tar.gz",
                "restore_items": ["scripts"],
                "restore_deps": False,
                "state": "restored",
                "scheduler_reload_required": False,
                "scheduler_reloaded": True,
                "dependencies_failed": False,
                "archived": True,
            }
            directory = base / ".backup_restore_requests"
            directory.mkdir()
            restore._write_restore_archive_index_pair({"version": 1, "records": {request_id: record}})
            restore._restore_archive_index_path().write_text("{damaged", encoding="utf-8")

            client = app.test_client()
            login_page = client.get("/login")
            login_csrf = csrf_token_from_html(login_page.get_data(as_text=True))
            login = client.post("/login", data={"token": TOKEN, "csrf_token": login_csrf})
            self.assertEqual(login.status_code, 302)
            client.get(login.headers["Location"])
            backup_page = client.get("/backup")
            form_csrf = csrf_token_from_html(backup_page.get_data(as_text=True))

            replay = client.post(
                "/backup/import",
                data={"file": (io.BytesIO(raw), "restore.tar.gz"), "restore_request_id": request_id,
                      "restore_items": ["scripts"], "restore_deps": "0", "csrf_token": form_csrf},
            )
            self.assertEqual(replay.status_code, 410)
            body = replay.get_data(as_text=True)
            self.assertIn(request_id, body)
            self.assertIn("确认已读并清除维护提示", body)
            event_id = restore._read_restore_archive_maintenance_state()["event_id"]
            html_csrf = csrf_token_from_html(body)

            rejected = client.post(
                "/backup/restore-maintenance/ack",
                data={"event_id": event_id, "request_id": request_id, "csrf_token": "bad"},
            )
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(restore._read_restore_archive_maintenance_state()["event_id"], event_id)

            acknowledged = client.post(
                "/backup/restore-maintenance/ack",
                data={"event_id": event_id, "request_id": request_id, "csrf_token": html_csrf},
            )
            self.assertEqual(acknowledged.status_code, 302)
            self.assertEqual(acknowledged.headers["Location"], "/backup?restore_request_id=" + request_id)
            self.assertTrue(restore._read_restore_archive_maintenance_state()["acknowledged"])

    def test_unrepairable_index_symlink_keeps_details_and_reports_maintenance(self):
        for linked_copy in ("primary", "backup"):
            with self.subTest(linked_copy=linked_copy):
                self.check_unrepairable_index_symlink(linked_copy)

    def check_unrepairable_index_symlink(self, linked_copy):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore

            directory = base / ".backup_restore_requests"
            directory.mkdir()
            request_id = "a" * 32
            restore._save_restore_request({
                "request_id": request_id,
                "archive_sha256": "1" * 64,
                "filename": "restore.tar.gz",
                "restore_items": ["scripts"],
                "restore_deps": False,
                "state": "restored",
                "scheduler_reload_required": False,
                "scheduler_reloaded": True,
                "dependencies_failed": False,
            })
            valid_index = {"version": 1, "records": {}}
            valid_path, linked_path = restore._restore_archive_index_path(), restore._restore_archive_backup_path()
            if linked_copy == "primary":
                valid_path, linked_path = linked_path, valid_path
            restore._write_restore_archive_index_file(valid_path, valid_index)
            target = base / "archive-index-target.json"
            target.write_text(json.dumps({"version": 1, "records": {}}), encoding="utf-8")
            linked_path.symlink_to(target)

            history = app.test_client().get(
                "/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN},
            )
            self.assertEqual(history.status_code, 200)
            self.assertIn("冗余副本修复尚未完成", history.json["maintenance_error"])
            self.assertTrue(history.json["maintenance_required"])
            self.assertTrue(restore._restore_request_path(request_id).exists())
            self.assertTrue(linked_path.is_symlink())
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), valid_index)

    def test_concurrent_history_compaction_keeps_archive_indexes_identical(self):
        with isolated_app() as (app, base), patch(
            "fls_manager.routes.backup.restore.RESTORE_RECEIPT_DETAIL_LIMIT", 2,
        ):
            from fls_manager.routes.backup import restore

            directory = base / ".backup_restore_requests"
            directory.mkdir()
            for number in range(1, 8):
                request_id = f"{number:032x}"
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
            clients = [app.test_client() for _ in range(4)]
            with ThreadPoolExecutor(4) as pool:
                responses = list(pool.map(
                    lambda client: client.get("/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN}),
                    clients,
                ))
            self.assertEqual([response.status_code for response in responses], [200] * 4)
            self.assertTrue(all(response.json["maintenance_error"] == "" for response in responses))
            self.assertTrue(all(response.json["total"] == 7 for response in responses))
            primary = restore._read_restore_archive_index()
            backup = restore._read_restore_archive_index_file(restore._restore_archive_backup_path())
            self.assertEqual(primary, backup)
            self.assertEqual(sum(restore._restore_request_path(f"{number:032x}").exists()
                                 for number in range(1, 8)), 2)

    def test_process_restart_recovers_crashes_after_index_publish_and_during_cleanup(self):
        for crash_point in ("after_backup_publish", "during_detail_cleanup"):
            with self.subTest(crash_point=crash_point), isolated_app() as (app, base):
                from fls_manager.routes.backup import restore

                raw = restore_test_archive().getvalue()
                directory = base / ".backup_restore_requests"
                directory.mkdir()
                request_ids = tuple(f"{number:032x}" for number in range(1, 4))
                for number, request_id in enumerate(request_ids, 1):
                    record = {
                        "request_id": request_id,
                        "archive_sha256": hashlib.sha256(raw).hexdigest(),
                        "filename": "restore.tar.gz",
                        "restore_items": ["data", "scripts"],
                        "restore_deps": False,
                        "state": "restored",
                        "scheduler_reload_required": False,
                        "scheduler_reloaded": True,
                        "dependencies_failed": False,
                        "created_at": f"2026-11-{number:02d} 00:00:00",
                        "updated_at": f"2026-11-{number:02d} 00:00:00",
                    }
                    restore._restore_request_path(request_id).write_text(json.dumps(record), encoding="utf-8")

                if crash_point == "after_backup_publish":
                    write_index = restore._write_restore_archive_index_file

                    def crash_after_backup(path, index):
                        result = write_index(path, index)
                        if path == restore._restore_archive_backup_path():
                            raise SystemExit("fixture process crash after backup index publish")
                        return result

                    patcher = patch.object(restore, "_write_restore_archive_index_file", crash_after_backup)
                else:
                    unlink = Path.unlink
                    target = restore._restore_request_path(request_ids[0])

                    def crash_during_cleanup(path, *args, **kwargs):
                        if path == target:
                            raise SystemExit("fixture process crash during archive detail cleanup")
                        return unlink(path, *args, **kwargs)

                    patcher = patch.object(Path, "unlink", crash_during_cleanup)

                with patch.object(restore, "RESTORE_RECEIPT_DETAIL_LIMIT", 1), patcher:
                    with self.assertRaises(SystemExit):
                        restore._compact_restore_receipts()
                self.assertTrue(all(restore._restore_request_path(request_id).exists() for request_id in request_ids))

                # A separate process retains only durable files, not flags, locks, or module state.
                restarted = subprocess.run(
                    [sys.executable, "-c", (
                        "from fls_manager.routes.backup import restore; "
                        "assert restore._compact_restore_receipts() == 0"
                    )],
                    cwd=Path(__file__).resolve().parents[1],
                    env=os.environ.copy(),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(restarted.returncode, 0, restarted.stderr)
                self.assertEqual(sum(restore._restore_request_path(request_id).exists()
                                     for request_id in request_ids), 1)
                primary = restore._read_restore_archive_index_file(restore._restore_archive_index_path())
                backup = restore._read_restore_archive_index_file(restore._restore_archive_backup_path())
                self.assertEqual(primary, backup)

                history = app.test_client().get(
                    "/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN},
                )
                self.assertEqual(history.status_code, 200)
                self.assertEqual(history.json["total"], 3)
                self.assertEqual(sum(item["archived"] for item in history.json["items"]), 2)
                with patch.object(restore, "_commit_restore_directories") as commit:
                    replay = self.post(app.test_client(), raw, restore_request_id=request_ids[0])
                self.assertEqual(replay.status_code, 410)
                commit.assert_not_called()

    def test_corrupt_or_symlinked_archive_index_fails_closed(self):
        for mode in ("invalid_json", "invalid_shape", "symlink", "both_invalid"):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                from fls_manager.routes.backup import restore

                directory = base / ".backup_restore_requests"
                directory.mkdir()
                index_path = restore._restore_archive_index_path()
                if mode in ("invalid_json", "both_invalid"):
                    index_path.write_text("{broken", encoding="utf-8")
                elif mode == "invalid_shape":
                    index_path.write_text(json.dumps({"version": 2, "records": {}}), encoding="utf-8")
                else:
                    target = base / "archive-index-target.json"
                    target.write_text(json.dumps({"version": 1, "records": {}}), encoding="utf-8")
                    index_path.symlink_to(target)
                if mode == "both_invalid":
                    restore._restore_archive_backup_path().write_text(
                        json.dumps({"version": 2, "records": {}}), encoding="utf-8",
                    )

                client = app.test_client()
                history = client.get("/api/backup/restore-history?limit=20", headers={"X-Token": TOKEN})
                self.assertEqual(history.status_code, 500)
                with patch.object(restore, "_commit_restore_directories") as commit:
                    replay = self.post(client, restore_test_archive().getvalue())
                    self.assertEqual(replay.status_code, 500)
                    commit.assert_not_called()

                self.assertTrue(index_path.is_symlink() if mode == "symlink" else index_path.exists())

    def test_archive_index_file_fsync_failure_does_not_publish_or_leave_temp_file(self):
        with isolated_app() as (_app, base):
            from fls_manager.routes.backup import restore

            directory = base / ".backup_restore_requests"
            directory.mkdir()
            path = restore._restore_archive_index_path()
            with patch.object(restore.os, "fsync", side_effect=OSError("fixture fsync failure")):
                with self.assertRaisesRegex(OSError, "fixture fsync failure"):
                    restore._write_restore_archive_index_file(path, {"version": 1, "records": {}})
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_index_repair_unions_partial_publications_and_rejects_identity_conflicts(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict), isolated_app() as (app, base):
                from fls_manager.routes.backup import restore

                directory = base / ".backup_restore_requests"
                directory.mkdir()
                summaries = {}
                for number in (1, 2):
                    request_id = f"{number:032x}"
                    summaries[request_id] = restore._restore_archive_summary({
                        "request_id": request_id, "archive_sha256": f"{number:064x}",
                        "restore_items": ["scripts"], "restore_deps": False,
                        "state": "restored", "scheduler_reload_required": False, "dependencies_failed": False,
                    })
                first_id, second_id = summaries
                primary = {"version": 1, "records": {first_id: summaries[first_id]}}
                backup = {"version": 1, "records": {second_id: summaries[second_id]}}
                if conflict:
                    backup["records"][first_id] = dict(summaries[first_id], archive_sha256="f" * 64)
                restore._write_restore_archive_index_file(restore._restore_archive_index_path(), primary)
                restore._write_restore_archive_index_file(restore._restore_archive_backup_path(), backup)
                original_files = {path: path.read_bytes() for path in directory.iterdir()}

                history = app.test_client().get("/api/backup/restore-history", headers={"X-Token": TOKEN})
                self.assertEqual(history.status_code, 500 if conflict else 200)
                if conflict:
                    self.assertEqual({path: path.read_bytes() for path in directory.iterdir()}, original_files)
                    with patch.object(restore, "_commit_restore_directories") as commit:
                        self.assertEqual(self.post(app.test_client(), restore_test_archive().getvalue()).status_code, 500)
                    commit.assert_not_called()
                else:
                    self.assertEqual({item["request_id"] for item in history.json["items"]}, set(summaries))
                    self.assertIn("自动合并并修复", history.json["maintenance_error"])
                    self.assertEqual(restore._read_restore_archive_index_file(restore._restore_archive_index_path()),
                                     restore._read_restore_archive_index_file(restore._restore_archive_backup_path()))

    def test_symlinked_index_temporary_file_is_never_followed(self):
        with isolated_app() as (_app, base):
            from fls_manager.routes.backup import restore

            restore.BACKUP_RESTORE_REQUESTS_DIR.mkdir()
            target = base / "index-temp-target"
            target.write_text("preserve target", encoding="utf-8")
            path = restore._restore_archive_index_path()
            temp = path.with_name(path.name + ".tmp")
            temp.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "临时文件必须是普通文件"):
                restore._write_restore_archive_index_file(path, {"version": 1, "records": {}})
            self.assertEqual(target.read_text(encoding="utf-8"), "preserve target")
            self.assertTrue(temp.is_symlink())
            self.assertFalse(path.exists())

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
