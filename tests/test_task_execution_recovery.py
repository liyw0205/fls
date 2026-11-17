import importlib
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app, sample_task
from tests.test_task_save_recovery import TaskFormParser


class TaskExecutionRecoveryTests(unittest.TestCase):
    def seed(self, command="echo execution"):
        from fls_manager import models
        models.save_tasks([sample_task("a", command=command), sample_task("b", command="echo other")])

    def post(self, client, action="run", request_id="a" * 32, task_id="a"):
        return client.post(f"/api/task/action/{action}/{task_id}",
                           json={"request_id": request_id}, headers={"X-Token": TOKEN})

    def wait_history(self, status):
        from fls_manager import models
        deadline = time.monotonic() + 6
        history = []
        while time.monotonic() < deadline:
            history = models.load_task_history()
            if history and history[0]["status"] == status:
                break
            time.sleep(0.02)
        self.assertTrue(history)
        self.assertEqual(history[0]["status"], status)
        return history

    def authenticate(self, client):
        with client.session_transaction() as session:
            session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                           security_verified=True, csrf_token="execution-csrf")

    def test_finished_short_run_replayed_after_restart_does_not_execute_twice(self):
        with isolated_app() as (app, _):
            from fls_manager import task_bulk, models
            self.seed()
            first = self.post(app.test_client())
            self.assertEqual(first.status_code, 200)
            self.wait_history("success")
            importlib.reload(task_bulk)
            with patch("fls_manager.routes.api.run_task_now") as run:
                replay = self.post(app.test_client())
                self.assertEqual(first.get_json(), replay.get_json())
                run.assert_not_called()
            self.assertEqual(len(models.load_task_history()), 1)
            self.assertEqual(models.get_task("a")["run_count"], 1)

    def test_parallel_run_and_request_id_selection_conflict(self):
        with isolated_app() as (app, _):
            self.seed()
            entered, release = threading.Event(), threading.Event()

            def submit(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return True, "submitted"

            with patch("fls_manager.routes.api.run_task_now", side_effect=submit) as run, ThreadPoolExecutor(2) as pool:
                first = pool.submit(self.post, app.test_client())
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.post, app.test_client())
                release.set()
                self.assertEqual(first.result(5).get_json(), second.result(5).get_json())
                self.assertEqual(self.post(app.test_client(), task_id="b").status_code, 409)
                self.assertEqual(self.post(app.test_client(), action="stop").status_code, 409)
                run.assert_called_once_with("a", source="manual")

    def test_stop_replay_preserves_later_real_run(self):
        with isolated_app() as (app, _):
            from fls_manager import task_runner
            self.seed("sleep 30")
            client = app.test_client()
            try:
                self.assertEqual(self.post(client).status_code, 200)
                self.wait_history("running")
                stopped = self.post(client, "stop", "b" * 32)
                self.assertEqual(stopped.status_code, 200)
                self.wait_history("stopped")
                self.assertEqual(self.post(client, request_id="c" * 32).status_code, 200)
                self.wait_history("running")
                with patch("fls_manager.routes.api.stop_task_now") as stop:
                    replay = self.post(client, "stop", "b" * 32)
                    self.assertEqual(stopped.get_json(), replay.get_json())
                    stop.assert_not_called()
                self.assertTrue(task_runner.is_running("a"))
            finally:
                task_runner.stop_task_now("a")

    def test_claim_and_result_write_failures_do_not_repeat_submission(self):
        with isolated_app() as (app, _):
            from fls_manager import task_bulk, models
            self.seed()
            with patch.object(task_bulk, "write_json", side_effect=OSError("claim full")), patch("fls_manager.routes.api.run_task_now") as run:
                self.assertEqual(self.post(app.test_client()).status_code, 500)
                run.assert_not_called()
            write = task_bulk.write_json

            def fail(path, record):
                if record["results"][0]["status"] == "succeeded":
                    raise OSError("result full")
                return write(path, record)

            with patch.object(task_bulk, "write_json", side_effect=fail):
                first = self.post(app.test_client())
            self.assertEqual(first.status_code, 500)
            self.assertEqual(first.get_json()["completed_ids"], ["a"])
            self.wait_history("success")
            with patch("fls_manager.routes.api.run_task_now") as run:
                replay = self.post(app.test_client())
                self.assertEqual(replay.status_code, 409)
                self.assertEqual(replay.get_json()["unknown_ids"], ["a"])
                run.assert_not_called()
            self.assertEqual(len(models.load_task_history()), 1)

    def test_invalid_request_permissions_csrf_and_missing_task_have_no_side_effects(self):
        with isolated_app() as (app, base):
            self.seed()
            client = app.test_client()
            before = (base / "data/tasks.json").read_bytes()
            with patch("fls_manager.routes.api.run_task_now") as run, patch("fls_manager.routes.api.stop_task_now") as stop:
                for action in ("run", "stop"):
                    url = f"/api/task/action/{action}/a"
                    for data in (None, [], {"request_id": ""}, {"request_id": None},
                                 {"request_id": True}, {"request_id": "../bad"}):
                        with self.subTest(action=action, data=data):
                            response = client.post(url, data=json.dumps(data), content_type="application/json", headers={"X-Token": TOKEN})
                            self.assertEqual(response.status_code, 400)
                    self.assertEqual(self.post(client, action, task_id="missing").status_code, 404)
                    self.authenticate(client)
                    self.assertEqual(client.post(url, json={"request_id": "b" * 32}).status_code, 400)
                    with patch("fls_manager.auth.query_token_enabled", return_value=False):
                        self.assertEqual(client.post(url + "?token=" + TOKEN, json={"request_id": "b" * 32}, headers={"X-CSRF-Token": "execution-csrf"}).status_code, 403)
                    with client.session_transaction() as session:
                        session["token_expire_at"] = 1
                    self.assertEqual(client.post(url, json={"request_id": "b" * 32}, headers={"X-CSRF-Token": "execution-csrf"}).status_code, 401)
                run.assert_not_called()
                stop.assert_not_called()
            self.assertEqual((base / "data/tasks.json").read_bytes(), before)
            self.assertFalse((base / "data/task_bulk_requests").exists())

    def test_form_failure_retry_and_api_replay_share_original_run(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            data = {"request_id": "a" * 32, "csrf_token": "execution-csrf"}
            with patch("fls_manager.routes.tasks.actions.run_task_now", return_value=(False, "任务启动失败：thread quota")):
                failed = client.post("/run/a?back=/collections", data=data)
            self.assertEqual(failed.status_code, 500)
            retry = TaskFormParser(failed.get_data(as_text=True)).forms[0]
            self.assertNotEqual(retry["fields"]["request_id"], data["request_id"])
            self.assertEqual(client.post(retry["action"], data={"request_id": retry["fields"]["request_id"]}).status_code, 400)
            success = client.post(retry["action"], data=retry["fields"], headers={"X-Requested-With": "FLS-Ajax"})
            self.assertEqual(success.status_code, 302)
            self.assertEqual(success.location, "/log/a?back=/collections")
            self.wait_history("success")
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
            with patch("fls_manager.routes.api.run_task_now") as run:
                self.assertEqual(self.post(client, request_id=retry["fields"]["request_id"]).status_code, 200)
                run.assert_not_called()
            self.assertEqual(len(models.load_task_history()), 1)

    def test_unknown_form_requires_confirmation_and_preserves_query_request(self):
        with isolated_app() as (app, _):
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            data = {"request_id": "a" * 32, "csrf_token": "execution-csrf"}
            with patch("fls_manager.routes.tasks.actions.run_task_now", side_effect=RuntimeError("after submission")) as run:
                failed = client.post("/run/a", data=data)
                self.assertEqual(failed.status_code, 409)
                self.assertIn("confirm(", failed.get_data(as_text=True))
                self.assertEqual(client.post("/run/a", data=data).status_code, 409)
                run.assert_called_once()
            retry = TaskFormParser(failed.get_data(as_text=True)).forms[0]
            self.assertNotEqual(retry["fields"]["request_id"], data["request_id"])
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
            self.wait_history("success")

    def test_stop_idle_is_idempotent_and_form_uses_same_record(self):
        with isolated_app() as (app, _):
            self.seed()
            client = app.test_client()
            first = self.post(client, "stop")
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.get_json()["ok"])
            with patch("fls_manager.routes.tasks.actions.stop_task_now") as stop:
                form = client.post("/stop/a", data={"request_id": "a" * 32}, headers={"X-Token": TOKEN})
                self.assertEqual(form.status_code, 302)
                stop.assert_not_called()

    def test_stop_form_failure_retries_and_permissions_block_side_effects(self):
        with isolated_app() as (app, _):
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            data = {"request_id": "b" * 32, "csrf_token": "execution-csrf"}
            with patch("fls_manager.routes.tasks.actions.stop_task_now", return_value=(False, "停止失败：process alive")) as stop:
                response = client.post("/stop/a?back=/collections", data=data)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(client.post("/stop/a", data=data).status_code, 409)
                stop.assert_called_once_with("a")
            retry = TaskFormParser(response.get_data(as_text=True)).forms[0]
            with patch("fls_manager.routes.tasks.actions.stop_task_now") as stop:
                self.assertEqual(client.post(retry["action"], data={"request_id": retry["fields"]["request_id"]}).status_code, 400)
                with patch("fls_manager.auth.query_token_enabled", return_value=False):
                    denied = client.post("/stop/a?token=" + TOKEN, data=data, headers={"X-Requested-With": "XMLHttpRequest"})
                    self.assertEqual(denied.status_code, 403)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post("/stop/a", data=data, headers={"X-Requested-With": "XMLHttpRequest"})
                self.assertEqual(expired.status_code, 401)
                stop.assert_not_called()
            self.authenticate(client)
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)

    def test_retry_timer_start_failure_finishes_attempt_and_history_form_reruns(self):
        with isolated_app() as (app, _):
            from fls_manager import models, task_runner
            task = sample_task("a", command="exit 1")
            task["retry"] = {"attempts": 1, "interval_seconds": 5}
            models.save_tasks([task])
            client = app.test_client()
            self.authenticate(client)
            created_timers = []

            class BrokenTimer:
                def __init__(self, *_args, **_kwargs):
                    self.cancelled = False
                    created_timers.append(self)

                def start(self):
                    raise RuntimeError("timer quota exhausted")

                def cancel(self):
                    self.cancelled = True

            request_id = "d" * 32
            with patch.object(task_runner.threading, "Timer", BrokenTimer):
                accepted = self.post(client, request_id=request_id)
                self.assertEqual(accepted.status_code, 200)
                first = self.wait_history("failed")[0]
                self.assertIn("重试计划失败：timer quota exhausted", first["message"])
                self.assertNotIn("a", task_runner.PENDING_RETRIES)
                self.assertNotIn("a", task_runner.RUNNING)
                self.assertTrue(created_timers[0].cancelled)

                replay = self.post(client, request_id=request_id)
                self.assertEqual(replay.get_json(), accepted.get_json())
                self.assertEqual(len(models.load_task_history()), 1)

                history_page = client.get("/history")
                self.assertEqual(history_page.status_code, 200)
                forms = TaskFormParser(history_page.get_data(as_text=True)).forms
                recovery = next(form for form in forms if form["action"].startswith("/run/a"))
                self.assertEqual(len(recovery["fields"]["request_id"]), 32)
                retry = client.post(
                    recovery["action"],
                    data=dict(recovery["fields"], csrf_token="execution-csrf"),
                    headers={"X-CSRF-Token": "execution-csrf"},
                )
                self.assertEqual(retry.status_code, 302)
                self.assertEqual(retry.location, "/log/a?back=/history")
                deadline = time.monotonic() + 6
                rows = models.load_task_history()
                while time.monotonic() < deadline and (
                    len(rows) < 2 or rows[0].get("id") == first.get("id") or rows[0].get("status") != "failed"
                ):
                    time.sleep(0.02)
                    rows = models.load_task_history()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["status"], "failed")
                self.assertNotIn("a", task_runner.PENDING_RETRIES)
                self.assertEqual(models.get_task("a")["run_count"], 2)

    def test_timeout_unknown_process_slot_stops_and_history_form_recovers(self):
        with isolated_app() as (app, base):
            from fls_manager import models, paths, task_runner
            task = sample_task("a", command="exit 0")
            models.save_tasks([task])
            log_file = paths.LOG_DIR / "late-timeout-exit.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("timeout termination was not confirmed\n", encoding="utf-8")

            class ExitedProcess:
                pid = 4321

                def poll(self):
                    return 9

            task_runner.RUNNING["a"] = {
                "status": "running", "process": ExitedProcess(), "pid": 4321,
                "history_id": "timeout-history", "history_start_ts": time.time() - 4,
                "log_file": str(log_file), "log_fp": None, "watcher_done": True,
                "stop_error": "超时后停止失败：无法确认任务进程已退出",
                "terminal_status": "timeout",
                "terminal_message": "任务超时，超过 5 秒；超时后停止失败：无法确认任务进程已退出",
            }
            models.save_task_history([{
                "id": "timeout-history", "task_id": "a", "task_name": "a",
                "command": "exit 0", "status": "running", "start_at": "2026-11-17 10:00:00",
                "end_at": "", "duration_seconds": 0, "return_code": None,
                "pid": 4321, "log_file": str(log_file), "message": "超时后停止失败",
            }])
            client = app.test_client()
            self.authenticate(client)

            rejected = self.post(client, action="run", request_id="a" * 32)
            self.assertEqual(rejected.status_code, 409)
            self.assertIn("任务已在运行中", rejected.json["msg"])
            self.assertTrue(task_runner.is_running("a"))

            stop_id = "b" * 32
            stopped = self.post(client, action="stop", request_id=stop_id)
            self.assertEqual(stopped.status_code, 200)
            self.assertEqual(stopped.json["stopped_count"], 1)
            self.assertEqual(models.load_task_history()[0]["status"], "timeout")
            self.assertIn("此前未确认退出，当前已确认进程退出", models.load_task_history()[0]["message"])
            with patch("fls_manager.routes.api.stop_task_now") as stop:
                replay = self.post(client, action="stop", request_id=stop_id)
                self.assertEqual(replay.json, stopped.json)
                stop.assert_not_called()

            forms = TaskFormParser(client.get("/history").get_data(as_text=True)).forms
            recovery = next(form for form in forms if form["action"].startswith("/run/a"))
            rerun = client.post(
                recovery["action"],
                data=dict(recovery["fields"], csrf_token="execution-csrf"),
                headers={"X-CSRF-Token": "execution-csrf"},
            )
            self.assertEqual(rerun.status_code, 302)
            self.assertEqual(rerun.location, "/log/a?back=/history")
            self.wait_history("success")
            self.assertEqual(models.get_task("a")["run_count"], 1)

    def test_terminal_history_write_failure_keeps_slot_and_form_recovers(self):
        with isolated_app() as (app, _):
            from fls_manager import models, paths, task_runner
            task = sample_task("a", command="exit 0")
            task["retry"] = {"attempts": 1, "interval_seconds": 5}
            models.save_tasks([task])
            log_file = paths.LOG_DIR / "terminal-history-recovery.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            history_record = {
                "id": "terminal-history", "task_id": "a", "task_name": "a",
                "command": "exit 0", "source": "manual", "status": "running",
                "retry_attempt": 0, "max_retries": 0, "start_at": "2026-11-17 10:00:00",
                "end_at": "", "duration_seconds": 0, "return_code": None,
                "pid": 4321, "log_file": str(log_file), "message": "运行中",
            }
            models.save_task_history([history_record])

            class ExitedProcess:
                pid = 4321

                def wait(self, timeout=None):
                    return 9

                def poll(self):
                    return 9

            class RecordingTimer:
                created = []

                def __init__(self, _interval, _callback):
                    self.cancelled = False
                    self.started = False
                    self.created.append(self)

                def start(self):
                    self.started = True

                def cancel(self):
                    self.cancelled = True

            process = ExitedProcess()
            log_fp = open(log_file, "ab", buffering=0)
            task_runner.RUNNING["a"] = {
                "status": "running", "process": process, "pid": process.pid,
                "history_id": history_record["id"], "history_start_ts": time.time() - 2,
                "history_record": dict(history_record), "log_file": str(log_file),
                "log_fp": log_fp, "watcher_done": False,
            }

            RecordingTimer.created = []
            with patch.object(task_runner.threading, "Timer", RecordingTimer), \
                    patch.object(task_runner, "update_task_history", side_effect=OSError("disk full")):
                task_runner.task_finish_watcher(
                    "a", task, process, str(log_file), log_fp,
                    history_record["id"], time.time() - 2, 0,
                )

            self.assertTrue(task_runner.is_running("a"))
            self.assertIn("a", task_runner.running_task_snapshot(["a"]))
            self.assertEqual(task_runner.RUNNING["a"]["terminal_status"], "failed")
            self.assertNotIn("a", task_runner.PENDING_RETRIES)
            self.assertTrue(RecordingTimer.created[0].cancelled)

            client = app.test_client()
            self.authenticate(client)
            api_request_id = "c" * 32
            with patch.object(task_runner, "update_task_history", side_effect=OSError("disk full")):
                api_failed = self.post(client, action="stop", request_id=api_request_id)
            self.assertEqual(api_failed.status_code, 409)
            self.assertIn("a", api_failed.get_json()["retry_ids"])
            self.assertIn("历史保存失败", api_failed.get_json()["failures"][0])
            with patch("fls_manager.routes.api.stop_task_now") as stop:
                replay = self.post(client, action="stop", request_id=api_request_id)
                self.assertEqual(replay.get_json(), api_failed.get_json())
                stop.assert_not_called()

            request_id = "d" * 32
            with patch.object(task_runner, "update_task_history", side_effect=OSError("disk full")):
                failed = client.post(
                    "/stop/a?back=/history",
                    data={"request_id": request_id, "csrf_token": "execution-csrf"},
                    headers={"X-CSRF-Token": "execution-csrf"},
                )
            self.assertEqual(failed.status_code, 409)
            self.assertIn("历史保存失败", failed.get_data(as_text=True))
            retry = TaskFormParser(failed.get_data(as_text=True)).forms[0]
            self.assertNotEqual(retry["fields"]["request_id"], request_id)

            history_path = paths.TASK_HISTORY_FILE
            damaged = b"{broken history\n"
            history_path.write_bytes(damaged)
            still_failed = client.post(
                retry["action"],
                data=dict(retry["fields"], csrf_token="execution-csrf"),
                headers={"X-CSRF-Token": "execution-csrf"},
            )
            self.assertEqual(still_failed.status_code, 409)
            self.assertEqual(history_path.read_bytes(), damaged)
            retry = TaskFormParser(still_failed.get_data(as_text=True)).forms[0]

            models.save_task_history([])
            recovered = client.post(
                retry["action"],
                data=dict(retry["fields"], csrf_token="execution-csrf"),
                headers={"X-CSRF-Token": "execution-csrf"},
            )
            self.assertEqual(recovered.status_code, 302)
            self.assertFalse(task_runner.is_running("a"))
            stop_replay = client.post(
                retry["action"],
                data=dict(retry["fields"], csrf_token="execution-csrf"),
                headers={"X-CSRF-Token": "execution-csrf"},
            )
            self.assertEqual(stop_replay.status_code, 302)
            history = models.load_task_history()[0]
            self.assertEqual(history["status"], "failed")
            self.assertIn("此前未确认退出，当前已确认进程退出", history["message"])

            forms = TaskFormParser(client.get("/history").get_data(as_text=True)).forms
            rerun = next(form for form in forms if form["action"].startswith("/run/a"))
            self.assertEqual(
                client.post(
                    rerun["action"],
                    data=dict(rerun["fields"], csrf_token="execution-csrf"),
                    headers={"X-CSRF-Token": "execution-csrf"},
                ).status_code,
                302,
            )
            self.wait_history("success")

    def test_pending_rename_keeps_recovery_link_and_never_starts_process(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            with patch("fls_manager.script_rename.pending_script_rename", return_value={"id": "c" * 32}):
                response = self.post(client)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.get_json()["recovery_url"], "/scripts/rename-recovery/" + "c" * 32)
                form = client.post("/run/a", data={"request_id": "b" * 32, "csrf_token": "execution-csrf"})
                self.assertEqual(form.status_code, 409)
                self.assertIn('href="/scripts/rename-recovery/' + "c" * 32 + '"', form.get_data(as_text=True))
            self.assertEqual(models.load_task_history(), [])

    def test_all_execution_forms_have_stable_ids_without_affecting_config_save(self):
        with isolated_app() as (app, base):
            from fls_manager import models, paths
            self.seed()
            (paths.SCRIPT_DIR / "execution.cfg").write_text("original")
            models.save_tasks([sample_task("a", command="echo execution", config_path="execution.cfg", collection_id="c1")])
            models.save_collections([{"id": "c1", "name": "Execution"}])
            models.save_task_history([{"id": "old", "task_id": "a", "status": "unknown"}])
            client = app.test_client()
            for path in ("/collections", "/log/a", "/history"):
                with self.subTest(path=path):
                    response = client.get(path, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 200)
                    forms = [form for form in TaskFormParser(response.get_data(as_text=True)).forms if form["action"].startswith(("/run/", "/stop/"))]
                    self.assertTrue(forms)
                    for form in forms:
                        self.assertRegex(form["fields"]["request_id"], r"^[a-f0-9]{32}$")
            response = client.get("/task/config/a", headers={"X-Token": TOKEN})
            self.assertIn('name="request_id"', response.get_data(as_text=True))
            config_form = TaskFormParser(response.get_data(as_text=True)).forms[0]
            config_form["fields"].update(content="saved config")
            self.assertEqual(client.post(config_form["action"], data=config_form["fields"], headers={"X-Token": TOKEN}).status_code, 200)
            self.assertEqual((paths.SCRIPT_DIR / "execution.cfg").read_text(), "saved config")
            self.assertFalse((base / "data/task_bulk_requests").exists())
