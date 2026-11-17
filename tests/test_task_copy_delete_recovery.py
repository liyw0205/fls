import importlib
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app, sample_task
from tests.test_task_save_recovery import TaskFormParser


class TaskCopyDeleteRecoveryTests(unittest.TestCase):
    def seed(self):
        from fls_manager import models
        models.save_tasks([sample_task("a", command="echo original", cron="0 8 * * *", run_count=7, pinned=True),
                           sample_task("b", command="echo other")])

    def post(self, client, action="copy", task_id="a", request_id="c" * 32):
        return client.post(f"/api/task/action/{action}/{task_id}", json={"request_id": request_id}, headers={"X-Token": TOKEN})

    def test_copy_reload_failure_duplicate_after_restart_and_real_recovery(self):
        with isolated_app() as (app, _):
            from fls_manager import models, scheduler, task_bulk
            self.seed()
            with patch("fls_manager.routes.api.reload_scheduler", side_effect=OSError("reload rejected")):
                response = self.post(app.test_client())
            self.assertEqual(response.status_code, 503)
            payload = response.get_json()
            self.assertTrue(payload["saved"])
            self.assertTrue(payload["reload_required"])
            self.assertEqual(payload["retry_ids"], [])
            copy_id = payload["created_ids"][0]
            copied = models.get_task(copy_id)
            self.assertEqual(copied["name"], "Task a-copy")
            self.assertEqual(copied["run_count"], 0)
            self.assertFalse(copied["pinned"])
            importlib.reload(task_bulk)
            models.save_tasks([task for task in models.load_tasks() if task["id"] != "a"])
            with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                self.assertEqual(self.post(app.test_client()).get_json(), payload)
                mutate.assert_not_called()
            self.assertEqual(app.test_client().post("/api/scheduler/reload", headers={"X-Token": TOKEN}).status_code, 200)
            self.assertIsNotNone(scheduler.scheduler.get_job("task_" + copy_id))
            self.assertEqual(len(models.load_tasks()), 2)

    def test_copy_concurrent_replays_create_one_and_conflicting_request_does_not_mutate(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            entered, release = threading.Event(), threading.Event()

            def reload():
                entered.set()
                self.assertTrue(release.wait(5))

            with patch("fls_manager.routes.api.reload_scheduler", side_effect=reload), ThreadPoolExecutor(2) as pool:
                first = pool.submit(self.post, app.test_client())
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.post, app.test_client())
                release.set()
                self.assertEqual(first.result(5).get_json(), second.result(5).get_json())
            before = models.load_tasks()
            self.assertEqual(self.post(app.test_client(), task_id="b").status_code, 409)
            self.assertEqual(self.post(app.test_client(), action="delete").status_code, 409)
            self.assertEqual(models.load_tasks(), before)
            self.assertEqual(len(before), 3)

    def test_copy_file_write_failure_then_retry_and_publication_error_truth(self):
        for published in (False, True):
            with self.subTest(published=published), isolated_app() as (app, _):
                from fls_manager import models, paths
                self.seed()
                replace = Path.replace

                def fail(path, target):
                    if target == paths.TASK_FILE:
                        if published:
                            replace(path, target)
                        raise OSError("publication fault")
                    return replace(path, target)

                with patch.object(Path, "replace", fail):
                    response = self.post(app.test_client())
                self.assertEqual(response.status_code, 503 if published else 500)
                self.assertEqual(response.get_json()["saved"], published)
                self.assertEqual(len(models.load_tasks()), 3 if published else 2)
                if not published:
                    self.assertEqual(response.get_json()["retry_ids"], ["a"])
                    retry = self.post(app.test_client(), request_id="d" * 32)
                    self.assertEqual(retry.status_code, 200)
                    self.assertEqual(len(models.load_tasks()), 3)

    def test_copy_record_completion_failure_reconciles_target_without_duplicate(self):
        with isolated_app() as (app, _):
            from fls_manager import task_bulk, models
            self.seed()
            write = task_bulk.write_json

            def fail(path, record):
                if "payload" in record:
                    raise OSError("receipt full")
                return write(path, record)

            with patch.object(task_bulk, "write_json", side_effect=fail):
                first = self.post(app.test_client())
            self.assertEqual(first.status_code, 500)
            self.assertEqual(len(first.get_json()["created_ids"]), 1)
            importlib.reload(task_bulk)
            with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                replay = self.post(app.test_client())
                self.assertEqual(replay.status_code, 409)
                self.assertTrue(replay.get_json()["saved"])
                self.assertEqual(replay.get_json()["created_ids"], first.get_json()["created_ids"])
                self.assertEqual(replay.get_json()["unknown_ids"], [])
                mutate.assert_not_called()
            self.assertEqual(len(models.load_tasks()), 3)

    def test_invalid_requests_claim_failure_corrupt_record_and_permissions_do_not_mutate(self):
        with isolated_app() as (app, base):
            from fls_manager import task_bulk
            self.seed()
            before = (base / "data/tasks.json").read_bytes()
            client = app.test_client()
            with patch("fls_manager.routes.api.mutate_tasks") as mutate, patch("fls_manager.routes.api.stop_task_now") as stop:
                for action in ("copy", "delete"):
                    for data in ([], None, {"request_id": "../bad"}, {"request_id": True}):
                        with self.subTest(action=action, data=data):
                            response = client.post(f"/api/task/action/{action}/a", data=json.dumps(data), content_type="application/json", headers={"X-Token": TOKEN})
                            self.assertEqual(response.status_code, 400)
                    self.assertEqual(self.post(client, action, "missing").status_code, 404)
                    with client.session_transaction() as session:
                        session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True, csrf_token="copy-csrf")
                    url = f"/api/task/action/{action}/a"
                    self.assertEqual(client.post(url, json={"request_id": "d" * 32}).status_code, 400)
                    with patch("fls_manager.auth.query_token_enabled", return_value=False):
                        self.assertEqual(client.post(url + "?token=" + TOKEN, json={}, headers={"X-CSRF-Token": "copy-csrf"}).status_code, 403)
                    with client.session_transaction() as session:
                        session["token_expire_at"] = 1
                    self.assertEqual(client.post(url, json={}, headers={"X-CSRF-Token": "copy-csrf"}).status_code, 401)
                self.assertEqual(client.post("/api/task/bulk-action", json={"action": "copy", "task_ids": ["a", "b"]}, headers={"X-Token": TOKEN}).status_code, 400)
                with patch.object(task_bulk, "write_json", side_effect=OSError("claim full")):
                    self.assertEqual(self.post(client).status_code, 500)
                record = task_bulk._record_path("c" * 32)
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text("broken")
                self.assertEqual(self.post(client).status_code, 500)
                self.assertEqual(record.read_text(), "broken")
                mutate.assert_not_called()
                stop.assert_not_called()
            self.assertEqual((base / "data/tasks.json").read_bytes(), before)

    def test_delete_reload_failure_and_lost_receipt_reconcile_without_second_stop(self):
        for receipt_failure in (False, True):
            with self.subTest(receipt_failure=receipt_failure), isolated_app() as (app, _):
                from fls_manager import models, scheduler, task_bulk
                self.seed()
                scheduler.reload_scheduler()
                write = task_bulk.write_json

                def save(path, record):
                    if receipt_failure and "payload" in record:
                        raise OSError("receipt full")
                    return write(path, record)

                with patch.object(task_bulk, "write_json", side_effect=save), patch("fls_manager.routes.api.reload_scheduler", side_effect=OSError("reload rejected")):
                    response = self.post(app.test_client(), "delete")
                self.assertEqual(response.status_code, 500 if receipt_failure else 503)
                self.assertIsNone(models.get_task("a"))
                with patch("fls_manager.routes.api.stop_task_now") as stop, patch("fls_manager.routes.api.mutate_tasks") as mutate:
                    replay = self.post(app.test_client(), "delete")
                    self.assertTrue(replay.get_json()["saved"])
                    self.assertEqual(replay.get_json()["completed_ids"], ["a"])
                    self.assertTrue(replay.get_json()["reload_required"])
                    stop.assert_not_called()
                    mutate.assert_not_called()
                self.assertEqual(app.test_client().post("/api/scheduler/reload", headers={"X-Token": TOKEN}).status_code, 200)
                self.assertIsNone(scheduler.scheduler.get_job("task_a"))

    def test_delete_rechecks_running_state_in_transaction_and_keeps_restarted_task(self):
        with isolated_app() as (app, _):
            from fls_manager import models, task_runner
            self.seed()

            def stop_then_restart(task_id):
                task_runner.RUNNING[task_id] = {"status": "starting", "history_id": "new-run"}
                return True, "stopped"

            try:
                with patch("fls_manager.routes.api.stop_task_now", side_effect=stop_then_restart), patch("fls_manager.routes.api.reload_scheduler") as reload:
                    response = self.post(app.test_client(), "delete")
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.get_json()["retry_ids"], ["a"])
                self.assertFalse(response.get_json()["saved"])
                self.assertIsNotNone(models.get_task("a"))
                reload.assert_not_called()
            finally:
                task_runner.RUNNING.pop("a", None)

    def test_delete_form_storage_failure_retries_then_saved_reload_error_recovers_with_csrf(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True, csrf_token="delete-csrf")
            data = {"request_id": "d" * 32, "csrf_token": "delete-csrf"}
            with patch("fls_manager.routes.tasks.actions.mutate_tasks", side_effect=OSError("read only")):
                failed = client.post("/task/delete/a?back=/tasks", data=data)
            self.assertEqual(failed.status_code, 500)
            self.assertIsNotNone(models.get_task("a"))
            retry = TaskFormParser(failed.get_data(as_text=True)).forms[0]
            self.assertNotEqual(retry["fields"]["request_id"], data["request_id"])
            self.assertEqual(client.post(retry["action"], data={"request_id": retry["fields"]["request_id"]}).status_code, 400)
            with patch("fls_manager.routes.tasks.actions.reload_scheduler", side_effect=OSError("reload rejected")):
                saved = client.post(retry["action"], data=retry["fields"], headers={"X-Requested-With": "FLS-Ajax"})
            self.assertEqual(saved.status_code, 503)
            self.assertIsNone(models.get_task("a"))
            recovery = TaskFormParser(saved.get_data(as_text=True)).forms[0]
            self.assertTrue(recovery["action"].startswith("/scheduler/reload"))
            self.assertEqual(client.post(recovery["action"], data=recovery["fields"]).status_code, 302)
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 503)
            self.assertIsNone(models.get_task("a"))

    def test_copy_during_pending_script_rename_is_rejected_without_creating_reference(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            before = models.load_tasks()
            with patch("fls_manager.script_rename.pending_script_rename", return_value={"id": "e" * 32}):
                response = self.post(app.test_client())
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()["created_ids"], [])
            self.assertEqual(models.load_tasks(), before)

    def test_incomplete_delete_form_offers_retry_even_when_reload_is_required(self):
        with isolated_app() as (app, _):
            from fls_manager import models, task_bulk
            self.seed()
            request_id = "d" * 32
            task_bulk.write_json(task_bulk._record_path(request_id), {
                "request_id": request_id, "action": "delete", "task_ids": ["a"],
                "results": [{"task_id": "a", "status": "unknown", "msg": "interrupted"}],
            })
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="delete-csrf")
            with patch("fls_manager.routes.tasks.actions.stop_task_now") as stop:
                response = client.post("/task/delete/a", data={"request_id": request_id, "csrf_token": "delete-csrf"})
                self.assertEqual(response.status_code, 409)
                stop.assert_not_called()
            forms = TaskFormParser(response.get_data(as_text=True)).forms
            self.assertTrue(any(form["action"].startswith("/scheduler/reload") for form in forms))
            retry = next(form for form in forms if form["action"].startswith("/task/delete/a"))
            self.assertNotEqual(retry["fields"]["request_id"], request_id)
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
            self.assertIsNone(models.get_task("a"))
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)

    def test_copy_id_collision_and_incomplete_record_fail_closed(self):
        with isolated_app() as (app, _):
            from fls_manager import models, task_bulk
            self.seed()
            existing_id = "e" * 32
            models.save_tasks(models.load_tasks() + [sample_task(existing_id)])
            before = models.load_tasks()
            with patch.object(task_bulk.uuid, "uuid4") as new_id:
                new_id.return_value.hex = existing_id
                response = self.post(app.test_client())
            self.assertEqual(response.status_code, 409)
            self.assertFalse(response.get_json()["saved"])
            self.assertEqual(response.get_json()["created_ids"], [])
            request_id = "f" * 32
            for copy_ids in (None, {}, {"a": "../bad"}, {"a": 123}):
                with self.subTest(copy_ids=copy_ids):
                    task_bulk.write_json(task_bulk._record_path(request_id), {
                        "request_id": request_id, "action": "copy", "task_ids": ["a"], "copy_ids": copy_ids,
                        "results": [{"task_id": "a", "status": "unknown", "msg": "interrupted"}],
                    })
                    with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                        self.assertEqual(self.post(app.test_client(), request_id=request_id).status_code, 500)
                        mutate.assert_not_called()
                    self.assertEqual(models.load_tasks(), before)

    def test_delete_running_process_finishes_history_and_removes_schedule_once(self):
        with isolated_app() as (app, _):
            from fls_manager import models, scheduler, task_runner
            models.save_tasks([sample_task("a", command="sleep 30", cron="0 8 * * *")])
            scheduler.reload_scheduler()
            client = app.test_client()
            try:
                self.assertEqual(client.post("/api/task/action/run/a", headers={"X-Token": TOKEN}).status_code, 200)
                deadline = time.monotonic() + 5
                proc = None
                while time.monotonic() < deadline:
                    proc = task_runner.running_task_snapshot().get("a", {}).get("process")
                    if proc is not None:
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(proc)
                response = self.post(client, "delete")
                self.assertEqual(response.status_code, 200)
                self.assertIsNotNone(proc.poll())
                self.assertIsNone(models.get_task("a"))
                self.assertIsNone(scheduler.scheduler.get_job("task_a"))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    history = models.load_task_history()
                    if history and history[0]["status"] == "stopped":
                        break
                    time.sleep(0.02)
                self.assertEqual(history[0]["status"], "stopped")
                with patch("fls_manager.routes.api.stop_task_now") as stop:
                    self.assertEqual(self.post(client, "delete").get_json(), response.get_json())
                    stop.assert_not_called()
                self.assertEqual(len(models.load_task_history()), 1)
            finally:
                task_runner.stop_task_now("a")
