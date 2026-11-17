import importlib
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app, sample_task


class TaskBulkRecoveryTests(unittest.TestCase):
    def seed(self):
        from fls_manager.models import save_tasks
        save_tasks([sample_task(task_id, command="echo bulk", cron="0 8 * * *", collection_id="c1")
                    for task_id in ("a", "b", "c")])

    def post(self, client, action="run", ids=None, request_id="a" * 32):
        return client.post("/api/task/bulk-action", json={
            "action": action, "task_ids": ids or ["a", "b", "c"], "request_id": request_id,
        }, headers={"X-Token": TOKEN})

    def test_run_partial_exception_is_unknown_and_replay_and_retry_do_not_repeat_success(self):
        with isolated_app() as (app, _):
            self.seed()
            client = app.test_client()
            with patch("fls_manager.routes.api.run_task_now", side_effect=[
                (True, "已提交启动"), RuntimeError("after submit"), (False, "任务启动失败：thread"),
            ]) as run:
                first = self.post(client)
                replay = self.post(client)
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.get_json(), replay.get_json())
                self.assertEqual(run.call_count, 3)
                payload = first.get_json()
                self.assertEqual(payload["completed_ids"], ["a"])
                self.assertEqual(payload["unknown_ids"], ["b"])
                self.assertEqual(payload["retry_ids"], ["c"])
                self.assertEqual(self.post(client, ids=["c"]).status_code, 409)
                self.assertEqual(run.call_count, 3)
            with patch("fls_manager.routes.api.run_task_now", return_value=(True, "已提交启动")) as run:
                retry = self.post(client, ids=payload["retry_ids"], request_id="b" * 32)
                self.assertEqual(retry.status_code, 200)
                run.assert_called_once_with("c", source="manual")

    def test_real_short_task_replay_after_completion_and_module_restart_does_not_run_twice(self):
        with isolated_app() as (app, _):
            from fls_manager import models, task_bulk
            self.seed()
            first = self.post(app.test_client(), ids=["a"])
            self.assertEqual(first.status_code, 200)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                history = models.load_task_history()
                if history and history[0]["status"] == "success":
                    break
                time.sleep(0.03)
            self.assertEqual(history[0]["status"], "success")
            importlib.reload(task_bulk)
            replay = self.post(app.test_client(), ids=["a"])
            self.assertEqual(first.get_json(), replay.get_json())
            self.assertEqual(len(models.load_task_history()), 1)
            self.assertEqual(models.get_task("a")["run_count"], 1)

    def test_parallel_duplicate_submission_runs_once(self):
        with isolated_app() as (app, _):
            self.seed()
            entered = threading.Event()
            release = threading.Event()

            def run(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return True, "submitted"

            with patch("fls_manager.routes.api.run_task_now", side_effect=run) as execute, ThreadPoolExecutor(2) as pool:
                first = pool.submit(self.post, app.test_client(), ids=["a"])
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.post, app.test_client(), ids=["a"])
                release.set()
                self.assertEqual(first.result(5).get_json(), second.result(5).get_json())
                execute.assert_called_once()

    def test_recovery_link_lookup_failure_preserves_completed_results(self):
        with isolated_app() as (app, _):
            self.seed()
            with patch("fls_manager.routes.api.run_task_now", side_effect=[
                (True, "submitted"), (False, "脚本重命名待恢复"),
            ]), patch("fls_manager.script_rename.pending_script_rename", side_effect=OSError("record unreadable")):
                response = self.post(app.test_client(), ids=["a", "b"])
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.get_json()["completed_ids"], ["a"])
            self.assertEqual(response.get_json()["retry_ids"], ["b"])
            self.assertEqual(response.get_json()["submitted_count"], 1)

    def test_invalid_selection_and_permissions_have_zero_side_effects(self):
        with isolated_app() as (app, base_dir):
            self.seed()
            target = base_dir / "data" / "tasks.json"
            before = target.read_bytes()
            client = app.test_client()
            with patch("fls_manager.routes.api.run_task_now") as run, patch("fls_manager.routes.api.mutate_tasks") as mutate:
                for data in (["run"], None, {"action": "run", "task_ids": {"a": 1}},
                             {"action": "run", "task_ids": ["a", True]},
                             {"action": "run", "task_ids": ["a", {}]},
                             {"action": "run", "task_ids": ["a"], "request_id": "../bad"}):
                    with self.subTest(data=data):
                        response = client.post("/api/task/bulk-action", data=json.dumps(data), content_type="application/json", headers={"X-Token": TOKEN})
                        self.assertEqual(response.status_code, 400)
                for action in ("run", "stop", "delete", "enable", "disable", "clear_collection"):
                    with self.subTest(action=action):
                        self.assertEqual(self.post(client, action, ["a", "missing"]).status_code, 404)
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True, csrf_token="bulk-csrf")
                self.assertEqual(client.post("/api/task/bulk-action", data={"action": "run", "task_ids": ["a"]}).status_code, 400)
                with patch("fls_manager.auth.query_token_enabled", return_value=False):
                    denied = client.post("/api/task/bulk-action?token=" + TOKEN, data={"action": "run", "task_ids": ["a"], "csrf_token": "bulk-csrf"})
                    self.assertEqual(denied.status_code, 403)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post("/api/task/bulk-action", data={"action": "run", "task_ids": ["a"], "csrf_token": "bulk-csrf"})
                self.assertEqual(expired.status_code, 401)
                run.assert_not_called()
                mutate.assert_not_called()
            self.assertEqual(target.read_bytes(), before)
            self.assertFalse((base_dir / "data" / "task_bulk_requests").exists())

    def test_claim_failure_and_corrupt_record_never_execute(self):
        with isolated_app() as (app, _):
            from fls_manager import task_bulk
            self.seed()
            client = app.test_client()
            with patch.object(task_bulk, "write_json", side_effect=OSError("disk full")), patch("fls_manager.routes.api.run_task_now") as run:
                self.assertEqual(self.post(client).status_code, 500)
                run.assert_not_called()
            target = task_bulk._record_path("a" * 32)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("broken json")
            with patch("fls_manager.routes.api.run_task_now") as run:
                self.assertEqual(self.post(client).status_code, 500)
                run.assert_not_called()
            self.assertEqual(target.read_text(), "broken json")

    def test_interrupted_result_write_keeps_confirmed_items_and_does_not_repeat_unknown(self):
        with isolated_app() as (app, _):
            from fls_manager import task_bulk
            self.seed()
            write = task_bulk.write_json

            def fail_after_first_result(path, record):
                if record["results"][0]["status"] == "succeeded":
                    raise OSError("result publication failed")
                return write(path, record)

            with patch.object(task_bulk, "write_json", side_effect=fail_after_first_result), patch("fls_manager.routes.api.run_task_now", return_value=(True, "submitted")) as run:
                response = self.post(app.test_client())
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.get_json()["completed_ids"], ["a"])
                self.assertEqual(response.get_json()["submitted_count"], 1)
                self.assertEqual(response.get_json()["retry_ids"], ["b", "c"])
                run.assert_called_once()
            with patch("fls_manager.routes.api.run_task_now") as run:
                recovered = self.post(app.test_client())
                self.assertEqual(recovered.status_code, 409)
                self.assertEqual(recovered.get_json()["unknown_ids"], ["a"])
                self.assertEqual(recovered.get_json()["retry_ids"], ["b", "c"])
                run.assert_not_called()

    def test_disable_and_delete_saved_reload_failure_recovers_without_reapplying(self):
        for action in ("disable", "delete"):
            with self.subTest(action=action), isolated_app() as (app, _):
                from fls_manager import models, scheduler
                self.seed()
                scheduler.reload_scheduler()
                client = app.test_client()
                with patch("fls_manager.routes.api.reload_scheduler", side_effect=RuntimeError("reload rejected")):
                    response = self.post(client, action, ["a"])
                payload = response.get_json()
                self.assertEqual(response.status_code, 503)
                self.assertTrue(payload["saved"])
                self.assertTrue(payload["reload_required"])
                self.assertEqual(payload["completed_ids"], ["a"])
                self.assertEqual(payload["retry_ids"], [])
                self.assertTrue(models.get_task("a") is None if action == "delete" else not models.get_task("a")["enabled"])
                with patch("fls_manager.routes.api.mutate_tasks") as mutate, patch("fls_manager.routes.api.stop_task_now") as stop:
                    replay = self.post(client, action, ["a"])
                    self.assertEqual(replay.get_json(), payload)
                    mutate.assert_not_called()
                    stop.assert_not_called()
                    restored = client.post("/api/scheduler/reload", headers={"X-Token": TOKEN})
                    self.assertEqual(restored.status_code, 200)
                    self.assertNotIn("task_a", [job.id for job in scheduler.scheduler.get_jobs()])

    def test_storage_failure_reports_stopped_but_not_deleted_then_form_retry_succeeds(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            with patch("fls_manager.routes.api.mutate_tasks", side_effect=OSError("read only")):
                response = self.post(client, "delete", ["a"])
            self.assertEqual(response.status_code, 500)
            payload = response.get_json()
            self.assertFalse(payload["saved"])
            self.assertEqual(payload["stopped_ids"], ["a"])
            self.assertEqual(payload["deleted_count"], 0)
            self.assertEqual(payload["failed_count"], 1)
            self.assertEqual(payload["retry_ids"], ["a"])
            self.assertIsNotNone(models.get_task("a"))
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True, csrf_token="bulk-csrf")
            data = {"action": "delete", "task_ids": ["a"], "request_id": "b" * 32, "csrf_token": "bulk-csrf"}
            retry = client.post("/api/task/bulk-action", data=data)
            self.assertEqual(retry.status_code, 200)
            self.assertIsNone(models.get_task("a"))
            self.assertEqual(client.post("/api/task/bulk-action", data=data).get_json(), retry.get_json())

    def test_after_publication_error_reports_saved_state(self):
        with isolated_app() as (app, _):
            self.seed()
            from fls_manager import models, paths
            replace = Path.replace

            def publish_then_fail(path, target):
                result = replace(path, target)
                if target == paths.TASK_FILE:
                    raise OSError("after publication")
                return result

            with patch.object(Path, "replace", publish_then_fail):
                response = self.post(app.test_client(), "disable", ["a"])
            self.assertEqual(response.status_code, 503)
            self.assertTrue(response.get_json()["saved"])
            self.assertEqual(response.get_json()["retry_ids"], [])
            self.assertFalse(models.get_task("a")["enabled"])

    def test_selection_rechecked_in_transaction_and_retry_cleanup_failure_reports_saved(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            mutate = models.mutate_tasks

            def remove_before_mutation(*args, **kwargs):
                models.save_tasks([task for task in models.load_tasks() if task["id"] != "b"])
                return mutate(*args, **kwargs)

            with patch("fls_manager.routes.api.mutate_tasks", side_effect=remove_before_mutation), patch("fls_manager.routes.api.reload_scheduler") as reload:
                response = self.post(app.test_client(), "disable", ["a", "b"])
                self.assertEqual(response.status_code, 409)
                self.assertFalse(response.get_json()["saved"])
                self.assertTrue(models.get_task("a")["enabled"])
                reload.assert_not_called()
            with patch("fls_manager.routes.api.cancel_task_retry", side_effect=OSError("retry cleanup")):
                response = self.post(app.test_client(), "disable", ["a"], request_id="b" * 32)
                self.assertEqual(response.status_code, 503)
                self.assertTrue(response.get_json()["saved"])
                self.assertTrue(response.get_json()["reload_required"])
                self.assertEqual(response.get_json()["retry_ids"], [])
                self.assertFalse(models.get_task("a")["enabled"])
